# 路径六：多文件批量预检与文件工作流（path-06-file-workflow）

> 优先级 P1　目标版本 v0.13.0　预估工作量 3-4 人日　依赖：路径 2（前端模块化，`features/preflight.js` 挂点）

## 1. 现状与问题

### 1.1 当前行为

- PDF 预检是**浏览器本地**的轻量检查：前端读取文件的轻量元数据（页数、页面框、加密状态、可读性），结构化提交 `/api/preflight`，后端 `preflight_file` 工具结合订单项做规则校验（尺寸匹配、页数匹配、命名建议等），结果记入 `items[].uploadedFile`；
- 当前数据模型每订单项只有**单个** `uploadedFile` 字段（`uploadedFiles` 数组存在但使用薄）；
- 原稿永不上传——预检只传元数据（这是隐私卖点，必须保持）；
- 走查脚本步骤 5 已经覆盖"单文件 + 命名问题"场景。

### 1.2 真实场景差距

印刷订单的常态是**一个订单项多个文件**：画册的封面文件 + 内页文件、折页的正面/背面、包装盒的刀模 + 图案、同一物料的多个版本（v1/v2）。当前单文件模型迫使用户逐个上传且只保留最后一个，交接单也无法表达"这一项包含哪些文件"。刀模文件（v0.9.x 遗留的"刀模版本校验"议题）也没有承载结构。

## 2. 目标与非目标

### 2.1 目标

1. 每个订单项支持**多个文件**（列表），每个文件独立记录：文件名、大小、页数、页面框、加密/可读、预检结论、与订单项的绑定关系、添加时间；
2. 批量添加：一次拖拽/选择多个文件，逐个自动预检，失败不阻塞其他文件；
3. 文件角色标记：`artwork`（图案/内文）、`cover`（封面）、`diecut`（刀模）、`other`——刀模文件触发"刀模尺寸与订单刀模尺寸一致性"提示（衔接 v0.9.x 遗留项）；
4. 交接单与导出反映完整文件清单（文件名 + 角色 + 预检结论 + 命名建议），形成"交接文件清单"段落；
5. 文件状态机可视化：待预检 → 通过 / 有警告 / 不通过（含具体原因）。

### 2.2 非目标

- 不上传原稿到服务端（隐私边界不动摇；服务端只见元数据）；
- 不做真正的印前检查（仍明确"不能替代 Acrobat/PitStop"，README 已声明）；
- 不做云端存储/网盘集成。

## 3. 方案设计

### 3.1 数据模型（会话内，不新增 SQLite 表）

`items[]` 新增 `files` 数组，元素结构：

```json
{
  "fileId": "f-8位随机", "name": "画册封面_v2.pdf", "role": "cover",
  "sizeBytes": 2048000, "pageCount": 1, "pageBox": "A4",
  "encrypted": false, "readable": true,
  "status": "passed | warning | failed",
  "findings": ["文件名建议含成品尺寸或用途", "页面框 210×297mm 与订单 A4 一致"],
  "addedAt": "…"
}
```

- 旧 `uploadedFile` 字段保留一个版本：迁移时若非空，转为 `files[0]`（role=artwork）并在 `normalize_state`（`STATE_SCHEMA_VERSION` 3）中完成；`uploadedFile` 仍写出（兼容旧前端快照消费方）一个版本后移除；
- 每项文件数上限 10，单文件元数据大小上限 4KB，防滥用。

### 3.2 预检规则扩展（tools.py `preflight_file`）

- 入参从单文件改为 `(item_index, file_meta)`，内部规则复用；新增规则：
  - **角色一致性**：`diecut` 文件名包含"刀模/刀版"提示；订单未填 `dimensions.dieCutSize` 时提示先补刀模尺寸；
  - **版本命名**：同角色多文件时提示 v1/v2 命名与日期，避免供应商拿错版本；
  - **页数一致性**：`artwork` 页数与订单 `pages` 不一致时给警告（区分封面文件单页属正常）；
- 输出 `findings[]` 分级：`info/warning/block`，`block` 级（如加密不可读）计入该订单项校验不通过。

### 3.3 前端交互（落在 `features/preflight.js`）

