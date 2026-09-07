# 路径三：受控供应商接入（path-03-supplier-integration）

> 优先级 P0　目标版本 v1.1.0　预估工作量 10-15 人日　依赖：路径 4（五件套基础设施）、路径 9 前两项（审计与 keyring）

## 1. 现状与问题

### 1.1 当前行为

- `supplier_adapters.py` 定义了平台能力档案（`PLATFORMS`，含品类范围、最大尺寸、交期参考）与按品类分层的字段映射协议（`"*"` 通用映射 + 按品类覆盖，支持 `productSpecs.<key>` 路径）——这是为真实平台预留的结构，但当前**全部是静态示例数据**，不发生任何外部请求；
- 询价链路已在本地闭环：`request_supplier_quote` 工具生成待确认询价（`awaiting_human_confirmation`），幂等键 `quote_idempotency_key`（订单数据 + 平台 + itemId 的稳定哈希）已实现，重复请求复用原请求，订单变化置 `stale`，人工确认后转 `confirmed`——**但仍不会向任何平台发起真实请求**；
- README 与产品内文案均已明确承诺"当前不会向盛大印刷或其他平台发起真实报价、下单或上传文件请求"。

### 1.2 为什么要做

订单闭环的最后一公里是"把确认过的订单交给供应商"。没有这一步，系统价值止步于"整理得很好的草稿"。路线图将 v1.1.0 定义为受控供应商接入：先报价草稿、交期查询、字段导出三类低风险接口，再考虑真实提交。本路径就是把这条链路按受控方式打通。

## 2. 目标与非目标

### 2.1 目标

1. 定义并落地**供应商适配器正式接口**：能力档案、报价草稿、交期查询、字段导出四类方法，核心 Agent 不写死任何平台名；
2. 落地**外部请求五件套**：幂等键、超时、重试（带类别区分的退避）、取消、审计——缺一不得上线；
3. **沙箱模式**为默认：所有适配器先以 dry-run 运行（生成"将要发送的请求"记录而不发送），用于联调与演示；
4. **人工确认门禁**贯穿：只有 `confirmation.status == "confirmed"` 的交接单才可能触发导出/报价类外部请求，且每次外部请求前再次确认；
5. 契约测试：以 fake 供应商 HTTP 服务覆盖全部适配器行为的超时/重试/错误分类。

### 2.2 非目标

- 不做真实下单、不做文件上传、不做支付（真实提交是更远期目标，见 5.5）；
- 不做供应商返回价格的自动采纳——外部返回值一律标注来源，仅供人参考；
- 不在本路径内做多平台并发比价（架构预留，实现靠后）。

## 3. 方案设计

### 3.1 适配器接口（正式化）

```python
class SupplierAdapter(Protocol):
    id: str                      # 平台稳定标识，如 "shengda"
    profile_version: str         # 能力档案版本

    def capabilities(self) -> dict: ...
        """品类/尺寸/工艺/交期能力档案（静态，带版本）。"""

    def quote_draft(self, request: QuoteRequest) -> QuoteDraftResult: ...
        """报价草稿：只读询价意向，不产生对用户的承诺。"""

    def lead_time(self, request: LeadTimeRequest) -> LeadTimeResult: ...
        """交期查询：按品类/数量/工艺给出参考区间。"""

    def export_fields(self, handoff: Handoff) -> ExportResult: ...
        """字段导出：把确认后的交接单映射为目标平台字段表，返回结构化数据。"""
```

实现约束：

- 所有方法**同步阻塞 + 短超时**（连接 5s / 读 15s，路径 4 统一定义超时分级）；
- 网络层统一走一个新的 `supplier_http.py`（复用 `urllib.request`），内建：五件套挂钩、错误分类（`Transient` / `Permanent` / `AuthFailed` / `ContractViolation`）、请求/响应摘要（**不含**用户敏感字段与凭据）写入审计；
- 适配器注册表沿用现有 `ADAPTERS` 模式，新增平台 = 新增一个文件 + 能力档案 + 映射表 + 契约测试，核心零改动。

### 3.2 五件套落地明细

