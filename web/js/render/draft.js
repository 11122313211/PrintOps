/* Order draft fields, inline editing with focus preservation, multi-product items, and spec forms. */

import {
  defaultDraftFields, dimensionLabels, labels, overviewFields, productDraftFields,
} from "../constants.js";
import { DESKTOP_QUERY, actions, appState, cloneData, scrollBehavior } from "../store.js";
import {
  $, captureFieldFocus, hasValue, restoreFieldFocus, revealPanel, showToast,
} from "../dom.js";
import { api, apiErrorText } from "../api.js";
import { clearPdfPreview } from "../pdf.js";
import { applyLayout, saveLayout, updateOrderToggle } from "../layout.js";
import { renderUndoBanner } from "../harness.js";

export function trackOrderChanges(order) {
  if (appState.suppressOrderDiff) {
    appState.previousOrder = cloneData(order || {});
    appState.updatedOrderKeys = new Set();
    appState.updatedSpecKeys = new Set();
    appState.suppressOrderDiff = false;
    return;
  }
  const previousSpecs = appState.previousOrder.productSpecs || {};
  const currentSpecs = order.productSpecs || {};
  appState.updatedOrderKeys = new Set(Object.keys(order).filter((key) => key !== "productSpecs" && JSON.stringify(appState.previousOrder[key]) !== JSON.stringify(order[key])));
  appState.updatedSpecKeys = new Set(Object.keys(currentSpecs).filter((key) => previousSpecs[key] !== currentSpecs[key]));
  appState.previousOrder = cloneData(order || {});
}

export function getFieldValue(key) {
  const items = Array.isArray(appState.state.order?.items) ? appState.state.order.items.filter((item) => item && typeof item === "object") : [];
  if (items.length > 1) {
    if (key === "productType") return items.map((item) => item.productType || "未分类").join(" + ");
    if (key === "quantity") return items.map((item) => `${item.productType || "产品"}${item.quantity ? ` ${item.quantity}` : " 待定数量"}`).join("；");
    if (key === "size") {
      const sizes = [...new Set(items.map((item) => item.size).filter(hasValue))];
      return sizes.length === 1 ? sizes[0] : sizes.length > 1 ? "各产品尺寸不同（见产品项）" : "各产品分别确认";
    }
  }
  return appState.state.order[key] || "";
}

export function dimensionRows(order = {}) {
  const dimensions = order.dimensions && typeof order.dimensions === "object" ? order.dimensions : {};
  return Object.entries(dimensionLabels)
    .map(([key, label]) => [key, label, dimensions[key]])
    .filter(([, , value]) => hasValue(value));
}

export function getDraftFieldSets() {
  const configured = productDraftFields[appState.state.order.productType] || defaultDraftFields;
  const profileKeys = new Set((appState.state.productProfile?.parameters || []).map((item) => item.key));
  return {
    overview: overviewFields,
    specs: configured.specs.filter((key) => !profileKeys.has(key)),
    process: configured.process.filter((key) => !profileKeys.has(key))
  };
}

export function isRequiredField(key) {
  const missing = Array.isArray(appState.state.missingFields) ? appState.state.missingFields : [];
  return missing.includes(labels[key]);
}

export function fieldProvenance(key) {
  return appState.state.fieldMeta?.[key] || null;
}

// A provenance/review entry needs an inline confirm chip when the server
// marked it pending or its confidence is still below the display threshold.
export function needsConfirmation(meta) {
  return meta?.status === "pending_confirmation" || Number(meta?.confidence) < .75;
}

export function confirmFieldButton(field) {
  return PrintOpsJourney.button("确认此值", () => changeField(field, "confirm"), "field-confirm");
}

export function reviewField(path, itemId) {
  return appState.state.review?.fields?.find(field => field.fieldPath === path && (itemId ? field.itemId === itemId : field.itemId == null || appState.state.order?.items?.length <= 1)) || {fieldPath: path, label: labels[path] || path, itemId};
}

