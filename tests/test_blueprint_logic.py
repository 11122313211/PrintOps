"""Unit tests for the interactive blueprint engine (web/js/blueprint.js) and
workcard Canvas schematic strip linkage (web/workcard.js).

Drives the native ES module directly via Node.js (stdlib-only unittest harness)
and skips cleanly if Node.js is unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT_JS = ROOT / "web" / "js" / "blueprint.js"
WORKCARD_JS = ROOT / "web" / "workcard.js"
NODE = shutil.which("node")


def _run_node_esm(script: str, payload: dict) -> dict:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    try:
        json.dump(payload, handle, ensure_ascii=False)
        handle.close()
        completed = subprocess.run(
            [NODE, "--input-type=module", "-e", script, str(BLUEPRINT_JS), str(WORKCARD_JS), handle.name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
    finally:
        Path(handle.name).unlink(missing_ok=True)
    if completed.returncode != 0:
        raise AssertionError(f"Node 执行失败：\nSTDOUT: {completed.stdout}\nSTDERR: {completed.stderr}")
    return json.loads(completed.stdout)


@unittest.skipIf(NODE is None, "未找到 node，跳过 blueprint.js 逻辑测试")
class BlueprintLogicTest(unittest.TestCase):
    def _eval_blueprint(self, calls: list[dict]) -> list:
        script = r"""
import fs from "node:fs";
import { pathToFileURL } from "node:url";

