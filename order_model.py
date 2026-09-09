"""Order data contracts: fields, quantity/dimension normalization, migrations.

Split from agent.py so the order model can evolve (schemaVersion, new
dimension semantics) without touching perception, tools, or the workflow.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from copy import deepcopy
from typing import Any


DIMENSION_DEFAULTS = {
    "finishedSize": "", "expandedSize": "", "dieCutSize": "", "packageSize": "",
}
PACKAGE_DIMENSION_PRODUCTS = {"包装盒", "手提袋"}
ORDER_DEFAULTS = {
    "productType": "", "productTypes": [], "items": [], "purpose": "", "quantity": "", "quantityValue": None, "quantityUnit": "", "size": "",
    "dimensions": deepcopy(DIMENSION_DEFAULTS),
    "pages": "", "orientation": "", "paper": "", "printing": "", "finishing": "", "binding": "",
    "deadline": "", "budget": "", "platform": "generic", "productSpecs": {},
}
ITEM_DEFAULTS = {
    "itemId": "", "productType": "", "purpose": "", "quantity": "", "quantityValue": None,
    "quantityUnit": "", "size": "", "dimensions": deepcopy(DIMENSION_DEFAULTS), "pages": "", "orientation": "", "paper": "",
    "printing": "", "finishing": "", "binding": "", "deadline": "", "budget": "",
    "productSpecs": {}, "selectedOption": None, "orderGenerated": False, "uploadedFile": None,
}
# Keep item identity compatible with the MCP selector contract.  Generated
# IDs use this same bound so a repaired session can always be addressed by a
# transport client without another lossy conversion.
MAX_ITEM_ID_LENGTH = 128
REQUIRED = ["productType", "quantity", "size", "paper", "printing", "deadline"]
RECOMMENDATION_FIELDS = {"productType", "productTypes", "items", "purpose", "quantity", "quantityValue", "quantityUnit", "size", "dimensions", "pages", "orientation", "paper", "printing", "finishing", "binding", "deadline", "budget", "productSpecs"}
LABELS = {
    "productType": "印刷品", "purpose": "使用场景", "quantity": "数量",
    "size": "成品尺寸", "pages": "页数", "orientation": "版式方向", "paper": "纸张/材料", "printing": "印刷颜色",
    "finishing": "表面工艺", "binding": "装订/后道", "deadline": "交期",
    "budget": "预算偏好", "platform": "目标平台",
}
DIMENSION_LABELS = {
    "finishedSize": "成品尺寸", "expandedSize": "展开尺寸",
    "dieCutSize": "刀模尺寸", "packageSize": "包装三维尺寸",
}
MATERIAL_SPEC_PRODUCTS = {"标签", "手提袋", "纸杯", "海报", "喷画", "PVC", "PVC卡"}


def _legacy_text(value: Any) -> str:
    """Convert a legacy scalar to display text without stringifying containers."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()[:4096]
    if isinstance(value, int):
        try:
            return str(value)[:4096]
        except (OverflowError, ValueError):
            # Python can reject conversion of an integer with more than the
            # interpreter's configured decimal-digit limit.
            return ""
    if isinstance(value, float):
        return str(value).strip()[:4096] if math.isfinite(value) else ""
    return ""


def _is_legacy_scalar(value: Any) -> bool:
    """Return whether a value is safe to use as a text-like order field."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return False
    return not isinstance(value, float) or math.isfinite(value)


def _legacy_number(value: Any) -> int | float | None:
    """Keep only finite numeric values for structured quantity fields."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _normalize_legacy_specs(value: Any) -> dict[str, str]:
    """Keep legacy product-spec maps scalar and bounded at the state boundary."""
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip()
        text = _legacy_text(raw_value)
        if name and text:
            normalized[name[:256]] = text[:4096]
    return normalized


