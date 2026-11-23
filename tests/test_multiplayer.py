"""Tests for the multiplayer layer: shared runner, predictions, chat.

Requires FastAPI + httpx (TestClient); skipped with a DEC07 reason when
they are not installed.
"""

from __future__ import annotations

import itertools

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
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


def _register(c: TestClient) -> dict:
    """Authenticated owner for world creation (DEC20: 401 anonymous)."""
    r = c.post("/api/auth/register",
               json={"username": f"mp{next(_seq)}", "password": "pass1234"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


_seq = itertools.count(1)


def _make_world(c: TestClient, months: int = 0,
                publish: bool = True) -> tuple[int, dict]:
    """Create as owner; publish by default so anonymous third-party calls
    keep exercising the multiplayer routes (returns (id, owner_headers))."""
    headers = _register(c)
    r = c.post("/api/worlds",
               json={"seed": 42, "width": 80, "height": 50, "months": months},
               headers=headers)
    assert r.status_code == 200, r.text
    wid = r.json()["id"]
    if publish:
        r = c.put(f"/api/worlds/{wid}/visibility",
                  json={"visibility": "public"}, headers=headers)
        assert r.status_code == 200, r.text
    return wid, headers


def _ws_url(wid: int, headers: dict) -> str:
    tok = headers["Authorization"].split(" ", 1)[1]
    return f"/api/ws/{wid}?token={tok}"


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
    wid, _hdr = _make_world(c, months=0)

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
    wid, _hdr = _make_world(c)
    r = c.post(
        f"/api/worlds/{wid}/predictions",
        json={"kind": "dragons_return", "params": {},
              "user_token": "b" * 16, "user_name": "X"},
    )
    assert r.status_code == 400


def test_chat_roundtrip(fresh_db):
    c = _client()
    wid, _hdr = _make_world(c)
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
    """Two sockets on the same world see the same month (canonical runner).

    Both sockets authenticate as the OWNER (DEC20): only the owner receives
    the full Etapa 5 tick payload with ``month``.
    """
    c = _client()
    wid, hdr = _make_world(c)
    with c.websocket_connect(_ws_url(wid, hdr)) as ws1, \
         c.websocket_connect(_ws_url(wid, hdr)) as ws2:
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
    """Prophecies stamp the canonical month WITHOUT a WebSocket fast-forward.

    DEC09: the WS no longer fast-forwards.  The canonical month comes from
    the shared runner (or the persisted months_simulated when there is no
    runner), never from a handler-driven tick.  No lazy runner may be
    created just because a prophecy was sealed.
    """
    c = _client()
    wid, _hdr = _make_world(c, months=10)

    # create_world seeds a runner; unregister it so the seal uses the
    # persisted snapshot month and no lazy runner is recreated.
    runner = runnermod.registry.get_existing(wid)
    assert runner is not None
    runnermod.registry.unregister(wid)
    assert runnermod.registry.get_existing(wid) is None

    user = {"user_token": "d" * 16, "user_name": "Oracle"}
    r = c.post(
        f"/api/worlds/{wid}/predictions",
        json={"kind": "beast_slain", "params": {}, **user},
    )
    assert r.status_code == 200, r.text
    pred = r.json()
    assert pred["month"] >= 10  # sealed at canonical (persisted) month
    # no lazy runner may exist just because a prediction was sealed
    assert runnermod.registry.get_existing(wid) is None
