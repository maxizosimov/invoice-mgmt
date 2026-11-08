"""Generate a varied set of realistic *fake* invoices with an answer key.

Every invoice uses its own hand-made layout (not this tool's templates), so they
exercise the extractor the way real-world invoices from different senders would:
different labels, date and number formats, languages, multi-page tables, scans,
phone photos, a thermal receipt, a Word file and a plain-text email.

All companies, people and numbers are fictional.

    invoice testset -o test-invoices      # writes the files + truth.json
    invoice eval test-invoices            # score extraction against truth.json
"""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any, Callable, Dict, List

from .model import compute_totals

# ---------------------------------------------------------------------------
# number / date formatting per locale
# ---------------------------------------------------------------------------
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
          "September", "Oktober", "November", "Dezember"]


def us_money(v, sym="$"):
    return f"{'-' if v < 0 else ''}{sym}{abs(v):,.2f}"


def eu_money(v, sym="€"):
    s = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{'-' if v < 0 else ''}{s} {sym}"


def num(v):
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def d_parts(iso):
    y, m, d = (int(x) for x in iso.split("-"))
    return y, m, d


def date_long(iso):            # March 3, 2026
    y, m, d = d_parts(iso)
    return f"{MONTHS[m - 1]} {d}, {y}"


def date_short_month(iso):     # 03 Mar 2026
    y, m, d = d_parts(iso)
    return f"{d:02d} {MONTHS[m - 1][:3]} {y}"


def date_us(iso):              # 03/14/2026
    y, m, d = d_parts(iso)
    return f"{m:02d}/{d:02d}/{y}"


def date_uk(iso):              # 14/03/2026
    y, m, d = d_parts(iso)
    return f"{d:02d}/{m:02d}/{y}"


def date_de(iso):              # 14.03.2026
    y, m, d = d_parts(iso)
    return f"{d:02d}.{m:02d}.{y}"


# ---------------------------------------------------------------------------
# invoice data (the answer key)
# ---------------------------------------------------------------------------
def _items(rows):
    return [{"description": d, "quantity": q, "unit_price": p} for d, q, p in rows]


