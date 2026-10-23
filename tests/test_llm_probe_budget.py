"""Capability-probe classification, route budgets, and key-file write safety.

These lock the Windows/API-key timeout fixes: probes fail fast with a named
root cause, the model-test route carries its own capped deadline, and the
plaintext key file is only ever visible at its final path with owner-only
permissions.
"""

import http.client
import io
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


def generation_calls(opener) -> int:
    """Count model calls, ignoring the header-only reachability pre-flight.

    The pre-flight is what lets a dead endpoint produce a root cause after one
    attempt, so the quota-relevant number is the POST count, not every socket.
    """
    return sum(1 for call in opener.call_args_list
               if getattr(call.args[0], "method", "POST") == "POST")


class ProbeClassification(unittest.TestCase):
    def test_live_endpoint_answers_the_capability_probe_in_two_round_trips(self):
        # Same latency shape as the field case that measured 8.7s: each model
        # hop needs a second.  The pre-flight settles liveness in milliseconds,
        # so three hops overlap and only the continuation stays serial.
        hop_seconds = 1.0
        state = {"live": 0, "peak": 0}
        guard = threading.Lock()
        hops: list[str] = []

        def scripted(request, timeout):
            if getattr(request, "method", "POST") == "GET":
                return _Json({"data": []})
            body = json.loads(request.data.decode("utf-8"))
            hop = _hop_of(body)
            with guard:
                hops.append(hop)
                state["live"] += 1
                state["peak"] = max(state["peak"], state["live"])
            time.sleep(hop_seconds)
            with guard:
                state["live"] -= 1
            return _hop_response(hop)

        planner = OpenAICompatiblePlanner("https://example.test/v1", "k", "m",
                                          allow_private_hosts=True)
        started = time.monotonic()
        with patch.object(llm_adapter.urllib.request, "urlopen", side_effect=scripted):
            result = planner.test_capabilities()
            elapsed = time.monotonic() - started
        self.assertEqual(sorted(hops), ["chat", "continuation", "json", "native"])
        self.assertEqual(result["chosenProtocol"], "native_tools")
        self.assertGreaterEqual(state["peak"], 3)
        # Serial hops need four seconds here; the parallel probe needs about two.
        self.assertLess(elapsed, 3.0, f"parallel probe took {elapsed:.2f}s")
        self.assertGreaterEqual(result["probeTimings"]["reachMs"], 0)

    def test_unreachable_endpoint_stops_after_first_probe(self):
        def refused(*_args, **_kwargs):
            raise URLError(ConnectionRefusedError(111, "refused"))

        planner = make_planner()
        with patch.object(llm_adapter.urllib.request, "urlopen", side_effect=refused) as opener:
            result = planner.test_capabilities()
        # "<= 1": whether the chat hop itself reaches the transport is a
        # scheduling race; that the fan-out never happens is not.
        self.assertLessEqual(generation_calls(opener), 1)
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
        self.assertLessEqual(generation_calls(opener), 1)
        self.assertIn("超时", result["message"])
        self.assertEqual(result["chosenProtocol"], "unreachable")

    def test_rejected_api_key_is_named_and_probes_stop(self):
        def unauthorized(request, *_args, **_kwargs):
            if getattr(request, "method", "POST") == "GET":
                return _Json({"data": []})
            raise HTTPError("https://example.test/v1/chat/completions",
                            401, "Unauthorized", {}, None)

        planner = make_planner()
        with patch.object(llm_adapter.urllib.request, "urlopen", side_effect=unauthorized) as opener:
            result = planner.test_capabilities()
        # A 401 on the base path proves nothing about the key, so the pre-flight
        # fails open and the completion hops carry the auth verdict.  Whether
        # the JSON/tool probes were already in flight when the chat hop came
        # back is a race; the invariant is that they never grow past the three
        # independent hops and the tool continuation never runs.
        self.assertLessEqual(generation_calls(opener), 3)
        self.assertIn("鉴权失败", result["message"])
        self.assertIn("API Key", result["message"])
        self.assertEqual(result["chosenProtocol"], "unavailable")
        self.assertLessEqual(result["probeTimings"]["hops"], 3)
        self.assertFalse(result["toolContinuation"])

    def test_protocol_errors_still_run_remaining_probes(self):
        def not_found(*_args, **_kwargs):
            raise HTTPError("https://example.test/v1/chat/completions", 404, "Not Found", {}, None)

        planner = make_planner()
        with patch.object(llm_adapter.urllib.request, "urlopen", side_effect=not_found) as opener:
            result = planner.test_capabilities()
        # chat + json + native; the continuation probe only runs after a
        # successful native tool call, so three model calls is the full run.
        self.assertEqual(generation_calls(opener), 3)
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
        # Every hop answers in 8s: past the handler budget (6s) and past the
        # probe floor (6s), inside the probe ceiling (15s).  The route must
        # outlive both and report the capability it actually observed instead of
        # manufacturing a timeout, and it must not need four serial round trips.
        self.assertEqual(payload["test"]["chosenProtocol"], "chat_only")
        self.assertTrue(payload["test"]["chat"])
        self.assertLess(elapsed, 14.0)


