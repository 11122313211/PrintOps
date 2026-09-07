# 路径二：前端模块化与定向渲染（path-02-frontend-modularization）

> 优先级 P0　目标版本 v0.12.0　预估工作量 4-5 人日　依赖：无；是路径 7（UI 深化）的强制前提

## 1. 现状与问题

### 1.1 当前行为（已核实）

- `app.js` 单文件 1,848 行，约 70 个顶层函数、约 15 个模块级可变全局（`state`、`sessionId`、`layout`、`llmSettings`、`isSending`、`chatRequestSeq`、`activeChatController`、`suppressOrderDiff`、`fileFeedback`、`pdfPreview` 等，app.js:52-74 一带）；
- `render(data)` 每次响应全量调用 `renderDraft / renderValidation / renderFileState / renderQuickReplies / renderOptions / renderTools / renderDecision / renderQuoteStatus / renderRunTrace / renderProgress / renderOrderHistory / renderSpecPresets` 约 12 个面板函数，并按 `messages` 数组重放消息；
- 输入框类元素随面板重建，`change` 事件驱动——响应到达时若用户正在 inline 编辑某字段，重建会丢焦点与未提交输入；
- 顶层事件绑定假设 index.html 中每个 ID 存在，ID 不匹配在加载时抛错（无防御）；
- 零测试、零 lint、无模块边界。

### 1.2 问题的影响

- **迭代速度**：任何 UI 改动都在 1,848 行里进行，回归面等于全部功能面；路径 7 的每一项 UI 优化都会被此放大风险；
- **正确性**：全量重绘造成焦点丢失、选中态闪跳、滚动位置漂移这类"说不上来但很难受"的问题；
- **可观测性**：状态散在全局变量里，没有单一事实源，新增功能容易引入状态不同步。

## 2. 目标与非目标

### 2.1 目标

1. `app.js` 拆分为原生 ES Modules 多文件（`type="module"`，无构建步骤，保持零依赖特色），单文件不超过 400 行；
2. 单一状态源：一个 `state` store + 显式订阅/分发，全局可变量收敛到 store 内；
3. 定向渲染：每次响应只重绘数据真正变化的面板（单帧面板更新数 ≤3 为性能预算）；
4. 焦点保持：任何面板重绘不中断正在进行的文本输入；
5. 建立 Playwright 冒烟测试（4 条路径）并接入 CI，弥补前端零测试。

### 2.2 非目标

- 不引入前端框架/构建工具/打包器；
- 不改任何视觉设计（styles.css 令牌体系不动）；
- 不在本路径内做新功能（迁移期冻结功能变更，见 5.3）。

## 3. 方案设计

### 3.1 目标文件结构

```
app/
  main.js            # 入口：初始化、bootstrap、事件绑定（原 app.js 尾部）
  api.js             # api()/apiStream()/apiErrorText()（与路径一对齐）
  store.js           # state 单例 + update(patch) + subscribe(panel, selector)
  render/
    chat.js          # 消息流（唯一支持追加渲染的面板）
    draft.js         # 订单草稿与字段编辑
    options.js       # 方案卡片与对比表
    panels.js        # 校验/文件/工具/决策/轨迹/进度等小面板
    history.js       # 订单历史与规格预设
  features/
    multi-product.js # 多产品项展开/切换
    preflight.js     # PDF 预检（含路径 6 将扩展的批量逻辑挂点）
    settings.js      # 接口设置对话框
    quote.js         # 询价状态/刷新/取消
  utils.js           # 格式化、本地存储、防抖
index.html           # <script type="module" src="app/main.js">
```

旧 `app.js` 在迁移期保留为兼容入口并逐步清空，迁移完成后的版本删除。

### 3.2 状态管理设计

`store.js` 采用最小可行的发布-订阅（不引库）：

```js
const state = Object.freeze({ ...initialState });
const listeners = new Map();          // panel -> Set<selector>
export function getState() { return state; }
export function update(patch) { /* 浅合并 + 通知 selector 结果变化的订阅者 */ }
export function subscribe(panel, selector) { /* 返回取消函数 */ }
```

规则：

