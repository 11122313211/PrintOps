# v1.5 薄背包执行计划（TD-08 + TD-10）

> 2026-11-22 定稿。v1.4.0 参考版发布（tag `v1.4.0`，Release 资产已上传）后，
> ROADMAP"v1.5+ 顺延项"收敛为两条计划内任务（TD-08、TD-10）+ 维护轨。
> 本文件把两条任务卡落到文件级与验收级，执行顺序 **V5-01 → V5-02 → V5-03**，
> 每卡一笔独立提交，每卡后跑 `python tools/dev_check.py`（六步全绿为过）。

## 背景与边界

- 组织性门槛（真实脱敏语料 ≥20 例、8 人真人走查、真实 dsh/模型端到端）永久不可行；
  发布口径维持**参考版**层级（`--release-gate --tier reference`，v1.4.0 已验证 exit 0）。
- 市场价采集面已封版（2026-11-22，见 `sd2000-price-probe-2026-11-21.md` 收敛声明），
  仅季度复核（`nextReviewAt=2027-02-19`，偏差 >15% 触发重标定）。
- 明确不做：app.js 模块化、流式输出、供应商 sandbox/live 接入、真实语料收集、
  稳定版声明。TD-14/15/16 安全硬化为可选项，不在本计划承诺。

## V5-01 TD-08 双轨行为差分断言（登记册 B.2，归属原 path-05 S5-07）

**问题**：111 例评测与绝大多数单测只固化规则轨（`tests/evaluate_agent.py` 每例
`Agent(Memory(...))` 无 planner）；LLM 轨仅有协议级测试（`tests/test_planner_loop.py`
14 例）。"同一句话两轨行为差异"没有任何断言——换提示词/模型的影响不可知。
提示词是 `llm_adapter.py:1234-1255` 的内联字符串，无版本字段，改一行即静默漂移。

**已知可复用设施**（全部现成，零新增框架）：
- `ScriptedPlanner`（`tests/test_planner_loop.py:40-69`）：enabled=True、按序弹出
  预置 plan、记录 calls——差分测试的 LLM 轨替身。
- `FakeResponse` + `patch("llm_adapter.urllib.request.urlopen", ...)`
  （`test_planner_loop.py:26-37`）：驱动真实 `OpenAICompatiblePlanner` 协议栈——
  提示词契约绊线的挂载点。
- 断言面：`chat()` 信封的 `order`/`fieldMeta`（source/confidence/status）/`missingFields`/
  `workflowStage`（`agent.py:3250-3284`）。

**交付物**（三件，全部离线可跑）：

1. `tests/test_dual_track_equivalence.py`（新）：N≥8 组差分用例。每组对同一句话
   各跑一次规则轨 `Agent(memory)` 与 LLM 轨 `Agent(memory, planner=ScriptedPlanner(...))`，
   断言**等价面**：订单字段值（productType/quantity/dimensions/productSpecs.*）、
   `missingFields`、`workflowStage` 一致；断言**白名单差异面**（允许不同、逐项钉住）：
   `fieldMeta.*.source`（rule vs model）、confidence 值、消息文案、toolTrace 形态、
   planner 意图抢跑（post_planner 被 planner 轮取代，`agent.py:1784-1791`）。
   用例取材：quote/price/capability/validation/handoff 各 1 + 否定 1 + 多产品 1 +
   补字段追问 1，与 `PLANNER_INTENT_PATTERNS` 六意图对齐。
2. **提示词契约绊线**（并入 `tests/test_llm_adapter_protocols.py`）：用 FakeResponse
   捕获请求 payload，断言 system prompt（`llm_adapter.py:1234-1255`）包含契约要素——
   JSON 输出形态、patch 白名单字段名（`PATCH_FIELDS`，`llm_adapter.py:304-307`）、
   工具使用规则、置信度语义。改提示词丢掉任一要素 → 测试红，"换提示词的影响"
   从静默变可见。顺手给提示词字符串加 `PLANNER_PROMPT_VERSION = "v1"` 常量
   （拼进 system prompt 或独立注释，变更时必须 bump 并跑差分用例）。
3. `tools/compare_modes.py`（新 CLI）：把一组句子过双轨，输出差异报告
   （元信息/总体差异率/差异清单/结论，结构沿用 `path-05-eval-quality.md:156-164`）
   到 `reports/`（不入库）。默认用 ScriptedPlanner 剧本；`--replay` 读
   `tests/fixtures/planner_replay/` 录制响应（本期只留目录与格式约定，
   真实模型录制按需进行——单人无 CI 联网面，录制 fixture 的漂移风险
   （S5-02）大于价值，此为对 S5-07 卡的有意收窄）。

