"""Entry point: long-running Service Bus consumer. KEDA scales the app 0 -> 1 when messages arrive
and back to 0 after the cooldown period, so no compute runs between test runs."""
from __future__ import annotations

import json
import threading
import time

from azure.servicebus import AutoLockRenewer, ServiceBusClient

from . import batch_worker, online_worker, probe
from .common import credential, env, log, message_timing, utcnow


def new_client() -> ServiceBusClient:
    # Service Bus clients are not thread-safe - every consumer thread gets its own.
    return ServiceBusClient(env("SERVICEBUS_FQDN"), credential=credential())


def consume(receiver_factory, handler, name: str) -> None:
    renewer = AutoLockRenewer(max_lock_renewal_duration=3600)
    while True:
        try:
            with new_client() as client, receiver_factory(client) as receiver:
                while True:
                    for msg in receiver.receive_messages(max_message_count=1, max_wait_time=30):
                        received_at = utcnow()
                        renewer.register(receiver, msg)
                        body = json.loads(str(msg))
                        log.info("[%s] received %s (delivery %d)", name, body.get("run_id"), msg.delivery_count)
                        try:
                            handler(body, message_timing(msg, received_at))
                            receiver.complete_message(msg)
                        except Exception:
                            log.exception("[%s] handler failed, abandoning message", name)
                            receiver.abandon_message(msg)
        except Exception:
            log.exception("[%s] receiver loop error, reconnecting", name)
            time.sleep(5)


def main() -> None:
    mode = env("WORKER_MODE")
    log.info("worker starting in mode=%s", mode)
    if mode == "probe":
        probe.run()
        return
    topic, sub = env("TOPIC_NAME"), env("SUBSCRIPTION_NAME")

    def topic_receiver(client: ServiceBusClient):
        return client.get_subscription_receiver(topic, sub)

    if mode in ("standard", "priority", "flex"):
        consume(topic_receiver, lambda body, timing: online_worker.handle(mode, body, timing), mode)
        return

    if mode != "batch":
        raise SystemExit(f"unknown WORKER_MODE {mode}")

    status_queue = env("BATCH_STATUS_QUEUE")
    sender_client = new_client()
    status_sender = sender_client.get_queue_sender(status_queue)
    lock = threading.Lock()  # serialises the shared sender and the job-state blobs

    def on_test(body, timing):
        with lock:
            batch_worker.submit(body, timing, status_sender)

    def on_status(body, timing):
        with lock:
            batch_worker.poll(body, status_sender)

    threads = [
        threading.Thread(target=consume, args=(topic_receiver, on_test, "batch-submit"), daemon=True),
        threading.Thread(target=consume, args=(lambda c: c.get_queue_receiver(status_queue), on_status,
                                               "batch-poll"), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
