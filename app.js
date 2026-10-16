const $ = (selector) => document.querySelector(selector);
const labels = {
  productType: "印刷品", purpose: "使用场景", quantity: "数量", size: "成品尺寸",
  pages: "页数", orientation: "版式方向", paper: "纸张/材料", printing: "印刷颜色", finishing: "表面工艺", binding: "装订/后道",
  deadline: "交期", budget: "预算偏好", platform: "目标平台"
};
const dimensionLabels = {
  finishedSize: "成品尺寸", expandedSize: "展开尺寸", dieCutSize: "刀模尺寸", packageSize: "包装三维尺寸",
  packageInnerSize: "包装内尺寸", packageOuterSize: "包装外尺寸"
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
let sidebarCollapsed = localStorage.getItem("printops_sidebar") === "collapsed" ||
  (!localStorage.getItem("printops_sidebar") && window.matchMedia("(max-width: 1050px)").matches);
let previousOrder = {};
let updatedOrderKeys = new Set();
let updatedSpecKeys = new Set();
let suppressOrderDiff = true;
let fileFeedback = null;
let pdfPreview = null;
const orderHistoryKey = "printops_order_history";
let serverOrderHistory = [];
let historyRequestSeq = 0;
let providers = [];
let storageInfo = {mode: "sqlite", persistent: true};
let orderWritePending = false;
let undoChange = null;
const unresolvedWrites = new Map();
// A response belongs to the view in which it was requested, even when two
// snapshots share a business revision (for example, product focus changes).
let viewEpoch = 0;
let viewLoading = false;
const responseView = Symbol("responseView");
let settingsView = "overview";
const specPresetKey = "printops_spec_presets";
const layoutKey = "printops_layout";
const LAYOUT_DEFAULTS = { sidebarW: 192, orderW: 335, orderCollapsed: false };
const SIDEBAR_RANGE = [180, 280];
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
  updateOrderToggle();
  updateBackdrop();
}

function updateOrderToggle() {
  const orderToggle = $("#order-toggle");
  if (orderToggle) {
    const collapsed = DESKTOP_QUERY.matches ? layout.orderCollapsed : $("#workspace").dataset.mobilePane !== "order";
    orderToggle.setAttribute("aria-label", collapsed ? "展开订单栏" : "收起订单栏");
    orderToggle.title = collapsed ? "展开订单栏" : "收起订单栏";
    orderToggle.dataset.collapsed = String(collapsed);
    orderToggle.setAttribute("aria-expanded", String(!collapsed));
  }
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
  return serverOrderHistory;
}

async function refreshOrderHistory(includeArchived = $("#history-include-archived")?.checked === true) {
  const seq = ++historyRequestSeq;
  try {
    const result = await api(`/api/orders?limit=100&includeArchived=${includeArchived ? "true" : "false"}`, undefined, "GET");
    if (seq !== historyRequestSeq) return;
    serverOrderHistory = result.orders.map((entry) => ({...entry, id: entry.orderId}));
    renderOrderHistory();
  } catch {
    $("#history-count").textContent = "同步失败";
  }
}

async function migrateLocalHistory() {
  const legacy = localStorage.getItem(orderHistoryKey);
  if (!legacy) return;
  const result = await api("/api/orders/import-local-history", {orders: JSON.parse(legacy)});
  if (!result.errors?.length) localStorage.removeItem(orderHistoryKey);
  else showToast("部分旧历史未迁移，原始备份仍保留在浏览器中", "error");
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
  if (sessionId) refreshOrderHistory();
}

function renderOrderHistory() {
  const root = $("#history-list");
  const history = loadOrderHistory();
  closeHistoryContextMenu();
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
    const orderTitle = orderHistoryTitle(item.order);
    const status = {draft:"草稿", collecting:"编辑中", ready_for_review:"待审核", confirmed:"已确认", handoff_ready:"交接就绪", quote_draft:"询价草稿", quote_received:"已收报价", cancelled:"已取消", archived:"已归档"};
    const statusLabel = status[item.status] || "编辑中";
    const historyTime = formatOrderHistoryTime(item.updatedAt);
    const title = document.createElement("strong");
    title.textContent = orderTitle;
    const meta = document.createElement("small");
    meta.className = "history-meta";
    meta.textContent = `${statusLabel} · ${historyTime}`;
    button.setAttribute("aria-label", `${orderTitle}，${statusLabel}，${historyTime}`);
    button.title = `${orderTitle} · ${statusLabel} · ${historyTime}`;
    button.append(title, meta);
    const row = document.createElement("div");
    row.className = "history-row";
    row.append(button);
    const hoverActions = document.createElement("div");
    hoverActions.className = "history-hover-actions";
    hoverActions.setAttribute("role", "group");
    hoverActions.setAttribute("aria-label", `订单操作：${orderTitle}`);
    const exportAction = document.createElement("button");
    exportAction.type = "button";
    exportAction.className = "history-hover-action";
    exportAction.dataset.historyAction = "export";
    exportAction.dataset.orderId = item.id;
    exportAction.textContent = "导出";
    exportAction.title = "导出订单备份";
    hoverActions.append(exportAction);
    if (item.status !== "archived") {
      const archiveAction = document.createElement("button");
      archiveAction.type = "button";
      archiveAction.className = "history-hover-action";
      archiveAction.dataset.historyAction = "archive";
      archiveAction.dataset.orderId = item.id;
      archiveAction.textContent = "归档";
      archiveAction.title = "归档订单";
      hoverActions.append(archiveAction);
    } else {
      const purgeAction = document.createElement("button");
      purgeAction.type = "button";
      purgeAction.className = "history-hover-action";
      purgeAction.dataset.historyAction = "purge";
      purgeAction.dataset.orderId = item.id;
      purgeAction.textContent = "删除";
      purgeAction.title = "永久删除归档订单";
      hoverActions.append(purgeAction);
    }
    row.append(hoverActions);
    button.dataset.historyMenu = item.id;
    button.setAttribute("aria-haspopup", "menu");
    button.setAttribute("aria-controls", "history-context-menu");
    button.setAttribute("aria-expanded", "false");
    root.append(row);
  });
}

let historyMenuTrigger = null;

function closeHistoryContextMenu({restoreFocus = false} = {}) {
  const menu = $("#history-context-menu");
  if (!menu) return;
  menu.hidden = true;
  menu.innerHTML = "";
  if (historyMenuTrigger) {
    historyMenuTrigger.setAttribute("aria-expanded", "false");
    if (restoreFocus && historyMenuTrigger.getClientRects().length) historyMenuTrigger.focus({preventScroll: true});
  }
  historyMenuTrigger = null;
}

function historyContextMenuItems(orderId) {
  const item = serverOrderHistory.find((entry) => entry.id === orderId);
  if (!item) return [];
  const items = [["export", "导出备份"]];
  if (item.status !== "archived") items.push(["archive", "归档订单"]);
  else items.push(["purge", "永久删除记录"]);
  return items;
}

