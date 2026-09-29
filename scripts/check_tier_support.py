"""Request every service tier (flex, priority, default) on every online deployment and record which tier served it.

Writes results/tier_support.json. Shows e.g. that requesting Flex where it is not supported does not necessarily fail –
the request can be served (and billed) as Standard, visible only in the response's service_tier.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from azure.identity import get_bearer_token_provider
from openai import OpenAI

from common import RESULTS_DIR, credential, outputs


def main() -> None:
    o = outputs()
    tp = get_bearer_token_provider(credential(), "https://cognitiveservices.azure.com/.default")
    client = OpenAI(base_url=o["openAiBaseUrl"], api_key=tp, timeout=900)
    rows = []
    for dep in (o["flexPairDeployment"], o["batchPairDeployment"]):
        for tier in ("default", "priority", "flex"):
            t = time.perf_counter()
            try:
                raw = client.chat.completions.with_raw_response.create(
                    model=dep, messages=[{"role": "user", "content": "Reply with the word OK."}],
                    service_tier=tier, max_completion_tokens=200)
                r = raw.parse()
                rows.append({"deployment": dep, "model": r.model, "requested": tier, "served": r.service_tier,
                             "status": raw.status_code, "latency_s": round(time.perf_counter() - t, 3),
                             "usage": {"prompt_tokens": r.usage.prompt_tokens,
                                       "completion_tokens": r.usage.completion_tokens}})
            except Exception as e:  # noqa: BLE001 - record any failure as evidence
                rows.append({"deployment": dep, "requested": tier, "status": getattr(e, "status_code", None),
                             "error": str(e)[:300], "latency_s": round(time.perf_counter() - t, 3)})
            print(rows[-1])
    out = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "results": rows}
    (RESULTS_DIR / "tier_support.json").write_text(json.dumps(out, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