- 面板渲染函数只读 `getState()` + 自身 props，不写全局；
- 副作用（API 调用、localStorage）只允许在 `features/` 与 `main.js` 中发生；
- `chatRequestSeq`/`activeChatController` 归入 store 的 `transient` 子树（不触发面板渲染）。

### 3.3 定向渲染协议

服务端每次响应的数据天然带有领域分区。渲染侧建立"响应分区 → 面板"映射：

| 响应字段变化 | 需重绘的面板 |
| --- | --- |
| messages | chat（追加式，不重建历史节点） |
| order | draft、options（含 selectedOption 失效）、multi-product |
| validation/readiness/missingFields | panels.validation、progress |
| itemOptions / selectedOption | options、quote |
| quoteRequests / activeQuoteRequestId | quote |
| runHistory / toolTrace | panels.trace、decision |
| history / specPresets | history |
| settings/llm | settings 按钮态 |

实现：`update(patch)` 对比新旧分区（浅比较 + 关键子键比较），把脏面板收集为 Set，微任务（`queueMicrotask`）合并后统一执行渲染，单帧内重复标记只渲染一次。消息流例外：永远追加渲染（`addMessage` 已是增量语义），不随分区比较。

### 3.4 焦点保持策略

- 渲染前检查 `document.activeElement` 是否落在将被重建的容器内；若是，跳过该容器本轮重绘并把待渲染内容记入 pending，待 `blur`/提交后执行；
- inline 编辑提交（`change`）后主动触发对应面板的定向刷新；
- 焦点恢复：容器重建前后记录 `activeElement` 的 `data-field` 定位，重建后恢复焦点（无匹配则不恢复）。

### 3.5 index.html 配套改动

- `<script type="module" src="app/main.js"></script>`；旧的 `?v=` 缓存参数可移除（服务端已 `Cache-Control: no-store`）；
- 静态白名单（server.py `STATIC_FILES`）需要允许 `app/` 目录下的模块文件——白名单从单文件集合改为"目录白名单 + 后缀白名单"（`.js/.css`，且仅 `app/` 与根目录白名单文件），这是本路径唯一涉及服务器的改动，必须同步扩展 `test_http_handler.py` 的白名单用例（确认 `/.git`、`/server.py`、`/data` 仍 404，新增 `/app/store.js` 200 与 `/app/../server.py` 归一化后 404 的用例）。

## 4. 接口与数据契约

- 面向服务端的契约完全不变（API、字段、错误结构零改动）；
- 面向浏览器的新契约：模块路径与导出清单（上表）是内部契约，写入本文件并随版本维护；
- localStorage 键保持兼容：`printops_session`、`printops_layout`、`printops_sidebar`、`printops_contrast`、`printops_presets` 语义与格式不变（老用户刷新即恢复）。

## 5. 分阶段实施步骤（渐进迁移，每步可发布）

| 阶段 | 内容 | 回归口径 | 预估 |
| --- | --- | --- | --- |
| A | 建立 `app/` 骨架 + store + utils；先迁移两个低风险面板（runTrace、quoteStatus）验证订阅机制 | 手动回归这两面板 + 全量单测 | 1 天 |
| B | 迁移 panels.js（校验/文件/工具/决策/进度）；引入定向渲染协议（先对这批面板生效） | 手动矩阵 + 单测 | 1 天 |
| C | 迁移 history/specPresets/settings；`api()` 移入 api.js | 同上 | 0.5 天 |
| D | 迁移 options.js（方案卡片与对比表，含键盘可达性逻辑原样搬运） | 手动：方案选择/失效/对比表 | 1 天 |
| E | 迁移 draft.js 与多产品/预检/询价 features；实现焦点保持 | 手动：inline 编辑不丢焦点 | 1 天 |
| F | chat 追加式渲染改造（不随 messages 数组重放）；删除旧 app.js；接入 Playwright 4 条冒烟进 CI | 冒烟 + 全量单测 + 走查脚本自测 | 1 天 |

### 5.1 Playwright 冒烟脚本定义（F 阶段交付）