function positionHistoryContextMenu(x, y) {
  const menu = $("#history-context-menu");
  if (!menu) return;
  const margin = 8;
  const rect = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(margin, Math.min(x, window.innerWidth - rect.width - margin))}px`;
  menu.style.top = `${Math.max(margin, Math.min(y, window.innerHeight - rect.height - margin))}px`;
}

function openHistoryContextMenu(orderId, trigger, point = {}) {
  const menu = $("#history-context-menu");
  const items = historyContextMenuItems(orderId);
  if (!menu || !items.length) return;
  closeHistoryContextMenu();
  menu.dataset.orderId = orderId;
  menu.innerHTML = "";
  items.forEach(([action, label], index) => {
    const control = document.createElement("button");
    control.type = "button";
    control.className = "history-context-action";
    control.dataset.historyMenuAction = action;
    control.dataset.historyAction = action;
    control.dataset.orderId = orderId;
    control.setAttribute("role", "menuitem");
    control.textContent = label;
    control.tabIndex = index === 0 ? 0 : -1;
    menu.append(control);
  });
  historyMenuTrigger = trigger;
  trigger?.setAttribute("aria-expanded", "true");
  menu.hidden = false;
  const anchor = trigger?.getBoundingClientRect();
  const x = Number.isFinite(point.x) ? point.x : (anchor ? anchor.right - menu.offsetWidth : window.innerWidth / 2);
  const y = Number.isFinite(point.y) ? point.y : (anchor ? anchor.bottom + 4 : window.innerHeight / 2);
  positionHistoryContextMenu(x, y);
  menu.querySelector("[role=menuitem]")?.focus();
}

function moveHistoryMenuFocus(direction) {
  const menu = $("#history-context-menu");
  if (!menu || menu.hidden) return;
  const items = [...menu.querySelectorAll("[role=menuitem]")];
  if (!items.length) return;
  const current = Math.max(0, items.indexOf(document.activeElement));
  const next = direction === "first" ? 0 : direction === "last" ? items.length - 1 : (current + direction + items.length) % items.length;
  items.forEach((item, index) => { item.tabIndex = index === next ? 0 : -1; });
  items[next].focus();
}

async function historyAction(action, orderId) {
  if (isSending) return;
  try {
    if (action === "export") {
      const result = await api(`/api/orders/${encodeURIComponent(orderId)}/export`, undefined, "GET");
      const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], {type: "application/json;charset=utf-8"}));
      const link = document.createElement("a");
      link.href = url;
      link.download = `printops-backup-${orderId}.json`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      showToast("完整订单备份已导出，包含来源、对话和审计记录");
    } else if (action === "archive") {
      if (!window.confirm("归档后订单只读，仍可查看和导出。确定归档此订单？")) return;
      const item = serverOrderHistory.find((entry) => entry.id === orderId);
      await api(`/api/orders/${encodeURIComponent(orderId)}/archive`, {expectedRevision: item.revision});
      if (orderId === sessionId) render(await api(`/api/orders/${encodeURIComponent(orderId)}`, undefined, "GET"), false);
      await refreshOrderHistory();
      showToast("订单已归档；选择“显示已归档订单”可查看和导出");
    } else if (action === "purge") {
      const item = serverOrderHistory.find((entry) => entry.id === orderId);
      if (!item || item.status !== "archived") return;
      if (!window.confirm("永久删除这条归档记录？订单、对话、报价和审计数据都会被清除；如需保留请先导出备份。")) return;
      await api(`/api/orders/${encodeURIComponent(orderId)}/purge`, {expectedRevision: item.revision, confirm: true});
      if (orderId === sessionId) {
        viewEpoch += 1;
        activeChatController?.abort();
        activeChatController = null;
        localStorage.removeItem("printops_session");
        sessionId = "";
        $("#chat-feed").innerHTML = "";
        await bootstrap();
      } else {
        await refreshOrderHistory();
      }
      showToast("归档记录已永久删除");
    }
  } catch (error) {
    showToast(apiErrorText(error, "历史订单操作失败，请重试"), "error");
    await refreshOrderHistory();
  }
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
  const epoch = ++viewEpoch;
  viewLoading = true;
  setComposerBusy(true);
  try {
    const snapshot = await api(`/api/orders/${encodeURIComponent(orderId)}`, undefined, "GET");
    if (epoch !== viewEpoch) return;
    $("#chat-feed").innerHTML = "";
    fileFeedback = null;
    clearPdfPreview();
    suppressOrderDiff = true;
    state = { order: {}, stage: "collect", quickReplies: [], options: [], selectedOption: null, activeItemSelectedOption: null, orderGenerated: false, handoff: null, confirmation: { status: "not_ready" }, quoteRequest: null, quoteRequests: [], activeQuoteRequestId: null, toolTrace: [], availableTools: state.availableTools || [], productProfile: null, activeItemIndex: null };
    render(snapshot, false);
    PrintOpsJourney.clearFailure();
    const unresolved = unresolvedWrites.get(sessionId);
    if (unresolved) PrintOpsJourney.failure(unresolved.error, unresolved.request, recoverOperation);
    $("#platform-select").value = snapshot.order?.platform || "generic";
    const messages = Array.isArray(snapshot.history) ? snapshot.history : [];
    if (messages.length) {
      addMessage("assistant", `已切换到订单 ${orderHistoryTitle(snapshot.order)}。`);
      messages.forEach((message) => addMessage(message.role === "user" ? "user" : "assistant", message.text || ""));
    } else addMessage("assistant", "已切换到这个订单。当前没有保存的对话内容，可以直接继续补充。");
    showToast("订单已切换");
  } catch (error) {
    if (epoch === viewEpoch) showToast(apiErrorText(error, "订单切换失败，请重试"), "error");
  } finally {
    if (epoch === viewEpoch) { viewLoading = false; setComposerBusy(false); }
  }
}

async function api(path, body, method = "POST", options = {}) {
  const epoch = viewEpoch;
  const requestSession = body?.sessionId || sessionId;
  const headers = { "Content-Type": "application/json" };
  if (window.__PRINTOPS_TOKEN__) headers["X-PrintOps-Token"] = window.__PRINTOPS_TOKEN__;
  if (method !== "GET" && body?.sessionId === sessionId && Number.isInteger(state.revision) && !["/api/session", "/api/session/context"].includes(path)) {
    body = {...body, expectedRevision: body.expectedRevision ?? state.revision};
  }
  const scoped = ["/api/chat", "/api/choose", "/api/preflight", "/api/tools/call", "/api/fields"].includes(path);
  if (scoped && body?.itemId == null && body?.itemIndex == null && state.order?.items?.length > 1) {
    body = {...body, itemId: state.order.items[state.activeItemIndex]?.itemId};
  }
  const mutation = method !== "GET" && Boolean(body?.sessionId) && !["/api/session", "/api/session/context", "/api/focus"].includes(path);
  if (mutation && viewLoading) throw new Error("正在切换订单或产品，请稍后继续。");
  if (mutation && requestSession === sessionId && (state.status === "archived" || state.orderStatus === "archived")) throw new Error("已归档订单只能查看或导出。");
  if (mutation && unresolvedWrites.has(body.sessionId)) {
    const unresolved = unresolvedWrites.get(body.sessionId);
    PrintOpsJourney.failure(unresolved.error, unresolved.request, recoverOperation);
    throw new Error("上次保存结果尚未确认，请先使用恢复提示读取最新状态。");
  }
  if (mutation && orderWritePending) throw new Error("上一项修改正在保存，请稍后继续。");
  if (mutation) body = {...body, operationId: body.operationId || crypto.randomUUID()};
  const fieldBefore = path === "/api/fields" ? (body.fields || Object.keys(body.values || {})).map(path => {
    const field = reviewField(path, body.itemId);
    return {fieldPath: path, label: field.label, value: field.value ?? field.candidateValue ?? null};
  }) : [];
  if (mutation) { orderWritePending = true; PrintOpsJourney.saved("正在保存…", "saving"); }
  try {
  const response = await fetch(path, {
    method,
    headers,
    body: method === "GET" ? undefined : JSON.stringify(body || {}),
    signal: options.signal
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    const error = new Error(detail.message || detail.error || `API ${response.status}`);
    error.code = detail.code;
    error.status = response.status;
    error.requestId = detail.requestId || response.headers.get("X-Request-ID") || "";
    throw error;
  }
  const result = await response.json();
  if (result && typeof result === "object") result[responseView] = epoch;
  if (result.needsReconciliation) {
    const error = new Error("这次请求已有执行记录，但尚无最终完成回执。请先读取最新订单并核对内容。");
    error.status = 408; error.code = "OPERATION_RECONCILIATION_REQUIRED";
    throw error;
  }
  if (mutation && epoch === viewEpoch && requestSession === sessionId) {
    PrintOpsJourney.clearFailure();
    PrintOpsJourney.saved(storageInfo.persistent ? "已保存到本机" : "仅当前进程保存 · 退出后丢失");
  }
  return result;
  } catch (error) {
    if (error.name !== "AbortError") {
      const request = {path, body: structuredClone(body || {}), method, conflict: error.status === 409, fieldBefore};
      if (mutation && (!error.status || error.status >= 500 || error.status === 408)) unresolvedWrites.set(body.sessionId, {error, request});
      if (epoch === viewEpoch) PrintOpsJourney.failure(error, request, recoverOperation);
    }
    throw error;
  } finally {
    if (mutation) {
      orderWritePending = false;
      setComposerBusy(isSending || viewLoading);
    }
  }
}

async function recoverOperation(request) {
  try {
    const id = request.body.sessionId || sessionId;
    if (!id) { await bootstrap(); return; }
    const latest = await api(`/api/orders/${encodeURIComponent(id)}`, undefined, "GET");
    if (id !== sessionId || latest[responseView] !== viewEpoch) return;
    unresolvedWrites.delete(id);
    render(latest, false);
    PrintOpsJourney.recovered(request, latest, async () => {
      if (id !== sessionId || ["confirm", "reject"].includes(request.body.action)) return;
      try {
        const result = await api(request.path, {...request.body, operationId: crypto.randomUUID(), expectedRevision: latest.revision}, request.method);
        if (id !== sessionId || result[responseView] !== viewEpoch) return;
        if (request.path === "/api/fields") {
          for (const path of request.body.fields || Object.keys(request.body.values || {})) {
            PrintOpsJourney.fieldSaved(reviewField(path, request.body.itemId), id, request.body.action === "clear" ? undefined : request.body.values[path]);
          }
        }
        render(result);
      }
      catch { /* The persistent recovery card carries the error. */ }
    });
  } catch { /* Keep the error visible until the service can be read. */ }
}

async function refreshCredentials(request) {
  try {
    // The homepage is same-origin and carries the server's current local token.
    // Parse only its JSON string; never execute scripts or discard the document.
    const response = await fetch("/", {cache: "no-store", credentials: "same-origin"});
    if (!response.ok) throw new Error("暂时无法读取本地访问凭据，请确认服务已启动。");
    const html = await response.text();
    const match = html.match(/window\.__PRINTOPS_TOKEN__\s*=\s*("(?:\\.|[^"\\])*")\s*;/);
    if (!match) throw new Error("本地服务未返回有效凭据，输入仍保留在当前页面。");
    const token = JSON.parse(match[1]);
    if (typeof token !== "string" || !token) throw new Error("本地访问凭据无效。");
    window.__PRINTOPS_TOKEN__ = token;
    if (!state.sessionId) await bootstrap();
    else await recoverOperation(request);
  } catch (error) { PrintOpsJourney.failure(error, request, recoverOperation); }
}

function reviewField(path, itemId) {
  return state.review?.fields?.find(field => field.fieldPath === path && (itemId ? field.itemId === itemId : field.itemId == null || state.order?.items?.length <= 1)) || {fieldPath: path, label: labels[path] || path, itemId};
}

async function changeField(field, action, value) {
  if (isSending || orderWritePending || viewLoading || state.status === "archived" || state.orderStatus === "archived") return;
  try {
    if (action === "set" && /(?:size|bleed|thickness)/i.test(field.fieldPath)) value = PrintOpsWorkbench.manualDimension(value);
  } catch (error) { showToast(error.message, "error"); return; }
  const before = field.value;
  const origin = sessionId;
  const focusId = field.fieldId;
  let focusAfterSave;
  isSending = true; setComposerBusy(true);
  try {
    const body = {sessionId, itemId: field.itemId, action};
    if (action === "set") body.values = {[field.fieldPath]: value};
    else body.fields = [field.fieldPath];
    const result = await api("/api/fields", body);
    if (origin !== sessionId) return;
    if (["set", "clear"].includes(action)) PrintOpsJourney.fieldSaved(field, origin);
    render(result, false);
    if (["set", "clear"].includes(action)) {
      undoChange = {field, before, sessionId, revision: result.revision};
      const notice = $("#change-notice");
      notice.hidden = false;
      notice.replaceChildren(PrintOpsJourney.el("span", `${field.label}已更新，相关结果需按当前版本核对。`), PrintOpsJourney.button("撤销本次修改", undoLastField));
    }
    const reviewInput = [...document.querySelectorAll("[data-review-input]")].find(input => input.dataset.reviewInput === focusId);
    const inline = [...document.querySelectorAll("[data-field-path]")].find(input => input.dataset.fieldPath === field.fieldPath && (!field.itemId || input.dataset.itemId === field.itemId));
    focusAfterSave = reviewInput?.closest(".journey-pane")?.hidden === false ? reviewInput : inline;
  } catch { /* Recovery card retains the request and field values. */ }
  finally { isSending = false; setComposerBusy(false); if (origin === sessionId) focusAfterSave?.focus({preventScroll: true}); }
}

async function undoLastField() {
  const change = undoChange;
  if (!change || change.sessionId !== sessionId || change.revision !== state.revision) { showToast("订单已有其他修改，请重新核对字段后编辑。", "error"); return; }
  undoChange = null;
  await changeField(change.field, change.before == null || change.before === "" ? "clear" : "set", change.before);
}

async function focusItem(itemId) {
  if (isSending || orderWritePending) return;
  const epoch = ++viewEpoch;
  viewLoading = true;
  fileFeedback = null; clearPdfPreview();
  try { render(await api("/api/focus", {sessionId, itemId, expectedRevision: state.revision}), false); }
  catch { /* Persistent error already displayed. */ }
  finally { if (epoch === viewEpoch) { viewLoading = false; setComposerBusy(false); } }
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
  closeHistoryContextMenu();
  sidebarCollapsed = Boolean(collapsed);
  $("#workspace")?.classList.toggle("sidebar-collapsed", sidebarCollapsed);
  const button = $("#sidebar-toggle");
  if (button) {
    button.setAttribute("aria-label", sidebarCollapsed ? "展开导航栏" : "收起导航栏");
    button.title = sidebarCollapsed ? "展开导航栏" : "收起导航栏";
    button.dataset.collapsed = String(sidebarCollapsed);
    button.setAttribute("aria-expanded", String(!sidebarCollapsed));
  }
  if (!DESKTOP_QUERY.matches) {
    if (!sidebarCollapsed) $(".sidebar").querySelector("button, input, summary")?.focus();
    else if ($(".sidebar").contains(document.activeElement)) button?.focus();
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

function activateSettingsView(view = "overview", {focus = false} = {}) {
  const nav = $("#settings-nav");
  const panels = document.querySelectorAll("[data-settings-panel]");
  const next = nav?.querySelector(`[data-settings-view="${view}"]`) ? view : "overview";
  settingsView = next;
  nav?.querySelectorAll("[data-settings-view]").forEach((button) => {
    const selected = button.dataset.settingsView === next;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  panels.forEach((panel) => { panel.hidden = panel.dataset.settingsPanel !== next; });
  const catalogPanel = $("#settings-panel-products .catalog-panel");
  const toolPanel = $("#settings-panel-tools .tool-panel");
  if (catalogPanel) catalogPanel.open = next === "products";
  if (toolPanel) toolPanel.open = next === "tools";
  if (focus) nav?.querySelector(`[data-settings-view="${next}"]`)?.focus();
}

function closeSettingsDialog() {
  const dialog = $("#settings-dialog");
  if (dialog?.open) dialog.close();
}

function settingsNavigationKeydown(event) {
  const buttons = [...$("#settings-nav")?.querySelectorAll("[data-settings-view]") || []];
  if (!buttons.length || !["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const current = Math.max(0, buttons.indexOf(document.activeElement));
  const index = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
    : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
  activateSettingsView(buttons[index].dataset.settingsView, {focus: true});
}

function isFeedNearBottom() {
  const feed = $("#chat-feed");
  return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
}

function scrollFeedToBottom() {
  const feed = $("#chat-feed");
  feed.scrollTop = feed.scrollHeight;
}

let markdownFallbackReported = false;

/* 渲染器缺失最典型的成因：本地服务是在本次更新前启动的，静态白名单里没有 markdown.js（404）。
   这类失败必须可见，否则气泡会安静地退回纯文本，看起来像"功能没生效"。 */
function reportMissingMarkdownRenderer() {
  if (markdownFallbackReported) return;
  markdownFallbackReported = true;
  console.warn("[PrintOps] markdown.js 未加载，气泡已回退为纯文本。请重启本地服务（python3 server.py）并强制刷新页面。");
  showToast("Markdown 渲染器未加载：请重启本地服务后刷新页面", "error");
}

/* 气泡正文统一交给 markdown.js 渲染；脚本未加载时退回纯文本，保证内容不丢。 */
function renderMessageBody(node, text) {
  const value = String(text ?? "");
  const markdown = window.PrintOpsMarkdown;
  if (markdown?.render) node.innerHTML = markdown.render(value);
  else {
    node.textContent = value;
    reportMissingMarkdownRenderer();
  }
}

/* 输入框预览只影响展示：发送给后端的始终是 #message-input 里的原始文本。 */
function renderComposerPreview() {
  const input = $("#message-input");
  const toggle = $("#preview-toggle");
  const preview = $("#composer-preview");
  const body = $("#composer-preview-body");
  if (!input || !toggle || !preview || !body) return;
  const hasContent = Boolean(input.value.trim());
  toggle.hidden = !hasContent;
  const active = hasContent && toggle.getAttribute("aria-pressed") === "true";
  preview.hidden = !active;
  if (active) renderMessageBody(body, input.value);
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
  bubble.className = "bubble markdown-body";
  renderMessageBody(bubble, text);
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

function releaseArchivedControls() {
  document.querySelectorAll("[data-archived-disabled]").forEach((control) => {
    control.disabled = control.dataset.archivedDisabled === "true";
    delete control.dataset.archivedDisabled;
  });
}

function applyArchivedState() {
  if (state.status !== "archived" && state.orderStatus !== "archived") {
    releaseArchivedControls();
    return;
  }
  document.querySelectorAll("#message-input, #message-form button[type='submit'], #upload-button, #drop-zone, #file-input, #confirm-order, #generate-order, #quote-refresh, #quote-cancel, #platform-select, #quick-replies button, #tool-list button, #catalog-list button, #parameter-form input, .field-input, .multi-product-editor-field input, .multi-product-focus, .field-confirm, #option-list button, .preset-apply, #request-options, .review-editor input, .review-editor button, .field-review-actions button").forEach((control) => {
    if (!control.hasAttribute("data-archived-disabled")) control.dataset.archivedDisabled = String(control.disabled);
    control.disabled = true;
  });
  $("#memory-status").textContent = "已归档 · 只读，可查看或导出；新建订单后可继续编辑。";
}

function releaseBusyControls() {
  document.querySelectorAll("[data-busy-disabled]").forEach(control => {
    control.disabled = control.dataset.busyDisabled === "true";
    delete control.dataset.busyDisabled;
  });
}

function setComposerBusy(busy) {
  releaseArchivedControls();
  if (!busy) releaseBusyControls();
  if (busy) document.querySelectorAll(".field-input, .multi-product-editor input, .multi-product-focus, .field-confirm, #request-options, .review-editor input, .review-editor button, .field-review-actions button, #next-task-action, .choose-option, #drop-zone, #generate-order, #confirm-order, #quote-cancel, #reset-order").forEach(control => {
    if (!control.hasAttribute("data-busy-disabled")) control.dataset.busyDisabled = String(control.disabled);
    control.disabled = true;
  });
  const input = $("#message-input");
  const send = $("#message-form button[type='submit']");
  const upload = $("#upload-button");
  const quoteRefresh = $("#quote-refresh");
  const label = send?.querySelector(".send-label");
  if (input) input.disabled = busy;
  if (send) {
    send.disabled = busy;
    send.classList.toggle("is-loading", busy);
    send.setAttribute("aria-busy", String(busy));
  }
  if (label) label.textContent = busy ? "处理中" : "发送";
  if (upload) upload.disabled = busy;
  if (quoteRefresh) quoteRefresh.disabled = busy;
  const platform = $("#platform-select");
  if (platform) platform.disabled = busy;
  document.querySelectorAll("#quick-replies button, #tool-list button, #catalog-list button, #parameter-form input, #preview-toggle").forEach((item) => { item.disabled = busy; });
  if (busy) {
    $("#agent-status").innerHTML = '<span class="status-dot thinking"></span>正在处理';
    $("#agent-trace").textContent = "正在分析需求…";
  }
  applyArchivedState();
}

function render(data, showMessages = true) {
  if (data[responseView] !== undefined && data[responseView] !== viewEpoch) return;
  if (data.sessionId === sessionId && Number.isInteger(data.revision) && data.revision < state.revision) return;
  // Release both overlays before computing availability for the new snapshot.
  releaseArchivedControls();
  releaseBusyControls();
  if (data.sessionId && data.sessionId !== sessionId) state = {...state, status: null, orderStatus: null};
  state = { ...state, ...data, order: data.order || state.order };
  trackOrderChanges(state.order);
  if (data.llm) renderSettings({ llm: data.llm });
  sessionId = data.sessionId || sessionId;
  if (sessionId) {
    localStorage.setItem("printops_session", sessionId);
    $("#order-number").textContent = `订单 ${sessionId.toUpperCase()}`;
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
  renderProviderStatus();
  renderPriceEstimate(data.toolResult);
  renderRunTrace();
  renderProgress();
  PrintOpsJourney.render(state);
  rememberOrderHistory();
  renderOrderHistory();
  renderSpecPresets();
  $("#agent-trace").textContent = state.workflowLabel || "本地订单整理";
  $("#memory-status").textContent = data.nextAction || (data.stage === "confirm" ? "订单记忆已锁定，等待人工确认。" : data.order?.productType ? "订单记忆已保存，可继续补充或修改。" : "等待第一条需求，Agent 将自动建立订单记忆。");
  $("#agent-status").innerHTML = '<span class="status-dot"></span>智能体在线';
  applyArchivedState();
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
  const protocolLabel = { native_tools: "原生工具", json_fallback: "JSON 兼容", error: "连接异常" }[llmSettings.protocolMode] || "自动协议";
  const stateText = $("#settings-state");
  if (stateText) {
    stateText.textContent = enabled
      ? (hasError ? `已配置：${llmSettings.model}（上次调用失败，已回退 · ${protocolLabel}）` : `已启用：${llmSettings.model} · ${protocolLabel}`)
      : "当前使用规则模式";
    stateText.classList.remove("is-ready", "is-warning", "is-error");
  }
  const note = $("#settings-note");
  if (note) note.textContent = data?.keyStorageWarning
    || (llmSettings.privateHostsAllowed
      ? "当前已允许访问本机或公司内网模型地址；不要把此服务暴露到公网。Key 仅由本机服务保存。"
      : "Key 仅由本机服务保存，不会显示在页面或接口响应中。内网模型地址默认拦截；URL 与模型留空即可继续使用规则模式。");
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
  if (button) {
    button.dataset.interfaceState = hasError ? "error" : enabled ? "enabled" : "rules";
    button.title = hasError ? "打开设置与能力（接口异常）" : enabled ? "打开设置与能力（接口已启用）" : "打开设置与能力";
  }
  const capabilities = $("#settings-capabilities");
  if (capabilities) capabilities.hidden = true;
}

function modelCapabilityPresentation(test = {}) {
  if (test.agentReady && test.chosenProtocol === "native_tools") {
    return { label: "Agent 工具链可用 · 原生工具", tone: "ready" };
  }
  if (test.agentReady && test.chosenProtocol === "json_fallback") {
    return { label: "Agent 工具链可用 · JSON 兼容", tone: "ready" };
  }
  if (test.chosenProtocol === "chat_only" || (test.chat && !test.agentReady)) {
    return { label: "仅支持聊天 · 不满足 Agent 协议", tone: "warning" };
  }
  return { label: "接口不可用", tone: "error" };
}

function renderModelCapabilities(test = {}) {
  const presentation = modelCapabilityPresentation(test);
  const latency = Number.isFinite(Number(test.latencyMs)) ? ` · ${Number(test.latencyMs)}ms` : "";
  const stateText = $("#settings-state");
  if (stateText) {
    stateText.textContent = `${presentation.label}${latency}（尚未保存）`;
    stateText.classList.remove("is-ready", "is-warning", "is-error");
    stateText.classList.add(`is-${presentation.tone}`);
  }

  const setCapability = (selector, supported, detail = "") => {
    const node = $(selector);
    if (!node) return;
    node.textContent = detail || (supported ? "支持" : "不支持");
    node.className = supported ? "is-supported" : "is-unsupported";
  };
  setCapability("#capability-agent", Boolean(test.agentReady),
    test.agentReady ? "可用" : "不可用");
  setCapability("#capability-json", Boolean(test.jsonPlan));
  setCapability("#capability-native", Boolean(test.nativeTools),
    test.nativeTools && !test.toolContinuation ? "可调用，续接失败" : "");
  setCapability("#capability-chat", Boolean(test.chat));
  const capabilities = $("#settings-capabilities");
  if (capabilities) capabilities.hidden = false;
  return presentation;
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

// Compact, keyboard-reachable status/help markers keep the order pane readable
// while preserving the explanation that used to occupy a full line.
function makeTooltipIcon(text, className, ariaLabel = text, glyph = "?") {
  const icon = document.createElement("span");
  icon.className = `tooltip-icon ${className}`;
  icon.textContent = glyph;
  icon.setAttribute("role", "img");
  icon.setAttribute("tabindex", "0");
  icon.setAttribute("aria-label", ariaLabel);
  icon.title = text;
  icon.dataset.tooltip = text;
  return icon;
}

function renderFieldList(selector, keys, editable = false) {
  const list = $(selector);
  list.innerHTML = "";
  keys.forEach((key) => {
    const current = getFieldValue(key);
    const perItem = key === "deadline" && (state.order?.items?.length || 0) > 1 && !hasValue(current);
    const missing = !perItem && !hasValue(current) && isRequiredField(key);
    const row = document.createElement("div");
    row.className = `field-row ${missing ? "required-missing" : ""} ${updatedOrderKeys.has(key) ? "updated" : ""}`;
    const label = document.createElement("span");
    label.className = "field-label";
    if (missing) label.append(makeTooltipIcon("必填，补充后才能生成订单", "field-status-icon", "必填，补充后才能生成订单", "!"));
    label.append(document.createTextNode(labels[key]));
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
      valueNode.dataset.fieldPath = key;
      valueNode.addEventListener("change", () => commitFieldEdit(key, valueNode));
    } else {
      valueNode = document.createElement("span");
      valueNode.className = `field-value ${hasValue(current) ? "" : "empty"}`;
      valueNode.textContent = current || (perItem ? "按产品分别设置" : missing ? "待补充" : "未设置");
    }
    valueWrap.append(valueNode);
    if (meta && (meta.status === "pending_confirmation" || Number(meta.confidence) < .75)) {
      const confirm = document.createElement("button");
      confirm.type = "button";
      confirm.className = "field-confirm";
      confirm.textContent = "确认此值";
      confirm.addEventListener("click", () => changeField(reviewField(key), "confirm"));
      valueWrap.append(confirm);
    }
    row.append(label, valueWrap);
    list.appendChild(row);
  });
}

function commitFieldEdit(key, input) {
  let value = input.value.trim();
  try { if (key === "size") value = PrintOpsWorkbench.manualDimension(value); }
  catch (error) { showToast(error.message, "error"); return; }
  const current = getFieldValue(key);
  if (value === (current || "")) return;
  if (isSending) {
    input.value = current || "";
    return;
  }
  changeField(reviewField(key), value ? "set" : "clear", value);
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
    const valueNode = document.createElement("input");
    valueNode.className = "field-input";
    valueNode.value = current;
    valueNode.setAttribute("aria-label", label);
    valueNode.dataset.fieldPath = `dimensions.${key}`;
    valueNode.addEventListener("change", () => changeField(reviewField(`dimensions.${key}`), valueNode.value.trim() ? "set" : "clear", valueNode.value.trim()));
    const meta = fieldProvenance(`dimensions.${key}`);
    const valueWrap = document.createElement("span");
    valueWrap.className = "field-value-wrap";
    valueWrap.append(valueNode);
    if (meta?.status === "pending_confirmation" || Number(meta?.confidence) < .75) valueWrap.append(PrintOpsJourney.button("确认此值", () => changeField(reviewField(`dimensions.${key}`), "confirm"), "field-confirm"));
    row.append(labelNode, valueWrap);
    list.appendChild(row);
  });
}

async function updateMultiItemField(index, key, value, kind = "base") {
  const path = kind === "spec" ? `productSpecs.${key}` : kind === "dimension" ? `dimensions.${key}` : key;
  await changeField(reviewField(path, state.order.items[index]?.itemId), String(value || "").trim() ? "set" : "clear", String(value || "").trim());
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
    const dimensionFields = Object.entries(dimensionLabels).filter(([key]) => key !== "finishedSize").map(([key, label]) => [key, label, "dimension", false]);
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
      input.value = currentValue || "";
      input.dataset.fieldPath = kind === "spec" ? `productSpecs.${key}` : kind === "dimension" ? `dimensions.${key}` : key;
      input.dataset.itemId = item.itemId;
      input.placeholder = required ? `请填写${label}` : label;
      input.addEventListener("change", () => updateMultiItemField(index, key, input.value, kind));
      field.append(name, input);
      const review = reviewField(input.dataset.fieldPath, item.itemId);
      if (review.provenance) field.append(PrintOpsWorkbench.evidence(review.provenance));
      if (review.status === "pending_confirmation") field.append(PrintOpsJourney.button("确认此值", () => changeField(review, "confirm"), "field-confirm"));
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
      focusItem(item.itemId);
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
  $("#order-export").hidden = !state.handoffDocument?.exportable;
  $("#export-label").textContent = confirmed ? "导出已确认交接单" : "导出未确认草稿";
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

function renderProviderStatus() {
  const panel = $("#provider-status");
  const provider = providers.find((entry) => entry.id === state.order.platform);
  panel.textContent = `${provider?.name || "供应商"}：${PrintOpsWorkbench.providerStatus(provider)}。${provider?.verified ? "数据已经验证" : "生产能力、价格与交期均待供应商确认"}`;
  panel.title = `来源：${provider?.source?.description || provider?.source || "本地字段模板"}；更新时间：${provider?.updatedAt || "未注明"}；有效期：${provider?.expiresAt || "未验证"}`;
}

function renderPriceEstimate(result) {
  const panel = $("#estimate-panel");
  if (renderPriceEstimate.sessionId !== sessionId) renderPriceEstimate.cached = null;
  if (result?.type === "estimate") {
    renderPriceEstimate.cached = {result, revision: state.revision, itemId: result.itemId || state.activeItemId || null};
    renderPriceEstimate.sessionId = sessionId;
  }
  const cached = renderPriceEstimate.cached;
  if (!cached) { panel.hidden = true; return; }
  const activeItemId = state.activeItemId || state.order?.items?.[state.activeItemIndex]?.itemId || null;
  if (cached.itemId !== activeItemId) { panel.hidden = true; return; }
  const stale = cached.revision !== state.revision;
  const estimate = cached.result;
  panel.replaceChildren();
  const title = stale ? "历史示例估算 · 参数已变" : ({ready: "示例估算", partial: "部分费用可算", blocked: "待补充计价依据"}[estimate.status] || "示例估算");
  panel.append(PrintOpsJourney.el("h3", title));
  panel.append(PrintOpsJourney.el("p", estimate.status === "ready" ? estimate.displayRange : estimate.status === "partial" ? `已知费用小计 ¥${estimate.knownSubtotal || "0.00"}，不代表总价` : "当前不提供完整价格区间", "estimate-total"));
  const item = state.order?.items?.find(item => item.itemId === cached.itemId);
  panel.append(PrintOpsJourney.el("p", `${item ? `${item.productType || "产品"} · ${cached.itemId} · ` : ""}订单版本 ${cached.revision} · 不含税 / 出厂 · 不含运输、打样及文件修改`, "estimate-scope"));
  if (stale) panel.append(PrintOpsJourney.el("p", "原金额已过期，不能作为当前订单估算。"));
  panel.append(PrintOpsJourney.button(stale ? "重新估算" : "更新估算", () => callTool("estimate_price")));
  panel.append(PrintOpsWorkbench.priceDetails(estimate));
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
  $("#product-profile-readiness").textContent = profile.missing?.length ? `还有 ${profile.missing.length} 项待补充` : "品类参数已填写";
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
    const hintText = item.hint || (item.required ? "必填参数" : "可选参数");
    if (!item.filled && item.required) label.append(makeTooltipIcon("必填，补充后才能生成订单", "field-status-icon", "必填，补充后才能生成订单", "!"));
    label.append(document.createTextNode(item.label));
    if (item.required) {
      const requiredMark = document.createElement("span");
      requiredMark.className = "required-mark";
      requiredMark.textContent = "*";
      label.append(document.createTextNode(" "), requiredMark);
    }
    if (item.hint) label.append(makeTooltipIcon(hintText, "parameter-hint-trigger", `${item.label}说明`));
    const input = document.createElement("input");
    input.id = `parameter-${item.key}`;
    input.value = item.value || "";
    input.placeholder = item.question || item.label;
    input.setAttribute("aria-describedby", `parameter-hint-${item.key}`);
    if (!item.filled && item.required) input.setAttribute("aria-invalid", "true");
    input.dataset.fieldPath = `productSpecs.${item.key}`;
    input.addEventListener("change", () => changeField(reviewField(input.dataset.fieldPath), input.value.trim() ? "set" : "clear", input.value.trim()));
    input.addEventListener("input", () => {
      if (!input.value.trim()) return;
      field.classList.remove("missing");
      input.removeAttribute("aria-invalid");
    });
    const hint = document.createElement("small");
    hint.className = "parameter-hint sr-only";
    hint.id = `parameter-hint-${item.key}`;
    const provenance = fieldProvenance(`productSpecs.${item.key}`);
    hint.textContent = hintText;
    field.append(label, input, hint, PrintOpsWorkbench.evidence(provenance));
    if (provenance?.status === "pending_confirmation" || Number(provenance?.confidence) < .75) field.append(PrintOpsJourney.button("确认此值", () => changeField(reviewField(input.dataset.fieldPath), "confirm"), "field-confirm"));
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
  const comparable = options.length >= 2;
  if (note) {
    note.hidden = !comparable;
    if (comparable) {
      const order = state.order.items?.[state.activeItemIndex] || state.order;
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

async function chooseOption(optionId) {
  const origin = document.activeElement;
  const selectionFocus = origin?.classList.contains("choose-option");
  const originSession = sessionId, originEpoch = viewEpoch;
  try {
    const data = await api("/api/choose", { sessionId, optionId,
      itemIndex: Array.isArray(state.order?.items) && state.order.items.length > 1 ? state.activeItemIndex : undefined });
    const restoreFocus = selectionFocus && originSession === sessionId && originEpoch === viewEpoch &&
      $("#workspace").dataset.view === "compare" && (document.activeElement === origin || document.activeElement === document.body);
    render(data);
    if (restoreFocus) $("#phase-compare").focus({preventScroll: true});
  }
  catch (error) { showToast(apiErrorText(error, "方案选择暂时失败，请重试"), "error"); }
}

function renderFileState() {
  const node = $("#file-state");
  const items = Array.isArray(state.order?.items) ? state.order.items : [];
  const activeItem = items.length > 1 && Number.isInteger(state.activeItemIndex) ? items[state.activeItemIndex] : null;
  const storedFile = activeItem ? activeItem.uploadedFile : state.uploadedFile;
  const storedCheck = activeItem ? state.preflightResults?.[activeItem.itemId] : state.preflightResults;
  const feedback = fileFeedback || (storedFile ? { ok: Boolean(storedCheck?.ok && !storedCheck?.stale), fileName: storedFile,
    message: storedCheck?.stale ? "规格已修改，原基础检查已过期，请重新选择文件检查。" : storedCheck?.ok ? "基础检查已通过；不替代专业印前验收。" : "文件已记录，基础检查仍需核对。" } : null);
  const activePreview = pdfPreview
    && (items.length <= 1 || storedFile === pdfPreview.fileName)
    && (!feedback || pdfPreview.fileName === feedback.fileName) ? pdfPreview : null;
  node.hidden = !feedback && !activePreview;
  if (!feedback && !activePreview) return;
  node.className = `file-state ${feedback?.ok && activePreview?.preflight?.ok !== false ? "ok" : "error"}`;
  node.innerHTML = "";
  const heading = document.createElement("div");
  heading.className = "file-state-heading";
  const name = document.createElement("strong");
  name.textContent = activePreview?.fileName || feedback?.fileName || "未命名文件";
  heading.append(name);
  heading.append(PrintOpsJourney.el("small", `所属产品：${activeItem?.productType || state.order.productType || "当前订单"} · ${storedCheck?.updatedAt ? `检查时间 ${formatFileTime(storedCheck.updatedAt)}` : "尚无有效检查时间"}`));
  if (feedback?.message) heading.append(PrintOpsJourney.el("p", feedback.message));
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
  if (!root) return;
  const tools = (state.availableTools || []).filter((item) => toolLabels[item.name]);
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

function renderCatalog(categories = []) {
  const root = $("#catalog-list");
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
    button.addEventListener("click", () => { closeSettingsDialog(); sendMessage(categoryExamples[category] || `我想做${category}`); });
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
  let percent = Number.isFinite(readiness) && readiness > 0 ? Math.min(100, Math.round(readiness)) : fallback;
  const items = state.order?.items || [];
  if (items.length > 1) {
    const fields = (state.review?.fields || []).filter(field => field.itemId && field.required);
    percent = fields.length ? Math.round(100 * fields.filter(field => hasValue(field.value) && !["missing", "pending_confirmation", "inferred", "stale"].includes(field.status)).length / fields.length)
      : Math.round(100 * items.reduce((count, item) => count + required.filter(key => hasValue(item[key])).length, 0) / (items.length * required.length));
  }
  $("#progress-stage").textContent = state.workflowLabel || ({ collect: "需求收集", clarify: "品类澄清", recommend: "方案选择", preflight: "文件预检", quote: "报价准备", confirm: "确认订单", export: "导出交接" })[workflowStage] || "需求收集";
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
  renderComposerPreview();
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
    $("#agent-status").textContent = error.status === 409 ? "需要核对最新版本" : "操作尚未完成";
    $("#message-input").value = text;
    autoGrowComposer();
    showToast("未完成的输入已保留；恢复连接后可修改并重新发送", "error");
  }
  finally {
    if (requestSeq !== chatRequestSeq) return;
    activeChatController = null;
    isSending = false;
    setComposerBusy(false);
    if (!patch) $("#message-input").focus();
  }
}

function autoGrowComposer() {
  const input = $("#message-input");
  if (!input) return;
  input.style.height = "auto";
  input.style.height = `${Math.min(120, Math.max(38, input.scrollHeight))}px`;
}

async function callTool(toolName, payload = {}) {
  const epoch = viewEpoch;
  try {
    if (toolName === "explain_print_term" && !payload.question) {
      payload = { question: state.order.productType ? `${state.order.productType}的纸张和工艺怎么选？` : "纸张和印刷工艺怎么选？" };
    }
    const result = await api("/api/tools/call", { sessionId, toolName, payload });
    if (epoch !== viewEpoch) return;
    render(result);
    showToast(`已调用：${toolLabels[toolName]?.[0] || toolName}`);
  } catch (error) {
    if (epoch === viewEpoch) showToast(apiErrorText(error, "工具调用失败，请确认本地服务已启动，或先刷新会话"), "error");
  }
}

$("#message-form").addEventListener("submit", (event) => { event.preventDefault(); sendMessage($("#message-input").value); });
$("#message-input").addEventListener("input", () => { autoGrowComposer(); renderComposerPreview(); });
$("#preview-toggle").addEventListener("click", () => {
  const toggle = $("#preview-toggle");
  toggle.setAttribute("aria-pressed", String(toggle.getAttribute("aria-pressed") !== "true"));
  renderComposerPreview();
});
$("#message-input").addEventListener("keydown", (event) => {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#message-form").requestSubmit(); }
});
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
  try {
    const document = await currentHandoff();
    await navigator.clipboard.writeText(document.markdown);
    showToast("当前版本交接单已复制");
  } catch (error) { showToast(error.message || "复制失败", "error"); }
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

async function currentHandoff() {
  const id = sessionId, revision = state.revision;
  const document = await api(`/api/orders/${encodeURIComponent(id)}/handoff?revision=${revision}`, undefined, "GET");
  if (id !== sessionId || revision !== state.revision) throw new Error("订单已变化，请重新核对并导出当前版本。");
  if (!document.exportable) throw new Error("交接单尚未就绪，请处理待确认字段并重新生成。");
  PrintOpsJourney.renderDocument(document);
  return document;
}

async function exportOrder(format) {
  try {
    const document = await currentHandoff();
    if (format === "print") { PrintOpsJourney.view("review"); window.print(); return; }
    let content, extension, contentType;
    if (format === "json") {
      content = JSON.stringify(document, null, 2); extension = "json"; contentType = "application/json";
    } else if (format === "csv") {
      const escape = (value) => {
        let text = String(value ?? "");
        if (/^[\s\uFEFF]*[=+@-]|^[\t\r\n]/.test(text)) text = "'" + text;
        return '"' + text.replaceAll('"', '""') + '"';
      };
      const documentStatuses = {blocked: "需求整理中，禁止作为生产交接单导出", stale: "已失效，请重新生成", draft: "未确认草稿", confirmed: "已本地确认", unconfirmed: "未确认"};
      const fieldStatuses = {missing: "待补充", pending_confirmation: "候选待核对", confirmed: "已核对", filled: "已填写", observed: "已填写", inferred: "推断待核对", stale: "已失效"};
      const fileStatuses = {pending_delivery: "文件待交付", stale: "检查已过期", checked: "已完成基础检查", needs_review: "需核对"};
      const checkStatuses = {ok: "通过", warn: "需关注", error: "未通过", info: "信息", unknown: "未确认"};
      const rows = [];
      const add = (group, item, path, label, value, status = "", fieldId = "") => rows.push([
        group, item?.itemId || "", item ? (item.itemIndex == null ? "整单" : item.itemIndex + 1) : "", item?.title || "",
        path, fieldId, label, value == null ? "未记录" : typeof value === "boolean" ? (value ? "是" : "否") : value, status
      ]);
      for (const [path, label, value] of [
        ["schemaVersion", "交接文档结构版本", document.schemaVersion], ["documentType", "文档类型", document.documentType],
        ["orderId", "订单", document.orderId], ["revision", "订单版本", document.revision],
        ["status", "交接状态", documentStatuses[document.status] || "待核对"], ["exportable", "允许导出", document.exportable],
        ["unitSystem", "尺寸单位", document.unitSystem], ["generatedAt", "生成时间", document.generatedAt],
        ["confirmedAt", "确认时间", document.confirmedAt ?? "尚未确认"], ["confirmedRevision", "确认版本", document.confirmedRevision ?? "无"],
        ["confirmation.status", "人工确认状态", documentStatuses[document.confirmation?.status] || "待核对"],
        ["confirmation.scope", "确认范围", document.confirmation?.scope], ["sent", "供应商发送", document.sent ? "已发送" : "尚未发送"]
      ]) add("交接信息", null, path, label, value);
      for (const item of document.items || []) {
        for (const [path, label] of [["itemId", "产品标识"], ["itemIndex", "产品索引（从0开始）"], ["title", "产品名称"]]) add("产品信息", item, path, label, item[path]);
        for (const field of item.fields || []) {
          add("生产规格", item, field.key, field.label, field.value ?? "待补充", fieldStatuses[field.status] || "待核对", field.fieldId);
          if (field.aliases) add("字段依据", item, `${field.key}.aliases`, `${field.label}同义字段`, JSON.stringify(field.aliases), "", field.fieldId);
          if (field.semanticConflict != null) add("字段依据", item, `${field.key}.semanticConflict`, `${field.label}同义字段冲突`, field.semanticConflict, "", field.fieldId);
        }
        add("工艺方案", item, "selectedOption", "已选方案标识", item.selectedOption ?? "未选择");
        if (item.selectedOptionDetails) {
          for (const [key, label] of [["id", "方案标识"], ["title", "方案名称"], ["printMode", "印刷方式"], ["composition", "方案组成"]]) {
            add("工艺方案", item, `selectedOptionDetails.${key}`, label, item.selectedOptionDetails[key]);
          }
        } else add("工艺方案", item, "selectedOptionDetails", "工艺方案详情", "未选择");
        const file = item.file || {};
        for (const [key, label] of [["fileName", "文件名"], ["itemId", "文件所属产品"], ["checkedAt", "文件检查时间"], ["version", "文件检查记录版本"], ["scope", "文件检查范围"], ["limitations", "文件检查限制"]]) {
          add("印刷文件", item, `file.${key}`, label, file[key]);
        }
        add("印刷文件", item, "file.status", "文件检查状态", fileStatuses[file.status] || "待核对");
        add("印刷文件", item, "file.inspectionLevel", "文件检查级别", {basic: "文件基本属性检查", metadata: "文件基本属性与已提供的 PDF 元数据检查"}[file.inspectionLevel] || "未执行基础检查");
        (file.checks || []).forEach((check, index) => add("文件基础检查", item, `file.checks.${index}`, check.label, check.detail, checkStatuses[check.status] || "未确认"));
        (file.warnings || []).forEach((warning, index) => add("文件复核提醒", item, `file.warnings.${index}`, "提醒", warning));
      }
      const issueLabels = {id: "事项标识", kind: "事项类型", field: "关联字段", fieldId: "字段标识", fieldPath: "字段路径", label: "事项名称", itemId: "产品标识", itemIndex: "产品索引（从0开始）", blocking: "是否阻断", section: "处理阶段", message: "事项说明", action: "处理动作"};
      (document.issues || []).forEach((issue, index) => {
        const item = document.items?.find(item => item.itemId === issue.itemId);
        for (const [key, value] of Object.entries(issue)) add("交接待办", item, `issues.${index}.${key}`, issueLabels[key] || key, value);
      });
      (document.notes || []).forEach((note, index) => add("交接时说明", null, `notes.${index}`, "说明", note));
      content = [["分组", "产品标识", "产品序号", "产品项", "字段路径", "字段标识", "字段", "内容", "状态"], ...rows].map(row => row.map(escape).join(",")).join("\n");
      extension = "csv"; contentType = "text/csv";
    } else { content = document.markdown; extension = "md"; contentType = "text/markdown"; }
    const url = URL.createObjectURL(new Blob([(format === "csv" ? "\ufeff" : "") + content], {type: contentType + ";charset=utf-8"}));
    const link = window.document.createElement("a");
    link.href = url;
    link.download = `printops-handoff-${document.orderId}-r${document.revision}.${extension}`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast(`已生成 ${extension.toUpperCase()} 下载文件，请打开核对；尚未发送供应商。`);
  } catch (error) { showToast(error.message || "交接单导出失败", "error"); }
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
  if (isSending || orderWritePending) return;
  const uploadScope = {sessionId, expectedRevision: state.revision, itemId: state.order?.items?.[state.activeItemIndex]?.itemId};
  if (state.order?.items?.length > 1 && !uploadScope.itemId) {
    showToast("请先在订单摘要中选择处理的产品项，再上传文件。", "error");
    return;
  }
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
      ...uploadScope,
      fileName: file.name,
      sizeBytes: file.size,
      pageCount: inspection.pageCount,
      encrypted: inspection.encrypted,
      readable: inspection.readable,
      inspection
    });
    if (uploadScope.sessionId !== sessionId) return;
    const currentItemId = state.order?.items?.[state.activeItemIndex]?.itemId;
    if (uploadScope.itemId && currentItemId !== uploadScope.itemId) {
      fileFeedback = null;
      clearPdfPreview();
      render(await api("/api/focus", {sessionId, itemId: currentItemId, expectedRevision: data.revision}), false);
      return;
    }
    fileFeedback = null;
    if (pdfPreview) pdfPreview.preflight = data.toolResult || null;
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
  if (!DESKTOP_QUERY.matches) { $("#workspace").dataset.mobilePane = "order"; updateOrderToggle(); $("#mobile-back").focus(); return; }
  layout.orderCollapsed = !layout.orderCollapsed;
  applyLayout();
  saveLayout();
});
$("#mobile-back").addEventListener("click", () => { closeHistoryContextMenu(); $("#workspace").dataset.mobilePane = "work"; updateOrderToggle(); $("#order-toggle").focus(); });
$("#workspace-backdrop").addEventListener("click", () => setSidebarCollapsed(true));
$("#history-context-menu").addEventListener("click", (event) => {
  const control = event.target.closest("[data-history-menu-action]");
  if (!control) return;
  const menu = $("#history-context-menu");
  const orderId = menu.dataset.orderId;
  closeHistoryContextMenu({restoreFocus: true});
  historyAction(control.dataset.historyMenuAction, orderId);
});
$("#history-list").addEventListener("contextmenu", (event) => {
  const item = event.target.closest(".history-item");
  if (!item) return;
  event.preventDefault();
  openHistoryContextMenu(item.dataset.sessionId, item, {x: event.clientX, y: event.clientY});
});
document.addEventListener("keydown", (event) => {
  if (document.querySelector("dialog[open]")) return;
  const menu = $("#history-context-menu");
  if (menu && !menu.hidden) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeHistoryContextMenu({restoreFocus: true});
      return;
    }
    if (event.key === "ArrowDown") { event.preventDefault(); moveHistoryMenuFocus(1); return; }
    if (event.key === "ArrowUp") { event.preventDefault(); moveHistoryMenuFocus(-1); return; }
    if (event.key === "Home") { event.preventDefault(); moveHistoryMenuFocus("first"); return; }
    if (event.key === "End") { event.preventDefault(); moveHistoryMenuFocus("last"); return; }
    if (event.key === "Tab") { event.preventDefault(); moveHistoryMenuFocus(event.shiftKey ? -1 : 1); return; }
  }
  const focusedHistoryItem = document.activeElement?.closest?.(".history-item");
  if (focusedHistoryItem && ((event.shiftKey && event.key === "F10") || event.key === "ContextMenu" || event.key === "Apps")) {
    event.preventDefault();
    openHistoryContextMenu(focusedHistoryItem.dataset.sessionId, focusedHistoryItem);
    return;
  }
  if (!DESKTOP_QUERY.matches && !sidebarCollapsed) {
    if (event.key === "Escape") { setSidebarCollapsed(true); $("#sidebar-toggle").focus(); }
    if (event.key === "Tab") {
      const nodes = [...$(".sidebar").querySelectorAll('button, input, summary, [tabindex="0"]')].filter(node => {
        if (node.disabled || !node.getClientRects().length) return false;
        for (let detail = node.closest("details:not([open])"); detail; detail = detail.parentElement?.closest("details:not([open])")) {
          if (!detail.querySelector(":scope > summary")?.contains(node)) return false;
        }
        return true;
      });
      const first = nodes[0], last = nodes.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  }
});
let wasDesktop = DESKTOP_QUERY.matches;
function syncPaneViewport() {
  closeHistoryContextMenu();
  if (wasDesktop && !DESKTOP_QUERY.matches) setSidebarCollapsed(true);
  wasDesktop = DESKTOP_QUERY.matches;
  updateOrderToggle();
  updateBackdrop();
}
window.addEventListener("resize", syncPaneViewport);
DESKTOP_QUERY.addEventListener("change", syncPaneViewport);
$("#theme-toggle").addEventListener("click", () => setHighContrast(document.documentElement.dataset.theme !== "high-contrast"));
$("#settings-nav").addEventListener("click", (event) => {
  const button = event.target.closest("[data-settings-view]");
  if (button) activateSettingsView(button.dataset.settingsView, {focus: true});
});
$("#settings-nav").addEventListener("keydown", settingsNavigationKeydown);
$("#history-include-archived").addEventListener("change", (event) => refreshOrderHistory(event.currentTarget.checked));
$("#history-list").addEventListener("click", (event) => {
  const hoverAction = event.target.closest("[data-history-action]");
  if (hoverAction) {
    event.stopPropagation();
    historyAction(hoverAction.dataset.historyAction, hoverAction.dataset.orderId);
    return;
  }
  const item = event.target.closest(".history-item");
  if (item) switchOrder(item.dataset.sessionId);
});
$("#history-list").addEventListener("pointerdown", (event) => {
  if (event.target.closest(".history-item")) closeHistoryContextMenu();
});
document.addEventListener("pointerdown", (event) => {
  const menu = $("#history-context-menu");
  if (!menu || menu.hidden) return;
  if (!menu.contains(event.target)) closeHistoryContextMenu();
});
$("#save-preset").addEventListener("click", saveCurrentSpecPreset);
$("#preset-list").addEventListener("click", (event) => {
  const apply = event.target.closest(".preset-apply");
  if (apply) applySpecPreset(apply.dataset.presetId);
  const remove = event.target.closest(".preset-remove");
  if (remove) removeSpecPreset(remove.dataset.presetId);
});
$("#reset-order").addEventListener("click", () => {
  const dialog = $("#confirm-reset-dialog");
  dialog.returnValue = "";
  dialog.showModal();
});

const confirmResetDialog = $("#confirm-reset-dialog");
confirmResetDialog.addEventListener("close", () => {
  if (confirmResetDialog.returnValue !== "confirm") {
    $("#reset-order").focus({preventScroll: true});
    return;
  }
  if (!DESKTOP_QUERY.matches) setSidebarCollapsed(true);
  (async () => {
    viewEpoch += 1;
    viewLoading = false;
    chatRequestSeq += 1;
    activeChatController?.abort();
    activeChatController = null;
    removePendingMessage();
    isSending = false;
    state.status = null;
    state.orderStatus = null;
    setComposerBusy(false);
    localStorage.removeItem("printops_session");
    $("#chat-feed").innerHTML = "";
    sessionId = "";
    suppressOrderDiff = true;
    fileFeedback = null;
    clearPdfPreview();
    await bootstrap();
    if (!DESKTOP_QUERY.matches) $("#message-input").focus({preventScroll: true});
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
    const presentation = renderModelCapabilities(result.test);
    showToast(presentation.label, presentation.tone === "ready" ? "success" : presentation.tone);
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
  activateSettingsView("overview", {focus: true});
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
    const message = error?.message || "";
    const settingsMessage = message.includes("URL 和模型名")
      ? "URL 和模型名需要同时填写"
      : message.includes("本机或内网")
        ? "该接口解析到本机或内网地址，当前安全策略不允许"
        : message.includes("接口 URL")
          ? message
          : "设置保存失败，请检查接口地址";
    showToast(settingsMessage, "error");
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
  if (location.protocol === "file:") {
    $("#agent-status").textContent = "需要本地服务";
    $("#storage-status") && ($("#storage-status").textContent = "未连接本地存储");
    $("#operation-error").hidden = false;
    $("#operation-error-title").textContent = "请通过本地服务打开 PrintOps";
    $("#operation-error-message").textContent = "在项目目录运行 python3 server.py，然后访问 http://localhost:4174。直接打开 HTML 文件无法保存订单或调用本地工具。";
    $("#operation-error-actions").append(PrintOpsJourney.button("打开本地工作台", () => {location.href = "http://localhost:4174";}));
    PrintOpsJourney.saved("当前未连接本地存储", "error");
    return;
  }
  try {
    const [platforms, products, tools, settings, storage] = await Promise.all([
      api("/api/platforms", undefined, "GET"), api("/api/products", undefined, "GET"),
      api("/api/tools", undefined, "GET"), api("/api/settings", undefined, "GET"), api("/api/storage", undefined, "GET")
    ]);
    storageInfo = storage;
    $("#storage-status") && ($("#storage-status").textContent = storage.persistent ? "本机自动保存 · SQLite" : "仅当前进程保存");
    PrintOpsJourney.saved(storage.persistent ? "本机自动保存 · SQLite" : "仅当前进程保存 · 退出后丢失");
    const platformSelect = $("#platform-select");
    platformSelect.innerHTML = "";
    providers = platforms.platforms;
    platforms.platforms.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = `${item.name} · ${PrintOpsWorkbench.providerStatus(item)}`;
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
    await migrateLocalHistory();
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
  } catch (error) {
    $("#agent-status").textContent = error.status === 401 || error.status === 403 ? "访问凭据需要更新" : "连接尚未就绪";
    $("#storage-status") && ($("#storage-status").textContent = "连接尚未就绪");
    addMessage("assistant", "本地服务未能完成初始化。请查看上方恢复提示；启动命令为 python3 server.py。");
  }
}

PrintOpsJourney.init({
  field: changeField, focus: focusItem, recommend: () => callTool("recommend_processes"),
  generate: () => $("#generate-order").click(), confirm: () => $("#confirm-order").click(), export: exportOrder,
  refreshCredentials,
  syncControls: () => { if (isSending || orderWritePending || viewLoading) setComposerBusy(true); else applyArchivedState(); }
});
setupSplitters();
applyLayout();
autoGrowComposer();
bootstrap();
setSidebarCollapsed(sidebarCollapsed);
setHighContrast(localStorage.getItem("printops_theme") === "high-contrast");
