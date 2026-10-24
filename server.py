"""Local static server and JSON API; no third-party dependencies."""

from __future__ import annotations

import argparse
import hashlib
from copy import copy, deepcopy
import json
import logging
import math
import os
import re
import secrets
import signal
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from agent import (Agent, Memory, RevisionConflictError, PLATFORMS, SUPPLIER_PROFILE_VERSION,
                   TOOL_META, TOOL_SCHEMAS, WORKFLOW_LABELS, MAX_PLANNER_TOOL_ROUNDS)
from product_knowledge import catalog_payload, parameter_state, KNOWLEDGE_MANIFEST
from llm_adapter import (ConfigWriteError, OpenAICompatiblePlanner, read_saved_config,
                         write_saved_config)
from order_contract import canonical_order, handoff_document
from order_model import normalize_state
from storage_contract import StoreConflictError, aggregate_tool_calls, summary
from supplier_adapters import list_platforms
from tools import validate_order
from request_runtime import (CONTROL_SLOTS, DeadlineExceeded, OutboundBusy, bounded_io,
                             check_deadline, current_deadline, deadline_lock, deadline_scope,
                             operation_deadline, remaining, replace_deadline)
import stdio_utf8


ROOT = Path(__file__).parent
# A deployment (systemd, read-only install directory, packaged app) may place
# the session database outside the repository.  The default is unchanged.
MEMORY_PATH = Path(os.getenv("PRINTOPS_MEMORY_PATH", "").strip() or (ROOT / "data/agent.sqlite3"))
# The store is built on first use instead of at import: constructing it here
# would raise before ``--check`` could explain an unusable path, leaving the
# "session database is not writable" startup check unreachable.
MEMORY: Memory | None = None
LLM_CONFIG_PATH = ROOT / "data/llm_config.json"
SAVED_LLM_CONFIG = read_saved_config(LLM_CONFIG_PATH)
ALLOW_PRIVATE_LLM_HOSTS = os.getenv("PRINTOPS_ALLOW_PRIVATE_LLM_HOSTS", "").strip().lower() in {
    "1", "true", "yes", "on",
}
PLANNER = OpenAICompatiblePlanner(
    SAVED_LLM_CONFIG.get("url", os.getenv("PRINTOPS_LLM_URL", "")),
    SAVED_LLM_CONFIG.get("key", os.getenv("PRINTOPS_LLM_KEY", "")),
    SAVED_LLM_CONFIG.get("model", os.getenv("PRINTOPS_LLM_MODEL", "")),
    timeout=OpenAICompatiblePlanner._timeout_from_env(),
    allow_private_hosts=ALLOW_PRIVATE_LLM_HOSTS,
)
LLM_LOCK = threading.Lock()
SESSION_LOCKS: dict[str, threading.Lock] = {}
SESSION_LOCKS_GUARD = threading.Lock()
MAX_BODY_BYTES = 1 * 1024 * 1024
# Scratch table used only by the startup write probe; the probe rolls back, so
# a successful start never persists it. The probe uses literal SQL, so this
# name must stay in sync with the statements in memory_write_probe().
PROBE_TABLE = "printops_startup_probe"
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LOGGER = logging.getLogger("printops.server")
DEFAULT_PORT = 4174
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_MAX_CONNECTIONS = 64
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_SLOW_REQUEST_MS = 1000.0
DEFAULT_SHUTDOWN_GRACE_SECONDS = 5.0


