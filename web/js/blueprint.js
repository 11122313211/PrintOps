/* PrintOps 交互式“规格与印前示意图”引擎（零第三方依赖原生 ESM）。
 * 纯几何/纸厚/书脊/折页/盒型/PDF 叠层计算函数不访问 window/document，可在 Node 中直接导入验证；
 * DOM/SVG 渲染函数全部采用 createElement / createElementNS / textContent，严禁拼接原始 HTML 字符串。
 */

const SVG_NS = "http://www.w3.org/2000/svg";
const PT_TO_MM = 25.4 / 72;
const MAX_DIMENSION_MM = 50000;
const PDF_TOLERANCE_MM = 1.5;

const STANDARD_SIZES_MM = {
  A0: [841, 1189],
  A1: [594, 841],
  A2: [420, 594],
  A3: [297, 420],
  A4: [210, 297],
  A5: [148, 210],
  A6: [105, 148],
  B3: [353, 500],
  B4: [250, 353],
  B5: [176, 250],
  B6: [125, 176],
};

const UNIT_FACTORS_MM = {
  mm: 1,
  "毫米": 1,
  cm: 10,
  "厘米": 10,
  m: 1000,
  "米": 1000,
  in: 25.4,
  inch: 25.4,
  "英寸": 25.4,
  pt: PT_TO_MM,
};

export const PAPER_BULK_FACTORS = {
  coated: { factor: 0.90, label: "铜版纸" },
  matte: { factor: 1.00, label: "哑粉纸" },
  offset: { factor: 1.25, label: "双胶纸" },
  card_specialty: { factor: 1.30, label: "白卡/特种纸" },
  bulky: { factor: 1.55, label: "轻型/纯质纸" },
  default: { factor: 1.05, label: "常规纸张" },
};

const SPINE_DISCLAIMER = "参考书脊厚度按通用松厚度系数估算，正式封面排版前请以印厂实纸打样为准";
const BLUEPRINT_DISCLAIMER = "仅作印前规格示意与参考估算，不构成最终刀模尺寸、供应商报价或专业 RIP 印前放行结论。";

const CUP_PRESETS = {
  250: { volumeMl: 250, volumeLabel: "250 ml（约 9 oz）", topDiaMm: 75, bottomDiaMm: 53, heightMm: 86, printableHeightMm: 62 },
  350: { volumeMl: 350, volumeLabel: "350 ml（约 12 oz）", topDiaMm: 82, bottomDiaMm: 57, heightMm: 108, printableHeightMm: 82 },
  500: { volumeMl: 500, volumeLabel: "500 ml（约 16 oz）", topDiaMm: 90, bottomDiaMm: 60, heightMm: 132, printableHeightMm: 104 },
};

const BOOKLET_PRODUCTS = new Set(["宣传册", "画册", "书籍", "产品册", "手册", "说明书", "目录册", "企业画册", "品牌画册"]);
const BOX_PRODUCTS = new Set(["包装盒", "彩盒", "纸盒", "礼品盒", "包装彩盒"]);
const CUP_PRODUCTS = new Set(["纸杯", "咖啡杯", "饮料杯", "杯套"]);
const FOLDED_PRODUCTS = new Set(["折页", "三折页", "二折页", "风琴折", "折页单"]);

// 容器级图层开关状态持久化，避免重绘时重置用户勾选。
const layerStateMap = new WeakMap();
let svgUidCounter = 0;

function round1(value) {
  return Math.round(value * 10) / 10;
}

function fmtMm(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const r = round1(value);
  return Number.isInteger(r) ? String(r) : r.toFixed(1);
}

function fmtSizePair(dims) {
  if (!Array.isArray(dims) || dims.length < 2) return "";
  return `${dims.map(fmtMm).join("×")} mm`;
}

function isValidMm(value) {
  return typeof value === "number" && Number.isFinite(value) && value > 0 && value <= MAX_DIMENSION_MM;
}

function normalizeUnit(unitRaw) {
  if (!unitRaw) return "mm";
  const key = String(unitRaw).trim().toLowerCase();
  return UNIT_FACTORS_MM[key] != null ? key : null;
}

function sanitizeMmArray(values) {
  if (!Array.isArray(values) || (values.length !== 2 && values.length !== 3)) return null;
  const result = [];
  for (const raw of values) {
    const num = Number(raw);
    if (!Number.isFinite(num) || num <= 0 || num > MAX_DIMENSION_MM) return null;
    const rounded = round1(num);
    if (!isValidMm(rounded)) return null;
    result.push(rounded);
  }
  return result;
}

/**
 * 解析常见平面/三维尺寸表达为毫米数组 [w, h] 或 [l, w, h]（保留 1 位小数精度）。
 * 支持字符串（"90×54 mm"、"210x297"、"120*80*60 mm"、"A4"、"12×8×6cm"）、数组与对象。
 */
export function parseSizeMm(raw) {
  if (raw == null || typeof raw === "boolean" || typeof raw === "number") return null;

  if (Array.isArray(raw)) {
    return sanitizeMmArray(raw);
  }

  if (typeof raw === "object") {
    const unitKey = normalizeUnit(raw.unit || "mm");
    if (!unitKey) return null;
    const factor = UNIT_FACTORS_MM[unitKey];
    const hasLength = raw.length != null && raw.length !== "";
    const hasWidth = raw.width != null && raw.width !== "";
    const hasHeight = raw.height != null && raw.height !== "";
    const hasDepth = raw.depth != null && raw.depth !== "";
    const hasShort = raw.w != null && raw.h != null;

    let axes = null;
    if (hasLength && hasWidth && hasHeight) {
      axes = [raw.length, raw.width, raw.height];
    } else if (hasWidth && hasHeight && hasDepth) {
      axes = [raw.width, raw.height, raw.depth];
    } else if (hasWidth && hasHeight) {
      axes = [raw.width, raw.height];
    } else if (raw.l != null && raw.w != null && raw.h != null) {
      axes = [raw.l, raw.w, raw.h];
    } else if (hasShort && raw.d != null) {
      axes = [raw.w, raw.h, raw.d];
    } else if (hasShort) {
      axes = [raw.w, raw.h];
    }
    if (!axes) return null;
    const converted = axes.map((v) => {
      if (typeof v === "boolean" || v === "" || v == null) return NaN;
      return Number(v) * factor;
    });
    return sanitizeMmArray(converted);
  }

  if (typeof raw !== "string") return null;
  const trimmed = raw.normalize("NFKC").trim();
  if (!trimmed || trimmed.length > 256) return null;

  // 若包含明确数字尺寸（如 "A4 (210×297 mm)"），优先走数值解析，否则回退标准纸幅名。
  const cleaned = trimmed
    .replace(/毫米/g, "mm")
    .replace(/厘米/g, "cm")
    .replace(/英寸/g, "in")
    .replace(/米/g, "m")
    .replace(/"/g, "in")
    .replace(/[（(][^）)]*[）)]/g, (group) => {
      // 括号里如果含乘号数字尺寸，则展开内容便于提取；否则去掉说明文字。
      return /[x×✕✖*]/.test(group) ? ` ${group.slice(1, -1)} ` : " ";
    });

  // 剔除前导中文标签或纸幅别名（如 "成品尺寸:"、"A4"、"约"、"长/宽/高"），保留负号以拦截非法负值。
  const stripped = cleaned
    .replace(/^(?:成品|展开|刀模|盒体|包装|内尺寸|外尺寸|内径|外径|规格|尺寸|文件|页面|约计|约|单页)[:：\s]*/gi, "")
    .replace(/^(?:(?:[AB]-?\d)|(?:大|正)\s*16\s*开)\s+(?=-?\d)/i, "")
    .replace(/[长宽高深厚][:：\s]*(?=-?\d)/g, "")
    .trim();

  // 检查完整轴切分：支持 2 轴或 3 轴，拒绝 4 轴及以上。
  const sepRe = /\s*(?:[xX×✕✖*]|\\\*)\s*/;
  if (sepRe.test(stripped)) {
    const parts = stripped.split(sepRe).filter((p) => p !== "");
    if (parts.length !== 2 && parts.length !== 3) return null;
    const parsedAxes = parts.map((axis) => {
      const m = axis.match(/^\s*(-?\d+(?:\.\d+)?)\s*(mm|cm|in|inch|m|pt)?\s*$/i);
      if (!m) return null;
      const val = Number(m[1]);
      if (!Number.isFinite(val) || val <= 0) return null;
      return { val, unit: m[2] ? m[2].toLowerCase() : null };
    });
    if (parsedAxes.every(Boolean)) {
      const trailingUnit = parsedAxes[parsedAxes.length - 1].unit || "mm";
      const mmValues = parsedAxes.map((item) => item.val * (UNIT_FACTORS_MM[item.unit || trailingUnit] || 1));
      return sanitizeMmArray(mmValues);
    }
    return null;
  }

  // ISO 标准纸张代号（A0–A6, B3–B6）
  const isoMatch = trimmed.replace(/\s+/g, "").toUpperCase().match(/^([AB])-?(\d)$/);
  if (isoMatch) {
    const key = `${isoMatch[1]}${isoMatch[2]}`;
    if (STANDARD_SIZES_MM[key]) return [...STANDARD_SIZES_MM[key]];
  }

  // 中文常见印刷开度别名
  if (/大\s*16\s*开/.test(trimmed)) return [210, 285];
  if (/正\s*16\s*开/.test(trimmed)) return [185, 260];

  return null;
}

/**
 * 从纸张描述（如 "157 g/m² 铜版纸"、"300g白卡纸"、"80克双胶纸"）提取克重与通用松厚度参考系数。
 */
