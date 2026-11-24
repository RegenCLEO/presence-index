import argparse
import re

import pytest
import yaml

from scan import panel as panel_file, reviews, store
from scan.panel import Brand, PanelError
from scan.records import EngineError
from scan.store import StoreError

POST = "business_data/trustpilot/reviews/task_post"
GET = "business_data/trustpilot/reviews/task_get/"
EARLIER = "2026-11-22T10:35:24Z"


def profile(rating, count, **extra):
    """A finished task's result, in the shape the vendor returns for a Trustpilot profile."""
    return {"domain": "x.example", "type": "trustpilot_reviews", "reviews_count": count,
            "rating": {"rating_type": "Max5", "value": rating, "votes_count": None, "rating_max": 5},
            "items_count": 1, "items": [{"type": "trustpilot_review", "rating": {"value": 5}}], **extra}


def task_reply(code, result=None, message="Ok."):
    return {"status_code": 20000, "tasks": [{"status_code": code, "status_message": message, "cost": 0,
                                             "result": [result] if result is not None else None}]}


IN_QUEUE = task_reply(40602, message="Task In Queue.")
NOT_FOUND = task_reply(40102, message="No Search Results.")


class Vendor:
    """Stands in for DataForSEO. ``answers`` maps a domain to the replies its result checks
    get, in order. The last reply repeats, and an exception in the list is raised."""

    def __init__(self, answers, cost=0.00075):
        self.answers, self.cost = answers, cost
        self.posts, self.gets, self.sleeps = [], [], []
        self.domains, self.checks = {}, {}

    def post(self, endpoint, body):
        self.posts.append((endpoint, body))
        task_id = f"task-{len(self.posts)}"
        self.domains[task_id] = body[0]["domain"]
        return {"status_code": 20000, "tasks": [{"id": task_id, "status_code": 20100,
                                                 "status_message": "Task Created.", "cost": self.cost}]}

    def get(self, endpoint):
        self.gets.append(endpoint)
        task_id = endpoint[len(GET):]
        script = self.answers[self.domains[task_id]]
        n = self.checks.get(task_id, 0)
        self.checks[task_id] = n + 1
        reply = script[min(n, len(script) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    @property
    def posted(self):
        return [body[0]["domain"] for _, body in self.posts]


# Volt City is the one brand with a second domain, volt.example.
ANSWERS = {
    "northmart.example": [task_reply(20000, profile(1.7, 41234))],
    "target.example": [NOT_FOUND],
    "gap.example": [task_reply(20000, profile(4.6, 250))],
    "voltcity.example": [task_reply(20000, profile(2.1, 18))],
    "volt.example": [task_reply(20000, profile(1.4, 600))],
}


def args_for(stage_args, panel_path, **kw):
    return stage_args(panel_path, **{"go": True, "attempts": 1, "poll_attempts": 3, "poll_interval": 5.0, **kw})


def run(args, vendor):
    return reviews.main(args, post=vendor.post, get=vendor.get, sleep=vendor.sleep)


def stored(args):
    return store.read_json(args.out / "reviews.json")


def checked(row):
    return [(e["profile"], e["status"], e["rating"], e["count"]) for e in row["profiles_checked"]]


def earlier_row(domain, status="ok", rating=None, count=0, mean=None, **extra):
    """A row as the stage wrote it before brands were looked up on every domain."""
    found = status == "ok"
    return {"status": status, "reason": {"ok": None, "no_profile": "no_trustpilot_profile"}.get(status, "task_pending"),
            "domain": domain, "n": count, "mean_rating": mean if mean is not None else rating, "fetched_at": EARLIER,
            "platforms": [{"platform": "trustpilot", "profile": domain, "rating": rating, "count": count}] if found else [],
            **extra}


def write_earlier_file(args, rows):
    panel = panel_file.load(args.panel)
    store.write_json(args.out / "reviews.json", {"panel_id": panel.panel_id, "panel_sha256": panel.sha256, "brands": rows})


# ---------- the request and the file ----------
def test_one_brand_is_one_post_and_one_result_check(make_panel, stage_args):
    vendor = Vendor(ANSWERS)
    args = args_for(stage_args, make_panel(brands=1))
    assert run(args, vendor) == 0
    assert vendor.posts == [(POST, [{"domain": "northmart.example", "depth": 1}])]
    assert vendor.gets == [GET + "task-1"]
    assert vendor.sleeps == []

    out = stored(args)
    assert set(out) == {"panel_id", "panel_sha256", "brands"}
    assert out["panel_id"] == "test-retailers-us-en" and len(out["panel_sha256"]) == 64
    row = out["brands"]["northmart"]
    at = row.pop("fetched_at")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", at)
    assert row == {
        "status": "ok", "reason": None,
        "profiles_checked": [{"profile": "northmart.example", "status": "ok", "rating": 1.7, "count": 41234,
                              "fetched_at": at}],
        "platforms": [{"platform": "trustpilot", "profile": "northmart.example", "rating": 1.7, "count": 41234}],
        "n": 41234, "mean_rating": 1.7}


def test_each_status_is_stored_and_a_rerun_makes_no_calls(make_panel, stage_args):
    answers = {**ANSWERS, "voltcity.example": [task_reply(20000, profile(None, 0, items_count=0, items=None))],
               "volt.example": [NOT_FOUND]}
    vendor = Vendor(answers)
    args = args_for(stage_args, make_panel())
    assert run(args, vendor) == 0
    assert vendor.posted == ["northmart.example", "target.example", "gap.example", "voltcity.example", "volt.example"]
    brands = stored(args)["brands"]
    assert {b: (r["status"], r["reason"], r["n"], r["mean_rating"]) for b, r in brands.items()} == {
        "northmart": ("ok", None, 41234, 1.7),
        "target": ("no_profile", "no_trustpilot_profile", 0, None),
        "gap": ("ok", None, 250, 4.6),
        "voltcity": ("ok", None, 0, None),       # a profile with no reviews is read, not missing
    }
    assert brands["target"]["platforms"] == []
    assert checked(brands["target"]) == [("target.example", "no_profile", None, 0)]
    assert brands["voltcity"]["platforms"] == [
        {"platform": "trustpilot", "profile": "voltcity.example", "rating": None, "count": 0}]

    again = Vendor(answers)
    assert run(args, again) == 0
    assert again.posts == [] and again.gets == []
    assert stored(args)["brands"] == brands


def test_the_cost_the_vendor_reports_is_logged_once_per_posted_task(make_panel, stage_args):
    args = args_for(stage_args, make_panel(brands=2))
    assert run(args, Vendor(ANSWERS, cost=0.00075)) == 0
    costs = store.read_jsonl(args.out / "cost_log.jsonl")
    assert [(c["purpose"], c["vendor"], c["ref"], c["model"], c["cost_usd"], c["basis"]) for c in costs] == [
        ("reviews", "dataforseo", "northmart", POST, 0.00075, "vendor"),
        ("reviews", "dataforseo", "target", POST, 0.00075, "vendor")]


def test_without_go_the_calls_are_printed_and_nothing_is_asked(make_panel, stage_args, capsys):
    vendor = Vendor(ANSWERS)
    args = args_for(stage_args, make_panel(), go=False)
    assert run(args, vendor) == 0
    assert vendor.posts == [] and vendor.gets == []
    assert not (args.out / "reviews.json").exists()
    printed = capsys.readouterr().out
    assert "4 brands with 5 domains, 0 already stored, 5 Trustpilot lookups to make" in printed
    assert f'northmart (northmart.example): POST {POST} [{{"domain": "northmart.example", "depth": 1}}]' in printed
    assert f'voltcity (volt.example): POST {POST} [{{"domain": "volt.example", "depth": 1}}]' in printed
    assert "dry run: nothing was asked" in printed


def test_a_brand_added_after_the_scan_is_looked_up(make_panel, stage_args):
    args = args_for(stage_args, make_panel(brands=1))
    (args.out / "additions.yaml").write_text(yaml.safe_dump({"brands": [
        {"id": "shopzone", "name": "ShopZone", "domain": "shopzone.example", "confirmed_by": "A Person"}]}))
    vendor = Vendor({**ANSWERS, "shopzone.example": [task_reply(20000, profile(3.9, 12))]})
    assert run(args, vendor) == 0
    assert stored(args)["brands"]["shopzone"]["n"] == 12

    # A brand whose confirmed domain changes is looked up again under the new one.
    (args.out / "additions.yaml").write_text(yaml.safe_dump({"brands": [
        {"id": "shopzone", "name": "ShopZone", "domain": "shopzone.test", "confirmed_by": "A Person"}]}))
    vendor = Vendor({"shopzone.test": [NOT_FOUND]})
    assert run(args, vendor) == 0
    assert vendor.posted == ["shopzone.test"]
    row = stored(args)["brands"]["shopzone"]
    assert row["status"] == "no_profile" and checked(row) == [("shopzone.test", "no_profile", None, 0)]


def test_a_file_from_another_panel_is_refused(make_panel, stage_args):
    args = args_for(stage_args, make_panel(brands=1))
    store.write_json(args.out / "reviews.json", {"panel_id": "other", "panel_sha256": "0" * 64, "brands": {}})
    vendor = Vendor(ANSWERS)
    with pytest.raises(StoreError, match="written under another panel"):
        run(args, vendor)
    assert vendor.posts == []


# ---------- a brand with more than one domain ----------
def test_the_profile_with_the_most_reviews_is_kept_and_profiles_are_not_added(make_panel, stage_args):
    vendor = Vendor(ANSWERS)
    args = args_for(stage_args, make_panel())
    assert run(args, vendor) == 0
    assert vendor.posted[-2:] == ["voltcity.example", "volt.example"]
    row = stored(args)["brands"]["voltcity"]
    assert checked(row) == [("voltcity.example", "ok", 2.1, 18), ("volt.example", "ok", 1.4, 600)]
    assert row["platforms"] == [{"platform": "trustpilot", "profile": "volt.example", "rating": 1.4, "count": 600}]
    assert (row["status"], row["n"], row["mean_rating"]) == ("ok", 600, 1.4)


def test_a_domain_that_fails_fails_the_brand_and_a_rerun_asks_only_for_that_domain(make_panel, stage_args):
    args = args_for(stage_args, make_panel())
    broken = Vendor({**ANSWERS, "volt.example": [task_reply(50000, message="Internal Error.")]})
    assert run(args, broken) == 1
    row = stored(args)["brands"]["voltcity"]
    assert (row["status"], row["reason"], row["platforms"], row["n"], row["mean_rating"]) == (
        "failed", "task_error", [], 0, None)
    assert checked(row) == [("voltcity.example", "ok", 2.1, 18), ("volt.example", "failed", None, 0)]
    assert row["profiles_checked"][1]["detail"] == "DataForSEO task error 50000: Internal Error."

    rerun = Vendor(ANSWERS)
    assert run(args, rerun) == 0
    assert rerun.posted == ["volt.example"]
    assert (stored(args)["brands"]["voltcity"]["status"], stored(args)["brands"]["voltcity"]["n"]) == ("ok", 600)


VOLT = Brand(id="voltcity", name="Volt City", domain="voltcity.example", other_domains=("volt.example",))


@pytest.mark.parametrize("main, other, status, chosen, n", [
    (("ok", 2.1, 18), ("ok", 1.4, 600), "ok", "volt.example", 600),
    (("ok", 2.1, 600), ("ok", 1.4, 600), "ok", "voltcity.example", 600),        # a tie goes to the main domain
    (("ok", 2.1, 18), ("no_profile", None, 0), "ok", "voltcity.example", 18),
    (("no_profile", None, 0), ("ok", 4.0, 3), "ok", "volt.example", 3),
    (("ok", None, 900), ("ok", 4.0, 3), "ok", "volt.example", 3),               # no rating counts as no reviews
    (("ok", None, 0), ("no_profile", None, 0), "ok", "voltcity.example", 0),
    (("no_profile", None, 0), ("no_profile", None, 0), "no_profile", None, 0),
    (("ok", 2.1, 18), ("failed", None, 0), "failed", None, 0),
    (("ok", 2.1, 18), None, "failed", None, 0),                                 # a domain not yet looked up
])
def test_a_brand_row_from_the_lookups_of_its_domains(main, other, status, chosen, n):
    lookups = {d: reviews.lookup(d, e[0], e[1], e[2], **({"reason": "task_error"} if e[0] == "failed" else {}))
               for d, e in zip(VOLT.domains, (main, other)) if e}
    row = reviews.brand_row(VOLT, lookups)
    assert (row["status"], row["n"]) == (status, n)
    assert [p["profile"] for p in row["platforms"]] == ([chosen] if chosen else [])
    assert [e["profile"] for e in row["profiles_checked"]] == list(lookups)
    assert row["reason"] == {"ok": None, "no_profile": "no_trustpilot_profile"}.get(
        status, "task_error" if other else "not_looked_up")


def test_a_domain_the_brand_no_longer_has_is_dropped_without_a_call(make_panel, stage_args):
    args = args_for(stage_args, make_panel(brands=1))
    added = {"id": "shopzone", "name": "ShopZone", "domain": "shopzone.example", "confirmed_by": "A Person"}
    (args.out / "additions.yaml").write_text(yaml.safe_dump({"brands": [{**added, "other_domains": ["zone.example"]}]}))
    answers = {**ANSWERS, "shopzone.example": [task_reply(20000, profile(3.9, 12))],
               "zone.example": [task_reply(20000, profile(2.0, 90))]}
    assert run(args, Vendor(answers)) == 0
    assert stored(args)["brands"]["shopzone"]["platforms"][0]["profile"] == "zone.example"

    (args.out / "additions.yaml").write_text(yaml.safe_dump({"brands": [added]}))
    vendor = Vendor(answers)
    assert run(args, vendor) == 0
    assert vendor.posts == [] and vendor.gets == []
    row = stored(args)["brands"]["shopzone"]
    assert checked(row) == [("shopzone.example", "ok", 3.9, 12)]
    assert (row["platforms"][0]["profile"], row["n"], row["mean_rating"]) == ("shopzone.example", 12, 3.9)


# ---------- a file written before brands were looked up on every domain ----------
def test_an_earlier_file_costs_one_lookup_for_the_one_domain_it_does_not_hold(make_panel, stage_args, capsys):
    args = args_for(stage_args, make_panel())
    earlier = {
        "northmart": earlier_row("northmart.example", rating=3.7, count=2504, mean=3.7000000000000006),
        "target": earlier_row("target.example", status="no_profile"),
        "gap": earlier_row("gap.example", rating=1.9, count=389, mean=1.8999999999999997),
        "voltcity": earlier_row("voltcity.example", rating=2.1, count=18),
    }
    write_earlier_file(args, earlier)

    assert reviews.main(args_for(stage_args, args.panel, go=False)) == 0
    assert "4 brands with 5 domains, 4 already stored, 1 Trustpilot lookups to make" in capsys.readouterr().out
    assert stored(args)["brands"] == earlier        # a dry run rewrites nothing

    vendor = Vendor(ANSWERS)
    assert run(args, vendor) == 0
    assert vendor.posted == ["volt.example"] and vendor.gets == [GET + "task-1"]
    brands = stored(args)["brands"]
    assert all("domain" not in row for row in brands.values())
    assert {b: (r["status"], r["n"], r["mean_rating"]) for b, r in brands.items()} == {
        "northmart": ("ok", 2504, 3.7), "target": ("no_profile", 0, None), "gap": ("ok", 389, 1.9),
        "voltcity": ("ok", 600, 1.4)}
    # A row no call touched keeps the time it was read.
    assert brands["northmart"]["fetched_at"] == brands["northmart"]["profiles_checked"][0]["fetched_at"] == EARLIER
    assert checked(brands["voltcity"]) == [("voltcity.example", "ok", 2.1, 18), ("volt.example", "ok", 1.4, 600)]
    assert brands["voltcity"]["profiles_checked"][0]["fetched_at"] == EARLIER
    assert brands["voltcity"]["fetched_at"] == brands["voltcity"]["profiles_checked"][1]["fetched_at"] != EARLIER

    again = Vendor(ANSWERS)
    assert run(args, again) == 0
    assert again.posts == [] and again.gets == []
    assert stored(args)["brands"] == brands


def test_an_earlier_row_with_an_unfinished_task_is_collected(make_panel, stage_args):
    args = args_for(stage_args, make_panel(brands=1))
    write_earlier_file(args, {"northmart": earlier_row("northmart.example", status="failed", task_id="task-9",
                                                       detail="no result after 60 checks")})
    vendor = Vendor(ANSWERS)
    vendor.domains["task-9"] = "northmart.example"
    assert run(args, vendor) == 0
    assert vendor.posts == [] and vendor.gets == [GET + "task-9"]
    assert stored(args)["brands"]["northmart"]["n"] == 41234


# ---------- waiting for a task ----------
def test_every_task_is_posted_before_any_is_checked_and_waits_use_the_given_sleep(make_panel, stage_args):
    slow = [IN_QUEUE, IN_QUEUE, task_reply(20000, profile(4.6, 250))]
    vendor = Vendor({"northmart.example": ANSWERS["northmart.example"], "target.example": slow})
    args = args_for(stage_args, make_panel(brands=2), poll_attempts=5, poll_interval=7.0)
    assert run(args, vendor) == 0
    assert len(vendor.posts) == 2
    assert vendor.gets == [GET + "task-1", GET + "task-2", GET + "task-2", GET + "task-2"]
    assert vendor.sleeps == [7.0, 7.0]
    assert stored(args)["brands"]["target"]["n"] == 250


def test_a_finished_task_with_no_result_yet_and_a_failed_check_are_asked_again(make_panel, stage_args):
    script = [task_reply(20000), EngineError("ReadTimeout: slow"), {"status_code": 20000, "tasks": []},
              task_reply(20000, profile(4.0, 10))]
    vendor = Vendor({"northmart.example": script})
    args = args_for(stage_args, make_panel(brands=1), poll_attempts=4)
    assert run(args, vendor) == 0
    assert len(vendor.gets) == 4
    assert stored(args)["brands"]["northmart"]["status"] == "ok"


def test_a_task_that_does_not_finish_is_collected_by_the_next_run_without_a_new_post(make_panel, stage_args,
                                                                                     capsys):
    args = args_for(stage_args, make_panel(brands=1), poll_attempts=2)
    first = Vendor({"northmart.example": [IN_QUEUE]})
    assert run(args, first) == 1
    assert len(first.gets) == 2 and first.sleeps == [5.0]
    row = stored(args)["brands"]["northmart"]
    assert (row["status"], row["reason"], row["platforms"], row["n"]) == ("failed", "task_pending", [], 0)
    found = row["profiles_checked"][0]
    assert (found["status"], found["reason"], found["task_id"]) == ("failed", "task_pending", "task-1")
    assert found["detail"] == "no result after 2 checks"

    capsys.readouterr()
    assert reviews.main(args_for(stage_args, args.panel, go=False)) == 0
    assert f"northmart (northmart.example): GET {GET}task-1 until the task ends" in capsys.readouterr().out

    second = Vendor(ANSWERS)
    second.domains["task-1"] = "northmart.example"
    assert run(args, second) == 0
    assert second.posts == [] and second.gets == [GET + "task-1"]
    row = stored(args)["brands"]["northmart"]
    assert (row["status"], row["n"]) == ("ok", 41234)
    assert "task_id" not in row["profiles_checked"][0]
    assert len(store.read_jsonl(args.out / "cost_log.jsonl")) == 1


def test_a_task_that_outlasts_two_runs_is_dropped_and_posted_again(make_panel, stage_args):
    args = args_for(stage_args, make_panel(brands=1), poll_attempts=1)
    assert run(args, Vendor({"northmart.example": [IN_QUEUE]})) == 1

    second = Vendor({"northmart.example": [IN_QUEUE]})
    second.domains["task-1"] = "northmart.example"
    assert run(args, second) == 1
    row = stored(args)["brands"]["northmart"]
    assert (row["status"], row["reason"]) == ("failed", "poll_timeout")
    assert "task_id" not in row["profiles_checked"][0]

    third = Vendor(ANSWERS)
    assert run(args, third) == 0
    assert len(third.posts) == 1
    assert stored(args)["brands"]["northmart"]["status"] == "ok"


# ---------- failures ----------
def test_a_post_that_fails_is_tried_again_then_left_for_a_rerun(make_panel, stage_args):
    args = args_for(stage_args, make_panel(brands=2), attempts=3)
    vendor = Vendor(ANSWERS)
    tries = []

    def post(endpoint, body):
        if body[0]["domain"] == "northmart.example":
            tries.append(body)
            raise EngineError("ConnectError: no route")
        return vendor.post(endpoint, body)

    assert reviews.main(args, post=post, get=vendor.get, sleep=vendor.sleep) == 1
    assert len(tries) == 3 and vendor.sleeps == [2, 4]
    brands = stored(args)["brands"]
    assert (brands["northmart"]["status"], brands["northmart"]["reason"]) == ("failed", "post_failed")
    found = brands["northmart"]["profiles_checked"][0]
    assert "ConnectError" in found["detail"] and "task_id" not in found
    assert brands["target"]["status"] == "no_profile"

    rerun = Vendor(ANSWERS)
    assert run(args, rerun) == 0
    assert rerun.posted == ["northmart.example"]


def test_a_task_the_vendor_refuses_is_failed_and_its_cost_is_still_logged(make_panel, stage_args):
    args = args_for(stage_args, make_panel(brands=1))

    def post(endpoint, body):
        return {"status_code": 20000, "tasks": [{"id": "t", "status_code": 40501,
                                                 "status_message": "Invalid Field: 'domain'.", "cost": 0}]}

    vendor = Vendor(ANSWERS)
    assert reviews.main(args, post=post, get=vendor.get, sleep=vendor.sleep) == 1
    row = stored(args)["brands"]["northmart"]
    assert (row["status"], row["reason"]) == ("failed", "post_failed")
    assert "40501" in row["profiles_checked"][0]["detail"] and vendor.gets == []
    assert [c["cost_usd"] for c in store.read_jsonl(args.out / "cost_log.jsonl")] == [0.0]


def test_a_task_that_ends_in_an_error_is_failed_and_posted_again_on_a_rerun(make_panel, stage_args):
    args = args_for(stage_args, make_panel(brands=1))
    vendor = Vendor({"northmart.example": [task_reply(50000, message="Internal Error.")]})
    assert run(args, vendor) == 1
    row = stored(args)["brands"]["northmart"]
    assert (row["status"], row["reason"], row["platforms"], row["n"], row["mean_rating"]) == (
        "failed", "task_error", [], 0, None)
    found = row["profiles_checked"][0]
    assert found["detail"] == "DataForSEO task error 50000: Internal Error." and "task_id" not in found

    rerun = Vendor(ANSWERS)
    assert run(args, rerun) == 0
    assert len(rerun.posts) == 1


# ---------- pure rules ----------
@pytest.mark.parametrize("reply, outcome", [
    (task_reply(20000, profile(4.2, 54)), "ok"),
    (NOT_FOUND, "no_profile"),
    (IN_QUEUE, "pending"),
    (task_reply(40601, message="Task Handed."), "pending"),
    (task_reply(20100, message="Task Created."), "pending"),
    (task_reply(20000), "pending"),
    (task_reply(20000, {}), "pending"),
    ({"tasks": []}, "pending"),
    ({}, "pending"),
    (task_reply(40501, message="Invalid Field."), "failed"),
])
def test_what_a_result_check_says(reply, outcome):
    assert reviews.read_task(reply)[0] == outcome


@pytest.mark.parametrize("result, expected", [
    (profile(4.2, 54), (4.2, 54)),
    (profile("4.2", "54"), (4.2, 54)),
    (profile(3.7000000000000006, 2504), (3.7, 2504)),                                     # stored to 2 decimals
    (profile(4.256, 10), (4.26, 10)),
    ({"rating": {"value": 3.1, "votes_count": 900}, "reviews_count": 880}, (3.1, 900)),   # votes_count comes first
    ({"rating": {"value": 3.1, "votes_count": None}, "items_count": 7}, (3.1, 7)),        # then the reviews returned
    ({"rating": None, "reviews_count": 12}, (None, 12)),
    ({"rating": {"value": "n/a"}, "reviews_count": "many"}, (None, 0)),
    ({}, (None, 0)),
])
def test_the_rating_and_the_count_of_a_profile(result, expected):
    assert reviews.header(result) == expected


def test_platforms_pool_to_the_sum_of_counts_and_the_count_weighted_mean():
    rows = [{"platform": "a", "rating": 4.0, "count": 300}, {"platform": "b", "rating": 2.0, "count": 100},
            {"platform": "c", "rating": None, "count": 50}, {"platform": "d", "rating": 5.0, "count": 0}]
    assert reviews.pooled(rows) == (400, 3.5)
    assert reviews.pooled([]) == (0, None)
    assert reviews.pooled(rows[2:]) == (0, None)
    # 1.9 * 389 / 389 is 1.8999999999999997 before it is rounded.
    assert reviews.pooled([{"platform": "a", "rating": 1.9, "count": 389}]) == (389, 1.9)
    assert reviews.pooled([{"platform": "a", "rating": 4.0, "count": 2}, {"platform": "b", "rating": 3.0, "count": 1}]) == (
        3, 3.67)


def test_the_command_line_defaults_are_the_ones_the_stage_reads():
    parser = argparse.ArgumentParser()
    reviews.add_arguments(parser)
    assert vars(parser.parse_args([])) == {"go": False, "attempts": 3, "poll_attempts": 60, "poll_interval": 5.0}
    assert parser.parse_args(["--go", "--poll-attempts", "4"]).poll_attempts == 4


def test_the_real_get_asks_for_credentials_before_any_call(monkeypatch):
    monkeypatch.delenv("DATAFORSEO_LOGIN", raising=False)
    monkeypatch.delenv("DATAFORSEO_PASSWORD", raising=False)
    with pytest.raises(PanelError, match="DATAFORSEO_LOGIN"):
        reviews.dataforseo_get()
