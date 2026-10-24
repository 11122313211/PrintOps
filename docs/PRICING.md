# 本地参考估算与离线校准

`pricing_engine.estimate_order()` 使用版本化 JSON 成本表，计算材料、准备、运行、表面工艺、装订后道和包装费用。它不请求网络，不读取供应商实时报价，不产生供应商提交。当前参数为人工构造的 **illustrative 示例数据**，不能据此推断市场价或盛大等平台的价格。

## 接口与金额

`tools.estimate_price()` 保留工具名称、`missing`、`pricingModelVersion` 和 `requiresHumanConfirmation`；`range` 从旧展示字符串升级为结构化金额，界面应使用 `displayRange`。每个 `recommend_processes()` 方案保留 `refPrice`，并增加完整 `estimate`。

```json
{
  "schemaVersion": "2.0",
  "type": "estimate",
  "status": "ready",
  "currency": "CNY",
  "range": {"low": "100.00", "mid": "100.00", "high": "125.00"},
  "displayRange": "¥100.00 - ¥125.00",
  "basis": {
    "kind": "local_reference",
    "rateCardId": "cn-local-reference",
    "version": "2026.11.01",
    "region": "CN-reference-only",
    "confidence": "illustrative",
    "isSupplierPrice": false
  }
}
```

内部使用 `Decimal`，金额经 `ROUND_HALF_UP` 保留两位小数，JSON 中使用字符串防止二进制浮点误差。`range.mid` 等于展示明细金额的总和；上下界是同一合计乘以示例不确定性区间。来源、录入人、有效期、区域、品类和置信等级随每条明细的 `rateSource` 返回。`basis` 还带 `taxBasis=exclusive`、`deliveryBasis=ex_works`。税费、运输、打样、文件修改、安装施工列入 `excludes`。

- `ready`：本品类所需驱动已覆盖，无未知工艺，返回完整区间。
- `partial`：已算出部分成本，存在未知工艺或缺少品类驱动。返回明细、`knownSubtotal`、`unpriced`，**不返回总价区间**。
- `blocked`：数量/计价尺寸/材料/面数等核心信息不足，品类未标定，或成本表未生效/过期。`range=null`。

`coverage` 按当前品类返回 `requiredDrivers`、`coveredDrivers`、`missingDrivers`、`unknownProcesses` 和 `ratio`；覆盖率不是价格准确率。`assumptions` 是字符串数组；比如未指定印刷方式时明确记录按合版模拟，未写表面工艺时明确记录按无表面工艺模拟。

## 计算规则

1. 数量必须是明确的正整数及品类支持的计数单位，不再按 500 隐式兜底。名片只接受“张”；输入“1200 份”返回 `quantity_unit_incompatible`，列出接受单位并要求确认实际计数，不把“份”猜成“张”。
2. 尺寸优先读取结构化 `measurements`，兼容 `dimensions` 和 `size` 展示串；字符串通过公共单位标准化器换成 mm。没有单位的自定义尺寸不能计价。包装盒必须提供展开/刀模平面尺寸，盒体长宽高不能替代材料展开面积；折页须有展开尺寸。
3. 纸张用量 = 单张面积 × 每份用纸张数 × 数量 × 损耗；质量 = 用量 × g/m² / 1000；材料成本 = kg × 每 kg 单价。按面积售卖的基材使用每 m² 单价。未知材质或未写克重的纸材不能套用泛化价格。
4. 书刊页数以**含封面总页数**计，必须是正偶数；骑马钉必须为 4 的倍数。每份用纸张数 = ceil(总页数 / 印刷面数)。封面另指定纸张时，4 页封面单独计价；否则明确记录同纸假设。
5. 印刷运行费按计印面积和面数计算；单色有明确折算。计价要求一种明确的面数与颜色，含否定、备选、白墨或额外印色的描述须先澄清；`productSpecs.cardColor` 中的专色或矛盾要求也会阻断，不能被顶层“四色”掩盖。开机准备按合版/专版/数码分别取值。
6. 每个表面工艺分别累加准备费 + 处理面积 × 单价，不取最高工艺系数。书刊按封面外侧 2 面，其他品类按 1 面全幅模拟，并把假设返回。未知工艺会产生 `partial`。
7. 骑马钉、胶装、锁线、糊盒、折页、模切、圆角各有准备费和计件费。包装盒计入模切+糊盒，标签计入模切；未标定结构/胶型/卷装返回 `partial`。
8. 最低收费为 `max(成本合计, 最低收费)`，明细只添加补差，不再重复加上整笔最低收费；不确定区间下界也不低于最低收费。
9. 同条件数量增加时，费用由固定项 + 非负线性项构成，最低收费取 max，总价不会在数量档位反向下降。当前没有数量折扣阶梯。

## 当前覆盖范围

| 类别 | 可计价条件 |
| --- | --- |
| 名片、单页、数码印刷、邀请函 | 平面尺寸、已标定材料、克重、明确单/双面和颜色 |
| 吊牌、PVC卡 | 平面尺寸和已标定材料（纸材按 g/m² 克重，PVC卡基按每 m² 卡材）；挂孔、打孔、编码等未列工艺一旦提及即转 partial |
| 宣传册、画册 | 另需总页数和已标定装订；可单独指定封面纸 |
| 折页 | 另需展开尺寸和二折/三折/风琴折 |
| 包装盒 | 展开/刀模尺寸；当前仅折叠盒/飞机盒的示例成本 |
| 信封封套、手提袋 | 展开/刀模尺寸和盒型（信封/封套、手提袋）；手提袋可另加穿绳后道 |
| 标签 | 已标定面材、方/矩/圆形、普通/永久胶、张装；其他胶型和卷装未标定 |
| 海报、喷画 | 已标定基材；仅画面/自行安装/墙面张贴等，不含施工 |
| PVC（展板）、联单、纸杯 | 独立标记未标定，返回 blocked，不能使用通用品类基价：展板缺板材厚度与裱板驱动，联单缺联数驱动（多联用纸无法按联数展开），纸杯为三维结构（见 1.4 规划） |

