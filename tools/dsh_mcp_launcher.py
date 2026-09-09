#!/usr/bin/env python3
"""Launch the PrintOps stdio MCP server for one trusted dsh session.

The dsh MCP client starts a child process with an argv array.  It does not
expand shell variables in that array, so a session id must be supplied by the
trusted host before this launcher is invoked.  Keeping the launcher in this
repository also makes the server path and working directory independent of
dsh's current directory.

This file intentionally has no third-party dependencies and never accepts
``--allow-any-session``.  ``--dry-run`` is useful for inspecting the exact
argv that a dsh profile should use without starting a server.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER = REPO_ROOT / "mcp_server.py"
DEFAULT_MEMORY = REPO_ROOT / "data" / "agent.sqlite3"
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CAPABILITY_LEVELS = {"L0", "L1"}
SANITIZED_ENV = (
    "PRINTOPS_MCP_SESSION_ID",
    "PRINTOPS_MCP_CAPABILITIES",
    "PRINTOPS_MEMORY_PATH",
)


class LauncherError(ValueError):
    """A user-facing launcher configuration error."""


def _session_id(value: Any) -> str:
    if not isinstance(value, str) or not SESSION_ID_PATTERN.fullmatch(value):
        raise LauncherError(
            "session id must contain only ASCII letters, digits, '_' or '-' "
            "and be 1-64 characters"
        )
    return value


def _capabilities(value: Any) -> str:
    if not isinstance(value, str):
        raise LauncherError("capabilities must be L0 or L0,L1")
    levels = {part.strip().upper() for part in value.split(",") if part.strip()}
    if not levels:
        raise LauncherError("capabilities must be L0 or L0,L1")
    unknown = levels - CAPABILITY_LEVELS
    if unknown:
        raise LauncherError("unsupported capability level: " + ", ".join(sorted(unknown)))
    # L1 is an additive level in mcp_server.py; make the implicit L0 explicit
    # so the command is stable even if callers pass just ``L1``.
    levels.add("L0")
    return ",".join(level for level in ("L0", "L1") if level in levels)


def _memory_path(value: Any) -> Path:
    if value is None or value == "":
        return DEFAULT_MEMORY
    if not isinstance(value, str):
        raise LauncherError("memory path must be a string")
    candidate = Path(os.path.expanduser(value))
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    return candidate.resolve(strict=False)


def build_server_argv(session_id: str, memory_path: str | Path | None = None,
                      capabilities: str = "L0") -> list[str]:
    """Build the exact argv used to replace this process with mcp_server.py."""
    session = _session_id(session_id)
    memory = _memory_path(memory_path)
    caps = _capabilities(capabilities)
    if not MCP_SERVER.is_file():
        raise LauncherError("mcp_server.py is missing from the repository root")
    interpreter = Path(sys.executable).resolve(strict=False)
    if not interpreter.is_file():
        raise LauncherError("the current Python interpreter is unavailable")
    return [
        str(interpreter),
        str(MCP_SERVER),
        "--session-id",
        session,
        "--memory-path",
        str(memory),
        "--capabilities",
        caps,
    ]


def _clean_environment() -> dict[str, str]:
    """Remove environment overrides that could disagree with explicit argv."""
    environment = dict(os.environ)
    for key in SANITIZED_ENV:
        environment.pop(key, None)
    return environment


def _dry_run_payload(argv: list[str]) -> dict[str, Any]:
    return {
        "cwd": str(REPO_ROOT),
        "argv": argv,
        "sessionBound": True,
        "allowAnySession": False,
        "environmentOverridesIgnored": list(SANITIZED_ENV),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch the session-bound PrintOps stdio MCP server")
    parser.add_argument("--session-id", required=True,
                        help="one already-authorized session id; shell placeholders are not expanded")
    parser.add_argument("--memory-path", default=None,
                        help="SQLite path; relative paths are resolved from the repository root")
    parser.add_argument("--capabilities", default="L0",
                        help="capability levels: L0 or L0,L1 (default: L0)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the resolved launch contract and do not start the server")
    args = parser.parse_args(argv)
    try:
        server_argv = build_server_argv(args.session_id, args.memory_path, args.capabilities)
    except LauncherError as error:
        print(f"PrintOps dsh launcher: {error}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(json.dumps(_dry_run_payload(server_argv), ensure_ascii=False,
                         allow_nan=False, separators=(",", ":")))
        return 0

    try:
        # execve preserves the dsh stdio pipes while avoiding an intermediate
        # shell and makes all server output protocol JSON only.
        os.chdir(REPO_ROOT)
        os.execve(server_argv[0], server_argv, _clean_environment())
    except OSError as error:
        print(f"PrintOps dsh launcher: unable to start MCP server ({error})", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
