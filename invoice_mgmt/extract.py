"""Template-driven extraction of invoice data from documents.

Extraction rules live in the template under ``extract:``. Each field accepts any of:

``patterns``  list of regexes; the named group ``value`` or the first group is used
``label``     label text(s); the value is whatever follows the label on that line
``block``     multi-line capture: ``{start: regex, until: regex, max_lines: 4}``
``value``     a constant
``type``      override the field type from ``fields:``

Line items are read with ``extract.line_items`` using either a ``row`` regex with
named groups, or ``table`` mode matching column headers of real tables.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .model import coerce, compute_totals, get_path, set_path, validate
from .readers import Document, read_document
from .template import Template, list_templates, load_template

FLAG_MAP = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL, "x": re.VERBOSE}
DEFAULT_FLAGS = re.IGNORECASE | re.MULTILINE


def _flags(spec: Dict[str, Any]) -> int:
    if "flags" not in spec:
        return DEFAULT_FLAGS
    out = 0
    for ch in str(spec["flags"]):
        out |= FLAG_MAP.get(ch, 0)
    return out


def _as_list(v) -> List[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _first_group(m: re.Match) -> Optional[str]:
    if "value" in m.re.groupindex:
        return m.group("value")
    if m.re.groups:
        for g in m.groups():
            if g is not None:
                return g
        return None
    return m.group(0)


def extract_field(text: str, spec: Dict[str, Any]) -> Optional[str]:
    if spec is None:
        return None
    if isinstance(spec, str):  # shorthand: a single pattern
        spec = {"patterns": [spec]}
    if "value" in spec:
        return spec["value"]
    flags = _flags(spec)

    for pattern in _as_list(spec.get("patterns")):
        m = re.search(pattern, text, flags)
        if m:
            val = _first_group(m)
            if val is not None and str(val).strip():
                return val

    for label in _as_list(spec.get("label")):
        pat = rf"{re.escape(label)}\s*[:#]?\s*(?P<value>[^\n]+?)\s*$"
        m = re.search(pat, text, flags)
        if m:
            return m.group("value")

    block = spec.get("block")
    if block:
        return _extract_block(text, block, flags)
    return None


def _extract_block(text: str, block: Dict[str, Any], flags: int) -> Optional[str]:
    lines = text.splitlines()
    start_re = re.compile(block["start"], flags)
    until_re = re.compile(block["until"], flags) if block.get("until") else None
    max_lines = int(block.get("max_lines", 6))
    include_start = block.get("include_start_line_rest", True)
    for idx, line in enumerate(lines):
        m = start_re.search(line)
        if not m:
            continue
        collected: List[str] = []
        rest = line[m.end():].strip(" :\t")
        if include_start and rest:
            collected.append(rest)
        for nxt in lines[idx + 1:]:
            if not nxt.strip() or (until_re and until_re.search(nxt)) or len(collected) >= max_lines:
                break
            collected.append(nxt.strip())
        excludes = [re.compile(x, flags) for x in _as_list(block.get("exclude"))]
        if excludes:
            collected = [c for c in collected if not any(x.search(c) for x in excludes)]
        if not collected:
            return None
        select = block.get("select")
        if select is not None:
            collected = _select_lines(collected, str(select))
        return "\n".join(collected) if collected else None
    return None


def _select_lines(lines: List[str], select: str) -> List[str]:
    """``"0"`` -> first line, ``"1:"`` -> all but first, ``"-1"`` -> last, ``"0:2"`` -> slice."""
    if ":" in select:
        a, b = (int(x) if x.strip() else None for x in select.split(":", 1))
        return lines[a:b]
    idx = int(select)
    return [lines[idx]] if -len(lines) <= idx < len(lines) else []


# -- line items --------------------------------------------------------------------
def _section(text: str, start: Optional[str], end: Optional[str], flags: int) -> str:
    if start:
        m = re.search(start, text, flags)
        if not m:
            return ""
        # Start on the line after the header.
        nl = text.find("\n", m.end())
        text = text[nl + 1:] if nl != -1 else ""
    if end:
        m = re.search(end, text, flags)
        if m:
            text = text[: m.start()]
    return text


def _items_from_rows(text: str, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    flags = _flags(spec) & ~re.MULTILINE
    body = _section(text, spec.get("start"), spec.get("end"), _flags(spec))
    row_res = [re.compile(r, flags) for r in _as_list(spec.get("row"))]
    skip_res = [re.compile(r, flags) for r in _as_list(spec.get("skip"))]
    continuation = spec.get("continuation", True)
    items: List[Dict[str, Any]] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or any(s.search(line) for s in skip_res):
            continue
        for rx in row_res:
            m = rx.search(line)
            if m:
                items.append({k: v for k, v in m.groupdict().items() if v is not None})
                break
        else:
            if continuation and items and "description" in items[-1]:
                items[-1]["description"] += " " + line
    return items


def _items_from_tables(tables: List[List[List[str]]], spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    columns: Dict[str, Any] = spec.get("columns", {})
    flags = _flags(spec)
    stop = re.compile(spec["end"], flags) if spec.get("end") else None
    for table in tables:
        for h_idx, header in enumerate(table):
            mapping: Dict[str, int] = {}
            for key, header_re in columns.items():
                for c_idx, cell in enumerate(header):
                    if c_idx in mapping.values():
                        continue
                    if any(re.search(h, cell or "", flags) for h in _as_list(header_re)):
                        mapping[key] = c_idx
                        break
            if "description" in mapping and len(mapping) >= min(2, len(columns)):
                items = []
                for row in table[h_idx + 1:]:
                    if stop and any(stop.search(c or "") for c in row):
                        break
                    item = {k: row[i] for k, i in mapping.items() if i < len(row) and row[i]}
                    if item.get("description"):
                        items.append(item)
                if items:
                    return items
    return []


def extract_items(doc: Document, spec: Dict[str, Any], item_fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not spec:
        return []
    items: List[Dict[str, Any]] = []
    mode = spec.get("mode", "auto")
    if mode in ("table", "auto") and spec.get("columns") and doc.tables:
        items = _items_from_tables(doc.tables, spec)
    if not items and mode in ("rows", "auto") and spec.get("row"):
        items = _items_from_rows(doc.text, spec)
    out = []
    for item in items:
        coerced = {}
        for key, val in item.items():
            ftype = (item_fields.get(key) or {}).get("type", "string")
            coerced[key] = coerce(val, ftype, item_fields.get(key))
        out.append(coerced)
    return out


# -- template detection -------------------------------------------------------------
def score_template(text: str, template: Template) -> int:
    return sum(1 for pat in template.match if re.search(pat, text, DEFAULT_FLAGS))


def detect_template(text: str, extra_dirs=None, fallback: str = "default") -> Tuple[Template, int]:
    best, best_score = None, 0
    for tpl in list_templates(extra_dirs):
        s = score_template(text, tpl)
        if s > best_score:
            best, best_score = tpl, s
    if best is None:
        best = load_template(fallback, extra_dirs)
    return best, best_score


def _field_spec(fields, path, rule, rules) -> Dict[str, Any]:
    """Merge the data-model spec with type hints from the extraction rule / template."""
    fspec = dict(fields.get(path) or {})
    if isinstance(rule, dict):
        fspec.update({k: v for k, v in rule.items() if k in ("type", "formats", "decimal_separator")})
    if fspec.get("type") == "date" and rules.get("date_formats"):
        fspec["formats"] = list(fspec.get("formats") or []) + list(rules["date_formats"])
    return fspec


# -- main entry point -------------------------------------------------------------
def extract(doc_or_path, template: Optional[Template] = None, extra_dirs=None,
            compute: bool = True) -> Dict[str, Any]:
    """Extract structured data. Returns ``{"data": ..., "warnings": [...], "template": name}``."""
    doc = doc_or_path if isinstance(doc_or_path, Document) else read_document(doc_or_path)
    if template is None:
        template, _ = detect_template(doc.text, extra_dirs)

    fields = template.fields
    rules = template.extract
    data: Dict[str, Any] = {}

    form_map: Dict[str, str] = rules.get("form_fields") or {}
    for path, spec in (rules.get("fields") or {}).items():
        raw = None
        if path in form_map and doc.form_fields.get(form_map[path]):
            raw = doc.form_fields[form_map[path]]
        if raw is None:
            raw = extract_field(doc.text, spec)
        if raw is None:
            continue
        fspec = _field_spec(fields, path, spec, rules)
        set_path(data, path, coerce(raw, fspec.get("type", "string"), fspec))

    for path, form_name in form_map.items():
        if get_path(data, path) in (None, "") and doc.form_fields.get(form_name):
            fspec = _field_spec(fields, path, None, rules)
            set_path(data, path, coerce(doc.form_fields[form_name], fspec.get("type", "string"), fspec))

    item_fields = (fields.get("items") or {}).get("item_fields", {})
    items = extract_items(doc, rules.get("line_items") or {}, item_fields)
    if items:
        data["items"] = items

    for path, spec in fields.items():  # constants defined by the template (e.g. seller of a vendor template)
        if spec and "extract_default" in spec and get_path(data, path) in (None, ""):
            set_path(data, path, spec["extract_default"])

    if compute:
        compute_totals(data)
    warnings = validate(data, fields)
    return {
        "source": str(doc.path) if doc.path else None,
        "template": template.name,
        "data": data,
        "warnings": warnings,
    }
