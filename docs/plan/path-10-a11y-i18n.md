# 路径十：可访问性、国际化与多端适配（path-10-a11y-i18n）

> 优先级 P2　目标版本：持续增强（首期 3 人日随 v0.13.0）　依赖：路径 2（模块化后文案与渲染职责清晰才好抽取）

## 1. 现状与问题

### 1.1 可访问性现状

已具备的基础（v0.10.0 起建立）：聊天流 `aria-live="polite"`、方案卡片 `role="button"` + Enter/Space、发送按钮 `aria-busy`、分隔条 `role="separator"` 键盘调节、输入框 sr-only 标签、toast `role="status"`、高对比度开关（持久化）。

已知的缺口：

1. **低置信度警告**依赖颜色 + title（路径 7 条目 3 解决主体，本路径负责审计兜底）；
2. **焦点管理**：对话框关闭后焦点回落未显式处理；面板重绘后焦点可能落在 body（路径 2 的焦点保持覆盖大部分）；
3. **动效无 reduced-motion 适配**：字段更新高亮渐隐、抽屉滑入等动画在系统"减少动态效果"设置下仍播放；
4. **对比度令牌未做 AA 全量审计**：高对比度模式之外，默认主题的部分次级文本（约 11px 的辅助行）对比度未必达标；
5. 表格类（方案对比表）在键盘下的导航顺序未经梳理。

### 1.2 国际化现状

- 全部文案硬编码中文（Python 端：LABELS、错误消息、quick replies、handoff 文本；前端：标签、toast、状态行）；
- `order_model.LABELS` / `DIMENSION_LABELS` 与前端 `labels`/`dimensionLabels` 双份维护（前后端重复定义，v0.11.0 清理时已知）；
- 无语言切换概念；数量单位（张/份/个）等语言强绑定逻辑渗透在规则里。

### 1.3 多端现状

- ≤1050px 抽屉模式、≤420px 顶栏收缩已处理；
- 平板中间宽度（750-1050px）的三栏挤压未专门走查；
- 触屏：拖拽分隔条基于 Pointer Events（已兼容），但 44px 触控目标未系统核查。

## 2. 目标与非目标

### 2.1 目标

1. **WCAG 2.1 AA 审计通过**（自动扫描 + 人工清单），形成基线并纳入发布检查；
2. `prefers-reduced-motion` 全量适配；
3. **i18n 地基**：文案集中于单一字典（前端 `i18n/zh.js`；后端复用 LABELS 体系扩展），建立 `t(key)` 取词习惯，为 en 语包铺路；
4. **英文语包首版**（界面骨架级：导航、按钮、状态行、错误码文案；对话内容与知识性文案暂不翻译——Agent 生成内容是另一回事）；
5. 平板宽度与触控目标走查修复。

### 2.2 非目标

- 不做 Agent 对话内容的多语言生成（那是模型能力问题，不是 UI i18n 问题；提示词可按语言切换列为远期）；
- 不做 RTL 语言；
- 不做自动翻译。

## 3. 方案设计

### 3.1 i18n 地基（关键决策：只抽"界面文案"，不抽"领域内容"）

- **界面文案**（按钮/标题/状态/toast/错误码）：抽到字典。前端 `app/i18n/zh.js` 导出 `zh` 字典与 `t(key, params?)`；语言选择存 localStorage（默认跟随 `navigator.language`，首版只有 zh，en 上线后可选）；
- **领域内容不抽**：品类名、工艺名、知识解释、handoff 正文保持中文（数据即内容；这些内容的多语言 = 知识库多语言，是独立的重量级工程，明确排除）；
- **后端错误消息**：错误码是契约（`INVALID_SESSION` 等），`message` 保持中文现状；前端对**已知错误码**用本地字典覆盖显示（message 仅作后备）——这样语言切换对常见错误即时生效，后端零改动；
- **前后端标签重复**（`labels` 双份维护）：顺带修复——后端 `/api/products` 已返回知识结构，新增 `/api/meta/labels` 或在现有响应附带 LABELS，前端删除手抄副本（这是一个真实的数据一致性修复，不只是 i18n）。

