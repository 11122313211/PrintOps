# PrintOps 实施验收矩阵（开发工作稿）

**验收日期：2026-10-15。状态：本轮开发回归通过；真实语料缺失，发布阻断。**

用户已确认本阶段没有可用的真实脱敏语料。本轮代码交付条件是：发布门槛可执行，并在缺少真实语料时保持阻断；不要求用合成样例补齐真实语料。代码改造验收与稳定发布验收分别记录。

本文把 `pasted-text-1.txt` 中的开发要求逐项映射到当前代码、测试证据和剩余工作。状态含义如下：

- **已实现+已测**：代码路径、针对性测试与本轮完整回归已核对；验证范围受所列环境限制。
- **部分实现**：主路径已存在，但仍有边界、环境或产品验收缺口。
- **未验证**：代码或离线测试存在，尚无所需真实环境/人工证据。
- **阻断发布**：当前条件明确不满足要求，不能用合成数据或接口桩代替。

## 1. 契约冻结

| 要求 | 当前实现和证据 | 状态 | 剩余验收动作 |
| --- | --- | --- | --- |
| 业务订单 schema 2.0、内部尺寸 mm | `schemas/order-v2.schema.json`、`order_contract.py:canonical_order`；`measurement_model.py`；`unit_normalizer.py`；`tests/test_order_contract_v2.py`、`tests/test_agent.py` | 已实现+已测 | `canonicalOrder` 是同一 revision 的派生只读视图；兼容扁平字段仍存在。继续检查 HTTP/MCP/导出一致性，不能把视图宣称为已全面替换旧内部结构。 |
| 状态存储 schema 与业务 schema 分离 | `storage_contract.py` 的 `STORE_CONTRACT_VERSION=1.0`；`order_model.py` 的状态迁移版本为 3；`docs/STORAGE_CONTRACT.md` 已说明差异 | 已实现+已测 | 在发布说明中固定“存储契约 1.0 / 业务订单 2.0 / 内部状态迁移 3”三种版本，避免统称 schemaVersion。 |
| 缺失值、待确认值显式表达 | `order_contract.py` 输出 `missing`、`pending_confirmation`、`candidateValue`；原文保留在外层 `fieldMeta`、`fieldCandidates`，canonical 只含 `evidenceRef` | 已实现+已测 | `tests/test_order_contract_v2.py` 验证待确认数量阻断计价、生成和询价；确认后解锁。否定清除及候选替换回归通过。 |
| 价格、供应商、存储默认模式 | `rate_cards/cn-local-reference.json`、`storage_factory.py`、`supplier_connectors.py` 分别声明 local reference、sqlite、export only | 已实现+已测 | 运行时启动检查中打印/记录最终模式；生产配置仍需部署环境验收。 |

## 2. 存储和历史

| 要求 | 当前实现和证据 | 状态 | 剩余验收动作 |
| --- | --- | --- | --- |
| `OrderStore` 可替换接口 | `storage_contract.py`；`storage_memory.py`、`storage_sqlite.py`、`storage_factory.py`；`tests/test_storage.py` | 已实现+已测 | 已包含在完整回归；Postgres 尚未实现，只预留替换接口。 |
| SQLite 默认、memory 可选、custom 显式注册 | `create_store()` 仅允许内置模式或注册工厂，拒绝任意动态 import；`docs/STORAGE_CONTRACT.md` | 已实现+已测 | 检查部署环境的 `PRINTOPS_STORE` 与数据目录权限；验证多进程只使用 SQLite。 |
| 快照、事件、消息、报价、审计表 | `storage_sqlite.py` 创建 `orders/order_events/messages/quote_requests/audit_log`，保留 `sessions` 镜像 | 已实现+已测 | 做一次旧库备份/恢复演练，确认备份可读且审计不会被投影重置删除。 |
| revision CAS、防并发覆盖 | `prepare_record()`、SQLite `BEGIN IMMEDIATE`、HTTP 409；`tests/test_storage.py`、`tests/test_storage_api.py`、`tests/test_patch_bridge.py` | 已实现+已测 | 在实际服务进程中用两个客户端重复修改同一 revision，确认前端能刷新而不是覆盖。 |
| 旧 JSON 迁移且保留原文 | SQLite 初始化迁移、`legacy_session_backups`、`legacy_migrated`；旧 JIS B 尺寸保持物理值，旧交接/确认/询价存证并失效；迁移幂等和重启测试 | 已实现+已测 | 用一份历史生产副本（脱敏）演练，记录迁移前后字段差异；当前没有独立迁移 CLI。 |
| 浏览器订单历史后端化 | `server.py` `/api/orders*`；`app.js` 读取后端历史，旧 localStorage 一次性导入；`tests/browser_smoke.spec.mjs` | 已实现+已测 | Chromium 已验证刷新、清除 localStorage 后恢复、归档和完整导出；旧浏览器历史导入失败链路由 API 测试覆盖，仍可补浏览器故障注入。 |
| Postgres/云存储 | 未实现；现阶段只要求预留扩展接口 | 后续扩展 | 若需要云部署，新增独立实现和跨后端契约测试；当前只能声明 SQLite/memory/custom registry。 |

