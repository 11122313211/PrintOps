const $ = (selector) => document.querySelector(selector);
const labels = {
  productType: "印刷品", purpose: "使用场景", quantity: "数量", size: "成品尺寸",
  pages: "页数", orientation: "版式方向", paper: "纸张/材料", printing: "印刷颜色", finishing: "表面工艺", binding: "装订/后道",
  deadline: "交期", budget: "预算偏好", platform: "目标平台"
};
const overviewFields = ["productType", "quantity", "purpose", "deadline", "budget", "platform"];
const defaultDraftFields = { specs: ["size", "pages", "orientation"], process: ["paper", "printing", "finishing", "binding"] };
const productDraftFields = {
  "宣传册": { specs: ["size", "orientation"], process: ["paper", "printing", "finishing"] },
  "画册": { specs: ["size", "orientation"], process: ["paper", "printing", "finishing"] },
  "单页": { specs: ["size", "orientation"], process: ["paper", "printing", "finishing"] },
  "折页": { specs: ["size", "orientation"], process: ["paper", "printing", "finishing", "binding"] },
  "名片": { specs: ["size", "orientation"], process: ["paper", "printing", "finishing"] },
  "PVC卡": { specs: ["size"], process: ["printing", "finishing", "binding"] },
  "吊牌": { specs: ["size"], process: ["paper", "printing", "finishing", "binding"] },
  "联单": { specs: ["size", "orientation"], process: ["paper", "printing", "finishing"] },
  "信封封套": { specs: [], process: ["paper", "printing", "finishing", "binding"] },
  "标签": { specs: ["size"], process: ["printing", "finishing", "binding"] },
  "包装盒": { specs: [], process: ["paper", "printing", "finishing", "binding"] },
  "手提袋": { specs: [], process: ["printing", "finishing", "binding"] },
  "纸杯": { specs: [], process: ["printing", "finishing", "binding"] },
  "海报": { specs: ["size"], process: ["printing", "finishing", "binding"] },
  "喷画": { specs: ["size"], process: ["printing", "finishing", "binding"] },
  "PVC": { specs: ["size"], process: ["printing", "finishing", "binding"] }
};
const toolLabels = {
  validate_order: ["校验订单", "检查缺失字段与风险"],
  recommend_processes: ["推荐工艺", "比较效果、成本、交期"],
  estimate_price: ["估算费用", "生成非正式价格区间"],
  prepare_handoff: ["生成交接单", "按目标平台整理字段"],
  explain_print_term: ["解释印刷", "理解纸张、出血与装订"]
};
const categoryExamples = {
  "名片/卡片": "做一批名片",
  "单张": "做一批单页",
  "标签/不干胶": "做一批不干胶标签",
  "书籍画册": "做一本画册",
  "广告物料": "做一张海报",
  "包装周边": "做一批包装盒",
  "办公用品": "做一批联单",
  "家居日常": "我想做家居印刷品",
  "动漫文创": "我想做动漫文创印刷品",
  "季节产品": "我想做季节产品",
  "现货/样品": "我想先看现货或样品"
};
let sessionId = localStorage.getItem("printops_session") || "";
let isSending = false;
let state = { order: {}, stage: "collect", quickReplies: [], options: [], selectedOption: null, orderGenerated: false, toolTrace: [], availableTools: [], productProfile: null };
let renderedProduct = "";
let hadRecommendations = false;
let llmSettings = { enabled: false, url: "", model: "", keyConfigured: false };
let pendingMessage = null;
let activeChatController = null;
let chatRequestSeq = 0;
let sidebarCollapsed = localStorage.getItem("printops_sidebar") === "collapsed";
let previousOrder = {};
let updatedOrderKeys = new Set();
let updatedSpecKeys = new Set();
let suppressOrderDiff = true;
let fileFeedback = null;

async function api(path, body, method = "POST", options = {}) {
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: method === "GET" ? undefined : JSON.stringify(body || {}),
    signal: options.signal
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    const error = new Error(detail.message || detail.error || `API ${response.status}`);
    error.code = detail.code;
    error.requestId = detail.requestId || response.headers.get("X-Request-ID") || "";
    throw error;
  }
  return response.json();
}

function apiErrorText(error, fallback) {
  const base = error?.message || fallback;
  return error?.requestId ? `${base}（请求号 ${error.requestId}）` : base;
}

