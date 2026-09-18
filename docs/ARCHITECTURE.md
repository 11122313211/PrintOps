# 轻量 Agent 框架

核心循环参考 LangGraph、PydanticAI 和 AutoGen 的开源设计，但不引入运行依赖：

```text
输入 -> 感知字段 -> 读取/更新记忆 -> 模型规划（可选） -> 调用白名单工具 -> 模型总结 -> 结构化响应 -> 运行记录
```

实现布局：`order_model.py` 持有订单数据契约、规范化与 `schemaVersion` 迁移；`nlu.py` 持有规则感知与置信度分级；`tools.py` 持有工具注册表与契约；`agent.py` 保留会话记忆、工作流和工具网关并 re-export 公共符号；`llm_adapter.py` 负责模型协议与 planner；`mcp_server.py` 提供 stdio MCP 适配；前端由 `app.js` 与 `markdown.js` 组成。

- `Agent`：维护任务状态；模型可提出字段补丁和受限工具调用，工具结果会回传模型总结，最多三轮，失败时回到确定性规则。每次运行生成 `runId` 与步骤事件，并保存字段来源、置信度和冲突。规则感知的置信度按证据强度分级（显式标签/单位 0.9+，裸数字与氛围词低于 0.75），生产字段低置信度时阻止生成交接单。
- `Memory`：SQLite 会话记忆，保存订单、最近 80 条消息和当前阶段；刷新页面可恢复完整对话。
- `TOOLS`：8 个白名单工具注册表，包含工艺推荐、印刷解释、文件预检、订单校验、费用估算、供应商能力匹配、询价准备和订单交接。
- `product_knowledge.py`：独立的产品目录与参数知识。参考盛大印刷官网（`sd2000.com`）公开导航的“名片/卡片、单张、标签/不干胶、书籍画册、广告物料、包装周边、办公用品”等高频品类；品类专属字段保存在 `order.productSpecs`。
- `PLATFORMS`：平台能力注册表。核心订单结构不依赖盛大，可继续增加平台适配器。
- `supplier_adapters.py`：供应商适配器协议。只负责标准订单字段映射和请求模板；真实网络报价/提交由后续平台实现，不能在核心 Agent 中伪造。
- `match_supplier_capability`：将标准订单逐字段匹配到目标平台能力档案，明确支持、待询价确认和不支持项；当前档案是静态示例，不代表实时产能或报价。
- `server.py`：轻量 JSON API。请求体限制为 1 MB，API 异常统一返回 `code/message/requestId`，同一会话在加载状态前加锁；后续可以替换为 FastAPI，不影响 Agent 接口。
- `dispatch_get` / `dispatch_post`：解析后的 API 契约边界。路由参数转换与 Agent 调用在无网络副作用的纯分发层集中维护，便于回归测试和后续替换 HTTP 框架。
- `llm_adapter.py`：OpenAI 兼容适配器与 planner。同时支持原生 `tools[].function` / `tool_calls` 和 JSON envelope 两种协议；模型输出只作为候选计划，仍须经过本地白名单校验。
- `mcp_server.py`：标准库 JSON-RPC stdio MCP server。会话绑定、L0/L1 capability 边界、参数校验与审计；对外只暴露审阅过的 MCP schema，所有执行都回到 `Agent.call_tool()`。
- `tools/dsh_mcp_launcher.py` / `tools/printops_local_host.py`：受信 launcher 与无 Node/npm/pnpm 的本地 fallback host。launcher 要求由受信宿主提供 session id，不接受 `--allow-any-session`。
- `markdown.js`：零依赖 Markdown 渲染器，供对话气泡与输入预览共用；先转义再解析，链接只放行 `http(s)` / `mailto`。

## 模型协作与本地集成层（v1.1.0）

该层在规则内核之上增加受控的模型与外部宿主集成，但不改变确定性边界：模型和 MCP 客户端都只能提出受限请求，是否执行仍由本地代码决定。

### Planner 工具循环

两种模型协议（原生 `tool_calls` 与 JSON envelope 回退）解析后收敛到同一份候选计划结构。

