"""Generate a starter Word template full of ``{{ placeholders }}`` you can restyle in Word."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .template import Template


def build_starter_docx(path, template: Optional[Template] = None) -> Path:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    d = docx.Document()
    style = d.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10)

    title = d.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = title.add_run("{{ labels.title or 'INVOICE' }}")
    run.bold, run.font.size = True, Pt(24)

    seller = d.add_paragraph()
    seller.add_run("{{ seller.name }}").bold = True
    d.add_paragraph("{{ seller.address }}")
    d.add_paragraph("{{ seller.email }}")

    meta = d.add_table(rows=3, cols=2)
    for row, (label, expr) in zip(meta.rows, [
        ("Invoice #", "{{ invoice_number }}"),
        ("Invoice Date", "{{ issue_date | date }}"),
        ("Due Date", "{{ due_date | date }}"),
    ]):
        row.cells[0].text, row.cells[1].text = label, expr

    d.add_paragraph()
    d.add_paragraph().add_run("Bill To").bold = True
    d.add_paragraph("{{ buyer.name }}")
    d.add_paragraph("{{ buyer.address }}")
    d.add_paragraph()

    items = d.add_table(rows=2, cols=4)
    items.style = "Table Grid"
    for cell, text in zip(items.rows[0].cells, ["Description", "Qty", "Unit Price", "Amount"]):
        cell.text = text
        cell.paragraphs[0].runs[0].bold = True
    # This row is repeated once per line item (any row containing {{ item.* }}).
    for cell, text in zip(items.rows[1].cells, [
        "{{ item.description }}", "{{ item.quantity | num }}",
        "{{ item.unit_price | money }}", "{{ item.amount | money }}",
    ]):
        cell.text = text

    d.add_paragraph()
    totals = d.add_table(rows=3, cols=2)
    for row, (label, expr) in zip(totals.rows, [
        ("Subtotal", "{{ subtotal | money }}"),
        ("Tax{% if tax_rate %} ({{ tax_rate | num }}%){% endif %}", "{{ tax | money }}"),
        ("Total", "{{ total | money }}"),
    ]):
        row.cells[0].text, row.cells[1].text = label, expr
        row.cells[1].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
    totals.rows[2].cells[0].paragraphs[0].runs[0].bold = True
    totals.rows[2].cells[1].paragraphs[0].runs[0].bold = True

    d.add_paragraph()
    d.add_paragraph("{% if payment_terms %}Payment Terms: {{ payment_terms }}{% endif %}")
    d.add_paragraph("{% if notes %}Notes: {{ notes }}{% endif %}")

    path = Path(path)
    d.save(str(path))
    return path