| 件 | 现状 | 本路径动作 |
| --- | --- | --- |
| 幂等键 | `quote_idempotency_key` 已用于本地询价 | 扩展到全部三类外部请求：`{platformId, kind, itemId, handoffHash}`；同键请求直接返回缓存结果并标记 `replayed` |
| 超时 | 无外部请求 | 连接 5s / 读 15s，按方法可覆盖；超时归类 `Transient` |
| 重试 | 无外部请求 | 仅 `Transient` 重试：指数退避（1s/2s/4s）+ 抖动，最多 3 次；`Permanent/ContractViolation` 不重试 |
| 取消 | 本地询价取消已有 | 外部请求不可真正取消已发出的调用，但语义上取消 = 不再等待并记录 `cancelled`；排队中的请求可取消 |
| 审计 | 无 | 新增 `data/audit.sqlite3`（或复用主库新表）`audit_log(id, ts, platform, kind, request_id, idempotency_key, request_digest, response_digest, outcome, latency_ms)`；只记摘要与哈希，不记原文，避免敏感内容落盘 |

### 3.3 沙箱模式（默认开启）

`data/llm_config.json` 同级新增 `data/supplier_config.json`：`{"mode": "sandbox"|"live", "platforms": {...}}`。`mode=sandbox` 时所有外部调用被拦截：适配器内部生成规范请求摘要 → 直接返回构造的演示响应（带 `"sandbox": true` 标记）→ 审计记录 `outcome=sandbox`。UI 上沙箱结果统一加"演示数据"角标。切到 `live` 需要：配置真实凭据（走路径 9 的 keyring）+ UI 二次确认 + CHANGELOG 级别的用户告知。**v1.1.0 只交付 sandbox 模式的完整链路 + live 模式的只读三类**。

### 3.4 确认门禁

复用并收紧现有确认状态机：

- 报价草稿/交期查询：允许在 `workflowStage == "quote"` 且订单完整时发起（属于"询价准备"，本来就要求先选方案）；
- 字段导出：仅允许 `confirmation.status == "confirmed"` 的交接单；导出前 UI 弹出确认对话框（列出目标平台、导出字段数、沙箱/实时标识），确认后执行并刷新询价/导出状态。

### 3.5 供应商响应的信任边界

外部返回的一切内容（价格、交期、错误文案）都视为**不可信输入**：

- 价格/交期只写入 `quoteRequests[].external` 子结构，UI 强制"外部返回，仅供参考"角标，不参与 `estimate_price` 的内部估价逻辑（内部估价仍以带版本的示例价格表为准）；
- 错误文案长度截断（200 字符）+ 白名单字符过滤后才能进入对话流；
- 响应结构不符合能力档案约定 → `ContractViolation`，记审计并向用户呈现通用错误，不透传原文。

## 4. 接口与数据契约

新增 API（全部过 `_guard()`，全部要求确认语义的显式 POST）：

| 端点 | 用途 | 返回要点 |
| --- | --- | --- |
| `POST /api/supplier/quote-draft` | 发起报价草稿 | `{quoteRequest, sandbox, source:"external"}` |
| `POST /api/supplier/lead-time` | 交期查询 | `{leadTime, sandbox}` |
| `POST /api/supplier/export` | 字段导出 | `{export, fieldMap, sandbox}` |
| `GET  /api/supplier/status` | 各平台连通性与模式 | `{platforms:[{id, mode, profileVersion, lastOutcome}]}` |

`quoteRequests[]` 条目扩展（向后兼容，新增键仅追加）：

```json
{
  "requestId": "…", "status": "awaiting_human_confirmation",
  "external": {"platform": "shengda", "kind": "quote_draft", "sandbox": true,
               "fetchedAt": "…", "resultDigest": "…", "referencePrice": null}
}
```

会话 schema：`STATE_SCHEMA_VERSION` 4（v0.13.0 的文件工作流已占用 3），迁移在 `normalize_state` 中给旧会话补默认（`external: null`）。

## 5. 分阶段实施步骤

| 阶段 | 内容 | 验证 | 预估 |
| --- | --- | --- | --- |
| A | `supplier_http.py`：超时/重试/取消/审计/错误分类（不接任何平台） | 单测：fake HTTP 服务覆盖超时、5xx 重试、4xx 不重试、取消、审计落盘 | 2 天 |
| B | 适配器接口正式化 + 盛大印刷示例适配器（能力档案已有数据）+ 沙箱拦截层 | 契约测试全绿；sandbox 下全链路演示 | 2 天 |
| C | 三个只读端点接入 Agent/会话状态 + `STATE_SCHEMA_VERSION` 4 迁移 | 契约测试 + 迁移测试 + 评测回归 | 2 天 |
| D | UI：平台状态页、询价卡片的外部结果角标、导出确认对话框 | 手动矩阵 + Playwright 用例 | 2 天 |
| E | live 模式（只读三类）+ keyring 凭据读取（路径 9）+ 限流（路径 9）联调 | 真实平台联调记录 | 2-4 天 |
| F | 文档：README 安全边界改写（去掉"不会发起真实请求"承诺的适用范围说明）、CHANGELOG、平台接入指南（新平台 how-to） | 文档评审 | 0.5 天 |

