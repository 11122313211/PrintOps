/* Browser-local PDF basic inspection (artwork stays local) and preflight metadata upload. */

import { actions, appState } from "./store.js";
import { $, showToast } from "./dom.js";
import { api, apiErrorText } from "./api.js";

export function clearPdfPreview() {
  if (appState.pdfPreview?.url) URL.revokeObjectURL(appState.pdfPreview.url);
  appState.pdfPreview = null;
}

export async function inspectPdf(file) {
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

export async function uploadFile(file) {
  if (appState.isSending || appState.orderWritePending) return;
  const uploadScope = {sessionId: appState.sessionId, expectedRevision: appState.state.revision, itemId: appState.state.order?.items?.[appState.state.activeItemIndex]?.itemId};
  if (appState.state.order?.items?.length > 1 && !uploadScope.itemId) {
    showToast("请先在订单摘要中选择处理的产品项，再上传文件。", "error");
    return;
  }
  clearPdfPreview();
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
  appState.fileFeedback = { ok: true, fileName: file.name, message: "文件已加载，正在做基础预检。" };
  actions.renderFileState();
  const inspection = await inspectPdf(file);
  if (!inspection.readable || !inspection.isPdf) {
    clearPdfPreview();
    appState.fileFeedback = { ok: false, fileName: file.name, message: "文件不是有效的 PDF，或内容无法读取。" };
    actions.renderFileState();
    actions.addMessage("assistant", appState.fileFeedback.message);
    return;
  }
  appState.pdfPreview = {
    url: URL.createObjectURL(file),
    fileName: file.name,
    size: file.size,
    type: file.type || "application/pdf",
    lastModified: file.lastModified,
    inspection
  };
  actions.renderFileState();
  try {
    const multiItems = Array.isArray(appState.state.order?.items) && appState.state.order.items.length > 1;
    const data = await api("/api/preflight", {
      ...uploadScope,
      fileName: file.name,
      sizeBytes: file.size,
      pageCount: inspection.pageCount,
      encrypted: inspection.encrypted,
      readable: inspection.readable,
      inspection
    });
    if (uploadScope.sessionId !== appState.sessionId) return;
    const currentItemId = appState.state.order?.items?.[appState.state.activeItemIndex]?.itemId;
    if (uploadScope.itemId && currentItemId !== uploadScope.itemId) {
      appState.fileFeedback = null;
      clearPdfPreview();
      actions.render(await api("/api/focus", {sessionId: appState.sessionId, itemId: currentItemId, expectedRevision: data.revision}), false);
      return;
    }
    appState.fileFeedback = null;
    if (appState.pdfPreview) appState.pdfPreview.preflight = data.toolResult || null;
    if (data.toolResult?.status === "blocked") {
      appState.fileFeedback = { ok: false, fileName: file.name, message: data.messages?.[0] || "请先选择产品项，再绑定文件。" };
    } else if (data.toolResult && !data.toolResult.ok) {
      appState.fileFeedback = data.toolResult;
    }
    actions.render(data);
  } catch (error) {
    clearPdfPreview();
    appState.fileFeedback = { ok: false, fileName: file.name, message: apiErrorText(error, "文件预检调用失败，请重试") };
    actions.renderFileState();
    showToast(appState.fileFeedback.message, "error");
  }
}
