# 通用供应商接入包：先运行本地 mock

示例 `examples/supplier_mock.py` 复用现有 `SupplierConnector`、`ConnectorRegistry`、`MockConnector` 和 `SupplierAdapter`，无需修改订单、Agent、HTTP 或 MCP 核心。示例没有网络调用、密钥、真实印厂数据或报价。

```sh
python3 examples/supplier_mock.py
python3 -m unittest discover -s tests -p 'test_supplier_example.py'
python3 -m unittest discover -s tests -p 'test_supplier_connectors.py'
```

第一条命令输出 JSON，包含 metadata、health、capabilities、询价草稿、读回、订单草稿、重复取消和禁止正式提交的结果。所有结果清楚标记模拟/未发送。教程的独立 registry 不修改内置盛大配置。

## 最小接入步骤

1. 新建实现 `SupplierConnector` 的可信 Python 类，或像示例一样继承无网络 mock；不要根据用户/模型输入动态导入模块或 URL。
2. 分配稳定 `providerId`，元数据声明契约版本、`mode`、`unitSystem=mm`、连接状态、来源和支持操作。教程沿用 `verified=false`、`updatedAt/expiresAt=null`、空生产能力；代码可运行不代表印厂能力已验证。
3. 使用 `SupplierAdapter.field_map` 映射通用与品类字段。示例将 `measurements.finishedSize` 映射为 `finished_size`，包装的刀模尺寸映射为 `dieline_size`；纸材映射为 `stock`。归一化发生在映射前，缺失单位会阻断。产品编码、枚举、数量单位和日期语义需按目标协议独立校验，不能只改字段名就声称正式兼容。
4. 启动时显式注册实例。重复 ID 默认报错，未知 ID 报错，不回退到另一供应商。要接入当前服务，可在可信部署入口启动服务之前运行下列代码；示例本身不改核心文件。

```python
from examples.supplier_mock import register_example
from supplier_connectors import REGISTRY

register_example(REGISTRY)
# 随后启动现有服务入口；list_platforms/HTTP/MCP 共享此 registry。
```

5. 执行契约测试。新示例覆盖完整协议、显式注册、字段映射、mm、无网络、草稿读回、返回值隔离、取消幂等和拒绝提交。现有连接器测试覆盖 HTTP/MCP 注册边界、持久幂等、deadline、未知提交结果和人工确认。

## 生产能力、错误与提交边界

| 状态/操作 | 接入要求 |
| --- | --- |
| `export_only` / 未配置 | 本地生成草稿，`sent=false`；可以整理交接，不声称支持某种材料、交期或实时价格 |
| `sandbox` / mock | 明确合成来源；示例读回的是本地草稿，不是供应商返回报价 |
| 能力数据过期或未验证 | 返回待核实，不依据未知资料做支持/不支持结论；授权后再补来源、更新时间及有效期 |
| `blocked` / `unsupported` | 呈现原因；不偷偷换目标供应商或执行不同操作 |
| 写操作超时或回执丢失 | 返回 `unknown`、`requiresReconciliation=true`；保留幂等键并核对，不用新键自动重发 |
| `cancel_wait` | 只中止本地等待，不能表示远端已撤单 |
| 正式 `submit_order` / `cancel` | 需要已授权传输、显式启用、持久 journal 和可信人工确认；示例永不正式提交 |

需要真实连接时使用已有 `GuardedTransportConnector` 的 transport seam，而不是给 mock 偷加 HTTP。宿主须先重载权威订单，校验当前 revision、订单指纹、操作者身份和确认有效期，再发起操作；Python `HumanConfirmation` 不是可直接信任的客户端令牌。官方鉴权、产品编码、上传、回调验签、限流和对账仍待具体授权协议。

```text
业务订单 → mm 校验 → 字段映射 → registry 中显式选择的 connector
                              ├─ mock / export：本地草稿，未发送
                              └─ 将来 live：授权 + 当前版本确认 + 持久幂等 → 可信 transport
                                     超时 → unknown → 查询/人工核对
```

盛大继续保持 `export_only`、`unconfigured`。本接入包不假定盛大是唯一未来接入方，也未完成任何真实供应商联调。
