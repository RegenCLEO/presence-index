"""Write the public detail of a ranked scan: the keywords, and each signal of the ten public rows.

Reads scores.json, snapshot.public.json and the files the scores were computed from;
writes detail.public.json. It adds no number to the scan. Each value is copied from a
stored file or counted from one, so a reader can recompute a published index from its
ten signal scores and trace each score to what the scan read.

The file holds no brand outside the rows of the public snapshot, and no timestamp, so
the same scan files always give the same bytes. Run it after the rank stage.
"""
from __future__ import annotations

from datetime import date
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional

from calculator import reference
from scan import panel as panel_file, store
from scan.rank import PUBLIC
from scan.records import owned_by_any, social_platform
from scan.score import (ADDITIONS, LOCAL, MENTION_FEED, ORGANIC_FLOOR_VOLUME, REVIEWS, SCORES, SERP, SITES,
                        SOCIAL_PLATFORMS, organic_weeks)

DETAIL = "detail.public.json"
SIGNAL_KEYS = ("state", "score", "se", "raw", "raw_range", "reason")
ORGANIC = "rec.organic_search"

Row = Dict[str, Any]


def _day(stamp: Optional[str]) -> Optional[str]:
    return stamp[:10] if stamp else None


# ---------- the panel's keywords ----------
def keyword_rows(panel: panel_file.Panel) -> List[Row]:
    """Each keyword with its monthly searches and its share of the Organic Search weight,
    largest first. A keyword with no known volume counts as the floor, as the score does."""
    volumes = [(k.text, k.volume if k.volume > 0 else ORGANIC_FLOOR_VOLUME) for k in panel.keywords]
    total = sum(v for _, v in volumes)
    return [{"text": text, "volume": int(v), "share": round(v / total, 6)}
            for text, v in sorted(volumes, key=lambda kv: (-kv[1], kv[0]))]


# ---------- what each signal was read from ----------
def latest_pulls(serp: Iterable[Row]) -> Dict[str, List[Row]]:
    """pull -> its rows, for the latest pull of each ISO week, oldest first. The same
    pulls organic_weeks scores."""
    by_week: Dict[tuple, Dict[str, List[Row]]] = {}
    for row in serp:
        week = date.fromisoformat(row["pull"]).isocalendar()[:2]
        by_week.setdefault(week, {}).setdefault(row["pull"], []).append(row)
    return {max(pulls): pulls[max(pulls)] for _, pulls in sorted(by_week.items())}


def best_position(row: Row, brand: panel_file.Brand) -> Optional[int]:
    owned = [r["position"] for r in row["results"]
             if 1 <= r["position"] <= 10 and owned_by_any(r["url"], brand.domains)
             and not owned_by_any(r["url"], brand.not_domains)]
    return min(owned, default=None)


def organic_evidence(serp: List[Row], brand: panel_file.Brand, top_keyword: str) -> Row:
    """Each week's visibility, the keywords with a page of the brand in the top 10, and
    the brand's best position for the keyword that carries the most weight."""
    pulls = latest_pulls(serp)
    weeks = []
    for pull, visibility, n in organic_weeks(serp, brand.domains, brand.not_domains):
        positions = {row["keyword"]: best_position(row, brand) for row in pulls[pull]}
        weeks.append({"pull": pull, "visibility": round(visibility, 6), "keywords": n,
                      "keywords_in_top_10": sum(1 for p in positions.values() if p),
                      "top_keyword_position": positions.get(top_keyword)})
    return {"top_keyword": top_keyword, "weeks": weeks}


def site_evidence(site: Optional[Row]) -> Optional[Row]:
    if not site or site.get("status") != "ok":
        return None
    summary, seo = site["summary"], site["site_seo"]
    counted = [p for p in site["pages"] if p["counted"]]
    parities = [p["parity"] for p in counted if p["parity"] is not None]
    found = {social_platform(url) for url in summary["social_urls"]} & set(SOCIAL_PLATFORMS)
    return {
        "read_on": _day(site.get("read_at")), "pages_listed": len(site["pages"]), "pages_read": len(counted),
        "answer_agent_access": summary["access"],
        "content_in_html_share": round(mean(parities), 4) if parities else None,
        "indexable_share": summary["indexable_share"],
        "lighthouse_seo": seo.get("lighthouse_seo"), "on_page_points": seo.get("on_page_points"),
        "social_platforms": sorted(found), "social_platforms_checked": list(SOCIAL_PLATFORMS),
        "org_markup": bool(summary["org_with_name_url"]), "org_same_as": bool(summary["org_same_as"]),
        "page_type_markup_share": summary["pagetype_share"], "valid_markup_share": summary["valid_share"],
    }


def reviews_evidence(row: Optional[Row]) -> Optional[Row]:
    if not row or row.get("status") != "ok":
        return None
    return {"read_on": _day(row.get("fetched_at")), "reviews": int(row["n"]), "mean_rating": row["mean_rating"],
            "profiles": [{"platform": p["platform"], "profile": p["profile"], "reviews": p["count"],
                          "rating": p["rating"]} for p in row.get("platforms") or []]}


