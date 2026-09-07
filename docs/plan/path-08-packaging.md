# 路径八：打包分发与升级（path-08-packaging）

> 优先级 P2　目标版本 v0.13.0　预估工作量 2-3 人日　依赖：路径 4（存储治理先行，交付物更干净）

## 1. 现状与问题

### 1.1 当前交付形态

- 源码目录 + 三个启动脚本（`start_mac.sh`、`start_windows.bat`、`start_windows.ps1`）；用户拿到的是"一个文件夹"，更新 = 手工替换文件夹；
- 无打包产物、无版本清单（VERSION 文件是唯一版本源）、无升级提示机制；
- ROADMAP P2 已登记："无打包/发布产物：依赖零安装是特色，但缺少 pipx/zipapp 交付与版本清单"；
- README 的"GitHub 发布"章节是手工 git 命令清单，没有 Release 资产约定。

### 1.2 问题的影响

- 给非开发同事试用时，"把整个文件夹拷给你"既不专业也易漏文件；
- 多版本并存时（用户机器上残留旧文件夹），健康检查 `/api/health` 只能证明"是 PrintOps"，不能证明"是哪个版本"——排查端口冲突时（README 已有章节）需要人工比对；
- 无 SHA256 清单，安全敏感的用户无法校验下载完整性。

## 2. 目标与非目标

### 2.1 目标

1. **zipapp 交付**（`.pyz`）：单文件可分发、双击/命令行即启、保持零依赖；
2. **版本自述**：`/api/health` 返回 `{ok, version, knowledgeVersion, schemaVersion, pricingModelVersion}`（当前只返回 `{ok:true}`），排障与升级检查有据可依；
3. **版本清单与校验**：Release 附 `SHA256SUMS`（.pyz 与源码 zip）；
4. **升级提示**：启动时与 `/api/health` 携带 `updateAvailable` 检查结果（只提示不自动更新）；
5. **启动器改进**：端口冲突时给出明确指引（检测到占用 → 提示旧进程与处理命令，而不是裸报错）；
6. GitHub Release 流程文档化：打标签 → 构建 → 清单 → 上传。

### 2.2 非目标

- 不做自动更新（本地工具，用户控制权优先；只做提示）；
- 不做 pip 包/PyPI 发布（零依赖特色下 zipapp 是更贴合的形态；pipx 方案保留评估）；
- 不做代码签名（Windows 签名证书成本与本地工具威胁模型不匹配；在 README 说明校验方式）。

## 3. 方案设计

### 3.1 zipapp 细节

- `python -m zipapp src -o dist/PrintOps-v{VER}.pyz -m "server:main"`——但当前代码是根目录平铺模块（`server.py` 等），zipapp 要求入口可从 zip 内 import。方案：构建脚本（`tools/build_pyz.py`，纯标准库）把根目录 `.py` + `index.html` + `app/` + `styles.css` 打进 zip，并把静态文件读取逻辑指向"zip 内资源或磁盘目录"（`Path(__file__).parent` 在 zipapp 下指向 zip 本身，`ROOT` 判定逻辑需适配：`sys frozen` 检测 + `__file__` 后缀 `.pyz` 判断）；
- `data/` 目录必须落在**磁盘**（zip 内不可写）：`Memory` 与配置路径在 zipapp 模式下解析为 `Path.cwd()/data` 或 `~/PrintOps/data`（决策：优先 `~/PrintOps/data`，避免依赖启动时的 cwd；首次运行创建）；
- 静态白名单逻辑适配：白名单文件从 zip 内读取（`importlib.resources` 或直接 `zipfile` 路径）；
- Windows/macOS 启动脚本新增 `.pyz` 版本（`python PrintOps-v0.13.0.pyz`）。

### 3.2 版本自述与升级提示

- `server.py` 启动时收集 `{"version": VERSION 内容, "knowledgeVersion", "stateSchemaVersion", "pricingModelVersion"}` 存模块级常量；`/api/health` 返回之（**信息无敏感性**：全部是本仓库公开的版本号，可豁免令牌——与 `/api/health` 现有豁免一致，但需在测试中锁定返回不含其他信息）；
- 升级检查：启动时（一次性、超时 3s、失败静默）请求 `https://printops.example/versions.json`（占位——实际地址在 GitHub Releases 的 `latest.json`），比对语义化版本，结果写 `updateAvailable`；**离线/失败完全不阻塞启动**；用户可在设置中关闭检查（`data/settings.json` 新增 `updateCheck: true|false`）；
- UI：导航栏版本号 hover 显示完整版本组，有升级时显示"新版本 vX.Y.Z 可用"非侵入提示（不弹窗）。