def _mkstemp_allowed() -> bool:
    """Report whether this environment can create a private temporary file.

    tempfile.mkstemp opens its file with O_TEMPORARY, which a restricted
    sandbox can forbid outright. KeyFileSafety drives the real Windows ACL
    path, so it is skipped when the environment cannot even create that
    temp file - exactly like the loopback-socket cases in
    test_runtime_hardening.py.
    """
    try:
        with tempfile.TemporaryDirectory() as probe_dir:
            fd, probe_file = tempfile.mkstemp(prefix=".llm-config-", dir=probe_dir)
        os.close(fd)
        os.unlink(probe_file)
        return True
    except OSError:
        return False


@unittest.skipUnless(_mkstemp_allowed(), "environment forbids private temporary files")
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


def _hop_of(body: dict) -> str:
    """Name a probe hop from its own request, independent of fan-out order."""
    messages = body.get("messages") or []
    if body.get("tools") or body.get("functions"):
        roles = {item.get("role") for item in messages if isinstance(item, dict)}
        return "continuation" if roles & {"tool", "function"} else "native"
    joined = json.dumps(messages, ensure_ascii=False)
    return "json" if "PRINTOPS_CAPABILITY_JSON_OK" in joined else "chat"


class _Json:
    """Smallest response object the probe transport accepts."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> "_Json":
        return self

    def __exit__(self, *_args) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]


def _hop_response(hop: str) -> _Json:
    if hop == "json":
        return _Json({"choices": [{"message": {"content":
            '{"reply":"PRINTOPS_CAPABILITY_JSON_OK","patch":{},"tool":null}'}}]})
    if hop == "native":
        return _Json({"choices": [{"message": {"content": None, "tool_calls": [{
            "id": "probe-call", "type": "function",
            "function": {"name": "printops_capability_probe",
                         "arguments": '{"token":"printops-probe-v1"}'}}]}}]})
    if hop == "continuation":
        return _Json({"choices": [{"message": {
            "content": "PRINTOPS_CAPABILITY_CONTINUATION_OK"}}]})
    return _Json({"choices": [{"message": {"content": "PRINTOPS_CAPABILITY_CHAT_OK"}}]})


class ProbeParallelism(unittest.TestCase):
    def test_independent_probes_overlap_behind_the_reachability_pre_flight(self):
        hop_seconds = 2.0
        state = {"live": 0, "peak": 0}
        guard = threading.Lock()
        hops: list[str] = []

        def scripted(request, timeout):
            if getattr(request, "method", "POST") == "GET":
                return _Json({"data": []})  # reachability pre-flight
            body = json.loads(request.data.decode("utf-8"))
            hop = _hop_of(body)
            with guard:
                hops.append(hop)
                state["live"] += 1
                state["peak"] = max(state["peak"], state["live"])
            time.sleep(hop_seconds)
            with guard:
                state["live"] -= 1
            return _hop_response(hop)

        planner = OpenAICompatiblePlanner("https://example.test/v1", "k", "m",
                                          allow_private_hosts=True)
        started = time.monotonic()
        with patch.object(llm_adapter.urllib.request, "urlopen", side_effect=scripted):
            result = planner.test_capabilities()
        elapsed = time.monotonic() - started
        self.assertEqual(sorted(hops), ["chat", "continuation", "json", "native"])
        self.assertEqual(result["chosenProtocol"], "native_tools")
        self.assertTrue(result["agentReady"])
        # Four serial hops of this shape measured 8.7s in the field.  Only the
        # continuation depends on an earlier answer, so the pre-flight lets the
        # three independent hops overlap and the probe lands in about two hops.
        self.assertGreaterEqual(state["peak"], 2)
        self.assertLess(elapsed, 3 * hop_seconds)
        timings = result["probeTimings"]
        self.assertEqual(timings["hops"], 4)
        for key in ("gateMs", "chatMs", "jsonMs", "nativeMs", "continuationMs"):
            self.assertIsInstance(timings[key], int)

    def test_dead_endpoint_still_costs_exactly_one_request(self):
        def refused(*_args, **_kwargs):
            raise URLError(ConnectionRefusedError(111, "refused"))

        planner = OpenAICompatiblePlanner("https://example.test/v1", "k", "m",
                                          allow_private_hosts=True)
        with patch.object(llm_adapter.urllib.request, "urlopen", side_effect=refused) as opener:
            started = time.monotonic()
            result = planner.test_capabilities()
            elapsed = time.monotonic() - started
        self.assertLessEqual(generation_calls(opener), 1)
        self.assertEqual(result["probeTimings"]["hops"], 1)
        self.assertLess(elapsed, OpenAICompatiblePlanner.CAPABILITY_PROBE_GATE_SECONDS + 0.5)


class ProbeReachability(unittest.TestCase):
    def test_a_timeout_names_the_connect_budget(self):
        planner = make_planner()
        with patch.object(llm_adapter.urllib.request, "urlopen",
                          side_effect=URLError(socket.timeout("timed out"))):
            reachable, message, kind, latency = planner._probe_reachability(
                planner.base_url, time.monotonic() + 6.0)
        self.assertFalse(reachable)
        self.assertEqual(kind, "network")
        self.assertIn("内未能建立连接", message)
        self.assertIsInstance(latency, int)

    def test_any_http_answer_proves_the_endpoint_is_live(self):
        for code in (401, 404, 405):
            planner = make_planner()
            with patch.object(llm_adapter.urllib.request, "urlopen",
                              side_effect=HTTPError("https://example.test/v1", code, "Nope", {}, None)):
                reachable, message, kind, _latency = planner._probe_reachability(
                    planner.base_url, time.monotonic() + 6.0)
            self.assertTrue(reachable, code)
            self.assertEqual(message, "")
            self.assertEqual(kind, "")

    def test_a_broken_transport_fails_open_instead_of_vetoing_the_probe(self):
        planner = make_planner()
        with patch.object(llm_adapter.urllib.request, "urlopen",
                          side_effect=ValueError("injected transport bug")):
            reachable, _message, _kind, _latency = planner._probe_reachability(
                planner.base_url, time.monotonic() + 6.0)
        self.assertTrue(reachable)


    def test_a_rejection_releases_the_socket_it_carries(self):
        # urllib hands an HTTPError an unread body. Without an explicit close
        # every provider rejection keeps a live connection until the traceback
        # is collected, and a parallel fan-out multiplies that.
        body = io.BytesIO(b'{"error":{"code":"invalid_api_key"}}')
        planner = make_planner()
        with patch.object(llm_adapter.urllib.request, "urlopen",
                          side_effect=HTTPError("https://example.test/v1", 401,
                                                "Unauthorized", {}, body)):
            reachable, _message, _kind, _latency = planner._probe_reachability(
                planner.base_url, time.monotonic() + 6.0)
        self.assertTrue(reachable)
        self.assertTrue(body.closed)

    def test_a_rejected_capability_hop_releases_its_socket_too(self):
        body = io.BytesIO(b'{"error":{"code":"invalid_api_key"}}')

        def scripted(request, *_args, **_kwargs):
            if getattr(request, "method", "POST") == "GET":
                return _Json({"data": []})
            raise HTTPError("https://example.test/v1/chat/completions",
                            401, "Unauthorized", {}, body)

        planner = make_planner()
        with patch.object(llm_adapter.urllib.request, "urlopen", side_effect=scripted):
            result = planner.test_capabilities()
        self.assertEqual(result["chosenProtocol"], "unavailable")
        self.assertTrue(body.closed)


class ProbeHopBudget(unittest.TestCase):
    def recorded(self, planner):
        seen: list[float] = []

        def scripted(request, timeout):
            if getattr(request, "method", "POST") == "GET":
                return _Json({"data": []})  # the pre-flight keeps its own budget
            seen.append(timeout)
            body = json.loads(request.data.decode("utf-8"))
            return _hop_response(_hop_of(body))

        with patch.object(llm_adapter.urllib.request, "urlopen", side_effect=scripted):
            planner.test_capabilities()
        return seen

    def test_each_hop_gets_at_least_the_probe_floor(self):
        seen = self.recorded(OpenAICompatiblePlanner("https://example.test/v1", "k", "m",
                                                     timeout=1, allow_private_hosts=True))
        self.assertTrue(seen)
        self.assertTrue(all(timeout >= 5.9 for timeout in seen), seen)

    def test_a_generous_model_timeout_cannot_run_one_hop_away(self):
        seen = self.recorded(OpenAICompatiblePlanner("https://example.test/v1", "k", "m",
                                                     timeout=120, allow_private_hosts=True))
        self.assertTrue(seen)
        self.assertTrue(all(timeout <= 15.1 for timeout in seen), seen)
        self.assertLessEqual(max(seen), OpenAICompatiblePlanner.CAPABILITY_PROBE_REQUEST_MAX_SECONDS + 0.1)

    def test_a_seven_second_hop_is_not_vetoed_by_the_floor(self):
        def scripted(request, timeout):
            if getattr(request, "method", "POST") == "GET":
                return _Json({"data": []})
            body = json.loads(request.data.decode("utf-8"))
            hop = _hop_of(body)
            time.sleep(7.0 if hop == "chat" else 0.0)
            return _hop_response(hop)

        planner = OpenAICompatiblePlanner("https://example.test/v1", "k", "m",
                                          timeout=20, allow_private_hosts=True)
        with patch.object(llm_adapter.urllib.request, "urlopen", side_effect=scripted):
            result = planner.test_capabilities()
        self.assertTrue(result["chat"])
        self.assertEqual(result["chosenProtocol"], "native_tools")


class ModelTestFailureCache(unittest.TestCase):
    class DeadPlanner:
        instances: list = []

        def __init__(self, timeout=0, allow_private_hosts=False):
            self.timeout, self.allow_private_hosts = timeout, allow_private_hosts
            self.base_url = self.model = self.api_key = ""
            self.probes = 0
            type(self).instances.append(self)

        def configure(self, url, model, key):
            self.base_url, self.model, self.api_key = url, model, key

        def test_capabilities(self):
            self.probes += 1
            return {"ok": False, "chat": False, "chosenProtocol": "unreachable",
                    "message": "模型接口不可达：连接被拒绝，目标端口未监听或地址错误，已跳过后续检查",
                    "errors": {"chat": "连接被拒绝"}, "latencyMs": 10,
                    "probeTimings": {"hops": 1, "gateMs": 10, "chatMs": 10}}

        def public_config(self):
            return {"enabled": True, "url": self.base_url, "model": self.model,
                    "keyConfigured": bool(self.api_key)}

    def setUp(self):
        server.MODEL_TEST_CACHE.clear()
        self.addCleanup(server.MODEL_TEST_CACHE.clear)
        saved = server.PLANNER
        self.addCleanup(setattr, server, "PLANNER", saved)
        server.PLANNER = OpenAICompatiblePlanner("https://saved.test/v1", "", "m",
                                                allow_private_hosts=True)
        self.DeadPlanner.instances = []

    def body(self, key="probe-key-alpha"):
        return {"url": "https://dead.test/v1", "model": "m", "key": key}

    def test_a_failed_verdict_is_reused_instead_of_reprobing(self):
        with patch.object(server, "OpenAICompatiblePlanner", self.DeadPlanner):
            first = server.test_llm_capabilities(self.body())
            second = server.test_llm_capabilities(self.body())
        probes = sum(instance.probes for instance in self.DeadPlanner.instances)
        self.assertEqual(probes, 1)
        self.assertFalse(first["test"].get("cached"))
        self.assertTrue(second["test"]["cached"])
        self.assertGreaterEqual(second["test"]["cacheTtlMs"], 0)

    def test_force_and_a_different_key_always_reprobe(self):
        with patch.object(server, "OpenAICompatiblePlanner", self.DeadPlanner):
            server.test_llm_capabilities(self.body())
            forced = server.test_llm_capabilities({**self.body(), "force": True})
            other = server.test_llm_capabilities(self.body("probe-key-beta"))
        probes = sum(instance.probes for instance in self.DeadPlanner.instances)
        self.assertFalse(forced["test"].get("cached"))
        self.assertFalse(other["test"].get("cached"))
        self.assertEqual(probes, 3)

    def test_a_live_verdict_is_never_reused(self):
        class LivePlanner(self.DeadPlanner):
            def test_capabilities(self):
                self.probes += 1
                return {"ok": True, "chat": True, "chosenProtocol": "json_fallback",
                        "agentReady": True, "message": "接口支持 JSON 规划。", "errors": {}}

        with patch.object(server, "OpenAICompatiblePlanner", LivePlanner):
            server.test_llm_capabilities(self.body())
            second = server.test_llm_capabilities(self.body())
        self.assertFalse(second["test"].get("cached"))
        self.assertEqual(sum(instance.probes for instance in LivePlanner.instances), 2)

    def test_the_failure_cache_never_holds_the_api_key(self):
        with patch.object(server, "OpenAICompatiblePlanner", self.DeadPlanner):
            payload = server.test_llm_capabilities(self.body("probe-key-leakcheck"))
        self.assertNotIn("probe-key-leakcheck", repr(payload))
        self.assertNotIn("probe-key-leakcheck", repr(server.MODEL_TEST_CACHE))
        self.assertTrue(server.MODEL_TEST_CACHE)
        self.assertTrue(all(len(key) == 32 and all(char in "0123456789abcdef" for char in key)
                            for key in server.MODEL_TEST_CACHE), list(server.MODEL_TEST_CACHE))


class ModelTestConcurrency(unittest.TestCase):
    def test_a_third_simultaneous_probe_is_asked_to_wait(self):
        release = threading.Event()
        state = {"running": 0}
        guard = threading.Lock()

        class BusyPlanner:
            def __init__(self, timeout=0, allow_private_hosts=False):
                self.timeout, self.allow_private_hosts = timeout, allow_private_hosts
                self.base_url = self.model = self.api_key = ""

            def configure(self, url, model, key):
                self.base_url, self.model, self.api_key = url, model, key

            def test_capabilities(self):
                with guard:
                    state["running"] += 1
                release.wait(10)
                with guard:
                    state["running"] -= 1
                return {"ok": True, "chat": True, "chosenProtocol": "native_tools",
                        "agentReady": True, "errors": {}}

            def public_config(self):
                return {"enabled": True, "url": self.base_url, "model": self.model}

        saved = server.PLANNER
        self.addCleanup(setattr, server, "PLANNER", saved)
        self.addCleanup(release.set)
        server.PLANNER = OpenAICompatiblePlanner("https://saved.test/v1", "", "m",
                                                allow_private_hosts=True)
        body = {"url": "https://busy.test/v1", "model": "m", "key": "k"}
        # The fake must be in place before the workers start: without it the
        # threads would probe the real network and never reach the slots.
        with patch.object(server, "OpenAICompatiblePlanner", BusyPlanner):
            workers = [threading.Thread(target=server.test_llm_capabilities, args=(body,),
                                        daemon=True)
                       for _ in range(server.MODEL_TEST_MAX_CONCURRENT)]
            for worker in workers:
                worker.start()
            deadline = time.monotonic() + 5
            while state["running"] < server.MODEL_TEST_MAX_CONCURRENT \
                    and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(state["running"], server.MODEL_TEST_MAX_CONCURRENT,
                             "the two live probes must hold both slots")
            with patch.object(server, "MODEL_TEST_SLOT_WAIT_SECONDS", 0.05):
                with self.assertRaises(server.RequestError) as ctx:
                    server.test_llm_capabilities(dict(body))
            self.assertEqual(ctx.exception.code, "MODEL_TEST_BUSY")
            self.assertEqual(ctx.exception.status, 429)
            release.set()
            for worker in workers:
                worker.join(10)
        self.assertEqual(state["running"], 0)


if __name__ == "__main__":
    unittest.main()