export async function changeField(field, action, value) {
  if (appState.isSending || appState.orderWritePending || appState.viewLoading || appState.state.status === "archived" || appState.state.orderStatus === "archived") return;
  try {
    if (action === "set" && /(?:size|bleed|thickness)/i.test(field.fieldPath)) value = PrintOpsWorkbench.manualDimension(value);
  } catch (error) { showToast(error.message, "error"); return; }
  const before = field.value;
  const origin = appState.sessionId;
  const focusId = field.fieldId;
  let focusAfterSave;
  appState.isSending = true; actions.setComposerBusy(true);
  try {
    const body = {sessionId: appState.sessionId, itemId: field.itemId, action};
    if (action === "set") body.values = {[field.fieldPath]: value};
    else body.fields = [field.fieldPath];
    const result = await api("/api/fields", body);
    if (origin !== appState.sessionId) return;
    if (["set", "clear"].includes(action)) PrintOpsJourney.fieldSaved(field, origin);
    actions.render(result, false);
    if (["set", "clear"].includes(action)) {
      appState.undoChange = {field, before, sessionId: appState.sessionId, revision: result.revision};
      renderUndoBanner({
        label: field.label,
        before,
        after: action === "clear" ? "" : value,
        revision: result.revision,
        onUndo: undoLastField,
      });
    }
    const reviewInput = [...document.querySelectorAll("[data-review-input]")].find(input => input.dataset.reviewInput === focusId);
    const inline = [...document.querySelectorAll("[data-field-path]")].find(input => input.dataset.fieldPath === field.fieldPath && (!field.itemId || input.dataset.itemId === field.itemId));
    focusAfterSave = reviewInput?.closest(".journey-pane")?.hidden === false ? reviewInput : inline;
  } catch { /* Recovery card retains the request and field values. */ }
  finally { appState.isSending = false; actions.setComposerBusy(false); if (origin === appState.sessionId) focusAfterSave?.focus({preventScroll: true}); }
}

export async function undoLastField() {
  const change = appState.undoChange;
  if (!change || change.sessionId !== appState.sessionId || change.revision !== appState.state.revision) { showToast("订单已有其他修改，请重新核对字段后编辑。", "error"); return; }
  appState.undoChange = null;
  await changeField(change.field, change.before == null || change.before === "" ? "clear" : "set", change.before);
}

export async function focusItem(itemId) {
  if (appState.isSending || appState.orderWritePending) return;
  const epoch = ++appState.viewEpoch;
  appState.viewLoading = true;
  appState.fileFeedback = null; clearPdfPreview();
  try { actions.render(await api("/api/focus", {sessionId: appState.sessionId, itemId, expectedRevision: appState.state.revision}), false); }
  catch { /* Persistent error already displayed. */ }
  finally { if (epoch === appState.viewEpoch) { appState.viewLoading = false; actions.setComposerBusy(false); } }
}

export function renderFieldList(selector, keys, editable = false) {
  const list = $(selector);
  captureFieldFocus(list);
  list.innerHTML = "";
  keys.forEach((key) => {
    const current = getFieldValue(key);
    const perItem = key === "deadline" && (appState.state.order?.items?.length || 0) > 1 && !hasValue(current);
    const missing = !perItem && !hasValue(current) && isRequiredField(key);
    const row = document.createElement("div");
    row.className = `field-row ${missing ? "required-missing" : ""} ${appState.updatedOrderKeys.has(key) ? "updated" : ""}`;
    const label = document.createElement("span");
    label.className = "field-label";
    if (missing) label.append(PrintOpsWorkbench.tooltipIcon("必填，补充后才能生成订单", "field-status-icon", "必填，补充后才能生成订单", "!"));
    label.append(document.createTextNode(labels[key]));
    const meta = fieldProvenance(key);
    const valueWrap = document.createElement("span");
    valueWrap.className = "field-value-wrap";
    let valueNode;
    if (editable && key !== "platform") {
      valueNode = document.createElement("input");
      valueNode.type = "text";
      valueNode.className = `field-input ${hasValue(current) ? "" : "empty"}`;
      valueNode.value = current || "";
      valueNode.placeholder = missing ? "待补充" : "未设置";
      valueNode.setAttribute("aria-label", labels[key]);
      valueNode.dataset.fieldPath = key;
      valueNode.dataset.fieldKey = `order:${key}`;
      valueNode.addEventListener("change", () => commitFieldEdit(key, valueNode));
    } else {
      valueNode = document.createElement("span");
      valueNode.className = `field-value ${hasValue(current) ? "" : "empty"}`;
      valueNode.textContent = current || (perItem ? "按产品分别设置" : missing ? "待补充" : "未设置");
    }
    valueWrap.append(valueNode);
    if (needsConfirmation(meta)) valueWrap.append(confirmFieldButton(reviewField(key)));
    row.append(label, valueWrap);
    list.appendChild(row);
  });
  restoreFieldFocus(list);
}

