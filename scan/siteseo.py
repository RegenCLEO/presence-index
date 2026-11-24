"""Site Content and Technical SEO for the public scan: one score for a brand's homepage.

60 points come from the SEO category of Google's Lighthouse, read from the PageSpeed
Insights API on its mobile setting. 40 points come from five facts in the homepage HTML,
worth 8 each: a title, a meta description, one canonical URL, an h1 with text, and the
share of images that have an alt attribute. The rule is METHODOLOGY.md, Appendix D.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional, Tuple

PAGESPEED_API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
KEY_ENV = "PAGESPEED_API_KEY"
SOURCE = "Lighthouse SEO category from PageSpeed Insights v5, mobile (60) + homepage HTML facts (40)"
ATTEMPTS = 3
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class PageSpeedError(Exception):
    """PageSpeed gave no SEO category score for the page."""


# ---------- the 40 points read from the HTML ----------
@dataclass(frozen=True)
class OnPage:
    title: bool
    meta_description: bool
    canonical: bool          # one canonical URL; two different ones do not count
    h1: bool
    images: int
    images_with_alt: int     # alt="" counts: it is the correct markup for a decorative image

    @property
    def points(self) -> int:
        alt = 8 if self.images == 0 else round(8 * self.images_with_alt / self.images)
        return 8 * (self.title + self.meta_description + self.canonical + self.h1) + alt


# Elements that never hold content, so they are closed as soon as they open.
_VOID = frozenset({"area", "base", "basefont", "bgsound", "br", "col", "command", "embed", "frame", "hr", "image",
                   "img", "input", "isindex", "keygen", "link", "menuitem", "meta", "nextid", "param", "source",
                   "spacer", "track", "wbr"})
# Text inside these is code or an annotation, not what the page says.
_NOT_TEXT = frozenset({"script", "style", "template", "rt", "rp"})


class _Reader(HTMLParser):
    """Keeps the open elements, so a heading or title a page never closes ends where its
    parent ends, and text is credited only to the elements it sits in."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open: List[str] = []
        self.title_depth: Optional[int] = None  # where the first <title> sits in open, while it is open
        self.title_seen = self.title = self.h1 = False
        self.description = ""
        self.canonicals: List[str] = []
        self.images = self.images_with_alt = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title" and not self.title_seen:
            self.title_seen, self.title_depth = True, len(self.open)
        elif tag == "meta" and (a.get("name") or "").lower() == "description":
            self.description = a.get("content") or ""  # the last one in the page wins
        elif tag == "link" and "canonical" in (a.get("rel") or "").split() and a.get("href"):
            self.canonicals.append(a["href"])
        elif tag == "img":
            self.images += 1
            self.images_with_alt += "alt" in a
        if tag not in _VOID:
            self.open.append(tag)

    def handle_endtag(self, tag):
        if tag not in self.open:
            return
        while self.open.pop() != tag:
            pass
        if self.title_depth is not None and len(self.open) <= self.title_depth:
            self.title_depth = None

    def handle_data(self, data):
        if not data.strip() or not _NOT_TEXT.isdisjoint(self.open):
            return
        if self.title_depth is not None:
            self.title = True
        if "h1" in self.open:
            self.h1 = True

    def unknown_decl(self, data):
        if data.startswith("CDATA["):
            self.handle_data(data[len("CDATA["):])


def on_page(html: str) -> OnPage:
    r = _Reader()
    r.feed(html or "")
    r.close()
    return OnPage(
        title=r.title,
        meta_description=bool(r.description.strip()),
        canonical=bool(r.canonicals and r.canonicals[0].strip() and len(set(r.canonicals)) == 1),
        h1=r.h1, images=r.images, images_with_alt=r.images_with_alt)


def composite(lighthouse_seo: float, on_page_points: int) -> int:
    """0 to 100. lighthouse_seo is the category score from 0 to 1, taken as a whole per cent."""
    per_cent = int(round(lighthouse_seo * 100))
    return max(0, min(100, round(per_cent / 100 * 60) + on_page_points))


# ---------- the 60 points read from PageSpeed ----------
# get(params, headers) -> (status code, parsed JSON body or None). Raises ConnectionError.
Get = Callable[[Dict[str, str], Dict[str, str]], Tuple[int, Any]]


def http_get(params: Dict[str, str], headers: Dict[str, str]) -> Tuple[int, Any]:
    import httpx

    try:
        resp = httpx.get(PAGESPEED_API, params=params, headers=headers, timeout=90.0)
    except httpx.HTTPError as exc:
        raise ConnectionError(type(exc).__name__) from exc
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, None


def pagespeed_seo(url: str, get: Get = http_get, sleep: Callable[[float], None] = time.sleep) -> float:
    """The Lighthouse SEO category score, 0 to 1, for one page. Raises PageSpeedError.

    PageSpeed loads the page itself from Google's side. The key goes in a header, never in
    the query string, so it cannot reach an error message.
    """
    error = ""
    for attempt in range(ATTEMPTS):
        if attempt:
            sleep(3 * 2 ** (attempt - 1))
        try:
            status, body = get({"url": url, "strategy": "mobile", "category": "seo"},
                               {"X-Goog-Api-Key": os.environ[KEY_ENV]})
        except ConnectionError as exc:
            error = str(exc)
            continue
        if status in RETRY_STATUS:
            error = f"HTTP {status}"
            continue
        if status != 200:
            raise PageSpeedError(f"HTTP {status}")
        try:
            score = body["lighthouseResult"]["categories"]["seo"]["score"]
        except (KeyError, TypeError):
            score = None
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise PageSpeedError("the response has no SEO category score")
        return float(score)
    raise PageSpeedError(f"{error} after {ATTEMPTS} attempts")


# ---------- the sites.json entry ----------
def measure(url: str, html: str, lighthouse: Callable[[str], float] = pagespeed_seo) -> Dict[str, Any]:
    """The site_seo entry for a homepage the site read already fetched: a score, or null
    with a reason. Nothing here asks the brand's site for a page.

    Without the Lighthouse score there is no score at all. A score from the HTML facts alone
    would be a different formula, and every scored brand must be on the same one.
    """
    if not os.environ.get(KEY_ENV):
        return {"score": None, "source": None, "reason": "pagespeed_key_missing"}
    try:
        facts = on_page(html)
        seo = lighthouse(url)
    except PageSpeedError as exc:
        return {"score": None, "source": SOURCE, "reason": "pagespeed_unavailable", "detail": str(exc)}
    except Exception as exc:
        return {"score": None, "source": SOURCE, "reason": "site_seo_failed", "detail": f"{type(exc).__name__}: {exc}"}
    return {"score": composite(seo, facts.points), "source": SOURCE, "reason": None,
            "lighthouse_seo": seo, "on_page_points": facts.points}