function setSidebarCollapsed(collapsed) {
  sidebarCollapsed = Boolean(collapsed);
  $("#workspace")?.classList.toggle("sidebar-collapsed", sidebarCollapsed);
  const button = $("#sidebar-toggle");
  if (button) {
    button.setAttribute("aria-label", sidebarCollapsed ? "展开导航栏" : "收起导航栏");
    button.title = sidebarCollapsed ? "展开导航栏" : "收起导航栏";
    button.dataset.collapsed = String(sidebarCollapsed);
  }
  localStorage.setItem("printops_sidebar", sidebarCollapsed ? "collapsed" : "expanded");
}

function setHighContrast(enabled) {
  document.documentElement.dataset.theme = enabled ? "high-contrast" : "";
  const button = $("#theme-toggle");
  if (button) {
    button.setAttribute("aria-pressed", String(enabled));
    button.textContent = enabled ? "标准对比" : "高对比";
    button.title = enabled ? "切换回标准对比模式" : "切换到高对比模式";
  }
  localStorage.setItem("printops_theme", enabled ? "high-contrast" : "standard");
}

function addMessage(role, text) {
  const message = document.createElement("div");
  message.className = `message ${role}`;
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = role === "assistant" ? "AI" : "我";
  const content = document.createElement("div");
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.textContent = `${role === "assistant" ? "印刷订单智能体" : "当前订单"} · ${new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date())}`;
  content.append(bubble, meta);
  message.append(avatar, content);
  $("#chat-feed").appendChild(message);
  $("#chat-feed").scrollTop = $("#chat-feed").scrollHeight;
}

function addPendingMessage() {
  const message = document.createElement("div");
  message.className = "message assistant pending-message";
  message.setAttribute("role", "status");
  const avatar = document.createElement("div");
  avatar.className = "avatar pending-avatar";
  avatar.textContent = "AI";
  const content = document.createElement("div");
  const bubble = document.createElement("div");
  bubble.className = "bubble pending-bubble";
  const label = document.createElement("span");
  const phases = ["正在理解需求", "正在匹配工艺", "正在整理回复"];
  let phaseIndex = 0;
  label.textContent = phases[phaseIndex];
  const dots = document.createElement("span");
  dots.className = "thinking-dots";
  dots.setAttribute("aria-hidden", "true");
  dots.innerHTML = "<i></i><i></i><i></i>";
  bubble.append(label, dots);
  content.appendChild(bubble);
  message.append(avatar, content);
  $("#chat-feed").appendChild(message);
  $("#chat-feed").scrollTop = $("#chat-feed").scrollHeight;
  const timer = setInterval(() => {
    phaseIndex = (phaseIndex + 1) % phases.length;
    label.textContent = phases[phaseIndex];
  }, 900);
  return { node: message, stop: () => { clearInterval(timer); message.remove(); } };
}

function removePendingMessage() {
  pendingMessage?.stop();
  pendingMessage = null;
}

function setComposerBusy(busy) {
  const input = $("#message-input");
  const send = $("#message-form button[type='submit']");
  const upload = $("#upload-button");
  const label = send?.querySelector(".send-label");
  if (input) input.disabled = busy;
  if (send) {
    send.disabled = busy;
    send.classList.toggle("is-loading", busy);
    send.setAttribute("aria-busy", String(busy));
  }
  if (label) label.textContent = busy ? "处理中" : "发送";
  if (upload) upload.disabled = busy;
  const platform = $("#platform-select");
  if (platform) platform.disabled = busy;
  document.querySelectorAll("#quick-replies button, #tool-list button, #catalog-list button, #parameter-form input").forEach((item) => { item.disabled = busy; });
  if (busy) {
    $("#agent-status").innerHTML = '<span class="status-dot thinking"></span>正在处理';
    $("#agent-trace").textContent = "正在分析需求…";
  }
}

