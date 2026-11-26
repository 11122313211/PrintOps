/* Local spec presets drawer (save/apply/remove reusable printing specification templates). */

import { specPresetKey } from "./constants.js";
import { actions, appState, scrollBehavior } from "./store.js";
import { $, hasValue, showToast } from "./dom.js";
import { formatOrderHistoryTime } from "./history.js";

export function loadSpecPresets() {
  try {
    const presets = JSON.parse(localStorage.getItem(specPresetKey) || "[]");
    return Array.isArray(presets) ? presets.filter((item) => item?.id && item.fields) : [];
  } catch {
    return [];
  }
}

export function saveSpecPresets(presets) {
  try { localStorage.setItem(specPresetKey, JSON.stringify(presets.slice(0, 8))); }
  catch { showToast("常用规格保存空间已满，请清理浏览器数据"); }
}

export function currentSpecPresetFields(order = {}) {
  return {
    paper: order.paper || "",
    printing: order.printing || "",
    finishing: order.finishing || "",
    binding: order.binding || ""
  };
}

export function specPresetHasContent(fields = {}) {
  return Object.values(fields).some((value) => hasValue(value));
}

export function specPresetTitle(fields = {}) {
  const parts = ["paper", "printing", "finishing", "binding"].map((key) => fields[key]).filter(Boolean);
  return parts.length ? parts.join(" · ") : "未命名规格";
}

// The preset drawer expands inside the docked sidebar footer — no popover
// positioning; the footer's grid row grows and the history area above shrinks.
export function openPresetDrawer() {
  const drawer = $("#preset-drawer");
  const toggle = $("#preset-toggle");
  if (!drawer || !toggle) return;
  actions.closeHistoryContextMenu();
  drawer.hidden = false;
  toggle.setAttribute("aria-expanded", "true");
  drawer.querySelector(".preset-item.is-new .preset-apply")?.scrollIntoView({block: "nearest", behavior: scrollBehavior()});
}

export function closePresetDrawer({restoreFocus = false} = {}) {
  const drawer = $("#preset-drawer");
  const toggle = $("#preset-toggle");
  if (!drawer || drawer.hidden) return;
  drawer.hidden = true;
  toggle?.setAttribute("aria-expanded", "false");
  if (restoreFocus && toggle?.getClientRects().length) toggle.focus({preventScroll: true});
}

export function togglePresetDrawer() {
  const drawer = $("#preset-drawer");
  if (!drawer) return;
  if (drawer.hidden) openPresetDrawer();
  else closePresetDrawer({restoreFocus: true});
}

export function renderSpecPresets(highlightId) {
  const root = $("#preset-list");
  const presets = loadSpecPresets();
  $("#preset-count").textContent = String(presets.length);
  $("#save-preset").disabled = !specPresetHasContent(currentSpecPresetFields(appState.state.order));
  root.innerHTML = "";
  if (!presets.length) {
    const empty = document.createElement("p");
    empty.className = "preset-empty";
    empty.textContent = "还没有常用规格。点“＋ 保存常用”把当前工艺组合存进来，点击即可复用。";
    root.append(empty);
    return;
  }
  presets.forEach((item) => {
    const row = document.createElement("div");
    row.className = "preset-item";
    if (highlightId && item.id === highlightId) row.classList.add("is-new");
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
  root.querySelector(".preset-item.is-new .preset-apply")?.scrollIntoView({block: "nearest", behavior: scrollBehavior()});
}

export function saveCurrentSpecPreset() {
  const fields = currentSpecPresetFields(appState.state.order);
  if (!specPresetHasContent(fields)) {
    showToast("请先补充纸张、颜色、工艺或装订");
    return;
  }
  const presets = loadSpecPresets();
  const signature = JSON.stringify(fields);
  const existingIndex = presets.findIndex((item) => JSON.stringify(item.fields) === signature);
  if (existingIndex >= 0) presets.splice(existingIndex, 1);
  const saved = { id: crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`, fields, createdAt: Date.now() };
  presets.unshift(saved);
  saveSpecPresets(presets);
  renderSpecPresets(saved.id);
  // Open the drawer so the flash on the new entry is visible feedback for
  // what "＋ 保存常用" just stored.
  openPresetDrawer();
  showToast("常用规格已保存，点击列表中的组合即可复用");
}

export async function applySpecPreset(presetId) {
  const preset = loadSpecPresets().find((item) => item.id === presetId);
  if (!preset || appState.isSending) return;
  const patch = Object.fromEntries(Object.entries(preset.fields).filter(([, value]) => hasValue(value)));
  if (!Object.keys(patch).length) return;
  await actions.sendMessage("沿用常用规格", patch);
}

export function removeSpecPreset(presetId) {
  saveSpecPresets(loadSpecPresets().filter((item) => item.id !== presetId));
  renderSpecPresets();
  showToast("常用规格已删除");
}
