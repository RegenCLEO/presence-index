"""AI engines. One adapter module per engine, all with the same call.

An adapter module defines:
  VENDOR             who bills the call, for the cost log
  REQUIRED           keys the panel's engine entry must hold (for example "model")
  check(config)      optional: raise EngineConfigError when credentials are missing
  ask(question, market, config) -> EngineResult

PS-1.0 settings apply to every adapter: web search on, no system prompt, no memory,
a new session for each call, and the country from the panel. The language from the panel
is sent to Google AI Overviews, Gemini and the chatgpt.com scraper. OpenAI's Responses
API and Anthropic's Messages API have no language field, so ChatGPT on OpenAI's API and
Claude get the question alone. The model is the one the panel names, except on the
chatgpt.com scraper, where the app picks it. No adapter has a default model.
"""
from __future__ import annotations

import importlib
from typing import Any, Dict

from scan.records import EngineResult, Market

ADAPTERS = {
    "chatgpt": "scan.engines.chatgpt",
    "chatgpt_scraper": "scan.engines.chatgpt_scraper",
    "google_ai": "scan.engines.google_aio",
    "claude": "scan.engines.claude",
    "gemini": "scan.engines.gemini",
    "fake": "scan.engines.fake",
}


class EngineConfigError(ValueError):
    pass


def adapter(engine: str, config: Dict[str, Any]):
    name = config.get("adapter", engine)
    if name not in ADAPTERS:
        raise EngineConfigError(f"engine {engine}: no adapter named {name!r}")
    return importlib.import_module(ADAPTERS[name])


def check(engine: str, config: Dict[str, Any]) -> None:
    """Fail before any call when the panel's entry for an engine is incomplete."""
    module = adapter(engine, config)
    missing = [key for key in getattr(module, "REQUIRED", ()) if not config.get(key)]
    if missing:
        raise EngineConfigError(f"engine {engine}: the panel must set {missing}")
    if hasattr(module, "check"):
        module.check(config)


def ask(engine: str, question: str, market: Market, config: Dict[str, Any]) -> EngineResult:
    return adapter(engine, config).ask(question, market, config)


def vendor(engine: str, config: Dict[str, Any]) -> str:
    return adapter(engine, config).VENDOR
