/* PrintOps Agent Harness UI — inline turn run capsules, field delta & approval gates, live execution timer, and diff/undo bar. */

import { labels, toolCodes, toolLabels } from "./constants.js";
import { actions, appState, scrollBehavior } from "./store.js";
import { $, fmtMs, hasValue } from "./dom.js";

const STEP_LABELS = {
  run: "运行周期",
  perceive: "需求感知",
  memory: "更新记忆",
  clarify: "品类澄清",
  plan: "模型规划",
  planner: "模型规划",
  tool: "调用工具",
  tool_result: "工具回执",
  validate: "订单校验",
  recommend: "工艺推荐",
  estimate: "费用估算",
  preflight: "文件预检",
  handoff: "交接整理",
  quote: "询价准备",
};

const HIDDEN_DELTA_KEYS = new Set([
  "items", "productTypes", "quantityValue", "quantityUnit", "dimensions",
  "productSpecs", "uploadedFile", "itemId", "selectedOption", "orderGenerated",
]);

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function summarizeRunDuration(events = []) {
  if (!Array.isArray(events) || !events.length) return null;
  let summed = 0;
  let hasExplicit = false;
  for (const event of events) {
    const ms = Number(event?.durationMs);
    if (Number.isFinite(ms) && ms >= 0) {
      summed += ms;
      hasExplicit = true;
    }
  }
  const firstAt = Date.parse(events[0]?.at || "");
  const lastAt = Date.parse(events[events.length - 1]?.at || "");
  if (Number.isFinite(firstAt) && Number.isFinite(lastAt) && lastAt >= firstAt) {
    const wall = Math.round(lastAt - firstAt);
    return Math.max(wall, hasExplicit ? Math.round(summed) : 0);
  }
  return hasExplicit ? Math.round(summed) : null;
}

function summarizeStepFlow(events = [], toolTrace = []) {
  const labelsOut = [];
  const pushUnique = (text) => {
    if (!text || labelsOut[labelsOut.length - 1] === text) return;
    labelsOut.push(text);
  };
  for (const event of events) {
    if (!event || event.step === "run") continue;
    if (event.tool && toolLabels[event.tool]?.[0]) {
      pushUnique(toolLabels[event.tool][0]);
    } else if (STEP_LABELS[event.step]) {
      pushUnique(STEP_LABELS[event.step]);
    } else if (event.step) {
      pushUnique(event.step);
    }
  }
  if (!labelsOut.length && Array.isArray(toolTrace)) {
    for (const item of toolTrace) pushUnique(String(item || ""));
  }
  return labelsOut.slice(0, 4).join(" → ") || "完成订单处理";
}

function resolveCapsuleStatus(events = [], pendingCount = 0) {
  if (events.some((event) => event?.status === "failed" || event?.status === "error")) return "error";
  if (pendingCount > 0 || events.some((event) => ["blocked", "fallback", "rejected", "warning"].includes(event?.status))) {
    return "warn";
  }
  return "ok";
}

/**
 * Assemble an immutable Harness metadata snapshot for the current live turn.
 */
