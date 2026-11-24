from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from scan import panel as panel_file

BRANDS = [
    {"id": "northmart", "name": "Northmart", "domain": "northmart.example", "aliases": ["North Mart"]},
    {"id": "target", "name": "Target", "domain": "target.example", "ignore": ["target audience", "target price"]},
    {"id": "gap", "name": "Gap", "domain": "gap.example", "ignore": ["price gap"]},
    {"id": "voltcity", "name": "Volt City", "domain": "voltcity.example", "other_domains": ["volt.example"]},
]


def panel_dict(questions=30, runs=2, keywords=6, brands=4, missing_every=5, **overrides):
    chosen = BRANDS[:brands]
    fake = {"adapter": "fake", "brands": [{"name": b["name"], "domain": b["domain"]} for b in chosen]}
    data = {
        "panel_id": "test-retailers-us-en", "protocol": "PS-1.0", "category": "Test retailers",
        "market": {"country": "US", "language": "en"},
        "days": ["2026-11-21"], "runs_per_day": runs, "keyword_pulls": ["2026-11-21", "2026-11-23"],
        "selection": {"mode": "list_plus_ai", "ai_named_max": 5},
        "engines": {
            "chatgpt": {**fake, "seed": "c"},
            "google_ai": {**fake, "seed": "g", "missing_every": missing_every},
            "claude": {**fake, "seed": "a"},
            "gemini": {**fake, "seed": "m"},
        },
        "reader": {"first": {"provider": "fake"}, "second": {"provider": "fake"}, "tiebreak": {"provider": "fake"}},
        "questions": [{"id": f"q{i:02d}", "text": f"where should I buy item {i} this black friday",
                       "stratum": "general" if i <= 6 else "electronics"} for i in range(1, questions + 1)],
        "keywords": [{"text": f"black friday item {i} deals", "volume": 1000 * i} for i in range(1, keywords + 1)],
        "brands": chosen,
    }
    data.update(overrides)
    return data


@pytest.fixture
def make_panel(tmp_path):
    """Write a frozen panel file and return its path."""
    def make(**kwargs) -> Path:
        path = tmp_path / "panel.yaml"
        path.write_text(yaml.safe_dump(panel_dict(**kwargs), sort_keys=False), encoding="utf-8")
        panel_file.freeze(path)
        return path
    return make


@pytest.fixture
def stage_args(tmp_path):
    """Arguments for a stage's main(), as the command line would build them."""
    def make(panel_path: Path, **kwargs) -> Namespace:
        out = tmp_path / "out"
        out.mkdir(exist_ok=True)
        return Namespace(panel=panel_path, out=out, **kwargs)
    return make
