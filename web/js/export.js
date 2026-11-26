/* Authoritative handoff preview, JSON/CSV/Markdown export, workcard PNG, and A4 print. */

import {
  EXPORT_CHECK_STATUS_LABELS, EXPORT_DOCUMENT_STATUS_LABELS,
  EXPORT_FIELD_STATUS_LABELS, EXPORT_FILE_STATUS_LABELS,
} from "./constants.js";
import { appState } from "./store.js";
import { showToast } from "./dom.js";
import { api, apiErrorText } from "./api.js";

export async function currentHandoff() {
  const id = appState.sessionId, revision = appState.state.revision;
  const document = await api(`/api/orders/${encodeURIComponent(id)}/handoff?revision=${revision}`, undefined, "GET");
  if (id !== appState.sessionId || revision !== appState.state.revision) throw new Error("订单已变化，请重新核对并导出当前版本。");
  if (!document.exportable) throw new Error("交接单尚未就绪，请处理待确认字段并重新生成。");
  PrintOpsJourney.renderDocument(document);
  return document;
}

// Pure serializer for the handoff CSV audit export. Wording is pinned by the
// journey_edges "csv audit" case (headers, status labels, formula escaping):
// move this code, never reword it.
export function handoffCsvDocument(document) {
  const escape = (value) => {
    let text = String(value ?? "");
    if (/^[\s\uFEFF]*[=+@-]|^[\t\r\n]/.test(text)) text = "'" + text;
    return '"' + text.replaceAll('"', '""') + '"';
  };
  const rows = [];
  const add = (group, item, path, label, value, status = "", fieldId = "") => rows.push([
    group, item?.itemId || "", item ? (item.itemIndex == null ? "整单" : item.itemIndex + 1) : "", item?.title || "",
    path, fieldId, label, value == null ? "未记录" : typeof value === "boolean" ? (value ? "是" : "否") : value, status
  ]);
  for (const [path, label, value] of [
    ["schemaVersion", "交接文档结构版本", document.schemaVersion], ["documentType", "文档类型", document.documentType],
    ["orderId", "订单", document.orderId], ["revision", "订单版本", document.revision],
    ["status", "交接状态", EXPORT_DOCUMENT_STATUS_LABELS[document.status] || "待核对"], ["exportable", "允许导出", document.exportable],
    ["unitSystem", "尺寸单位", document.unitSystem], ["generatedAt", "生成时间", document.generatedAt],
    ["confirmedAt", "确认时间", document.confirmedAt ?? "尚未确认"], ["confirmedRevision", "确认版本", document.confirmedRevision ?? "无"],
    ["confirmation.status", "人工确认状态", EXPORT_DOCUMENT_STATUS_LABELS[document.confirmation?.status] || "待核对"],
    ["confirmation.scope", "确认范围", document.confirmation?.scope], ["sent", "供应商发送", document.sent ? "已发送" : "尚未发送"]
  ]) add("交接信息", null, path, label, value);
  for (const item of document.items || []) {
    for (const [path, label] of [["itemId", "产品标识"], ["itemIndex", "产品索引（从0开始）"], ["title", "产品名称"]]) add("产品信息", item, path, label, item[path]);
    for (const field of item.fields || []) {
      add("生产规格", item, field.key, field.label, field.value ?? "待补充", EXPORT_FIELD_STATUS_LABELS[field.status] || "待核对", field.fieldId);
      if (field.aliases) add("字段依据", item, `${field.key}.aliases`, `${field.label}同义字段`, JSON.stringify(field.aliases), "", field.fieldId);
      if (field.semanticConflict != null) add("字段依据", item, `${field.key}.semanticConflict`, `${field.label}同义字段冲突`, field.semanticConflict, "", field.fieldId);
    }
    add("工艺方案", item, "selectedOption", "已选方案标识", item.selectedOption ?? "未选择");
    if (item.selectedOptionDetails) {
      for (const [key, label] of [["id", "方案标识"], ["title", "方案名称"], ["printMode", "印刷方式"], ["composition", "方案组成"]]) {
        add("工艺方案", item, `selectedOptionDetails.${key}`, label, item.selectedOptionDetails[key]);
      }
    } else add("工艺方案", item, "selectedOptionDetails", "工艺方案详情", "未选择");
    const file = item.file || {};
    for (const [key, label] of [["fileName", "文件名"], ["itemId", "文件所属产品"], ["checkedAt", "文件检查时间"], ["version", "文件检查记录版本"], ["scope", "文件检查范围"], ["limitations", "文件检查限制"]]) {
      add("印刷文件", item, `file.${key}`, label, file[key]);
    }
    add("印刷文件", item, "file.status", "文件检查状态", EXPORT_FILE_STATUS_LABELS[file.status] || "待核对");
    add("印刷文件", item, "file.inspectionLevel", "文件检查级别", {basic: "文件基本属性检查", metadata: "文件基本属性与已提供的 PDF 元数据检查"}[file.inspectionLevel] || "未执行基础检查");
    (file.checks || []).forEach((check, index) => add("文件基础检查", item, `file.checks.${index}`, check.label, check.detail, EXPORT_CHECK_STATUS_LABELS[check.status] || "未确认"));
    (file.warnings || []).forEach((warning, index) => add("文件复核提醒", item, `file.warnings.${index}`, "提醒", warning));
  }
  const issueLabels = {id: "事项标识", kind: "事项类型", field: "关联字段", fieldId: "字段标识", fieldPath: "字段路径", label: "事项名称", itemId: "产品标识", itemIndex: "产品索引（从0开始）", blocking: "是否阻断", section: "处理阶段", message: "事项说明", action: "处理动作"};
  (document.issues || []).forEach((issue, index) => {
    const item = document.items?.find(item => item.itemId === issue.itemId);
    for (const [key, value] of Object.entries(issue)) add("交接待办", item, `issues.${index}.${key}`, issueLabels[key] || key, value);
  });
  (document.notes || []).forEach((note, index) => add("交接时说明", null, `notes.${index}`, "说明", note));
  return [["分组", "产品标识", "产品序号", "产品项", "字段路径", "字段标识", "字段", "内容", "状态"], ...rows].map(row => row.map(escape).join(",")).join("\n");
}

export async function exportOrder(format) {
  try {
    const document = await currentHandoff();
    // 小卡走预览对话框（视觉产物需先确认），其余格式直接下载。
    if (format === "card") {
      if (typeof PrintOpsWorkCard === "undefined") throw new Error("工单小卡组件未加载，请刷新页面后重试。");
      PrintOpsWorkCard.open(document, {notify: showToast});
      return;
    }
    if (format === "print") { PrintOpsJourney.view("review"); window.print(); return; }
    let content, extension, contentType;
    if (format === "json") {
      content = JSON.stringify(document, null, 2); extension = "json"; contentType = "application/json";
    } else if (format === "csv") {
      content = handoffCsvDocument(document);
      extension = "csv"; contentType = "text/csv";
    } else { content = document.markdown; extension = "md"; contentType = "text/markdown"; }
    const url = URL.createObjectURL(new Blob([(format === "csv" ? "\ufeff" : "") + content], {type: contentType + ";charset=utf-8"}));
    const link = window.document.createElement("a");
    link.href = url;
    link.download = `printops-handoff-${document.orderId}-r${document.revision}.${extension}`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast(`已生成 ${extension.toUpperCase()} 下载文件，请打开核对；尚未发送供应商。`);
  } catch (error) { showToast(error.message || "交接单导出失败", "error"); }
}
