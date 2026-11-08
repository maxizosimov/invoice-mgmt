"""Produce invoices from data: PDF (generated), HTML/text (Jinja), DOCX (placeholders), PDF forms.

Everything is driven by the template's ``render:`` section, so changing the look of
an invoice means editing YAML (colors, labels, columns, currency) rather than code.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape

from .model import apply_defaults, compute_totals, parse_number, validate
from .template import BUILTIN_DIR, Template

FORMATS = ("pdf", "html", "docx", "pdf-form", "text")


class FillError(Exception):
    pass


# -- formatting helpers ------------------------------------------------------------
def money_formatter(cfg: Dict[str, Any]):
    symbol = cfg.get("currency_symbol", "$")
    decimals = int(cfg.get("decimals", 2))
    thousands = cfg.get("thousands_separator", ",")
    dec_sep = cfg.get("decimal_separator", ".")
    position = cfg.get("symbol_position", "before")

    def fmt(value: Any, with_symbol: bool = True) -> str:
        num = parse_number(value)
        if num is None:
            return "" if value is None else str(value)
        neg = num < 0
        s = f"{abs(num):,.{decimals}f}".replace(",", "\0").replace(".", dec_sep).replace("\0", thousands)
        if with_symbol and symbol:
            s = f"{symbol}{s}" if position == "before" else f"{s} {symbol}"
        return f"-{s}" if neg else s

    return fmt


def date_formatter(cfg: Dict[str, Any]):
    out_fmt = cfg.get("date_format", "%Y-%m-%d")

    def fmt(value: Any, pattern: Optional[str] = None) -> str:
        if not value:
            return ""
        try:
            return datetime.strptime(str(value), "%Y-%m-%d").strftime(pattern or out_fmt)
        except ValueError:
            return str(value)

    return fmt


def make_number_formatter(cfg: Dict[str, Any]):
    dec_sep = cfg.get("decimal_separator", ".")

    def fmt(value: Any, decimals: int = 2) -> str:
        num = parse_number(value)
        if num is None:
            return "" if value is None else str(value)
        if float(num).is_integer():
            return str(int(num))
        return f"{num:.{decimals}f}".rstrip("0").rstrip(".").replace(".", dec_sep)

    return fmt


number_formatter = make_number_formatter({})


def render_config(template: Template) -> Dict[str, Any]:
    cfg = dict(template.render)
    cfg.setdefault("labels", {})
    return cfg


def make_env(template: Template, search: Optional[List[Path]] = None, autoescape: bool = True) -> Environment:
    cfg = render_config(template)
    paths = [str(p) for p in (search or [])] + [str(template.base_dir), str(BUILTIN_DIR)]
    env = Environment(
        loader=FileSystemLoader(paths),
        undefined=ChainableUndefined,
        autoescape=select_autoescape(["html", "htm", "xml", "j2"]) if autoescape else False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["money"] = money_formatter(cfg)
    env.filters["date"] = date_formatter(cfg)
    env.filters["num"] = make_number_formatter(cfg)
    return env


def prepare(data: Dict[str, Any], template: Template, compute: bool = True) -> Dict[str, Any]:
    """Apply template defaults and compute totals on a copy of ``data``."""
    data = copy.deepcopy(data)
    apply_defaults(data, template.fields)
    if compute:
        compute_totals(data)
    return data


def context(data: Dict[str, Any], template: Template) -> Dict[str, Any]:
    cfg = render_config(template)
    return {**data, "invoice": data, "render": cfg, "labels": cfg.get("labels", {})}


def _render_string(env: Environment, s: str, ctx: Dict[str, Any]) -> str:
    if "{{" not in s and "{%" not in s:
        return s
    return env.from_string(s).render(**ctx)


# -- HTML / text (Jinja) ----------------------------------------------------------
def fill_jinja(data, template: Template, output: Path, layout: Optional[str] = None, fmt: str = "html") -> Path:
    section = template.render.get(fmt, {}) or {}
    layout = layout or section.get("template") or ("invoice.html.j2" if fmt == "html" else "invoice.txt.j2")
    layout_path = template.resolve_path(layout)
    search = [layout_path.parent] if layout_path.exists() else []
    env = make_env(template, search, autoescape=fmt == "html")
    tpl = env.get_template(layout_path.name if layout_path.exists() else layout)
    output.write_text(tpl.render(**context(data, template)), encoding="utf-8")
    return output


# -- PDF (generated with reportlab) -------------------------------------------------
DEFAULT_COLUMNS = [
    {"key": "description", "label": "Description", "width": 0.52, "align": "left"},
    {"key": "quantity", "label": "Qty", "width": 0.12, "align": "right", "format": "num"},
    {"key": "unit_price", "label": "Unit Price", "width": 0.18, "align": "right", "format": "money"},
    {"key": "amount", "label": "Amount", "width": 0.18, "align": "right", "format": "money"},
]


def fill_pdf(data, template: Template, output: Path) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4, LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    cfg = render_config(template)
    pdf_cfg = {**cfg, **(cfg.get("pdf") or {})}
    labels = cfg["labels"]
    money = money_formatter(pdf_cfg)
    fdate = date_formatter(pdf_cfg)
    number_formatter = make_number_formatter(pdf_cfg)
    env = make_env(template, autoescape=False)
    ctx = context(data, template)

    def L(key: str, default: str) -> str:
        return _render_string(env, str(labels.get(key, default)), ctx)

    accent = colors.HexColor(pdf_cfg.get("accent_color", "#1f4e79"))
    muted = colors.HexColor(pdf_cfg.get("muted_color", "#666666"))
    font = pdf_cfg.get("font", "Helvetica")
    bold = pdf_cfg.get("font_bold", "Helvetica-Bold")
    size = float(pdf_cfg.get("font_size", 9.5))
    page = LETTER if str(pdf_cfg.get("page_size", "letter")).lower() == "letter" else A4
    margin = float(pdf_cfg.get("margin_mm", 18)) * mm

    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["Normal"], fontName=font, fontSize=size, leading=size * 1.35)
    small = ParagraphStyle("small", parent=body, textColor=muted, fontSize=size * 0.9)
    strong = ParagraphStyle("strong", parent=body, fontName=bold)
    heading = ParagraphStyle("heading", parent=strong, textColor=accent, fontSize=size * 0.95, spaceAfter=2)
    title = ParagraphStyle("title", parent=strong, fontSize=size * 2.6, leading=size * 3, textColor=accent,
                           alignment=TA_RIGHT)
    right = ParagraphStyle("right", parent=body, alignment=TA_RIGHT)

    def P(text: Any, style=body) -> Paragraph:
        return Paragraph(escape("" if text is None else str(text)).replace("\n", "<br/>"), style)

    def party_lines(party: Dict[str, Any]) -> List[str]:
        party = party or {}
        lines = [party.get("address")]
        for key, prefix in (("email", ""), ("phone", ""), ("tax_id", L("tax_id", "Tax ID") + ": ")):
            if party.get(key):
                lines.append(f"{prefix}{party[key]}")
        return [l for l in lines if l]

    width = page[0] - 2 * margin
    story: List[Any] = []

    # Header: seller (left) / title + meta (right)
    seller = data.get("seller") or {}
    left: List[Any] = []
    logo = pdf_cfg.get("logo")
    if logo and template.resolve_path(logo).exists():
        img = Image(str(template.resolve_path(logo)))
        h = float(pdf_cfg.get("logo_height_mm", 14)) * mm
        img.drawWidth, img.drawHeight = img.imageWidth * h / img.imageHeight, h
        img.hAlign = "LEFT"
        left.append(img)
    left.append(P(seller.get("name"), ParagraphStyle("sn", parent=strong, fontSize=size * 1.4,
                                                      leading=size * 1.8)))
    left += [P("\n".join(party_lines(seller)), small)]

    meta_rows = []
    for key, label, value in (
        ("invoice_number", L("invoice_number", "Invoice #"), data.get("invoice_number")),
        ("issue_date", L("issue_date", "Invoice Date"), fdate(data.get("issue_date"))),
        ("due_date", L("due_date", "Due Date"), fdate(data.get("due_date"))),
        ("po_number", L("po_number", "PO #"), data.get("po_number")),
    ):
        if value:
            meta_rows.append([P(label, small), P(value, right)])
    meta = Table(meta_rows, colWidths=[width * 0.2, width * 0.25]) if meta_rows else Spacer(1, 1)
    if meta_rows:
        meta.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                                  ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    header = Table([[left, [P(L("title", "INVOICE"), title), Spacer(1, 4), meta]]],
                   colWidths=[width * 0.55, width * 0.45])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [header, Spacer(1, 10 * mm)]

    # Parties: bill to / ship to
    buyer = data.get("buyer") or {}
    party_cells = [[P(L("bill_to", "Bill To"), heading), P(buyer.get("name"), strong),
                    P("\n".join(party_lines(buyer)), body)]]
    ship = data.get("ship_to") or {}
    if ship.get("name") or ship.get("address"):
        party_cells.append([P(L("ship_to", "Ship To"), heading), P(ship.get("name"), strong),
                            P(ship.get("address"), body)])
    parties = Table([party_cells], colWidths=[width / len(party_cells)] * len(party_cells))
    parties.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [parties, Spacer(1, 8 * mm)]

    # Line items
    columns = pdf_cfg.get("columns") or DEFAULT_COLUMNS
    total_w = sum(float(c.get("width", 1)) for c in columns)
    col_widths = [width * float(c.get("width", 1)) / total_w for c in columns]
    head_style = ParagraphStyle("th", parent=strong, textColor=colors.white)

    def cell(item: Dict[str, Any], col: Dict[str, Any]) -> Paragraph:
        val = item.get(col["key"])
        f = col.get("format")
        if f == "money":
            val = money(val)
        elif f == "num":
            val = number_formatter(val)
        elif f == "date":
            val = fdate(val)
        style = right if col.get("align") == "right" else body
        return P(val, style)

    rows = [[P(L(f"col_{c['key']}", c.get("label", c["key"])),
               ParagraphStyle("thr", parent=head_style, alignment=TA_RIGHT) if c.get("align") == "right"
               else head_style) for c in columns]]
    for item in data.get("items") or []:
        rows.append([cell(item, c) for c in columns])
    items_tbl = Table(rows, colWidths=col_widths, repeatRows=1)
    zebra = colors.HexColor(pdf_cfg.get("zebra_color", "#f3f6f9"))
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), accent),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, muted),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for r in range(2, len(rows), 2):
        style_cmds.append(("BACKGROUND", (0, r), (-1, r), zebra))
    items_tbl.setStyle(TableStyle(style_cmds))
    story += [items_tbl, Spacer(1, 4 * mm)]

    # Totals
    tot_rows = [[P(L("subtotal", "Subtotal"), right), P(money(data.get("subtotal")), right)]]
    if parse_number(data.get("discount")):
        tot_rows.append([P(L("discount", "Discount"), right), P(money(-parse_number(data["discount"])), right)])
    if data.get("tax") not in (None, "") or data.get("tax_rate") not in (None, ""):
        rate = data.get("tax_rate")
        tax_label = L("tax", "Tax") + (f" ({number_formatter(rate)}%)" if rate not in (None, "") else "")
        tot_rows.append([P(tax_label, right), P(money(data.get("tax")), right)])
    if parse_number(data.get("shipping")):
        tot_rows.append([P(L("shipping", "Shipping"), right), P(money(data.get("shipping")), right)])
    big = ParagraphStyle("big", parent=right, fontName=bold, fontSize=size * 1.25, leading=size * 1.6,
                         textColor=accent)
    tot_rows.append([P(L("total", "Total"), big), P(money(data.get("total")), big)])
    totals = Table(tot_rows, colWidths=[width * 0.25, width * 0.2], hAlign="RIGHT")
    totals.setStyle(TableStyle([("LINEABOVE", (0, -1), (-1, -1), 1, accent),
                                ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    story += [totals, Spacer(1, 10 * mm)]

    # Notes / terms / payment instructions
    for key, default in (("payment_terms", "Payment Terms"), ("payment_instructions", "Payment Instructions"),
                         ("notes", "Notes")):
        if data.get(key):
            story += [P(L(key, default), heading), P(data[key], body), Spacer(1, 4 * mm)]

    footer_text = _render_string(env, str(pdf_cfg.get("footer", "")), ctx)

    def on_page(canvas, doc):
        if footer_text:
            canvas.saveState()
            canvas.setFont(font, size * 0.8)
            canvas.setFillColor(muted)
            canvas.drawCentredString(page[0] / 2, margin / 2, footer_text)
            canvas.restoreState()

    doc = SimpleDocTemplate(str(output), pagesize=page, leftMargin=margin, rightMargin=margin,
                            topMargin=margin, bottomMargin=margin,
                            title=f"{L('title', 'INVOICE').title()} {data.get('invoice_number', '')}".strip(),
                            author=str(seller.get("name") or ""))
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return output


# -- DOCX (Word file with {{ placeholders }}) --------------------------------------
def _iter_paragraphs(container):
    for p in container.paragraphs:
        yield p
    for t in getattr(container, "tables", []):
        for row in t.rows:
            for c in row.cells:
                yield from _iter_paragraphs(c)


def _render_paragraph(p, env: Environment, ctx: Dict[str, Any]) -> None:
    full = "".join(r.text for r in p.runs)
    if "{{" not in full and "{%" not in full:
        return
    rendered = env.from_string(full).render(**ctx)
    # Keep the formatting of the first run; placeholders split across runs are handled by joining.
    p.runs[0].text = rendered
    for r in p.runs[1:]:
        r.text = ""


def _expand_item_rows(document, env: Environment, ctx: Dict[str, Any]) -> None:
    """Rows that contain ``{{ item.* }}`` are repeated once per line item."""
    item_re = re.compile(r"\{\{[^}]*\bitem\.")
    for table in document.tables:
        for row in list(table.rows):
            row_text = "".join(c.text for c in row.cells)
            if not item_re.search(row_text):
                continue
            tr = row._tr
            parent = tr.getparent()
            anchor = tr
            items = ctx.get("items") or []
            for idx, item in enumerate(items, 1):
                new_tr = copy.deepcopy(tr)
                anchor.addnext(new_tr)
                anchor = new_tr
                from docx.table import _Row
                new_row = _Row(new_tr, table)
                item_ctx = {**ctx, "item": item, "index": idx}
                for c in new_row.cells:
                    for p in _iter_paragraphs(c):
                        _render_paragraph(p, env, item_ctx)
            parent.remove(tr)


def fill_docx(data, template: Template, output: Path, layout: Optional[str] = None) -> Path:
    import docx

    section = template.render.get("docx", {}) or {}
    src = layout or section.get("template")
    if not src:
        from .docx_starter import build_starter_docx
        src_path = output.with_suffix(".starter.tmp.docx")
        build_starter_docx(src_path, template)
        cleanup = True
    else:
        src_path, cleanup = template.resolve_path(src), False
        if not src_path.exists():
            raise FillError(f"DOCX template not found: {src_path}")

    document = docx.Document(str(src_path))
    if cleanup:
        src_path.unlink(missing_ok=True)
    env = make_env(template, autoescape=False)
    ctx = context(data, template)
    _expand_item_rows(document, env, ctx)
    for p in _iter_paragraphs(document):
        _render_paragraph(p, env, ctx)
    for s in document.sections:
        for part in (s.header, s.footer):
            for p in _iter_paragraphs(part):
                _render_paragraph(p, env, ctx)
    document.save(str(output))
    return output


# -- fillable PDF forms --------------------------------------------------------------
def fill_pdf_form(data, template: Template, output: Path, layout: Optional[str] = None) -> Path:
    from pypdf import PdfReader, PdfWriter

    section = template.render.get("pdf_form", {}) or {}
    src = layout or section.get("file")
    if not src:
        raise FillError("Template has no render.pdf_form.file (or pass --layout form.pdf)")
    src_path = template.resolve_path(src)
    if not src_path.exists():
        raise FillError(f"PDF form not found: {src_path}")
    mapping: Dict[str, str] = section.get("fields") or {}
    if not mapping:
        raise FillError("Template has no render.pdf_form.fields mapping (form field -> value expression)")

    env = make_env(template, autoescape=False)
    ctx = context(data, template)
    values = {}
    for form_field, expr in mapping.items():
        expr = str(expr)
        if "{{" not in expr and "{%" not in expr:
            expr = "{{ " + expr + " }}"  # bare path shorthand: `seller.name`
        values[form_field] = env.from_string(expr).render(**ctx)

    reader = PdfReader(str(src_path))
    writer = PdfWriter()
    writer.append(reader)
    flatten = bool(section.get("flatten"))
    for page in writer.pages:
        if page.get("/Annots"):
            writer.update_page_form_field_values(page, values, auto_regenerate=False, flatten=flatten)
    writer.set_need_appearances_writer(True)
    with open(output, "wb") as fh:
        writer.write(fh)
    return output


def list_pdf_form_fields(path) -> Dict[str, Any]:
    from pypdf import PdfReader

    fields = PdfReader(str(path)).get_fields() or {}
    return {name: {"type": str(f.get("/FT", "")), "value": f.get("/V")} for name, f in fields.items()}


# -- dispatcher -------------------------------------------------------------------
def guess_format(output: Path) -> str:
    ext = output.suffix.lower()
    return {".pdf": "pdf", ".html": "html", ".htm": "html", ".docx": "docx",
            ".txt": "text", ".md": "text"}.get(ext, "pdf")


def fill(data: Dict[str, Any], template: Template, output, fmt: Optional[str] = None,
         layout: Optional[str] = None, compute: bool = True) -> Dict[str, Any]:
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fmt = fmt or guess_format(output)
    if fmt not in FORMATS:
        raise FillError(f"Unknown format '{fmt}'. Choose from: {', '.join(FORMATS)}")
    prepared = prepare(data, template, compute)
    if fmt == "pdf":
        fill_pdf(prepared, template, output)
    elif fmt in ("html", "text"):
        fill_jinja(prepared, template, output, layout, fmt)
    elif fmt == "docx":
        fill_docx(prepared, template, output, layout)
    elif fmt == "pdf-form":
        fill_pdf_form(prepared, template, output, layout)
    return {"output": str(output), "format": fmt, "data": prepared,
            "warnings": validate(prepared, template.fields)}