## 3. 记忆、感知和单位

| 要求 | 当前实现和证据 | 状态 | 剩余验收动作 |
| --- | --- | --- | --- |
| 统一 cm/in → mm，保留原文证据 | `unit_normalizer.py`、`measurement_model.py`、`order_model.normalize_order_dimensions()`；`tests/test_agent.py`、`tests/test_pricing_engine.py` | 已实现+已测 | 对 PDF 预检、供应商导出和 UI 手工编辑分别检查单位；现有 UI 手工尺寸编辑要求 mm，cm/in 应在对话入口输入。 |
| 成品/展开/刀模/包装内外尺寸分离 | `DIMENSION_DEFAULTS`、`SEMANTICS`、NLU 标注尺寸；`tests/test_v091_features.py`、`tests/test_agent.py` | 已实现+已测 | 加入一条同时包含成品、展开、内尺寸和外尺寸的真实脱敏案例，检查四个字段不串项。 |
| 克重、工艺、装订词汇归一 | `domain_vocab.py`；`normalize_print_term()`；`tests/test_pricing_engine.py` 推荐方案测试 | 已实现+已测 | 检查所有 UI、handoff、estimate 文案；禁止旧 `157g`、`250g` 形式从推荐结果泄漏。 |
| 否定词、修改词、相对交期 | `nlu.py` 的正向窗口、否定窗口、中文数量、工作日/周表达；`tests/test_v091_features.py`、`tests/test_planner_loop.py` | 已实现+已测 | 用真实客服文本复核“不要 A，改 B”“下周内”“两工作日”等组合；当前只保存原始相对表达，没有日历/节假日承诺。 |
| 多产品隔离和 itemIndex | `perception_candidates.py`、`nlu.py`、`agent.py` item routing；`tests/test_agent.py` 多产品用例 | 已实现+已测 | 完成三项以上混合订单的人工走查，确认推荐、报价、文件预检各自绑定 item。 |
| 字段来源、置信度、原文依据 | `fieldMeta`、`fieldCandidates`、`candidate_evidence()`、`workbench.js:evidence()`；真实 Chromium 点击确认后刷新恢复 | 已实现+已测 | 仍需真实语料评价证据归属；不能只看 `coverage.ratio` 判断准确率。 |
| 真实语料 ≥20 例且字段准确率 ≥95% | `tests/eval_cases_real.json` 当前 `cases: []`；`tests/evaluate_agent.py --release-gate` 拒绝 pending/record/fail；`tests/test_real_corpus_eval.py` | 门槛已实现；阻断发布 | 待有真实语料后收集并脱敏 20–50 例，逐项人工标注，运行严格评测并归档。测试中的临时人工 fixtures 不能算真实订单。 |

## 4. 计价和校准

