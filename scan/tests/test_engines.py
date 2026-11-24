import importlib
import sys

import pytest

from scan import engines, llm
from scan.engines import EngineConfigError, chatgpt, chatgpt_scraper, claude, gemini, google_aio
from scan.records import EngineError, Market

US = Market(country="US", language="en")


def test_claude_cites_only_the_urls_attached_to_text():
    raw = {"turns": [{
        "stop_reason": "end_turn",
        "content": [
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "best tv deals"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
                {"type": "web_search_result", "url": "https://a.example/tv", "title": "A"},
                {"type": "web_search_result", "url": "https://b.example/tv", "title": "B"},
                {"type": "web_search_result", "url": "https://uncited.example/", "title": "C"},
            ]},
            {"type": "text", "text": "Try Northmart", "citations": [
                {"type": "web_search_result_location", "url": "https://a.example/tv", "title": "A",
                 "cited_text": "...", "encrypted_index": "x"}]},
            {"type": "text", "text": " or Volt City.", "citations": [
                {"type": "web_search_result_location", "url": "https://b.example/tv", "title": "B",
                 "cited_text": "...", "encrypted_index": "y"},
                {"type": "web_search_result_location", "url": "https://a.example/tv", "title": "A",
                 "cited_text": "...", "encrypted_index": "z"}]},
        ],
        "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5,
                  "server_tool_use": {"web_search_requests": 1, "web_fetch_requests": 0}},
    }]}
    assert claude.parse(raw) == ("Try Northmart or Volt City.", ["https://a.example/tv", "https://b.example/tv"])
    assert claude.tokens(raw["turns"]) == {"input_tokens": 100, "output_tokens": 20,
                                           "cache_read_input_tokens": 5, "web_search_requests": 1}


def test_claude_uses_the_basic_search_tool_unless_the_panel_names_another():
    assert claude.search_tool("claude-sonnet-5-5", US, {"max_searches": 3}) == {
        "type": "web_search_20250305", "name": "web_search", "max_uses": 3,
        "user_location": {"type": "approximate", "country": "US"}}
    assert claude.search_tool("claude-sonnet-5-5", US, {"web_search_tool": "web_search_20260209"})["type"] \
        == "web_search_20260209"


def test_openai_two_annotations_to_one_url_give_one_cited_url():
    raw = {"status": "completed", "output": [
        {"type": "web_search_call", "status": "completed", "action": {"type": "search", "query": "tv deals"}},
        {"type": "message", "role": "assistant", "content": [{
            "type": "output_text", "text": "Northmart has the best TV deals.",
            "annotations": [
                {"type": "url_citation", "url": "https://northmart.example/tv", "title": "TV", "start_index": 0, "end_index": 9},
                {"type": "url_citation", "url": "https://northmart.example/tv", "title": "TV", "start_index": 10, "end_index": 20},
            ]}]},
    ], "usage": {"input_tokens": 50, "output_tokens": 9}}
    assert chatgpt.parse(raw) == ("Northmart has the best TV deals.", ["https://northmart.example/tv"], False, 1)
    assert chatgpt.tokens(raw) == {"input_tokens": 50, "output_tokens": 9}


REDIRECT_A = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AAA"
REDIRECT_B = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/BBB"


def test_gemini_joins_text_parts_and_resolves_redirects():
    response = {
        "candidates": [{
            "content": {"role": "model", "parts": [
                {"text": "planning", "thought": True},
                {"text": "Northmart has TVs. "},
                {"text": "Volt City has laptops."},
            ]},
            "finish_reason": "STOP",
            "grounding_metadata": {"grounding_chunks": [
                {"web": {"uri": REDIRECT_A, "title": "northmart.example"}},
                {"web": {"uri": REDIRECT_B, "title": "voltcity.example", "domain": "voltcity.example"}},
            ]},
        }],
        "usage_metadata": {"prompt_token_count": 12, "candidates_token_count": 30, "thoughts_token_count": 8},
    }
    found = {REDIRECT_A: "https://www.northmart.example/tvs"}
    raw = gemini.build_raw(response, found.get)
    assert raw["resolved"] == {REDIRECT_A: "https://www.northmart.example/tvs", REDIRECT_B: None}
    assert gemini.parse(raw) == ("Northmart has TVs. Volt City has laptops.",
                                 ["https://www.northmart.example/tvs", "https://voltcity.example/"], False)
    assert gemini.tokens(response) == {"input_tokens": 12, "output_tokens": 38}


