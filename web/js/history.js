/* Server-backed order history, sidebar list, context menu, settings archive table, and switching. */

import { ORDER_STATUS_LABELS, orderHistoryKey } from "./constants.js";
import { actions, appState, cloneData } from "./store.js";
import { $, showToast } from "./dom.js";
import { api, apiErrorText, recoverOperation } from "./api.js";
import { clearPdfPreview } from "./pdf.js";

export function loadOrderHistory() {
  return appState.serverOrderHistory;
}

export async function refreshOrderHistory(includeArchived = $("#history-include-archived")?.checked === true) {
  const seq = ++appState.historyRequestSeq;
  try {
    const result = await api(`/api/orders?limit=100&includeArchived=${includeArchived ? "true" : "false"}`, undefined, "GET");
    if (seq !== appState.historyRequestSeq) return;
    appState.serverOrderHistory = result.orders.map((entry) => ({...entry, id: entry.orderId}));
    renderOrderHistory();
  } catch {
    $("#history-count").textContent = "同步失败";
  }
}

export function renderSettingsOrders() {
  const root = $("#settings-history-list");
  if (!root) return;
  root.innerHTML = "";
  if (!appState.settingsOrders.length) {
    const empty = document.createElement("p");
    empty.className = "settings-history-empty";
    empty.textContent = "还没有订单记录。开始对话后会自动出现在这里，可归档、导出或永久删除。";
    root.append(empty);
  } else {
    appState.settingsOrders.forEach((item) => {
      const row = document.createElement("div");
      row.className = `settings-history-row${item.status === "archived" ? " is-archived" : ""}`;
      const check = document.createElement("input");
      check.type = "checkbox";
      check.className = "settings-history-check";
      check.dataset.orderId = item.id;
      check.checked = appState.settingsHistorySelection.has(item.id);
      check.setAttribute("aria-label", `选择 ${orderHistoryTitle(item.order)}`);
      const view = document.createElement("button");
      view.type = "button";
      view.className = "settings-history-view";
      view.dataset.orderId = item.id;
      const title = document.createElement("strong");
      title.textContent = orderHistoryTitle(item.order);
      const meta = document.createElement("small");
      meta.textContent = item.status === "archived" ? `已归档 · ${formatOrderHistoryTime(item.updatedAt)}` : formatOrderHistoryTime(item.updatedAt);
      view.append(title, meta);
      const exportAction = document.createElement("button");
      exportAction.type = "button";
      exportAction.className = "settings-history-action";
      exportAction.dataset.historyAction = "export";
      exportAction.dataset.orderId = item.id;
      exportAction.textContent = "导出";
      exportAction.title = "导出订单备份";
      row.append(check, view, exportAction);
      if (item.status !== "archived") {
        const archiveAction = document.createElement("button");
        archiveAction.type = "button";
        archiveAction.className = "settings-history-action";
        archiveAction.dataset.historyAction = "archive";
        archiveAction.dataset.orderId = item.id;
        archiveAction.textContent = "归档";
        archiveAction.title = "归档订单";
        row.append(archiveAction);
      }
      const purgeAction = document.createElement("button");
      purgeAction.type = "button";
      purgeAction.className = "settings-history-action danger";
      purgeAction.dataset.historyAction = "purge";
      purgeAction.dataset.orderId = item.id;
      purgeAction.textContent = "永久删除";
      purgeAction.title = item.status === "archived" ? "永久删除归档订单" : "永久删除订单（先自动归档）";
      row.append(purgeAction);
      root.append(row);
    });
  }
  // Drop selections whose orders no longer exist after a refresh.
  const ids = new Set(appState.settingsOrders.map((item) => item.id));
  for (const id of appState.settingsHistorySelection) {
    if (!ids.has(id)) appState.settingsHistorySelection.delete(id);
  }
  syncSettingsHistoryToolbar();
}

export function syncSettingsHistoryToolbar() {
  const count = $("#settings-history-selection-count");
  const selectAll = $("#settings-history-select-all");
  if (!count || !selectAll) return;
  const selected = appState.settingsHistorySelection.size;
  count.textContent = `已选 ${selected}`;
  selectAll.checked = appState.settingsOrders.length > 0 && selected === appState.settingsOrders.length;
  selectAll.indeterminate = selected > 0 && selected < appState.settingsOrders.length;
  document.querySelectorAll("#settings-history-toolbar [data-history-bulk]").forEach((button) => {
    button.disabled = selected === 0;
  });
}

