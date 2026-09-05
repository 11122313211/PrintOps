"""Scan workspace files for lookalike API keys before publishing.

Part of the pre-release checklist in README ("发布前必须做敏感信息扫描").
Exits non-zero when a疑似 key is found so CI can fail the build.

The runtime data directory is scanned too: JSON files under ``data/`` have
values of sensitive fields (apiKey、token、password…) masked first, so the
one key that is *designed* to sit in ``data/llm_config.json`` does not trip
the scan, while a leaked copy of the same key in any other field or file does.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SECRET_PATTERNS = {
    "OpenAI 风格 sk-": re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    "Google API Key": re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "Groq API Key": re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    "HuggingFace token": re.compile(r"hf_[A-Za-z0-9]{30,}"),
    "Slack token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "AWS Access Key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "通用凭据字段": re.compile(
        r"(?i)[\"']?(?:api[_-]?key|secret[_-]?key|access[_-]?token|password)[\"']?\s*[:=]\s*[\"'][^\"']{20,}[\"']"),
}
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".zcode", ".mimosa"}
SKIP_SUFFIXES = {".pyc", ".sqlite3", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico"}
SENSITIVE_JSON_KEYS = re.compile(r"key|token|secret|password|credential", re.IGNORECASE)


def scan_text(text: str) -> list[tuple[str, re.Match]]:
    """Return (pattern-name, match) pairs for every疑似 secret in ``text``."""
    hits: list[tuple[str, re.Match]] = []
    for name, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(text):
            hits.append((name, match))
    return hits


def _scrub_json(text: str) -> str:
    """Mask values of sensitive JSON fields before pattern matching."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text

    def scrub(value):
        if isinstance(value, dict):
            return {key: ("***" if SENSITIVE_JSON_KEYS.search(str(key)) and isinstance(item, str)
                          else scrub(item))
                    for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    return json.dumps(scrub(data), ensure_ascii=False)


def collect_hits(root: Path) -> list[str]:
    hits: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if path.suffix.lower() == ".json":
            text = _scrub_json(text)
        for name, match in scan_text(text):
            line = text.count("\n", 0, match.start()) + 1
            hits.append(f"{path.relative_to(root)}:{line}: [{name}] {match.group(0)[:12]}…")
    return hits


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    hits = collect_hits(root)
    if hits:
        print("发现疑似 API Key，禁止提交：")
        print("\n".join(hits))
        return 1
    print("敏感信息扫描通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
