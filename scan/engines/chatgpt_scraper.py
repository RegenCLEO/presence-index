"""ChatGPT as its app answers: DataForSEO's ChatGPT scraper.

Request: POST one task ``{keyword, location_code, language_code, force_web_search: true}``
to ai_optimization/chat_gpt/llm_scraper/live/advanced with Basic auth. DataForSEO asks
chatgpt.com and returns the answer, the model that wrote it and the sources it cited.

The panel selects this adapter with ``chatgpt: {adapter: chatgpt_scraper}``. It cannot
name the model: the app chooses it, and the answer row records the one DataForSEO reports.
The request has no field for a system prompt, a memory or a session, so the scan cannot
set them and relies on the vendor.

Only ``sources``, the pages the answer cited, count as cited URLs. ``search_results``
lists every page the model retrieved, cited or not, and is left out. There is no fallback
to OpenAI's API: a failed scrape is asked again on the next run, so every ChatGPT answer
in a scan has one origin.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from scan.engines import EngineConfigError
from scan.engines.google_aio import LOCATION_CODES
from scan.records import EngineError, EngineResult, Market

VENDOR = "dataforseo"
REQUIRED = ()
ENDPOINT = "https://api.dataforseo.com/v3/ai_optimization/chat_gpt/llm_scraper/live/advanced"
MAX_QUESTION_CHARS = 2000   # the endpoint's limit on ``keyword``
TIMEOUT_S = 150.0           # the endpoint runs a task for up to 120 seconds
MODEL_NOT_REPORTED = "chatgpt-app"


def check(config: Dict[str, Any]) -> None:
    missing = [k for k in ("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD") if not os.environ.get(k)]
    if missing:
        raise EngineConfigError(f"chatgpt scraper: {' and '.join(missing)} not set")


def keyword(question: str) -> str:
    """The endpoint decodes %XX and reads '+' as a space, so both are escaped."""
    if len(question) > MAX_QUESTION_CHARS:
        raise EngineConfigError(f"chatgpt scraper: a question is longer than {MAX_QUESTION_CHARS} characters")
    return question.replace("%", "%25").replace("+", "%2B")


def ask(question: str, market: Market, config: Dict[str, Any]) -> EngineResult:
    import httpx

    location_code = config.get("location_code") or LOCATION_CODES.get(market.country.upper())
    if not location_code:
        raise EngineConfigError(f"chatgpt scraper: no location code for {market.country}; set location_code")
    task = {"keyword": keyword(question), "location_code": int(location_code),
            "language_code": market.language, "force_web_search": True}
    auth = (os.environ["DATAFORSEO_LOGIN"], os.environ["DATAFORSEO_PASSWORD"])
    try:
        response = httpx.post(ENDPOINT, json=[task], auth=auth, timeout=TIMEOUT_S)
        response.raise_for_status()
        raw = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise EngineError(f"dataforseo: {exc}") from exc
    text, cited, cost, model = parse(raw)
    if not text.strip():
        raise EngineError("dataforseo: the ChatGPT scraper returned no answer text", cost_usd=cost)
    settings = {"route": "dataforseo_chatgpt_scraper", "endpoint": ENDPOINT, "task": task,
                "country": market.country, "language": market.language, "model_chosen_by": "chatgpt.com"}
    return EngineResult(model=model or MODEL_NOT_REPORTED, settings=settings, raw=raw, text=text,
                        cited_urls=cited, valid=True, cost_usd=cost)


def parse(raw: Dict[str, Any]) -> Tuple[str, List[str], Optional[float], Optional[str]]:
    """(text, cited_urls, cost_usd, model). Raises EngineError for a failed request or task."""
    if raw.get("status_code") != 20000:
        raise EngineError(f"dataforseo: status {raw.get('status_code')} {raw.get('status_message')}")
    tasks = raw.get("tasks") or []
    if not tasks:
        raise EngineError("dataforseo: response has no task")
    task = tasks[0]
    cost = task.get("cost")
    if task.get("status_code") != 20000 or not task.get("result"):
        raise EngineError(f"dataforseo: task status {task.get('status_code')} {task.get('status_message')}",
                          cost_usd=cost)
    result = task["result"][0] or {}
    items = [item for item in result.get("items") or [] if isinstance(item, dict)]
    text = (result.get("markdown") or "").strip()
    if not text:
        text = "\n\n".join(part for part in ((item.get("markdown") or "").strip() for item in items) if part)
    sources = list(result.get("sources") or [])
    for item in items:
        sources.extend(item.get("sources") or [])
    urls = [(source.get("url") or "").strip() for source in sources if isinstance(source, dict)]
    return text, list(dict.fromkeys(url for url in urls if url)), cost, result.get("model") or None
