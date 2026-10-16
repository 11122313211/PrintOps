"""Deterministic physical units. No model or supplier assumptions are used."""
from decimal import Decimal, InvalidOperation
import re
import unicodedata

# ISO 216. JIS B sizes must be supplied explicitly in mm.
STANDARD_SIZES = {
    "A0": (841, 1189), "A1": (594, 841), "A2": (420, 594), "A3": (297, 420),
    "A4": (210, 297), "A5": (148, 210), "A6": (105, 148),
    "B3": (353, 500), "B4": (250, 353), "B5": (176, 250), "B6": (125, 176),
}
FACTORS = {"mm": Decimal(1), "cm": Decimal(10), "in": Decimal("25.4"), "inch": Decimal("25.4")}


def decimal_text(value):
    return format(Decimal(str(value)).normalize(), "f")


def dimension_values(value):
    """Return mm Decimals, or None for an ambiguous/invalid measurement.

    A trailing unit applies to preceding unitless axes. Mixed explicit units
    are converted separately; a missing unit is never silently assumed.
    """
    if not isinstance(value, str) or len(value) > 256:
        return None
    text = unicodedata.normalize("NFKC", value).strip().lower()
    text = text.replace("毫米", "mm").replace("厘米", "cm").replace("英寸", "in").replace('"', 'in')
    text = re.sub(r"([ab])\s*-?\s*(\d)", r"\1\2", text)
    if text.upper() in STANDARD_SIZES:
        return tuple(Decimal(v) for v in STANDARD_SIZES[text.upper()])
    axes = re.split(r"\s*(?:[x×✕✖*]|\\\*)\s*", text)
    if len(axes) not in (2, 3):
        return None
    parsed = [re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(mm|cm|in|inch)?\s*", axis) for axis in axes]
    if not all(parsed):
        return None
    trailing = parsed[-1].group(2)
    if any(not match.group(2) for match in parsed) and not trailing:
        return None
    values = tuple(Decimal(match.group(1)) * FACTORS[match.group(2) or trailing] for match in parsed)
    return values if all(Decimal(0) < v <= Decimal(1000000) for v in values) else None


def normalize_dimension(value):
    values = dimension_values(value)
    if values is not None:
        return "×".join(decimal_text(v) for v in values) + " mm"
    return str(value or "").strip()


def normalize_length(value):
    """Normalize scalar dimensions such as bleed and material thickness."""
    if not isinstance(value, str):
        return value
    pattern = r"(\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|inch|in|英寸)(?![a-z])"
    def convert(match):
        unit = {"毫米": "mm", "厘米": "cm", "英寸": "in"}.get(match[2].lower(), match[2].lower())
        return decimal_text(Decimal(match[1]) * FACTORS[unit]) + " mm"
    return re.sub(pattern, convert, value, flags=re.I)
