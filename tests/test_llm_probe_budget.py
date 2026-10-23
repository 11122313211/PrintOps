"""Capability-probe classification, route budgets, and key-file write safety.

These lock the Windows/API-key timeout fixes: probes fail fast with a named
root cause, the model-test route carries its own capped deadline, and the
plaintext key file is only ever visible at its final path with owner-only
permissions.
"""

import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import llm_adapter
import server
from llm_adapter import OpenAICompatiblePlanner
from request_runtime import current_deadline, deadline_scope


def make_planner():
    return OpenAICompatiblePlanner("https://example.test/v1", "k", "m", allow_private_hosts=True)


class ProbeClassification(unittest.TestCase):
    def test_unreachable_endpoint_stops_after_first_probe(self):
        def refused(*_args, **_kwargs):
            raise URLError(ConnectionRefusedError(111, "refused"))

        planner = make_planner()
        with patch.object(llm_adapter.urllib.request, "urlopen", side_effect=refused) as opener:
            result = planner.test_capabilities()
        self.assertEqual(opener.call_count, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["chosenProtocol"], "unreachable")
        self.assertIn("连接被拒绝", result["message"])
        self.assertIn("已跳过后续检查", result["message"])

    def test_timeout_endpoint_reports_timeout_once(self):
        def timeout(*_args, **_kwargs):
            raise URLError(socket.timeout("timed out"))

        planner = make_planner()
        with patch.object(llm_adapter.urllib.request, "urlopen", side_effect=timeout) as opener:
            result = planner.test_capabilities()
        self.assertEqual(opener.call_count, 1)
        self.assertIn("超时", result["message"])
        self.assertEqual(result["chosenProtocol"], "unreachable")

    def test_rejected_api_key_is_named_and_probes_stop(self):
        def unauthorized(*_args, **_kwargs):
            raise HTTPError("https://example.test/v1/chat/completions", 401, "Unauthorized", {}, None)

        planner = make_planner()
        with patch.object(llm_adapter.urllib.request, "urlopen", side_effect=unauthorized) as opener:
            result = planner.test_capabilities()
        self.assertEqual(opener.call_count, 1)
        self.assertIn("鉴权失败", result["message"])
        self.assertIn("API Key", result["message"])
        self.assertEqual(result["chosenProtocol"], "unavailable")

    def test_protocol_errors_still_run_remaining_probes(self):
        def not_found(*_args, **_kwargs):
            raise HTTPError("https://example.test/v1/chat/completions", 404, "Not Found", {}, None)

        planner = make_planner()
        with patch.object(llm_adapter.urllib.request, "urlopen", side_effect=not_found) as opener:
            result = planner.test_capabilities()
        # chat + json + native; the continuation probe only runs after a
        # successful native tool call, so three requests is the full run.
        self.assertEqual(opener.call_count, 3)
        self.assertIn("HTTP 404", result["errors"]["chat"])


class SaveValidation(unittest.TestCase):
    def test_save_reports_validation_timeout_not_generic_deadline(self):
        with patch.object(server, "PLANNER", OpenAICompatiblePlanner()), \
             patch.object(OpenAICompatiblePlanner, "configure",
                          side_effect=lambda *_a: time.sleep(0.5)):
            with deadline_scope(time.monotonic() + 0.05):
                with self.assertRaises(server.RequestError) as ctx:
                    server.update_llm_settings({"url": "https://slow-dns.test/v1", "model": "m", "key": "k"})
        self.assertEqual(ctx.exception.code, "ADDRESS_VALIDATION_TIMEOUT")
        self.assertEqual(ctx.exception.status, 504)


