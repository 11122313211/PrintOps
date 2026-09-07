# 路径四：并发与可靠性治理（path-04-reliability）

> 优先级 P1　目标版本 v0.12.0　预估工作量 2-3 人日　依赖：无（与路径 1 同期实施最优）

## 1. 现状与问题（逐项对应 ROADMAP P1/P2 遗留）

1. **全局 LLM_LOCK 串行化**：`server.py` 的 `LLM_LOCK` 在 `/api/settings`、`/api/model/test` 之外并未覆盖 chat（chat 直接使用共享 `PLANNER`），但 `PLANNER.last_error` 是实例级共享状态——两个会话并发聊天时，A 会话的错误信息会被 B 会话覆盖，UI 显示的"上次调用失败"可能张冠李戴；`configure()`（保存设置）也可能在另一线程的 `plan()` 进行中改写 `base_url`。
2. **重试策略形同虚设**：`MAX_ATTEMPTS = 2`、固定 80ms 退避，429（限流）与 5xx、网络错误同等对待，无 `Retry-After` 读取、无抖动。
3. **会话与运行记录无限增长**：`sessions` 表每会话全量 JSON 重写且从不清理；`SESSION_LOCKS` 字典只增不减（每会话一把 `threading.Lock`，长期运行泄漏）；`quoteRequests` 有 40 条上限、`runHistory` 20 条、messages 80 条（这些已封顶，问题集中在 sessions 行数与锁表）。
4. **单条消息无长度上限**：`/api/chat` 的 `text` 只有整体 1MB body 限制；超长文本会撑爆 NLU 正则回溯与 token 预算。
5. **超时不分级**：`urlopen(timeout=20)` 是单一超时，连接慢和响应慢不可区分。

## 2. 目标与非目标

### 2.1 目标

1. 并发聊天（两个浏览器标签、两个会话）错误信息互不串扰；配置变更不影响进行中的请求；
2. 重试按错误类别分级：429 尊重 `Retry-After`，5xx/网络错误指数退避 + 抖动，4xx 不重试；
3. 存储有界：sessions 行数上限（LRU 淘汰 + 用户可见的历史列表同步收缩）、锁表淘汰；
4. `/api/chat`、`/api/session` 等入口对 `text`/`note` 等自由文本字段加长度上限（4000 字符）与控制字符过滤；
5. 超时分级：连接 5s、读取 20s（模型）/15s（未来供应商）。

### 2.2 非目标

- 不做跨进程并发（单进程多线程已满足本地单用户场景）；
- 不做分布式锁、消息队列；
- 不改会话 schema（不递增 STATE_SCHEMA_VERSION）。

## 3. 方案设计

### 3.1 Planner 状态隔离

方案：把"最近一次错误"从共享实例状态改为**调用级返回**。

- `plan()` / `plan_stream()` 返回值从 `plan | None` 扩展为 `(plan, error)` 元组或 `PlanOutcome` 数据类（`{"plan": …, "error": None | str}`）；
- `Agent._ask_planner` 捕获 outcome.error 写入本次 run 的事件流（`_event("plan", "failed", error)`），不再依赖实例字段；
- `public_config()` 的 `lastError` 仅反映**配置层面**的错误（URL 非法等，configure 时产生），与运行时调用错误分离；
- `configure()` 与 `plan()` 的并发：`PLANNER` 的可变三元组（base_url/api_key/model）改为一次性赋值的不可变快照对象，chat 开始时取快照、全程使用该快照——配置中途变更只影响下一个请求。

### 3.2 重试策略（供聊天与未来供应商共用）

`llm_adapter.py` 内建通用退避助手：

```python
def retry_plan(kind: str) -> list[float]:
    # kind: "chat" -> [0.5, 1.5]；"supplier" -> [1.0, 2.0, 4.0]（路径 3 使用）
    # 每次延迟乘以 uniform(0.8, 1.2) 抖动
```

- 429：读 `Retry-After` 头（秒），无则用退避表；仅重试 1 次（避免加剧限流）；
- 5xx / 408 / 425 / 网络错误：按退避表全量重试；
- 4xx（除上述）与解析失败：不重试；
- 每次重试写 run 事件（"模型第 N 次重试，原因 …"），用户在轨迹面板可见。

### 3.3 存储与锁治理

- **sessions LRU**：`Memory` 增加会话元数据表 `session_meta(id, updated_at, size_bytes)`（同库新表，不递增 schemaVersion——它是服务端内部表）；每次 `save` 更新；启动时与每日首次写入时执行 `prune(keep=200)`：删除最旧的超限行，并同步删除其 `session_locks` 条目。被淘汰会话的前端表现：历史列表不再出现；localStorage 里存的 `sessionId` 失效时走"新建会话"路径（已有兜底）。
- **SESSION_LOCKS 淘汰**：锁获取时若字典超过 512 项，清扫"当前未被持有"的锁（`lock.acquire(blocking=False)` 成功即释放并删除）。清扫与获取同在 `SESSION_LOCKS_GUARD` 内，无竞态。
- **写入放大**（可选优化，v0.12 不做）：会话 JSON 超过 256KB 时 gzip 后存 BLOB，读取时透明解压——先埋点测量再决定。