export function commitFieldEdit(key, input) {
  let value = input.value.trim();
  try { if (key === "size") value = PrintOpsWorkbench.manualDimension(value); }
  catch (error) { showToast(error.message, "error"); return; }
  const current = getFieldValue(key);
  if (value === (current || "")) return;
  if (appState.isSending) {
    input.value = current || "";
    return;
  }
  changeField(reviewField(key), value ? "set" : "clear", value);
}

export function renderDimensionRows(selector, order = appState.state.order) {
  const list = $(selector);
  if (!list) return;
  dimensionRows(order).forEach(([key, label, current]) => {
    if (key === "finishedSize" && current === order.size) return;
    const row = document.createElement("div");
    row.className = "field-row dimension-row";
    const labelNode = document.createElement("span");
    labelNode.className = "field-label";
    labelNode.textContent = label;
    const valueNode = document.createElement("input");
    valueNode.className = "field-input";
    valueNode.value = current;
    valueNode.setAttribute("aria-label", label);
    valueNode.dataset.fieldPath = `dimensions.${key}`;
    valueNode.dataset.fieldKey = `order:dimensions.${key}`;
    valueNode.addEventListener("change", () => changeField(reviewField(`dimensions.${key}`), valueNode.value.trim() ? "set" : "clear", valueNode.value.trim()));
    const meta = fieldProvenance(`dimensions.${key}`);
    const valueWrap = document.createElement("span");
    valueWrap.className = "field-value-wrap";
    valueWrap.append(valueNode);
    if (needsConfirmation(meta)) valueWrap.append(confirmFieldButton(reviewField(`dimensions.${key}`)));
    row.append(labelNode, valueWrap);
    list.appendChild(row);
  });
  // Completes a restore renderFieldList left pending: the focused dimension
  // input only exists again once these rows are re-appended.
  restoreFieldFocus(list);
}

export async function updateMultiItemField(index, key, value, kind = "base") {
  const path = kind === "spec" ? `productSpecs.${key}` : kind === "dimension" ? `dimensions.${key}` : key;
  await changeField(reviewField(path, appState.state.order.items[index]?.itemId), String(value || "").trim() ? "set" : "clear", String(value || "").trim());
}

