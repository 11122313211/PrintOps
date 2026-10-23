"""Operational backup contracts: WAL consistency, evidence, restore and policy."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from storage_backup import create_backup, inventory, restore_isolated, verify_backup
from storage_policy import StoragePolicy, retention_plan
from storage_sqlite import SQLiteOrderStore


class StorageBackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.sqlite3"
        self.store = SQLiteOrderStore(self.source)
        self.record = self.store.create_order({"orderId": "example", "state": {
            "schemaVersion": 3, "revision": 0, "order": {"productType": "名片"},
            "messages": [{"role": "user", "text": "合成订单"}], "quoteRequests": []}})
        self.record["state"]["confirmation"] = {"status": "confirmed"}
        self.record["state"]["quoteRequests"] = [{"requestId": "q1", "status": "stale", "audit": ["synthetic"]}]
        self.record = self.store.save_order(self.record, 0)
        self.bundle = self.root / "backup"

    def test_backup_and_restore_preserve_all_tables_without_touching_source(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("CREATE TABLE plugin_evidence (id TEXT, raw BLOB)")
            db.execute("INSERT INTO plugin_evidence VALUES ('extra', ?)", (b'\x00\xff',))
        before = inventory(self.source)
        manifest = create_backup(self.source, self.bundle)
        self.assertEqual(manifest["inventory"], before)
        self.assertIn("plugin_evidence", manifest["inventory"]["tables"])
        self.assertTrue(verify_backup(self.bundle)["verified"])
        result = restore_isolated(self.bundle, self.root / "restored")
        self.assertTrue(all(result["checks"].values()))
        self.assertFalse(result["liveDatabaseChanged"])
        self.assertEqual(inventory(result["database"]), before)
        self.assertEqual(inventory(self.source), before)
        self.assertEqual(SQLiteOrderStore(result["database"]).load_order("example")["revision"], 1)

    def test_wal_commits_are_included_but_uncommitted_writes_are_not(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("CREATE TABLE wal_fixture (value TEXT)")
            db.execute("INSERT INTO wal_fixture VALUES ('committed')")
            db.commit()
            db.execute("INSERT INTO wal_fixture VALUES ('uncommitted')")
            create_backup(self.source, self.bundle)
            with closing(sqlite3.connect(self.bundle / "orders.sqlite3")) as backup, backup:
                self.assertEqual(backup.execute("SELECT value FROM wal_fixture").fetchall(), [("committed",)])
            db.rollback()

    def test_concurrent_business_writes_have_consistent_snapshot_and_events(self):
        def write():
            record = self.store.load_order("example")
            for quantity in range(1, 12):
                record["state"]["order"]["quantity"] = f"{quantity} 张"
                record = self.store.save_order(record, record["revision"])
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(write)
            create_backup(self.source, self.bundle)
            future.result()
        verify_backup(self.bundle)
        with closing(sqlite3.connect(self.bundle / "orders.sqlite3")) as db, db:
            revision = db.execute("SELECT revision FROM orders WHERE order_id='example'").fetchone()[0]
            self.assertEqual(db.execute("SELECT MAX(revision) FROM order_events WHERE order_id='example'").fetchone()[0], revision)
            mirror = json.loads(db.execute("SELECT state FROM sessions WHERE id='example'").fetchone()[0])
            self.assertEqual(mirror["revision"], revision)

    def test_tampering_and_existing_restore_destination_are_rejected(self):
        create_backup(self.source, self.bundle)
        with self.assertRaises(ValueError):
            restore_isolated(self.bundle, self.root)
        with self.assertRaises(ValueError):
            create_backup(self.source, self.bundle)
        with closing(sqlite3.connect(self.bundle / "orders.sqlite3")) as db, db:
            db.execute("DELETE FROM audit_log")
        with self.assertRaisesRegex(ValueError, "digest"):
            verify_backup(self.bundle)
        self.assertFalse((self.root / "never-created").exists())
        with self.assertRaises(ValueError):
            restore_isolated(self.bundle, self.root / "never-created")
        self.assertFalse((self.root / "never-created").exists())

    def test_cli_round_trip(self):
        script = Path(__file__).resolve().parents[1] / "tools" / "order_backup.py"
        for arguments in (["create", str(self.source), "--output", str(self.bundle)],
                          ["verify", str(self.bundle)],
                          ["restore", str(self.bundle), "--destination", str(self.root / "cli-restore")],
                          ["retention-plan", "--directory", str(self.root)]):
            result = subprocess.run([sys.executable, str(script), *arguments], capture_output=True, text=True,
                                encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1",
                                                       "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsInstance(json.loads(result.stdout), dict)

    def test_malformed_manifest_is_rejected_without_unsafe_retention_planning(self):
        create_backup(self.source, self.bundle)
        path = self.bundle / "manifest.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for invalid in ([], {**original, "createdAt": None}, {**original, "createdAt": "2026-10-15T00:00:00"}):
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaises(ValueError):
                verify_backup(self.bundle)

    def test_legacy_store_migrated_before_backup_retains_original_and_cas(self):
        legacy = self.root / "old.sqlite3"
        raw = json.dumps({"order": {"productType": "名片", "size": "B5"}, "revision": 7})
        with closing(sqlite3.connect(legacy)) as db, db:
            db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL)")
            db.execute("INSERT INTO sessions VALUES ('legacy', ?)", (raw,))
        with self.assertRaises(ValueError):
            create_backup(legacy, self.root / "unmigrated")
        SQLiteOrderStore(legacy)
        create_backup(legacy, self.bundle)
        result = restore_isolated(self.bundle, self.root / "migrated-restore")
        with closing(sqlite3.connect(result["database"])) as db, db:
            self.assertEqual(db.execute("SELECT state FROM legacy_session_backups").fetchone()[0], raw)
            self.assertEqual(db.execute("SELECT revision FROM orders").fetchone()[0], 7)


class StoragePolicyTest(unittest.TestCase):
    def test_defaults_preserve_everything_and_invalid_policy_is_rejected(self):
        policy = StoragePolicy()
        backups = [{"bundle": "old", "verified": True, "createdAt": "2020-01-01T00:00:00+00:00"}]
        self.assertFalse(retention_plan(policy, backups)["backupExpirationCandidates"])
        for values in ({"preserve_audit": False}, {"backup_keep_count": 0}, {"archive_after_days": True}, {"typo": 10}):
            with self.assertRaises(ValueError):
                StoragePolicy.from_mapping(values)

    def test_retention_only_plans_and_preserves_newest_and_active_orders(self):
        backups = [{"bundle": str(day), "verified": True, "createdAt": f"2026-10-{day:02d}T00:00:00+00:00"} for day in (1, 2, 3)]
        backups.append({"bundle": "bad", "verified": False})
        orders = [{"orderId": status, "status": status, "revision": 4, "updatedAt": "2026-01-01T00:00:00+00:00"}
                  for status in ("cancelled", "draft", "confirmed", "archived")]
        plan = retention_plan(StoragePolicy(backup_keep_count=1, backup_max_age_days=2, archive_after_days=30),
                              backups, orders, now=datetime(2026, 10, 15, tzinfo=timezone.utc))
        self.assertEqual(plan["backupExpirationCandidates"], ["2", "1"])
        self.assertEqual(plan["archiveCandidates"], [{"orderId": "cancelled", "expectedRevision": 4}])
        self.assertEqual(plan["mode"], "plan_only")
        self.assertTrue(plan["preserveAudit"])
        self.assertFalse(plan["deleteBusinessRecords"])


if __name__ == "__main__":
    unittest.main()
