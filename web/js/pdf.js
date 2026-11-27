/* Browser-local PDF deep inspection (artwork stays local), multi-item preview cache, and preflight metadata upload. */

import { actions, appState } from "./store.js";
import { showToast } from "./dom.js";
import { api, apiErrorText } from "./api.js";
import { inspectPdfBytes } from "./pdf_parser.js";

function previewMap() {
  if (!(appState.pdfPreviews instanceof Map)) {
    appState.pdfPreviews = new Map();
  }
  return appState.pdfPreviews;
}

function previewKeyFor(sessionId, itemId) {
  return `${sessionId || "default"}::${itemId || "single"}`;
}

function resolveActiveItemId(state) {
  const items = Array.isArray(state?.order?.items) ? state.order.items : null;
  if (!items || !items.length) return null;
  const idx = state?.activeItemIndex;
  if (typeof idx === "number" && idx >= 0 && idx < items.length) {
    return items[idx]?.itemId || null;
  }
  if (items.length === 1) {
    return items[0]?.itemId || null;
  }
  return null;
}

function revokePreviewEntry(entry, map) {
  if (!entry?.url || typeof URL?.revokeObjectURL !== "function") return;
  if (map) {
    for (const existing of map.values()) {
      if (existing !== entry && existing?.url === entry.url) return;
    }
  }
  URL.revokeObjectURL(entry.url);
}

export function activePreviewKey(state = appState.state, sessionId = appState.sessionId) {
  return previewKeyFor(sessionId, resolveActiveItemId(state));
}

export function syncActivePdfPreview() {
  const map = previewMap();
  const key = activePreviewKey();
  const preview = map.get(key) || null;
  appState.pdfPreview = preview;
  return preview;
}

export function getActivePdfPreview() {
  return syncActivePdfPreview();
}

export function clearPdfPreview(all = false) {
  const map = previewMap();
  if (all === true) {
    const entries = [...map.values()];
    map.clear();
    for (const entry of entries) revokePreviewEntry(entry, null);
    if (appState.pdfPreview && !entries.includes(appState.pdfPreview)) {
      revokePreviewEntry(appState.pdfPreview, null);
    }
    appState.pdfPreview = null;
    return;
  }

  if (typeof all === "string" && all.length > 0) {
    const prefix = `${all}::`;
    for (const [key, entry] of [...map.entries()]) {
      if (entry?.sessionId === all || key.startsWith(prefix)) {
        map.delete(key);
        revokePreviewEntry(entry, map);
      }
    }
    syncActivePdfPreview();
    return;
  }

  const key = activePreviewKey();
  const existing = map.get(key) || null;
  if (existing) {
    map.delete(key);
    revokePreviewEntry(existing, map);
  }
  if (appState.pdfPreview && appState.pdfPreview !== existing) {
    const stillCached = [...map.values()].includes(appState.pdfPreview);
    if (!stillCached) revokePreviewEntry(appState.pdfPreview, map);
  }
  appState.pdfPreview = null;
}

function deletePreviewByKey(key) {
  const map = previewMap();
  const existing = map.get(key) || null;
  if (existing) {
    map.delete(key);
    revokePreviewEntry(existing, map);
  }
  if (appState.pdfPreview === existing) {
    appState.pdfPreview = null;
  }
}

export async function inspectPdf(file) {
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    return await inspectPdfBytes(bytes);
  } catch {
    return {
      readable: false,
      isPdf: false,
      pdfVersion: null,
      pageCount: null,
      encrypted: false,
      hasEof: false,
      boxes: { media: null, crop: null, trim: null, bleed: null },
      colorSpaces: [],
      fontEmbedding: "unknown",
      imageCount: 0,
      hasTransparency: false,
      hasOverprint: false,
      compressedStreams: { detected: 0, decoded: 0, objStmCount: 0 },
      pageSummary: {
        analyzedPages: 0,
        uniformSize: true,
        mixedOrientation: false,
        variants: [],
        mismatchPages: [],
      },
      bleedAnalysis: { source: "none", minMm: null, maxMm: null },
      imageSummary: {
        count: 0,
        minWidthPx: null,
        minHeightPx: null,
        lowResCount: 0,
        lowResSuspects: [],
      },
    };
  }
}

