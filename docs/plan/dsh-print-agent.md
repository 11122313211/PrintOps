# DeepSeek Harness 印刷 Agent 规划

> 状态：PoC 阶段（2026-09-09）
> 范围：纸品/商业印刷订单前置整理、工艺解释、印前信息收集和人工确认前的报价交接
> 本文是架构计划，不引入 dsh 运行依赖，也不改变当前 PrintOps 的生产路径。

> dsh 兼容性快照（2026-09-09）：官方 GitHub tag
> `dsh-v0.1.5-alpha.1`（commit `5dda764ed3aa172535a7967b06ff95d9cbfe536a`）
> 主 CLI 包为 `@deepseek-ai/dsh@0.1.5-alpha.1`，MCP client 为
> `@deepseek-ai/dsh-mcp-client@0.1.5-alpha.1`；npm `alpha` dist-tag 指向
> `0.1.5-alpha.1`，而 npm `latest` dist-tag 仍是
> `0.1.2-rc.1`；根包声明 Node `^22.19.0 || >=24.0.0`、pnpm `11.7.0`。
> dsh 仍明确处于 developer preview，正式接入必须固定具体版本/tag（不要使用
> 浮动的 `latest`），升级后重新做 smoke/parity 验证。
>
> 当前工作环境没有 PATH 中的 dsh、Node 或 npm；本仓库已完成第一方 skills、stdio MCP
> adapter、session-bound L1 `apply_order_patch` bridge 及本地回归，尚未完成真实 dsh
> 进程的端到端联调、on/off parity 或生产启用。

## 1. 决策摘要

可以借助 DeepSeek Harness（`dsh`），但它应当是**旁路编排与评测层**，不是订单领域内核。推荐的责任边界如下：

| 层 | 负责什么 | 不负责什么 |
|---|---|---|
| dsh | DeepSeek 运行、skill 按需加载、对话编排、场景回放和评测 | 订单持久化、最终规则判定、供应商提交 |
| 印刷 skill | 需求理解、追问顺序、术语解释、知识上下文和结构化建议 | 直接写 SQLite、绕过 schema、宣布生产放行 |
| PrintOps MCP adapter | 把受控能力转换为 MCP Tools，绑定会话和权限 | 暴露任意 Python 函数或任意文件路径 |
| PrintOps domain kernel | `Agent` 状态机、订单契约、规则、预检边界、供应商适配、人工确认 | 由模型自行决定高风险副作用 |

一句话架构：**模型负责理解和解释，skill 负责组织知识，MCP 负责受控调用，确定性内核负责约束和执行，人负责确认高风险结果。**

当前仓库已经具备可复用的内核：`agent.py`、`order_model.py`、`nlu.py`、`tools.py`、`product_knowledge.py` 和 `supplier_adapters.py`。因此首期不应重写 Agent，也不应把印刷规则迁移到一个超长 system prompt。

## 2. 目标与非目标

### 目标

1. 在不破坏现有原生 Agent 的前提下，用 dsh 驱动印刷需求收集和工具编排。
2. 让每个模型提出的字段都有原文证据、置信度、知识版本和可审计的 patch。
3. 让 dsh 可以调用 PrintOps 的确定性工具，同时保持 session 隔离、人工确认和无副作用默认值。
4. 建立可重复的 dsh on/off parity 评测、故障回退和安全回归。
5. 为后续颜色、PDF、排版和供应商能力提供可替换的 sidecar 插槽。

### 明确不做

- 不让 dsh 替换 `Agent` 状态机或 SQLite 会话记忆。
- 不在第一期接入真实供应商下单、CUPS 物理打印或 Printful 履约。
- 不允许 `preflight_file` 读取模型指定的任意本地路径；当前仍只接收浏览器提取的元数据。
- 不把第三方 MCP 的“ready/通过”结果当作生产放行结论。
- 不把实时价格、产能、交期写成静态 skill 知识。
- 不因接入本地 DeepSeek/Ollama/vLLM 而直接放宽现有 SSRF 防护。

## 3. 部署模式

先保留三种模式，用 feature flag 控制：

