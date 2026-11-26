/* Keyboard-first Harness command surface: composer `/` slash command popover & global Ctrl/Cmd+K palette. */

import { DESKTOP_QUERY, actions, appState } from "./store.js";
import { $, showToast } from "./dom.js";
import { applyLayout, saveLayout, setHighContrast, updateOrderToggle } from "./layout.js";
import { loadOrderHistory, orderHistoryTitle } from "./history.js";
import { autoGrowComposer, renderComposerPreview } from "./render/chat.js";
import { callTool } from "./render/panels.js";

function isArchived() {
  return appState.state?.status === "archived" || appState.state?.orderStatus === "archived";
}

function isBusy() {
  return Boolean(appState.isSending || appState.orderWritePending || appState.viewLoading);
}

const SLASH_COMMANDS = [
  {
    cmd: "/推荐",
    aliases: ["rec", "recommend", "options", "方案", "工艺"],
    code: "RF",
    title: "推荐工艺方案",
    desc: "按当前参数生成可对比的工艺与材料方案",
    write: true,
    run: () => callTool("recommend_processes"),
  },
  {
    cmd: "/估价",
    aliases: ["price", "est", "estimate", "估算", "费用"],
    code: "PR",
    title: "估算本地参考费用",
    desc: "生成分项本地示例估算（非供应商正式报价）",
    write: true,
    run: () => callTool("estimate_price"),
  },
  {
    cmd: "/校验",
    aliases: ["check", "val", "validate", "检查", "风控"],
    code: "CK",
    title: "校验订单完整性",
    desc: "检查必填缺口、尺寸规范与生产风险",
    write: true,
    run: () => callTool("validate_order"),
  },
  {
    cmd: "/对比",
    aliases: ["compare", "cmp", "比较"],
    code: "V2",
    title: "查看工艺方案对比",
    desc: "切换到方案对比视图（只读，不修改订单）",
    write: false,
    run: () => PrintOpsJourney.view("compare", true),
  },
  {
    cmd: "/交接",
    aliases: ["review", "handoff", "核对", "导出"],
    code: "V3",
    title: "查看核对与交接资料",
    desc: "切换到核对与交接视图（只读，不自动生成）",
    write: false,
    run: () => PrintOpsJourney.view("review", true),
  },
  {
    cmd: "/询价",
    aliases: ["quote", "qt", "报价"],
    code: "QT",
    title: "准备供应商询价草稿",
    desc: "生成待人工确认的本地询价请求",
    write: true,
    run: () => callTool("request_supplier_quote"),
  },
  {
    cmd: "/匹配",
    aliases: ["supplier", "cap", "供应商", "平台"],
    code: "CP",
    title: "检查目标平台能力",
    desc: "核对当前平台支持范围与待确认项",
    write: true,
    run: () => callTool("match_supplier_capability"),
  },
  {
    cmd: "/解释",
    aliases: ["explain", "kb", "术语", "纸张", "出血"],
    code: "KB",
    title: "解释印刷专业术语",
    desc: "可附带问题，例如：/解释 出血与模切",
    write: true,
    acceptsArg: true,
    run: (arg) => callTool("explain_print_term", arg ? { question: arg } : {}),
  },
  {
    cmd: "/新建",
    aliases: ["new", "reset", "新订单"],
    code: "NW",
    title: "新建一份订单",
    desc: "打开新建确认框，当前订单保留在历史中",
    write: false,
    run: () => $("#reset-order")?.click(),
  },
];

let slashMatches = [];
let slashActiveIndex = 0;
let paletteItems = [];
let paletteActiveIndex = 0;
let paletteReturnFocus = null;

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function parseSlashInput(rawValue) {
  const value = String(rawValue || "");
  if (!value.startsWith("/") || value.includes("\n")) return null;
  const trimmed = value.slice(1);
  const spaceIndex = trimmed.search(/\s/);
  if (spaceIndex === -1) return { query: trimmed.trim().toLowerCase(), arg: "" };
  return {
    query: trimmed.slice(0, spaceIndex).trim().toLowerCase(),
    arg: trimmed.slice(spaceIndex + 1).trim(),
  };
}

function filterSlashCommands(query) {
  if (!query) return SLASH_COMMANDS;
  return SLASH_COMMANDS.filter((item) => {
    const haystack = [item.cmd.slice(1), item.title, item.desc, item.code, ...(item.aliases || [])]
      .join(" ")
      .toLowerCase();
    return haystack.includes(query);
  });
}

