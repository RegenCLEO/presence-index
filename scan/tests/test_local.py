import pytest
import yaml

import reference
from scan import local, panel as panel_file, store
from scan.panel import Brand, PanelError
from scan.store import StoreError
from scan.tests.conftest import panel_dict

BRANDS = [
    {"id": "northmart", "name": "Northmart", "domain": "northmart.example", "aliases": ["North Mart"]},
    {"id": "target", "name": "Target", "domain": "target.example"},
    {"id": "webonly", "name": "Webonly Market", "domain": "webonly.example", "local": False},
]

OPEN = {"timetable": {"monday": [{"open": {"hour": 9, "minute": 0}, "close": {"hour": 21, "minute": 0}}],
                      "sunday": None}, "current_status": "open"}


def place(title, url=None, **fields):
    return {"type": "maps_search", "title": title, "url": url, **fields}


NORTHMART_STORE = place(
    "Northmart Supercenter", "https://www.northmart.example/store/12", domain="www.northmart.example",
    address="12 Main Street, Springfield, IL 62701", phone="+1 217-555-0199", category="Department store",
    work_hours=OPEN, place_id="ChIJnorth", cid="111", rating={"value": 4.2, "votes_count": "1530"})
NORTHMART_PLACES = [
    place("Southmart", "https://www.southmart.example/"),
    place("Northmart Fans Club", "https://www.fans.example/"),
    place("Northmart"),
    {"type": "maps_paid_item", "title": "Northmart", "url": "https://www.northmart.example/"},
    NORTHMART_STORE,
    place("North Mart Express", "https://northmart.example/express", cid="112"),
]
TARGET_PLACES = [place("Target Optical", "https://www.targetoptical.example/"), place("Target")]


def reply(items, cost=0.002):
    return {"status_code": 20000, "tasks": [{"status_code": 20000, "cost": cost, "result": [{"items": items}]}]}


class FakePost:
    """Answers each brand's Maps search from a table and records every call."""

    def __init__(self, **places):
        self.calls = []
        self.places = {"Northmart": NORTHMART_PLACES, "Target": TARGET_PLACES, **places}

    def __call__(self, endpoint, body):
        self.calls.append((endpoint, body))
        return reply(self.places[body[0]["keyword"]])


@pytest.fixture
def scan(tmp_path, stage_args):
    """Run the stage on a panel of the given brands. Returns (exit code, the brands in local.json)."""
    panel_path = tmp_path / "panel.yaml"

    def write_panel(brands=BRANDS, **overrides):
        panel_path.write_text(yaml.safe_dump({**panel_dict(), "brands": brands, **overrides}, sort_keys=False),
                              encoding="utf-8")
        return panel_file.freeze(panel_path)

    def run(post, **kw):
        if not panel_path.exists():
            write_panel()
        args = stage_args(panel_path, **{"go": True, "attempts": 1, "location_code": None, **kw})
        code = local.main(args, post=post, sleep=lambda s: None)
        path = args.out / "local.json"
        return code, store.read_json(path)["brands"] if path.exists() else None

    run.write_panel = write_panel
    run.out = tmp_path / "out"
    return run


def brand(**kw):
    return Brand(**{"id": "northmart", "name": "Northmart", "domain": "northmart.example", **kw})


# ---------- the rules ----------
def test_a_title_matches_a_name_it_equals_or_holds_from_six_characters():
    assert local.titles_match("Northmart", "NORTHMART")
    assert local.titles_match("Northmart", "Northmart Supercenter")
    assert local.titles_match("Northmart Stores Inc.", "Northmart")
    assert local.titles_match("Gap", "GAP")
    assert not local.titles_match("Gap", "Gap Factory")
    assert not local.titles_match("Northmart", "Southmart")
    assert not local.titles_match("Northmart", None)


def test_the_listing_is_the_first_place_with_the_brands_name_and_its_own_website():
    places = [p for p in NORTHMART_PLACES if p["type"] == "maps_search"]
    assert local.own_listing(places, brand()) is NORTHMART_STORE


def test_a_title_with_no_website_or_with_another_website_is_not_the_listing():
    places = [place("Northmart"), place("Northmart", "https://www.fans.example/northmart"),
              place("Southmart", "https://www.northmart.example/")]
    assert local.own_listing(places, brand()) is None


