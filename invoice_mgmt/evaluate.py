"""Score extraction against an answer key (``truth.json`` written by ``invoice testset``)."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .extract import extract
from .model import get_path, parse_number

FIELDS = ["invoice_number", "issue_date", "due_date", "seller.name", "buyer.name",
          "subtotal", "tax", "total"]


def _norm(v: Any) -> str:
    return re.sub(r"[^0-9a-zÀ-ɏ]+", "", str(v).lower())


def same(expected: Any, got: Any) -> bool:
    if isinstance(expected, (int, float)):
        g = parse_number(got)
        return g is not None and abs(g - expected) < 0.011
    return _norm(expected) == _norm(got)


def score_one(truth: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    checks: Dict[str, Optional[bool]] = {}
    for f in FIELDS:
        exp = get_path(truth, f)
        if exp in (None, ""):
            continue
        checks[f] = same(exp, get_path(data, f))
    t_items, g_items = truth.get("items") or [], data.get("items") or []
    checks["items.count"] = len(t_items) == len(g_items)
    t_amounts = sorted(round(i["amount"], 2) for i in t_items)
    g_amounts = sorted(round(parse_number(i.get("amount")) or 0, 2) for i in g_items)
    checks["items.amounts"] = t_amounts == g_amounts
    return checks


def run(directory, engine: Optional[str] = None, llm_options: Optional[Dict[str, Any]] = None,
        template=None, only: Optional[List[str]] = None, out=None) -> Dict[str, Any]:
    import sys
    out = out or sys.stdout
    d = Path(directory)
    truth = json.loads((d / "truth.json").read_text(encoding="utf-8"))
    results, total_ok, total_n = [], 0, 0
    per_field: Dict[str, List[bool]] = {}
    for name, entry in truth.items():
        if only and not any(o in name for o in only):
            continue
        t0 = time.time()
        error = None
        try:
            res = extract(d / name, template, engine=engine, llm_options=llm_options)
            data = res["data"]
        except Exception as exc:  # report and carry on with the next file
            res, data, error = {}, {}, f"{type(exc).__name__}: {exc}"
        secs = time.time() - t0
        checks = score_one(entry["data"], data)
        ok = sum(1 for v in checks.values() if v)
        total_ok += ok
        total_n += len(checks)
        for k, v in checks.items():
            per_field.setdefault(k, []).append(bool(v))
        print(f"\n{name}  [{entry['kind']}]", file=out)
        print(f"  engine: {res.get('engine', '-')}   time: {secs:.1f}s   score: {ok}/{len(checks)}", file=out)
        if error:
            print(f"  ERROR: {error}", file=out)
        for k, v in checks.items():
            if not v:
                if k.startswith("items."):
                    got = [i.get("amount") for i in data.get("items") or []]
                    exp = [i["amount"] for i in entry["data"].get("items") or []]
                    print(f"  x {k:<15} expected {len(exp)} lines {exp[:6]}{'...' if len(exp) > 6 else ''}"
                          f"  got {len(got)} {got[:6]}{'...' if len(got) > 6 else ''}", file=out)
                else:
                    print(f"  x {k:<15} expected {get_path(entry['data'], k)!r:<28} got {get_path(data, k)!r}",
                          file=out)
        statuses: Dict[str, int] = {}
        for e in (res.get("fields") or {}).values():
            statuses[e["status"]] = statuses.get(e["status"], 0) + 1
        if statuses:
            print("  verify: " + ", ".join(f"{v} {k}" for k, v in sorted(statuses.items())), file=out)
        results.append({"file": name, "score": ok, "of": len(checks), "seconds": round(secs, 1),
                        "engine": res.get("engine"), "error": error, "checks": checks})
    print("\n" + "=" * 70, file=out)
    print("Per field accuracy:", file=out)
    for k, vals in per_field.items():
        print(f"  {k:<15} {sum(vals):>2}/{len(vals)}", file=out)
    pct = 100 * total_ok / total_n if total_n else 0
    print(f"OVERALL: {total_ok}/{total_n} checks correct ({pct:.0f}%)", file=out)
    return {"results": results, "correct": total_ok, "total": total_n}
