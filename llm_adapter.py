"""Optional OpenAI-compatible planner.

The deterministic Agent remains the fallback when no model is configured or
when a provider is temporarily unavailable.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import socket
import ssl
import stat
import subprocess
import tempfile
import time
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as futures_wait
from urllib.error import HTTPError, URLError
from copy import copy, deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from order_model import DIMENSION_DEFAULTS
from product_knowledge import known_product_spec_keys
from request_runtime import (DeadlineExceeded, bounded_io, deadline_sleep,
                             operation_deadline, remaining)


def _reject_private_host(hostname: str | None) -> None:
    """SSRF guard: reject hosts that resolve to loopback/private/reserved space.

    Literal IPs are checked directly; resolvable hostnames are resolved once so
    an innocent-looking name pointing at internal space is rejected too. A name
    that cannot resolve at all is allowed through — it cannot reach an internal
    target, and the request itself will fail with a connection error.
    """
    name = (hostname or "").strip().lower().rstrip(".")
    if not name:
        raise ValueError("接口 URL 缺少主机名")
    if name == "localhost" or name.endswith(".localhost") or name.endswith(".local"):
        raise ValueError("接口 URL 不能指向本机或内网地址")
    try:
        addresses = [ipaddress.ip_address(name)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(name, None)
        except (OSError, UnicodeError):
            return
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    for ip in addresses:
        cgnat = ipaddress.ip_network("100.64.0.0/10") if ip.version == 4 else None
        if ip.is_loopback or ip.is_private or ip.is_reserved or ip.is_link_local \
                or ip.is_multicast or ip.is_unspecified or (cgnat and ip in cgnat):
            raise ValueError("接口 URL 不能指向本机或内网地址")


def normalize_base_url(value: str, allow_private_hosts: bool = False) -> str:
    """Validate an OpenAI-compatible HTTP endpoint without exposing credentials.

    Private/loopback targets remain blocked by default because this URL is
    user-controlled and the server makes the outbound request. Trusted local
    deployments can explicitly opt in for a company-internal model endpoint.
    """
    value = (value or "").strip().rstrip("/")
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("接口 URL 必须以 http:// 或 https:// 开头")
    if not parsed.hostname:
        raise ValueError("接口 URL 缺少主机名")
    if parsed.username or parsed.password:
        raise ValueError("接口 URL 不应包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("接口 URL 不应包含查询参数或片段")
    if not allow_private_hosts:
        _reject_private_host(parsed.hostname)
    return value


def _close_error_response(error: HTTPError) -> None:
    """Drop the unread response body an HTTPError keeps attached.

    urllib raises HTTPError before the body is read, so the connection stays
    open until the exception - and the traceback that references it - is
    collected. A capability probe fans out concurrently, and every provider
    rejection (401, 404, 405) would otherwise park a live socket for the
    lifetime of the traceback. Closing is best effort: this is cleanup on an
    error path and must never mask the original diagnosis.
    """
    try:
        error.close()
    except Exception:
        pass


def read_saved_config(path: str | Path) -> dict[str, str]:
    config_path = Path(path)
    if not config_path.is_file():
        return {}
    if os.name != "nt":
        # dsh credentials-local fails closed on world-readable secrets.
        # Self-heal the mode first (a hand-edited file may carry the
        # creator's umask); only refuse to load when it stays exposed.
        try:
            if config_path.stat().st_mode & 0o077:
                os.chmod(config_path, 0o600)
                if config_path.stat().st_mode & 0o077:
                    return {}
        except OSError:
            return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: str(data.get(key, "")) for key in ("url", "model", "key") if data.get(key) is not None}


def _restrict_key_file(path: Path) -> None:
    """Best-effort owner-only permissions for the plaintext key file.

    mkstemp already creates 0600 on POSIX; Windows chmod is a no-op, so drop
    inherited ACL entries and grant the current user Full Control via icacls.
    Full Control matters: the grant also carries DELETE, and ``os.replace``
    cannot rename a file whose ACL allows only read/write (WinError 5).  The
    file stays private to this account, but remains replaceable by it.
    Failures are never fatal: the inherited user ACL still works.
    """
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return
    for account in _key_file_accounts():
        try:
            proc = subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:(F)"],
                capture_output=True, timeout=5, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return
        if proc.returncode == 0:
            return


def _key_file_accounts() -> list[str]:
    """Candidate icacls principals: domain-qualified first, plain user second."""
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    accounts = [f"{domain}\\{user}" if domain and user else "", user]
    return [account for index, account in enumerate(accounts)
            if account and account not in accounts[:index]]


def _clear_readonly(path: Path) -> None:
    """Drop the Windows read-only attribute that blocks os.replace."""
    if os.name != "nt":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & stat.S_IWRITE:
        return
    try:
        os.chmod(path, mode | stat.S_IWRITE)
    except OSError:
        pass


_REPLACE_RETRY_DELAYS = (0.05, 0.12, 0.25)


def _replace_with_retry(source: Path, target: Path) -> None:
    """Atomic replace with short retries for Windows sharing violations.

    Antivirus, the search indexer and cloud-sync filters open files briefly,
    and os.replace then reports WinError 5/32/33.  A restrictive ACL is not
    transient, so the last attempt surfaces the stage-tagged error instead of
    retrying forever; the caller's cleanup still unlinks the temp file.
    """
    healed = False
    for attempt, delay in enumerate(_REPLACE_RETRY_DELAYS):
        try:
            os.replace(source, target)
            return
        except PermissionError as error:
            if not healed:
                # A file written by an older build may carry an (R,W)-only
                # ACL; grant this account Full Control once before retrying.
                healed = True
                _clear_readonly(target)
                if os.name == "nt" and target.exists():
                    _restrict_key_file(target)
            if attempt == len(_REPLACE_RETRY_DELAYS) - 1:
                raise ConfigWriteError("替换配置文件", target, error) from error
            time.sleep(delay)
        except OSError as error:
            raise ConfigWriteError("替换配置文件", target, error) from error


class ConfigWriteError(OSError):
    """A settings-file write failed at a named stage.

    The stage and the OS error code are what let the settings panel say
    "replace failed: Windows error 5" instead of a generic write failure; the
    original exception stays chained for the server log.
    """

    def __init__(self, stage: str, path: str | Path, cause: BaseException) -> None:
        winerror = getattr(cause, "winerror", None)
        errno_value = getattr(cause, "errno", None)
        detail = f"{stage}失败"
        if winerror is not None:
            detail += f"（Windows 错误 {winerror}）"
        elif errno_value is not None:
            detail += f"（错误 {errno_value}）"
        super().__init__(f"{detail}：{cause}")
        # OSError.__init__ clears winerror, so the copied codes go on after it.
        self.winerror = winerror
        self.errno_value = errno_value
        self.stage = stage
        self.path = str(path)
        self.cause = cause


def write_saved_config(path: str | Path, config: dict[str, str]) -> None:
    # dsh atomic-write pattern: a crash can never leave a half-written
    # config behind, and the key is only ever visible at its final path
    # with owner-only permissions already applied.
    config_path = Path(path)
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ConfigWriteError("创建配置目录", config_path.parent, error) from error
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".llm-config-", dir=str(config_path.parent))
    except OSError as error:
        raise ConfigWriteError("创建临时文件", config_path.parent, error) from error
    tmp_path = Path(tmp_name)
    try:
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(config, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise ConfigWriteError("写入临时文件", tmp_path, error) from error
        _restrict_key_file(tmp_path)
        _clear_readonly(config_path)
        _replace_with_retry(tmp_path, config_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _system_proxy_hint() -> str:
    """Name the configured proxy when a network failure may come from it.

    urllib honours the Windows system proxy, so a broken proxy tunnel resets
    TLS and looks exactly like a dead endpoint. Only host and port are shown;
    proxy URLs can carry credentials.
    """
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return ""
    proxy = proxies.get("https") or proxies.get("http") or ""
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}") if proxy else None
    if not parsed or not parsed.hostname:
        return ""
    endpoint = f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname
    return f"（本机系统代理 {endpoint} 可能不可用，可切换节点或临时关闭后重试）"


class OpenAICompatiblePlanner:
    MAX_ATTEMPTS = 2
    RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
    RETRY_BACKOFF_SECONDS = 0.08
    PATCH_FIELDS = {
        "productType", "purpose", "quantity", "quantityValue", "quantityUnit", "size", "dimensions", "pages", "orientation", "paper",
        "printing", "finishing", "binding", "deadline", "budget", "platform", "productSpecs",
    }
    MAX_EVIDENCE_QUOTE = 2048
    MAX_EVIDENCE_SOURCE = 128
    MAX_PATCH_VALUE = 4096
    MAX_METADATA_FIELDS = 128
    MAX_PLAN_ANNOTATIONS = 32
    MAX_PLAN_ANNOTATION_TEXT = 1024
    MAX_PLAN_META_VERSION = 128
    MAX_CONTEXT_HISTORY = 8
    MAX_CONTEXT_MESSAGE_CHARS = 2000
    MAX_CONTEXT_VALUE_DEPTH = 5
    MAX_CONTEXT_LIST_ITEMS = 24
    MAX_CONTEXT_OBJECT_KEYS = 64
    MAX_CONTEXT_ORDER_BYTES = 12000
    MAX_CONTEXT_TOOL_RESULT_BYTES = 12000
    # The per-field limits above protect individual values.  This aggregate
    # cap protects the complete HTTP request (system prompt, history, tool
    # schemas and the bounded ledger) from silently growing beyond a provider
    # context window.  It is intentionally configurable for small gateways
    # and tests.
    MAX_CONTEXT_REQUEST_BYTES = 96 * 1024
    MAX_CONTEXT_LEDGER_ENTRIES = 8
    MAX_CONTEXT_LEDGER_BYTES = 32 * 1024
    MAX_TOOL_DESCRIPTION = 512
    CAPABILITY_PROBE_TOTAL_SECONDS = 26.0
    # One probe hop is the unit of probe latency.  The floor keeps a fast
    # endpoint from being judged inside a two-second window; the ceiling lets a
    # reasoning model answer within PRINTOPS_LLM_TIMEOUT without letting one
    # hung hop eat the whole probe (see docs/RUNTIME_BUDGET.md).
    CAPABILITY_PROBE_REQUEST_SECONDS = 6.0
    CAPABILITY_PROBE_REQUEST_MAX_SECONDS = 15.0
    # cc-switch's health check is a header-only GET on the base URL: any HTTP
    # answer proves the endpoint is live, and only a network-level failure is
    # decisive.  A refused port costs about two seconds on Windows and a
    # firewalled one the whole TCP retransmission budget, so liveness must be
    # settled by a cheap GET next to the chat hop instead of by waiting on a
    # generation request.  Both waits below are therefore deliberately short.
    CAPABILITY_PROBE_CONNECT_SECONDS = 2.5
    CAPABILITY_PROBE_GATE_SECONDS = 2.5
    MAX_CAPABILITY_RESPONSE_BYTES = 256 * 1024
    MAX_PLAN_RESPONSE_BYTES = 2 * 1024 * 1024
    CAPABILITY_TOOL_NAME = "printops_capability_probe"
    CAPABILITY_TOKEN = "printops-probe-v1"

    @staticmethod
    def _is_patch_scalar(value: Any) -> bool:
        """Only admit finite text/numeric scalars into the model patch."""
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return False
        return not (isinstance(value, float) and not math.isfinite(value))

    def __init__(self, base_url: str = "", api_key: str = "", model: str = "", timeout: int = 20,
                 allow_private_hosts: bool = False) -> None:
        self.allow_private_hosts = bool(allow_private_hosts)
        try:
            safe_url = normalize_base_url(base_url, allow_private_hosts=self.allow_private_hosts)
            config_error = ""
        except ValueError:
            safe_url = ""
            config_error = "接口 URL 格式不正确"
        self.base_url, self.api_key, self.model, self.timeout = safe_url, (api_key or "").strip(), (model or "").strip(), timeout
        self.last_error = config_error
        self.last_protocol = ""

    def for_request(self) -> "OpenAICompatiblePlanner":
        """Freeze config without repeating DNS; diagnostics belong to this run."""
        planner = copy(self)
        planner.last_error = ""
        planner.last_protocol = ""
        return planner

    def _request_json(self, request, deadline: float, maximum: int) -> Any:
        # Capture the opener before dispatch (also keeps injected test/local
        # transports stable if a timed-out worker exits after its caller).
        opener = urllib.request.urlopen

        def read_response():
            with opener(request, timeout=remaining(deadline)) as response:
                if hasattr(response, "read1"):
                    chunks, length = [], 0
                    while True:
                        budget = remaining(deadline)
                        raw_socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                        if raw_socket is not None:
                            raw_socket.settimeout(budget)
                        chunk = response.read1(min(65536, maximum + 1 - length))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        length += len(chunk)
                        if length > maximum:
                            raise ValueError("model response is too large")
                    raw = b"".join(chunks)
                else:
                    # Small injected response wrappers may expose read only.
                    try:
                        raw = response.read(maximum + 1)
                    except TypeError:
                        raw = response.read()
                remaining(deadline)
                if not isinstance(raw, (bytes, bytearray)) or len(raw) > maximum:
                    raise ValueError("model response is too large")
                return json.loads(bytes(raw).decode("utf-8"))

        return bounded_io(read_response, deadline)

    @staticmethod
    def _timeout_from_env() -> float:
        """Per-request model budget in seconds, clamped to 5..120.

        Slow reasoning models need more than the 20-second default; the
        HTTP total budget (PRINTOPS_REQUEST_TIMEOUT) must be raised in
        step, because a plan never outlives its request. See
        docs/RUNTIME_BUDGET.md.
        """
        raw = os.getenv("PRINTOPS_LLM_TIMEOUT", "").strip()
        try:
            value = float(raw) if raw else 20.0
        except ValueError:
            value = 20.0
        return max(5.0, min(120.0, value))

    @classmethod
    def from_env(cls) -> "OpenAICompatiblePlanner":
        allow_private = os.getenv("PRINTOPS_ALLOW_PRIVATE_LLM_HOSTS", "").strip().lower() in {"1", "true", "yes", "on"}
        return cls(os.getenv("PRINTOPS_LLM_URL", ""), os.getenv("PRINTOPS_LLM_KEY", ""),
                   os.getenv("PRINTOPS_LLM_MODEL", ""), timeout=cls._timeout_from_env(),
                   allow_private_hosts=allow_private)

    def configure(self, base_url: str, model: str, api_key: str = "") -> None:
        self.base_url = normalize_base_url(base_url, allow_private_hosts=self.allow_private_hosts)
        self.model = (model or "").strip()
        self.api_key = (api_key or "").strip()
        self.last_error = ""
        self.last_protocol = ""

    def public_config(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "url": self.base_url, "model": self.model,
                "keyConfigured": bool(self.api_key), "lastError": self.last_error,
                "privateHostsAllowed": self.allow_private_hosts,
                "protocolMode": self.last_protocol or "auto"}

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model)

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
                except (TypeError, ValueError, json.JSONDecodeError):
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
                except (TypeError, ValueError, json.JSONDecodeError):
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

    def plan(self, text: str, order: dict[str, Any], tools: list[dict[str, Any]],
             history: list[dict[str, str]] | None = None,
             tool_result: dict[str, Any] | None = None,
             tool_call: dict[str, Any] | None = None,
             tool_ledger: list[dict[str, Any]] | None = None,
             context_budget: int | None = None,
             **_kwargs: Any) -> dict[str, Any] | None:
        """Ask an OpenAI-compatible provider using native tools with JSON fallback.

        ``tool_ledger`` is optional so the adapter remains source-compatible
        with older Agent/custom planner implementations.  When supplied, the
        complete bounded ledger is replayed as native assistant/tool messages,
        or as one ``toolResults`` array in the JSON fallback envelope.
        """
        if not self.enabled:
            return None
        try:
            request_deadline = operation_deadline(self.timeout)
        except DeadlineExceeded:
            self.last_error, self.last_protocol = "请求总时限已到，模型结果未应用", "timeout"
            return None
        allow_tools = _kwargs.get("allow_tools", True) is not False
        if context_budget is None:
            candidate_budget = _kwargs.get("max_context_bytes", _kwargs.get("context_bytes"))
            if isinstance(candidate_budget, int) and not isinstance(candidate_budget, bool):
                context_budget = candidate_budget
        prompt = {"role": "system", "content": (
            "你是印刷订单助手，使用简洁自然的中文和非专业用户对话。"
            "本接口优先使用 OpenAI-compatible native function/tool calls；如果服务端不支持 tools，"
            "会改用 JSON fallback。两种协议都必须返回一个 PrintOps JSON 对象："
            "{reply:string,patch:object,confidence:object,evidence:array,questions:array,risks:array,"
            "knowledgeVersion:string,tool:{name:string,arguments:object}|null}。"
            "native tool call 的 arguments 必须是 JSON 对象；JSON fallback 也必须把 tool 写在该对象内。"
            "reply 是给用户看的自然语言，必须在需要时提出下一步问题；patch 只能使用订单字段 "
            "productType,purpose,quantity,quantityValue,quantityUnit,size,dimensions,pages,orientation,paper,"
            "printing,finishing,binding,deadline,budget,platform,productSpecs。quantity 是给用户看的数量文本，"
            "quantityValue 是数字，quantityUnit 是张、份、个等单位；三者不一致时只提交 quantity，让系统统一规范化。"
            "dimensions 只能包含 finishedSize、expandedSize、dieCutSize、packageSize。品类专属参数必须放在 "
            "productSpecs 对象中（例如 folding、paperParts、boxSize、boxStructure、labelMaterial、labelShape、"
            "bagSize、handle、cupVolume、displayMaterial、install、boardThickness）。confidence 是字段路径到 "
            "0~1 数字的对象；evidence 是 [{field,quote,source}]，quote 必须是支持该字段的原文短引文，source 填 "
            "user、rule 或 model。questions 和 risks 只能记录待确认事项，不得替代确定性校验；knowledgeVersion 必须来自 "
            "当前 PrintOps 知识上下文。未知品类参数不要放进 patch，可列入 rejectedFields。工具使用规则：信息不完整时优先调用 "
            "validate_order；订单核心字段完整且用户需要方案时调用 recommend_processes；用户问费用时调用 estimate_price；"
            "术语问题调用 explain_print_term；只有订单满足系统前置条件时才调用询价或交接工具。工具调用必须使用当前可用工具名和 "
            "JSON 对象参数，不要把完整 order 作为参数，系统会提供当前会话订单。收到工具结果后，要么调用下一步不同的必要工具，"
            "要么给出总结，不要重复同一工具。不要编造价格、供应商能力或已提交订单。"
        )}

        history_messages: list[dict[str, Any]] = []
        for item in (history or [])[-self.MAX_CONTEXT_HISTORY:]:
            if not isinstance(item, dict):
                continue
            role = item.get("role") if item.get("role") in {"user", "assistant"} else "user"
            content = item.get("text", item.get("content", ""))
            content = str(content or "").strip()
            if content:
                history_messages.append({"role": role,
                                         "content": self._clip_context_text(content, self.MAX_CONTEXT_MESSAGE_CHARS)})

        provider_tools = self._provider_tools(tools) if allow_tools else []
        compact_catalog = self._compact_tool_catalog(tools) if allow_tools else []
        ledger_source: Any = tool_ledger
        raw_batch_calls = _kwargs.get("tool_calls")
        if not ledger_source and isinstance(raw_batch_calls, list):
            # A transitional Agent may pass batch call metadata before it has
            # started persisting receipts.  Keep every call visible and attach
            # the supplied result to the newest one.
            ledger_source = [
                {"call": call,
                 "result": tool_result if index == len(raw_batch_calls) - 1 else None}
                for index, call in enumerate(raw_batch_calls)
                if isinstance(call, dict)
            ]
        entries = self._normalize_tool_ledger(ledger_source, tool_result=tool_result, tool_call=tool_call)
        ledger_context = self._ledger_context(entries)

        # The current user payload is built once and then copied for each
        # protocol variant.  This is the key invariant that prevents a
        # native->JSON retry from appending a second copy of the user turn.
        common_user: dict[str, Any] = {
            "text": self._clip_context_text(str(text or ""), self.MAX_CONTEXT_MESSAGE_CHARS),
            "order": self._bounded_context_object(order, self.MAX_CONTEXT_ORDER_BYTES),
            "protocol": "native_tools_or_json_fallback",
        }

        def add_ledger(payload: dict[str, Any]) -> None:
            if not ledger_context:
                return
            payload["toolResults"] = deepcopy(ledger_context)
            # Keep the old singular field for adapters that only understand
            # one observation; the array remains the authoritative ledger.
            payload["toolResult"] = self._bounded_context_object(
                tool_result if tool_result is not None else ledger_context[-1].get("result"),
                self.MAX_CONTEXT_TOOL_RESULT_BYTES,
            )

        native_user = deepcopy(common_user)
        if provider_tools:
            # Keep the compact catalog on the first request for older
            # OpenAI-compatible gateways that inspect the user envelope
            # instead of the protocol-level ``tools`` field.  Once a ledger
            # exists, names are sufficient because the full schemas remain in
            # the protocol field and this avoids repeating descriptions.
            native_user["tools"] = (compact_catalog if not entries else
                                     [{"name": item["function"]["name"]} for item in provider_tools])
        else:
            native_user["tools"] = []
            # With native tools disabled (including the final synthesis turn),
            # the JSON user envelope is the only observation channel.
            add_ledger(native_user)
        native_messages: list[dict[str, Any]] = [prompt, *history_messages,
                                                 {"role": "user", "content": json.dumps(native_user, ensure_ascii=False)}]
        if entries and allow_tools:
            native_messages = self._native_ledger_messages(native_messages, entries)
        native_payload: dict[str, Any] = {"model": self.model, "messages": native_messages}
        if provider_tools:
            native_payload["tools"] = provider_tools
            native_payload["tool_choice"] = "auto"
        native_payload = self._fit_request_budget(native_payload, context_budget)

        payload_variants: list[dict[str, Any]] = [native_payload]
        if provider_tools:
            # Older gateways reject protocol-level ``tools``.  Rebuild from
            # system + history + exactly one current user payload; native
            # assistant/tool messages are represented by ``toolResults``.
            fallback_user = deepcopy(common_user)
            add_ledger(fallback_user)
            fallback_user["tools"] = compact_catalog
            fallback_messages: list[dict[str, Any]] = [prompt, *history_messages,
                                                        {"role": "user", "content": json.dumps(
                                                            fallback_user, ensure_ascii=False)}]
            fallback_payload = self._fit_request_budget(
                {"model": self.model, "messages": fallback_messages}, context_budget)
            payload_variants.append(fallback_payload)

        self.last_error = ""
        valid_tool_names = ({item.get("name") for item in tools if isinstance(item, dict)}
                            if allow_tools else set())
        protocol_before_request = self.last_protocol
        for variant_index, variant in enumerate(payload_variants):
            request = urllib.request.Request(
                f"{self.base_url}/chat/completions", data=json.dumps(variant, ensure_ascii=False).encode(), method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json",
                         **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
            )
            for attempt in range(self.MAX_ATTEMPTS):
                retryable = False
                try:
                    result = self._request_json(request, request_deadline, self.MAX_PLAN_RESPONSE_BYTES)
                    native_plan = self._native_tool_plan(result) if variant_index == 0 and provider_tools else None
                    if native_plan is not None:
                        plan = self.validate_plan(native_plan, valid_tool_names)
                        if plan is not None:
                            self.last_protocol = native_plan.get("_nativeProtocol", "native_tools")
                            self.last_error = ""
                            return plan
                        # A malformed native envelope should get a JSON retry,
                        # rather than silently ending the Agent's tool loop.
                        self.last_error = "模型 native 工具响应无法识别"
                        if variant_index == 0 and len(payload_variants) > 1:
                            break
                    else:
                        # If the provider advertised native call fields but
                        # none could be decoded, force the JSON variant.  A
                        # malformed native call must not be mistaken for a
                        # successful text-only turn.
                        if variant_index == 0 and provider_tools and self._has_native_call_marker(result):
                            self.last_error = "模型 native 工具响应无法解析，尝试 JSON fallback"
                            if len(payload_variants) > 1:
                                break
                        content = self._response_content(result)
                        if content in (None, ""):
                            content = self._response_text(result)
                        plan = self.validate_plan(self._parse_plan(content), valid_tool_names)
                        if plan is not None:
                            if variant_index:
                                self.last_protocol = "json_fallback"
                            elif protocol_before_request == "native_tools":
                                # A native tool turn is commonly followed by
                                # a plain JSON synthesis response.  Preserve
                                # the session's effective protocol mode so UI
                                # diagnostics do not report a false downgrade.
                                self.last_protocol = "native_tools"
                            else:
                                self.last_protocol = "native_json"
                            self.last_error = ""
                            return plan
                        self.last_error = "模型返回内容无法识别"
                        if variant_index == 0 and len(payload_variants) > 1:
                            break
                except DeadlineExceeded:
                    self.last_error, self.last_protocol = "请求总时限已到，模型结果未应用", "timeout"
                    return None
                except HTTPError as error:
                    self.last_error = f"模型接口返回 HTTP {error.code}"
                    self.last_protocol = "error"
                    _close_error_response(error)
                    if variant_index == 0 and error.code in {400, 404, 405, 422}:
                        break
                    retryable = error.code in self.RETRYABLE_HTTP_CODES
                except (URLError, TimeoutError, OSError):
                    self.last_error = "模型接口连接失败"
                    self.last_protocol = "error"
                    retryable = True
                except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
                    self.last_error = "模型接口返回格式异常"
                    self.last_protocol = "error"
                    if variant_index == 0 and len(payload_variants) > 1:
                        break
                if retryable and attempt + 1 < self.MAX_ATTEMPTS:
                    try:
                        deadline_sleep(self.RETRY_BACKOFF_SECONDS * (attempt + 1), request_deadline)
                    except DeadlineExceeded:
                        self.last_error, self.last_protocol = "请求总时限已到，模型结果未应用", "timeout"
                        return None
        return None

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

    @staticmethod
    def _classify_probe_error(error: Exception) -> tuple[str, str]:
        """Map a probe failure to (user-facing cause, kind).

        dsh's timeout library keeps capability-owned reason codes instead
        of one generic error; kind drives fail-fast: network/auth stop
        the probe (the endpoint or key will not heal mid-probe), protocol
        errors continue (payload-specific 4xx/5xx are still informative).
        """
        reason = getattr(error, "reason", None)
        if isinstance(error, HTTPError):
            if error.code in (401, 403):
                return f"HTTP {error.code}：API Key 被拒绝或无权访问该模型", "auth"
            if error.code == 404:
                return "HTTP 404：接口路径或模型名不存在（URL 通常应以 /v1 结尾）", "protocol"
            if error.code == 429:
                return "HTTP 429：供应商限流，请稍后重试", "protocol"
            return f"HTTP {error.code}", "protocol"
        if isinstance(error, DeadlineExceeded):
            # The probe's own bounded wait expired (urlopen reads can outlast
            # the caller budget); from the user's side this is indistinguish-
            # able from a socket read timeout, so report the same actionable
            # cause instead of an internal deadline name.
            return "连接或响应超时，端点未在时限内返回数据", "network"
        if isinstance(error, (TimeoutError, socket.timeout)) \
                or isinstance(reason, (TimeoutError, socket.timeout)):
            return "连接或响应超时，端点未在时限内返回数据" + _system_proxy_hint(), "network"
        if isinstance(error, socket.gaierror) or isinstance(reason, socket.gaierror):
            return "无法解析主机名，请检查域名拼写或本机 DNS/代理", "network"
        if isinstance(error, ConnectionRefusedError) or isinstance(reason, ConnectionRefusedError):
            return "连接被拒绝，目标端口未监听或地址错误" + _system_proxy_hint(), "network"
        if isinstance(error, ssl.SSLError) or isinstance(reason, ssl.SSLError):
            return "TLS 握手失败，请检查 HTTPS 证书或代理设置" + _system_proxy_hint(), "network"
        if isinstance(error, URLError):
            return "网络错误：" + str(reason or error) + _system_proxy_hint(), "network"
        return "模型接口连接失败", "network"

    def _probe_reachability(self, target_url: str, deadline: float) -> tuple[bool, str, str, int]:
        """Ask the endpoint once, without a model request, whether it answers.

        Only a network-level failure decides anything: a 401/404 on the base
        path says nothing about the key or the completions route, which the
        capability hops judge.  Any unexpected shape fails open, because a
        diagnostic must never veto the probe it is trying to accelerate.
        """
        began = time.monotonic()
        budget = min(self.CAPABILITY_PROBE_CONNECT_SECONDS,
                     max(0.5, deadline - time.monotonic()))
        request = urllib.request.Request(target_url, method="GET",
                                         headers={"Accept": "*/*"})

        def fetch() -> Any:
            with urllib.request.urlopen(request, timeout=remaining(
                    operation_deadline(budget, deadline))) as response:
                return getattr(response, "status", 200)

        try:
            bounded_io(fetch, operation_deadline(budget, deadline))
            return True, "", "", self._latency(began)
        except HTTPError as error:
            _close_error_response(error)
            return True, "", "", self._latency(began)
        except (URLError, TimeoutError, OSError) as error:
            message, kind = self._classify_probe_error(error)
            if kind == "network" and "超时" in message:
                message = f"{message}（{budget:.1f} 秒内未能建立连接）"
            return False, message, kind, self._latency(began)
        except Exception:
            return True, "", "", self._latency(began)

    def _capability_post(self, target_url: str, target_model: str, target_key: str,
                         payload: dict[str, Any], deadline: float) -> tuple[dict[str, Any] | None, str, str]:
        """Send one bounded, non-retrying capability request."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "能力探测已超时", "network"
        try:
            configured_timeout = float(self.timeout)
        except (TypeError, ValueError, OverflowError):
            configured_timeout = self.CAPABILITY_PROBE_REQUEST_SECONDS
        if not math.isfinite(configured_timeout) or configured_timeout <= 0:
            configured_timeout = self.CAPABILITY_PROBE_REQUEST_SECONDS
        probe_budget = max(self.CAPABILITY_PROBE_REQUEST_SECONDS,
                           min(configured_timeout, self.CAPABILITY_PROBE_REQUEST_MAX_SECONDS))
        request_timeout = min(remaining, probe_budget)
        request = urllib.request.Request(
            f"{target_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     **({"Authorization": f"Bearer {target_key}"} if target_key else {})},
        )
        try:
            request_deadline = operation_deadline(request_timeout, deadline)
            decoded = self._request_json(request, request_deadline, self.MAX_CAPABILITY_RESPONSE_BYTES)
            if not isinstance(decoded, dict):
                return None, "模型接口响应不是 JSON 对象", "protocol"
            return decoded, "", ""
        except HTTPError as error:
            message, kind = self._classify_probe_error(error)
            _close_error_response(error)
            return None, message, kind
        except (URLError, TimeoutError, OSError) as error:
            message, kind = self._classify_probe_error(error)
            return None, message, kind
        except (UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            return None, "模型接口返回格式异常", "protocol"

    def _probe_root_cause(self, result: dict[str, Any], started: float,
                          error: str, kind: str, skipped: str) -> dict[str, Any]:
        """Stop the probe and report the one cause the user can act on."""
        result["errors"]["chat"] = error
        prefix = "鉴权失败：" if kind == "auth" else "模型接口不可达："
        result["message"] = f"{prefix}{error}，已跳过{skipped}"
        result["chosenProtocol"] = "unavailable" if kind == "auth" else "unreachable"
        result["latencyMs"] = self._latency(started)
        self.last_error, self.last_protocol = result["message"], ""
        return result

    def test_capabilities(self, base_url: str | None = None, model: str | None = None,
                          api_key: str | None = None) -> dict[str, Any]:
        """Probe whether an endpoint can drive the Agent without order data.

        The probe never executes a real tool.  It advertises one synthetic,
        side-effect-free function and checks whether the model returns a
        matching call.  Tool-result continuation is left to the Agent loop,
        which already retries with the JSON protocol when a provider rejects
        a ``tool`` turn; probing it here cost a second generation round trip
        (2-9s on reasoning models) and misjudged providers by the model's
        wording rather than by the protocol it accepted.
        """
        started = time.monotonic()
        result: dict[str, Any] = {
            "ok": False,
            "chat": False,
            "jsonPlan": False,
            "nativeTools": False,
            "chosenProtocol": "unavailable",
            "agentReady": False,
            "message": "模型接口尚未配置",
            "latencyMs": None,
            "errors": {},
        }
        try:
            target_url = self.base_url if base_url is None else normalize_base_url(
                base_url, allow_private_hosts=self.allow_private_hosts)
        except ValueError:
            result["message"] = "接口 URL 格式不正确"
            result["latencyMs"] = self._latency(started)
            return result
        target_model = self.model if model is None else (model or "").strip()
        target_key = self.api_key if api_key is None else (api_key or "").strip()
        if not target_url or not target_model:
            result["latencyMs"] = self._latency(started)
            return result

        deadline = started + self.CAPABILITY_PROBE_TOTAL_SECONDS
        errors: dict[str, str] = result["errors"]
        timings: dict[str, int] = {}
        result["probeTimings"] = timings

        chat_marker = "PRINTOPS_CAPABILITY_CHAT_OK"
        chat_payload = {
            "model": target_model,
            "messages": [
                {"role": "system", "content": "这是只读协议能力检查，不处理订单或业务数据。"},
                {"role": "user", "content": f"请回复 {chat_marker}。"},
            ],
            "max_tokens": 32,
        }
        json_marker = "PRINTOPS_CAPABILITY_JSON_OK"
        json_payload = {
            "model": target_model,
            "messages": [
                {"role": "system", "content": (
                    "这是只读协议能力检查，不处理订单或业务数据。"
                    "只输出请求的 JSON 对象，不要 Markdown 或额外文字。")},
                {"role": "user", "content": (
                    '{"reply":"' + json_marker + '","patch":{},"tool":null}')},
            ],
            "max_tokens": 96,
        }
        tool_schema = {
            "type": "function",
            "function": {
                "name": self.CAPABILITY_TOOL_NAME,
                "description": "只读协议探测函数；无外部副作用，不访问任何订单。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "token": {"type": "string", "enum": [self.CAPABILITY_TOKEN]},
                    },
                    "required": ["token"],
                    "additionalProperties": False,
                },
            },
        }
        native_messages = [
            {"role": "system", "content": (
                "这是只读协议能力检查。必须调用给定的无副作用工具一次，"
                "并原样传入指定 token；不要处理订单或业务数据。")},
            {"role": "user", "content": f"调用 {self.CAPABILITY_TOOL_NAME}，token={self.CAPABILITY_TOKEN}。"},
        ]
        native_payload = {
            "model": target_model,
            "messages": native_messages,
            "tools": [tool_schema],
            "tool_choice": "auto",
            "max_tokens": 96,
        }

        def hop(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str, str, int]:
            began = time.monotonic()
            response, message, kind = self._capability_post(
                target_url, target_model, target_key, payload, deadline)
            return response, message, kind, self._latency(began)

        # Three serial generation round trips measured 8.7s on a live gateway.
        # Chat, JSON and native tools are independent once the endpoint is known
        # to be alive, so they overlap in one round trip.  A header-only GET
        # runs beside them to settle liveness first: on a live endpoint it
        # answers in a few hundred milliseconds, while a refused or firewalled
        # endpoint yields one named root cause instead of three identical
        # connect failures.
        pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="printops-capability")
        try:
            gate_began = time.monotonic()
            chat_future = pool.submit(hop, chat_payload)
            reach_future = pool.submit(self._probe_reachability, target_url, deadline)
            futures_wait((chat_future, reach_future), self.CAPABILITY_PROBE_GATE_SECONDS,
                         return_when=FIRST_COMPLETED)
            timings["gateMs"] = self._latency(gate_began)
            if reach_future.done():
                reachable, reach_error, reach_kind, timings["reachMs"] = reach_future.result()
                if not reachable:
                    timings["hops"] = 1
                    return self._probe_root_cause(result, started, reach_error, reach_kind,
                                                  "后续检查")
            chat_resolved = chat_future.done()
            if chat_resolved:
                chat_response, chat_error, chat_kind, timings["chatMs"] = chat_future.result()
                if chat_response is None and chat_kind in {"network", "auth"}:
                    timings["hops"] = 1
                    return self._probe_root_cause(result, started, chat_error, chat_kind,
                                                  "后续检查")
            json_future = pool.submit(hop, json_payload)
            native_future = pool.submit(hop, native_payload)
            if not chat_resolved:
                chat_response, chat_error, chat_kind, timings["chatMs"] = chat_future.result()
            json_response, json_error, _json_kind, timings["jsonMs"] = json_future.result()
            native_response, native_error, _native_kind, timings["nativeMs"] = native_future.result()
        finally:
            # Abandon a hop that outlived the verdict instead of holding the
            # handler open: request_runtime discards late results, and a hung
            # connection does not heal while the user waits for an answer.
            pool.shutdown(wait=False)
        timings["hops"] = 3
        if chat_response is None and chat_kind in {"network", "auth"}:
            # Nothing proved the endpoint answers a completion request, so the
            # remaining verdict would only repeat the same failure.
            return self._probe_root_cause(result, started, chat_error, chat_kind,
                                          "后续检查")

        if chat_response is not None and self._response_text(chat_response).strip():
            result["chat"] = True
        else:
            errors["chat"] = chat_error or "模型接口返回空内容"

        if json_response is not None:
            value = self._strict_json_object(self._response_text(json_response))
            normalized = self.validate_plan(value, set())
            result["jsonPlan"] = bool(
                normalized and normalized.get("reply") == json_marker
                and normalized.get("patch") == {} and normalized.get("tool") is None)
        if not result["jsonPlan"]:
            errors["jsonPlan"] = json_error or "未返回指定 JSON 规划对象"

        matching_call: dict[str, Any] | None = None
        if native_response is not None:
            native_calls, _native_protocol = self._native_calls(native_response)
            matching_call = next((call for call in native_calls
                                  if call.get("function", {}).get("name") == self.CAPABILITY_TOOL_NAME
                                  and call.get("function", {}).get("arguments", {}).get("token")
                                  == self.CAPABILITY_TOKEN), None)
        result["nativeTools"] = matching_call is not None
        if matching_call is None:
            errors["nativeTools"] = native_error or "未返回带正确参数的 native tool call"

        if result["nativeTools"]:
            chosen = "native_tools"
            message = "接口支持原生工具调用，可用于 Agent。"
        elif result["jsonPlan"]:
            chosen = "json_fallback"
            message = "接口支持 JSON 规划，可使用兼容模式运行 Agent。"
        elif result["chat"]:
            chosen = "chat_only"
            message = "接口仅通过基础聊天检查，暂不满足 Agent 规划协议。"
        else:
            chosen = "unavailable"
            message = "模型接口不可用或返回格式不兼容。"
        result["chosenProtocol"] = chosen
        result["agentReady"] = chosen in {"native_tools", "json_fallback"}
        result["ok"] = bool(result["chat"] or result["jsonPlan"] or result["nativeTools"])
        result["message"] = message
        result["latencyMs"] = self._latency(started)
        self.last_protocol = chosen
        self.last_error = "" if result["agentReady"] else message
        return result

    def test_connection(self, base_url: str | None = None, model: str | None = None,
                        api_key: str | None = None) -> dict[str, Any]:
        """Check a provider without sending order data or exposing the key."""
        target_url = self.base_url if base_url is None else normalize_base_url(
            base_url, allow_private_hosts=self.allow_private_hosts)
        target_model = self.model if model is None else (model or "").strip()
        target_key = self.api_key if api_key is None else (api_key or "").strip()
        if not (target_url and target_model):
            return {"ok": False, "message": "模型接口尚未配置", "latencyMs": None}
        payload = {
            "model": target_model,
            "messages": [{"role": "user", "content": "只回复 OK"}],
            "max_tokens": 4,
        }
        request = urllib.request.Request(
            f"{target_url}/chat/completions", data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     **({"Authorization": f"Bearer {target_key}"} if target_key else {})},
        )
        started = time.monotonic()
        try:
            request_deadline = operation_deadline(self.timeout)
            result = self._request_json(request, request_deadline, self.MAX_PLAN_RESPONSE_BYTES)
            if not self._response_text(result).strip():
                self.last_error = "模型接口返回空内容"
                return {"ok": False, "message": self.last_error, "latencyMs": self._latency(started)}
            self.last_error = ""
            return {"ok": True, "message": "模型接口连接正常", "latencyMs": self._latency(started)}
        except HTTPError as error:
            self.last_error = f"模型接口返回 HTTP {error.code}"
            _close_error_response(error)
        except (URLError, TimeoutError, OSError):
            self.last_error = "模型接口连接失败"
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            self.last_error = "模型接口返回格式异常"
        return {"ok": False, "message": self.last_error, "latencyMs": self._latency(started)}

    @staticmethod
    def _latency(started: float) -> int:
        return round((time.monotonic() - started) * 1000)

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
        except json.JSONDecodeError:
            pass
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        # A plain-language answer is still useful. It should not discard the
        # model entirely just because it did not follow the JSON instruction.
        return {"reply": text, "patch": {}, "tool": None}

    @classmethod
    def _normalize_plan_annotation(cls, value: Any) -> str | dict[str, str] | None:
        """Keep advisory skill questions/risks small and display-oriented.

        These fields are useful for dsh skill output, but they are not an
        authority channel.  Drop arbitrary nested model data before the plan
        reaches the Agent's persisted state.
        """
        if isinstance(value, str):
            text = value.strip()
            return text[:cls.MAX_PLAN_ANNOTATION_TEXT] if text else None
        if not isinstance(value, dict):
            return None
        result: dict[str, str] = {}
        for key in ("field", "question", "text", "message", "risk", "severity", "source", "code"):
            raw = value.get(key)
            if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
                continue
            if isinstance(raw, float) and not math.isfinite(raw):
                continue
            text = str(raw).strip()
            if text:
                result[key] = text[:cls.MAX_PLAN_ANNOTATION_TEXT]
        return result or None

    @classmethod
    def _normalize_plan_annotations(cls, raw: Any) -> list[str | dict[str, str]]:
        if not isinstance(raw, list):
            return []
        result: list[str | dict[str, str]] = []
        for item in raw[:cls.MAX_PLAN_ANNOTATIONS]:
            normalized = cls._normalize_plan_annotation(item)
            if normalized is not None:
                result.append(normalized)
        return result

    @classmethod
    def _normalize_plan_version(cls, raw: Any) -> str:
        if not isinstance(raw, str):
            return ""
        return raw.strip()[:cls.MAX_PLAN_META_VERSION]

    @classmethod
    def validate_plan(cls, value: Any, tool_names: set[str] | None = None) -> dict[str, Any] | None:
        """Normalize the model contract before it can mutate order state or call a tool."""
        if not isinstance(value, dict):
            return None
        reply = value.get("reply")
        reply = str(reply).strip() if reply is not None else ""
        if len(reply) > 4000:
            reply = reply[:4000].rstrip() + "..."
        raw_patch = value.get("patch")
        patch: dict[str, Any] = {}
        rejected_fields: list[str] = []
        product_hint = ""
        if isinstance(raw_patch, dict) and isinstance(raw_patch.get("productType"), str):
            product_hint = raw_patch.get("productType", "").strip()
        allowed_specs = known_product_spec_keys(product_hint or None)
        # Dimension aliases are accepted by the Agent's canonical dimensions
        # gateway even though they are not product-specific catalog questions.
        allowed_specs.update(DIMENSION_DEFAULTS)
        if isinstance(raw_patch, dict):
            for raw_key, item in raw_patch.items():
                # JSON object names are strings, but custom planner adapters
                # can call this validator with a Python mapping directly.
                # Reject non-string names before set membership or string
                # operations so malformed payloads fail closed instead of
                # escaping as a TypeError.
                if not isinstance(raw_key, str):
                    rejected_fields.append("<non-string-patch-key>")
                    continue
                key = raw_key
                if key not in cls.PATCH_FIELDS:
                    if isinstance(key, str) and key.strip():
                        rejected_fields.append(key.strip()[:256])
                    continue
                if item is None:
                    continue
                if key == "productSpecs":
                    if not isinstance(item, dict):
                        rejected_fields.append("productSpecs")
                        continue
                    specs: dict[str, str] = {}
                    for raw_name, raw_spec in item.items():
                        if not isinstance(raw_name, str):
                            rejected_fields.append("productSpecs.<non-string-key>")
                            continue
                        name = raw_name.strip()
                        if not name:
                            continue
                        field_name = f"productSpecs.{name}"[:256]
                        if name not in allowed_specs:
                            rejected_fields.append(field_name)
                            continue
                        if raw_spec is None:
                            continue
                        if not cls._is_patch_scalar(raw_spec):
                            rejected_fields.append(field_name)
                            continue
                        spec = str(raw_spec).strip()
                        if spec:
                            specs[name] = spec[:cls.MAX_EVIDENCE_QUOTE]
                    if specs:
                        patch[key] = specs
                elif key == "dimensions":
                    if not isinstance(item, dict):
                        rejected_fields.append("dimensions")
                        continue
                    dimensions: dict[str, str] = {}
                    for raw_name, raw_value in item.items():
                        if not isinstance(raw_name, str):
                            rejected_fields.append("dimensions.<non-string-key>")
                            continue
                        name = raw_name.strip()
                        field_name = f"dimensions.{name}"[:256]
                        if name not in DIMENSION_DEFAULTS:
                            rejected_fields.append(field_name)
                            continue
                        if raw_value is None:
                            continue
                        if not cls._is_patch_scalar(raw_value):
                            rejected_fields.append(field_name)
                            continue
                        normalized = str(raw_value).strip()
                        if normalized:
                            dimensions[name] = normalized[:cls.MAX_EVIDENCE_QUOTE]
                    if dimensions:
                        patch[key] = dimensions
                elif cls._is_patch_scalar(item):
                    try:
                        text = str(item).strip()
                    except (OverflowError, ValueError):
                        rejected_fields.append(str(key).strip()[:256])
                        continue
                    text = text[:cls.MAX_PATCH_VALUE]
                    if text:
                        patch[key] = text
                else:
                    rejected_fields.append(str(key).strip()[:256])
        raw_tool = value.get("tool")
        tool: dict[str, Any] | None = None
        if isinstance(raw_tool, dict):
            name = raw_tool.get("name")
            arguments = raw_tool.get("arguments", {})
            if isinstance(name, str) and name in (tool_names or set()) and isinstance(arguments, dict):
                tool = {"name": name, "arguments": arguments}
        confidence = cls._normalize_confidence(value.get("confidence"), patch)
        evidence = cls._normalize_evidence(value.get("evidence"), patch)
        supplied_rejected = value.get("rejectedFields")
        if isinstance(supplied_rejected, list):
            for item in supplied_rejected:
                if isinstance(item, str) and item.strip():
                    rejected_fields.append(item.strip()[:256])
        # Keep audit output deterministic and bounded.  A model can still
        # receive a useful reply when every proposed field was rejected.
        rejected_fields = list(dict.fromkeys(rejected_fields))[:cls.MAX_METADATA_FIELDS]
        metadata_present = any(key in value for key in
                              ("questions", "risks", "knowledgeVersion", "reportedKnowledgeVersion"))
        if not reply and not patch and tool is None and not rejected_fields and not metadata_present:
            return None
        result: dict[str, Any] = {"reply": reply, "patch": patch, "tool": tool}
        if confidence:
            result["confidence"] = confidence
        if evidence:
            result["evidence"] = evidence
        if rejected_fields:
            result["rejectedFields"] = rejected_fields
        # Preserve the common dsh skill envelope after bounding it.  Agent
        # treats these values as advisory ``planMeta`` only; they cannot write
        # order fields or authorize a tool call.
        if "questions" in value:
            result["questions"] = cls._normalize_plan_annotations(value.get("questions"))
        if "risks" in value:
            result["risks"] = cls._normalize_plan_annotations(value.get("risks"))
        if "knowledgeVersion" in value:
            result["knowledgeVersion"] = cls._normalize_plan_version(value.get("knowledgeVersion"))
        if "reportedKnowledgeVersion" in value:
            result["reportedKnowledgeVersion"] = cls._normalize_plan_version(value.get("reportedKnowledgeVersion"))
        raw_native_calls = value.get("_nativeToolCalls")
        normalized_native_calls: list[dict[str, Any]] = []
        if isinstance(raw_native_calls, list):
            for raw_call in raw_native_calls[:cls.MAX_CONTEXT_LEDGER_ENTRIES]:
                call = cls._normalize_native_call(raw_call)
                if call is not None and call["function"]["name"] in (tool_names or set()):
                    normalized_native_calls.append(call)
        native_call = value.get("_nativeToolCall")
        normalized_native_call = cls._normalize_native_call(native_call)
        if (normalized_native_call is not None
                and normalized_native_call["function"]["name"] in (tool_names or set())):
            if not normalized_native_calls:
                normalized_native_calls.append(normalized_native_call)
            result["_nativeToolCall"] = normalized_native_call
        if normalized_native_calls:
            # Preserve all calls for a bounded batch.  The first-call field is
            # retained above for older Agent implementations.
            result["_nativeToolCalls"] = normalized_native_calls
        native_protocol = value.get("_nativeProtocol")
        if isinstance(native_protocol, str) and native_protocol.strip():
            result["_nativeProtocol"] = native_protocol.strip()[:64]
        return result

    @classmethod
    def _normalize_confidence(cls, raw: Any, patch: dict[str, Any]) -> dict[str, float]:
        """Keep only finite confidence grades for fields in the accepted patch."""
        if not isinstance(raw, dict):
            return {}
        accepted = cls._patch_field_paths(patch)
        normalized: dict[str, float] = {}
        for raw_key, raw_value in raw.items():
            if not isinstance(raw_key, str):
                continue
            key = raw_key.strip()
            if not key or not cls._metadata_path_allowed(key, accepted):
                continue
            if isinstance(raw_value, bool):
                continue
            try:
                number = float(raw_value)
            except (OverflowError, TypeError, ValueError):
                continue
            if not math.isfinite(number):
                continue
            normalized[key[:256]] = round(max(0.0, min(1.0, number)), 3)
            if len(normalized) >= cls.MAX_METADATA_FIELDS:
                break
        return normalized

    @classmethod
    def _normalize_evidence(cls, raw: Any, patch: dict[str, Any]) -> dict[str, dict[str, str]]:
        """Normalize dsh evidence arrays or field-to-quote maps.

        The public dsh contract uses an array of ``{field, quote, source}``
        records, while adapters often find a compact ``{field: quote}`` map
        more convenient.  Both are converted to a bounded field map here.
        """
        if not isinstance(raw, (dict, list)):
            return {}
        accepted = cls._patch_field_paths(patch)
        candidates: list[tuple[Any, Any, Any]] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                candidates.append((item.get("field"), item.get("quote", item.get("evidence")), item.get("source")))
        else:
            for field, item in raw.items():
                if isinstance(item, dict):
                    candidates.append((field, item.get("quote", item.get("evidence")), item.get("source")))
                else:
                    candidates.append((field, item, None))
        normalized: dict[str, dict[str, str]] = {}
        for raw_field, raw_quote, raw_source in candidates:
            if not isinstance(raw_field, str):
                continue
            field = raw_field.strip()
            # Evidence is useful only for values this plan is allowed to
            # apply; this also prevents arbitrary metadata keys from leaking
            # into the Agent state.
            if not field or not cls._metadata_path_allowed(field, accepted):
                continue
            if not isinstance(raw_quote, str):
                continue
            quote = raw_quote.strip()
            if not quote:
                continue
            entry = {"quote": quote[:cls.MAX_EVIDENCE_QUOTE]}
            if isinstance(raw_source, str) and raw_source.strip():
                entry["source"] = raw_source.strip()[:cls.MAX_EVIDENCE_SOURCE]
            normalized[field[:256]] = entry
            if len(normalized) >= cls.MAX_METADATA_FIELDS:
                break
        return normalized

    @staticmethod
    def _patch_field_paths(patch: dict[str, Any]) -> set[str]:
        paths: set[str] = set()
        for key in patch:
            if key == "productSpecs" and isinstance(patch.get(key), dict):
                paths.add("productSpecs")
                paths.update(f"productSpecs.{name}" for name in patch[key])
            elif key == "dimensions" and isinstance(patch.get(key), dict):
                paths.add("dimensions")
                paths.update(f"dimensions.{name}" for name in patch[key])
            else:
                paths.add(str(key))
        return paths

    @staticmethod
    def _metadata_path_allowed(field: str, accepted: set[str]) -> bool:
        """Allow an optional stable multi-product item prefix."""
        if field in accepted or field in {"productSpecs", "dimensions"}:
            return True
        parts = field.split(".")
        if len(parts) < 3 or parts[0] != "items":
            return False
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", parts[1]):
            return False
        remainder = ".".join(parts[2:])
        return remainder in accepted or remainder in {"productSpecs", "dimensions"}
