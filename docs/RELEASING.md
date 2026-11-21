# 发布操作手册（RELEASING）

本文回答"**怎么**发布一个版本"；"**能不能**发布"由 [RELEASE_CHECKLIST](RELEASE_CHECKLIST.md) 判定——候选版允许组织性门槛（真实语料、真人走查、真实 dsh/模型）未闭合，但必须在 CHANGELOG 写明口径，不得把候选版描述成稳定版。

## 1. 收口版本文档

1. `VERSION` 文件 bump 到目标版本（候选版同样 bump，沿用 1.3.0 的 `chore(release): vX.Y.Z candidate` 模式）；
2. CHANGELOG 新增版本段落，逐条主线一句话带过并链接证据文档；Unreleased 段清空；
3. ROADMAP「当前阶段」节与顺延清单更新；README 的版本描述、能力矩阵与文档索引核对；
4. 债务登记册（`docs/plan/appendix-tech-debt.md`）过一遍：已偿条目迁入 B.5 趋势表，"待排期"超过三个版本的条目升级为活跃风险。

## 2. 发布门禁

本机与 CI 共用同一条门禁，发布前以本机全量为准：

```bash
python tools/dev_check.py            # 编译 + 单测 + 评测 + 知识复核 + secret_scan + repo_guard
npx playwright test --config tests/journey.config.mjs
npx playwright test --config tests/browser_smoke.config.mjs
npx playwright test --config tests/ui_positions.config.mjs
npx playwright test --config tests/ui_focus.config.mjs
```

发布加跑 [RELEASE_CHECKLIST](RELEASE_CHECKLIST.md)「发布前最终检查」的逐条命令（含 `--release-gate`；真实语料为 0 时它按预期失败，候选版记录该结果即可）。全部输出保存到 `reports/`（不入库），关键结论摘录入 CHANGELOG。

## 3. 打标签与推送

```bash
git status                 # 工作树必须干净
git tag -a vX.Y.Z -m "PrintOps vX.Y.Z（候选版/稳定版，口径见 CHANGELOG）"
git push origin main --tags
```

推送后观察 GitHub Actions 四组合（windows 3.9/3.14 + ubuntu 两组合）全绿；若 CI 复现本机未复现的失败，先修复再重打标签，不带着红 CI 出 Release。

## 4. GitHub Release 资产

用 `git archive` 生成源码包并附校验和（打包产物 pipx/zipapp 仍属 [path-08](plan/path-08-packaging.md) 顺延，当前只发源码包）：

```bash
git archive --format=zip --prefix=PrintOps-vX.Y.Z/ vX.Y.Z -o PrintOps-vX.Y.Z-source.zip
sha256sum PrintOps-vX.Y.Z-source.zip > SHA256SUMS.txt      # Windows Git Bash 自带 sha256sum
```

Release 说明直接引用 CHANGELOG 对应段落，逐字保留口径：示例估算不构成报价；订单不会被自动发送给供应商；真实语料/真人走查门槛状态如实标注。

## 5. 发布后

- 核对 Release 页资产与 SHA256SUMS 一致；
- 在债务登记册/ROADMAP 记录发布日期与下一个版本的目标主线；
- 不回改已发布 tag；发现问题走新提交 + 新 tag。
