# Weight tests

`sensitivity/<category>/<market>/<language>/<ranking-date>.json` is the weight test of one public ranking, written by `python -m scan sensitivity`. Each signal weight is moved 25 per cent down and up, one at a time, and the indexes and ranks are rebuilt with the rules of Appendix D. The file names only the brands in the published rows. A brand outside them is counted, not named.

The file's `scores_sha256` is the SHA-256 of the `scores.json` it was made from, the same hash the ranking's snapshot names.

| Test | Ranking |
|---|---|
| [black-friday-retailers/us/en/2026-11-23.json](black-friday-retailers/us/en/2026-11-23.json) | [Black Friday retailers, United States, 23 November 2026](https://regencleo.ai/cleo-index/rankings/black-friday-retailers-us-2026-11-23/) |

On that ranking, 3 of the 20 tests change a rank and no index moves more than 2.5 points:

- Organic Search at 75 per cent of its weight: Amazon drops from joint first to joint third, level with Target. Walmart and Best Buy stay joint first.
- AI Recommendation at 125 per cent, or Independent Mentions at 75 per cent: one unpublished brand moves up into joint seventh, and IKEA drops from joint tenth to joint eleventh.
