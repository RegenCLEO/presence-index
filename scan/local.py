"""Find each brand's Google Maps listing and write local.json.

One DataForSEO Maps search per brand, for the brand's name in the panel's country. The
first place whose title is the brand's and whose website is on the brand's own domains
is the brand's listing. Google is the only directory a public scan checks. A brand with
``local: false`` is not applicable and no call is made for it.

Without --go the stage prints the calls it would make with an upper bound on their cost,
and stops. With --go it skips every brand already stored as "ok" or "not_applicable", so
a rerun finishes what an earlier run left.

The listing's name, address and phone are not compared with the brand's site, because
the site read holds no address or phone. A brand with a listing therefore has nap_share
1.0, and its row says the comparison was not made.
"""
from __future__ import annotations

import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from scan import keywords, panel as panel_file, store
from scan.keywords import Post
from scan.panel import Brand
from scan.records import EngineError, utc_now

LOCAL = "local.json"
MAPS = "serp/google/maps/live/advanced"
DIRECTORY_SET = "google_only"

# USD per Maps search, as CLEO's product records it. It prices the estimate a dry run
# prints. The cost log takes the cost DataForSEO returns with each task.
STATED_PRICE_USD = 0.002

# Title matches left out for their website, kept in the row so a person can see why a
# brand has no listing.
NEAR_MISSES_KEPT = 3
_NOISE = re.compile(r"[^a-z0-9]+")

# ask(body) -> the items of one Maps search. Raises EngineError when every attempt fails.
Ask = Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]


def add_arguments(p) -> None:
    p.add_argument("--go", action="store_true",
                   help="make the calls; without it the stage only lists them with their cost")
    p.add_argument("--attempts", type=int, default=3, help="tries per call before the brand is left for a rerun")
    p.add_argument("--location-code", type=int, help="DataForSEO location_code, overriding the panel country")


# ---------- pure rules ----------
def _plain(text: Any) -> str:
    """Letters and digits only, in lower case."""
    return _NOISE.sub("", str(text or "").lower())


def titles_match(wanted: str, candidate: Any) -> bool:
    """True when a place's title is the name asked for. Names are compared on letters and
    digits. One may hold the other ("Joe's Pizza" and "Joe's Pizza Main St") when the
    shorter has six characters or more."""
    a, b = _plain(wanted), _plain(candidate)
    if not a or not b:
        return False
    if a == b:
        return True
    return min(len(a), len(b)) >= 6 and (a in b or b in a)


def carries_name(item: Mapping[str, Any], brand: Brand) -> bool:
    """True when the place's title is one of the brand's names, or starts with one. A short
    name may only start a title ("Lowe's" in "Lowe's Home Improvement"): the caller also
    requires the place's website to be the brand's own, which keeps other businesses out."""
    title = _plain(item.get("title"))
    return any(titles_match(name, item.get("title")) or (len(_plain(name)) >= 3 and title.startswith(_plain(name)))
               for name in brand.names)