def test_a_short_name_may_start_the_title_of_a_place_on_the_brands_own_site():
    lowes = Brand(id="lowes", name="Lowe's", domain="lowes.example")
    store = place("Lowe\u2019s Home Improvement", "https://www.lowes.example/store/OK-Tulsa/0241")
    assert local.own_listing([store], lowes) is store
    assert local.own_listing([place("Lowe's Home Improvement", "https://www.fans.example/")], lowes) is None
    assert local.own_listing([place("Gallowes Inn", "https://www.lowes.example/")], lowes) is None


def test_an_alias_in_the_title_and_a_bare_domain_are_enough():
    express = place("NM Express", domain="northmart.example")
    assert local.own_listing([express], brand(aliases=("NM Express",))) is express
    assert local.own_listing([express], brand()) is None


def test_a_site_under_another_brands_domain_is_not_the_brands_website():
    gap, oldnavy = panel_file.carve([Brand(id="gap", name="Gap Stores", domain="gap.example"),
                                     Brand(id="oldnavy", name="Old Navy", domain="oldnavy.gap.example")])
    shared = place("Old Navy by Gap Stores", "https://oldnavy.gap.example/stores/1")
    assert local.own_listing([shared], gap) is None
    assert local.own_listing([shared], oldnavy) is shared


@pytest.mark.parametrize("work_hours, expected", [
    (OPEN, True),
    ({"timetable": {"monday": None, "tuesday": []}}, False),
    ({"timetable": None, "current_status": "open"}, False),
    (None, None),
    ("always", None),
])
def test_hours_are_present_when_a_day_has_an_opening_period(work_hours, expected):
    assert local.has_hours(work_hours) is expected


def test_the_facts_kept_of_a_listing():
    assert local.listing_facts(NORTHMART_STORE) == {
        "title": "Northmart Supercenter", "address": "12 Main Street, Springfield, IL 62701",
        "phone": "+1 217-555-0199", "category": "Department store", "has_hours": True,
        "place_id": "ChIJnorth", "cid": "111", "rating": 4.2, "ratings_count": 1530,
        "website": "https://www.northmart.example/store/12"}
    bare = local.listing_facts(place("Northmart", domain="northmart.example", cid=987, rating=None,
                                     category=" ", work_hours={"timetable": {}}))
    assert bare == {"title": "Northmart", "address": None, "phone": None, "category": None, "has_hours": False,
                    "place_id": None, "cid": "987", "rating": None, "ratings_count": None,
                    "website": "northmart.example"}


def test_the_listing_url_comes_from_the_cid_or_else_the_place_id():
    assert local.listing_url({"cid": "111", "place_id": "ChIJ"}) == "https://maps.google.com/?cid=111"
    assert local.listing_url({"cid": None, "place_id": "ChIJ"}) == (
        "https://www.google.com/maps/place/?q=place_id:ChIJ")
    assert local.listing_url({"cid": None, "place_id": None}) is None


# ---------- the stage ----------
def test_one_maps_search_per_local_brand_and_none_for_a_brand_with_no_location(scan):
    post = FakePost()
    code, brands = scan(post)
    assert code == 0
    assert post.calls == [
        ("serp/google/maps/live/advanced", [{"keyword": "Northmart", "location_code": 2840, "language_code": "en"}]),
        ("serp/google/maps/live/advanced", [{"keyword": "Target", "location_code": 2840, "language_code": "en"}]),
    ]
    assert brands["webonly"] == {
        "status": "not_applicable", "reason": "no_location", "directory_set": None, "directories": {},
        "presence_share": None, "nap_share": None, "hours_category_share": None,
        "fetched_at": brands["webonly"]["fetched_at"]}


def test_the_row_of_a_brand_with_a_listing(scan):
    _, brands = scan(FakePost())
    row = brands["northmart"]
    assert row.pop("fetched_at").endswith("Z")
    assert row == {
        "status": "ok", "reason": None, "directory_set": "google_only",
        "directories": {"google": {"listed": True, "url": "https://maps.google.com/?cid=111",
                                   "mismatch_fields": []}},
        "presence_share": 1.0, "nap_share": 1.0, "hours_category_share": 1.0,
        "nap_check": {"compared": False, "reason": "no_crawled_profile"},
        "listing": {"title": "Northmart Supercenter", "address": "12 Main Street, Springfield, IL 62701",
                    "phone": "+1 217-555-0199", "category": "Department store", "has_hours": True,
                    "place_id": "ChIJnorth", "cid": "111", "rating": 4.2, "ratings_count": 1530,
                    "website": "https://www.northmart.example/store/12"},
        "search": {"keyword": "Northmart", "location_code": 2840, "language_code": "en", "places": 5,
                   "title_matches": 4,
                   "near_misses": [{"title": "Northmart Fans Club", "website": "https://www.fans.example/"},
                                   {"title": "Northmart", "website": None}]},
    }
    assert reference.local_listings_score(row["presence_share"], row["nap_share"],
                                          row["hours_category_share"]) == pytest.approx(100.0)


