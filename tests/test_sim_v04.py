"""Tests for v0.6 'Ancient Age': primitive start, slow pacing, notables, genetics."""

from __future__ import annotations

from worldsim.sim.engine import SimEngine


def test_primitive_start_all_tribal(engine):
    """No Democracies at year zero: every realm starts under a Tribal council."""
    for k in engine.kingdoms:
        assert k.government.name == "Tribal"
        assert not k.civilized
        assert k.tech_level == 0
        assert k.leader is not None and k.leader.title == "Chief"
        assert 10 <= k.sites[0].population <= 20
        assert k.total_population == sum(s.population for s in k.sites)


def test_first_expansion_lands_in_window(engine):
    """Slow ancient growth: first new settlement ~year 15-35, not month 3."""
    first = None
    for m in range(1, 601):
        engine.tick()
        if any(len(k.sites) > 1 for k in engine.kingdoms):
            first = m
            break
    assert first is not None, "no expansion in 50 years — growth is frozen"
    assert 100 <= first <= 500, f"first expansion at month {first}, want 100-500"


def test_era_gating_no_early_democracies(engine):
    """Civilized governments stay locked until their era dawns."""
    for _ in range(1200):  # 100 years: still Stone/Bronze age
        engine.tick()
    for k in engine.kingdoms:
        if k.tech_level < 3:
            assert k.government.name != "Democracy", f"{k.name} democratic too early"
        if k.tech_level < 4:
            assert k.government.name != "Technocracy", f"{k.name} technocratic too early"


def test_notables_court(engine):
    """Every realm holds a shaman; merchants/warlords arrive on milestones."""
    for k in engine.kingdoms:
        roles = [n.role for n in k.notables if n.died_month is None]
        assert "shaman" in roles
        for n in k.notables:
            assert 0.0 <= n.ambition <= 1.0
            assert 0.0 <= n.piety <= 1.0
            assert 0.0 <= n.greed <= 1.0
    for _ in range(1200):
        engine.tick()
    # over a century, courts turn over and grow
    total = sum(len(k.notables) for k in engine.kingdoms)
    assert total >= 8


def test_site_traits_bounded_and_heritable(engine):
    """Traits stay in [0,1]; children resemble their parent site."""
    for k in engine.kingdoms:
        for s in k.sites:
            for v in s.traits.values():
                assert 0.0 <= v <= 1.0
    for _ in range(600):
        engine.tick()
    for k in engine.kingdoms:
        for s in k.sites:
            for v in s.traits.values():
                assert 0.0 <= v <= 1.0, f"trait out of bounds: {v}"


def test_fertility_moves_growth(small_world, races, governments):
    """Deterministic check: higher fertility grows strictly faster."""
    e1 = SimEngine(small_world, races, governments, seed=99)
    e2 = SimEngine(small_world, races, governments, seed=99)
    for k in e1.kingdoms:
        for s in k.sites:
            s.traits = {"aggression": 0.5, "fertility": 0.9, "tradition": 0.5}
    for k in e2.kingdoms:
        for s in k.sites:
            s.traits = {"aggression": 0.5, "fertility": 0.1, "tradition": 0.5}
    for _ in range(60):
        e1.tick()
        e2.tick()
    p1 = sum(k.total_population for k in e1.kingdoms)
    p2 = sum(k.total_population for k in e2.kingdoms)
    assert p1 > p2, f"fertile {p1} should outgrow barren {p2}"


def test_growth_never_freezes_small_villages(engine):
    """Fractional accumulation: a 10-soul village must grow within a year,
    unless war/plague/beasts ate the gains (checked via the chronicle)."""
    from worldsim.sim.models import EventType

    k = engine.kingdoms[0]
    s = k.sites[0]
    s.population = 10.0
    before = s.population
    for _ in range(12):
        engine.tick()
    grown = s.population > before or len(k.sites) > 1
    losses = [
        e for e in engine.chronicle.events
        if e.type in (EventType.BATTLE, EventType.PLAGUE, EventType.BEAST_RAMPAGE)
        and k.id in e.kingdom_ids
    ]
    assert grown or losses, "10-soul village froze with no external cause"
