"""Row shapes shared by every stage, and the one place hosts and ids are matched.

The files are the contract between stages. Each row is a plain dict so it goes to
JSON unchanged; the TypedDicts below say which keys a stage may rely on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, TypedDict
from urllib.parse import urlparse


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- hosts ----------
def host_of(url_or_host: Optional[str]) -> str:
    """Lower-case host without a leading www."""
    raw = (url_or_host or "").strip().lower()
    if not raw:
        return ""
    try:
        host = urlparse(raw if "://" in raw else f"https://{raw}").hostname or ""
    except ValueError:  # for example an unclosed IPv6 bracket in a scraped link
        return ""
    host = host.rstrip(".")
    return host[4:] if host.startswith("www.") else host


def is_owned(url: Optional[str], domain: str) -> bool:
    """A URL is owned when its host is the domain or a subdomain of it."""
    host = host_of(url)
    return bool(host and domain) and (host == domain or host.endswith("." + domain))


def owned_by_any(url: Optional[str], domains: Iterable[str]) -> bool:
    return any(is_owned(url, d) for d in domains)


# ---------- social profiles ----------
# A link to one of these is a button or a post, not the brand's profile.
_NOT_A_PROFILE = frozenset({"sharer", "sharer.php", "share", "share.php", "dialog", "plugins", "tr", "login",
                            "intent", "home", "search", "hashtag", "i", "p", "reel", "reels", "explore", "accounts"})


def social_platform(url: Optional[str]) -> Optional[str]:
    """The platform a profile URL belongs to, or None when the link is not a profile."""
    raw = str(url or "")
    try:
        path = [p for p in urlparse(raw if "://" in raw else f"https://{raw}").path.split("/") if p]
    except ValueError:
        return None
    if not path:
        return None
    first = path[0].lower()
    if is_owned(raw, "linkedin.com"):
        return "linkedin" if first == "company" and len(path) > 1 else None
    if is_owned(raw, "youtube.com"):
        return "youtube" if first.startswith("@") or first == "channel" else None
    if is_owned(raw, "tiktok.com"):
        return "tiktok" if first.startswith("@") else None
    for site, platform in (("instagram.com", "instagram"), ("x.com", "x"), ("twitter.com", "x"),
                           ("facebook.com", "facebook")):
        if is_owned(raw, site):
            return None if first in _NOT_A_PROFILE else platform
    return None


# ---------- ids ----------
def answer_id(day: str, engine: str, question_id: str, run: int) -> str:
    """One answer per question, engine, day and run. ``day`` comes from the panel, never the clock."""
    return f"{day}/{engine}/{question_id}/r{run}"


def mention_id(answer: str, brand_id: str) -> str:
    return f"{answer}|{brand_id}"


def serp_id(pull: str, keyword: str) -> str:
    return f"{pull}/{keyword}"


# ---------- what a collector returns ----------
@dataclass(frozen=True)
class Market:
    country: str   # ISO 3166-1 alpha-2, for example "US"
    language: str  # ISO 639-1, for example "en"


@dataclass
class EngineResult:
    """One engine's answer to one question.

    ``valid`` is False for a final outcome that is not an answer, such as a search with
    no AI Overview. A failed call is not a result: the adapter raises EngineError and
    nothing is stored, so the next run asks again.
    """
    model: str
    settings: Dict[str, Any]
    raw: Dict[str, Any]
    text: str
    cited_urls: List[str]
    valid: bool
    invalid_reason: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    cost_usd: Optional[float] = None  # set only when the vendor returns a cost


@dataclass
class LlmResult:
    """A reader model's JSON reply, with the usage the vendor returned."""
    data: Dict[str, Any]
    model: str
    usage: Dict[str, Any] = field(default_factory=dict)
    cost_usd: Optional[float] = None


class EngineError(Exception):
    """The call failed or returned something unusable. Safe to retry.

    ``cost_usd`` is set when the vendor billed the failed call, so it still reaches the cost log.
    """

    def __init__(self, message: str, cost_usd: Optional[float] = None):
        super().__init__(message)
        self.cost_usd = cost_usd


# ---------- rows on disk ----------
class AnswerRow(TypedDict):
    id: str
    panel_id: str
    panel_sha256: str
    day: str
    question_id: str
    engine: str
    run: int
    model: str
    settings: Dict[str, Any]
    asked_at: str
    raw: Dict[str, Any]
    text: str
    cited_urls: List[str]
    valid: bool
    invalid_reason: Optional[str]


class SerpResult(TypedDict):
    position: int
    url: str


class SerpRow(TypedDict):
    id: str
    panel_id: str
    panel_sha256: str
    pull: str
    keyword: str
    volume: float
    checked_at: str
    results: List[SerpResult]  # organic results only, positions 1 to 10


class ReadRecord(TypedDict):
    model: str
    classification: str        # RECOMMENDED, MENTIONED or NOT_MENTIONED
    polarity: str              # positive, neutral or negative
    position: Optional[int]    # order of first appearance among the businesses named, from 1


class MentionRow(TypedDict):
    id: str
    panel_id: str
    panel_sha256: str
    answer_id: str
    brand_id: str
    value: float               # 1, 0.5 or 0
    classification: str
    polarity: str
    position: Optional[int]
    own_domain_cited: bool
    proposed: bool             # False when no alias or domain matched, so no model read it
    reads: List[ReadRecord]
    settled_by: str            # "not_proposed", "agree" or "tiebreak"
    reader_version: str
    names_sha: str             # Brand.names_sha when the row was written


class CostRow(TypedDict):
    panel_id: str
    day: str
    purpose: str               # answer, read, keyword or siteread
    vendor: str
    ref: str                   # id of the row this call produced
    model: str
    usage: Dict[str, Any]
    cost_usd: Optional[float]
    basis: str                 # "vendor" (cost returned by the vendor), "prices" or "unpriced"
    at: str