1. `smoke-first-chat.spec.js`：打开首页 → 发送标准画册话术 → 断言草稿出现五个字段、方案卡片三张；
2. `smoke-multi-product.spec.js`：发送双产品话术 → 点击侧栏第二项 → 修改数量 → 断言第一项数量未被污染；
3. `smoke-error-recovery.spec.js`：拦截 `/api/chat` 返回 500 → 断言错误气泡带 requestId → 恢复后重发成功；
4. `smoke-refresh-restore.spec.js`：完整一轮对话后 `page.reload()` → 断言会话、消息、方案选择恢复。

dev 依赖仅 Playwright（npm 独立目录 `e2e/`，不进运行链路；README 注明"运行时零依赖不变"）。

## 6. 测试与验收

- 每阶段结束跑：149+ 单测全绿、`node --check` 全部模块、手动回归清单（对应面板）；
- 性能预算：用 Performance 面板验证一次典型响应（含方案）重绘面板数 ≤3；
- 焦点保持验收：inline 编辑"纸张"字段 → 另一浏览器标签触发会话事件（或直接再发一条消息）→ 焦点与未提交输入保持；
- 刷新恢复：localStorage 全键兼容验证（v0.10 用户升级场景）；
- 白名单扩展后 `test_http_handler.py` 更新并全绿。

## 7. 风险与对策

| 风险 | 概率 | 对策 |
| --- | --- | --- |
| 迁移期引入行为回归 | 中 | 渐进迁移每步可发布；迁移期冻结功能改动；每步全量回归 |
| 循环依赖（features ↔ store ↔ render） | 中 | 依赖方向强制单向：render → store；features → (store, api)；utils 不依赖任何上层；`node --check` + 简单 import 检查脚本 |
| 模块化后请求竞态被破坏 | 低 | `chatRequestSeq`/AbortController 语义原样迁入 store.transient；冒烟用例 3 覆盖 |
| 双入口期旧 app.js 与新 app/ 并存造成混乱 | 中 | A-F 每阶段旧入口同时加载新模块的开关式共存，F 阶段一次性删除旧文件并更新 CHANGELOG |
| Playwright 在 Windows CI 的浏览器下载慢 | 低 | CI 缓存浏览器二进制；失败允许 retry 一次但失败即红 |

## 8. 工作量与验收门禁

4-5 人日（含 Playwright 接入）。门禁：拆分后单文件 ≤400 行、全局可变量 ≤3 个（store 内部除外）、冒烟 4 条全绿、性能预算达标、CHANGELOG 记录文件结构变化与缓存策略变化。

## 9. 依赖与后续

- 完成后立即解锁路径 7（UI 深化的全部条目都建立在定向渲染与新模块边界上）；
- 路径 1（流式）的 `apiStream` 落位在本路径的 `api.js`——建议路径 1 的阶段 C 与本路径阶段 C 协调排期，避免同一文件两次大改；
- 实施记录：〔待填〕

## 10. 任务卡分解

