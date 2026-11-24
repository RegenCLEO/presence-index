"""Read each brand's robots.txt, homepage and key pages, and write sites.json.

Without --go the stage lists each brand's key pages and stops. With --go it reads every
brand not yet read (or read and failed) and rewrites sites.json after each brand.
The fetching rules: robots.txt first and obeyed for our own user agent, at most one
request a second per host, at most three redirects, no JavaScript, no paid fetch route.
A site that blocks us is recorded as blocked and never worked around.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urldefrag, urljoin, urlparse, urlsplit

import reference
from scan import panel as panel_file, siteseo, store
from scan.panel import Brand
from scan.records import host_of, social_platform, utc_now

SITES, ANSWERS, SERP = "sites.json", "answers.jsonl", "serp.jsonl"
# Robots rules match the token before the slash. Changing it would change which sites block the scan.
USER_AGENT = "CleoCrawler/1.0 (+https://regencleo.ai/crawler/)"
ROBOTS_TOKEN = USER_AGENT.split("/", 1)[0]
MAX_KEY_PAGES = 20
MAX_REDIRECTS = 3
PARITY_MIN_WORDS = 50
BLOCK_STATUS = frozenset({401, 403, 406, 429, 451, 503})
# A homepage that is busy or rate-limiting us may answer later, so the brand is retried.
RETRY_STATUS = frozenset({429, 503})
TRACKING_PARAMS = frozenset({"gclid", "fbclid", "msclkid", "srsltid", "mc_cid", "mc_eid", "_ga", "ref_src"})
REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
VERDICT_AGENTS = tuple(dict.fromkeys((*(a for agents in reference.ANSWER_AGENTS.values() for a in agents),
                                      "Bingbot")))
_PAGETYPE_NAMED = {"Article", "NewsArticle", "BlogPosting", "Product", "Service", "FAQPage", "HowTo", "Event"}
_ORG_TYPES = ("Organization", "Corporation", "LocalBusiness", "Brand", "MedicalOrganization",
              "EducationalOrganization", "GovernmentOrganization")
_CHROME_TAGS = {"script", "style", "nav", "footer", "header", "noscript"}
# Fingerprints of bot-challenge pages from Vercel, Cloudflare, DataDome, PerimeterX, Imperva,
# Akamai and AWS WAF. Titles are matched against <title> only.
_CHALLENGE_FINGERPRINTS = ("vercel.link/security-checkpoint", "/cdn-cgi/challenge-platform/",
                           "challenges.cloudflare.com", "cf-browser-verification", "_cf_chl_opt",
                           "captcha-delivery.com", "px-captcha", "_incapsula_resource", "incapsula incident",
                           "sec-cpt", "awswaf")
_CHALLENGE_TITLES = ("security checkpoint", "verifying your browser", "verifying you are human",
                     "just a moment", "attention required! | cloudflare", "robot or human", "access denied",
                     "pardon our interruption", "are you a robot")


# ---------- the network seam ----------
@dataclass(frozen=True)
class Response:
    status: int
    headers: Dict[str, str]  # lower-case names
    text: str


class FetchError(Exception):
    """Network failure or timeout. Safe to retry."""


# fetch(url) -> Response for one GET that does not follow redirects. Raises FetchError.
Fetch = Callable[[str], Response]


def http_fetch() -> Fetch:
    import httpx

    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20.0, follow_redirects=False)

    def fetch(url: str) -> Response:
        try:
            r = client.get(url)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc
        # A header sent more than once keeps every line, joined by newlines. httpx joins them
        # with commas, which hides where one X-Robots-Tag line ends and the next begins.
        headers: Dict[str, str] = {}
        for k, v in r.headers.multi_items():
            key = k.lower()
            headers[key] = f"{headers[key]}\n{v}" if key in headers else v
        return Response(r.status_code, headers, r.text)
    return fetch


# ---------- robots.txt ----------
@dataclass
class RobotsRules:
    groups: Dict[str, List[Tuple[bool, str]]] = field(default_factory=dict)  # token -> [(allow, pattern)]


def parse_robots(text: str) -> RobotsRules:
    rules = RobotsRules()
    current: List[str] = []
    rules_seen = False
    # A byte order mark in front of the first line would hide its User-agent.
    for raw in (text or "").lstrip("\ufeff").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        directive, _, value = line.partition(":")
        directive, value = directive.strip().lower(), value.strip()
        if directive == "user-agent":
            head = value.split("/", 1)[0].split()
            token = head[0].lower() if head else ""
            if not token:
                continue
            if rules_seen:
                current, rules_seen = [], False
            current.append(token)
            rules.groups.setdefault(token, [])
        elif directive in ("allow", "disallow"):
            rules_seen = True
            if value:
                for token in current:
                    rules.groups[token].append((directive == "allow", value))
    return rules


def _path_matches(pattern: str, path: str) -> bool:
    anchored = pattern.endswith("$")
    core = pattern[:-1] if anchored else pattern
    regex = "^" + ".*".join(re.escape(part) for part in core.split("*")) + ("$" if anchored else "")
    return re.match(regex, path) is not None


def robots_allows(rules: RobotsRules, token: str, url_or_path: str = "/") -> bool:
    """Longest matching rule wins, Allow wins a tie; the most specific group the token
    starts with, else "*", else everything is allowed."""
    parsed = urlsplit(url_or_path)  # urlsplit keeps ";params" in the path, as robots rules expect
    path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    token_l = token.lower()
    specific = [k for k in rules.groups if k != "*" and token_l.startswith(k)]
    key = max(specific, key=len) if specific else "*" if "*" in rules.groups else None
    if key is None:
        return True
    best: Optional[Tuple[int, bool]] = None
    for allow, pattern in rules.groups[key]:
        if _path_matches(pattern, path) and (best is None or len(pattern) > best[0]
                                             or (len(pattern) == best[0] and allow)):
            best = (len(pattern), allow)
    return best is None or best[1]


# ---------- HTML facts ----------
@dataclass
class PageFacts:
    words: int
    noindex: bool
    canonical: Optional[str]
    json_ld: List[Any]
    links: List[str]
    title: str


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.text: List[str] = []
        self.robots: List[str] = []
        self.canonicals: List[str] = []
        self.links: List[str] = []
        self.json_ld_raw: List[str] = []
        self._ld: Optional[List[str]] = None
        self._title: Optional[List[str]] = None
        self.title = ""

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and a.get("type", "").strip().lower() == "application/ld+json":
            self._ld = []
        if tag in _CHROME_TAGS:
            self.skip += 1
        if tag == "title" and not self.title:
            self._title = []
        elif tag == "meta" and a.get("name", "").lower() in GOOGLE_AGENTS:
            self.robots.extend(d.strip().lower() for d in a.get("content", "").split(",") if d.strip())
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split() and a.get("href"):
            self.canonicals.append(a["href"].strip())
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"].strip())

    def handle_endtag(self, tag):
        if tag == "script" and self._ld is not None:
            self.json_ld_raw.append("".join(self._ld))
            self._ld = None
        if tag == "title" and self._title is not None:
            self.title = " ".join("".join(self._title).split())
            self._title = None
        if tag in _CHROME_TAGS and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if self._ld is not None:
            self._ld.append(data)
        if self._title is not None:
            self._title.append(data)
        if not self.skip:
            self.text.append(data)


# Names a robots directive is aimed at that Google obeys: every crawler, or Googlebot.
GOOGLE_AGENTS = ("robots", "googlebot")
# Directives written "name: value". Any other "word:" at the start of a line names a crawler.
# "none" is Google's short form of "noindex, nofollow".
NOINDEX = frozenset({"noindex", "none"})
VALUED_DIRECTIVES = frozenset({"max-snippet", "max-image-preview", "max-video-preview", "unavailable_after"})


def google_directives(lines: Iterable[str]) -> List[str]:
    """The X-Robots-Tag directives Google obeys. A part that starts with a crawler's name
    ("bingbot: noindex") switches the crawler for it and the parts after it, up to the
    next crawler's name or the end of the line. A line starts aimed at every crawler. A
    proxy may fold two header lines into one with a comma, which this reads the same way."""
    out: List[str] = []
    for line in lines:
        agent = None
        for part in (part.strip() for part in line.split(",")):
            head, colon, rest = part.partition(":")
            name = head.strip().lower()
            if colon and name not in VALUED_DIRECTIVES and " " not in name and name:
                agent, part = name, rest.strip()
            if part and (agent is None or agent in GOOGLE_AGENTS):
                out.append(part.lower())
    return out


def read_html(html: str, base_url: str, headers: Dict[str, str]) -> PageFacts:
    """Facts from the raw HTML only, no JavaScript. Visible text drops script, style, nav,
    footer, header and noscript."""
    r = _Reader()
    r.feed(html or "")
    r.close()
    blocks = []
    for raw in r.json_ld_raw:
        try:
            blocks.append(json.loads(raw))
        except ValueError:
            continue  # a block that fails to parse is dropped, not counted as invalid
    header_directives = google_directives((headers.get("x-robots-tag") or "").splitlines())
    def absolute(href: str) -> Optional[str]:
        try:
            return urldefrag(urljoin(base_url, href))[0]
        except ValueError:  # a malformed link in the page is not a link
            return None

    return PageFacts(words=len(" ".join(r.text).split()), noindex=bool(NOINDEX & set(r.robots + header_directives)),
                     canonical=absolute(r.canonicals[0]) if r.canonicals else None, json_ld=blocks,
                     links=[u for u in map(absolute, r.links) if u], title=r.title)


def is_challenge(resp: Response) -> bool:
    if "cf-mitigated" in resp.headers:
        return True
    m = re.search(r"<title[^>]*>(.*?)</title>", resp.text or "", re.I | re.S)
    if m and any(t in m.group(1).lower() for t in _CHALLENGE_TITLES):
        return True
    # A real page can load a challenge vendor's script, so a script name alone marks a
    # challenge only on a page with almost no text or a status other than 200.
    low = (resp.text or "").lower()
    if not any(fp in low for fp in _CHALLENGE_FINGERPRINTS):
        return False
    if resp.status != 200:
        return True
    try:
        return read_html(resp.text, "https://challenge.invalid/", {}).words < PARITY_MIN_WORDS
    except Exception:
        return True


def is_html(resp: Response) -> bool:
    kind = resp.headers.get("content-type", "")
    return not kind or "html" in kind.lower()


# ---------- JSON-LD rules ----------
def _items(blocks: List[Any]) -> List[Any]:
    return [i for b in blocks for i in (b if isinstance(b, list) else [b])]


def is_json_ld_valid(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    ctx = block.get("@context")
    if "schema.org" not in (" ".join(map(str, ctx)) if isinstance(ctx, list) else str(ctx or "")):
        return False
    graph = block.get("@graph")
    if not block.get("@type") and isinstance(graph, list):
        return bool(graph) and all(isinstance(n, dict) and n.get("@type") for n in graph)
    return bool(block.get("@type"))


def json_ld_valid(blocks: List[Any]) -> bool:
    items = _items(blocks)
    return bool(items) and all(is_json_ld_valid(i) for i in items)


def json_ld_types(blocks: List[Any]) -> List[str]:
    types: List[str] = []
    for item in _items(blocks):
        if not isinstance(item, dict):
            continue
        graph = item.get("@graph")
        for node in [item] + ([n for n in graph if isinstance(n, dict)] if isinstance(graph, list) else []):
            t = node.get("@type")
            types.extend(x for x in (t if isinstance(t, list) else [t]) if isinstance(x, str))
    return list(dict.fromkeys(types))


def org_json_ld(blocks: List[Any]) -> Optional[Dict[str, Any]]:
    """The first organisation entity in document order."""
    for data in blocks:
        items = data if isinstance(data, list) else (data.get("@graph") or [data]) if isinstance(data, dict) else []
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and isinstance(item.get("@type"), str) and item["@type"] in _ORG_TYPES:
                same_as = item.get("sameAs") or []
                return {"name": item.get("name") or None, "url": item.get("url") or None,
                        "sameAs": [same_as] if isinstance(same_as, str)
                        else [s for s in same_as if isinstance(s, str)] if isinstance(same_as, list) else []}
    return None


def _pagetype_hit(types: List[str]) -> bool:
    return any(t in _PAGETYPE_NAMED or t.endswith("Page") for t in types)


# ---------- indexability ----------
@dataclass
class IndexInput:
    final_status: int
    redirects: int
    noindex: bool
    googlebot_allowed: bool
    canonical: Optional[str]


def _query(query: str) -> str:
    """The query without tracking parameters: /sale?utm_source=chatgpt.com is the page /sale."""
    kept = [part for part in query.split("&") if part
            and not part.split("=", 1)[0].lower().startswith("utm_")
            and part.split("=", 1)[0].lower() not in TRACKING_PARAMS]
    return "&".join(kept)


def _canonical_key(url: str) -> str:
    """Scheme-, www-, trailing-slash-, fragment- and tracking-parameter-insensitive."""
    p = urlparse(urldefrag(url)[0])
    query = _query(p.query)
    return host_of(url) + (p.path.rstrip("/") or "") + (f"?{query}" if query else "")


def resolve_indexable(pages: Dict[str, IndexInput]) -> Dict[str, bool]:
    """200 after at most 3 redirects, no noindex, Googlebot allowed, and a canonical that
    is absent, self-referencing or leads to an indexable page in ``pages``. A canonical
    cycle, or a chain into one, is not indexable."""
    by_key = {_canonical_key(u): u for u in pages}
    result: Dict[str, bool] = {}
    for start in pages:
        chain: List[str] = []
        url = start
        while True:
            if url in result:
                outcome = result[url]
                break
            if url in chain:
                outcome = False
                break
            chain.append(url)
            p = pages[url]
            if p.final_status != 200 or p.redirects > MAX_REDIRECTS or p.noindex or not p.googlebot_allowed:
                outcome = False
                break
            target = by_key.get(_canonical_key(p.canonical)) if p.canonical else url
            if target is None:
                outcome = False
                break
            if target == url:
                outcome = True
                break
            url = target
        for u in chain:
            result[u] = outcome
    return result


# ---------- key pages ----------
def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}".lower()


def homepage(brand: Brand) -> str:
    return f"https://{brand.domain}/"


def _key(url: str, brand: Brand) -> str:
    """The brand's own host, with or without www, merges onto the homepage origin."""
    p = urlparse(urldefrag(url)[0])
    query = _query(p.query)
    path = (p.path or "/") + (f"?{query}" if query else "")
    if host_of(url) == brand.domain:
        return f"https://{brand.domain}{path}"
    return f"{p.scheme.lower()}://{p.netloc.lower()}{path}"