export function renderMultiProductItems() {
  const panel = $("#multi-product-panel");
  const root = $("#multi-product-list");
  if (!panel || !root) return;
  const items = Array.isArray(appState.state.order?.items) ? appState.state.order.items.filter((item) => item && typeof item === "object") : [];
  panel.hidden = items.length < 2;
  // TD-11: same-order re-renders keep the user's expanded item/editor state
  // and the focused editor input, mirroring journey.js renderReview.
  const sameOrder = root.dataset.orderId === String(appState.sessionId || "");
  const itemOpenStates = new Map(sameOrder ? [...root.querySelectorAll(".multi-product-item")].map((node) => [node.dataset.itemId, node.open]) : []);
  const editorOpenStates = new Map(sameOrder ? [...root.querySelectorAll(".multi-product-item")].map((node) => [node.dataset.itemId, node.querySelector(".multi-product-editor")?.open === true]) : []);
  captureFieldFocus(root);
  root.innerHTML = "";
  root.dataset.orderId = String(appState.sessionId || "");
  if (items.length < 2) return;
  $("#multi-product-count").textContent = `${items.length} 项`;
  const itemFields = [
    ["quantity", "数量"], ["pages", "页数"], ["paper", "材料"],
    ["printing", "颜色"], ["finishing", "表面工艺"], ["binding", "后道/装订"], ["deadline", "交期"], ["uploadedFile", "文件"]
  ];
  const specLabels = {
    cardStock: "名片材质", cardCorners: "圆角", folding: "折页方式", bleed: "出血",
    labelMaterial: "标签面材", labelShape: "标签形状", adhesive: "胶水类型", boxSize: "盒体尺寸",
    boxSizeInner: "内尺寸", boxSizeOuter: "外尺寸", boxStructure: "盒型结构", dieCut: "刀模文件", bagSize: "袋体尺寸", bagMaterial: "袋体材料",
    handle: "提手方式", cupVolume: "容量", cupMaterial: "杯身材料", innerCoating: "内淋膜",
    displayMaterial: "展示介质", install: "安装加工", hangHole: "挂孔", string: "穿绳/配件"
  };
  const validationByIndex = new Map((appState.state.validation?.itemValidations || []).map((item) => [item.index, item]));
  items.forEach((item, index) => {
    const itemValidation = validationByIndex.get(index);
    const details = document.createElement("details");
    details.className = `multi-product-item ${appState.state.activeItemIndex === index ? "active" : ""}`;
    details.dataset.itemId = item.itemId ?? String(index);
    details.open = appState.state.activeItemIndex === index
      || (itemOpenStates.has(details.dataset.itemId) ? itemOpenStates.get(details.dataset.itemId) : index === 0);
    const summary = document.createElement("summary");
    const heading = document.createElement("span");
    heading.className = "multi-product-item-heading";
    const indexLabel = document.createElement("span");
    indexLabel.className = "multi-product-item-index";
    indexLabel.textContent = String(index + 1).padStart(2, "0");
    const productName = document.createElement("strong");
    productName.textContent = item.productType || `产品项 ${index + 1}`;
    heading.append(indexLabel, productName);
    const status = document.createElement("span");
    status.className = `multi-product-status ${itemValidation?.ok ? "ready" : "attention"}`;
    if (appState.state.activeItemIndex === index) status.textContent = "当前处理";
    else if (item.selectedOption) status.textContent = "已选方案";
    else if (itemValidation?.ok) status.textContent = "基础字段已齐";
    else {
      const missing = [...(itemValidation?.missing || []), ...(itemValidation?.productMissing || [])];
      status.textContent = missing.length ? `待补 ${missing[0]}` : "待逐项确认";
    }
    const chevron = document.createElement("span");
    chevron.className = "multi-product-chevron";
    summary.append(heading, status, chevron);
    details.appendChild(summary);

    const body = document.createElement("div");
    body.className = "multi-product-item-body";
    const fields = document.createElement("dl");
    fields.className = "multi-product-fields";
    itemFields.forEach(([key, label]) => {
      if (!hasValue(item[key])) return;
      const row = document.createElement("div");
      const term = document.createElement("dt");
      const value = document.createElement("dd");
      term.textContent = label;
      value.textContent = item[key];
      row.append(term, value);
      fields.appendChild(row);
    });
    dimensionRows(item).forEach(([key, label, dimensionValue]) => {
      if (key === "finishedSize" && dimensionValue === item.size) return;
      const row = document.createElement("div");
      const term = document.createElement("dt");
      const value = document.createElement("dd");
      term.textContent = label;
      value.textContent = dimensionValue;
      row.append(term, value);
      fields.appendChild(row);
    });
    const specs = item.productSpecs && typeof item.productSpecs === "object" ? item.productSpecs : {};
    Object.entries(specs).slice(0, 4).forEach(([key, value]) => {
      if (!hasValue(value)) return;
      const row = document.createElement("div");
      const term = document.createElement("dt");
      const detail = document.createElement("dd");
      term.textContent = specLabels[key] || key;
      detail.textContent = value;
      row.append(term, detail);
      fields.appendChild(row);
    });
    if (!fields.children.length) {
      const empty = document.createElement("p");
      empty.className = "multi-product-empty";
      empty.textContent = "尚未识别到本项参数，请在对话中补充。";
      body.appendChild(empty);
    } else {
      body.appendChild(fields);
    }
    if (itemValidation) {
      const readiness = document.createElement("p");
      readiness.className = `multi-product-readiness ${itemValidation.ok ? "ready" : "attention"}`;
      const missing = [...(itemValidation.missing || []), ...(itemValidation.productMissing || [])];
      readiness.textContent = missing.length
        ? `本项信息度 ${itemValidation.readiness || 0}% · 还需确认 ${missing.join("、")}`
        : `本项信息度 ${itemValidation.readiness || 0}% · 基础字段已齐`;
      body.appendChild(readiness);
    }
    const editor = document.createElement("details");
    editor.className = "multi-product-editor";
    editor.open = editorOpenStates.get(details.dataset.itemId) === true;
    const editorSummary = document.createElement("summary");
    editorSummary.textContent = "直接编辑本项";
    editor.appendChild(editorSummary);
    const editorGrid = document.createElement("div");
    editorGrid.className = "multi-product-editor-grid";
    const baseFields = [
      ["quantity", "数量"], ["size", "成品尺寸"], ["pages", "页数"], ["paper", "纸张/材料"],
      ["printing", "印刷颜色"], ["finishing", "表面工艺"], ["binding", "后道/装订"], ["deadline", "交期"]
    ];
    const dimensionFields = Object.entries(dimensionLabels).filter(([key]) => key !== "finishedSize").map(([key, label]) => [key, label, "dimension", false]);
    const productParameters = (itemValidation?.parameters || []).map((parameter) => [parameter.key, parameter.label || parameter.key, true, parameter.required]);
    [...baseFields.map(([key, label]) => [key, label, "base", false]), ...dimensionFields,
      ...productParameters.map(([key, label, isSpec, required]) => [key, label, isSpec ? "spec" : "base", required])]
      .forEach(([key, label, kind, required]) => {
      const field = document.createElement("label");
      const currentValue = kind === "spec" ? specs[key] : kind === "dimension" ? (item.dimensions?.[key] || "") : item[key];
      field.className = `multi-product-editor-field ${required && !hasValue(currentValue) ? "missing" : ""}`;
      const name = document.createElement("span");
      name.textContent = required ? `${label} *` : label;
      const input = document.createElement("input");
      input.type = "text";
      input.value = currentValue || "";
      input.dataset.fieldPath = kind === "spec" ? `productSpecs.${key}` : kind === "dimension" ? `dimensions.${key}` : key;
      input.dataset.itemId = item.itemId;
      input.dataset.fieldKey = `item:${item.itemId ?? index}:${input.dataset.fieldPath}`;
      input.placeholder = required ? `请填写${label}` : label;
      input.addEventListener("change", () => updateMultiItemField(index, key, input.value, kind));
      field.append(name, input);
      const review = reviewField(input.dataset.fieldPath, item.itemId);
      if (review.provenance) field.append(PrintOpsWorkbench.evidence(review.provenance));
      // Unlike the single-product rows, the multi-item editor gates on the
      // server-computed review status alone; low-confidence provenance is
      // surfaced by the review pane instead of a chip here.
      if (review.status === "pending_confirmation") field.append(confirmFieldButton(review));
      editorGrid.appendChild(field);
    });
    editor.appendChild(editorGrid);
    body.appendChild(editor);
    const focus = document.createElement("button");
    focus.type = "button";
    focus.className = "multi-product-focus";
    focus.textContent = "处理这一项";
    focus.addEventListener("click", (event) => {
      event.preventDefault();
      if (appState.state.activeItemIndex !== index) {
        appState.fileFeedback = null;
        clearPdfPreview();
      }
      focusItem(item.itemId);
    });
    body.appendChild(focus);
    details.appendChild(body);
    root.appendChild(details);
  });
  restoreFieldFocus(root);
}

