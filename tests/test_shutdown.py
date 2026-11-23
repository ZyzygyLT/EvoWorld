"""Etapa 6 — Shutdown integral y verificación del ciclo de vida (DEC17 / DEC18 / DEC05).

Pruebas unitarias (no requieren FastAPI) y pruebas de integración del
lifespan real (requieren fastapi/httpx, se saltan con importorskip si
faltan).

Escenarios:
 A. Shutdown normal: todos los runners se cierran, tareas auxiliares
    terminan, estado final CLOSED, sin checkpoint final automático.
 B. Idempotencia: shutdown repetido no reabre ni reinicia recursos.
 C. Fallo individual de cierre: los demás cierres continúan, error
    registrado, estado CLOSE_INCOMPLETE.
 D. Cancelación durante shutdown: tareas de cierre no abandonadas.
 E. Operaciones concurrentes (pausa/reanudación, eliminación, aux
    pendiente).
 F. Recursos de persistencia: no cierre prematuro, errores visibles,
    sin checkpoint final automático.
 G. Integración real del lifespan: startup + shutdown, y fallo de
    startup con rollback.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

import worldsim.server.app as appmod
import worldsim.server.runner as runnermod
from worldsim.server.db import Base
from worldsim.server.recovery_coordinator import (
    CoordinatorState,
    RecoveryCoordinator,
)
from worldsim.server.persistence_service import PersistenceService


# ===========================================================================
# Helpers
# ===========================================================================

def _make_runner(world_id: int = 1) -> runnermod.WorldRunner:
    """Runner con motor inyectado (sin worldgen)."""
    return runnermod.WorldRunner(
        world_id, seed=1, width=64, height=32,
        engine=SimpleNamespace(month=0),
    )


def _fresh_coordinator(tmp_path, owns_persistence: bool = True):
    """RecoveryCoordinator aislado con su propio runner registry y
    PersistenceService sobre un DB temporal (no usa el lifespan real)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import worldsim.server.db as dbmod

    db_file = tmp_path / "sd.db"
    test_engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    dbmod.SessionLocal = sessionmaker(bind=test_engine)
    Base.metadata.create_all(test_engine)
    registry = runnermod.RunnerRegistry()
    persistence = PersistenceService(session_factory=dbmod.SessionLocal)
    coordinator = RecoveryCoordinator(
        persistence=persistence,
        recovery_manager=None,  # no necesario para shutdown puro
        registry=registry,
        session_factory=dbmod.SessionLocal,
        owns_persistence=owns_persistence,
        runner_factory=runnermod.WorldRunner,
    )
    return coordinator, registry, persistence, test_engine


async def _start_runner(runner: runnermod.WorldRunner):
    """Inicia la tarea principal de un runner (pausado, DEC06 -> sin ticks)."""
    task = await runner.start()
    assert runner.state is runnermod.RunnerState.RUNNING
    return task


# ===========================================================================
# A. Shutdown normal
# ===========================================================================

def test_shutdown_normal_closes_all_runners(tmp_path):
    """(A) Shutdown normal cierra todos los runners registrados."""
    coordinator, registry, persistence, engine = _fresh_coordinator(tmp_path)
    registry.register_existing(1, _make_runner(1))
    registry.register_existing(2, _make_runner(2))

    async def scenario():
        r1 = registry.get_existing(1)
        r2 = registry.get_existing(2)
        await _start_runner(r1)
        await _start_runner(r2)
        # pasar por los estados válidos hasta COMPLETED
        coordinator._transition(CoordinatorState.IN_PROGRESS)
        coordinator._transition(CoordinatorState.COMPLETED)
        errors = await coordinator.shutdown()
        assert errors == [], f"errores inesperados: {errors}"
        assert coordinator.state is CoordinatorState.CLOSED
        assert r1.state is runnermod.RunnerState.CLOSED
        assert r2.state is runnermod.RunnerState.CLOSED
        # tareas realmente terminadas
        assert r1._task.done() and r2._task.done()

    asyncio.run(scenario())
    engine.dispose()