export async function refreshSettingsOrders() {
  try {
    const result = await api("/api/orders?limit=100&includeArchived=true", undefined, "GET");
    appState.settingsOrders = result.orders.map((entry) => ({...entry, id: entry.orderId}));
  } catch {
    return;
  }
  renderSettingsOrders();
}

export async function migrateLocalHistory() {
  const legacy = localStorage.getItem(orderHistoryKey);
  if (!legacy) return;
  const result = await api("/api/orders/import-local-history", {orders: JSON.parse(legacy)});
  if (!result.errors?.length) localStorage.removeItem(orderHistoryKey);
  else showToast("部分旧历史未迁移，原始备份仍保留在浏览器中", "error");
}

export function orderHasContent(order = {}) {
  return Boolean(order.productType || order.quantity || order.orderGenerated);
}

export function orderHistoryTitle(order = {}) {
  if (Array.isArray(order.items) && order.items.length > 1) {
    const summary = order.items.map((item) => `${item.productType || "产品"}${item.quantity ? ` ${item.quantity}` : ""}`).join("；");
    return `${order.items.map((item) => item.productType || "产品").join("+")} · ${summary}`;
  }
  return order.productType || order.quantity ? `${order.productType || "未分类"} · ${order.quantity || "待定数量"}` : "订单草稿";
}

export function formatOrderHistoryTime(timestamp) {
  return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(timestamp));
}

export function rememberOrderHistory() {
  if (appState.sessionId) refreshOrderHistory();
}

export function renderOrderHistory() {
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
    button.setAttribute("aria-current", String(item.id === appState.sessionId));
    const orderTitle = orderHistoryTitle(item.order);
    const statusLabel = ORDER_STATUS_LABELS[item.status] || "编辑中";
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
    // One-click delete for active orders lives in the right-click menu only:
    // the sidebar rows are ~160px wide, and a third hover button's strip would
    // cover the row's center point and swallow clicks on the row itself.
    row.append(hoverActions);
    button.dataset.historyMenu = item.id;
    button.setAttribute("aria-haspopup", "menu");
    button.setAttribute("aria-controls", "history-context-menu");
    button.setAttribute("aria-expanded", "false");
    root.append(row);
  });
}

export function closeHistoryContextMenu({restoreFocus = false} = {}) {
  const menu = $("#history-context-menu");
  if (!menu) return;
  menu.hidden = true;
  menu.innerHTML = "";
  if (appState.historyMenuTrigger) {
    appState.historyMenuTrigger.setAttribute("aria-expanded", "false");
    if (restoreFocus && appState.historyMenuTrigger.getClientRects().length) appState.historyMenuTrigger.focus({preventScroll: true});
  }
  appState.historyMenuTrigger = null;
}

export function historyContextMenuItems(orderId) {
  const item = appState.serverOrderHistory.find((entry) => entry.id === orderId);
  if (!item) return [];
  const items = [["export", "导出备份"]];
  if (item.status !== "archived") {
    // "永久删除" stays after "归档订单": the keyboard-menu test walks one
    // ArrowDown from the first item onto the archive entry.
    items.push(["archive", "归档订单"], ["purge", "永久删除"]);
  } else {
    items.push(["purge", "永久删除记录"]);
  }
  return items;
}

