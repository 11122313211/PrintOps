import tempfile
import os
import unittest
from unittest.mock import patch as mock_patch
import json
from pathlib import Path
from urllib.error import URLError

from agent import (Agent, Memory, ORDER_DEFAULTS, estimate_price, match_supplier_capability,
                    preflight_file, prepare_handoff, recommend_processes, request_supplier_quote,
                    validate_order)
from llm_adapter import OpenAICompatiblePlanner, normalize_base_url, read_saved_config, write_saved_config
from product_knowledge import KNOWLEDGE_VERSION, parameter_state


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.tmp.name) / "agent.sqlite3")
        self.agent = Agent(self.memory)

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_recommend_and_generate(self):
        result = self.agent.chat("我要做 500 份 A4 宣传册，32页骑马钉，下周一要用，想要有质感", {"paper": "待推荐", "printing": "双面四色"})
        self.assertEqual(result["stage"], "recommend")
        self.assertEqual(len(result["options"]), 3)
        self.agent.choose("balanced")
        result = self.agent.generate()
        self.assertTrue(result["orderGenerated"])
        self.assertIn("目标平台：通用印刷平台", result["handoff"]["text"])
        self.assertIn("调用工具：prepare_handoff", result["toolTrace"])

    def test_order_response_carries_versioned_knowledge_provenance(self):
        result = self.agent.chat("做 500 张 A4 名片，250g铜版纸，双面四色，下周内")
        self.assertEqual(result["knowledge"]["version"], KNOWLEDGE_VERSION)
        self.assertEqual(result["validation"]["knowledgeVersion"], KNOWLEDGE_VERSION)
        self.assertEqual(result["productProfile"]["knowledge"]["version"], KNOWLEDGE_VERSION)
        self.assertEqual(parameter_state(result["order"])["knowledge"]["version"], KNOWLEDGE_VERSION)

    def test_supplier_capability_identifies_static_profile_revision(self):
        result = match_supplier_capability({**ORDER_DEFAULTS, "productType": "名片", "size": "A4"}, "shengda")
        self.assertEqual(result["knowledgeVersion"], KNOWLEDGE_VERSION)
        self.assertRegex(result["supplierProfileVersion"], r"^\d{4}\.\d{2}\.\d{2}$")

    def test_memory_survives_new_agent(self):
        self.agent.chat("做 100 份 A5 名片，一周内拿到", {"paper": "250g 铜版纸", "printing": "双面四色"})
        restored = Agent(self.memory, self.agent.id).snapshot()
        self.assertEqual(restored["order"]["productType"], "名片")
        self.assertEqual(restored["order"]["quantity"], "100 份")
        self.assertEqual(restored["order"]["quantityValue"], 100)
        self.assertEqual(restored["order"]["quantityUnit"], "份")
        self.assertEqual(restored["toolTrace"], ["恢复会话记忆"])

    def test_old_session_quantity_is_migrated_to_structured_fields(self):
        legacy_id = "legacy-session"
        legacy = self.memory.fresh_state()
        legacy["order"]["productType"] = "包装盒"
        legacy["order"]["quantity"] = "500 个"
        legacy["order"].pop("quantityValue", None)
        legacy["order"].pop("quantityUnit", None)
        self.memory.save(legacy_id, legacy)
        restored = Agent(self.memory, legacy_id).state["order"]
        self.assertEqual(restored["quantity"], "500 个")
        self.assertEqual(restored["quantityValue"], 500)
        self.assertEqual(restored["quantityUnit"], "个")

    def test_snapshot_includes_chat_history_for_ui_restore(self):
        self.agent.chat("做 100 份 A4 名片，一周内拿到", {"paper": "250g 铜版纸", "printing": "双面四色"})
        restored = Agent(self.memory, self.agent.id).snapshot()
        self.assertEqual([item["role"] for item in restored["history"]], ["user", "assistant"])
        self.assertIn("100 份", restored["history"][1]["text"])

    def test_runtime_returns_run_trace_workflow_and_tool_contracts(self):
        result = self.agent.chat("做 500 份 A4 名片")
        self.assertRegex(result["runId"], r"^[0-9a-f]{16}$")
        self.assertEqual(result["workflowStage"], "collect")
        self.assertEqual(result["workflowLabel"], "需求收集")
        self.assertEqual(result["runTrace"][0]["status"], "started")
        self.assertTrue(any(item["tool"] == "validate_order" for item in result["runTrace"] if item.get("tool")))
        recommend = next(item for item in result["availableTools"] if item["name"] == "recommend_processes")
        self.assertEqual(recommend["input"]["type"], "object")
        self.assertEqual(recommend["output"]["type"], "array")

    def test_field_provenance_and_correction_conflict_are_exposed(self):
        first = self.agent.chat("做 500 份 A4 名片")
        self.assertEqual(first["fieldMeta"]["quantity"]["source"], "rule")
        self.assertLess(first["fieldMeta"]["quantity"]["confidence"], 1)
        second = self.agent.chat("数量改为 800 份", {"paper": "250g 铜版纸", "printing": "双面四色", "deadline": "下周内"})
        self.assertEqual(second["fieldMeta"]["quantity"]["source"], "rule")
        self.assertTrue(any(item["field"] == "quantity" and item["previous"] == "500 份" for item in second["conflicts"]))
        self.assertEqual(second["workflowStage"], "recommend")

    def test_product_specific_missing_parameters_use_clarify_workflow_stage(self):
        result = self.agent.chat("做 500 个包装盒，60*40*20cm，350g白卡纸，双面四色，下周内")
        self.assertEqual(result["workflowStage"], "clarify")
        self.assertEqual(result["workflowLabel"], "品类澄清")
        self.assertEqual(result["decision"]["stage"], "clarify")

    def test_low_confidence_field_requires_user_confirmation_before_generation(self):
        self.agent.chat("做 500 份 A4 名片，250g铜版纸，双面四色，下周内")
        self.agent._update_order({"budget": "优先视觉质感"}, source="model", confidence=0.68)
        self.agent.choose("balanced")
        blocked = self.agent.generate()
        self.assertFalse(blocked["orderGenerated"])
        self.assertIn("低置信度字段", blocked["messages"][0])
        confirmed = self.agent.chat("确认预算偏好", {"budget": "优先视觉质感"})
        self.assertEqual(confirmed["fieldMeta"]["budget"]["source"], "user")
        self.assertEqual(confirmed["fieldMeta"]["budget"]["confidence"], 1.0)

    def test_runtime_stage_reflects_quote_and_file_preflight_tools(self):
        quote = self.agent.call_tool("estimate_price")
        self.assertEqual(quote["workflowStage"], "quote")
        preflight = self.agent.upload("artwork.pdf", 1024, page_count=1)
        self.assertEqual(preflight["workflowStage"], "preflight")

    def test_run_history_survives_restart(self):
        result = self.agent.chat("做 100 份 A4 名片")
        restored_agent = Agent(self.memory, self.agent.id)
        self.assertTrue(restored_agent.state["runHistory"])
        self.assertEqual(restored_agent.state["runHistory"][-1]["runId"], result["runId"])
        self.assertTrue(restored_agent.state["runHistory"][-1]["events"])

    def test_agent_handles_corrections_and_invalidates_old_choice(self):
        result = self.agent.chat("做 500 份 A4 宣传册，32页横版，250g铜版纸，双面四色，三天后")
        self.assertEqual(result["order"]["pages"], "32 页")
        self.assertEqual(result["order"]["orientation"], "横版")
        self.agent.choose("balanced")
        self.assertEqual(self.agent.state["order"]["binding"], "骑马钉")
        result = self.agent.chat("数量改为 1,200 份")
        self.assertEqual(result["order"]["quantity"], "1200 份")
        self.assertIsNone(result["selectedOption"])

    def test_explanation_tool_answers_non_expert_questions(self):
        result = self.agent.chat("哑粉纸和铜版纸怎么选？")
        self.assertEqual(result["toolTrace"][-1], "调用工具：explain_print_term")

    def test_pdf_preflight_reports_pages_and_filename_risks(self):
        result = self.agent.upload("画册-无出血-RGB.pdf", 1024 * 1024, page_count=52)
        check = result["toolResult"]
        self.assertTrue(check["ok"])
        self.assertEqual(check["pageCount"], 52)
        self.assertIn("52 页", result["messages"][0])
        self.assertIn("缺少出血", result["messages"][0])
        self.assertIn("RGB 颜色", result["messages"][0])
        self.assertIn("装订方式", result["messages"][0])

    def test_pdf_preflight_rejects_encrypted_file(self):
        result = self.agent.upload("报价单.pdf", 1024, page_count=2, encrypted=True)
        check = result["toolResult"]
        self.assertFalse(check["ok"])
        self.assertIn("已加密", result["messages"][0])
        self.assertIsNone(self.agent.state["uploadedFile"])

    def test_pdf_metadata_preflight_reports_print_clues_without_claiming_pass(self):
        check = preflight_file(
            "artwork.pdf", 1024, page_count=1, expected_size="A4",
            inspection={
                "isPdf": True, "pdfVersion": "1.7", "hasEof": True,
                "boxes": {"media": [0, 0, 612, 792], "trim": [0, 0, 595.28, 841.89]},
                "colorSpaces": ["DeviceRGB"], "fontEmbedding": "unknown", "imageCount": 2,
                "hasTransparency": True, "hasOverprint": False,
            },
        )
        self.assertTrue(check["ok"])
        self.assertEqual(check["inspectionLevel"], "metadata")
        labels = {item["label"] for item in check["checks"]}
        self.assertIn("页面 MediaBox", labels)
        self.assertIn("出血 BleedBox", labels)
        self.assertIn("颜色空间线索", labels)
        self.assertIn("成品尺寸一致性", labels)
        self.assertTrue(any(item["label"] == "成品尺寸一致性" and item["status"] == "ok" for item in check["checks"]))
        self.assertTrue(any("RGB" in warning for warning in check["warnings"]))
        self.assertTrue(any("透明度" in warning for warning in check["warnings"]))

        mismatch = preflight_file(
            "artwork.pdf", 1024, page_count=1, expected_size="B4",
            inspection={"isPdf": True, "boxes": {"trim": [0, 0, 595.28, 841.89]}},
        )
        self.assertTrue(any(item["label"] == "成品尺寸一致性" and item["status"] == "warn" for item in mismatch["checks"]))
        self.assertTrue(any("展开尺寸" in warning for warning in mismatch["warnings"]))

    def test_booklet_validation_flags_saddle_stitch_page_conflict(self):
        self.agent.chat("做 500 本 A4 画册 200页，250g铜版纸，双面四色，下周内，骑马钉")
        result = validate_order(self.agent.state["order"])
        self.assertTrue(result["ok"])
        self.assertTrue(any("200 页画册使用骑马钉" in item["message"] for item in result["risks"]))
        self.assertTrue(any("锁线胶装" in item["suggestion"] for item in result["risks"]))

    def test_api_result_includes_structured_validation(self):
        result = self.agent.chat("做 A4 画册 200页，250g铜版纸，双面四色，下周内，骑马钉")
        self.assertIn("validation", result)
        self.assertTrue(result["validation"]["risks"])

    def test_patch_fields_are_whitelisted(self):
        result = self.agent.chat("做 A4 名片", {"unknownField": "should not persist"})
        self.assertNotIn("unknownField", result["order"])

    def test_spec_preset_patch_applies_process_fields(self):
        result = self.agent.chat("沿用常用规格", {
            "paper": "250g 铜版纸", "printing": "双面四色",
            "finishing": "哑膜", "binding": "骑马钉"
        })
        self.assertEqual(result["order"]["paper"], "250g 铜版纸")
        self.assertEqual(result["order"]["printing"], "双面四色")
        self.assertEqual(result["order"]["finishing"], "哑膜")
        self.assertEqual(result["order"]["binding"], "骑马钉")

    def test_optional_product_spec_can_be_cleared(self):
        self.agent.chat("做 100 份 A4 名片，250g 铜版纸，双面四色，下周内，圆角")
        self.assertEqual(self.agent.state["order"]["productSpecs"].get("cardCorners"), "圆角")
        self.agent.chat("清除圆角", {"productSpecs": {"cardCorners": ""}})
        self.assertNotIn("cardCorners", self.agent.state["order"]["productSpecs"])

    def test_supplier_handoff_reports_capability_readiness(self):
        order = {**ORDER_DEFAULTS, "productType": "画册", "paper": "250g 铜版纸",
                 "finishing": "哑膜", "size": "A4", "deadline": "下周", "platform": "shengda"}
        result = prepare_handoff(order)
        self.assertEqual(result["knowledgeVersion"], KNOWLEDGE_VERSION)
        readiness = result["supplierReadiness"]
        self.assertTrue(any(item["field"] == "品类" for item in readiness["supported"]))
        self.assertTrue(any(item["value"] == "250g 铜版纸" for item in readiness["supported"]))
        self.assertTrue(any(item["value"] == "哑膜" for item in readiness["supported"]))
        self.assertTrue(any(item["field"] == "成品尺寸" for item in readiness["needsReview"]))
        self.assertTrue(any(item["field"] == "交期" for item in readiness["needsReview"]))

    def test_supplier_quote_request_is_mapped_and_requires_confirmation(self):
        order = {**ORDER_DEFAULTS, "platform": "shengda", "productType": "名片", "quantity": "500 张",
                 "quantityValue": 500, "quantityUnit": "张", "size": "A4", "paper": "250g 铜版纸",
                 "printing": "双面四色", "deadline": "下周"}
        result = request_supplier_quote(order)
        self.assertEqual(result["status"], "awaiting_human_confirmation")
        self.assertEqual(result["knowledgeVersion"], KNOWLEDGE_VERSION)
        self.assertTrue(result["requiresHumanConfirmation"])
        self.assertEqual(result["mappedOrder"]["quantityUnit"], "张")
        self.assertNotIn("price", result)

        unsupported = {**order, "productType": "PVC", "paper": "PVC板"}
        blocked = request_supplier_quote(unsupported)
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(blocked["requiresHumanConfirmation"])

    def test_quote_request_is_persisted_and_retries_are_idempotent(self):
        self.agent.chat("做 500 张 A4 名片，250g铜版纸，双面四色，下周内")
        self.agent.choose("balanced")
        first = self.agent.call_tool("request_supplier_quote", {"platformId": "shengda"})
        request = first["toolResult"]
        self.assertEqual(request["status"], "awaiting_human_confirmation")
        self.assertTrue(request["requestId"].startswith("quote-"))
        self.assertTrue(request["idempotencyKey"].startswith("quote:"))
        second = self.agent.call_tool("request_supplier_quote", {"platformId": "shengda"})
        self.assertEqual(second["toolResult"]["requestId"], request["requestId"])
        self.assertTrue(second["toolResult"]["idempotent"])
        self.assertEqual(len(self.agent.state["quoteRequests"]), 1)

    def test_quote_request_survives_restart_and_human_confirmation_updates_status(self):
        self.agent.chat("做 500 张 A4 名片，250g铜版纸，双面四色，下周内")
        self.agent.choose("balanced")
        self.agent.call_tool("request_supplier_quote", {"platformId": "shengda"})
        request_id = self.agent.state["activeQuoteRequestId"]
        restored = Agent(self.memory, self.agent.id)
        status = restored.quote_status(request_id)
        self.assertEqual(status["toolResult"]["status"], "awaiting_human_confirmation")
        restored.generate()
        confirmed = restored.confirm("已核对询价字段")
        self.assertEqual(confirmed["quoteRequest"]["status"], "confirmed")

    def test_order_change_marks_pending_quote_stale(self):
        self.agent.chat("做 500 张 A4 名片，250g铜版纸，双面四色，下周内")
        self.agent.choose("balanced")
        self.agent.call_tool("request_supplier_quote", {"platformId": "shengda"})
        request_id = self.agent.state["activeQuoteRequestId"]
        changed = self.agent.chat("数量改为 800 张")
        request = next(item for item in changed["quoteRequests"] if item["requestId"] == request_id)
        self.assertEqual(request["status"], "stale")
        self.assertIsNone(changed["activeQuoteRequestId"])

    def test_cancel_quote_is_idempotent(self):
        self.agent.chat("做 500 张 A4 名片，250g铜版纸，双面四色，下周内")
        self.agent.choose("balanced")
        self.agent.call_tool("request_supplier_quote", {"platformId": "shengda"})
        request_id = self.agent.state["activeQuoteRequestId"]
        cancelled = self.agent.cancel_quote(request_id, "不再询价")
        self.assertEqual(cancelled["quoteRequest"]["status"], "cancelled")
        repeated = self.agent.cancel_quote(request_id)
        self.assertTrue(repeated["toolResult"]["idempotent"])
        self.assertEqual(repeated["quoteRequest"]["status"], "cancelled")

    def test_supplier_capability_matches_dimensions_and_flags_unsupported_material(self):
        supported = match_supplier_capability({**ORDER_DEFAULTS, "platform": "shengda", "productType": "名片",
                                               "size": "A4", "paper": "250g 铜版纸", "finishing": "哑膜",
                                               "deadline": "下周"})
        self.assertEqual(supported["status"], "review")
        self.assertTrue(any(item["field"] == "成品尺寸" and item["value"] == "A4" for item in supported["supported"]))
        self.assertFalse(supported["unsupported"])
        unsupported = match_supplier_capability({**ORDER_DEFAULTS, "platform": "shengda", "productType": "名片",
                                                 "size": "A4", "paper": "PVC板", "finishing": "击凸",
                                                 "deadline": "下周"})
        self.assertTrue(any(item["field"] == "纸张/材料" for item in unsupported["unsupported"]))

    def test_generate_is_idempotent_after_draft_exists(self):
        self.agent.chat("做 500 份 A4 名片，250g铜版纸，双面四色，下周内")
        self.agent.choose("balanced")
        first = self.agent.generate()
        second = self.agent.generate()
        self.assertTrue(first["orderGenerated"])
        self.assertTrue(second["orderGenerated"])
        self.assertIn("已经生成", second["messages"][0])

    def test_handoff_and_human_confirmation_survive_restart(self):
        self.agent.chat("做 500 份 A4 名片，250g铜版纸，双面四色，下周内")
        self.agent.choose("balanced")
        generated = self.agent.generate()
        self.assertEqual(generated["confirmation"]["status"], "pending")
        self.assertEqual(generated["handoff"]["status"], "ready")
        restored = Agent(self.memory, self.agent.id).snapshot()
        self.assertEqual(restored["confirmation"]["status"], "pending")
        self.assertEqual(restored["handoff"]["status"], "ready")

        confirmed = Agent(self.memory, self.agent.id).confirm("已核对文件和交期")
        self.assertEqual(confirmed["confirmation"]["status"], "confirmed")
        self.assertEqual(confirmed["workflowStage"], "export")
        self.assertFalse(confirmed["decision"]["humanConfirmationRequired"])
        final = Agent(self.memory, self.agent.id).snapshot()
        self.assertEqual(final["confirmation"]["note"], "已核对文件和交期")

    def test_order_change_invalidates_confirmed_handoff(self):
        self.agent.chat("做 500 份 A4 名片，250g铜版纸，双面四色，下周内")
        self.agent.choose("balanced")
        self.agent.generate()
        self.agent.confirm()
        changed = self.agent.chat("数量改为 800 份")
        self.assertFalse(changed["orderGenerated"])
        self.assertEqual(changed["confirmation"]["status"], "not_ready")
        self.assertIsNone(changed["handoff"])

    def test_platform_is_not_hardcoded(self):
        result = self.agent.set_platform("shengda")
        self.assertEqual(result["order"]["platform"], "shengda")
        result = self.agent.set_platform("unknown")
        self.assertEqual(result["order"]["platform"], "generic")

    def test_common_printing_language_reaches_tool_stage(self):
        result = self.agent.chat("做 500 册 A4 宣传册，157克哑粉纸，双面彩印，三天后要用，预算不要太高")
        self.assertEqual(result["stage"], "recommend")
        self.assertEqual(result["order"]["quantity"], "500 册")
        self.assertEqual(result["order"]["quantityValue"], 500)
        self.assertEqual(result["order"]["quantityUnit"], "册")
        self.assertEqual(result["order"]["paper"], "157g 哑粉纸")
        self.assertEqual(result["order"]["deadline"], "三天后")
        self.assertEqual(result["toolTrace"][-1], "调用工具：recommend_processes")

    def test_explicit_tool_gateway(self):
        result = self.agent.call_tool("estimate_price")
        self.assertEqual(result["toolTrace"], ["调用工具：estimate_price"])
        self.assertIn("range", result["toolResult"])
        self.assertTrue(any(item["name"] == "validate_order" for item in result["availableTools"]))

    def test_a4_without_quantity_is_not_misread_as_four(self):
        result = self.agent.chat("做 A4 宣传册")
        self.assertEqual(result["order"]["size"], "A4")
        self.assertEqual(result["order"]["quantity"], "")
        self.assertEqual(result["stage"], "collect")

    def test_punctuation_without_quantity_does_not_crash_perception(self):
        result = self.agent.chat("我想做一份面向客户的品牌宣传册，请先帮我判断需要哪些参数。")
        self.assertEqual(result["order"]["productType"], "宣传册")
        self.assertEqual(result["order"]["quantity"], "")
        self.assertEqual(result["stage"], "collect")

    def test_a_and_custom_dimensions_are_normalized(self):
        result = self.agent.chat("做 B4 宣传册")
        self.assertEqual(result["order"]["size"], "B4")
        result = self.agent.chat("尺寸 210\\*267mm")
        self.assertEqual(result["order"]["size"], "210×267MM")
        result = self.agent.chat("做 210*267mm 的折页")
        self.assertEqual(result["order"]["size"], "210×267MM")
        self.assertNotEqual(result["order"]["quantity"], "210 份")

    def test_size_variants_are_normalized_without_becoming_quantity(self):
        cases = {
            "B 4": "B4",
            "B-4": "B4",
            "Ｂ４": "B4",
            "210＊267": "210×267",
            "210 x 267 毫米": "210×267MM",
            "210mm × 267mm": "210×267MM",
            "210毫米*267毫米": "210×267MM",
        }
        for raw_size, expected in cases.items():
            parsed = Agent._perceive(f"做 {raw_size} 宣传册")
            self.assertEqual(parsed["size"], expected, raw_size)
            self.assertNotIn("quantity", parsed, raw_size)

        parsed = Agent._perceive("做 500 份 B4 宣传册，210\\*267mm")
        self.assertEqual(parsed["quantity"], "500 份")
        self.assertEqual(parsed["size"], "B4 / 210×267MM")

    def test_labeled_dimensions_keep_finished_expanded_and_die_cut_separate(self):
        parsed = Agent._perceive("做 500 张折页，成品尺寸 210*267mm，展开尺寸 426*267mm，刀模尺寸 432*273mm")
        self.assertEqual(parsed["size"], "210×267MM")
        self.assertEqual(parsed["dimensions"]["finishedSize"], "210×267MM")
        self.assertEqual(parsed["dimensions"]["expandedSize"], "426×267MM")
        self.assertEqual(parsed["dimensions"]["dieCutSize"], "432×273MM")
        self.assertEqual(parsed["dimensions"]["packageSize"], "")

    def test_structural_size_is_migrated_to_package_dimension(self):
        parsed = Agent._perceive("做 500 个天地盖包装盒，60*40*20cm")
        self.assertEqual(parsed["size"], "60×40×20CM")
        self.assertEqual(parsed["dimensions"]["packageSize"], "60×40×20CM")
        self.assertEqual(parsed["dimensions"]["finishedSize"], "")

    def test_legacy_session_migrates_size_to_dimension_meaning(self):
        legacy_id = "legacy-dimension-session"
        legacy = self.memory.fresh_state()
        legacy["order"].pop("dimensions", None)
        legacy["order"].update({"productType": "包装盒", "size": "60×40×20CM"})
        self.memory.save(legacy_id, legacy)
        restored = Agent(self.memory, legacy_id).state["order"]
        self.assertEqual(restored["dimensions"]["packageSize"], "60×40×20CM")
        self.assertEqual(restored["dimensions"]["finishedSize"], "")

    def test_legacy_dimension_field_meta_is_migrated_with_the_order(self):
        legacy_id = "legacy-dimension-meta-session"
        legacy = self.memory.fresh_state()
        legacy["fieldMeta"] = {
            "productSpecs.expandedSize": {"value": "426×267MM", "source": "model", "confidence": 0.68},
            "items.item-1.productSpecs.dieCutSize": {"value": "432×273MM", "source": "rule", "confidence": 0.84},
        }
        self.memory.save(legacy_id, legacy)
        restored = Agent(self.memory, legacy_id).state["fieldMeta"]
        self.assertIn("dimensions.expandedSize", restored)
        self.assertIn("items.item-1.dimensions.dieCutSize", restored)
        self.assertNotIn("productSpecs.expandedSize", restored)
        self.assertNotIn("items.item-1.productSpecs.dieCutSize", restored)

    def test_multi_product_dimensions_are_copied_only_for_an_unambiguous_shared_size(self):
        parsed = Agent._perceive("做 500 张名片和 1000 张折页，成品尺寸 A4，157g哑粉纸，双面四色，下周内")
        self.assertEqual(len(parsed["items"]), 2)
        self.assertEqual(parsed["items"][0]["dimensions"]["finishedSize"], "A4")
        self.assertEqual(parsed["items"][1]["dimensions"]["finishedSize"], "A4")

    def test_dimension_alias_from_model_patch_is_canonicalized(self):
        result = self.agent.chat("做 500 张折页，210*267mm", {
            "productSpecs": {"expandedSize": "426×267MM", "dieCutSize": "432×273MM"}
        })
        self.assertEqual(result["order"]["dimensions"]["expandedSize"], "426×267MM")
        self.assertEqual(result["order"]["dimensions"]["dieCutSize"], "432×273MM")
        self.assertNotIn("expandedSize", result["order"]["productSpecs"])
        self.assertNotIn("dieCutSize", result["order"]["productSpecs"])

    def test_quantity_units_are_preserved_and_default_by_product(self):
        cases = {
            "做 2000 张折页，210*267mm": ("2000 张", 2000, "张"),
            "做 500 个包装盒，60*40*20cm": ("500 个", 500, "个"),
            "做 300 本联单，A4": ("300 本", 300, "本"),
            "做 2万名片，A4": ("20000 张", 20000, "张"),
        }
        for text, expected in cases.items():
            parsed = Agent._perceive(text)
            self.assertEqual((parsed["quantity"], parsed["quantityValue"], parsed["quantityUnit"]), expected, text)

        no_unit = Agent._perceive("做 500 名片，A4")
        self.assertEqual(no_unit["quantityUnit"], "张")
        self.assertEqual(no_unit["quantity"], "500 张")

        agent = Agent(self.memory)
        agent.chat("做 500 张名片，A4")
        agent._update_order({"quantity": "600 个", "quantityUnit": "张"}, source="user", confidence=1.0)
        self.assertEqual(agent.state["order"]["quantity"], "600 个")
        self.assertEqual(agent.state["order"]["quantityUnit"], "个")

    def test_multi_product_request_is_split_and_blocked_from_combined_quote(self):
        parsed = Agent._perceive("做 500 张名片和 1000 张折页，A4，157g哑粉纸，双面四色，下周内")
        self.assertEqual(parsed["productTypes"], ["名片", "折页"])
        self.assertEqual(len(parsed["items"]), 2)
        self.assertEqual(parsed["items"][0]["quantity"], "500 张")
        self.assertEqual(parsed["items"][1]["quantity"], "1000 张")
        self.assertEqual(parsed["items"][0]["size"], "A4")
        self.assertEqual(parsed["items"][0]["paper"], "157g 哑粉纸")
        self.assertEqual(parsed["items"][1]["printing"], "双面四色")

        result = self.agent.chat("做 500 张名片和 1000 张折页，A4，157g哑粉纸，双面四色，下周内")
        self.assertEqual(result["workflowStage"], "clarify")
        self.assertFalse(result["validation"]["ok"])
        self.assertTrue(result["validation"]["multiProduct"])
        self.assertIn("多个印刷品", result["messages"][0])
        focused = self.agent.chat("处理第 2 项：折页")
        self.assertEqual(focused["activeItemIndex"], 1)
        self.assertEqual(focused["order"]["productType"], "名片")
        self.assertIn("第 2 项", focused["messages"][0])

    def test_multi_product_items_have_stable_ids_and_focused_updates_are_isolated(self):
        first = self.agent.chat("做 500 张名片和 1000 张折页，A4，157g哑粉纸，双面四色，下周内")
        items = first["order"]["items"]
        self.assertEqual([item["itemId"] for item in items], ["item-1", "item-2"])
        self.assertEqual([item["status"] for item in first["validation"]["itemValidations"]], ["ready", "needs_input"])

        self.agent.chat("处理第 2 项：折页")
        updated = self.agent.chat("数量改为 1200 张，尺寸改为 210*267mm，三折")
        updated_items = updated["order"]["items"]
        self.assertEqual(updated_items[0]["quantity"], "500 张")
        self.assertEqual(updated_items[0]["size"], "A4")
        self.assertEqual(updated_items[1]["quantity"], "1200 张")
        self.assertEqual(updated_items[1]["size"], "210×267MM")
        self.assertEqual(updated_items[1]["productSpecs"]["folding"], "三折")
        self.assertEqual(updated["fieldMeta"]["items.item-2.size"]["source"], "rule")
        item = updated["validation"]["itemValidations"][1]
        self.assertTrue(item["ok"])
        self.assertEqual(item["productMissing"], [])

        restored = Agent(self.memory, self.agent.id).snapshot()
        self.assertEqual([item["itemId"] for item in restored["order"]["items"]], ["item-1", "item-2"])
        self.assertEqual(restored["order"]["items"][1]["quantity"], "1200 张")

    def test_chat_item_index_routes_api_style_update(self):
        self.agent.chat("做 500 张名片和 1000 张折页，A4，157g哑粉纸，双面四色，下周内")
        result = self.agent.chat("更新当前项", {"paper": "250g 铜版纸"}, item_index=1)
        self.assertEqual(result["order"]["items"][1]["paper"], "250g 铜版纸")
        self.assertEqual(result["order"]["items"][0]["paper"], "157g 哑粉纸")
        self.assertEqual(result["activeItemIndex"], 1)

    def test_multi_product_items_can_recommend_choose_and_generate_separately(self):
        self.agent.chat("做 500 张名片和 1000 张折页，A4，157g哑粉纸，双面四色，下周内")
        self.agent.chat("处理第 2 项：折页")
        recommended = self.agent.chat("三折")
        self.assertEqual(recommended["workflowStage"], "recommend")
        self.assertEqual(len(recommended["options"]), 3)
        chosen = self.agent.choose("balanced")
        self.assertEqual(chosen["order"]["items"][1]["selectedOption"], "balanced")

        first = self.agent.chat("处理第 1 项：名片")
        self.assertEqual(first["workflowStage"], "recommend")
        self.assertEqual(len(first["options"]), 3)
        self.agent.choose("balanced")
        generated = self.agent.generate()
        self.assertTrue(generated["orderGenerated"])
        self.assertEqual(generated["workflowStage"], "confirm")
        self.assertEqual(generated["handoff"]["status"], "ready")
        self.assertEqual(len(generated["handoff"]["items"]), 2)
        self.assertIn("第 1 项：名片", generated["handoff"]["text"])
        self.assertIn("第 2 项：折页", generated["handoff"]["text"])

        self.agent.chat("处理第 2 项：折页")
        changed = self.agent.chat("数量改为 1200 张")
        self.assertFalse(changed["orderGenerated"])
        self.assertIsNone(changed["order"]["items"][1]["selectedOption"])
        self.assertEqual(changed["order"]["items"][1]["quantity"], "1200 张")

    def test_multi_product_tool_can_estimate_current_item_without_combining(self):
        self.agent.chat("做 500 张名片和 1000 张折页，A4，157g哑粉纸，双面四色，下周内")
        self.agent.chat("处理第 1 项：名片")
        estimate = self.agent.call_tool("estimate_price")
        self.assertEqual(estimate["toolResult"]["itemIndex"], 0)
        self.assertIsNotNone(estimate["toolResult"]["range"])
        self.assertNotIn("不能合并", estimate["messages"][0])

    def test_multi_product_file_is_bound_to_the_selected_item(self):
        self.agent.chat("做 500 张名片和 1000 张折页，A4，157g哑粉纸，双面四色，下周内")
        blocked = self.agent.upload("artwork.pdf", 1024, page_count=1)
        self.assertEqual(blocked["toolResult"]["reason"], "item_required")
        self.assertEqual(self.agent.state["uploadedFiles"], [])

        checked = self.agent.upload("fold.pdf", 1024, page_count=1, item_index=1)
        self.assertTrue(checked["toolResult"]["ok"])
        self.assertEqual(checked["toolResult"]["itemIndex"], 1)
        self.assertEqual(self.agent.state["order"]["items"][1]["uploadedFile"], "fold.pdf")
        self.assertIsNone(self.agent.state["order"]["items"][0]["uploadedFile"])
        self.assertEqual(self.agent.state["uploadedFiles"][0]["itemId"], "item-2")
        restored = Agent(self.memory, self.agent.id).snapshot()
        self.assertEqual(restored["uploadedFiles"][0]["fileName"], "fold.pdf")

    def test_multi_product_high_risk_tools_are_blocked_individually(self):
        order = {**ORDER_DEFAULTS, "productType": "名片", "productTypes": ["名片", "折页"],
                 "items": [{"productType": "名片", "quantity": "500 张"},
                           {"productType": "折页", "quantity": "1000 张"}],
                 "quantity": "500 张", "quantityValue": 500, "quantityUnit": "张",
                 "size": "A4", "paper": "157g 哑粉纸", "printing": "双面四色", "deadline": "下周"}

        self.assertEqual(recommend_processes(order), [])
        estimate = estimate_price(order)
        self.assertEqual(estimate["status"], "blocked")
        self.assertIsNone(estimate["range"])
        quote = request_supplier_quote(order)
        self.assertEqual(quote["status"], "blocked")
        self.assertTrue(quote["multiProduct"])
        handoff = prepare_handoff(order)
        self.assertEqual(handoff["status"], "blocked")
        self.assertFalse(handoff["mappedOrder"])

        self.agent.state["order"].update(order)
        result = self.agent.call_tool("recommend_processes")
        self.assertEqual(result["toolResult"]["reason"], "multi_product")
        self.assertEqual(result["workflowStage"], "clarify")

    def test_product_profiles_capture_different_parameter_sets(self):
        fold = Agent._perceive("做 1000 张三折页，210*285mm，157克哑粉纸，双面四色，下周内")
        self.assertEqual(fold["productType"], "折页")
        self.assertEqual(fold["productSpecs"]["folding"], "三折")

        box = Agent._perceive("做 500 个天地盖包装盒，60*40*20cm，350g白卡纸，双面四色，下周内")
        self.assertEqual(box["productType"], "包装盒")
        self.assertEqual(box["productSpecs"]["boxSize"], "60×40×20CM")
        self.assertEqual(box["productSpecs"]["boxStructure"], "天地盖")

    def test_product_validation_reports_specialized_parameters(self):
        result = self.agent.chat("做 500 个包装盒，60*40*20cm，350g白卡纸，双面四色，下周内")
        self.assertIn("盒型结构", [item["label"] for item in result["productProfile"]["missing"]])
        self.assertEqual(result["productProfile"]["category"], "包装周边")

    def test_label_and_invoice_parameters_are_not_generic_fields(self):
        label = Agent._perceive("做 2000 张圆形透明不干胶标签，50*50mm，四色印刷，下周内")
        self.assertEqual(label["productSpecs"], {"labelMaterial": "透明不干胶", "labelShape": "圆形"})
        invoice = Agent._perceive("做 300 本三联无碳联单，A4，单色印刷，下周内，需要流水号")
        self.assertEqual(invoice["productSpecs"]["paperParts"], "三联")
        self.assertEqual(invoice["productSpecs"]["numbering"], "需要连续编号")

    def test_label_recommendations_keep_label_material(self):
        result = self.agent.chat("做 2000 张圆形透明不干胶标签，50*50mm，四色印刷，下周内")
        self.assertTrue(all("透明不干胶" in item["description"] for item in result["options"]))
        self.assertNotIn("哑粉纸", " ".join(item["description"] for item in result["options"]))
        result = self.agent.choose("balanced")
        self.assertEqual(result["order"]["paper"], "")

    def test_structural_product_cannot_generate_without_specialized_fields(self):
        self.agent.chat("做 2000 张透明不干胶标签，50*50mm，四色印刷，下周内")
        self.agent.choose("balanced")
        result = self.agent.generate()
        self.assertFalse(result["orderGenerated"])
        self.assertIn("品类参数", result["messages"][0])

    def test_llm_config_helpers_validate_and_protect_saved_config(self):
        self.assertEqual(normalize_base_url("https://example.com/v1/"), "https://example.com/v1")
        with self.assertRaises(ValueError):
            normalize_base_url("example.com/v1")
        with self.assertRaises(ValueError):
            normalize_base_url("https://example.com/v1?api_key=secret")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "llm_config.json"
            write_saved_config(path, {"url": "https://example.com/v1", "model": "demo", "key": "secret"})
            self.assertEqual(read_saved_config(path)["model"], "demo")
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_reading_product_requires_pages_before_generate(self):
        self.agent.chat("做 500 份 A4 宣传册，157g哑粉纸，双面四色，下周内")
        self.agent.choose("balanced")
        result = self.agent.generate()
        self.assertFalse(result["orderGenerated"])
        self.assertIn("页数", result["messages"][0])

    def test_llm_planner_keeps_history_and_parses_fenced_json(self):
        planner = OpenAICompatiblePlanner("https://example.com/v1", "test-key", "demo", timeout=1)
        response = {"choices": [{"message": {"content": '```json\n{"reply":"请补充数量","patch":{}}\n```'}}]}
        with mock_patch("urllib.request.urlopen", return_value=FakeResponse(response)) as request:
            plan = planner.plan("做宣传册", {"productType": "宣传册"}, [], [{"role": "user", "text": "想做一本画册"}])
        self.assertEqual(plan["reply"], "请补充数量")
        payload = json.loads(request.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "想做一本画册"})
        self.assertEqual(payload["messages"][-1]["role"], "user")

    def test_llm_planner_accepts_plain_text_instead_of_silent_fallback(self):
        planner = OpenAICompatiblePlanner("https://example.com/v1", "", "demo", timeout=1)
        with mock_patch("urllib.request.urlopen", return_value=FakeResponse({"choices": [{"message": {"content": "可以，先确认成品尺寸。"}}]})):
            plan = planner.plan("我想做宣传册", {}, [])
        self.assertEqual(plan["reply"], "可以，先确认成品尺寸。")
        self.assertEqual(planner.last_error, "")

    def test_llm_planner_retries_transient_connection_once(self):
        planner = OpenAICompatiblePlanner("https://example.com/v1", "", "demo", timeout=1)
        with mock_patch(
            "urllib.request.urlopen",
            side_effect=[URLError("temporary"), FakeResponse({"choices": [{"message": {"content": "重试成功"}}]})],
        ) as request:
            plan = planner.plan("请继续", {}, [])
        self.assertEqual(plan["reply"], "重试成功")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(planner.last_error, "")

    def test_agent_returns_llm_reply_and_trace_without_key(self):
        class StubPlanner:
            enabled = True
            model = "demo"
            last_error = ""

            def plan(self, text, order, tools, history):
                self.history = history
                return {"reply": "我记住了这个项目。请告诉我数量。", "patch": {"productType": "宣传册"}}

            def public_config(self):
                return {"enabled": True, "url": "https://example.com/v1", "model": self.model, "keyConfigured": False, "lastError": ""}

        planner = StubPlanner()
        agent = Agent(self.memory, planner=planner)
        first = agent.chat("我需要一本企业画册")
        second = agent.chat("用于客户拜访")
        self.assertIn("我记住了这个项目", first["messages"][0])
        self.assertIn("调用模型：demo", first["toolTrace"])
        self.assertEqual(planner.history[-1]["text"], "我记住了这个项目。请告诉我数量。")
        self.assertTrue(second["llm"]["enabled"])

    def test_llm_agent_closes_tool_loop_and_keeps_tool_result(self):
        class ToolPlanner:
            enabled = True
            model = "demo"
            last_error = ""

            def __init__(self):
                self.calls = []

            def plan(self, text, order, tools, history, tool_result=None):
                self.calls.append(tool_result)
                if tool_result is None:
                    return {"reply": "", "patch": {}, "tool": {"name": "recommend_processes", "arguments": {}}}
                return {"reply": "我已经比较了三种工艺，综合方案更适合当前订单。", "patch": {}, "tool": None}

            def public_config(self):
                return {"enabled": True, "url": "https://example.com/v1", "model": self.model,
                        "keyConfigured": False, "lastError": ""}

        planner = ToolPlanner()
        result = Agent(self.memory, planner=planner).chat(
            "做 500 份 A4 名片，250g铜版纸，双面四色，下周内"
        )
        self.assertEqual(len(planner.calls), 2)
        self.assertEqual(len(result["options"]), 3)
        self.assertIn("比较了三种工艺", result["messages"][0])
        self.assertIn("调用模型总结工具结果：demo", result["toolTrace"])
        self.assertEqual([item["role"] for item in result["history"]], ["user", "assistant"])

    def test_tool_payload_shape_does_not_crash_agent(self):
        result = self.agent.call_tool("explain_print_term", ["错误参数"])
        self.assertIn("JSON 对象", result["messages"][0])

    def test_model_connection_test_is_safe_and_reports_empty_content(self):
        planner = OpenAICompatiblePlanner("https://example.com/v1", "test-key", "demo", timeout=1)
        with mock_patch("urllib.request.urlopen", return_value=FakeResponse({"choices": [{"message": {"content": "OK"}}]})) as request:
            result = planner.test_connection()
        self.assertTrue(result["ok"])
        self.assertIsInstance(result["latencyMs"], int)
        payload = request.call_args.args[0]
        self.assertTrue(payload.headers.get("Authorization", "").startswith("Bearer "))
        self.assertNotIn("test-key", payload.data.decode("utf-8"))

        with mock_patch("urllib.request.urlopen", return_value=FakeResponse({"choices": []})):
            result = planner.test_connection()
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "模型接口返回空内容")

    def test_invalid_constructor_url_is_not_exposed(self):
        planner = OpenAICompatiblePlanner("https://user:hidden@example.com/v1", "key", "demo", timeout=1)
        self.assertFalse(planner.enabled)
        self.assertNotIn("hidden", str(planner.public_config()))

    def test_agent_keeps_chat_alive_when_planner_raises(self):
        class BrokenPlanner:
            enabled = True
            model = "demo"
            last_error = ""

            def plan(self, *_args):
                raise RuntimeError("provider-specific failure")

            def public_config(self):
                return {"enabled": True, "url": "https://example.com/v1", "model": self.model, "keyConfigured": False, "lastError": self.last_error}

        result = Agent(self.memory, planner=BrokenPlanner()).chat("我想做一张海报。")
        self.assertTrue(result["messages"])
        self.assertIn("模型回退：模型调用异常", result["toolTrace"])


if __name__ == "__main__":
    unittest.main()