export function syncDraftGroups(hasProduct, hasProcessData) {
  const recommendations = (appState.state.options || []).length > 0;
  if (appState.state.order.productType !== appState.renderedProduct) {
    appState.renderedProduct = appState.state.order.productType || "";
    $("[data-draft-group='overview']").open = true;
    $("[data-draft-group='specs']").open = hasProduct;
    $("[data-draft-group='process']").open = hasProcessData;
  } else if (recommendations && !appState.hadRecommendations) {
    $("[data-draft-group='process']").open = true;
  }
  appState.hadRecommendations = recommendations;
  updateDraftToggle();
}

export function renderDraft() {
  const profile = appState.state.productProfile || { parameters: [], missing: [] };
  const parameters = profile.parameters || [];
  const fields = getDraftFieldSets();
  const multiItems = Array.isArray(appState.state.order?.items) ? appState.state.order.items.filter((item) => item && typeof item === "object") : [];
  const isMultiProduct = multiItems.length > 1 || (appState.state.validation?.multiProduct || []).length > 0;
  renderFieldList("#overview-fields", fields.overview, !isMultiProduct);
  renderFieldList("#spec-fields", fields.specs, !isMultiProduct);
  if (!isMultiProduct) renderDimensionRows("#spec-fields");
  renderFieldList("#process-fields", fields.process, !isMultiProduct);
  if (isMultiProduct) {
    $("#product-context").hidden = true;
    $("#parameter-form").innerHTML = "";
  } else {
    renderParameterForm(profile);
  }
  renderMultiProductItems();

  const overviewFilled = fields.overview.filter((key) => hasValue(getFieldValue(key))).length;
  const specFilled = fields.specs.filter((key) => hasValue(getFieldValue(key))).length + parameters.filter((item) => item.filled).length;
  const specTotal = fields.specs.length + parameters.length;
  const processFilled = fields.process.filter((key) => hasValue(getFieldValue(key))).length;
  $("#overview-meta").textContent = `${overviewFilled} / ${fields.overview.length} 已填写`;
  $("#specs-meta").textContent = isMultiProduct ? `多产品 · ${multiItems.length || (appState.state.validation?.multiProduct || []).length} 项` : appState.state.order.productType ? `${profile.category || "其他印刷品"} · ${specFilled} / ${specTotal}` : "待识别品类";
  $("#process-meta").textContent = `${processFilled} / ${fields.process.length} 已填写`;

  const complete = Array.isArray(appState.state.missingFields)
    ? appState.state.missingFields.length === 0
    : ["productType", "quantity", "size", "paper", "printing", "deadline"].every((key) => appState.state.order[key]);
  const productMissing = profile.missing || [];
  const itemValidations = Array.isArray(appState.state.validation?.itemValidations) ? appState.state.validation.itemValidations : [];
  const multiItemsReady = isMultiProduct && multiItems.length > 1 && itemValidations.length === multiItems.length
    && itemValidations.every((item) => item.ok) && multiItems.every((item) => item.selectedOption);
  const readyToGenerate = isMultiProduct ? multiItemsReady : complete && productMissing.length === 0 && appState.state.selectedOption;
  const confirmed = appState.state.confirmation?.status === "confirmed";
  const status = $("#draft-status");
  status.textContent = isMultiProduct ? (appState.state.orderGenerated ? (confirmed ? "已确认" : "待人工确认") : multiItemsReady ? "可生成" : "逐项确认") : appState.state.orderGenerated ? (confirmed ? "已确认" : "待人工确认") : !complete ? "待补充" : productMissing.length ? "待确认规格" : appState.state.selectedOption ? "可生成" : "可选方案";
  status.className = `draft-status ${isMultiProduct && !multiItemsReady || !isMultiProduct && productMissing.length || appState.state.orderGenerated && !confirmed ? "attention" : confirmed ? "confirmed" : complete ? "ready" : ""}`;
  $("#generate-order").disabled = !readyToGenerate || appState.state.orderGenerated;
  $("#generate-order").textContent = isMultiProduct ? (appState.state.orderGenerated ? "整体交接单已生成" : multiItemsReady ? "生成整体交接单" : "逐项确认后生成") : appState.state.orderGenerated ? "订单草稿已生成" : !complete ? "补全信息后生成" : productMissing.length ? "补全规格后生成" : !appState.state.selectedOption ? "选择方案后生成" : "生成订单草稿";
  $("#copy-order").disabled = !appState.state.orderGenerated;
  const confirmButton = $("#confirm-order");
  if (confirmButton) {
    confirmButton.hidden = !appState.state.orderGenerated;
    confirmButton.disabled = confirmed;
    confirmButton.textContent = confirmed ? "已确认交接单" : "确认交接单";
  }
  $("#order-export").hidden = !appState.state.handoffDocument?.exportable;
  $("#export-label").textContent = confirmed ? "导出已确认交接单" : "导出未确认草稿";
  syncDraftGroups(Boolean(appState.state.order.productType), fields.process.some((key) => hasValue(getFieldValue(key))) || Boolean(appState.state.selectedOption));
}

