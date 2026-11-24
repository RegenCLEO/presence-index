# Panels

The AI Search question sets, one file per category, market and language:

`panels/<category>/<market>/<language>/<window-end>.yaml`

Each set is frozen before a 28-day window starts and published here when the window closes. No windows have closed yet.

A public scan (Appendix D of the method) publishes its panel with its ranking, under the date of the ranking. The file's last line is its SHA-256, the hash the ranking names. It is the SHA-256 of the panel's content as canonical JSON, without the `sha256` field and without comments (`digest` in `scan/panel.py`). Loading the file with `scan.panel.load` checks it.

| Panel | Ranking |
|---|---|
| [black-friday-retailers/us/en/2026-11-23.yaml](black-friday-retailers/us/en/2026-11-23.yaml) | [Black Friday retailers, United States, 23 November 2026](https://regencleo.ai/cleo-index/rankings/black-friday-retailers-us-2026-11-23/) |

A scan in `ai_top` mode lists no brands in its panel. The brands come from the answers, and a person confirms each domain in the scan's `additions.yaml`. That file names brands the public ranking does not publish, so it stays with the scan's private outputs.
