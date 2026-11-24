import pytest

from scan import siteseo
from scan.siteseo import OnPage, PageSpeedError

URL = "https://northmart.example/"
FULL = ('<html><head><title>Northmart</title><meta name="description" content="Deals on TVs">'
        '<link rel="canonical" href="https://northmart.example/"></head><body><h1>Black Friday</h1>'
        '<img src="a.png" alt="A TV"><img src="b.png" alt=""></body></html>')


def test_a_page_with_every_fact_has_40_on_page_points():
    assert siteseo.on_page(FULL) == OnPage(title=True, meta_description=True, canonical=True, h1=True,
                                           images=2, images_with_alt=2)
    assert siteseo.on_page(FULL).points == 40


def test_an_empty_page_keeps_only_the_points_for_having_no_image_without_alt():
    assert siteseo.on_page("").points == 8
    assert siteseo.on_page(None).points == 8


@pytest.mark.parametrize("html, fact, points", [
    ("<title>  </title>", "title", 8),
    ("<svg><title></title></svg><title>Later</title>", "title", 8),          # the first title is the one read
    ('<meta name="description" content="x"><meta name="Description" content="">', "meta_description", 8),
    ('<link rel="canonical" href="/a"><link rel="canonical" href="/b">', "canonical", 8),
    ('<link rel="Canonical" href="/a">', "canonical", 8),
    ('<link rel="canonical" href=" ">', "canonical", 8),
    ('<h1><img src="logo.png" alt="Northmart"></h1>', "h1", 8),             # an h1 with no text is no h1
    ('<h1><a href="/"><svg><defs><style>.a{fill:#111}</style></defs><path d="M0 0"/></svg></a></h1>', "h1", 8),
    ('<h1 id="t"><script>document.getElementById("t").textContent = pageTitle;</script></h1>', "h1", 8),
    ('<header><h1><a href="/"><img src="logo.png" alt="Northmart"></a></header><p>Deals on TVs</p>', "h1", 8),
])
def test_a_fact_that_does_not_hold_earns_nothing(html, fact, points):
    facts = siteseo.on_page(html)
    assert getattr(facts, fact) is False and facts.points == points


def test_two_canonical_links_to_the_same_url_count_as_one():
    html = '<link rel="canonical" href="/a"><link rel="alternate canonical" href="/a">'
    assert siteseo.on_page(html).canonical is True


@pytest.mark.parametrize("with_alt, total, points", [(0, 4, 0), (1, 4, 2), (3, 4, 6), (1, 16, 0), (3, 16, 2)])
def test_alt_points_are_the_share_of_images_with_an_alt_attribute(with_alt, total, points):
    html = "<img alt='x'>" * with_alt + "<img>" * (total - with_alt)
    assert siteseo.on_page(html).points == points


@pytest.mark.parametrize("lighthouse, on_page, expected", [
    (1.0, 40, 100), (0.92, 40, 95), (0.925, 32, 87), (0.0, 0, 0), (0.5, 8, 38), (0.775, 16, 63),
])
def test_composite_is_60_points_of_lighthouse_plus_the_on_page_points(lighthouse, on_page, expected):
    assert siteseo.composite(lighthouse, on_page) == expected


# ---------- PageSpeed ----------
def body(score):
    return {"lighthouseResult": {"categories": {"seo": {"score": score}}}}


class Replies:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, params, headers):
        self.calls.append((params, headers))
        got = self.replies.pop(0)
        if isinstance(got, Exception):
            raise got
        return got


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("PAGESPEED_API_KEY", "secret-key")


def test_pagespeed_asks_for_the_mobile_seo_category_with_the_key_in_a_header():
    get = Replies((200, body(0.92)))
    assert siteseo.pagespeed_seo(URL, get=get) == 0.92
    params, headers = get.calls[0]
    assert params == {"url": URL, "strategy": "mobile", "category": "seo"}
    assert headers == {"X-Goog-Api-Key": "secret-key"}


def test_pagespeed_waits_and_tries_again_when_busy():
    get, waits = Replies((429, None), ConnectionError("ReadTimeout"), (200, body(1))), []
    assert siteseo.pagespeed_seo(URL, get=get, sleep=waits.append) == 1.0
    assert waits == [3, 6]


def test_pagespeed_gives_up_after_three_busy_replies():
    get = Replies((503, None), (503, None), (503, None))
    with pytest.raises(PageSpeedError, match="HTTP 503 after 3 attempts"):
        siteseo.pagespeed_seo(URL, get=get, sleep=lambda s: None)
    assert get.replies == []


@pytest.mark.parametrize("reply, message", [
    ((400, {"error": {"message": "bad url"}}), "HTTP 400"),
    ((200, {"lighthouseResult": {"categories": {}}}), "no SEO category score"),
    ((200, body(None)), "no SEO category score"),
    ((200, None), "no SEO category score"),
])
def test_pagespeed_without_a_score_is_an_error_and_is_not_asked_again(reply, message):
    get = Replies(reply)
    with pytest.raises(PageSpeedError, match=message):
        siteseo.pagespeed_seo(URL, get=get, sleep=lambda s: None)
    assert len(get.calls) == 1


# ---------- the sites.json entry ----------
def test_measure_stores_the_score_and_both_parts():
    got = siteseo.measure(URL, FULL, lighthouse=lambda url: 0.92)
    assert got == {"score": 95, "source": siteseo.SOURCE, "reason": None, "lighthouse_seo": 0.92, "on_page_points": 40}


def test_without_the_lighthouse_score_there_is_no_score_from_the_html_alone():
    def unavailable(url):
        raise PageSpeedError("HTTP 429 after 3 attempts")
    got = siteseo.measure(URL, FULL, lighthouse=unavailable)
    assert (got["score"], got["reason"], got["detail"]) == (None, "pagespeed_unavailable", "HTTP 429 after 3 attempts")


def test_without_a_key_pagespeed_is_not_called(monkeypatch):
    monkeypatch.delenv("PAGESPEED_API_KEY")
    called = []
    got = siteseo.measure(URL, FULL, lighthouse=called.append)
    assert (got["score"], got["reason"], called) == (None, "pagespeed_key_missing", [])


def test_an_unexpected_failure_is_stored_and_does_not_stop_the_stage():
    got = siteseo.measure(URL, FULL, lighthouse=lambda url: 1 / 0)
    assert (got["score"], got["reason"]) == (None, "site_seo_failed")