export function closeSlashMenu() {
  const menu = $("#slash-menu");
  const trigger = $("#slash-trigger");
  if (menu) {
    menu.hidden = true;
    menu.replaceChildren();
  }
  if (trigger) trigger.setAttribute("aria-expanded", "false");
  slashMatches = [];
  slashActiveIndex = 0;
}

function renderSlashMenu(matches, arg = "") {
  const menu = $("#slash-menu");
  const trigger = $("#slash-trigger");
  if (!menu) return;
  slashMatches = matches;
  if (!matches.length) {
    closeSlashMenu();
    return;
  }
  if (slashActiveIndex >= matches.length) slashActiveIndex = 0;
  const archived = isArchived();
  menu.replaceChildren();

  const header = el("div", null, "slash-menu-head");
  header.append(el("span", "工具与视图命令"), el("span", "↑↓ 选择 · Enter 执行 · Esc 关闭", "slash-menu-hint"));
  menu.append(header);

  matches.forEach((item, index) => {
    const disabled = item.write && (archived || isBusy());
    const btn = el("button", null, `slash-menu-item${index === slashActiveIndex ? " is-active" : ""}`);
    btn.type = "button";
    btn.role = "option";
    btn.setAttribute("aria-selected", String(index === slashActiveIndex));
    btn.dataset.slashIndex = String(index);
    btn.disabled = disabled;

    const code = el("span", item.code, "slash-menu-code");
    const copy = el("span", null, "slash-menu-copy");
    const nameRow = el("strong", `${item.cmd}  ${item.title}`);
    const descText = disabled
      ? (archived ? "当前订单已归档（只读）" : "正在处理中，请稍候")
      : (item.acceptsArg && arg ? `将询问：“${arg}”` : item.desc);
    copy.append(nameRow, el("small", descText));

    btn.append(code, copy);
    btn.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      if (!disabled) executeSlashCommand(item, arg);
    });
    menu.append(btn);
  });

  menu.hidden = false;
  if (trigger) trigger.setAttribute("aria-expanded", "true");
}

function updateSlashActiveVisual() {
  const menu = $("#slash-menu");
  if (!menu || menu.hidden) return;
  menu.querySelectorAll(".slash-menu-item").forEach((node, index) => {
    const active = index === slashActiveIndex;
    node.classList.toggle("is-active", active);
    node.setAttribute("aria-selected", String(active));
    if (active) node.scrollIntoView({ block: "nearest" });
  });
}

function executeSlashCommand(item, arg = "") {
  if (!item) return false;
  if (item.write && isArchived()) {
    showToast("已归档订单为只读状态，不能执行写操作工具", "error");
    closeSlashMenu();
    return true;
  }
  if (item.write && isBusy()) {
    showToast("当前有请求正在处理中，请稍候再试", "error");
    return true;
  }
  const input = $("#message-input");
  if (input) {
    input.value = "";
    autoGrowComposer();
    renderComposerPreview();
  }
  closeSlashMenu();
  item.run(arg);
  return true;
}

export function syncSlashMenuFromInput() {
  const input = $("#message-input");
  if (!input) return;
  const parsed = parseSlashInput(input.value);
  if (!parsed) {
    closeSlashMenu();
    return;
  }
  const matches = filterSlashCommands(parsed.query);
  slashActiveIndex = 0;
  renderSlashMenu(matches, parsed.arg);
}

export function toggleSlashMenu() {
  const menu = $("#slash-menu");
  const input = $("#message-input");
  if (!menu || !input || input.disabled) return;
  if (!menu.hidden) {
    closeSlashMenu();
    input.focus();
    return;
  }
  if (!input.value.startsWith("/")) {
    input.value = "/";
    autoGrowComposer();
    renderComposerPreview();
  }
  input.focus();
  syncSlashMenuFromInput();
}

export function handleSlashKeydown(event) {
  if (event.isComposing || event.keyCode === 229) return false;
  const menu = $("#slash-menu");
  if (!menu || menu.hidden || !slashMatches.length) return false;

  if (event.key === "Escape") {
    event.preventDefault();
    closeSlashMenu();
    return true;
  }
  if (event.key === "ArrowDown") {
    event.preventDefault();
    slashActiveIndex = (slashActiveIndex + 1) % slashMatches.length;
    updateSlashActiveVisual();
    return true;
  }
  if (event.key === "ArrowUp") {
    event.preventDefault();
    slashActiveIndex = (slashActiveIndex - 1 + slashMatches.length) % slashMatches.length;
    updateSlashActiveVisual();
    return true;
  }
  if ((event.key === "Enter" && !event.shiftKey) || event.key === "Tab") {
    event.preventDefault();
    const parsed = parseSlashInput($("#message-input")?.value || "");
    const chosen = slashMatches[slashActiveIndex];
    if (event.key === "Tab" && chosen?.acceptsArg && !parsed?.arg) {
      const input = $("#message-input");
      if (input) {
        input.value = `${chosen.cmd} `;
        autoGrowComposer();
        syncSlashMenuFromInput();
      }
      return true;
    }
    return executeSlashCommand(chosen, parsed?.arg || "");
  }
  return false;
}