- 订单项文件区：拖拽区（支持多选）+ 文件列表（角色下拉、状态徽标、findings 展开、删除）；
- 拖入 N 个文件：并行度 1 逐个走浏览器元数据提取 → 逐个 POST `/api/preflight`（带 `fileId` 与 `role`）→ 列表状态徽标实时更新；中途失败该文件标 failed，其余继续；
- 批量操作：全部通过时该项显示"文件就绪"；存在 block 级发现时订单项校验不通过（与既有"文件预检"阶段门禁一致）。

### 3.4 交接单与导出

`prepare_handoff` 输出新增"交接文件清单"段：

```
第 1 项（画册）文件 3 个：
  - 画册封面_v2.pdf（封面，预检通过）
  - 画册内文_32p.pdf（内文，预检警告：页数 32 与订单一致）
  - 画册刀模.cdr（刀模，预检通过；刀模尺寸未填写，请与供应商确认）
```

供应商导出（路径 3 的 `export_fields`）把 `files[]` 纳入字段映射（文件名/角色/预检结论三类字段）。

## 4. 接口与数据契约

- `/api/preflight` 请求体扩展：`{sessionId, itemIndex, file: {name, sizeBytes, pageCount, pageBox, encrypted, readable}, fileId?, role?}`——旧的单文件字段保留一个版本兼容；
- 响应新增 `files` 快照与 `fileId`；
- `STATE_SCHEMA_VERSION` 3：迁移 `uploadedFile → files`；
- 前端浏览器侧元数据提取函数抽出为纯函数（可被单测覆盖的 DOM 解析逻辑，Playwright 用例覆盖）。

## 5. 分阶段实施步骤

| 阶段 | 内容 | 验证 | 预估 |
| --- | --- | --- | --- |
| A | 数据模型 + 迁移（schema 4）+ `preflight_file` 多文件改造与三条新规则 | 单测：迁移、规则矩阵（角色/版本/页数）、上限 | 1 天 |
| B | 前端批量拖拽与文件列表 UI（features/preflight.js） | 手动矩阵：多文件、失败不阻塞、删除、角色切换 | 1.5 天 |
| C | 交接单清单段 + 导出字段 + 状态机徽标 | 单测：handoff 文本快照；手动确认链路 | 0.5 天 |
| D | Playwright 用例（拖拽单文件 + 多文件）+ 文档 | 冒烟 + CHANGELOG | 0.5 天 |

## 6. 测试与验收

- 单测：迁移（旧会话 uploadedFile → files）、新规则 6-8 例、上限与越界、handoff 快照；
- Playwright：选择 3 个文件 → 断言三条列表项与状态；其中 1 个加密文件 → failed 徽标 + 项级不通过；
- 走查脚本步骤 5 扩展为多文件版本（更新 RELEASE_CHECKLIST）；
- 验收演示：画册三项文件（封面/内文/刀模）完成预检并在交接单中呈现清单。

## 7. 风险与对策

| 风险 | 概率 | 对策 |
| --- | --- | --- |
| 浏览器 PDF 元数据提取对不同 PDF 版本行为不一 | 中 | 提取逻辑保持"尽力而为 + 明确失败"（encrypted/readable 字段已有语义）；提取失败不算预检不通过，标"无法读取，请人工确认" |
| 文件列表 UI 复杂度拖垮窄屏 | 中 | 窄屏折叠为"文件 3 个 ✓"摘要，展开再看列表 |
| 与路径 3 导出的字段映射膨胀 | 低 | 只导出名字/角色/结论三类原子字段，映射表按品类分层已有机制 |
| 迁移丢文件记录 | 低 | 迁移单测覆盖空/单文件/多文件三态；uploadedFile 保留一个版本双写 |

## 8. 工作量与验收门禁

3-4 人日。门禁：迁移测试 + 规则矩阵全绿、Playwright 批量用例通过、交接单快照评审通过、CHANGELOG 记录 schema 4 与接口变化。

## 9. 依赖与后续

- 依赖路径 2 的 preflight 模块边界；
- 为路径 3 的导出与远期"真实提交（含文件上传）"准备数据结构——届时上传的是"已通过预检的文件清单"，本路径的角色标记直接复用；
- 刀模版本校验遗留项在本路径的 diecut 角色中部分落地（尺寸一致性提示），完整"刀模文件版本记录"留待真实提交阶段；
- 实施记录：〔待填〕

## 10. 任务卡分解

