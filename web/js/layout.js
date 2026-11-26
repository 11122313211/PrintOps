/* Resizable three-column layout, splitters, composer resize, sidebar, and high-contrast theme. */

import {
  COMPOSER_RANGE, LAYOUT_DEFAULTS, ORDER_RANGE, SIDEBAR_RANGE, layoutKey,
} from "./constants.js";
import { DESKTOP_QUERY, actions, appState } from "./store.js";
import { $, showToast, updateBackdrop } from "./dom.js";

export function loadLayout() {
  try {
    const stored = JSON.parse(localStorage.getItem(layoutKey) || "{}");
    return {
      sidebarW: clampLayoutWidth(Number(stored.sidebarW) || LAYOUT_DEFAULTS.sidebarW, SIDEBAR_RANGE),
      orderW: clampLayoutWidth(Number(stored.orderW) || LAYOUT_DEFAULTS.orderW, ORDER_RANGE),
      orderCollapsed: Boolean(stored.orderCollapsed),
      composerH: clampLayoutWidth(Number(stored.composerH) || LAYOUT_DEFAULTS.composerH, COMPOSER_RANGE)
    };
  } catch {
    return { ...LAYOUT_DEFAULTS };
  }
}

export function clampLayoutWidth(value, [min, max]) {
  return Math.min(max, Math.max(min, Math.round(value)));
}

export function saveLayout() {
  try {
    localStorage.setItem(layoutKey, JSON.stringify({ sidebarW: appState.layout.sidebarW, orderW: appState.layout.orderW, orderCollapsed: appState.layout.orderCollapsed, composerH: appState.layout.composerH }));
  } catch { /* 布局是增强项，存储失败不影响使用 */ }
}

export function applyLayout() {
  const workspace = $("#workspace");
  workspace.style.setProperty("--sidebar-w", `${appState.layout.sidebarW}px`);
  workspace.style.setProperty("--order-w", `${appState.layout.orderW}px`);
  workspace.classList.toggle("order-collapsed", appState.layout.orderCollapsed);
  updateOrderToggle();
  updateBackdrop();
}

export function updateOrderToggle() {
  const orderToggle = $("#order-toggle");
  if (orderToggle) {
    const collapsed = DESKTOP_QUERY.matches ? appState.layout.orderCollapsed : $("#workspace").dataset.mobilePane !== "order";
    orderToggle.setAttribute("aria-label", collapsed ? "展开订单栏" : "收起订单栏");
    orderToggle.title = collapsed ? "展开订单栏" : "收起订单栏";
    orderToggle.dataset.collapsed = String(collapsed);
    orderToggle.setAttribute("aria-expanded", String(!collapsed));
  }
}

export function resetPaneWidth(pane) {
  if (pane === "sidebar") appState.layout.sidebarW = LAYOUT_DEFAULTS.sidebarW;
  if (pane === "order") appState.layout.orderW = LAYOUT_DEFAULTS.orderW;
  applyLayout();
  saveLayout();
  showToast("栏宽已重置");
}

export function setupSplitters() {
  document.querySelectorAll(".pane-splitter").forEach((splitter) => {
    const pane = splitter.dataset.pane;
    splitter.addEventListener("pointerdown", (event) => {
      if (!DESKTOP_QUERY.matches || (pane === "sidebar" && appState.sidebarCollapsed)) return;
      event.preventDefault();
      splitter.setPointerCapture(event.pointerId);
      splitter.classList.add("is-dragging");
      document.body.classList.add("is-resizing");
      const workspaceRect = $("#workspace").getBoundingClientRect();
      const onMove = (moveEvent) => {
        if (pane === "sidebar") {
          appState.layout.sidebarW = clampLayoutWidth(moveEvent.clientX - workspaceRect.left, SIDEBAR_RANGE);
          $("#workspace").style.setProperty("--sidebar-w", `${appState.layout.sidebarW}px`);
        } else {
          appState.layout.orderW = clampLayoutWidth(workspaceRect.right - moveEvent.clientX, ORDER_RANGE);
          $("#workspace").style.setProperty("--order-w", `${appState.layout.orderW}px`);
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
        if (appState.sidebarCollapsed) return;
        appState.layout.sidebarW = clampLayoutWidth(appState.layout.sidebarW + step, SIDEBAR_RANGE);
      } else {
        appState.layout.orderW = clampLayoutWidth(appState.layout.orderW + step, ORDER_RANGE);
      }
      applyLayout();
      saveLayout();
    });
  });
}

export function applyComposerHeight() {
  const input = $("#message-input");
  if (!input) return;
  input.style.height = `${appState.layout.composerH}px`;
  $(".composer-resize")?.setAttribute("aria-valuenow", String(appState.layout.composerH));
}

export function setupComposerResize() {
  const handle = $(".composer-resize");
  if (!handle || !$("#message-input")) return;
  handle.setAttribute("aria-valuenow", String(appState.layout.composerH));
  handle.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    handle.classList.add("is-dragging");
    document.body.classList.add("is-resizing-rows");
    const startY = event.clientY;
    const startH = appState.layout.composerH;
    const onMove = (moveEvent) => {
      appState.layout.composerH = clampLayoutWidth(startH + (startY - moveEvent.clientY), COMPOSER_RANGE);
      applyComposerHeight();
    };
    const finish = () => {
      handle.classList.remove("is-dragging");
      document.body.classList.remove("is-resizing-rows");
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", finish);
      handle.removeEventListener("pointercancel", finish);
      saveLayout();
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", finish);
    handle.addEventListener("pointercancel", finish);
  });
  handle.addEventListener("dblclick", () => {
    appState.layout.composerH = LAYOUT_DEFAULTS.composerH;
    applyComposerHeight();
    actions.autoGrowComposer();
    saveLayout();
  });
  handle.addEventListener("keydown", (event) => {
    const step = event.key === "ArrowUp" ? 16 : event.key === "ArrowDown" ? -16 : 0;
    if (!step) return;
    event.preventDefault();
    appState.layout.composerH = clampLayoutWidth(appState.layout.composerH + step, COMPOSER_RANGE);
    applyComposerHeight();
    saveLayout();
  });
}

