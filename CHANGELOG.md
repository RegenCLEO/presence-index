# Changelog

## 1.0.0 (24 November 2026)

The first public snapshot of this repository. It holds the code and the panel behind the [Black Friday retailers ranking for the United States](https://regencleo.ai/cleo-index/rankings/black-friday-retailers-us-2026-11-23/). The scoring maths and `config/cpi-1.0.0.yaml` are the method as first described in September 2026.

- Adds the `reviews`, `mentions` and `local` stages, which measure Reviews & Ratings from Trustpilot, Independent Mentions from five sources and Local Listings from Google Maps. Appendix D describes all ten signals of a public scan and the route each source is read by. A public scan score stays Provisional whatever its coverage.
- Publishes the Black Friday panel under `panels/`, with its SHA-256.
- Adds the `sensitivity` stage, which moves each signal weight 25 per cent down and up and rebuilds the ranks, as section 4 of the method promises. The Black Friday result is in `sensitivity/`. In 3 of its 20 tests a rank changes. With Organic Search at 75 per cent of its weight, Amazon drops from joint first to joint third.
- The method says how each source is read. ChatGPT answers, Google Maps listings, Trustpilot ratings, and Reddit and LinkedIn posts are bought from providers that collect them from public pages. The scan's README names each provider. Section 5 no longer says the index never uses scraped data.
- Scores and snapshots record the model each engine and reader used, and the version of the reader prompt.
- The index range is centred on the unrounded index. On the 23 November ranking this moves one end of six ranges by 0.1 (Walmart, Best Buy, Target, Costco, Home Depot, Old Navy). No rank changes.
- Public snapshots carry recommendation and citation rates unrounded, so a page rounds them once. Before, Best Buy's rate showed as 63.9% in the table and 64.0% in its detail.
- `X-Robots-Tag` lines aimed at a crawler other than Googlebot no longer make a page unindexable, and a repeated header keeps every line. A `googlebot` meta tag now counts, as `robots` did.
- Organic Search no longer needs five keywords in a pull. It needs the whole keyword panel in two calendar weeks, as Appendix D says.
- A panel that names Perplexity, or a malformed question, keyword or market, is refused with a clear error when it loads. Two added brands with one id or one domain are refused.
- The crawler's user agent points at https://regencleo.ai/crawler/. Its token, `CleoCrawler`, is unchanged.
- Dependencies are pinned to exact versions.
- Corrects the wording of references 2, 5, 8, 12, 13 and 14 and the sentences that cite them, after a check of each source. No figure used in scoring changes.
- Adds `NOTICE`, `SECURITY.md` and `CONTRIBUTING.md`, and a licence for published scan results and panel files.

### Also in 1.0.0, made before the first scan

- Adds Appendix D to the method: the public scan protocol PS-1.0, with its thresholds, its range and rank rules, and the signals it measures on less than the full method. No version 1.0 score changes.
- The reference calculator gains `effective_n`, `wilson_effective` and `signal_range`, the range of an engine-weighted rate. No score changes.
- Adds `scan/`, the code that runs the public scan.
- The scan can ask ChatGPT through a data provider's scraper of chatgpt.com, the route CLEO's product uses. No score changes.
- The scan asks Claude with the basic web search tool, because answers from the newer tool carry no citations. It also resolves the `google.com/goto` links in AI Overview citations to the pages they lead to. No score changes.
- A URL on a site that sits under another brand's domain counts for the brand whose site it is. No score changes.
- Adds `scan/PANEL.md`, the guide to writing a panel, and `python -m scan volumes`, which fills a draft panel's keywords from Google Ads search volumes. No score changes.
- The method gives the address for proposals, and Appendix A names the config file that holds the weights, curves and coverage thresholds. No score changes.
- Adds the `detail` stage, which writes `detail.public.json`: the panel's keywords with their volumes, and each signal's score and evidence for the rows of the public snapshot. No score changes.

### The method, first described in September 2026

- First public version of the CLEO Presence Index: ten signals across five surfaces and the website, weighted 70 for Presence Recognition and 30 for Presence Readiness.
- Scoring curves are judgement defaults. They will be calibrated against measured businesses in a later release, recorded here.
- Replaces CLEO's earlier owned and earned presence model. Scores from the earlier model are not comparable.
