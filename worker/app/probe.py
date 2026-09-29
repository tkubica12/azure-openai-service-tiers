"""Probe mode – runs as a scheduled Container Apps Job (cron, every 15 minutes).

Sends ONE small test run (a single prompt) to the Service Bus topic, so the Standard, Priority, Flex and Batch
workers are exercised around the clock (day/night, several days) without the laptop being online.
Run ids are derived from the UTC slot time: probe-YYYYMMDD-HHMM.
"""
from __future__ import annotations

import json
import os

from azure.servicebus import ServiceBusClient, ServiceBusMessage

from .common import credential, env, iso, log, utcnow
from .prompts import PROMPTS

SLOT_MINUTES = 15


def run() -> None:
    now = utcnow()
    slot = now.replace(minute=now.minute - now.minute % SLOT_MINUTES, second=0, microsecond=0)
    slot_index = int(slot.timestamp()) // (SLOT_MINUTES * 60)
    pid, text = PROMPTS[slot_index % len(PROMPTS)]  # deterministic rotation through all prompts
    campaign = os.environ.get("PROBE_CAMPAIGN", "probe")
    run_id = f"{campaign}-{slot:%Y%m%d-%H%M}"
    body = {
        "run_id": run_id,
        "campaign": campaign,
        "created_at": iso(now),
        "scheduled_for": iso(now),
        "prompts": [{"id": f"{run_id}-{pid}", "text": text}],
        "params": {"reasoning_effort": "low", "max_completion_tokens": 1000},
    }
    with ServiceBusClient(env("SERVICEBUS_FQDN"), credential=credential()) as sb, \
            sb.get_topic_sender(env("TOPIC_NAME")) as sender:
        sender.send_messages(ServiceBusMessage(json.dumps(body), content_type="application/json",
                                               message_id=run_id, subject="llm-test"))
    log.info("probe sent %s prompt=%s", run_id, pid)
