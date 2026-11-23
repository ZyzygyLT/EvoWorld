"""Etapa 5 behavioral verification (DEC09 / DEC16) via TestClient.

Requires FastAPI + httpx.  Module-level pytest.importorskip makes the WHOLE
module SKIP (not error) when they are absent (DEC07).

12 verifications (DEC09 / DEC16 / §5 / §6):

 1. WS does not execute ticks directly.
 2. No fast-forward initiated from WS.
 3. Ticks still occur via WorldRunner._run().
 4. Pause/resume use the Etapa 4 coordinator.
 5. not_recoverable world gets no lazy runner (409/4409 instead).
 6. Inexistent world → 404 (WS 4404).
 7. not_recoverable world → 409 (WS 4409).
 8. Invalid params → 400 (WS 4400).
 9. Temporary infrastructure failure → 503 (WS 4503).
10. WS disconnect cleans up subscribers.
11. Individual errors are not masked / infra is not hidden.
12. No regressions in existing HTTP routes.
"""

from __future__ import annotations

import json
import itertools
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from sqlalchemy.exc import OperationalError

import worldsim.server.app as appmod
import worldsim.server.runner as runnermod
from worldsim.server.db import Base
from worldsim.server.errors import ws_close_code


# ---------------------------------------------------------------------------
# fakes / fixtures
# ---------------------------------------------------------------------------

_owner_seq = itertools.count(1)


class _FakeResult:
    def __init__(self, status: str, detail: str = "") -> None:
        self.status = status
        self.detail = detail


class _FakeCoord:
    """Stand-in for the lifespan-installed RecoveryCoordinator."""

    def __init__(self, table: dict | None) -> None:
        self.not_recoverable = table or {}


def _fresh_app(monkeypatch, tmp_path):
    """Isolated TestClient + fresh DB + fresh runner registry (no lifespan,
    hence no coordinator unless a test installs one)."""
    db_file = tmp_path / "test_e5.db"
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import worldsim.server.db as dbmod

    test_engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    dbmod.SessionLocal = sessionmaker(bind=test_engine)
    runnermod.registry = runnermod.RunnerRegistry()
    appmod.registry = runnermod.registry
    Base.metadata.create_all(test_engine)
    return TestClient(appmod.app), test_engine


