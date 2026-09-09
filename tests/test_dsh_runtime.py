"""Offline runtime contracts for the dsh-facing PrintOps integration."""

from __future__ import annotations

import json
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path



REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_helper(filename: str, module_name: str):
    """Load a helper under tools/ without shadowing the legacy tools.py module."""
    path = REPO_ROOT / "tools" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_launcher = _load_helper("dsh_mcp_launcher.py", "printops_dsh_mcp_launcher_test")
_smoke = _load_helper("dsh_mcp_smoke.py", "printops_dsh_mcp_smoke_test")
build_server_argv = _launcher.build_server_argv
run_smoke = _smoke.run_smoke


class DshRuntimeTest(unittest.TestCase):
    def test_runtime_lock_contains_exact_upstream_pins_and_explicit_status(self):
        path = REPO_ROOT / ".dsh" / "runtime.lock.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["dsh"]["package"], "@deepseek-ai/dsh")
        self.assertEqual(payload["dsh"]["release"], "0.1.5-alpha.1")
        self.assertEqual(payload["dsh"]["tag"], "dsh-v0.1.5-alpha.1")
        self.assertEqual(payload["dsh"]["commit"],
                         "5dda764ed3aa172535a7967b06ff95d9cbfe536a")
        self.assertEqual(payload["mcpClient"], {
            "package": "@deepseek-ai/dsh-mcp-client",
            "version": "0.1.5-alpha.1",
        })
        self.assertEqual(payload["skillPackages"]["version"], "0.1.5-alpha.1")
        self.assertEqual(payload["pnpm"]["required"], "11.7.0")
        self.assertEqual(payload["integration"]["defaultCapabilities"], "L0")
        self.assertFalse(payload["integration"]["enabledByDefault"])
        self.assertIn("transitive-lockfile-required",
                      payload["integration"]["lockStatus"])

    def test_profile_is_a_disabled_example_and_never_an_executable_placeholder(self):
        path = REPO_ROOT / ".dsh" / "profile.example.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(payload["enabled"])
        args = payload["mcp"]["argsTemplate"]
        self.assertIn("__BOUND_SESSION_ID__", args)
        self.assertNotIn("${PRINTOPS_SESSION_ID}", json.dumps(payload))
        self.assertEqual(payload["mcp"]["transport"], "stdio")
        self.assertEqual(payload["mcp"]["firstSmokeTools"],
                         ["validate_order", "explain_print_term"])
        self.assertNotIn("--allow-any-session", args)

        manifest = json.loads((REPO_ROOT / ".dsh" / "profile.example" / "package.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual(manifest["dsh"]["profile"]["bundles"],
                         ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless"])
        self.assertEqual(manifest["dependencies"]["@deepseek-ai/dsh-mcp-client"],
                         "0.1.5-alpha.1")
        patch = (REPO_ROOT / ".dsh" / "profile.example" / "cordis.patch.yml")
        patch_text = patch.read_text(encoding="utf-8")
        self.assertIn("disabled: true", patch_text)
        self.assertIn("@deepseek-ai/dsh-mcp-client", patch_text)
        self.assertIn("PRINTOPS_MCP_SESSION_ID", patch_text)
        self.assertIn("failOnStartupError: true", patch_text)

    def test_launcher_resolves_absolute_paths_and_rejects_inherited_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "nested" / "agent.sqlite3"
            environment = dict(os.environ)
            environment.update({
                "PRINTOPS_MCP_SESSION_ID": "attacker-session",
                "PRINTOPS_MCP_CAPABILITIES": "L0,L1",
                "PRINTOPS_MEMORY_PATH": "/tmp/attacker.sqlite3",
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            completed = subprocess.run(
                [sys.executable, "tools/dsh_mcp_launcher.py", "--session-id", "bound-session",
                 "--memory-path", str(db), "--capabilities", "L0", "--dry-run"],
                cwd=str(REPO_ROOT), env=environment, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["cwd"], str(REPO_ROOT))
        self.assertTrue(payload["sessionBound"])
        self.assertFalse(payload["allowAnySession"])
        self.assertEqual(payload["argv"][payload["argv"].index("--session-id") + 1],
                         "bound-session")
        self.assertEqual(payload["argv"][payload["argv"].index("--capabilities") + 1], "L0")
        self.assertEqual(payload["argv"][payload["argv"].index("--memory-path") + 1],
                         str(db.resolve()))
        self.assertTrue(Path(payload["argv"][1]).is_absolute())
        self.assertNotIn("--allow-any-session", payload["argv"])

    def test_launcher_rejects_invalid_session_and_capability_without_protocol_output(self):
        for extra in (("--session-id", "bad session"),
                      ("--session-id", "ok", "--capabilities", "L2")):
            completed = subprocess.run(
                [sys.executable, "tools/dsh_mcp_launcher.py", *extra, "--dry-run"],
                cwd=str(REPO_ROOT), text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                check=False,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stdout, "")
            self.assertIn("launcher:", completed.stderr)

    def test_build_server_argv_makes_l1_additive_and_uses_root_for_relative_db(self):
        argv = build_server_argv("session_1", "data/custom.sqlite3", "L1")
        self.assertEqual(argv[argv.index("--capabilities") + 1], "L0,L1")
        self.assertEqual(argv[argv.index("--memory-path") + 1],
                         str((REPO_ROOT / "data" / "custom.sqlite3").resolve()))

    def test_offline_transcript_smoke_uses_only_readonly_l0_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            summary = run_smoke("runtime-smoke", Path(temporary) / "agent.sqlite3")
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["responseCount"], 4)
        self.assertIn("validate_order", summary["tools"])
        self.assertIn("explain_print_term", summary["tools"])
        self.assertNotIn("prepare_handoff", summary["tools"])

    def test_project_skills_match_dsh_filesystem_frontmatter_contract(self):
        skills_root = REPO_ROOT / ".dsh" / "skills"
        skill_dirs = sorted(path for path in skills_root.iterdir() if path.is_dir())
        self.assertEqual(len(skill_dirs), 5)
        name_pattern = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        for skill_dir in skill_dirs:
            self.assertRegex(skill_dir.name, name_pattern)
            skill_file = skill_dir / "SKILL.md"
            self.assertTrue(skill_file.is_file(), skill_file)
            body = skill_file.read_text(encoding="utf-8")
            self.assertTrue(body.startswith("---\n"), skill_file)
            frontmatter, separator, _ = body[4:].partition("\n---\n")
            self.assertTrue(separator, skill_file)
            fields = {}
            for line in frontmatter.splitlines():
                key, separator, value = line.partition(":")
                if separator:
                    fields[key.strip()] = value.strip()
            self.assertEqual(fields.get("name"), skill_dir.name)
            self.assertTrue(fields.get("description"), skill_file)
            self.assertNotIn("modelInvocable:", frontmatter)
            self.assertNotIn("userInvocable:", frontmatter)
            self.assertNotIn("disableModelInvocation:", frontmatter)


if __name__ == "__main__":
    unittest.main()