| 要求 | 当前实现和证据 | 状态 | 剩余验收动作 |
| --- | --- | --- | --- |
| 独立 Decimal 成本引擎 | `pricing_engine.py`、`pricing_schema.py`；`tests/test_pricing_engine.py` | 已实现+已测 | 成本 golden、边界和完整回归已通过；变更 rate card 时递增版本并更新对应预期。 |
| 参数移出 Python、带来源/版本/区域/有效期 | `rate_cards/cn-local-reference.json`、`pricing_schema.validate_rate_card()`；校验来源引用、完整费率、日期、计数单位、税运口径 | 已实现+已测 | 任何参数变更递增版本并人工复核；当前数据明确是 illustrative，不是供应商实价。 |
| 材料、准备、运行、工艺、装订/模切、包装、最低收费明细 | `estimate_order()` 的 `lineItems`、`rateSource`、`knownSubtotal`；`docs/PRICING.md` | 已实现+已测 | UI 验收每条明细、来源和包含/不包含项；检查未知后道为 partial 而非虚构价格。 |
| 缺数量、单位、尺寸、材料、面数时阻断 | `pricing_engine.py` required drivers；无隐藏 500；备选/否定/额外印色及名片专属印色冲突阻断完整区间 | 已实现+已测 | 对每个支持品类补一条 API 错误信息快照，确认 missing 文案可行动。 |
| 名片“份”不得猜成“张” | `quantity_unit_incompatible`、`quantityValidation.converted=false`；`tests/test_pricing_engine.py::test_business_card_copies_are_rejected_without_guessing_sheet_count` | 已实现+已测 | 在聊天和 UI 里确认用户能看到“接受张，请确认实际计数”，且不产生价格区间。 |
| 推荐和依据使用 canonical mm/g/m² | `tools.recommend_processes()` 标准化尺寸/纸张；推荐回传完整 `estimate` | 已实现+已测 | 继续扫描推荐、handoff、前端展示文本，避免 legacy `g` 或裸 `A4` 作为物理依据。 |
| 数量单调、最低收费只补差 | 成本组件非负线性，`minimum_adjustment`；对应 monotonic/minimum 测试 | 已实现+已测 | 更新 rate card 后重复数量边界测试，不允许重新引入阶梯反向下降。 |
| 未知工艺/材料/品类不套通用基价 | `partial`/`blocked`、`unpriced`、独立类别覆盖；计价测试 | 已实现+已测 | 前端把 partial 显示为“部分成本已核算”，不可把 coverage ratio 当价格准确率。 |
| CSV 校准离线、可比条件严格、只出 draft | `tools/calibrate_pricing.py`；`docs/examples/pricing-calibration.example.csv`；校准测试 | 已实现+已测 | 仅导入有授权的脱敏报价；人工复核后另行发布 rate card，CLI 不自动发布。 |
| 真实市场/供应商报价 | 当前只有人工构造示例参数，无实时网络 | 阻断该能力 | 取得授权报价源、口径、有效期和复核人后，单独建立校准和上线记录。 |

## 5. 工具调用和执行规划

| 要求 | 当前实现和证据 | 状态 | 剩余验收动作 |
| --- | --- | --- | --- |
| 白名单工具、输入输出 schema、无任意参数透传 | `tools.py` `TOOL_SCHEMAS`、`Agent.call_tool()`；`tests/test_tool_boundaries.py`、`tests/test_agent.py` | 已实现+已测 | 对每个公开工具跑 JSON schema 和错误参数测试；保持多产品 aggregate price/handoff 阻断。 |
| 工具结果成为规划上下文，最多有界循环 | `agent.py` planner receipts/native tool loop；`tests/test_planner_loop.py` | 已实现+已测 | 用真实模型 endpoint 做一次 tool-call/fallback parity；离线 fake planner 不能替代真实模型验收。 |
| 错误、超时、重试、幂等可观察 | Agent run trace、quote idempotency、supplier guarded transport | 部分实现 | 真实 HTTP Handler 和浏览器端验证 requestId、超时、恢复；当前供应商 transport 仍是可注入协议。 |
| 本地规则回退可独立工作 | `nlu.py`、Agent 规则路径与无模型测试 | 已实现+已测 | 启动无模型/断网的 10 分钟北极星场景，记录澄清轮数和失败输入。 |
| dsh、真实模型 endpoint、开关 parity | 本轮 `tools/dsh_mcp_smoke.py` 通过，planner 离线回归通过 | 离线已测；真实环境未验证 | 在目标内网环境执行真实 dsh headless、模型 endpoint、开关前后对照并保存摘要。 |