export function tryExecuteRawSlash(text) {
  const parsed = parseSlashInput(text);
  if (!parsed || !parsed.query) return false;
  const exact = SLASH_COMMANDS.find((item) =>
    item.cmd.slice(1).toLowerCase() === parsed.query
    || (item.aliases || []).some((alias) => alias.toLowerCase() === parsed.query),
  );
  if (!exact) return false;
  return executeSlashCommand(exact, parsed.arg);
}

/* ---------------- Global Ctrl/Cmd+K Command & Order Switcher Palette ---------------- */

function buildPaletteEntries(queryText = "") {
  const query = String(queryText || "").trim().toLowerCase();
  const archived = isArchived();
  const busy = isBusy();
  const entries = [];

  const views = [
    { group: "工作区视图", code: "01", title: "需求收集与对话", desc: "返回对话工作区", keywords: "intake chat 对话 收集", run: () => PrintOpsJourney.view("intake", true) },
    { group: "工作区视图", code: "02", title: "工艺方案对比", desc: "只读对比材料、工艺与本地示例费用", keywords: "compare options 方案 对比", run: () => PrintOpsJourney.view("compare", true) },
    { group: "工作区视图", code: "03", title: "核对与交接资料", desc: "检查所有参数、证据来源并准备导出", keywords: "review handoff 核对 交接 导出", run: () => PrintOpsJourney.view("review", true) },
  ];

  const tools = SLASH_COMMANDS.filter((item) => item.write).map((item) => ({
    group: "Agent 工具命令",
    code: item.code,
    title: `${item.cmd} · ${item.title}`,
    desc: archived ? "当前订单已归档（只读）" : item.desc,
    keywords: [item.cmd, item.code, ...(item.aliases || [])].join(" "),
    disabled: archived || busy,
    run: () => item.run(""),
  }));

  const history = loadOrderHistory().slice(0, 12).map((item) => {
    const title = orderHistoryTitle(item.order);
    const isCurrent = item.id === appState.sessionId;
    return {
      group: "历史订单快切",
      code: item.status === "archived" ? "AR" : isCurrent ? "NOW" : "ID",
      title,
      desc: `${item.id.toUpperCase()}${isCurrent ? " · 当前订单" : ""}${item.status === "archived" ? " · 已归档" : ""}`,
      keywords: `${item.id} ${title} history order 历史 订单`,
      disabled: isCurrent,
      run: () => actions.switchOrder?.(item.id),
    };
  });

  const workspace = [
    {
      group: "工作台操作",
      code: "NW",
      title: "新建一份订单",
      desc: "保留当前订单于历史并开启新对话",
      keywords: "new reset 新建 重置",
      run: () => $("#reset-order")?.click(),
    },
    {
      group: "工作台操作",
      code: "OD",
      title: appState.layout.orderCollapsed ? "展开右侧订单栏" : "收起右侧订单栏",
      desc: "切换右侧订单草稿栏显隐",
      keywords: "order pane toggle 订单栏 展开 收起",
      run: () => {
        if (!DESKTOP_QUERY.matches) {
          $("#workspace").dataset.mobilePane = $("#workspace").dataset.mobilePane === "order" ? "work" : "order";
          updateOrderToggle();
        } else {
          appState.layout.orderCollapsed = !appState.layout.orderCollapsed;
          applyLayout();
          saveLayout();
        }
      },
    },
    {
      group: "工作台操作",
      code: "HC",
      title: document.documentElement.dataset.theme === "high-contrast" ? "切换回标准对比主题" : "切换为高对比模式",
      desc: "调整工作台文字与边框对比度",
      keywords: "theme contrast high 高对比 主题",
      run: () => setHighContrast(document.documentElement.dataset.theme !== "high-contrast"),
    },
    {
      group: "工作台操作",
      code: "ST",
      title: "打开设置与能力",
      desc: "查看模型配置、产品索引、历史管理与工具统计",
      keywords: "settings model tools 设置 模型 工具箱",
      run: () => $("#settings-button")?.click(),
    },
  ];

  for (const item of [...views, ...tools, ...history, ...workspace]) {
    if (!query) {
      entries.push(item);
      continue;
    }
    const haystack = `${item.group} ${item.code} ${item.title} ${item.desc} ${item.keywords || ""}`.toLowerCase();
    if (haystack.includes(query)) entries.push(item);
  }
  return entries;
}

