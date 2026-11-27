/* Native ES Module entrypoint for PrintOps Workbench (zero bundler, zero framework). */

import { DESKTOP_QUERY, actions, appState, cloneData, notify, responseView } from "./store.js";
import { $, reportMissingBindings, showToast } from "./dom.js";
import { api, apiErrorText, refreshCredentials } from "./api.js";
import { getActivePdfPreview, syncActivePdfPreview } from "./pdf.js";
import { renderBlueprintPanel } from "./blueprint.js";
import { renderPricingSimulator, resetSimulatorState } from "./pricing_simulator.js";
import {
  applyComposerHeight, applyLayout, loadLayout, setHighContrast,
  setSidebarCollapsed, setupComposerResize, setupSplitters,
} from "./layout.js";
import {
  closeHistoryContextMenu, migrateLocalHistory, refreshOrderHistory,
  rememberOrderHistory, renderOrderHistory, switchOrder,
} from "./history.js";
import { closePresetDrawer, renderSpecPresets } from "./presets.js";
import { exportOrder } from "./export.js";
import {
  addMessage, applyArchivedState, autoGrowComposer, releaseArchivedControls,
  releaseBusyControls, removePendingMessage, sendMessage, setComposerBusy,
} from "./render/chat.js";
import {
  changeField, focusDraftField, focusItem, renderDraft, renderParameterForm, reviewField, trackOrderChanges,
} from "./render/draft.js";
import { renderOptions, renderProgress, renderQuickReplies } from "./render/options.js";
import {
  callTool, renderCatalog, renderDecision, renderFileState, renderPriceEstimate,
  renderProviderStatus, renderQuoteStatus, renderRunTrace, renderTools, renderValidation,
} from "./render/panels.js";
import { closeSettingsDialog, renderSettings } from "./render/settings.js";
import { buildTurnMeta, syncInlineGates, updateHarnessHeaderMeta } from "./harness.js";
import { closeSlashMenu } from "./commands.js";
import { bindEvents } from "./events.js";

function sliceChanged(key, value) {
  let next;
  try { next = JSON.stringify(value); } catch { return true; }
  if (appState.renderFingerprints[key] === next) return false;
  appState.renderFingerprints[key] = next;
  return true;
}

function activeOrderSnapshot() {
  const order = appState.state.order || {};
  const items = Array.isArray(order.items) ? order.items : [];
  if (items.length > 1) {
    const idx = Number.isInteger(appState.state.activeItemIndex) && items[appState.state.activeItemIndex]
      ? appState.state.activeItemIndex : 0;
    const item = items[idx] || {};
    return { order: item, itemIndex: idx, itemId: item.itemId || appState.state.activeItemId || null, isMulti: true };
  }
  return { order, itemIndex: null, itemId: items[0]?.itemId || null, isMulti: false };
}

function activePdfInspectionForBlueprint() {
  const { itemId, isMulti } = activeOrderSnapshot();
  const localPreview = getActivePdfPreview();
  const storedCheck = isMulti && itemId
    ? appState.state.preflightResults?.[itemId]
    : appState.state.preflightResults;
  if (localPreview?.inspection) {
    const preflight = localPreview.preflight || storedCheck || null;
    return {
      ...localPreview.inspection,
      ...(preflight?.observedSizeMm ? { observedSizeMm: preflight.observedSizeMm } : {}),
      ...(preflight?.measuredBleedMm != null ? { bleedMm: preflight.measuredBleedMm } : {}),
      ...(preflight?.checks ? { checks: preflight.checks } : {}),
    };
  }
  if (storedCheck && typeof storedCheck === "object" && !storedCheck.stale && (storedCheck.checks || storedCheck.observedSizeMm)) {
    return {
      ...(storedCheck.observedSizeMm ? { observedSizeMm: storedCheck.observedSizeMm } : {}),
      ...(storedCheck.measuredBleedMm != null ? { bleedMm: storedCheck.measuredBleedMm } : {}),
      checks: storedCheck.checks || [],
    };
  }
  return null;
}

function renderBlueprints(force = false) {
  const { order, itemId } = activeOrderSnapshot();
  const pdfInfo = activePdfInspectionForBlueprint();
  const fingerprint = [
    appState.sessionId, itemId, order?.productType, order?.size, order?.dimensions,
    order?.pages, order?.paper, order?.binding, order?.productSpecs, pdfInfo,
  ];
  if (!force && !sliceChanged("blueprint", fingerprint)) return;
  renderBlueprintPanel($("#spec-blueprint-host"), order, pdfInfo, { compact: true });
  renderBlueprintPanel($("#review-blueprint-host"), order, pdfInfo, { compact: false });
}

