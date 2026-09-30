"""Batch worker – a fundamentally different application flow.

Batch is not a request/response API. The worker must:
  1. build a JSONL file (one line per request, the deployment name inside every line),
  2. upload it through the Files API,
  3. create a batch job,
  4. persist job state somewhere durable,
  5. come back later (here: a scheduled Service Bus message) and poll the job,
  6. download the output file and correlate lines by custom_id.
"""
from __future__ import annotations

import io
import json
import time
from datetime import datetime, timedelta, timezone

from azure.servicebus import ServiceBusMessage

from .common import env, iso, log, openai_client, read_json, utcnow, worker_info, write_json

POLL_INTERVAL_S = 60
TERMINAL = {"completed", "failed", "expired", "cancelled"}
BATCH_ENDPOINT = "/v1/chat/completions"


def _state_blob(run_id: str) -> str:
    return f"{run_id}/batch-job.json"


def _ts(epoch: int | None) -> str | None:
    return iso(datetime.fromtimestamp(epoch, tz=timezone.utc)) if epoch else None


def _schedule_poll(sender, run_id: str, batch_id: str, poll: int) -> None:
    msg = ServiceBusMessage(json.dumps({"run_id": run_id, "batch_id": batch_id, "poll": poll}),
                            content_type="application/json")
    sender.schedule_messages(msg, utcnow() + timedelta(seconds=POLL_INTERVAL_S))


def _find_batch(client, run_id: str):
    """Batch create is not idempotent: a gateway timeout (504) can still leave a created batch behind."""
    for b in client.batches.list(limit=100).data:
        if (b.metadata or {}).get("run_id") == run_id:
            return b
    return None


def submit(job: dict, timing: dict, status_sender) -> None:
    client = openai_client()
    existing = _find_batch(client, job["run_id"])
    if existing:
        log.info("run=%s batch %s already exists (redelivery), adopting it", job["run_id"], existing.id)
        _submit_state(job, timing, status_sender, existing, existing.input_file_id, None, utcnow(), utcnow(),
                      "adopted on redelivery")
        return
    deployment = env("BATCH_PAIR_BATCH_DEPLOYMENT")
    params = job.get("params", {})
    lines = [
        json.dumps({
            "custom_id": p["id"],
            "method": "POST",
            "url": BATCH_ENDPOINT,
            "body": {
                "model": deployment,
                "messages": [{"role": "user", "content": p["text"]}],
                "reasoning_effort": params.get("reasoning_effort", "low"),
                "max_completion_tokens": params.get("max_completion_tokens", 2000),
            },
        })
        for p in job["prompts"]
    ]
    t_upload = utcnow()
    file = client.files.create(file=(f"{job['run_id']}.jsonl", io.BytesIO("\n".join(lines).encode())), purpose="batch")
    # Azure validates uploaded files asynchronously; the batch can only be created once the file is processed.
    for _ in range(60):
        file = client.files.retrieve(file.id)
        if file.status in ("processed", "error"):
            break
        time.sleep(2)
    t_file_ready = utcnow()
    note = None
    try:
        batch = client.batches.create(
            input_file_id=file.id,
            endpoint=BATCH_ENDPOINT,
            completion_window="24h",
            metadata={"run_id": job["run_id"]},
        )
    except Exception as e:  # noqa: BLE001
        log.warning("run=%s batch create failed (%s), checking whether it was created anyway", job["run_id"], e)
        batch = None
        for _ in range(10):
            time.sleep(15)
            batch = _find_batch(client, job["run_id"])
            if batch:
                break
        if not batch:
            raise
        note = f"create returned {type(e).__name__}, batch found afterwards"
    _submit_state(job, timing, status_sender, batch, file.id, file.status, t_upload, t_file_ready, note)


def _submit_state(job, timing, status_sender, batch, file_id, file_status, t_upload, t_file_ready, note) -> None:
    submitted_at = utcnow()
    log.info("run=%s submitted batch %s (file %s status %s) %s", job["run_id"], batch.id, file_id, file_status, note or "")
    state = {
        "run_id": job["run_id"],
        "deployment": env("BATCH_PAIR_BATCH_DEPLOYMENT"),
        "batch_id": batch.id,
        "input_file_id": file_id,
        "input_file_status": file_status,
        "submit_note": note,
        "prompts": [p["id"] for p in job["prompts"]],
        "message": timing,
        "worker_submit": worker_info(),
        "upload_started_at": iso(t_upload),
        "file_processed_at": iso(t_file_ready),
        "submitted_at": iso(submitted_at),
        "timeline": [{"observed_at": iso(submitted_at), "status": batch.status}],
        "polls": 0,
        "done": False,
    }
    write_json(_state_blob(job["run_id"]), state)
    _schedule_poll(status_sender, job["run_id"], batch.id, 1)


