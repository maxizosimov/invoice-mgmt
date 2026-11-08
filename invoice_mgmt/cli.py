"""Command line interface: ``invoice <command> ...``

    invoice extract  FILE... [-t TEMPLATE|auto] [-f json|yaml|csv] [-o OUT]
    invoice fill     DATA.(json|yaml) -o OUT.(pdf|html|docx|txt) [-t TEMPLATE] [--set k=v]
    invoice inspect  FILE                     show raw text/tables/form fields (for writing patterns)
    invoice templates list | show NAME | new NAME [--from BASE]
    invoice sample   [-o data.json]           write example invoice data
    invoice docx-starter OUT.docx             write an editable Word template with placeholders
    invoice form-fields FORM.pdf              list fields of a fillable PDF
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import yaml

from . import __version__
from .extract import detect_template, extract
from .llm import LLMError
from .fill import FORMATS, FillError, fill, list_pdf_form_fields
from .model import flatten, set_path
from .readers import ReaderError, read_document
from .template import TemplateError, find_template_file, list_templates, load_template

SAMPLE_DATA: Dict[str, Any] = {
    "invoice_number": "INV-2026-0042",
    "issue_date": "2026-11-08",
    "due_date": "2026-12-08",
    "po_number": "PO-7781",
    "currency": "USD",
    "seller": {
        "name": "Northwind Studio LLC",
        "address": "500 Market Street, Suite 12\nSan Francisco, CA 94105",
        "email": "billing@northwind.example",
        "phone": "+1 415 555 0100",
    },
    "buyer": {
        "name": "Contoso Ltd.",
        "address": "1 Harbor Way\nSeattle, WA 98101",
        "email": "ap@contoso.example",
    },
    "items": [
        {"description": "Website redesign", "quantity": 1, "unit_price": 4200.00},
        {"description": "Consulting hours", "quantity": 12.5, "unit_price": 150.00},
        {"description": "Hosting (12 months)", "quantity": 12, "unit_price": 25.00},
    ],
    "tax_rate": 8.5,
    "payment_terms": "Net 30",
    "payment_instructions": "Bank transfer to ACME Bank, IBAN US00 0000 0000 0000",
    "notes": "Thank you for choosing Northwind Studio.",
}


# -- helpers ------------------------------------------------------------------------
def _load_data(path: str) -> Dict[str, Any]:
    if path == "-":
        text = sys.stdin.read()
        return yaml.safe_load(text) or {}
    p = Path(path)
    with open(p, "r", encoding="utf-8") as fh:
        if p.suffix.lower() == ".json":
            return json.load(fh)
        return yaml.safe_load(fh) or {}


def _parse_set(pairs: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--set expects key=value, got '{pair}'")
        key, value = pair.split("=", 1)
        set_path(out, key.strip(), yaml.safe_load(value) if value.strip() else "")
    return out


def _deep_update(base: Dict[str, Any], upd: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def _emit(text: str, output: str = None) -> None:
    if output:
        Path(output).write_text(text, encoding="utf-8")
        print(f"Wrote {output}", file=sys.stderr)
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")


def _to_csv(results: List[Dict[str, Any]], items: bool) -> str:
    buf = io.StringIO()
    rows: List[Dict[str, Any]] = []
    for r in results:
        data = r["data"]
        base = {"source": r.get("source"), "template": r.get("template")}
        header = {k: v for k, v in flatten(data).items() if k != "items"}
        if items:
            for idx, item in enumerate(data.get("items") or [], 1):
                rows.append({**base, "invoice_number": data.get("invoice_number"), "line": idx, **item})
        else:
            status = {f"{k}__status": v["status"] for k, v in (r.get("fields") or {}).items()}
            rows.append({**base, "engine": r.get("engine"), **header,
                         "item_count": len(data.get("items") or []), **status,
                         "warnings": "; ".join(r.get("warnings") or [])})
    keys: List[str] = []
    for row in rows:
        for k in row:
            if k not in keys:
                keys.append(k)
    writer = csv.DictWriter(buf, fieldnames=keys)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else str(v).replace("\n", ", ")) for k, v in row.items()})
    return buf.getvalue()


# -- commands ----------------------------------------------------------------------
def cmd_extract(args) -> int:
    results = []
    for f in args.files:
        doc = read_document(f)
        if args.template == "auto":
            tpl, _ = detect_template(doc.text, args.templates_dir)
        else:
            tpl = load_template(args.template, args.templates_dir)
        llm_opts = {"model": args.model, "base_url": args.llm_url, "provider": args.provider,
                    "vision": args.vision, "vision_model": args.vision_model}
        res = extract(doc, tpl, compute=not args.no_compute, engine=args.engine, llm_options=llm_opts)
        results.append(res)
        name = Path(f).name
        if res.get("notice"):
            print(f"notice [{name}]: {res['notice']}", file=sys.stderr)
        if res.get("fields") and not args.quiet:
            _print_report(name, res, sys.stderr)
        for w in res["warnings"]:
            print(f"warning [{name}]: {w}", file=sys.stderr)

    if args.format == "csv":
        text = _to_csv(results, args.items)
    else:
        payload: Any = results if len(results) > 1 else results[0]
        if args.data_only:
            payload = [r["data"] for r in results] if len(results) > 1 else results[0]["data"]
        if args.format == "yaml":
            text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        else:
            text = json.dumps(payload, indent=2, ensure_ascii=False)
    _emit(text, args.output)
    return 1 if args.strict and any(r["warnings"] for r in results) else 0


STATUS_MARK = {"confirmed": "OK ", "corrected": "FIX", "added": "NEW", "unconfirmed": "?? ", "missing": "-- "}


def _print_report(name: str, res: Dict[str, Any], out) -> None:
    report = res["fields"]
    counts: Dict[str, int] = {}
    for entry in report.values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    print(f"== {name}: field verification ({res['engine']})", file=out)
    for path, e in report.items():
        mark = STATUS_MARK.get(e["status"], "   ")
        if path == "items":
            detail = f"{e['count']} line(s) (rules {e['rules_count']}, llm {e['llm_count']})"
        else:
            detail = repr(e["value"])
            if e["status"] == "corrected":
                detail += f"   (rules had {e['rules']!r})"
            if e.get("evidence_ok") is False:
                detail += ("   [evidence NOT found in document]" if e.get("evidence_check") == "document text"
                           else "   [value doesn't match the model's quote]")
        print(f"  [{mark}] {e['status']:<11} {path:<22} {detail}", file=out)
    print("  " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())), file=out)


def cmd_llm_check(args) -> int:
    from .llm import LLMError, check
    tpl = load_template(args.template, args.templates_dir)
    try:
        print(check(tpl, model=args.model, base_url=args.llm_url, provider=args.provider,
                    vision=args.vision, vision_model=args.vision_model))
        return 0
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def cmd_fill(args) -> int:
    data = _load_data(args.data) if args.data else {}
    _deep_update(data, _parse_set(args.set))
    tpl = load_template(args.template, args.templates_dir)
    res = fill(data, tpl, args.output, fmt=args.format, layout=args.layout, compute=not args.no_compute)
    for w in res["warnings"]:
        print(f"warning: {w}", file=sys.stderr)
    print(f"Wrote {res['output']} ({res['format']}, template '{tpl.name}')", file=sys.stderr)
    return 1 if args.strict and res["warnings"] else 0


def cmd_inspect(args) -> int:
    doc = read_document(args.file)
    print("=== TEXT " + "=" * 60)
    for i, line in enumerate(doc.text.splitlines(), 1):
        print(f"{i:4}| {line}" if args.line_numbers else line)
    if doc.tables:
        print(f"\n=== TABLES ({len(doc.tables)}) " + "=" * 50)
        for t_idx, table in enumerate(doc.tables, 1):
            print(f"-- table {t_idx}")
            for row in table:
                print(" | ".join(row))
    if doc.form_fields:
        print("\n=== FORM FIELDS " + "=" * 52)
        for k, v in doc.form_fields.items():
            print(f"{k}: {v}")
    scored = []
    from .extract import score_template
    for tpl in list_templates(args.templates_dir):
        scored.append((score_template(doc.text, tpl), tpl.name))
    scored.sort(reverse=True)
    print("\n=== TEMPLATE MATCH SCORES " + "=" * 42)
    for s, name in scored:
        print(f"{s:3}  {name}")
    return 0


def cmd_templates(args) -> int:
    if args.action == "list":
        for tpl in list_templates(args.templates_dir):
            print(f"{tpl.name:20} {tpl.path}\n{'':20} {tpl.description}")
        return 0
    if not args.name:
        raise SystemExit("template name required")
    if args.action == "show":
        tpl = load_template(args.name, args.templates_dir)
        print(yaml.safe_dump(tpl.data, sort_keys=False, allow_unicode=True))
        return 0
    if args.action == "path":
        print(find_template_file(args.name, args.templates_dir))
        return 0
    if args.action == "new":
        out_dir = Path(args.dir or "templates")
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{args.name}.yaml"
        if out.exists() and not args.force:
            raise SystemExit(f"{out} already exists (use --force to overwrite)")
        base = args.base
        load_template(base, args.templates_dir)  # validate base exists
        out.write_text(NEW_TEMPLATE.format(name=args.name, base=base), encoding="utf-8")
        print(f"Created {out}. Edit it, then test with:\n"
              f"  invoice inspect some-invoice.pdf\n"
              f"  invoice extract some-invoice.pdf -t {out}", file=sys.stderr)
        return 0
    return 2


NEW_TEMPLATE = """\
# Template '{name}' - inherits everything from '{base}'; override only what differs.
# Tip: run `invoice templates show {base}` to see every option you can override.
extends: {base}
name: {name}
description: TODO describe which invoices / layout this template is for

