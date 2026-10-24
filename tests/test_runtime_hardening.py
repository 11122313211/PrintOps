"""Production runtime hardening: lifecycle, admission control and observability.

Covered here are the deployment behaviours that the domain tests do not touch:

* signal-driven graceful shutdown for ``server.py``, ``mcp_server.py`` and the
  local host fallback (including the exit code and the JSON document a wrapper
  can parse);
* the bounded worker pool that replaces ThreadingHTTPServer's unbounded thread
  creation, and the 503 an over-capacity client receives;
* the per-request socket timeout that unblocks stalled (Slowloris-style)
  clients;
* the single structured request log line (requestId / status / latency) and the
  slow-request escalation;
* fail-fast startup self-checks (static assets, writable session database,
  port availability).

Socket-level cases are skipped automatically when the environment forbids
binding a loopback listening socket (for example a restricted sandbox); they
run in the normal suite.
"""

from __future__ import annotations

import http.client
import importlib.util
import io
import json
import os
import signal
import socket
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import mcp_server
import server
from agent import Memory


REPO_ROOT = Path(__file__).resolve().parents[1]


def _loopback_bindable() -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False
    finally:
        probe.close()


LOOPBACK_BINDABLE = _loopback_bindable()
requires_loopback = unittest.skipUnless(
    LOOPBACK_BINDABLE, "environment forbids binding a loopback listening socket"
)


def _free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


def _readline_with_timeout(stream, timeout: float):
    """Read one line without letting a stuck child process hang the suite."""
    result: dict[str, object] = {}

    def _read() -> None:
        try:
            result["line"] = stream.readline()
        except (OSError, ValueError) as error:  # pragma: no cover - defensive
            result["error"] = error

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    reader.join(timeout)
    return result.get("line")


class FakeSocket:
    """Minimal socket double for the connection-admission path."""

    def __init__(self) -> None:
        self.sent = b""
        self.closed = False
        self.shutdown_calls = 0

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def shutdown(self, how) -> None:
        self.shutdown_calls += 1

    def close(self) -> None:
        self.closed = True


def _unbound_server(max_connections: int = 1) -> server.PrintOpsHTTPServer:
    """A PrintOpsHTTPServer with the admission state but no listening socket."""
    httpd = server.PrintOpsHTTPServer.__new__(server.PrintOpsHTTPServer)
    httpd.max_connections = max_connections
    httpd._connection_slots = threading.BoundedSemaphore(max_connections)
    httpd.rejected_connections = 0
    return httpd


def _stub_handler(path: str, command: str) -> server.Handler:
    handler = server.Handler.__new__(server.Handler)
    handler.path = path
    handler.command = command
    return handler


class InFlightRequestsTest(unittest.TestCase):
    def test_wait_for_idle_blocks_until_the_last_request_finishes(self):
        tracker = server.InFlightRequests()
        tracker.enter()
        tracker.enter()
        self.assertEqual(tracker.active(), 2)
        self.assertFalse(tracker.wait_for_idle(0.05))
        tracker.leave()
        self.assertEqual(tracker.active(), 1)
        self.assertFalse(tracker.wait_for_idle(0.05))
        tracker.leave()
        self.assertEqual(tracker.active(), 0)
        self.assertTrue(tracker.wait_for_idle(1.0))

    def test_wait_for_idle_returns_immediately_when_no_request_is_active(self):
        tracker = server.InFlightRequests()
        started = time.monotonic()
        self.assertTrue(tracker.wait_for_idle(5.0))
        self.assertLess(time.monotonic() - started, 1.0)