def build_cases() -> List[Dict[str, Any]]:
    rng = random.Random(42)
    wholesale_products = [
        "Ceramic mug 350ml white", "Ceramic mug 350ml black", "Espresso cup set (4)", "Latte glass 300ml",
        "Teapot 1.2L stoneware", "Coffee grinder burr", "Pour-over kettle 1L", "Paper filters #4 (100)",
        "Cold brew jar 1.5L", "Milk jug stainless 600ml", "Tamper 58mm", "Knock box small",
        "Barista towel (3)", "Syrup pump", "Bamboo tray", "Sugar dispenser glass", "Stirrers wooden (1000)",
        "Takeaway cup 12oz (50)", "Takeaway lid 12oz (50)", "Cup sleeve kraft (100)", "Napkins 2-ply (500)",
        "Chalkboard A-frame", "Menu holder acrylic", "Cleaning tablets (100)", "Descaler 1L",
        "Group head brush", "Thermometer clip-on", "Scale 2kg 0.1g", "Shot glass 60ml (2)",
        "Storage canister 1kg", "Biscuit jar", "Cake stand 30cm", "Serving tongs", "Ice scoop",
    ]
    cases: List[Dict[str, Any]] = [
        {
            "file": "01-modern-services.pdf", "kind": "Text PDF, modern 2-column layout, hours x rate",
            "layout": "modern", "data": {
                "invoice_number": "BL-2026-0318", "issue_date": "2026-03-18", "due_date": "2026-04-17",
                "currency": "USD", "po_number": "PO-55102",
                "seller": {"name": "Bluelake Digital Studio", "address": "88 Pine Street, Floor 3\nPortland, OR 97204",
                           "email": "accounts@bluelake.example"},
                "buyer": {"name": "Riverside Dental Group", "address": "1450 River Road\nEugene, OR 97401",
                          "email": "billing@riversidedental.example"},
                "items": _items([("UX research & interviews", 14, 120.0), ("Website design (8 templates)", 1, 3800.0),
                                 ("Front-end development", 32.5, 110.0), ("Content migration", 6, 85.0)]),
                "tax_rate": 0, "payment_terms": "Net 30"},
        },
        {
            "file": "02-freelancer-minimal.pdf", "kind": "Text PDF, monospace, minimal labels",
            "layout": "minimal", "data": {
                "invoice_number": "0047", "issue_date": "2026-02-02", "due_date": "2026-02-16",
                "currency": "USD",
                "seller": {"name": "Jordan Lee Photography", "email": "jordan@jlphoto.example"},
                "buyer": {"name": "Maple & Oak Weddings", "address": "22 Elm Court\nBurlington, VT 05401"},
                "items": _items([("Wedding coverage (8h)", 1, 2400.0), ("Second shooter", 1, 600.0),
                                 ("Printed album 30x30", 1, 450.0), ("Travel", 120, 0.67)]),
                "tax_rate": 6.0},
        },
        {
            "file": "03-wholesale-multipage.pdf", "kind": "Text PDF, 2 pages, SKU table, discount + shipping",
            "layout": "wholesale", "data": {
                "invoice_number": "SO-884213", "issue_date": "2026-01-27", "due_date": "2026-02-26",
                "currency": "USD", "po_number": "4500018823",
                "seller": {"name": "Copperline Wholesale Supply", "address": "4100 Commerce Blvd\nReno, NV 89502"},
                "buyer": {"name": "The Daily Grind Cafe", "address": "9 Main Street\nBoise, ID 83702"},
                "items": [{"sku": f"CW-{1000 + i * 7}", "description": p, "quantity": rng.choice([2, 4, 6, 10, 12, 24]),
                           "unit_price": round(rng.uniform(1.5, 65), 2)} for i, p in enumerate(wholesale_products)],
                "discount": 75.0, "shipping": 48.5, "tax_rate": 7.25, "payment_terms": "Net 30"},
        },
        {
            "file": "04-uk-vat.pdf", "kind": "Text PDF, UK 'Tax Invoice', GBP, VAT 20%, dd/mm/yyyy",
            "layout": "uk", "data": {
                "invoice_number": "INV/24/1193", "issue_date": "2026-04-03", "due_date": "2026-05-03",
                "currency": "GBP",
                "seller": {"name": "Hartley & Finch Ltd", "address": "Unit 4, Canal Wharf\nLeeds LS11 5PS",
                           "tax_id": "GB 284 1193 07"},
                "buyer": {"name": "Northgate Lettings", "address": "17 Station Approach\nYork YO24 1AB"},
                "items": _items([("Boiler service", 3, 95.0), ("Gas safety certificate (CP12)", 3, 65.0),
                                 ("Replacement thermostat", 1, 142.5), ("Call-out (out of hours)", 1, 80.0)]),
                "tax_rate": 20.0, "payment_terms": "30 days"},
        },
        {
            "file": "05-german-rechnung.pdf", "kind": "Text PDF in German, EUR, 1.234,56 €, MwSt 19%",
            "layout": "german", "data": {
                "invoice_number": "RE-2026-0412", "issue_date": "2026-03-12", "due_date": "2026-03-26",
                "currency": "EUR",
                "seller": {"name": "Weber Haustechnik GmbH", "address": "Industriestraße 12\n70565 Stuttgart",
                           "tax_id": "DE 312 456 789"},
                "buyer": {"name": "Bäckerei Schmidt", "address": "Marktplatz 3\n71032 Böblingen"},
                "items": _items([("Wartung Kälteanlage", 1, 420.0), ("Arbeitszeit Techniker (Std.)", 5.5, 68.0),
                                 ("Kältemittel R290 (kg)", 2, 39.9), ("Anfahrtspauschale", 1, 45.0)]),
                "tax_rate": 19.0},
        },
        {
            "file": "06-utility-bill.pdf", "kind": "Text PDF, utility statement: account box, 'Amount due'",
            "layout": "utility", "data": {
                "invoice_number": "7731-0092-4418", "issue_date": "2026-02-28", "due_date": "2026-03-21",
                "currency": "USD",
                "seller": {"name": "Cascade Power & Water"},
                "buyer": {"name": "Alex Moreno", "address": "301 Birch Lane Apt 2\nTacoma, WA 98402"},
                "items": _items([("Electricity 612 kWh", 612, 0.1132), ("Water 4,200 gal", 1, 38.4),
                                 ("Basic service charge", 1, 12.5)]),
                "tax_rate": 3.85},
        },
        {
            "file": "07-receipt.pdf", "kind": "Text PDF, narrow thermal till receipt",
            "layout": "receipt", "data": {
                "invoice_number": "R-20419", "issue_date": "2026-05-09", "currency": "USD",
                "seller": {"name": "Greenfield Hardware", "address": "512 Oak Ave, Madison, WI 53703"},
                "items": _items([("Deck screws 5lb", 2, 24.99), ("Wood stain 1gal", 1, 39.95),
                                 ("Paint brush 3in", 3, 7.49), ("Drop cloth 9x12", 1, 12.99)]),
                "tax_rate": 5.5},
        },
        {
            "file": "08-consulting.docx", "kind": "Word document with tables",
            "layout": "docx", "data": {
                "invoice_number": "AC-1007", "issue_date": "2026-06-30", "due_date": "2026-07-30",
                "currency": "USD",
                "seller": {"name": "Ashcroft Consulting LLC", "address": "200 Congress Ave, Suite 900\nAustin, TX 78701",
                           "email": "finance@ashcroft.example"},
                "buyer": {"name": "Lone Star Logistics Inc.", "address": "7800 Freight Way\nHouston, TX 77029"},
                "items": _items([("Supply-chain assessment (fixed fee)", 1, 12500.0),
                                 ("Workshop facilitation (days)", 2, 1800.0),
                                 ("Travel expenses (at cost)", 1, 642.18)]),
                "tax_rate": 0, "payment_terms": "Net 30"},
        },
        {
            "file": "09-email-invoice.txt", "kind": "Plain-text invoice pasted from an email",
            "layout": "email", "data": {
                "invoice_number": "2026-031", "issue_date": "2026-03-31", "due_date": "2026-04-14",
                "currency": "CAD",
                "seller": {"name": "Northern Code Works", "email": "hello@northerncode.example"},
                "buyer": {"name": "Prairie Grain Co-op"},
                "items": _items([("Maintenance retainer - March", 1, 1500.0), ("Bug fixes (hours)", 7.5, 95.0)]),
                "tax_rate": 5.0},
        },
    ]
    # Scans / photos of earlier layouts with new data
    cases += [
        {
            "file": "10-scanned-modern.pdf", "kind": "SCAN: image-only PDF (no text layer), slightly rotated, noisy",
            "layout": "modern", "image": "scan", "data": {
                "invoice_number": "BL-2026-0402", "issue_date": "2026-04-02", "due_date": "2026-05-02",
                "currency": "USD",
                "seller": {"name": "Bluelake Digital Studio", "address": "88 Pine Street, Floor 3\nPortland, OR 97204",
                           "email": "accounts@bluelake.example"},
                "buyer": {"name": "Summit Outdoor Gear", "address": "620 Alpine Way\nBend, OR 97701"},
                "items": _items([("Brand refresh workshop", 1, 2200.0), ("Logo & identity system", 1, 4500.0),
                                 ("Design hours", 18, 115.0)]),
                "tax_rate": 0, "payment_terms": "Net 30"},
        },
        {
            "file": "11-phone-photo-uk.jpg", "kind": "PHOTO: JPEG of a printed UK invoice, tilted, uneven light",
            "layout": "uk", "image": "photo", "data": {
                "invoice_number": "INV/24/1207", "issue_date": "2026-04-19", "due_date": "2026-05-19",
                "currency": "GBP",
                "seller": {"name": "Hartley & Finch Ltd", "address": "Unit 4, Canal Wharf\nLeeds LS11 5PS",
                           "tax_id": "GB 284 1193 07"},
                "buyer": {"name": "Castlegate Hotel", "address": "1 Castlegate\nYork YO1 9RN"},
                "items": _items([("Kitchen extractor service", 1, 260.0), ("Replace fan motor", 1, 318.75),
                                 ("Labour (hours)", 4, 55.0)]),
                "tax_rate": 20.0, "payment_terms": "30 days"},
        },
        {
            "file": "12-photo-receipt.png", "kind": "PHOTO: PNG of a till receipt",
            "layout": "receipt", "image": "photo", "data": {
                "invoice_number": "R-20577", "issue_date": "2026-05-21", "currency": "USD",
                "seller": {"name": "Greenfield Hardware", "address": "512 Oak Ave, Madison, WI 53703"},
                "items": _items([("Garden hose 50ft", 1, 34.99), ("Hose nozzle", 1, 9.99),
                                 ("Potting soil 1cu ft", 4, 8.49)]),
                "tax_rate": 5.5},
        },
    ]
    for case in cases:
        compute_totals(case["data"])
    return cases