export function parsePaperGsm(paperText) {
  const text = paperText == null ? "" : String(paperText).normalize("NFKC").trim();

  // 解析克重（g/m², gsm, g, 克），排除负号与明显越界值。
  let gsm = null;
  if (typeof paperText === "number" && Number.isFinite(paperText)) {
    if (paperText >= 20 && paperText <= 2000) gsm = round1(paperText);
  } else if (text) {
    const gsmMatch = text.match(/(?<![-\d.])(\d+(?:\.\d+)?)\s*(?:g\s*\/\s*(?:m[²2]|㎡)|gsm|克\s*\/\s*平方米|克重|克|g(?![a-z]))/i);
    if (gsmMatch) {
      const idx = gsmMatch.index || 0;
      const hasLeadingMinus = idx > 0 && text[idx - 1] === "-";
      const val = Number(gsmMatch[1]);
      if (!hasLeadingMinus && Number.isFinite(val) && val >= 20 && val <= 2000) {
        gsm = round1(val);
      }
    } else if (/^\d+(?:\.\d+)?$/.test(text)) {
      const val = Number(text);
      if (Number.isFinite(val) && val >= 20 && val <= 2000) gsm = round1(val);
    }
  }

  // 按顺序匹配纸种：先识别哑粉/无光铜，再识别轻型/纯质，再识别双胶/白卡/铜版。
  let kind = "default";
  let bulkFactor = PAPER_BULK_FACTORS.default.factor;
  let kindLabel = PAPER_BULK_FACTORS.default.label;

  if (/哑粉|无光铜|哑光铜|哑铜/.test(text)) {
    kind = "matte";
    bulkFactor = PAPER_BULK_FACTORS.matte.factor;
    kindLabel = "哑粉纸";
  } else if (/轻型纸|轻型|纯质纸|纯质|蒙肯/.test(text)) {
    kind = "bulky";
    bulkFactor = PAPER_BULK_FACTORS.bulky.factor;
    kindLabel = /纯质/.test(text) ? "纯质纸" : "轻型纸";
  } else if (/双胶|胶版|道林|书写/.test(text)) {
    kind = "offset";
    bulkFactor = PAPER_BULK_FACTORS.offset.factor;
    if (/道林/.test(text)) kindLabel = "道林纸";
    else if (/书写/.test(text)) kindLabel = "书写纸";
    else if (/胶版/.test(text)) kindLabel = "胶版纸";
    else kindLabel = "双胶纸";
  } else if (/白卡|黑卡|卡纸|牛皮|特种|艺术|超感|触感/.test(text)) {
    kind = "card_specialty";
    bulkFactor = PAPER_BULK_FACTORS.card_specialty.factor;
    if (/白卡/.test(text)) kindLabel = "白卡纸";
    else if (/牛皮/.test(text)) kindLabel = "牛皮纸";
    else if (/艺术/.test(text)) kindLabel = "艺术纸";
    else if (/特种/.test(text)) kindLabel = "特种纸";
    else kindLabel = "卡纸/特种纸";
  } else if (/铜版|光铜|亮铜/.test(text)) {
    kind = "coated";
    bulkFactor = PAPER_BULK_FACTORS.coated.factor;
    kindLabel = "铜版纸";
  }

  return { gsm, kind, bulkFactor, kindLabel };
}

function parsePagesCount(rawPages) {
  if (rawPages == null || rawPages === "" || typeof rawPages === "boolean") return null;
  if (typeof rawPages === "number") {
    return Number.isFinite(rawPages) && rawPages > 0 && rawPages <= 5000 ? Math.round(rawPages) : null;
  }
  const text = String(rawPages).normalize("NFKC").trim();
  if (!text) return null;
  // 拦截明确负数
  if (/-\s*\d+/.test(text)) return null;
  const match = text.match(/(\d+)\s*(?:页|[Pp])?/);
  if (!match) return null;
  const count = Number(match[1]);
  return Number.isFinite(count) && count > 0 && count <= 5000 ? count : null;
}

/**
 * 估算画册/宣传册/书籍参考书脊厚度（mm）并输出骑马钉装订风险预警。
 */
export function estimateSpineMm({ pages, paper, coverPaper, binding } = {}) {
  const pageCount = parsePagesCount(pages);
  const innerPaper = parsePaperGsm(paper);
  const coverInfo = coverPaper ? parsePaperGsm(coverPaper) : null;
  const bindingText = String(binding || "").trim();
  const isSaddleStitch = /骑马钉|骑马装/.test(bindingText);

  const warnings = [];
  if (isSaddleStitch && pageCount != null && pageCount % 4 !== 0) {
    warnings.push(`骑马钉页数须为 4 的倍数（当前 ${pageCount}P），请核对正反面页码编排`);
  }

  if (pageCount == null || innerPaper.gsm == null) {
    if (isSaddleStitch && pageCount != null && pageCount >= 48) {
      warnings.unshift("骑马钉页数偏厚，装订后易起拱或书脊爆裂，建议评估改为胶装/锁线胶装");
    }
    return {
      available: false,
      spineMm: null,
      pages: pageCount,
      sheets: pageCount != null ? pageCount / 2 : null,
      gsm: innerPaper.gsm,
      kindLabel: innerPaper.kindLabel,
      bulkFactor: innerPaper.bulkFactor,
      warning: warnings.length ? warnings.join("；") : null,
      disclaimer: SPINE_DISCLAIMER,
    };
  }

  const totalSheets = pageCount / 2;
  const useSeparateCover = Boolean(!isSaddleStitch && coverInfo && coverInfo.gsm != null && pageCount > 4);

  let rawThickness;
  let innerSheets = totalSheets;
  let coverSheets = 0;

  if (useSeparateCover) {
    innerSheets = (pageCount - 4) / 2;
    coverSheets = 2;
    rawThickness =
      innerSheets * (innerPaper.gsm / 1000) * innerPaper.bulkFactor +
      coverSheets * (coverInfo.gsm / 1000) * coverInfo.bulkFactor;
  } else {
    rawThickness = totalSheets * (innerPaper.gsm / 1000) * innerPaper.bulkFactor;
  }

  const spineMm = Math.max(0.2, round1(rawThickness));

  if (isSaddleStitch && (pageCount >= 48 || spineMm >= 3.5)) {
    warnings.unshift("骑马钉页数偏厚，装订后易起拱或书脊爆裂，建议评估改为胶装/锁线胶装");
  }

  const result = {
    available: true,
    spineMm,
    pages: pageCount,
    sheets: totalSheets,
    gsm: innerPaper.gsm,
    kindLabel: innerPaper.kindLabel,
    bulkFactor: innerPaper.bulkFactor,
    warning: warnings.length ? warnings.join("；") : null,
    disclaimer: SPINE_DISCLAIMER,
  };
  if (useSeparateCover) {
    result.innerSheets = innerSheets;
    result.coverSheets = coverSheets;
    result.coverGsm = coverInfo.gsm;
    result.coverKindLabel = coverInfo.kindLabel;
  }
  return result;
}

function parseBleedMm(rawBleed, productType, targetSizeMm) {
  if (rawBleed != null && rawBleed !== "") {
    const text = String(rawBleed).normalize("NFKC");
    if (/无出血|不出血|0\s*mm/i.test(text)) return 0;
    const m = text.match(/(\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米)?/i);
    if (m) {
      const unit = (m[2] || "mm").toLowerCase();
      const mm = Number(m[1]) * (UNIT_FACTORS_MM[unit] || 1);
      if (Number.isFinite(mm) && mm >= 0 && mm <= 30) return round1(mm);
    }
  }
  if (Array.isArray(targetSizeMm)) {
    const minSide = Math.min(targetSizeMm[0], targetSizeMm[1]);
    if (minSide < 30) return 2;
  }
  if (productType === "喷画") return 5;
  return 3;
}

function parseRoundedCorners(specs, finishing, productType) {
  const cornerText = String(specs.cardCorners || "").trim();
  const shapeText = String(specs.labelShape || "").trim();
  const finishText = String(finishing || "").trim();
  const combined = `${cornerText} ${shapeText} ${finishText}`;

  if (/直角|不要圆角|无需圆角|不切圆角/.test(cornerText)) {
    return { enabled: false, radiusMm: 0, label: "" };
  }
  if (/圆角|\bR\s*\d+/i.test(combined)) {
    const rMatch = combined.match(/R\s*(\d+(?:\.\d+)?)|圆角\s*(\d+(?:\.\d+)?)\s*(?:mm)?/i);
    const parsedR = rMatch ? Number(rMatch[1] || rMatch[2]) : NaN;
    const radiusMm = Number.isFinite(parsedR) && parsedR > 0 && parsedR <= 20 ? round1(parsedR) : (productType === "PVC卡" ? 3.2 : 3);
    return { enabled: true, radiusMm, label: `圆角 R${fmtMm(radiusMm)}` };
  }
  return { enabled: false, radiusMm: 0, label: "" };
}

function parseFoldingConfig(order, finishedMm, expandedMm) {
  const specs = order.productSpecs || {};
  const rawFold = String(specs.folding || "").trim();
  const productType = String(order.productType || "").trim();
  const combined = `${rawFold} ${productType} ${order.binding || ""}`;

  let type = "tri_roll";
  let panels = 3;
  let label = "三折页（包心折）";
  let tip = "包心折内页通常比外页窄 1~2mm（如 297mm 展开按 100+100+97mm 分幅），避免折叠顶纸起皱。";

  if (/风琴|Z字|Z折|手风琴|折扇/i.test(combined)) {
    type = "accordion";
    panels = /四折|4折/.test(combined) ? 4 : 3;
    label = panels === 4 ? "四折风琴折" : "风琴折（Z字折）";
    tip = "风琴折各折幅等宽交替正反折，折痕处建议避开细小文字与满版深色折压。";
  } else if (/关门|对门/.test(combined)) {
    type = "gate";
    panels = 3;
    label = "关门折（对门折）";
    tip = "关门折左右双扉页向内合拢，内扉页总宽宜比中幅略窄 1~2mm 避免中缝顶纸。";
  } else if (/四折|4折/.test(combined)) {
    type = "four_fold";
    panels = 4;
    label = "四折页";
    tip = "四折页压痕密集，纸张超过 157g 时建议先压痕后折叠，防止折线爆色。";
  } else if (/二折|对折|2折|双折/.test(combined)) {
    type = "bi_fold";
    panels = 2;
    label = "二折页（对折）";
    tip = "对折页展开宽为成品宽的 2 倍；双面跨折线图文需提前核对压痕居中位置。";
  } else if (!rawFold && productType === "折页") {
    label = "折页（暂按三折包心折示意）";
  }

  let resolvedExpanded = expandedMm ? [...expandedMm] : null;
  let resolvedFinished = finishedMm ? [...finishedMm] : null;
  let expandedInferred = false;

  if (resolvedExpanded) {
    // 展开页默认沿长边排布折幅（例如 210×297 展开三折时，297mm 为折幅总宽）。
    if (resolvedExpanded[0] < resolvedExpanded[1]) {
      const matchesVertical =
        resolvedFinished &&
        Math.abs(resolvedExpanded[1] - resolvedFinished[0] * panels) <
          Math.abs(resolvedExpanded[0] - resolvedFinished[0] * panels);
      if (matchesVertical || !resolvedFinished) {
        resolvedExpanded = [resolvedExpanded[1], resolvedExpanded[0]];
      }
    }
  } else if (resolvedFinished) {
    const fw = resolvedFinished[0];
    const fh = resolvedFinished[1];
    if (type === "tri_roll") {
      const innerNarrow = fw >= 60 ? 2 : 1;
      const expW = round1(fw * 2 + Math.max(10, fw - innerNarrow));
      resolvedExpanded = [expW, fh];
    } else if (type === "gate") {
      resolvedExpanded = [round1(fw * 2), fh];
    } else {
      resolvedExpanded = [round1(fw * panels), fh];
    }
    expandedInferred = true;
  }

  let panelWidthsMm = null;
  if (resolvedExpanded) {
    const totalW = resolvedExpanded[0];
    if (type === "tri_roll") {
      if (Math.abs(totalW - 297) < 0.2) {
        panelWidthsMm = [100, 100, 97];
      } else {
        const gap = totalW >= 150 ? 2 : 1;
        const outer = round1((totalW + gap) / 3);
        const inner = round1(totalW - outer * 2);
        panelWidthsMm = [outer, outer, Math.max(1, inner)];
      }
    } else if (type === "gate") {
      const mid = round1(totalW * 0.5);
      const wing = round1((totalW - mid) / 2);
      const rightWing = round1(totalW - mid - wing);
      panelWidthsMm = [wing, mid, rightWing];
    } else if (type === "bi_fold") {
      const half = round1(totalW / 2);
      panelWidthsMm = [half, round1(totalW - half)];
    } else {
      const each = round1(totalW / panels);
      const list = Array.from({ length: panels - 1 }, () => each);
      list.push(round1(totalW - each * (panels - 1)));
      panelWidthsMm = list;
    }

    if (!resolvedFinished && panelWidthsMm) {
      resolvedFinished = [type === "gate" ? panelWidthsMm[1] : panelWidthsMm[0], resolvedExpanded[1]];
    }
  }

  return {
    type,
    label,
    panels,
    panelWidthsMm,
    expandedSizeMm: resolvedExpanded,
    finishedSizeMm: resolvedFinished,
    expandedInferred,
    tip,
  };
}

