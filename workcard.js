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
    "--paper-light": "#fffaf0", "--bg-raised": "rgba(249, 242, 226, .5)",
    "--ink": "#292722", "--ink-soft": "#514b43", "--quiet": "#62594d", "--ink-faint": "#a39682",
    "--rule": "#c8b89c", "--rule-light": "#dfd3bd", "--line-warm": "#c9b99e",
    "--red": "#a34a3e", "--red-deep": "#78332d",
    "--brass": "#96713e", "--gold-pale": "#f2e7cf", "--line-gold": "#c5a46d",
    "--green": "#466b5d", "--green-pale": "#e2eadf", "--line-green": "#aabfae",
    "--warning": "#8a5a27", "--line-red": "#d8a28c"
  };

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
      return {
        heading: (raw.length > 1 ? `产品项 ${position + 1} · ` : "") + (item.title || "待识别产品"),
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
        ctx.fillStyle = o.fill;
        if (o.stroke) { ctx.strokeStyle = o.stroke; ctx.lineWidth = o.strokeW || 1; }
        roundedPath(ctx, o.x, o.y, o.w, o.h, o.r);
        ctx.fill();
        if (o.stroke) ctx.stroke();
      } else if (o.op === "line") {
        ctx.strokeStyle = o.color;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(o.x1, o.y1);
        ctx.lineTo(o.x2, o.y2);
        ctx.stroke();
      } else if (o.op === "text") {
        ctx.font = o.font;
        ctx.letterSpacing = o.spacing || "0px";
        ctx.fillStyle = o.color;
        ctx.textAlign = o.align || "left";
        ctx.fillText(o.text, o.x, o.y);
      }
    }
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