```text
模式 A：原生 PrintOps（当前基线）
浏览器 -> server.py -> Agent -> tools / SQLite

模式 B：dsh 外部编排（PoC）
dsh + print-* skills -> PrintOps stdio MCP -> Agent.call_tool()

模式 C：dsh 与 PrintOps 双向桥接（后续）
Web UI / dsh -> 统一 session-bound adapter -> Agent -> 受控 sidecar
```

模式 A 永远保留为回退路径。模式 B 是首个验证目标：dsh 进程崩溃、超时、返回非法 JSON 或 MCP 不可用时，订单状态不能丢失，也不能被部分写入。

在没有 Node/npm/pnpm 或 DeepSeek 凭据的开发机上，使用
`tools/printops_local_host.py` 作为**纯 Python 标准库验证 host**：它发现项目 skills，
通过同一个 stdio launcher 做 MCP transcript smoke，再把消息交给确定性 PrintOps Agent。
该 host 是 dsh 的本地替代验证路径，不执行真实模型调用，也不改变 dsh 集成默认关闭的策略。

### dsh 运行约束

- dsh 属于开发预览性质，必须锁定精确 npm/package 版本、Node 运行时和 lockfile；本规划的基线是 GitHub tag `dsh-v0.1.5-alpha.1` / npm `alpha` `0.1.5-alpha.1`，不是浮动的 `latest`。
- `.dsh/` 只放 profile、skill 和适配配置；不要把核心业务规则复制成第二份。当前仓库
  附 `.dsh/runtime.lock.json` 与 `.dsh/profile.example/`，后者按 dsh alpha.1 的
  `package.json` + `dsh.profile` + `cordis.patch.yml` 形状提供禁用模板。
- `SKILL.md` 中的 hard boundary 只是模型指导，不是安全边界；本项目的 MCP adapter 强制自定义的 `L0`/`L1` capability，dsh profile 只负责选择插件组合。dsh skill 的 `modelInvocable`、`userInvocable`（以及本地 frontmatter 的 `disable-model-invocation`、`user-invocable`）只控制调用面，不提供订单级权限隔离；不要假设存在 `allowed-tools` 语义。
- DeepSeek 远程调用采用显式 opt-in，发送最小化、脱敏后的文本和订单投影；原始 PDF、API key 和完整审计数据不得发送。
- 本地模型 endpoint 只能通过明确的受信 allowlist 或 stdio adapter 使用，不能关闭环回/私网/重定向防护。
- 当前 PrintOps planner adapter 保留最多三轮工具规划，并在超时或可重试网络错误后回退；这是本项目约束，不是 dsh 的默认轮数。

## 4. 第一方印刷 skills

在本次检索范围内没有发现成熟、官方、专门针对纸品商业印刷订单的 dsh skill。建议自建五个小而可测试的 skill，而不是一个覆盖所有工艺的“大印刷 skill”。

| Skill | 触发时机 | 主要输出 | 权限边界 |
|---|---|---|---|
| `print-intake` | 用户描述印刷需求时 | 字段 patch、原文证据、置信度、必要追问 | 不能写状态 |
| `print-specs` | 已识别品类后 | 品类参数、尺寸语义、缺失项 | 不能猜实时价格 |
| `print-process` | 用户比较工艺时 | 纸张/颜色/后道/装订/合版专版解释和风险 | 只能引用版本化知识 |
| `print-preflight` | 有文件元数据或预检结果时 | 检查项解释、警告、人工复核清单 | 不能宣布生产放行 |
| `print-quote-handoff` | 订单准备交接或询价时 | 完整性摘要、能力匹配解释、报价草稿 | 不能发外部请求 |

以下是本项目定义的 skill 输出契约（不是 dsh 原生 schema），供 adapter 和评测器校验：

```json
{
  "patch": {},
  "evidence": [
    {"field": "quantity", "quote": "500份", "source": "user"}
  ],
  "confidence": {"quantity": 1.0},
  "questions": [],
  "risks": [],
  "knowledgeVersion": "printops-knowledge-<version>"
}
```

约束：

