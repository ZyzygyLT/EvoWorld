"""Etapa 1B — RecoveryCoordinator (DEC01/DEC04/DEC06) y startup (DEC02).

Cubre la máquina de estados, la Etapa A (recuperar+registrar), la Etapa B
(activar+verificar), el rollback de ambas, la disponibilidad de resultados
no recuperables y la secuencia de startup.  Etapa 1B-R1 añade la política
de runners PAUSADOS con tarea iniciada (Corrección A) y el rollback +
re-propagación ante ``asyncio.CancelledError`` (Corrección B).

Las pruebas de lifespan/WebSocket necesitan FastAPI+httpx (DEC07, extra
`server` ausente en este entorno): se crean con ``importorskip`` y quedan
como SKIPPED/NO EJECUTADAS — nunca se declaran aprobadas sin ejecución.

Numeración según Etapa 1B §12:
  Máquina de estados: ms_01..ms_10
  Etapa A:            a_01..a_08
  Etapa B:            b_01..b_05
  Integración:        i_01..i_05
Etapa 1B-R1 (§2 Corrección A, §3 Corrección B):
  Pausados:           pa_01
  Cancelación:        c_01..c_07
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from worldsim.server import db
from worldsim.server.db import Base, WorldRow
from worldsim.server.recovery_coordinator import (
    CoordinatorState,
    RecoveryCoordinator,
    run_startup,
)
from worldsim.server.recovery_manager import RecoveryResult
from worldsim.server.runner import RunnerRegistry, WorldRunner

SRC = str(Path(__file__).resolve().parents[1] / "src")


# ---------------------------------------------------------------------------
# Doubles y helpers
# ---------------------------------------------------------------------------


class FakeEngine:
    """Motor observable mínimo: mes + tick (sin worldgen)."""

    def __init__(self, month: int = 0) -> None:
        self.month = month
        self.paused = False

    def tick(self) -> None:
        self.month += 1


def _ok(world_id: int, status: str = "running", engine=None) -> RecoveryResult:
    """Resultado ``ok`` que replica el contrato de ``RecoveryManager.recover``.

    El manager real fija el espejo del motor antes de devolver el resultado
    (``engine.paused = world_status == "paused"``, recovery_manager.py):
    el doble hace lo mismo para que las pruebas puedan verificar la
    distinción persistido / runner / motor (Etapa 1B-R1 §2).
    """
    eng = engine if engine is not None else FakeEngine()
    eng.paused = status == "paused"
    return RecoveryResult(
        status="ok",
        world_id=world_id,
        engine=eng,
        world_status=status,
        checkpoint_sequence=1,
        checkpoint_month=0,
        diagnostics="recovered (test)",
    )


def _not_recoverable(
    world_id: int, status: str = "running", why: str = "corrupt"
) -> RecoveryResult:
    return RecoveryResult(
        status="not_recoverable",
        world_id=world_id,
        world_status=status,
        diagnostics=why,
    )


def _no_checkpoints(world_id: int, status: str = "running") -> RecoveryResult:
    return RecoveryResult(
        status="no_checkpoints",
        world_id=world_id,
        world_status=status,
        diagnostics="no checkpoints (test)",
    )


class FakeRecoveryManager:
    """Doble programable: por mundo -> resultado, Excepción o hook callable."""

    def __init__(self, plan: dict, events: list | None = None) -> None:
        self.plan = dict(plan)
        self.events = events if events is not None else []
        self.calls: list[int] = []

    def recover(self, world_id: int) -> RecoveryResult:
        self.calls.append(world_id)
        self.events.append(("recover", world_id))
        if world_id not in self.plan:
            raise AssertionError(f"recover({world_id}) no programado en el test")
        outcome = self.plan[world_id]
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome(world_id)
        return outcome


class FakePersistence:
    """Doble de PersistenceService para observar drain/close (ownership §10)."""

    def __init__(self, *, fail_close: bool = False) -> None:
        self._closed = False
        self.fail_close = fail_close
        self.drain_calls = 0
        self.close_calls = 0

    def wait_pending_writes(self) -> None:
        if self._closed:
            raise RuntimeError("PersistenceService is closed")
        self.drain_calls += 1

    def close(self) -> None:
        self.close_calls += 1
        # el real marca _closed ANTES de poder fallar: lo mimetizamos
        self._closed = True
        if self.fail_close:
            raise RuntimeError("persistence close exploded")


class OrderingPersistence(FakePersistence):
    """FakePersistence que registra drain/close en una lista compartida.

    Permite verificar el ORDEN de los acontecimientos (drenaje, cierre y
    propagación de la cancelación) mediante eventos en lugar de sleeps.
    """

    def __init__(self, events: list, *, fail_close: bool = False) -> None:
        super().__init__(fail_close=fail_close)
        self.events = events

    def wait_pending_writes(self) -> None:
        self.events.append(("drain",))
        super().wait_pending_writes()

    def close(self) -> None:
        self.events.append(("close",))
        super().close()


class BlockingPersistence(OrderingPersistence):
    """Drenaje que se bloquea hasta que el test lo libera (§3.4).

    ``drain_entered`` se señala DESDE EL HILO de drenaje: cuando el test lo
    observa, el coordinador ya está esperando el cleanup protegido — punto
    determinista para entregar una SEGUNDA cancelación mientras el
    drenaje/cierre sigue en curso.  ``released`` deja constancia de que el
    drenaje se reanudó y completó.
    """

    def __init__(
        self,
        events: list,
        drain_entered: threading.Event,
        drain_release: threading.Event,
    ) -> None:
        super().__init__(events)
        self.drain_entered = drain_entered
        self.drain_release = drain_release
        self.released = False

    def wait_pending_writes(self) -> None:
        self.events.append(("drain_entered",))
        self.drain_entered.set()
        self.released = self.drain_release.wait(timeout=10)
        super().wait_pending_writes()


def build_coordinator(
    Session,
    plan: dict,
    *,
    events: list | None = None,
    registry: RunnerRegistry | None = None,
    persistence=None,
    owns_persistence: bool = False,
    runner_factory=None,
):
    registry = registry if registry is not None else RunnerRegistry()
    persistence = persistence if persistence is not None else FakePersistence()
    coordinator = RecoveryCoordinator(
        persistence=persistence,
        recovery_manager=FakeRecoveryManager(plan, events),
        registry=registry,
        session_factory=Session,
        owns_persistence=owns_persistence,
        runner_factory=runner_factory,
    )
    return coordinator, registry, persistence


def recording_factory(events: list):
    """Runner que registra cada start() en la lista compartida de eventos."""

    class RecordingRunner(WorldRunner):
        async def start(self):
            events.append(("start", self.world_id))
            return await super().start()

    return RecordingRunner


def slow_factory(delay: float = 0.2):
    """Runner cuyo start() se suspende (permite observar estados intermedios)."""

    class SlowRunner(WorldRunner):
        async def start(self):
            await asyncio.sleep(delay)
            return await super().start()

    return SlowRunner


def exploding_factory(created: list, explode_world: int | None = None):
    """Runner que falla en start() DESPUÉS de crear su tarea principal (§7)."""

    class Recording(WorldRunner):
        async def start(self):
            return await super().start()

    class Exploding(WorldRunner):
        async def start(self):
            # la tarea se crea ANTES de lanzar: el rollback debe encontrarla
            self._task = asyncio.create_task(asyncio.sleep(30))
            raise RuntimeError("activation exploded")

    def factory(world_id, *args, **kwargs):
        cls = Exploding if world_id == explode_world else Recording
        instance = cls(world_id, *args, **kwargs)
        created.append(instance)
        return instance

    return factory


def barrier_factory(
    created: list,
    *,
    entered: asyncio.Event,
    gate: asyncio.Event,
    block_world: int,
    aux_tasks: list | None = None,
    aux_coro=None,
):
    """Runner que se bloquea en ``start()`` ANTES de crear su tarea (1B-R1).

    - ``block_world``: ``start()`` avisa (``entered``) y espera ``gate`` —
      ese punto de suspensión es donde se entrega la cancelación de forma
      determinista (sin sleeps arbitrarios).
    - los demás mundos inician normalmente: su tarea principal queda creada
      ANTES de que el mundo bloqueado se suspenda.
    - si se pasa ``aux_tasks``, justo antes de bloquearse crea una tarea
      auxiliar sobre ``_aux_tasks`` (el mismo conjunto que lee el rollback)
      para ejercitar el drenaje de auxiliares durante la cancelación;
      ``aux_coro`` define esa auxiliar (por defecto una breve que termina
      durante el drenaje; un coroutine eterno ejercita el timeout).
    """

    class BarrierRunner(WorldRunner):
        async def start(self):
            if self.world_id == block_world:
                if aux_tasks is not None:
                    coro = (
                        aux_coro() if aux_coro is not None else asyncio.sleep(0.05)
                    )
                    aux = asyncio.create_task(coro)
                    self._aux_tasks.add(aux)
                    aux_tasks.append(aux)
                entered.set()
                await gate.wait()
            return await super().start()

    def factory(world_id, *args, **kwargs):
        instance = BarrierRunner(world_id, *args, **kwargs)
        created.append(instance)
        return instance

    return factory


async def _startup_capture(coordinator, captured: dict, events: list | None = None):
    """Ejecuta ``startup()`` y guarda la excepción que este PROPAGA.

    El ``raise`` desnudo de ``startup()`` preserva la instancia original, así
    que lo capturado AQUÍ es exactamente la excepción que salió del
    coordinador (para verificar identidad con la que se estaba manejando).

    Además, EN EL INSTANTE en que la excepción sale de ``startup()`` se
    captura el estado del rollback (``_rollback_completed``/``state``/
    ``_attempt_runners``): debe estar ya completo AQUÍ, lo que prueba que el
    rollback terminó ANTES de la propagación (§3.5).  Si se pasa ``events``,
    se registra ``("propagated", exc)`` al final para ordenarlo frente a los
    eventos de drenaje/cierre del PersistenceService.
    """
    try:
        return await coordinator.startup()
    except BaseException as exc:
        captured["propagated"] = exc
        captured["rollback_completed"] = coordinator._rollback_completed
        captured["state"] = coordinator.state
        captured["attempt_runners_at_raise"] = dict(coordinator._attempt_runners)
        if events is not None:
            events.append(("propagated", exc))
        raise


def _spy_guarded(coordinator, handled: list) -> None:
    """Captura la excepción que ``startup()`` está manejando (identidad).

    ``_rollback_guarded`` se invoca directamente desde el bloque ``except``
    de ``startup()`` SIN ningún ``await`` intermedio, así que al arrancar la
    corrutina espía ``sys.exc_info()`` devuelve EXACTAMENTE la instancia
    activa del ``except`` — la misma que el ``raise`` final re-propaga.
    """
    real = coordinator._rollback_guarded

    async def spy():
        handled.append(sys.exc_info()[1])
        return await real()

    coordinator._rollback_guarded = spy  # sombra en la instancia (sin self)


async def stop_runners(registry: RunnerRegistry, world_ids) -> None:
    """Sustituye a close() (Etapa 2): cancela, ESPERA terminación y drena."""
    for world_id in world_ids:
        runner = registry.get_existing(world_id)
        if runner is None:
            continue
        task = getattr(runner, "_task", None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait([task], timeout=5)
        await runner.wait_aux_tasks(timeout=5)


def _pending_tasks():
    """Tareas vivas del loop distintas de la propia prueba (observable)."""
    return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    """Session factory aislado por test; también aísla db.SessionLocal.

    resolve_pending/league/auth usan db.SessionLocal en tiempo de ejecución
    (los runners activos los invocan en cada tick), así que hay que
    desacoplarlos de la BD de desarrollo preexistente.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'coordinator.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", Session)
    yield Session
    engine.dispose()


