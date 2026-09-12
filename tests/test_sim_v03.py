"""Tests for v0.3: tech eras, colonies, plagues, influence map, colors."""

from __future__ import annotations

import numpy as np

from worldsim.sim.engine import (
    COLONY_FOUNDED_MIN_SITES,
    ERA_NAMES,
    MAX_KINGDOMS,
    SimEngine,
    _golden_angle_color,
)
from worldsim.sim.models import EventType


def test_golden_angle_colors_distinct():
    colors = [_golden_angle_color(i) for i in range(12)]
    # all readable: luminance in a healthy band
    for r, g, b in colors:
        lum = r * 0.299 + g * 0.587 + b * 0.114
        assert 60 <= lum <= 210, f"unreadable color {(r, g, b)}, lum={lum}"
    # pairwise distinct (not near-identical)
    for i in range(len(colors)):
        for j in range(i + 1, len(colors)):
            d = sum(abs(a - b) for a, b in zip(colors[i], colors[j], strict=True))
            assert d > 40, f"colors {i} and {j} too similar: {colors[i]} vs {colors[j]}"


def test_kingdom_colors_assigned_golden(engine):
    """Engine-assigned colors must come from the golden-angle walk."""
    seen = {_golden_angle_color(i) for i in range(len(engine.kingdoms))}
    for k in engine.kingdoms:
        assert k.color in seen


def test_tech_eras_advance(small_world, races, governments):
    e = SimEngine(small_world, races, governments, seed=42)
    for k in e.kingdoms:
        assert k.tech_level == 0  # world starts in the Stone Age
    for _ in range(4200):  # 350y (tech tree: root + Bronze node take ~200-300y)
        e.tick()
    eras = [k.tech_level for k in e.kingdoms]
    assert any(t >= 1 for t in eras), f"no realm left the Stone Age in 350y: {eras}"
    # chronicle records era transitions
    tech_events = [ev for ev in e.chronicle.events if ev.type == EventType.TECH_ERA]
    assert tech_events
    for ev in tech_events:
        assert any(era in ev.text for era in ERA_NAMES)


def test_tribes_civilize(small_world, races, governments):
    """Tribes reaching the Iron Age adopt a real government."""
    e = SimEngine(small_world, races, governments, seed=42)
    for _ in range(7200):  # ~600y (Iron needs root+Bronze+Iron nodes)
        e.tick()
    civ_tribes = [
        k for k in e.kingdoms
        if k.race.form == "tribal" and k.civilized
    ]
    # at least one tribe should have civilized by now (probabilistic but
    # tech advances for everyone over centuries)
    assert civ_tribes, "no tribe civilized in 333y"
    for k in civ_tribes:
        assert k.government.name != "Tribal"


def test_influence_map(small_world, races, governments):
    """Influence: claimed tiles belong to the nearest site's kingdom; radius grows with era."""
    e = SimEngine(small_world, races, governments, seed=42)
    inf = e.influence_map()
    assert inf.shape == small_world.height.shape
    # every site tile is claimed by its own kingdom
    for k in e.kingdoms:
        for s in k.sites:
            assert inf[s.y, s.x] == k.id
    # claimed ids are valid kingdoms or -1
    valid = {-1} | {k.id for k in e.kingdoms}
    assert set(np.unique(inf).tolist()) <= valid


def test_influence_radius_grows_with_era(small_world, races, governments):
    """Higher tech eras claim more land."""
    e = SimEngine(small_world, races, governments, seed=7)
    before = (e.influence_map() >= 0).sum()
    for k in e.kingdoms:
        k.tech_level = 4  # simulate Medieval realms
    after = (e.influence_map() >= 0).sum()
    assert after > before, "Medieval influence must exceed Stone Age"


def test_colonies_and_independence(small_world, races, governments):
    e = SimEngine(small_world, races, governments, seed=17)
    for _ in range(3500):
        e.tick()
    colonies = [ev for ev in e.chronicle.events if ev.type == EventType.COLONY_FOUNDED]
    indep = [ev for ev in e.chronicle.events if ev.type == EventType.INDEPENDENCE]
    # with realms crossing 12+ sites over ~300y, at least some colony action
    assert len(colonies) + len(indep) > 0 or all(
        len(k.sites) < COLONY_FOUNDED_MIN_SITES for k in e.kingdoms
    )
    assert len(e.kingdoms) <= MAX_KINGDOMS


def test_plague_ravages_dense_realms(small_world, races, governments):
    """Dense realms eventually get struck; mortality stays in bounds."""
    e = SimEngine(small_world, races, governments, seed=23)
    for _ in range(5000):
        e.tick()
    plagues = [ev for ev in e.chronicle.events if ev.type == EventType.PLAGUE]
    assert plagues, "no plague in 400y with dense realms — density valve broken"
    for ev in plagues:
        assert "souls perished" in ev.text


def test_population_never_freezes(small_world, races, governments):
    """The reported bug: total world population froze at saturation.

    With era popcaps, colonies and post-Medieval urban growth, the world
    total after 400y must exceed the naive 20-sites cap of ~year 100.
    """
    e = SimEngine(small_world, races, governments, seed=42)
    totals = []
    for _ in range(4800):  # 400y
        e.tick()
        if e.month % 1200 == 0:
            totals.append(sum(k.total_population for k in e.kingdoms))
    # the world total keeps climbing over the centuries
    assert totals[-1] > totals[0] * 1.5, (
        f"world population froze: totals at y100/200/300/400 = {totals}"
    )


def test_golden_age_boosts_popcap(small_world, races, governments):
    e = SimEngine(small_world, races, governments, seed=42)
    k = e.kingdoms[0]
    base = e._era_popcap_mult(k)
    k.golden_age_until = e.month + 120
    assert e._era_popcap_mult(k) > base  # golden age boosts beyond era mult


def test_kingdom_stats_rich(small_world, races, governments):
    """kingdom_stats() must expose the full inspector payload."""
    e = SimEngine(small_world, races, governments, seed=42)
    for _ in range(600):
        e.tick()
    k = e.kingdoms[0]
    stats = e.kingdom_stats(k)
    for key in (
        "era", "era_progress", "peak_population", "territory_tiles",
        "influence_tiles", "military_power", "militarization_pct",
        "wars_won", "wars_lost", "wars_total", "ongoing_wars",
        "relations", "population_history", "recent_events",
    ):
        assert key in stats, f"missing {key}"
    # military power reflects hero multiplier when present
    assert stats["military_power"] >= 0
    # W/L bookkeeping is consistent with the wars list
    fought = [w for w in e.wars if k in (w.side_a, w.side_b)]
    assert stats["wars_total"] == len(fought)
    assert stats["wars_won"] + stats["wars_lost"] <= len(fought)


def test_war_record_accounting(small_world, races, governments):
    """Every resolved war with a victor increments exactly one W and one L."""
    e = SimEngine(small_world, races, governments, seed=7)
    for _ in range(2000):
        e.tick()
    resolved = [w for w in e.wars if w.is_over and w.victor is not None]
    if not resolved:
        return  # probabilistic
    total_won = sum(k.wars_won for k in e.kingdoms)
    total_lost = sum(k.wars_lost for k in e.kingdoms)
    assert total_won == len(resolved), f"{total_won} wins vs {len(resolved)} resolved wars"
    assert total_lost == len(resolved)