- `patch` 只允许 `order_model` 的白名单字段；`productSpecs` 还要按 `product_knowledge.profile_for(product)` 的参数表做 key/type 校验。
- 未知字段不能静默写入订单；可以作为 `rejectedFields` 返回给评测器。
- 每个关键数字应尽量带用户原文或规则来源；缺少证据的字段可以暂留在草稿，但必须标为低置信度，并在 `generate`、`prepare_handoff` 或询价前阻断。普通 planner 路径会清理并记录证据/置信度；L1 受控 bridge 另执行严格的 provenance 形状校验，并在不合规时拒绝写入。
- 置信度低于现有人工确认阈值时只能生成追问，不能触发 `generate`、`prepare_handoff` 或询价。
- skill 不直接调用 SQLite，也不直接修改 `confirmation`、`quoteRequests` 或供应商状态。

当前切片已提供五个 `SKILL.md`；运行 profile/overlay 在 P3 接入时再创建，目录形状建议为（`cordis.patch.yml` 为拟议文件名，需按锁定版本调整）：

```text
.dsh/
  cordis.patch.yml  # 拟议 profile/overlay 文件名
  skills/
    print-intake/SKILL.md
    print-specs/SKILL.md
    print-process/SKILL.md
    print-preflight/SKILL.md
    print-quote-handoff/SKILL.md
```

`SKILL.md` 应包含 dsh 所需的 YAML frontmatter、kebab-case 名称、触发描述、输入/输出契约、禁止事项和知识版本。复杂规则仍放在 Python 内核或版本化知识 manifest 中，skill 只负责在恰当时机把它们解释给模型。

按照当前 dsh skill registry 约定，项目工作区可放置
`.dsh/skills/<kebab-name>/SKILL.md`，正文按需加载，目录摘要只包含
`name`/`description`；是否被运行时发现仍取决于锁定版本所挂载的
`dsh-skill-filesystem`/`dsh-tool-skill` 等 skill 插件和启动 profile，不能只凭目录存在
就假定已加载。skill 的可调用性策略应显式写入
frontmatter/profile（或对应 dsh 配置），不能把 Markdown 中的权限声明当成执行控制。

## 5. PrintOps MCP adapter

### 首期形态

当前切片已提供一个 dependency-free 的 stdio MCP PoC（`mcp_server.py`，服务名
`printops-mcp`），由父进程启动并连接现有 Python 内核。它不增加公网监听面，也不改
现有 HTTP API；后续确有跨进程/远程需求时，再评估 Streamable HTTP。

基础协议范围：

```text
initialize
tools/list
tools/call
可选：resources/list、resources/read
```

dsh 官方 `@deepseek-ai/dsh-mcp-client` 当前只桥接 MCP Tools，不桥接 Resources/Prompts；因此 Resources/Prompts 不能成为首期运行前提。产品目录、术语和知识版本应先通过只读工具或显式上下文提供。

### 工具分级

| 级别 | 示例 | 首期策略 |
|---|---|---|
| L0 只读/确定性 | `validate_order`、`recommend_processes`、`explain_print_term`、`estimate_price`、能力匹配 | 首期开放，结果标明版本和参考性质 |
| L1 本地状态写入 | 元数据预检、`prepare_handoff`、询价草稿、受限 `apply_order_patch` | 绑定 session，逐工具审计；patch 还要求 provenance、revision/CAS 和幂等校验，默认不开放 L0 |
| L2 外部副作用 | 真实供应商提交、CUPS 打印、Printful 下单 | 第一阶段不注册；独立 server、独立 capability、人工确认后才可用 |

### Session 与 schema 契约

现有 `TOOL_SCHEMAS` 是 provider-neutral 的 `{input, output}` 描述，不是 MCP 标准 `inputSchema`；且部分 schema 声明需要 `order`，但 `Agent.call_tool()` 实际从 session state 读取订单。adapter 不得原样暴露这些内部假设。

建议的调用流程：

