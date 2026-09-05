const $ = (selector) => document.querySelector(selector);
const labels = {
  productType: "印刷品", purpose: "使用场景", quantity: "数量", size: "成品尺寸",
  pages: "页数", orientation: "版式方向", paper: "纸张/材料", printing: "印刷颜色", finishing: "表面工艺", binding: "装订/后道",
  deadline: "交期", budget: "预算偏好", platform: "目标平台"
};
const dimensionLabels = {
  finishedSize: "成品尺寸", expandedSize: "展开尺寸", dieCutSize: "刀模尺寸", packageSize: "包装三维尺寸"
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
  request_supplier_quote: ["准备询价", "生成待人工确认的询价请求"],
  match_supplier_capability: ["匹配供应商", "检查平台能力与待确认项"],
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
let state = { order: {}, stage: "collect", workflowStage: "collect", quickReplies: [], options: [], selectedOption: null, activeItemSelectedOption: null, orderGenerated: false, handoff: null, confirmation: { status: "not_ready" }, quoteRequest: null, quoteRequests: [], activeQuoteRequestId: null, toolTrace: [], runTrace: [], availableTools: [], productProfile: null, fieldMeta: {}, conflicts: [], activeItemIndex: null };
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
let pdfPreview = null;
const orderHistoryKey = "printops_order_history";
const specPresetKey = "printops_spec_presets";
const layoutKey = "printops_layout";
const LAYOUT_DEFAULTS = { sidebarW: 204, orderW: 335, orderCollapsed: false };
const SIDEBAR_RANGE = [180, 320];
const ORDER_RANGE = [300, 480];
const DESKTOP_QUERY = window.matchMedia("(min-width: 1051px)");

function loadLayout() {
  try {
    const stored = JSON.parse(localStorage.getItem(layoutKey) || "{}");
    return {
      sidebarW: clampLayoutWidth(Number(stored.sidebarW) || LAYOUT_DEFAULTS.sidebarW, SIDEBAR_RANGE),
      orderW: clampLayoutWidth(Number(stored.orderW) || LAYOUT_DEFAULTS.orderW, ORDER_RANGE),
      orderCollapsed: Boolean(stored.orderCollapsed)
    };
  } catch {
    return { ...LAYOUT_DEFAULTS };
  }
}

function clampLayoutWidth(value, [min, max]) {
  return Math.min(max, Math.max(min, Math.round(value)));
}

function saveLayout() {
  try {
    localStorage.setItem(layoutKey, JSON.stringify({ sidebarW: layout.sidebarW, orderW: layout.orderW, orderCollapsed: layout.orderCollapsed }));
  } catch { /* 布局是增强项，存储失败不影响使用 */ }
}

function applyLayout() {
  const workspace = $("#workspace");
  workspace.style.setProperty("--sidebar-w", `${layout.sidebarW}px`);
  workspace.style.setProperty("--order-w", `${layout.orderW}px`);
  workspace.classList.toggle("order-collapsed", layout.orderCollapsed);
  const orderToggle = $("#order-toggle");
  if (orderToggle) {
    const collapsed = layout.orderCollapsed;
    orderToggle.setAttribute("aria-label", collapsed ? "展开订单栏" : "收起订单栏");
    orderToggle.title = collapsed ? "展开订单栏" : "收起订单栏";
    orderToggle.dataset.collapsed = String(collapsed);
  }
  updateBackdrop();
}

function resetPaneWidth(pane) {
  if (pane === "sidebar") layout.sidebarW = LAYOUT_DEFAULTS.sidebarW;
  if (pane === "order") layout.orderW = LAYOUT_DEFAULTS.orderW;
  applyLayout();
  saveLayout();
  showToast("栏宽已重置");
}

function setupSplitters() {
  document.querySelectorAll(".pane-splitter").forEach((splitter) => {
    const pane = splitter.dataset.pane;
    splitter.addEventListener("pointerdown", (event) => {
      if (!DESKTOP_QUERY.matches || (pane === "sidebar" && sidebarCollapsed)) return;
      event.preventDefault();
      splitter.setPointerCapture(event.pointerId);
      splitter.classList.add("is-dragging");
      document.body.classList.add("is-resizing");
      const workspaceRect = $("#workspace").getBoundingClientRect();
      const onMove = (moveEvent) => {
        if (pane === "sidebar") {
          layout.sidebarW = clampLayoutWidth(moveEvent.clientX - workspaceRect.left, SIDEBAR_RANGE);
          $("#workspace").style.setProperty("--sidebar-w", `${layout.sidebarW}px`);
        } else {
          layout.orderW = clampLayoutWidth(workspaceRect.right - moveEvent.clientX, ORDER_RANGE);
          $("#workspace").style.setProperty("--order-w", `${layout.orderW}px`);
        }
      };
      const finish = () => {
        splitter.classList.remove("is-dragging");
        document.body.classList.remove("is-resizing");
        splitter.removeEventListener("pointermove", onMove);
        splitter.removeEventListener("pointerup", finish);
        splitter.removeEventListener("pointercancel", finish);
        saveLayout();
      };
      splitter.addEventListener("pointermove", onMove);
      splitter.addEventListener("pointerup", finish);
      splitter.addEventListener("pointercancel", finish);
    });
    splitter.addEventListener("dblclick", () => resetPaneWidth(pane));
    splitter.addEventListener("keydown", (event) => {
      const step = event.key === "ArrowRight" ? 16 : event.key === "ArrowLeft" ? -16 : 0;
      if (!step) return;
      event.preventDefault();
      if (pane === "sidebar") {
        if (sidebarCollapsed) return;
        layout.sidebarW = clampLayoutWidth(layout.sidebarW + step, SIDEBAR_RANGE);
      } else {
        layout.orderW = clampLayoutWidth(layout.orderW + step, ORDER_RANGE);
      }
      applyLayout();
      saveLayout();
    });
  });
}

function updateBackdrop() {
  const backdrop = $("#workspace-backdrop");
  if (!backdrop) return;
  const drawerOpen = !DESKTOP_QUERY.matches && !sidebarCollapsed;
  backdrop.hidden = !drawerOpen;
  document.body.classList.toggle("drawer-open", drawerOpen);
}

let layout = loadLayout();

function cloneData(value) {
  if (typeof structuredClone === "function") return structuredClone(value);
  return JSON.parse(JSON.stringify(value));
}

function loadOrderHistory() {
  try {
    const history = JSON.parse(localStorage.getItem(orderHistoryKey) || "[]");
    return Array.isArray(history) ? history.filter((item) => item?.id && item.order) : [];
  } catch {
    return [];
  }
}

function saveOrderHistory(history) {
  try { localStorage.setItem(orderHistoryKey, JSON.stringify(history.slice(0, 8))); }
  catch { showToast("订单历史保存空间已满，请清理浏览器数据"); }
}

function orderHasContent(order = {}) {
  return Boolean(order.productType || order.quantity || order.orderGenerated);
}

function orderHistoryTitle(order = {}) {
  if (Array.isArray(order.items) && order.items.length > 1) {
    const summary = order.items.map((item) => `${item.productType || "产品"}${item.quantity ? ` ${item.quantity}` : ""}`).join("；");
    return `${order.items.map((item) => item.productType || "产品").join("+")} · ${summary}`;
  }
  return order.productType || order.quantity ? `${order.productType || "未分类"} · ${order.quantity || "待定数量"}` : "订单草稿";
}

function formatOrderHistoryTime(timestamp) {
  return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(timestamp));
}