export function renderParameterForm(profile) {
  const context = $("#product-context");
  const contextWasHidden = context.hidden;
  context.hidden = !appState.state.order?.productType;
  if (contextWasHidden && !context.hidden) revealPanel(context);
  $("#product-profile-summary").textContent = profile.summary || "先按通用字段收集订单信息。";
  $("#product-profile-readiness").textContent = profile.missing?.length ? `还有 ${profile.missing.length} 项待补充` : "品类参数已填写";
  const root = $("#parameter-form");
  captureFieldFocus(root);
  root.innerHTML = "";
  if (!appState.state.order?.productType) return;
  const title = document.createElement("p");
  title.className = "parameter-form-title";
  title.textContent = `${profile.category || "产品"}参数`;
  root.appendChild(title);
  (profile.parameters || []).forEach((item) => {
    const field = document.createElement("div");
    field.className = `parameter-field ${!item.filled && item.required ? "missing" : ""}`;
    const label = document.createElement("label");
    label.htmlFor = `parameter-${item.key}`;
    const hintText = item.hint || (item.required ? "必填参数" : "可选参数");
    if (!item.filled && item.required) label.append(PrintOpsWorkbench.tooltipIcon("必填，补充后才能生成订单", "field-status-icon", "必填，补充后才能生成订单", "!"));
    label.append(document.createTextNode(item.label));
    if (item.required) {
      const requiredMark = document.createElement("span");
      requiredMark.className = "required-mark";
      requiredMark.textContent = "*";
      label.append(document.createTextNode(" "), requiredMark);
    }
    if (item.hint) label.append(PrintOpsWorkbench.tooltipIcon(hintText, "parameter-hint-trigger", `${item.label}说明`));
    const input = document.createElement("input");
    input.id = `parameter-${item.key}`;
    input.value = item.value || "";
    input.placeholder = item.question || item.label;
    input.setAttribute("aria-describedby", `parameter-hint-${item.key}`);
    if (!item.filled && item.required) input.setAttribute("aria-invalid", "true");
    input.dataset.fieldPath = `productSpecs.${item.key}`;
    input.dataset.fieldKey = `order:productSpecs.${item.key}`;
    input.addEventListener("change", () => changeField(reviewField(input.dataset.fieldPath), input.value.trim() ? "set" : "clear", input.value.trim()));
    input.addEventListener("input", () => {
      if (!input.value.trim()) return;
      field.classList.remove("missing");
      input.removeAttribute("aria-invalid");
    });
    const hint = document.createElement("small");
    hint.className = "parameter-hint sr-only";
    hint.id = `parameter-hint-${item.key}`;
    const provenance = fieldProvenance(`productSpecs.${item.key}`);
    hint.textContent = hintText;
    field.append(label, input, hint, PrintOpsWorkbench.evidence(provenance));
    if (needsConfirmation(provenance)) field.append(confirmFieldButton(reviewField(input.dataset.fieldPath)));
    root.appendChild(field);
  });
  restoreFieldFocus(root);
}

