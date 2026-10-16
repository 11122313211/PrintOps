# 1.2 发布门槛与验收清单

本文回答“什么时候可以正式发布 1.2”。`VERSION` 仍为 `1.2.0` 候选基线，当前未发布改造增加存储、mm 契约、参考计价和供应商协议；逐项实现证据见 [实施验收矩阵](IMPLEMENTATION_ACCEPTANCE.md) 与 [下一阶段开发验收](NEXT_STAGE_ACCEPTANCE.md)。本清单继承历史门槛，不能把代码合并或旧测试结果等同于稳定发布。

用户已确认当前没有真实脱敏语料。本轮代码交付要求是严格门槛可执行且保持阻断；真实语料不能由测试 fixtures、示例报价或人工编写需求代替。正式发布必须运行 `--release-gate`。

## v1.0.0 历史门槛（继承）

| # | 门槛 | 状态 |
| --- | --- | --- |
| 1 | 静态路由白名单 + 本地访问令牌 | ✅ 已完成（`server.py`，401/403 守卫，跨来源拒绝） |
| 2 | 真实 HTTP Handler 测试（socket 级） | ✅ 已完成（`tests/test_http_handler.py`，13 例） |
| 3 | Key 明文风险提示 + 敏感扫描扩充并纳入 `data/` JSON | ✅ 已完成（设置面板警告 + `tools/secret_scan.py`） |
| 4 | 真实脱敏订单评测（至少 20 例，字段准确率 ≥95%） | ⛔ 当前 `cases=[]`；严格发布检查非零退出，等待真实语料 |
| 5 | 死代码清理 + 合并 `_update_item`/`_update_order` | ✅ 已完成（统一到 `_apply_patch`） |
| 6 | SQLite WAL + busy_timeout + 损坏会话恢复 | ✅ 已完成（`tests/test_persistence.py`） |
| 7 | 真人使用走查（10 分钟北极星场景） | ⏳ **需要真人执行**（脚本见下） |

## v1.1.0 新增集成门槛

| # | 门槛 | 状态 |
| --- | --- | --- |
| 8 | 标准库 MCP stdio server、session binding 与 L0/L1 capability 边界 | ✅ 已完成（`mcp_server.py`、`tests/test_mcp_server.py`） |
| 9 | dsh 元数据/profile、5 个印刷 skill 与受信 launcher 校验 | ✅ 已完成（`.dsh/`、`tests/test_dsh_metadata.py`、`tests/test_dsh_runtime.py`） |
| 10 | 无 npm/pnpm 的 Python local host 与 MCP transcript smoke | ✅ 已完成（`tools/printops_local_host.py`、`tools/dsh_mcp_smoke.py`、`tests/test_local_host.py`） |
| 11 | native `tool_calls`/JSON fallback、有界 3 轮工具循环与上下文预算 | ✅ 已完成离线契约验证（`llm_adapter.py`、`tests/test_agent.py`） |
| 12 | 真实 dsh headless、真实模型 endpoint 和 dsh on/off parity | ⏳ **需要内网环境与真实运行时验收** |
| 13 | 浏览器级工具时间线、错误恢复与窄屏冒烟 | ◐ Chromium 与 WebKit 桌面/窄视口自动化通过，详见 [BROWSER_JOURNEY](BROWSER_JOURNEY.md)；Firefox 启动环境阻断，真实移动和人工走查未完成 |
| 14 | 真实供应商 live 接入 | ⏸ 不在当前候选范围，授权接入规划为 v1.3+；当前无真实 transport 或 live 工具入口 |

## v1.2.0 新增门槛

