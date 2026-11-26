"""Force UTF-8 on the standard streams of command-line entry points.

MCP stdio is UTF-8 by specification, and every JSON document this project
prints uses ``ensure_ascii=False``.  On Windows consoles with a legacy code
page (cp936, cp1252, ...) a plain ``print`` would corrupt or crash on
characters such as ``²`` or ``≥``.  Entry points call
``configure()`` once before writing anything; replaced or reattached
streams (tests, embedding hosts) are left untouched.
"""
from __future__ import annotations

import sys


def configure(*, stdin: bool = False) -> None:
    streams = (sys.stdin, sys.stdout, sys.stderr) if stdin else (sys.stdout, sys.stderr)
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass
