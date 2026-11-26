/* Chat message stream, safe markdown rendering, composer preview, busy/archived controls, and send loop. */

import { actions, appState } from "../store.js";
import { $, isFeedNearBottom, scrollFeedToBottom, showToast } from "../dom.js";
import { api, apiErrorText } from "../api.js";
import { createPendingIndicator, createTurnHarnessNode } from "../harness.js";

export function reportMissingMarkdownRenderer() {
  if (appState.markdownFallbackReported) return;
  appState.markdownFallbackReported = true;
  console.warn("[PrintOps] markdown.js 未加载，气泡已回退为纯文本。请重启本地服务（python3 server.py）并强制刷新页面。");
  showToast("Markdown 渲染器未加载：请重启本地服务后刷新页面", "error");
}

export function renderMessageBody(node, text) {
  const value = String(text ?? "");
  const markdown = window.PrintOpsMarkdown;
  if (markdown?.render) node.innerHTML = markdown.render(value);
  else {
    node.textContent = value;
    reportMissingMarkdownRenderer();
  }
}

export function renderComposerPreview() {
  const input = $("#message-input");
  const toggle = $("#preview-toggle");
  const preview = $("#composer-preview");
  const body = $("#composer-preview-body");
  if (!input || !toggle || !preview || !body) return;
  const hasContent = Boolean(input.value.trim());
  toggle.hidden = !hasContent;
  const active = hasContent && toggle.getAttribute("aria-pressed") === "true";
  preview.hidden = !active;
  if (active) renderMessageBody(body, input.value);
}

export function addMessage(role, text, turnMeta = null) {
  const stick = isFeedNearBottom();
  const message = document.createElement("div");
  message.className = `message ${role}`;
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = role === "assistant" ? "AI" : "我";
  const content = document.createElement("div");
  content.className = "message-content";
  const bubble = document.createElement("div");
  bubble.className = "bubble markdown-body";
  renderMessageBody(bubble, text);
  const harnessNode = role === "assistant" && turnMeta ? createTurnHarnessNode(turnMeta) : null;
  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.textContent = `${role === "assistant" ? "印刷订单智能体" : "我"} · ${new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date())}`;
  content.append(bubble);
  if (harnessNode) content.append(harnessNode);
  content.append(meta);
  message.append(avatar, content);
  $("#chat-feed").appendChild(message);
  if (stick) scrollFeedToBottom();
}

export function addPendingMessage() {
  const stick = isFeedNearBottom();
  const indicator = createPendingIndicator();
  $("#chat-feed").appendChild(indicator.node);
  if (stick) scrollFeedToBottom();
  return indicator;
}

export function removePendingMessage() {
  appState.pendingMessage?.stop();
  appState.pendingMessage = null;
}

export function releaseArchivedControls() {
  document.querySelectorAll("[data-archived-disabled]").forEach((control) => {
    control.disabled = control.dataset.archivedDisabled === "true";
    delete control.dataset.archivedDisabled;
  });
}

export function applyArchivedState() {
  if (appState.state.status !== "archived" && appState.state.orderStatus !== "archived") {
    releaseArchivedControls();
    return;
  }
  document.querySelectorAll("#message-input, #message-form button[type='submit'], #upload-button, #slash-trigger, #drop-zone, #file-input, #confirm-order, #generate-order, #quote-refresh, #quote-cancel, #platform-select, #quick-replies button, #tool-list button, #catalog-list button, #parameter-form input, .field-input, .multi-product-editor-field input, .multi-product-focus, .field-confirm, .harness-gate-btn, #option-list button, .preset-apply, #request-options, .review-editor input, .review-editor button, .field-review-actions button").forEach((control) => {
    if (!control.hasAttribute("data-archived-disabled")) control.dataset.archivedDisabled = String(control.disabled);
    control.disabled = true;
  });
  $("#memory-status").textContent = "已归档 · 只读，可查看或导出；新建订单后可继续编辑。";
}

export function releaseBusyControls() {
  document.querySelectorAll("[data-busy-disabled]").forEach(control => {
    control.disabled = control.dataset.busyDisabled === "true";
    delete control.dataset.busyDisabled;
  });
}

