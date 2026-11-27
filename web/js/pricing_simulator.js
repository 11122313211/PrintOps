/* 量价拐点模拟器 & 工艺 What-If 试算器（纯原生 ESM，仅使用 createElement/textContent，只读视图零 POST）。 */

const SPEC_FALLBACK_LABELS = {
  cardCorners: "名片切角",
  folding: "折页方式",
  boxStructure: "盒型结构",
  coverPaper: "封面用纸",
  labelShape: "标签形状",
  adhesive: "标签胶型",
  labelFormat: "交付形式",
  install: "安装方式",
  boardThickness: "板材厚度",
  cupVolume: "纸杯容量",
  paperParts: "联单联数",
};

const EMPTY_FINISHING_LABELS = new Set([
  "",
  "无",
  "无需",
  "无工艺",
  "无特殊工艺",
  "无特殊表面工艺",
  "不覆膜",
]);

const DEFAULT_DISCLAIMER =
  "所有阶梯与试算基于本地参考价卡模拟，不含税费/运费/打样，不构成供应商正式报价。";

const simState = {
  sessionId: "",
  itemKey: "",
  revision: null,
  signature: "",
  baselineData: null,
  data: null,
  overrides: {},
  loading: false,
  applying: false,
  error: "",
  notice: "",
  collapsed: false,
  requestSeq: 0,
  inflightSignature: "",
  failedSignature: "",
  lastContainer: null,
  lastContext: null,
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function formatNumber(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value ?? "");
  try {
    return new Intl.NumberFormat("zh-CN").format(num);
  } catch {
    return String(num);
  }
}

function formatQty(qty, unit) {
  if (qty === null || qty === undefined || qty === "") return "未定数量";
  return `${formatNumber(qty)}${unit ? ` ${unit}` : ""}`;
}

function formatPct(value, withPlus = true) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "";
  const fixed = Math.abs(num).toFixed(1);
  if (num > 0) return `${withPlus ? "+" : ""}${fixed}%`;
  if (num < 0) return `-${fixed}%`;
  return "0.0%";
}

function normalizeMaterialKey(text) {
  return String(text || "")
    .replace(/(\d+(?:\.\d+)?)\s*(?:g\/m[²2]|g|克)/gi, "$1g/m2")
    .replace(/\s+/g, "")
    .toLowerCase();
}

function activeOrderFromContext(context) {
  const order = context?.state?.order;
  if (!order || typeof order !== "object") return {};
  const items = Array.isArray(order.items) ? order.items : [];
  if (items.length > 1) {
    if (context.itemId) {
      const found = items.find((item) => item && item.itemId === context.itemId);
      if (found) return found;
    }
    if (Number.isInteger(context.itemIndex) && items[context.itemIndex]) {
      return items[context.itemIndex];
    }
    const fallbackIndex = Number.isInteger(context.state?.activeItemIndex)
      ? context.state.activeItemIndex
      : 0;
    return items[fallbackIndex] || order;
  }
  return order;
}

function resolveItemKey(context) {
  if (context?.itemId) return String(context.itemId);
  if (Number.isInteger(context?.itemIndex)) return `idx:${context.itemIndex}`;
  const order = context?.state?.order;
  const items = Array.isArray(order?.items) ? order.items : [];
  if (items.length > 1 && Number.isInteger(context?.state?.activeItemIndex)) {
    const active = items[context.state.activeItemIndex];
    return active?.itemId ? String(active.itemId) : `idx:${context.state.activeItemIndex}`;
  }
  return "single";
}

function isContainerVisible(container) {
  if (!container || !container.isConnected) return false;
  if (container.hidden) return false;
  if (container.closest("[hidden]")) return false;
  const workspace = document.getElementById("workspace");
  if (workspace && workspace.dataset && workspace.dataset.view) {
    const comparePane = container.closest("#phase-compare");
    if (comparePane && workspace.dataset.view !== "compare") {
      return false;
    }
  }
  return true;
}

function cloneOverrides(source) {
  const out = {};
  if (!source || typeof source !== "object") return out;
  for (const [key, val] of Object.entries(source)) {
    if (key === "productSpecs" && val && typeof val === "object") {
      const specs = { ...val };
      if (Object.keys(specs).length) out.productSpecs = specs;
    } else if (Array.isArray(val)) {
      out[key] = [...val];
    } else if (val !== undefined && val !== null) {
      out[key] = val;
    }
  }
  return out;
}

function hasAnyOverrides(overrides) {
  if (!overrides || typeof overrides !== "object") return false;
  return Object.keys(overrides).some((key) => {
    if (key === "productSpecs") {
      return overrides.productSpecs && Object.keys(overrides.productSpecs).length > 0;
    }
    return overrides[key] !== undefined && overrides[key] !== null;
  });
}

function sameFinishingList(a, b) {
  const cleanA = (Array.isArray(a) ? a : []).filter((v) => !EMPTY_FINISHING_LABELS.has(String(v || "").trim()));
  const cleanB = (Array.isArray(b) ? b : []).filter((v) => !EMPTY_FINISHING_LABELS.has(String(v || "").trim()));
  if (cleanA.length !== cleanB.length) return false;
  return cleanA.every((item, idx) => item === cleanB[idx]);
}

