"""ChatGPT: OpenAI's Responses API with the web search tool.

Request: ``client.responses.create(model, input=question, tools=[web_search with an
approximate user location], store=False)``. No instructions, no previous response, no
sampling parameters. The Responses API has no language field, so the language is not sent.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

from scan.engines import EngineConfigError
from scan.records import EngineError, EngineResult, Market

VENDOR = "openai"
REQUIRED = ("model",)


def check(config: Dict[str, Any]) -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise EngineConfigError("chatgpt: OPENAI_API_KEY is not set")


def client():
    import openai
    # The run stage owns retries, so the SDK makes one attempt.
    return openai.OpenAI(max_retries=0)


def ask(question: str, market: Market, config: Dict[str, Any]) -> EngineResult:
    import openai

    tool = {"type": "web_search", "user_location": {"type": "approximate", "country": market.country}}
    try:
        response = client().responses.create(model=config["model"], input=question, tools=[tool], store=False)
    except openai.OpenAIError as exc:
        raise EngineError(f"openai: {exc}") from exc
    raw = response.to_dict(mode="json")
    if raw.get("error") or raw.get("status") in ("failed", "incomplete", "cancelled"):
        raise EngineError(f"openai: response {raw.get('status')}: {raw.get('error') or raw.get('incomplete_details')}")
    if any(item.get("type") == "web_search_call" and item.get("status") == "failed" for item in raw.get("output") or []):
        raise EngineError("openai: a web search call failed")
    text, cited, refused, searches = parse(raw)
    settings = {
        "route": "openai", "sdk_method": "responses.create", "tool": tool, "store": False,
        "country": market.country, "language": market.language, "language_sent": False,
        "web_search_calls": searches,
    }
    usage = {**tokens(raw), "web_search_calls": searches}
    if refused and not text:
        return EngineResult(model=config["model"], settings=settings, raw=raw, text="", cited_urls=[],
                            valid=False, invalid_reason="refusal", usage=usage)
    if not text.strip():
        raise EngineError(f"openai: empty answer (status={raw.get('status')}, "
                          f"incomplete_details={raw.get('incomplete_details')})")
    return EngineResult(model=config["model"], settings=settings, raw=raw, text=text, cited_urls=cited,
                        valid=True, usage=usage)


def tokens(raw: Dict[str, Any]) -> Dict[str, int]:
    usage = raw.get("usage") or {}
    return {"input_tokens": usage.get("input_tokens") or 0, "output_tokens": usage.get("output_tokens") or 0}


def parse(raw: Dict[str, Any]) -> Tuple[str, List[str], bool, int]:
    """(text, cited_urls, refused, web_search_calls) from a Responses API body."""
    texts: List[str] = []
    urls: List[str] = []
    refused = False
    searches = 0
    for item in raw.get("output") or []:
        if item.get("type") == "web_search_call":
            searches += 1
            continue
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "refusal":
                refused = True
            if part.get("type") != "output_text":
                continue
            if part.get("text"):
                texts.append(part["text"])
            for note in part.get("annotations") or []:
                if note.get("type") == "url_citation" and note.get("url"):
                    urls.append(note["url"].strip())
    return "\n\n".join(texts), list(dict.fromkeys(urls)), refused, searches