const mod = await import(pathToFileURL(process.argv[1]).href);
const { calls } = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const results = calls.map((c) => {
  const fn = mod[c.fn];
  if (typeof fn !== "function") throw new Error(`Missing export: ${c.fn}`);
  return fn(...(c.args || []));
});
process.stdout.write(JSON.stringify({ results }));
"""
        return _run_node_esm(script, {"calls": calls})["results"]

    def test_parse_size_mm_valid_and_invalid_boundaries(self):
        cases = [
            ("90×54 mm", [90, 54]),
            ("210x297", [210, 297]),
            ("120*80*60 mm", [120, 80, 60]),
            ("12×8×6 cm", [120, 80, 60]),
            ("10 in × 8 in", [254, 203.2]),
            ("1.2m × 0.8m", [1200, 800]),
            ("A4", [210, 297]),
            ("A4 (210×297 mm)", [210, 297]),
            ("长120×宽80×高60mm", [120, 80, 60]),
            ("大16开", [210, 285]),
            ({"width": 90, "height": 54, "unit": "mm"}, [90, 54]),
            ({"width": 12, "height": 8, "depth": 6, "unit": "cm"}, [120, 80, 60]),
            ({"length": 150.04, "width": 90.06, "height": 40, "unit": "mm"}, [150, 90.1, 40]),
            ([210.04, 297.06], [210, 297.1]),
            # 非法/异常边界
            ("", None),
            (None, None),
            ("210", None),
            ("10×20×30×40 mm", None),
            ("-90×54 mm", None),
            ("90×-54 mm", None),
            ("0×54 mm", None),
            ("999999×54 mm", None),
            ({"width": -10, "height": 54, "unit": "mm"}, None),
            ({"width": 0, "height": 54, "unit": "mm"}, None),
            ({"width": 100000, "height": 54, "unit": "mm"}, None),
        ]
        results = self._eval_blueprint([{"fn": "parseSizeMm", "args": [inp]} for inp, _ in cases])
        for (inp, expected), actual in zip(cases, results):
            with self.subTest(input=inp):
                self.assertEqual(actual, expected)

    def test_parse_paper_gsm_and_bulk_factors(self):
        cases = [
            ("157 g/m² 铜版纸", 157, 0.90, "铜版纸"),
            ("300g白卡纸", 300, 1.30, "白卡纸"),
            ("80克双胶纸", 80, 1.25, "双胶纸"),
            ("128g 哑粉纸", 128, 1.00, "哑粉纸"),
            ("157g 哑光铜版纸", 157, 1.00, "哑粉纸"),
            ("70g 轻型纸", 70, 1.55, "轻型纸"),
            ("100g 纯质纸", 100, 1.55, "纯质纸"),
            ("100g 道林纸", 100, 1.25, "道林纸"),
            ("120g 牛皮纸", 120, 1.30, "牛皮纸"),
            ("250g 艺术纸", 250, 1.30, "艺术纸"),
            ("120g 常规未标纸", 120, 1.05, "常规纸张"),
            ("铜版纸", None, 0.90, "铜版纸"),
            ("-100g 铜版纸", None, 0.90, "铜版纸"),
            ("", None, 1.05, "常规纸张"),
        ]
        results = self._eval_blueprint([{"fn": "parsePaperGsm", "args": [text]} for text, *_ in cases])
        for (text, exp_gsm, exp_factor, exp_label), actual in zip(cases, results):
            with self.subTest(paper=text):
                self.assertEqual(actual["gsm"], exp_gsm)
                self.assertAlmostEqual(actual["bulkFactor"], exp_factor, places=2)
                self.assertEqual(actual["kindLabel"], exp_label)

    def test_estimate_spine_mm_calculations_and_warnings(self):
        calls = [
            # 1. 标准胶装无封面纸：48P / 2 = 24 张；24 * 0.157 * 0.9 = 3.3912 -> 3.4 mm
            {"fn": "estimateSpineMm", "args": [{"pages": "48页", "paper": "157 g/m² 铜版纸", "binding": "胶装"}]},
            # 2. 胶装指定封面纸：减 4P 封面（30 张正文 + 2 张 250g 铜版封面）
            #    30 * 0.157 * 0.9 + 2 * 0.250 * 0.9 = 4.239 + 0.45 = 4.689 -> 4.7 mm
            {"fn": "estimateSpineMm", "args": [{"pages": "64P", "paper": "157g 铜版纸", "coverPaper": "250g 铜版纸", "binding": "胶装"}]},
            # 3. 骑马钉安全范围：32P 80克双胶纸 -> 16 * 0.08 * 1.25 = 1.6 mm，无预警
            {"fn": "estimateSpineMm", "args": [{"pages": 32, "paper": "80克双胶纸", "binding": "骑马钉"}]},
            # 4. 骑马钉页数 >= 48 触发预警
            {"fn": "estimateSpineMm", "args": [{"pages": "48页", "paper": "128g 铜版纸", "binding": "骑马钉"}]},
            # 5. 骑马钉厚度 >= 3.5mm（36P * 200g 哑粉纸 -> 18 * 0.2 * 1.0 = 3.6mm）触发预警
            {"fn": "estimateSpineMm", "args": [{"pages": 36, "paper": "200g 哑粉纸", "binding": "骑马钉"}]},
            # 6. 骑马钉非 4 的倍数触发提示
            {"fn": "estimateSpineMm", "args": [{"pages": "18页", "paper": "128g 铜版纸", "binding": "骑马钉"}]},
            # 7. 极薄 2P 下限钳制到 0.2mm
            {"fn": "estimateSpineMm", "args": [{"pages": 2, "paper": "60g 铜版纸", "binding": "胶装"}]},
            # 8. 缺少页数返回 available=False 但保留 disclaimer
            {"fn": "estimateSpineMm", "args": [{"pages": "", "paper": "157g 铜版纸", "binding": "胶装"}]},
        ]
        r = self._eval_blueprint(calls)

        self.assertTrue(r[0]["available"])
        self.assertEqual(r[0]["spineMm"], 3.4)
        self.assertEqual(r[0]["sheets"], 24)
        self.assertIsNone(r[0]["warning"])
        self.assertIn("印厂实纸打样为准", r[0]["disclaimer"])

        self.assertTrue(r[1]["available"])
        self.assertEqual(r[1]["spineMm"], 4.7)
        self.assertEqual(r[1]["innerSheets"], 30)
        self.assertEqual(r[1]["coverSheets"], 2)

        self.assertTrue(r[2]["available"])
        self.assertEqual(r[2]["spineMm"], 1.6)
        self.assertIsNone(r[2]["warning"])

        self.assertIn("骑马钉页数偏厚", r[3]["warning"])
        self.assertIn("胶装", r[3]["warning"])

        self.assertEqual(r[4]["spineMm"], 3.6)
        self.assertIn("骑马钉页数偏厚", r[4]["warning"])

        self.assertIn("4 的倍数", r[5]["warning"])

        self.assertEqual(r[6]["spineMm"], 0.2)

        self.assertFalse(r[7]["available"])
        self.assertIsNone(r[7]["spineMm"])
        self.assertIn("印厂实纸打样为准", r[7]["disclaimer"])

    def test_build_blueprint_model_all_product_kinds(self):
        calls = [
            # empty
            {"fn": "buildBlueprintModel", "args": [{}]},
            {"fn": "buildBlueprintModel", "args": [{"productType": "名片"}]},
            # flat + rounded corners
            {"fn": "buildBlueprintModel", "args": [{
                "productType": "名片",
                "size": "90×54 mm",
                "productSpecs": {"cardCorners": "圆角 R3"},
            }]},
            # folded tri-roll 297mm -> 100 + 100 + 97mm
            {"fn": "buildBlueprintModel", "args": [{
                "productType": "折页",
                "dimensions": {"expandedSize": "297×210 mm"},
                "productSpecs": {"folding": "三折"},
            }]},
            # folded accordion 300mm -> 100 + 100 + 100mm
            {"fn": "buildBlueprintModel", "args": [{
                "productType": "折页",
                "dimensions": {"expandedSize": "300×210 mm"},
                "productSpecs": {"folding": "风琴折"},
            }]},
            # folded bi-fold from finishedSize -> expanded inferred 296×210
            {"fn": "buildBlueprintModel", "args": [{
                "productType": "单页",
                "size": "148×210 mm",
                "productSpecs": {"folding": "二折"},
            }]},
            # booklet with spine + coverSpread
            {"fn": "buildBlueprintModel", "args": [{
                "productType": "画册",
                "size": "210×285 mm",
                "pages": "64P",
                "paper": "157g 铜版纸",
                "binding": "胶装",
                "productSpecs": {"coverPaper": "250g 铜版纸"},
            }]},
            # box with 3D + dieCut flatSizeMm
            {"fn": "buildBlueprintModel", "args": [{
                "productType": "包装盒",
                "dimensions": {"packageSize": "120×80×60 mm", "dieCutSize": "380×260 mm"},
                "productSpecs": {"boxStructure": "天地盖"},
            }]},
            # cup
            {"fn": "buildBlueprintModel", "args": [{
                "productType": "纸杯",
                "productSpecs": {"cupVolume": "350ml", "cupMaterial": "单 PE"},
            }]},
        ]
        m = self._eval_blueprint(calls)

        self.assertEqual(m[0]["kind"], "empty")
        self.assertEqual(m[1]["kind"], "empty")

        self.assertEqual(m[2]["kind"], "flat")
        self.assertEqual(m[2]["targetSizeMm"], [90, 54])
        self.assertTrue(m[2]["roundedCorners"]["enabled"])
        self.assertEqual(m[2]["roundedCorners"]["radiusMm"], 3)

        self.assertEqual(m[3]["kind"], "folded")
        self.assertEqual(m[3]["targetSizeMm"], [297, 210])
        self.assertEqual(m[3]["folding"]["panelWidthsMm"], [100, 100, 97])

        self.assertEqual(m[4]["kind"], "folded")
        self.assertEqual(m[4]["folding"]["panelWidthsMm"], [100, 100, 100])

        self.assertEqual(m[5]["kind"], "folded")
        self.assertEqual(m[5]["expandedSizeMm"], [296, 210])
        self.assertEqual(m[5]["folding"]["panelWidthsMm"], [148, 148])

        self.assertEqual(m[6]["kind"], "booklet")
        self.assertTrue(m[6]["spine"]["available"])
        self.assertEqual(m[6]["spine"]["spineMm"], 4.7)
        self.assertEqual(m[6]["coverSpreadMm"], [424.7, 285])

        self.assertEqual(m[7]["kind"], "box")
        self.assertEqual(m[7]["boxSizeMm"], [120, 80, 60])
        self.assertEqual(m[7]["flatSizeMm"], [380, 260])
        self.assertEqual(m[7]["targetSizeMm"], [380, 260])

        self.assertEqual(m[8]["kind"], "cup")
        self.assertEqual(m[8]["cup"]["volumeMl"], 350)

    def test_pdf_overlay_matching_rotation_bleed_and_mismatch(self):
        # 1 pt = 25.4 / 72 mm => mm * 72 / 25.4 pt
        def pt(mm: float) -> float:
            return round(mm * 72 / 25.4, 3)

        card_order = {"productType": "名片", "size": "90×54 mm"}
        fold_order = {
            "productType": "折页",
            "dimensions": {"finishedSize": "100×210 mm", "expandedSize": "297×210 mm"},
            "productSpecs": {"folding": "三折"},
        }
        box_only_3d = {"productType": "包装盒", "dimensions": {"packageSize": "120×80×60 mm"}}
        box_with_diecut = {
            "productType": "包装盒",
            "dimensions": {"packageSize": "120×80×60 mm", "dieCutSize": "380×260 mm"},
        }

        calls = [
            # 1. 精确匹配 + TrimBox/BleedBox 实测出血 3mm
            {"fn": "buildBlueprintModel", "args": [card_order, {
                "boxes": {
                    "trim": [pt(3), pt(3), pt(93), pt(57)],
                    "bleed": [0, 0, pt(96), pt(60)],
                    "media": [0, 0, pt(96), pt(60)],
                },
            }]},
            # 2. 旋转 90° 自动对齐匹配
            {"fn": "buildBlueprintModel", "args": [card_order, {
                "boxes": {"trim": [0, 0, pt(54), pt(90)]},
            }]},
            # 3. 容差边界：±1.5mm 通过，+1.6mm 不通过
            {"fn": "buildBlueprintModel", "args": [card_order, {"observedSizeMm": [91.5, 55.5]}]},
            {"fn": "buildBlueprintModel", "args": [card_order, {"observedSizeMm": [91.6, 54]}]},
            # 4. 仅 MediaBox 多出 6×6mm（疑似含 3mm 出血未写 TrimBox）
            {"fn": "buildBlueprintModel", "args": [card_order, {
                "boxes": {"media": [0, 0, pt(96), pt(60)]},
            }]},
            # 5. 折页优先比对展开尺寸（direct observedSizeMm + bleedMm）
            {"fn": "buildBlueprintModel", "args": [fold_order, {"observedSizeMm": [297, 210], "bleedMm": 3}]},
            # 6. 包装盒仅三维尺寸 -> info；有刀模尺寸 -> match
            {"fn": "buildBlueprintModel", "args": [box_only_3d, {"observedSizeMm": [380, 260]}]},
            {"fn": "buildBlueprintModel", "args": [box_with_diecut, {"observedSizeMm": [380, 260], "bleedMm": 3}]},
            # 7. 服务端 preflightResult.checks 回退解析 TrimBox 与 BleedBox
            {"fn": "buildBlueprintModel", "args": [card_order, {
                "checks": [
                    {"label": "裁切 TrimBox", "status": "ok", "detail": "约 90.0×54.0 mm"},
                    {"label": "出血 BleedBox", "status": "ok", "detail": "约 96.0×60.0 mm"},
                ],
            }]},
        ]
        models = self._eval_blueprint(calls)

        ov0 = models[0]["pdfOverlay"]
        self.assertEqual(ov0["status"], "match")
        self.assertTrue(ov0["matches"])
        self.assertEqual(ov0["deltaW"], 0)
        self.assertEqual(ov0["deltaH"], 0)
        self.assertEqual(ov0["measuredBleedMm"], 3)

        ov1 = models[1]["pdfOverlay"]
        self.assertEqual(ov1["status"], "match")
        self.assertTrue(ov1["rotated"])
        self.assertTrue(ov1["matches"])

        self.assertEqual(models[2]["pdfOverlay"]["status"], "match")
        self.assertEqual(models[3]["pdfOverlay"]["status"], "mismatch")

        ov4 = models[4]["pdfOverlay"]
        self.assertEqual(ov4["status"], "mismatch")
        self.assertEqual(ov4["deltaW"], 6)
        self.assertIn("TrimBox", ov4["summary"])

        ov5 = models[5]["pdfOverlay"]
        self.assertEqual(ov5["status"], "match")
        self.assertEqual(ov5["measuredBleedMm"], 3)

        self.assertEqual(models[6]["pdfOverlay"]["status"], "info")
        self.assertIsNone(models[6]["pdfOverlay"]["matches"])
        self.assertEqual(models[7]["pdfOverlay"]["status"], "match")
        self.assertEqual(models[8]["pdfOverlay"]["status"], "match")
        self.assertEqual(models[8]["pdfOverlay"]["measuredBleedMm"], 3)

    def test_blueprint_no_innerhtml_and_safe_dom_rendering(self):
        source = BLUEPRINT_JS.read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", source, "blueprint.js 严禁使用 innerHTML")
        self.assertNotIn("outerHTML", source, "blueprint.js 严禁使用 outerHTML")
        self.assertNotIn("insertAdjacentHTML", source, "blueprint.js 严禁使用 insertAdjacentHTML")

        # 使用最小 mock DOM 验证渲染、XSS 安全、图层开关持久化与空态隐藏
        script = r"""
