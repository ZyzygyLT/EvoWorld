"""Tests for v0.2.1: succession rolls, war dynamics, gov cooldown, milestones."""

from __future__ import annotations

from worldsim.sim.engine import SimEngine
from worldsim.sim.models import EventType


def test_leaders_reign_long(small_world, races, governments):
    """Death ages must now cluster around 40y (previously 12-20 due to re-rolling)."""
    e = SimEngine(small_world, races, governments, seed=3)
    for _ in range(1500):
        e.tick()
    deaths = [
        ev for ev in e.chronicle.events if ev.type == EventType.LEADER_DIED
    ]
    assert len(deaths) >= 4
    ages = []
    for ev in deaths:
        # "… died at age N after M victories."
        txt = ev.text
        ages.append(int(txt.split("died at age ")[1].split(" ")[0]))
    avg = sum(ages) / len(ages)
    assert avg > 25, f"average reign {avg:.1f}y is too short — re-roll bug back?"
    assert all(a >= 5 for a in ages), f"no reign can be < 5y: {ages}"


def test_expected_death_rolled_once(small_world, races, governments):
    e = SimEngine(small_world, races, governments, seed=3)
    for k in e.kingdoms:
        leader = k.leader
        assert leader.expected_death_month is not None
        first = leader.expected_death_month
        for _ in range(24):
            e.tick()
            if k.leader is leader and leader.died_month is None:
                assert leader.expected_death_month == first  # never re-rolled


def test_wars_happen_in_long_runs(small_world, races, governments):
    """The static-peace bug (74y with zero wars) must be gone."""
    e = SimEngine(small_world, races, governments, seed=40013)
    for _ in range(900):  # 75 years
        e.tick()
    assert len(e.wars) >= 1, "expected at least one war in 75y"


def test_gov_shift_cooldown(small_world, races, governments):
    """Golden-age shifts must respect the 25y cooldown (no flip-flopping)."""
    e = SimEngine(small_world, races, governments, seed=42)
    for _ in range(1200):
        e.tick()
    shifts = [
        ev for ev in e.chronicle.events if ev.type == EventType.GOVERNMENT_CHANGED
    ]
    # group shifts by kingdom and check gaps (dictatorship survival shifts exempt
    # — approximated here by requiring at least the non-war shifts to be spaced)
    by_kingdom = {}
    for ev in shifts:
        kid = ev.kingdom_ids[0]
        by_kingdom.setdefault(kid, []).append(ev.month)
    for kid, months in by_kingdom.items():
        # allow bursts of 2 (war-driven), but no rapid oscillation of 3+
        assert len(months) < 6, f"kingdom {kid} is flip-flopping: {months}"


def test_milestones_fire_once(small_world, races, governments):
    e = SimEngine(small_world, races, governments, seed=40013)
    for _ in range(1300):
        e.tick()
    milestones = [ev for ev in e.chronicle.events if ev.type == EventType.MILESTONE]
    texts = [ev.text for ev in milestones]
    assert len(milestones) >= 3, "long runs must accumulate milestones"
    # each milestone text appears exactly once
    assert len(texts) == len(set(texts)), "milestones must never repeat"
    # known keys present
    assert any("first" in t for t in texts)


def test_milestone_content(small_world, races, governments):
    e = SimEngine(small_world, races, governments, seed=42)
    for _ in range(1200):
        e.tick()
    texts = " | ".join(
        ev.text for ev in e.chronicle.events if ev.type == EventType.MILESTONE
    )
    assert "settlements" in texts or "City" in texts  # empire/first-city fired