def test_a_brand_with_no_listing_of_its_own_has_presence_zero(scan):
    _, brands = scan(FakePost())
    row = brands["target"]
    assert (row["status"], row["reason"]) == ("ok", None)
    assert row["directories"] == {"google": {"listed": False, "url": None, "mismatch_fields": []}}
    assert (row["presence_share"], row["nap_share"], row["hours_category_share"]) == (0.0, 0.0, 0.0)
    assert row["listing"] is None
    assert row["nap_check"] == {"compared": False, "reason": "no_crawled_profile"}
    assert row["search"]["title_matches"] == 2
    assert row["search"]["near_misses"] == [
        {"title": "Target Optical", "website": "https://www.targetoptical.example/"},
        {"title": "Target", "website": None}]


def test_a_listing_without_hours_or_without_a_category_scores_no_hours_and_category(scan):
    store_page = "https://www.northmart.example/store/1"
    for fields in ({"category": "Department store"}, {"work_hours": OPEN},
                   {"category": "Department store", "work_hours": {"timetable": {"monday": None}}}):
        _, brands = scan(FakePost(Target=[place("Target", "https://www.target.example/", **fields)],
                                  Northmart=[place("Northmart", store_page, category="Store", work_hours=OPEN)]))
        row = brands["target"]
        assert (row["presence_share"], row["nap_share"], row["hours_category_share"]) == (1.0, 1.0, 0.0)
        assert row["directories"]["google"] == {"listed": True, "url": None, "mismatch_fields": []}
        assert reference.local_listings_score(1.0, 1.0, 0.0) == pytest.approx(80.0)
        (scan.out / "local.json").unlink()


def test_the_file_carries_the_panel_and_each_call_its_cost_and_a_rerun_makes_no_calls(scan):
    scan(FakePost())
    data = store.read_json(scan.out / "local.json")
    panel = panel_file.load(scan.out.parent / "panel.yaml")
    assert (data["panel_id"], data["panel_sha256"]) == (panel.panel_id, panel.sha256)
    costs = store.read_jsonl(scan.out / "cost_log.jsonl")
    assert [(c["purpose"], c["vendor"], c["ref"], c["model"], c["cost_usd"], c["basis"]) for c in costs] == [
        ("local", "dataforseo", "local/northmart", local.MAPS, 0.002, "vendor"),
        ("local", "dataforseo", "local/target", local.MAPS, 0.002, "vendor"),
    ]
    again = FakePost()
    code, brands = scan(again)
    assert code == 0 and again.calls == []
    assert set(brands) == {"northmart", "target", "webonly"}


def test_a_failed_search_fails_the_brand_and_the_rerun_asks_only_for_it(scan):
    def flaky(endpoint, body):
        if body[0]["keyword"] == "Target":
            return {"status_code": 20000, "tasks": [{"status_code": 40501, "status_message": "bad", "cost": 0}]}
        return reply(NORTHMART_PLACES)

    code, brands = scan(flaky)
    assert code == 1
    assert brands["northmart"]["status"] == "ok"
    assert (brands["target"]["status"], brands["target"]["reason"]) == ("failed", "maps_search_failed")
    assert "40501" in brands["target"]["detail"]
    assert brands["target"]["directories"] == {} and brands["target"]["presence_share"] is None
    post = FakePost()
    code, brands = scan(post)
    assert code == 0
    assert [body[0]["keyword"] for _, body in post.calls] == ["Target"]
    assert brands["target"]["status"] == "ok"


