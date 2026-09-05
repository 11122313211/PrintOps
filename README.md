# PrintOps 印刷订单智能体

PrintOps 是一个面向市场、设计和采购团队的本地印刷订单 Agent。用户只需要描述“要印什么、用于什么场景”，系统就会逐步补齐印刷品类所需参数，解释工艺取舍，生成可询价、可交接的订单草稿。

项目的北极星目标是：让非印刷专业用户在 10 分钟内把模糊需求整理成完整、可靠、可追溯的印刷订单。

核心分工保持清晰：

- 模型负责理解、追问、解释和选择工具。
- 确定性规则负责字段规范化、品类参数、工艺约束和风险判断。
- 工具负责预检、方案、询价准备和导出等动作。
- 生成交接单和未来的外部提交必须经过人工确认。

## 快速开始

### 环境要求

- Python 3.9 或更高版本。
- 只使用 Python 标准库，不需要安装第三方包。
- macOS、Linux 和 Windows 共用同一套 Agent、API 和前端代码。

### 唯一本地入口

PrintOps 的默认端口和验收端口统一为 **4174**：

```text
http://localhost:4174/
```

不要使用历史临时端口打开项目。启动后可以访问下面的健康检查接口确认服务是否真的属于 PrintOps：

```text
http://localhost:4174/api/health
```

正常响应为 `{"ok": true}`。

### macOS / Linux

在项目根目录执行：

```bash
python3 server.py
```

也可以使用启动脚本：

```bash
./start_mac.sh
```

如果脚本没有执行权限，可先运行 `chmod +x start_mac.sh`。

### Windows

在项目根目录双击 `start_windows.bat`，或者在 PowerShell 中执行：

```powershell
.\start_windows.ps1
```

启动终端需要保持打开，关闭终端会停止本地服务。

### 端口冲突排查

默认端口被其他程序占用时，先确认占用者，不要直接把浏览器切到旧端口：

macOS / Linux：

```bash
lsof -nP -iTCP:4174 -sTCP:LISTEN
```

Windows PowerShell：

```powershell
Get-NetTCPConnection -LocalPort 4174 -State Listen
```

如果确认是旧的 PrintOps 进程，可以结束旧进程后重新启动。开发排障时也可以临时使用环境变量覆盖端口，但提交、验收和团队协作仍统一使用 `4174`：

```bash
PRINTOPS_PORT=4174 python3 server.py
```

## 使用流程

1. 在聊天框描述印刷品、用途、数量和已有尺寸，例如“做 500 张 B4 双面海报，下周要用”。
2. Agent 根据品类补问必要参数；不确定的字段会标记为待确认，不会静默猜测。
3. 在右侧订单草稿中检查数量、尺寸、材料、颜色、后道、文件和平台能力。
4. 选择经济、平衡或质感方案；修改字段后，旧方案和旧交接状态会自动失效。
5. 在浏览器本地完成 PDF 基础预检，查看页数、页面框、命名和常见印前风险。
6. 生成交接单并人工确认，再导出 JSON、CSV 或 Markdown；当前不会向任何供应商真实下单。

## 当前能力

- 用自然语言收集用途、品类、数量、尺寸、纸张/材料、颜色、工艺、装订、交期和预算。
- 按品类补齐差异化参数，覆盖画册、折页、名片、联单、标签、包装盒、手提袋、纸杯、海报、喷画和 PVC 等。
- 识别 A4、B4、`210*267mm` 等尺寸表达，并区分：
  - `dimensions.finishedSize`：成品平面尺寸。
  - `dimensions.expandedSize`：折叠或包装展开尺寸。
  - `dimensions.dieCutSize`：刀模尺寸。
  - `dimensions.packageSize`：包装长、宽、高等三维尺寸。
