# 供应商连接器开发契约

PrintOps 内置连接器只在本地生成草稿。盛大印刷、平台 A、平台 B 是未配置的字段模板，**没有 API 授权、生产能力、价格、库存或交期数据**。模板字段不是供应商官方 API 定义。通用模板和本地 mock 保证没有供应商数据时仍可完整演示订单整理、方案选择、参考估算、预检、草稿、确认、历史恢复。

## 可替换接口

`SupplierConnector` 定义于 `supplier_contract.py`，实现者须完整提供：

```python
metadata() -> dict
health() -> dict
capabilities() -> dict
create_quote_draft(order, context=None) -> dict
get_quote(request_id, context=None) -> dict
create_order_draft(order, context=None) -> dict
submit_order(order, context=None) -> dict
cancel(request_id, context=None) -> dict
```

草稿方法只生成本地可审阅的草稿，不意味着报价已发送。`submit_order` 和 `cancel` 是未来集成扩展点；没有作为 HTTP、MCP、模型工具暴露。没有支持的操作必须返回 `status=unsupported, reason=operation_not_supported, sent=false`。未知 provider 必须抛出 `UnknownProviderError`（工具边界转换成 `invalid_arguments`），不能静默回退。

连接器只能由部署代码注册可信实例：

```python
from supplier_connectors import REGISTRY
REGISTRY.register(reviewed_connector)
```

`register` 检查完整协议、版本、mm 单位、模式和操作集合。不支持从订单、对话或 API 请求动态下载 Python、动态 import 任意模块或加载任意 URL。注册新供应商后，`list_platforms()` 和现有工具读取同一 registry；原有 `ADAPTERS` / `PLATFORMS` 保留兼容视图。

## 模式和状态

`mode` 只有 `export_only`、`sandbox`、`live`。`connectionStatus` 单独表达 `local`、`unconfigured`、`connected` 等连接状态，不能把 `unconfigured` 当作运行模式。

盛大默认元数据：

```json
{
  "providerId": "shengda",
  "mode": "export_only",
  "connectionStatus": "unconfigured",
  "networkEnabled": false,
  "verified": false,
  "capabilities": ["quote_draft", "order_draft"],
  "source": {"kind": "local_export_template"},
  "updatedAt": null,
  "expiresAt": null
}
```

`updatedAt=null` 表示没有供应商数据更新时间，不用软件发布日期代替。`supplierProfileVersion` 仅是本地模板版本。未验证或过期的生产能力只能返回 `review`，不能判定支持或不支持。价格、材料、产能和交期应分别由经授权的数据源给出，并标识来源与有效期。`mock` 记录始终有 `source.kind=mock`、`mock=true`、`sent=false`。

## 标准订单和维度

`SupplierAdapter.map_order` 通过 `unit_normalizer` 和 `measurement_model` 转换尺寸。支持通用映射、品类覆盖和点路径，例如 `productSpecs.boxSize`、`dimensions.packageInnerSize`、`measurements.finishedSize`。

`size`、`dimensions`、`productSpecs` 中的物理尺寸和结构化 `measurements` 都统一为 mm。包内、包外、成品、展开、刀模分别保留，不从包装长宽高推断展开面积。`60×40×20cm` 导出为 `600×400×200 mm`；原文证据留在订单证据层，不传作供应商物理量。运输层拒绝未标明单位或不可解析的尺寸；不能偷偷假定 mm。

同一次更新同时提供不一致的 `dimensions.finishedSize` 和旧 `size` 时，以显式 `dimensions` 为准，并同步 `size`；包装的 `boxSize`、`bagSize`、`boxSizeInner`、`boxSizeOuter` 也同步到对应的 canonical 字段。确认一个尺寸只确认它及其兼容别名，不确认其他尺寸。原文证据和存储迁移备份保持可追溯。

schema 3 的新输入 A/B 系列采用 ISO 216，因此 B4 为 `250×353 mm`、B5 为 `176×250 mm`。旧 schema 1/2 曾将裸 B4/B5 解释为 JIS；迁移会固定为 `257×364 mm`、`182×257 mm`，并保存 `measurementMigrations` 中的原始标记，避免历史订单在升级时被改变实际尺寸。历史记录已经写明 mm/cm 数值的，按其明确数值换算。

