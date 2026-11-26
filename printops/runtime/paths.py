"""Canonical repository and resource paths for PrintOps (stdlib only)."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = REPO_ROOT / "web"
DATA_DIR = REPO_ROOT / "data"
RATE_CARDS_DIR = REPO_ROOT / "rate_cards"
DEFAULT_RATE_CARD = RATE_CARDS_DIR / "cn-local-reference.json"
VERSION_PATH = REPO_ROOT / "VERSION"
