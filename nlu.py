"""Rule-based language perception: extract order fields from natural language.

Split from agent.py.  ``perceive`` is the single entry point used by the
Agent; the extractors are pure functions so they can be unit-tested directly.
"""

from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from typing import Any

from order_model import QUANTITY_CAPTURE, _is_three_dimensional_size, normalize_order_dimensions, parse_quantity
from product_knowledge import alias_map, find_product


def _find_product_mentions(text: str) -> list[tuple[str, int, int]]:
    """Find non-overlapping catalog mentions so multi-product requests are explicit."""
    normalized = unicodedata.normalize("NFKC", text or "")
    candidates = []
    for alias, product in sorted(alias_map().items(), key=lambda item: len(item[0]), reverse=True):
        for match in re.finditer(re.escape(alias), normalized, re.IGNORECASE):
            candidates.append((match.start(), match.end(), product))
    accepted: list[tuple[str, int, int]] = []
    for start, end, product in sorted(candidates, key=lambda item: (item[0], -(item[1] - item[0]))):
        if any(start < other_end and end > other_start for _, other_start, other_end in accepted):
            continue
        accepted.append((product, start, end))
    return sorted(accepted, key=lambda item: item[1])


def perceive(text: str, allow_multi: bool = True) -> tuple[dict[str, Any], dict[str, float]]:
    """Extract order fields plus a per-field evidence confidence in [0, 1].

    Confidence grades how directly the value was stated: explicit labels,
    units and full patterns score >= 0.85; bare numbers in context, vibe
    words and heuristic attributions score below the 0.75 confirmation
    threshold so the Agent asks the user before trusting them.
    """
    # Normalize full-width input copied from design tools or IMEs first.
    text = unicodedata.normalize("NFKC", text or "")
    data: dict[str, Any] = {}
    confidence: dict[str, float] = {}
    product = find_product(text)
    # Keep invitation cards from the original MVP as a lightweight family.
    if not product and "邀请函" in text:
        product = "邀请函"
    if product:
        data["productType"] = product
        confidence["productType"] = 0.92
    size_matches = _extract_sizes(text)
    size_spans = [(match.start(), match.end()) for match, _ in size_matches]
    quantity_candidates: list[tuple[int, int, tuple[str, int | float, str]]] = []
    explicit_quantity = re.search(
        rf"(?:数量|印刷量|印多少|做多少)\s*(?:改成|改为|调整为|为|是)?\s*{QUANTITY_CAPTURE}",
        text,
    )
    if explicit_quantity:
        raw = "".join(item or "" for item in explicit_quantity.groups())
        parsed = parse_quantity(raw, product, explicit_quantity.group(3) or "")
        if parsed:
            data["quantity"], data["quantityValue"], data["quantityUnit"] = parsed
            confidence["quantity"] = 0.95
    for match in re.finditer(rf"(?:约|大约|需要|印刷|做)?\s*{QUANTITY_CAPTURE}", text):
        # Dimension numbers (including B4's 4) are never quantities.
        number_start, number_end = match.span(1)
        if any(start <= number_start and number_end <= end for start, end in size_spans):
            continue
        unit = match.group(3) or match.group(2) or ""
        next_chars = text[match.end():].lstrip()
        prefix_text = text[match.start():number_start]
        has_quantity_context = bool(re.search(r"(?:约|大约|需要|印刷|做|数量)\s*$", prefix_text))
        if (unit or has_quantity_context) and not next_chars.startswith(("x", "X", "×", "*", "\\")):
            parsed = parse_quantity(match.group(0), product, match.group(3) or "")
            if parsed:
                data["quantity"], data["quantityValue"], data["quantityUnit"] = parsed
                # An explicit unit is strong evidence; a bare number that
                # merely follows "做/需要" is a guess the user must confirm.
                confidence["quantity"] = 0.95 if match.group(3) else (0.9 if match.group(2) else 0.72)
                quantity_candidates.append((number_start, number_end, parsed))
    if size_matches:
        normalized_sizes = []
        for _, size in size_matches:
            if size not in normalized_sizes:
                normalized_sizes.append(size)
        labeled_dimensions = {
            key: _extract_labeled_size(text, marker)
            for key, marker in (
                ("finishedSize", r"(?:成品|裁切)(?:尺寸|大小)?"),
                ("expandedSize", r"(?:展开|摊开)(?:尺寸|大小)?"),
                ("dieCutSize", r"(?:刀模|刀线)(?:尺寸|大小)?"),
                ("packageSize", r"(?:包装|盒体|袋体)(?:尺寸|大小)?"),
            )
        }
        labeled_dimensions = {key: value for key, value in labeled_dimensions.items() if value}
        if labeled_dimensions:
            data["dimensions"] = labeled_dimensions
            for key in labeled_dimensions:
                confidence[f"dimensions.{key}"] = 0.95
            # ``size`` remains the backwards-compatible finished-size
            # field. Never let an expanded or die-cut size replace it.
            if labeled_dimensions.get("finishedSize"):
                data["size"] = labeled_dimensions["finishedSize"]
                confidence["size"] = 0.95
            elif labeled_dimensions.get("packageSize") and _is_three_dimensional_size(labeled_dimensions["packageSize"]):
                # Keep the legacy display field for structural products;
                # the nested dimension remains the authoritative meaning.
                data["size"] = labeled_dimensions["packageSize"]
                confidence["size"] = 0.95
            elif not data.get("size"):
                data["size"] = ""
        else:
            data["size"] = " / ".join(normalized_sizes)
            confidence["size"] = 0.9
    if match := re.search(r"(?:共|约|大约)?\s*(\d{1,3})\s*(?:页|P(?![A-Za-z]))", text, re.I):
        data["pages"] = f"{match.group(1)} 页"
        confidence["pages"] = 0.9
    if re.search(r"横版|横向|横式", text):
        data["orientation"] = "横版"; confidence["orientation"] = 0.85
    elif re.search(r"竖版|竖向|纵向|直式", text):
        data["orientation"] = "竖版"; confidence["orientation"] = 0.85
    paper_match = re.search(r"(\d{2,3})\s*(?:g|克)\s*(哑粉纸|铜版纸|白卡纸|牛皮纸|胶版纸)", text, re.I)
    if paper_match:
        data["paper"] = f"{paper_match.group(1)}g {paper_match.group(2)}"
        confidence["paper"] = 0.9
    else:
        for item in ["特种纸", "牛皮纸", "白卡纸", "铜版纸", "哑粉纸", "胶版纸"]:
            if item in text:
                data["paper"] = item
                confidence["paper"] = 0.75
                break
    if re.search(r"双面.*?(?:四色|彩印)?|两面", text):
        data["printing"] = "双面四色"; confidence["printing"] = 0.85
    elif re.search(r"单面.*?(?:四色|彩印)?", text):
        data["printing"] = "单面四色"; confidence["printing"] = 0.85
    elif "四色" in text or "彩色" in text or "彩印" in text:
        data["printing"] = "四色印刷"; confidence["printing"] = 0.78
    elif "黑白" in text or "单色" in text:
        data["printing"] = "单色印刷"; confidence["printing"] = 0.85
    finish_names = ["哑膜", "亮膜", "覆膜", "烫金", "烫银", "局部UV", "局部 UV", "击凸", "压凹", "上光"]
    removed_finishes = [item for item in finish_names if re.search(rf"(?:不要|不需要|无需|不用|去掉|取消).{{0,5}}{re.escape(item)}", text, re.I)]
    finishes = [item for item in finish_names if item.lower() in text.lower() and item not in removed_finishes]
    if removed_finishes and not finishes:
        data["finishing"] = "无特殊工艺"; confidence["finishing"] = 0.85
    elif finishes:
        data["finishing"] = "、".join(dict.fromkeys(finishes)); confidence["finishing"] = 0.88
    if "骑马钉" in text:
        data["binding"] = "骑马钉"; confidence["binding"] = 0.88
    elif "胶装" in text:
        data["binding"] = "胶装"; confidence["binding"] = 0.88
    elif "锁线" in text:
        data["binding"] = "锁线胶装"; confidence["binding"] = 0.88
    elif re.search(r"(?:不要|不需要|无需|不用|去掉|取消).{0,5}装订", text):
        data["binding"] = "无需装订"; confidence["binding"] = 0.85
    if budget_match := re.search(r"预算\s*(?:控制在|不超过|约|为)?\s*([\d,]+)\s*[元块]", text):
        data["budget"] = f"预算 ¥{budget_match.group(1).replace(',', '')}"
        confidence["budget"] = 0.9
    elif re.search(r"低预算|便宜|控制成本|经济|不超过\s*\d+\s*[元块]", text):
        data["budget"] = "优先控制成本"; confidence["budget"] = 0.72
    elif re.search(r"高级|质感|精致|有档次", text):
        data["budget"] = "优先视觉质感"; confidence["budget"] = 0.62
    elif re.search(r"赶|尽快|明天|后天|下周|三天|两天", text):
        data["budget"] = "优先交期"; confidence["budget"] = 0.7
    if match := re.search(r"(今天|明天|后天|(?:三|两|一|四|五|六|七)天(?:后|内)?|\d+\s*天(?:后|内)?|本周[一二三四五六日天]?|下周[一二三四五六日天]?|月底|\d{1,2}月\d{1,2}日)", text):
        data["deadline"] = re.sub(r"\s+", "", match.group(1))
        confidence["deadline"] = 0.85
    if "宣传" in text or "推广" in text or "活动" in text:
        data["purpose"] = "品牌宣传"; confidence["purpose"] = 0.68
    platform_aliases = {"盛大": "shengda", "平台A": "platform_a", "平台 B": "platform_b", "平台B": "platform_b"}
    for phrase, platform in platform_aliases.items():
        if phrase in text:
            data["platform"] = platform
            confidence["platform"] = 0.95
            break
    if not product and re.search(r"天地盖|抽屉盒|折叠盒|飞机盒|开窗盒", text):
        product = data["productType"] = "包装盒"
        confidence["productType"] = 0.78
    specs = _extract_product_specs(text, product, data.get("size", ""))
    if specs:
        data["productSpecs"] = specs
        for key, value in specs.items():
            # Placeholder values ("需确认刀模文件") mark an inference, not a
            # user statement, so they must be confirmed before generation.
            confidence[f"productSpecs.{key}"] = 0.55 if "需确认" in str(value) else 0.85
    normalize_order_dimensions(data)
    if allow_multi:
        mentions = _find_product_mentions(text)
        distinct_products = list(dict.fromkeys(item[0] for item in mentions))
        if len(mentions) > 1 and len(distinct_products) > 1:
            items = []
            used_quantities: set[int] = set()
            for index, (item_product, item_start, _) in enumerate(mentions):
                segment_start = item_start
                segment_end = mentions[index + 1][1] if index + 1 < len(mentions) else len(text)
                item, item_confidence = perceive(text[segment_start:segment_end], allow_multi=False)
                item["productType"] = item_product
                if quantity_candidates:
                    mention_mid = (item_start + mentions[index][2]) / 2
                    available = [
                        (candidate_index, candidate)
                        for candidate_index, candidate in enumerate(quantity_candidates)
                        if candidate_index not in used_quantities
                    ]
                    if available:
                        candidate_index, (_, _, quantity) = min(
                            available,
                            key=lambda entry: abs(((entry[1][0] + entry[1][1]) / 2) - mention_mid),
                        )
                        item["quantity"], item["quantityValue"], item["quantityUnit"] = quantity
                        used_quantities.add(candidate_index)
                # Values written after the product mentions are commonly
                # shared by every item. Copy only unambiguous shared fields;
                # product-specific dimensions and specs stay item-local.
                for shared_key in ("purpose", "orientation", "paper", "printing", "finishing", "binding", "deadline", "budget"):
                    if not item.get(shared_key) and data.get(shared_key):
                        item[shared_key] = deepcopy(data[shared_key])
                        if shared_key in confidence:
                            item_confidence[shared_key] = confidence[shared_key]
                if len(size_matches) == 1 and not item.get("size") and data.get("size"):
                    item["size"] = data["size"]
                    if "size" in confidence:
                        item_confidence["size"] = confidence["size"]
                if len(size_matches) == 1 and data.get("dimensions"):
                    item["dimensions"] = deepcopy(data["dimensions"])
                    for key in data["dimensions"]:
                        if f"dimensions.{key}" in confidence:
                            item_confidence[f"dimensions.{key}"] = confidence[f"dimensions.{key}"]
                items.append(item)
            data["productType"] = mentions[0][0]
            data["productTypes"] = distinct_products
            data["items"] = items
    return data, confidence


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
    dimensions = [value for _, value in _extract_sizes(text)]
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


def _extract_labeled_size(text: str, marker_pattern: str) -> str:
    """Read the first size immediately following a dimension label."""
    marker = re.search(rf"{marker_pattern}\s*[:：]?", text, re.IGNORECASE)
    if not marker:
        return ""
    tail = text[marker.end(): marker.end() + 96]
    matches = _extract_sizes(tail)
    return matches[0][1] if matches else ""