1. 当前 stdio PoC 由受信父进程用 `--session-id`（或 `PRINTOPS_MCP_SESSION_ID`）绑定一个会话；它没有 token 握手。未来若需要不受信宿主，再增加 server-side token binding，并保持显式会话校验。
2. 每次 `tools/call` 必须提供并校验显式 `sessionId`，同时核对进程 binding 和工具允许的参数。
3. adapter 从 `Memory`/`Agent` 读取当前订单，调用唯一网关 `Agent.call_tool()`；不直接调用 `TOOLS` 函数。
4. 拒绝未知工具、未知参数、错误类型、越权 `itemIndex`、过大字符串和跨 session 读取。
5. 返回稳定外壳，例如 `{status, toolResult, runId, warnings}`，并保留人工确认和 `knowledgeVersion`。

dsh 侧的 MCP 配置应使用官方 client overlay 的形状，并保持默认关闭；下面的
`failOnStartupError` 字段以 `@deepseek-ai/dsh-mcp-client@0.1.5-alpha.1` 的
配置类型为准，换版本必须重新核对 schema，例如：

```yaml
- insert:
    - id: printops-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: printops
        transport: stdio
        command: python3
        args:
          - !!js '`${process.cwd()}/tools/dsh_mcp_launcher.py`'
          - --session-id
          - !!js 'process.env.PRINTOPS_MCP_SESSION_ID ?? ""'
          - --capabilities
          - L0
        cwd: !!js process.cwd()
        toolCallTimeoutMs: 60000
        failOnStartupError: true
        reconnect:
          enabled: false
```

上面是 alpha.1 配置形状示例；可复制的禁用模板在 `.dsh/profile.example/cordis.patch.yml`。
换版本时必须重新核对字段和启动失败开关。`PRINTOPS_MCP_SESSION_ID` 不是 shell 占位符：
它由 dsh 配置层的 `!!js` 表达式读取，再作为一个 argv 字符串交给受信 launcher；缺失或
非法值会在 launcher 处失败。MCP client 本身不会对 `args` 做 shell 展开。正式接入时应
使用绝对路径、受控环境变量和锁定的 overlay 文件，并在 dsh 版本对应的配置目录中做一次
`tools/list`/`tools/call` smoke test。一个 stdio
MCP 进程只绑定一个 session；需要多个并发 dsh 会话时，为每个会话启动独立 launcher
实例，或先实现带 token/handshake 的共享 adapter。stdio 子进程启动前 dsh 会清洗常见凭据
环境变量和 `DSH_*` 变量；不要依赖继承环境传递密钥。

首期对外 schema 应按 session 语义重写。例如：

```json
{
  "name": "validate_order",
  "description": "校验当前绑定会话的订单，不接受外部 order 覆盖",
  "inputSchema": {
    "type": "object",
    "properties": {"sessionId": {"type": "string"}},
    "required": ["sessionId"],
    "additionalProperties": false
  }
}
```

### 订单 patch 通道（当前 PoC 的明确边界）

skill 返回的 `patch` 仍是建议，不能直接改变 SQLite。当前 `mcp_server.py` 已提供独立
的 L1 `apply_order_patch` bridge：它通过 `OpenAICompatiblePlanner.validate_plan()` 和
Agent 的字段白名单/产品 profile 校验，只接受增量字段，不接受完整 `order`、`items` 或
`productTypes` 替换；多产品订单必须显式绑定一个 `itemId`/`itemIndex`。

每次写入都要求绑定 `sessionId`、`expectedRevision`，并记录 `runTrace`、provenance、
`knowledgeVersion`、accepted/rejected fields。SQLite `BEGIN IMMEDIATE` CAS 保护跨 MCP
实例的竞争写入；`patchId` 加规范化 payload digest 支持安全重试，并拒绝同一 ID 的不同
内容。`L0` 默认不注册该工具，L1 也不会启用供应商、CUPS 等 L2 外部副作用。

bridge 已通过本地契约、边界、幂等和并发回归，但真实 dsh 进程联调、dsh on/off parity、
30 场景验收和生产启用尚未完成。首轮 dsh 验收仍应先限定为只读解释/校验 smoke；随后再
验证受限 patch 的增量落库、回退路径和人工确认门。bridge 失败、版本不匹配或证据不足时
只能返回拒绝/追问，不得部分写入。

