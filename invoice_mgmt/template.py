"""Loading, resolving and merging invoice templates.

A template is a YAML file describing:

* ``fields``   – the data model (types, defaults, labels)
* ``match``    – regexes used to auto-detect which template fits a document
* ``extract``  – how to pull data out of an invoice (regex patterns, line items)
* ``render``   – how to produce an invoice (PDF layout, HTML, DOCX, PDF forms)

Templates can ``extends:`` another template and only override what differs,
which is what makes adapting to a new vendor/layout quick.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

BUILTIN_DIR = Path(__file__).parent / "templates"
ENV_VAR = "INVOICE_TEMPLATES"


class TemplateError(Exception):
    """Raised when a template cannot be found or is invalid."""


def deep_merge(base: Any, override: Any) -> Any:
    """Recursively merge ``override`` into ``base`` (dicts merge, everything else replaces)."""
    if isinstance(base, dict) and isinstance(override, dict):
        out = copy.deepcopy(base)
        for key, value in override.items():
            out[key] = deep_merge(out.get(key), value) if key in out else copy.deepcopy(value)
        return out
    return copy.deepcopy(override)


def search_dirs(extra: Optional[List[os.PathLike]] = None) -> List[Path]:
    """Directories searched for templates, highest priority first."""
    dirs: List[Path] = [Path(d) for d in (extra or [])]
    env = os.environ.get(ENV_VAR)
    if env:
        dirs += [Path(p) for p in env.split(os.pathsep) if p]
    dirs.append(Path.cwd() / "templates")
    dirs.append(BUILTIN_DIR)
    seen, unique = set(), []
    for d in dirs:
        key = d.resolve() if d.exists() else d
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def find_template_file(name_or_path: str, extra_dirs=None) -> Path:
    candidate = Path(name_or_path)
    if candidate.suffix in (".yaml", ".yml") and candidate.is_file():
        return candidate
    for d in search_dirs(extra_dirs):
        for ext in (".yaml", ".yml"):
            p = d / f"{name_or_path}{ext}"
            if p.is_file():
                return p
    raise TemplateError(
        f"Template '{name_or_path}' not found. Searched: "
        + ", ".join(str(d) for d in search_dirs(extra_dirs))
    )


@dataclass
class Template:
    name: str
    path: Optional[Path]
    data: Dict[str, Any] = field(default_factory=dict)

    # -- convenience accessors -------------------------------------------------
    @property
    def description(self) -> str:
        return self.data.get("description", "")

    @property
    def fields(self) -> Dict[str, Dict[str, Any]]:
        return self.data.get("fields", {}) or {}

    @property
    def match(self) -> List[str]:
        return self.data.get("match", []) or []

    @property
    def extract(self) -> Dict[str, Any]:
        return self.data.get("extract", {}) or {}

    @property
    def render(self) -> Dict[str, Any]:
        return self.data.get("render", {}) or {}

    @property
    def base_dir(self) -> Path:
        return self.path.parent if self.path else Path.cwd()

    def resolve_path(self, p: str) -> Path:
        """Resolve a file referenced by the template (relative to the template file)."""
        path = Path(p)
        if path.is_absolute() or path.exists():
            return path
        return self.base_dir / path


def _load_raw(path: Path) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise TemplateError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise TemplateError(f"Template {path} must be a YAML mapping")
    return data


def load_template(name_or_path: str, extra_dirs=None, _chain: Optional[List[str]] = None) -> Template:
    """Load a template by name or path, resolving ``extends`` recursively."""
    path = find_template_file(name_or_path, extra_dirs)
    chain = (_chain or []) + [str(path.resolve())]
    raw = _load_raw(path)

    parent_name = raw.pop("extends", None)
    if parent_name:
        # Look for the parent next to the child first, then in the normal dirs.
        dirs = [path.parent] + list(extra_dirs or [])
        parent_path = find_template_file(parent_name, dirs)
        if str(parent_path.resolve()) in chain:
            raise TemplateError(f"Circular 'extends' detected: {' -> '.join(chain)}")
        parent = load_template(str(parent_path), dirs, chain)
        merged = deep_merge(parent.data, raw)
        # A child's extraction rule for a field replaces the parent's rule entirely
        # (merging regex lists from two layouts is never what you want).
        child_rules = (raw.get("extract") or {}).get("fields") or {}
        for key, rule in child_rules.items():
            merged["extract"]["fields"][key] = copy.deepcopy(rule)
    else:
        merged = raw

    name = raw.get("name") or path.stem
    merged["name"] = name
    return Template(name=name, path=path, data=merged)


def list_templates(extra_dirs=None) -> List[Template]:
    """Return every loadable template (first match wins when names collide)."""
    found: Dict[str, Template] = {}
    for d in search_dirs(extra_dirs):
        if not d.is_dir():
            continue
        for p in sorted(list(d.glob("*.yaml")) + list(d.glob("*.yml"))):
            if p.stem in found:
                continue
            try:
                found[p.stem] = load_template(str(p), extra_dirs)
            except TemplateError:
                continue
    return list(found.values())
