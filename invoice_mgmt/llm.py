"""Local-LLM extraction for invoices that don't follow a fixed layout.

Talks to a model running on your own machine - nothing leaves it:

* ``ollama`` (default)  - https://ollama.com, ``ollama pull qwen2.5:7b``
* ``openai``            - any OpenAI-compatible local server (LM Studio, llama.cpp
                          ``llama-server``, vLLM, ...)

The template's ``fields:`` become a JSON schema the model must fill, so the same
template still decides *what* gets extracted. Per-template ``llm.instructions`` let
you add hints for a tricky vendor without writing regexes.

Settings (template ``llm:`` section, overridden by env vars, overridden by CLI flags):

    llm:
      provider: ollama               # INVOICE_LLM_PROVIDER
      model: qwen2.5:7b              # INVOICE_LLM_MODEL
      base_url: http://localhost:11434   # INVOICE_LLM_URL
      timeout: 300
      max_chars: 24000               # longer documents are truncated
      instructions: ""               # extra hints appended to the prompt
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .model import coerce, compute_totals, set_path, validate
from .readers import Document
from .template import Template

DEFAULTS = {
    "provider": "ollama",
    "model": "qwen2.5:7b",
    "base_url": None,  # provider-specific default below
    "timeout": 300,
    "max_chars": 24000,
    "temperature": 0,
    "instructions": "",
    # Vision: read page images (scans, photos, or layouts where text order is garbled).
    #   auto   - use images only when the document has no text layer (scans / photos)
    #   always - send page images together with the text for every PDF/image
    #   off    - never (documents without text then cannot be read)
    "vision": "auto",
    "vision_model": "gemma3:12b",
    "max_pages": 4,
    "image_dpi": 150,
}
VISION_MODES = ("auto", "always", "off")
DEFAULT_URLS = {"ollama": "http://localhost:11434", "openai": "http://localhost:1234/v1"}
ENV = {"provider": "INVOICE_LLM_PROVIDER", "model": "INVOICE_LLM_MODEL", "base_url": "INVOICE_LLM_URL",
       "vision": "INVOICE_VISION", "vision_model": "INVOICE_VISION_MODEL"}

SYSTEM_PROMPT = """You extract structured data from invoices.
Rules:
- Copy values exactly as they appear in the document; never invent data.
- Use null for anything not present in the document.
- Dates: output YYYY-MM-DD.
- Money and quantities: plain numbers without currency symbols or thousands separators (1234.5).
- tax_rate is a percentage number (8.5 means 8.5%).
- seller is the party issuing the invoice / being paid; buyer is the party billed.
- items: one entry per invoice line (products, services, fees). Do not include subtotal,
  tax or total rows as items.