function render(data, showMessages = true) {
  state = { ...state, ...data, order: data.order || state.order };
  trackOrderChanges(state.order);
  if (data.llm) renderSettings({ llm: data.llm });
  sessionId = data.sessionId || sessionId;
  if (sessionId) {
    localStorage.setItem("printops_session", sessionId);
    $("#order-number").textContent = `SESSION ${sessionId.toUpperCase()}`;
  }
  if (showMessages) (data.messages || []).forEach((message) => {
    addMessage(typeof message === "string" ? "assistant" : message.role || "assistant", typeof message === "string" ? message : message.text || "");
  });
  renderDraft();
  renderFileState();
  renderQuickReplies();
  renderOptions();
  renderTools();
  renderProgress();
  $("#agent-trace").textContent = data.toolTrace?.length ? data.toolTrace.join("  /  ") : "等待输入";
  $("#memory-status").textContent = data.nextAction || (data.stage === "confirm" ? "订单记忆已锁定，等待人工确认。" : data.order?.productType ? "订单记忆已保存，可继续补充或修改。" : "等待第一条需求，Agent 将自动建立订单记忆。");
  $("#agent-status").innerHTML = '<span class="status-dot"></span>Agent 在线';
}

function trackOrderChanges(order) {
  if (suppressOrderDiff) {
    previousOrder = structuredClone(order || {});
    updatedOrderKeys = new Set();
    updatedSpecKeys = new Set();
    suppressOrderDiff = false;
    return;
  }
  const previousSpecs = previousOrder.productSpecs || {};
  const currentSpecs = order.productSpecs || {};
  updatedOrderKeys = new Set(Object.keys(order).filter((key) => key !== "productSpecs" && JSON.stringify(previousOrder[key]) !== JSON.stringify(order[key])));
  updatedSpecKeys = new Set(Object.keys(currentSpecs).filter((key) => previousSpecs[key] !== currentSpecs[key]));
  previousOrder = structuredClone(order || {});
}

function renderSettings(data) {
  llmSettings = data?.llm || llmSettings;
  const enabled = Boolean(llmSettings.enabled);
  const hasError = enabled && Boolean(llmSettings.lastError);
  const stateText = $("#settings-state");
  if (stateText) stateText.textContent = enabled
    ? (hasError ? `已配置：${llmSettings.model}（上次调用失败，已回退）` : `已启用：${llmSettings.model}`)
    : "当前使用规则模式";
  const url = $("#settings-url");
  const model = $("#settings-model");
  const key = $("#settings-key");
  if (url) url.value = llmSettings.url || "";
  if (model) model.value = llmSettings.model || "";
  if (key) {
    key.value = "";
    key.placeholder = llmSettings.keyConfigured ? "已配置，留空保持不变" : "可选，不填也可使用规则模式";
  }
  const button = $("#settings-button");
  if (button) button.textContent = hasError ? "模型异常" : enabled ? "模型已启用" : "接口设置";
}

function hasValue(value) {
  return value !== undefined && value !== null && String(value).trim() !== "";
}

function getFieldValue(key) {
  if (key !== "platform") return state.order[key] || "";
  const option = [...$("#platform-select").options].find((item) => item.value === state.order.platform);
  return option?.textContent || state.order.platform || "";
}

function getDraftFieldSets() {
  const configured = productDraftFields[state.order.productType] || defaultDraftFields;
  const profileKeys = new Set((state.productProfile?.parameters || []).map((item) => item.key));
  return {
    overview: overviewFields,
    specs: configured.specs.filter((key) => !profileKeys.has(key)),
    process: configured.process.filter((key) => !profileKeys.has(key))
  };
}

function isRequiredField(key) {
  const missing = Array.isArray(state.missingFields) ? state.missingFields : [];
  return missing.includes(labels[key]);
}

function renderFieldList(selector, keys) {
  const list = $(selector);
  list.innerHTML = "";
  keys.forEach((key) => {
    const current = getFieldValue(key);
    const missing = !hasValue(current) && isRequiredField(key);
    const row = document.createElement("div");
    row.className = `field-row ${missing ? "required-missing" : ""} ${updatedOrderKeys.has(key) ? "updated" : ""}`;
    const label = document.createElement("span");
    label.className = "field-label";
    label.textContent = labels[key];
    const value = document.createElement("span");
    value.className = `field-value ${hasValue(current) ? "" : "empty"}`;
    value.textContent = current || (missing ? "待补充" : "未设置");
    row.append(label, value);
    list.appendChild(row);
  });
}

function syncDraftGroups(hasProduct, hasProcessData) {
  const recommendations = (state.options || []).length > 0;
  if (state.order.productType !== renderedProduct) {
    renderedProduct = state.order.productType || "";
    $("[data-draft-group='overview']").open = true;
    $("[data-draft-group='specs']").open = hasProduct;
    $("[data-draft-group='process']").open = hasProcessData;
  } else if (recommendations && !hadRecommendations) {
    $("[data-draft-group='process']").open = true;
  }
  hadRecommendations = recommendations;
  updateDraftToggle();
}

