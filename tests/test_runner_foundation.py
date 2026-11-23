"""Etapa 1A — Fundamentos de RunnerRegistry (DEC11) y WorldRunner (DEC12).

Comportamiento observable de registro/retirada de runners, inyección de
motor y ciclo de vida.  No depende de FastAPI: los tests de integración
con TestClient (test_server / test_multiplayer / test_league / test_auth)
requieren DEC07 (fastapi) y no son ejecutables en este entorno.

Numeración según Etapa 1A §6:
  1-9   RunnerRegistry
  10-16 WorldRunner
Extras: validación de argumentos, no-inicio implícito, motor no reemplazable
tras start(), semántica de drenaje de tareas auxiliares y compatibilidad.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from worldsim.server.runner import RunnerRegistry, WorldRunner
from worldsim.sim.engine import SimEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dummy_engine(month: int = 0) -> SimpleNamespace:
    """Lightweight engine stand-in: no worldgen cost for registry tests."""
    return SimpleNamespace(month=month, paused=False)


def _make_runner(world_id: int = 1, engine=None) -> WorldRunner:
    """Runner with an injected engine (never builds one)."""
    if engine is None:
        engine = _dummy_engine()
    return WorldRunner(world_id, seed=1, width=64, height=32, engine=engine)


@pytest.fixture()
def no_engine_build(monkeypatch):
    """Replace engine construction with a cheap dummy (keeps tests fast)."""
    def fake_build(self, seed, width, height):
        return _dummy_engine()
    monkeypatch.setattr(WorldRunner, "_build_engine", fake_build)


class _FakeWS:
    """Fake websocket recording delivered payloads (optionally slow/failing)."""

    def __init__(self, delay: float = 0.0, fail: bool = False) -> None:
        self.delay = delay
        self.fail = fail
        self.sent: list[str] = []

    async def send_text(self, msg: str) -> None:
        await asyncio.sleep(self.delay)
        if self.fail:
            raise OSError("fake socket broke")
        self.sent.append(msg)


# ---------------------------------------------------------------------------
# RunnerRegistry (DEC11) — tests 1-9
# ---------------------------------------------------------------------------

def test_01_register_existing_accepts_runner():
    """(1) Registro de un runner existente."""
    reg = RunnerRegistry()
    r = _make_runner(1)
    assert reg.register_existing(1, r) is r
    assert reg.get_existing(1) is r  # (2) misma instancia vía get_existing


def test_02_register_existing_duplicate_rejected_no_replacement():
    """(3) Rechazo de duplicado + (4) sin sustitución silenciosa."""
    reg = RunnerRegistry()
    first = _make_runner(7)
    second = _make_runner(7)
    reg.register_existing(7, first)
    with pytest.raises(ValueError, match="already has a registered runner"):
        reg.register_existing(7, second)
    # the original registration is untouched (no silent replacement)
    assert reg.get_existing(7) is first
    assert reg.get_existing(7) is not second


def test_03_register_existing_rejects_inconsistent_association():
    """Validación de asociación world_id <-> runner."""
    reg = RunnerRegistry()
    r = _make_runner(2)  # runner belongs to world 2
    with pytest.raises(ValueError, match="belongs to world 2"):
        reg.register_existing(1, r)
    assert reg.get_existing(1) is None
    assert reg.get_existing(2) is None  # nothing registered anywhere


def test_04_register_existing_validates_argument_types():
    """Argumentos inválidos -> TypeError (nada registrado)."""
    reg = RunnerRegistry()
    r = _make_runner(3)
    with pytest.raises(TypeError, match="world_id must be int"):
        reg.register_existing("3", r)
    with pytest.raises(TypeError, match="runner must be WorldRunner"):
        reg.register_existing(3, object())
    assert reg.get_existing(3) is None


def test_05_register_existing_does_not_start_runner(monkeypatch):
    """El registro nunca arranca el runner (start() no es invocado)."""
    started: list[int] = []
    monkeypatch.setattr(
        WorldRunner, "start", lambda self: started.append(self.world_id)
    )
    reg = RunnerRegistry()
    r = _make_runner(9)
    reg.register_existing(9, r)
    assert started == []


def test_06_unregister_returns_runner_and_clears_entry():
    """(5) Retirada devuelve el runner; (6) get_existing -> None después."""
    reg = RunnerRegistry()
    r = _make_runner(4)
    reg.register_existing(4, r)
    assert reg.unregister(4) is r
    assert reg.get_existing(4) is None
    # well-defined result for an absent id (idempotent)
    assert reg.unregister(4) is None
    # no residual reference: re-registering the same id works afterwards
    assert reg.register_existing(4, r) is r


def test_07_08_09_get_lazy_creates_and_reuses_single_instance(no_engine_build):
    """(7) get con runner existente, (8) creación lazy, (9) sin duplicados."""
    reg = RunnerRegistry()
    # (8) lazy creation: nothing exists yet
    assert reg.get_existing(10) is None
    r1 = reg.get(10, seed=1, width=64, height=32)
    # (9) repeated access (even with different build args) returns ONE instance
    r2 = reg.get(10, seed=99, width=128, height=64)
    r3 = reg.get(10, seed=1, width=64, height=32)
    assert r1 is r2 is r3
    assert reg.get_existing(10) is r1  # (7) same instance via get_existing
    # and a different world gets its own instance
    other = reg.get(11, seed=1, width=64, height=32)
    assert other is not r1
    assert reg.get_existing(11) is other


def test_get_existing_never_creates(monkeypatch):
    """get_existing no debe crear runners jamás (DEC11)."""
    def boom(self, seed, width, height):
        raise AssertionError("get_existing must not build engines")
    monkeypatch.setattr(WorldRunner, "_build_engine", boom)
    reg = RunnerRegistry()
    assert reg.get_existing(42) is None


# ---------------------------------------------------------------------------
# WorldRunner — tests 10-16
# ---------------------------------------------------------------------------

def test_10_normal_construction_without_engine_builds_real_engine():
    """(10) Construcción normal sin engine=: construye un SimEngine real."""
    r = WorldRunner(100, seed=42, width=64, height=32)
    assert isinstance(r.eng, SimEngine)
    assert r.eng.month == 0
    # default observable state preserved (compatibilidad §5)
    assert r.paused is True
    assert r.speed == 1


def test_11_12_injected_engine_used_verbatim():
    """(11) Inyección + (12) identidad exacta de la instancia."""
    engine = _dummy_engine(month=7)
    r = WorldRunner(101, seed=1, width=64, height=32, engine=engine)
    assert r.eng is engine  # identity, not a copy
    assert r.eng.month == 7  # state carried over untouched


def test_13_injected_engine_skips_construction(monkeypatch):
    """(13) Con engine= no se construye un segundo motor.

    Control: sin engine= la misma construcción SÍ llama a _build_engine
    (demuestra que el parche es efectivo y que la ruta normal se conserva).
    """
    def boom(self, seed, width, height):
        raise AssertionError("engine construction must not run when injected")
    monkeypatch.setattr(WorldRunner, "_build_engine", boom)

    injected = _dummy_engine()
    r = WorldRunner(102, seed=1, width=64, height=32, engine=injected)
    assert r.eng is injected  # no second engine was built

    with pytest.raises(AssertionError, match="must not run"):
        WorldRunner(103, seed=1, width=64, height=32)  # normal path builds


def test_14_start_is_idempotent_while_running():
    """(14) Inicio sin duplicación: misma tarea en llamadas repetidas."""
    r = _make_runner(104)

    async def scenario():
        t1 = await r.start()
        t2 = await r.start()
        t3 = await r.start()
        try:
            assert t1 is t2 is t3  # single main task, never duplicated
            assert not t1.done()
        finally:
            t1.cancel()
            try:
                await t1
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())


def test_15_start_after_close_rejected():
    """(15) Rechazo de inicio después del cierre.

    Activada en Etapa 2 (DEC18): ``close()`` está implementado y la guardia
    de ``start()`` queda activa desde el inicio del cierre (estado
    ``CLOSING``), de modo que un runner cerrado nunca se reinicia.
    """
    r = _make_runner(107)

    async def scenario():
        await r.close()
        assert r._closed  # el cierre comenzó: guardia activa (§2.4)
        with pytest.raises(RuntimeError, match="cannot start"):
            await r.start()
        assert r._task is None  # nunca se creó tarea: cierre definitivo
        await r.close()  # repetir tras cerrar sigue siendo un no-op correcto

    asyncio.run(scenario())


def test_engine_cannot_be_replaced_after_start():
    """DEC12: el motor no se sustituye una vez comenzada la ejecución."""
    r = _make_runner(105)

    async def scenario():
        task = await r.start()
        try:
            with pytest.raises(RuntimeError, match="cannot replace the engine"):
                r.eng = _dummy_engine(month=99)
            assert r.eng.month == 0  # original engine untouched
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())


def test_engine_can_be_assigned_before_start():
    """DEC12: asignar el motor antes de start() sigue permitido (app.py POST)."""
    r = _make_runner(106)
    new_engine = _dummy_engine(month=5)
    r.eng = new_engine  # must not raise: never started
    assert r.eng is new_engine


def test_16_auxiliary_tasks_retained_until_completion():
    """(16) Referencias fuertes a tareas auxiliares hasta su terminación."""
    r = _make_runner(107)
    ws = _FakeWS(delay=0.05)
    r.subscribe(ws)

    async def scenario():
        r._broadcast_system("hola")
        # drained = every spawned aux task ran to completion (not abandoned)
        assert await r.wait_aux_tasks(timeout=2.0) is True
        # observable outcome: the payload actually reached the socket
        assert ws.sent == [json.dumps({"type": "system", "text": "hola"})]
        # draining again while idle is a well-defined no-op
        assert await r.wait_aux_tasks(timeout=0.1) is True

    asyncio.run(scenario())


def test_wait_aux_tasks_reports_pending_then_drains():
    """El drenaje informa de tareas pendientes y nunca las abandona."""
    r = _make_runner(108)
    ws = _FakeWS(delay=0.4)
    r.subscribe(ws)

    async def scenario():
        r._broadcast_system("x")
        # still pending within a short timeout...
        assert await r.wait_aux_tasks(timeout=0.01) is False
        # ...but it completes later: the strong reference kept it alive
        assert await r.wait_aux_tasks(timeout=2.0) is True
        assert ws.sent  # task finished and delivered

    asyncio.run(scenario())


def test_auxiliary_task_failure_is_logged_not_lost(caplog):
    """Regla 11: una tarea auxiliar fallida se registra, no se silencia."""
    r = _make_runner(109)
    ws = _FakeWS(fail=True)
    r.subscribe(ws)

    async def scenario():
        r._broadcast_system("doomed")
        # drain completes even though the send failed (no hang, no raise)
        assert await r.wait_aux_tasks(timeout=2.0) is True

    with caplog.at_level(logging.WARNING, logger="worldsim.server.runner"):
        asyncio.run(scenario())
    assert any("auxiliary task failed" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Compatibilidad de la interfaz pública (§5)
# ---------------------------------------------------------------------------

def test_public_interface_preserved():
    """subscribe/unsubscribe/set_selected y estado por defecto intactos."""
    r = _make_runner(110)
    ws = _FakeWS()
    r.subscribe(ws)
    assert ws in [sub["ws"] for sub in r.subscribers.values()]
    r.set_selected(ws, 3)
    assert r.subscribers[id(ws)]["selected"] == 3
    r.unsubscribe(ws)
    assert r.subscribers == {}
    r.unsubscribe(ws)  # idempotent for unknown sockets
    assert r.paused is True and r.speed == 1
