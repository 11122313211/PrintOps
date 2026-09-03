"""Small, dependency-free print order agent.

Flow: perceive -> remember -> plan -> call tools -> respond.
The contracts are intentionally compatible with a future LangGraph/FastAPI layer.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import json
import re
import sqlite3
import unicodedata
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from product_knowledge import find_product, parameter_state, profile_for


ORDER_DEFAULTS = {
    "productType": "", "purpose": "", "quantity": "", "size": "",
    "pages": "", "orientation": "", "paper": "", "printing": "", "finishing": "", "binding": "",
    "deadline": "", "budget": "", "platform": "generic", "productSpecs": {},
}
REQUIRED = ["productType", "quantity", "size", "paper", "printing", "deadline"]
HISTORY_LIMIT = 80
MAX_PLANNER_TOOL_ROUNDS = 2
RECOMMENDATION_FIELDS = {"productType", "quantity", "size", "pages", "orientation", "paper", "printing", "finishing", "binding", "deadline", "budget", "productSpecs"}
LABELS = {
    "productType": "印刷品", "purpose": "使用场景", "quantity": "数量",
    "size": "成品尺寸", "pages": "页数", "orientation": "版式方向", "paper": "纸张/材料", "printing": "印刷颜色",
    "finishing": "表面工艺", "binding": "装订/后道", "deadline": "交期",
    "budget": "预算偏好", "platform": "目标平台",
}
MATERIAL_SPEC_PRODUCTS = {"标签", "手提袋", "纸杯", "海报", "喷画", "PVC", "PVC卡"}
def required_order_keys(order: dict[str, Any]) -> list[str]:
    """Return base fields that make sense for the selected product family."""
    product = order.get("productType")
    return [key for key in REQUIRED if not (key == "paper" and product in MATERIAL_SPEC_PRODUCTS)]


class Memory:
    """SQLite-backed session memory; survives server restarts."""

    def __init__(self, path: str | Path = "data/agent.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL)")

    def load(self, session_id: str) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT state FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return self.fresh_state()
        state = json.loads(row[0])
        order = deepcopy(ORDER_DEFAULTS)
        order.update(state.get("order") or {})
        state["order"] = order
        state.setdefault("messages", [])
        state.setdefault("stage", "collect")
        state.setdefault("selectedOption", None)
        state.setdefault("orderGenerated", False)
        state.setdefault("uploadedFile", None)
        return state

    def save(self, session_id: str, state: dict[str, Any]) -> None:
        data = json.dumps(state, ensure_ascii=False)
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?)", (session_id, data))

    @staticmethod
    def fresh_state() -> dict[str, Any]:
        return {"order": deepcopy(ORDER_DEFAULTS), "messages": [], "stage": "collect",
                "selectedOption": None, "orderGenerated": False, "uploadedFile": None}

    @contextmanager
    def _db(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            yield db


PLATFORMS = {
    "generic": {"name": "通用印刷平台", "mode": "export", "capabilities": ["标准订单导出"]},
    "shengda": {"name": "盛大印刷", "mode": "manual", "capabilities": ["订单草稿", "人工询价"]},
    "platform_a": {"name": "平台 A", "mode": "adapter", "capabilities": ["字段映射", "订单草稿"]},
    "platform_b": {"name": "平台 B", "mode": "adapter", "capabilities": ["字段映射", "订单草稿"]},
    "supplier": {"name": "自定义供应商", "mode": "adapter", "capabilities": ["字段映射"]},
}

Tool = Callable[..., Any]
TOOLS: dict[str, Tool] = {}
TOOL_META: dict[str, dict[str, Any]] = {}


def tool(fn: Tool) -> Tool:
    TOOLS[fn.__name__] = fn
    TOOL_META[fn.__name__] = {"name": fn.__name__, "description": (fn.__doc__ or "").strip()}
    return fn


@tool
def recommend_processes(order: dict[str, Any]) -> list[dict[str, str]]:
    """根据用途、预算、交期和工艺约束生成可比较的候选方案。"""
    product = order.get("productType") or "印刷品"
    profile = profile_for(product)
    premium = order.get("budget") == "优先视觉质感" or product in {"包装盒", "邀请函"}
    fast = order.get("budget") == "优先交期" or order.get("deadline") in {"今天", "明天", "后天", "一周内"}
    known_paper = order.get("paper") or ""
    printing = order.get("printing") or "四色印刷"
    format_note = f"{order.get('size')}，{order.get('pages')}" if order.get("size") and order.get("pages") else order.get("size") or "按成品尺寸确认"
    binding = order.get("binding") or profile.get("defaultBinding", "无需装订")
    profile_note = profile.get("recommendation", "先确认用途、尺寸、材料、颜色、数量和交期。")
    specs = order.get("productSpecs") or {}
    material_profiles = {
        "标签": (specs.get("labelMaterial") or "铜版不干胶", ("无特殊表面工艺", "按面材适配上光 / 覆膜", "白墨 / 专色")),
        "手提袋": (specs.get("bagMaterial") or "250g 白卡纸", ("无特殊表面工艺", "覆膜", "烫金 / 专色")),
        "纸杯": (specs.get("cupMaterial") or "食品级淋膜纸", ("无特殊表面工艺", "食品级上光", "专色")),
        "海报": (specs.get("displayMaterial") or "海报纸 / 背胶", ("无特殊表面工艺", "覆膜", "高精度输出")),
        "喷画": (specs.get("displayMaterial") or "户外灯布", ("无特殊表面工艺", "户外防护", "高精度输出")),
        "PVC": ("PVC 板材", ("无特殊表面工艺", "覆面 / 背胶", "高精度输出")),
        "PVC卡": ("PVC 卡基", ("无特殊表面工艺", "覆膜", "专色 / 编码")),
    }
    if product in material_profiles:
        material, finishes = material_profiles[product]
        materials = (material, material, material)
    elif product == "包装盒":
        material = known_paper if known_paper not in {"", "待推荐"} else "350g 白卡纸"
        materials = (material, material, material)
        finishes = ("无特殊表面工艺", "哑膜", "烫金 / 击凸")
    else:
        material = known_paper if known_paper not in {"", "待推荐"} else "157g 哑粉纸"
        materials = (material, "200g 铜版纸", "特种纸" if premium else "高克重纸张")
        finishes = ("无特殊工艺", "哑膜", "烫金 / 击凸")
    return [
        {"id": "economy", "title": "经济方案", "description": f"{materials[0]} + {printing}，{format_note}，{finishes[0]}，{binding}。",
         "cost": "成本较低", "lead": "交期较快" if fast else "交期稳定", "score": "适合控预算",
         "reason": f"{profile_note}适合预算敏感的项目。", "risk": "视觉层次和耐磨性相对基础。",
         "paper": materials[0], "finishing": finishes[0], "binding": binding},
        {"id": "balanced", "title": "平衡方案", "description": f"{materials[1]} + {printing}，{finishes[1]}，{binding}，兼顾效果与生产稳定性。",
         "cost": "成本中等", "lead": "交期稳定", "score": "综合推荐",
         "reason": f"{profile_note}在颜色表现、手感和成本之间取平衡。", "risk": "覆膜或复杂后道会增加少量加工时间和费用。",
         "paper": materials[1], "finishing": finishes[1], "binding": binding},
        {"id": "premium", "title": "质感方案", "description": f"{materials[2]} + {printing}，{finishes[2]}，{binding}，强化品牌表现。",
         "cost": "成本较高", "lead": "需要确认加急" if fast else "交期较长", "score": "视觉优先",
         "reason": f"{profile_note}适合品牌发布和需要触感记忆点的物料。", "risk": "需要专色、打样、结构或后道工艺确认。",
         "paper": materials[2], "finishing": finishes[2], "binding": binding},
    ]


def _number(value: str) -> int | None:
    match = re.search(r"\d[\d,]*(?:\.\d+)?", value or "")
    return int(float(match.group(0).replace(",", ""))) if match else None


@tool
def explain_print_term(question: str) -> dict[str, str]:
    """解释常见印刷术语，帮助非专业用户做选择。"""
    text = question.lower()
    if any(term in text for term in ("哑粉", "铜版", "纸张", "纸怎么选", "纸材")):
        answer = "哑粉纸颜色柔和、反光少，适合阅读型宣传册；铜版纸色彩更鲜亮、表面更光滑，适合图片和营销物料；不确定时可先用平衡方案。"
        topic = "纸张选择"
    elif any(term in text for term in ("出血", "安全边", "裁切")):
        answer = "出血是画面超出成品裁切线的区域，常规建议四边各 3mm；文字和标志要放在安全边距内，避免裁切后贴边。"
        topic = "出血与安全边"
    elif any(term in text for term in ("覆膜", "哑膜", "亮膜")):
        answer = "哑膜触感细腻、反光少，适合高端和阅读场景；亮膜颜色更亮、耐磨性好，适合促销物料。覆膜会增加一点成本和交期。"
        topic = "覆膜选择"
    elif any(term in text for term in ("四色", "专色", "印刷颜色", "黑白")):
        answer = "四色印刷适合大多数彩色文件；专色适合品牌色和高一致性要求；黑白/单色成本较低，适合文字资料。"
        topic = "颜色模式"
    elif any(term in text for term in ("装订", "骑马钉", "胶装")):
        answer = "骑马钉适合页数较少的宣传册，摊平性好；胶装适合页数较多、需要更正式的画册；包装盒通常需要糊盒而不是书刊装订。"
        topic = "装订方式"
    else:
        answer = "我可以解释纸张、出血、颜色、覆膜和装订。你也可以直接告诉我用途、数量、尺寸和交期，我会替你做选择。"
        topic = "印刷基础"
    return {"topic": topic, "answer": answer, "next": "如果愿意，我可以把这个偏好直接写入当前订单。"}


@tool
def preflight_file(file_name: str, size_bytes: int) -> dict[str, Any]:
    """检查文件类型和大小，返回基础印前预检结果。"""
    if not file_name.lower().endswith(".pdf"):
        return {"ok": False, "message": "MVP 暂只支持 PDF 文件。"}
    if size_bytes > 20 * 1024 * 1024:
        return {"ok": False, "message": "文件超过 20 MB，请压缩后再上传。"}
    return {"ok": True, "message": "基础检查通过；出血、颜色和字体仍需正式印前检查。"}


@tool
def prepare_handoff(order: dict[str, Any]) -> dict[str, Any]:
    """按目标平台生成标准化订单交接文本。"""
    platform = PLATFORMS.get(order["platform"], PLATFORMS["generic"])
    fields = [(key, LABELS[key]) for key in LABELS if key != "platform"]
    lines = [f"目标平台：{platform['name']}"] + [f"{label}：{order[key] or '未填写'}" for key, label in fields]
    profile = parameter_state(order)
    if profile["parameters"]:
        lines.append(f"品类分类：{profile['category']}")
        lines.append("品类参数：")
        lines.extend(f"- {item['label']}：{item['value'] or '未填写'}" for item in profile["parameters"])
    text = "\n".join(lines)
    return {"platform": platform, "text": text, "productProfile": profile, "requiresHumanConfirmation": True}


@tool
def estimate_price(order: dict[str, Any]) -> dict[str, Any]:
    """根据订单字段估算价格区间，不替代印刷厂正式报价。"""
    missing = [LABELS[key] for key in ("productType", "quantity", "size", "printing") if not order.get(key)]
    if missing:
        return {"type": "estimate", "range": None, "missing": missing,
                "assumptions": "至少需要印刷品、数量、尺寸和印刷颜色后才能估算。", "requiresHumanConfirmation": True}
    quantity = _number(order.get("quantity", "")) or 500
    base = 180 if order.get("productType") in {"名片", "折页", "单页"} else 520
    if order.get("productType") in {"包装盒", "手提袋", "纸杯", "标签"}:
        base *= 1.35
    unit = max(0.35, base / max(quantity, 1))
    if order.get("finishing") and order["finishing"] not in {"无特殊工艺", "待推荐"}: unit *= 1.35
    low, high = round(quantity * unit * 0.85), round(quantity * unit * 1.25)
    return {"type": "estimate", "range": f"¥{low} - ¥{high}", "assumptions": "按常规纸张、四色印刷和当前数量估算，未含运输及特殊打样。", "requiresHumanConfirmation": True}


@tool
def validate_order(order: dict[str, Any]) -> dict[str, Any]:
    """校验订单字段完整性，输出阻塞项、风险和下一步建议。"""
    required_keys = required_order_keys(order)
    missing = [LABELS[key] for key in required_keys if not order.get(key)]
    warnings = []
    suggestions = []
    profile = parameter_state(order)
    product_missing = [item["label"] for item in profile["missing"]]
    if order.get("productType") in {"宣传册", "画册"} and not order.get("binding"):
        warnings.append("宣传册/画册尚未确认装订方式")
        suggestions.append("页数少于 48 页可优先考虑骑马钉，页数较多再考虑胶装")
    if order.get("finishing") in {"烫金", "烫金 / 击凸"}:
        warnings.append("烫金需要确认文件专色、线条粗细和加急交期")
    if order.get("printing") == "双面四色" and order.get("productType") in {"宣传册", "画册"} and not order.get("pages"):
        suggestions.append("补充页数后才能准确判断装订和纸张克重")
    if order.get("size") and any(re.fullmatch(r"\d+×\d+(?:×\d+)?", part) for part in order["size"].split(" / ")):
        warnings.append("自定义尺寸未注明单位，请确认是 mm 还是 cm")
    if product_missing:
        warnings.append(f"{order.get('productType') or '该品类'}还需确认：{'、'.join(product_missing)}")
        suggestions.append(profile["missing"][0]["question"])
    if order.get("productType") == "包装盒" and order.get("size") and not (order.get("productSpecs") or {}).get("boxSize"):
        suggestions.append("包装盒请补充长×宽×高，并区分内尺寸/外尺寸")
    if order.get("productType") in {"喷画", "海报", "PVC"} and (order.get("productSpecs") or {}).get("install"):
        if "户外" in str((order.get("productSpecs") or {}).get("install")) and order.get("productType") == "海报":
            warnings.append("户外展示请确认介质耐候性与安装安全")
    quantity = _number(order.get("quantity", ""))
    if quantity is not None and quantity <= 0:
        warnings.append("数量必须大于 0")
    readiness = round((len(required_keys) - len(missing)) / len(required_keys) * 100) if required_keys else 100
    return {"ok": not missing and quantity != 0, "missing": missing, "productMissing": product_missing,
            "productProfile": profile, "warnings": warnings, "suggestions": suggestions,
            "readiness": readiness, "productReadiness": profile["readiness"]}


class Agent:
    def __init__(self, memory: Memory, session_id: str | None = None, planner: Any = None) -> None:
        self.memory, self.id, self.planner = memory, session_id or uuid.uuid4().hex[:12], planner
        self.state = memory.load(self.id)
        self.trace: list[str] = []

    def chat(self, text: str, patch: dict[str, str] | None = None) -> dict[str, Any]:
        self.trace = ["感知需求"]
        perceived = self._perceive(text)
        if patch is not None and not isinstance(patch, dict):
            patch = {}
        perceived.update({key: value for key, value in (patch or {}).items() if key in ORDER_DEFAULTS})
        self._update_order(perceived)
        self._remember("user", text)

        # An optional LLM planner improves language understanding while field
        # patches and tool names remain constrained by this Agent.
        if self.planner and getattr(self.planner, "enabled", False):
            self.trace.append(f"调用模型：{getattr(self.planner, 'model', '已配置模型')}")
            plan = self._ask_planner(text)
            last_tool_name = ""
            last_tool_response: dict[str, Any] | None = None
            final_reply = ""
            for tool_round in range(MAX_PLANNER_TOOL_ROUNDS):
                if not isinstance(plan, dict):
                    break
                self._apply_plan(plan)
                planned_tool = plan.get("tool")
                if not isinstance(planned_tool, dict) or planned_tool.get("name") not in TOOLS:
                    if plan.get("reply"):
                        final_reply = str(plan["reply"])
                    break
                tool_name = str(planned_tool["name"])
                if tool_name == last_tool_name:
                    self.trace.append(f"阻止重复工具：{tool_name}")
                    break
                last_tool_name = tool_name
                last_tool_response = self.call_tool(
                    tool_name, planned_tool.get("arguments", {}), preserve_trace=True, remember=False,
                )
                if tool_round + 1 >= MAX_PLANNER_TOOL_ROUNDS:
                    break
                self.trace.append(f"调用模型总结工具结果：{getattr(self.planner, 'model', '已配置模型')}")
                plan = self._ask_planner(text, {"name": tool_name, "result": last_tool_response.get("toolResult")})
            if last_tool_response is not None:
                result = last_tool_response.get("toolResult")
                message = final_reply.strip() or self._planner_tool_fallback(last_tool_name, result)
                self._remember("assistant", message)
                self._save()
                return self._result(
                    [message], options=last_tool_response.get("options", []),
                    handoff=last_tool_response.get("handoff"), tool_result=result,
                )
            if final_reply:
                self.state["stage"] = "collect" if self._missing_fields() else "recommend"
                self._remember("assistant", final_reply)
                self._save()
                return self._result([final_reply])
            if getattr(self.planner, "last_error", ""):
                self.trace.append(f"模型回退：{self.planner.last_error}")

        if self._is_explanation_request(text):
            result = self._call("explain_print_term", text)
            message = f"{result['topic']}：{result['answer']}\n\n{result['next']}"
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=result)

        # Explicit intents let users invoke a tool before the order is complete.
        if any(term in text for term in ("多少钱", "价格", "报价", "预算估算")):
            result = self._call("estimate_price", self.state["order"])
            message = (f"按当前已填写信息，费用只能做区间估算：{result['range']}。\n{result['assumptions']}"
                       if result["range"] else f"我调用了费用估算工具，但信息还不足。\n还需要：{'、'.join(result['missing'])}。")
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=result)
        if any(term in text for term in ("检查订单", "校验订单", "还缺什么", "检查一下")):
            result = self._call("validate_order", self.state["order"])
            message = self._validation_message(result)
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=result)
        if any(term in text for term in ("生成订单", "生成草稿", "下单")):
            self._save()
            return self.generate()

        missing = self._missing_fields()
        if missing:
            self.state["stage"] = "collect"
            validation = self._call("validate_order", self.state["order"])
            message = f"{self._summary()}\n\n还需要确认：{self._question(missing[0])}"
            quick = self._quick_replies(missing[0])
            options: list[dict[str, str]] = []
            tool_result: Any = validation
        else:
            self.state["stage"] = "recommend"
            options = self._call("recommend_processes", self.state["order"])
            profile = parameter_state(self.state["order"])
            quick = self._product_quick_replies(profile["missing"][0]["key"]) if profile["missing"] else []
            product_note = (f"基础订单信息已经齐了。为了让{profile.get('category', '该品类')}对接更准确，建议补充：{profile['missing'][0]['question']}"
                            if profile["missing"] else "信息已经齐了。")
            message = f"{self._summary()}\n\n{product_note}\n我调用工艺推荐工具生成了 3 个可执行方案，请选择一个。"
            tool_result = options
        self._remember("assistant", message)
        self._save()
        return self._result([message], quick, options, tool_result=tool_result)

    def call_tool(self, name: str, payload: dict[str, Any] | None = None, preserve_trace: bool = False,
                  remember: bool = True) -> dict[str, Any]:
        """Public tool gateway used by a UI, MCP bridge, or an LLM planner."""
        if not preserve_trace:
            self.trace = []
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            self.trace.append(f"工具参数无效：{name}")
            return self._tool_reply("工具参数需要使用 JSON 对象，未执行。", remember=remember)
        if name == "recommend_processes":
            result = self._call(name, self.state["order"])
            self.state["stage"] = "recommend"
            return self._tool_reply("已调用工艺推荐工具。", options=result, tool_result=result, remember=remember)
        if name == "validate_order":
            result = self._call(name, self.state["order"])
            return self._tool_reply(self._validation_message(result), tool_result=result, remember=remember)
        if name == "explain_print_term":
            result = self._call(name, str(payload.get("question", "印刷工艺怎么选？")))
            message = f"{result['topic']}：{result['answer']}\n\n{result['next']}"
            return self._tool_reply(message, tool_result=result, remember=remember)
        if name == "estimate_price":
            result = self._call(name, self.state["order"])
            message = (f"已调用费用估算工具：{result['range']}。" if result["range"]
                       else f"费用估算工具需要更多信息：{'、'.join(result['missing'])}。")
            return self._tool_reply(message, tool_result=result, remember=remember)
        if name == "prepare_handoff":
            result = self._call(name, self.state["order"])
            return self._tool_reply("已调用订单交接工具，生成平台适配文本。", tool_result=result, handoff=result, remember=remember)
        if name == "preflight_file":
            try:
                size_bytes = int(payload.get("sizeBytes", 0))
            except (TypeError, ValueError):
                size_bytes = 0
            result = self._call(name, str(payload.get("fileName", "")), size_bytes)
            if result.get("ok"):
                self.state["uploadedFile"] = payload.get("fileName")
            return self._tool_reply(result["message"], tool_result=result, remember=remember)
        return self._tool_reply(f"工具 {name} 不在白名单中，未执行。", remember=remember)

    def choose(self, option_id: str) -> dict[str, Any]:
        self.trace = []
        validation = self._call("validate_order", self.state["order"])
        if not validation["ok"]:
            return self._result([f"还不能选择方案。{self._validation_message(validation)}"], tool_result=validation)
        options = self._call("recommend_processes", self.state["order"])
        option = next((item for item in options if item["id"] == option_id), None)
        if not option:
            return self._result(["没有找到这个方案。"], [], options)
        self.state["selectedOption"] = option_id
        updates = {"finishing": option["finishing"], "binding": option.get("binding", self.state["order"]["binding"])}
        if self.state["order"].get("productType") not in MATERIAL_SPEC_PRODUCTS:
            updates["paper"] = option["paper"]
        self.state["order"].update(updates)
        message = f"已选择{option['title']}，订单参数已更新。"
        self._remember("assistant", message)
        self._save()
        return self._result([message], [], options)

    def generate(self) -> dict[str, Any]:
        self.trace = []
        if self.state["orderGenerated"]:
            return self._result(["订单草稿已经生成，正式提交前请继续人工确认。"])
        validation = self._call("validate_order", self.state["order"])
        if not validation["ok"]:
            message = f"订单还不能生成。{self._validation_message(validation)}"
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=validation)
        if validation.get("productMissing"):
            missing = "、".join(validation["productMissing"])
            message = f"订单还不能生成。{self.state['order']['productType']}还缺少品类参数：{missing}。先补充后再生成交接单。"
            self._remember("assistant", message)
            self._save()
            return self._result([message], tool_result=validation)
        if not self.state["selectedOption"]:
            options = self._call("recommend_processes", self.state["order"])
            message = "请先选择工艺方案，再生成订单草稿。"
            self._remember("assistant", message)
            self._save()
            return self._result([message], [], options, tool_result=options)
        handoff = self._call("prepare_handoff", self.state["order"])
        self.state.update({"stage": "confirm", "orderGenerated": True})
        message = "订单草稿已生成。正式提交前仍需要人工确认价格、文件和交期。"
        self._remember("assistant", message)
        self._save()
        return self._result([message], [], [], handoff)

    def upload(self, file_name: str, size_bytes: int) -> dict[str, Any]:
        self.trace = []
        check = self._call("preflight_file", file_name, size_bytes)
        if check["ok"]:
            self.state["uploadedFile"] = file_name
            self._save()
        return self._result([check["message"]])

    def set_platform(self, platform_id: str) -> dict[str, Any]:
        platform_id = platform_id if platform_id in PLATFORMS else "generic"
        self.state["order"]["platform"] = platform_id
        self._save()
        return self._result([f"目标平台已切换为{PLATFORMS[platform_id]['name']}。订单核心字段保持不变。"])

    def snapshot(self) -> dict[str, Any]:
        options = self._call("recommend_processes", self.state["order"]) if self.state["stage"] != "collect" else []
        self.trace = ["恢复会话记忆"] if self.state["messages"] else []
        return self._result([], [], options)

    def _call(self, name: str, *args: Any) -> Any:
        self.trace.append(f"调用工具：{name}")
        return TOOLS[name](*args)

    def _result(self, messages: list[str], quick: list[dict[str, Any]] | None = None,
                options: list[dict[str, str]] | None = None, handoff: dict[str, Any] | None = None,
                tool_result: Any = None) -> dict[str, Any]:
        validation = validate_order(self.state["order"])
        return {"sessionId": self.id, "messages": messages, "quickReplies": quick or [], "options": options or [],
                "order": deepcopy(self.state["order"]), "stage": self.state["stage"],
                "selectedOption": self.state["selectedOption"], "orderGenerated": self.state["orderGenerated"],
                "uploadedFile": self.state["uploadedFile"], "toolTrace": self.trace,
                "availableTools": self.available_tools(), "toolResult": tool_result, "handoff": handoff,
                "history": deepcopy(self.state["messages"]), "readiness": validation["readiness"],
                "missingFields": validation["missing"],
                "llm": self.planner.public_config() if self.planner and hasattr(self.planner, "public_config") else None,
                "productProfile": parameter_state(self.state["order"]), "nextAction": self._next_action(validation)}

    def _tool_reply(self, message: str, remember: bool = True, **kwargs: Any) -> dict[str, Any]:
        if remember:
            self._remember("assistant", message)
        self._save()
        return self._result([message], **kwargs)

    @staticmethod
    def available_tools() -> list[dict[str, Any]]:
        return list(TOOL_META.values())

    def _summary(self) -> str:
        order = self.state["order"]
        parts = [f"{LABELS[key]}：{order[key]}" for key in ("productType", "quantity", "size", "pages", "orientation", "paper", "printing", "finishing", "binding", "deadline", "budget") if order.get(key)]
        profile = parameter_state(order)
        spec_parts = [f"{item['label']}：{item['value']}" for item in profile["parameters"] if item.get("value")]
        if spec_parts:
            parts.append("关键参数：" + "、".join(spec_parts[:3]))
        return "我理解的是：" + "；".join(parts) + "。" if parts else "我还没有识别到有效订单信息。"

    def _missing_fields(self) -> list[str]:
        return [key for key in required_order_keys(self.state["order"]) if not self.state["order"].get(key)]

    def _next_action(self, validation: dict[str, Any]) -> str:
        if validation["missing"]:
            return f"下一步：补充{validation['missing'][0]}"
        if validation.get("productMissing"):
            return f"下一步：确认{validation['productMissing'][0]}（{self.state['order'].get('productType') or '当前品类'}专属参数）"
        if not self.state["selectedOption"]:
            return "下一步：比较并选择工艺方案"
        if not self.state["orderGenerated"]:
            return "下一步：确认方案后生成订单草稿"
        return "下一步：人工确认文件、价格和交期"

    def _update_order(self, changes: dict[str, Any]) -> set[str]:
        previous_product = self.state["order"].get("productType")
        valid: dict[str, Any] = {}
        for key, value in changes.items():
            if key not in ORDER_DEFAULTS or value is None:
                continue
            if key == "productSpecs" and isinstance(value, dict):
                specs = {str(name): str(item).strip() for name, item in value.items() if str(item).strip()}
                if specs:
                    valid[key] = {**(self.state["order"].get("productSpecs") or {}), **specs}
            elif str(value).strip():
                valid[key] = str(value).strip()
        changed = {key for key, value in valid.items() if self.state["order"].get(key) != value}
        self.state["order"].update(valid)
        if "productType" in changed and previous_product and previous_product != self.state["order"].get("productType"):
            # Product-specific fields belong to the old item and must not leak into a new draft.
            self.state["order"]["productSpecs"] = {}
            changed.add("productSpecs")
        if changed & RECOMMENDATION_FIELDS:
            self.state["selectedOption"] = None
            self.state["orderGenerated"] = False
            if self.state["stage"] == "confirm": self.state["stage"] = "recommend"
        return changed

    @staticmethod
    def _is_explanation_request(text: str) -> bool:
        return bool(re.search(r"怎么选|如何选|什么区别|有什么区别|是什么|解释|为什么", text)) and bool(
            re.search(r"纸|出血|安全边|裁切|覆膜|哑膜|亮膜|专色|四色|黑白|装订|骑马钉|胶装|工艺", text, re.I)
        )

    @staticmethod
    def _validation_message(result: dict[str, Any]) -> str:
        if result["ok"] and not result["warnings"]:
            suffix = "；".join(result.get("suggestions", []))
            return f"订单字段目前完整（信息度 {result.get('readiness', 100)}%），暂未发现规则警告。" + (f"\n建议：{suffix}" if suffix else " 可以调用工艺推荐工具。")
        parts = []
        if result["missing"]: parts.append("还缺少：" + "、".join(result["missing"]))
        if result["warnings"]: parts.append("需要确认：" + "；".join(result["warnings"]))
        if result.get("suggestions"): parts.append("建议：" + "；".join(result["suggestions"]))
        return "。".join(parts) + "。"

    def _apply_plan(self, plan: dict[str, Any]) -> None:
        patch = plan.get("patch") if isinstance(plan, dict) else None
        if not isinstance(patch, dict):
            return
        self._update_order(patch)

    def _ask_planner(self, text: str, tool_result: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Call a provider with bounded context; provider failures stay inside the Agent."""
        history = self.state["messages"][:-1]
        try:
            if tool_result is None:
                return self.planner.plan(text, self.state["order"], self.available_tools(), history)
            try:
                return self.planner.plan(text, self.state["order"], self.available_tools(), history, tool_result=tool_result)
            except TypeError:
                # Keep compatibility with an older custom planner implementation.
                return self.planner.plan(text, self.state["order"], self.available_tools(), history)
        except Exception:
            if hasattr(self.planner, "last_error"):
                self.planner.last_error = "模型调用异常"
            return None

    def _planner_tool_fallback(self, name: str, result: Any) -> str:
        """Give the user a useful answer if the second synthesis call fails."""
        if name == "recommend_processes":
            return "已生成工艺方案，请在右侧比较效果、成本、交期和注意事项。"
        if name == "validate_order" and isinstance(result, dict):
            return self._validation_message(result)
        if name == "explain_print_term" and isinstance(result, dict):
            return f"{result.get('topic', '印刷基础')}：{result.get('answer', '')}\n\n{result.get('next', '')}".strip()
        if name == "estimate_price" and isinstance(result, dict):
            return (f"按当前信息，费用区间为：{result['range']}。\n{result['assumptions']}"
                    if result.get("range") else f"费用估算还需要：{'、'.join(result.get('missing', []))}。")
        if name == "prepare_handoff":
            return "订单交接信息已准备好，正式提交前请人工确认价格、文件和交期。"
        if isinstance(result, dict) and result.get("message"):
            return str(result["message"])
        return "工具已完成处理，请查看订单面板中的结果。"

    def _remember(self, role: str, text: str) -> None:
        self.state["messages"] = (self.state["messages"] + [{"role": role, "text": text}])[-HISTORY_LIMIT:]

    def _save(self) -> None:
        self.memory.save(self.id, self.state)

    @staticmethod
    def _perceive(text: str) -> dict[str, Any]:
        # Normalize full-width input copied from design tools or IMEs first.
        text = unicodedata.normalize("NFKC", text or "")
        data: dict[str, Any] = {}
        product = find_product(text)
        # Keep invitation cards from the original MVP as a lightweight family.
        if not product and "邀请函" in text:
            product = "邀请函"
        if product:
            data["productType"] = product
        size_matches = Agent._extract_sizes(text)
        size_spans = [(match.start(), match.end()) for match, _ in size_matches]
        explicit_quantity = re.search(r"(?:数量|印刷量|印多少|做多少)\s*(?:改成|改为|调整为|为|是)?\s*(\d[\d,]*(?:\.\d+)?)\s*(万|千|百|份|本|册|张|个|件|盒|套|包)?", text)
        if explicit_quantity:
            count = float(explicit_quantity.group(1).replace(",", "")) * {"万": 10000, "千": 1000, "百": 100}.get(explicit_quantity.group(2) or "", 1)
            data["quantity"] = f"{int(count)} 份"
        for match in re.finditer(r"(?:约|大约|需要|印刷|做)?\s*(\d[\d,]*(?:\.\d+)?)\s*(万|千|百|份|本|册|张|个|件|盒|套|包)?", text):
            # Dimension numbers (including B4's 4) are never quantities.
            number_start, number_end = match.span(1)
            if any(start <= number_start and number_end <= end for start, end in size_spans):
                continue
            unit = match.group(2) or ("份" if re.search(r"印|做|需要|数量", text) else "")
            next_chars = text[match.end():].lstrip()
            if unit and (match.group(2) or not next_chars.startswith(("x", "X", "×", "*", "\\"))):
                count = float(match.group(1).replace(",", "")) * {"万": 10000, "千": 1000, "百": 100}.get(unit, 1)
                data["quantity"] = f"{int(count)} 份"
                break
        if size_matches:
            normalized_sizes = []
            for _, size in size_matches:
                if size not in normalized_sizes:
                    normalized_sizes.append(size)
            data["size"] = " / ".join(normalized_sizes)
        if match := re.search(r"(?:共|约|大约)?\s*(\d{1,3})\s*(?:页|P(?![A-Za-z]))", text, re.I):
            data["pages"] = f"{match.group(1)} 页"
        if re.search(r"横版|横向|横式", text): data["orientation"] = "横版"
        elif re.search(r"竖版|竖向|纵向|直式", text): data["orientation"] = "竖版"
        paper_match = re.search(r"(\d{2,3})\s*(?:g|克)\s*(哑粉纸|铜版纸|白卡纸|牛皮纸|胶版纸)", text, re.I)
        if paper_match:
            data["paper"] = f"{paper_match.group(1)}g {paper_match.group(2)}"
        else:
            for item in ["特种纸", "牛皮纸", "白卡纸", "铜版纸", "哑粉纸", "胶版纸"]:
                if item in text: data["paper"] = item; break
        if re.search(r"双面.*?(?:四色|彩印)?|两面", text): data["printing"] = "双面四色"
        elif re.search(r"单面.*?(?:四色|彩印)?", text): data["printing"] = "单面四色"
        elif "四色" in text or "彩色" in text or "彩印" in text: data["printing"] = "四色印刷"
        elif "黑白" in text or "单色" in text: data["printing"] = "单色印刷"
        finish_names = ["哑膜", "亮膜", "覆膜", "烫金", "烫银", "局部UV", "局部 UV", "击凸", "压凹", "上光"]
        removed_finishes = [item for item in finish_names if re.search(rf"(?:不要|不需要|无需|不用|去掉|取消).{{0,5}}{re.escape(item)}", text, re.I)]
        finishes = [item for item in finish_names if item.lower() in text.lower() and item not in removed_finishes]
        if removed_finishes and not finishes: data["finishing"] = "无特殊工艺"
        elif finishes: data["finishing"] = "、".join(dict.fromkeys(finishes))
        if "骑马钉" in text: data["binding"] = "骑马钉"
        elif "胶装" in text: data["binding"] = "胶装"
        elif "锁线" in text: data["binding"] = "锁线胶装"
        elif re.search(r"(?:不要|不需要|无需|不用|去掉|取消).{0,5}装订", text): data["binding"] = "无需装订"
        if budget_match := re.search(r"预算\s*(?:控制在|不超过|约|为)?\s*([\d,]+)\s*[元块]", text):
            data["budget"] = f"预算 ¥{budget_match.group(1).replace(',', '')}"
        elif re.search(r"低预算|便宜|控制成本|经济|不超过\s*\d+\s*[元块]", text): data["budget"] = "优先控制成本"
        elif re.search(r"高级|质感|精致|有档次", text): data["budget"] = "优先视觉质感"
        elif re.search(r"赶|尽快|明天|后天|下周|三天|两天", text): data["budget"] = "优先交期"
        if match := re.search(r"(今天|明天|后天|(?:三|两|一|四|五|六|七)天(?:后|内)?|\d+\s*天(?:后|内)?|本周[一二三四五六日天]?|下周[一二三四五六日天]?|月底|\d{1,2}月\d{1,2}日)", text):
            data["deadline"] = re.sub(r"\s+", "", match.group(1))
        if "宣传" in text or "推广" in text or "活动" in text: data["purpose"] = "品牌宣传"
        platform_aliases = {"盛大": "shengda", "平台A": "platform_a", "平台 B": "platform_b", "平台B": "platform_b"}
        for phrase, platform in platform_aliases.items():
            if phrase in text: data["platform"] = platform; break
        if not product and re.search(r"天地盖|抽屉盒|折叠盒|飞机盒|开窗盒", text):
            product = data["productType"] = "包装盒"
        specs = Agent._extract_product_specs(text, product, data.get("size", ""))
        if specs:
            data["productSpecs"] = specs
        return data

    @staticmethod
    def _extract_product_specs(text: str, product: str, size: str) -> dict[str, str]:
        """Extract only the product-specific details understood by the MVP catalog."""
        specs: dict[str, str] = {}

        def set_if(pattern: str, key: str, value: str | None = None, flags: int = re.I) -> None:
            match = re.search(pattern, text, flags)
            if match:
                specs[key] = value or match.group(1)

        fold = re.search(r"(二折|三折|四折|对折|风琴折|荷包折|卷折)", text)
        if fold:
            specs["folding"] = fold.group(1)
        parts = re.search(r"([二三四五六])\s*联", text)
        if parts:
            specs["paperParts"] = f"{parts.group(1)}联"
        if re.search(r"流水号|连续编号|打号码|编号", text):
            specs["numbering"] = "需要连续编号"
        if re.search(r"(?:不要|不需要|无需|不用).{0,4}(?:编号|流水号)", text):
            specs["numbering"] = "不需要编号"

        structure = re.search(r"(天地盖|抽屉盒|折叠盒|飞机盒|书型盒|开窗盒|异型盒)", text)
        if structure:
            specs["boxStructure"] = structure.group(1)
        dimensions = [value for _, value in Agent._extract_sizes(text)]
        three_dimensions = next((value for value in dimensions if value.count("×") >= 2), "")
        if product == "包装盒" or structure:
            if three_dimensions:
                specs["boxSize"] = three_dimensions
            if "刀模" in text or "刀线" in text:
                specs["dieCut"] = "需确认刀模文件" if re.search(r"没有|无|未有|需要制作", text) else "已有/提供刀模文件"
        if product == "手提袋" and three_dimensions:
            specs["bagSize"] = three_dimensions
        if product == "信封封套" and size:
            specs["envelopeSize"] = size
        if product in {"海报", "喷画", "PVC"} and size:
            specs["displaySize" if product != "PVC" else "boardSize"] = size

        material_terms = ["铜版不干胶", "透明不干胶", "牛皮纸不干胶", "PET", "PVC", "热敏纸", "不干胶"]
        material = next((term for term in material_terms if term.lower() in text.lower()), "")
        if material and product == "标签":
            specs["labelMaterial"] = material
        shape = re.search(r"(方形|圆形|椭圆形|异形|圆角)", text)
        if shape and product == "标签":
            specs["labelShape"] = shape.group(1)
        adhesive = re.search(r"(可移胶|强粘胶|普通胶|冷冻胶|可移除胶)", text)
        if adhesive and product == "标签":
            specs["adhesive"] = adhesive.group(1)

        bag_material = next((term for term in ["无纺布", "帆布", "牛皮纸", "白卡纸"] if term in text), "")
        if bag_material and product == "手提袋":
            specs["bagMaterial"] = bag_material
        handle = re.search(r"(棉绳|扁绳|丝带|尼龙绳|手挽绳|穿绳)", text)
        if handle and product == "手提袋":
            specs["handle"] = handle.group(1)
        if product == "手提袋" and "承重" in text:
            load = re.search(r"承重\s*(\d+(?:\.\d+)?)\s*(?:kg|公斤|千克)?", text, re.I)
            specs["loadBearing"] = f"{load.group(1)}kg" if load else "需确认承重"

        volume = re.search(r"(\d+(?:\.\d+)?)\s*(?:ml|毫升)", text, re.I)
        if volume and product == "纸杯":
            specs["cupVolume"] = f"{volume.group(1)}ml"
        cup_material = re.search(r"(单\s*PE|双\s*PE|食品级纸杯纸)", text, re.I)
        if cup_material and product == "纸杯":
            specs["cupMaterial"] = re.sub(r"\s+", " ", cup_material.group(1)).upper() if "PE" in cup_material.group(1).upper() else cup_material.group(1)
        if product == "纸杯" and re.search(r"不需要?淋膜|无淋膜", text):
            specs["innerCoating"] = "不需要内淋膜"
        elif product == "纸杯" and "淋膜" in text:
            specs["innerCoating"] = "需要内淋膜"

        display_material = next((term for term in ["背胶", "灯片", "车贴", "灯布", "相纸", "写真布", "KT板", "刀刮布", "PVC"] if term.lower() in text.lower()), "")
        if display_material and product in {"海报", "喷画"}:
            specs["displayMaterial"] = display_material
        if product == "PVC":
            thickness = re.search(r"(?:板材厚度|厚度|PVC)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(?:mm|毫米)", text, re.I)
            if thickness:
                specs["boardThickness"] = f"{thickness.group(1)}mm"
        install = next((term for term in ["墙面张贴", "墙面", "裱板", "展架", "易拉宝", "打孔", "包边", "挂装", "支架", "户外", "室内"] if term in text), "")
        if install and product in {"海报", "喷画", "PVC"}:
            specs["install"] = install
        distance = re.search(r"(?:观看距离|距离)\s*(\d+(?:\.\d+)?)\s*(米|m)", text, re.I)
        if distance and product == "喷画":
            specs["viewingDistance"] = f"{distance.group(1)}米"

        if product == "名片":
            if "圆角" in text: specs["cardCorners"] = "圆角"
            elif "直角" in text: specs["cardCorners"] = "直角"
            if "专色" in text: specs["cardColor"] = "专色"
        if product == "PVC卡":
            card_type = re.search(r"(智能卡|人像证卡|滴胶卡|冲切卡|异形卡)", text)
            if card_type: specs["cardType"] = card_type.group(1)
            thickness = re.search(r"(?:卡片厚度|卡厚|厚度)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(?:mm|毫米)", text, re.I)
            if not thickness:
                thickness = re.search(r"(?<![\d×x*])((?:0?\.38|0?\.5|0?\.76|0?\.8|0?\.84))\s*(?:mm|毫米)", text, re.I)
            if thickness: specs["cardThickness"] = f"{thickness.group(1)}mm"
            if re.search(r"芯片|磁条|IC卡|ID卡", text, re.I): specs["chip"] = "需要芯片/磁条"
        if product == "吊牌":
            hole = re.search(r"(圆孔|蝴蝶孔|挂孔|打孔)", text)
            if hole: specs["hangHole"] = hole.group(1)
            string = re.search(r"(棉绳|扁绳|丝带|尼龙绳|别针|配绳)", text)
            if string: specs["string"] = string.group(1)
        if "出血" in text and product in {"单页", "折页", "名片", "宣传册", "画册"}:
            bleed = re.search(r"出血\s*(\d+(?:\.\d+)?)\s*(?:mm|毫米)?", text, re.I)
            specs["bleed"] = f"{bleed.group(1)}mm" if bleed else "需确认出血"
        opening = re.search(r"(上开口|侧开口|自封|胶条封口|不干胶封口)", text)
        if opening and product == "信封封套":
            specs["opening"] = opening.group(1)
        if product == "信封封套" and "开口" in text and "opening" not in specs:
            specs["opening"] = "需确认开口方向"
        if product == "数码印刷":
            if re.search(r"可变数据|每份不同|个性化|流水号", text): specs["variableData"] = "需要可变数据"
            if "打样" in text: specs["proofing"] = "需要打样"
        return specs

    @staticmethod
    def _extract_sizes(text: str) -> list[tuple[re.Match[str], str]]:
        """Find standard or custom finished sizes and return normalized labels."""
        separator = r"(?:[x×✕✖]|(?:\\)?\*)"
        dimension = (
            rf"\d{{2,4}}\s*(?:mm|毫米|cm|厘米)?\s*{separator}\s*"
            rf"\d{{2,4}}\s*(?:mm|毫米|cm|厘米)?"
            rf"(?:\s*{separator}\s*\d{{2,4}}\s*(?:mm|毫米|cm|厘米)?)?"
        )
        pattern = re.compile(
            rf"(?<![A-Za-z0-9])(?:[AB]\s*-?\s*[3-6](?!\d)|{dimension})"
            rf"(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        found: list[tuple[re.Match[str], str]] = []
        for match in pattern.finditer(text):
            compact = re.sub(r"\s+", "", match.group(0)).upper()
            if re.fullmatch(r"[AB]-?[3-6]", compact):
                size = compact.replace("-", "")
            else:
                size = compact.replace("毫米", "MM").replace("厘米", "CM")
                size = size.replace("\\*", "×").replace("*", "×")
                size = size.replace("X", "×").replace("✕", "×").replace("✖", "×")
                size = re.sub(r"(?:MM|CM)(?=×)", "", size)
            found.append((match, size))
        return found

    @staticmethod
    def _question(key: str) -> str:
        return {"productType": "你想做哪一种印刷品？", "quantity": "大约需要多少份？", "size": "成品尺寸是多少？",
                "paper": "对纸张有偏好吗？不确定可以选按效果推荐。", "printing": "需要单面、双面还是黑白印刷？",
                "deadline": "什么时候需要拿到成品？"}[key]

    @staticmethod
    def _quick_replies(key: str) -> list[dict[str, Any]]:
        choices = {
            "productType": [("宣传册", "宣传册"), ("折页", "折页"), ("名片", "名片"), ("包装盒", "包装盒")],
            "quantity": [("100 份", "100 份"), ("500 份", "500 份"), ("1,000 份", "1000 份")],
            "size": [("A4", "A4"), ("A5", "A5"), ("210 × 285 mm", "210×285MM")],
            "paper": [("按效果推荐", "待推荐"), ("157g 哑粉纸", "157g 哑粉纸"), ("250g 铜版纸", "250g 铜版纸")],
            "printing": [("双面四色", "双面四色"), ("单面四色", "单面四色"), ("黑白/单色", "单色印刷")],
            "deadline": [("一周内", "一周内"), ("两周内", "两周内"), ("时间不紧", "时间不紧")],
        }
        return [{"label": label, "data": {key: value}} for label, value in choices.get(key, [])]

    @staticmethod
    def _product_quick_replies(key: str) -> list[dict[str, Any]]:
        choices = {
            "folding": [("二折", "二折"), ("三折", "三折"), ("风琴折", "风琴折")],
            "paperParts": [("二联", "二联"), ("三联", "三联"), ("四联", "四联")],
            "boxStructure": [("天地盖", "天地盖"), ("抽屉盒", "抽屉盒"), ("折叠盒", "折叠盒")],
            "labelMaterial": [("铜版不干胶", "铜版不干胶"), ("透明不干胶", "透明不干胶"), ("PET", "PET")],
            "labelShape": [("方形", "方形"), ("圆形", "圆形"), ("异形", "异形")],
            "cardType": [("智能卡", "智能卡"), ("人像证卡", "人像证卡"), ("滴胶卡", "滴胶卡")],
            "cardThickness": [("0.38mm", "0.38mm"), ("0.76mm", "0.76mm"), ("其他厚度", "需确认卡片厚度")],
            "hangHole": [("圆孔", "圆孔"), ("蝴蝶孔", "蝴蝶孔"), ("打孔", "打孔")],
            "string": [("棉绳", "棉绳"), ("扁绳", "扁绳"), ("不需要配绳", "无需配绳")],
            "bagMaterial": [("白卡纸", "白卡纸"), ("牛皮纸", "牛皮纸"), ("无纺布", "无纺布")],
            "handle": [("棉绳", "棉绳"), ("扁绳", "扁绳"), ("丝带", "丝带")],
            "cupMaterial": [("单 PE", "单 PE"), ("双 PE", "双 PE")],
            "innerCoating": [("需要内淋膜", "需要内淋膜"), ("不需要内淋膜", "不需要内淋膜")],
            "displayMaterial": [("背胶", "背胶"), ("灯片", "灯片"), ("车贴", "车贴")],
            "install": [("墙面张贴", "墙面张贴"), ("裱板", "裱板"), ("打孔包边", "打孔包边")],
            "boardThickness": [("3mm", "3mm"), ("5mm", "5mm"), ("10mm", "10mm")],
        }
        return [{"label": label, "data": {"productSpecs": {key: value}}} for label, value in choices.get(key, [])]