export function updateDraftToggle() {
  const groups = [...document.querySelectorAll(".draft-group")];
  const allOpen = groups.every((group) => group.open);
  const button = $("#toggle-draft-groups");
  button.dataset.allOpen = String(allOpen);
  button.setAttribute("aria-label", allOpen ? "收起全部订单分组" : "展开全部订单分组");
  button.title = allOpen ? "收起全部订单分组" : "展开全部订单分组";
}

/**
 * Expand, scroll to, pulse-highlight, and focus an inline draft input by fieldPath.
 * Returns true if an inline input was located and focused; false lets callers fall back to #phase-review.
 */
export function focusDraftField(fieldPath) {
  if (!fieldPath) return false;
  const orderPanel = $("#order-panel");
  if (!orderPanel) return false;
  if (DESKTOP_QUERY.matches && appState.layout.orderCollapsed) {
    appState.layout.orderCollapsed = false;
    applyLayout();
    saveLayout();
  } else if (!DESKTOP_QUERY.matches && $("#workspace").dataset.mobilePane !== "order") {
    $("#workspace").dataset.mobilePane = "order";
    updateOrderToggle();
  }
  const input = orderPanel.querySelector(`[data-field-path="${CSS.escape(fieldPath)}"]`);
  if (!input || input.disabled) return false;
  const group = input.closest(".draft-group");
  if (group && !group.open) {
    group.open = true;
    updateDraftToggle();
  }
  const row = input.closest(".field-row, .parameter-field") || input;
  row.scrollIntoView({ block: "center", behavior: scrollBehavior() });
  row.classList.remove("is-flash-highlight");
  void row.offsetWidth;
  row.classList.add("is-flash-highlight");
  row.addEventListener("animationend", () => row.classList.remove("is-flash-highlight"), { once: true });
  input.focus({ preventScroll: true });
  if (typeof input.select === "function") input.select();
  return true;
}

