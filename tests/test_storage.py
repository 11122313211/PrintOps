"""Store contracts, migration, atomicity and concurrency; synthetic fixtures only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from storage_contract import OrderStore, StoreConflictError, ToolCallRecorder, timestamp
from storage_factory import create_store, register_store
from storage_memory import InMemoryOrderStore
from storage_sqlite import SQLiteOrderStore


def fixture(order_id="sample"):
    return {"orderId": order_id, "state": {"order": {"productType": "名片", "quantity": "10 张"},
            "revision": 0, "schemaVersion": "2.0", "messages": [], "quoteRequests": []}}


class StoreContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stores = [InMemoryOrderStore(), SQLiteOrderStore(Path(self.tmp.name) / "store.sqlite3")]

    def tearDown(self):
        self.tmp.cleanup()

    def test_both_backends_round_trip_detached_records_and_list(self):
        for store in self.stores:
            with self.subTest(mode=store.metadata()["mode"]):
                self.assertIsInstance(store, OrderStore)
                saved = store.create_order(fixture())
                saved["state"]["order"]["quantity"] = "changed outside store"
                self.assertEqual(store.load_order("sample")["state"]["order"]["quantity"], "10 张")
                self.assertEqual(store.list_orders()["total"], 1)
                self.assertEqual(store.list_orders()["orders"][0]["orderId"], "sample")
                self.assertIsNone(store.load_order("absent"))

    def test_snapshot_and_events_advance_once_and_stale_writer_is_rejected(self):
        for store in self.stores:
            with self.subTest(mode=store.metadata()["mode"]):
                saved = store.create_order(fixture())
                stale = deepcopy(saved)
                saved["state"]["order"]["quantity"] = "20 张"
                updated = store.save_order(saved, 0)
                self.assertEqual(updated["revision"], 1)
                with self.assertRaises(StoreConflictError):
                    store.save_order(stale, 0)
                self.assertEqual(len(store.load_events("sample")), 2)
                self.assertEqual(store.load_events("sample")[-1]["payload"]["changes"]["order"]["before"]["quantity"], "10 张")
                self.assertEqual(store.save_order(updated, 1)["revision"], 1)
                self.assertEqual(len(store.load_events("sample")), 2)

    def test_full_message_history_outlives_context_window_with_repeated_messages(self):
        for store in self.stores:
            with self.subTest(mode=store.metadata()["mode"]):
                record = store.create_order(fixture())
                for index in range(85):
                    message = {"role": "user", "text": "synthetic repeated message"}
                    record["state"]["messages"] = (record["state"]["messages"] + [message])[-80:]
                    record["newMessages"] = [message]
                    record = store.save_order(record, record["revision"])
                self.assertEqual(len(store.load_order("sample")["state"]["messages"]), 80)
                exported = store.export_order("sample")
                self.assertEqual(len(exported["messages"]), 85)
                self.assertEqual(len({m["messageId"] for m in exported["messages"]}), 85)

    def test_archive_retains_export_and_is_excluded_from_active_history(self):
        for store in self.stores:
            with self.subTest(mode=store.metadata()["mode"]):
                store.create_order(fixture())
                archived = store.delete_order("sample", 0)
                self.assertEqual(archived["status"], "archived")
                self.assertEqual(store.list_orders()["total"], 0)
                self.assertEqual(store.list_orders({"includeArchived": True})["total"], 1)
                self.assertEqual(store.delete_order("sample", archived["revision"]), archived)
                with self.assertRaises(StoreConflictError):
                    store.delete_order("sample", 0)
                archived["state"]["order"]["quantity"] = "40 张"
                with self.assertRaises(ValueError):
                    store.save_order(archived, archived["revision"])
                exported = store.export_order("sample")
                self.assertEqual(exported["state"]["order"]["quantity"], "10 张")
                self.assertIn("exported", [a["action"] for a in exported["auditLog"]])
                purged = store.purge_order("sample", archived["revision"])
                self.assertEqual(purged, {"orderId": "sample", "deleted": True})
                self.assertIsNone(store.load_order("sample"))
                self.assertEqual(store.list_orders({"includeArchived": True})["total"], 0)
                self.assertIsNone(store.export_order("sample"))

    def test_quotes_and_confirmation_are_projected_and_audited(self):
        for store in self.stores:
            with self.subTest(mode=store.metadata()["mode"]):
                record = store.create_order(fixture())
                record["state"]["quoteRequests"] = [{"requestId": "synthetic-quote", "status": "awaiting_human_confirmation"}]
                record = store.save_order(record, 0)
                self.assertEqual(record["status"], "quote_draft")
                record["state"]["quoteRequests"][0]["status"] = "stale"
                record["state"]["confirmation"] = {"status": "confirmed"}
                record = store.save_order(record, record["revision"])
                exported = store.export_order("sample")
                self.assertEqual(exported["quoteRequests"][0]["status"], "stale")
                self.assertEqual(exported["status"], "confirmed")
                self.assertIn("confirmation_changed", [a["action"] for a in exported["auditLog"]])

    def test_bad_event_rolls_back_snapshot_messages_and_revision(self):
        for store in self.stores:
            with self.subTest(mode=store.metadata()["mode"]):
                before = store.create_order(fixture())
                attempted = deepcopy(before)
                attempted["state"]["order"]["quantity"] = "99 张"
                attempted["newMessages"] = [{"role": "user", "text": "must roll back"}]
                attempted["events"] = [{"eventType": "invalid", "payload": {"notJson": object()}}]
                with self.assertRaises(TypeError):
                    store.save_order(attempted, 0)
                self.assertEqual(store.load_order("sample"), before)
                self.assertEqual(len(store.load_events("sample")), 1)
                self.assertEqual(store.export_order("sample")["messages"], [])

    def test_pagination_status_filter_and_duplicate_creation(self):
        for store in self.stores:
            with self.subTest(mode=store.metadata()["mode"]):
                for index in range(4):
                    store.create_order(fixture(f"synthetic-{index}"))
                self.assertEqual(len(store.list_orders({"limit": 2, "offset": 1})["orders"]), 2)
                self.assertEqual(store.list_orders({"status": "confirmed"})["total"], 0)
                with self.assertRaises(StoreConflictError):
                    store.create_order(fixture("synthetic-0"))

    def test_tool_call_log_records_aggregates_and_filters_by_window(self):
        for store in self.stores:
            with self.subTest(mode=store.metadata()["mode"]):
                self.assertIsInstance(store, ToolCallRecorder)
                fresh = timestamp()
                stale = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="microseconds")
                store.record_tool_call({"tool_name": "estimate_price", "status": "ok", "duration_ms": 100,
                                        "session_id": "synthetic", "run_id": "run-1", "item_index": 0,
                                        "created_at": fresh})
                store.record_tool_call({"tool_name": "estimate_price", "status": "blocked",
                                        "reason": "order_not_ready", "duration_ms": 200,
                                        "session_id": "synthetic", "created_at": fresh})
                store.record_tool_call({"tool_name": "request_supplier_quote", "status": "rejected",
                                        "reason": "tool_name", "created_at": stale})
                # Malformed entries never raise; they degrade to a failed row.
                store.record_tool_call({"tool_name": 42, "status": "mystery", "duration_ms": "x"})
                month = store.load_tool_stats(30)
                self.assertEqual(month["windowDays"], 30)
                self.assertEqual(month["scope"], "local-only")
                self.assertEqual(month["totals"]["calls"], 3)
                self.assertEqual(month["totals"]["ok"], 1)
                self.assertEqual(month["totals"]["blocked"], 1)
                self.assertEqual(month["totals"]["failed"], 1)
                self.assertEqual(month["totals"]["successRate"], 33)
                self.assertEqual(month["totals"]["avgMs"], 100.0)
                self.assertEqual(month["totals"]["p95Ms"], 200.0)
                self.assertEqual(month["tools"][0]["tool"], "estimate_price")
                self.assertEqual(month["tools"][0]["calls"], 2)
                self.assertEqual(month["blockedReasons"], [{"reason": "order_not_ready", "count": 1}])
                quarter = store.load_tool_stats(90)
                self.assertEqual(quarter["totals"]["calls"], 4)
                self.assertEqual(quarter["totals"]["rejected"], 1)
                estimate = next(tool for tool in quarter["tools"] if tool["tool"] == "estimate_price")
                self.assertEqual(estimate["successRate"], 50)
                with self.assertRaises(ValueError):
                    store.load_tool_stats(0)
                with self.assertRaises(ValueError):
                    store.load_tool_stats("soon")


class SQLiteMigrationTest(unittest.TestCase):
    def test_corrupt_canonical_projection_is_backed_up_before_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "corrupt.sqlite3"
            store = SQLiteOrderStore(path)
            store.create_order(fixture())
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE orders SET runtime_json = '{broken' WHERE order_id = 'sample'")
            self.assertIsNone(store.load_order("sample"))
            backups = list((Path(temporary) / "corrupted").glob("sample-*.json"))
            self.assertEqual(len(backups), 1)
            backed_up = json.loads(backups[0].read_text(encoding="utf-8"))
            self.assertEqual(backed_up["snapshot"]["runtime_json"], "{broken")
            self.assertTrue(store.load_events("sample"))

    def test_legacy_session_migrates_once_and_original_evidence_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite3"
            raw = {"order": {"productType": "名片", "quantity": "500张", "size": "A4"},
                   "messages": [{"role": "user", "text": "合成测试：A4"}],
                   "fieldMeta": {"size": {"value": "A4", "source": "rule", "confidence": 0.84}}, "revision": 7}
            encoded = json.dumps(raw, ensure_ascii=False)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL)")
                db.execute("INSERT INTO sessions VALUES (?, ?)", ("legacy", encoded))
            store = SQLiteOrderStore(path)
            self.assertEqual(store.load_order("legacy")["revision"], 7)
            self.assertEqual(store.load_order("legacy")["state"]["order"]["quantityValue"], 500)
            events = store.load_events("legacy")
            reopened = SQLiteOrderStore(path)
            self.assertEqual(reopened.load_events("legacy"), events)
            with closing(sqlite3.connect(path)) as db, db:
                backup = db.execute("SELECT state FROM legacy_session_backups WHERE session_id='legacy'").fetchone()[0]
                self.assertEqual(backup, encoded)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM storage_migrations").fetchone()[0], 1)

    def test_two_store_instances_compete_on_same_cas(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "shared.sqlite3"
            first, second = SQLiteOrderStore(path), SQLiteOrderStore(path)
            first.create_order(fixture())
            barrier = threading.Barrier(2)
            def write(store, quantity):
                record = store.load_order("sample")
                record["state"]["order"]["quantity"] = quantity
                barrier.wait()
                try:
                    store.save_order(record, 0)
                    return "saved"
                except StoreConflictError:
                    return "conflict"
            with ThreadPoolExecutor(max_workers=2) as pool:
                a, b = pool.submit(write, first, "20 张"), pool.submit(write, second, "30 张")
                self.assertEqual(sorted([a.result(), b.result()]), ["conflict", "saved"])
            self.assertEqual(first.load_order("sample")["revision"], 1)
            self.assertEqual(len(first.load_events("sample")), 2)


class StoreFactoryTest(unittest.TestCase):
    def test_memory_mode_never_creates_a_file_and_custom_registration_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "must-not-exist.sqlite3"
            with patch.dict("os.environ", {"PRINTOPS_STORE": "memory"}):
                store = create_store(path=path)
                store.create_order(fixture())
            self.assertFalse(path.exists())
            self.assertFalse(store.metadata()["persistent"])
            register_store("synthetic_custom", lambda **kwargs: InMemoryOrderStore())
            self.assertIsInstance(create_store("synthetic_custom"), OrderStore)
            with self.assertRaises(ValueError):
                create_store("unregistered")
            with self.assertRaises(ValueError):
                register_store("sqlite", lambda: InMemoryOrderStore())


if __name__ == "__main__":
    unittest.main()
