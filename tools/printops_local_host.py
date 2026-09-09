#!/usr/bin/env python3
"""Minimal local PrintOps host with no Node, npm, pnpm, or third-party deps.

This host is the local fallback for environments where DeepSeek Harness cannot
be installed.  It validates the dsh-facing skill/MCP contract through the same
stdio launcher, then uses the deterministic PrintOps Agent for chat.  It is
deliberately a host for the local kernel, not a claim that the dsh runtime is
present or that a remote model is being called.
"""

from __future__ import annotations

import argparse
import atexit
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / ".dsh" / "skills"
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TEMP_DIRS: list[Path] = []


def _cleanup_temp_dirs() -> None:
    for directory in _TEMP_DIRS:
        try:
            shutil.rmtree(directory, ignore_errors=True)
        except OSError:
            pass


atexit.register(_cleanup_temp_dirs)


class LocalHostError(RuntimeError):
    """A bounded, user-facing local host error."""


class _JsonArgumentParser(argparse.ArgumentParser):
    """Turn command-line validation failures into the host JSON protocol."""

    def error(self, message: str) -> None:  # pragma: no cover - exercised via CLI
        raise LocalHostError(message)


def _session_id(value: Any) -> str:
    if not isinstance(value, str) or not SESSION_ID_PATTERN.fullmatch(value):
        raise LocalHostError(
            "session id must contain only ASCII letters, digits, '_' or '-' "
            "and be 1-64 characters"
        )
    return value


def _frontmatter(skill_file: Path) -> dict[str, str]:
    """Read the two scalar fields needed for a local skill inventory.

    The dsh filesystem skill only needs the frontmatter summary here.  A small
    parser keeps this fallback independent of PyYAML while avoiding execution
    or interpretation of arbitrary skill content.
    """
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise LocalHostError(f"unable to read skill {skill_file}: {error}") from error
    if not text.startswith("---\n"):
        raise LocalHostError(f"skill {skill_file} has no frontmatter")
    frontmatter, separator, _ = text[4:].partition("\n---\n")
    if not separator:
        raise LocalHostError(f"skill {skill_file} has an unterminated frontmatter block")
    fields: dict[str, str] = {}
    for line in frontmatter.splitlines():
        key, marker, value = line.partition(":")
        if marker:
            fields[key.strip()] = value.strip()
    name = fields.get("name", "")
    description = fields.get("description", "")
    if not name or not description:
        raise LocalHostError(f"skill {skill_file} needs name and description")
    return {"name": name, "description": description}


def discover_skills(skills_root: str | Path = SKILLS_ROOT) -> list[dict[str, str]]:
    """Return the bounded project skill inventory in stable order."""
    root = Path(skills_root)
    if not root.is_dir():
        raise LocalHostError(f"skills directory is missing: {root}")
    discovered: list[dict[str, str]] = []
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        skill_file = directory / "SKILL.md"
        if not skill_file.is_file():
            continue
        summary = _frontmatter(skill_file)
        if summary["name"] != directory.name:
            raise LocalHostError(
                f"skill directory/name mismatch: {directory.name} != {summary['name']}"
            )
        # Keep the inventory useful to a host or diagnostic UI without
        # exposing the full skill body.  A repository-relative path is stable
        # across machines and is still directly resolvable from REPO_ROOT.
        try:
            relative_path = skill_file.resolve().relative_to(REPO_ROOT)
            summary["path"] = relative_path.as_posix()
        except ValueError:
            # Tests and embedders may point discovery at an external fixture;
            # preserve that fixture's path rather than failing inventory.
            summary["path"] = str(skill_file.resolve())
        discovered.append(summary)
    if not discovered:
        raise LocalHostError("no project skills were discovered")
    return discovered


def _resolve_memory_path(value: str | Path | None) -> Path:
    if value is None or str(value) == "":
        # Keep a default host run disposable.  Persistent state is opt-in via
        # --memory-path, which prevents a smoke check from changing data/.
        directory = Path(tempfile.mkdtemp(prefix="printops-local-host-"))
        _TEMP_DIRS.append(directory)
        return directory / "agent.sqlite3"
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve(strict=False)


def _session_exists(memory_path: Path, session_id: str) -> bool:
    """Return whether a persisted session exists before this host run.

    The check is intentionally read-only and tolerant of a not-yet-created
    database/table.  ``Memory`` remains the authority for loading and saving;
    this helper only supplies the user-visible created/reused diagnostic.
    """
    if not memory_path.is_file():
        return False
    try:
        with sqlite3.connect(memory_path, timeout=1.0) as db:
            row = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
            ).fetchone()
            if row is None:
                return False
            return db.execute(
                "SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (session_id,)
            ).fetchone() is not None
    except sqlite3.Error:
        # Let ``Memory`` produce the authoritative error on startup.  A
        # malformed or locked database must never turn the diagnostic into a
        # false claim that an existing session was reused.
        return False


def _runtime_inventory() -> dict[str, Any]:
    """Report optional runtimes without making them a startup requirement."""
    return {
        "python": sys.version.split()[0],
        "dsh": shutil.which("dsh") is not None,
        "node": shutil.which("node") is not None,
        "npm": shutil.which("npm") is not None,
        "pnpm": shutil.which("pnpm") is not None,
    }


