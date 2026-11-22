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

## V5-02 执行记录（2026-11-22）

TD-10 落地：`tests/evaluate_agent.py` 新增 `_longtail_cases()`（12 例，tag="长尾"），
`CASES = _hand_cases() + _matrix_cases() + _longtail_cases()`；未改任何产品代码
（nlu.py / product_knowledge.py / agent.py / llm_adapter.py / server.py 均未动），
`COMPLETION_TURNS`/`EXPECT_COMPLETE` 未动。期望值钉法：先读品类 profile hint，
再用临时探针脚本（reports/ 下，gitignore 路径，未提交）逐句调 Agent+Memory 看
fieldMeta/order 归一值，hint 口径与引擎行为一致后才钉入 expected。

### 12 例清单

| name | 话术 | 断言字段 |
| --- | --- | --- |
| 长尾-纸杯单PE大容量档 | 做 3000 个纸杯，500ml，单PE | productType / quantity / cupVolume=500ml / cupMaterial=单PE |
| 长尾-纸杯内淋膜250档 | 做 8000 个250ml纸杯，需要内淋膜 | productType / quantity / cupVolume=250ml / innerCoating=需要内淋膜 |
| 长尾-喷画平米计价 | 做 6 平米户外灯布喷画 | productType / quantity=6 平方米 / displayMaterial=灯布 / install=户外 |
| 长尾-喷画室内视距 | 做 100 张室内写真布喷画，观看距离 5 米 | productType / quantity / displayMaterial=写真布 / install=室内 / viewingDistance=5米 |
| 长尾-PVC卡038冲切 | 做 500 张 0.38mm PVC卡，异形冲切卡 | productType / quantity / cardType=冲切卡 / cardThickness=0.38 mm |
| 长尾-PVC卡磁条 | 做 1000 张PVC卡，需要磁条 | productType / quantity / chip=需要芯片/磁条 |
| 长尾-联单三联打号 | 做 200 本三联送货单，需要打号 | productType（送货单别名）/ quantity / paperParts=三联 / numbering=需要连续编号 |
| 长尾-联单四联 | 做 150 本四联无碳联单 | productType / quantity / paperParts=四联 |
| 长尾-手提袋牛皮纸扁绳 | 做 400 个牛皮纸手提袋，扁绳提手 | productType / quantity / bagMaterial=牛皮纸 / handle=扁绳 |
| 长尾-手提袋白卡承重 | 做 300 个白卡纸手提袋，覆膜，承重 5 公斤 | productType / quantity / bagMaterial=白卡纸 / finishing=覆膜 / loadBearing=5kg |
| 长尾-异形标签枚计 | 做 5000 枚异形透明不干胶标签，需要刀模 | productType / quantity=5000 枚 / labelShape=异形 / labelMaterial=透明不干胶 |
| 长尾-异形标签万枚改张 | 做 2万枚异形铜版不干胶标签 → 数量改成 3000 张 | productType / quantity=3000 张 / labelShape=异形 / labelMaterial=铜版不干胶 |

### 最终评测数字

- 总数 111 → **123**（55 手写 + 56 矩阵 + 12 长尾），通过 **123/123**；
- 字段准确率 **100%**（390/390 个 expected 键，较基线 342 新增 48 键全中）；
  tag 分组"长尾"12 例 100%，其余 7 组保持 100%；
- 可完成用例基础完整率 **93%**（分母 14 例不变，13/14）；
- 平均达标轮数 1.27、平均响应 23.3ms——111 例基线同口径复跑为 1.27/93%，
  本卡纯增量无回归；
- `python tools/dev_check.py` 六步全绿（compileall / unit tests 585 项 /
  evaluation 123 例 / knowledge review / secret scan / repo guard）；
- 种子复跑：`PYTHONHASHSEED=1/2/3 python -m unittest discover -s tests -p "test_*.py"`
  三次全绿（585 项，4 项平台跳过；TD-20 教训落实）；
- `tests/test_real_corpus_eval` 19 项无回归。

### 勘误清单（话术口径调整记录，均为"调整话术落 hint 口径"，产品代码未改；建议随 TD-09 溯源评估）

