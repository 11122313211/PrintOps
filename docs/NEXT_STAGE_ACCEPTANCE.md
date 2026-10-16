# 下一阶段开发交付与验收

日期：2026-10-15。范围：[NEXT_STAGE_PRODUCT_DESIGN_PLAN](NEXT_STAGE_PRODUCT_DESIGN_PLAN.md) 的 G01–G08。基线 `9f21c0e` 上的未提交工作区，`VERSION` 仍为 1.2.0 候选版；不代表正式发布或北极星真人目标已经达成。

## 交付判断

当前具备从需求、字段核对、方案比较到本地确认和当前版本交接导出的可运行路径。服务端持有权威订单，前端为三阶段工作台；供应商接口保持通用、无真实网络传输。当前价格全部是示例参数，真实语料仍为 0，发布门槛保留阻断。

| 工作包 | 已交付与开发落点 | 证据 | 尚未证明 |
| --- | --- | --- | --- |
| G01 定义/首用 | 中英README同步，三种用途入口、file协议启动提示、8人固定任务/评分包 | README、USER_WALKTHROUGH、首用浏览器用例、新截图 | 真人从安装到完成的理解与耗时 |
| G02 下一步 | 后端review字段/问题/门禁投影；任务卡与阻断定位；供应商说明独立 | order_contract.py、journey.js、旅程API | ≥7/8用户10秒内说出下一步 |
| G03 核对/作用域 | fields显式设置/清空/确认/拒绝、撤销新版本；focus只切视图；稳定itemId；输入草稿与焦点保留；旧响应隔离 | app.js、journey.js、agent.py、字段/多品类/乱序回归 | 真人辨认作用对象与误操作率 |
| G04 恢复 | 保存回执、持久错误卡、409差异、候选冲突不重放、未知结果先读取；按订单隔离；不刷新更新凭据；操作回执与订单同事务 | server.py、agent.py、持久幂等/API/故障浏览器测试 | 真实部署容量与长时运行；异常退出后人工对账 |
| G05 交接 | 后端权威文档、草稿/本地确认版本、JSON/MD/CSV/复制/打印；同义字段去重、冲突阻断；A4产品分组与页码 | 下载一致性测试；实际打印PDF与逐页渲染检查 | 独立复核者60秒找字段；各浏览器实际打印对话框设置 |
| G06 比较/计价 | 主区域比较、仅看差异、独立选择按钮；示例口径、ready/partial/blocked/stale；按产品隔离缓存 | workbench.js、app.js、四态及键盘回归 | 真人理解方案取舍；真实成本精度 |
| G07 视觉/可访问 | 令牌、44px高频控件、抽屉焦点、短播报、窄屏切换、减少动画；组件状态矩阵 | UI_STATES、浏览器与实际截图、打印检查 | Firefox环境阻断；真实手机/200%缩放/辅助技术与全面对比度审计 |
| G08 运行/扩展 | SQLite一致性备份、新目录恢复验证、保留策略接口；5个成本算例；本地mock扩展包；整体请求预算 | STORAGE_OPERATIONS、PRICING_WORKED_EXAMPLES、SUPPLIER_EXTENSION_GUIDE、RUNTIME_BUDGET及测试 | 真实价格校准、企业存储、授权供应商与真实模型环境 |

## 本轮工程验证