def quote_idempotency_key(order: dict[str, Any], platform_id: str, item_id: str | None = None) -> str:
    """Build a stable key from the order data that affects a supplier quote."""
    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): normalize(value[key]) for key in sorted(value)}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, str):
            return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).strip())
        return value

    payload = {
        "platformId": str(platform_id or "generic"),
        "itemId": str(item_id) if item_id else None,
        "order": {key: normalize(order.get(key)) for key in sorted(RECOMMENDATION_FIELDS) if key != "items"},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"quote:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


QUANTITY_MULTIPLIERS = {"万": 10000, "千": 1000, "百": 100}
QUANTITY_UNIT_ALIASES = {
    "份": "份", "本": "本", "册": "册", "张": "张", "个": "个", "件": "件", "盒": "盒",
    "套": "套", "包": "包", "箱": "箱", "杯": "杯", "块": "块", "枚": "枚",
    "平方米": "平方米", "平米": "平方米", "㎡": "平方米",
}
QUANTITY_CAPTURE = (
    r"(\d[\d,]*(?:\.\d+)?)\s*(?:(万|千|百)\s*)?"
    r"(平方米|平米|㎡|份|本|册|张|个|件|盒|套|包|箱|杯|块|枚)?"
)
DEFAULT_QUANTITY_UNITS = {
    "名片": "张", "单页": "张", "折页": "张", "标签": "张", "吊牌": "张", "海报": "张",
    "喷画": "张", "PVC": "块", "PVC卡": "张", "包装盒": "个", "手提袋": "个", "纸杯": "个",
    "信封封套": "个", "宣传册": "本", "画册": "本", "联单": "本", "数码印刷": "份",
}


def default_quantity_unit(product: str | None) -> str:
    """Choose a display unit only when the user did not provide one."""
    return DEFAULT_QUANTITY_UNITS.get(product or "", "份")


def parse_quantity(value: Any, product: str | None = None, unit_hint: str = "") -> tuple[str, int | float, str] | None:
    """Return a stable display value, numeric value and unit for an order quantity."""
    # Do not stringify dictionaries/lists supplied by a malformed model or
    # persisted session.  A container such as ``{"count": 500}`` must not
    # become a valid quantity merely because its repr contains digits.
    if value is None or value == "" or not _is_legacy_scalar(value):
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    match = re.search(
        r"(?<![A-Za-z0-9])([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:(万|千|百)\s*)?"
        r"(平方米|平米|㎡|份|本|册|张|个|件|盒|套|包|箱|杯|块|枚)?",
        text,
    )
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    count = number * QUANTITY_MULTIPLIERS.get(match.group(2) or "", 1)
    if not math.isfinite(count):
        return None
    # An explicit unit in the display text is authoritative; the hint is for
    # numeric-only patches and legacy records that lack a unit.
    safe_unit_hint = _legacy_text(unit_hint) if _is_legacy_scalar(unit_hint) else ""
    raw_unit = match.group(3) or safe_unit_hint or ""
    unit = QUANTITY_UNIT_ALIASES.get(raw_unit, raw_unit) or default_quantity_unit(product)
    numeric: int | float = int(count) if count.is_integer() else round(count, 3)
    count_text = str(numeric)
    return f"{count_text} {unit}", numeric, unit


def normalize_order_quantity(order: dict[str, Any]) -> None:
    """Migrate old sessions and keep quantity display/number/unit in sync."""
    raw_quantity = order.get("quantity")
    safe_quantity = _legacy_text(raw_quantity) if _is_legacy_scalar(raw_quantity) else ""
    safe_unit = _legacy_text(order.get("quantityUnit")) if _is_legacy_scalar(order.get("quantityUnit")) else ""
    parsed = parse_quantity(safe_quantity, order.get("productType"), safe_unit)
    if not parsed:
        # Preserve an empty quantity while sanitizing its structured siblings;
        # malformed containers are cleared instead of being retained in state.
        order["quantity"] = safe_quantity
        order["quantityUnit"] = safe_unit
        order["quantityValue"] = _legacy_number(order.get("quantityValue"))
        if raw_quantity not in (None, "") and not safe_quantity:
            order["quantityValue"] = None
            order["quantityUnit"] = ""
        return
    display, numeric, unit = parsed
    order["quantity"] = display
    order["quantityValue"] = numeric
    order["quantityUnit"] = unit


def _is_three_dimensional_size(value: Any) -> bool:
    """Identify a structural L×W×H value without guessing its unit."""
    return len(re.findall(r"×", unicodedata.normalize("NFKC", str(value or "")))) >= 2


def normalize_order_dimensions(order: dict[str, Any]) -> None:
    """Keep explicit dimension meanings while migrating legacy ``size`` data."""
    raw = order.get("dimensions") if isinstance(order.get("dimensions"), dict) else {}
    dimensions = deepcopy(DIMENSION_DEFAULTS)
    for key in DIMENSION_DEFAULTS:
        value = _legacy_text(raw.get(key))
        if value:
            dimensions[key] = value

    specs = _normalize_legacy_specs(order.get("productSpecs"))
    # Capture aliases before the cleanup below. Legacy clients stored
    # finished/expanded/die-cut/package meanings under ``productSpecs``; once
    # removed from that map they must still be copied into canonical
    # ``dimensions`` fields.
    legacy_dimension_aliases = {
        key: specs.get(key) for key in DIMENSION_DEFAULTS
    }
    # Dimension meanings have a single canonical home. Remove aliases that
    # may have been written by an older client or an unconstrained model.
    order["productSpecs"] = {key: value for key, value in specs.items() if key not in DIMENSION_DEFAULTS}
    specs = order["productSpecs"]
    for key, value in legacy_dimension_aliases.items():
        if not dimensions[key] and value:
            dimensions[key] = value
    if not dimensions["packageSize"]:
        dimensions["packageSize"] = _legacy_text(specs.get("boxSize") or specs.get("bagSize"))

    legacy_size = _legacy_text(order.get("size"))
    # Keep the legacy compatibility field scalar as well; otherwise a
    # malformed persisted object could survive beside the canonical dimension.
    order["size"] = legacy_size
    if legacy_size:
        if _is_three_dimensional_size(legacy_size) or order.get("productType") in PACKAGE_DIMENSION_PRODUCTS:
            if not dimensions["packageSize"]:
                dimensions["packageSize"] = legacy_size
        elif not dimensions["finishedSize"]:
            dimensions["finishedSize"] = legacy_size
    order["dimensions"] = dimensions


def merge_dimension_patch(current: dict[str, Any] | None, patch: dict[str, Any]) -> dict[str, str]:
    """Merge only the four dimension meanings accepted by the order model."""
    dimensions = deepcopy(DIMENSION_DEFAULTS)
    if isinstance(current, dict):
        dimensions.update({key: str(current.get(key) or "").strip() for key in DIMENSION_DEFAULTS})
    for key, value in patch.items():
        if key in DIMENSION_DEFAULTS:
            dimensions[key] = str(value).strip() if value is not None else ""
    return dimensions


def migrate_dimension_field_meta(state: dict[str, Any]) -> None:
    """Move legacy product-spec provenance keys to the canonical dimensions path."""
    metadata = state.get("fieldMeta") if isinstance(state.get("fieldMeta"), dict) else {}
    for field in list(metadata):
        if not isinstance(field, str):
            continue
        parts = field.split(".")
        target = ""
        if len(parts) == 2 and parts[0] == "productSpecs" and parts[1] in DIMENSION_DEFAULTS:
            target = f"dimensions.{parts[1]}"
        elif field.startswith("items."):
            # Item IDs are allowed to contain dots.  Match the canonical
            # suffix instead of assuming the ID is a single dot-delimited
            # token (the old migration did that and silently skipped aliases).
            for dimension in DIMENSION_DEFAULTS:
                suffix = f".productSpecs.{dimension}"
                if field.endswith(suffix):
                    item_id = field[len("items."):-len(suffix)]
                    if item_id:
                        target = f"items.{item_id}.dimensions.{dimension}"
                    break
        if not target:
            continue
        if target not in metadata:
            metadata[target] = metadata[field]
        metadata.pop(field, None)
    state["fieldMeta"] = metadata


def _item_id_text(value: Any) -> str:
    """Return a bounded text candidate without coercing containers."""
    return _legacy_text(value) if _is_legacy_scalar(value) else ""


def _generated_item_id(index: int, used: set[str], reserved: set[str]) -> str:
    """Generate a deterministic, bounded ID that cannot collide with peers."""
    ordinal = index + 1
    base = f"item-{ordinal}"
    # A pathological list can make the decimal ordinal exceed the transport
    # bound.  A short digest keeps the fallback deterministic in that case.
    if len(base) > MAX_ITEM_ID_LENGTH:
        digest = hashlib.sha256(str(ordinal).encode("ascii")).hexdigest()[:24]
        base = f"item-{digest}"
    candidate = base
    suffix = 2
    while candidate in used or candidate in reserved:
        suffix_text = str(suffix)
        stem = base[:MAX_ITEM_ID_LENGTH - len(suffix_text) - 1]
        candidate = f"{stem}-{suffix_text}"
        suffix += 1
    return candidate


def normalize_order_items(order: dict[str, Any],
                          id_mapping: list[dict[str, Any]] | None = None) -> None:
    """Normalize multi-product items and repair duplicate/invalid identities.

    ``id_mapping`` is an optional migration ledger populated with one entry per
    retained item.  It lets the state normalizer move provenance, file, quote,
    and hand-off references after an old session's IDs are repaired while the
    public function remains backwards compatible (it still returns ``None``).
    """
    raw_items = order.get("items")
    if not isinstance(raw_items, list):
        order["items"] = []
        if id_mapping is not None:
            id_mapping.clear()
        return

    # Reserve every valid raw ID.  This means repairing an earlier malformed
    # item never steals a later item's already-stable identity, even when that
    # later ID itself appears more than once (the first duplicate still wins).
    candidates: list[tuple[int, dict[str, Any], str]] = []
    counts: dict[str, int] = {}
    for raw_index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            continue
        raw_id = _item_id_text(raw.get("itemId"))
        candidates.append((raw_index, raw, raw_id))
        if raw_id and len(raw_id) <= MAX_ITEM_ID_LENGTH:
            counts[raw_id] = counts.get(raw_id, 0) + 1
    reserved = set(counts)

    text_fields = (
        "productType", "purpose", "quantity", "quantityUnit", "size", "pages",
        "orientation", "paper", "printing", "finishing", "binding", "deadline", "budget",
    )
    normalized: list[dict[str, Any]] = []
    mappings: list[dict[str, Any]] = []
    used: set[str] = set()
    for raw_index, raw, raw_id in candidates:
        item = deepcopy(ITEM_DEFAULTS)
        # Copy only fields with the type expected by the order contract.  A
        # malformed persisted object must not leave a dict/list in a scalar
        # production field or turn into executable text through ``str()``.
        for key in text_fields:
            if key in raw:
                item[key] = _legacy_text(raw[key]) if _is_legacy_scalar(raw[key]) else ""
        if "quantityValue" in raw:
            item["quantityValue"] = _legacy_number(raw.get("quantityValue"))
        if "dimensions" in raw:
            item["dimensions"] = deepcopy(raw["dimensions"]) if isinstance(raw["dimensions"], dict) else {}
        if "selectedOption" in raw:
            selected = raw.get("selectedOption")
            item["selectedOption"] = selected.strip() if isinstance(selected, str) and selected.strip() else None
        if "orderGenerated" in raw:
            item["orderGenerated"] = raw.get("orderGenerated") if isinstance(raw.get("orderGenerated"), bool) else False
        if "uploadedFile" in raw:
            uploaded = raw.get("uploadedFile")
            item["uploadedFile"] = uploaded.strip() if isinstance(uploaded, str) and uploaded.strip() else None

        # Keep the first occurrence of a valid ID.  Empty, overlong, and later
        # duplicate values receive a deterministic fallback that skips all
        # unique IDs reserved above.
        normalized_index = len(normalized)
        if raw_id and len(raw_id) <= MAX_ITEM_ID_LENGTH and raw_id not in used:
            item_id = raw_id
        else:
            item_id = _generated_item_id(normalized_index, used, reserved)
        used.add(item_id)
        item["itemId"] = item_id
        item["productSpecs"] = _normalize_legacy_specs(raw.get("productSpecs"))
        normalize_order_quantity(item)
        normalize_order_dimensions(item)
        normalized.append(item)
        mappings.append({"rawIndex": raw_index, "index": normalized_index,
                         "oldId": raw_id or None, "newId": item_id})
    order["items"] = normalized
    if id_mapping is not None:
        id_mapping.clear()
        id_mapping.extend(mappings)
    product_types = list(dict.fromkeys(str(item["productType"]) for item in normalized if item.get("productType")))
    if len(product_types) > 1:
        order["productTypes"] = product_types
        if not order.get("productType"):
            order["productType"] = product_types[0]


def _item_id_maps(id_mapping: list[dict[str, Any]] | None,
                  previous_items: list[Any] | None = None) -> dict[str, Any]:
    """Build lookup tables used while migrating references to repaired IDs."""
    maps: dict[str, Any] = {
        "byIndex": {}, "byRawIndex": {}, "byOld": {}, "indexByNew": {}, "current": set(),
    }
    for raw in id_mapping or []:
        if not isinstance(raw, dict):
            continue
        new_id = raw.get("newId")
        if not isinstance(new_id, str) or not new_id or len(new_id) > MAX_ITEM_ID_LENGTH:
            continue
        maps["current"].add(new_id)
        index = raw.get("index")
        if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
            maps["byIndex"][index] = new_id
            maps["indexByNew"][new_id] = index
        raw_index = raw.get("rawIndex")
        if isinstance(raw_index, int) and not isinstance(raw_index, bool) and raw_index >= 0:
            maps["byRawIndex"][raw_index] = new_id
        old_id = raw.get("oldId")
        if isinstance(old_id, str) and old_id:
            maps["byOld"].setdefault(old_id, []).append(new_id)
    # A whole-list patch can replace IDs before the target normalizer sees the
    # old references.  Add index-based aliases from the previous live list so
    # fieldMeta/options/files/quotes do not point at a removed key in the same
    # process.  Existing aliases remain as a fallback for IDs not present in
    # the previous list.
    if isinstance(previous_items, list):
        previous_aliases: dict[str, list[str]] = {}
        for index, raw_item in enumerate(previous_items):
            if not isinstance(raw_item, dict):
                continue
            old_id = _item_id_text(raw_item.get("itemId"))
            new_id = maps["byIndex"].get(index)
            if old_id and new_id:
                previous_aliases.setdefault(old_id, []).append(new_id)
        for old_id, aliases in previous_aliases.items():
            existing = maps["byOld"].get(old_id, [])
            maps["byOld"][old_id] = aliases + [item for item in existing if item not in aliases]
    return maps


def _resolve_item_reference(item_id: Any, item_index: Any,
                            maps: dict[str, Any], *, prefer_index: bool = True) -> str | None:
    """Resolve an item reference, preferring an explicit index when present."""
    index = (item_index if isinstance(item_index, int) and not isinstance(item_index, bool)
             and item_index >= 0 else None)
    text = _item_id_text(item_id)
    if prefer_index and index is not None:
        resolved = maps["byIndex"].get(index)
        if resolved is not None:
            return resolved
    if text:
        matches = maps["byOld"].get(text)
        if matches:
            # A legacy duplicate ID is inherently ambiguous.  Keep the first
            # occurrence for ID-only references; callers with itemIndex above
            # still resolve the intended occurrence precisely.
            return matches[0]
        if text in maps["current"] and len(text) <= MAX_ITEM_ID_LENGTH:
            return text
    if not prefer_index and index is not None:
        resolved = maps["byIndex"].get(index)
        if resolved is not None:
            return resolved
    if index is not None:
        return maps["byRawIndex"].get(index)
    return None


def _remap_item_path(value: str, maps: dict[str, Any], context_index: int | None = None) -> str:
    """Rewrite ``items.<itemId>.*`` provenance paths after ID repair."""
    if not isinstance(value, str) or not value.startswith("items."):
        return value
    rest = value[len("items."):]
    # IDs are not required to be punctuation-free.  Longest-prefix matching
    # handles IDs containing dots without mistaking an inner field for the ID.
    for old_id in sorted(maps["byOld"], key=len, reverse=True):
        marker = old_id + "."
        if rest.startswith(marker):
            # A path embeds the ID itself; a surrounding list position is only
            # contextual and must not silently retarget a reordered handoff.
            new_id = _resolve_item_reference(old_id, None, maps, prefer_index=False)
            if new_id:
                return "items." + new_id + rest[len(old_id):]
        elif rest == old_id:
            new_id = _resolve_item_reference(old_id, None, maps, prefer_index=False)
            if new_id:
                return "items." + new_id
    token, separator, suffix = rest.partition(".")
    if token.isdigit() and len(token) <= 20:
        try:
            index = int(token)
        except (OverflowError, ValueError):
            index = -1
        if index >= 0:
            new_id = _resolve_item_reference(None, index, maps)
            if new_id:
                return "items." + new_id + (separator + suffix if separator else "")
    return value


def _remap_field_meta_ids(value: Any, maps: dict[str, Any]) -> Any:
    """Move item-scoped provenance keys while preserving canonical collisions."""
    if not isinstance(value, dict):
        return value
    result: dict[Any, Any] = {}
    for raw_key, entry in value.items():
        key = _remap_item_path(raw_key, maps) if isinstance(raw_key, str) else raw_key
        # If both a repaired canonical key and a legacy alias are present,
        # retain the canonical value rather than letting migration overwrite it.
        if key in result and key != raw_key:
            continue
        result[key] = entry
    return result


def _remap_item_options_ids(value: Any, maps: dict[str, Any]) -> Any:
    """Move the item-options map to repaired IDs before shape validation."""
    if not isinstance(value, dict):
        return value
    result: dict[Any, Any] = {}
    for raw_id, options in value.items():
        new_id = _resolve_item_reference(raw_id, None, maps, prefer_index=False)
        # Once an order has concrete items, an option bucket for an unknown
        # item is stale and must not remain addressable by a future selector.
        if new_id is None and maps["current"]:
            continue
        key = new_id or raw_id
        if key in result and key != raw_id:
            continue
        result[key] = options
    return result


def _remap_nested_item_references(value: Any, maps: dict[str, Any],
                                  context_index: int | None = None) -> Any:
    """Recursively rewrite JSON-shaped handoff/quote/file item references."""
    if isinstance(value, list):
        return [_remap_nested_item_references(item, maps, context_index)
                for item in value]
    if not isinstance(value, dict):
        return value
    explicit_index = (value.get("itemIndex") if isinstance(value.get("itemIndex"), int)
                      and not isinstance(value.get("itemIndex"), bool) else None)
    local_index = explicit_index if explicit_index is not None else context_index
    result: dict[Any, Any] = {}
    for raw_key, raw_value in value.items():
        if raw_key == "itemId":
            # Explicit itemIndex is authoritative.  A positional context from
            # an aggregate list is advisory and must not rewrite a handoff
            # whose item IDs were intentionally reordered.
            resolved = _resolve_item_reference(
                raw_value, local_index, maps,
                prefer_index=explicit_index is not None or not _item_id_text(raw_value))
            # Unknown/container IDs are unbound once a concrete item set is
            # available; retaining the raw object would let malformed state
            # masquerade as a selector on the next run.
            result[raw_key] = resolved if resolved is not None else None
            if resolved is not None and explicit_index is not None:
                result["itemIndex"] = maps["indexByNew"].get(resolved, explicit_index)
        elif raw_key == "itemIndex" and "itemId" in value:
            # Keep an explicit index aligned with the canonical ID even when
            # the input dictionary listed itemIndex after itemId.
            if not isinstance(raw_value, int) or isinstance(raw_value, bool) or raw_value < 0:
                result[raw_key] = None
            else:
                resolved = _resolve_item_reference(value.get("itemId"), raw_value, maps)
                result[raw_key] = (maps["indexByNew"].get(resolved, raw_value)
                                   if resolved is not None else None)
        elif raw_key in {"field", "path"} and isinstance(raw_value, str):
            result[raw_key] = _remap_item_path(raw_value, maps, local_index)
        elif raw_key in {"changedFields", "uncertain", "fields"} and isinstance(raw_value, list):
            result[raw_key] = [_remap_item_path(item, maps, local_index)
                               if isinstance(item, str) else item for item in raw_value]
        elif raw_key in {"items", "supplierReadiness"} and isinstance(raw_value, list):
            result[raw_key] = [_remap_nested_item_references(item, maps, index)
                               for index, item in enumerate(raw_value)]
        else:
            result[raw_key] = _remap_nested_item_references(raw_value, maps, local_index)
    return result


def _remap_uploaded_file_references(value: Any, maps: dict[str, Any]) -> Any:
    """Rewrite file bindings and clear references to removed/unknown items."""
    if not isinstance(value, list):
        return value
    result: list[Any] = []
    for raw in value:
        if not isinstance(raw, dict):
            result.append(raw)
            continue
        item_index = raw.get("itemIndex")
        item_id = raw.get("itemId")
        resolved = _resolve_item_reference(item_id, item_index, maps)
        copy = deepcopy(raw)
        if resolved is not None:
            copy["itemId"] = resolved
            copy["itemIndex"] = maps["indexByNew"].get(resolved, item_index)
        elif maps["current"] and (item_id is not None or item_index is not None):
            # Keep the file record for audit, but make it explicitly unbound.
            copy["itemId"] = None
            copy["itemIndex"] = None
        result.append(copy)
    return result


def _remap_quote_request_references(value: Any, maps: dict[str, Any],
                                   active_request_id: Any = None) -> Any:
    """Rewrite quote item refs and stale requests whose identity changed."""
    if not isinstance(value, list):
        return value
    result: list[Any] = []
    for raw in value:
        if not isinstance(raw, dict):
            result.append(raw)
            continue
        old_item_id = raw.get("itemId")
        old_item_index = raw.get("itemIndex")
        rewritten = _remap_nested_item_references(raw, maps)
        if not isinstance(rewritten, dict):
            result.append(rewritten)
            continue
        new_item_id = rewritten.get("itemId")
        identity_changed = (_item_id_text(old_item_id) != _item_id_text(new_item_id)
                            or (old_item_index != rewritten.get("itemIndex")
                                and old_item_index is not None))
        status = rewritten.get("status")
        if identity_changed and status in {"awaiting_human_confirmation", "confirmed"}:
            rewritten["status"] = "stale"
            rewritten["staleReason"] = "产品项 ID 已规范化，原询价幂等键失效，请重新准备询价。"
            if rewritten.get("requestId") == active_request_id:
                # The caller clears the active ID after this pass; keeping the
                # marker here makes the decision explicit for direct callers.
                rewritten["_activeInvalidated"] = True
        result.append(rewritten)
    return result


def _remap_rejected_fields(value: Any, maps: dict[str, Any]) -> Any:
    """Rewrite audit-only field paths without treating them as order data."""
    if not isinstance(value, list):
        return value
    return [_remap_item_path(item, maps) if isinstance(item, str) else item for item in value]


def migrate_state_item_references(state: dict[str, Any],
                                  id_mapping: list[dict[str, Any]] | None,
                                  previous_items: list[Any] | None = None) -> None:
    """Migrate persisted item references after IDs are normalized.

    The migration is intentionally idempotent: once a repaired ID is stored,
    running ``normalize_state`` again maps it to itself.  Ambiguous legacy
    ID-only references choose the first duplicate; records carrying
    ``itemIndex`` retain the exact item they originally addressed.
    """
    if not isinstance(state, dict) or not id_mapping:
        return
    maps = _item_id_maps(id_mapping, previous_items)
    if not maps["current"]:
        return
    if "fieldMeta" in state:
        state["fieldMeta"] = _remap_field_meta_ids(state.get("fieldMeta"), maps)
    if "itemOptions" in state:
        state["itemOptions"] = _remap_item_options_ids(state.get("itemOptions"), maps)
    if "rejectedFields" in state:
        state["rejectedFields"] = _remap_rejected_fields(state.get("rejectedFields"), maps)
    active_request_id = (state.get("activeQuoteRequestId").strip()
                         if isinstance(state.get("activeQuoteRequestId"), str)
                         else None)
    if "quoteRequests" in state:
        state["quoteRequests"] = _remap_quote_request_references(
            state.get("quoteRequests"), maps, active_request_id)
    for key in ("handoff", "conflicts", "lastRun", "runHistory", "planMeta"):
        if key in state:
            state[key] = _remap_nested_item_references(state.get(key), maps)
    if "uploadedFiles" in state:
        state["uploadedFiles"] = _remap_uploaded_file_references(state.get("uploadedFiles"), maps)
    requests = state.get("quoteRequests")
    if isinstance(requests, list):
        invalidated: set[str] = set()
        for item in requests:
            if not isinstance(item, dict) or not item.pop("_activeInvalidated", False):
                continue
            request_id = item.get("requestId")
            if isinstance(request_id, str):
                invalidated.add(request_id)
        if active_request_id in invalidated:
            state["activeQuoteRequestId"] = None


def required_order_keys(order: dict[str, Any]) -> list[str]:
    """Return base fields that make sense for the selected product family."""
    product = order.get("productType")
    return [key for key in REQUIRED if not (key == "paper" and product in MATERIAL_SPEC_PRODUCTS)]


def _multi_product_info(order: dict[str, Any]) -> list[str]:
    """Return labels when an order contains multiple independent order items."""
    product_types = order.get("productTypes") if isinstance(order.get("productTypes"), list) else []
    items = order.get("items") if isinstance(order.get("items"), list) else []
    labels: list[str] = []
    for value in product_types:
        if value and str(value) not in labels:
            labels.append(str(value))
    for item in items:
        if isinstance(item, dict) and item.get("productType"):
            value = str(item["productType"])
            if value not in labels:
                labels.append(value)
    if len(product_types) > 1 or len(items) > 1 or len(labels) > 1:
        return labels or ["多个订单项"]


def _number(value: Any) -> int | None:
    text = _legacy_text(value) if _is_legacy_scalar(value) else ""
    match = re.search(r"\d[\d,]*(?:\.\d+)?", text)
    if not match:
        return None
    try:
        number = float(match.group(0).replace(",", ""))
        if not math.isfinite(number):
            return None
        return int(number)
    except (OverflowError, ValueError):
        return None


def _parse_size_mm(value: str) -> tuple[float, ...] | None:
    """Parse common named/custom sizes for capability checks, without guessing units."""
    text = unicodedata.normalize("NFKC", str(value or "")).upper().replace("＊", "×").replace("*", "×")
    named = {"A3": (297.0, 420.0), "A4": (210.0, 297.0), "A5": (148.0, 210.0),
             "B4": (257.0, 364.0), "B5": (182.0, 257.0)}
    compact = re.sub(r"\s+", "", text)
    if compact in named:
        return named[compact]
    match = re.fullmatch(r"(\d+(?:\.\d+)?)×(\d+(?:\.\d+)?)(?:×(\d+(?:\.\d+)?))?(MM|CM)?", compact)
    if not match:
        return None
    values = tuple(float(item) for item in match.groups()[:3] if item is not None)
    unit = match.group(4)
    if unit == "CM":
        return tuple(item * 10 for item in values)
    if unit == "MM":
        return values
    return None


def _parse_max_size(value: str) -> tuple[float, ...] | None:
    text = unicodedata.normalize("NFKC", str(value or "")).upper().replace("＊", "×").replace("*", "×")
    if text == "A3+":
        return (330.0, 480.0)
    return _parse_size_mm(text)


STATE_SCHEMA_VERSION = 2
# ``revision`` is an optimistic-concurrency counter for the session-bound
# patch bridge.  Keep it within the range that JSON/JavaScript clients can
# represent exactly; older sessions simply start at zero.
MAX_STATE_REVISION = 2**53 - 1
STATE_STAGE_VALUES = frozenset({
    "collect", "clarify", "recommend", "preflight", "quote", "confirm", "export",
})
_STATE_MISSING = object()


def _safe_state_scalar(value: Any) -> Any:
    """Return a JSON scalar, rejecting non-finite numbers and containers."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return _STATE_MISSING


def _safe_state_tree(value: Any, depth: int = 0) -> Any:
    """Copy advisory persisted data without retaining executable Python objects."""
    scalar = _safe_state_scalar(value)
    if scalar is not _STATE_MISSING:
        return deepcopy(scalar)
    if depth >= 8:
        return _STATE_MISSING
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (raw_key, raw_value) in enumerate(value.items()):
            if index >= 128 or not isinstance(raw_key, str):
                continue
            safe = _safe_state_tree(raw_value, depth + 1)
            if safe is not _STATE_MISSING:
                result[raw_key[:256]] = safe
        return result
    if isinstance(value, list):
        result: list[Any] = []
        for raw_value in value[:128]:
            safe = _safe_state_tree(raw_value, depth + 1)
            if safe is not _STATE_MISSING:
                result.append(safe)
        return result
    return _STATE_MISSING


def _normalize_state_messages(value: Any) -> list[dict[str, str]]:
    """Keep the chat history shape consumed by the planner and UI."""
    if not isinstance(value, list):
        return []
    messages: list[dict[str, str]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        role, text = raw.get("role"), raw.get("text")
        if isinstance(role, str) and isinstance(text, str):
            messages.append({"role": role, "text": text})
    return messages


def _normalize_state_uploaded_files(value: Any) -> list[dict[str, Any]]:
    """Keep only the file binding fields used by the preflight workflow."""
    if not isinstance(value, list):
        return []
    files: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        file_name = raw.get("fileName")
        if not isinstance(file_name, str) or not file_name.strip():
            continue
        item_id = raw.get("itemId")
        if item_id is not None:
            item_id = (item_id.strip() if isinstance(item_id, str) and item_id.strip()
                       and len(item_id.strip()) <= MAX_ITEM_ID_LENGTH else None)
        item_index = raw.get("itemIndex")
        if item_index is not None and (
                isinstance(item_index, bool) or not isinstance(item_index, int) or item_index < 0):
            item_index = None
        files.append({"itemId": item_id, "itemIndex": item_index, "fileName": file_name.strip()})
    return files


def _normalize_state_handoff(value: Any) -> dict[str, Any] | None:
    """Retain only a tool-shaped handoff envelope at the confirmation gate."""
    if not isinstance(value, dict):
        return None
    status, text = value.get("status"), value.get("text")
    if not isinstance(status, str) or status not in {"ready", "blocked"} or not isinstance(text, str):
        return None
    safe = _safe_state_tree(value)
    return safe if isinstance(safe, dict) else None


def _normalize_state_confirmation(value: Any) -> dict[str, Any]:
    """Normalize the human-confirmation gate and discard nested payloads."""
    if not isinstance(value, dict):
        return {"status": "not_ready"}
    raw_status = value.get("status")
    status = raw_status if isinstance(raw_status, str) and raw_status in {"not_ready", "pending", "confirmed"} else "not_ready"
    result: dict[str, Any] = {"status": status}
    for key in ("confirmedAt", "note"):
        raw = value.get(key)
        if isinstance(raw, str):
            result[key] = raw
    return result


def _normalize_state_plan_meta(value: Any) -> dict[str, Any]:
    """Keep advisory skill annotations display-only and shape-stable."""
    if not isinstance(value, dict):
        return {"questions": [], "risks": [], "knowledgeVersion": ""}
    result: dict[str, Any] = {"questions": [], "risks": [], "knowledgeVersion": ""}
    allowed_annotation_keys = {"field", "question", "text", "message", "risk", "severity", "source", "code"}
    for name in ("questions", "risks"):
        raw_items = value.get(name)
        if not isinstance(raw_items, list):
            continue
        items: list[Any] = []
        for raw_item in raw_items[:32]:
            if isinstance(raw_item, str):
                text = raw_item.strip()
                if text:
                    items.append(text)
                continue
            if not isinstance(raw_item, dict):
                continue
            annotation: dict[str, str] = {}
            for key, item in raw_item.items():
                if not isinstance(key, str) or key not in allowed_annotation_keys \
                        or not isinstance(item, (str, int, float)) \
                        or isinstance(item, bool) or (isinstance(item, float) and not math.isfinite(item)):
                    continue
                text = str(item).strip()
                if text:
                    annotation[key] = text
            if annotation:
                items.append(annotation)
        result[name] = items
    for name in ("knowledgeVersion", "reportedKnowledgeVersion"):
        raw = value.get(name)
        if isinstance(raw, str) and raw.strip():
            result[name] = raw.strip()[:128]
    return result


def _normalize_state_field_meta(value: Any) -> dict[str, dict[str, Any]]:
    """Keep provenance entries bounded; only whole-order list paths stay structured."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for raw_key, raw_entry in value.items():
        if not isinstance(raw_key, str) or not isinstance(raw_entry, dict):
            continue
        key = raw_key.strip()[:256]
        if not key:
            continue
        raw_value = raw_entry.get("value", _STATE_MISSING)
        if raw_value is _STATE_MISSING:
            continue
        if raw_value is not None and not _is_legacy_scalar(raw_value):
            # Whole-order list patches legitimately record their list value in
            # provenance. Keep those two known paths bounded, but reject
            # containers for scalar production fields.
            if key not in {"items", "productTypes"}:
                continue
            raw_value = _safe_state_tree(raw_value)
            if raw_value is _STATE_MISSING:
                continue
        entry: dict[str, Any] = {"value": deepcopy(raw_value)}
        for name in ("source", "sourceLabel", "runId", "updatedAt"):
            raw = raw_entry.get(name)
            if isinstance(raw, str):
                entry[name] = raw
            elif name == "runId" and raw is None:
                entry[name] = None
        confidence = raw_entry.get("confidence")
        if (isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                and (not isinstance(confidence, float) or math.isfinite(confidence))):
            try:
                normalized_confidence = float(confidence)
            except (OverflowError, ValueError):
                normalized_confidence = None
            if normalized_confidence is not None and math.isfinite(normalized_confidence):
                entry["confidence"] = max(0.0, min(1.0, normalized_confidence))
        evidence = raw_entry.get("evidence")
        if isinstance(evidence, dict):
            quote = evidence.get("quote", evidence.get("evidence"))
            if isinstance(quote, str) and quote.strip():
                clean_evidence = {"quote": quote.strip()}
                source = evidence.get("source")
                if isinstance(source, str) and source.strip():
                    clean_evidence["source"] = source.strip()
                entry["evidence"] = clean_evidence
        result[key] = entry
        if len(result) >= 128:
            break
    return result


def _normalize_state_conflicts(value: Any) -> list[dict[str, Any]]:
    """Retain correction records with scalar values or known list paths."""
    if not isinstance(value, list):
        return []
    conflicts: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict) or not isinstance(raw.get("field"), str):
            continue
        previous = raw.get("previous", _STATE_MISSING)
        current = raw.get("current", _STATE_MISSING)
        value_is_valid = True
        for name, item in (("previous", previous), ("current", current)):
            if item is _STATE_MISSING:
                value_is_valid = False
                break
            if item is None or _is_legacy_scalar(item):
                continue
            if raw["field"] not in {"items", "productTypes"}:
                value_is_valid = False
                break
            safe = _safe_state_tree(item)
            if safe is _STATE_MISSING:
                value_is_valid = False
                break
            if name == "previous":
                previous = safe
            else:
                current = safe
        if not value_is_valid:
            continue
        entry: dict[str, Any] = {"field": raw["field"], "previous": deepcopy(previous), "current": deepcopy(current)}
        for name in ("label", "source", "sourceLabel", "runId", "at"):
            item = raw.get(name)
            if isinstance(item, str):
                entry[name] = item
            elif name == "runId" and item is None:
                entry[name] = None
        if isinstance(raw.get("resolved"), bool):
            entry["resolved"] = raw["resolved"]
        conflicts.append(entry)
    return conflicts


