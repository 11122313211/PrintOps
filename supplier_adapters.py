"""Compatibility field adapters for the provider-neutral connector registry.

Bundled provider names are export presets, not authorized supplier integrations.
All dimensional fields use the shared unit normalizer before leaving the order
boundary. No adapter performs a network operation.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import uuid4

from supplier_contract import CONTRACT_VERSION, UnknownProviderError

SUPPLIER_PROFILE_VERSION = "2026.10.15"


def invalid_export_dimensions(order: Mapping[str, Any]) -> list[str]:
    """Reject ambiguous physical units before a draft is called exportable."""
    from unit_normalizer import dimension_values
    values = {"size": order.get("size")}
    for key, value in (order.get("dimensions") or {}).items():
        values[f"dimensions.{key}"] = value
    specs = order.get("productSpecs") or {}
    for key in ("boxSize", "boxSizeInner", "boxSizeOuter", "bagSize", "finishedSize", "expandedSize",
                "dieCutSize", "packageSize", "packageInnerSize", "packageOuterSize"):
        values[f"productSpecs.{key}"] = specs.get(key)
    for key, value in (order.get("measurements") or {}).items():
        if isinstance(value, dict):
            axes = [value[axis] for axis in ("width", "height", "depth") if value.get(axis) is not None]
            values[f"measurements.{key}"] = "×".join(str(axis) for axis in axes) + " " + str(value.get("unit") or "")
    invalid = [key for key, value in values.items() if value and dimension_values(value) is None]
    for index, item in enumerate(order.get("items") or []):
        if isinstance(item, dict):
            invalid.extend(f"items.{item.get('itemId') or index}.{key}" for key in invalid_export_dimensions(item))
    return invalid


def canonical_export_order(order: Mapping[str, Any]) -> dict[str, Any]:
    from unit_normalizer import normalize_dimension, normalize_length
    from measurement_model import parse_measurement, SEMANTICS

    result = deepcopy(dict(order))
    if result.get("size"):
        result["size"] = normalize_dimension(result["size"])
    dimensions = result.get("dimensions")
    if isinstance(dimensions, dict):
        result["dimensions"] = {key: normalize_dimension(value) if value else value
                                for key, value in dimensions.items()}
    specs = result.get("productSpecs")
    if isinstance(specs, dict):
        dimension_keys = ("boxSize", "boxSizeInner", "boxSizeOuter", "bagSize", "finishedSize",
                          "expandedSize", "dieCutSize", "packageSize", "packageInnerSize", "packageOuterSize")
        for key, value in specs.items():
            if isinstance(value, str) and key not in dimension_keys:
                specs[key] = normalize_length(value)
        for key in dimension_keys:
            if specs.get(key):
                specs[key] = normalize_dimension(specs[key])
    # Regenerate structured measurements from the normalized fields, instead of
    # trusting a client-supplied unit label over incompatible numeric values.
    measurements: dict[str, Any] = {}
    for key, value in (result.get("dimensions") or {}).items():
        if value:
            parsed = parse_measurement(value, SEMANTICS.get(key, key))
            if parsed:
                measurements[key] = parsed
    if result.get("size") and not measurements.get("finishedSize"):
        parsed = parse_measurement(result["size"], "finished")
        if parsed:
            measurements["finishedSize"] = parsed
    # Structured-only orders are normalized by the same shared parser. Never
    # silently relabel numeric cm values as mm or lose the supplied dimension.
    for key, value in (result.get("measurements") or {}).items():
        if key not in measurements and isinstance(value, dict):
            axes = [value[axis] for axis in ("width", "height", "depth") if value.get(axis) is not None]
            text = "×".join(str(axis) for axis in axes) + " " + str(value.get("unit") or "")
            parsed = parse_measurement(text, SEMANTICS.get(key, key))
            if parsed:
                measurements[key] = parsed
    if measurements:
        result["measurements"] = measurements
    else:
        result.pop("measurements", None)
    if isinstance(result.get("items"), list):
        result["items"] = [canonical_export_order(item) for item in result["items"] if isinstance(item, dict)]
    result["unitSystem"] = "mm"
    return result


@dataclass(frozen=True)
class SupplierAdapter:
    platform_id: str
    name: str
    mode: str = "export_only"
    field_map: Mapping[str, Any] = field(default_factory=dict)

    def map_order(self, order: Mapping[str, Any]) -> dict[str, Any]:
        canonical = canonical_export_order(order)
        merged = dict(self.field_map.get("*") or {})
        overlay = self.field_map.get(str(canonical.get("productType") or ""))
        if isinstance(overlay, Mapping):
            merged.update(overlay)
        mapped: dict[str, Any] = {}
        for source, target in merged.items():
            value: Any = canonical
            for part in source.split("."):
                value = value.get(part) if isinstance(value, Mapping) else None
            if value not in (None, "", {}, []):
                mapped[target] = deepcopy(value)
        return mapped

    def prepare_quote_request(self, order: Mapping[str, Any], capability: Mapping[str, Any]) -> dict[str, Any]:
        # Compatibility callers still get an unsent, reviewable local draft.
        invalid = invalid_export_dimensions(order)
        if invalid:
            return {"status": "blocked", "reason": "invalid_measurement", "invalidFields": invalid,
                    "providerId": self.platform_id, "sent": False, "requiresHumanConfirmation": True}
        metadata = platform_metadata(self.platform_id)
        return {
            "requestId": f"quote-{uuid4().hex}",
            "status": "awaiting_human_confirmation", "providerId": self.platform_id,
            "platformId": self.platform_id, "platform": self.name,
            "adapterMode": self.mode, "mode": self.mode,
            "mappedOrder": self.map_order(order), "unitSystem": "mm",
            "capabilityStatus": capability.get("status", "review"),
            "connectionStatus": metadata["connectionStatus"],
            "verified": False, "networkEnabled": False, "sent": False,
            "source": metadata["source"], "updatedAt": None, "expiresAt": None,
            "requiresHumanConfirmation": True,
            "message": "已生成本地询价草稿，尚未发送。供应商生产能力、价格和交期需另行确认。",
        }


DEFAULT_FIELD_MAP = {
    "productType": "productType", "quantity": "quantity", "quantityValue": "quantityValue",
    "quantityUnit": "quantityUnit", "size": "size", "pages": "pages", "orientation": "orientation",
    "paper": "paper", "printing": "printing", "finishing": "finishing", "binding": "binding",
    "deadline": "deadline", "budget": "budget", "productSpecs": "productSpecs",
    "dimensions": "dimensions", "measurements": "measurements", "unitSystem": "unitSystem",
    "items": "items",
}

# Unverified local mapping examples, NOT Shengda's API field specification.
SHENGDA_FIELD_MAP = {
    "*": DEFAULT_FIELD_MAP,
    "名片": {"productSpecs.cardStock": "material"},
    "包装盒": {"productSpecs.boxSize": "boxDimension", "productSpecs.boxStructure": "boxStyle"},
}
ADAPTERS: dict[str, SupplierAdapter] = {
    "generic": SupplierAdapter("generic", "通用印刷平台", field_map={"*": DEFAULT_FIELD_MAP}),
    "shengda": SupplierAdapter("shengda", "盛大印刷", field_map=SHENGDA_FIELD_MAP),
    "platform_a": SupplierAdapter("platform_a", "平台 A", field_map={"*": DEFAULT_FIELD_MAP}),
    "platform_b": SupplierAdapter("platform_b", "平台 B", field_map={"*": DEFAULT_FIELD_MAP}),
    "supplier": SupplierAdapter("supplier", "自定义供应商", field_map={"*": DEFAULT_FIELD_MAP}),
}


def get_adapter(platform_id: str | None) -> SupplierAdapter:
    selected = platform_id or "generic"
    if selected not in ADAPTERS:
        raise UnknownProviderError(f"Unknown supplier provider: {selected}")
    return ADAPTERS[selected]


def platform_metadata(platform_id: str) -> dict[str, Any]:
    adapter = get_adapter(platform_id)
    return {
        "providerId": platform_id, "name": adapter.name, "displayName": adapter.name,
        "mode": "export_only", "connectionStatus": "local" if platform_id == "generic" else "unconfigured",
        "contractVersion": CONTRACT_VERSION, "unitSystem": "mm", "authType": "none",
        "capabilities": ["quote_draft", "order_draft"], "verified": False,
        "networkEnabled": False, "requiresHumanConfirmation": True,
        "source": {"kind": "local_export_template", "description": "本地字段映射模板；未验证供应商生产能力或 API 字段"},
        "updatedAt": None, "expiresAt": None,
        "supplierProfile": {"categories": [], "maxSize": None, "papers": [], "finishing": [], "leadTime": None},
    }


# Kept for old imports. ConnectorRegistry also updates this view on registration.
PLATFORMS = {provider_id: platform_metadata(provider_id) for provider_id in ADAPTERS}


def list_platforms() -> list[dict[str, Any]]:
    from supplier_connectors import REGISTRY
    return [{"id": entry["providerId"], **entry, "supplierProfileVersion": SUPPLIER_PROFILE_VERSION}
            for entry in REGISTRY.list_metadata()]


def __getattr__(name: str) -> Any:
    # Lazy compatibility alias avoids a circular import during registry startup.
    if name == "REGISTRY":
        from supplier_connectors import REGISTRY
        return REGISTRY
    raise AttributeError(name)