function renderPaletteList(query = "") {
  const list = $("#command-palette-list");
  if (!list) return;
  paletteItems = buildPaletteEntries(query);
  paletteActiveIndex = Math.max(0, paletteItems.findIndex((item) => !item.disabled));
  if (paletteActiveIndex < 0) paletteActiveIndex = 0;

  list.replaceChildren();
  if (!paletteItems.length) {
    list.append(el("p", "没有匹配的命令或订单，请换个关键词试试。", "command-palette-empty"));
    return;
  }

  let currentGroup = "";
  paletteItems.forEach((item, index) => {
    if (item.group !== currentGroup) {
      currentGroup = item.group;
      list.append(el("div", currentGroup, "command-palette-group"));
    }
    const btn = el("button", null, `command-palette-item${index === paletteActiveIndex ? " is-active" : ""}`);
    btn.type = "button";
    btn.role = "option";
    btn.setAttribute("aria-selected", String(index === paletteActiveIndex));
    btn.dataset.paletteIndex = String(index);
    btn.disabled = Boolean(item.disabled);

    const code = el("span", item.code, "command-palette-code");
    const copy = el("span", null, "command-palette-copy");
    copy.append(el("strong", item.title), el("small", item.desc));
    btn.append(code, copy);
    btn.addEventListener("click", () => runPaletteEntry(index));
    list.append(btn);
  });
}

function updatePaletteActiveVisual() {
  const list = $("#command-palette-list");
  if (!list) return;
  list.querySelectorAll(".command-palette-item").forEach((node) => {
    const idx = Number(node.dataset.paletteIndex);
    const active = idx === paletteActiveIndex;
    node.classList.toggle("is-active", active);
    node.setAttribute("aria-selected", String(active));
    if (active) node.scrollIntoView({ block: "nearest" });
  });
}

function movePaletteSelection(delta) {
  if (!paletteItems.length) return;
  const total = paletteItems.length;
  for (let step = 1; step <= total; step += 1) {
    const next = (paletteActiveIndex + delta * step + total) % total;
    if (!paletteItems[next]?.disabled) {
      paletteActiveIndex = next;
      updatePaletteActiveVisual();
      return;
    }
  }
}

function runPaletteEntry(index) {
  const entry = paletteItems[index];
  if (!entry || entry.disabled) return;
  closeCommandPalette({ restoreFocus: false });
  entry.run();
}

export function openCommandPalette() {
  const dialog = $("#command-palette");
  const input = $("#command-palette-input");
  if (!dialog || !input) return;
  if (dialog.open) {
    closeCommandPalette({ restoreFocus: true });
    return;
  }
  if (document.querySelector("dialog[open]")) return;
  closeSlashMenu();
  paletteReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  input.value = "";
  renderPaletteList("");
  dialog.showModal();
  input.focus();
}

export function closeCommandPalette({ restoreFocus = true } = {}) {
  const dialog = $("#command-palette");
  if (dialog?.open) dialog.close();
  if (restoreFocus && paletteReturnFocus && document.contains(paletteReturnFocus)) {
    paletteReturnFocus.focus({ preventScroll: true });
  }
  paletteReturnFocus = null;
}

export function bindCommandPaletteEvents() {
  const dialog = $("#command-palette");
  const input = $("#command-palette-input");
  const closeBtn = $("#command-palette-close");
  if (!dialog || !input) return;

  input.addEventListener("input", () => renderPaletteList(input.value));
  input.addEventListener("keydown", (event) => {
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === "ArrowDown") { event.preventDefault(); movePaletteSelection(1); return; }
    if (event.key === "ArrowUp") { event.preventDefault(); movePaletteSelection(-1); return; }
    if (event.key === "Enter") { event.preventDefault(); runPaletteEntry(paletteActiveIndex); }
  });
  closeBtn?.addEventListener("click", () => closeCommandPalette({ restoreFocus: true }));
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeCommandPalette({ restoreFocus: true });
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) closeCommandPalette({ restoreFocus: true });
  });
}