def test_gemini_with_no_candidates_is_blocked():
    assert gemini.parse({"response": {"prompt_feedback": {"block_reason": "SAFETY"}}, "resolved": {}}) == ("", [], True)


def dfs(task):
    return {"status_code": 20000, "status_message": "Ok.", "tasks": [task]}


def test_google_aio_reads_the_ai_overview():
    raw = dfs({"status_code": 20000, "cost": 0.0035, "result": [{"items": [
        {"type": "organic", "url": "https://organic.example/"},
        {"type": "ai_overview", "items": [
            {"type": "ai_overview_element", "text": "Northmart and Gap have sales.", "references": [
                {"type": "ai_overview_reference", "url": "https://northmart.example/a", "domain": "northmart.example"},
                {"type": "ai_overview_reference", "url": "https://northmart.example/b", "domain": "northmart.example"},
            ]},
            {"type": "ai_overview_element", "text": "Volt City too.", "references": [
                {"type": "ai_overview_reference", "url": "https://northmart.example/a", "domain": "northmart.example"},
            ]},
        ], "references": [{"type": "ai_overview_reference", "url": "https://gap.example/", "domain": "gap.example"}]},
    ]}]})
    assert google_aio.parse(raw) == (
        "Northmart and Gap have sales.\n\nVolt City too.",
        ["https://northmart.example/a", "https://northmart.example/b", "https://gap.example/"], 0.0035, True)


def test_google_aio_without_an_overview_is_a_final_invalid_answer(monkeypatch):
    raw = dfs({"status_code": 20000, "cost": 0.002, "result": [{"items": [
        {"type": "organic", "url": "https://organic.example/"}]}]})
    assert google_aio.parse(raw) == ("", [], 0.002, False)

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return raw

    sent = {}

    def post(url, json, auth, timeout):
        sent.update(url=url, json=json)
        return Response()

    httpx = type(sys)("httpx")
    httpx.post, httpx.HTTPError = post, Exception
    monkeypatch.setitem(sys.modules, "httpx", httpx)
    monkeypatch.setenv("DATAFORSEO_LOGIN", "login")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "password")
    result = google_aio.ask("where to buy a tv", Market("AE", "ar"), {})
    assert (result.valid, result.invalid_reason, result.cost_usd, result.text) == (False, "no_ai_overview", 0.002, "")
    assert sent["json"] == [{"keyword": "where to buy a tv", "location_code": 2784, "language_code": "ar",
                             "device": "desktop", "load_async_ai_overview": True}]


GOTO = "https://google.com/goto?url=CAESabc"


def test_google_redirect_citations_become_the_pages_they_lead_to(monkeypatch):
    monkeypatch.setattr(google_aio.time, "sleep", lambda seconds: None)
    hops = {GOTO: (301, "https://www.google.com/goto?url=CAESabc"),
            "https://www.google.com/goto?url=CAESabc": (302, "https://northmart.example/black-friday"),
            "https://google.com/goto?url=same": (302, "https://gap.example/"),
            "https://google.com/goto?url=refused": (200, "")}
    asked = []

    def get(url):
        asked.append(url)
        return hops[url]

    urls, counts = google_aio.resolve_redirects(
        ["https://gap.example/", GOTO, "https://google.com/goto?url=same", "https://google.com/goto?url=refused"], get)
    assert urls == ["https://gap.example/", "https://northmart.example/black-friday",
                    "https://google.com/goto?url=refused"]
    assert counts == {"found": 3, "resolved": 2}
    assert all(url.split("/")[2] in ("google.com", "www.google.com") for url in asked)


