# 附录B：技术债务登记册（appendix-tech-debt）

> 登记所有已知但未在本计划主路径中立即解决的技术债务。每条含：编号、位置、描述、
> 影响、归属路径（或"待排期"）、状态。规则：新发现的债务先登记再决定是否入路径；
> 已偿还的条目保留并标记日期，作为债务趋势记录。

## B.1 代码结构与正确性

| 编号 | 位置 | 描述 | 影响 | 归属 | 状态 |
| --- | --- | --- | --- | --- | --- |
| TD-02 | agent.py 多产品拆分路径 | 单产品阶段积累的 fieldMeta（provenance）在拆分为 items 时不迁移到 `items.{itemId}.*` 键，只在顶层键留存 | 项级置信度展示与路径 10 的徽标体系在"首条多产品消息"场景下缺数据 | 路径 7（S7-04）实现时一并评估迁移方案 | 未开始 |
| TD-07 | tools/secret_scan.py | 模式仍以"形态匹配"为主，无法识别无前缀的自定义凭据（如 `Bearer xxx` 变量拼接） | 理论漏报 | 待排期（增加 Bearer/Authorization 头形态） | 未开始 |
| TD-21 | agent.py `_set_field_meta` / paper 镜像 | 确认 `paper` 只解决 paper 冲突：镜像参数（如名片材质 `productSpecs.cardStock`）值同步但 fieldMeta/冲突不随确认升级，低置信替换后确认会留下一条未解决的镜像冲突 | 冲突面板残留一条"待确认"，需要用户再确认一次镜像字段 | 待排期（确认 paper 时对 linked spec 字段同步 `_set_field_meta`；1.4 候选） | 未开始 |

## B.2 行为一致性

| 编号 | 位置 | 描述 | 影响 | 归属 | 状态 |
| --- | --- | --- | --- | --- | --- |
| TD-08 | nlu.py vs LLM 双轨 | 同一句话两轨行为差异未固化断言（换提示词/模型的影响不可知） | 模型变更风险不可见 | 路径 5（S5-07） | 计划内 |
| TD-09 | product_knowledge.py | 行业风险规则缺口：折页压痕线、深色实底折痕爆裂、专色配置说明（ROADMAP v0.9.x 遗留） | 行业专业性不足 | 待排期（v0.14 知识专项；依赖可溯源来源） | 未开始 |
| TD-10 | 评测语料 | 长尾品类（纸杯/喷画/PVC/联单/手提袋/异形标签）用例薄 | 品类回归盲区 | 路径 5（S5-06） | 计划内 |

## B.3 运行与安全

| 编号 | 位置 | 描述 | 影响 | 归属 | 状态 |
| --- | --- | --- | --- | --- | --- |
| TD-12 | llm_adapter.py | 同步阻塞 20s×2、固定退避、last_error 共享 | 等待体验与并发正确性 | 路径 1/4 | 计划内 |
| TD-13 | agent.py Memory | sessions 无限增长；`_db` 每操作新连接（无长连接池，本地可接受） | 存储膨胀 | 路径 4（S4-06） | 计划内 |
| TD-14 | llm_adapter.py SSRF | 配置时校验与请求时之间存在 DNS 重绑定理论窗口 | 本地单机威胁模型内可接受 | 路径 9（S9-08） | 计划内 |
| TD-15 | llm_config.json | Key 明文为默认存储 | 本机文件泄露即凭据泄露 | 路径 9（S9-06/07） | 计划内 |
| TD-16 | server.py 静态白名单 | 目录白名单扩展后需保持"`/app/../server.py` 归一化 404"类测试常青 | 路径穿越回归 | 路径 2（S2-01） | 计划内 |
| TD-22 | app.js/journey.js 静态扫描 XSS 入口标记 | 2026-10-24 Mimosa deep 扫描（scan-2026-10-24T07-02-43，113 项）triage：83 项为 `reports/journey-webkit/**` Playwright trace viewer 第三方产物噪音（gitignored 测试工具，非源码）；30 项项目源码标记逐一核查——addMessage/renderMessageBody/journey.js:173 的 innerHTML 均走 markdown.js 白名单渲染器（先转义、仅白名单标签、链接限 http(s)/mailto，`test_markdown_render.py` 契约锁定），renderFieldList/renderToolStats 等为 textContent，querySelector→mongo-sort-injection 为启发式误报（仓库无 MongoDB），SSRF 指向 llm_adapter.py:1478 的用户配置模型端点请求（有 host 校验，残余窗口即 TD-14）。扫描 run status 为 inconclusive（动态派发致调用图不完整），不构成安全结论 | 扫描噪音长期存在，renderer 契约变更时需人工复查污点链 | 待排期（renderer 或渲染路径变更时重扫；考虑让扫描聚焦源码目录排除 reports/） | 已 triage |

## B.4 文档与流程

| 编号 | 位置 | 描述 | 影响 | 归属 | 状态 |
| --- | --- | --- | --- | --- | --- |
| TD-17 | README"GitHub 发布"章节 | 仍是手工 git 命令清单，未与 RELEASING.md 流程对齐 | 发布操作易出错 | 路径 8（S8-10 后统一改写指向） | 未开始 |
| TD-18 | ARCHITECTURE.md | 未包含 v0.11.0 的令牌/白名单/SSRF 安全模型更新 | 架构文档滞后 | 随 v0.11.0 提交一并补 | 未开始 |

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

## B.6 登记流程

1. 新债务：在本文件对应小节追加一行（编号 TD-续），描述必须含可定位的文件/行为；
2. 评估：是否纳入已有路径的任务卡（写归属与卡号），或标"待排期"；
3. 偿还：状态改"已完成 vX.Y.Z"，并迁移到 B.5 趋势表；
4. 每版本发布评审时过一遍本登记册，"待排期"超过三个版本仍未入路径的条目升级为活跃风险（进附录 A）。