export function setComposerBusy(busy) {
  releaseArchivedControls();
  if (!busy) releaseBusyControls();
  if (busy) document.querySelectorAll(".field-input, .multi-product-editor input, .multi-product-focus, .field-confirm, .harness-gate-btn, #slash-trigger, #request-options, .review-editor input, .review-editor button, .field-review-actions button, #next-task-action, .choose-option, #drop-zone, #generate-order, #confirm-order, #quote-cancel, #reset-order").forEach(control => {
    if (!control.hasAttribute("data-busy-disabled")) control.dataset.busyDisabled = String(control.disabled);
    control.disabled = true;
  });
  const input = $("#message-input");
  const send = $("#message-form button[type='submit']");
  const upload = $("#upload-button");
  const quoteRefresh = $("#quote-refresh");
  const label = send?.querySelector(".send-label");
  if (input) input.disabled = busy;
  if (send) {
    send.disabled = busy;
    send.classList.toggle("is-loading", busy);
    send.setAttribute("aria-busy", String(busy));
  }
  if (label) label.textContent = busy ? "处理中" : "发送";
  if (upload) upload.disabled = busy;
  if (quoteRefresh) quoteRefresh.disabled = busy;
  const platform = $("#platform-select");
  if (platform) platform.disabled = busy;
  const chatCancel = $("#chat-cancel");
  // The stop control only makes sense while a chat turn is actually waiting;
  // other busy callers (field saves, view loads) keep it hidden.
  if (chatCancel) chatCancel.hidden = !(busy && appState.isSending);
  document.querySelectorAll("#quick-replies button, #tool-list button, #catalog-list button, #parameter-form input, #preview-toggle").forEach((item) => { item.disabled = busy; });
  if (busy) {
    $("#agent-status").innerHTML = '<span class="status-dot thinking"></span>正在处理';
    $("#agent-trace").textContent = "正在分析需求…";
  }
  applyArchivedState();
}

export async function sendMessage(text, patch) {
  if (!text?.trim() || appState.isSending || appState.chatSettleWait) return;
  const requestSession = appState.sessionId;
  // A stopped turn keeps running on the server and holds the session lock,
  // and its response is the only reliable carrier of the post-turn revision
  // (reads are lock-free and can race the turn). Let it land before the next
  // request, or the server's CAS check rejects the request as a conflict.
  if (appState.stoppedChatSettling && appState.activeChatSettle) {
    appState.chatSettleWait = true;
    const settle = appState.activeChatSettle;
    appState.stoppedChatSettling = false;
    appState.activeChatSettle = null;
    $("#agent-trace").textContent = "正在等待上一条回复完成…";
    const send = $("#message-form button[type='submit']");
    if (send) send.disabled = true;
    try { await settle; } catch { /* its own error path already ran */ }
    appState.chatSettleWait = false;
    setComposerBusy(appState.isSending || appState.viewLoading);
    // The wait can straddle an order switch or reset; the queued text must
    // not be posted into whatever session is current afterwards.
    if (appState.sessionId !== requestSession) return;
  }
  const requestSeq = ++appState.chatRequestSeq;
  const controller = new AbortController();
  appState.activeChatController?.abort();
  appState.activeChatController = controller;
  appState.isSending = true;
  addMessage("user", text);
  $("#message-input").value = "";
  autoGrowComposer();
  renderComposerPreview();
  setComposerBusy(true);
  appState.pendingMessage = addPendingMessage();
  const settle = (async () => {
    try {
      const result = await api("/api/chat", { sessionId: appState.sessionId, text, patch }, "POST", { signal: controller.signal });
      if (requestSeq !== appState.chatRequestSeq) return;
      removePendingMessage();
      actions.render(result);
    } catch (error) {
      if (error?.name === "AbortError" || requestSeq !== appState.chatRequestSeq) return;
      removePendingMessage();
      const suffix = error?.requestId ? `（请求号 ${error.requestId}）` : "";
      addMessage("assistant", `${error?.message || "智能体暂时无法连接，请确认本地服务已启动。"}${suffix}`);
      $("#agent-status").textContent = error.status === 409 ? "需要核对最新版本" : "操作尚未完成";
      // A stopped turn must not clobber whatever the user typed afterwards.
      if (!appState.stoppedChatSettling) {
        $("#message-input").value = text;
        autoGrowComposer();
        renderComposerPreview();
        showToast("未完成的输入已保留；恢复连接后可修改并重新发送", "error");
      }
    } finally {
      const stopped = appState.stoppedChatSettling;
      appState.stoppedChatSettling = false;
      if (requestSeq !== appState.chatRequestSeq) return;
      appState.activeChatController = null;
      appState.isSending = false;
      // Recompute instead of clearing: a stopped turn can land while a view
      // load or field write is keeping other controls busy.
      setComposerBusy(appState.viewLoading || appState.orderWritePending);
      // A late response from a stopped turn must not steal focus the user
      // has moved elsewhere while waiting.
      if (!patch && (!stopped || document.activeElement === document.body || document.activeElement === $("#message-input"))) $("#message-input").focus();
    }
  })();
  appState.activeChatSettle = settle;
  settle.catch(() => {});
}

export function autoGrowComposer() {
  const input = $("#message-input");
  if (!input) return;
  input.style.height = "auto";
  input.style.height = `${Math.max(appState.layout.composerH, Math.min(120, Math.max(38, input.scrollHeight)))}px`;
}
