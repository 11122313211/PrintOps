"""Canonical order/item patch engine, field edits, provenance migration, and MCP patch bridge."""

from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any

from printops.domain.knowledge import KNOWLEDGE_VERSION, known_product_spec_keys, paper_spec_key, parameter_state
from printops.domain.order_contract import production_fingerprint
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
    _multi_product_info,
    default_quantity_unit,
    merge_dimension_patch,
    migrate_dimension_field_meta,
    migrate_state_item_references,
    normalize_order_dimensions,
    normalize_order_items,
    parse_quantity,
    size_dimension_key,
)
from printops.domain.units import normalize_dimension
from printops.agent.memory import ITEM_PATCH_KEYS, MAX_PATCH_VALUE, ORDER_PATCH_KEYS


class PatchEngineMixin:
    """Authoritative order and item mutation gateway."""

    def edit_fields(self, action: str, values: dict[str, Any] | None = None,
                    fields: list[str] | None = None, item_index: int | None = None) -> dict[str, Any]:
        """Explicit user edits/confirmation, without manufacturing a chat utterance."""
        if self.state.get("orderStatus") == "archived":
            raise ValueError("已归档订单只读")
        if action not in {"set", "clear", "confirm", "reject"}:
            raise ValueError("未知字段操作")
        if action == "set":
            if not isinstance(values, dict) or not values or len(values) > 64:
                raise ValueError("values 需要非空字段映射，最多64项")
            paths = list(values)
        else:
            if not isinstance(fields, list) or not fields or len(fields) > 64:
                raise ValueError("fields 需要非空字段列表，最多64项")
            paths = fields
        items = self.state["order"].get("items") or []
        if len(items) > 1 and item_index is None:
            raise ValueError("多产品字段操作必须明确 itemId")
        target = items[item_index] if item_index is not None else self.state["order"]
        item_id = target.get("itemId") if item_index is not None else None
        prefix = f"items.{item_id}." if item_id else ""
        allowed = (ITEM_PATCH_KEYS if item_id else ORDER_PATCH_KEYS) - {"uploadedFile", "items", "productTypes", "measurements", "unitSystem", "dimensions", "productSpecs"}
        requested_product = ((values.get("productType") or values.get(prefix + "productType"))
                             if action == "set" else None)
        specs = known_product_spec_keys(requested_product if isinstance(requested_product, str) else target.get("productType") or None)
        def read(path):
            parts = path.split(".")
            return (target.get(parts[0]) or {}).get(parts[1]) if len(parts) == 2 else target.get(path)
        normalized_paths = []
        for path in paths:
            if not isinstance(path, str):
                raise ValueError("字段标识必须是字符串")
            relative = path[len(prefix):] if prefix and path.startswith(prefix) else path
            if relative.startswith("items."):
                raise ValueError("字段路径与产品作用对象不一致")
            parts = relative.split(".")
            valid = (len(parts) == 1 and relative in allowed or len(parts) == 2 and
                     (parts[0] == "dimensions" and parts[1] in DIMENSION_DEFAULTS or parts[0] == "productSpecs" and parts[1] in specs))
            if not valid:
                raise ValueError(f"未知或只读字段：{relative}")
            if any(existing == relative for existing, _ in normalized_paths):
                raise ValueError("同一字段不能在一次操作中重复指定")
            value = values[path] if action == "set" else read(relative)
            if action == "set" and (not self._is_patch_scalar(value) or value is None or str(value).strip() == "" or len(str(value)) > MAX_PATCH_VALUE):
                raise ValueError("填写值必须为非空标量；清空请使用 clear")
            if action == "confirm" and value in (None, ""):
                raise ValueError("缺失字段不能确认")
            normalized_paths.append((relative, deepcopy(value)))
        quantity_patch = {}
        if action in {"set", "confirm"} and any(path in {"quantity", "quantityValue", "quantityUnit"} for path, _ in normalized_paths):
            requested = dict(normalized_paths)
            raw = requested.get("quantity", requested.get("quantityValue", target.get("quantityValue")))
            unit = requested.get("quantityUnit", target.get("quantityUnit") or "")
            parsed = parse_quantity(raw, requested_product or target.get("productType"), str(unit))
            if not parsed or parsed[1] <= 0 or re.search(r"[-−]\s*\d", str(raw)):
                raise ValueError("数量必须是大于零的有效数量")
            quantity_patch = {"quantity": parsed[0], "quantityValue": parsed[1], "quantityUnit": parsed[2]}
        if action in {"set", "clear"}:
            # The generic paper field and the profile's material parameter
            # (for example 名片材质) are one concept in the UI. Editing either
            # updates both, so the priced paper and the visible spec agree.
            linked = paper_spec_key(requested_product or target.get("productType"))
            if linked:
                spec_path = f"productSpecs.{linked}"
                present = {path for path, _ in normalized_paths}
                for path, value in list(normalized_paths):
                    other = spec_path if path == "paper" else "paper" if path == spec_path else ""
                    if other and other not in present:
                        normalized_paths.append((other, deepcopy(value)))
                        present.add(other)
        # Validate the entire request before touching any field, making mixed requests atomic.
        before = {path: deepcopy(read(path)) for path, _ in normalized_paths}
        self._begin_run(f"fields:{action}")
        if item_index is not None:
            self.state["activeItemIndex"] = item_index
        if action in {"set", "confirm"}:
            patch = {}
            for path, value in normalized_paths:
                parts = path.split(".")
                if len(parts) == 2:
                    patch.setdefault(parts[0], {})[parts[1]] = value
                else:
                    patch[path] = value
            patch.update(quantity_patch)
            if item_index is None:
                self._update_order(patch, source="user", confidence=1)
            else:
                self._update_item(item_index, patch, source="user", confidence=1)
        else:
            for path, _ in normalized_paths:
                aliases = {path}
                dimension = (path.split(".", 1)[1] if path.startswith("dimensions.") else
                             DIMENSION_SPEC_ALIASES.get(path.split(".", 1)[1]) if path.startswith("productSpecs.") else
                             size_dimension_key(target.get("size"), target.get("productType", "")) if path == "size" else None)
                if dimension:
                    aliases.add("dimensions." + dimension)
                    aliases.update("productSpecs." + alias for alias, meaning in DIMENSION_SPEC_ALIASES.items() if meaning == dimension)
                    if dimension == size_dimension_key(target.get("size"), target.get("productType", "")):
                        aliases.add("size")
                if path in {"quantity", "quantityValue", "quantityUnit"}:
                    aliases.update({"quantity", "quantityValue", "quantityUnit"})
                for alias in aliases:
                    parts = alias.split(".")
                    if len(parts) == 2:
                        target.setdefault(parts[0], {}).pop(parts[1], None)
                    else:
                        target[alias] = None if alias == "quantityValue" else ""
                    self.state.get("fieldMeta", {}).pop(prefix + alias, None)
                    for candidate in self.state.get("fieldCandidates", []):
                        if candidate.get("field") == prefix + alias and candidate.get("status") != "rejected":
                            candidate["status"] = "rejected"
            # Normalize once after every alias is removed: normalizing between
            # two linked fields (paper and its profile parameter) would let one
            # of them resurrect the other mid-clear.
            normalize_order_dimensions(target)
            if not item_id:
                self.state["selectedOption"] = None
            else:
                target["selectedOption"] = None
                target["orderGenerated"] = False
            self._clear_process_options(item_index)
            self._mark_preflight_stale(item_index, "用户清空或拒绝了生产字段")
            self._invalidate_delivery_state()
        self.state["workflowStage"] = self._base_workflow_stage()
        self._event("memory", "ok", f"用户字段操作：{action}", changedFields=[prefix + path for path, _ in normalized_paths])
        result = self._result([])
        live = self.state["order"]["items"][item_index] if item_index is not None else self.state["order"]
        def after(path):
            parts = path.split(".")
            return (live.get(parts[0]) or {}).get(parts[1]) if len(parts) == 2 else live.get(path)
        result["fieldChanges"] = [{"fieldPath": path, "fieldId": prefix + path, "itemId": item_id,
                                    "before": before[path], "after": deepcopy(after(path))} for path, _ in normalized_paths]
        result["changeImpact"] = {"handoffInvalidated": self.state.get("confirmation", {}).get("status") == "stale",
                                  "requiresReestimate": action != "confirm", "action": action}
        return result

    def _apply_patch(self, target: dict[str, Any], previous: dict[str, Any],
                     changes: dict[str, Any], *, source: str, confidence: float,
                     field_confidence: dict[str, float] | None, allowed_keys: set[str],
                     meta_prefix: str = "", list_fields: dict[str, Any] | None = None,
                     settle=None, field_evidence: dict[str, dict[str, str]] | None = None) -> set[str]:
        """Shared constrained-patch engine for the whole order and each item.

        ``previous`` is the untouched pre-patch copy used for conflict and
        provenance records; ``meta_prefix`` scopes provenance fields for items;
        ``list_fields`` carries order-only list patches; ``settle`` runs after
        the values land but before provenance is recorded. Returns the set of
        keys whose value actually changed. Recommendation invalidation stays
        with the callers because order and items differ there.
        """
        valid: dict[str, Any] = {}
        rejected_fields: list[str] = []
        quantity_keys = {"quantity", "quantityValue", "quantityUnit"}
        # ``items``/``productTypes`` are order-only list patches handled by
        # ``_update_order`` before this function, even when their normalized
        # value happens to equal the current state.  Item-scoped patches do
        # not get this exception.
        list_patch_keys = {"items", "productTypes"} if list_fields is not None else set()
        permitted_change_keys = set(allowed_keys) | quantity_keys | set(list_fields or {}) | list_patch_keys
        # ``productSpecs`` is handled below as a nested allowlist.  Other
        # unknown top-level keys are retained only in the audit trail.
        for raw_key in changes:
            # JSON patches have string keys.  Keep the lower-level gateway
            # defensive for direct Python callers and custom mappings too:
            # checking membership on an unhashable key would otherwise raise
            # before the rest of the valid patch can be applied.
            if not isinstance(raw_key, str):
                rejected_fields.append(f"{meta_prefix}<non-string-patch-key>")
                continue
            key = raw_key.strip()
            if key and raw_key not in permitted_change_keys:
                rejected_fields.append(f"{meta_prefix}{key}")
        if any(key in changes for key in quantity_keys):
            invalid_quantity = [
                key for key in quantity_keys
                if key in changes and changes.get(key) is not None
                and (not self._is_patch_scalar(changes.get(key))
                     or (isinstance(changes.get(key), str)
                         and len(changes.get(key).strip()) > MAX_PATCH_VALUE))
            ]
            if invalid_quantity:
                rejected_fields.extend(f"{meta_prefix}{key}" for key in invalid_quantity)
            else:
                product = str(changes.get("productType") or target.get("productType") or "")
                raw_quantity = changes.get("quantity")
                if raw_quantity in (None, ""):
                    raw_quantity = changes.get("quantityValue", target.get("quantity"))
                unit_hint = changes.get("quantityUnit") or ""
                parsed_quantity = parse_quantity(raw_quantity, product, str(unit_hint))
                if parsed_quantity:
                    display, numeric, unit = parsed_quantity
                    valid.update({"quantity": display, "quantityValue": numeric, "quantityUnit": unit})
                    if all(target.get(key) == value for key, value in
                           (("quantity", display), ("quantityValue", numeric), ("quantityUnit", unit))):
                        # Re-stating an identical quantity is still a confirmation:
                        # it must be able to clear a low confidence grade.
                        for key, value in (("quantity", display), ("quantityValue", numeric), ("quantityUnit", unit)):
                            self._set_field_meta(f"{meta_prefix}{key}", value, source, confidence,
                                                 field_confidence, field_evidence)
                else:
                    # Keep malformed scalar quantities out of the order and
                    # expose the rejection to the bridge/audit caller.
                    rejected_fields.extend(
                        f"{meta_prefix}{key}" for key in quantity_keys
                        if key in changes and changes.get(key) is not None
                    )
        for key, value in changes.items():
            if not isinstance(key, str):
                # Already recorded above; do not perform set membership on a
                # possibly unhashable custom mapping key.
                continue
            if key not in allowed_keys or value is None or key in quantity_keys:
                continue
            if key == "dimensions":
                if not isinstance(value, dict):
                    rejected_fields.append(f"{meta_prefix}dimensions")
                    continue
                current_dimensions = dict(target.get("dimensions") or {})
                next_dimensions = dict(DIMENSION_DEFAULTS)
                next_dimensions.update({name: str(current_dimensions.get(name) or "").strip()
                                        for name in DIMENSION_DEFAULTS})
                for raw_name, dimension_value in value.items():
                    if not isinstance(raw_name, str):
                        rejected_fields.append(f"{meta_prefix}dimensions.<non-string-key>")
                        continue
                    name = raw_name.strip()
                    if name not in DIMENSION_DEFAULTS:
                        rejected_fields.append(f"{meta_prefix}dimensions.{name}")
                        continue
                    if (dimension_value is not None and (
                            not self._is_patch_scalar(dimension_value)
                            or (isinstance(dimension_value, float) and not math.isfinite(dimension_value)))):
                        rejected_fields.append(f"{meta_prefix}dimensions.{name}")
                        continue
                    next_dimensions[name] = str(dimension_value).strip() if dimension_value is not None else ""
                if next_dimensions != current_dimensions:
                    valid[key] = next_dimensions
            elif key == "productSpecs":
                if not isinstance(value, dict):
                    rejected_fields.append(f"{meta_prefix}productSpecs")
                    continue
                current_specs = dict(target.get("productSpecs") or {})
                next_specs = dict(current_specs)
                dimension_patch: dict[str, Any] = {}
                product_name = str(changes.get("productType") or target.get("productType") or "").strip()
                allowed_specs = known_product_spec_keys(product_name or None)
                for raw_name, spec_value in value.items():
                    if not isinstance(raw_name, str):
                        rejected_fields.append(f"{meta_prefix}productSpecs")
                        continue
                    name = raw_name.strip()
                    name = str(name).strip()
                    if not name:
                        continue
                    # Dimension aliases are normalized into the canonical
                    # ``dimensions`` object; all other spec names must belong
                    # to the selected product profile (or the union when the
                    # product has not been identified yet).
                    if name not in DIMENSION_DEFAULTS and name not in allowed_specs:
                        rejected_fields.append(f"{meta_prefix}productSpecs.{name}")
                        continue
                    if (not self._is_patch_scalar(spec_value)
                            or (isinstance(spec_value, float) and not math.isfinite(spec_value))):
                        rejected_fields.append(f"{meta_prefix}productSpecs.{name}")
                        continue
                    normalized = str(spec_value).strip() if spec_value is not None else ""
                    if name in DIMENSION_DEFAULTS:
                        dimension_patch[name] = normalized
                        continue
                    if normalized:
                        if self._equivalent_value(next_specs.get(name), normalized):
                            if source == "user" or self._has_field_signal(
                                    f"{meta_prefix}productSpecs.{name}", field_confidence, field_evidence):
                                self._set_field_meta(f"{meta_prefix}productSpecs.{name}",
                                                     next_specs.get(name), source, confidence,
                                                     field_confidence, field_evidence)
                            continue
                        next_specs[name] = normalized
                    else:
                        next_specs.pop(name, None)
                if next_specs != current_specs:
                    valid[key] = next_specs
                if dimension_patch:
                    valid["dimensions"] = merge_dimension_patch(target.get("dimensions"), dimension_patch)
            elif not self._is_patch_scalar(value):
                rejected_fields.append(f"{meta_prefix}{key}")
                continue
            else:
                try:
                    normalized = str(value).strip()
                except (OverflowError, ValueError):
                    rejected_fields.append(f"{meta_prefix}{key}")
                    continue
                normalized = normalized[:MAX_PATCH_VALUE]
                if not normalized:
                    continue
                if self._equivalent_value(target.get(key), normalized):
                    if source == "user" or self._has_field_signal(
                            f"{meta_prefix}{key}", field_confidence, field_evidence):
                        self._set_field_meta(f"{meta_prefix}{key}", target.get(key), source, confidence,
                                             field_confidence, field_evidence)
                    continue
                valid[key] = normalized
        if list_fields:
            valid.update(list_fields)
        # Resolve all legacy dimension inputs before mutating the order. An
        # explicit canonical dimension wins over either a spec alias or size,
        # independent of JSON key order. Accepted same-value inputs also carry
        # confirmation intent, without confirming any sibling dimensions.
        dimension_inputs: dict[str, tuple[Any, str]] = {}
        requested_specs = changes.get("productSpecs") if isinstance(changes.get("productSpecs"), dict) else {}
        spec_allowlist = known_product_spec_keys(str(changes.get("productType") or target.get("productType") or "") or None)
        for raw_name, value in requested_specs.items():
            if not isinstance(raw_name, str) or not self._is_patch_scalar(value):
                continue
            name = raw_name.strip()
            if name in DIMENSION_DEFAULTS:
                dimension_inputs[name] = (value, f"productSpecs.{name}")
            elif name in DIMENSION_SPEC_ALIASES and name in spec_allowlist:
                dimension_inputs[DIMENSION_SPEC_ALIASES[name]] = (value, f"productSpecs.{name}")
        requested_dimensions = changes.get("dimensions") if isinstance(changes.get("dimensions"), dict) else {}
        for raw_name, value in requested_dimensions.items():
            if (isinstance(raw_name, str) and raw_name.strip() in DIMENSION_DEFAULTS
                    and (value is None or self._is_patch_scalar(value))):
                dimension_inputs[raw_name.strip()] = (value, f"dimensions.{raw_name.strip()}")
        if ("size" in changes and "size" in allowed_keys and self._is_patch_scalar(changes["size"])
                and str(changes["size"]).strip()):
            primary = size_dimension_key(changes["size"], str(changes.get("productType") or target.get("productType") or ""))
            dimension_inputs.setdefault(primary, (changes["size"], "size"))
        if dimension_inputs:
            valid["dimensions"] = merge_dimension_patch(target.get("dimensions"),
                                                        {name: value for name, (value, _) in dimension_inputs.items()})
        changed = {key for key, value in valid.items() if target.get(key) != value}
        target.update(valid)
        # A legacy size edit and its structured meaning are one atomic write.
        # Explicit dimensions win when both are supplied in this patch.
        primary_key = size_dimension_key(target.get("size"), str(target.get("productType") or ""))
        if primary_key in dimension_inputs:
            target["size"] = normalize_dimension(dimension_inputs[primary_key][0])
        previous_dimensions = previous.get("dimensions") or {}
        normalize_order_dimensions(target)
        if previous_dimensions != target.get("dimensions"):
            changed.add("dimensions")
        if previous.get("size") != target.get("size"):
            changed.add("size")
        if previous.get("productSpecs") != target.get("productSpecs"):
            changed.add("productSpecs")
        if "productType" in changed and previous.get("productType") != target.get("productType"):
            # Product-specific fields belong to the old item and must not leak into a new draft.
            # Preserve explicitly supplied fields for the *new* product when
            # the caller changes productType and specs in one atomic patch.
            # ``valid.productSpecs`` is normally a merge with the old map, so
            # capture only keys explicitly present in this patch.  Otherwise
            # changing a product while adding one new spec would resurrect
            # specs belonging to the previous product.
            incoming_specs: dict[str, Any] = {}
            requested_specs = changes.get("productSpecs") if isinstance(changes.get("productSpecs"), dict) else {}
            new_product_specs = known_product_spec_keys(str(target.get("productType") or "") or None)
            for raw_name, value in requested_specs.items():
                # ``requested_specs`` normally came from JSON, but direct
                # planner adapters can provide arbitrary Python mappings.
                # Keep restoration as strict as the main patch validator:
                # names must be strings and booleans are never production text.
                if (not isinstance(raw_name, str)):
                    continue
                name = raw_name.strip()
                if (name and name not in DIMENSION_DEFAULTS and name in new_product_specs
                        and value not in (None, "") and self._is_patch_scalar(value)):
                    normalized = str(value).strip()
                    if normalized:
                        incoming_specs[name] = normalized
            incoming_dimensions: dict[str, Any] | None = None
            requested_dimensions = changes.get("dimensions") if isinstance(changes.get("dimensions"), dict) else None
            if requested_dimensions is not None:
                incoming_dimensions = {
                    name: value for name, value in requested_dimensions.items()
                    if isinstance(name, str) and name in DIMENSION_DEFAULTS
                    and value not in (None, "") and self._is_patch_scalar(value)
                }
            for raw_name, value in requested_specs.items():
                if isinstance(raw_name, str) and raw_name in DIMENSION_DEFAULTS \
                        and value not in (None, "") and self._is_patch_scalar(value):
                    if incoming_dimensions is None:
                        incoming_dimensions = {}
                    incoming_dimensions.setdefault(raw_name, value)
            target["productSpecs"] = {}
            target["dimensions"] = deepcopy(DIMENSION_DEFAULTS)
            if incoming_specs:
                target["productSpecs"] = incoming_specs
            if incoming_dimensions is not None:
                target["dimensions"] = merge_dimension_patch(target["dimensions"], incoming_dimensions)
            # Re-run migration after restoring the new product's specs so
            # package/expanded/die-cut aliases populate canonical dimensions.
            normalize_order_dimensions(target)
            changed.add("productSpecs")
            changed.add("dimensions")
            spec_prefix = f"{meta_prefix}productSpecs."
            for field in list(self.state.get("fieldMeta") or {}):
                if field.startswith(spec_prefix):
                    self.state["fieldMeta"].pop(field, None)
        if settle is not None:
            settle()
        # Deterministic conflict ordering: ``changed`` is a set, and hashing
        # varies per process, which would otherwise reorder the conflict log.
        for key in sorted(changed):
            if key == "productSpecs":
                before = previous.get("productSpecs") or {}
                after = target.get("productSpecs") or {}
                for name in sorted(set(before) | set(after)):
                    field = f"{meta_prefix}productSpecs.{name}"
                    old_value, new_value = before.get(name, ""), after.get(name, "")
                    if old_value == new_value and name not in (changes.get("productSpecs") or {}):
                        continue
                    if old_value and new_value and old_value != new_value:
                        self._record_conflict(field, old_value, new_value, source)
                    self._set_field_meta(field, new_value, source, confidence, field_confidence, field_evidence)
            elif key == "dimensions":
                before = previous.get("dimensions") or {}
                after = target.get("dimensions") or {}
                for name in DIMENSION_DEFAULTS:
                    field = f"{meta_prefix}dimensions.{name}"
                    old_value, new_value = before.get(name, ""), after.get(name, "")
                    if old_value == new_value and name not in (changes.get("dimensions") or {}) and name not in (changes.get("productSpecs") or {}):
                        continue
                    if old_value and new_value and old_value != new_value:
                        self._record_conflict(field, old_value, new_value, source)
                    self._set_field_meta(field, new_value, source, confidence, field_confidence, field_evidence)
            else:
                field = f"{meta_prefix}{key}"
                old_value, new_value = previous.get(key), target.get(key)
                if old_value and new_value and old_value != new_value:
                    self._record_conflict(field, old_value, new_value, source)
                self._set_field_meta(field, new_value, source, confidence, field_confidence, field_evidence)
        # Explicit re-confirmation of an unchanged dictionary still resolves
        # that physical field. Propagate its evidence/grade only to aliases of
        # that field, preserving every unrelated measurement's provenance.
        for name, (_, origin) in dimension_inputs.items():
            key = f"{meta_prefix}dimensions.{name}"
            if source != "user" and not self._has_field_signal(f"{meta_prefix}{origin}", field_confidence, field_evidence):
                continue
            value = (target.get("dimensions") or {}).get(name)
            grade = self._resolve_confidence(f"{meta_prefix}{origin}", confidence, field_confidence)
            evidence = self._resolve_evidence(f"{meta_prefix}{origin}", field_evidence)
            aliases = [key]
            if name == size_dimension_key(target.get("size"), str(target.get("productType") or "")):
                aliases.append(f"{meta_prefix}size")
            aliases.extend(f"{meta_prefix}productSpecs.{alias}" for alias, dimension in DIMENSION_SPEC_ALIASES.items()
                           if dimension == name and alias in (target.get("productSpecs") or {}))
            for alias in aliases:
                self._set_field_meta(alias, value, source, grade,
                                     field_evidence={alias: evidence} if evidence else None)
        self._record_rejected_fields(rejected_fields)
        return changed

    def _update_item(self, index: int, changes: dict[str, Any], source: str = "rule",
                     confidence: float = 0.84,
                     field_confidence: dict[str, float] | None = None,
                     field_evidence: dict[str, dict[str, str]] | None = None) -> set[str]:
        """Apply a constrained patch to one product item in a multi-product order."""
        # A caller may have loaded or constructed state directly instead of
        # going through Memory.load.  Repair IDs before deriving the provenance
        # prefix so a duplicate item cannot record the patch under the wrong
        # occurrence.
        initial_mapping: list[dict[str, Any]] = []
        normalize_order_items(self.state["order"], id_mapping=initial_mapping)
        migrate_state_item_references(self.state, initial_mapping)
        items = self.state["order"].get("items")
        if not isinstance(items, list) or not (0 <= index < len(items)) or not isinstance(changes, dict):
            return set()
        previous = deepcopy(items[index])
        previous_items = deepcopy(items)
        item = deepcopy(previous)
        item_id = item.get("itemId") or f"item-{index + 1}"

        def settle() -> None:
            items[index] = item
            id_mapping: list[dict[str, Any]] = []
            normalize_order_items(self.state["order"], id_mapping=id_mapping)
            migrate_state_item_references(self.state, id_mapping, previous_items=previous_items)

        changed = self._apply_patch(
            item, previous, changes, source=source, confidence=confidence,
            field_confidence=field_confidence, field_evidence=field_evidence,
            allowed_keys=ITEM_PATCH_KEYS,
            meta_prefix=f"items.{item_id}.", settle=settle)
        # normalize_order_items rebuilt the items list with fresh dicts; work
        # on the live item from here on.
        item = self.state["order"]["items"][index]
        if changed & RECOMMENDATION_FIELDS:
            item["selectedOption"] = None
            item["orderGenerated"] = False
            self._clear_process_options(index)
            self._mark_preflight_stale(index, "该产品项订单字段发生变化")
            self._invalidate_delivery_state()
            self.state["stage"] = "recommend"
            self.state["workflowStage"] = "recommend"
        return changed

    def _update_order(self, changes: dict[str, Any], source: str = "rule", confidence: float = 0.84,
                      field_confidence: dict[str, float] | None = None,
                      field_evidence: dict[str, dict[str, str]] | None = None) -> set[str]:
        order = self.state["order"]
        previous = deepcopy(order)
        previous_items = deepcopy(order.get("items")) if isinstance(order.get("items"), list) else None
        # Order-only list patches: items must be normalized as whole items, and
        # a new items list derives the productTypes summary.
        list_fields: dict[str, Any] = {}
        for key in ("productTypes", "items"):
            if key not in changes or not isinstance(changes.get(key), list):
                continue
            candidate = deepcopy(order)
            candidate[key] = deepcopy(changes[key])
            if key == "items":
                normalize_order_items(candidate)
                normalized_items = candidate["items"]
            else:
                normalized_items = deepcopy(changes[key])
            if normalized_items != order.get(key):
                list_fields[key] = normalized_items
                if key == "items":
                    next_types = candidate.get("productTypes", [])
                    if next_types != order.get("productTypes", []):
                        list_fields["productTypes"] = next_types
        def settle() -> None:
            id_mapping: list[dict[str, Any]] = []
            normalize_order_items(order, id_mapping=id_mapping)
            migrate_state_item_references(self.state, id_mapping, previous_items=previous_items)

        changed = self._apply_patch(
            order, previous, changes, source=source, confidence=confidence,
            field_confidence=field_confidence, field_evidence=field_evidence,
            allowed_keys=ORDER_PATCH_KEYS,
            list_fields=list_fields, settle=settle)
        # First turn from a single-product order to product items: relocate the
        # product-scoped provenance that accumulated at the top level so the
        # item-scoped views find it under ``items.{itemId}.*``.
        if not (isinstance(previous_items, list) and previous_items):
            self._migrate_single_product_provenance(previous, changed)
        # Replacing the item list can be a pure identity repair/migration. In
        # that case ``migrate_state_item_references`` has already moved cached
        # options to the repaired IDs; preserve those buckets for unchanged
        # items instead of discarding the whole multi-item cache.
        order_core_changes = changed & (RECOMMENDATION_FIELDS - {"items", "productTypes"})
        if order_core_changes:
            self.state["selectedOption"] = None
            self._clear_process_options()
            self._mark_preflight_stale(reason="订单关键字段发生变化")
            self._invalidate_delivery_state()
            if self.state["stage"] == "confirm": self.state["stage"] = "recommend"
            self.state["workflowStage"] = "recommend"
        elif changed & {"items", "productTypes"}:
            self._mark_preflight_stale(reason="产品项列表发生变化")
            self._invalidate_delivery_state()
            if self.state["stage"] == "confirm": self.state["stage"] = "recommend"
            self.state["workflowStage"] = "recommend"
        elif "platform" in changed:
            # A platform switch can invalidate a previously mapped handoff even
            # when the production recommendation itself remains unchanged.
            self._invalidate_delivery_state()
            if self.state["stage"] == "confirm": self.state["stage"] = "recommend"
            self.state["workflowStage"] = "recommend"
        return changed

    def _migrate_single_product_provenance(self, previous_order: dict[str, Any],
                                           changed: set[str]) -> None:
        """TD-02: move product-scoped provenance into item keys at split time.

        While the order is still single-product, conversation provenance
        accumulates under top-level ``fieldMeta`` keys (``paper``, ``size``,
        ``dimensions.*``, ``productSpecs.*``, ``quantity``...).  When a patch
        first turns the order into product items, the item badge and confidence
        views read ``items.{itemId}.*`` keys and would find nothing.

        Attribution follows perception mention order: items are listed in the
        order their products were mentioned, so the pre-split order product
        (``previous_order["productType"]``) maps to the first item carrying the
        same ``productType``.  Entries whose value was written by the splitting
        patch itself (``changed``) describe the new first-item compatibility
        projection and therefore follow item 1 instead.  Entries that cannot be
        attributed stay at the top level: no pre-split product, no item with a
        matching product type, or keys that never describe one product
        (``productType``, ``productTypes``, ``platform``, ``purpose``,
        ``budget``, ``deadline`` and the whole item list).  Evidence,
        confidence and status are moved verbatim — never recomputed — and item
        order values are not touched here; this only relocates provenance.
        Key iteration is ``sorted()`` so the relocation never depends on hash
        order.
        """
        items = self.state["order"].get("items")
        if not isinstance(items, list) or not items \
                or not isinstance(items[0], dict):
            return
        field_meta = self.state.get("fieldMeta")
        if not isinstance(field_meta, dict):
            return
        first_prefix = f"items.{items[0].get('itemId') or 'item-1'}."
        product = str(previous_order.get("productType") or "").strip()
        target_prefix = ""
        if product:
            target_index = next((index for index, item in enumerate(items)
                                 if isinstance(item, dict)
                                 and str(item.get("productType") or "").strip() == product), None)
            if target_index is not None:
                target_prefix = f"items.{items[target_index].get('itemId') or f'item-{target_index + 1}'}."
        migrated_keys: list[str] = []
        for key in sorted(field_meta):
            if not isinstance(key, str) or key.startswith("items.") \
                    or not self._is_product_scoped_meta_key(key) \
                    or not isinstance(field_meta[key], dict):
                continue
            prefix = first_prefix if key in changed else target_prefix
            if not prefix:
                continue
            # A same-turn item-scoped parse can already hold a more precise
            # entry than the coarse top-level copy (e.g. size 0.9 vs 0.55);
            # relocation fills gaps and must never downgrade such entries.
            entry = field_meta.pop(key)
            item_key = prefix + key
            if item_key not in field_meta:
                field_meta[item_key] = deepcopy(entry)
            migrated_keys.append(key)
        if not migrated_keys:
            return
        # The evidence drawer keeps its audit entries under the owning item.
        first_id = str(items[0].get("itemId") or "item-1")
        target_id = target_prefix[len("items."):-1]
        migrated = set(migrated_keys)
        # The audit list shares candidate dicts with the live turn's local
        # candidate list (chat builds state["fieldCandidates"] by
        # concatenation).  Rebuild with renamed copies instead of mutating in
        # place, or the item-scoped meta writes later in the same turn would
        # replay the top-level candidates under their new item names and
        # overwrite higher-confidence entries with coarse top-level grades.
        rebuilt = []
        for candidate in self.state.get("fieldCandidates", []):
            if not isinstance(candidate, dict):
                rebuilt.append(candidate)
                continue
            field = candidate.get("field")
            if field not in migrated:
                rebuilt.append(candidate)
                continue
            field = str(field)
            renamed = deepcopy(candidate)
            if field in changed:
                renamed["field"] = first_prefix + field
                renamed["scope"] = first_id
            elif target_prefix:
                renamed["field"] = target_prefix + field
                renamed["scope"] = target_id
            else:
                rebuilt.append(candidate)
                continue
            rebuilt.append(renamed)
        self.state["fieldCandidates"] = rebuilt[-128:]

    @staticmethod
    def _bridge_patch_paths(patch: dict[str, Any]) -> list[str]:
        """Return deterministic accepted field paths for bridge receipts."""
        paths: list[str] = []
        if not isinstance(patch, dict):
            return paths
        for key, value in patch.items():
            if not isinstance(key, str):
                continue
            if key in {"productSpecs", "dimensions"} and isinstance(value, dict):
                for name in value:
                    if isinstance(name, str) and name.strip():
                        paths.append(f"{key}.{name.strip()}"[:256])
                continue
            paths.append(key.strip()[:256])
        return list(dict.fromkeys(path for path in paths if path))

    def apply_order_patch(self, plan: dict[str, Any],
                          item_index: int | None = None,
                          expected_revision: int | None = None,
                          patch_id: str | None = None,
                          patch_digest: str | None = None,
                          source: str = "model") -> dict[str, Any]:
        """Apply one normalized dsh plan through the shared patch kernel.

        The MCP adapter performs transport/schema validation and calls this
        method while holding the session lock. This method deliberately does
        not accept a complete order replacement: callers can only provide the
        normalized patch returned by the planner validator.
        """
        if not isinstance(plan, dict) or not isinstance(plan.get("patch"), dict):
            raise ValueError("patch plan must be a normalized object")
        if source not in {"model", "user", "rule", "recommendation"}:
            raise ValueError("unsupported patch source")
        before_state = deepcopy(self.state)
        previous_run = (self.run_id, self.run_operation, deepcopy(self.run_events),
                        list(self.trace))
        previous_revision = self.state.get("revision", 0)
        self._begin_run("apply_order_patch")
        self.trace = []
        try:
            patch = plan.get("patch") or {}
            self._record_plan_meta(plan)
            self._record_rejected_fields(plan.get("rejectedFields"))
            rejected_before = set(self.state.get("rejectedFields") or [])
            field_confidence = plan.get("confidence") if isinstance(plan.get("confidence"), dict) else None
            field_evidence = self._normalize_field_evidence(plan.get("evidence"))
            accepted = self._bridge_patch_paths(patch)
            changed: set[str]
            if item_index is not None:
                items = self.state.get("order", {}).get("items")
                if not isinstance(items, list) or not (0 <= item_index < len(items)):
                    raise ValueError("itemIndex out of range")
                item_id = (items[item_index].get("itemId")
                           if isinstance(items[item_index], dict) else None) or f"item-{item_index + 1}"
                changed = self._update_item(
                    item_index, patch, source=source, confidence=0.68,
                    field_confidence=field_confidence, field_evidence=field_evidence)
                accepted = [f"items.{item_id}.{path}"[:256] for path in accepted]
                changed_fields = [
                    f"items.{item_id}.{key}"[:256] for key in sorted(changed)
                ]
            else:
                changed = self._update_order(
                    patch, source=source, confidence=0.68,
                    field_confidence=field_confidence, field_evidence=field_evidence)
                changed_fields = sorted(str(key)[:256] for key in changed)
            rejected = [str(value)[:256] for value in (plan.get("rejectedFields") or [])
                        if isinstance(value, str) and value.strip()]
            rejected = list(dict.fromkeys(rejected))
            # The planner validator may accept a product-spec key using the
            # union profile while the item-scoped kernel rejects it for the
            # selected product. Reflect the kernel's actual rejection in the
            # receipt instead of claiming that field was accepted.
            newly_rejected = [
                value for value in (self.state.get("rejectedFields") or [])
                if value not in rejected_before and isinstance(value, str)
            ]
            for value in newly_rejected:
                if value not in rejected:
                    rejected.append(value[:256])
            if item_index is not None:
                accepted = [
                    path for path in accepted
                    if path not in set(rejected)
                    and not any(path.endswith("." + value) for value in rejected)
                ]
            else:
                accepted = [path for path in accepted if path not in set(rejected)]
            predicted_revision = (
                min(int(previous_revision) + 1, MAX_STATE_REVISION)
                if isinstance(previous_revision, int) and not isinstance(previous_revision, bool)
                else None
            )
            self._event(
                "patch", "ok" if accepted else "rejected",
                "已通过受控 bridge 应用字段 patch" if accepted else "patch 没有可应用字段",
                patchId=patch_id, expectedRevision=expected_revision,
                patchDigest=patch_digest,
                previousRevision=previous_revision, revision=predicted_revision,
                acceptedFields=accepted, changedFields=changed_fields,
                rejectedFields=rejected,
                knowledgeVersion=plan.get("knowledgeVersion") or None,
            )
            applied = bool(accepted)
            tool_result = {
                "status": "applied" if applied else "rejected",
                **({} if applied else {"reason": "no_valid_fields"}),
                "changedFields": changed_fields,
                "acceptedFields": accepted,
                "rejectedFields": rejected,
                "knowledgeVersion": KNOWLEDGE_VERSION,
                "reportedKnowledgeVersion": plan.get("knowledgeVersion", ""),
            }
            message = ("已通过受控 bridge 应用订单字段 patch。"
                       if applied else "patch 中没有可应用的字段，未写入订单。")
            response = self._result([message],
                                    tool_result=tool_result)
            revision = self.state.get("revision", previous_revision)
            response["revision"] = revision
            response["acceptedFields"] = accepted
            response["rejectedFields"] = rejected
            if isinstance(response.get("toolResult"), dict):
                response["toolResult"]["revision"] = revision
            return response
        except Exception:
            self.state = before_state
            self.run_id, self.run_operation, self.run_events, self.trace = previous_run
            self._cas_expected_revision = None
            raise

    @staticmethod
    def _merge_repeated_product_mentions(perceived: dict[str, Any], confidence: dict[str, float],
                                         candidates: list[dict[str, Any]] | None = None) -> None:
        """Collapse repeated mentions of one product into its first item, in place.

        ``nlu.perceive`` builds one item per catalog mention.  A common phrasing
        restates the product to attach its parameters ("做名片和海报，名片90×54mm…，
        海报A2…"), which would otherwise keep an empty stub item plus a filled
        duplicate for the same product.  A later mention of an already-seen
        product therefore updates that product's first item instead of adding a
        second one: later non-empty values win, empty values never erase, and
        the per-mention confidence grades travel with the values they graded.
        One message with N distinct products keeps yielding exactly N items.

        Runs after :func:`field_candidates` so every mention's evidence window
        was computed against its own clause; matching item-scoped candidates
        are re-prefixed to the surviving item alongside the patch and grades.
        """
        items = perceived.get("items")
        if not isinstance(items, list) or len(items) < 2:
            return
        merged: list[Any] = []
        merged_grades: list[dict[str, float]] = []
        mention_target: dict[int, int] = {}
        item_index_by_product: dict[str, int] = {}
        changed = False
        for mention_index, item in enumerate(items):
            source_prefix = f"items.item-{mention_index + 1}."
            if not isinstance(item, dict):
                merged.append(item)
                merged_grades.append({})
                continue
            product = str(item.get("productType") or "").strip()
            target = item_index_by_product.get(product) if product else None
            if target is None:
                if product:
                    item_index_by_product[product] = len(merged)
                mention_target[mention_index] = len(merged)
                merged.append(deepcopy(item))
                merged_grades.append({key[len(source_prefix):]: grade
                                      for key, grade in confidence.items()
                                      if isinstance(key, str) and key.startswith(source_prefix)})
                continue
            changed = True
            mention_target[mention_index] = target
            base = merged[target]
            base_grades = merged_grades[target]
            for key, value in item.items():
                if key == "productType" or value in (None, "") or base.get(key) == value:
                    continue
                if isinstance(value, dict):
                    slot = base.get(key)
                    if not isinstance(slot, dict):
                        slot = {}
                        base[key] = slot
                    for name, entry in value.items():
                        if entry in (None, "") or slot.get(name) == entry:
                            continue
                        slot[name] = deepcopy(entry)
                        grade = confidence.get(f"{source_prefix}{key}.{name}",
                                               confidence.get(f"{source_prefix}{key}"))
                        if grade is not None:
                            base_grades[f"{key}.{name}"] = grade
                    continue
                base[key] = deepcopy(value)
                grade = confidence.get(f"{source_prefix}{key}")
                if grade is not None:
                    base_grades[key] = grade
        if not changed:
            return
        perceived["items"] = merged
        # Scalar top-level fields are a compatibility projection of item 1
        # (see ``nlu.perceive``).  Re-derive it from the merged first item so
        # the projection cannot keep describing a pre-merge mention stub.
        for key in ("quantity", "quantityValue", "quantityUnit", "size", "dimensions",
                    "paper", "printing", "finishing", "binding", "productSpecs"):
            value = merged[0].get(key) if merged and isinstance(merged[0], dict) else None
            perceived.pop(key, None)
            if value not in (None, ""):
                perceived[key] = deepcopy(value)
        for key in [key for key in confidence if isinstance(key, str) and key.startswith("items.item-")]:
            del confidence[key]
        for index, grades in enumerate(merged_grades):
            for key, grade in grades.items():
                confidence[f"items.item-{index + 1}.{key}"] = grade
        for candidate in candidates or []:
            if not isinstance(candidate, dict):
                continue
            field = candidate.get("field")
            if not isinstance(field, str) or not field.startswith("items.item-"):
                continue
            parts = field.split(".", 2)
            if len(parts) < 3:
                continue
            try:
                mention = int(parts[1].removeprefix("item-")) - 1
            except ValueError:
                continue
            target = mention_target.get(mention)
            if target is None or target == mention:
                continue
            candidate["field"] = f"items.item-{target + 1}.{parts[2]}"
            candidate["scope"] = f"item-{target + 1}"

    def _apply_plan(self, plan: dict[str, Any]) -> set[str]:
        self._record_plan_meta(plan)
        patch = plan.get("patch") if isinstance(plan, dict) else None
        if not isinstance(patch, dict):
            if isinstance(plan, dict):
                self._record_rejected_fields(plan.get("rejectedFields"))
            return set()
        field_confidence = plan.get("confidence") if isinstance(plan.get("confidence"), dict) else None
        field_evidence = self._normalize_field_evidence(plan.get("evidence"))
        self._record_rejected_fields(plan.get("rejectedFields"))
        items = self.state.get("order", {}).get("items")
        active_index = self._item_index() if isinstance(items, list) and len(items) > 1 else None
        if active_index is not None:
            changed = self._update_item(
                active_index, patch, source="model", confidence=0.68,
                field_confidence=field_confidence, field_evidence=field_evidence)
        else:
            changed = self._update_order(
                patch, source="model", confidence=0.68,
                field_confidence=field_confidence, field_evidence=field_evidence)
        if changed:
            event_data: dict[str, Any] = {"changedFields": sorted(changed)}
            rejected = self.state.get("rejectedFields") or []
            if rejected:
                event_data["rejectedFields"] = deepcopy(rejected)
            self._event("plan", "ok", "模型提出了受限字段更新", **event_data)
        return changed