## 6. 供应商中立连接器

| 要求 | 当前实现和证据 | 状态 | 剩余验收动作 |
| --- | --- | --- | --- |
| `SupplierConnector` 协议、registry、未知 provider 显式失败 | `supplier_contract.py`、`supplier_connectors.py`；`tests/test_supplier_connectors.py` | 已实现+已测 | 20 项连接器协议测试已包含在完整回归。 |
| export_only/sandbox/live 分层，默认无网络 | `ExportConnector`、`MockConnector`、`GuardedTransportConnector`；所有 bundled providers verified=false | 已实现+已测 | 部署前检查 live feature flag 未启用；不能把 mock 或 export draft 说成已发送。 |
| 超时、有限重试、幂等、取消、审计 | `TransportPolicy`、`RequestJournal`、SQLite journal；供应商定向测试覆盖 | 已实现+已测 | 运行跨进程 journal 演练；将 unknown 状态交给人工核对流程。 |
| 盛大真实 API、字段、授权、生产能力 | 仓库没有真实 transport、凭据、官方字段契约或联调 | 阻断该能力 | 取得供应商授权和 API 文档后做 sandbox contract test，再人工批准 live。 |
| 人工确认可认证、可追责 | 有 `HumanConfirmation` 数据结构和校验 | 部分实现 | 仍需宿主身份认证、服务器保存的确认记录和权限审计；当前 Python 值不是签名 token。 |

## 7. 前端工作台

| 要求 | 当前实现和证据 | 状态 | 剩余验收动作 |
| --- | --- | --- | --- |
| 字段来源、证据、待确认操作 | `workbench.js`、`app.js` 字段渲染、`field-confirm`；Chromium 真实点击并刷新恢复 | 已实现+已测 | 跨浏览器和真人可理解性仍待验证。 |
| 价格区分本地估算/供应商报价 | `renderPriceEstimate()`、`priceDetails()`；浏览器展开所有明细并核对 illustrative 来源 | 已实现+已测 | 继续真人走查 partial、blocked、ready 三种文案的理解情况。 |
| 供应商状态、仅导出和未配置 | `renderProviderStatus()`、`providerStatus()`；浏览器选择通用、未配置和 mock 平台 | 已实现+已测 | 不能把本地/mock 状态验证当作实际供应商联调。 |
| 订单历史刷新恢复、归档、导出 | 后端 API + Chromium 实际刷新、清存储恢复、下载完整 JSON、归档只读与重置 | 已实现+已测 | 断开供应商/模型网络时规则订单仍可工作；不把浏览器到本机服务断线解释为必须具备完整离线编辑队列。 |
| 供应商超时、重试、取消、旧报价 stale | 后端 quote 状态与按钮、Agent stale/idempotency 测试 | 部分实现 | 浏览器结合模拟故障走完整状态机；未验证真实网络故障。 |
| 窄屏、PDF 预检、错误可读性 | Chromium 390×844 实测无横向溢出、抽屉打开/ESC 关闭与填写正常；PDF 工具测试独立 | 部分验证 | 真实移动浏览器/触屏、真实 PDF 印前能力和真人可理解性仍须独立验证。 |

## 8. 发布门槛和开发顺序

开发回归：`PYTHONPATH=. python3 tests/evaluate_agent.py`。保持原有行为：真实语料为空或不足 20 例仅报告，已达 20 例但准确率不足会失败。

严格发布检查：`PYTHONPATH=. python3 tests/evaluate_agent.py --release-gate`。必须同时满足合成评测字段准确率 ≥90%、完整率 ≥80%，真实语料至少 20 例且字段准确率 ≥95%；缺失、为空、格式无效、不足样本或准确率不足均返回退出码 1。JSON 输出明确给出 `releaseGateEnabled` 和 `releaseGatePassed`，退出码不能由“合成测试通过”替代。

