# 路径一：聊天流式与响应感（path-01-streaming）

> 优先级 P0　目标版本 v0.12.0　预估工作量 3-4 人日　依赖：无（与路径 4 同期实施可共享改造点）

## 1. 现状与问题

### 1.1 当前行为

- 前端 `api("/api/chat", ...)` 发起同步 POST；服务端 `do_POST` → `dispatch_post` → `Agent.chat()` 内部通过 `OpenAICompatiblePlanner.plan()` 用 `urllib.request.urlopen(request, timeout=self.timeout)` 同步调用模型（`llm_adapter.py`）。
- 超时 20 秒、最多 2 次尝试（`MAX_ATTEMPTS = 2`、`RETRY_BACKOFF_SECONDS = 0.08`），最坏情况一次聊天在模型环节停留约 40 秒。
- 前端等待期间只有一个不确定的转圈（`aria-busy` 发送按钮 + 待处理消息气泡），没有任何过程信息。用户不知道系统是在理解、在追问还是在生成方案。
- 规则模式（未配置 LLM）下 `plan()` 直接返回 None，聊天在 50ms 内完成——等待体验问题只在 LLM 模式存在，但 LLM 模式恰恰是面向非专业用户的主路径。

### 1.2 问题的影响

- 北极星指标"10 分钟整理完成订单"中，每轮澄清对话若平均等待 5-20 秒，5 轮即 1-2 分钟纯等待，且是心理负担最重的"无反馈等待"。
- 走查脚本门槛 7 的步骤 1（澄清轮数 ≤3）会因等待焦虑被放大——用户倾向于在等待中重复提交或刷新（前端已有 `chatRequestSeq` + `AbortController` 竞态保护，重复提交会被正确丢弃，但体验仍是失败的）。

## 2. 目标与非目标

### 2.1 目标

1. LLM 模式下，从用户点击发送到**首字节可见反馈**（"正在理解…"阶段提示或模型增量文本）不超过 1 秒；
2. 模型回复文本**逐字渐进呈现**；工具调用与字段补丁只在**最终帧**一次性落地（保证确定性边界不被流式过程破坏）；
3. 规则模式行为完全不变（不引入伪流式）；
4. 流式失败自动降级为现有同步路径，功能零损失；
5. 保留既有竞态语义：新请求中止旧请求，被中止的流不得写入任何状态。

### 2.2 非目标

- 不做多模型并发请求、不做本地 token 级打字机特效模拟（没有模型增量时假装打字是欺骗性 UI，明确不做）；
- 不改变补丁/工具白名单校验逻辑；
- 不在本路径内做供应商流式（路径 3 的外部请求全部非流式）。

## 3. 方案设计

### 3.1 总体架构：服务端中转的 SSE

浏览器 ←（SSE，chunked）→ 本地 server ←（OpenAI 流式 HTTP）→ 模型厂商。

选择服务端中转而非前端直连厂商的理由：(1) API Key 不出本机服务进程；(2) SSRF 校验点唯一；(3) 请求头注入令牌与同源校验复用现有 `_guard()`；(4) 规则模式与 LLM 模式对外呈现同一协议。

### 3.2 事件协议（`text/event-stream`）

```
event: stage        data: {"stage":"plan","message":"正在理解需求…"}
event: delta        data: {"text":"好的，我"}
event: delta        data: {"text":"们先确认尺寸…"}
event: final        data: {…完整的 chat 响应 JSON，与现有 /api/chat 返回体一致…}
event: error        data: {"code":"…","message":"…","requestId":"…"}
```

关键设计决策：

- **`final` 帧是唯一权威**：delta 只用于展示，patch/tool 解析、`_apply_patch`、工具执行、记忆写入全部在 `final` 帧对应的既有同步逻辑里执行。流式过程绝不写状态，这样流被中止/出错时状态一致性天然成立。
- **事件序号与 requestId**：每个事件带 `seq` 递增序号；`X-Request-ID` 沿用现有机制，error 帧与 `final.error` 都能回溯。
- **心跳**：每 5 秒发 `event: ping` 防止中间层超时断连（本地直连通常不需要，防御性保留）。

### 3.3 服务端改动（server.py）

1. `do_POST` 增加 `/api/chat/stream` 路由（与 `/api/chat` 并存，旧的先保留一个版本再切换）；
2. 通过 `self.wfile` 直接写 SSE 帧（复用 `reply()` 的头写法，`Content-Type: text/event-stream`、`Cache-Control: no-store`、`Connection: close`）；写帧时捕获 `BrokenPipeError/ConnectionResetError` 并中止上游读取；
3. 会话锁：流式期间持有 `_session_lock(session_id)` 直到 `final` 帧写完（防止流式过程中另一请求修改同一会话）；
4. 令牌/同源守卫：`/api/chat/stream` 与 `/api/chat` 同策略（`_guard()` 在读 body 前执行）。