# ---------------------------------------------------------------------------
# PDF layouts (reportlab canvas, each deliberately different)
# ---------------------------------------------------------------------------
def _lines(c, x, y, lines, font="Helvetica", size=9.5, leading=12.5, color=None):
    from reportlab.lib import colors
    c.setFont(font, size)
    c.setFillColor(color or colors.black)
    for line in lines:
        c.drawString(x, y, line)
        y -= leading
    return y


def _right(c, x, y, text, font="Helvetica", size=9.5):
    c.setFont(font, size)
    c.drawRightString(x, y, text)


def layout_modern(path, d):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    W, H = LETTER
    c = canvas.Canvas(str(path), pagesize=LETTER)
    teal = colors.HexColor("#0f766e")
    c.setFillColor(teal)
    c.rect(0, H - 95, W, 95, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(40, H - 50, d["seller"]["name"])
    c.setFont("Helvetica", 9)
    for i, line in enumerate(d["seller"]["address"].split("\n") + [d["seller"]["email"]]):
        c.drawString(40, H - 66 - i * 11, line)
    c.setFont("Helvetica-Bold", 26)
    c.drawRightString(W - 40, H - 55, "Invoice")

    y = H - 135
    c.setFillColor(teal)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(40, y, "BILLED TO")
    _lines(c, 40, y - 15, [d["buyer"]["name"]] + d["buyer"]["address"].split("\n") + [d["buyer"].get("email", "")])
    meta = [("Invoice No.", d["invoice_number"]), ("Date of issue", date_long(d["issue_date"])),
            ("Payment due", date_long(d["due_date"]))]
    if d.get("po_number"):
        meta.append(("Your PO", d["po_number"]))
    for i, (k, v) in enumerate(meta):
        c.setFillColor(colors.grey)
        _right(c, W - 160, y - i * 15, k, size=9)
        c.setFillColor(colors.black)
        _right(c, W - 40, y - i * 15, v, "Helvetica-Bold", 9.5)

    y -= 95
    cols = [(40, "Description", "l"), (W - 230, "Hours / Qty", "r"), (W - 140, "Rate", "r"), (W - 40, "Amount", "r")]
    c.setFillColor(colors.HexColor("#e6f2f1"))
    c.rect(32, y - 6, W - 64, 20, fill=1, stroke=0)
    c.setFillColor(teal)
    c.setFont("Helvetica-Bold", 9)
    for x, label, a in cols:
        if a == "l":
            c.drawString(x, y, label)
        else:
            c.drawRightString(x, y, label)
    c.setFillColor(colors.black)
    y -= 24
    for it in d["items"]:
        c.setFont("Helvetica", 9.5)
        c.drawString(40, y, it["description"])
        _right(c, W - 230, y, num(it["quantity"]))
        _right(c, W - 140, y, us_money(it["unit_price"]))
        _right(c, W - 40, y, us_money(it["amount"]))
        c.setStrokeColor(colors.HexColor("#dddddd"))
        c.line(32, y - 7, W - 32, y - 7)
        y -= 22
    y -= 10
    rows = [("Subtotal", us_money(d["subtotal"])), (f"Tax ({num(d['tax_rate'])}%)", us_money(d["tax"]))]
    for k, v in rows:
        _right(c, W - 140, y, k)
        _right(c, W - 40, y, v)
        y -= 16
    c.setFillColor(teal)
    _right(c, W - 140, y - 6, "Amount due (USD)", "Helvetica-Bold", 11)
    _right(c, W - 40, y - 6, us_money(d["total"]), "Helvetica-Bold", 13)
    c.setFillColor(colors.grey)
    _lines(c, 40, 90, [f"Terms: {d.get('payment_terms', '')}. Please include the invoice number with your payment.",
                       "ACH: Bluelake Digital Studio, Routing 000000000, Account 0000000000"], size=8,
           color=colors.grey)
    c.save()


def layout_minimal(path, d):
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    W, H = LETTER
    c = canvas.Canvas(str(path), pagesize=LETTER)
    y = H - 70
    y = _lines(c, 60, y, [d["seller"]["name"].upper(), d["seller"]["email"], ""], "Courier-Bold", 11, 14)
    y = _lines(c, 60, y, [f"invoice {d['invoice_number']}", f"date    {date_us(d['issue_date'])}",
                          f"due     {date_us(d['due_date'])}", "", "to:", d["buyer"]["name"]]
               + d["buyer"]["address"].split("\n") + ["", "-" * 64], "Courier", 10, 13)
    for it in d["items"]:
        line = f"{it['description'][:30]:<30} {num(it['quantity']):>6} x {it['unit_price']:>8.2f} {it['amount']:>10.2f}"
        y = _lines(c, 60, y, [line], "Courier", 10, 13)
    y = _lines(c, 60, y, ["-" * 64,
                          f"{'subtotal':>52} {d['subtotal']:>10.2f}",
                          f"{'sales tax ' + num(d['tax_rate']) + '%':>52} {d['tax']:>10.2f}",
                          f"{'TOTAL USD':>52} {d['total']:>10.2f}", "",
                          "thanks! venmo @jordanlee-photo or check payable to Jordan Lee"], "Courier", 10, 13)
    c.save()


def layout_wholesale(path, d):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet

    ss = getSampleStyleSheet()
    story = []
    head = Table([[Paragraph(f"<b>{d['seller']['name']}</b><br/>" + d["seller"]["address"].replace("\n", "<br/>")
                             + "<br/>Tel (775) 555-0142", ss["Normal"]),
                   Paragraph("<font size=16><b>SALES INVOICE</b></font><br/><br/>"
                             f"Document: {d['invoice_number']}<br/>Invoice date: {date_us(d['issue_date'])}<br/>"
                             f"Due date: {date_us(d['due_date'])}<br/>Customer PO: {d['po_number']}", ss["Normal"])]],
                 colWidths=[100 * mm, 80 * mm])
    story += [head, Spacer(1, 8 * mm)]
    story += [Table([[Paragraph("<b>SOLD TO</b><br/>" + d["buyer"]["name"] + "<br/>"
                                + d["buyer"]["address"].replace("\n", "<br/>"), ss["Normal"]),
                      Paragraph("<b>SHIP TO</b><br/>Same as sold-to<br/>Ship via: Ground freight", ss["Normal"])]],
                    colWidths=[90 * mm, 90 * mm]), Spacer(1, 6 * mm)]
    rows = [["Line", "Item #", "Description", "Qty", "Unit", "Ext. Price"]]
    for i, it in enumerate(d["items"], 1):
        rows.append([str(i), it["sku"], it["description"], num(it["quantity"]), f"{it['unit_price']:,.2f}",
                     f"{it['amount']:,.2f}"])
    t = Table(rows, colWidths=[12 * mm, 22 * mm, 80 * mm, 15 * mm, 22 * mm, 27 * mm], repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dddddd")),
                           ("FONTSIZE", (0, 0), (-1, -1), 8.5), ("ALIGN", (3, 0), (-1, -1), "RIGHT")]))
    story += [t, Spacer(1, 5 * mm)]
    tot = Table([["Merchandise total", f"{d['subtotal']:,.2f}"], ["Less: promo discount", f"-{d['discount']:,.2f}"],
                 ["Freight", f"{d['shipping']:,.2f}"], [f"Sales tax {num(d['tax_rate'])}%", f"{d['tax']:,.2f}"],
                 ["INVOICE TOTAL (USD)", f"{d['total']:,.2f}"]], colWidths=[50 * mm, 30 * mm], hAlign="RIGHT")
    tot.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                             ("LINEABOVE", (0, -1), (-1, -1), 1, colors.black)]))
    story += [tot, Spacer(1, 6 * mm),
              Paragraph(f"Terms: {d['payment_terms']}. Claims must be made within 7 days of receipt.", ss["Normal"])]

    def footer(cv, doc):
        cv.setFont("Helvetica", 8)
        cv.drawRightString(LETTER[0] - 40, 30, f"Page {doc.page}")
    SimpleDocTemplate(str(path), pagesize=LETTER).build(story, onFirstPage=footer, onLaterPages=footer)


