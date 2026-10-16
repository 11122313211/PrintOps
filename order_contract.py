"""Version 2 API view of an order. Flat legacy fields remain a read/write facade.

All views derive from the same stored revision; this is not a second store.
Missing and uncertain values are explicit rather than empty strings/defaults.
"""
from copy import deepcopy
import hashlib
import html
import json
import re
from measurement_model import SEMANTICS, parse_measurement
from order_model import DIMENSION_SPEC_ALIASES, size_dimension_key
from unit_normalizer import normalize_dimension
from domain_vocab import normalize_print_term

ORDER_SCHEMA_VERSION = "2.0"
ORDER_STATUSES = frozenset({"draft", "collecting", "ready_for_review", "confirmed", "handoff_ready",
                            "quote_draft", "quote_received", "cancelled", "archived"})
SCALAR_FIELDS = ("productType", "purpose", "quantity", "quantityValue", "quantityUnit", "pages",
                 "orientation", "paper", "printing", "finishing", "binding", "deadline", "budget")


def canonical_order(order, field_meta=None, prefix=""):
    field_meta = field_meta or {}
    def field(key, value):
        meta = field_meta.get(prefix + key) or {}
        status = meta.get("status") or ("confirmed" if meta.get("source") == "user" else "inferred")
        if value in (None, "", {}, []):
            return {"value": None, "status": "missing"}
        if meta.get("confidence", 1) < .75:
            status = "pending_confirmation"
        result = {"value": deepcopy(value) if status != "pending_confirmation" else None, "status": status}
        if status == "pending_confirmation":
            result["candidateValue"] = deepcopy(value)
        if meta:
            result["provenance"] = {name: deepcopy(meta[name]) for name in
                                    ("source", "sourceLabel", "confidence", "runId", "updatedAt") if name in meta}
            if meta.get("evidence"):
                # Raw input lives in fieldMeta/fieldCandidates, outside the
                # production contract. Retain a stable reference to that record.
                result["provenance"]["evidenceRef"] = {"field": prefix + key, "runId": meta.get("runId")}
        return result
    result = {"schemaVersion": ORDER_SCHEMA_VERSION, "unitSystem": "mm",
              "fields": {key: field(key, order.get(key)) for key in SCALAR_FIELDS},
              "measurements": {}, "productSpecs": {}, "items": []}
    for key, semantic in SEMANTICS.items():
        value = parse_measurement((order.get("dimensions") or {}).get(key), semantic)
        result["measurements"][key] = field("dimensions." + key, value)
    for key, value in (order.get("productSpecs") or {}).items():
        if key not in SEMANTICS and key not in DIMENSION_SPEC_ALIASES:
            result["productSpecs"][key] = field("productSpecs." + key, value)
    for index, item in enumerate(order.get("items") or []):
        if not isinstance(item, dict):
            continue
        item_id = item.get("itemId") or f"item-{index + 1}"
        entry = canonical_order(item, field_meta, f"items.{item_id}.")
        entry["itemId"] = item_id
        result["items"].append(entry)
    return result


def production_fingerprint(state):
    """Bind delivery approval to business data, independent of read/run revisions."""
    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if key != "orderGenerated"}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value
    payload = {key: clean(state.get(key)) for key in
               ("order", "selectedOption", "uploadedFile", "uploadedFiles", "preflightResults")}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _document_fields(fields, order):
    """Coalesce equivalent aliases, retaining contradictory values for review."""
    groups = {}
    for field in fields:
        key = field["fieldPath"]
        semantic = ("dimensions." + size_dimension_key(order.get("size"), order.get("productType", ""))
                    if key == "size" else key.removeprefix("productSpecs.")
                    if key in {"productSpecs.pages", "productSpecs.binding"} else key)
        groups.setdefault(semantic, []).append(field)
    projected, conflicts = [], []
    def comparable(field, semantic):
        value = field.get("candidateValue") if field["status"] == "pending_confirmation" else field["value"]
        if value in (None, ""):
            return None
        if semantic.startswith("dimensions."):
            value = normalize_dimension(value)
        elif semantic == "pages":
            match = re.fullmatch(r"\s*(\d+)\s*(?:页|[Pp])?\s*", str(value))
            if match:
                value = match[1]
        elif semantic == "binding":
            value = normalize_print_term("binding", value)
        return re.sub(r"\s+", "", str(value))
    for semantic, aliases in groups.items():
        distinct = {comparable(field, semantic) for field in aliases} - {None}
        conflict = len(distinct) > 1
        primary = min(aliases, key=lambda field: (comparable(field, semantic) is None,
                                                  field["fieldPath"] != semantic))
        keep = aliases if conflict else [primary]
        for field in keep:
            entry = {"key": field["fieldPath"], "fieldId": field["fieldId"], "label": field["label"],
                     "value": deepcopy(field["value"]), "status": field["status"]}
            if conflict:
                entry.update({"label": field["label"] + "（同义字段冲突）", "semanticConflict": True})
            elif len(aliases) > 1:
                entry["aliases"] = [alias["fieldId"] for alias in aliases if alias is not primary]
                if any(alias["status"] == "pending_confirmation" for alias in aliases):
                    entry.update({"value": None, "status": "pending_confirmation"})
            projected.append(entry)
        if conflict:
            conflicts.append(primary)
    return projected, conflicts


def review_contract(state, *, uncertain_fields=None):
    """Read-only UI projection. Production rules stay in validate_order/Agent gates."""
    from order_model import LABELS, DIMENSION_LABELS, DIMENSION_SPEC_ALIASES, required_order_keys
    from product_knowledge import parameter_state
    from tools import validate_order, match_supplier_capability
    order, metadata = state.get("order") or {}, state.get("fieldMeta") or {}
    items = order.get("items") or []
    multi = len(items) > 1
    scopes = [(item, item.get("itemId"), index) for index, item in enumerate(items)] if multi else [(order, None, None)]
    fields, issues = [], []
    uncertain = set(uncertain_fields or [])
    def issue(kind, field, message, *, blocking=True, action="edit", code=None):
        key = field["fieldId"] if field else code or kind
        value = {"id": f"{kind}:{key}:{code or action}", "kind": kind,
                 "field": key, "fieldId": key, "fieldPath": field["fieldPath"] if field else None,
                 "label": field["label"] if field else message, "itemId": field["itemId"] if field else None,
                 "itemIndex": field["itemIndex"] if field else None, "blocking": blocking,
                 "section": "now" if blocking else "handoff", "message": message, "action": action}
        issues.append(value)
        return value
    for current, item_id, index in scopes:
        prefix = f"items.{item_id}." if item_id else ""
        required = set(required_order_keys(current))
        profile, validation = parameter_state(current), validate_order(current)
        definitions = [(key, label, key in required, current.get(key), "overview" if key in
                        {"productType", "quantity", "purpose", "deadline", "budget"} else "specs")
                       for key, label in LABELS.items() if key != "platform"]
        definitions += [(f"dimensions.{key}", label, False, (current.get("dimensions") or {}).get(key), "specs")
                        for key, label in DIMENSION_LABELS.items()]
        definitions += [(f"productSpecs.{p['key']}", p["label"], bool(p.get("required")),
                         (current.get("productSpecs") or {}).get(p["key"], p.get("value")), "specs")
                        for p in profile.get("parameters", []) if p.get("key") not in DIMENSION_SPEC_ALIASES]
        # Legacy profile dimension aliases point to the existing physical field.
        for p in profile.get("parameters", []):
            if p.get("key") in DIMENSION_SPEC_ALIASES:
                canonical = "dimensions." + DIMENSION_SPEC_ALIASES[p["key"]]
                definitions = [(key, label, needed or bool(p.get("required")) if key == canonical else needed, value, group)
                               for key, label, needed, value, group in definitions]
        by_key = {}
        for key, label, needed, value, group in definitions:
            aliases = [key]
            if key == "quantity":
                aliases += ["quantityValue", "quantityUnit"]
            meta = metadata.get(prefix + key) or {}
            production = key not in {"purpose", "budget"}
            present = value not in (None, "", {}, [])
            def is_pending(alias):
                alias_meta = metadata.get(prefix + alias) or {}
                try:
                    low_confidence = float(alias_meta.get("confidence", 1)) < .75
                except (TypeError, ValueError, OverflowError):
                    low_confidence = True
                return prefix + alias in uncertain or alias_meta.get("status") == "pending_confirmation" or low_confidence
            pending = present and any(is_pending(alias) for alias in aliases)
            status = "missing" if not present else "pending_confirmation" if pending else "confirmed" if meta.get("status") == "confirmed" or meta.get("source") == "user" else "filled"
            field = {"key": key, "fieldPath": key, "fieldId": prefix + key, "field": prefix + key,
                     "label": label, "itemId": item_id, "itemIndex": index, "value": None if pending else deepcopy(value) if present else None,
                     "candidateValue": deepcopy(value) if pending else None, "status": status, "required": needed,
                     "production": production, "group": group, "provenance": deepcopy(meta)}
            fields.append(field)
            by_key[key] = field
            if needed and not present:
                issue("missing", field, f"请补充{label}，用于确定生产规格。")
            elif pending:
                issue("pending_confirmation", field, f"请核对{label}的候选值与原文依据。", blocking=production, action="confirm")
        # Include validation failures that a nonempty string cannot satisfy.
        for label in validation.get("missing", []):
            match = next((f for f in by_key.values() if label.startswith(f["label"])), None)
            if match and not any(i["fieldId"] == match["fieldId"] and i["blocking"] for i in issues):
                issue("missing", match, f"{label}尚未满足生产要求。")
        for field in _document_fields(list(by_key.values()), current)[1]:
            issue("risk", field, f"{field['label']}的同义字段值不一致，请核对后统一。", code="semantic_alias_conflict")
        if not validation.get("ok") and not validation.get("missing") and not validation.get("multiProduct"):
            issue("risk", by_key["quantity"], "数量必须是大于零的有效数量。")
        for risk in validation.get("risks", []):
            issue("risk", by_key.get("binding"), risk.get("message", "生产规格需要复核"),
                  blocking=risk.get("level") == "error", code="production_risk")
        selection = current.get("selectedOption") if multi else state.get("selectedOption")
        if not selection:
            field = {"fieldId": prefix + "selectedOption", "fieldPath": "selectedOption", "label": "工艺方案", "itemId": item_id, "itemIndex": index}
            issue("missing", field, "请比较并选择本项工艺方案。", action="compare")
        capability_order = {**current, "platform": order.get("platform") or "generic"}
        capability = match_supplier_capability(capability_order)
        for unsupported in capability.get("unsupported", []):
            issue("risk", by_key.get("productType"), unsupported.get("message") or "供应商明确不支持该规格。", code="supplier_unsupported")
    # Registry uncertainty is a handoff note, never four duplicate production blockers.
    issue("risk", None, "当前仅生成本地交接资料；供应商生产能力、价格和交期仍需核实，尚未发送。",
          blocking=False, action="review", code="supplier_unverified")
    confirmation = state.get("confirmation") or {}
    handoff = state.get("handoff") or {}
    stale = confirmation.get("status") == "stale" or bool(handoff.get("orderFingerprint") and handoff["orderFingerprint"] != production_fingerprint(state))
    if stale:
        issue("stale", None, "订单已修改，原交接单与整单确认已失效，请重新生成并复核。",
              blocking=False, action="generate", code="handoff_stale")["section"] = "now"
    for current, item_id, index in scopes:
        check = state.get("preflightResults") or {}
        if multi:
            check = check.get(item_id, {}) if isinstance(check, dict) else next((p for p in check if p.get("itemId") == item_id), {})
        if isinstance(check, dict) and check.get("stale"):
            f = {"fieldId": f"items.{item_id}.uploadedFile" if item_id else "uploadedFile", "fieldPath": "uploadedFile", "label": "印刷文件", "itemId": item_id, "itemIndex": index}
            issue("stale", f, "文件基础检查已过期；交接时需说明并重新检查。", blocking=False, action="upload")
    blockers = [i for i in issues if i["blocking"]]
    archived = state.get("orderStatus") == "archived"
    generated = bool(state.get("orderGenerated") and (state.get("handoff") or {}).get("status") == "ready")
    result = {"schemaVersion": "1.0", "revision": state.get("revision", 0), "fields": fields, "issues": issues,
              "blockingCount": len(blockers), "canGenerate": not blockers and not archived,
              "canExport": generated and not blockers and not stale,
              "confirmationStatus": confirmation.get("status", "not_ready"), "readOnly": archived}
    next_issue = next(iter(blockers), None)
    action = ("view" if archived else next_issue["action"] if next_issue else "export" if result["canExport"] and confirmation.get("status") == "confirmed" else "confirm_order" if result["canExport"] else "generate")
    result.update({"nextIssueId": next_issue["id"] if next_issue else None,
                   "nextAction": {"action": action, "issueId": next_issue["id"] if next_issue else None,
                                  "fieldId": next_issue["fieldId"] if next_issue else None,
                                  "itemId": next_issue["itemId"] if next_issue else None}})
    return result


