# invoice-mgmt

A template-driven invoice tool that works in both directions:

- **Extract** – read invoices (PDF, DOCX, TXT, fillable PDF forms) and get structured data out (JSON / YAML / CSV). A **local LLM checks every field**.
- **Fill** – take invoice data (JSON / YAML) and produce an invoice: a styled **PDF**, **HTML**, **DOCX** (from your own Word layout), plain **text**, or a filled-in **PDF form**.

Both directions are driven by one YAML **template**. To support a new vendor layout or a new invoice style, you write a small YAML file that `extends` an existing template and overrides only the parts that differ. You don't change any code.

## How extraction works (local LLM as a field-by-field checker)

Invoices from different senders all look different, so `invoice extract` runs in **verify** mode by default:

1. The template's rules (regex, labels, tables) make a quick first pass at every field.
2. A **local LLM** (Ollama, LM Studio, or llama.cpp, running on your own machine) gets the document and those candidate values. For **every field** it confirms or corrects the value, and it quotes the exact text in the document that supports it.
3. The tool checks that the quoted text really appears in the document. If it doesn't, the LLM's value is not accepted silently. This guards against the model making values up.
4. Each field gets a status:

| Status | Meaning |
|---|---|
| `confirmed` | the rules and the LLM agree |
| `corrected` | the LLM disagreed and its evidence was found in the document → the LLM's value is used |
| `added` | the rules found nothing; the LLM found the value |
| `unconfirmed` | the rules found a value that the LLM couldn't confirm → kept, with a warning |
| `missing` | neither found a value |

```
== bill.pdf: field verification (rules+llm-verify:ollama/qwen2.5:7b)
  [OK ] confirmed   invoice_number         'AS-55120'
  [FIX] corrected   total                  357.36   (rules had 333.98)
  [NEW] added       buyer.email            'ap@northwind.example'
  [OK ] confirmed   items                  3 line(s) (rules 3, llm 3)
```

The full report is also in the JSON output (under `"fields"`) and in the CSV output (as `<field>__status` columns). Other engines are available with `--engine llm` (the model alone) or `--engine rules` (no model; fast and deterministic). If no local LLM is running, verify mode falls back to rules only and prints a notice saying the fields were **not** verified.

### Set up the local LLM (macOS)

```bash
brew install ollama
brew services start ollama
ollama pull qwen2.5:7b        # text model
ollama pull gemma3:12b        # vision model (for scans / photos)
invoice llm-check             # checks the server and both models
```

`qwen2.5:7b` (about 5 GB) is the default and runs well on Apple Silicon. You can use any other model with `--model llama3.1:8b`, the `INVOICE_LLM_MODEL` environment variable, or `llm.model` in a template. For LM Studio or llama.cpp, use `--provider openai --llm-url http://localhost:1234/v1`. Templates can give the model vendor-specific hints with `llm.instructions`, and can describe fields with a `description:` on each field.

### Scans and photos (vision mode)

Scanned PDFs, image-only PDFs, and photos (`.jpg`, `.png`, `.webp`, `.tif`, multi-page TIFF) are read by a **vision model** looking at the page images. You don't need to run OCR first.

```bash
invoice extract scan.pdf                    # no text layer → vision model (gemma3:12b) reads the pages
invoice extract receipt-photo.jpg
invoice extract weird-layout.pdf --vision always   # send page images even for normal PDFs
invoice extract scan.pdf --vision-model qwen2.5vl:7b
```

| `llm.vision` / `--vision` | Behaviour |
|---|---|
| `auto` (default) | page images are used only when there is no text layer; normal PDFs use the faster text model |
| `always` | page images **plus** the text layer go to the vision model. This helps with multi-column layouts where the text comes out in a jumbled order |
| `off` | text only |

On a scan, the model's quoted evidence can't be found in any document text. Instead, each value is checked against the quote the model gave (`evidence_check: quote (image)`), and the math checks still run (line items add up to the subtotal; subtotal + tax = total). The output includes a notice telling you to review the fields marked `added`.