- 规则感知按证据强度为每个字段记录置信度；低置信度的生产字段必须人工确认后才能生成交接单。
- 工艺推荐区分合版/专版并说明批次色差与颜色可控性；低于品类常规起印量时给出提示。
- 包装盒支持内尺寸/外尺寸语义记录，未标注时在订单校验中提示；糊盒刀模以内尺寸为准。
- 交接文本附“开数参考”（大度/正度整裁数与纸张利用率）；费用估算基于带版本的示例价格参数表，只作量级参考。
- 支持多产品订单，使用稳定 `itemId` 隔离每一项的数量、规格、文件、方案和交接状态。
- 根据预算、用途、交期和生产约束生成可解释的经济、平衡、质感方案。
- 支持自然语言修改和否定，例如“数量改成 1200 张”“不要覆膜”。
- 浏览器只读取 PDF 的轻量元数据，原稿不会上传到本地服务。
- 通过平台能力档案检查品类、材料、工艺、尺寸和交期限制，不把任何一家供应商写死在核心流程里。
- 本地保存会话记忆、字段来源、置信度、冲突、运行轨迹和询价状态，刷新或重启后可以继续处理。
- 可选接入 OpenAI 兼容 API；模型不可用、未配置或返回异常时自动回退到规则 Agent。

## Agent 工作流

```text
需求理解 -> 品类澄清 -> 参数补全 -> 工艺推荐 -> 文件预检
       -> 报价准备 -> 人工确认 -> 平台交接 / 导出
```

工作流阶段在 API 和 UI 中使用以下稳定标识：`collect`、`clarify`、`recommend`、`preflight`、`quote`、`confirm`、`export`。

## 可选接入 LLM

点击界面右上角的“接口设置”，填写 OpenAI 兼容接口 URL、模型名称和可选 API Key。URL 和模型名同时留空即可使用规则模式。

也可以通过环境变量提供初始配置：

```bash
export PRINTOPS_LLM_URL="https://example.com/v1"
export PRINTOPS_LLM_MODEL="模型名称"
export PRINTOPS_LLM_KEY="仅在本机环境中提供"
python3 server.py
```

安全约束：

- 通过界面保存的 API Key 只保存在本机 `data/llm_config.json`，文件权限为 `600`（Windows 使用系统文件权限）；通过环境变量启动时只在当前进程中使用，不会自动写入文件。
- Key 不会返回到 `/api/settings`，也不会写入聊天记录、运行轨迹、订单导出或日志。
- `data/`、`.env`、数据库和本地配置已加入 `.gitignore`。
- README、源码、截图和 GitHub Issue 中都不能粘贴真实 Key；发布前必须做敏感信息扫描。
- LLM 只提交受限字段补丁和工具意图，订单校验、能力匹配、询价幂等和人工确认仍由本地确定性代码负责。

## API 速览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | 服务健康检查 |
| POST | `/api/session` | 新建或恢复会话 |
| POST | `/api/chat` | 对话补全订单，支持 `itemIndex` |
| POST | `/api/choose` | 选择工艺方案 |
| POST | `/api/generate` | 生成订单交接草稿 |
| POST | `/api/confirm` | 记录人工确认，不提交供应商 |
| POST | `/api/preflight` | 接收结构化 PDF 预检线索 |
| POST | `/api/platform` | 切换目标平台 |
| POST | `/api/quote/status` | 查询本地询价状态 |
| POST | `/api/quote/cancel` | 取消本地询价请求 |
| GET | `/api/products` | 查看产品目录、参数和知识版本 |
| GET | `/api/platforms` | 查看平台能力档案 |
| GET | `/api/tools` | 查看白名单工具契约 |
| POST | `/api/tools/call` | 显式调用白名单工具 |
| GET / POST | `/api/settings` | 查看或更新本机模型配置（不返回 Key） |
| POST | `/api/model/test` | 测试模型连接，不保存配置、不发送订单数据 |

API 错误统一返回 `code`、`message`、`requestId`，响应头包含 `X-Request-ID`。

## 目录结构