### 3.4 模型侧改动（llm_adapter.py）

新增 `plan_stream(text, order, tools, history, tool_result, on_delta)`：

- 请求体加 `"stream": true`；`urlopen` 返回的响应按行读取，解析 `data:` 行；
- 增量 JSON 拼接：OpenAI 兼容接口的 `choices[].delta.content` 分片拼接；对 `delta.reasoning_content`（部分厂商）忽略或计入 stage 消息；
- 每收到 delta 回调 `on_delta(text_fragment)`（由 server 转成 SSE 帧）；同时累积完整文本；
- 结束后走既有 `_parse_plan` → `validate_plan` 管线（白名单校验完全复用），返回与 `plan()` 相同结构；
- **降级链**：厂商不支持 stream（返回非 SSE 体）→ 解析失败 → 网络异常，任一情况在首个 delta 之前发生则退回 `plan()` 同步路径；已产出部分 delta 后失败则发 `event: error` 并由前端提示重试（不静默降级，避免"看到一半的回复消失"）。

### 3.5 前端改动（app.js）

1. `apiStream(path, body, {onStage, onDelta})`：`fetch` + `response.body.getReader()` 手动按 `\n\n` 分帧解析 SSE；
2. 待处理气泡分两段渲染：`stage` 更新状态行文本；`delta` 增量追加到气泡文本（每帧 `textContent +=`，配合既有的"贴近底部才自动滚动"逻辑）；
3. `final` 帧走既有 `render(data)`；`error` 帧走既有错误气泡路径（带 requestId）；
4. `AbortController` 语义不变：abort 时 `reader.cancel()`，服务端捕获断连后中止上游；
5. 规则模式：请求普通 `/api/chat`（响应快，不需要流式），由首次响应里的 `planner.enabled` 或直接按端点区分。

## 4. 接口与数据契约

- 新端点：`POST /api/chat/stream`，请求体与 `/api/chat` 完全一致（`sessionId`、`text`、`patch`、`itemIndex`）；
- 响应头：`Content-Type: text/event-stream`；`X-Request-ID` 必带；
- 事件 schema 固定四类（stage/delta/final/error），新增事件类型必须在本文档登记后才允许实现；
- 兼容承诺：`/api/chat` 同步端点在 v0.12-v0.13 全程保留，前端默认流式、失败自动回退同步，二者最终状态一致。

## 5. 分阶段实施步骤

| 阶段 | 内容 | 验证 | 预估 |
| --- | --- | --- | --- |
| A | `llm_adapter.plan_stream()` + fake SSE 测试服务器（单测：delta 拼接、非流式回退、白名单校验复用、reasoning_content 容错） | 单测 8-10 例 | 1 天 |
| B | server `/api/chat/stream` 端到端（令牌/同源/会话锁/断连清理），socket 级测试读 SSE 帧 | http 测试 5-6 例 | 1 天 |
| C | 前端 `apiStream` + 待处理气泡渐进渲染 + 降级链 | 手动矩阵（LLM 开/关、断网、中止、双击发送） | 1 天 |
| D | 心跳、超时预算（首字节 5s、总时长 60s 上限）、CHANGELOG/README | 回归 + 性能预算核对 | 0.5 天 |

## 6. 测试与验收

- 单测：SSE 行解析的 6 种边界（跨 chunk 分片、`data: [DONE]`、空 delta、非 JSON 行、多 choice、CRLF）；
- 契约：`final` 帧与同步 `/api/chat` 返回体逐字段一致（同一输入双端点快照对比）；
- socket 级：令牌缺失 401、跨来源 403、客户端中途断开不写会话状态、会话锁不泄漏；
- 性能预算：LLM 模式 mock 厂商首字节延迟 300ms 时，端到端首字反馈 <1s；
- 验收演示脚本：配置真实模型，走查脚本步骤 1 的三句话，观察渐进反馈。

## 7. 风险与对策

| 风险 | 概率 | 对策 |
| --- | --- | --- |
| 厂商 SSE delta 结构差异（reasoning_content、工具调用分片） | 高 | 白名单式解析：只认 `delta.content` 字符串分片，其余字段忽略；未知结构走同步回退 |
| 流式中途失败导致"半截回复" | 中 | final 前失败一律 error 帧 + 前端明确提示；不把半截文本当结论 |
| 会话锁被长流占用导致并发请求排队 | 中 | 流式会话通常单用户单会话；锁内只做读 + final 写，上游网络读取阶段不持锁（final 写入时短暂获取） |
| chunked 响应被 SimpleHTTPRequestHandler 基类干扰 | 低 | 已切换 BaseHTTPRequestHandler 自管响应体，无缓存层干扰；Windows 关闭语义沿用 `_discard_body` 经验 |

