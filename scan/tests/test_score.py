import json

import pytest
import yaml

import reference
from scan import panel as panel_file, reader, score, store
from scan.records import answer_id, mention_id

REC, CIT, ORGANIC = "rec.ai_recommendation", "rec.ai_citation", "rec.organic_search"


def number(question_id: str) -> int:
    return int(question_id[1:])


def answers_for(panel, invalid=(), unsourced=()):
    """One answer per question, engine and run. ``invalid`` and ``unsourced`` hold
    (engine, question id, run) keys."""
    day = panel.days[0]
    return [{"id": answer_id(day, engine, q.id, run), "panel_id": panel.panel_id, "panel_sha256": panel.sha256,
             "day": day, "question_id": q.id, "engine": engine, "run": run,
             "valid": (engine, q.id, run) not in invalid,
             "cited_urls": [] if (engine, q.id, run) in unsourced else ["https://news.example/story"]}
            for q, engine, run in panel.calls(day)]


def mentions_for(panel, answers, value=lambda a, brand_id: 0.0, cited=lambda a, brand_id: False):
    return [{"id": mention_id(a["id"], b.id), "panel_id": panel.panel_id, "panel_sha256": panel.sha256,
             "answer_id": a["id"], "brand_id": b.id, "value": value(a, b.id), "own_domain_cited": cited(a, b.id),
             "names_sha": b.names_sha, "reader_version": reader.VERSION}
            for a in answers if a["valid"] for b in panel.brands]


def serp_for(panel, pulls, first=lambda pull, i: None, keywords=8, volume=100):
    """``keywords`` rows per pull, each of the same volume. ``first(pull, i)`` is the host
    at position 1 for keyword ``i``, or None."""
    return [{"id": f"{pull}/kw{i}", "panel_id": panel.panel_id, "panel_sha256": panel.sha256, "pull": pull,
             "keyword": f"kw{i}", "volume": volume,
             "results": [{"position": 1, "url": f"https://{first(pull, i) or 'other.example'}/deals"},
                         {"position": 2, "url": "https://another.example/"}]}
            for pull in pulls for i in range(keywords)]


def ok_site(seo=80.0, parities=(1, 1), **summary):
    page = {"url": "https://site.example/", "status_code": 200, "counted": True, "excluded_reason": None,
            "indexable": True, "json_ld_types": ["Organization"], "json_ld_valid": True}
    return {
        "status": "ok", "reason": None, "read_at": "2026-11-21T10:00:00Z", "robots_verdicts": {"Googlebot": True},
        "pages": [{**page, "parity": p} for p in parities],
        "summary": {"access": 100, "indexable_share": 1.0, "org_with_name_url": True, "org_same_as": True,
                    "pagetype_share": 1.0, "valid_share": 1.0,
                    "social_urls": ["https://www.linkedin.com/company/northmart", "https://instagram.com/northmart",
                                    "https://x.com/northmart", "https://www.facebook.com/northmart"], **summary},
        "site_seo": {"score": seo, "source": "fake", "reason": None},
    }


# northmart: recommended on questions 1 to 15 (rate 0.5, score 90), own domain cited on
# questions 1 to 7 in both runs and on question 8 in run one (rate 7.5 / 30 = 0.25, score 90),
# first for 2 of 8 equal keywords in both weeks (visibility 0.25, score 90).
def all_ok(panel, **overrides):
    """Every brand's site read ok, so the site signals count for the scan. An override of
    None leaves that brand unread."""
    sites = {b.id: ok_site() for b in panel.brands}
    sites.update(overrides)
    return {bid: site for bid, site in sites.items() if site is not None}


def northmart_value(a, brand_id):
    return 1.0 if brand_id == "northmart" and number(a["question_id"]) <= 15 else 0.0


def northmart_cited(a, brand_id):
    q = number(a["question_id"])
    return brand_id == "northmart" and (q <= 7 or (q == 8 and a["run"] == 1))


def northmart_first(pull, i):
    return "www.northmart.example" if i < 2 else None