### 5.1 契约测试清单（fake supplier server）

1. 正常报价草稿：请求摘要符合字段映射；幂等重放返回相同结果 + `replayed: true`；
2. 读超时 → 重试 2 次后成功；持续超时 → 3 次后 `Transient` 失败，UI 呈现可重试；
3. 4xx（如平台参数拒绝）→ 不重试，`Permanent`，错误摘要进对话流；
4. 响应缺字段 / 价格字段类型错误 → `ContractViolation`，原文不透传；
5. 取消：排队中取消成功；执行中取消标记 cancelled 且不更新会话外部字段；
6. 审计断言：上述每条案例后 `audit_log` 的行数、outcome、digest 正确；
7. 沙箱：任何 live 配置缺失时自动 sandbox，不发起 socket 连接（用"监听端口计数为 0"断言）。

## 6. 测试与验收

- 全部契约测试 + 既有 149 测试回归 + 评测回归（外部字段不进入字段准确率统计口径，需在 evaluate_agent 显式排除）；
- 沙箱全链路走查：确认订单 → 发起报价草稿 → 观察演示结果角标 → 导出字段 → 审计可查；
- Windows CI：fake supplier server 在 Windows runner 上稳定（注意端口回收等待）；
- 安全复核：凭据不进日志/审计/导出（secret_scan 人工复核 + 审计 digest 复核）。

## 7. 风险与对策

| 风险 | 概率 | 对策 |
| --- | --- | --- |
| 真实平台接口与公开资料不符 | 高 | 适配器实现严格以联调记录为准；能力档案版本随联调更新；无法核实的字段宁缺毋滥 |
| 用户误解沙箱结果为真实价格 | 中 | 全链路"演示数据"角标 + 导出确认对话框显式标注模式 |
| 外部请求拖慢聊天主链路 | 中 | 外部请求与聊天完全异步（独立端点、独立会话锁粒度），聊天永不等待平台 |
| 审计表无限增长 | 中 | 审计保留 90 天，启动时清理；digest 前先压缩 |
| 平台凭据泄露 | 低但致命 | keyring 优先（路径 9）；环境变量次之；明文文件仅作为降级并强警告；凭据永不进审计/日志/导出 |

## 8. 工作量与验收门禁

10-15 人日。v1.1.0 门禁：契约测试 7 组全绿、沙箱全链路演示通过、live 只读三类至少一家平台联调记录、审计可查、CHANGELOG 明确告知"外部交互已启用及其边界"、README 安全边界章节重写。

## 9. 依赖与后续

- 强依赖路径 4 的超时/重试基座与路径 9 的审计/keyring/限流（排期上路径 4 → 路径 9（部分）→ 本路径）；
- 远期（v1.2+）：真实提交（含文件上传）、多平台比价、价格回写——均需在本路径的审计与确认框架内追加，不新开旁路；
- 实施记录：〔待填〕

## 10. 任务卡分解

