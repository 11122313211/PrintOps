"""Dependency-free stdio MCP adapter for the PrintOps domain kernel.

The adapter is deliberately small and session-bound.  It exposes a reviewed
MCP schema instead of forwarding the internal ``TOOL_SCHEMAS`` verbatim, and
all execution goes through ``Agent.call_tool`` so the existing state,
validation, audit and confirmation gates remain authoritative.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
import re
import sys
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from agent import Agent, Memory, RevisionConflictError
from llm_adapter import OpenAICompatiblePlanner
from order_model import DIMENSION_DEFAULTS, parse_quantity
from product_knowledge import KNOWLEDGE_MANIFEST, KNOWLEDGE_VERSION, catalog_payload
from supplier_adapters import PLATFORMS
from tools import validate_order


SERVER_NAME = "printops-mcp"
SERVER_VERSION = "0.1.0"
MCP_PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = {MCP_PROTOCOL_VERSION, "2025-03-26"}
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_STRING_LENGTH = 4096
MAX_INSPECTION_BYTES = 16 * 1024
MAX_MESSAGE_BYTES = 64 * 1024
MAX_ITEM_ID_LENGTH = 128
MAX_RESPONSE_BYTES = 128 * 1024
# Keep numeric arguments within the range represented exactly by JSON clients
# (notably JavaScript), and avoid converting attacker-controlled huge Python
# integers to float inside the domain tools.
MAX_SAFE_INTEGER = 2**53 - 1
MAX_METHOD_LENGTH = 128
MAX_REQUEST_ID_LENGTH = 128
DEFAULT_MEMORY_PATH = Path(__file__).resolve().parent / "data" / "agent.sqlite3"
MAX_SESSION_LOCKS = 128
MAX_PATCH_BYTES = 64 * 1024
MAX_PATCH_ID_LENGTH = 128
PATCH_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

# These are the order fields that ``Agent._item_order`` may inherit from the
# top-level compatibility projection when a multi-product item leaves them
# blank.  Their top-level provenance therefore still applies to a selected
# item, even though it is not stored under ``items.<itemId>.*``.
INHERITED_ITEM_FIELDS = (
    "purpose", "orientation", "paper", "printing", "finishing", "binding",
    "deadline", "budget",
)

# Capability levels are intentionally explicit.  L2 (external side effects)
# is not implemented by this server and must live in a separate process.
L0_TOOLS = {
    "validate_order",
    "recommend_processes",
    "explain_print_term",
    "estimate_price",
    "match_supplier_capability",
}
L1_TOOLS = {"preflight_file", "prepare_handoff", "request_supplier_quote",
            "apply_order_patch"}


class MCPError(Exception):
    """A protocol or capability error safe to return to an MCP client."""

    def __init__(self, code: str, message: str, rpc_code: int = -32602,
                 data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = str(message)[:1024]
        self.rpc_code = rpc_code
        self.data = data if isinstance(data, dict) else {}


def _request_id(value: Any) -> Any:
    """Validate a JSON-RPC request id before it can be echoed to a peer."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise MCPError("invalid_request", "请求 id 必须是字符串、数字或 null", rpc_code=-32600)
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise MCPError("invalid_request", "请求 id 超过数值上限", rpc_code=-32600)
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > MAX_SAFE_INTEGER:
            raise MCPError("invalid_request", "请求 id 必须是有限数字", rpc_code=-32600)
        return value
    if isinstance(value, str):
        if len(value) > MAX_REQUEST_ID_LENGTH:
            raise MCPError("invalid_request", "请求 id 超过长度限制", rpc_code=-32600)
        return value
    raise MCPError("invalid_request", "请求 id 必须是字符串、数字或 null", rpc_code=-32600)


def _safe_request_id(value: Any) -> Any:
    """Return a serializable id for error/result envelopes."""
    try:
        return _request_id(value)
    except MCPError:
        return None


def _json_size(value: Any) -> int:
    try:
        # MCP transports strict JSON. Python's encoder otherwise emits
        # non-standard ``NaN``/``Infinity`` tokens for hostile metadata.
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        return MAX_INSPECTION_BYTES + 1


def _reject_json_constant(value: str) -> None:
    """Reject Python's non-standard NaN/Infinity JSON extensions."""
    raise ValueError(f"invalid JSON constant: {value}")


def _string(value: Any, field: str, maximum: int = MAX_STRING_LENGTH,
            required: bool = False) -> str | None:
    if value is None:
        if required:
            raise MCPError("invalid_arguments", f"缺少参数：{field}")
        return None
    if not isinstance(value, str):
        raise MCPError("invalid_arguments", f"参数 {field} 必须是字符串")
    value = value.strip()
    if required and not value:
        raise MCPError("invalid_arguments", f"参数 {field} 不能为空")
    if len(value) > maximum:
        raise MCPError("invalid_arguments", f"参数 {field} 超过长度限制")
    return value


def _file_name(value: Any) -> str:
    value = _string(value, "fileName", 255, required=True)
    if any(char in value for char in ("/", "\\", "\x00")) or value in {".", ".."}:
        raise MCPError("invalid_arguments", "fileName 必须是安全的文件名，不能包含路径")
    return value


def _integer(value: Any, field: str, *, nullable: bool = False,
             minimum: int = 0, maximum: int = MAX_SAFE_INTEGER) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise MCPError("invalid_arguments", f"参数 {field} 必须是整数")
    if value < minimum:
        raise MCPError("invalid_arguments", f"参数 {field} 不能小于 {minimum}")
    if value > maximum:
        raise MCPError("invalid_arguments", f"参数 {field} 超过数值上限")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise MCPError("invalid_arguments", f"参数 {field} 必须是布尔值")
    return value


