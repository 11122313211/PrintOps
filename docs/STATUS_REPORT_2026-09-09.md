# PrintOps 当前状态与 UI/工具匹配报告

日期：2026-09-09
范围：当前工作区代码、4174 本地运行实例、测试与 dsh/MCP 适配层

> 版本同步（2026-09-10）：当前 `VERSION` 为 `1.1.0`，定位为受控本地 MCP/dsh 集成候选版。本报告是 2026-09-09 的状态快照；真实 dsh/model/browser 验收、真实语料门槛和供应商 live 接入状态仍按正文所列边界执行。

## 1. 结论摘要

PrintOps 已经是一个可运行的本地印刷订单工作台，不再只是 UI 原型：规则 Agent、订单模型、SQLite 会话、8 个系统工具、人工确认闸门、PDF 基础预检、询价草稿、MCP/dsh sidecar 和可选 OpenAI-compatible planner 都已存在。

当前完成度可以分成三层：

| 层 | 当前状态 | 判断 |
| --- | --- | --- |
| 规则订单内核 | 已完成 MVP 主链路 | 可稳定做需求收集、补字段、推荐方案、生成草稿、人工确认和导出 |
| UI 与 HTTP 系统工具 | 已基本贯通 | 主要路径已映射；仍缺浏览器自动化回归和少数工具/可观测性补齐 |
| 真实模型 / dsh 端到端 | 已完成适配层，未完成生产验收 | planner 已支持 JSON envelope 和原生 `tool_calls`；真实内网 endpoint、dsh headless 和长链路仍需可重复验收 |

当前 4174 已重新加载工作区最新代码，并以 `PRINTOPS_ALLOW_PRIVATE_LLM_HOSTS=1` 启动。非敏感状态显示：内网 URL 已配置、模型已启用、Key 已配置、内网地址策略已开启。API Key 不在本报告中记录。

## 2. 当前已有的系统

### 2.1 UI 工作台

- `index.html` + `styles.css` + `app.js` 构成零构建、零 npm/pnpm 依赖的浏览器工作台。
- 入口包括：自然语言对话、订单草稿 inline 编辑、多产品订单项、方案卡片与对比、PDF 上传/基础预检、目标平台切换、询价状态、确认交接单、JSON/CSV/Markdown 导出、模型设置、高对比模式和会话恢复。
- UI 会消费后端快照中的 `order`、`validation`、`readiness`、`fieldMeta`、`supplierCapability`、`options`、`quoteRequests`、`runTrace`、`availableTools` 等状态。
- UI 当前仍是单文件 `app.js`（约 1,863 行），计划中的 ES Module 拆分、定向渲染和 Playwright 冒烟尚未落地。

### 2.2 规则 Agent 与订单内核

- `nlu.py`：规则感知、字段抽取、数量/尺寸语义。
- `order_model.py`：订单契约、规范化、品类字段和多产品项。
- `product_knowledge.py`：品类目录、专属参数、版本化知识和示例价格参数。
- `agent.py`：SQLite 会话、工作流阶段、字段 provenance/confidence、冲突记录、工具网关、人工确认和最多三轮 planner tool-loop。
- `tools.py`：8 个白名单系统工具，工具函数本身不做外部网络写入。
- `supplier_adapters.py`：静态供应商能力和字段映射；当前是适配协议/草稿，不是真实供应商提交。

### 2.3 HTTP API

现有接口已经覆盖主链路：

| UI 动作 | HTTP 路由 | 后端执行 |
| --- | --- | --- |
| 初始化/恢复 | `GET /api/session`、`GET /api/products`、`GET /api/tools` | 读取会话、产品知识、工具契约 |
| 对话建单 | `POST /api/chat` | 规则感知 → 可选模型规划 → patch → 工具执行 |
| 选方案/生成/确认 | `/api/choose`、`/api/generate`、`/api/confirm` | 状态机、人工闸门、交接单 |
| 文件预检 | `POST /api/preflight` | 本地 PDF 线索 + `preflight_file` |
| 询价草稿 | `/api/quote/status`、`/api/quote/cancel` | 幂等、状态和人工确认 |
| 工具箱 | `POST /api/tools/call` | 统一经过 `Agent.call_tool()` 网关 |
| 模型设置 | `/api/settings`、`/api/model/test` | URL/Key/模型配置和连接测试 |

### 2.4 系统工具

当前注册 8 个工具，UI 显式展示其中 7 个：

