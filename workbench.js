/* Presentation helpers. All externally supplied text enters textContent. */
(function (root) {
  const node = (tag, text, cls) => {
    const element = document.createElement(tag);
    if (text != null) element.textContent = text;
    if (cls) element.className = cls;
    return element;
  };
  function priceDetails(estimate) {
    const box = node("details", null, "price-details");
    box.append(node("summary", "查看计价依据与费用组成"));
    const basis = estimate.basis || {};
    box.append(node("p", `${basis.sourceDescription || "本地参考参数"} · ${basis.version || "未标定"} · ${basis.region || "未指定区域"}`));
    box.append(node("small", `有效期：${basis.effectiveFrom || "未注明"} — ${basis.expiresAt || "未设失效日"} · ${basis.confidence === "illustrative" ? "示例参数，未经市场报价校准" : basis.confidence || "未验证"}`));
    const list = node("dl");
    for (const line of estimate.lineItems || []) {
      const row = node("div");
      row.append(node("dt", line.name), node("dd", `¥${line.amount} · ${line.basis || ""}`));
      list.append(row);
    }
    box.append(list);
    if (estimate.missing?.length) box.append(node("p", `待补充：${estimate.missing.join("、")}`));
    if (estimate.coverage?.unknownProcesses?.length) box.append(node("p", `未标定工艺：${estimate.coverage.unknownProcesses.join("、")}`));
    box.append(node("p", `包含：${(estimate.includes || []).join("、") || "见成本明细"}`));
    box.append(node("p", `未包含：${(estimate.excludes || []).join("、") || "无"}`));
    box.append(node("p", estimate.disclaimer || "示例参数估算，不构成报价，以供应商回复为准。"));
    return box;
  }
  function providerStatus(provider) {
    if (!provider) return "供应商未配置";
    if (provider.connectionStatus === "unconfigured") return "未配置 · 仅生成本地草稿";
    if (provider.mode === "export_only") return "仅导出 · 无网络请求";
    if (provider.mode === "sandbox") return "沙箱数据 · 不产生真实订单";
    return provider.verified && provider.networkEnabled ? "实时接口已配置" : "未验证 · 需供应商确认";
  }
  function tooltipIcon(text, cls, label, glyph = "?") {
    const icon = node("span", glyph, `tooltip-icon ${cls}`);
    icon.setAttribute("role", "img");
    icon.setAttribute("tabindex", "0");
    icon.setAttribute("aria-label", label || text);
    icon.title = text;
    icon.dataset.tooltip = text;
    return icon;
  }
  function evidence(meta) {
    // Provenance chips and "no traceable source" tooltips were noise beside
    // each input: the field value is the answer, and the quote is the only
    // part worth keeping behind a disclosure.
    const box = node("span", null, "field-evidence");
    box.hidden = true;
    const quote = meta?.evidence?.quote;
    if (!quote) return box;
    const details = node("details");
    details.append(node("summary", "依据"), node("p", quote));
    box.append(details);
    box.hidden = false;
    return box;
  }
  function manualDimension(value) {
    if (/(?:cm|inch|in)(?![a-z])|厘米|英寸|["″]/i.test(value)) throw new Error("手工尺寸请使用 mm；其他单位可在对话中输入并自动换算。");
    if (/^\s*\d+(?:\.\d+)?\s*[×x*]\s*\d+(?:\.\d+)?(?:\s*[×x*]\s*\d+(?:\.\d+)?)?\s*$/i.test(value)) return value + " mm";
    return value;
  }
  const api = {priceDetails, providerStatus, evidence, manualDimension};
  root.PrintOpsWorkbench = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : window);
