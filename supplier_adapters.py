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
    # Two levels: "*" holds the common mapping and an optional per-productType
    # overlay renames category-specific fields for a real provider.
    field_map: Mapping[str, Any] = field(default_factory=dict)

    def map_order(self, order: Mapping[str, Any]) -> dict[str, Any]:
        """Map standard order fields to provider field names, omitting blanks.

        A ``productSpecs.<key>`` source reads one category parameter out of
        the nested spec object so a provider can give it a dedicated field.
        """
        product_type = str(order.get("productType") or "")
        overlay = self.field_map.get(product_type) if product_type else None
        merged = dict(self.field_map.get("*") or {})
        if isinstance(overlay, Mapping):
            merged.update(overlay)
        specs = order.get("productSpecs") if isinstance(order.get("productSpecs"), Mapping) else {}
        mapped: dict[str, Any] = {}
        for source, target in merged.items():
            if source.startswith("productSpecs."):
                value = specs.get(source.split(".", 1)[1])
            else:
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

# Example per-category overlays.  They document the structure a real platform
# integration is expected to fill in: category parameters live inside
# productSpecs until a live adapter lifts them into dedicated provider fields.
SHENGDA_FIELD_MAP = {
    "*": DEFAULT_FIELD_MAP,
    "名片": {"productSpecs.cardStock": "material"},
    "包装盒": {"productSpecs.boxSize": "boxDimension", "productSpecs.boxStructure": "boxStyle"},
}

ADAPTERS: dict[str, SupplierAdapter] = {
    "generic": SupplierAdapter("generic", "通用印刷平台", "export", {"*": DEFAULT_FIELD_MAP}),
    "shengda": SupplierAdapter("shengda", "盛大印刷", "manual", SHENGDA_FIELD_MAP),
    "platform_a": SupplierAdapter("platform_a", "平台 A", "adapter", {"*": DEFAULT_FIELD_MAP}),
    "platform_b": SupplierAdapter("platform_b", "平台 B", "adapter", {"*": DEFAULT_FIELD_MAP}),
    "supplier": SupplierAdapter("supplier", "自定义供应商", "adapter", {"*": DEFAULT_FIELD_MAP}),
}


def get_adapter(platform_id: str | None) -> SupplierAdapter:
    return ADAPTERS.get(platform_id or "generic", ADAPTERS["generic"])


PLATFORMS = {
    "generic": {"name": "通用印刷平台", "mode": "export", "capabilities": ["标准订单导出"],
                "supplierProfile": {"categories": ["全品类"], "maxSize": "按供应商确认", "papers": ["按供应商确认"], "finishing": ["按供应商确认"], "leadTime": "按供应商确认"}},
    "shengda": {"name": "盛大印刷", "mode": "manual", "capabilities": ["订单草稿", "人工询价"],
                "supplierProfile": {"categories": ["名片", "单页", "折页", "宣传册", "画册", "标签", "包装盒"], "maxSize": "1200×900mm", "papers": ["铜版纸", "哑粉纸", "白卡纸", "牛皮纸"], "finishing": ["覆膜", "哑膜", "亮膜", "烫金", "局部UV"], "leadTime": "1-5 天"}},
    "platform_a": {"name": "平台 A", "mode": "adapter", "capabilities": ["字段映射", "订单草稿"],
                   "supplierProfile": {"categories": ["名片", "单页", "折页", "宣传册", "画册"], "maxSize": "A3+", "papers": ["铜版纸", "哑粉纸"], "finishing": ["覆膜", "哑膜", "亮膜"], "leadTime": "2-6 天"}},
    "platform_b": {"name": "平台 B", "mode": "adapter", "capabilities": ["字段映射", "订单草稿"],
                   "supplierProfile": {"categories": ["名片", "标签", "手提袋", "包装盒"], "maxSize": "900×600mm", "papers": ["白卡纸", "牛皮纸", "不干胶"], "finishing": ["烫金", "击凸", "局部UV"], "leadTime": "3-7 天"}},
    "supplier": {"name": "自定义供应商", "mode": "adapter", "capabilities": ["字段映射"],
                 "supplierProfile": {"categories": ["待补充"], "maxSize": "待补充", "papers": ["待补充"], "finishing": ["待补充"], "leadTime": "待补充"}},
}
SUPPLIER_PROFILE_VERSION = "2026.09.04"
