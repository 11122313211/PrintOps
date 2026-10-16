# 下一阶段产品流程：浏览器验收

本轮使用 Playwright 1.56.1 的实际桌面浏览器引擎，连接临时 SQLite 和本地 Python HTTP 服务。模型关闭，页面外部网络请求被阻断；测试结束删除临时数据库。所有订单和 PDF 都是合成资料，未使用真人订单或供应商接口。

运行新流程专项：

```sh
npx playwright test --config tests/journey.config.mjs
```

默认保持 Chromium；其他引擎使用环境变量，报告分别输出，互不覆盖：

```sh
npx playwright install firefox webkit
PRINTOPS_TEST_BROWSER=firefox npx playwright test --config tests/journey.config.mjs
PRINTOPS_TEST_BROWSER=webkit npx playwright test --config tests/journey.config.mjs
```

现有基础套件仍独立运行：

```sh
npx playwright test --config tests/browser_smoke.config.mjs
```

## 覆盖的用户流程

| 流程 | 实际验证 |
| --- | --- |
| 首次示例 | 点击只填入输入框，不保存或覆盖已有订单；提交后及恢复已有订单时示例入口隐藏 |
| 单产品交接 | 填齐 → 比较 → 选择 → 生成草稿 → 人工确认 → 下载 JSON/Markdown；核对订单 ID、revision、确认版本、产品字段和 Markdown 原文；修改数量后旧确认与导出失效 |
| CSV 交接 | 实际上传、生成、确认并下载 CSV；使用 Python 标准 `csv` 解析器核对文档元数据、确认版本、itemId、生产字段、选中方案及组成、文件状态/时间/范围/检查明细/提醒；五次导出覆盖 `=`、`+`、`-`、`@`、前导空白与制表符的公式前缀，含逗号和引号的文件名可正确解析，危险内容以单引号保护 |
| 字段复核 | 模糊数量作为候选；确认、拒绝、清空、撤销；撤销是新 revision，刷新保留结果 |
| 2/3 产品作用域 | UI 切换不增加业务消息、不推进业务 revision；后续聊天请求携带明确 itemId，只改目标项数量 |
| 真实版本冲突 | 独立认证 API 客户端先写入，旧页面收到 409；保留输入，读取最新值显示差异，点击重新应用后才覆盖 |
| 写入成功但丢回执 | 请求真实提交到服务端后模拟浏览器连接失败；只读恢复已保存结果，不给未知操作自动重放入口；A 的回执在切 B 后丢失不污染 B，返回 A 后继续编辑可保留，但新写入被阻断，读取恢复后才允许显式提交 |
| 键盘 | Enter/Space 展开和收起方案理由不选择方案、不发业务写入；逐个 focus 后使用键盘输入与 Enter/Space 完成单品提交、选方案、生成、确认及实际下载 JSON，核对确认版本和产品字段 |
| 价格状态 | ready 显示示例范围；partial 只显示已知小计和未计工艺；blocked 不提供范围；修改后旧金额隐藏或明确失效 |
| 文件与检查状态 | 合成 A4 PDF 绑定单项、刷新保留检查；尺寸修改令检查 stale，界面不再宣称基础检查通过 |
| 文件与切换竞态 | 使用受控 File 读取延迟，读取过程中切到另一项；最终请求保留上传起始 itemId，文件不串项，页面保持最新选择项 |
| 未选产品上传 | 多产品无明确 itemId 时，在读取文件和发起预检请求前阻断，不改变订单版本 |
| 异步响应归属 | 订单 A 的工具请求已完成但回包被延迟时打开 B，旧结果不覆盖 B；相同 revision 的产品选择乱序返回时保留最后一次选择 |
| 产品估价归属 | 第一项生成估价后切到第二项，不把第一项价格作为第二项的当前估价 |
| 未保存复核草稿 | 数量草稿跨阶段切换、保存另一字段后仍保留；明确保存数量前不写入后端 |
| 归档只读 | 归档后在补充、比较、核对阶段间切换，聊天、方案请求、复核输入及保存保持禁用 |
| 候选值冲突 | confirm/reject 收到真实 409 后不得重放到已变化的候选；清空操作先展示当前值与清空意图，读取本身不改值 |
| 凭据恢复 | 合成一次 403 后重新读取本地凭据，失败的聊天输入仍保留，恢复本身不提交草稿或推进 revision |
| 布局 | 390/768/1280px 以及 640px 重排视口；补充、比较、核对界面无页面横向溢出；窄屏“订单 / 当前任务”切换正常 |