def test_a_refused_google_redirect_is_tried_once_more_and_a_failed_request_keeps_the_link(monkeypatch):
    monkeypatch.setattr(google_aio.time, "sleep", lambda seconds: None)
    replies = [(429, ""), (302, "https://northmart.example/")]
    assert google_aio.resolve_redirects([GOTO], lambda url: replies.pop(0)) == (
        ["https://northmart.example/"], {"found": 1, "resolved": 1})

    def broken(url):
        raise EngineError("google: timed out")
    assert google_aio.resolve_redirects([GOTO], broken) == ([GOTO], {"found": 1, "resolved": 0})


def test_citations_without_redirects_make_no_request():
    assert google_aio.resolve_redirects(["https://gap.example/"]) == (["https://gap.example/"], {"found": 0, "resolved": 0})


def test_google_aio_task_error_raises():
    with pytest.raises(EngineError, match="40101"):
        google_aio.parse(dfs({"status_code": 40101, "status_message": "Internal SE Server Error", "result": None}))


SCRAPED = {"status_code": 20000, "cost": 0.004, "result": [{
    "model": "gpt-5", "markdown": "Northmart has the best TV deals.",
    "sources": [{"type": "chat_gpt_source", "url": "https://northmart.example/tv"},
                {"type": "chat_gpt_source", "url": "https://reviews.example/tvs"}],
    "search_results": [{"type": "chatgpt_search_result", "url": "https://retrieved-not-cited.example/"}],
    "items": [{"type": "chat_gpt_text", "markdown": "Northmart has the best TV deals.",
               "sources": [{"url": "https://northmart.example/tv"}, {"url": "https://voltcity.example/"}]}],
}]}


def fake_httpx(monkeypatch, raw):
    """Stand in for httpx so no test reaches DataForSEO. Returns what the adapter sent."""
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return raw

    sent = {}

    def post(url, json, auth, timeout):
        sent.update(url=url, json=json)
        return Response()

    httpx = type(sys)("httpx")
    httpx.post, httpx.HTTPError = post, Exception
    monkeypatch.setitem(sys.modules, "httpx", httpx)
    monkeypatch.setenv("DATAFORSEO_LOGIN", "login")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "password")
    return sent


def test_chatgpt_scraper_cites_the_sources_and_not_every_page_retrieved():
    assert chatgpt_scraper.parse(dfs(SCRAPED)) == (
        "Northmart has the best TV deals.",
        ["https://northmart.example/tv", "https://reviews.example/tvs", "https://voltcity.example/"], 0.004, "gpt-5")


def test_chatgpt_scraper_joins_the_items_when_the_answer_has_no_top_level_text():
    raw = dfs({"status_code": 20000, "cost": 0.004, "result": [{"markdown": None, "items": [
        {"type": "chat_gpt_text", "markdown": "First part."}, {"type": "chat_gpt_images", "markdown": ""},
        {"type": "chat_gpt_table", "markdown": "Second part."}]}]})
    assert chatgpt_scraper.parse(raw) == ("First part.\n\nSecond part.", [], 0.004, None)


def test_chatgpt_scraper_asks_with_web_search_on_and_records_the_model_the_app_used(monkeypatch):
    sent = fake_httpx(monkeypatch, dfs(SCRAPED))
    result = chatgpt_scraper.ask("is 50% off + free shipping a good tv deal", Market("AE", "ar"), {})
    assert sent["url"] == chatgpt_scraper.ENDPOINT
    assert sent["json"] == [{"keyword": "is 50%25 off %2B free shipping a good tv deal", "location_code": 2784,
                             "language_code": "ar", "force_web_search": True}]
    assert (result.valid, result.model, result.cost_usd) == (True, "gpt-5", 0.004)
    assert result.settings["route"] == "dataforseo_chatgpt_scraper"
    assert result.cited_urls[0] == "https://northmart.example/tv"


