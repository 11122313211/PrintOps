import tempfile
import unittest
from pathlib import Path

import server
from agent import Agent, Memory


class ServerContractTest(unittest.TestCase):
    """Exercise the JSON boundary used by the browser without external calls."""

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

    def request(self, method, path, payload=None):
        payload = dict(payload or {})
        if method == "GET":
            return server.dispatch_get(path)
        session_id = server._session_id(payload.get("sessionId"))
        agent = Agent(server.MEMORY, session_id, planner=server.PLANNER) if session_id else Agent(server.MEMORY, planner=server.PLANNER)
        return server.dispatch_post(path, payload, agent)

    def test_health_and_catalog_contracts(self):
        result = self.request("GET", "/api/health")
        self.assertEqual(result, {"ok": True})

        result = self.request("GET", "/api/tools")
        self.assertTrue(any(item["name"] == "validate_order" for item in result["tools"]))

        platforms = self.request("GET", "/api/platforms")
        self.assertTrue(platforms["platforms"])
        self.assertTrue(all(item.get("supplierProfileVersion") for item in platforms["platforms"]))

        catalog = self.request("GET", "/api/products")
        self.assertIn("knowledge", catalog)
        self.assertEqual(catalog["knowledge"]["schemaVersion"], "1.0")
        self.assertTrue(catalog["knowledge"]["dimensionDefinitions"]["size"]["requiresConfirmation"])
        self.assertTrue(catalog["products"])
        self.assertEqual(catalog["products"][0]["knowledge"]["version"], catalog["knowledge"]["version"])

    def test_multi_product_item_index_is_preserved_across_http_requests(self):
        initial = {
            "text": "做 500 张名片和 1000 张折页，A4，157g哑粉纸，双面四色，下周内",
        }
        result = self.request("POST", "/api/chat", initial)
        session_id = result["sessionId"]
        self.assertEqual(len(result["order"]["items"]), 2)

        result = self.request("POST", "/api/chat", {
            "sessionId": session_id,
            "itemIndex": 1,
            "text": "补充第二项",
            "patch": {"quantity": "1200 张", "productSpecs": {"folding": "三折"}},
        })
        items = result["order"]["items"]
        self.assertEqual(items[0]["quantity"], "500 张")
        self.assertEqual(items[1]["quantity"], "1200 张")
        self.assertEqual(items[1]["productSpecs"]["folding"], "三折")
        self.assertEqual(result["activeItemIndex"], 1)

        result = self.request("POST", "/api/choose", {
            "sessionId": session_id,
            "itemIndex": 1,
            "optionId": "balanced",
        })
        self.assertEqual(result["order"]["items"][1]["selectedOption"], "balanced")
        self.assertIsNone(result["order"]["items"][0]["selectedOption"])

    def test_generate_requires_all_multi_product_items_to_be_ready(self):
        result = self.request("POST", "/api/chat", {
            "text": "做 500 张名片和 1000 张折页，A4，157g哑粉纸，双面四色，下周内",
        })
        session_id = result["sessionId"]

        result = self.request("POST", "/api/generate", {"sessionId": session_id})
        self.assertFalse(result["orderGenerated"])
        self.assertEqual(result["handoff"], None)
        self.assertIn("第 2 项", result["messages"][0])
        self.assertEqual(result["validation"]["itemValidations"][1]["status"], "needs_input")

    def test_confirm_route_persists_approval_after_generation(self):
        result = self.request("POST", "/api/chat", {
            "text": "做 500 份 A4 名片，250g铜版纸，双面四色，下周内",
        })
        session_id = result["sessionId"]
        self.request("POST", "/api/choose", {"sessionId": session_id, "optionId": "balanced"})
        generated = self.request("POST", "/api/generate", {"sessionId": session_id})
        self.assertEqual(generated["confirmation"]["status"], "pending")

        confirmed = self.request("POST", "/api/confirm", {
            "sessionId": session_id,
            "note": "已核对",
        })
        self.assertEqual(confirmed["confirmation"]["status"], "confirmed")
        self.assertEqual(confirmed["workflowStage"], "export")
        self.assertEqual(confirmed["handoff"]["status"], "ready")

    def test_quote_status_and_cancel_routes_share_persisted_request(self):
        result = self.request("POST", "/api/chat", {
            "text": "做 500 张 A4 名片，250g铜版纸，双面四色，下周内",
        })
        session_id = result["sessionId"]
        self.request("POST", "/api/choose", {"sessionId": session_id, "optionId": "balanced"})
        quoted = self.request("POST", "/api/tools/call", {
            "sessionId": session_id,
            "toolName": "request_supplier_quote",
            "payload": {"platformId": "shengda"},
        })
        request_id = quoted["quoteRequest"]["requestId"]
        status = self.request("POST", "/api/quote/status", {
            "sessionId": session_id, "requestId": request_id,
        })
        self.assertEqual(status["toolResult"]["status"], "awaiting_human_confirmation")
        cancelled = self.request("POST", "/api/quote/cancel", {
            "sessionId": session_id, "requestId": request_id, "reason": "改走另一家供应商",
        })
        self.assertEqual(cancelled["quoteRequest"]["status"], "cancelled")
        repeated = self.request("POST", "/api/quote/cancel", {
            "sessionId": session_id, "requestId": request_id,
        })
        self.assertTrue(repeated["toolResult"]["idempotent"])

    def test_preflight_route_requires_and_preserves_multi_product_item_index(self):
        result = self.request("POST", "/api/chat", {
            "text": "做 500 张名片和 1000 张折页，A4，157g哑粉纸，双面四色，下周内",
        })
        session_id = result["sessionId"]
        blocked = self.request("POST", "/api/preflight", {
            "sessionId": session_id, "fileName": "artwork.pdf", "sizeBytes": 1024, "pageCount": 1,
        })
        self.assertEqual(blocked["toolResult"]["reason"], "item_required")

        checked = self.request("POST", "/api/preflight", {
            "sessionId": session_id, "itemIndex": 1, "fileName": "fold.pdf",
            "sizeBytes": 1024, "pageCount": 1,
        })
        self.assertTrue(checked["toolResult"]["ok"])
        self.assertEqual(checked["toolResult"]["itemIndex"], 1)
        self.assertEqual(checked["order"]["items"][1]["uploadedFile"], "fold.pdf")

    def test_bad_json_and_unknown_route_have_safe_request_ids(self):
        with self.assertRaises(server.RequestError) as unknown:
            self.request("POST", "/api/does-not-exist", {})
        self.assertEqual(unknown.exception.code, "NOT_FOUND")

        with self.assertRaises(server.RequestError) as invalid:
            self.request("POST", "/api/chat", {"sessionId": "bad id", "text": "继续"})
        self.assertEqual(invalid.exception.code, "INVALID_SESSION")


if __name__ == "__main__":
    unittest.main()
