"""Tests for the dependency-free local host fallback."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_host():
    path = REPO_ROOT / "tools" / "printops_local_host.py"
    spec = importlib.util.spec_from_file_location("printops_local_host_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = _load_host()


class LocalHostTest(unittest.TestCase):
    def test_discovers_all_project_skills_without_yaml_dependency(self):
        skills = host.discover_skills()
        self.assertEqual(
            [item["name"] for item in skills],
            ["print-intake", "print-preflight", "print-process",
             "print-quote-handoff", "print-specs"],
        )
        self.assertTrue(all(item["description"] for item in skills))
        self.assertTrue(all(item["path"].endswith("/SKILL.md") for item in skills))

    def test_run_host_validates_mcp_and_processes_rule_mode_message(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "agent.sqlite3"
            result = host.run_host(
                session_id="local-test",
                memory_path=db,
                messages=["做 500 张 A4 名片，250g铜版纸，双面四色，下周内"],
            )
            resumed = host.run_host(
                session_id="local-test",
                memory_path=db,
                messages=[],
                validate_mcp=False,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["runtime"], "python-stdlib")
            self.assertFalse(result["dshCompatible"])
            self.assertEqual(result["mcp"]["responseCount"], 4)
            self.assertIn("validate_order", result["mcp"]["tools"])
            self.assertIn("explain_print_term", result["mcp"]["tools"])
            self.assertEqual(result["response"]["workflowStage"], "recommend")
            self.assertEqual(result["response"]["order"]["quantityValue"], 500)
            self.assertTrue(result["sessionCreated"])
            self.assertFalse(result["sessionReused"])
            self.assertFalse(resumed["sessionCreated"])
            self.assertTrue(resumed["sessionReused"])

    def test_cli_emits_one_strict_json_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "agent.sqlite3"
            completed = subprocess.run(
                [sys.executable, "tools/printops_local_host.py",
                 "--session-id", "cli-test", "--memory-path", str(db),
                 "--message", "解释一下出血"],
                cwd=str(REPO_ROOT),
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["sessionId"], "cli-test")
        self.assertEqual(payload["runtime"], "python-stdlib")
        self.assertEqual(payload["mcp"]["capabilities"], "L0")
        self.assertIn("出血", payload["response"]["messages"][0])

    def test_invalid_session_fails_before_starting_mcp(self):
        with self.assertRaises(host.LocalHostError):
            host.run_host(session_id="not valid", validate_mcp=False)

    def test_smoke_cli_returns_mcp_summary_without_processing_message(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "agent.sqlite3"
            completed = subprocess.run(
                [sys.executable, "tools/printops_local_host.py", "--smoke",
                 "--session-id", "smoke-cli", "--memory-path", str(db)],
                cwd=str(REPO_ROOT),
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "smoke")
        self.assertEqual(payload["mcp"]["responseCount"], 4)
        self.assertIn("validate_order", payload["mcp"]["tools"])

    def test_invalid_session_cli_is_still_one_strict_json_document(self):
        completed = subprocess.run(
            [sys.executable, "tools/printops_local_host.py", "--session-id", "bad session"],
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["type"], "LocalHostError")


if __name__ == "__main__":
    unittest.main()
