# 附录B：技术债务登记册（appendix-tech-debt）

> 登记所有已知但未在本计划主路径中立即解决的技术债务。每条含：编号、位置、描述、
> 影响、归属路径（或"待排期"）、状态。规则：新发现的债务先登记再决定是否入路径；
> 已偿还的条目保留并标记日期，作为债务趋势记录。

## B.1 代码结构与正确性

| 编号 | 位置 | 描述 | 影响 | 归属 | 状态 |
| --- | --- | --- | --- | --- | --- |
| TD-24 | perception_candidates.py 子句窗口 | 中文全角标点经 NFKC 展开后子句边界判断被跳过，多产品消息的 fieldMeta evidence quote 常退化为整句 | 依据抽屉原文摘录不够精确 | 待排期（低优先级；证据可追溯性打磨） | 未开始 |

## B.2 行为一致性

| 编号 | 位置 | 描述 | 影响 | 归属 | 状态 |
| --- | --- | --- | --- | --- | --- |
| TD-09 | product_knowledge.py | 行业风险规则缺口：折页压痕线、深色实底折痕爆裂、专色配置说明（ROADMAP v0.9.x 遗留） | 行业专业性不足 | 待排期（v0.14 知识专项；依赖可溯源来源） | 未开始 |
| TD-25 | agent.py call_tool estimate_price 分支 vs _handle_price | 估价意图两轨 workflowStage 不一致：规则轨 _handle_price 不置工作流态（停在 recommend），planner 轨 call_tool 的 estimate_price 分支置 "quote"（对 preflight/quote/export 粘性），同订单同估算区间信封工作流态不同 | 切换 planner 前后 UI 阶段语义漂移 | 待裁定（V5-01 差分断言发现，tools/compare_modes.py KNOWN_DIVERGENCES 已预登记；方向：规则轨同步置 quote，或确认 call_tool 置态为 planner 轨特有语义） | 未开始 |

## B.3 运行与安全

