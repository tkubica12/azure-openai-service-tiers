"""Shared plumbing: configuration, Entra ID credentials, Service Bus and Blob helpers.

No keys or connection strings anywhere – everything authenticates with the
user-assigned managed identity (in Azure) or the Azure CLI login (on a laptop).
"""
from __future__ import annotations

import json
import logging
import os
import socket
from datetime import datetime, timezone

from azure.identity import AzureCliCredential, ManagedIdentityCredential, get_bearer_token_provider
from azure.storage.blob import BlobServiceClient, ContentSettings
from openai import OpenAI

PROCESS_STARTED_AT = datetime.now(timezone.utc)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
for noisy in ("azure", "uamqp", "httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("worker")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Missing environment variable {name}")
    return value


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


_credential = None


def credential():
    global _credential
    if _credential is None:
        client_id = os.environ.get("AZURE_CLIENT_ID")
        _credential = ManagedIdentityCredential(client_id=client_id) if client_id else AzureCliCredential()
    return _credential


def openai_client() -> OpenAI:
    """The one and only OpenAI client used by every worker (standard, flex and batch).

    A generous timeout is required for Flex (requests may wait for capacity);
    SDK retries are disabled so that every attempt is measured explicitly.
    """
    token_provider = get_bearer_token_provider(credential(), "https://cognitiveservices.azure.com/.default")
    token_provider()  # warm the token cache so measured latencies never include Entra token acquisition
    return OpenAI(
        base_url=env("OPENAI_BASE_URL"),
        api_key=token_provider,
        timeout=900.0,
        max_retries=0,
    )


_blob_service = None


def blob_container():
    global _blob_service
    if _blob_service is None:
        _blob_service = BlobServiceClient(env("STORAGE_ACCOUNT_URL"), credential=credential())
    return _blob_service.get_container_client(env("RESULTS_CONTAINER", "results"))


def write_json(blob_name: str, payload) -> None:
    data = json.dumps(payload, indent=2, default=str).encode("utf-8")
    blob_container().upload_blob(
        blob_name, data, overwrite=True, content_settings=ContentSettings(content_type="application/json")
    )
    log.info("wrote blob %s (%d bytes)", blob_name, len(data))


def read_json(blob_name: str):
    return json.loads(blob_container().download_blob(blob_name).readall())


def blob_exists(blob_name: str) -> bool:
    return blob_container().get_blob_client(blob_name).exists()


def _resolve(url_var: str) -> dict:
    """Evidence of private networking: which IP do the Storage / Foundry hostnames resolve to?"""
    from urllib.parse import urlparse

    host = urlparse(os.environ.get(url_var, "")).hostname
    if not host:
        return {}
    try:
        return {"host": host, "ips": sorted({a[4][0] for a in socket.getaddrinfo(host, 443)})}
    except OSError as e:
        return {"host": host, "error": str(e)}


def worker_info() -> dict:
    return {
        "mode": os.environ.get("WORKER_MODE"),
        "replica": os.environ.get("CONTAINER_APP_REPLICA_NAME", socket.gethostname()),
        "revision": os.environ.get("CONTAINER_APP_REVISION"),
        "process_started_at": iso(PROCESS_STARTED_AT),
        "dns": {"storage": _resolve("STORAGE_ACCOUNT_URL"), "openai": _resolve("OPENAI_BASE_URL")},
    }


def message_timing(msg, received_at: datetime) -> dict:
    """Queueing / scale-from-zero timing of a Service Bus message."""
    enqueued = msg.enqueued_time_utc
    scheduled = msg.scheduled_enqueue_time_utc
    visible_from = scheduled or enqueued
    if visible_from is not None and visible_from.tzinfo is None:
        visible_from = visible_from.replace(tzinfo=timezone.utc)
    return {
        "enqueued_at": iso(enqueued),
        "scheduled_enqueue_at": iso(scheduled),
        "received_at": iso(received_at),
        "delivery_count": msg.delivery_count,
        "pickup_delay_s": (received_at - visible_from).total_seconds() if visible_from else None,
        "cold_start": PROCESS_STARTED_AT > visible_from if visible_from else None,
    }
