import json
from pathlib import Path

import pytest

FIXTURE_DIR = Path("tests/fixtures/thesportsdb")

FIXTURE_FILES = [
    "f1_r1_2026.json",
    "f1_r3_2026.json",
    "f1_r500_2026_testing.json",
    "wec_r1_2026.json",
    "wec_r2_2026_class_split.json",
    "wec_r3_2026_hyperpole.json",
    "wec_r500_2026_prologue.json",
    "indycar_r1_2026.json",
    "indycar_r500_2026_empty.json",
    "imsa_r1_2026.json",
    "imsa_r500_2026_roar.json",
]


@pytest.mark.parametrize("filename", FIXTURE_FILES)
def test_fixture_is_valid_json_with_events_key(filename):
    data = json.loads((FIXTURE_DIR / filename).read_text())
    assert "events" in data


def test_f1_round1_has_practice_qualifying_and_race():
    data = json.loads((FIXTURE_DIR / "f1_r1_2026.json").read_text())
    names = [e["strEvent"] for e in data["events"]]
    assert any("Practice 1" in n for n in names)
    assert any("Qualifying" in n and "Sprint" not in n for n in names)
    assert "Australian Grand Prix" in names


def test_f1_round3_has_sprint_qualifying_and_sprint():
    data = json.loads((FIXTURE_DIR / "f1_r3_2026.json").read_text())
    names = [e["strEvent"] for e in data["events"]]
    assert any("Sprint Qualifying" in n for n in names)
    assert any(n.endswith("Sprint") for n in names)


def test_wec_round1_race_id_matches_overrides_example():
    data = json.loads((FIXTURE_DIR / "wec_r1_2026.json").read_text())
    race = next(e for e in data["events"] if e["strEvent"] == "6 Hours of Imola")
    assert race["idEvent"] == "2421035"
    assert race["strTime"] == "00:00:00"


def test_wec_round3_hyperpole_uses_en_dash():
    data = json.loads((FIXTURE_DIR / "wec_r3_2026_hyperpole.json").read_text())
    names = [e["strEvent"] for e in data["events"]]
    hyperpole_names = [n for n in names if "Hyperpole" in n]
    assert hyperpole_names
    assert any("–" in n for n in hyperpole_names)


def test_wec_round2_class_split_qualifying_uses_hyphen():
    data = json.loads((FIXTURE_DIR / "wec_r2_2026_class_split.json").read_text())
    names = [e["strEvent"] for e in data["events"]]
    assert any("Qualifying - LMGT3" in n or "Qualifying - Hypercar" in n for n in names)


def test_indycar_round500_is_empty():
    data = json.loads((FIXTURE_DIR / "indycar_r500_2026_empty.json").read_text())
    assert data["events"] in (None, [])


def test_imsa_round1_is_bare_race_name_with_unconfirmed_time():
    data = json.loads((FIXTURE_DIR / "imsa_r1_2026.json").read_text())
    race = data["events"][0]
    assert race["strEvent"] == "Rolex 24 At DAYTONA"
    assert race["strTime"] == "00:00:00"


def test_round_500_fixtures_are_non_championship_named():
    f1 = json.loads((FIXTURE_DIR / "f1_r500_2026_testing.json").read_text())
    wec = json.loads((FIXTURE_DIR / "wec_r500_2026_prologue.json").read_text())
    imsa = json.loads((FIXTURE_DIR / "imsa_r500_2026_roar.json").read_text())
    assert any("Testing" in e["strEvent"] for e in f1["events"])
    assert any("Prologue" in e["strEvent"] for e in wec["events"])
    assert any("Roar" in e["strEvent"] for e in imsa["events"])
