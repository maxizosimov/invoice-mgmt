import json

import pytest

from invoice_mgmt import extract, fill, load_template
from invoice_mgmt import llm
from invoice_mgmt.extract import value_in_evidence
from invoice_mgmt.readers import page_images, read_document


@pytest.fixture
def scanned(tmp_path, sample):
    """An image-only PDF (like a scanner produces) and a phone-photo style PNG."""
    import pypdfium2 as pdfium

    pdf = tmp_path / "real.pdf"
    fill(sample, load_template("default"), pdf)
    img = pdfium.PdfDocument(str(pdf))[0].render(scale=1.5).to_pil().convert("RGB")
    scan = tmp_path / "scan.pdf"
    img.save(scan, "PDF")
    photo = tmp_path / "photo.jpg"
    img.save(photo, "JPEG")
    return {"text_pdf": pdf, "scan": scan, "photo": photo}


ANSWER = {
    "invoice_number": {"value": "INV-2026-0042", "evidence": "Invoice # INV-2026-0042"},
    "issue_date": {"value": "2026-11-08", "evidence": "Invoice Date November 08, 2026"},
    "seller.name": {"value": "Northwind Studio LLC", "evidence": "Northwind Studio LLC"},
    "buyer.name": {"value": "Contoso Ltd.", "evidence": "Bill To Contoso Ltd."},
    "total": {"value": 6916.88, "evidence": "Total $6,916.88"},
    "tax": {"value": 541.88, "evidence": "Tax (8.5%) $541.88"},
    "subtotal": {"value": 6375.0, "evidence": "Subtotal $6,375.00"},
    "po_number": {"value": "PO-1234", "evidence": "PO # PO-7781"},  # quote contradicts value
    "items": [
        {"description": "Website redesign", "quantity": 1, "unit_price": 4200, "amount": 4200},
        {"description": "Consulting hours", "quantity": 12.5, "unit_price": 150, "amount": 1875},
        {"description": "Hosting (12 months)", "quantity": 12, "unit_price": 25, "amount": 300},
    ],
}


@pytest.fixture
def fake(monkeypatch):
    calls = []

    def _call(system, prompt, schema, cfg, images=None, model=None):
        calls.append({"prompt": prompt, "images": images or [], "model": model})
        return json.dumps(ANSWER)
    monkeypatch.setattr(llm, "call_model", _call)
    return calls


def test_scan_has_no_text_but_renders(scanned):
    doc = read_document(scanned["scan"])
    assert doc.kind == "pdf" and not doc.has_text and doc.can_render
    imgs = page_images(doc)
    assert len(imgs) == 1 and imgs[0][:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("key", ["scan", "photo"])
def test_verify_reads_scans_with_vision_model(scanned, fake, key):
    res = extract(scanned[key], load_template("default"), engine="verify")
    call = fake[0]
    assert call["model"] == "gemma3:12b" and len(call["images"]) == 1
    assert "page image" in call["prompt"] and "INVOICE TEXT" not in call["prompt"]
    assert "vision" in res["engine"] and "No text layer" in res["notice"]
    f = res["fields"]
    assert f["invoice_number"]["status"] == "added" and f["invoice_number"]["evidence_ok"] is True
    assert f["issue_date"]["evidence_ok"] is True
    assert f["total"]["evidence_check"] == "quote (image)" and f["total"]["evidence_ok"] is True
    assert f["po_number"]["evidence_ok"] is False
    assert any("po_number" in w for w in res["warnings"])
    assert res["data"]["total"] == 6916.88 and len(res["data"]["items"]) == 3
    assert not any("!=" in w for w in res["warnings"])  # totals math checks out


def test_text_pdf_uses_text_model_by_default(scanned, fake):
    extract(scanned["text_pdf"], load_template("default"), engine="verify")
    assert fake[0]["model"] == "qwen2.5:7b" and fake[0]["images"] == []


def test_vision_always_sends_images_and_text(scanned, fake):
    res = extract(scanned["text_pdf"], load_template("default"), engine="verify",
                  llm_options={"vision": "always", "vision_model": "qwen2.5vl:7b"})
    assert fake[0]["model"] == "qwen2.5vl:7b" and fake[0]["images"]
    assert "INVOICE TEXT" in fake[0]["prompt"]
    assert res["fields"]["total"]["evidence_check"] == "document text"


def test_vision_off_cannot_read_scan(scanned, fake):
    with pytest.raises(llm.LLMError, match="no text layer"):
        extract(scanned["scan"], load_template("default"), engine="verify", llm_options={"vision": "off"})


def test_rules_engine_explains_scan(scanned):
    res = extract(scanned["scan"], load_template("default"), engine="rules")
    assert "vision" in res["notice"]


def test_image_payloads(monkeypatch):
    sent = []
    monkeypatch.setattr(llm, "_post_json", lambda url, payload, t: sent.append(payload) or
                        {"message": {"content": "{}"}, "choices": [{"message": {"content": "{}"}}]})
    llm.call_model("s", "p", {}, llm.llm_config(None), images=[b"png"], model="gemma3:12b")
    llm.call_model("s", "p", {}, llm.llm_config(None, provider="openai"), images=[b"png"])
    assert sent[0]["model"] == "gemma3:12b" and sent[0]["messages"][1]["images"] == ["cG5n"]
    parts = sent[1]["messages"][1]["content"]
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,cG5n"


@pytest.mark.parametrize("value,quote,ok", [
    (6916.88, "Total $6,916.88", True),
    (6916.88, "Gesamt 6.916,88 €", True),
    (6916.88, "Total $6,961.88", False),
    ("2026-11-08", "Invoice Date November 08, 2026", True),
    ("2026-11-08", "Date 08/11/2026", True),
    ("2026-11-08", "Date 2026-12-01", False),
    ("Contoso Ltd.", "Bill To: CONTOSO LTD.", True),
    ("Contoso Ltd.", "Fabrikam", False),
])
def test_value_in_evidence(value, quote, ok):
    assert value_in_evidence(value, quote) is ok
