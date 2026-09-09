"""Focused contracts for the session-bound dsh patch bridge."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from agent import Agent, Memory
from mcp_server import MCP_PROTOCOL_VERSION, PrintOpsMCP
from product_knowledge import KNOWLEDGE_VERSION


class PatchBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "agent.sqlite3"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def request(server: PrintOpsMCP, request_id: int, method: str, params=None):
        return server.handle({"jsonrpc": "2.0", "id": request_id, "method": method,
                              "params": {} if params is None else params})

    def initialize(self, server: PrintOpsMCP) -> None:
        response = self.request(server, 1, "initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
        })
        self.assertIn("result", response)

    def test_apply_order_patch_is_l1_only_and_schema_is_explicit(self) -> None:
        l0 = PrintOpsMCP(self.db, bound_session_id="schema", capabilities="L0")
        self.initialize(l0)
        listed = self.request(l0, 2, "tools/list")
        self.assertNotIn("apply_order_patch",
                         {item["name"] for item in listed["result"]["tools"]})

        l1 = PrintOpsMCP(self.db, bound_session_id="schema", capabilities="L0,L1")
        self.initialize(l1)
        listed = self.request(l1, 2, "tools/list")
        tool = next(item for item in listed["result"]["tools"]
                    if item["name"] == "apply_order_patch")
        self.assertIn("expectedRevision", tool["inputSchema"]["required"])
        self.assertFalse(tool["inputSchema"]["additionalProperties"])
        self.assertNotIn("order", tool["inputSchema"]["properties"])

    def test_patch_applies_provenance_and_increments_revision(self) -> None:
        sid = "apply"
        agent = Agent(Memory(self.db), sid)
        agent.chat("做 500 张名片")
        before = Agent(Memory(self.db), sid).state
        server = PrintOpsMCP(self.db, bound_session_id=sid, capabilities="L0,L1")
        self.initialize(server)
        response = self.request(server, 2, "tools/call", {
            "name": "apply_order_patch",
            "arguments": {
                "sessionId": sid,
                "patch": {"size": "A4", "unknownField": "drop"},
                "evidence": [{"field": "size", "quote": "A4", "source": "user"}],
                "confidence": {"size": 0.96},
                "knowledgeVersion": KNOWLEDGE_VERSION,
                "expectedRevision": before["revision"],
                "patchId": "apply-1",
            },
        })
        payload = response["result"]["structuredContent"]
        self.assertEqual(payload["toolResult"]["status"], "applied")
        self.assertEqual(payload["toolResult"]["acceptedFields"], ["size"])
        self.assertEqual(payload["toolResult"]["rejectedFields"], ["unknownField"])
        self.assertEqual(payload["toolResult"]["revision"], before["revision"] + 1)
        self.assertEqual(payload["order"]["size"], "A4")
        self.assertEqual(payload["fieldMeta"]["size"]["confidence"], 0.96)
        self.assertEqual(payload["fieldMeta"]["size"]["evidence"]["quote"], "A4")
        self.assertEqual(payload["planMeta"]["knowledgeVersion"], KNOWLEDGE_VERSION)
        self.assertTrue(any(event.get("step") == "patch"
                            for event in payload["runTrace"]))

    def test_revision_conflict_does_not_mutate_session(self) -> None:
        sid = "cas"
        agent = Agent(Memory(self.db), sid)
        agent.chat("做 500 张名片")
        before = Agent(Memory(self.db), sid).state
        server = PrintOpsMCP(self.db, bound_session_id=sid, capabilities="L0,L1")
        self.initialize(server)
        response = self.request(server, 2, "tools/call", {
            "name": "apply_order_patch",
            "arguments": {"sessionId": sid, "patch": {"size": "A4"},
                          "expectedRevision": before["revision"] + 1},
        })
        self.assertEqual(response["error"]["data"]["code"], "revision_conflict")
        self.assertEqual(response["error"]["data"]["revision"], before["revision"])
        after = Agent(Memory(self.db), sid).state
        self.assertEqual(after["order"], before["order"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["runHistory"], before["runHistory"])

    def test_forbidden_whole_items_patch_is_rejected_atomically(self) -> None:
        sid = "whole-items"
        agent = Agent(Memory(self.db), sid)
        agent.chat("做 500 张名片")
        before = Agent(Memory(self.db), sid).state
        server = PrintOpsMCP(self.db, bound_session_id=sid, capabilities="L0,L1")
        self.initialize(server)
        response = self.request(server, 2, "tools/call", {
            "name": "apply_order_patch",
            "arguments": {
                "sessionId": sid,
                "patch": {"items": [{"itemId": "evil", "productType": "标签"}],
                          "size": "A4"},
                "expectedRevision": before["revision"],
            },
        })
        self.assertEqual(response["error"]["data"]["code"], "invalid_arguments")
        after = Agent(Memory(self.db), sid).state
        self.assertEqual(after["order"], before["order"])
        self.assertEqual(after["revision"], before["revision"])

    def test_item_id_patch_is_scoped_to_one_product(self) -> None:
        sid = "items"
        agent = Agent(Memory(self.db), sid)
        agent.chat("做 500 张名片和 1000 张折页，A4，157g哑粉纸，双面四色，下周内")
        before = Agent(Memory(self.db), sid).state
        item_id = before["order"]["items"][1]["itemId"]
        server = PrintOpsMCP(self.db, bound_session_id=sid, capabilities="L0,L1")
        self.initialize(server)
        response = self.request(server, 2, "tools/call", {
            "name": "apply_order_patch",
            "arguments": {
                "sessionId": sid, "itemId": item_id,
                "patch": {"pages": "4"},
                "evidence": {"pages": {"quote": "4页", "source": "user"}},
                "confidence": {"pages": 1.0},
                "expectedRevision": before["revision"],
            },
        })
        payload = response["result"]["structuredContent"]
        self.assertEqual(payload["toolResult"]["status"], "applied")
        self.assertEqual(payload["toolResult"]["acceptedFields"], [f"items.{item_id}.pages"])
        self.assertEqual(payload["order"]["items"][1]["pages"], "4")
        self.assertNotEqual(payload["order"]["items"][0].get("pages"), "4")
        self.assertEqual(payload["fieldMeta"][f"items.{item_id}.pages"]["evidence"]["quote"], "4页")

    def test_patch_id_retry_is_idempotent(self) -> None:
        sid = "retry"
        agent = Agent(Memory(self.db), sid)
        agent.chat("做 500 张名片")
        before = Agent(Memory(self.db), sid).state
        server = PrintOpsMCP(self.db, bound_session_id=sid, capabilities="L0,L1")
        self.initialize(server)
        args = {"sessionId": sid, "patch": {"size": "A4"},
                "expectedRevision": before["revision"], "patchId": "retry-1"}
        first = self.request(server, 2, "tools/call", {"name": "apply_order_patch", "arguments": args})
        first_payload = first["result"]["structuredContent"]
        retry = self.request(server, 3, "tools/call", {"name": "apply_order_patch", "arguments": args})
        retry_payload = retry["result"]["structuredContent"]
        self.assertEqual(first_payload["revision"], retry_payload["revision"])
        self.assertTrue(retry_payload["toolResult"]["idempotent"])
        self.assertEqual(Agent(Memory(self.db), sid).state["revision"], first_payload["revision"])

    def test_two_mcp_processes_share_sqlite_cas(self) -> None:
        sid = "cross-process"
        agent = Agent(Memory(self.db), sid)
        agent.chat("做 500 张名片")
        before = Agent(Memory(self.db), sid).state["revision"]
        servers = [
            PrintOpsMCP(self.db, bound_session_id=sid, capabilities="L0,L1"),
            PrintOpsMCP(self.db, bound_session_id=sid, capabilities="L0,L1"),
        ]
        for server in servers:
            self.initialize(server)
        barrier = threading.Barrier(2)
        responses = []

        def invoke(index: int) -> None:
            barrier.wait()
            responses.append(self.request(
                servers[index], index + 2, "tools/call", {
                    "name": "apply_order_patch",
                    "arguments": {
                        "sessionId": sid,
                        "patch": {"size": f"A{4 + index}"},
                        "expectedRevision": before,
                        "patchId": f"cross-{index}",
                    },
                }))

        threads = [threading.Thread(target=invoke, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        successes = [item for item in responses if "result" in item]
        conflicts = [item for item in responses if "error" in item]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["error"]["data"]["code"], "revision_conflict")
        final = Agent(Memory(self.db), sid).state
        self.assertEqual(final["revision"], before + 1)
        self.assertIn(final["order"]["size"], {"A4", "A5"})

    def test_reusing_patch_id_with_different_content_is_rejected(self) -> None:
        sid = "id-conflict"
        agent = Agent(Memory(self.db), sid)
        agent.chat("做 500 张名片")
        before = Agent(Memory(self.db), sid).state
        server = PrintOpsMCP(self.db, bound_session_id=sid, capabilities="L0,L1")
        self.initialize(server)
        base = {
            "sessionId": sid, "patch": {"size": "A4"},
            "expectedRevision": before["revision"], "patchId": "same-id",
        }
        first = self.request(server, 2, "tools/call",
                            {"name": "apply_order_patch", "arguments": base})
        self.assertIn("result", first)
        changed = dict(base)
        changed["patch"] = {"size": "A5"}
        changed["expectedRevision"] = first["result"]["structuredContent"]["revision"]
        second = self.request(server, 3, "tools/call",
                             {"name": "apply_order_patch", "arguments": changed})
        self.assertEqual(second["error"]["data"]["code"], "idempotency_conflict")
        final = Agent(Memory(self.db), sid).state
        self.assertEqual(final["order"]["size"], "A4")
        self.assertEqual(final["revision"], first["result"]["structuredContent"]["revision"])

    def test_semantically_invalid_quantity_patch_does_not_write_order(self) -> None:
        sid = "bad-quantity"
        agent = Agent(Memory(self.db), sid)
        agent.chat("做 500 张名片")
        before = Agent(Memory(self.db), sid).state
        server = PrintOpsMCP(self.db, bound_session_id=sid, capabilities="L0,L1")
        self.initialize(server)
        response = self.request(server, 2, "tools/call", {
            "name": "apply_order_patch",
            "arguments": {
                "sessionId": sid, "patch": {"quantity": "not-a-quantity"},
                "expectedRevision": before["revision"],
            },
        })
        self.assertEqual(response["error"]["data"]["code"], "invalid_patch")
        final = Agent(Memory(self.db), sid).state
        self.assertEqual(final["order"], before["order"])
        self.assertEqual(final["revision"], before["revision"])

    def test_malformed_provenance_is_rejected_before_state_write(self) -> None:
        sid = "bad-meta"
        agent = Agent(Memory(self.db), sid)
        agent.chat("做 500 张名片")
        before = Agent(Memory(self.db), sid).state
        server = PrintOpsMCP(self.db, bound_session_id=sid, capabilities="L0,L1")
        self.initialize(server)
        response = self.request(server, 2, "tools/call", {
            "name": "apply_order_patch",
            "arguments": {
                "sessionId": sid, "patch": {"size": "A4"},
                "confidence": {"size": "not-a-number"},
                "expectedRevision": before["revision"],
            },
        })
        self.assertEqual(response["error"]["data"]["code"], "invalid_patch")
        final = Agent(Memory(self.db), sid).state
        self.assertEqual(final["order"], before["order"])
        self.assertEqual(final["revision"], before["revision"])


if __name__ == "__main__":
    unittest.main()
