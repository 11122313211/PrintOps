"""Product taxonomy and parameter knowledge for the print-order agent.

The catalog is intentionally provider-neutral.  It is based on the public
product families visible in 盛大印刷's product navigation and common print
shop terminology; supplier adapters can map these fields later.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


OFFICIAL_NAV_CATEGORIES = [
    "名片/卡片", "单张", "标签/不干胶", "书籍画册", "广告物料", "包装周边",
    "办公用品", "家居日常", "动漫文创", "季节产品", "现货/样品",
]
CATALOG_SOURCE = "https://www.sd2000.com/"


PRODUCT_CATALOG: dict[str, dict[str, Any]] = {
    "宣传册": {
        "category": "书籍画册",
        "aliases": ["宣传册", "产品册", "手册", "说明书", "目录册"],
        "summary": "多页阅读型物料，装订方式和页数会直接影响报价与交期。",
        "parameters": [
            {"key": "pages", "label": "页数", "required": True, "question": "内页一共多少页？", "hint": "按成品页数填写，通常以 4 页为一个装订增量。"},
            {"key": "binding", "label": "装订方式", "required": True, "question": "希望骑马钉、胶装，还是其他装订？", "hint": "少页数优先骑马钉，页数较多可考虑胶装。"},
            {"key": "coverPaper", "label": "封面用纸", "required": False, "question": "封面是否需要比内页更厚？", "hint": "封面常用高克重纸或覆膜提升耐用性。"},
        ],
        "defaultBinding": "骑马钉",
        "recommendation": "先确认页数和装订，再比较纸张克重与封面后道。",
    },
    "画册": {
        "category": "书籍画册",
        "aliases": ["画册", "企业画册", "品牌画册"],
        "summary": "强调图片与品牌呈现的多页印刷品，纸张、色彩和装订是核心决策。",
        "parameters": [
            {"key": "pages", "label": "页数", "required": True, "question": "画册一共多少页？", "hint": "页数决定装订工艺、纸张用量和交期。"},
            {"key": "binding", "label": "装订方式", "required": True, "question": "希望骑马钉、胶装还是锁线胶装？", "hint": "页数较多或需要长期翻阅时再考虑胶装/锁线。"},
            {"key": "coverPaper", "label": "封面用纸", "required": False, "question": "封面要不要加厚或覆膜？", "hint": "封面加厚、哑膜能提升耐磨和触感。"},
        ],
        "defaultBinding": "胶装",
        "recommendation": "先确认页数、装订和图片色彩要求，再选择铜版纸或特种纸。",
    },
    "单页": {
        "category": "单张",
        "aliases": ["单页", "传单", "宣传单", "DM单"],
        "summary": "单张传播物料，重点是成品尺寸、双面印刷、纸张克重和是否需要折叠。",
        "parameters": [
            {"key": "folding", "label": "折叠方式", "required": False, "question": "单页需要折叠吗？", "hint": "如果需要折叠，应改按折页品类确认折法。"},
            {"key": "bleed", "label": "出血", "required": False, "question": "文件是否已预留出血？", "hint": "常规四边各 3mm，文字和 Logo 需留安全边。"},
        ],
        "defaultBinding": "无需装订",
        "recommendation": "数量较大时优先比较纸张克重与印刷面数，避免不必要的复杂后道。",
    },
    "折页": {
        "category": "单张",
        "aliases": ["折页", "三折页", "二折页", "风琴折", "折页单"],
        "summary": "经过压痕和折叠的单张物料，折法、成品尺寸与展开尺寸必须同时确认。",
        "parameters": [
            {"key": "folding", "label": "折页方式", "required": True, "question": "需要二折、三折、风琴折还是其他折法？", "hint": "折页方式决定展开尺寸、压痕位置和文件页序。"},
            {"key": "bleed", "label": "出血", "required": False, "question": "文件是否已按折页展开尺寸预留出血？", "hint": "折页文件要同时标注成品尺寸和展开尺寸。"},
        ],
        "defaultBinding": "压痕 / 折页",
        "recommendation": "先确认折法和展开尺寸，再选择纸张克重，避免折痕爆裂。",
    },
    "名片": {
        "category": "名片/卡片",
        "aliases": ["名片", "个人名片", "企业名片", "卡片"],
        "summary": "小尺寸高频商务物料，核心是成品尺寸、单双面、纸张厚度和圆角/特殊工艺。",
        "parameters": [
            {"key": "cardStock", "label": "名片材质", "required": False, "question": "想用铜版纸、白卡纸还是特种纸？", "hint": "常见名片会用 250g-350g 卡纸；特殊纸需先打样。"},
            {"key": "cardCorners", "label": "圆角", "required": False, "question": "需要圆角吗？", "hint": "圆角属于额外模切/后道，需在文件和报价中标注。"},
        ],
        "defaultBinding": "无需装订",
        "recommendation": "优先确认单双面、卡纸克重和圆角，再决定是否加专色、烫金。",
    },
    "PVC卡": {
        "category": "名片/卡片",
        "aliases": ["PVC卡", "PVC智能卡", "PVC人像证卡", "PVC滴胶卡", "PVC冲切卡", "PVC高品质冲切卡"],
        "summary": "卡片类塑料印刷品，卡型、厚度、是否带芯片和表面工艺必须单独确认。",
        "parameters": [
            {"key": "cardType", "label": "卡片类型", "required": True, "question": "是智能卡、人像证卡、滴胶卡还是异形冲切卡？", "hint": "不同卡型对应不同生产流程，不能只按普通 PVC 卡参样。"},
            {"key": "cardThickness", "label": "卡片厚度", "required": True, "question": "需要 0.38mm、0.76mm 还是其他厚度？", "hint": "厚度会影响芯片、冲切和卡片挺度。"},
            {"key": "chip", "label": "芯片/磁条", "required": False, "question": "需要芯片、磁条或编码吗？", "hint": "智能卡要先确认芯片样品、加密和读写要求。"},
        ],
        "defaultBinding": "冲切 / 覆膜",
        "recommendation": "先确认卡型、厚度和芯片，再确认覆膜、签名条、冲切和编码。",
    },
    "吊牌": {
        "category": "标签/不干胶",
        "aliases": ["吊牌", "服装吊牌", "箱包标签", "组合吊牌"],
        "summary": "悬挂式标签，纸张克重之外还要确认打孔位置、孔径、穿绳和折叠结构。",
        "parameters": [
            {"key": "hangHole", "label": "挂孔", "required": True, "question": "需要圆孔、蝴蝶孔还是其他挂孔？", "hint": "孔位、孔径和距边会影响刀模与承重。"},
            {"key": "string", "label": "穿绳/配件", "required": False, "question": "需要配绳、别针或其他配件吗？", "hint": "配件颜色、长度和装配方式需要单独确认。"},
        ],
        "defaultBinding": "模切 / 打孔 / 穿绳",
        "recommendation": "先确认挂孔和穿绳，再按悬挂承重选择纸张和覆膜。",
    },
    "联单": {
        "category": "办公用品",
        "aliases": ["联单", "无碳联单", "送货单", "出库单", "票据"],
        "summary": "多联连续使用的办公单据，联数、复写顺序、编号和装订方式比普通宣传品更关键。",
        "parameters": [
            {"key": "paperParts", "label": "联数", "required": True, "question": "需要二联、三联还是更多联？", "hint": "联数决定无碳纸组合和每联颜色。"},
            {"key": "numbering", "label": "流水号", "required": False, "question": "是否需要连续编号或打号码？", "hint": "编号需要确认起始号、位数和每联是否同步。"},
            {"key": "binding", "label": "装订方式", "required": False, "question": "需要胶头、装订线还是撕边？", "hint": "联单常见胶头或针式装订，不按书刊骑马钉处理。"},
        ],
        "defaultBinding": "胶头装订",
        "recommendation": "先确认联数和每联用途，再确认编号、打孔和装订方式。",
    },
    "信封封套": {
        "category": "办公用品",
        "aliases": ["信封", "封套", "信封封套", "档案袋"],
        "summary": "需要模切、糊合的办公包装类印刷品，开口方向和展开刀模不能省略。",
        "parameters": [
            {"key": "envelopeSize", "label": "规格尺寸", "required": True, "question": "信封/封套成品尺寸是多少？", "hint": "请区分成品尺寸与展开刀模尺寸。"},
            {"key": "opening", "label": "开口与糊口", "required": True, "question": "开口方向、封口方式有什么要求？", "hint": "常见有上开口、侧开口、胶条或不干胶封口。"},
        ],
        "defaultBinding": "模切 / 糊合",
        "recommendation": "先确认内装物尺寸和开口方向，再确定纸张、刀模与封口。",
    },
    "标签": {
        "category": "标签/不干胶",
        "aliases": ["标签", "不干胶", "贴纸", "标签贴", "不干胶标签"],
        "summary": "贴附型印刷品，尺寸之外还要确认面材、胶水、形状、底纸和使用环境。",
        "parameters": [
            {"key": "labelMaterial", "label": "面材", "required": True, "question": "标签面材用铜版不干胶、透明膜、PET 还是其他？", "hint": "面材决定耐水、耐磨、透明效果和成本。"},
            {"key": "labelShape", "label": "标签形状", "required": True, "question": "标签是方形、圆形还是异形？", "hint": "异形标签需要刀模；圆角也要明确半径或效果。"},
            {"key": "adhesive", "label": "胶水类型", "required": False, "question": "需要普通胶、可移胶还是强粘胶？", "hint": "贴纸的使用表面和温度决定胶水选择。"},
        ],
        "defaultBinding": "模切 / 排废",
        "recommendation": "先确认贴在哪里、是否接触水/油，再选面材和胶水。",
    },
    "包装盒": {
        "category": "包装周边",
        "aliases": ["包装盒", "彩盒", "纸盒", "礼品盒", "包装彩盒"],
        "summary": "结构型包装，成品尺寸、展开刀模、盒型结构、纸板克重和表面工艺缺一不可。",
        "parameters": [
            {"key": "boxSize", "label": "盒体尺寸", "required": True, "question": "包装盒成品长、宽、高是多少？", "hint": "三维尺寸要写清内尺寸/外尺寸，不能只给一个平面尺寸。"},
            {"key": "boxStructure", "label": "盒型结构", "required": True, "question": "是天地盖、抽屉盒、折叠盒、飞机盒还是其他结构？", "hint": "结构决定刀模、糊口、用料和装配方式。"},
            {"key": "dieCut", "label": "刀模文件", "required": False, "question": "已有刀模线或结构图吗？", "hint": "没有刀模时通常需要供应商先做结构确认。"},
        ],
        "defaultBinding": "模切 / 糊盒",
        "recommendation": "包装盒先确认结构和三维尺寸，再谈纸板克重、覆膜、烫金和专色。",
    },
    "手提袋": {
        "category": "包装周边",
        "aliases": ["手提袋", "纸袋", "手挽袋", "无纺布袋", "帆布袋"],
        "summary": "兼顾承重与品牌展示的包装袋，袋体尺寸、材料、绳/提手和承重要求要一起确认。",
        "parameters": [
            {"key": "bagSize", "label": "袋体尺寸", "required": True, "question": "手提袋成品长、宽、侧宽是多少？", "hint": "袋体常用长×宽×侧宽三维规格。"},
            {"key": "bagMaterial", "label": "袋体材料", "required": True, "question": "用白卡、牛皮纸、无纺布还是帆布？", "hint": "材料决定承重、印刷方式和后续折叠。"},
            {"key": "handle", "label": "提手方式", "required": True, "question": "提手用棉绳、扁绳、丝带还是其他？", "hint": "提手颜色、长度和打结方式会影响打样。"},
        ],
        "defaultBinding": "模切 / 糊袋 / 穿绳",
        "recommendation": "先按内装物反推三维尺寸和承重，再确认纸张或布料及提手。",
    },
    "纸杯": {
        "category": "包装周边",
        "aliases": ["纸杯", "咖啡杯", "饮料杯", "杯套"],
        "summary": "接触饮品的包装印刷品，容量、杯身材料、内淋膜和食品接触要求优先。",
        "parameters": [
            {"key": "cupVolume", "label": "容量", "required": True, "question": "纸杯容量是多少毫升？", "hint": "常见 250ml、350ml、500ml，容量决定杯身刀模。"},
            {"key": "cupMaterial", "label": "杯身材料", "required": True, "question": "需要单 PE、双 PE 或其他纸杯材料？", "hint": "食品接触场景要向供应商确认材料合规证明。"},
            {"key": "innerCoating", "label": "内淋膜", "required": True, "question": "内层是否需要防水淋膜？", "hint": "饮品纸杯通常需要内淋膜，具体以供应商规格为准。"},
        ],
        "defaultBinding": "模切 / 卷口",
        "recommendation": "先确认容量和使用场景，再确认纸张、淋膜和食品级合规要求。",
    },
    "海报": {
        "category": "广告物料",
        "aliases": ["海报", "宣传海报", "展板海报"],
        "summary": "平面展示物料，尺寸、展示距离、安装方式和介质比普通宣传单更重要。",
        "parameters": [
            {"key": "displayMaterial", "label": "展示介质", "required": True, "question": "海报印在相纸、背胶、KT 板还是其他介质？", "hint": "室内短期可用相纸，户外或长期展示要考虑耐候材料。"},
            {"key": "install", "label": "安装方式", "required": True, "question": "是墙面张贴、裱板、展架还是其他安装？", "hint": "安装方式会反向决定材料厚度与是否需要背胶。"},
        ],
        "defaultBinding": "裁切 / 裱板",
        "recommendation": "以展示距离和安装方式选择介质，不要只按屏幕效果选纸。",
    },
    "喷画": {
        "category": "广告物料",
        "aliases": ["喷画", "喷绘", "写真", "户外广告", "广告布"],
        "summary": "大幅面输出类物料，重点是观看距离、室内外环境、介质和边缘加工。",
        "parameters": [
            {"key": "displayMaterial", "label": "喷印介质", "required": True, "question": "用背胶、灯片、车贴、灯布还是其他介质？", "hint": "介质决定透光、耐候、画面质感和安装方法。"},
            {"key": "install", "label": "安装与加工", "required": True, "question": "需要打孔、穿绳、包边还是裱板？", "hint": "大幅面通常需要在四边预留加工和安装空间。"},
            {"key": "viewingDistance", "label": "观看距离", "required": False, "question": "主要观看距离大约是多少？", "hint": "观看距离影响分辨率和喷印精度建议。"},
        ],
        "defaultBinding": "裁切 / 打孔 / 包边",
        "recommendation": "先确认室内外和观看距离，再选介质、分辨率及边缘加工。",
    },
    "PVC": {
        "category": "广告物料",
        "aliases": ["PVC", "PVC板", "雪弗板", "KT板", "展板"],
        "summary": "板材类展示品，板材厚度、尺寸、画面覆面和安装方式决定最终效果。",
        "parameters": [
            {"key": "boardThickness", "label": "板材厚度", "required": True, "question": "板材需要 3mm、5mm 还是其他厚度？", "hint": "厚度影响挺度、重量和安装方式。"},
            {"key": "install", "label": "安装方式", "required": True, "question": "需要背胶、挂装、支架还是裱框？", "hint": "先定安装方式才能匹配板材和背面加工。"},
        ],
        "defaultBinding": "裁切 / 裱面",
        "recommendation": "先确认板材厚度和安装承重，再确定喷印介质与边缘处理。",
    },
    "数码印刷": {
        "category": "单张",
        "aliases": ["数码印刷", "数码快印", "快印", "短版印刷"],
        "summary": "适合短版、个性化和快速交付，数量、是否可变数据和文件页序是重点。",
        "parameters": [
            {"key": "variableData", "label": "可变数据", "required": False, "question": "每份内容或编号需要变化吗？", "hint": "可变数据会影响文件整理、校对和生产流程。"},
            {"key": "proofing", "label": "打样方式", "required": False, "question": "需要先打样确认颜色吗？", "hint": "短版项目可以先做数码样确认版式与颜色。"},
        ],
        "defaultBinding": "按文件输出",
        "recommendation": "数量较少或内容需要变化时优先考虑数码印刷，先确认文件页序。",
    },
}


GENERIC_PROFILE = {
    "category": "其他印刷品",
    "aliases": [],
    "summary": "暂未匹配到具体品类，将先按通用印刷参数收集。",
    "parameters": [],
    "defaultBinding": "无需装订",
    "recommendation": "先确认成品用途、尺寸、材料、颜色、数量和交期。",
}


def alias_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for product, profile in PRODUCT_CATALOG.items():
        for alias in [product, *profile.get("aliases", [])]:
            mapping[alias] = product
    return mapping


def find_product(text: str) -> str:
    """Return the longest matching canonical product name."""
    mapping = alias_map()
    for alias in sorted(mapping, key=len, reverse=True):
        if alias in text:
            return mapping[alias]
    return ""


def profile_for(product: str) -> dict[str, Any]:
    return deepcopy(PRODUCT_CATALOG.get(product, GENERIC_PROFILE))


def _value_for(order: dict[str, Any], key: str) -> Any:
    if key in order and order.get(key):
        return order.get(key)
    specs = order.get("productSpecs") or {}
    return specs.get(key)


def parameter_state(order: dict[str, Any]) -> dict[str, Any]:
    """Return profile metadata with filled/missing product-specific parameters."""
    profile = profile_for(order.get("productType", ""))
    parameters = []
    missing = []
    for item in profile.get("parameters", []):
        current = _value_for(order, item["key"])
        item = deepcopy(item)
        item["value"] = current or ""
        item["filled"] = bool(current)
        parameters.append(item)
        if item.get("required") and not current:
            missing.append(item)
    profile["parameters"] = parameters
    profile["missing"] = missing
    profile["readiness"] = round((len(parameters) - len(missing)) / len(parameters) * 100) if parameters else 100
    return profile
