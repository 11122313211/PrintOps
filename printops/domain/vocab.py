"""One vocabulary for production display, independent of provider IDs."""
import re

UNIT_SYSTEM = "mm"
CURRENCY = "CNY"
GRAMMAGE_UNIT = "g/m²"


def normalize_print_term(field, value):
    if not isinstance(value, str):
        return value
    if field in {"paper", "coverPaper", "innerPaper", "cardStock"}:
        value = re.sub(r"(\d+(?:\.\d+)?)\s*(?:g(?:/m[²2])?|克)(?![a-z])",
                       r"\1 g/m²", value, flags=re.I)
    if field == "finishing":
        value = re.sub(r"局部\s*uv", "局部 UV", value, flags=re.I)
    return value
