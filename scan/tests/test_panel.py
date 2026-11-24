import pytest
import yaml

from scan import panel as panel_file
from scan.tests.conftest import panel_dict


def test_frozen_panel_loads_with_its_calls(make_panel):
    panel = panel_file.load(make_panel(questions=3))
    assert (panel.market.country, panel.market.language) == ("US", "en")
    calls = panel.calls("2026-11-21")
    assert len(calls) == 3 * 4 * 2
    assert [run for _, _, run in calls[:12]] == [1] * 12
    assert panel.brands[3].domains == ("voltcity.example", "volt.example")


def test_one_changed_character_fails_the_hash(make_panel):
    path = make_panel(questions=3)
    path.write_text(path.read_text().replace("item 2 this", "item 2 thiz"))
    with pytest.raises(panel_file.PanelError, match="hash mismatch"):
        panel_file.load(path)


def test_freeze_is_stable_and_replaces_an_old_hash(make_panel):
    path = make_panel(questions=3)
    first = panel_file.load(path).sha256
    assert panel_file.freeze(path) == first
    assert path.read_text().count("sha256:") == 1


def test_a_day_outside_the_panel_is_refused(make_panel):
    with pytest.raises(panel_file.PanelError, match="not in the panel"):
        panel_file.load(make_panel(questions=3)).calls("2026-11-22")


@pytest.mark.parametrize("change, message", [
    ({"engines": {"bing": {}}}, "engines must be drawn"),
    ({"selection": {"mode": "random"}}, "selection.mode"),
    ({"runs_per_day": 0}, "runs_per_day"),
    ({"days": ["21 Nov"]}, "YYYY-MM-DD"),
    ({"brands": [{"id": "a", "name": "A", "domain": "a.example"}, {"id": "a", "name": "B", "domain": "b.example"}]},
     "duplicate brand ids"),
    ({"questions": [{"id": "q1", "text": "x"}, {"id": "q1", "text": "y"}]}, "duplicate question ids"),
    ({"questions": [{"id": "q1"}]}, "every question needs a 'text'"),
    ({"keywords": [{"volume": 10}]}, "every keyword needs a 'text'"),
    ({"market": {"language": "en"}}, "every market needs a 'country'"),
    ({"market": {"country": "US"}}, "every market needs a 'language'"),
    ({"engines": {"perplexity": {"model": "sonar"}}}, r"no adapter for \['perplexity'\]"),
    ({"brands": [{"id": "a", "name": "A", "domain": "a.example"},
                 {"id": "b", "name": "B", "domain": "b.example", "other_domains": ["a.example"]}]},
     "duplicate brand domains"),
])
def test_bad_panels_are_refused(change, message):
    with pytest.raises(panel_file.PanelError, match=message):
        panel_file.parse({**panel_dict(questions=2), **change}, verify=False)


def test_additions_add_a_confirmed_brand_and_an_alias_without_touching_the_hash(make_panel, tmp_path):
    panel = panel_file.load(make_panel(questions=3))
    additions = tmp_path / "additions.yaml"
    additions.write_text(yaml.safe_dump({
        "aliases": [{"brand_id": "voltcity", "alias": "VoltCity", "answer_id": "2026-11-21/claude/q01/r1"}],
        "brands": [{"id": "shopzone", "name": "ShopZone", "domain": "https://www.shopzone.example/",
                    "confirmed_by": "a person"}],
    }))
    merged = panel_file.with_additions(panel, additions)
    assert merged.sha256 == panel.sha256
    assert [b.id for b in merged.brands] == ["northmart", "target", "gap", "voltcity", "shopzone"]
    assert merged.brands[3].aliases == ("VoltCity",)
    assert (merged.brands[4].domain, merged.brands[4].source) == ("shopzone.example", "ai_named")


@pytest.mark.parametrize("second, message", [
    ({"id": "shopzone", "name": "ShopZone Two", "domain": "shopzone-two.example"}, "another added brand"),
    ({"id": "shopzone2", "name": "ShopZone Two", "domain": "www.shopzone.example"}, "another added brand"),
    ({"id": "shopzone2", "name": "ShopZone Two", "domain": "two.example", "other_domains": ["shopzone.example"]},
     "another added brand"),
    ({"id": "shopzone2", "name": "ShopZone Two", "domain": "two.example", "other_domains": ["northmart.example"]},
     "a listed brand"),
])
def test_an_added_brand_that_shares_an_id_or_any_domain_is_refused(make_panel, tmp_path, second, message):
    additions = tmp_path / "additions.yaml"
    additions.write_text(yaml.safe_dump({"brands": [
        {"id": "shopzone", "name": "ShopZone", "domain": "shopzone.example", "confirmed_by": "a person"},
        {**second, "confirmed_by": "a person"}]}))
    with pytest.raises(panel_file.PanelError, match=f"duplicates {message}"):
        panel_file.with_additions(panel_file.load(make_panel(questions=3)), additions)


def test_the_main_command_prints_a_clean_error_for_a_malformed_panel(tmp_path, capsys):
    from scan.__main__ import main
    data = panel_dict(questions=2)
    del data["questions"][0]["text"]
    path = tmp_path / "panel.yaml"
    path.write_text(yaml.safe_dump(data))
    assert main(["score", "--panel", str(path), "--out", str(tmp_path)]) == 2
    assert "error: every question needs a 'text'" in capsys.readouterr().err


def test_an_unconfirmed_added_brand_is_refused(make_panel, tmp_path):
    additions = tmp_path / "additions.yaml"
    additions.write_text(yaml.safe_dump({"brands": [{"id": "x", "name": "X", "domain": "x.example"}]}))
    with pytest.raises(panel_file.PanelError, match="confirmed_by"):
        panel_file.with_additions(panel_file.load(make_panel(questions=3)), additions)


def test_a_site_under_another_brands_domain_counts_for_its_own_brand_only():
    data = panel_dict()
    data["brands"] = [{"id": "gap", "name": "Gap", "domain": "gap.example"},
                      {"id": "oldnavy", "name": "Old Navy", "domain": "oldnavy.gap.example"}]
    gap, old_navy = panel_file.parse(data, verify=False).brands
    assert gap.not_domains == ("oldnavy.gap.example",) and old_navy.not_domains == ()
    assert gap.owns("https://www.gap.example/sale") and not gap.owns("https://oldnavy.gap.example/sale")
    assert old_navy.owns("https://oldnavy.gap.example/sale") and not old_navy.owns("https://gap.example/")
    assert gap.owns("https://corporate.gap.example/news")