## 8. 工作量与验收门禁

3-4 人日。发布 v0.12.0 前必须：新增测试全绿、`/api/chat` 同步回归零变化、真实模型演示通过、CHANGELOG 记录新端点与降级语义。

## 9. 依赖与后续

- 与路径 4 的 planner 隔离同期实施（`last_error` 下沉后，流式的 stage/error 事件才有稳定来源）；
- 为路径 7 的"阶段进度可视化"提供事件源（stage 事件即进度条数据）；
- 实施记录：〔待填〕

## 10. 任务卡分解

| 卡号 | 所属阶段 | 任务 | 改动文件 | 验收标准 | 预估 |
| --- | --- | --- | --- | --- | --- |
| S1-01 | A | SSE 行解析器（跨 chunk 分片缓冲、`data:` 前缀剥离、`[DONE]` 识别、CRLF 兼容） | llm_adapter.py、tests/test_llm_stream.py（新） | 6 种边界单测全绿；解析为纯函数可独立测试 | 0.5d |
| S1-02 | A | `plan_stream()` 主流程：stream 请求构造、delta 回调、全文累积、复用 `_parse_plan`+`validate_plan` | llm_adapter.py | 白名单校验与 `plan()` 完全一致（同输入快照对比测试） | 0.5d |
| S1-03 | A | 降级链：非 SSE 响应体/解析失败/首 delta 前异常 → 自动回退 `plan()`；已出 delta 后失败 → 抛 `StreamBroken` | llm_adapter.py | 三条回退路径单测；fake 服务器模拟三态 | 0.5d |
| S1-04 | A | fake SSE 测试服务器（标准库 `http.server`，可编程回放分片序列） | tests/fake_sse_server.py（新） | 支持延迟、断连、乱序、非标准头四种注入 | 0.5d |
| S1-05 | B | `/api/chat/stream` 路由：守卫、请求体读取、SSE 头与帧写入、断连清理 | server.py | socket 级测试 5 例（守卫 2、正常流 1、断连 1、异常 1） | 0.5d |
| S1-06 | B | 会话锁策略：final 写入时短暂持锁；上游读取阶段不持锁 | server.py | 并发单测：流式进行中另一会话请求不受阻 | 0.5d |
| S1-07 | C | `apiStream()`：fetch + ReadableStream 分帧、事件分发、abort 语义 | app/api.js（或迁移期 app.js） | 手动矩阵 + Playwright 断言渐进文本 | 0.5d |
| S1-08 | C | 待处理气泡渐进渲染（stage 行 + delta 追加 + 底部吸附规则沿用） | app.js | 视觉走查截图；`aria-live` 不抖动（每帧合并为一次朗读单元） | 0.5d |
| S1-09 | D | 心跳（5s ping）、首字节 5s/总长 60s 超时预算、超时归类 | llm_adapter.py、server.py | 超时注入测试；超时后 error 帧到达 | 0.25d |
| S1-10 | D | 文档与切换：CHANGELOG、README API 表、`/api/chat` 保留声明 | 文档 | 文档评审通过 | 0.25d |

## 11. 详细测试矩阵

| 层 | 用例组 | 断言要点 | 卡号 |
| --- | --- | --- | --- |
| 单测 | 分片边界 | 一个 JSON 字符的 UTF-8 多字节被 chunk 切开仍正确拼接 | S1-01 |
| 单测 | delta 累积 | 回调顺序与全文一致性；空 delta 不回调 | S1-01/02 |
| 单测 | 白名单复用 | 流式产出的 patch/tool 与同步路径同一输入逐字段相同 | S1-02 |
| 单测 | 回退三态 | 非 SSE 体 / 语义失败 / 首帧前异常 → plan() 结果返回 | S1-03 |
| 单测 | 半途失败 | 已回调 delta 后异常 → StreamBroken，不回退不静默 | S1-03 |
| 单测 | 429 限流 | Retry-After 被尊重（衔接路径 4） | S1-02 |
| 契约 | final 一致性 | 同一输入 stream final 与 POST /api/chat 返回体 JSON 逐键相等 | S1-05 |
| socket | 守卫 | 无令牌 401 / 跨来源 403 / Origin 匹配放行 | S1-05 |
| socket | 断连清理 | 客户端 mid-stream 断开：上游连接关闭、会话状态零写入、锁释放 | S1-05/06 |
| socket | 超时 | 上游挂起 → error 帧在预算内到达 | S1-09 |
| Playwright | 渐进呈现 | mock 分片 3 段 → 气泡文本分 3 次增长 → final 后正常 render | S1-07/08 |
| Playwright | 中止 | 发送后立即重发：第一条被中止、无半截文本残留 | S1-07 |

