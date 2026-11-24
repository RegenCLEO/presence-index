"""Google AI Overviews: DataForSEO serp/google/organic/live/advanced.

Request: POST one task ``{keyword, location_code, language_code, device: "desktop",
load_async_ai_overview: true}`` with Basic auth. A search with no AI Overview, or one
whose overview has no text, is a final outcome and is not asked again. There is no
fallback to AI Mode.

DataForSEO returns many cited pages as ``google.com/goto?url=<token>`` links, which
name no site. ``ask`` follows each one on Google's own hosts and reads the page's
address from the ``Location`` header. It never requests the page itself. A link Google
does not redirect stays as it arrived, and ``settings["redirects"]`` counts both kinds.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from scan.engines import EngineConfigError
from scan.records import EngineError, EngineResult, Market

VENDOR = "dataforseo"
REQUIRED = ()
ENDPOINT = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"
# DataForSEO location codes by country. config["location_code"] overrides.
LOCATION_CODES = {
    "US": 2840, "GB": 2826, "CA": 2124, "AU": 2036, "DE": 2276, "FR": 2250,
    "IN": 2356, "SG": 2702, "AE": 2784, "SA": 2682, "JP": 2392,
}
TIMEOUT_S = 120.0  # load_async_ai_overview waits for Google to finish the overview
REDIRECT_HOPS = 3         # Google answers 301 to www.google.com/goto, then 302 to the page
REDIRECT_TIMEOUT_S = 10.0
REDIRECT_PAUSE_S = 1.0    # between links, and before the one retry of a refused link


def check(config: Dict[str, Any]) -> None:
    missing = [k for k in ("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD") if not os.environ.get(k)]
    if missing:
        raise EngineConfigError(f"google_ai: {' and '.join(missing)} not set")


def ask(question: str, market: Market, config: Dict[str, Any]) -> EngineResult:
    import httpx

    location_code = config.get("location_code") or LOCATION_CODES.get(market.country.upper())
    if not location_code:
        raise EngineConfigError(f"google_ai: no location code for {market.country}; set location_code")
    task = {"keyword": question, "location_code": int(location_code), "language_code": market.language,
            "device": "desktop", "load_async_ai_overview": True}
    auth = (os.environ["DATAFORSEO_LOGIN"], os.environ["DATAFORSEO_PASSWORD"])
    try:
        response = httpx.post(ENDPOINT, json=[task], auth=auth, timeout=TIMEOUT_S)
        response.raise_for_status()
        raw = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise EngineError(f"dataforseo: {exc}") from exc
    settings = {"route": "dataforseo", "endpoint": ENDPOINT, "task": task,
                "country": market.country, "language": market.language}
    text, cited, cost, present = parse(raw)
    model = "google-ai-overview"
    if present and not text.strip():
        # An AI Overview that arrives with no text did not finish loading. That is a vendor
        # fault to retry, not a search with no AI Overview.
        raise EngineError("dataforseo: the AI Overview came back with no text", cost_usd=cost)
    if not text.strip():
        return EngineResult(model=model, settings=settings, raw=raw, text="", cited_urls=[], valid=False,
                            invalid_reason="no_ai_overview", cost_usd=cost)
    cited, settings["redirects"] = resolve_redirects(cited)
    return EngineResult(model=model, settings=settings, raw=raw, text=text, cited_urls=cited, valid=True,
                        cost_usd=cost)


def _is_goto(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname in ("google.com", "www.google.com") and parsed.path == "/goto"


# get(url) -> (status code, Location header). Raises EngineError when the request fails.
Get = Callable[[str], Tuple[int, str]]


def _follow(get: Get, url: str) -> str:
    """The address a google.com/goto link leads to, or "" when Google does not give one."""
    for _ in range(REDIRECT_HOPS):
        try:
            status, location = get(url)
            if status == 429 or status >= 500:
                time.sleep(REDIRECT_PAUSE_S)
                status, location = get(url)
        except EngineError:
            return ""
        if status not in (301, 302, 303, 307, 308) or not location:
            return ""
        if not _is_goto(location):
            return location if urlparse(location).scheme in ("http", "https") else ""
        url = location
    return ""


def _google_get() -> Get:
    import httpx

    client = httpx.Client(timeout=REDIRECT_TIMEOUT_S, follow_redirects=False, headers={"User-Agent": "Mozilla/5.0"})

    def get(url: str) -> Tuple[int, str]:
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            raise EngineError(f"google: {exc}") from exc
        return response.status_code, response.headers.get("location") or ""
    return get


def resolve_redirects(urls: List[str], get: Optional[Get] = None) -> Tuple[List[str], Dict[str, int]]:
    """(urls with each google.com/goto link replaced by the page it leads to, counts).
    Only Google's hosts are requested, one link at a time."""
    counts = {"found": sum(1 for u in urls if _is_goto(u)), "resolved": 0}
    if not counts["found"]:
        return urls, counts
    get = get or _google_get()
    out: List[str] = []
    for url in urls:
        if _is_goto(url):
            if counts["resolved"] or any(_is_goto(u) for u in out):
                time.sleep(REDIRECT_PAUSE_S)
            real = _follow(get, url)
            counts["resolved"] += bool(real)
            url = real or url
        out.append(url)
    return list(dict.fromkeys(out)), counts


def _real_url(url: str) -> str:
    """A google.com/goto redirect hides the site; take its target when the link
    carries one (inference: a url or q parameter), else keep the link."""
    parsed = urlparse(url)
    if parsed.hostname in ("google.com", "www.google.com") and parsed.path == "/goto":
        query = parse_qs(parsed.query)
        for key in ("url", "q"):
            target = (query.get(key) or [""])[0]
            if urlparse(target).scheme in ("http", "https"):
                return target
    return url


def parse(raw: Dict[str, Any]) -> Tuple[str, List[str], Optional[float], bool]:
    """(text, cited_urls, cost_usd, an AI Overview item was present). Raises EngineError for
    a failed request or task."""
    if raw.get("status_code") != 20000:
        raise EngineError(f"dataforseo: status {raw.get('status_code')} {raw.get('status_message')}")
    tasks = raw.get("tasks") or []
    if not tasks:
        raise EngineError("dataforseo: response has no task")
    task = tasks[0]
    if task.get("status_code") != 20000 or not task.get("result"):
        raise EngineError(f"dataforseo: task status {task.get('status_code')} {task.get('status_message')}")
    cost = task.get("cost")
    items = task["result"][0].get("items") or []
    overview = next((i for i in items if i.get("type") == "ai_overview"), None)
    if overview is None:
        return "", [], cost, False
    texts: List[str] = []
    urls: List[str] = []
    for element in overview.get("items") or []:
        if element.get("text"):
            texts.append(element["text"])
        urls.extend(ref.get("url") or "" for ref in element.get("references") or [])
    urls.extend(ref.get("url") or "" for ref in overview.get("references") or [])
    text = "\n\n".join(texts) or overview.get("markdown") or overview.get("text") or ""
    return text, list(dict.fromkeys(_real_url(u) for u in urls if u)), cost, True
