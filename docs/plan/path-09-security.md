# 路径九：安全纵深（path-09-security）

> 优先级 P2（其中审计与 keyring 因路径 3 依赖而提升为 P1）　目标版本 v0.13.0 → v1.1.0　预估工作量 3-4 人日　依赖：无（A/B 阶段先行于路径 3）

## 1. 现状与问题

v0.11.0 已闭合 ROADMAP P0 全部四项（白名单、本地令牌、Key 明文提醒、SSRF 校验）并扩充了敏感扫描。本路径处理其上残留的纵深议题：

1. **凭据仍以明文落盘为默认**：界面保存的 Key 写入 `data/llm_config.json`（0600 仅 POSIX 生效），只有"提醒"没有"替代"；Windows 凭据管理器/系统钥匙串未被利用；
2. **本地 API 无限流**：令牌拦截了跨来源，但持有令牌的页面（或本机任意进程拿到令牌）可以无限制轰接口；
3. **无操作审计**：谁在何时确认了交接单、改了平台、发起询价——目前只有会话内 run 事件（随会话可能被淘汰），没有独立的、不可随会话消失的操作留痕（路径 3 的外部交互必须以此打底）；
4. **SSRF 残余风险**：配置时校验与实际请求之间存在 DNS 重绑定理论窗口（ROADMAP 已记录"对单机工具可接受"）；
5. **`.env` 与环境变量约定不统一**：README 提到 `.env` 在 gitignore，但代码只读 `PRINTOPS_*` 环境变量，无 `.env` 加载（文档与实现有轻微脱节）。

## 2. 目标与非目标

### 2.1 目标

1. **凭据存储升级**：优先使用系统凭据存储（Windows Credential Manager / macOS Keychain），明文文件降级为显式选择并保留强警告；
2. **本地限流**：按令牌/来源维度的简单令牌桶，防误用与失控循环；
3. **审计日志**：独立于会话的操作留痕（确认、平台切换、询价、导出、设置变更），为路径 3 的外部交互打底；
4. **SSRF 收尾**：请求时刻复验目标 IP（配置时校验 + 请求时校验双保险）；
5. **`.env` 支持**：标准库解析 `.env`（若存在），统一文档与实现。

### 2.2 非目标

- 不做用户账户/多用户；
- 不做加密的会话存储（会话数据不含凭据，威胁模型内可接受）；
- 不做入侵检测/告警（本地单用户工具）。

## 3. 方案设计

### 3.1 凭据存储（分级降级链）

优先级：`PRINTOPS_LLM_KEY` 环境变量 > 系统凭据存储 > 明文文件（显式选择）。

- **系统凭据存储**：零依赖约束下用 `ctypes` 调 Windows `CredWrite/CredRead`（advapi32）；macOS 用 `security` 命令行（`security add-generic-password` / `find-generic-password`）；Linux 尝试 `secret-tool`（libsecret），不可用则降级。三者各约 40-60 行，封装为 `credential_store.py`，带能力探测函数 `available() -> bool`；
- **降级契约**：凭据存储不可用或用户在设置中勾选"以明文保存 Key（不推荐）"时，才写 `llm_config.json` 并触发既有 `keyStorageWarning`；UI 设置面板显示当前存储方式（"系统凭据管理器 / 明文文件（不推荐）"）；
- **迁移**：检测到明文文件中有 Key 且凭据存储可用 → 迁移向导式提示（一键迁移，迁移后清空明文字段）；不做静默迁移（用户知情权）；
- 凭据存储条目：服务名 `PrintOps`、账户名 = URL+模型摘要，便于多配置。

### 3.2 本地限流

令牌桶（内存实现，约 50 行）：

- 维度：按会话令牌（全服务单令牌）+ 按 endpoint 组（chat 类 10 次/分钟、burst 20；读类 60 次/分钟；供应商类 10 次/分钟——路径 3 使用）；
- 超限返回 429 `RATE_LIMITED` + `Retry-After`；
- 放行白名单：`/api/health` 不限流；
- 限流状态是进程内的，重启清零（本地工具可接受）；
- 前端 `api()` 对 429 的处理：toast 提示"操作太频繁，请稍候"+ 显示 Retry-After 秒数。

### 3.3 审计日志

- 存储：主库新表 `audit_log(id INTEGER PK, ts TEXT, actor TEXT, action TEXT, target TEXT, request_id TEXT, digest TEXT)`——`actor` 在无账户体系下固定 `local`，为远期多用户留位；`digest` 是动作关键内容的短哈希（确认备注等敏感文本不落原文）；
- 记录点：`/api/confirm`（人工确认交接单）、`/api/platform`（平台切换）、询价发起/取消、`/api/settings` 变更（不含值）、（路径 3）全部外部请求；
- 保留 90 天，启动时清理（同路径 4 的 prune 模式）；
- 查询：本期仅提供 `GET /api/audit?limit=100`（令牌保护）供排障；不做 UI 页面（决策面板显示"最近操作 N 条"计数即可）。