- 工具轮与最终总结轮是**分开的预算**：模型最多使用三轮本地工具轮，之后仍有独立的总结轮；
- 完全相同的「工具名 + 参数」组合会被阻断，避免重复调用；
- 提供方把三轮浪费在劣质计划上时，显式用户请求仍有一条独立、有界的本地补偿预算（`MAX_PLANNER_REQUIRED_CALLS`），防止错误或畸形的模型调用压掉必要的确定性工具；
- 规划账本有界（`MAX_PLANNER_LEDGER_ENTRIES` / `MAX_PLANNER_LEDGER_BYTES`），不保留无界 transcript，也不落原始推理文本；
- 上下文传输预算明确：历史取最近 `MAX_CONTEXT_HISTORY`（8）条、单条消息 `MAX_CONTEXT_MESSAGE_CHARS`（2,000）字符，订单摘要与工具结果各限 `MAX_CONTEXT_ORDER_BYTES` / `MAX_CONTEXT_TOOL_RESULT_BYTES`（12KB）；后续原生轮次不重复发送完整工具 schema。

模型完全不发起工具调用时，规则 Agent 会按本地意图补齐必要的校验、估价、术语解释或工艺推荐。这是兜底，不是把决策权交回模型。

### MCP server

`mcp_server.py` 是零依赖 JSON-RPC stdio MCP server（协议版本 `2024-11-05`，兼容 `2025-03-26`），面向 dsh 这类 MCP 客户端。

- **会话绑定**：session id 必须匹配 `^[A-Za-z0-9_-]{1,64}$`，由受信宿主在启动 launcher 前提供；
- **能力分级**：L0 只含只读工具——内部 8 个工具中的 `validate_order`、`recommend_processes`、`explain_print_term`、`estimate_price`、`match_supplier_capability`，加上 MCP 层新增的 `get_order_context`；L1 追加 `preflight_file`、`prepare_handoff`、`request_supplier_quote` 三个内部工具，以及 MCP 层新增的 `apply_order_patch`；**L2（外部副作用）不由本 server 实现**，必须放在独立进程中。因此 MCP 对外共 10 个工具，比内部注册表多出的两个只在 MCP 层存在；
- **不转发内部 schema**：对外暴露审阅过的 MCP schema，而不是内部 `TOOL_SCHEMAS` 原文；
- **单一执行入口**：所有工具执行都经过 `Agent.call_tool()`，状态机、校验、审计和人工确认门保持权威；
- 字符串长度、`inspection` 元数据与请求体都有字节上限（`MAX_STRING_LENGTH`、`MAX_INSPECTION_BYTES`、`MAX_MESSAGE_BYTES`）。

### dsh launcher 与本地 fallback host

- `tools/dsh_mcp_launcher.py` 为一个受信 dsh 会话启动 stdio MCP server。dsh 以 argv 数组启动子进程且不展开 shell 变量，因此 session id 必须由受信宿主提前注入；launcher 刻意不提供 `--allow-any-session`，`--dry-run` 用于检查 profile 应使用的 argv。
- `tools/printops_local_host.py` 是 dsh 无法安装时的本地 fallback：只用 Python 标准库。它通过同一个 stdio launcher 验证 skill/MCP 契约，再把对话交给确定性 Agent。
- 两者都只是本地内核的宿主，**不宣称 dsh 运行时存在或正在调用远程模型**。真实 dsh headless、真实模型端到端与 on/off parity 仍是发布门槛（见 `docs/RELEASE_CHECKLIST.md` 门槛 12）。

## 前端渲染边界

`markdown.js` 是对话气泡与输入框预览共用的零依赖 Markdown 渲染器，浏览器与测试环境共享同一实现。设计取舍：

1. **先转义、再解析**：原始 HTML 永远按文字显示，模型或用户输入无法注入标签；
2. **只覆盖真实出现的语法**：标题、列表、表格、代码块、引用、链接与加粗，不追求完整 Markdown 兼容；
3. **不解析 `_斜体_`**：避免误伤 `a_b_c.pdf` 这类文件名；
4. **容忍未闭合标记**：输出被截断时余下文字仍按纯文本展示，不会整段吞掉；
5. **链接白名单**：只放行 `http(s)` 与 `mailto`。

发送给后端的始终是输入框原始文本，渲染只影响展示，`nlu.py` 的解析路径不变。

静态资源由 `server.py` 的 `STATIC_FILES` 白名单控制（`/`、`/index.html`、`/app.js`、`/markdown.js`、`/styles.css`）。白名单保存在进程内存中，修改代码后必须重启服务，否则新增资源会返回 404。

知识清单由 `product_knowledge.py` 的 `KNOWLEDGE_MANIFEST` 维护，包含版本、复核日期、来源范围、成品/展开/刀模/包装三维尺寸定义和人工确认策略。`/api/products` 返回完整清单，订单校验、估算、供应商能力匹配和 Agent 会话响应返回 `knowledgeVersion`；供应商静态能力另带 `supplierProfileVersion`。版本元数据用于追溯规则依据，不表示来源具有实时产能、价格或交期保证。