export function positionHistoryContextMenu(x, y) {
  const menu = $("#history-context-menu");
  if (!menu) return;
  const margin = 8;
  const rect = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(margin, Math.min(x, window.innerWidth - rect.width - margin))}px`;
  menu.style.top = `${Math.max(margin, Math.min(y, window.innerHeight - rect.height - margin))}px`;
}

export function openHistoryContextMenu(orderId, trigger, point = {}) {
  const menu = $("#history-context-menu");
  const items = historyContextMenuItems(orderId);
  if (!menu || !items.length) return;
  closeHistoryContextMenu();
  actions.closePresetDrawer();
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
  appState.historyMenuTrigger = trigger;
  trigger?.setAttribute("aria-expanded", "true");
  menu.hidden = false;
  const anchor = trigger?.getBoundingClientRect();
  const x = Number.isFinite(point.x) ? point.x : (anchor ? anchor.right - menu.offsetWidth : window.innerWidth / 2);
  const y = Number.isFinite(point.y) ? point.y : (anchor ? anchor.bottom + 4 : window.innerHeight / 2);
  positionHistoryContextMenu(x, y);
  menu.querySelector("[role=menuitem]")?.focus();
}

export function moveHistoryMenuFocus(direction) {
  const menu = $("#history-context-menu");
  if (!menu || menu.hidden) return;
  const items = [...menu.querySelectorAll("[role=menuitem]")];
  if (!items.length) return;
  const current = Math.max(0, items.indexOf(document.activeElement));
  const next = direction === "first" ? 0 : direction === "last" ? items.length - 1 : (current + direction + items.length) % items.length;
  items.forEach((item, index) => { item.tabIndex = index === next ? 0 : -1; });
  items[next].focus();
}

// History actions can fire from the sidebar list or the settings history
// panel; the settings cache holds entries the sidebar currently hides.
export function findOrderEntry(orderId) {
  return appState.serverOrderHistory.find((entry) => entry.id === orderId) || appState.settingsOrders.find((entry) => entry.id === orderId);
}

export async function exportOrderBackup(orderId) {
  const result = await api(`/api/orders/${encodeURIComponent(orderId)}/export`, undefined, "GET");
  const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], {type: "application/json;charset=utf-8"}));
  const link = document.createElement("a");
  link.href = url;
  link.download = `printops-backup-${orderId}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// The server only purges archived orders, so deleting an active order archives
// it first and chains the purge with the revision the archive call returned.
export async function purgeOrderEntry(item) {
  let revision = item.revision;
  if (item.status !== "archived") {
    const archived = await api(`/api/orders/${encodeURIComponent(item.id)}/archive`, {expectedRevision: revision});
    revision = archived.revision;
  }
  await api(`/api/orders/${encodeURIComponent(item.id)}/purge`, {expectedRevision: revision, confirm: true});
}

export async function teardownIfCurrentOrder(orderId) {
  if (orderId !== appState.sessionId) return;
  // Mirror the reset-dialog teardown: without the sequence bump and
  // actions.removePendingMessage(), an aborted chat turn leaves its thinking
  // bubble animating on a detached node.
  appState.viewEpoch += 1;
  appState.viewLoading = false;
  appState.chatRequestSeq += 1;
  appState.activeChatController?.abort();
  appState.activeChatController = null;
  appState.stoppedChatSettling = false;
  appState.activeChatSettle = null;
  appState.chatSettleWait = false;
  actions.removePendingMessage();
  appState.isSending = false;
  appState.state.status = null;
  appState.state.orderStatus = null;
  actions.setComposerBusy(false);
  localStorage.removeItem("printops_session");
  appState.sessionId = "";
  $("#chat-feed").innerHTML = "";
  await actions.bootstrap();
}

// Bulk operations from the settings history panel run one request per order;
// per-order failures (e.g. a revision conflict from another client) are
// skipped and summarized so one stuck order cannot block the rest.
export async function settingsHistoryBulk(kind) {
  if (appState.isSending || !appState.settingsHistorySelection.size) return;
  const targets = appState.settingsOrders.filter((item) => appState.settingsHistorySelection.has(item.id));
  if (!targets.length) return;
  let done = 0;
  try {
    if (kind === "archive") {
      const pending = targets.filter((item) => item.status !== "archived");
      if (!pending.length) {
        showToast("所选订单均已归档");
        return;
      }
      if (!window.confirm(`将归档 ${pending.length} 个订单？归档后只读，仍可查看和导出。`)) return;
      const currentId = appState.sessionId;
      for (const item of pending) {
        try {
          await api(`/api/orders/${encodeURIComponent(item.id)}/archive`, {expectedRevision: item.revision});
          done += 1;
        } catch {}
      }
      if (pending.some((item) => item.id === currentId)) {
        try { actions.render(await api(`/api/orders/${encodeURIComponent(currentId)}`, undefined, "GET"), false); } catch {}
      }
      showToast(done === pending.length ? `已归档 ${done} 个订单` : `已归档 ${done} 个订单，${pending.length - done} 个失败`);
    } else if (kind === "export") {
      if (!window.confirm(`导出 ${targets.length} 个订单的完整备份？`)) return;
      for (const item of targets) {
        try {
          await exportOrderBackup(item.id);
          done += 1;
        } catch {}
      }
      showToast(done === targets.length ? `已导出 ${done} 个订单备份` : `已导出 ${done} 个订单备份，${targets.length - done} 个失败`);
    } else if (kind === "delete") {
      if (!window.confirm(`将永久删除 ${targets.length} 个订单（含对话、报价与审计数据），不可恢复；未归档订单会先自动归档再清除。如需保留请先导出备份。`)) return;
      const currentId = appState.sessionId;
      let includedCurrent = false;
      for (const item of targets) {
        try {
          await purgeOrderEntry(item);
          done += 1;
          includedCurrent = includedCurrent || item.id === currentId;
        } catch {}
      }
      if (includedCurrent) await teardownIfCurrentOrder(currentId);
      showToast(done === targets.length ? `已永久删除 ${done} 个订单` : `已永久删除 ${done} 个订单，${targets.length - done} 个失败`);
    } else {
      return;
    }
  } finally {
    appState.settingsHistorySelection.clear();
    await refreshSettingsOrders();
    await refreshOrderHistory();
  }
}

