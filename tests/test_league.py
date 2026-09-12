"""Tests for the Dynasty League: claims, scoring, seasons, race filters."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import worldsim.server.app as appmod
import worldsim.server.runner as runnermod
from worldsim.server.db import Base


def _fresh(tmp_path: Path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import worldsim.server.db as dbmod

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'test_league.db'}",
        connect_args={"check_same_thread": False},
    )
    dbmod.SessionLocal = sessionmaker(bind=test_engine)
    runnermod.registry = runnermod.RunnerRegistry()
    appmod.registry = runnermod.registry
    Base.metadata.create_all(test_engine)
    return TestClient(appmod.app)


def _world(c: TestClient, **kw) -> int:
    body = {"seed": 42, "width": 80, "height": 50, "months": 0}
    body.update(kw)
    r = c.post("/api/worlds", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _user(i: int = 1) -> dict:
    return {"user_token": f"tok{i:04d}-abcd", "user_name": f"Oracle{i}"}


def test_claim_and_rules(tmp_path):
    c = _fresh(tmp_path)
    wid = _world(c, mode="league")

    # claim kingdom 0
    r = c.post(f"/api/worlds/{wid}/claims", json={"kingdom_id": 0, **_user(1)})
    assert r.status_code == 200, r.text
    assert r.json()["kingdom_id"] == 0

    # same kingdom by another user -> 400
    r = c.post(f"/api/worlds/{wid}/claims", json={"kingdom_id": 0, **_user(2)})
    assert r.status_code == 400

    # same user on another kingdom -> 400 (one per user)
    r = c.post(f"/api/worlds/{wid}/claims", json={"kingdom_id": 1, **_user(1)})
    assert r.status_code == 400

    # list shows the claim
    claims = c.get(f"/api/worlds/{wid}/claims").json()
    assert len(claims) == 1
    assert claims[0]["user_name"] == "Oracle1"


def test_claim_rejected_on_single_world(tmp_path):
    c = _fresh(tmp_path)
    wid = _world(c, mode="single")
    r = c.post(f"/api/worlds/{wid}/claims", json={"kingdom_id": 0, **_user(1)})
    assert r.status_code == 400


def test_scoring_flows_to_standings(tmp_path):
    c = _fresh(tmp_path)
    wid = _world(c, mode="league", months=120)
    # two oracles claim two realms
    c.post(f"/api/worlds/{wid}/claims", json={"kingdom_id": 0, **_user(1)})
    c.post(f"/api/worlds/{wid}/claims", json={"kingdom_id": 1, **_user(2)})

    # drive the canonical runner forward through history
    runner = runnermod.registry.get_existing(wid)
    assert runner is not None
    from worldsim.server.league import score_new_events

    start_month = runner.eng.month
    for _ in range(960):  # 80 years of history to score
        runner.eng.tick()
    msgs = score_new_events(wid, runner.eng, start_month)
    assert isinstance(msgs, list)

    board = c.get(f"/api/worlds/{wid}/standings").json()
    assert {u["user"] for u in board} == {"Oracle1", "Oracle2"}
    assert all("points" in u and "rank" in u for u in board)
    # ranks are ordered by points desc (negatives allowed by design)
    pts = [u["points"] for u in board]
    assert pts == sorted(pts, reverse=True)
    assert all(isinstance(u["points"], int) for u in board)


def test_season_end_freezes_league(tmp_path):
    c = _fresh(tmp_path)
    # tiny season: ends as soon as the runner passes it
    wid = _world(c, mode="league", months=200)
    r = c.post(f"/api/worlds/{wid}/claims", json={"kingdom_id": 0, **_user(1)})
    assert r.status_code == 200

    from worldsim.server.league import maybe_end_season

    runner = runnermod.registry.get_existing(wid)
    # force the runner past a 12-year season by shrinking it
    import worldsim.server.db as dbmod

    with dbmod.SessionLocal() as db:
        from worldsim.server.db import WorldRow

        row = db.get(WorldRow, wid)
        row.season_years = 12
        db.commit()
    msg = maybe_end_season(wid, runner.eng)
    assert msg is not None  # 200 months > 12 years
    assert "🏆" in msg
    # second call is a no-op (already over)
    assert maybe_end_season(wid, runner.eng) is None


def test_enabled_races_filter(tmp_path):
    c = _fresh(tmp_path)
    wid = _world(c, enabled_races=["Human", "Orc", "Giant"])
    state = c.get(f"/api/worlds/{wid}").json()
    races = {k["race"] for k in state["kingdoms"]}
    assert races <= {"Human", "Orc"}
    # fallback guard: nonsense filter restores the full set
    wid2 = _world(c, enabled_races=["Nope"])
    races2 = {k["race"] for k in c.get(f"/api/worlds/{wid2}").json()["kingdoms"]}
    assert len(races2) > 0


def test_world_list_carries_mode(tmp_path):
    c = _fresh(tmp_path)
    wid = _world(c, mode="league")
    worlds = {w["id"]: w for w in c.get("/api/worlds").json()}
    assert worlds[wid]["mode"] == "league"
    assert worlds[wid]["name"]
    assert "season_over" in worlds[wid]
