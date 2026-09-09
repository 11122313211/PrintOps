# PrintOps dsh profile

本目录保存 PrintOps 的 dsh skill，以及一个**禁用状态的 profile 示例**。精确版本快照见
[`runtime.lock.json`](runtime.lock.json)；它锁定已核对的 dsh alpha.1、MCP client、Node
范围和 pnpm 版本，但不冒充 npm 的完整传递依赖 lockfile。生产接入前仍须在有网络的
构建环境捕获上游 lockfile，并对升级重跑 smoke/parity。

在支持项目级 skill registry、且已挂载对应 skill/filesystem 插件的 dsh 版本中，项目根目录
下的 `.dsh/skills/<name>/SKILL.md` 会自动发现，通常不需要另行注册。dsh 仍处于 developer
preview，当前环境未完成真实 dsh 进程联调；[`profile.example.json`](profile.example.json)
只是按 alpha.1 资料整理的 descriptor，不会被自动加载；可复制的真实 profile 形状在
[`profile.example/`](profile.example/)（`package.json` + `cordis.patch.yml` + `cordis.yml`），
同样默认把 MCP row 标记为 disabled。

本地 MCP 入口（建议从仓库根目录启动；也可以省略 `--memory-path` 使用适配器旁边的默认数据库）：

```text
python3 mcp_server.py --session-id <session-id> --memory-path /absolute/path/to/print-order-agent-mvp-v0.1.0/data/agent.sqlite3
```

推荐让 dsh 通过受信 launcher 启动 MCP。launcher 会把 server 和 SQLite 路径解析为绝对
路径，拒绝无效 session、L2 capability 和 `--allow-any-session`，并使用 argv 直接
`execve`，不经过 shell：

```text
python3 tools/dsh_mcp_launcher.py --session-id <validated-session-id> --capabilities L0
```

启动前可以先检查最终 argv（不会启动 MCP）：

```text
python3 tools/dsh_mcp_launcher.py --session-id smoke-session --dry-run
```

该命令输出的 `__BOUND_SESSION_ID__` 不能直接传给 dsh；profile 中的占位符必须由受信宿主
替换成具体 session ID。dsh MCP client 不会展开 shell 变量，也不会替换此占位符。

默认只开放 L0 只读工具。`--capabilities L0,L1` 会额外开放元数据预检、本地交接、询价草稿和受控的 `apply_order_patch`；不会启用真实供应商、CUPS 或其他外部副作用。`<session-id>` 必须由受信 launcher 以具体值替换，不能假设 dsh 或 shell 会替换 MCP 配置 `args` 中的字符串占位符。

虽然 adapter 已列出推荐等 L0 工具，dsh 首轮联调建议只启用 `validate_order` 与 `explain_print_term`；通过 parity 和安全门后再扩展推荐、预检、patch 和草稿流程。

`--allow-any-session` 仅供本地测试，生产 launcher 必须使用并校验固定的 `--session-id`。当前 adapter 兼容 Python 3.9+；dsh/client 的 Node 版本按锁定的 dsh 发布版本要求执行。

不依赖 dsh/Node/npm 的 stdio transcript smoke：

```text
python3 tools/dsh_mcp_smoke.py
```

它从临时 SQLite 启动 launcher，验证 `initialize`、`tools/list`、`explain_print_term` 和
`validate_order`，并确认 L0 不泄露 L1 工具。需要复用指定会话时传入
 `--memory-path /absolute/path/to/agent.sqlite3 --session-id <id>`。

需要同时演示 skill 发现、MCP smoke 和本地规则 Agent 时，可直接运行无第三方依赖的
fallback host：

```text
python3 tools/printops_local_host.py --session-id local-demo --smoke
python3 tools/printops_local_host.py --session-id local-demo --memory-path data/agent.sqlite3 \
  --message "做 500 张 A4 名片，250g铜版纸，双面四色，下周内"
```

它明确标记 `runtime: "python-stdlib"`，不冒充真实 dsh，也不会调用 DeepSeek 模型；省略
`--memory-path` 时使用临时 SQLite，会话结束后清理。

skill 的文字约束不承担权限隔离；MCP capability 才是执行边界。一个 stdio 进程只绑定一个会话，dsh 接管期间不要让 HTTP 路径并发写入同一 `sessionId`。

当前 adapter 已提供独立的 L1 `apply_order_patch` PoC。它只接受受限增量字段，要求绑定
`sessionId` 和 `expectedRevision`，并通过共享 planner 校验、严格 provenance 校验、SQLite
CAS、审计和 `patchId` 幂等保护写入；L0 默认不会暴露它。skill 产出的 `patch` 仍只是
建议，skill 不能直接写 SQLite。真实 dsh 进程联调、dsh on/off parity 和生产启用尚未完成，
因此首轮仍应先按只读解释/校验 smoke 验证，之后再逐步纳入 patch、推荐、预检和草稿流程。

任何要进入生产动作或触发高风险工具的 skill 输出，都必须先经过 PrintOps 的字段白名单、置信度、版本和人工确认门；受限 patch 只能经 L1 bridge 按 revision/CAS 规则提交并记录 provenance，未经确认不得生成或提交。skill 本身不直接修改会话状态。