| 卡号 | 阶段 | 任务 | 改动文件 | 验收标准 | 预估 |
| --- | --- | --- | --- | --- | --- |
| S3-01 | A | 错误分类体系（Transient/Permanent/AuthFailed/ContractViolation）+ 异常类型 | supplier_http.py（新） | 四类异常单测；分类映射表评审 | 0.5d |
| S3-02 | A | 超时/重试/退避（复用路径 4 retry_plan，supplier 档） | supplier_http.py | fake server：超时重试 2 次成功；持续失败 3 次终止 | 0.5d |
| S3-03 | A | 幂等缓存（键扩展到三方法 + replayed 标记） | supplier_adapters.py | 同键二次调用不发 socket（连接计数为 0 断言） | 0.5d |
| S3-04 | A | 审计挂钩（请求/响应摘要 + outcome + latency） | supplier_http.py、agent.py | 每类调用后 audit_log 断言；digest 不含敏感原文 | 0.5d |
| S3-05 | B | 适配器 Protocol 正式化 + 注册表重构（保持既有 ADAPTERS 兼容） | supplier_adapters.py | 既有测试零修改通过 | 0.5d |
| S3-06 | B | 盛大示例适配器（capabilities/quote_draft/lead_time/export_fields 四方法） | suppliers/shengda.py（新） | 契约测试全绿；能力档案版本随实现更新 | 1d |
| S3-07 | B | 沙箱拦截层 + supplier_config.json + 演示响应构造 | supplier_adapters.py、server.py | sandbox 下 socket 连接数为 0；响应带 sandbox 标记 | 0.5d |
| S3-08 | C | 三个只读端点接入 + STATE_SCHEMA_VERSION 4（external 键）迁移 | server.py、agent.py、order_model.py | 迁移测试（旧会话补默认）；契约测试 | 1d |
| S3-09 | C | 确认门禁：export 仅 confirmed；quote/lead 需订单完整 | agent.py | 门禁负向测试（未确认/未完整 → blocked） | 0.5d |
| S3-10 | D | 平台状态页（GET /api/supplier/status 前端呈现） | app/features/quote.js（迁移后） | 模式/版本/最近结果可见 | 0.5d |
| S3-11 | D | 询价卡片外部结果角标 + "演示数据"标识 | app/render/quote.js | 沙箱/实时两种角标快照 | 0.5d |
| S3-12 | D | 导出确认对话框（平台/字段数/模式/确认按钮） | app/features/quote.js | 未确认不可导出；确认后调用并刷新状态 | 0.5d |
| S3-13 | E | live 模式开关 + keyring 凭据读取联调（路径 9-C） | supplier_config 读取链 | 真实平台联调记录归档 | 1-3d |
| S3-14 | E | 限流接入（供应商组阈值） | server.py | 超限 429 + Retry-After | 0.25d |
| S3-15 | F | README 安全边界改写 + 平台接入指南 + CHANGELOG | 文档 | 文档评审 | 0.5d |

## 11. 字段映射示例（shengda.quote_draft，节选）

| 订单侧路径 | 平台字段 | 类型 | 转换规则 | 缺失策略 |
| --- | --- | --- | --- | --- |
| `items[i].productType` | CategoryName | string | 品类字典映射（画册→画册/宣传册） | 必填，缺失拒绝 |
| `items[i].quantityValue` | Quantity | int | 直接使用（平台起印量校验由对端负责） | 必填 |
| `items[i].dimensions.finishedSize` | SizeText | string | `210×285MM` → `"210*285mm"`（平台用星号+小写） | 必填 |
| `items[i].paper` | PaperName | string | 品类分层映射表查 `paper` 别名 | 缺失填 `""` 并标记 warning |
| `items[i].printing` | PrintColor | enum | 双面四色→`4/4`，单面四色→`4/0`，黑白→`1/1` | 必填 |
| `items[i].productSpecs.binding` | AfterProcess[] | array | 骑马钉/胶装→平台码表 | 可选 |
| `handoff.confirmation.confirmedAt` | HasConfirmed | bool | 必须 true 才允许调用 | 服务端门禁 |

映射表数据结构沿用现有品类分层协议（`"*"` + 品类覆盖 + `productSpecs.<key>` 路径），上表即该协议的一个实例——实现时写成数据而非代码。

## 12. 详细测试矩阵（契约测试 7 组 × 断言）

| 组 | 场景 | 关键断言 | 卡号 |
| --- | --- | --- | --- |
| 1 | 正常报价草稿 | 请求摘要与映射表一致；响应解析完整；审计一行 outcome=ok | S3-02/04/06 |
| 2 | 幂等重放 | 同键第二次调用：连接数 0、`replayed:true`、结果与首次相同 | S3-03 |
| 3 | 超时重试 | 读超时 → 2 次重试后成功；持续超时 → 3 次 Transient 失败、可重试提示 | S3-02 |
| 4 | 4xx | 不重试；Permanent；错误摘要 ≤200 字符进对话流 | S3-01 |
| 5 | 契约违反 | 缺字段/类型错误 → ContractViolation；原文不透传；审计 outcome=contract | S3-01 |
| 6 | 取消 | 排队中取消 → cancelled；执行中取消 → 不写外部字段、审计 cancelled | S3-02 |
| 7 | 沙箱 | 无 live 配置自动 sandbox：零 socket、响应带 sandbox、审计 outcome=sandbox | S3-07 |

## 13. 关键实现草图：适配器接口与五件套执行器