def _env_number(name: str, default: Any, cast: Callable[[str], Any]) -> Any:
    """Read a numeric tuning knob without letting a typo abort module import."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        LOGGER.warning("忽略无效的 %s=%r，使用默认值 %s", name, raw, default)
        return default


def _port_from_env() -> tuple[int, str | None]:
    """Parse ``PRINTOPS_PORT`` and remember an invalid value as a startup problem."""
    raw = os.getenv("PRINTOPS_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT, None
    try:
        return int(raw), None
    except ValueError:
        return DEFAULT_PORT, f"环境变量 PRINTOPS_PORT={raw!r} 不是有效端口号"


PORT, PORT_ENV_ERROR = _port_from_env()
MAX_CONNECTIONS = max(
    1, int(_env_number("PRINTOPS_MAX_CONNECTIONS", DEFAULT_MAX_CONNECTIONS, int))
)
REQUEST_TIMEOUT_SECONDS = float(
    _env_number("PRINTOPS_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT_SECONDS, float)
)
# dsh clampTimeout pattern: routes that declare bounded network work get
# their own capped budget instead of silently colliding with the global
# request deadline mid-probe. MIN_ROUTE_EXTENSION_BASE_SECONDS keeps
# deliberately tiny handler budgets (tests, hardened deployments) intact.
DNS_CHECK_SECONDS = 6.0
MODEL_TEST_BUDGET_SECONDS = 40.0
ROUTE_DEADLINE_CAP_SECONDS = 120.0
MIN_ROUTE_EXTENSION_BASE_SECONDS = 5.0
# The chat route's declared workload: one initial plan plus at most
# MAX_PLANNER_TOOL_ROUNDS tool rounds and one tools-disabled synthesis, each
# capped by the planner's per-call budget, plus local tool execution,
# storage writes and the response write.
PLANNER_ROUTE_MARGIN_SECONDS = 10.0
# Capability probes are bounded by construction: one gated hop plus two
# parallel hops in a single round trip.  Two more knobs keep the route itself
# from becoming a way to spend the user's API quota or the shared outbound
# budget: an unreachable verdict is reused for a few seconds instead of
# re-probing on every click, and only two probes may run at once.
MODEL_TEST_FAILURE_CACHE_SECONDS = float(
    _env_number("PRINTOPS_MODEL_TEST_FAILURE_CACHE_SECONDS", 15.0, float))
MODEL_TEST_CACHE_MAX_ENTRIES = 16
MODEL_TEST_MAX_CONCURRENT = 2
MODEL_TEST_SLOT_WAIT_SECONDS = 2.0
PROBE_FINGERPRINT_VERSION = "capability-probe-v3-single-round-trip"
SLOW_REQUEST_MS = float(_env_number("PRINTOPS_SLOW_REQUEST_MS", DEFAULT_SLOW_REQUEST_MS, float))
SHUTDOWN_GRACE_SECONDS = float(
    _env_number("PRINTOPS_SHUTDOWN_GRACE_SECONDS", DEFAULT_SHUTDOWN_GRACE_SECONDS, float)
)

# Local access token: generated per process, injected into the served page,
# and required on every API call except /api/health. A random website cannot
# read it (same-origin policy), so browser-borne CSRF/DNS-rebinding requests
# fail with 401/403.
TOKEN_HEADER = "X-PrintOps-Token"
ACCESS_TOKEN = secrets.token_urlsafe(24)
# Static routes are whitelisted: the server must never expose source files,
# docs, .git or runtime data. Non-API paths outside this set return 404.
STATIC_FILES = {
    "": "index.html", "/": "index.html", "/index.html": "index.html",
    "/app.js": "app.js", "/markdown.js": "markdown.js", "/workbench.js": "workbench.js", "/journey.js": "journey.js", "/styles.css": "styles.css",
}
STATIC_CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "workbench.js": "text/javascript; charset=utf-8",
    "journey.js": "text/javascript; charset=utf-8",
    "markdown.js": "text/javascript; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
}
# Windows reports a browser that disappeared mid-request as ConnectionAbortedError
# (WSAEINTR 10053), which is neither BrokenPipeError nor ConnectionResetError.
# Closing the settings dialog during a probe is normal traffic, so these writes
# must not fall through to the generic handler and be logged as a 500.
CLIENT_ABORTS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
KEY_STORAGE_WARNING = ("API Key 以明文保存在本机 data/llm_config.json（文件权限 600）。"
                       "请勿把该文件提交到 Git 或分享给他人；更安全的做法是仅用环境变量提供 Key。")


class RequestError(Exception):
    """An expected client/API error that can be safely shown to the UI."""

    def __init__(self, message: str, status: int = 400, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def _key_storage_warning() -> str:
    """Report plaintext key storage to API consumers only.

    The settings panel deliberately does not print this note: it exposes the
    local key path and storage detail in the UI.  The field stays in the
    documented /api/settings payload for callers that audit key storage.
    """
    try:
        return KEY_STORAGE_WARNING if read_saved_config(LLM_CONFIG_PATH).get("key") else ""
    except OSError:
        return ""


def llm_settings() -> dict[str, object]:
    with deadline_lock(LLM_LOCK):
        payload: dict[str, object] = {"llm": PLANNER.public_config()}
    warning = _key_storage_warning()
    if warning:
        payload["keyStorageWarning"] = warning
    return payload


def _configure_bounded(candidate, url: str, model: str, api_key: str) -> None:
    """Validate and apply an endpoint config in a short bounded worker.

    URL validation may hit the OS resolver, which Python cannot cancel; the
    wait is capped at DNS_CHECK_SECONDS so a dead DNS turns into a specific,
    actionable error instead of eating the whole request budget and surfacing
    as a generic 408 while the user is only saving their API key.

    A busy outbound pool is reported separately: the validation never ran, so
    telling the user to check their address or DNS would be wrong.  The save
    draws from the control pool, so a stuck model probe cannot starve it.
    """
    budget = min(DNS_CHECK_SECONDS, max(1.0, float(candidate.timeout)))
    try:
        bounded_io(lambda: candidate.configure(url, model, api_key),
                   operation_deadline(budget), CONTROL_SLOTS)
    except OutboundBusy as error:
        raise RequestError(
            "上一次接口地址校验仍未结束，请稍后重试",
            429, "OUTBOUND_BUSY") from error
    except DeadlineExceeded as error:
        raise RequestError(
            f"接口地址校验超时（{budget:.0f} 秒）：主机名解析或出站等待未完成，"
            "请检查本机 DNS/代理，或确认端点可达后重试",
            504, "ADDRESS_VALIDATION_TIMEOUT") from error


def _config_write_message(error: OSError) -> str:
    """Name the failing step and the OS error code, not just "cannot write"."""
    hints = {5: "拒绝访问", 32: "文件被其他程序占用", 33: "文件被锁定"}
    detail = ""
    winerror = getattr(error, "winerror", None)
    if winerror is not None:
        hint = hints.get(winerror)
        detail = f"（Windows 错误 {winerror}{'：' + hint if hint else ''}）"
    stage = getattr(error, "stage", "写入配置文件")
    return (f"{stage}失败，无法写入本机配置文件 data/llm_config.json{detail}；"
            "请检查 data 目录的写入权限，或确认同步盘/安全软件没有占用该文件")


def update_llm_settings(body: dict[str, object],
                        request_id: str = "") -> dict[str, object]:
    global PLANNER
    url = str(body.get("url", "")).strip()
    model = str(body.get("model", "")).strip()
    clear = bool(body.get("clear"))
    if bool(url) != bool(model):
        raise ValueError("URL 和模型名需要同时填写，或同时留空使用规则模式")
    candidate = planner_for_request()
    if clear or not url:
        candidate.configure("", "", "")
    else:
        api_key = str(body.get("key", "")).strip() or candidate.api_key
        # URL validation can resolve DNS. Keep it outside the config lock,
        # bounded, and confined to a candidate that is never published late.
        _configure_bounded(candidate, url, model, api_key)
    with deadline_lock(LLM_LOCK):
        try:
            write_saved_config(LLM_CONFIG_PATH, {"url": candidate.base_url, "model": candidate.model, "key": candidate.api_key})
        except OSError as error:
            # Antivirus locking the file, a read-only install and a restrictive
            # ACL all land here; the generic 500 would hide an actionable
            # permission issue, and the request id ties the toast to this line.
            LOGGER.error(
                "config write failed requestId=%s stage=%s path=%s winerror=%s errno=%s cause=%r",
                request_id, getattr(error, "stage", "写入配置文件"),
                getattr(error, "path", str(LLM_CONFIG_PATH)),
                getattr(error, "winerror", None), getattr(error, "errno", None),
                getattr(error, "cause", error))
            raise RequestError(_config_write_message(error), 500, "CONFIG_WRITE_FAILED") from error
        PLANNER = candidate
        payload: dict[str, object] = {"llm": PLANNER.public_config()}
    warning = _key_storage_warning()
    if warning:
        payload["keyStorageWarning"] = warning
    return payload


def planner_for_request():
    """Copy config under a short lock; never share per-run diagnostics."""
    with deadline_lock(LLM_LOCK):
        if hasattr(PLANNER, "for_request"):
            return PLANNER.for_request()
        planner = copy(PLANNER)  # Compatibility for injected local planners.
        for attribute in ("last_error", "last_protocol"):
            if hasattr(planner, attribute):
                setattr(planner, attribute, "")
        return planner


def planner_chat_budget_seconds(planner: object) -> float:
    """Route budget for one chat turn: bounded planner rounds plus local work.

    Each planner call is capped by its own timeout (never by this route
    budget), so the declared workload is (rounds + synthesis + initial plan) ×
    per-call timeout; the margin covers tool execution, storage writes and the
    response write. ROUTE_DEADLINE_CAP_SECONDS clamps the final deadline inside
    Handler._extend_request_deadline.
    """
    try:
        per_call = float(getattr(planner, "timeout", 0) or 0)
    except (TypeError, ValueError):
        per_call = 0.0
    if not math.isfinite(per_call) or per_call <= 0:
        per_call = 20.0  # OpenAICompatiblePlanner._timeout_from_env default.
    return PLANNER_ROUTE_MARGIN_SECONDS + (MAX_PLANNER_TOOL_ROUNDS + 2) * per_call


MODEL_TEST_CACHE: dict[str, tuple[float, dict[str, object]]] = {}
MODEL_TEST_CACHE_LOCK = threading.Lock()
MODEL_TEST_SLOTS = threading.BoundedSemaphore(MODEL_TEST_MAX_CONCURRENT)


def _model_test_fingerprint(url: str, model: str, api_key: str) -> str:
    """Identify one probe run; the API key is hashed here and never leaves."""
    digest = hashlib.sha256()
    for part in (PROBE_FINGERPRINT_VERSION, url, model, api_key):
        digest.update(str(part).encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
    return digest.hexdigest()[:32]


def _cached_model_test(fingerprint: str) -> dict[str, object] | None:
    """Reuse a very recent "unreachable / rejected" verdict if one stands."""
    with deadline_lock(MODEL_TEST_CACHE_LOCK):
        stored = MODEL_TEST_CACHE.get(fingerprint)
        if stored is None:
            return None
        expiry, payload = stored
        now = time.monotonic()
        if expiry <= now:
            MODEL_TEST_CACHE.pop(fingerprint, None)
            return None
        cached = deepcopy(payload)
        cached["cached"] = True
        cached["cacheAgeMs"] = max(
            0, int((MODEL_TEST_FAILURE_CACHE_SECONDS - (expiry - now)) * 1000))
        cached["cacheTtlMs"] = max(0, int((expiry - now) * 1000))
        return cached


def _remember_model_test(fingerprint: str, result: dict[str, object]) -> None:
    """Cache only "cannot reach" or "cannot authenticate" verdicts.

    A capability verdict goes stale the moment the user edits a field, so live
    results are never reused.  The failure cache exists so a dead endpoint does
    not absorb the outbound budget on every repeated click; force bypasses it
    and no entry ever holds the key itself.
    """
    if result.get("ok") or str(result.get("chosenProtocol")) not in {"unreachable", "unavailable"}:
        return
    if not isinstance(result.get("message"), str) or not result.get("message"):
        return
    with deadline_lock(MODEL_TEST_CACHE_LOCK):
        now = time.monotonic()
        expired = [key for key, (expiry, _payload) in MODEL_TEST_CACHE.items() if expiry <= now]
        for key in expired:
            MODEL_TEST_CACHE.pop(key, None)
        MODEL_TEST_CACHE[fingerprint] = (now + MODEL_TEST_FAILURE_CACHE_SECONDS, deepcopy(result))
        while len(MODEL_TEST_CACHE) > MODEL_TEST_CACHE_MAX_ENTRIES:
            oldest = min(MODEL_TEST_CACHE, key=lambda key: MODEL_TEST_CACHE[key][0])
            MODEL_TEST_CACHE.pop(oldest, None)


def _acquire_model_test_slot() -> None:
    """Stop a probe storm from starving the shared outbound worker pool."""
    budget = MODEL_TEST_SLOT_WAIT_SECONDS
    left = remaining()
    if left is not None:
        budget = min(budget, max(0.0, left))
    if not MODEL_TEST_SLOTS.acquire(timeout=budget):
        raise RequestError("同时进行的模型检测已达上限，请稍候再试", 429, "MODEL_TEST_BUSY")


def test_llm_capabilities(body: dict[str, object]) -> dict[str, object]:
    """Probe a request-local config snapshot without holding the config lock."""
    saved = planner_for_request()
    tester = saved
    probe_url, probe_model, probe_key = saved.base_url, saved.model, saved.api_key
    if any(key in body for key in ("url", "model", "key")):
        tester = OpenAICompatiblePlanner(
            timeout=saved.timeout,
            allow_private_hosts=getattr(saved, "allow_private_hosts", False),
        )
        probe_url = str(body.get("url", "")).strip()
        probe_model = str(body.get("model", "")).strip()
        probe_key = str(body.get("key", "")).strip()
        if not probe_key and probe_url == saved.base_url and probe_model == saved.model:
            probe_key = saved.api_key
        _configure_bounded(tester, probe_url, probe_model, probe_key)
    fingerprint = _model_test_fingerprint(probe_url, probe_model, probe_key)
    if not bool(body.get("force")):
        cached = _cached_model_test(fingerprint)
        if cached is not None:
            return {"llm": tester.public_config(), "test": cached}
    _acquire_model_test_slot()
    try:
        result = tester.test_capabilities()
    finally:
        MODEL_TEST_SLOTS.release()
    _remember_model_test(fingerprint, result)
    return {"llm": tester.public_config(), "test": result}


def _session_id(value: object) -> str | None:
    """Accept only opaque, bounded IDs generated by this app."""
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not SESSION_ID_PATTERN.fullmatch(value):
        raise RequestError("会话标识无效，请刷新页面后重试", code="INVALID_SESSION")
    return value


def _session_lock(session_id: str) -> threading.Lock:
    with deadline_lock(SESSION_LOCKS_GUARD):
        return SESSION_LOCKS.setdefault(session_id, threading.Lock())


def _item_index(body: dict[str, object]) -> int | None:
    value = body.get("itemIndex")
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise RequestError("itemIndex 必须是非负整数", code="INVALID_ITEM")
    return value


def _resolve_item_target(agent: Agent, body: dict[str, Any]) -> int | None:
    """Resolve identity only after CAS has checked this exact revision."""
    index, item_id = _item_index(body), body.get("itemId")
    if index is None and item_id is None:
        return None
    items = agent.state["order"].get("items") or []
    if item_id is not None:
        if not isinstance(item_id, str) or not item_id.strip() or len(item_id) > 128:
            raise RequestError("itemId 无效", code="INVALID_ITEM")
        resolved = next((i for i, item in enumerate(items) if item.get("itemId") == item_id), None)
        if resolved is None:
            raise RequestError("指定产品项不存在，请恢复最新订单", 409, "ITEM_NOT_FOUND")
        if index is not None and index != resolved:
            raise RequestError("产品项与索引不一致，请恢复最新订单", 409, "ITEM_TARGET_CONFLICT")
        index = resolved
    if index is None or not 0 <= index < len(items) or len(items) < 2:
        raise RequestError("指定产品项不存在，请恢复最新订单", 409, "ITEM_NOT_FOUND")
    return index


def _preflight(agent: Agent, body: dict[str, object]):
    """Dispatch file preflight without guessing scanner state.

    ``Agent.upload`` keeps historical defaults for trusted in-process callers,
    but an HTTP payload is an untrusted transport boundary.  Requiring both
    booleans here keeps the browser API, public tool gateway and MCP contract
    aligned: an omitted flag means the client failed to provide inspection
    evidence, rather than meaning ``False``/``True``.
    """
    missing = [key for key in ("encrypted", "readable") if key not in body]
    if missing:
        raise RequestError("预检必须明确提供 encrypted 和 readable", code="INVALID_PAYLOAD")
    encrypted = body.get("encrypted")
    readable = body.get("readable")
    if not isinstance(encrypted, bool) or not isinstance(readable, bool):
        raise RequestError("参数 encrypted 和 readable 必须是布尔值", code="INVALID_PAYLOAD")
    return agent.upload(
        str(body.get("fileName", "")), int(body.get("sizeBytes", 0)),
        int(body["pageCount"]) if body.get("pageCount") is not None else None,
        encrypted, readable, body.get("inspection"), body.get("expectedSize"), _item_index(body),
    )


IDEMPOTENT_MUTATION_PATHS = frozenset({"/api/chat", "/api/fields", "/api/choose", "/api/generate",
                                     "/api/confirm", "/api/preflight", "/api/tools/call", "/api/platform",
                                     "/api/quote/status", "/api/quote/cancel"})


def dispatch_post(path: str, body: dict[str, object], agent: Agent):
    """Persist an operation receipt with every commit of an identified mutation."""
    operation_id = body.get("operationId")
    if path not in IDEMPOTENT_MUTATION_PATHS or operation_id is None:
        return _dispatch_post(path, body, agent)
    if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", operation_id):
        raise RequestError("operationId 格式无效", code="INVALID_OPERATION")
    request = {key: value for key, value in body.items() if key not in {"expectedRevision", "operationId"}}
    request_hash = hashlib.sha256(json.dumps({"path": path, "body": request}, ensure_ascii=False,
                                            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    receipts = agent.state.get("operationReceipts") or []
    receipt = next((value for value in receipts if value.get("operationId") == operation_id), None)
    if receipt is None:
        # Snapshot receipts are bounded; the append-only events retain the
        # operation identity for the entire lifetime of this order.
        lookup = getattr(agent.memory.store, "find_operation_receipt", None)
        if lookup:
            receipt = lookup(agent.id, operation_id)
        else:
            receipt = next((event["payload"] for event in reversed(agent.memory.store.load_events(agent.id))
                            if event.get("eventType") == "operation_receipt" and
                            event.get("payload", {}).get("operationId") == operation_id), None)
    if receipt:
        if receipt.get("requestHash") != request_hash:
            raise RequestError("同一 operationId 不可用于不同内容", 409, "OPERATION_CONFLICT")
        result = order_history_snapshot(agent.memory, agent.id)
        result.update({"operationReceipt": deepcopy(receipt), "duplicateOperation": True,
                       "needsReconciliation": receipt.get("status") != "completed"})
        return result
    agent._operation_context = {"operationId": operation_id, "requestHash": request_hash,
                                "path": path, "startedAt": agent._timestamp()}
    agent._operation_complete = False
    try:
        result = _dispatch_post(path, body, agent)
        # Normal business routes finish through Agent._result, which commits
        # the receipt atomically with the final business state. A future route
        # that returns early still records an outcome before returning success.
        if not agent._operation_complete:
            agent._operation_complete = True
            agent._save()
            result = {**result, **order_history_snapshot(agent.memory, agent.id)}
        receipt = next(value for value in agent.state["operationReceipts"] if value["operationId"] == operation_id)
        result.update({"operationReceipt": deepcopy(receipt), "duplicateOperation": False,
                       "needsReconciliation": False})
        return result
    finally:
        agent._operation_context = None
        agent._operation_complete = False


def _dispatch_post(path: str, body: dict[str, object], agent: Agent):
    """Dispatch a parsed API payload without doing network I/O."""
    history_result = dispatch_history_post(path, body, agent.memory)
    if history_result is not None:
        return history_result
    if agent.state.get("orderStatus") == "archived":
        if path in {"/api/session", "/api/session/context", "/api/focus"}:
            if path == "/api/focus":
                return focus_snapshot(agent, body)
            return order_history_snapshot(agent.memory, agent.id)
        raise RequestError("已归档订单只读，请新建订单继续编辑", 409, "ORDER_ARCHIVED")
    expected_revision = body.get("expectedRevision")
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    target_body = {key: body[key] for key in ("itemId", "itemIndex") if body.get(key) is not None}
    if path == "/api/tools/call":
        for key in ("itemId", "itemIndex"):
            if payload.get(key) is not None:
                if key in target_body and target_body[key] != payload[key]:
                    raise RequestError("工具作用对象不一致", 409, "ITEM_TARGET_CONFLICT")
                target_body[key] = payload[key]
    if (target_body or path in {"/api/focus", "/api/fields"}) and expected_revision is None:
        raise RequestError("此操作需要 expectedRevision", code="REVISION_REQUIRED")
    if expected_revision is not None:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise RequestError("expectedRevision 必须是非负整数", code="INVALID_REVISION")
        if expected_revision != agent.state.get("revision", 0):
            raise RequestError("订单已更新，请刷新后重试", 409, "REVISION_CONFLICT")
        agent._cas_expected_revision = expected_revision
    index = _resolve_item_target(agent, target_body)
    if target_body:
        body = {**body, "itemIndex": index}
    if path == "/api/focus":
        return focus_snapshot(agent, body)
    if path == "/api/fields":
        try:
            result = agent.edit_fields(str(body.get("action", "")), body.get("values"), body.get("fields"), index)
            result["requestContext"] = {"itemId": agent.state["order"]["items"][index]["itemId"] if index is not None else None,
                                        "itemIndex": index, "expectedRevision": expected_revision}
            return result
        except ValueError as error:
            raise RequestError(str(error), code="INVALID_FIELD")
    if path == "/api/tools/call" and target_body:
        payload = {key: value for key, value in payload.items() if key != "itemId"}
        body = {**body, "payload": {**payload, "itemIndex": index}}
    routes = {
        "/api/session": lambda: session_snapshot(agent),
        "/api/session/context": lambda: agent.context_snapshot(_item_index(body)),
        "/api/chat": lambda: agent.chat(str(body.get("text", "")), body.get("patch"), _item_index(body)),
        "/api/choose": lambda: agent.choose(str(body.get("optionId", "")), _item_index(body)),
        "/api/generate": lambda: agent.generate(),
        "/api/confirm": lambda: agent.confirm(str(body.get("note", ""))),
        "/api/quote/status": lambda: agent.quote_status(str(body.get("requestId", "")).strip() or None),
        "/api/quote/cancel": lambda: agent.cancel_quote(
            str(body.get("requestId", "")).strip() or None,
            str(body.get("reason", "用户取消询价")),
        ),
        "/api/preflight": lambda: _preflight(agent, body),
        "/api/platform": lambda: agent.set_platform(str(body.get("platformId", "generic"))),
        "/api/tools/call": lambda: agent.call_tool(str(body.get("toolName", "")), body.get("payload", {})),
    }
    action = routes.get(path)
    if action is None:
        raise RequestError("接口不存在", 404, "NOT_FOUND")
    result = action()
    if isinstance(result, dict):
        result["requestContext"] = {"itemId": target_body.get("itemId") or (agent.state["order"]["items"][index]["itemId"] if index is not None else None),
                                    "itemIndex": index, "expectedRevision": expected_revision}
    return result


def focus_snapshot(agent: Agent, body: dict[str, Any]) -> dict[str, Any]:
    revision = body.get("expectedRevision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise RequestError("上下文切换需要 expectedRevision", code="REVISION_REQUIRED")
    if revision != agent.state.get("revision", 0):
        raise RequestError("订单已更新，请恢复最新订单", 409, "REVISION_CONFLICT")
    index = _resolve_item_target(agent, body)
    return order_history_snapshot(agent.memory, agent.id, item_index=index, override_focus=True,
                                  expected_revision=revision)


def session_snapshot(agent: Agent) -> dict[str, Any]:
    """Create a new session once; restoring an existing order is read-only.

    Browser reloads must not append workflow runs, consume a CAS revision, or
    invalidate an existing quote. Explicit Agent.snapshot callers retain the
    existing traced-operation behavior outside this HTTP read boundary.
    """
    if agent.memory.store.load_order(agent.id) is not None:
        return order_history_snapshot(agent.memory, agent.id)
    return agent.snapshot()


def _history_record(memory: Memory, order_id: str) -> dict[str, Any]:
    if not _session_id(order_id):
        raise RequestError("订单标识无效", code="INVALID_ORDER")
    record = memory.store.load_order(order_id)
    if record is None:
        raise RequestError("订单不存在", 404, "ORDER_NOT_FOUND")
    return record


def order_history_snapshot(memory: Memory, order_id: str, *, item_index: int | None = None,
                           override_focus: bool = False, expected_revision: int | None = None) -> dict[str, Any]:
    """Load UI history without running tools or consuming a workflow revision."""
    record = _history_record(memory, order_id)
    if expected_revision is not None and record["revision"] != expected_revision:
        raise RequestError("订单已更新，请恢复最新订单", 409, "REVISION_CONFLICT")
    agent = Agent(memory, record["sessionId"], planner=planner_for_request())
    # Render the same revision whose timestamps/status were read above, even
    # if another process commits between the two reads.
    agent.state = normalize_state(deepcopy(record["state"]))
    if override_focus:
        agent.state["activeItemIndex"] = item_index
    state = agent.state
    context = agent.context_snapshot()
    validation = validate_order(state["order"])
    active_index = agent._item_index()
    active = agent._item_order(active_index) if active_index is not None else None
    review = agent.review()
    return {**deepcopy(state), "orderId": record["orderId"], "sessionId": record["sessionId"],
            "review": review, "handoffDocument": handoff_document(state, record["orderId"], review),
            "activeItemId": active.get("itemId") if active else None,
            "status": record["status"], "createdAt": record["createdAt"], "updatedAt": record["updatedAt"],
            "canonicalOrder": canonical_order(state["order"], state.get("fieldMeta")),
            "history": deepcopy(state.get("messages", [])), "messages": [], "quickReplies": [],
            "options": agent._context_options(state, active), "availableTools": agent.available_tools(),
            "validation": validation, "missingFields": validation.get("missing", []),
            "readiness": validation.get("readiness", 0), "nextAction": agent._next_action(validation),
            "workflowStage": context["workflowStage"], "workflowLabel": WORKFLOW_LABELS[context["workflowStage"]],
            "productProfile": parameter_state(state["order"]), "knowledge": deepcopy(KNOWLEDGE_MANIFEST),
            "decision": agent._decision_summary(validation), "supplierCapability": context.get("supplierCapability"),
            "activeItemSelectedOption": active.get("selectedOption") if active else None,
            "quoteRequest": deepcopy(agent._quote_request()), "toolTrace": ["恢复会话记忆"],
            "runTrace": deepcopy((state.get("lastRun") or {}).get("events", [])),
            "llm": agent.planner.public_config() if hasattr(agent.planner, "public_config") else None}


def dispatch_history_post(path: str, body: dict[str, object], memory: Memory) -> dict[str, Any] | None:
    if path == "/api/orders/import-local-history":
        records = body.get("orders")
        if not isinstance(records, list) or len(records) > 100:
            raise RequestError("orders 必须是至多 100 项的数组", code="INVALID_PAYLOAD")
        imported, skipped, errors = [], [], []
        for index, raw in enumerate(records):
            try:
                if not isinstance(raw, dict) or not isinstance(raw.get("order"), dict):
                    raise RequestError("历史项需要 id 和 order", code="INVALID_PAYLOAD")
                order_id = _session_id(raw.get("id") or raw.get("orderId"))
                if not order_id:
                    raise RequestError("历史项缺少 id", code="INVALID_ORDER")
                if memory.store.load_order(order_id) is not None:
                    skipped.append(order_id)
                    continue
                state = Memory.fresh_state()
                state["order"] = raw["order"]
                state = normalize_state(state)
                # Browser snapshots are unconfirmed input, never an approval
                # or provider response. Retain the original record as evidence.
                try:
                    memory.store.create_order({"orderId": order_id, "state": state,
                                               "events": [{"eventType": "browser_history_imported", "actor": "migration",
                                                           "payload": {"source": "localStorage", "original": raw}}]})
                    imported.append(order_id)
                except StoreConflictError:
                    skipped.append(order_id)
            except (RequestError, TypeError, ValueError) as error:
                errors.append({"index": index, "message": str(error)})
        return {"imported": imported, "skipped": skipped, "errors": errors,
                "orders": memory.store.list_orders()["orders"]}
    match = re.fullmatch(r"/api/orders/([^/]+)/purge", path)
    if match:
        order_id = match.group(1)
        record = _history_record(memory, order_id)
        if record["status"] != "archived":
            raise RequestError("只有已归档订单可以永久删除", 409, "ORDER_NOT_ARCHIVED")
        if body.get("confirm") is not True:
            raise RequestError("永久删除需要 confirm=true", 400, "CONFIRM_REQUIRED")
        revision = body.get("expectedRevision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise RequestError("删除需要 expectedRevision 整数", code="INVALID_REVISION")
        try:
            return memory.store.purge_order(order_id, revision)
        except StoreConflictError as error:
            raise RequestError(f"订单已更新，请刷新后重试（当前 revision={error.current}）", 409, "REVISION_CONFLICT")
        except KeyError:
            raise RequestError("订单不存在", 404, "ORDER_NOT_FOUND")
        except ValueError as error:
            raise RequestError(str(error), 409, "ORDER_NOT_ARCHIVED")
    match = re.fullmatch(r"/api/orders/([^/]+)/archive", path)
    if match:
        order_id = match.group(1)
        _history_record(memory, order_id)
        revision = body.get("expectedRevision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise RequestError("归档需要 expectedRevision 整数", code="INVALID_REVISION")
        try:
            return summary(memory.store.delete_order(order_id, revision))
        except StoreConflictError as error:
            raise RequestError(f"订单已更新，请刷新后重试（当前 revision={error.current}）", 409, "REVISION_CONFLICT")
    return None


def dispatch_get(path: str) -> dict[str, object] | None:
    """Return public GET payloads; ``None`` delegates to static file handling."""
    parsed = urlsplit(path)
    path, query = unquote(parsed.path), parse_qs(parsed.query)
    if path == "/api/storage":
        return session_memory().store.metadata()
    if path == "/api/orders":
        try:
            limit, offset = int(query.get("limit", [50])[0]), int(query.get("offset", [0])[0])
        except (TypeError, ValueError):
            raise RequestError("分页参数必须是整数", code="INVALID_PAGINATION")
        filters = {"limit": limit, "offset": offset,
                   "includeArchived": query.get("includeArchived", ["false"])[0].lower() == "true",
                   "status": query.get("status", [None])[0]}
        return session_memory().store.list_orders(filters)
    match = re.fullmatch(r"/api/orders/([^/]+)/handoff", path)
    if match:
        memory = session_memory()
        record = _history_record(memory, match.group(1))
        try:
            revision = int(query.get("revision", [""])[0])
        except (TypeError, ValueError):
            raise RequestError("交接单读取需要 revision", code="REVISION_REQUIRED")
        if revision != record["revision"]:
            raise RequestError("订单版本已变化，请重新核对后导出", 409, "REVISION_CONFLICT")
        agent = Agent(memory, record["sessionId"], planner=planner_for_request())
        agent.state = normalize_state(deepcopy(record["state"]))
        return handoff_document(agent.state, record["orderId"], agent.review())
    match = re.fullmatch(r"/api/orders/([^/]+)(/export)?", path)
    if match:
        memory = session_memory()
        record = _history_record(memory, match.group(1))
        if match.group(2):
            exported = memory.store.export_order(record["orderId"])
            exported["canonicalOrder"] = canonical_order(exported["state"].get("order", {}), exported["state"].get("fieldMeta"))
            return exported
        return order_history_snapshot(memory, record["orderId"])
    if path == "/api/health":
        return {"ok": True}
    if path == "/api/settings":
        return llm_settings()
    if path == "/api/platforms":
        return {"platforms": list_platforms()}
    if path == "/api/tools":
        tools = [{**dict(meta), **dict(TOOL_SCHEMAS.get(name, {}))} for name, meta in TOOL_META.items()]
        return {"tools": tools}
    if path == "/api/tools/stats":
        # v1.3.0 tool-call telemetry. window accepts 7d/30d/90d (default 30d);
        # the guard above already enforced the same token rule as every other
        # /api/* route, and the numbers stay on this machine's database.
        raw_window = str((query.get("window") or ["30d"])[0] or "").strip().lower()
        match = re.fullmatch(r"(7|30|90)d?", raw_window)
        if not match:
            raise RequestError("window 参数仅支持 7d、30d 或 90d", code="INVALID_WINDOW")
        store = session_memory().store
        loader = getattr(store, "load_tool_stats", None)
        stats = (loader(int(match.group(1))) if callable(loader)
                 else aggregate_tool_calls([], int(match.group(1))))
        stats["message"] = "仅统计本机数据库中的工具调用记录，不会上传或共享。"
        return stats
    if path == "/api/products":
        return catalog_payload()
    return None


class InFlightRequests:
    """Count the HTTP requests being handled so shutdown can drain them."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active = 0

    def enter(self) -> None:
        with self._condition:
            self._active += 1

    def leave(self) -> None:
        with self._condition:
            self._active -= 1
            if self._active <= 0:
                self._condition.notify_all()

    def active(self) -> int:
        with self._condition:
            return self._active

    def wait_for_idle(self, timeout: float) -> bool:
        """Wait until no request is in flight; ``False`` means the grace period expired."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while self._active > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


IN_FLIGHT = InFlightRequests()


def reject_overloaded(request) -> None:
    """Answer a connection we cannot serve with a bounded 503 and close it.

    The rejection happens before any worker thread exists, so a flood of slow
    connections cannot grow the thread count or open more SQLite connections.
    """
    body = json.dumps({"ok": False, "code": "SERVER_BUSY", "message": "服务繁忙，请稍后重试"},
                      ensure_ascii=False).encode("utf-8")
    response = b"".join((
        b"HTTP/1.1 503 Service Unavailable\r\n",
        b"Content-Type: application/json; charset=utf-8\r\n",
        b"Content-Length: ", str(len(body)).encode("ascii"), b"\r\n",
        b"Connection: close\r\n",
        b"Retry-After: 1\r\n",
        b"\r\n",
        body,
    ))
    try:
        request.sendall(response)
    except OSError:
        pass
    finally:
        try:
            # Read the peer's request out of the socket before teardown.
            # On Windows a close() with unread receive data — or a SHUT_RD
            # that discards it — answers with a RST that erases the 503
            # written above, so give the bytes a brief chance to arrive and
            # consume them, then half-close the write side only.
            request.settimeout(0.2)
            try:
                while request.recv(8192):
                    pass
            except OSError:
                pass
        except (AttributeError, OSError):
            pass
        try:
            request.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            request.close()
        except OSError:
            pass


class PrintOpsHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with bounded concurrency and prompt, non-blocking shutdown.

    ``ThreadingHTTPServer`` starts one thread per accepted connection with no
    upper bound, so stalled clients can exhaust threads and file descriptors.
    ``daemon_threads`` keeps a stuck client from blocking interpreter exit and
    ``block_on_close = False`` keeps ``server_close()`` from joining handler
    threads: a shutdown must never wait on a slow client.
    """

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, server_address, RequestHandlerClass,
                 max_connections: int | None = None) -> None:
        self.max_connections = max(1, int(max_connections or MAX_CONNECTIONS))
        self._connection_slots = threading.BoundedSemaphore(self.max_connections)
        self.rejected_connections = 0
        super().__init__(server_address, RequestHandlerClass)

    def process_request(self, request, client_address) -> None:
        if not self._connection_slots.acquire(blocking=False):
            self.rejected_connections += 1
            LOGGER.warning("连接被拒绝：已达并发上限 maxConnections=%s rejectedTotal=%s",
                           self.max_connections, self.rejected_connections)
            reject_overloaded(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            # Release in the worker thread so a crashing handler still frees
            # its slot instead of permanently shrinking the connection budget.
            self._connection_slots.release()

    def has_free_connection_slot(self) -> bool:
        """Report whether a new connection could be accepted right now."""
        if not self._connection_slots.acquire(blocking=False):
            return False
        self._connection_slots.release()
        return True

    def wait_for_idle(self, timeout: float) -> bool:
        return IN_FLIGHT.wait_for_idle(timeout)


def memory_path() -> Path:
    """Return the session database path the process should use.

    The module attribute wins over the constant so replacing ``server.MEMORY``
    (the seam the tests already use) still redirects the startup probe.
    """
    memory = MEMORY
    return memory.path if memory is not None else MEMORY_PATH


def session_memory() -> Memory:
    """Return the process-wide session store, building it on first use."""
    global MEMORY
    memory = MEMORY
    if memory is None:
        memory = Memory(MEMORY_PATH)
        MEMORY = memory
    return memory


def memory_write_probe(path: str | Path) -> None:
    """Prove the session database can be opened and written, without changing data.

    A write transaction is required, not just ``W_OK`` on the directory: it is
    the only check that also covers a read-only mount, a read-only database
    file and a missing permission to create the journal/WAL sidecar.  The
    transaction is rolled back, so the probe leaves no rows or tables behind.
    """
    database = Path(path)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(database), timeout=5.0)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE IF NOT EXISTS printops_startup_probe (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO printops_startup_probe (id) VALUES (1)")
        connection.rollback()
    finally:
        connection.close()


def startup_checks(port: int | None = None) -> list[str]:
    """Return actionable startup problems; an empty list means safe to serve.

    These run before the socket opens so a broken deployment fails fast with a
    readable message instead of a traceback, or a UI that loads and then 500s
    on the first request.  ``port`` is only probed on demand because the real
    bind happens immediately afterwards during a normal start.
    """
    problems: list[str] = []
    if PORT_ENV_ERROR:
        problems.append(PORT_ENV_ERROR)
    for name in sorted(set(STATIC_FILES.values())):
        if not (ROOT / name).is_file():
            problems.append(f"缺少静态资源 {name}（部署包不完整）")
    try:
        configured_store = getattr(MEMORY, "store", None)
        mode = configured_store.metadata()["mode"] if configured_store is not None else os.getenv("PRINTOPS_STORE", "sqlite").strip() or "sqlite"
        if mode == "sqlite":
            memory_write_probe(memory_path())
        else:
            session_memory()  # Validate an explicit memory/custom selection.
    except (OSError, sqlite3.Error, ValueError, TypeError) as error:
        problems.append(f"会话数据库不可写：{memory_path()}（{error}）")
    if port is not None:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((LOOPBACK_HOST, int(port)))
            finally:
                probe.close()
        except (OSError, ValueError, OverflowError) as error:
            problems.append(f"端口 {port} 不可用：{error}"
                            "（端口可能已被占用；可用 PRINTOPS_PORT 更换）")
    return problems


def config_write_probe(directory: str | Path) -> str:
    """Prove the settings directory accepts a temp file and an atomic replace.

    Startup only logs this: a rules-mode session is still useful when the
    config cannot be written, but the failure should be known before the user
    fills the settings form and gets a 500 on save.
    """
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".printops-config-probe-", dir=str(directory))
        probe_path, target = Path(probe), None
        try:
            os.close(fd)
            target = probe_path.with_name(probe_path.name + ".target")
            os.replace(probe_path, target)
        finally:
            probe_path.unlink(missing_ok=True)
            if target is not None:
                target.unlink(missing_ok=True)
    except OSError as error:
        return f"模型配置文件目录不可写：{directory}（{error}）"
    return ""


