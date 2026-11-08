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

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .model import coerce, compute_totals, flatten, get_path, parse_date, parse_number, set_path, validate
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
ENGINES = ("verify", "llm", "rules")
ENGINE_ENV = "INVOICE_ENGINE"


def extract(doc_or_path, template: Optional[Template] = None, extra_dirs=None,
            compute: bool = True, engine: Optional[str] = None,
            llm_options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Extract structured data from an invoice.

    ``engine``:
      * ``"verify"`` (default) - template rules extract, then a local LLM confirms or
        corrects *every field* and quotes its evidence; result has a per-field report.
        Falls back to rules-only (with a notice) if no local LLM is reachable.
      * ``"llm"``    - the local LLM alone reads the invoice
      * ``"rules"``  - only the template's regex rules (fast, deterministic, no model)

    Returns ``{"source", "template", "engine", "data", "warnings"}`` plus ``"fields"``
    (per-field verification report) for the verify engine.
    """
    doc = doc_or_path if isinstance(doc_or_path, Document) else read_document(doc_or_path)
    if template is None:
        template, _ = detect_template(doc.text, extra_dirs)
    engine = (engine or os.environ.get(ENGINE_ENV) or (template.data.get("llm") or {}).get("engine")
              or "verify")
    if engine == "auto":
        engine = "verify"
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}, got '{engine}'")

    if engine == "rules":
        res = extract_rules(doc, template, compute)
        if not doc.has_text and not doc.form_fields:
            res["notice"] = ("No text layer (scan or photo): rules can't read it. "
                             "Use --engine verify or llm so the vision model reads the page images.")
        return res

    from .llm import LLMError, extract_with_llm

    if engine == "llm":
        res = extract_with_llm(doc, template, compute=compute, **(llm_options or {}))
        if not doc.has_text:
            res["notice"] = VISION_ONLY_NOTICE
        return res

    rules_res = extract_rules(doc, template, compute=False)
    try:
        from .llm import ask_verification
        answer, model = ask_verification(doc, template, rules_res["data"], **(llm_options or {}))
    except LLMError as exc:
        if not doc.has_text and not doc.form_fields:
            raise  # nothing the rules can do without text
        if compute:
            compute_totals(rules_res["data"])
        rules_res["warnings"] = validate(rules_res["data"], template.fields)
        rules_res["notice"] = f"Local LLM unavailable - fields NOT verified, template rules only: {exc}"
        return rules_res
    data, report, warnings = merge_verification(doc, template, rules_res["data"], answer)
    if compute:
        compute_totals(data)
    extra = {"notice": VISION_ONLY_NOTICE} if not doc.has_text else {}
    return {**extra,
        "source": rules_res["source"],
        "template": template.name,
        "engine": f"rules+llm-verify:{model}",
        "data": data,
        "fields": report,
        "warnings": validate(data, template.fields) + warnings,
    }


VISION_ONLY_NOTICE = ("No text layer - values were read from page images by the vision model. Evidence "
                      "can't be located in document text, so each value is only checked against the quote "
                      "the model gave plus the totals math. Review fields marked 'added'.")


# -- verification merge -------------------------------------------------------------
def _norm(s: Any) -> str:
    return re.sub(r"[\s,]+", " ", str(s)).strip().lower()


def evidence_found(evidence: Any, text: str) -> bool:
    return bool(evidence) and _norm(evidence) in _norm(text)


def value_in_evidence(value: Any, evidence: Any) -> bool:
    """Consistency check used for image-only documents: does the quote contain the value?"""
    if value is None or not evidence:
        return False
    ev = str(evidence)
    if isinstance(value, (int, float)):
        for token in re.findall(r"-?\d[\d.,' ]*\d|\d", ev):
            token = token.replace(" ", "").replace("'", "")
            for sep in (".", ","):
                num = parse_number(token, sep)
                if num is not None and abs(num - value) < 0.005:
                    return True
        return False
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
        y, m, d = str(value).split("-")
        if y in ev and re.search(rf"(?<!\d)0?{int(d)}(?!\d)", ev):
            return True
        return parse_date(ev.strip()) == value
    return _norm(value) in _norm(ev)


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 0.005
    return _norm(a) == _norm(b)


def merge_verification(doc: Document, template: Template, candidates: Dict[str, Any],
                       answer: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """Combine rule candidates with the LLM's verdicts.

    Per-field status:
      confirmed   rules and LLM agree
      corrected   LLM disagrees with rules -> LLM value used
      added       rules found nothing, LLM found a value
      unconfirmed rules found a value, LLM could not find it -> rules value kept
      missing     neither found anything
    ``evidence_ok`` is False when the LLM's quoted evidence is not in the document
    (possible hallucination) - such LLM values are never silently accepted.
    """
    fields = template.fields
    rules_cfg = template.extract
    text_check = doc.has_text
    data: Dict[str, Any] = {}
    report: Dict[str, Any] = {}
    warnings: List[str] = []

    for path, spec in fields.items():
        spec = spec or {}
        if spec.get("type") == "list":
            continue
        rule_val = get_path(candidates, path)
        entry = answer.get(path) if isinstance(answer.get(path), dict) else {}
        raw_llm = entry.get("value")
        fspec = _field_spec(fields, path, None, rules_cfg)
        llm_val = coerce(raw_llm, fspec.get("type", "string"), fspec) if raw_llm not in (None, "") else None
        evidence = entry.get("evidence")
        if llm_val is None:
            ev_ok = None
        elif text_check:
            ev_ok = evidence_found(evidence, doc.text)
        else:
            ev_ok = value_in_evidence(llm_val, evidence)

        if rule_val in (None, "") and llm_val is None:
            status, final = "missing", None
        elif llm_val is None:
            status, final = "unconfirmed", rule_val
            warnings.append(f"{path}: LLM could not confirm rules value {rule_val!r}")
        elif rule_val in (None, ""):
            status, final = "added", llm_val
            if not ev_ok:
                warnings.append(f"{path}: LLM value {llm_val!r} has no matching evidence in the document")
        elif _same(rule_val, llm_val):
            status, final = "confirmed", rule_val
        else:
            status = "corrected"
            if ev_ok:
                final = llm_val
                warnings.append(f"{path}: corrected {rule_val!r} -> {llm_val!r} (evidence: {evidence!r})")
            else:
                final = rule_val
                warnings.append(f"{path}: LLM suggested {llm_val!r} instead of {rule_val!r} but its evidence "
                                f"was not found in the document - kept rules value, please check")
        if final not in (None, ""):
            set_path(data, path, final)
        if status != "missing":
            report[path] = {"status": status, "value": final, "rules": rule_val, "llm": llm_val,
                            "evidence": evidence, "evidence_ok": ev_ok,
                            "evidence_check": "document text" if text_check else "quote (image)"}

    # line items
    rule_items = candidates.get("items") or []
    from .llm import normalise
    llm_items = normalise({"items": answer.get("items") or []}, {"items": fields.get("items") or {}}).get("items", [])
    if not llm_items:
        status, items = ("unconfirmed" if rule_items else "missing"), rule_items
    elif not rule_items:
        status, items = "added", llm_items
    elif len(rule_items) == len(llm_items) and all(
            _same(a.get("amount"), b.get("amount")) for a, b in zip(rule_items, llm_items)):
        status, items = "confirmed", rule_items
    else:
        status, items = "corrected", llm_items
        warnings.append(f"items: rules found {len(rule_items)} line(s), LLM found {len(llm_items)} - "
                        "used LLM lines, please check")
    if items:
        data["items"] = items
    report["items"] = {"status": status, "count": len(items), "rules_count": len(rule_items),
                       "llm_count": len(llm_items)}
    return data, report, warnings


def extract_rules(doc: Document, template: Template, compute: bool = True) -> Dict[str, Any]:
    """Regex/template-rule extraction (no model needed)."""
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
        "engine": "rules",
        "data": data,
        "warnings": warnings,
    }