def make_world(Session, status: str = "running", *, name: str = "w") -> int:
    session = Session()
    try:
        row = WorldRow(
            seed=42, width=64, height=32, months_simulated=0,
            land_pct=50.0, state_json="{}",
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            name=name, mode="single", sea_level=0.2, status=status,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Máquina de estados
# ---------------------------------------------------------------------------


def test_ms_01_estado_inicial(db_session):
    coordinator, _registry, _persistence = build_coordinator(db_session, {})
    assert coordinator.state is CoordinatorState.NOT_STARTED
    assert coordinator.rollback_errors == []


def test_ms_02_inicio_unico(db_session):
    wid = make_world(db_session, status="running")
    coordinator, registry, _persistence = build_coordinator(
        db_session, {wid: _ok(wid)}
    )

    async def scenario():
        summary = await coordinator.startup()
        runner = registry.get_existing(wid)
        assert runner is not None
        assert runner._task is not None and not runner._task.done()
        await stop_runners(registry, [wid])
        return summary

    summary = asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.COMPLETED
    assert summary.recovered == (wid,)
    assert summary.activated == (wid,)


def test_ms_03_startup_duplicado_rechazado(db_session):
    wid = make_world(db_session, status="running")
    coordinator, registry, _persistence = build_coordinator(
        db_session, {wid: _ok(wid)}
    )

    async def scenario():
        await coordinator.startup()
        with pytest.raises(RuntimeError, match="cannot start up from state COMPLETED"):
            await coordinator.startup()  # duplicado: la instancia no se reutiliza
        assert coordinator.state is CoordinatorState.COMPLETED  # intacto
        await stop_runners(registry, [wid])

    asyncio.run(scenario())


def test_ms_04_completed_solo_tras_ambas_etapas(db_session):
    wid = make_world(db_session, status="running")
    coordinator, registry, _persistence = build_coordinator(
        db_session, {wid: _ok(wid)}, runner_factory=slow_factory(0.2)
    )

    async def scenario():
        task = asyncio.create_task(coordinator.startup())
        await asyncio.sleep(0.05)  # dentro de la Etapa B (start() suspendido)
        assert coordinator.state is CoordinatorState.IN_PROGRESS
        with pytest.raises(RuntimeError, match="invalid transition"):
            coordinator.begin_shutdown()  # COMPLETED aún no existe
        summary = await task
        assert coordinator.state is CoordinatorState.COMPLETED
        assert summary.activated == (wid,)
        await stop_runners(registry, [wid])

    asyncio.run(scenario())


def test_ms_05_fallo_y_estado_failed(db_session):
    w1 = make_world(db_session, status="running")
    w2 = make_world(db_session, status="running")
    w3 = make_world(db_session, status="running")
    events: list = []
    boom = RuntimeError("infra down")
    coordinator, registry, persistence = build_coordinator(
        db_session,
        {w1: _ok(w1), w2: boom, w3: _ok(w3)},
        events=events,
        owns_persistence=True,
    )

    async def scenario():
        with pytest.raises(RuntimeError, match="infra down") as excinfo:
            await coordinator.startup()
        # la excepción original se propaga SIN envolver, tras el cleanup
        assert excinfo.value is boom
        # rollback de Etapa A: desregistrar lo registrado, sin iniciar nada
        assert registry.get_existing(w1) is None
        assert not any(event[0] == "start" for event in events)
        assert persistence.drain_calls == 1
        assert persistence.close_calls == 1

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.FAILED
    assert coordinator.rollback_errors == []  # cleanup limpio


def test_ms_06_retry_solo_con_recursos_vivos(db_session):
    wid = make_world(db_session, status="running")
    calls = {"n": 0}

    def flaky(_world_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient db error")
        return _ok(wid)

    coordinator, registry, persistence = build_coordinator(
        db_session, {wid: flaky}, owns_persistence=False
    )

    async def scenario():
        with pytest.raises(RuntimeError, match="transient db error"):
            await coordinator.startup()
        assert coordinator.state is CoordinatorState.FAILED
        assert coordinator.rollback_errors == []
        assert persistence.close_calls == 0  # recurso externo: no se tocó
        # regla 5: FAILED -> NOT_STARTED -> ... -> COMPLETED
        summary = await coordinator.startup()
        assert summary.recovered == (wid,)
        await stop_runners(registry, [wid])

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.COMPLETED


def test_ms_07_retry_rechazado_si_recursos_cerrados(db_session):
    wid = make_world(db_session, status="running")
    coordinator, registry, persistence = build_coordinator(
        db_session, {wid: RuntimeError("always fails")}, owns_persistence=True
    )

    async def scenario():
        with pytest.raises(RuntimeError, match="always fails"):
            await coordinator.startup()
        # el rollback cerró el servicio de propiedad exclusiva...
        assert persistence.close_calls == 1
        # ...reglas 4-6: esta instancia ya no puede reutilizarse
        with pytest.raises(RuntimeError, match="retry rejected"):
            await coordinator.startup()

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.FAILED


def test_ms_07b_retry_rechazado_con_errores_de_cleanup(db_session):
    wid = make_world(db_session, status="running")
    persistence = FakePersistence(fail_close=True)
    coordinator, _registry, _persistence2 = build_coordinator(
        db_session,
        {wid: RuntimeError("boom")},
        persistence=persistence,
        owns_persistence=True,
    )

    async def scenario():
        with pytest.raises(RuntimeError, match="boom"):
            await coordinator.startup()
        # FAILED se establece AUNQUE el cleanup falle (§7) y el error
        # original no se sustituye por el de cleanup
        assert any("close exploded" in str(e) for e in coordinator.rollback_errors)
        with pytest.raises(RuntimeError, match="retry rejected"):
            await coordinator.startup()

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.FAILED


def test_ms_08_estados_terminales(db_session):
    wid1 = make_world(db_session, status="paused")
    coordinator, _registry, _persistence = build_coordinator(
        db_session, {wid1: _ok(wid1, status="paused")}
    )

    async def start(c):
        await c.startup()

    asyncio.run(start(coordinator))
    assert coordinator.state is CoordinatorState.COMPLETED
    coordinator.begin_shutdown()
    assert coordinator.state is CoordinatorState.SHUTTING_DOWN
    coordinator.finish_shutdown(complete=True)
    assert coordinator.state is CoordinatorState.CLOSED  # terminal
    with pytest.raises(RuntimeError, match="cannot start up from state CLOSED"):
        asyncio.run(coordinator.startup())
    with pytest.raises(RuntimeError, match="invalid transition"):
        coordinator.begin_shutdown()

    # rama CLOSE_INCOMPLETE con otra instancia
    wid2 = make_world(db_session, status="paused")
    # este coordinador ve TODOS los mundos de la BD (incl. wid1): el plan
    # debe contemplarlos aunque este test solo ejerza la transición.
    other, _r2, _p2 = build_coordinator(
        db_session,
        {wid1: _ok(wid1, status="paused"), wid2: _ok(wid2, status="paused")},
    )
    asyncio.run(start(other))
    other.begin_shutdown()
    other.finish_shutdown(complete=False)
    assert other.state is CoordinatorState.CLOSE_INCOMPLETE  # terminal
    with pytest.raises(
        RuntimeError, match="cannot start up from state CLOSE_INCOMPLETE"
    ):
        asyncio.run(other.startup())
    with pytest.raises(RuntimeError, match="invalid transition"):
        other.begin_shutdown()


def test_ms_09_transiciones_invalidas(db_session):
    coordinator, _registry, _persistence = build_coordinator(db_session, {})
    with pytest.raises(RuntimeError, match="invalid transition"):
        coordinator.begin_shutdown()  # NOT_STARTED -> SHUTTING_DOWN no existe
    with pytest.raises(RuntimeError, match="SHUTTING_DOWN"):
        coordinator.finish_shutdown(complete=True)  # requiere SHUTTING_DOWN

    wid = make_world(db_session, status="running")
    failed, _r2, _p2 = build_coordinator(
        db_session, {wid: RuntimeError("x")}
    )

    async def fail():
        with pytest.raises(RuntimeError, match="x"):
            await failed.startup()

    asyncio.run(fail())
    assert failed.state is CoordinatorState.FAILED
    with pytest.raises(RuntimeError, match="invalid transition"):
        failed.begin_shutdown()  # FAILED -> SHUTTING_DOWN no existe


def test_ms_10_startup_concurrente_rechazado(db_session):
    wid = make_world(db_session, status="running")
    coordinator, registry, _persistence = build_coordinator(
        db_session, {wid: _ok(wid)}, runner_factory=slow_factory(0.2)
    )

    async def scenario():
        first = asyncio.create_task(coordinator.startup())
        await asyncio.sleep(0.05)  # primera ejecución dentro de la Etapa B
        with pytest.raises(RuntimeError, match="already in progress"):
            await coordinator.startup()  # regla 7: dos arranques simultáneos
        await first
        assert coordinator.state is CoordinatorState.COMPLETED
        await stop_runners(registry, [wid])

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Etapa A — recuperación y registro
# ---------------------------------------------------------------------------


def test_a_01_recuperacion_mundo_running(db_session):
    wid = make_world(db_session, status="running")
    engine = FakeEngine(month=12)
    coordinator, registry, _persistence = build_coordinator(
        db_session, {wid: _ok(wid, status="running", engine=engine)}
    )

    async def scenario():
        summary = await coordinator.startup()
        runner = registry.get_existing(wid)
        # DEC12: la instancia inyectada es EXACTAMENTE la recuperada
        assert runner.eng is engine
        assert runner.eng.month == 12
        # running: pausa en memoria desactivada y tarea creada en la Etapa B
        assert runner.paused is False
        assert runner._task is not None and not runner._task.done()
        assert summary.recovered == (wid,)
        await stop_runners(registry, [wid])

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.COMPLETED


def test_a_02_recuperacion_mundo_paused_conserva_pausa(db_session):
    wid = make_world(db_session, status="paused")
    engine = FakeEngine(month=7)
    coordinator, registry, _persistence = build_coordinator(
        db_session, {wid: _ok(wid, status="paused", engine=engine)}
    )

    async def scenario():
        summary = await coordinator.startup()
        runner = registry.get_existing(wid)
        assert runner.eng is engine
        # 1B-R1: "conservando la pausa" con la tarea INICIADA (DEC06):
        # registrado, tarea viva, pausa activa y espejo del motor correcto.
        assert runner.paused is True
        assert runner.eng.paused is True  # espejo fijado en la recuperación
        assert runner._task is not None and not runner._task.done()
        assert summary.paused_registered == (wid,)

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.COMPLETED


def test_a_03_registro_via_register_existing(db_session):
    wid = make_world(db_session, status="running")

    class SpyRegistry(RunnerRegistry):
        def __init__(self):
            super().__init__()
            self.registrations: list = []

        def register_existing(self, world_id, runner):
            self.registrations.append((world_id, runner))
            return super().register_existing(world_id, runner)

    registry = SpyRegistry()
    coordinator, returned, _persistence = build_coordinator(
        db_session, {wid: _ok(wid)}, registry=registry
    )
    assert returned is registry

    async def scenario():
        await coordinator.startup()
        await stop_runners(registry, [wid])

    asyncio.run(scenario())
    # una única llamada a register_existing(world_id, runner) (DEC11)
    assert len(registry.registrations) == 1
    registered_id, registered_runner = registry.registrations[0]
    assert registered_id == wid
    assert registered_runner is registry.get_existing(wid)


def test_a_04_nada_se_inicia_durante_la_etapa_a(db_session):
    w1 = make_world(db_session, status="running")
    w2 = make_world(db_session, status="running")
    events: list = []

    def mid_stage_hook(_world_id):
        # observación intermedia: durante la Etapa A NO existe ninguna tarea
        # en el loop (lo único pendiente es el propio startup)
        stray = _pending_tasks()
        assert stray == [], f"tareas inesperadas durante la Etapa A: {stray}"
        return _ok(w2)

    coordinator, registry, _persistence = build_coordinator(
        db_session,
        {w1: _ok(w1), w2: mid_stage_hook},
        events=events,
        runner_factory=recording_factory(events),
    )

    async def scenario():
        await coordinator.startup()
        await stop_runners(registry, [w1, w2])

    asyncio.run(scenario())
    # orden observable: TODAS las recuperaciones antes de CUALQUIER inicio
    first_start = min(i for i, e in enumerate(events) if e[0] == "start")
    last_recover = max(i for i, e in enumerate(events) if e[0] == "recover")
    assert last_recover < first_start
    assert events[0][0] == "recover"


def test_a_05_aislamiento_mundo_not_recoverable(db_session):
    w1 = make_world(db_session, status="running")
    w2 = make_world(db_session, name="corrupt", status="running")
    w3 = make_world(db_session, name="stopped", status="stopped")
    nr2 = _not_recoverable(w2, status="running", why="all checkpoints corrupt")
    nr3 = _not_recoverable(w3, status="stopped", why="status 'stopped' unhelpful")
    coordinator, registry, _persistence = build_coordinator(
        db_session, {w1: _ok(w1), w2: nr2, w3: nr3}
    )

    async def scenario():
        summary = await coordinator.startup()
        assert summary.recovered == (w1,)
        assert summary.not_recoverable == (w2, w3)
        await stop_runners(registry, [w1])

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.COMPLETED
    # registro explícito por world_id, en memoria (DEC04), sin persistirlo
    assert coordinator.not_recoverable[w2] is nr2
    assert coordinator.not_recoverable[w3] is nr3
    assert coordinator.recovery_results[w2].status == "not_recoverable"
    # sin runner ni motor sustituto para los no recuperables
    assert registry.get_existing(w2) is None
    assert registry.get_existing(w3) is None


def test_a_06_continua_tras_fallo_recuperable_individual(db_session):
    w1 = make_world(db_session, status="running")
    w2 = make_world(db_session, name="nocp", status="running")
    w3 = make_world(db_session, name="after", status="running")
    coordinator, registry, _persistence = build_coordinator(
        db_session,
        {w1: _ok(w1), w2: _no_checkpoints(w2), w3: _ok(w3)},
    )

    async def scenario():
        summary = await coordinator.startup()
        # el mundo individual fallido se aisla y los demás continúan
        assert summary.recovered == (w1, w3)
        assert summary.no_checkpoints == (w2,)
        await stop_runners(registry, [w1, w3])

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.COMPLETED
    assert coordinator.recovery_manager.calls == [w1, w2, w3]
    assert w2 not in coordinator.not_recoverable  # resultado distinto
    assert registry.get_existing(w2) is None


def test_a_07_rollback_ante_fallo_de_infraestructura(db_session):
    w1 = make_world(db_session, status="running")
    w2 = make_world(db_session, name="boom", status="running")
    w3 = make_world(db_session, name="never", status="running")
    events: list = []
    boom = RuntimeError("db connection lost")
    coordinator, registry, persistence = build_coordinator(
        db_session,
        {w1: _ok(w1), w2: boom, w3: _ok(w3)},
        events=events,
        owns_persistence=True,
    )

    async def scenario():
        with pytest.raises(RuntimeError, match="db connection lost") as excinfo:
            await coordinator.startup()
        assert excinfo.value is boom  # excepción original preservada
        # runner registrado antes del fallo: desregistrado, sin residuos
        assert registry.get_existing(w1) is None
        assert registry.get_existing(w2) is None
        assert registry.get_existing(w3) is None  # jamás alcanzado
        # recurso de propiedad exclusiva: drenado y cerrado
        assert persistence.drain_calls == 1
        assert persistence.close_calls == 1
        # jamás hubo actividad de start()
        assert not any(event[0] == "start" for event in events)

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.FAILED
    assert coordinator.rollback_errors == []


def test_a_08_sin_motores_sustitutos(db_session, monkeypatch):
    wid = make_world(db_session, status="running")
    engine = FakeEngine(month=3)

    def boom(self, seed, width, height):
        raise AssertionError("el motor inyectado no debe reconstruirse")

    # control: el parche es efectivo (sin engine= SÍ intentaría construir)
    monkeypatch.setattr(WorldRunner, "_build_engine", boom)
    with pytest.raises(AssertionError, match="intentaría construir|reconstruir"):
        WorldRunner(wid, seed=1, width=32, height=32)

    coordinator, registry, _persistence = build_coordinator(
        db_session, {wid: _ok(wid, engine=engine)}
    )

    async def scenario():
        await coordinator.startup()
        # ningún runner construyó un motor propio: usan el inyectado
        assert registry.get_existing(wid).eng is engine
        await stop_runners(registry, [wid])

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.COMPLETED


# ---------------------------------------------------------------------------
# Etapa B — activación
# ---------------------------------------------------------------------------


def test_b_01_runners_running_inician_y_producen_ticks(
    db_session, monkeypatch, races, governments
):
    wid = make_world(db_session, status="running")
    # _broadcast_tick importa app (FastAPI, DEC07 ausente): se sustituye SOLO
    # ese acoplamiento. Tick, resolución de profecías y liga corren de verdad.
    monkeypatch.setattr(WorldRunner, "_broadcast_tick", lambda self: None)
    # F7-C (§16.16): un runner con operaciones duraderas serializa el motor
    # en el checkpoint de T1/T2, que un doble mínimo (FakeEngine) no puede
    # producir. Este test de activación usa un motor REAL sobre un mundo
    # propio para que el tick autónomo sea un ciclo duradero completo.
    from worldsim.sim.engine import SimEngine
    from worldsim.worldgen import WorldConfig
    from worldsim.worldgen.pipeline import generate_world

    engine = SimEngine(
        generate_world(WorldConfig(seed=42, width=64, height=32)),
        races, governments, seed=42,
    )
    coordinator, registry, _persistence = build_coordinator(
        db_session, {wid: _ok(wid, engine=engine)}
    )

    async def scenario():
        await coordinator.startup()
        runner = registry.get_existing(wid)
        assert runner.paused is False
        assert runner._task is not None and not runner._task.done()
        month0 = runner.eng.month
        await asyncio.sleep(1.2)  # >= 2 intervalos de tick (0.5 s)
        # simulación AUTÓNOMA real: la tarea activada avanza el mes
        assert runner.eng.month > month0
        await stop_runners(registry, [wid])

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.COMPLETED


def test_b_02_runner_paused_con_tarea_viva_cero_ticks_y_reanudacion(
    db_session, monkeypatch, races, governments
):
    """1B-R1 §2: pausado registrado con tarea iniciada, CERO ticks y
    reanudación por el mecanismo disponible.

    Verificaciones: (1) registrado, (2) tarea principal viva tras el
    startup, (3) sigue pausado, (4) espejo del motor + estado persistido
    distinto, (5) ningún tick mientras dura la pausa — probado con una
    barrera determinista: el runner `running` del mismo startup avanza de
    verdad, lo que demuestra que el loop giró mientras el pausado llevaba
    cero ticks —, (6) al reanudar (misma palanca que el handler WS de
    app.py: `runner.paused = False`) los ticks continúan, (7) el runner
    `running` sigue funcionando como antes.

    Sin esperas temporales fijas: PUSH_INTERVAL se acelera SOLO en este
    test y todas las esperas son de condición (contadores) con timeout de
    seguridad.
    """
    import worldsim.server.runner as runnermod

    paused_id = make_world(db_session, status="paused")
    running_id = make_world(db_session, name="run", status="running")
    # _broadcast_tick importa app (FastAPI, DEC07 ausente): solo ese acople.
    monkeypatch.setattr(WorldRunner, "_broadcast_tick", lambda self: None)
    # loop acelerado: las esperas pasan a ser de condición (contadores)
    monkeypatch.setattr(runnermod, "PUSH_INTERVAL", 0)
    # F7-C (§16.16): los ticks autónomos pasan por T1/T2, que serializan el
    # motor en checkpoints — se usan motores REALES (uno por runner, mundos
    # propios) en lugar del doble mínimo, sin tocar ninguna otra aserción.
    from worldsim.sim.engine import SimEngine
    from worldsim.worldgen import WorldConfig
    from worldsim.worldgen.pipeline import generate_world

    paused_engine = SimEngine(
        generate_world(WorldConfig(seed=42, width=64, height=32)),
        races, governments, seed=42,
    )
    running_engine = SimEngine(
        generate_world(WorldConfig(seed=42, width=64, height=32)),
        races, governments, seed=42,
    )
    coordinator, registry, _persistence = build_coordinator(
        db_session,
        {
            paused_id: _ok(paused_id, status="paused", engine=paused_engine),
            running_id: _ok(running_id, engine=running_engine),
        },
    )

    async def wait_for(predicate, *, timeout=5.0):
        """Espera de condición con timeout de seguridad (sin sleeps fijos)."""
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0)

    async def scenario():
        summary = await coordinator.startup()
        paused_runner = registry.get_existing(paused_id)
        running_runner = registry.get_existing(running_id)

        # (1) registrado + (2) tarea viva + (3) pausado + (4) espejo
        assert paused_runner is not None
        assert paused_runner._task is not None and not paused_runner._task.done()
        assert paused_runner.paused is True
        assert paused_runner.eng.paused is True
        assert summary.paused_registered == (paused_id,)
        assert summary.activated == (paused_id, running_id)
        # (4) el estado PERSISTIDO sigue distinto: la fila no se tocó
        with db_session() as session:
            assert session.get(WorldRow, paused_id).status == "paused"

        # (5) barrera determinista: el runner running produce ticks reales;
        # con >= 3 ticks el loop giró varias iteraciones y el pausado tuvo
        # sus oportunidades de tickar — debe llevar CERO.
        await wait_for(lambda: running_runner.eng.month >= 3)
        assert paused_runner.eng.month == 0
        assert paused_runner._task is not None and not paused_runner._task.done()

        # (6) reanudar con el mecanismo disponible (el flag que el handler
        # WS de app.py alterna): los ticks continúan normalmente.
        paused_runner.paused = False
        await wait_for(lambda: paused_runner.eng.month >= 1)

        # (7) el runner running sigue funcionando como antes
        assert running_runner.eng.month >= 3
        await stop_runners(registry, [paused_id, running_id])

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.COMPLETED


def test_b_03_fallo_activacion_tras_crear_tarea(db_session):
    wid = make_world(db_session, status="running")
    created: list = []
    coordinator, registry, _persistence = build_coordinator(
        db_session, {wid: _ok(wid)}, runner_factory=exploding_factory(created, wid)
    )

    async def scenario():
        with pytest.raises(RuntimeError, match="activation exploded"):
            await coordinator.startup()
        # la tarea creada antes del fallo quedó cancelada y TERMINADA
        # (no basta con haber solicitado la cancelación)
        task = created[0]._task
        assert task is not None
        assert task.done()
        # runner desregistrado y sin tareas huérfanas en el loop
        assert registry.get_existing(wid) is None
        assert _pending_tasks() == []

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.FAILED


def test_b_04_rollback_de_todos_los_runners_del_intento(db_session):
    w1 = make_world(db_session, status="running")
    w2 = make_world(db_session, name="boom", status="running")
    w3 = make_world(db_session, name="paused", status="paused")
    created: list = []
    coordinator, registry, _persistence = build_coordinator(
        db_session,
        {w1: _ok(w1), w2: _ok(w2), w3: _ok(w3, status="paused")},
        runner_factory=exploding_factory(created, explode_world=w2),
    )

    async def scenario():
        with pytest.raises(RuntimeError, match="activation exploded"):
            await coordinator.startup()
        by_world = {r.world_id: r for r in created}
        # activado con éxito antes del fallo: cancelado y terminado
        assert by_world[w1]._task.done()
        # registrado en la A, nunca iniciado: desregistrado sin tarea
        assert by_world[w3]._task is None
        # TODOS los runners del intento fuera del registro
        assert registry.get_existing(w1) is None
        assert registry.get_existing(w2) is None
        assert registry.get_existing(w3) is None

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.FAILED


def test_b_05_ausencia_de_tareas_huerfanas_tras_fallo(db_session):
    w1 = make_world(db_session, status="running")
    w2 = make_world(db_session, name="boom", status="running")
    created: list = []
    coordinator, registry, _persistence = build_coordinator(
        db_session,
        {w1: _ok(w1), w2: _ok(w2)},
        runner_factory=exploding_factory(created, explode_world=w2),
    )

    async def scenario():
        with pytest.raises(RuntimeError, match="activation exploded"):
            await coordinator.startup()
        # ni la tarea de w1 (cancelada en el rollback) ni la de w2 (creada
        # y cancelada) sobreviven: el loop queda limpio
        assert _pending_tasks() == []

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.FAILED


# ---------------------------------------------------------------------------
# Etapa 1B-R1 — Corrección A: runners pausados con tarea iniciada (DEC06)
# ---------------------------------------------------------------------------


def test_pa_01_pausado_registrado_tarea_viva_y_tres_estados_distintos(db_session):
    """Corrección A §2 — distinción conservada: persistido / runner / motor.

    ``test_b_02`` cubre el comportamiento observable (cero ticks con
    barrera determinista y reanudación); aquí se fija el CONTRATO DE
    ESTADOS: la fila persistida sigue ``paused`` (sin escrituras nuevas,
    DEC14 no adelantado), el runner arranca con ``paused=True``, el espejo
    del motor refleja la pausa fijada en la recuperación y la tarea
    principal existe y está viva — todo para un runner ``paused`` que además
    cuenta como ``activated``.
    """
    wid = make_world(db_session, status="paused")
    coordinator, registry, _persistence = build_coordinator(
        db_session, {wid: _ok(wid, status="paused")}
    )

    async def scenario():
        summary = await coordinator.startup()
        runner = registry.get_existing(wid)

        # (1) registrado y (2) tarea principal viva después del startup
        assert runner is not None
        task = coordinator._attempt_tasks.get(wid)
        assert task is not None and not task.done()
        assert runner._task is task  # la MISMA tarea que arrancó la Etapa B

        # (3) sigue pausado y (4) el espejo del motor refleja la pausa
        assert runner.paused is True
        assert runner.eng.paused is True

        # estado PERSISTIDO distinto: la fila no se tocó (sin DEC14)
        with db_session() as session:
            assert session.get(WorldRow, wid).status == "paused"
        # registro del intento: sigue siendo "paused" (no colapsa a "running")
        assert coordinator._attempt_status[wid] == "paused"

        # un runner pausado cuenta como activado Y como pausado
        assert summary.activated == (wid,)
        assert summary.paused_registered == (wid,)

        await stop_runners(registry, [wid])

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.COMPLETED


# ---------------------------------------------------------------------------
# Etapa 1B-R1 — Corrección B: rollback y propagación ante CancelledError
# ---------------------------------------------------------------------------


def test_c_01_cancel_en_etapa_a_tras_registrar(db_session):
    """§3.1 — cancelación pedida DURANTE la Etapa A (síncrona).

    La Etapa A no tiene puntos de suspensión: ``cancel()`` solo fija la
    bandera pendiente, que ``_check_cancelled()`` detecta DESPUÉS de
    registrar (al menos) un runner y ANTES de la Etapa B.  El rollback
    completo se ejecuta antes de propagar la MISMA ``CancelledError``.
    """
    w1 = make_world(db_session, status="running")
    w2 = make_world(db_session, name="second", status="running")
    seen: dict = {}

    def cancelling_hook(world_id):
        # w1 ya fue recuperado Y registrado en este punto (orden por id)
        seen["w1_registered"] = registry.get_existing(w1) is not None
        seen["stage_b_not_started"] = coordinator._stage_b_started is False
        # petición de cancelación SIN punto de suspensión (Etapa A síncrona)
        asyncio.current_task().cancel()
        return _ok(world_id)

    coordinator, registry, _persistence = build_coordinator(
        db_session, {w1: _ok(w1), w2: cancelling_hook}
    )
    handled: list = []
    _spy_guarded(coordinator, handled)
    captured: dict = {}

    async def scenario():
        startup = asyncio.create_task(_startup_capture(coordinator, captured))
        done, pending = await asyncio.wait([startup], timeout=10)
        assert not pending, "startup no terminó tras la cancelación"
        assert startup in done

        # (1) cancelación detectada DESPUÉS de registrar en la Etapa A
        assert seen["w1_registered"] is True
        assert seen["stage_b_not_started"] is True

        # (9) + identidad: la MISMA instancia sale del coordinador
        exc = captured["propagated"]
        assert isinstance(exc, asyncio.CancelledError)
        assert handled and handled[0] is exc

        # (5) rollback COMPLETO antes de propagar (estado capturado EN EL
        # INSTANTE en que la excepción sale de startup())
        assert captured["rollback_completed"] is True
        assert captured["state"] is CoordinatorState.FAILED
        assert captured["attempt_runners_at_raise"] == {}

        # (7) runners del intento desregistrados
        assert registry.get_existing(w1) is None
        assert registry.get_existing(w2) is None
        assert coordinator._attempt_tasks == {}
        # (6) sin tareas huérfanas en el loop
        assert _pending_tasks() == []

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.FAILED


def test_c_02_cancel_en_etapa_b_tras_iniciar(db_session, monkeypatch):
    """§3.2 — cancelación DURANTE la Etapa B, con ≥1 runner ya iniciado.

    Punto de entrega determinista: el segundo runner se bloquea en
    ``start()`` (barrera) ANTES de crear su tarea; cuando el test lo
    observa, el PRIMER runner ya tiene su tarea principal viva.  La
    cancelación se entrega ahí con ``task.cancel()`` — sin sleeps.
    """
    monkeypatch.setattr(WorldRunner, "_broadcast_tick", lambda self: None)
    w1 = make_world(db_session, status="running")
    w2 = make_world(db_session, name="block", status="running")
    created: list = []
    entered = asyncio.Event()
    gate = asyncio.Event()
    coordinator, registry, _persistence = build_coordinator(
        db_session,
        {w1: _ok(w1), w2: _ok(w2)},
        runner_factory=barrier_factory(
            created, entered=entered, gate=gate, block_world=w2
        ),
    )
    handled: list = []
    _spy_guarded(coordinator, handled)
    captured: dict = {}

    async def scenario():
        startup = asyncio.create_task(_startup_capture(coordinator, captured))
        await entered.wait()

        # ≥1 runner YA iniciado en la Etapa B, con su tarea viva
        w1_runner = registry.get_existing(w1)
        started = coordinator._attempt_tasks.get(w1)
        assert started is not None and not started.done()
        assert w1_runner._task is started

        # entrega determinista en el punto de barrera (cancelación externa)
        startup.cancel()
        done, pending = await asyncio.wait([startup], timeout=10)
        assert not pending, "startup no terminó tras la cancelación"

        # (9) + identidad de la CancelledError propagada
        exc = captured["propagated"]
        assert isinstance(exc, asyncio.CancelledError)
        assert handled and handled[0] is exc

        # (5) rollback COMPLETO antes de propagar
        assert captured["rollback_completed"] is True
        assert captured["state"] is CoordinatorState.FAILED
        assert captured["attempt_runners_at_raise"] == {}

        # (6) la tarea iniciada quedó TERMINADA (no solo cancelada)
        assert started.done()
        # (7) runners del intento desregistrados
        assert registry.get_existing(w1) is None
        assert registry.get_existing(w2) is None
        # (6) sin tareas huérfanas en el loop
        assert _pending_tasks() == []

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.FAILED


def test_c_03_cancel_con_tareas_auxiliares_drenadas(db_session, monkeypatch):
    """§3.3 — cancelación con tareas auxiliares: el rollback DRENA su
    finalización real (no basta con haber solicitado cancelación)."""
    monkeypatch.setattr(WorldRunner, "_broadcast_tick", lambda self: None)
    w1 = make_world(db_session, status="running")
    w2 = make_world(db_session, name="block", status="running")
    created: list = []
    entered = asyncio.Event()
    gate = asyncio.Event()
    aux_tasks: list = []
    coordinator, registry, _persistence = build_coordinator(
        db_session,
        {w1: _ok(w1), w2: _ok(w2)},
        runner_factory=barrier_factory(
            created,
            entered=entered,
            gate=gate,
            block_world=w2,
            aux_tasks=aux_tasks,
        ),
    )
    handled: list = []
    _spy_guarded(coordinator, handled)
    captured: dict = {}

    async def scenario():
        startup = asyncio.create_task(_startup_capture(coordinator, captured))
        await entered.wait()

        # la tarea auxiliar EXISTE y sigue viva en el momento de cancelar
        assert len(aux_tasks) == 1
        aux = aux_tasks[0]
        w2_runner = registry.get_existing(w2)
        assert aux in w2_runner._aux_tasks
        assert not aux.done()

        startup.cancel()
        done, pending = await asyncio.wait([startup], timeout=10)
        assert not pending, "startup no terminó tras la cancelación"

        # (9) + identidad y (5) rollback antes de propagar
        exc = captured["propagated"]
        assert isinstance(exc, asyncio.CancelledError)
        assert handled and handled[0] is exc
        assert captured["rollback_completed"] is True
        assert captured["state"] is CoordinatorState.FAILED

        # la auxiliar terminó DE VERDAD durante el drenaje del rollback
        # (esperada hasta el final, no abandonada ni solo cancelada)
        assert aux.done()
        assert aux.cancelled() is False
        # (7) runners desregistrados y (6) sin huérfanas
        assert registry.get_existing(w1) is None
        assert registry.get_existing(w2) is None
        assert _pending_tasks() == []

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.FAILED


def test_c_04_cancel_durante_drenaje_y_cierre_persistence(db_session):
    """§3.4 — cancelación mientras se drena/cierra el PersistenceService.

    Dos cancelaciones: la original (Etapa A) que dispara el rollback, y una
    SEGUNDA entregada mientras el drenaje está bloqueado en su hilo — el
    cleanup protegido la absorbe sin abandonar el trabajo, y el cierre se
    completa ANTES de propagar la cancelación original.  El orden se
    verifica con eventos compartidos (hilo + corrutina), sin sleeps:
    ``drain_entered < drain < close < propagated``.
    """
    w1 = make_world(db_session, status="running")
    w2 = make_world(db_session, name="second", status="running")
    events: list = []
    drain_entered = threading.Event()
    drain_release = threading.Event()

    def cancelling_hook(world_id):
        asyncio.current_task().cancel()  # cancelación ORIGINAL (Etapa A)
        return _ok(world_id)

    persistence = BlockingPersistence(events, drain_entered, drain_release)
    coordinator, registry, _persistence = build_coordinator(
        db_session,
        {w1: _ok(w1), w2: cancelling_hook},
        events=events,
        persistence=persistence,
        owns_persistence=True,
    )
    handled: list = []
    _spy_guarded(coordinator, handled)
    captured: dict = {}

    async def scenario():
        startup = asyncio.create_task(
            _startup_capture(coordinator, captured, events)
        )
        # el drenaje real corre en SU hilo: al señalarse, el coordinador ya
        # está esperando el cleanup protegido (punto determinista)
        entered_ok = await asyncio.to_thread(drain_entered.wait, 10)
        assert entered_ok, "el rollback nunca llegó al drenaje"

        # SEGUNDA cancelación mientras el drenaje sigue bloqueado: debe
        # absorberse sin interrumpir ni abandonar el cleanup
        startup.cancel()
        drain_release.set()

        done, pending = await asyncio.wait([startup], timeout=10)
        assert not pending, "startup no terminó tras la cancelación"

        # (9) se propaga la cancelación ORIGINAL (la 2ª se absorbe)
        exc = captured["propagated"]
        assert isinstance(exc, asyncio.CancelledError)
        assert handled and handled[0] is exc

        # (5) rollback COMPLETO antes de propagar — el ORDEN lo demuestran
        # los eventos compartidos entre el hilo de drenaje y la corrutina
        names = [e[0] for e in events]
        assert (
            names.index("drain_entered")
            < names.index("drain")
            < names.index("close")
            < names.index("propagated")
        )
        assert captured["rollback_completed"] is True
        assert captured["state"] is CoordinatorState.FAILED

        # (8) propiedad exclusiva: drenaje completado y servicio cerrado
        assert persistence.released is True
        assert persistence.drain_calls == 1
        assert persistence.close_calls == 1
        assert persistence._closed is True

        # (7) runners desregistrados y (6) sin huérfanas
        assert registry.get_existing(w1) is None
        assert registry.get_existing(w2) is None
        assert _pending_tasks() == []

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.FAILED


def test_c_05_fallo_de_cleanup_bloquea_retry_y_no_sustituye_cancelacion(
    db_session
):
    """§3.6/§3.10 — un fallo de cleanup se ACUMULA sin sustituir a la
    cancelación original y BLOQUEA cualquier retry inseguro."""
    w1 = make_world(db_session, status="running")
    w2 = make_world(db_session, name="second", status="running")
    events: list = []

    def cancelling_hook(world_id):
        asyncio.current_task().cancel()
        return _ok(world_id)

    persistence = OrderingPersistence(events, fail_close=True)
    coordinator, registry, _persistence = build_coordinator(
        db_session,
        {w1: _ok(w1), w2: cancelling_hook},
        events=events,
        persistence=persistence,
        owns_persistence=True,
    )
    handled: list = []
    _spy_guarded(coordinator, handled)
    captured: dict = {}

    async def scenario():
        startup = asyncio.create_task(
            _startup_capture(coordinator, captured, events)
        )
        done, pending = await asyncio.wait([startup], timeout=10)
        assert not pending, "startup no terminó tras la cancelación"

        # la cancelación ORIGINAL se propaga (identidad); el error de
        # cierre NO la sustituye ni la oculta
        exc = captured["propagated"]
        assert isinstance(exc, asyncio.CancelledError)
        assert handled and handled[0] is exc
        assert any(
            "close exploded" in str(e) for e in coordinator.rollback_errors
        )

        # FAILED se registra aunque el cleanup falle (§7) y el cierre se
        # intentó ANTES de propagar
        assert captured["rollback_completed"] is True
        assert captured["state"] is CoordinatorState.FAILED
        names = [e[0] for e in events]
        assert names.index("close") < names.index("propagated")
        assert persistence.close_calls == 1

        # (10) cleanup sucio -> retry BLOQUEADO en esta instancia
        assert coordinator._can_retry() is False
        with pytest.raises(RuntimeError, match="retry rejected"):
            await coordinator.startup()

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.FAILED


def test_c_06_persistence_real_y_executor_cerrados_en_ownership(db_session):
    """§3.8 — con propiedad exclusiva, el PersistenceService REAL y su
    executor terminan CERRADOS tras la cancelación, y el recurso muerto
    bloquea el retry (reglas 4-6)."""
    from worldsim.server.persistence_service import PersistenceService

    w1 = make_world(db_session, status="running")
    w2 = make_world(db_session, name="second", status="running")

    def cancelling_hook(world_id):
        asyncio.current_task().cancel()
        return _ok(world_id)

    persistence = PersistenceService(session_factory=db_session)
    coordinator, registry, _persistence2 = build_coordinator(
        db_session,
        {w1: _ok(w1), w2: cancelling_hook},
        persistence=persistence,
        owns_persistence=True,
    )
    handled: list = []
    _spy_guarded(coordinator, handled)
    captured: dict = {}

    async def scenario():
        startup = asyncio.create_task(_startup_capture(coordinator, captured))
        done, pending = await asyncio.wait([startup], timeout=10)
        assert not pending, "startup no terminó tras la cancelación"

        # identidad de la cancelación original + rollback antes de propagar
        exc = captured["propagated"]
        assert isinstance(exc, asyncio.CancelledError)
        assert handled and handled[0] is exc
        assert captured["rollback_completed"] is True
        assert captured["state"] is CoordinatorState.FAILED
        assert coordinator.rollback_errors == []  # cierre real limpio

        # (8) servicio REAL cerrado + executor fuera de servicio (observable:
        # submit() sobre un executor apagado lanza RuntimeError)
        assert persistence._closed is True
        assert persistence.executor._shutdown is True
        with pytest.raises(RuntimeError):
            persistence.executor.submit(lambda: None)

        # recurso ya no vivo -> retry rechazado (reglas 4-6)
        assert coordinator._resources_alive() is False
        assert coordinator._can_retry() is False
        with pytest.raises(RuntimeError, match="retry rejected"):
            await coordinator.startup()

        # (7) runners desregistrados y (6) sin huérfanas
        assert registry.get_existing(w1) is None
        assert registry.get_existing(w2) is None
        assert _pending_tasks() == []

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.FAILED


def test_c_07_timeout_de_rollback_retiene_referencia_y_bloquea_retry(
    db_session, monkeypatch
):
    """§3 timeout — auxiliar que NUNCA termina: error de cleanup registrado,
    referencia FUERTE conservada en ``_lingering`` mientras siga activa y
    retry bloqueado (nunca se declara «rollback limpio» ni se afirma
    terminación solo por haberla solicitado)."""
    import worldsim.server.recovery_coordinator as rcmod

    monkeypatch.setattr(rcmod, "ROLLBACK_TIMEOUT", 0.05)
    monkeypatch.setattr(WorldRunner, "_broadcast_tick", lambda self: None)

    w1 = make_world(db_session, status="running")
    w2 = make_world(db_session, name="block", status="running")
    created: list = []
    entered = asyncio.Event()
    gate = asyncio.Event()
    aux_release = asyncio.Event()
    aux_tasks: list = []
    coordinator, registry, _persistence = build_coordinator(
        db_session,
        {w1: _ok(w1), w2: _ok(w2)},
        runner_factory=barrier_factory(
            created,
            entered=entered,
            gate=gate,
            block_world=w2,
            aux_tasks=aux_tasks,
            aux_coro=lambda: aux_release.wait(),  # jamás termina sola
        ),
    )
    handled: list = []
    _spy_guarded(coordinator, handled)
    captured: dict = {}

    async def scenario():
        startup = asyncio.create_task(_startup_capture(coordinator, captured))
        await entered.wait()

        # ≥1 runner iniciado y auxiliar viva antes de cancelar
        started = coordinator._attempt_tasks.get(w1)
        assert started is not None and not started.done()
        aux = aux_tasks[0]
        assert not aux.done()

        startup.cancel()
        done, pending = await asyncio.wait([startup], timeout=10)
        assert not pending, "startup no terminó tras la cancelación"

        # identidad + rollback «completo» pero NO limpio
        exc = captured["propagated"]
        assert isinstance(exc, asyncio.CancelledError)
        assert handled and handled[0] is exc
        assert captured["rollback_completed"] is True
        assert captured["state"] is CoordinatorState.FAILED
        assert captured["attempt_runners_at_raise"] == {}

        # el timeout quedó REGISTRADO como error de cleanup (nunca se
        # afirmó que la auxiliar terminó)
        assert any(
            "auxiliary tasks still pending" in str(e)
            for e in coordinator.rollback_errors
        )
        # referencia FUERTE conservada mientras el trabajo siga activo
        assert aux in coordinator._lingering
        assert started.done()  # la principal SÍ terminó

        # (10) cleanup sucio -> retry BLOQUEADO
        assert coordinator._can_retry() is False
        with pytest.raises(RuntimeError, match="retry rejected"):
            await coordinator.startup()

        # liberar el trabajo colgado: al terminar, la referencia se suelta
        aux_release.set()
        await asyncio.wait([aux], timeout=5)
        await asyncio.sleep(0)  # deja correr los done-callbacks
        assert aux.done() and aux.cancelled() is False
        assert aux not in coordinator._lingering

        # al final: runners desregistrados y sin tareas huérfanas
        assert registry.get_existing(w1) is None
        assert registry.get_existing(w2) is None
        assert _pending_tasks() == []

    asyncio.run(scenario())
    assert coordinator.state is CoordinatorState.FAILED


# ---------------------------------------------------------------------------
# Integración (ejecutables sin FastAPI)
# ---------------------------------------------------------------------------


def test_i_01_ensure_schema_ejecutado_antes_de_recuperar(tmp_path):
    """DEC02/DEC06: la recuperación real consulta tablas que solo
    ``ensure_schema()`` crea, ANTES de que se ejecuten las etapas.

    La BD de prueba nace SOLO con la tabla ``worlds`` (creada por el test).
    Cualquier otra tabla la debe crear ``ensure_schema()`` dentro de
    ``run_startup``.  Si no corriera antes de la recuperación, el
    ``RecoveryManager`` real fallaría al consultar ``world_checkpoints``
    (OperationalError) y el startup terminaría en FAILED.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'startup.db'}")
    WorldRow.__table__.create(engine)  # SOLO la tabla worlds
    Session = sessionmaker(bind=engine)
    session = Session()
    row = WorldRow(
        seed=7, width=32, height=32, months_simulated=0, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="only", mode="single", sea_level=0.2, status="running",
    )
    session.add(row)
    session.commit()
    world_id = row.id
    session.close()

    registry = RunnerRegistry()
    coordinator = asyncio.run(run_startup(session_factory=Session, registry=registry))
    try:
        assert coordinator.state is CoordinatorState.COMPLETED
        # ensure_schema creó el resto del esquema (predictions, ...)
        assert inspect(engine).has_table("predictions")
        assert inspect(engine).has_table("world_checkpoints")
        # la recuperación REAL se ejecutó después y resolvió el mundo
        result = coordinator.recovery_results[world_id]
        assert result.status == "no_checkpoints"
        # sin checkpoints no hay runner ni motor sustituto (DEC04)
        assert registry.get_existing(world_id) is None
    finally:
        coordinator.persistence.close()  # el test libera su propio recurso
    engine.dispose()


def test_i_02_importar_db_no_ejecuta_ensure_schema(tmp_path):
    """DEC02: importar ``worldsim.server.db`` no crea ni modifica el esquema."""
    db_file = tmp_path / "import_side_effect.db"
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["WORLDSIM_DB"] = f"sqlite:///{db_file.as_posix()}"
    proc = subprocess.run(
        [sys.executable, "-c", "import worldsim.server.db"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    assert not db_file.exists(), (
        "importar db.py no debe crear ni tocar la base de datos (DEC02)"
    )


# ---------------------------------------------------------------------------
# Integración (requieren FastAPI + httpx: DEC07 — NO EJECUTADAS aquí)
# ---------------------------------------------------------------------------


def _fastapi_or_skip():
    pytest.importorskip("fastapi", reason="DEC07: extra `server` no instalado")
    pytest.importorskip("httpx", reason="DEC07: TestClient necesita httpx")


def _fresh_server_env(tmp_path, monkeypatch):
    """Aísla la app de la BD de desarrollo y del registro global.

    Patrón ya usado por test_server/test_multiplayer: se reconecta
    db.SessionLocal y se rebindan los dos nombres del registro (app y
    runner).  Debe llamarse DESPUÉS de ``_fastapi_or_skip()``.
    """
    from sqlalchemy import create_engine as _create

    import worldsim.server.app as appmod
    import worldsim.server.runner as runnermod

    engine = _create(f"sqlite:///{tmp_path / 'lifespan.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", Session)
    monkeypatch.setattr(runnermod, "registry", runnermod.RunnerRegistry())
    monkeypatch.setattr(appmod, "registry", runnermod.registry)
    return appmod


def test_i_03_lifespan_ejecuta_startup(tmp_path, monkeypatch):
    """(NO EJECUTADA sin fastapi) El lifespan ejecuta el startup completo."""
    _fastapi_or_skip()
    from fastapi.testclient import TestClient

    appmod = _fresh_server_env(tmp_path, monkeypatch)
    with TestClient(appmod.app) as client:
        coordinator = appmod.app.state.coordinator
        assert coordinator.state is CoordinatorState.COMPLETED
        assert client.get("/api/meta").status_code == 200
    # el cierre integrado es Etapa 6/DEC17: aquí el test libera su recurso
    appmod.app.state.coordinator.persistence.close()


def _exception_text(exc, _seen=None) -> str:
    """repr() de una excepción incluyendo groups/causes/contexts (sin ciclos)."""
    _seen = _seen if _seen is not None else set()
    if id(exc) in _seen:
        return ""
    _seen.add(id(exc))
    parts = [repr(exc)]
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            parts.append(_exception_text(inner, _seen))
    if exc.__cause__ is not None:
        parts.append(_exception_text(exc.__cause__, _seen))
    if exc.__context__ is not None:
        parts.append(_exception_text(exc.__context__, _seen))
    return " ".join(parts)


def test_i_04_startup_fallido_impide_disponibilidad(tmp_path, monkeypatch):
    """(NO EJECUTADA sin fastapi) Un startup fallido nunca deja la app
    disponible «como si la recuperación hubiera terminado» (§9)."""
    _fastapi_or_skip()
    from fastapi.testclient import TestClient

    appmod = _fresh_server_env(tmp_path, monkeypatch)

    async def exploding_run_startup(**_kwargs):
        raise RuntimeError("startup exploded")

    monkeypatch.setattr(appmod, "run_startup", exploding_run_startup)

    with pytest.raises(BaseException) as excinfo:
        with TestClient(appmod.app):
            pass  # inalcanzable: la aplicación no llega a estar disponible
    assert "startup exploded" in _exception_text(excinfo.value)


def test_i_05_ws_no_ejecutado_durante_startup(tmp_path, monkeypatch):
    """(NO EJECUTADA sin fastapi) Ninguna operación WebSocket se ejecuta
    mientras el startup está en curso (DEC06): el servidor no empieza a
    atender conexiones hasta que el lifespan termina."""
    _fastapi_or_skip()
    import time as _time

    from fastapi.testclient import TestClient

    appmod = _fresh_server_env(tmp_path, monkeypatch)
    real_run_startup = appmod.run_startup
    entered = threading.Event()
    release = threading.Event()

    async def slow_run_startup(**kwargs):
        entered.set()
        # Bloquea el hilo del portal durante el startup: mientras no termine,
        # no se atiende ninguna conexión (misma garantía que uvicorn, que
        # no empieza a aceptar sockets antes del lifespan).
        while not release.wait(0.01):
            pass
        return await real_run_startup(**kwargs)

    monkeypatch.setattr(appmod, "run_startup", slow_run_startup)

    observed: dict = {"finished": False, "state_after_ws": None}

    def gate():
        if not entered.wait(timeout=10):
            observed["gate_error"] = "el startup nunca empezó"
            release.set()
            return
        _time.sleep(0.3)
        # la operación WS NO pudo ejecutarse durante el startup
        observed["finished_during_startup"] = observed["finished"]
        release.set()

    def ws_worker():
        if not entered.wait(timeout=10):
            return
        try:
            with client.websocket_connect("/api/ws/1"):
                pass
        except BaseException as exc:  # mundo inexistente -> cierre 4404
            observed["ws_error"] = repr(exc)
        finally:
            observed["finished"] = True
            coordinator = getattr(appmod.app.state, "coordinator", None)
            observed["state_after_ws"] = (
                coordinator.state if coordinator is not None else None
            )

    client = TestClient(appmod.app)
    threading.Thread(target=gate, daemon=True).start()
    worker = threading.Thread(target=ws_worker, daemon=True)
    worker.start()

    client.__enter__()  # ejecuta el lifespan (bloqueado en release)
    try:
        assert appmod.app.state.coordinator.state is CoordinatorState.COMPLETED
        worker.join(timeout=10)
        assert observed["finished"] is True
        # el handler WS solo pudo correr DESPUÉS de COMPLETED
        assert observed.get("finished_during_startup") is False
        assert observed["state_after_ws"] is CoordinatorState.COMPLETED
    finally:
        client.__exit__(None, None, None)
        appmod.app.state.coordinator.persistence.close()
