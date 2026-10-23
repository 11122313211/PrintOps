# Contributing / 参与开发

PrintOps 刻意保持轻量。动手前请先读“核心约束”，它们比代码风格更重要。

## 环境

- 运行产品：只需 **Python 3.9+**（标准库，无 pip 安装）。`python3 server.py` 后打开 http://localhost:4174/。
- 浏览器测试（可选）：Node.js 18+、`npm ci` 与 `npm run test:browser:install`（Playwright 仅测试用，不是运行时依赖）。

## 核心约束

1. **运行时保持标准库**：Agent、server、存储、计价不得引入第三方依赖；确有需要先开 issue 讨论。
2. **人工确认边界不可跳过**：生成、确认、询价、交接都必须经过本地校验；模型输出永远只是候选。
3. **不伪造真实连接**：当前没有真实 HTTP transport 或供应商授权；新供应商请通过 registry 扩展，
   内置模板保持 `verified=false`、`networkEnabled=false`。
4. **示例价不是市场价**：rate card 变更必须版本化，并在文档与输出中标注 illustrative。
5. **隐私红线**：不提交真实订单、原稿、密钥或任何 `data/` 内容；提交前跑 `python3 tools/repo_guard.py`。

## 开发循环

```bash
python3 tools/dev_check.py           # 编译 + 463 项单测 + 评测 + secret_scan + repo_guard
python3 tools/dev_check.py --quick   # 只跑编译与守护（快速回路）
PYTHONPATH=. python3 tests/evaluate_agent.py --release-gate --json   # 发布门槛（真实语料不足时必须失败）
```

本项目**有意不使用 GitHub Actions**（决策与理由见 README“开发与验收”与 docs/RELEASE_CHECKLIST.md），
发布前在本机执行上述检查。跨平台代码请同时考虑 Windows（GBK 控制台、SQLite 文件锁、无 SIGTERM）差异，
参考 `stdio_utf8.py` 与 `tests/test_runtime_hardening.py` 的平台处理。

## 测试与提交

- 单元/契约测试放 `tests/test_*.py`（unittest；浏览器测试用 Playwright `tests/*.spec.mjs`）。
- 行为变化写入 `CHANGELOG.md` 的 `Unreleased` 段。
- 从 `main` 拉分支；提交信息用 `feat|fix|docs|chore(scope): 摘要`；PR 里写明动机、影响的契约边界
  （订单/存储/计价/MCP/供应商）与你实际跑过的命令及结果。