### 3.2 en 语包首版范围（约 60-80 键）

导航与标题、按钮动词、状态行、错误码映射、设置对话框、进度条七阶段名、打印交接单固定段落标题。**不含**：quick replies、品类字典、示例话术（这些与领域耦合，en 版用户直接用中文对话可接受，文档说明）。

### 3.3 WCAG AA 审计清单（人工 + 自动结合）

自动（axe-core via Playwright，dev 依赖）：对比度、aria 属性合法性、表单标签、标题层级。

人工清单（一次性建 `docs/a11y-audit.md`，此后每版本核对增量）：

- 全键盘路径：Tab 顺序覆盖所有可操作控件、无键盘陷阱（对话框 Esc 已有）；
- 焦点可见：`focus-visible` 样式令牌统一；
- 表格键盘导航：对比表按行进出的顺序；
- 动效：`@media (prefers-reduced-motion: reduce)` 关闭渐隐/滑入；
- 文本缩放 200% 布局不破；
- 屏幕阅读器语义抽查：进度条 aria-valuetext、徽标 role、消息流 live region 行为。

### 3.4 平板与触控

- 750-1050px：三栏最小宽度与抽屉触发阈值重新走查（当前 DESKTOP_QUERY 断点单一），决策：960px 以下即抽屉，避免中间挤压态；
- 触控目标：分隔条把手、折叠按钮、徽标关闭热区 ≥44×44（视觉不变，扩大命中区）；
- 触屏长按选中与拖拽分隔条的冲突测试（Pointer Events 已有，补 touch-action 复核）。

## 4. 接口与数据契约

- `/api/meta/labels`（或并入既有响应）：LABELS/DIMENSION_LABELS 单一事实源下发给前端；
- 语言切换键：`localStorage.printops_lang`（`zh` | `en`）；
- i18n 字典键命名规范：`域.对象.属性`（如 `nav.title`、`err.INVALID_SESSION`、`stage.recommend`），登记于 `app/i18n/README.md`；
- 错误码 → en 文案映射表是前端字典的一部分，后端错误码清单是唯一来源（tools/server 定义处加注释指向）。

## 5. 分阶段实施步骤

| 阶段 | 内容 | 预估 |
| --- | --- | --- |
| A | 后端 labels 下发 + 前端删除双份副本（数据一致性修复，独立可发布） | 0.5 天 |
| B | 前端 `t()` 地基 + 现有界面文案抽取为 zh 字典（纯重构，行为不变） | 1 天 |
| C | en 骨架语包 + 语言切换入口（设置对话框内） | 1 天 |
| D | axe-core 扫描接入 Playwright + 修复自动项；reduced-motion 适配 | 0.5 天 |
| E | 人工 a11y 清单首轮 + 平板/触控修复 + `docs/a11y-audit.md` 建档 | 0.5 天 |

## 6. 测试与验收

- 回归口径：抽字典前后 UI 快照（视觉不变）；切 en 后骨架界面全英文、对话流仍中文（明确预期）；
- axe 扫描进 CI（serious/critical 违例即红）；
- 键盘全路径人工验收一次并记录于 a11y-audit.md；
- 缩放 200% 与 960px 断点截图存档。

## 7. 风险与对策

| 风险 | 概率 | 对策 |
| --- | --- | --- |
| 文案抽取遗漏导致双语混杂 | 高（首版必然） | en 语包只承诺骨架级；遗漏键回退 zh 并在开发模式 console.warn 未覆盖键 |
| 界面语言切换与 Agent 中文回复的错位感 | 中 | 设置面板明示"界面语言 ≠ 对话语言"；对话语言由模型配置决定（远期项） |
| 后端标签下发破坏既有响应消费方 | 低 | 新键追加而非改造既有响应结构 |
| axe 扫描的误报噪音 | 中 | CI 只卡 serious/critical；moderate 进周清列表 |

## 8. 工作量与验收门禁

首期 3 人日。门禁：axe serious/critical 清零、双语骨架切换可用、a11y-audit.md 基线建档、LABELS 双份副本消除。

## 9. 依赖与后续

