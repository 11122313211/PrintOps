# PrintOps 全方位安全体检报告（2026-11-22）

> 方法：三个并行只读审计代理覆盖三大攻击面（HTTP 服务 / LLM 与数据流 / 存储·引擎·前端）
> + 项目自带 Mimosa deep 全扫（scan-2026-11-22T10-02-07.773Z，114 项，无新 hypotheses，
> 依赖 4 包零命中，seal sha256:68964498…）+ 主会话对全部中危项的逐条代码复核。
> 威胁模型：本地单机单用户（server.py 绑定 127.0.0.1，进程内令牌）。分级按
> "是否突破已声明威胁模型" 优先。
>
> **处置状态（2026-11-22 收尾）**：H1→TD-26 ✅（471de3a）、L1→TD-27 ✅（2c3e7f1）、
> TD-28 全批 ✅（server.py 四项 c4a2707 / RecursionError+上界 da08000 / NUL+凭据
> 形态 0d905b2 / NLU 单趟扫描 e32c5c0，评测 123 例等价全绿）；S9 与 H6 按体检
> 结论记录维持；D1 的 ARCHITECTURE 声明已改述（CHANGELOG 历史段按惯例不动，
> 由 Unreleased 修复条目superseded）。

## 一、发现总览

| # | 级别 | 位置 | 摘要 |
| --- | --- | --- | --- |
| H1 | **中危** | server.py:1303-1315,1261,1539 | 浏览器侧 DNS rebinding 全链路：静态路径无防护 + index.html 注入令牌 + Origin==Host 恒过 |
| L1 | **中危** | llm_adapter.py:1355-1370,389-392 | LLM 请求跟随 3xx 重定向且 Authorization 头随跳：key 外发 + 内网探测双通道 |
| H2 | 低 | server.py:1420-1423 | 深嵌套 JSON → RecursionError 落 500 而非 400（错误分类失真） |
| H3 | 低 | server.py:64-65,419-421 | SESSION_LOCKS 只增不删，长期运行慢性泄漏 |
| H4 | 低 | server.py:743-750 | /api/orders limit/offset 无 clamp，limit=-1 全表加载 |
| H5 | 低 | server.py:1502-1533 | 请求日志换行注入（%0A 伪造日志行） |
| S1 | 低 | pricing_schema.py:107-109,23-36 | rate card 数值无上界：1e999999 可使 estimate_order 抛未捕获 decimal 异常 |
| L10b | 低 | llm_adapter.py:417,1424 | _request_json 同样未捕 RecursionError，/api/model/test 升 500 |
| S7 | 低 | markdown.js:16-17,68-73 | NUL 占位符可被输入字面量伪造（仅复制/清空已渲染元素，无注入） |
| S8 | 低 | tools/secret_scan.py:19-40 | 凭据形态缺口：PEM 头/Stripe sk_live_/GitLab glpat-/npm_/JWT/LTAI |
| S2 | 低 | nlu.py:56-79 | perceive O(n²)：20k 字符最坏 ~4.3s（无 ReDoS 炸弹，纯延迟） |
| S9 | 低 | journey.js:12-16 | 核对草稿明文存 sessionStorage（同 profile 可读，如实记录） |
| D1 | 声明不一致 | ARCHITECTURE.md:62；CHANGELOG.md:333 | "同源约束/阻断 DNS-rebinding 面"声明过强（H1 使其不成立） |

## 二、中危详情（已主会话逐条复核）

### H1 浏览器侧 DNS rebinding 全链路（✅ 已修复 2026-11-22：Host 白名单 + 3 项 socket 级测试，见 TD-26 偿还记录与 CHANGELOG Unreleased）

证据链四处：
1. `server.py:1303-1315` — `do_GET` 仅 `/api/*` 走 `_guard()`（Origin+token），静态路径直接 `serve_static`，无 Origin/Host/token 检查；
2. `server.py:1539` — 每次 `index.html` 响应注入 `<script>window.__PRINTOPS_TOKEN__=<明文token></script>`（app.js:888 读取后作为 API 凭据）；
3. `server.py:1261` — `_origin_allowed` 判定 `urlsplit(origin).netloc == Host 头`，rebinding 场景两者同为攻击者域，恒通过；全文件无 Host 白名单；
4. `tests/test_http_handler.py:112-119` — 仅测 `evil.example`≠`127.0.0.1` 形态，rebinding 形态（Origin==Host==攻击者域）无用例。

触发：攻击者页面对 attacker.com:4174 做 DNS A 记录切换 → 浏览器同源 `fetch("/")` 读出注入 token → 以 token 调全部 API（导出订单、改设置、purge）。纯跨域 CSRF 已被 token 拦住（401），本链路破坏的是"读不到 token"前提。
**修复建议**：`serve_static`（至少 index.html）加 Host 头白名单（仅 127.0.0.1/localhost/[::1] 带端口），一处小改即可闭合并让 D1 两处声明成立；补 rebinding 形态测试。

### L1 LLM 请求跟随重定向且 Authorization 随跳（✅ 已修复 2026-11-22：进程级 _NoRedirect opener + 双服务器端到端测试，见 TD-27 偿还记录与 CHANGELOG Unreleased）