| 工具 | 规则 Agent | HTTP/MCP | UI 入口 | 状态 |
| --- | --- | --- | --- | --- |
| `validate_order` | 是 | 是 | 工具箱/对话流程 | 已贯通 |
| `recommend_processes` | 是 | 是 | 工具箱/模型规划 | 已贯通 |
| `estimate_price` | 是 | 是 | 工具箱/价格意图 | 已贯通 |
| `prepare_handoff` | 是 | 是 | 生成交接单 | 已贯通 |
| `request_supplier_quote` | 是 | 是 | 询价面板/工具箱 | 已贯通，停在人工确认 |
| `match_supplier_capability` | 是 | 是 | 工具箱/决策面板 | 已贯通 |
| `explain_print_term` | 是 | 是 | 工具箱/解释意图 | 已贯通 |
| `preflight_file` | 是 | 是 | 上传 PDF 流程 | 后端已贯通，但未显示在工具箱 |

这里的“工具执行”不是直接把模型交给系统：模型只能提出受限的 `tool.name + arguments`，最终仍由 Agent 白名单、参数边界、订单完整度、低置信度和人工确认门决定是否执行。

### 2.5 MCP / dsh 层

- `mcp_server.py`：标准库 JSON-RPC stdio MCP server，带 session binding、L0/L1 capability、参数校验、审计和受控 patch bridge。
- `tools/dsh_mcp_launcher.py`：拒绝任意 session/L2 的受信启动器。
- `tools/dsh_mcp_smoke.py`：无需 Node/npm/pnpm 的 MCP transcript smoke。
- `tools/printops_local_host.py`：无需 dsh/DeepSeek 的 Python fallback host。
- `.dsh/skills/`：5 个印刷 skill，已具备项目内发现和 profile 示例。
- 真实 dsh headless、真实 DeepSeek tool-call、dsh on/off parity 和生产启用仍未完成。

## 3. 已验证结果

- 完整 Python 测试：`236` 项通过（含本机回环 HTTP handler）；执行时需要允许绑定本机回环端口。
- 合成评测：`111` cases，字段准确率 `100%`，14 个可完成订单完整率 `100%`，平均 1.22 轮。
- `tools/dsh_mcp_smoke.py`：通过，L0 工具边界有效。
- `tools/secret_scan.py`：通过。
- `git diff --check`：通过。
- 4174 `/api/health`：通过；已确认当前实例加载最新 `privateHostsAllowed` 字段。
- 当前真实内网 endpoint 在本次执行环境曾返回 DNS `gaierror`。这属于 VPN/DNS/网络可达性问题，不是 URL 语法问题；代码只能允许访问，不能替代公司 DNS。公开文档使用占位域名，真实地址只保留在本机忽略的配置中。

## 4. UI、系统工具与模型的匹配判断

### 已匹配的部分

1. UI 的按钮和上传流程最终都进入 HTTP API，再进入 `Agent`，没有从浏览器直接调用工具函数。
2. HTTP、MCP 和模型 planner 共用 `Agent.call_tool()` 或受控 patch kernel，订单完整度、低置信度、平台能力和人工确认规则一致。
3. UI 展示的字段来源/置信度与后端 `fieldMeta` 一致，低置信度字段会影响生成/交接。
4. `runTrace` 能显示感知、规划、工具开始/完成/阻断/失败等事件，便于判断主链路是否执行。

### 尚未完全匹配的部分

1. UI 工具箱只展示 7 个工具，`preflight_file` 只通过上传入口调用；这不是功能缺失，但工具目录和 UI 目录不是同一份声明。
2. `TOOL_SCHEMAS` 是内部 provider-neutral schema；planner 现在会把它转换成 OpenAI-compatible `tools[].function`，并去掉聊天模型不应携带的完整 `order` 参数。JSON fallback 仍保留精简工具目录。
3. planner 现在同时解析标准 `message.tool_calls` 和 JSON `message.content`，并在 native 模式把工具结果作为 `assistant/tool` 上下文回传。
4. planner 最多三轮有界工具规划，重复判断改为“工具名 + 参数”；同一工具允许带不同参数继续，但仍禁止完全相同的无限循环。
5. UI 的运行记录现在能区分模型请求工具和本地 fallback 工具，但不显示工具参数校验结果、原生 tool-call ID、拒绝原因和第二轮输入摘要，排障信息仍不够。
6. 上下文传输已做有界化：历史最多 8 条、单条 2,000 字符；订单摘要和工具结果分别限制 12KB；native 后续轮次只发工具名，JSON 兼容模式才发精简参数 schema。
6. `planMeta`、`rejectedFields`、模型原始 evidence 的后端契约已存在，但 UI 没有独立面板展示“模型提出但被系统拒绝的字段”。

离线复现实验已经证明：JSON envelope 和标准原生 `tool_calls` 都可以执行 `recommend_processes`，第二轮能收到工具结果；即使模型只返回自然语言，Agent 也会按本地意图兜底执行必要的校验、费用、术语、询价或方案工具。真实内网 API 仍需要做端到端走查，确认其返回格式和工具 schema 兼容。

## 5. 当前运行与端口结论

