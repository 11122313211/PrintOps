/* Task-oriented presentation. Validation and confirmation remain server-owned. */
(function (root) {
  const $ = (selector) => document.querySelector(selector);
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const statuses = {missing: "未填写", pending_confirmation: "待确认", confirmed: "已确认", observed: "已填写", inferred: "待确认", filled: "已填写", stale: "需重新核对"};
  let callbacks, snapshot = {}, currentView = "intake", lastOrder = "", lastRevision;
  const draftKey = "printops_review_drafts_v1";
  let drafts = {};
  try { const stored = JSON.parse(sessionStorage.getItem(draftKey) || "{}"); if (stored && typeof stored === "object" && !Array.isArray(stored)) drafts = stored; } catch { /* Draft persistence is optional. */ }
  const draftId = (orderId, fieldId) => JSON.stringify([orderId, fieldId]);
  function persistDrafts() { try { sessionStorage.setItem(draftKey, JSON.stringify(drafts)); } catch { /* Keep the in-memory copy. */ } }
  function fieldSaved(field, orderId, expectedValue) {
    const key = draftId(orderId, field.fieldId);
    if (expectedValue !== undefined && drafts[key] && drafts[key].value.trim() !== String(expectedValue).trim()) return;
    delete drafts[key]; persistDrafts();
  }
  const button = (text, action, className = "secondary-action") => {
    const control = el("button", text, className);
    control.type = "button";
    control.addEventListener("click", action);
    return control;
  };
  function view(name, focus = false) {
    currentView = name;
    $(".task-bar").hidden = name === "intake";
    for (const pane of document.querySelectorAll(".journey-pane")) pane.hidden = pane.id !== `phase-${name}`;
    for (const tab of document.querySelectorAll("[data-view]")) {
      if (tab.dataset.view === name) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    }
    $("#workspace").dataset.view = name;
    $("#workspace-title").textContent = {intake: "把想法说出来", compare: "比较效果与成本", review: "把每一项核对清楚"}[name];
    if (snapshot.sessionId && lastOrder === snapshot.sessionId) render(snapshot);
    if (focus) $(name === "intake" ? "#message-input" : `#phase-${name}`).focus({preventScroll: true});
  }
  function init(actions) {
    callbacks = actions;
    $("#phase-compare").append($("#estimate-panel"), $("#recommendations"));
    $("#phase-review").append($(".order-actions"), $("#order-export"));
    for (const pane of document.querySelectorAll(".journey-pane")) pane.tabIndex = -1;
    for (const tab of document.querySelectorAll("[data-view]")) tab.addEventListener("click", () => {
      if (tab.closest(".order-section") && $("#workspace").dataset.mobilePane === "order") $("#mobile-back").click();
      view(tab.dataset.view, true);
    });
    for (const example of document.querySelectorAll("[data-example]")) example.addEventListener("click", () => {
      $("#message-input").value = example.dataset.example;
      $("#message-input").dispatchEvent(new Event("input"));
      $("#message-input").focus();
    });
    $("#request-options").addEventListener("click", () => callbacks.recommend());
  }
  function status(field) { return statuses[field.status] || (field.value ? "已填写" : "未填写"); }
  function renderReview(data) {
    const root = $("#review-checklist");
    const sameOrder = root.dataset.orderId === data.sessionId;
    const expanded = new Set(sameOrder ? [...root.querySelectorAll(".review-editor[open]")].map(node => node.querySelector("input")?.dataset.reviewInput) : []);
    const openGroups = new Map(sameOrder ? [...root.querySelectorAll(".review-group")].map(node => [node.dataset.itemId, node.open]) : []);
    const focused = sameOrder && root.contains(document.activeElement) ? document.activeElement : null;
    const focus = focused?.dataset.reviewInput ? {id: focused.dataset.reviewInput, start: focused.selectionStart, end: focused.selectionEnd} : null;
    const readonly = data.status === "archived" || data.orderStatus === "archived";
    root.replaceChildren();
    root.dataset.orderId = data.sessionId || "";
    const fields = (data.review?.fields || []).filter(field => field.required || field.value != null || field.candidateValue != null);
    if (!fields.length) { root.append(el("p", "填写需求后，这里会列出可核对的生产参数。")); return; }
    const groups = new Map();
    for (const field of fields) {
      const key = field.itemId || "order";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(field);
    }
    for (const [itemId, entries] of groups) {
      const group = el("details", null, "review-group");
      group.dataset.itemId = itemId;
      group.open = openGroups.has(itemId) ? openGroups.get(itemId) : groups.size === 1 || data.activeItemId === itemId;
      const item = data.order?.items?.find((entry) => entry.itemId === itemId);
      const summary = el("summary", `${item ? `${(entries[0].itemIndex ?? 0) + 1}. ${item.productType || "产品"}` : "订单参数"} · ${entries.filter(f => ["missing", "pending_confirmation"].includes(f.status) && f.required).length} 项需处理`);
      group.append(summary);
      for (const field of entries) {
        const row = el("div", null, "review-field");
        row.dataset.fieldId = field.fieldId;
        const heading = el("div", null, "review-field-heading");
        heading.append(el("strong", field.label), el("span", status(field), `field-state state-${field.status}`));
        row.append(heading);
        const display = field.value ?? field.candidateValue;
        row.append(el("p", display == null || display === "" ? (field.required ? "待补充" : "未设置") : String(display), "review-value"));
        if (field.provenance) row.append(PrintOpsWorkbench.evidence(field.provenance));
        const edit = el("details", null, "review-editor");
        const key = draftId(data.sessionId, field.fieldId);
        const draft = drafts[key];
        edit.open = expanded.has(field.fieldId) || Boolean(draft);
        edit.append(el("summary", "编辑或清空"));
        const input = el("input");
        input.value = draft ? draft.value : display ?? "";
        input.disabled = readonly;
        input.setAttribute("aria-label", `核对 ${field.label} ${field.itemIndex != null ? `第 ${field.itemIndex + 1} 项` : "订单"}`);
        input.dataset.reviewInput = field.fieldId;
        input.addEventListener("input", () => {
          if (input.value === String(display ?? "")) delete drafts[key];
          else drafts[key] = {value: input.value, baseValue: draft?.baseValue ?? display ?? null};
          persistDrafts();
        });
        edit.append(input, button("保存修改", () => callbacks.field(field, input.value.trim() ? "set" : "clear", input.value.trim())));
        if (display != null && display !== "") edit.append(button("清空此字段", () => callbacks.field(field, "clear")));
        row.append(edit);
        if (draft) row.append(el("p", draft.baseValue !== (display ?? null) ? "尚未保存的修改；服务器值已变化，请核对后保存。" : "尚未保存的修改", "review-draft-status"));
        if (field.status === "pending_confirmation") {
          const actions = el("div", null, "field-review-actions");
          actions.append(button("确认候选值", () => callbacks.field(field, "confirm")), button("拒绝候选", () => callbacks.field(field, "reject")));
          row.append(actions);
        }
        if (readonly) row.querySelectorAll("input, button").forEach(control => { control.disabled = true; });
        group.append(row);
      }
      root.append(group);
    }
    if (focus) {
      const input = [...root.querySelectorAll("[data-review-input]")].find(node => node.dataset.reviewInput === focus.id);
      if (input && !input.disabled && !input.closest(".journey-pane")?.hidden) {
        input.focus({preventScroll: true});
        if (focus.start != null) input.setSelectionRange(focus.start, focus.end);
      }
    }
  }
  async function locate(issue) {
    if (issue.itemId && issue.itemId !== snapshot.activeItemId) await callbacks.focus(issue.itemId);
    if (issue.action === "compare") { view("compare"); ($(".choose-option:not(:disabled)") || $("#request-options")).focus(); return; }
    if (issue.action === "upload") { view("intake"); $("#upload-button").click(); return; }
    view("review");
    const row = [...document.querySelectorAll(".review-field")].find(node => node.dataset.fieldId === issue.fieldId);
    if (row) {
      $("#review-checklist-section").open = true;
      row.closest(".review-group").open = true;
      row.querySelector(".review-editor").open = true;
      row.scrollIntoView({block: "center", behavior: "smooth"});
      row.querySelector("input").focus({preventScroll: true});
    } else if (issue.kind === "stale") callbacks.generate();
    else { view("intake"); $("#message-input").focus(); }
  }
  function renderIssues(data) {
    const issues = data.review?.issues || [];
    const root = $("#risk-list");
    root.replaceChildren();
    $("#risk-panel").hidden = !issues.length;
    $("#risk-count").textContent = `${issues.filter(issue => issue.blocking).length} 项需处理`;
    for (const [section, label] of [["now", "现在需要处理"], ["handoff", "交接时说明"]]) {
      const selected = issues.filter(issue => (issue.section || (issue.blocking ? "now" : "handoff")) === section);
      if (!selected.length) continue;
      root.append(el("h4", label, "risk-section-heading"));
      for (const issue of selected) {
        const row = el("div", null, `risk-item risk-${issue.blocking ? "missing" : "info"}`);
        row.append(el("strong", issue.message));
        if (issue.fieldPath || issue.action === "compare") row.append(button("去处理", () => locate(issue), "issue-action"));
        root.append(row);
      }
    }
  }
  function renderDocument(document) {
    const preview = $("#handoff-preview");
    preview.replaceChildren();
    if (!document?.exportable) {
      preview.append(el("h3", "交接单尚未就绪"), el("p", "完成待处理项与方案选择后生成。未确认的候选不会写成确定的生产规格。"));
      return;
    }
    preview.append(el("p", "PRINTOPS / 本地交接资料", "eyebrow"));
    const text = document.markdown || document.text || "";
    const content = el("div", null, "handoff-content markdown-body");
    // Same allowlisted renderer used by chat; order text is never inserted as HTML.
    if (typeof PrintOpsMarkdown !== "undefined") content.innerHTML = PrintOpsMarkdown.render(text);
    else content.append(el("pre", text));
    let section;
    for (const node of [...content.childNodes]) {
      if (node.nodeName === "H2") { section = el("section", null, "handoff-section"); content.insertBefore(section, node); }
      if (section) section.append(node);
    }
    preview.append(content);
  }
  function render(data) {
    snapshot = data;
    const newOrder = lastOrder !== data.sessionId;
    if (newOrder) { view("intake"); lastRevision = undefined; lastOrder = data.sessionId; }
    const items = data.order?.items || [];
    const item = items[data.activeItemIndex];
    const context = items.length > 1 ? item ? `正在处理：第 ${data.activeItemIndex + 1} 项 · ${item.productType || "产品"}` : "多产品订单 · 请先选择处理项" : `当前产品：${data.order?.productType || "尚未填写"}`;
    $("#active-item-context").textContent = context;
    $("#drop-zone").setAttribute("aria-label", `${context}，拖拽或点击上传 PDF 文件`);
    $("#starter-panel").hidden = Boolean(data.order?.productType || data.order?.quantity || items.length);
    $("#compare-empty").hidden = Boolean(data.options?.length);
    $("#request-options").textContent = data.options?.length ? "重新生成本项方案" : "生成本项方案";
    renderReview(data);
    if (newOrder || !data.orderGenerated) $("#review-checklist-section").open = !data.orderGenerated;
    if (data.review) renderIssues(data);
    renderDocument(data.handoffDocument);
    const issue = data.review?.issues?.find(entry => entry.blocking);
    const archived = data.status === "archived" || data.orderStatus === "archived";
    let title, description, label, action;
    if (archived) { title = "已归档，只读查看"; description = "仍可从历史导出完整备份。"; label = "查看交接资料"; action = () => view("review", true); }
    else if (!data.order?.productType && !items.length) { title = "先说说你的印刷用途"; description = "可以从数量、用途或交期开始。"; label = "开始填写"; action = () => {view("intake");$("#message-input").focus();}; }
    else if (issue) { title = issue.message; description = `还有 ${data.review.blockingCount} 项需处理，确认后再交接。`; label = issue.action === "compare" ? "比较方案" : "处理这一项"; action = () => locate(issue); }
    else if (data.confirmation?.status === "confirmed") { title = "交接单已确认"; description = "可导出当前版本；供应商尚未接收。"; label = "导出交接单"; action = () => {view("review", true); callbacks.export("markdown");}; }
    else if (data.orderGenerated) { title = "核对交接单后确认"; description = "检查各产品的数量、尺寸、材料和文件。"; label = currentView === "review" ? "确认交接单" : "前往复核"; action = () => currentView === "review" ? callbacks.confirm() : view("review", true); }
    else if (data.review?.canGenerate || !$("#generate-order").disabled) { title = "生产参数与方案已齐备"; description = "生成当前版本交接草稿，进入最终核对。"; label = "生成交接草稿"; action = () => {view("review", true);callbacks.generate();}; }
    else if (data.options?.length) { title = "比较并选择本项方案"; description = context; label = "比较方案"; action = () => view("compare", true); }
    else if (data.order?.productType) { title = data.nextAction || "继续补充印刷参数"; description = context; label = "继续填写"; action = () => { view("intake"); $("#message-input").focus(); }; }
    else { title = "先说说你的印刷用途"; description = "可以从数量、用途或交期开始。"; label = "开始填写"; action = () => {view("intake");$("#message-input").focus();}; }
    $("#next-task-title").textContent = title;
    $("#next-task-description").textContent = description;
    const control = $("#next-task-action");
    control.textContent = label; control.onclick = action;
    $("#journey-announcement").textContent = title;
    if (lastRevision !== undefined && data.revision !== lastRevision && !newOrder) {
      const notice = $("#change-notice");
      notice.hidden = false;
      notice.textContent = data.orderGenerated ? "交接内容已更新，请核对当前版本。" : "订单已更新；相关方案、估算与交接确认以当前版本为准。";
    }
    lastRevision = data.revision;
    callbacks?.syncControls?.();
  }
  function saved(text, tone = "saved") { $("#save-status").textContent = text; $("#save-status").dataset.state = tone; }
  function failure(error, request, recover) {
    const conflict = error.status === 409;
    const title = conflict ? "订单已有更新，请先核对差异" : error.status === 401 || error.status === 403 ? "本地访问凭据已失效" : error.status === 408 ? "操作超时，保存结果尚未确认" : error.status >= 500 ? "本地服务暂时无法完成操作" : error.status >= 400 ? "这项内容需要调整" : "连接中断，保存结果尚未确认";
    $("#operation-error").hidden = false;
    $("#operation-error-title").textContent = title;
    $("#operation-error-message").textContent = `${error.message || "请检查本地服务是否运行。"} 输入已保留，先读取最新状态再继续。`;
    $("#operation-error-detail").textContent = [error.code, error.requestId].filter(Boolean).join(" · ") || "没有服务器请求号";
    const actions = $("#operation-error-actions"); actions.replaceChildren();
    actions.append(button("读取最新状态", () => recover(request)));
    if ([401, 403].includes(error.status)) actions.append(button("重新加载凭据", () => callbacks.refreshCredentials(request)));
    if (error.status >= 400 && error.status < 500 && error.status !== 408) actions.append(button("收起提示", () => { $("#operation-error").hidden = true; }));
    saved(conflict ? "有版本冲突，尚未保存" : "尚未确认保存成功", "error");
  }
  function recovered(request, data, retry) {
    const box = $("#operation-error-diff"); box.replaceChildren();
    const values = request?.body?.values || request?.body?.patch || {};
    for (const [path, proposed] of Object.entries(values)) {
      const field = data.review?.fields?.find(field => field.fieldPath === path && (!request.body.itemId || field.itemId === request.body.itemId));
      box.append(el("p", `${field?.label || path}：当前 ${String(field?.value ?? "未填写")}；本次输入 ${String(proposed || "清空")}`));
    }
    for (const path of request?.body?.fields || []) {
      const field = data.review?.fields?.find(field => field.fieldPath === path && (!request.body.itemId || field.itemId === request.body.itemId));
      const before = request.fieldBefore?.find(field => field.fieldPath === path);
      const action = {clear: "清空", confirm: "确认候选", reject: "拒绝候选"}[request.body.action] || request.body.action;
      box.append(el("p", `${field?.label || before?.label || path}：操作前 ${String(before?.value ?? "未填写")}；当前 ${String(field?.value ?? field?.candidateValue ?? "未填写")}；本次操作 ${action}`));
    }
    $("#operation-error-message").textContent = "已读取服务器最新状态。核对已保存内容后，决定是否重新应用本次输入。";
    const actions = $("#operation-error-actions"); actions.replaceChildren();
    // Only a rejected CAS write is known not to have committed. Unknown outcomes never auto-replay.
    const candidateAction = ["confirm", "reject"].includes(request?.body?.action);
    if (candidateAction) $("#operation-error-message").textContent = "已读取服务器最新状态。确认或拒绝必须重新核对当前候选值，请回到对应字段操作。";
    if (request?.conflict && !candidateAction && ["/api/fields", "/api/chat"].includes(request.path)) actions.append(button("核对后重新应用", retry));
    actions.append(button("保留当前服务器内容", () => {$("#operation-error").hidden = true;}));
  }
  function clearFailure() { $("#operation-error").hidden = true; $("#operation-error-diff").replaceChildren(); }
  root.PrintOpsJourney = {init, render, view, locate, saved, failure, recovered, clearFailure, renderDocument, fieldSaved, el, button};
})(window);