- Addresses: keep line breaks as \\n, exclude names, emails and phone numbers."""


class LLMError(Exception):
    """The local model could not be reached or returned unusable output."""


# -- configuration -------------------------------------------------------------------
def llm_config(template: Optional[Template] = None, **overrides) -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    if template is not None:
        cfg.update({k: v for k, v in (template.data.get("llm") or {}).items() if v is not None})
    for key, var in ENV.items():
        if os.environ.get(var):
            cfg[key] = os.environ[var]
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    if cfg["provider"] not in DEFAULT_URLS:
        raise LLMError(f"Unknown LLM provider '{cfg['provider']}'. Use: {', '.join(DEFAULT_URLS)}")
    if cfg["vision"] not in VISION_MODES:
        raise LLMError(f"llm.vision must be one of {', '.join(VISION_MODES)}, got '{cfg['vision']}'")
    cfg["base_url"] = (cfg.get("base_url") or DEFAULT_URLS[cfg["provider"]]).rstrip("/")
    return cfg


def wants_vision(doc: Document, cfg: Dict[str, Any]) -> bool:
    if cfg["vision"] == "off" or not doc.can_render:
        return False
    return cfg["vision"] == "always" or not doc.has_text


def prepare_inputs(doc: Document, cfg: Dict[str, Any]):
    """Return ``(images, model)``: page images (possibly empty) and the model to use."""
    from .readers import page_images

    if wants_vision(doc, cfg):
        images = page_images(doc, int(cfg["max_pages"]), int(cfg["image_dpi"]))
        if images:
            return images, cfg["vision_model"]
    if not doc.has_text and not doc.form_fields:
        hint = ("set vision to auto/always" if doc.can_render else "this file type has no pages to render")
        raise LLMError(f"{doc.path} has no text layer and vision is off ({hint}).")
    return [], cfg["model"]


# -- schema from template fields -------------------------------------------------------
JSON_TYPES = {"number": "number", "money": "number", "float": "number", "decimal": "number",
              "integer": "integer"}


def _json_type(spec: Dict[str, Any]) -> Dict[str, Any]:
    t = JSON_TYPES.get((spec or {}).get("type", "string"), "string")
    out: Dict[str, Any] = {"type": [t, "null"]}
    desc = (spec or {}).get("description") or (spec or {}).get("label")
    if desc:
        out["description"] = str(desc)
    if (spec or {}).get("type") == "date":
        out["description"] = (out.get("description", "") + " (YYYY-MM-DD)").strip()
    return out


def build_schema(fields: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Turn the template's dotted ``fields`` into a nested JSON schema."""
    root: Dict[str, Any] = {"type": "object", "properties": {}}
    for path, spec in fields.items():
        spec = spec or {}
        if spec.get("llm") is False:  # let templates hide fields from the model
            continue
        if spec.get("type") == "list":
            item_props = {k: _json_type(v) for k, v in (spec.get("item_fields") or {}).items()}
            node: Dict[str, Any] = {"type": "array", "items": {
                "type": "object", "properties": item_props}}
        else:
            node = _json_type(spec)
        parts = path.split(".")
        cur = root
        for part in parts[:-1]:
            cur = cur["properties"].setdefault(part, {"type": "object", "properties": {}})
            if "properties" not in cur:  # a scalar field was declared with the same prefix
                break
        else:
            cur["properties"][parts[-1]] = node
            continue

    def require_all(obj):
        # Strict structured output (OpenAI-style) needs every key required + no extras;
        # optional values are expressed as nullable types instead.
        if obj.get("type") == "object":
            obj["required"] = list(obj["properties"])
            obj["additionalProperties"] = False
            for child in obj["properties"].values():
                require_all(child)
        elif obj.get("type") == "array":
            require_all(obj["items"])
    require_all(root)
    return root


# -- prompt ---------------------------------------------------------------------------
def build_prompt(doc: Document, template: Template, cfg: Dict[str, Any], n_images: int = 0) -> str:
    parts: List[str] = []
    if n_images:
        parts.append(f"The invoice is attached as {n_images} page image(s). Read every value from the images"
                     + (" and use the extracted text below as a helper (it may be out of order)."
                        if doc.has_text else "."))
    text = doc.text
    if len(text) > cfg["max_chars"]:
        text = text[: cfg["max_chars"]] + "\n[... truncated ...]"
    if text.strip():
        parts.append("INVOICE TEXT:\n<<<\n" + text + "\n>>>")
    if doc.tables:
        rendered = []
        for t_idx, table in enumerate(doc.tables[:10], 1):
            rendered.append(f"Table {t_idx}:\n" + "\n".join(" | ".join(r) for r in table[:80]))
        parts.append("TABLES DETECTED IN THE DOCUMENT:\n" + "\n\n".join(rendered))
    if doc.form_fields:
        parts.append("PDF FORM FIELDS:\n" + "\n".join(f"{k}: {v}" for k, v in doc.form_fields.items() if v))
    if cfg.get("instructions"):
        parts.append("ADDITIONAL INSTRUCTIONS:\n" + str(cfg["instructions"]).strip())
    parts.append("Return only the JSON object.")
    return "\n\n".join(parts)