```text
.
├── agent.py                 # Agent 状态机、会话记忆、工作流和工具网关
├── order_model.py           # 订单数据契约、数量/尺寸规范化与 schema 迁移
├── nlu.py                   # 规则感知：字段抽取与置信度分级
├── tools.py                 # 白名单工具注册表与工具契约
├── server.py                # 本地静态服务与 JSON API，默认端口 4174
├── llm_adapter.py           # OpenAI 兼容模型适配器和本地配置读写
├── product_knowledge.py     # 品类目录、专属参数、印刷知识与示例价格参数表
├── supplier_adapters.py     # 平台能力档案与按品类分层的字段映射协议
├── index.html               # 浏览器入口和订单工作台结构
├── app.js                   # UI 状态、对话和 API 交互
├── styles.css               # 工业感界面样式
├── start_mac.sh             # macOS / Linux 启动脚本
├── start_windows.bat        # Windows CMD 启动脚本
├── start_windows.ps1        # Windows PowerShell 启动脚本
├── tools/
│   └── secret_scan.py       # 发布前敏感信息扫描
├── docs/
│   ├── ARCHITECTURE.md      # 架构、数据契约和安全边界
│   ├── ROADMAP.md           # 版本路线图和持续目标
│   └── OPEN_SOURCE_OPTIONS.md # 开源项目选型参考
├── tests/
│   ├── test_agent.py        # Agent 单元测试
│   ├── test_server.py       # API 契约测试
│   ├── test_v09_features.py # v0.9.0 特性测试
│   └── evaluate_agent.py    # 脱敏订单评测
└── data/.gitkeep             # 运行时数据目录占位文件
```

## 测试与开发

在项目根目录执行：

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=. python3 tests/evaluate_agent.py
PYTHONPATH=. python3 tests/evaluate_agent.py --json
python3 -m py_compile agent.py server.py llm_adapter.py product_knowledge.py supplier_adapters.py order_model.py nlu.py tools.py
python3 tools/secret_scan.py
git diff --check
```

以上命令由 `.github/workflows/ci.yml` 在每次 push / PR 时自动执行（Ubuntu 与 Windows × Python 3.9/3.12）。

发布前至少确认：字段准确率不低于 95%、基础订单完整率达标、多产品之间没有字段污染、模型异常能回退、刷新能恢复会话、等待和错误状态可见，并通过敏感信息扫描。

## 安全边界与已知限制

- 当前供应商档案是静态能力示例，不代表实时价格、产能或交期。
- 费用估算来自内置示例价格参数表（带版本），仅用于量级参考，不构成报价。
- 当前不会向盛大印刷或其他平台发起真实报价、下单或上传文件请求。
- PDF 预检是浏览器本地的轻量检查，不能替代 Acrobat、PitStop 或印刷厂正式印前检查。
- 开数参考是纯几何估算，实际拼版以供应商为准。
- 外部提交必须在后续适配器中实现人工确认、幂等、超时、重试、取消和审计。
- 规则和知识版本会随版本更新；订单响应会携带知识版本，便于追溯一次推荐使用的依据。

## 文档与版本

- [架构与数据契约](docs/ARCHITECTURE.md)
- [持续目标与路线图](docs/ROADMAP.md)
- [开源选型参考](docs/OPEN_SOURCE_OPTIONS.md)
- [版本变更记录](CHANGELOG.md)

当前版本以根目录 `VERSION` 文件为准，当前为 `v0.10.1`。macOS 与 Windows 只更换启动脚本，订单逻辑和 UI 不分叉。

## License

本项目以 [MIT License](LICENSE) 发布。

## GitHub 发布

本地仓库已经按可发布结构整理，但当前没有配置远程仓库。创建空的 GitHub repository 后，在项目根目录执行：

```bash
git remote add origin <你的 GitHub 仓库 URL>
git add .
git commit -m "docs: rewrite README and standardize local port"
git push -u origin main
git tag v0.8.2
git push origin v0.8.2
```

推送前请确认没有把运行时数据或密钥带入版本库：

```bash
git status --short
git ls-files data
rg -n --hidden --glob '!data/**' 'sk-[A-Za-z0-9_-]{12,}' .
```

不要在 README、提交信息、Issue 或截图中记录真实 API Key。仓库 URL、账号和 GitHub 授权需要由项目维护者自行确认，不能猜测或复用未知凭据。
