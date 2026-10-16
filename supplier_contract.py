"""Supplier extension boundary; no network implementations or provider secrets.

Applications register trusted connector instances at startup. Connector payloads
contain canonical orders (mm), never planner-selected URLs or Python modules.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, runtime_checkable

CONTRACT_VERSION = "1.0"
MODES = frozenset({"export_only", "sandbox", "live"})
OPERATIONS = frozenset({"quote_draft", "get_quote", "order_draft", "submit_order", "cancel"})


class UnknownProviderError(ValueError):
    """An explicit provider selection must never silently choose another one."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def order_fingerprint(order: Mapping[str, Any]) -> str:
    serialized = json.dumps(dict(order), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class HumanConfirmation:
    """Created by an authenticated application confirmation handler, not a model.

    No HTTP/MCP endpoint in PrintOps exposes live operations. A future host must
    verify the human actor before constructing this value; a boolean from a
    request or a model must not be converted into confirmation.
    """

    provider_id: str
    order_revision: int
    order_fingerprint: str
    actor: str
    confirmed_at: str
    expires_at: str
    action: str = "submit_order"
    request_id: str | None = None


@dataclass(frozen=True)
class RequestContext:
    revision: int = 0
    idempotency_key: str | None = None
    confirmation: HumanConfirmation | None = None


@dataclass(frozen=True)
class TransportPolicy:
    timeout_seconds: float = 5.0
    deadline_seconds: float = 15.0
    max_attempts: int = 2
    max_in_flight: int = 4

    def __post_init__(self) -> None:
        if not 0 < self.timeout_seconds <= self.deadline_seconds <= 120:
            raise ValueError("Require 0 < timeout <= deadline <= 120 seconds")
        if not 1 <= self.max_attempts <= 3 or not 1 <= self.max_in_flight <= 16:
            raise ValueError("Attempts must be 1..3; in-flight requests must be 1..16")


@runtime_checkable
class SupplierConnector(Protocol):
    def metadata(self) -> dict[str, Any]: ...
    def health(self) -> dict[str, Any]: ...
    def capabilities(self) -> dict[str, Any]: ...
    def create_quote_draft(self, order: Mapping[str, Any], context: RequestContext | None = None) -> dict[str, Any]: ...
    def get_quote(self, request_id: str, context: RequestContext | None = None) -> dict[str, Any]: ...
    def create_order_draft(self, order: Mapping[str, Any], context: RequestContext | None = None) -> dict[str, Any]: ...
    def submit_order(self, order: Mapping[str, Any], context: RequestContext | None = None) -> dict[str, Any]: ...
    def cancel(self, request_id: str, context: RequestContext | None = None) -> dict[str, Any]: ...


class SupplierTransport(Protocol):
    def execute(self, operation: str, payload: Mapping[str, Any], *, timeout: float,
                idempotency_key: str | None, cancel_event: Any) -> dict[str, Any]: ...


class RequestJournal(Protocol):
    """Implementations must atomically claim keys and never overwrite a claim."""
    durable: bool
    def claim(self, key: str, fingerprint: str, record: dict[str, Any]) -> tuple[bool, dict[str, Any]]: ...
    def finish(self, key: str, result: dict[str, Any]) -> None: ...
    def get(self, key: str) -> dict[str, Any] | None: ...
    def append_audit(self, event: dict[str, Any]) -> None: ...