def scan_rows(panel, **answer_kwargs):
    answers = answers_for(panel, **answer_kwargs)
    return answers, mentions_for(panel, answers, northmart_value, northmart_cited), \
        serp_for(panel, panel.keyword_pulls, northmart_first)


@pytest.fixture
def panel(make_panel):
    return panel_file.load(make_panel())


def test_the_index_range_is_centred_on_the_unrounded_index():
    # Stored scores have one decimal, but their weighted mean need not: here it is 64.04.
    signals = {sid: {"state": reference.LIVE, "score": 64.04, "se": 0.0} for sid in reference.REGISTRY}
    signals["rec.ai_recommendation"]["se"] = 2.0
    b = score.assemble(signals)

    # The error is 0.24 * 2.0, so the range is 0.94 either side. Centred on 64.04 the upper end is 65.0.
    # Centred on the rounded 64.0 it would be 64.9.
    lo, hi = b["raw_index"] - 1.96 * b["se"], b["raw_index"] + 1.96 * b["se"]
    assert b["index"] == 64.0
    assert b["range"] == [round(lo, 1), round(hi, 1)] == [63.1, 65.0]


def test_seven_live_signals_are_74_percent_provisional_and_the_other_three_are_not_measured(panel):
    answers, mentions, serp = scan_rows(panel)
    scores = score.score_scan(panel, answers, mentions, serp, all_ok(panel))
    b = scores["brands"]["northmart"]

    assert b["coverage"] == 0.74
    assert b["status"] == "provisional"
    assert b["label"] == "CLEO Presence Index, public scan, CPI v1.0, 74% of method measured"
    assert b["fully_measured"] is True
    assert {sid: b["signals"][sid]["score"] for sid in score.PROTOCOL_SIGNALS} == {
        REC: 90.0, CIT: 90.0, ORGANIC: 90.0,
        "rdy.crawl_access": 100.0, "rdy.site_seo": 80.0, "rdy.social": 100.0, "rdy.structured_data": 100.0}
    # (24 + 14 + 10) * 90 + 9 * 100 + 9 * 80 + 6 * 100 + 2 * 100 = 6740, over 74.
    assert b["index"] == 91.1
    assert b["raw_index"] == pytest.approx(6740 / 74)
    assert b["range"][0] < 91.1 < b["range"][1]
    assert b["raw_range"] == [b["raw_index"] - 1.96 * b["se"], b["raw_index"] + 1.96 * b["se"]]
    assert (b["rec_rate"], b["cit_rate"]) == (pytest.approx(0.5), pytest.approx(0.25))
    for sid in ("rec.reviews", "rec.independent_mentions", "rdy.local_listings"):
        assert (b["signals"][sid]["state"], b["signals"][sid]["reason"], b["signals"][sid]["score"]) == \
            ("not_measured", "not_in_protocol", None)
    assert "grade" not in json.dumps(scores)


def test_index_can_be_recomputed_from_the_stored_signal_scores_and_errors(panel):
    answers, mentions, serp = scan_rows(panel)
    b = score.score_scan(panel, answers, mentions, serp, all_ok(panel, northmart=ok_site(seo=80.04)))["brands"]["northmart"]

    weights = {REC: 24, ORGANIC: 14, CIT: 10, "rdy.crawl_access": 9, "rdy.site_seo": 9, "rdy.social": 6, "rdy.structured_data": 2}
    assert b["signals"]["rdy.site_seo"]["score"] == 80.0
    assert b["raw_index"] == pytest.approx(6740 / 74, rel=1e-12)
    stored = {sid: b["signals"][sid]["se"] for sid in weights}
    assert stored[REC] > 0
    assert b["se"] == pytest.approx(sum((w / 74) ** 2 * stored[sid] ** 2 for sid, w in weights.items()) ** 0.5, rel=1e-12)