class BoundedConcurrencyTest(unittest.TestCase):
    def test_over_capacity_connection_gets_503_and_starts_no_worker(self):
        httpd = _unbound_server(max_connections=1)
        self.assertTrue(httpd._connection_slots.acquire(blocking=False))
        client = FakeSocket()

        with mock.patch.object(server.LOGGER, "warning") as warning:
            httpd.process_request(client, ("127.0.0.1", 43210))

        self.assertEqual(httpd.rejected_connections, 1)
        self.assertEqual(warning.call_args.args[1], 1)
        self.assertIn(b"HTTP/1.1 503 Service Unavailable", client.sent)
        self.assertIn(b"Connection: close", client.sent)
        self.assertIn(b"Retry-After: 1", client.sent)
        payload = json.loads(client.sent.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
        self.assertEqual(payload["code"], "SERVER_BUSY")
        self.assertTrue(client.closed)
        # The slot that was already in use is still the only one in use, so the
        # rejected connection cannot have allocated a second worker.
        self.assertFalse(httpd.has_free_connection_slot())

    def test_worker_slot_is_released_after_the_handler_thread_ends(self):
        httpd = _unbound_server(max_connections=1)
        finished = threading.Event()

        def _recording_process_request_thread(self, request, client_address):
            finished.set()

        with mock.patch.object(socketserver.ThreadingMixIn, "process_request_thread",
                               _recording_process_request_thread):
            httpd.process_request(FakeSocket(), ("127.0.0.1", 43211))
            self.assertTrue(finished.wait(5.0), "worker thread never ran")

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not httpd.has_free_connection_slot():
            time.sleep(0.01)
        self.assertTrue(httpd.has_free_connection_slot())

    def test_default_configuration_bounds_threads_and_never_joins_them(self):
        self.assertTrue(server.PrintOpsHTTPServer.daemon_threads)
        self.assertFalse(server.PrintOpsHTTPServer.block_on_close)
        self.assertGreaterEqual(server.MAX_CONNECTIONS, 1)
        self.assertGreaterEqual(server.PrintOpsHTTPServer.request_queue_size, 16)


class RequestLifecycleTest(unittest.TestCase):
    def test_handler_declares_a_bounded_request_timeout(self):
        if server.REQUEST_TIMEOUT_SECONDS > 0:
            self.assertEqual(server.Handler.timeout, server.REQUEST_TIMEOUT_SECONDS)
            self.assertLessEqual(server.Handler.timeout, 120)
        else:
            self.assertIsNone(server.Handler.timeout)

    def test_completed_request_emits_one_structured_line(self):
        handler = _stub_handler("/api/sessions?token=super-secret", "GET")
        handler._begin_request()
        self.assertEqual(server.IN_FLIGHT.active(), 1)
        handler.log_request(200)

        with mock.patch.object(server.LOGGER, "info") as info, \
                mock.patch.object(server.LOGGER, "warning") as warning:
            handler._finish_request()

        self.assertEqual(server.IN_FLIGHT.active(), 0)
        info.assert_called_once()
        message, *args = info.call_args.args
        self.assertIn("requestId=%s", message)
        self.assertIn("status=%s", message)
        self.assertIn("durationMs=%.1f", message)
        self.assertEqual(args[0], handler.request_id)
        self.assertEqual(args[3], 200)
        self.assertLess(args[4], 60000)
        self.assertEqual(info.call_args.kwargs["extra"]["status"], 200)
        self.assertEqual(info.call_args.kwargs["extra"]["request_id"], handler.request_id)
        # Query strings are never logged: they can carry the access token.
        self.assertNotIn("super-secret", str(args))
        warning.assert_not_called()

    def test_protocol_error_before_the_request_line_is_logged_safely(self):
        # A socket timeout/reset can arrive before the request line is parsed,
        # when the handler has no command or path yet.
        handler = server.Handler.__new__(server.Handler)
        with mock.patch.object(server.LOGGER, "info") as info:
            handler.log_message("Request timed out: %r", TimeoutError("read"))
        info.assert_called_once()
        self.assertEqual(info.call_args.args[1], "-")
        self.assertEqual(info.call_args.args[2], "-")
        self.assertIn("Request timed out", info.call_args.args[3])

    def test_log_request_only_records_the_status(self):
        handler = _stub_handler("/api/health", "GET")
        with mock.patch.object(server.LOGGER, "info") as info:
            handler.log_request(404)
        info.assert_not_called()
        self.assertEqual(handler._response_status, 404)

    def test_slow_request_is_escalated_to_a_warning(self):
        if server.SLOW_REQUEST_MS <= 0:
            self.skipTest("slow-request reporting is disabled by configuration")
        handler = _stub_handler("/api/chat", "POST")
        handler._begin_request()
        handler._request_started = time.monotonic() - (server.SLOW_REQUEST_MS / 1000.0 + 0.05)

        with mock.patch.object(server.LOGGER, "info"), \
                mock.patch.object(server.LOGGER, "warning") as warning:
            handler._finish_request()

        warning.assert_called_once()
        self.assertIn("慢请求", warning.call_args.args[0])
        self.assertIn("durationMs=%.1f", warning.call_args.args[0])


class ClientAbortTest(unittest.TestCase):
    """A browser that closes the dialog mid-request is not a server failure."""

    def handler(self, error):
        instance = server.Handler.__new__(server.Handler)
        instance.requestline = "POST /api/model/test HTTP/1.1"
        instance.command = "POST"
        instance.path = "/api/model/test"
        instance.request_version = "HTTP/1.1"
        instance.client_address = ("127.0.0.1", 54321)
        instance.server = mock.MagicMock()
        instance.headers = {}
        instance.request_id = "abort-test"
        instance._bound_response_timeout = lambda **_kwargs: None
        instance.send_response = mock.Mock()
        instance.send_header = mock.Mock()
        instance.end_headers = mock.Mock()
        instance.wfile = mock.Mock()
        instance.wfile.write = mock.Mock(side_effect=error)
        return instance

    def test_aborted_client_write_is_swallowed(self):
        for error in (ConnectionAbortedError(10053, "aborted"),
                      BrokenPipeError(32, "pipe"), ConnectionResetError(10054, "reset")):
            handler = self.handler(error)
            self.assertIsNone(handler.reply({"ok": True}))
            self.assertIsNone(handler.error_response(server.RequestError("x", 500, "X")))
        self.assertIn(ConnectionAbortedError, server.CLIENT_ABORTS)

    def test_other_socket_errors_still_reach_the_generic_handler(self):
        handler = self.handler(OSError(22, "invalid argument"))
        with self.assertRaises(OSError):
            handler.reply({"ok": True})


class SignalHandlerTest(unittest.TestCase):
    def test_sigterm_triggers_shutdown_and_restores_the_previous_handler(self):
        calls: list[int] = []
        done = threading.Event()

        class FakeServer:
            def shutdown(self):
                calls.append(1)
                done.set()

        previous_term = signal.getsignal(signal.SIGTERM)
        previous_int = signal.getsignal(signal.SIGINT)
        restore = server.install_signal_handlers(FakeServer(), stream=io.StringIO())
        try:
            registered = signal.getsignal(signal.SIGTERM)
            self.assertNotEqual(registered, previous_term)
            registered(signal.SIGTERM, None)
            self.assertTrue(done.wait(5.0), "graceful shutdown was never requested")
            self.assertEqual(len(calls), 1)
        finally:
            restore()
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous_term)
        self.assertEqual(signal.getsignal(signal.SIGINT), previous_int)

    def test_second_signal_forces_exit_instead_of_hanging(self):
        done = threading.Event()

        class FakeServer:
            def shutdown(self):
                done.set()

        stream = io.StringIO()
        restore = server.install_signal_handlers(FakeServer(), stream=stream)
        try:
            registered = signal.getsignal(signal.SIGTERM)
            with mock.patch.object(server.os, "_exit") as force_exit:
                registered(signal.SIGTERM, None)
                registered(signal.SIGTERM, None)
            force_exit.assert_called_once_with(128 + int(signal.SIGTERM))
            self.assertIn("强制退出", stream.getvalue())
        finally:
            restore()
        self.assertTrue(done.wait(5.0))