export function getOrderGroups() {
  const multiItems = Array.isArray(appState.state.order?.items) ? appState.state.order.items.filter((item) => item && typeof item === "object") : [];
  if (multiItems.length > 1) {
    const itemGroups = [];
    multiItems.forEach((item, index) => {
      const itemLabel = `产品项 ${index + 1}${item.productType ? ` · ${item.productType}` : ""}`;
      const overview = [["数量", item.quantity], ["使用场景", item.purpose], ["交期", item.deadline], ["预算偏好", item.budget]];
      const specs = [["成品尺寸", item.size], ...dimensionRows(item).filter(([key, value, dimensionValue]) => !(key === "finishedSize" && dimensionValue === item.size)).map(([, label, value]) => [label, value]), ["页数", item.pages], ["版式方向", item.orientation]];
      const process = [["纸张/材料", item.paper], ["印刷颜色", item.printing], ["表面工艺", item.finishing], ["后道/装订", item.binding]];
      const productSpecs = Object.entries(item.productSpecs || {}).map(([key, value]) => [key, value]);
      itemGroups.push([`${itemLabel} · 订单概览`, overview.filter(([, value]) => hasValue(value))]);
      itemGroups.push([`${itemLabel} · 产品规格`, [...specs, ...productSpecs].filter(([, value]) => hasValue(value))]);
      itemGroups.push([`${itemLabel} · 生产工艺`, process.filter(([, value]) => hasValue(value))]);
    });
    return itemGroups;
  }
  const fields = getDraftFieldSets();
  return [
    ["订单概览", fields.overview.map((key) => [labels[key], getFieldValue(key)]).filter(([, value]) => hasValue(value))],
    ["产品规格", [
      ...fields.specs.map((key) => [labels[key], getFieldValue(key)]).filter(([, value]) => hasValue(value)),
      ...dimensionRows(appState.state.order).filter(([key, , value]) => !(key === "finishedSize" && value === appState.state.order.size)).map(([, label, value]) => [label, value]),
      ...(appState.state.productProfile?.parameters || []).map((item) => [item.label, item.value || (item.required ? "待确认" : "")]).filter(([, value]) => hasValue(value))
    ]],
    ["生产工艺", fields.process.map((key) => [labels[key], getFieldValue(key)]).filter(([, value]) => hasValue(value))]
  ];
}