def test_blocked_or_unread_site_lowers_coverage_and_is_never_a_zero(panel):
    answers, mentions, serp = scan_rows(panel)
    blocked = {"status": "blocked", "reason": "crawler_blocked", "read_at": "2026-11-21T10:00:00Z"}
    scores = score.score_scan(panel, answers, mentions, serp, all_ok(panel, northmart=blocked, target=None))

    assert scores["brands"]["gap"]["fully_measured"] is True
    b = scores["brands"]["northmart"]
    assert (b["coverage"], b["status"], b["index"], b["range"]) == (0.48, "unrated", None, None)
    assert b["fully_measured"] is False
    assert b["label"] == "CLEO Presence Index, public scan, CPI v1.0, 48% of method measured"
    for sid in score.READINESS:
        assert (b["signals"][sid]["state"], b["signals"][sid]["score"], b["signals"][sid]["reason"]) == \
            ("not_measured", None, "crawler_blocked")
    unread = scores["brands"]["target"]
    assert unread["coverage"] == 0.48
    assert unread["signals"]["rdy.site_seo"] == {"state": "not_measured", "score": None, "se": 0.0, "raw": None,
                                                  "raw_range": None, "reason": "site_not_read"}


def test_engine_counts_from_60_valid_answers_not_59(panel):
    answers = answers_for(panel, invalid={("google_ai", "q01", 1)})
    scores = score.score_scan(panel, answers, mentions_for(panel, answers), [], {})

    assert scores["engines"]["google_ai"] == {
        "weight": 0.25, "asked": 60, "valid": 59, "questions": 30, "counts_for_recommendation": False,
        "with_sources": 59, "questions_with_sources": 30, "counts_for_citation": False}
    assert (scores["engines"]["chatgpt"]["valid"], scores["engines"]["chatgpt"]["counts_for_recommendation"]) == (60, True)
    assert scores["brands"]["northmart"]["engines"]["google_ai"] == {"rate": 0.0, "questions": 30, "valid": 59, "counts": False}


def test_engine_needs_10_questions_as_well_as_60_answers():
    assert score.engine_counts(answers=60, questions=10) is True
    assert score.engine_counts(answers=60, questions=9) is False


def chatgpt_only(a, brand_id):
    return 1.0 if brand_id == "northmart" and a["engine"] == "chatgpt" and number(a["question_id"]) <= 15 else 0.0


def test_signal_is_live_when_counted_engines_carry_half_the_weight_of_the_panels_engines(panel):
    # chatgpt 0.41 + claude 0.08 = 0.49 of the panel's 0.95. Of all five engines it would be
    # 0.49 and the signal would be warming up.
    answers = answers_for(panel, invalid={("google_ai", "q01", 1), ("gemini", "q01", 1)})
    scores = score.score_scan(panel, answers, mentions_for(panel, answers, chatgpt_only), [], {})

    rec = scores["brands"]["northmart"]["signals"][REC]
    assert rec["state"] == "live"
    assert rec["raw"] == pytest.approx(0.41 * 0.5 / 0.49)


def test_signal_is_warming_when_counted_engines_carry_under_half_the_weight(panel):
    # gemini 0.21 + claude 0.08 = 0.29 of 0.95.
    answers = answers_for(panel, invalid={("chatgpt", "q01", 1), ("google_ai", "q01", 1)})
    scores = score.score_scan(panel, answers, mentions_for(panel, answers, chatgpt_only), [], {})

    b = scores["brands"]["northmart"]
    assert (b["signals"][REC]["state"], b["signals"][REC]["reason"], b["signals"][REC]["score"]) == \
        ("warming_up", "not_enough_answers", None)
    assert b["rec_rate"] is None


def test_citation_counts_only_answers_with_sources(make_panel):
    panel = panel_file.load(make_panel(questions=31))
    # chatgpt's second answer to q01 cites nothing, so q01 is the one sourced answer: value 1, not 0.5.
    answers = answers_for(panel, unsourced={("chatgpt", "q01", 2)})
    cited = lambda a, brand_id: brand_id == "northmart" and a["engine"] == "chatgpt" and a["question_id"] == "q01"
    scores = score.score_scan(panel, answers, mentions_for(panel, answers, cited=cited), [], {})

    assert (scores["engines"]["chatgpt"]["with_sources"], scores["engines"]["chatgpt"]["counts_for_citation"]) == (61, True)
    assert scores["brands"]["northmart"]["cit_rate"] == pytest.approx(0.41 / 0.95 / 31)


