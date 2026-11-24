import json

import pytest

from scan import panel as panel_file, rank, score, store
from scan.tests.test_score import ok_site, scan_rows, write_scan

LABEL = "CLEO Presence Index, public scan, CPI v1.0, 74% of method measured"
BLOCKED_SIGNALS = {
    "rec.ai_recommendation": {"state": "live", "reason": None},
    "rec.reviews": {"state": "not_measured", "reason": "not_in_protocol"},
    "rdy.crawl_access": {"state": "not_measured", "reason": "crawler_blocked"},
    "rdy.site_seo": {"state": "not_measured", "reason": "crawler_blocked"},
}


def brand(name, raw_index, half_width=1.0, rec_rate=0.3, ranked=True):
    """A scores.json brand. ``ranked=False`` is a brand whose site blocked the reader."""
    b = {"name": name, "domain": f"{name.lower().replace(' ', '')}.example", "source": "list",
         "raw_index": raw_index, "raw_range": [raw_index - half_width, raw_index + half_width],
         "rec_rate": rec_rate, "rec_rate_range": [round(rec_rate - 0.05, 4), round(rec_rate + 0.05, 4)], "cit_rate": 0.1,
         "index": round(raw_index, 1), "range": [round(raw_index - half_width, 1), round(raw_index + half_width, 1)],
         "coverage": 0.74, "status": "provisional", "label": LABEL, "fully_measured": True,
         "signals": {"rec.ai_recommendation": {"state": "live", "reason": None}}}
    if not ranked:
        b.update(index=None, range=None, coverage=0.48, status="unrated", fully_measured=False, signals=BLOCKED_SIGNALS)
    return b


def scores_of(*brands, runs=None):
    by_id = {b["name"].lower().replace(" ", "-"): b for b in brands}
    same = {bid: {"raw_index": b["raw_index"], "rec_rate": b["rec_rate"]} for bid, b in by_id.items()}
    return {"panel_id": "test-retailers-us-en", "panel_sha256": "a" * 64, "protocol": "PS-1.0", "cpi_version": "1.0.0",
            "engines": {"chatgpt": {"weight": 0.41, "asked": 120, "valid": 120}},
            "models": {"engines": {"chatgpt": ["gpt-test"]}, "readers": {"first": "fake"}, "reader_version": ["r1-test"]},
            "category": "Test retailers", "market": {"country": "US", "language": "en"}, "days": ["2026-11-21"],
            "keyword_pulls": ["2026-11-21", "2026-11-23"], "questions": 60, "runs_per_day": 2,
            "signals_measured": ["rec.ai_recommendation"], "signals_not_measured": ["rec.reviews"], "warnings": [],
            "brands": by_id, "runs": runs or {"1": same, "2": same}}


def run_of(**raw_index_by_id):
    return {bid: {"raw_index": v, "rec_rate": 0.3} for bid, v in raw_index_by_id.items()}


def test_brands_whose_ranges_reach_the_anchor_share_a_rank():
    # B's upper bound 80 reaches A's lower bound 78. C's upper bound 77 does not, although it overlaps B.
    full, public = rank.snapshots(scores_of(brand("C", 75, 2), brand("A", 80, 2), brand("B", 78, 2)), "hash")

    assert [(r["name"], r["rank"], r["rank_label"]) for r in public["rows"]] == [("A", 1, "1="), ("B", 1, "1="), ("C", 3, "3")]
    assert [r["tier"] for r in full["ranked"]] == [1, 1, 2]
    assert public["leader"] == {"status": "joint_leaders", "brands": ["A", "B"]}


def test_order_is_unrounded_index_then_recommendation_rate_then_name():
    _, public = rank.snapshots(scores_of(
        brand("Beta", 70.0, rec_rate=0.2), brand("Zed", 70.0, rec_rate=0.4), brand("Alpha", 70.0, rec_rate=0.2),
        brand("Low", 70.04 - 0.05), brand("Top", 70.04)), "hash")

    # Top and Low both show 70.0; the unrounded index separates them from the three at exactly 70.
    assert [r["name"] for r in public["rows"]] == ["Top", "Zed", "Alpha", "Beta", "Low"]
    assert [r["index"] for r in public["rows"]] == [70.0, 70.0, 70.0, 70.0, 70.0]


def test_one_brand_alone_in_tier_one_is_the_leader():
    _, public = rank.snapshots(scores_of(brand("A", 90), brand("B", 70)), "hash")
    assert public["leader"] == {"status": "leader", "brands": ["A"]}
    assert [r["rank_label"] for r in public["rows"]] == ["1", "2"]


def test_more_than_three_brands_in_tier_one_means_no_leader():
    three = [brand(n, 80 - i * 0.1, 2) for i, n in enumerate("ABC")]
    assert rank.snapshots(scores_of(*three), "hash")[1]["leader"] == {"status": "joint_leaders", "brands": ["A", "B", "C"]}
    four = [*three, brand("D", 79.5, 2)]
    assert rank.snapshots(scores_of(*four), "hash")[1]["leader"] == {"status": "none", "brands": []}