- 依赖路径 2（模块化）与路径 8（`/api/health` 版本键，用于语言包版本对齐）；
- 远期（v1.2+）：对话语言切换（提示词按语言注入）、handoff 模板多语言、知识库 en 版；
- 实施记录：〔待填〕

## 10. 任务卡分解

| 卡号 | 阶段 | 任务 | 改动文件 | 验收标准 | 预估 |
| --- | --- | --- | --- | --- | --- |
| S10-01 | A | `/api/meta/labels` 下发 LABELS/DIMENSION_LABELS | server.py、agent.py（或 order_model.py） | 契约测试；前端删除手抄副本后草稿渲染不变 | 0.25d |
| S10-02 | A | 前端删除 labels/dimensionLabels 双份副本，改消费接口 | app.js（迁移后 render/draft.js） | grep 无本地副本；视觉零变化 | 0.25d |
| S10-03 | B | `t(key, params)` 地基 + zh 字典抽取（约 90 键：导航/按钮/状态/toast/阶段） | app/i18n/zh.js、app/i18n/README.md | 抽取前后 UI 快照一致；开发模式无未覆盖键告警 | 1d |
| S10-04 | B | 已知错误码本地文案映射（INVALID_SESSION 等 12 个） | app/i18n/zh.js | 错误码→文案映射表与后端定义处注释互指 | 0.25d |
| S10-05 | C | en 骨架语包（60-80 键）+ 设置对话框语言切换 + `printops_lang` | app/i18n/en.js、settings | 切 en：骨架全英文、对话流仍中文（明示预期） | 1d |
| S10-06 | C | axe-core 接入 Playwright（serious/critical 即红） | e2e/、ci.yml | 首轮扫描修复后 CI 全绿 | 0.5d |
| S10-07 | D | `prefers-reduced-motion` 全量适配（渐隐/滑入/拖拽动效） | styles.css | 系统开关下动画关闭截图 | 0.25d |
| S10-08 | D | 对比度审计修复（次级文本 11px 行达标 AA） | styles.css 令牌 | axe 对比度规则零违例 | 0.5d |
| S10-09 | E | 人工 a11y 清单首轮（键盘全路径/焦点可见/表格导航/缩放 200%）+ `docs/a11y-audit.md` 建档 | 文档 | 清单全部勾选或登记为待办 | 0.5d |
| S10-10 | E | 平板断点调整（960px 以下抽屉）+ 触控目标 ≥44px 命中区 | styles.css、app.js | 768px/960px 截图走查；触控热区清单 | 0.5d |

## 11. i18n 键命名规范与首批键分布

命名规范：`域.对象[.属性]`，登记于 `app/i18n/README.md`；新增界面文案必须先登记键再使用。

| 域 | 键数（首批） | 示例 |
| --- | --- | --- |
| nav.* | 8 | nav.title、nav.tools、nav.orders、nav.settings |
| composer.* | 6 | composer.placeholder、composer.send、composer.stop |
| draft.* | 14 | draft.required、draft.optional、draft.confirmed、draft.pendingConfirm |
| options.* | 9 | options.economy、options.balanced、options.premium、options.refPrice |
| stage.* | 7 | stage.collect…stage.export（与 workflowStage 键一一对应） |
| quote.* | 8 | quote.awaiting、quote.confirmed、quote.stale、quote.cancel |
| err.* | 12 | err.INVALID_SESSION、err.PAYLOAD_TOO_LARGE、err.RATE_LIMITED |
| toast.* | 10 | toast.saved、toast.reset、toast.streamFallback |
| print.* | 8 | print.title、print.disclaimer、print.confirmation |
| 合计 | ≈82 | — |

axe 首轮扫描规则集：color-contrast、aria-valid-attr、aria-roles、label、button-name、html-has-lang、image-alt、duplicate-id（余项进周清列表）。

## 12. 详细测试矩阵