def test_engine_with_59_sourced_answers_counts_for_recommendation_but_not_citation(panel):
    answers = answers_for(panel, unsourced={("claude", "q01", 1)})
    cited = lambda a, brand_id: brand_id == "northmart" and a["engine"] == "claude"
    scores = score.score_scan(panel, answers, mentions_for(panel, answers, cited=cited), [], {})

    claude = scores["engines"]["claude"]
    assert (claude["valid"], claude["counts_for_recommendation"]) == (60, True)
    assert (claude["with_sources"], claude["counts_for_citation"]) == (59, False)
    # With claude counted the rate would be 0.08 / 0.95.
    assert scores["brands"]["northmart"]["signals"][CIT]["state"] == "live"
    assert scores["brands"]["northmart"]["cit_rate"] == 0.0


def test_missing_mention_row_raises_and_is_never_read_as_zero(panel):
    answers = answers_for(panel)
    mentions = mentions_for(panel, answers)

    with pytest.raises(score.ScoreError, match=r"missing 1 of the 960 rows.*parse stage"):
        score.score_scan(panel, answers, mentions[1:], [], {})
    with pytest.raises(score.ScoreError, match=r"more than one row for 1 \(answer, brand\) pairs"):
        score.score_scan(panel, answers, mentions + mentions[:1], [], {})


def test_second_keyword_pull_in_the_same_iso_week_leaves_organic_warming(panel):
    answers = answers_for(panel)
    mentions = mentions_for(panel, answers)

    def organic(pulls):
        scores = score.score_scan(panel, answers, mentions, serp_for(panel, pulls, northmart_first), {})
        return scores["brands"]["northmart"]["signals"][ORGANIC]

    # 2026-11-21 is a Saturday and 2026-11-22 the Sunday of the same ISO week; the 23rd is the next Monday.
    same_week = organic(["2026-11-21", "2026-11-22"])
    assert (same_week["state"], same_week["reason"], same_week["score"]) == ("warming_up", "not_enough_weeks", None)
    two_weeks = organic(["2026-11-21", "2026-11-23"])
    assert (two_weeks["state"], two_weeks["score"], two_weeks["se"]) == ("live", 90.0, 0.0)
    assert two_weeks["raw"] == pytest.approx(0.25)
    assert organic([])["state"] == "not_measured"


def test_organic_uses_each_weeks_latest_pull_owned_urls_and_the_volume_floor():
    def row(pull, keyword, volume, results):
        return {"pull": pull, "keyword": keyword, "volume": volume,
                "results": [{"position": p, "url": u} for p, u in results]}

    weeks = score.organic_weeks([
        row("2026-11-16", "a", 100, [(1, "https://voltcity.example/")]),
        # The later pull of the same week replaces the one above. Volume 0 counts as 10.
        row("2026-11-21", "a", 0, [(1, "https://voltcity.example/")]),
        row("2026-11-21", "b", 30, [(1, "https://other.example/"), (11, "https://voltcity.example/")]),
        # A subdomain of the brand's second domain is owned; the best owned position counts.
        row("2026-11-23", "a", 100, [(3, "https://shop.volt.example/tv"), (7, "https://voltcity.example/")]),
    ], ("voltcity.example", "volt.example"))

    assert [(pull, n) for pull, _, n in weeks] == [("2026-11-21", 2), ("2026-11-23", 1)]
    assert weeks[0][1] == pytest.approx(10 / 40)
    assert weeks[1][1] == pytest.approx(6.02 / 19.38)


def test_organic_is_live_from_two_weeks_of_the_whole_keyword_panel_however_small():
    # The panel example has three keywords. Appendix D sets no minimum count beyond the whole panel.
    weeks = [("2026-11-21", 0.25, 3), ("2026-11-23", 0.25, 3)]
    assert score.organic_signal(weeks, 3)["score"] == 90.0
    assert score.organic_signal(weeks[:1], 3)["reason"] == "not_enough_weeks"


