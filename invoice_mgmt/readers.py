"""Read raw text and tables out of invoice files (PDF, DOCX, plain text)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

Table = List[List[str]]


@dataclass
class Document:
    path: Optional[Path]
    text: str
    tables: List[Table] = field(default_factory=list)
    form_fields: Dict[str, str] = field(default_factory=dict)


class ReaderError(Exception):
    pass


def _clean_cell(cell) -> str:
    return " ".join(str(cell or "").split())


def read_pdf(path: Path) -> Document:
    import pdfplumber

    texts, tables = [], []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            texts.append(page.extract_text() or "")
            for tbl in page.extract_tables() or []:
                tables.append([[_clean_cell(c) for c in row] for row in tbl])
    text = "\n".join(texts)
    form_fields = read_pdf_form_fields(path)
    if not text.strip() and not form_fields:
        raise ReaderError(
            f"No text layer found in {path}. It is probably a scanned image; "
            "run it through OCR first (e.g. `ocrmypdf in.pdf out.pdf`)."
        )
    return Document(path=path, text=text, tables=tables, form_fields=form_fields)


def read_pdf_form_fields(path: Path) -> Dict[str, str]:
    """Return ``{field_name: value}`` for fillable (AcroForm) PDFs, else ``{}``."""
    from pypdf import PdfReader

    try:
        fields = PdfReader(str(path)).get_fields() or {}
    except Exception:  # malformed forms shouldn't break text extraction
        return {}
    out = {}
    for name, f in fields.items():
        value = f.get("/V") if hasattr(f, "get") else None
        out[name] = "" if value is None else str(value)
    return out


def read_docx(path: Path) -> Document:
    import docx
    from docx.table import Table as DocxTable
    from docx.text.paragraph import Paragraph

    d = docx.Document(str(path))
    lines: List[str] = []
    tables: List[Table] = []
    # Walk body in document order so text and tables stay interleaved.
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            lines.append(Paragraph(child, d).text)
        elif tag == "tbl":
            tbl = DocxTable(child, d)
            rows = [[_clean_cell(c.text) for c in row.cells] for row in tbl.rows]
            tables.append(rows)
            lines.extend(" ".join(c for c in row if c) for row in rows)
    return Document(path=path, text="\n".join(lines), tables=tables)


def read_text(path: Path) -> Document:
    return Document(path=path, text=path.read_text(encoding="utf-8", errors="replace"))


READERS = {
    ".pdf": read_pdf,
    ".docx": read_docx,
    ".txt": read_text,
    ".text": read_text,
    ".md": read_text,
    ".eml": read_text,
}


def read_document(path) -> Document:
    path = Path(path)
    if not path.is_file():
        raise ReaderError(f"File not found: {path}")
    reader = READERS.get(path.suffix.lower())
    if reader is None:
        raise ReaderError(
            f"Unsupported file type '{path.suffix}'. Supported: {', '.join(sorted(READERS))}"
        )
    return reader(path)
