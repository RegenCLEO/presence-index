"""Rank the fully measured brands and write the full and the public snapshot.

Reads scores.json; writes snapshot.full.json (every brand) and snapshot.public.json (the
first ten rows, plus a rate-only line for an unranked brand whose recommendation rate
would place it among them). Neither file holds a timestamp, so the same scores.json
always gives the same bytes. The rank rules are METHODOLOGY.md, Appendix D.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from scan import panel as panel_file, store
from scan.score import ADDITIONS, SCORES

FULL, PUBLIC = "snapshot.full.json", "snapshot.public.json"
PUBLIC_ROWS = 10
MAX_JOINT_LEADERS = 3
STABILITY_TOP = 3
PUBLIC_ROW_KEYS = ("rank", "rank_label", "name", "domain", "index", "range", "rec_rate", "rec_rate_range",
                   "cit_rate", "coverage", "status", "source", "label")
HEADER_KEYS = ("panel_id", "panel_sha256", "protocol", "cpi_version", "category", "market", "days", "keyword_pulls",
               "questions", "runs_per_day", "signals_measured", "signals_not_measured", "warnings", "engines", "models")

Brand = Dict[str, Any]


def _order(raw_index: float, rec_rate: float, name: str, brand_id: str) -> tuple:
    """Unrounded index, then recommendation rate, then name. The id makes the order total."""
    return (-raw_index, -(rec_rate or 0.0), name, brand_id)


def tiers(ordered: List[Tuple[str, Brand]]) -> List[List[Tuple[str, Brand]]]:
    """The first brand anchors a tier. A later brand joins it while its upper bound reaches
    the anchor's lower bound, and otherwise anchors the next tier."""
    out: List[List[Tuple[str, Brand]]] = []
    for brand_id, brand in ordered:
        if out and brand["raw_range"][1] >= out[-1][0][1]["raw_range"][0]:
            out[-1].append((brand_id, brand))
        else:
            out.append([(brand_id, brand)])
    return out


def ranked_rows(brands: Dict[str, Brand]) -> List[Dict[str, Any]]:
    """One row per fully measured brand with an index, best first. Brands in a tier share
    the rank, which counts every brand in the tiers above: 1=, 1=, 3."""
    eligible = [(bid, b) for bid, b in brands.items() if b["fully_measured"] and b["index"] is not None]
    eligible.sort(key=lambda item: _order(item[1]["raw_index"], item[1]["rec_rate"], item[1]["name"], item[0]))
    rows, rank = [], 1
    for number, tier in enumerate(tiers(eligible), start=1):
        for brand_id, b in tier:
            rows.append({
                "brand_id": brand_id, "tier": number, "rank": rank,
                "rank_label": f"{rank}=" if len(tier) > 1 else str(rank),
                "name": b["name"], "domain": b["domain"], "index": b["index"], "range": b["range"],
                "raw_index": b["raw_index"], "raw_range": b["raw_range"],
                # Unrounded, so a page that shows one decimal rounds once, as the detail does.
                "rec_rate": b["rec_rate"], "rec_rate_range": b["rec_rate_range"], "cit_rate": b["cit_rate"],
                "coverage": b["coverage"], "status": b["status"], "source": b["source"], "label": b["label"],
            })
        rank += len(tier)
    return rows


