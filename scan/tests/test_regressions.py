"""One test per bug the independent review reproduced before the first live run."""
import os
from datetime import datetime, timezone

import pytest
import yaml

from scan import keywords, panel as panel_file, parse, reader, run, siteread, siteseo, store
from scan.engines import google_aio
from scan.panel import Brand, PanelError
from scan.records import EngineError, EngineResult, host_of
from scan.siteread import Response
from scan.tests.test_parse import STUB_READERS, Stub, mentions, parse_args, write_answers

DAY = "2026-11-21"
HTML = {"content-type": "text/html; charset=utf-8"}


def run_args(stage_args, panel_path, **kw):
    return stage_args(panel_path, **{"day": None, "go": True, "workers": 4, "attempts": 2, "limit": None,
                                     "any_day": True, **kw})


# ---------- paid data is never lost ----------
def test_a_value_json_cannot_hold_is_stored_as_text_not_dropped(tmp_path):
    store.append_jsonl(tmp_path / "rows.jsonl", {"id": "a", "raw": {"expires_at": datetime(2026, 11, 21, tzinfo=timezone.utc)}})
    assert store.read_jsonl(tmp_path / "rows.jsonl") == [{"id": "a", "raw": {"expires_at": "2026-11-21 00:00:00+00:00"}}]


def test_an_adapter_bug_fails_one_call_and_every_other_answer_is_stored(make_panel, stage_args, monkeypatch):
    args = run_args(stage_args, make_panel(questions=3))
    real = run.engines.ask

    def buggy(engine, question, market, config):
        if engine == "gemini" and "item 1" in question:
            raise KeyError("candidates")
        return real(engine, question, market, config)

    monkeypatch.setattr(run.engines, "ask", buggy)
    assert run.main(args) == 1
    assert len(store.read_jsonl(args.out / "answers.jsonl")) == 22
    assert {e["error"] for e in store.read_jsonl(args.out / "errors.jsonl")} == {"KeyError: 'candidates'"}


def test_a_billed_call_that_fails_still_reaches_the_cost_log(make_panel, stage_args, monkeypatch):
    args = run_args(stage_args, make_panel(questions=1, runs=1), attempts=2)
    real = run.engines.ask
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    tries = []

    def half_loaded(engine, question, market, config):
        if engine == "google_ai" and not tries:
            tries.append(1)
            raise EngineError("the AI Overview came back with no text", cost_usd=0.004)
        return real(engine, question, market, config)

    monkeypatch.setattr(run.engines, "ask", half_loaded)
    assert run.main(args) == 0
    costs = {c["ref"]: c["cost_usd"] for c in store.read_jsonl(args.out / "cost_log.jsonl")}
    assert costs[f"{DAY}/google_ai/q01/r1#failed"] == 0.004
    assert len(costs) == 5


def test_an_ai_overview_with_no_text_is_retried_not_stored_as_missing(monkeypatch):
    raw = {"status_code": 20000, "tasks": [{"status_code": 20000, "cost": 0.004, "result": [{"items": [
        {"type": "ai_overview", "items": [], "references": []}]}]}]}
    assert google_aio.parse(raw) == ("", [], 0.004, True)


def test_a_reader_reply_with_a_malformed_id_is_a_failed_read_not_a_crash():
    with pytest.raises(reader.ReadError, match="not asked"):
        reader.parse_reply({"brands": [{"id": ["gap"], "classification": "MENTIONED", "polarity": "neutral",
                                        "position": 1}], "other_businesses": []}, ["gap"])


def test_two_stages_cannot_write_one_directory_and_a_dead_stages_lock_is_free(tmp_path):
    (tmp_path / ".lock").write_text("999999999")  # left by a stage that died: no process holds it
    with store.exclusive(tmp_path):
        assert (tmp_path / ".lock").read_text() == str(os.getpid())
        with pytest.raises(store.StoreError, match="another scan stage"):
            with store.exclusive(tmp_path):
                pass
    with store.exclusive(tmp_path):
        pass


