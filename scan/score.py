"""Score every brand from the scan's answers, mentions, keyword pulls and site reads.

Reads answers.jsonl, mentions.jsonl, serp.jsonl and sites.json; writes scores.json.
All maths comes from calculator/reference.py. The rules for turning rows into the inputs
of that maths are the public scan protocol (METHODOLOGY.md, Appendix D).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from statistics import mean
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import reference
from reference import LIVE, NA, NOT_MEASURED, WARMING, Z95, lognormal_score, wilson

from scan import panel as panel_file, store
from scan.records import owned_by_any, social_platform

ANSWERS, MENTIONS, SERP, SITES, ADDITIONS = "answers.jsonl", "mentions.jsonl", "serp.jsonl", "sites.json", "additions.yaml"
REVIEWS, MENTION_FEED, LOCAL = "reviews.json", "mentions.json", "local.json"
SCORES = "scores.json"
CPI_VERSION = "1.0.0"

# Thresholds of the public scan protocol (METHODOLOGY.md, Appendix D).
AI_MIN_ANSWERS_PER_ENGINE = 60        # an engine counts from this many valid answers
AI_MIN_QUESTIONS_PER_ENGINE = 10      # on at least this many questions
AI_MIN_WEIGHT_SHARE = 0.50            # counted engines carry this share of the weight of the engines asked
ORGANIC_MIN_WEEKS = 2                 # calendar weeks with a pull
ORGANIC_FLOOR_VOLUME = 10             # monthly searches given to a keyword with no known volume
SOCIAL_PLATFORMS = ("linkedin", "instagram", "x", "facebook")

READINESS = ("rdy.crawl_access", "rdy.site_seo", "rdy.social", "rdy.structured_data")
# What PS-1.0 measures. The other three signals of the method are outside the protocol.
PROTOCOL_SIGNALS = ("rec.ai_recommendation", "rec.ai_citation", "rec.organic_search", *READINESS)
NOT_IN_PROTOCOL = tuple(sid for sid in reference.REGISTRY if sid not in PROTOCOL_SIGNALS)
# The file each of those three is read from. A scan measures one of them when its stage
# (reviews, mentions or local) has written that file. Without the file it stays out.
SUPPLEMENT_FILES = {"rec.reviews": REVIEWS, "rec.independent_mentions": MENTION_FEED, "rdy.local_listings": LOCAL}

Row = Dict[str, Any]
Signal = Dict[str, Any]
Cells = Dict[Tuple[str, str], List[Row]]  # (engine, question id) -> the answers given to it

# Each AI signal: which valid answers it counts, and the value a mention row gives one of them.
# AI Citation counts only answers that cite any source.
AI_SIGNALS: Dict[str, Tuple[Callable[[Row], bool], Callable[[Row], float]]] = {
    "rec.ai_recommendation": (lambda answer: True, lambda mention: float(mention["value"])),
    "rec.ai_citation": (lambda answer: bool(answer["cited_urls"]),
                        lambda mention: 1.0 if mention["own_domain_cited"] else 0.0),
}


class ScoreError(store.StoreError):
    pass


# ---------- signals ----------
def _signal(state: str, score: Optional[float] = None, se: float = 0.0, raw: Optional[float] = None,
            raw_range: Optional[List[float]] = None, reason: Optional[str] = None) -> Signal:
    """Score rounded to 1 decimal and the error left unrounded. The index is computed from
    these stored values, so anyone can recompute it from scores.json."""
    return {"state": state, "score": None if score is None else round(score, 1), "se": se or 0.0,
            "raw": raw, "raw_range": raw_range, "reason": reason}


def zero_error_range(rate: float, n_questions: int, curve: tuple) -> tuple:
    """(rate_low, rate_high, score, score_se) for a combined rate whose error is 0.

    Every question gave the same value, so the effective sample is undefined and
    reference.wilson_effective returns a range of zero width. The range is a Wilson
    interval on the number of panel questions instead (METHODOLOGY.md, Appendix D).
    """
    lo, hi = wilson(rate * n_questions, n_questions)
    s_lo, s_hi = lognormal_score(lo, *curve), lognormal_score(hi, *curve)
    return lo, hi, lognormal_score(rate, *curve), (s_hi - s_lo) / (2 * Z95)


def ai_measure(values_by_engine: Dict[str, List[float]], weights: Dict[str, float], counted: Set[str],
               curve: tuple, n_questions: int) -> Optional[Dict[str, float]]:
    """Rate, its error and range, score and score error, unrounded; None while warming up.

    values_by_engine: one value per question for each engine. weights: every engine of the
    panel, so the live share is taken of the engines asked. counted: the engines that
    pass the 60-answers-on-10-questions rule.
    """
    live = {e: v for e, v in values_by_engine.items() if e in counted}
    result = reference.engine_weighted_rate(live, weights, min_n=AI_MIN_QUESTIONS_PER_ENGINE,
                                            min_weight_share=AI_MIN_WEIGHT_SHARE)
    if result is None:
        return None
    rate, se, _ = result
    if se > 0:
        score, _, _, score_se = reference.signal_range(rate, se, curve)
        lo, hi = reference.wilson_effective(rate, se)
    else:
        lo, hi, score, score_se = zero_error_range(rate, n_questions, curve)
    return {"rate": rate, "rate_se": se, "low": lo, "high": hi, "score": score, "score_se": score_se}


def ai_signal(values_by_engine: Dict[str, List[float]], weights: Dict[str, float], counted: Set[str],
              curve: tuple, n_questions: int) -> Signal:
    m = ai_measure(values_by_engine, weights, counted, curve, n_questions)
    if m is None:
        return _signal(WARMING, reason="not_enough_answers")
    return _signal(LIVE, m["score"], m["score_se"], raw=m["rate"], raw_range=[round(m["low"], 4), round(m["high"], 4)])


def organic_weeks(serp: Iterable[Row], domains: Sequence[str],
                  not_domains: Sequence[str] = ()) -> List[Tuple[str, float, int]]:
    """(pull, visibility, keywords) per ISO week, oldest first, from that week's latest pull."""
    by_week: Dict[Tuple[int, int], Dict[str, List[Row]]] = {}
    for row in serp:
        week = date.fromisoformat(row["pull"]).isocalendar()[:2]
        by_week.setdefault(week, {}).setdefault(row["pull"], []).append(row)
    weeks = []
    for week in sorted(by_week):
        pull = max(by_week[week])
        rows = []
        for row in by_week[week][pull]:
            owned = [r["position"] for r in row["results"]
                     if 1 <= r["position"] <= 10 and owned_by_any(r["url"], domains)
                     and not owned_by_any(r["url"], not_domains)]
            volume = row.get("volume") or 0
            rows.append((volume if volume > 0 else ORGANIC_FLOOR_VOLUME, min(owned, default=None)))
        weeks.append((pull, reference.visibility(rows), len(rows)))
    return weeks


