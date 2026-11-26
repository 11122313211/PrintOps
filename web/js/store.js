/* Single centralized frontend state store (eliminates all top-level mutable globals). */

import { LAYOUT_DEFAULTS } from "./constants.js";

export const DESKTOP_QUERY = window.matchMedia("(min-width: 1051px)");
export const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
export const scrollBehavior = () => (prefersReducedMotion.matches ? "auto" : "smooth");
export const responseView = Symbol("responseView");
export const pendingFieldFocus = new WeakMap();

export const appState = {
  sessionId: localStorage.getItem("printops_session") || "",
  isSending: false,
  state: {
    order: {}, stage: "collect", workflowStage: "collect", quickReplies: [],
    options: [], selectedOption: null, activeItemSelectedOption: null,
    orderGenerated: false, handoff: null, confirmation: { status: "not_ready" },
    quoteRequest: null, quoteRequests: [], activeQuoteRequestId: null,
    toolTrace: [], runTrace: [], availableTools: [], productProfile: null,
    fieldMeta: {}, conflicts: [], activeItemIndex: null,
  },
  renderedProduct: "",
  hadRecommendations: false,
  llmSettings: { enabled: false, url: "", model: "", keyConfigured: false },
  pendingMessage: null,
  activeChatController: null,
  chatRequestSeq: 0,
  stoppedChatSettling: false,
  activeChatSettle: null,
  chatSettleWait: false,
  lastFocusedElement: null,
  sidebarCollapsed: localStorage.getItem("printops_sidebar") === "collapsed" ||
    (!localStorage.getItem("printops_sidebar") && window.matchMedia("(max-width: 1050px)").matches),
  previousOrder: {},
  updatedOrderKeys: new Set(),
  updatedSpecKeys: new Set(),
  suppressOrderDiff: true,
  fileFeedback: null,
  pdfPreview: null,
  serverOrderHistory: [],
  settingsOrders: [],
  settingsHistorySelection: new Set(),
  historyRequestSeq: 0,
  providers: [],
  storageInfo: { mode: "sqlite", persistent: true },
  orderWritePending: false,
  undoChange: null,
  activeUndoNoticeRevision: null,
  renderFingerprints: Object.create(null),
  unresolvedWrites: new Map(),
  viewEpoch: 0,
  viewLoading: false,
  settingsView: "overview",
  settingsAbort: null,
  settingsProbeUsedCache: false,
  layout: { ...LAYOUT_DEFAULTS },
  historyMenuTrigger: null,
  markdownFallbackReported: false,
  wasDesktop: DESKTOP_QUERY.matches,
};

const listeners = new Map();

export function subscribe(channel, fn) {
  if (!listeners.has(channel)) listeners.set(channel, new Set());
  listeners.get(channel).add(fn);
  return () => listeners.get(channel)?.delete(fn);
}

export function notify(channel, payload) {
  const subs = listeners.get(channel);
  if (!subs) return;
  for (const fn of subs) fn(payload, appState);
}

export function cloneData(value) {
  if (typeof structuredClone === "function") return structuredClone(value);
  return JSON.parse(JSON.stringify(value));
}

/* Late-bound action registry to break cross-module UI orchestration cycles cleanly. */
export const actions = {};
