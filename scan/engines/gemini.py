"""Gemini: Google's official API with Google Search grounding, through google-genai.

Request: ``client.models.generate_content(model, contents=question, config=
GenerateContentConfig(tools=[Tool(google_search=GoogleSearch())], tool_config=
ToolConfig(retrieval_config=RetrievalConfig(language_code=<market language>))))``.
No system instruction, no sampling parameters, no fallback model. Grounding takes no
country code, only a point. A country's centroid is one town, which would localise
"near me" answers there, so no point is sent unless the panel sets
``lat_lng: [lat, lng]`` for the engine. The row's settings record what was sent.

Routes (``config["route"]``, default "vertex"):
  vertex     Vertex AI. GOOGLE_API_KEY or GEMINI_API_KEY as an express key, else a
             service account from VERTEX_AI_SA_JSON or VERTEX_AI_SA_PATH, with the
             project from config["project"], VERTEX_AI_PROJECT_ID, GOOGLE_CLOUD_PROJECT
             or the account file, and the location from config["location"],
             VERTEX_AI_LOCATION, GOOGLE_CLOUD_LOCATION or "global".
  developer  Gemini Developer API with GEMINI_API_KEY.

Grounding sources arrive as redirect URLs on vertexaisearch.cloud.google.com. ``ask``
follows each redirect once and keeps both URLs in raw["resolved"].
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from scan.engines import EngineConfigError
from scan.records import EngineError, EngineResult, Market

VENDOR = "google"
REQUIRED = ("model",)

REDIRECT_HOSTS = ("vertexaisearch.cloud.google.com",)
TIMEOUT_MS = 180_000
BLOCK_REASONS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY"}


def _route(config: Dict[str, Any]) -> str:
    route = config.get("route", "vertex")
    if route not in ("vertex", "developer"):
        raise EngineConfigError(f"gemini: route must be 'vertex' or 'developer', not {route!r}")
    return route


def _express_key() -> Optional[str]:
    return os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")


def check(config: Dict[str, Any]) -> None:
    if _route(config) == "developer":
        if not os.environ.get("GEMINI_API_KEY"):
            raise EngineConfigError("gemini: the developer route needs GEMINI_API_KEY")
        return
    if not (_express_key() or os.environ.get("VERTEX_AI_SA_JSON") or os.environ.get("VERTEX_AI_SA_PATH")):
        raise EngineConfigError("gemini: the vertex route needs GOOGLE_API_KEY, GEMINI_API_KEY, "
                                "VERTEX_AI_SA_JSON or VERTEX_AI_SA_PATH")


def _timeout():
    """The SDK sets no timeout of its own, and a stalled call would hold its slot for good."""
    from google.genai import types
    return types.HttpOptions(timeout=TIMEOUT_MS)


def client(config: Dict[str, Any]) -> Tuple[Any, str]:
    """(genai.Client, auth kind) for the configured route."""
    from google import genai

    if _route(config) == "developer":
        return genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=_timeout()), "developer_api_key"
    key = _express_key()
    if key:
        return genai.Client(vertexai=True, api_key=key, http_options=_timeout()), "vertex_express_key"
    from google.oauth2 import service_account

    text = os.environ.get("VERTEX_AI_SA_JSON")
    if not text:
        with open(os.environ["VERTEX_AI_SA_PATH"], encoding="utf-8") as fh:
            text = fh.read()
    info = json.loads(text)
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    project = (config.get("project") or os.environ.get("VERTEX_AI_PROJECT_ID")
               or os.environ.get("GOOGLE_CLOUD_PROJECT") or info.get("project_id"))
    location = (config.get("location") or os.environ.get("VERTEX_AI_LOCATION")
                or os.environ.get("GOOGLE_CLOUD_LOCATION") or "global")
    return genai.Client(vertexai=True, project=project, location=location, credentials=credentials,
                        http_options=_timeout()), "vertex_service_account"


def follow_redirect(url: str) -> Optional[str]:
    """The Location a grounding redirect points to, without fetching the target page."""
    import httpx

    try:
        response = httpx.get(url, follow_redirects=False, timeout=15.0)
    except httpx.HTTPError:
        return None
    location = response.headers.get("location") if response.is_redirect else None
    return location if location and urlparse(location).scheme in ("http", "https") else None


def ask(question: str, market: Market, config: Dict[str, Any]) -> EngineResult:
    import httpx
    from google.genai import errors, types

    latlng = config.get("lat_lng")
    retrieval: Dict[str, Any] = {"language_code": market.language}
    if latlng:
        retrieval["lat_lng"] = types.LatLng(latitude=float(latlng[0]), longitude=float(latlng[1]))
    options: Dict[str, Any] = {
        "tools": [types.Tool(google_search=types.GoogleSearch())],
        "tool_config": types.ToolConfig(retrieval_config=types.RetrievalConfig(**retrieval)),
    }
    if config.get("max_output_tokens"):
        options["max_output_tokens"] = int(config["max_output_tokens"])
    sdk, auth = client(config)
    try:
        response = sdk.models.generate_content(model=config["model"], contents=question,
                                               config=types.GenerateContentConfig(**options))
    except (errors.APIError, httpx.HTTPError) as exc:
        raise EngineError(f"gemini: {exc}") from exc
    raw = build_raw(response.model_dump(mode="json", exclude_none=True), follow_redirect)
    settings = {
        "route": _route(config), "auth": auth, "sdk_method": "models.generate_content", "tool": "google_search",
        "country": market.country, "language": market.language, "language_code": market.language,
        "lat_lng": list(latlng) if latlng else None, "max_output_tokens": options.get("max_output_tokens"),
    }
    usage = tokens(raw["response"])
    text, cited, blocked = parse(raw)
    finish = ((raw["response"].get("candidates") or [{}])[0]).get("finish_reason")
    if finish == "MAX_TOKENS":
        raise EngineError("gemini: the answer was cut off at the output limit")
    if blocked:
        return EngineResult(model=config["model"], settings=settings, raw=raw, text=text, cited_urls=cited,
                            valid=False, invalid_reason="blocked", usage=usage)
    if not text.strip():
        raise EngineError("gemini: empty answer")
    return EngineResult(model=config["model"], settings=settings, raw=raw, text=text, cited_urls=cited,
                        valid=True, usage=usage)


def tokens(response: Dict[str, Any]) -> Dict[str, int]:
    meta = response.get("usage_metadata") or {}
    # Thinking tokens are billed as output, so they count in output_tokens (inference).
    return {"input_tokens": meta.get("prompt_token_count") or 0,
            "output_tokens": (meta.get("candidates_token_count") or 0) + (meta.get("thoughts_token_count") or 0)}


def _chunks(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates = response.get("candidates") or []
    meta = (candidates[0].get("grounding_metadata") or {}) if candidates else {}
    return [c["web"] for c in meta.get("grounding_chunks") or [] if c.get("web")]


def build_raw(response: Dict[str, Any], resolve: Callable[[str], Optional[str]]) -> Dict[str, Any]:
    """The stored raw: the vendor response untouched, plus where each redirect led."""
    resolved: Dict[str, Optional[str]] = {}
    for web in _chunks(response):
        uri = web.get("uri") or ""
        if uri and uri not in resolved and urlparse(uri).hostname in REDIRECT_HOSTS:
            resolved[uri] = resolve(uri)
    return {"response": response, "resolved": resolved}


def _host_like(value: Optional[str]) -> Optional[str]:
    host = (value or "").strip().lower().split("/")[0].split("?")[0]
    host = host[4:] if host.startswith("www.") else host
    ok = "." in host and " " not in host and host not in REDIRECT_HOSTS
    return host if ok else None


def parse(raw: Dict[str, Any]) -> Tuple[str, List[str], bool]:
    """(text, cited_urls, blocked). Text is every non-thought text part, joined."""
    response = raw.get("response") or {}
    candidates = response.get("candidates") or []
    if not candidates:
        # No candidate is a block only when Google says so. Otherwise it is a fault to retry.
        return "", [], bool((response.get("prompt_feedback") or {}).get("block_reason"))
    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p.get("text") or "" for p in parts if not p.get("thought")).strip()
    if not text and candidate.get("finish_reason") in BLOCK_REASONS:
        return "", [], True
    resolved = raw.get("resolved") or {}
    urls: List[str] = []
    for web in _chunks(response):
        uri = web.get("uri") or ""
        if urlparse(uri).hostname not in REDIRECT_HOSTS:
            if uri:
                urls.append(uri)
            continue
        real = resolved.get(uri)
        if not real:
            host = _host_like(web.get("domain")) or _host_like(web.get("title"))
            real = f"https://{host}/" if host else None
        if real:
            urls.append(real)
    return text, list(dict.fromkeys(urls)), False
