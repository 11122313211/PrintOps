"""Allowlist validation for model-proposed patches, tool calls, confidence, and evidence."""

from __future__ import annotations

import math
import re
from typing import Any

from printops.domain.order_model import DIMENSION_DEFAULTS
from printops.domain.knowledge import known_product_spec_keys


class PlanValidatorMixin:
    """Strict allowlist validation ensuring model output stays advisory/candidate-only."""

    @staticmethod
    def _is_patch_scalar(value: Any) -> bool:
        """Only admit finite text/numeric scalars into the model patch."""
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return False
        return not (isinstance(value, float) and not math.isfinite(value))

    @classmethod
    def _normalize_plan_annotation(cls, value: Any) -> str | dict[str, str] | None:
        """Keep advisory skill questions/risks small and display-oriented.

        These fields are useful for dsh skill output, but they are not an
        authority channel.  Drop arbitrary nested model data before the plan
        reaches the Agent's persisted state.
        """
        if isinstance(value, str):
            text = value.strip()
            return text[:cls.MAX_PLAN_ANNOTATION_TEXT] if text else None
        if not isinstance(value, dict):
            return None
        result: dict[str, str] = {}
        for key in ("field", "question", "text", "message", "risk", "severity", "source", "code"):
            raw = value.get(key)
            if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
                continue
            if isinstance(raw, float) and not math.isfinite(raw):
                continue
            text = str(raw).strip()
            if text:
                result[key] = text[:cls.MAX_PLAN_ANNOTATION_TEXT]
        return result or None

    @classmethod
    def _normalize_plan_annotations(cls, raw: Any) -> list[str | dict[str, str]]:
        if not isinstance(raw, list):
            return []
        result: list[str | dict[str, str]] = []
        for item in raw[:cls.MAX_PLAN_ANNOTATIONS]:
            normalized = cls._normalize_plan_annotation(item)
            if normalized is not None:
                result.append(normalized)
        return result

    @classmethod
    def _normalize_plan_version(cls, raw: Any) -> str:
        if not isinstance(raw, str):
            return ""
        return raw.strip()[:cls.MAX_PLAN_META_VERSION]

    @classmethod
    def validate_plan(cls, value: Any, tool_names: set[str] | None = None) -> dict[str, Any] | None:
        """Normalize the model contract before it can mutate order state or call a tool."""
        if not isinstance(value, dict):
            return None
        reply = value.get("reply")
        reply = str(reply).strip() if reply is not None else ""
        if len(reply) > 4000:
            reply = reply[:4000].rstrip() + "..."
        raw_patch = value.get("patch")
        patch: dict[str, Any] = {}
        rejected_fields: list[str] = []
        product_hint = ""
        if isinstance(raw_patch, dict) and isinstance(raw_patch.get("productType"), str):
            product_hint = raw_patch.get("productType", "").strip()
        allowed_specs = known_product_spec_keys(product_hint or None)
        # Dimension aliases are accepted by the Agent's canonical dimensions
        # gateway even though they are not product-specific catalog questions.
        allowed_specs.update(DIMENSION_DEFAULTS)
        if isinstance(raw_patch, dict):
            for raw_key, item in raw_patch.items():
                # JSON object names are strings, but custom planner adapters
                # can call this validator with a Python mapping directly.
                # Reject non-string names before set membership or string
                # operations so malformed payloads fail closed instead of
                # escaping as a TypeError.
                if not isinstance(raw_key, str):
                    rejected_fields.append("<non-string-patch-key>")
                    continue
                key = raw_key
                if key not in cls.PATCH_FIELDS:
                    if isinstance(key, str) and key.strip():
                        rejected_fields.append(key.strip()[:256])
                    continue
                if item is None:
                    continue
                if key == "productSpecs":
                    if not isinstance(item, dict):
                        rejected_fields.append("productSpecs")
                        continue
                    specs: dict[str, str] = {}
                    for raw_name, raw_spec in item.items():
                        if not isinstance(raw_name, str):
                            rejected_fields.append("productSpecs.<non-string-key>")
                            continue
                        name = raw_name.strip()
                        if not name:
                            continue
                        field_name = f"productSpecs.{name}"[:256]
                        if name not in allowed_specs:
                            rejected_fields.append(field_name)
                            continue
                        if raw_spec is None:
                            continue
                        if not cls._is_patch_scalar(raw_spec):
                            rejected_fields.append(field_name)
                            continue
                        spec = str(raw_spec).strip()
                        if spec:
                            specs[name] = spec[:cls.MAX_EVIDENCE_QUOTE]
                    if specs:
                        patch[key] = specs
                elif key == "dimensions":
                    if not isinstance(item, dict):
                        rejected_fields.append("dimensions")
                        continue
                    dimensions: dict[str, str] = {}
                    for raw_name, raw_value in item.items():
                        if not isinstance(raw_name, str):
                            rejected_fields.append("dimensions.<non-string-key>")
                            continue
                        name = raw_name.strip()
                        field_name = f"dimensions.{name}"[:256]
                        if name not in DIMENSION_DEFAULTS:
                            rejected_fields.append(field_name)
                            continue
                        if raw_value is None:
                            continue
                        if not cls._is_patch_scalar(raw_value):
                            rejected_fields.append(field_name)
                            continue
                        normalized = str(raw_value).strip()
                        if normalized:
                            dimensions[name] = normalized[:cls.MAX_EVIDENCE_QUOTE]
                    if dimensions:
                        patch[key] = dimensions
                elif cls._is_patch_scalar(item):
                    try:
                        text = str(item).strip()
                    except (OverflowError, ValueError):
                        rejected_fields.append(str(key).strip()[:256])
                        continue
                    text = text[:cls.MAX_PATCH_VALUE]
                    if text:
                        patch[key] = text
                else:
                    rejected_fields.append(str(key).strip()[:256])
        raw_tool = value.get("tool")
        tool: dict[str, Any] | None = None
        if isinstance(raw_tool, dict):
            name = raw_tool.get("name")
            arguments = raw_tool.get("arguments", {})
            if isinstance(name, str) and name in (tool_names or set()) and isinstance(arguments, dict):
                tool = {"name": name, "arguments": arguments}
        confidence = cls._normalize_confidence(value.get("confidence"), patch)
        evidence = cls._normalize_evidence(value.get("evidence"), patch)
        supplied_rejected = value.get("rejectedFields")
        if isinstance(supplied_rejected, list):
            for item in supplied_rejected:
                if isinstance(item, str) and item.strip():
                    rejected_fields.append(item.strip()[:256])
        # Keep audit output deterministic and bounded.  A model can still
        # receive a useful reply when every proposed field was rejected.
        rejected_fields = list(dict.fromkeys(rejected_fields))[:cls.MAX_METADATA_FIELDS]
        metadata_present = any(key in value for key in
                              ("questions", "risks", "knowledgeVersion", "reportedKnowledgeVersion"))
        if not reply and not patch and tool is None and not rejected_fields and not metadata_present:
            return None
        result: dict[str, Any] = {"reply": reply, "patch": patch, "tool": tool}
        if confidence:
            result["confidence"] = confidence
        if evidence:
            result["evidence"] = evidence
        if rejected_fields:
            result["rejectedFields"] = rejected_fields
        # Preserve the common dsh skill envelope after bounding it.  Agent
        # treats these values as advisory ``planMeta`` only; they cannot write
        # order fields or authorize a tool call.
        if "questions" in value:
            result["questions"] = cls._normalize_plan_annotations(value.get("questions"))
        if "risks" in value:
            result["risks"] = cls._normalize_plan_annotations(value.get("risks"))
        if "knowledgeVersion" in value:
            result["knowledgeVersion"] = cls._normalize_plan_version(value.get("knowledgeVersion"))
        if "reportedKnowledgeVersion" in value:
            result["reportedKnowledgeVersion"] = cls._normalize_plan_version(value.get("reportedKnowledgeVersion"))
        raw_native_calls = value.get("_nativeToolCalls")
        normalized_native_calls: list[dict[str, Any]] = []
        if isinstance(raw_native_calls, list):
            for raw_call in raw_native_calls[:cls.MAX_CONTEXT_LEDGER_ENTRIES]:
                call = cls._normalize_native_call(raw_call)
                if call is not None and call["function"]["name"] in (tool_names or set()):
                    normalized_native_calls.append(call)
        native_call = value.get("_nativeToolCall")
        normalized_native_call = cls._normalize_native_call(native_call)
        if (normalized_native_call is not None
                and normalized_native_call["function"]["name"] in (tool_names or set())):
            if not normalized_native_calls:
                normalized_native_calls.append(normalized_native_call)
            result["_nativeToolCall"] = normalized_native_call
        if normalized_native_calls:
            # Preserve all calls for a bounded batch.  The first-call field is
            # retained above for older Agent implementations.
            result["_nativeToolCalls"] = normalized_native_calls
        native_protocol = value.get("_nativeProtocol")
        if isinstance(native_protocol, str) and native_protocol.strip():
            result["_nativeProtocol"] = native_protocol.strip()[:64]
        return result

    @classmethod
    def _normalize_confidence(cls, raw: Any, patch: dict[str, Any]) -> dict[str, float]:
        """Keep only finite confidence grades for fields in the accepted patch."""
        if not isinstance(raw, dict):
            return {}
        accepted = cls._patch_field_paths(patch)
        normalized: dict[str, float] = {}
        for raw_key, raw_value in raw.items():
            if not isinstance(raw_key, str):
                continue
            key = raw_key.strip()
            if not key or not cls._metadata_path_allowed(key, accepted):
                continue
            if isinstance(raw_value, bool):
                continue
            try:
                number = float(raw_value)
            except (OverflowError, TypeError, ValueError):
                continue
            if not math.isfinite(number):
                continue
            normalized[key[:256]] = round(max(0.0, min(1.0, number)), 3)
            if len(normalized) >= cls.MAX_METADATA_FIELDS:
                break
        return normalized

    @classmethod
    def _normalize_evidence(cls, raw: Any, patch: dict[str, Any]) -> dict[str, dict[str, str]]:
        """Normalize dsh evidence arrays or field-to-quote maps.

        The public dsh contract uses an array of ``{field, quote, source}``
        records, while adapters often find a compact ``{field: quote}`` map
        more convenient.  Both are converted to a bounded field map here.
        """
        if not isinstance(raw, (dict, list)):
            return {}
        accepted = cls._patch_field_paths(patch)
        candidates: list[tuple[Any, Any, Any]] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                candidates.append((item.get("field"), item.get("quote", item.get("evidence")), item.get("source")))
        else:
            for field, item in raw.items():
                if isinstance(item, dict):
                    candidates.append((field, item.get("quote", item.get("evidence")), item.get("source")))
                else:
                    candidates.append((field, item, None))
        normalized: dict[str, dict[str, str]] = {}
        for raw_field, raw_quote, raw_source in candidates:
            if not isinstance(raw_field, str):
                continue
            field = raw_field.strip()
            # Evidence is useful only for values this plan is allowed to
            # apply; this also prevents arbitrary metadata keys from leaking
            # into the Agent state.
            if not field or not cls._metadata_path_allowed(field, accepted):
                continue
            if not isinstance(raw_quote, str):
                continue
            quote = raw_quote.strip()
            if not quote:
                continue
            entry = {"quote": quote[:cls.MAX_EVIDENCE_QUOTE]}
            if isinstance(raw_source, str) and raw_source.strip():
                entry["source"] = raw_source.strip()[:cls.MAX_EVIDENCE_SOURCE]
            normalized[field[:256]] = entry
            if len(normalized) >= cls.MAX_METADATA_FIELDS:
                break
        return normalized

    @staticmethod
    def _patch_field_paths(patch: dict[str, Any]) -> set[str]:
        paths: set[str] = set()
        for key in patch:
            if key == "productSpecs" and isinstance(patch.get(key), dict):
                paths.add("productSpecs")
                paths.update(f"productSpecs.{name}" for name in patch[key])
            elif key == "dimensions" and isinstance(patch.get(key), dict):
                paths.add("dimensions")
                paths.update(f"dimensions.{name}" for name in patch[key])
            else:
                paths.add(str(key))
        return paths

    @staticmethod
    def _metadata_path_allowed(field: str, accepted: set[str]) -> bool:
        """Allow an optional stable multi-product item prefix."""
        if field in accepted or field in {"productSpecs", "dimensions"}:
            return True
        parts = field.split(".")
        if len(parts) < 3 or parts[0] != "items":
            return False
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", parts[1]):
            return False
        remainder = ".".join(parts[2:])
        return remainder in accepted or remainder in {"productSpecs", "dimensions"}