def test_a_half_finished_keyword_pull_is_not_an_observation():
    weeks = [("2026-11-21", 0.25, 100), ("2026-11-23", 0.25, 62)]
    signal = score.organic_signal(weeks, 100)
    assert (signal["state"], signal["reason"], signal["score"]) == ("warming_up", "keyword_pull_incomplete", None)


def test_zero_error_rule_puts_the_range_on_the_number_of_questions(make_panel):
    panel = panel_file.load(make_panel(questions=60))
    answers = answers_for(panel)
    scores = score.score_scan(panel, answers, mentions_for(panel, answers), [], {})

    b = scores["brands"]["northmart"]
    assert b["signals"][REC]["state"] == "live"
    assert b["rec_rate"] == 0.0
    assert b["rec_rate_range"] == [0.0, 0.0602]
    # The curve scores 0.0602 at 16.5, so the score error is 16.5 / 3.92.
    assert (b["signals"][REC]["score"], b["signals"][REC]["se"]) == (0.0, pytest.approx(4.221, abs=0.0005))


def test_rate_with_an_error_keeps_the_effective_sample_range_not_the_zero_error_rule():
    weights = {"chatgpt": 0.41, "claude": 0.08}
    values = {"chatgpt": [1.0] * 15 + [0.0] * 15, "claude": [1.0] * 15 + [0.0] * 15}
    m = score.ai_measure(values, weights, {"chatgpt", "claude"}, (0.15, 0.50), 30)

    # SE^2 = 0.25 / 30 * (0.41^2 + 0.08^2) / 0.49^2, so n* = 0.25 / SE^2 = 30 * 0.2401 / 0.1745.
    assert m["rate"] == pytest.approx(0.5)
    assert reference.effective_n(m["rate"], m["rate_se"]) == pytest.approx(30 * 0.2401 / 0.1745)
    assert (m["low"], m["high"]) == pytest.approx(reference.wilson(0.5 * 41.278, 41.278), abs=1e-4)


def test_each_run_is_scored_on_its_own_answers(panel):
    answers = answers_for(panel)
    run_one = lambda a, brand_id: 1.0 if brand_id == "northmart" and a["run"] == 1 and number(a["question_id"]) <= 15 else 0.0
    scores = score.score_scan(panel, answers, mentions_for(panel, answers, run_one), [], {})

    assert scores["brands"]["northmart"]["rec_rate"] == pytest.approx(0.25)
    assert scores["runs"]["1"]["northmart"]["rec_rate"] == pytest.approx(0.5)
    assert scores["runs"]["2"]["northmart"]["rec_rate"] == 0.0
    # Run two has two live signals, both scoring 0. Run one has AI Recommendation at 90: 24 * 90 / 34.
    assert scores["runs"]["2"]["northmart"]["raw_index"] == 0.0
    assert scores["runs"]["1"]["northmart"]["raw_index"] == pytest.approx(24 * 90 / 34)


def test_runs_compare_only_pairs_answered_in_every_run(make_panel):
    panel = panel_file.load(make_panel(questions=31))
    # chatgpt has 61 valid answers, so it still counts. Its q01 has no answer in run two.
    answers = answers_for(panel, invalid={("chatgpt", "q01", 2)})
    q01 = lambda a, brand_id: 1.0 if brand_id == "northmart" and a["question_id"] == "q01" else 0.0
    scores = score.score_scan(panel, answers, mentions_for(panel, answers, q01), [], {})

    assert scores["engines"]["chatgpt"]["counts_for_recommendation"] is True
    assert scores["brands"]["northmart"]["rec_rate"] == pytest.approx(1 / 31)
    # In both runs chatgpt is scored on the other 30 questions (rate 0) and the rest on all 31.
    paired = (0.25 + 0.08 + 0.21) / 0.95 / 31
    assert scores["runs"]["1"]["northmart"]["rec_rate"] == pytest.approx(paired)
    assert scores["runs"]["2"]["northmart"]["rec_rate"] == pytest.approx(paired)
    assert scores["runs"]["1"]["northmart"]["raw_index"] == pytest.approx(scores["runs"]["2"]["northmart"]["raw_index"])