function parseCupSpec(specs) {
  const raw = String(specs.cupVolume || "").normalize("NFKC").trim();
  let ml = null;
  if (raw) {
    const mlMatch = raw.match(/(\d+(?:\.\d+)?)\s*(?:ml|毫升)/i);
    const ozMatch = raw.match(/(\d+(?:\.\d+)?)\s*(?:oz|盎司)/i);
    if (mlMatch) ml = Math.round(Number(mlMatch[1]));
    else if (ozMatch) ml = Math.round(Number(ozMatch[1]) * 29.57);
    else if (/^\d+$/.test(raw)) ml = Number(raw);
  }
  if (ml && CUP_PRESETS[ml]) return { ...CUP_PRESETS[ml], matchedPreset: true };
  if (ml) {
    const closest = ml <= 290 ? CUP_PRESETS[250] : ml <= 420 ? CUP_PRESETS[350] : CUP_PRESETS[500];
    return {
      ...closest,
      volumeMl: ml,
      volumeLabel: `${ml} ml（示意比例）`,
      matchedPreset: false,
    };
  }
  return {
    ...CUP_PRESETS[350],
    volumeMl: null,
    volumeLabel: "容量待确认（暂按 350 ml 示意）",
    matchedPreset: false,
  };
}

function boxPointsToMm(box) {
  if (!Array.isArray(box) || box.length !== 4) return null;
  const nums = box.map(Number);
  if (!nums.every(Number.isFinite)) return null;
  const w = round1(Math.abs(nums[2] - nums[0]) * PT_TO_MM);
  const h = round1(Math.abs(nums[3] - nums[1]) * PT_TO_MM);
  return isValidMm(w) && isValidMm(h) ? [w, h] : null;
}

function extractCheckSizeMm(checks, labelPattern) {
  if (!Array.isArray(checks)) return null;
  const found = checks.find((c) => c && labelPattern.test(String(c.label || "")));
  if (!found || !found.detail) return null;
  return parseSizeMm(String(found.detail));
}

function buildPdfOverlay(pdfInspection, targetSizeMm, targetLabel) {
  if (!pdfInspection || typeof pdfInspection !== "object") return null;

  // 兼容直接传 inspection、preflightResult 或 appState.pdfPreview 外层包装。
  const inner = pdfInspection.inspection && typeof pdfInspection.inspection === "object"
    ? pdfInspection.inspection
    : pdfInspection;
  const boxes = inner.boxes && typeof inner.boxes === "object" ? inner.boxes : {};
  const checks = Array.isArray(pdfInspection.checks)
    ? pdfInspection.checks
    : Array.isArray(pdfInspection.preflight?.checks)
      ? pdfInspection.preflight.checks
      : null;

  const trimMm = boxPointsToMm(boxes.trim) || extractCheckSizeMm(checks, /TrimBox/i);
  const cropMm = boxPointsToMm(boxes.crop);
  const mediaMm = boxPointsToMm(boxes.media) || extractCheckSizeMm(checks, /MediaBox/i);
  const bleedBoxMm = boxPointsToMm(boxes.bleed) || extractCheckSizeMm(checks, /BleedBox/i);

  const directObserved = parseSizeMm(pdfInspection.observedSizeMm || inner.observedSizeMm);
  const directBleedBox = parseSizeMm(pdfInspection.bleedSizeMm || inner.bleedSizeMm);

  let observedSizeMm = null;
  let source = "";
  if (directObserved && directObserved.length === 2) {
    observedSizeMm = directObserved;
    source = String(pdfInspection.boxSource || inner.boxSource || "PDF 页面");
  } else if (trimMm) {
    observedSizeMm = trimMm;
    source = "TrimBox";
  } else if (cropMm) {
    observedSizeMm = cropMm;
    source = "CropBox";
  } else if (mediaMm) {
    observedSizeMm = mediaMm;
    source = "MediaBox";
  }

  if (!observedSizeMm) return null;

  const outerBleedSizeMm = directBleedBox || bleedBoxMm || (trimMm && mediaMm ? mediaMm : null);
  let measuredBleedMm = null;
  if (typeof pdfInspection.bleedMm === "number" && Number.isFinite(pdfInspection.bleedMm) && pdfInspection.bleedMm >= 0) {
    measuredBleedMm = round1(pdfInspection.bleedMm);
  } else if (typeof inner.bleedMm === "number" && Number.isFinite(inner.bleedMm) && inner.bleedMm >= 0) {
    measuredBleedMm = round1(inner.bleedMm);
  } else if (trimMm && outerBleedSizeMm) {
    const bx = (outerBleedSizeMm[0] - trimMm[0]) / 2;
    const by = (outerBleedSizeMm[1] - trimMm[1]) / 2;
    if (bx >= -0.05 && by >= -0.05) {
      measuredBleedMm = round1(Math.max(0, Math.min(bx, by)));
    }
  }

  const fileName = String(pdfInspection.fileName || inner.fileName || "").trim();
  const pageCount = Number.isInteger(inner.pageCount) ? inner.pageCount : null;
  const colorSpaces = Array.isArray(inner.colorSpaces) ? inner.colorSpaces.slice(0, 6) : [];

  if (!Array.isArray(targetSizeMm) || targetSizeMm.length !== 2) {
    const bleedNote = measuredBleedMm != null ? `，实测出血约 ${fmtMm(measuredBleedMm)} mm` : "";
    return {
      available: true,
      source,
      fileName,
      pageCount,
      colorSpaces,
      observedSizeMm,
      alignedSizeMm: [...observedSizeMm],
      bleedBoxSizeMm: outerBleedSizeMm,
      rotated: false,
      matches: null,
      deltaW: null,
      deltaH: null,
      measuredBleedMm,
      status: "info",
      summary: `PDF ${source} 约 ${fmtSizePair(observedSizeMm)}${bleedNote}；当前订单尚无对应平面目标尺寸可供叠层比对。`,
    };
  }

  const [tw, th] = targetSizeMm;
  const [ow, oh] = observedSizeMm;
  const directErr = Math.max(Math.abs(ow - tw), Math.abs(oh - th));
  const rotatedErr = Math.max(Math.abs(oh - tw), Math.abs(ow - th));
  const rotated = rotatedErr + 1e-6 < directErr;
  const alignedSizeMm = rotated ? [oh, ow] : [ow, oh];
  const alignedBleedBox = outerBleedSizeMm
    ? (rotated ? [outerBleedSizeMm[1], outerBleedSizeMm[0]] : [...outerBleedSizeMm])
    : null;

  const deltaW = round1(alignedSizeMm[0] - tw);
  const deltaH = round1(alignedSizeMm[1] - th);
  const matches = Math.abs(deltaW) <= PDF_TOLERANCE_MM && Math.abs(deltaH) <= PDF_TOLERANCE_MM;
  const status = matches ? "match" : "mismatch";

  const rotHint = rotated ? "（旋转 90° 对齐）" : "";
  const bleedHint = measuredBleedMm != null ? `，实测出血约 ${fmtMm(measuredBleedMm)} mm` : "";
  let summary;
  if (matches) {
    summary = `PDF ${source} ${fmtSizePair(observedSizeMm)}${rotHint}与${targetLabel} ${fmtSizePair(targetSizeMm)} 一致${bleedHint}。`;
  } else {
    const sign = (v) => (v > 0 ? `+${fmtMm(v)}` : fmtMm(v));
    const looksLikeBleedIncluded =
      source === "MediaBox" &&
      deltaW >= 3 &&
      deltaH >= 3 &&
      Math.abs(deltaW - deltaH) <= 1.5 &&
      deltaW <= 14;
    const extraHint = looksLikeBleedIncluded
      ? `（疑似已含约 ${fmtMm(deltaW / 2)} mm 四边出血但未写入 TrimBox，建议人工核对裁切线）`
      : "，建议核对文件尺寸或展开尺寸设定";
    summary = `PDF ${source} ${fmtSizePair(observedSizeMm)}${rotHint}与${targetLabel} ${fmtSizePair(targetSizeMm)} 偏差 ${sign(deltaW)} / ${sign(deltaH)} mm${bleedHint}${extraHint}。`;
  }

  return {
    available: true,
    source,
    fileName,
    pageCount,
    colorSpaces,
    observedSizeMm,
    alignedSizeMm,
    bleedBoxSizeMm: alignedBleedBox,
    rotated,
    matches,
    deltaW,
    deltaH,
    measuredBleedMm,
    status,
    summary,
  };
}

/**
 * 根据订单规格与可选 PDF 预检结果构建纯数据蓝图模型。
 */