class McpSignalHandlerTest(unittest.TestCase):
    def test_shutdown_request_is_a_base_exception_that_is_never_swallowed(self):
        self.assertTrue(issubclass(mcp_server.ShutdownRequested, BaseException))
        self.assertFalse(issubclass(mcp_server.ShutdownRequested, Exception))

    def test_signal_handler_raises_shutdown_request_and_is_restorable(self):
        previous = signal.getsignal(signal.SIGTERM)
        restore = mcp_server.install_signal_handlers()
        try:
            registered = signal.getsignal(signal.SIGTERM)
            self.assertNotEqual(registered, previous)
            with self.assertRaises(mcp_server.ShutdownRequested):
                registered(signal.SIGTERM, None)
        finally:
            restore()
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    @unittest.skipIf(os.name == "nt",
                     "Windows cannot deliver SIGTERM to a child process; graceful stop is POSIX-only")
    def test_sigterm_shuts_the_stdio_server_down_with_exit_code_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            process = subprocess.Popen(
                [sys.executable, "mcp_server.py", "--session-id", "sigterm-test",
                 "--memory-path", str(Path(temporary) / "agent.sqlite3")],
                cwd=str(REPO_ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"},
            )
            try:
                initialize = json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": mcp_server.MCP_PROTOCOL_VERSION},
                })
                self.assertIsNotNone(process.stdin)
                process.stdin.write(initialize + "\n")
                process.stdin.flush()
                ready = _readline_with_timeout(process.stdout, 20.0)
                self.assertIsNotNone(ready, "MCP server never answered initialize")
                self.assertIn('"result"', ready)

                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=20)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("exiting cleanly", stderr)
        # The initialize response was already consumed above, so a clean
        # shutdown must not append a second, partial protocol line.
        self.assertEqual(stdout.strip(), "", stdout)