function renderDraft() {
  const profile = state.productProfile || { parameters: [], missing: [] };
  const parameters = profile.parameters || [];
  const fields = getDraftFieldSets();
  renderFieldList("#overview-fields", fields.overview);
  renderFieldList("#spec-fields", fields.specs);
  renderFieldList("#process-fields", fields.process);
  renderParameterForm(profile);

  const overviewFilled = fields.overview.filter((key) => hasValue(getFieldValue(key))).length;
  const specFilled = fields.specs.filter((key) => hasValue(getFieldValue(key))).length + parameters.filter((item) => item.filled).length;
  const specTotal = fields.specs.length + parameters.length;
  const processFilled = fields.process.filter((key) => hasValue(getFieldValue(key))).length;
  $("#overview-meta").textContent = `${overviewFilled} / ${fields.overview.length} 已填写`;
  $("#specs-meta").textContent = state.order.productType ? `${profile.category || "其他印刷品"} · ${specFilled} / ${specTotal}` : "待识别品类";
  $("#process-meta").textContent = `${processFilled} / ${fields.process.length} 已填写`;

  const complete = Array.isArray(state.missingFields)
    ? state.missingFields.length === 0
    : ["productType", "quantity", "size", "paper", "printing", "deadline"].every((key) => state.order[key]);
  const productMissing = profile.missing || [];
  const readyToGenerate = complete && productMissing.length === 0 && state.selectedOption;
  const status = $("#draft-status");
  status.textContent = state.orderGenerated ? "已生成" : !complete ? "待补充" : productMissing.length ? "待确认规格" : state.selectedOption ? "可生成" : "可选方案";
  status.className = `draft-status ${state.orderGenerated ? "confirmed" : productMissing.length ? "attention" : complete ? "ready" : ""}`;
  $("#generate-order").disabled = !readyToGenerate || state.orderGenerated;
  $("#generate-order").textContent = state.orderGenerated ? "订单草稿已生成" : !complete ? "补全信息后生成" : productMissing.length ? "补全规格后生成" : !state.selectedOption ? "选择方案后生成" : "生成订单草稿";
  $("#copy-order").disabled = !state.orderGenerated;
  $("#order-export").hidden = !state.orderGenerated;
  syncDraftGroups(Boolean(state.order.productType), fields.process.some((key) => hasValue(getFieldValue(key))) || Boolean(state.selectedOption));
}

function renderParameterForm(profile) {
  const context = $("#product-context");
  context.hidden = !state.order?.productType;
  $("#product-profile-summary").textContent = profile.summary || "先按通用字段收集订单信息。";
  $("#product-profile-readiness").textContent = `${profile.readiness ?? 100}%`;
  const root = $("#parameter-form");
  root.innerHTML = "";
  if (!state.order?.productType) return;
  const title = document.createElement("p");
  title.className = "parameter-form-title";
  title.textContent = `${profile.category || "产品"}参数`;
  root.appendChild(title);
  (profile.parameters || []).forEach((item) => {
    const field = document.createElement("div");
    field.className = `parameter-field ${!item.filled && item.required ? "missing" : ""}`;
    const label = document.createElement("label");
    label.htmlFor = `parameter-${item.key}`;
    label.innerHTML = `${item.label}${item.required ? ' <span class="required-mark">*</span>' : ""}`;
    const input = document.createElement("input");
    input.id = `parameter-${item.key}`;
    input.value = item.value || "";
    input.placeholder = item.question || item.label;
    input.setAttribute("aria-describedby", `parameter-hint-${item.key}`);
    if (!item.filled && item.required) input.setAttribute("aria-invalid", "true");
    input.addEventListener("change", () => {
      const value = input.value.trim();
      if (!value) {
        field.classList.add("missing");
        input.setAttribute("aria-invalid", "true");
        showToast(`${item.label}是必填参数`);
        return;
      }
      if (value === item.value) return;
      field.classList.remove("missing");
      input.removeAttribute("aria-invalid");
      sendMessage(`更新${item.label}：${value}`, { productSpecs: { [item.key]: value } });
    });
    input.addEventListener("input", () => {
      if (!input.value.trim()) return;
      field.classList.remove("missing");
      input.removeAttribute("aria-invalid");
    });
    const hint = document.createElement("small");
    hint.className = "parameter-hint";
    hint.id = `parameter-hint-${item.key}`;
    hint.textContent = item.hint || (item.required ? "必填参数" : "可选参数");
    field.append(label, input, hint);
    root.appendChild(field);
  });
}

