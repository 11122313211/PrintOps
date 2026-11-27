/* Centralized DOM event listener wiring (checked against BINDING_TARGET_IDS). */

import { DESKTOP_QUERY, actions, appState } from "./store.js";
import { $, on, showToast } from "./dom.js";
import { api, apiErrorText } from "./api.js";
import {
  applyLayout, restoreFocusAfterViewportChange, saveLayout, setHighContrast,
  setSidebarCollapsed, syncPaneViewport, updateOrderToggle,
} from "./layout.js";
import {
  closeHistoryContextMenu, historyAction, moveHistoryMenuFocus,
  openHistoryContextMenu, refreshOrderHistory, refreshSettingsOrders,
  renderSettingsOrders, settingsHistoryBulk, switchOrder, syncSettingsHistoryToolbar,
} from "./history.js";
import {
  applySpecPreset, closePresetDrawer, removeSpecPreset, saveCurrentSpecPreset, togglePresetDrawer,
} from "./presets.js";
import { clearPdfPreview, uploadFile } from "./pdf.js";
import { resetSimulatorState } from "./pricing_simulator.js";
import { currentHandoff, exportOrder } from "./export.js";
import {
  addMessage, applyArchivedState, autoGrowComposer, removePendingMessage,
  renderComposerPreview, sendMessage, setComposerBusy,
} from "./render/chat.js";
import { getOrderGroups, updateDraftToggle } from "./render/draft.js";
import { loadToolStats, renderFileState } from "./render/panels.js";
import {
  activateSettingsView, closeSettingsDialog, renderModelCapabilities,
  renderSettings, settingsNavigationKeydown,
} from "./render/settings.js";
import { openLatestRunTrace } from "./harness.js";
import {
  bindCommandPaletteEvents, closeSlashMenu, handleSlashKeydown, openCommandPalette,
  syncSlashMenuFromInput, toggleSlashMenu, tryExecuteRawSlash,
} from "./commands.js";

