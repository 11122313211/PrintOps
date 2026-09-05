"""Scan workspace files for lookalike API keys before publishing.

Part of the pre-release checklist in README ("发布前必须做敏感信息扫描").
Exits non-zero when a疑似 key is found so CI can fail the build.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{12,}")
SKIP_DIRS = {".git", "data", "__pycache__", ".venv", "venv", "node_modules", ".zcode"}
SKIP_SUFFIXES = {".pyc", ".sqlite3", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico"}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
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
        for match in PATTERN.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            hits.append(f"{path.relative_to(root)}:{line}: {match.group(0)[:10]}…")
    if hits:
        print("发现疑似 API Key，禁止提交：")
        print("\n".join(hits))
        return 1
    print("敏感信息扫描通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
