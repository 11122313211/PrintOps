"""Tests for the annotation helper (tools/annotate_case.py, 门槛 4 support).

The helper must only *suggest* agent-extracted values for human review:
system defaults and recommendation-carried values never enter the draft, and
the tool itself never writes the corpus file.
"""

import copy
import sys
import unittest
from pathlib import Path

_TOOLS_DIR = str(Path(__file__).resolve().parents[1] / "tools")
_ROOT = str(Path(__file__).resolve().parents[1])
for entry in (_TOOLS_DIR, _ROOT):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import annotate_case  # noqa: E402
from order_model import ORDER_DEFAULTS  # noqa: E402


class FakeAgent(annotate_case.Agent):
    """Deterministic stub: no disk I/O, hand-set field provenance."""

    def __init__(self, memory, session_id=None, planner=None):  # noqa: D401
        self.id = session_id or "stub"
        self.planner = planner
        self.state = {"order": copy.deepcopy(ORDER_DEFAULTS),
                      "fieldMeta": {}, "messages": [], "itemOptions": {}}
        self.trace: list[str] = []

    def chat(self, text, patch=None, item_index=None):
        order = self.state["order"]
        order["productType"] = "名片"
        order["quantity"] = "500 张"
        order["size"] = "A4"
        order["paper"] = "250g 铜版纸"
        order["platform"] = "generic"
        meta = self.state["fieldMeta"]
        for key, value in (("quantity", "500 张"), ("size", "A4"), ("paper", "250g 铜版纸")):
            meta[key] = {"value": value, "source": "rule"}
        meta["platform"] = {"value": "generic", "source": "system"}
        return {"order": order}


class BuildCaseDraftTest(unittest.TestCase):
    def test_suggestions_exclude_system_sources_and_keep_reviewed_fields(self):
        draft = annotate_case.build_case_draft("真实-名片001", ["做 500 张 A4 名片"],
                                               agent_factory=FakeAgent)
        self.assertEqual(draft["name"], "真实-名片001")
        self.assertEqual(draft["turns"], [{"text": "做 500 张 A4 名片"}])
        self.assertEqual(draft["expected"]["productType"], "名片")
        self.assertEqual(draft["expected"]["quantity"], "500 张")
        self.assertEqual(draft["expected"]["size"], "A4")
        # system 来源（platform 默认值）不得进入建议
        self.assertNotIn("platform", draft["expected"])

    def test_item_paths_use_item_provenance(self):
        class ItemAgent(FakeAgent):
            def chat(self, text, patch=None, item_index=None):
                order = self.state["order"]
                item = {"itemId": "item-1", "productType": "名片", "quantity": "500 张"}
                order["items"] = [item]
                order["productType"] = ""
                self.state["fieldMeta"]["items.item-1.quantity"] = {
                    "value": "500 张", "source": "user"}
                return {"order": order}

        draft = annotate_case.build_case_draft("真实-多产品", ["做 500 张名片"],
                                               agent_factory=ItemAgent)
        self.assertEqual(draft["expected"].get("items.0.quantity"), "500 张")
        self.assertNotIn("quantity", draft["expected"])

    def test_tool_module_documents_no_write_access(self):
        source = Path(annotate_case.__file__).read_text(encoding="utf-8")
        self.assertNotIn("eval_cases_real.json\", \"w", source.replace(" ", ""))
        self.assertIn("不读写语料文件", source)


if __name__ == "__main__":
    unittest.main()