def key_pages(brand: Brand, answers: Iterable[Dict[str, Any]], serp: Iterable[Dict[str, Any]],
              limit: int = MAX_KEY_PAGES) -> List[str]:
    """Homepage first, then owned URLs cited in valid answers or ranking in the pulls,
    most frequent first, ties by URL. One count per row a URL appears in."""
    counts: Counter = Counter()
    rows = [r.get("cited_urls") or [] for r in answers if r.get("valid")]
    rows += [[x.get("url") for x in r.get("results") or []] for r in serp]
    for urls in rows:
        counts.update({_key(u, brand) for u in urls if isinstance(u, str) and brand.owns(u)})
    chosen = [homepage(brand)]
    for url, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if len(chosen) >= limit:
            break
        if url not in chosen:
            chosen.append(url)
    return chosen


# ---------- polite fetching ----------
class Refused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Fetched:
    final_url: str
    redirects: int
    resp: Response


class Crawler:
    """Every request of one run: robots.txt per origin, one request a second per host,
    retries on network failure, at most MAX_REDIRECTS redirects, each hop robots-checked."""

    def __init__(self, fetch: Fetch, interval: float = 1.0, attempts: int = 3,
                 sleep: Callable[[float], None] = time.sleep):
        self.fetch, self.interval, self.attempts, self.sleep = fetch, interval, attempts, sleep
        self.last: Dict[str, float] = {}
        self.robots: Dict[str, Optional[RobotsRules]] = {}  # None: robots.txt unavailable

    def _get(self, url: str) -> Response:
        host = host_of(url)  # www and the bare domain are one host for the one-a-second limit
        last: Optional[Exception] = None
        for attempt in range(1, self.attempts + 1):
            wait = self.last.get(host, -1e9) + self.interval - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            try:
                return self.fetch(url)
            except FetchError as exc:
                last = exc
                if attempt < self.attempts:
                    self.sleep(2 * attempt)
            finally:
                self.last[host] = time.monotonic()
        raise FetchError(f"{self.attempts} attempts failed for {url}, last: {last}")

    def _follow(self, url: str, check_robots: bool, stay: Optional[Callable[[str], bool]] = None) -> Fetched:
        """``stay`` says which URLs belong to the site being read. A redirect that leaves
        it (a queue page, a parked domain) is refused, not read as the brand's page."""
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            if check_robots:
                rules = self.rules(current)
                if rules is None:
                    raise Refused("robots_unavailable")
                if not robots_allows(rules, ROBOTS_TOKEN, current):
                    raise Refused("robots_disallowed" if hop == 0 else "redirect_robots_disallowed")
            resp = self._get(current)
            location = resp.headers.get("location")
            if resp.status not in REDIRECT_STATUS or not location:
                return Fetched(current, hop, resp)
            try:
                current = urldefrag(urljoin(current, location))[0]
            except ValueError:
                raise Refused("redirect_malformed")
            if stay is not None and not stay(current):
                raise Refused("redirect_off_site")
        raise Refused("too_many_redirects")

    def rules(self, url: str) -> Optional[RobotsRules]:
        """RFC 9309: 2xx is parsed, 4xx allows all, anything else leaves the origin
        unavailable. A network failure propagates as FetchError."""
        origin = _origin(url)
        if origin not in self.robots:
            self.robots[origin] = None
            resp = self._follow(origin + "/robots.txt", check_robots=False).resp
            if 200 <= resp.status < 300:
                self.robots[origin] = parse_robots(resp.text)
            elif 400 <= resp.status < 500 and resp.status != 429:
                # 429 means "ask later", not "no robots.txt": it leaves the origin unavailable.
                self.robots[origin] = parse_robots("")
        return self.robots[origin]

    def allows(self, token: str, url: str) -> bool:
        rules = self.rules(url)
        return rules is not None and robots_allows(rules, token, url)

    def page(self, url: str, stay: Optional[Callable[[str], bool]] = None) -> Fetched:
        return self._follow(url, check_robots=True, stay=stay)


