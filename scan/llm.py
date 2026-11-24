"""The reader's model calls: one user message, no web search, a JSON object back.

anthropic  messages.create with output_config effort "low". JSON output there needs a
           schema, so the first JSON object in the text is parsed.
openai     responses.create with text.format json_object, store=False. The API needs
           the word "JSON" in the prompt for this format.
gemini     models.generate_content with response_mime_type "application/json", on the
           client the gemini engine builds (route "vertex" unless SCAN_GEMINI_ROUTE says
           "developer").
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict

from scan.records import EngineError, LlmResult

READER_MAX_TOKENS = 8000


def first_json_object(text: str) -> Dict[str, Any]:
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            value, _ = decoder.raw_decode(text, start)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(value, dict):
            return value
        start = text.find("{", start + 1)
    raise EngineError(f"reply is not a JSON object: {text[:200]!r}")


def _anthropic(model: str, prompt: str):
    import anthropic
    from scan.engines import claude

    try:
        response = claude.client().messages.create(model=model, max_tokens=READER_MAX_TOKENS,
                                                   output_config={"effort": "low"},
                                                   messages=[{"role": "user", "content": prompt}])
    except anthropic.APIError as exc:
        raise EngineError(f"anthropic: {exc}") from exc
    raw = response.to_dict(mode="json")
    text = "".join(b.get("text") or "" for b in raw.get("content") or [] if b.get("type") == "text")
    return text, claude.tokens([raw])


def _openai(model: str, prompt: str):
    import openai
    from scan.engines import chatgpt

    try:
        response = chatgpt.client().responses.create(model=model, input=prompt, store=False,
                                                     text={"format": {"type": "json_object"}})
    except openai.OpenAIError as exc:
        raise EngineError(f"openai: {exc}") from exc
    raw = response.to_dict(mode="json")
    text, _, _, _ = chatgpt.parse(raw)
    return text, chatgpt.tokens(raw)


def _gemini(model: str, prompt: str):
    import httpx
    from google.genai import errors, types
    from scan.engines import gemini

    sdk, _ = gemini.client({"route": os.environ.get("SCAN_GEMINI_ROUTE", "vertex")})
    try:
        response = sdk.models.generate_content(
            model=model, contents=prompt, config=types.GenerateContentConfig(response_mime_type="application/json"))
    except (errors.APIError, httpx.HTTPError) as exc:
        raise EngineError(f"gemini: {exc}") from exc
    raw = response.model_dump(mode="json", exclude_none=True)
    text, _, _ = gemini.parse({"response": raw})
    return text, gemini.tokens(raw)


PROVIDERS = {"anthropic": _anthropic, "openai": _openai, "gemini": _gemini}


def complete_json(provider: str, model: str, prompt: str) -> LlmResult:
    if provider not in PROVIDERS:
        raise ValueError(f"no reader provider named {provider!r}")
    text, usage = PROVIDERS[provider](model, prompt)
    return LlmResult(data=first_json_object(text), model=model, usage=usage, cost_usd=None)