def test_chatgpt_scraper_with_no_answer_text_is_a_billed_failure_to_ask_again(monkeypatch):
    fake_httpx(monkeypatch, dfs({"status_code": 20000, "cost": 0.004, "result": [{"markdown": "", "items": []}]}))
    with pytest.raises(EngineError, match="no answer text") as caught:
        chatgpt_scraper.ask("where to buy a tv", US, {})
    assert caught.value.cost_usd == 0.004


def test_chatgpt_scraper_task_error_raises():
    with pytest.raises(EngineError, match="40501"):
        chatgpt_scraper.parse(dfs({"status_code": 40501, "status_message": "Invalid Field", "result": None}))


def test_chatgpt_scraper_refuses_a_question_over_the_endpoint_limit():
    with pytest.raises(EngineConfigError, match="2000"):
        chatgpt_scraper.keyword("x" * 2001)


def test_the_panel_selects_the_scraper_by_adapter_and_names_no_model(monkeypatch):
    monkeypatch.setenv("DATAFORSEO_LOGIN", "login")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "password")
    config = {"adapter": "chatgpt_scraper"}
    engines.check("chatgpt", config)
    assert engines.adapter("chatgpt", config) is chatgpt_scraper
    assert engines.vendor("chatgpt", config) == "dataforseo"


@pytest.mark.parametrize("engine,config,variable", [
    ("chatgpt", {"model": "m"}, "OPENAI_API_KEY"),
    ("chatgpt", {"adapter": "chatgpt_scraper"}, "DATAFORSEO_LOGIN"),
    ("claude", {"model": "m"}, "ANTHROPIC_API_KEY"),
    ("google_ai", {}, "DATAFORSEO_LOGIN"),
    ("gemini", {"model": "m", "route": "developer"}, "GEMINI_API_KEY"),
])
def test_check_fails_without_the_credential(monkeypatch, engine, config, variable):
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DATAFORSEO_LOGIN",
                 "DATAFORSEO_PASSWORD", "GEMINI_API_KEY", "GOOGLE_API_KEY", "VERTEX_AI_SA_JSON", "VERTEX_AI_SA_PATH"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(EngineConfigError, match=variable):
        engines.check(engine, config)


def test_gemini_vertex_check_fails_without_any_credential(monkeypatch):
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "VERTEX_AI_SA_JSON", "VERTEX_AI_SA_PATH"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(EngineConfigError, match="vertex route"):
        engines.check("gemini", {"model": "m"})


@pytest.mark.parametrize("engine", ["chatgpt", "claude", "gemini"])
def test_the_panel_must_name_the_model(engine):
    with pytest.raises(EngineConfigError, match="model"):
        engines.check(engine, {})


def test_modules_import_without_vendor_packages(monkeypatch):
    for name in ("anthropic", "openai", "google", "google.genai", "google.oauth2", "httpx"):
        monkeypatch.setitem(sys.modules, name, None)
    for name in [n for n in sys.modules if n.startswith("scan.engines.") or n == "scan.llm"]:
        monkeypatch.delitem(sys.modules, name)
    for name in ("scan.engines.chatgpt", "scan.engines.chatgpt_scraper", "scan.engines.claude",
                 "scan.engines.gemini", "scan.engines.google_aio", "scan.llm"):
        assert importlib.import_module(name).__name__ == name


def test_reader_reply_parses_the_first_json_object():
    assert llm.first_json_object('Here it is: {"a": [1, {"b": 2}]} and {"c": 3}') == {"a": [1, {"b": 2}]}
    with pytest.raises(EngineError):
        llm.first_json_object("[1, 2] no object here")