import fs from "node:fs";
import { pathToFileURL } from "node:url";

class MockClassList {
  constructor(el) { this.el = el; this.set = new Set(); }
  add(...names) { names.forEach((n) => n && this.set.add(n)); this.sync(); }
  remove(...names) { names.forEach((n) => this.set.delete(n)); this.sync(); }
  toggle(name, force) {
    const on = force === undefined ? !this.set.has(name) : Boolean(force);
    if (on) this.set.add(name); else this.set.delete(name);
    this.sync();
    return on;
  }
  contains(name) { return this.set.has(name); }
  sync() { this.el._className = [...this.set].join(" "); }
}

class MockNode {
  constructor(tag, ns = null) {
    this.tagName = tag;
    this.ns = ns;
    this.attrs = {};
    this.dataset = {};
    this.children = [];
    this.listeners = {};
    this.hidden = false;
    this.classList = new MockClassList(this);
    this._text = "";
  }
  set className(v) { this.classList.set = new Set(String(v || "").split(/\s+/).filter(Boolean)); this.classList.sync(); }
  get className() { return this._className || ""; }
  set textContent(v) { this._text = String(v ?? ""); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "class") this.className = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  append(...nodes) {
    for (const n of nodes) {
      if (n && n.tagName === "#fragment") this.children.push(...n.children);
      else if (n != null) this.children.push(n);
    }
  }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  click() { (this.listeners.click || []).forEach((fn) => fn()); }
  querySelectorAll(sel) {
    const out = [];
    const attrMatch = sel.match(/^\[([^=\]]+)="([^"]+)"\]$/);
    const visit = (node) => {
      for (const child of node.children || []) {
        if (attrMatch && child.attrs?.[attrMatch[1]] === attrMatch[2]) out.push(child);
        if (sel.startsWith(".") && child.classList?.contains(sel.slice(1))) out.push(child);
        visit(child);
      }
    };
    visit(this);
    return out;
  }
}