### 3.4 SSRF 请求时复验

`llm_adapter`（与路径 3 的 `supplier_http`）在实际 `urlopen` 前对已解析的目标做一次 `_reject_private_host` 复验（复用同函数）。DNS 双取一致性不强求（TOCTOU 窗口收窄到毫秒级即可，单机威胁模型下足够）。

### 3.5 `.env` 支持

标准库实现 30 行解析器（`KEY=VALUE`、`#` 注释、引号剥离、不做插值）；启动时若项目根存在 `.env` 则加载到 `os.environ`（不覆盖已有环境变量）；README 与实现统一（`.env` 支持存在、敏感扫描覆盖 `.env`——`secret_scan` 已不跳过隐藏文件需确认 `.env` 在扫描路径内）。

## 4. 接口与数据契约

- `/api/settings` 返回扩展：`{"llm": {...}, "keyStorage": "system|plaintext|env", "keyStorageWarning": …}`（`keyStorage` 新键）；
- 新错误码：`RATE_LIMITED`（429，带 `Retry-After` 头）；
- `GET /api/audit`：`{"entries": [{ts, action, target, requestId}], "total": N}`；
- 明文文件格式不变（`llm_config.json`），新增可选键 `"storage": "plaintext"`（迁移向导写入）。

## 5. 分阶段实施步骤

| 阶段 | 内容 | 验证 | 预估 |
| --- | --- | --- | --- |
| A | 审计表 + 记录点 + 清理 + `/api/audit` | 单测：五类动作各一条、90 天清理、digest 不含原文 | 1 天 |
| B | 限流（令牌桶 + 429 + 前端提示） | 单测：桶边界、恢复；http 测试：连续 21 次 chat 第 21 次 429 | 0.5 天 |
| C | `credential_store.py`（Win/macOS/Linux 三实现 + 探测）+ 设置面板存储方式展示与迁移向导 | 平台可用性手测（Windows CI 上 CredWrite 可跑）；单测 mock 三后端 | 1.5 天 |
| D | SSRF 请求时复验 + `.env` 加载 + secret_scan 覆盖确认 | 单测：复验拦截、.env 不覆盖已有变量 | 0.5 天 |
| E | 文档：README 安全章节重写（凭据分级、限流、审计） | 文档评审 | 0.5 天 |

## 6. 测试与验收

- 审计：确认动作后 `/api/audit` 可查、digest 与原文无关（扫描审计表内容不得出现备注原文）、90 天清理；
- 限流：突发 21 连发第 21 次 429 + Retry-After；health 永不受限；
- 凭据：Windows 上 CredWrite/CredRead 往返（CI 可跑）；明文降级路径在无凭据存储环境（模拟）生效；迁移向导后明文字段为空；
- SSRF：请求时复验的单测（patch getaddrinfo 返回内网地址 → 拒绝）；
- secret_scan：`.env` 内放置形态化凭据（运行时拼接的测试夹具）能被扫出。

## 7. 风险与对策

| 风险 | 概率 | 对策 |
| --- | --- | --- |
| ctypes 调用凭据 API 在不同 Windows 版本差异 | 低 | 只用 CredWrite/CredRead/CredFree 三个稳定 API；失败一律降级明文 + 警告，不阻塞 |
| 限流误伤正常高频用户 | 低 | 阈值按真实使用画像放宽（chat 10/分钟远超人类速率）；429 提示明确 |
| 审计表与主库耦合影响性能 | 低 | 审计写入同库但独立表、批内一次提交；量大时（不会发生）可拆库 |
| macOS `security` 需要钥匙串授权弹窗 | 中 | 探测失败即降级；文档说明首次授权现象 |

## 8. 工作量与验收门禁

3-4 人日。门禁：A/B 阶段在 v0.13.0 完成（路径 3 前置），C 阶段（keyring）在路径 3 阶段 E 前完成；全部平台凭据路径不落明文为最终态验收。

## 9. 依赖与后续

- 审计（A）与限流（B）是路径 3 阶段 E（live 模式）的硬前置；
- 凭据分级是路径 8 升级提示之外"专业交付感"的组成部分；
- 远期多用户（若发生）：`actor` 字段与限流维度已预留扩展位；
- 实施记录：〔待填〕

## 10. 任务卡分解

