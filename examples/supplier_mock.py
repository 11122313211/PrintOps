#!/usr/bin/env python3
"""Runnable neutral supplier extension; all data and responses are synthetic."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from supplier_adapters import SupplierAdapter
from supplier_connectors import ConnectorRegistry, MockConnector
from supplier_contract import RequestContext
import stdio_utf8


class ExampleSupplierConnector(MockConnector):
    """Change field mapping without modifying order, planner, HTTP or MCP code."""
    def __init__(self, provider_id: str = "example_local_press") -> None:
        super().__init__(provider_id)
        self.adapter = SupplierAdapter(provider_id, "示例印厂（本地模拟）", mode="sandbox", field_map={
            "*": {"productType": "product_name", "quantityValue": "quantity", "quantityUnit": "quantity_unit",
                  "size": "finished_size_text", "measurements.finishedSize": "finished_size",
                  "paper": "stock", "printing": "ink_sides", "finishing": "finishes",
                  "binding": "binding", "unitSystem": "unit_system"},
            "包装盒": {"measurements.dieCutSize": "dieline_size", "productSpecs.boxStructure": "structure"},
        })
        self._metadata.update({"name": self.adapter.name, "displayName": self.adapter.name,
                               "source": {"kind": "mock", "description": "接入教程合成数据，不代表任何真实印厂"}})

    def create_quote_draft(self, order: Mapping[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        return deepcopy(super().create_quote_draft(order, context))

    def create_order_draft(self, order: Mapping[str, Any], context: RequestContext | None = None) -> dict[str, Any]:
        result = self.create_quote_draft(order, context)
        if result.get("status") == "blocked":
            return result
        result["type"] = "order_draft"
        with self.lock:
            self.requests[result["requestId"]] = deepcopy(result)
        return result


def register_example(registry: ConnectorRegistry) -> ExampleSupplierConnector:
    connector = ExampleSupplierConnector()
    registry.register(connector)
    return connector


def demo() -> dict[str, Any]:
    registry = ConnectorRegistry()  # Does not change bundled provider configuration.
    connector = register_example(registry)
    order = {"productType": "名片", "quantityValue": 500, "quantityUnit": "张", "size": "90×54 mm",
             "paper": "300 g/m² 铜版纸", "printing": "双面四色", "finishing": "哑膜"}
    quote = connector.create_quote_draft(order, RequestContext(revision=4))
    order_draft = connector.create_order_draft(order, RequestContext(revision=4))
    return {"synthetic": True, "metadata": connector.metadata(), "health": connector.health(),
            "capabilities": connector.capabilities(), "quoteDraft": quote,
            "retrieved": connector.get_quote(quote["requestId"]), "orderDraft": order_draft,
            "cancelled": connector.cancel(quote["requestId"]),
            "repeatCancel": connector.cancel(quote["requestId"]),
            "submit": connector.submit_order(order)}


if __name__ == "__main__":
    stdio_utf8.configure()
    print(json.dumps(demo(), ensure_ascii=False, indent=2))