def _normalize_state_run_event(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    safe = _safe_state_tree(value)
    return safe if isinstance(safe, dict) else None


def _normalize_state_run(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for name in ("runId", "operation", "status", "startedAt", "finishedAt"):
        item = value.get(name)
        if isinstance(item, str):
            result[name] = item
    events = value.get("events")
    if isinstance(events, list):
        result["events"] = [event for raw in events
                             if (event := _normalize_state_run_event(raw)) is not None]
    else:
        result["events"] = []
    return result if result.get("runId") else None


def _normalize_state_item_options(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    for raw_item_id, raw_options in value.items():
        if not isinstance(raw_item_id, str) or not isinstance(raw_options, list):
            continue
        options: list[dict[str, Any]] = []
        for raw_option in raw_options:
            if not isinstance(raw_option, dict):
                continue
            option: dict[str, Any] = {}
            for key, item in raw_option.items():
                if not isinstance(key, str):
                    continue
                safe = _safe_state_scalar(item)
                if safe is not _STATE_MISSING:
                    option[key[:128]] = safe
            if isinstance(option.get("id"), str) and option["id"].strip():
                options.append(option)
        result[raw_item_id[:128]] = options
    return result


def _normalize_state_quote_requests(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    requests: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict) or not isinstance(raw.get("requestId"), str):
            continue
        safe = _safe_state_tree(raw)
        if not isinstance(safe, dict):
            continue
        status = safe.get("status")
        if not isinstance(status, str):
            continue
        requests.append(safe)
    return requests


def normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    """Apply every schema migration so old sessions load exactly like fresh ones.

    New session keys belong here (one setdefault each) together with a bump of
    ``STATE_SCHEMA_VERSION``; ad-hoc migrations stay in this single function.
    """
    order = deepcopy(ORDER_DEFAULTS)
    raw_order = state.get("order")
    if isinstance(raw_order, dict):
        order.update(raw_order)
    # Normalize text-like order fields before dimension/quantity migration so
    # malformed containers cannot reach downstream lookups or capability code.
    text_fields = (
        "productType", "purpose", "quantity", "quantityUnit", "size", "pages",
        "orientation", "paper", "printing", "finishing", "binding", "deadline", "budget",
        "platform",
    )
    for key in text_fields:
        if key not in order or _is_legacy_scalar(order.get(key)):
            order[key] = _legacy_text(order.get(key))
        else:
            order[key] = deepcopy(ORDER_DEFAULTS[key])
    if not isinstance(order.get("quantityValue"), (int, float)) or isinstance(order.get("quantityValue"), bool) \
            or (isinstance(order.get("quantityValue"), float) and not math.isfinite(order["quantityValue"])):
        order["quantityValue"] = None
    if not isinstance(order.get("dimensions"), dict):
        order["dimensions"] = {}
    if not isinstance(order.get("productSpecs"), dict):
        order["productSpecs"] = {}
    raw_product_types = order.get("productTypes")
    if isinstance(raw_product_types, list):
        order["productTypes"] = [
            text for value in raw_product_types
            if _is_legacy_scalar(value) and (text := _legacy_text(value))
        ]
    else:
        order["productTypes"] = []
    normalize_order_quantity(order)
    normalize_order_dimensions(order)
    item_id_mapping: list[dict[str, Any]] = []
    normalize_order_items(order, id_mapping=item_id_mapping)
    state["order"] = order
    # Migrate item-scoped state before the individual normalizers below cap or
    # discard legacy keys (notably overlong IDs in fieldMeta/itemOptions).
    migrate_state_item_references(state, item_id_mapping)
    state["messages"] = _normalize_state_messages(state.get("messages"))
    raw_stage = state.get("stage")
    state["stage"] = raw_stage if isinstance(raw_stage, str) and raw_stage in STATE_STAGE_VALUES else "collect"
    selected = state.get("selectedOption")
    state["selectedOption"] = selected.strip() if isinstance(selected, str) and selected.strip() else None
    state["orderGenerated"] = state.get("orderGenerated") if isinstance(state.get("orderGenerated"), bool) else False
    uploaded_file = state.get("uploadedFile")
    state["uploadedFile"] = uploaded_file.strip() if isinstance(uploaded_file, str) and uploaded_file.strip() else None
    state["uploadedFiles"] = _normalize_state_uploaded_files(state.get("uploadedFiles"))
    state["handoff"] = _normalize_state_handoff(state.get("handoff"))
    state["confirmation"] = _normalize_state_confirmation(state.get("confirmation"))
    state["fieldMeta"] = _normalize_state_field_meta(state.get("fieldMeta"))
    migrate_dimension_field_meta(state)
    state["conflicts"] = _normalize_state_conflicts(state.get("conflicts"))
    state["lastRun"] = _normalize_state_run(state.get("lastRun"))
    raw_history = state.get("runHistory")
    state["runHistory"] = [record for raw in raw_history or []
                            if (record := _normalize_state_run(raw)) is not None] \
        if isinstance(raw_history, list) else []
    # ``rejectedFields`` is an audit-only migration field.  Keep it bounded and
    # string-only when loading sessions created by an older client or by a
    # malformed model response; it must never become a source of executable
    # order data.
    raw_rejected = state.get("rejectedFields")
    if isinstance(raw_rejected, list):
        rejected: list[str] = []
        for value in raw_rejected:
            if not isinstance(value, str):
                continue
            value = value.strip()
            if value and value not in rejected:
                rejected.append(value[:256])
        state["rejectedFields"] = rejected[-128:]
    else:
        state["rejectedFields"] = []
    # Planner/skill annotations are advisory metadata.  Keep the envelope
    # stable across restarts, but never let malformed values become executable
    # order fields.
    state["planMeta"] = _normalize_state_plan_meta(state.get("planMeta"))
    raw_workflow_stage = state.get("workflowStage")
    state["workflowStage"] = (raw_workflow_stage if isinstance(raw_workflow_stage, str)
                               and raw_workflow_stage in STATE_STAGE_VALUES else state["stage"])
    raw_index = state.get("activeItemIndex")
    item_count = len(order.get("items")) if isinstance(order.get("items"), list) else 0
    state["activeItemIndex"] = (raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool)
                                 and item_count > 1 and 0 <= raw_index < item_count else None)
    state["itemOptions"] = _normalize_state_item_options(state.get("itemOptions"))
    state["quoteRequests"] = _normalize_state_quote_requests(state.get("quoteRequests"))
    active_quote_id = state.get("activeQuoteRequestId")
    state["activeQuoteRequestId"] = active_quote_id.strip() if isinstance(active_quote_id, str) and active_quote_id.strip() else None
    schema_version = state.get("schemaVersion")
    state["schemaVersion"] = schema_version if isinstance(schema_version, int) and not isinstance(schema_version, bool) \
        and schema_version > 0 else STATE_SCHEMA_VERSION
    raw_revision = state.get("revision")
    state["revision"] = (raw_revision if isinstance(raw_revision, int)
                          and not isinstance(raw_revision, bool)
                          and 0 <= raw_revision <= MAX_STATE_REVISION else 0)
    return state


# Full-sheet sizes (mm) for imposition hints. 印张光边按四边各 3mm 预留。
SHEET_SIZES_MM = {"大度": (889.0, 1194.0), "正度": (787.0, 1092.0)}
TRIM_MARGIN_MM = 3.0


def imposition_hint(size: str | None) -> str | None:
    """Estimate how many finished flat pieces fit on a full print sheet.

    Pure geometry for reference only: whole-piece counts on 大度/正度 sheets
    after a 3mm trim margin, trying both orientations. Real imposition also
    depends on grain direction, bleed and the supplier's layout.
    """
    parsed = _parse_size_mm(size or "")
    if not parsed or len(parsed) != 2:
        return None
    short, long = sorted(parsed)
    best: tuple[int, str, float] | None = None
    for name, (sheet_w, sheet_h) in SHEET_SIZES_MM.items():
        usable_w, usable_h = sheet_w - 2 * TRIM_MARGIN_MM, sheet_h - 2 * TRIM_MARGIN_MM
        for piece_w, piece_h in ((short, long), (long, short)):
            count = int(usable_w // piece_w) * int(usable_h // piece_h)
            if count <= 0:
                continue
            utilization = count * short * long / (sheet_w * sheet_h) * 100
            if best is None or count > best[0]:
                best = (count, name, utilization)
    if best is None:
        return None
    count, name, utilization = best
    return f"{name}全张约可出 {count} 裁，纸张利用率约 {utilization:.0f}%（参考，以供应商拼版为准）"
