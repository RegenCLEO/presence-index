import json

import reference

from scan import sensitivity
from scan.tests.test_rank import brand, scores_of


def scored(name, rec, crawl):
    """A brand measured on AI Recommendation and Crawl and Render Access only, both without error."""
    b = brand(name, 0.0)
    b["signals"] = {sid: {"state": "not_applicable", "score": None, "se": 0.0} for sid in reference.REGISTRY}
    b["signals"]["rec.ai_recommendation"] = {"state": "live", "score": rec, "se": 0.0}
    b["signals"]["rdy.crawl_access"] = {"state": "live", "score": crawl, "se": 0.0}
    b["raw_index"] = (24 * rec + 9 * crawl) / 33
    b["index"], b["raw_range"] = round(b["raw_index"], 1), [b["raw_index"], b["raw_index"]]
    return b


def pick(full, signal, factor):
    return next(t for t in full["tests"] if t["signal"] == signal and t["factor"] == factor)


# Base: Beta (70, 80) is 72.73 and Alpha (80, 50) is 71.82. With AI Recommendation at 30
# Alpha is (2400 + 450) / 39 = 73.08 and Beta is (2100 + 720) / 39 = 72.31; at 18 Beta stays first.
SCORES = scores_of(scored("Alpha", 80, 50), scored("Beta", 70, 80))


def test_each_weight_moves_a_quarter_either_way():
    full, _ = sensitivity.sensitivity(SCORES, "hash")

    assert len(full["tests"]) == 2 * len(reference.REGISTRY)
    raised = pick(full, "rec.ai_recommendation", 1.25)
    assert (raised["weight"], raised["tested_weight"]) == (24, 30.0)


def test_a_heavier_weight_that_reorders_the_brands_is_reported():
    full, _ = sensitivity.sensitivity(SCORES, "hash")

    raised = pick(full, "rec.ai_recommendation", 1.25)
    assert [(r["name"], r["rank_label"], r["tested_rank_label"]) for r in raised["rows"]] == [
        ("Beta", "1", "2"), ("Alpha", "2", "1")]
    assert raised["ranks_changed"] == 2 and raised["leaders_changed"] and raised["order_changed"]
    assert raised["largest_index_change"] == 1.26  # Alpha, 71.82 to 73.08
    lowered = pick(full, "rec.ai_recommendation", 0.75)
    assert lowered["ranks_changed"] == 0 and not lowered["leaders_changed"]
    # Crawl and Render Access at 6.75 favours Alpha the same way: 73.41 against 72.20.
    assert [(t["signal"], t["factor"]) for t in full["tests"] if t["leaders_changed"]] == [
        ("rec.ai_recommendation", 1.25), ("rdy.crawl_access", 0.75)]
    assert full["summary"]["tests_changing_leaders"] == 2


def test_signals_a_brand_does_not_have_change_nothing():
    full, _ = sensitivity.sensitivity(SCORES, "hash")

    reviews = pick(full, "rec.reviews", 0.75)
    assert reviews["largest_index_change"] == 0.0 and reviews["ranks_changed"] == 0


def test_the_public_file_names_only_published_brands(monkeypatch):
    monkeypatch.setattr(sensitivity, "PUBLIC_ROWS", 1)

    full, public = sensitivity.sensitivity(SCORES, "hash")

    raised = pick(public, "rec.ai_recommendation", 1.25)
    assert [r["name"] for r in raised["rows"]] == ["Beta"]
    assert raised["unpublished_brands_entering_public_rows"] == 1
    assert "Alpha" not in json.dumps(public)
    assert public["sensitivity_sha256"] == full["sensitivity_sha256"]


def test_the_same_scores_give_the_same_bytes():
    assert sensitivity.sensitivity(SCORES, "hash") == sensitivity.sensitivity(SCORES, "hash")
