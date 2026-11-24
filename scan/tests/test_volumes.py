from argparse import Namespace

import pytest
import yaml

from scan import panel as panel_file, store, volumes
from scan.panel import PanelError

DRAFT = """\
panel_id: test-retailers-us-en
market: {country: US, language: en}

# what the panel asks Google
keywords:                       # unbranded
  - {text: "old keyword", volume: 0}

# from the named outside list
brands:
  - {id: northmart, name: Northmart, domain: northmart.example}
"""

CANDIDATES = """\
# a comment
[general]
Black Friday deals
black friday sale
black friday sales
deals black friday
cyber monday deals
[tvs]
black friday tv deals
black friday 100 inch tv deals
"""

VOLUMES = {"black friday deals": 9000, "black friday sale": 500, "black friday sales": 700,
           "deals black friday": 9000, "cyber monday deals": 8000, "black friday tv deals": 3000}


class FakePost:
    def __init__(self, found=VOLUMES, task_status=20000):
        self.calls, self.found, self.task_status = [], found, task_status

    def __call__(self, endpoint, body):
        self.calls.append((endpoint, body))
        result = [{"keyword": kw, "search_volume": volume} for kw, volume in self.found.items()]
        return {"status_code": 20000, "tasks": [{"status_code": self.task_status, "status_message": "x",
                                                 "cost": 0.09, "result": result}]}


@pytest.fixture
def build(tmp_path):
    """A draft panel, a candidates file and the arguments the command line would build."""
    def make(panel=DRAFT, **kwargs) -> Namespace:
        panel_path, candidates_path = tmp_path / "panel.yaml", tmp_path / "candidates.txt"
        panel_path.write_text(panel, encoding="utf-8")
        candidates_path.write_text(CANDIDATES, encoding="utf-8")
        return Namespace(**{"panel": panel_path, "out": tmp_path / "build", "candidates": candidates_path,
                            "keep": 3, "exclude_word": ["cyber"], "go": True, **kwargs})
    return make


def keywords_of(args):
    return yaml.safe_load(args.panel.read_text(encoding="utf-8"))["keywords"]


def test_a_dry_run_makes_no_call_and_writes_nothing(build):
    args, post = build(go=False), FakePost()
    assert volumes.main(args, post=post) == 0
    assert post.calls == [] and not args.out.exists()
    assert keywords_of(args) == [{"text": "old keyword", "volume": 0}]


def test_one_call_fills_the_panel_with_the_highest_volumes(build):
    args, post = build(), FakePost()
    assert volumes.main(args, post=post) == 0
    assert post.calls == [("keywords_data/google_ads/search_volume/live", [{
        "keywords": ["black friday 100 inch tv deals", "black friday deals", "black friday sale",
                     "black friday sales", "black friday tv deals", "cyber monday deals", "deals black friday"],
        "location_code": 2840, "language_code": "en"}])]
    assert keywords_of(args) == [{"text": "black friday deals", "volume": 9000},
                                 {"text": "black friday tv deals", "volume": 3000},
                                 {"text": "black friday sales", "volume": 700}]
    cost = store.read_jsonl(args.out / "cost_log.jsonl")
    assert [(row["purpose"], row["vendor"], row["cost_usd"]) for row in cost] == [("volumes", "dataforseo", 0.09)]


def test_the_rest_of_the_panel_and_its_comments_stay(build):
    args = build()
    volumes.main(args, post=FakePost())
    text = args.panel.read_text(encoding="utf-8")
    assert "# what the panel asks Google\nkeywords:\n" in text
    assert "\n\n# from the named outside list\nbrands:\n" in text
    assert "old keyword" not in text
    assert yaml.safe_load(text)["brands"] == yaml.safe_load(DRAFT)["brands"]


def test_the_table_says_why_a_candidate_is_out(build):
    args = build()
    volumes.main(args, post=FakePost())
    lines = (args.out / "keyword-volumes.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "keyword,group,volume,returned,kept,removed_because"
    assert "black friday deals,general,9000,yes,yes," in lines
    assert "deals black friday,general,9000,yes,no,near duplicate of 'black friday deals'" in lines
    assert "black friday sale,general,500,yes,no,near duplicate of 'black friday sales'" in lines
    assert "cyber monday deals,general,8000,yes,no,contains the excluded word 'cyber'" in lines
    assert "black friday 100 inch tv deals,tvs,0,no,no," in lines


def test_a_rerun_with_the_same_candidates_makes_no_call(build):
    args, post = build(), FakePost()
    volumes.main(args, post=post)
    assert volumes.main(Namespace(**{**vars(args), "go": False, "keep": 2}), post=post) == 0
    assert len(post.calls) == 1
    assert [k["text"] for k in keywords_of(args)] == ["black friday deals", "black friday tv deals"]


def test_changed_candidates_need_a_new_call(build):
    args, post = build(), FakePost()
    volumes.main(args, post=post)
    args.candidates.write_text(CANDIDATES + "black friday toy deals\n", encoding="utf-8")
    assert volumes.main(Namespace(**{**vars(args), "go": False}), post=post) == 0
    assert len(post.calls) == 1
    assert volumes.main(args, post=post) == 0
    assert len(post.calls) == 2


def test_too_few_candidates_with_a_volume_leave_the_panel_unchanged(build):
    args = build(keep=4)
    with pytest.raises(PanelError, match="3 candidates have a volume"):
        volumes.main(args, post=FakePost())
    assert keywords_of(args) == [{"text": "old keyword", "volume": 0}]
    assert (args.out / "keyword-volumes.csv").exists()


def test_a_frozen_panel_is_refused(make_panel, build):
    args = build()
    args.panel = make_panel()
    with pytest.raises(PanelError, match="frozen"):
        volumes.main(args, post=FakePost())


def test_a_failed_task_is_logged_as_paid_and_not_saved(build):
    args = build()
    assert volumes.main(args, post=FakePost(task_status=40501)) == 1
    assert len(store.read_jsonl(args.out / "cost_log.jsonl")) == 1
    assert not (args.out / "volume-lookup.json").exists()
    assert keywords_of(args) == [{"text": "old keyword", "volume": 0}]


def test_candidates_are_checked_before_any_call(tmp_path):
    path = tmp_path / "candidates.txt"
    path.write_text("black friday deals\nBlack  Friday deals\n", encoding="utf-8")
    with pytest.raises(PanelError, match="listed twice"):
        volumes.candidates(path)
    path.write_text(" ".join(["deal"] * 11) + "\n", encoding="utf-8")
    with pytest.raises(PanelError, match="10 words at most"):
        volumes.candidates(path)


def test_a_panel_without_a_keywords_block_gets_one(build):
    args = build(panel="panel_id: p\nmarket: {country: US, language: en}\n")
    volumes.main(args, post=FakePost())
    assert len(keywords_of(args)) == 3


def test_the_filled_panel_freezes(build):
    args = build(panel=yaml.safe_dump({**yaml.safe_load(open_panel()), "keywords": []}, sort_keys=False))
    volumes.main(args, post=FakePost())
    panel_file.freeze(args.panel)
    assert [k.text for k in panel_file.load(args.panel).keywords] == [
        "black friday deals", "black friday tv deals", "black friday sales"]


def open_panel() -> str:
    from scan.tests.conftest import panel_dict
    return yaml.safe_dump(panel_dict(), sort_keys=False)