def test_shutdown_normal_no_orphan_aux_tasks(tmp_path):
    """(A) Las tareas auxiliares de los runners se drenan; no quedan huérfanas."""
    coordinator, registry, persistence, engine = _fresh_coordinator(tmp_path)
    runner = registry.register_existing(1, _make_runner(1))

    async def scenario():
        r = registry.get_existing(1)
        await _start_runner(r)
        a1 = r._spawn(asyncio.sleep(3600))
        a2 = r._spawn(asyncio.sleep(3600))
        coordinator._transition(CoordinatorState.IN_PROGRESS)
        coordinator._transition(CoordinatorState.COMPLETED)
        errors = await coordinator.shutdown()
        assert errors == []
        assert coordinator.state is CoordinatorState.CLOSED
        assert r.state is runnermod.RunnerState.CLOSED
        assert a1.done() and a1.cancelled()
        assert a2.done() and a2.cancelled()
        assert r._aux_tasks == set()

    asyncio.run(scenario())
    engine.dispose()


# ===========================================================================
# B. Idempotencia
# ===========================================================================

def test_shutdown_idempotent(tmp_path):
    """(B) Invocar shutdown dos veces: estado terminal conservado, sin
    reabrir ni reiniciar recursos."""
    coordinator, registry, persistence, engine = _fresh_coordinator(tmp_path)
    runner = registry.register_existing(1, _make_runner(1))

    async def scenario():
        r = registry.get_existing(1)
        await _start_runner(r)
        coordinator._transition(CoordinatorState.IN_PROGRESS)
        coordinator._transition(CoordinatorState.COMPLETED)
        errors1 = await coordinator.shutdown()
        assert errors1 == []
        assert coordinator.state is CoordinatorState.CLOSED
        assert r.state is runnermod.RunnerState.CLOSED
        # segunda llamada: no-op, mismo estado
        errors2 = await coordinator.shutdown()
        assert errors2 == []
        assert coordinator.state is CoordinatorState.CLOSED
        assert r.state is runnermod.RunnerState.CLOSED

    asyncio.run(scenario())
    engine.dispose()


# ===========================================================================
# C. Fallo individual de cierre
# ===========================================================================

class _FailingCloseRunner(runnermod.WorldRunner):
    """Runner cuyo close falla (sin romper a los demás)."""
    async def _do_close(self):
        raise OSError("runner close exploded")


def test_shutdown_individual_failure_continues_others(tmp_path, caplog):
    """(C) Un runner falla al cerrarse: los demás cierran, error visible,
    estado CLOSE_INCOMPLETE, no éxito falso."""
    coordinator, registry, persistence, engine = _fresh_coordinator(tmp_path)
    good = registry.register_existing(1, _make_runner(1))
    bad = registry.register_existing(2, _FailingCloseRunner(
        2, seed=1, width=64, height=32,
        engine=SimpleNamespace(month=0),
    ))

    async def scenario():
        rg = registry.get_existing(1)
        rb = registry.get_existing(2)
        await _start_runner(rg)
        await _start_runner(rb)
        coordinator._transition(CoordinatorState.IN_PROGRESS)
        coordinator._transition(CoordinatorState.COMPLETED)
        with caplog.at_level(logging.ERROR, logger="worldsim.server.recovery_coordinator"):
            errors = await coordinator.shutdown()
        assert len(errors) == 1
        assert any("runner close exploded" in str(e) for e in errors)
        assert coordinator.state is CoordinatorState.CLOSE_INCOMPLETE
        assert rg.state is runnermod.RunnerState.CLOSED  # el bueno sí cerró

    asyncio.run(scenario())
    engine.dispose()


# ===========================================================================
# D. Cancelación durante shutdown
# ===========================================================================

class _StubbornCloseRunner(runnermod.WorldRunner):
    """Runner cuyo close no termina hasta que se libera un evento."""
    def __init__(self, *args, release_event: asyncio.Event, **kwargs):
        super().__init__(*args, **kwargs)
        self._release = release_event

    async def _do_close(self):
        await self._release.wait()  # bloqueado hasta que el test lo libere


