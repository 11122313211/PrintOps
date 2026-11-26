"""Compatibility facade for ``printops.agent`` (re-exports legacy ``agent.py`` symbols)."""

from __future__ import annotations

from printops.agent.facade import Agent
from printops.agent.memory import (
    FIELD_SOURCE_LABELS,
    HISTORY_LIMIT,
    ITEM_PATCH_KEYS,
    MAX_PATCH_VALUE,
    MAX_PLANNER_LEDGER_BYTES,
    MAX_PLANNER_LEDGER_ENTRIES,
    MAX_PLANNER_REQUIRED_CALLS,
    MAX_PLANNER_TOOL_ROUNDS,
    MAX_QUOTE_REQUESTS,
    MAX_RUN_EVENTS,
    MAX_RUN_HISTORY,
    MAX_TOOL_FILE_NAME_LENGTH,
    MAX_TOOL_INSPECTION_BYTES,
    MAX_TOOL_NAME_LENGTH,
    MAX_TOOL_ORDER_BYTES,
    MAX_TOOL_SAFE_INTEGER,
    Memory,
    ORDER_PATCH_KEYS,
    PLANNER_FAILURE_STATUSES,
    PLANNER_RETRYABLE_FAILURE_STATUSES,
    QUOTE_ACTIVE_STATUSES,
    RevisionConflictError,
    WORKFLOW_LABELS,
)
from printops.domain.knowledge import KNOWLEDGE_MANIFEST, KNOWLEDGE_VERSION
from printops.domain.nlu import perceive
from printops.domain.order_model import (
    DEFAULT_QUANTITY_UNITS,
    DIMENSION_DEFAULTS,
    DIMENSION_SPEC_ALIASES,
    ITEM_DEFAULTS,
    LABELS,
    MATERIAL_SPEC_PRODUCTS,
    MAX_STATE_REVISION,
    ORDER_DEFAULTS,
    QUANTITY_UNIT_ALIASES,
    RECOMMENDATION_FIELDS,
    REQUIRED,
    STATE_SCHEMA_VERSION,
    default_quantity_unit,
    normalize_state,
    parse_quantity,
    required_order_keys,
)
from printops.supplier.adapters import (
    ADAPTERS,
    PLATFORMS,
    SUPPLIER_PROFILE_VERSION,
    SupplierAdapter,
    get_adapter,
)
from printops.supplier.connectors import REGISTRY
from printops.supplier.contract import UnknownProviderError
from printops.tools.registry import (
    TOOLS,
    TOOL_META,
    TOOL_SCHEMAS,
    estimate_price,
    explain_print_term,
    match_supplier_capability,
    preflight_file,
    prepare_handoff,
    recommend_processes,
    request_supplier_quote,
    validate_order,
)