### 3.3 启动器改进

- 端口占用：启动前主动探测 4174，若被占用且响应体形如 PrintOps（调 `/api/health` 读 version），输出"端口被 PrintOps vX 占用（PID N），如需重启请先结束该进程：…"（Windows `Get-NetTCPConnection`/macOS `lsof` 指引已在 README，启动器直接给出对应命令与探测到的 PID）；占用者非 PrintOps 则提示换端口运行（`PRINTOPS_PORT`）；
- 后台运行提示：保持"关闭终端即停止"的现有语义（简单透明），但启动横幅明确说明。

### 3.4 构建与 Release 流程

`tools/build_pyz.py` 职责：打 zipapp → 生成 `SHA256SUMS` → 打包 `PrintOps-v{VER}-source.zip`（排除 data/.git/__pycache__）→ 输出 `dist/`。发布步骤写入 `docs/RELEASING.md`（对 RELEASE_CHECKLIST 的补充：构建 → 本地运行 .pyz 冒烟 → tag → GitHub Release 上传三件资产）。

## 4. 接口与数据契约

- `/api/health` 返回体扩展（向后兼容：新增键，旧消费方只读 `ok` 不受影响）：
  `{"ok": true, "version": "0.13.0", "knowledgeVersion": "…", "stateSchemaVersion": 4, "pricingModelVersion": "…", "updateAvailable": null | {"latest": "…"}}`
- `data/settings.json`（新文件，与 llm_config.json 分离）：`{"updateCheck": true}`；
- zipapp 运行模式判定常量暴露给测试（frozen 模式下用临时 cwd 验证 data 目录解析）。

## 5. 分阶段实施步骤

| 阶段 | 内容 | 验证 | 预估 |
| --- | --- | --- | --- |
| A | 资源路径抽象（ROOT 解析：磁盘/zipapp 双模式）+ data 目录固定到 `~/PrintOps/data`（zipapp 模式） | 单测：两种模式路径解析；源码模式行为不变 | 0.5 天 |
| B | `/api/health` 版本自述 + 升级检查（含关闭开关与超时静默） | http 测试：版本键存在、离线不阻塞、updateCheck=false 不发请求 | 0.5 天 |
| C | `tools/build_pyz.py` + `.pyz` 启动脚本 + 构建产物本地冒烟（起服务 → health → 一轮对话） | 构建脚本单测（产物包含清单断言）+ 手动冒烟 | 1 天 |
| D | 启动器端口占用指引 + `docs/RELEASING.md` + SHA256SUMS | 手动：占用场景两种分支 | 0.5 天 |

## 6. 测试与验收

- zipapp 冒烟矩阵：Windows（bat/ps1）+ macOS/Linux（sh）各跑一次 health + 一轮规则模式对话 + 重启后会话恢复（data 在 `~/PrintOps/data`）；
- 升级检查离线行为：断网启动时间无感知差异；
- 版本键与 VERSION/知识版本一致性（改 VERSION 忘同步的场景由测试拦截：health.version 必须等于 VERSION 文件内容）；
- 验收演示：把 `.pyz` 单文件发给未配置环境的同事，双击可用。

## 7. 风险与对策

| 风险 | 概率 | 对策 |
| --- | --- | --- |
| zipapp 内资源读取遗漏（新增静态文件忘打包） | 中 | 构建脚本内置"静态白名单文件必须存在"断言，缺文件构建失败 |
| data 目录迁移造成老用户会话"丢失" | 低 | 仅 zipapp 模式用新目录；源码模式保持 `项目根/data` 不变；CHANGELOG 显式说明 |
| 升级检查端点不存在/迁移 | 低 | 检查失败静默；地址集中一处常量；关关闭开关 |
| `.pyz` 被杀毒软件误报 | 低 | README 提供源码运行替代路径；Release 附 SHA256SUMS 供校验 |

## 8. 工作量与验收门禁

2-3 人日。门禁：双平台 .pyz 冒烟通过、health 版本一致性测试、RELEASING.md 完成一次演练发布（draft release）。

## 9. 依赖与后续

- 路径 4 的会话治理先行（交付物更小更稳）；
- 升级提示端点是路径 10（国际化）文案迁移的第一批键；
- 实施记录：〔待填〕

## 10. 任务卡分解