globalThis.document = {
  createElement: (tag) => new MockNode(tag),
  createElementNS: (ns, tag) => new MockNode(tag, ns),
  createDocumentFragment: () => new MockNode("#fragment"),
  createTextNode: (t) => { const n = new MockNode("#text"); n._text = String(t); return n; },
};

const { renderBlueprintPanel } = await import(pathToFileURL(process.argv[1]).href);

const container = new MockNode("section");
// 1. 空态隐藏
const emptyModel = renderBlueprintPanel(container, {});
const emptyHidden = container.hidden;

// 2. 渲染含潜在 XSS 字符串的名片 + PDF 叠层
const payloadOrder = {
  productType: "名片",
  size: "90×54 mm",
  productSpecs: { cardCorners: "<img src=x onerror=alert(1)> 圆角 R3" },
};
const rendered = renderBlueprintPanel(
  container,
  payloadOrder,
  { observedSizeMm: [90, 54], bleedMm: 3 },
  { compact: true }
);
const buttons = container.querySelectorAll(".blueprint-layer-btn");
const bleedBtn = buttons.find((b) => b.dataset.layerToggle === "bleed");
const bleedBefore = container.querySelectorAll('[data-layer="bleed"]')[0].classList.contains("is-hidden");
bleedBtn.click();
const bleedAfter = container.querySelectorAll('[data-layer="bleed"]')[0].classList.contains("is-hidden");

