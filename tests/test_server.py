"""Smoke test of the FastAPI server (REST + WebSocket) via TestClient.

The WS is a SHARED CLOCK (WorldRunner): pushes happen on the runner's own
asyncio task, so under the sync TestClient we assert on the immediate
snapshot handshake, not on autonomous ticking.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import worldsim.server.app as appmod
import worldsim.server.runner as runnermod


def _fresh_app(monkeypatch, tmp_path: Path):
    """Isolated app instance with its own DB + runner registry per test.

    Single patch point: db.SessionLocal covers app/resolver/league.
    """
    db_file = tmp_path / "test_worlds.db"

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import worldsim.server.db as dbmod

    test_engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    dbmod.SessionLocal = sessionmaker(bind=test_engine)
    runnermod.registry = runnermod.RunnerRegistry()
    appmod.registry = runnermod.registry
    appmod.Base.metadata.create_all(test_engine)
    return TestClient(appmod.app)


def test_server_rest(tmp_path, monkeypatch):
    c = _fresh_app(monkeypatch, tmp_path)

    # 1. create a small world with 24 pre-simulated months
    r = c.post("/api/worlds", json={"seed": 42, "width": 100, "height": 60, "months": 24})
    assert r.status_code == 200, r.text
    wid = r.json()["id"]
    state = r.json()["state"]
    assert state["month"] == 24
    assert len(state["kingdoms"]) == 4

    # 2. list
    r = c.get("/api/worlds")
    assert r.status_code == 200 and len(r.json()) == 1
    assert r.json()[0]["seed"] == 42

    # 3. fetch full state
    r = c.get(f"/api/worlds/{wid}")
    assert r.status_code == 200 and r.json()["month"] == 24

    # 4. 404
    r = c.get("/api/worlds/999")
    assert r.status_code == 404

    # 5. delete
    r = c.delete(f"/api/worlds/{wid}")
    assert r.status_code == 200
    r = c.get("/api/worlds")
    assert len(r.json()) == 0


def test_server_ws_snapshot_handshake(tmp_path, monkeypatch):
    """Connecting yields an immediate tick snapshot of the shared engine."""
    c = _fresh_app(monkeypatch, tmp_path)
    r = c.post("/api/worlds", json={"seed": 7, "width": 80, "height": 50, "months": 12})
    wid = r.json()["id"]

    with c.websocket_connect(f"/api/ws/{wid}") as ws:
        data = ws.receive_json()
        assert data["type"] == "tick"
        # shared runner fast-forwarded to the saved month on first attach
        assert data["month"] >= 12
        assert len(data["kingdoms"]) == 4
        for k in data["kingdoms"]:
            if k["leader"]:
                assert "reign_years" in k["leader"]
                assert "battles_won" in k["leader"]
        # control messages are accepted; runner state mutates once the
        # endpoint's receive loop processes them (TestClient runs the loop
        # between send and assert, so give it a moment via another receive)
        ws.send_json({"speed": 4})
        runner = runnermod.registry.get_existing(wid)
        ws.send_json({"pause": True})  # pause=True (the shared default)
        # trigger loop iterations: the endpoint drains pending messages on
        # its next receive cycle; send one more control to be safe
        ws.send_json({"speed": 4})
        assert runner.speed == 4
        assert runner.paused is True  # shared clock starts (and stays) paused


def test_server_meta(tmp_path, monkeypatch):
    c = _fresh_app(monkeypatch, tmp_path)
    r = c.get("/api/meta")
    assert r.status_code == 200
    meta = r.json()
    gov_names = [g["name"] for g in meta["governments"]]
    assert "Democracy" in gov_names and "Tribal" in gov_names
    demo = next(g for g in meta["governments"] if g["name"] == "Democracy")
    assert demo["militarization"] == 20
    assert demo["tech_bonus"] == 20
    assert "elected" in demo["description"]  # real description text
    race_names = [r["name"] for r in meta["races"]]
    assert "Dwarf" in race_names and "Giant" in race_names
    # multiplayer: prediction kinds exposed for the UI
    assert "prediction_kinds" in meta
    assert "era_reach" in meta["prediction_kinds"]


def test_kingdom_stats_endpoint(tmp_path, monkeypatch):
    c = _fresh_app(monkeypatch, tmp_path)
    r = c.post("/api/worlds", json={"seed": 42, "width": 80, "height": 50, "months": 60})
    wid = r.json()["id"]

    r = c.get(f"/api/worlds/{wid}/stats/0")
    assert r.status_code == 200
    stats = r.json()
    for key in (
        "name", "race", "government", "era", "era_progress", "population",
        "peak_population", "sites", "territory_tiles", "influence_tiles",
        "military_power", "militarization_pct", "wars_won", "wars_lost",
        "wars_total", "relations", "population_history", "recent_events",
    ):
        assert key in stats, f"stats missing {key}"
    assert isinstance(stats["relations"], list)
    assert isinstance(stats["recent_events"], list)

    # 404 for a bogus kingdom
    r = c.get(f"/api/worlds/{wid}/stats/999")
    assert r.status_code == 404


def test_ws_selected_stats_stream(tmp_path, monkeypatch):
    """Selecting a kingdom pushes its rich stats on a private channel.

    Broadcast ticks never carry selection state (that was the
    panel-stays-hidden bug); the socket that selected gets its own
    {'type': 'stats'} message immediately and after every broadcast.
    """
    c = _fresh_app(monkeypatch, tmp_path)
    r = c.post("/api/worlds", json={"seed": 7, "width": 80, "height": 50, "months": 0})
    wid = r.json()["id"]

    with c.websocket_connect(f"/api/ws/{wid}") as ws:
        data = ws.receive_json()
        assert data["type"] == "tick"
        assert "selected_stats" not in data  # ticks carry no selection state
        ws.send_json({"selected": 0})
        data2 = ws.receive_json()  # immediate private reply on selection
        assert data2["type"] == "stats"
        assert data2["stats"]["id"] == 0
        assert "military_power" in data2["stats"]
        # deselection clears it with an explicit null stats message
        ws.send_json({"selected": None})
        data3 = ws.receive_json()
        assert data3["type"] == "stats"
        assert data3["stats"] is None
