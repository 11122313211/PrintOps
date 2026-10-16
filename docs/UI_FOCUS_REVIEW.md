# 入口状态与键盘交互微调验收

日期：2026-10-16。承接 [UI 位置计划](UI_POSITION_INTERACTION_PLAN.md) 和[第一批位置微调验收](UI_POSITION_REVIEW.md)。本批依据实际使用发现，补齐入口的状态表达和键盘焦点，不重排页面、不增加工作阶段。

## 已修复的问题

| 问题 | 局部调整 | 验收结果 |
| --- | --- | --- |
| 手机订单栏隐藏时，按钮仍叫“收起订单栏” | 根据桌面收栏偏好或手机实际 pane，同步名称、title、展开状态；两侧按钮关联现有栏位 ID | 打开、返回、切换窗口宽度后，状态与真实显示一致，保留桌面收栏偏好 |
| “前往复核”展示交接内容后，焦点留在已经变成“确认”的原按钮上 | 用户导航时使用现有 `view(..., true)`，将焦点移到目标内容区 | 下一 Tab 进入核对内容；导航无确认、无下载、无订单版本变化，返回保留未发草稿 |
| 键盘选择方案后，重建按钮导致焦点掉到 BODY | 成功响应仍属当前订单和比较页、且用户未移往其它控件时，恢复到现有比较区域 | 选择成功后可继续键盘操作；延迟响应不抢用户主动移动的焦点或视图 |
| 左栏把闭合 details 内的隐藏控件算进 Tab 循环 | 在原过滤中排除闭合 details 的不可见内容，保留其 summary | 前后两个边界均在可见左栏循环；原生新建 dialog 仍优先处理 Escape |
| WebKit 平台下拉框真实高度为 21 px | 为原生 select 显式设置 44 px 高度 | 两浏览器、两种宽度的实际边框盒均为 44 px，保留原生控件 |

跨窗口测试还发现 WebKit 的 resize 发生时，媒体查询可能尚未更新。最终修正仅让原处理函数同时监听媒体查询 change，避免页面已切换而按钮语义仍为旧状态。

产品文件为 `index.html`、`styles.css`、`app.js`、`journey.js`。本批 `journey.js` 的变化仅为现有用户导航启用焦点参数；生成、确认、导出条件与回调不变。没有改动后端、订单字段、计价、存储、平台授权或接口请求协议。

## 验证与版本边界

测试使用临时 SQLite、合成订单和隔离服务；用户 4174 订单没有参与测试。两浏览器为 Chromium 141.0.7390.37、WebKit 26；主要宽度 390、1440 px。

| 检查 | Chromium | WebKit | 证据 |
| --- | --- | --- | --- |
| 完整业务交互 journey | 34/34 | 34/34 | [Chromium](../reports/ui-position-tuning/focus-followup/journey-chromium.json)、[WebKit](../reports/ui-position-tuning/focus-followup/journey-webkit.json) |
| 新增焦点与入口专项，第一版修复后 | 14/14 | 12/14 | [Chromium](../reports/ui-position-tuning/focus-followup/chromium/verification/report.json)、[WebKit](../reports/ui-position-tuning/focus-followup/webkit/verification/report.json) |
| 最终媒体查询监听修正后，仅复验跨断点 | 2/2 | 2/2 | [Chromium](../reports/ui-position-tuning/focus-followup/chromium/breakpoint-verification/report.json)、[WebKit](../reports/ui-position-tuning/focus-followup/webkit/breakpoint-verification/report.json) |

新增专项的 14 个用例在两种浏览器中均已有通过证据；WebKit 最初失败的两个跨断点用例经修复后分别复验通过。完整 journey 与其余专项执行早于最后的媒体查询监听修补；最后差异由两浏览器共四项定向用例和独立跨断点审查覆盖。不能将这些报告描述为全部在同一个最终源码哈希下运行。

最终截图：[桌面平台选择聚焦](../reports/ui-position-tuning/focus-followup/final/screenshots/chromium-platform-focused-1440.png)、[手机订单入口](../reports/ui-position-tuning/focus-followup/final/screenshots/webkit-mobile-order-390.png)。截图使用合成空单及测试中的栏位偏好，不代表用户现有订单。

专项验证真实选择、真实生成后的导航、实际控件尺寸和双向 Tab；延迟选择测试调用真实 API，只暂缓响应交付，不注入订单状态。初始失败基线、各版源码和报告均保留。最终统计、哈希及差异见 [verification.json](../reports/ui-position-tuning/focus-followup/final/verification.json)。

独立审查确认上述路径通过，包括两浏览器共 36 个跨断点展示状态、桌面收栏偏好保留，以及八组延迟响应不抢焦点。详见[焦点独立 review](../reports/ui-position-tuning/focus-review.md) 与[入口尺寸审查](../reports/ui-position-tuning/accessibility-entry-review.md)。这些结果不代表真实辅助技术或真人找入口测试已完成。

## 复现与小批回退

```sh
npx playwright test --config tests/ui_focus.config.mjs
PRINTOPS_TEST_BROWSER=webkit npx playwright test --config tests/ui_focus.config.mjs
```

上述默认命令会重跑完整 14 项专项并覆盖默认 verification 目录；需要保留一次新运行时，设置独立的 `PRINTOPS_FOCUS_RUN` 名称。媒体查询专项可追加 `--grep 'order disclosure'`。

本批基线在 `reports/ui-position-tuning/focus-followup/baseline/`，最终源码与逐文件 patch 在同目录的 `final/`。只对本批差异做回退，先确认之后没有新修改，不恢复整个 Git 工作区。上一批 `reports/ui-position-tuning/final/` 保持不覆盖。

## 尚待完成的使用验证

原生 Safari 已通过网站设置实际选择 200% 缩放，并在补丁前完成“到达输入区→保留未发草稿→打开订单栏→查看方案→返回对话”的真实操作。最终补丁后重新加载时，本机锁屏阻断了原生操作，因此最终版本的 200% 复验、恢复测试站点 100% 缩放及关闭临时标签页仍待解锁后完成。记录见[原生 Safari 走查](../reports/ui-position-tuning/focus-followup/native-safari-zoom.md)。

真实手机软键盘、系统中文选词、辅助技术和 5 名新用户入口走查仍未执行。后续位置调整继续由实际漏找、误触或遮挡记录驱动；C2 任务条重排保持候选。既有至少 20 条真实脱敏订单的发布门槛不由这些合成测试替代。
