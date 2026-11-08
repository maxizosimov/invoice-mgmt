"""Read raw text, tables and page images out of invoice files (PDF, DOCX, text, images)."""

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
    kind: str = "text"  # pdf | docx | text | image

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())

    @property
    def can_render(self) -> bool:
        """True if page images can be produced for a vision model."""
        return self.path is not None and self.kind in ("pdf", "image")


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
    # A scanned PDF has no text layer: we keep going, a vision model can read the pages.
    return Document(path=path, text=text, tables=tables, form_fields=form_fields, kind="pdf")


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
    return Document(path=path, text="\n".join(lines), tables=tables, kind="docx")


def read_text(path: Path) -> Document:
    return Document(path=path, text=path.read_text(encoding="utf-8", errors="replace"), kind="text")


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif")


def read_image(path: Path) -> Document:
    """Photos / scans: there is no text, only a vision model (or OCR) can read them."""
    from PIL import Image

    try:
        with Image.open(path) as im:
            im.verify()
    except Exception as exc:
        raise ReaderError(f"Cannot open image {path}: {exc}") from exc
    return Document(path=path, text="", kind="image")


def page_images(doc: Document, max_pages: int = 4, dpi: int = 150, max_side: int = 1800) -> List[bytes]:
    """Render the document's pages as PNG bytes for a vision model."""
    import io

    from PIL import Image, ImageOps

    if not doc.can_render:
        return []
    pil_pages = []
    if doc.kind == "pdf":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(doc.path))
        try:
            for i in range(min(len(pdf), max_pages)):
                pil_pages.append(pdf[i].render(scale=dpi / 72).to_pil())
        finally:
            pdf.close()
    else:
        with Image.open(doc.path) as im:
            frames = getattr(im, "n_frames", 1)
            for i in range(min(frames, max_pages)):
                im.seek(i)
                pil_pages.append(ImageOps.exif_transpose(im.copy()))
    out = []
    for img in pil_pages:
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        out.append(buf.getvalue())
    return out


READERS = {
    ".pdf": read_pdf,
    ".docx": read_docx,
    ".txt": read_text,
    ".text": read_text,
    ".md": read_text,
    ".eml": read_text,
    **{ext: read_image for ext in IMAGE_EXTS},
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