| 卡号 | 阶段 | 任务 | 改动文件 | 验收标准 | 预估 |
| --- | --- | --- | --- | --- | --- |
| S9-01 | A | audit_log 表 + 写入助手（digest 短哈希、90 天清理） | agent.py（或新 audit.py） | 五类动作各一条断言；原文不落库 | 0.5d |
| S9-02 | A | 记录点接线：confirm/platform/询价发起取消/settings 变更 | server.py、agent.py | 每类动作审计行字段正确 | 0.25d |
| S9-03 | A | `GET /api/audit`（令牌保护，limit≤100）+ 决策面板"最近操作 N 条"计数 | server.py、app.js | 契约测试；UI 计数一致 | 0.25d |
| S9-04 | B | 令牌桶限流（chat 10/min burst 20、读 60/min、supplier 10/min、health 豁免） | server.py | 21 连发第 21 次 429 + Retry-After；恢复后放行 | 0.5d |
| S9-05 | B | 前端 429 处理（toast + Retry-After 秒数） | app/api.js（迁移后） | 手动触发限流提示可见 | 0.1d |
| S9-06 | C | credential_store.py 接口与探测（Win CredWrite/macOS security/Linux secret-tool） | credential_store.py（新） | 接口单测（mock 三后端）；Windows CI 实跑往返 | 1d |
| S9-07 | C | 设置面板存储方式展示 + 明文迁移向导（一键迁移后清空明文字段） | app/features/settings.js、llm_adapter.py、server.py | 迁移后 llm_config.json 无 Key；UI 显示"系统凭据管理器" | 0.5d |
| S9-08 | D | SSRF 请求时复验（urlopen 前） | llm_adapter.py、supplier_http.py | patch getaddrinfo 内网 → 拒绝单测 | 0.25d |
| S9-09 | D | `.env` 标准库加载（不覆盖已有环境变量）+ secret_scan 覆盖确认 | server.py（或 env.py） | .env 单测；扫描覆盖断言 | 0.25d |
| S9-10 | E | README 安全章节重写（凭据分级/限流/审计/.env） | 文档 | 文档评审 | 0.5d |

## 11. 接口与结构定义

```python
# credential_store.py
def available() -> bool: ...                     # 当前平台凭据存储是否可用
def save(service: str, account: str, secret: str) -> bool: ...
def load(service: str, account: str) -> str | None: ...
def delete(service: str, account: str) -> bool: ...
# 约定：service 固定 "PrintOps"；account = f"{base_url}#{model}"
# 任何异常都不得向上抛：调用方据返回值走明文降级 + 警告
```

限流参数表（进程内令牌桶，按令牌维度共享）：

| endpoint 组 | 容量 | 补充速率 | 说明 |
| --- | --- | --- | --- |
| chat（含 /api/chat、/api/chat/stream、/api/focus） | 20 | 10/min | burst 覆盖批量补充场景 |
| read（/api/products、/api/platforms、/api/tools、/api/settings、/api/audit） | 60 | 60/min | — |
| supplier（路径 3 三端点） | 10 | 10/min | 外部请求成本高 |
| /api/health | ∞ | — | 豁免 |

audit_log 结构：`audit_log(id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL DEFAULT 'local', action TEXT NOT NULL, target TEXT, request_id TEXT, digest TEXT, latency_ms INTEGER)`。digest = sha256(动作关键内容)[:16]；确认备注、Key、订单文本永不入库。

## 12. 详细测试矩阵

| 层 | 用例组 | 断言要点 | 卡号 |
| --- | --- | --- | --- |
| 单测 | 审计五类 | confirm/platform/quote 发起/取消/settings 各产生一行，字段与 digest 正确 | S9-01/02 |
| 单测 | 审计清理 | 插入 91 天前记录 → 启动清理后消失 | S9-01 |
| 契约 | /api/audit | limit 截断、令牌缺失 401 | S9-03 |
| http | 限流边界 | 第 21 次 chat → 429 + Retry-After；health 不受限；等待后恢复 | S9-04 |
| 单测 | 凭据往返 | mock 三后端 save/load/delete；异常 → 返回 False 不抛 | S9-06 |
| 集成 | 迁移向导 | 明文 Key + 可用凭据存储 → 迁移 → 明文字段空、load 成功 | S9-07 |
| 单测 | SSRF 复验 | 请求时解析到 10.x → ValueError 拒绝（不发出请求） | S9-08 |
| 单测 | .env | 文件加载生效；不覆盖进程已有变量；格式容错（引号/注释） | S9-09 |
| 扫描 | .env 覆盖 | 形态化凭据（运行时拼接夹具）在 .env 内可被 secret_scan 扫出 | S9-09 |