| 卡号 | 阶段 | 任务 | 改动文件 | 验收标准 | 预估 |
| --- | --- | --- | --- | --- | --- |
| S8-01 | A | ROOT 解析抽象：磁盘模式（现状）与 zipapp 模式（`__file__` 以 .pyz 结尾判定） | server.py（或新增 paths.py） | 两模式路径解析单测；源码模式行为零变化 | 0.25d |
| S8-02 | A | zipapp 模式 data 目录固定 `~/PrintOps/data`（首次运行创建） | agent.py（Memory 路径）、llm_adapter.py（配置路径） | frozen 模式单测（临时 HOME） | 0.25d |
| S8-03 | A | 静态文件读取适配 zip 内资源（白名单三文件 + app/ 模块） | server.py | zipapp 冒烟：页面与模块全部 200 | 0.5d |
| S8-04 | B | `/api/health` 版本自述键（version/knowledge/stateSchema/pricingModel） | server.py | 一致性测试：health.version == VERSION 文件内容 | 0.25d |
| S8-05 | B | 升级检查：启动一次性、3s 超时、失败静默、`updateCheck` 开关 | server.py、data/settings.json 读取 | 断网启动时间无差异；开关关闭零请求（连接计数断言） | 0.5d |
| S8-06 | B | UI 版本 hover 与升级非侵入提示 | app.js（迁移后） | 有/无升级两态快照 | 0.25d |
| S8-07 | C | `tools/build_pyz.py`：打 zipapp + 白名单存在断言 + SHA256SUMS + source.zip | tools/build_pyz.py（新） | 产物清单断言单测；缺文件构建失败 | 0.5d |
| S8-08 | C | `.pyz` 启动脚本（三平台） | start_pyz.bat/.sh | 双平台实跑 health + 一轮对话 | 0.25d |
| S8-09 | D | 启动器端口占用指引（识别 PrintOps 占用 → 给出 PID 与处理命令） | 三个启动脚本 + server.py 启动前探测 | 占用/非占用两分支手测 | 0.25d |
| S8-10 | D | `docs/RELEASING.md` + GitHub Release 资产约定（pyz/sums/source.zip） | 文档 | 演练发布一次（draft release） | 0.25d |

## 11. /api/health 完整契约（本路径后）

```json
{
  "ok": true,
  "version": "0.13.0",
  "knowledgeVersion": "2026.09.05",
  "stateSchemaVersion": 3,
  "pricingModelVersion": "2026.09.05",
  "updateAvailable": null
}
```

- `updateAvailable` 形态：`{"latest": "0.14.0"}`（仅当远端 versions.json 的 latest 语义化大于本地）；检查被禁用或失败时为 null；
- 豁免令牌策略不变：该端点只暴露版本号（仓库公开信息），测试锁定"返回体不含其他任何键"；
- 版本一致性：`version` 读自 VERSION 文件而非硬编码，改版本忘同步由测试拦截。

## 12. 构建产物清单与测试矩阵

产物（dist/）：

| 文件 | 内容 | 校验 |
| --- | --- | --- |
| PrintOps-v{VER}.pyz | 全部 .py + index.html + app/ + styles.css + VERSION | SHA256SUMS 行 |
| PrintOps-v{VER}-source.zip | 源码目录（排除 data/.git/__pycache__/.mimosa） | SHA256SUMS 行 |
| SHA256SUMS | 上述两文件的 sha256 | sha256sum -c 通过 |

测试矩阵：

| 层 | 用例组 | 断言要点 | 卡号 |
| --- | --- | --- | --- |
| 单测 | ROOT 双模式 | 磁盘/zipapp 判定与路径解析 | S8-01 |
| 单测 | data 定位 | frozen 模式 → `~/PrintOps/data`（临时 HOME 注入） | S8-02 |
| 单测 | 资源读取 | zip 模式白名单三文件内容与磁盘一致 | S8-03 |
| http | health | 六键结构；version==VERSION；无多余键 | S8-04 |
| 单测 | 升级检查 | 离线静默；开关关闭零请求；latest 大于本地才提示 | S8-05 |
| 构建 | 清单断言 | 白名单文件缺失 → 构建失败退出非零 | S8-07 |
| 手动 | 冒烟 | 双平台 .pyz：health + 一轮对话 + 重启会话恢复 | S8-08 |
| 手动 | 端口指引 | PrintOps 占用给出 PID 命令；第三方占用提示换端口 | S8-09 |