def layout_uk(path, d):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    W, H = A4
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Times-Bold", 22)
    c.drawString(50, H - 70, d["seller"]["name"])
    _lines(c, 50, H - 88, d["seller"]["address"].split("\n") + [f"VAT Reg. No. {d['seller']['tax_id']}"],
           "Times-Roman", 10, 12)
    c.setFont("Times-Bold", 16)
    c.drawRightString(W - 50, H - 70, "TAX INVOICE")
    for i, (k, v) in enumerate([("Invoice number:", d["invoice_number"]), ("Tax point / date:", date_uk(d["issue_date"])),
                                ("Due date:", date_uk(d["due_date"]))]):
        c.setFont("Times-Roman", 10)
        c.drawRightString(W - 140, H - 92 - i * 13, k)
        c.drawRightString(W - 50, H - 92 - i * 13, v)
    c.setStrokeColor(colors.black)
    c.rect(50, H - 225, 220, 75)
    _lines(c, 58, H - 165, ["Invoice to:", d["buyer"]["name"]] + d["buyer"]["address"].split("\n"), "Times-Roman", 10, 13)
    y = H - 265
    heads = [(50, "Qty"), (90, "Description"), (W - 200, "Unit price"), (W - 125, "VAT"), (W - 50, "Net")]
    c.setFont("Times-Bold", 10)
    for x, h in heads:
        (c.drawString if x < 100 else c.drawRightString)(x, y, h)
    c.line(50, y - 5, W - 50, y - 5)
    y -= 20
    for it in d["items"]:
        c.setFont("Times-Roman", 10)
        c.drawString(50, y, num(it["quantity"]))
        c.drawString(90, y, it["description"])
        c.drawRightString(W - 200, y, us_money(it["unit_price"], "£"))
        c.drawRightString(W - 125, y, f"{num(d['tax_rate'])}%")
        c.drawRightString(W - 50, y, us_money(it["amount"], "£"))
        y -= 16
    c.line(50, y + 6, W - 50, y + 6)
    y -= 10
    for k, v, bold in [("Total net", us_money(d["subtotal"], "£"), False),
                       (f"VAT @ {num(d['tax_rate'])}%", us_money(d["tax"], "£"), False),
                       ("Total payable", us_money(d["total"], "£"), True)]:
        c.setFont("Times-Bold" if bold else "Times-Roman", 11 if bold else 10)
        c.drawRightString(W - 130, y, k)
        c.drawRightString(W - 50, y, v)
        y -= 16
    _lines(c, 50, 110, [f"Payment terms: {d['payment_terms']}.",
                        "Bank: Northern Counties Bank  Sort code 00-00-00  Account 00000000",
                        "Registered in England & Wales No. 07311932"], "Times-Italic", 9, 12)
    c.save()


