# 订单业务契约 2.0

PrintOps 的订单由后端存储，HTTP、MCP、前端工作台读取同一订单和 revision。`canonicalOrder` 是该快照的业务视图，`order` 是现有接口的兼容视图；不维护两份可独立修改的订单。结构定义见 [`schemas/order-v2.schema.json`](../schemas/order-v2.schema.json)（JSON Schema Draft 2020-12）。

## 版本与默认规则

| 契约 | 版本/默认值 | 含义 |
| --- | --- | --- |
| 业务订单 | `schemaVersion="2.0"` | 本文的 canonicalOrder |
| Agent 状态迁移 | `schemaVersion=3` | 存储的工作流/历史状态，独立于业务版本 |
| 存储接口 | `1.0`，默认 SQLite | 可替换为 memory 或显式注册的实现 |
| 物理尺寸 | `unitSystem="mm"` | 数值轴和显示值统一毫米 |
| 价格 | `local_reference` / CNY | 当前为示例成本参数参考估算 |
| 内置供应商 | `export_only` | 只生成本地字段映射和草稿；mock 单独标记沙箱 |

当前一条 session 对应一张订单，支持多产品 items。身份、revision、工作流状态位于响应外层；嵌套产品用稳定的 `itemId` 关联来源、文件、方案和询价，不能用显示下标作为持久化身份。

## 字段状态与证据

空值是 `{"value":null,"status":"missing"}`。待确认值是 `{"value":null,"candidateValue":"1000 张","status":"pending_confirmation"}`。明确识别和用户确认分别使用 `inferred`、`confirmed`。置信度低于 0.75 的生产字段需要用户确认，不能用于生成交接单或询价草稿，也不能直接产生完整价格区间。

`provenance` 记录来源、置信度、时间、runId 和 `evidenceRef` 引用；原文只在响应外层 `fieldMeta` / `fieldCandidates` 与历史证据中保存，不嵌入 canonicalOrder。`fieldCandidates` 保留识别结果、原文区间、作用范围、否定标记与处理状态。原始输入允许包含 cm、英寸及用户原话；这些是证据，不能当作已经标准化的生产参数。用户确认或替换候选值通过现有受约束 patch 写入，确认与修改同样进入事件和审计。

例如“铜版纸还是白卡纸”会保留待确认候选，而不会直接批准一种纸张。“不要 300g 铜版纸”按统一克重匹配已有字段；“第 2 项改成可移胶”按第 2 项品类解析。多产品字段按语句边界隔离，只有“都/统一/全部/均”后明确表达的属性才共享，不能因为“都下周交货”而顺带共享纸张。

## 物理尺寸

结构化尺寸形如 `{"width":600,"height":400,"depth":200,"unit":"mm","semantic":"package_inner"}`。二维对象没有 depth。支持成品 finished、展开 expanded、刀模 die_cut、包装语义未定 package_unspecified、包装内尺寸 package_inner、包装外尺寸 package_outer。

自然语言支持 cm/in/mm 的逐轴换算；没有单位的数值或多个备选尺寸不视为有效物理量。手工尺寸输入框按 mm 输入，输入 cm/英寸时提示改用对话换算。canonicalOrder 的物理尺寸统一放在 measurements，旧 boxSize 等别名仅保留在兼容 order 视图，避免产生第二个生产尺寸。包装内外尺寸不能替代展开面积；内尺寸也不能自动当作外尺寸。旧 `size`、嵌套 `dimensions`、兼容尺寸别名与 `measurements` 必须保持一致；同时提供冲突值时，以显式 `dimensions` 为准同步兼容字段。

新输入的 A/B 标准尺寸采用 ISO 216，B4=250×353 mm、B5=176×250 mm。使用 JIS B 系列的客户应直接提供实际毫米值，不能只报名称。旧状态迁移须保持旧版本的 B4/B5 尺寸含义，不把历史 JIS 数值重新解释成 ISO 数值；原始状态另有备份。迁移前已准备的交接、询价、方案和确认保存到 `legacyDeliveryEvidence`；当前产物失效并要求重选/重新确认，不能直接沿用旧单位交接单。

纸张克重显示为 g/m²，工艺使用统一词汇如“局部 UV”。相对交期保留“下周内”“两工作日”等原始日历语义；当前不包含工厂节假日历和承诺日期计算，不能把所有自然日自动转换为工作日。

## 订单状态与兼容边界

状态词汇固定为 draft、collecting、ready_for_review、confirmed、handoff_ready、quote_draft、quote_received、cancelled、archived。当前默认离线流程主要产生草稿、收集、待审核、确认、询价草稿及归档状态；保留未来状态名不表示已经获得真实供应商报价或发出订单。

`POST /api/session` 对已有订单只读恢复，不增加 revision、不运行工具。新业务写入使用 expectedRevision CAS；冲突返回 409，客户端必须刷新后重试。归档后只读，历史面板可显示已归档记录并导出完整 JSON 备份。

当前没有浏览器离线写入队列：供应商或模型不可用时可以使用本地规则工作流；浏览器连不上本机服务时保留待发送文本并提示重试。跨设备使用需要部署可达的服务和存储，接口可替换不等于已经完成云部署。

## 验证与发布

业务行为回归见 `tests/test_order_contract_v2.py` 及尺寸/迁移回归；HTTP/MCP 保留兼容测试；桌面 Chromium 与 390 px 布局见 [`BROWSER_SMOKE.md`](BROWSER_SMOKE.md)。正式发布必须运行 `python3 tests/evaluate_agent.py --release-gate`；真实语料至少 20 条、字段准确率至少 95%。用户当前没有真实订单语料，保留发布阻断，不以合成用例替代。