证据：全部模型请求走 `urllib.request.urlopen` 默认 opener（`llm_adapter.py:389-392`）；Request 带 `Authorization: Bearer {key}`（`llm_adapter.py:1355-1360`，同形态还有能力探测 ：1539-1544、test_connection :1777-1780、探活 GET :1502-1507）；URL 私网校验只在配置时做一次（`_reject_private_host`，:34-88），重定向目标不复检；Python 默认 `HTTPRedirectHandler` 跟随 301/302/303（POST 转 GET）与 307/308，**Authorization 头跨主机保留**。
触发：被劫持/恶意 OpenAI 兼容网关（或 http 明文中间人）302 → attacker.com 即收到 Bearer key；302 → 127.0.0.1:xxxx / 169.254.169.254 即绕过私网拦截做内网探测。
**修复建议**：自建禁重定向 opener（或对每跳复检 `_reject_private_host` 并剔除跨主机 Authorization）；`http://` scheme 是否保留白名单内一并复核（key 可明文过网）。

## 三、低危/卫生项摘要

- **H2/L10b RecursionError**：`json.loads` 深嵌套（90k 层/180KB 实测）抛 RecursionError，`server.py:1420-1423` 与 `llm_adapter.py:417,1424` 的捕获元组均不含 → 500。建议补 `RecursionError` → 400 INVALID_JSON（mcp_server.py:329-344 已是正确范式）。服务不崩，线程存活。
- **H3**：`SESSION_LOCKS.setdefault` 无清理路径（server.py:64-65,419-421）；持 token 方可用随机 sessionId 批量制造条目。建议 purge/归档时清理或 LRU+TTL。
- **H4**：`GET /api/orders?limit=-1` → SQLite `LIMIT -1` 无上限全表加载 + 逐条全量 JSON 反序列化（server.py:743-750、storage_sqlite.py:276-279）。建议 clamp 1..500。
- **H5**：请求路径 unquote 后直接进 LOGGER.info（server.py:1502-1533），%0A 可伪造日志行。建议剥离控制字符或 repr()。
- **S1**：rate card 数值仅非负校验，`"1e999999"` 通过 `decimal_value`（is_finite=True），`money()` quantize 抛 InvalidOperation / 引擎乘法抛 Overflow 均未捕获 → 手改 rate card 后所有估价持续 500（实测）。建议绝对上界（perM2/perKg ≤1e6、wasteFactor ≤10、band ≤10）。
- **S7**：markdown.js NUL 占位符 `\u0000N\u0000` 可被输入字面量伪造（tokens 只含先前生成的安全 HTML，无逃逸）→ 仅能复制/清空已渲染元素。建议源文本剥除 NUL。
- **S8**：secret_scan 10 类模式缺 PEM 头/Stripe sk_live_/glpat-/npm_/JWT/LTAI；SKIP_DIRS 含 reports/（文档已声明取舍）。建议补 2-3 条主流形态。
- **S2**：NLU 逐别名 81 次 finditer 全文扫描 → O(n²)，20k 字符最坏 ~4.3s/请求（实测 77→4269ms 曲线）。无 ReDoS；可预合并大正则。
- **S9**：核对草稿（printops_review_drafts_v1）明文存 sessionStorage；localStorage 键均为用户自输入。建议文档注明（不修行为）。
- **H6**：/api/health 完全开放仅回 `{"ok":true}`——存活探测面，威胁模型内可接受，如实记录。

## 四、验证良好清单（安全确认，无需修复）

SQL 全参数化（storage_sqlite.py 逐条核对，备份侧只读连接 mode=ro+query_only+trusted_schema=OFF）；备份链防篡改（digest+inventory 双校验、TOCTOU 复核、zip 组件不存在故无 zip-slip）；损坏 JSON 隔离自愈 + CAS 无绕过（_quarantine/safe_id/prepare_record）；前端 XSS 收敛（innerHTML 唯一入口走白名单渲染器、canvas fillText、textContent、无内联脚本无外链资源）；提示注入写面收敛（PATCH_FIELDS 白名单+validate_plan 降级+工具参数网关键白名单+MCP 桥更严校验）；mcp_server 仅 stdio 无端口；supplier 三件套零网络调用（grep 证实）；dsh_mcp_launcher 固定 argv 无 shell；令牌恒时比较、进程内生成不可为空；411/413/慢读断开/503 前置等运行时硬化齐备；NLU 正则无 catastrophic backtracking；compare_modes/annotate_case/knowledge_review/repo_guard 无代码执行面；新增 _longtail_cases 与 CLI 默认集均为固定内容。

## 五、与既有债务的衔接

- TD-14（SSRF DNS rebinding 窗口）：本次 H1 是**浏览器侧**对应链路，登记 TD-26 承接；L1 的重定向绕过登记 TD-27。
- TD-15（key 明文存储）：本次确认缓解面完备（原子写+icacls、git 历史干净、key 不入日志/遥测/备份/API），维持台账。
- TD-16（静态白名单回归）：白名单实现本身 fail-closed 良好；H1 在其外侧（guard 缺失），不合并。
- TD-25：与本审计无关，维持待裁定。

## 六、处置建议（优先级）

1. **TD-26（H1）**：Host 白名单 + rebinding 形态测试 + 修正 ARCHITECTURE/CHANGELOG 两处声明——唯一突破威胁模型边界的项，改动小。
2. **TD-27（L1）**：禁重定向 opener + 跳复检/去 Authorization + 测试（fixture 重定向响应）。
3. **TD-28 批量**：RecursionError 分类×2、limit clamp、SESSION_LOCKS 清理、日志净化、NUL 剥除、rate card 上界、secret_scan 补模式——每项独立小改，可分笔或按批推进。
4. S2/S9/H6：记录维持，择机优化。