def thirteen():
    """Brand01 to Brand09 alone on their ranks, Brand10 to Brand12 sharing rank 10, Brand13 last."""
    spaced = [brand(f"Brand{i:02d}", 100 - 5 * i) for i in range(1, 10)]
    return [*spaced, brand("Brand10", 45.0), brand("Brand11", 44.5), brand("Brand12", 44.2), brand("Brand13", 30.0)]


def test_public_snapshot_holds_the_first_ten_rows_and_counts_who_shares_the_last_rank():
    full, public = rank.snapshots(scores_of(*thirteen()), "hash")

    assert [r["name"] for r in public["rows"]] == [f"Brand{i:02d}" for i in range(1, 11)]
    assert public["rows"][9]["rank_label"] == "10="
    assert public["unpublished_sharing_last_rank"] == 2
    assert [(r["name"], r["rank_label"]) for r in full["ranked"][10:]] == [("Brand11", "10="), ("Brand12", "10="), ("Brand13", "13")]
    text = json.dumps(public)
    for hidden in ("Brand11", "Brand12", "Brand13", "brand11.example", "brand12.example", "brand13.example"):
        assert hidden not in text
    assert set(public["rows"][0]) == {"rank", "rank_label", "name", "domain", "index", "range", "rec_rate",
                                      "rec_rate_range", "cit_rate", "coverage", "status", "source", "label"}
    assert public["rows"][0] == {
        "rank": 1, "rank_label": "1", "name": "Brand01", "domain": "brand01.example", "index": 95.0,
        "range": [94.0, 96.0], "rec_rate": 0.3, "rec_rate_range": [0.25, 0.35], "cit_rate": 0.1, "coverage": 0.74,
        "status": "provisional", "source": "list", "label": LABEL}


def test_unranked_brand_shows_its_rate_only_when_its_rate_is_among_the_ten_highest():
    ranked = thirteen()
    for i, b in enumerate(ranked):
        b["rec_rate"] = 0.5 if i < 8 else 0.1   # eight brands at 0.5, five at 0.1
    # Rates, highest first: 0.9, eight at 0.5, then 0.2 in tenth place.
    full, public = rank.snapshots(scores_of(
        *ranked, brand("Blocked High", 60, rec_rate=0.9, ranked=False), brand("Blocked Level", 60, rec_rate=0.2, ranked=False),
        brand("Blocked Low", 60, rec_rate=0.19, ranked=False)), "hash")

    assert public["rate_only"] == [
        {"name": "Blocked High", "rec_rate": 0.9, "rec_rate_range": [0.85, 0.95], "reason": "crawler_blocked"},
        {"name": "Blocked Level", "rec_rate": 0.2, "rec_rate_range": [0.15, 0.25], "reason": "crawler_blocked"}]
    assert [(r["name"], r["reason"], r["rate_only"], r["status"]) for r in full["unranked"]] == [
        ("Blocked High", "crawler_blocked", True, "unrated"), ("Blocked Level", "crawler_blocked", True, "unrated"),
        ("Blocked Low", "crawler_blocked", False, "unrated")]
    assert "Blocked Low" not in json.dumps(public) and "blockedlow.example" not in json.dumps(public)
    assert [r["name"] for r in public["rows"]] == [f"Brand{i:02d}" for i in range(1, 11)]


def test_stable_when_both_runs_have_the_same_top_three_in_any_order():
    brands = [brand(n, v) for n, v in (("A", 90), ("B", 80), ("C", 70), ("D", 60))]
    runs = {"1": run_of(a=90, b=80, c=70, d=60), "2": run_of(a=79, b=85, c=72, d=60)}
    full, public = rank.snapshots(scores_of(*brands, runs=runs), "hash")

    assert full["stability"] == {"top": {"1": ["A", "B", "C"], "2": ["B", "A", "C"]}, "stable": True, "needs_second_day": False}
    assert public["stability"] == {"stable": True, "needs_second_day": False}


def test_a_different_top_three_in_run_two_needs_a_second_day():
    brands = [brand(n, v) for n, v in (("A", 90), ("B", 80), ("C", 70), ("D", 60))]
    runs = {"1": run_of(a=90, b=80, c=70, d=60), "2": run_of(a=90, b=80, c=60, d=70)}
    full, public = rank.snapshots(scores_of(*brands, runs=runs), "hash")

    assert full["stability"]["top"] == {"1": ["A", "B", "C"], "2": ["A", "B", "D"]}
    assert public["stability"] == {"stable": False, "needs_second_day": True}


def test_stability_keeps_every_brand_tied_at_third_and_ignores_unranked_brands():
    brands = [brand(n, v) for n, v in (("A", 90), ("B", 80), ("C", 70), ("D", 70))]
    tied = run_of(a=90, b=80, c=70, d=70, blocked=99)
    full, _ = rank.snapshots(scores_of(*brands, brand("Blocked", 99, ranked=False), runs={"1": tied, "2": tied}), "hash")

    assert full["stability"]["top"] == {"1": ["A", "B", "C", "D"], "2": ["A", "B", "C", "D"]}
    assert full["stability"]["stable"] is True