当前品类覆盖是开发模型能力声明，不是供应商能力声明。推荐方案的纸张和说明采用 g/m²，尺寸采用 mm；兼容输入的 A4、cm 和旧 `250g` 表达不能原样泄漏到推荐方案与计价依据中。

## 成本表管理

活动示例表在 `rate_cards/cn-local-reference.json`。金额参数均为字符串，Python 中不再维护单价。`product_knowledge.PRICE_MODEL` 只是读取 JSON 的兼容 facade。每个参数通过 `sourceId` 指向来源记录并继承表级区域/生效/到期/置信等级。加载时检验实际存在的来源及录入人、日期、单位、完整费率、非负参数、损耗不低于 1 和上下界顺序；过期或未生效版本阻断估算。本引擎只接受明确 `isSupplierPrice=false` 的 local_reference 成本表，且必须采用不含税、出厂口径，防止混入不同报价口径。`confidence` 收紧为单值枚举 `illustrative`：表级、来源记录与参数级出现任何其他取值都会被校验拒绝。

当前版本 `2026.11.01` 的参数由来源 `industry-standard-flow-2026-10` 按**通用工艺流程重推**：纸张按吨价假设取中换算（元/kg = 吨价 ÷ 1000；铜版纸 7000–8000 元/吨 → 7.5、哑粉纸 7500–8500 → 8、胶版纸 6000–7000 → 6.5、白卡纸 6500–7500 → 7、牛皮纸 6000–7000 → 6.5；PVC卡基按 0.76 mm × 密度 1.4 g/cm³ × 9000 元/吨 → 9.5 元/m²）；印刷按合版（拼版分摊，50 元 + 0.5 元/m²·面）、专版（单独制版，260 元 + 0.4 元/m²·面）、数码（无版费，30 元 + 1.5 元/m²·面）三档；表面工艺、装订后道与最低收费按市场通行量级重推，完整推导过程见 `docs/PRICING_WORKED_EXAMPLES.md`。上一版来源 `illustrative-cost-design-2026-10` 保留作历史沿革，活动参数不再引用它。

知识库与成本表的复核时效由 `product_knowledge.knowledge_review_status()`（周期 90 天，`tools/knowledge_review.py` 报告，dev_check 非 `--quick` 步骤）检查；差集、来源描述缺失或成本表不可读属于结构性问题，复核到期只作警告。

更新表需人工确认来源、明确递增版本，执行计价 golden、数量单调性和类别覆盖测试。禁止把示例参数改名为“实时市场价”或“供应商实价”；`industry-standard-flow` 同样是人工构造口径，只代表“市场通行量级”的工程假设，不是采价结果，任何时候都必须保持 `confidence: illustrative` 与 `isSupplierPrice: false`。

## 可执行的 CSV 校准导入

```bash
python3 tools/calibrate_pricing.py docs/examples/pricing-calibration.example.csv \
  --output data/calibration/2026-10-15-draft.json \
  --as-of 2026-10-15 --min-samples 5 --max-age-days 90
```

示例 CSV 也是人工构造数据，只验证导入链路。真实使用时仅导入有授权的脱敏报价。全部必需列如下：

```text
quote_id,source_id,source_description,quote_date,valid_until,
product,width_mm,height_mm,dimension_semantic,quantity,quantity_unit,
material,gsm,pages,printing,sides,print_mode,finishing,binding,product_specs,
region,lead_workdays,currency,total_amount,tax_basis,tax_rate,
delivery_basis,shipping_net_amount
```

字段不得留空。无工艺/装订写 `none`，非纸基材 `gsm=0`，单页类 `pages=0`，无专属参数 `product_specs={}`。后者是扁平 JSON，须保留结构、胶型、卷/张装等影响成本的条件。尺寸只接受明确 mm 数值及 `finished/expanded/die_cut` 语义，面数为 1/2，数量为正整数和张/份/本/个/块/套。报价时间不能在未来，过期或超过 `max-age-days` 的数据排除。重复 `quote_id` 拒绝，避免同一报价虚增样本。

税费口径：`tax_basis` 为 `inclusive/exclusive`；`tax_rate` 用 0~1 比例（13% 写 0.13）。交付口径：`delivery_basis` 为 `ex_works/delivered`；运费 `shipping_net_amount` 始终是**不含税**金额，出厂口径必须为 0。含税总价先除以 `(1+tax_rate)`，再扣净运费，归一为 CNY、不含税、出厂口径。

仅在品类、尺寸及语义、**精确数量**及单位、材料及克重、页数、面数/颜色/印刷方式、全部工艺、专属参数、地区和交期一致时合桶。暂不将不同数量强行合入同一阶梯，也不跨品类混合统计。每桶达到门槛（默认 5，最低 3）才输出 median/P25/P75；未达门槛为 `insufficient_samples`。有效期取桶内最早到期/历史最大年龄边界。

输出为 `pricing_calibration_draft`，`published=false`、状态 `review_required`，不是活动 rate card。任何无效行记录原因并使 CLI 退出码为 1；整体格式无效为 2；无错误为 0。CLI 拒绝写入活动 `rate_cards/` 目录，不会更新示例表或网络发布。人工复核授权与可比口径后，才能单独设计成本参数或历史样本参考表；总报价统计不会被自动反解成虚假的材料单价。

验证：`python3 -m unittest tests.test_pricing_engine -v`。
