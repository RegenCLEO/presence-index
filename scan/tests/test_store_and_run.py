import json

import pytest

from scan import run, store
from scan.records import EngineError, host_of, is_owned


def args_for(stage_args, panel_path, **kw):
    return stage_args(panel_path, **{"day": None, "go": True, "workers": 4, "attempts": 2, "limit": None, "any_day": True, **kw})


def test_hosts_match_the_product_rule():
    assert host_of("https://WWW.Example.com/a?b=1") == "example.com"
    assert host_of("shop.example.com.") == "shop.example.com"
    assert is_owned("https://deals.example.com/x", "example.com")
    assert not is_owned("https://notexample.com/x", "example.com")
    assert not is_owned("", "example.com")


def test_a_torn_last_line_is_skipped_on_read_and_removed_by_repair(tmp_path):
    path = tmp_path / "rows.jsonl"
    store.append_jsonl(path, {"id": "a"})
    store.append_jsonl(path, {"id": "b"})
    with open(path, "ab") as fh:
        fh.write(b'{"id": "c", "text": "cut of')
    assert [r["id"] for r in store.read_jsonl(path)] == ["a", "b"]
    assert store.repair(path) == len(b'{"id": "c", "text": "cut of')
    store.append_jsonl(path, {"id": "c"})
    assert [r["id"] for r in store.read_jsonl(path)] == ["a", "b", "c"]
    assert store.repair(path) == 0


def test_a_bad_line_in_the_middle_is_an_error(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_bytes(b'{"id": "a"}\nnot json\n{"id": "b"}\n')
    with pytest.raises(store.StoreError, match="line 2"):
        store.read_jsonl(path)


def test_dry_run_asks_nothing(make_panel, stage_args, capsys):
    args = args_for(stage_args, make_panel(questions=3), go=False)
    assert run.main(args) == 0
    assert not (args.out / "answers.jsonl").exists()
    assert "24 to make" in capsys.readouterr().out


def test_run_stores_every_answer_once_and_a_rerun_adds_nothing(make_panel, stage_args):
    args = args_for(stage_args, make_panel(questions=5, missing_every=2))
    assert run.main(args) == 0
    rows = store.read_jsonl(args.out / "answers.jsonl")
    assert len(rows) == 5 * 4 * 2 == len({r["id"] for r in rows})
    assert rows[0]["id"].startswith("2026-11-21/") and rows[0]["panel_sha256"]
    missing = [r for r in rows if not r["valid"]]
    assert missing and {r["engine"] for r in missing} == {"google_ai"}
    assert {r["invalid_reason"] for r in missing} == {"no_ai_overview"}
    costs = store.read_jsonl(args.out / "cost_log.jsonl")
    assert sorted(c["ref"] for c in costs) == sorted(r["id"] for r in rows)

    assert run.main(args) == 0
    assert len(store.read_jsonl(args.out / "answers.jsonl")) == 40
    assert len(store.read_jsonl(args.out / "cost_log.jsonl")) == 40


def test_a_failed_call_is_not_stored_and_the_rerun_asks_it_again(make_panel, stage_args, monkeypatch):
    args = args_for(stage_args, make_panel(questions=3))
    real = run.engines.ask
    monkeypatch.setattr(run.time, "sleep", lambda s: None)

    def flaky(engine, question, market, config):
        if engine == "claude" and "item 2" in question:
            raise EngineError("503 from vendor")
        return real(engine, question, market, config)

    monkeypatch.setattr(run.engines, "ask", flaky)
    assert run.main(args) == 1
    assert len(store.read_jsonl(args.out / "answers.jsonl")) == 22
    assert sorted(e["id"] for e in store.read_jsonl(args.out / "errors.jsonl")) == [
        "2026-11-21/claude/q02/r1", "2026-11-21/claude/q02/r2"]

    monkeypatch.setattr(run.engines, "ask", real)
    assert run.main(args) == 0
    assert len(store.read_jsonl(args.out / "answers.jsonl")) == 24


def test_a_run_resumes_after_a_crash_left_half_a_row(make_panel, stage_args):
    args = args_for(stage_args, make_panel(questions=3), limit=2)
    assert run.main(args) == 0
    path = args.out / "answers.jsonl"
    whole = path.read_bytes()
    path.write_bytes(whole[: len(whole) - 40])
    args.limit = None
    assert run.main(args) == 0
    rows = store.read_jsonl(path)
    assert len(rows) == 24 == len({r["id"] for r in rows})


def test_rows_from_another_panel_are_refused(make_panel, stage_args):
    args = args_for(stage_args, make_panel(questions=3))
    store.append_jsonl(args.out / "answers.jsonl", {"id": "x", "panel_sha256": "someone-else"})
    with pytest.raises(store.StoreError, match="another panel"):
        run.main(args)


def test_cost_rows_use_the_vendor_cost_then_the_price_table_then_say_unpriced(tmp_path):
    common = dict(panel_id="p", day="2026-11-21", purpose="answer")
    a = store.log_cost(tmp_path, vendor="dataforseo", ref="1", usage={"tasks": 1}, cost_usd=0.004, **common)
    b = store.log_cost(tmp_path, vendor="anthropic", ref="2", model="claude-sonnet-5-5",
                       usage={"input_tokens": 1000, "output_tokens": 500, "web_search_requests": 2}, **common)
    c = store.log_cost(tmp_path, vendor="openai", ref="3", model="unknown-model", usage={"input_tokens": 10}, **common)
    assert (a["basis"], a["cost_usd"]) == ("vendor", 0.004)
    assert (b["basis"], b["cost_usd"]) == ("prices", pytest.approx(0.002 + 0.005 + 0.02))
    assert (c["basis"], c["cost_usd"]) == ("unpriced", None)
    summary = store.cost_summary(tmp_path)
    assert (summary["calls"], summary["unpriced_calls"]) == (3, 1)
    assert json.loads((tmp_path / "cost_log.jsonl").read_text().splitlines()[1])["model"] == "claude-sonnet-5-5"