def organic_signal(weeks: List[Tuple[str, float, int]], panel_keywords: int) -> Signal:
    if not weeks:
        return _signal(NOT_MEASURED, reason="no_keyword_pulls")
    if any(n < panel_keywords for _, _, n in weeks[-4:]):
        # Visibility over part of the keyword panel is a different number. Finish the pull.
        return _signal(WARMING, reason="keyword_pull_incomplete")
    if len(weeks) < ORGANIC_MIN_WEEKS:
        return _signal(WARMING, reason="not_enough_weeks")
    rate, se = reference.organic_rate([vis for _, vis, _ in weeks])
    score, score_se = reference.score_with_se(rate, se, reference.DEFAULT_CURVES["rec.organic_search"])
    return _signal(LIVE, score, score_se, raw=rate,
                   raw_range=[round(max(0.0, rate - Z95 * se), 4), round(rate + Z95 * se, 4)])


def readiness_signals(site: Optional[Row]) -> Dict[str, Signal]:
    """The four signals the site read fills. A site that was not read is not measured, never 0."""
    if site is None or site["status"] != "ok":
        reason = "site_not_read" if site is None else site.get("reason") or f"site_{site['status']}"
        return {sid: _signal(NOT_MEASURED, reason=reason) for sid in READINESS}
    summary, seo = site["summary"], site["site_seo"]
    parities = [p["parity"] for p in site["pages"] if p["counted"] and p["parity"] is not None]
    found = {social_platform(url) for url in summary["social_urls"]} & set(SOCIAL_PLATFORMS)
    return {
        "rdy.crawl_access": _signal(LIVE, reference.crawl_render_score(summary["access"], parities, summary["indexable_share"]))
        if parities else _signal(NOT_MEASURED, reason="no_pages_read"),
        "rdy.site_seo": _signal(LIVE, seo["score"]) if seo["score"] is not None
        else _signal(NOT_MEASURED, reason=seo.get("reason") or "no_seo_score"),
        "rdy.social": _signal(LIVE, reference.social_profiles_score(len(found), len(SOCIAL_PLATFORMS)), raw=len(found)),
        "rdy.structured_data": _signal(LIVE, reference.structured_data_score(
            bool(summary["org_with_name_url"]), bool(summary["org_same_as"]),
            summary["pagetype_share"], summary["valid_share"])),
    }


