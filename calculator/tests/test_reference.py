import pytest
from reference import (LIVE, WARMING, NOT_MEASURED, NA, REGISTRY, ENGINE_WEIGHTS_V1, Mention, Signal,
                       lognormal_score, calibrate, engine_weights, wilson, engine_weighted_rate, visibility,
                       reviews_score, mentions_mass, answer_agent_access, parity, crawl_render_score,
                       structured_data_score, compute_index, like_for_like_change, mdc, band_for,
                       next_grade, sensitivity, effective_n, wilson_effective, signal_range)

A = {"rec.ai_recommendation": 42, "rec.ai_citation": 55, "rec.organic_search": 60, "rec.reviews": 72,
     "rec.independent_mentions": 38, "rdy.crawl_access": 90, "rdy.site_seo": 78, "rdy.social": 65,
     "rdy.local_listings": 70, "rdy.structured_data": 80}
C = {"rec.organic_search": 20, "rec.reviews": 25, "rec.independent_mentions": 10, "rdy.crawl_access": 85,
     "rdy.site_seo": 70, "rdy.social": 40, "rdy.local_listings": 55, "rdy.structured_data": 60}
AI_WARMING = {"rec.ai_recommendation": WARMING, "rec.ai_citation": WARMING}


def sigs(scores, states=None):
    states = states or {}
    return [Signal(i, l, s, w, states.get(i, LIVE), scores.get(i)) for i, (l, s, w) in REGISTRY.items()]


def test_registry_totals():
    assert sum(w for _, _, w in REGISTRY.values()) == 100
    assert sum(w for l, _, w in REGISTRY.values() if l == "recognition") == 70
    surfaces = {}
    for _, s, w in REGISTRY.values():
        surfaces[s] = surfaces.get(s, 0) + w
    assert surfaces == {"ai_search": 34, "website": 20, "social": 16, "search": 14, "reviews": 12, "maps": 4}


def test_curve_anchors_and_floor():
    assert lognormal_score(0.15, 0.15, 0.50) == pytest.approx(50, abs=0.01)
    assert lognormal_score(0.50, 0.15, 0.50) == pytest.approx(90, abs=0.01)
    assert lognormal_score(0, 0.15, 0.50) == 0
    with pytest.raises(ValueError):
        lognormal_score(0.1, 0.5, 0.2)


def test_calibration():
    values = [i / 100 for i in range(1, 101)] + [0] * 50
    x50, x90 = calibrate(values, (9, 99))
    assert x50 == pytest.approx(0.505) and x90 == pytest.approx(0.901)
    assert calibrate([0.1] * 29, (0.15, 0.5)) == (0.15, 0.5)


def test_engine_weight_rule_v1():
    trackers = {"similarweb": {"chatgpt": 55.5, "gemini": 25.6, "claude": 9.3, "perplexity": 0.9},
                "sensortower": {"chatgpt": 46.4, "gemini": 27.7, "claude": 10.3}}
    w = engine_weights(trackers, fixed={"google_ai": 0.25})
    assert w == pytest.approx(ENGINE_WEIGHTS_V1)
    assert sum(w.values()) == pytest.approx(1.0)


def test_worked_examples():
    r1 = compute_index(sigs(A))
    assert (r1["index"], r1["status"], band_for(r1["raw_index"])) == (59.8, "rated", "Building")
    assert r1["layers"]["readiness"]["score"] == 78.1 and r1["layers"]["recognition"]["score"] == 52.0
    assert {k: v["score"] for k, v in r1["surfaces"].items()} == {
        "website": 83.6, "maps": 70.0, "search": 60.0, "ai_search": 45.8, "social": 48.1, "reviews": 72.0}
    r2 = compute_index(sigs(A, {"rdy.local_listings": NA}))
    assert (r2["index"], r2["coverage"], r2["layers"]["readiness"]["score"]) == (59.4, 1.0, 79.3)
    r3 = compute_index(sigs(C, AI_WARMING))
    assert (r3["index"], r3["coverage"], r3["status"]) == (40.2, 0.66, "provisional")
    c5 = dict(C, **{"rec.ai_recommendation": 5, "rec.ai_citation": 0})
    r5 = compute_index(sigs(c5))
    assert (r5["index"], r5["status"], band_for(r5["raw_index"])) == (27.8, "rated", "Weak")
    assert r5["layers"]["recognition"]["score"] == 11.4
    assert like_for_like_change(sigs(C, AI_WARMING), sigs(c5)) == pytest.approx(0.0)


def test_contributions_sum_to_index():
    r = compute_index(sigs(A))
    assert sum(v["contribution"] for v in r["layers"].values()) == pytest.approx(r["raw_index"], abs=0.05)
    assert sum(v["contribution"] for v in r["surfaces"].values()) == pytest.approx(r["raw_index"], abs=0.05)


def test_not_applicable_versus_not_measured():
    na = compute_index(sigs(A, {"rdy.local_listings": NA}))
    nm = compute_index(sigs(A, {"rdy.local_listings": NOT_MEASURED}))
    assert na["index"] == nm["index"] == 59.4
    assert na["coverage"] == 1.0 and nm["coverage"] == 0.96