// 重绘后图层开关保持关闭
renderBlueprintPanel(container, payloadOrder, { observedSizeMm: [90, 54], bleedMm: 3 }, { compact: true });
const bleedAfterRerender = container.querySelectorAll('[data-layer="bleed"]')[0].classList.contains("is-hidden");

// 3. 验证其余所有品类（折页 / 画册含书脊预警 / 包装盒含刀模 / 纸杯 / 圆形标签）均可无报错渲染 SVG
const modeFixtures = [
  { productType: "折页", dimensions: { expandedSize: "297×210 mm" }, productSpecs: { folding: "三折" } },
  { productType: "画册", size: "210×285 mm", pages: "48页", paper: "157g 铜版纸", binding: "骑马钉" },
  { productType: "包装盒", dimensions: { packageSize: "120×80×60 mm", dieCutSize: "380×260 mm" }, productSpecs: { boxStructure: "抽屉盒" } },
  { productType: "包装盒", dimensions: { packageSize: "120×80×60 mm" }, productSpecs: { boxStructure: "天地盖" } },
  { productType: "纸杯", productSpecs: { cupVolume: "500ml" } },
  { productType: "标签", size: "50×50 mm", productSpecs: { labelShape: "圆形" } },
  { productType: "吊牌", size: "60×90 mm", productSpecs: { hangHole: "圆孔" } },
];
const renderedKinds = modeFixtures.map((order) => {
  const host = new MockNode("div");
  const model = renderBlueprintPanel(host, order, { observedSizeMm: [297, 210] });
  const svgs = host.querySelectorAll(".blueprint-svg");
  if (svgs.length !== 1) throw new Error(`Expected 1 SVG for ${order.productType}, got ${svgs.length}`);
  return model.kind;
});

