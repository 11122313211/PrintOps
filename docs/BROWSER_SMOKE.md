# 浏览器开发验收

这是可选的开发验收依赖。PrintOps 的正常启动仍只需要 Python 标准库；运行 `python3 server.py` 不会启动 Node、Playwright 或浏览器。

## 安装与运行

需要 Python 3、Node.js 18+ 和 npm。首次执行：

```sh
npm ci
npm run test:browser:install
npm run test:browser
```

后续直接运行 `npm run test:browser`。`@playwright/test` 作为固定版本的 `devDependency` 保存在 `package.json` / `package-lock.json` 中。浏览器由 Playwright 下载并运行，不调用用户的系统 Chrome，也不依赖已有浏览器登录态。Linux CI 若缺少浏览器系统库，可由 CI 管理员使用 Playwright 官方安装方式补齐依赖。

可通过 `PRINTOPS_TEST_PYTHON=/absolute/path/to/python3 npm run test:browser` 指定 Python。服务监听随机的 `127.0.0.1` 端口，正常结束和测试失败都会停止服务。

## 实际覆盖

测试文件为 `tests/browser_smoke.spec.mjs`；每个用例使用新的浏览器上下文，通过真实点击、输入、刷新和 HTTP 响应完成验证。

| 用例 | 验收断言 |
| --- | --- |
| `history restores the active order after reload and survives cleared browser storage` | 对话填写后刷新恢复订单和历史；恢复不改变 revision；清空 localStorage 后仍可从后端订单历史选回原订单 |
| `chat converts dimensions to mm and manual dimensions reject cm` | 对话中成品尺寸/展开尺寸独立换算成 mm；手工 cm 输入被明确拦截；手工无单位数字按页面约定存为 mm；局部改成品尺寸保留展开尺寸 |
| `price line items expand and retain illustrative provenance` | 点击估算工具后，真实展开 `<details>`；显示 API 返回的所有费用项、金额、示例来源和免责声明 |
| `supplier selection visibly reports the unconfigured local-draft state` | 通用平台显示本地导出；未配置的供应商显示本地草稿及待确认能力；模拟供应商明确显示沙箱 |
| `dangerous HTML is inert in chat and persisted field rendering` | 合成 `<img onerror>` / `<script>` 输入在聊天和字段持久化恢复后均不执行，不产生可执行 DOM 节点 |
| `low-confidence quantity requires the visible confirmation button and persists confirmation` | “大约1k张名片”的标准视图先为待确认且生产 value 为 null；点击“确认此值”后变为用户确认/100%，刷新仍保留 |
| `history downloads a complete backup and archives an order without preventing a new order` | 实际下载并解析完整 JSON，核对订单/标准视图/对话/事件/审计；归档后从默认历史隐藏、字段与上传只读；显示归档后仍可导出，重置后可编辑新订单 |
| `390px layout does not overflow horizontally and navigation opens and closes` | 在 390×844 窗口实际测量页面宽度，打开导航抽屉、ESC 关闭并填写订单，均无横向溢出 |

测试服务器使用临时 SQLite 数据库，覆盖运行时模型配置为空，不修改项目的 `data/`。所有输入都是合成测试数据，不能计入真实客户样本评测。浏览器上下文阻断非测试服务器的网络请求，并在出现意外外部请求或未捕获 JavaScript 错误时使测试失败。

`server.py` 的已有会话恢复接口单独有 `tests.test_storage_api.OrderHistoryAPITest.test_session_restore_is_readonly_and_preserves_the_cas_revision` 验证：不执行工具、不保存订单、不追加事件/审计，原 revision 仍可用于下一次业务修改。

## 产物与边界

产物位于 Git 忽略的 `reports/browser-smoke/`：`report.json`、`html/index.html`、每项实际页面截图，以及失败时的 Playwright trace。查看 HTML 报告：

```sh
npx playwright show-report reports/browser-smoke/html
```

当前使用 Chromium 验收桌面流程及一个 390px 窄屏布局。没有覆盖 Firefox/WebKit、真实移动浏览器和触屏、真实模型规划、真实供应商请求、真实 PDF 印前能力、价格市场准确度或真实客户输入分布；这些仍需各自的验证证据。截图记录实际渲染，但不是像素级视觉回归基线，也不能替代人工布局检查。
