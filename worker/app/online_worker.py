"""Standard, Priority and Flex workers – the same code, only the service tier differs.

WORKER_MODE=standard -> service_tier="default", runs both pairs:
    pair A = gpt-5.6-sol (compared with Priority and Flex), pair B = gpt-5.4-mini (compared with Batch)
WORKER_MODE=priority -> service_tier="priority", runs pair A only
WORKER_MODE=flex     -> service_tier="flex", runs pair A only (Flex supports gpt-5.6-sol)
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .common import blob_exists, env, log, openai_client, utcnow, iso, worker_info, write_json
from .llm import complete
from .prompts import heavy_prompt

SERVICE_TIER = {"standard": "default", "priority": "priority", "flex": "flex"}


def targets(mode: str, job: dict | None = None) -> list[tuple[str, str]]:
    if mode == "standard":
        pairs = [("A", env("FLEX_PAIR_DEPLOYMENT")), ("B", env("BATCH_PAIR_DEPLOYMENT"))]
    else:
        pairs = [("A", env("FLEX_PAIR_DEPLOYMENT"))]
    wanted = ((job or {}).get("params") or {}).get("pairs")
    return [p for p in pairs if not wanted or p[0] in wanted]


def run_pair(mode: str, pair: str, deployment: str, job: dict, timing: dict) -> None:
    blob_name = f"{job['run_id']}/{mode}-{pair}.json"
    if blob_exists(blob_name):
        # A re-sent message for the same run would repeat the same nonce and be served from the prompt cache.
        log.warning("run=%s mode=%s pair=%s already has a result – duplicate message skipped", job["run_id"], mode, pair)
        return
    client = openai_client()
    tier = SERVICE_TIER[mode]
    params = job.get("params", {})
    records = []
    for prompt in job["prompts"]:
        if "heavy" in prompt:
            # Built here, not in the message: ~200 KB of text would exceed the Service Bus message size limit.
            # Tier-specific nonce at the start of the prompt => no prompt-cache sharing between tiers.
            spec = prompt["heavy"]
            text = heavy_prompt(f"{prompt['id']}-{mode}", spec["tickets"], spec["answers"])
        else:
            text = prompt["text"]
        result = complete(
            client,
            deployment,
            text,
            tier,
            reasoning_effort=params.get("reasoning_effort", "low"),
            max_completion_tokens=params.get("max_completion_tokens", 2000),
        )
        log.info("run=%s mode=%s pair=%s prompt=%s success=%s latency=%s tier=%s", job["run_id"], mode, pair,
                 prompt["id"], result["success"], result.get("latency_s"), result.get("service_tier"))
        records.append({"run_id": job["run_id"], "mode": mode, "pair": pair, "deployment": deployment,
                        "prompt_id": prompt["id"], **result})
    write_json(blob_name, {
        "run_id": job["run_id"],
        "mode": mode,
        "pair": pair,
        "deployment": deployment,
        "service_tier_requested": tier,
        "message": timing,
        "worker": worker_info(),
        "completed_at": iso(utcnow()),
        "records": records,
    })


def handle(mode: str, job: dict, timing: dict) -> None:
    # Pairs run concurrently so that Standard and Flex hit the model at the same time of day.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_pair, mode, pair, dep, job, timing) for pair, dep in targets(mode, job)]
        for f in futures:
            f.result()