def test_readiness_scores_come_from_the_site_summary():
    site = ok_site(seo=None, parities=(1, 0, None), access=50, indexable_share=0.5, org_same_as=False,
                   pagetype_share=0.5, valid_share=0.5,
                   social_urls=["linkedin.com/company/acme", "https://twitter.com/acme", "https://youtube.com/@acme"])
    site["pages"].append({**site["pages"][0], "counted": False, "excluded_reason": "redirect", "parity": 0})
    site["site_seo"]["reason"] = "pagespeed_failed"
    signals = score.readiness_signals(site)

    # 0.40 * 50 + 35 * mean(1, 0) + 25 * 0.5; 25 + 0 + 30 * 0.5 + 20 * 0.5; linkedin and x of four.
    assert {sid: (s["state"], s["score"]) for sid, s in signals.items()} == {
        "rdy.crawl_access": ("live", 50.0), "rdy.site_seo": ("not_measured", None),
        "rdy.social": ("live", 50.0), "rdy.structured_data": ("live", 50.0)}
    assert signals["rdy.site_seo"]["reason"] == "pagespeed_failed"


def test_crawl_access_without_a_parity_is_not_measured():
    signals = score.readiness_signals(ok_site(parities=(None,)))
    assert (signals["rdy.crawl_access"]["state"], signals["rdy.crawl_access"]["score"]) == ("not_measured", None)
    assert signals["rdy.structured_data"]["score"] == 100.0


@pytest.mark.parametrize("url, platform", [
    ("https://www.linkedin.com/company/acme", "linkedin"),
    ("https://www.linkedin.com/in/someone", None),
    ("https://www.linkedin.com/company", None),
    ("instagram.com/acme", "instagram"),
    ("https://twitter.com/acme", "x"),
    ("https://x.com/acme", "x"),
    ("https://m.facebook.com/acme", "facebook"),
    ("https://facebook.com/", None),
    ("https://www.youtube.com/@acme", "youtube"),
    ("https://www.youtube.com/watch", None),
    ("https://www.tiktok.com/@acme", "tiktok"),
    ("https://acme.example/about", None),
    # A host that only ends in the letters of a platform's domain is not that platform.
    ("https://www.netflix.com/acme", None),
    ("https://www.dropbox.com/acme", None),
    ("https://notfacebook.com/acme", None),
    # A share button or a post is not a profile.
    ("https://www.facebook.com/sharer/sharer.php?u=https://acme.example", None),
    ("https://twitter.com/intent/tweet?text=hello", None),
    ("https://x.com/share?url=https://acme.example", None),
    ("https://www.instagram.com/p/Cabc123/", None),
    ("https://[broken/acme", None),
])
def test_social_platform(url, platform):
    assert score.social_platform(url) == platform


def write_scan(out, panel, answers, mentions, serp, sites):
    for name, rows in ((score.ANSWERS, answers), (score.MENTIONS, mentions), (score.SERP, serp)):
        for row in rows:
            store.append_jsonl(out / name, row)
    store.write_json(out / score.SITES, {"panel_id": panel.panel_id, "panel_sha256": panel.sha256, "brands": sites})


