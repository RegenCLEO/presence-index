"""Pull Google's organic top ten for every panel keyword and append it to serp.jsonl.

One DataForSEO organic SERP call per keyword per pull. Without --go the stage says how
many checks remain and stops. With --go it skips every keyword already stored for the
pull, so a rerun finishes what an earlier run left.
"""
from __future__ import annotations

import base64
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from scan import panel as panel_file, store
from scan.panel import Panel, PanelError
from scan.records import EngineError, SerpResult, SerpRow, serp_id, utc_now

SERP, ERRORS = "serp.jsonl", "errors.jsonl"
ENDPOINT = "serp/google/organic/live/advanced"
API_ROOT = "https://api.dataforseo.com/v3/"
DEPTH = 10

# DataForSEO location codes by country.
LOCATION_CODES = {
    "US": 2840, "GB": 2826, "CA": 2124, "AU": 2036, "DE": 2276,
    "FR": 2250, "IN": 2356, "SG": 2702, "AE": 2784, "JP": 2392,
}

# post(endpoint, body) -> the decoded JSON reply. Raises EngineError when the call fails.
Post = Callable[[str, List[Dict[str, Any]]], Dict[str, Any]]


def add_arguments(p) -> None:
    p.add_argument("--pull", help="pull date, one of the panel's keyword_pulls "
                                  "(default: the only pull not yet complete)")
    p.add_argument("--go", action="store_true", help="make the calls; without it the stage only counts them")
    p.add_argument("--any-week", action="store_true",
                   help="file a pull under a date outside the current calendar week (rehearsal only)")
    p.add_argument("--attempts", type=int, default=3, help="tries per keyword before it is left for a rerun")
    p.add_argument("--location-code", type=int, help="DataForSEO location_code, overriding the panel country")


# ---------- pure rules ----------
def require_distinct_weeks(pulls) -> None:
    """The organic signal takes one observation per calendar week."""
    weeks: Dict[tuple, str] = {}
    for pull in pulls:
        week = tuple(date.fromisoformat(pull).isocalendar())[:2]
        if week in weeks:
            raise PanelError(f"keyword pulls {weeks[week]} and {pull} fall in the same ISO week "
                             f"{week[0]}-W{week[1]:02d}; each pull must be in its own week")
        weeks[week] = pull


def location_code(panel: Panel, override: Optional[int] = None) -> int:
    if override:
        return int(override)
    if panel.siteread.get("location_code"):
        return int(panel.siteread["location_code"])
    country = panel.market.country
    if country not in LOCATION_CODES:
        raise PanelError(f"no DataForSEO location code for country {country}; "
                         "set siteread.location_code in the panel or pass --location-code")
    return LOCATION_CODES[country]


def choose_pull(panel: Panel, requested: Optional[str], done: set) -> str:
    if requested:
        if requested not in panel.keyword_pulls:
            raise PanelError(f"pull {requested} is not in the panel's keyword_pulls {list(panel.keyword_pulls)}")
        return requested
    open_pulls = [p for p in panel.keyword_pulls
                  if any(serp_id(p, k.text) not in done for k in panel.keywords)]
    if len(open_pulls) == 1:
        return open_pulls[0]
    state = "every pull is complete" if not open_pulls else f"pulls {open_pulls} are not complete"
    raise PanelError(f"{state}; pass --pull (one of {list(panel.keyword_pulls)})")


def request_body(keyword: str, language: str, location: int) -> List[Dict[str, Any]]:
    # No load_async_ai_overview: this pull wants organic results only.
    return [{"keyword": keyword, "location_code": location, "language_code": language,
             "device": "desktop", "depth": DEPTH}]