def handoff_document(state, order_id, review=None):
    """One supplier-facing field mapping for preview, JSON, Markdown and print."""
    review = review if review is not None else review_contract(state)
    confirmation, handoff = state.get("confirmation") or {}, state.get("handoff") or {}
    fingerprint = production_fingerprint(state)
    stale = confirmation.get("status") == "stale" or bool(handoff.get("orderFingerprint") and handoff["orderFingerprint"] != fingerprint)
    exportable = bool(review["canExport"] and not stale)
    confirmed = exportable and confirmation.get("status") == "confirmed" and confirmation.get("orderFingerprint") == fingerprint
    status = "stale" if stale else "confirmed" if confirmed else "draft" if exportable else "blocked"
    order = state.get("order") or {}
    scopes = [(item, item.get("itemId"), index) for index, item in enumerate(order.get("items") or [])] if len(order.get("items") or []) > 1 else [(order, None, None)]
    items = []
    for current, item_id, index in scopes:
        selected = [f for f in review["fields"] if f["itemId"] == item_id and (f["value"] is not None or f["required"] or f["status"] == "pending_confirmation")]
        # Blocked documents intentionally retain null values, never promote candidates.
        doc_fields, _ = _document_fields(selected, current)
        check = state.get("preflightResults") or {}
        if item_id:
            check = check.get(item_id, {}) if isinstance(check, dict) else next((p for p in check if p.get("itemId") == item_id), {})
        if not isinstance(check, dict):
            check = {}
        file_name = current.get("uploadedFile") if item_id else state.get("uploadedFile") or current.get("uploadedFile")
        if check.get("fileName"):
            file_name = check["fileName"]
        file_status = "pending_delivery" if not file_name else "stale" if check.get("stale") else "checked" if check.get("ok") else "needs_review"
        file = {"status": file_status, "fileName": file_name, "itemId": item_id,
                "checkedAt": check.get("updatedAt"), "version": check.get("updatedAt"),
                "checks": deepcopy(check.get("checks") or []), "warnings": deepcopy(check.get("warnings") or []),
                "inspectionLevel": check.get("inspectionLevel"),
                "scope": {"basic": "文件基本属性检查", "metadata": "文件基本属性与已提供的 PDF 元数据检查"}.get(check.get("inspectionLevel"), "未执行基础检查"),
                "limitations": "仅文件基础检查；未提供专业 RIP、字体、分辨率及色彩生产验收。"}
        selected_id = current.get("selectedOption") if item_id else state.get("selectedOption")
        options = state.get("processOptions") or state.get("itemOptions") or []
        if isinstance(options, dict):
            options = options.get(item_id, [])
        selected_option = next((option for option in options if option.get("id") == selected_id), {})
        option_details = None
        if selected_id:
            option_details = {"id": selected_id, "title": selected_option.get("title") or
                              {"economy": "经济方案", "balanced": "平衡方案", "premium": "质感方案"}.get(selected_id, "已选工艺方案"),
                              "printMode": selected_option.get("printMode"),
                              "composition": " + ".join(str(current[key]) for key in ("paper", "printing", "finishing", "binding") if current.get(key))}
        items.append({"itemId": item_id or "order", "itemIndex": index, "title": current.get("productType") or "待识别产品",
                      "fields": doc_fields, "selectedOption": selected_id, "selectedOptionDetails": option_details, "file": file})
    document = {"schemaVersion": "1.0", "documentType": "supplier_handoff", "orderId": order_id,
                "revision": state.get("revision", 0), "status": status, "exportable": exportable,
                "unitSystem": "mm", "confirmedAt": confirmation.get("confirmedAt") if confirmed else None,
                "confirmedRevision": confirmation.get("confirmedRevision") if confirmed else None,
                "confirmation": {"status": "confirmed" if confirmed else "stale" if stale else "unconfirmed",
                                 "scope": "本地人工确认记录，不代表企业身份签章"},
                "generatedAt": handoff.get("generatedAt"), "items": items,
                "issues": deepcopy(review["issues"]),
                "notes": ["当前仅生成本地交接资料；供应商生产能力、价格和交期仍需核实，尚未发送。",
                          "任何示例估算均不构成供应商报价。"], "sent": False}
    def text(value):
        if value is None or value == "":
            return "待确认/待补充"
        return html.escape(str(value), quote=False).replace("|", "\\|").replace("\n", " ")
    field_statuses = {"missing": "待补充", "pending_confirmation": "候选待核对", "confirmed": "已核对", "filled": "已填写"}
    file_statuses = {"pending_delivery": "文件待交付", "stale": "检查已过期", "checked": "已完成基础检查", "needs_review": "需核对"}
    check_statuses = {"ok": "通过", "warn": "需关注", "error": "未通过", "info": "信息", "unknown": "未确认"}
    blocks = ["# 印刷订单交接单", f"订单：{text(order_id)} · 版本：{document['revision']}",
             f"状态：{ {'blocked':'需求整理中，禁止作为生产交接单导出','stale':'已失效，请重新生成','draft':'未确认草稿','confirmed':'已本地确认'}[status] }",
             f"确认时间：{document['confirmedAt'] or '尚未确认'} · 确认版本：{document['confirmedRevision'] if confirmed else '无'}",
             "尺寸单位：mm。"]
    for item in items:
        blocks.append(f"## {text(item['title'])}（{text(item['itemId'])}）")
        rows = ["| 字段 | 值 | 状态 |", "| --- | --- | --- |"]
        rows += [f"| {text(f['label'])} | {text(f['value'])} | {field_statuses.get(f['status'], '待核对')} |" for f in item["fields"]]
        blocks.append("\n".join(rows))
        option = item["selectedOptionDetails"]
        if option:
            blocks.append(f"工艺方案：{text(option['title'])}；组成：{text(option['composition'])}")
        file = item["file"]
        file_summary = f"{text(file['fileName'])} · {file_statuses[file['status']]}" if file["fileName"] else file_statuses[file["status"]]
        blocks += [f"文件：{file_summary}",
                   f"检查范围：{text(file['scope'])}；{file['limitations']}"]
        if file["checkedAt"]:
            blocks.append(f"检查时间/记录版本：{text(file['checkedAt'])}")
        if file["checks"]:
            rows = ["| 基础检查项 | 结果 | 说明 |", "| --- | --- | --- |"]
            rows += [f"| {text(check.get('label'))} | {check_statuses.get(check.get('status'), '未确认')} | {text(check.get('detail'))} |" for check in file["checks"]]
            blocks.append("\n".join(rows))
        if file["warnings"]:
            blocks.append("\n".join(f"- 文件复核：{text(warning)}" for warning in file["warnings"]))
    blocks.append("## 交接说明")
    notes = [f"- {text(note)}" for note in document["notes"]]
    notes += [f"- {text(i['message'])}" for i in review["issues"] if i["section"] == "handoff" and i["message"] not in document["notes"]]
    blocks.append("\n".join(notes))
    document["markdown"] = "\n\n".join(blocks)
    return document
