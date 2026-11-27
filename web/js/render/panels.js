/* Secondary panels: decision, quote status, provider status, estimate, trace, validation, files, tools, stats, catalog. */

import {
  ESTIMATE_STATUS_LABELS, QUOTE_STATUS_LABELS, categoryExamples, labels, toolCodes, toolLabels,
} from "../constants.js";
import { actions, appState } from "../store.js";
import {
  $, fmtMs, formatFileSize, formatFileTime, hasValue, revealPanel, showToast,
} from "../dom.js";
import { api, apiErrorText } from "../api.js";
import { getActivePdfPreview } from "../pdf.js";

export function renderDecision() {
  const stage = appState.state.workflowStage || appState.state.stage || "collect";
  const label = appState.state.workflowLabel || ({ collect: "需求收集", clarify: "品类澄清", recommend: "方案选择", preflight: "文件预检", quote: "报价准备", confirm: "订单确认", export: "导出交接" }[stage] || "需求收集");
  const decision = appState.state.decision || {};
  const workflow = $("#workflow-label");
  const reason = $("#decision-reason");
  const run = $("#decision-run");
  const confidence = $("#decision-confidence");
  if (workflow) workflow.textContent = label;
  if (reason) reason.textContent = decision.reason || appState.state.nextAction || "等待第一条需求。";
  if (confidence) confidence.textContent = decision.humanConfirmationRequired ? "需人工确认" : "可解释";
  if (run) run.textContent = appState.state.runId ? `运行 ${appState.state.runId}` : "本次运行尚未开始";
}

export function renderQuoteStatus() {
  const panel = $("#quote-panel");
  if (!panel) return;
  const requests = Array.isArray(appState.state.quoteRequests) ? appState.state.quoteRequests : [];
  const request = appState.state.quoteRequest || requests[requests.length - 1];
  if (!request?.requestId || !request.status) {
    panel.hidden = true;
    return;
  }
  const status = $("#quote-status");
  status.textContent = QUOTE_STATUS_LABELS[request.status] || request.status;
  status.className = `quote-status status-${request.status}`;
  $("#quote-message").textContent = request.message || (request.status === "stale" ? request.staleReason || "订单已变化，请重新准备询价。" : "");
  $("#quote-request-id").textContent = request.requestId;
  const canCancel = ["awaiting_human_confirmation", "confirmed"].includes(request.status);
  $("#quote-cancel").disabled = !canCancel;
  $("#quote-refresh").disabled = false;
  const quoteWasHidden = panel.hidden;
  panel.hidden = false;
  if (quoteWasHidden) revealPanel(panel);
}

export function renderProviderStatus() {
  const panel = $("#provider-status");
  const provider = appState.providers.find((entry) => entry.id === appState.state.order.platform);
  panel.textContent = `${provider?.name || "供应商"}：${PrintOpsWorkbench.providerStatus(provider)}。${provider?.verified ? "数据已经验证" : "生产能力、价格与交期均待供应商确认"}`;
  panel.title = `来源：${provider?.source?.description || provider?.source || "本地字段模板"}；更新时间：${provider?.updatedAt || "未注明"}；有效期：${provider?.expiresAt || "未验证"}`;
}

export function renderPriceEstimate(result) {
  const panel = $("#estimate-panel");
  if (renderPriceEstimate.sessionId !== appState.sessionId) renderPriceEstimate.cached = null;
  if (result?.type === "estimate") {
    renderPriceEstimate.cached = {result, revision: appState.state.revision, itemId: result.itemId || appState.state.activeItemId || null};
    renderPriceEstimate.sessionId = appState.sessionId;
  }
  const cached = renderPriceEstimate.cached;
  if (!cached) { panel.hidden = true; return; }
  const activeItemId = appState.state.activeItemId || appState.state.order?.items?.[appState.state.activeItemIndex]?.itemId || null;
  if (cached.itemId !== activeItemId) { panel.hidden = true; return; }
  const stale = cached.revision !== appState.state.revision;
  const estimate = cached.result;
  panel.replaceChildren();
  const title = stale ? "历史示例估算 · 参数已变" : (ESTIMATE_STATUS_LABELS[estimate.status] || "示例估算");
  panel.append(PrintOpsJourney.el("h3", title));
  panel.append(PrintOpsJourney.el("p", estimate.status === "ready" ? estimate.displayRange : estimate.status === "partial" ? `已知费用小计 ¥${estimate.knownSubtotal || "0.00"}，不代表总价` : "当前不提供完整价格区间", "estimate-total"));
  const item = appState.state.order?.items?.find(item => item.itemId === cached.itemId);
  panel.append(PrintOpsJourney.el("p", `${item ? `${item.productType || "产品"} · ${cached.itemId} · ` : ""}订单版本 ${cached.revision} · 不含税 / 出厂 · 不含运输、打样及文件修改`, "estimate-scope"));
  if (stale) panel.append(PrintOpsJourney.el("p", "原金额已过期，不能作为当前订单估算。"));
  panel.append(PrintOpsJourney.button(stale ? "重新估算" : "更新估算", () => callTool("estimate_price")));
  panel.append(PrintOpsWorkbench.priceDetails(estimate));
  const estimateWasHidden = panel.hidden;
  panel.hidden = false;
  if (estimateWasHidden) revealPanel(panel);
}