- 默认端口仍是 `4174`，`start_mac.sh`、Windows 启动脚本和 `server.py` 都以 4174 为默认。
- `4175` 只是临时验证实例，已停止。
- 4174 已停止旧进程并用当前工作区代码重启，启动参数包含：

```bash
PRINTOPS_ALLOW_PRIVATE_LLM_HOSTS=1 python3 server.py
```

- 4174 当前可访问；模型实际能否请求成功仍取决于本机的 DNS/VPN 和 endpoint 的 API 兼容性。

## 6. 下一步建议（按优先级）

### P0：用真实内网 API 闭合工具调用验收

1. 用公司 endpoint 做 3 条真实走查，确认 URL/DNS、模型名、鉴权和 `/chat/completions` 均可用：
   - 完整订单 → `recommend_processes`；
   - 术语问题 → `explain_print_term`；
   - 信息不足 → 不调用高风险工具并明确缺失字段。
2. 记录真实响应的 `finish_reason`、是否 native tool-call、工具名和第二轮结果，不记录 Key 和完整原稿。
3. 为每轮增加 `planRound`、`requestedTool`、`acceptedTool`、`rejectedReason`、`providerRequestId` 的安全审计字段，UI 只展示摘要。
4. 保留本地 fake OpenAI server 的契约测试，作为真实 endpoint 不可用时的离线门禁。
5. 对每轮上下文记录大小预算；当前实现已限制历史为最近 8 条、单条消息 2,000 字符，订单摘要 12KB、工具结果 12KB，并从聊天工具 schema 中移除完整订单和输出 schema。原生工具后续轮次只携带工具名，JSON 兼容回退才携带精简参数 schema。

### P1：完成 UI 与工具目录的一致性

1. 由 `/api/tools` 生成唯一工具目录，UI 不再维护 `toolLabels` 的独立遗漏列表；缺失 label 的工具显示“系统工具”或隐藏原因要可追踪。
2. 工具按钮显示状态：可调用、信息不足、需人工确认、被阻断、已完成。
3. 在运行记录增加“模型规划 → 系统校验 → 工具执行 → 工具结果 → 模型总结”的五段时间线。
4. 增加“被拒字段/被拒工具”面板，连接 `rejectedFields`、`planMeta` 和 runTrace。
5. 当前模型协议模式已在接口设置状态中显示（JSON 兼容 / 原生工具 / 连接异常）；完整 rule fallback 和工具拒绝摘要仍需补到运行记录。

### P1：完成浏览器级验收

优先不引入运行时依赖，但可在独立 `e2e/` 开发目录使用 Playwright：

- 首次对话 → 订单字段 → 三档方案；
- 双产品 → 切换第 2 项 → 只修改第 2 项；
- API 失败 → 显示 requestId → 恢复重试；
- 刷新 → 会话/消息/方案/询价状态恢复；
- 模型 native tool-call → UI 显示完整五段 trace。

### P2：前端结构改进

- 按既有 `docs/plan/path-02-frontend-modularization.md` 拆成原生 ES Modules，不引入框架和构建链。
- 先迁移 `runTrace`、工具箱、设置和询价，再迁移订单草稿/多产品，最后做聊天增量渲染。
- 目标：单文件不超过 400 行、单一状态源、定向渲染、编辑焦点不丢失。

### P2：dsh 生产前收口

- 先只开放 L0：`validate_order`、`explain_print_term`、`recommend_processes`。
- 通过真实 dsh transcript、session 隔离、重启恢复、非法参数、tool timeout 和 on/off parity 后，再开放 L1 patch/询价草稿。
- 真实供应商、CUPS、外部提交继续保持 L2 禁用。

## 7. 建议的下一版验收门

下一版不要只看“API 连接成功”，至少要同时满足：

1. 4174 `/api/health` 和 `/api/settings` 显示当前实例确实是新代码。
2. 真实模型返回 native `tool_calls` 时，`runTrace` 出现请求、校验、执行、结果、总结五段。
3. 工具被阻断时，UI 明确显示阻断原因，而不是看起来像模型没响应。
4. 同一场景在 rule-only、JSON fallback、native tools 三种模式下订单字段和工具结果一致。
5. 236 项单测、合成评测 111/100% 和 MCP smoke 已通过；浏览器 5 条冒烟仍需单独执行后才能关闭该门禁。
6. 真实脱敏语料补到至少 20 例并达到字段准确率 ≥95%，否则继续标记为候选版，不宣称生产稳定。

## 8. 总体判断

当前最值得做的不是继续堆印刷字段，而是把“模型协议 → 系统工具 → UI 可观测性”这条链闭合。规则内核和工具安全边界已经足够支撑下一步；真正的短板是 native tool-call 兼容、运行实例一致性、UI 对工具状态的解释能力，以及浏览器级真实验收。
