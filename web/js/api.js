/* HTTP API transport with local token header, 409/401 recovery, and unified error phrasing. */

import { appState, actions, responseView } from "./store.js";
import { $, showToast } from "./dom.js";

export async function api(path, body, method = "POST", options = {}) {
  const epoch = appState.viewEpoch;
  const requestSession = body?.sessionId || appState.sessionId;
  const headers = { "Content-Type": "application/json" };
  if (window.__PRINTOPS_TOKEN__) headers["X-PrintOps-Token"] = window.__PRINTOPS_TOKEN__;
  if (method !== "GET" && body?.sessionId === appState.sessionId && Number.isInteger(appState.state.revision) && !["/api/session", "/api/session/context", "/api/pricing/simulate"].includes(path)) {
    body = {...body, expectedRevision: body.expectedRevision ?? appState.state.revision};
  }
  const scoped = ["/api/chat", "/api/choose", "/api/preflight", "/api/tools/call", "/api/fields"].includes(path);
  if (scoped && body?.itemId == null && body?.itemIndex == null && appState.state.order?.items?.length > 1) {
    body = {...body, itemId: appState.state.order.items[appState.state.activeItemIndex]?.itemId};
  }
  const mutation = method !== "GET" && Boolean(body?.sessionId) && !["/api/session", "/api/session/context", "/api/focus", "/api/pricing/simulate"].includes(path);
  if (mutation && appState.viewLoading) throw new Error("正在切换订单或产品，请稍后继续。");
  if (mutation && requestSession === appState.sessionId && (appState.state.status === "archived" || appState.state.orderStatus === "archived")) throw new Error("已归档订单只能查看或导出。");
  if (mutation && appState.unresolvedWrites.has(body.sessionId)) {
    const unresolved = appState.unresolvedWrites.get(body.sessionId);
    PrintOpsJourney.failure(unresolved.error, unresolved.request, recoverOperation);
    throw new Error("上次保存结果尚未确认，请先使用恢复提示读取最新状态。");
  }
  if (mutation && appState.orderWritePending) throw new Error("上一项修改正在保存，请稍后继续。");
  if (mutation) body = {...body, operationId: body.operationId || crypto.randomUUID()};
  const fieldBefore = path === "/api/fields" ? (body.fields || Object.keys(body.values || {})).map(path => {
    const field = actions.reviewField(path, body.itemId);
    return {fieldPath: path, label: field.label, value: field.value ?? field.candidateValue ?? null};
  }) : [];
  if (mutation) { appState.orderWritePending = true; PrintOpsJourney.saved("正在保存…", "saving"); }
  try {
  const response = await fetch(path, {
    method,
    headers,
    body: method === "GET" ? undefined : JSON.stringify(body || {}),
    signal: options.signal
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    const error = new Error(detail.message || detail.error || `API ${response.status}`);
    error.code = detail.code;
    error.status = response.status;
    error.requestId = detail.requestId || response.headers.get("X-Request-ID") || "";
    throw error;
  }
  const result = await response.json();
  if (result && typeof result === "object") result[responseView] = epoch;
  if (result.needsReconciliation) {
    const error = new Error("这次请求已有执行记录，但尚无最终完成回执。请先读取最新订单并核对内容。");
    error.status = 408; error.code = "OPERATION_RECONCILIATION_REQUIRED";
    throw error;
  }
  if (mutation && epoch === appState.viewEpoch && requestSession === appState.sessionId) {
    PrintOpsJourney.clearFailure();
    PrintOpsJourney.saved(appState.storageInfo.persistent ? "已保存到本机" : "仅当前进程保存 · 退出后丢失");
  }
  return result;
  } catch (error) {
    if (error.name !== "AbortError") {
      const request = {path, body: structuredClone(body || {}), method, conflict: error.status === 409, fieldBefore};
      if (mutation && (!error.status || error.status >= 500 || error.status === 408)) appState.unresolvedWrites.set(body.sessionId, {error, request});
      if (epoch === appState.viewEpoch) PrintOpsJourney.failure(error, request, recoverOperation);
    }
    throw error;
  } finally {
    if (mutation) {
      appState.orderWritePending = false;
      actions.setComposerBusy(appState.isSending || appState.viewLoading);
    }
  }
}

export async function recoverOperation(request) {
  try {
    const id = request.body.sessionId || appState.sessionId;
    if (!id) { await actions.bootstrap(); return; }
    const latest = await api(`/api/orders/${encodeURIComponent(id)}`, undefined, "GET");
    if (id !== appState.sessionId || latest[responseView] !== appState.viewEpoch) return;
    appState.unresolvedWrites.delete(id);
    actions.render(latest, false);
    PrintOpsJourney.recovered(request, latest, async () => {
      if (id !== appState.sessionId || ["confirm", "reject"].includes(request.body.action)) return;
      try {
        const result = await api(request.path, {...request.body, operationId: crypto.randomUUID(), expectedRevision: latest.revision}, request.method);
        if (id !== appState.sessionId || result[responseView] !== appState.viewEpoch) return;
        if (request.path === "/api/fields") {
          for (const path of request.body.fields || Object.keys(request.body.values || {})) {
            PrintOpsJourney.fieldSaved(actions.reviewField(path, request.body.itemId), id, request.body.action === "clear" ? undefined : request.body.values[path]);
          }
        }
        actions.render(result);
      }
      catch { /* The persistent recovery card carries the error. */ }
    });
  } catch { /* Keep the error visible until the service can be read. */ }
}

export async function refreshCredentials(request) {
  try {
    // The homepage is same-origin and carries the server's current local token.
    // Parse only its JSON string; never execute scripts or discard the document.
    const response = await fetch("/", {cache: "no-store", credentials: "same-origin"});
    if (!response.ok) throw new Error("暂时无法读取本地访问凭据，请确认服务已启动。");
    const html = await response.text();
    const match = html.match(/window\.__PRINTOPS_TOKEN__\s*=\s*("(?:\\.|[^"\\])*")\s*;/);
    if (!match) throw new Error("本地服务未返回有效凭据，输入仍保留在当前页面。");
    const token = JSON.parse(match[1]);
    if (typeof token !== "string" || !token) throw new Error("本地访问凭据无效。");
    window.__PRINTOPS_TOKEN__ = token;
    if (!appState.state.sessionId) await actions.bootstrap();
    else await recoverOperation(request);
  } catch (error) { PrintOpsJourney.failure(error, request, recoverOperation); }
}

export function apiErrorText(error, fallback) {
  // TD-19: a code with dedicated copy in PrintOpsJourney.ERROR_TEXTS keeps
  // that copy; everything else falls back to the server message, then the
  // caller's per-site fallback.
  const known = PrintOpsJourney.ERROR_TEXTS?.[error?.code];
  const base = known?.title || error?.message || fallback;
  return error?.requestId ? `${base}（请求号 ${error.requestId}）` : base;
}