async function applySimulatorOverrides(fieldValues) {
  if (!fieldValues || !Object.keys(fieldValues).length) return;
  if (appState.isSending || appState.orderWritePending || appState.viewLoading) return;
  const archived = appState.state.status === "archived" || appState.state.orderStatus === "archived";
  if (archived) {
    showToast("已归档订单仅支持只读试算，不能写入修改。", "error");
    return;
  }
  const origin = appState.sessionId;
  const { itemId, isMulti } = activeOrderSnapshot();
  appState.isSending = true;
  setComposerBusy(true);
  try {
    const body = { sessionId: origin, action: "set", values: fieldValues };
    if (isMulti && itemId) body.itemId = itemId;
    const result = await api("/api/fields", body);
    if (origin !== appState.sessionId) return;
    for (const [path, val] of Object.entries(fieldValues)) {
      PrintOpsJourney.fieldSaved(reviewField(path, isMulti ? itemId : undefined), origin, val);
    }
    render(result, false);
    showToast("已将试算配置写入订单草稿，请按当前版本重新核对方案与交接单");
  } catch (error) {
    showToast(apiErrorText(error, "试算配置写入失败，请重试"), "error");
    throw error;
  } finally {
    appState.isSending = false;
    setComposerBusy(false);
  }
}

function renderSimulator() {
  const host = $("#pricing-simulator-host");
  if (!host) return;
  const { itemIndex, itemId, isMulti } = activeOrderSnapshot();
  const archived = appState.state.status === "archived" || appState.state.orderStatus === "archived";
  const sessionId = appState.sessionId;
  renderPricingSimulator(host, {
    sessionId,
    state: appState.state,
    revision: appState.state.revision,
    itemIndex: isMulti ? itemIndex : null,
    itemId: isMulti ? itemId : null,
    archived,
    fetchInitial: () => {
      const params = new URLSearchParams({ sessionId });
      if (isMulti && itemId) params.set("itemId", itemId);
      else if (isMulti && Number.isInteger(itemIndex)) params.set("itemIndex", String(itemIndex));
      return api(`/api/pricing/simulate?${params.toString()}`, undefined, "GET");
    },
    fetchSimulate: (overrides) => api("/api/pricing/simulate", {
      sessionId,
      ...(isMulti && itemId ? { itemId } : isMulti && Number.isInteger(itemIndex) ? { itemIndex } : {}),
      overrides,
    }),
    onApplyOverrides: applySimulatorOverrides,
  });
}

export function render(data, showMessages = true) {
  if (data[responseView] !== undefined && data[responseView] !== appState.viewEpoch) return;
  if (data.sessionId === appState.sessionId && Number.isInteger(data.revision) && data.revision < appState.state.revision) return;
  // Release both overlays before computing availability for the new snapshot.
  releaseArchivedControls();
  releaseBusyControls();
  const prevOrder = cloneData(appState.state.order || {});
  const switchingSession = Boolean(data.sessionId && data.sessionId !== appState.sessionId);
  if (switchingSession) {
    appState.state = { ...appState.state, status: null, orderStatus: null };
    appState.renderFingerprints = Object.create(null);
    appState.activeUndoNoticeRevision = null;
    closeSlashMenu();
    resetSimulatorState();
  }
  appState.state = { ...appState.state, ...data, order: data.order || appState.state.order };
  trackOrderChanges(appState.state.order);
  if (data.llm) renderSettings({ llm: data.llm });
  appState.sessionId = data.sessionId || appState.sessionId;
  syncActivePdfPreview();
  if (appState.sessionId) {
    localStorage.setItem("printops_session", appState.sessionId);
    const orderNum = $("#order-number");
    orderNum.textContent = `订单 ${appState.sessionId.toUpperCase()}`;
    orderNum.title = `订单编号：${appState.sessionId.toUpperCase()}`;
  }
  if (showMessages) {
    const incoming = Array.isArray(data.messages) ? data.messages : [];
    const lastAssistantIdx = incoming.reduce((found, msg, idx) => {
      const role = typeof msg === "string" ? "assistant" : (msg?.role || "assistant");
      return role === "assistant" ? idx : found;
    }, -1);
    const turnMeta = lastAssistantIdx >= 0
      ? buildTurnMeta(appState.state, prevOrder, appState.updatedOrderKeys, appState.updatedSpecKeys)
      : null;
    incoming.forEach((message, idx) => {
      const role = typeof message === "string" ? "assistant" : message.role || "assistant";
      const text = typeof message === "string" ? message : message.text || "";
      addMessage(role, text, idx === lastAssistantIdx ? turnMeta : null);
    });
  }
  const s = appState.state;
  if (sliceChanged("draft", [
    appState.sessionId, s.order, s.missingFields, s.productProfile, s.fieldMeta,
    s.selectedOption, s.activeItemIndex, s.orderGenerated, s.confirmation,
    s.handoffDocument?.exportable, s.validation?.itemValidations, s.validation?.multiProduct,
    [...appState.updatedOrderKeys], [...appState.updatedSpecKeys],
  ])) {
    renderDraft();
  }
  // Avoid redundant #risk-list DOM churn: PrintOpsJourney.render() owns #risk-list whenever data.review is present.
  if (!s.review) renderValidation();
  if (sliceChanged("files", [
    appState.sessionId, s.activeItemIndex, s.uploadedFile, s.preflightResults,
    appState.fileFeedback, getActivePdfPreview()?.fileName || null, Boolean(getActivePdfPreview()),
    s.order?.items?.map((item) => item?.uploadedFile),
  ])) {
    renderFileState();
  }
  renderBlueprints();
  if (sliceChanged("quickReplies", s.quickReplies)) renderQuickReplies();
  if (sliceChanged("options", [
    appState.sessionId, s.options, s.selectedOption, s.activeItemSelectedOption,
    s.activeItemIndex, s.order?.productType, s.order?.quantity, s.order?.size,
  ])) {
    renderOptions();
  }
  renderSimulator();
  if (sliceChanged("tools", s.availableTools)) renderTools();
  renderDecision();
  if (sliceChanged("quote", [appState.sessionId, s.activeQuoteRequestId, s.quoteRequest, s.quoteRequests])) {
    renderQuoteStatus();
  }
  if (sliceChanged("provider", [s.order?.platform, appState.providers])) renderProviderStatus();
  renderPriceEstimate(data.toolResult);
  if (sliceChanged("runTrace", [s.runId, s.runTrace])) renderRunTrace();
  renderProgress();
  PrintOpsJourney.render(appState.state);
  syncInlineGates(appState.state);
  updateHarnessHeaderMeta(appState.state);
  rememberOrderHistory();
  renderOrderHistory();
  renderSpecPresets();
  $("#agent-trace").textContent = appState.state.workflowLabel || "本地订单整理";
  $("#memory-status").textContent = data.nextAction || (data.stage === "confirm" ? "订单记忆已锁定，等待人工确认。" : data.order?.productType ? "订单记忆已保存，可继续补充或修改。" : "等待第一条需求，Agent 将自动建立订单记忆。");
  $("#agent-status").innerHTML = '<span class="status-dot"></span>智能体在线';
  applyArchivedState();
  notify("state:rendered", appState.state);
}