class MemoryWriteProbeTest(unittest.TestCase):
    def test_probe_accepts_a_writable_path_and_leaves_no_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "agent.sqlite3"
            server.memory_write_probe(database)
            self.assertTrue(database.is_file())
            connection = sqlite3.connect(str(database))
            try:
                tables = [row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()]
            finally:
                connection.close()
        self.assertNotIn(server.PROBE_TABLE, tables)

    def test_probe_reports_a_path_that_cannot_be_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            blocker = Path(temporary) / "not-a-directory"
            blocker.write_text("x", encoding="utf-8")
            with self.assertRaises(OSError):
                server.memory_write_probe(blocker / "agent.sqlite3")


class StartupChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous_memory = server.MEMORY
        server.MEMORY = Memory(Path(self.tmp.name) / "agent.sqlite3")
        self.addCleanup(self._restore_memory)
        port_error = mock.patch.object(server, "PORT_ENV_ERROR", None)
        port_error.start()
        self.addCleanup(port_error.stop)

    def _restore_memory(self):
        server.MEMORY = self.previous_memory

    def test_writable_install_passes_all_checks(self):
        self.assertEqual(server.startup_checks(), [])

    def test_static_assets_are_required(self):
        with mock.patch.object(server, "STATIC_FILES", {"": "missing-index.html"},
                               create=True):
            problems = server.startup_checks()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("missing-index.html", problems[0])

    def test_unwritable_session_database_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            blocker = Path(temporary) / "not-a-directory"
            blocker.write_text("x", encoding="utf-8")
            with mock.patch.object(server, "MEMORY",
                                   types.SimpleNamespace(path=blocker / "agent.sqlite3")):
                problems = server.startup_checks()
        self.assertTrue(any("会话数据库不可写" in problem for problem in problems))

    def test_unusable_memory_path_is_reported_when_no_store_was_built(self):
        """The shipped default is an unbuilt store, so the probe must not need one."""
        with tempfile.TemporaryDirectory() as temporary:
            blocker = Path(temporary) / "not-a-directory"
            blocker.write_text("x", encoding="utf-8")
            with mock.patch.object(server, "MEMORY", None), \
                    mock.patch.object(server, "MEMORY_PATH", blocker / "agent.sqlite3"):
                problems = server.startup_checks()
        self.assertTrue(any("会话数据库不可写" in problem for problem in problems), problems)

    def test_invalid_port_environment_variable_is_reported(self):
        with mock.patch.object(server, "PORT_ENV_ERROR",
                               "环境变量 PRINTOPS_PORT='abc' 不是有效端口号"):
            problems = server.startup_checks()
        self.assertTrue(any("PRINTOPS_PORT" in problem for problem in problems))

    def test_normal_start_does_not_probe_the_port(self):
        with mock.patch.object(server.socket, "socket",
                               side_effect=AssertionError("port must not be probed")):
            self.assertEqual(server.startup_checks(), [])

    @requires_loopback
    def test_port_probe_reports_an_occupied_port(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            problems = server.startup_checks(port=port)
        finally:
            listener.close()
        self.assertTrue(any(str(port) in problem for problem in problems), problems)


class LazyMemoryTest(unittest.TestCase):
    """The module must import even when the configured session path is unusable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous_memory = server.MEMORY
        self.addCleanup(self._restore_memory)

    def _restore_memory(self):
        server.MEMORY = self.previous_memory

    def test_session_memory_is_built_on_first_use_and_cached(self):
        target = Path(self.tmp.name) / "agent.sqlite3"
        with mock.patch.object(server, "MEMORY", None), \
                mock.patch.object(server, "MEMORY_PATH", target):
            first = server.session_memory()
            second = server.session_memory()
        self.assertIsInstance(first, Memory)
        self.assertIs(first, second)
        self.assertEqual(Path(first.path), target)

    def test_session_memory_honours_a_replaced_module_value(self):
        replacer = object()
        with mock.patch.object(server, "MEMORY", replacer):
            self.assertIs(server.session_memory(), replacer)

    def test_memory_path_falls_back_to_the_configured_constant(self):
        target = Path(self.tmp.name) / "agent.sqlite3"
        with mock.patch.object(server, "MEMORY", None), \
                mock.patch.object(server, "MEMORY_PATH", target):
            self.assertEqual(server.memory_path(), target)

    def test_memory_path_prefers_a_built_store(self):
        store = Memory(Path(self.tmp.name) / "other.sqlite3")
        with mock.patch.object(server, "MEMORY", store):
            self.assertEqual(Path(server.memory_path()), Path(store.path))


class LoggingConfigurationTest(unittest.TestCase):
    def test_environment_level_is_honoured(self):
        with mock.patch.dict(os.environ, {"PRINTOPS_LOG_LEVEL": "WARNING"}):
            with mock.patch.object(server.logging, "basicConfig") as basic_config, \
                    mock.patch.object(server.LOGGER, "setLevel") as set_level:
                server.configure_logging()
        set_level.assert_called_once_with(server.logging.WARNING)
        self.assertLessEqual(basic_config.call_count, 1)

    def test_invalid_level_falls_back_to_info(self):
        with mock.patch.dict(os.environ, {"PRINTOPS_LOG_LEVEL": "not-a-level"}):
            with mock.patch.object(server.logging, "basicConfig"), \
                    mock.patch.object(server.LOGGER, "setLevel") as set_level:
                server.configure_logging()
        set_level.assert_called_once_with(server.logging.INFO)


class MainEntrypointTest(unittest.TestCase):
    def test_check_mode_reports_ok_and_exits_zero(self):
        stdout = io.StringIO()
        with mock.patch.object(server, "configure_logging"), \
                mock.patch.object(server, "startup_checks", return_value=[]) as checks, \
                mock.patch.object(sys, "stdout", stdout):
            code = server.main(["--check"])
        self.assertEqual(code, 0)
        checks.assert_called_once_with(server.PORT)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["problems"], [])

    def test_check_mode_reports_problems_and_exits_non_zero(self):
        stdout = io.StringIO()
        with mock.patch.object(server, "configure_logging"), \
                mock.patch.object(server, "startup_checks",
                                  return_value=["会话数据库不可写：/nope（denied）"]), \
                mock.patch.object(sys, "stdout", stdout):
            code = server.main(["--check", "--port", "0"])
        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["port"], 0)
        self.assertIn("会话数据库不可写", payload["problems"][0])

    def test_check_mode_reports_an_unusable_memory_path_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as temporary:
            blocker = Path(temporary) / "not-a-directory"
            blocker.write_text("x", encoding="utf-8")
            environment = dict(os.environ,
                               PRINTOPS_MEMORY_PATH=str(blocker / "agent.sqlite3"),
                               PYTHONUTF8="1")
            result = subprocess.run(
                [sys.executable, "server.py", "--check"], cwd=str(REPO_ROOT),
                env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", timeout=30,
            )
        self.assertNotIn("Traceback", result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertTrue(any("会话数据库不可写" in problem for problem in payload["problems"]),
                        payload["problems"])

    def test_out_of_range_port_fails_before_any_self_check(self):
        with mock.patch.object(server, "configure_logging"), \
                mock.patch.object(server, "startup_checks") as checks, \
                mock.patch.object(sys, "stderr", io.StringIO()):
            code = server.main(["--port", "70000"])
        self.assertEqual(code, 1)
        checks.assert_not_called()


@requires_loopback
class LoopbackRuntimeTest(unittest.TestCase):
    """Socket-level coverage of the bounds that production depends on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous_memory = server.MEMORY
        server.MEMORY = Memory(Path(self.tmp.name) / "agent.sqlite3")
        self.addCleanup(self._restore_memory)

    def _restore_memory(self):
        server.MEMORY = self.previous_memory

    def _serve(self, handler=server.Handler, max_connections=None):
        httpd = server.PrintOpsHTTPServer(("127.0.0.1", 0), handler,
                                         max_connections=max_connections)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd

    def test_request_log_carries_request_id_status_and_latency(self):
        httpd = self._serve()
        port = httpd.server_address[1]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            with self.assertLogs("printops.server", level="INFO") as logs:
                connection.request("GET", "/api/health")
                response = connection.getresponse()
                response.read()
            self.assertEqual(response.status, 200)
            request_id = response.getheader("X-Request-ID")
        finally:
            connection.close()
        self.assertTrue(request_id)
        # Filter by this request's id: the shared logger also carries other
        # tests' background traffic when the whole suite runs at once, and
        # the subject here is that this request is logged exactly once.
        lines = [line for line in logs.output if f"requestId={request_id}" in line]
        self.assertEqual(len(lines), 1, logs.output)
        self.assertIn(f"requestId={request_id}", lines[0])
        self.assertIn("status=200", lines[0])
        self.assertIn("durationMs=", lines[0])

    def test_over_capacity_client_is_rejected_with_503(self):
        entered = threading.Event()

        class SlowHandler(server.Handler):
            def do_GET(self):
                entered.set()
                time.sleep(0.6)
                return super().do_GET()

        httpd = self._serve(handler=SlowHandler, max_connections=1)
        port = httpd.server_address[1]
        first = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        second = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            first.putrequest("GET", "/api/health", skip_accept_encoding=True)
            first.endheaders()
            self.assertTrue(entered.wait(5.0), "the first request never reached the handler")

            second.request("GET", "/api/health")
            rejected = second.getresponse()
            payload = json.loads(rejected.read())
            self.assertEqual(rejected.status, 503)
            self.assertEqual(payload["code"], "SERVER_BUSY")

            accepted = first.getresponse()
            self.assertEqual(accepted.status, 200)
            accepted.read()
        finally:
            first.close()
            second.close()
        self.assertEqual(httpd.rejected_connections, 1)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not httpd.has_free_connection_slot():
            time.sleep(0.01)
        self.assertTrue(httpd.has_free_connection_slot())

    def test_stalled_client_is_disconnected_by_the_request_timeout(self):
        class ShortTimeoutHandler(server.Handler):
            timeout = 0.3

        httpd = self._serve(handler=ShortTimeoutHandler)
        port = httpd.server_address[1]
        stalled = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            stalled.settimeout(5.0)
            started = time.monotonic()
            data = stalled.recv(1024)
            elapsed = time.monotonic() - started
        finally:
            stalled.close()
        self.assertEqual(data, b"", "a client that never sends a request must be dropped")
        self.assertLess(elapsed, 4.0)

        # Dropping a stalled client must not wedge the server for real ones.
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
        finally:
            connection.close()

    @unittest.skipIf(os.name == "nt",
                     "Windows cannot deliver SIGTERM to a child process; graceful stop is POSIX-only")
    def test_sigterm_stops_the_server_process_with_exit_code_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            port = _free_port()
            process = subprocess.Popen(
                [sys.executable, "server.py", "--port", str(port)],
                cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8",
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
                     "PRINTOPS_MEMORY_PATH": str(Path(temporary) / "agent.sqlite3")},
            )
            try:
                deadline = time.monotonic() + 20.0
                ready = False
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                        connection.request("GET", "/api/health")
                        ready = connection.getresponse().status == 200
                        connection.close()
                    except OSError:
                        ready = False
                    if ready:
                        break
                    time.sleep(0.1)
                self.assertTrue(ready, "server.py never became ready")

                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=20)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("signal=15", stderr)
        self.assertIn("server stopped", stderr)
        self.assertIn(f"http://localhost:{port}", stdout)