Settings: `vision_model` (default `gemma3:12b`), `max_pages` (default 4), and `image_dpi` (default 150) under `llm:` in a template, or the `INVOICE_VISION` / `INVOICE_VISION_MODEL` environment variables. For iPhone HEIC photos, export them as JPG first.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate      # run this in each new terminal before using `invoice`
pip install -e .
```

Alternatively, `pipx install -e .` puts `invoice` on your PATH without activating anything.
Homebrew and other system Pythons refuse a plain `pip install` (the "externally-managed-environment" error), so use one of these two options.

Requires Python 3.9+. Dependencies: PyYAML, Jinja2, pdfplumber, pypdf, reportlab, python-docx.

## Quick start

```bash
invoice sample -o data.json                 # example invoice data
invoice fill data.json -o invoice.pdf       # generate a PDF (format comes from the extension)
invoice fill data.json -o invoice.html
invoice fill data.json -o invoice.docx
invoice fill data.json -t eu-vat -o eu.pdf --set seller.tax_id=DE123456789

invoice extract invoice.pdf                 # -> JSON (template auto-detected)
invoice extract *.pdf -f csv -o invoices.csv            # one row per invoice
invoice extract *.pdf -f csv --items -o line-items.csv  # one row per line item
```

Useful flags:

| Command | Flag | Meaning |
|---|---|---|
| `fill` | `--set key=value` | override or add values, e.g. `--set buyer.name="Foo Inc" --set tax_rate=0` |
| `fill` | `-l / --layout FILE` | use a different `.j2`, `.docx`, or form `.pdf` for this run |
| `fill` | `--no-compute` | don't calculate line amounts, subtotal, tax, or total |
| `extract` | `-t NAME` | force a template (default `auto` picks the best `match:`) |
| `extract` | `--data-only` | print just the data, without the source/template/warnings wrapper |
| both | `--strict` | exit with code 1 if there are warnings (missing required fields, totals that don't add up) |
| global | `-T DIR` | extra template directory (also `$INVOICE_TEMPLATES`) |

## Invoice data

```yaml
invoice_number: INV-2026-0042
issue_date: 2026-11-08          # ISO dates in data; output format is set by the template
due_date: 2026-12-08
seller: {name: Northwind Studio LLC, address: "500 Market St\nSan Francisco, CA", email: billing@northwind.example}
buyer:  {name: Contoso Ltd., address: "1 Harbor Way\nSeattle, WA 98101"}
items:
  - {description: Website redesign, quantity: 1, unit_price: 4200}
  - {description: Consulting hours, quantity: 12.5, unit_price: 150}
tax_rate: 8.5                   # amount, subtotal, tax, and total are calculated if you leave them out
payment_terms: Net 30
notes: Thank you!
```

A full example is in [`examples/sample-invoice.yaml`](examples/sample-invoice.yaml). Any extra keys you add (for example `project_code`) can be used in HTML, DOCX, and PDF-form layouts.

## Templates

Template search order: `-T DIR` → `$INVOICE_TEMPLATES` → `./templates/` → built-in templates (`invoice_mgmt/templates/`).

```bash
invoice templates list
invoice templates show default         # the full resolved YAML: every option you can override
invoice templates new my-vendor        # creates ./templates/my-vendor.yaml (extends default)
invoice templates new my-eu --from eu-vat
```

Built-in templates:

| Template | Purpose |
|---|---|
| `default` | Generic US-style invoice. Broad extraction patterns, Letter-size PDF. The base for everything else. |
| `eu-vat` | A4, `1.234,56 €` formatting, day-first dates, VAT labels. Only ~50 lines of overrides. |
| `acme-supplies` | Example vendor *extraction* template: custom invoice-number label, `SKU … Qty x Price = Amount` rows, and an extra `sku` column. |

### Template anatomy

```yaml
extends: default            # inherit everything; dicts deep-merge, lists replace
name: my-vendor
match: ['My Vendor Inc', 'Ref No\.']      # used by --template auto

fields:                     # data model: types, defaults, required
  seller.name: {type: string, extract_default: My Vendor Inc}

