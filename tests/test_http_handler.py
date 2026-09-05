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

    # -- static whitelist --------------------------------------------------

    def test_whitelisted_static_files_are_served(self):
        for path in ("/", "/index.html", "/app.js?v=20260905-6", "/styles.css"):
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