**验收**：dev_check 六步全绿（含新增用例）；差分用例修复前应在"去掉等价断言"
的变异下失败（自证有效）；`compare_modes.py --help` 与样例报告可复现；
TD-08 迁入登记册 B.5 趋势表（备注：回放 fixture 留格式约定不录制）。
规模：1.5~2d。

## V5-02 TD-10 长尾品类评测用例（登记册 B.2，归属原 path-05 S5-06）

**缺口实测**（111 例按 expected.productType 统计，2026-11-22）：

| 品类 | 现有 | 目标 | 缺口 | 断言字段（均在 nlu.py 有提取器） |
| --- | --- | --- | --- | --- |
| 纸杯 | 2 | 4 | +2 | `cupVolume`（盎司/250ml 两种表达）、`cupMaterial`/`innerCoating` |
| 喷画 | 1 | 3 | +2 | `displayMaterial`、`viewingDistance`、`install`（平米话术） |
| PVC卡 | 2 | 4 | +2 | `cardThickness`（0.38/0.76mm）、`chip`（芯片/磁条）、`cardType` 异形冲切 |
| 联单 | 2 | 4 | +2 | `paperParts`（三联/四联）、`numbering`（打号） |
| 手提袋 | 1 | 3 | +2 | `bagMaterial`（白卡/牛皮纸）、`handle`（棉绳/丝带）、覆膜 |
| 异形标签 | 0 | 2 | +2 | `labelShape="异形"`（刀模 hint）、`labelMaterial`、数量表达（枚/张） |

**落点**：`tests/evaluate_agent.py` 的 `CASES = _hand_cases() + _matrix_cases()`
追加 `_longtail_cases()`（12 例，tag="长尾"，run_suite 按 tag 分组统计自动生效）。
话术方向沿用 `path-05-eval-quality.md:68-79`；expected 只标原话明确提到的字段
（同文件 :79 口径），具体期望值在实现时以 `product_knowledge.py` 品类 hint 的
档位表述为准（如纸杯容量档"250ml/350ml/500ml 不自动换算"→ 盎司话术的期望值
按 hint 口径钉）。

**验收口径**（S5-06 原文）：新用例全绿；**失败例转知识勘误清单**（喂 TD-09 的
溯源来源需求，不阻塞本卡——按知识 hint 修正 expected 或登记勘误，二选一留痕）。
评测总数 111 → 123，README/CONTRIBUTING/ROADMAP 的计数表述同步；
`COMPLETION_TURNS` 不动 → 完整率分母不变；字段准确率阈值（≥90/完整率 ≥80）为
固定常量不受语料加多影响，但每条新 expected 键计入分母，报告数字如实更新。
规模：1d。

## V5-03 收口与发布

1. 登记册：TD-08、TD-10 迁 B.5 趋势表（附差分用例数/评测用例数证据）；
   B.2 若清空则如实标注"行为一致性表暂空"。
2. ROADMAP"当前阶段"改述为 v1.5；CHANGELOG 开 v1.5.0 段；
   VERSION → `1.5.0`（沿用 `chore(release)` 候选模式）。
3. 发布：全量门禁（dev_check + 4 playwright 套件 + 两档 release-gate +
   knowledge_review + py_compile + secret_scan + dsh smoke）→ tag `v1.5.0` →
   Release 资产（zip + SHA256SUMS，按 `docs/RELEASING.md`）→ CI 四组合观察。
   rate card 已带 market-listed 来源，参考档门槛直接复用，无需新标定。
4. 维护轨（不占开发时间，登记即可）：
   - 季度复核 2027-02-19（knowledge_review 自动检查到期）；
   - Mimosa 重扫触发条件不变：renderer 契约变更或引擎运行面变更时
     （历次 seal 记 TD-22，扫描链条延续）；
   - TD-24、TD-09 维持待排期；TD-14/15/16 可选不入承诺。

## 提交与验证节奏

| 卡 | 提交 | 门禁 |
| --- | --- | --- |
| V5-02 先行（纯增量、低风险，顺带把本计划提交的扫描结论带入） | `test(evaluate): add 12 long-tail category cases` | dev_check + 评测报告人工过目 + ≥3 seed 复跑（TD-20 教训） |
| V5-01 | `test(planner): pin dual-track equivalence and prompt contract` | dev_check + 变异自证 + 多 seed |
| V5-03 | docs + `chore(release)` 两笔 | 全量发布门禁 + CI 四腿 |

Mimosa 扫描按既有模式延续：每笔实质提交前钩子报 scanner_enobufs，
补扫结论随下笔实质提交记入 TD-22。