export async function uploadFile(file) {
  if (appState.isSending || appState.orderWritePending) return;
  const uploadScope = {
    sessionId: appState.sessionId,
    expectedRevision: appState.state.revision,
    itemId: appState.state.order?.items?.[appState.state.activeItemIndex]?.itemId,
  };
  if (appState.state.order?.items?.length > 1 && !uploadScope.itemId) {
    showToast("请先在订单摘要中选择处理的产品项，再上传文件。", "error");
    return;
  }
  const scopeKey = previewKeyFor(uploadScope.sessionId, uploadScope.itemId || resolveActiveItemId(appState.state));
  deletePreviewByKey(scopeKey);
  syncActivePdfPreview();
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    appState.fileFeedback = { ok: false, fileName: file.name, message: "目前仅支持上传 PDF 文件。" };
    actions.addMessage("user", `上传文件：${file.name}`);
    showToast(appState.fileFeedback.message, "error");
    actions.renderFileState();
    return;
  }
  actions.addMessage("user", `上传文件：${file.name}`);
  if (file.size > 20 * 1024 * 1024) {
    appState.fileFeedback = { ok: false, fileName: file.name, message: "文件超过 20 MB，请压缩后再上传。" };
    showToast(appState.fileFeedback.message, "error");
    actions.renderFileState();
    return;
  }
  appState.fileFeedback = { ok: true, fileName: file.name, message: "文件已加载，正在做本地深度预检。" };
  actions.renderFileState();
  const inspection = await inspectPdf(file);
  if (!inspection.readable || !inspection.isPdf) {
    deletePreviewByKey(scopeKey);
    syncActivePdfPreview();
    appState.fileFeedback = { ok: false, fileName: file.name, message: "文件不是有效的 PDF，或内容无法读取。" };
    actions.renderFileState();
    actions.addMessage("assistant", appState.fileFeedback.message);
    return;
  }
  const previewEntry = {
    url: URL.createObjectURL(file),
    fileName: file.name,
    size: file.size,
    type: file.type || "application/pdf",
    lastModified: file.lastModified,
    inspection,
    preflight: null,
    itemId: uploadScope.itemId || resolveActiveItemId(appState.state) || null,
    sessionId: uploadScope.sessionId || "",
  };
  previewMap().set(scopeKey, previewEntry);
  syncActivePdfPreview();
  actions.renderFileState();
  try {
    const data = await api("/api/preflight", {
      ...uploadScope,
      fileName: file.name,
      sizeBytes: file.size,
      pageCount: inspection.pageCount,
      encrypted: inspection.encrypted,
      readable: inspection.readable,
      inspection,
    });
    if (uploadScope.sessionId !== appState.sessionId) {
      previewEntry.preflight = data.toolResult || null;
      return;
    }
    const currentItemId = appState.state.order?.items?.[appState.state.activeItemIndex]?.itemId;
    if (uploadScope.itemId && currentItemId !== uploadScope.itemId) {
      previewEntry.preflight = data.toolResult || null;
      appState.fileFeedback = null;
      syncActivePdfPreview();
      actions.render(
        await api("/api/focus", {
          sessionId: appState.sessionId,
          itemId: currentItemId,
          expectedRevision: data.revision,
        }),
        false
      );
      return;
    }
    appState.fileFeedback = null;
    previewEntry.preflight = data.toolResult || null;
    syncActivePdfPreview();
    if (data.toolResult?.status === "blocked") {
      appState.fileFeedback = { ok: false, fileName: file.name, message: data.messages?.[0] || "请先选择产品项，再绑定文件。" };
    } else if (data.toolResult && !data.toolResult.ok) {
      appState.fileFeedback = data.toolResult;
    }
    actions.render(data);
  } catch (error) {
    deletePreviewByKey(scopeKey);
    syncActivePdfPreview();
    appState.fileFeedback = { ok: false, fileName: file.name, message: apiErrorText(error, "文件预检调用失败，请重试") };
    actions.renderFileState();
    showToast(appState.fileFeedback.message, "error");
  }
}
