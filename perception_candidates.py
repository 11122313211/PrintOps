"""Evidence records between perception and order updates.

Evidence offsets refer to the original turn, never to converted units. When a
field has no unique literal match the entire utterance is cited explicitly.
"""
import re
from nlu import NEGATION, NEGATABLE, _find_product_mentions
import unicodedata


def field_candidates(text, patch, confidence, scope="order"):
    records = []
    mentions = _find_product_mentions(text)
    boundaries = [0]
    if unicodedata.normalize("NFKC", text) == text:
        for previous, following in zip(mentions, mentions[1:]):
            separators = list(re.finditer(r"[，,;；、]|以及|和|另做|另外", text[previous[2]:following[1]]))
            boundaries.append(previous[2] + separators[-1].end() if separators else following[1])
    boundaries.append(len(text))
    def visit(values, prefix="", left=0, right=None):
        right = len(text) if right is None else right
        for key, value in values.items():
            path = prefix + key
            if key in {"measurements", "unitSystem", "quantityValue", "quantityUnit"}:
                continue
            if isinstance(value, dict):
                visit(value, path + ".", left, right)
            elif key == "items" and isinstance(value, list):
                for index, item in enumerate(value):
                    a, b = (boundaries[index:index+2] if index + 1 < len(boundaries) else (0, len(text)))
                    visit(item, f"items.item-{index + 1}.", a, b)
            elif isinstance(value, (str, int, float)) and value != "":
                raw = str(value)
                start = text.find(raw, left, right)
                if start < 0:
                    start, raw = left, text[left:right]
                grade = confidence.get(path, confidence.get(key, .84))
                records.append({"field": path, "value": value, "rawText": raw, "start": start,
                                "end": start + len(raw), "confidence": grade, "negated": False,
                                "scope": prefix.split(".")[1] if prefix.startswith("items.") else scope,
                                "reason": "explicit" if grade >= .75 else "inference",
                                "status": "inferred" if grade >= .75 else "pending_confirmation"})
    visit(patch)
    for match in re.finditer(rf"{NEGATION}\s*(?:用|采用)?\s*({NEGATABLE})", text, re.I):
        value = match[1]
        field = ("paper" if "纸" in value else "printing" if value in {"双面", "单面", "四色", "黑白"} else "binding" if "装订" in value or "钉" in value or "胶装" in value
                 else "size" if re.fullmatch(r"[AB]\s*-?\s*[0-6]", value, re.I) else "finishing")
        records.append({"field": field, "value": value, "rawText": match[0], "start": match.start(),
                        "end": match.end(), "confidence": 1.0, "negated": True, "scope": scope,
                        "reason": "explicit_negation", "status": "rejected"})
    return records


def candidate_evidence(candidates):
    return {c["field"]: {"quote": c["rawText"], "source": "user_utterance", "start": c["start"],
                          "end": c["end"]} for c in candidates if not c["negated"]}
