"""Deep PDF object-stream parsing (Node.js native ESM) and server-side preflight checks."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import unittest
import zlib
from pathlib import Path

from printops.tools.registry import TOOL_SCHEMAS, preflight_file

ROOT = Path(__file__).resolve().parents[1]
PARSER_PATH = ROOT / "web" / "js" / "pdf_parser.js"
MM_TO_PT = 72.0 / 25.4


def _mm_box(x0_mm: float, y0_mm: float, x1_mm: float, y1_mm: float) -> str:
    return (
        f"[{x0_mm * MM_TO_PT:.3f} {y0_mm * MM_TO_PT:.3f} "
        f"{x1_mm * MM_TO_PT:.3f} {y1_mm * MM_TO_PT:.3f}]"
    )


def _build_pdf_with_objstm(
    compressed_objects: list[tuple[int, str]],
    raw_objects: list[tuple[int, str]] | None = None,
    extra_streams: list[tuple[int, str, bytes]] | None = None,
    version: str = "1.7",
    include_eof: bool = True,
    trailer_extra: str = "",
) -> bytes:
    """Construct a valid PDF 1.5+ byte stream with real FlateDecode /Type /ObjStm streams."""
    chunks: list[bytes] = [f"%PDF-{version}\n%\xe2\xe3\xcf\xd3\n".encode("latin1")]

    for obj_num, body in (raw_objects or []):
        chunks.append(f"{obj_num} 0 obj\n{body}\nendobj\n".encode("latin1"))

    if compressed_objects:
        # Build ISO 32000 7.5.7 ObjStm index header + object payloads
        offsets: list[tuple[int, int]] = []
        body_parts: list[bytes] = []
        cursor = 0
        for obj_num, obj_body in compressed_objects:
            encoded = (obj_body.strip() + "\n").encode("latin1")
            offsets.append((obj_num, cursor))
            body_parts.append(encoded)
            cursor += len(encoded)
        header_str = " ".join(f"{num} {off}" for num, off in offsets) + "\n"
        header_bytes = header_str.encode("latin1")
        uncompressed = header_bytes + b"".join(body_parts)
        compressed = zlib.compress(uncompressed)
        objstm_num = 900
        chunks.append(
            (
                f"{objstm_num} 0 obj\n"
                f"<< /Type /ObjStm /N {len(compressed_objects)} "
                f"/First {len(header_bytes)} /Filter /FlateDecode "
                f"/Length {len(compressed)} >>\nstream\n"
            ).encode("latin1")
            + compressed
            + b"\nendstream\nendobj\n"
        )

    for obj_num, dict_str, stream_bytes in (extra_streams or []):
        chunks.append(
            f"{obj_num} 0 obj\n{dict_str}\nstream\n".encode("latin1")
            + stream_bytes
            + b"\nendstream\nendobj\n"
        )

    chunks.append(f"trailer\n<< /Root 1 0 R {trailer_extra} >>\n".encode("latin1"))
    if include_eof:
        chunks.append(b"%%EOF\n")
    return b"".join(chunks)


def _run_js_parser(pdf_bytes: bytes) -> tuple[dict, int]:
    """Invoke web/js/pdf_parser.js via Node.js and return (parsed_dict, json_byte_length)."""
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("Node.js is required to test web/js/pdf_parser.js")
    parser_url = PARSER_PATH.resolve().as_uri()
    script = (
        f"import {{ inspectPdfBytes }} from {json.dumps(parser_url)};\n"
        "const chunks = [];\n"
        "for await (const c of process.stdin) chunks.push(c);\n"
        "const b64 = Buffer.concat(chunks).toString('utf8').trim();\n"
        "const bytes = new Uint8Array(Buffer.from(b64, 'base64'));\n"
        "const res = await inspectPdfBytes(bytes);\n"
        "process.stdout.write(JSON.stringify(res));\n"
    )
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        input=base64.b64encode(pdf_bytes).decode("ascii"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"Node parser failed (rc={proc.returncode}): {proc.stderr}")
    raw_json = proc.stdout
    return json.loads(raw_json), len(raw_json.encode("utf-8"))


class BrowserPdfParserTest(unittest.TestCase):
    def test_compressed_objstm_multipage_with_3mm_bleed(self) -> None:
        media_bleed = _mm_box(0, 0, 216, 303)
        trim = _mm_box(3, 3, 213, 300)
        compressed = [
            (2, "<< /Type /Pages /Count 4 /Kids [3 0 R 4 0 R 5 0 R 6 0 R] >>"),
            (3, f"<< /Type /Page /Parent 2 0 R /MediaBox {media_bleed} /TrimBox {trim} /BleedBox {media_bleed} >>"),
            (4, f"<< /Type /Page /Parent 2 0 R /MediaBox {media_bleed} /TrimBox {trim} /BleedBox {media_bleed} >>"),
            (5, f"<< /Type /Page /Parent 2 0 R /MediaBox {media_bleed} /TrimBox {trim} /BleedBox {media_bleed} >>"),
            (6, f"<< /Type /Page /Parent 2 0 R /MediaBox {media_bleed} /TrimBox {trim} /BleedBox {media_bleed} >>"),
            (7, "<< /Type /FontDescriptor /FontName /SimHei /FontFile2 8 0 R >>"),
            (9, "<< /Type /XObject /Subtype /Image /Width 2480 /Height 3508 /ColorSpace /DeviceCMYK >>"),
            # Tiny decorative masks must NOT trigger low-res suspects.
            (10, "<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray >>"),
            (11, "<< /Type /XObject /Subtype /Image /Width 16 /Height 16 /ColorSpace /DeviceGray >>"),
        ]
        pdf_bytes = _build_pdf_with_objstm(
            compressed_objects=compressed,
            raw_objects=[(1, "<< /Type /Catalog /Pages 2 0 R >>")],
        )

        inspected, json_size = _run_js_parser(pdf_bytes)
        self.assertLess(json_size, 8192)
        self.assertTrue(inspected["readable"])
        self.assertTrue(inspected["isPdf"])
        self.assertEqual(inspected["pdfVersion"], "1.7")
        self.assertTrue(inspected["hasEof"])
        self.assertFalse(inspected["encrypted"])
        self.assertEqual(inspected["pageCount"], 4)
        self.assertGreaterEqual(inspected["compressedStreams"]["detected"], 1)
        self.assertGreaterEqual(inspected["compressedStreams"]["decoded"], 1)
        self.assertEqual(inspected["compressedStreams"]["objStmCount"], 1)
        self.assertEqual(inspected["pageSummary"]["analyzedPages"], 4)
        self.assertTrue(inspected["pageSummary"]["uniformSize"])
        self.assertFalse(inspected["pageSummary"]["mixedOrientation"])
        self.assertEqual(inspected["pageSummary"]["mismatchPages"], [])
        self.assertEqual(inspected["bleedAnalysis"]["source"], "bleed_vs_trim")
        self.assertAlmostEqual(inspected["bleedAnalysis"]["minMm"], 3.0, delta=0.1)
        self.assertEqual(inspected["colorSpaces"], ["DeviceCMYK"])
        self.assertEqual(inspected["fontEmbedding"], "embedded")
        self.assertEqual(inspected["imageSummary"]["lowResSuspects"], [])

        # End-to-end verification against server preflight_file
        result = preflight_file(
            "brochure.pdf", len(pdf_bytes), page_count=inspected["pageCount"],
            encrypted=inspected["encrypted"], readable=inspected["readable"],
            inspection=inspected, expected_size="A4", expected_pages="4页", product_type="宣传册",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["expectedPagesMatch"])
        self.assertAlmostEqual(result["measuredBleedMm"], 3.0, delta=0.1)
        self.assertEqual(result["observedSizeMm"], [210.0, 297.0])
        self.assertTrue(any(c["label"] == "PDF 对象流" and c["status"] == "ok" for c in result["checks"]))
        self.assertTrue(any(c["label"] == "实测单边出血" and c["status"] == "ok" for c in result["checks"]))
        self.assertTrue(any(c["label"] == "逐页尺寸一致性" and c["status"] == "ok" for c in result["checks"]))

    def test_short_1mm_bleed_from_media_vs_trim_in_objstm(self) -> None:
        media = _mm_box(0, 0, 212, 299)
        trim = _mm_box(1, 1, 211, 298)
        compressed = [
            (2, "<< /Type /Pages /Count 2 /Kids [3 0 R 4 0 R] >>"),
            (3, f"<< /Type /Page /Parent 2 0 R /MediaBox {media} /TrimBox {trim} >>"),
            (4, f"<< /Type /Page /Parent 2 0 R /MediaBox {media} /TrimBox {trim} >>"),
        ]
        pdf_bytes = _build_pdf_with_objstm(compressed, [(1, "<< /Type /Catalog /Pages 2 0 R >>")])
        inspected, json_size = _run_js_parser(pdf_bytes)
        self.assertLess(json_size, 8192)
        self.assertEqual(inspected["bleedAnalysis"]["source"], "media_vs_trim")
        self.assertAlmostEqual(inspected["bleedAnalysis"]["minMm"], 1.0, delta=0.1)

        result = preflight_file(
            "flyer.pdf", len(pdf_bytes), page_count=inspected["pageCount"],
            inspection=inspected, expected_size="A4",
        )
        self.assertTrue(any("实测单边出血仅约 1.0 mm" in w for w in result["warnings"]))
        self.assertAlmostEqual(result["measuredBleedMm"], 1.0, delta=0.1)

    def test_mixed_page_sizes_and_orientation_with_rotation(self) -> None:
        a4 = _mm_box(0, 0, 210, 297)
        a5 = _mm_box(0, 0, 148, 210)
        b5_land = _mm_box(0, 0, 250, 176)
        compressed = [
            (2, "<< /Type /Pages /Count 4 /Kids [3 0 R 4 0 R 5 0 R 6 0 R] >>"),
            (3, f"<< /Type /Page /Parent 2 0 R /MediaBox {a4} /TrimBox {a4} /Rotate 0 >>"),
            # Page 2 has A4 box rotated 90 degrees -> logical landscape 297x210
            (4, f"<< /Type /Page /Parent 2 0 R /MediaBox {a4} /TrimBox {a4} /Rotate 90 >>"),
            # Page 3 is A5 portrait -> size mismatch
            (5, f"<< /Type /Page /Parent 2 0 R /MediaBox {a5} /TrimBox {a5} >>"),
            # Page 4 is B5 landscape -> size & orientation mismatch
            (6, f"<< /Type /Page /Parent 2 0 R /MediaBox {b5_land} /TrimBox {b5_land} >>"),
        ]
        pdf_bytes = _build_pdf_with_objstm(compressed, [(1, "<< /Type /Catalog /Pages 2 0 R >>")])
        inspected, json_size = _run_js_parser(pdf_bytes)
        self.assertLess(json_size, 8192)
        self.assertEqual(inspected["pageCount"], 4)
        summary = inspected["pageSummary"]
        self.assertEqual(summary["analyzedPages"], 4)
        self.assertFalse(summary["uniformSize"])
        self.assertTrue(summary["mixedOrientation"])
        self.assertEqual(summary["mismatchPages"], [2, 3, 4])
        # Verify Rotate=90 produced a landscape 297x210 variant
        self.assertTrue(
            any(
                abs(v["widthMm"] - 297.0) <= 1.0 and abs(v["heightMm"] - 210.0) <= 1.0 and v["orientation"] == "landscape"
                for v in summary["variants"]
            )
        )

        result = preflight_file("mixed.pdf", len(pdf_bytes), page_count=4, inspection=inspected)
        self.assertTrue(any("多页尺寸不一致" in w and "2、3、4" in w for w in result["warnings"]))
        self.assertTrue(any("横版与竖版页面混排" in w for w in result["warnings"]))
        self.assertEqual(
            result["pageConsistency"],
            {"uniformSize": False, "mixedOrientation": True, "mismatchPages": [2, 3, 4]},
        )

    def test_low_res_images_detected_while_masks_ignored(self) -> None:
        a4 = _mm_box(0, 0, 210, 297)
        compressed = [
            (2, "<< /Type /Pages /Count 1 /Kids [3 0 R] >>"),
            (3, f"<< /Type /Page /Parent 2 0 R /MediaBox {a4} /TrimBox {a4} >>"),
            (4, "<< /Type /XObject /Subtype /Image /Width 200 /Height 150 /ColorSpace /DeviceRGB >>"),
            (5, "<< /Type /XObject /Subtype /Image /Width 640 /Height 480 /ColorSpace /DeviceRGB >>"),
            (6, "<< /Type /XObject /Subtype /Image /Width 2500 /Height 3500 /ColorSpace /DeviceCMYK >>"),
            (7, "<< /Type /XObject /Subtype /Image /Width 16 /Height 16 /ColorSpace /DeviceGray >>"),
            (8, "<< /Type /XObject /Subtype /Image /Width 120 /Height 120 /ImageMask true >>"),
        ]
        pdf_bytes = _build_pdf_with_objstm(compressed, [(1, "<< /Type /Catalog /Pages 2 0 R >>")])
        inspected, json_size = _run_js_parser(pdf_bytes)
        self.assertLess(json_size, 8192)
        img = inspected["imageSummary"]
        self.assertEqual(img["count"], 5)
        self.assertEqual(img["lowResCount"], 2)
        self.assertEqual(len(img["lowResSuspects"]), 2)
        dims = {(item["widthPx"], item["heightPx"]) for item in img["lowResSuspects"]}
        self.assertEqual(dims, {(200, 150), (640, 480)})

        result = preflight_file("poster.pdf", len(pdf_bytes), page_count=1, inspection=inspected)
        self.assertTrue(any("2 处疑似低分辨率图片" in w and "200×150" in w for w in result["warnings"]))
        self.assertTrue(any(c["label"] == "图片分辨率线索" and c["status"] == "warn" for c in result["checks"]))

    def test_corrupt_stream_and_bounded_json_size_under_stress(self) -> None:
        # 20 pages across 10 distinct sizes + 10 low-res images + 1 corrupted FlateDecode stream
        compressed = [(2, "<< /Type /Pages /Count 20 /Kids [" + " ".join(f"{i} 0 R" for i in range(10, 30)) + "] >>")]
        for idx in range(20):
            w = 100 + (idx % 10) * 15
            h = 140 + (idx % 10) * 15
            box = _mm_box(0, 0, w, h)
            compressed.append((10 + idx, f"<< /Type /Page /Parent 2 0 R /MediaBox {box} /TrimBox {box} >>"))
        for idx in range(10):
            compressed.append((50 + idx, f"<< /Type /XObject /Subtype /Image /Width {100 + idx * 10} /Height {80 + idx * 10} >>"))

        corrupt_stream = (888, "<< /Type /ObjStm /N 1 /First 5 /Filter /FlateDecode /Length 16 >>", b"\x78\x9c\xff\x00\xde\xad\xbe\xef" * 2)
        pdf_bytes = _build_pdf_with_objstm(
            compressed_objects=compressed,
            raw_objects=[(1, "<< /Type /Catalog /Pages 2 0 R >>")],
            extra_streams=[corrupt_stream],
        )

        inspected, json_size = _run_js_parser(pdf_bytes)
        self.assertTrue(inspected["readable"])
        self.assertEqual(inspected["pageCount"], 20)
        self.assertLessEqual(len(inspected["pageSummary"]["variants"]), 6)
        self.assertLessEqual(len(inspected["pageSummary"]["mismatchPages"]), 8)
        self.assertLessEqual(len(inspected["imageSummary"]["lowResSuspects"]), 4)
        self.assertLess(json_size, 8192)

    def test_array_filter_deflate_raw_inherited_mediabox_and_non_pdf(self) -> None:
        # Build an ObjStm using raw deflate (wbits=-15) and array /Filter [/FlateDecode]
        a4 = _mm_box(0, 0, 210, 297)
        obj_body_1 = f"<< /Type /Pages /Count 2 /Kids [3 0 R 4 0 R] /MediaBox {a4} >>\n"
        obj_body_2 = "<< /Type /Page /Parent 2 0 R >>\n"
        obj_body_3 = "<< /Type /Page /Parent 2 0 R >>\n"
        header = f"2 0 3 {len(obj_body_1)} 4 {len(obj_body_1) + len(obj_body_2)}\n"
        payload = (header + obj_body_1 + obj_body_2 + obj_body_3).encode("latin1")
        comp = zlib.compressobj(wbits=-15)
        raw_deflate = comp.compress(payload) + comp.flush()

        pdf_bytes = _build_pdf_with_objstm(
            compressed_objects=[],
            raw_objects=[(1, "<< /Type /Catalog /Pages 2 0 R >>")],
            extra_streams=[(
                20,
                f"<< /Type /ObjStm /N 3 /First {len(header)} /Filter [ /FlateDecode ] /Length {len(raw_deflate)} >>",
                raw_deflate,
            )],
        )
        inspected, json_size = _run_js_parser(pdf_bytes)
        self.assertLess(json_size, 8192)
        self.assertEqual(inspected["pageCount"], 2)
        self.assertEqual(inspected["compressedStreams"]["decoded"], 1)
        self.assertEqual(inspected["pageSummary"]["analyzedPages"], 2)
        self.assertTrue(inspected["pageSummary"]["uniformSize"])
        self.assertIsNotNone(inspected["boxes"]["media"])

        # Non-PDF bytes stay safe and mark isPdf=False
        non_pdf, _ = _run_js_parser(b"hello world not a pdf")
        self.assertTrue(non_pdf["readable"])
        self.assertFalse(non_pdf["isPdf"])

    def test_pdf_js_multi_item_preview_cache_and_cleanup(self) -> None:
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("Node.js is required to test web/js/pdf.js")
        store_url = (ROOT / "web" / "js" / "store.js").resolve().as_uri()
        pdf_url = (ROOT / "web" / "js" / "pdf.js").resolve().as_uri()
        script = (
            "const revoked = [];\n"
            "globalThis.window = { matchMedia: () => ({ matches: false }), location: { origin: 'http://localhost' } };\n"
            "globalThis.localStorage = { getItem: () => '', setItem: () => {}, removeItem: () => {} };\n"
            "globalThis.document = { querySelector: () => null, getElementById: () => null };\n"
            "globalThis.URL = { createObjectURL: () => 'blob:mock', revokeObjectURL: (u) => revoked.push(u) };\n"
            f"const {{ appState }} = await import({json.dumps(store_url)});\n"
            f"const pdfMod = await import({json.dumps(pdf_url)});\n"
            "appState.sessionId = 'sess-1';\n"
            "appState.state.order = { items: [{ itemId: 'item-a' }, { itemId: 'item-b' }] };\n"
            "appState.state.activeItemIndex = 0;\n"
            "const keyA = pdfMod.activePreviewKey();\n"
            "appState.pdfPreviews = new Map([\n"
            "  [keyA, { url: 'blob:a', fileName: 'a.pdf', itemId: 'item-a', sessionId: 'sess-1' }],\n"
            "  ['sess-1::item-b', { url: 'blob:b', fileName: 'b.pdf', itemId: 'item-b', sessionId: 'sess-1' }],\n"
            "  ['sess-2::single', { url: 'blob:c', fileName: 'c.pdf', itemId: null, sessionId: 'sess-2' }],\n"
            "]);\n"
            "const activeA = pdfMod.getActivePdfPreview();\n"
            "appState.state.activeItemIndex = 1;\n"
            "const activeB = pdfMod.getActivePdfPreview();\n"
            "pdfMod.clearPdfPreview();\n"
            "const afterClearCurrent = { size: appState.pdfPreviews.size, current: appState.pdfPreview, hasA: appState.pdfPreviews.has(keyA) };\n"
            "pdfMod.clearPdfPreview('sess-1');\n"
            "const afterClearSession = { size: appState.pdfPreviews.size, hasSess2: appState.pdfPreviews.has('sess-2::single') };\n"
            "pdfMod.clearPdfPreview(true);\n"
            "process.stdout.write(JSON.stringify({\n"
            "  keyA, activeA: activeA?.fileName, activeB: activeB?.fileName,\n"
            "  afterClearCurrent, afterClearSession, finalSize: appState.pdfPreviews.size, revoked\n"
            "}));\n"
        )
        proc = subprocess.run(
            [node, "--input-type=module", "-e", script],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        if proc.returncode != 0:
            raise AssertionError(f"pdf.js cache test failed: {proc.stderr}")
        data = json.loads(proc.stdout)
        self.assertEqual(data["keyA"], "sess-1::item-a")
        self.assertEqual(data["activeA"], "a.pdf")
        self.assertEqual(data["activeB"], "b.pdf")
        self.assertEqual(data["afterClearCurrent"]["size"], 2)
        self.assertIsNone(data["afterClearCurrent"]["current"])
        self.assertTrue(data["afterClearCurrent"]["hasA"])
        self.assertEqual(data["afterClearSession"]["size"], 1)
        self.assertTrue(data["afterClearSession"]["hasSess2"])
        self.assertEqual(data["finalSize"], 0)
        self.assertEqual(data["revoked"], ["blob:b", "blob:a", "blob:c"])


class ServerPreflightDeepTest(unittest.TestCase):
    def test_tool_schema_public_contract_unchanged(self) -> None:
        schema = TOOL_SCHEMAS["preflight_file"]["input"]
        self.assertEqual(
            set(schema["properties"].keys()),
            {"fileName", "sizeBytes", "pageCount", "encrypted", "readable", "inspection", "expectedSize", "itemIndex"},
        )
        self.assertEqual(set(schema["required"]), {"fileName", "sizeBytes", "encrypted", "readable"})

    def test_bleed_zero_and_direct_box_fallback(self) -> None:
        a4_pt = [0, 0, 595.28, 841.89]
        zero_bleed = preflight_file(
            "card.pdf", 2048, page_count=2,
            inspection={
                "isPdf": True,
                "boxes": {"media": a4_pt, "trim": a4_pt, "bleed": a4_pt},
            },
        )
        self.assertEqual(zero_bleed["measuredBleedMm"], 0.0)
        self.assertTrue(any("BleedBox 与 TrimBox 重合（出血 0 mm）" in w for w in zero_bleed["warnings"]))

        # Good 3mm bleed computed directly from boxes without bleedAnalysis
        bleed_3mm = [-8.504, -8.504, 595.28 + 8.504, 841.89 + 8.504]
        ok_bleed = preflight_file(
            "card.pdf", 2048, page_count=2,
            inspection={
                "isPdf": True,
                "boxes": {"media": bleed_3mm, "trim": a4_pt, "bleed": bleed_3mm},
            },
        )
        self.assertAlmostEqual(ok_bleed["measuredBleedMm"], 3.0, delta=0.1)
        self.assertTrue(any(c["label"] == "实测单边出血" and c["status"] == "ok" for c in ok_bleed["checks"]))

    def test_expected_pages_cross_check_and_odd_booklet_warning(self) -> None:
        conflict = preflight_file(
            "catalog.pdf", 4096, page_count=15,
            expected_pages="16页", product_type="画册",
        )
        self.assertTrue(conflict["ok"])
        self.assertFalse(conflict["expectedPagesMatch"])
        self.assertTrue(any(c["label"] == "订单页数一致性" and c["status"] == "warn" for c in conflict["checks"]))
        self.assertTrue(any("文件实测为 15 页，与订单填写的 16 页不一致" in w for w in conflict["warnings"]))
        self.assertTrue(any("画册/宣传册文件页数为奇数" in w for w in conflict["warnings"]))

        matched = preflight_file(
            "catalog.pdf", 4096, page_count=16,
            expected_pages="16P", product_type="宣传册",
        )
        self.assertTrue(matched["expectedPagesMatch"])
        self.assertTrue(any(c["label"] == "订单页数一致性" and c["status"] == "ok" for c in matched["checks"]))
        self.assertFalse(any("奇数" in w for w in matched["warnings"]))

        # "16开" is paper format, not page count
        paper_cut = preflight_file("sheet.pdf", 1024, page_count=2, expected_pages="16开")
        self.assertIsNone(paper_cut["expectedPagesMatch"])

    def test_undecoded_compressed_streams_give_precise_hint(self) -> None:
        result = preflight_file(
            "packed.pdf", 2048, page_count=0,
            inspection={
                "isPdf": True,
                "compressedStreams": {"detected": 2, "decoded": 0, "objStmCount": 2},
            },
        )
        self.assertTrue(any(c["label"] == "PDF 对象流" and c["status"] == "warn" for c in result["checks"]))
        self.assertTrue(any("检测到压缩对象流但本地未能解压" in w for w in result["warnings"]))

    def test_malformed_inspection_never_raises(self) -> None:
        malformed = {
            "isPdf": True,
            "pdfVersion": 12345,
            "boxes": {"media": "bad", "trim": [0, 0, "x", float("nan")], "bleed": [1, 2]},
            "compressedStreams": {"detected": True, "decoded": "nope", "objStmCount": -3},
            "bleedAnalysis": {"source": "bleed_vs_trim", "minMm": float("nan")},
            "pageSummary": {
                "analyzedPages": "many",
                "uniformSize": "yes",
                "mixedOrientation": 1,
                "mismatchPages": ["bad", True, -1, 3],
            },
            "imageSummary": {
                "count": "lots",
                "lowResCount": True,
                "lowResSuspects": [None, "bad", {"widthPx": True, "heightPx": -5}],
            },
            "colorSpaces": "DeviceCMYK",
            "imageCount": True,
        }
        result = preflight_file(
            "weird.pdf", 1024, page_count=2,
            inspection=malformed, expected_size="A4",
            expected_pages=True,  # type: ignore[arg-type]
            product_type="画册",
        )
        self.assertIsInstance(result["ok"], bool)
        self.assertIsNone(result["measuredBleedMm"])
        self.assertIsNone(result["observedSizeMm"])
        self.assertIsNone(result["pageConsistency"])
        self.assertIsNone(result["expectedPagesMatch"])


if __name__ == "__main__":
    unittest.main()
