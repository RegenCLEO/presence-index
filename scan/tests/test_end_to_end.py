"""The whole pipeline on a fake scan: every stage reads what the stage before it wrote."""
import json

from scan import keywords, siteread, siteseo, store
from scan.__main__ import main as cli
from scan.siteread import Response
from scan.tests.conftest import BRANDS

HTML = {"content-type": "text/html; charset=utf-8"}


def page(domain):
    words = " ".join(f"word{i}" for i in range(80))
    org = {"@context": "https://schema.org", "@type": "Organization", "name": domain, "url": f"https://{domain}/",
           "sameAs": [f"https://www.instagram.com/{domain.split('.')[0]}"]}
    return Response(200, HTML, f"<html><head><title>{domain}</title>"
                               f'<script type="application/ld+json">{json.dumps(org)}</script></head>'
                               f'<body><main><p>{words}</p><a href="https://www.facebook.com/{domain}">fb</a>'
                               f"</main></body></html>")


def fake_fetch(url):
    if url.endswith("/robots.txt"):
        return Response(404, {}, "")
    return page(url.split("/")[2])


def fake_serp(endpoint, body):
    items = [{"type": "organic", "rank_group": 1, "url": "https://www.northmart.example/deals"},
             {"type": "organic", "rank_group": 2, "url": "https://reviews.example.org/guide"},
             {"type": "organic", "rank_group": 4, "url": "https://target.example/sale"}]
    return {"status_code": 20000, "tasks": [{"status_code": 20000, "cost": 0.002, "result": [{"items": items}]}]}


def test_a_fake_scan_goes_from_panel_to_public_snapshot(make_panel, stage_args, monkeypatch):
    panel_path = make_panel(questions=30)
    out = stage_args(panel_path).out
    base = ["--panel", str(panel_path), "--out", str(out)]
    monkeypatch.delenv("PAGESPEED_API_KEY", raising=False)
    monkeypatch.setattr(siteseo, "measure", lambda url, html: {"score": 71, "source": "test", "reason": None})

    assert cli(["run", *base]) == 0 and not (out / "answers.jsonl").exists()
    assert cli(["run", *base, "--any-day", "--go"]) == 0
    for pull in ("2026-11-21", "2026-11-23"):
        args = stage_args(panel_path, pull=pull, go=True, attempts=1, location_code=None, any_week=True)
        assert keywords.main(args, post=fake_serp) == 0
    assert siteread.main(stage_args(panel_path, go=True, refresh=False), fetch=fake_fetch, interval=0,
                         sleep=lambda s: None) == 0
    assert cli(["parse", *base, "--go"]) == 0
    assert cli(["score", *base]) == 0
    assert cli(["rank", *base]) == 0

    answers = store.read_jsonl(out / "answers.jsonl")
    valid = [a for a in answers if a["valid"]]
    assert len(answers) == 30 * 4 * 2
    assert len(store.read_jsonl(out / "serp.jsonl")) == 6 * 2
    assert len(store.read_jsonl(out / "mentions.jsonl")) == len(valid) * len(BRANDS)

    scores = store.read_json(out / "scores.json")
    google_valid = sum(1 for a in valid if a["engine"] == "google_ai")
    assert google_valid < 60 and scores["engines"]["google_ai"]["counts_for_recommendation"] is False
    assert all(scores["engines"][e]["counts_for_recommendation"] for e in ("chatgpt", "claude", "gemini"))
    assert scores["signals_measured"] == ["rec.ai_recommendation", "rec.ai_citation", "rec.organic_search",
                                          "rdy.crawl_access", "rdy.site_seo", "rdy.social", "rdy.structured_data"]

    public = store.read_json(out / "snapshot.public.json")
    assert sorted(r["name"] for r in public["rows"]) == ["Gap", "Northmart", "Target", "Volt City"]
    assert {(r["coverage"], r["status"]) for r in public["rows"]} == {(0.74, "provisional")}
    assert {r["label"] for r in public["rows"]} == {"CLEO Presence Index, public scan, CPI v1.0, 74% of method measured"}
    assert all(0 <= r["range"][0] <= r["index"] <= r["range"][1] <= 100 for r in public["rows"])
    assert public["stability"] == {"stable": True, "needs_second_day": False}
    assert public["rate_only"] == [] and "grade" not in json.dumps(public)
    northmart = scores["brands"]["northmart"]["signals"]
    assert northmart["rec.organic_search"]["state"] == "live" and northmart["rec.organic_search"]["raw"] == 1.0
    assert northmart["rdy.social"]["score"] == 50.0
    assert northmart["rdy.site_seo"]["score"] == 71

    before = {name: (out / name).read_bytes() for name in ("answers.jsonl", "mentions.jsonl", "snapshot.public.json")}
    assert cli(["run", *base, "--any-day", "--go"]) == 0 and cli(["parse", *base, "--go"]) == 0
    assert cli(["score", *base]) == 0 and cli(["rank", *base]) == 0
    assert {name: (out / name).read_bytes() for name in before} == before


def test_rank_refuses_scores_older_than_their_inputs(make_panel, stage_args, monkeypatch, capsys):
    panel_path = make_panel(questions=30)
    out = stage_args(panel_path).out
    base = ["--panel", str(panel_path), "--out", str(out)]
    assert cli(["run", *base, "--any-day", "--go"]) == 0 and cli(["parse", *base, "--go"]) == 0 and cli(["score", *base]) == 0
    (out / "sites.json").write_text(json.dumps({"panel_sha256": store.read_json(out / "scores.json")["panel_sha256"],
                                                "brands": {}}))
    assert cli(["rank", *base]) == 2
    assert "changed after scores.json was written" in capsys.readouterr().err
