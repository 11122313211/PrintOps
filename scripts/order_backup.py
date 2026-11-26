#!/usr/bin/env python3
"""Create/verify SQLite backups and rehearse restores without a live cutover."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storage_backup import create_backup, restore_isolated, verify_backup
from storage_policy import StoragePolicy, retention_plan
import stdio_utf8


def main(argv=None) -> int:
    stdio_utf8.configure()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, help="JSON StoragePolicy (optional)")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="consistent online backup into a new bundle")
    create.add_argument("source", type=Path)
    create.add_argument("--output", type=Path)
    verify = commands.add_parser("verify", help="verify digest, schema and every table")
    verify.add_argument("bundle", type=Path)
    restore = commands.add_parser("restore", help="restore only into a new isolated directory")
    restore.add_argument("bundle", type=Path)
    restore.add_argument("--destination", required=True, type=Path)
    plan = commands.add_parser("retention-plan", help="list candidates; never delete or archive")
    plan.add_argument("--directory", type=Path)
    args = parser.parse_args(argv)
    try:
        policy = StoragePolicy.from_file(args.policy) if args.policy else StoragePolicy()
        if args.command == "create":
            output = args.output or Path(policy.backup_directory) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            result = {"bundle": str(output.resolve()), **create_backup(args.source, output)}
        elif args.command == "verify":
            result = verify_backup(args.bundle)
        elif args.command == "restore":
            result = restore_isolated(args.bundle, args.destination)
        else:
            directory = args.directory or Path(policy.backup_directory)
            backups, rejected = [], []
            for path in sorted(directory.iterdir()) if directory.exists() else []:
                if not path.is_dir():
                    continue
                try:
                    backups.append(verify_backup(path))
                except (OSError, ValueError, sqlite3.Error, KeyError) as error:
                    rejected.append({"bundle": str(path), "reason": str(error)})
            result = {**retention_plan(policy, backups), "unverifiedBundlesKept": rejected}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error, KeyError, TimeoutError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
