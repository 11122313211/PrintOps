"""Socket-level tests for the real HTTP handler.

tests/test_server.py exercises the dispatch functions directly; these tests
start a real ThreadingHTTPServer so the security boundary itself is covered:
token enforcement, cross-origin rejection, the static whitelist (no /.git,
source files or data downloads), and the request guards (411/413/415).
"""

import http.client
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

import server
from agent import Memory


class HttpGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.previous_memory = server.MEMORY
        cls.previous_locks = server.SESSION_LOCKS
        cls.previous_planner = server.PLANNER
        server.MEMORY = Memory(Path(cls.tmp.name) / "agent.sqlite3")
        server.SESSION_LOCKS = {}
        server.PLANNER = server.OpenAICompatiblePlanner()
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.MEMORY = cls.previous_memory
        server.SESSION_LOCKS = cls.previous_locks
        server.PLANNER = cls.previous_planner
        cls.tmp.cleanup()

    def request(self, path, method="POST", body=None, headers=None, token="auto", origin=None):
        """Send one request over a raw connection; return (status, json-or-None)."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = dict(headers or {})
        if token == "auto":
            headers["X-PrintOps-Token"] = server.ACCESS_TOKEN
        elif token is not None:
            headers["X-PrintOps-Token"] = token
        if origin is not None:
            headers["Origin"] = origin
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, (json.loads(raw) if raw and raw.lstrip()[:1] == b"{" else None)
        finally:
            connection.close()

    # -- token enforcement -------------------------------------------------

    def test_health_is_open_but_other_api_requires_token(self):
        status, payload = self.request("/api/health", method="GET", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True})

        status, payload = self.request("/api/products", method="GET", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "UNAUTHORIZED")

        status, payload = self.request("/api/products", method="GET", token="wrong-token")
        self.assertEqual(status, 401)

        status, payload = self.request("/api/products", method="GET")
        self.assertEqual(status, 200)
        self.assertTrue(payload["products"])

    def test_tool_stats_requires_token_and_reports_local_scope(self):
        status, payload = self.request("/api/tools/stats", method="GET", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "UNAUTHORIZED")
        status, payload = self.request("/api/tools/stats", method="GET", token="wrong-token")
        self.assertEqual(status, 401)
        status, payload = self.request("/api/tools/stats", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(payload["scope"], "local-only")
        self.assertEqual(payload["windowDays"], 30)
        self.assertTrue(payload["message"])

    def test_page_carries_token_and_it_is_accepted(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as response:
            html = response.read().decode("utf-8")
        marker = "window.__PRINTOPS_TOKEN__="
        self.assertIn(marker, html)
        token = html.split(marker, 1)[1].split('"', 2)[1]
        status, payload = self.request("/api/session", body={}, token=token)
        self.assertEqual(status, 200)
        self.assertTrue(payload["sessionId"])

    def test_post_api_requires_token(self):
        status, payload = self.request("/api/session", body={}, token=None)
        self.assertEqual(status, 401)
        status, _ = self.request("/api/settings", body={"url": "", "model": ""}, token=None)
        self.assertEqual(status, 401)

    # -- origin check ------------------------------------------------------

    def test_cross_origin_post_is_rejected(self):
        status, payload = self.request("/api/session", body={}, origin="http://evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "FORBIDDEN_ORIGIN")

    def test_same_origin_post_is_accepted(self):
        status, payload = self.request("/api/session", body={}, origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(status, 200)

    # -- host allowlist (TD-26: DNS rebinding) -----------------------------

    def _with_host(self, host):
        """One request whose Host header is overridden (http.client auto-fills
        Host with 127.0.0.1, so rebinding forms need an explicit override)."""
        return self.request("/api/session", body={}, headers={"Host": host},
                            origin=f"http://{host}")

    def test_rebinding_host_is_rejected_on_api_post(self):
        # Origin == Host == attacker domain: the old same-origin check passed
        # by construction; the Host allowlist is what now rejects it.
        status, payload = self._with_host("attacker.com:4174")
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "FORBIDDEN_HOST")

    def test_rebinding_host_is_rejected_on_static_page(self):
        # The token-carrying index.html must never reach a non-loopback Host,
        # even though static paths skip the token guard.
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("GET", "/", headers={"Host": "attacker.com:4174",
                                                "Origin": "http://attacker.com:4174"})
        response = connection.getresponse()
        raw = response.read()
        self.assertEqual(response.status, 403)
        self.assertIn(b"FORBIDDEN_HOST", raw)
        self.assertNotIn(b"__PRINTOPS_TOKEN__", raw)

    def test_loopback_host_variants_are_accepted(self):
        for host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"):
            status, _ = self._with_host(host)
            self.assertEqual(status, 200, host)

    # -- static whitelist --------------------------------------------------

    def test_whitelisted_static_files_are_served(self):
        for path in ("/", "/index.html", "/app.js?v=20260905-6", "/markdown.js?v=20260918-1", "/styles.css"):
            status, _ = self.request(path, method="GET")
            self.assertEqual(status, 200, path)

    def test_non_whitelisted_paths_return_404(self):
        for path in ("/.git/config", "/.gitignore", "/server.py", "/agent.py",
                     "/tools/secret_scan.py", "/docs/ROADMAP.md", "/README.md",
                     "/data/agent.sqlite3", "/data", "/favicon.ico", "/nope.txt"):
            status, _ = self.request(path, method="GET")
            self.assertEqual(status, 404, path)

    def test_post_to_static_path_returns_404(self):
        status, payload = self.request("/", body={})
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "NOT_FOUND")

    # -- request guards ----------------------------------------------------

    def test_deeply_nested_json_returns_400_not_500(self):
        # TD-28: RecursionError is a RuntimeError, not ValueError — before the
        # fix it escaped read_json's except tuple and surfaced as a 500.
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        deep = b"[" * 60000 + b"]" * 60000
        try:
            connection.request("POST", "/api/settings", body=deep, headers={
                "Content-Type": "application/json",
                "X-PrintOps-Token": server.ACCESS_TOKEN})
            response = connection.getresponse()
            raw = response.read()
        finally:
            connection.close()
        self.assertEqual(response.status, 400)
        self.assertIn(b"INVALID_JSON", raw)

    def test_orders_pagination_is_clamped(self):
        # TD-28: LIMIT -1 means unbounded in SQLite; the server clamps to
        # 1..500 / offset >= 0 instead of letting one request scan the table.
        for query in ("?limit=-1", "?limit=999999", "?offset=-5", "?limit=-1&offset=-1"):
            status, _ = self.request(f"/api/orders{query}", method="GET")
            self.assertEqual(status, 200, query)

    def test_request_log_lines_survive_control_characters(self):
        # TD-28: %0A in the path decoded into a real newline and could forge a
        # structured log line; the handler now escapes control characters.
        with self.assertLogs(server.LOGGER, level="INFO") as captured:
            self.request("/api/health%0aX-Forged%20yes", method="GET")
        messages = [record.getMessage() for record in captured.records]
        forged = [m for m in messages if "api/health" in m]
        self.assertTrue(forged, "请求日志应包含该路径")
        for message in forged:
            self.assertNotIn("\n", message, "日志行不得包含原始换行")
        self.assertIn("\\u000a", "\n".join(forged), "控制字符应转义而非删除")

    def test_session_locks_evict_only_unused_entries(self):
        # TD-28: the lock dict grows one entry per session; eviction must skip
        # entries another thread is waiting on (marked in-use).
        previous_locks, previous_in_use = server.SESSION_LOCKS, server.SESSION_LOCKS_IN_USE
        server.SESSION_LOCKS, server.SESSION_LOCKS_IN_USE = {}, set()
        try:
            for index in range(server.MAX_SESSION_LOCKS - 1):
                server._session_lock(f"filler-{index}")
            busy = server._session_lock("busy")
            self.assertIn("busy", server.SESSION_LOCKS_IN_USE)
            server._session_lock("arriver")  # full: must evict a filler, never "busy"
            self.assertIn("busy", server.SESSION_LOCKS)
            self.assertIn("arriver", server.SESSION_LOCKS)
            self.assertLessEqual(len(server.SESSION_LOCKS), server.MAX_SESSION_LOCKS + 1)
            self.assertIs(server._session_lock("busy"), busy)
            with server._session_guard("arriver"):
                self.assertIn("arriver", server.SESSION_LOCKS_IN_USE)
            self.assertNotIn("arriver", server.SESSION_LOCKS_IN_USE)
        finally:
            server.SESSION_LOCKS, server.SESSION_LOCKS_IN_USE = previous_locks, previous_in_use

    def test_missing_content_length_returns_411(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.putrequest("POST", "/api/session", skip_accept_encoding=True)
            connection.putheader("Content-Type", "application/json")
            connection.putheader("X-PrintOps-Token", server.ACCESS_TOKEN)
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 411)
            self.assertEqual(json.loads(response.read())["code"], "LENGTH_REQUIRED")
        finally:
            connection.close()

    def test_oversized_body_returns_413(self):
        big = {"text": "做 500 张名片，" + "A" * (server.MAX_BODY_BYTES + 1024)}
        status, payload = self.request("/api/chat", body=big)
        self.assertEqual(status, 413)
        self.assertEqual(payload["code"], "PAYLOAD_TOO_LARGE")

    def test_chat_text_over_limit_returns_400(self):
        status, payload = self.request("/api/chat", body={"text": "做500张名片，" + "长" * (server.MAX_CHAT_CHARS + 1)})
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "MESSAGE_TOO_LONG")

    def test_chat_text_within_limit_is_accepted(self):
        status, payload = self.request("/api/chat", body={"text": "做500张名片，90×54mm，300g铜版纸"})
        self.assertEqual(status, 200)
        self.assertIn("paper", payload["order"])

    def test_wrong_content_type_returns_415(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            body = json.dumps({}, ensure_ascii=False).encode("utf-8")
            connection.request("POST", "/api/session", body=body, headers={
                "Content-Type": "text/plain", "X-PrintOps-Token": server.ACCESS_TOKEN})
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 415)
        finally:
            connection.close()

    def test_invalid_json_returns_400(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request("POST", "/api/session", body="{not json", headers={
                "Content-Type": "application/json", "X-PrintOps-Token": server.ACCESS_TOKEN})
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 400)
            self.assertEqual(payload["code"], "INVALID_JSON")
            self.assertTrue(payload.get("requestId"))
        finally:
            connection.close()

    def test_post_only_route_returns_404_for_get(self):
        status, payload = self.request("/api/chat", method="GET")
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