def _compact_agent_response(response: dict[str, Any]) -> dict[str, Any]:
    """Keep CLI output useful without dumping the full chat history."""
    keys = (
        "sessionId", "messages", "workflowStage", "workflowLabel", "order",
        "validation", "nextAction", "decision", "toolResult", "revision",
        "fieldMeta", "planMeta", "rejectedFields", "runId",
    )
    return {key: response[key] for key in keys if key in response}


def _load_agent(memory_path: Path, session_id: str) -> Any:
    # Imports stay inside the execution path so --help and skill inspection do
    # not initialize SQLite or pull in any optional application state.
    sys.path.insert(0, str(REPO_ROOT))
    from agent import Agent, Memory  # pylint: disable=import-outside-toplevel

    return Agent(Memory(memory_path), session_id)


def run_mcp_smoke(session_id: str, memory_path: Path, capabilities: str) -> dict[str, Any]:
    """Run the actual stdio transcript used by the dsh-facing boundary."""
    helper_path = REPO_ROOT / "tools" / "dsh_mcp_smoke.py"
    if not helper_path.is_file():
        raise LocalHostError("dsh_mcp_smoke.py is missing")
    import importlib.util  # local stdlib import

    spec = importlib.util.spec_from_file_location("printops_local_smoke", helper_path)
    if spec is None or spec.loader is None:
        raise LocalHostError("unable to load dsh_mcp_smoke.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.run_smoke(session_id, memory_path, capabilities)
    except Exception as error:  # expose one stable host-level error
        raise LocalHostError(str(error)) from error


def run_host(
    session_id: str = "local-demo",
    memory_path: str | Path | None = None,
    capabilities: str = "L0",
    messages: list[str] | None = None,
    validate_mcp: bool = True,
) -> dict[str, Any]:
    """Run one local host session and return a JSON-serializable summary."""
    session = _session_id(session_id)
    skills = discover_skills()
    db_path = _resolve_memory_path(memory_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    session_reused = _session_exists(db_path, session)
    summary: dict[str, Any] = {
        "ok": True,
        "runtime": "python-stdlib",
        "dshCompatible": False,
        "sessionId": session,
        "sessionCreated": not session_reused,
        "sessionReused": session_reused,
        "memoryPath": str(db_path),
        "runtimes": _runtime_inventory(),
        "skills": skills,
    }
    if validate_mcp:
        summary["mcp"] = run_mcp_smoke(session, db_path, capabilities)
    agent = _load_agent(db_path, session)
    responses: list[dict[str, Any]] = []
    for message in messages or []:
        if not isinstance(message, str) or not message.strip():
            continue
        responses.append(_compact_agent_response(agent.chat(message.strip())))
    if responses:
        summary["messageCount"] = len(responses)
        summary["response"] = responses[-1]
    else:
        summary["response"] = _compact_agent_response(agent.snapshot())
    return summary


def _messages_from_stdin() -> Iterator[str]:
    for line in sys.stdin:
        text = line.strip()
        if text:
            yield text


def main(argv: list[str] | None = None) -> int:
    parser = _JsonArgumentParser(
        description="Run the dependency-free local PrintOps host (no dsh/Node/npm/pnpm required)"
    )
    parser.add_argument("--session-id", default="local-demo")
    parser.add_argument("--memory-path", default=None,
                        help="SQLite path; omit for a disposable temporary session")
    parser.add_argument("--capabilities", default="L0",
                        help="MCP capabilities: L0 or L0,L1")
    parser.add_argument("--message", action="append", default=[],
                        help="one local rule-mode message; may be repeated")
    parser.add_argument("--smoke", action="store_true",
                        help="run the MCP/skill smoke only (do not process messages)")
    parser.add_argument("--interactive", action="store_true",
                        help="read one message per stdin line and emit one JSON result per line")
    parser.add_argument("--no-mcp", action="store_true",
                        help="skip MCP transcript validation (not recommended for acceptance)")
    try:
        args = parser.parse_args(argv)
        if args.smoke and (args.message or args.interactive or args.no_mcp):
            raise LocalHostError(
                "--smoke cannot be combined with --message, --interactive, or --no-mcp"
            )
        # An interactive process performs the MCP handshake once, then keeps
        # the deterministic Agent in the same session for each input line.
        initial = run_host(
            session_id=args.session_id,
            memory_path=args.memory_path,
            capabilities=args.capabilities,
            messages=[] if args.smoke else args.message,
            validate_mcp=not args.no_mcp,
        )
        if args.smoke:
            initial["mode"] = "smoke"
        if not args.interactive:
            print(json.dumps(initial, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")))
            return 0

        print(json.dumps(initial, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")))
        # Reopen the same persisted session for stdin messages.  This keeps the
        # command simple and makes each output line independently inspectable.
        db_path = Path(initial["memoryPath"])
        agent = _load_agent(db_path, _session_id(args.session_id))
        for message in _messages_from_stdin():
            result = {
                "ok": True,
                "runtime": "python-stdlib",
                "sessionId": args.session_id,
                "response": _compact_agent_response(agent.chat(message)),
            }
            print(json.dumps(result, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")), flush=True)
        return 0
    except (LocalHostError, OSError, ValueError, TypeError, sqlite3.Error) as error:
        # This CLI is consumed by dsh-side wrappers and CI, so failures are a
        # single strict JSON document too.  Keep diagnostics bounded and avoid
        # leaking a traceback or arbitrary exception representation.
        payload = {
            "ok": False,
            "runtime": "python-stdlib",
            "error": {
                "type": error.__class__.__name__,
                "message": str(error)[:1024],
            },
        }
        print(json.dumps(payload, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