export function buildTurnMeta(data = {}, prevOrder = {}, updatedOrderKeys = new Set(), updatedSpecKeys = new Set()) {
  const order = data.order || {};
  const runTrace = Array.isArray(data.runTrace) ? data.runTrace : [];
  const toolTrace = Array.isArray(data.toolTrace) ? data.toolTrace : [];
  const reviewFields = Array.isArray(data.review?.fields) ? data.review.fields : [];
  const activeItemId = data.activeItemId || order.items?.[data.activeItemIndex]?.itemId || null;

  const deltas = [];
  for (const key of updatedOrderKeys) {
    if (HIDDEN_DELTA_KEYS.has(key)) continue;
    const value = order[key];
    if (!hasValue(value)) continue;
    const prevValue = prevOrder?.[key];
    deltas.push({
      fieldPath: key,
      label: labels[key] || key,
      value: String(value),
      prevValue: hasValue(prevValue) ? String(prevValue) : "",
      kind: hasValue(prevValue) ? "update" : "add",
    });
  }

  const prevSpecs = prevOrder?.productSpecs || {};
  const currentSpecs = order.productSpecs || {};
  const paramLabels = new Map(
    (data.productProfile?.parameters || []).map((item) => [item.key, item.label || item.key]),
  );
  for (const specKey of updatedSpecKeys) {
    const value = currentSpecs[specKey];
    if (!hasValue(value)) continue;
    const prevValue = prevSpecs[specKey];
    deltas.push({
      fieldPath: `productSpecs.${specKey}`,
      label: paramLabels.get(specKey) || specKey,
      value: String(value),
      prevValue: hasValue(prevValue) ? String(prevValue) : "",
      kind: hasValue(prevValue) ? "update" : "add",
    });
  }

  const pendingCandidates = reviewFields
    .filter((field) => {
      if (field?.status !== "pending_confirmation") return false;
      if (activeItemId && field.itemId && field.itemId !== activeItemId) return false;
      return field.candidateValue != null && String(field.candidateValue).trim() !== "";
    })
    .map((field) => ({
      field,
      fieldId: field.fieldId,
      fieldPath: field.fieldPath,
      label: field.label || labels[field.key] || field.fieldPath,
      candidateValue: String(field.candidateValue),
      confidence: Number.isFinite(Number(field.provenance?.confidence))
        ? Math.round(Number(field.provenance.confidence) * 100)
        : null,
      quote: field.provenance?.evidence?.quote || "",
    }));

  const options = Array.isArray(data.options) ? data.options : [];
  const selectedOption = data.selectedOption || data.activeItemSelectedOption;

  return {
    runId: data.runId || "",
    runTrace,
    toolTrace,
    decision: data.decision || null,
    revision: Number.isInteger(data.revision) ? data.revision : null,
    workflowStage: data.workflowStage || data.stage || "collect",
    workflowLabel: data.workflowLabel || "",
    durationMs: summarizeRunDuration(runTrace),
    deltas,
    pendingCandidates,
    optionsCount: options.length,
    hasSelectedOption: Boolean(selectedOption),
    canGenerate: Boolean(data.review?.canGenerate),
    orderGenerated: Boolean(data.orderGenerated),
    confirmed: data.confirmation?.status === "confirmed",
  };
}

/**
 * Render an inline collapsible Harness Run Capsule + Field Delta & Gate Bar for an assistant turn.
 */
