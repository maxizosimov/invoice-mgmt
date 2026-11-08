"""Invoice data helpers: dotted paths, type coercion, totals calculation."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional

DATE_INPUT_FORMATS = [
    # Ambiguous numeric dates default to US month-first; set `extract.date_formats`
    # in a template (e.g. ["%d/%m/%Y"]) for day-first documents.
    "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d.%m.%Y", "%m-%d-%Y", "%d-%m-%Y", "%m/%d/%y",
    "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y",
    "%Y/%m/%d",
]


# -- dotted path access ---------------------------------------------------------
def get_path(data: Dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return default
    return cur


def set_path(data: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def flatten(data: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """Flatten nested dicts to dotted keys (lists are left as-is)."""
    out: Dict[str, Any] = {}
    for key, value in data.items():
        full = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, f"{full}."))
        else:
            out[full] = value
    return out


# -- coercion --------------------------------------------------------------------
def parse_number(value: Any, decimal_sep: str = ".") -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    s = str(value).strip()
    negative = s.startswith("(") and s.endswith(")") or s.startswith("-") or s.endswith("-")
    s = re.sub(r"[^\d,.\-]", "", s).strip("-")
    if decimal_sep == ",":
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        num = float(Decimal(s))
    except (InvalidOperation, ValueError):
        return None
    return -num if negative else num


def parse_date(value: Any, formats: Optional[Iterable[str]] = None) -> Optional[str]:
    """Parse to ISO ``YYYY-MM-DD``; returns the original string if unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = re.sub(r"\s+", " ", str(value).strip().rstrip("."))
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    for fmt in list(formats or []) + DATE_INPUT_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


def coerce(value: Any, ftype: str, spec: Optional[Dict[str, Any]] = None) -> Any:
    spec = spec or {}
    if value is None:
        return None
    if ftype in ("number", "money", "float", "decimal"):
        return parse_number(value, spec.get("decimal_separator", "."))
    if ftype == "integer":
        num = parse_number(value)
        return int(num) if num is not None else None
    if ftype == "date":
        return parse_date(value, spec.get("formats"))
    if ftype == "multiline":
        return "\n".join(line.strip() for line in str(value).strip().splitlines() if line.strip())
    return re.sub(r"[ \t]+", " ", str(value)).strip()


def round_money(value: float, places: int = 2) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


# -- defaults & totals ----------------------------------------------------------
def apply_defaults(data: Dict[str, Any], fields: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    for path, spec in fields.items():
        if spec and "default" in spec and get_path(data, path) in (None, ""):
            default = spec["default"]
            if default == "today":
                default = date.today().isoformat()
            set_path(data, path, default)
    return data


def compute_totals(data: Dict[str, Any], places: int = 2) -> Dict[str, Any]:
    """Fill in missing line amounts, subtotal, tax, total. Never overwrites given totals."""
    items: List[Dict[str, Any]] = data.get("items") or []
    for item in items:
        qty = parse_number(item.get("quantity"))
        price = parse_number(item.get("unit_price"))
        if item.get("amount") in (None, "") and qty is not None and price is not None:
            item["amount"] = round_money(qty * price, places)
        elif item.get("amount") not in (None, ""):
            item["amount"] = parse_number(item["amount"])

    if items and data.get("subtotal") in (None, ""):
        data["subtotal"] = round_money(sum(parse_number(i.get("amount")) or 0 for i in items), places)

    subtotal = parse_number(data.get("subtotal")) or 0.0
    discount = parse_number(data.get("discount")) or 0.0
    tax_rate = parse_number(data.get("tax_rate"))
    if data.get("tax") in (None, "") and tax_rate is not None:
        data["tax"] = round_money((subtotal - discount) * tax_rate / 100.0, places)
    if data.get("total") in (None, ""):
        tax = parse_number(data.get("tax")) or 0.0
        shipping = parse_number(data.get("shipping")) or 0.0
        data["total"] = round_money(subtotal - discount + tax + shipping, places)
    return data


def validate(data: Dict[str, Any], fields: Dict[str, Dict[str, Any]], tolerance: float = 0.02) -> List[str]:
    """Return a list of human readable warnings (missing required fields, math mismatches)."""
    warnings: List[str] = []
    for path, spec in fields.items():
        if spec and spec.get("required") and get_path(data, path) in (None, "", []):
            warnings.append(f"Missing required field: {path}")

    items = data.get("items") or []
    for idx, item in enumerate(items, 1):
        qty, price, amount = (parse_number(item.get(k)) for k in ("quantity", "unit_price", "amount"))
        if None not in (qty, price, amount) and abs(qty * price - amount) > tolerance:
            warnings.append(f"Line {idx}: quantity x unit_price ({qty * price:.2f}) != amount ({amount:.2f})")

    subtotal = parse_number(data.get("subtotal"))
    if items and subtotal is not None:
        s = sum(parse_number(i.get("amount")) or 0 for i in items)
        if abs(s - subtotal) > tolerance:
            warnings.append(f"Sum of line items ({s:.2f}) != subtotal ({subtotal:.2f})")

    total = parse_number(data.get("total"))
    if subtotal is not None and total is not None:
        expected = (subtotal - (parse_number(data.get("discount")) or 0)
                    + (parse_number(data.get("tax")) or 0) + (parse_number(data.get("shipping")) or 0))
        if abs(expected - total) > tolerance:
            warnings.append(f"subtotal - discount + tax + shipping ({expected:.2f}) != total ({total:.2f})")
    return warnings