export function setSidebarCollapsed(collapsed) {
  actions.closeHistoryContextMenu();
  appState.sidebarCollapsed = Boolean(collapsed);
  $("#workspace")?.classList.toggle("sidebar-collapsed", appState.sidebarCollapsed);
  const button = $("#sidebar-toggle");
  if (button) {
    button.setAttribute("aria-label", appState.sidebarCollapsed ? "展开导航栏" : "收起导航栏");
    button.title = appState.sidebarCollapsed ? "展开导航栏" : "收起导航栏";
    button.dataset.collapsed = String(appState.sidebarCollapsed);
    button.setAttribute("aria-expanded", String(!appState.sidebarCollapsed));
  }
  if (!DESKTOP_QUERY.matches) {
    if (!appState.sidebarCollapsed) $(".sidebar").querySelector("button, input, summary")?.focus();
    // P0-2: collapsing the drawer hides whatever had focus (WebKit never
    // moves focus on pointer-down), leaving it on <body>; route it back to
    // the visible toggle — but only when the sidebar owned that focus, so a
    // breakpoint crossing that hides another pane cannot steal the reclaim.
    else if ($(".sidebar").contains(document.activeElement)
      || (document.activeElement === document.body && appState.lastFocusedElement instanceof Element && appState.lastFocusedElement.closest(".sidebar"))) button?.focus();
  }
  localStorage.setItem("printops_sidebar", appState.sidebarCollapsed ? "collapsed" : "expanded");
  updateBackdrop();
}

export function setHighContrast(enabled) {
  document.documentElement.dataset.theme = enabled ? "high-contrast" : "";
  const button = $("#theme-toggle");
  if (button) {
    button.setAttribute("aria-pressed", String(enabled));
    button.textContent = enabled ? "标准对比" : "高对比";
    button.title = enabled ? "切换回标准对比模式" : "切换到高对比模式";
  }
  localStorage.setItem("printops_theme", enabled ? "high-contrast" : "standard");
}

// P0-2: hand focus to a visible entry when the pane that owned it is hidden.
export function restoreFocusAfterViewportChange() {
  if (document.activeElement !== document.body || !appState.lastFocusedElement) return;
  if (document.querySelector("dialog[open]")) return;
  const last = appState.lastFocusedElement;
  if (!(last instanceof Element)) return;
  const pane = last.closest(".sidebar, .order-section, .chat-section");
  // The pane must actually be hidden now; a visible pane whose focused
  // element was merely re-rendered away belongs to the TD-11 machinery.
  if (!pane || pane.getClientRects().length) return;
  if (pane.classList.contains("sidebar")) { $("#sidebar-toggle")?.focus(); return; }
  if (pane.classList.contains("order-section")) { $("#order-toggle")?.focus(); return; }
  if (pane.classList.contains("chat-section")) { $("#mobile-back")?.focus(); return; }
}

export function syncPaneViewport() {
  actions.closeHistoryContextMenu();
  if (appState.wasDesktop && !DESKTOP_QUERY.matches) setSidebarCollapsed(true);
  appState.wasDesktop = DESKTOP_QUERY.matches;
  updateOrderToggle();
  updateBackdrop();
  restoreFocusAfterViewportChange();
  // Chromium may run the resize/media-change events before the media-query
  // style recalc lands: the focus owner is not blurred yet and its pane still
  // reports visible, so the synchronous restore above bails out. Re-check
  // after the next layout frame so the stranded focus is still handed off.
  requestAnimationFrame(() => requestAnimationFrame(restoreFocusAfterViewportChange));
}
