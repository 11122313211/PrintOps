/* Settings dialog navigation, model config rendering, and capability probe presentation. */

import { actions, appState } from "../store.js";
import { $, revealPanel } from "../dom.js";
import { refreshSettingsOrders } from "../history.js";
import { loadToolStats } from "./panels.js";

export function activateSettingsView(view = "overview", {focus = false} = {}) {
  const nav = $("#settings-nav");
  const panels = document.querySelectorAll("[data-settings-panel]");
  const next = nav?.querySelector(`[data-settings-view="${view}"]`) ? view : "overview";
  appState.settingsView = next;
  nav?.querySelectorAll("[data-settings-view]").forEach((button) => {
    const selected = button.dataset.settingsView === next;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  panels.forEach((panel) => {
    const show = panel.dataset.settingsPanel === next;
    panel.hidden = !show;
    if (show) revealPanel(panel);
  });
  const catalogPanel = $("#settings-panel-products .catalog-panel");
  const toolPanel = $("#settings-panel-tools .tool-panel");
  const toolStatsPanel = $("#tool-stats-panel");
  if (catalogPanel) catalogPanel.open = next === "products";
  if (toolPanel) toolPanel.open = next === "tools";
  if (toolStatsPanel) toolStatsPanel.open = next === "tools";
  if (next === "history") refreshSettingsOrders();
  if (focus) nav?.querySelector(`[data-settings-view="${next}"]`)?.focus();
}

export function closeSettingsDialog() {
  const dialog = $("#settings-dialog");
  if (dialog?.open) dialog.close();
}

export function settingsNavigationKeydown(event) {
  const buttons = [...$("#settings-nav")?.querySelectorAll("[data-settings-view]") || []];
  if (!buttons.length || !["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const current = Math.max(0, buttons.indexOf(document.activeElement));
  const index = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
    : (current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
  activateSettingsView(buttons[index].dataset.settingsView, {focus: true});
}

export function renderSettings(data) {
  appState.llmSettings = data?.llm || appState.llmSettings;
  const enabled = Boolean(appState.llmSettings.enabled);
  const hasError = enabled && Boolean(appState.llmSettings.lastError);
  const protocolLabel = { native_tools: "原生工具", json_fallback: "JSON 兼容", error: "连接异常" }[appState.llmSettings.protocolMode] || "自动协议";
  const stateText = $("#settings-state");
  if (stateText) {
    stateText.textContent = enabled
      ? (hasError ? `已配置：${appState.llmSettings.model}（上次调用失败，已回退 · ${protocolLabel}）` : `已启用：${appState.llmSettings.model} · ${protocolLabel}`)
      : "当前使用规则模式";
    stateText.classList.remove("is-ready", "is-warning", "is-error");
  }
  const note = $("#settings-note");
  if (note) note.textContent = appState.llmSettings.privateHostsAllowed
    ? "当前已允许访问本机或公司内网模型地址；不要把此服务暴露到公网。Key 仅由本机服务保存。"
    : "Key 仅由本机服务保存，不会显示在页面或接口响应中。内网模型地址默认拦截；URL 与模型留空即可继续使用规则模式。";
  const url = $("#settings-url");
  const model = $("#settings-model");
  const key = $("#settings-key");
  if (url) url.value = appState.llmSettings.url || "";
  if (model) model.value = appState.llmSettings.model || "";
  if (key) {
    key.value = "";
    key.placeholder = appState.llmSettings.keyConfigured ? "已配置，留空保持不变" : "可选，不填也可使用规则模式";
  }
  const button = $("#settings-button");
  if (button) {
    button.dataset.interfaceState = hasError ? "error" : enabled ? "enabled" : "rules";
    button.title = hasError ? "打开设置与能力（接口异常）" : enabled ? "打开设置与能力（接口已启用）" : "打开设置与能力";
  }
  const capabilities = $("#settings-capabilities");
  if (capabilities) capabilities.hidden = true;
  const probeDetail = $("#settings-probe-detail");
  if (probeDetail) { probeDetail.textContent = ""; probeDetail.hidden = true; }
  actions.updateHarnessHeaderMeta?.();
}

export function modelCapabilityPresentation(test = {}) {
  if (test.agentReady && test.chosenProtocol === "native_tools") {
    return { label: "Agent 工具链可用 · 原生工具", tone: "ready" };
  }
  if (test.agentReady && test.chosenProtocol === "json_fallback") {
    return { label: "Agent 工具链可用 · JSON 兼容", tone: "ready" };
  }
  if (test.chosenProtocol === "chat_only" || (test.chat && !test.agentReady)) {
    return { label: "仅支持聊天 · 不满足 Agent 协议", tone: "warning" };
  }
  if (test.chosenProtocol === "unreachable" && test.message) {
    return { label: test.message, tone: "error" };
  }
  if (!test.ok && test.message && (test.message.includes("鉴权") || test.message.includes("API Key"))) {
    return { label: test.message, tone: "error" };
  }
  return { label: "接口不可用", tone: "error" };
}

export function modelProbeDetail(test = {}) {
  // Attribute the wait instead of showing one number: which hop of the
  // capability probe actually cost the time.
  const timings = test?.probeTimings || {};
  const seconds = (label, value) => {
    const ms = Number(value);
    return Number.isFinite(ms) && ms > 0 ? label + " " + (ms / 1000).toFixed(1) + "s" : "";
  };
  const hops = [
    seconds("聊天", timings.chatMs),
    seconds("JSON", timings.jsonMs),
    seconds("工具链", timings.nativeMs),
  ].filter(Boolean);
  const parts = [];
  if (hops.length) parts.push("并行探测 · " + hops.join(" · "));
  const total = Number(test?.latencyMs);
  if (Number.isFinite(total) && total > 0) parts.push("合计 " + (total / 1000).toFixed(1) + "s");
  if (test?.cached) {
    const left = Math.max(1, Math.round((Number(test.cacheTtlMs) || 0) / 1000));
    parts.push("复用 " + left + " 秒前的不可达判定，再点一次将强制重测");
  }
  return parts.join(" · ");
}

export function renderModelCapabilities(test = {}) {
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
  setCapability("#capability-native", Boolean(test.nativeTools));
  setCapability("#capability-chat", Boolean(test.chat));
  const capabilities = $("#settings-capabilities");
  if (capabilities) capabilities.hidden = false;
  const detail = $("#settings-probe-detail");
  if (detail) {
    detail.textContent = modelProbeDetail(test);
    detail.hidden = !detail.textContent;
  }
  return presentation;
}
