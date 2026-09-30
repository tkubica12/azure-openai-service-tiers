"""Probe mode – runs as a scheduled Container Apps Job (cron).

Sends ONE test run (a single prompt) to the Service Bus topic, so the workers are exercised around the clock
(day/night, several days) without the laptop being online. Run ids are derived from the UTC slot time:
<campaign>-YYYYMMDD-HHMM. Campaign "heavy50k" (every 30 minutes): ~50k input / ~5k output tokens, streaming,
Standard/Priority/Flex only.
"""
from __future__ import annotations

import json
import os

from azure.servicebus import ServiceBusClient, ServiceBusMessage

from .common import credential, env, iso, log, utcnow
from .prompts import HEAVY_ANSWERS, HEAVY_TICKETS, PROMPTS

SLOT_MINUTES = int(os.environ.get("PROBE_SLOT_MINUTES", "15"))


def run() -> None:
    now = utcnow()
    slot = now.replace(minute=now.minute - now.minute % SLOT_MINUTES, second=0, microsecond=0)
    slot_index = int(slot.timestamp()) // (SLOT_MINUTES * 60)
    campaign = os.environ.get("PROBE_CAMPAIGN", "probe")
    run_id = f"{campaign}-{slot:%Y%m%d-%H%M}"
    if campaign.startswith("heavy"):
        # Large fixed context + long fixed-shape answer, Standard/Priority/Flex only (pair A, no Batch).
        # Only the prompt spec travels in the message; each worker builds the ~50k-token prompt itself.
        prompt = {"id": f"{run_id}-triage", "heavy": {"tickets": HEAVY_TICKETS, "answers": HEAVY_ANSWERS}}
        params = {"reasoning_effort": os.environ.get("PROBE_REASONING_EFFORT", "none"),
                  "max_completion_tokens": 8000, "pairs": ["A"], "skip_batch": True}
    else:
        pid, text = PROMPTS[slot_index % len(PROMPTS)]  # deterministic rotation through all prompts
        prompt = {"id": f"{run_id}-{pid}", "text": text}
        params = {"reasoning_effort": "low", "max_completion_tokens": 1000}
    body = {
        "run_id": run_id,
        "campaign": campaign,
        "created_at": iso(now),
        "scheduled_for": iso(now),
        "prompts": [prompt],
        "params": params,
    }
    with ServiceBusClient(env("SERVICEBUS_FQDN"), credential=credential()) as sb, \
            sb.get_topic_sender(env("TOPIC_NAME")) as sender:
        sender.send_messages(ServiceBusMessage(json.dumps(body), content_type="application/json",
                                               message_id=run_id, subject="llm-test"))
    log.info("probe sent %s prompt=%s", run_id, prompt["id"])