迁移前的交接单、询价请求、人工确认和工艺选择保存在 `legacyDeliveryEvidence.original`，不会改写历史原件。当前交接单和已选方案清空，询价保留请求 ID 与审计但标记为 `stale`，确认与工具回执也失效，恢复到信息收集阶段；当前交接草稿须重新生成，人工确认也须重新完成。SQLite 同时保留旧 `sessions` 原始 JSON 备份，重复恢复不会重复迁移。

## 可注入传输与故障约束

`GuardedTransportConnector` 不包含 HTTP 实现，只接收实现以下方法的可信对象：

```python
execute(operation, payload, *, timeout, idempotency_key, cancel_event) -> dict
```

默认 `TransportPolicy` 为单次 5 秒、总 deadline 15 秒、最多 2 次尝试、最多 4 个在途调用。读取报价可对超时或明确的 `retryable_error` 做有界重试。提交和取消不自动重试；超时或异常返回 `unknown` 和 `requiresReconciliation=true`，不能声称提交失败，也不能自动换一个幂等键再发。

传输适配器必须尊重 timeout 和取消信号。宿主用有界 daemon worker 限制等待时间和并发，即使错误传输忽略 timeout 也不会无限堆积线程；Python 无法强制停止已经执行的外部请求，所以超时后仍需供应商查询与人工核对。`cancel_wait(key)` 仅中止等待；远端撤单须调用 `cancel(request_id)`，两者含义不同。

`RequestContext` 带 `revision`、`idempotency_key`、`confirmation`。幂等键按 provider 和 operation 隔离。同键不同订单指纹、revision 或撤销目标被阻断。重复请求返回已有结果；进程崩溃后未完成的 claim 返回 unknown，绝不盲目补发。接口不能凭一个 `confirmed=true` 接受正式提交。

## 持久化和审计

`RequestJournal` 可替换为企业存储，要求 `claim` 原子占位，`finish` 写结果，`get` 查询，`append_audit` 留痕。

- `MemoryRequestJournal` 用于离线和沙箱测试，不能开启 live。
- `SQLiteRequestJournal(path)` 用唯一键和事务保存请求、结果及审计，重启后仍保留幂等与未知状态。
- 审计事件包含 provider、操作、订单指纹、revision、幂等键、时间、结果状态和尝试次数。默认不写原始对话、凭据、客户地址和完整订单。
- 写操作在发送前必须完成 journal claim 与审计；失败则阻断发送。发送后落盘失败返回 unknown，要求核对。

## Live 接入条件

没有实际授权也可以完成上述协议、mock、契约测试和 UI。当前仓库不会发起任何真实供应商网络请求。

未来部署 live 需同时具备：可信传输代码、明确启用 `live_enabled`、`networkEnabled=true`、已连接状态、配置凭据、已验证健康与字段契约，以及持久化 journal。提交和取消还需 `HumanConfirmation`，绑定 provider、订单指纹、revision、操作、撤销目标和有效时间。

`HumanConfirmation` 是供**经过身份验证的服务器人工确认处理器**构造的 Python 值，不是签名令牌。未来公开 live API 时必须鉴别人类操作者、按订单授权，并从服务器保存的确认记录恢复该值；严禁将客户端或模型给出的字典/布尔值直接转换为确认。调用前必须从权威订单存储重载当前 revision 与订单内容，在存储事务或提交占位机制内校验确认仍有效；连接器只比较传入值，不自行读取订单数据库，不能用过期的客户端快照证明它仍是当前版本。

接入方还应提供产品编码、单位、鉴权轮换、字段枚举、上传协议、回调验签、报价有效期、限流、取消规则及授权范围。需要新操作时提升契约版本，避免把正式询价发送藏在本地 `create_quote_draft` 中。

## 验收

运行：

```sh
python3 -m unittest discover -s tests -p 'test_supplier_connectors.py'
```

测试覆盖默认禁止网络、未知供应商、注册与替换、mm 映射、未验证/过期数据、mock 状态与取消、超时和重试上限、取消等待、提交未知状态、持久化幂等与重启、不完整 claim、人工确认失效以及审计失败阻断。真实供应商沙箱与正式 API 验收需等有授权和对方接口文档后进行，当前测试不冒充真实联调。