def test_gating():
    states = {k: NOT_MEASURED for k in ["rec.ai_recommendation", "rec.ai_citation", "rec.organic_search", "rec.reviews"]}
    r = compute_index(sigs(A, states))
    assert r["coverage"] == 0.40 and r["status"] == "unrated" and r["index"] is None


def test_grades_hysteresis():
    assert next_grade("Building", [66.6] * 6) == "Building"
    assert next_grade("Building", [66.6] * 7) == "Strong"
    assert next_grade("Building", [66.4] * 7) == "Building"
    assert next_grade("Building", [38.4] * 7) == "Weak"
    assert next_grade("Weak", [70.0] * 7) == "Strong"
    assert next_grade(None, [59.8]) == "Building"


def test_mdc_and_wilson():
    assert mdc(1.4) == pytest.approx(3.88, abs=0.01)
    lo, hi = wilson(84, 420)
    assert (hi - lo) / 2 == pytest.approx(0.038, abs=0.001)


def test_engine_weighted_rate_and_warm_up():
    values = {e: [1.0] * 60 + [0.0] * 140 for e in ENGINE_WEIGHTS_V1}
    rate, se, live = engine_weighted_rate(values, ENGINE_WEIGHTS_V1)
    assert rate == pytest.approx(0.30) and len(live) == 5 and se > 0
    thin = {"chatgpt": [1.0] * 250, "claude": [0.0] * 250}
    assert engine_weighted_rate(thin, ENGINE_WEIGHTS_V1) is None


def test_range_of_an_engine_weighted_rate():
    # A rate of 0.20 with SE 0.02 is as certain as a plain proportion from 400 answers.
    assert effective_n(0.20, 0.02) == pytest.approx(400)
    lo, hi = wilson_effective(0.20, 0.02)
    assert (lo, hi) == pytest.approx(wilson(80, 400))
    assert 0.20 - lo < hi - 0.20  # asymmetric below 0.5
    score, low, high, se = signal_range(0.20, 0.02, (0.15, 0.50))
    assert (score, low, high) == pytest.approx((lognormal_score(0.20, 0.15, 0.50), lognormal_score(lo, 0.15, 0.50),
                                                lognormal_score(hi, 0.15, 0.50)))
    assert se == pytest.approx((high - low) / 3.92)


def test_a_rate_with_no_error_has_a_range_of_zero_width():
    assert effective_n(0.3, 0.0) == 0.0
    assert wilson_effective(0.3, 0.0) == (0.3, 0.3)
    assert wilson_effective(1.0, 0.05) == (1.0, 1.0)  # r(1 - r) is 0, so there is no effective sample
    assert signal_range(0.3, 0.0, (0.15, 0.50))[3] == 0.0


def test_reviews():
    assert reviews_score(0, 0) == 0
    assert reviews_score(1, 5.0) == pytest.approx(1.9, abs=0.05)
    assert reviews_score(20, 4.8) == pytest.approx(34.5, abs=0.05)
    assert reviews_score(200, 4.7) == pytest.approx(80.6, abs=0.05)
    assert reviews_score(400, 4.8) == pytest.approx(91.9, abs=0.05)


def test_mentions():
    wire = [Mention("C", 1, eligible=False) for _ in range(51)]
    assert mentions_mass(wire) == 0
    assert mentions_mass([Mention("A", 0)]) == pytest.approx(3.0)
    assert mentions_mass([Mention("A", 180)]) == pytest.approx(1.5)
    threads = [Mention("community", d, community="r/example") for d in range(5)]
    assert mentions_mass(threads) == pytest.approx(sum(0.5 ** (d / 180) for d in range(3)))
    assert mentions_mass([Mention("B", 400)]) == 0


def test_visibility():
    assert visibility([(1000, 1)]) == pytest.approx(1.0)
    assert visibility([(1000, None)]) == 0
    assert visibility([(1000, 3), (1000, 11)]) == pytest.approx(6.02 / (2 * 19.38))


def test_crawl_render():
    training_blocked = {"GPTBot": False, "ClaudeBot": False, "Google-Extended": False, "CCBot": False,
                        "Bytespider": False, "anthropic-ai": False}
    assert answer_agent_access(training_blocked) == pytest.approx(100)
    assert answer_agent_access({"Googlebot": False}) == 0
    assert answer_agent_access({"OAI-SearchBot": False}) == pytest.approx(100 - 41 * 0.5)
    assert parity(9000, 10000) == 1.0
    assert crawl_render_score(100, [1.0] * 20, 1.0) == pytest.approx(100)
    assert crawl_render_score(100, [1.0], 1.0, days_unreachable=7) == 0


def test_structured_data_ignores_llms_txt():
    assert structured_data_score(True, True, 1.0, 1.0) == 100
    assert structured_data_score(False, True, 0.0, 0.0) == 0


def test_sensitivity_identical_businesses():
    same = {b: dict(A) for b in ("x", "y", "z")}
    assert set(sensitivity(same).values()) == {0}
