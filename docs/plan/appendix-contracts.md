# 附录C：接口与数据契约变更总表（appendix-contracts）

> 汇总十条路径引入的全部 API、错误码与数据契约变更，供实施时一次性对照。
> 约定：所有新端点默认过 `_guard()`（令牌 + 同源）；所有会话结构变更走
> `normalize_state` 迁移并递增 `STATE_SCHEMA_VERSION`；所有错误响应保持
> `{ok:false, code, message, requestId}` 结构。

## C.1 新增端点

| 端点 | 方法 | 路径 | 版本 | 用途 | 守卫 | 特殊语义 |
| --- | --- | --- | --- | --- | --- | --- |
| /api/chat/stream | POST | 路径 1 | v0.12 | 流式聊天（SSE：stage/delta/final/error/ping） | 令牌+同源 | final 帧为唯一权威；断连零状态写入 |
| /api/focus | POST | 路径 7 | v0.12 | 多产品聚焦切换 | 令牌+同源 | 不写 messages、不触发 NLU、不改 workflowStage |
| /api/supplier/quote-draft | POST | 路径 3 | v1.1 | 平台报价草稿 | 令牌+同源+确认门禁 | 幂等键重放；沙箱默认 |
| /api/supplier/lead-time | POST | 路径 3 | v1.1 | 交期查询 | 令牌+同源 | 同上 |
| /api/supplier/export | POST | 路径 3 | v1.1 | 字段导出 | 令牌+同源+confirmed 门禁 | 导出前 UI 二次确认 |
| /api/supplier/status | GET | 路径 3 | v1.1 | 平台连通性与模式 | 令牌+同源 | — |
| /api/audit | GET | 路径 9 | v0.13 | 审计查询（limit≤100） | 令牌+同源 | 仅摘要与哈希 |

## C.2 既有端点的响应扩展（向后兼容，仅新增键）

| 端点 | 新增键 | 路径/版本 | 说明 |
| --- | --- | --- | --- |
| /api/health | version、knowledgeVersion、stateSchemaVersion、pricingModelVersion、updateAvailable | 路径 8 / v0.13 | 只暴露版本号；测试锁定无多余键 |
| /api/settings（GET/POST） | keyStorage | 路径 9 / v0.13 | 值：system / plaintext / env |
| /api/settings（POST） | keyStorageWarning | 路径 9 / v0.13（v0.11 已有，语义不变） | 明文落盘时提示 |
| /api/chat（同步） | plannerError | 路径 4 / v0.12 | 仅当本次规划失败回退规则模式时出现 |
| /api/preflight（请求） | file:{…}、fileId、role | 路径 6 / v0.13 | 旧单文件字段保留一个版本 |
| 会话快照 items[] | files[] | 路径 6 / v0.13 | 见路径 6 第 11 节字段规范 |
| 会话快照 quoteRequests[] | external | 路径 3 / v1.1 | 外部平台返回的结构化结果 |

## C.3 新增错误码

| 错误码 | HTTP | 路径/版本 | 触发条件 |
| --- | --- | --- | --- |
| PAYLOAD_FIELD_TOO_LONG | 400 | 路径 4 / v0.12 | text/note 等自由文本超 4000 字符（可经 PRINTOPS_MAX_TEXT 调整） |
| RATE_LIMITED | 429 | 路径 9 / v0.13 | 令牌桶耗尽；响应带 Retry-After 头 |
| UNAUTHORIZED | 401 | 路径 9（v0.11 已引入） | 令牌缺失或不匹配 |
| FORBIDDEN_ORIGIN | 403 | 路径 9（v0.11 已引入） | Origin 与 Host 不符 |
| STREAM_BROKEN | —（SSE error 帧） | 路径 1 / v0.12 | 流式中途失败（不作为 HTTP 错误码存在） |
| SupplierXxx | — | 路径 3 / v1.1 | 外部错误在服务端归类后以既有结构透出，不新增透传码 |

## C.4 会话 schema 演进（STATE_SCHEMA_VERSION）

| 版本 | 引入版本 | 变更 | 迁移要点 |
| --- | --- | --- | --- |
| 2 | v0.9.0 | 当前基线（dimensions、quantity 三元组、fieldMeta） | — |
| 3 | v0.13.0（路径 6） | items[].files 数组；uploadedFile → files[0]（role=artwork），旧字段双写一版本 | 空/单/多文件三态迁移单测 |
| 4 | v1.1.0（路径 3） | quoteRequests[].external（默认 null） | 旧会话补默认值即可 |

内部表（不递增 schemaVersion）：`session_meta`（路径 4，LRU 治理）、`audit_log`（路径 9）。

## C.5 新增本地文件

| 文件 | 路径 | 版本 | 内容与边界 |
| --- | --- | --- | --- |
| data/supplier_config.json | 路径 3 | v1.1 | `{"mode":"sandbox"|"live","platforms":{…}}`；gitignore 内 |
| data/settings.json | 路径 8 | v0.13 | `{"updateCheck":true}`；gitignore 内 |
| data/corrupted/*.json | 路径 6 之外（v0.11 已有） | — | 损坏会话备份（已 gitignore） |
| tests/eval_cases_real.json | 路径 5 | 已就绪 | 真实语料（进版本库，脱敏要求见内说明） |
| tests/fixtures/planner_replay/ | 路径 5 | v0.12 | LLM 回放 fixture（进版本库，含录制说明） |
| docs/eval/history.jsonl | 路径 5 | v0.12 | 评测趋势记录（进版本库） |

## C.6 兼容性承诺汇总

1. `/api/chat` 同步端点在 v0.12-v0.13 全程保留；
2. `uploadedFile` 旧字段在 schema 3 后双写一个版本再移除；
3. localStorage 五个键（session/layout/sidebar/contrast/presets）格式不变；
4. 错误响应结构与 `X-Request-ID` 头永不变更；
5. 任何破坏性变更（schema、API 删除、键语义变化）必须在 CHANGELOG 显式标注迁移说明，并在对应路径文档登记降级/共存策略。