本次可追溯的运行证据（合成输入与临时数据库，不计作真实客户语料）：

| 范围 | 命令/证据 | 结果 |
| --- | --- | --- |
| Python 完整回归 | `python3 -m unittest discover -s tests -p 'test_*.py'` | **419 项全部通过**，含旧单位产物失效、异常旧状态及当前 schema 元数据兼容 |
| 计价、旧工具集成 | `python3 -m unittest tests.test_pricing_engine tests.test_v09_features.PriceModelTest tests.test_v09_features.IndustryDepthTest -q` | 29 项通过，含印色歧义/额外印色与费率来源校验 |
| 存储、迁移、HTTP/CAS | `python3 -m unittest tests.test_storage tests.test_storage_api tests.test_persistence tests.test_patch_bridge tests.test_runtime_hardening tests.test_http_handler` | 存储替换、旧库迁移、CAS、只读恢复和审计回归通过，纳入全套总数 |
| 供应商协议 | `python3 -m unittest discover -s tests -p 'test_supplier_connectors.py'` | 20 项通过；全部为本地/mock/注入传输测试 |
| 发布门槛逻辑 | `python3 -m unittest tests.test_real_corpus_eval -v` | 12 项通过，包含空/缺失/无效/不足语料阻断、准确率失败、合成回归失败及双门槛通过 |
| 浏览器开发冒烟 | `npm run test:browser`；`reports/browser-smoke/report.json`，2026-10-16 复跑 | Chromium 12 项通过，0 跳过/失败/flaky；范围详见 `docs/BROWSER_SMOKE.md` |
| 严格发布命令 | `PYTHONPATH=. python3 tests/evaluate_agent.py --release-gate --json` | 主流程最新运行：111 条合成案例，字段准确率 100%、完整率 93%；真实 verdict=pending；releaseGatePassed=false；退出码 1 |
| 价格校准 CLI | 示例 CSV 输出至临时目录 | 1 桶、0 拒绝、0 过期、published=false；示例为人工构造 |
| 真实语料 | 直接检查 `tests/eval_cases_real.json` | 0 例，发布门槛保持阻断 |
| 业务 JSON Schema | Draft 2020-12 校验器（仅临时安装） | schema 合法；6 个空值/多产品/待确认/包装尺寸样例符合结构 |
| MCP 和静态检查 | `tools/dsh_mcp_smoke.py`、`tools/secret_scan.py`、JS 语法、`git diff --check` | 通过；dsh 为本地 MCP 冒烟，非真实模型联调 |

执行日志保存在运行工作区 `reports/development-acceptance/`（Git 忽略），浏览器报告和截图保存在 `reports/browser-smoke/`。这些都是本轮本地开发证据，不是生产客户数据。

后续开发/发布顺序：

1. 本轮已完成代码、自动化回归及文档；评审后再提交变更。尚未 commit、push 或发布。
2. 待真实语料可用后补齐 `tests/eval_cases_real.json` 至至少 20 例；字段准确率必须达到 95%；运行 `--release-gate` 保存退出码和报告。在此之前不填造假的“真实”语料。
3. 已完成 Chromium 刷新/历史/归档、字段确认、价格明细和窄屏冒烟；继续真人、跨浏览器及故障注入验证。
4. 在目标环境完成 dsh headless、真实模型 endpoint 和规则/模型 parity；记录 provider 响应摘要，不保存密钥或客户原文。
5. 在没有供应商授权前，产品能力限定为 export_only 本地草稿；发布仍必须满足真实语料门槛。真实 sandbox/live 另开变更和人工批准。
6. README、ROADMAP、CHANGELOG 与发布清单已同步。VERSION 保留 1.2.0；通过真实验收后再决定正式版本和发布。

**当前发布结论：阻断。** 代码已形成可评审的存储、单位、感知、计价和连接器边界；严格发布检查能够执行并在空真实语料时失败，满足本轮保留门槛的要求。稳定发布仍需真实语料、真人走查和目标环境真实 dsh/模型证据；真实供应商接入须另行授权和联调，不能由本地测试替代。
