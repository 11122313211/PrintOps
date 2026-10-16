# 订单存储：一致性备份、保留策略与隔离恢复

本阶段在 `OrderStore` 之外增加运维边界：`storage_policy.StoragePolicy` 定义策略，`storage_backup` 实现 SQLite 备份与校验，`tools/order_backup.py` 提供可执行入口。业务保存、CAS、归档和审计接口不变。其他数据库可继续实现 `OrderStore`，并使用各自的一致性备份机制；本 CLI 只处理 SQLite。

```text
正在使用的 SQLite（含 WAL）
  → SQLite backup API → 新备份目录：数据库 + manifest
  → 校验全部表、数据库完整性和版本
  → 新隔离目录恢复 → 临时副本演练迁移、消息/审计、CAS
  → 输出恢复报告；线上库仍不变
失败/版本不兼容 → 明确退出，不切换线上库
```

## 配置与默认行为

示例配置为 `docs/examples/storage-policy.example.json`，未知字段、非正整数和 `preserve_audit=false` 均拒绝。

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `backup_directory` | `data/backups` | CLI 未显式指定输出时的备份目录 |
| `backup_interval_hours` | 24 | 运维调度建议；当前没有后台调度器，不代表已经每 24 小时执行 |
| `backup_keep_count` | null | 保留数量阈值；null 不按数量提出过期候选 |
| `backup_max_age_days` | null | 备份年龄阈值；null 不按年龄提出过期候选 |
| `archive_after_days` | null | 仅对已取消订单提出归档候选；不依据年龄处理活跃或已确认订单 |
| `preserve_audit` | true | 追溯记录必须保留；当前不支持审计删除 |

`retention_plan(policy, backups, orders)` 只输出计划。两个备份阈值同时设置时，必须同时满足才列入候选；最新一份已验证备份始终保留，无法验证的目录也保留。订单候选携带 `expectedRevision`，未来执行归档时仍须经过 `OrderStore.delete_order` 的 CAS。当前没有自动删除备份、清理业务表、定时归档或清理审计的功能。

用户可以在订单已归档后显式永久删除该记录：`POST /api/orders/{id}/purge` 必须同时提供当前 `expectedRevision` 和 `confirm: true`。该操作只接受 `archived` 状态，并在一个事务中删除订单快照、消息、事件、报价、审计行及兼容镜像；它不属于自动保留策略，无法恢复，调用前应先导出备份。`preserve_audit=true` 约束自动保留策略，不阻止用户主动清除一条已归档订单的全部数据。

## 可运行命令

从仓库根目录执行，以下输出目录须不存在：

```sh
python3 tools/order_backup.py --policy docs/examples/storage-policy.example.json create data/agent.sqlite3 --output data/backups/manual-001
python3 tools/order_backup.py verify data/backups/manual-001
python3 tools/order_backup.py restore data/backups/manual-001 --destination data/restore-drills/manual-001
python3 tools/order_backup.py --policy docs/examples/storage-policy.example.json retention-plan
```

退出码 0 表示该命令成功，2 表示配置/路径/完整性/版本/恢复失败。`restore` 不能覆盖已有目录，不能指向正在使用的数据库。恢复成功仅证明隔离副本通过检查，不自动切换服务。实际切换须停止写入、核对路径与附件、另行安排切换和回滚。

## 备份内容与保证

- 使用 `sqlite3.Connection.backup` 获取一致性快照，包含已提交 WAL 事务，不包含未提交事务；不能直接复制正在使用的 `.sqlite3` 文件替代。
- 备份生成独立数据库文件，manifest 包含备份 ID、时间、SQLite 版本、存储契约版本、迁移版本、订单 schema 版本、数据库 SHA-256、全部表的行数和内容哈希、数据库结构哈希。插件扩展表和 `sqlite_sequence` 也纳入。
- 当前支持存储迁移版本 1 与已知订单 schema；只有旧 `sessions` 表的库先通过现有 `SQLiteOrderStore` 迁移，再备份。未知未来版本拒绝恢复演练，不自行降级。
- SQLite `integrity_check`、`foreign_key_check`、字节摘要及全部表清单一致后，才进入恢复演练。演练临时副本检查打开存储不改变现有内容，再写入合成订单，验证消息、事件、审计和过期 CAS 拒绝。合成记录不进入交付的恢复库。
- 默认复制超时为 30 秒，可通过 Python API 指定 `(0, 300]` 秒；超时中止本次备份并清理临时目录。部分输出不会有有效校验结果，不视为成功备份。

备份覆盖数据库中的完整消息与审计，因此按本地数据文件管理；新目录权限为 0700，数据库与 manifest 为 0600。manifest 证明内容一致性，不提供签名认证或加密。原始上传文件字节、数据库之外的附件、模型密钥和环境配置不在此备份范围；订单中的附件引用仍保留。完整灾备还需要单独保存这些外部资源并核对引用。

## 本轮验收

```sh
python3 -m unittest discover -s tests -p 'test_storage_backup.py'
```

9 项测试覆盖全表与扩展表、在线 WAL、并发保存的快照/事件一致性、篡改拒绝、禁止覆盖、CLI 往返、旧库迁移原件、CAS 和保留策略。使用临时合成数据库；未演练真实大容量库、磁盘故障、跨 SQLite 大版本或企业数据库恢复。哈希清单按表扫描，需根据实际数据规模安排备份窗口和存储容量；不能把本轮小数据测试当作容量保证。
