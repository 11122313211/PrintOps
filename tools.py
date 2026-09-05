"""Whitelisted agent tools: recommendation, preflight, validation, handoff.

Split from agent.py.  Tools are pure functions over the order model plus the
platform registry; they never perform network writes and never touch session
state directly.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Callable

from order_model import (DIMENSION_LABELS, LABELS, _multi_product_info, _number,
                         _parse_max_size, _parse_size_mm, imposition_hint, required_order_keys)
from product_knowledge import (KNOWLEDGE_VERSION, PRICE_MODEL, PRICE_MODEL_VERSION,
                               parameter_state, profile_for)
from supplier_adapters import PLATFORMS, SUPPLIER_PROFILE_VERSION, get_adapter


Tool = Callable[..., Any]
TOOLS: dict[str, Tool] = {}
TOOL_META: dict[str, dict[str, Any]] = {}

# A small, provider-neutral contract.  The UI can render these contracts and
# an eventual LangGraph/MCP adapter can pass them to a model without coupling
# the core agent to one SDK.  Property descriptions keep the model from
# guessing argument semantics from names alone.
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "recommend_processes": {
        "input": {"type": "object", "properties": {"order": {"type": "object", "description": "当前订单对象（含品类、数量、尺寸、材料等已填字段）"}}, "required": ["order"]},
        "output": {"type": "array", "items": {"type": "object"}},
    },
    "explain_print_term": {
        "input": {"type": "object", "properties": {"question": {"type": "string", "description": "用户的印刷术语问题原文，例如“哑粉纸和铜版纸有什么区别”"}}, "required": ["question"]},
        "output": {"type": "object", "properties": {"topic": {"type": "string"}, "answer": {"type": "string"}}},
    },
    "preflight_file": {
        "input": {"type": "object", "properties": {"fileName": {"type": "string", "description": "PDF 文件名（含扩展名）"}, "sizeBytes": {"type": "integer", "description": "文件大小（字节）"}, "pageCount": {"type": ["integer", "null"], "description": "浏览器解析到的页数；解析失败填 null"}, "inspection": {"type": ["object", "null"], "description": "浏览器本地轻量扫描得到的 PDF 元数据对象"}, "expectedSize": {"type": ["string", "null"], "description": "订单成品尺寸（如 210×285MM），用于与页面框比对"}, "itemIndex": {"type": ["integer", "null"], "description": "多产品订单中绑定文件的产品项下标"}}, "required": ["fileName", "sizeBytes"]},
        "output": {"type": "object", "properties": {"ok": {"type": "boolean"}, "warnings": {"type": "array"}}},
    },
    "validate_order": {
        "input": {"type": "object", "properties": {"order": {"type": "object", "description": "当前订单对象；校验缺失字段、品类参数和生产风险"}}, "required": ["order"]},
        "output": {"type": "object", "properties": {"ok": {"type": "boolean"}, "missing": {"type": "array"}, "risks": {"type": "array"}}},
    },
    "estimate_price": {
        "input": {"type": "object", "properties": {"order": {"type": "object", "description": "当前订单对象；按示例价格参数表估算费用量级"}, "itemIndex": {"type": ["integer", "null"], "description": "多产品订单中要估算的产品项下标"}}, "required": ["order"]},
        "output": {"type": "object", "properties": {"range": {"type": "string"}, "missing": {"type": "array"}}},
    },
    "prepare_handoff": {
        "input": {"type": "object", "properties": {"order": {"type": "object", "description": "当前订单对象；生成面向目标平台的交接文本"}, "itemIndex": {"type": ["integer", "null"], "description": "多产品订单中要交接的产品项下标"}}, "required": ["order"]},
        "output": {"type": "object", "properties": {"text": {"type": "string"}, "supplierReadiness": {"type": "object"}}},
    },
    "match_supplier_capability": {
        "input": {"type": "object", "properties": {"order": {"type": "object", "description": "当前订单对象；与平台能力档案逐字段比对"}, "platformId": {"type": "string", "description": "目标平台 ID，可选值见 /api/platforms"}, "itemIndex": {"type": ["integer", "null"], "description": "多产品订单中要匹配的产品项下标"}}, "required": ["order"]},
        "output": {"type": "object", "properties": {"status": {"type": "string"}, "supported": {"type": "array"}, "needsReview": {"type": "array"}, "unsupported": {"type": "array"}}},
    },
    "request_supplier_quote": {
        "input": {"type": "object", "properties": {"order": {"type": "object", "description": "当前订单对象；准备待人工确认的询价请求，不会发送给供应商"}, "platformId": {"type": "string", "description": "目标平台 ID，可选值见 /api/platforms"}, "itemIndex": {"type": ["integer", "null"], "description": "多产品订单中要询价的产品项下标"}}, "required": ["order"]},
        "output": {"type": "object", "properties": {"status": {"type": "string"}, "requestId": {"type": "string"}, "mappedOrder": {"type": "object"}, "requiresHumanConfirmation": {"type": "boolean"}}},
    },
}


def tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    TOOLS[fn.__name__] = fn
    TOOL_META[fn.__name__] = {"name": fn.__name__, "description": (fn.__doc__ or "").strip()}
    return fn


@tool
def recommend_processes(order: dict[str, Any]) -> list[dict[str, str]]:
    """根据用途、预算、交期和工艺约束生成可比较的候选方案。"""
    if _multi_product_info(order):
        # Keep the low-level tool contract as an array for existing callers;
        # Agent.call_tool adds the structured blocked response and explanation.
        return []
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
        {"id": "economy", "title": "经济方案", "printMode": "合版",
         "description": f"合版拼单：{materials[0]} + {printing}，{format_note}，{finishes[0]}，{binding}。",
         "cost": "成本较低", "lead": "交期较快" if fast else "交期稳定", "score": "适合控预算",
         "reason": f"{profile_note}合版多单拼版，起印量低、单价低，适合预算敏感的项目。",
         "risk": "合版颜色与其他订单共用版面，存在轻微批次色差，且不能指定专色。",
         "paper": materials[0], "finishing": finishes[0], "binding": binding},
        {"id": "balanced", "title": "平衡方案", "printMode": "合版",
         "description": f"合版拼单：{materials[1]} + {printing}，{finishes[1]}，{binding}，兼顾效果与生产稳定性。",
         "cost": "成本中等", "lead": "交期稳定", "score": "综合推荐",
         "reason": f"{profile_note}在颜色表现、手感和成本之间取平衡，仍是合版产线的稳定配置。",
         "risk": "覆膜或复杂后道会增加少量加工时间和费用；颜色一致性要求高时考虑专版。",
         "paper": materials[1], "finishing": finishes[1], "binding": binding},
        {"id": "premium", "title": "质感方案", "printMode": "专版",
         "description": f"专版开机：{materials[2]} + {printing}，{finishes[2]}，{binding}，强化品牌表现。",
         "cost": "成本较高", "lead": "需要确认加急" if fast else "交期较长", "score": "视觉优先",
         "reason": f"{profile_note}专版单独制版，颜色可控、可上专色，适合品牌发布和需要触感记忆点的物料。",
         "risk": "专版版费与开机费较高、起印量更高；需要专色、打样、结构或后道工艺确认。",
         "paper": materials[2], "finishing": finishes[2], "binding": binding},
    ]


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
def preflight_file(file_name: str, size_bytes: int, page_count: int | None = None,
                   encrypted: bool = False, readable: bool = True,
                   inspection: dict[str, Any] | None = None,
                   expected_size: str | None = None) -> dict[str, Any]:
    """检查文件类型、大小和浏览器本地解析出的印前线索。"""
    checks = []
    errors = []
    warnings = []
    suggestions = []
    if not file_name.lower().endswith(".pdf"):
        errors.append("MVP 暂只支持 PDF 文件。")
        checks.append({"label": "文件类型", "status": "error", "detail": "仅支持 PDF"})
    else:
        checks.append({"label": "文件类型", "status": "ok", "detail": "PDF"})
    if size_bytes <= 0:
        errors.append("文件大小无效，请重新选择文件。")
        checks.append({"label": "文件大小", "status": "error", "detail": "大小无效"})
    elif size_bytes > 20 * 1024 * 1024:
        errors.append("文件超过 20 MB，请压缩后再上传。")
        checks.append({"label": "文件大小", "status": "error", "detail": "超过 20 MB"})
    else:
        checks.append({"label": "文件大小", "status": "ok", "detail": f"{size_bytes / 1024 / 1024:.1f} MB"})
    if not readable:
        errors.append("文件内容无法读取，请确认文件未损坏后重新上传。")
        checks.append({"label": "文件内容", "status": "error", "detail": "无法读取"})
    elif encrypted:
        errors.append("PDF 已加密，请先解除密码保护再上传。")
        checks.append({"label": "文件保护", "status": "error", "detail": "已加密"})
    else:
        checks.append({"label": "文件保护", "status": "ok", "detail": "未发现加密标记"})
    if page_count is None:
        warnings.append("无法解析页数，请在下单前人工确认页数。")
        checks.append({"label": "页数", "status": "unknown", "detail": "未解析到"})
    elif page_count <= 0:
        warnings.append("未解析到页数，可能使用了压缩对象流，请人工确认页数。")
        checks.append({"label": "页数", "status": "unknown", "detail": "未解析到"})
    else:
        checks.append({"label": "页数", "status": "ok", "detail": f"{page_count} 页"})
        if page_count >= 49:
            warnings.append("页数较多，请确认装订方式和翻阅强度。")
            suggestions.append("页数较多时优先确认胶装、锁线或特殊装订方案。")
    file_hints = " ".join(part.lower() for part in re.split(r"[\s_-]+", file_name) if part)
    naming_warnings = []
    if "无出血" in file_name or "no-bleed" in file_hints:
        naming_warnings.append("文件名提示可能缺少出血")
    if "rgb" in file_hints:
        naming_warnings.append("文件名提示可能使用 RGB 颜色")
    if "低分辨率" in file_name or "low-res" in file_hints:
        naming_warnings.append("文件名提示可能存在低分辨率图片")
    if naming_warnings:
        warnings.extend(f"{item}，请上传前检查印前设置。" for item in naming_warnings)
        checks.append({"label": "文件命名", "status": "warn", "detail": "；".join(naming_warnings)})
    else:
        checks.append({"label": "文件命名", "status": "ok", "detail": "未发现常见风险标记"})

    # The browser performs a bounded, local PDF metadata scan.  Treat every
    # result as a clue, not a production-grade proof; final preflight stays a
    # human or professional PDF tool responsibility.
    inspection = inspection if isinstance(inspection, dict) else {}
    if inspection:
        if inspection.get("isPdf") is False:
            errors.append("未检测到有效的 PDF 文件头，请重新导出文件。")
            checks.append({"label": "PDF 文件头", "status": "error", "detail": "格式异常"})
        elif inspection.get("isPdf") is True:
            checks.append({"label": "PDF 文件头", "status": "ok", "detail": "格式标记正常"})
        pdf_version = str(inspection.get("pdfVersion") or "").strip()[:12]
        if pdf_version:
            checks.append({"label": "PDF 版本", "status": "info", "detail": f"PDF {pdf_version}"})
        inspected_pages = inspection.get("pageCount")
        if isinstance(inspected_pages, int) and page_count and inspected_pages != page_count:
            warnings.append("浏览器解析页数与提交页数不一致，请人工核对")
            checks.append({"label": "页数一致性", "status": "warn", "detail": f"提交 {page_count} 页 / 解析 {inspected_pages} 页"})
        if inspection.get("hasEof") is False:
            warnings.append("未发现 PDF 结束标记，文件可能未完整导出")
            checks.append({"label": "文件结束标记", "status": "warn", "detail": "未发现 %%EOF"})
        elif inspection.get("hasEof") is True:
            checks.append({"label": "文件结束标记", "status": "ok", "detail": "已发现 %%EOF"})

        boxes = inspection.get("boxes") if isinstance(inspection.get("boxes"), dict) else {}
        media_box = boxes.get("media") if isinstance(boxes.get("media"), list) else None
        trim_box = boxes.get("trim") if isinstance(boxes.get("trim"), list) else None
        bleed_box = boxes.get("bleed") if isinstance(boxes.get("bleed"), list) else None

        def box_detail(box: Any) -> str:
            if not isinstance(box, list) or len(box) != 4:
                return "未解析到"
            try:
                width = abs(float(box[2]) - float(box[0])) * 25.4 / 72
                height = abs(float(box[3]) - float(box[1])) * 25.4 / 72
                return f"约 {width:.1f}×{height:.1f} mm"
            except (TypeError, ValueError):
                return "格式异常"

        if media_box:
            checks.append({"label": "页面 MediaBox", "status": "info", "detail": box_detail(media_box)})
        if trim_box:
            checks.append({"label": "裁切 TrimBox", "status": "ok", "detail": box_detail(trim_box)})
        else:
            warnings.append("未解析到 TrimBox，成品裁切尺寸需要人工确认")
            checks.append({"label": "裁切 TrimBox", "status": "unknown", "detail": "未解析到"})
        if bleed_box:
            checks.append({"label": "出血 BleedBox", "status": "ok", "detail": box_detail(bleed_box)})
        else:
            warnings.append("未解析到 BleedBox，出血需要人工确认")
            suggestions.append("确认成品四边通常各预留约 3mm 出血，并检查文字安全边")
            checks.append({"label": "出血 BleedBox", "status": "unknown", "detail": "未解析到"})

        # Compare the requested flat size with TrimBox (or MediaBox when no
        # TrimBox is available). Three-dimensional package sizes stay unknown.
        expected_sizes = []
        for part in str(expected_size or "").split("/"):
            parsed = _parse_size_mm(part.strip())
            if parsed and len(parsed) == 2:
                expected_sizes.append(parsed)
        observed_box = trim_box or media_box
        if expected_sizes and observed_box and len(observed_box) == 4:
            try:
                observed = (abs(float(observed_box[2]) - float(observed_box[0])) * 25.4 / 72,
                            abs(float(observed_box[3]) - float(observed_box[1])) * 25.4 / 72)
                matches = any(
                    (abs(observed[0] - candidate[0]) <= 1 and abs(observed[1] - candidate[1]) <= 1)
                    or (abs(observed[0] - candidate[1]) <= 1 and abs(observed[1] - candidate[0]) <= 1)
                    for candidate in expected_sizes
                )
                observed_detail = f"约 {observed[0]:.1f}×{observed[1]:.1f} mm"
                if matches:
                    checks.append({"label": "成品尺寸一致性", "status": "ok", "detail": f"文件 {observed_detail}"})
                else:
                    warnings.append(f"文件页面尺寸为{observed_detail}，与订单成品尺寸不一致，请确认是否为展开尺寸")
                    checks.append({"label": "成品尺寸一致性", "status": "warn", "detail": f"文件 {observed_detail} / 订单 {expected_size}"})
            except (TypeError, ValueError):
                checks.append({"label": "成品尺寸一致性", "status": "unknown", "detail": "尺寸格式异常"})
        elif expected_sizes:
            checks.append({"label": "成品尺寸一致性", "status": "unknown", "detail": "缺少可比页面框"})

        color_spaces = inspection.get("colorSpaces") if isinstance(inspection.get("colorSpaces"), list) else []
        color_spaces = [str(item)[:24] for item in color_spaces if item][:8]
        if color_spaces:
            checks.append({"label": "颜色空间线索", "status": "info", "detail": "、".join(color_spaces)})
            if "DeviceRGB" in color_spaces:
                warnings.append("检测到 RGB 颜色空间，印刷前请确认是否需要转换为 CMYK")
            if any(item in color_spaces for item in ("Separation", "DeviceN")):
                warnings.append("检测到专色/多色版线索，请确认专色名称和供应商配置")
        else:
            checks.append({"label": "颜色空间线索", "status": "unknown", "detail": "未解析到"})

        font_state = str(inspection.get("fontEmbedding") or "unknown")
        if font_state == "embedded":
            checks.append({"label": "字体嵌入线索", "status": "ok", "detail": "发现字体文件嵌入标记"})
        elif font_state == "missing":
            warnings.append("发现字体可能未嵌入，印刷前请转曲或嵌入字体")
            checks.append({"label": "字体嵌入线索", "status": "warn", "detail": "可能未嵌入"})
        else:
            checks.append({"label": "字体嵌入线索", "status": "unknown", "detail": "无法从轻量扫描确认"})

        image_count = inspection.get("imageCount")
        if isinstance(image_count, int) and image_count > 0:
            checks.append({"label": "图片对象", "status": "info", "detail": f"发现 {image_count} 个图片对象；分辨率需专业工具确认"})
        else:
            checks.append({"label": "图片对象", "status": "unknown", "detail": "未解析到"})
        if inspection.get("hasTransparency"):
            warnings.append("检测到透明度对象，需确认扁平化和叠印效果")
            checks.append({"label": "透明度线索", "status": "warn", "detail": "发现透明度对象"})
        if inspection.get("hasOverprint"):
            warnings.append("检测到叠印线索，请确认黑版和专色叠印设置")
            checks.append({"label": "叠印线索", "status": "warn", "detail": "发现叠印标记"})
    if errors:
        message = errors[0]
    elif warnings:
        page_summary = f"已解析到 {page_count} 页；" if page_count and page_count > 0 else ""
        message = f"基础检查完成，{page_summary}有 {len(warnings)} 项需要人工确认：" + "；".join(warnings)
    else:
        message = "基础检查通过；出血、颜色和字体仍需正式印前检查。"
    return {"ok": not errors, "message": message, "fileName": file_name, "sizeBytes": size_bytes,
            "pageCount": page_count, "encrypted": encrypted, "checks": checks,
            "warnings": warnings, "suggestions": suggestions,
            "inspectionLevel": "metadata" if inspection else "basic"}


@tool
def match_supplier_capability(order: dict[str, Any], platform_id: str | None = None) -> dict[str, Any]:
    """将标准订单与供应商能力档案逐字段匹配，返回支持、待确认和不支持项。"""
    selected_id = platform_id or order.get("platform") or "generic"
    platform = PLATFORMS.get(selected_id, PLATFORMS["generic"])
    profile = platform.get("supplierProfile", {})
    supported: list[dict[str, Any]] = []
    needs_review: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []

    def review(field: str, message: str) -> None:
        needs_review.append({"field": field, "message": message})

    def support(field: str, value: Any) -> None:
        supported.append({"field": field, "value": value})

    product_type = order.get("productType", "")
    categories = profile.get("categories", [])
    if product_type and (product_type in categories or "全品类" in categories):
        support("品类", product_type)
    elif product_type:
        unsupported.append({"field": "品类", "message": f"供应商档案未明确支持{product_type}"})
    else:
        review("品类", "尚未确定印刷品品类")

    paper = order.get("paper", "")
    papers = profile.get("papers", [])
    if paper:
        if not papers or "按供应商确认" in papers:
            review("纸张/材料", "纸张能力需要供应商人工确认")
        elif any(item.lower() in paper.lower() for item in papers):
            support("纸张/材料", paper)
        else:
            unsupported.append({"field": "纸张/材料", "message": f"{paper}不在常用材料档案中"})

    finishing = order.get("finishing", "")
    finishings = profile.get("finishing", [])
    if finishing:
        if not finishings or "按供应商确认" in finishings:
            review("表面工艺", "表面工艺能力需要供应商人工确认")
        else:
            matched = [item for item in finishings if item.lower() in finishing.lower()]
            if matched:
                support("表面工艺", "、".join(matched))
            else:
                unsupported.append({"field": "表面工艺", "message": f"{finishing}不在常用工艺档案中"})

    size = order.get("size", "")
    max_size = profile.get("maxSize", "")
    if size:
        limit = _parse_max_size(max_size)
        actual = _parse_size_mm(size.split(" / ")[0])
        if max_size in {"", "按供应商确认", "待补充"}:
            review("成品尺寸", f"请由供应商确认{size}的可生产范围")
        elif limit and actual:
            actual_sorted, limit_sorted = sorted(actual), sorted(limit)
            if len(actual_sorted) > len(limit_sorted):
                review("成品尺寸", f"{size}包含结构尺寸，需供应商按刀模和展开尺寸确认")
            elif len(actual_sorted) == len(limit_sorted) and all(a <= b for a, b in zip(actual_sorted, limit_sorted)):
                support("成品尺寸", size)
                review("成品尺寸", f"档案范围已覆盖{size}，仍需确认成品/展开尺寸和出血要求")
            else:
                unsupported.append({"field": "成品尺寸", "message": f"{size}可能超过供应商最大尺寸{max_size}"})
        else:
            review("成品尺寸", f"请确认{size}与供应商最大尺寸{max_size}的关系")

    deadline = order.get("deadline", "")
    if deadline:
        lead_time = profile.get("leadTime", "按供应商确认")
        if lead_time in {"", "按供应商确认", "待补充"}:
            review("交期", f"请由供应商确认{deadline}是否可交付")
        else:
            review("交期", f"静态档案参考交期为{lead_time}，仍需确认{deadline}")

    product_profile = parameter_state(order)
    if product_profile.get("parameters"):
        filled = [item["label"] for item in product_profile["parameters"] if item.get("filled")]
        if filled:
            review("品类参数", f"已填写{('、'.join(filled))}，需映射到供应商字段并人工确认")

    multi_product = _multi_product_info(order)
    if multi_product:
        label = "、".join(multi_product) if len(multi_product) > 1 else "多个订单项"
        items = order.get("items") if isinstance(order.get("items"), list) else []
        split_ready = items and all(isinstance(item, dict) and item.get("selectedOption") for item in items)
        review("多产品订单", f"{label}已拆分，将按产品项分别确认能力" if split_ready
               else f"检测到{label}，需拆分为独立订单项后再询价")

    status = "unsupported" if unsupported else "review" if needs_review else "ready"
    denominator = len(supported) + len(needs_review) + len(unsupported)
    confidence = round(len(supported) / denominator * 100) if denominator else 0
    return {"platformId": selected_id, "platform": platform["name"], "status": status,
            "confidence": confidence, "supported": supported, "needsReview": needs_review,
            "unsupported": unsupported, "profile": profile,
            "knowledgeVersion": KNOWLEDGE_VERSION,
            "supplierProfileVersion": SUPPLIER_PROFILE_VERSION,
            "multiProduct": multi_product,
            "requiresHumanConfirmation": status != "ready" or bool(needs_review)}


@tool
def request_supplier_quote(order: dict[str, Any], platform_id: str | None = None) -> dict[str, Any]:
    """Prepare a supplier quote request without sending anything externally."""
    selected_id = platform_id or order.get("platform") or "generic"
    platform = PLATFORMS.get(selected_id, PLATFORMS["generic"])
    capability = match_supplier_capability(order, selected_id)
    multi_product = capability.get("multiProduct") or _multi_product_info(order)
    if capability.get("unsupported") or multi_product:
        message = ("当前订单包含多个产品项，未生成合并询价请求；请在当前产品项中分别询价。"
                   if multi_product else "当前平台存在明确不支持项，未生成询价请求；请切换平台或先人工确认。")
        return {
            "status": "blocked",
            "platformId": selected_id,
            "platform": platform["name"],
            "capabilityStatus": capability.get("status"),
            "knowledgeVersion": KNOWLEDGE_VERSION,
            "supplierProfileVersion": SUPPLIER_PROFILE_VERSION,
            "unsupported": capability["unsupported"],
            "multiProduct": multi_product,
            "requiresHumanConfirmation": True,
            "message": message,
        }
    adapter = get_adapter(selected_id)
    return {
        **adapter.prepare_quote_request(order, capability),
        "knowledgeVersion": KNOWLEDGE_VERSION,
        "supplierProfileVersion": SUPPLIER_PROFILE_VERSION,
    }


@tool
def prepare_handoff(order: dict[str, Any]) -> dict[str, Any]:
    """按目标平台生成标准化订单交接文本。"""
    platform_id = order.get("platform") or "generic"
    platform = PLATFORMS.get(platform_id, PLATFORMS["generic"])
    adapter = get_adapter(platform_id)
    supplier_profile = platform.get("supplierProfile", {})
    readiness = match_supplier_capability(order)
    multi_product = _multi_product_info(order)
    if multi_product:
        return {
            "status": "blocked",
            "platform": platform,
            "adapter": {"platformId": adapter.platform_id, "mode": adapter.mode},
            "mappedOrder": {},
            "text": "订单包含多个产品项，需分别完成各项确认后再生成整体交接单。",
            "productProfile": parameter_state(order),
            "supplierReadiness": readiness,
            "knowledgeVersion": KNOWLEDGE_VERSION,
            "multiProduct": multi_product,
            "requiresHumanConfirmation": True,
        }
    dimensions = order.get("dimensions") if isinstance(order.get("dimensions"), dict) else {}
    fields = [(key, LABELS[key]) for key in LABELS if key != "platform"
              and not (key == "size" and dimensions.get("packageSize") and not dimensions.get("finishedSize"))]
    lines = [f"目标平台：{platform['name']}"] + [f"{label}：{order[key] or '未填写'}" for key, label in fields]
    dimension_lines = [f"{DIMENSION_LABELS[key]}：{dimensions.get(key)}"
                       for key in DIMENSION_LABELS if dimensions.get(key)]
    if dimension_lines:
        lines.append("尺寸定义：")
        lines.extend(f"- {line}" for line in dimension_lines)
    imposition = imposition_hint(dimensions.get("finishedSize") or order.get("size") or "")
    if imposition:
        lines.append(f"开数参考：{imposition}")
    profile = parameter_state(order)
    if profile["parameters"]:
        lines.append(f"品类分类：{profile['category']}")
        lines.append("品类参数：")
        lines.extend(f"- {item['label']}：{item['value'] or '未填写'}" for item in profile["parameters"])
    text = "\n".join(lines)
    return {"status": "ready", "platform": platform, "adapter": {"platformId": adapter.platform_id, "mode": adapter.mode},
            "mappedOrder": adapter.map_order(order), "text": text, "productProfile": profile,
            "supplierReadiness": readiness,
            "knowledgeVersion": KNOWLEDGE_VERSION,
            "requiresHumanConfirmation": True}


@tool
def estimate_price(order: dict[str, Any]) -> dict[str, Any]:
    """按知识库示例价格参数表估算费用量级，不替代印刷厂正式报价。"""
    multi_product = _multi_product_info(order)
    if multi_product:
        return {"type": "estimate", "status": "blocked", "range": None, "missing": [],
                "multiProduct": multi_product, "assumptions": "多个产品项不能合并估算；请拆分后分别估算。",
                "knowledgeVersion": KNOWLEDGE_VERSION, "requiresHumanConfirmation": True}
    missing = [LABELS[key] for key in ("productType", "quantity", "size", "printing") if not order.get(key)]
    if missing:
        return {"type": "estimate", "range": None, "missing": missing,
                "assumptions": "至少需要印刷品、数量、尺寸和印刷颜色后才能估算。",
                "knowledgeVersion": KNOWLEDGE_VERSION, "requiresHumanConfirmation": True}
    quantity = _number(order.get("quantity", "")) or 500
    base = PRICE_MODEL["categories"].get(order.get("productType"), PRICE_MODEL["defaultBase"])
    tier = next((entry["factor"] for entry in PRICE_MODEL["quantityTiers"]
                 if entry["upTo"] is None or quantity <= entry["upTo"]), 1.0)
    finishing = str(order.get("finishing") or "")
    finishing_factor = max(
        (factor for term, factor in PRICE_MODEL["finishingFactors"].items() if term in finishing),
        default=1.0,
    )
    total = max(base * tier * finishing_factor, PRICE_MODEL["minimumTotal"])
    band = PRICE_MODEL["band"]
    low, high = round(total * band["low"]), round(total * band["high"])
    assumptions = (f"依据内置示例价格参数表（版本 {PRICE_MODEL_VERSION}）按品类基数、数量阶跃和工艺系数估算，"
                   "不构成报价；实际以供应商回复为准，未含运输和打样。")
    return {"type": "estimate", "range": f"¥{low} - ¥{high}",
            "assumptions": assumptions, "knowledgeVersion": KNOWLEDGE_VERSION,
            "pricingModelVersion": PRICE_MODEL_VERSION, "requiresHumanConfirmation": True}


@tool
def validate_order(order: dict[str, Any]) -> dict[str, Any]:
    """校验订单字段完整性，输出阻塞项、风险和下一步建议。"""
    required_keys = required_order_keys(order)
    missing = [LABELS[key] for key in required_keys if not order.get(key)]
    warnings = []
    suggestions = []
    risks = []
    profile = parameter_state(order)
    product_missing = [item["label"] for item in profile["missing"]]
    multi_product = _multi_product_info(order)
    if multi_product:
        label = "、".join(multi_product) if len(multi_product) > 1 else "多个订单项"
        warnings.append(f"检测到多个印刷品：{label}，当前需要拆分为独立订单项")
        suggestions.append("请分别确认每个产品的数量、尺寸、材料和交期，再分别询价")
    if order.get("productType") in {"宣传册", "画册"} and not order.get("binding"):
        warnings.append("宣传册/画册尚未确认装订方式")
        suggestions.append("页数少于 48 页可优先考虑骑马钉，页数较多再考虑胶装")
    page_count = _number(order.get("pages", ""))
    if (order.get("productType") in {"宣传册", "画册"} and order.get("binding") == "骑马钉"
            and page_count is not None and page_count >= 48):
        message = f"{page_count} 页画册使用骑马钉，装订强度和摊平度可能不足"
        warnings.append(message)
        suggestions.append("页数达到 48 页或更多时，优先确认胶装、锁线胶装或特殊装订")
        risks.append({"level": "warning", "message": message, "suggestion": suggestions[-1]})
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
    profile_min = profile.get("minQuantity")
    if profile_min and quantity is not None and 0 < quantity < profile_min:
        warnings.append(f"{order.get('productType') or '该品类'}常规起印量约 {profile_min}，"
                        f"当前数量 {quantity} 可能无法安排生产")
        suggestions.append("可与供应商确认合版凑版条件，或将数量调整到常规起印量")
    readiness = round((len(required_keys) - len(missing)) / len(required_keys) * 100) if required_keys else 100
    item_validations = _validate_order_items(order) if multi_product else []
    item_readiness = round(sum(item["readiness"] for item in item_validations) / len(item_validations)) if item_validations else None
    if item_validations and all(item.get("ok") for item in item_validations) \
            and all(item.get("selectedOption") for item in (order.get("items") or []) if isinstance(item, dict)):
        warnings = [item for item in warnings if "检测到多个印刷品" not in item]
        suggestions = [item for item in suggestions if "分别确认每个产品" not in item]
    return {"ok": not missing and quantity != 0 and not multi_product, "missing": missing,
            "multiProduct": multi_product, "productMissing": product_missing,
            "productProfile": profile, "warnings": warnings, "suggestions": suggestions,
            "risks": risks, "readiness": readiness, "productReadiness": profile["readiness"],
            "itemValidations": item_validations, "itemReadiness": item_readiness,
            "knowledgeVersion": KNOWLEDGE_VERSION}


def _validate_order_items(order: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate each product item against its own product profile.

    Top-level order fields stay available for backwards compatibility, but a
    multi-product order must use these per-item results before recommendation,
    quote, or handoff actions are enabled.
    """
    items = order.get("items") if isinstance(order.get("items"), list) else []
    shared_fields = ("purpose", "orientation", "paper", "printing", "finishing", "binding", "deadline", "budget")
    results: list[dict[str, Any]] = []
    for index, raw_item in enumerate(items):
        if not isinstance(raw_item, dict):
            continue
        item = deepcopy(raw_item)
        item["items"] = []
        item["productTypes"] = []
        for key in shared_fields:
            if not item.get(key) and order.get(key):
                item[key] = deepcopy(order[key])
        item_result = validate_order(item)
        ready = bool(item_result.get("ok")) and not item_result.get("productMissing")
        results.append({
            "itemId": str(raw_item.get("itemId") or f"item-{index + 1}"),
            "index": index,
            "productType": raw_item.get("productType") or "",
            "ok": ready,
            "status": "ready" if ready else "needs_input",
            "missing": item_result.get("missing", []),
            "productMissing": item_result.get("productMissing", []),
            "warnings": item_result.get("warnings", []),
            "risks": item_result.get("risks", []),
            "parameters": [{"key": parameter.get("key"), "label": parameter.get("label"),
                            "value": parameter.get("value", ""), "filled": parameter.get("filled", False),
                            "required": parameter.get("required", False)}
                           for parameter in item_result.get("productProfile", {}).get("parameters", [])
                           if parameter.get("key")],
            "readiness": round((item_result.get("readiness", 0) + item_result.get("productReadiness", 0)) / 2),
            "productReadiness": item_result.get("productReadiness", 0),
        })
    return results
