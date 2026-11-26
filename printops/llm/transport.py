"""HTTP transport, SSRF host validation, redirect blocking, and retry backoff."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import socket
import time
from typing import Any
import urllib.error
from urllib.error import HTTPError, URLError
import urllib.parse
from urllib.parse import urlparse
import urllib.request

from printops.runtime.deadline import (
    DeadlineExceeded,
    bounded_io,
    remaining,
)

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """TD-27 (2026-11-22 security audit L1): never follow 3xx from gateways.

    urllib's default redirect handler re-sends the Authorization header to
    whatever host Location points at, and the private-host validation only
    ever saw the original URL — a compromised gateway could exfiltrate the
    API key or turn the client into an internal network oracle. Returning
    None makes every 3xx surface as HTTPError, which the existing call sites
    already treat as a hard failure (3xx is not in RETRYABLE_HTTP_CODES).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

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


# Install process-wide non-redirecting opener (TD-27).
urllib.request.install_opener(urllib.request.build_opener(_NoRedirect()))


class TransportMixin:
    """Bounded HTTP JSON transport and backoff helpers for the LLM planner."""

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
    def _retry_backoff_seconds(cls, error: Exception | None, attempt: int) -> float:
        """TD-12: deterministic per-class backoff before a retry attempt."""
        if isinstance(error, HTTPError) and error.code == 429:
            return cls.RETRY_BACKOFF_RATE_LIMIT_SECONDS * (2 ** attempt)
        if isinstance(error, HTTPError):
            return cls.RETRY_BACKOFF_SERVER_SECONDS * (attempt + 1)
        return cls.RETRY_BACKOFF_NETWORK_SECONDS * (2 ** attempt)

    @staticmethod
    def _latency(started: float) -> int:
        return round((time.monotonic() - started) * 1000)