## 订单数据契约

数量保留兼容 UI 的展示字段 `quantity`，同时写入 `quantityValue`（数字）和 `quantityUnit`（张、份、本、个、块等）。规则感知、模型补丁和旧 SQLite 会话加载都会经过同一套规范化；明确单位优先保留用户输入，未提供单位时才按品类选择默认值。报价、供应商适配器和评测应使用数值/单位字段，不要从展示文本猜测单位。

尺寸保留兼容展示字段 `size`，同时写入 `dimensions`：`finishedSize`（成品平面尺寸）、`expandedSize`（展开尺寸）、`dieCutSize`（刀模尺寸）和 `packageSize`（包装长宽高）。没有标签的旧尺寸会按二维/三维和品类做保守迁移；带“成品尺寸/展开尺寸/刀模尺寸”等标签的输入按标签写入，包装三维尺寸不会被交接文本误称为成品平面尺寸。模型或用户修改尺寸时只允许更新这四个白名单键，生成交接前仍要求供应商或人工确认。

文件预检分两层：浏览器在本地读取 PDF 头、页数和有限对象标记，再把 `inspection` 元数据传给 `preflight_file`；服务端统一生成检查项和人工确认提示，并在存在订单平面尺寸时比对 TrimBox/MediaBox。原始 PDF 不经过本地服务，轻量扫描也不对出血、色彩、分辨率或字体做生产级放行结论。

供应商交接分三层：标准订单 -> `match_supplier_capability` 能力匹配 -> `SupplierAdapter.map_order` 字段映射。`request_supplier_quote` 只生成 `awaiting_human_confirmation` 请求，包含 `requestId`、稳定 `idempotencyKey`、能力状态和映射后的订单；相同订单、平台和产品项会复用活动请求，不重复生成。请求持久化在会话的 `quoteRequests` 中，支持 `quote_status` 查询和 `cancel_quote` 取消；订单字段、文件或目标平台变化会将活动请求标记为 `stale`。只有取得明确人工确认后，未来的真实适配器才允许发起外部请求。`generate` 生成的交接单和 `confirm` 的人工确认状态会持久化到会话。订单含多个未完成产品项时，询价、估价、推荐和交接工具统一返回 `blocked`，不生成合并结果。每个 `items[]` 记录稳定 `itemId`，`validate_order` 返回 `itemValidations`；对话可通过 `activeItemIndex` 或 API `itemIndex` 将字段更新限制在一个产品项内。

## Runtime 记录

接口响应中的 `workflowStage`、`decision`、`runId`、`runTrace`、`fieldMeta`、`conflicts` 和 `quoteRequest` 是面向 UI、评测和后续 Agent 编排层的稳定数据。工作流阶段包括 `collect`（需求收集）、`clarify`（品类澄清）、`recommend`（方案选择）、`preflight`（文件预检）、`quote`（报价准备）、`confirm`（订单确认）和 `export`（导出交接）。`runTrace` 只记录步骤状态、工具名、耗时和安全摘要，不记录 API Key；工具输入/输出契约由 `TOOL_SCHEMAS` 提供。模型规划最多三轮，网络瞬态错误最多短重试一次；询价请求具备本地持久化、幂等、取消和失效状态，真实供应商提交仍需要后续受控适配器。

## Agent 决策边界

1. 感知：从自然语言抽取印刷品、数量、开本/自定义尺寸、页数、方向、纸张、颜色、后道、装订、交期和预算，并识别三折页、二/三联、天地盖、标签面材、袋体/提手、纸杯容量、喷画介质等品类参数；一句话中出现多个产品时保存为独立的 `items`，按数量与产品提及距离进行初步归属，公共参数会复制到缺少该字段的产品项。
2. 记忆：只接受白名单字段；用户修改关键字段后自动清除旧方案和已生成草稿，避免订单参数过期。
3. 解释：遇到“怎么选/有什么区别”等问题，调用 `explain_print_term` 给出面向非专业用户的解释。
4. 决策：必填字段完整后调用 `recommend_processes`，每个方案返回适用场景、成本、交期和风险；多产品订单必须先拆分，不允许生成合并方案。
5. 审核：`validate_order` 输出通用缺失项，以及当前品类的 `productMissing`、品类信息度、风险和建议；生成订单前再次校验。
6. 模型协作：`llm_adapter.py` 只接受白名单字段和工具名；`POST /api/model/test` 可测试当前或弹窗中填写的配置，不发送订单数据、不返回 Key。

核心订单字段保持平台无关，真实平台只负责字段映射和提交；任何外部提交都必须经过人工确认。