def leader(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Tier one: one leader, two or three joint leaders, none when more share it."""
    first = [r["name"] for r in rows if r["tier"] == 1]
    if not first or len(first) > MAX_JOINT_LEADERS:
        return {"status": "none", "brands": []}
    return {"status": "leader" if len(first) == 1 else "joint_leaders", "brands": first}


def unranked_reason(brand: Brand) -> str:
    """Why the brand has no rank: the reasons of the signals the protocol measures that are not live."""
    # The code only: text after a colon can be a vendor's or a site's own error message.
    reasons = {(s["reason"] or s["state"]).split(":")[0].strip() for s in brand["signals"].values()
               if s["state"] not in ("live", "not_applicable")
               and s["reason"] not in ("not_in_protocol", "measured_for_too_few_brands")}
    return ", ".join(sorted(reasons)) or "no_index"


def unranked_rows(brands: Dict[str, Brand], ranked: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every brand without a rank, highest recommendation rate first. ``rate_only`` marks the
    ones the public snapshot names: a rate above zero that is among the ten highest of all
    the scan's brands, so it would place in the top ten on rate."""
    ranked_ids = {r["brand_id"] for r in ranked}
    rates = sorted((b["rec_rate"] for b in brands.values() if b["rec_rate"] is not None), reverse=True)
    tenth = rates[min(len(rates), PUBLIC_ROWS) - 1] if rates else None
    rows = []
    for brand_id, b in brands.items():
        if brand_id in ranked_ids:
            continue
        rate = b["rec_rate"]
        rows.append({
            "brand_id": brand_id, "name": b["name"], "domain": b["domain"], "reason": unranked_reason(b),
            "rec_rate": rate, "rec_rate_range": b["rec_rate_range"], "coverage": b["coverage"],
            "status": b["status"], "source": b["source"],
            "rate_only": tenth is not None and rate is not None and rate > 0 and rate >= tenth,
        })
    rows.sort(key=lambda r: (-(brands[r["brand_id"]]["rec_rate"] or 0.0), r["name"], r["brand_id"]))
    return rows


def run_top(run: Dict[str, Dict[str, Any]], names: Dict[str, str]) -> List[str]:
    """The ids of one run's top three among the ranked brands, with every brand tied at third."""
    rows = sorted((_order(r["raw_index"], r["rec_rate"], names[bid], bid) for bid, r in run.items()
                   if bid in names and r["raw_index"] is not None and r["rec_rate"] is not None))
    if len(rows) > STABILITY_TOP:
        third = rows[STABILITY_TOP - 1][0]
        rows = [r for r in rows if r[0] <= third]
    return [r[3] for r in rows]


def stability(runs: Dict[str, Dict[str, Dict[str, Any]]], ranked: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run one against run two. A run with no AI rate cannot confirm the other, so ``stable``
    is then false. One run alone, or a scan with no ranked brand, is not a check: null."""
    names = {r["brand_id"]: r["name"] for r in ranked}
    top = {run: run_top(rows, names) for run, rows in sorted(runs.items())}
    sets = [frozenset(ids) for ids in top.values()]
    stable = None if len(sets) < 2 or not ranked else all(sets) and len(set(sets)) == 1
    return {"top": {run: [names[bid] for bid in ids] for run, ids in top.items()},
            "stable": stable, "needs_second_day": None if stable is None else not stable}


def digest(body: Dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshots(scores: Dict[str, Any], scores_sha256: Optional[str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """(full, public). The public one holds no brand outside the first ten rows except the
    rate-only lines, and per-run names stay in the full one for the same reason."""
    missing = [k for k in HEADER_KEYS if k not in scores]
    if missing:
        raise store.StoreError(f"{SCORES} has no {', '.join(missing)}. It was written by an older version: "
                               "run the score stage again.")
    brands = scores["brands"]
    ranked = ranked_rows(brands)
    unranked = unranked_rows(brands, ranked)
    public_rows = ranked[:PUBLIC_ROWS]
    sharing_last = sum(1 for r in ranked[PUBLIC_ROWS:] if r["rank"] == public_rows[-1]["rank"]) if public_rows else 0
    stable = stability(scores["runs"], ranked)
    header = {**{k: scores[k] for k in HEADER_KEYS}, "signal_sources": scores.get("signal_sources", {}),
              "scores_sha256": scores_sha256}

    full = {**header, "leader": leader(ranked), "ranked": ranked, "unranked": unranked,
            "published_rows": len(public_rows), "unpublished_sharing_last_rank": sharing_last, "stability": stable}
    full["snapshot_sha256"] = digest(full)
    public = {
        **header, "snapshot_sha256": full["snapshot_sha256"], "leader": full["leader"],
        "rows": [{k: r[k] for k in PUBLIC_ROW_KEYS} for r in public_rows],
        "unpublished_sharing_last_rank": sharing_last,
        "rate_only": [{k: r[k] for k in ("name", "rec_rate", "rec_rate_range", "reason")} for r in unranked if r["rate_only"]],
        "stability": {"stable": stable["stable"], "needs_second_day": stable["needs_second_day"]},
    }
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
    stale = sorted(name for name, sha in scores["inputs"].items() if store.file_sha256(out / name) != sha)
    if stale:
        raise store.StoreError(f"{', '.join(stale)} changed after {SCORES} was written. Run the score stage again.")
    full, public = snapshots(scores, store.file_sha256(path))
    store.write_json(out / FULL, full)
    store.write_json(out / PUBLIC, public)
    print(f"ranked {len(full['ranked'])} brands, published {len(public['rows'])}, "
          f"{len(public['rate_only'])} rate-only, leader: {full['leader']['status']}, "
          f"stable: {full['stability']['stable']}. Wrote {out / FULL} and {out / PUBLIC}")
    return 0