640px 是 1280px 浏览器在 200% 放大时的布局宽度等效测试，**未验证实际浏览器缩放、手机系统键盘或真实移动设备**。滚动区域自身允许表格横向滚动，断言检查页面是否整体溢出。键盘通路使用定位后的 `focus()` 和真实键盘事件，未验证全程自然 Tab 顺序、屏幕阅读器或辅助技术；通过这些脚本也不能证明颜色对比度符合全部要求。

PDF fixture 为 `tests/fixtures/synthetic-a4-print.pdf`，内容明确标示 SYNTHETIC TEST ONLY。脚本覆盖文件元数据检查与状态归属，不声称完成专业印前、RIP、字体、分辨率或色彩生产验收；文件字节不会上传给真实供应商。

CSV 验证检查实际下载字节、UTF-8 BOM、标准解析结果与公式前缀保护，没有在 Excel 或其他电子表格软件中执行公式，也未将其解释为所有导入设置下的完整兼容验收。

## 结果与证据

2026-10-16 完成 **34 项完整流程，Chromium 与 WebKit 各 34 项通过**。当前默认命令包含所有边界、键盘、CSV、异步响应、文件归属与布局用例；每例有实际页面截图，交接用例保存实际下载的 JSON/Markdown/CSV；失败时保留 Playwright trace 和截图用于复现。

现有 **12 项基础测试在最终布局修改后复跑通过**，报告仍在 `reports/browser-smoke/`，覆盖历史恢复、mm、来源明细、未配置供应商、安全文本、低置信确认、归档与完整备份、窄屏导航及设置/风险清单布局。

最终跨引擎记录：

| 引擎 | 结果 | 证据 |
| --- | --- | --- |
| Chromium 141.0.7390.37 | 完整 34 项通过 | `reports/journey-browser/report.json` |
| WebKit 26.0 | 完整 34 项通过 | `reports/journey-webkit/report.json` |
| Firefox 142.0.1 / Playwright build 1495 | 本机环境阻断，未进入页面，0 项通过 | `reports/journey-firefox/launch-diagnostics.json`；首次套件 1 interrupted / 16 not run |

Firefox 在 macOS 27.0.1（26A434）arm64 上分别以 headless 和可见窗口模式进行独立启动检查，均在 15 秒后超时；日志含 `sandbox_extension_issue_file_to_process ... Operation not permitted`，headless 另有 `RenderCompositorSWGL failed mapping default framebuffer`。未通过关闭浏览器安全策略绕过。该结果不能解释为页面兼容失败或通过，需在可启动的支持环境补做 Firefox 验证。WebKit 是 Playwright 引擎，不能外推为所有 Safari 版本或真实 iPhone。

各报告目录下的 `html/index.html` 为可浏览报告；上述通过结果无跳过或不稳定重试。仅复跑两个追加项并保存独立报告可使用：

```sh
PLAYWRIGHT_HTML_OPEN=never \
PLAYWRIGHT_JSON_OUTPUT_FILE=reports/journey-extra-chromium/report.json \
PLAYWRIGHT_HTML_OUTPUT_DIR=reports/journey-extra-chromium/html \
npx playwright test --config tests/journey.config.mjs --grep 'supplemental:' \
  --reporter=list,json,html --output reports/journey-extra-chromium/results
```

WebKit 使用 `PRINTOPS_TEST_BROWSER=webkit`，并将三个报告路径中的 `chromium` 改为 `webkit`。

单独复跑 CSV 时，将 `--grep` 改为 `'csv audit:'`，三个报告路径中的 `journey-extra-` 改为 `journey-csv-`，可保留其他批次证据。

本轮浏览器测试发现并推动修复 JSON/Markdown 统一附加 BOM、无初始产品作用域的上传等问题，并验证异步响应、复核草稿、候选冲突和恢复边界；没有为了通过检查而注入虚构供应商结果或强制选择未确认字段。

Firefox 页面验证仍受环境阻断；真实手机、真实授权供应商、生产容量、真实用户 10 分钟完成率尚未覆盖。8 人首次使用走查及至少 20 例真实脱敏语料、字段准确率至少 95% 的正式发布要求仍需独立完成；自动化合成测试不替代这些门槛。