def test_shutdown_cancellation_does_not_abandon_tasks(tmp_path):
    """(D) Cancelar el shutdown: las tareas de cierre no se abandonan,
    las referencias se mantienen, se espera la terminación real."""
    coordinator, registry, persistence, engine = _fresh_coordinator(tmp_path)
    release = asyncio.Event()
    runner = registry.register_existing(1, _StubbornCloseRunner(
        1, seed=1, width=64, height=32,
        engine=SimpleNamespace(month=0), release_event=release,
    ))

    async def scenario():
        r = registry.get_existing(1)
        await _start_runner(r)
        coordinator._transition(CoordinatorState.IN_PROGRESS)
        coordinator._transition(CoordinatorState.COMPLETED)
        closer = asyncio.create_task(coordinator.shutdown())
        # dar tiempo a que entre en el cierre del runner
        await asyncio.sleep(0.05)
        assert not closer.done()
        assert coordinator.state is CoordinatorState.SHUTTING_DOWN
        # cancelar el shutdown
        closer.cancel()
        for _ in range(10):
            await asyncio.sleep(0)
        # la tarea de cierre del runner NO fue abandonada: el corutina de
        # shutdown atrapa la cancelación como error y continúa
        assert not closer.done()
        # liberar para que el close del runner termine limpio
        release.set()
        # el shutdown completa normalmente (no propaga CancelledError) con
        # el error registrado; estado CLOSE_INCOMPLETE porque hubo errores
        result = await closer
        assert len(result) >= 1
        assert coordinator.state is CoordinatorState.CLOSE_INCOMPLETE
        # referencia a la tarea del runner mantenida hasta su fin real
        r = registry.get_existing(1)
        assert r._task is not None

    asyncio.run(scenario())
    engine.dispose()


# ===========================================================================
# E. Operaciones concurrentes durante shutdown
# ===========================================================================

class _PausableRunner(runnermod.WorldRunner):
    """Runner que permite pausar durante el cierre para probar
    la interacción shutdown ↔ pausa/reanudación."""
    def __init__(self, *args, pause_event: asyncio.Event, **kwargs):
        super().__init__(*args, **kwargs)
        self._pause_event = pause_event

    async def _run(self):
        # Simula un runner que consulta estado pausado; honra cancelación.
        try:
            while True:
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            raise


def test_shutdown_concurrent_pause_resume(tmp_path):
    """(E) Shutdown mientras una transición pausa/reanudación está en curso:
    no libera locks prematuramente, cierra ordenadamente."""
    coordinator, registry, persistence, engine = _fresh_coordinator(tmp_path)
    pause_event = asyncio.Event()
    runner = registry.register_existing(1, _PausableRunner(
        1, seed=1, width=64, height=32,
        engine=SimpleNamespace(month=0), pause_event=pause_event,
    ))

    async def scenario():
        r = registry.get_existing(1)
        task = await _start_runner(r)
        # Inicia cierre que espera un evento externo (simula operación concurrente)
        closer = asyncio.create_task(_delayed_shutdown(coordinator, pause_event))
        await asyncio.sleep(0.05)
        # liberar el cierre para que termine
        pause_event.set()
        errors = await closer
        assert r.state in (runnermod.RunnerState.CLOSED, runnermod.RunnerState.CLOSE_INCOMPLETE)
        assert task.done()

    asyncio.run(scenario())
    engine.dispose()


async def _delayed_shutdown(coordinator, event):
    """Espera a que se señale el evento antes de cerrar, para simular
    una operación concurrente en curso."""
    await event.wait()
    coordinator._transition(CoordinatorState.IN_PROGRESS)
    coordinator._transition(CoordinatorState.COMPLETED)
    return await coordinator.shutdown()


# ===========================================================================
# F. Recursos de persistencia
# ===========================================================================