## 12. 关键实现草图：SSE 解析器与两级降级链

流式是三条 P0 路径中唯一改变"请求-响应"形态的一条，实现草图聚焦两个风险最高的
部件：非标准厂商 SSE 的容错解析、以及任何一环失败时的降级链。目标：流式是加速，
不是可用性前提——**流式挂了必须无声退回同步路径**。

### 12.1 服务端转发层（server.py 扩展，约 60 行）

```python
def do_POST_chat_stream(self, body, session_id):
    """/api/chat?stream=1：先发响应头，再按事件转发，最后发终帧。"""
    self.send_response(200)
    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
    self.send_header("Cache-Control", "no-store")
    self.send_header("X-PrintOps-Stream", "v1")
    self.end_headers()
    for event in agent.chat_events(body["text"]):        # 生成器：阶段帧 + 正文帧 + 终帧
        self.wfile.write(f"event: {event.type}
data: {json.dumps(event.data, ensure_ascii=False)}

".encode())
        self.wfile.flush()                                # 每帧必须 flush，否则本地缓冲吞掉流式
```

帧类型三种：`stage`（workflowStage 变化，复用现有阶段机）、`delta`（正文增量，
仅 LLM 流式可用时）、`final`（完整响应 JSON，与同步接口字节级一致）。前端只在
收到 `final` 后才提交 store——**中间帧只渲染不进状态**，保证刷新恢复与现有快照
语义零改动。

### 12.2 客户端解析（app/api.js 扩展）

```js
export async function apiStream(path, body, { onStage, onDelta }) {
  const response = await fetch(path + "?stream=1", { method: "POST", headers, body: ... });
  if (!response.ok || response.headers.get("X-PrintOps-Stream") !== "v1") return api(path, body); // 降级①
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "", final = null;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    for (const frame of drainFrames(buffer)) {           // 按切帧，残帧留缓冲
      if (frame.type === "stage") onStage?.(frame.data);
      else if (frame.type === "delta") onDelta?.(frame.data);
      else if (frame.type === "final") final = frame.data;
    }
  }
  return final ?? api(path, body);                        // 降级②：流断在半路 → 同步重放
}
```

### 12.3 适配器流式解析（llm_adapter.py 扩展，约 80 行）

1. `stream=True` 请求上游，`urlopen` 返回按行读；delta 提取只接受白名单路径
   `choices[0].delta.content`——非标准厂商发的多余字段一律忽略（容错第一原则）；
2. 累积的全文在流结束后仍走现有 `_parse_plan → validate_plan` 全量校验：**流式
   只改变"用户看到文字的时刻"，不改变计划的可信边界**——校验不过则丢弃已显示的
   正文，回复一条降级说明并走同步重试（最坏情况与现状等价）；
3. 上游不支持流式（响应头无 `text/event-stream` 或首块即完整 JSON）→ 直接走同步
   路径。三级降级链：LLM 流式 → LLM 同步 → 规则模式，每级切换记入 `llm_fallback`
   事件（附录 D），带 `degraded_from` 字段。

### 12.4 超时与取消语义

- 上游读块间隔 >5s 视为流停滞，主动断开并降级同步（`socket.setdefaulttimeout` 不动，
  用响应对象上的超时读）；
- 前端 `AbortController` 中止 fetch 时，服务端在下次 `wfile.write` 捕获
  BrokenPipe/ConnectionReset 后停止上游读取并释放会话锁（与 v0.11.0 的排空纪律
  同一处理位）；
- 会话锁持有时间从"整个 LLM 调用"降为"每帧写入"——依赖路径 4 的锁粒度改造
  （S4-01/02 是 S1 的硬前置，总纲执行顺序已按此排列）。

### 12.5 验收抽要（完整矩阵见 §11）

fake SSE 服务器（`tests/fake_remote.py`）覆盖五型上游：标准 OpenAI delta、无流式
完整 JSON、中断流（3 帧后断开）、乱序字段、每帧>4KB 的长正文；断言：白名单外字段
被忽略、终帧与同步接口输出一致、断流后前端无半截文本残留（§11 中止用例的机制
解释）、降级链每级切换有事件记录。