export async function historyAction(action, orderId) {
  if (appState.isSending) return;
  try {
    if (action === "export") {
      await exportOrderBackup(orderId);
      showToast("完整订单备份已导出，包含来源、对话和审计记录");
    } else if (action === "archive") {
      const item = findOrderEntry(orderId);
      if (!item) return;
      if (!window.confirm("归档后订单只读，仍可查看和导出。确定归档此订单？")) return;
      await api(`/api/orders/${encodeURIComponent(orderId)}/archive`, {expectedRevision: item.revision});
      if (orderId === appState.sessionId) actions.render(await api(`/api/orders/${encodeURIComponent(orderId)}`, undefined, "GET"), false);
      await refreshOrderHistory();
      showToast("订单已归档；可在设置 → 历史订单中查看、导出或永久删除");
    } else if (action === "purge") {
      const item = findOrderEntry(orderId);
      if (!item) return;
      const active = item.status !== "archived";
      const message = active
        ? "永久删除该订单？未归档订单会先自动归档再清除，对话、报价和审计数据都会被删除；如需保留请先导出备份。"
        : "永久删除这条归档记录？订单、对话、报价和审计数据都会被清除；如需保留请先导出备份。";
      if (!window.confirm(message)) return;
      await purgeOrderEntry(item);
      await teardownIfCurrentOrder(orderId);
      await refreshOrderHistory();
      showToast("订单已永久删除");
    }
  } catch (error) {
    showToast(apiErrorText(error, "历史订单操作失败，请重试"), "error");
    await refreshOrderHistory();
  }
}

export async function switchOrder(orderId) {
  if (!orderId || orderId === appState.sessionId || appState.isSending) return;
  const epoch = ++appState.viewEpoch;
  appState.viewLoading = true;
  actions.setComposerBusy(true);
  try {
    const snapshot = await api(`/api/orders/${encodeURIComponent(orderId)}`, undefined, "GET");
    if (epoch !== appState.viewEpoch) return;
    $("#chat-feed").innerHTML = "";
    appState.fileFeedback = null;
    clearPdfPreview();
    appState.suppressOrderDiff = true;
    appState.state = { order: {}, stage: "collect", quickReplies: [], options: [], selectedOption: null, activeItemSelectedOption: null, orderGenerated: false, handoff: null, confirmation: { status: "not_ready" }, quoteRequest: null, quoteRequests: [], activeQuoteRequestId: null, toolTrace: [], availableTools: appState.state.availableTools || [], productProfile: null, activeItemIndex: null };
    actions.render(snapshot, false);
    PrintOpsJourney.clearFailure();
    const unresolved = appState.unresolvedWrites.get(appState.sessionId);
    if (unresolved) PrintOpsJourney.failure(unresolved.error, unresolved.request, recoverOperation);
    $("#platform-select").value = snapshot.order?.platform || "generic";
    const messages = Array.isArray(snapshot.history) ? snapshot.history : [];
    if (messages.length) {
      actions.addMessage("assistant", `已切换到订单 ${orderHistoryTitle(snapshot.order)}。`);
      messages.forEach((message) => actions.addMessage(message.role === "user" ? "user" : "assistant", message.text || ""));
    } else actions.addMessage("assistant", "已切换到这个订单。当前没有保存的对话内容，可以直接继续补充。");
    showToast("订单已切换");
  } catch (error) {
    if (epoch === appState.viewEpoch) showToast(apiErrorText(error, "订单切换失败，请重试"), "error");
  } finally {
    if (epoch === appState.viewEpoch) { appState.viewLoading = false; actions.setComposerBusy(false); }
  }
}