function rememberOrderHistory() {
  if (!sessionId || !orderHasContent(state.order)) return;
  const history = loadOrderHistory();
  const record = {
    id: sessionId,
    order: cloneData(state.order || {}),
    stage: state.stage || "collect",
    orderGenerated: Boolean(state.orderGenerated),
    updatedAt: Date.now()
  };
  const existingIndex = history.findIndex((item) => item.id === sessionId);
  if (existingIndex >= 0) history.splice(existingIndex, 1);
  saveOrderHistory([record, ...history]);
}

function renderOrderHistory() {
  const root = $("#history-list");
  const history = loadOrderHistory();
  $("#history-count").textContent = `${history.length} 单`;
  root.innerHTML = "";
  if (!history.length) {
    const empty = document.createElement("p");
    empty.className = "history-empty";
    empty.textContent = "开始填写后会自动记录。";
    root.append(empty);
    return;
  }
  history.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-item";
    button.dataset.sessionId = item.id;
    button.setAttribute("aria-current", String(item.id === sessionId));
    const title = document.createElement("strong");
    title.textContent = orderHistoryTitle(item.order);
    const meta = document.createElement("small");
    meta.textContent = `${item.orderGenerated ? "已生成" : item.order?.selectedOption ? "已选方案" : item.stage === "recommend" ? "待选方案" : "编辑中"} · ${formatOrderHistoryTime(item.updatedAt)}`;
    button.append(title, meta);
    root.append(button);
  });
}

function loadSpecPresets() {
  try {
    const presets = JSON.parse(localStorage.getItem(specPresetKey) || "[]");
    return Array.isArray(presets) ? presets.filter((item) => item?.id && item.fields) : [];
  } catch {
    return [];
  }
}

function saveSpecPresets(presets) {
  try { localStorage.setItem(specPresetKey, JSON.stringify(presets.slice(0, 8))); }
  catch { showToast("常用规格保存空间已满，请清理浏览器数据"); }
}

function currentSpecPresetFields(order = {}) {
  return {
    paper: order.paper || "",
    printing: order.printing || "",
    finishing: order.finishing || "",
    binding: order.binding || ""
  };
}

function specPresetHasContent(fields = {}) {
  return Object.values(fields).some((value) => hasValue(value));
}

function specPresetTitle(fields = {}) {
  const parts = ["paper", "printing", "finishing", "binding"].map((key) => fields[key]).filter(Boolean);
  return parts.length ? parts.join(" · ") : "未命名规格";
}

function renderSpecPresets() {
  const root = $("#preset-list");
  const presets = loadSpecPresets();
  $("#preset-count").textContent = `${presets.length} 组`;
  $("#save-preset").disabled = !specPresetHasContent(currentSpecPresetFields(state.order));
  root.innerHTML = "";
  if (!presets.length) {
    const empty = document.createElement("p");
    empty.className = "preset-empty";
    empty.textContent = "保存后可快速复用。";
    root.append(empty);
    return;
  }
  presets.forEach((item) => {
    const row = document.createElement("div");
    row.className = "preset-item";
    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "preset-apply";
    apply.dataset.presetId = item.id;
    const title = document.createElement("strong");
    title.textContent = specPresetTitle(item.fields);
    const meta = document.createElement("small");
    meta.textContent = formatOrderHistoryTime(item.createdAt);
    apply.append(title, meta);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "preset-remove";
    remove.dataset.presetId = item.id;
    remove.setAttribute("aria-label", `删除规格 ${specPresetTitle(item.fields)}`);
    remove.textContent = "×";
    row.append(apply, remove);
    root.append(row);
  });
}

