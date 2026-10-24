"""One-command local gate: this project intentionally has no CI (see README).

Releases are validated on the developer's own machine. This runner chains the
documented checks into one command with a stable summary:

    python3 tools/dev_check.py          # compile, unit tests, evaluation, guards
    python3 tools/dev_check.py --quick  # compile + guards only (fast loop)

It is standard-library only and forces UTF-8 pipes so Chinese report text
survives legacy Windows consoles.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE = r"(\.git|__pycache__|node_modules|\.venv|^venv|[/\\]data[/\\])"


def _run(name, argv):
    env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    started = time.monotonic()
    proc = subprocess.run(argv, cwd=str(ROOT), env=env, encoding="utf-8",
                          errors="replace", stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, check=False)
    elapsed = time.monotonic() - started
    if proc.returncode == 0:
        print("[PASS] {0} ({1:.1f}s)".format(name, elapsed))
        return True
    print("[FAIL] {0} ({1:.1f}s, exit={2})".format(name, elapsed, proc.returncode))
    tail = [line for line in (proc.stdout or "").splitlines() if line.strip()][-8:]
    for line in tail:
        print("       " + line)
    return False


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass
    parser = argparse.ArgumentParser(description="PrintOps 本机一键门禁（替代 CI 的本地发布检查）")
    parser.add_argument("--quick", action="store_true",
                        help="跳过单元测试与评测，只做编译与仓库守护")
    args = parser.parse_args()
    print("PrintOps dev gate - Python {0} on {1}, cwd={2}".format(
        sys.version.split()[0], os.name, ROOT))
    results = [_run("compileall",
                    [sys.executable, "-m", "compileall", "-q", "-x", EXCLUDE, "."])]
    if not args.quick:
        results.append(_run("unit tests",
                            [sys.executable, "-m", "unittest", "discover",
                             "-s", "tests", "-p", "test_*.py"]))
        results.append(_run("evaluation", [sys.executable, "tests/evaluate_agent.py"]))
        results.append(_run("knowledge review", [sys.executable, "tools/knowledge_review.py"]))
    results.append(_run("secret scan", [sys.executable, "tools/secret_scan.py"]))
    results.append(_run("repo guard", [sys.executable, "tools/repo_guard.py"]))
    print("")
    print("本地门禁：" + ("全部通过" if all(results) else "存在失败项（发布检查未通过）"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())