# 本地运行数据

`data/` 是 PrintOps 的本机运行目录，不是 GitHub 数据目录。

允许提交的只有本说明和 `.gitkeep`。下面这些内容由程序或用户在本机生成，均已在
仓库根目录的 `.gitignore` 中排除：

- `agent.sqlite3*`：会话、订单草稿和 SQLite WAL 文件
- `llm_config.json`：模型地址、模型名和本机 API Key
- `corrupted/`：损坏会话的隔离备份
- `uploads/`、`imports/`：用户原稿或导入文件
- `exports/`、`backups/`：交接单、报告和本地备份

不要使用 `git add -f data/*`，也不要把真实原稿、客户信息或 API Key 复制到仓库。
需要分享配置时，请使用仓库里的示例文件（例如 `.dsh/profile.example.json`），并
先移除地址、令牌和客户数据。

提交前可在仓库根目录运行：

```bash
python3 tools/repo_guard.py
python3 tools/secret_scan.py
```

`repo_guard.py` 只检查 Git 视角下的文件，不会读取或上传这些本地数据。