# ---------- reviews, independent mentions and local listings ----------
def reviews_signal(row: Optional[Row]) -> Signal:
    """From one brand's row of reviews.json. A profile with no reviews scores 0. A brand the
    review platform has no profile for is not measured: one platform says nothing of the rest."""
    if not row or row.get("status") != "ok":
        return _signal(NOT_MEASURED, reason=(row or {}).get("reason") or "no_review_lookup")
    n = int(row["n"])
    return _signal(LIVE, reference.reviews_score(n, row["mean_rating"]) if n else 0.0, raw=n)


def independent_mentions_signal(row: Optional[Row]) -> Signal:
    """From one brand's row of mentions.json. No mention found is a score of 0, not a gap."""
    if not row or row.get("status") != "ok":
        return _signal(NOT_MEASURED, reason=(row or {}).get("reason") or "no_mention_lookup")
    mass = float(row["mass"])
    return _signal(LIVE, lognormal_score(mass, *reference.DEFAULT_CURVES["rec.independent_mentions"]),
                   raw=round(mass, 2))


def local_listings_signal(brand: panel_file.Brand, row: Optional[Row]) -> Signal:
    """From one brand's row of local.json. Not applicable to a brand with no store or service area."""
    if not brand.local or (row or {}).get("status") == "not_applicable":
        return _signal(NA, reason="no_location")
    if not row or row.get("status") != "ok" or not row.get("directories"):
        return _signal(NOT_MEASURED, reason=(row or {}).get("reason") or "no_listing_check")
    return _signal(LIVE, reference.local_listings_score(row["presence_share"], row["nap_share"],
                                                        row["hours_category_share"]),
                   raw=round(row["presence_share"], 3))


def supplement_sources(sid: str, stored: Row) -> List[str]:
    """What a stage read for its signal: the review platforms, the mention sources that
    ran, or the local directories checked. A published ranking names them."""
    brands = stored.get("brands", {}).values()
    if sid == "rec.reviews":
        return sorted({p["platform"] for b in brands for p in b.get("platforms") or []})
    if sid == "rec.independent_mentions":
        return sorted(name for name, source in (stored.get("sources") or {}).items() if source.get("ran"))
    return sorted({name for b in brands for name in b.get("directories") or {}})


def supplement_signal(sid: str, brand: panel_file.Brand, row: Optional[Row]) -> Signal:
    if sid == "rec.reviews":
        return reviews_signal(row)
    if sid == "rec.independent_mentions":
        return independent_mentions_signal(row)
    return local_listings_signal(brand, row)


# ---------- index ----------
def assemble(signals: Dict[str, Signal], registry: Optional[Dict[str, tuple]] = None) -> Dict[str, Any]:
    """The index and its 95% range from the stored signal values. The scan never outputs a grade.
    ``registry`` replaces the method's signal weights, for the sensitivity test only."""
    result = reference.compute_index([
        reference.Signal(sid, layer, surface, weight, signals[sid]["state"], signals[sid]["score"], signals[sid]["se"])
        for sid, (layer, surface, weight) in (registry or reference.REGISTRY).items()])
    index, raw_index, se = result["index"], result["raw_index"], result["se"]
    return {
        "index": index, "raw_index": raw_index, "se": se, "coverage": result["coverage"], "status": result["status"],
        # Centred on the unrounded index, so each end is rounded once.
        "range": [round(raw_index - Z95 * se, 1), round(raw_index + Z95 * se, 1)] if index is not None and se else None,
        "raw_range": None if raw_index is None else [raw_index - Z95 * se, raw_index + Z95 * se],
    }


def label(coverage: float) -> str:
    version = ".".join(CPI_VERSION.split(".")[:2])
    return f"CLEO Presence Index, public scan, CPI v{version}, {round(coverage * 100, 1):g}% of method measured"


# ---------- answers ----------
def cells_of(answers: Iterable[Row]) -> Cells:
    out: Cells = {}
    for a in answers:
        out.setdefault((a["engine"], a["question_id"]), []).append(a)
    return out


