"""Send test runs to the Service Bus topic from the laptop (Entra ID auth via Azure CLI login).

Every run is ONE message on the topic `llm-tests`; the topic fans it out to the standard, flex and
batch subscriptions, so all three workers process exactly the same prompts. Runs are sent as
*scheduled* messages spaced in time, which lets the Container Apps scale back to zero between
runs – every run therefore also measures a realistic cold start.

Example:
    uv run python scripts/send_tests.py --campaign smoke --runs 1
    uv run python scripts/send_tests.py --campaign main --runs 12 --interval-min 20 --start-delay-min 2
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta, timezone

from azure.servicebus import ServiceBusClient, ServiceBusMessage

from common import RESULTS_DIR, credential, outputs
from prompts import PROMPTS


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign", required=True, help="name prefix of the run ids")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--interval-min", type=float, default=20.0, help="spacing between runs")
    ap.add_argument("--start-delay-min", type=float, default=0.0, help="delay before the first run")
    ap.add_argument("--prompts", type=int, default=5, help="prompts per run")
    ap.add_argument("--max-completion-tokens", type=int, default=1000)
    ap.add_argument("--reasoning-effort", default="low")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    out = outputs()
    rnd = random.Random(args.seed)
    now = datetime.now(timezone.utc)
    runs = []
    for i in range(args.runs):
        scheduled = now + timedelta(minutes=args.start_delay_min + i * args.interval_min)
        chosen = rnd.sample(PROMPTS, k=min(args.prompts, len(PROMPTS)))
        run_id = f"{args.campaign}-r{i + 1:02d}"
        runs.append({
            "run_id": run_id,
            "campaign": args.campaign,
            "created_at": now.isoformat(),
            "scheduled_for": scheduled.isoformat(),
            "prompts": [{"id": f"{run_id}-{pid}", "text": text} for pid, text in chosen],
            "params": {"reasoning_effort": args.reasoning_effort,
                       "max_completion_tokens": args.max_completion_tokens},
        })

    with ServiceBusClient(out["serviceBusFqdn"], credential=credential()) as sb, \
            sb.get_topic_sender(out["topicName"]) as sender:
        for run in runs:
            msg = ServiceBusMessage(json.dumps(run), content_type="application/json", message_id=run["run_id"],
                                    subject="llm-test")
            scheduled = datetime.fromisoformat(run["scheduled_for"])
            if scheduled <= datetime.now(timezone.utc) + timedelta(seconds=5):
                sender.send_messages(msg)
            else:
                sender.schedule_messages(msg, scheduled)
            print(f"{run['run_id']}: {len(run['prompts'])} prompts, visible at {run['scheduled_for']}")

    manifest_dir = RESULTS_DIR / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_dir / f"{args.campaign}.json"
    path.write_text(json.dumps({"campaign": args.campaign, "sent_at": now.isoformat(), "args": vars(args),
                                "runs": runs}, indent=2), encoding="utf-8")
    print(f"manifest written to {path}")


if __name__ == "__main__":
    main()