def _session(value: Any) -> str:
    if not isinstance(value, str) or not SESSION_ID_PATTERN.fullmatch(value):
        raise MCPError("invalid_session", "会话标识无效")
    return value


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": required, "additionalProperties": False}


SESSION_PROPERTY = {
    "type": "string",
    "description": "已绑定的 PrintOps 会话标识；订单从该会话读取，不接受外部 order 覆盖",
}
ITEM_INDEX_PROPERTY = {
    "type": ["integer", "null"],
    "minimum": 0,
    "maximum": MAX_SAFE_INTEGER,
    "description": "多产品订单中的产品项下标；单产品可省略",
}
ITEM_ID_PROPERTY = {
    "type": ["string", "null"],
    "minLength": 1,
    "maxLength": MAX_ITEM_ID_LENGTH,
    "description": "多产品订单中的稳定产品项 ID；可与 itemIndex 二选一",
}


def _tool_definitions(allowed: set[str]) -> list[dict[str, Any]]:
    """Return reviewed MCP schemas, never the internal provider-neutral ones."""
    definitions: dict[str, dict[str, Any]] = {
        "validate_order": {
            "description": "校验绑定会话的当前订单，不接受外部 order 覆盖。",
            "inputSchema": _schema({"sessionId": SESSION_PROPERTY}, ["sessionId"]),
        },
        "recommend_processes": {
            "description": "为绑定会话的当前订单生成可比较的印刷工艺方案。",
            "inputSchema": _schema(
                {"sessionId": SESSION_PROPERTY, "itemIndex": ITEM_INDEX_PROPERTY,
                 "itemId": ITEM_ID_PROPERTY},
                ["sessionId"],
            ),
        },
        "explain_print_term": {
            "description": "解释一个印刷术语或工艺选择问题。",
            "inputSchema": _schema(
                {"sessionId": SESSION_PROPERTY,
                 "question": {"type": "string", "maxLength": 2000}},
                ["sessionId", "question"],
            ),
        },
        "estimate_price": {
            "description": "按版本化示例参数表估算费用量级，不构成供应商报价。",
            "inputSchema": _schema(
                {"sessionId": SESSION_PROPERTY, "itemIndex": ITEM_INDEX_PROPERTY,
                 "itemId": ITEM_ID_PROPERTY},
                ["sessionId"],
            ),
        },
        "match_supplier_capability": {
            "description": "将当前订单与静态供应商能力档案逐字段匹配。",
            "inputSchema": _schema(
                {"sessionId": SESSION_PROPERTY,
                 "platformId": {"type": ["string", "null"], "maxLength": 128},
                 "itemIndex": ITEM_INDEX_PROPERTY, "itemId": ITEM_ID_PROPERTY},
                ["sessionId"],
            ),
        },
        "preflight_file": {
            "description": "根据浏览器提供的 PDF 元数据做轻量预检；结果仍需人工印前复核。",
            "inputSchema": _schema(
                {"sessionId": SESSION_PROPERTY,
                 "fileName": {"type": "string", "maxLength": 255},
                 "sizeBytes": {"type": "integer", "minimum": 0, "maximum": MAX_SAFE_INTEGER},
                 "pageCount": {"type": ["integer", "null"], "minimum": 0,
                               "maximum": MAX_SAFE_INTEGER},
                 "encrypted": {"type": "boolean"},
                 "readable": {"type": "boolean"},
                 "inspection": {"type": ["object", "null"]},
                 "expectedSize": {"type": ["string", "null"], "maxLength": 128},
                 "itemIndex": ITEM_INDEX_PROPERTY, "itemId": ITEM_ID_PROPERTY},
                ["sessionId", "fileName", "sizeBytes", "encrypted", "readable"],
            ),
        },
        "prepare_handoff": {
            "description": "生成当前订单的本地交接草稿；不会联系供应商。",
            "inputSchema": _schema(
                {"sessionId": SESSION_PROPERTY, "itemIndex": ITEM_INDEX_PROPERTY,
                 "itemId": ITEM_ID_PROPERTY},
                ["sessionId"],
            ),
        },
        "request_supplier_quote": {
            "description": "生成待人工确认的询价草稿；不会发起外部网络请求。",
            "inputSchema": _schema(
                {"sessionId": SESSION_PROPERTY,
                 "platformId": {"type": ["string", "null"], "maxLength": 128},
                 "itemIndex": ITEM_INDEX_PROPERTY, "itemId": ITEM_ID_PROPERTY},
                ["sessionId"],
            ),
        },
        "apply_order_patch": {
            "description": "将经过证据和置信度校验的增量订单 patch 写入绑定会话；不会接受完整 order/items 替换。",
            "inputSchema": _schema(
                {"sessionId": SESSION_PROPERTY,
                 "patch": {"type": "object", "maxProperties": 32},
                 "evidence": {"type": ["array", "object"], "maxProperties": 128,
                              "maxItems": 128},
                 "confidence": {"type": ["object", "null"], "maxProperties": 128},
                 "knowledgeVersion": {"type": ["string", "null"], "maxLength": 128},
                 "reportedKnowledgeVersion": {"type": ["string", "null"], "maxLength": 128},
                 "expectedRevision": {"type": "integer", "minimum": 0,
                                      "maximum": MAX_SAFE_INTEGER},
                 "patchId": {"type": ["string", "null"], "maxLength": MAX_PATCH_ID_LENGTH},
                 "itemIndex": ITEM_INDEX_PROPERTY, "itemId": ITEM_ID_PROPERTY},
                ["sessionId", "patch", "expectedRevision"],
            ),
        },
    }
    return [{"name": name, "description": definitions[name]["description"],
             "inputSchema": definitions[name]["inputSchema"]}
            for name in definitions if name in allowed]


