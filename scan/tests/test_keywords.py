import pytest

from scan import keywords, store
from scan.panel import PanelError


def reply(items, cost=0.002):
    return {"status_code": 20000, "tasks": [{"status_code": 20000, "cost": cost, "result": [{"items": items}]}]}


ITEMS = [
    {"type": "paid", "rank_group": 1, "url": "https://ads.example/buy"},
    {"type": "organic", "rank_group": 2, "url": "https://www.northmart.example/tv"},
    {"type": "featured_snippet", "rank_group": 1, "url": "https://snippet.example/"},
    {"type": "organic", "rank_group": 1, "url": "https://target.example/deals"},
    {"type": "organic", "rank_group": 3, "url": "https://www.google.com/goto?u=x",
     "breadcrumb": "https://gap.example › Sale"},
    {"type": "organic", "rank_group": 4, "url": "https://www.google.com/goto?u=y",
     "breadcrumb": "10+ comments · 9 months ago"},
    *({"type": "organic", "rank_group": n, "url": f"https://site{n}.example/"} for n in range(5, 12)),
    {"type": "people_also_ask", "rank_group": 1},
]


class FakePost:
    def __init__(self, items=ITEMS):
        self.calls, self.items = [], items

    def __call__(self, endpoint, body):
        self.calls.append((endpoint, body))
        return reply(self.items)


def args_for(stage_args, panel_path, **kw):
    return stage_args(panel_path, **{"pull": "2026-11-21", "go": True, "attempts": 1, "location_code": None, "any_week": True, **kw})


def test_the_request_for_one_keyword(make_panel, stage_args):
    post = FakePost()
    assert keywords.main(args_for(stage_args, make_panel(keywords=1)), post=post) == 0
    assert post.calls == [("serp/google/organic/live/advanced", [{
        "keyword": "black friday item 1 deals", "location_code": 2840, "language_code": "en",
        "device": "desktop", "depth": 10}])]


def test_organic_items_become_positions_one_to_ten():
    assert keywords.organic_results({"items": ITEMS}) == [
        {"position": 1, "url": "https://target.example/deals"},
        {"position": 2, "url": "https://www.northmart.example/tv"},
        {"position": 3, "url": "https://gap.example/"},
        {"position": 5, "url": "https://site5.example/"},
        {"position": 6, "url": "https://site6.example/"},
        {"position": 7, "url": "https://site7.example/"},
        {"position": 8, "url": "https://site8.example/"},
        {"position": 9, "url": "https://site9.example/"},
        {"position": 10, "url": "https://site10.example/"},
    ]


def test_rows_and_cost_rows_are_written_and_a_rerun_makes_no_calls(make_panel, stage_args):
    panel_path = make_panel(keywords=3)
    args = args_for(stage_args, panel_path)
    first = FakePost()
    assert keywords.main(args, post=first) == 0
    assert len(first.calls) == 3
    rows = store.read_jsonl(args.out / "serp.jsonl")
    assert [r["id"] for r in rows] == ["2026-11-21/black friday item 1 deals",
                                       "2026-11-21/black friday item 2 deals",
                                       "2026-11-21/black friday item 3 deals"]
    assert rows[1]["volume"] == 2000.0
    assert rows[0]["results"][0] == {"position": 1, "url": "https://target.example/deals"}
    costs = store.read_jsonl(args.out / "cost_log.jsonl")
    assert [(c["purpose"], c["vendor"], c["cost_usd"], c["basis"]) for c in costs] == [
        ("keyword", "dataforseo", 0.002, "vendor")] * 3

    again = FakePost()
    assert keywords.main(args, post=again) == 0
    assert again.calls == []
    assert len(store.read_jsonl(args.out / "serp.jsonl")) == 3


def test_a_failed_keyword_is_logged_and_left_for_a_rerun(make_panel, stage_args):
    args = args_for(stage_args, make_panel(keywords=2))

    def flaky(endpoint, body):
        if body[0]["keyword"].endswith("2 deals"):
            return {"status_code": 20000, "tasks": [{"status_code": 40501, "status_message": "bad", "cost": 0}]}
        return reply(ITEMS)

    assert keywords.main(args, post=flaky) == 1
    assert [r["id"] for r in store.read_jsonl(args.out / "serp.jsonl")] == ["2026-11-21/black friday item 1 deals"]
    assert [e["id"] for e in store.read_jsonl(args.out / "errors.jsonl")] == ["2026-11-21/black friday item 2 deals"]
    post = FakePost()
    assert keywords.main(args, post=post) == 0
    assert [body[0]["keyword"] for _, body in post.calls] == ["black friday item 2 deals"]


def test_the_default_pull_is_the_only_one_not_complete(make_panel, stage_args):
    args = args_for(stage_args, make_panel(keywords=1))
    keywords.main(args, post=FakePost())
    post = FakePost()
    assert keywords.main(args_for(stage_args, args.panel, pull=None), post=post) == 0
    assert [r["pull"] for r in store.read_jsonl(args.out / "serp.jsonl")] == ["2026-11-21", "2026-11-23"]


def test_a_pull_outside_the_panel_is_refused(make_panel, stage_args):
    post = FakePost()
    with pytest.raises(PanelError, match="2026-11-22 is not in the panel's keyword_pulls"):
        keywords.main(args_for(stage_args, make_panel(), pull="2026-11-22"), post=post)
    assert post.calls == []


def test_two_pulls_in_one_iso_week_are_refused(make_panel, stage_args):
    post = FakePost()
    path = make_panel(keyword_pulls=["2026-11-23", "2026-11-28"])
    with pytest.raises(PanelError, match="same ISO week 2026-W48"):
        keywords.main(args_for(stage_args, path, pull="2026-11-23"), post=post)
    assert post.calls == []


def test_without_go_nothing_is_asked(make_panel, stage_args, capsys):
    post = FakePost()
    assert keywords.main(args_for(stage_args, make_panel(keywords=4), go=False), post=post) == 0
    assert post.calls == []
    assert "4 checks to make" in capsys.readouterr().out