# -- transport ------------------------------------------------------------------------
def _post_json(url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:500]
        raise LLMError(f"LLM server returned HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, OSError) as exc:
        if isinstance(exc, (TimeoutError,)) or "timed out" in str(exc):
            raise LLMError(f"Local LLM timed out after {timeout}s at {url}. Large/vision models can be "
                           "slow: raise llm.timeout or reduce llm.max_pages.") from exc
        raise LLMError(f"Cannot reach local LLM at {url} ({exc}). Is it running? "
                       "For Ollama: `ollama serve` and `ollama pull <model>`.") from exc


def _get_json(url: str, timeout: float = 5) -> Dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LLMError(f"Cannot reach local LLM at {url} ({exc})") from exc


def call_model(system: str, prompt: str, schema: Dict[str, Any], cfg: Dict[str, Any],
               images: Optional[List[bytes]] = None, model: Optional[str] = None) -> str:
    import base64

    model = model or cfg["model"]
    b64 = [base64.b64encode(img).decode() for img in images or []]
    if cfg["provider"] == "ollama":
        user: Dict[str, Any] = {"role": "user", "content": prompt}
        if b64:
            user["images"] = b64
        messages = [{"role": "system", "content": system}, user]
        res = _post_json(f"{cfg['base_url']}/api/chat", {
            "model": model, "messages": messages, "stream": False,
            "format": schema, "options": {"temperature": cfg["temperature"]},
        }, cfg["timeout"])
        return (res.get("message") or {}).get("content", "")
    content: Any = prompt
    if b64:
        content = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in b64]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    res = _post_json(f"{cfg['base_url']}/chat/completions", {
        "model": model, "messages": messages, "temperature": cfg["temperature"],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "invoice", "schema": schema, "strict": True}},
    }, cfg["timeout"])
    try:
        return res["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Unexpected response from LLM server: {str(res)[:300]}") from exc