@pytest.mark.parametrize("task", [
    {"status_code": 20000, "cost": 0.002, "result": [{"items": None}]},
    {"status_code": 20000, "cost": 0.002, "result": [{"items": [{"type": "maps_paid_item", "title": "Target"}]}]},
    {"status_code": 40102, "status_message": "No Search Results.", "cost": 0.002},
])
def test_a_search_that_returns_no_place_is_not_scored_as_presence_zero(scan, task):
    def post(endpoint, body):
        if body[0]["keyword"] == "Target":
            return {"status_code": 20000, "tasks": [task]}
        return reply(NORTHMART_PLACES)

    code, brands = scan(post, attempts=3)
    assert code == 1
    assert (brands["target"]["status"], brands["target"]["reason"]) == ("failed", "maps_search_empty")
    assert brands["target"]["presence_share"] is None
    assert [c["ref"] for c in store.read_jsonl(scan.out / "cost_log.jsonl")] == ["local/northmart", "local/target"]


def test_a_call_is_tried_again_up_to_the_attempts_given_and_each_billed_try_is_logged(scan):
    def no_result(endpoint, body):
        return {"status_code": 20000, "tasks": [{"status_code": 20000, "cost": 0.002, "result": None}]}

    code, brands = scan(no_result, attempts=2)
    assert code == 1
    assert (brands["northmart"]["status"], brands["northmart"]["reason"]) == ("failed", "maps_search_failed")
    assert "2 attempts failed" in brands["northmart"]["detail"]
    assert len(store.read_jsonl(scan.out / "cost_log.jsonl")) == 4


def test_a_reply_of_an_unknown_shape_fails_that_brand_only(scan):
    def post(endpoint, body):
        if body[0]["keyword"] == "Northmart":
            return reply("not a list")
        if body[0]["keyword"] == "Target":
            raise KeyError("tasks")
        raise AssertionError(body)

    code, brands = scan(post)
    assert code == 1
    assert (brands["northmart"]["status"], brands["northmart"]["reason"]) == ("failed", "maps_search_failed")
    assert (brands["target"]["status"], brands["target"]["reason"]) == ("failed", "reader_error")
    assert "KeyError" in brands["target"]["detail"]
    assert brands["webonly"]["status"] == "not_applicable"


def test_without_go_the_calls_and_their_cost_are_printed_and_nothing_is_asked(scan, capsys):
    post = FakePost()
    code, brands = scan(post, go=False, attempts=3)
    assert code == 0 and post.calls == [] and brands is None
    assert not (scan.out / "cost_log.jsonl").exists()
    printed = capsys.readouterr().out
    assert "Google Maps search, location_code 2840, language en" in printed
    assert 'northmart: 1 call: Maps search for "Northmart"' in printed
    assert 'target: 1 call: Maps search for "Target"' in printed
    assert "webonly: no store or service area, so not applicable. No call." in printed
    assert "2 DataForSEO calls." in printed
    assert "($0.002 a call) that is $0.004, and at most $0.012" in printed
    assert "dry run: nothing was asked" in printed


def test_the_search_runs_in_the_panels_country_and_language_or_the_code_given(scan):
    scan.write_panel(market={"country": "GB", "language": "en"})
    post = FakePost()
    scan(post)
    assert post.calls[0][1] == [{"keyword": "Northmart", "location_code": 2826, "language_code": "en"}]
    (scan.out / "local.json").unlink()
    post = FakePost()
    _, brands = scan(post, location_code=1023191)
    assert post.calls[0][1][0]["location_code"] == 1023191
    assert brands["northmart"]["search"]["location_code"] == 1023191


def test_a_country_with_no_location_code_is_refused_before_any_call(scan):
    scan.write_panel(market={"country": "BR", "language": "pt"})
    post = FakePost()
    with pytest.raises(PanelError, match="no DataForSEO location code for country BR"):
        scan(post)
    assert post.calls == []


def test_a_brand_whose_local_flag_changed_is_done_again(scan):
    scan(FakePost())
    data = store.read_json(scan.out / "local.json")
    data["brands"]["webonly"], data["brands"]["target"] = data["brands"]["target"], data["brands"]["webonly"]
    store.write_json(scan.out / "local.json", data)
    post = FakePost()
    _, brands = scan(post)
    assert [body[0]["keyword"] for _, body in post.calls] == ["Target"]
    assert (brands["target"]["status"], brands["webonly"]["status"]) == ("ok", "not_applicable")


def test_a_file_written_under_another_panel_is_refused(scan):
    scan.write_panel()
    store.write_json(scan.out / "local.json", {"panel_id": "other", "panel_sha256": "0" * 64, "brands": {}})
    post = FakePost()
    with pytest.raises(StoreError, match="another panel"):
        scan(post)
    assert post.calls == []