export function buildBlueprintModel(order, pdfInspection = null) {
  if (!order || typeof order !== "object") {
    return {
      kind: "empty",
      productType: "",
      title: "印前规格蓝图",
      subtitle: "",
      badges: [],
      targetSizeMm: null,
      finishedSizeMm: null,
      expandedSizeMm: null,
      flatSizeMm: null,
      boxSizeMm: null,
      boxStructure: "",
      packageSizeBasis: "",
      bleedMm: 3,
      safeMarginMm: 3,
      roundedCorners: { enabled: false, radiusMm: 0, label: "" },
      shape: "rect",
      hangHole: "",
      folding: null,
      spine: null,
      coverSpreadMm: null,
      cup: null,
      pdfOverlay: null,
      notes: [],
      disclaimer: BLUEPRINT_DISCLAIMER,
    };
  }

  const productType = String(order.productType || "").trim();
  const dims = order.dimensions && typeof order.dimensions === "object" ? order.dimensions : {};
  const specs = order.productSpecs && typeof order.productSpecs === "object" ? order.productSpecs : {};

  // 提取各类候选尺寸
  const parsedMainSize = parseSizeMm(order.size);
  const finishedFromDims = parseSizeMm(dims.finishedSize);
  const expandedFromDims = parseSizeMm(dims.expandedSize || specs.expandedSize);
  const dieCutFromDims = parseSizeMm(dims.dieCutSize || specs.dieCutSize);
  const specFlatSize = parseSizeMm(specs.envelopeSize || specs.displaySize || specs.boardSize);

  let package3dMm = null;
  let packageSizeBasis = "";
  const packageCandidates = [
    [dims.packageOuterSize || specs.boxSizeOuter, "外尺寸"],
    [dims.packageInnerSize || specs.boxSizeInner, "内尺寸"],
    [dims.packageSize || specs.boxSize || specs.bagSize, "成品三维"],
  ];
  for (const [candidate, basis] of packageCandidates) {
    const parsed = parseSizeMm(candidate);
    if (parsed && parsed.length === 3) {
      package3dMm = parsed;
      packageSizeBasis = basis;
      break;
    }
  }
  if (!package3dMm && parsedMainSize && parsedMainSize.length === 3) {
    package3dMm = parsedMainSize;
    packageSizeBasis = "成品三维";
  }

  let finishedMm =
    (finishedFromDims && finishedFromDims.length === 2 ? finishedFromDims : null) ||
    (parsedMainSize && parsedMainSize.length === 2 ? parsedMainSize : null) ||
    (specFlatSize && specFlatSize.length === 2 ? specFlatSize : null);

  const expandedMm = expandedFromDims && expandedFromDims.length === 2 ? expandedFromDims : null;
  const dieCutMm = dieCutFromDims && dieCutFromDims.length === 2 ? dieCutFromDims : null;

  // 处理显式版式方向（如 "A4" + "横版"）
  const orientation = String(order.orientation || "").trim();
  if (finishedMm && /^[AB]\d$/i.test(String(order.size || "").trim())) {
    if (/横/.test(orientation) && finishedMm[0] < finishedMm[1]) {
      finishedMm = [finishedMm[1], finishedMm[0]];
    } else if (/竖/.test(orientation) && finishedMm[0] > finishedMm[1]) {
      finishedMm = [finishedMm[1], finishedMm[0]];
    }
  }

  const hasFoldingSpec = Boolean(
    specs.folding && !/^(无|不折|无需折叠|单张不折)$/.test(String(specs.folding).trim())
  );
  const isBox = BOX_PRODUCTS.has(productType) || Boolean(package3dMm);
  const isCup = CUP_PRODUCTS.has(productType);
  const isFolded = !isBox && !isCup && (FOLDED_PRODUCTS.has(productType) || hasFoldingSpec);
  const isBooklet = !isBox && !isCup && !isFolded && BOOKLET_PRODUCTS.has(productType);

  const spine = isBooklet
    ? estimateSpineMm({
        pages: order.pages || specs.pages,
        paper: order.paper,
        coverPaper: specs.coverPaper,
        binding: order.binding || specs.binding,
      })
    : null;

  const hasAny2d = Boolean(finishedMm || expandedMm || dieCutMm);

  // 判定 kind
  let kind = "empty";
  if (isBox) {
    kind = "box";
  } else if (isCup) {
    kind = "cup";
  } else if (isFolded) {
    kind = "folded";
  } else if (isBooklet) {
    kind = hasAny2d || spine?.available || Boolean(spine?.warning) ? "booklet" : "empty";
  } else if (hasAny2d) {
    kind = "flat";
  }

  if (kind === "empty") {
    return {
      kind: "empty",
      productType,
      title: "印前规格蓝图",
      subtitle: "",
      badges: [],
      targetSizeMm: null,
      finishedSizeMm: null,
      expandedSizeMm: null,
      flatSizeMm: null,
      boxSizeMm: null,
      boxStructure: "",
      packageSizeBasis: "",
      bleedMm: 3,
      safeMarginMm: 3,
      roundedCorners: { enabled: false, radiusMm: 0, label: "" },
      shape: "rect",
      hangHole: "",
      folding: null,
      spine: null,
      coverSpreadMm: null,
      cup: null,
      pdfOverlay: null,
      notes: [],
      disclaimer: BLUEPRINT_DISCLAIMER,
    };
  }

  const badges = [];
  const notes = [];
  let targetSizeMm = null;
  let targetLabel = "成品尺寸";
  let expandedSizeMm = expandedMm;
  let flatSizeMm = dieCutMm || expandedMm || null;
  let folding = null;
  let cup = null;
  let coverSpreadMm = null;
  const boxStructure = String(specs.boxStructure || "").trim();
  const roundedCorners = parseRoundedCorners(specs, order.finishing, productType);
  const shape = /圆形|圆贴/.test(String(specs.labelShape || "")) ? "circle" : "rect";
  const hangHole = String(specs.hangHole || "").trim();

  if (productType) badges.push({ label: productType, tone: "accent" });

  if (kind === "box") {
    targetSizeMm = dieCutMm || expandedMm || finishedMm || null;
    targetLabel = dieCutMm ? "刀模尺寸" : "展开尺寸";
    flatSizeMm = dieCutMm || expandedMm || (package3dMm ? null : finishedMm);
    if (package3dMm) {
      badges.push({ label: `${packageSizeBasis || "三维"} ${fmtSizePair(package3dMm)}`, tone: "neutral" });
    } else {
      badges.push({ label: "三维尺寸待补充", tone: "warn" });
    }
    if (boxStructure) badges.push({ label: boxStructure, tone: "info" });
    if (flatSizeMm) {
      badges.push({ label: `${dieCutMm ? "刀模" : "展开"} ${fmtSizePair(flatSizeMm)}`, tone: "neutral" });
    }
    notes.push("包装盒三维尺寸须明确区分内尺寸与外尺寸，纸板厚度会影响内装物配合间隙。");
    if (!flatSizeMm) {
      notes.push("正式印刷前需向印厂确认结构刀模线（含粘口、插舌与压痕位置），再按刀模排版。");
    } else {
      notes.push(`已记录${dieCutMm ? "刀模" : "展开"}二维尺寸 ${fmtSizePair(flatSizeMm)}，出血与图文延展请沿刀模外框预留。`);
    }
  } else if (kind === "folded") {
    folding = parseFoldingConfig(order, finishedMm, expandedMm);
    expandedSizeMm = folding.expandedSizeMm;
    finishedMm = folding.finishedSizeMm;
    targetSizeMm = expandedSizeMm || finishedMm || null;
    targetLabel = "展开尺寸";
    badges.push({ label: folding.label, tone: "info" });
    if (expandedSizeMm) {
      badges.push({ label: `展开 ${fmtSizePair(expandedSizeMm)}${folding.expandedInferred ? "（推算）" : ""}`, tone: "neutral" });
    }
    if (finishedMm) {
      badges.push({ label: `成品 ${fmtSizePair(finishedMm)}`, tone: "neutral" });
    }
    notes.push(folding.tip);
    notes.push("折页文件须同时核对正反面页序、折痕压线位置及展开四边出血。");
  } else if (kind === "booklet") {
    targetSizeMm = finishedMm || expandedMm || null;
    targetLabel = "成品尺寸";
    if (targetSizeMm) badges.push({ label: `成品 ${fmtSizePair(targetSizeMm)}`, tone: "neutral" });
    if (order.binding || specs.binding) {
      badges.push({ label: String(order.binding || specs.binding), tone: "info" });
    }
    if (spine?.available) {
      badges.push({ label: `参考书脊 ≈ ${fmtMm(spine.spineMm)} mm`, tone: spine.warning ? "warn" : "ok" });
      if (targetSizeMm) {
        coverSpreadMm = [round1(targetSizeMm[0] * 2 + spine.spineMm), targetSizeMm[1]];
        notes.push(`封面连晒展开宽度参考：${fmtMm(coverSpreadMm[0])}×${fmtMm(coverSpreadMm[1])} mm（封底 + 书脊 ${fmtMm(spine.spineMm)}mm + 封面）。`);
      }
    } else {
      notes.push("补充页数（如 48P）与正文纸张克重（如 157g 铜版纸）后，可自动估算参考书脊厚度。");
    }
    if (spine?.warning) {
      notes.unshift(spine.warning);
    } else {
      notes.push("靠近装订边（内口）文字建议比普通安全边再多留 3~5mm，避免胶装吃口遮挡。");
    }
  } else if (kind === "cup") {
    cup = parseCupSpec(specs);
    targetSizeMm = null;
    badges.push({ label: cup.volumeLabel, tone: cup.matchedPreset ? "info" : "warn" });
    if (specs.cupMaterial) badges.push({ label: String(specs.cupMaterial), tone: "neutral" });
    if (specs.innerCoating) badges.push({ label: String(specs.innerCoating), tone: "neutral" });
    notes.push("纸杯杯身呈上宽下窄扇形展开，重要 Logo 与二维码需放置在中段可印安全区，避免卷口与杯底变形。");
    notes.push("不同模具厂的杯口卷边、杯高与扇形刀模存在差异，正式完稿前请向供应商索取对应容量刀模线。");
  } else {
    // flat
    targetSizeMm = finishedMm || expandedMm || dieCutMm;
    targetLabel = finishedMm ? "成品尺寸" : expandedMm ? "展开尺寸" : "刀模尺寸";
    if (targetSizeMm) badges.push({ label: `${targetLabel} ${fmtSizePair(targetSizeMm)}`, tone: "neutral" });
    if (roundedCorners.enabled) badges.push({ label: roundedCorners.label, tone: "info" });
    if (shape === "circle") badges.push({ label: "圆形模切", tone: "info" });
    if (hangHole) badges.push({ label: `挂孔 · ${hangHole}`, tone: "info" });
    if (specs.boardThickness) badges.push({ label: `板厚 ${specs.boardThickness}`, tone: "neutral" });
    if (specs.paperParts) badges.push({ label: `${specs.paperParts}无碳`, tone: "neutral" });
  }

  const bleedMm = parseBleedMm(specs.bleed, productType, targetSizeMm);
  const safeMarginMm =
    Array.isArray(targetSizeMm) && Math.min(targetSizeMm[0], targetSizeMm[1]) >= 140 ? 5 : 3;

  if (kind === "flat") {
    notes.push(`常规四边各预留 ${fmtMm(bleedMm)} mm 出血，底色/背景图延伸至红虚线，文字与 Logo 保持在绿点线安全边（内缩 ${fmtMm(safeMarginMm)} mm）以内。`);
    if (roundedCorners.enabled) {
      notes.push(`已标示 ${roundedCorners.label} 模切轮廓，四角边缘文字请进一步内移避开圆角裁切区。`);
    }
    if (hangHole) {
      notes.push("吊牌顶部挂孔区附近需避让关键文字与二维码，具体孔径与距边以刀模为准。");
    }
    if (productType === "喷画" || productType === "海报") {
      notes.push("大幅面喷印若含打孔、包边或绷架安装，请在订单中额外写明留白/包边尺寸。");
    }
  }

  const pdfOverlay = buildPdfOverlay(pdfInspection, targetSizeMm, targetLabel);
  if (pdfOverlay) {
    badges.push({
      label: pdfOverlay.status === "match" ? "PDF 尺寸一致" : pdfOverlay.status === "mismatch" ? "PDF 尺寸偏差" : "PDF 已读取",
      tone: pdfOverlay.status === "match" ? "ok" : pdfOverlay.status === "mismatch" ? "warn" : "info",
    });
  }

  const title = productType ? `印前规格蓝图 · ${productType}` : "印前规格蓝图";
  let subtitle = "";
  if (kind === "box") {
    subtitle = package3dMm
      ? `盒体${packageSizeBasis || "三维"} ${fmtSizePair(package3dMm)}${boxStructure ? ` · ${boxStructure}` : ""}${flatSizeMm ? ` · 展开/刀模 ${fmtSizePair(flatSizeMm)}` : ""}`
      : "包装盒三维结构示意（请补充长×宽×高 mm）";
  } else if (kind === "cup") {
    subtitle = `${cup.volumeLabel} · 示意杯高约 ${cup.heightMm} mm（以实模扇形刀模为准）`;
  } else if (kind === "folded") {
    subtitle = expandedSizeMm
      ? `展开 ${fmtSizePair(expandedSizeMm)} · 出血 ${fmtMm(bleedMm)} mm · 安全边 ${fmtMm(safeMarginMm)} mm`
      : `${folding.label}（请补充成品或展开尺寸）`;
  } else if (kind === "booklet") {
    const parts = [];
    if (targetSizeMm) parts.push(`成品 ${fmtSizePair(targetSizeMm)}`);
    if (spine?.available) parts.push(`参考书脊 ≈ ${fmtMm(spine.spineMm)} mm`);
    parts.push(`出血 ${fmtMm(bleedMm)} mm`);
    subtitle = parts.join(" · ");
  } else {
    subtitle = `${targetLabel} ${fmtSizePair(targetSizeMm)} · 四边出血 ${fmtMm(bleedMm)} mm · 安全边 ${fmtMm(safeMarginMm)} mm`;
  }

  const disclaimer = spine?.available ? `${SPINE_DISCLAIMER}。${BLUEPRINT_DISCLAIMER}` : BLUEPRINT_DISCLAIMER;

  return {
    kind,
    productType,
    title,
    subtitle,
    badges,
    targetSizeMm,
    finishedSizeMm: finishedMm,
    expandedSizeMm,
    flatSizeMm,
    boxSizeMm: package3dMm,
    boxStructure,
    packageSizeBasis,
    bleedMm,
    safeMarginMm,
    roundedCorners,
    shape,
    hangHole,
    folding,
    spine,
    coverSpreadMm,
    cup,
    pdfOverlay,
    notes,
    disclaimer,
  };
}

