"""Smoke test of the FastAPI server (REST + WebSocket) via TestClient."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import worldsim.server.app as appmod


def _fresh_app(monkeypatch, tmp_path: Path):
    """Isolated app instance with its own DB file per test."""
    db_file = tmp_path / "test_worlds.db"

    # rebuild the module-level engine/session against a temp DB
    appmod.engine_db = appmod.create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    appmod.SessionLocal = appmod.sessionmaker(bind=appmod.engine_db)
    appmod.Base.metadata.create_all(appmod.engine_db)
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


def test_server_ws_stream(tmp_path, monkeypatch):
    c = _fresh_app(monkeypatch, tmp_path)

    r = c.post("/api/worlds", json={"seed": 7, "width": 80, "height": 50, "months": 0})
    wid = r.json()["id"]

    months = []
    with c.websocket_connect(f"/api/ws/{wid}") as ws:
        for i in range(3):
            data = ws.receive_json()
            assert data["type"] == "tick"
            assert len(data["kingdoms"]) == 4
            # leader payload carries reign data now
            for k in data["kingdoms"]:
                if k["leader"]:
                    assert "reign_years" in k["leader"]
                    assert "battles_won" in k["leader"]
            months.append(data["month"])
            if i == 0:
                ws.send_json({"speed": 2})
    # months must advance (speed=2 applied after first push)
    assert months[1] > months[0]
    assert months[2] - months[1] == 2


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
    c = _fresh_app(monkeypatch, tmp_path)
    r = c.post("/api/worlds", json={"seed": 7, "width": 80, "height": 50, "months": 0})
    wid = r.json()["id"]

    with c.websocket_connect(f"/api/ws/{wid}") as ws:
        data = ws.receive_json()
        assert data["selected_stats"] is None  # nothing selected yet
        ws.send_json({"selected": 0, "speed": 1})
        data2 = ws.receive_json()
        assert data2["selected_stats"] is not None
        assert data2["selected_stats"]["id"] == 0
        assert "military_power" in data2["selected_stats"]
        # deselection clears it
        ws.send_json({"selected": None})
        data3 = ws.receive_json()
        assert data3["selected_stats"] is None
