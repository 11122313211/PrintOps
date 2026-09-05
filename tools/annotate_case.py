"""真实脱敏订单用例的标注辅助工具（门槛 4）。

把一条（或多轮）脱敏订单原话转换成可直接粘贴进 tests/eval_cases_real.json
的用例草稿：Agent 的字段提取结果只作为"待核对建议"预填 expected，人工核对
修改后才算标注。工具只打印到控制台，不读写语料文件——真实语料的每一行都
必须经过人工确认，否则评测就失去了证据效力。

系统默认值（source=system）与方案带入值（source=recommendation）不进入建议，
只保留用户显式输入（user）、规则识别（rule）与模型推断（model）来源的字段。

用法：
    python tools/annotate_case.py --name "真实-名片001" \
        --turn "帮客户做500张A4名片，250克铜版纸，双面，下周要" \
        [--turn "数量改成1200张"] [--tag 真实]

多产品订单会输出 items.N.<字段> 形式的路径。空字段不会出现在 expected 里。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import Agent, Memory  # noqa: E402
from order_model import LABELS  # noqa: E402

SUGGESTED_SOURCES = {"user", "rule", "model"}


def _order_fields(order: dict, prefix: str, meta_prefix: str) -> list[tuple[str, str, str]]:
    """Flatten an order into (eval-path, suggested-value, provenance-key) triples."""
    pairs: list[tuple[str, str, str]] = []
    for key in LABELS:
        if key == "productType":
            continue
        value = order.get(key)
        if isinstance(value, str) and value.strip():
            pairs.append((f"{prefix}{key}", value.strip(), f"{meta_prefix}{key}"))
    for name, value in (order.get("dimensions") or {}).items():
        if isinstance(value, str) and value.strip():
            pairs.append((f"{prefix}dimensions.{name}", value.strip(),
                          f"{meta_prefix}dimensions.{name}"))
    for name, value in (order.get("productSpecs") or {}).items():
        if isinstance(value, str) and value.strip():
            pairs.append((f"{prefix}productSpecs.{name}", value.strip(),
                          f"{meta_prefix}productSpecs.{name}"))
    return pairs


def build_case_draft(name: str, turns: list[str], tag: str = "真实",
                     agent_factory: type[Agent] = Agent) -> dict:
    """Run the deterministic agent over the turns and return a case draft.

    The ``expected`` values are *suggestions* from field extraction — every
    value must be human-reviewed before the case counts as a real annotation.
    """
    with tempfile.TemporaryDirectory(prefix="printops-annotate-") as directory:
        agent = agent_factory(Memory(Path(directory) / "agent.sqlite3"))
        final: dict = {}
        for text in turns:
            final = agent.chat(text)
    field_meta = agent.state.get("fieldMeta") or {}

    def suggested(meta_key: str) -> bool:
        meta = field_meta.get(meta_key)
        return bool(meta) and meta.get("source") in SUGGESTED_SOURCES

    order = final.get("order") or {}
    expected: dict[str, str] = {}
    items = order.get("items")
    if isinstance(items, list) and items:
        for index, item in enumerate(items):
            item_id = item.get("itemId") or f"item-{index + 1}"
            for path, value, meta_key in _order_fields(item, f"items.{index}.", f"items.{item_id}."):
                if suggested(meta_key):
                    expected[path] = value
    else:
        for path, value, meta_key in _order_fields(order, "", ""):
            if suggested(meta_key):
                expected[path] = value
    product = order.get("productType") or "、".join(
        str(item.get("productType")) for item in (items or []) if item.get("productType"))
    if product:
        expected["productType"] = product
    return {"name": name, "tag": tag,
            "turns": [{"text": text} for text in turns],
            "expected": expected}


def main() -> int:
    parser = argparse.ArgumentParser(description="生成真实脱敏用例草稿（expected 为待人工核对的建议值）")
    parser.add_argument("--name", required=True, help="用例唯一名称，例如 真实-名片001")
    parser.add_argument("--tag", default="真实", help="分组标签，默认 真实")
    parser.add_argument("--turn", required=True, action="append", dest="turns",
                        help="一轮用户原话；多轮对话重复本参数（按顺序）")
    args = parser.parse_args()

    draft = build_case_draft(args.name, args.turns, args.tag)
    print("== 字段对照（均为 Agent 提取的建议值，逐项核对后再保留；未提及的字段请删除）==")
    for path, value in draft["expected"].items():
        print(f"  {path} = {value}")
    if any(path.startswith("items.") for path in draft["expected"]):
        print("\n提示：多产品订单仅预填有逐项来源记录（items.* provenance）的字段；"
              "其余字段请按原话人工补全 items.0.<字段> 路径。")
    print("\n== 核对后粘贴进 tests/eval_cases_real.json 的 cases 数组 ==")
    print(json.dumps(draft, ensure_ascii=False, indent=2))
    print("\n注意：本工具不读写语料文件；未核对的值不要留在语料里。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