def mentions_evidence(row: Optional[Row]) -> Optional[Row]:
    """Per source: the mentions found, and how many of them count toward the mass. A
    mention does not count when it is paid, owned, older than the window or not about
    the business."""
    if not row or row.get("status") != "ok":
        return None
    counted: Dict[str, int] = {}
    for mention in row["mentions"]:
        if mention["eligible"]:
            counted[mention["source"]] = counted.get(mention["source"], 0) + 1
    return {"read_on": _day(row.get("fetched_at")), "mass": round(float(row["mass"]), 2),
            "sources": {name: {"found": found, "counted": counted.get(name, 0)}
                        for name, found in sorted(row["counts"].items())}}


def local_evidence(row: Optional[Row]) -> Optional[Row]:
    if not row or row.get("status") != "ok":
        return None
    listing = row.get("listing") or {}
    return {"read_on": _day(row.get("fetched_at")), "directories": sorted(row.get("directories") or {}),
            "listing": listing.get("title"), "category": listing.get("category"),
            "has_hours": bool(listing.get("has_hours")), "addresses_compared": bool((row.get("nap_check") or {}).get("compared")),
            "presence_share": row["presence_share"], "hours_category_share": row["hours_category_share"]}


# ---------- the file ----------
def detail(panel: panel_file.Panel, scores: Row, public: Row, serp: List[Row], sites: Dict[str, Row],
           supplements: Dict[str, Dict[str, Row]]) -> Row:
    """The public detail. supplements: file name -> that file's brands, for the files the scan has."""
    keywords = keyword_rows(panel)
    by_domain = {b["domain"]: bid for bid, b in scores["brands"].items()}
    brands_by_id = {b.id: b for b in panel.brands}
    rows = []
    for public_row in public["rows"]:
        bid = by_domain[public_row["domain"]]
        scored, brand = scores["brands"][bid], brands_by_id[bid]
        organic = organic_evidence(serp, brand, keywords[0]["text"]) if keywords else None
        signal = scored["signals"].get(ORGANIC) or {}
        if organic and signal.get("state") == reference.LIVE:
            weekly = mean(w["visibility"] for w in organic["weeks"][-4:])
            if abs(weekly - signal["raw"]) > 1e-5:
                raise store.StoreError(f"{brand.name}: the weekly visibilities give {weekly}, and {SCORES} holds "
                                       f"{signal['raw']}. Run the score and rank stages again.")
        rows.append({
            "domain": public_row["domain"], "name": public_row["name"],
            "engines": {e: {k: v[k] for k in ("rate", "valid", "questions", "counts")}
                        for e, v in sorted(scored["engines"].items())},
            "signals": {sid: {k: scored["signals"][sid][k] for k in SIGNAL_KEYS} for sid in reference.REGISTRY},
            "evidence": {
                "organic": organic,
                "site": site_evidence(sites.get(bid)),
                "reviews": reviews_evidence((supplements.get(REVIEWS) or {}).get(bid)),
                "mentions": mentions_evidence((supplements.get(MENTION_FEED) or {}).get(bid)),
                "local": local_evidence((supplements.get(LOCAL) or {}).get(bid)),
            },
        })
    return {
        "panel_id": scores["panel_id"], "panel_sha256": scores["panel_sha256"],
        "snapshot_sha256": public["snapshot_sha256"], "scores_sha256": public["scores_sha256"],
        "weights": {sid: weight for sid, (_, _, weight) in reference.REGISTRY.items()},
        "curves": {sid: {"x50": x50, "x90": x90} for sid, (x50, x90) in reference.DEFAULT_CURVES.items()},
        "click_curve": {str(pos): share for pos, share in sorted(reference.CTR_AWR_UNBRANDED_2026_07.items())},
        "mention_tiers": dict(reference.TIERS),
        "keywords": keywords,
        "brands": rows,
    }


def add_arguments(p) -> None:
    pass


def main(args) -> int:
    out = args.out
    panel = panel_file.with_additions(panel_file.load(args.panel), out / ADDITIONS)
    for name in (SCORES, PUBLIC):
        if not (out / name).exists():
            raise store.StoreError(f"{out / name} does not exist. Run the score and rank stages first.")
    scores, public = store.read_json(out / SCORES), store.read_json(out / PUBLIC)
    store.require_panel([scores], panel.sha256, out / SCORES)
    stale = sorted(name for name, sha in scores["inputs"].items() if store.file_sha256(out / name) != sha)
    if stale:
        raise store.StoreError(f"{', '.join(stale)} changed after {SCORES} was written. Run the score stage again.")
    if public.get("scores_sha256") != store.file_sha256(out / SCORES):
        raise store.StoreError(f"{PUBLIC} was not written from this {SCORES}. Run the rank stage again.")

    sites = store.read_json(out / SITES)["brands"] if (out / SITES).exists() else {}
    supplements = {name: store.read_json(out / name)["brands"]
                   for name in (REVIEWS, MENTION_FEED, LOCAL) if (out / name).exists()}
    body = detail(panel, scores, public, store.read_jsonl(out / SERP), sites, supplements)
    store.write_json(out / DETAIL, body)
    print(f"wrote {out / DETAIL}: {len(body['keywords'])} keywords and {len(body['brands'])} brands, "
          f"each with {len(reference.REGISTRY)} signals")
    return 0