extract:
  date_formats: ['%d/%m/%Y']              # day-first dates
  fields:                   # each rule REPLACES the parent's rule for that field
    invoice_number: {label: 'Ref No.'}                       # value after the label
    total: {patterns: ['^Amount payable[^\n]*?(?P<value>[\d,]+\.\d{2})']}
    buyer.address:                                           # multi-line block
      block: {start: '^Deliver to', until: '^Item', max_lines: 4}
  form_fields: {invoice_number: InvNo}    # read values from fillable PDF fields
  line_items:
    start: '^Item'          # header line (parsing starts below it)
    end: '^Subtotal'
    row: ['^(?P<description>.+?)\s+(?P<quantity>\d+)\s+(?P<unit_price>[\d,.]+)\s+(?P<amount>[\d,.]+)$']
    columns: {description: ['item'], quantity: ['qty'], amount: ['amount']}  # for real tables

render:
  currency_symbol: "£"
  date_format: "%d %b %Y"
  labels: {title: TAX INVOICE, tax: VAT}  # every visible label can be renamed or translated
  pdf:
    page_size: a4
    accent_color: "#333333"
    logo: logo.png          # paths are relative to the template file
    footer: "{{ seller.name }} · Company no. 1234567"
    columns:                # choose and order the line-item columns
      - {key: description, label: Description, width: 0.6}
      - {key: amount, label: Amount, width: 0.4, align: right, format: money}
  html:     {template: my-layout.html.j2}
  docx:     {template: my-layout.docx}
  pdf_form: {file: supplier-form.pdf, fields: {InvNo: invoice_number, Total: "{{ total | money }}"}}
```

Extraction rules for a field (the first one that produces a value wins):

- `patterns` – a list of regexes. The tool uses the named group `value`, or else group 1. Regexes are case-insensitive and multiline (change this with `flags: "ims"`).
- `label` – the rest of the line after this text.
- `block` – lines after a `start` heading, until a blank line, `until`, or `max_lines`. Optionally `select: "0"` (first line) or `"1:"` (all but the first), and `exclude` regexes to drop lines.
- `value` – a constant.

### Adapting to a new invoice layout

1. `invoice inspect their-invoice.pdf -n` shows the raw text, the tables, the form fields, and how well each template matches.
2. `invoice templates new their-vendor`, then add `match:` patterns and override only the fields that came out wrong.
3. Run `invoice extract their-invoice.pdf -t templates/their-vendor.yaml` until it prints no warnings. The tool checks that line items add up to the subtotal and that subtotal + tax = total, so the warnings show you which fields are still wrong.

### Producing a new invoice style

- **Quick restyle:** change `render.pdf` (colors, logo, columns, page size, footer) and `render.labels`.
- **Fully custom HTML:** copy `invoice_mgmt/templates/invoice.html.j2` next to your template, edit it, and set `render.html.template`.
- **Word:** `invoice docx-starter my-layout.docx` writes an editable Word file full of `{{ placeholders }}`. Style it in Word and set `render.docx.template`. A table row that contains `{{ item.… }}` is repeated once for each line item. Formatting is kept per paragraph: each placeholder takes the style of the first run in its paragraph.
- **Someone else's fillable PDF:** `invoice form-fields their-form.pdf` lists the field names. Map them under `render.pdf_form.fields`, then run `invoice fill data.json -f pdf-form -o out.pdf`.

Jinja filters available in every layout: `money`, `date` (with an optional strftime pattern), and `num`.

## Python API

```python
from invoice_mgmt import extract, fill, load_template

result = extract("bill.pdf")                       # auto-detects the template, LLM-verified
print(result["data"]["total"], result["warnings"])
print(result["fields"]["total"])                   # {'status': 'confirmed', 'evidence': ..., ...}
extract("bill.pdf", engine="rules")                # no model

fill(result["data"], load_template("eu-vat"), "copy.pdf")
```

## Limitations

- Vision mode reads at most `max_pages` pages per invoice (default 4). Increase it for long invoices. Vision models are slower: expect tens of seconds per page with `gemma3:12b`.
- Numeric dates such as `03/04/2026` are read month-first unless the template sets `extract.date_formats`.

## Development

```bash
pip install -e '.[dev]'
pytest
```