export function renderRunTrace() {
  const root = $("#run-trace-list");
  const id = $("#run-id");
  if (!root || !id) return;
  const events = Array.isArray(appState.state.runTrace) ? appState.state.runTrace : [];
  id.textContent = appState.state.runId ? appState.state.runId : "等待运行";
  root.innerHTML = "";
  if (!events.length) {
    const empty = document.createElement("p");
    empty.className = "run-empty";
    empty.textContent = "发送需求后，这里会显示感知、规划和工具执行步骤。";
    root.append(empty);
    return;
  }
  events.forEach((event) => {
    const row = document.createElement("div");
    row.className = `run-event run-${event.status || "ok"}`;
    const marker = document.createElement("span");
    marker.className = "run-event-marker";
    const body = document.createElement("span");
    body.className = "run-event-body";
    const title = document.createElement("strong");
    title.textContent = event.tool ? `${event.step} · ${event.tool}` : event.step;
    const detail = document.createElement("small");
    detail.textContent = event.durationMs ? `${event.detail || ""} · ${event.durationMs}ms` : event.detail || "";
    body.append(title, detail);
    row.append(marker, body);
    root.append(row);
  });
}

export function renderValidation() {
  const validation = appState.state.validation || { missing: [], risks: [] };
  const missingRisks = (validation.missing || []).map((field) => ({
    level: "missing",
    message: `缺少必填字段：${field}`,
    suggestion: "可直接发送内容补充，或在订单草稿中查看待补充项。"
  }));
  const lowConfidenceRisks = Object.entries(appState.state.fieldMeta || {})
    .filter(([, meta]) => hasValue(meta?.value) && Number(meta?.confidence) < 0.75)
    .map(([key, meta]) => ({
      level: "warning",
      message: `${labels[key] || key}来自${meta.sourceLabel || "模型推断"}，置信度 ${Math.round(Number(meta.confidence) * 100)}%`,
      suggestion: "请在订单草稿中确认或修改这个字段。"
    }));
  const capability = appState.state.supplierCapability || {};
  const capabilityRisks = (capability.unsupported || []).map((item) => ({
    level: "error", message: `平台能力不匹配：${item.field}`, suggestion: item.message || "请切换平台或联系供应商确认。"
  })).concat((capability.needsReview || []).map((item) => ({
    level: "warning", message: `平台待确认：${item.field}`, suggestion: item.message || "请在询价前人工确认。"
  })));
  const risks = [...missingRisks, ...(validation.risks || []), ...lowConfidenceRisks, ...capabilityRisks];
  const panel = $("#risk-panel");
  panel.hidden = risks.length === 0;
  $("#risk-count").textContent = `${risks.length} 项`;
  const list = $("#risk-list");
  list.innerHTML = "";
  risks.forEach((item) => {
    const risk = document.createElement("div");
    risk.className = `risk-item risk-${item.level || "warning"}`;
    const message = document.createElement("strong");
    message.textContent = item.message;
    risk.append(message);
    if (item.suggestion) {
      const suggestion = document.createElement("small");
      suggestion.textContent = `建议：${item.suggestion}`;
      risk.append(suggestion);
    }
    list.appendChild(risk);
  });
}

