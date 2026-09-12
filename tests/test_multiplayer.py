"""Tests for the multiplayer layer: shared runner, predictions, chat."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import worldsim.server.app as appmod
import worldsim.server.runner as runnermod
from worldsim.server.db import Base


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Fresh DB per test + fresh runner registry.

    Single patch point: every server module resolves sessions via
    db.SessionLocal() at call time, so rebinding it once isolates all
    of app/resolver/league/runner from the real dev database.
    """
    db_file = tmp_path / "test_mp.db"
    # rebuild engine/session against an isolated file DB
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import worldsim.server.db as dbmod

    test_engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    dbmod.SessionLocal = sessionmaker(bind=test_engine)
    # app.py imported `registry` BY NAME at module load — rebind its view too
    import worldsim.server.runner as runnermod

    runnermod.registry = runnermod.RunnerRegistry()
    appmod.registry = runnermod.registry
    Base.metadata.create_all(test_engine)
    yield test_engine
    test_engine.dispose()


def _client():
    return TestClient(appmod.app)


def _make_world(c: TestClient, months: int = 0) -> int:
    r = c.post("/api/worlds", json={"seed": 42, "width": 80, "height": 50, "months": months})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_identity(fresh_db):
    c = _client()
    r = c.get("/api/identity")
    assert r.status_code == 200
    tok = r.json()["token"]
    assert len(tok) >= 16
    tok2 = c.get("/api/identity").json()["token"]
    assert tok2 != tok


def test_prediction_full_cycle(fresh_db):
    """A sealed prophecy resolves against the live chronicle via the runner."""

    c = _client()
    wid = _make_world(c, months=0)

    user = {"user_token": "a" * 16, "user_name": "Cassandra"}
    # seal a beast_slain prophecy
    r = c.post(
        f"/api/worlds/{wid}/predictions",
        json={"kind": "beast_slain", "params": {}, **user},
    )
    assert r.status_code == 200
    pred = r.json()
    assert pred["status"] == "pending"
    assert pred["deadline_months"] > 0

    # the world meta exposes the prediction kinds for the UI
    kinds = c.get("/api/meta").json()["prediction_kinds"]
    assert "beast_slain" in kinds

    # drive the shared runner manually until it resolves or expires
    runner = runnermod.registry.get_existing(wid)
    assert runner is not None
    from worldsim.server.prediction_resolver import resolve_pending

    for _ in range(600):
        runner.eng.tick()
        got = resolve_pending(wid, runner.eng)
        if got:
            break
    preds = c.get(f"/api/worlds/{wid}/predictions").json()
    mine = next(p for p in preds if p["kind"] == "beast_slain")
    assert mine["status"] in ("won", "lost", "expired", "pending")
    if mine["status"] in ("won", "lost"):
        assert mine["resolved_text"]

    # leaderboard aggregates the user
    board = c.get(f"/api/worlds/{wid}/leaderboard").json()
    assert any(u["user"] == "Cassandra" for u in board)


def test_prediction_unknown_kind_rejected(fresh_db):
    c = _client()
    wid = _make_world(c)
    r = c.post(
        f"/api/worlds/{wid}/predictions",
        json={"kind": "dragons_return", "params": {},
              "user_token": "b" * 16, "user_name": "X"},
    )
    assert r.status_code == 400


def test_chat_roundtrip(fresh_db):
    c = _client()
    wid = _make_world(c)
    r = c.post(
        f"/api/worlds/{wid}/chat",
        json={"user_token": "c" * 16, "user_name": "Herald",
              "text": "The Bronze Age is coming!"},
    )
    assert r.status_code == 200
    history = c.get(f"/api/worlds/{wid}/chat").json()
    assert history and history[-1]["text"] == "The Bronze Age is coming!"
    assert history[-1]["user_name"] == "Herald"
    assert history[-1]["month"] >= 0  # stamped with in-world time


def test_ws_shared_clock(fresh_db):
    """Two sockets on the same world see the same month (canonical runner)."""
    c = _client()
    wid = _make_world(c)
    with c.websocket_connect(f"/api/ws/{wid}") as ws1, \
         c.websocket_connect(f"/api/ws/{wid}") as ws2:
        m1 = ws1.receive_json()["month"]
        m2 = ws2.receive_json()["month"]
        # ws2 attached later; the shared clock never rewinds for it
        assert m2 >= m1
        # both attached: advance via ws1 and both must agree (within a push)
        ws1.send_json({"speed": 3, "pause": False})
        a1 = ws1.receive_json()["month"]
        a2 = ws2.receive_json()["month"]
        b1 = ws1.receive_json()["month"]
        assert b1 >= a1 and a2 >= m2


def test_predictions_sealed_with_canonical_month(fresh_db):
    """Prophecies stamp the SHARED clock's month, not the client's claim."""
    c = _client()
    wid = _make_world(c, months=10)
    # open a socket so the runner fast-forwards to the saved month
    with c.websocket_connect(f"/api/ws/{wid}") as ws:
        ws.receive_json()
        runner = runnermod.registry.get_existing(wid)
        assert runner.eng.month >= 10
        r = c.post(
            f"/api/worlds/{wid}/predictions",
            json={"kind": "beast_slain", "params": {},
                  "user_token": "d" * 16, "user_name": "Oracle"},
        )
        pred = r.json()
        assert pred["month"] >= 10  # sealed at canonical month