`preflight_file` 当前只接收必填的 `fileName`、`sizeBytes`、`encrypted`、`readable`，以及可选的
`pageCount`、`expectedSize`、产品项选择器和浏览器产生的有限 `inspection` 元数据。未来若需要
原始 PDF，必须另设受限工作目录、realpath/symlink 检查、大小/页数/CPU/超时上限和沙箱 worker。

stdio MCP 实例内的调用按会话串行，但 SQLite 目前不提供 HTTP 进程与 MCP 进程之间的全局会话锁。因此 PoC 运行时一个会话只能由一个 owner 写入：dsh 连接 MCP 后，不要同时让浏览器 HTTP 路径修改同一 `sessionId`；需要并行入口时应先落地统一的跨进程锁/队列。

### 错误与审计

当前实现可测试的错误/阻断码包括 `invalid_session`、`unknown_tool`、`invalid_arguments`、
`capability_denied`、`item_required`、`order_not_ready`、`low_confidence`、
`selection_required` 和 `internal_error`；`blocked_by_confirmation`、
`knowledge_unavailable` 作为后续 sidecar/能力层预留。错误消息可以给模型解释，但不得泄露
API key、绝对路径或内部堆栈。

沿用现有 `runId`、`runTrace`、审计摘要和确认状态；记录 dsh fallback、MCP 拒绝、工具耗时和副作用计数，但不记录原稿内容或密钥。

## 6. 外部 skill / MCP 选型

以下是候选，不是首期依赖。下表的活跃度/许可证是 2026-09-09 的检索快照，
只用于排队和风险分层，不等于安全、正确或生产可用。接入前必须重新做源码、
许可证、路径处理、网络副作用和测试审计。

