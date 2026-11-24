import re

import pytest
import yaml

from scan import panel as panel_file, parse, run, store
from scan.panel import PanelError
from scan.records import LlmResult

DAY = "2026-11-21"
STUB_READERS = {"first": {"provider": "stub", "model": "m1"}, "second": {"provider": "stub", "model": "m2"},
                "tiebreak": {"provider": "stub", "model": "m3"}}


def parse_args(stage_args, panel_path, **kw):
    return stage_args(panel_path, **{"go": True, "workers": 2, "attempts": 2, "sample_csv": None, **kw})


def write_answers(out, panel_path, answers):
    """answers: (engine, question_id, text, cited_urls, valid)."""
    panel = panel_file.load(panel_path)
    for engine, qid, text, cited, valid in answers:
        store.append_jsonl(out / "answers.jsonl", {
            "id": f"{DAY}/{engine}/{qid}/r1", "panel_id": panel.panel_id, "panel_sha256": panel.sha256,
            "day": DAY, "question_id": qid, "engine": engine, "run": 1, "model": "x", "settings": {},
            "asked_at": "t", "raw": {}, "text": text, "cited_urls": cited, "valid": valid,
            "invalid_reason": None if valid else "no_ai_overview"})


class Stub:
    """A reader whose verdict per (model, brand id) comes from ``table``; default NOT_MENTIONED."""

    def __init__(self, table=None, others=None, drop=None):
        self.table, self.others, self.drop, self.calls = table or {}, others or {}, drop, []

    def __call__(self, provider, model, prompt):
        ids = re.findall(r"^- id: (\S+)", prompt, re.M)
        self.calls.append((model, sorted(ids)))
        brands = [{"id": i, "classification": self.table.get((model, i), "NOT_MENTIONED"),
                   "polarity": "neutral", "position": None} for i in ids if i != self.drop]
        named = [n for word, names in self.others.items() if word in prompt for n in names]
        return LlmResult(data={"brands": brands, "other_businesses": named}, model=model,
                         usage={"input_tokens": 100})


def mentions(out):
    return {(m["answer_id"], m["brand_id"]): m for m in store.read_jsonl(out / "mentions.jsonl")}


def test_stage_one_proposes_on_word_boundaries_after_ignore_phrases(make_panel):
    brands = {b.id: b for b in panel_file.load(make_panel()).brands}
    assert parse.proposes(brands["target"], "I recommend Target for TVs", []) is True
    assert parse.proposes(brands["target"], "know your target audience", []) is False
    assert parse.proposes(brands["gap"], "the price gap is small", []) is False
    assert parse.proposes(brands["gap"], "Gap has jeans", []) is True
    assert parse.proposes(brands["northmart"], "try North Mart today", []) is True
    assert parse.proposes(brands["northmart"], "no store named here", ["https://shop.northmart.example/tv"]) is True
    assert parse.proposes(brands["northmart"], "no store named here", ["https://reviews.example.org/"]) is False


def test_fake_engine_answers_parse_to_one_row_per_valid_answer_and_brand(make_panel, stage_args):
    path = make_panel(questions=10, runs=1, missing_every=3)
    args = stage_args(path, day=None, go=True, workers=2, attempts=1, limit=None, any_day=True)
    assert run.main(args) == 0
    assert parse.main(parse_args(stage_args, path)) == 0

    answers = store.read_jsonl(args.out / "answers.jsonl")
    valid = [a for a in answers if a["valid"]]
    invalid = [a["id"] for a in answers if not a["valid"]]
    rows = mentions(args.out)
    assert (len(answers), len(valid), len(rows)) == (40, 37, 148)
    assert {a for a, _ in rows}.isdisjoint(invalid)

    first = {b: rows[("2026-11-21/chatgpt/q01/r1", b)] for b in ("voltcity", "target", "northmart", "gap")}
    assert [(r["value"], r["own_domain_cited"], r["settled_by"]) for r in first.values()] == [
        (1.0, True, "agree"), (0.5, False, "agree"), (0.0, False, "not_proposed"), (0.0, False, "not_proposed")]
    for a in valid:
        named = {b for b in ("Northmart", "Target", "Gap", "Volt City") if b in a["text"]}
        for (aid, bid), r in rows.items():
            if aid != a["id"]:
                continue
            name = {"northmart": "Northmart", "target": "Target", "gap": "Gap", "voltcity": "Volt City"}[bid]
            expected = 1.0 if f"recommend {name}" in a["text"] else 0.5 if name in named else 0.0
            assert (r["value"], r["own_domain_cited"]) == (expected, expected == 1.0)


