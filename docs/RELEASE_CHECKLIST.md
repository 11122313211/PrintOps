# 1.1 发布门槛与验收清单

本文回答"什么时候可以正式发布 1.1"。当前 `1.1.0` 是**受控本地集成代码基线候选版**，还不能宣称通用稳定发布；
本清单继承 v1.0.0 的真实语料与真人走查门槛，并新增 MCP/dsh、本地 host、模型工具循环和上下文传输门槛。
v1.0.0 的历史版本语义保留在 CHANGELOG，不因本清单升级而改写。

## v1.0.0 历史门槛（继承）

| # | 门槛 | 状态 |
| --- | --- | --- |
| 1 | 静态路由白名单 + 本地访问令牌 | ✅ 已完成（`server.py`，401/403 守卫，跨来源拒绝） |
| 2 | 真实 HTTP Handler 测试（socket 级） | ✅ 已完成（`tests/test_http_handler.py`，13 例） |
| 3 | Key 明文风险提示 + 敏感扫描扩充并纳入 `data/` JSON | ✅ 已完成（设置面板警告 + `tools/secret_scan.py`） |
| 4 | 真实脱敏订单评测（20-50 例，字段准确率 ≥95%） | ⏳ 机制已就绪，**等待真实语料**（见下） |
| 5 | 死代码清理 + 合并 `_update_item`/`_update_order` | ✅ 已完成（统一到 `_apply_patch`） |
| 6 | SQLite WAL + busy_timeout + 损坏会话恢复 | ✅ 已完成（`tests/test_persistence.py`） |
| 7 | 真人使用走查（10 分钟北极星场景） | ⏳ **需要真人执行**（脚本见下） |

## v1.1.0 新增集成门槛

| # | 门槛 | 状态 |
| --- | --- | --- |
| 8 | 标准库 MCP stdio server、session binding 与 L0/L1 capability 边界 | ✅ 已完成（`mcp_server.py`、`tests/test_mcp_server.py`） |
| 9 | dsh 元数据/profile、5 个印刷 skill 与受信 launcher 校验 | ✅ 已完成（`.dsh/`、`tests/test_dsh_metadata.py`、`tests/test_dsh_runtime.py`） |
| 10 | 无 npm/pnpm 的 Python local host 与 MCP transcript smoke | ✅ 已完成（`tools/printops_local_host.py`、`tools/dsh_mcp_smoke.py`、`tests/test_local_host.py`） |
| 11 | native `tool_calls`/JSON fallback、有界 3 轮工具循环与上下文预算 | ✅ 已完成离线契约验证（`llm_adapter.py`、`tests/test_agent.py`） |
| 12 | 真实 dsh headless、真实模型 endpoint 和 dsh on/off parity | ⏳ **需要内网环境与真实运行时验收** |
| 13 | 浏览器级工具时间线、错误恢复与窄屏冒烟 | ⏳ **需要浏览器验收** |
| 14 | 真实供应商 live 接入 | ⏸ **不在 1.1.0 范围，顺延 v1.2+** |

## 1.1 集成门槛说明

本地可重复执行以下无第三方依赖检查；它们证明 MCP/工具边界，不冒充真实 dsh 或真实模型端到端：

```bash
python3 tools/dsh_mcp_smoke.py
python3 tools/printops_local_host.py --session-id release-smoke --smoke
```

原生 tool-call 与上下文预算由离线测试覆盖。真实 dsh headless、真实内网模型和浏览器检查必须在相应环境单独记录 provider 响应格式、工具请求/结果摘要和失败原因，不得记录 API Key 或完整客户原稿。

## 门槛 4：真实脱敏订单语料

合成语料由实现同一套规则的人编写，不能证明真实用户话术下的表现。

1. 收集 20-50 例真实订单需求（客服记录、聊天记录、邮件均可），逐条**脱敏**：
   删除客户名称、电话、地址、邮箱、订单号等个人信息，只保留印刷需求描述。
2. 用标注辅助工具生成用例草稿（只打印到控制台，不写语料文件）：
   `python3 tools/annotate_case.py --name "真实-名片001" --turn "客户原话（脱敏后）"`，
   多轮对话重复 `--turn`。输出的 expected 是 Agent 提取的**建议值**（系统默认值已剔除），
   必须逐项与原话核对后保留——没提过的字段删掉，提过的值不符合的改掉。
3. 核对后把 JSON 粘贴进 `tests/eval_cases_real.json` 的 `cases` 数组，脱敏要求同上。
   （不使用工具时，按文件内 `_instructions` 的结构手写也可。）
4. 运行 `PYTHONPATH=. python3 tests/evaluate_agent.py`：报告会单独输出"真实脱敏语料"
   一节。达到 20 例后启用硬门槛——字段准确率 <95% 时脚本以非零码退出。
5. 未达 100% 的用例会逐字段打印期望值与实际值；修复规则或在 `expected` 中纠正标注后重跑。

## 门槛 7：真人走查脚本（10 分钟北极星场景）

由一名**未参与开发**的同事或目标用户执行，观察者记录。设备：任意一台本机。

准备：按 README 启动服务，浏览器打开 `http://localhost:4174/`，全程不提示操作细节。

| 步骤 | 任务（只把任务念给用户） | 记录 |
| --- | --- | --- |
| 1 | "你想印 500 份 A4 双面海报，下周要用，直接告诉它。" | 用户输入了几句话？系统追问了几轮（澄清轮数）？ |
| 2 | "把数量改成 1200，不要覆膜。" | 字段是否正确更新？旧方案是否失效提示？ |
| 3 | "再加一个 1000 张的三折页，组成一张订单。" | 两项之间数量/尺寸有没有串项？ |
| 4 | "分别为两项选一个性价比方案。" | 方案对比表是否可读？参考费用是否被理解？ |
| 5 | "把你的 PDF 拖进去做预检。"（准备一个有命名问题的 PDF） | 预检结论是否可理解？错误是否可见？ |
| 6 | "生成交接单并确认，导出 Markdown。" | 确认与导出是否顺畅？刷新页面后会话是否恢复？ |
| 7 | 断网/不配置模型重复步骤 1 | 规则回退是否无缝？ |

通过标准：步骤 1 澄清轮数 ≤3；全程无静默错误；步骤 6 刷新恢复成功。
结果记录到本文件末尾的"走查记录"小节（日期、执行人、澄清轮数、问题清单）。

### 走查记录

- （待填）日期 / 执行人 / 澄清轮数 / 遇到的问题 / 结论

## 发布前最终检查（每次发布都要跑）

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=. python3 tests/evaluate_agent.py
python3 -m py_compile agent.py server.py llm_adapter.py product_knowledge.py supplier_adapters.py order_model.py nlu.py tools.py mcp_server.py tools/dsh_mcp_launcher.py tools/dsh_mcp_smoke.py tools/printops_local_host.py
python3 tools/secret_scan.py
python3 tools/dsh_mcp_smoke.py
git diff --check
```

转为稳定 1.1 发布前还需：门槛 4 真实语料达标 + 门槛 7 真人走查通过 + 门槛 12 真实 dsh/模型验收 + 门槛 13 浏览器冒烟通过 + `README`/`VERSION` 版本号核对。真实供应商 live 接入不是 1.1.0 的发布条件，仍须在 v1.2+ 单独验收。
