# CLEO Presence Index

The CLEO Presence Index is a 0 to 100 score for how present a business is where buyers look: Maps, Search, AI Search, Social and Reviews, plus the website underneath. This repository holds the open methodology and the reference calculator for version 1.0.

- [METHODOLOGY.md](METHODOLOGY.md): the method in full, with the ten signals, their weights and the rules for missing data.
- [config/cpi-1.0.0.yaml](config/cpi-1.0.0.yaml): every weight, curve, engine weight and threshold in one file.
- [calculator/reference.py](calculator/reference.py): the reference implementation of the scoring maths.
- [calculator/tests/](calculator/tests/): tests that reproduce the worked examples in the method.
- [panels/](panels/): the question sets used for AI Search, published after each 28-day window closes.
- [sensitivity/](sensitivity/): the weight test of each public ranking, with every signal weight moved 25 per cent either way.
- [scan/](scan/): the code that runs the public scan for one category, under the protocol in Appendix D of the method.
- [CHANGELOG.md](CHANGELOG.md): version history.

The scoring code CLEO runs is built from the same reference implementation.

## Run the calculator

Python 3.10 or later. The calculator uses only the standard library. The tests need pytest and PyYAML.

```
pip install pytest pyyaml
pytest
```

Score a business from its signal scores:

```python
import sys
sys.path.insert(0, "calculator")
from reference import REGISTRY, Signal, LIVE, NOT_MEASURED, compute_index

scores = {"rec.ai_recommendation": 42, "rec.ai_citation": 55, "rec.organic_search": 60,
          "rec.reviews": 72, "rdy.crawl_access": 90, "rdy.site_seo": 78, "rdy.social": 65,
          "rdy.local_listings": 70, "rdy.structured_data": 80}
signals = [Signal(sid, layer, surface, weight, LIVE if sid in scores else NOT_MEASURED, scores.get(sid))
           for sid, (layer, surface, weight) in REGISTRY.items()]
print(compute_index(signals))
```

## Propose a change

Open an issue with the **Proposal** template. Every proposal gets a published response. Proposals for version 1.1 are open now. [CONTRIBUTING.md](CONTRIBUTING.md) says how code is published and why pull requests are not taken. Report a vulnerability as [SECURITY.md](SECURITY.md) says.

## Versioning

Versions follow MAJOR.MINOR.PATCH, as described in section 9 of the method. A change to weights, signals or curve shapes is a new version, recorded in the changelog with its effect on scores.

## Licence

- Methodology text (`METHODOLOGY.md`, `exhibits/`): [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- Code and configuration: [Apache 2.0](LICENSE).
- Panel files (`panels/`), weight tests (`sensitivity/`) and the files a ranking publishes (its snapshot and detail): [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Credit "CLEO Presence Index public scan" and link the ranking.

Copyright 2026 REGENAI CLEO. See [NOTICE](NOTICE).

The name "CLEO Presence Index" is reserved to CLEO. Others may describe a score as "based on the CLEO Presence Index methodology".

CLEO by RegenAI, [regencleo.ai](https://regencleo.ai/cleo-index/methodology)