def test_disagreeing_reads_go_to_the_tiebreak_and_agreeing_reads_do_not(make_panel, stage_args):
    path = make_panel(reader=STUB_READERS)
    args = parse_args(stage_args, path)
    write_answers(args.out, path, [("chatgpt", "q01", "I recommend Northmart. Gap is fine.", [], True)])
    stub = Stub({("m1", "northmart"): "RECOMMENDED", ("m2", "northmart"): "MENTIONED",
                 ("m3", "northmart"): "MENTIONED", ("m1", "gap"): "MENTIONED", ("m2", "gap"): "MENTIONED"})
    assert parse.main(args, complete=stub) == 0

    assert sorted(stub.calls) == [("m1", ["gap", "northmart"]), ("m2", ["gap", "northmart"]), ("m3", ["northmart"])]
    rows = mentions(args.out)
    north, gap = rows[(f"{DAY}/chatgpt/q01/r1", "northmart")], rows[(f"{DAY}/chatgpt/q01/r1", "gap")]
    assert (north["value"], north["settled_by"], [r["model"] for r in north["reads"]]) == (0.5, "tiebreak", ["m1", "m2", "m3"])
    assert (gap["value"], gap["settled_by"]) == (0.5, "agree")
    assert len(store.read_jsonl(args.out / "cost_log.jsonl")) == 3


def test_a_reply_that_drops_a_brand_writes_nothing_and_a_rerun_finishes(make_panel, stage_args):
    path = make_panel(reader=STUB_READERS)
    args = parse_args(stage_args, path)
    write_answers(args.out, path, [("chatgpt", "q01", "Northmart and Gap both sell this.", [], True),
                                   ("claude", "q01", "Nothing named.", [], True)])
    broken = Stub(drop="gap")
    assert parse.main(args, complete=broken) == 1
    # Two failed tries at read one of the first answer. The second answer names no listed
    # brand and is still read twice, for the businesses outside the list.
    assert sorted(broken.calls) == [("m1", []), ("m1", ["gap", "northmart"]), ("m1", ["gap", "northmart"]), ("m2", [])]
    rows = mentions(args.out)
    assert sorted(rows) == [(f"{DAY}/claude/q01/r1", b) for b in ("gap", "northmart", "target", "voltcity")]
    assert [e["id"] for e in store.read_jsonl(args.out / "read_errors.jsonl")] == [f"{DAY}/chatgpt/q01/r1"]

    working = Stub({(m, b): "MENTIONED" for m in ("m1", "m2") for b in ("northmart", "gap")})
    assert parse.main(args, complete=working) == 0
    rows = mentions(args.out)
    assert len(rows) == 8
    assert rows[(f"{DAY}/chatgpt/q01/r1", "gap")]["value"] == 0.5

    again = Stub()
    assert parse.main(args, complete=again) == 0
    assert again.calls == []
    assert len(store.read_jsonl(args.out / "mentions.jsonl")) == 8


def test_a_brand_added_later_is_the_only_one_read_on_the_rerun(make_panel, stage_args):
    path = make_panel(reader=STUB_READERS)
    args = parse_args(stage_args, path)
    write_answers(args.out, path, [("chatgpt", "q01", "Northmart or Bolt Hub.", [], True),
                                   ("gemini", "q01", "Gap only.", [], True)])
    assert parse.main(args, complete=Stub()) == 0
    (args.out / "additions.yaml").write_text(yaml.safe_dump({"brands": [
        {"id": "bolthub", "name": "Bolt Hub", "domain": "bolthub.example", "confirmed_by": "a person"}]}))

    stub = Stub({("m1", "bolthub"): "RECOMMENDED", ("m2", "bolthub"): "RECOMMENDED"})
    assert parse.main(args, complete=stub) == 0
    assert stub.calls == [("m1", ["bolthub"]), ("m2", ["bolthub"])]
    rows = mentions(args.out)
    assert len(rows) == 10
    assert rows[(f"{DAY}/chatgpt/q01/r1", "bolthub")]["value"] == 1.0
    assert rows[(f"{DAY}/gemini/q01/r1", "bolthub")]["settled_by"] == "not_proposed"


def test_a_tiebreak_that_is_the_first_reader_is_refused(make_panel, stage_args):
    path = make_panel(reader={**STUB_READERS, "tiebreak": {"provider": "stub", "model": "m1"}})
    with pytest.raises(PanelError, match="tiebreak"):
        parse.main(parse_args(stage_args, path), complete=Stub())


def test_candidates_count_unlisted_businesses_by_engine_weight_and_keep_alias_gaps_apart(make_panel, stage_args):
    path = make_panel(reader=STUB_READERS)
    args = parse_args(stage_args, path)
    write_answers(args.out, path, [("chatgpt", "q01", "Northmart, or try Acme.", [], True),
                                   ("claude", "q01", "Northmart vs ACME", [], True),
                                   ("gemini", "q01", "Northmart alone, also North-Mart", [], True),
                                   ("google_ai", "q01", "", [], False)])
    stub = Stub({(m, "northmart"): "MENTIONED" for m in ("m1", "m2")},
                others={"try Acme": ["Acme Outlet"], "ACME": ["acme outlet.com"], "North-Mart": ["North-Mart"]})
    assert parse.main(args, complete=stub) == 0

    cands = store.read_json(args.out / "candidates.json")
    assert [(u["name"], u["share"], u["answers"]) for u in cands["unlisted"]] == [("Acme Outlet", 0.7, 2)]
    assert cands["alias_gaps"] == [{"brand_id": "northmart", "spelling": "North-Mart",
                                    "answer_ids": [f"{DAY}/gemini/q01/r1"]}]
    assert cands["listed_shares"]["northmart"] == 1.0
    assert cands["tenth_listed_share"] is None
    assert cands["would_enter"] == ["Acme Outlet"]