function pruneBaselineMatches(overrides, data) {
  const base = data?.baselineValues;
  if (!base || typeof base !== "object") return cloneOverrides(overrides);
  const next = cloneOverrides(overrides);
  const materialField = data?.adjustable?.materialField || "paper";

  if (next.quantity !== undefined && Number(next.quantity) === Number(base.quantity)) {
    delete next.quantity;
  }
  if (next.paper !== undefined && normalizeMaterialKey(next.paper) === normalizeMaterialKey(base.material)) {
    delete next.paper;
  }
  if (next.finishing !== undefined && sameFinishingList(next.finishing, base.finishing)) {
    delete next.finishing;
  }
  if (next.binding !== undefined && String(next.binding) === String(base.binding || "")) {
    delete next.binding;
  }
  if (next.printMode !== undefined && String(next.printMode) === String(base.printMode || "合版")) {
    delete next.printMode;
  }
  if (next.printing !== undefined && String(next.printing) === String(base.printing || "")) {
    delete next.printing;
  }
  if (next.pages !== undefined && String(next.pages) === String(base.pages || "")) {
    delete next.pages;
  }
  if (next.productSpecs && typeof next.productSpecs === "object") {
    const baseSpecs = base.productSpecs || {};
    for (const [specKey, specVal] of Object.entries({ ...next.productSpecs })) {
      const baseVal =
        specKey === "labelMaterial" || specKey === "displayMaterial"
          ? baseSpecs[specKey] || base.material || ""
          : baseSpecs[specKey] || "";
      const isMatSpec =
        (materialField === `productSpecs.${specKey}`) || specKey === "coverPaper";
      const equal = isMatSpec
        ? normalizeMaterialKey(specVal) === normalizeMaterialKey(baseVal)
        : String(specVal || "") === String(baseVal || "");
      if (equal) delete next.productSpecs[specKey];
    }
    if (!Object.keys(next.productSpecs).length) {
      delete next.productSpecs;
    }
  }
  return next;
}

/**
 * 将试算覆盖项映射为标准 /api/fields 的扁平非空 values 字典。
 * 注意：printMode 不属于订单字段（属于工艺方案选择），绝不写入 /api/fields。
 */
function buildWritableFieldValues(overrides, data) {
  const values = {};
  if (!overrides || typeof overrides !== "object") return values;

  const unit = data?.unit || data?.baselineValues?.unit || "";
  const materialField = data?.adjustable?.materialField || "paper";

  if (overrides.quantity !== undefined && Number.isInteger(Number(overrides.quantity))) {
    const qty = Number(overrides.quantity);
    if (qty > 0) {
      values.quantity = unit ? `${qty} ${unit}` : String(qty);
    }
  }

  if (typeof overrides.paper === "string" && overrides.paper.trim()) {
    values[materialField] = overrides.paper.trim();
  }

  if (overrides.finishing !== undefined) {
    if (Array.isArray(overrides.finishing)) {
      const parts = overrides.finishing
        .map((item) => String(item || "").trim())
        .filter((item) => item && !EMPTY_FINISHING_LABELS.has(item));
      values.finishing = parts.length ? parts.join("、") : "无特殊工艺";
    } else if (typeof overrides.finishing === "string") {
      const text = overrides.finishing.trim();
      values.finishing = text || "无特殊工艺";
    }
  }

  for (const key of ["binding", "pages", "printing"]) {
    if (typeof overrides[key] === "string" && overrides[key].trim()) {
      values[key] = overrides[key].trim();
    }
  }

  if (overrides.productSpecs && typeof overrides.productSpecs === "object") {
    for (const [specKey, specVal] of Object.entries(overrides.productSpecs)) {
      if (typeof specVal === "string" && specVal.trim()) {
        values[`productSpecs.${specKey}`] = specVal.trim();
      }
    }
  }

  return values;
}

function getEffectiveValues(data) {
  return data?.simulatedValues || data?.baselineValues || {};
}

async function loadInitial(container, context, signature) {
  if (typeof context?.fetchInitial !== "function") return;
  const seq = ++simState.requestSeq;
  simState.loading = true;
  simState.error = "";
  simState.notice = "";
  simState.inflightSignature = signature;
  paint(container, context);
  try {
    const result = await context.fetchInitial();
    if (seq !== simState.requestSeq || simState.signature !== signature) return;
    simState.baselineData = result;
    simState.data = result;
    simState.overrides = {};
    simState.loading = false;
    simState.inflightSignature = "";
    simState.failedSignature = "";
    paint(container, context);
  } catch (err) {
    if (seq !== simState.requestSeq || simState.signature !== signature) return;
    simState.loading = false;
    simState.inflightSignature = "";
    simState.failedSignature = signature;
    simState.error = err?.message || "暂无法读取量价试算，请稍后重试。";
    paint(container, context);
  }
}

async function runWhatIf(nextOverrides) {
  const container = simState.lastContainer;
  const context = simState.lastContext;
  if (!container || !context) return;

  const cleaned = pruneBaselineMatches(nextOverrides, simState.baselineData || simState.data);
  simState.overrides = cleaned;
  simState.error = "";
  simState.notice = "";

  // 当所有改动恢复到基准时，直接还原本地缓存基线，零额外 POST 请求
  if (!hasAnyOverrides(cleaned) && simState.baselineData) {
    simState.requestSeq += 1;
    simState.loading = false;
    simState.data = simState.baselineData;
    paint(container, context);
    return;
  }

  if (typeof context.fetchSimulate !== "function") {
    paint(container, context);
    return;
  }

  const seq = ++simState.requestSeq;
  const targetSig = simState.signature;
  simState.loading = true;
  paint(container, context);

  try {
    const result = await context.fetchSimulate(cleaned);
    if (seq !== simState.requestSeq || simState.signature !== targetSig) return;
    simState.data = result;
    simState.loading = false;
    paint(container, context);
  } catch (err) {
    if (seq !== simState.requestSeq || simState.signature !== targetSig) return;
    simState.loading = false;
    simState.error = err?.message || "试算请求失败，请检查所选参数后重试。";
    paint(container, context);
  }
}