export function bindEvents() {
  const render = (...args) => actions.render(...args);
  const bootstrap = (...args) => actions.bootstrap(...args);
  document.addEventListener("focusin", (event) => { appState.lastFocusedElement = event.target; });

  on("message-form", "submit", (event) => {
    event.preventDefault();
    const raw = $("#message-input").value;
    if (tryExecuteRawSlash(raw)) return;
    sendMessage(raw);
  });
  on("message-input", "input", () => { autoGrowComposer(); renderComposerPreview(); syncSlashMenuFromInput(); });
  on("slash-trigger", "click", toggleSlashMenu);
  on("cmd-palette-trigger", "click", openCommandPalette);
  on("trace-jump", "click", openLatestRunTrace);
  bindCommandPaletteEvents();
  on("preview-toggle", "click", () => {
    const toggle = $("#preview-toggle");
    toggle.setAttribute("aria-pressed", String(toggle.getAttribute("aria-pressed") !== "true"));
    renderComposerPreview();
  });
  on("message-input", "keydown", (event) => {
    if (event.isComposing || event.keyCode === 229) return;
    if (handleSlashKeydown(event)) return;
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#message-form").requestSubmit(); }
  });
  on("chat-cancel", "click", () => {
    if ((!appState.isSending && !appState.activeChatController) || appState.stoppedChatSettling) return;
    // Stop waiting only. The server finishes and commits the turn regardless
    // (the session lock serializes any follow-up request behind it), so the
    // fetch stays alive: when the response lands it renders normally and
    // refreshes the local revision, keeping the next request clear of the
    // server's CAS revision check.
    appState.stoppedChatSettling = true;
    removePendingMessage();
    appState.isSending = false;
    setComposerBusy(false);
    $("#message-input").focus();
    showToast("已停止等待回复；这条消息仍会被处理，结果稍后显示在对话和订单里");
  });
  on("generate-order", "click", async () => { try { render(await api("/api/generate", { sessionId: appState.sessionId })); } catch (error) { showToast(apiErrorText(error, "订单生成失败，请重试"), "error"); } });
  on("confirm-order", "click", async () => {
    try { render(await api("/api/confirm", { sessionId: appState.sessionId, note: "用户在订单工作台确认" })); }
    catch (error) { showToast(apiErrorText(error, "确认交接单失败，请重试"), "error"); }
  });
  on("quote-refresh", "click", async () => {
    const requestId = appState.state.activeQuoteRequestId || appState.state.quoteRequest?.requestId || (appState.state.quoteRequests?.length ? appState.state.quoteRequests[appState.state.quoteRequests.length - 1]?.requestId : "");
    if (!requestId) return;
    try {
      render(await api("/api/quote/status", { sessionId: appState.sessionId, requestId }));
      showToast("询价状态已刷新");
    } catch (error) { showToast(apiErrorText(error, "询价状态刷新失败，请重试"), "error"); }
  });
  on("quote-cancel", "click", async () => {
    const requestId = appState.state.activeQuoteRequestId || appState.state.quoteRequest?.requestId || (appState.state.quoteRequests?.length ? appState.state.quoteRequests[appState.state.quoteRequests.length - 1]?.requestId : "");
    if (!requestId) return;
    try {
      render(await api("/api/quote/cancel", { sessionId: appState.sessionId, requestId, reason: "用户在订单工作台取消询价" }));
      showToast("询价请求已取消");
    } catch (error) { showToast(apiErrorText(error, "取消询价失败，请重试"), "error"); }
  });
  on("copy-order", "click", async () => {
    try {
      const document = await currentHandoff();
      await navigator.clipboard.writeText(document.markdown);
      showToast("当前版本交接单已复制");
    } catch (error) { showToast(error.message || "复制失败", "error"); }
  });

  document.querySelectorAll(".export-option").forEach((button) => button.addEventListener("click", () => exportOrder(button.dataset.format)));
  on("toggle-draft-groups", "click", () => {
    const groups = [...document.querySelectorAll(".draft-group")];
    const open = !groups.every((group) => group.open);
    groups.forEach((group) => { group.open = open; });
    updateDraftToggle();
  });
  document.querySelectorAll(".draft-group").forEach((group) => group.addEventListener("toggle", updateDraftToggle));

  on("upload-button", "click", () => $("#file-input").click());
  on("drop-zone", "click", () => $("#file-input").click());
  on("file-input", "change", async () => {
    await uploadFile($("#file-input").files[0]);
    $("#file-input").value = "";
  });
  ["dragenter", "dragover"].forEach((type) => on("drop-zone", type, (event) => {
    event.preventDefault();
    $("#drop-zone").classList.add("is-dragging");
  }));
  ["dragleave", "drop"].forEach((type) => on("drop-zone", type, () => $("#drop-zone").classList.remove("is-dragging")));
  on("drop-zone", "drop", (event) => {
    event.preventDefault();
    uploadFile(event.dataTransfer?.files?.[0]);
  });
  on("platform-select", "change", async (event) => { try { render(await api("/api/platform", { sessionId: appState.sessionId, platformId: event.target.value })); } catch (error) { showToast(apiErrorText(error, "平台切换失败"), "error"); } });
  on("sidebar-toggle", "click", () => setSidebarCollapsed(!appState.sidebarCollapsed));
  on("order-toggle", "click", () => {
    if (!DESKTOP_QUERY.matches) { $("#workspace").dataset.mobilePane = "order"; updateOrderToggle(); $("#mobile-back").focus(); return; }
    appState.layout.orderCollapsed = !appState.layout.orderCollapsed;
    applyLayout();
    saveLayout();
    // WebKit never focuses buttons on pointer-down, so collapsing can hide
    // the pane that still owns focus.
    restoreFocusAfterViewportChange();
  });
  on("mobile-back", "click", () => { closeHistoryContextMenu(); $("#workspace").dataset.mobilePane = "work"; updateOrderToggle(); $("#order-toggle").focus(); });
  on("workspace-backdrop", "click", () => { setSidebarCollapsed(true); $("#sidebar-toggle").focus(); });
  on("history-context-menu", "click", (event) => {
    const control = event.target.closest("[data-history-menu-action]");
    if (!control) return;
    const menu = $("#history-context-menu");
    const orderId = menu.dataset.orderId;
    closeHistoryContextMenu({restoreFocus: true});
    historyAction(control.dataset.historyMenuAction, orderId);
  });
  on("history-list", "contextmenu", (event) => {
    const item = event.target.closest(".history-item");
    if (!item) return;
    event.preventDefault();
    openHistoryContextMenu(item.dataset.sessionId, item, {x: event.clientX, y: event.clientY});
  });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && !event.shiftKey && !event.altKey && event.key.toLowerCase() === "k") {
      const openDialog = document.querySelector("dialog[open]");
      if (!openDialog || openDialog.id === "command-palette") {
        event.preventDefault();
        openCommandPalette();
        return;
      }
    }
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
    const presetDrawer = $("#preset-drawer");
    if (presetDrawer && !presetDrawer.hidden && event.key === "Escape") {
      event.preventDefault();
      closePresetDrawer({restoreFocus: true});
      return;
    }
    const focusedHistoryItem = document.activeElement?.closest?.(".history-item");
    if (focusedHistoryItem && ((event.shiftKey && event.key === "F10") || event.key === "ContextMenu" || event.key === "Apps")) {
      event.preventDefault();
      openHistoryContextMenu(focusedHistoryItem.dataset.sessionId, focusedHistoryItem);
      return;
    }
    if (!DESKTOP_QUERY.matches && !appState.sidebarCollapsed) {
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

  window.addEventListener("resize", syncPaneViewport);
  DESKTOP_QUERY.addEventListener("change", syncPaneViewport);
  on("theme-toggle", "click", () => setHighContrast(document.documentElement.dataset.theme !== "high-contrast"));
  on("settings-nav", "click", (event) => {
    const button = event.target.closest("[data-settings-view]");
    if (button) activateSettingsView(button.dataset.settingsView, {focus: true});
  });
  on("settings-nav", "keydown", settingsNavigationKeydown);
  on("history-include-archived", "change", (event) => refreshOrderHistory(event.currentTarget.checked));
  on("history-list", "click", (event) => {
    const hoverAction = event.target.closest("[data-history-action]");
    if (hoverAction) {
      event.stopPropagation();
      historyAction(hoverAction.dataset.historyAction, hoverAction.dataset.orderId);
      return;
    }
    const item = event.target.closest(".history-item");
    if (item) switchOrder(item.dataset.sessionId);
  });
  on("history-list", "pointerdown", (event) => {
    if (event.target.closest(".history-item")) closeHistoryContextMenu();
  });
  on("settings-history-list", "click", async (event) => {
    const action = event.target.closest("[data-history-action]");
    if (action) {
      const kind = action.dataset.historyAction;
      await historyAction(kind, action.dataset.orderId);
      if (kind === "archive" || kind === "purge") await refreshSettingsOrders();
      return;
    }
    const view = event.target.closest(".settings-history-view");
    if (view && view.dataset.orderId) {
      closeSettingsDialog();
      switchOrder(view.dataset.orderId);
    }
  });
  on("settings-history-list", "change", (event) => {
    const check = event.target.closest(".settings-history-check");
    if (!check) return;
    if (check.checked) appState.settingsHistorySelection.add(check.dataset.orderId);
    else appState.settingsHistorySelection.delete(check.dataset.orderId);
    syncSettingsHistoryToolbar();
  });
  on("settings-history-select-all", "change", (event) => {
    appState.settingsHistorySelection.clear();
    if (event.currentTarget.checked) appState.settingsOrders.forEach((item) => appState.settingsHistorySelection.add(item.id));
    renderSettingsOrders();
  });
  on("settings-history-toolbar", "click", (event) => {
    const control = event.target.closest("[data-history-bulk]");
    if (control) settingsHistoryBulk(control.dataset.historyBulk);
  });
  document.addEventListener("pointerdown", (event) => {
    const menu = $("#history-context-menu");
    if (menu && !menu.hidden && !menu.contains(event.target)) closeHistoryContextMenu();
    const drawer = $("#preset-drawer");
    const toggle = $("#preset-toggle");
    if (drawer && !drawer.hidden && !drawer.contains(event.target) && !toggle?.contains(event.target)) closePresetDrawer();
    const slashMenu = $("#slash-menu");
    const slashTrigger = $("#slash-trigger");
    const msgInput = $("#message-input");
    if (slashMenu && !slashMenu.hidden && !slashMenu.contains(event.target) && !slashTrigger?.contains(event.target) && event.target !== msgInput) {
      closeSlashMenu();
    }
  });
  on("save-preset", "click", saveCurrentSpecPreset);
  on("preset-toggle", "click", togglePresetDrawer);
  on("preset-list", "click", (event) => {
    const apply = event.target.closest(".preset-apply");
    if (apply) {
      closePresetDrawer();
      applySpecPreset(apply.dataset.presetId);
    }
    const remove = event.target.closest(".preset-remove");
    if (remove) removeSpecPreset(remove.dataset.presetId);
  });
  on("reset-order", "click", () => {
    const dialog = $("#confirm-reset-dialog");
    dialog.returnValue = "";
    dialog.showModal();
  });

  on("confirm-reset-dialog", "close", () => {
    const dialog = $("#confirm-reset-dialog");
    if (!dialog || dialog.returnValue !== "confirm") {
      $("#reset-order").focus({preventScroll: true});
      return;
    }
    if (!DESKTOP_QUERY.matches) setSidebarCollapsed(true);
    (async () => {
      appState.viewEpoch += 1;
      appState.viewLoading = false;
      appState.chatRequestSeq += 1;
      appState.activeChatController?.abort();
      appState.activeChatController = null;
      appState.stoppedChatSettling = false;
      appState.activeChatSettle = null;
      appState.chatSettleWait = false;
      removePendingMessage();
      appState.isSending = false;
      appState.state.status = null;
      appState.state.orderStatus = null;
      setComposerBusy(false);
      const prevSessionId = appState.sessionId;
      localStorage.removeItem("printops_session");
      $("#chat-feed").innerHTML = "";
      appState.sessionId = "";
      appState.suppressOrderDiff = true;
      appState.activeUndoNoticeRevision = null;
      appState.renderFingerprints = Object.create(null);
      closeSlashMenu();
      resetSimulatorState();
      appState.fileFeedback = null;
      clearPdfPreview(prevSessionId || false);
      await bootstrap();
      if (!DESKTOP_QUERY.matches) $("#message-input").focus({preventScroll: true});
      showToast("已重置当前订单，开始新的对话");
    })();
  });

  on("settings-test", "click", async () => {
    const button = $("#settings-test");
    const force = appState.settingsProbeUsedCache;
    appState.settingsProbeUsedCache = false;
    button.disabled = true;
    // The accessible name stays constant while the elapsed digits live in an
    // aria-hidden span: a button whose label mutates five times a second makes
    // screen readers re-announce it, and proportional digits make it jitter.
    button.textContent = "测试中…";
    const elapsed = document.createElement("span");
    elapsed.className = "settings-test-elapsed";
    elapsed.setAttribute("aria-hidden", "true");
    button.append(elapsed);
    const began = performance.now();
    const tick = () => {
      elapsed.textContent = " " + ((performance.now() - began) / 1000).toFixed(1) + "s";
    };
    tick();
    const timer = setInterval(tick, 200);
    appState.settingsAbort?.abort();
    const controller = new AbortController();
    appState.settingsAbort = controller;
    try {
      const result = await api("/api/model/test", {
        url: $("#settings-url").value.trim(), model: $("#settings-model").value.trim(), key: $("#settings-key").value,
        force,
      }, "POST", { signal: controller.signal });
      if (controller.signal.aborted) return;
      appState.settingsProbeUsedCache = Boolean(result.test?.cached);
      const presentation = renderModelCapabilities(result.test);
      showToast(presentation.label, presentation.tone === "ready" ? "success" : presentation.tone);
    } catch (error) {
      if (error?.name === "AbortError" || controller.signal.aborted) return;
      // A forced click that never reached the endpoint must stay forced: falling
      // through would show the very verdict the user clicked to replace.
      appState.settingsProbeUsedCache = force;
      // TD-19: fixed copy for mapped codes comes from the shared ERROR_TEXTS.
      const known = PrintOpsJourney.ERROR_TEXTS?.[error?.code];
      const message = known?.title || error?.message || "模型连接测试失败";
      showToast(`${message}${error?.requestId ? `（请求号 ${error.requestId}）` : ""}`, "error");
    } finally {
      clearInterval(timer);
      if (appState.settingsAbort === controller) appState.settingsAbort = null;
      button.disabled = false;
      button.textContent = "测试连接";
    }
  });

  on("settings-button", "click", () => {
    renderSettings({ llm: appState.llmSettings });
    $("#settings-dialog").showModal();
    activateSettingsView("overview", {focus: true});
    // Tool-call telemetry refreshes on every settings open; failures keep the
    // previous content and degrade to an inline note.
    loadToolStats();
  });
  on("settings-cancel", "click", () => $("#settings-dialog").close());
  on("settings-dialog", "close", () => { appState.settingsAbort?.abort(); appState.settingsAbort = null; });
  on("settings-cancel-secondary", "click", () => $("#settings-dialog").close());
  on("settings-form", "submit", async (event) => {
    event.preventDefault();
    const save = $("#settings-save");
    save.disabled = true;
    try {
      appState.settingsAbort?.abort();
      const controller = new AbortController();
      appState.settingsAbort = controller;
      let result;
      try {
        result = await api("/api/settings", {
          url: $("#settings-url").value.trim(), model: $("#settings-model").value.trim(), key: $("#settings-key").value
        }, "POST", { signal: controller.signal });
      } finally {
        if (appState.settingsAbort === controller) appState.settingsAbort = null;
      }
      renderSettings(result);
      $("#settings-dialog").close();
      showToast(result.llm.enabled ? `已启用模型：${result.llm.model}` : "已切换为规则模式");
    } catch (error) {
      if (error?.name === "AbortError") return;
      // Server messages name the real cause (DNS timeout, busy outbound pool,
      // unwritable config, rejected host). The old fallback replaced all of them
      // with "check the address", which blamed a correct URL.
      // TD-19: the two dedicated texts live in the shared ERROR_TEXTS table;
      // the server answers both with the generic INVALID_SETTINGS code, so the
      // detection stays on the message content and unknown codes fall through
      // to the server message untouched.
      const known = PrintOpsJourney.ERROR_TEXTS;
      const message = error?.message || "";
      const settingsMessage = message.includes("URL 和模型名")
        ? known.settingsUrlModel.title
        : message.includes("本机或内网")
          ? known.settingsPrivateHost.title
          : message
            ? `${message}${error?.requestId ? `（请求号 ${error.requestId}）` : ""}`
            : "设置保存失败，请稍后重试";
      showToast(settingsMessage, "error");
    } finally {
      save.disabled = false;
    }
  });
  on("settings-clear", "click", async () => {
    const clear = $("#settings-clear");
    clear.disabled = true;
    try {
      const result = await api("/api/settings", { clear: true });
      renderSettings(result);
      $("#settings-dialog").close();
      showToast("已清空模型配置，继续使用规则模式");
    } catch (error) {
      // TD-19: same shape as the shared apiErrorText lookup (message first,
      // fixed fallback), now table-aware for mapped codes.
      showToast(apiErrorText(error, "配置清空失败，请重试"), "error");
    }
    finally { clear.disabled = false; }
  });
}