| 层 | 用例组 | 断言要点 | 卡号 |
| --- | --- | --- | --- |
| 契约 | labels 下发 | 与 order_model 常量逐键相等 | S10-01 |
| Playwright | 视觉不变 | 抽取前后关键面板 DOM 快照一致 | S10-02/03 |
| Playwright | 语言切换 | en 下导航/按钮/错误提示英文；对话流中文 | S10-05 |
| 单测 | 字典完备 | en/zh 键集合一致或登记豁免；t() 未知键 console.warn | S10-05 |
| axe | CI 门禁 | serious/critical = 0 | S10-06 |
| 手动 | reduced-motion | 系统开启后无位移动画 | S10-07 |
| 手动 | 缩放 200% | 布局无横向溢出、无遮挡 | S10-09 |
| 手动 | 键盘全路径 | Tab 顺序覆盖全部可操作控件、无陷阱 | S10-09 |
| 手动 | 触控 | 分隔条/按钮 44px 热区 | S10-10 |

## 13. i18n 抽取脚本设计与伪本地化测试

§11 定义了键命名规范，本节定义**怎么把 1,848 行 app.js 里的中文字面量迁移到键**，
以及怎么防止"语包漏键、硬编码回潮"。i18n 最常见的失败方式不是方案错，而是第二语言
包上线三个月后新代码又开始直接写中文——必须用脚本和测试把纪律固化。

### 13.1 抽取脚本（开发期工具，标准库实现）

`tools/i18n_extract.py`（S10-04 的实现体）：

1. **扫描**：对 `app.js` 按行正则抽取中文字面量（模板串、字符串字面量、模板插值中
   的中文段），输出 `键候选表`：行号、原文、上下文函数名、建议键名（按 §11 命名
   空间：`chat.* / draft.* / options.* / settings.* / toast.* / a11y.*`）。
2. **比对**：与现有 `zh.js` 比对，已有键跳过，新键标 `NEW`，疑似重复（同文不同键）
   标 `DUP` 供人工合并；孤儿键（zh.js 有、代码无引用）标 `ORPHAN`。
3. **产出**：`data/i18n-report.md`——人不是从零翻译，而是在报告上确认键名、改写
   变量插值（`数量改为 {value} 张` 形态），确认后由脚本生成 `zh.js` 增量段落。
   脚本只写报告与增量段，**不直接改写 app.js**——字面量到键的替换由人工逐段完成，
   避免脚本误伤模板串里的印刷术语。

### 13.2 语包文件结构与加载

```
lang/zh.js    → window.PRINTOPS_I18N = { "meta.version": "2026.09-1", "chat.placeholder": "...", ... }
lang/en.js    → 同键集（缺失键回退 zh，允许渐进翻译）
```

- `app.js` 顶部新增 `t(key, params)`：查 `PRINTOPS_I18N`，缺失回退 zh 包，再缺失
  回退键名本身并 console.warn（开发期可见，生产静默）；
- 插值统一 `{name}` 占位符，禁止字符串拼接造句（英词语序不同，拼接必错）；
- 服务端返回的中文（校验消息、方案说明、交接文本）**首期不进语包**：这些文本来自
  知识层，与 knowledge version 绑定，en 化属于路径 3 之后的知识层工程——`Accept-Language`
  检测只切 UI 层，避免出现"半英文订单"的更差体验；该限制写入 README 已知限制。

### 13.3 伪本地化（pseudo-i18n）测试

每版本 CI 跑一次伪本地化构建：`lang/pseudo.js` 把所有键值替换为 `[键名▸值◂]` 形态，
Playwright 冒烟在伪包下走一遍首聊路径，断言：①界面上不再出现任何裸中文正则命中
（抽查 20 个高危面板元素）；②无 `[undefined]`/`[chat.]` 形态的键名漏出；③按钮无
因文本变长而溢出（伪包天然拉长 30% 文本，等效宽语种压力测试）。该检查把"漏键"
从用户投诉变成 CI 红灯。

### 13.4 验收

- 抽取脚本对当前 app.js 的报告可复现（同输入同输出，DUP/ORPHAN 人工复核清单为零歧义）；
- `t()` 回退链三级各有单测；伪本地化冒烟在 CI 绿；
- `lang/` 目录加入 secret_scan 白名单核验（无凭据字段）与 `--record` 之外的构建产物排除。