def cells_by_run(answers: Sequence[Row], runs: Iterable[int]) -> Dict[int, Cells]:
    """Each run's cells, keeping only the (engine, question) pairs answered in every run, so
    the runs are compared on the same questions."""
    by_run = {run: cells_of(a for a in answers if a["run"] == run) for run in runs}
    paired = set.intersection(*(set(cells) for cells in by_run.values()))
    return {run: {key: cells[key] for key in paired} for run, cells in by_run.items()}


def tally(cells: Cells) -> Dict[str, Tuple[int, int]]:
    """engine -> (answers, questions)."""
    out: Dict[str, Tuple[int, int]] = {}
    for (engine, _), answers in cells.items():
        n, q = out.get(engine, (0, 0))
        out[engine] = (n + len(answers), q + 1)
    return out


def engine_counts(answers: int, questions: int) -> bool:
    """An engine counts from 60 valid answers on at least 10 questions."""
    return answers >= AI_MIN_ANSWERS_PER_ENGINE and questions >= AI_MIN_QUESTIONS_PER_ENGINE


def mention_index(valid_answers: Sequence[Row], mentions: Iterable[Row],
                  names_sha: Dict[str, str]) -> Dict[Tuple[str, str], Row]:
    """(answer id, brand id) -> mention row, after checking every valid answer has exactly
    one row for every brand. A missing row is an unread answer, not a brand that was absent."""
    brand_ids = list(names_sha)
    index: Dict[Tuple[str, str], Row] = {}
    repeated = set()
    for m in mentions:
        key = (m["answer_id"], m["brand_id"])
        if key in index:
            repeated.add(key)
        index[key] = m
    needed = {(a["id"], b) for a in valid_answers for b in brand_ids}
    old_names = sum(1 for key in needed if key in index and index[key].get("names_sha") != names_sha[key[1]])
    if old_names:
        raise ScoreError(f"{old_names} rows in {MENTIONS} were written before a name was added for their brand in "
                         f"{ADDITIONS}. Run the parse stage, then score again.")
    missing = len(needed - set(index))
    repeated &= needed
    if missing:
        raise ScoreError(f"{MENTIONS} is missing {missing} of the {len(needed)} rows needed (one per valid answer "
                         "and brand). Run the parse stage, then score again.")
    if repeated:
        raise ScoreError(f"{MENTIONS} has more than one row for {len(repeated)} (answer, brand) pairs. "
                         "Each pair must be read once.")
    return index


def question_values(cells: Cells, mentions: Dict[Tuple[str, str], Row], brand_id: str,
                    value_of: Callable[[Row], float]) -> Dict[str, List[float]]:
    """Per engine, one value per question: the mean over that question's answers."""
    out: Dict[str, List[float]] = {}
    for (engine, _), answers in sorted(cells.items()):
        out.setdefault(engine, []).append(mean(value_of(mentions[(a["id"], brand_id)]) for a in answers))
    return out


# ---------- the scan ----------
@dataclass(frozen=True)
class AiInput:
    """What one AI signal reads from the scan, the same for every brand."""
    cells: Cells
    runs: Dict[int, Cells]
    totals: Dict[str, Tuple[int, int]]   # engine -> (answers, questions), for every panel engine
    value_of: Callable[[Row], float]
    counted: Set[str]                    # engines passing the 60-answers-on-10-questions rule
    curve: tuple