export async function bootstrap() {
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
    appState.storageInfo = storage;
    $("#storage-status") && ($("#storage-status").textContent = storage.persistent ? "本机自动保存 · SQLite" : "仅当前进程保存");
    PrintOpsJourney.saved(storage.persistent ? "本机自动保存 · SQLite" : "仅当前进程保存 · 退出后丢失");
    const platformSelect = $("#platform-select");
    platformSelect.innerHTML = "";
    appState.providers = platforms.platforms;
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
    const snapshot = await api("/api/session", appState.sessionId ? { sessionId: appState.sessionId } : {});
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

// Wire shared action registry before binding events or booting
Object.assign(actions, {
  render,
  bootstrap,
  refreshOrderHistory,
  switchOrder,
  closeHistoryContextMenu,
  closePresetDrawer,
  closeSettingsDialog,
  closeSlashMenu,
  sendMessage,
  addMessage,
  removePendingMessage,
  setComposerBusy,
  applyArchivedState,
  applyComposerHeight,
  autoGrowComposer,
  setSidebarCollapsed,
  renderFileState,
  reviewField,
  changeField,
  focusDraftField,
  updateHarnessHeaderMeta,
  exportOrder,
});

// Initialize layout from persisted preferences before first paint
appState.layout = loadLayout();
bindEvents();

PrintOpsJourney.init({
  field: changeField, focus: focusItem, recommend: () => callTool("recommend_processes"),
  generate: () => $("#generate-order").click(), confirm: () => $("#confirm-order").click(), export: exportOrder,
  refreshCredentials,
  closeSidebar: () => setSidebarCollapsed(true),
  focusDraftField,
  canFocusInlineDraft: () => DESKTOP_QUERY.matches && !appState.layout.orderCollapsed,
  hasUndoNotice: (revision) => appState.activeUndoNoticeRevision != null && appState.activeUndoNoticeRevision === revision,
  onViewChange: (name) => {
    if (name === "compare") renderSimulator();
    if (name === "review") renderBlueprints(true);
  },
  syncControls: () => { if (appState.isSending || appState.orderWritePending || appState.viewLoading) setComposerBusy(true); else applyArchivedState(); }
});
setupSplitters();
setupComposerResize();
applyLayout();
autoGrowComposer();
// TD-06: every listed id was checked before wiring; report whatever is still
// missing in one message so a single broken element cannot hide behind the
// first TypeError.
reportMissingBindings();
bootstrap();
setSidebarCollapsed(appState.sidebarCollapsed);
setHighContrast(localStorage.getItem("printops_theme") === "high-contrast");
notify("boot", appState);
