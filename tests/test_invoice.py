import json

import pytest
import yaml

from invoice_mgmt import extract, fill, load_template
from invoice_mgmt.cli import main
from invoice_mgmt.extract import detect_template, extract_field
from invoice_mgmt.model import compute_totals, parse_date, parse_number
from invoice_mgmt.readers import Document, read_document
from invoice_mgmt.template import TemplateError


# -- model ------------------------------------------------------------------------
@pytest.mark.parametrize("raw,sep,expected", [
    ("$1,234.50", ".", 1234.5),
    ("1.234,50 €", ",", 1234.5),
    ("(45.00)", ".", -45.0),
    ("", ".", None),
])
def test_parse_number(raw, sep, expected):
    assert parse_number(raw, sep) == expected


@pytest.mark.parametrize("raw", ["2026-11-08", "11/08/2026", "November 8th, 2026", "8 Nov 2026"])
def test_parse_date(raw):
    assert parse_date(raw) == "2026-11-08"


def test_parse_date_day_first():
    assert parse_date("08/11/2026", ["%d/%m/%Y"]) == "2026-11-08"


def test_compute_totals_does_not_overwrite_given_values():
    data = {"items": [{"quantity": 2, "unit_price": 10}], "tax_rate": 10, "total": 999}
    compute_totals(data)
    assert data["items"][0]["amount"] == 20
    assert data["subtotal"] == 20 and data["tax"] == 2.0
    assert data["total"] == 999


# -- templates --------------------------------------------------------------------
def test_extends_merges_and_replaces_rules():
    tpl = load_template("acme-supplies")
    assert tpl.render["currency_symbol"] == "$"            # inherited
    assert tpl.render["pdf"]["accent_color"] == "#b5121b"  # overridden
    assert tpl.render["pdf"]["page_size"] == "letter"      # deep-merged
    assert tpl.extract["fields"]["invoice_number"] == {"label": "Bill No."}  # replaced, not merged
    assert "subtotal" in tpl.extract["fields"]


def test_missing_template():
    with pytest.raises(TemplateError):
        load_template("does-not-exist")


def test_circular_extends(tmp_path):
    (tmp_path / "a.yaml").write_text("extends: b\n")
    (tmp_path / "b.yaml").write_text("extends: a\n")
    with pytest.raises(TemplateError, match="Circular"):
        load_template(str(tmp_path / "a.yaml"))


def test_extract_field_variants():
    text = "Ref No: X-1\nBill To:\nJane Doe\n12 Road\n\nOther"
    assert extract_field(text, {"label": "Ref No"}) == "X-1"
    assert extract_field(text, r"Ref\s+No:\s*(\S+)") == "X-1"
    assert extract_field(text, {"block": {"start": "^Bill To"}}) == "Jane Doe\n12 Road"
    assert extract_field(text, {"block": {"start": "^Bill To", "select": "1:"}}) == "12 Road"
    assert extract_field(text, {"value": "const"}) == "const"


# -- round trips: fill then extract ------------------------------------------------
@pytest.mark.parametrize("ext", ["pdf", "docx", "txt"])
def test_round_trip_default(tmp_path, sample, ext):
    out = tmp_path / f"inv.{ext}"
    fill(sample, load_template("default"), out)
    res = extract(out, load_template("default"))
    d = res["data"]
    assert res["warnings"] == []
    assert d["invoice_number"] == "INV-2026-0042"
    assert d["issue_date"] == "2026-11-08"
    assert d["due_date"] == "2026-12-08"
    assert d["seller"]["name"] == "Northwind Studio LLC"
    assert d["buyer"]["name"] == "Contoso Ltd."
    assert d["subtotal"] == 6375.0
    assert d["tax"] == 541.88
    assert d["total"] == 6916.88
    assert [i["description"] for i in d["items"]] == [
        "Website redesign", "Consulting hours", "Hosting (12 months)"]
    assert d["items"][1]["quantity"] == 12.5


def test_round_trip_eu(tmp_path, sample):
    sample["seller"]["tax_id"] = "DE123456789"
    tpl = load_template("eu-vat")
    out = tmp_path / "eu.pdf"
    fill(sample, tpl, out)
    text = read_document(out).text
    assert "6.916,88 €" in text and "VAT (8,5%)" in text and "08.11.2026" in text
    doc = read_document(out)
    assert detect_template(doc.text)[0].name == "eu-vat"
    res = extract(doc, tpl)
    assert res["warnings"] == []
    assert res["data"]["total"] == 6916.88
    assert res["data"]["tax_rate"] == 8.5
    assert res["data"]["seller"]["tax_id"] == "DE123456789"
    assert len(res["data"]["items"]) == 3