// ============================================================================
// DOM / SVG 渲染引擎（仅在浏览器端调用 renderBlueprintPanel 时触达 document）
// ============================================================================

function svgEl(tag, attrs = {}, text = null) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v != null) node.setAttribute(k, String(v));
  }
  if (text != null) node.textContent = String(text);
  return node;
}

function htmlEl(tag, className = "", text = null) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

function getLayerState(container) {
  let state = layerStateMap.get(container);
  if (!state) {
    state = {
      bleed: container.dataset.bpBleed !== "0",
      safe: container.dataset.bpSafe !== "0",
      dims: container.dataset.bpDims !== "0",
      pdf: container.dataset.bpPdf !== "0",
    };
    layerStateMap.set(container, state);
  }
  return state;
}

function persistLayerState(container, state) {
  layerStateMap.set(container, state);
  container.dataset.bpBleed = state.bleed ? "1" : "0";
  container.dataset.bpSafe = state.safe ? "1" : "0";
  container.dataset.bpDims = state.dims ? "1" : "0";
  container.dataset.bpPdf = state.pdf ? "1" : "0";
}

function createSvgDefs(uid) {
  const defs = svgEl("defs");

  // 工业蓝图微网格底纹
  const pattern = svgEl("pattern", {
    id: `${uid}-grid`,
    width: "16",
    height: "16",
    patternUnits: "userSpaceOnUse",
  });
  pattern.append(
    svgEl("path", {
      d: "M 16 0 L 0 0 0 16",
      fill: "none",
      class: "bp-grid-line",
      "stroke-width": "0.6",
    })
  );
  defs.append(pattern);

  // 双向工程箭头（多颜色共用独立 marker）
  const addArrow = (suffix, cls) => {
    const marker = svgEl("marker", {
      id: `${uid}-arr-${suffix}`,
      viewBox: "0 0 8 8",
      refX: "7",
      refY: "4",
      markerWidth: "6",
      markerHeight: "6",
      orient: "auto-start-reverse",
    });
    marker.append(svgEl("path", { d: "M 0 1 L 7 4 L 0 7 z", class: cls }));
    defs.append(marker);
  };
  addArrow("dim", "bp-arrow-dim");
  addArrow("red", "bp-arrow-red");
  addArrow("green", "bp-arrow-green");
  addArrow("info", "bp-arrow-info");

  return defs;
}

function drawDimLine(group, uid, x1, y1, x2, y2, label, opts = {}) {
  const suffix = opts.arrow || "dim";
  const lineClass = opts.lineClass || "bp-dim-line";
  const textClass = opts.textClass || "bp-dim-text";
  const offset = opts.extOffset || 0;

  // 可选延伸指示线
  if (opts.extFrom && opts.extTo) {
    group.append(
      svgEl("line", { x1: opts.extFrom[0], y1: opts.extFrom[1], x2: x1, y2: y1, class: "bp-ext-line" }),
      svgEl("line", { x1: opts.extTo[0], y1: opts.extTo[1], x2: x2, y2: y2, class: "bp-ext-line" })
    );
  }

  group.append(
    svgEl("line", {
      x1,
      y1,
      x2,
      y2,
      class: lineClass,
      "marker-start": `url(#${uid}-arr-${suffix})`,
      "marker-end": `url(#${uid}-arr-${suffix})`,
    })
  );

  const mx = (x1 + x2) / 2 + (opts.dx || 0);
  const my = (y1 + y2) / 2 + (opts.dy || 0) + offset;
  const approxW = Math.max(38, String(label).length * 6.4 + 10);

  // 小胶囊底衬，避免标注文字压线难读
  group.append(
    svgEl("rect", {
      x: round1(mx - approxW / 2),
      y: round1(my - 8.5),
      width: round1(approxW),
      height: "16",
      rx: "3",
      class: "bp-label-halo",
    }),
    svgEl(
      "text",
      {
        x: round1(mx),
        y: round1(my + 3.5),
        "text-anchor": "middle",
        class: textClass,
      },
      label
    )
  );
}

function drawCropMarks(group, x, y, w, h, gap = 4, len = 8) {
  const segs = [
    [x - gap - len, y, x - gap, y],
    [x, y - gap - len, x, y - gap],
    [x + w + gap, y, x + w + gap + len, y],
    [x + w, y - gap - len, x + w, y - gap],
    [x - gap - len, y + h, x - gap, y + h],
    [x, y + h + gap, x, y + h + gap + len],
    [x + w + gap, y + h, x + w + gap + len, y + h],
    [x + w, y + h + gap, x + w, y + h + gap + len],
  ];
  for (const [x1, y1, x2, y2] of segs) {
    group.append(svgEl("line", { x1: round1(x1), y1: round1(y1), x2: round1(x2), y2: round1(y2), class: "bp-crop-mark" }));
  }
}

function fitRectInBox(rawW, rawH, boxX, boxY, boxW, boxH) {
  const safeW = Math.max(1, rawW);
  const safeH = Math.max(1, rawH);
  const rawRatio = safeW / safeH;
  const clampedRatio = Math.max(0.32, Math.min(3.1, rawRatio));
  const clamped = Math.abs(rawRatio - clampedRatio) > 0.05;

  let drawW;
  let drawH;
  if (clampedRatio >= boxW / boxH) {
    drawW = boxW;
    drawH = boxW / clampedRatio;
  } else {
    drawH = boxH;
    drawW = boxH * clampedRatio;
  }
  const x = round1(boxX + (boxW - drawW) / 2);
  const y = round1(boxY + (boxH - drawH) / 2);
  return {
    x,
    y,
    w: round1(drawW),
    h: round1(drawH),
    pxPerMmX: drawW / safeW,
    pxPerMmY: drawH / safeH,
    clamped,
  };
}

function drawPdfOverlayLayer(svg, model, trimBox) {
  const overlay = model.pdfOverlay;
  if (!overlay || !trimBox) return null;

  const gPdf = svgEl("g", { "data-layer": "pdf", class: "bp-layer-pdf" });
  const isMatch = overlay.status === "match";
  const isMismatch = overlay.status === "mismatch";
  const rectClass = isMatch ? "bp-pdf-box is-match" : isMismatch ? "bp-pdf-box is-mismatch" : "bp-pdf-box is-info";

  const target = model.targetSizeMm;
  const aligned = overlay.alignedSizeMm;
  if (Array.isArray(target) && Array.isArray(aligned)) {
    const scaleW = Math.max(0.35, Math.min(1.45, aligned[0] / target[0]));
    const scaleH = Math.max(0.35, Math.min(1.45, aligned[1] / target[1]));
    const pw = round1(trimBox.w * scaleW);
    const ph = round1(trimBox.h * scaleH);
    const px = round1(trimBox.x + (trimBox.w - pw) / 2);
    const py = round1(trimBox.y + (trimBox.h - ph) / 2);

    if (overlay.bleedBoxSizeMm) {
      const bScaleW = Math.max(0.35, Math.min(1.55, overlay.bleedBoxSizeMm[0] / target[0]));
      const bScaleH = Math.max(0.35, Math.min(1.55, overlay.bleedBoxSizeMm[1] / target[1]));
      const bw = round1(trimBox.w * bScaleW);
      const bh = round1(trimBox.h * bScaleH);
      gPdf.append(
        svgEl("rect", {
          x: round1(trimBox.x + (trimBox.w - bw) / 2),
          y: round1(trimBox.y + (trimBox.h - bh) / 2),
          width: bw,
          height: bh,
          rx: "2",
          class: "bp-pdf-bleed",
        })
      );
    }

    gPdf.append(
      svgEl("rect", {
        x: px,
        y: py,
        width: pw,
        height: ph,
        rx: "2",
        class: rectClass,
      })
    );

    const badgeText = isMatch
      ? `PDF ${fmtSizePair(overlay.observedSizeMm)} ✓ 一致`
      : `PDF ${fmtSizePair(overlay.observedSizeMm)} ⚠ 偏差`;
    const badgeW = Math.max(122, badgeText.length * 6.5 + 14);
    const bx = round1(Math.min(448 - badgeW, Math.max(12, px + pw - badgeW)));
    const by = round1(Math.max(10, py - 22));
    gPdf.append(
      svgEl("rect", {
        x: bx,
        y: by,
        width: round1(badgeW),
        height: "18",
        rx: "9",
        class: isMatch ? "bp-pdf-tag is-match" : "bp-pdf-tag is-mismatch",
      }),
      svgEl(
        "text",
        {
          x: round1(bx + badgeW / 2),
          y: round1(by + 12.5),
          "text-anchor": "middle",
          class: "bp-pdf-tag-text",
        },
        badgeText
      )
    );
  }
  svg.append(gPdf);
  return gPdf;
}