### 3.4 输入长度与字符治理

`server.py` 新增输入净化助手，应用于 `chat.text`、`confirm.note`、`quote cancel.reason`：

- 长度上限 4000 字符（超限 400 `PAYLOAD_FIELD_TOO_LONG`，错误信息提示"请分多条消息描述"）；
- 剥离 C0 控制字符（保留 \n\t）；
- NUL 字节直接 400。

`evaluate_agent` 语料不受影响（最长用例远小于上限）。

### 3.5 超时分级

`urllib.request.urlopen(request, timeout=...)` 的 timeout 是读超时；连接超时需要通过给 `Request` 换 socket 级设置实现。标准库下最简做法：`http.client.HTTPConnection(host, timeout=connect_timeout)` 手工建连再发请求，或接受"单一 20s 读超时 + DNS/连接由系统栈处理"。**决策：v0.12 采用双超时的 `http.client` 直连实现**（约 30 行，与路径 1 的流式读取共用请求构造函数），避免为连接超时引入 socket 黑科技。

## 4. 接口与数据契约

- `/api/chat` 返回体新增可选键 `plannerError`（仅当本次调用失败且回退规则模式时出现），前端在决策面板显示"模型本次不可用，已用规则模式"——取代从 `public_config().lastError` 读全局错误；
- `/api/settings` 返回的 `llm.lastError` 语义收窄为"配置错误"（文档与 UI 文案同步）；
- 新错误码：`PAYLOAD_FIELD_TOO_LONG`；
- 无 schemaVersion 递增；`session_meta` 表是纯服务端内部结构。

## 5. 分阶段实施步骤

| 阶段 | 内容 | 验证 | 预估 |
| --- | --- | --- | --- |
| A | Planner 状态隔离 + 快照配置（`plan()` 返回 outcome；`_ask_planner` 适配；`public_config` 收窄） | 单测：并发两 Agent 各自错误独立；配置热替换不影响进行中请求（用 fake urlopen 挂起验证） | 1 天 |
| B | 重试退避 + Retry-After + 重试事件 | 单测：429/500/4xx/网络错误四类 × 断言重试次数与延迟区间（patch time.sleep） | 0.5 天 |
| C | sessions LRU + 锁淘汰 + `session_meta` | 单测：251 个会话后最旧被淘汰、锁表 ≤512、淘汰会话加载走新建路径 | 0.5 天 |
| D | 输入净化 + 双超时 + http.client 直连改造 | http 测试：4001 字符 400、控制字符剥离；单测：连接超时/读超时区分 | 0.5 天 |
| E | 前端 plannerError 呈现 + 文档 | 手动 + CHANGELOG | 0.5 天 |

## 6. 测试与验收

- 新增并发测试：两线程同时 `Agent.chat`（不同会话、fake planner 一个成功一个失败），断言各自 run 事件的 error 正确归属——这是本路径的核心验收；
- LRU 压测：循环创建 300 会话，断言行数 ≤200 且最近会话全部存活；
- 长度边界：4000/4001 字符两侧；
- 既有 149 测试回归（注意：`test_v09_features` 中断言 `planner.last_error` 的用例需同步改造为断言 outcome）；
- 验收演示：双开浏览器标签并发对话，决策面板错误提示互不串扰。

## 7. 风险与对策

| 风险 | 概率 | 对策 |
| --- | --- | --- |
| outcome 结构变更破坏既有调用点 | 中 | 保留 `plan()` 兼容包装（返回 None 时读 outcome.error 旧路径）一个版本；全量 grep 调用点 |
| LRU 淘汰误删活跃会话 | 低 | 淘汰按 `updated_at` 排序且只删 keep 之外的；活跃会话每次交互都更新时间戳；淘汰上限保守（200） |
| http.client 直连引入 TLS 细节差异 | 中 | 复用 `ssl.create_default_context()`；SSRF 校验仍走 `normalize_base_url`；契约测试用本地 fake HTTPS 不现实——用 http 目标测试 + 生产仅 https 提示 |
| 消息上限影响超长订单描述用户 | 低 | 错误信息引导分多条；上限可经环境变量覆盖（`PRINTOPS_MAX_TEXT=4000`） |

## 8. 工作量与验收门禁

2-3 人日。门禁：并发正确性测试通过、存储有界测试通过、既有测试回归全绿、CHANGELOG 记录 `plannerError` 语义与新增错误码。

## 9. 依赖与后续

- `retry_plan` 是路径 3 供应商重试的直接基座；
- planner 快照配置为路径 1 流式提供稳定请求上下文；
- 会话 LRU 为路径 8（打包分发）的"数据目录健康"打底；
- 实施记录：〔待填〕

