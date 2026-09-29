"""Helpers for laptop-side scripts: read deployment outputs and build Entra-authenticated clients."""
from __future__ import annotations

import json
from pathlib import Path

from azure.identity import AzureCliCredential

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "results"


def outputs() -> dict:
    path = ROOT / "outputs.json"
    if not path.exists():
        raise SystemExit("outputs.json not found - run infra/deploy.ps1 first")
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(raw, list):
        return {o["name"]: o["value"] for o in raw}
    return {k: (v["value"] if isinstance(v, dict) and "value" in v else v) for k, v in raw.items()}


def credential() -> AzureCliCredential:
    return AzureCliCredential(process_timeout=60)