function renderFlatOrFoldedSvg(svg, uid, model, vbW, vbH) {
  const isFolded = model.kind === "folded";
  const isBooklet = model.kind === "booklet";
  const dims = model.targetSizeMm || (isFolded ? [297, 210] : [210, 285]);
  const isPlaceholder = !model.targetSizeMm;

  // 画册右侧留出书脊剖面区
  const boxX = isBooklet ? 44 : 68;
  const boxY = 44;
  const boxW = isBooklet ? 228 : vbW - 136;
  const boxH = vbH - 96;

  const fit = fitRectInBox(dims[0], dims[1], boxX, boxY, boxW, boxH);
  const bleedPx = model.bleedMm > 0 ? Math.max(7, Math.min(14, round1(model.bleedMm * Math.min(fit.pxPerMmX, fit.pxPerMmY) * 1.4))) : 0;
  const safePx = Math.max(7, Math.min(16, round1(model.safeMarginMm * Math.min(fit.pxPerMmX, fit.pxPerMmY) * 1.4)));
  const cornerRx = model.roundedCorners?.enabled
    ? Math.max(4, Math.min(18, round1(model.roundedCorners.radiusMm * Math.min(fit.pxPerMmX, fit.pxPerMmY))))
    : 0;

  // 图层 1：出血框（红虚线 + 淡红出血环）
  const gBleed = svgEl("g", { "data-layer": "bleed", class: "bp-layer-bleed" });
  if (bleedPx > 0) {
    if (model.shape === "circle") {
      const r = round1(Math.min(fit.w, fit.h) / 2);
      const cx = round1(fit.x + fit.w / 2);
      const cy = round1(fit.y + fit.h / 2);
      gBleed.append(
        svgEl("circle", { cx, cy, r: r + bleedPx, class: "bp-bleed-fill" }),
        svgEl("circle", { cx, cy, r: r + bleedPx, class: "bp-bleed-stroke" })
      );
    } else {
      gBleed.append(
        svgEl("rect", {
          x: fit.x - bleedPx,
          y: fit.y - bleedPx,
          width: fit.w + bleedPx * 2,
          height: fit.h + bleedPx * 2,
          rx: cornerRx ? cornerRx + 3 : 0,
          class: "bp-bleed-fill",
        }),
        svgEl("rect", {
          x: fit.x - bleedPx,
          y: fit.y - bleedPx,
          width: fit.w + bleedPx * 2,
          height: fit.h + bleedPx * 2,
          rx: cornerRx ? cornerRx + 3 : 0,
          class: "bp-bleed-stroke",
        })
      );
    }
    gBleed.append(
      svgEl(
        "text",
        {
          x: round1(fit.x - bleedPx + 4),
          y: round1(fit.y - bleedPx - 4),
          class: "bp-tag-red",
        },
        `出血 +${fmtMm(model.bleedMm)}mm`
      )
    );
  }
  svg.append(gBleed);

  // 主体成品裁切框（恒显）
  const gTrim = svgEl("g", { class: "bp-layer-trim" });
  if (model.shape === "circle") {
    const r = round1(Math.min(fit.w, fit.h) / 2);
    const cx = round1(fit.x + fit.w / 2);
    const cy = round1(fit.y + fit.h / 2);
    gTrim.append(
      svgEl("circle", { cx, cy, r, class: "bp-trim-fill" }),
      svgEl("circle", { cx, cy, r, class: "bp-trim-stroke" })
    );
  } else {
    gTrim.append(
      svgEl("rect", {
        x: fit.x,
        y: fit.y,
        width: fit.w,
        height: fit.h,
        rx: cornerRx,
        class: "bp-trim-fill",
      }),
      svgEl("rect", {
        x: fit.x,
        y: fit.y,
        width: fit.w,
        height: fit.h,
        rx: cornerRx,
        class: "bp-trim-stroke",
      })
    );
    drawCropMarks(gTrim, fit.x, fit.y, fit.w, fit.h, bleedPx + 3, 7);
  }

  // 吊牌顶部挂孔示意
  if (model.hangHole && model.shape !== "circle") {
    const hx = round1(fit.x + fit.w / 2);
    const hy = round1(fit.y + Math.max(12, Math.min(20, fit.h * 0.14)));
    gTrim.append(
      svgEl("circle", { cx: hx, cy: hy, r: "4.5", class: "bp-hole-mark" }),
      svgEl("text", { x: hx + 9, y: hy + 3, class: "bp-tag-muted" }, model.hangHole)
    );
  }

  // 画册左装订边标记
  if (isBooklet) {
    const bindW = Math.max(10, Math.min(18, round1(fit.w * 0.08)));
    gTrim.append(
      svgEl("rect", {
        x: fit.x,
        y: fit.y,
        width: bindW,
        height: fit.h,
        class: "bp-binding-band",
      })
    );
    const isSaddle = /骑马钉/.test(String(model.badges.map((b) => b.label).join(" ")));
    if (isSaddle) {
      // 两个骑马钉铁丝示意点
      for (const ratio of [0.28, 0.72]) {
        const sy = round1(fit.y + fit.h * ratio);
        gTrim.append(
          svgEl("line", {
            x1: fit.x + 3,
            y1: sy - 5,
            x2: fit.x + 3,
            y2: sy + 5,
            class: "bp-staple-mark",
          })
        );
      }
    }
    gTrim.append(
      svgEl(
        "text",
        {
          x: round1(fit.x + bindW + 6),
          y: round1(fit.y + fit.h - 10),
          class: "bp-tag-muted",
        },
        "← 装订边"
      )
    );
  }

  // 折页折痕虚线与 P1/P2/P3 分幅标签
  if (isFolded && model.folding) {
    const widths = model.folding.panelWidthsMm || Array.from({ length: model.folding.panels }, () => 100);
    const total = widths.reduce((a, b) => a + b, 0) || 1;
    let cursorX = fit.x;
    widths.forEach((pwMm, idx) => {
      const segW = (pwMm / total) * fit.w;
      const cx = round1(cursorX + segW / 2);
      const cy = round1(fit.y + fit.h / 2);
      const isNarrowInner = model.folding.type === "tri_roll" && idx === widths.length - 1;
      gTrim.append(
        svgEl("text", { x: cx, y: cy - 4, "text-anchor": "middle", class: "bp-panel-code" }, `P${idx + 1}`),
        svgEl(
          "text",
          { x: cx, y: cy + 12, "text-anchor": "middle", class: isNarrowInner ? "bp-panel-sub is-inner" : "bp-panel-sub" },
          isPlaceholder ? (isNarrowInner ? "内折略窄" : "等幅示意") : `${fmtMm(pwMm)}mm${isNarrowInner ? " 内折" : ""}`
        )
      );
      if (idx < widths.length - 1) {
        const lineX = round1(cursorX + segW);
        gTrim.append(
          svgEl("line", {
            x1: lineX,
            y1: fit.y - bleedPx - 2,
            x2: lineX,
            y2: fit.y + fit.h + bleedPx + 2,
            class: "bp-fold-crease",
          }),
          svgEl("text", { x: lineX, y: fit.y + fit.h + bleedPx + 14, "text-anchor": "middle", class: "bp-tag-info" }, `折痕${idx + 1}`)
        );
      }
      cursorX += segW;
    });
  } else if (isPlaceholder) {
    gTrim.append(
      svgEl(
        "text",
        { x: round1(fit.x + fit.w / 2), y: round1(fit.y + fit.h / 2 + 4), "text-anchor": "middle", class: "bp-tag-muted" },
        "成品尺寸待补充"
      )
    );
  }
  svg.append(gTrim);

  // 图层 2：安全边（绿点线）
  const gSafe = svgEl("g", { "data-layer": "safe", class: "bp-layer-safe" });
  if (fit.w > safePx * 2 + 8 && fit.h > safePx * 2 + 8) {
    if (model.shape === "circle") {
      const r = round1(Math.min(fit.w, fit.h) / 2 - safePx);
      const cx = round1(fit.x + fit.w / 2);
      const cy = round1(fit.y + fit.h / 2);
      gSafe.append(svgEl("circle", { cx, cy, r: Math.max(6, r), class: "bp-safe-stroke" }));
    } else {
      const insetLeft = isBooklet ? safePx + 8 : safePx;
      gSafe.append(
        svgEl("rect", {
          x: fit.x + insetLeft,
          y: fit.y + safePx,
          width: Math.max(8, fit.w - insetLeft - safePx),
          height: Math.max(8, fit.h - safePx * 2),
          rx: Math.max(0, cornerRx - 2),
          class: "bp-safe-stroke",
        })
      );
    }
    gSafe.append(
      svgEl(
        "text",
        {
          x: round1(fit.x + fit.w - safePx - 4),
          y: round1(fit.y + safePx + 12),
          "text-anchor": "end",
          class: "bp-tag-green",
        },
        `安全边 ${fmtMm(model.safeMarginMm)}mm`
      )
    );
  }
  svg.append(gSafe);

  // 图层 3：工程尺寸标注
  const gDims = svgEl("g", { "data-layer": "dims", class: "bp-layer-dims" });
  if (!isPlaceholder) {
    if (model.shape === "circle") {
      const r = round1(Math.min(fit.w, fit.h) / 2);
      const cx = round1(fit.x + fit.w / 2);
      const cy = round1(fit.y + fit.h / 2);
      drawDimLine(gDims, uid, cx - r, cy, cx + r, cy, `Ø ${fmtMm(dims[0])} mm`, { dy: -12 });
    } else {
      const topY = round1(fit.y - bleedPx - 18);
      drawDimLine(gDims, uid, fit.x, topY, fit.x + fit.w, topY, `${isFolded ? "展开宽 " : ""}${fmtMm(dims[0])} mm`, {
        extFrom: [fit.x, fit.y - bleedPx - 2],
        extTo: [fit.x + fit.w, fit.y - bleedPx - 2],
      });
      const sideX = isBooklet ? round1(fit.x - bleedPx - 18) : round1(fit.x + fit.w + bleedPx + 22);
      drawDimLine(gDims, uid, sideX, fit.y, sideX, fit.y + fit.h, `${fmtMm(dims[1])} mm`, {
        extFrom: [isBooklet ? fit.x - bleedPx - 2 : fit.x + fit.w + bleedPx + 2, fit.y],
        extTo: [isBooklet ? fit.x - bleedPx - 2 : fit.x + fit.w + bleedPx + 2, fit.y + fit.h],
      });
    }
    if (model.roundedCorners?.enabled) {
      gDims.append(
        svgEl("text", { x: round1(fit.x + fit.w - 4), y: round1(fit.y + fit.h + bleedPx + 14), "text-anchor": "end", class: "bp-tag-info" }, model.roundedCorners.label)
      );
    }
    if (fit.clamped) {
      gDims.append(
        svgEl("text", { x: round1(fit.x + fit.w / 2), y: vbH - 8, "text-anchor": "middle", class: "bp-tag-muted" }, "（宽幅比例已压缩示意）")
      );
    }
  }
  svg.append(gDims);

  // 画册右侧：独立比例放大书脊剖面条
  if (isBooklet) {
    const spX = 312;
    const spY = 34;
    const spW = 134;
    const spH = vbH - 60;
    const gSpine = svgEl("g", { class: "bp-layer-spine" });
    gSpine.append(
      svgEl("rect", { x: spX, y: spY, width: spW, height: spH, rx: "8", class: "bp-spine-card" }),
      svgEl("text", { x: spX + spW / 2, y: spY + 18, "text-anchor": "middle", class: "bp-spine-title" }, "书脊剖面放大示意")
    );

    const spine = model.spine;
    const barCenterX = spX + spW / 2;
    const barTop = spY + 34;
    const barH = Math.max(76, spH - 92);
    // 书脊放大视觉宽度（按厚度在 12~44px 之间非线性映射，确保清晰可见）
    const spineVisualW = spine?.available
      ? Math.max(12, Math.min(44, round1(10 + Math.sqrt(spine.spineMm) * 9)))
      : 16;
    const bx = round1(barCenterX - spineVisualW / 2);

    // 封面外包边 + 内页层叠细线
    gSpine.append(
      svgEl("rect", { x: bx, y: barTop, width: spineVisualW, height: barH, rx: "2", class: "bp-spine-bar" })
    );
    const leafCount = Math.max(3, Math.min(9, Math.floor(spineVisualW / 4)));
    for (let i = 1; i < leafCount; i++) {
      const lx = round1(bx + (spineVisualW * i) / leafCount);
      gSpine.append(svgEl("line", { x1: lx, y1: barTop + 3, x2: lx, y2: barTop + barH - 3, class: "bp-spine-leaf" }));
    }

    const labelY = barTop + barH + 18;
    if (spine?.available) {
      drawDimLine(gSpine, uid, bx, labelY, bx + spineVisualW, labelY, `≈ ${fmtMm(spine.spineMm)} mm`, {
        arrow: spine.warning ? "red" : "dim",
        dy: 14,
      });
      gSpine.append(
        svgEl(
          "text",
          { x: barCenterX, y: labelY + 34, "text-anchor": "middle", class: "bp-tag-muted" },
          `${spine.pages}P · ${spine.gsm}g ${spine.kindLabel}`
        )
      );
      if (spine.warning) {
        gSpine.append(
          svgEl("rect", { x: spX + 10, y: spY + spH - 22, width: spW - 20, height: "16", rx: "8", class: "bp-warn-pill" }),
          svgEl("text", { x: barCenterX, y: spY + spH - 11, "text-anchor": "middle", class: "bp-warn-pill-text" }, "⚠ 骑马钉厚度预警")
        );
      }
    } else {
      gSpine.append(
        svgEl("text", { x: barCenterX, y: labelY + 8, "text-anchor": "middle", class: "bp-tag-muted" }, "待填页数与纸张克重")
      );
    }
    svg.append(gSpine);
  }

  // 图层 4：PDF 叠层
  drawPdfOverlayLayer(svg, model, isPlaceholder ? null : fit);
}

