"""Business journeys for review, stable product targeting and supplier exports."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server
from agent import Agent, Memory
from nlu import perceive
from order_contract import handoff_document
from storage_memory import InMemoryOrderStore


COMPLETE_SINGLE = "做500张名片，90×54mm，300g白卡纸，双面四色，无特殊工艺，下周内"
MULTI = "做500张名片和1000张折页，A4，157g哑粉纸，双面四色，下周内"


class ProductJourneyApiTest(unittest.TestCase):
    def setUp(self):
        self.memory = Memory(store=InMemoryOrderStore())
        self.patcher = patch.object(server, "MEMORY", self.memory)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.agent = Agent(self.memory)
        self.agent.chat(COMPLETE_SINGLE)

    def post(self, path, **body):
        agent = Agent(self.memory, self.agent.id)
        body.setdefault("sessionId", self.agent.id)
        body.setdefault("expectedRevision", agent.state["revision"])
        return server.dispatch_post(path, body, agent)

    def state(self):
        return deepcopy(self.memory.load(self.agent.id))

    def persisted_snapshot(self):
        return {"record": self.memory.store.load_order(self.agent.id),
                "events": self.memory.store.load_events(self.agent.id)}

    def ready(self):
        self.post("/api/choose", optionId="economy")
        return self.post("/api/generate")

    def multi(self):
        self.agent = Agent(self.memory)
        return self.agent.chat(MULTI)

    def field(self, result, name, item_id=None):
        return next(f for f in result["review"]["fields"] if f["fieldPath"] == name and f["itemId"] == item_id)

    def test_focus_does_not_write_or_turn_context_switch_into_chat(self):
        initial = self.multi()
        before = self.persisted_snapshot()
        result = self.post("/api/focus", itemId="item-2")
        self.assertEqual(result["activeItemId"], "item-2")
        self.assertEqual(result["activeItemIndex"], 1)
        self.assertEqual(result["revision"], initial["revision"])
        self.assertEqual(result["messages"], [])
        self.assertEqual(self.persisted_snapshot(), before)
        order_context = self.post("/api/focus", itemId=None)
        self.assertIsNone(order_context["activeItemId"])
        self.assertEqual(order_context["revision"], initial["revision"])

    def test_focus_rechecks_revision_when_persistence_changes_after_target_lookup(self):
        self.multi()
        stale_agent = Agent(self.memory, self.agent.id)
        self.post("/api/fields", action="set", itemId="item-2", values={"quantity": "1200张"})
        with self.assertRaises(server.RequestError) as error:
            server.focus_snapshot(stale_agent, {"itemId": "item-2", "expectedRevision": stale_agent.state["revision"]})
        self.assertEqual(error.exception.code, "REVISION_CONFLICT")

    def test_id_target_survives_item_reorder_and_review_ids_remain_stable(self):
        initial = self.multi()
        issue_ids = {i["id"] for i in initial["review"]["issues"] if i["itemId"] == "item-2"}
        agent = Agent(self.memory, self.agent.id)
        agent.state["order"]["items"].reverse()
        agent._save()
        result = self.post("/api/fields", action="set", itemId="item-2", values={"quantity": "1200张"})
        self.assertEqual(result["order"]["items"][0]["itemId"], "item-2")
        self.assertEqual(result["order"]["items"][0]["quantity"], "1200 张")
        self.assertEqual(result["order"]["items"][1]["quantity"], "500 张")
        current_ids = {i["id"] for i in result["review"]["issues"] if i["itemId"] == "item-2"}
        self.assertEqual(issue_ids, current_ids)
        self.assertEqual(self.field(result, "quantity", "item-2")["fieldId"], "items.item-2.quantity")

    def test_ambiguous_stale_and_mismatched_targets_never_write(self):
        self.multi()
        before = self.persisted_snapshot()
        cases = [({"itemId": "item-2", "itemIndex": 0}, "ITEM_TARGET_CONFLICT"),
                 ({"itemId": "gone"}, "ITEM_NOT_FOUND"),
                 ({"itemIndex": True}, "INVALID_ITEM"),
                 ({"itemId": "item-2", "expectedRevision": 0}, "REVISION_CONFLICT"),
                 ({}, "INVALID_FIELD")]
        for body, code in cases:
            with self.subTest(code=code), self.assertRaises(server.RequestError) as error:
                self.post("/api/fields", action="set", values={"quantity": "999张"}, **body)
            self.assertEqual(error.exception.code, code)
        with self.assertRaises(server.RequestError) as error:
            server.dispatch_post("/api/chat", {"itemId": "item-2", "text": "999张"}, Agent(self.memory, self.agent.id))
        self.assertEqual(error.exception.code, "REVISION_REQUIRED")
        self.assertEqual(before, self.persisted_snapshot())

    def test_candidate_is_not_production_and_explicit_confirm_retains_evidence(self):
        self.agent = Agent(self.memory)
        initial = self.agent.chat("大约1000张名片，90×54mm，300g白卡纸，双面四色，下周内")
        field = self.field(initial, "quantity")
        self.assertEqual(field["status"], "pending_confirmation")
        self.assertIsNone(field["value"])
        self.assertEqual(field["candidateValue"], "1000 张")
        self.assertFalse(initial["handoffDocument"]["exportable"])
        self.assertIsNone(next(f["value"] for f in initial["handoffDocument"]["items"][0]["fields"] if f["key"] == "quantity"))
        before_messages = self.state()["messages"]
        result = self.post("/api/fields", action="confirm", fields=["quantity"])
        self.assertEqual(self.field(result, "quantity")["status"], "confirmed")
        self.assertEqual(result["fieldMeta"]["quantity"]["source"], "user")
        self.assertEqual(result["fieldMeta"]["quantity"]["confidence"], 1)
        self.assertEqual(result["history"], before_messages)
        candidates = [c for c in result["fieldCandidates"] if c["field"] == "quantity"]
        self.assertTrue(candidates)
        self.assertTrue(all(c["status"] == "confirmed" and c["rawText"] for c in candidates))
        restored = server.order_history_snapshot(self.memory, self.agent.id)
        self.assertEqual(self.field(restored, "quantity")["status"], "confirmed")

    def test_reject_and_clear_remove_all_quantity_and_size_aliases_after_reload(self):
        self.post("/api/fields", action="reject", fields=["quantity"])
        result = self.post("/api/fields", action="clear", fields=["dimensions.finishedSize"])
        restored = server.order_history_snapshot(self.memory, self.agent.id)
        self.assertIsNone(restored["order"]["quantityValue"])
        self.assertFalse(restored["order"]["quantityUnit"])
        self.assertFalse(restored["order"]["quantity"])
        self.assertFalse(restored["order"]["size"])
        self.assertFalse(restored["order"]["dimensions"]["finishedSize"])
        self.assertEqual(self.field(restored, "quantity")["status"], "missing")
        self.assertTrue(any(i["blocking"] and i["fieldPath"] == "size" for i in restored["review"]["issues"]))
        self.assertFalse(result["review"]["canGenerate"])
        self.assertTrue(all(c["status"] == "rejected" for c in restored["fieldCandidates"] if c["field"] == "quantity"))

    def test_mixed_invalid_batch_is_atomic_and_field_change_supports_cas_undo(self):
        before = self.persisted_snapshot()
        with self.assertRaises(server.RequestError):
            self.post("/api/fields", action="set", values={"quantity": "999张", "productionApproval": True})
        for invalid in ("未知", "0张", "-50张"):
            with self.subTest(quantity=invalid), self.assertRaises(server.RequestError):
                self.post("/api/fields", action="set", values={"paper": "铜版纸", "quantity": invalid})
        self.assertEqual(before, self.persisted_snapshot())
        result = self.post("/api/fields", action="set", values={"quantity": "600张"})
        change = result["fieldChanges"][0]
        self.assertEqual(change["before"], "500 张")
        self.assertEqual(change["after"], "600 张")
        undone = self.post("/api/fields", action="set", values={"quantity": change["before"]}, expectedRevision=result["revision"])
        self.assertEqual(undone["order"]["quantity"], "500 张")
        with self.assertRaises(server.RequestError):
            self.post("/api/fields", action="set", values={"quantity": change["after"]}, expectedRevision=result["revision"])

    def test_receipt_ledger_survives_sqlite_restart_without_snapshot_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.sqlite3"
            self.memory = Memory(path)
            self.agent = Agent(self.memory)
            self.agent.chat(COMPLETE_SINGLE)
            first = self.post("/api/fields", action="set", values={"quantity": "600张"}, operationId="durable-1")
            agent = Agent(self.memory, self.agent.id)
            agent.state["operationReceipts"] = []
            agent._save()
            self.memory = Memory(path)
            before = self.persisted_snapshot()
            retry = self.post("/api/fields", action="set", values={"quantity": "600张"}, operationId="durable-1")
            self.assertTrue(retry["duplicateOperation"])
            self.assertEqual(retry["operationReceipt"], first["operationReceipt"])
            self.assertEqual(before, self.persisted_snapshot())
    def test_pending_status_alone_and_quantity_alias_uncertainty_block_production(self):
        self.post("/api/choose", optionId="economy")
        agent = Agent(self.memory, self.agent.id)
        agent.state["fieldMeta"]["quantityValue"] = {"value": 500, "confidence": 1, "status": "pending_confirmation", "source": "model"}
        agent._save()
        result = self.post("/api/generate")
        self.assertFalse(result["orderGenerated"])
        self.assertEqual(self.field(result, "quantity")["status"], "pending_confirmation")
        self.assertFalse(result["review"]["canGenerate"])

    def test_handoff_draft_confirm_restart_invalidate_and_read_only_exports(self):
        draft = self.ready()
        self.assertEqual(draft["handoffDocument"]["status"], "draft")
        self.assertTrue(draft["handoffDocument"]["exportable"])
        self.assertEqual(draft["handoffDocument"]["confirmation"]["status"], "unconfirmed")
        self.assertEqual(draft["handoffDocument"]["items"][0]["file"]["status"], "pending_delivery")
        result = self.post("/api/confirm")
        document = result["handoffDocument"]
        self.assertEqual(document["status"], "confirmed")
        self.assertEqual(document["confirmedRevision"], result["revision"])
        self.assertTrue(document["confirmedAt"])
        self.assertEqual(document["unitSystem"], "mm")
        self.assertIs(document["sent"], False)
        before = self.persisted_snapshot()
        exported = server.dispatch_get(f"/api/orders/{self.agent.id}/handoff?revision={result['revision']}")
        self.assertEqual(exported, document)
        self.assertEqual(before, self.persisted_snapshot())
        self.assertNotIn("messages", exported)
        self.assertEqual(exported["items"][0]["selectedOptionDetails"]["title"], "经济方案")
        self.assertIn("| 字段 | 值 | 状态 |\n| --- | --- | --- |\n|", exported["markdown"])
        self.assertNotIn("pending_delivery", exported["markdown"])
        self.assertNotIn("| filled |", exported["markdown"])
        edited = self.post("/api/fields", action="set", values={"quantity": "600张"})
        self.assertEqual(edited["handoffDocument"]["status"], "stale")
        self.assertFalse(edited["handoffDocument"]["exportable"])
        with self.assertRaises(server.RequestError) as error:
            server.dispatch_get(f"/api/orders/{self.agent.id}/handoff?revision={result['revision']}")
        self.assertEqual(error.exception.code, "REVISION_CONFLICT")

    def test_fingerprint_detects_untracked_data_change(self):
        self.ready()
        state = self.state()
        state["order"]["quantity"] = "999 张"
        document = handoff_document(state, self.agent.id)
        self.assertEqual(document["status"], "stale")
        self.assertFalse(document["exportable"])

    def test_handoff_coalesces_equal_aliases_and_preserves_conflicting_values(self):
        agent = Agent(self.memory)
        result = agent.chat("做100本宣传册，成品210×297mm，32页，157g铜版纸，双面四色，骑马钉，下周内")
        document = result["handoffDocument"]
        fields = document["items"][0]["fields"]
        self.assertEqual(len([field for field in fields if field["key"] in {"size", "dimensions.finishedSize"}]), 1)
        self.assertEqual(len([field for field in fields if field["key"] in {"pages", "productSpecs.pages"}]), 1)
        self.assertEqual(len([field for field in fields if field["key"] in {"binding", "productSpecs.binding"}]), 1)
        self.assertIn("dimensions.finishedSize", {field["key"] for field in fields})
        self.assertNotIn("文件待交付 · 文件待交付", document["markdown"])
        self.assertEqual(document["markdown"].count("尚未发送"), 1)
        agent.state["order"]["productSpecs"]["pages"] = "64页"
        conflict = handoff_document(agent.state, agent.id, agent.review())
        conflicting = [field for field in conflict["items"][0]["fields"] if field.get("semanticConflict")]
        self.assertEqual({field["value"] for field in conflicting}, {"32 页", "64页"})
        self.assertTrue(any(issue["blocking"] and "semantic_alias_conflict" in issue["id"] for issue in conflict["issues"]))
        self.assertFalse(conflict["exportable"])

    def test_multi_product_targeted_chat_tools_preflight_and_export_share_identity(self):
        self.multi()
        result = self.post("/api/chat", itemId="item-2", text="补充第二项", patch={"productSpecs": {"folding": "三折"}})
        self.assertEqual(result["requestContext"]["itemId"], "item-2")
        self.post("/api/fields", action="set", itemId="item-1",
                  values={"size": "90×54mm", "paper": "300g白卡纸", "printing": "双面四色", "deadline": "下周内"})
        self.post("/api/choose", itemId="item-1", optionId="economy")
        self.post("/api/choose", itemId="item-2", optionId="economy")
        result = self.post("/api/tools/call", toolName="validate_order", payload={"itemId": "item-2"})
        self.assertEqual(result["requestContext"]["itemIndex"], 1)
        checked = self.post("/api/preflight", itemId="item-2", fileName="fold.pdf", sizeBytes=1024,
                            pageCount=1, encrypted=False, readable=True)
        self.assertEqual(checked["order"]["items"][1]["uploadedFile"], "fold.pdf")
        document = self.post("/api/generate")["handoffDocument"]
        self.assertEqual(document["status"], "draft")
        self.assertTrue(document["exportable"])
        first, second = document["items"]
        self.assertEqual(first["file"]["status"], "pending_delivery")
        self.assertEqual(second["file"]["fileName"], "fold.pdf")
        self.assertEqual(second["file"]["itemId"], "item-2")
        self.assertIn("未提供专业 RIP", second["file"]["limitations"])
        edited = self.post("/api/fields", action="set", itemId="item-2", values={"dimensions.finishedSize": "148×210mm"})
        self.assertEqual(edited["handoffDocument"]["items"][1]["file"]["status"], "stale")
        supplier_notes = [i for i in edited["review"]["issues"] if "supplier_unverified" in i["id"]]
        self.assertEqual(len(supplier_notes), 1)
        self.assertFalse(supplier_notes[0]["blocking"])

    def test_lost_response_retry_does_not_repeat_chat_or_save_and_conflicts_are_explicit(self):
        body = {"text": "数量改成600张", "operationId": "lost-chat-1"}
        initial = self.post("/api/chat", **body)
        before = self.persisted_snapshot()
        retried = self.post("/api/chat", expectedRevision=0, **body)
        self.assertTrue(retried["duplicateOperation"])
        self.assertEqual(retried["revision"], initial["revision"])
        self.assertEqual(retried["operationReceipt"]["status"], "completed")
        self.assertFalse(retried["needsReconciliation"])
        self.assertEqual(before, self.persisted_snapshot())
        with self.assertRaises(server.RequestError) as error:
            self.post("/api/chat", text="改成700张", operationId="lost-chat-1")
        self.assertEqual(error.exception.code, "OPERATION_CONFLICT")

    def test_lost_confirmation_response_reuses_receipt_and_exact_confirmation(self):
        self.ready()
        result = self.post("/api/confirm", operationId="confirm-1")
        before = self.persisted_snapshot()
        retried = self.post("/api/confirm", operationId="confirm-1", expectedRevision=0)
        self.assertEqual(retried["confirmation"], result["confirmation"])
        self.assertEqual(retried["handoffDocument"], result["handoffDocument"])
        self.assertEqual(before, self.persisted_snapshot())

    def test_partial_commit_retries_require_reconciliation_instead_of_repeating(self):
        agent = Agent(self.memory, self.agent.id)
        body = {"sessionId": self.agent.id, "expectedRevision": agent.state["revision"], "text": "数量改为600张", "operationId": "partial-1"}
        with patch.object(agent, "_result", side_effect=RuntimeError("response interrupted")):
            with self.assertRaises(RuntimeError):
                server.dispatch_post("/api/chat", body, agent)
        before = self.persisted_snapshot()
        retried = self.post("/api/chat", text=body["text"], operationId="partial-1")
        self.assertTrue(retried["needsReconciliation"])
        self.assertEqual(retried["operationReceipt"]["status"], "running")
        self.assertEqual(before, self.persisted_snapshot())

    def test_bare_negation_does_not_turn_no_lamination_into_lamination(self):
        example = "做单页，数量确定500张，成品210×297mm，157g铜版纸，双面四色，下周内，不覆膜。"
        self.assertEqual(perceive(example)[0]["finishing"], "无特殊工艺")
        self.assertEqual(perceive("名片不覆膜，改烫金")[0]["finishing"], "烫金")
        self.assertEqual(perceive("名片覆膜")[0]["finishing"], "覆膜")

    def test_receipt_cache_eviction_never_replays_old_completed_or_running_operations(self):
        first = self.post("/api/fields", action="set", values={"quantity": "600张"}, operationId="old-completed")
        agent = Agent(self.memory, self.agent.id)
        body = {"sessionId": self.agent.id, "text": "数量改为700张", "operationId": "old-running"}
        with patch.object(agent, "_result", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                server.dispatch_post("/api/chat", body, agent)
        for index in range(101):
            self.post("/api/fields", action="set", values={"quantity": f"{800 + index}张"}, operationId=f"later-{index}")
        ids = {receipt["operationId"] for receipt in self.state()["operationReceipts"]}
        self.assertNotIn("old-completed", ids)
        self.assertIn("old-running", ids)
        before = self.persisted_snapshot()
        retry = self.post("/api/fields", action="set", values={"quantity": "600张"}, operationId="old-completed")
        self.assertTrue(retry["duplicateOperation"])
        self.assertEqual(retry["operationReceipt"], first["operationReceipt"])
        self.assertEqual(retry["order"]["quantity"], "900 张")
        partial = self.post("/api/chat", text=body["text"], operationId="old-running")
        self.assertTrue(partial["needsReconciliation"])
        self.assertEqual(before, self.persisted_snapshot())


if __name__ == "__main__":
    unittest.main()