def _parse_json(content: str) -> Dict[str, Any]:
    content = (content or "").strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content[content.find("{"):]
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end == -1:
        raise LLMError(f"Model did not return JSON: {content[:300]}")
    try:
        return json.loads(content[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"Model returned invalid JSON: {exc}") from exc


# -- normalisation --------------------------------------------------------------------
def normalise(raw: Dict[str, Any], fields: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Coerce the model's answer through the same type rules as regex extraction; drop nulls."""
    from .model import get_path

    data: Dict[str, Any] = {}
    for path, spec in fields.items():
        spec = spec or {}
        value = get_path(raw, path)
        if spec.get("type") == "list":
            item_fields = spec.get("item_fields") or {}
            items = []
            for item in value or []:
                if not isinstance(item, dict):
                    continue
                clean = {}
                for k, v in item.items():
                    if v in (None, ""):
                        continue
                    ispec = item_fields.get(k) or {}
                    clean[k] = coerce(v, ispec.get("type", "string"), ispec)
                if clean.get("description") or clean.get("amount") is not None:
                    items.append(clean)
            if items:
                data[path] = items
            continue
        if value in (None, "", []):
            continue
        set_path(data, path, coerce(value, spec.get("type", "string"), spec))
    return data


def extract_with_llm(doc: Document, template: Template, compute: bool = True,
                     **overrides) -> Dict[str, Any]:
    cfg = llm_config(template, **overrides)
    schema = build_schema(template.fields)
    images, model = prepare_inputs(doc, cfg)
    content = call_model(SYSTEM_PROMPT, build_prompt(doc, template, cfg, len(images)), schema, cfg,
                         images=images, model=model)
    data = normalise(_parse_json(content), template.fields)
    if compute:
        compute_totals(data)
    return {
        "source": str(doc.path) if doc.path else None,
        "template": template.name,
        "engine": f"llm:{cfg['provider']}/{model}" + (f" (vision, {len(images)} page(s))" if images else ""),
        "data": data,
        "warnings": validate(data, template.fields),
    }


def check(template: Optional[Template] = None, **overrides) -> str:
    """Return a status report, raising LLMError if the server or a needed model is unavailable."""
    cfg = llm_config(template, **overrides)
    needed = [("text model", cfg["model"])]
    if cfg["vision"] != "off":
        needed.append(("vision model", cfg["vision_model"]))
    if cfg["provider"] == "ollama":
        tags = _get_json(f"{cfg['base_url']}/api/tags")
        names = [m.get("name", "") for m in tags.get("models", [])]
        for role, model in needed:
            wanted = model if ":" in model else model + ":latest"
            if wanted not in names and model not in names:
                raise LLMError(f"Ollama is running but the {role} '{model}' is not pulled. "
                               f"Run: ollama pull {model}   (installed: {', '.join(names) or 'none'})")
        if cfg["vision"] != "off":
            try:
                info = _post_json(f"{cfg['base_url']}/api/show", {"model": cfg["vision_model"]}, 10)
                caps = info.get("capabilities")
                if caps is not None and "vision" not in caps:
                    raise LLMError(f"'{cfg['vision_model']}' cannot read images (capabilities: "
                                   f"{', '.join(caps)}). Use a vision model, e.g. gemma3:12b, qwen2.5vl:7b.")
            except LLMError as exc:
                if "cannot read images" in str(exc):
                    raise
    else:
        models = _get_json(f"{cfg['base_url']}/models")
        names = [m.get("id", "") for m in models.get("data", [])]
    lines = [f"OK: {cfg['provider']} at {cfg['base_url']}"]
    lines += [f"  {role}: {model}" for role, model in needed]
    lines.append(f"  vision: {cfg['vision']}")
    lines.append(f"  available: {', '.join(names)}")
    return "\n".join(lines)


# -- field-by-field verification ---------------------------------------------------------
VERIFY_PROMPT = """You are verifying invoice fields extracted by a rule-based parser.
The parser's CANDIDATE values may be correct, wrong, or missing.
For EVERY field:
- value: the correct value according to the document (null if the document does not contain it).
  Keep the candidate if it is correct; replace it if it is wrong; fill it in if missing.
- evidence: the exact text copied from the document that proves the value (null if value is null).
  Copy it character-for-character (from the text, or as printed on the page image)
  so it can be located in the document.
For items, return the full, correct list of invoice lines."""


def _scalar_paths(fields: Dict[str, Dict[str, Any]]) -> List[str]:
    return [p for p, s in fields.items() if (s or {}).get("type") != "list" and (s or {}).get("llm") is not False]


def build_verify_schema(fields: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    for path in _scalar_paths(fields):
        props[path] = {
            "type": "object",
            "properties": {"value": _json_type(fields[path]), "evidence": {"type": ["string", "null"]}},
            "required": ["value", "evidence"],
            "additionalProperties": False,
        }
    full = build_schema(fields)
    for path, spec in fields.items():
        if (spec or {}).get("type") == "list":
            props[path] = full["properties"][path]
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def build_verify_prompt(doc: Document, template: Template, cfg: Dict[str, Any],
                        candidates: Dict[str, Any], n_images: int = 0) -> str:
    from .model import get_path

    cand = {p: get_path(candidates, p) for p in _scalar_paths(template.fields)}
    cand["items"] = candidates.get("items") or []
    return (build_prompt(doc, template, cfg, n_images).replace("Return only the JSON object.", "")
            + "\n\nCANDIDATE VALUES FROM THE PARSER:\n" + json.dumps(cand, indent=1, default=str)
            + "\n\nReturn only the JSON object.")


def ask_verification(doc: Document, template: Template, candidates: Dict[str, Any],
                     **overrides):
    """Ask the model to confirm/correct every field. Returns ``(raw answer, model label)``."""
    cfg = llm_config(template, **overrides)
    images, model = prepare_inputs(doc, cfg)
    content = call_model(VERIFY_PROMPT, build_verify_prompt(doc, template, cfg, candidates, len(images)),
                         build_verify_schema(template.fields), cfg, images=images, model=model)
    label = f"{cfg['provider']}/{model}" + (f" (vision, {len(images)} page(s))" if images else "")
    return _parse_json(content), label