def _is_google_goto(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
        return parsed.hostname in ("google.com", "www.google.com") and parsed.path == "/goto"
    except ValueError:
        return False


def organic_result_url(item: Dict[str, Any]) -> str:
    """The site an organic result links to, or "" when it cannot be told.

    Google serves some results through google.com/goto redirects that DataForSEO passes
    through. The breadcrumb still names the site, and a forum thread's breadcrumb names none."""
    url = item.get("url")
    if not isinstance(url, str):
        return ""
    try:
        urlparse(url).hostname
    except ValueError:
        return ""
    if not _is_google_goto(url):
        return url
    breadcrumb = item.get("breadcrumb")
    head = breadcrumb.split("›", 1)[0].strip() if isinstance(breadcrumb, str) else ""
    if not head or any(c.isspace() for c in head) or "…" in head:
        return ""
    try:
        host = urlparse(head if "//" in head else f"//{head}").hostname or ""
    except ValueError:
        return ""
    return f"https://{host}/" if "." in host else ""


def organic_results(task_result: Dict[str, Any]) -> List[SerpResult]:
    """Organic items only, positions 1 to 10 by rank_group. rank_group counts within an
    item's own type, so an ad or SERP feature at rank_group 1 is not organic position 1."""
    out: List[SerpResult] = []
    for item in task_result.get("items") or []:
        pos = item.get("rank_group")
        if item.get("type") != "organic" or not isinstance(pos, int) or not 1 <= pos <= DEPTH:
            continue
        url = organic_result_url(item)
        if url:
            out.append({"position": pos, "url": url})
    return sorted(out, key=lambda r: r["position"])


def task_of(reply: Dict[str, Any]) -> Dict[str, Any]:
    if reply.get("status_code") != 20000:
        raise EngineError(f"DataForSEO error {reply.get('status_code')}: {reply.get('status_message')}")
    tasks = reply.get("tasks") or []
    if not tasks:
        raise EngineError("DataForSEO returned no task")
    return tasks[0]


NO_RESULTS = 40102  # DataForSEO: the search returned nothing


def task_result(task: Dict[str, Any]) -> Dict[str, Any]:
    if task.get("status_code") == NO_RESULTS:
        # Final: nobody ranks for it. Failing it would leave the pull incomplete for good.
        return {"items": []}
    if task.get("status_code") != 20000:
        raise EngineError(f"DataForSEO task error {task.get('status_code')}: {task.get('status_message')}")
    if not task.get("result"):
        # No result is no data, not a page without organic results.
        raise EngineError("DataForSEO task has no result")
    return task["result"][0]


# ---------- the network ----------
def dataforseo_post(timeout: float = 60.0) -> Post:
    import httpx

    login, password = os.environ.get("DATAFORSEO_LOGIN"), os.environ.get("DATAFORSEO_PASSWORD")
    if not login or not password:
        raise PanelError("set DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD to call DataForSEO")
    auth = base64.b64encode(f"{login}:{password}".encode()).decode()
    client = httpx.Client(timeout=timeout, headers={"Authorization": f"Basic {auth}"})

    def post(endpoint: str, body: List[Dict[str, Any]]) -> Dict[str, Any]:
        try:
            resp = client.post(API_ROOT + endpoint, json=body)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EngineError(f"{type(exc).__name__}: {exc}") from exc
    return post


# ---------- the stage ----------
def check_week(pull: str, any_week: bool) -> None:
    """Organic Search counts one observation per calendar week, so a pull must be made in
    the week it is filed under. Two pulls made on one day are one observation."""
    today = datetime.now(timezone.utc).date()
    if not any_week and date.fromisoformat(pull).isocalendar()[:2] != today.isocalendar()[:2]:
        raise PanelError(f"pull {pull} is not in this calendar week (today is {today} UTC). "
                         "Make each pull in its own week. For a rehearsal pass --any-week.")


def main(args, post: Optional[Post] = None) -> int:
    if not args.go:
        return _main(args, post)
    with store.exclusive(args.out):
        return _main(args, post)


def _main(args, post: Optional[Post] = None) -> int:
    panel = panel_file.load(args.panel)
    out: Path = args.out
    if not panel.keywords:
        print(f"panel {panel.panel_id} has no keywords; nothing to pull.")
        return 0
    require_distinct_weeks(panel.keyword_pulls)
    location = location_code(panel, getattr(args, "location_code", None))

    serp = out / SERP
    store.repair(serp)
    existing = store.read_jsonl(serp)
    store.require_panel(existing, panel.sha256, serp)
    done = {r["id"] for r in existing}
    pull = choose_pull(panel, args.pull, done)
    todo = [k for k in panel.keywords if serp_id(pull, k.text) not in done]

    print(f"panel {panel.panel_id} pull {pull}: {len(panel.keywords)} keywords, "
          f"{len(panel.keywords) - len(todo)} already stored, {len(todo)} checks to make "
          f"(location_code {location}, language {panel.market.language})")
    if not args.go:
        print("dry run: nothing was asked. Pass --go to make the calls.")
        return 0
    if not todo:
        return 0
    check_week(pull, getattr(args, "any_week", False))
    post = post or dataforseo_post()

    failed = 0
    for kw in todo:
        row_id = serp_id(pull, kw.text)
        body = request_body(kw.text, panel.market.language, location)
        last: Optional[Exception] = None
        result = None
        for attempt in range(1, args.attempts + 1):
            try:
                task = task_of(post(ENDPOINT, body))
                if task.get("cost") is not None:
                    # Logged before the result is read: a task that bills and then
                    # fails to parse is still a paid call.
                    store.log_cost(out, panel_id=panel.panel_id, day=pull, purpose="keyword",
                                   vendor="dataforseo", ref=row_id, model=ENDPOINT,
                                   cost_usd=float(task["cost"]))
                result = task_result(task)
                break
            except EngineError as exc:
                last = exc
                if attempt < args.attempts:
                    time.sleep(2 * attempt)
        if result is None:
            failed += 1
            store.append_jsonl(out / ERRORS, {"id": row_id, "at": utc_now(),
                                              "error": f"{args.attempts} attempts failed, last: {last}"})
            print(f"failed {row_id}: {last}", file=sys.stderr)
            continue
        row: SerpRow = {"id": row_id, "panel_id": panel.panel_id, "panel_sha256": panel.sha256,
                        "pull": pull, "keyword": kw.text, "volume": kw.volume,
                        "checked_at": utc_now(), "results": organic_results(result)}
        store.append_jsonl(serp, row)
    print(f"stored {len(todo) - failed} keyword checks, {failed} failed. Cost so far: {store.cost_summary(out)}")
    if failed:
        print("rerun the same command to retry the failed keywords.", file=sys.stderr)
    return 1 if failed else 0