def test_a_torn_cost_row_is_repaired_before_the_next_stage_appends(tmp_path):
    common = dict(panel_id="p", day="2026-11-21", purpose="answer", vendor="dataforseo")
    store.log_cost(tmp_path, ref="1", cost_usd=0.004, **common)
    with open(tmp_path / "cost_log.jsonl", "ab") as fh:
        fh.write(b'{"ref": "2", "cost_u')
    with store.exclusive(tmp_path):
        store.log_cost(tmp_path, ref="3", cost_usd=0.004, **common)
    assert [c["ref"] for c in store.read_jsonl(tmp_path / "cost_log.jsonl")] == ["1", "3"]
    assert store.cost_summary(tmp_path)["calls"] == 2


def test_half_a_surrogate_pair_in_a_vendor_answer_is_stored_not_fatal(tmp_path):
    store.append_jsonl(tmp_path / "rows.jsonl", {"id": "a", "text": "deal \ud83d ends"})
    assert store.read_jsonl(tmp_path / "rows.jsonl") == [{"id": "a", "text": "deal \ud83d ends"}]


def test_a_slow_engine_does_not_hold_up_the_others(make_panel, stage_args, monkeypatch):
    import threading
    args = run_args(stage_args, make_panel(questions=4, runs=1))
    real, gate, seen = run.engines.ask, threading.Event(), []

    def ask(engine, question, market, config):
        if engine == "chatgpt":
            gate.wait(5)   # chatgpt answers only after every other engine has finished
        result = real(engine, question, market, config)
        seen.append(engine)
        if sum(1 for e in seen if e != "chatgpt") == 12:
            gate.set()
        return result

    monkeypatch.setattr(run.engines, "ask", ask)
    assert run.main(args) == 0
    assert set(seen[:12]) == {"google_ai", "claude", "gemini"} and seen.count("chatgpt") == 4


def test_rehearsal_answers_are_marked_and_a_scan_refuses_their_directory(make_panel, stage_args):
    path = make_panel(questions=2, days=["2020-01-01"])
    assert run.main(run_args(stage_args, path, limit=1)) == 0
    args = run_args(stage_args, path, any_day=False)
    assert all(r["off_day"] for r in store.read_jsonl(args.out / "answers.jsonl"))
    with pytest.raises(store.StoreError, match="rehearsal answers"):
        run.main(args)


# ---------- a published number is never a stale or wrong zero ----------
def test_curly_apostrophes_and_non_breaking_spaces_do_not_hide_a_brand():
    macys = Brand(id="macys", name="Macy's", domain="macys.example")
    best = Brand(id="bestbuy", name="Best Buy", domain="bestbuy.example")
    assert parse.proposes(macys, "Macy’s has the widest range.", []) is True
    assert parse.proposes(best, "Try Best Buy for laptops.", []) is True
    assert parse.proposes(macys, "Many stores have deals.", []) is False


def test_an_alias_added_after_parse_gets_the_answer_read_instead_of_keeping_its_zero(make_panel, stage_args):
    path = make_panel(reader=STUB_READERS)
    args = parse_args(stage_args, path)
    write_answers(args.out, path, [("chatgpt", "q01", "VoltCty has the best price.", [], True)])
    key = (f"{DAY}/chatgpt/q01/r1", "voltcity")
    assert parse.main(args, complete=Stub()) == 0
    assert (mentions(args.out)[key]["proposed"], mentions(args.out)[key]["value"]) == (False, 0.0)

    (args.out / "additions.yaml").write_text(yaml.safe_dump({"aliases": [
        {"brand_id": "voltcity", "alias": "VoltCty", "answer_id": key[0]}]}))
    reads = Stub(table={("m1", "voltcity"): "MENTIONED", ("m2", "voltcity"): "MENTIONED"})
    assert parse.main(args, complete=reads) == 0
    row = mentions(args.out)[key]
    assert (row["proposed"], row["value"], row["settled_by"]) == (True, 0.5, "agree")
    assert len(store.read_jsonl(args.out / "mentions.jsonl")) == 4


# ---------- the site reader ----------
def test_a_malformed_link_does_not_stop_the_site_read():
    assert host_of("https://[domain]/sale") == ""
    facts = siteread.read_html("<html><body><a href='https://[domain]/sale'>x</a><a href='/tv'>tv</a></body></html>",
                               "https://northmart.example/", {})
    assert facts.links == ["https://northmart.example/tv"]


