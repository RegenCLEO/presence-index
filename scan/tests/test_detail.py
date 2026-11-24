import json

import pytest

from scan import detail, panel as panel_file, rank, score, store
from scan.tests.test_score import all_ok, ok_site, scan_rows, write_scan


@pytest.fixture
def scan(make_panel, stage_args):
    """A scored and ranked scan on disk, and the arguments of its stages."""
    args = stage_args(make_panel())
    panel = panel_file.load(args.panel)
    write_scan(args.out, panel, *scan_rows(panel), all_ok(panel))
    assert score.main(args) == 0 and rank.main(args) == 0
    return args, panel


def written(args):
    return json.loads((args.out / detail.DETAIL).read_text(encoding="utf-8"))


def test_the_file_holds_the_public_rows_and_the_signals_the_scores_hold(scan):
    args, _ = scan
    assert detail.main(args) == 0
    body, scores = written(args), store.read_json(args.out / score.SCORES)
    public = store.read_json(args.out / rank.PUBLIC)

    assert [b["domain"] for b in body["brands"]] == [r["domain"] for r in public["rows"]]
    assert (body["snapshot_sha256"], body["scores_sha256"]) == (public["snapshot_sha256"], public["scores_sha256"])
    by_domain = {b["domain"]: b for b in scores["brands"].values()}
    for brand in body["brands"]:
        stored = by_domain[brand["domain"]]["signals"]
        assert brand["signals"] == {sid: {k: stored[sid][k] for k in detail.SIGNAL_KEYS} for sid in body["weights"]}


def test_a_published_index_is_the_weighted_mean_of_its_live_signal_scores(scan):
    args, _ = scan
    detail.main(args)
    body = written(args)
    rows = {r["domain"]: r for r in store.read_json(args.out / rank.PUBLIC)["rows"]}
    for brand in body["brands"]:
        live = {sid: s for sid, s in brand["signals"].items() if s["state"] == "live"}
        weight = sum(body["weights"][sid] for sid in live)
        index = sum(body["weights"][sid] * s["score"] for sid, s in live.items()) / weight
        assert round(index, 1) == rows[brand["domain"]]["index"]


def test_keywords_come_largest_first_with_shares_that_add_up_to_one(scan):
    args, panel = scan
    detail.main(args)
    keywords = written(args)["keywords"]
    assert len(keywords) == len(panel.keywords)
    assert [k["volume"] for k in keywords] == sorted((k["volume"] for k in keywords), reverse=True)
    assert sum(k["share"] for k in keywords) == pytest.approx(1.0, abs=1e-4)


def test_the_weekly_visibilities_are_the_ones_the_organic_score_was_made_from(scan):
    args, _ = scan
    detail.main(args)
    northmart = next(b for b in written(args)["brands"] if b["domain"].startswith("northmart"))
    weeks = northmart["evidence"]["organic"]["weeks"]
    # northmart is first for 2 of 8 equal keywords in both weeks.
    assert [(w["keywords"], w["keywords_in_top_10"], w["visibility"]) for w in weeks] == [(8, 2, 0.25), (8, 2, 0.25)]
    assert northmart["signals"]["rec.organic_search"]["raw"] == pytest.approx(0.25)


def test_the_same_scan_gives_the_same_bytes(scan):
    args, _ = scan
    detail.main(args)
    first = (args.out / detail.DETAIL).read_bytes()
    detail.main(args)
    assert (args.out / detail.DETAIL).read_bytes() == first


def test_a_snapshot_from_other_scores_is_refused(scan):
    args, _ = scan
    public = store.read_json(args.out / rank.PUBLIC)
    store.write_json(args.out / rank.PUBLIC, {**public, "scores_sha256": "0" * 64})
    with pytest.raises(store.StoreError, match="Run the rank stage again"):
        detail.main(args)


def test_an_input_changed_after_scoring_is_refused(scan):
    args, _ = scan
    with (args.out / score.SERP).open("a", encoding="utf-8") as handle:
        handle.write("\n")
    with pytest.raises(store.StoreError, match="changed after"):
        detail.main(args)


def test_site_evidence_names_the_facts_behind_the_four_site_signals():
    found = detail.site_evidence(ok_site(parities=(1, 0), org_same_as=False, pagetype_share=0.5))
    assert (found["pages_read"], found["content_in_html_share"], found["read_on"]) == (2, 0.5, "2026-11-21")
    assert found["social_platforms"] == ["facebook", "instagram", "linkedin", "x"]
    assert (found["org_markup"], found["org_same_as"], found["page_type_markup_share"]) == (True, False, 0.5)
    assert detail.site_evidence({"status": "blocked", "reason": "crawler_blocked"}) is None
    assert detail.site_evidence(None) is None


def test_mention_evidence_counts_what_was_found_and_what_counts():
    row = {"status": "ok", "mass": 3.14159, "fetched_at": "2026-11-22T11:00:00Z",
           "counts": {"reddit": 3, "youtube": 1},
           "mentions": [{"source": "reddit", "eligible": True}, {"source": "reddit", "eligible": False},
                        {"source": "youtube", "eligible": True}]}
    assert detail.mentions_evidence(row) == {"read_on": "2026-11-22", "mass": 3.14, "sources": {
        "reddit": {"found": 3, "counted": 1}, "youtube": {"found": 1, "counted": 1}}}
    assert detail.mentions_evidence({"status": "failed", "reason": "no_source_ran"}) is None


def test_review_and_local_evidence_copy_the_stored_reading():
    reviews = {"status": "ok", "n": 73, "mean_rating": 1.6, "fetched_at": "2026-11-22T10:00:00Z",
               "platforms": [{"platform": "trustpilot", "profile": "shop.example", "count": 73, "rating": 1.6}]}
    assert detail.reviews_evidence(reviews) == {"read_on": "2026-11-22", "reviews": 73, "mean_rating": 1.6, "profiles": [
        {"platform": "trustpilot", "profile": "shop.example", "reviews": 73, "rating": 1.6}]}
    assert detail.reviews_evidence({"status": "no_profile"}) is None

    local = {"status": "ok", "fetched_at": "2026-11-22T10:00:00Z", "directories": {"google": {"listed": True}},
             "listing": {"title": "Shop", "category": "Department store", "has_hours": True, "address": "1 Road"},
             "nap_check": {"compared": False}, "presence_share": 1.0, "nap_share": 1.0, "hours_category_share": 1.0}
    found = detail.local_evidence(local)
    assert (found["listing"], found["category"], found["has_hours"], found["addresses_compared"]) == \
        ("Shop", "Department store", True, False)
    assert "address" not in found
    assert detail.local_evidence({"status": "not_applicable"}) is None