export function renderFileState() {
  const node = $("#file-state");
  const items = Array.isArray(appState.state.order?.items) ? appState.state.order.items : [];
  const activeItem = items.length > 1 && Number.isInteger(appState.state.activeItemIndex) ? items[appState.state.activeItemIndex] : null;
  const storedFile = activeItem ? activeItem.uploadedFile : appState.state.uploadedFile;
  const storedCheck = activeItem ? appState.state.preflightResults?.[activeItem.itemId] : appState.state.preflightResults;
  const feedback = appState.fileFeedback || (storedFile ? { ok: Boolean(storedCheck?.ok && !storedCheck?.stale), fileName: storedFile,
    message: storedCheck?.stale ? "规格已修改，原基础检查已过期，请重新选择文件检查。" : storedCheck?.ok ? "基础检查已通过；不替代专业印前验收。" : "文件已记录，基础检查仍需核对。" } : null);
  const localPreview = getActivePdfPreview();
  const activePreview = localPreview
    && (items.length <= 1 || !storedFile || storedFile === localPreview.fileName)
    && (!feedback || localPreview.fileName === feedback.fileName) ? localPreview : null;
  node.hidden = !feedback && !activePreview;
  if (!feedback && !activePreview) return;
  node.className = `file-state ${feedback?.ok && activePreview?.preflight?.ok !== false ? "ok" : "error"}`;
  node.innerHTML = "";
  const heading = document.createElement("div");
  heading.className = "file-state-heading";
  const name = document.createElement("strong");
  name.textContent = activePreview?.fileName || feedback?.fileName || "未命名文件";
  heading.append(name);
  heading.append(PrintOpsJourney.el("small", `所属产品：${activeItem?.productType || appState.state.order.productType || "当前订单"} · ${storedCheck?.updatedAt ? `检查时间 ${formatFileTime(storedCheck.updatedAt)}` : "尚无有效检查时间"}`));
  if (feedback?.message) heading.append(PrintOpsJourney.el("p", feedback.message));
  const renderChecksBlock = (preflight) => {
    if (!preflight || !Array.isArray(preflight.checks) || !preflight.checks.length) return null;
    const checks = document.createElement("div");
    checks.className = "file-checks";
    preflight.checks.forEach((item) => {
      const check = document.createElement("div");
      check.className = `file-check check-${item.status}`;
      const term = document.createElement("span");
      const detail = document.createElement("strong");
      term.textContent = item.label;
      detail.textContent = item.detail;
      check.append(term, detail);
      checks.appendChild(check);
    });
    if (Array.isArray(preflight.warnings) && preflight.warnings.length) {
      const warning = document.createElement("p");
      warning.className = "file-warning";
      warning.textContent = `需要确认：${preflight.warnings.join("；")}`;
      checks.appendChild(warning);
    }
    return checks;
  };
  if (activePreview) {
    const meta = document.createElement("div");
    meta.className = "file-meta";
    [
      ["大小", formatFileSize(activePreview.size)],
      ["类型", activePreview.type === "application/pdf" ? "PDF" : activePreview.type || "PDF"],
      ["修改时间", formatFileTime(activePreview.lastModified)]
    ].forEach(([label, value]) => {
      const item = document.createElement("div");
      const term = document.createElement("span");
      const detail = document.createElement("strong");
      term.textContent = label;
      detail.textContent = value;
      item.append(term, detail);
      meta.append(item);
    });
    const viewer = document.createElement("iframe");
    viewer.className = "pdf-viewer";
    viewer.title = `${activePreview.fileName} 预览`;
    viewer.src = activePreview.url;
    const fallback = document.createElement("div");
    fallback.className = "pdf-preview-fallback";
    const openLink = document.createElement("a");
    openLink.href = activePreview.url;
    openLink.target = "_blank";
    openLink.rel = "noopener";
    openLink.textContent = "在新窗口打开 PDF";
    fallback.append("内置预览不可用时，", openLink);
    heading.append(meta);
    const checksNode = renderChecksBlock(activePreview.preflight || storedCheck);
    if (checksNode) heading.append(checksNode);
    heading.append(viewer, fallback);
  } else {
    const checksNode = renderChecksBlock(storedCheck);
    if (checksNode) heading.append(checksNode);
    else {
      const detail = document.createElement("small");
      detail.textContent = feedback?.message || "";
      heading.append(detail);
    }
  }
  node.append(heading);
}

export function renderTools() {
  const root = $("#tool-list");
  if (!root) return;
  const tools = (appState.state.availableTools || []).filter((item) => toolLabels[item.name]);
  const countLabel = `${tools.length} 项`;
  $("#tool-count") && ($("#tool-count").textContent = countLabel);
  $("#tool-count-label") && ($("#tool-count-label").textContent = countLabel);
  $("#settings-tool-summary") && ($("#settings-tool-summary").textContent = countLabel);
  root.innerHTML = "";
  tools.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tool-item";
    const code = document.createElement("span");
    code.className = "tool-code";
    code.textContent = toolCodes[item.name] || "··";
    code.title = item.name;
    const copy = document.createElement("span");
    copy.innerHTML = `<strong>${toolLabels[item.name][0]}</strong><small>${toolLabels[item.name][1]}</small>`;
    button.append(code, copy);
    button.addEventListener("click", () => callTool(item.name));
    root.appendChild(button);
  });
}