class LocalHostSignalTest(unittest.TestCase):
    @staticmethod
    def _load_host():
        path = REPO_ROOT / "tools" / "printops_local_host.py"
        spec = importlib.util.spec_from_file_location("printops_local_host_signal_test", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"unable to load {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_signal_handler_raises_structured_shutdown_and_is_restorable(self):
        host = self._load_host()
        previous = signal.getsignal(signal.SIGTERM)
        restore = host.install_signal_handlers()
        try:
            registered = signal.getsignal(signal.SIGTERM)
            self.assertNotEqual(registered, previous)
            with self.assertRaises(host.ShutdownRequested):
                registered(signal.SIGTERM, None)
            self.assertFalse(issubclass(host.ShutdownRequested, Exception))
        finally:
            restore()
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    @unittest.skipIf(os.name == "nt",
                     "Windows cannot deliver SIGTERM to a child process; graceful stop is POSIX-only")
    def test_sigterm_still_produces_one_strict_json_document(self):
        host = self._load_host()
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "agent.sqlite3"
            process = subprocess.Popen(
                [sys.executable, "tools/printops_local_host.py", "--interactive",
                 "--session-id", "sigterm-host", "--memory-path", str(database)],
                cwd=str(REPO_ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"},
            )
            try:
                first = _readline_with_timeout(process.stdout, 30.0)
                self.assertIsNotNone(first, "local host never produced its first document")
                if json.loads(first).get("ok"):
                    process.send_signal(signal.SIGTERM)
                    remainder, stderr = process.communicate(timeout=20)
                    lines = (first + remainder).strip().splitlines()
                else:
                    remainder, stderr = process.communicate(timeout=20)
                    lines = first.strip().splitlines()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

        self.assertEqual(process.returncode, 1, stderr)
        payload = json.loads(lines[-1])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["type"], "ShutdownRequested")
        self.assertEqual(payload["runtime"], "python-stdlib")


if __name__ == "__main__":
    unittest.main()