def test_a_robots_file_with_a_byte_order_mark_still_blocks():
    rules = siteread.parse_robots("﻿User-agent: *\nDisallow: /\n")
    assert siteread.robots_allows(rules, "CleoCrawler", "/") is False


def test_a_failed_site_seo_call_exits_1_and_the_rerun_retries_only_the_score(make_panel, stage_args, monkeypatch, tmp_path):
    path = make_panel(brands=1)
    args = stage_args(path, go=True, refresh=False)
    monkeypatch.setenv("PAGESPEED_API_KEY", "test-key")
    replies = [{"score": None, "source": "stub", "reason": "pagespeed_unavailable", "detail": "429 from PageSpeed"},
               {"score": 77, "source": "stub", "reason": None}]
    monkeypatch.setattr(siteseo, "measure", lambda url, html: replies.pop(0))
    fetched = []
    words = " ".join(f"word{i}" for i in range(80))

    def fetch(url):
        fetched.append(url)
        return Response(404, {}, "") if url.endswith("robots.txt") else Response(200, HTML, f"<html><body>{words}</body></html>")

    assert siteread.main(args, fetch=fetch, interval=0, sleep=lambda s: None) == 1
    first = store.read_json(args.out / "sites.json")["brands"]["northmart"]
    assert (first["status"], first["site_seo"]["score"]) == ("ok", None)
    pages = len(fetched)

    assert siteread.main(args, fetch=fetch, interval=0, sleep=lambda s: None) == 0
    assert store.read_json(args.out / "sites.json")["brands"]["northmart"]["site_seo"]["score"] == 77
    assert fetched[pages:] == ["https://northmart.example/robots.txt", "https://northmart.example/"]  # no key page again


# ---------- dates ----------
def test_answers_are_not_asked_under_a_scan_day_that_is_not_today(make_panel, stage_args):
    args = run_args(stage_args, make_panel(questions=1, days=["2020-01-01"]), any_day=False)
    with pytest.raises(PanelError, match="is not today"):
        run.main(args)
    assert not (args.out / "answers.jsonl").exists()


def test_a_keyword_pull_is_not_filed_under_another_week(make_panel, stage_args):
    args = stage_args(make_panel(keywords=1, keyword_pulls=["2020-01-01", "2020-01-08"]), pull="2020-01-01", go=True,
                      attempts=1, location_code=None, any_week=False)
    calls = []
    with pytest.raises(PanelError, match="not in this calendar week"):
        keywords.main(args, post=lambda endpoint, body: calls.append(body))
    assert calls == []


# ---------- second review ----------
def test_a_site_seo_that_reached_only_one_brand_does_not_decide_the_ranking(make_panel):
    from scan import score
    from scan.tests.test_score import all_ok, ok_site, scan_rows
    panel = panel_file.load(make_panel(questions=60))
    answers, mention_rows, serp = scan_rows(panel)
    sites = all_ok(panel, **{b.id: ok_site(seo=None) for b in panel.brands if b.id != "gap"})
    scores = score.score_scan(panel, answers, mention_rows, serp, sites)
    assert "rdy.site_seo" not in scores["signals_measured"]
    gap = scores["brands"]["gap"]["signals"]["rdy.site_seo"]
    assert (gap["state"], gap["reason"]) == ("not_measured", "measured_for_too_few_brands")
    assert all(b["fully_measured"] and b["coverage"] == 0.65 for b in scores["brands"].values())


def test_scores_refuse_mentions_read_before_a_name_was_added(make_panel):
    from dataclasses import replace
    from scan import score
    from scan.tests.test_score import all_ok, scan_rows
    panel = panel_file.load(make_panel(questions=60))
    answers, mention_rows, serp = scan_rows(panel)
    renamed = replace(panel, brands=tuple(replace(b, aliases=("NM Stores",)) if b.id == "northmart" else b
                                          for b in panel.brands))
    with pytest.raises(score.ScoreError, match="before a name was added"):
        score.score_scan(renamed, answers, mention_rows, serp, all_ok(panel))