def score_scan(panel: panel_file.Panel, answers: Sequence[Row], mentions: Iterable[Row], serp: Sequence[Row],
               sites: Dict[str, Row], supplements: Optional[Dict[str, Dict[str, Row]]] = None) -> Dict[str, Any]:
    """Everything in scores.json except the input hashes. ``sites`` is sites.json's ``brands``.
    ``supplements`` maps a signal id in SUPPLEMENT_FILES to the ``brands`` of its stage's file,
    for the stages that ran."""
    supplements = supplements or {}
    weights = {e: reference.ENGINE_WEIGHTS_V1[e] for e in panel.engines}
    n_questions = len(panel.questions)
    runs = range(1, panel.runs_per_day + 1)
    valid = [a for a in answers if a["valid"]]
    by_pair = mention_index(valid, mentions, {b.id: b.names_sha for b in panel.brands})

    # Which engines count depends on the answers alone, so it is decided once for the whole
    # scan and reused for each run: one run has at most 60 answers per engine.
    ai = {}
    for sid, (counts, value_of) in AI_SIGNALS.items():
        population = [a for a in valid if counts(a)]
        cells = cells_of(population)
        seen = tally(cells)
        totals = {e: seen.get(e, (0, 0)) for e in weights}
        ai[sid] = AiInput(cells, cells_by_run(population, runs), totals, value_of,
                          {e for e, (n, q) in totals.items() if engine_counts(n, q)}, reference.DEFAULT_CURVES[sid])
    rec, cit = ai["rec.ai_recommendation"], ai["rec.ai_citation"]

    def ai_signals(brand_id: str, run: Optional[int] = None) -> Dict[str, Signal]:
        return {sid: ai_signal(question_values(s.cells if run is None else s.runs[run], by_pair, brand_id, s.value_of),
                               weights, s.counted, s.curve, n_questions)
                for sid, s in ai.items()}

    measured_by_brand = {}
    for brand in panel.brands:
        fixed = {
            "rec.organic_search": organic_signal(organic_weeks(serp, brand.domains, brand.not_domains), len(panel.keywords)),
            **readiness_signals(sites.get(brand.id)),
            **{sid: supplement_signal(sid, brand, supplements[sid].get(brand.id)) if sid in supplements
               else _signal(NOT_MEASURED, reason="not_in_protocol") for sid in NOT_IN_PROTOCOL},
        }
        measured_by_brand[brand.id] = ({**ai_signals(brand.id), **fixed},
                                       {run: {**ai_signals(brand.id, run), **fixed} for run in runs})

    # Brands are comparable only when the same signals are live for each. A signal counts
    # for this scan when it is live for at least half the brands. Below that (AI Citation
    # with too few sourced answers, Organic Search before the second pull, Site SEO that
    # reached only a few brands) it is out for every brand, including the few that have it,
    # and lowers every brand's coverage alike. A brand missing a signal that does count
    # (a blocked site) is scored on what it has and left out of the ranking. A brand a
    # signal does not apply to is neither counted nor held back by it.
    candidates = (*PROTOCOL_SIGNALS, *(sid for sid in NOT_IN_PROTOCOL if sid in supplements))
    states = {sid: [whole[sid]["state"] for whole, _ in measured_by_brand.values()] for sid in candidates}
    live_for = {sid: states[sid].count(LIVE) for sid in candidates}
    measured = [sid for sid in candidates
                if live_for[sid] and 2 * live_for[sid] >= len(states[sid]) - states[sid].count(NA)]

    def comparable(signals: Dict[str, Signal]) -> Dict[str, Signal]:
        return {sid: _signal(NOT_MEASURED, reason="measured_for_too_few_brands")
                if sid in candidates and sid not in measured and signals[sid]["state"] == LIVE else signals[sid]
                for sid in reference.REGISTRY}

    brands: Dict[str, Any] = {}
    per_run: Dict[str, Dict[str, Any]] = {str(run): {} for run in runs}
    for brand in panel.brands:
        whole, by_run = measured_by_brand[brand.id]
        signals = comparable(whole)
        index = assemble(signals)
        if index["status"] == "rated":
            # A public scan reads fewer sources than the full method, so its scores stay
            # Provisional whatever the coverage (METHODOLOGY.md, Appendix D).
            index["status"] = "provisional"
        rec_values = question_values(rec.cells, by_pair, brand.id, rec.value_of)
        brands[brand.id] = {
            "name": brand.name, "domain": brand.domain, "source": brand.source,
            **index, "label": label(index["coverage"]),
            "fully_measured": bool(measured) and all(signals[sid]["state"] in (LIVE, NA) for sid in measured),
            "rec_rate": signals["rec.ai_recommendation"]["raw"],
            "rec_rate_range": signals["rec.ai_recommendation"]["raw_range"],
            "cit_rate": signals["rec.ai_citation"]["raw"],
            "engines": {e: {"rate": round(mean(rec_values[e]), 4) if e in rec_values else None,
                            "questions": rec.totals[e][1], "valid": rec.totals[e][0],
                            "counts": e in rec.counted} for e in weights},
            "signals": signals,
        }
        for run in runs:
            run_signals = comparable(by_run[run])
            per_run[str(run)][brand.id] = {"raw_index": assemble(run_signals)["raw_index"],
                                           "rec_rate": run_signals["rec.ai_recommendation"]["raw"]}

    warnings = []
    for day in sorted({a["day"] for a in answers}):
        planned = len(panel.questions) * panel.runs_per_day
        for e in weights:
            asked = sum(1 for a in answers if a["engine"] == e and a["day"] == day)
            if asked != planned:
                warnings.append(f"{e} has {asked} of {planned} answers for {day}; rerun the run stage")
    for pull in sorted({r["pull"] for r in serp}):
        got = sum(1 for r in serp if r["pull"] == pull)
        if got != len(panel.keywords):
            warnings.append(f"keyword pull {pull} has {got} of {len(panel.keywords)} keywords; rerun the keywords stage")

    return {
        "panel_id": panel.panel_id, "panel_sha256": panel.sha256, "protocol": panel.protocol,
        "cpi_version": CPI_VERSION, "category": panel.category, "warnings": warnings,
        "market": {"country": panel.market.country, "language": panel.market.language},
        # The days and pulls that hold data, not the ones the panel planned.
        "days": sorted({a["day"] for a in answers}), "keyword_pulls": sorted({r["pull"] for r in serp}),
        "questions": len(panel.questions), "runs_per_day": panel.runs_per_day,
        "signals_measured": measured,
        "signals_not_measured": [sid for sid in reference.REGISTRY if sid not in measured],
        "thresholds": {
            "ai_min_answers_per_engine": AI_MIN_ANSWERS_PER_ENGINE,
            "ai_min_questions_per_engine": AI_MIN_QUESTIONS_PER_ENGINE,
            "ai_min_weight_share": AI_MIN_WEIGHT_SHARE,
            "organic_min_weeks": ORGANIC_MIN_WEEKS,
            "organic_floor_volume": ORGANIC_FLOOR_VOLUME,
        },
        "engines": {e: {
            "weight": w, "asked": sum(1 for a in answers if a["engine"] == e),
            "valid": rec.totals[e][0], "questions": rec.totals[e][1],
            "counts_for_recommendation": e in rec.counted,
            "with_sources": cit.totals[e][0], "questions_with_sources": cit.totals[e][1],
            "counts_for_citation": e in cit.counted,
        } for e, w in weights.items()},
        # What produced the answers and read them, so a ranking names its models and the
        # version of the reader prompt. The engine models are the ones the answers record:
        # on the chatgpt.com route the app picks the model.
        "models": {
            "engines": {e: sorted({a["model"] for a in valid if a["engine"] == e and a.get("model")}) for e in weights},
            "readers": {slot: cfg.get("model") or cfg.get("provider") for slot, cfg in sorted(panel.reader.items())},
            # Only the rows the scores were made from: one per valid answer and panel brand.
            "reader_version": sorted({by_pair[(a["id"], b.id)].get("reader_version") or "unrecorded"
                                      for a in valid for b in panel.brands}),
        },
        "brands": brands, "runs": per_run,
    }


