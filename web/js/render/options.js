/* Process option cards, comparison matrix, quick replies, and readiness progress bar. */

import { actions, appState } from "../store.js";
import { $, hasValue, revealPanel, showToast } from "../dom.js";
import { api, apiErrorText } from "../api.js";
import { renderPriceEstimate } from "./panels.js";

export function renderQuickReplies() {
  const root = $("#quick-replies");
  root.innerHTML = "";
  (appState.state.quickReplies || []).forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "quick-reply";
    button.textContent = item.label;
    button.addEventListener("click", () => actions.sendMessage(item.label, item.data));
    root.appendChild(button);
  });
}

export function renderOptionCompare(options) {
  const wrap = $("#option-compare");
  const note = $("#option-note");
  const comparable = options.length >= 2;
  if (note) {
    note.hidden = !comparable;
    if (comparable) {
      const order = appState.state.order.items?.[appState.state.activeItemIndex] || appState.state.order;
      note.textContent = `比较基准：${order.productType || "当前产品"} · ${order.quantity || "数量待确认"} · ${order.size || "尺寸待确认"}。示例估算不含税与运输，交期为示例，均需供应商核实。`;
    }
  }
  if (!wrap) return;
  wrap.hidden = !comparable;
  wrap.innerHTML = "";
  if (!comparable) return;
  const recommendedIndex = Math.max(0, options.findIndex((o) => o.score === "综合推荐"));
  const rows = [
    ["印刷方式", (o) => o.printMode],
    ["材料", (o) => o.paper],
    ["表面工艺", (o) => o.finishing],
    ["装订/后道", (o) => o.binding],
    ["示例估算", (o) => o.estimate?.status === "partial" ? "部分费用可算，非完整总价" : o.estimate?.status === "blocked" ? "待补充计价依据" : o.refPrice],
    ["交期参考", (o) => o.leadDetail || o.lead],
    ["适用", (o) => o.score]
  ];
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  const headLabel = document.createElement("th");
  headLabel.textContent = "维度";
  headRow.appendChild(headLabel);
  options.forEach((option, index) => {
    const th = document.createElement("th");
    th.textContent = option.title;
    if (index === recommendedIndex) th.className = "recommended";
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  const filter = document.createElement("label");
  filter.className = "compare-filter";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  filter.append(checkbox, "只看方案差异");
  checkbox.addEventListener("change", () => tbody.querySelectorAll(".same-option-value").forEach(row => {row.hidden = checkbox.checked;}));
  wrap.append(filter);
  rows.forEach(([label, pick]) => {
    const tr = document.createElement("tr");
    const different = new Set(options.map(pick)).size > 1;
    tr.className = different ? "different-option-value" : "same-option-value";
    const th = document.createElement("th");
    th.textContent = `${label}${different ? " · 差异" : ""}`;
    tr.appendChild(th);
    options.forEach((option, index) => {
      const td = document.createElement("td");
      td.textContent = pick(option) || "未填";
      if (index === recommendedIndex) td.className = "recommended";
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
}

export function renderOptions() {
  const root = $("#option-list");
  root.innerHTML = "";
  const recommendations = $("#recommendations");
  const options = appState.state.options || [];
  recommendations.hidden = !options.length;
  $("#recommendation-count").textContent = `${options.length} 个方案`;
  renderOptionCompare(options);
  options.forEach((option, index) => {
    const selected = appState.state.selectedOption === option.id || appState.state.activeItemSelectedOption === option.id;
    const card = document.createElement("article");
    card.className = `option-card ${selected ? "selected" : ""}`;
    card.setAttribute("aria-label", option.title);
    card.innerHTML = `${option.score === "综合推荐" || index === 1 ? '<span class="option-badge">推荐</span>' : ""}<div class="option-card-heading"><h4></h4><span class="option-score"></span></div><p></p><dl class="option-grid"></dl><button class="option-details-toggle" type="button">展开推荐理由</button><div class="option-details" hidden></div>`;
    card.querySelector("h4").textContent = option.title;
    card.querySelector(".option-score").textContent = option.score || "";
    card.append(PrintOpsJourney.el("small", "本地示例参数 · 不含税/运输 · 非供应商报价", "option-estimate-label"));
    card.querySelector("p").textContent = option.description;
    [["示例估算", option.refPrice], ["交期", option.leadDetail || option.lead], ["印刷方式", option.printMode], ["材料", option.paper], ["工艺", option.finishing], ["装订", option.binding]].forEach(([name, value]) => {
      if (!value) return;
      const row = document.createElement("div");
      const term = document.createElement("dt");
      const detail = document.createElement("dd");
      term.textContent = name;
      detail.textContent = value;
      row.append(term, detail);
      card.querySelector(".option-grid").appendChild(row);
    });
    const toggle = card.querySelector(".option-details-toggle");
    const details = card.querySelector(".option-details");
    details.innerHTML = "";
    const reason = document.createElement("p");
    reason.textContent = option.reason ? `适合：${option.reason}` : "";
    const risk = document.createElement("p");
    risk.textContent = option.risk ? `注意：${option.risk}` : "";
    details.append(reason, risk);
    if (option.estimate) {
      const breakdown = PrintOpsWorkbench.priceDetails(option.estimate);
      breakdown.addEventListener("click", (event) => event.stopPropagation());
      breakdown.addEventListener("keydown", (event) => event.stopPropagation());
      card.append(breakdown);
    }
    toggle.addEventListener("click", (event) => {
      event.stopPropagation();
      const visible = details.hidden;
      details.hidden = !visible;
      toggle.textContent = visible ? "收起推荐理由" : "展开推荐理由";
    });
    const choose = PrintOpsJourney.button(selected ? "已选此方案" : `选择${option.title}`, () => chooseOption(option.id), "choose-option secondary-action");
    choose.setAttribute("aria-pressed", String(selected));
    choose.disabled = selected;
    card.append(choose);
    root.appendChild(card);
  });

}

export async function chooseOption(optionId) {
  const origin = document.activeElement;
  const selectionFocus = origin?.classList.contains("choose-option");
  const originSession = appState.sessionId, originEpoch = appState.viewEpoch;
  try {
    const data = await api("/api/choose", { sessionId: appState.sessionId, optionId,
      itemIndex: Array.isArray(appState.state.order?.items) && appState.state.order.items.length > 1 ? appState.state.activeItemIndex : undefined });
    const restoreFocus = selectionFocus && originSession === appState.sessionId && originEpoch === appState.viewEpoch &&
      $("#workspace").dataset.view === "compare" && (document.activeElement === origin || document.activeElement === document.body);
    actions.render(data);
    if (restoreFocus) $("#phase-compare").focus({preventScroll: true});
  }
  catch (error) { showToast(apiErrorText(error, "方案选择暂时失败，请重试"), "error"); }
}

export function renderProgress() {
  const required = ["productType", "quantity", "size", "paper", "printing", "deadline"];
  const workflowStage = appState.state.workflowStage || appState.state.stage || "collect";
  // Progress = field readiness from the backend validation; workflow stages
  // are shown by the step list, not baked into the percentage.
  const fallback = Math.round((required.filter((key) => appState.state.order[key]).length / required.length) * 100);
  const readiness = Number(appState.state.readiness);
  let percent = Number.isFinite(readiness) && readiness > 0 ? Math.min(100, Math.round(readiness)) : fallback;
  const items = appState.state.order?.items || [];
  if (items.length > 1) {
    const fields = (appState.state.review?.fields || []).filter(field => field.itemId && field.required);
    percent = fields.length ? Math.round(100 * fields.filter(field => hasValue(field.value) && !["missing", "pending_confirmation", "inferred", "stale"].includes(field.status)).length / fields.length)
      : Math.round(100 * items.reduce((count, item) => count + required.filter(key => hasValue(item[key])).length, 0) / (items.length * required.length));
  }
  $("#progress-stage").textContent = appState.state.workflowLabel || ({ collect: "需求收集", clarify: "品类澄清", recommend: "方案选择", preflight: "文件预检", quote: "报价准备", confirm: "确认订单", export: "导出交接" })[workflowStage] || "需求收集";
  const bar = $("#progress-bar");
  bar.style.width = `${percent}%`;
  bar.title = "字段信息度";
  $("#progress-percent").textContent = `信息度 ${percent}%`;
  const order = ["collect", "recommend", "confirm"];
  document.querySelectorAll(".step").forEach((step) => {
    const displayStage = workflowStage === "clarify" ? "collect" : ["preflight", "quote", "export"].includes(workflowStage) ? "confirm" : workflowStage;
    step.classList.toggle("active", step.dataset.step === displayStage);
    step.classList.toggle("done", order.indexOf(step.dataset.step) < order.indexOf(displayStage));
  });
}