| 卡号 | 阶段 | 任务 | 改动文件 | 验收标准 | 预估 |
| --- | --- | --- | --- | --- | --- |
| S6-01 | A | files[] 数据模型 + 上限（10 个/项、元数据 4KB） | order_model.py、agent.py | 上限越界单测；ITEM_DEFAULTS 扩展 | 0.25d |
| S6-02 | A | schema 3 迁移：uploadedFile → files[0]（role=artwork），旧字段双写一版本 | order_model.py（normalize_state） | 迁移单测：空/单/多文件三态 + 旧字段双写断言 | 0.25d |
| S6-03 | A | `preflight_file` 改造为 (item_index, file_meta) 入参 | tools.py、agent.py | 既有预检单测适配后全绿 | 0.5d |
| S6-04 | A | 三条新规则：diecut 角色识别与刀模尺寸提示、同角色版本命名提示、artwork 页数一致性警告 | tools.py | 规则矩阵单测 6-8 例 | 0.5d |
| S6-05 | A | findings 分级（info/warning/block）与项级校验联动（block 计入不通过） | tools.py、agent.py | block 联动单测；warning 不阻断 | 0.25d |
| S6-06 | B | 批量拖拽上传与列表 UI（并行度 1、逐个更新、失败不阻塞） | app/features/preflight.js、app/render/draft.js | 手动矩阵：3 文件含 1 失败 → 其余正常 | 1d |
| S6-07 | B | 角色下拉、状态徽标、findings 展开、删除（含确认） | 同上 | 交互走查；删除后重新添加正常 | 0.5d |
| S6-08 | C | 交接单"交接文件清单"段落 | tools.py（prepare_handoff） | handoff 快照单测（含刀模未填尺寸文案） | 0.25d |
| S6-09 | C | 窄屏折叠摘要（"文件 3 个 ✓"） | styles.css、app/render/draft.js | 420px/768px 截图走查 | 0.25d |
| S6-10 | D | Playwright：多文件拖拽 + 加密文件失败路径 | e2e/ | 用例全绿进 CI | 0.5d |

## 11. files[] 字段与 findings 分级规范

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| fileId | string | ✓ | `f-` + 8 位随机，项内唯一 |
| name | string | ✓ | 原文件名（长度截断 200） |
| role | enum | ✓ | artwork / cover / diecut / other |
| sizeBytes / pageCount / pageBox | number/string | 浏览器可提取则填 | 提取失败置 null，状态不定为 failed |
| encrypted / readable | bool | ✓ | 提取失败时 readable=false |
| status | enum | ✓ | passed / warning / failed（failed 仅限加密不可读与 block 级发现） |
| findings | array | — | `{level, text}`，text ≤120 字符 |
| addedAt | ISO 时间 | ✓ | — |

findings 分级语义：`info`（命名建议类，仅展示）；`warning`（需人工确认，不阻断项级校验）；`block`（加密不可读、尺寸明确冲突等，阻断项级校验与整体生成）。

## 12. 详细测试矩阵

| 层 | 用例组 | 断言要点 | 卡号 |
| --- | --- | --- | --- |
| 单测 | 迁移 | uploadedFile 非空 → files[0]；空 → files=[]；旧字段双写存在 | S6-02 |
| 单测 | 角色规则 | diecut 命名含"刀模"识别；订单无 dieCutSize → warning 文案精确 | S6-04 |
| 单测 | 版本规则 | 同角色两个文件名含 v1/v2 → info 建议日期命名 | S6-04 |
| 单测 | 页数规则 | artwork 页数 ≠ 订单 pages → warning；cover 单页不警告 | S6-04 |
| 单测 | block 联动 | 含 block finding 的项：itemValidations 状态 needs_input | S6-05 |
| 单测 | 上限 | 第 11 个文件拒绝（工具返回 blocked 语义） | S6-01 |
| 契约 | 快照 | 响应 items[].files 快照完整、fileId 稳定 | S6-03 |
| 契约 | handoff | 交接文本含"交接文件清单"段与三行文件 | S6-08 |
| Playwright | 批量 | 3 文件（1 加密）→ 两绿一红、项级不通过 | S6-10 |
| 手动 | 窄屏 | 420px 摘要态可展开 | S6-09 |

## 13. 多文件绑定状态机与预检规则分层

多文件预检（`files[]`）引入了比单文件复杂得多的生命周期。没有显式状态机，"文件删了
订单项还在引用""重复上传同一文件""换方案后预检结论过期"这类边界会以缺陷形式逐个
暴露。本节把状态机与规则分层定死，作为 S6 系列任务卡的实现依据。