# ---------- one brand ----------
def _page_row(url: str, status: Optional[int] = None, excluded: Optional[str] = None) -> Dict[str, Any]:
    return {"url": url, "status_code": status, "counted": excluded is None, "excluded_reason": excluded,
            "parity": None, "indexable": None, "json_ld_types": [], "json_ld_valid": None}


def _not_read(status: str, reason: str) -> Dict[str, Any]:
    return {"status": status, "reason": reason, "read_at": utc_now(), "robots_verdicts": {}, "pages": [],
            "summary": None, "site_seo": {"score": None, "source": None, "reason": f"site_{status}"}}


def read_brand(brand: Brand, urls: List[str], crawler: Crawler) -> Tuple[Dict[str, Any], Optional[str]]:
    """(the site read for one brand, its homepage HTML when read ok). ``urls[0]`` is the homepage."""
    entry, html = _read_brand(brand, urls, crawler)
    return entry, html


def read_homepage(brand: Brand, home: str, crawler: Crawler):
    """(Fetched, None) for a homepage that can be scored, else (None, the not-read entry)."""
    def own(url: str) -> bool:
        return brand.owns(url)

    try:
        if crawler.rules(home) is None:
            return None, _not_read("failed", "robots_unavailable")
    except (FetchError, Refused):
        return None, _not_read("failed", "robots_unavailable")
    if not crawler.allows(ROBOTS_TOKEN, home):
        return None, _not_read("blocked", "robots_disallows_homepage")
    try:
        first = crawler.page(home, stay=own)
    except FetchError:
        return None, _not_read("failed", "homepage_unreachable")
    except Refused as exc:
        return None, _not_read("blocked" if exc.reason.endswith("disallowed") else "failed", f"homepage_{exc.reason}")
    code = first.resp.status
    if code in RETRY_STATUS:
        return None, _not_read("failed", f"homepage_status_{code}")
    if code in BLOCK_STATUS:
        return None, _not_read("blocked", f"homepage_status_{code}")
    if is_challenge(first.resp):
        return None, _not_read("blocked", "homepage_challenge_page")
    if code != 200:
        # Only a 200 is the brand's homepage. Anything else (202 from a firewall, 404, 5xx) is not scored.
        return None, _not_read("failed", f"homepage_status_{code}")
    if not is_html(first.resp):
        return None, _not_read("failed", "homepage_not_html")
    return first, None


