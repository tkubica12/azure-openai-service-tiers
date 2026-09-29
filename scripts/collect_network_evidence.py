"""Pull Storage access-log evidence (caller IP, auth type) from Log Analytics into results/network_evidence.json.

Shows that Container Apps workers reach Storage from private VNet addresses (10.60.x.x) via the private
endpoint and authenticate with OAuth (Entra ID), never with account keys / SAS.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone

from common import RESULTS_DIR, outputs

QUERY = """
StorageBlobLogs
| where TimeGenerated > ago({days}d)
| extend ip = tostring(split(CallerIpAddress, ':')[0])
| extend source = case(AuthenticationType == 'TrustedAccess', 'Azure control plane (Bicep deployment, trusted service)',
                       ip startswith '10.60.', 'Container Apps (VNet, private endpoint)',
                       'Operator laptop (public, IP firewall)')
| summarize requests = count() by source, CallerIpAddress = ip, AuthenticationType, StatusText
| order by source asc, requests desc
"""


def az(*args: str) -> str:
    exe = shutil.which("az") or shutil.which("az.cmd")
    return subprocess.run([exe, *args], check=True, capture_output=True, text=True).stdout


def main(days: int = 14) -> None:
    rg = outputs()["resourceGroup"]
    ws = az("monitor", "log-analytics", "workspace", "list", "-g", rg, "--query", "[0].customerId", "-o", "tsv").strip()
    rows = json.loads(az("monitor", "log-analytics", "query", "-w", ws, "--analytics-query",
                         " ".join(QUERY.format(days=days).split()), "-o", "json"))
    for r in rows:
        r.pop("TableName", None)
    out = {"queried_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "storage": rows}
    path = RESULTS_DIR / "network_evidence.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    for r in rows:
        print(f"{r['source']:<42} {r['CallerIpAddress']:<16} {r['AuthenticationType']:<8} {r['StatusText']:<28} {r['requests']}")
    print(f"written {path}")


if __name__ == "__main__":
    main()