def _register(c) -> dict:
    """Register a fresh owner and return its Authorization headers (F7-A:
    world creation now requires an authenticated session)."""
    n = next(_owner_seq)
    r = c.post(
        "/api/auth/register",
        json={"username": f"owner{n}", "password": "pass1234"},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _make_world(c, months: int = 0, publish: bool = True) -> tuple[int, dict]:
    """Create a world as an authenticated owner (DEC20: 401 anonymous).

    By default the world is PUBLISHED so anonymous third-party calls keep
    exercising the Etapa 5 behavior on a visible world; returns
    ``(world_id, owner_headers)`` for owner-only operations (full detail,
    DELETE, WS with ?token=).
    """
    headers = _register(c)
    r = c.post(
        "/api/worlds",
        json={"seed": 42, "width": 80, "height": 50, "months": months},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    wid = r.json()["id"]
    if publish:
        r = c.put(
            f"/api/worlds/{wid}/visibility",
            json={"visibility": "public"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
    return wid, headers


def _ws_url(wid: int, headers: dict) -> str:
    """WS handshake URL carrying the owner session token (?token=)."""
    tok = headers["Authorization"].split(" ", 1)[1]
    return f"/api/ws/{wid}?token={tok}"


def _install_not_recoverable(c, world_id: int, detail: str = "no checkpoint") -> None:
    """Simulate a RecoveryCoordinator record for ``world_id`` (DEC04/§5)."""
    appmod.app.state.coordinator = _FakeCoord({world_id: _FakeResult("not_recoverable", detail)})


def _uninstall_coordinator() -> None:
    appmod.app.state.coordinator = _FakeCoord({})


# ---------------------------------------------------------------------------
# 1 + 2. WS: no direct tick, no fast-forward
# ---------------------------------------------------------------------------

def test_ws_no_direct_tick(tmp_path, monkeypatch):
    """DEC09: attaching to a WS does not tick the engine."""
    c, _ = _fresh_app(monkeypatch, tmp_path)
    wid, hdr = _make_world(c, months=0)
    with c.websocket_connect(_ws_url(wid, hdr)) as ws:
        ws.receive_json()  # immediate snapshot
        runner = runnermod.registry.get_existing(wid)
        assert runner is not None
        assert runner.eng.month == 0  # handler did NOT tick


def test_ws_no_fast_forward(tmp_path, monkeypatch):
    """DEC09: no fast-forward initiated from WS.

    create_world already ticked `months` times and seeded the runner.
    Unregister it, then attach: a fresh runner starts at month 0 and the
    handler must NOT replay the saved months (old fast-forward behavior).
    """
    c, _ = _fresh_app(monkeypatch, tmp_path)
    wid, hdr = _make_world(c, months=12)
    runnermod.registry.unregister(wid)
    assert runnermod.registry.get_existing(wid) is None

    with c.websocket_connect(_ws_url(wid, hdr)) as ws:
        data = ws.receive_json()
        assert data["type"] == "tick"
        runner = runnermod.registry.get_existing(wid)
        assert runner.eng.month == 0, "WS fast-forwarded the engine"


# ---------------------------------------------------------------------------
# 3. ticks via WorldRunner._run (not the handler)
# ---------------------------------------------------------------------------

def test_ticks_via_runner_run(tmp_path, monkeypatch):
    """DEC09: ticks occur through _run() after resume, never via WS."""
    c, _ = _fresh_app(monkeypatch, tmp_path)
    wid, hdr = _make_world(c, months=0)
    with c.websocket_connect(_ws_url(wid, hdr)) as ws:
        runner = runnermod.registry.get_existing(wid)
        assert runner is not None
        assert runner.paused is True  # DEC06: runners start paused
        ws.send_json({"pause": False})  # resume via coordinator (DEC14)
        # deadline barrier: _run ticks every PUSH_INTERVAL once unpaused
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if runner.eng.month >= 1:
                break
            time.sleep(0.05)
        assert runner.eng.month >= 1, "WorldRunner._run did not tick the engine"


# ---------------------------------------------------------------------------
# 4. pause/resume via Etapa 4 coordinator
# ---------------------------------------------------------------------------

def test_ws_pause_resume_coordinator(tmp_path, monkeypatch):
    """Etapa 4 / DEC14: WS pause/resume go through the coordinator."""
    c, _ = _fresh_app(monkeypatch, tmp_path)
    wid, hdr = _make_world(c, months=0)
    with c.websocket_connect(_ws_url(wid, hdr)) as ws:
        runner = runnermod.registry.get_existing(wid)
        ws.send_json({"pause": True})
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if runner.paused is True:
                break
            time.sleep(0.05)
        assert runner.paused is True  # coordinator mirror applied

        ws.send_json({"pause": False})
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if runner.paused is False:
                break
            time.sleep(0.05)
        assert runner.paused is False


# ---------------------------------------------------------------------------
# 5 + 7. not_recoverable: 409/4409, no lazy runner
# ---------------------------------------------------------------------------

def test_not_recoverable_no_lazy_runner(tmp_path, monkeypatch):
    """§5: a not_recoverable world gets no runner, no substitute engine."""
    c, _ = _fresh_app(monkeypatch, tmp_path)
    wid, _hdr = _make_world(c)  # published: anonymous third party passes
    # the visibility gate (404) and reaches the DEC16 guard (409).
    # create_world semilla un runner; lo retiramos para que las rutas 409
    # no puedan alegar que ya existía uno.
    runnermod.registry.unregister(wid)
    assert runnermod.registry.get_existing(wid) is None
    _install_not_recoverable(c, wid)
    try:
        # HTTP routes refuse with 409 (no lazy runner is created by them).
        for path, method, payload in (
            (f"/api/worlds/{wid}/predictions", "post",
             {"kind": "beast_slain", "params": {},
              "user_token": "u" * 16, "user_name": "Oracle"}),
            (f"/api/worlds/{wid}/chat", "post",
             {"user_token": "u" * 16, "user_name": "Oracle", "text": "hola"}),
            (f"/api/worlds/{wid}/claims", "post",
             {"kingdom_id": 0, "user_token": "u" * 16, "user_name": "Oracle"}),
        ):
            r = getattr(c, method)(path, json=payload)
            assert r.status_code == 409, f"{path}: {r.status_code} {r.text}"
        # no lazy runner was created by the refused routes
        assert runnermod.registry.get_existing(wid) is None
        # WS refuses with 4409 and creates no runner
        try:
            with c.websocket_connect(f"/api/ws/{wid}") as ws:
                ws.receive_json()
            raise AssertionError("WS should have been refused for not_recoverable")
        except WebSocketDisconnect as exc:
            assert exc.code == ws_close_code(409), exc.code
        assert runnermod.registry.get_existing(wid) is None
    finally:
        _uninstall_coordinator()


# ---------------------------------------------------------------------------
# 6. inexistent world -> 404 (WS 4404)
# ---------------------------------------------------------------------------

def test_inexistent_world_404(tmp_path, monkeypatch):
    c, _ = _fresh_app(monkeypatch, tmp_path)
    r = c.get("/api/worlds/999")
    assert r.status_code == 404
    try:
        with c.websocket_connect("/api/ws/999") as ws:
            ws.receive_json()
        raise AssertionError("WS should have been refused for inexistent world")
    except WebSocketDisconnect as exc:
        assert exc.code == ws_close_code(404), exc.code


# ---------------------------------------------------------------------------
# 8. invalid params -> 400 (WS 4400)
# ---------------------------------------------------------------------------

def test_invalid_params_400(tmp_path, monkeypatch):
    c, _ = _fresh_app(monkeypatch, tmp_path)
    wid, _hdr = _make_world(c)  # published: anonymous reaches validation
    # unknown prediction kind -> 400
    r = c.post(
        f"/api/worlds/{wid}/predictions",
        json={"kind": "dragons_return", "params": {},
              "user_token": "u" * 16, "user_name": "X"},
    )
    assert r.status_code == 400, r.text
    # WS: invalid JSON text -> 4400 (third party on a public world gets the
    # bounded 'world' handshake message first, then the close)
    with c.websocket_connect(f"/api/ws/{wid}") as ws:
        ws.receive_json()  # handshake message
        ws.send_text("this is not json")
        try:
            ws.receive_json()
            raise AssertionError("WS should close 4400 for invalid JSON")
        except WebSocketDisconnect as exc:
            assert exc.code == ws_close_code(400), exc.code
    # WS: non-dict JSON -> 4400
    with c.websocket_connect(f"/api/ws/{wid}") as ws:
        ws.receive_json()
        ws.send_text(json.dumps([1, 2, 3]))
        try:
            ws.receive_json()
            raise AssertionError("WS should close 4400 for non-dict message")
        except WebSocketDisconnect as exc:
            assert exc.code == ws_close_code(400), exc.code


# ---------------------------------------------------------------------------
# DEC15: las únicas llamadas .tick() fuera de WorldRunner._run son creación
# (inicialización acotada) y replay de lectura en motor desechable. Ninguna
# avanza el reloj de la simulación en vivo.
# ---------------------------------------------------------------------------

def test_create_world_ticks_are_initialization(tmp_path, monkeypatch):
    """create_world pre-simula `months` al CREAR el mundo (inicialización).

    Los ticks ocurren sobre un motor local ANTES de que exista un
    WorldRunner iniciado; el runner resultante arranca con eng.month == months
    y pausado (DEC06). No es avance del reloj en vivo.
    """
    c, _ = _fresh_app(monkeypatch, tmp_path)
    hdr = _register(c)
    r = c.post("/api/worlds", json={"seed": 42, "width": 80, "height": 50,
                                    "months": 24}, headers=hdr)
    assert r.status_code == 200, r.text
    wid = r.json()["id"]
    assert r.json()["state"]["month"] == 24
    runner = runnermod.registry.get_existing(wid)
    assert runner is not None
    assert runner.eng.month == 24  # inicialización reflejada en el runner
    assert runner.paused is True  # DEC06: no arranca ejecutando


def test_get_kingdom_stats_does_not_advance_shared_clock(tmp_path, monkeypatch):
    """get_kingdom_stats replaya en un motor LOCAL y deseachable.

    No toca el motor del WorldRunner compartido, aunque esté activo. El
    mes del runner no cambia tras la consulta.
    """
    c, _ = _fresh_app(monkeypatch, tmp_path)
    wid, hdr = _make_world(c, months=10)
    with c.websocket_connect(_ws_url(wid, hdr)) as ws:
        ws.receive_json()  # handshake snapshot (month == 10, pausado)
        runner = runnermod.registry.get_existing(wid)
        assert runner is not None
        before = runner.eng.month  # 10
        # Consulta de stats: replay local hasta state["month"] == 10.
        # (mundo publicado: el tercero anónimo también pasa la compuerta)
        r = c.get(f"/api/worlds/{wid}/stats/0")
        assert r.status_code == 200, r.text
        after = runner.eng.month
        assert after == before, "get_kingdom_stats avanzó el reloj compartido"


# ---------------------------------------------------------------------------
# 9. temporary infrastructure failure -> 503 (WS 4503)
# ---------------------------------------------------------------------------

def test_infra_failure_503(tmp_path, monkeypatch):
    c, _ = _fresh_app(monkeypatch, tmp_path)
    wid, _hdr = _make_world(c)

    orig_load_row = appmod._load_row

    def _boom(world_id: int):
        raise OperationalError("SELECT 1", (), None)

    monkeypatch.setattr(appmod, "_load_row", _boom)
    try:
        r = c.get(f"/api/worlds/{wid}")
        assert r.status_code == 503, f"{r.status_code} {r.text}"
        # WS: infra -> close 4503
        try:
            with c.websocket_connect(f"/api/ws/{wid}") as ws:
                ws.receive_json()
            raise AssertionError("WS should close 4503 on infra failure")
        except WebSocketDisconnect as exc:
            assert exc.code == ws_close_code(503), exc.code
    finally:
        monkeypatch.setattr(appmod, "_load_row", orig_load_row)


# ---------------------------------------------------------------------------
# 10. WS disconnect cleans up subscribers
# ---------------------------------------------------------------------------

def test_ws_disconnect_cleanup(tmp_path, monkeypatch):
    c, _ = _fresh_app(monkeypatch, tmp_path)
    wid, hdr = _make_world(c)
    with c.websocket_connect(_ws_url(wid, hdr)) as ws:
        ws.receive_json()
        runner = runnermod.registry.get_existing(wid)
        assert len(runner.subscribers) == 1
    # disconnect ran unsubscribe: dar una pequeña ventana al handler para
    # procesar el cierre y limpiar el suscriptor.
    deadline = time.time() + 5.0
    runner = runnermod.registry.get_existing(wid)
    while time.time() < deadline and len(runner.subscribers) != 0:
        time.sleep(0.05)
        runner = runnermod.registry.get_existing(wid)
    assert runner is not None and len(runner.subscribers) == 0


# ---------------------------------------------------------------------------
# 11. individual errors not masked / infra not hidden
# ---------------------------------------------------------------------------

def test_infra_is_not_masked_as_404_or_409(tmp_path, monkeypatch):
    """Rule 8: an infrastructure failure stays 503, never 404/409/500."""
    c, _ = _fresh_app(monkeypatch, tmp_path)
    wid, _hdr = _make_world(c)

    def _boom(world_id: int):
        raise OperationalError("SELECT 1", (), None)

    monkeypatch.setattr(appmod, "_load_row", _boom)
    try:
        r = c.get(f"/api/worlds/{wid}")
        assert r.status_code == 503, f"{r.status_code} {r.text}"
        body = r.json()
        assert "infrastructure" in body.get("detail", "").lower()
    finally:
        monkeypatch.setattr(appmod, "_load_row", appmod._load_row)


# ---------------------------------------------------------------------------
# 12. no regressions in existing HTTP routes
# ---------------------------------------------------------------------------

def test_http_routes_smoke(tmp_path, monkeypatch):
    c, _ = _fresh_app(monkeypatch, tmp_path)
    wid, hdr = _make_world(c, months=5)  # auth create + publish (DEC20)

    r = c.get("/api/worlds")
    assert r.status_code == 200
    worlds_page = r.json()
    assert len(worlds_page["worlds"]) == 1

    # owner sees the full saved state
    r = c.get(f"/api/worlds/{wid}", headers=hdr)
    assert r.status_code == 200 and r.json()["month"] == 5

    r = c.get(f"/api/worlds/{wid}/stats/0")
    assert r.status_code == 200

    r = c.post(
        f"/api/worlds/{wid}/predictions",
        json={"kind": "beast_slain", "params": {},
              "user_token": "u" * 16, "user_name": "Oracle"},
    )
    assert r.status_code == 200
    assert r.json()["month"] >= 0

    r = c.post(
        f"/api/worlds/{wid}/chat",
        json={"user_token": "u" * 16, "user_name": "Herald", "text": "hola"},
    )
    assert r.status_code == 200
    r = c.get(f"/api/worlds/{wid}/chat")
    assert r.status_code == 200 and r.json()[-1]["text"] == "hola"

    r = c.get(f"/api/worlds/{wid}/leaderboard")
    assert r.status_code == 200

    r = c.get("/api/meta")
    assert r.status_code == 200 and "prediction_kinds" in r.json()

    r = c.get("/api/identity")
    assert r.status_code == 200

    # DELETE works (DEC10-exempt from 409) and forgets not_recoverable
    r = c.delete(f"/api/worlds/{wid}", headers=hdr)
    assert r.status_code == 200
    r = c.get("/api/worlds")
    assert len(r.json()["worlds"]) == 0