def _read_brand(brand: Brand, urls: List[str], crawler: Crawler) -> Tuple[Dict[str, Any], Optional[str]]:
    home = urls[0]
    first, not_read = read_homepage(brand, home, crawler)
    if first is None:
        return not_read, None

    def own(url: str) -> bool:
        return brand.owns(url)

    # The homepage may have redirected from the bare domain to www or back. Key pages on
    # that same host are asked for at the origin that answered, so each costs one request.
    live_origin = _origin(first.final_url)

    def at_live_origin(url: str) -> str:
        if host_of(url) != host_of(first.final_url):
            return url
        p = urlparse(url)
        return live_origin + (p.path or "/") + (f"?{p.query}" if p.query else "")

    rows: List[Dict[str, Any]] = []
    counted: List[Tuple[Dict[str, Any], Fetched, PageFacts]] = []
    seen_final: set = set()
    for url in urls:
        try:
            got = first if url == home else crawler.page(at_live_origin(url), stay=own)
        except Refused as exc:
            rows.append(_page_row(url, excluded=exc.reason))
            continue
        except FetchError:
            rows.append(_page_row(url, excluded="fetch_failed"))
            continue
        code = got.resp.status
        # A key page we may not read, or that refuses us, is left out of every share and
        # mean. It is not scored as parity 0 and not indexable, because a block says nothing
        # about the brand's readiness (METHODOLOGY.md, Appendix D).
        if code in BLOCK_STATUS:
            excluded = f"blocked_status_{code}"
        elif is_challenge(got.resp):
            excluded = "challenge_page"
        elif not is_html(got.resp):
            excluded = "not_html"
        elif _canonical_key(got.final_url) in seen_final:
            excluded = "same_page_as_earlier_key_page"
        else:
            excluded = None
        facts = None
        if excluded is None:
            try:
                facts = read_html(got.resp.text, got.final_url, got.resp.headers)
            except Exception:  # one page's odd markup leaves that page out, not the brand
                excluded = "unreadable_html"
        row = _page_row(url, code, excluded)
        rows.append(row)
        if facts is not None:
            seen_final.add(_canonical_key(got.final_url))
            counted.append((row, got, facts))

    index_inputs = {got.final_url: IndexInput(got.resp.status, got.redirects, facts.noindex,
                                              crawler.allows("Googlebot", got.final_url), facts.canonical)
                    for _, got, facts in counted}
    indexable = resolve_indexable(index_inputs)
    for row, got, facts in counted:
        row.update(parity=1 if facts.words >= PARITY_MIN_WORDS else 0, indexable=indexable[got.final_url],
                   json_ld_types=json_ld_types(facts.json_ld), json_ld_valid=json_ld_valid(facts.json_ld))

    verdicts = {agent: all(crawler.allows(agent, row["url"]) for row, _, _ in counted) for agent in VERDICT_AGENTS}
    records = [row for row, _, _ in counted]
    org = org_json_ld(counted[0][2].json_ld)
    socials = {link for _, _, facts in counted for link in facts.links
               if not brand.owns(link) and social_platform(link)}

    def share(rs, hit) -> float:
        return sum(1 for r in rs if hit(r)) / len(rs) if rs else 0.0

    summary = {
        "access": reference.answer_agent_access(verdicts),
        "indexable_share": share(records, lambda r: r["indexable"]),
        "org_with_name_url": bool(org and org["name"] and org["url"]),
        "org_same_as": bool(org and org["sameAs"]),
        "pagetype_share": share(records[1:], lambda r: _pagetype_hit(r["json_ld_types"])),
        "valid_share": share(records, lambda r: r["json_ld_valid"]),
        "social_urls": sorted(socials | set((org or {}).get("sameAs") or [])),
    }
    return {"status": "ok", "reason": None, "read_at": utc_now(), "robots_verdicts": verdicts,
            "pages": rows, "summary": summary, "site_seo": None}, first.resp.text


