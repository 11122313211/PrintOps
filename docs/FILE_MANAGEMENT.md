# 文件管理与发布边界

PrintOps 采用“源码/规则可提交，运行数据留本机”的目录策略。GitHub Desktop 只应
提交下面的共享文件；本地运行产生的文件即使存在于工作区，也不应出现在提交列表中。

## 目录约定

| 范围 | 例子 | Git 策略 |
| --- | --- | --- |
| 共享源码与规则 | `*.py`、`app.js`、`styles.css`、`.dsh/skills/` | 提交 |
| 共享文档与测试 | `README*`、`docs/`、`tests/` | 提交；测试样例必须脱敏 |
| 运行状态 | `data/agent.sqlite3*`、`data/corrupted/` | 只留本机 |
| 模型配置 | `data/llm_config.json`、`.env*`（`.env.example` 除外） | 只留本机 |
| 用户文件 | `data/uploads/`、`data/imports/`、`inbox/` | 只留本机 |
| 生成结果 | `data/exports/`、`data/backups/`、`outbox/`、`reports/` | 只留本机 |
| 凭据与证书 | `*.secret`、`*.pem`、`*.key`、`*.p12`、`*.pfx` | 只留本机 |

根目录 `.gitignore` 是唯一生效的忽略清单。`data/README.md` 和 `data/.gitkeep` 是
目录说明文件，其他 `data/` 内容默认都不会被 Git 跟踪。

## GitHub Desktop 提交流程

1. 在 **Repository → Repository settings** 确认当前仓库是 `PrintOps`，分支为 `main`。
2. 提交前运行 `python3 tools/repo_guard.py`，确认没有 `ERROR`。
3. 再运行 `python3 tools/secret_scan.py`；发现命中时先移出凭据或客户数据。
4. 在 Changes 列表中只勾选源码、规则、测试和文档。`data/` 下的数据库、配置和原稿不应出现；若出现，先取消勾选并检查 `.gitignore`。
5. 提交后再点击 **Push origin**。不要使用 “Force add” 把被忽略的本地文件加入提交。

## 本地清理

清理前先关闭 PrintOps 服务，并确认文件确实是运行态数据。下面命令只操作已约定的
本地目录，不会删除源码：

```bash
find data/corrupted data/uploads data/imports data/exports data/backups \
  -type f -mtime +30 -print
```

确认列表后再按需删除；SQLite 主库和 `llm_config.json` 应由操作者自行备份或清理。
仓库检查脚本只报告问题，不会自动删除文件。

## 发布前检查

```bash
python3 tools/repo_guard.py
python3 tools/secret_scan.py
python3 -m unittest discover -s tests -p 'test_*.py'
git diff --check
```

如果 GitHub Desktop 显示了被忽略文件，通常是文件曾经被强制跟踪过；先运行
`git ls-files data` 检查，再移除错误的跟踪记录（不要删除本机文件）：

```bash
git rm --cached -- data/agent.sqlite3 data/llm_config.json
```

仅对确认误跟踪的路径执行上面的命令，并在提交前再次运行 `repo_guard.py`。