| 项目 | 适合位置 | 结论 |
|---|---|---|
| [`kcgdz/mcp-print`](https://github.com/kcgdz/mcp-print) | 颜色、Delta E、ICC、墨量、拼版、书脊、预检等只读算法 | Tier-A 评估候选（MIT、Python、近期有提交）；作为 Python 3.10+ 隔离 sidecar 或参考实现，不作为生产放行规则；其文件路径能力需单独沙箱，接入前重新核对提交、许可证和测试 |
| [`hanweg/mcp-pdf-tools`](https://github.com/hanweg/mcp-pdf-tools) | 通用 PDF 提取/审计 | 后置（Unlicense、代码更新较旧）；先做恶意 PDF、资源耗尽和路径审计 |
| [`NameetP/pdfmux`](https://github.com/NameetP/pdfmux) | PDF 抽取和检查 | 后置（依赖较重、偏通用抽取）；不能替代印刷规则内核 |
| [`Raymond-Leung7/dsh-md2pdf`](https://github.com/Raymond-Leung7/dsh-md2pdf) | 交接单 Markdown 转 PDF 原型 | 仅读取代码作原型参考（仓库未声明许可证，不作为依赖）；需加固路径校验，不是印刷知识 skill |
| [`lucdesign/indesign-mcp-server`](https://github.com/lucdesign/indesign-mcp-server) | InDesign 排版自动化 | 未来设计生产能力；独立文件和权限沙箱 |
| [`Purple-Horizons/printful-mcp`](https://github.com/Purple-Horizons/printful-mcp) | Printful POD 目录/履约 | 未来供应商适配；网络和下单副作用默认禁用 |
| [`steveclarke/mcp-printer`](https://github.com/steveclarke/mcp-printer) | CUPS 本地物理打印 | 与商业印刷订单不同；仅人工确认后独立 opt-in |

`dsh-print3d`、像素打印、热敏小票等项目针对其他打印场景，不纳入当前纸品商业印刷范围。

仓库内可用的 `pdf:pdf`、`documents`、`spreadsheets` 和 `imagegen` 是 Codex 工作技能，不是 dsh 运行时插件：它们可辅助文档 QA、数据分析和示意图制作，但不应被当作生产印前判断或自动接入 PrintOps。

## 7. 分阶段实施与退出门

### P0：契约冻结与基线（约 1 周）

- 冻结 `Agent.call_tool()`、`schemaVersion`、`fieldMeta`、`runTrace`、`knowledgeVersion`。
- 建立 dsh off/on feature flag、fallback 标记和最小订单投影。
- 固定 dsh/Node 版本，记录许可证和依赖清单。

退出门：现有测试集不回退，111 个合成评测继续通过；dsh 关闭时状态和响应保持现有语义。

### P1：第一方 skills（1-2 周）

- 将现有五个 `SKILL.md` 正式化，补齐统一 patch/evidence contract 和评测 fixture。
- 建立中文完整需求、模糊需求、否定修改、多产品和包装尺寸语义 fixture。
- 只让 skill 产生建议，不给直接状态写权限。

退出门：低置信度生产字段必追问；未知 `productSpecs` 键被拒绝或隔离；不相关 skill 不加载。

### P2：stdio MCP（当前切片已提供 PoC，正式化约 1-2 周）

- 已实现 schema 转换器、session binding、L0/L1 工具、受控 patch bridge 和错误/审计；仓库现附
  `.dsh/runtime.lock.json`、禁用状态的 `profile.example.json`、受信 launcher 和离线
  transcript smoke。正式 dsh client smoke、HTTP/MCP 多入口统一锁和真实 dsh 环境验证仍待完成；
  patch bridge 的 SQLite CAS 已覆盖独立 MCP 实例之间的竞争写入。
- 用 JSON-RPC transcript 测试 initialize、tools/list、tools/call、重启和并发；离线 smoke
  额外验证 launcher 在非仓库 cwd 下仍使用绝对路径，且继承环境不能覆盖显式 session/capability。
- 让内部 schema 与对外 MCP schema 分离，禁止 `order` 覆盖会话状态。

退出门：非法工具/参数和跨 session 访问 100% 拒绝；重启后 SQLite 会话可恢复；未确认时没有外部请求。

### P3：dsh 编排 PoC（1-2 周）

- 先让 dsh 加载 `print-intake`，只调用一个本地 L0 MCP 工具，验证只读 smoke；
- 用已实现的 session-bound patch bridge 验证字段增量落库、CAS 冲突、幂等重试和回退路径。
- 验证 DeepSeek 远程 endpoint 的最小数据投影、超时、429/5xx、非法 JSON 和 native fallback。
- 对比 dsh on/off 的字段、阶段、确认状态和审计摘要。

退出门：至少 30 个代表场景通过；dsh 故障不丢状态、不污染订单、不绕过人工确认。

### P4：印前 sidecar（2-4 周，可选）

- 评估 `mcp-print` 或 PDF 解析器的隔离部署和双算对照。
- 覆盖错尺寸、缺框、加密、畸形、超大 PDF 和路径越权 fixture。

退出门：所有结果都标为参考/需人工复核；任何解析器异常都不能让订单自动 release。

### P5：供应商与外部副作用（后置）

- 先接 sandbox/read-only 能力，再做真实报价。
- 保留 `SupplierAdapter`、幂等键、取消/stale 和短超时。
- 真实提交放在独立 capability，确认前调用计数必须为零。

退出门：确认前外部请求为 0；确认后重复请求复用幂等键；订单变化使旧草稿失效；凭据不进入 trace。

## 8. 风险优先级

| 风险 | 级别 | 主要措施 |
|---|---|---|
| dsh alpha API 变化 | 高 | 精确锁版本、独立 adapter、默认 off、一键回退 |
| 模型越权 patch/工具 | 高 | JSON Schema、字段白名单、二次校验、能力分级 |
| MCP 跨 session 泄露 | 高 | server-side binding、每次鉴权、并发隔离回归 |
| prompt/file injection | 高 | 内容不可信、长度限制、沙箱解析、权限不由文档改变 |
| 原稿/API key 外发 | 高 | 最小投影、脱敏 trace、远程模型显式 opt-in |
| 外部 MCP 规则错误 | 高 | sidecar 隔离、版本/来源、golden 对照、人工复核 |
| Python 3.9/3.10 与 Node 冲突 | 中 | 核心保持零依赖，外部能力使用 sidecar/独立环境 |
| 价格/交期知识漂移 | 中 | `knowledgeVersion`、来源、复核日期，实时值只来自供应商 |
| 多产品串项 | 高 | 稳定 `itemId`、每项 schema、跨项回归 |
| 确认范围含糊 | 高 | 展示具体差异，修改后自动 stale，记录确认摘要 |

## 9. 首批验收场景

1. dsh 关闭：现有测试和合成评测结果不变。
2. dsh 超时、崩溃、非法 JSON：回退规则 Agent，`runTrace` 标记 fallback，订单不丢不污染。
3. 完整中文需求：字段带原文 evidence/confidence，不编造实时价格和交期。
4. 缺数量、尺寸或品类参数：只追问必要字段，不能生成交接单。
5. 包装内尺寸、外尺寸、成品/展开/刀模尺寸分别落入正确字段。
6. 多产品订单：稳定 `itemId`；只修改第二项时第一项方案和询价不失效。
7. “数量改成 1200，不要覆膜”：旧方案/quote 变 stale，重新校验并留审计。
8. MCP 缺 session、未知参数、越权 item、未知工具：明确拒绝。
9. MCP 重启和并发：会话恢复、写入串行、无 SQLite 损坏。
10. PDF 元数据异常、路径 traversal、prompt injection：拒绝或警告，不能改变 capability。
11. 确认前供应商 HTTP 调用计数为零；确认后最多一次并复用幂等键。
12. 至少 20 个脱敏真实案例后再锁定真实字段准确率门槛（建议不低于 95%）；合成案例保持 100%。

## 10. 下一步 PoC

当前切片已经完成第一方 skills、MCP 边界和 L1 patch bridge；下一步正式联调仍保持很小：

1. 使用 `.dsh/runtime.lock.json` 固定 dsh/Node/client 版本；将
   `profile.example.json` 按锁定版本的真实 schema 转换并确认 skill 插件实际加载。
2. 先运行 `python3 tools/dsh_mcp_smoke.py`，再在真实 dsh 中用只读 stdio MCP adapter
   跑 `validate_order` 和 `explain_print_term` 协议 smoke。
3. 在真实 dsh 中验证已实现的 session-bound patch bridge；分别覆盖“建议不落库”和
   “受限增量落库”的契约，并检查 CAS、幂等和 provenance 拒绝路径。
4. 用一个固定 `sessionId` 跑 5 类 transcript：完整需求、缺字段、否定修改、多产品、非法参数。
5. 对比 dsh off/on 的 `order`、`fieldMeta`、`workflowStage`、`runTrace` 和确认状态。
6. 只有 parity 和安全门通过后，才加入 `recommend_processes`、预检和交接草稿。

这一步完成前，不应引入 `mcp-print`、真实供应商 API、CUPS、Printful 或任意原始 PDF 读取。

## 11. 参考资料

- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
- [dsh architecture (alpha.1 tag; commit `5dda764ed3aa172535a7967b06ff95d9cbfe536a`)](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/5dda764ed3aa172535a7967b06ff95d9cbfe536a/docs/architecture.md)
- [dsh skills guide (alpha.1 tag; commit `5dda764ed3aa172535a7967b06ff95d9cbfe536a`)](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/5dda764ed3aa172535a7967b06ff95d9cbfe536a/docs/subsystems/skills.zh.md)
- [dsh MCP guide (alpha.1 tag; commit `5dda764ed3aa172535a7967b06ff95d9cbfe536a`)](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/5dda764ed3aa172535a7967b06ff95d9cbfe536a/docs/user/guide/mcp-memory.zh.md)
- [dsh MCP client decision note (alpha.1 tag; commit `5dda764ed3aa172535a7967b06ff95d9cbfe536a`)](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/5dda764ed3aa172535a7967b06ff95d9cbfe536a/.agents/notes/implemented/feature/2026-07-07-mcp-client-plugin.zh.md)
- [mcp-print](https://github.com/kcgdz/mcp-print)

外部项目的许可证、活跃度和安全性在正式接入前必须重新核对；本文不复制其代码，也不把仓库 README 的“生产就绪”表述视为本项目的验收结论。
