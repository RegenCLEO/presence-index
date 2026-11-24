"""An engine that answers from a hash of the question. For tests and offline dry runs.

Panel entry: ``{adapter: fake, seed: "a", brands: [{name, domain}], missing_every: 5}``.
With ``missing_every`` set, that share of questions returns no answer, as a search with
no AI Overview does.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict

from scan.records import EngineResult, Market

VENDOR = "fake"
REQUIRED = ("brands",)


def ask(question: str, market: Market, config: Dict[str, Any]) -> EngineResult:
    h = int(hashlib.sha256(f"{config.get('seed', '')}|{question}".encode()).hexdigest(), 16)
    settings = {"adapter": "fake", "country": market.country, "language": market.language}
    every = config.get("missing_every")
    if every and h % every == 0:
        return EngineResult(model="fake-1", settings=settings, raw={"answer": None}, text="", cited_urls=[],
                            valid=False, invalid_reason="no_ai_overview", usage={"calls": 1}, cost_usd=0.0)
    brands = config["brands"]
    first = brands[h % len(brands)]
    second = brands[(h // 7) % len(brands)]
    text = f"For {question} I recommend {first['name']}."
    if second is not first:
        text += f" {second['name']} is also an option."
    cited = [f"https://www.{first['domain']}/deals", "https://reviews.example.org/guide"]
    return EngineResult(model="fake-1", settings=settings, raw={"answer": text, "sources": cited}, text=text,
                        cited_urls=cited, valid=True, usage={"calls": 1}, cost_usd=0.0)