def layout_german(path, d):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    W, H = A4
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 7)
    c.drawString(60, H - 140, f"{d['seller']['name']} · {d['seller']['address'].replace(chr(10), ' · ')}")
    _lines(c, 60, H - 160, [d["buyer"]["name"]] + d["buyer"]["address"].split("\n"), size=10.5, leading=14)
    c.setFont("Helvetica-Bold", 15)
    c.drawRightString(W - 60, H - 70, d["seller"]["name"])
    _lines(c, W - 220, H - 160, [f"Rechnungsnummer:  {d['invoice_number']}",
                                 f"Rechnungsdatum:   {date_de(d['issue_date'])}",
                                 f"Kundennummer:     K-20931",
                                 f"Leistungsdatum:   {date_de(d['issue_date'])}"], size=9.5)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(60, H - 260, f"Rechnung {d['invoice_number']}")
    _lines(c, 60, H - 285, ["Sehr geehrte Damen und Herren,",
                            "für unsere Leistungen erlauben wir uns, Ihnen folgendes in Rechnung zu stellen:"], size=10)
    y = H - 330
    c.setFillColor(colors.HexColor("#eeeeee"))
    c.rect(56, y - 5, W - 112, 18, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 9)
    for x, h, r in [(62, "Pos.", 0), (95, "Bezeichnung", 0), (W - 220, "Menge", 1), (W - 140, "Einzelpreis", 1),
                    (W - 64, "Gesamt", 1)]:
        (c.drawRightString if r else c.drawString)(x, y, h)
    y -= 20
    for i, it in enumerate(d["items"], 1):
        c.setFont("Helvetica", 9.5)
        c.drawString(62, y, str(i))
        c.drawString(95, y, it["description"])
        c.drawRightString(W - 220, y, num(it["quantity"]).replace(".", ","))
        c.drawRightString(W - 140, y, eu_money(it["unit_price"]))
        c.drawRightString(W - 64, y, eu_money(it["amount"]))
        y -= 17
    y -= 8
    for k, v, b in [("Nettobetrag", eu_money(d["subtotal"]), 0), (f"zzgl. {num(d['tax_rate'])} % MwSt.", eu_money(d["tax"]), 0),
                    ("Gesamtbetrag", eu_money(d["total"]), 1)]:
        c.setFont("Helvetica-Bold" if b else "Helvetica", 10.5 if b else 9.5)
        c.drawRightString(W - 140, y, k)
        c.drawRightString(W - 64, y, v)
        y -= 16
    _lines(c, 60, y - 25, [f"Bitte überweisen Sie den Rechnungsbetrag bis zum {date_de(d['due_date'])} ohne Abzug.",
                           "Mit freundlichen Grüßen", d["seller"]["name"]], size=9.5)
    c.setFont("Helvetica", 7)
    c.drawString(60, 50, f"USt-IdNr.: {d['seller']['tax_id']} · Amtsgericht Stuttgart HRB 123456 · "
                         "IBAN DE00 0000 0000 0000 0000 00")
    c.save()