def configure_logging() -> None:
    """Send operational logs to stderr; ``PRINTOPS_LOG_LEVEL`` overrides the level."""
    level_name = os.getenv("PRINTOPS_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int) or isinstance(level, bool):
        level = logging.INFO
    if not logging.getLogger().handlers:
        logging.basicConfig(level=level, stream=sys.stderr,
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
    LOGGER.setLevel(level)


def build_server(port: int, host: str = LOOPBACK_HOST,
                 max_connections: int | None = None) -> PrintOpsHTTPServer:
    """Bind the development-grade HTTP server to the loopback interface."""
    return PrintOpsHTTPServer((host, int(port)), Handler, max_connections=max_connections)


def install_signal_handlers(server: Any, stream: Any = None) -> Callable[[], None]:
    """Route SIGINT/SIGTERM into a graceful shutdown; return a restore callable.

    ``shutdown()`` must not run inside the signal handler itself: it waits for
    ``serve_forever`` to return, which would deadlock the main thread.  A short
    helper thread requests the stop instead, and a second signal exits hard so
    an operator is never forced to reach for SIGKILL.
    """
    previous: dict[int, Any] = {}
    log_stream = stream if stream is not None else sys.stderr
    seen = {"count": 0}

    def _handle(signum, frame) -> None:
        seen["count"] += 1
        if seen["count"] > 1:
            print(f"PrintOps: 再次收到信号 {signum}，强制退出", file=log_stream, flush=True)
            os._exit(128 + int(signum))
        LOGGER.info("收到停止信号 signal=%s，开始优雅关闭", signum)
        threading.Thread(target=server.shutdown, name="printops-graceful-shutdown",
                         daemon=True).start()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, _handle)
        except (ValueError, OSError, AttributeError):
            previous.pop(signum, None)

    def restore() -> None:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError, TypeError):
                pass

    return restore