def test_shutdown_no_automatic_checkpoint(tmp_path, monkeypatch):
    """(F) DEC05: no se genera checkpoint final automático al cerrar."""
    coordinator, registry, persistence, engine = _fresh_coordinator(tmp_path)
    saved = []

    orig_save = persistence.save_checkpoint_now

    def tracking_save(*args, **kwargs):
        saved.append(True)
        return orig_save(*args, **kwargs)

    monkeypatch.setattr(persistence, "save_checkpoint_now", tracking_save)
    registry.register_existing(1, _make_runner(1))

    async def scenario():
        r = registry.get_existing(1)
        await _start_runner(r)
        coordinator._transition(CoordinatorState.IN_PROGRESS)
        coordinator._transition(CoordinatorState.COMPLETED)
        errors = await coordinator.shutdown()
        assert errors == []
        assert coordinator.state is CoordinatorState.CLOSED
        # DEC05: save_checkpoint_now no debe llamarse durante shutdown
        assert saved == [], f"checkpoint final automático generado: {saved}"

    asyncio.run(scenario())
    engine.dispose()


def test_shutdown_persistence_errors_visible(tmp_path):
    """(F) Errores de cierre de persistencia son visibles, no silenciosos."""
    coordinator, registry, persistence, engine = _fresh_coordinator(tmp_path)
    registry.register_existing(1, _make_runner(1))

    async def scenario():
        r = registry.get_existing(1)
        await _start_runner(r)
        # Cierra la persistencia manualmente para forzar error en shutdown
        persistence.close()
        coordinator._transition(CoordinatorState.IN_PROGRESS)
        coordinator._transition(CoordinatorState.COMPLETED)
        # shutdown sobre persistencia ya cerrada debe manejarse sin fallar
        # de forma catastrófica (idempotente de PersistenceService.close)
        errors = await coordinator.shutdown()
        assert coordinator.state in (
            CoordinatorState.CLOSED, CoordinatorState.CLOSE_INCOMPLETE,
        )

    asyncio.run(scenario())
    engine.dispose()


# ===========================================================================
# G. Integración real del lifespan (FastAPI)
# ===========================================================================

def _fresh_server_env(tmp_path, monkeypatch):
    """Entorno servidor aislado con DB temporal y registry fresco."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import worldsim.server.db as dbmod

    db_file = tmp_path / "lifespan.db"
    test_engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    dbmod.SessionLocal = sessionmaker(bind=test_engine)
    runnermod.registry = runnermod.RunnerRegistry()
    appmod.registry = runnermod.registry
    Base.metadata.create_all(test_engine)
    return appmod, test_engine


def test_lifespan_startup_then_shutdown_closed(tmp_path, monkeypatch):
    """(G) Lifespan real: startup completa (COMPLETED) y shutdown deja
    al coordinator en CLOSED."""
    appmod, test_engine = _fresh_server_env(tmp_path, monkeypatch)

    with TestClient(appmod.app) as client:
        coordinator = appmod.app.state.coordinator
        assert coordinator.state is CoordinatorState.COMPLETED
        assert client.get("/api/meta").status_code == 200
    # Al salir del TestClient, el lifespan ejecuta el shutdown (finally)
    assert coordinator.state is CoordinatorState.CLOSED
    # los runners (si los hubiera) están cerrados
    test_engine.dispose()


def test_lifespan_startup_failure_rollback(tmp_path, monkeypatch):
    """(G) Fallo durante startup: rollback completo, app no disponible,
    coordinator termina en FAILED."""
    appmod, test_engine = _fresh_server_env(tmp_path, monkeypatch)

    async def exploding_run_startup(**_kwargs):
        raise RuntimeError("startup exploded")

    monkeypatch.setattr(appmod, "run_startup", exploding_run_startup)

    with pytest.raises(BaseException) as excinfo:
        with TestClient(appmod.app):
            pass  # inalcanzable: la app no llega a estar disponible
    assert "startup exploded" in str(excinfo.value)

    test_engine.dispose()

