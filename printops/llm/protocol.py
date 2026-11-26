"""Native tool_calls and JSON-envelope protocol parsers plus prompt version anchor."""

from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any

# Prompt contract anchor (TD-08). Bump when modifying planner system prompts.
PLANNER_PROMPT_VERSION = "1"


class ProtocolMixin:
    """Dual-protocol (native tool_calls + JSON envelope) response parsing."""

    @staticmethod
    def _response_message(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {}
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return {}
        message = choices[0].get("message")
        if not isinstance(message, dict):
            # Streaming-compatible proxies sometimes return the same shape
            # under ``delta`` even when the client did not request a stream.
            message = choices[0].get("delta")
        return message if isinstance(message, dict) else {}

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any] | None:
        """Coerce provider function arguments to a JSON object.

        OpenAI-compatible gateways disagree on whether ``arguments`` is a
        JSON string or an already decoded mapping.  Keeping this conversion
        in one place means the Agent never has to trust provider-specific
        shapes.
        """
        if isinstance(value, str):
            if not value.strip():
                return {}
            try:
                value = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
        return deepcopy(value) if isinstance(value, dict) else None

    @staticmethod
    def _strict_json_object(content: str) -> dict[str, Any] | None:
        """Decode an explicitly requested JSON object without text fallback."""
        text = (content or "").strip()
        if text.startswith("```"):
            text = text[3:]
            if text.lower().startswith("json"):
                text = text[4:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        try:
            value = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @classmethod
    def _normalize_native_call(cls, raw_call: Any) -> dict[str, Any] | None:
        """Normalize Chat Completions, legacy, and Responses function calls."""
        if not isinstance(raw_call, dict):
            return None
        function = raw_call.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            arguments = function.get("arguments", {})
        else:
            # Responses API uses direct ``name``/``arguments`` fields on an
            # output item; a few legacy gateways do the same for
            # ``function_call``.
            name = raw_call.get("name")
            arguments = raw_call.get("arguments", {})
        if not isinstance(name, str) or not name.strip():
            return None
        normalized_arguments = cls._json_object(arguments)
        if normalized_arguments is None:
            return None
        call_id = raw_call.get("id") or raw_call.get("call_id") or raw_call.get("callId")
        result: dict[str, Any] = {
            "id": str(call_id)[:256] if isinstance(call_id, str) and call_id else "",
            "type": "function",
            "function": {
                "name": name.strip()[:128],
                "arguments": normalized_arguments,
            },
        }
        # Preserve a small provider index when present.  It is useful for
        # correlating batched calls but is never trusted as an order field.
        index = raw_call.get("index")
        if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
            result["index"] = index
        return result

    @classmethod
    def _native_calls(cls, result: Any) -> tuple[list[dict[str, Any]], str]:
        """Extract all bounded native calls and identify their wire shape.

        Besides modern Chat Completions ``tool_calls``, this accepts the
        legacy singleton ``function_call`` and Responses API ``output``
        function-call items.  Calls are de-duplicated because some proxies
        mirror a Responses item into both a top-level and a message field.
        """
        if not isinstance(result, dict):
            return [], ""
        message = cls._response_message(result)
        candidates: list[tuple[Any, str]] = []
        raw_calls = message.get("tool_calls") if isinstance(message, dict) else None
        if isinstance(raw_calls, list):
            candidates.extend((item, "native_tools") for item in raw_calls)
        if isinstance(message.get("function_call"), dict):
            candidates.append((message["function_call"], "legacy_function_call"))

        # Some OpenAI-compatible servers put function-call items in a content
        # array even though they otherwise follow the Chat Completions shape.
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "")
                if item_type in {"function_call", "tool_call"}:
                    candidates.append((item, "responses_function_call"))

        output = result.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "")
                if item_type in {"function_call", "tool_call"}:
                    candidates.append((item, "responses_function_call"))
        if isinstance(result.get("function_call"), dict):
            candidates.append((result["function_call"], "legacy_function_call"))

        calls: list[dict[str, Any]] = []
        seen: set[str] = set()
        modes: list[str] = []
        for raw_call, mode in candidates:
            call = cls._normalize_native_call(raw_call)
            if call is None:
                continue
            function = call["function"]
            try:
                identity = json.dumps(
                    [call.get("id", ""), function.get("name", ""), function.get("arguments", {})],
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                )
            except (TypeError, ValueError, OverflowError):
                identity = f"{call.get('id', '')}:{function.get('name', '')}:{len(calls)}"
            if identity in seen:
                continue
            seen.add(identity)
            calls.append(call)
            modes.append(mode)
            if len(calls) >= cls.MAX_CONTEXT_LEDGER_ENTRIES:
                break
        if not calls:
            return [], ""
        if "native_tools" in modes:
            return calls, "native_tools"
        return calls, modes[0]

    @classmethod
    def _has_native_call_marker(cls, result: Any) -> bool:
        """Whether a response claims to contain a native function call."""
        if not isinstance(result, dict):
            return False
        message = cls._response_message(result)
        if isinstance(message, dict) and (
                bool(message.get("tool_calls"))
                or isinstance(message.get("function_call"), dict)):
            return True
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and any(
                isinstance(item, dict) and item.get("type") in {"function_call", "tool_call"}
                for item in content):
            return True
        output = result.get("output")
        return isinstance(output, list) and any(
            isinstance(item, dict) and item.get("type") in {"function_call", "tool_call"}
            for item in output
        )

    @classmethod
    def _response_content(cls, result: Any) -> Any:
        """Return text/content from both Chat Completions and Responses."""
        if not isinstance(result, dict):
            return ""
        message = cls._response_message(result)
        if message:
            if "content" in message:
                content = message.get("content")
                if isinstance(content, list):
                    return "".join(
                        str(part.get("text") or part.get("value") or "")
                        for part in content
                        if isinstance(part, dict)
                        and part.get("type", "text") in {"text", "output_text"}
                    )
                return content
            # Legacy gateways occasionally expose text directly on message.
            if "text" in message:
                return message.get("text")
        if "output_text" in result:
            return result.get("output_text")
        output = result.get("output")
        if isinstance(output, list):
            parts: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type in {"message", "output_text", "text"}:
                    content = item.get("content", item.get("text", ""))
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type", "text") in {"text", "output_text"}:
                                parts.append(str(part.get("text") or part.get("value") or ""))
                    elif content:
                        parts.append(str(content))
            return "".join(parts)
        return ""

    @classmethod
    def _content_plan(cls, content: Any) -> dict[str, Any] | None:
        """Parse a JSON plan envelope embedded in native message content."""
        if isinstance(content, (dict, list)):
            try:
                content = json.dumps(content, ensure_ascii=False)
            except (TypeError, ValueError, OverflowError):
                return None
        if not isinstance(content, str):
            return None
        text = content.strip()
        if not text:
            return None
        return cls._parse_plan(text)

    @classmethod
    def _native_tool_plan(cls, result: Any) -> dict[str, Any] | None:
        """Convert any supported native function-call response to our plan.

        A native call remains authoritative for tool selection.  If the model
        also puts a normal PrintOps JSON envelope in ``content``, its patch and
        metadata are merged while the envelope's ``tool`` value is ignored.
        This lets models extract order fields and request a tool in one turn.
        """
        calls, protocol = cls._native_calls(result)
        if not calls:
            return None
        content = cls._response_content(result)
        content_plan = cls._content_plan(content)
        plan: dict[str, Any] = deepcopy(content_plan) if isinstance(content_plan, dict) else {
            "reply": str(content).strip() if isinstance(content, str) else "",
            "patch": {},
        }
        # Native calls are the sole authority for the requested tool.  This
        # prevents a content envelope from smuggling a second, conflicting
        # tool while still retaining its patch/evidence metadata.
        first = calls[0]
        plan["tool"] = {
            "name": first["function"]["name"],
            "arguments": deepcopy(first["function"]["arguments"]),
        }
        plan["_nativeToolCall"] = deepcopy(first)
        plan["_nativeToolCalls"] = deepcopy(calls)
        plan["_nativeProtocol"] = protocol
        return plan

    @classmethod
    def _response_text(cls, result: Any) -> str:
        """Read text from common Chat Completions and Responses-compatible shapes."""
        content = cls._response_content(result)
        if isinstance(content, list):
            return "".join(
                str(part.get("text", part.get("value", ""))) for part in content
                if isinstance(part, dict) and part.get("type", "text") in {"text", "output_text"}
            )
        if isinstance(content, dict):
            try:
                return json.dumps(content, ensure_ascii=False)
            except (TypeError, ValueError, OverflowError):
                return ""
        if content not in (None, ""):
            return str(content)
        if isinstance(result, dict):
            choices = result.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                return str(choices[0].get("text") or "")
        return ""

    @staticmethod
    def _parse_plan(content: str) -> dict[str, Any] | None:
        text = (content or "").strip()
        if not text:
            return None
        # Models occasionally ignore the no-fence instruction; remove only the
        # surrounding fence and then accept the first valid JSON object.
        if text.startswith("```"):
            text = text[3:]
            if text.startswith("json"):
                text = text[4:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        try:
            value = json.loads(text)
            if isinstance(value, dict):
                return value
        except (json.JSONDecodeError, RecursionError):
            pass
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(text[index:])
            except (json.JSONDecodeError, RecursionError):
                continue
            if isinstance(value, dict):
                return value
        # A plain-language answer is still useful. It should not discard the
        # model entirely just because it did not follow the JSON instruction.
        return {"reply": text, "patch": {}, "tool": None}
