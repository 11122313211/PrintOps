<div align="center">

# PrintOps 印刷订单智能体

**把模糊的印刷需求，整理成规格可核对、依据可追溯的本地交接单**

[简体中文](README.md) · [English](README.en.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Version](https://img.shields.io/badge/version-1.3.0.1--candidate-blue)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)

**Python 标准库运行时 · 本地优先 · 可选模型 · 人工核对与确认**

![PrintOps 工作台](docs/screenshots/workbench.png)

</div>

## 给谁解决什么

PrintOps 面向市场、设计、采购等非印刷专业用户。先描述用途和大致需求，再按提示补充品类、数量、尺寸、材料、颜色与交期；比较工艺方案、核对依据，生成本地交接单供供应商复核。

**北极星目标**：首次用户在 10 分钟内独立完成一份规格正确、关键参数已核对、依据可追溯的交接单。计时从首条有效需求开始，包含澄清、等待、修改、确认和导出；实际打开导出文件，确认内容与当前已确认订单版本一致，才算完成。“字段填满”或“可以生成”只是中间状态。

首轮验收采用 8 名未参与开发的非专业用户，每人一个指定任务：3 个名片、3 个单页、2 个标准宣传册。目标是至少 7 个任务在 10 分钟内独立合格完成，关键尺寸/数量错误、产品串项和过期确认沿用为 0；所有参与者理解估算不等于报价、订单尚未发送。**尚未完成真人验证**；固定任务卡、判定规则与空白评分表见 [真人走查包](docs/USER_WALKTHROUGH.md)。

## 当前能力与边界

- `VERSION` 为 **1.3.0.1 候选基线（NewUI 分支：蓝晒制版视觉语言）**（随发布收口 bump），实现与验证范围见 [下一阶段开发验收](docs/NEXT_STAGE_ACCEPTANCE.md)。代码更新不等于稳定发布。
- 它是订单前置整理工具。当前没有真实供应商授权、HTTP transport 或 live 工具入口；确认、导出和打印都不代表供应商已接收。
- 参考估算来自版本化的 **illustrative 人工示例参数**，不是市场价、供应商报价或交期承诺。PDF 检查只提供基础线索，不能证明文件可直接生产。
- 真实脱敏语料目前为 **0**，严格发布门槛保持阻断。开发评测、演示输入、可用性任务和示例报价不能替代真实订单。
- 当前领域规则与演示输入使用中文；英文 README 是说明文档，不代表英文对话已通过验收。

## 快速开始

产品运行只需 **Python 3.9+** 和浏览器，不需要 npm、Playwright 或第三方 Python 包。

```bash
git clone https://github.com/11122313211/PrintOps.git
cd PrintOps
python3 server.py --check
python3 server.py
```

Windows 可在项目目录双击 `start_windows.bat`。服务启动后打开 [http://localhost:4174/](http://localhost:4174/)；健康检查为 [http://localhost:4174/api/health](http://localhost:4174/api/health)。**不要双击 `index.html` 或使用 `file://`**，页面需要本地 API。首次使用无需配置模型，可直接走规则模式。

终端会显示本地访问令牌，服务页面会自动携带。curl 调试需要 `X-PrintOps-Token` 请求头，健康检查除外。默认使用 `PRINTOPS_STORE=sqlite` 和 `PRINTOPS_MEMORY_PATH=data/agent.sqlite3`；`memory` 模式只适合临时演示或测试，进程退出即丢失。历史、归档和完整备份见 [存储契约](docs/STORAGE_CONTRACT.md)。

| 启动问题 | 下一步 |
| --- | --- |
| 地址以 `file://` 开头 | 启动服务，改用上面的 HTTP 地址。 |
| 无法连接 / 健康检查失败 | 查看启动终端，确认进程仍在运行，再运行 `python3 server.py --check`。 |
| 服务重启后出现鉴权失败 | 重新打开服务页面以获取当前进程令牌；不要复制旧令牌。 |
| 4174 已被占用 | 先确认监听进程：macOS/Linux 用 `lsof -nP -iTCP:4174 -sTCP:LISTEN`；PowerShell 用 `Get-NetTCPConnection -LocalPort 4174 -State Listen`。 |

## 完整用途案例

以下为**人工编写的演示数据**。2026-10-15 使用内存存储、无模型的 Agent 验证了两轮输入、选择方案、生成及本地确认；这不是真人用时或新界面验收结果。界面入口可能随开发调整，以当前页面为准。

1. 输入：`门店开业要发给路人，约500张，下周要用。`
   系统先追问：“你想做哪一种印刷品？”用途不能代替生产规格，约数需要核实。
2. 示例中的用户确定采用单页后补充：`做单页，数量确定500张，成品210×297mm，157g铜版纸，双面四色，下周内，无特殊工艺。`
   系统规范为 `500 张`、`210 × 297 mm`（A4）、`157 g/m² 铜版纸`，生成三档方案。不知道材料时先要求解释或推荐，再核对所选参数；不要把示例答案当作所有用途的默认规格。
3. 查看差异与计价依据，在本例选择“经济方案”；核对仍为 500 张、上述成品尺寸及材料、双面四色、无特殊工艺、无需装订。选择其他方案可能改变材料或后道，需要重新核对。
4. 生成交接草稿，检查字段和供应商待核实事项。本例尚无文件，明确记录“文件待交付，价格与交期待供应商复核”，再作本地人工确认。
5. 导出并打开文件，核对内容、确认状态和版本。变更数量、尺寸、材料或方案后，重新生成与确认。右侧“查看交接资料”提供当前权威版本的交接预览、JSON/CSV/Markdown和A4打印；历史中的完整备份另用于恢复/审计，详见 [开发验收](docs/NEXT_STAGE_ACCEPTANCE.md)。

### 交接单内容样例

以下为上述规则运行生成的**本地交接草稿节选**，只展示规格与边界，不冒充已发送订单；实际订单号、版本、确认时间以运行记录为准。

```text
目标平台：通用印刷平台
供应商状态：仅本地草稿，生产能力、价格和交期需确认
印刷品：单页
数量：500 张
成品尺寸：210×297 mm
纸张/材料：157 g/m² 铜版纸
印刷颜色：双面四色
表面工艺：无特殊工艺
装订/后道：无需装订
交期：下周
```

本地确认备注：`仅核对本地交接内容；文件待交付，价格与交期待供应商复核。` 备注不会变成供应商承诺。待确认字段候选仍不能作为已确定生产规格生成交接单。

## 能力矩阵

| 能力 | 当前可用 | 限制与证据 |
| --- | --- | --- |
| 识别品类与补全参数 | 17 个目录品类、品类追问、修改/否定、多产品独立项 | 目录数量不等于全品类可估价；真实话术准确性仍待真实语料验证。 |
| 尺寸与依据 | mm 归一；cm/in、ISO 216 别名输入；成品/展开/刀模/包装未定语义/内尺寸/外尺寸分开保存 | 原文与候选保留；低置信度生产参数需确认。见 [订单契约](docs/ORDER_CONTRACT.md)。 |
| 有条件示例计价 | 名片、单页、数码印刷、邀请函、吊牌、PVC卡、信封封套、手提袋；宣传册/画册、折页、部分包装/标签/海报有各自条件 | 示例参数按 rate card `2026.11.01` 按 `industry-standard-flow-2026-10` 来源人工重推（引擎逻辑零改动，golden 与算例手工重算）；数量单位、材料、面数和计价尺寸必须明确；折页需展开尺寸，书刊需页数/装订，包装三维不能代替平面刀模。见 [计价覆盖](docs/PRICING.md#当前覆盖范围)。 |
| 未标定品类 | PVC 展板、联单、纸杯仍可整理参数并写明阻断原因（本轮 blocked 由 7 类收窄到 3 类：吊牌/PVC卡/信封封套/手提袋已按现行 family 枚举标定解禁） | 不能套用通用品类价格；返回 blocked 并注明驱动缺口（展板缺板材厚度、联单缺联数、纸杯为三维结构，规划中的计价引擎 schema 3.0 解决）。未知工艺只返回已知明细与未计入项，不返回完整区间。 |
| 可解释估算 | Decimal 拆分材料、准备、运行、表面工艺、装订后道、模切、包装和最低收费补差 | `ready` 完整示例区间、`partial` 已知小计、`blocked` 补充原因；不含税、出厂口径，来源/版本/未包含项可查。 |
| 文件基础检查 | 浏览器本地 PDF 页数、页面框、颜色空间和字体嵌入线索；原稿不上传 | 不替代专业印前检查；没有文件必须标为待交付。 |
| 记忆与恢复 | 后端 OrderStore 权威状态；SQLite/memory/custom、CAS、完整历史、归档与备份 | 内置默认 SQLite；memory 不持久；企业数据库需可信注册实现。 |
| 供应商连接 | 通用本地导出、未配置供应商、本地 mock；连接器协议与契约测试 | 没有真实供应商连接；mock/sandbox 结果不是实际生产能力。 |
| 交接与确认 | 权威交接草稿、本地人工确认、JSON/CSV/Markdown导出、A4打印及独立完整备份 | 确认不是企业身份签章；导出不是发送。导出前复核当前版本，参数变化令旧确认失效。 |
| 工具调用统计 | 每次工具调用在网关计时落库（ok/failed/blocked/rejected 全终态），本地 `tool_call_log` 持久化；`GET /api/tools/stats?window=30d`（7/30/90 天）与设置工具箱统计视图 | 数据仅来自本机数据库（scope: local-only），不上传不共享；令牌守卫与其他接口一致；统计写入失败不影响对话主流程。 |
| 知识复核机制 | manifest 声明 90 天复核周期与下次复核日，`knowledge_review_status()` 返回 ok/due/overdue 与 rate card 余量；`tools/knowledge_review.py` 离线检查（dev_check 非 `--quick` 路径挂载） | 复核到期在订单响应注入提示级 `knowledge_review_due` 风险；到期仅警告不阻断，结构性不一致才以退出码 1 失败。 |
| 草稿编辑与界面健壮性 | 草稿字段重绘按稳定 `data-field-key` 恢复焦点与选区，展开状态跨重绘保留；绑定前一次性元素 ID 存在性断言；错误文案单一来源（`PrintOpsJourney.ERROR_TEXTS`） | 响应在输入途中到达不再打断编辑；缺元素以汇总横幅报出并只跳过缺失项，不再借第一个 TypeError 让整页绑定失联。 |

## 界面预览

以下为 **2026-10-25、1.3.0.1 NewUI 分支（蓝晒制版视觉语言）**的 Chromium 截图，订单为人工合成展示数据；当前 UI 位置微调以实际页面和浏览器专项结果为准。390px 图片只验证桌面窄视口，不证明真实手机/触控适配。三阶段为”补充需求 → 比较方案 → 核对与交接”。

| 方案对比 | 390px 窄视口预览 |
| --- | --- |
| ![方案对比](docs/screenshots/plan-comparison.png) | <img src="docs/screenshots/mobile-390.png" width="260" alt="390px 窄视口预览"> |

## 开发与验收

本项目有意不使用 GitHub Actions，发布检查以本机为准。漏洞报告方式见 [SECURITY.md](SECURITY.md)，开发约束见 [CONTRIBUTING.md](CONTRIBUTING.md)。

Python 运行时和单元/契约检查使用标准库。**浏览器开发测试另需 Node.js 18+、npm、固定版本 Playwright 和 Chromium**；它们不是启动产品的依赖。

```bash
python3 tools/dev_check.py                             # 本机一键门禁：编译+单测+评测+知识复核+守护（--quick 只跑编译与守护）
python3 -m unittest discover -s tests -p "test_*.py"
PYTHONPATH=. python3 tests/evaluate_agent.py             # 开发报告
PYTHONPATH=. python3 tests/evaluate_agent.py --release-gate --json
python3 tools/knowledge_review.py                       # 知识复核门禁：dev_check 非 --quick 已自动挂载
python3 tools/secret_scan.py
python3 tools/repo_guard.py
python3 tools/dsh_mcp_smoke.py
npm ci
npm run test:browser:install
npm run test:browser
npx playwright test --config tests/journey.config.mjs
npx playwright test --config tests/ui_positions.config.mjs
npx playwright test --config tests/ui_focus.config.mjs
# Optional second browser engine
npx playwright install webkit
PRINTOPS_TEST_BROWSER=webkit npx playwright test --config tests/journey.config.mjs
PRINTOPS_TEST_BROWSER=webkit npx playwright test --config tests/ui_positions.config.mjs
PRINTOPS_TEST_BROWSER=webkit npx playwright test --config tests/ui_focus.config.mjs
```

严格发布门槛：合成字段准确率 ≥90%、可完成用例完整率 ≥80%；真实脱敏订单至少 20 例且字段准确率 ≥95%。真实语料缺失、为空、格式错误、不足或低准确率均以退出码 1 阻断。当前真实库为空；不加 `--release-gate` 的开发命令成功不能证明发布通过。

2026-10-24（1.3.0 候选基线）本轮 Python 单测 **544 项通过**，dev_check 六步全绿；Chromium 浏览器四套件 journey **34**、UI 位置 **16**、UI 焦点 **15**、基础冒烟 **13** 项通过。111 条合成用例字段准确率 100%，不代表真人交接成功率。严格发布检查因真实语料 0 例而按预期失败（组织性阻断，不属于本轮阻塞项）。完整证据见 [浏览器记录](docs/BROWSER_JOURNEY.md) 与 [开发验收](docs/NEXT_STAGE_ACCEPTANCE.md)；Firefox 在本机启动环境阻断，真实手机、真人走查与真实 dsh/模型仍待独立验证。

## 进阶：模型、MCP 与运行边界

### 可选模型

未配置模型时，规则 Agent、默认 SQLite 和 PDF 基础检查留在本机。启用 OpenAI-compatible 模型后，当前消息、有限历史、精简订单和工具结果发送到配置的 endpoint；确认该服务的日志与留存策略。模型不可用时保留规则回退。

设置页的"测试连接"会给出具体根因（Key 被拒、DNS 超时、端点不可达、TLS 失败等）并在首个网络性失败时立即返回；探测进行中关闭设置弹窗即取消请求。慢推理模型可通过 `PRINTOPS_LLM_TIMEOUT`（默认 20 秒，5–120 封顶）放宽单次规划预算，需同步调高 `PRINTOPS_REQUEST_TIMEOUT`（见 [请求预算](docs/RUNTIME_BUDGET.md)）。探测采用连通预探测与三跳并发，在单次往返内完成并附带每跳耗时；不可达结论 15 秒内复用，第二次点击自动强制重测。

模型请求使用最近 8 条历史（每条最多 2,000 字符）、订单摘要/工具结果各最多 12KB，并限制嵌套和集合规模。模型最多进行 3 轮有界工具规划，工具统一经过 `Agent.call_tool()`；参数不能携带任意完整订单取代本地状态。原生 `tools[].function` 不可用时回退 JSON envelope；模型未发工具调用时，规则按意图补齐必要校验、估价、术语解释或推荐。

公司模型在受信内网时，显式开启地址例外后启动：

```bash
PRINTOPS_ALLOW_PRIVATE_LLM_HOSTS=1 python3 server.py
```

在设置中填入公司提供的 base URL，例如 `https://llm.internal.example/v1`，程序请求 `/chat/completions`。这是示例地址；开关不会修复 DNS，须先接入公司网络/VPN。默认拒绝私有、环回等地址，开启例外后也保留其他 URL 检查；本地服务不应暴露公网。

### 无 npm/pnpm 的本地 host

```bash
python3 tools/printops_local_host.py \
  --session-id local-demo \
  --message "做500张名片，成品90×54mm，300g铜版纸，双面四色，下周内"
```

该标准库 host 发现 `.dsh/skills` 下五个 skill，通过 stdio launcher 验证 MCP 边界，再使用规则 Agent；不调用 DeepSeek 模型，不等于真实 dsh headless 验收。`--interactive` 开启逐行交互，`--memory-path data/agent.sqlite3` 保留会话。只检查边界可运行 `python3 tools/dsh_mcp_smoke.py`。

### 安全与数据

除健康检查外 API 要求进程内本地令牌；带 Origin 的跨来源请求被拒绝。静态文件采用白名单，源码、运行数据与 `.git` 不对外提供。模型 Key 不进入对话、运行 trace 或交接导出；本机 `data/llm_config.json` 使用权限 600，环境变量可避免写入该文件。SQLite 启用 WAL 与 busy_timeout，损坏会话隔离保存；更多边界见架构、存储契约及路线图。

## 文档与路线图

| 文档 | 内容 |
| --- | --- |
| [下一阶段产品与设计计划](docs/NEXT_STAGE_PRODUCT_DESIGN_PLAN.md) / [开发验收](docs/NEXT_STAGE_ACCEPTANCE.md) / [组件状态](docs/UI_STATES.md) | 目标、已实现证据、未完成真人门槛及下一轮任务。 |
| [真人走查包](docs/USER_WALKTHROUGH.md) / [度量口径](docs/plan/appendix-metrics.md) | 固定任务卡、8 人分配、独立复核、空白记录与事件设计。 |
| [持续目标](docs/ROADMAP.md) / [发布清单](docs/RELEASE_CHECKLIST.md) | 当前优先级、已实现与未闭合门槛。 |
| [实施验收矩阵](docs/IMPLEMENTATION_ACCEPTANCE.md) / [浏览器验收](docs/BROWSER_SMOKE.md) | 实际证据和未验证范围。 |
| [架构](docs/ARCHITECTURE.md) / [订单契约](docs/ORDER_CONTRACT.md) | 模块、字段证据、尺寸语义和权威状态。 |
| [存储](docs/STORAGE_CONTRACT.md) / [计价](docs/PRICING.md) / [供应商连接器](docs/SUPPLIER_CONNECTORS.md) | 可替换契约、示例成本与未来接入条件。 |
| [存储操作](docs/STORAGE_OPERATIONS.md) / [成本算例](docs/PRICING_WORKED_EXAMPLES.md) / [连接器扩展示例](docs/SUPPLIER_EXTENSION_GUIDE.md) / [请求预算](docs/RUNTIME_BUDGET.md) | 备份恢复、人工复核计算、第三方本地接入和运行时边界。 |
| [文件管理](docs/FILE_MANAGEMENT.md) / [dsh 集成](docs/plan/dsh-print-agent.md) | 提交边界与可选集成。 |
| [历史开发计划](docs/DEVELOPMENT_PLAN.md) / [开源选型](docs/OPEN_SOURCE_OPTIONS.md) | 设计参考；历史排期和任务卡不代表当前状态。 |

v1.3.0 仍为候选基线；先完成本地订单闭环与独立验收。真实供应商接入已顺延至 v1.4+，需另行授权、官方协议、身份认证、服务器确认和 sandbox 验证；不会因存在连接器接口而自动开启。

## 贡献与许可证

欢迎 Issue 与 PR；提交前运行相关 Python/浏览器回归、开发评测与敏感扫描，发布时额外运行严格门槛并保存证据。[MIT](LICENSE) © PrintOps contributors。

品类目录参考盛大印刷公开产品导航（[sd2000.com](https://www.sd2000.com/)）等公开信息，仅用于参数整理，不代表供应商实时价格、产能或交期。