class PrintOpsMCP:
    """Line-oriented JSON-RPC MCP server backed by one PrintOps memory store."""

    def __init__(self, memory_path: str | Path | None = None,
                 bound_session_id: str | None = None,
                 allow_any_session: bool = False,
                 capabilities: str | None = None) -> None:
        # Resolve the implicit database beside this adapter.  dsh commonly
        # launches an MCP child from its own working directory, so a cwd-
        # relative default would silently create a second, empty session DB.
        configured_path = memory_path
        if configured_path is None or configured_path == "":
            configured_path = os.getenv("PRINTOPS_MEMORY_PATH") or DEFAULT_MEMORY_PATH
        self.memory = Memory(configured_path)
        configured_session = bound_session_id or os.getenv("PRINTOPS_MCP_SESSION_ID", "")
        if configured_session:
            self.bound_session_id = _session(configured_session)
        else:
            self.bound_session_id = None
        self.allow_any_session = bool(allow_any_session)
        self.allowed_tools = self._parse_capabilities(
            capabilities if capabilities is not None else os.getenv("PRINTOPS_MCP_CAPABILITIES", "L0")
        )
        self._locks: dict[str, threading.Lock] = {}
        self._lock_refs: dict[str, int] = {}
        self._locks_guard = threading.Lock()
        self.initialized = False

    @staticmethod
    def _parse_capabilities(value: str) -> set[str]:
        levels = {part.strip().upper() for part in str(value).split(",") if part.strip()}
        if not levels:
            levels = {"L0"}
        unknown = levels - {"L0", "L1"}
        if unknown:
            raise ValueError(f"unsupported MCP capability: {', '.join(sorted(unknown))}")
        allowed = set(L0_TOOLS)
        if "L1" in levels:
            allowed.update(L1_TOOLS)
        return allowed

    def _evict_idle_locks_locked(self, limit: int = MAX_SESSION_LOCKS) -> None:
        """Trim the per-session lock cache; caller must hold ``_locks_guard``."""
        if len(self._locks) <= limit:
            return
        for session_id, lock in list(self._locks.items()):
            if len(self._locks) <= limit:
                break
            if self._lock_refs.get(session_id, 0) == 0 and not lock.locked():
                self._locks.pop(session_id, None)
                self._lock_refs.pop(session_id, None)

    @contextmanager
    def _lock_for(self, session_id: str):
        """Yield a session lock while pinning it against cache eviction.

        The reference is registered before blocking on ``acquire``.  A waiting
        caller therefore cannot have its lock entry evicted and replaced by a
        second lock for the same session.
        """
        with self._locks_guard:
            # Make room for a new idle entry when possible. Active/waiting
            # entries remain pinned and may temporarily exceed the bound.
            if session_id not in self._locks and len(self._locks) >= MAX_SESSION_LOCKS:
                self._evict_idle_locks_locked(MAX_SESSION_LOCKS - 1)
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[session_id] = lock
            self._lock_refs[session_id] = self._lock_refs.get(session_id, 0) + 1
        try:
            lock.acquire()
            yield lock
        finally:
            lock.release()
            with self._locks_guard:
                refs = self._lock_refs.get(session_id, 1) - 1
                if refs > 0:
                    self._lock_refs[session_id] = refs
                else:
                    self._lock_refs.pop(session_id, None)
                self._evict_idle_locks_locked()

    def _require_session(self, args: dict[str, Any]) -> str:
        session_id = _session(args.get("sessionId"))
        if self.bound_session_id and session_id != self.bound_session_id:
            raise MCPError("invalid_session", "会话未绑定到当前 MCP 进程")
        if not self.bound_session_id and not self.allow_any_session:
            raise MCPError("invalid_session", "当前 MCP 进程没有绑定会话")
        return session_id

    def _validate_args(self, name: str, raw: Any) -> tuple[str, dict[str, Any]]:
        if not isinstance(name, str) or not name:
            raise MCPError("invalid_arguments", "工具名必须是非空字符串")
        if not isinstance(raw, dict):
            raise MCPError("invalid_arguments", "工具参数必须是 JSON 对象")
        args = dict(raw)
        session_id = self._require_session(args)
        args.pop("sessionId", None)

        common = {"itemIndex", "itemId"}
        allowed: dict[str, set[str]] = {
            "validate_order": set(),
            "recommend_processes": common,
            "estimate_price": common,
            "prepare_handoff": common,
            "explain_print_term": {"question"},
            "match_supplier_capability": {"platformId", "itemIndex", "itemId"},
            "request_supplier_quote": {"platformId", "itemIndex", "itemId"},
            "preflight_file": {"fileName", "sizeBytes", "pageCount", "encrypted",
                                "readable", "inspection", "expectedSize", "itemIndex", "itemId"},
            "apply_order_patch": {"patch", "evidence", "confidence", "knowledgeVersion",
                                  "reportedKnowledgeVersion", "expectedRevision", "patchId",
                                  "itemIndex", "itemId"},
        }
        if name not in allowed:
            raise MCPError("unknown_tool", f"工具不存在：{name}", rpc_code=-32601)
        if name not in self.allowed_tools:
            raise MCPError("capability_denied", f"工具未启用：{name}", rpc_code=-32601)
        # JSON object keys are strings, but callers of ``handle`` can invoke
        # this adapter directly with a Python dict.  Reject non-string keys
        # explicitly so a mixed-key dict cannot trigger ``sorted``/formatting
        # TypeErrors and get misreported as an internal server failure.
        non_string_keys = [key for key in args if not isinstance(key, str)]
        if non_string_keys:
            raise MCPError("invalid_arguments", "参数名必须是字符串")
        unknown = [key for key in args if key not in allowed[name]]
        if unknown:
            labels = sorted(str(key)[:128] for key in unknown)
            raise MCPError("invalid_arguments", f"不支持的参数：{'、'.join(labels)}")

        if "itemIndex" in args:
            args["itemIndex"] = _integer(args["itemIndex"], "itemIndex", nullable=True)
        if "itemId" in args:
            args["itemId"] = _string(args["itemId"], "itemId", MAX_ITEM_ID_LENGTH)
            if args["itemId"] == "":
                raise MCPError("invalid_arguments", "参数 itemId 不能为空")
        if name == "explain_print_term":
            args["question"] = _string(args.get("question"), "question", 2000, required=True)
        if name in {"match_supplier_capability", "request_supplier_quote"}:
            args["platformId"] = _string(args.get("platformId"), "platformId", 128)
            if args["platformId"] is not None and args["platformId"] not in PLATFORMS:
                raise MCPError("invalid_arguments", "未知目标平台")
        if name == "preflight_file":
            args["fileName"] = _file_name(args.get("fileName"))
            args["sizeBytes"] = _integer(args.get("sizeBytes"), "sizeBytes")
            if "encrypted" not in args or "readable" not in args:
                raise MCPError("invalid_arguments", "预检必须明确提供 encrypted 和 readable")
            if "pageCount" in args:
                args["pageCount"] = _integer(args["pageCount"], "pageCount", nullable=True)
            if "encrypted" in args:
                args["encrypted"] = _boolean(args["encrypted"], "encrypted")
            if "readable" in args:
                args["readable"] = _boolean(args["readable"], "readable")
            if "expectedSize" in args:
                args["expectedSize"] = _string(args.get("expectedSize"), "expectedSize", 128)
            if "inspection" in args:
                inspection = args.get("inspection")
                if inspection is not None and not isinstance(inspection, dict):
                    raise MCPError("invalid_arguments", "参数 inspection 必须是对象或 null")
                if _json_size(inspection) > MAX_INSPECTION_BYTES:
                    raise MCPError("invalid_arguments", "参数 inspection 超过大小限制")
        if name == "apply_order_patch":
            if "patch" not in args or not isinstance(args.get("patch"), dict):
                raise MCPError("invalid_arguments", "参数 patch 必须是 JSON 对象")
            patch = args["patch"]
            if _json_size(patch) > MAX_PATCH_BYTES:
                raise MCPError("invalid_arguments", "参数 patch 超过大小限制")
            # Whole-order replacement would bypass the item-scoped gateway and
            # can silently reorder or delete products. It is intentionally not
            # part of the bridge contract.
            forbidden = [key for key in patch
                         if isinstance(key, str) and key in {"items", "productTypes"}]
            if forbidden:
                raise MCPError("invalid_arguments", "patch 不允许替换 items 或 productTypes")
            if "expectedRevision" not in args:
                raise MCPError("invalid_arguments", "缺少参数：expectedRevision")
            args["expectedRevision"] = _integer(args["expectedRevision"], "expectedRevision")
            for key in ("confidence",):
                if key in args and args[key] is not None and not isinstance(args[key], dict):
                    raise MCPError("invalid_arguments", f"参数 {key} 必须是对象或 null")
                if key in args and _json_size(args[key]) > MAX_INSPECTION_BYTES:
                    raise MCPError("invalid_arguments", f"参数 {key} 超过大小限制")
            if "evidence" in args:
                evidence = args["evidence"]
                if not isinstance(evidence, (dict, list)):
                    raise MCPError("invalid_arguments", "参数 evidence 必须是数组、对象或省略")
                if _json_size(evidence) > MAX_INSPECTION_BYTES:
                    raise MCPError("invalid_arguments", "参数 evidence 超过大小限制")
            for key in ("knowledgeVersion", "reportedKnowledgeVersion"):
                if key in args:
                    args[key] = _string(args.get(key), key, 128)
            if "patchId" in args:
                patch_id = _string(args.get("patchId"), "patchId", MAX_PATCH_ID_LENGTH)
                if patch_id is not None and not PATCH_ID_PATTERN.fullmatch(patch_id):
                    raise MCPError("invalid_arguments", "参数 patchId 格式无效")
                args["patchId"] = patch_id
        return session_id, args

    @staticmethod
    def _resolve_item_selector(agent: Agent, name: str, args: dict[str, Any]) -> None:
        """Resolve and authorize item selectors against the bound session state."""
        has_index = "itemIndex" in args and args.get("itemIndex") is not None
        has_id = "itemId" in args and args.get("itemId") is not None
        if has_index and has_id:
            raise MCPError("invalid_arguments", "itemIndex 和 itemId 只能选择一个")
        items = agent.state.get("order", {}).get("items")
        is_multi = isinstance(items, list) and len(items) > 1
        item_scoped = name in {"recommend_processes", "estimate_price", "prepare_handoff",
                               "match_supplier_capability", "request_supplier_quote",
                               "preflight_file", "apply_order_patch"}
        if (has_index or has_id) and not item_scoped:
            raise MCPError("invalid_arguments", f"工具 {name} 不支持产品项选择器")
        if not is_multi and (has_index or has_id):
            raise MCPError("invalid_arguments", "当前会话不是多产品订单，不能使用产品项选择器")
        if is_multi and item_scoped and not (has_index or has_id):
            raise MCPError("item_required", "多产品订单必须指定 itemIndex 或 itemId")
        if not (has_index or has_id):
            return
        resolved_index: int | None = None
        if has_index:
            resolved_index = args["itemIndex"]
            if not isinstance(resolved_index, int) or resolved_index < 0 or resolved_index >= len(items):
                raise MCPError("invalid_arguments", "itemIndex 超出当前会话的产品项范围")
        else:
            item_id = args["itemId"]
            for index, item in enumerate(items):
                if isinstance(item, dict) and item.get("itemId") == item_id:
                    resolved_index = index
                    break
            if resolved_index is None:
                raise MCPError("invalid_arguments", "itemId 不属于当前会话")
        # Agent.call_tool currently accepts itemIndex; normalize the stable ID
        # selector to that internal representation only after authorization.
        args["itemIndex"] = resolved_index
        args.pop("itemId", None)

    @staticmethod
    def _public_response(response: dict[str, Any]) -> dict[str, Any]:
        """Project and bound an Agent response without planner configuration."""
        keys = ("sessionId", "order", "toolResult", "validation", "workflowStage",
                "confirmation", "decision", "runId", "runTrace", "fieldMeta",
                "conflicts", "messages", "nextAction", "rejectedFields", "planMeta",
                "revision", "acceptedFields")
        projected = {key: deepcopy(response[key]) for key in keys if key in response}
        return PrintOpsMCP._fit_response(projected)

    @staticmethod
    def _bounded(value: Any, depth: int = 0) -> Any:
        """Bound untrusted state before putting it on the MCP transport."""
        if depth >= 6:
            return "[truncated]"
        if isinstance(value, str):
            return value if len(value) <= 4096 else value[:4093] + "..."
        if isinstance(value, float):
            return value if math.isfinite(value) else "[invalid-number]"
        if isinstance(value, (int, bool)) or value is None:
            return value
        if isinstance(value, list):
            values = [PrintOpsMCP._bounded(item, depth + 1) for item in value[:64]]
            if len(value) > 64:
                values.append("[truncated]")
            return values
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 64:
                    result["[truncated]"] = True
                    break
                result[str(key)[:128]] = PrintOpsMCP._bounded(item, depth + 1)
            return result
        return str(value)[:4096]

    @staticmethod
    def _fit_response(projected: dict[str, Any]) -> dict[str, Any]:
        bounded = PrintOpsMCP._bounded(projected)
        try:
            size = len(json.dumps(bounded, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError, RecursionError):
            size = MAX_RESPONSE_BYTES + 1
        if size <= MAX_RESPONSE_BYTES:
            return bounded
        # Keep the tool result and state-machine decision useful while dropping
        # the largest optional fields (full order/history/provenance).
        compact = {key: bounded[key] for key in
                   ("sessionId", "toolResult", "workflowStage", "confirmation",
                    "decision", "runId", "nextAction", "rejectedFields", "planMeta")
                   if key in bounded}
        compact["truncated"] = True
        try:
            if len(json.dumps(compact, ensure_ascii=False, allow_nan=False).encode("utf-8")) <= MAX_RESPONSE_BYTES:
                return compact
        except (TypeError, ValueError, RecursionError):
            pass
        # Last-resort fixed-size envelope; this should only be reachable for a
        # corrupted or adversarially large tool result.
        return {"sessionId": bounded.get("sessionId"), "workflowStage": bounded.get("workflowStage"),
                "toolResult": "[truncated]", "truncated": True}

    @staticmethod
    def _patch_digest(agent: Agent, args: dict[str, Any], plan: dict[str, Any]) -> str:
        """Hash the normalized patch envelope used for idempotent retries."""
        item_index = args.get("itemIndex")
        item_id = None
        items = agent.state.get("order", {}).get("items")
        if (isinstance(item_index, int) and isinstance(items, list)
                and 0 <= item_index < len(items)
                and isinstance(items[item_index], dict)):
            item_id = items[item_index].get("itemId")
        value = {
            "patch": plan.get("patch") or {},
            "evidence": plan.get("evidence") or {},
            "confidence": plan.get("confidence") or {},
            "knowledgeVersion": plan.get("knowledgeVersion") or "",
            "reportedKnowledgeVersion": plan.get("reportedKnowledgeVersion") or "",
            "itemIndex": item_index,
            "itemId": item_id,
        }
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_patch_provenance(raw: dict[str, Any]) -> None:
        """Reject malformed provenance instead of silently writing a patch.

        The shared planner validator intentionally drops untrusted metadata
        that a model may have attached to an otherwise useful response. A
        state-writing MCP boundary is stricter: if a caller supplies
        confidence or evidence, their container and entries must be well
        formed. Unknown field names remain advisory and are filtered by
        validate_plan as before.
        """
        confidence = raw.get("confidence")
        if confidence is not None:
            if not isinstance(confidence, dict):
                raise MCPError("invalid_patch", "confidence 必须是对象或 null")
            if len(confidence) > 128:
                raise MCPError("invalid_patch", "confidence 字段数量超过限制")
            for field, value in confidence.items():
                if not isinstance(field, str) or not field.strip():
                    raise MCPError("invalid_patch", "confidence 字段名无效")
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise MCPError("invalid_patch", "confidence 必须是有限数字")
                try:
                    number = float(value)
                except (TypeError, ValueError, OverflowError):
                    raise MCPError("invalid_patch", "confidence 必须是有限数字")
                if not math.isfinite(number) or number < 0 or number > 1:
                    raise MCPError("invalid_patch", "confidence 必须位于 0 到 1")
        if "evidence" not in raw or raw.get("evidence") is None:
            return
        evidence = raw.get("evidence")
        if isinstance(evidence, list):
            if len(evidence) > 128:
                raise MCPError("invalid_patch", "evidence 条目数量超过限制")
            for entry in evidence:
                if not isinstance(entry, dict):
                    raise MCPError("invalid_patch", "evidence 条目必须是对象")
                field = entry.get("field")
                quote = entry.get("quote", entry.get("evidence"))
                if (not isinstance(field, str) or not field.strip()
                        or not isinstance(quote, str) or not quote.strip()):
                    raise MCPError("invalid_patch", "evidence 必须包含非空 field 和 quote")
                if ("source" in entry and entry["source"] is not None
                        and not isinstance(entry["source"], str)):
                    raise MCPError("invalid_patch", "evidence.source 必须是字符串")
            return
        if not isinstance(evidence, dict):
            raise MCPError("invalid_patch", "evidence 必须是数组、对象或 null")
        if len(evidence) > 128:
            raise MCPError("invalid_patch", "evidence 条目数量超过限制")
        for field, entry in evidence.items():
            if not isinstance(field, str) or not field.strip():
                raise MCPError("invalid_patch", "evidence 字段名无效")
            if isinstance(entry, str):
                if not entry.strip():
                    raise MCPError("invalid_patch", "evidence quote 不能为空")
                continue
            if not isinstance(entry, dict):
                raise MCPError("invalid_patch", "evidence 条目必须是字符串或对象")
            quote = entry.get("quote", entry.get("evidence"))
            if not isinstance(quote, str) or not quote.strip():
                raise MCPError("invalid_patch", "evidence 必须包含非空 quote")
            if ("source" in entry and entry["source"] is not None
                    and not isinstance(entry["source"], str)):
                raise MCPError("invalid_patch", "evidence.source 必须是字符串")

    @staticmethod
    def _patch_receipt(agent: Agent, patch_id: str | None,
                       digest: str) -> dict[str, Any] | None:
        """Find a recent successful bridge receipt by patch ID."""
        if not patch_id:
            return None
        history = agent.state.get("runHistory")
        if not isinstance(history, list):
            return None
        for record in reversed(history):
            if not isinstance(record, dict):
                continue
            for event in reversed(record.get("events") or []):
                if not isinstance(event, dict) or event.get("step") != "patch":
                    continue
                if event.get("patchId") != patch_id:
                    continue
                return {
                    "digest": event.get("patchDigest"),
                    "status": event.get("status") or "ok",
                    "acceptedFields": event.get("acceptedFields") or [],
                    "changedFields": event.get("changedFields") or [],
                    "rejectedFields": event.get("rejectedFields") or [],
                    "knowledgeVersion": event.get("knowledgeVersion") or KNOWLEDGE_VERSION,
                    "revision": event.get("revision"),
                }
        return None

    def _idempotent_patch_response(self, agent: Agent, session_id: str,
                                   receipt: dict[str, Any]) -> dict[str, Any]:
        """Return a bounded current-session response without applying again."""
        applied = receipt.get("status") == "ok" and bool(receipt.get("acceptedFields"))
        tool_result = {
            "status": "applied" if applied else "rejected",
            "idempotent": True,
            **({} if applied else {"reason": "no_valid_fields"}),
            "changedFields": receipt.get("changedFields") or [],
            "acceptedFields": receipt.get("acceptedFields") or [],
            "rejectedFields": receipt.get("rejectedFields") or [],
            "knowledgeVersion": KNOWLEDGE_VERSION,
            "revision": agent.state.get("revision", 0),
        }
        message = ("该 patch 已经应用，重试未重复写入。"
                   if applied else "该 patch 已被拒绝，重试未重复写入。")
        response = agent._result([message],
                                tool_result=tool_result)
        response["revision"] = agent.state.get("revision", 0)
        response["acceptedFields"] = tool_result["acceptedFields"]
        response["rejectedFields"] = tool_result["rejectedFields"]
        return self._public_response(response)

    def _call_tool(self, name: str, raw_args: Any) -> dict[str, Any]:
        session_id, args = self._validate_args(name, raw_args)
        with self._lock_for(session_id):
            agent = Agent(self.memory, session_id)
            self._resolve_item_selector(agent, name, args)
            if name == "apply_order_patch":
                # Normalize through the same planner contract used by the
                # optional LLM path. No tool call is accepted from this
                # write-only bridge.
                raw_plan = {
                    "patch": args.get("patch"),
                    "evidence": args.get("evidence"),
                    "confidence": args.get("confidence"),
                    "knowledgeVersion": args.get("knowledgeVersion"),
                    "reportedKnowledgeVersion": args.get("reportedKnowledgeVersion"),
                }
                self._validate_patch_provenance(raw_plan)
                plan = OpenAICompatiblePlanner.validate_plan(raw_plan, set())
                if plan is None:
                    raise MCPError("invalid_patch", "patch 未包含可应用的受限字段")
                normalized_patch = plan.get("patch") or {}
                quantity_keys = {"quantity", "quantityValue", "quantityUnit"}
                if quantity_keys.intersection(normalized_patch):
                    items = agent.state.get("order", {}).get("items")
                    selected_item = None
                    item_index = args.get("itemIndex")
                    if (isinstance(item_index, int) and isinstance(items, list)
                            and 0 <= item_index < len(items)
                            and isinstance(items[item_index], dict)):
                        selected_item = items[item_index]
                    base_order = selected_item or agent.state.get("order", {})
                    product = normalized_patch.get("productType") or base_order.get("productType", "")
                    raw_quantity = normalized_patch.get("quantity")
                    if raw_quantity in (None, ""):
                        raw_quantity = normalized_patch.get("quantityValue")
                    unit_hint = normalized_patch.get("quantityUnit") or ""
                    if parse_quantity(raw_quantity, product, unit_hint) is None:
                        raise MCPError("invalid_patch", "quantity patch 不是有效数量")
                accepted = Agent._bridge_patch_paths(normalized_patch)
                rejected = [str(value)[:256] for value in (plan.get("rejectedFields") or [])
                            if isinstance(value, str) and value.strip()]
                rejected = list(dict.fromkeys(rejected))
                if not accepted:
                    # A fully rejected patch is observable but has no state
                    # mutation and therefore does not consume a revision.
                    tool_result = {
                        "status": "rejected", "reason": "no_valid_fields",
                        "acceptedFields": [], "rejectedFields": rejected,
                        "knowledgeVersion": KNOWLEDGE_VERSION,
                        "revision": agent.state.get("revision", 0),
                    }
                    response = agent._result([], tool_result=tool_result)
                    response["revision"] = agent.state.get("revision", 0)
                    response["acceptedFields"] = []
                    response["rejectedFields"] = rejected
                    return self._public_response(response)
                patch_id = args.get("patchId")
                digest = self._patch_digest(agent, args, plan)
                receipt = self._patch_receipt(agent, patch_id, digest)
                if receipt is not None:
                    if receipt.get("digest") != digest:
                        raise MCPError(
                            "idempotency_conflict",
                            "patchId 已用于不同的 patch",
                            rpc_code=-32009,
                            data={"patchId": patch_id},
                        )
                    return self._idempotent_patch_response(agent, session_id, receipt)
                current_revision = agent.state.get("revision", 0)
                expected_revision = args.get("expectedRevision")
                if expected_revision != current_revision:
                    raise MCPError(
                        "revision_conflict",
                        "会话版本已变化，请重新读取后再提交 patch",
                        rpc_code=-32009,
                        data={"expectedRevision": expected_revision,
                              "revision": current_revision},
                    )
                agent._cas_expected_revision = expected_revision
                try:
                    response = agent.apply_order_patch(
                        plan,
                        item_index=args.get("itemIndex"),
                        expected_revision=expected_revision,
                        patch_id=patch_id,
                        patch_digest=digest,
                    )
                except RevisionConflictError as error:
                    # A second MCP process may have committed after the
                    # precheck. The SQLite CAS is authoritative.
                    raise MCPError(
                        "revision_conflict",
                        "会话版本已变化，请重新读取后再提交 patch",
                        rpc_code=-32009,
                        data={"expectedRevision": expected_revision,
                              "revision": error.current},
                    ) from error
                return self._public_response(response)
            # Keep the domain gateway authoritative for both successful calls
            # and readiness rejections.  Besides avoiding two subtly different
            # precondition paths, this records blocked MCP calls in the same
            # run/audit trace as other Agent operations.
            response = agent.call_tool(name, args, remember=False)
        return self._public_response(response)

    @staticmethod
    def _has_value(value: Any) -> bool:
        return value not in (None, "", {})

    @staticmethod
    def _same_value(left: Any, right: Any) -> bool:
        """Compare inherited values using the kernel's normalization rules."""
        try:
            return Agent._equivalent_value(left, right)
        except (AttributeError, TypeError, ValueError):
            return left == right

    @staticmethod
    def _is_low_confidence(meta: Any) -> bool:
        """Fail closed for malformed provenance rather than raising over MCP."""
        if not isinstance(meta, dict) or not PrintOpsMCP._has_value(meta.get("value")):
            return False
        try:
            confidence = float(meta.get("confidence", 1))
        except (TypeError, ValueError, OverflowError):
            return True
        # ``nan`` does not compare below the threshold; explicitly reject it.
        return not confidence >= 0.75

    @classmethod
    def _uncertain_fields_for_item(cls, agent: Agent,
                                   item_order: dict[str, Any] | None,
                                   item_index: int | None) -> list[str]:
        """Compatibility wrapper around the Agent's shared provenance check."""
        return agent._uncertain_fields_for_item(item_order, item_index)

    @staticmethod
    def _precondition(agent: Agent, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """Delegate the readiness gate to the domain Agent implementation."""
        return agent._tool_precondition(name, args.get("itemIndex"))

    @staticmethod
    def _resource_list() -> list[dict[str, Any]]:
        return [
            {"uri": "printops://products", "name": "products",
             "description": "PrintOps 品类目录和参数提示（版本化）",
             "mimeType": "application/json"},
            {"uri": f"printops://knowledge/{KNOWLEDGE_VERSION}",
             "name": "knowledge", "description": "当前印刷知识 manifest",
             "mimeType": "application/json"},
        ]

    @staticmethod
    def _read_resource(uri: Any) -> dict[str, Any]:
        if not isinstance(uri, str) or len(uri) > 256:
            raise MCPError("invalid_arguments", "资源 URI 无效")
        if uri == "printops://products":
            value = catalog_payload()
        elif uri == f"printops://knowledge/{KNOWLEDGE_VERSION}":
            value = deepcopy(KNOWLEDGE_MANIFEST)
        else:
            raise MCPError("resource_not_found", "资源不存在", rpc_code=-32602)
        return {"contents": [{"uri": uri, "mimeType": "application/json",
                               "text": json.dumps(value, ensure_ascii=False)}]}

    def _initialize(self, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise MCPError("invalid_arguments", "initialize 参数必须是 JSON 对象")
        requested = params.get("protocolVersion")
        if requested is not None and not isinstance(requested, str):
            raise MCPError("invalid_arguments", "protocolVersion 必须是字符串")
        protocol = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSION
        self.initialized = True
        return {"protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False},
                                  "resources": {"subscribe": False, "listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}

    @staticmethod
    def _result(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": _safe_request_id(request_id), "result": result}

    @staticmethod
    def _error(request_id: Any, error: MCPError) -> dict[str, Any]:
        data = {"code": error.code}
        if error.data:
            # Error details are advisory transport metadata; bound them before
            # echoing anything supplied by a client.
            data.update(PrintOpsMCP._bounded(error.data))
        return {"jsonrpc": "2.0", "id": _safe_request_id(request_id),
                "error": {"code": error.rpc_code, "message": error.message,
                           "data": data}}

    def handle(self, message: Any) -> dict[str, Any] | None:
        """Handle one parsed JSON-RPC message; notifications return ``None``."""
        is_notification = isinstance(message, dict) and "id" not in message
        request_id = None
        try:
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise MCPError("invalid_request", "请求必须是 JSON-RPC 2.0 对象", rpc_code=-32600)
            if "id" in message:
                request_id = _request_id(message.get("id"))
            # JSON-RPC notifications never receive a response, including
            # notifications for methods this synchronous adapter does not
            # implement.  Keep this before method/params validation so a
            # malformed notification cannot create an unsolicited reply.
            if is_notification:
                return None
            method = message.get("method")
            params = message.get("params", {})
            if not isinstance(method, str) or not method or len(method) > MAX_METHOD_LENGTH:
                raise MCPError("invalid_request", "method 必须是非空字符串且不超过长度限制", rpc_code=-32600)
            if method == "initialize":
                return self._result(request_id, self._initialize(params))
            if not self.initialized:
                raise MCPError("not_initialized", "请先调用 initialize", rpc_code=-32000)
            if not isinstance(params, dict):
                raise MCPError("invalid_arguments", "params 必须是 JSON 对象")
            if method == "ping":
                return self._result(request_id, {})
            if method == "tools/list":
                return self._result(request_id, {"tools": _tool_definitions(self.allowed_tools)})
            if method == "resources/list":
                return self._result(request_id, {"resources": self._resource_list()})
            if method == "resources/read":
                return self._result(request_id, self._read_resource(params.get("uri")))
            if method == "tools/call":
                name = params.get("name") if isinstance(params, dict) else None
                arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
                result = self._call_tool(name, arguments)
                return self._result(request_id, {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                    "structuredContent": result,
                    "isError": False,
                })
            raise MCPError("method_not_found", "方法不存在", rpc_code=-32601)
        except MCPError as error:
            return self._error(request_id, error)
        except Exception:
            # Never return stack traces or filesystem details over MCP.
            return self._error(request_id, MCPError("internal_error", "PrintOps MCP 内部错误", rpc_code=-32603))

    def serve(self, input_stream: Any = None, output_stream: Any = None) -> None:
        input_stream = input_stream or sys.stdin
        output_stream = output_stream or sys.stdout
        while True:
            line = input_stream.readline(MAX_MESSAGE_BYTES + 1)
            if not line:
                break
            if not line.strip():
                continue
            if len(line.encode("utf-8", errors="replace")) > MAX_MESSAGE_BYTES:
                # Drain the remainder of an overlong line so a following
                # request can still be processed on the same stdio stream.
                while "\n" not in line:
                    chunk = input_stream.readline(MAX_MESSAGE_BYTES + 1)
                    if not chunk:
                        break
                    line = chunk
                    if "\n" in chunk:
                        break
                response = self._error(None, MCPError("message_too_large", "请求超过大小限制", rpc_code=-32600))
                output_stream.write(json.dumps(response, ensure_ascii=False, allow_nan=False,
                                               separators=(",", ":")) + "\n")
                output_stream.flush()
                continue
            try:
                # Python accepts JavaScript-style non-finite constants by
                # default; MCP peers require strict JSON.
                message = json.loads(line, parse_constant=_reject_json_constant)
            except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
                response = self._error(None, MCPError("parse_error", "请求不是有效 JSON", rpc_code=-32700))
            else:
                response = self.handle(message)
            if response is not None:
                output_stream.write(json.dumps(response, ensure_ascii=False, allow_nan=False,
                                               separators=(",", ":")) + "\n")
                output_stream.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PrintOps stdio MCP server")
    parser.add_argument("--memory-path", default=None, help="SQLite session database path")
    parser.add_argument("--session-id", default=None, help="Bind this process to one session")
    parser.add_argument("--allow-any-session", action="store_true",
                        help="Allow any valid session ID (development/testing only)")
    parser.add_argument("--capabilities", default=None, help="Comma-separated levels: L0 or L0,L1")
    args = parser.parse_args(argv)
    if not args.session_id and not os.getenv("PRINTOPS_MCP_SESSION_ID") and not args.allow_any_session:
        print("PrintOps MCP requires --session-id or PRINTOPS_MCP_SESSION_ID; "
              "use --allow-any-session only for development", file=sys.stderr)
        return 2
    try:
        server = PrintOpsMCP(memory_path=args.memory_path, bound_session_id=args.session_id,
                             allow_any_session=args.allow_any_session,
                             capabilities=args.capabilities)
    except (OSError, ValueError, MCPError) as error:
        print(f"PrintOps MCP startup failed: {error}", file=sys.stderr)
        return 2
    server.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