def test_main_writes_scores_json_with_the_brand_a_person_added(make_panel, stage_args):
    args = stage_args(make_panel())
    (args.out / score.ADDITIONS).write_text(yaml.safe_dump({"brands": [
        {"id": "bestco", "name": "Best Co", "domain": "bestco.example", "confirmed_by": "tester"}]}), encoding="utf-8")
    panel = panel_file.with_additions(panel_file.load(args.panel), args.out / score.ADDITIONS)
    write_scan(args.out, panel, *scan_rows(panel), all_ok(panel))

    assert score.main(args) == 0
    first = (args.out / score.SCORES).read_bytes()
    scores = json.loads(first)

    assert set(scores) == {"panel_id", "panel_sha256", "protocol", "cpi_version", "inputs", "thresholds", "engines", "models",
                           "brands", "runs", "category", "market", "days", "keyword_pulls", "questions",
                           "runs_per_day", "signals_measured", "signals_not_measured", "warnings",
                           "signal_sources"}
    assert scores["signal_sources"] == {}
    assert scores["signals_measured"] == list(score.PROTOCOL_SIGNALS)
    assert scores["signals_not_measured"] == ["rec.reviews", "rec.independent_mentions", "rdy.local_listings"]
    assert (scores["panel_id"], scores["protocol"], scores["cpi_version"]) == ("test-retailers-us-en", "PS-1.0", "1.0.0")
    assert scores["panel_sha256"] == panel.sha256
    assert scores["thresholds"] == {"ai_min_answers_per_engine": 60, "ai_min_questions_per_engine": 10,
                                    "ai_min_weight_share": 0.5, "organic_min_weeks": 2,
                                    "organic_floor_volume": 10}
    assert set(scores["inputs"]) == {"answers.jsonl", "mentions.jsonl", "serp.jsonl", "sites.json", "additions.yaml"}
    assert scores["inputs"]["mentions.jsonl"] == store.file_sha256(args.out / "mentions.jsonl")
    assert set(scores["engines"]) == {"chatgpt", "claude", "gemini", "google_ai"}
    assert scores["models"]["readers"] == {"first": "fake", "second": "fake", "tiebreak": "fake"}
    assert scores["models"]["reader_version"] == [reader.VERSION]
    assert scores["brands"]["northmart"]["index"] == 91.1
    assert (scores["brands"]["northmart"]["source"], scores["brands"]["bestco"]["source"]) == ("list", "ai_named")
    assert (scores["brands"]["bestco"]["name"], scores["brands"]["bestco"]["domain"]) == ("Best Co", "bestco.example")
    assert set(scores["runs"]) == {"1", "2"}
    assert set(scores["runs"]["1"]) == {"northmart", "target", "gap", "voltcity", "bestco"}
    assert "grade" not in first.decode("utf-8")

    assert score.main(args) == 0
    assert (args.out / score.SCORES).read_bytes() == first


def test_main_refuses_rows_from_another_panel(make_panel, stage_args):
    args = stage_args(make_panel())
    panel = panel_file.load(args.panel)
    answers, mentions, serp = scan_rows(panel)
    mentions[0]["panel_sha256"] = "0" * 64
    write_scan(args.out, panel, answers, mentions, serp, {})

    with pytest.raises(store.StoreError, match="mentions.jsonl holds rows from another panel"):
        score.main(args)
    assert not (args.out / score.SCORES).exists()


# ---------- reviews, independent mentions and local listings ----------
def supplements_for(panel, **rows_by_signal):
    """Every brand gets the same row for each signal named, unless a dict of brand rows is given."""
    return {sid: rows if set(rows) >= {b.id for b in panel.brands} else {b.id: rows for b in panel.brands}
            for sid, rows in rows_by_signal.items()}


REVIEWED = {"status": "ok", "n": 400, "mean_rating": 4.5}
MENTIONED = {"status": "ok", "mass": 60.0}
LISTED = {"status": "ok", "directories": {"google": {"listed": True}}, "presence_share": 0.6, "nap_share": 1.0,
          "hours_category_share": 0.0}


def test_the_three_other_signals_count_when_their_stages_ran(panel):
    answers, mentions, serp = scan_rows(panel)
    extra = supplements_for(panel, **{"rec.reviews": REVIEWED, "rec.independent_mentions": MENTIONED,
                                      "rdy.local_listings": LISTED})
    scores = score.score_scan(panel, answers, mentions, serp, all_ok(panel), extra)
    b = scores["brands"]["northmart"]

    assert scores["signals_measured"] == [*score.PROTOCOL_SIGNALS, *score.NOT_IN_PROTOCOL]
    assert scores["signals_not_measured"] == []
    assert b["coverage"] == 1.0 and b["fully_measured"] is True
    assert b["label"] == "CLEO Presence Index, public scan, CPI v1.0, 100% of method measured"
    assert b["signals"]["rec.reviews"] == {**b["signals"]["rec.reviews"], "state": "live", "raw": 400,
                                           "score": round(reference.reviews_score(400, 4.5), 1)}
    assert (b["signals"]["rec.independent_mentions"]["score"], b["signals"]["rec.independent_mentions"]["raw"]) == (90.0, 60.0)
    assert (b["signals"]["rdy.local_listings"]["score"], b["signals"]["rdy.local_listings"]["raw"]) == (64.0, 0.6)


