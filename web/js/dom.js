/* DOM selectors, focus preservation, toast notifications, and bootstrap ID guard (TD-06). */

import { BINDING_TARGET_IDS } from "./constants.js";
import { DESKTOP_QUERY, appState, pendingFieldFocus, scrollBehavior } from "./store.js";

export const $ = (selector) => document.querySelector(selector);
export const missingBindingIds = new Set(BINDING_TARGET_IDS.filter((id) => !document.getElementById(id)));

export function showToast(text, kind = "success") {
  const node = $("#toast");
  node.textContent = text;
  node.className = `toast show ${kind}`;
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => node.classList.remove("show"), 2600);
}

export function updateBackdrop() {
  const backdrop = $("#workspace-backdrop");
  if (!backdrop) return;
  const drawerOpen = !DESKTOP_QUERY.matches && !appState.sidebarCollapsed;
  backdrop.hidden = !drawerOpen;
  document.body.classList.toggle("drawer-open", drawerOpen);
}

export function isFeedNearBottom() {
  const feed = $("#chat-feed");
  return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
}

export function revealPanel(el) {
  if (!el || el.hidden) return;
  el.classList.remove("reveal");
  void el.offsetWidth;
  el.classList.add("reveal");
  el.addEventListener("animationend", () => el.classList.remove("reveal"), { once: true });
}

export function scrollFeedToBottom() {
  const feed = $("#chat-feed");
  feed.scrollTo({ top: feed.scrollHeight, behavior: scrollBehavior() });
}

export function captureFieldFocus(root) {
  const active = document.activeElement;
  const focus = active instanceof HTMLInputElement && root?.contains(active) && active.dataset.fieldKey
    ? {key: active.dataset.fieldKey, start: active.selectionStart, end: active.selectionEnd} : null;
  if (root) pendingFieldFocus.set(root, focus);
  return focus;
}

export function restoreFieldFocus(root) {
  const focus = root ? pendingFieldFocus.get(root) : null;
  if (!focus) return false;
  const input = root.querySelector(`[data-field-key="${CSS.escape(focus.key)}"]`);
  if (!input || input.disabled || input.closest("details:not([open])") || input.closest("[hidden]")) return false;
  pendingFieldFocus.delete(root);
  input.focus({preventScroll: true});
  if (focus.start != null && focus.end != null) {
    try { input.setSelectionRange(focus.start, focus.end); } catch { /* input types without selectable text */ }
  }
  return true;
}

export function hasValue(value) {
  return value !== undefined && value !== null && String(value).trim() !== "";
}

export function formatFileSize(sizeBytes) {
  if (!Number.isFinite(sizeBytes) || sizeBytes < 0) return "未知大小";
  if (sizeBytes < 1024) return `${sizeBytes} B`;
  if (sizeBytes < 1024 * 1024) return `${(sizeBytes / 1024).toFixed(1)} KB`;
  return `${(sizeBytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatFileTime(timestamp) {
  if (!timestamp) return "未知时间";
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" }).format(new Date(timestamp));
}

export function fmtMs(value) {
  return Number.isFinite(Number(value)) ? `${Math.round(Number(value))}ms` : "—";
}

export function on(id, type, listener, options) {
  const node = document.getElementById(id);
  if (!node) {
    missingBindingIds.add(id);
    return null;
  }
  node.addEventListener(type, listener, options);
  return node;
}

export function reportMissingBindings() {
  if (!missingBindingIds.size) return;
  const list = [...missingBindingIds].join("、");
  console.error(`[PrintOps] 页面缺少 ${missingBindingIds.size} 个预期元素（${list}），相关交互未绑定。请确认页面文件完整后重启本地服务并刷新。`);
  const banner = document.createElement("div");
  banner.className = "binding-banner";
  banner.setAttribute("role", "alert");
  banner.textContent = `界面初始化不完整：缺少 ${missingBindingIds.size} 个界面元素（${list}）。部分操作暂时不可用，请重启本地服务并刷新页面。`;
  document.body.prepend(banner);
}