export function createTurnHarnessNode(turnMeta) {
  if (!turnMeta) return null;
  const hasTrace = turnMeta.runTrace.length > 0 || Boolean(turnMeta.runId);
  const hasDeltas = turnMeta.deltas.length > 0;
  const hasGates = turnMeta.pendingCandidates.length > 0;
  const showCompareCta = turnMeta.optionsCount > 0 && !turnMeta.hasSelectedOption;
  const showReviewCta = !showCompareCta && (turnMeta.canGenerate || (turnMeta.orderGenerated && !turnMeta.confirmed));
  if (!hasTrace && !hasDeltas && !hasGates && !showCompareCta && !showReviewCta) return null;

  const container = el("div", null, "harness-turn");

  if (hasTrace) {
    const status = resolveCapsuleStatus(turnMeta.runTrace, turnMeta.pendingCandidates.length);
    const capsule = el("details", null, `harness-capsule is-${status}`);
    if (turnMeta.runId) capsule.dataset.runId = turnMeta.runId;

    const summary = el("summary", null, "harness-capsule-summary");
    const dot = el("span", null, "harness-status-dot");
    dot.setAttribute("aria-hidden", "true");
    const flow = el("span", summarizeStepFlow(turnMeta.runTrace, turnMeta.toolTrace), "harness-flow-text");
    const metrics = el("span", null, "harness-metrics");

    if (hasDeltas) {
      metrics.append(el("span", `更新 ${turnMeta.deltas.length} 项`, "harness-metric-pill is-delta"));
    }
    if (hasGates) {
      metrics.append(el("span", `${turnMeta.pendingCandidates.length} 项待确认`, "harness-metric-pill is-gate"));
    }
    if (turnMeta.durationMs !== null) {
      metrics.append(el("span", fmtMs(turnMeta.durationMs), "harness-metric-pill is-mono"));
    }
    if (turnMeta.revision !== null && turnMeta.revision > 0) {
      metrics.append(el("span", `r${turnMeta.revision}`, "harness-metric-pill is-mono"));
    }

    const caret = el("span", "▾", "harness-capsule-caret");
    caret.setAttribute("aria-hidden", "true");
    summary.append(dot, flow, metrics, caret);
    capsule.append(summary);

    const body = el("div", null, "harness-capsule-body");
    if (turnMeta.decision?.reason) {
      const decisionRow = el("div", null, "harness-decision-line");
      decisionRow.append(
        el("span", turnMeta.decision.humanConfirmationRequired ? "人工门禁" : "决策说明", "harness-decision-tag"),
        el("span", turnMeta.decision.reason, "harness-decision-text"),
      );
      body.append(decisionRow);
    }

    if (turnMeta.runTrace.length) {
      const traceList = el("ol", null, "harness-trace-list");
      for (const event of turnMeta.runTrace) {
        const li = el("li", null, `harness-trace-item is-${event.status || "ok"}`);
        const codeText = event.tool ? (toolCodes[event.tool] || "TL") : (event.step === "run" ? "RN" : "ST");
        const code = el("span", codeText, "harness-trace-code");
        const main = el("span", null, "harness-trace-main");
        const titleText = event.tool && toolLabels[event.tool]?.[0]
          ? `${STEP_LABELS[event.step] || event.step} · ${toolLabels[event.tool][0]}`
          : (STEP_LABELS[event.step] || event.step || "步骤");
        main.append(el("strong", titleText));
        if (event.detail) main.append(el("small", event.detail));
        li.append(code, main);
        if (Number.isFinite(Number(event.durationMs))) {
          li.append(el("span", fmtMs(event.durationMs), "harness-trace-ms"));
        }
        traceList.append(li);
      }
      body.append(traceList);
    }

    const foot = el("div", null, "harness-capsule-foot");
    if (turnMeta.runId) foot.append(el("code", `run:${turnMeta.runId}`));
    if (turnMeta.workflowLabel) foot.append(el("span", `阶段：${turnMeta.workflowLabel}`));
    body.append(foot);

    capsule.append(body);
    container.append(capsule);
  }

  if (hasDeltas || showCompareCta || showReviewCta) {
    const bar = el("div", null, "harness-delta-bar");
    for (const delta of turnMeta.deltas.slice(0, 6)) {
      const chip = el("button", null, `harness-delta-chip is-${delta.kind}`);
      chip.type = "button";
      chip.dataset.fieldPath = delta.fieldPath;
      chip.title = delta.prevValue
        ? `${delta.label}：${delta.prevValue} → ${delta.value}（点击定位到订单草稿）`
        : `${delta.label}：${delta.value}（点击定位到订单草稿）`;
      chip.append(
        el("span", delta.kind === "add" ? "+" : "~", "harness-delta-op"),
        el("span", `${delta.label}：${delta.value}`),
      );
      chip.addEventListener("click", () => actions.focusDraftField?.(delta.fieldPath));
      bar.append(chip);
    }
    if (turnMeta.deltas.length > 6) {
      bar.append(el("span", `+${turnMeta.deltas.length - 6} 项`, "harness-delta-more"));
    }
    if (showCompareCta) {
      const cta = el("button", `比较 ${turnMeta.optionsCount} 个工艺方案 →`, "harness-cta-chip");
      cta.type = "button";
      cta.addEventListener("click", () => PrintOpsJourney.view("compare", true));
      bar.append(cta);
    } else if (showReviewCta) {
      const cta = el("button", turnMeta.orderGenerated ? "前往确认交接单 →" : "前往核对与交接 →", "harness-cta-chip");
      cta.type = "button";
      cta.addEventListener("click", () => PrintOpsJourney.view("review", true));
      bar.append(cta);
    }
    container.append(bar);
  }

  if (hasGates) {
    const gateList = el("div", null, "harness-gate-list");
    for (const candidate of turnMeta.pendingCandidates) {
      const row = el("div", null, "harness-gate-row");
      row.dataset.gateFieldId = candidate.fieldId;
      row.dataset.gateCandidate = candidate.candidateValue;

      const info = el("div", null, "harness-gate-info");
      const badge = el("span", "待确认候选", "harness-gate-badge");
      const text = el("strong", `${candidate.label}：${candidate.candidateValue}`);
      info.append(badge, text);
      if (candidate.confidence != null) {
        info.append(el("span", `置信度 ${candidate.confidence}%`, "harness-gate-conf"));
      }
      if (candidate.quote) {
        info.append(el("small", `原文：“${candidate.quote}”`, "harness-gate-quote"));
      }

      const controls = el("div", null, "harness-gate-actions");
      const confirmBtn = el("button", "确认候选", "harness-gate-btn is-confirm");
      confirmBtn.type = "button";
      confirmBtn.addEventListener("click", () => actions.changeField?.(candidate.field, "confirm"));

      const rejectBtn = el("button", "拒绝", "harness-gate-btn is-reject");
      rejectBtn.type = "button";
      rejectBtn.addEventListener("click", () => actions.changeField?.(candidate.field, "reject"));

      controls.append(confirmBtn, rejectBtn);
      row.append(info, controls);
      gateList.append(row);
    }
    container.append(gateList);
  }

  return container;
}

