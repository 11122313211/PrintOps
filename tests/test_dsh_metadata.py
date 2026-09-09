"""Focused contracts for dsh-style patch provenance and spec allowlists."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent import Agent, Memory
from order_model import normalize_state, parse_quantity
from llm_adapter import OpenAICompatiblePlanner


class DshMetadataTest(unittest.TestCase):
    def test_state_migration_normalizes_rejected_fields(self) -> None:
        state = {"order": {}, "rejectedFields": ["  paper ", "paper", 7, ""]}
        normalized = normalize_state(state)
        self.assertEqual(normalized["rejectedFields"], ["paper"])

    def test_state_migration_drops_complex_legacy_spec_values(self) -> None:
        normalized = normalize_state({"order": {
            "productType": "包装盒",
            "size": {"unexpected": "object"},
            "dimensions": {"packageSize": [60, 40, 20]},
            "productSpecs": {
                "boxSize": {"length": 60},
                "boxStructure": ["天地盖"],
                "boxSizeOuter": "65×45×25CM",
                "safe": 7,
            },
        }})
        order = normalized["order"]
        self.assertEqual(order["size"], "")
        self.assertEqual(order["dimensions"]["packageSize"], "")
        self.assertEqual(order["productSpecs"], {
            "boxSizeOuter": "65×45×25CM", "safe": "7",
        })

    def test_quantity_parser_rejects_containers_and_nonfinite_values(self) -> None:
        self.assertIsNone(parse_quantity({"count": 500}, "名片"))
        self.assertIsNone(parse_quantity([500], "名片"))
        self.assertIsNone(parse_quantity(float("nan"), "名片"))
        self.assertIsNone(parse_quantity(True, "名片"))

    def test_state_migration_clears_complex_order_and_item_scalars(self) -> None:
        normalized = normalize_state({
            "order": {
                "productType": {"value": "名片"},
                "quantity": {"count": 500},
                "quantityValue": {"value": 500},
                "paper": ["250g"],
                "platform": {"id": "shengda"},
                "items": [{
                    "itemId": {"id": "item-1"},
                    "productType": {"value": "名片"},
                    "quantity": {"count": 100},
                    "paper": ["250g"],
                    "selectedOption": {"id": "balanced"},
                    "uploadedFile": ["art.pdf"],
                    "orderGenerated": "yes",
                }],
            },
        })
        order = normalized["order"]
        self.assertEqual(order["productType"], "")
        self.assertEqual(order["quantity"], "")
        self.assertIsNone(order["quantityValue"])
        self.assertEqual(order["paper"], "")
        self.assertEqual(order["platform"], "generic")
        self.assertEqual(len(order["items"]), 1)
        item = order["items"][0]
        self.assertEqual(item["itemId"], "item-1")
        self.assertEqual(item["productType"], "")
        self.assertEqual(item["quantity"], "")
        self.assertEqual(item["paper"], "")
        self.assertIsNone(item["selectedOption"])
        self.assertIsNone(item["uploadedFile"])
        self.assertFalse(item["orderGenerated"])

    def test_state_migration_fails_closed_for_malformed_top_level_metadata(self) -> None:
        normalized = normalize_state({
            "order": {"productType": "名片", "quantity": "500 张", "size": "A4"},
            "messages": {"role": "user", "text": {"secret": True}},
            "stage": {"name": "confirm"},
            "selectedOption": {"id": "balanced"},
            "orderGenerated": {"value": True},
            "uploadedFile": {"path": "/private/secret.pdf"},
            "uploadedFiles": {"item-1": {"fileName": "secret.pdf"}},
            "handoff": ["leak"],
            "confirmation": {"status": {"value": "confirmed"}, "note": {"secret": True}},
            "fieldMeta": {"size": {"value": {"secret": True}},
                          "quantity": {"value": "500 张", "confidence": 0.9}},
            "conflicts": {"field": "size"},
            "lastRun": ["leak"],
            "runHistory": {"run": "leak"},
            "workflowStage": ["confirm"],
            "activeItemIndex": {"value": 0},
            "itemOptions": {"item-1": {"id": "balanced"}},
            "quoteRequests": {"requestId": "quote-leak"},
            "activeQuoteRequestId": {"requestId": "quote-leak"},
            "planMeta": {"questions": {"secret": True}},
            "schemaVersion": {"value": 999},
        })

        self.assertEqual(normalized["messages"], [])
        self.assertEqual(normalized["stage"], "collect")
        self.assertIsNone(normalized["selectedOption"])
        self.assertFalse(normalized["orderGenerated"])
        self.assertIsNone(normalized["uploadedFile"])
        self.assertEqual(normalized["uploadedFiles"], [])
        self.assertIsNone(normalized["handoff"])
        self.assertEqual(normalized["confirmation"], {"status": "not_ready"})
        self.assertEqual(normalized["fieldMeta"]["quantity"]["value"], "500 张")
        self.assertNotIn("size", normalized["fieldMeta"])
        self.assertEqual(normalized["conflicts"], [])
        self.assertIsNone(normalized["lastRun"])
        self.assertEqual(normalized["runHistory"], [])
        self.assertEqual(normalized["workflowStage"], "collect")
        self.assertIsNone(normalized["activeItemIndex"])
        self.assertEqual(normalized["itemOptions"], {})
        self.assertEqual(normalized["quoteRequests"], [])
        self.assertIsNone(normalized["activeQuoteRequestId"])
        self.assertEqual(normalized["planMeta"], {"questions": [], "risks": [], "knowledgeVersion": ""})
        self.assertEqual(normalized["schemaVersion"], 2)

    def test_state_migration_preserves_valid_top_level_records(self) -> None:
        normalized = normalize_state({
            "order": {"productType": "名片", "quantity": "500 张", "size": "A4"},
            "messages": [{"role": "user", "text": "做名片", "ignored": {"nested": True}}],
            "stage": "recommend",
            "selectedOption": " balanced ",
            "orderGenerated": True,
            "uploadedFile": " art.pdf ",
            "uploadedFiles": [{"itemId": None, "itemIndex": None, "fileName": " art.pdf ", "ignored": []}],
            "handoff": {"status": "ready", "text": "交接文本", "mappedOrder": {"quantity": "500 张"}},
            "confirmation": {"status": "pending", "note": "请人工确认", "ignored": {"drop": True}},
            "fieldMeta": {"size": {"value": "A4", "source": "user", "confidence": 1}},
            "conflicts": [{"field": "size", "previous": "A4", "current": "B4", "source": "user"}],
            "lastRun": {"runId": "run-1", "status": "completed", "events": [{"step": "chat"}]},
            "runHistory": [{"runId": "run-1", "status": "completed", "events": []}],
            "workflowStage": "recommend",
            "itemOptions": {"item-1": [{"id": "balanced", "title": "平衡方案", "ignored": {"x": 1}}]},
            "quoteRequests": [{"requestId": "quote-1", "status": "awaiting_human_confirmation",
                               "mappedOrder": {"quantity": "500 张"}}],
            "activeQuoteRequestId": " quote-1 ",
        })

        self.assertEqual(normalized["messages"], [{"role": "user", "text": "做名片"}])
        self.assertEqual(normalized["stage"], "recommend")
        self.assertEqual(normalized["selectedOption"], "balanced")
        self.assertTrue(normalized["orderGenerated"])
        self.assertEqual(normalized["uploadedFile"], "art.pdf")
        self.assertEqual(normalized["uploadedFiles"], [{"itemId": None, "itemIndex": None, "fileName": "art.pdf"}])
        self.assertEqual(normalized["handoff"]["status"], "ready")
        self.assertEqual(normalized["confirmation"], {"status": "pending", "note": "请人工确认"})
        self.assertEqual(normalized["fieldMeta"]["size"]["confidence"], 1.0)
        self.assertEqual(normalized["conflicts"][0]["previous"], "A4")
        self.assertEqual(normalized["lastRun"]["runId"], "run-1")
        self.assertEqual(normalized["runHistory"][0]["runId"], "run-1")
        self.assertEqual(normalized["itemOptions"]["item-1"][0]["id"], "balanced")
        self.assertEqual(normalized["quoteRequests"][0]["requestId"], "quote-1")
        self.assertEqual(normalized["activeQuoteRequestId"], "quote-1")

    def test_patch_gateway_rejects_complex_values_without_stringifying(self) -> None:
        with TemporaryDirectory() as tmp:
            agent = Agent(Memory(Path(tmp) / "agent.sqlite3"))
            agent._update_order({
                "productType": {"value": "名片"},
                "quantity": {"value": 500},
                "dimensions": ["A4"],
                "paper": {"value": "250g"},
            }, source="model", confidence=0.68)
        order = agent.state["order"]
        self.assertEqual(order["productType"], "")
        self.assertEqual(order["quantity"], "")
        self.assertEqual(order["paper"], "")
        self.assertEqual(order["dimensions"]["finishedSize"], "")
        self.assertIn("productType", agent.state["rejectedFields"])
        self.assertIn("quantity", agent.state["rejectedFields"])
        self.assertIn("dimensions", agent.state["rejectedFields"])

    def test_patch_boundaries_reject_non_string_keys_without_crashing(self) -> None:
        class WeirdPatch(dict):
            """Expose an adversarial mapping key that JSON cannot encode."""

            def items(self):
                return [([], "ignored"), ("quantity", "500")]

            def __iter__(self):
                return iter([[], "quantity"])

        plan = OpenAICompatiblePlanner.validate_plan({
            "reply": "已处理",
            "patch": WeirdPatch(),
        }, set())
        self.assertEqual(plan["patch"], {"quantity": "500"})
        self.assertIn("<non-string-patch-key>", plan["rejectedFields"])

        class WeirdChanges:
            def __iter__(self):
                return iter([[], "quantity"])

            def items(self):
                return [([], "ignored"), ("quantity", "500")]

            def __contains__(self, key):
                return key == "quantity"

            def get(self, key, default=None):
                return "500" if key == "quantity" else default

        with TemporaryDirectory() as tmp:
            agent = Agent(Memory(Path(tmp) / "agent.sqlite3"))
            agent._update_order(WeirdChanges(), source="model", confidence=0.68)
        self.assertEqual(agent.state["order"]["quantity"], "500 份")
        self.assertIn("<non-string-patch-key>", agent.state["rejectedFields"])

    def test_malformed_confidence_fails_closed_during_generation_checks(self) -> None:
        with TemporaryDirectory() as tmp:
            agent = Agent(Memory(Path(tmp) / "agent.sqlite3"))
            agent._update_order({"size": "A4"}, source="model", confidence="not-a-number")
            self.assertIn("size", agent._low_confidence_fields(production_only=True))

    def test_planner_filters_unknown_and_complex_specs(self) -> None:
        plan = OpenAICompatiblePlanner.validate_plan({
            "reply": "已记录",
            "patch": {"productType": "名片", "productSpecs": {
                "cardStock": "300g 白卡", "unknown": "不要写入", "cardCorners": {"nested": True},
            }},
            "confidence": {"productSpecs.cardStock": 0.94,
                           "items.item-2.productSpecs.cardStock": 0.91,
                           "unknown": 0.99},
            "evidence": [{"field": "productSpecs.cardStock", "quote": "300g 白卡", "source": "user"},
                         {"field": "items.item-2.productSpecs.cardStock", "quote": "300g 白卡", "source": "user"}],
        }, set())
        self.assertEqual(plan["patch"]["productSpecs"], {"cardStock": "300g 白卡"})
        self.assertEqual(plan["rejectedFields"], ["productSpecs.unknown", "productSpecs.cardCorners"])
        self.assertEqual(plan["confidence"]["productSpecs.cardStock"], 0.94)
        self.assertIn("items.item-2.productSpecs.cardStock", plan["evidence"])

    def test_planner_accepts_canonical_dimensions_and_rejects_unknown_names(self) -> None:
        plan = OpenAICompatiblePlanner.validate_plan({
            "reply": "已记录尺寸",
            "patch": {"dimensions": {"finishedSize": "210×297MM", "unsafe": "x"}},
            "confidence": {"dimensions.finishedSize": 0.96},
            "evidence": {"dimensions.finishedSize": "A4"},
        }, set())
        self.assertEqual(plan["patch"], {"dimensions": {"finishedSize": "210×297MM"}})
        self.assertEqual(plan["rejectedFields"], ["dimensions.unsafe"])

    def test_planner_preserves_bounded_skill_annotations(self) -> None:
        plan = OpenAICompatiblePlanner.validate_plan({
            "questions": ["  是否需要出血？  ", {"field": "size", "question": "确认成品尺寸", "nested": {"drop": True}}],
            "risks": [{"severity": "review", "message": "需要人工印前复核", "ignored": [1, 2]}],
            "knowledgeVersion": "  printops-test  ",
            "reportedKnowledgeVersion": "reported-1",
        }, set())
        self.assertEqual(plan["questions"], ["是否需要出血？", {"field": "size", "question": "确认成品尺寸"}])
        self.assertEqual(plan["risks"], [{"severity": "review", "message": "需要人工印前复核"}])
        self.assertEqual(plan["knowledgeVersion"], "printops-test")
        self.assertEqual(plan["reportedKnowledgeVersion"], "reported-1")

    def test_planner_drops_malformed_skill_annotations(self) -> None:
        plan = OpenAICompatiblePlanner.validate_plan({
            "reply": "已检查",
            "questions": "not-a-list",
            "risks": [None, True, {"message": float("nan")}, {"message": "保留"}],
            "knowledgeVersion": {"version": "spoof"},
        }, set())
        self.assertEqual(plan["questions"], [])
        self.assertEqual(plan["risks"], [{"message": "保留"}])
        self.assertEqual(plan["knowledgeVersion"], "")

    def test_model_provenance_reaches_field_meta(self) -> None:
        class Planner:
            enabled = True
            model = "test"
            last_error = ""

            def plan(self, *_args, **_kwargs):
                return {
                    "reply": "已记录",
                    "patch": {"productType": "名片", "productSpecs": {"cardStock": "300g 白卡"}},
                    "confidence": {"productSpecs.cardStock": 0.94},
                    "evidence": [{"field": "productSpecs.cardStock", "quote": "300g 白卡", "source": "user"}],
                }

            def public_config(self):
                return {"enabled": True, "model": self.model}

        with TemporaryDirectory() as tmp:
            result = Agent(Memory(Path(tmp) / "agent.sqlite3"), planner=Planner()).chat("我要做名片")
        meta = result["fieldMeta"]["productSpecs.cardStock"]
        self.assertEqual(meta["confidence"], 0.94)
        self.assertEqual(meta["evidence"]["quote"], "300g 白卡")
        self.assertEqual(result["order"]["productSpecs"], {"cardStock": "300g 白卡"})

    def test_skill_annotations_are_persisted_as_advisory_plan_meta(self) -> None:
        class Planner:
            enabled = True
            model = "test"
            last_error = ""

            def plan(self, *_args, **_kwargs):
                return {
                    "reply": "还需要确认文件",
                    "patch": {},
                    "questions": ["是否有出血？", {"field": "size", "question": "确认成品尺寸"}],
                    "risks": [{"severity": "review", "message": "需要人工印前复核"}],
                    "knowledgeVersion": "test-knowledge",
                }

            def public_config(self):
                return {"enabled": True, "model": self.model}

        with TemporaryDirectory() as tmp:
            result = Agent(Memory(Path(tmp) / "agent.sqlite3"), planner=Planner()).chat("检查文件")
        self.assertEqual(result["planMeta"]["questions"][0], "是否有出血？")
        self.assertEqual(result["planMeta"]["questions"][1]["field"], "size")
        self.assertTrue(any(item.get("code") == "knowledge_version_mismatch"
                            for item in result["planMeta"]["risks"] if isinstance(item, dict)))

    def test_product_switch_keeps_only_new_profile_specs(self) -> None:
        with TemporaryDirectory() as tmp:
            agent = Agent(Memory(Path(tmp) / "agent.sqlite3"))
            agent._update_order({"productType": "名片", "productSpecs": {"cardStock": "300g"}},
                                source="user", confidence=1.0)
            agent._update_order({"productType": "包装盒", "productSpecs": {
                "boxStructure": "天地盖", "boxSize": "60×40×20CM", "cardStock": "旧字段",
            }}, source="user", confidence=1.0)
        self.assertEqual(agent.state["order"]["productSpecs"], {
            "boxStructure": "天地盖", "boxSize": "60×40×20CM",
        })
        self.assertIn("productSpecs.cardStock", agent.state["rejectedFields"])

    def test_item_prefix_provenance_is_scoped_to_selected_item(self) -> None:
        with TemporaryDirectory() as tmp:
            agent = Agent(Memory(Path(tmp) / "agent.sqlite3"))
            agent._update_order({"items": [
                {"itemId": "item-1", "productType": "名片"},
                {"itemId": "item-2", "productType": "折页"},
            ]}, source="user", confidence=1.0)
            agent._update_item(1, {"productSpecs": {"folding": "三折"}}, source="model", confidence=0.68,
                               field_confidence={"items.item-2.productSpecs.folding": 0.9},
                               field_evidence={"items.item-2.productSpecs.folding": {
                                   "quote": "三折", "source": "user"}})
        meta = agent.state["fieldMeta"]["items.item-2.productSpecs.folding"]
        self.assertEqual(meta["confidence"], 0.9)
        self.assertEqual(meta["evidence"]["quote"], "三折")
        self.assertNotIn("items.item-1.productSpecs.folding", agent.state["fieldMeta"])

    def test_duplicate_and_invalid_item_ids_are_repaired_idempotently(self) -> None:
        long_id = "x" * 129
        state = normalize_state({"order": {"items": [
            {"itemId": "dup", "productType": "名片"},
            {"itemId": "dup", "productType": "折页"},
            {"itemId": "", "productType": "标签"},
            {"itemId": long_id, "productType": "海报"},
            {"itemId": "item-5", "productType": "手提袋"},
        ]}})
        ids = [item["itemId"] for item in state["order"]["items"]]
        self.assertEqual(ids, ["dup", "item-2", "item-3", "item-4", "item-5"])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(0 < len(item_id) <= 128 for item_id in ids))
        self.assertEqual(state, normalize_state(state))

    def test_item_references_migrate_with_explicit_index_and_quote_stales(self) -> None:
        state = normalize_state({
            "order": {"items": [
                {"itemId": "dup", "productType": "名片"},
                {"itemId": "dup", "productType": "折页"},
            ]},
            "fieldMeta": {
                "items.dup.quantity": {"value": "500 张"},
                "items.1.quantity": {"value": "1000 张"},
            },
            "itemOptions": {"dup": [{"id": "balanced"}]},
            "uploadedFiles": [
                {"itemId": "dup", "itemIndex": 1, "fileName": "fold.pdf"},
                {"itemId": "missing", "itemIndex": 99, "fileName": "orphan.pdf"},
            ],
            "quoteRequests": [{
                "requestId": "quote-1", "status": "awaiting_human_confirmation",
                "itemId": "dup", "itemIndex": 1, "idempotencyKey": "old-key",
                "orderFingerprint": "old-fingerprint",
                "mappedOrder": {"itemId": "dup", "itemIndex": 1},
            }],
            "activeQuoteRequestId": "quote-1",
            "handoff": {"status": "ready", "text": "交接", "items": [
                {"itemId": "dup", "itemIndex": 1},
                {"itemId": "dup", "itemIndex": 0},
            ]},
            "conflicts": [{"field": "items.1.quantity", "previous": "500", "current": "1000"}],
            "rejectedFields": ["items.1.paper"],
            "runHistory": [{"runId": "run-1", "events": [{"itemId": "dup", "itemIndex": 1}]}],
        })
        self.assertEqual([item["itemId"] for item in state["order"]["items"]], ["dup", "item-2"])
        self.assertIn("items.item-2.quantity", state["fieldMeta"])
        self.assertEqual(state["uploadedFiles"][0]["itemId"], "item-2")
        self.assertEqual(state["uploadedFiles"][0]["itemIndex"], 1)
        self.assertIsNone(state["uploadedFiles"][1]["itemId"])
        self.assertIsNone(state["uploadedFiles"][1]["itemIndex"])
        request = state["quoteRequests"][0]
        self.assertEqual(request["itemId"], "item-2")
        self.assertEqual(request["mappedOrder"]["itemId"], "item-2")
        self.assertEqual(request["status"], "stale")
        self.assertIsNone(state["activeQuoteRequestId"])
        self.assertEqual(state["handoff"]["items"][0]["itemId"], "item-2")
        self.assertEqual(state["handoff"]["items"][1]["itemId"], "dup")
        self.assertEqual(state["conflicts"][0]["field"], "items.item-2.quantity")
        self.assertEqual(state["rejectedFields"], ["items.item-2.paper"])
        self.assertEqual(state["runHistory"][0]["events"][0]["itemId"], "item-2")
        self.assertEqual(state, normalize_state(state))

    def test_duplicate_item_patch_migrates_existing_item_references(self) -> None:
        with TemporaryDirectory() as tmp:
            agent = Agent(Memory(Path(tmp) / "agent.sqlite3"))
            agent.state = normalize_state({
                "order": {"items": [
                    {"itemId": "old-a", "productType": "名片"},
                    {"itemId": "old-b", "productType": "折页"},
                ]},
                "fieldMeta": {"items.old-b.quantity": {"value": "1000 张"}},
                "itemOptions": {"old-b": [{"id": "balanced"}]},
                "uploadedFiles": [{"itemId": "old-b", "itemIndex": 1, "fileName": "fold.pdf"}],
            })
            agent._update_order({"items": [
                {"itemId": "dup", "productType": "名片"},
                {"itemId": "dup", "productType": "折页"},
            ]}, source="user", confidence=1.0)
        self.assertEqual([item["itemId"] for item in agent.state["order"]["items"]], ["dup", "item-2"])
        self.assertIn("items.item-2.quantity", agent.state["fieldMeta"])
        self.assertIn("item-2", agent.state["itemOptions"])
        self.assertEqual(agent.state["uploadedFiles"][0]["itemId"], "item-2")

    def test_item_id_with_dots_keeps_dimension_provenance_migration(self) -> None:
        state = normalize_state({
            "order": {"items": [{"itemId": "legacy.dot", "productType": "折页"}]},
            "fieldMeta": {
                "items.legacy.dot.productSpecs.dieCutSize": {
                    "value": "432×273MM", "source": "rule",
                },
            },
        })
        self.assertIn("items.legacy.dot.dimensions.dieCutSize", state["fieldMeta"])

    def test_large_numeric_state_values_fail_closed(self) -> None:
        huge = "9" * 5000
        self.assertIsNone(parse_quantity(huge, "名片"))
        normalized = normalize_state({
            "order": {"items": [{"itemId": "item-1", "productType": "名片"}]},
            "fieldMeta": {"size": {"value": "A4", "confidence": 10 ** 5000}},
            "rejectedFields": [f"items.{huge}.quantity"],
        })
        self.assertNotIn("confidence", normalized["fieldMeta"].get("size", {}))


if __name__ == "__main__":
    unittest.main()