async function applyOverridesToOrder() {
  const container = simState.lastContainer;
  const context = simState.lastContext;
  if (!container || !context || typeof context.onApplyOverrides !== "function") return;
  if (context.archived || simState.applying || simState.loading) return;

  const fieldValues = buildWritableFieldValues(simState.overrides, simState.data || simState.baselineData);
  if (!Object.keys(fieldValues).length) return;

  const hadPrintMode = Boolean(simState.overrides.printMode);
  simState.applying = true;
  simState.error = "";
  simState.notice = "";
  paint(container, context);

  try {
    await context.onApplyOverrides(fieldValues);
    simState.applying = false;
    simState.overrides = {};
    simState.notice = hadPrintMode
      ? "已写入试算规格到订单草稿；印刷方式请在上方重新生成的方案卡中选择。"
      : "已将试算配置写入订单草稿，相关推荐方案与确认状态已同步刷新。";
    paint(container, context);
  } catch (err) {
    simState.applying = false;
    simState.error = err?.message || "写入订单失败，试算配置仍保留在当前面板。";
    paint(container, context);
  }
}

function updateScalarOverride(key, value) {
  const next = cloneOverrides(simState.overrides);
  if (value === undefined || value === null) {
    delete next[key];
  } else {
    next[key] = value;
  }
  runWhatIf(next);
}

function updateSpecOverride(specKey, value) {
  const next = cloneOverrides(simState.overrides);
  next.productSpecs = { ...(next.productSpecs || {}) };
  next.productSpecs[specKey] = value;
  runWhatIf(next);
}

function toggleFinishing(processName) {
  const currentList = Array.isArray(simState.overrides.finishing)
    ? [...simState.overrides.finishing]
    : [...(getEffectiveValues(simState.data).finishing || [])];
  const exists = currentList.includes(processName);
  const updated = exists
    ? currentList.filter((item) => item !== processName)
    : [...currentList, processName];
  const next = cloneOverrides(simState.overrides);
  next.finishing = updated;
  runWhatIf(next);
}

function renderBlockedCard(data) {
  const baseline = data.baseline || {};
  const card = el("div", "pricing-sim-blocked");
  const title = el("strong", "pricing-sim-blocked-title", "暂不能进行量价与工艺试算");
  card.append(title);

  if (baseline.reasonDetail) {
    card.append(el("p", "pricing-sim-blocked-desc", baseline.reasonDetail));
  } else if (Array.isArray(baseline.missing) && baseline.missing.length) {
    const items = baseline.missing.slice(0, 4).join("、");
    card.append(
      el(
        "p",
        "pricing-sim-blocked-desc",
        `当前订单尚缺关键计价驱动（${items}）。请先在左侧对话或右侧订单草稿中补充完整，即可解锁阶梯量价与工艺差价试算。`
      )
    );
  } else {
    card.append(
      el(
        "p",
        "pricing-sim-blocked-desc",
        "当前品类或规格尚未满足本地参考价卡的最低计价前提，补充参数后将自动展示阶梯与工艺试算。"
      )
    );
  }
  return card;
}

