"""Schema 2.0 physical dimensions; evidence stays outside this structure."""
from unit_normalizer import dimension_values

SEMANTICS = {
    "finishedSize": "finished", "expandedSize": "expanded", "dieCutSize": "die_cut",
    "packageSize": "package_unspecified", "packageInnerSize": "package_inner",
    "packageOuterSize": "package_outer",
}


def parse_measurement(value, semantic="finished"):
    values = dimension_values(value)
    if values is None:
        return None
    result = {axis: int(v) if v == int(v) else float(v)
              for axis, v in zip(("width", "height", "depth"), values)}
    result.update(unit="mm", semantic=semantic)
    return result