function updateDraftToggle() {
  const groups = [...document.querySelectorAll(".draft-group")];
  const allOpen = groups.every((group) => group.open);
  const button = $("#toggle-draft-groups");
  button.dataset.allOpen = String(allOpen);
  button.setAttribute("aria-label", allOpen ? "收起全部订单分组" : "展开全部订单分组");
  button.title = allOpen ? "收起全部订单分组" : "展开全部订单分组";
}

function renderQuickReplies() {
  const root = $("#quick-replies");
  root.innerHTML = "";
  (state.quickReplies || []).forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "quick-reply";
    button.textContent = item.label;
    button.addEventListener("click", () => sendMessage(item.label, item.data));
    root.appendChild(button);
  });
}

function renderOptions() {
  const root = $("#option-list");
  root.innerHTML = "";
  const recommendations = $("#recommendations");
  const options = state.options || [];
  recommendations.hidden = !options.length;
  $("#recommendation-count").textContent = `${options.length} 个方案`;
  options.forEach((option, index) => {
    const card = document.createElement("article");
    card.className = `option-card ${state.selectedOption === option.id ? "selected" : ""}`;
    card.innerHTML = `${option.score === "综合推荐" || index === 1 ? '<span class="option-badge">推荐</span>' : ""}<div class="option-card-heading"><h4></h4><span class="option-score"></span></div><p></p><dl class="option-grid"></dl><button class="option-details-toggle" type="button">展开推荐理由</button><div class="option-details" hidden></div>`;
    card.querySelector("h4").textContent = option.title;
    card.querySelector(".option-score").textContent = option.score || "";
    card.querySelector("p").textContent = option.description;
    [["成本", option.cost], ["交期", option.lead], ["材料", option.paper], ["工艺", option.finishing], ["装订", option.binding]].forEach(([name, value]) => {
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
    toggle.addEventListener("click", (event) => {
      event.stopPropagation();
      const visible = details.hidden;
      details.hidden = !visible;
      toggle.textContent = visible ? "收起推荐理由" : "展开推荐理由";
    });
    card.addEventListener("click", async () => {
      try { render(await api("/api/choose", { sessionId, optionId: option.id })); }
      catch (error) { addMessage("assistant", apiErrorText(error, "方案选择暂时失败，请重试。")); }
    });
    root.appendChild(card);
  });
  if (options.length && state.stage === "recommend" && !state.orderGenerated) {
    requestAnimationFrame(() => recommendations.closest(".order-section")?.scrollTo({ top: Math.max(0, recommendations.offsetTop - 12), behavior: "smooth" }));
  }
}

function renderFileState() {
  const node = $("#file-state");
  const feedback = fileFeedback || (state.uploadedFile ? { ok: true, fileName: state.uploadedFile, message: "基础检查已通过。" } : null);
  node.hidden = !feedback;
  if (!feedback) return;
  node.className = `file-state ${feedback.ok ? "ok" : "error"}`;
  node.innerHTML = "";
  const name = document.createElement("strong");
  name.textContent = feedback.fileName || "未命名文件";
  const detail = document.createElement("small");
  detail.textContent = feedback.message || "";
  node.append(name, detail);
}

function renderTools() {
  const root = $("#tool-list");
  const tools = (state.availableTools || []).filter((item) => toolLabels[item.name]);
  $("#tool-count").textContent = `${tools.length} 项`;
  root.innerHTML = "";
  tools.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tool-item";
    const code = document.createElement("span");
    code.className = "tool-code";
    code.textContent = item.name === "validate_order" ? "CK" : item.name === "recommend_processes" ? "RF" : item.name === "estimate_price" ? "¥" : item.name === "explain_print_term" ? "IN" : "EX";
    const copy = document.createElement("span");
    copy.innerHTML = `<strong>${toolLabels[item.name][0]}</strong><small>${toolLabels[item.name][1]}</small>`;
    button.append(code, copy);
    button.addEventListener("click", () => callTool(item.name));
    root.appendChild(button);
  });
}