// v1.3.0 tool-call statistics in the settings toolbox. The render only ever
// uses createElement/textContent — no HTML interpolation — so server-side
// tool names and reasons cannot inject markup.
export function renderToolStats(stats) {
  const list = $("#tool-stats-list");
  const empty = $("#tool-stats-empty");
  if (!list) return;
  const totals = (stats && typeof stats === "object" && stats.totals) || {};
  const tools = Array.isArray(stats?.tools) ? stats.tools : [];
  const reasons = Array.isArray(stats?.blockedReasons) ? stats.blockedReasons : [];
  list.innerHTML = "";
  const isEmpty = !tools.length && !Number(totals.calls);
  if (empty) {
    empty.hidden = !isEmpty;
    empty.textContent = "暂无记录：发送需求或点击工具后，这里会显示调用量、成功率与耗时。";
  }
  if (isEmpty) return;
  const summary = document.createElement("div");
  summary.className = "run-event run-ok";
  const summaryBody = document.createElement("span");
  summaryBody.className = "run-event-body";
  const summaryTitle = document.createElement("strong");
  summaryTitle.textContent = `全部工具 · ${Number(totals.calls) || 0} 次`;
  const summaryDetail = document.createElement("small");
  summaryDetail.textContent = `成功率 ${Number(totals.successRate) || 0}% · 平均 ${fmtMs(totals.avgMs)} · p95 ${fmtMs(totals.p95Ms)}`;
  summaryBody.append(summaryTitle, summaryDetail);
  summary.append(document.createElement("span"), summaryBody);
  list.append(summary);
  tools.forEach((tool) => {
    const row = document.createElement("div");
    row.className = `run-event run-${tool.blocked || tool.failed || tool.rejected ? "fallback" : "ok"}`;
    const body = document.createElement("span");
    body.className = "run-event-body";
    const title = document.createElement("strong");
    title.textContent = `${tool.tool || "未知工具"} · ${Number(tool.calls) || 0} 次`;
    const detail = document.createElement("small");
    const parts = [`成功率 ${Number(tool.successRate) || 0}%`, `平均 ${fmtMs(tool.avgMs)}`, `p95 ${fmtMs(tool.p95Ms)}`];
    if (Number(tool.blocked)) parts.push(`阻断 ${tool.blocked}`);
    if (Number(tool.failed)) parts.push(`失败 ${tool.failed}`);
    if (Number(tool.rejected)) parts.push(`拒绝 ${tool.rejected}`);
    detail.textContent = parts.join(" · ");
    body.append(title, detail);
    row.append(document.createElement("span"), body);
    list.append(row);
  });
  if (reasons.length) {
    const row = document.createElement("div");
    row.className = "run-event run-fallback";
    const body = document.createElement("span");
    body.className = "run-event-body";
    const title = document.createElement("strong");
    title.textContent = "阻断原因";
    const detail = document.createElement("small");
    detail.textContent = reasons
      .slice(0, 6)
      .map((item) => `${item.reason || "未注明"} ×${Number(item.count) || 0}`)
      .join("、");
    body.append(title, detail);
    row.append(document.createElement("span"), body);
    list.append(row);
  }
}

export async function loadToolStats() {
  const list = $("#tool-stats-list");
  const empty = $("#tool-stats-empty");
  if (!list) return;
  try {
    renderToolStats(await api("/api/tools/stats?window=30d", undefined, "GET"));
  } catch (error) {
    list.innerHTML = "";
    if (empty) {
      empty.hidden = false;
      empty.textContent = "调用统计读取失败，请稍后重新打开设置。";
    }
  }
}

export function renderCatalog(categories = []) {  const root = $("#catalog-list");
  if (!root) return;
  root.innerHTML = "";
  const countLabel = `${categories.length} 类`;
  $("#catalog-count") && ($("#catalog-count").textContent = countLabel);
  $("#product-count") && ($("#product-count").textContent = countLabel);
  $("#settings-product-summary") && ($("#settings-product-summary").textContent = countLabel);
  categories.forEach((category) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "catalog-chip";
    button.textContent = category;
    button.title = categoryExamples[category] || category;
    button.addEventListener("click", () => { actions.closeSettingsDialog(); actions.sendMessage(categoryExamples[category] || `我想做${category}`); });
    root.appendChild(button);
  });
}

export async function callTool(toolName, payload = {}) {
  const epoch = appState.viewEpoch;
  try {
    if (toolName === "explain_print_term" && !payload.question) {
      payload = { question: appState.state.order.productType ? `${appState.state.order.productType}的纸张和工艺怎么选？` : "纸张和印刷工艺怎么选？" };
    }
    const result = await api("/api/tools/call", { sessionId: appState.sessionId, toolName, payload });
    if (epoch !== appState.viewEpoch) return;
    actions.render(result);
    showToast(`已调用：${toolLabels[toolName]?.[0] || toolName}`);
  } catch (error) {
    if (epoch === appState.viewEpoch) showToast(apiErrorText(error, "工具调用失败，请确认本地服务已启动，或先刷新会话"), "error");
  }
}