| 编号 | 位置 | 描述 | 影响 | 归属 | 状态 |
| --- | --- | --- | --- | --- | --- |
| TD-14 | llm_adapter.py SSRF | 配置时校验与请求时之间存在 DNS 重绑定理论窗口 | 本地单机威胁模型内可接受 | 路径 9（S9-08） | 计划内 |
| TD-15 | llm_config.json | Key 明文为默认存储 | 本机文件泄露即凭据泄露 | 路径 9（S9-06/07） | 计划内 |
| TD-16 | server.py 静态白名单 | 目录白名单扩展后需保持"`/app/../server.py` 归一化 404"类测试常青 | 路径穿越回归 | 路径 2（S2-01） | 计划内 |
| TD-22 | app.js/journey.js 静态扫描 XSS 入口标记 | 2026-11-21 Mimosa deep 重扫（scan-2026-11-21T09-54-25.592Z，114 项，seal sha256:921c095d…）：与 2026-10-24 基线（113 项）同量级，TD-23 渲染路径修复与 journey.js ERROR_TEXTS 增项未引入新入口；既有 triage 结论继续有效——innerHTML 均走 markdown.js 白名单渲染器（`test_markdown_render.py` 契约锁定），其余为 textContent 或启发式误报。同日二次重扫（scan-2026-11-21T11-58-04.423Z，114 项，seal sha256:2e62afc9…）：覆盖参考版发布层级与 rate card 标定两笔提交（evaluate_agent 两档门槛为评测脚本非运行面、其余为文档与 JSON）， findings 数与无新 hypotheses 维持不变；同日三次重扫（scan-2026-11-21T12-09-29.671Z，114 项，seal sha256:bdf1fbd6…）：覆盖标定提交 98e865d（schema 枚举扩展/卡 JSON/manifest/测试），结论同前；同日四次重扫（scan-2026-11-21T12-20-42.805Z，114 项，seal sha256:d89c8102…）：覆盖 TD-22 记录提交 3171ba7（纯文档），结论同前；同日五次重扫（scan-2026-11-21T12-36-00.868Z，114 项，seal sha256:7f2d99d8…）：覆盖海报回采标定提交 4bebecc（probe 文档/rate card 海报块/契约测试集合），结论同前；同日六次重扫（scan-2026-11-21T12-46-54.127Z，114 项，seal sha256:cb35e16f…）：覆盖胶装画册变体标定提交 08e65d5（variantReferences/卡 v2026.12.04/测试扩展），结论同前；次日七次重扫（scan-2026-11-22T04-32-09.915Z，114 项，seal sha256:39e80b51…）：覆盖 v1.4.0 参考版收口提交 f150849（CHANGELOG 折叠与版本表述），结论同前；历次提交前钩子报 scanner_enobufs（结论迟到）以对应重扫结论补齐；同日八次重扫（scan-2026-11-22T04-51-43.252Z，114 项，seal sha256:de4b9d50…）：覆盖 v1.5 执行计划提交 1c46e88（纯文档），结论同前；同日九次重扫（scan-2026-11-22T07-40-47.545Z，114 项，seal sha256:57e6b7d5…）：覆盖 V5-02 长尾用例提交 491054c（评测语料+12 例/文档/TD-22），结论同前；同日十次重扫（scan-2026-11-22T08-12-54.636Z，114 项，seal sha256:1037bac1…）：覆盖 V5-01 双轨差分提交 bd8062c（新测试/CLI/PLANNER_PROMPT_VERSION 锚），结论同前；同日十二次重扫（scan-2026-11-22T10-02-07.773Z，114 项，seal sha256:68964498…）：覆盖 v1.5.0 发布态（tag v1.5.0=收口提交 21cbdea），结论同前；同日发起全方位安全体检（docs/plan/security-audit-2026-11-22.md，三面代理+主会话复核），产出 TD-26/27/28；同日十三次重扫（scan-2026-11-22T10-18-39.488Z，114 项，seal sha256:da6cb051…）：覆盖审计登记提交 b451474（纯文档），结论同前；同日十四次重扫（scan-2026-11-22T10-30-45.347Z，114 项，seal sha256:0af1edc6…）：覆盖 TD-26 Host 白名单修复提交 471de3a（server.py/测试/文档），结论同前；同日十五次重扫（scan-2026-11-22T10-42-26.245Z，114 项，seal sha256:2e68f154…）：覆盖 TD-27 禁重定向提交 2c3e7f1（llm_adapter/端到端测试），结论同前 | 重扫结论未变；renderer 契约变更或引擎运行面变更时再次重扫 | 待排期（考虑让扫描聚焦源码目录排除 reports/） | 已 triage（2026-11-21 两次重扫） |

## B.4 文档与流程

（当前无未偿条目。）

## B.5 已偿还债务（趋势记录）

