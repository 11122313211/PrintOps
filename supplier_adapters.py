"""Provider-neutral supplier adapter contracts.

Adapters only map the standard order and prepare an explicit request payload.
They do not perform network writes; a real provider integration can replace an
adapter without changing the Agent workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


@dataclass(frozen=True)
class SupplierAdapter:
    platform_id: str
    name: str
    mode: str = "export"
    field_map: Mapping[str, str] = field(default_factory=dict)

    def map_order(self, order: Mapping[str, Any]) -> dict[str, Any]:
        """Map standard order fields to provider field names, omitting blanks."""
        mapped: dict[str, Any] = {}
        for source, target in self.field_map.items():
            value = order.get(source)
            if value not in (None, "", {}, []):
                mapped[target] = value
        return mapped

    def prepare_quote_request(self, order: Mapping[str, Any], capability: Mapping[str, Any]) -> dict[str, Any]:
        """Prepare a manual-confirmation request; never call a provider."""
        return {
            "requestId": f"quote-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            "status": "awaiting_human_confirmation",
            "platformId": self.platform_id,
            "platform": self.name,
            "adapterMode": self.mode,
            "mappedOrder": self.map_order(order),
            "capabilityStatus": capability.get("status", "review"),
            "requiresHumanConfirmation": True,
            "message": "已准备询价请求，尚未向供应商发送；确认后再由对应平台适配器提交。",
        }


DEFAULT_FIELD_MAP = {
    "productType": "productType", "quantity": "quantity", "quantityValue": "quantityValue",
    "quantityUnit": "quantityUnit", "size": "size", "pages": "pages", "orientation": "orientation",
    "paper": "paper", "printing": "printing", "finishing": "finishing", "binding": "binding",
    "deadline": "deadline", "budget": "budget", "productSpecs": "productSpecs",
}

ADAPTERS: dict[str, SupplierAdapter] = {
    "generic": SupplierAdapter("generic", "通用印刷平台", "export", DEFAULT_FIELD_MAP),
    "shengda": SupplierAdapter("shengda", "盛大印刷", "manual", DEFAULT_FIELD_MAP),
    "platform_a": SupplierAdapter("platform_a", "平台 A", "adapter", DEFAULT_FIELD_MAP),
    "platform_b": SupplierAdapter("platform_b", "平台 B", "adapter", DEFAULT_FIELD_MAP),
    "supplier": SupplierAdapter("supplier", "自定义供应商", "adapter", DEFAULT_FIELD_MAP),
}


def get_adapter(platform_id: str | None) -> SupplierAdapter:
    return ADAPTERS.get(platform_id or "generic", ADAPTERS["generic"])
