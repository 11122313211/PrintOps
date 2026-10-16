# UI 位置与交互微调验收记录

日期：2026-10-16。对应计划：[UI_POSITION_INTERACTION_PLAN.md](UI_POSITION_INTERACTION_PLAN.md)。本轮围绕入口位置与对话可用性做小批调整，A1/A2、B1/B2、C1 已实施；C2 任务条重排保持候选状态。

本文件记录第一批位置调整的冻结版本与证据。后续新增的入口展开语义、键盘焦点、WebKit 下拉高度与断点时序修复，以及最新验证边界，见 [UI_FOCUS_REVIEW.md](UI_FOCUS_REVIEW.md)。第一批报告与快照保持历史原样。

## 1. 当前结果与范围

| 对象 | 现在的位置或表现 | 原有行为 |
| --- | --- | --- |
| 新建订单 | 左栏历史列表上方，使用完整文字 | 仍需原确认弹窗，取消保留当前订单，旧单仍在历史中 |
| 左右栏开关 | 对话标题左侧控制左栏，右侧控制订单栏 | 继续沿用原开关、栏宽和用户偏好 |
| 工艺方案、交接资料 | 右栏“订单草稿”标题下，上传区之前 | 仍使用原 `data-view` 页面，查看不自动生成、确认或下载 |
| 目标平台 | 右栏供应商信息内，有可见标签 | 仍由用户主动切换，执行原平台修改请求 |
| 输入示例 | 默认折叠为一行“查看输入示例” | 原三条示例可展开；点击只填入输入框，由用户发送 |
| 手机顶栏 | 平台与新建移出后恢复单行 | 高对比、接口设置保留现有位置 |

保留暖纸色、品牌红、三栏结构和原业务页面。第一批位置调整的产品修改限于 `index.html`、`styles.css`、`app.js`；该批冻结时的 `journey.js` 与该批基线逐字相同。后续焦点补丁仅对用户导航启用原有焦点参数，见最新记录。没有重写 Agent、后端、订单协议、计价、存储或供应商授权规则。

“统一入口”采用固定主位置，保留必要的上下文快捷操作。例如输入区上传与右侧 PDF 拖放仍调用原文件处理流程，不复制文件状态；下一步任务条仍调用原动态动作。

## 2. 位置调整之外的交互修复

以下改动由实际使用或自动化复现支持，单独列出，避免把功能修复混作纯排版变动。

| 问题 | 修复与验证 |
| --- | --- |
| 手机短屏时示例卡约占 241 px，任务条遮住发送按钮 | 折叠原示例、解除阻止聊天区域收缩的最小高度；极短横屏允许容器滚动。390×667、390×568 的原遮挡均已复核解除；发送目标至少 44 CSS px。 |
| 新建弹窗打开时，左栏键盘处理抢占 Escape/焦点 | 原生 dialog 打开时侧栏键盘处理让位；取消后显式聚焦原新建入口；手机确认后收起左栏并聚焦输入。 |
| WebKit 确认新建后重开弹窗，按 Escape 会再次创建订单 | 每次打开前清空原生 dialog 的旧 `returnValue`；保留“确认一次→重开→Escape”回归。 |
| 中文组词回车可能触发发送 | `isComposing` 或兼容键码 229 时不提交；普通 Enter 与 Shift+Enter 保留。证据来自模拟 composition 事件，实际系统输入法仍待走查。 |
| 延迟回执后切单、从归档切回可写单，部分按钮保持禁用 | 正确恢复现有 busy/归档禁用覆盖，再由原渲染逻辑计算当前可用性；确认、取消询价纳入原禁用恢复机制。未新增生成或确认规则。 |
| 高对比模式用户气泡白字白底，修正底色后 Markdown 标题黑字黑底 | 用户气泡设为白字黑底，内部标题继承文字色；普通文字、链接、代码、标题及表格均通过对比度至少 7 的专项。 |

按钮恢复修复包含真实生成/确认操作回归：旧订单 A 的延迟响应结束后，B 的 ready、pending、confirmed 三种状态保持正确，实际请求携带 B 的会话与版本。独立审查还验证了两种浏览器中的本地询价取消，未向供应商提交请求。

## 3. 浏览器验证

环境：Playwright 1.56.1；Chromium 141.0.7390.37、WebKit 26；本机无头浏览器。使用临时 SQLite、隔离 loopback 服务和合成订单，不写入现有业务订单，不调用模型。

