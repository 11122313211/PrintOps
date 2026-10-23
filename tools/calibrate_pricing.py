#!/usr/bin/env python3
"""Validate authorized historical quotes and produce an unpublished JSON draft.

This command performs no network requests and never modifies active rate cards.
See docs/PRICING.md for the exact tax/shipping comparison convention.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pricing_schema import decimal_value, money
import stdio_utf8

REQUIRED_COLUMNS = (
    "quote_id", "source_id", "source_description", "quote_date", "valid_until",
    "product", "width_mm", "height_mm", "dimension_semantic", "quantity", "quantity_unit",
    "material", "gsm", "pages", "printing", "sides", "print_mode", "finishing", "binding",
    "product_specs", "region", "lead_workdays", "currency", "total_amount", "tax_basis", "tax_rate",
    "delivery_basis", "shipping_net_amount",
)
COMPARABLE_COLUMNS = (
    "product", "width_mm", "height_mm", "dimension_semantic", "quantity", "quantity_unit",
    "material", "gsm", "pages", "printing", "sides", "print_mode", "finishing", "binding",
    "product_specs", "region", "lead_workdays",
)


def _number(row: dict[str, str], key: str, *, zero: bool = False, integer: bool = False) -> Decimal:
    value = decimal_value(row[key])
    if value < 0 or (not zero and value == 0) or value > Decimal("1000000000"):
        raise ValueError(f"{key} 超出有效范围")
    if integer and value != value.to_integral_value():
        raise ValueError(f"{key} 必须是整数")
    row[key] = str(value.normalize())
    return value


def _percentile(values: list[Decimal], numerator: int, denominator: int = 4) -> Decimal:
    ordered = sorted(values)
    position = Decimal(len(ordered) - 1) * numerator / denominator
    lower = int(position)
    fraction = position - lower
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def calibrate(csv_path: str | Path, *, as_of: date, min_samples: int = 5,
              max_age_days: int = 90) -> dict:
    if min_samples < 3 or max_age_days < 1:
        raise ValueError("min_samples 至少为3，max_age_days 必须为正数")
    groups: dict[tuple, list[dict]] = defaultdict(list)
    seen: set[str] = set()
    rejected: list[dict] = []
    expired: list[str] = []
    with Path(csv_path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or set(REQUIRED_COLUMNS) - set(reader.fieldnames):
            raise ValueError("CSV 缺少必需列：" + ", ".join(sorted(set(REQUIRED_COLUMNS) - set(reader.fieldnames or []))))
        for line, raw in enumerate(reader, 2):
            try:
                if None in raw:
                    raise ValueError("CSV 列数不一致")
                row = {key: str(raw.get(key) or "").strip() for key in REQUIRED_COLUMNS}
                if any(not row[key] for key in REQUIRED_COLUMNS):
                    raise ValueError("必需值不得为空（无工艺/不适用请填写 none 或0）")
                if any(row[key].lower() in {"none", "unknown", "待确认", "未知", "0"} for key in ("source_id", "source_description")):
                    raise ValueError("报价来源必须可追溯，不能使用缺省占位符")
                if row["quote_id"] in seen:
                    raise ValueError("quote_id 重复，不得用同一报价增加样本数")
                seen.add(row["quote_id"])
                quoted, valid = date.fromisoformat(row["quote_date"]), date.fromisoformat(row["valid_until"])
                if quoted > as_of or valid < quoted:
                    raise ValueError("报价时间或有效期不合法")
                if as_of > valid or (as_of - quoted).days > max_age_days:
                    expired.append(row["quote_id"]); continue
                if row["currency"] != "CNY":
                    raise ValueError("只接受 CNY，不能混合币种")
                if row["quantity_unit"] not in {"张", "份", "本", "个", "块", "套"}:
                    raise ValueError("quantity_unit 必须是明确的计数单位")
                if row["dimension_semantic"] not in {"finished", "expanded", "die_cut"}:
                    raise ValueError("尺寸必须为成品/展开/刀模平面尺寸，不能以盒体三维尺寸代替")
                for key in ("width_mm", "height_mm", "quantity", "lead_workdays"):
                    _number(row, key, integer=key in {"quantity", "lead_workdays"})
                for key in ("gsm", "pages"):
                    _number(row, key, zero=True, integer=key == "pages")
                sides = _number(row, "sides", integer=True)
                if sides not in {Decimal(1), Decimal(2)}:
                    raise ValueError("sides 必须为1或2")
                if row["tax_basis"] not in {"inclusive", "exclusive"} or row["delivery_basis"] not in {"ex_works", "delivered"}:
                    raise ValueError("必须明确税费和交付口径")
                tax = _number(row, "tax_rate", zero=True)
                if tax > 1:
                    raise ValueError("tax_rate 使用0~1比例，13%须填写0.13")
                shipping = _number(row, "shipping_net_amount", zero=True)
                if row["delivery_basis"] == "ex_works" and shipping != 0:
                    raise ValueError("ex_works 报价 shipping_net_amount 必须为0")
                total = _number(row, "total_amount")
                net_total = total / (1 + tax) if row["tax_basis"] == "inclusive" else total
                ex_works_total = net_total - shipping
                if ex_works_total <= 0:
                    raise ValueError("扣除税费和净运费后报价必须为正")
                specs = json.loads(row["product_specs"])
                if not isinstance(specs, dict) or any(not isinstance(key, str) or not isinstance(value, (str, int, float, bool)) for key, value in specs.items()):
                    raise ValueError("product_specs 必须为扁平 JSON 对象")
                row["product_specs"] = json.dumps(specs, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
                row["finishing"] = "+".join(sorted(set(part.strip() for part in row["finishing"].replace("、", "+").split("+") if part.strip())))
                key = tuple(row[column] for column in COMPARABLE_COLUMNS)
                groups[key].append({"quoteId": row["quote_id"], "sourceId": row["source_id"],
                                    "sourceDescription": row["source_description"], "quoteDate": row["quote_date"],
                                    "validUntil": min(valid, quoted + timedelta(days=max_age_days)).isoformat(), "taxBasis": row["tax_basis"],
                                    "taxRate": row["tax_rate"], "deliveryBasis": row["delivery_basis"],
                                    "shippingNetAmount": row["shipping_net_amount"], "normalizedTotal": ex_works_total})
            except (ValueError, TypeError, KeyError) as error:
                rejected.append({"line": line, "quoteId": str(raw.get("quote_id") or ""), "reason": str(error)})
    buckets = []
    for key, samples in sorted(groups.items()):
        eligible = len(samples) >= min_samples
        values = [sample["normalizedTotal"] for sample in samples]
        buckets.append({"comparableOrder": dict(zip(COMPARABLE_COLUMNS, key)), "sampleCount": len(samples),
                        "status": "review_required" if eligible else "insufficient_samples",
                        "statistics": {"p25": money(_percentile(values, 1)), "median": money(_percentile(values, 2)),
                                       "p75": money(_percentile(values, 3))} if eligible else None,
                        "expiresAt": min(sample["validUntil"] for sample in samples),
                        "samples": [{**sample, "normalizedTotal": money(sample["normalizedTotal"])} for sample in samples]})
    return {"schemaVersion": "2.0", "type": "pricing_calibration_draft", "published": False,
            "sourceType": "historical_quote", "isSupplierLivePrice": False, "asOf": as_of.isoformat(),
            "currency": "CNY", "unitSystem": "mm", "taxBasis": "exclusive", "deliveryBasis": "ex_works",
            "minimumSamples": min_samples, "maxAgeDays": max_age_days, "buckets": buckets,
            "rejectedRows": rejected, "expiredQuoteIds": expired,
            "nextAction": "人工检查来源授权、可比口径与有效期；本草稿不能被计价引擎直接加载，不会自动发布。"}


def main() -> int:
    stdio_utf8.configure()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path")
    parser.add_argument("--output", required=True)
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--min-samples", type=int, default=5)
    parser.add_argument("--max-age-days", type=int, default=90)
    args = parser.parse_args()
    try:
        output = Path(args.output).resolve()
        active = Path(__file__).resolve().parents[1] / "rate_cards"
        if output == active or active in output.parents:
            raise ValueError("校准草稿不得写入活动 rate_cards 目录")
        result = calibrate(args.csv_path, as_of=date.fromisoformat(args.as_of), min_samples=args.min_samples, max_age_days=args.max_age_days)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"output": str(output), "published": False, "buckets": len(result["buckets"]),
                          "rejectedRows": len(result["rejectedRows"]), "expiredRows": len(result["expiredQuoteIds"])}))
        return 1 if result["rejectedRows"] else 0
    except (ValueError, OSError) as error:
        print(f"校准失败：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
