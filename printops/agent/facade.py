"""PrintOps Agent facade assembling modular provenance, patch, planner, gateway, delivery, and context mixins."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
import time
import unicodedata
from typing import Any
import uuid

from printops.domain.knowledge import (
    field_question,
    field_quick_replies,
    paper_spec_key,
    parameter_state,
    product_spec_quick_replies,
)
from printops.domain.nlu import perceive
from printops.domain.order_model import (
    MAX_STATE_REVISION,
    RECOMMENDATION_FIELDS,
    _multi_product_info,
    default_quantity_unit,
    normalize_order_dimensions,
    normalize_state,
)
from printops.domain.perception_candidates import candidate_evidence, field_candidates
from printops.domain.units import normalize_dimension
from printops.runtime.deadline import check_deadline
from printops.supplier.adapters import PLATFORMS
from printops.tools.registry import validate_order
from printops.agent.context_projector import ContextProjectorMixin
from printops.agent.delivery_lifecycle import DeliveryLifecycleMixin
from printops.agent.memory import (
    HISTORY_LIMIT,
    MAX_RUN_EVENTS,
    MAX_RUN_HISTORY,
    Memory,
    RevisionConflictError,
)
from printops.agent.patch_engine import PatchEngineMixin
from printops.agent.planner_orchestrator import PlannerOrchestratorMixin
from printops.agent.provenance import ProvenanceMixin
from printops.agent.tool_gateway import ToolGatewayMixin


class Agent(
    ProvenanceMixin,
    PatchEngineMixin,
    PlannerOrchestratorMixin,
    ToolGatewayMixin,
    DeliveryLifecycleMixin,
    ContextProjectorMixin,
):
    """Stateful, dependency-free print order agent."""

    def __init__(self, memory: Memory, session_id: str | None = None, planner: Any = None) -> None:
        self.memory, self.id, self.planner = memory, session_id or uuid.uuid4().hex[:12], planner
        self.state = memory.load(self.id)
        # ``rejectedFields`` was added after the original state schema; keep
        # older SQLite sessions readable without requiring a schema bump.
        if not isinstance(self.state.get("rejectedFields"), list):
            self.state["rejectedFields"] = []
        if not isinstance(self.state.get("planMeta"), dict):
            self.state["planMeta"] = {}
        if not isinstance(self.state.get("toolReceipts"), list):
            self.state["toolReceipts"] = []
        if not isinstance(self.state.get("processOptions"), (list, dict)):
            self.state["processOptions"] = deepcopy(self.state.get("itemOptions") or []) \
                if isinstance(self.state.get("itemOptions"), (list, dict)) else []
        if not isinstance(self.state.get("preflightResults"), (dict, list)):
            self.state["preflightResults"] = {}
        # The persisted revision advances by _save only when the state
        # (excluding the counter itself) changed. Keeping a canonical
        # snapshot prevents helper methods that save twice during one
        # operation from spuriously advancing the CAS token.
        raw_revision = self.state.get("revision")
        self.state["revision"] = (raw_revision if isinstance(raw_revision, int)
                                   and not isinstance(raw_revision, bool)
                                   and 0 <= raw_revision <= MAX_STATE_REVISION else 0)
        self._saved_state_digest = self._state_digest(self.state)
        self._saved_revision = self.state["revision"]
        self._pending_messages: list[dict[str, Any]] = []
        self.trace: list[str] = []
        self.run_id = ""
        self.run_operation = ""
        self.run_events: list[dict[str, Any]] = []
        self._cas_expected_revision: int | None = None
        self._planner_tool_receipts: list[dict[str, Any]] = []
        # Planner tool calls are nested inside one chat run.  Public tool
        # calls still finalize their own run, while planner calls must leave
        # the outer run open so its trace contains every round.
        self._nested_tool_execution = False

    @staticmethod
    def _state_digest(state: dict[str, Any]) -> str | None:
        """Return a stable digest for revision tracking, excluding revision."""
        state_copy = deepcopy(state)
        state_copy.pop("revision", None)
        try:
            encoded = json.dumps(state_copy, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError):
            # An unusual direct-caller state is treated as changed rather than
            # accidentally reusing a stale optimistic-concurrency token.
            return None
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def _begin_run(self, operation: str) -> None:
        """Start a bounded, inspectable run while keeping the old trace API."""
        self.run_id = uuid.uuid4().hex[:16]
        self.run_operation = operation
        self.run_events = []
        self._planner_tool_receipts = []
        self._event("run", "started", f"开始{operation}")

    def _event(self, step: str, status: str = "ok", detail: str = "", **extra: Any) -> None:
        if not self.run_id:
            return
        event = {"step": step, "status": status, "detail": detail, "at": self._timestamp()}
        event.update({key: value for key, value in extra.items() if value is not None})
        self.run_events.append(event)
        if len(self.run_events) > MAX_RUN_EVENTS:
            self.run_events = self.run_events[-MAX_RUN_EVENTS:]

    def _finish_run(self, status: str = "completed") -> dict[str, Any] | None:
        if not self.run_id:
            return self.state.get("lastRun")
        self._event("run", status, "运行完成" if status == "completed" else "运行结束")
        record = {"runId": self.run_id, "operation": self.run_operation, "status": status,
                  "startedAt": self.run_events[0]["at"] if self.run_events else self._timestamp(),
                  "finishedAt": self._timestamp(), "events": deepcopy(self.run_events)}
        history = list(self.state.get("runHistory") or [])
        history.append(record)
        self.state["runHistory"] = history[-MAX_RUN_HISTORY:]
        self.state["lastRun"] = record
        self.run_id = ""
        self.run_operation = ""
        self.run_events = []
        return record

    def _item_index(self, value: Any = None) -> int | None:
        """Resolve an explicit or active product-item index safely."""
        items = self.state["order"].get("items")
        candidate = self.state.get("activeItemIndex") if value is None else value
        try:
            candidate = int(candidate)
        except (OverflowError, TypeError, ValueError):
            return None
        return candidate if isinstance(items, list) and len(items) > 1 and 0 <= candidate < len(items) else None

    def _item_order(self, index: int) -> dict[str, Any] | None:
        """Build a standalone order view for one item and inherit only shared fields."""
        items = self.state["order"].get("items")
        if not isinstance(items, list) or not (0 <= index < len(items)) or not isinstance(items[index], dict):
            return None
        item = deepcopy(items[index])
        item["items"] = []
        item["productTypes"] = []
        item["platform"] = self.state["order"].get("platform") or "generic"
        # Explicitly shared values are copied to each item during perception.
        # The top-level compatibility fields describe item 1, not all items.
        return item

    def _item_validation(self, index: int) -> dict[str, Any] | None:
        item_order = self._item_order(index)
        if item_order is None:
            return None
        result = validate_order(item_order)
        return {
            "itemId": item_order.get("itemId") or f"item-{index + 1}", "index": index,
            "productType": item_order.get("productType") or "", "ok": bool(result.get("ok")) and not result.get("productMissing"),
            "missing": result.get("missing", []), "productMissing": result.get("productMissing", []),
            "warnings": result.get("warnings", []), "risks": result.get("risks", []),
            "parameters": [{"key": parameter.get("key"), "label": parameter.get("label"),
                            "value": parameter.get("value", ""), "filled": parameter.get("filled", False),
                            "required": parameter.get("required", False)}
                           for parameter in result.get("productProfile", {}).get("parameters", [])
                           if parameter.get("key")],
            "readiness": round((result.get("readiness", 0) + result.get("productReadiness", 0)) / 2),
            "productReadiness": result.get("productReadiness", 0),
        }

    def _recommend_item(self, index: int) -> list[dict[str, str]]:
        item_order = self._item_order(index)
        if item_order is None:
            return []
        validation = validate_order(item_order)
        if not validation.get("ok") or validation.get("productMissing"):
            return []
        options = self._call("recommend_processes", item_order)
        item_id = item_order.get("itemId") or f"item-{index + 1}"
        self.state.setdefault("itemOptions", {})[item_id] = deepcopy(options)
        self._store_process_options(options, index)
        self.state["workflowStage"] = "recommend"
        return options

    def chat(self, text: str, patch: dict[str, str] | None = None, item_index: int | None = None) -> dict[str, Any]:
        self._begin_run("chat")
        self.trace = ["感知需求"]
        self._event("perceive", "ok", "已完成规则感知")
        focus_match = re.search(r"第\s*(\d+)\s*项", text or "")
        target_supplied = item_index is not None or focus_match is not None
        requested_index: Any = item_index
        if requested_index is None and focus_match:
            try:
                requested_index = int(focus_match.group(1)) - 1
            except ValueError:
                requested_index = -1
        # Product-specific follow-ups belong to the item this turn targets.
        # Resolve the hint before perception, including a newly selected item.
        hint_index = requested_index if target_supplied else self.state.get("activeItemIndex")
        hint_items = self.state["order"].get("items")
        product_hint = self.state["order"].get("productType") or ""
        if (isinstance(hint_items, list) and len(hint_items) > 1
                and isinstance(hint_index, int) and not isinstance(hint_index, bool)
                and 0 <= hint_index < len(hint_items) and isinstance(hint_items[hint_index], dict)):
            product_hint = hint_items[hint_index].get("productType") or ""
        perceived, perceived_confidence = self._perceive_full(text, product_hint)
        candidates = field_candidates(text, perceived, perceived_confidence)
        # One item per distinct product: a restated mention ("…，名片90×54mm")
        # attaches parameters to the product's first item instead of splitting
        # a duplicate. Runs before the evidence map so item-scoped evidence
        # keys already use the surviving item's prefix.
        self._merge_repeated_product_mentions(perceived, perceived_confidence, candidates)
        evidence = candidate_evidence(candidates)
        explicit_action = self._has_explicit_action_intent(text)
        if target_supplied:
            items = self.state["order"].get("items")
            if (isinstance(requested_index, bool)
                    or not isinstance(requested_index, int)
                    or not isinstance(items, list)
                    or not (0 <= requested_index < len(items))):
                self._remember("user", text)
                message = "没有找到指定的产品项，请重新选择后再继续。"
                self._remember("assistant", message)
                self._save()
                return self._result([message], tool_result={"status": "blocked", "reason": "item_not_found"})
            self.state["activeItemIndex"] = requested_index
            # A focus-only command should not trigger a second perception pass
            # or overwrite any product fields. Action turns continue below in
            # the newly selected item's context. A negation ("第1项不要300g铜版纸")
            # produces no positive perception but must still reach the negation
            # block below instead of being swallowed by this shortcut (TD-23).
            if not perceived and not patch and not explicit_action \
                    and not any(candidate.get("negated") for candidate in candidates):
                self._remember("user", text)
                product = items[requested_index].get("productType") if isinstance(items[requested_index], dict) else ""
                message = f"已切换到第 {requested_index + 1} 项{f'：{product}' if product else ''}。请继续补充或确认这一项的参数。"
                self._remember("assistant", message)
                self._save()
                return self._result([message])
        if patch is not None and not isinstance(patch, dict):
            patch = {}
        items = self.state["order"].get("items")
        active_item = self.state.get("activeItemIndex")
        target_item = (isinstance(items, list) and isinstance(active_item, int)
                       and 0 <= active_item < len(items) and len(items) > 1)
        if target_item:
            for candidate in candidates:
                candidate["field"] = f"items.{items[active_item]['itemId']}." + candidate["field"]
                candidate["scope"] = items[active_item]["itemId"]
        self.state["fieldCandidates"] = (self.state.get("fieldCandidates", []) + candidates)[-128:]
        if target_item:
            changed_item = self._update_item(active_item, perceived, source="rule", confidence=0.84,
                                             field_confidence=perceived_confidence, field_evidence=evidence)
            if patch:
                changed_item |= self._update_item(active_item, patch, source="user", confidence=1.0)
            changed_fields = [f"items.{items[active_item].get('itemId', f'item-{active_item + 1}')}.{key}" for key in changed_item]
        else:
            changed = self._update_order(perceived, source="rule", confidence=0.84,
                                         field_confidence=perceived_confidence, field_evidence=evidence)
            if patch:
                changed |= self._update_order(patch, source="user", confidence=1.0)
            changed_fields = list(changed)
        # _update_item/_update_order rebuild the items list with fresh dicts
        # via normalize_order_items, so the reference captured above is stale
        # by now. Re-read before negation clears values, or the writes land on
        # an orphaned copy and are silently lost (TD-23).
        items = self.state["order"].get("items")
        # Negation can remove a previously accepted value, but must not delete
        # the replacement supplied in this same turn ("不要A4，改B5").
        target = items[active_item] if target_item else self.state["order"]
        prefix = f"items.{target.get('itemId')}." if target_item else ""
        for candidate in candidates:
            candidate_key = candidate["field"].removeprefix(prefix) if prefix else candidate["field"]
            if not candidate["negated"] or candidate_key in perceived:
                continue
            key, value = candidate_key, str(candidate["value"])
            current = target.get(key) or ""
            # Evidence keeps the original spelling; compare canonical material
            # terminology so "不要300g铜版纸" also removes "300 g/m² 铜版纸".
            from printops.domain.vocab import normalize_print_term
            denied = normalize_dimension(value) if key == "size" else normalize_print_term(key, value)
            normalized_current = normalize_dimension(current) if key == "size" else normalize_print_term(key, current)
            equivalent_denial = re.sub(r"\s+", "", str(denied)) in re.sub(r"\s+", "", str(normalized_current))
            if current and equivalent_denial:
                target[key] = ""
                self.state.get("fieldMeta", {}).pop(prefix + key, None)
                if key == "paper":
                    # The profile parameter mirrors paper; clearing only one
                    # would let normalization restore the removed material.
                    linked = paper_spec_key(target.get("productType"))
                    if linked:
                        target.setdefault("productSpecs", {}).pop(linked, None)
                        self.state.get("fieldMeta", {}).pop(prefix + f"productSpecs.{linked}", None)
                if key == "size":
                    for dimension, prior in list(target.get("dimensions", {}).items()):
                        if prior == current:
                            target["dimensions"][dimension] = ""
                            self.state.get("fieldMeta", {}).pop(prefix + "dimensions." + dimension, None)
                    normalize_order_dimensions(target)
                self._clear_process_options(active_item if target_item else None)
                self.state["selectedOption"] = None
                self._invalidate_delivery_state()
        if "items" in perceived:
            for index, item in enumerate(self.state["order"].get("items", [])):
                for candidate in candidates:
                    source_prefix = f"items.item-{index + 1}."
                    if candidate["field"].startswith(source_prefix) and not candidate["negated"]:
                        key = candidate["field"][len(source_prefix):]
                        self._set_field_meta(f"items.{item['itemId']}.{key}", candidate["value"], "rule",
                                             candidate["confidence"], field_evidence={key: evidence.get(candidate["field"], {})})
        self.state["workflowStage"] = self._base_workflow_stage()
        self._event("memory", "ok", "已更新订单记忆", changedFields=changed_fields)
        self._remember("user", text)

        multi_products = _multi_product_info(self.state["order"])
        if multi_products and not explicit_action:
            self.state["stage"] = "collect"
            self.state["workflowStage"] = "clarify"
            validation = self._call("validate_order", self.state["order"])
            item_validations = validation.get("itemValidations") or []
            current_item = next((item for item in item_validations if item.get("index") == active_item), None)
            item_options: list[dict[str, str]] = []
            if target_item and current_item:
                self._event("clarify", "ok" if current_item.get("ok") else "blocked", "已更新独立产品项",
                            itemId=current_item.get("itemId"), readiness=current_item.get("readiness"))
                missing = list(current_item.get("missing") or []) + list(current_item.get("productMissing") or [])
                item_name = f"（{current_item.get('productType')}）" if current_item.get("productType") else ""
                if missing:
                    message = (f"已更新第 {active_item + 1} 项{item_name}，"
                               f"当前信息度 {current_item.get('readiness', 0)}%。还需确认：{'、'.join(missing)}。")
                else:
                    item_options = self._recommend_item(active_item)
                    message = (f"已更新第 {active_item + 1} 项{item_name}，当前信息度 100%。"
                               f"我为这一项生成了 {len(item_options)} 个工艺方案，请选择后再继续询价。")
            else:
                self._event("clarify", "blocked", "检测到多个产品，需要拆分订单项", products=multi_products)
                message = (f"我识别到多个印刷品：{'、'.join(multi_products)}。"
                            "它们的尺寸、材料和后道不同，当前不能合并成一张订单或直接报价。"
                            "请先分别确认每个产品的数量、尺寸、材料和交期，我会按独立订单项继续。")
            self._remember("assistant", message)
            self._save()
            return self._result([message], options=item_options, tool_result=item_options or validation)

        # Generating a multi-product order remains an aggregate operation. An
        # ordinal only establishes which item the UI should show if generation
        # is blocked; it never creates a partial handoff that bypasses the other
        # items' readiness and selection gates.
        dispatched = self._dispatch_intents(text, "pre_planner")
        if dispatched is not None:
            return dispatched

        # An optional LLM planner improves language understanding while field
        # patches and tool names remain constrained by this Agent.
        planned = self._planner_chat_turn(text)
        if planned is not None:
            return planned

        # Explicit intents let users invoke a tool before the order is complete.
        dispatched = self._dispatch_intents(text, "post_planner")
        if dispatched is not None:
            return dispatched

        missing = self._missing_fields()
        if missing:
            self.state["stage"] = "collect"
            validation = self._call("validate_order", self.state["order"])
            message = f"{self._summary()}\n\n还需要确认：{self._question(missing[0])}"
            quick = self._quick_replies(missing[0], self.state["order"].get("productType"))
            options: list[dict[str, str]] = []
            tool_result: Any = validation
        else:
            self.state["stage"] = "recommend"
            options = self._call("recommend_processes", self.state["order"])
            self._store_process_options(options)
            profile = parameter_state(self.state["order"])
            quick = self._product_quick_replies(profile["missing"][0]["key"]) if profile["missing"] else []
            product_note = (f"基础订单信息已经齐了。为了让{profile.get('category', '该品类')}对接更准确，建议补充：{profile['missing'][0]['question']}"
                            if profile["missing"] else "信息已经齐了。")
            message = f"{self._summary()}\n\n{product_note}\n我调用工艺推荐工具生成了 3 个可执行方案，请选择一个。"
            tool_result = options
        self._remember("assistant", message)
        self._save()
        return self._result([message], quick, options, tool_result=tool_result)

    def _remember(self, role: str, text: str) -> None:
        message = {"role": role, "text": text}
        self.state["messages"] = (self.state["messages"] + [message])[-HISTORY_LIMIT:]
        self._pending_messages.append(deepcopy(message))

    def _save(self) -> None:
        check_deadline()
        current_revision = self.state.get("revision")
        if (not isinstance(current_revision, int) or isinstance(current_revision, bool)
                or current_revision < 0 or current_revision > MAX_STATE_REVISION):
            current_revision = 0
            self.state["revision"] = current_revision
        operation = getattr(self, "_operation_context", None)
        operation_events = []
        if operation:
            receipts = self.state.setdefault("operationReceipts", [])
            receipt = next((value for value in receipts if value.get("operationId") == operation["operationId"]), None)
            if receipt is None:
                receipt = deepcopy(operation)
                receipts.append(receipt)
            receipt.update({"status": "completed" if self._operation_complete else "running",
                            "revision": min(current_revision + 1, MAX_STATE_REVISION)})
            if self._operation_complete:
                receipt.setdefault("completedAt", self._timestamp())
            # The snapshot is a cache, not the idempotency ledger. Keep every
            # unresolved operation and the latest 100 completed operations;
            # every commit also appends its receipt in the same transaction.
            self.state["operationReceipts"] = ([value for value in receipts if value.get("status") != "completed"]
                                               + [value for value in receipts if value.get("status") == "completed"][-100:])
            operation_events = [{"eventType": "operation_receipt", "actor": "api", "payload": deepcopy(receipt)}]
        digest = self._state_digest(self.state)
        if digest != self._saved_state_digest:
            self.state["revision"] = min(current_revision + 1, MAX_STATE_REVISION)
        expected_revision = (self._cas_expected_revision if self._cas_expected_revision is not None
                             else self._saved_revision)
        ok, stored_revision = self.memory.save_if_revision(
            self.id, self.state, expected_revision, new_messages=self._pending_messages or None,
            events=operation_events or None)
        self._cas_expected_revision = None
        if not ok:
            raise RevisionConflictError(expected_revision, stored_revision)
        self._pending_messages = []
        self._saved_revision = self.state["revision"]
        self._saved_state_digest = self._state_digest(self.state)

    @staticmethod
    def _perceive(text: str, allow_multi: bool = True) -> dict[str, Any]:
        """Delegate to :mod:`nlu`; fields only, for legacy call sites/tests."""
        return perceive(text, allow_multi=allow_multi)[0]

    @classmethod
    def _perceive_full(cls, text: str, product_hint: str = "") -> tuple[dict[str, Any], dict[str, float]]:
        """Return fields plus the per-field evidence confidence grade."""
        return perceive(text, product_hint=product_hint)


    @staticmethod
    def _question(key: str) -> str:
        return field_question(key)

    @staticmethod
    def _quick_replies(key: str, product: str | None = None) -> list[dict[str, Any]]:
        return field_quick_replies(key, default_unit=default_quantity_unit(product))

    @staticmethod
    def _product_quick_replies(key: str) -> list[dict[str, Any]]:
        return product_spec_quick_replies(key)