| 套件 | Chromium | WebKit | 证据 |
| --- | --- | --- | --- |
| 完整业务交互 journey | 34/34 | 34/34 | [Chromium](../reports/journey-browser/report.json)、[WebKit](../reports/journey-webkit/report.json) |
| UI 位置与交互专项 | 15/15 | 15/15 | [Chromium](../reports/ui-position-tuning/chromium/report.json)、[WebKit](../reports/ui-position-tuning/webkit/report.json) |
| smoke | 8/8 | 未运行 | [报告](../reports/browser-smoke/report.json) |
| 最终高对比标题修补后专项复验 | 1/1 | 1/1 | [Chromium](../reports/ui-position-tuning/high-contrast-chromium.json)、[WebKit](../reports/ui-position-tuning/high-contrast-webkit.json) |

五份完整套件共 106 项次通过。随后扩展了高对比测试中的标题、表格和代码块断言，仅增加用户气泡标题的颜色继承规则，并在两种浏览器各复验一次该专项；没有重复计算为新增的独立用例。

覆盖入口可达、往返保留未发草稿、仅查看不写订单、不自动下载、新建取消与焦点、平台显式修改、示例只填入、长输入、模拟输入法回车、归档只读及延迟回执后的控件恢复。独立审查另检查两种浏览器各六个尺寸，共 12 组；当前没有遗留阻塞项。完整过程和曾发现的问题见[独立 review](../reports/ui-position-tuning/independent-review.md)。

## 4. 排版证据

最终静态测量中，收起的示例高度均为 46 px：

| 视口 | 聊天记录区域高度 | 输入区纵向范围 | 判定 |
| --- | --- | --- | --- |
| 1440×900 | 438 px | 720–827 px | 桌面布局成立 |
| 390×844 | 390 px | 660–767 px | 手机宽度下输入区完整 |
| 320×568 | 114 px | 384–491 px | 短屏无发送/任务条重叠 |

独立短屏检查中，390×667 发送按钮底部为 581 px、任务条顶部为 590 px；390×568 分别约 482、491 px。844×390 横屏通过滚动访问展开示例与发送，不声称所有内容始终固定可见。原始测量见[measurements.json](../reports/ui-position-tuning/final/measurements.json)。

截图使用隔离测试订单：

- [桌面对话](../reports/ui-position-tuning/final/chat-1440.png)、[手机对话](../reports/ui-position-tuning/final/chat-390.png)、[320 px 短屏](../reports/ui-position-tuning/final/chat-320.png)。
- [桌面已填订单](../reports/ui-position-tuning/final/order-1440.png)、[手机订单栏](../reports/ui-position-tuning/final/order-390.png)、[高对比](../reports/ui-position-tuning/final/high-contrast-1440.png)。

## 5. 复现与按批回退

在项目根目录运行，测试夹具会启动隔离服务：

```sh
npx playwright test --config tests/journey.config.mjs
PRINTOPS_TEST_BROWSER=webkit npx playwright test --config tests/journey.config.mjs
npx playwright test --config tests/ui_positions.config.mjs
PRINTOPS_TEST_BROWSER=webkit npx playwright test --config tests/ui_positions.config.mjs
npm run test:browser
```

原始报告、截图与快照位于本地 `reports/`，属于生成证据。最终统计、源文件哈希和差异见 [verification.json](../reports/ui-position-tuning/final/verification.json)。

本批起点为 `reports/ui-position-tuning/baseline/`，A/B/C 各批均保留了产品快照和相邻批差异，最终版本保存在 `final/source/`，相对起点的逐文件 patch 在 `final/diffs/`。回退时只反向应用需要撤回的本批差异，并先核对当前文件，避免覆盖后续工作。不可用整个工作区的 `git reset` 或 `git restore` 回退，因为其中还有本轮之前的业务修改。

## 6. 下一阶段验收

1. **真人找入口**：邀请 5 名未参与开发的人，完成继续聊天、找回旧单、新建后取消、查看并返回方案、查看交接、找到平台设置六个任务；记录首次点击、用时、误入和求助。试验目标为每项至少 4/5 人无需提示完成，误确认、误导出和输入丢失为 0。
2. **真实设备**：在实际手机检查软键盘弹出/收起、输入法选词、长输入与回到对话；用浏览器真实 200% 缩放核对重排。这些不能用桌面改视口替代。
3. **只按观察继续微调**：若新建入口漏找，调整其左栏内部位置或强调程度；若方案/交接入口不明显，调整既有入口的间距与点击面积；若任务条持续挤占对话，再比较 C2 轻提示版。每批只处理一组问题，保留同任务前后记录及独立 review。

上述真人、真实软键盘、系统中文输入法和实际缩放仍未验收。项目既有至少 20 条真实脱敏订单发布门槛继续保留，合成浏览器订单不替代它。本轮完成的是一批有回归证据的 UI 微调，不代表整个项目达到发布标准。