function saveCurrentSpecPreset() {
  const fields = currentSpecPresetFields(state.order);
  if (!specPresetHasContent(fields)) {
    showToast("请先补充纸张、颜色、工艺或装订");
    return;
  }
  const presets = loadSpecPresets();
  const signature = JSON.stringify(fields);
  const existingIndex = presets.findIndex((item) => JSON.stringify(item.fields) === signature);
  if (existingIndex >= 0) presets.splice(existingIndex, 1);
  presets.unshift({ id: crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`, fields, createdAt: Date.now() });
  saveSpecPresets(presets);
  renderSpecPresets();
  showToast("常用规格已保存");
}

async function applySpecPreset(presetId) {
  const preset = loadSpecPresets().find((item) => item.id === presetId);
  if (!preset || isSending) return;
  const patch = Object.fromEntries(Object.entries(preset.fields).filter(([, value]) => hasValue(value)));
  if (!Object.keys(patch).length) return;
  await sendMessage("沿用常用规格", patch);
}

function removeSpecPreset(presetId) {
  saveSpecPresets(loadSpecPresets().filter((item) => item.id !== presetId));
  renderSpecPresets();
  showToast("常用规格已删除");
}

async function switchOrder(orderId) {
  if (!orderId || orderId === sessionId || isSending) return;
  setComposerBusy(true);
  try {
    const snapshot = await api("/api/session", { sessionId: orderId });
    $("#chat-feed").innerHTML = "";
    fileFeedback = null;
    clearPdfPreview();
    suppressOrderDiff = true;
    state = { order: {}, stage: "collect", quickReplies: [], options: [], selectedOption: null, activeItemSelectedOption: null, orderGenerated: false, handoff: null, confirmation: { status: "not_ready" }, quoteRequest: null, quoteRequests: [], activeQuoteRequestId: null, toolTrace: [], availableTools: state.availableTools || [], productProfile: null, activeItemIndex: null };
    render(snapshot, false);
    $("#platform-select").value = snapshot.order?.platform || "generic";
    const messages = Array.isArray(snapshot.history) ? snapshot.history : [];
    if (messages.length) {
      addMessage("assistant", `已切换到订单 ${orderHistoryTitle(snapshot.order)}。`);
      messages.forEach((message) => addMessage(message.role === "user" ? "user" : "assistant", message.text || ""));
    } else addMessage("assistant", "已切换到这个订单。当前没有保存的对话内容，可以直接继续补充。");
    showToast("订单已切换");
  } catch (error) {
    showToast(apiErrorText(error, "订单切换失败，请重试"), "error");
  } finally {
    setComposerBusy(false);
  }
}

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

function clearPdfPreview() {
  if (pdfPreview?.url) URL.revokeObjectURL(pdfPreview.url);
  pdfPreview = null;
}

function formatFileSize(sizeBytes) {
  if (!Number.isFinite(sizeBytes) || sizeBytes < 0) return "未知大小";
  if (sizeBytes < 1024) return `${sizeBytes} B`;
  if (sizeBytes < 1024 * 1024) return `${(sizeBytes / 1024).toFixed(1)} KB`;
  return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatFileTime(timestamp) {
  if (!timestamp) return "未知时间";
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" }).format(new Date(timestamp));
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
  updateBackdrop();
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

function isFeedNearBottom() {
  const feed = $("#chat-feed");
  return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
}

function scrollFeedToBottom() {
  const feed = $("#chat-feed");
  feed.scrollTop = feed.scrollHeight;
}

function addMessage(role, text) {
  const stick = isFeedNearBottom();
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
  meta.textContent = `${role === "assistant" ? "印刷订单智能体" : "我"} · ${new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date())}`;
  content.append(bubble, meta);
  message.append(avatar, content);
  $("#chat-feed").appendChild(message);
  if (stick) scrollFeedToBottom();
}

function addPendingMessage() {
  const message = document.createElement("div");
  message.className = "message assistant pending-message";
  message.setAttribute("role", "status");
  const stick = isFeedNearBottom();
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
  if (stick) scrollFeedToBottom();
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
  const confirm = $("#confirm-order");
  const quoteRefresh = $("#quote-refresh");
  const quoteCancel = $("#quote-cancel");
  const label = send?.querySelector(".send-label");
  if (input) input.disabled = busy;
  if (send) {
    send.disabled = busy;
    send.classList.toggle("is-loading", busy);
    send.setAttribute("aria-busy", String(busy));
  }
  if (label) label.textContent = busy ? "处理中" : "发送";
  if (upload) upload.disabled = busy;
  if (confirm && busy) confirm.disabled = true;
  if (quoteRefresh) quoteRefresh.disabled = busy;
  if (quoteCancel && busy) quoteCancel.disabled = true;
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
  renderValidation();
  renderFileState();
  renderQuickReplies();
  renderOptions();
  renderTools();
  renderDecision();
  renderQuoteStatus();
  renderRunTrace();
  renderProgress();
  rememberOrderHistory();
  renderOrderHistory();
  renderSpecPresets();
  $("#agent-trace").textContent = data.toolTrace?.length ? data.toolTrace.join("  /  ") : "等待输入";
  $("#memory-status").textContent = data.nextAction || (data.stage === "confirm" ? "订单记忆已锁定，等待人工确认。" : data.order?.productType ? "订单记忆已保存，可继续补充或修改。" : "等待第一条需求，Agent 将自动建立订单记忆。");
  $("#agent-status").innerHTML = '<span class="status-dot"></span>智能体在线';
}

function trackOrderChanges(order) {
  if (suppressOrderDiff) {
    previousOrder = cloneData(order || {});
    updatedOrderKeys = new Set();
    updatedSpecKeys = new Set();
    suppressOrderDiff = false;
    return;
  }
  const previousSpecs = previousOrder.productSpecs || {};
  const currentSpecs = order.productSpecs || {};
  updatedOrderKeys = new Set(Object.keys(order).filter((key) => key !== "productSpecs" && JSON.stringify(previousOrder[key]) !== JSON.stringify(order[key])));
  updatedSpecKeys = new Set(Object.keys(currentSpecs).filter((key) => previousSpecs[key] !== currentSpecs[key]));
  previousOrder = cloneData(order || {});
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
  if (button) button.textContent = hasError ? "接口异常" : enabled ? "接口已启用" : "接口设置";
}

function hasValue(value) {
  return value !== undefined && value !== null && String(value).trim() !== "";
}

function getFieldValue(key) {
  const items = Array.isArray(state.order?.items) ? state.order.items.filter((item) => item && typeof item === "object") : [];
  if (items.length > 1) {
    if (key === "productType") return items.map((item) => item.productType || "未分类").join(" + ");
    if (key === "quantity") return items.map((item) => `${item.productType || "产品"}${item.quantity ? ` ${item.quantity}` : " 待定数量"}`).join("；");
    if (key === "size") {
      const sizes = [...new Set(items.map((item) => item.size).filter(hasValue))];
      return sizes.length === 1 ? sizes[0] : sizes.length > 1 ? "各产品尺寸不同（见产品项）" : "各产品分别确认";
    }
  }
  if (key !== "platform") return state.order[key] || "";
  const option = [...$("#platform-select").options].find((item) => item.value === state.order.platform);
  return option?.textContent || state.order.platform || "";
}

function dimensionRows(order = {}) {
  const dimensions = order.dimensions && typeof order.dimensions === "object" ? order.dimensions : {};
  return Object.entries(dimensionLabels)
    .map(([key, label]) => [key, label, dimensions[key]])
    .filter(([, , value]) => hasValue(value));
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

function fieldProvenance(key) {
  return state.fieldMeta?.[key] || null;
}

function renderFieldList(selector, keys, editable = false) {
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
    const meta = fieldProvenance(key);
    const valueWrap = document.createElement("span");
    valueWrap.className = "field-value-wrap";
    let valueNode;
    if (editable && key !== "platform") {
      valueNode = document.createElement("input");
      valueNode.type = "text";
      valueNode.className = `field-input ${hasValue(current) ? "" : "empty"}`;
      valueNode.value = current || "";
      valueNode.placeholder = missing ? "待补充" : "未设置";
      valueNode.setAttribute("aria-label", labels[key]);
      valueNode.addEventListener("change", () => commitFieldEdit(key, valueNode));
    } else {
      valueNode = document.createElement("span");
      valueNode.className = `field-value ${hasValue(current) ? "" : "empty"}`;
      valueNode.textContent = current || (missing ? "待补充" : "未设置");
    }
    const source = document.createElement("small");
    if (meta) {
      const confidence = Math.round(Number(meta.confidence || 0) * 100);
      source.className = `field-source ${confidence < 75 ? "low-confidence" : ""}`;
      source.textContent = `${meta.sourceLabel || meta.source || "已记录"} · ${confidence}%`;
      source.title = confidence < 75 ? "该字段置信度较低，生成订单前请人工确认" : "字段来源与置信度";
    } else {
      source.className = "field-source empty";
      source.textContent = "尚无来源记录";
    }
    valueWrap.append(valueNode, source);
    row.append(label, valueWrap);
    list.appendChild(row);
  });
}

function commitFieldEdit(key, input) {
  const value = input.value.trim();
  const current = getFieldValue(key);
  if (value === (current || "")) return;
  if (!value) {
    input.value = current || "";
    showToast("暂不支持直接清空，请在对话中说明，例如「不要覆膜」或「数量改成 800」", "error");
    return;
  }
  if (isSending) {
    input.value = current || "";
    return;
  }
  sendMessage(`更新${labels[key]}：${value}`, { [key]: value });
}

function renderDimensionRows(selector, order = state.order) {
  const list = $(selector);
  if (!list) return;
  dimensionRows(order).forEach(([key, label, current]) => {
    if (key === "finishedSize" && current === order.size) return;
    const row = document.createElement("div");
    row.className = "field-row dimension-row";
    const labelNode = document.createElement("span");
    labelNode.className = "field-label";
    labelNode.textContent = label;
    const valueNode = document.createElement("span");
    valueNode.className = "field-value";
    valueNode.textContent = current;
    const meta = fieldProvenance(`dimensions.${key}`);
    const valueWrap = document.createElement("span");
    valueWrap.className = "field-value-wrap";
    const source = document.createElement("small");
    source.className = meta && Number(meta.confidence) < 0.75 ? "field-source low-confidence" : "field-source";
    source.textContent = meta
      ? `${meta.sourceLabel || meta.source || "已记录"} · ${Math.round(Number(meta.confidence || 0) * 100)}%`
      : "尺寸语义已分开记录";
    valueWrap.append(valueNode, source);
    row.append(labelNode, valueWrap);
    list.appendChild(row);
  });
}

async function updateMultiItemField(index, key, value, kind = "base") {
  const normalized = String(value || "").trim();
  if (!normalized || isSending) return;
  isSending = true;
  setComposerBusy(true);
  try {
    const patch = kind === "spec" ? { productSpecs: { [key]: normalized } }
      : kind === "dimension" ? { dimensions: { [key]: normalized } } : { [key]: normalized };
    render(await api("/api/chat", { sessionId, itemIndex: index, text: `更新第 ${index + 1} 项`, patch }));
  } catch (error) {
    addMessage("assistant", apiErrorText(error, "产品项更新失败，请重试。"));
  } finally {
    isSending = false;
    setComposerBusy(false);
  }
}

function renderMultiProductItems() {
  const panel = $("#multi-product-panel");
  const root = $("#multi-product-list");
  if (!panel || !root) return;
  const items = Array.isArray(state.order?.items) ? state.order.items.filter((item) => item && typeof item === "object") : [];
  panel.hidden = items.length < 2;
  root.innerHTML = "";
  if (items.length < 2) return;
  $("#multi-product-count").textContent = `${items.length} 项`;
  const itemFields = [
    ["quantity", "数量"], ["pages", "页数"], ["paper", "材料"],
    ["printing", "颜色"], ["finishing", "表面工艺"], ["binding", "后道/装订"], ["deadline", "交期"], ["uploadedFile", "文件"]
  ];
  const specLabels = {
    cardStock: "名片材质", cardCorners: "圆角", folding: "折页方式", bleed: "出血",
    labelMaterial: "标签面材", labelShape: "标签形状", adhesive: "胶水类型", boxSize: "盒体尺寸",
    boxSizeInner: "内尺寸", boxSizeOuter: "外尺寸", boxStructure: "盒型结构", dieCut: "刀模文件", bagSize: "袋体尺寸", bagMaterial: "袋体材料",
    handle: "提手方式", cupVolume: "容量", cupMaterial: "杯身材料", innerCoating: "内淋膜",
    displayMaterial: "展示介质", install: "安装加工", hangHole: "挂孔", string: "穿绳/配件"
  };
  const validationByIndex = new Map((state.validation?.itemValidations || []).map((item) => [item.index, item]));
  items.forEach((item, index) => {
    const itemValidation = validationByIndex.get(index);
    const details = document.createElement("details");
    details.className = `multi-product-item ${state.activeItemIndex === index ? "active" : ""}`;
    details.open = state.activeItemIndex === index || index === 0;
    const summary = document.createElement("summary");
    const heading = document.createElement("span");
    heading.className = "multi-product-item-heading";
    const indexLabel = document.createElement("span");
    indexLabel.className = "multi-product-item-index";
    indexLabel.textContent = String(index + 1).padStart(2, "0");
    const productName = document.createElement("strong");
    productName.textContent = item.productType || `产品项 ${index + 1}`;
    heading.append(indexLabel, productName);
    const status = document.createElement("span");
    status.className = `multi-product-status ${itemValidation?.ok ? "ready" : "attention"}`;
    if (state.activeItemIndex === index) status.textContent = "当前处理";
    else if (item.selectedOption) status.textContent = "已选方案";
    else if (itemValidation?.ok) status.textContent = "基础字段已齐";
    else {
      const missing = [...(itemValidation?.missing || []), ...(itemValidation?.productMissing || [])];
      status.textContent = missing.length ? `待补 ${missing[0]}` : "待逐项确认";
    }
    const chevron = document.createElement("span");
    chevron.className = "multi-product-chevron";
    summary.append(heading, status, chevron);
    details.appendChild(summary);

    const body = document.createElement("div");
    body.className = "multi-product-item-body";
    const fields = document.createElement("dl");
    fields.className = "multi-product-fields";
    itemFields.forEach(([key, label]) => {
      if (!hasValue(item[key])) return;
      const row = document.createElement("div");
      const term = document.createElement("dt");
      const value = document.createElement("dd");
      term.textContent = label;
      value.textContent = item[key];
      row.append(term, value);
      fields.appendChild(row);
    });
    dimensionRows(item).forEach(([key, label, dimensionValue]) => {
      if (key === "finishedSize" && dimensionValue === item.size) return;
      const row = document.createElement("div");
      const term = document.createElement("dt");
      const value = document.createElement("dd");
      term.textContent = label;
      value.textContent = dimensionValue;
      row.append(term, value);
      fields.appendChild(row);
    });
    const specs = item.productSpecs && typeof item.productSpecs === "object" ? item.productSpecs : {};
    Object.entries(specs).slice(0, 4).forEach(([key, value]) => {
      if (!hasValue(value)) return;
      const row = document.createElement("div");
      const term = document.createElement("dt");
      const detail = document.createElement("dd");
      term.textContent = specLabels[key] || key;
      detail.textContent = value;
      row.append(term, detail);
      fields.appendChild(row);
    });
    if (!fields.children.length) {
      const empty = document.createElement("p");
      empty.className = "multi-product-empty";
      empty.textContent = "尚未识别到本项参数，请在对话中补充。";
      body.appendChild(empty);
    } else {
      body.appendChild(fields);
    }
    if (itemValidation) {
      const readiness = document.createElement("p");
      readiness.className = `multi-product-readiness ${itemValidation.ok ? "ready" : "attention"}`;
      const missing = [...(itemValidation.missing || []), ...(itemValidation.productMissing || [])];
      readiness.textContent = missing.length
        ? `本项信息度 ${itemValidation.readiness || 0}% · 还需确认 ${missing.join("、")}`
        : `本项信息度 ${itemValidation.readiness || 0}% · 基础字段已齐`;
      body.appendChild(readiness);
    }
    const editor = document.createElement("details");
    editor.className = "multi-product-editor";
    const editorSummary = document.createElement("summary");
    editorSummary.textContent = "直接编辑本项";
    editor.appendChild(editorSummary);
    const editorGrid = document.createElement("div");
    editorGrid.className = "multi-product-editor-grid";
    const baseFields = [
      ["quantity", "数量"], ["size", "成品尺寸"], ["pages", "页数"], ["paper", "纸张/材料"],
      ["printing", "印刷颜色"], ["finishing", "表面工艺"], ["binding", "后道/装订"], ["deadline", "交期"]
    ];
    const dimensionFields = Object.entries(dimensionLabels).map(([key, label]) => [key, label, "dimension", false]);
    const productParameters = (itemValidation?.parameters || []).map((parameter) => [parameter.key, parameter.label || parameter.key, true, parameter.required]);
    [...baseFields.map(([key, label]) => [key, label, "base", false]), ...dimensionFields,
      ...productParameters.map(([key, label, isSpec, required]) => [key, label, isSpec ? "spec" : "base", required])]
      .forEach(([key, label, kind, required]) => {
      const field = document.createElement("label");
      const currentValue = kind === "spec" ? specs[key] : kind === "dimension" ? (item.dimensions?.[key] || "") : item[key];
      field.className = `multi-product-editor-field ${required && !hasValue(currentValue) ? "missing" : ""}`;
      const name = document.createElement("span");
      name.textContent = required ? `${label} *` : label;
      const input = document.createElement("input");
      input.type = "text";
      input.value = currentValue;
      input.placeholder = required ? `请填写${label}` : label;
      input.addEventListener("change", () => updateMultiItemField(index, key, input.value, kind));
      field.append(name, input);
      editorGrid.appendChild(field);
    });
    editor.appendChild(editorGrid);
    body.appendChild(editor);
    const focus = document.createElement("button");
    focus.type = "button";
    focus.className = "multi-product-focus";
    focus.textContent = "处理这一项";
    focus.addEventListener("click", (event) => {
      event.preventDefault();
      if (state.activeItemIndex !== index) {
        fileFeedback = null;
        clearPdfPreview();
      }
      sendMessage(`处理第 ${index + 1} 项${item.productType ? `：${item.productType}` : ""}`);
    });
    body.appendChild(focus);
    details.appendChild(body);
    root.appendChild(details);
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
  const multiItems = Array.isArray(state.order?.items) ? state.order.items.filter((item) => item && typeof item === "object") : [];
  const isMultiProduct = multiItems.length > 1 || (state.validation?.multiProduct || []).length > 0;
  renderFieldList("#overview-fields", fields.overview, !isMultiProduct);
  renderFieldList("#spec-fields", fields.specs, !isMultiProduct);
  if (!isMultiProduct) renderDimensionRows("#spec-fields");
  renderFieldList("#process-fields", fields.process, !isMultiProduct);
  if (isMultiProduct) {
    $("#product-context").hidden = true;
    $("#parameter-form").innerHTML = "";
  } else {
    renderParameterForm(profile);
  }
  renderMultiProductItems();

  const overviewFilled = fields.overview.filter((key) => hasValue(getFieldValue(key))).length;
  const specFilled = fields.specs.filter((key) => hasValue(getFieldValue(key))).length + parameters.filter((item) => item.filled).length;
  const specTotal = fields.specs.length + parameters.length;
  const processFilled = fields.process.filter((key) => hasValue(getFieldValue(key))).length;
  $("#overview-meta").textContent = `${overviewFilled} / ${fields.overview.length} 已填写`;
  $("#specs-meta").textContent = isMultiProduct ? `多产品 · ${multiItems.length || (state.validation?.multiProduct || []).length} 项` : state.order.productType ? `${profile.category || "其他印刷品"} · ${specFilled} / ${specTotal}` : "待识别品类";
  $("#process-meta").textContent = `${processFilled} / ${fields.process.length} 已填写`;

  const complete = Array.isArray(state.missingFields)
    ? state.missingFields.length === 0
    : ["productType", "quantity", "size", "paper", "printing", "deadline"].every((key) => state.order[key]);
  const productMissing = profile.missing || [];
  const itemValidations = Array.isArray(state.validation?.itemValidations) ? state.validation.itemValidations : [];
  const multiItemsReady = isMultiProduct && multiItems.length > 1 && itemValidations.length === multiItems.length
    && itemValidations.every((item) => item.ok) && multiItems.every((item) => item.selectedOption);
  const readyToGenerate = isMultiProduct ? multiItemsReady : complete && productMissing.length === 0 && state.selectedOption;
  const confirmed = state.confirmation?.status === "confirmed";
  const status = $("#draft-status");
  status.textContent = isMultiProduct ? (state.orderGenerated ? (confirmed ? "已确认" : "待人工确认") : multiItemsReady ? "可生成" : "逐项确认") : state.orderGenerated ? (confirmed ? "已确认" : "待人工确认") : !complete ? "待补充" : productMissing.length ? "待确认规格" : state.selectedOption ? "可生成" : "可选方案";
  status.className = `draft-status ${isMultiProduct && !multiItemsReady || !isMultiProduct && productMissing.length || state.orderGenerated && !confirmed ? "attention" : confirmed ? "confirmed" : complete ? "ready" : ""}`;
  $("#generate-order").disabled = !readyToGenerate || state.orderGenerated;
  $("#generate-order").textContent = isMultiProduct ? (state.orderGenerated ? "整体交接单已生成" : multiItemsReady ? "生成整体交接单" : "逐项确认后生成") : state.orderGenerated ? "订单草稿已生成" : !complete ? "补全信息后生成" : productMissing.length ? "补全规格后生成" : !state.selectedOption ? "选择方案后生成" : "生成订单草稿";
  $("#copy-order").disabled = !state.orderGenerated;
  const confirmButton = $("#confirm-order");
  if (confirmButton) {
    confirmButton.hidden = !state.orderGenerated;
    confirmButton.disabled = confirmed;
    confirmButton.textContent = confirmed ? "已确认交接单" : "确认交接单";
  }
  $("#order-export").hidden = !confirmed;
  syncDraftGroups(Boolean(state.order.productType), fields.process.some((key) => hasValue(getFieldValue(key))) || Boolean(state.selectedOption));
}

function renderDecision() {
  const stage = state.workflowStage || state.stage || "collect";
  const label = state.workflowLabel || ({ collect: "需求收集", clarify: "品类澄清", recommend: "方案选择", preflight: "文件预检", quote: "报价准备", confirm: "订单确认", export: "导出交接" }[stage] || "需求收集");
  const decision = state.decision || {};
  const workflow = $("#workflow-label");
  const reason = $("#decision-reason");
  const run = $("#decision-run");
  const confidence = $("#decision-confidence");
  if (workflow) workflow.textContent = label;
  if (reason) reason.textContent = decision.reason || state.nextAction || "等待第一条需求。";
  if (confidence) confidence.textContent = decision.humanConfirmationRequired ? "需人工确认" : "可解释";
  if (run) run.textContent = state.runId ? `运行 ${state.runId}` : "本次运行尚未开始";
}

function renderQuoteStatus() {
  const panel = $("#quote-panel");
  if (!panel) return;
  const requests = Array.isArray(state.quoteRequests) ? state.quoteRequests : [];
  const request = state.quoteRequest || requests[requests.length - 1];
  if (!request?.requestId || !request.status) {
    panel.hidden = true;
    return;
  }
  const labels = {
    awaiting_human_confirmation: "待人工确认",
    confirmed: "已确认，待提交",
    cancelled: "已取消",
    stale: "已失效",
    submitted: "已提交",
    failed: "提交失败"
  };
  const status = $("#quote-status");
  status.textContent = labels[request.status] || request.status;
  status.className = `quote-status status-${request.status}`;
  $("#quote-message").textContent = request.message || (request.status === "stale" ? request.staleReason || "订单已变化，请重新准备询价。" : "");
  $("#quote-request-id").textContent = request.requestId;
  const canCancel = ["awaiting_human_confirmation", "confirmed"].includes(request.status);
  $("#quote-cancel").disabled = !canCancel;
  $("#quote-refresh").disabled = false;
  panel.hidden = false;
}

function renderRunTrace() {
  const root = $("#run-trace-list");
  const id = $("#run-id");
  if (!root || !id) return;
  const events = Array.isArray(state.runTrace) ? state.runTrace : [];
  id.textContent = state.runId ? state.runId : "等待运行";
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

function renderValidation() {
  const validation = state.validation || { missing: [], risks: [] };
  const missingRisks = (validation.missing || []).map((field) => ({
    level: "missing",
    message: `缺少必填字段：${field}`,
    suggestion: "可直接发送内容补充，或在订单草稿中查看待补充项。"
  }));
  const lowConfidenceRisks = Object.entries(state.fieldMeta || {})
    .filter(([, meta]) => hasValue(meta?.value) && Number(meta?.confidence) < 0.75)
    .map(([key, meta]) => ({
      level: "warning",
      message: `${labels[key] || key}来自${meta.sourceLabel || "模型推断"}，置信度 ${Math.round(Number(meta.confidence) * 100)}%`,
      suggestion: "请在订单草稿中确认或修改这个字段。"
    }));
  const capability = state.supplierCapability || {};
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
        field.classList.toggle("missing", Boolean(item.required));
        if (item.required) {
          input.setAttribute("aria-invalid", "true");
          showToast(`${item.label}是必填参数`);
        } else {
          input.removeAttribute("aria-invalid");
          if (item.value) sendMessage(`清除${item.label}`, { productSpecs: { [item.key]: "" } });
        }
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
    const provenance = fieldProvenance(`productSpecs.${item.key}`);
    const provenanceText = provenance ? ` · ${provenance.sourceLabel || provenance.source} ${Math.round(Number(provenance.confidence || 0) * 100)}%` : "";
    hint.textContent = `${item.hint || (item.required ? "必填参数" : "可选参数")}${provenanceText}`;
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

function renderOptionCompare(options) {
  const wrap = $("#option-compare");
  const note = $("#option-note");
  const comparable = options.length >= 2 && options.every((o) => o.refPrice);
  if (note) {
    note.hidden = !comparable;
    if (comparable) note.textContent = "参考费用按示例价格参数表估算，不构成报价；交期为常规示例。均以供应商回复为准。";
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
    ["参考费用", (o) => o.refPrice],
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
  rows.forEach(([label, pick]) => {
    const tr = document.createElement("tr");
    const th = document.createElement("th");
    th.textContent = label;
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

function renderOptions() {
  const root = $("#option-list");
  root.innerHTML = "";
  const recommendations = $("#recommendations");
  const options = state.options || [];
  recommendations.hidden = !options.length;
  $("#recommendation-count").textContent = `${options.length} 个方案`;
  renderOptionCompare(options);
  options.forEach((option, index) => {
    const selected = state.selectedOption === option.id || state.activeItemSelectedOption === option.id;
    const card = document.createElement("article");
    card.className = `option-card ${selected ? "selected" : ""}`;
    card.setAttribute("role", "button");
    card.setAttribute("tabindex", "0");
    card.setAttribute("aria-pressed", String(selected));
    card.setAttribute("aria-label", `选择${option.title}`);
    card.innerHTML = `${option.score === "综合推荐" || index === 1 ? '<span class="option-badge">推荐</span>' : ""}<div class="option-card-heading"><h4></h4><span class="option-score"></span></div><p></p><dl class="option-grid"></dl><button class="option-details-toggle" type="button">展开推荐理由</button><div class="option-details" hidden></div>`;
    card.querySelector("h4").textContent = option.title;
    card.querySelector(".option-score").textContent = option.score || "";
    card.querySelector("p").textContent = option.description;
    [["参考费用", option.refPrice], ["交期", option.leadDetail || option.lead], ["印刷方式", option.printMode], ["材料", option.paper], ["工艺", option.finishing], ["装订", option.binding]].forEach(([name, value]) => {
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
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        chooseOption(option.id);
      }
    });
    card.addEventListener("click", () => chooseOption(option.id));
    root.appendChild(card);
  });
  if (options.length && state.stage === "recommend" && !state.orderGenerated) {
    requestAnimationFrame(() => recommendations.closest(".order-section")?.scrollTo({ top: Math.max(0, recommendations.offsetTop - 12), behavior: "smooth" }));
  }
}

async function chooseOption(optionId) {
  try {
    render(await api("/api/choose", { sessionId, optionId,
      itemIndex: Array.isArray(state.order?.items) && state.order.items.length > 1 ? state.activeItemIndex : undefined }));
  }
  catch (error) { showToast(apiErrorText(error, "方案选择暂时失败，请重试"), "error"); }
}

function renderFileState() {
  const node = $("#file-state");
  const items = Array.isArray(state.order?.items) ? state.order.items : [];
  const activeItem = items.length > 1 && Number.isInteger(state.activeItemIndex) ? items[state.activeItemIndex] : null;
  const storedFile = activeItem ? activeItem.uploadedFile : state.uploadedFile;
  const feedback = fileFeedback || (storedFile ? { ok: true, fileName: storedFile, message: "基础检查已通过。" } : null);
  const activePreview = pdfPreview
    && (items.length <= 1 || storedFile === pdfPreview.fileName)
    && (!feedback || pdfPreview.fileName === feedback.fileName) ? pdfPreview : null;
  node.hidden = !feedback && !activePreview;
  if (!feedback && !activePreview) return;
  node.className = `file-state ${feedback.ok && activePreview?.preflight?.ok !== false ? "ok" : "error"}`;
  node.innerHTML = "";
  const heading = document.createElement("div");
  heading.className = "file-state-heading";
  const name = document.createElement("strong");
  name.textContent = activePreview?.fileName || feedback?.fileName || "未命名文件";
  heading.append(name);
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
    if (activePreview.preflight) {
      const checks = document.createElement("div");
      checks.className = "file-checks";
      activePreview.preflight.checks.forEach((item) => {
        const check = document.createElement("div");
        check.className = `file-check check-${item.status}`;
        const term = document.createElement("span");
        const detail = document.createElement("strong");
        term.textContent = item.label;
        detail.textContent = item.detail;
        check.append(term, detail);
        checks.appendChild(check);
      });
      if (activePreview.preflight.warnings.length) {
        const warning = document.createElement("p");
        warning.className = "file-warning";
        warning.textContent = `需要确认：${activePreview.preflight.warnings.join("；")}`;
        checks.append(warning);
      }
      heading.append(checks);
    }
    heading.append(viewer, fallback);
  } else {
    const detail = document.createElement("small");
    detail.textContent = feedback?.message || "";
    heading.append(detail);
  }
  node.append(heading);
}

async function inspectPdf(file) {
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    const decoder = new TextDecoder("windows-1252");
    const content = decoder.decode(bytes);
    const header = decoder.decode(bytes.slice(0, 1024));
    const tail = decoder.decode(bytes.slice(Math.max(0, bytes.length - 2048)));
    const parseBox = (name) => {
      const pattern = new RegExp(`/${name}\\s*\\[\\s*(-?[\\d.]+)\\s+(-?[\\d.]+)\\s+(-?[\\d.]+)\\s+(-?[\\d.]+)\\s*\\]`);
      const match = content.match(pattern);
      return match ? match.slice(1).map(Number) : null;
    };
    const colorSpaces = ["DeviceRGB", "DeviceCMYK", "Separation", "DeviceN"].filter((name) => new RegExp(`/${name}\\b`).test(content));
    return {
      readable: true,
      isPdf: header.startsWith("%PDF-"),
      pdfVersion: header.match(/^%PDF-(\d\.\d)/)?.[1] || null,
      pageCount: (content.match(/\/Type\s*\/Page\b/g) || []).length,
      encrypted: /\/Encrypt\b/.test(content),
      hasEof: tail.includes("%%EOF"),
      boxes: { media: parseBox("MediaBox"), crop: parseBox("CropBox"), trim: parseBox("TrimBox"), bleed: parseBox("BleedBox") },
      colorSpaces,
      fontEmbedding: /\/FontFile(?:2|3)?\b/.test(content) ? "embedded" : "unknown",
      imageCount: (content.match(/\/Subtype\s*\/Image\b/g) || []).length,
      hasTransparency: /\/SMask\b|\/(?:ca|CA)\s+0?\.\d+/i.test(content),
      hasOverprint: /\/(?:OP|op)\b/.test(content)
    };
  } catch (error) {
    return { readable: false, isPdf: false, pdfVersion: null, pageCount: null, encrypted: false, hasEof: false, boxes: {}, colorSpaces: [], fontEmbedding: "unknown", imageCount: 0, hasTransparency: false, hasOverprint: false };
  }
}

const toolCodes = {
  validate_order: "CK", recommend_processes: "RF", estimate_price: "PR",
  prepare_handoff: "HO", request_supplier_quote: "QT",
  match_supplier_capability: "CP", explain_print_term: "KB"
};

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
    code.textContent = toolCodes[item.name] || "··";
    code.title = item.name;
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
  const workflowStage = state.workflowStage || state.stage || "collect";
  // Progress = field readiness from the backend validation; workflow stages
  // are shown by the step list, not baked into the percentage.
  const fallback = Math.round((required.filter((key) => state.order[key]).length / required.length) * 100);
  const readiness = Number(state.readiness);
  const percent = Number.isFinite(readiness) && readiness > 0 ? Math.min(100, Math.round(readiness)) : fallback;
  $("#progress-stage").textContent = state.workflowLabel || ({ collect: "需求收集", clarify: "品类澄清", recommend: "方案选择", preflight: "文件预检", quote: "报价准备", confirm: "确认订单", export: "导出交接" })[workflowStage] || "需求收集";
  const bar = $("#progress-bar");
  bar.style.width = `${percent}%`;
  bar.title = "字段信息度";
  $("#progress-percent").textContent = `${percent}%`;
  const order = ["collect", "recommend", "confirm"];
  document.querySelectorAll(".step").forEach((step) => {
    const displayStage = workflowStage === "clarify" ? "collect" : ["preflight", "quote", "export"].includes(workflowStage) ? "confirm" : workflowStage;
    step.classList.toggle("active", step.dataset.step === displayStage);
    step.classList.toggle("done", order.indexOf(step.dataset.step) < order.indexOf(displayStage));
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
  autoGrowComposer();
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
    addMessage("assistant", `${error?.message || "智能体暂时无法连接，请确认本地服务已启动。"}${suffix}`);
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

function autoGrowComposer() {
  const input = $("#message-input");
  if (!input) return;
  input.style.height = "auto";
  input.style.height = `${Math.min(120, Math.max(38, input.scrollHeight))}px`;
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
    showToast(apiErrorText(error, "工具调用失败，请确认本地服务已启动，或先刷新会话"), "error");
  }
}

$("#message-form").addEventListener("submit", (event) => { event.preventDefault(); sendMessage($("#message-input").value); });
$("#message-input").addEventListener("input", autoGrowComposer);
$("#message-input").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#message-form").requestSubmit(); } });
$("#generate-order").addEventListener("click", async () => { try { render(await api("/api/generate", { sessionId })); } catch (error) { showToast(apiErrorText(error, "订单生成失败，请重试"), "error"); } });
$("#confirm-order").addEventListener("click", async () => {
  try { render(await api("/api/confirm", { sessionId, note: "用户在订单工作台确认" })); }
  catch (error) { showToast(apiErrorText(error, "确认交接单失败，请重试"), "error"); }
});
$("#quote-refresh").addEventListener("click", async () => {
  const requestId = state.activeQuoteRequestId || state.quoteRequest?.requestId || (state.quoteRequests?.length ? state.quoteRequests[state.quoteRequests.length - 1]?.requestId : "");
  if (!requestId) return;
  try {
    render(await api("/api/quote/status", { sessionId, requestId }));
    showToast("询价状态已刷新");
  } catch (error) { showToast(apiErrorText(error, "询价状态刷新失败，请重试"), "error"); }
});
$("#quote-cancel").addEventListener("click", async () => {
  const requestId = state.activeQuoteRequestId || state.quoteRequest?.requestId || (state.quoteRequests?.length ? state.quoteRequests[state.quoteRequests.length - 1]?.requestId : "");
  if (!requestId) return;
  try {
    render(await api("/api/quote/cancel", { sessionId, requestId, reason: "用户在订单工作台取消询价" }));
    showToast("询价请求已取消");
  } catch (error) { showToast(apiErrorText(error, "取消询价失败，请重试"), "error"); }
});
$("#copy-order").addEventListener("click", async () => {
  const groups = getOrderGroups();
  const lines = groups.filter(([, items]) => items.length).flatMap(([title, items]) => [`【${title}】`, ...items.map(([label, value]) => `${label}：${value}`), ""]);
  try { await navigator.clipboard.writeText(lines.join("\n")); showToast("订单信息已复制"); } catch { showToast("当前浏览器不支持自动复制", "error"); }
});

function getOrderGroups() {
  const multiItems = Array.isArray(state.order?.items) ? state.order.items.filter((item) => item && typeof item === "object") : [];
  if (multiItems.length > 1) {
    const itemGroups = [];
    multiItems.forEach((item, index) => {
      const itemLabel = `产品项 ${index + 1}${item.productType ? ` · ${item.productType}` : ""}`;
      const overview = [["数量", item.quantity], ["使用场景", item.purpose], ["交期", item.deadline], ["预算偏好", item.budget]];
      const specs = [["成品尺寸", item.size], ...dimensionRows(item).filter(([key, value, dimensionValue]) => !(key === "finishedSize" && dimensionValue === item.size)).map(([, label, value]) => [label, value]), ["页数", item.pages], ["版式方向", item.orientation]];
      const process = [["纸张/材料", item.paper], ["印刷颜色", item.printing], ["表面工艺", item.finishing], ["后道/装订", item.binding]];
      const productSpecs = Object.entries(item.productSpecs || {}).map(([key, value]) => [key, value]);
      itemGroups.push([`${itemLabel} · 订单概览`, overview.filter(([, value]) => hasValue(value))]);
      itemGroups.push([`${itemLabel} · 产品规格`, [...specs, ...productSpecs].filter(([, value]) => hasValue(value))]);
      itemGroups.push([`${itemLabel} · 生产工艺`, process.filter(([, value]) => hasValue(value))]);
    });
    return itemGroups;
  }
  const fields = getDraftFieldSets();
  return [
    ["订单概览", fields.overview.map((key) => [labels[key], getFieldValue(key)]).filter(([, value]) => hasValue(value))],
    ["产品规格", [
      ...fields.specs.map((key) => [labels[key], getFieldValue(key)]).filter(([, value]) => hasValue(value)),
      ...dimensionRows(state.order).filter(([key, , value]) => !(key === "finishedSize" && value === state.order.size)).map(([, label, value]) => [label, value]),
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
      knowledgeVersion: state.knowledge?.version || state.productProfile?.knowledge?.version || "",
      order: state.order,
      productProfile: state.productProfile,
      handoff: state.handoff ? { text: state.handoff.text, supplierReadiness: state.handoff.supplierReadiness } : null,
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
  clearPdfPreview();
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    fileFeedback = { ok: false, fileName: file.name, message: "目前仅支持上传 PDF 文件。" };
    addMessage("user", `上传文件：${file.name}`);
    showToast(fileFeedback.message, "error");
    renderFileState();
    return;
  }
  addMessage("user", `上传文件：${file.name}`);
  if (file.size > 20 * 1024 * 1024) {
    fileFeedback = { ok: false, fileName: file.name, message: "文件超过 20 MB，请压缩后再上传。" };
    showToast(fileFeedback.message, "error");
    renderFileState();
    return;
  }
  fileFeedback = { ok: true, fileName: file.name, message: "文件已加载，正在做基础预检。" };
  renderFileState();
  const inspection = await inspectPdf(file);
  if (!inspection.readable || !inspection.isPdf) {
    clearPdfPreview();
    fileFeedback = { ok: false, fileName: file.name, message: "文件不是有效的 PDF，或内容无法读取。" };
    renderFileState();
    addMessage("assistant", fileFeedback.message);
    return;
  }
  pdfPreview = {
    url: URL.createObjectURL(file),
    fileName: file.name,
    size: file.size,
    type: file.type || "application/pdf",
    lastModified: file.lastModified,
    inspection
  };
  renderFileState();
  try {
    const multiItems = Array.isArray(state.order?.items) && state.order.items.length > 1;
    const data = await api("/api/preflight", {
      sessionId,
      fileName: file.name,
      sizeBytes: file.size,
      pageCount: inspection.pageCount,
      encrypted: inspection.encrypted,
      readable: inspection.readable,
      inspection,
      itemIndex: multiItems ? state.activeItemIndex : undefined
    });
    fileFeedback = null;
    pdfPreview.preflight = data.toolResult || null;
    if (data.toolResult?.status === "blocked") {
      fileFeedback = { ok: false, fileName: file.name, message: data.messages?.[0] || "请先选择产品项，再绑定文件。" };
    } else if (data.toolResult && !data.toolResult.ok) {
      fileFeedback = data.toolResult;
    }
    render(data);
  } catch (error) {
    clearPdfPreview();
    fileFeedback = { ok: false, fileName: file.name, message: apiErrorText(error, "文件预检调用失败，请重试") };
    renderFileState();
    showToast(fileFeedback.message, "error");
  }
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
$("#platform-select").addEventListener("change", async (event) => { try { render(await api("/api/platform", { sessionId, platformId: event.target.value })); } catch (error) { showToast(apiErrorText(error, "平台切换失败"), "error"); } });
$("#sidebar-toggle").addEventListener("click", () => setSidebarCollapsed(!sidebarCollapsed));
$("#order-toggle").addEventListener("click", () => {
  layout.orderCollapsed = !layout.orderCollapsed;
  applyLayout();
  saveLayout();
});
$("#workspace-backdrop").addEventListener("click", () => setSidebarCollapsed(true));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !DESKTOP_QUERY.matches && !sidebarCollapsed) setSidebarCollapsed(true);
});
window.addEventListener("resize", updateBackdrop);
$("#theme-toggle").addEventListener("click", () => setHighContrast(document.documentElement.dataset.theme !== "high-contrast"));
$("#history-list").addEventListener("click", (event) => {
  const item = event.target.closest(".history-item");
  if (item) switchOrder(item.dataset.sessionId);
});
$("#save-preset").addEventListener("click", saveCurrentSpecPreset);
$("#preset-list").addEventListener("click", (event) => {
  const apply = event.target.closest(".preset-apply");
  if (apply) applySpecPreset(apply.dataset.presetId);
  const remove = event.target.closest(".preset-remove");
  if (remove) removeSpecPreset(remove.dataset.presetId);
});
$("#reset-order").addEventListener("click", () => {
  $("#confirm-reset-dialog").showModal();
});

const confirmResetDialog = $("#confirm-reset-dialog");
confirmResetDialog.addEventListener("close", () => {
  if (confirmResetDialog.returnValue !== "confirm") return;
  (async () => {
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
    clearPdfPreview();
    await bootstrap();
    showToast("已重置当前订单，开始新的对话");
  })();
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
    showToast(error.message || "模型连接测试失败", "error");
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
    showToast(error.message.includes("URL 和模型名") ? "URL 和模型名需要同时填写" : error.message.includes("接口 URL") ? "接口 URL 格式不正确" : "设置保存失败，请检查接口地址", "error");
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
  } catch { showToast("配置清空失败，请重试", "error"); }
  finally { clear.disabled = false; }
});

function showToast(text, kind = "success") {
  const node = $("#toast");
  node.textContent = text;
  node.className = `toast show ${kind}`;
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => node.classList.remove("show"), 2600);
}

async function bootstrap() {
  try {
    const [platforms, products, tools, settings] = await Promise.all([
      api("/api/platforms", undefined, "GET"), api("/api/products", undefined, "GET"),
      api("/api/tools", undefined, "GET"), api("/api/settings", undefined, "GET")
    ]);
    const platformSelect = $("#platform-select");
    platformSelect.innerHTML = "";
    platforms.platforms.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.name;
      const profile = item.supplierProfile || {};
      option.title = [
        `品类：${(profile.categories || []).join("、") || "待补充"}`,
        `最大尺寸：${profile.maxSize || "待补充"}`,
        `交期参考：${profile.leadTime || "待补充"}`
      ].join("\n");
      platformSelect.appendChild(option);
    });
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

setupSplitters();
applyLayout();
autoGrowComposer();
bootstrap();
setSidebarCollapsed(sidebarCollapsed);
setHighContrast(localStorage.getItem("printops_theme") === "high-contrast");