路径 3 是全计划书唯一与外部系统交互的路径，实现草图的意义在于把"五件套"（幂等、
超时、重试、取消、审计）从口号变成接口签名——任何适配器实现不满足这些签名，契约
测试直接失败。

### 13.1 适配器统一接口（supplier_adapters.py 扩展）

```python
class ControlledSupplierAdapter(SupplierAdapter):
    """受控供应商适配器：只读三类动作 + 强制五件套。"""

    capability: str                      # "quote_draft" | "lead_time" | "field_export"
    sandbox_base_url: str                # 沙箱地址，live 地址由受控配置注入

    def prepare(self, order: dict, item_id: str | None) -> dict:
        """订单 → 平台字段映射（复用现有 category 字段分层协议），纯函数。"""

    def execute(self, payload: dict, ctx: ExecutionContext) -> ExecutionResult:
        """唯一的外呼入口。ctx 携带五件套，实现不得自行发请求。"""

class ExecutionContext:
    idempotency_key: str                 # 复用 quote_idempotency_key 扩展版
    timeout_s: float = 8.0               # 硬上限，低于 LLM 的 20s
    retry_policy: RetryPolicy            # 按错误类别（连接/5xx/429）指数退避
    cancel_token: CancelToken            # 会话级取消（/api/quote/cancel 复用）
    audit_sink: AuditSink                # 全程留痕（请求摘要/响应摘要/耗时/结果）
```

### 13.2 五件套执行器（唯一外呼通道，agent.py 不直接持有 socket）

```python
def execute_controlled(adapter, payload, ctx) -> ExecutionResult:
    if not CONFIRMED_BY_HUMAN:                      # 人工确认闸门：非确认态直接拒绝
        return ExecutionResult(status="blocked", reason="awaiting_human_confirmation")
    with AUDIT.record(ctx, payload) as audit:       # 审计先于请求：先写意图后写结果
        for attempt in ctx.retry_policy:
            if ctx.cancel_token.cancelled:
                return ExecutionResult(status="cancelled", audit_ref=audit.ref)
            try:
                raw = urlopen_controlled(adapter, payload, ctx.timeout_s)   # SSRF 校验复用 llm_adapter
                audit.set_response(raw)
                return adapter.parse(raw)
            except Retryable as error:
                audit.retry(error)
                sleep(ctx.retry_policy.backoff(attempt))
        return ExecutionResult(status="failed", audit_ref=audit.ref)
```

实现约束：执行器放 `supplier_execution.py`（新文件，约 200 行）；`ExecutionResult`
带 `status ∈ {ok, blocked, cancelled, failed, stale}`，与现有询价状态机语义对齐；
审计记录含幂等键、payload 摘要（字段名 + 长度，不含订单值明文）、响应摘要、每次
重试时间戳，落 `data/audit/YYYYMM.jsonl`（按月滚动，比照附录 D 的 JSONL 纪律）。

### 13.3 沙箱目录与配置形态

```
data/supplier_config.json     → { "platformId": "shengda", "mode": "sandbox"|"live",
                                  "liveEnabled": false }   # live 默认 false，双确认才可置 true
```

`mode=sandbox` 时 `execute_controlled` 把 base_url 钉到 `sandbox_base_url` 且审计
outcome 强制标 `sandbox`；`liveEnabled` 的翻转要求：契约测试 7 组全绿 + 走查归档 +
CHANGELOG 显式记录。零配置时（无 supplier_config.json）全部动作返回 sandbox 演练
结果——保证"没有配置就永远不会外呼"。

### 13.4 契约测试的执行位（对应 §12 七组）

每组契约测试 = fake 沙箱服务器（标准库 `http.server` 起本地端口）+ 断言五件套行为：
幂等键相同不重发、超时中断且审计 failed、重试按退避发生、取消在重试间隙生效、
未确认态返回 blocked、审计行完整、sandbox 标记存在。fake 服务器是路径 1 fake SSE
服务器的姊妹实现，共用 `tests/fake_remote.py` 基座。

### 13.5 与人工确认闸门的衔接

现有确认流（`/api/confirm` → confirmation.status=confirmed → 询价转 confirmed）
扩展一字段：`confirmation.scope = "handoff" | "external"`。生成本地交接单只要求
`handoff` 作用域；发起外部执行必须存在 `scope=external` 的确认记录且其时间戳晚于
最近一次订单变更（stale 确认不算数）。该字段进入 STATE_SCHEMA_VERSION=3 迁移。