### 13.1 单文件状态机

```
                ┌──────────┐  上传成功   ┌──────────┐  绑定到订单项  ┌──────────┐
  浏览器本地 ──▶│ previewed │──────────▶│ uploaded  │──────────────▶│ bound     │
                └──────────┘            └────┬─────┘                └────┬─────┘
                       │ 加密/不可读           │ 删除文件                 │ 所在项字段变更/
                       ▼                     ▼                          │ 订单修改/平台切换
                 ┌──────────┐          ┌──────────┐                     ▼
                 │ rejected  │          │ removed  │──────────────▶ stale（结论过期，
                 └──────────┘          └──────────┘                  需重新预检，保留历史）
```

状态定义与迁移规则：

| 状态 | 含义 | 允许的迁出 | 事件来源 |
| --- | --- | --- | --- |
| previewed | 仅浏览器解析了元数据，未提交 | uploaded、rejected | `/api/preflight` 前 |
| uploaded | 已登记到 `files[]`，未绑定项 | bound、removed | S6-03 |
| bound | `fileId` 与 `itemId` 建立绑定 | stale、removed | S6-04 |
| rejected | 加密/不可读/超上限，拒绝入库 | （终态，仅保留记录） | S6-01 |
| removed | 用户删除；条目保留 tombstone（fileId+时间+原因） | —— | S6-05 |
| stale | 绑定后所在项字段/平台/方案发生变化 | bound（重新预检后） | 复用 `_invalidate_delivery_state` 钩子 |

实现约束：

1. 迁移只允许发生在 `_apply_patch`/`upload`/显式删除三个入口，禁止 UI 直接改状态；
   状态机本体放在 `order_model.py`（`FILE_TRANSITIONS` 常量表 + `transition_file()`
   纯函数），非法迁移抛 `RequestError`——与尺寸归一化同样的"迁移集中一处"纪律。
2. `stale` 的判定复用 v0.11.0 询价失效的现有钩子（订单字段/文件/平台变化触发），
   不新建第二套失效机制，避免两套"过期"语义打架。
3. tombstone 保留 30 天后随会话清理一并回收（路径 4 的 LRU 清理钩子），防止
   `files[]` 无界增长。
4. 幂等：同一文件（SHA-256 前 16 位 + 字节数相同）重复上传返回原 `fileId` 并标记
   `duplicateOf`，不产生第二条记录。

### 13.2 预检规则分层（三级，禁止混层）

| 层 | 判定依据 | 示例 | 失败语义 |
| --- | --- | --- | --- |
| L1 结构层 | 浏览器必然可判：加密、页数、页面框、字节数 | 加密 PDF、0 页 | rejected，阻断入库 |
| L2 规范层 | 命名与文件组织约定：文件名含项号/版本号、含"印刷"字样、日期格式 | `未命名-1.pdf` | warning（findings 级别 warn），不阻断 |
| L3 印刷风险层 | product_knowledge 的品类风险规则：出血缺失、RGB 图像、细字最小号、折页压痕线 | 3mm 出血缺失 | warning 起步；规则库成熟后按品类升级为 block（须在知识版本变更记录中声明） |

分层理由：L1 是硬边界（工具无法继续）；L2/L3 是建议——把"命名不规范"当成阻断会
劝退真实用户。L3 的规则逐条来自 `product_knowledge.py` 的风险条目，每条带知识
版本，禁止在预检代码里硬编码具体印刷规则（保持"规则在知识层、执行在工具层"边界）。

### 13.3 交接文本的文件清单段

`prepare_handoff` 新增"交接文件清单"段：每个 bound/stale 文件一行，含
`文件名 · 绑定项 · 页数 · 预检结论（绿/黄/红） · fileState`；stale 文件必须带
"⚠ 结论过期"前缀。导出的 JSON/CSV/Markdown 三种格式同样包含该段（与路径 3 的
导出适配器共用同一序列化函数，禁止两份实现）。

### 13.4 验收

- 状态机纯函数全迁移覆盖测试（合法迁移绿、非法迁移抛错）；
- 重复上传幂等、tombstone 30 天回收各有单测；
- Playwright：上传 3 文件（1 加密）→ 两绿一红；修改绑定项字段 → 该文件转 stale 且
  交接文本出现"结论过期"前缀。