function renderCatalog(categories = []) {
  const root = $("#catalog-list");
  root.innerHTML = "";
  categories.forEach((category) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "catalog-chip";
    button.textContent = category;
    button.title = categoryExamples[category] || category;
    button.addEventListener("click", () => sendMessage(categoryExamples[category] || `我想做${category}`));
    root.appendChild(button);
  });
}

function renderProgress() {
  const required = ["productType", "quantity", "size", "paper", "printing", "deadline"];
  const complete = Array.isArray(state.missingFields)
    ? required.length - state.missingFields.length
    : required.filter((key) => state.order[key]).length;
  const percent = state.stage === "confirm" ? 100 : state.stage === "recommend" ? 66 : Math.round((complete / required.length) * 55);
  $("#progress-stage").textContent = ({ collect: "需求收集", recommend: "方案选择", confirm: "确认订单" })[state.stage] || "需求收集";
  $("#progress-bar").style.width = `${percent}%`;
  $("#progress-percent").textContent = `${percent}%`;
  const order = ["collect", "recommend", "confirm"];
  document.querySelectorAll(".step").forEach((step) => {
    step.classList.toggle("active", step.dataset.step === state.stage);
    step.classList.toggle("done", order.indexOf(step.dataset.step) < order.indexOf(state.stage));
  });
}

async function sendMessage(text, patch) {
  if (!text?.trim() || isSending) return;
  const requestSeq = ++chatRequestSeq;
  const controller = new AbortController();
  activeChatController?.abort();
  activeChatController = controller;
  isSending = true;
  addMessage("user", text);
  $("#message-input").value = "";
  setComposerBusy(true);
  pendingMessage = addPendingMessage();
  try {
    const request = api("/api/chat", { sessionId, text, patch }, "POST", { signal: controller.signal });
    const [result] = await Promise.all([request, new Promise((resolve) => setTimeout(resolve, 220))]);
    if (requestSeq !== chatRequestSeq) return;
    removePendingMessage();
    render(result);
  } catch (error) {
    if (error?.name === "AbortError" || requestSeq !== chatRequestSeq) return;
    removePendingMessage();
    const suffix = error?.requestId ? `（请求号 ${error.requestId}）` : "";
    addMessage("assistant", `${error?.message || "Agent 暂时无法连接，请确认本地服务已启动。"}${suffix}`);
    $("#agent-status").innerHTML = '<span class="status-dot offline"></span>连接失败';
  }
  finally {
    if (requestSeq !== chatRequestSeq) return;
    activeChatController = null;
    isSending = false;
    setComposerBusy(false);
    $("#message-input").focus();
  }
}

async function callTool(toolName, payload = {}) {
  try {
    if (toolName === "explain_print_term" && !payload.question) {
      payload = { question: state.order.productType ? `${state.order.productType}的纸张和工艺怎么选？` : "纸张和印刷工艺怎么选？" };
    }
    const result = await api("/api/tools/call", { sessionId, toolName, payload });
    render(result);
    showToast(`已调用：${toolLabels[toolName]?.[0] || toolName}`);
  } catch (error) {
    addMessage("assistant", apiErrorText(error, "工具调用失败，请确认 Agent 服务已启动，或先刷新会话。"));
  }
}

$("#message-form").addEventListener("submit", (event) => { event.preventDefault(); sendMessage($("#message-input").value); });
$("#message-input").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#message-form").requestSubmit(); } });
$("#generate-order").addEventListener("click", async () => { try { render(await api("/api/generate", { sessionId })); } catch (error) { addMessage("assistant", apiErrorText(error, "订单生成失败，请重试。")); } });
$("#copy-order").addEventListener("click", async () => {
  const groups = getOrderGroups();
  const lines = groups.filter(([, items]) => items.length).flatMap(([title, items]) => [`【${title}】`, ...items.map(([label, value]) => `${label}：${value}`), ""]);
  try { await navigator.clipboard.writeText(lines.join("\n")); showToast("订单信息已复制"); } catch { showToast("当前浏览器不支持自动复制"); }
});

function getOrderGroups() {
  const fields = getDraftFieldSets();
  return [
    ["订单概览", fields.overview.map((key) => [labels[key], getFieldValue(key)]).filter(([, value]) => hasValue(value))],
    ["产品规格", [
      ...fields.specs.map((key) => [labels[key], getFieldValue(key)]).filter(([, value]) => hasValue(value)),
      ...(state.productProfile?.parameters || []).map((item) => [item.label, item.value || (item.required ? "待确认" : "")]).filter(([, value]) => hasValue(value))
    ]],
    ["生产工艺", fields.process.map((key) => [labels[key], getFieldValue(key)]).filter(([, value]) => hasValue(value))]
  ];
}

