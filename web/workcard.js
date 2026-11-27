/* PrintOps 工单小卡：把已生成的交接单渲染成一张可分享的 PNG 图像版。
   内容与印刷订单交接单一一对应：全部生产字段（含尺寸明细与品类参数，待确认
   值以“待确认/待补充”原样呈现）、工艺方案、印刷文件与检查范围、文件基础检查
   表、文件复核提醒、交接说明，外加确认时间/版本与页脚。零依赖 Canvas 手绘
   （项目不引第三方渲染库），预览即导出——对话框里展示的就是最终下载/复制的
   同一张画布。颜色一律运行时读取 styles.css 设计令牌，令牌调整（含文件末尾
   二次覆盖与高对比主题）无需同步本文件。 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.PrintOpsWorkCard = api;
})(typeof globalThis !== "undefined" ? globalThis : null, function () {
  "use strict";

  const CARD_WIDTH = 720;
  const EXPORT_SCALE = 2;
  const PAD = 30;
  const CONTENT_W = CARD_WIDTH - PAD * 2;
  // 状态措辞与交接单 markdown 同源（order_contract.handoff_document）。
  const STATUS_LABELS = {blocked: "需求整理中", stale: "已失效", draft: "未确认草稿", confirmed: "已本地确认", unconfirmed: "未确认"};
  const STATUS_COLORS = {
    confirmed: {bg: "--green-pale", text: "--green", line: "--line-green"},
    draft: {bg: "--gold-pale", text: "--warning", line: "--line-gold"},
    unconfirmed: {bg: "--gold-pale", text: "--warning", line: "--line-gold"},
    stale: {bg: "--red-pale", text: "--red-deep", line: "--line-red"},
    blocked: {bg: "--red-pale", text: "--red-deep", line: "--line-red"}
  };
  const FIELD_STATUS_LABELS = {missing: "待补充", pending_confirmation: "候选待核对", confirmed: "已核对", filled: "已填写", observed: "已填写", inferred: "推断待核对", stale: "已失效"};
  const FIELD_ALERT_STATUSES = new Set(["待补充", "候选待核对", "推断待核对", "已失效"]);
  const FILE_STATUS_LABELS = {checked: "已完成基础检查", pending_delivery: "文件待交付", stale: "检查已过期", needs_review: "需核对"};
  const CHECK_STATUS_LABELS = {ok: "通过", warn: "需关注", error: "未通过", info: "信息", unknown: "未确认"};
  const SERIF = 'Georgia, "Songti SC", "SimSun", serif';
  const SANS = '"Avenir Next", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif';
  const MONO = 'ui-monospace, Consolas, "Courier New", monospace';
  // getComputedStyle 拿不到令牌时按亮色主题兜底，避免整卡画成默认黑色。
  const TOKEN_FALLBACKS = {
    "--paper": "#eee5d5", "--paper-light": "#fffaf0", "--paper-raise": "#fbf4e6", "--paper-deep": "#e3d6c0",
    "--bg-raised": "rgba(249, 242, 226, .5)",
    "--ink": "#292722", "--ink-soft": "#514b43", "--quiet": "#62594d", "--ink-faint": "#a39682",
    "--rule": "#c8b89c", "--rule-light": "#dfd3bd", "--line-warm": "#c9b99e",
    "--red": "#a34a3e", "--red-deep": "#78332d", "--red-pale": "#f1ddd5",
    "--brass": "#96713e", "--gold-pale": "#f2e7cf", "--line-gold": "#c5a46d",
    "--green": "#466b5d", "--green-pale": "#e2eadf", "--line-green": "#aabfae",
    "--info": "#4b6575", "--warning": "#8a5a27", "--line-red": "#d8a28c"
  };

  // 与 blueprint.js 同源的轻量尺寸/纸厚/书脊解析（保持 workcard.js 零外部模块依赖独立运行）。
  const ISO_SIZES_MM = {
    A0: [841, 1189], A1: [594, 841], A2: [420, 594], A3: [297, 420],
    A4: [210, 297], A5: [148, 210], A6: [105, 148],
    B3: [353, 500], B4: [250, 353], B5: [176, 250], B6: [125, 176]
  };
  const UNIT_MM = {mm: 1, "毫米": 1, cm: 10, "厘米": 10, m: 1000, "米": 1000, in: 25.4, inch: 25.4, "英寸": 25.4};
  const round1 = (v) => Math.round(v * 10) / 10;
  const fmtMm = (v) => {
    const r = round1(v);
    return Number.isInteger(r) ? String(r) : r.toFixed(1);
  };
  const fmtDims = (arr) => `${arr.map(fmtMm).join("×")} mm`;

  function parseBlueprintSize(raw) {
    if (typeof raw !== "string") return null;
    const text = raw.normalize("NFKC").trim();
    if (!text || text.length > 256) return null;
    const cleaned = text
      .replace(/毫米/g, "mm").replace(/厘米/g, "cm").replace(/英寸/g, "in").replace(/米/g, "m").replace(/"/g, "in")
      .replace(/[（(][^）)]*[）)]/g, (g) => (/[x×✕✖*]/.test(g) ? ` ${g.slice(1, -1)} ` : " "));
    const stripped = cleaned
      .replace(/^(?:成品|展开|刀模|盒体|包装|内尺寸|外尺寸|规格|尺寸|约)[:：\s]*/gi, "")
      .replace(/^(?:(?:[AB]-?\d)|(?:大|正)\s*16\s*开)\s+(?=-?\d)/i, "")
      .replace(/[长宽高深厚][:：\s]*(?=-?\d)/g, "")
      .trim();
    const sep = /\s*(?:[xX×✕✖*]|\\\*)\s*/;
    if (sep.test(stripped)) {
      const parts = stripped.split(sep).filter(Boolean);
      if (parts.length !== 2 && parts.length !== 3) return null;
      const parsed = parts.map((p) => {
        const m = p.match(/^\s*(-?\d+(?:\.\d+)?)\s*(mm|cm|in|inch|m)?\s*$/i);
        if (!m) return null;
        const val = Number(m[1]);
        return Number.isFinite(val) && val > 0 ? {val, unit: m[2] ? m[2].toLowerCase() : null} : null;
      });
      if (!parsed.every(Boolean)) return null;
      const trailing = parsed[parsed.length - 1].unit || "mm";
      const out = parsed.map((item) => round1(item.val * (UNIT_MM[item.unit || trailing] || 1)));
      return out.every((v) => v > 0 && v <= 50000) ? out : null;
    }
    const iso = text.replace(/\s+/g, "").toUpperCase().match(/^([AB])-?(\d)$/);
    if (iso && ISO_SIZES_MM[iso[1] + iso[2]]) return [...ISO_SIZES_MM[iso[1] + iso[2]]];
    if (/大\s*16\s*开/.test(text)) return [210, 285];
    if (/正\s*16\s*开/.test(text)) return [185, 260];
    return null;
  }

  function parsePaperBulk(paperText) {
    const text = String(paperText || "").normalize("NFKC").trim();
    let gsm = null;
    const m = text.match(/(?<![-\d.])(\d+(?:\.\d+)?)\s*(?:g\s*\/\s*(?:m[²2]|㎡)|gsm|克\s*\/\s*平方米|克重|克|g(?![a-z]))/i);
    if (m) {
      const val = Number(m[1]);
      if (Number.isFinite(val) && val >= 20 && val <= 2000) gsm = round1(val);
    }
    let factor = 1.05, label = "常规纸张";
    if (/哑粉|无光铜|哑光铜|哑铜/.test(text)) { factor = 1.00; label = "哑粉纸"; }
    else if (/轻型|纯质|蒙肯/.test(text)) { factor = 1.55; label = /纯质/.test(text) ? "纯质纸" : "轻型纸"; }
    else if (/双胶|胶版|道林|书写/.test(text)) { factor = 1.25; label = "双胶纸"; }
    else if (/白卡|黑卡|卡纸|牛皮|特种|艺术/.test(text)) { factor = 1.30; label = /白卡/.test(text) ? "白卡纸" : "特种纸"; }
    else if (/铜版|光铜|亮铜/.test(text)) { factor = 0.90; label = "铜版纸"; }
    return {gsm, factor, label};
  }

  function estimateWorkcardSpine(pagesRaw, paperRaw, coverRaw, bindingRaw) {
    const pMatch = String(pagesRaw || "").normalize("NFKC").match(/(?<![-\d.])(\d+)\s*(?:页|[Pp])?/);
    const pages = pMatch ? Number(pMatch[1]) : null;
    if (!pages || pages <= 0 || pages > 5000) return null;
    const inner = parsePaperBulk(paperRaw);
    if (!inner.gsm) return null;
    const isSaddle = /骑马钉|骑马装/.test(String(bindingRaw || ""));
    const cover = coverRaw ? parsePaperBulk(coverRaw) : null;
    let rawThickness;
    if (!isSaddle && cover && cover.gsm && pages > 4) {
      rawThickness = ((pages - 4) / 2) * (inner.gsm / 1000) * inner.factor + 2 * (cover.gsm / 1000) * cover.factor;
    } else {
      rawThickness = (pages / 2) * (inner.gsm / 1000) * inner.factor;
    }
    const spineMm = Math.max(0.2, round1(rawThickness));
    const warnings = [];
    if (isSaddle && (pages >= 48 || spineMm >= 3.5)) warnings.push("骑马钉页数偏厚，建议评估改为胶装");
    if (isSaddle && pages % 4 !== 0) warnings.push(`骑马钉页数须为 4 的倍数（当前 ${pages}P）`);
    return {spineMm, pages, gsm: inner.gsm, label: inner.label, warning: warnings.join("；") || ""};
  }

  function extractItemBlueprint(item) {
    const map = {};
    for (const field of item.fields || []) {
      if (!field.muted && field.key) map[field.key] = field.value;
    }
    const product = String(map.productType || item.rawTitle || "").trim();
    const mainParsed = parseBlueprintSize(map.size);
    const finished = parseBlueprintSize(map["dimensions.finishedSize"]) || (mainParsed && mainParsed.length === 2 ? mainParsed : null);
    const expanded = parseBlueprintSize(map["dimensions.expandedSize"] || map["productSpecs.expandedSize"]);
    const dieCut = parseBlueprintSize(map["dimensions.dieCutSize"] || map["productSpecs.dieCutSize"]);
    const pkg3d =
      parseBlueprintSize(map["dimensions.packageOuterSize"]) ||
      parseBlueprintSize(map["dimensions.packageInnerSize"]) ||
      parseBlueprintSize(map["dimensions.packageSize"]) ||
      (mainParsed && mainParsed.length === 3 ? mainParsed : null);
    const flatSize = dieCut || expanded || null;

    if (pkg3d && pkg3d.length === 3) {
      return {
        kind: "box",
        modeLabel: "包装盒三维线框",
        boxMm: pkg3d,
        flatMm: flatSize && flatSize.length === 2 ? flatSize : null,
        boxStructure: String(map["productSpecs.boxStructure"] || "").trim()
      };
    }

    const foldingText = String(map["productSpecs.folding"] || "").trim();
    const isFolded = (product === "折页" || /折页/.test(product) || (foldingText && !/^(无|不折|无需折叠)$/.test(foldingText)));
    const isBooklet = /宣传册|画册|书籍|手册|说明书|目录册/.test(product);
    const primary2d = finished || expanded || dieCut;
    if (!primary2d || primary2d.length !== 2) return null;

    const bleedRaw = String(map["productSpecs.bleed"] || "");
    const bleedMatch = bleedRaw.match(/(\d+(?:\.\d+)?)\s*mm/i);
    const bleedMm = /无出血/.test(bleedRaw) ? 0 : (bleedMatch ? round1(Number(bleedMatch[1])) : (product === "喷画" ? 5 : 3));
    const safeMm = Math.min(primary2d[0], primary2d[1]) >= 140 ? 5 : 3;

    if (isFolded) {
      let exp = expanded && expanded.length === 2 ? [...expanded] : null;
      const isBi = /二折|对折|2折/.test(foldingText);
      const isAcc = /风琴|Z字|Z折/i.test(foldingText);
      const panels = isBi ? 2 : /四折|4折/.test(foldingText) ? 4 : 3;
      if (exp && exp[0] < exp[1]) exp = [exp[1], exp[0]];
      if (!exp && finished) {
        const expW = (!isBi && !isAcc && panels === 3) ? round1(finished[0] * 3 - (finished[0] >= 60 ? 2 : 1)) : round1(finished[0] * panels);
        exp = [expW, finished[1]];
      }
      let panelWidths;
      if (!isBi && !isAcc && panels === 3) {
        if (Math.abs(exp[0] - 297) < 0.2) panelWidths = [100, 100, 97];
        else {
          const outer = round1((exp[0] + (exp[0] >= 150 ? 2 : 1)) / 3);
          panelWidths = [outer, outer, Math.max(1, round1(exp[0] - outer * 2))];
        }
      } else {
        const each = round1(exp[0] / panels);
        panelWidths = Array.from({length: panels - 1}, () => each).concat([round1(exp[0] - each * (panels - 1))]);
      }
      return {
        kind: "folded",
        modeLabel: isBi ? "二折压痕示意" : isAcc ? "风琴折示意" : "三折压痕示意",
        sizeMm: exp,
        finishedMm: finished,
        panelWidths,
        bleedMm,
        safeMm
      };
    }

    if (isBooklet) {
      const spine = estimateWorkcardSpine(
        map.pages || map["productSpecs.pages"],
        map.paper,
        map["productSpecs.coverPaper"],
        map.binding || map["productSpecs.binding"]
      );
      return {
        kind: "booklet",
        modeLabel: "画册成品与书脊",
        sizeMm: primary2d,
        bleedMm,
        safeMm,
        spine,
        binding: String(map.binding || map["productSpecs.binding"] || "").trim()
      };
    }

    const cornerText = `${map["productSpecs.cardCorners"] || ""} ${map.finishing || ""}`;
    const rounded = /圆角|\bR\s*\d+/i.test(cornerText) && !/直角|不要圆角|无需圆角/.test(String(map["productSpecs.cardCorners"] || ""));
    const circle = /圆形|圆贴/.test(String(map["productSpecs.labelShape"] || ""));
    return {
      kind: "flat",
      modeLabel: circle ? "圆形模切示意" : (product ? `${product}平面规格` : "平面裁切规格"),
      sizeMm: primary2d,
      bleedMm,
      safeMm,
      rounded,
      circle
    };
  }

  function readTokens() {
    const styles = window.getComputedStyle(window.document.documentElement);
    const map = {};
    for (const key of Object.keys(TOKEN_FALLBACKS)) {
      const raw = (styles.getPropertyValue(key) || "").trim();
      map[key] = raw || TOKEN_FALLBACKS[key];
    }
    return (name) => map[name] || TOKEN_FALLBACKS[name] || "#292722";
  }

  const serif = (size, weight = 600) => ({font: `${weight} ${size}px ${SERIF}`});
  const sans = (size, weight = 500, spacing = "") => ({font: `${weight} ${size}px ${SANS}`, spacing});
  const mono = (size, weight = 500) => ({font: `${weight} ${size}px ${MONO}`});

  function setFont(ctx, spec) {
    ctx.font = spec.font;
    ctx.letterSpacing = spec.spacing || "0px";
  }

  /* CJK 场景逐字符断行即可；超出行数时末行以省略号收尾。 */
  function wrapLines(ctx, text, maxWidth, maxLines) {
    const source = String(text ?? "").replace(/\s+/g, " ").trim();
    if (!source) return [];
    const lines = [];
    let current = "";
    for (const char of source) {
      if (current && ctx.measureText(current + char).width > maxWidth) {
        lines.push(current);
        current = char;
      } else current += char;
    }
    if (current) lines.push(current);
    if (lines.length <= maxLines) return lines;
    let last = lines[maxLines - 1];
    while (last && ctx.measureText(last + "…").width > maxWidth) last = last.slice(0, -1);
    return lines.slice(0, maxLines - 1).concat(last ? last + "…" : lines[maxLines - 1]);
  }

  /* 值的呈现与交接单 markdown 的 text() 同规：空缺原样示为“待确认/待补充”。 */
  function displayValue(value) {
    if (value == null || value === "") return {text: "待确认/待补充", muted: true};
    if (typeof value === "boolean") return {text: value ? "是" : "否", muted: false};
    return {text: String(value), muted: false};
  }

  function buildModel(handoff) {
    const raw = Array.isArray(handoff.items) ? handoff.items : [];
    const items = raw.map((item, position) => {
      const fields = (item.fields || []).map((field) => {
        const shown = displayValue(field.value);
        const statusLabel = FIELD_STATUS_LABELS[field.status] || (shown.muted ? "待核对" : "");
        return {
          key: field.key || "",
          label: field.label || field.key || "",
          value: shown.text,
          muted: shown.muted,
          statusLabel,
          alert: shown.muted || FIELD_ALERT_STATUSES.has(statusLabel)
        };
      });
      const builtItem = {
        heading: (raw.length > 1 ? `产品项 ${position + 1} · ` : "") + (item.title || "待识别产品"),
        rawTitle: item.title || "",
        itemId: item.itemId || "",
        option: item.selectedOptionDetails || null,
        file: item.file || {},
        fileLabel: FILE_STATUS_LABELS[item.file?.status] || "",
        fileStatus: item.file?.status || "",
        fileOk: item.file?.status === "checked",
        checks: (item.file?.checks || []).map((check) => ({
          label: String(check.label ?? ""),
          status: CHECK_STATUS_LABELS[check.status] || "未确认",
          detail: check.detail == null || check.detail === "" ? "未记录" : String(check.detail)
        })),
        warnings: (item.file?.warnings || []).map((warning) => String(warning)),
        fields
      };
      builtItem.blueprint = extractItemBlueprint(builtItem);
      return builtItem;
    });
    const deadlines = [...new Set(items.map((item) => {
      const field = item.fields.find((candidate) => candidate.key === "deadline");
      return field && !field.muted ? field.value : "";
    }).filter(Boolean))];
    // “文件待交付”说明还没有可检查的文件，不计入检查分母。
    const checkedStatuses = new Set(["checked", "stale", "needs_review"]);
    const filesTotal = items.filter((item) => checkedStatuses.has(item.fileStatus)).length;
    const filesChecked = items.filter((item) => item.fileOk).length;
    // 交接说明 = 文档 notes + 交接阶段事项（与 markdown 的 交接说明 同源）。
    const baseNotes = (handoff.notes || []).map(String);
    const handoffNotes = (handoff.issues || [])
      .filter((issue) => issue.section === "handoff" && issue.message && !baseNotes.includes(issue.message))
      .map((issue) => String(issue.message));
    return {
      orderId: handoff.orderId, revision: handoff.revision, status: handoff.status || "unconfirmed",
      confirmedAt: handoff.confirmedAt || "", confirmedRevision: handoff.confirmedRevision,
      generatedAt: handoff.generatedAt || "", items,
      notes: baseNotes.concat(handoffNotes),
      deadline: deadlines.length === 1 ? deadlines[0] : deadlines.length > 1 ? "以各产品项约定为准" : "",
      sideNote: filesTotal ? `文件检查 ${filesChecked}/${filesTotal}` : (items.length > 1 ? `共 ${items.length} 项产品` : "")
    };
  }

  function roundedPath(ctx, x, y, w, h, r) {
    const [tl, tr, br, bl] = Array.isArray(r) ? r : [r, r, r, r];
    ctx.beginPath();
    ctx.moveTo(x + tl, y);
    ctx.lineTo(x + w - tr, y);
    ctx.arcTo(x + w, y, x + w, y + tr, tr);
    ctx.lineTo(x + w, y + h - br);
    ctx.arcTo(x + w, y + h, x + w - br, y + h, br);
    ctx.lineTo(x + bl, y + h);
    ctx.arcTo(x, y + h, x, y + h - bl, bl);
    ctx.lineTo(x, y + tl);
    ctx.arcTo(x, y, x + tl, y, tl);
    ctx.closePath();
  }

  function drawOps(ctx, ops) {
    ctx.textBaseline = "alphabetic";
    for (const o of ops) {
      if (o.op === "rect") {
        if (ctx.setLineDash) ctx.setLineDash(o.dash || []);
        roundedPath(ctx, o.x, o.y, o.w, o.h, o.r || 0);
        if (o.fill) { ctx.fillStyle = o.fill; ctx.fill(); }
        if (o.stroke) { ctx.strokeStyle = o.stroke; ctx.lineWidth = o.strokeW || 1; ctx.stroke(); }
        if (o.dash && ctx.setLineDash) ctx.setLineDash([]);
      } else if (o.op === "line") {
        if (ctx.setLineDash) ctx.setLineDash(o.dash || []);
        ctx.strokeStyle = o.color;
        ctx.lineWidth = o.width || 1;
        ctx.beginPath();
        ctx.moveTo(o.x1, o.y1);
        ctx.lineTo(o.x2, o.y2);
        ctx.stroke();
        if (o.dash && ctx.setLineDash) ctx.setLineDash([]);
      } else if (o.op === "poly") {
        if (!Array.isArray(o.points) || o.points.length < 2) continue;
        if (ctx.setLineDash) ctx.setLineDash(o.dash || []);
        ctx.beginPath();
        ctx.moveTo(o.points[0][0], o.points[0][1]);
        for (let i = 1; i < o.points.length; i++) ctx.lineTo(o.points[i][0], o.points[i][1]);
        if (o.close !== false) ctx.closePath();
        if (o.fill) { ctx.fillStyle = o.fill; ctx.fill(); }
        if (o.stroke) { ctx.strokeStyle = o.stroke; ctx.lineWidth = o.strokeW || 1; ctx.stroke(); }
        if (o.dash && ctx.setLineDash) ctx.setLineDash([]);
      } else if (o.op === "circle") {
        if (ctx.setLineDash) ctx.setLineDash(o.dash || []);
        ctx.beginPath();
        ctx.arc(o.cx, o.cy, o.r, 0, Math.PI * 2);
        if (o.fill) { ctx.fillStyle = o.fill; ctx.fill(); }
        if (o.stroke) { ctx.strokeStyle = o.stroke; ctx.lineWidth = o.strokeW || 1; ctx.stroke(); }
        if (o.dash && ctx.setLineDash) ctx.setLineDash([]);
      } else if (o.op === "text") {
        ctx.font = o.font;
        ctx.letterSpacing = o.spacing || "0px";
        ctx.fillStyle = o.color;
        ctx.textAlign = o.align || "left";
        ctx.fillText(o.text, o.x, o.y);
      }
    }
  }

  function fitThumbRect(rawW, rawH, bx, by, bw, bh) {
    const ratio = Math.max(0.4, Math.min(2.8, Math.max(1, rawW) / Math.max(1, rawH)));
    let w, h;
    if (ratio >= bw / bh) { w = bw; h = bw / ratio; }
    else { h = bh; w = bh * ratio; }
    return {x: round1(bx + (bw - w) / 2), y: round1(by + (bh - h) / 2), w: round1(w), h: round1(h)};
  }

  /* 在产品项字段格下方绘制印前尺寸缩略示意图条（纯 Canvas 2D），
     同步量高与记录 ops，保证预览与 2x 导出零裁切、零重叠。 */
  function appendBlueprintStrip(ops, ctx, bp, y, t, text) {
    const diagW = 196;
    const diagH = 88;
    const infoX = PAD + diagW + 24;
    const infoW = CONTENT_W - diagW - 36;

    let primaryText = "";
    let secondaryText = "";
    let secondaryColor = t("--quiet");
    if (bp.kind === "box") {
      primaryText = `三维 ${fmtDims(bp.boxMm)}${bp.boxStructure ? ` · ${bp.boxStructure}` : ""}`;
      secondaryText = bp.flatMm
        ? `展开/刀模 ${fmtDims(bp.flatMm)} · 红L / 绿W / 蓝H 等轴测示意`
        : "红L / 绿W / 蓝H 等轴测线框示意 · 完稿前请向印厂核对刀模线";
      secondaryColor = bp.flatMm ? t("--ink-soft") : t("--quiet");
    } else if (bp.kind === "folded") {
      primaryText = `展开 ${fmtDims(bp.sizeMm)}（分幅 ${bp.panelWidths.map(fmtMm).join("+")} mm）`;
      secondaryText = `${bp.finishedMm ? `成品 ${fmtDims(bp.finishedMm)} · ` : ""}红虚线出血 ${fmtMm(bp.bleedMm)}mm · 蓝虚线折痕 · 绿点线安全边`;
    } else if (bp.kind === "booklet") {
      primaryText = bp.spine
        ? `成品 ${fmtDims(bp.sizeMm)} · 参考书脊 ≈ ${fmtMm(bp.spine.spineMm)} mm（${bp.spine.pages}P · ${bp.spine.gsm}g ${bp.spine.label}）`
        : `成品 ${fmtDims(bp.sizeMm)} · 四边出血 ${fmtMm(bp.bleedMm)} mm · 安全边 ${fmtMm(bp.safeMm)} mm`;
      if (bp.spine && bp.spine.warning) {
        secondaryText = `⚠ ${bp.spine.warning}`;
        secondaryColor = t("--warning");
      } else {
        secondaryText = "实线裁切 · 红虚线出血 · 绿点线安全边 · 右侧为书脊剖面放大示意";
      }
    } else {
      primaryText = `成品 ${fmtDims(bp.sizeMm)} · 四边出血 ${fmtMm(bp.bleedMm)} mm · 安全边 ${fmtMm(bp.safeMm)} mm`;
      secondaryText = bp.rounded
        ? "实线裁切（含圆角示意） · 红虚线出血 · 绿点线安全边"
        : bp.circle
          ? "圆形实线模切 · 红虚线出血 · 绿点线安全边"
          : "实线裁切 · 红虚线出血 · 绿点线安全边";
    }

    setFont(ctx, sans(12, 600));
    const primaryLines = wrapLines(ctx, primaryText, infoW, 3);
    setFont(ctx, sans(10.5, 500));
    const secondaryLines = wrapLines(ctx, secondaryText, infoW, 2);
    const disclaimerText = "仅规格示意，不构成最终刀模尺寸、实纸书脊或专业印前放行结论";
    setFont(ctx, sans(9.5, 500));
    const disclaimerLines = wrapLines(ctx, disclaimerText, infoW, 2);

    const rightH = 34 + primaryLines.length * 17 + 4 + secondaryLines.length * 15 + 6 + disclaimerLines.length * 13 + 12;
    const stripH = Math.max(114, rightH);
    const diagX = PAD + 12;
    const diagY = y + Math.round((stripH - diagH) / 2);

    // 外框 + 左侧暖纸制图底框
    ops.push({op: "rect", x: PAD, y, w: CONTENT_W, h: stripH, r: 8, fill: t("--paper-raise"), stroke: t("--line-warm"), strokeW: 1});
    ops.push({op: "rect", x: diagX, y: diagY, w: diagW, h: diagH, r: 6, fill: t("--paper-light"), stroke: t("--rule-light"), strokeW: 1});

    // 左侧缩略矢量图
    if (bp.kind === "box") {
      const [L, W, H] = bp.boxMm;
      const maxAxis = Math.max(L, W, H, 1);
      const u = 32;
      const len = Math.max(0.35, L / maxAxis) * u;
      const wid = Math.max(0.35, W / maxAxis) * u;
      const hei = Math.max(0.35, H / maxAxis) * u;
      const cos30 = 0.866, sin30 = 0.5;
      const cx = diagX + diagW / 2;
      const rawP0 = [cx, diagY + diagH - 12];
      const rawPL = [rawP0[0] + len * cos30, rawP0[1] - len * sin30];
      const rawPW = [rawP0[0] - wid * cos30, rawP0[1] - wid * sin30];
      const rawPB = [rawPW[0] + len * cos30, rawPW[1] - len * sin30];
      const rawT0 = [rawP0[0], rawP0[1] - hei];
      const rawTL = [rawPL[0], rawPL[1] - hei];
      const rawTW = [rawPW[0], rawPW[1] - hei];
      const rawTB = [rawPB[0], rawPB[1] - hei];
      const shiftY = (diagY + diagH / 2 + 4) - (rawTB[1] + rawP0[1]) / 2;
      const s = (p) => [round1(p[0]), round1(p[1] + shiftY)];
      const P0 = s(rawP0), PL = s(rawPL), PW = s(rawPW), PB = s(rawPB);
      const T0 = s(rawT0), TL = s(rawTL), TW = s(rawTW), TB = s(rawTB);

      ops.push({op: "poly", points: [T0, TL, TB, TW], fill: t("--paper-light")});
      ops.push({op: "poly", points: [P0, PW, TW, T0], fill: t("--paper-raise")});
      ops.push({op: "poly", points: [P0, PL, TL, T0], fill: t("--paper-deep")});
      ops.push({op: "line", x1: PB[0], y1: PB[1], x2: PW[0], y2: PW[1], color: t("--line-warm"), dash: [3, 2]});
      ops.push({op: "line", x1: PB[0], y1: PB[1], x2: PL[0], y2: PL[1], color: t("--line-warm"), dash: [3, 2]});
      ops.push({op: "line", x1: PB[0], y1: PB[1], x2: TB[0], y2: TB[1], color: t("--line-warm"), dash: [3, 2]});
      ops.push({op: "poly", points: [T0, TL, TB, TW], stroke: t("--ink"), strokeW: 1.4});
      ops.push({op: "line", x1: P0[0], y1: P0[1], x2: T0[0], y2: T0[1], color: t("--ink"), width: 1.4});
      ops.push({op: "line", x1: PW[0], y1: PW[1], x2: TW[0], y2: TW[1], color: t("--ink"), width: 1.4});
      // 三轴彩色标线：红 L / 绿 W / 蓝 H
      ops.push({op: "line", x1: P0[0], y1: P0[1], x2: PL[0], y2: PL[1], color: t("--red"), width: 2});
      ops.push({op: "line", x1: PW[0], y1: PW[1], x2: P0[0], y2: P0[1], color: t("--green"), width: 2});
      ops.push({op: "line", x1: PL[0], y1: PL[1], x2: TL[0], y2: TL[1], color: t("--info"), width: 2});
      const topCx = round1((T0[0] + TB[0]) / 2), topCy = round1((T0[1] + TB[1]) / 2);
      ops.push({op: "line", x1: topCx, y1: topCy + 1, x2: topCx, y2: topCy - 10, color: t("--info"), width: 1.3});
      text("↑", topCx, topCy - 8, sans(9, 700), t("--info"), {align: "center"});
      text(`L${fmtMm(L)}`, round1((P0[0] + PL[0]) / 2 + 8), round1((P0[1] + PL[1]) / 2 + 10), mono(8.5, 700), t("--red-deep"), {align: "center"});
      text(`W${fmtMm(W)}`, round1((PW[0] + P0[0]) / 2 - 8), round1((PW[1] + P0[1]) / 2 + 10), mono(8.5, 700), t("--green"), {align: "center"});
      text(`H${fmtMm(H)}`, PL[0] + 6, round1((PL[1] + TL[1]) / 2 + 3), mono(8.5, 700), t("--info"));
    } else if (bp.kind === "booklet") {
      const fit = fitThumbRect(bp.sizeMm[0], bp.sizeMm[1], diagX + 14, diagY + 18, 94, 54);
      if (bp.bleedMm > 0) {
        ops.push({op: "rect", x: fit.x - 4, y: fit.y - 4, w: fit.w + 8, h: fit.h + 8, r: 0, stroke: t("--red"), strokeW: 1.1, dash: [4, 2]});
      }
      ops.push({op: "rect", x: fit.x, y: fit.y, w: fit.w, h: fit.h, r: 0, fill: t("--paper-raise"), stroke: t("--ink"), strokeW: 1.4});
      ops.push({op: "rect", x: fit.x, y: fit.y, w: Math.max(6, Math.round(fit.w * 0.1)), h: fit.h, r: 0, fill: t("--paper-deep")});
      if (fit.w > 18 && fit.h > 14) {
        ops.push({op: "rect", x: fit.x + 8, y: fit.y + 4, w: fit.w - 12, h: fit.h - 8, r: 0, stroke: t("--green"), strokeW: 1, dash: [2, 2]});
      }
      text(`${fmtMm(bp.sizeMm[0])}×${fmtMm(bp.sizeMm[1])}`, round1(fit.x + fit.w / 2), fit.y - 7, mono(8.5, 600), t("--ink-soft"), {align: "center"});

      // 右侧书脊剖面放大条
      const spCx = diagX + 154;
      const spTop = diagY + 18;
      const spH = 44;
      const spW = bp.spine ? Math.max(10, Math.min(26, Math.round(8 + Math.sqrt(bp.spine.spineMm) * 5))) : 12;
      const spX = round1(spCx - spW / 2);
      ops.push({op: "rect", x: spX, y: spTop, w: spW, h: spH, r: 1, fill: t("--paper-light"), stroke: t("--ink"), strokeW: 1.4});
      const leaves = Math.max(2, Math.floor(spW / 4));
      for (let i = 1; i < leaves; i++) {
        const lx = round1(spX + (spW * i) / leaves);
        ops.push({op: "line", x1: lx, y1: spTop + 2, x2: lx, y2: spTop + spH - 2, color: t("--line-warm"), width: 0.8});
      }
      text("书脊", spCx, spTop - 5, sans(8.5, 700), t("--brass"), {align: "center"});
      text(
        bp.spine ? `≈${fmtMm(bp.spine.spineMm)}mm` : "待估算",
        spCx,
        spTop + spH + 14,
        mono(9, 700),
        bp.spine && bp.spine.warning ? t("--warning") : t("--ink"),
        {align: "center"}
      );
    } else if (bp.kind === "folded") {
      const fit = fitThumbRect(bp.sizeMm[0], bp.sizeMm[1], diagX + 20, diagY + 19, 156, 50);
      if (bp.bleedMm > 0) {
        ops.push({op: "rect", x: fit.x - 4, y: fit.y - 4, w: fit.w + 8, h: fit.h + 8, r: 0, stroke: t("--red"), strokeW: 1.1, dash: [4, 2]});
      }
      ops.push({op: "rect", x: fit.x, y: fit.y, w: fit.w, h: fit.h, r: 0, fill: t("--paper-raise"), stroke: t("--ink"), strokeW: 1.4});
      if (fit.w > 18 && fit.h > 14) {
        ops.push({op: "rect", x: fit.x + 4, y: fit.y + 4, w: fit.w - 8, h: fit.h - 8, r: 0, stroke: t("--green"), strokeW: 1, dash: [2, 2]});
      }
      const total = bp.panelWidths.reduce((a, b) => a + b, 0) || 1;
      let cx = fit.x;
      bp.panelWidths.forEach((pw, idx) => {
        const segW = (pw / total) * fit.w;
        text(`P${idx + 1}`, round1(cx + segW / 2), round1(fit.y + fit.h / 2 + 3), mono(8.5, 700), t("--ink-soft"), {align: "center"});
        if (idx < bp.panelWidths.length - 1) {
          const lx = round1(cx + segW);
          ops.push({op: "line", x1: lx, y1: fit.y - 4, x2: lx, y2: fit.y + fit.h + 4, color: t("--info"), width: 1.3, dash: [5, 2, 1.5, 2]});
        }
        cx += segW;
      });
      text(`展开 ${fmtMm(bp.sizeMm[0])}×${fmtMm(bp.sizeMm[1])}`, round1(fit.x + fit.w / 2), fit.y - 7, mono(8.5, 600), t("--ink-soft"), {align: "center"});
    } else {
      // flat
      const fit = fitThumbRect(bp.sizeMm[0], bp.sizeMm[1], diagX + 22, diagY + 19, 124, 50);
      if (bp.circle) {
        const r = round1(Math.min(fit.w, fit.h) / 2);
        const cx = round1(fit.x + fit.w / 2), cy = round1(fit.y + fit.h / 2);
        if (bp.bleedMm > 0) ops.push({op: "circle", cx, cy, r: r + 4, stroke: t("--red"), strokeW: 1.1, dash: [4, 2]});
        ops.push({op: "circle", cx, cy, r, fill: t("--paper-raise"), stroke: t("--ink"), strokeW: 1.5});
        if (r > 8) ops.push({op: "circle", cx, cy, r: r - 4, stroke: t("--green"), strokeW: 1, dash: [2, 2]});
        text(`Ø ${fmtMm(bp.sizeMm[0])} mm`, cx, cy - r - 7, mono(8.5, 600), t("--ink-soft"), {align: "center"});
      } else {
        const cr = bp.rounded ? 5 : 0;
        if (bp.bleedMm > 0) {
          ops.push({op: "rect", x: fit.x - 4, y: fit.y - 4, w: fit.w + 8, h: fit.h + 8, r: cr ? cr + 2 : 0, stroke: t("--red"), strokeW: 1.1, dash: [4, 2]});
        }
        ops.push({op: "rect", x: fit.x, y: fit.y, w: fit.w, h: fit.h, r: cr, fill: t("--paper-raise"), stroke: t("--ink"), strokeW: 1.5});
        if (fit.w > 18 && fit.h > 14) {
          ops.push({op: "rect", x: fit.x + 4, y: fit.y + 4, w: fit.w - 8, h: fit.h - 8, r: Math.max(0, cr - 2), stroke: t("--green"), strokeW: 1, dash: [2, 2]});
        }
        text(`${fmtMm(bp.sizeMm[0])} mm`, round1(fit.x + fit.w / 2), fit.y - 7, mono(8.5, 600), t("--ink-soft"), {align: "center"});
        text(`${fmtMm(bp.sizeMm[1])}`, round1(fit.x + fit.w + 7), round1(fit.y + fit.h / 2 + 3), mono(8.5, 600), t("--ink-soft"));
      }
    }

    // 右侧规格说明文字
    let ry = y + 22;
    text(`印前规格示意 · ${bp.modeLabel}`, infoX, ry, sans(10, 700, "0.6px"), t("--brass"));
    text("仅规格示意", CARD_WIDTH - PAD - 12, ry, sans(9.5, 600), t("--quiet"), {align: "right"});
    ry += 20;
    primaryLines.forEach((line, i) => text(line, infoX, ry + i * 17, sans(12, 600), t("--ink")));
    ry += primaryLines.length * 17 + 4;
    secondaryLines.forEach((line, i) => text(line, infoX, ry + i * 15, sans(10.5, bp.spine && bp.spine.warning ? 600 : 500), secondaryColor));
    ry += secondaryLines.length * 15 + 6;
    disclaimerLines.forEach((line, i) => text(line, infoX, ry + i * 13, sans(9.5, 500), t("--ink-faint")));

    return stripH;
  }

  /* 度量与绘制共用一条布局路径：先在 1x 画布上量出总高并记录绘制指令，
     再按导出倍率重建画布回放指令，保证预览与导出像素一致。 */
  function buildOps(ctx, model, t) {
    const ops = [];
    const text = (value, x, y, spec, color, extra = {}) => ops.push({op: "text", text: value, x, y, font: spec.font, spacing: spec.spacing || "", color, ...extra});
    const rule = (y1, color) => ops.push({op: "line", x1: PAD, y1: y1 + 0.5, x2: CARD_WIDTH - PAD, y2: y1 + 0.5, color});
    // 小节标签（工艺方案/印刷文件/文件基础检查/交接说明），黄铜色小字距眉题。
    const section = (label) => {
      y += 10;
      text(label, PAD, y + 10, sans(10, 700, "1px"), t("--brass"));
      y += 22;
    };
    // 整行“标签：内容”，随内容换行。
    const detailRows = (rows) => {
      for (const rowText of rows) {
        const lines = wrapLines(ctx, rowText, CONTENT_W, 6);
        lines.forEach((lineText, i) => text(lineText, PAD, y + 13 + i * 16, sans(12, 500), t("--ink-soft")));
        y += lines.length * 16 + 5;
      }
    };

    // 顶部印条 + 品牌行 + 状态徽章 + 单号行 + 确认信息行
    ops.push({op: "rect", x: 0, y: 0, w: CARD_WIDTH, h: 6, r: 0, fill: t("--red")});
    let y = 34;
    setFont(ctx, serif(15, 700));
    const brandW = Math.ceil(ctx.measureText("PrintOps").width) + 28;
    ops.push({op: "rect", x: PAD, y, w: brandW, h: 32, r: [11, 4, 11, 4], fill: t("--red-deep")});
    text("PrintOps", PAD + 14, y + 21, serif(15, 700), "#fffaf0");
    text("印刷工单 · 交接单图像版", PAD + brandW + 12, y + 21, sans(11, 700, "1.5px"), t("--quiet"));
    const status = STATUS_LABELS[model.status] || STATUS_LABELS.unconfirmed;
    const statusColor = STATUS_COLORS[model.status] || STATUS_COLORS.unconfirmed;
    setFont(ctx, sans(11, 700));
    const badgeW = Math.ceil(ctx.measureText(status).width) + 24;
    ops.push({op: "rect", x: CARD_WIDTH - PAD - badgeW, y: y + 4, w: badgeW, h: 24, r: 12, fill: t(statusColor.bg), stroke: t(statusColor.line), strokeW: 1});
    text(status, CARD_WIDTH - PAD - badgeW / 2, y + 20, sans(11, 700), t(statusColor.text), {align: "center"});
    y += 44;
    text(`${model.orderId} · r${model.revision}`, PAD, y + 10, mono(12), t("--quiet"));
    y += 22;
    const confirmedAt = model.confirmedAt ? String(model.confirmedAt).replace("T", " ").slice(0, 16) : "尚未确认";
    const confirmedRev = model.confirmedRevision == null ? "无" : String(model.confirmedRevision);
    text(`确认 ${confirmedAt} · 确认版本 ${confirmedRev} · 尺寸单位 mm`, PAD, y + 10, mono(11), t("--ink-faint"));
    y += 24;
    y += 14;
    rule(y, t("--rule"));
    rule(y + 2, t("--rule"));
    y += 26;

    // 产品项：衬线标题 + 单号 + 文件状态 + 全字段 2 列格 + 工艺/文件/检查/提醒
    const cellW = (CONTENT_W - 10) / 2;
    for (const [index, item] of model.items.entries()) {
      if (index) {
        y += 6;
        rule(y, t("--rule-light"));
        y += 16;
      }
      text(item.heading, PAD, y + 15, serif(15), t("--ink"));
      setFont(ctx, serif(15));
      const headingW = ctx.measureText(item.heading).width;
      if (item.itemId) text(`· ${item.itemId}`, PAD + headingW + 10, y + 15, mono(10), t("--ink-faint"));
      if (item.fileLabel) text(item.fileLabel, CARD_WIDTH - PAD, y + 15, sans(11, 700), t(item.fileOk ? "--green" : "--warning"), {align: "right"});
      y += 24;
      for (let start = 0; start < item.fields.length; start += 2) {
        const row = item.fields.slice(start, start + 2);
        const inner = cellW - 20;
        setFont(ctx, sans(13, 600));
        const rows = row.map((field) => wrapLines(ctx, field.value, inner, 12));
        const cellH = Math.max(...rows.map((lines) => 35 + lines.length * 16));
        row.forEach((field, column) => {
          const x = PAD + column * (cellW + 10);
          ops.push({op: "rect", x, y, w: cellW, h: cellH, r: 8, fill: t("--bg-raised"), stroke: t("--line-warm"), strokeW: 1});
          text(field.label, x + 10, y + 18, sans(10, 700, "0.5px"), t("--quiet"));
          if (field.statusLabel) {
            text(field.statusLabel, x + cellW - 10, y + 18, sans(9, 700), t(field.alert ? "--warning" : "--ink-faint"), {align: "right"});
          }
          rows[column].forEach((value, i) => text(value, x + 10, y + 38 + i * 16, sans(13, 600), t(field.muted ? "--warning" : "--ink")));
        });
        y += cellH + 8;
      }

      // 拥有明确平面/三维尺寸时，紧随字段格附带印前尺寸缩略示意图条。
      if (item.blueprint) {
        y += 2;
        const stripH = appendBlueprintStrip(ops, ctx, item.blueprint, y, t, text);
        y += stripH + 8;
      }

      // 工艺方案（与交接单“工艺方案：标题；组成：…”同内容）
      section("工艺方案");
      if (item.option) {
        detailRows([
          item.option.title || "已选工艺方案",
          item.option.printMode ? `印刷方式：${item.option.printMode}` : "",
          item.option.composition ? `组成：${item.option.composition}` : ""
        ].filter(Boolean));
      } else {
        text("未选择", PAD, y + 13, sans(12, 500), t("--warning"));
        y += 21;
      }

      // 印刷文件（文件名 · 检查状态 · 范围 · 限制 · 时间，同交接单“文件：”段）
      section("印刷文件");
      const file = item.file;
      detailRows([
        `文件：${file.fileName ? String(file.fileName) + " · " : ""}${item.fileLabel || "未执行检查"}`,
        file.scope ? `检查范围：${file.scope}` : "",
        file.limitations ? String(file.limitations) : "",
        file.checkedAt ? `检查时间：${String(file.checkedAt).replace("T", " ").slice(0, 16)}` : ""
      ].filter(Boolean));

      // 文件基础检查表（基础检查项 | 结果 | 说明）
      if (item.checks.length) {
        section("文件基础检查");
        const colStatus = PAD + 170;
        const colDetail = PAD + 250;
        const detailW = CONTENT_W - 250;
        text("基础检查项", PAD, y + 11, sans(9, 700), t("--ink-faint"));
        text("结果", colStatus, y + 11, sans(9, 700), t("--ink-faint"));
        text("说明", colDetail, y + 11, sans(9, 700), t("--ink-faint"));
        y += 20;
        for (const [checkIndex, check] of item.checks.entries()) {
          if (checkIndex) rule(y, t("--rule-light"));
          y += 6;
          const detailLines = wrapLines(ctx, check.detail, detailW, 4);
          text(check.label, PAD, y + 12, sans(10, 500), t("--ink-soft"));
          text(check.status, colStatus, y + 12, sans(10, 700), t(check.status === "通过" ? "--green" : check.status === "未通过" ? "--red-deep" : "--warning"));
          detailLines.forEach((lineText, i) => text(lineText, colDetail, y + 12 + i * 15, sans(10, 500), t("--quiet")));
          y += Math.max(18, detailLines.length * 15) + 10;
        }
        y += 2;
      }

      // 文件复核提醒
      for (const warning of item.warnings) {
        y += 4;
        const lines = wrapLines(ctx, `文件复核：${warning}`, CONTENT_W, 4);
        lines.forEach((lineText, i) => text(lineText, PAD, y + 12 + i * 15, sans(10, 600), t("--warning")));
        y += lines.length * 15 + 10;
      }
    }

    // 交期强调条（多产品交期不一致时不替用户拍板）
    if (model.deadline) {
      y += 6;
      const stripH = 42;
      ops.push({op: "rect", x: PAD, y, w: CONTENT_W, h: stripH, r: 8, fill: t("--gold-pale")});
      ops.push({op: "rect", x: PAD, y, w: 10, h: stripH, r: [8, 0, 0, 8], fill: t("--brass")});
      text("交期", PAD + 24, y + 26, sans(11, 700, "0.5px"), t("--warning"));
      text(model.deadline, PAD + 58, y + 26, sans(14, 700), t("--ink"));
      if (model.sideNote) text(model.sideNote, CARD_WIDTH - PAD - 14, y + 26, mono(11), t("--quiet"), {align: "right"});
      y += stripH;
    }

    // 交接说明（notes + 交接阶段事项）
    if (model.notes.length) {
      y += 10;
      rule(y, t("--rule-light"));
      y += 18;
      text("交接说明", PAD, y + 13, serif(13), t("--ink"));
      y += 26;
      for (const note of model.notes) {
        const lines = wrapLines(ctx, note, CONTENT_W - 14, 6);
        text("·", PAD, y + 12, sans(11, 700), t("--brass"));
        lines.forEach((lineText, i) => text(lineText, PAD + 14, y + 12 + i * 15, sans(10.5, 500), t("--ink-soft")));
        y += lines.length * 15 + 9;
      }
    }

    y += 20;
    rule(y, t("--rule-light"));
    y += 18;
    const generated = model.generatedAt ? `生成于 ${String(model.generatedAt).replace("T", " ").slice(0, 16)} · ` : "";
    text(`${generated}本地交接资料 · 尚未发送供应商`, PAD, y + 10, sans(10.5), t("--quiet"));
    return {ops, height: Math.max(y + 36, 430)};
  }

  function renderCard(handoff, options = {}) {
    if (!handoff || !Array.isArray(handoff.items) || !handoff.items.length) throw new Error("交接单中没有可展示的产品项。");
    const scale = Math.max(1, Math.min(4, Number(options.scale) || EXPORT_SCALE));
    const canvas = window.document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("当前环境不支持画布渲染。");
    canvas.width = CARD_WIDTH;
    canvas.height = 10;
    const t = readTokens();
    const {ops, height} = buildOps(ctx, buildModel(handoff), t);
    canvas.width = CARD_WIDTH * scale;
    canvas.height = Math.round(height * scale);
    ctx.scale(scale, scale);
    // 导出的 PNG 必须自带纸色卡底，否则透明画布贴进聊天工具会显成黑底。
    const scene = [{op: "rect", x: 0, y: 0, w: CARD_WIDTH, h: height, r: 0, fill: t("--paper-light")}].concat(ops);
    drawOps(ctx, scene);
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `工单图像版：${handoff.orderId} r${handoff.revision}，${handoff.items.length} 个产品项`);
    return canvas;
  }

  function cardFileName(handoff) {
    return `printops-workcard-${handoff.orderId}-r${handoff.revision}.png`;
  }

  function toPngBlob(canvas) {
    return new Promise((resolve, reject) => canvas.toBlob(
      (blob) => blob ? resolve(blob) : reject(new Error("画布导出失败，请重试。")), "image/png"));
  }

  let wired = false, dialog, canvasSlot, downloadButton, copyButton, current = null, currentCanvas = null;
  // app.js 打开预览时传入自己的 showToast；未传则回退到全局（便于独立调试）。
  let notifier = null;
  function notify(message, type) {
    const deliver = notifier || (typeof window.showToast === "function" ? window.showToast : null);
    if (deliver) deliver(message, type);
  }

  function wire() {
    dialog = window.document.getElementById("workcard-dialog");
    canvasSlot = window.document.getElementById("workcard-preview");
    downloadButton = window.document.getElementById("workcard-download");
    copyButton = window.document.getElementById("workcard-copy");
    const closeButton = window.document.getElementById("workcard-close");
    if (!dialog || !canvasSlot || !downloadButton || !copyButton || !closeButton) return false;
    copyButton.hidden = !(window.ClipboardItem && navigator.clipboard && navigator.clipboard.write);
    downloadButton.addEventListener("click", async () => {
      if (!currentCanvas) return;
      try {
        const blob = await toPngBlob(currentCanvas);
        const url = URL.createObjectURL(blob);
        const link = window.document.createElement("a");
        link.href = url;
        link.download = cardFileName(current);
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        notify("已生成工单图像版 PNG，请打开核对；尚未发送供应商。");
      } catch (error) { notify(error.message || "工单图像版导出失败，请重试。", "error"); }
    });
    copyButton.addEventListener("click", async () => {
      if (!currentCanvas) return;
      try {
        const blob = await toPngBlob(currentCanvas);
        await navigator.clipboard.write([new ClipboardItem({"image/png": blob})]);
        notify("工单图像版已复制，可直接粘贴给同事或供应商。");
      } catch (error) { notify("复制失败，浏览器未授权剪贴板；请改用下载 PNG。", "error"); }
    });
    closeButton.addEventListener("click", () => dialog.close());
    return true;
  }

  function open(handoff, options = {}) {
    notifier = typeof options.notify === "function" ? options.notify : null;
    if (!wired) wired = wire();
    if (!wired) throw new Error("工单小卡组件缺失，请确认页面文件完整后刷新。");
    currentCanvas = renderCard(handoff);
    current = handoff;
    canvasSlot.replaceChildren(currentCanvas);
    if (dialog.open) dialog.close();
    dialog.showModal();
    return true;
  }

  return {renderCard, cardFileName, open};
});