function renderBoxSvg(svg, uid, model, vbW, vbH) {
  const hasFlat = Boolean(model.flatSizeMm);
  const raw3d = model.boxSizeMm || [120, 80, 60];
  const isPlaceholder = !model.boxSizeMm;

  // 等轴测 3D 比例映射（钳制极端长宽比，防止压扁成一条线）
  const maxAxis = Math.max(raw3d[0], raw3d[1], raw3d[2], 1);
  const norm = raw3d.map((v) => Math.max(0.28, Math.min(1, v / maxAxis)));
  const unit = hasFlat ? 76 : 96;
  const len = norm[0] * unit;
  const wid = norm[1] * unit;
  const hei = norm[2] * unit;

  const cos30 = 0.866;
  const sin30 = 0.5;
  // 等轴测坐标系：L 向右下方延展，W 向左下方延展，H 向上延展
  const centerX = hasFlat ? 148 : vbW / 2;
  const baseY = vbH - 36;

  // 底面 4 点（p0 前下角，pL 右下角，pW 左下角，pB 后下角）
  const p0 = [centerX, baseY];
  const pL = [p0[0] + len * cos30, p0[1] - len * sin30];
  const pW = [p0[0] - wid * cos30, p0[1] - wid * sin30];
  const pB = [pW[0] + len * cos30, pW[1] - len * sin30];

  // 顶面 4 点
  const t0 = [p0[0], p0[1] - hei];
  const tL = [pL[0], pL[1] - hei];
  const tW = [pW[0], pW[1] - hei];
  const tB = [pB[0], pB[1] - hei];

  // 垂直居中微调
  const minY = tB[1];
  const maxY = p0[1];
  const desiredCenterY = vbH / 2 + 12;
  const shiftY = desiredCenterY - (minY + maxY) / 2;
  const pt = (p) => [round1(p[0]), round1(p[1] + shiftY)];
  const P0 = pt(p0), PL = pt(pL), PW = pt(pW), PB = pt(pB);
  const T0 = pt(t0), TL = pt(tL), TW = pt(tW), TB = pt(tB);

  const polyStr = (pts) => pts.map((p) => `${p[0]},${p[1]}`).join(" ");

  const gBox = svgEl("g", { class: "bp-layer-box" });
  // 三个可见面（顶面最亮、左面中调、右面稍深，还原暖纸立体感）
  gBox.append(
    svgEl("polygon", { points: polyStr([T0, TL, TB, TW]), class: "bp-box-face-top" }),
    svgEl("polygon", { points: polyStr([P0, PW, TW, T0]), class: "bp-box-face-left" }),
    svgEl("polygon", { points: polyStr([P0, PL, TL, T0]), class: "bp-box-face-right" })
  );

  // 内部隐藏棱线（虚线）
  for (const [a, b] of [[PB, PW], [PB, PL], [PB, TB]]) {
    gBox.append(svgEl("line", { x1: a[0], y1: a[1], x2: b[0], y2: b[1], class: "bp-box-hidden" }));
  }

  // 外轮廓实线
  gBox.append(
    svgEl("polygon", { points: polyStr([T0, TL, TB, TW]), class: "bp-box-edge" }),
    svgEl("polyline", { points: polyStr([PW, P0, PL]), class: "bp-box-edge" }),
    svgEl("line", { x1: P0[0], y1: P0[1], x2: T0[0], y2: T0[1], class: "bp-box-edge" }),
    svgEl("line", { x1: PW[0], y1: PW[1], x2: TW[0], y2: TW[1], class: "bp-box-edge" }),
    svgEl("line", { x1: PL[0], y1: PL[1], x2: TL[0], y2: TL[1], class: "bp-box-edge" })
  );

  // 顶部开盖 / 抽拉方向指示箭头
  const isDrawer = /抽屉/.test(model.boxStructure || "");
  const topCx = round1((T0[0] + TB[0]) / 2);
  const topCy = round1((T0[1] + TB[1]) / 2);
  if (isDrawer) {
    const sx = round1((P0[0] + PL[0]) / 2);
    const sy = round1((P0[1] + TL[1]) / 2);
    gBox.append(
      svgEl("line", { x1: sx - 8, y1: sy + 4, x2: sx + 20, y2: sy - 10, class: "bp-open-arrow", "marker-end": `url(#${uid}-arr-info)` }),
      svgEl("text", { x: sx + 24, y: sy - 8, class: "bp-tag-info" }, "抽拉方向")
    );
  } else {
    gBox.append(
      svgEl("line", { x1: topCx, y1: topCy + 2, x2: topCx, y2: topCy - 26, class: "bp-open-arrow", "marker-end": `url(#${uid}-arr-info)` }),
      svgEl("text", { x: topCx + 8, y: topCy - 18, class: "bp-tag-info" }, model.boxStructure ? `${model.boxStructure} · 开盖` : "开盖方向")
    );
  }
  svg.append(gBox);

  // 尺寸标注图层（红 L、绿 W、蓝 H）
  const gDims = svgEl("g", { "data-layer": "dims", class: "bp-layer-dims" });
  const lblL = isPlaceholder ? "长 L 待填" : `L ${fmtMm(raw3d[0])}mm`;
  const lblW = isPlaceholder ? "宽 W 待填" : `W ${fmtMm(raw3d[1])}mm`;
  const lblH = isPlaceholder ? "高 H 待填" : `H ${fmtMm(raw3d[2])}mm`;

  drawDimLine(gDims, uid, P0[0] + 6, P0[1] + 14, PL[0] + 6, PL[1] + 14, lblL, {
    arrow: "red",
    lineClass: "bp-dim-line-red",
    textClass: "bp-dim-text-red",
    dy: 10,
  });
  drawDimLine(gDims, uid, PW[0] - 6, PW[1] + 14, P0[0] - 6, P0[1] + 14, lblW, {
    arrow: "green",
    lineClass: "bp-dim-line-green",
    textClass: "bp-dim-text-green",
    dy: 10,
  });
  drawDimLine(gDims, uid, PL[0] + 18, PL[1], TL[0] + 18, TL[1], lblH, {
    arrow: "info",
    lineClass: "bp-dim-line-info",
    textClass: "bp-dim-text-info",
    dx: 16,
  });
  svg.append(gDims);

  // 若同时提供了刀模/展开二维尺寸，在右侧附带 2D 刀模展开缩略盒并支持 PDF 叠层比对！
  if (hasFlat) {
    const flat = model.flatSizeMm;
    const panelX = 296;
    const panelY = 30;
    const panelW = 152;
    const panelH = vbH - 54;
    const gFlat = svgEl("g", { class: "bp-layer-flat-inset" });
    gFlat.append(
      svgEl("rect", { x: panelX, y: panelY, width: panelW, height: panelH, rx: "8", class: "bp-spine-card" }),
      svgEl("text", { x: panelX + panelW / 2, y: panelY + 18, "text-anchor": "middle", class: "bp-spine-title" }, "展开 / 刀模二维尺寸")
    );
    const fit = fitRectInBox(flat[0], flat[1], panelX + 22, panelY + 34, panelW - 44, panelH - 72);

    const gBleed = svgEl("g", { "data-layer": "bleed", class: "bp-layer-bleed" });
    gBleed.append(
      svgEl("rect", {
        x: fit.x - 5,
        y: fit.y - 5,
        width: fit.w + 10,
        height: fit.h + 10,
        class: "bp-bleed-stroke",
      })
    );
    gFlat.append(gBleed);

    gFlat.append(
      svgEl("rect", { x: fit.x, y: fit.y, width: fit.w, height: fit.h, class: "bp-trim-fill" }),
      svgEl("rect", { x: fit.x, y: fit.y, width: fit.w, height: fit.h, class: "bp-trim-stroke" })
    );

    const gSafe = svgEl("g", { "data-layer": "safe", class: "bp-layer-safe" });
    if (fit.w > 18 && fit.h > 18) {
      gSafe.append(svgEl("rect", { x: fit.x + 6, y: fit.y + 6, width: fit.w - 12, height: fit.h - 12, class: "bp-safe-stroke" }));
    }
    gFlat.append(gSafe);

    gFlat.append(
      svgEl("text", { x: panelX + panelW / 2, y: panelY + panelH - 14, "text-anchor": "middle", class: "bp-dim-text" }, fmtSizePair(flat))
    );
    svg.append(gFlat);

    drawPdfOverlayLayer(svg, model, fit);
  }
}

