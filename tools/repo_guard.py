"""Check the Git boundary before a commit or GitHub Desktop push.

The guard reports two classes of mistakes without modifying files:

* runtime/secrets that are already tracked;
* untracked files that look like local runtime data but are not ignored.

It intentionally uses only the Python standard library and Git's read-only
commands so it is safe to run against a live local workspace.
"""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TRACKED_FORBIDDEN = (
    "data/*.sqlite",
    "data/*.sqlite3",
    "data/*.sqlite3-*",
    "data/*.db",
    "data/*.db-*",
    "data/llm_config.json",
    "data/corrupted/*",
    "data/uploads/*",
    "data/imports/*",
    "data/exports/*",
    "data/backups/*",
    ".env",
    ".env.*",
    "*.secret",
    "*.secret.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.log",
)
LOCAL_CANDIDATE = (
    "data/*.sqlite",
    "data/*.sqlite3",
    "data/*.sqlite3-*",
    "data/*.db",
    "data/*.db-*",
    "data/llm_config.json",
    "data/corrupted/*",
    "data/uploads/*",
    "data/imports/*",
    "data/exports/*",
    "data/backups/*",
    ".env",
    ".env.*",
    "*.secret",
    "*.secret.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.log",
    "local/*",
    "runtime/*",
    "inbox/*",
    "outbox/*",
    "uploads/*",
    "exports/*",
    "artifacts/*",
    "reports/*",
)


def _git(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    # ``.env.example`` is the one intentionally shareable env file.
    if path == ".env.example":
        return False
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def main() -> int:
    try:
        tracked = _git("ls-files")
        status = _git("status", "--porcelain=v1", "--untracked-files=all")
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: 无法读取 Git 状态：{exc}")
        return 2

    tracked_runtime = [path for path in tracked if _matches(path, TRACKED_FORBIDDEN)]
    unignored_local: list[str] = []
    for line in status:
        # Porcelain v1 has a two-character status prefix followed by a path.
        if len(line) < 4 or line[0:2] == "!!":
            continue
        path = line[3:]
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        if _matches(path, LOCAL_CANDIDATE):
            unignored_local.append(path)

    errors = []
    if tracked_runtime:
        errors.append("已被 Git 跟踪的本地运行态/凭据：" + ", ".join(tracked_runtime))
    if unignored_local:
        errors.append("未被忽略的本地运行态候选：" + ", ".join(unignored_local))

    if errors:
        print("仓库边界检查失败：")
        for error in errors:
            print(f"- {error}")
        print("请检查 .gitignore；不要使用 git add -f 上传这些文件。")
        return 1

    print(f"仓库边界检查通过：{len(tracked)} 个 tracked 文件；未发现待上传的本地运行态文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
