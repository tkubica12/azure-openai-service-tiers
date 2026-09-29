"""Collect metering / billing evidence into results/billing_evidence.json.

Three independent sources:
1. Azure Monitor metrics of the Foundry resource split by ServiceTierRequest / ServiceTierResponse
   (how Azure *meters* Flex vs Standard on the very same deployment).
2. Cost Management actual cost of the resource group grouped by Meter (which billing meters were charged).
   Cost data typically lags 8-24 h behind usage.
3. Azure Retail Prices API (public) - list prices of the Standard / Flex / Batch meters for our models.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from common import RESULTS_DIR, outputs

METRICS = {
    "AzureOpenAIRequests": ["ModelDeploymentName", "ServiceTierRequest", "ServiceTierResponse", "StatusCode"],
    "ProcessedPromptTokens": ["ModelDeploymentName", "ServiceTierRequest", "ServiceTierResponse"],
    "GeneratedTokens": ["ModelDeploymentName", "ServiceTierRequest", "ServiceTierResponse"],
}
RETAIL_REGION = "swedencentral"
RETAIL_PATTERNS = ["5.6 sol ShortCo", "5.4 mini", "54 mini"]


def az(*args: str) -> str:
    exe = shutil.which("az") or shutil.which("az.cmd")
    return subprocess.run([exe, *args], check=True, capture_output=True, text=True).stdout


def collect_metrics(resource_id: str, start: datetime, end: datetime) -> list[dict]:
    rows: list[dict] = []
    for metric, dims in METRICS.items():
        flt = " and ".join(f"{d} eq '*'" for d in dims)
        raw = json.loads(az("monitor", "metrics", "list", "--resource", resource_id, "--metric", metric,
                            "--aggregation", "Total", "--interval", "PT1H",
                            "--start-time", start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "--end-time", end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "--filter", flt, "-o", "json"))
        for v in raw.get("value", []):
            for ts in v.get("timeseries", []):
                md = {m["name"]["value"].lower(): m["value"] for m in ts.get("metadatavalues", [])}
                total = sum(p.get("total") or 0 for p in ts.get("data", []))
                if total:
                    rows.append({
                        "metric": metric,
                        "deployment": md.get("modeldeploymentname"),
                        "tier_request": md.get("servicetierrequest"),
                        "tier_response": md.get("servicetierresponse"),
                        "status": md.get("statuscode"),
                        "total": total,
                    })
    return rows


def collect_cost(scope: str, start: datetime, end: datetime) -> dict:
    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": start.strftime("%Y-%m-%dT00:00:00Z"), "to": end.strftime("%Y-%m-%dT23:59:59Z")},
        "dataset": {
            "granularity": "None",
            "aggregation": {"cost": {"name": "Cost", "function": "Sum"},
                            "qty": {"name": "UsageQuantity", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "MeterCategory"},
                         {"type": "Dimension", "name": "MeterSubCategory"},
                         {"type": "Dimension", "name": "Meter"}],
        },
    }
    tmp = RESULTS_DIR / ".cost_query.json"
    tmp.write_text(json.dumps(body), encoding="utf-8")
    try:
        raw = json.loads(az("rest", "--method", "post", "--url",
                            f"https://management.azure.com{scope}/providers/Microsoft.CostManagement/query?api-version=2023-11-01",
                            "--body", f"@{tmp}", "-o", "json"))
    except subprocess.CalledProcessError as e:
        return {"error": (e.stderr or str(e))[-500:], "rows": []}
    finally:
        tmp.unlink(missing_ok=True)
    cols = [c["name"] for c in raw["properties"]["columns"]]
    rows = [dict(zip(cols, r)) for r in raw["properties"]["rows"]]
    rows.sort(key=lambda r: -(r.get("Cost") or 0))
    return {"rows": rows}


def retail_prices() -> list[dict]:
    items: list[dict] = []
    flt = (f"armRegionName eq '{RETAIL_REGION}' and serviceName eq 'Foundry Models' "
           "and contains(productName,'GPT5')")
    url = "https://prices.azure.com/api/retail/prices?$filter=" + urllib.parse.quote(flt)
    while url:
        with urllib.request.urlopen(url, timeout=60) as r:
            page = json.load(r)
        items += page.get("Items", [])
        url = page.get("NextPageLink")
    keep = []
    for i in items:
        name = i["meterName"].replace(" 1M Tokens", "")
        if not any(p in name for p in RETAIL_PATTERNS) or not name.endswith("Gl"):
            continue
        low = name.lower()
        if (" pp " in low and "5.6 sol" not in low) or "longco" in low or " wr " in low:
            continue
        keep.append({"meter": name, "price_per_1m": i["retailPrice"], "currency": i["currencyCode"],
                     "meterId": i["meterId"], "effective": i["effectiveStartDate"][:10]})
    keep.sort(key=lambda r: r["meter"])
    return keep


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="2026-09-28", help="UTC date the experiment started (YYYY-MM-DD)")
    args = ap.parse_args()

    o = outputs()
    rg = o["resourceGroup"]
    sub = az("account", "show", "--query", "id", "-o", "tsv").strip()
    foundry_id = az("cognitiveservices", "account", "show", "-g", rg, "-n", o["foundryName"], "--query", "id", "-o", "tsv").strip()
    start = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    out: dict = {"queried_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "window": [start.isoformat(), end.isoformat()]}
    print("metrics ...")
    out["metrics"] = collect_metrics(foundry_id, start, end)
    print("cost management ...")
    out["cost"] = collect_cost(f"/subscriptions/{sub}/resourceGroups/{rg}", start, end)
    print("retail prices ...")
    out["retail"] = retail_prices()

    path = RESULTS_DIR / "billing_evidence.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    agg: dict = defaultdict(float)
    for r in out["metrics"]:
        agg[(r["metric"], r["deployment"], r["tier_request"], r["tier_response"])] += r["total"]
    for k, v in sorted(agg.items()):
        print(f"{k[0]:<22} {k[1]:<18} req={k[2]:<8} resp={k[3]:<8} {v:>10.0f}")
    print(f"cost rows: {len(out['cost'].get('rows', []))}  retail meters: {len(out['retail'])}")
    print(f"written {path}")


if __name__ == "__main__":
    main()
