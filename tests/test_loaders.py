"""Data loader tests."""

from __future__ import annotations

from worldsim.sim.models import SocialForm


def test_load_races(races):
    assert len(races) == 8
    names = {r.name for r in races}
    assert names == {"Dwarf", "Human", "Elf", "Orc", "Goblin", "Giant", "Wyrm", "Warg Pack"}

    dwarf = next(r for r in races if r.name == "Dwarf")
    assert dwarf.preferred_biomes == (9,)
    assert dwarf.form == "civilized"
    assert dwarf.social_form == SocialForm.CIVILIZED

    goblin = next(r for r in races if r.name == "Goblin")
    assert goblin.social_form == SocialForm.TRIBAL


def test_load_governments(governments):
    assert len(governments) == 8
    names = {g.name for g in governments}
    assert "Absolute Monarchy" in names
    assert "Democracy" in names
    demo = next(g for g in governments if g.name == "Democracy")
    assert demo.tech_bonus == 20
    assert 0 <= demo.militarization <= 100


def test_race_fields(races):
    for r in races:
        assert 1 <= r.strength <= 3
        assert r.reproduction > 0
        assert r.form in ("civilized", "tribal", "site")
        assert len(r.preferred_biomes) >= 1