# ---------- the stage ----------
def add_arguments(p) -> None:
    p.add_argument("--go", action="store_true", help="fetch; without it the stage only lists the key pages")
    p.add_argument("--refresh", action="store_true", help="read again brands already read ok or blocked")


def main(args, fetch: Optional[Fetch] = None, interval: float = 1.0,
         sleep: Callable[[float], None] = time.sleep) -> int:
    if not args.go:
        return _main(args, fetch, interval, sleep)
    with store.exclusive(args.out):
        return _main(args, fetch, interval, sleep)


def _main(args, fetch: Optional[Fetch], interval: float, sleep: Callable[[float], None]) -> int:
    panel = panel_file.with_additions(panel_file.load(args.panel), args.out / "additions.yaml")
    out: Path = args.out
    answers, serp = store.read_jsonl(out / ANSWERS), store.read_jsonl(out / SERP)
    store.require_panel(answers, panel.sha256, out / ANSWERS)
    store.require_panel(serp, panel.sha256, out / SERP)
    inputs = store.input_hashes(out, [ANSWERS, SERP])

    path = out / SITES
    sites = store.read_json(path) if path.exists() else {"brands": {}}
    if sites.get("panel_sha256", panel.sha256) != panel.sha256:
        raise store.StoreError(f"{path} was written under another panel ({sites['panel_sha256']}). "
                               "Use a new output directory for a new or changed panel.")
    if sites.get("inputs") and sites["inputs"] != inputs:
        print(f"note: {ANSWERS} or {SERP} changed since sites.json was last written; brands read "
              "before took their key pages from the older files. Pass --refresh to read them again.",
              file=sys.stderr)
    sites.update(panel_id=panel.panel_id, panel_sha256=panel.sha256)

    with_seo = bool(os.environ.get(siteseo.KEY_ENV))
    plan = []
    for brand in panel.brands:
        prior = sites["brands"].get(brand.id, {})
        if prior.get("status") in ("ok", "blocked") and not args.refresh:
            # A brand read ok whose Site SEO call failed keeps its pages; only the score is tried again.
            seo_missing = prior["status"] == "ok" and with_seo and (prior.get("site_seo") or {}).get("score") is None
            action = "seo" if seo_missing else "skip"
        else:
            action = "read"
        plan.append((brand, key_pages(brand, answers, serp), action))

    if not with_seo:
        print(f"warning: {siteseo.KEY_ENV} is not set, so Site SEO will be not measured for every brand.",
              file=sys.stderr)
    for brand, urls, action in plan:
        state = {"read": f"{len(urls)} key pages", "seo": "already read, Site SEO to retry",
                 "skip": "already read, skipped"}[action]
        print(f"{brand.id}: {state}")
        if not args.go and action == "read":
            for url in urls:
                print(f"  {url}")
    if not args.go:
        print("dry run: nothing was fetched. Pass --go to read the sites.")
        return 0

    crawler = Crawler(fetch or http_fetch(), interval=interval, sleep=sleep)
    failed = 0
    for brand, urls, action in plan:
        if action == "skip":
            continue
        html: Optional[str] = None
        try:
            if action == "seo":
                # The pages are kept. Only the homepage is fetched again, for its HTML.
                entry = sites["brands"][brand.id]
                first, _ = read_homepage(brand, urls[0], crawler)
                html = first.resp.text if first is not None else None
            else:
                entry, html = read_brand(brand, urls, crawler)
        except Exception as exc:  # one site's odd HTML must not stop the brands after it
            entry = {**_not_read("failed", "reader_error"), "detail": f"{type(exc).__name__}: {exc}"}
        if entry["status"] == "ok":
            entry["site_seo"] = siteseo.measure(urls[0], html) if html is not None else \
                {"score": None, "source": None, "reason": "homepage_not_read_again"}
        failed += entry["status"] == "failed"
        sites["brands"][brand.id] = entry
        sites["inputs"] = inputs
        store.write_json(path, sites)
        print(f"{brand.id}: {entry['status']}" + (f" ({entry['reason']})" if entry["reason"] else ""))
    read_ok = {b: e for b, e in sites["brands"].items() if e["status"] == "ok"}
    # Site SEO is expected when the PageSpeed key is set, or when any brand has a score:
    # a brand without one would then be measured on fewer signals than the others.
    expected = with_seo or any(e["site_seo"]["score"] is not None for e in read_ok.values())
    no_seo = sorted(b for b, e in read_ok.items() if expected and e["site_seo"]["score"] is None)
    if failed:
        print(f"{failed} brands failed; rerun the same command to retry them.", file=sys.stderr)
    if no_seo:
        # Without this a brand would drop out of the ranking with nothing said.
        print(f"Site SEO is missing for {no_seo}: " + "; ".join(f"{b}: {sites['brands'][b]['site_seo']['reason']}"
              for b in no_seo) + ". Rerun the same command to try those again.", file=sys.stderr)
    return 1 if failed or no_seo else 0