class RouteDeadline(unittest.TestCase):
    def handler(self, timeout):
        instance = server.Handler.__new__(server.Handler)
        instance.timeout = timeout
        instance._request_received = time.monotonic()
        instance._deadline_timer = None
        return instance

    def test_probe_route_raises_budget_within_cap(self):
        instance = self.handler(30.0)
        with deadline_scope(instance._request_received + 30.0):
            instance._extend_request_deadline(40.0)
            self.assertGreater(current_deadline(), instance._request_received + 39.0)
            instance._cancel_deadline_timer()

    def test_route_never_exceeds_cap(self):
        instance = self.handler(30.0)
        with deadline_scope(instance._request_received + 30.0):
            instance._extend_request_deadline(500.0)
            self.assertLessEqual(current_deadline(),
                                 instance._request_received + server.ROUTE_DEADLINE_CAP_SECONDS + 0.01)
            instance._cancel_deadline_timer()

    def test_tiny_handler_budget_is_not_extended(self):
        instance = self.handler(0.22)
        with deadline_scope(instance._request_received + 0.22):
            instance._extend_request_deadline(40.0)
            self.assertLessEqual(current_deadline(), instance._request_received + 1.0)


class ModelTestRouteIntegration(unittest.TestCase):
    def test_slow_probe_survives_global_timeout_on_probe_route(self):
        class SlowResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit=-1):
                time.sleep(8)
                return json.dumps({"choices": [{"message": {"content": "PRINTOPS_CAPABILITY_CHAT_OK"}}]}).encode()

        class MediumBudgetHandler(server.Handler):
            timeout = 6.0

        try:
            httpd = server.PrintOpsHTTPServer(("127.0.0.1", 0), MediumBudgetHandler, max_connections=4)
        except OSError as error:
            self.skipTest(f"loopback unavailable: {error}")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        started = time.monotonic()
        with patch.object(llm_adapter.urllib.request, "urlopen", return_value=SlowResponse()):
            connection = http.client.HTTPConnection(*httpd.server_address, timeout=30)
            try:
                connection.request("POST", "/api/model/test",
                                   json.dumps({"url": "https://example.test/v1", "model": "m", "key": "k"}),
                                   {server.TOKEN_HEADER: server.ACCESS_TOKEN, "Content-Type": "application/json"})
                response = connection.getresponse()
                payload = json.loads(response.read())
            finally:
                connection.close()
        elapsed = time.monotonic() - started
        self.assertEqual(response.status, 200, payload)
        self.assertEqual(payload["test"]["chosenProtocol"], "unreachable")
        self.assertLess(elapsed, 12.0)


class KeyFileSafety(unittest.TestCase):
    def test_write_is_atomic_and_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "llm_config.json"
            llm_adapter.write_saved_config(target, {"url": "https://x.test/v1", "model": "m", "key": "sk-1"})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["key"], "sk-1")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["llm_config.json"])
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o077, 0)

    def test_failed_write_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "llm_config.json"
            with patch.object(llm_adapter.os, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    llm_adapter.write_saved_config(target, {"key": "sk-1"})
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_read_self_heals_loose_posix_mode(self):
        if os.name == "nt":
            self.skipTest("POSIX permission bits do not exist on Windows")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "llm_config.json"
            target.write_text(json.dumps({"url": "https://x.test/v1", "model": "m", "key": "sk-1"}), encoding="utf-8")
            os.chmod(target, 0o644)
            config = llm_adapter.read_saved_config(target)
            self.assertEqual(config.get("key"), "sk-1")
            self.assertEqual(target.stat().st_mode & 0o077, 0)


class PlannerEnv(unittest.TestCase):
    def test_llm_timeout_env_clamped(self):
        base = {"PRINTOPS_LLM_URL": "", "PRINTOPS_LLM_MODEL": "", "PRINTOPS_LLM_KEY": ""}
        with patch.dict(os.environ, {**base, "PRINTOPS_LLM_TIMEOUT": "45"}):
            self.assertEqual(OpenAICompatiblePlanner.from_env().timeout, 45.0)
        with patch.dict(os.environ, {**base, "PRINTOPS_LLM_TIMEOUT": "9999"}):
            self.assertEqual(OpenAICompatiblePlanner.from_env().timeout, 120.0)
        with patch.dict(os.environ, {**base, "PRINTOPS_LLM_TIMEOUT": "bad"}):
            self.assertEqual(OpenAICompatiblePlanner.from_env().timeout, 20.0)


if __name__ == "__main__":
    unittest.main()