# Regexes that identify this layout (used by `--template auto`).
match:
  # - 'Vendor Name Inc'

fields: {{}}
  # seller.name: {{type: string, extract_default: Vendor Name Inc}}

extract:
  fields: {{}}
    # invoice_number: {{label: 'Ref No.'}}
    # total: {{patterns: ['^Amount payable[^\\n]*?(?P<value>[\\d,]+\\.\\d{{2}})']}}
  # line_items:
  #   start: '^Description'
  #   end: '^Subtotal'
  #   row: ['^(?P<description>.+?)\\s+(?P<quantity>\\d+)\\s+(?P<unit_price>[\\d,.]+)\\s+(?P<amount>[\\d,.]+)$']

render: {{}}
  # currency_symbol: "$"
  # labels: {{title: TAX INVOICE}}
  # pdf: {{accent_color: "#333333", logo: logo.png, page_size: a4}}
  # html: {{template: my-layout.html.j2}}
  # docx: {{template: my-layout.docx}}
  # pdf_form: {{file: form.pdf, fields: {{"InvoiceNo": invoice_number, "Total": "{{{{ total | money }}}}"}}}}
"""


def cmd_sample(args) -> int:
    text = (yaml.safe_dump(SAMPLE_DATA, sort_keys=False) if args.output and args.output.endswith((".yaml", ".yml"))
            else json.dumps(SAMPLE_DATA, indent=2))
    _emit(text, args.output)
    return 0


def cmd_docx_starter(args) -> int:
    from .docx_starter import build_starter_docx
    tpl = load_template(args.template, args.templates_dir)
    build_starter_docx(args.output, tpl)
    print(f"Wrote {args.output}. Restyle it in Word, keep the {{{{ placeholders }}}}, then set "
          f"render.docx.template in your template YAML.", file=sys.stderr)
    return 0


def cmd_form_fields(args) -> int:
    fields = list_pdf_form_fields(args.file)
    if not fields:
        print("No fillable form fields found.", file=sys.stderr)
        return 1
    print("# Paste into render.pdf_form.fields and map each to a data path:")
    for name, info in fields.items():
        print(f'"{name}": ""   # {info["type"]}')
    return 0


# -- parser ------------------------------------------------------------------------
def _llm_args(parser) -> None:
    g = parser.add_argument_group("local LLM")
    g.add_argument("--model", help="model name (default qwen2.5:7b; env INVOICE_LLM_MODEL)")
    g.add_argument("--provider", choices=["ollama", "openai"],
                   help="ollama (default) or openai-compatible local server, e.g. LM Studio")
    g.add_argument("--llm-url", help="server URL (default http://localhost:11434 for ollama)")
    g.add_argument("--vision", choices=["auto", "always", "off"],
                   help="read page images with a vision model: auto (default) = only when there is no "
                        "text layer (scans/photos); always = also for normal PDFs (env INVOICE_VISION)")
    g.add_argument("--vision-model", help="vision model (default gemma3:12b; env INVOICE_VISION_MODEL)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="invoice", description="Template-driven invoice extraction and filling.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-T", "--templates-dir", action="append", default=[],
                   help="extra directory to search for templates (repeatable)")
    sub = p.add_subparsers(dest="command", required=True)

    e = sub.add_parser("extract", help="get data out of invoice files")
    e.add_argument("files", nargs="+")
    e.add_argument("-t", "--template", default="auto", help="template name/path, or 'auto' (default)")
    e.add_argument("-f", "--format", choices=["json", "yaml", "csv"], default="json")
    e.add_argument("-o", "--output")
    e.add_argument("--items", action="store_true", help="CSV: one row per line item")
    e.add_argument("--data-only", action="store_true", help="omit source/template/warnings wrapper")
    e.add_argument("--no-compute", action="store_true", help="don't derive missing totals")
    e.add_argument("--strict", action="store_true", help="exit 1 if any warnings")
    e.add_argument("-e", "--engine", choices=["verify", "llm", "rules"],
                   help="verify (default): rules extract, local LLM confirms/corrects each field; "
                        "llm: LLM only; rules: regex rules only (env INVOICE_ENGINE)")
    e.add_argument("-q", "--quiet", action="store_true", help="don't print the per-field report")
    _llm_args(e)
    e.set_defaults(func=cmd_extract)

    lc = sub.add_parser("llm-check", help="check the local LLM is running and the model is pulled")
    lc.add_argument("-t", "--template", default="default")
    _llm_args(lc)
    lc.set_defaults(func=cmd_llm_check)

    f = sub.add_parser("fill", help="generate / fill an invoice from data")
    f.add_argument("data", nargs="?", help="JSON or YAML data file ('-' for stdin)")
    f.add_argument("-o", "--output", required=True)
    f.add_argument("-t", "--template", default="default")
    f.add_argument("-f", "--format", choices=FORMATS, help="default: from output extension")
    f.add_argument("-l", "--layout", help="override layout file (.j2 / .docx / form .pdf)")
    f.add_argument("-s", "--set", action="append", default=[], metavar="KEY=VALUE",
                   help="override a value, e.g. --set buyer.name='Foo Inc' (repeatable)")
    f.add_argument("--no-compute", action="store_true", help="don't calculate amounts/totals")
    f.add_argument("--strict", action="store_true", help="exit 1 if any warnings")
    f.set_defaults(func=cmd_fill)

    i = sub.add_parser("inspect", help="show what the tool 'sees' in a file")
    i.add_argument("file")
    i.add_argument("-n", "--line-numbers", action="store_true")
    i.set_defaults(func=cmd_inspect)

    t = sub.add_parser("templates", help="list / show / create templates")
    t.add_argument("action", choices=["list", "show", "path", "new"])
    t.add_argument("name", nargs="?")
    t.add_argument("--from", dest="base", default="default", help="base template for 'new'")
    t.add_argument("--dir", help="where to create the new template (default ./templates)")
    t.add_argument("--force", action="store_true")
    t.set_defaults(func=cmd_templates)

    s = sub.add_parser("sample", help="write example invoice data")
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_sample)

    d = sub.add_parser("docx-starter", help="write an editable Word template with placeholders")
    d.add_argument("output")
    d.add_argument("-t", "--template", default="default")
    d.set_defaults(func=cmd_docx_starter)

    ff = sub.add_parser("form-fields", help="list fields of a fillable PDF")
    ff.add_argument("file")
    ff.set_defaults(func=cmd_form_fields)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except (TemplateError, ReaderError, FillError, FileNotFoundError, LLMError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