def add_arguments(p) -> None:
    pass


def main(args) -> int:
    out = args.out
    # Hashed before the files are read: if one changes in between, rank sees a mismatch.
    present = [name for name in SUPPLEMENT_FILES.values() if (out / name).exists()]
    inputs = store.input_hashes(out, (ANSWERS, MENTIONS, SERP, SITES, ADDITIONS, *present))
    panel = panel_file.with_additions(panel_file.load(args.panel), out / ADDITIONS)
    rows = {}
    for name in (ANSWERS, MENTIONS, SERP):
        rows[name] = store.read_jsonl(out / name)
        store.require_panel(rows[name], panel.sha256, out / name)
    sites = store.read_json(out / SITES) if (out / SITES).exists() else {"panel_sha256": panel.sha256, "brands": {}}
    store.require_panel([sites], panel.sha256, out / SITES)

    supplements, sources = {}, {}
    for sid, name in SUPPLEMENT_FILES.items():
        if name in present:
            stored = store.read_json(out / name)
            store.require_panel([stored], panel.sha256, out / name)
            supplements[sid] = stored["brands"]
            sources[sid] = supplement_sources(sid, stored)

    scores = score_scan(panel, rows[ANSWERS], rows[MENTIONS], rows[SERP], sites["brands"], supplements)
    # Only for the signals that count in this scan.
    scores["signal_sources"] = {sid: names for sid, names in sources.items() if sid in scores["signals_measured"]}
    scores["inputs"] = inputs
    store.write_json(out / SCORES, scores)
    with_index = sum(1 for b in scores["brands"].values() if b["index"] is not None)
    full = sum(1 for b in scores["brands"].values() if b["fully_measured"])
    print(f"scored {len(scores['brands'])} brands: {with_index} with an index, {full} fully measured. Wrote {out / SCORES}")
    for warning in scores["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    return 0
