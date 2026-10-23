# Security Policy / 安全策略

## TL;DR

PrintOps 是本地优先（local-first）的订单整理工具：HTTP API 只绑定回环地址，页面凭进程级随机令牌访问，
原稿在浏览器本地解析、不上传，供应商“询价/交接”只生成本地草稿——不会自动发送任何外部请求。

PrintOps is a local-first tool: the API binds to loopback only, the page authenticates with a per-process
token, artwork is parsed in the browser and never uploaded, and supplier interactions produce local drafts
that are never sent automatically.

## 报告漏洞 / Reporting a Vulnerability

请使用 GitHub 私密披露：[Report a vulnerability](https://github.com/11122313211/PrintOps/security/advisories/new)。
请勿直接开公开 issue。我们会先确认复现，再在修复或缓解方案就绪后协调披露。

## 当前本地安全模型（供复现参考）

- **回环绑定与同源检查**：仅监听 `127.0.0.1`；带 `Origin` 头的请求必须与 `Host` 同源。
- **进程随机访问令牌**：`X-PrintOps-Token`，页面自动注入、重启即失效；健康检查豁免。
- **请求边界**：1 MB 请求体、行级限制、总时限（408）、连接槽上限（过载 503 + Retry-After）。
- **模型是候选而非权威**：LLM 只能提出白名单字段补丁与受限工具调用，执行仍由本地代码决定；
  API Key 存 `data/llm_config.json`（gitignored、0600），不回显给前端。
- **MCP stdio 能力分级**：会话绑定、L0/L1 capability、参数校验与审计；launcher 拒绝 `--allow-any-session`。
- **SSRF 守卫**：模型 Base URL 默认拒绝私网/环回地址（`PRINTOPS_ALLOW_PRIVATE_LLM_HOSTS` 仅为显式调试）。
- **无静默外部副作用**：供应商提交必须人工确认；内置连接器默认 `verified=false`、`networkEnabled=false`。

## 在范围内 / In scope

认证绕过、令牌泄露、跨源滥用、路径穿越、经由模型配置的 SSRF、SQLite 数据破坏、MCP stdio 注入、
以及任何能让“本地草稿”在未经确认时外发的行为。

## 不在范围内 / Out of scope

- 示例价格与市场价不符（产品语义即 illustrative，见 README 能力矩阵）；
- 用户自行 `git add -f` 提交本地数据的风险（由 `tools/repo_guard.py` 与文档阻断）；
- 用户机器本身已被恶意软件控制的情形；
- 浏览器本地 PDF 预检覆盖不到的生产级印前缺陷（文档已声明非放行结论）。

## 相关工具

```bash
python3 tools/secret_scan.py   # 凭据泄漏扫描
python3 tools/repo_guard.py    # Git 运行态边界检查
python3 tools/dev_check.py     # 本机一键门禁
```