function renderCupSvg(svg, uid, model, vbW, vbH) {
  const cup = model.cup || CUP_PRESETS[350];
  const cx = 160;
  const topY = 48;
  const cupH = Math.min(156, vbH - 86);
  const scale = cupH / cup.heightMm;
  const topW = round1(cup.topDiaMm * scale);
  const botW = round1(cup.bottomDiaMm * scale);
  const botY = round1(topY + cupH);

  const gCup = svgEl("g", { class: "bp-layer-cup" });
  // 杯身梯形轮廓
  const bodyPts = `${cx - topW / 2},${topY} ${cx + topW / 2},${topY} ${cx + botW / 2},${botY} ${cx - botW / 2},${botY}`;
  gCup.append(
    svgEl("polygon", { points: bodyPts, class: "bp-trim-fill" }),
    svgEl("polygon", { points: bodyPts, class: "bp-trim-stroke" }),
    // 卷口椭圆
    svgEl("ellipse", { cx, cy: topY, rx: round1(topW / 2 + 3), ry: "6.5", class: "bp-cup-rim" }),
    // 杯底弧线
    svgEl("ellipse", { cx, cy: botY, rx: round1(botW / 2), ry: "4", class: "bp-trim-stroke" })
  );
  svg.append(gCup);

  // 可印安全区图层（联动安全边开关）
  const gSafe = svgEl("g", { "data-layer": "safe", class: "bp-layer-safe" });
  const padTop = round1(cupH * 0.16);
  const padBot = round1(cupH * 0.12);
  const safeTopY = topY + padTop;
  const safeBotY = botY - padBot;
  const lerpW = (y) => topW + ((botW - topW) * (y - topY)) / cupH;
  const swTop = lerpW(safeTopY) - 8;
  const swBot = lerpW(safeBotY) - 8;
  const safePts = `${round1(cx - swTop / 2)},${safeTopY} ${round1(cx + swTop / 2)},${safeTopY} ${round1(cx + swBot / 2)},${safeBotY} ${round1(cx - swBot / 2)},${safeBotY}`;
  gSafe.append(
    svgEl("polygon", { points: safePts, class: "bp-cup-safe-fill" }),
    svgEl("polygon", { points: safePts, class: "bp-safe-stroke" }),
    svgEl("text", { x: cx, y: round1((safeTopY + safeBotY) / 2 + 4), "text-anchor": "middle", class: "bp-tag-green" }, "可印安全区")
  );
  svg.append(gSafe);

  // 尺寸标注图层
  const gDims = svgEl("g", { "data-layer": "dims", class: "bp-layer-dims" });
  drawDimLine(gDims, uid, cx - topW / 2, topY - 20, cx + topW / 2, topY - 20, `口径 ≈${cup.topDiaMm}mm`, {
    extFrom: [cx - topW / 2, topY - 6],
    extTo: [cx + topW / 2, topY - 6],
  });
  drawDimLine(gDims, uid, cx - topW / 2 - 26, topY, cx - topW / 2 - 26, botY, `高 ≈${cup.heightMm}mm`, {
    extFrom: [cx - topW / 2 - 4, topY],
    extTo: [cx - botW / 2 - 4, botY],
  });
  svg.append(gDims);

  // 右侧：扇形展开示意卡片
  const cardX = 276;
  const cardY = 36;
  const cardW = 168;
  const cardH = vbH - 68;
  const gFan = svgEl("g", { class: "bp-layer-fan" });
  gFan.append(
    svgEl("rect", { x: cardX, y: cardY, width: cardW, height: cardH, rx: "8", class: "bp-spine-card" }),
    svgEl("text", { x: cardX + cardW / 2, y: cardY + 18, "text-anchor": "middle", class: "bp-spine-title" }, "扇形展开排版提醒")
  );
  const fx = cardX + cardW / 2;
  const fy = cardY + cardH + 20;
  const rOuter = Math.min(104, cardH + 6);
  const rInner = rOuter * 0.56;
  const a1 = (-122 * Math.PI) / 180;
  const a2 = (-58 * Math.PI) / 180;
  const p = (r, a) => [round1(fx + r * Math.cos(a)), round1(fy + r * Math.sin(a))];
  const [ox1, oy1] = p(rOuter, a1);
  const [ox2, oy2] = p(rOuter, a2);
  const [ix2, iy2] = p(rInner, a2);
  const [ix1, iy1] = p(rInner, a1);
  const fanPath = `M ${ox1} ${oy1} A ${rOuter} ${rOuter} 0 0 1 ${ox2} ${oy2} L ${ix2} ${iy2} A ${rInner} ${rInner} 0 0 0 ${ix1} ${iy1} Z`;
  gFan.append(
    svgEl("path", { d: fanPath, class: "bp-cup-fan" }),
    svgEl("text", { x: fx, y: cardY + cardH - 34, "text-anchor": "middle", class: "bp-dim-text" }, cup.volumeLabel),
    svgEl("text", { x: fx, y: cardY + cardH - 16, "text-anchor": "middle", class: "bp-tag-muted" }, "卷口下 12mm / 底上 8mm 避让")
  );
  svg.append(gFan);
}

function applyLayerVisibility(svg, state) {
  for (const key of ["bleed", "safe", "dims", "pdf"]) {
    const visible = Boolean(state[key]);
    for (const group of svg.querySelectorAll(`[data-layer="${key}"]`)) {
      group.classList.toggle("is-hidden", !visible);
    }
  }
}

/**
 * 在指定容器内渲染交互式“规格与印前示意图”面板。
 * options: { compact?: boolean }
 */
export function renderBlueprintPanel(container, order, pdfInspection = null, options = {}) {
  const model = buildBlueprintModel(order, pdfInspection);
  if (!container) return model;

  if (model.kind === "empty") {
    container.hidden = true;
    container.replaceChildren();
    return model;
  }

  container.hidden = false;
  container.classList.add("blueprint-panel");
  container.classList.toggle("blueprint-panel--compact", Boolean(options.compact));

  const state = getLayerState(container);
  const uid = `bp-${++svgUidCounter}`;

  // 顶栏：标题 + 胶囊徽章
  const header = htmlEl("div", "blueprint-header");
  const titleWrap = htmlEl("div", "blueprint-title-wrap");
  titleWrap.append(htmlEl("h3", "blueprint-title", model.title));
  if (model.subtitle) titleWrap.append(htmlEl("p", "blueprint-subtitle", model.subtitle));

  const badgeRow = htmlEl("div", "blueprint-badges");
  for (const badge of model.badges) {
    badgeRow.append(htmlEl("span", `blueprint-badge is-${badge.tone || "neutral"}`, badge.label));
  }
  header.append(titleWrap, badgeRow);

  // 图层开关栏（纯本地 UI 状态）
  const controls = htmlEl("div", "blueprint-controls");
  controls.setAttribute("role", "group");
  controls.setAttribute("aria-label", "蓝图参考图层开关");

  const has2dLayers = model.kind === "flat" || model.kind === "folded" || model.kind === "booklet" || (model.kind === "box" && Boolean(model.flatSizeMm));
  const toggleSpecs = [];
  if (has2dLayers) {
    toggleSpecs.push({ key: "bleed", label: "出血线", dotClass: "dot-bleed" });
    toggleSpecs.push({ key: "safe", label: "安全边", dotClass: "dot-safe" });
  } else if (model.kind === "cup") {
    toggleSpecs.push({ key: "safe", label: "可印安全区", dotClass: "dot-safe" });
  }
  toggleSpecs.push({ key: "dims", label: "尺寸标注", dotClass: "dot-dims" });
  if (model.pdfOverlay) {
    toggleSpecs.push({
      key: "pdf",
      label: "PDF 叠层",
      dotClass: model.pdfOverlay.status === "mismatch" ? "dot-pdf-warn" : "dot-pdf",
    });
  }

  // 构建 SVG 画布
  const vbW = 460;
  const vbH = options.compact ? 236 : 272;
  const stage = htmlEl("div", "blueprint-stage");
  const svg = svgEl("svg", {
    class: "blueprint-svg",
    viewBox: `0 0 ${vbW} ${vbH}`,
    role: "img",
    "aria-label": `${model.title}：${model.subtitle || "印前规格示意图"}`,
  });
  svg.append(
    createSvgDefs(uid),
    svgEl("rect", { x: "0", y: "0", width: vbW, height: vbH, class: "bp-canvas-bg" }),
    svgEl("rect", { x: "0", y: "0", width: vbW, height: vbH, fill: `url(#${uid}-grid)` })
  );

  if (model.kind === "box") {
    renderBoxSvg(svg, uid, model, vbW, vbH);
  } else if (model.kind === "cup") {
    renderCupSvg(svg, uid, model, vbW, vbH);
  } else {
    renderFlatOrFoldedSvg(svg, uid, model, vbW, vbH);
  }

  applyLayerVisibility(svg, state);
  stage.append(svg);

  for (const spec of toggleSpecs) {
    const btn = htmlEl("button", `blueprint-layer-btn${state[spec.key] ? " is-active" : ""}`);
    btn.type = "button";
    btn.dataset.layerToggle = spec.key;
    btn.setAttribute("aria-pressed", state[spec.key] ? "true" : "false");
    const dot = htmlEl("span", `blueprint-layer-dot ${spec.dotClass}`);
    dot.setAttribute("aria-hidden", "true");
    btn.append(dot, document.createTextNode(spec.label));
    btn.addEventListener("click", () => {
      state[spec.key] = !state[spec.key];
      persistLayerState(container, state);
      btn.classList.toggle("is-active", state[spec.key]);
      btn.setAttribute("aria-pressed", state[spec.key] ? "true" : "false");
      applyLayerVisibility(svg, state);
    });
    controls.append(btn);
  }

  const fragment = document.createDocumentFragment();
  fragment.append(header, controls, stage);

  // PDF 叠层比对状态条
  if (model.pdfOverlay) {
    const pdfBar = htmlEl("div", `blueprint-pdf-bar is-${model.pdfOverlay.status}`);
    const tag = htmlEl(
      "strong",
      "blueprint-pdf-status",
      model.pdfOverlay.status === "match" ? "PDF 叠层核对一致" : model.pdfOverlay.status === "mismatch" ? "PDF 尺寸存在偏差" : "PDF 页面线索"
    );
    const detail = htmlEl("span", "blueprint-pdf-summary", model.pdfOverlay.summary);
    pdfBar.append(tag, detail);
    fragment.append(pdfBar);
  }

  // 底部印前要点列表
  if (model.notes.length) {
    const list = htmlEl("ul", "blueprint-notes");
    for (const note of model.notes.slice(0, options.compact ? 3 : 4)) {
      list.append(htmlEl("li", "blueprint-note-item", note));
    }
    fragment.append(list);
  }

  // 诚实边界声明
  fragment.append(htmlEl("p", "blueprint-disclaimer", model.disclaimer));

  container.replaceChildren(fragment);
  return model;
}
