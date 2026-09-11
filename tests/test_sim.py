"""Simulation engine tests."""

from __future__ import annotations

from worldsim.sim.models import EventType, Relation


def test_initial_setup(engine):
    assert len(engine.kingdoms) == 4
    civilized = [k for k in engine.kingdoms if k.race.form == "civilized"]
    tribal = [k for k in engine.kingdoms if k.race.form == "tribal"]
    assert len(civilized) == 2 and len(tribal) == 2
    for k in engine.kingdoms:
        assert len(k.sites) == 1
        assert k.sites[0].is_capital
        assert k.sites[0].population == 20


def test_mutable_state_not_shared(engine):
    """Original bug: Civ.Sites was a CLASS attribute shared by all instances."""
    a, b = engine.kingdoms[0], engine.kingdoms[1]
    a.sites.append(a.sites[0])  # append junk to one
    assert len(b.sites) == 1
    assert a.sites is not b.sites


def test_tick_advances_time(engine):
    engine.tick()
    assert engine.month == 1


def test_population_grows(engine):
    for _ in range(24):
        engine.tick()
    for k in engine.kingdoms:
        assert k.total_population > 20


def test_expansion_creates_sites(engine):
    for _ in range(240):
        engine.tick()
    expanded = [k for k in engine.kingdoms if len(k.sites) > 1]
    assert expanded, "expected at least one kingdom to expand in 20 years"


def test_chronicle_records_foundings(engine):
    for _ in range(12):
        engine.tick()
    kinds = {e.type for e in engine.chronicle.events}
    assert EventType.KINGDOM_FOUNDED in kinds


def test_wars_resolve_and_flag_consistent(engine):
    for _ in range(600):
        engine.tick()
    for k in engine.kingdoms:
        in_war = any(
            not w.is_over and k in (w.side_a, w.side_b) for w in engine.wars
        )
        assert k.at_war == in_war, f"{k.name} at_war={k.at_war} but in_war={in_war}"


def test_relations_valid(engine):
    for _ in range(100):
        engine.tick()
    for rel in engine.relations.values():
        assert isinstance(rel, Relation)


def test_determinism(small_world, races, governments):
    from worldsim.sim.engine import SimEngine

    e1 = SimEngine(small_world, races, governments, seed=7)
    e2 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(120):
        e1.tick()
        e2.tick()
    assert e1.month == e2.month
    assert [(k.total_population, len(k.sites)) for k in e1.kingdoms] == [
        (k.total_population, len(k.sites)) for k in e2.kingdoms
    ]
    assert len(e1.chronicle.events) == len(e2.chronicle.events)


def test_occupancy_map(engine):
    t = engine.territory_map()
    assert t.shape == (50, 100)
    occupied = t[t >= 0]
    assert len(occupied) == 4  # one capital each


def test_long_run_stable(small_world, races, governments):
    """500 ticks must not crash; populations bounded by 20 sites * popcap."""
    from worldsim.sim.engine import MAX_SITES, SimEngine

    e = SimEngine(small_world, races, governments, seed=11)
    for _ in range(500):
        e.tick()
    for k in e.kingdoms:
        assert len(k.sites) <= MAX_SITES
        max_pop = sum(s.popcap for s in k.sites)
        assert k.total_population <= max_pop + 50  # small slack for intra-month growth