function exportOrder(format) {
  const groups = getOrderGroups();
  const rows = groups.flatMap(([section, items]) => items.map(([label, value]) => ({ section, label, value })));
  const fileBase = `printops-order-${sessionId || "draft"}`;
  let content = "";
  let contentType = "text/plain;charset=utf-8";
  let extension = "txt";
  if (format === "json") {
    content = JSON.stringify({
      exportedAt: new Date().toISOString(),
      sessionId,
      platform: state.order.platform || "generic",
      stage: state.stage,
      selectedOption: state.selectedOption,
      order: state.order,
      productProfile: state.productProfile,
      fields: rows
    }, null, 2);
    contentType = "application/json;charset=utf-8";
    extension = "json";
  } else if (format === "csv") {
    const escapeCsv = (value) => `"${String(value).replaceAll('"', '""')}"`;
    content = ["Section,Field,Value", ...rows.map((row) => [row.section, row.label, row.value].map(escapeCsv).join(","))].join("\n");
    contentType = "text/csv;charset=utf-8";
    extension = "csv";
  } else {
    content = ["# PrintOps 订单", ...groups.filter(([, items]) => items.length).flatMap(([title, items]) => [`## ${title}`, ...items.map(([label, value]) => `- **${label}**：${value}`), ""])].join("\n");
    contentType = "text/markdown;charset=utf-8";
    extension = "md";
  }
  const url = URL.createObjectURL(new Blob([`\ufeff${content}`], { type: contentType }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `${fileBase}.${extension}`;
  link.click();
  URL.revokeObjectURL(url);
  showToast(`订单已导出为 ${extension.toUpperCase()}`);
}

document.querySelectorAll(".export-option").forEach((button) => button.addEventListener("click", () => exportOrder(button.dataset.format)));
$("#toggle-draft-groups").addEventListener("click", () => {
  const groups = [...document.querySelectorAll(".draft-group")];
  const open = !groups.every((group) => group.open);
  groups.forEach((group) => { group.open = open; });
  updateDraftToggle();
});
document.querySelectorAll(".draft-group").forEach((group) => group.addEventListener("toggle", updateDraftToggle));
async function uploadFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    fileFeedback = { ok: false, fileName: file.name, message: "MVP 暂只支持 PDF 文件。" };
    addMessage("user", `上传文件：${file.name}`);
    addMessage("assistant", fileFeedback.message);
    renderFileState();
    return;
  }
  addMessage("user", `上传文件：${file.name}`);
  try { const data = await api("/api/preflight", { sessionId, fileName: file.name, sizeBytes: file.size }); render(data); }
  catch (error) { addMessage("assistant", apiErrorText(error, "文件预检调用失败，请重试。")); }
}
$("#upload-button").addEventListener("click", () => $("#file-input").click());
$("#drop-zone").addEventListener("click", () => $("#file-input").click());
$("#file-input").addEventListener("change", async () => {
  await uploadFile($("#file-input").files[0]);
  $("#file-input").value = "";
});
["dragenter", "dragover"].forEach((type) => $("#drop-zone").addEventListener(type, (event) => {
  event.preventDefault();
  $("#drop-zone").classList.add("is-dragging");
}));
["dragleave", "drop"].forEach((type) => $("#drop-zone").addEventListener(type, () => $("#drop-zone").classList.remove("is-dragging")));
$("#drop-zone").addEventListener("drop", (event) => {
  event.preventDefault();
  uploadFile(event.dataTransfer?.files?.[0]);
});
$("#platform-select").addEventListener("change", async (event) => { try { render(await api("/api/platform", { sessionId, platformId: event.target.value })); } catch (error) { showToast(apiErrorText(error, "平台切换失败")); } });
$("#sidebar-toggle").addEventListener("click", () => setSidebarCollapsed(!sidebarCollapsed));
$("#theme-toggle").addEventListener("click", () => setHighContrast(document.documentElement.dataset.theme !== "high-contrast"));
$("#reset-order").addEventListener("click", async () => {
  chatRequestSeq += 1;
  activeChatController?.abort();
  activeChatController = null;
  removePendingMessage();
  isSending = false;
  setComposerBusy(false);
  localStorage.removeItem("printops_session");
  $("#chat-feed").innerHTML = "";
  sessionId = "";
  suppressOrderDiff = true;
  fileFeedback = null;
  await bootstrap();
});