function renderTiersSection(data) {
  const section = el("section", "pricing-sim-section");
  const header = el("div", "pricing-sim-section-head");
  header.append(
    el("h4", "pricing-sim-section-title", "数量阶梯与性价比拐点"),
    el("span", "pricing-sim-section-hint", "点击任意数量档即可带入下方试算")
  );
  section.append(header);

  const unit = data.unit || "";
  const sweetSpot = data.sweetSpot;
  if (sweetSpot && sweetSpot.summary) {
    const banner = el("div", "pricing-sim-sweet-banner");
    const bannerCopy = el("div", "pricing-sim-sweet-copy");
    bannerCopy.append(
      el("span", "pricing-sim-badge is-sweet", "性价比拐点"),
      el("span", "pricing-sim-sweet-text", sweetSpot.summary)
    );
    const tryBtn = el(
      "button",
      "pricing-sim-sweet-btn secondary-action",
      `试算 ${formatQty(sweetSpot.quantity, sweetSpot.unit || unit)}`
    );
    tryBtn.type = "button";
    tryBtn.disabled = simState.loading || simState.applying;
    tryBtn.addEventListener("click", () => updateScalarOverride("quantity", sweetSpot.quantity));
    banner.append(bannerCopy, tryBtn);
    section.append(banner);
  }

  const tiers = Array.isArray(data.tiers) ? data.tiers : [];
  const readyUnits = tiers
    .filter((tier) => tier.status === "ready" && Number.isFinite(Number(tier.unitMid)))
    .map((tier) => Number(tier.unitMid));
  const maxUnitPrice = readyUnits.length ? Math.max(...readyUnits) : 0;

  const grid = el("div", "pricing-sim-tiers");
  const baselineQty = data.baselineQuantity;

  tiers.forEach((tier) => {
    const isBaseline = baselineQty !== null && baselineQty !== undefined && tier.quantity === baselineQty;
    const isSelectedSim = Boolean(simState.overrides.quantity) && Number(simState.overrides.quantity) === tier.quantity;
    const isCurrent = Boolean(tier.isCurrent);
    const isSweet = Boolean(tier.isSweetSpot);

    const card = el(
      "button",
      [
        "pricing-sim-tier-card",
        isCurrent ? "is-current" : "",
        isSweet ? "is-sweet" : "",
        isSelectedSim ? "is-sim-selected" : "",
        tier.status !== "ready" ? "is-unavailable" : "",
      ]
        .filter(Boolean)
        .join(" ")
    );
    card.type = "button";
    card.setAttribute("aria-pressed", String(isCurrent || isSelectedSim));
    card.dataset.tierQuantity = String(tier.quantity);
    card.addEventListener("click", () => updateScalarOverride("quantity", tier.quantity));

    // 顶行：数量 + 徽章
    const top = el("div", "pricing-sim-tier-top");
    top.append(el("strong", "pricing-sim-tier-qty", formatQty(tier.quantity, tier.unit || unit)));

    const badges = el("span", "pricing-sim-tier-badges");
    if (isBaseline && !simState.overrides.quantity) {
      badges.append(el("span", "pricing-sim-badge is-current", "当前"));
    } else {
      if (isSelectedSim) badges.append(el("span", "pricing-sim-badge is-sim", "试算"));
      if (isBaseline) badges.append(el("span", "pricing-sim-badge is-current", "原单"));
    }
    if (isSweet) {
      badges.append(el("span", "pricing-sim-badge is-sweet", "拐点"));
    }
    if (badges.childNodes.length) top.append(badges);
    card.append(top);

    // 示例区间
    const rangeText = tier.status === "ready" ? tier.displayRange : tier.displayRange || "暂不能估算";
    card.append(el("div", "pricing-sim-tier-range", rangeText));

    // 单价与相对降幅
    if (tier.status === "ready" && tier.unitMid) {
      const unitRow = el("div", "pricing-sim-tier-unit-row");
      unitRow.append(
        el(
          "span",
          "pricing-sim-tier-unit",
          `≈ ¥${tier.unitMid} / ${tier.unit || unit || "件"}`
        )
      );
      if (
        tier.deltaFromCurrentUnitPct !== null &&
        tier.deltaFromCurrentUnitPct !== undefined &&
        !tier.isCurrent
      ) {
        const pct = Number(tier.deltaFromCurrentUnitPct);
        const chipCls = pct < -0.5 ? "is-down" : pct > 0.5 ? "is-up" : "is-flat";
        unitRow.append(
          el("span", `pricing-sim-pct ${chipCls}`, `单价 ${formatPct(pct)}`)
        );
      }
      card.append(unitRow);

      // 相对单价对比进度条
      const barTrack = el("div", "pricing-sim-unit-bar");
      barTrack.setAttribute("aria-hidden", "true");
      const ratio = maxUnitPrice > 0 ? Math.max(8, Math.min(100, Math.round((Number(tier.unitMid) / maxUnitPrice) * 100))) : 100;
      const barFill = el("span", `pricing-sim-unit-fill${isSweet ? " is-sweet" : ""}`);
      barFill.style.width = `${ratio}%`;
      barTrack.append(barFill);
      card.append(barTrack);

      // 底行：总价对比
      const foot = el("div", "pricing-sim-tier-foot");
      if (tier.isCurrent) {
        foot.textContent = simState.overrides.quantity ? "当前试算档" : "当前订单基准";
      } else if (tier.deltaFromCurrentTotalPct !== null && tier.deltaFromCurrentTotalPct !== undefined) {
        foot.textContent = `总价 ${formatPct(tier.deltaFromCurrentTotalPct)}`;
      } else if (tier.note) {
        foot.textContent = tier.note;
      }
      card.append(foot);
    } else {
      card.append(el("div", "pricing-sim-tier-foot", tier.note || "待补充参数"));
    }

    grid.append(card);
  });

  section.append(grid);
  return section;
}

