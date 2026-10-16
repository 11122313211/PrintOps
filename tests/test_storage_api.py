"""Backend history API and Memory compatibility tests using synthetic orders."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent import Agent, Memory, RevisionConflictError
from storage_memory import InMemoryOrderStore
import server


class OrderHistoryAPITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory = Memory(Path(self.tmp.name) / "history.sqlite3")
        self.memory_patch = patch.object(server, "MEMORY", self.memory)
        self.memory_patch.start()
        self.addCleanup(self.memory_patch.stop)

    def seed(self, order_id="synthetic-order", quantity="20 张"):
        state = Memory.fresh_state()
        state["order"].update({"productType": "名片", "quantity": quantity})
        self.memory.save(order_id, state)
        return self.memory.store.load_order(order_id)

    def test_list_load_and_canonical_export_use_one_backend_revision(self):
        record = self.seed()
        listed = server.dispatch_get("/api/orders?limit=1&offset=0")
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["orders"][0]["orderId"], record["orderId"])
        loaded = server.dispatch_get("/api/orders/synthetic-order")
        self.assertEqual(loaded["order"]["quantity"], "20 张")
        self.assertEqual(loaded["canonicalOrder"]["unitSystem"], "mm")
        self.assertEqual(loaded["revision"], record["revision"])
        self.assertEqual(self.memory.store.load_order(record["orderId"])["revision"], record["revision"])
        exported = server.dispatch_get("/api/orders/synthetic-order/export")
        self.assertEqual(exported["state"]["order"]["quantity"], "20 张")
        self.assertIn("canonicalOrder", exported)
        self.assertIn("events", exported)
        self.assertEqual(exported["auditLog"][-1]["action"], "exported")

    def test_archival_requires_current_revision_and_keeps_readonly_snapshot(self):
        record = self.seed()
        with self.assertRaises(server.RequestError) as missing:
            server.dispatch_history_post("/api/orders/synthetic-order/archive", {}, self.memory)
        self.assertEqual(missing.exception.code, "INVALID_REVISION")
        with self.assertRaises(server.RequestError) as stale:
            server.dispatch_history_post("/api/orders/synthetic-order/archive", {"expectedRevision": 9}, self.memory)
        self.assertEqual(stale.exception.status, 409)
        archived = server.dispatch_history_post("/api/orders/synthetic-order/archive", {"expectedRevision": record["revision"]}, self.memory)
        self.assertEqual(archived["status"], "archived")
        self.assertEqual(server.dispatch_get("/api/orders")["total"], 0)
        self.assertEqual(server.dispatch_get("/api/orders?includeArchived=true")["total"], 1)
        agent = Agent(self.memory, "synthetic-order")
        self.assertEqual(server.dispatch_post("/api/session", {}, agent)["status"], "archived")
        with self.assertRaises(server.RequestError) as readonly:
            server.dispatch_post("/api/chat", {"text": "数量改为 30 张"}, agent)
        self.assertEqual(readonly.exception.code, "ORDER_ARCHIVED")
        with self.assertRaises(server.RequestError) as missing_confirm:
            server.dispatch_history_post("/api/orders/synthetic-order/purge", {"expectedRevision": archived["revision"]}, self.memory)
        self.assertEqual(missing_confirm.exception.code, "CONFIRM_REQUIRED")
        purged = server.dispatch_history_post("/api/orders/synthetic-order/purge", {"expectedRevision": archived["revision"], "confirm": True}, self.memory)
        self.assertEqual(purged, {"orderId": "synthetic-order", "deleted": True})
        self.assertEqual(server.dispatch_get("/api/orders?includeArchived=true")["total"], 0)

    def test_browser_migration_never_overwrites_backend_or_imports_approval(self):
        self.seed()
        legacy = [
            {"id": "synthetic-order", "order": {"quantity": "999 张"}},
            {"id": "browser-only", "order": {"productType": "名片", "quantity": "12 张"}, "orderGenerated": True,
             "confirmation": {"status": "confirmed"}},
            {"id": "bad id", "order": {}},
        ]
        result = server.dispatch_history_post("/api/orders/import-local-history", {"orders": legacy}, self.memory)
        self.assertEqual(result["imported"], ["browser-only"])
        self.assertEqual(result["skipped"], ["synthetic-order"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(self.memory.load("synthetic-order")["order"]["quantity"], "20 张")
        imported = self.memory.load("browser-only")
        self.assertFalse(imported["orderGenerated"])
        self.assertNotEqual(imported["confirmation"]["status"], "confirmed")
        source_event = self.memory.store.load_events("browser-only")[-1]
        self.assertEqual(source_event["payload"]["original"], legacy[1])
        retry = server.dispatch_history_post("/api/orders/import-local-history", {"orders": legacy[:2]}, self.memory)
        self.assertEqual(retry["imported"], [])
        self.assertEqual(len(retry["skipped"]), 2)

    def test_unknown_order_is_not_silently_created(self):
        with self.assertRaises(server.RequestError) as unknown:
            server.dispatch_get("/api/orders/missing")
        self.assertEqual(unknown.exception.status, 404)
        self.assertEqual(self.memory.store.list_orders()["total"], 0)
        with self.assertRaises(server.RequestError):
            server.dispatch_get("/api/orders?limit=oops")

    def test_http_expected_revision_is_checked_before_mutation(self):
        self.seed()
        agent = Agent(self.memory, "synthetic-order")
        before = deepcopy(agent.state)
        with self.assertRaises(server.RequestError) as stale:
            server.dispatch_post("/api/chat", {"expectedRevision": 77, "text": "数量改为 30 张"}, agent)
        self.assertEqual(stale.exception.status, 409)
        self.assertEqual(before, agent.state)

    def test_session_restore_is_readonly_and_preserves_the_cas_revision(self):
        agent = Agent(self.memory, "browser-refresh")
        created = server.dispatch_post("/api/session", {}, agent)
        changed = server.dispatch_post("/api/chat", {
            "expectedRevision": created["revision"], "text": "合成测试：500张名片",
        }, agent)
        before = self.memory.store.load_order(agent.id)
        events = self.memory.store.load_events(agent.id)
        with self.memory._db() as db:
            audit_before = list(db.execute("SELECT * FROM audit_log"))
        with patch.object(Agent, "_call", side_effect=AssertionError("restore must not invoke tools")), \
             patch.object(self.memory, "save_if_revision", side_effect=AssertionError("restore must not save")):
            for _ in range(2):
                restored = server.dispatch_post("/api/session", {"sessionId": agent.id}, Agent(self.memory, agent.id))
                self.assertEqual(restored["revision"], changed["revision"])
                self.assertEqual(restored["history"], before["state"]["messages"])
                self.assertEqual(restored["order"], changed["order"])
        self.assertEqual(self.memory.store.load_order(agent.id), before)
        self.assertEqual(self.memory.store.load_events(agent.id), events)
        with self.memory._db() as db:
            self.assertEqual(list(db.execute("SELECT * FROM audit_log")), audit_before)
        updated = server.dispatch_post("/api/chat", {
            "expectedRevision": changed["revision"], "text": "数量改为600张",
        }, Agent(self.memory, agent.id))
        self.assertEqual(updated["order"]["quantity"], "600 张")

    def test_memory_backend_works_through_existing_agent_facade(self):
        memory = Memory(store=InMemoryOrderStore())
        agent = Agent(memory, "ephemeral")
        agent._remember("user", "synthetic exact message")
        agent._save()
        restored = Agent(memory, "ephemeral")
        self.assertEqual(restored.state["messages"], [{"role": "user", "text": "synthetic exact message"}])
        with patch.object(server, "MEMORY", memory):
            self.assertFalse(server.dispatch_get("/api/storage")["persistent"])
            self.assertEqual(server.dispatch_get("/api/orders")["total"], 1)

    def test_stale_agents_cannot_overwrite_the_same_revision(self):
        self.seed()
        first = Agent(self.memory, "synthetic-order")
        second = Agent(self.memory, "synthetic-order")
        first._remember("user", "first synthetic write")
        first._save()
        second._remember("user", "stale synthetic write")
        with self.assertRaises(RevisionConflictError):
            second._save()
        exported = self.memory.store.export_order("synthetic-order")
        self.assertEqual([m["text"] for m in exported["messages"]], ["first synthetic write"])


if __name__ == "__main__":
    unittest.main()
