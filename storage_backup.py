"""Consistent SQLite backup bundles and isolated, verifiable restore drills.

No raw copy of a live WAL database, no live restore, and no record deletion.
The manifest is an integrity receipt, not a cryptographic signature.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from typing import Any
from uuid import uuid4

from storage_contract import STORE_CONTRACT_VERSION, StoreConflictError

BACKUP_FORMAT = "printops-sqlite-backup/1"
DATABASE_NAME = "orders.sqlite3"
REQUIRED_TABLES = {"orders", "sessions", "storage_migrations", "legacy_session_backups",
                   "order_events", "messages", "quote_requests", "audit_log"}


def _read_only(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA trusted_schema=OFF")
    return db


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def inventory(path: str | Path) -> dict[str, Any]:
    """Fingerprint the contract tables, extension tables and sequence state.

    Contract tables are row-digested through literal SQL; an extension table
    is reported by its DDL instead, because a SELECT built from a table name
    read out of sqlite_master would put database metadata into a statement.
    """
    with closing(_read_only(Path(path))) as db:
        db.execute("BEGIN")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("SQLite foreign key check failed")
        schema = [list(row) for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
        existing = {row[1] for row in schema if row[0] == "table"}
        if not REQUIRED_TABLES.issubset(existing):
            raise ValueError("database is not a current PrintOps order store; migrate legacy sessions first")
        tables = {}
        for name, rows in (
            ("audit_log", db.execute("SELECT * FROM audit_log").fetchall()),
            ("legacy_session_backups", db.execute("SELECT * FROM legacy_session_backups").fetchall()),
            ("messages", db.execute("SELECT * FROM messages").fetchall()),
            ("order_events", db.execute("SELECT * FROM order_events").fetchall()),
            ("orders", db.execute("SELECT * FROM orders").fetchall()),
            ("quote_requests", db.execute("SELECT * FROM quote_requests").fetchall()),
            ("sessions", db.execute("SELECT * FROM sessions").fetchall()),
            ("sqlite_sequence", db.execute("SELECT * FROM sqlite_sequence").fetchall()),
            ("storage_migrations", db.execute("SELECT * FROM storage_migrations").fetchall()),
        ):
            hashes = []
            for row in rows:
                typed = [("blob", value.hex()) if isinstance(value, bytes) else (type(value).__name__, value)
                         for value in row]
                hashes.append(hashlib.sha256(_json(typed).encode()).hexdigest())
            tables[name] = {"rows": len(hashes), "sha256": hashlib.sha256("".join(sorted(hashes)).encode()).hexdigest()}
        # Extension tables are outside the PrintOps contract. They are still
        # reported, identified by their DDL, because selecting from a table
        # name read out of sqlite_master would put database metadata into a
        # statement; the file digest covers their rows.
        schema_by_name = {row[1]: row[3] for row in schema if row[0] == "table"}
        for name, ddl in sorted(schema_by_name.items()):
            if name not in tables:
                tables[name] = {"rows": None,
                                "sha256": hashlib.sha256(_json(ddl).encode()).hexdigest()}
        if not REQUIRED_TABLES.issubset(tables):
            raise ValueError("database is not a current PrintOps order store; migrate legacy sessions first")
        migrations = [row[0] for row in db.execute("SELECT version FROM storage_migrations ORDER BY version")]
        if migrations != [1]:
            raise ValueError("unsupported storage migration version")
        versions = [row[0] for row in db.execute("SELECT DISTINCT schema_version FROM orders ORDER BY schema_version")]
        for version in versions:
            if version not in {"1", "2", "2.0", "3"}:
                raise ValueError("unsupported order schema version")
        return {"tables": tables, "schemaSha256": hashlib.sha256(_json(schema).encode()).hexdigest(),
                "storageMigrations": migrations, "orderSchemaVersions": versions,
                "userVersion": db.execute("PRAGMA user_version").fetchone()[0]}


def create_backup(source: str | Path, bundle: str | Path, *, timeout_seconds: float = 30) -> dict[str, Any]:
    source, bundle = Path(source).resolve(), Path(bundle).absolute()
    if not source.is_file():
        raise ValueError("source database does not exist")
    if not 0 < timeout_seconds <= 300:
        raise ValueError("backup timeout must be in (0, 300] seconds")
    if bundle.exists() or bundle.is_symlink():
        raise ValueError("backup bundle must be a new directory")
    bundle.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".printops-backup-", dir=bundle.parent))
    try:
        database = temporary / DATABASE_NAME
        deadline = time.monotonic() + timeout_seconds
        def progress(status: int, remaining: int, total: int) -> None:
            if time.monotonic() >= deadline:
                raise TimeoutError("SQLite backup deadline exceeded")
        with closing(_read_only(source)) as original, closing(sqlite3.connect(database)) as target:
            original.backup(target, pages=128, progress=progress, sleep=0.01)
            # Produce a self-contained image; the live source may use WAL.
            target.execute("PRAGMA journal_mode=DELETE")
        os.chmod(database, 0o600)
        manifest = {"format": BACKUP_FORMAT, "backupId": uuid4().hex,
                    "createdAt": datetime.now(timezone.utc).isoformat(), "databaseFile": DATABASE_NAME,
                    "storeContractVersion": STORE_CONTRACT_VERSION, "sqliteVersion": sqlite3.sqlite_version,
                    "databaseSha256": _digest_file(database), "inventory": inventory(database),
                    "scope": "all SQLite tables; uploaded file bytes and external credentials excluded"}
        (temporary / "manifest.json").write_text(_json(manifest) + "\n", encoding="utf-8")
        os.chmod(temporary / "manifest.json", 0o600)
        # Refuse overwrite even if another operator created the destination meanwhile.
        bundle.mkdir(mode=0o700)
        for name in (DATABASE_NAME, "manifest.json"):
            os.replace(temporary / name, bundle / name)
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def verify_backup(bundle: str | Path) -> dict[str, Any]:
    bundle = Path(bundle)
    database, manifest_path = bundle / DATABASE_NAME, bundle / "manifest.json"
    if bundle.is_symlink() or database.is_symlink() or manifest_path.is_symlink():
        raise ValueError("backup paths must not be symlinks")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT or manifest.get("databaseFile") != DATABASE_NAME:
        raise ValueError("unsupported backup manifest")
    if manifest.get("storeContractVersion") != STORE_CONTRACT_VERSION:
        raise ValueError("unsupported store contract version")
    try:
        created = datetime.fromisoformat(manifest["createdAt"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid backup timestamp") from error
    if created.tzinfo is None or not isinstance(manifest.get("backupId"), str) or not manifest["backupId"]:
        raise ValueError("backup identity or timezone is missing")
    if _digest_file(database) != manifest.get("databaseSha256"):
        raise ValueError("backup database digest mismatch")
    actual = inventory(database)
    if actual != manifest.get("inventory"):
        raise ValueError("backup table/schema inventory mismatch")
    return {"verified": True, "bundle": str(bundle.resolve()), **manifest}


def _compatibility_drill(database: Path) -> dict[str, Any]:
    """Exercise migration, message/audit preservation and CAS on a disposable copy."""
    from storage_sqlite import SQLiteOrderStore
    with tempfile.TemporaryDirectory(prefix="printops-restore-check-") as temporary:
        probe = Path(temporary) / DATABASE_NAME
        shutil.copyfile(database, probe)
        before = inventory(probe)
        store = SQLiteOrderStore(probe)
        after = inventory(probe)
        if before != after:
            raise ValueError("opening current backup unexpectedly changed database contents")
        order_id = "restore-check-" + uuid4().hex
        record = store.create_order({"orderId": order_id, "state": {"schemaVersion": 3, "revision": 0,
                                    "order": {}, "messages": [{"role": "user", "text": "restore drill"}],
                                    "quoteRequests": []}})
        stale = deepcopy(record)
        record["state"]["confirmation"] = {"status": "confirmed"}
        saved = store.save_order(record, record["revision"])
        try:
            store.save_order(stale, stale["revision"])
        except StoreConflictError:
            pass
        else:
            raise ValueError("CAS conflict was not rejected")
        export = store.export_order(order_id)
        if saved["revision"] != 1 or len(export["messages"]) != 1 or not export["auditLog"] or len(export["events"]) != 2:
            raise ValueError("restore compatibility drill failed")
        return {"migrationOpenPreserved": True, "allTablesPreserved": True, "messagesPreserved": True,
                "auditPreserved": True, "staleCASRejected": True, "probeWritesIsolated": True}


def restore_isolated(bundle: str | Path, destination: str | Path) -> dict[str, Any]:
    verified = verify_backup(bundle)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("restore destination must be a new isolated directory")
    destination.mkdir(mode=0o700, parents=True)
    database = destination / DATABASE_NAME
    try:
        shutil.copyfile(Path(bundle) / DATABASE_NAME, database)
        os.chmod(database, 0o600)
        # Recheck after copying so a concurrently replaced source cannot escape validation.
        if _digest_file(database) != verified["databaseSha256"] or inventory(database) != verified["inventory"]:
            raise ValueError("restored image differs from verified backup")
        checks = _compatibility_drill(database)
        report = {"status": "verified_isolated_restore", "backupId": verified["backupId"],
                  "database": str(database), "checks": checks, "liveDatabaseChanged": False}
        (destination / "restore-report.json").write_text(_json(report) + "\n", encoding="utf-8")
        return report
    except Exception:
        shutil.rmtree(destination)
        raise