function renderWhatIfControls(data) {
  const adjustable = data.adjustable || {};
  const effective = getEffectiveValues(data);
  const materialField = adjustable.materialField || "paper";
  const controls = el("div", "pricing-sim-controls");

  // 1. 印刷方式（合版 / 专版 / 数码）胶囊组
  const printModes = Array.isArray(adjustable.printModes) ? adjustable.printModes : [];
  if (printModes.length) {
    const group = el("div", "pricing-sim-field");
    group.append(el("span", "pricing-sim-label", "印刷方式"));
    const pills = el("div", "pricing-sim-pills");
    pills.setAttribute("role", "group");
    pills.setAttribute("aria-label", "印刷方式试算");
    const activeMode = effective.printMode || "合版";
    printModes.forEach((mode) => {
      const active = activeMode === mode;
      const btn = el("button", `pricing-sim-pill${active ? " is-active" : ""}`, mode);
      btn.type = "button";
      btn.setAttribute("aria-pressed", String(active));
      btn.disabled = simState.applying;
      btn.addEventListener("click", () => updateScalarOverride("printMode", mode));
      pills.append(btn);
    });
    group.append(pills);
    controls.append(group);
  }

  // 2. 纸张 / 材料下拉选择
  const materials = Array.isArray(adjustable.materials) ? adjustable.materials : [];
  if (materials.length) {
    const group = el("label", "pricing-sim-field");
    group.append(el("span", "pricing-sim-label", "纸张 / 材料"));
    const select = el("select", "pricing-sim-select");
    select.setAttribute("aria-label", "纸张或材料试算");
    select.disabled = simState.applying;
    const currentMat = effective.material || effective.paper || "";
    if (!currentMat) {
      const placeholder = el("option", "", "请选择已标定材料…");
      placeholder.value = "";
      select.append(placeholder);
    }
    materials.forEach((mat) => {
      const opt = el("option", "", mat);
      opt.value = mat;
      if (normalizeMaterialKey(mat) === normalizeMaterialKey(currentMat)) {
        opt.selected = true;
      }
      select.append(opt);
    });
    select.addEventListener("change", () => {
      if (!select.value) return;
      if (materialField === "productSpecs.labelMaterial") {
        updateSpecOverride("labelMaterial", select.value);
      } else if (materialField === "productSpecs.displayMaterial") {
        updateSpecOverride("displayMaterial", select.value);
      } else {
        updateScalarOverride("paper", select.value);
      }
    });
    group.append(select);
    controls.append(group);
  }

  // 3. 印刷面数与颜色
  const printings = Array.isArray(adjustable.printings) ? adjustable.printings : [];
  if (printings.length) {
    const group = el("label", "pricing-sim-field");
    group.append(el("span", "pricing-sim-label", "印刷面数 / 印色"));
    const select = el("select", "pricing-sim-select");
    select.setAttribute("aria-label", "印刷面数与颜色试算");
    select.disabled = simState.applying;
    const currentPrinting = effective.printing || "";
    if (!currentPrinting) {
      const placeholder = el("option", "", "请选择印色…");
      placeholder.value = "";
      select.append(placeholder);
    }
    printings.forEach((item) => {
      const opt = el("option", "", item);
      opt.value = item;
      if (item === currentPrinting) opt.selected = true;
      select.append(opt);
    });
    select.addEventListener("change", () => {
      if (select.value) updateScalarOverride("printing", select.value);
    });
    group.append(select);
    controls.append(group);
  }

  // 4. 书刊装订（仅 booklet）
  const bindings = Array.isArray(adjustable.bindings) ? adjustable.bindings : [];
  if (bindings.length) {
    const group = el("div", "pricing-sim-field");
    group.append(el("span", "pricing-sim-label", "书刊装订"));
    const pills = el("div", "pricing-sim-pills");
    pills.setAttribute("role", "group");
    pills.setAttribute("aria-label", "装订方式试算");
    const activeBinding = effective.binding || "";
    bindings.forEach((binding) => {
      const active = activeBinding === binding;
      const btn = el("button", `pricing-sim-pill${active ? " is-active" : ""}`, binding);
      btn.type = "button";
      btn.setAttribute("aria-pressed", String(active));
      btn.disabled = simState.applying;
      btn.addEventListener("click", () => updateScalarOverride("binding", binding));
      pills.append(btn);
    });
    group.append(pills);
    controls.append(group);
  }

  // 5. 品类专属可调驱动（cardCorners / folding / cupVolume / paperParts / boardThickness / boxStructure 等）
  const specOptions = adjustable.specOptions && typeof adjustable.specOptions === "object"
    ? adjustable.specOptions
    : {};
  const specLabels = adjustable.specLabels && typeof adjustable.specLabels === "object"
    ? adjustable.specLabels
    : {};
  const currentSpecs = effective.productSpecs || {};

  Object.entries(specOptions).forEach(([specKey, choices]) => {
    if (!Array.isArray(choices) || !choices.length) return;
    const labelText = specLabels[specKey] || SPEC_FALLBACK_LABELS[specKey] || specKey;
    const currentVal = currentSpecs[specKey] || "";

    // 2~4 项紧凑驱动（非封面纸）使用胶囊切换，其余使用下拉框
    const usePills = choices.length <= 4 && specKey !== "coverPaper";
    if (usePills) {
      const group = el("div", "pricing-sim-field");
      group.append(el("span", "pricing-sim-label", labelText));
      const pills = el("div", "pricing-sim-pills");
      pills.setAttribute("role", "group");
      pills.setAttribute("aria-label", `${labelText}试算`);
      choices.forEach((choice) => {
        const active =
          currentVal === choice || (specKey === "cardCorners" && !currentVal && choice === "直角");
        const btn = el("button", `pricing-sim-pill${active ? " is-active" : ""}`, choice);
        btn.type = "button";
        btn.setAttribute("aria-pressed", String(active));
        btn.disabled = simState.applying;
        btn.addEventListener("click", () => updateSpecOverride(specKey, choice));
        pills.append(btn);
      });
      group.append(pills);
      controls.append(group);
    } else {
      const group = el("label", "pricing-sim-field");
      group.append(el("span", "pricing-sim-label", labelText));
      const select = el("select", "pricing-sim-select");
      select.setAttribute("aria-label", `${labelText}试算`);
      select.disabled = simState.applying;
      if (specKey === "coverPaper") {
        const sameAsInner = el("option", "", "同内页用纸（默认）");
        sameAsInner.value = "";
        if (!currentVal) sameAsInner.selected = true;
        select.append(sameAsInner);
      } else if (!currentVal) {
        const placeholder = el("option", "", `请选择${labelText}…`);
        placeholder.value = "";
        select.append(placeholder);
      }
      choices.forEach((choice) => {
        const opt = el("option", "", choice);
        opt.value = choice;
        const matched =
          specKey === "coverPaper"
            ? normalizeMaterialKey(choice) === normalizeMaterialKey(currentVal)
            : choice === currentVal;
        if (matched && currentVal) opt.selected = true;
        select.append(opt);
      });
      select.addEventListener("change", () => {
        if (specKey === "coverPaper" || select.value) {
          updateSpecOverride(specKey, select.value);
        }
      });
      group.append(select);
      controls.append(group);
    }
  });

  // 6. 表面工艺多选开关药丸（整行铺开）
  const finishings = Array.isArray(adjustable.finishings) ? adjustable.finishings : [];
  if (finishings.length) {
    const group = el("div", "pricing-sim-field pricing-sim-field-wide");
    const labelRow = el("div", "pricing-sim-label-row");
    labelRow.append(
      el("span", "pricing-sim-label", "表面工艺（可多选叠加）"),
      el("span", "pricing-sim-subhint", "点击即时累加或取消工艺")
    );
    group.append(labelRow);

    const pills = el("div", "pricing-sim-pills pricing-sim-finishing-pills");
    pills.setAttribute("role", "group");
    pills.setAttribute("aria-label", "表面工艺多选试算");
    const activeFinishes = new Set(Array.isArray(effective.finishing) ? effective.finishing : []);

    const noneBtn = el(
      "button",
      `pricing-sim-pill pricing-sim-pill-none${activeFinishes.size === 0 ? " is-active" : ""}`,
      "无工艺"
    );
    noneBtn.type = "button";
    noneBtn.setAttribute("aria-pressed", String(activeFinishes.size === 0));
    noneBtn.disabled = simState.applying;
    noneBtn.addEventListener("click", () => updateScalarOverride("finishing", []));
    pills.append(noneBtn);

    finishings.forEach((proc) => {
      const active = activeFinishes.has(proc);
      const btn = el("button", `pricing-sim-pill${active ? " is-active" : ""}`, proc);
      btn.type = "button";
      btn.setAttribute("aria-pressed", String(active));
      btn.disabled = simState.applying;
      btn.addEventListener("click", () => toggleFinishing(proc));
      pills.append(btn);
    });
    group.append(pills);
    controls.append(group);
  }

  return controls;
}