// 确认潜在 XSS 负载从未生成 <img> 元素
const hasImgTag = (() => {
  let found = false;
  const walk = (n) => {
    if (String(n.tagName || "").toLowerCase() === "img") found = true;
    (n.children || []).forEach(walk);
  };
  walk(container);
  return found;
})();

process.stdout.write(JSON.stringify({
  emptyKind: emptyModel.kind,
  emptyHidden,
  visibleHidden: container.hidden,
  compactClass: container.classList.contains("blueprint-panel--compact"),
  buttonToggles: buttons.map((b) => b.dataset.layerToggle),
  bleedBefore,
  bleedAfter,
  bleedAfterRerender,
  kind: rendered.kind,
  renderedKinds,
  hasImgTag,
}));
"""
        out = _run_node_esm(script, {})
        self.assertEqual(out["emptyKind"], "empty")
        self.assertTrue(out["emptyHidden"])
        self.assertFalse(out["visibleHidden"])
        self.assertTrue(out["compactClass"])
        self.assertEqual(out["buttonToggles"], ["bleed", "safe", "dims", "pdf"])
        self.assertFalse(out["bleedBefore"])
        self.assertTrue(out["bleedAfter"])
        self.assertTrue(out["bleedAfterRerender"])
        self.assertFalse(out["hasImgTag"])
        self.assertEqual(out["renderedKinds"], ["folded", "booklet", "box", "box", "cup", "flat", "flat"])

    def test_workcard_canvas_schematic_strip_height_and_draw_ops(self):
        """Verify PrintOpsWorkCard.renderCard draws schematic strips cleanly for all modes."""
        script = r"""
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);

class MockCtx {
  constructor() { this.ops = 0; this.dashes = []; this.texts = []; }
  scale() {}
  beginPath() {}
  closePath() {}
  moveTo() {}
  lineTo() {}
  arcTo() {}
  arc() {}
  fill() { this.ops++; }
  stroke() { this.ops++; }
  setLineDash(d) { this.dashes.push(d); }
  measureText(s) { return { width: String(s || "").length * 7 }; }
  fillText(t) { this.texts.push(String(t)); this.ops++; }
}

class MockCanvas {
  constructor() { this.width = 0; this.height = 0; this.attrs = {}; this.ctx = new MockCtx(); }
  getContext(type) { return type === "2d" ? this.ctx : null; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "class") this.className = v; }
}

globalThis.window = {
  document: {
    documentElement: {},
    createElement: (tag) => (tag === "canvas" ? new MockCanvas() : {}),
  },
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
};

const WorkCard = require(process.argv[2]);