def layout_utility(path, d):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    W, H = LETTER
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFillColor(colors.HexColor("#1d4ed8"))
    c.setFont("Helvetica-Bold", 18)
    c.drawString(40, H - 55, d["seller"]["name"])
    c.setFillColor(colors.black)
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 70, "Customer service 1-800-555-0199  ·  cascadepw.example")
    c.setStrokeColor(colors.HexColor("#1d4ed8"))
    c.roundRect(W - 250, H - 150, 210, 110, 6)
    _lines(c, W - 240, H - 60, ["Account number", d["invoice_number"], "", "Statement date",
                                date_short_month(d["issue_date"]), "", "Please pay by", date_short_month(d["due_date"])],
           size=9, leading=11)
    _lines(c, 40, H - 110, ["Service address:", d["buyer"]["name"]] + d["buyer"]["address"].split("\n"), size=10)
    y = H - 200
    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, y, "Summary of charges  (billing period Feb 1 - Feb 28)")
    y -= 22
    for it in d["items"]:
        c.setFont("Helvetica", 10)
        c.drawString(50, y, it["description"])
        if it["quantity"] != 1:
            c.drawString(300, y, f"@ ${it['unit_price']:.4f}")
        c.drawRightString(W - 60, y, f"{it['amount']:.2f}")
        y -= 16
    for k, v in [("Current charges before tax", d["subtotal"]), (f"State utility tax {d['tax_rate']}%", d["tax"])]:
        c.drawString(50, y, k)
        c.drawRightString(W - 60, y, f"{v:.2f}")
        y -= 16
    c.setFillColor(colors.HexColor("#1d4ed8"))
    c.rect(40, y - 30, W - 80, 30, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(50, y - 20, "Amount due")
    c.drawRightString(W - 60, y - 20, us_money(d["total"]))
    c.setFillColor(colors.black)
    c.setDash(3, 3)
    c.line(30, 170, W - 30, 170)
    _lines(c, 40, 150, ["Return this portion with your payment", f"Account {d['invoice_number']}",
                        f"Amount due {us_money(d['total'])}   Due {date_short_month(d['due_date'])}"], size=9)
    c.save()


def layout_receipt(path, d):
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    W = 80 * mm
    H = (120 + 14 * len(d["items"])) * mm / 2.2
    c = canvas.Canvas(str(path), pagesize=(W, H))
    y = H - 20
    c.setFont("Courier-Bold", 11)
    c.drawCentredString(W / 2, y, d["seller"]["name"].upper())
    y -= 12
    c.setFont("Courier", 7.5)
    c.drawCentredString(W / 2, y, d["seller"]["address"])
    y -= 10
    c.drawCentredString(W / 2, y, "(608) 555-0117")
    y -= 16
    for line in [f"RECEIPT #{d['invoice_number']}", f"{date_us(d['issue_date'])} 14:32  REG 2  CASHIER: DANA", "-" * 38]:
        c.drawString(8, y, line)
        y -= 10
    for it in d["items"]:
        c.drawString(8, y, it["description"][:24].upper())
        c.drawRightString(W - 8, y, f"{it['amount']:.2f}")
        y -= 9
        if it["quantity"] != 1:
            c.drawString(16, y, f"{num(it['quantity'])} @ {it['unit_price']:.2f}")
            y -= 9
    for line, v in [("-" * 38, None), ("SUBTOTAL", d["subtotal"]), (f"TAX {num(d['tax_rate'])}%", d["tax"]),
                    ("TOTAL", d["total"]), ("VISA **** 4421", d["total"])]:
        c.setFont("Courier-Bold" if line == "TOTAL" else "Courier", 8.5 if line == "TOTAL" else 7.5)
        c.drawString(8, y, line)
        if v is not None:
            c.drawRightString(W - 8, y, f"{v:.2f}")
        y -= 10
    c.setFont("Courier", 7.5)
    c.drawCentredString(W / 2, y - 6, "THANK YOU FOR SHOPPING LOCAL")
    c.save()


def layout_docx(path, d):
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    doc = docx.Document()
    h = doc.add_paragraph()
    r = h.add_run(d["seller"]["name"])
    r.bold, r.font.size, r.font.color.rgb = True, Pt(18), RGBColor(0x33, 0x33, 0x66)
    doc.add_paragraph(d["seller"]["address"].replace("\n", ", ") + " | " + d["seller"]["email"])
    t = doc.add_paragraph()
    t.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    t.add_run("STATEMENT OF SERVICES / INVOICE").bold = True
    meta = doc.add_table(rows=2, cols=4)
    meta.style = "Light Grid Accent 1"
    for i, (k, v) in enumerate([("Invoice", d["invoice_number"]), ("Issued", date_long(d["issue_date"])),
                                ("Client", d["buyer"]["name"]), ("Due", date_long(d["due_date"]))]):
        meta.rows[i // 2].cells[(i % 2) * 2].text = k
        meta.rows[i // 2].cells[(i % 2) * 2 + 1].text = v
    doc.add_paragraph()
    doc.add_paragraph("Client address: " + d["buyer"]["address"].replace("\n", ", "))
    items = doc.add_table(rows=1, cols=4)
    items.style = "Light List Accent 1"
    for cell, text in zip(items.rows[0].cells, ["Service", "Units", "Rate", "Line total"]):
        cell.text = text
    for it in d["items"]:
        row = items.add_row().cells
        row[0].text, row[1].text = it["description"], num(it["quantity"])
        row[2].text, row[3].text = us_money(it["unit_price"]), us_money(it["amount"])
    doc.add_paragraph()
    p = doc.add_paragraph(f"Fees total: {us_money(d['subtotal'])}\nSales tax: not applicable (professional services)\n")
    p.add_run(f"Balance due: {us_money(d['total'])}").bold = True
    doc.add_paragraph(f"Terms: {d['payment_terms']}. Wire details on request.")
    doc.save(str(path))


def layout_email(path, d):
    lines = [
        "From: Northern Code Works <hello@northerncode.example>",
        "To: accounts@prairiegrain.example",
        f"Subject: Invoice #{d['invoice_number']} - {d['buyer']['name']}",
        "",
        "Hi team,",
        "",
        f"Please find our invoice for March below. Payment is due by {date_long(d['due_date'])}.",
        "",
        f"  Invoice #:   {d['invoice_number']}",
        f"  Issued:      {date_long(d['issue_date'])}",
        f"  Bill to:     {d['buyer']['name']}",
        "",
    ]
    for it in d["items"]:
        lines.append(f"  - {it['description']}: {num(it['quantity'])} x ${it['unit_price']:.2f} = ${it['amount']:,.2f}")
    lines += ["",
              f"  Subtotal:    ${d['subtotal']:,.2f} CAD",
              f"  GST ({num(d['tax_rate'])}%):    ${d['tax']:,.2f}",
              f"  Total:       ${d['total']:,.2f} CAD",
              "", "E-transfer to hello@northerncode.example works best.", "", "Cheers,", "Sam", d["seller"]["name"]]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


LAYOUTS: Dict[str, Callable] = {
    "modern": layout_modern, "minimal": layout_minimal, "wholesale": layout_wholesale, "uk": layout_uk,
    "german": layout_german, "utility": layout_utility, "receipt": layout_receipt,
    "docx": layout_docx, "email": layout_email,
}


# ---------------------------------------------------------------------------
# scan / photo simulation
# ---------------------------------------------------------------------------
def _render_pdf(pdf_path, scale):
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        return pdf[0].render(scale=scale).to_pil().convert("RGB")
    finally:
        pdf.close()


def make_scan(pdf_path, out_path, seed=1):
    from PIL import Image, ImageFilter
    rng = random.Random(seed)
    img = _render_pdf(pdf_path, 200 / 72).convert("L")
    img = img.rotate(rng.uniform(-1.6, 1.6), resample=Image.BICUBIC, expand=True, fillcolor=255)
    noise = Image.effect_noise(img.size, 18).point(lambda v: 255 if v > 120 else v)
    img = Image.blend(img, noise.convert("L"), 0.08).filter(ImageFilter.GaussianBlur(0.5))
    img.save(out_path, "PDF", resolution=200)


def make_photo(pdf_path, out_path, seed=2):
    from PIL import Image, ImageDraw, ImageFilter
    rng = random.Random(seed)
    page = _render_pdf(pdf_path, 170 / 72)
    pad = 120
    bg = Image.new("RGB", (page.width + pad * 2, page.height + pad * 2), (92, 76, 64))  # wooden table
    bg.paste(page, (pad, pad))
    bg = bg.rotate(rng.uniform(3, 6), resample=Image.BICUBIC, expand=False, fillcolor=(92, 76, 64))
    # uneven lighting: dark gradient from one corner
    shade = Image.new("L", bg.size, 0)
    draw = ImageDraw.Draw(shade)
    for i in range(0, bg.width, 8):
        draw.line([(i, 0), (i, bg.height)], fill=int(90 * i / bg.width))
    bg = Image.composite(Image.new("RGB", bg.size, (0, 0, 0)), bg, shade.point(lambda v: v // 2))
    bg = bg.filter(ImageFilter.GaussianBlur(0.8))
    if str(out_path).lower().endswith(".png"):
        bg.save(out_path, "PNG", optimize=True)
    else:
        bg.save(out_path, "JPEG", quality=72)


# ---------------------------------------------------------------------------
def generate(out_dir) -> Path:
    import tempfile

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    truth: Dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for case in build_cases():
            target = out / case["file"]
            layout = LAYOUTS[case["layout"]]
            if case.get("image"):
                src = Path(tmp) / (target.stem + ".pdf")
                layout(src, case["data"])
                (make_scan if case["image"] == "scan" else make_photo)(src, target)
            else:
                layout(target, case["data"])
            truth[case["file"]] = {"kind": case["kind"], "data": copy.deepcopy(case["data"])}
    (out / "truth.json").write_text(json.dumps(truth, indent=2, ensure_ascii=False), encoding="utf-8")
    return out