def test_vendor_template_autodetect(examples):
    res = extract(examples / "acme-bill.txt")
    assert res["template"] == "acme-supplies"
    d = res["data"]
    assert d["invoice_number"] == "AS-55120"
    assert d["issue_date"] == "2026-10-28"
    assert d["items"][1] == {"sku": "TN-220", "description": "Toner cartridge black high-yield",
                             "quantity": 2.0, "unit_price": 89.99, "amount": 179.98}
    assert d["total"] == 357.36
    assert res["warnings"] == []


def test_table_mode_extraction():
    doc = Document(path=None, text="Invoice # A-100\nTotal 30.00", tables=[[
        ["Item", "Hours", "Rate", "Line Total"],
        ["Design", "2", "10.00", "20.00"],
        ["Review", "1", "10.00", "10.00"],
    ]])
    res = extract(doc, load_template("default"))
    assert res["data"]["items"] == [
        {"description": "Design", "quantity": 2.0, "unit_price": 10.0, "amount": 20.0},
        {"description": "Review", "quantity": 1.0, "unit_price": 10.0, "amount": 10.0},
    ]
    assert res["data"]["subtotal"] == 30.0


def test_validation_flags_bad_math():
    doc = Document(path=None, text="Invoice # A-1\nSubtotal 10.00\nTotal 50.00")
    res = extract(doc, load_template("default"))
    assert any("total" in w for w in res["warnings"])


# -- fillable PDF forms ------------------------------------------------------------
def test_pdf_form_fill_and_extract(tmp_path, sample, form_pdf):
    tpl_path = tmp_path / "formtpl.yaml"
    tpl_path.write_text(yaml.safe_dump({
        "extends": "default",
        "render": {"pdf_form": {"file": str(form_pdf), "fields": {
            "InvoiceNo": "invoice_number",
            "Customer": "buyer.name",
            "InvoiceDate": "{{ issue_date | date('%m/%d/%Y') }}",
            "TotalDue": "{{ total | money }}",
        }}},
        "extract": {"form_fields": {
            "invoice_number": "InvoiceNo", "buyer.name": "Customer",
            "issue_date": "InvoiceDate", "total": "TotalDue",
        }},
    }))
    tpl = load_template(str(tpl_path))
    out = tmp_path / "filled.pdf"
    fill(sample, tpl, out, fmt="pdf-form")
    doc = read_document(out)
    assert doc.form_fields["TotalDue"] == "$6,916.88"
    d = extract(doc, tpl)["data"]
    assert d["invoice_number"] == "INV-2026-0042"
    assert d["buyer"]["name"] == "Contoso Ltd."
    assert d["issue_date"] == "2026-11-08"
    assert d["total"] == 6916.88


# -- other renderers -----------------------------------------------------------------
def test_html_render(tmp_path, sample):
    sample["buyer"]["name"] = "<script>x</script>"
    out = tmp_path / "inv.html"
    fill(sample, load_template("default"), out)
    html = out.read_text()
    assert "$6,916.88" in html and "November 08, 2026" in html
    assert "<script>x</script>" not in html  # autoescaped


def test_docx_custom_layout_repeats_item_rows(tmp_path, sample):
    from invoice_mgmt.docx_starter import build_starter_docx
    layout = tmp_path / "layout.docx"
    build_starter_docx(layout)
    out = tmp_path / "out.docx"
    fill(sample, load_template("default"), out, layout=str(layout))
    import docx
    tables = docx.Document(str(out)).tables
    items_table = tables[1]
    assert len(items_table.rows) == 1 + len(sample["items"])
    assert items_table.rows[2].cells[3].text == "$1,875.00"
    assert "{{" not in read_document(out).text


# -- CLI --------------------------------------------------------------------------
def test_cli_end_to_end(tmp_path, capsys, examples):
    data = tmp_path / "data.json"
    assert main(["sample", "-o", str(data)]) == 0
    pdf = tmp_path / "inv.pdf"
    assert main(["fill", str(data), "-o", str(pdf), "--set", "buyer.name=Fabrikam", "--set", "tax_rate=0"]) == 0
    capsys.readouterr()
    assert main(["extract", str(pdf), "--data-only"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["buyer"]["name"] == "Fabrikam"
    assert out["total"] == 6375.0

    csv_out = tmp_path / "all.csv"
    assert main(["extract", str(pdf), str(examples / "acme-bill.txt"), "-f", "csv", "-o", str(csv_out)]) == 0
    lines = csv_out.read_text().splitlines()
    assert len(lines) == 3 and "invoice_number" in lines[0]


def test_cli_new_template(tmp_path, capsys):
    assert main(["templates", "new", "my-vendor", "--dir", str(tmp_path)]) == 0
    tpl = load_template(str(tmp_path / "my-vendor.yaml"))
    assert tpl.name == "my-vendor"
    assert tpl.render["currency_symbol"] == "$"


def test_cli_errors_are_friendly(capsys):
    assert main(["extract", "nope.pdf"]) == 2
    assert "File not found" in capsys.readouterr().err
