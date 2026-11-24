"""CLEO Presence Index v1.0: reference implementation.

Normative for the scoring maths. Licence: Apache-2.0.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import log, sqrt
from statistics import NormalDist, mean, pvariance, stdev

Z90 = NormalDist().inv_cdf(0.90)  # 1.2816
Z95 = 1.96

LIVE, WARMING, NOT_MEASURED, NA = "live", "warming_up", "not_measured", "not_applicable"
SURFACES = ("website", "maps", "search", "ai_search", "social", "reviews")

# signal_id: (layer, surface, weight)
REGISTRY = {
    "rec.ai_recommendation": ("recognition", "ai_search", 24),
    "rec.organic_search": ("recognition", "search", 14),
    "rec.reviews": ("recognition", "reviews", 12),
    "rec.ai_citation": ("recognition", "ai_search", 10),
    "rec.independent_mentions": ("recognition", "social", 10),
    "rdy.crawl_access": ("readiness", "website", 9),
    "rdy.site_seo": ("readiness", "website", 9),
    "rdy.social": ("readiness", "social", 6),
    "rdy.local_listings": ("readiness", "maps", 4),
    "rdy.structured_data": ("readiness", "website", 2),
}

# Judgement defaults (x50, x90); replaced by calibrated values at release.
DEFAULT_CURVES = {
    "rec.ai_recommendation": (0.15, 0.50),
    "rec.ai_citation": (0.05, 0.25),
    "rec.organic_search": (0.05, 0.25),
    "rec.independent_mentions": (10.0, 60.0),
    "rec.reviews.volume": (50.0, 400.0),
}

ENGINE_WEIGHTS_V1 = {"chatgpt": 0.41, "google_ai": 0.25, "gemini": 0.21, "claude": 0.08, "perplexity": 0.05}

ANSWER_AGENTS = {
    "chatgpt": ("OAI-SearchBot", "ChatGPT-User"),
    "google_ai": ("Googlebot",),
    "gemini": ("Googlebot",),
    "claude": ("Claude-SearchBot", "Claude-User"),
    "perplexity": ("PerplexityBot", "Perplexity-User"),
}

# Advanced Web Ranking CTR study: unbranded queries, Google US, all devices, July 2026, per cent.
CTR_AWR_UNBRANDED_2026_07 = {1: 19.38, 2: 9.64, 3: 6.02, 4: 3.88, 5: 2.68,
                             6: 1.86, 7: 1.37, 8: 1.10, 9: 0.94, 10: 0.81}

TIERS = {"A": 3.0, "B": 1.5, "C": 0.5, "community": 1.0}


# ---------- 1. Curves and calibration ----------
def lognormal_score(x: float, x50: float, x90: float) -> float:
    """Higher-is-better curve: x50 scores 50, x90 scores 90, x <= 0 scores 0."""
    if x50 <= 0 or x90 <= x50:
        raise ValueError("need 0 < x50 < x90")
    if x <= 0:
        return 0.0
    mu = log(x50)
    sigma = (log(x90) - mu) / Z90
    return 100.0 * NormalDist().cdf((log(x) - mu) / sigma)


def quantile(sorted_values: list, q: float) -> float:
    """Linear interpolation between closest ranks."""
    n = len(sorted_values)
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def calibrate(values: list, default: tuple, min_positive: int = 30) -> tuple:
    """x50 = median of positive values, x90 = their 90th percentile; else the default."""
    pos = sorted(v for v in values if v > 0)
    if len(pos) < min_positive:
        return default
    x50, x90 = quantile(pos, 0.50), quantile(pos, 0.90)
    return (x50, x90) if x90 > x50 else default


def score_with_se(x: float, se_x: float, curve: tuple) -> tuple:
    """Score through a curve; propagate a standard error (points) by the interval method."""
    s = lognormal_score(x, *curve)
    if se_x <= 0:
        return s, 0.0
    lo = lognormal_score(max(x - Z95 * se_x, 0.0), *curve)
    hi = lognormal_score(x + Z95 * se_x, *curve)
    return s, (hi - lo) / (2 * Z95)


# ---------- 2. Engine weight rule ----------
def engine_weights(trackers: dict, fixed: dict, floor: float = 0.05) -> dict:
    """Fixed engines keep their weight; the rest is split by average tracker share with a floor,
    rounded to whole points by largest remainder."""
    engines = sorted({e for shares in trackers.values() for e in shares} - set(fixed))
    avg = {e: mean(s[e] for s in trackers.values() if e in s) for e in engines}
    pool = 1.0 - sum(fixed.values())
    floored, active, provisional = {}, dict(avg), {}
    while active:
        share_pool = pool - floor * len(floored)
        total = sum(active.values())
        provisional = {e: share_pool * v / total for e, v in active.items()}
        low = [e for e, w in provisional.items() if w < floor]
        if not low:
            break
        for e in low:
            floored[e] = floor
            del active[e]
    points = round((pool - floor * len(floored)) * 100)
    raw = {e: provisional[e] * 100 for e in active}
    whole = {e: int(raw[e]) for e in raw}
    for e in sorted(raw, key=lambda k: raw[k] - whole[k], reverse=True)[: points - sum(whole.values())]:
        whole[e] += 1
    out = {e: p / 100 for e, p in whole.items()}
    out.update(floored)
    out.update(fixed)
    return out


# ---------- 3. AI signals ----------
def wilson(k: float, n: int, z: float = Z95) -> tuple:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def engine_weighted_rate(values_by_engine: dict, weights: dict, min_n: int = 200, min_weight_share: float = 0.70):
    """values: per-response labels (1, 0.5, 0) for AI Recommendation, or 1/0 flags per citing
    response for AI Citation. Returns (rate, se, live_engines), or None while warming up."""
    live = {e: v for e, v in values_by_engine.items() if e in weights and len(v) >= min_n}
    w_live = sum(weights[e] for e in live)
    if not live or w_live < min_weight_share * sum(weights.values()):
        return None
    rate = sum(weights[e] * mean(v) for e, v in live.items()) / w_live
    se = sqrt(sum((weights[e] / w_live) ** 2 * pvariance(v) / len(v) for e, v in live.items()))
    return rate, se, sorted(live)


def effective_n(rate: float, se: float) -> float:
    """n* = r(1 - r) / SE^2: the sample size a plain proportion would need for this SE."""
    return rate * (1 - rate) / (se * se) if se > 0 else 0.0


def wilson_effective(rate: float, se: float) -> tuple:
    """95% Wilson range on the effective sample. Asymmetric whenever rate is not 0.5."""
    n = effective_n(rate, se)
    if n <= 0:
        return (rate, rate)
    return wilson(rate * n, n)


def signal_range(rate: float, se: float, curve: tuple) -> tuple:
    """(score, score_low, score_high, score_se). The curve is monotone, so scoring the
    rate's range end points gives the score's range exactly."""
    lo, hi = wilson_effective(rate, se)
    s_lo, s_hi = lognormal_score(lo, *curve), lognormal_score(hi, *curve)
    return lognormal_score(rate, *curve), s_lo, s_hi, (s_hi - s_lo) / (2 * Z95)


# ---------- 4. Organic Search Visibility ----------
def visibility(rows: list, ctr: dict = CTR_AWR_UNBRANDED_2026_07) -> float:
    """rows: (monthly volume, best owned position or None). Positions outside the curve score 0."""
    den = sum(vol * ctr[1] for vol, _ in rows)
    num = sum(vol * ctr.get(pos, 0.0) for vol, pos in rows if pos)
    return num / den if den else 0.0


def organic_rate(weekly_vis: list) -> tuple:
    """Mean of the last four weekly visibility values and its standard error."""
    last = weekly_vis[-4:]
    se = stdev(last) / sqrt(len(last)) if len(last) >= 2 else 0.0
    return mean(last), se


# ---------- 5. Reviews & Ratings ----------
def reviews_score(n: int, mean_rating: float, prior_mean: float = 4.2, strength: int = 20,
                  volume_curve: tuple = DEFAULT_CURVES["rec.reviews.volume"]) -> float:
    if n == 0:
        return 0.0
    r_star = (strength * prior_mean + n * mean_rating) / (strength + n)
    s_rating = min(max((r_star - 3.5) / 1.3, 0.0), 1.0) * 100
    evidence = n / (n + strength)
    return 0.6 * s_rating * evidence + 0.4 * lognormal_score(n, *volume_curve)


# ---------- 6. Independent Mentions ----------
@dataclass
class Mention:
    tier: str                 # "A", "B", "C" or "community"
    age_days: int
    community: str = None     # subreddit, forum domain or YouTube channel for community mentions
    eligible: bool = True     # False when owned, wire, syndicated, paid or negative


def mentions_mass(mentions: list, half_life: float = 180, window: int = 365, community_cap: int = 3) -> float:
    """Each Mention is one deduplicated story or thread (clustering happens upstream)."""
    kept, per_bucket = [], {}
    for m in sorted(mentions, key=lambda m: m.age_days):
        if not m.eligible or not 0 <= m.age_days <= window:
            continue
        if m.tier == "community":
            key = (m.community, m.age_days // 30)
            if per_bucket.get(key, 0) >= community_cap:
                continue
            per_bucket[key] = per_bucket.get(key, 0) + 1
        kept.append(m)
    return sum(TIERS[m.tier] * 0.5 ** (m.age_days / half_life) for m in kept)


# ---------- 7. Readiness signals ----------
def answer_agent_access(allowed: dict, weights: dict = ENGINE_WEIGHTS_V1, agents: dict = ANSWER_AGENTS) -> float:
    """allowed: robots.txt verdict per user agent (may fetch the homepage and every key page).
    Agents not named in robots.txt are allowed by default. Returns 0 to 100."""
    if not allowed.get("Googlebot", True) or not allowed.get("Bingbot", True):
        return 0.0
    share = {e: mean(1.0 if allowed.get(a, True) else 0.0 for a in agents[e]) for e in weights}
    return 100 * sum(weights[e] * share[e] for e in weights) / sum(weights.values())


def parity(raw_chars: int, rendered_chars: int, threshold: float = 0.80) -> float:
    if rendered_chars <= 0:
        return 1.0 if raw_chars > 0 else 0.0
    return min(1.0, raw_chars / (threshold * rendered_chars))


def crawl_render_score(access: float, parities: list, indexable_share: float, days_unreachable: int = 0) -> float:
    if days_unreachable >= 7:
        return 0.0
    return 0.40 * access + 0.35 * 100 * mean(parities) + 0.25 * 100 * indexable_share


def titles_meta_score(pages: list) -> float:
    def one(p):
        t, m = (p.get("title") or "").strip(), (p.get("meta_description") or "").strip()
        return 40 * bool(t) + 20 * (30 <= len(t) <= 65) + 25 * bool(m) + 15 * (70 <= len(m) <= 160)
    titles = [(p.get("title") or "").strip().lower() for p in pages if (p.get("title") or "").strip()]
    duplicate_share = 1 - len(set(titles)) / len(titles) if titles else 0.0
    return max(0.0, mean(one(p) for p in pages) - 20 * duplicate_share)


def headings_score(pages: list) -> float:
    return mean(60 * (p["h1_count"] == 1) + 40 * (not p["skips_heading_level"]) for p in pages)


def cwv_score(lcp_ms: float, inp_ms: float, cls: float) -> float:
    def band(v, good, poor):
        return 100 if v <= good else 50 if v <= poor else 0
    return mean([band(lcp_ms, 2500, 4000), band(inp_ms, 200, 500), band(cls, 0.1, 0.25)])


def content_score(pages: list, near_duplicate_share: float) -> float:
    """pages carry the ARS content_size and content_quality sub-scores (0 to 100)."""
    return mean(0.6 * p["ars_content_size"] + 0.4 * p["ars_content_quality"] for p in pages) * (1 - near_duplicate_share)


def site_seo_score(titles_meta: float, headings: float, link_depth_share: float, cwv: float,
                   content: float, fresh_share: float) -> float:
    return (0.15 * titles_meta + 0.10 * headings + 0.15 * 100 * link_depth_share + 0.20 * cwv
            + 0.25 * content + 0.15 * 100 * fresh_share)


def structured_data_score(org_with_name_url: bool, org_same_as: bool, pagetype_share: float, valid_share: float) -> float:
    return 25 * org_with_name_url + 25 * (org_with_name_url and org_same_as) + 30 * pagetype_share + 20 * valid_share


def social_profiles_score(satisfied: int, listed: int) -> float:
    return 100.0 * satisfied / listed if listed else 0.0


def local_listings_score(presence_share: float, nap_share: float, hours_category_share: float) -> float:
    return 100 * (0.40 * presence_share + 0.40 * nap_share + 0.20 * hours_category_share)


# ---------- 8. Aggregation ----------
@dataclass
class Signal:
    id: str
    layer: str
    surface: str
    weight: float
    state: str
    score: float = None
    se: float = 0.0


def weighted(signals: list):
    w = sum(s.weight for s in signals)
    return sum(s.weight * s.score for s in signals) / w if w else None


def _group(applicable: list, pred, w_live_total: float) -> dict:
    app = [s for s in applicable if pred(s)]
    live = [s for s in app if s.state == LIVE]
    w_app, w_live = sum(s.weight for s in app), sum(s.weight for s in live)
    score = weighted(live)
    return {"score": None if score is None else round(score, 1),
            "coverage": round(w_live / w_app, 3) if w_app else None,
            "contribution": round(w_live * score / w_live_total, 2) if score is not None and w_live_total else 0.0}


def compute_index(signals: list) -> dict:
    applicable = [s for s in signals if s.state != NA]
    live = [s for s in applicable if s.state == LIVE]
    w_app, w_live = sum(s.weight for s in applicable), sum(s.weight for s in live)
    coverage = w_live / w_app if w_app else 0.0
    index = weighted(live)
    se = sqrt(sum((s.weight / w_live) ** 2 * s.se ** 2 for s in live)) if w_live else None
    status = "rated" if coverage >= 0.80 else "provisional" if coverage >= 0.50 else "unrated"
    return {
        "index": None if status == "unrated" or index is None else round(index, 1),
        "raw_index": index, "se": se, "coverage": round(coverage, 3), "status": status,
        "layers": {g: _group(applicable, lambda s, g=g: s.layer == g, w_live) for g in ("readiness", "recognition")},
        "surfaces": {k: _group(applicable, lambda s, k=k: s.surface == k, w_live) for k in SURFACES},
    }


# ---------- 9. Trend and grades ----------
def like_for_like_change(then: list, now: list):
    ids = {s.id for s in then if s.state == LIVE} & {s.id for s in now if s.state == LIVE}
    a = weighted([s for s in then if s.id in ids])
    b = weighted([s for s in now if s.id in ids])
    return None if a is None or b is None else b - a


def mdc(se_index: float) -> float:
    return Z95 * sqrt(2) * se_index


def ewma(values: list, alpha: float = 0.25) -> list:
    out, s = [], None
    for v in values:
        s = v if s is None else alpha * v + (1 - alpha) * s
        out.append(s)
    return out


BANDS = [(85, "Leading"), (65, "Strong"), (40, "Building"), (0, "Weak")]
HYSTERESIS, HOLD_DAYS = 1.5, 7


def band_for(x: float) -> str:
    return next(name for lo, name in BANDS if x >= lo)


def next_grade(current, smoothed_history: list) -> str:
    """smoothed_history: daily smoothed index, oldest first. Grades apply only when Rated."""
    if current is None:
        return band_for(smoothed_history[-1])
    window = smoothed_history[-HOLD_DAYS:]
    if len(window) < HOLD_DAYS:
        return current
    lo = next(l for l, n in BANDS if n == current)
    hi = min((l for l, n in BANDS if l > lo), default=101)
    if all(x >= hi + HYSTERESIS for x in window) or all(x < lo - HYSTERESIS for x in window):
        return band_for(window[-1])
    return current


# ---------- 10. Release tooling ----------
def sensitivity(businesses: dict, delta: float = 0.25) -> dict:
    """businesses: {business_id: {signal_id: score}} over live signals. For each signal, scale its
    weight by (1 - delta) and (1 + delta) and report the largest absolute rank change."""
    base_w = {s: w for s, (_, _, w) in REGISTRY.items()}

    def ranks(weights):
        idx = {b: sum(weights[s] * v for s, v in sc.items()) / sum(weights[s] for s in sc)
               for b, sc in businesses.items()}
        order = sorted(idx, key=lambda b: (-idx[b], b))
        return {b: i + 1 for i, b in enumerate(order)}

    base = ranks(base_w)
    out = {}
    for s in base_w:
        worst = 0
        for f in (1 - delta, 1 + delta):
            w = dict(base_w)
            w[s] = w[s] * f
            r = ranks(w)
            worst = max(worst, max(abs(r[b] - base[b]) for b in base))
        out[s] = worst
    return out