| 例名 / 方向 | 现象 | 处理 |
| --- | --- | --- |
| 纸杯"9 盎司"容量 | `cupVolume` 提取仅匹配 ml/毫升（nlu.py:447-449），盎司不落库；hint 容量档即 ml（250/350/500 不自动换算） | 话术调整至 hint 档位："9 盎司+材质"改为"500ml+单PE" |
| 纸杯"内涂" | `innerCoating` 提取只认"淋膜"（nlu.py:453-456），"内涂"不落库；hint 问句口径为"防水淋膜/内淋膜" | 话术改"需要内淋膜" |
| 喷画"喷绘布" | `displayMaterial` 提取词表（nlu.py:458-460）无"喷绘布"，介质整键缺失；hint 问句列举"背胶、灯片、车贴、灯布" | 话术改词表内的"灯布"（另一例用"写真布"，同在词表） |
| 喷画"3×2 米"尺寸 | 尺寸系统单位集为 mm/cm/inch（unit_normalizer.py），"米"不支持；无单位尺寸按"缺失单位永不默认"进入"需明确 mm 数值"确认态（探针实测 size/displaySize 记为裸值"3×2"） | 平米话术改为纯平方米数量表达（"6 平米"→quantity=6 平方米），不钉 displaySize |
| 手提袋"白卡"口语 | `bagMaterial` 仅匹配"白卡纸"全词（nlu.py:437-439），口语"白卡"不落库，而知识问句口径含"白卡"（product_knowledge.py:316） | 话术用"白卡纸"；"白卡"口语落库缺口留给 TD-09 |
| PVC卡"异形冲切卡"直连 | "PVC异形冲切卡"整串会被品类识别为板材 PVC——"PVC"别名后接非 ASCII 汉字仍算有效提及（nlu.py:76-77 ASCII 边界检查），卡型/厚度全丢 | 话术写作"PVC卡，异形冲切卡"保住品类别名，cardType 归一值为"冲切卡"；"异形"卡型前缀落库留给 TD-09 |

### 计数同步（grep "111" 结论）

按卡片口径 grep README.md / README.en.md / CONTRIBUTING.md / docs/ROADMAP.md /
docs/PRICING.md：命中仅 5 处——README.md:145 与 README.en.md:145（2026-10-25
1.3.0 候选基线，带历史日期）、docs/ROADMAP.md:68 与 :71（v1.0.0 已归档基线）、
README.md:40 / README.en.md:40（GitHub 仓库名含"111"）；**五个文件内无当前状态
计数表述**，按"历史表述一字不动"口径未做改动，当前数字以本节为准
（123 = 55 手写 + 56 矩阵 + 12 长尾）。范围外现状句（如
docs/plan/path-05-eval-quality.md:13、docs/IMPLEMENTATION_ACCEPTANCE.md:109
"主流程最新运行：111 条"）留待 V5-03 收口时统一复核，本卡不越界改动。

## V5-01 执行记录（2026-11-22）

三件交付物全部落地（提交 bd8062c）：`tests/test_dual_track_equivalence.py` 10 组
差分用例（等价面：15 订单标量字段 + dimensions/productSpecs + missingFields +
workflowStage；白名单差异面逐项钉住）；提示词契约绊线 5 例 + `PLANNER_PROMPT_VERSION="1"`
变更锚（llm_adapter.py +8 行，唯一产品代码改动）；`tools/compare_modes.py`
CLI（--case-file/--out/--json/--baseline；KNOWN_DIVERGENCES 台账只许缩不许涨）。
配套 tests/test_compare_modes.py 11 例。全套单测 611 项，3 种子复跑全绿，
变异自证两轮红→恢复→绿（等价面剧本变异 + provenance 钉变异）。

**产出分歧清单**：①估价意图两轨 workflowStage 不一致（recommend vs quote，
call_tool 置态粘性）——超出白名单，登记 **TD-25** 待裁定，KNOWN_DIVERGENCES
预登记 + 差分用例 2 单独钉住；②同值重述翻牌不对称（quantity 三键全翻 vs size
派生字段保持 rule）——确认为有意设计，钉住；③planner 文字总结被强制工具作废
——安全设计澄清；④capability/否定询价两轨消息"巧合级同串"——已钉防单侧漂移；
⑤fieldMeta 差分必须走 {source, confidence} 稳定投影（runId/updatedAt 易变）。
内置默认集等价面差异 1/190（0.53%），全部为预登记已知分歧。