function makeHandoff(items) {
  return {
    orderId: "PO-TEST-01",
    revision: 3,
    status: "confirmed",
    confirmedAt: "2026-11-27T10:00:00Z",
    confirmedRevision: 3,
    generatedAt: "2026-11-27T10:05:00Z",
    items,
    notes: ["本地交接测试"],
    issues: [],
  };
}

const noSizeCanvas = WorkCard.renderCard(makeHandoff([{
  itemId: "item-0",
  title: "名片",
  fields: [{ key: "productType", label: "印刷品", value: "名片", status: "confirmed" }],
  file: { status: "pending_delivery" },
}]), { scale: 1 });

const multiModeCanvas = WorkCard.renderCard(makeHandoff([
  {
    itemId: "item-1",
    title: "名片",
    fields: [
      { key: "productType", label: "印刷品", value: "名片", status: "confirmed" },
      { key: "size", label: "成品尺寸", value: "90×54 mm", status: "confirmed" },
      { key: "productSpecs.cardCorners", label: "圆角", value: "圆角 R3", status: "confirmed" },
    ],
    file: { status: "pending_delivery" },
  },
  {
    itemId: "item-2",
    title: "折页",
    fields: [
      { key: "productType", label: "印刷品", value: "折页", status: "confirmed" },
      { key: "dimensions.expandedSize", label: "展开尺寸", value: "297×210 mm", status: "confirmed" },
      { key: "productSpecs.folding", label: "折页方式", value: "三折", status: "confirmed" },
    ],
    file: { status: "pending_delivery" },
  },
  {
    itemId: "item-3",
    title: "画册",
    fields: [
      { key: "productType", label: "印刷品", value: "画册", status: "confirmed" },
      { key: "size", label: "成品尺寸", value: "210×285 mm", status: "confirmed" },
      { key: "pages", label: "页数", value: "48页", status: "confirmed" },
      { key: "paper", label: "纸张/材料", value: "157g 铜版纸", status: "confirmed" },
      { key: "binding", label: "装订/后道", value: "骑马钉", status: "confirmed" },
    ],
    file: { status: "pending_delivery" },
  },
  {
    itemId: "item-4",
    title: "包装盒",
    fields: [
      { key: "productType", label: "印刷品", value: "包装盒", status: "confirmed" },
      { key: "dimensions.packageSize", label: "包装三维尺寸", value: "120×80×60 mm", status: "confirmed" },
      { key: "dimensions.dieCutSize", label: "刀模尺寸", value: "380×260 mm", status: "confirmed" },
      { key: "productSpecs.boxStructure", label: "盒型结构", value: "天地盖", status: "confirmed" },
    ],
    file: { status: "pending_delivery" },
  },
]), { scale: 1 });

const singleWithSizeCanvas = WorkCard.renderCard(makeHandoff([{
  itemId: "item-0",
  title: "名片",
  fields: [
    { key: "productType", label: "印刷品", value: "名片", status: "confirmed" },
    { key: "size", label: "成品尺寸", value: "90×54 mm", status: "confirmed" },
  ],
  file: { status: "pending_delivery" },
}]), { scale: 1 });

process.stdout.write(JSON.stringify({
  exports: Object.keys(WorkCard).sort(),
  noSizeHeight: noSizeCanvas.height,
  singleWithSizeHeight: singleWithSizeCanvas.height,
  multiModeHeight: multiModeCanvas.height,
  multiTextsJoined: multiModeCanvas.ctx.texts.join(" | "),
  dashResetAtEnd: multiModeCanvas.ctx.dashes.length > 0 && multiModeCanvas.ctx.dashes[multiModeCanvas.ctx.dashes.length - 1].length === 0,
}));
"""
        out = _run_node_esm(script, {})
        self.assertEqual(out["exports"], ["cardFileName", "open", "renderCard"])
        self.assertGreater(out["singleWithSizeHeight"], out["noSizeHeight"])
        self.assertGreater(out["multiModeHeight"], out["singleWithSizeHeight"])
        self.assertTrue(out["dashResetAtEnd"])
        joined = out["multiTextsJoined"]
        self.assertIn("仅规格示意", joined)
        self.assertIn("100+100+97 mm", joined)
        self.assertIn("参考书脊", joined)
        self.assertIn("骑马钉页数偏厚", joined)
        self.assertIn("120×80×60 mm", joined)


if __name__ == "__main__":
    unittest.main()
