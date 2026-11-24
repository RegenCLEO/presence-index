"""Look up each brand's Trustpilot rating and review count and write reviews.json.

These are the inputs of the Reviews & Ratings signal, gathered from outside. Each of a
brand's domains costs one DataForSEO Trustpilot task. The task is posted and its result is
polled until it ends. Without --go the stage prints the calls it would make and stops. With
--go it skips every domain already stored with a profile or with none, so a rerun finishes
what an earlier run left.

A brand with several domains can have several Trustpilot profiles. They are one platform
and the same shoppers may be on each, so they are never added together. The brand's
profile is the one with the most reviews, and the row lists every profile under
"profiles_checked".

A brand's status is "ok" (every domain answered and at least one has a profile, even one
with no reviews), "no_profile" (every domain answered and none has a profile) or "failed"
(a lookup did not complete). A failed domain whose task was posted keeps the task id, and
the next run collects that task before it pays for a new one.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from scan import panel as panel_file, store
from scan.keywords import API_ROOT, Post, dataforseo_post, task_of
from scan.panel import Brand, PanelError
from scan.records import EngineError, utc_now

REVIEWS, ADDITIONS = "reviews.json", "additions.yaml"
PLATFORM = "trustpilot"
TASK_POST = "business_data/trustpilot/reviews/task_post"
TASK_GET = "business_data/trustpilot/reviews/task_get/"
DEPTH = 1                  # reviews fetched per task; the rating and the count come with any depth
POLL_ATTEMPTS = 60
POLL_INTERVAL_S = 5.0

OK = 20000
NO_RESULTS = 40102                     # DataForSEO: nothing was found for the request
IN_PROGRESS = (20100, 40601, 40602)    # task created, handed to a worker, in the queue
DONE = ("ok", "no_profile")            # a domain with one of these is not looked up again

# get(endpoint) -> the decoded JSON reply. Raises EngineError when the call fails.
Get = Callable[[str], Dict[str, Any]]
Sleep = Callable[[float], None]


def add_arguments(p) -> None:
    p.add_argument("--go", action="store_true", help="make the calls; without it the stage only lists them")
    p.add_argument("--attempts", type=int, default=3, help="tries to post a brand's task before it is left for a rerun")
    p.add_argument("--poll-attempts", type=int, default=POLL_ATTEMPTS,
                   help="rounds of result checks before an unfinished task is left for a rerun")
    p.add_argument("--poll-interval", type=float, default=POLL_INTERVAL_S, help="seconds between rounds")


# ---------- pure rules ----------
def request_body(domain: str) -> List[Dict[str, Any]]:
    return [{"domain": domain, "depth": DEPTH}]


def created_id(task: Dict[str, Any]) -> str:
    """The id of a task the vendor accepted."""
    code = task.get("status_code")
    if isinstance(code, int) and code >= 40000:
        raise EngineError(f"DataForSEO task error {code}: {task.get('status_message')}")
    if not task.get("id"):
        raise EngineError("DataForSEO returned no task id")
    return str(task["id"])


def read_task(reply: Dict[str, Any]) -> Tuple[str, Any]:
    """What one result check says about a task.

    ("ok", result) when the task ended with a result, ("no_profile", None) when the
    vendor found nothing for the domain, ("pending", None) while the task is not finished
    and ("failed", message) when it ended with any other error."""
    tasks = reply.get("tasks") or []
    task = tasks[0] if tasks and isinstance(tasks[0], dict) else {}
    code = task.get("status_code")
    results = task.get("result") or []
    result = results[0] if results else None
    if code == OK and isinstance(result, dict) and result:
        return "ok", result
    if code == NO_RESULTS:
        return "no_profile", None
    if code is None or code == OK or code in IN_PROGRESS:
        # A reply with no task, or a finished task with no result yet, is checked again.
        return "pending", None
    return "failed", f"DataForSEO task error {code}: {task.get('status_message')}"


def _number(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def header(result: Dict[str, Any]) -> Tuple[Optional[float], int]:
    """(rating, review count) of a profile.

    The rating is rating.value, rounded to 2 decimals. The count is rating.votes_count when
    the vendor sets it, otherwise reviews_count, otherwise the number of reviews in the reply."""
    rating_obj = result.get("rating") if isinstance(result.get("rating"), dict) else {}
    total = rating_obj.get("votes_count")
    if total is None:
        total = result.get("reviews_count") or result.get("items_count") or 0
    try:
        count = int(total)
    except (TypeError, ValueError):
        count = 0
    rating = _number(rating_obj.get("value"))
    return (round(rating, 2) if rating is not None else None), count


def pooled(platforms: Iterable[Dict[str, Any]]) -> Tuple[int, Optional[float]]:
    """(n, mean rating) over platform rows. n is the sum of the counts and the mean is
    weighted by count and rounded to 2 decimals. A row with no rating or no reviews adds
    nothing to either."""
    counted = [(float(p["rating"]), p["count"]) for p in platforms if p["rating"] is not None and p["count"] > 0]
    n = sum(count for _, count in counted)
    if n == 0:
        return 0, None
    return n, round(sum(rating * count for rating, count in counted) / n, 2)


def domains_of(brand: Brand) -> List[str]:
    """The brand's domains, the main one first, each once."""
    return [d for d in dict.fromkeys(brand.domains) if d]