def website_of(item: Mapping[str, Any]) -> Optional[str]:
    """The website a place gives: its link, or the bare domain when it has no link."""
    for key in ("url", "domain"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def on_own_site(item: Mapping[str, Any], brand: Brand) -> bool:
    return any(isinstance(item.get(key), str) and brand.owns(item[key]) for key in ("url", "domain"))


def own_listing(places: Iterable[Mapping[str, Any]], brand: Brand) -> Optional[Mapping[str, Any]]:
    """The first place that carries one of the brand's names and links to the brand's own
    site. A title alone is not enough: another business may share the name."""
    return next((p for p in places if carries_name(p, brand) and on_own_site(p, brand)), None)


def has_hours(work_hours: Any) -> Optional[bool]:
    """True when the listing opens on at least one day. A day with no opening period is
    a closed day. None when the reply holds no readable hours, which says nothing either way."""
    if not isinstance(work_hours, dict):
        return None
    timetable = work_hours.get("timetable")
    return isinstance(timetable, dict) and any(periods for periods in timetable.values())


def _text(value: Any) -> Optional[str]:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return (value.strip() or None) if isinstance(value, str) else None


def _number(value: Any) -> Optional[float]:
    """A number the vendor sent as a number or as text. A boolean or a value that is not
    finite is not a number."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def listing_facts(item: Mapping[str, Any]) -> Dict[str, Any]:
    """What the row keeps of the brand's listing."""
    rating = item.get("rating") if isinstance(item.get("rating"), dict) else {}
    votes = _number(rating.get("votes_count"))
    return {"title": _text(item.get("title")), "address": _text(item.get("address")),
            "phone": _text(item.get("phone")), "category": _text(item.get("category")),
            "has_hours": has_hours(item.get("work_hours")),
            "place_id": _text(item.get("place_id")), "cid": _text(item.get("cid")),
            "rating": _number(rating.get("value")), "ratings_count": None if votes is None else int(votes),
            "website": website_of(item)}


def listing_url(listing: Mapping[str, Any]) -> Optional[str]:
    if listing["cid"]:
        return f"https://maps.google.com/?cid={listing['cid']}"
    if listing["place_id"]:
        return f"https://www.google.com/maps/place/?q=place_id:{listing['place_id']}"
    return None


def request_body(name: str, location: int, language: str) -> List[Dict[str, Any]]:
    # A country's location_code makes the search country-wide. The request names no point
    # and no city, so Google chooses which of a chain's places to return.
    return [{"keyword": name, "location_code": location, "language_code": language}]


# ---------- one brand ----------
def not_applicable() -> Dict[str, Any]:
    return {"status": "not_applicable", "reason": "no_location", "directory_set": None, "directories": {},
            "presence_share": None, "nap_share": None, "hours_category_share": None, "fetched_at": utc_now()}


def _failed(reason: str, detail: str) -> Dict[str, Any]:
    return {"status": "failed", "reason": reason, "detail": detail, "directory_set": DIRECTORY_SET,
            "directories": {}, "presence_share": None, "nap_share": None, "hours_category_share": None,
            "fetched_at": utc_now()}


def measure(brand: Brand, body: List[Dict[str, Any]], ask: Ask) -> Dict[str, Any]:
    """The local.json entry for one brand that has a store or a service area."""
    try:
        items = ask(body)
    except EngineError as exc:
        return _failed("maps_search_failed", str(exc))
    places = [i for i in items if i.get("type") == "maps_search"]
    if not places:
        # A search that returns no place at all cannot tell a brand without a listing
        # from a search Maps did not serve, so it is not scored as presence 0.
        return _failed("maps_search_empty", "the Maps search returned no place")
    named = [p for p in places if carries_name(p, brand)]
    found = own_listing(places, brand)
    listing = listing_facts(found) if found is not None else None

    # A listing nothing was compared with has no mismatch, and a listed directory with no
    # mismatch counts as consistent. nap_check says the comparison was not made.
    presence = nap = 1.0 if listing else 0.0
    hours_category = 1.0 if listing and listing["has_hours"] and listing["category"] else 0.0
    near_misses = [{"title": _text(p.get("title")), "website": website_of(p)}
                   for p in named if not on_own_site(p, brand)][:NEAR_MISSES_KEPT]
    return {"status": "ok", "reason": None, "directory_set": DIRECTORY_SET,
            "directories": {"google": {"listed": listing is not None,
                                       "url": listing_url(listing) if listing else None,
                                       "mismatch_fields": []}},
            "presence_share": presence, "nap_share": nap, "hours_category_share": hours_category,
            "nap_check": {"compared": False, "reason": "no_crawled_profile"},
            "listing": listing,
            "search": {**body[0], "places": len(places), "title_matches": len(named), "near_misses": near_misses},
            "fetched_at": utc_now()}


# ---------- the network ----------
def asker(post: Post, out: Path, panel_id: str, brand_id: str, attempts: int,
          sleep: Callable[[float], None]) -> Ask:
    day = datetime.now(timezone.utc).date().isoformat()

    def ask(body: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        last: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                task = keywords.task_of(post(MAPS, body))
                if task.get("cost") is not None:
                    # Logged before the result is read: a task that bills and then fails
                    # to parse is still a paid call.
                    store.log_cost(out, panel_id=panel_id, day=day, purpose="local", vendor="dataforseo",
                                   ref=f"local/{brand_id}", model=MAPS, cost_usd=float(task["cost"]))
                result = keywords.task_result(task)
                items = result.get("items") if isinstance(result, dict) else result
                if items is not None and not isinstance(items, list):
                    raise EngineError("DataForSEO task result holds no list of items")
                return [i for i in items or [] if isinstance(i, dict)]
            except EngineError as exc:
                last = exc
                if attempt < attempts:
                    sleep(2 * attempt)
        raise EngineError(f"{attempts} attempts failed, last: {last}")
    return ask


# ---------- the stage ----------
def main(args, post: Optional[Post] = None, sleep: Callable[[float], None] = time.sleep) -> int:
    if not args.go:
        return _main(args, post, sleep)
    with store.exclusive(args.out):
        return _main(args, post, sleep)


def _main(args, post: Optional[Post], sleep: Callable[[float], None]) -> int:
    panel = panel_file.with_additions(panel_file.load(args.panel), args.out / "additions.yaml")
    out: Path = args.out
    attempts = max(1, int(getattr(args, "attempts", None) or 3))
    location = keywords.location_code(panel, getattr(args, "location_code", None))
    language = panel.market.language

    path = out / LOCAL
    data = {"brands": {}, **(store.read_json(path) if path.exists() else {})}
    if data.get("panel_sha256", panel.sha256) != panel.sha256:
        raise store.StoreError(f"{path} was written under another panel ({data['panel_sha256']}). "
                               "Use a new output directory for a new or changed panel.")
    data.update(panel_id=panel.panel_id, panel_sha256=panel.sha256)

    # (brand, action). A brand is skipped only when its stored status still agrees with
    # its local flag.
    plan = []
    for brand in panel.brands:
        prior = (data["brands"].get(brand.id) or {}).get("status")
        if prior == ("ok" if brand.local else "not_applicable"):
            plan.append((brand, "skip"))
        else:
            plan.append((brand, "ask" if brand.local else "not_applicable"))

    print(f"panel {panel.panel_id}: Google Maps search, location_code {location}, language {language}")
    for brand, action in plan:
        print(f"{brand.id}: " + {"skip": "already stored, skipped",
                                 "not_applicable": "no store or service area, so not applicable. No call.",
                                 "ask": f'1 call: Maps search for "{brand.name}"'}[action])
    calls = sum(1 for _, action in plan if action == "ask")
    once = calls * STATED_PRICE_USD
    print(f"{calls} DataForSEO calls. At the price CLEO's product records (${STATED_PRICE_USD} a call) that is "
          f"${once:.3f}, and at most ${once * attempts:.3f} if every call takes all {attempts} attempts "
          "and each attempt is billed.")
    if not args.go:
        print("dry run: nothing was asked. Pass --go to make the calls.")
        return 0
    if calls:
        post = post or keywords.dataforseo_post()

    failed = 0
    for brand, action in plan:
        if action == "skip":
            continue
        if action == "not_applicable":
            entry = not_applicable()
        else:
            try:
                entry = measure(brand, request_body(brand.name, location, language),
                                asker(post, out, panel.panel_id, brand.id, attempts, sleep))
            except Exception as exc:  # one reply of an unknown shape must not stop the brands after it
                entry = _failed("reader_error", f"{type(exc).__name__}: {exc}")
        failed += entry["status"] == "failed"
        data["brands"][brand.id] = entry
        store.write_json(path, data)
        print(f"{brand.id}: {entry['status']}" + (f" ({entry['reason']})" if entry["reason"] else ""),
              file=sys.stderr if entry["status"] == "failed" else sys.stdout)
    print(f"stored {calls - failed} brands, {failed} failed. Cost so far: {store.cost_summary(out)}")
    if failed:
        print("rerun the same command to retry the failed brands.", file=sys.stderr)
    return 1 if failed else 0