| # | 门槛 | 状态 |
| --- | --- | --- |
| 15 | 优雅关闭（SIGINT/SIGTERM，等待在途请求，退出码 0） | ✅ 已完成（`server.py`、`mcp_server.py`、`tests/test_runtime_hardening.py`） |
| 16 | 并发连接上界与超限拒绝 | ✅ 已完成（`PrintOpsHTTPServer`，超限返回 503） |
| 17 | 请求超时与结构化日志（requestId / 耗时） | ✅ 已完成 |
| 18 | 启动自检 `python3 server.py --check` 与可读失败原因 | ✅ 已完成 |
| 19 | 零依赖 Markdown 渲染：仅白名单标签、无 `on*` 事件处理器、链接协议白名单 | ✅ 已完成（`markdown.js`、`tests/test_markdown_render.py`） |
| 20 | 整请求截止时间（防滴灌式慢客户端占满连接槽） | ✅ HTTP 共用 monotonic deadline：请求行/头/体、锁等待、SQLite、模型各轮/重试/回退和响应；`tests/test_request_deadlines.py` 覆盖慢客户端和超时无迟到提交。系统 DNS 等不可硬取消操作的受限线程边界见 [RUNTIME_BUDGET](RUNTIME_BUDGET.md) |

## 2026-10 未发布改造验收

| # | 门槛 | 实现与限制 |
| --- | --- | --- |
| 21 | 业务 schema 2.0、结构化尺寸 mm、g/m²、证据与候选 | `tests/test_order_contract_v2.py`、字段来源测试；canonical 视图与兼容扁平字段共存，交期不承诺节假日排期 |
| 22 | 可替换存储、旧库迁移、CAS、消息/事件/审计、历史后端化 | `tests/test_storage.py`、`tests/test_storage_api.py`、持久化和跨进程测试；内置 SQLite/memory，Postgres 未实现 |
| 23 | Decimal 计价 golden、来源有效期、缺驱动阻断、数量单调 | `tests/test_pricing_engine.py`；当前参数全部为 illustrative，不能作为市场或供应商报价 |
| 24 | 离线校准与发布隔离 | CSV 仅生成统计 draft，不能写活动 rate_cards 或自动发布；示例 CSV 不是供应商数据 |
| 25 | supplier registry、无网络默认、mock、超时/幂等/取消/审计 | `tests/test_supplier_connectors.py`；测试使用注入传输，不证明已授权接入；live 仍需认证人工处理器和真实供应商联调 |
| 26 | 严格真实语料发布检查 | `tests/test_real_corpus_eval.py`；`--release-gate` 必须拒绝空库、不足样本、无效语料和低准确率，目前按预期阻断 |

当前代码的完整测试总数、合成指标与浏览器结果以最终运行记录为准，不引用旧版本数字作为本轮验收证据。

## 1.2 集成门槛说明

本地可重复执行以下无第三方依赖检查；它们证明 MCP/工具边界，不冒充真实 dsh 或真实模型端到端：

```bash
python3 tools/dsh_mcp_smoke.py
python3 tools/printops_local_host.py --session-id release-smoke --smoke
```

原生 tool-call 与上下文预算由离线测试覆盖。真实 dsh headless、真实内网模型和浏览器检查必须在相应环境单独记录 provider 响应格式、工具请求/结果摘要和失败原因，不得记录 API Key 或完整客户原稿。

## 门槛 4：真实脱敏订单语料

合成语料由实现同一套规则的人编写，不能证明真实用户话术下的表现。

1. 收集 20-50 例真实订单需求（客服记录、聊天记录、邮件均可），逐条**脱敏**：
   删除客户名称、电话、地址、邮箱、订单号等个人信息，只保留印刷需求描述。
2. 用标注辅助工具生成用例草稿（只打印到控制台，不写语料文件）：
   `python3 tools/annotate_case.py --name "真实-名片001" --turn "客户原话（脱敏后）"`，
   多轮对话重复 `--turn`。输出的 expected 是 Agent 提取的**建议值**（系统默认值已剔除），
   必须逐项与原话核对后保留——没提过的字段删掉，提过的值不符合的改掉。
3. 核对后把 JSON 粘贴进 `tests/eval_cases_real.json` 的 `cases` 数组，脱敏要求同上。
   （不使用工具时，按文件内 `_instructions` 的结构手写也可。）
4. 发布检查运行 `PYTHONPATH=. python3 tests/evaluate_agent.py --release-gate`：报告分别输出合成与真实语料。真实语料缺失、为空、不足 20 例、格式错误或字段准确率 <95% 都以退出码 1 阻断；合成字段准确率还须 ≥90%、可完成用例完整率 ≥80%。加 `--json` 可保存 `realCorpusVerdict`、`releaseGateEnabled` 和 `releaseGatePassed`。
5. 未达 100% 的用例会逐字段打印期望值与实际值；修复规则或在 `expected` 中纠正标注后重跑。