| 卡号 | 阶段 | 任务 | 改动文件 | 验收标准 | 预估 |
| --- | --- | --- | --- | --- | --- |
| S2-01 | A | `app/` 骨架 + index.html 改 `type=module` + 服务端目录白名单扩展 | index.html、server.py、test_http_handler.py | 白名单新用例 4 条全绿；页面加载无 404 | 0.25d |
| S2-02 | A | store.js（update/subscribe/selector 浅比较 + transient 子树） | app/store.js、app/utils.js | 单测（node --experimental 或 Playwright 内嵌页测试）订阅触发次数正确 | 0.5d |
| S2-03 | A | 迁移 runTrace、quoteStatus 两面板 | app/render/panels.js、app/render/quote.js | 手动回归：询价状态/刷新/取消全链路 | 0.25d |
| S2-04 | B | 定向渲染协议上线（先覆盖 panels 批）：分区→面板映射表 + 微任务合并 | app/store.js | 注入响应后单帧重绘面板数 ≤3（Performance 验证） | 0.5d |
| S2-05 | B | panels.js 全量（校验/文件/工具/决策/进度） | app/render/panels.js | 手动矩阵八项通过 | 0.5d |
| S2-06 | C | api.js 迁移（含 401/429 处理、requestId 提取） | app/api.js | api 错误路径矩阵（400/401/403/413/429/500/断网）手动通过 | 0.25d |
| S2-07 | C | history/specPresets/settings 迁移 | app/render/history.js、app/features/settings.js | localStorage 全键兼容验证 | 0.25d |
| S2-08 | D | options.js（卡片+对比表+键盘可达） | app/render/options.js | 方案选择/失效重选/对比表交互回归 | 1d |
| S2-09 | E | draft.js + 焦点保持 | app/render/draft.js | inline 编辑中触发响应：焦点与输入保持 | 0.5d |
| S2-10 | E | multi-product/preflight/quote features 迁移 | app/features/*.js | 直切前既有行为不变（聊天话术切换仍可用） | 0.5d |
| S2-11 | F | chat 追加式渲染（messages 不重放，增量 append + 会话切换全量重建） | app/render/chat.js | 会话切换历史完整；单条新消息只追加一次 | 0.5d |
| S2-12 | F | 删除旧 app.js + 全局变量清点 ≤3 | 根目录 | grep 全局赋值清点通过；node --check 全绿 | 0.25d |
| S2-13 | F | Playwright 4 条冒烟 + CI 接入 | e2e/*.spec.js、.github/workflows/ci.yml | 冒烟在 ubuntu+windows CI 全绿 | 0.5d |

## 11. store.js 参考实现（骨架）

```js
const listeners = new Map();            // panel -> [{selector, last, callback}]
let state = Object.freeze(initialState());

export function getState() { return state; }

export function update(patch) {
  const next = Object.freeze({ ...state, ...patch });
  const dirty = new Set();
  for (const [panel, subs] of listeners) {
    for (const sub of subs) {
      const value = sub.selector(next);
      if (!shallowEqual(value, sub.last)) { sub.last = value; dirty.add(sub); }
    }
  }
  state = next;
  if (dirty.size) queueMicrotask(() => {
    const batch = new Map();            // panel -> [subs]，单帧合并
    for (const sub of dirty) batch.set(sub.panel, [...(batch.get(sub.panel) || []), sub]);
    for (const [panel, subs] of batch) panel(subs.map(s => s.last));
  });
}
```

规则：渲染函数签名统一 `renderPanel(selectedValues)`；`transient` 子树（seq/controller）不注册订阅；跨面板一致性（如 selectedOption 同时影响 options 与 quote）由"两个订阅、同一响应"天然保证。

## 12. 详细测试矩阵

| 层 | 用例组 | 断言要点 | 卡号 |
| --- | --- | --- | --- |
| 白名单 | 目录白名单 | `/app/store.js` 200；`/app/../server.py` 归一化 404；`/app/x.jsx` 404 | S2-01 |
| 单测 | store | 同值不触发；异值触发一次；微任务合并单帧 | S2-02 |
| Playwright | 首次对话 | 发送→五字段草稿+三方案卡 | S2-13 |
| Playwright | 多产品 | 直切前：聊天话术切换→隔离断言 | S2-10/13 |
| Playwright | 错误恢复 | 500→错误气泡含 requestId→恢复重发 | S2-13 |
| Playwright | 刷新恢复 | reload→会话/消息/方案选择恢复 | S2-07/13 |
| 手动 | 焦点保持 | 编辑"纸张"时注入响应：焦点与输入保持 | S2-09 |
| 手动 | localStorage | v0.10 旧键全部生效 | S2-07 |
| 性能 | 渲染预算 | 典型响应 Performance 面板重绘 ≤3 面板 | S2-04 |

## 13. 关键实现草图：面板注册表与定向渲染

§11 给出了 store 骨架，本节补齐"渲染层怎么改"的落地图。定向渲染的核心不是重写
渲染函数，而是给现有 11 个 `renderXxx` 套上**订阅 + 脏标记**的外壳，让迁移可以
一 panel 一 PR 地推进——迁移期任何时刻全量渲染与定向渲染都是等价可回退的。

### 13.1 面板注册表（registry）

```js
// render-registry.js —— 每个面板一行：依赖的状态切片 + 渲染函数
export const PANELS = [
  { name: "draft",       slice: ["order", "fieldMeta", "activeItemIndex"], fn: renderDraft },
  { name: "validation",  slice: ["validation", "order"],                   fn: renderValidation },
  { name: "fileState",   slice: ["fileFeedback", "order"],                 fn: renderFileState },
  { name: "quickReplies",slice: ["quickReplies"],                          fn: renderQuickReplies },
  { name: "options",     slice: ["options", "selectedOption", "order"],    fn: renderOptions },
  { name: "tools",       slice: ["availableTools"],                        fn: renderTools },
  { name: "decision",    slice: ["nextAction", "stage", "runTrace"],       fn: renderDecision },
  { name: "quoteStatus", slice: ["quoteRequest", "quoteRequests"],         fn: renderQuoteStatus },
  { name: "runTrace",    slice: ["runTrace"],                              fn: renderRunTrace },
  { name: "progress",    slice: ["order", "validation"],                   fn: renderProgress },
  { name: "orderHistory",slice: ["order", "itemOptions"],                  fn: renderOrderHistory },
  { name: "specPresets", slice: ["order"],                                 fn: renderSpecPresets },
];
```

### 13.2 定向渲染主循环

```js
let lastSnapshot = {};                     // name → 该面板切片的稳定哈希
export function renderPanels(state) {
  let updated = 0;
  for (const panel of PANELS) {
    const snap = hashSlice(state, panel.slice);
    if (snap === lastSnapshot[panel.name]) continue;   // 未变化：跳过
    panel.fn(state);
    lastSnapshot[panel.name] = snap;
    updated += 1;
    if (updated >= 3) queueMicrotask(() => renderPanels(state)); // 预算保护：超 3 面板让出主线程
  }
  metricsEvent("render_batch", { panels_updated: updated });        // 附录 D 埋点
}
```

要点：`hashSlice` 用结构化取值 + `JSON.stringify`（切片都很小，性能足够）；预算
保护让最坏情况退化为"分帧的全量渲染"，保证 60fps 不被单帧大重绘击穿——这与
C2 指标（单帧 ≤3 面板）直接对应。

### 13.3 焦点保持（迁移期的硬约束）

1. `renderDraft` 在重绘前记录 `document.activeElement` 的 `data-focus-key`（每个
   inline 输入框在注册时打上唯一键），重绘后若同键元素存在则恢复焦点与选区；
2. 输入中的字段（`document.activeElement` 处于输入中且值未提交）所在面板**跳过本
   轮重绘**（脏标记挂起到 blur）——宁肯晚一帧更新，不打断输入；
3. 焦点行为在 Playwright 冒烟中固化：inline 编辑 → 模拟响应到达 → 断言焦点仍在
   原输入框且光标位置未变（对应 S2-08）。

### 13.4 模块拆分的物理布局（S2-01 执行依据）

```
app/            ← 新目录，index.html 以 <script type="module" src="app/main.js"> 引入
├── main.js         启动装配：bootstrap → store.init → renderPanels → 事件绑定
├── api.js          api()/apiErrorText/token 头（现 app.js:380-400）
├── store.js        §11 骨架：state 容器、set()/subscribe()、localStorage 持久化
├── render-*.js     每面板一个文件（registry 逐行点亮）
├── events.js       全部 addEventListener 装配（现 app.js 1450+ 行的事件区）
├── pdf.js          浏览器本地 PDF 元数据解析（自包含，最先拆出）
└── i18n.js         路径 10 的 t() 入口（占位，先导出恒等函数）
```

迁移规则：旧 `app.js` 保留为兼容入口（仅转发到 `app/main.js`），直到 S2-13 验收后
删除；`styles.css` 与 id 体系不动——拆分只动 JS 组织，不动 DOM 契约，保证每一步
可独立回退。

### 13.5 迁移期回归门禁

每张 S2 卡合并前必须通过四件套：149+ 单测全绿（不变）、Playwright 四条冒烟、
手工双清单（首轮对话 + 多产品切换）、C2 预算抽测。任何一张卡引入功能改动即违反
"迁移期禁止功能改动"纪律，评审直接退回。