def main(argv: list[str] | None = None) -> int:
    """Run the local HTTP API with fail-fast startup and graceful shutdown."""
    stdio_utf8.configure()
    parser = argparse.ArgumentParser(prog="server.py",
                                     description="PrintOps 本地 HTTP API（纯标准库）")
    parser.add_argument("--port", type=int, default=None,
                        help=f"监听端口（默认 PRINTOPS_PORT，回退 {DEFAULT_PORT}）")
    parser.add_argument("--check", action="store_true",
                        help="只运行启动自检（含端口探测）并退出，不提供服务")
    args = parser.parse_args(argv)
    configure_logging()
    port = PORT if args.port is None else args.port
    if not 0 <= port <= 65535:
        print(f"PrintOps 启动失败：端口 {port} 超出 0-65535", file=sys.stderr)
        return 1

    problems = startup_checks(port if args.check else None)
    config_problem = config_write_probe(LLM_CONFIG_PATH.parent)
    if args.check:
        print(json.dumps({"ok": not problems and not config_problem, "port": port,
                          "memoryPath": str(memory_path()), "configPath": str(LLM_CONFIG_PATH),
                          "configWritable": not config_problem,
                          "problems": problems + ([config_problem] if config_problem else [])},
                         ensure_ascii=False, allow_nan=False, separators=(",", ":")))
        return 1 if problems or config_problem else 0
    if problems:
        for problem in problems:
            print(f"PrintOps 启动自检失败：{problem}", file=sys.stderr)
        return 1
    if config_problem:
        LOGGER.warning("%s（模型设置将无法保存）", config_problem)

    try:
        httpd = build_server(port)
    except OSError as error:
        print(f"PrintOps 启动失败：无法监听 {LOOPBACK_HOST}:{port}（{error}）。"
              f"若端口被占用，可先结束占用进程（macOS: lsof -nP -iTCP:{port} -sTCP:LISTEN）"
              f"或用 PRINTOPS_PORT 换端口。", file=sys.stderr)
        return 1

    restore_signals = install_signal_handlers(httpd)
    try:
        bound_port = httpd.server_address[1]
        LOGGER.info("listening url=http://localhost:%s maxConnections=%s pid=%s",
                    bound_port, httpd.max_connections, os.getpid())
        print(f"PrintOps order agent: http://localhost:{bound_port}")
        print(f"本地访问令牌（curl 调试用 -H 'X-PrintOps-Token: ...'）：{ACCESS_TOKEN}")
        httpd.serve_forever(poll_interval=0.25)
    finally:
        restore_signals()
        if not httpd.wait_for_idle(SHUTDOWN_GRACE_SECONDS):
            LOGGER.warning("优雅关闭超时，仍有 %s 个请求在途", IN_FLIGHT.active())
        httpd.server_close()
        LOGGER.info("server stopped pid=%s", os.getpid())
    return 0