def test_a_public_scan_score_stays_provisional_at_full_coverage(panel):
    answers, mentions, serp = scan_rows(panel)
    extra = supplements_for(panel, **{"rec.reviews": REVIEWED, "rec.independent_mentions": MENTIONED,
                                      "rdy.local_listings": LISTED})
    scores = score.score_scan(panel, answers, mentions, serp, all_ok(panel), extra)
    assert {b["status"] for b in scores["brands"].values()} == {"provisional"}


def test_a_stage_that_did_not_run_leaves_its_signal_out_of_the_protocol(panel):
    answers, mentions, serp = scan_rows(panel)
    scores = score.score_scan(panel, answers, mentions, serp, all_ok(panel),
                              supplements_for(panel, **{"rec.reviews": REVIEWED}))
    b = scores["brands"]["northmart"]
    assert scores["signals_measured"] == [*score.PROTOCOL_SIGNALS, "rec.reviews"]
    assert b["coverage"] == 0.86
    assert b["signals"]["rec.independent_mentions"]["reason"] == "not_in_protocol"


def test_a_profile_with_no_reviews_scores_zero_and_a_missing_profile_is_not_measured(panel):
    answers, mentions, serp = scan_rows(panel)
    rows = {b.id: REVIEWED for b in panel.brands}
    rows["gap"] = {"status": "ok", "n": 0, "mean_rating": None}
    rows["target"] = {"status": "no_profile", "reason": "no_trustpilot_profile", "n": 0, "mean_rating": None}
    scores = score.score_scan(panel, answers, mentions, serp, all_ok(panel), {"rec.reviews": rows})
    gap, target = scores["brands"]["gap"], scores["brands"]["target"]
    assert (gap["signals"]["rec.reviews"]["state"], gap["signals"]["rec.reviews"]["score"]) == ("live", 0.0)
    assert gap["fully_measured"] is True
    assert (target["signals"]["rec.reviews"]["state"], target["signals"]["rec.reviews"]["reason"]) \
        == ("not_measured", "no_trustpilot_profile")
    assert target["fully_measured"] is False


def test_local_listings_does_not_apply_to_a_brand_with_no_store(panel):
    from dataclasses import replace
    online = replace(panel, brands=tuple(replace(b, local=b.id != "voltcity") for b in panel.brands))
    answers, mentions, serp = scan_rows(online)
    rows = {b.id: LISTED for b in online.brands if b.id != "voltcity"}
    scores = score.score_scan(online, answers, mentions, serp, all_ok(online), {"rdy.local_listings": rows})
    volt, north = scores["brands"]["voltcity"], scores["brands"]["northmart"]

    assert "rdy.local_listings" in scores["signals_measured"]
    assert (volt["signals"]["rdy.local_listings"]["state"], volt["signals"]["rdy.local_listings"]["reason"]) \
        == ("not_applicable", "no_location")
    assert volt["fully_measured"] is True
    assert north["coverage"] == 0.78
    assert volt["coverage"] == 0.771   # 74 of the 96 points that apply to it


def test_the_sources_a_stage_read_are_named_for_the_published_ranking():
    assert score.supplement_sources("rec.reviews", {"brands": {
        "a": {"platforms": [{"platform": "trustpilot"}]}, "b": {"platforms": []}}}) == ["trustpilot"]
    assert score.supplement_sources("rec.independent_mentions", {"brands": {}, "sources": {
        "reddit": {"ran": True}, "quora": {"ran": False}, "ai_citations": {"ran": True}}}) == ["ai_citations", "reddit"]
    assert score.supplement_sources("rdy.local_listings", {"brands": {
        "a": {"directories": {"google": {"listed": True}}}, "b": {"directories": {}}}}) == ["google"]