| 编号 | 原描述 | 偿还版本 | 备注 |
| --- | --- | --- | --- |
| TD-28 | 健壮性低危批：RecursionError 分类×2、SESSION_LOCKS 只增不删、limit=-1 全表加载、日志换行注入、markdown NUL 占位符、rate card 数值无上界、secret_scan 形态缺口、NLU O(n²) | 1.5.0 后 | 四批修复：server.py（RecursionError→400 INVALID_JSON、limit clamp 1~500、锁有界驱逐 in-use 保护、日志\uu 转义，c4a2707）；llm_adapter 十处解析捕获补 RecursionError + pricing_schema 金额 ≤1e6/因子 ≤10 绝对上界（da08000）；markdown 双入口剥 NUL + secret_scan 补 PEM/Stripe/glpat/npm/JWT/LTAI 六形态（0d905b2）；NLU 合并别名正则单趟扫描 + O(n) 消重（实测别名密集文本 0.68→0.02s，e32c5c0）。评测 123 例等价全绿；S9（sessionStorage 明文）与 H6（health 存活探测面）按体检结论记录维持 |
| TD-27 | 模型请求默认 opener 跟随 3xx 且 Authorization 头跨主机保留、重定向目标不复检私网 | 1.5.0 后 | llm_adapter 导入时进程级安装 _NoRedirect opener：任何 3xx 以 HTTPError 硬失败（不在重试集合）；真实双服务器端到端测试（网关 302 → 目标零命中、错误消息含 302 不含 key）+ 全局 opener 安装断言；ARCHITECTURE SSRF 条目补禁跟随 |
| TD-26 | 浏览器侧 DNS rebinding 全链路（静态路径无 _guard + index.html 注入令牌 + Origin==Host 恒过） | 1.5.0 后 | Host 头白名单 LOCAL_HOSTNAMES（127.0.0.1/localhost/::1）前置到 _guard 与 serve_static，非环回 Host 一律 403 FORBIDDEN_HOST；新增 3 项 socket 级测试（rebinding POST/静态页无令牌/环回变体放行）；ARCHITECTURE 同源条目改述；CHANGELOG Unreleased 记录 |
| TD-08 | nlu.py vs LLM 双轨：同一句话两轨行为差异未固化断言 | 1.5.0 | V5-01 落地：10 组差分用例（等价面+白名单差异面钉住）+ 提示词契约绊线（PLANNER_PROMPT_VERSION 变更锚 + PlannerPromptContractTest 5 例）+ tools/compare_modes.py 差异报告 CLI（--json/--baseline 跨提示词版本对比）；回放 fixture 有意收窄为格式约定；发现并登记 TD-25 |
| TD-10 | 评测语料：长尾品类（纸杯/喷画/PVC/联单/手提袋/异形标签）用例薄 | 1.5.0 | V5-02 落地：_longtail_cases() 12 例（tag=长尾），评测 111→123 例全绿（390/390 键）；6 条话术口径勘误随 TD-09 评估（见 v15 计划 V5-02 执行记录） |
| RD-01 | `_update_item`/`_update_order` 约 220 行重复且已漂移 | v0.11.0 | 合并为 `_apply_patch`，三处分叉同步修正 |
| RD-02 | 静态服务暴露 `/.git` 与源码 | v0.11.0 | 静态白名单落地 |
| RD-03 | 本地 API 无鉴权 | v0.11.0 | 进程内令牌 + 同源校验 |
| RD-04 | SQLite 无 WAL/损坏会话永久 500 | v0.11.0 | WAL + busy_timeout + 隔离自愈 |
| RD-05 | secret_scan 仅识别 sk- 且跳过 data/ | v0.11.0 | 8 类模式 + data/ JSON 遮蔽扫描 |
| TD-01 | chat() 约 185 行对话主流程，内联中文意图匹配散落难测 | 1.3.0 | 规划器编排段提为 `_planner_chat_turn()`，8 类意图收敛为有序 `INTENT_DISPATCH` 表（否定感知复用 `PLANNER_INTENT_PATTERNS`，失败状态集合收编为 `PLANNER_RETRYABLE_FAILURE_STATUSES`/`PLANNER_FAILURE_STATUSES`）；112 例单测全绿 + 评测 111 例字段准确率 100%，新增 `tests/test_intent_dispatch.py` 回归 |
| TD-03 | `_quote_request()` 无 id 回退可能返回已取消请求 | 1.3.0 | 回退只认活跃状态 `QUOTE_ACTIVE_STATUSES`（awaiting_human_confirmation/confirmed），无活跃请求返回空 |
| TD-04 | itemOptions 直接索引持久化数据，坏状态 500 | 1.3.0 | `choose()` 方案条目形状校验 + `_normalize_state_item_options` 键守卫，坏数据自愈返回可读错误 |
| TD-05 | schemaVersion 只写不校验，高版本状态无警告 | 1.3.0 | 高版本加载记录 warning（`printops.order_model`）并注入提示级 `state_version_newer` 风险，不阻断 |
| TD-06 | 前端顶层绑定假设每个 ID 存在，缺失难排查 | 1.3.0 | bootstrap 绑定前一次性 ID 存在性断言：console.error 汇总 + 顶部横幅，只跳过缺失项 |
| TD-11 | 全量重绘导致输入焦点丢失与选中态闪跳 | 1.3.0 | 四个重绘函数补 `data-field-key` 焦点/选区契约（order:/item: 键，展开状态跨重绘保留），`ui_focus` 新增保持用例 |
| TD-19 | 前后端各自维护错误文案，无单一来源 | 1.3.0 | 持续错误卡与设置页文案收敛到 `PrintOpsJourney.ERROR_TEXTS`（状态桶 + 已映射 code，未知 code 回落），已有文案一字未变 |
| TD-20 | 尺寸别名归一测试哈希顺序敏感偶发（PYTHONHASHSEED flaky） | 1.3.0 | `_update_order` 冲突记录循环改 `sorted(changed)` / `sorted(set(before)\|set(after))` 确定性顺序；测试按 `field == 'paper'` 定位目标冲突而非 `conflicts[-1]`；7 个 seed 复跑全绿。镜像确认残留另立 TD-21 |
| TD-02 | 单产品依据拆分时不迁入 items 键，"首条多产品消息"项级依据缺失 | 1.3.0 后 | `_migrate_single_product_provenance` 按 mention 归属把顶层产品作用域依据原样迁入 `items.{itemId}.*`（只补空不降级、审计条目改名用重建副本）；同轮发现并修复重述品名拆出空壳重复项（一条消息 N 个产品恰 N 项）。MCP 低置信契约随迁移更新为项级路径 |
| TD-21 | 确认 paper 不升级镜像品类参数的依据与冲突 | 1.3.0 后 | `_set_field_meta` 按 `paper_spec_key` 传播确认到镜像 spec 字段（同 source/置信/evidence，不重写镜像值），镜像冲突随确认一次解决；显式改过镜像字段时不传播 |
| TD-20b | runtime_hardening 请求日志断言全量跑偶发双计数 | 1.3.0 后 | 共享 logger 混入其他测试后台流量；断言改按 `requestId={id}` 过滤后计数（该请求恰好登记一次），多 seed 全量验证 |
| TD-23 | 项级否定在焦点场景丢语义（短路吞否定 + 否定写孤儿对象） | 1.4.0 | 焦点短路让否定轮落到否定块；否定块清除前重读重建后的 items 列表；`_clear_process_options` 传项索引。两条回归用例钉住（修复前各失败，修复后 7/7），多 seed 全绿 |
| TD-07 | secret_scan 只识别带供应商前缀的凭据形态 | 1.4.0 | 增 Bearer 令牌与 Authorization 头取值两条模式；全仓扫描零新增命中；命中/放行两方向单测 |
| TD-12 | planner 重试仅 2 次、固定 80ms 退避 | 1.4.0 | 按错误类别退避：429 指数（0.5s×2^n）、其余可重试 5xx 线性、网络错误指数；全部仍走 deadline_sleep，MAX_ATTEMPTS 不变 |
| TD-13 | 原描述"Memory sessions 无限增长"已失效；实际无界增长点是 SQLite tool_call_log（建表注释明示 no retention pruning） | 1.4.0 | Memory 已是 OrderStore 门面且 state 内有界（消息 80/run 历史 20/候选 128）；tool_call_log 改为 TOOL_CALL_LOG_DISK_LIMIT=5000 有界环（插入与初始化时清理），audit_log/order_events 按 preserve_audit 红线一行不动 |
| TD-17 | 原描述"README GitHub 发布章节未对齐"已失效（README 已无该章节）；实际是发布操作手册缺失 | 1.4.0 | 新建 docs/RELEASING.md（文档收口、门禁、打标签、Release 资产与 SHA256SUMS、发布后事项），README 贡献节链接之 |
| TD-18 | ARCHITECTURE.md 未包含 v0.11.0 起的安全模型 | 1.4.0 | 新增"本地安全模型"节：令牌鉴权、同源约束、静态白名单、SSRF 校验与 TD-14 残余窗口、WAL 隔离自愈、Key 明文提醒、限流未做如实标注 |

## B.6 登记流程

1. 新债务：在本文件对应小节追加一行（编号 TD-续），描述必须含可定位的文件/行为；
2. 评估：是否纳入已有路径的任务卡（写归属与卡号），或标"待排期"；
3. 偿还：状态改"已完成 vX.Y.Z"，并迁移到 B.5 趋势表；
4. 每版本发布评审时过一遍本登记册，"待排期"超过三个版本仍未入路径的条目升级为活跃风险（进附录 A）。
