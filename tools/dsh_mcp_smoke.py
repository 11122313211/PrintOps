#!/usr/bin/env python3
"""Run an offline JSON-RPC transcript against the dsh-facing MCP launcher.

This smoke does not require dsh, Node, npm, or network access.  It exercises
the same stdio boundary that ``@deepseek-ai/dsh-mcp-client`` uses: initialize,
the initialized notification, tools/list, and two read-only tool calls.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "tools" / "dsh_mcp_launcher.py"
PROTOCOL_VERSION = "2024-11-05"
MAX_LINE_BYTES = 128 * 1024


class SmokeError(RuntimeError):
    """A failed offline MCP transcript assertion."""


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _request(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def _decode_responses(stdout: str) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    for line_number, line in enumerate(stdout.splitlines(), 1):
        if not line.strip():
            continue
        if len(line.encode("utf-8", errors="replace")) > MAX_LINE_BYTES:
            raise SmokeError(f"stdout line {line_number} exceeds {MAX_LINE_BYTES} bytes")
        try:
            value = json.loads(line, parse_constant=_reject_constant)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise SmokeError(f"stdout line {line_number} is not strict JSON: {error}") from error
        if not isinstance(value, dict):
            raise SmokeError(f"stdout line {line_number} is not a JSON object")
        responses.append(value)
    return responses


def run_smoke(session_id: str = "dsh-smoke", memory_path: str | Path | None = None,
              capabilities: str = "L0", timeout_seconds: float = 10.0) -> dict[str, Any]:
    """Run and verify a bounded read-only transcript.

    ``memory_path`` is optional; an ephemeral SQLite file is used when it is
    omitted.  The returned summary is JSON-serializable for CI logs.
    """
    if not LAUNCHER.is_file():
        raise SmokeError("dsh_mcp_launcher.py is missing")
    if timeout_seconds <= 0 or timeout_seconds > 120:
        raise SmokeError("timeout_seconds must be between 0 and 120")

    requests = [
        _request(1, "initialize", {"protocolVersion": PROTOCOL_VERSION}),
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        _request(2, "tools/list", {}),
        _request(3, "tools/call", {
            "name": "explain_print_term",
            "arguments": {"sessionId": session_id, "question": "出血是什么"},
        }),
        _request(4, "tools/call", {
            "name": "validate_order",
            "arguments": {"sessionId": session_id},
        }),
    ]
    transcript = "\n".join(json.dumps(item, ensure_ascii=False, allow_nan=False,
                                        separators=(",", ":")) for item in requests) + "\n"

    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        if memory_path is None:
            temporary = tempfile.TemporaryDirectory(prefix="printops-dsh-smoke-")
            db_path = Path(temporary.name) / "agent.sqlite3"
        else:
            db_path = Path(memory_path)
            if not db_path.is_absolute():
                db_path = REPO_ROOT / db_path
            db_path = db_path.resolve(strict=False)

        command = [sys.executable, str(LAUNCHER), "--session-id", session_id,
                   "--memory-path", str(db_path), "--capabilities", capabilities]
        environment = dict(os.environ)
        # A conflicting inherited value must not override explicit launcher
        # argv.  This also models hosts that retain stale process settings.
        environment["PRINTOPS_MCP_SESSION_ID"] = "wrong-inherited-session"
        environment["PRINTOPS_MCP_CAPABILITIES"] = "L0,L1"
        with tempfile.TemporaryDirectory(prefix="printops-dsh-cwd-") as cwd:
            try:
                completed = subprocess.run(
                    command,
                    input=transcript,
                    text=True,
                    encoding="utf-8",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    env=environment,
                    timeout=timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise SmokeError("MCP transcript timed out") from error
            except OSError as error:
                raise SmokeError(f"unable to start MCP launcher: {error}") from error

        if completed.returncode != 0:
            detail = completed.stderr.strip()[:1024]
            raise SmokeError(f"MCP launcher exited {completed.returncode}: {detail}")
        responses = _decode_responses(completed.stdout)
        if len(responses) != 4:
            raise SmokeError(f"expected 4 JSON-RPC responses, got {len(responses)}")
        ids = [response.get("id") for response in responses]
        if ids != [1, 2, 3, 4]:
            raise SmokeError(f"unexpected response ids: {ids!r}")

        initialize = responses[0].get("result")
        if not isinstance(initialize, dict) or initialize.get("serverInfo", {}).get("name") != "printops-mcp":
            raise SmokeError("initialize response did not identify printops-mcp")

        listed = responses[1].get("result", {}).get("tools")
        if not isinstance(listed, list):
            raise SmokeError("tools/list did not return a tools array")
        tool_names = {item.get("name") for item in listed if isinstance(item, dict)}
        required = {"validate_order", "explain_print_term"}
        if not required.issubset(tool_names):
            raise SmokeError(f"tools/list is missing first-smoke tools: {sorted(required - tool_names)}")
        if capabilities.strip().upper() == "L0" and "prepare_handoff" in tool_names:
            raise SmokeError("L0 smoke unexpectedly exposed an L1 tool")

        explain = responses[2].get("result")
        structured = explain.get("structuredContent") if isinstance(explain, dict) else None
        if not isinstance(structured, dict) or structured.get("sessionId") != session_id:
            raise SmokeError("explain_print_term did not preserve the bound session")
        if explain.get("isError") is not False:
            raise SmokeError("explain_print_term returned an MCP error")

        validate = responses[3].get("result")
        if not isinstance(validate, dict) or validate.get("isError") is not False:
            raise SmokeError("validate_order returned an MCP error")
        validate_structured = validate.get("structuredContent")
        if not isinstance(validate_structured, dict) or validate_structured.get("sessionId") != session_id:
            raise SmokeError("validate_order did not preserve the bound session")

        return {
            "ok": True,
            "sessionId": session_id,
            "capabilities": capabilities,
            "responseCount": len(responses),
            "tools": sorted(tool_names),
            "stderrBytes": len(completed.stderr.encode("utf-8", errors="replace")),
        }
    finally:
        if temporary is not None:
            temporary.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the offline PrintOps dsh/MCP transcript smoke")
    parser.add_argument("--session-id", default="dsh-smoke")
    parser.add_argument("--memory-path", default=None)
    parser.add_argument("--capabilities", default="L0")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    try:
        summary = run_smoke(args.session_id, args.memory_path, args.capabilities, args.timeout)
    except SmokeError as error:
        print(f"dsh MCP smoke failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