def poll(msg_body: dict, status_sender) -> None:
    client = openai_client()
    run_id, batch_id = msg_body["run_id"], msg_body["batch_id"]
    state = read_json(_state_blob(run_id))
    if state.get("done"):
        return
    batch = client.batches.retrieve(batch_id)
    observed_at = utcnow()
    state["polls"] = msg_body.get("poll", 0)
    counts = batch.request_counts.model_dump() if batch.request_counts else None
    if not state["timeline"] or state["timeline"][-1]["status"] != batch.status:
        state["timeline"].append({"observed_at": iso(observed_at), "status": batch.status, "counts": counts})
    state["batch"] = {
        "status": batch.status,
        "created_at": _ts(batch.created_at),
        "in_progress_at": _ts(batch.in_progress_at),
        "finalizing_at": _ts(batch.finalizing_at),
        "completed_at": _ts(batch.completed_at),
        "failed_at": _ts(batch.failed_at),
        "expired_at": _ts(batch.expired_at),
        "request_counts": counts,
        "errors": batch.errors.model_dump() if batch.errors else None,
        "output_file_id": batch.output_file_id,
        "error_file_id": batch.error_file_id,
    }
    log.info("run=%s batch=%s poll=%s status=%s counts=%s", run_id, batch_id, state["polls"], batch.status, counts)

    if batch.status not in TERMINAL:
        write_json(_state_blob(run_id), state)
        _schedule_poll(status_sender, run_id, batch_id, state["polls"] + 1)
        return

    state["done"] = True
    state["done_observed_at"] = iso(observed_at)
    state["worker_poll"] = worker_info()
    submitted = datetime.fromisoformat(state["submitted_at"])
    visible = state["message"].get("scheduled_enqueue_at") or state["message"].get("enqueued_at")
    service_latency = (batch.completed_at - batch.created_at) if batch.completed_at and batch.created_at else None

    lines = []
    for fid in (batch.output_file_id, batch.error_file_id):
        if fid:
            content = client.files.content(fid).read().decode("utf-8")
            lines += [json.loads(line) for line in content.splitlines() if line.strip()]
    by_id = {line["custom_id"]: line for line in lines}

    records = []
    for pid in state["prompts"]:
        line = by_id.get(pid)
        resp = (line or {}).get("response") or {}
        body = resp.get("body") or {}
        ok = batch.status == "completed" and resp.get("status_code") == 200
        usage = body.get("usage") or {}
        choice = (body.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        records.append({
            "run_id": run_id,
            "mode": "batch",
            "pair": "B",
            "deployment": state["deployment"],
            "prompt_id": pid,
            "success": ok,
            "batch_id": batch_id,
            "batch_status": batch.status,
            # Service-side job duration (created -> completed) – the Batch equivalent of request latency.
            "latency_s": service_latency if ok else None,
            # What the application experienced: submission -> our poller noticed completion.
            "total_s": (observed_at - submitted).total_seconds(),
            # From the moment the test message became visible on the bus.
            "end_to_end_s": (observed_at - datetime.fromisoformat(visible)).total_seconds() if visible else None,
            "service_tier": body.get("service_tier"),
            "model": body.get("model"),
            "finish_reason": choice.get("finish_reason"),
            "output_chars": len(content),
            "output_preview": content[:200],
            "status_code": resp.get("status_code"),
            "error": (line or {}).get("error") or (body.get("error") if not ok else None),
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
            },
        })
    write_json(_state_blob(run_id), state)
    write_json(f"{run_id}/batch-B.json", {
        "run_id": run_id,
        "mode": "batch",
        "pair": "B",
        "deployment": state["deployment"],
        "message": state["message"],
        "worker": worker_info(),
        "completed_at": iso(utcnow()),
        "batch": state["batch"],
        "timeline": state["timeline"],
        "polls": state["polls"],
        "records": records,
    })