## 10. 任务卡分解

| 卡号 | 阶段 | 任务 | 改动文件 | 验收标准 | 预估 |
| --- | --- | --- | --- | --- | --- |
| S4-01 | A | PlanOutcome 结构（plan/error）+ `plan()` 改造与兼容包装 | llm_adapter.py | 既有 planner 相关单测适配后全绿 | 0.5d |
| S4-02 | A | `_ask_planner` 消费 outcome → run 事件；快照配置（configure 产不可变快照） | agent.py、llm_adapter.py | 并发归属单测（双会话错误互不串扰） | 0.5d |
| S4-03 | A | `public_config().lastError` 语义收窄为配置错误 | llm_adapter.py、server.py、app.js 文案 | 设置面板展示"配置错误"专用文案 | 0.25d |
| S4-04 | B | retry_plan 助手 + 429 Retry-After + 类别分支 | llm_adapter.py | 四类错误 × 重试次数/延迟断言（patch sleep） | 0.5d |
| S4-05 | B | 重试过程写 run 事件（第 N 次重试） | agent.py | 轨迹面板可见重试提示 | 0.25d |
| S4-06 | C | session_meta 表 + save 更新时间戳 + prune(keep=200) | agent.py | 251 会话压测：行数 ≤200、最新全存活 | 0.5d |
| S4-07 | C | SESSION_LOCKS 淘汰（>512 时清扫未持有锁） | server.py | 锁表上限单测 | 0.25d |
| S4-08 | D | 输入净化助手（4000 字符上限、控制字符剥离）+ `PRINTOPS_MAX_TEXT` 覆盖 | server.py | 4000/4001 边界；控制字符剥离；环境变量生效 | 0.25d |
| S4-09 | D | http.client 直连 + 连接/读取双超时（与路径 1 共用请求构造） | llm_adapter.py | 连接超时与读超时分类单测 | 0.5d |
| S4-10 | E | plannerError 前端呈现 + 文档 | app.js（迁移后 api/panels）、文档 | 决策面板"已回退规则模式"文案可见 | 0.25d |

## 11. 核心数据结构定义

```python
@dataclass(frozen=True)
class PlanOutcome:
    plan: dict[str, Any] | None      # 与现有 plan() 返回结构一致
    error: str | None                # 用户可读的错误描述（已脱敏）
    attempts: int                    # 实际尝试次数（含重试）
    retried_after: float | None      # 最后一次重试等待秒数（429 时有值）

@dataclass(frozen=True)
class PlannerSnapshot:               # configure() 产物，chat 全程使用
    base_url: str
    api_key: str
    model: str
    timeout: int
```

`retry_plan` 展开语义：

| 错误类别 | 判定 | 重试次数 | 延迟策略 |
| --- | --- | --- | --- |
| 限流 | HTTP 429 | 1 | `Retry-After` 头秒数；缺失则 2s×抖动 |
| 服务端瞬时 | 408/425/5xx | 全量 | chat 档 [0.5, 1.5]s；supplier 档 [1, 2, 4]s；均乘 uniform(0.8,1.2) |
| 网络失败 | URLError/Timeout/OSError | 全量 | 同上 |
| 请求缺陷 | 其余 4xx | 0 | 立即失败 |
| 契约失败 | 解析/校验失败 | 0 | 立即失败（错误写 outcome.error） |

## 12. 详细测试矩阵

| 层 | 用例组 | 断言要点 | 卡号 |
| --- | --- | --- | --- |
| 单测 | 并发归属 | 两线程各自 Agent.chat，一败一成，run 事件 error 各自正确 | S4-02 |
| 单测 | 快照隔离 | plan 进行中 configure() 换 URL：进行中请求用旧快照完成 | S4-02 |
| 单测 | 429 | Retry-After: 7 → 延迟 ∈ [5.6, 8.4]；仅重试 1 次 | S4-04 |
| 单测 | 5xx | 重试 2 次、延迟落在档位区间 | S4-04 |
| 单测 | 4xx | 0 次重试立即失败 | S4-04 |
| 单测 | LRU | 300 会话循环写入 → 行数 200、最近 50 全存活、被淘汰会话 load 走新建 | S4-06 |
| 单测 | 锁淘汰 | 制造 600 会话锁 → 清扫后 ≤512、活跃锁未误删 | S4-07 |
| http | 长度边界 | text 4000 通过 / 4001 → 400 PAYLOAD_FIELD_TOO_LONG | S4-08 |
| http | 控制字符 | 带 \x00 文本 → 400；带 \n\t 保留 | S4-08 |
| 单测 | 双超时 | 连接拒绝（连接超时类）与慢响应（读超时类）分类正确 | S4-09 |
| 契约 | plannerError | 规划失败回退时响应含 plannerError，决策面板可见 | S4-10 |
