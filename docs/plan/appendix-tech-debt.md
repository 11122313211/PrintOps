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
| TD-08 | nlu.py vs LLM 双轨 | 同一句话两轨行为差异未固化断言（换提示词/模型的影响不可知） | 模型变更风险不可见 | 路径 5（S5-07） | 计划内 |
| TD-09 | product_knowledge.py | 行业风险规则缺口：折页压痕线、深色实底折痕爆裂、专色配置说明（ROADMAP v0.9.x 遗留） | 行业专业性不足 | 待排期（v0.14 知识专项；依赖可溯源来源） | 未开始 |
| TD-10 | 评测语料 | 长尾品类（纸杯/喷画/PVC/联单/手提袋/异形标签）用例薄 | 品类回归盲区 | 路径 5（S5-06） | 计划内 |

## B.3 运行与安全

| 编号 | 位置 | 描述 | 影响 | 归属 | 状态 |
| --- | --- | --- | --- | --- | --- |
| TD-14 | llm_adapter.py SSRF | 配置时校验与请求时之间存在 DNS 重绑定理论窗口 | 本地单机威胁模型内可接受 | 路径 9（S9-08） | 计划内 |
| TD-15 | llm_config.json | Key 明文为默认存储 | 本机文件泄露即凭据泄露 | 路径 9（S9-06/07） | 计划内 |
| TD-16 | server.py 静态白名单 | 目录白名单扩展后需保持"`/app/../server.py` 归一化 404"类测试常青 | 路径穿越回归 | 路径 2（S2-01） | 计划内 |
| TD-22 | app.js/journey.js 静态扫描 XSS 入口标记 | 2026-11-21 Mimosa deep 重扫（scan-2026-11-21T09-54-25.592Z，114 项，seal sha256:921c095d…）：与 2026-10-24 基线（113 项）同量级，TD-23 渲染路径修复与 journey.js ERROR_TEXTS 增项未引入新入口；既有 triage 结论继续有效——innerHTML 均走 markdown.js 白名单渲染器（`test_markdown_render.py` 契约锁定），其余为 textContent 或启发式误报。同日二次重扫（scan-2026-11-21T11-58-04.423Z，114 项，seal sha256:2e62afc9…）：覆盖参考版发布层级与 rate card 标定两笔提交（evaluate_agent 两档门槛为评测脚本非运行面、其余为文档与 JSON）， findings 数与无新 hypotheses 维持不变；两笔提交前钩子报 scanner_enobufs（结论迟到）以本次重扫结论补齐 | 重扫结论未变；renderer 契约变更或引擎运行面变更时再次重扫 | 待排期（考虑让扫描聚焦源码目录排除 reports/） | 已 triage（2026-11-21 两次重扫） |

## B.4 文档与流程

（当前无未偿条目。）

## B.5 已偿还债务（趋势记录）

| 编号 | 原描述 | 偿还版本 | 备注 |

## B.5 已偿还债务（趋势记录）

| 编号 | 原描述 | 偿还版本 | 备注 |
| --- | --- | --- | --- |
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
