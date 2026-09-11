"""Tests for the v0.2 sim extensions: leaders, heroes, beasts, named wars, export."""

from __future__ import annotations

import json

from worldsim.sim.engine import SimEngine
from worldsim.sim.models import EventType


def test_leaders_crowned_and_succeed(small_world, races, governments):
    e = SimEngine(small_world, races, governments, seed=3)
    # every kingdom starts with a living leader
    for k in e.kingdoms:
        assert k.leader is not None
        assert k.leader.died_month is None
    # run very long: someone must die and be succeeded
    for _ in range(1500):
        e.tick()
    crowned = [ev for ev in e.chronicle.events if ev.type == EventType.LEADER_CROWNED]
    died = [ev for ev in e.chronicle.events if ev.type == EventType.LEADER_DIED]
    assert len(crowned) >= 5
    assert len(died) >= 1
    # succession keeps a living leader on every living kingdom
    for k in e.kingdoms:
        if k.sites:
            assert k.leader is not None and k.leader.died_month is None


def test_beasts_spawn_and_die(small_world, races, governments):
    e = SimEngine(small_world, races, governments, seed=5, n_beasts=3)
    assert len(e.beasts) == 3
    sighted = [ev for ev in e.chronicle.events if ev.type == EventType.BEAST_SIGHTED]
    assert len(sighted) == 3
    # long run: beasts either get slain, rampage, or survive — but never crash
    for _ in range(600):
        e.tick()
    for b in e.beasts:
        assert 0 <= b.x < small_world.width
        assert 0 <= b.y < small_world.height_tiles


def test_war_has_name_and_cause(small_world, races, governments):
    e = SimEngine(small_world, races, governments, seed=7)
    for _ in range(1000):
        e.tick()
    assert e.wars, "expected wars in a long run"
    for w in e.wars:
        assert w.name, "every war must have a name"
        assert w.cause in ("border friction", "disputed lands") or "alliance" in w.cause
    declared = [ev for ev in e.chronicle.events if ev.type == EventType.WAR_DECLARED]
    for ev in declared:
        assert ev.text.count("began") == 1  # named wars in the chronicle text


def test_heroes_emerge_and_fall(small_world, races, governments):
    e = SimEngine(small_world, races, governments, seed=11)
    for _ in range(1500):
        e.tick()
    emerged = [ev for ev in e.chronicle.events if ev.type == EventType.HERO_EMERGED]
    # heroes are rare; with 4 kingdoms over 125y at least one should exist
    assert len(emerged) >= 1, "expected at least one hero in a very long war-filled run"
    for ev in emerged:
        assert "champion" in ev.text.lower()


def test_battle_heat_accumulates(small_world, races, governments):
    e = SimEngine(small_world, races, governments, seed=7)
    for _ in range(1000):
        e.tick()
    heat = e.battle_heat()
    assert heat.shape == small_world.height.shape
    if e.wars:
        total_battle_deaths = sum(w.total_dead for w in e.wars)
        if total_battle_deaths > 0:
            assert heat.sum() > 0, "battle deaths must appear in the heatmap"


def test_export_full_state(small_world, races, governments, tmp_path):
    from worldsim.export.state import engine_to_dict, engine_to_json

    e = SimEngine(small_world, races, governments, seed=7)
    for _ in range(300):
        e.tick()
    d = engine_to_dict(e)
    assert d["month"] == e.month
    assert len(d["kingdoms"]) == 4
    k0 = d["kingdoms"][0]
    assert {"id", "name", "race", "government", "sites", "leader", "population_history"} <= set(k0)
    assert len(d["chronicle"]) == len(e.chronicle.events)
    # JSON round-trip
    p = engine_to_json(e, tmp_path / "state.json")
    loaded = json.loads(p.read_text(encoding="utf-8"))
    assert loaded["month"] == e.month
    assert len(loaded["chronicle"]) == len(d["chronicle"])


def test_population_history_sampled(small_world, races, governments):
    e = SimEngine(small_world, races, governments, seed=2)
    for _ in range(600):
        e.tick()
    for k in e.kingdoms:
        assert len(k.population_history) >= 1, "yearly samples must accumulate"


def test_alliance_events_logged(small_world, races, governments):
    e = SimEngine(small_world, races, governments, seed=9)
    for _ in range(1200):
        e.tick()
    formed = [ev for ev in e.chronicle.events if ev.type == EventType.ALLIANCE_FORMED]
    # alliances are possible; if any formed, relations dict must reflect WAR/ALLIANCE
    if formed:
        assert any(r == "ALLIANCE" for r in e.relations.values()) or len(e.wars) > 0
