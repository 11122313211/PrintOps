import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import Agent
from mcp_server import (DEFAULT_MEMORY_PATH, MAX_MESSAGE_BYTES, MAX_SAFE_INTEGER,
                        MAX_METHOD_LENGTH,
                        MAX_SESSION_LOCKS, MCP_PROTOCOL_VERSION, PrintOpsMCP)


class PrintOpsMCPTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "agent.sqlite3"
        self.server = PrintOpsMCP(self.db, allow_any_session=True)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def request(server, request_id, method, params=None):
        return server.handle({"jsonrpc": "2.0", "id": request_id,
                              "method": method,
                              "params": {} if params is None else params})

    def initialize(self):
        result = self.request(self.server, 1, "initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        })
        self.assertEqual(result["result"]["serverInfo"]["name"], "printops-mcp")

    def test_initialize_and_tools_list_expose_reviewed_session_schema(self):
        self.initialize()
        result = self.request(self.server, 2, "tools/list")
        tools = result["result"]["tools"]
        names = {item["name"] for item in tools}
        self.assertEqual(names, {
            "validate_order", "recommend_processes", "explain_print_term",
            "estimate_price", "match_supplier_capability",
        })
        validate = next(item for item in tools if item["name"] == "validate_order")
        self.assertIn("sessionId", validate["inputSchema"]["required"])
        self.assertTrue(validate["inputSchema"]["additionalProperties"] is False)
        self.assertNotIn("order", validate["inputSchema"]["properties"])

    def test_tool_call_uses_persisted_session_and_returns_projected_result(self):
        self.initialize()
        result = self.request(self.server, 2, "tools/call", {
            "name": "explain_print_term",
            "arguments": {"sessionId": "mcp-session", "question": "纸张怎么选"},
        })
        payload = result["result"]
        self.assertFalse(payload["isError"])
        structured = payload["structuredContent"]
        self.assertEqual(structured["sessionId"], "mcp-session")
        self.assertEqual(structured["toolResult"]["topic"], "纸张选择")
        self.assertNotIn("llm", structured)

    def test_unknown_order_override_and_disabled_l1_are_rejected(self):
        self.initialize()
        invalid = self.request(self.server, 2, "tools/call", {
            "name": "validate_order",
            "arguments": {"sessionId": "mcp-session", "order": {"quantity": "999"}},
        })
        self.assertEqual(invalid["error"]["data"]["code"], "invalid_arguments")

        denied = self.request(self.server, 3, "tools/call", {
            "name": "prepare_handoff",
            "arguments": {"sessionId": "mcp-session"},
        })
        self.assertEqual(denied["error"]["data"]["code"], "capability_denied")

    def test_unknown_platform_id_is_rejected_at_mcp_boundary(self):
        self.initialize()
        response = self.request(self.server, 4, "tools/call", {
            "name": "match_supplier_capability",
            "arguments": {"sessionId": "mcp-session", "platformId": "not-a-platform"},
        })
        self.assertEqual(response["error"]["data"]["code"], "invalid_arguments")

    def test_recommendation_and_handoff_are_blocked_until_order_is_ready(self):
        server = PrintOpsMCP(self.db, bound_session_id="empty", capabilities="L0,L1")
        self.request(server, 1, "initialize", {"protocolVersion": MCP_PROTOCOL_VERSION})
        recommended = self.request(server, 2, "tools/call", {
            "name": "recommend_processes", "arguments": {"sessionId": "empty"},
        })
        recommended_result = recommended["result"]["structuredContent"]
        self.assertEqual(recommended_result["toolResult"]["status"], "blocked")
        self.assertEqual(recommended_result["toolResult"]["reason"], "order_not_ready")

        handoff = self.request(server, 3, "tools/call", {
            "name": "prepare_handoff", "arguments": {"sessionId": "empty"},
        })
        handoff_result = handoff["result"]["structuredContent"]
        self.assertEqual(handoff_result["toolResult"]["status"], "blocked")
        self.assertEqual(handoff_result["toolResult"]["reason"], "order_not_ready")

    def test_low_confidence_blocks_handoff_and_quote(self):
        session_id = "uncertain"
        agent = Agent(self.server.memory, session_id)
        agent.chat("做 500 张 A4 名片，250g铜版纸，双面四色，下周内")
        agent.state["selectedOption"] = "balanced"
        agent.state["fieldMeta"]["size"] = {"value": "A4", "confidence": 0.5}
        agent.memory.save(session_id, agent.state)
        server = PrintOpsMCP(self.db, bound_session_id=session_id, capabilities="L0,L1")
        self.request(server, 1, "initialize", {"protocolVersion": MCP_PROTOCOL_VERSION})
        for request_id, name in ((2, "prepare_handoff"), (3, "request_supplier_quote")):
            blocked = self.request(server, request_id, "tools/call", {
                "name": name, "arguments": {"sessionId": session_id},
            })
            self.assertEqual(blocked["result"]["structuredContent"]["toolResult"]["reason"], "low_confidence")

    def test_low_confidence_does_not_block_process_recommendation(self):
        session_id = "uncertain-recommend"
        agent = Agent(self.server.memory, session_id)
        agent.chat("做 500 张单页，A4，157g哑粉纸，双面四色，下周内")
        agent.state["fieldMeta"]["size"]["confidence"] = 0.5
        agent.memory.save(session_id, agent.state)
        server = PrintOpsMCP(self.db, bound_session_id=session_id, capabilities="L0")
        self.request(server, 1, "initialize", {"protocolVersion": MCP_PROTOCOL_VERSION})
        result = self.request(server, 2, "tools/call", {
            "name": "recommend_processes", "arguments": {"sessionId": session_id},
        })
        self.assertNotIn("error", result)
        tool_result = result["result"]["structuredContent"]["toolResult"]
        self.assertIsInstance(tool_result, list)
        self.assertTrue(tool_result)

    def test_non_string_argument_keys_are_client_errors(self):
        self.initialize()
        malformed = self.server.handle({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "validate_order",
                        "arguments": {"sessionId": "mcp-session", 7: "unexpected"}},
        })
        self.assertEqual(malformed["error"]["data"]["code"], "invalid_arguments")

    def test_multi_product_shared_low_confidence_blocks_selected_item(self):
        session_id = "shared-uncertain"
        agent = Agent(self.server.memory, session_id)
        agent.chat("做 500 张名片和 1000 张折页，A4，157g哑粉纸，双面四色，下周内")
        # The first item is complete enough for the readiness check. Its
        # paper and size are inherited from the top-level compatibility view,
        # where the parser stores their provenance.
        agent.state["order"]["items"][0]["selectedOption"] = "balanced"
        agent.state["fieldMeta"]["paper"]["confidence"] = 0.5
        agent.state["fieldMeta"]["dimensions.finishedSize"]["confidence"] = 0.5
        agent.memory.save(session_id, agent.state)

        server = PrintOpsMCP(self.db, bound_session_id=session_id, capabilities="L0,L1")
        self.request(server, 1, "initialize", {"protocolVersion": MCP_PROTOCOL_VERSION})
        blocked = self.request(server, 2, "tools/call", {
            "name": "prepare_handoff",
            "arguments": {"sessionId": session_id, "itemIndex": 0},
        })
        payload = blocked["result"]["structuredContent"]
        self.assertEqual(payload["toolResult"]["reason"], "low_confidence")
        self.assertIn("paper", payload["toolResult"]["uncertain"])
        self.assertIn("dimensions.finishedSize", payload["toolResult"]["uncertain"])

    def test_precondition_only_blocks_low_confidence_production_fields(self):
        session_id = "preference-confidence"
        agent = Agent(self.server.memory, session_id)
        agent.chat("做 500 张名片，A4，157g哑粉纸，双面四色，下周内")
        agent.state["selectedOption"] = "balanced"
        agent.state["fieldMeta"]["budget"] = {"value": "优先视觉质感", "confidence": 0.2}
        agent.memory.save(session_id, agent.state)
        server = PrintOpsMCP(self.db, bound_session_id=session_id, capabilities="L0,L1")
        self.request(server, 1, "initialize", {"protocolVersion": MCP_PROTOCOL_VERSION})
        result = self.request(server, 2, "tools/call", {
            "name": "prepare_handoff", "arguments": {"sessionId": session_id},
        })
        self.assertNotIn("error", result)
        self.assertNotEqual(result["result"]["structuredContent"]["toolResult"].get("reason"),
                            "low_confidence")

    def test_default_memory_path_is_relative_to_adapter_module(self):
        # Mock construction so this regression test never touches the shared
        # repository database. The cwd must not affect the implicit path.
        with patch.dict(os.environ, {"PRINTOPS_MEMORY_PATH": ""}, clear=False), \
                patch("mcp_server.Memory") as memory_cls:
            PrintOpsMCP(bound_session_id="path-check")
        self.assertEqual(memory_cls.call_args.args[0], DEFAULT_MEMORY_PATH)

    def test_initialize_and_request_params_must_be_objects(self):
        for invalid_params in ([], "", False, None):
            server = PrintOpsMCP(self.db, bound_session_id="params-check")
            response = server.handle({"jsonrpc": "2.0", "id": 1,
                                      "method": "initialize", "params": invalid_params})
            self.assertEqual(response["error"]["data"]["code"], "invalid_arguments")

        self.initialize()
        for invalid_params in ([], "", False, None):
            response = self.server.handle({"jsonrpc": "2.0", "id": 10,
                                           "method": "tools/list", "params": invalid_params})
            self.assertEqual(response["error"]["data"]["code"], "invalid_arguments")

    def test_session_lock_cache_is_bounded_for_any_session_mode(self):
        # Sequential leases leave entries idle, so an untrusted stream of
        # session IDs must not grow the process indefinitely.
        for index in range(MAX_SESSION_LOCKS + 17):
            with self.server._lock_for(f"session-{index}"):
                pass
        self.assertLessEqual(len(self.server._locks), MAX_SESSION_LOCKS)
        self.assertLessEqual(len(self.server._lock_refs), MAX_SESSION_LOCKS)

    def test_item_selector_rejects_empty_ids_and_non_item_tools(self):
        self.initialize()
        empty_id = self.request(self.server, 2, "tools/call", {
            "name": "match_supplier_capability",
            "arguments": {"sessionId": "mcp-session", "itemId": ""},
        })
        self.assertEqual(empty_id["error"]["data"]["code"], "invalid_arguments")

        non_item = self.request(self.server, 3, "tools/call", {
            "name": "explain_print_term",
            "arguments": {"sessionId": "mcp-session", "question": "出血", "itemIndex": 0},
        })
        self.assertEqual(non_item["error"]["data"]["code"], "invalid_arguments")

    def test_malformed_params_and_tool_names_are_client_errors(self):
        self.initialize()
        malformed = self.server.handle({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": [],
        })
        self.assertEqual(malformed["error"]["data"]["code"], "invalid_arguments")

        bad_name = self.request(self.server, 3, "tools/call", {
            "name": ["validate_order"], "arguments": {"sessionId": "mcp-session"},
        })
        self.assertEqual(bad_name["error"]["data"]["code"], "invalid_arguments")

    def test_request_id_and_method_are_bounded_json_rpc_scalars(self):
        self.initialize()
        huge_method = self.server.handle({
            "jsonrpc": "2.0", "id": 2,
            "method": "x" * (MAX_METHOD_LENGTH + 1), "params": {},
        })
        self.assertEqual(huge_method["error"]["data"]["code"], "invalid_request")
        self.assertLess(len(huge_method["error"]["message"]), 200)

        for invalid_id in ([], {}, True, float("nan"), MAX_SAFE_INTEGER + 1):
            response = self.server.handle({
                "jsonrpc": "2.0", "id": invalid_id, "method": "ping", "params": {},
            })
            self.assertEqual(response["error"]["data"]["code"], "invalid_request")

    def test_malformed_notifications_remain_silent(self):
        self.initialize()
        response = self.server.handle({"jsonrpc": "2.0", "method": [], "params": []})
        self.assertIsNone(response)

    def test_notifications_are_silent(self):
        self.initialize()
        for method in ("notifications/initialized", "notifications/cancelled", "unknown/notification"):
            response = self.server.handle({"jsonrpc": "2.0", "method": method, "params": {}})
            self.assertIsNone(response, method)

    def test_deeply_nested_json_is_reported_without_killing_server(self):
        output = io.StringIO()
        nested = "[" * 1500 + "]" * 1500
        self.server.serve(io.StringIO(nested + "\n" + json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": MCP_PROTOCOL_VERSION},
        }) + "\n"), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
        self.assertEqual(responses[0]["error"]["data"]["code"], "parse_error")
        self.assertEqual(responses[1]["result"]["serverInfo"]["name"], "printops-mcp")

    def test_bound_process_rejects_other_sessions_and_resources_are_read_only(self):
        bound = PrintOpsMCP(self.db, bound_session_id="only-this", capabilities="L0")
        self.request(bound, 1, "initialize", {"protocolVersion": MCP_PROTOCOL_VERSION})
        wrong = self.request(bound, 2, "tools/call", {
            "name": "validate_order", "arguments": {"sessionId": "other"},
        })
        self.assertEqual(wrong["error"]["data"]["code"], "invalid_session")

        listed = self.request(bound, 3, "resources/list")
        self.assertTrue(any(item["uri"] == "printops://products" for item in listed["result"]["resources"]))
        read = self.request(bound, 4, "resources/read", {"uri": "printops://products"})
        self.assertEqual(read["result"]["contents"][0]["mimeType"], "application/json")
        self.assertIn("products", json.loads(read["result"]["contents"][0]["text"]))

    def test_l1_preflight_preserves_encryption_and_readability_flags(self):
        server = PrintOpsMCP(self.db, bound_session_id="preflight", capabilities="L0,L1")
        self.request(server, 1, "initialize", {"protocolVersion": MCP_PROTOCOL_VERSION})
        encrypted = self.request(server, 2, "tools/call", {
            "name": "preflight_file",
            "arguments": {"sessionId": "preflight", "fileName": "locked.pdf",
                          "sizeBytes": 1024, "pageCount": 1, "encrypted": True,
                          "readable": True},
        })
        self.assertFalse(encrypted["result"]["structuredContent"]["toolResult"]["ok"])
        self.assertIn("已加密", encrypted["result"]["structuredContent"]["toolResult"]["message"])

        unreadable = self.request(server, 3, "tools/call", {
            "name": "preflight_file",
            "arguments": {"sessionId": "preflight", "fileName": "broken.pdf",
                          "sizeBytes": 1024, "pageCount": 1, "encrypted": False,
                          "readable": False},
        })
        self.assertFalse(unreadable["result"]["structuredContent"]["toolResult"]["ok"])
        self.assertIn("无法读取", unreadable["result"]["structuredContent"]["toolResult"]["message"])

    def test_multi_product_selectors_are_range_checked_and_item_id_resolves(self):
        session_id = "multi-session"
        Agent(self.server.memory, session_id).chat("做 500 张名片和 1000 张折页，A4，157g哑粉纸，双面四色，下周内")
        server = PrintOpsMCP(self.db, bound_session_id=session_id, capabilities="L0")
        self.request(server, 1, "initialize", {"protocolVersion": MCP_PROTOCOL_VERSION})

        out_of_range = self.request(server, 2, "tools/call", {
            "name": "match_supplier_capability",
            "arguments": {"sessionId": session_id, "itemIndex": 999},
        })
        self.assertEqual(out_of_range["error"]["data"]["code"], "invalid_arguments")

        missing = self.request(server, 3, "tools/call", {
            "name": "match_supplier_capability",
            "arguments": {"sessionId": session_id},
        })
        self.assertEqual(missing["error"]["data"]["code"], "item_required")

        by_id = self.request(server, 4, "tools/call", {
            "name": "match_supplier_capability",
            "arguments": {"sessionId": session_id, "itemId": "item-2"},
        })
        self.assertFalse("error" in by_id)
        self.assertEqual(by_id["result"]["structuredContent"]["toolResult"]["itemIndex"], 1)

        conflict = self.request(server, 5, "tools/call", {
            "name": "match_supplier_capability",
            "arguments": {"sessionId": session_id, "itemIndex": 0, "itemId": "item-2"},
        })
        self.assertEqual(conflict["error"]["data"]["code"], "invalid_arguments")

    def test_stdio_transcript_has_no_non_json_stdout(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION}},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "validate_order", "arguments": {"sessionId": "stdio-session"}}},
        ]
        env = dict(os.environ)
        env.pop("PRINTOPS_MCP_SESSION_ID", None)
        completed = subprocess.run(
            [sys.executable, "mcp_server.py", "--session-id", "stdio-session",
             "--memory-path", str(self.db)],
            input="\n".join(json.dumps(item, ensure_ascii=False) for item in requests) + "\n",
            text=True, capture_output=True, env=env, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        responses = [json.loads(line) for line in lines]
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "printops-mcp")
        self.assertFalse(responses[1]["result"]["isError"])

    def test_stdio_rejects_oversized_line_and_continues(self):
        requests = [
            "x" * (MAX_MESSAGE_BYTES + 10),
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": MCP_PROTOCOL_VERSION}}),
        ]
        completed = subprocess.run(
            [sys.executable, "mcp_server.py", "--session-id", "stdio-session",
             "--memory-path", str(self.db)],
            input="\n".join(requests) + "\n", text=True, capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        self.assertEqual(responses[0]["error"]["data"]["code"], "message_too_large")
        self.assertEqual(responses[1]["result"]["serverInfo"]["name"], "printops-mcp")

    def test_stdio_rejects_nonfinite_json_constants(self):
        completed = subprocess.run(
            [sys.executable, "mcp_server.py", "--session-id", "strict-json",
             "--memory-path", str(self.db)],
            input='{"jsonrpc":"2.0","id":1,"method":"initialize",'
                  '"params":{"protocolVersion":"2024-11-05","value":NaN}}\n',
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        response = json.loads(completed.stdout)
        self.assertEqual(response["error"]["data"]["code"], "parse_error")

    def test_nonfinite_inspection_values_are_rejected(self):
        server = PrintOpsMCP(self.db, allow_any_session=True, capabilities="L0,L1")
        self.request(server, 1, "initialize", {"protocolVersion": MCP_PROTOCOL_VERSION})
        response = self.request(server, 2, "tools/call", {
            "name": "preflight_file",
            "arguments": {"sessionId": "mcp-session", "fileName": "x.pdf",
                          "sizeBytes": 1, "encrypted": False, "readable": True,
                          "inspection": {"boxes": {"trim": [0, 0, float("nan"), 1]}}},
        })
        self.assertEqual(response["error"]["data"]["code"], "invalid_arguments")

    def test_integer_arguments_have_a_json_safe_upper_bound(self):
        server = PrintOpsMCP(self.db, allow_any_session=True, capabilities="L0,L1")
        self.request(server, 1, "initialize", {"protocolVersion": MCP_PROTOCOL_VERSION})
        response = self.request(server, 2, "tools/call", {
            "name": "preflight_file",
            "arguments": {"sessionId": "mcp-session", "fileName": "x.pdf",
                          "sizeBytes": MAX_SAFE_INTEGER + 1,
                          "encrypted": False, "readable": True},
        })
        self.assertEqual(response["error"]["data"]["code"], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
