"""Prompts are part of the worker package so that the in-Azure probe job and the laptop scripts use the same set."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))

from app.prompts import PROMPTS  # noqa: E402,F401
