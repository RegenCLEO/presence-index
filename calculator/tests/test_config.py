from pathlib import Path

import yaml

import reference as r

CONFIG = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "cpi-1.0.0.yaml").read_text())


def test_signals_match_registry():
    assert {k: (v["layer"], v["surface"], v["weight"]) for k, v in CONFIG["signals"].items()} == r.REGISTRY


def test_weights_sum_to_100_with_layers_70_30():
    weights = CONFIG["signals"].values()
    assert sum(s["weight"] for s in weights) == 100
    assert sum(s["weight"] for s in weights if s["layer"] == "recognition") == 70


def test_curves_engines_and_agents_match():
    assert {k: (v["x50"], v["x90"]) for k, v in CONFIG["curves"].items()} == r.DEFAULT_CURVES
    assert CONFIG["engine_weights"] == r.ENGINE_WEIGHTS_V1
    assert abs(sum(CONFIG["engine_weights"].values()) - 1.0) < 1e-9
    assert {k: tuple(v) for k, v in CONFIG["answer_agents"].items()} == r.ANSWER_AGENTS


def test_ctr_tiers_bands_and_hysteresis_match():
    assert CONFIG["ctr_awr_unbranded_2026_07"] == r.CTR_AWR_UNBRANDED_2026_07
    assert CONFIG["mention_tiers"] == r.TIERS
    assert [(b["min"], b["name"]) for b in CONFIG["bands"]] == r.BANDS
    assert (CONFIG["grade_hysteresis"]["points"], CONFIG["grade_hysteresis"]["hold_days"]) == (r.HYSTERESIS, r.HOLD_DAYS)
