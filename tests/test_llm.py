import json

import pytest

from invoice_mgmt import extract, load_template
from invoice_mgmt import llm
from invoice_mgmt.cli import main
from invoice_mgmt.readers import Document

TEXT = """Bright Lights Electrical
Ref: BL/2026/88
Raised 3 March 2026
Customer: Harbor Cafe
Rewire kitchen 1 900.00 900.00
Callout fee 1 75.00 75.00
Total payable 975.00
"""


def fake_model(answer):
    calls = []

    def _call(system, prompt, schema, cfg):
        calls.append({"system": system, "prompt": prompt, "schema": schema, "cfg": cfg})
        return json.dumps(answer)
    return _call, calls


def field(value, evidence=None):
    return {"value": value, "evidence": evidence}


def test_verify_schema_covers_every_field():
    tpl = load_template("default")
    schema = llm.build_verify_schema(tpl.fields)
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["seller.name"]["properties"]["value"]["type"] == ["string", "null"]
    assert schema["properties"]["total"]["properties"]["value"]["type"] == ["number", "null"]
    assert schema["properties"]["items"]["type"] == "array"


def test_verify_statuses(monkeypatch):
    answer = {
        "seller.name": field("Bright Lights Electrical", "Bright Lights Electrical"),  # confirmed
        "invoice_number": field("BL/2026/88", "Ref: BL/2026/88"),                     # added
        "issue_date": field("2026-03-03", "Raised 3 March 2026"),                      # added
        "buyer.name": field("Harbor Cafe", "Customer: Harbor Cafe"),                    # added
        "total": field(975.0, "Total payable 975.00"),                                 # confirmed
        "po_number": field("PO-999", "PO-999"),                                        # hallucinated
        "items": [
            {"description": "Rewire kitchen", "quantity": 1, "unit_price": 900, "amount": 900},
            {"description": "Callout fee", "quantity": 1, "unit_price": 75, "amount": 75},
        ],
    }
    call, calls = fake_model(answer)
    monkeypatch.setattr(llm, "call_model", call)
    res = extract(Document(None, TEXT), load_template("default"), engine="verify")

    f = res["fields"]
    assert f["seller.name"]["status"] == "confirmed"
    assert f["total"]["status"] == "confirmed"
    assert f["invoice_number"]["status"] == "added" and res["data"]["invoice_number"] == "BL/2026/88"
    assert f["issue_date"]["value"] == "2026-03-03"
    assert f["po_number"]["evidence_ok"] is False
    assert any("po_number" in w and "evidence" in w for w in res["warnings"])
    assert f["items"]["status"] == "added" and len(res["data"]["items"]) == 2
    assert res["data"]["subtotal"] == 975.0
    # the model saw the rules' candidates
    assert "CANDIDATE VALUES" in calls[0]["prompt"] and "Bright Lights Electrical" in calls[0]["prompt"]


def test_verify_corrects_only_with_evidence(monkeypatch):
    text = "Acme\nInvoice # INV-1\nSubtotal 100.00\nTotal 110.00\nLate fee applied, pay this amount: 120.00\n"
    # rules read 110.00; the model says 120 and quotes it -> corrected
    call, _ = fake_model({"total": field(120.0, "pay this amount: 120.00"),
                          "invoice_number": field("INV-2", "Invoice # INV-2"),  # no such text
                          "items": []})
    monkeypatch.setattr(llm, "call_model", call)
    res = extract(Document(None, text), load_template("default"), engine="verify")
    assert res["fields"]["total"]["status"] == "corrected"
    assert res["data"]["total"] == 120.0
    # correction without real evidence is not applied
    assert res["fields"]["invoice_number"]["status"] == "corrected"
    assert res["data"]["invoice_number"] == "INV-1"
    assert any("kept rules value" in w for w in res["warnings"])
    assert res["fields"]["subtotal"]["status"] == "unconfirmed"


def test_verify_falls_back_to_rules_when_no_llm(monkeypatch):
    def down(*a, **k):
        raise llm.LLMError("connection refused")
    monkeypatch.setattr(llm, "call_model", down)
    res = extract(Document(None, "Invoice # A-1\nTotal 5.00"), load_template("default"), engine="verify")
    assert res["engine"] == "rules" and "NOT verified" in res["notice"]
    assert res["data"]["invoice_number"] == "A-1"


def test_llm_only_engine(monkeypatch):
    call, _ = fake_model({"invoice_number": "X1", "total": "1,234.50", "seller": {"name": "S"},
                          "items": [{"description": "Thing", "quantity": 2, "unit_price": 617.25,
                                     "amount": None}]})
    monkeypatch.setattr(llm, "call_model", call)
    res = extract(Document(None, "whatever"), load_template("default"), engine="llm")
    assert res["data"]["total"] == 1234.5
    assert res["data"]["items"][0]["amount"] == 1234.5
    assert res["engine"].startswith("llm:ollama/")


def test_config_precedence(monkeypatch):
    tpl = load_template("default")
    assert llm.llm_config(tpl)["base_url"] == "http://localhost:11434"
    monkeypatch.setenv("INVOICE_LLM_MODEL", "llama3.2")
    assert llm.llm_config(tpl)["model"] == "llama3.2"
    assert llm.llm_config(tpl, model="mistral")["model"] == "mistral"
    assert llm.llm_config(tpl, provider="openai")["base_url"] == "http://localhost:1234/v1"


def test_ollama_and_openai_payloads(monkeypatch):
    sent = []

    def post(url, payload, timeout):
        sent.append((url, payload))
        if url.endswith("/api/chat"):
            return {"message": {"content": "{}"}}
        return {"choices": [{"message": {"content": "{}"}}]}
    monkeypatch.setattr(llm, "_post_json", post)
    schema = {"type": "object"}
    llm.call_model("s", "p", schema, llm.llm_config(None))
    llm.call_model("s", "p", schema, llm.llm_config(None, provider="openai"))
    assert sent[0][0] == "http://localhost:11434/api/chat" and sent[0][1]["format"] == schema
    assert sent[1][0] == "http://localhost:1234/v1/chat/completions"
    assert sent[1][1]["response_format"]["json_schema"]["schema"] == schema


def test_cli_prints_report(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("INVOICE_ENGINE", "verify")
    call, _ = fake_model({"total": field(975.0, "Total payable 975.00"), "items": []})
    monkeypatch.setattr(llm, "call_model", call)
    f = tmp_path / "inv.txt"
    f.write_text(TEXT)
    assert main(["extract", str(f), "-t", "default"]) == 0
    err = capsys.readouterr().err
    assert "field verification" in err and "confirmed" in err
