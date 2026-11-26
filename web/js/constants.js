/* Domain labels, status vocabularies, layout defaults, and DOM binding IDs. */

export const labels = {
  productType: "印刷品", purpose: "使用场景", quantity: "数量", size: "成品尺寸",
  pages: "页数", orientation: "版式方向", paper: "纸张/材料", printing: "印刷颜色", finishing: "表面工艺", binding: "装订/后道",
  deadline: "交期", budget: "预算偏好"
};
export const dimensionLabels = {
  finishedSize: "成品尺寸", expandedSize: "展开尺寸", dieCutSize: "刀模尺寸", packageSize: "包装三维尺寸",
  packageInnerSize: "包装内尺寸", packageOuterSize: "包装外尺寸"
};
// Status label maps, hoisted beside the field labels so every renderer reads
// one source per status domain. The export variants deliberately use audit
// phrasing (已核对/候选待核对) while the review pane in journey.js uses user
// phrasing (已确认/待确认); both wordings are test-pinned — keep the domains
// separate, do not merge them.
export const ORDER_STATUS_LABELS = {draft: "草稿", collecting: "编辑中", ready_for_review: "待审核", confirmed: "已确认", handoff_ready: "交接就绪", quote_draft: "询价草稿", quote_received: "已收报价", cancelled: "已取消", archived: "已归档"};
export const QUOTE_STATUS_LABELS = {awaiting_human_confirmation: "待人工确认", confirmed: "已确认，待提交", cancelled: "已取消", stale: "已失效", submitted: "已提交", failed: "提交失败"};
export const ESTIMATE_STATUS_LABELS = {ready: "示例估算", partial: "部分费用可算", blocked: "待补充计价依据"};
export const EXPORT_DOCUMENT_STATUS_LABELS = {blocked: "需求整理中，禁止作为生产交接单导出", stale: "已失效，请重新生成", draft: "未确认草稿", confirmed: "已本地确认", unconfirmed: "未确认"};
export const EXPORT_FIELD_STATUS_LABELS = {missing: "待补充", pending_confirmation: "候选待核对", confirmed: "已核对", filled: "已填写", observed: "已填写", inferred: "推断待核对", stale: "已失效"};
export const EXPORT_FILE_STATUS_LABELS = {pending_delivery: "文件待交付", stale: "检查已过期", checked: "已完成基础检查", needs_review: "需核对"};
export const EXPORT_CHECK_STATUS_LABELS = {ok: "通过", warn: "需关注", error: "未通过", info: "信息", unknown: "未确认"};
export const overviewFields = ["productType", "quantity", "purpose", "deadline", "budget"];
export const defaultDraftFields = { specs: ["size", "pages", "orientation"], process: ["paper", "printing", "finishing", "binding"] };
export const productDraftFields = {
  "宣传册": { specs: ["size", "orientation"], process: ["paper", "printing", "finishing"] },
  "画册": { specs: ["size", "orientation"], process: ["paper", "printing", "finishing"] },
  "单页": { specs: ["size", "orientation"], process: ["paper", "printing", "finishing"] },
  "折页": { specs: ["size", "orientation"], process: ["paper", "printing", "finishing", "binding"] },
  "名片": { specs: ["size", "orientation"], process: ["paper", "printing", "finishing"] },
  "PVC卡": { specs: ["size"], process: ["printing", "finishing", "binding"] },
  "吊牌": { specs: ["size"], process: ["paper", "printing", "finishing", "binding"] },
  "联单": { specs: ["size", "orientation"], process: ["paper", "printing", "finishing"] },
  "信封封套": { specs: [], process: ["paper", "printing", "finishing", "binding"] },
  "标签": { specs: ["size"], process: ["printing", "finishing", "binding"] },
  "包装盒": { specs: [], process: ["paper", "printing", "finishing", "binding"] },
  "手提袋": { specs: [], process: ["printing", "finishing", "binding"] },
  "纸杯": { specs: [], process: ["printing", "finishing", "binding"] },
  "海报": { specs: ["size"], process: ["printing", "finishing", "binding"] },
  "喷画": { specs: ["size"], process: ["printing", "finishing", "binding"] },
  "PVC": { specs: ["size"], process: ["printing", "finishing", "binding"] }
};
export const toolLabels = {
  validate_order: ["校验订单", "检查缺失字段与风险"],
  recommend_processes: ["推荐工艺", "比较效果、成本、交期"],
  estimate_price: ["估算费用", "生成非正式价格区间"],
  prepare_handoff: ["生成交接单", "按目标平台整理字段"],
  request_supplier_quote: ["准备询价", "生成待人工确认的询价请求"],
  match_supplier_capability: ["匹配供应商", "检查平台能力与待确认项"],
  explain_print_term: ["解释印刷", "理解纸张、出血与装订"]
};
export const categoryExamples = {
  "名片/卡片": "做一批名片",
  "单张": "做一批单页",
  "标签/不干胶": "做一批不干胶标签",
  "书籍画册": "做一本画册",
  "广告物料": "做一张海报",
  "包装周边": "做一批包装盒",
  "办公用品": "做一批联单",
  "家居日常": "我想做家居印刷品",
  "动漫文创": "我想做动漫文创印刷品",
  "季节产品": "我想做季节产品",
  "现货/样品": "我想先看现货或样品"
};

export const orderHistoryKey = "printops_order_history";
export const specPresetKey = "printops_spec_presets";
export const layoutKey = "printops_layout";
export const LAYOUT_DEFAULTS = { sidebarW: 240, orderW: 348, orderCollapsed: false, composerH: 44 };
export const SIDEBAR_RANGE = [204, 320];
export const ORDER_RANGE = [310, 480];
export const COMPOSER_RANGE = [44, 280];

export const toolCodes = {
  validate_order: "CK", recommend_processes: "RF", estimate_price: "PR",
  prepare_handoff: "HO", request_supplier_quote: "QT",
  match_supplier_capability: "CP", explain_print_term: "KB"
};


export const BINDING_TARGET_IDS = [
  "message-form", "message-input", "preview-toggle", "chat-cancel", "generate-order", "confirm-order",
  "quote-refresh", "quote-cancel", "copy-order", "toggle-draft-groups", "upload-button",
  "drop-zone", "file-input", "platform-select", "sidebar-toggle", "order-toggle",
  "mobile-back", "workspace-backdrop", "history-context-menu", "history-list",
  "theme-toggle", "settings-nav", "history-include-archived", "save-preset",
  "preset-toggle", "preset-list", "reset-order", "confirm-reset-dialog", "settings-test",
  "settings-button", "settings-cancel", "settings-dialog", "settings-cancel-secondary",
  "settings-form", "settings-clear", "settings-history-list",
  "settings-history-select-all", "settings-history-toolbar", "workcard-dialog",
  "cmd-palette-trigger", "trace-jump", "slash-trigger", "slash-menu",
  "command-palette", "command-palette-input", "command-palette-close", "command-palette-list"
];