日常开发可不加 `--release-gate`，此时真实语料不足仅记录；这个命令成功不代表发布通过。门槛单元测试中的临时“真实格式”样例是人工构造，仅验证退出码与阈值，不计入真实语料数量。

## 门槛 7：真人走查（10 分钟合格交接）

固定任务卡、可提供业务信息、许可规格集合、独立评分表、理解题和空白记录统一放在 [USER_WALKTHROUGH](USER_WALKTHROUGH.md)。**当前只准备了验收包，真人执行与结果仍留空。**

- 8 名未参与开发的非专业首次用户，每人一个主任务：名片 3 人、单页 3 人、标准宣传册 2 人。补充的多产品、文件、故障恢复场景另报，不混入主指标。
- 开始：服务就绪后，首条有效需求提交。结束：当前版本通过必要校验和关键生产参数核对、明确确认，导出后实际打开文件并与该版本核对一致。任务内澄清、等待、修改、确认和导出均计时；安装启动单列。
- 主指标：600 秒内独立完成且内容复核合格的任务数 / 所有已开始主任务数。失败、超时、放弃和得到操作帮助的任务不得从分母删除。
- 初轮目标：至少 7/8 合格完成；关键数量/尺寸错误、产品串项、过期确认沿用均为 0；8 人均理解示例估算不是供应商报价、订单尚未发送。逐品类样本很少，只作形成性验证，不外推总体成功率。
- 主持人不提示按钮位置或步骤，只按固定业务信息卡回答需求问题；不同合理工艺按许可集合评分，不能事后改答案让结果通过。
- 主任务无文件时明确“文件待交付、未检查”。有文件补充任务必须记录合成文件版本、归属、基础检查范围；不能把文件基础预检当成生产许可。
- “缺失字段清空”“可以生成”“点击导出”都不能作为最终通过依据。正式记录包含原始产物、对应版本/确认、逐人耗时、帮助/失败原因、理解题原话和独立评分。

### 本轮记录入口

| 材料 | 状态 / 实际记录位置 |
| --- | --- |
| 固定任务卡与 8 人分配 | 已定义于 USER_WALKTHROUGH，尚未执行 |
| 逐人计时、独立评分、原始导出 | |
| 四道理解题与关键错误清单 | |
| 补充文件 / 恢复 / 多产品 / 环境验证 | |
| 验收结论与下一轮问题 | |

可用性任务均为人工编写，不能计入门槛 4 的真实客户语料。真人数据留空时不得写“通过”。

## 发布前最终检查（每次发布都要跑）

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=. python3 tests/evaluate_agent.py --release-gate
python3 -m py_compile agent.py server.py llm_adapter.py product_knowledge.py supplier_adapters.py supplier_contract.py supplier_connectors.py order_model.py order_contract.py measurement_model.py unit_normalizer.py domain_vocab.py perception_candidates.py storage_contract.py storage_factory.py storage_sqlite.py storage_memory.py pricing_schema.py pricing_engine.py nlu.py tools.py mcp_server.py tools/calibrate_pricing.py tools/dsh_mcp_launcher.py tools/dsh_mcp_smoke.py tools/printops_local_host.py request_runtime.py storage_policy.py storage_backup.py examples/supplier_mock.py tools/order_backup.py
python3 tools/secret_scan.py
python3 tools/dsh_mcp_smoke.py
git diff --check
```

浏览器命令与报告位置见 [BROWSER_SMOKE](BROWSER_SMOKE.md)，需要独立安装开发测试依赖；运行产品本身仍只需 Python 标准库。真人走查和真实 dsh/模型验收必须在相应环境记录，不由自动浏览器或 mock 结果替代。

转为稳定 1.2 发布前还需：门槛 4 真实语料达标、门槛 7 真人走查、门槛 12 真实 dsh/模型、门槛 13 浏览器范围及未完成运行时门槛闭合，并核对 `README`/`VERSION`。真实供应商 live 不是当前本地候选版的发布条件；后续启用需独立取得授权、通过 sandbox 和字段契约、实现人工身份认证与服务器确认记录，完成幂等/对账/审计验收。