def test_a_run_with_no_ai_rate_cannot_confirm_the_other():
    brands = [brand("A", 90), brand("B", 80)]
    empty = {"a": {"raw_index": 50.0, "rec_rate": None}, "b": {"raw_index": 40.0, "rec_rate": None}}
    full, _ = rank.snapshots(scores_of(*brands, runs={"1": empty, "2": empty}), "hash")
    assert (full["stability"]["stable"], full["stability"]["needs_second_day"]) == (False, True)

    one_run, _ = rank.snapshots(scores_of(*brands, runs={"1": run_of(a=90, b=80)}), "hash")
    assert (one_run["stability"]["stable"], one_run["stability"]["needs_second_day"]) == (None, None)


def test_both_snapshots_carry_the_same_hashes_and_no_grade():
    scores = scores_of(*thirteen())
    full, public = rank.snapshots(scores, "scores-hash")

    body = {k: v for k, v in full.items() if k != "snapshot_sha256"}
    assert full["snapshot_sha256"] == rank.digest(body) == public["snapshot_sha256"]
    assert len(full["snapshot_sha256"]) == 64
    for snapshot in (full, public):
        assert (snapshot["panel_id"], snapshot["panel_sha256"], snapshot["scores_sha256"]) == \
            ("test-retailers-us-en", "a" * 64, "scores-hash")
        assert snapshot["engines"] == {"chatgpt": {"weight": 0.41, "asked": 120, "valid": 120}}
        assert "grade" not in json.dumps(snapshot)
    scores["brands"]["brand13"]["raw_index"] = 30.5
    assert rank.snapshots(scores, "scores-hash")[0]["snapshot_sha256"] != full["snapshot_sha256"]


def scored(make_panel, stage_args, sites):
    args = stage_args(make_panel())
    panel = panel_file.load(args.panel)
    write_scan(args.out, panel, *scan_rows(panel), sites)
    assert score.main(args) == 0
    return args


def test_rank_reads_what_score_writes_and_the_same_scores_give_the_same_bytes(make_panel, stage_args):
    args = scored(make_panel, stage_args, {"northmart": ok_site(), "target": ok_site(seo=40.0), "gap": ok_site(seo=20.0),
                                           "voltcity": {"status": "blocked", "reason": "crawler_blocked"}})
    assert rank.main(args) == 0
    first = {name: (args.out / name).read_bytes() for name in (rank.FULL, rank.PUBLIC)}
    full, public = json.loads(first[rank.FULL]), json.loads(first[rank.PUBLIC])

    # Target and Gap are never named, so both AI rates are 0 and only the site score separates
    # them: 2060 / 74 and 1880 / 74. The zero-error range is far wider than that gap, so they share rank 2.
    assert [(r["rank_label"], r["name"], r["index"]) for r in public["rows"]] == \
        [("1", "Northmart", 91.1), ("2=", "Target", 27.8), ("2=", "Gap", 25.4)]
    assert public["leader"] == {"status": "leader", "brands": ["Northmart"]}
    assert public["rows"][0]["label"] == LABEL
    assert public["rate_only"] == []   # Volt City's rate is 0: a blocked brand nobody recommends is not named
    assert [r["name"] for r in full["unranked"]] == ["Volt City"]
    assert public["scores_sha256"] == store.file_sha256(args.out / score.SCORES)
    assert public["stability"] == {"stable": True, "needs_second_day": False}
    assert set(public) == {"panel_id", "panel_sha256", "protocol", "cpi_version", "engines", "scores_sha256",
                           "category", "market", "days", "keyword_pulls", "questions", "runs_per_day",
                           "signals_measured", "signals_not_measured", "warnings", "signal_sources",
                           "snapshot_sha256", "leader", "rows", "unpublished_sharing_last_rank", "rate_only", "stability",
                           "models"}

    assert rank.main(args) == 0
    assert {name: (args.out / name).read_bytes() for name in (rank.FULL, rank.PUBLIC)} == first


def test_main_refuses_scores_from_another_panel_or_none(make_panel, stage_args):
    args = stage_args(make_panel())
    with pytest.raises(store.StoreError, match="Run the score stage first"):
        rank.main(args)

    store.write_json(args.out / score.SCORES, scores_of(brand("A", 90)))
    with pytest.raises(store.StoreError, match="holds rows from another panel"):
        rank.main(args)
    assert not (args.out / rank.PUBLIC).exists()


def test_public_rates_are_not_rounded_before_the_page_rounds_them():
    # 0.63949 shows as 63.9% when rounded once. Rounded to 4 decimals first it became 0.6395, then 64.0%.
    _, public = rank.snapshots(scores_of(brand("A", 80, rec_rate=0.63949)), "hash")

    assert public["rows"][0]["rec_rate"] == 0.63949
    assert f"{public['rows'][0]['rec_rate'] * 100:.1f}" == "63.9"


def test_scores_written_before_models_were_recorded_ask_for_a_new_score():
    old = scores_of(brand("A", 80))
    del old["models"]
    with pytest.raises(store.StoreError, match="has no models.*run the score stage again"):
        rank.snapshots(old, "hash")
