"""Field provenance, confidence grading, conflict recording, and uncertainty checks."""

from __future__ import annotations

from copy import deepcopy
import math
import re
import unicodedata
from typing import Any

from printops.domain.knowledge import KNOWLEDGE_VERSION, paper_spec_key
from printops.domain.order_model import DIMENSION_DEFAULTS, LABELS, MATERIAL_SPEC_PRODUCTS
from printops.agent.memory import FIELD_SOURCE_LABELS, MAX_PATCH_VALUE


class ProvenanceMixin:
    """Field-level evidence, confidence, conflict tracking, and plan-meta normalization."""

    PRODUCTION_FIELD_KEYS = {"productType", "quantity", "quantityValue", "quantityUnit", "size",
                             "dimensions", "pages", "orientation", "paper", "printing",
                             "finishing", "binding", "deadline"}
    # ``_item_order`` inherits these values from the top-level compatibility
    # projection.  Their top-level provenance therefore applies to a focused
    # multi-product item when that item has no explicit value of its own.
    INHERITED_ITEM_FIELDS = (
        "purpose", "orientation", "paper", "printing", "finishing", "binding",
        "deadline", "budget",
    )
    PRODUCT_SCOPED_META_KEYS = frozenset({
        "quantity", "quantityValue", "quantityUnit", "size", "pages", "paper",
        "printing", "finishing", "binding", "orientation",
    })

    @staticmethod
    def _finite_confidence(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (OverflowError, TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _is_patch_scalar(value: Any) -> bool:
        """Return whether a model/user patch value is safe to stringify.

        Order fields are text or numeric scalars.  Dicts/lists must never be
        coerced with ``str(...)`` because doing so can turn an attacker/model
        payload such as ``{"quantity": 500}`` into an executable field value.
        Booleans are intentionally excluded: none of the current order
        contracts are boolean fields, and ``True``/``False`` would otherwise
        become misleading production text.
        """
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return False
        return not (isinstance(value, float) and not math.isfinite(value))

    def _resolve_confidence(self, key: str, base: float, field_confidence: dict[str, float] | None) -> float:
        """Prefer a planner/NLU field grade over the scalar fallback."""
        fallback = self._finite_confidence(base)
        if fallback is None:
            # Malformed provenance must fail closed as low confidence, while
            # still allowing the rest of the conversation to continue.
            fallback = 0.0
        if not field_confidence:
            return fallback
        candidates = [key]
        if key.startswith("items."):
            remainder = ".".join(key.split(".")[2:])
            candidates.extend([remainder, ".".join(remainder.split(".")[-2:])])
        if key.startswith("productSpecs."):
            candidates.append("productSpecs")
        if key.startswith("dimensions."):
            name = key.split(".", 1)[1]
            candidates.extend([f"productSpecs.{name}", "dimensions"])
            if name == "finishedSize":
                candidates.append("size")
            elif name == "packageSize":
                candidates.extend(["productSpecs.boxSize", "productSpecs.bagSize", "size"])
        if key in {"quantityValue", "quantityUnit"}:
            candidates.append("quantity")
        for candidate in candidates:
            if candidate not in field_confidence:
                continue
            number = self._finite_confidence(field_confidence[candidate])
            if number is not None:
                return number
        return fallback

    @staticmethod
    def _normalize_evidence_entry(value: Any) -> dict[str, str] | None:
        """Normalize one dsh evidence record and bound untrusted strings."""
        if isinstance(value, str):
            quote = value.strip()
            source = ""
        elif isinstance(value, dict):
            raw_quote = value.get("quote", value.get("evidence", ""))
            # Evidence quotes are display/audit text, not arbitrary JSON.  Do
            # not stringify lists, mappings, or booleans into misleading
            # provenance that could later look like user confirmation.
            if not isinstance(raw_quote, str):
                return None
            quote = raw_quote.strip()
            raw_source = value.get("source", "")
            source = raw_source.strip() if isinstance(raw_source, str) else ""
        else:
            return None
        if not quote:
            return None
        entry = {"quote": quote[:2048]}
        if source:
            entry["source"] = source[:128]
        if isinstance(value, dict):
            for key in ("start", "end"):
                if isinstance(value.get(key), int) and not isinstance(value[key], bool) and value[key] >= 0:
                    entry[key] = value[key]
        return entry

    @classmethod
    def _normalize_field_evidence(cls, value: Any) -> dict[str, dict[str, str]]:
        """Accept dsh's evidence array and compact field-to-quote maps."""
        if not isinstance(value, (dict, list)):
            return {}
        entries: list[tuple[Any, Any]] = []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    entries.append((item.get("field"), item))
        else:
            entries.extend((field, item) for field, item in value.items())
        result: dict[str, dict[str, str]] = {}
        for raw_field, raw_entry in entries:
            if not isinstance(raw_field, str):
                continue
            field = raw_field.strip()
            if not field:
                continue
            normalized = cls._normalize_evidence_entry(raw_entry)
            if normalized is None:
                continue
            result[field[:256]] = normalized
            if len(result) >= 128:
                break
        return result

    @classmethod
    def _resolve_evidence(cls, key: str,
                          field_evidence: dict[str, dict[str, str]] | None) -> dict[str, str] | None:
        if not field_evidence:
            return None
        candidates = [key]
        if key.startswith("items."):
            remainder = ".".join(key.split(".")[2:])
            candidates.extend([remainder, ".".join(remainder.split(".")[-2:])])
        if key.startswith("productSpecs."):
            candidates.append("productSpecs")
        if key.startswith("dimensions."):
            name = key.split(".", 1)[1]
            candidates.extend([f"productSpecs.{name}", "dimensions"])
            if name == "finishedSize":
                candidates.append("size")
            elif name == "packageSize":
                candidates.extend(["productSpecs.boxSize", "productSpecs.bagSize", "size"])
        if key in {"quantityValue", "quantityUnit"}:
            candidates.append("quantity")
        for candidate in candidates:
            if candidate in field_evidence:
                return deepcopy(field_evidence[candidate])
        return None

    def _has_field_signal(self, key: str,
                          field_confidence: dict[str, float] | None,
                          field_evidence: dict[str, dict[str, str]] | None) -> bool:
        """Tell whether a planner supplied provenance specifically for a field."""
        if self._resolve_evidence(key, field_evidence) is not None:
            return True
        if not field_confidence:
            return False
        candidates = [key]
        if key.startswith("items."):
            remainder = ".".join(key.split(".")[2:])
            candidates.extend([remainder, ".".join(remainder.split(".")[-2:])])
        if key.startswith("productSpecs."):
            candidates.append("productSpecs")
        if key.startswith("dimensions."):
            name = key.split(".", 1)[1]
            candidates.extend([f"productSpecs.{name}", "dimensions"])
            if name == "finishedSize":
                candidates.append("size")
            elif name == "packageSize":
                candidates.extend(["productSpecs.boxSize", "productSpecs.bagSize", "size"])
        if key in {"quantityValue", "quantityUnit"}:
            candidates.append("quantity")
        return any(candidate in field_confidence
                   and self._finite_confidence(field_confidence[candidate]) is not None
                   for candidate in candidates)

    def _set_field_meta(self, key: str, value: Any, source: str, confidence: float,
                        field_confidence: dict[str, float] | None = None,
                        field_evidence: dict[str, dict[str, str]] | None = None) -> None:
        if value in (None, ""):
            self.state.setdefault("fieldMeta", {}).pop(key, None)
            return
        graded = self._resolve_confidence(key, confidence, field_confidence)
        entry: dict[str, Any] = {
            "value": deepcopy(value), "source": source,
            "sourceLabel": FIELD_SOURCE_LABELS.get(source, source),
            "confidence": round(max(0.0, min(1.0, float(graded))), 2),
            "runId": self.run_id or None, "updatedAt": self._timestamp(),
            "status": "confirmed" if source == "user" else "inferred" if graded >= .75 else "pending_confirmation",
        }
        evidence = self._resolve_evidence(key, field_evidence)
        if evidence is None:
            # Re-confirming an unchanged value should not discard its prior
            # evidence, while a changed value naturally receives fresh data.
            previous = self.state.setdefault("fieldMeta", {}).get(key)
            if isinstance(previous, dict) and self._equivalent_value(previous.get("value"), value):
                prior_evidence = previous.get("evidence")
                if isinstance(prior_evidence, dict):
                    evidence = self._normalize_evidence_entry(prior_evidence)
        if evidence is not None:
            entry["evidence"] = evidence
        self.state.setdefault("fieldMeta", {})[key] = entry
        for conflict in self.state.get("conflicts", []):
            if (isinstance(conflict, dict) and conflict.get("field") == key
                    and self._equivalent_value(conflict.get("current"), value)):
                conflict["resolved"] = entry["status"] != "pending_confirmation" and entry["confidence"] >= .75
        if source == "user":
            for candidate in self.state.get("fieldCandidates", []):
                if candidate.get("field") == key and candidate.get("status") == "pending_confirmation":
                    candidate["status"] = "confirmed" if self._equivalent_value(candidate.get("value"), value) else "rejected"
        self._propagate_paper_meta(key, entry)

    def _propagate_paper_meta(self, key: str, entry: dict[str, Any]) -> None:
        """Mirror a ``paper`` provenance update onto the category paper spec.

        TD-21: ``normalize_order_dimensions`` keeps the profile parameter (for
        example 名片材质 ``cardStock``) in lockstep with the generic ``paper``
        value, but re-confirming ``paper`` used to settle only the ``paper``
        conflict, leaving an unresolved mirrored conflict and a stale
        pending meta entry behind.  When the mirrored field exists and already
        carries the mirrored (equivalent) value, it receives the same source,
        grade and evidence as the ``paper`` write, so both fields upgrade
        together.  The mirrored key always ends in ``productSpecs.*``, so this
        branch cannot re-enter itself.  A missing or empty mirror — nothing
        was mirrored yet — and non-equivalent values (an explicit later edit
        of the spec owns that field) are left untouched.  Only provenance is
        written here; the mirrored order value is already synchronized by the
        state normalizer, so no value write and no revision churn occurs.
        """
        key = str(key)
        items = self.state["order"].get("items")
        prefix = ""
        product: Any = ""
        specs: Any = None
        if key == "paper":
            product = self.state["order"].get("productType")
            specs = self.state["order"].get("productSpecs")
        elif key.startswith("items.") and key.endswith(".paper") and isinstance(items, list):
            for item in items:
                # Item IDs may contain dots; match the full generated prefix
                # instead of splitting on a fixed token count.
                if isinstance(item, dict) and key.startswith(f"items.{item.get('itemId')}.paper"):
                    prefix = key[:len(key) - len("paper")]
                    product = item.get("productType")
                    specs = item.get("productSpecs")
                    break
        else:
            return
        spec = paper_spec_key(product)
        if not spec:
            return
        mirror_value = specs.get(spec) if isinstance(specs, dict) else None
        if mirror_value in (None, "") or not self._equivalent_value(mirror_value, entry.get("value")):
            return
        mirror_key = f"{prefix}productSpecs.{spec}"
        evidence = entry.get("evidence")
        self._set_field_meta(mirror_key, mirror_value, str(entry.get("source") or ""),
                             entry.get("confidence"),
                             field_evidence={mirror_key: deepcopy(evidence)}
                             if isinstance(evidence, dict) else None)

    def _record_rejected_fields(self, fields: list[str] | set[str] | tuple[str, ...] | None) -> None:
        """Keep a bounded, de-duplicated audit list of rejected patch paths."""
        if not isinstance(fields, (list, set, tuple)) or not fields:
            return
        current = self.state.setdefault("rejectedFields", [])
        if not isinstance(current, list):
            current = self.state["rejectedFields"] = []
        for raw in fields:
            field = str(raw).strip()
            if field and field not in current:
                current.append(field[:256])
        del current[:-128]

    @staticmethod
    def _normalize_plan_annotation(value: Any) -> str | dict[str, str] | None:
        """Bound one advisory skill question/risk before it reaches a client."""
        if isinstance(value, str):
            text = value.strip()
            return text[:1024] if text else None
        if not isinstance(value, dict):
            return None
        # Keep a small, display-oriented subset; arbitrary nested model data
        # does not belong in the persisted run envelope.
        result: dict[str, str] = {}
        for key in ("field", "question", "text", "message", "risk", "severity", "source", "code"):
            raw = value.get(key)
            if isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
                if isinstance(raw, float) and not math.isfinite(raw):
                    continue
                text = str(raw).strip()
                if text:
                    result[key] = text[:512]
        return result or None

    @classmethod
    def _normalize_plan_meta(cls, value: Any) -> dict[str, Any]:
        """Normalize the common dsh skill envelope without granting authority."""
        if not isinstance(value, dict):
            return {"questions": [], "risks": [], "knowledgeVersion": ""}
        normalized: dict[str, Any] = {"questions": [], "risks": [], "knowledgeVersion": ""}
        for key in ("questions", "risks"):
            raw_items = value.get(key)
            if not isinstance(raw_items, list):
                continue
            items: list[str | dict[str, str]] = []
            for raw_item in raw_items[:32]:
                item = cls._normalize_plan_annotation(raw_item)
                if item is not None:
                    items.append(item)
            normalized[key] = items
        version = value.get("knowledgeVersion")
        if isinstance(version, str):
            normalized["knowledgeVersion"] = version.strip()[:128]
        if isinstance(value.get("reportedKnowledgeVersion"), str):
            normalized["reportedKnowledgeVersion"] = value["reportedKnowledgeVersion"].strip()[:128]
        if normalized["knowledgeVersion"] and normalized["knowledgeVersion"] != KNOWLEDGE_VERSION:
            normalized["risks"].append({
                "code": "knowledge_version_mismatch",
                "severity": "review",
                "message": "模型知识版本与当前 PrintOps 知识版本不一致，需要复核。",
            })
        return normalized

    def _record_plan_meta(self, plan: Any) -> None:
        """Persist advisory skill output separately from the order state."""
        if not isinstance(plan, dict):
            return
        keys = {"questions", "risks", "knowledgeVersion", "reportedKnowledgeVersion"}
        if not keys.intersection(plan):
            return
        self.state["planMeta"] = self._normalize_plan_meta(plan)

    @classmethod
    def _is_production_field(cls, key: str) -> bool:
        """Preference fields (budget/purpose/platform) never block generation."""
        if key.startswith("items."):
            key = ".".join(key.split(".")[2:])
        if key.startswith(("productSpecs.", "dimensions.")):
            return True
        return key in cls.PRODUCTION_FIELD_KEYS

    def _low_confidence_fields(self, production_only: bool = False) -> list[str]:
        metadata = self.state.get("fieldMeta") or {}
        if not isinstance(metadata, dict):
            return []
        fields = []
        for key, meta in metadata.items():
            if not isinstance(meta, dict) or meta.get("value") in (None, "", {}):
                continue
            confidence = self._finite_confidence(meta.get("confidence", 1))
            # A malformed confidence attached to a present field is treated as
            # uncertain instead of raising during generation.
            if confidence is None or confidence < 0.75 or meta.get("status") == "pending_confirmation":
                fields.append(str(key))
        if production_only:
            fields = [key for key in fields if self._is_production_field(key)]
        return fields

    @staticmethod
    def _meta_low_confidence(meta: Any) -> bool:
        """Return whether present field provenance is below the approval bar."""
        if not isinstance(meta, dict) or meta.get("value") in (None, "", {}):
            return False
        try:
            confidence = float(meta.get("confidence", 1))
        except (TypeError, ValueError, OverflowError):
            return True
        return not math.isfinite(confidence) or confidence < 0.75 or meta.get("status") == "pending_confirmation"

    def _uncertain_fields_for_item(self, item_order: dict[str, Any] | None,
                                   item_index: int | None) -> list[str]:
        """Find low-confidence production fields effective for one item.

        A multi-product item can inherit shared values from the top-level
        compatibility order.  Check explicit item provenance first, then only
        apply a top-level grade when the inherited value is equal to the
        selected item's effective value.
        """
        field_meta = self.state.get("fieldMeta") or {}
        if not isinstance(field_meta, dict):
            return []
        if item_order is None or not isinstance(item_index, int):
            return [str(key) for key, meta in field_meta.items()
                    if not str(key).startswith("items.")
                    and self._is_production_field(str(key))
                    and self._meta_low_confidence(meta)]

        item_id = item_order.get("itemId") or f"item-{item_index + 1}"
        prefix = f"items.{item_id}."
        uncertain: list[str] = []

        # Explicit item provenance takes precedence over shared fallback.
        for raw_key, meta in field_meta.items():
            key = str(raw_key)
            if key.startswith(prefix) and self._is_production_field(key) \
                    and self._meta_low_confidence(meta):
                uncertain.append(key)

        top_order = self.state.get("order") or {}

        def top_value_for(key: str) -> Any:
            if key.startswith("dimensions."):
                dimensions = top_order.get("dimensions")
                if isinstance(dimensions, dict):
                    return dimensions.get(key.split(".", 1)[1])
                return None
            return top_order.get(key)

        def has_value(value: Any) -> bool:
            return value not in (None, "", {})

        def inherited_meta(key: str, item_value: Any) -> None:
            if not has_value(item_value) or key not in field_meta:
                return
            # An item-specific grade was already considered above.
            if f"{prefix}{key}" in field_meta:
                return
            top_value = top_value_for(key)
            if has_value(top_value) and self._equivalent_value(item_value, top_value) \
                    and self._is_production_field(key) \
                    and self._meta_low_confidence(field_meta.get(key)):
                uncertain.append(key)

        for key in self.INHERITED_ITEM_FIELDS:
            inherited_meta(key, item_order.get(key))

        # ``size`` and canonical dimensions can also be inherited from a
        # single top-level phrase by ``_item_order``.
        inherited_meta("size", item_order.get("size"))
        item_dimensions = item_order.get("dimensions") if isinstance(item_order.get("dimensions"), dict) else {}
        for key in DIMENSION_DEFAULTS:
            inherited_meta(f"dimensions.{key}", item_dimensions.get(key))

        return list(dict.fromkeys(uncertain))

    def _record_conflict(self, key: str, previous: Any, current: Any, source: str) -> None:
        if previous in (None, "", {}) or current in (None, "", {}) or previous == current:
            return
        conflicts = list(self.state.get("conflicts") or [])
        conflicts.append({"field": key, "label": LABELS.get(key, key), "previous": deepcopy(previous),
                          "current": deepcopy(current), "source": source,
                          "sourceLabel": FIELD_SOURCE_LABELS.get(source, source),
                          "resolved": True, "runId": self.run_id or None, "at": self._timestamp()})
        self.state["conflicts"] = conflicts[-20:]

    @staticmethod
    def _equivalent_value(left: Any, right: Any) -> bool:
        """Ignore harmless spacing/full-width differences from model output."""
        if left in (None, "") or right in (None, ""):
            return left == right
        normalize = lambda value: re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).lower()
        return normalize(left) == normalize(right)

    @classmethod
    def _is_product_scoped_meta_key(cls, key: str) -> bool:
        """Whether one top-level provenance key describes a single product."""
        if key in cls.PRODUCT_SCOPED_META_KEYS:
            return True
        return key.startswith("dimensions.") or key.startswith("productSpecs.")
