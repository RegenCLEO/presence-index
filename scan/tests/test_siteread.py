import json

import pytest

from scan import panel as panel_file, siteread, siteseo, store
from scan.siteread import FetchError, Response

HOME = "https://northmart.example/"
ROBOTS = "https://northmart.example/robots.txt"
HTML = {"content-type": "text/html; charset=utf-8"}


def html(words=60, head="", body_extra=""):
    text = " ".join(f"word{i}" for i in range(words))
    return (f"<html><head><title>Northmart</title>{head}</head><body>"
            f"<nav>{'menu ' * 80}</nav><main><p>{text}</p></main>{body_extra}"
            f"<footer>{'legal ' * 80}</footer></body></html>")


def ok(body, headers=None):
    return Response(200, {**HTML, **(headers or {})}, body)


class FakeFetch:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, url):
        self.calls.append(url)
        got = self.pages.get(url, Response(404, HTML, "<html>not found</html>"))
        if isinstance(got, Exception):
            raise got
        return got


@pytest.fixture
def scan(make_panel, stage_args, monkeypatch):
    """A one-brand panel, answers citing ``cited`` owned URLs, and a runner."""
    monkeypatch.delenv("PAGESPEED_API_KEY", raising=False)

    def make(pages, cited=(), refresh=False):
        path = make_panel(brands=1)
        args = stage_args(path, go=True, refresh=refresh)
        sha = panel_file.load(path).sha256
        for i, url in enumerate(cited):
            store.append_jsonl(args.out / "answers.jsonl", {"id": f"a{i}", "panel_sha256": sha, "valid": True,
                                                            "cited_urls": [url]})
        fetch = FakeFetch(pages)

        def run():
            code = siteread.main(args, fetch=fetch, interval=0, sleep=lambda s: None)
            return code, store.read_json(args.out / "sites.json")["brands"]["northmart"]
        return run, fetch, args
    return make


def test_disallowing_oai_searchbot_sets_its_verdict_false_and_access_to_79_5(scan):
    run, _, _ = scan({ROBOTS: Response(200, {}, "User-agent: OAI-SearchBot\nDisallow: /\n"), HOME: ok(html())})
    code, brand = run()
    assert code == 0
    assert brand["status"] == "ok"
    assert brand["robots_verdicts"] == {"OAI-SearchBot": False, "ChatGPT-User": True, "Googlebot": True,
                                        "Claude-SearchBot": True, "Claude-User": True, "PerplexityBot": True,
                                        "Perplexity-User": True, "Bingbot": True}
    assert brand["summary"]["access"] == pytest.approx(79.5)


def test_a_homepage_403_is_blocked_and_no_deep_page_is_asked_for(scan):
    run, fetch, _ = scan({ROBOTS: Response(404, {}, ""), HOME: Response(403, HTML, "denied")},
                         cited=["https://northmart.example/tv"])
    code, brand = run()
    assert code == 0
    assert (brand["status"], brand["reason"], brand["pages"]) == ("blocked", "homepage_status_403", [])
    assert brand["site_seo"]["score"] is None
    assert fetch.calls == [ROBOTS, HOME]


def test_a_challenge_homepage_is_blocked(scan):
    challenge = ok("<html><head><title>Just a moment...</title></head><body></body></html>")
    run, _, _ = scan({ROBOTS: Response(404, {}, ""), HOME: challenge})
    assert run()[1]["reason"] == "homepage_challenge_page"


def test_a_deep_page_403_is_not_counted_and_lowers_no_share(scan):
    pages = {ROBOTS: Response(404, {}, ""), HOME: ok(html()),
             "https://northmart.example/tv": ok(html()),
             "https://northmart.example/locked": Response(403, HTML, "denied")}
    run, _, _ = scan(pages, cited=["https://northmart.example/tv", "https://northmart.example/locked"])
    _, brand = run()
    assert [(p["url"], p["counted"], p["excluded_reason"], p["parity"], p["indexable"]) for p in brand["pages"]] == [
        (HOME, True, None, 1, True),
        ("https://northmart.example/locked", False, "blocked_status_403", None, None),
        ("https://northmart.example/tv", True, None, 1, True),
    ]
    assert brand["summary"]["indexable_share"] == 1.0


def test_a_page_under_50_words_has_parity_0(scan):
    # html() adds one title word, which counts as text.
    pages = {ROBOTS: Response(404, {}, ""), HOME: ok(html(words=49)),
             "https://northmart.example/thin": ok(html(words=48))}
    run, _, _ = scan(pages, cited=["https://northmart.example/thin"])
    assert [p["parity"] for p in run()[1]["pages"]] == [1, 0]