/**
 * Synchronize inline candidate gates in #chat-feed whenever authoritative server state changes.
 */
export function syncInlineGates(state = appState.state) {
  const rows = document.querySelectorAll("#chat-feed .harness-gate-row[data-gate-field-id]");
  if (!rows.length) return;
  const fieldMap = new Map((state?.review?.fields || []).map((field) => [field.fieldId, field]));
  const archived = state?.status === "archived" || state?.orderStatus === "archived";

  rows.forEach((row) => {
    const fieldId = row.dataset.gateFieldId;
    const latest = fieldMap.get(fieldId);
    const controls = row.querySelector(".harness-gate-actions");
    const badge = row.querySelector(".harness-gate-badge");
    if (!controls || !badge) return;

    if (!latest || latest.status !== "pending_confirmation") {
      row.classList.add("is-resolved");
      const resolvedLabel = latest?.status === "confirmed"
        ? `已确认 · ${latest.value ?? ""}`.trim()
        : latest?.value
          ? `已更新 · ${latest.value}`
          : "已处理";
      badge.textContent = resolvedLabel;
      controls.replaceChildren(el("span", latest?.status === "confirmed" ? "✓ 已写入规格" : "已关闭", "harness-gate-done"));
    } else if (archived) {
      controls.querySelectorAll("button").forEach((btn) => { btn.disabled = true; });
    }
  });
}

/**
 * Honest live pending indicator replacing the hardcoded 900ms fake phase loop.
 */
export function createPendingIndicator() {
  const message = el("div", null, "message assistant pending-message");
  message.setAttribute("role", "status");

  const avatar = el("div", "AI", "avatar pending-avatar");
  const content = el("div");
  const bubble = el("div", null, "bubble pending-bubble harness-pending");

  const modeLabel = appState.llmSettings?.enabled && appState.llmSettings?.model
    ? `模型 · ${appState.llmSettings.model}`
    : "本地规则引擎";
  const items = appState.state?.order?.items || [];
  const activeIdx = appState.state?.activeItemIndex;
  const targetLabel = items.length > 1 && Number.isInteger(activeIdx) && items[activeIdx]
    ? `正在处理第 ${activeIdx + 1} 项（${items[activeIdx].productType || "产品"}）`
    : "正在解析需求并校验规格";

  const head = el("div", null, "harness-pending-head");
  const modePill = el("span", modeLabel, "harness-pending-mode");
  const timerNode = el("span", "0.0s", "harness-pending-elapsed");
  timerNode.setAttribute("aria-hidden", "true");
  head.append(modePill, timerNode);

  const line = el("div", null, "harness-pending-line");
  const label = el("span", targetLabel, "harness-pending-label");
  const dots = el("span", null, "thinking-dots");
  dots.setAttribute("aria-hidden", "true");
  dots.append(el("i"), el("i"), el("i"));
  line.append(label, dots);

  const hint = el("small", "模型响应较慢时，可点击下方「停止」先回到输入。", "harness-pending-hint");
  hint.hidden = true;

  bubble.append(head, line, hint);
  content.append(bubble);
  message.append(avatar, content);

  const startedAt = performance.now();
  const timer = setInterval(() => {
    const elapsedSec = (performance.now() - startedAt) / 1000;
    timerNode.textContent = `${elapsedSec.toFixed(1)}s`;
    if (elapsedSec >= 2.5 && hint.hidden) hint.hidden = false;
  }, 100);

  return {
    node: message,
    stop: () => {
      clearInterval(timer);
      message.remove();
    },
  };
}