def test_an_alias_added_later_also_redoes_the_answers_already_read_for_the_brand(make_panel, stage_args):
    path = make_panel(reader=STUB_READERS)
    args = parse_args(stage_args, path)
    cited = ["https://voltcity.example/tv"]
    write_answers(args.out, path, [("chatgpt", "q01", "VC Stores has the best price.", cited, True)])
    key = (f"{DAY}/chatgpt/q01/r1", "voltcity")
    assert parse.main(args, complete=Stub()) == 0   # proposed by its domain, read as not mentioned
    assert (mentions(args.out)[key]["proposed"], mentions(args.out)[key]["value"]) == (True, 0.0)

    (args.out / "additions.yaml").write_text(yaml.safe_dump({"aliases": [
        {"brand_id": "voltcity", "alias": "VC Stores", "answer_id": key[0]}]}))
    reads = Stub(table={("m1", "voltcity"): "MENTIONED", ("m2", "voltcity"): "MENTIONED"})
    assert parse.main(args, complete=reads) == 0
    assert mentions(args.out)[key]["value"] == 0.5
    assert [c for c in reads.calls if "voltcity" in c[1]] == [("m1", ["voltcity"]), ("m2", ["voltcity"])]
    assert len(store.read_jsonl(args.out / "mentions.jsonl")) == 4


def test_public_reasons_carry_no_vendor_or_site_error_text():
    from scan import rank
    brand = {"signals": {"rdy.site_seo": {"state": "not_measured", "reason": "pagespeed_unavailable: 429 for url ...&key=SECRET"},
                         "rec.reviews": {"state": "not_measured", "reason": "not_in_protocol"}}}
    assert rank.unranked_reason(brand) == "pagespeed_unavailable"


def test_a_redirect_off_the_brands_site_is_not_read_as_the_brand(make_panel, stage_args):
    args = stage_args(make_panel(brands=1), go=True, refresh=False)
    pages = {"https://northmart.example/robots.txt": Response(404, {}, ""),
             "https://northmart.example/": Response(302, {"location": "https://queue.other.example/wait"}, "")}
    fetched = []

    def fetch(url):
        fetched.append(url)
        return pages[url]

    assert siteread.main(args, fetch=fetch, interval=0, sleep=lambda s: None) == 1
    brand = store.read_json(args.out / "sites.json")["brands"]["northmart"]
    assert (brand["status"], brand["reason"]) == ("failed", "homepage_redirect_off_site")
    assert "https://queue.other.example/wait" not in fetched


def test_a_tracking_parameter_does_not_make_a_second_page_or_break_the_canonical():
    brand = Brand(id="northmart", name="Northmart", domain="northmart.example")
    answers = [{"valid": True, "cited_urls": ["https://northmart.example/sale?utm_source=chatgpt.com",
                                              "https://www.northmart.example/sale", "https://northmart.example/?utm_source=x"]}]
    assert siteread.key_pages(brand, answers, []) == ["https://northmart.example/", "https://northmart.example/sale"]


def test_a_real_page_that_loads_a_challenge_script_is_not_a_block_and_a_thin_one_is():
    words = " ".join(f"word{i}" for i in range(80))
    script = '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
    real = Response(200, HTML, f"<html><head><title>Shop</title>{script}</head><body><p>{words}</p></body></html>")
    wall = Response(200, HTML, f"<html><head><title>Shop</title>{script}</head><body>Checking</body></html>")
    human = Response(200, HTML, f"<html><head><title>Robot or human?</title></head><body><p>{words}</p></body></html>")
    assert (siteread.is_challenge(real), siteread.is_challenge(wall), siteread.is_challenge(human)) == (False, True, True)


def test_a_keyword_with_no_search_results_is_stored_as_ranking_nobody():
    assert keywords.organic_results(keywords.task_result({"status_code": 40102, "status_message": "No Search Results."})) == []


def test_a_cut_off_or_failed_search_answer_is_not_a_valid_answer():
    from scan.engines import claude
    failed = {"turns": [{"content": [{"type": "web_search_tool_result", "content": {
        "type": "web_search_tool_result_error", "error_code": "too_many_requests"}}]}]}
    capped = {"turns": [{"content": [{"type": "web_search_tool_result", "content": {
        "type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"}}]}]}
    fine = {"turns": [{"content": [{"type": "web_search_tool_result", "content": [{"type": "web_search_result", "url": "https://a.example/"}]}]}]}
    assert (claude.search_error(failed), claude.search_error(capped), claude.search_error(fine)) == ("too_many_requests", "", "")