def test_noindex_in_meta_or_header_makes_a_page_not_indexable(scan):
    pages = {ROBOTS: Response(404, {}, ""), HOME: ok(html()),
             "https://northmart.example/meta": ok(html(head='<meta name="robots" content="noindex, follow">')),
             "https://northmart.example/header": ok(html(), {"x-robots-tag": "googlebot: noindex"})}
    run, _, _ = scan(pages, cited=["https://northmart.example/meta", "https://northmart.example/header"])
    _, brand = run()
    assert [(p["url"], p["indexable"]) for p in brand["pages"]] == [
        (HOME, True), ("https://northmart.example/header", False), ("https://northmart.example/meta", False)]
    assert brand["summary"]["indexable_share"] == pytest.approx(1 / 3)


@pytest.mark.parametrize("header, noindex", [
    ("bingbot: noindex", False),                        # aimed at another crawler
    ("noindex", True),                                  # aimed at every crawler
    ("googlebot: noindex, nofollow", True),
    ("max-snippet: 20, noindex", True),                 # a directive with a value, not a crawler's name
    ("unavailable_after: 25 Jun 2030 15:00:00 PST", False),
    ("googlebot: nofollow\nbingbot: noindex", False),  # two lines, each for its own crawler
    ("bingbot: nofollow\nnoindex", True),              # the second line is for every crawler
    ("bingbot: nofollow, googlebot: noindex", True),    # two lines a proxy folded into one
    ("unavailable_after: 2030-12-01, googlebot: noindex", True),
    ("googlebot: noindex, bingbot: nofollow", True),
    ("bingbot: noindex, googlebot: nofollow", False),
    ("none", True),                                     # Google's short form of noindex, nofollow
    ("googlebot: none", True),
])
def test_x_robots_tag_counts_only_lines_for_google_or_for_every_crawler(header, noindex):
    facts = siteread.read_html(html(), HOME, {"x-robots-tag": header})
    assert facts.noindex is noindex


def test_meta_robots_for_bing_does_not_make_a_page_unindexable_for_google():
    assert siteread.read_html(html(head='<meta name="bingbot" content="noindex">'), HOME, {}).noindex is False
    assert siteread.read_html(html(head='<meta name="googlebot" content="noindex">'), HOME, {}).noindex is True
    assert siteread.read_html(html(head='<meta name="robots" content="none">'), HOME, {}).noindex is True