/**
 * Render an upgraded Diff & Undo banner inside #change-notice without breaking the
 * test-pinned "撤销本次修改" button label.
 */
export function renderUndoBanner({ label, before, after, revision, onUndo }) {
  const notice = $("#change-notice");
  if (!notice) return;
  appState.activeUndoNoticeRevision = revision;
  notice.hidden = false;
  notice.classList.add("is-undo-banner");

  const copy = el("div", null, "change-notice-copy");
  if (Number.isInteger(revision)) copy.append(el("span", `r${revision}`, "change-notice-rev"));
  const beforeText = hasValue(before) ? String(before) : "空";
  const afterText = hasValue(after) ? String(after) : "已清空";
  copy.append(el("span", `${label}：${beforeText} → ${afterText}，相关结果需按当前版本核对。`, "change-notice-text"));

  const btns = el("div", null, "change-notice-actions");
  const undoBtn = el("button", "撤销本次修改", "secondary-action change-notice-undo");
  undoBtn.type = "button";
  undoBtn.addEventListener("click", () => onUndo?.());

  const closeBtn = el("button", "×", "change-notice-dismiss");
  closeBtn.type = "button";
  closeBtn.setAttribute("aria-label", "收起修改提示");
  closeBtn.title = "收起提示";
  closeBtn.addEventListener("click", () => {
    notice.hidden = true;
    appState.activeUndoNoticeRevision = null;
  });

  btns.append(undoBtn, closeBtn);
  notice.replaceChildren(copy, btns);
}

/**
 * Update compact Harness header badges (revision, execution mode, latest trace jump).
 */
export function updateHarnessHeaderMeta(state = appState.state) {
  const revNode = $("#revision-badge");
  if (revNode) {
    const rev = Number.isInteger(state?.revision) ? state.revision : 0;
    revNode.textContent = `r${rev}`;
    revNode.title = `当前订单状态版本号：revision ${rev}`;
  }

  const modeNode = $("#mode-badge");
  if (modeNode) {
    const llm = appState.llmSettings || {};
    if (llm.enabled && llm.model) {
      const shortModel = llm.model.length > 18 ? `${llm.model.slice(0, 16)}…` : llm.model;
      modeNode.textContent = llm.lastError ? `回退规则` : `模型 · ${shortModel}`;
      modeNode.dataset.mode = llm.lastError ? "fallback" : "llm";
      modeNode.title = llm.lastError ? `上次模型调用异常，已安全回退规则引擎` : `当前模型规划：${llm.model}`;
    } else {
      modeNode.textContent = "规则引擎";
      modeNode.dataset.mode = "rules";
      modeNode.title = "当前使用本地确定性规则引擎（零外部依赖）";
    }
  }

  const traceJump = $("#trace-jump");
  if (traceJump) {
    const hasRun = Boolean(state?.runId || (state?.runTrace && state.runTrace.length));
    traceJump.disabled = !hasRun;
    traceJump.title = hasRun ? "查看最新一轮运行轨迹" : "发送需求后可查看运行轨迹";
  }
}

/**
 * Jump to and expand the latest Harness run capsule in #chat-feed (or open settings toolbox trace).
 */
export function openLatestRunTrace() {
  const capsules = document.querySelectorAll("#chat-feed .harness-capsule");
  if (capsules.length) {
    PrintOpsJourney.view("intake");
    const latest = capsules[capsules.length - 1];
    latest.open = true;
    latest.scrollIntoView({ block: "nearest", behavior: scrollBehavior() });
    latest.querySelector("summary")?.focus({ preventScroll: true });
    return;
  }
  $("#settings-button")?.click();
  document.querySelector('[data-settings-view="tools"]')?.click();
  const runPanel = $("#run-panel");
  if (runPanel) runPanel.open = true;
}