function renderCompareBar(data) {
  const baseline = data.baseline || {};
  const simulated = data.simulated || {};
  const delta = data.delta || {};
  const unit = data.unit || "";

  const wrap = el("div", `pricing-sim-compare${simState.loading ? " is-updating" : ""}`);

  // 左：当前订单基准
  const left = el("div", "pricing-sim-compare-card is-baseline");
  left.append(
    el("span", "pricing-sim-compare-label", "当前订单基准"),
    el(
      "strong",
      "pricing-sim-compare-price",
      baseline.status === "ready" ? baseline.displayRange : baseline.displayRange || "暂不能估算"
    )
  );
  const baseMeta = [];
  if (data.baselineQuantity) baseMeta.push(formatQty(data.baselineQuantity, unit));
  if (delta.unitBaseline) baseMeta.push(`折合 ≈ ¥${delta.unitBaseline}/${unit || "件"}`);
  left.append(el("small", "pricing-sim-compare-meta", baseMeta.join(" · ") || "基线参数待补全"));

  // 中：差额徽章
  const mid = el("div", "pricing-sim-compare-delta");
  if (simState.loading) {
    mid.append(el("span", "pricing-sim-delta-badge is-flat", "试算中…"));
  } else if (delta.comparable) {
    const dir = delta.direction || "flat";
    const diffText =
      delta.midDiff === "0.00"
        ? "差额 ¥0.00"
        : `${delta.midDiff.startsWith("-") ? "-¥" : "+¥"}${delta.midDiff.replace(/^[+-]/, "")}`;
    const pctPart = Number.isFinite(Number(delta.midPct)) && delta.midDiff !== "0.00"
      ? ` (${formatPct(delta.midPct)})`
      : "";
    mid.append(el("span", `pricing-sim-delta-badge is-${dir}`, `${diffText}${pctPart}`));
    if (delta.unitDiffPct !== null && delta.unitDiffPct !== undefined) {
      const uPct = Number(delta.unitDiffPct);
      const uLabel = Math.abs(uPct) < 0.05 ? "单价持平" : `折合单价 ${formatPct(uPct)}`;
      mid.append(el("small", "pricing-sim-delta-unit", uLabel));
    }
  } else {
    const statusLabel = simulated.status === "partial" ? "含未标定项" : "暂无法对比";
    mid.append(el("span", "pricing-sim-delta-badge is-warn", statusLabel));
  }

  // 右：试算示例价
  const right = el("div", "pricing-sim-compare-card is-simulated");
  right.append(
    el("span", "pricing-sim-compare-label", data.hasOverrides ? "试算示例价（已调参数）" : "试算示例价（同基准）"),
    el(
      "strong",
      "pricing-sim-compare-price",
      simulated.status === "ready"
        ? simulated.displayRange
        : simulated.status === "partial"
          ? `已知小计 ¥${simulated.knownSubtotal || "0.00"}（非完整总价）`
          : simulated.displayRange || "暂不能估算"
    )
  );
  const simMeta = [];
  if (data.currentQuantity) simMeta.push(formatQty(data.currentQuantity, unit));
  if (delta.unitSimulated) simMeta.push(`折合 ≈ ¥${delta.unitSimulated}/${unit || "件"}`);
  right.append(el("small", "pricing-sim-compare-meta", simMeta.join(" · ") || "需补齐必要参数"));

  wrap.append(left, mid, right);

  // 若试算降级为 partial / blocked，如实列出未标定或缺失原因
  if (simulated.status !== "ready") {
    const reasons = [
      ...(Array.isArray(simulated.unpriced) ? simulated.unpriced : []),
      ...(Array.isArray(simulated.missing) ? simulated.missing : []),
    ].filter(Boolean);
    if (simulated.reasonDetail) reasons.unshift(simulated.reasonDetail);
    const note = el("div", "pricing-sim-degraded");
    note.setAttribute("role", "status");
    note.textContent = reasons.length
      ? `当前试算组合未得出完整区间：${reasons.slice(0, 4).join("；")}。不伪造参考价。`
      : "当前试算组合缺少必要计价依据，暂不生成完整参考区间。";
    const box = el("div", "pricing-sim-compare-wrap");
    box.append(wrap, note);
    return box;
  }

  return wrap;
}

