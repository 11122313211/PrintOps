"""API contract tests for pricing simulation and cross-field deep PDF preflight."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server
from agent import Agent, Memory


class PrintIntelligenceApiTest(unittest.TestCase):
    """End-to-end API contracts for read-only simulation & cross-field PDF preflight."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_memory = server.MEMORY
        self.previous_locks = server.SESSION_LOCKS
        self.previous_planner = server.PLANNER
        server.MEMORY = Memory(Path(self.tmp.name) / "agent.sqlite3")
        server.SESSION_LOCKS = {}
        server.PLANNER = server.OpenAICompatiblePlanner()

    def tearDown(self):
        server.MEMORY = self.previous_memory
        server.SESSION_LOCKS = self.previous_locks
        server.PLANNER = self.previous_planner
        self.tmp.cleanup()

    def request(self, method: str, path: str, payload: dict | None = None):
        payload = dict(payload or {})
        if method == "GET":
            return server.dispatch_get(path)
        session_id = server._session_id(payload.get("sessionId"))
        agent = (
            Agent(server.MEMORY, session_id, planner=server.PLANNER)
            if session_id
            else Agent(server.MEMORY, planner=server.PLANNER)
        )
        return server.dispatch_post(path, payload, agent)

    def test_pricing_simulate_get_and_post_are_read_only_and_exact(self):
        created = self.request("POST", "/api/chat", {
            "text": "500张名片，成品90×54mm，300g铜版纸，双面四色，无特殊工艺，无需装订，下周内",
        })
        sid = created["sessionId"]
        rev_before = created["revision"]

        # GET loads baseline tiers & adjustable options without advancing revision
        initial = self.request("GET", f"/api/pricing/simulate?sessionId={sid}")
        self.assertEqual(initial["type"], "pricing_simulation")
        self.assertTrue(initial["readOnly"])
        self.assertEqual(initial["sessionId"], sid)
        self.assertEqual(initial["revision"], rev_before)
        self.assertEqual(initial["baseline"]["status"], "ready")
        self.assertGreaterEqual(len(initial["tiers"]), 4)
        self.assertTrue(any(tier["isCurrent"] and tier["quantity"] == 500 for tier in initial["tiers"]))
        self.assertIsNotNone(initial["sweetSpot"])

        # POST runs What-If simulation without mutating order or revision
        sim = self.request("POST", "/api/pricing/simulate", {
            "sessionId": sid,
            "overrides": {"quantity": 1000, "finishing": ["哑膜", "烫金"]},
        })
        self.assertTrue(sim["hasOverrides"])
        self.assertEqual(sim["simulated"]["status"], "ready")
        self.assertTrue(sim["delta"]["comparable"])
        self.assertGreater(float(sim["simulated"]["range"]["mid"]), float(sim["baseline"]["range"]["mid"]))

        after = self.request("GET", f"/api/orders/{sid}")
        self.assertEqual(after["revision"], rev_before)
        self.assertEqual(after["order"]["quantityValue"], 500)
        self.assertEqual(after["order"]["finishing"], "无特殊工艺")

    def test_pricing_simulate_allows_archived_orders_and_validates_inputs(self):
        created = self.request("POST", "/api/chat", {
            "text": "500张名片，成品90×54mm，300g铜版纸，双面四色，无特殊工艺，无需装订，下周内",
        })
        sid = created["sessionId"]
        self.request("POST", f"/api/orders/{sid}/archive", {
            "expectedRevision": created["revision"],
        })

        # Archived order remains readable for both GET and POST read-only simulation
        sim_get = self.request("GET", f"/api/pricing/simulate?sessionId={sid}")
        self.assertTrue(sim_get["archived"])
        self.assertEqual(sim_get["baseline"]["status"], "ready")

        sim_post = self.request("POST", "/api/pricing/simulate", {
            "sessionId": sid,
            "overrides": {"quantity": 2000},
        })
        self.assertTrue(sim_post["archived"])
        self.assertEqual(sim_post["simulated"]["status"], "ready")

        # Invalid overrides return 400 INVALID_SIMULATION
        with self.assertRaises(server.RequestError) as bad_override:
            self.request("POST", "/api/pricing/simulate", {
                "sessionId": sid,
                "overrides": {"quantity": -5},
            })
        self.assertEqual(bad_override.exception.status, 400)
        self.assertEqual(bad_override.exception.code, "INVALID_SIMULATION")

        # Unknown sessionId on GET returns 404 ORDER_NOT_FOUND
        with self.assertRaises(server.RequestError) as missing:
            self.request("GET", "/api/pricing/simulate?sessionId=nonexistent123")
        self.assertEqual(missing.exception.status, 404)
        self.assertEqual(missing.exception.code, "ORDER_NOT_FOUND")

    def test_pricing_simulate_multi_product_item_isolation(self):
        created = self.request("POST", "/api/chat", {
            "text": "我要印500张名片和200张单页，下周内",
        })
        sid = created["sessionId"]
        items = created["order"]["items"]
        self.assertEqual(len(items), 2)
        first_id, second_id = items[0]["itemId"], items[1]["itemId"]

        sim_first = self.request("GET", f"/api/pricing/simulate?sessionId={sid}&itemId={first_id}")
        sim_second = self.request("GET", f"/api/pricing/simulate?sessionId={sid}&itemId={second_id}")
        self.assertEqual(sim_first["itemId"], first_id)
        self.assertEqual(sim_second["itemId"], second_id)
        self.assertNotEqual(sim_first["productType"], sim_second["productType"])

        with self.assertRaises(server.RequestError) as bad_item:
            self.request("GET", f"/api/pricing/simulate?sessionId={sid}&itemId=item-missing")
        self.assertEqual(bad_item.exception.status, 409)
        self.assertEqual(bad_item.exception.code, "ITEM_NOT_FOUND")

    def test_preflight_api_enforces_cross_field_pages_and_bleed_measurement(self):
        created = self.request("POST", "/api/chat", {
            "text": "200本宣传册，成品210×297mm，16页，157g铜版纸，双面四色，骑马钉，下周内",
        })
        sid = created["sessionId"]

        # Mismatched page count (14 vs 16) + insufficient 1.0mm bleed
        res_warn = self.request("POST", "/api/preflight", {
            "sessionId": sid,
            "expectedRevision": created["revision"],
            "fileName": "brochure-v1.pdf",
            "sizeBytes": 102400,
            "pageCount": 14,
            "encrypted": False,
            "readable": True,
            "inspection": {
                "isPdf": True,
                "pdfVersion": "1.7",
                "pageCount": 14,
                "hasEof": True,
                "boxes": {
                    "media": [0, 0, 601.0, 847.6],
                    "trim": [2.835, 2.835, 598.11, 844.725],
                    "bleed": [0, 0, 600.945, 847.56],
                },
                "bleedAnalysis": {"source": "bleed_vs_trim", "minMm": 1.0, "maxMm": 1.0},
                "compressedStreams": {"detected": 2, "decoded": 2, "objStmCount": 1},
                "colorSpaces": ["DeviceCMYK"],
                "fontEmbedding": "embedded",
            },
        })
        tool_res = res_warn["toolResult"]
        self.assertTrue(tool_res["ok"])
        self.assertFalse(tool_res["expectedPagesMatch"])
        self.assertAlmostEqual(tool_res["measuredBleedMm"], 1.0, places=1)
        self.assertTrue(any("14" in w and "16" in w for w in tool_res["warnings"]))
        self.assertTrue(any("出血" in w and "1.0" in w for w in tool_res["warnings"]))

        # Matching page count (16 vs 16) + 3.0mm bleed via call_tool gateway
        res_ok = self.request("POST", "/api/tools/call", {
            "sessionId": sid,
            "expectedRevision": res_warn["revision"],
            "toolName": "preflight_file",
            "payload": {
                "fileName": "brochure-v2.pdf",
                "sizeBytes": 102400,
                "pageCount": 16,
                "encrypted": False,
                "readable": True,
                "inspection": {
                    "isPdf": True,
                    "pdfVersion": "1.7",
                    "pageCount": 16,
                    "hasEof": True,
                    "boxes": {
                        "media": [0, 0, 612.28, 858.90],
                        "trim": [8.504, 8.504, 603.78, 850.39],
                        "bleed": [0, 0, 612.28, 858.90],
                    },
                    "bleedAnalysis": {"source": "bleed_vs_trim", "minMm": 3.0, "maxMm": 3.0},
                    "colorSpaces": ["DeviceCMYK"],
                    "fontEmbedding": "embedded",
                },
            },
        })
        ok_tool = res_ok["toolResult"]
        self.assertTrue(ok_tool["expectedPagesMatch"])
        self.assertAlmostEqual(ok_tool["measuredBleedMm"], 3.0, places=1)
        bleed_check = next(c for c in ok_tool["checks"] if c["label"] == "实测单边出血")
        self.assertEqual(bleed_check["status"], "ok")

    def test_pricing_simulate_http_endpoint_requires_local_token(self):
        httpd = server.build_server(0)
        port = httpd.server_address[1]
        import threading
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            created = self.request("POST", "/api/chat", {
                "text": "500张名片，成品90×54mm，300g铜版纸，双面四色，无特殊工艺，无需装订，下周内",
            })
            sid = created["sessionId"]

            # Missing token must return 401
            with self.assertRaises(HTTPError) as unauth:
                urlopen(f"http://127.0.0.1:{port}/api/pricing/simulate?sessionId={sid}", timeout=5)
            self.assertEqual(unauth.exception.code, 401)
            unauth.exception.close()

            # With local token GET succeeds
            req = Request(
                f"http://127.0.0.1:{port}/api/pricing/simulate?sessionId={sid}",
                headers={server.TOKEN_HEADER: server.ACCESS_TOKEN},
            )
            with urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["type"], "pricing_simulation")
            self.assertTrue(data["readOnly"])
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