$("#settings-test").addEventListener("click", async () => {
  const button = $("#settings-test");
  button.disabled = true;
  button.textContent = "测试中…";
  try {
    const result = await api("/api/model/test", {
      url: $("#settings-url").value.trim(), model: $("#settings-model").value.trim(), key: $("#settings-key").value
    });
    const stateText = $("#settings-state");
    if (stateText) stateText.textContent = result.test?.ok
      ? `连接正常 · ${result.test.latencyMs}ms（尚未保存）`
      : result.test?.message || "模型连接失败";
    showToast(result.test?.ok ? `连接正常 · ${result.test.latencyMs}ms` : result.test?.message || "模型连接失败");
  } catch (error) {
    showToast(error.message || "模型连接测试失败");
  } finally {
    button.disabled = false;
    button.textContent = "测试连接";
  }
});

$("#settings-button").addEventListener("click", () => {
  renderSettings({ llm: llmSettings });
  $("#settings-dialog").showModal();
});
$("#settings-cancel").addEventListener("click", () => $("#settings-dialog").close());
$("#settings-cancel-secondary").addEventListener("click", () => $("#settings-dialog").close());
$("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const save = $("#settings-save");
  save.disabled = true;
  try {
    const result = await api("/api/settings", {
      url: $("#settings-url").value.trim(), model: $("#settings-model").value.trim(), key: $("#settings-key").value
    });
    renderSettings(result);
    $("#settings-dialog").close();
    showToast(result.llm.enabled ? `已启用模型：${result.llm.model}` : "已切换为规则模式");
  } catch (error) {
    showToast(error.message.includes("URL 和模型名") ? "URL 和模型名需要同时填写" : error.message.includes("接口 URL") ? "接口 URL 格式不正确" : "设置保存失败，请检查接口地址");
  } finally {
    save.disabled = false;
  }
});
$("#settings-clear").addEventListener("click", async () => {
  const clear = $("#settings-clear");
  clear.disabled = true;
  try {
    const result = await api("/api/settings", { clear: true });
    renderSettings(result);
    $("#settings-dialog").close();
    showToast("已清空模型配置，继续使用规则模式");
  } catch { showToast("配置清空失败，请重试"); }
  finally { clear.disabled = false; }
});

function showToast(text) { const node = $("#toast"); node.textContent = text; node.classList.add("show"); setTimeout(() => node.classList.remove("show"), 2200); }

async function bootstrap() {
  try {
    const [platforms, products, tools, settings] = await Promise.all([
      api("/api/platforms", undefined, "GET"), api("/api/products", undefined, "GET"),
      api("/api/tools", undefined, "GET"), api("/api/settings", undefined, "GET")
    ]);
    $("#platform-select").innerHTML = platforms.platforms.map((item) => `<option value="${item.id}">${item.name}</option>`).join("");
    renderCatalog(products.categories || []);
    renderSettings(settings);
    const snapshot = await api("/api/session", sessionId ? { sessionId } : {});
    const hasMemory = snapshot.order?.productType || snapshot.order?.quantity;
    const history = Array.isArray(snapshot.history) ? snapshot.history : [];
    render({ ...snapshot, availableTools: tools.tools }, false);
    $("#platform-select").value = snapshot.order?.platform || "generic";
    if (history.length) {
      addMessage("assistant", "已恢复上次订单记忆。你可以继续补充、修改，或切换目标平台。");
      history.forEach((message) => addMessage(message.role === "user" ? "user" : "assistant", message.text || ""));
    }
    else addMessage("assistant", "你好，我会把你的想法整理成印刷订单。直接说用途、数量、效果或预算即可。");
    if (!hasMemory) render({ quickReplies: [{ label: "做一本宣传册", data: { productType: "宣传册" } }, { label: "做一批名片", data: { productType: "名片" } }, { label: "做一批包装盒", data: { productType: "包装盒" } }] }, false);
  } catch {
    $("#agent-status").innerHTML = '<span class="status-dot offline"></span>服务未启动';
    addMessage("assistant", "请在项目目录运行 python3 server.py，再刷新此页面。");
  }
}

bootstrap();
setSidebarCollapsed(sidebarCollapsed);
setHighContrast(localStorage.getItem("printops_theme") === "high-contrast");