function renderWhatIfSection(data, context) {
  const section = el("section", "pricing-sim-section");
  const header = el("div", "pricing-sim-section-head");
  header.append(
    el("h4", "pricing-sim-section-title", "工艺 & 材料 What-If 试算"),
    el("span", "pricing-sim-section-hint", "切换参数实时预览本地参考差价")
  );
  section.append(header);

  section.append(renderWhatIfControls(data));
  section.append(renderCompareBar(data));

  // 操作栏：重置试算 + 将试算配置写入订单
  const overrides = simState.overrides;
  const hasLocalChanges = hasAnyOverrides(overrides) || Boolean(data.hasOverrides);
  const writableValues = buildWritableFieldValues(overrides, data);
  const writableCount = Object.keys(writableValues).length;
  const onlyPrintModeChanged = hasLocalChanges && writableCount === 0 && Boolean(overrides.printMode);

  const actionRow = el("div", "pricing-sim-actions");
  const btnGroup = el("div", "pricing-sim-action-buttons");

  const resetBtn = el("button", "secondary-action pricing-sim-reset-btn", "重置试算");
  resetBtn.type = "button";
  resetBtn.disabled = !hasLocalChanges || simState.applying;
  resetBtn.addEventListener("click", () => runWhatIf({}));

  const applyBtn = el(
    "button",
    "primary-action pricing-sim-apply-btn",
    simState.applying ? "正在写入订单…" : "将试算配置写入订单"
  );
  applyBtn.type = "button";
  applyBtn.disabled =
    writableCount === 0 ||
    Boolean(context.archived) ||
    simState.applying ||
    simState.loading ||
    typeof context.onApplyOverrides !== "function";
  applyBtn.addEventListener("click", () => applyOverridesToOrder());

  btnGroup.append(resetBtn, applyBtn);

  const noteText = context.archived
    ? "已归档订单仅支持只读试算，不能写入修改。"
    : onlyPrintModeChanged
      ? "印刷方式（合版/专版/数码）属于工艺方案维度，试算器仅模拟差价；如需切换请在上方方案卡片中选择。"
      : "写入将更新当前订单草稿并使旧确认、旧方案与既有预检结果失效。";
  const applyNote = el("small", "pricing-sim-apply-note", noteText);

  actionRow.append(btnGroup, applyNote);
  section.append(actionRow);

  return section;
}