- Python 3.9.6：完整 **463 项通过**，0失败、0跳过。最终全套日志：`reports/next-stage-acceptance/final-unittests.stderr.txt`；此前后端命令详情保留在 `backend-summary.json`（该批次462项），后续增加1项扫描回归。
- 合成评测：111例，342/342字段匹配（100%）；基础完整率13/14（93%，四舍五入）。“尺寸识别”用例仍未完整。该旧指标只检查字段完整性，不等于确认、导出或真人10分钟完成率。
- 严格发布：`PYTHONPATH=. python3 tests/evaluate_agent.py --release-gate --json` **退出码1**；真实0例、`realCorpusVerdict=pending`、`releaseGatePassed=false`，与用户保留门槛的要求一致。
- 离线MCP/local-host、自检、Python编译、敏感模式扫描通过。末轮浏览器报告的PNG/ZIP Base64碰撞Google密钥前缀；扫描增加token起始边界，11项安全回归验证仍检出文本/引号/URL中的凭据，未跳过报告目录。这些检查不证明真实dsh/provider兼容，扫描也不是全面安全审计。
- Chromium/WebKit 各 34 项完整 journey 通过，Chromium 基础冒烟 12 项通过；最终运行与逐例覆盖见 [BROWSER_JOURNEY](BROWSER_JOURNEY.md)，基础套件见 [BROWSER_SMOKE](BROWSER_SMOKE.md)。测试使用临时 SQLite、关闭模型并拦截外部请求；合成记录不进入真实库。
- 存储备份/恢复使用隔离数据库和表内容hash验证，未操作业务库。价格人工算例为3个ready、1个partial、1个blocked；第三方mock无网络。
- 视觉检查：Chromium生成1/2/3产品与长字段交接PDF；用Poppler渲染每页人工检查。1/2产品各1页，3产品2页；396字长字段样例1页，固定列宽避免长文本挤压标签，5个最终页面全部检查；文件待交付与未配置供应商明确，不声称可生产。报告及图片在 `reports/journey-design/`。

原始报告默认被Git忽略。README截图、以下文档和合成fixture已作为可纳入版本管理的交付材料；复现时应重新生成报告，不把旧报告当作后续代码的验收。

## 复现入口

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=. python3 tests/evaluate_agent.py --json
PYTHONPATH=. python3 tests/evaluate_agent.py --release-gate --json
python3 tools/secret_scan.py
python3 tools/repo_guard.py
python3 tools/dsh_mcp_smoke.py
npm ci
npx playwright install chromium webkit
npx playwright test --config tests/browser_smoke.config.mjs
npx playwright test --config tests/journey.config.mjs
PRINTOPS_TEST_BROWSER=webkit npx playwright test --config tests/journey.config.mjs
```

严格发布命令当前预期失败，不能删除flag来制造发布通过。产品运行本身不需要npm或Playwright。

打印/截图复现：先运行 `python3 -u tests/browser_smoke_server.py`，读取它输出的临时本地URL；另一个终端执行 `node tools/capture_journey_demo.mjs <该URL>`。只允许localhost/127.0.0.1，不使用业务订单或外部服务。检查结束用Ctrl+C停止隔离服务。截图依赖字体与浏览器环境，不作像素一致性承诺。

## 仍需执行的下一阶段目标

| 优先级 | 目标 | 完成证据与依赖 |
| --- | --- | --- |
| P0 | 用8名首次用户验证10分钟合格交接 | 按USER_WALKTHROUGH执行3/3/2任务；≥7/8独立合格、关键错误0、全部理解非报价及未发送；保留原始文件和失败，不用自动化代替 |
| P0 | 补齐实际设备与辅助操作 | 一台真实手机、实际200%缩放、全键盘及屏幕阅读器；逐状态对比度记录（核心8组令牌已抽检通过，非全面合规证明）；在支持Firefox的环境重跑，不声称当前Firefox通过 |
| P0 发布门槛 | 收集并独立标注真实脱敏订单 | ≥20例、字段准确率≥95%；用户当前无数据，保持门槛和空库；资料到位后再验收 |
| P1 | 真实模型/dsh集成验收 | 具备合法可用环境后记录协议、回退、超时与on/off parity；不记录密钥或客户原稿 |
| P1 | 提升计价依据 | 获取可用成本/报价、统一税运口径，人工核对拼版/大纸/损耗/最低收费，生成校准draft并复核后发布活动版本 |
| P2 | 按部署需求扩展 | 企业存储、备份调度和容量策略以实际部署需求为触发；默认不自动删除追溯资料 |
| P2 | 接入任一授权供应商 | 复用通用协议与mock套件；取得官方协议、授权和sandbox后再开发真实transport、认证、确认、对账与审计；盛大没有特殊硬编码前提 |

当前不新增主Agent/子Agent产品架构。下一轮优先消除真人任务中的失败原因，再安排新功能；不按天数作承诺。
