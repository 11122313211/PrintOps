"""TD-08 双轨行为差分断言（V5-01）。

此前 111+12 例评测与绝大多数单测只走规则轨，LLM 轨仅有协议级测试；"同一句话
两轨行为差异"没有任何断言，换提示词/模型的影响不可知。本文件对同一句话各跑
一次规则轨（``Agent(memory)``，无 planner）与 LLM 轨（``Agent(memory,
planner=ScriptedPlanner(...))``，tests.test_planner_loop 的离线替身），逐组钉住：

- **等价面**（必须一致）：订单字段值（productType/quantity/dimensions/
  productSpecs.* 等两轨最终 state 字段）、``missingFields``、``workflowStage``；
- **白名单差异面**（允许不同、逐项钉住防意外漂移）：``fieldMeta.*.source``
  （rule vs model）、confidence 数值、消息文案不同源、toolTrace/toolReceipts
  形态、planner 意图抢跑（post_planner 被 planner 轮取代时结果消息来自 planner）。

用例与 ``Agent.PLANNER_INTENT_PATTERNS`` 六意图对齐（agent.py:186-193）：
quote/price/capability/validation/recommendation/handoff 各 1 + 否定 1 +
多产品 1 + 缺字段追问 2。每个测试方法 docstring 的第一句说明"这组钉的是什么漂移"。

已知白名单外分歧（真实行为分歧，报告用）：
- 完整订单估价：规则轨 ``_handle_price`` 直接调 ``_estimate_reference``，
  不置工作流态（workflowStage 保持 recommend）；planner 轨经
  ``call_tool("estimate_price")`` 会置 ``workflowStage="quote"``
  （agent.py call_tool 的 estimate_price 分支）。两轨订单与估算结果等价，
  仅信封 workflowStage 不同——见 test_price_intent_scripts_estimate_tool。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent import Agent, Memory
from tests.test_planner_loop import ScriptedPlanner, final_plan, tool_plan


READY_TEXT = "做 500 张 A4 名片，250g铜版纸，双面四色，下周内"

# 等价面：两轨最终订单状态必须一致的标量字段（缺省视为 ""）。
ORDER_FIELDS = (
    "productType", "purpose", "quantity", "quantityValue", "quantityUnit",
    "size", "pages", "orientation", "paper", "printing", "finishing",
    "binding", "deadline", "budget", "platform",
)


class DualTrackEquivalenceTest(unittest.TestCase):
    """同一句话、两轨跑分、等价面必须一致，白名单差异面逐项钉住。"""

    # ---------------------------------------------------------------- helpers

    def _dual(self, text: str, plans: list, setup=None):
        """Run ``text`` through both tracks on fresh, identical sessions.

        剧本 planner 在 setup 轮之后才挂上（沿用 test_planner_loop 的
        ``agent.planner = ScriptedPlanner(...)`` 习惯），保证 setup 轮不消费剧本，
        剧本全部留给被差分的那一轮。
        """
        temporary = tempfile.TemporaryDirectory()
        try:
            rule_agent = Agent(Memory(Path(temporary.name) / "rule.sqlite3"))
            llm_agent = Agent(Memory(Path(temporary.name) / "llm.sqlite3"))
            for agent in (rule_agent, llm_agent):
                if setup:
                    setup(agent)
            planner = ScriptedPlanner(*plans)
            llm_agent.planner = planner
            rule_envelope = rule_agent.chat(text)
            llm_envelope = llm_agent.chat(text)
            return rule_envelope, llm_envelope, planner
        finally:
            temporary.cleanup()

    def _assert_equivalent(self, rule, llm, *, workflow_stage: bool = True,
                           fields: tuple = ORDER_FIELDS) -> None:
        """等价面断言：订单字段、missingFields（、workflowStage）必须一致。"""
        for key in fields:
            self.assertEqual(rule["order"].get(key, ""), llm["order"].get(key, ""),
                             f"订单字段 {key} 两轨不一致")
        for key in ("dimensions", "productSpecs"):
            self.assertEqual(rule["order"].get(key) or {}, llm["order"].get(key) or {},
                             f"嵌套字段 {key} 两轨不一致")
        self.assertEqual(rule["missingFields"], llm["missingFields"],
                         "missingFields 两轨不一致")
        if workflow_stage:
            self.assertEqual(rule["workflowStage"], llm["workflowStage"],
                             "workflowStage 两轨不一致")

    @staticmethod
    def _meta_projection(field_meta: dict) -> dict:
        """fieldMeta 的稳定投影：只留 source/confidence。

        完整 entry 还带 runId/updatedAt/evidence 等易变字段，跨会话天然不同，
        不能直接整体比较；差分只钉 provenance 语义面。
        """
        return {key: {"source": value.get("source"), "confidence": value.get("confidence")}
                for key, value in field_meta.items()}

    def _assert_planner_ran(self, planner: ScriptedPlanner, text: str) -> None:
        """planner 轮确实接手了这一轮（第一笔 plan 调用的 text 就是原话）。"""
        self.assertGreaterEqual(len(planner.calls), 1)
        self.assertEqual(planner.calls[0]["text"], text)

    def _assert_rule_track_shape(self, rule) -> None:
        """规则轨形态钉：post_planner 直派不经 planner，无模型调用、无回执账本。"""
        self.assertNotIn("调用模型", " ".join(rule["toolTrace"]))
        self.assertEqual(rule["toolReceipts"], [])

    # ----------------------------------------------------------------- cases

    def test_quote_intent_asks_for_supplier_quote(self) -> None:
        """钉：quote 意图——等价面一致；消息文案两轨不同源（规则 handler 直读
        quote_draft.message vs planner 回执兜底文案）、planner 抢跑 post_planner、
        回执只存在于 planner 轨。"""
        def setup(agent: Agent) -> None:
            agent.chat(READY_TEXT)
            agent.choose("balanced")

        rule, llm, planner = self._dual(
            "请报价", plans=[None], setup=setup)

        self._assert_equivalent(rule, llm)
        self._assert_rule_track_shape(rule)
        self._assert_planner_ran(planner, "请报价")
        # 白名单差异 1：消息文案不同源（规则 handler 的草稿文案 vs planner 回执兜底）。
        self.assertIn("已生成本地询价草稿", rule["messages"][0])
        self.assertIn("已准备询价请求", llm["messages"][0])
        self.assertNotEqual(rule["messages"][0], llm["messages"][0])
        # 白名单差异 2：toolTrace 形态（planner 轨有模型调用与最终总结）。
        self.assertIn("调用模型：scripted", llm["toolTrace"])
        self.assertIn("调用模型最终总结：scripted", llm["toolTrace"])
        self.assertIn("调用工具：request_supplier_quote", llm["toolTrace"])
        self.assertIn("调用工具：request_supplier_quote", rule["toolTrace"])
        # 白名单差异 3：toolReceipts 形态（planner 轨记录回执账本，规则轨为空）。
        self.assertEqual([item["name"] for item in llm["toolReceipts"]],
                         ["request_supplier_quote"])
        # 等价面补充：两轨拿到的询价草稿本体一致（status 同源）。
        self.assertIsInstance(rule["toolResult"], dict)
        self.assertEqual(rule["toolResult"].get("status"),
                         llm["toolResult"].get("status"))

    def test_price_intent_scripts_estimate_tool(self) -> None:
        """钉：price 意图（剧本让模型调 estimate_price 再合成总结）——估算结果、
        订单与 workflowStage（TD-25 偿还后两轨均置 quote）全等价；结果消息来自
        planner 合成（抢跑 post_planner）。"""
        def setup(agent: Agent) -> None:
            agent.chat(READY_TEXT)

        rule, llm, planner = self._dual(
            "费用是多少",
            plans=[tool_plan("estimate_price"), final_plan("模型已完成估价总结。")],
            setup=setup)

        # 等价面（含 workflowStage：TD-25 已偿还，两轨均置 quote）。
        self._assert_equivalent(rule, llm)
        self.assertEqual(rule["workflowStage"], "quote")
        self.assertEqual(llm["workflowStage"], "quote")
        # 等价面补充：估算区间完全一致（同一本地估价内核）。
        self.assertEqual(rule["toolResult"]["type"], "estimate")
        self.assertEqual(rule["toolResult"].get("range"), llm["toolResult"].get("range"))
        # 白名单差异 1：消息文案来源——planner 合成抢跑 post_planner 的规则文案。
        self.assertEqual(llm["messages"][0], "模型已完成估价总结。")
        self.assertNotEqual(rule["messages"][0], llm["messages"][0])
        # 白名单差异 2：回执账本与工具轨迹形态。
        self.assertEqual([item["name"] for item in llm["toolReceipts"]],
                         ["estimate_price"])
        self.assertEqual(llm["toolReceipts"][0]["status"], "ready")
        self._assert_rule_track_shape(rule)
        self._assert_planner_ran(planner, "费用是多少")

    def test_capability_intent_message_wording_is_path_independent(self) -> None:
        """钉：capability 意图——两轨消息文案恰好同串（规则 handler 文案 vs
        planner 回执兜底文案），这一"巧合级契约"钉住防任一侧文案漂移；
        模型只回文字时本地强制能力匹配工具（安全网）。"""
        rule, llm, planner = self._dual("能印吗", plans=[None])

        self._assert_equivalent(rule, llm)
        self._assert_rule_track_shape(rule)
        self._assert_planner_ran(planner, "能印吗")
        # 消息等价（两条不同代码路径产出同一文案）。
        self.assertEqual(rule["messages"][0], llm["messages"][0])
        self.assertEqual(rule["messages"][0],
                         "已完成供应商能力匹配，请查看支持项与待确认项。")
        # 白名单差异：回执账本只存在于 planner 轨；强制工具由本地意图兜底。
        self.assertEqual([item["name"] for item in llm["toolReceipts"]],
                         ["match_supplier_capability"])
        self.assertIn("调用工具：match_supplier_capability", rule["toolTrace"])
        self.assertIn("调用工具：match_supplier_capability", llm["toolTrace"])
        self.assertIn("调用模型最终总结：scripted", llm["toolTrace"])

    def test_validation_intent_forces_validate_order(self) -> None:
        """钉：validation 意图——模型只回文字时本地强制 validate_order（与规则轨
        同一工具、同一校验结果）；planner 合成消息取代规则校验文案（抢跑）。"""
        rule, llm, planner = self._dual(
            "订单完整吗",
            plans=[tool_plan("validate_order"), final_plan("模型已核对订单信息。")])

        self._assert_equivalent(rule, llm)
        self._assert_rule_track_shape(rule)
        self._assert_planner_ran(planner, "订单完整吗")
        # 等价面补充：校验结果本体一致（同一 validate_order 内核）。
        self.assertEqual(rule["toolResult"].get("missing"),
                         llm["toolResult"].get("missing"))
        # 白名单差异 1：结果消息来自 planner 合成而非规则校验文案。
        self.assertEqual(llm["messages"][0], "模型已核对订单信息。")
        self.assertNotEqual(rule["messages"][0], llm["messages"][0])
        # 白名单差异 2：回执账本形态。
        self.assertEqual([item["name"] for item in llm["toolReceipts"]],
                         ["validate_order"])
        self.assertIn("调用工具：validate_order", rule["toolTrace"])

    def test_recommendation_intent_fallback_wording_differs(self) -> None:
        """钉：recommendation 意图（查询型意图不路由轮次）——空订单下规则轨走
        "兜底追问"文案、planner 轨走"回执兜底"文案，两轨文案不同源且逐串钉住，
        防任一侧静默改写；强制 recommend_processes 的 blocked 回执只在 planner 轨。"""
        rule, llm, planner = self._dual("怎么印", plans=[None])

        self._assert_equivalent(rule, llm)
        self._assert_rule_track_shape(rule)
        self._assert_planner_ran(planner, "怎么印")
        # 白名单差异 1：消息文案不同源（逐串钉住）。
        self.assertTrue(rule["messages"][0].startswith("我还没有识别到有效订单信息。"),
                        rule["messages"][0])
        self.assertEqual(llm["messages"][0],
                         "当前步骤暂不能完成，请先补充：印刷品、数量、成品尺寸、"
                         "纸张/材料、印刷颜色、交期。")
        self.assertNotEqual(rule["messages"][0], llm["messages"][0])
        # 白名单差异 2：blocked 回执只在 planner 轨。
        self.assertEqual([item["name"] for item in llm["toolReceipts"]],
                         ["recommend_processes"])
        self.assertEqual(llm["toolReceipts"][0]["status"], "blocked")
        self.assertIn("调用工具：validate_order", rule["toolTrace"])
        # planner 轨的强制推荐在就绪门处被阻断（轨迹形态与规则轨不同）。
        self.assertIn("工具阻断：recommend_processes", llm["toolTrace"])

    def test_handoff_intent_forces_prepare_handoff(self) -> None:
        """钉：handoff 意图——模型只回文字时本地强制 prepare_handoff（blocked）；
        规则轨"订单还不能生成"文案 vs planner 轨回执兜底文案，不同源逐串钉住。"""
        rule, llm, planner = self._dual("请生成交接单", plans=[None])

        self._assert_equivalent(rule, llm)
        self._assert_rule_track_shape(rule)
        self._assert_planner_ran(planner, "请生成交接单")
        self.assertTrue(rule["messages"][0].startswith("订单还不能生成。"),
                        rule["messages"][0])
        self.assertEqual(llm["messages"][0],
                         "当前步骤暂不能完成，请先补充：印刷品、数量、成品尺寸、"
                         "纸张/材料、印刷颜色、交期。")
        self.assertNotEqual(rule["messages"][0], llm["messages"][0])
        self.assertEqual([item["name"] for item in llm["toolReceipts"]],
                         ["prepare_handoff"])
        self.assertEqual(llm["toolReceipts"][0]["status"], "blocked")

    def test_negated_quote_never_triggers_quote_tool(self) -> None:
        """钉：否定询价（"先不要询价，只检查订单"）——两轨都不得触发
        request_supplier_quote，消息文案等价；planner 轨的 validate_order 回执
        是白名单形态差异。"""
        rule, llm, planner = self._dual(
            "先不要询价，只检查订单", plans=[None])

        self._assert_equivalent(rule, llm)
        self._assert_rule_track_shape(rule)
        self._assert_planner_ran(planner, "先不要询价，只检查订单")
        # 否定在两轨都生效：询价工具一次都不许出现。
        self.assertNotIn("调用工具：request_supplier_quote", rule["toolTrace"])
        self.assertNotIn("调用工具：request_supplier_quote", llm["toolTrace"])
        self.assertEqual([item["name"] for item in llm["toolReceipts"]],
                         ["validate_order"])
        # 消息文案等价（两条路径产出同一校验文案）。
        self.assertEqual(rule["messages"][0], llm["messages"][0])
        self.assertTrue(rule["messages"][0].startswith("还缺少："),
                        rule["messages"][0])

    def test_multi_product_gate_preempts_planner(self) -> None:
        """钉：多产品门先于 planner（agent.py chat 的 multi_products 分支）——
        模型完全无法抢跑，两轨信封零差异（等价面 + 文案 + 轨迹全等），剧本
        planner 一次都不被调用。"""
        plans = [final_plan("模型不应看到这句话。")]
        rule, llm, planner = self._dual(
            "做名片和海报", plans=plans)

        # planner 未被咨询：整单按规则轨多产品门处理。
        self.assertEqual(planner.calls, [])
        self.assertEqual(plans, [final_plan("模型不应看到这句话。")])
        # 等价面：订单本体（含 items 拆分）逐字节一致。
        self.assertEqual(rule["order"], llm["order"])
        self.assertEqual(rule["missingFields"], llm["missingFields"])
        self.assertEqual(rule["workflowStage"], llm["workflowStage"])
        self.assertEqual(rule["workflowStage"], "clarify")
        # 白名单差异为零：文案、fieldMeta（稳定投影）、toolTrace、回执全等。
        self.assertEqual(rule["messages"], llm["messages"])
        self.assertEqual(self._meta_projection(rule["fieldMeta"]),
                         self._meta_projection(llm["fieldMeta"]))
        self.assertEqual(rule["toolTrace"], llm["toolTrace"])
        self.assertEqual(rule["toolReceipts"], llm["toolReceipts"])
        self.assertIn("我识别到多个印刷品", rule["messages"][0])

    def test_incomplete_order_model_patch_matches_rule_fields(self) -> None:
        """钉：缺字段追问 + 模型 patch 等价——"做 500 张名片"下模型 patch 写回
        同一数量（quantity=500张），两轨最终订单字段值一致；fieldMeta 的
        quantity 三键翻转为 model/0.68 是白名单差异（规则轨 rule/0.92-0.95）；
        planner 自己的文字总结被强制工具作废，消息落回回执兜底文案。"""
        patch_plan = {"reply": "模型已登记数量。", "patch": {"quantity": "500张"},
                      "tool": None}
        rule, llm, planner = self._dual(
            "做 500 张名片", plans=[patch_plan])

        self._assert_equivalent(rule, llm)
        self._assert_rule_track_shape(rule)
        self._assert_planner_ran(planner, "做 500 张名片")
        # 白名单差异 1：fieldMeta.source 翻转 + confidence 数值（同值重述也翻牌，
        # 见 _update_order 的 quantity 重述分支）。
        self.assertEqual(rule["fieldMeta"]["quantity"]["source"], "rule")
        self.assertEqual(llm["fieldMeta"]["quantity"]["source"], "model")
        self.assertEqual(rule["fieldMeta"]["quantity"]["confidence"], 0.95)
        self.assertEqual(llm["fieldMeta"]["quantity"]["confidence"], 0.68)
        self.assertEqual(llm["fieldMeta"]["quantityValue"]["source"], "model")
        self.assertEqual(llm["fieldMeta"]["quantityUnit"]["source"], "model")
        # 未被模型触碰的字段，来源两轨一致（稳定投影比较，不含易变的 runId/时间戳）。
        self.assertEqual(self._meta_projection(rule["fieldMeta"])["productType"],
                         self._meta_projection(llm["fieldMeta"])["productType"])
        # 白名单差异 2：planner 的总结被强制工具作废，消息来自回执兜底（校验文案）。
        self.assertEqual(llm["messages"][0],
                         "还缺少：成品尺寸、纸张/材料、印刷颜色、交期。")
        self.assertTrue(rule["messages"][0].startswith("我理解的是："),
                        rule["messages"][0])
        self.assertNotEqual(rule["messages"][0], llm["messages"][0])
        # 白名单差异 3：强制 validate_order 的回执只在 planner 轨。
        self.assertEqual([item["name"] for item in llm["toolReceipts"]],
                         ["validate_order"])

    def test_followup_size_patch_partial_provenance_flip(self) -> None:
        """钉：补字段追问（"尺寸改成A3"）+ 模型 patch 等价——size 归一值
        297×420 mm 两轨一致；白名单差异：模型 patch 把 size 的 provenance 翻成
        model/0.68，但派生的 dimensions.finishedSize 保持 rule/0.9（部分翻转的
        不对称行为逐项钉住防漂移）。"""
        def setup(agent: Agent) -> None:
            agent.chat("做 500 张名片")

        patch_plan = {"reply": "模型已更新尺寸。", "patch": {"size": "A3"},
                      "tool": None}
        rule, llm, planner = self._dual(
            "尺寸改成A3", plans=[patch_plan], setup=setup)

        self._assert_equivalent(rule, llm)
        self.assertEqual(rule["order"]["size"], "297×420 mm")
        self._assert_rule_track_shape(rule)
        self._assert_planner_ran(planner, "尺寸改成A3")
        # 白名单差异 1：size 翻牌（rule/0.9 → model/0.68），数值面等价。
        self.assertEqual(rule["fieldMeta"]["size"]["source"], "rule")
        self.assertEqual(rule["fieldMeta"]["size"]["confidence"], 0.9)
        self.assertEqual(llm["fieldMeta"]["size"]["source"], "model")
        self.assertEqual(llm["fieldMeta"]["size"]["confidence"], 0.68)
        # 白名单差异 2（不对称）：派生维度 provenance 不翻牌，两轨都是 rule。
        self.assertEqual(llm["fieldMeta"]["dimensions.finishedSize"]["source"], "rule")
        self.assertEqual(llm["fieldMeta"]["dimensions.finishedSize"]["confidence"], 0.9)
        # 未被模型触碰的字段保持 rule，两轨一致。
        self.assertEqual(self._meta_projection(rule["fieldMeta"])["quantity"],
                         self._meta_projection(llm["fieldMeta"])["quantity"])
        # 白名单差异 3：消息文案不同源（规则追问 vs planner 回执兜底）。
        self.assertTrue(rule["messages"][0].startswith("我理解的是："),
                        rule["messages"][0])
        self.assertEqual(llm["messages"][0],
                         "还缺少：纸张/材料、印刷颜色、交期。")
        self.assertNotEqual(rule["messages"][0], llm["messages"][0])
        self.assertEqual([item["name"] for item in llm["toolReceipts"]],
                         ["validate_order"])


if __name__ == "__main__":
    unittest.main()