function paint(container, context) {
  if (!container) return;
  container.replaceChildren();

  const activeOrder = activeOrderFromContext(context);
  const productType = activeOrder?.productType || simState.data?.productType || "";

  const root = el("div", "pricing-sim");
  root.setAttribute("aria-label", "量价拐点与工艺试算器");

  // 顶部栏
  const head = el("div", "pricing-sim-head");
  const headInfo = el("div", "pricing-sim-head-info");
  headInfo.append(el("span", "pricing-sim-eyebrow", "量价模拟 · WHAT-IF 试算"));
  const titleRow = el("div", "pricing-sim-title-row");
  titleRow.append(el("h3", "pricing-sim-title", "数量阶梯与工艺差价试算"));
  if (productType) {
    const qtySummary = simState.data?.baselineQuantity
      ? ` · 基准 ${formatQty(simState.data.baselineQuantity, simState.data.unit)}`
      : "";
    titleRow.append(el("span", "pricing-sim-chip", `${productType}${qtySummary}`));
  }
  headInfo.append(titleRow);

  const headActions = el("div", "pricing-sim-head-actions");
  headActions.append(el("span", "pricing-sim-readonly", "只读模拟 · 不改草稿"));
  const toggleBtn = el(
    "button",
    "secondary-action pricing-sim-toggle",
    simState.collapsed ? "展开量价与工艺试算" : "收起试算器"
  );
  toggleBtn.type = "button";
  toggleBtn.setAttribute("aria-expanded", String(!simState.collapsed));
  toggleBtn.addEventListener("click", () => {
    simState.collapsed = !simState.collapsed;
    if (!simState.collapsed && !simState.data && simState.sessionId && productType) {
      loadInitial(container, context, simState.signature);
      return;
    }
    paint(container, context);
  });
  headActions.append(toggleBtn);
  head.append(headInfo, headActions);
  root.append(head);

  if (simState.collapsed) {
    container.append(root);
    return;
  }

  // 提示消息 / 错误条
  if (simState.notice) {
    const notice = el("div", "pricing-sim-feedback is-ok", simState.notice);
    notice.setAttribute("role", "status");
    root.append(notice);
  }
  if (simState.error) {
    const errBar = el("div", "pricing-sim-feedback is-error");
    errBar.setAttribute("role", "alert");
    errBar.append(el("span", "", simState.error));
    const retryBtn = el("button", "secondary-action pricing-sim-retry-btn", "重试");
    retryBtn.type = "button";
    retryBtn.addEventListener("click", () => {
      if (hasAnyOverrides(simState.overrides)) {
        runWhatIf(simState.overrides);
      } else {
        simState.failedSignature = "";
        loadInitial(container, context, simState.signature);
      }
    });
    errBar.append(retryBtn);
    root.append(errBar);
  }

  if (!productType) {
    root.append(
      el(
        "p",
        "pricing-sim-empty",
        "请先确定印刷品类与基础规格，即可在此比较不同采购数量档的单价变化与工艺差价。"
      )
    );
    root.append(el("p", "pricing-sim-disclaimer", DEFAULT_DISCLAIMER));
    container.append(root);
    return;
  }

  if (!simState.data) {
    if (simState.loading) {
      const loadingBox = el("div", "pricing-sim-loading", "正在加载本地参考阶梯与工艺选项…");
      loadingBox.setAttribute("role", "status");
      root.append(loadingBox);
    } else if (!simState.error) {
      const prompt = el("div", "pricing-sim-idle");
      prompt.append(el("span", "", "进入比较视图后将自动加载量价阶梯与工艺试算。"));
      const loadBtn = el("button", "secondary-action", "立即加载试算");
      loadBtn.type = "button";
      loadBtn.addEventListener("click", () => loadInitial(container, context, simState.signature));
      prompt.append(loadBtn);
      root.append(prompt);
    }
    root.append(el("p", "pricing-sim-disclaimer", DEFAULT_DISCLAIMER));
    container.append(root);
    return;
  }

  const data = simState.data;
  const tiers = Array.isArray(data.tiers) ? data.tiers : [];
  const hasAnyCalculableTier = tiers.some((tier) => tier.status === "ready" || tier.status === "partial");
  const baselineBlocked = data.baseline?.status === "blocked" && !hasAnyCalculableTier;

  if (baselineBlocked) {
    root.append(renderBlockedCard(data));
  } else {
    root.append(renderTiersSection(data));
    root.append(renderWhatIfSection(data, context));
  }

  root.append(
    el("p", "pricing-sim-disclaimer", DEFAULT_DISCLAIMER)
  );
  container.append(root);
}

/**
 * 重置试算器本地状态（切换会话或切换多产品项时调用）。
 */
export function resetSimulatorState() {
  simState.sessionId = "";
  simState.itemKey = "";
  simState.revision = null;
  simState.signature = "";
  simState.baselineData = null;
  simState.data = null;
  simState.overrides = {};
  simState.loading = false;
  simState.applying = false;
  simState.error = "";
  simState.notice = "";
  simState.inflightSignature = "";
  simState.failedSignature = "";
  simState.requestSeq += 1;
}

/**
 * 在指定容器渲染量价拐点模拟器 & 工艺 What-If 试算器。
 * 视图自动初始化仅发 GET（context.fetchInitial），绝不自动发 POST。
 */
export function renderPricingSimulator(container, context) {
  if (!container) return;
  const ctx = context && typeof context === "object" ? context : {};
  const sessionId = String(ctx.sessionId || "");
  const itemKey = resolveItemKey(ctx);
  const revision = ctx.revision ?? ctx.state?.revision ?? null;
  const signature = `${sessionId}|${itemKey}|${revision}`;

  const sessionOrItemChanged =
    sessionId !== simState.sessionId || itemKey !== simState.itemKey;
  const revisionChanged = signature !== simState.signature;

  if (sessionOrItemChanged || revisionChanged) {
    simState.sessionId = sessionId;
    simState.itemKey = itemKey;
    simState.revision = revision;
    simState.signature = signature;
    simState.baselineData = null;
    simState.data = null;
    simState.overrides = {};
    simState.loading = false;
    simState.applying = false;
    simState.error = "";
    if (sessionOrItemChanged) simState.notice = "";
    simState.inflightSignature = "";
    simState.failedSignature = "";
    simState.requestSeq += 1;
  }

  simState.lastContainer = container;
  simState.lastContext = ctx;

  paint(container, ctx);

  const activeOrder = activeOrderFromContext(ctx);
  const hasProduct = Boolean(activeOrder && activeOrder.productType);
  const shouldFetch =
    !simState.collapsed &&
    Boolean(sessionId) &&
    hasProduct &&
    !simState.data &&
    simState.inflightSignature !== signature &&
    simState.failedSignature !== signature &&
    isContainerVisible(container);

  if (shouldFetch) {
    loadInitial(container, ctx, signature);
  }
}
