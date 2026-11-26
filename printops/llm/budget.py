"""Request context clipping, bounded tool ledger, and payload byte-budget fitting."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any


class BudgetMixin:
    """Context compression and byte-budget management for planner requests."""

    @staticmethod
    def _provider_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Translate the internal tool catalog to OpenAI function schemas.

        The Agent remains the authority for session state, so the internal
        ``order`` argument is deliberately removed from model-facing schemas.
        The planner may request a tool and small selectors such as ``itemIndex``
        or ``platformId``; the gateway supplies the current order itself.
        """
        provider_tools: list[dict[str, Any]] = []
        for item in tools:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            schema = item.get("input")
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            schema = json.loads(json.dumps(schema, ensure_ascii=False))
            properties = schema.get("properties")
            if not isinstance(properties, dict):
                properties = {}
                schema["properties"] = properties
            properties.pop("order", None)
            required = schema.get("required")
            if isinstance(required, list):
                schema["required"] = [key for key in required if key != "order"]
            provider_tools.append({
                "type": "function",
                "function": {
                    "name": name.strip(),
                    "description": str(item.get("description") or "").strip(),
                    "parameters": schema,
                },
            })
        return provider_tools

    @classmethod
    def _compact_tool_catalog(cls, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep a small JSON fallback catalog without duplicating output schemas."""
        catalog: list[dict[str, Any]] = []
        for item in cls._provider_tools(tools):
            function = item["function"]
            parameters = function.get("parameters", {"type": "object", "properties": {}})
            if isinstance(parameters, dict):
                compact_parameters: dict[str, Any] = {
                    key: parameters[key] for key in ("type", "required") if key in parameters
                }
                properties = parameters.get("properties")
                if isinstance(properties, dict):
                    compact_properties: dict[str, Any] = {}
                    for name, schema in properties.items():
                        if not isinstance(schema, dict):
                            continue
                        compact_schema = {
                            key: schema[key] for key in ("type", "description", "enum", "items")
                            if key in schema
                        }
                        compact_properties[name] = compact_schema
                    compact_parameters["properties"] = compact_properties
                parameters = compact_parameters
            catalog.append({
                "name": function["name"],
                "description": function.get("description", "")[:cls.MAX_TOOL_DESCRIPTION],
                "input": parameters,
            })
        return catalog

    @staticmethod
    def _clip_context_text(value: str, limit: int) -> str:
        """Keep both the beginning and end of model context when clipping text."""
        text = str(value)
        if len(text) <= limit:
            return text
        if limit < 32:
            return text[:limit]
        marker = "...[已截断]..."
        side = (limit - len(marker)) // 2
        return f"{text[:side]}{marker}{text[-side:]}"

    @staticmethod
    def _clip_context_text_bytes(value: str, limit: int) -> str:
        """Clip UTF-8 text by bytes so the transport budget is real."""
        text = str(value)
        raw = text.encode("utf-8")
        if len(raw) <= limit:
            return text
        marker = "...[上下文已截断]..."
        marker_bytes = len(marker.encode("utf-8"))
        side_bytes = max(1, (limit - marker_bytes) // 2)
        head = raw[:side_bytes].decode("utf-8", "ignore")
        tail = raw[-side_bytes:].decode("utf-8", "ignore")
        return f"{head}{marker}{tail}"

    @classmethod
    def _compact_context_value(cls, value: Any, depth: int = 0) -> Any:
        """Bound nested provider context without changing the local tool result.

        Tool output is trusted local data, but it can contain a large order,
        evidence, or repeated option arrays. The model only needs a bounded
        view; the complete result remains in the Agent response and trace.
        """
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return cls._clip_context_text(value, cls.MAX_CONTEXT_MESSAGE_CHARS)
        if depth >= cls.MAX_CONTEXT_VALUE_DEPTH:
            return "[上下文层级已省略]"
        if isinstance(value, dict):
            compact: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= cls.MAX_CONTEXT_OBJECT_KEYS:
                    compact["_truncatedKeys"] = len(value) - index
                    break
                safe_key = cls._clip_context_text(str(key), 128)
                compact[safe_key] = cls._compact_context_value(item, depth + 1)
            return compact
        if isinstance(value, (list, tuple)):
            compact_list = [cls._compact_context_value(item, depth + 1)
                            for item in value[:cls.MAX_CONTEXT_LIST_ITEMS]]
            if len(value) > cls.MAX_CONTEXT_LIST_ITEMS:
                compact_list.append({"_truncatedItems": len(value) - cls.MAX_CONTEXT_LIST_ITEMS})
            return compact_list
        return cls._clip_context_text(str(value), cls.MAX_CONTEXT_MESSAGE_CHARS)

    @classmethod
    def _bounded_context_object(cls, value: Any, max_bytes: int) -> Any:
        """Return a JSON-safe context object within a byte budget."""
        compact = cls._compact_context_value(value)
        try:
            encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError, OverflowError, RecursionError):
            return {"_contextError": "工具结果无法编码"}
        if len(encoded.encode("utf-8")) <= max_bytes:
            return compact
        # The common oversized case is a list of options/evidence. Keep the
        # shape and a deterministic prefix so the provider can still summarize.
        if isinstance(compact, dict):
            reduced = dict(compact)
            for key in ("options", "evidence", "items", "toolTrace", "events", "history"):
                if isinstance(reduced.get(key), list) and len(reduced[key]) > 3:
                    reduced[key] = reduced[key][:3] + [{"_truncatedItems": "更多内容已保留在本地结果"}]
            try:
                encoded = json.dumps(reduced, ensure_ascii=False, separators=(",", ":"))
                if len(encoded.encode("utf-8")) <= max_bytes:
                    return reduced
            except (TypeError, ValueError, OverflowError, RecursionError):
                pass
        summary_limit = max(64, max_bytes - 96)
        for _ in range(4):
            result = {"_contextTruncated": True,
                      "summary": cls._clip_context_text_bytes(encoded, summary_limit)}
            try:
                if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= max_bytes:
                    return result
            except (TypeError, ValueError, OverflowError, RecursionError):
                return {"_contextError": "工具结果无法编码"}
            summary_limit = max(32, int(summary_limit * 0.75))
        return {"_contextTruncated": True, "summary": "[上下文超出传输预算]"}

    @classmethod
    def _normalize_tool_ledger(cls, ledger: Any = None,
                               tool_result: dict[str, Any] | None = None,
                               tool_call: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Normalize Agent receipts into a bounded provider-neutral ledger.

        The Agent has intentionally kept this argument loose so older custom
        planners continue to work.  Accept the common spellings used by
        callers (``name``, ``tool``, ``toolName`` and ``result``/
        ``toolResult``) and retain only context-safe metadata.
        """
        raw_entries: list[Any] = []
        if isinstance(ledger, dict):
            candidate = ledger.get("entries", ledger.get("toolResults", ledger.get("ledger")))
            raw_entries = candidate if isinstance(candidate, list) else [ledger]
        elif isinstance(ledger, (list, tuple)):
            raw_entries = list(ledger)

        def make_entry(raw: Any, fallback_call: Any = None,
                       fallback_result: Any = None) -> dict[str, Any] | None:
            if not isinstance(raw, dict):
                raw = {}
            call_raw = (raw.get("call") or raw.get("toolCall") or raw.get("_nativeToolCall")
                        or raw.get("nativeCall") or fallback_call)
            call = cls._normalize_native_call(call_raw) if isinstance(call_raw, dict) else None
            function = call.get("function", {}) if isinstance(call, dict) else {}
            name = function.get("name") if isinstance(function, dict) else None
            if not isinstance(name, str) or not name:
                for key in ("name", "tool", "toolName"):
                    candidate = raw.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        name = candidate.strip()[:128]
                        break
            if not isinstance(name, str) or not name:
                return None
            arguments = function.get("arguments", {}) if isinstance(function, dict) else {}
            if not isinstance(arguments, dict):
                arguments = raw.get("arguments", {})
            arguments = cls._json_object(arguments) or {}
            if call is None:
                call_id = raw.get("id") or raw.get("callId") or raw.get("call_id")
                call = {
                    "id": str(call_id)[:256] if isinstance(call_id, str) and call_id else "",
                    "type": "function",
                    "function": {"name": name, "arguments": deepcopy(arguments)},
                }
            result = raw.get("result", raw.get("toolResult", raw.get("response", fallback_result)))
            entry: dict[str, Any] = {
                "id": str(call.get("id") or "")[:256],
                "name": name[:128],
                "arguments": deepcopy(arguments),
                "result": deepcopy(result),
                "call": deepcopy(call),
            }
            # These fields are useful for stale-result and audit reasoning,
            # but are kept scalar and bounded to avoid replaying raw traces.
            for key in ("round", "itemIndex", "stale", "inputFingerprint", "signature", "revision", "status"):
                value = raw.get(key)
                if isinstance(value, bool) or isinstance(value, (int, float, str)):
                    if isinstance(value, str):
                        entry[key] = value[:256]
                    else:
                        entry[key] = value
            return entry

        entries: list[dict[str, Any]] = []
        for raw in raw_entries:
            entry = make_entry(raw)
            if entry is not None:
                entries.append(entry)

        # ``tool_result``/``tool_call`` are the pre-ledger API and must still
        # be represented exactly once when a caller has not yet migrated.
        if tool_result is not None or tool_call is not None:
            fallback_entry = make_entry(tool_result or {}, tool_call, tool_result)
            if fallback_entry is not None:
                identity = cls._ledger_identity(fallback_entry)
                if not any(cls._ledger_identity(item) == identity for item in entries):
                    entries.append(fallback_entry)

        # Keep the newest entries.  Deduplicate by call id/signature while
        # preserving order, which also handles a repeated compatibility call.
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in entries:
            identity = cls._ledger_identity(entry)
            if identity in seen:
                continue
            seen.add(identity)
            deduped.append(entry)
        return deduped[-cls.MAX_CONTEXT_LEDGER_ENTRIES:]

    @staticmethod
    def _ledger_identity(entry: dict[str, Any]) -> str:
        call_id = entry.get("id")
        if call_id:
            return f"id:{call_id}"
        signature = entry.get("signature")
        if isinstance(signature, str) and signature:
            return f"signature:{signature}"
        try:
            arguments = json.dumps(entry.get("arguments", {}), ensure_ascii=False,
                                   sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError, OverflowError):
            arguments = "?"
        return (f"sig:{entry.get('name', '')}:{arguments}:"
                f"{entry.get('inputFingerprint', '')}:{entry.get('round', '')}")

    @classmethod
    def _ledger_context(cls, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return a bounded, serializable view of receipts for a request."""
        result: list[dict[str, Any]] = []
        for entry in entries:
            public: dict[str, Any] = {
                "id": entry.get("id", ""),
                "name": entry.get("name", ""),
                "arguments": cls._bounded_context_object(entry.get("arguments", {}), 2048),
                "result": cls._bounded_context_object(entry.get("result"), cls.MAX_CONTEXT_TOOL_RESULT_BYTES),
            }
            for key in ("round", "itemIndex", "stale", "inputFingerprint", "signature", "revision", "status"):
                if key in entry:
                    public[key] = entry[key]
            result.append(public)
        encoded = cls._json_bytes(result)
        if encoded <= cls.MAX_CONTEXT_LEDGER_BYTES:
            return result
        # Shrink individual results before dropping old receipts.  Keeping the
        # name/id of every recent call is more useful than one giant result.
        reduced = deepcopy(result)
        for item in reduced:
            item["result"] = cls._bounded_context_object(item.get("result"), 2048)
        while len(reduced) > 1 and cls._json_bytes(reduced) > cls.MAX_CONTEXT_LEDGER_BYTES:
            reduced.pop(0)
        if cls._json_bytes(reduced) > cls.MAX_CONTEXT_LEDGER_BYTES:
            for item in reduced:
                item["result"] = {"_contextTruncated": True, "summary": "[工具结果超出传输预算]"}
        return reduced

    @staticmethod
    def _json_bytes(value: Any) -> int:
        try:
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError, OverflowError, RecursionError):
            return 1 << 60

    @classmethod
    def _native_ledger_messages(cls, messages: list[dict[str, Any]],
                                entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Append valid assistant/tool message pairs for a native request."""
        result = deepcopy(messages)
        # Receipts from one native response share a round number.  Replaying
        # that response as one assistant message with several tool calls is
        # closer to the OpenAI contract than interleaving separate assistant
        # messages, while receipts from different rounds remain separate.
        groups: list[list[tuple[int, dict[str, Any]]]] = []
        group_keys: list[Any] = []
        for index, entry in enumerate(entries):
            key = entry.get("round") if "round" in entry else ("single", index)
            if group_keys and group_keys[-1] == key:
                groups[-1].append((index, entry))
            else:
                group_keys.append(key)
                groups.append([(index, entry)])
        for group in groups:
            wire_calls: list[dict[str, Any]] = []
            tool_messages: list[tuple[str, Any]] = []
            for index, entry in group:
                call = entry.get("call") if isinstance(entry.get("call"), dict) else None
                if call is None:
                    call = {
                        "id": entry.get("id") or f"call_printops_{index + 1}",
                        "type": "function",
                        "function": {"name": entry.get("name", ""),
                                      "arguments": entry.get("arguments", {})},
                    }
                call_id = str(call.get("id") or entry.get("id") or f"call_printops_{index + 1}")[:256]
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                call_name = str(function.get("name") or entry.get("name") or "")[:128]
                arguments = function.get("arguments", entry.get("arguments", {}))
                if not isinstance(arguments, dict):
                    arguments = {}
                wire_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {"name": call_name,
                                  "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))},
                })
                tool_result = entry.get("result")
                if (isinstance(tool_result, dict) and "result" in tool_result
                        and any(key in tool_result for key in ("name", "tool", "toolName"))):
                    # Agent compatibility envelope: send the actual tool result to
                    # the provider, while keeping name/id in the assistant call.
                    tool_result = tool_result.get("result")
                tool_messages.append((call_id, tool_result))
            result.append({"role": "assistant", "content": None, "tool_calls": wire_calls})
            for call_id, tool_result in tool_messages:
                bounded = cls._bounded_context_object(tool_result, cls.MAX_CONTEXT_TOOL_RESULT_BYTES)
                result.append({"role": "tool", "tool_call_id": call_id,
                               "content": json.dumps(bounded, ensure_ascii=False)})
        return result

    @classmethod
    def _fit_request_budget(cls, payload: dict[str, Any], max_bytes: int | None = None) -> dict[str, Any]:
        """Bound a fully assembled request while preserving current user text."""
        budget = max_bytes if isinstance(max_bytes, int) and max_bytes > 0 else cls.MAX_CONTEXT_REQUEST_BYTES
        if cls._json_bytes(payload) <= budget:
            return payload
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []

        def mark_user_envelope() -> None:
            for item in reversed(messages):
                if not isinstance(item, dict) or item.get("role") != "user":
                    continue
                content = item.get("content")
                if not isinstance(content, str):
                    return
                try:
                    body = json.loads(content)
                except (TypeError, ValueError, RecursionError, json.JSONDecodeError):
                    return
                if isinstance(body, dict):
                    body["_contextTruncated"] = True
                    item["content"] = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
                return

        # Keep transport metadata inside the documented user envelope.  Strict
        # OpenAI-compatible gateways reject unknown top-level request keys.
        mark_user_envelope()
        # Remove oldest conversational history first.  Current user and
        # protocol ledger messages are always retained.  Do not assume the
        # current user is at a fixed index: native follow-ups append
        # assistant/tool pairs after it.
        while cls._json_bytes(payload) > budget:
            user_indexes = [index for index, item in enumerate(messages)
                            if isinstance(item, dict) and item.get("role") == "user"]
            current_index = user_indexes[-1] if user_indexes else len(messages)
            history_indexes = [index for index in range(1, current_index)]
            if not history_indexes:
                break
            del messages[history_indexes[0]]
        if cls._json_bytes(payload) <= budget:
            return payload
        user_messages = [item for item in messages if isinstance(item, dict) and item.get("role") == "user"]
        if user_messages:
            user = user_messages[-1]
            try:
                body = json.loads(user.get("content", "{}")) if isinstance(user.get("content"), str) else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                body = {}
            if isinstance(body, dict):
                body["order"] = cls._bounded_context_object(body.get("order", {}), 2048)
                if isinstance(body.get("toolResults"), list):
                    body["toolResults"] = cls._ledger_context(
                        [{"id": item.get("id", ""), "name": item.get("name", ""),
                          "arguments": item.get("arguments", {}), "result": item.get("result")}
                         for item in body["toolResults"] if isinstance(item, dict)])
                if "toolResult" in body:
                    body["toolResult"] = cls._bounded_context_object(body["toolResult"], 2048)
                body["text"] = cls._clip_context_text_bytes(str(body.get("text", "")), 1024)
                body["_contextTruncated"] = True
                user["content"] = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        if cls._json_bytes(payload) <= budget:
            return payload
        # Native ledger tool messages can dominate the request even after
        # each receipt passed its individual bound. Reduce observations and
        # then discard the oldest complete assistant/tool groups while keeping
        # the current user turn intact.
        for item in messages:
            if not isinstance(item, dict) or item.get("role") != "tool":
                continue
            content = item.get("content")
            if isinstance(content, str):
                try:
                    decoded = json.loads(content)
                except (TypeError, ValueError, RecursionError, json.JSONDecodeError):
                    decoded = content
                bounded = cls._bounded_context_object(decoded, 2048)
                item["content"] = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))
        while cls._json_bytes(payload) > budget:
            group_start = next((index for index, item in enumerate(messages)
                                if isinstance(item, dict) and item.get("role") == "assistant"
                                and isinstance(item.get("tool_calls"), list)), None)
            if group_start is None:
                break
            group_end = group_start + 1
            while group_end < len(messages) and isinstance(messages[group_end], dict) \
                    and messages[group_end].get("role") == "tool":
                group_end += 1
            del messages[group_start:group_end]
        if cls._json_bytes(payload) <= budget:
            return payload
        # Native schemas can be sizeable.  Retain every function name and a
        # minimal valid object schema as the final bounded representation.
        protocol_tools = payload.get("tools")
        if isinstance(protocol_tools, list):
            minimal: list[dict[str, Any]] = []
            for item in protocol_tools:
                if not isinstance(item, dict):
                    continue
                function = item.get("function") if isinstance(item.get("function"), dict) else {}
                name = function.get("name")
                if isinstance(name, str) and name:
                    minimal.append({"type": "function", "function": {
                        "name": name[:128], "description": "",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    }})
            payload["tools"] = minimal
        if cls._json_bytes(payload) <= budget:
            return payload
        # Keep a deterministic marker in the user envelope even for an
        # unusually tiny configured budget; callers can inspect it and decide
        # whether to retry with a larger context window.
        if user_messages:
            user_messages[-1]["content"] = json.dumps(
                {"text": "", "order": {}, "tools": [], "_contextTruncated": True},
                ensure_ascii=False, separators=(",", ":"),
            )
        payload.pop("tools", None)
        payload.pop("tool_choice", None)
        # A caller may deliberately set a very small budget (for example a
        # smoke test or a gateway with a tiny context window).  Trim the
        # system instruction as a final measure so the advertised budget is
        # still meaningful; the normal default never reaches this branch.
        system_messages = [item for item in messages if isinstance(item, dict)
                           and item.get("role") == "system"]
        if system_messages:
            system_messages[0]["content"] = cls._clip_context_text_bytes(
                str(system_messages[0].get("content", "")), max(0, budget // 3))
        if cls._json_bytes(payload) > budget:
            # Keep only a minimal valid message envelope if even the clipped
            # system prompt plus current user exceeds the requested budget.
            minimal_messages: list[dict[str, Any]] = []
            if budget >= 64:
                minimal_messages.append({"role": "system", "content": "PrintOps JSON"})
            minimal_messages.append({"role": "user", "content": '{"_contextTruncated":true}'})
            payload["messages"] = minimal_messages
        # Do not claim the cap was honored when the configured value is below
        # the irreducible JSON envelope (model name + marker).  For all sane
        # budgets this is guaranteed by the branch above.
        return payload
