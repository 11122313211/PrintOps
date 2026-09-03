import tempfile
import os
import unittest
from unittest.mock import patch as mock_patch
import json
from pathlib import Path

from agent import Agent, Memory, validate_order
from llm_adapter import OpenAICompatiblePlanner, normalize_base_url, read_saved_config, write_saved_config


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

    def test_memory_survives_new_agent(self):
        self.agent.chat("做 100 份 A5 名片，一周内拿到", {"paper": "250g 铜版纸", "printing": "双面四色"})
        restored = Agent(self.memory, self.agent.id).snapshot()
        self.assertEqual(restored["order"]["productType"], "名片")
        self.assertEqual(restored["order"]["quantity"], "100 份")
        self.assertEqual(restored["toolTrace"], ["恢复会话记忆"])

    def test_snapshot_includes_chat_history_for_ui_restore(self):
        self.agent.chat("做 100 份 A4 名片，一周内拿到", {"paper": "250g 铜版纸", "printing": "双面四色"})
        restored = Agent(self.memory, self.agent.id).snapshot()
        self.assertEqual([item["role"] for item in restored["history"]], ["user", "assistant"])
        self.assertIn("100 份", restored["history"][1]["text"])

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

    def test_booklet_validation_flags_saddle_stitch_page_conflict(self):
        self.agent.chat("做 A4 画册 200页，250g铜版纸，双面四色，下周内，骑马钉")
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

    def test_generate_is_idempotent_after_draft_exists(self):
        self.agent.chat("做 500 份 A4 名片，250g铜版纸，双面四色，下周内")
        self.agent.choose("balanced")
        first = self.agent.generate()
        second = self.agent.generate()
        self.assertTrue(first["orderGenerated"])
        self.assertTrue(second["orderGenerated"])
        self.assertIn("已经生成", second["messages"][0])

    def test_platform_is_not_hardcoded(self):
        result = self.agent.set_platform("shengda")
        self.assertEqual(result["order"]["platform"], "shengda")
        result = self.agent.set_platform("unknown")
        self.assertEqual(result["order"]["platform"], "generic")

    def test_common_printing_language_reaches_tool_stage(self):
        result = self.agent.chat("做 500 册 A4 宣传册，157克哑粉纸，双面彩印，三天后要用，预算不要太高")
        self.assertEqual(result["stage"], "recommend")
        self.assertEqual(result["order"]["quantity"], "500 份")
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
