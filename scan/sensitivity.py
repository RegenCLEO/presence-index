"""Test how the ranking moves when each signal weight moves 25 per cent either way.

Reads scores.json; writes sensitivity.full.json (every ranked brand) and
sensitivity.public.json (names only the brands in the public rows). Each test changes one
signal's weight and keeps the others, so the weights of the measured signals are
renormalised as they are for the index itself. Indexes, ranges and ranks are rebuilt with
the rules of the rank stage. No call leaves the machine. The promise this keeps is
METHODOLOGY.md, section 4 (Exhibit 3).
"""
from __future__ import annotations

from typing import Any, Dict, List

import reference

from scan import panel as panel_file, store
from scan.rank import PUBLIC_ROWS, digest, ranked_rows
from scan.score import ADDITIONS, SCORES, assemble

FULL, PUBLIC = "sensitivity.full.json", "sensitivity.public.json"
FACTORS = (0.75, 1.25)

Brand = Dict[str, Any]


def reweighted(brands: Dict[str, Brand], registry: Dict[str, tuple]) -> Dict[str, Brand]:
    """Every brand with its index and range rebuilt under ``registry``."""
    out = {}
    for brand_id, b in brands.items():
        if b["index"] is None:
            out[brand_id] = b
            continue
        a = assemble(b["signals"], registry)
        out[brand_id] = {**b, "index": a["index"], "raw_index": a["raw_index"], "range": a["range"],
                         "raw_range": a["raw_range"]}
    return out


def variants() -> List[Dict[str, Any]]:
    out = []
    for sid, (layer, surface, weight) in reference.REGISTRY.items():
        for factor in FACTORS:
            registry = dict(reference.REGISTRY)
            registry[sid] = (layer, surface, weight * factor)
            out.append({"signal": sid, "factor": factor, "weight": weight, "tested_weight": weight * factor,
                        "registry": registry})
    return out


def compare(base: List[Dict[str, Any]], test: List[Dict[str, Any]]) -> Dict[str, Any]:
    """How ``test`` differs from ``base``, over the brands ranked in both."""
    now = {r["brand_id"]: r for r in test}
    rows = []
    for r in base:
        # A weight change can move a brand's coverage below the line, and it then has no rank.
        t = now.get(r["brand_id"])
        rows.append({"brand_id": r["brand_id"], "name": r["name"], "rank_label": r["rank_label"],
                     "tested_rank_label": t and t["rank_label"], "index": r["index"], "tested_index": t and t["index"],
                     "index_change": t and round(t["raw_index"] - r["raw_index"], 2)})
    base_leaders = sorted(r["brand_id"] for r in base if r["rank"] == 1)
    test_leaders = sorted(r["brand_id"] for r in test if r["rank"] == 1)
    base_top = {r["brand_id"] for r in base[:PUBLIC_ROWS]}
    return {
        "rows": rows,
        "ranks_changed": sum(1 for r in rows if r["rank_label"] != r["tested_rank_label"]),
        "order_changed": [r["brand_id"] for r in base] != [r["brand_id"] for r in test],
        "leaders_changed": base_leaders != test_leaders,
        "entered_public_rows": sorted({r["brand_id"] for r in test[:PUBLIC_ROWS]} - base_top),
        "largest_index_change": max((abs(r["index_change"]) for r in rows if r["index_change"] is not None), default=0.0),
    }


def sensitivity(scores: Dict[str, Any], scores_sha256: str) -> tuple:
    """(full, public)."""
    brands = scores["brands"]
    base = ranked_rows(brands)
    published = {r["brand_id"] for r in base[:PUBLIC_ROWS]}
    tests = []
    for v in variants():
        result = compare(base, ranked_rows(reweighted(brands, v["registry"])))
        tests.append({k: v[k] for k in ("signal", "factor", "weight", "tested_weight")} | result)
    summary = {
        "tests": len(tests),
        "tests_changing_any_rank": sum(1 for t in tests if t["ranks_changed"]),
        "tests_changing_leaders": sum(1 for t in tests if t["leaders_changed"]),
        "tests_changing_public_rows": sum(1 for t in tests if t["entered_public_rows"]),
        "largest_index_change": max(t["largest_index_change"] for t in tests),
    }
    header = {k: scores[k] for k in ("panel_id", "panel_sha256", "cpi_version", "category", "market")}
    header |= {"scores_sha256": scores_sha256, "factors": list(FACTORS), "ranked_brands": len(base)}
    full = {**header, "summary": summary, "tests": tests}
    full["sensitivity_sha256"] = digest(full)

    def public_test(t: Dict[str, Any]) -> Dict[str, Any]:
        rows = [{k: r[k] for k in ("name", "rank_label", "tested_rank_label", "index", "tested_index", "index_change")}
                for r in t["rows"] if r["brand_id"] in published]
        return {k: t[k] for k in ("signal", "factor", "weight", "tested_weight", "ranks_changed", "leaders_changed",
                                  "largest_index_change")} | {
            "order_changed": t["order_changed"],
            "unpublished_brands_entering_public_rows": len(t["entered_public_rows"]),
            "rows": rows}

    public = {**header, "sensitivity_sha256": full["sensitivity_sha256"], "summary": summary,
              "tests": [public_test(t) for t in tests]}
    return full, public


def add_arguments(p) -> None:
    pass


def main(args) -> int:
    out = args.out
    panel = panel_file.with_additions(panel_file.load(args.panel), out / ADDITIONS)
    path = out / SCORES
    if not path.exists():
        raise store.StoreError(f"{path} does not exist. Run the score stage first.")
    scores = store.read_json(path)
    store.require_panel([scores], panel.sha256, path)
    full, public = sensitivity(scores, store.file_sha256(path))
    store.write_json(out / FULL, full)
    store.write_json(out / PUBLIC, public)
    s = full["summary"]
    print(f"{s['tests']} tests: {s['tests_changing_any_rank']} change a rank, {s['tests_changing_leaders']} change "
          f"the leaders, {s['tests_changing_public_rows']} change who is in the public rows; largest index change "
          f"{s['largest_index_change']}. Wrote {out / FULL} and {out / PUBLIC}")
    return 0