class Handler(BaseHTTPRequestHandler):
    # Bound how long a client may occupy a worker thread mid-request; a stalled
    # read must not hold a slot (and a SQLite connection) forever.
    timeout = REQUEST_TIMEOUT_SECONDS if REQUEST_TIMEOUT_SECONDS > 0 else None

    def _expire_request_read(self) -> None:
        self.close_connection = True
        try:
            # Interrupt an in-progress buffered header/body read while
            # leaving a brief opportunity for a structured 408 response.
            self.connection.shutdown(socket.SHUT_RD)
        except OSError:
            pass

    def _arm_deadline_timer(self, deadline: float) -> None:
        self._cancel_deadline_timer()
        timer = threading.Timer(max(0.0, deadline - time.monotonic()), self._expire_request_read)
        timer.daemon = True
        timer.start()
        self._deadline_timer = timer

    def _cancel_deadline_timer(self) -> None:
        timer = getattr(self, "_deadline_timer", None)
        if timer is not None:
            timer.cancel()
            self._deadline_timer = None

    def _extend_request_deadline(self, total_seconds: float) -> None:
        """Give a route with declared network work its own capped budget.

        Mirrors the dsh clampTimeout rule: the route budget may exceed the
        global request timeout but never the hard cap, and handlers with a
        deliberately tiny budget (tests, hardened deployments) keep it.
        """
        current = current_deadline()
        received = getattr(self, "_request_received", None)
        if current is None or received is None:
            return
        if not self.timeout or self.timeout < MIN_ROUTE_EXTENSION_BASE_SECONDS:
            return
        target = received + min(max(float(self.timeout), total_seconds), ROUTE_DEADLINE_CAP_SECONDS)
        if target <= current:
            return
        replace_deadline(target)
        self._arm_deadline_timer(target)

    def handle_one_request(self):
        # Start before request-line/header parsing. Per-read socket timeouts
        # alone let a client send one byte before every timeout indefinitely.
        self._request_received = time.monotonic()
        seconds = self.timeout
        deadline = (self._request_received + seconds
                    if seconds is not None and math.isfinite(seconds) and seconds > 0 else None)
        with deadline_scope(deadline):
            if deadline is not None:
                self._arm_deadline_timer(deadline)
            try:
                return super().handle_one_request()
            except DeadlineExceeded:
                self.close_connection = True
                if self.command:
                    self.error_response(RequestError("请求总时限已到，请核对订单状态后重试", 408, "REQUEST_DEADLINE"))
            except OSError:
                self.close_connection = True
                # _expire_request_read's shutdown(SHUT_RD) surfaces as an OSError here
                # (WSAECONNABORTED on Windows) before any read-level check can
                # raise DeadlineExceeded.  Clients that already sent a request
                # line still get the structured 408 they would see on POSIX
                # instead of a silent abort.
                if self.command and deadline is not None and time.monotonic() >= deadline - 0.01:
                    try:
                        self.error_response(RequestError("请求总时限已到，请核对订单状态后重试", 408, "REQUEST_DEADLINE"))
                    except OSError:
                        pass
            finally:
                self._cancel_deadline_timer()

    def finish(self):
        """Close the connection without provoking a Windows RST.

        ``close()`` with unread receive data turns into WSAECONNRESET on
        Windows and can erase an already-written response — the 408 deadline
        reply among them.  Flush, drop pending client bytes within a bounded,
        non-blocking budget, then let the normal FIN teardown run.
        """
        try:
            self.wfile.flush()
        except OSError:
            pass
        try:
            self.connection.setblocking(False)
            for _ in range(16):
                try:
                    if not self.connection.recv(8192):
                        break
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    break
        except OSError:
            pass
        super().finish()

    def _origin_allowed(self) -> bool:
        """Same-origin check: a present Origin header must match Host."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            return urlsplit(origin).netloc == (self.headers.get("Host") or "")
        except ValueError:
            return False

    def _authorized(self) -> bool:
        token = self.headers.get(TOKEN_HEADER, "")
        try:
            return secrets.compare_digest(token.encode("utf-8"), ACCESS_TOKEN.encode("utf-8"))
        except (TypeError, ValueError, UnicodeEncodeError):
            return False

    def _discard_body(self) -> None:
        """Read and drop a pending request body.

        Closing a connection with unread body data makes Windows emit a RST
        that can abort an unrelated later connection accepted from the same
        backlog, so every early rejection drains the declared body first.
        """
        raw_length = self.headers.get("Content-Length")
        try:
            remaining = int(raw_length) if raw_length else 0
        except (TypeError, ValueError):
            return
        if remaining <= 0:
            return
        try:
            self._drain(remaining)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _guard(self) -> bool:
        """Reject cross-origin and tokenless API access with a JSON error."""
        if not self._origin_allowed():
            self._discard_body()
            self.error_response(RequestError("跨来源请求被拒绝", 403, "FORBIDDEN_ORIGIN"))
            return False
        if not self._authorized():
            self._discard_body()
            self.error_response(RequestError("本地访问令牌缺失或不匹配，请刷新页面后重试", 401, "UNAUTHORIZED"))
            return False
        return True

    def do_GET(self):
        self._begin_request()
        path = unquote(urlsplit(self.path).path)
        try:
            check_deadline()
            if path.startswith("/api/"):
                if path != "/api/health" and not self._guard():
                    return None
                public_payload = dispatch_get(self.path)
                if public_payload is not None:
                    return self.reply(public_payload)
                return self.error_response(RequestError("接口不存在", 404, "NOT_FOUND"))
            return self.serve_static(path)
        except RequestError as error:
            return self.error_response(error)
        except DeadlineExceeded:
            return self.error_response(RequestError("请求总时限已到，请核对订单状态后重试", 408, "REQUEST_DEADLINE"))
        except socket.timeout:
            return self.error_response(RequestError("请求读取超时，请重试", 408, "REQUEST_TIMEOUT"))
        except CLIENT_ABORTS:
            return None
        except Exception:
            LOGGER.exception("GET request failed requestId=%s path=%s", self.request_id, path)
            return self.error_response(RequestError("服务暂时无法处理请求，请重试", 500, "INTERNAL_ERROR"))
        finally:
            self._finish_request()

    def do_POST(self):
        self._begin_request()
        path = unquote(urlsplit(self.path).path)
        try:
            check_deadline()
            if not self._guard():
                return None
            body = self.read_json()
            if path == "/api/settings":
                try:
                    return self.reply(update_llm_settings(body, self.request_id))
                except ValueError as error:
                    raise RequestError(str(error), 400, "INVALID_SETTINGS")
            if path == "/api/model/test":
                if str(body.get("url", "")).strip() or bool(getattr(PLANNER, "enabled", False)):
                    # The probe announces its own bounded workload: one DNS
                    # check plus three capability hops in a single round trip,
                    # each capped at
                    # llm_adapter.CAPABILITY_PROBE_REQUEST_MAX_SECONDS.  Give
                    # the route the matching capped budget instead of letting
                    # the global deadline abort a slow-endpoint test.
                    self._extend_request_deadline(MODEL_TEST_BUDGET_SECONDS)
                try:
                    return self.reply(test_llm_capabilities(body))
                except ValueError as error:
                    raise RequestError(str(error), 400, "INVALID_SETTINGS")
            if path.startswith("/api/orders/"):
                history_result = dispatch_history_post(path, body, session_memory())
                if history_result is not None:
                    return self.reply(history_result)
                raise RequestError("接口不存在", 404, "NOT_FOUND")
            # Snapshot the planner before the session lock so the chat route
            # can announce its bounded model workload up front: a legitimate
            # multi-round tool conversation must not be cut down by the global
            # request deadline into 408 REQUEST_DEADLINE mid-turn.
            planner = planner_for_request()
            if path == "/api/chat" and getattr(planner, "enabled", False):
                self._extend_request_deadline(planner_chat_budget_seconds(planner))
            session_id = _session_id(body.get("sessionId"))
            if session_id:
                # Lock before loading state so concurrent requests cannot read
                # the same snapshot and overwrite each other's changes.
                with deadline_lock(_session_lock(session_id)):
                    agent = Agent(session_memory(), session_id, planner=planner)
                    return self.reply(dispatch_post(path, body, agent))
            agent = Agent(session_memory(), planner=planner)
            with deadline_lock(_session_lock(agent.id)):
                return self.reply(dispatch_post(path, body, agent))
        except RequestError as error:
            return self.error_response(error)
        except DeadlineExceeded:
            return self.error_response(RequestError("请求总时限已到，请核对订单状态后重试", 408, "REQUEST_DEADLINE"))
        except (RevisionConflictError, StoreConflictError):
            return self.error_response(RequestError("订单已更新，请刷新后重试", 409, "REVISION_CONFLICT"))
        except (TypeError, ValueError):
            return self.error_response(RequestError("请求参数格式不正确", 400, "INVALID_PAYLOAD"))
        except socket.timeout:
            return self.error_response(RequestError("请求读取超时，请重试", 408, "REQUEST_TIMEOUT"))
        except CLIENT_ABORTS:
            return None
        except Exception:
            LOGGER.exception("POST request failed requestId=%s path=%s", self.request_id, path)
            return self.error_response(RequestError("本次处理失败，请重试", 500, "AGENT_ERROR"))
        finally:
            self._finish_request()

    def read_json(self) -> dict[str, object]:
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            raise RequestError("请求缺少内容长度", 411, "LENGTH_REQUIRED")
        try:
            length = int(content_length)
        except (TypeError, ValueError):
            raise RequestError("请求内容长度无效", 400, "INVALID_LENGTH")
        if length < 0:
            raise RequestError("请求内容长度无效", 400, "INVALID_LENGTH")
        if length > MAX_BODY_BYTES:
            # Drain the declared body first so the client can finish writing
            # and actually receive the 413 instead of a connection reset.
            self._drain(length)
            raise RequestError("请求内容过大，请缩小后重试", 413, "PAYLOAD_TOO_LARGE")
        raw = self.rfile.read(length)
        check_deadline()
        if len(raw) != length:
            raise RequestError("请求内容未完整接收", 400, "INCOMPLETE_BODY")
        if not raw:
            return {}
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type and content_type != "application/json":
            raise RequestError("请求必须使用 JSON 格式", 415, "UNSUPPORTED_MEDIA_TYPE")
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise RequestError("请求 JSON 无法解析", 400, "INVALID_JSON")
        if not isinstance(value, dict):
            raise RequestError("请求体必须是 JSON 对象", 400, "INVALID_PAYLOAD")
        return value

    def _drain(self, length: int, cap: int = 16 * 1024 * 1024) -> None:
        remaining = min(length, cap)
        while remaining > 0:
            check_deadline()
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def serve_static(self, path: str):
        """Serve only whitelisted static files; the page carries the access token."""
        name = STATIC_FILES.get(path)
        if name is None:
            return self.error_response(RequestError("资源不存在", 404, "NOT_FOUND"))
        try:
            raw = (ROOT / name).read_bytes()
        except OSError:
            return self.error_response(RequestError("资源不存在", 404, "NOT_FOUND"))
        if name == "index.html":
            raw = index_html_with_token(raw)
        self._bound_response_timeout()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_CONTENT_TYPES[name])
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", getattr(self, "request_id", ""))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except CLIENT_ABORTS:
            return None

    def reply(self, data, status=200):
        self._bound_response_timeout(allow_expired=status >= 400)
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Request-ID", getattr(self, "request_id", ""))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except CLIENT_ABORTS:
            return None

    def error_response(self, error: RequestError):
        return self.reply({"ok": False, "code": error.code, "message": error.message,
                           "requestId": getattr(self, "request_id", "")}, error.status)

    def _bound_response_timeout(self, allow_expired: bool = False) -> None:
        try:
            budget = remaining()
        except DeadlineExceeded:
            if not allow_expired:
                raise
            self.close_connection = True
            budget = 0.1  # Bounded error delivery, never another business budget.
        if budget is not None and hasattr(self, "connection"):
            self.connection.settimeout(budget)

    def _begin_request(self) -> None:
        self.request_id = uuid.uuid4().hex[:12]
        self._request_started = getattr(self, "_request_received", time.monotonic())
        self._response_status = None
        IN_FLIGHT.enter()

    def _finish_request(self) -> None:
        """Emit one structured line per request and release the drain counter."""
        IN_FLIGHT.leave()
        started = getattr(self, "_request_started", None)
        duration_ms = max(0.0, (time.monotonic() - started) * 1000.0) if started else 0.0
        status = getattr(self, "_response_status", None) or 0
        request_id = getattr(self, "request_id", "")
        command = getattr(self, "command", "") or ""
        path = unquote(urlsplit(getattr(self, "path", "") or "").path)
        LOGGER.info("requestId=%s method=%s path=%s status=%s durationMs=%.1f",
                    request_id, command, path, status, duration_ms,
                    extra={"request_id": request_id, "status": status,
                           "duration_ms": round(duration_ms, 1)})
        if SLOW_REQUEST_MS > 0 and duration_ms >= SLOW_REQUEST_MS:
            LOGGER.warning("慢请求 requestId=%s method=%s path=%s status=%s durationMs=%.1f",
                           request_id, command, path, status, duration_ms)

    def log_request(self, code="-", size="-"):
        """Remember the status code; ``_finish_request`` logs it once, with latency."""
        self._response_status = code

    def log_message(self, format, *args):
        """Log a protocol-level error without leaking query strings.

        ``log_error`` also runs for connections that die before a request line
        is parsed (socket timeout, reset), when ``command`` and ``path`` are
        not set yet; reading them blindly would raise AttributeError inside the
        error path itself.
        """
        command = getattr(self, "command", "") or "-"
        raw_path = getattr(self, "path", "") or ""
        try:
            path = unquote(urlsplit(raw_path).path) or "-"
        except ValueError:
            path = "-"
        try:
            detail = format % args if args else str(format)
        except (TypeError, ValueError):
            detail = str(format)
        LOGGER.info("%s %s %s", command, path, detail)


def index_html_with_token(raw: bytes) -> bytes:
    """Inject the per-process access token into the served page."""
    html = raw.decode("utf-8")
    script = f"<script>window.__PRINTOPS_TOKEN__={json.dumps(ACCESS_TOKEN)};</script>"
    if "</head>" in html:
        html = html.replace("</head>", f"{script}</head>", 1)
    else:
        html = script + html
    return html.encode("utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
