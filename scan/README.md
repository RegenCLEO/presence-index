# Public scan (PS-1.0)

Runs the CLEO Presence Index public scan for one category, under the protocol in [Appendix D of the method](../METHODOLOGY.md). It asks a frozen set of questions on AI engines, finds which brands the answers recommend, reads Google results and each brand's site, scores each brand and ranks them.

The scoring maths is `calculator/reference.py`. This package only gathers the inputs and calls it.

## Set up

```
python -m venv .venv
.venv/bin/pip install -r scan/requirements.txt pytest
.venv/bin/python -m pytest
```

Keys come from the environment. Each source names the variables it needs in the table under [Data routes](#data-routes). A stage whose key is missing stops before any call, or, for a mentions source, skips that source and says why.

## Build a panel

[PANEL.md](PANEL.md) is the guide to writing a panel: how the 60 questions and the keyword candidates are written, the prompts for an AI assistant that drafts them, and the rules a person checks before the freeze. One command belongs to that work and runs on a draft panel:

```
python -m scan volumes  --panel draft.yaml --out BUILD_DIR --candidates candidates.txt [--exclude-word WORD] [--go]
```

It looks up the monthly search volume of every candidate in one DataForSEO call and writes the 100 highest into the draft's `keywords:` block.

## Stages

Each stage is one command that reads files in the output directory and writes one. Keep the output directory outside this repository: the full snapshot holds brands that are never published.

```
python -m scan freeze   panel.yaml
python -m scan run      --panel panel.yaml --out DIR [--go]
python -m scan keywords --panel panel.yaml --out DIR --pull 2026-11-21 [--go]
python -m scan siteread --panel panel.yaml --out DIR [--go]
python -m scan parse    --panel panel.yaml --out DIR [--go] [--sample-csv check.csv]
python -m scan reviews  --panel panel.yaml --out DIR [--go]
python -m scan mentions --panel panel.yaml --out DIR [--go] [--skip-source NAME] [--publishers FILE]
python -m scan local    --panel panel.yaml --out DIR [--go]
python -m scan score    --panel panel.yaml --out DIR
python -m scan rank     --panel panel.yaml --out DIR
python -m scan detail   --panel panel.yaml --out DIR
python -m scan sensitivity --panel panel.yaml --out DIR
```

| Stage | Reads | Writes |
|---|---|---|
| `run` | panel | `answers.jsonl`: one row per question, engine and run, with the raw response |
| `keywords` | panel | `serp.jsonl`: Google's organic top ten per keyword and pull |
| `siteread` | `answers.jsonl`, `serp.jsonl` | `sites.json`: robots.txt, homepage and up to 20 key pages per brand |
| `parse` | `answers.jsonl` | `mentions.jsonl` (1, 0.5 or 0 per answer and brand), `reads.jsonl`, `candidates.json` |
| `reviews` | panel, `additions.yaml` | `reviews.json`: each brand's Trustpilot rating and review count |
| `mentions` | panel, `answers.jsonl`, `additions.yaml` | `mentions.json`: each brand's independent mentions with their tier, and the sources read. The vendor replies go to `mention_fetches.jsonl` and the reader model's verdicts to `mention_relevance.jsonl` |
| `local` | panel, `additions.yaml` | `local.json`: each brand's Google Maps listing |
| `score` | all of the above | `scores.json`, with the models each engine and reader used and the reader prompt version |
| `rank` | `scores.json` | `snapshot.full.json` (private), `snapshot.public.json` (top ten) |
| `detail` | `scores.json`, `snapshot.public.json` and the files the scores were made from | `detail.public.json`: the keywords with their volumes, and each signal's score and evidence for the rows of the public snapshot |
| `sensitivity` | `scores.json` | `sensitivity.full.json` (private) and `sensitivity.public.json`: the indexes and ranks with each signal weight moved 25 per cent down and up, one at a time. The public file names only the brands in the public snapshot |

Every stage that calls a vendor or a website does nothing without `--go`. It prints the calls it would make and stops. `cost_log.jsonl` gets one row per paid call.

A rerun of any stage picks up where the last one stopped. `answers.jsonl` is append-only: a stored answer is never asked again or edited. A failed call is not stored, so the rerun asks it again. A failed web search, a cut-off answer and an AI Overview that arrives empty are failed calls. A search that shows no AI Overview is a stored answer marked not valid.

Only one writing stage can use an output directory at a time. A second one stops with a message.

## Days and weeks

`run --day` must be one of the panel's days, and the stage refuses to ask on any other date (one day either side allows for time zones). `keywords --pull` must be made in the calendar week it is filed under, because Organic Search counts one observation per week. List the reserve scan day in the panel before freezing: days are part of the hash.

## Rehearsal

`python -m scan run --panel panel.yaml --out REHEARSAL_DIR --limit 3 --any-day --go` asks the first three questions only. Use a separate output directory, so rehearsal answers never sit under a scan day. A three-question scan cannot produce an Index: an engine counts from 60 valid answers on 10 questions.

## Known limits

- The stages first ran against live vendors for one category: `run`, `keywords`, `siteread` and `parse` on 21 November 2026, and `reviews`, `mentions` and `local` on 22 November. Expect faults the tests do not cover.
- Google serves organic results through `google.com/goto` links and stops redirecting them after a few requests. `keywords` takes the site from the result's breadcrumb, so a result whose breadcrumb names no site, such as a forum thread, has no position.
- Site SEO is scored on the homepage only: 60 points from the SEO category of Google's Lighthouse, read from PageSpeed, and 40 from the homepage HTML `siteread` already fetched. The method's full composition needs a crawl of the whole site. PageSpeed fetches the homepage itself from Google's side. A brand PageSpeed cannot score has no Site SEO and is not ranked. Rerun `siteread` to try it again.
- Stopping `run` or `parse` with Ctrl-C drops the calls not yet started. The few already in flight (two per engine by default) are paid for and not stored.
- A brand added through `additions.yaml` is read on its own, not in one prompt with the listed brands.
- A brand that no answer cites and no keyword ranks has only its homepage as a key page.
- A failed call that a vendor billed by tokens does not reach the cost log. DataForSEO's billed failures do.
- `prices.yaml` has no Google prices yet, so Gemini calls are logged as unpriced and the cost log counts them. It has no OpenAI prices either. The 23 November scan asked ChatGPT through DataForSEO, which returns its own cost.
- Perplexity has no adapter. A panel that names it is refused when it loads.
- Reviews read Trustpilot only and Local Listings Google Maps only. The listing's address and phone are not compared with the brand's site, so a brand with a listing has full name, address and phone consistency.

## Rules the code holds to

- **Settings.** Web search on, the model the panel names, no system prompt, no memory, a new session for each call, and the country from the panel. The language is sent to Google AI Overviews, Gemini and the chatgpt.com scraper. OpenAI's and Anthropic's APIs have no language field, so ChatGPT on OpenAI's API and Claude get the question alone.
- **ChatGPT has two routes.** `chatgpt: {model: ...}` asks OpenAI's API. `chatgpt: {adapter: chatgpt_scraper}` asks chatgpt.com through DataForSEO's scraper, and needs only the DataForSEO login. On that route the app picks the model and the answer row records it, and the scan cannot set the system prompt, the memory or the session. Only the sources an answer cites count as citations, not every page the model retrieved. A scan uses one route for all its ChatGPT answers.
- **Claude's search tool.** The adapter uses `web_search_20250305`, the tool CLEO's product uses. With `web_search_20260209` the model searches inside code execution and its answers arrive without citations, so Claude would never count for AI Citation. `web_search_tool` in the panel names another tool.
- **Google's redirect links.** DataForSEO returns many of an AI Overview's cited pages as `google.com/goto` links that name no site. The scan follows each link on Google's own hosts and reads the page's address from the redirect. It never requests the page. A link Google does not redirect stays as it arrived and matches no brand. Each answer row counts both kinds in `settings.redirects`.
- **A site under another brand's domain.** When two brands in a scan have domains where one sits under the other, a URL counts for the brand whose site it is. `oldnavy.gap.com` counts for Old Navy and not for Gap.
- **Thresholds.** An engine counts from 60 valid answers on 10 questions. An AI signal is live when the engines that count carry half the weight of the panel's engines.
- **Ten signals.** The core stages give seven: AI Recommendation, AI Citation, Organic Search, Crawl and Render, Site SEO, Social Profiles, Structured Data, 74% of the method. `reviews`, `mentions` and `local` give the other three. A scan that skips one of them records its signal as not measured for every brand. Every public scan score is Provisional and has no grade, whatever its coverage, because each signal reads fewer sources than the full method.
- **Organic Search.** Live from pulls of the whole keyword panel in two calendar weeks. No other minimum applies.
- **Comparable brands.** A signal counts for a scan when it is live for at least half the brands. A signal below that is out for every brand and lowers every brand's coverage alike. A brand missing a signal that counts (a blocked site) keeps its scores but is not ranked.
- **Ranking.** Brands whose ranges overlap share a rank ("1="). Run one and run two are ranked separately, and the snapshot says whether the top three held. An unranked brand whose recommendation rate is among the ten highest is named on a separate line with its rate.
- **Blocked sites.** The scan never works around a block. A blocked site's four site signals are not measured.
- **Brands the answers name.** `parse` lists them in `candidates.json`. A person confirms each one's domain in `DIR/additions.yaml`, then reruns `siteread`, `parse`, `score` and `rank`. The panel hash never changes.

```yaml
# DIR/additions.yaml
brands:
  - {id: shopzone, name: ShopZone, domain: shopzone.com, confirmed_by: "name of the person"}
aliases:
  - {brand_id: example-retailer, alias: "Example Co", answer_id: "2026-11-21/claude/q01/r1"}
```

## Data routes

The 23 November 2026 scan used these routes. No raw vendor response is kept in this repository: DataForSEO's terms treat exporting raw responses as redistribution.

| Source | Route | Official API or licensed data | Environment variables |
|---|---|---|---|
| ChatGPT | DataForSEO's scraper of chatgpt.com (`adapter: chatgpt_scraper`) | No. A third party queries the consumer app | `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD` |
| ChatGPT, other route | OpenAI's Responses API (`chatgpt: {model: ...}`) | Official API | `OPENAI_API_KEY` |
| Claude | Anthropic's Messages API | Official API | `ANTHROPIC_API_KEY` |
| Gemini | Google's Vertex AI | Official API | `GOOGLE_API_KEY` or `GEMINI_API_KEY`, or a service account in `VERTEX_AI_SA_JSON` or `VERTEX_AI_SA_PATH` with `VERTEX_AI_PROJECT_ID` |
| Google AI Overviews, Google organic results | DataForSEO's search results API | No. A third party collects Google's results pages | `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD` |
| Keyword volumes | DataForSEO's Google Ads search volume API | Google Ads data resold by DataForSEO | `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD` |
| Site SEO | Google's PageSpeed Insights API | Official API | `PAGESPEED_API_KEY` |
| Brand websites | Read by the scan itself as CleoCrawler, obeying robots.txt | Public pages | none |
| Trustpilot | DataForSEO's business data API | No. A third party collects Trustpilot's pages | `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD` |
| Google Maps | DataForSEO's search results API | No. A third party collects Google Maps results | `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD` |
| Bluesky | Bluesky's public search API | Official API | none |
| YouTube | YouTube Data API v3 | Official API | `YOUTUBE_API_KEY` |
| Reddit | An Apify actor that scrapes Reddit | No | `APIFY_TOKEN` |
| LinkedIn | An Apify actor that scrapes LinkedIn post search | No | `APIFY_TOKEN` |
| Pages the AI answers cite | Taken from the stored answers. No page is fetched | Not applicable | none |
| Answer and mention readers | The providers the panel names under `reader` | Official APIs | The provider's key above |

The `mentions` stage can also read Instagram, TikTok and Quora through Apify, and Reddit, Quora and Medium through Google site searches on DataForSEO. The 23 November scan left those out with `--skip-source`.