def test_the_fetcher_keeps_every_line_of_a_repeated_header(monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(200, headers=[("X-Robots-Tag", "googlebot: nofollow"), ("X-Robots-Tag", "bingbot: noindex")],
                              text="<html></html>")
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    resp = siteread.http_fetch()(HOME)
    assert resp.headers["x-robots-tag"].splitlines() == ["googlebot: nofollow", "bingbot: noindex"]


def test_organization_json_ld_with_name_url_and_same_as_sets_both_flags(scan):
    org = {"@context": "https://schema.org", "@type": "Organization", "name": "Northmart",
           "url": "https://northmart.example/", "sameAs": ["https://www.instagram.com/northmart"]}
    product = {"@context": "https://schema.org", "@type": "Product", "name": "TV"}
    pages = {ROBOTS: Response(404, {}, ""),
             HOME: ok(html(head=f'<script type="application/ld+json">{json.dumps(org)}</script>',
                           body_extra='<a href="https://www.linkedin.com/company/northmart">in</a>')),
             "https://northmart.example/tv": ok(html(
                 head=f'<script type="application/ld+json">{json.dumps(product)}</script>'))}
    run, _, _ = scan(pages, cited=["https://northmart.example/tv"])
    _, brand = run()
    assert brand["pages"][0]["json_ld_types"] == ["Organization"]
    assert brand["summary"] == {
        "access": pytest.approx(100.0), "indexable_share": 1.0, "org_with_name_url": True, "org_same_as": True,
        "pagetype_share": 1.0, "valid_share": 1.0,
        "social_urls": ["https://www.instagram.com/northmart", "https://www.linkedin.com/company/northmart"]}


def test_key_pages_are_capped_at_20_with_the_homepage_first():
    brand = panel_file.Brand(id="northmart", name="Northmart", domain="northmart.example")
    answers = [{"valid": True, "cited_urls": [f"https://www.northmart.example/p{n:02d}#top"]}
               for n in range(30) for _ in range(1 + (n % 3))]
    answers.append({"valid": False, "cited_urls": ["https://northmart.example/invalid"] * 1})
    answers.append({"valid": True, "cited_urls": ["https://elsewhere.example/p00", HOME]})
    serp = [{"results": [{"position": 1, "url": "https://deals.northmart.example/a"}]}] * 5
    got = siteread.key_pages(brand, answers, serp)
    assert got == [HOME, "https://deals.northmart.example/a",
                   *(f"https://northmart.example/p{n:02d}" for n in (2, 5, 8, 11, 14, 17, 20, 23, 26, 29)),
                   *(f"https://northmart.example/p{n:02d}" for n in (1, 4, 7, 10, 13, 16, 19, 22))]
    assert len(got) == 20


def test_a_path_robots_disallows_for_our_agent_is_never_fetched(scan):
    robots = "User-agent: *\nAllow: /\n\nUser-agent: CleoCrawler\nDisallow: /private\n"
    pages = {ROBOTS: Response(200, {}, robots), HOME: ok(html()),
             "https://northmart.example/private/x": ok(html())}
    run, fetch, _ = scan(pages, cited=["https://northmart.example/private/x"])
    _, brand = run()
    assert "https://northmart.example/private/x" not in fetch.calls
    assert brand["pages"][1] == {"url": "https://northmart.example/private/x", "status_code": None,
                                 "counted": False, "excluded_reason": "robots_disallowed", "parity": None,
                                 "indexable": None, "json_ld_types": [], "json_ld_valid": None}


def test_robots_disallowing_our_agent_on_the_homepage_blocks_the_brand(scan):
    run, fetch, _ = scan({ROBOTS: Response(200, {}, "User-agent: CleoCrawler\nDisallow: /\n"), HOME: ok(html())})
    _, brand = run()
    assert (brand["status"], brand["reason"]) == ("blocked", "robots_disallows_homepage")
    assert fetch.calls == [ROBOTS]


def test_an_unreachable_homepage_fails_and_is_retried_on_the_next_run(scan):
    pages = {ROBOTS: Response(404, {}, ""), HOME: FetchError("timeout")}
    run, fetch, _ = scan(pages)
    code, brand = run()
    assert (code, brand["status"], brand["reason"]) == (1, "failed", "homepage_unreachable")
    assert fetch.calls == [ROBOTS, HOME, HOME, HOME]
    pages[HOME] = ok(html())
    code, brand = run()
    assert (code, brand["status"]) == (0, "ok")


def test_a_second_run_skips_a_brand_already_read(scan):
    run, fetch, _ = scan({ROBOTS: Response(404, {}, ""), HOME: ok(html())})
    first = run()[1]
    fetch.calls.clear()
    code, again = run()
    assert (code, fetch.calls, again) == (0, [], first)


def test_more_than_three_redirects_is_not_followed(scan):
    pages = {ROBOTS: Response(404, {}, ""), HOME: ok(html()),
             **{f"https://northmart.example/r{n}": Response(301, {"location": f"/r{n + 1}"}, "") for n in range(5)}}
    run, fetch, _ = scan(pages, cited=["https://northmart.example/r0"])
    _, brand = run()
    assert brand["pages"][1]["excluded_reason"] == "too_many_redirects"
    assert "https://northmart.example/r4" not in fetch.calls


def test_without_a_pagespeed_key_site_seo_is_not_measured(scan, capsys):
    run, _, _ = scan({ROBOTS: Response(404, {}, ""), HOME: ok(html())})
    _, brand = run()
    assert brand["site_seo"] == {"score": None, "source": None, "reason": "pagespeed_key_missing"}
    assert "Site SEO will be not measured for every brand" in capsys.readouterr().err


def test_site_seo_is_scored_on_the_homepage_html_this_stage_fetched(scan, monkeypatch):
    monkeypatch.setenv("PAGESPEED_API_KEY", "test-key")
    asked, measure = [], siteseo.measure
    monkeypatch.setattr(siteseo, "measure",
                        lambda url, page: measure(url, page, lighthouse=lambda u: asked.append(u) or 0.9))
    home = html(head='<meta name="description" content="Deals">', body_extra="<img src='a.png'><img src='b.png' alt=''>")
    run, fetch, _ = scan({ROBOTS: Response(404, {}, ""), HOME: ok(home)})
    code, brand = run()
    # 54 from Lighthouse at 0.9, 8 each for the title and the description, 4 for one image of two with alt.
    assert (code, brand["site_seo"]["score"], brand["site_seo"]["on_page_points"]) == (0, 74, 20)
    assert asked == [HOME] and fetch.calls.count(HOME) == 1
