import copy
from pathlib import Path

import pytest

from invoice_mgmt.cli import SAMPLE_DATA

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture(autouse=True)
def _rules_engine_by_default(monkeypatch):
    """Tests are deterministic: no local LLM unless a test mocks one in."""
    monkeypatch.setenv("INVOICE_ENGINE", "rules")
    for var in ("INVOICE_LLM_MODEL", "INVOICE_LLM_URL", "INVOICE_LLM_PROVIDER"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def sample():
    return copy.deepcopy(SAMPLE_DATA)


@pytest.fixture
def examples():
    return EXAMPLES


@pytest.fixture
def form_pdf(tmp_path):
    """A small fillable PDF with AcroForm text fields."""
    from reportlab.pdfgen import canvas

    path = tmp_path / "form.pdf"
    c = canvas.Canvas(str(path))
    c.drawString(50, 800, "Supplier Invoice Form")
    for i, name in enumerate(["InvoiceNo", "Customer", "InvoiceDate", "TotalDue"]):
        y = 750 - i * 40
        c.drawString(50, y + 5, name)
        c.acroForm.textfield(name=name, x=150, y=y, width=250, height=20)
    c.save()
    return path
