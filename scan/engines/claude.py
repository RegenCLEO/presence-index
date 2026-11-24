"""Claude: Anthropic's Messages API with the server web search tool.

Request: ``client.messages.create(model, max_tokens, tools=[web search], messages=[the
question])``. No system prompt, no sampling parameters, no fallbacks. A ``pause_turn``
is resumed by sending the question and the paused assistant turn back unchanged, as
the Claude API reference (shared/tool-use-concepts.md) says. The Messages API has no
language field, so the language is not sent.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

from scan.engines import EngineConfigError
from scan.records import EngineError, EngineResult, Market

VENDOR = "anthropic"
REQUIRED = ("model",)

# The basic web search tool, the one CLEO's product uses. With web_search_20260209 the
# model runs its searches inside code execution and the answer's text blocks carry no
# citations (rehearsal of 21 November 2026: 0 cited URLs in 6 answers, against 5 an answer
# with this tool). config["web_search_tool"] overrides it.
WEB_SEARCH_TOOL = "web_search_20250305"
MAX_TOKENS = 16000      # under the SDK's non-streaming limit; thinking counts against it
MAX_CONTINUATIONS = 5   # pause_turn resumes per question


def check(config: Dict[str, Any]) -> None:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise EngineConfigError("claude: ANTHROPIC_API_KEY is not set")


def client():
    import anthropic
    # The run stage owns retries, so the SDK makes one attempt.
    return anthropic.Anthropic(max_retries=0)


def search_tool(model: str, market: Market, config: Dict[str, Any]) -> Dict[str, Any]:
    tool: Dict[str, Any] = {"type": config.get("web_search_tool") or WEB_SEARCH_TOOL, "name": "web_search",
                            "user_location": {"type": "approximate", "country": market.country}}
    if config.get("max_searches"):
        tool["max_uses"] = int(config["max_searches"])
    return tool


def ask(question: str, market: Market, config: Dict[str, Any]) -> EngineResult:
    import anthropic

    model = config["model"]
    tool = search_tool(model, market, config)
    max_tokens = int(config.get("max_tokens", MAX_TOKENS))
    sdk = client()
    turns: List[Dict[str, Any]] = []
    messages: List[Dict[str, Any]] = [{"role": "user", "content": question}]
    try:
        for _ in range(MAX_CONTINUATIONS + 1):
            response = sdk.messages.create(model=model, max_tokens=max_tokens, tools=[tool], messages=messages)
            turns.append(response.to_dict(mode="json"))
            if response.stop_reason != "pause_turn":
                break
            # Every paused turn goes back, in order: a later turn's tool results refer to
            # tool calls made in an earlier one.
            messages.append({"role": "assistant", "content": response.content})
    except anthropic.APIError as exc:
        raise EngineError(f"anthropic: {exc}") from exc
    raw = {"turns": turns}
    settings = {
        "route": "anthropic", "sdk_method": "messages.create", "tool": tool, "max_tokens": max_tokens,
        "country": market.country, "language": market.language, "language_sent": False,
        "turns": len(turns),
    }
    usage = tokens(turns)
    stop = turns[-1].get("stop_reason")
    text, cited = parse(raw)
    if stop == "refusal":
        return EngineResult(model=model, settings=settings, raw=raw, text=text, cited_urls=cited,
                            valid=False, invalid_reason="refusal", usage=usage)
    if stop == "pause_turn":
        raise EngineError(f"anthropic: still paused after {MAX_CONTINUATIONS} continuations")
    if stop == "max_tokens":
        raise EngineError("anthropic: the answer was cut off at max_tokens")
    failed_search = search_error(raw)
    if failed_search:
        raise EngineError(f"anthropic: web search failed ({failed_search})")
    if not text.strip():
        raise EngineError(f"anthropic: empty answer (stop_reason={stop})")
    return EngineResult(model=model, settings=settings, raw=raw, text=text, cited_urls=cited,
                        valid=True, usage=usage)


def tokens(turns: List[Dict[str, Any]]) -> Dict[str, int]:
    """Usage summed over the turns, in the unit names prices.yaml uses."""
    total: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
    for turn in turns:
        usage = turn.get("usage") or {}
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            if usage.get(key) is not None:
                total[key] = total.get(key, 0) + usage[key]
        searches = (usage.get("server_tool_use") or {}).get("web_search_requests")
        if searches is not None:
            total["web_search_requests"] = total.get("web_search_requests", 0) + searches
    return total


def search_error(raw: Dict[str, Any]) -> str:
    """The error code of a web search that failed, or "". Reaching the panel's search cap
    is not a failure: the answer stands on the searches made before it."""
    for turn in raw.get("turns") or []:
        for block in turn.get("content") or []:
            content = block.get("content") if block.get("type") == "web_search_tool_result" else None
            if isinstance(content, dict) and content.get("error_code") not in (None, "max_uses_exceeded"):
                return str(content["error_code"])
    return ""


def parse(raw: Dict[str, Any]) -> Tuple[str, List[str]]:
    """(text, cited_urls). Cited URLs come from the citations on the answer's text
    blocks only; search results the answer does not cite are left out."""
    texts: List[str] = []
    urls: List[str] = []
    for turn in raw.get("turns") or []:
        for block in turn.get("content") or []:
            if block.get("type") != "text":
                continue
            texts.append(block.get("text") or "")
            for citation in block.get("citations") or []:
                if citation.get("type") == "web_search_result_location" and citation.get("url"):
                    urls.append(citation["url"])
    return "".join(texts).strip(), list(dict.fromkeys(urls))