def lookup(domain: str, status: str, rating: Optional[float] = None, count: int = 0,
           fetched_at: Optional[str] = None, **failure: Any) -> Dict[str, Any]:
    """One domain's entry in a row's profiles_checked. ``failure`` holds the reason, the
    detail and the task id of a lookup that did not complete."""
    return {"profile": domain, "status": status, "rating": rating, "count": count,
            "fetched_at": fetched_at or utc_now(), **failure}


def profile_lookup(domain: str, result: Dict[str, Any]) -> Dict[str, Any]:
    rating, count = header(result)
    return lookup(domain, "ok", rating, count)


def stored_lookups(row: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """The lookups a stored row holds, by domain.

    A row written before brands were looked up on every domain has one ``domain`` and no
    profiles_checked. It is read as one lookup of that domain."""
    if not row:
        return {}
    if "profiles_checked" in row:
        return {e["profile"]: e for e in row["profiles_checked"]}
    domain, status, at = row.get("domain"), row.get("status"), row.get("fetched_at")
    if not domain or status not in (*DONE, "failed"):
        return {}
    if status == "failed":
        failure = {k: row[k] for k in ("reason", "detail", "task_id") if row.get(k) is not None}
        return {domain: lookup(domain, "failed", fetched_at=at, **failure)}
    platform = (row.get("platforms") or [{}])[0]
    rating = _number(platform.get("rating"))
    return {domain: lookup(domain, status, round(rating, 2) if rating is not None else None,
                           int(platform.get("count") or 0), fetched_at=at)}


def brand_row(brand: Brand, lookups: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """One brand's row in reviews.json, from the lookups of its domains.

    The row is "failed" while a domain's lookup is missing or did not complete. Otherwise
    the brand's profile is the one with the most reviews, the main domain's on a tie. A
    profile with no rating counts as one with no reviews, as it does in the pooling."""
    checked = [lookups[d] for d in domains_of(brand) if d in lookups]
    failed = [e for e in checked if e["status"] == "failed"]
    profiles = [e for e in checked if e["status"] == "ok"]
    platforms: List[Dict[str, Any]] = []
    times = [e["fetched_at"] for e in checked]
    if failed or len(checked) < len(domains_of(brand)):
        status, reason = "failed", (failed[0].get("reason") if failed else None) or "not_looked_up"
    elif profiles:
        status, reason = "ok", None
        best = max(profiles, key=lambda e: e["count"] if e["rating"] is not None else 0)
        platforms = [{"platform": PLATFORM, "profile": best["profile"], "rating": best["rating"], "count": best["count"]}]
        times = [best["fetched_at"]]
    else:
        status, reason = "no_profile", "no_trustpilot_profile"
    n, mean_rating = pooled(platforms)
    return {"status": status, "reason": reason, "profiles_checked": checked, "platforms": platforms,
            "n": n, "mean_rating": mean_rating, "fetched_at": max(times, default=None) or utc_now()}


def action(found: Optional[Dict[str, Any]]) -> Tuple[str, Optional[str]]:
    """What a run does for one domain, with the id of the task to collect when there is one.

    "skip" for a domain already looked up, "collect" for a task an earlier run posted and
    did not finish, and "post" for a new lookup."""
    if not found:
        return "post", None
    if found["status"] in DONE:
        return "skip", None
    if found.get("task_id"):
        return "collect", str(found["task_id"])
    return "post", None


# ---------- the network ----------
def dataforseo_get(timeout: float = 60.0) -> Get:
    import httpx

    login, password = os.environ.get("DATAFORSEO_LOGIN"), os.environ.get("DATAFORSEO_PASSWORD")
    if not login or not password:
        raise PanelError("set DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD to call DataForSEO")
    auth = base64.b64encode(f"{login}:{password}".encode()).decode()
    client = httpx.Client(timeout=timeout, headers={"Authorization": f"Basic {auth}"})

    def get(endpoint: str) -> Dict[str, Any]:
        try:
            resp = client.get(API_ROOT + endpoint)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EngineError(f"{type(exc).__name__}: {exc}") from exc
    return get


# ---------- the stage ----------
def main(args, post: Optional[Post] = None, get: Optional[Get] = None, sleep: Sleep = time.sleep) -> int:
    if not args.go:
        return _main(args, post, get, sleep)
    with store.exclusive(args.out):
        return _main(args, post, get, sleep)


def _post_task(post: Post, brand: Brand, domain: str, attempts: int, sleep: Sleep, out: Path, panel_id: str) -> str:
    """Post the task for one domain and return its id. Every reply that reports a cost is logged."""
    last: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            task = task_of(post(TASK_POST, request_body(domain)))
            if task.get("cost") is not None:
                # Logged before the id is read. A task that bills and is then refused is
                # still a paid call.
                store.log_cost(out, panel_id=panel_id, day=utc_now()[:10], purpose="reviews",
                               vendor="dataforseo", ref=brand.id, model=TASK_POST, cost_usd=float(task["cost"]))
            return created_id(task)
        except EngineError as exc:
            last = exc
            if attempt < attempts:
                sleep(2 * attempt)
    raise EngineError(f"{attempts} attempts failed, last: {last}")


def _main(args, post: Optional[Post], get: Optional[Get], sleep: Sleep) -> int:
    out: Path = args.out
    panel = panel_file.with_additions(panel_file.load(args.panel), out / ADDITIONS)
    path = out / REVIEWS
    reviews = store.read_json(path) if path.exists() else {}
    if reviews.get("panel_sha256", panel.sha256) != panel.sha256:
        raise store.StoreError(f"{path} was written under another panel ({reviews['panel_sha256']}). "
                               "Use a new output directory for a new or changed panel.")
    reviews.update(panel_id=panel.panel_id, panel_sha256=panel.sha256)
    brands: Dict[str, Dict[str, Any]] = reviews.setdefault("brands", {})

    # Only the lookups of the domains a brand has now are kept. A stored row counts as
    # done when it covers every one of them.
    lookups: Dict[str, Dict[str, Dict[str, Any]]] = {}
    plan: List[Tuple[Brand, str, str, Optional[str]]] = []   # brand, domain, action, task id to collect
    for brand in panel.brands:
        held = stored_lookups(brands.get(brand.id))
        lookups[brand.id] = {d: held[d] for d in domains_of(brand) if d in held}
        plan += [(brand, d, *action(lookups[brand.id].get(d))) for d in domains_of(brand)]
    todo = [(brand, domain, task_id) for brand, domain, act, task_id in plan if act != "skip"]

    print(f"panel {panel.panel_id}: {len(panel.brands)} brands with {len(plan)} domains, "
          f"{len(plan) - len(todo)} already stored, {len(todo)} Trustpilot lookups to make")
    for brand, domain, act, task_id in plan:
        if act == "skip":
            print(f"{brand.id} ({domain}): already stored ({lookups[brand.id][domain]['status']}), skipped")
        elif act == "collect":
            print(f"{brand.id} ({domain}): GET {TASK_GET}{task_id} until the task ends (posted by an earlier run)")
        else:
            print(f"{brand.id} ({domain}): POST {TASK_POST} {json.dumps(request_body(domain))}, "
                  f"then GET {TASK_GET}<task id> until the task ends")
    if not args.go:
        print("dry run: nothing was asked. Pass --go to make the calls.")
        return 0

    # A stored row is rewritten when the brand's domains or the row shape changed since it
    # was written. This makes no call.
    rebuilt = {b.id: brand_row(b, lookups[b.id]) for b in panel.brands if b.id in brands}
    if any(brands[brand_id] != row for brand_id, row in rebuilt.items()):
        brands.update(rebuilt)
        store.write_json(path, reviews)
    if not todo:
        return 0
    post = post or dataforseo_post()
    get = get or dataforseo_get()

    def save(brand: Brand, found: Dict[str, Any], say: bool = True) -> None:
        lookups[brand.id][found["profile"]] = found
        brands[brand.id] = brand_row(brand, lookups[brand.id])
        store.write_json(path, reviews)
        if found["status"] == "ok":
            print(f"{brand.id} ({found['profile']}): ok ({found['count']} reviews, rating {found['rating']})")
        elif say:
            print(f"{brand.id} ({found['profile']}): {found['status']}"
                  + (f" ({found['reason']})" if found["status"] == "failed" else ""))

    # Post every task first and then check them together, so the whole stage waits about
    # as long as its slowest task.
    pending: Dict[Tuple[str, str], Tuple[Brand, str, bool]] = {}   # (brand id, domain) -> (brand, task id, resumed)
    for brand, domain, task_id in todo:
        if task_id:
            pending[brand.id, domain] = (brand, task_id, True)
            continue
        try:
            task_id = _post_task(post, brand, domain, args.attempts, sleep, out, panel.panel_id)
        except EngineError as exc:
            save(brand, lookup(domain, "failed", reason="post_failed", detail=str(exc)))
            continue
        # Stored before the first check, so a run that stops here leaves a task the next run collects.
        save(brand, lookup(domain, "failed", reason="task_pending", task_id=task_id,
                           detail="the task was posted and its result has not been collected"), say=False)
        pending[brand.id, domain] = (brand, task_id, False)

    last_error: Dict[Tuple[str, str], str] = {}
    for round_no in range(1, args.poll_attempts + 1):
        for key, (brand, task_id, _) in list(pending.items()):
            domain = key[1]
            try:
                outcome, value = read_task(get(TASK_GET + task_id))
            except EngineError as exc:
                # One check that fails does not end the lookup. The next round asks again.
                last_error[key] = str(exc)
                continue
            if outcome == "pending":
                continue
            del pending[key]
            if outcome == "ok":
                save(brand, profile_lookup(domain, value))
            elif outcome == "no_profile":
                save(brand, lookup(domain, "no_profile"))
            else:
                save(brand, lookup(domain, "failed", reason="task_error", detail=value))
        if not pending:
            break
        if round_no < args.poll_attempts:
            sleep(args.poll_interval)

    for key, (brand, task_id, resumed) in pending.items():
        detail = f"no result after {args.poll_attempts} checks"
        if key in last_error:
            detail += f", last error: {last_error[key]}"
        if resumed:
            # A task that outlasts two runs is dropped, and the next run posts a new one.
            save(brand, lookup(key[1], "failed", reason="poll_timeout", detail=detail))
        else:
            save(brand, lookup(key[1], "failed", reason="task_pending", task_id=task_id, detail=detail))

    worked = list(dict.fromkeys(brand.id for brand, _, _ in todo))
    counts = {s: sum(1 for brand_id in worked if brands[brand_id]["status"] == s) for s in ("ok", "no_profile", "failed")}
    print(f"made {len(todo)} lookups for {len(worked)} brands: {counts['ok']} ok, {counts['no_profile']} with no "
          f"profile, {counts['failed']} failed. Cost so far: {store.cost_summary(out)}")
    if counts["failed"]:
        print("rerun the same command to retry the failed lookups.", file=sys.stderr)
    return 1 if counts["failed"] else 0
