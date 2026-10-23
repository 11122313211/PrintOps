"""Total budgets, slow clients, and isolated model diagnostics."""

import http.client
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent import Agent, Memory
from llm_adapter import OpenAICompatiblePlanner
import request_runtime
from request_runtime import DeadlineExceeded, bounded_io, deadline_scope, deadline_lock
import server


class Response:
    def __init__(self, delay=0):
        self.delay = delay

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self, *_args):
        time.sleep(self.delay)
        return json.dumps({"choices": [{"message": {"content": "本地测试回复"}}]}).encode()


class BudgetTest(unittest.TestCase):
    def test_lock_wait_uses_remaining_request_budget(self):
        lock = threading.Lock()
        lock.acquire()
        started = time.monotonic()
        try:
            with deadline_scope(started + 0.06):
                with self.assertRaises(DeadlineExceeded), deadline_lock(lock):
                    self.fail("must not enter a locked business section")
        finally:
            lock.release()
        self.assertLess(time.monotonic() - started, 0.5)

    def test_all_model_rounds_share_one_budget_and_late_result_is_discarded(self):
        planner = OpenAICompatiblePlanner("https://example.test/v1", "", "test", allow_private_hosts=True)
        started = time.monotonic()
        with patch("llm_adapter.urllib.request.urlopen", side_effect=lambda *a, **k: Response(0.06)) as opener:
            with deadline_scope(started + 0.15):
                self.assertIsNotNone(planner.plan("one", {}, []))
                self.assertIsNotNone(planner.plan("two", {}, []))
                self.assertIsNone(planner.plan("three", {}, []))
                self.assertIsNone(planner.plan("four", {}, []))
            count = opener.call_count
            time.sleep(0.08)  # Let the discarded read finish before unpatching.
        self.assertEqual(count, 3)
        self.assertEqual(planner.last_protocol, "timeout")
        self.assertLess(time.monotonic() - started, 0.6)

    def test_resolver_or_header_stall_does_not_hold_caller_or_grow_workers(self):
        release = threading.Event()
        finished = threading.Event()
        calls = []

        def stalled_io():
            calls.append(1)
            release.wait(2)
            finished.set()
            return "late"

        with patch.object(request_runtime, "_OUTBOUND_SLOTS", threading.BoundedSemaphore(1)):
            try:
                with self.assertRaises(DeadlineExceeded):
                    bounded_io(stalled_io, time.monotonic() + 0.05)
                with self.assertRaises(DeadlineExceeded):
                    bounded_io(stalled_io, time.monotonic() + 0.05)
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
                self.assertTrue(finished.wait(1))
                # Wait for the worker's finally to release the patched slot.
                self.assertTrue(request_runtime._OUTBOUND_SLOTS.acquire(timeout=1))
                request_runtime._OUTBOUND_SLOTS.release()

    def test_expired_request_cannot_commit_agent_state(self):
        memory = Memory(mode="memory")
        agent = Agent(memory, "expired")
        with deadline_scope(time.monotonic() - 1):
            with self.assertRaises(DeadlineExceeded):
                agent.chat("做500张名片")
        self.assertIsNone(memory.store.load_order("expired"))

    def test_config_snapshot_and_diagnostics_are_not_shared(self):
        configured = OpenAICompatiblePlanner("https://example.test/v1", "secret", "old", allow_private_hosts=True)
        with patch.object(server, "PLANNER", configured):
            first, second = server.planner_for_request(), server.planner_for_request()
            configured.model = "new"
            first.last_error, first.last_protocol = "failed", "error"
            self.assertEqual(first.model, "old")
            self.assertEqual(second.model, "old")
            self.assertEqual(second.last_error, "")
            self.assertEqual(configured.last_error, "")
            self.assertEqual(server.planner_for_request().model, "new")

    def test_capability_probe_does_not_hold_configuration_lock(self):
        entered, release = threading.Event(), threading.Event()
        configured = OpenAICompatiblePlanner("https://example.test/v1", "", "test", allow_private_hosts=True)
        outputs = []

        def probe(local):
            entered.set()
            release.wait(1)
            local.last_error = "probe-local"
            return {"ok": False}

        with patch.object(server, "PLANNER", configured), patch.object(OpenAICompatiblePlanner, "test_capabilities", probe):
            worker = threading.Thread(target=lambda: outputs.append(server.test_llm_capabilities({})), daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                with deadline_scope(time.monotonic() + 0.1):
                    self.assertEqual(server.llm_settings()["llm"]["model"], "test")
                self.assertEqual(configured.last_error, "")
            finally:
                release.set()
                worker.join(1)
            self.assertEqual(outputs[0]["llm"]["lastError"], "probe-local")
            self.assertEqual(configured.last_error, "")


class SocketDeadlineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory_patch = patch.object(server, "MEMORY", Memory(Path(self.temp.name) / "orders.sqlite3"))
        self.memory_patch.start()
        self.addCleanup(self.memory_patch.stop)

    def serve(self):
        class ShortBudgetHandler(server.Handler):
            timeout = 0.22

        try:
            httpd = server.PrintOpsHTTPServer(("127.0.0.1", 0), ShortBudgetHandler, max_connections=1)
        except OSError as error:
            self.skipTest(f"loopback unavailable: {error}")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd

    def drip(self, prefix):
        httpd = self.serve()
        client = socket.create_connection(httpd.server_address, timeout=1)
        stop = threading.Event()
        client.sendall(prefix)

        def send_bytes():
            while not stop.wait(0.035):
                try:
                    client.sendall(b" ")
                except OSError:
                    return

        sender = threading.Thread(target=send_bytes, daemon=True)
        started = time.monotonic()
        sender.start()
        received = b""
        self.last_reset = False
        try:
            while True:
                try:
                    chunk = client.recv(4096)
                except ConnectionResetError:
                    # Windows surfaces a deadline abort as WSAECONNRESET.
                    # shutdown(SHUT_RD) with a still-dribbling peer resets the
                    # whole connection there, so the structured 408 may never
                    # arrive; that abort itself is the deadline enforcement.
                    self.last_reset = True
                    break
                if not chunk:
                    break
                received += chunk
        finally:
            stop.set()
            client.close()
            sender.join(1)
        self.assertLess(time.monotonic() - started, 0.8)
        limit = time.monotonic() + 0.5
        while not httpd.has_free_connection_slot() and time.monotonic() < limit:
            time.sleep(0.01)
        self.assertTrue(httpd.has_free_connection_slot())
        return received

    def test_header_drip_cannot_reset_total_deadline(self):
        self.drip(b"GET /api/health HTTP/1.1\r\nHost: localhost\r\nX-Slow: ")

    def test_body_drip_cannot_reset_total_deadline(self):
        prefix = (f"POST /api/chat HTTP/1.1\r\nHost: localhost\r\n"
                  f"{server.TOKEN_HEADER}: {server.ACCESS_TOKEN}\r\n"
                  "Content-Type: application/json\r\nContent-Length: 1000\r\n\r\n{").encode()
        received = self.drip(prefix)
        if self.last_reset:
            # The connection was aborted at the deadline (Windows behaviour):
            # the response cannot arrive, but the limit was enforced.
            self.assertEqual(received, b"")
        else:
            self.assertIn(b"408", received)
            self.assertIn(b"REQUEST_DEADLINE", received)

    def test_waiting_session_request_times_out_without_mutation(self):
        httpd = self.serve()
        agent = Agent(server.MEMORY, "busy-session")
        agent.chat("做500张名片")
        before = server.MEMORY.store.load_order(agent.id)
        lock = server._session_lock(agent.id)
        lock.acquire()
        client = http.client.HTTPConnection(*httpd.server_address, timeout=1)
        try:
            client.request("POST", "/api/chat", json.dumps({"sessionId": agent.id,
                "expectedRevision": before["revision"], "text": "数量改成800张"}),
                {server.TOKEN_HEADER: server.ACCESS_TOKEN, "Content-Type": "application/json"})
            response = client.getresponse()
            result = json.loads(response.read())
            self.assertEqual(response.status, 408)
            self.assertEqual(result["code"], "REQUEST_DEADLINE")
        finally:
            lock.release()
            client.close()
        self.assertEqual(server.MEMORY.store.load_order(agent.id), before)

    def test_sqlite_write_lock_uses_total_budget_and_preserves_existing_order(self):
        agent = Agent(server.MEMORY, "db-busy")
        agent.chat("做500张名片")
        before = server.MEMORY.store.load_order(agent.id)
        connection = sqlite3.connect(server.MEMORY.path)
        connection.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        try:
            with deadline_scope(started + 0.1), self.assertRaises(DeadlineExceeded):
                agent.chat("数量改成800张")
        finally:
            connection.rollback()
            connection.close()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(server.MEMORY.store.load_order(agent.id), before)


if __name__ == "__main__":
    unittest.main()
