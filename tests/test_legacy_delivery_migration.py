"""Old delivery evidence survives schema migration without authorizing reuse."""
from copy import deepcopy
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent import Agent, Memory
from order_model import normalize_state, STATE_SCHEMA_VERSION


def legacy_delivery():
    return {
        "schemaVersion": 2, "revision": 7,
        "order": {"productType": "名片", "quantity": "500张", "size": "B5",
                  "paper": "300g铜版纸", "printing": "双面四色", "deadline": "下周内"},
        "stage": "handoff", "workflowStage": "handoff", "orderStatus": "confirmed",
        "selectedOption": "standard", "processOptions": [{"id": "standard", "title": "旧方案"}],
        "orderGenerated": True,
        "confirmation": {"status": "confirmed", "confirmedAt": "2025-01-01T00:00:00Z"},
        "handoff": {"status": "ready", "text": "原件：B5 / 18.2×25.7cm",
                    "mappedOrder": {"size": "B5"}},
        "quoteRequests": [{"requestId": "preserved-request", "status": "quoted",
                           "mappedOrder": {"size": "18.2×25.7cm"},
                           "audit": [{"action": "draft_created"}]}],
        "activeQuoteRequestId": "preserved-request",
        "toolReceipts": [{"name": "prepare_handoff", "status": "ok", "stale": False,
                          "result": {"status": "ready", "text": "18.2×25.7cm"}}],
    }


class LegacyDeliveryMigrationTest(unittest.TestCase):
    def assert_stale_delivery(self, state):
        self.assertEqual(state["schemaVersion"], STATE_SCHEMA_VERSION)
        self.assertIsNone(state["handoff"])
        self.assertFalse(state["orderGenerated"])
        self.assertEqual(state["confirmation"], {"status": "stale", "staleReason": "order_schema_migrated"})
        self.assertIsNone(state["selectedOption"])
        self.assertFalse(state["processOptions"])
        self.assertFalse(state["itemOptions"])
        self.assertEqual(state["stage"], "collect")
        self.assertEqual(state["workflowStage"], "collect")
        self.assertIsNone(state["activeQuoteRequestId"])
        self.assertNotEqual(state.get("orderStatus"), "confirmed")

    def test_migration_preserves_exact_artifacts_and_invalidates_current_delivery(self):
        original = legacy_delivery()
        migrated = normalize_state(deepcopy(original))
        self.assert_stale_delivery(migrated)
        self.assertEqual(migrated["order"]["size"], "182×257 mm")
        quote = migrated["quoteRequests"][0]
        self.assertEqual(quote["requestId"], "preserved-request")
        self.assertEqual(quote["status"], "stale")
        self.assertEqual(quote["migrationPreviousStatus"], "quoted")
        self.assertEqual(quote["audit"], original["quoteRequests"][0]["audit"])
        self.assertTrue(migrated["toolReceipts"][0]["stale"])
        evidence = migrated["legacyDeliveryEvidence"][0]["original"]
        for key in ("order", "handoff", "confirmation", "quoteRequests", "selectedOption", "toolReceipts"):
            self.assertEqual(evidence[key], original[key])
        self.assertEqual(normalize_state(deepcopy(migrated)), migrated)
        self.assertEqual(len(migrated["legacyDeliveryEvidence"]), 1)
        migrated["quoteRequests"][0]["audit"].append({"action": "changed"})
        self.assertEqual(evidence["quoteRequests"], original["quoteRequests"])

    def test_item_only_or_confirmation_only_delivery_is_also_invalidated(self):
        for old in (
            {"schemaVersion": 2, "order": {"items": [
                {"itemId": "poster", "productType": "海报", "size": "B4", "selectedOption": "old", "orderGenerated": True},
                {"itemId": "card", "productType": "名片", "size": "90×54mm"}]}},
            {"schemaVersion": 2, "order": {}, "confirmation": {"status": "confirmed"}},
        ):
            with self.subTest(old=old):
                migrated = normalize_state(deepcopy(old))
                self.assert_stale_delivery(migrated)
                for item in migrated["order"]["items"]:
                    self.assertFalse(item.get("selectedOption"))
                    self.assertFalse(item.get("orderGenerated"))
                self.assertEqual(migrated["legacyDeliveryEvidence"][0]["original"]["order"], old["order"])
                self.assertEqual(normalize_state(deepcopy(migrated)), migrated)

    def test_sqlite_migration_and_restart_keep_original_backup_and_do_not_regenerate(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite3"
            original = legacy_delivery()
            encoded = json.dumps(original, ensure_ascii=False)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL)")
                db.execute("INSERT INTO sessions VALUES (?, ?)", ("legacy", encoded))
            memory = Memory(path)
            state = Agent(memory, "legacy").state
            self.assert_stale_delivery(state)
            self.assertEqual(state["revision"], 7)
            snapshot = Agent(memory, "legacy").snapshot()
            self.assertIsNone(snapshot["handoff"])
            self.assertFalse(snapshot["orderGenerated"])
            restored = Agent(Memory(path), "legacy").state
            self.assert_stale_delivery(restored)
            self.assertEqual(restored["legacyDeliveryEvidence"], state["legacyDeliveryEvidence"])
            self.assertEqual(restored["revision"], memory.store.load_order("legacy")["revision"])
            export = memory.store.export_order("legacy")
            self.assertEqual(export["quoteRequests"][0]["requestId"], "preserved-request")
            self.assertEqual(export["quoteRequests"][0]["status"], "stale")
            self.assertNotEqual(export["status"], "confirmed")
            with closing(sqlite3.connect(path)) as db, db:
                backup = db.execute("SELECT state FROM legacy_session_backups WHERE session_id = 'legacy'").fetchone()[0]
                self.assertEqual(backup, encoded)


if __name__ == "__main__":
    unittest.main()
