"""Etapa 4 — Transiciones de pausa/reanudación por mundo (DEC08/DEC14/DEC15).

Pruebas deterministas: barreras/eventos (``threading.Event`` dentro del
worker thread de la escritura; nunca sleeps arbitrarios de coordinación),
verificación explícita de orden, identidad y generación, y
``asyncio.sleep`` únicamente como yield de planificación o como VENTANA DE
OBSERVACIÓN acotada del bucle periódico de ticks (un caso negativo —"no
ocurrió ningún tick"— solo puede demostrarse con una ventana; cada uno
lleva su control positivo que demuestra que la ventana es suficiente).
Sin FastAPI (DEC07): la ruta WebSocket y el lifespan que integran estas
transiciones se verifican por separado (py_compile).

Numeración según Etapa 4 §6:
  1  pausa normal
  2  reanudación normal
  3  pausa repetida
  4  reanudación repetida
  5  pausa y reanudación concurrentes sobre el mismo mundo
  6  operaciones simultáneas sobre mundos diferentes
  7  cancelación antes de iniciar la escritura
  8  cancelación mientras la escritura sigue ejecutándose
  9  escritura que termina correctamente después de cancelar al llamador
 10  escritura que falla después de cancelar al llamador
 11  reconciliación antes de permitir una nueva transición
 12  protección frente a operaciones obsoletas
 13  coherencia entre estado persistido, runner y engine
 14  eliminación concurrente con pausa o reanudación
 15  shutdown mientras hay una transición activa
 15b (extra) shutdown cancelado: espera hasta el fin real, sin abandonar
 16  ausencia de ticks mientras el runner está pausado
 17  reanudación sin duplicación de tareas ni ticks
 18  ausencia de tareas auxiliares abandonadas
extra reanudación sin runner rechazada (sin fila contradictoria)

El §6 final también se verifica aquí: un error no deja locks retenidos
ni estados contradictorios silenciosos (tests 10, 12, 14, 15 y 18).
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from worldsim.server import runner as runnermod
from worldsim.server.db import Base, WorldRow
from worldsim.server.runner import (
    RunnerRegistry,
    RunnerState,
    WorldRunner,
    safe_delete_world,
)
from worldsim.server.transitions import (
    TransitionCoordinator,
    TransitionError,
    WorldNotFoundError,
    pause_world,
    resume_world,
)

W_A = 7   # mundo bajo prueba (fixture)
W_B = 8   # segundo mundo (test 6)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Engine:
    """Motor ligero con contador de ticks (sin worldgen, sin coste)."""

    def __init__(self) -> None:
        self.month = 0
        self.paused = False
        self.ticks = 0

    def tick(self) -> None:
        self.ticks += 1
        self.month += 1


@pytest.fixture()
def db(tmp_path):
    """Base SQLite por archivo con las filas de los mundos A y B."""
    engine = create_engine(f"sqlite:///{tmp_path / 'e4.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as s:
        for wid, seed in ((W_A, 1), (W_B, 2)):
            s.add(
                WorldRow(
                    id=wid, seed=seed, width=64, height=32,
                    months_simulated=0, land_pct=0.0, state_json="{}",
                    created_at="2026-01-01", status="running",
                )
            )
        s.commit()
    yield factory
    engine.dispose()


@pytest.fixture()
def coord():
    return TransitionCoordinator()


@pytest.fixture()
def registry():
    return RunnerRegistry()


@pytest.fixture()
def runner(registry):
    r = WorldRunner(W_A, seed=1, width=64, height=32, engine=_Engine())
    registry.register_existing(W_A, r)
    return r


def _status(db, wid: int = W_A) -> str | None:
    with db() as s:
        row = s.get(WorldRow, wid)
    return None if row is None else row.status


def _deleted_at(db, wid: int = W_A):
    with db() as s:
        row = s.get(WorldRow, wid)
    return None if row is None else row.deleted_at


def _set_status(db, wid: int, status: str) -> None:
    with db() as s:
        s.get(WorldRow, wid).status = status
        s.commit()


def _force_running(db, runner, wid: int = W_A) -> None:
    """Given: mundo en ejecución (fila, runner y espejo del engine)."""
    _set_status(db, wid, "running")
    runner.paused = False
    runner.eng.paused = False


def _force_paused(db, runner, wid: int = W_A) -> None:
    """Given: mundo pausado (fila, runner y espejo del engine)."""
    _set_status(db, wid, "paused")
    runner.paused = True
    runner.eng.paused = True


def _assert_coherent(db, runner, status: str, wid: int = W_A) -> None:
    """Then: persistido == operativo == espejo (§4.3/§6.13)."""
    assert _status(db, wid) == status
    expected = status == "paused"
    assert runner.paused is expected
    assert runner.eng.paused is expected


class _Gate:
    """Barrera determinista dentro del worker thread de la escritura.

    Bloquea ANTES de tocar la base de datos (en la apertura de la sesión),
    de modo que la transacción no retiene ningún lock de SQLite y una
    eliminación concurrente puede completarse sin contención.  El timeout
    de 30 s es una red de seguridad contra errores del test (un test que
    olvida liberar falla ruidosamente, nunca se cuelga).
    """

    def __init__(self) -> None:
        self.reached = threading.Event()   # la escritura llegó al punto
        self.release = threading.Event()   # el test la deja continuar
        self.fail: BaseException | None = None  # si se lanza, la escritura falla
        self.opens = 0


class _GatedFactory:
    """session_factory que atraviesa un :class:`_Gate` antes de abrir sesión."""

    def __init__(self, real, gate: _Gate) -> None:
        self._real = real
        self._gate = gate

    def __call__(self):
        g = self._gate
        g.opens += 1
        g.reached.set()
        if not g.release.wait(timeout=30):
            raise AssertionError("gate never released (test bug)")
        if g.fail is not None:
            raise g.fail
        return self._real()


class _FailingFactory:
    """session_factory cuya apertura falla de inmediato (sin barrera)."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __call__(self):
        raise self._exc


async def _await_gate(gate: _Gate, timeout: float = 5.0) -> None:
    """Espera a que la escritura llegue a la barrera (evento, no sleep)."""
    ok = await asyncio.to_thread(gate.reached.wait, timeout)
    assert ok, "the write never reached the gate"


async def _wait_ticks(engine: _Engine, n: int, timeout: float = 3.0) -> None:
    """Espera acotada y positiva a que la ruta ordinaria tickee."""
    deadline = time.monotonic() + timeout
    while engine.ticks < n:
        assert time.monotonic() < deadline, (
            f"expected {n} ticks, saw {engine.ticks}"
        )
        await asyncio.sleep(0.005)


def _quiet_ticks(monkeypatch) -> None:
    """Neutraliza resolución de profecías y broadcasts para observar SOLO
    la ruta de ticks (DEC07: app.py no es importable sin fastapi)."""
    monkeypatch.setattr(runnermod, "resolve_pending", lambda wid, eng: None)
    monkeypatch.setattr(WorldRunner, "_broadcast_tick", lambda self: None)
    monkeypatch.setattr(WorldRunner, "_broadcast_league_scores", lambda self: None)
    monkeypatch.setattr(WorldRunner, "_check_season_end", lambda self: None)


# ---------------------------------------------------------------------------
# 1-4: transiciones básicas
# ---------------------------------------------------------------------------


def test_01_pausa_normal(db, registry, runner, coord):
    """(1) Pausa normal: persistido + operativo + espejo, generación 1."""
    _force_running(db, runner)

    async def scenario():
        res = await pause_world(
            W_A, session_factory=db, registry=registry, coordinator=coord,
        )
        assert res.world_id == W_A
        assert res.status == "paused"
        assert res.generation == 1
        assert res.applied is True
        assert coord.generation(W_A) == 1
        assert coord.applied_generation(W_A) == 1

    asyncio.run(scenario())
    _assert_coherent(db, runner, "paused")
    assert not coord._lock_for(W_A).locked()


def test_02_reanudacion_normal(db, registry, runner, coord):
    """(2) Reanudación normal: los tres estados vuelven a 'running'."""
    _force_paused(db, runner)

    async def scenario():
        res = await resume_world(
            W_A, session_factory=db, registry=registry, coordinator=coord,
        )
        assert res.status == "running"
        assert res.generation == 1
        assert res.applied is True

    asyncio.run(scenario())
    _assert_coherent(db, runner, "running")
    assert not coord._lock_for(W_A).locked()


def test_03_pausa_repetida(db, registry, runner, coord):
    """(3) Pausa repetida: idempotente, sin errores ni estados a medias.

    Las repeticiones encuentran la fila ya en el destino (la escritura se
    omite) pero la puerta de aplicado sigue verificando la coherencia.
    """
    _force_running(db, runner)

    async def scenario():
        for gen in (1, 2, 3):
            res = await pause_world(
                W_A, session_factory=db, registry=registry, coordinator=coord,
            )
            assert res.generation == gen
            assert res.applied is True
            assert res.status == "paused"
            assert _status(db) == "paused"

    asyncio.run(scenario())
    _assert_coherent(db, runner, "paused")
    assert coord.applied_generation(W_A) == 3
    assert coord.reconcile_errors == []


def test_04_reanudacion_repetida(db, registry, runner, coord):
    """(4) Reanudación repetida: idempotente y coherente en cada vuelta."""
    _force_paused(db, runner)

    async def scenario():
        for gen in (1, 2, 3):
            res = await resume_world(
                W_A, session_factory=db, registry=registry, coordinator=coord,
            )
            assert res.generation == gen
            assert res.applied is True
            assert _status(db) == "running"

    asyncio.run(scenario())
    _assert_coherent(db, runner, "running")
    assert coord.applied_generation(W_A) == 3


# ---------------------------------------------------------------------------
# 5-6: concurrencia
# ---------------------------------------------------------------------------


def test_05_concurrentes_mismo_mundo(db, registry, runner, coord):
    """(5) Pausa y reanudación concurrentes sobre el MISMO mundo.

    Serialización por lock FIFO: la reanudación (entró después) espera a
    que la pausa termine de reconciliar, y por ser la más reciente (gen 2)
    es la que controla el estado final (§2/§4.2).
    """
    _force_running(db, runner)
    gate = _Gate()
    gated = _GatedFactory(db, gate)

    async def scenario():
        pause_task = asyncio.create_task(
            pause_world(W_A, session_factory=gated,
                        registry=registry, coordinator=coord)
        )
        await _await_gate(gate)  # la pausa retiene el lock, escritura en vuelo
        resume_task = asyncio.create_task(
            resume_world(W_A, session_factory=db,
                         registry=registry, coordinator=coord)
        )
        await asyncio.sleep(0)  # yield: la reanudación queda en `lock.acquire`
        assert not resume_task.done()  # serialización por mundo (§4.1)
        gate.release.set()
        res_pause = await pause_task
        res_resume = await resume_task
        assert (res_pause.generation, res_pause.applied) == (1, True)
        assert (res_resume.generation, res_resume.applied) == (2, True)
        # la operación más reciente es la que prevalece (§2)
        assert coord.applied_generation(W_A) == 2
        assert coord.generation(W_A) == 2

    asyncio.run(scenario())
    _assert_coherent(db, runner, "running")
    assert coord._writes == set()


def test_06_mundos_diferentes_simultaneos(db, registry, runner, coord):
    """(6) Operaciones sobre mundos distintos NO se bloquean entre sí.

    El mundo B completa su pausa mientras la escritura del mundo A sigue
    bloqueada en su barrera: no existe lock global (§4.1).
    """
    runner_b = WorldRunner(W_B, seed=2, width=64, height=32, engine=_Engine())
    registry.register_existing(W_B, runner_b)
    _force_running(db, runner)
    _force_running(db, runner_b, W_B)
    gate = _Gate()
    gated = _GatedFactory(db, gate)

    async def scenario():
        task_a = asyncio.create_task(
            pause_world(W_A, session_factory=gated,
                        registry=registry, coordinator=coord)
        )
        await _await_gate(gate)  # mundo A bloqueado en su escritura
        res_b = await asyncio.wait_for(
            pause_world(W_B, session_factory=db,
                        registry=registry, coordinator=coord),
            timeout=5.0,  # si hubiera lock global, expira y el test falla
        )
        assert res_b.applied is True
        gate.release.set()
        res_a = await task_a
        assert res_a.applied is True

    asyncio.run(scenario())
    _assert_coherent(db, runner, "paused")
    _assert_coherent(db, runner_b, "paused", W_B)


# ---------------------------------------------------------------------------
# 7-11: cancelación y reconciliación (§4.5)
# ---------------------------------------------------------------------------


def test_07_cancelacion_antes_de_iniciar_la_escritura(db, registry, runner, coord):
    """(7) Cancelación antes de iniciar la escritura: nada se escribió.

    La operación cancelada quedó esperando el lock del mundo (nunca abrió
    sesión); su ``begin``/``end`` quedan balanceados y el lock se libera.
    """
    _force_running(db, runner)
    gate = _Gate()
    gated = _GatedFactory(db, gate)

    async def scenario():
        hold = asyncio.create_task(
            pause_world(W_A, session_factory=gated,
                        registry=registry, coordinator=coord)
        )
        await _await_gate(gate)  # la primera transición retiene el lock
        queued = asyncio.create_task(
            pause_world(W_A, session_factory=db,
                        registry=registry, coordinator=coord)
        )
        await asyncio.sleep(0)  # queda esperando el lock, SIN escritura
        assert not queued.done()
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert queued.cancelled()
        assert gate.opens == 1  # solo la primera transición abrió sesión
        gate.release.set()
        res = await hold
        assert res.applied is True
        assert coord._active == 0  # begin/end balanceados (§4.1)
        assert not coord._lock_for(W_A).locked()

    asyncio.run(scenario())
    _assert_coherent(db, runner, "paused")
    assert coord.reconcile_errors == []


def test_08_cancelacion_mientras_la_escritura_sigue_ejecutandose(
    db, registry, runner, coord,
):
    """(8) Cancelación con la escritura EN EJECUCIÓN (§4.5.1-2).

    ``task.cancel()`` no termina el trabajo del worker thread: la
    operación espera su finalización REAL (shield ≠ completion), la
    reconcilia y SOLO ENTONCES propaga la cancelación original.  No queda
    ningún task de escritura huérfano (regla 5).
    """
    _force_running(db, runner)
    gate = _Gate()
    gated = _GatedFactory(db, gate)

    async def scenario():
        task = asyncio.create_task(
            pause_world(W_A, session_factory=gated,
                        registry=registry, coordinator=coord)
        )
        await _await_gate(gate)  # escritura en ejecución en su worker thread
        task.cancel()
        await asyncio.sleep(0)  # la cancelación se entrega al esperar
        assert not task.done()  # la escritura sigue viva: cancel() no la paró
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task  # §4.5.6: la cancelación ORIGINAL, tras reconciliar
        assert coord._writes == set()  # sin tasks de escritura abandonados

    asyncio.run(scenario())
    # resultado efectivo de la escritura (confirmó) reconciliado con lo
    # operativo antes de liberar el lock: nada contradictorio
    _assert_coherent(db, runner, "paused")


def test_09_escritura_correcta_tras_cancelar_al_llamador(
    db, registry, runner, coord,
):
    """(9) La escritura termina bien después de cancelar al llamador.

    El resultado efectivo manda: la fila queda en el destino y el estado
    operativo se reconcilió con ella; sin errores ocultos (no hubo ninguno)
    y la siguiente transición puede ejecutarse de inmediato.
    """
    _force_running(db, runner)
    gate = _Gate()
    gated = _GatedFactory(db, gate)

    async def scenario():
        task = asyncio.create_task(
            pause_world(W_A, session_factory=gated,
                        registry=registry, coordinator=coord)
        )
        await _await_gate(gate)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        # resultado efectivo: la escritura confirmó y quedó aplicada
        assert _status(db) == "paused"
        assert runner.paused is True and runner.eng.paused is True
        assert coord.reconcile_errors == []  # ningún fallo que ocultar
        # el lock ya está libre y el estado reconciliado: otra transición
        # procede de inmediato (§4.5.5)
        res = await resume_world(
            W_A, session_factory=db, registry=registry, coordinator=coord,
        )
        assert res.applied is True
        assert coord._writes == set()

    asyncio.run(scenario())
    _assert_coherent(db, runner, "running")


def test_10_escritura_falla_tras_cancelar_al_llamador(db, registry, runner, coord):
    """(10) La escritura FALLA después de cancelar al llamador (§4.5).

    El error es invisible para el llamador cancelado: se conserva en
    ``reconcile_errors`` + log (nunca en silencio).  La fila y lo operativo
    quedan intactos (el commit nunca ocurrió) y el error NO retiene el
    lock: la siguiente transición procede.
    """
    _force_running(db, runner)
    gate = _Gate()
    gate.fail = sqlite3.OperationalError("disk full")
    gated = _GatedFactory(db, gate)

    async def scenario():
        task = asyncio.create_task(
            pause_world(W_A, session_factory=gated,
                        registry=registry, coordinator=coord)
        )
        await _await_gate(gate)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        # el fallo original quedó registrado (visible para la auditoría)
        assert len(coord.reconcile_errors) == 1
        assert isinstance(coord.reconcile_errors[0], sqlite3.OperationalError)
        assert coord._writes == set()
        # sin escritura confirmada: fila y operativos intactos, coherentes
        assert _status(db) == "running"
        assert runner.paused is False and runner.eng.paused is False
        # el lock no quedó retenido por el error (§6 final)
        assert not coord._lock_for(W_A).locked()
        res = await pause_world(
            W_A, session_factory=db, registry=registry, coordinator=coord,
        )
        assert res.applied is True

    asyncio.run(scenario())
    _assert_coherent(db, runner, "paused")


def test_11_reconciliacion_antes_de_permitir_nueva_transicion(
    db, registry, runner, coord,
):
    """(11) La reconciliación ocurre ANTES de liberar el lock (§4.5.5).

    Mientras la pausa cancelada no termine de reconciliar su escritura,
    la reanudación concurrente NO puede entrar; recién entonces obtiene el
    lock y observa un estado ya coherente.
    """
    _force_running(db, runner)
    gate = _Gate()
    gated = _GatedFactory(db, gate)

    async def scenario():
        pause_task = asyncio.create_task(
            pause_world(W_A, session_factory=gated,
                        registry=registry, coordinator=coord)
        )
        await _await_gate(gate)
        pause_task.cancel()
        await asyncio.sleep(0)  # la pausa drena su escritura con el lock puesto
        resume_task = asyncio.create_task(
            resume_world(W_A, session_factory=db,
                         registry=registry, coordinator=coord)
        )
        await asyncio.sleep(0)  # la reanudación queda esperando el lock
        # mientras no reconcilice, la reanudación no puede haber entrado
        assert not resume_task.done()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await pause_task
        res = await resume_task  # recién entonces corre, sobre estado reconciliado
        assert res.applied is True
        assert coord._writes == set()
        assert coord.reconcile_errors == []

    asyncio.run(scenario())
    _assert_coherent(db, runner, "running")


# ---------------------------------------------------------------------------
# 12-13: obsoletas y coherencia
# ---------------------------------------------------------------------------


def test_12_proteccion_frente_a_operaciones_obsoletas(
    db, registry, runner, coord,
):
    """(12) Una operación antigua no altera el estado de una más reciente.

    Nivel público: la gen 2 (reanudación) es la aplicada.  Nivel de puerta
    (blanco, como en tests de Etapa 2 que usan ``runner._closed``): un
    replay de la gen 1 contra ``_apply_in_memory`` se rechaza y no toca
    runner, espejo ni generación aplicada.
    """
    _force_running(db, runner)

    async def scenario():
        r1 = await pause_world(
            W_A, session_factory=db, registry=registry, coordinator=coord,
        )
        r2 = await resume_world(
            W_A, session_factory=db, registry=registry, coordinator=coord,
        )
        assert (r1.generation, r2.generation) == (1, 2)
        assert coord.applied_generation(W_A) == 2

        # replay de la gen 1 (obsoleta) con la semántica de la pausa:
        accepted = coord._apply_in_memory(W_A, 1, "paused", registry)
        assert accepted is False
        assert runner.paused is False          # la gen 2 sigue mandando
        assert runner.eng.paused is False
        assert coord.applied_generation(W_A) == 2  # sin retroceso

        # con estado "sentinela" distinto: tampoco lo altera
        runner.paused = True
        accepted = coord._apply_in_memory(W_A, 1, "running", registry)
        assert accepted is False
        assert runner.paused is True           # la obsoleta no aplicó
        runner.paused = False

    asyncio.run(scenario())
    _assert_coherent(db, runner, "running")


def test_13_coherencia_persistido_runner_engine(db, registry, runner, coord):
    """(13) Fila, runner y espejo del engine coinciden en cada transición."""
    _force_running(db, runner)

    async def scenario():
        await pause_world(
            W_A, session_factory=db, registry=registry, coordinator=coord,
        )
        _assert_coherent(db, runner, "paused")
        await resume_world(
            W_A, session_factory=db, registry=registry, coordinator=coord,
        )
        _assert_coherent(db, runner, "running")
        await pause_world(
            W_A, session_factory=db, registry=registry, coordinator=coord,
        )
        _assert_coherent(db, runner, "paused")
        await resume_world(
            W_A, session_factory=db, registry=registry, coordinator=coord,
        )
        _assert_coherent(db, runner, "running")

    asyncio.run(scenario())
    assert coord.reconcile_errors == []


# ---------------------------------------------------------------------------
# 14: eliminación concurrente
# ---------------------------------------------------------------------------


def test_14_eliminacion_concurrente(db, registry, runner, coord):
    """(14) Eliminación concurrente con pausa/reanudación (§4.6).

    Tres fases, todas deterministas:
      (a) la marca ``begin_delete`` rechaza transiciones nuevas y nada
          cambia; tras ``end_delete`` vuelven a permitirse;
      (b) la eliminación comienza DURANTE la escritura → la puerta rechaza,
          la fila se revierte al valor previo y el operativo queda intacto;
      (c) ``safe_delete_world`` completa su protocolo con una escritura en
          vuelo → la escritura encuentra la fila ausente
          (``WorldNotFoundError``) y no toca nada.
    """
    _force_running(db, runner)

    async def scenario():
        # -- fase (a): marca de eliminación activa ------------------------
        registry.begin_delete(W_A)
        with pytest.raises(TransitionError, match="being deleted"):
            await pause_world(
                W_A, session_factory=db, registry=registry, coordinator=coord,
            )
        assert _status(db) == "running"  # nada cambió por el rechazo
        assert runner.paused is False
        registry.end_delete(W_A)
        res = await pause_world(
            W_A, session_factory=db, registry=registry, coordinator=coord,
        )
        assert res.applied is True  # tras end_delete vuelve a permitirse

        # -- fase (b): eliminación comienza durante la escritura ----------
        gate = _Gate()
        gated = _GatedFactory(db, gate)
        resume_task = asyncio.create_task(
            resume_world(W_A, session_factory=gated,
                         registry=registry, coordinator=coord)
        )
        await _await_gate(gate)  # escritura de la reanudación en vuelo
        registry.begin_delete(W_A)  # marca SINCRÓNICA durante la escritura
        gate.release.set()
        res = await resume_task
        assert res.applied is False  # la puerta rechazó el aplicado (§4.6)
        # reconciliación: la fila volvió al previo y el operativo intacto
        assert _status(db) == "paused"
        assert runner.paused is True and runner.eng.paused is True
        registry.end_delete(W_A)

        # -- fase (c): safe_delete_world completa con escritura en vuelo ---
        gate2 = _Gate()
        gated2 = _GatedFactory(db, gate2)
        pause_task = asyncio.create_task(
            pause_world(W_A, session_factory=gated2,
                        registry=registry, coordinator=coord)
        )
        await _await_gate(gate2)  # validaciones pasadas; aún sin tocar la BD
        await asyncio.wait_for(
            safe_delete_world(W_A, session_factory=db, registry=registry),
            timeout=10.0,  # sin contención: la transición no retiene locks
        )
        gate2.release.set()
        with pytest.raises(WorldNotFoundError):
            await pause_task  # la fila ya no existe: nada que aplicar
        # DEC10/F7-E.2-I: eliminación lógica; se conserva la fila y su
        # último estado, pero queda marcada y fuera del registro operativo.
        assert _status(db) == "paused"
        assert _deleted_at(db) is not None
        assert registry.get_existing(W_A) is None
        assert runner.state is RunnerState.CLOSED  # protocolo DEC10 intacto
        assert coord._writes == set()
        assert not coord._lock_for(W_A).locked()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 15/15b: shutdown
# ---------------------------------------------------------------------------


def test_15_shutdown_durante_transicion_activa(db, registry, runner, coord):
    """(15) Shutdown con una transición activa (§4.6).

    El shutdown rechaza transiciones nuevas desde su primer instante y
    ESPERA a la activa (que termina completa, con su escritura
    reconciliada); recién entonces vuelve.  Sin locks retenidos.
    """
    _force_running(db, runner)
    gate = _Gate()
    gated = _GatedFactory(db, gate)

    async def scenario():
        pause_task = asyncio.create_task(
            pause_world(W_A, session_factory=gated,
                        registry=registry, coordinator=coord)
        )
        await _await_gate(gate)
        shutdown_task = asyncio.create_task(coord.shutdown())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # determinista: mientras _active > 0 el shutdown no puede terminar
        assert coord._active == 1
        assert not shutdown_task.done()
        # nuevas transiciones rechazadas desde el inicio del shutdown
        with pytest.raises(TransitionError, match="shutdown"):
            await pause_world(
                W_A, session_factory=db, registry=registry, coordinator=coord,
            )
        gate.release.set()
        res = await pause_task  # la transición en curso termina completa
        assert res.applied is True
        await shutdown_task  # y recién entonces el shutdown vuelve
        assert coord._accepting is False
        with pytest.raises(TransitionError, match="shutdown"):
            await resume_world(
                W_A, session_factory=db, registry=registry, coordinator=coord,
            )
        assert not coord._lock_for(W_A).locked()
        assert coord._writes == set()

    asyncio.run(scenario())
    _assert_coherent(db, runner, "paused")


def test_15b_shutdown_cancelado_espera_hasta_el_fin_real(
    db, registry, runner, coord,
):
    """(extra/15) Shutdown cancelado: conserva la espera, no abandona la
    escritura (§4.5 aplicado al shutdown) y propaga su cancelación."""
    _force_running(db, runner)
    gate = _Gate()
    gated = _GatedFactory(db, gate)

    async def scenario():
        pause_task = asyncio.create_task(
            pause_world(W_A, session_factory=gated,
                        registry=registry, coordinator=coord)
        )
        await _await_gate(gate)
        shutdown_task = asyncio.create_task(coord.shutdown())
        await asyncio.sleep(0)
        assert not shutdown_task.done()
        shutdown_task.cancel()  # cancelan al llamador del shutdown
        gate.release.set()
        res = await pause_task  # la transición activa termina completa igual
        assert res.applied is True
        with pytest.raises(asyncio.CancelledError):
            await shutdown_task  # ...y el shutdown propaga su cancelación
        assert coord._active == 0
        assert coord._writes == set()  # ninguna escritura abandonada
        assert coord._accepting is False  # las nuevas siguen rechazadas

    asyncio.run(scenario())
    _assert_coherent(db, runner, "paused")


# ---------------------------------------------------------------------------
# 16-17: ticks por la ruta ordinaria
# ---------------------------------------------------------------------------


def test_16_sin_ticks_mientras_runner_pausado(
    db, registry, runner, coord, monkeypatch,
):
    """(16) Mientras el runner está pausado, NO se ejecuta ningún tick.

    El bucle ``_run`` (única ruta de ticks, DEC06) se observa durante una
    ventana acotada de ~30 iteraciones (PUSH_INTERVAL reducido a 5 ms): el
    control positivo previo demuestra que la ventana es suficiente para
    tickear, y tras la pausa el contador no se mueve (§4.3).
    """
    _quiet_ticks(monkeypatch)
    monkeypatch.setattr(runnermod, "PUSH_INTERVAL", 0.005)
    engine = runner.eng
    _force_running(db, runner)

    async def scenario():
        task = await runner.start()
        try:
            # control positivo: la ruta ordinaria tickea con el runner libre
            await _wait_ticks(engine, 1, timeout=3.0)
            await pause_world(
                W_A, session_factory=db, registry=registry, coordinator=coord,
            )
            _assert_coherent(db, runner, "paused")
            # ventana de observación del bucle (~30 iteraciones): ningún
            # tick puede ocurrir bajo `if self.paused: continue`
            ticks_at_pause = engine.ticks
            await asyncio.sleep(0.15)
            assert engine.ticks == ticks_at_pause
            # reanudar: los ticks vuelven por la MISMA ruta ordinaria
            await resume_world(
                W_A, session_factory=db, registry=registry, coordinator=coord,
            )
            _assert_coherent(db, runner, "running")
            await _wait_ticks(engine, ticks_at_pause + 1, timeout=3.0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())


def test_17_reanudacion_sin_duplicacion_de_tareas_ni_ticks(
    db, registry, runner, coord, monkeypatch,
):
    """(17) Reanudar no duplica tareas, no llama a ``start()`` y reanuda
    los ticks por la MISMA tarea principal (§4.4)."""
    _quiet_ticks(monkeypatch)
    monkeypatch.setattr(runnermod, "PUSH_INTERVAL", 0.005)
    engine = runner.eng
    _force_running(db, runner)

    # espía de start(): la reanudación jamás debe invocarlo
    start_calls: list[int] = []
    original_start = WorldRunner.start

    async def spy_start(self):
        start_calls.append(self.world_id)
        return await original_start(self)

    monkeypatch.setattr(WorldRunner, "start", spy_start)

    async def scenario():
        t1 = await runner.start()
        start_calls.clear()  # solo importa lo que pase tras la pausa
        await _wait_ticks(engine, 1, timeout=3.0)
        await pause_world(
            W_A, session_factory=db, registry=registry, coordinator=coord,
        )
        ticks_frozen = engine.ticks
        await asyncio.sleep(0.1)
        assert engine.ticks == ticks_frozen  # pausa real, sin ticks

        before = set(asyncio.all_tasks())
        await resume_world(
            W_A, session_factory=db, registry=registry, coordinator=coord,
        )
        after = set(asyncio.all_tasks())
        assert after == before  # ni tareas nuevas ni auxiliares creadas
        assert start_calls == []  # jamás se llamó a start() (§4.4)
        assert runner._task is t1 and not t1.done()  # MISMA tarea principal
        # los ticks continúan por esa única tarea (sin duplicación)
        await _wait_ticks(engine, ticks_frozen + 1, timeout=3.0)
        assert runner._task is t1

        task = t1
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 18: nada abandonado
# ---------------------------------------------------------------------------


def test_18_sin_tareas_auxiliares_abandonadas(db, registry, runner, coord):
    """(18) Tras cancelaciones, fallos de escritura y un shutdown
    cancelado: sin tasks huérfanos, sin locks retenidos, sin errores
    invisibles y sin tareas del runner creadas por las transiciones."""
    _force_running(db, runner)

    async def scenario():
        # (i) cancelación con escritura en vuelo que completa tras cancelar
        gate1 = _Gate()
        gated1 = _GatedFactory(db, gate1)
        t1 = asyncio.create_task(
            pause_world(W_A, session_factory=gated1,
                        registry=registry, coordinator=coord)
        )
        await _await_gate(gate1)
        t1.cancel()
        await asyncio.sleep(0)
        gate1.release.set()
        with pytest.raises(asyncio.CancelledError):
            await t1

        # (ii) escritura que falla SIN cancelación: error visible para su
        # llamador (por eso NO entra en reconcile_errors), lock liberado
        failing = _FailingFactory(sqlite3.OperationalError("disk full"))
        with pytest.raises(sqlite3.OperationalError, match="disk full"):
            await pause_world(
                W_A, session_factory=failing,
                registry=registry, coordinator=coord,
            )
        assert not coord._lock_for(W_A).locked()

        # (iii) shutdown cancelado mientras espera una transición activa
        gate3 = _Gate()
        gated3 = _GatedFactory(db, gate3)
        t3 = asyncio.create_task(
            resume_world(W_A, session_factory=gated3,
                         registry=registry, coordinator=coord)
        )
        await _await_gate(gate3)
        sd = asyncio.create_task(coord.shutdown())
        await asyncio.sleep(0)
        sd.cancel()
        gate3.release.set()
        res3 = await t3  # la transición termina completa y reconciliada
        assert res3.applied is True
        with pytest.raises(asyncio.CancelledError):
            await sd

        # §6: nada abandonado, nada retenido, nada contradictorio
        assert coord._writes == set()  # sin tasks de escritura huérfanos
        assert coord._active == 0 and coord._idle.is_set()
        assert not coord._lock_for(W_A).locked()
        # ningún error quedó invisible: (ii) fue visible para su llamador
        assert coord.reconcile_errors == []
        pending = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task()
        ]
        assert pending == []  # sin tareas vivas abandonadas
        assert runner._task is None  # las transiciones nunca crean tareas
        assert runner._aux_tasks == set()

    asyncio.run(scenario())
    _assert_coherent(db, runner, "running")


# ---------------------------------------------------------------------------
# Extra: reanudación sin runner
# ---------------------------------------------------------------------------


def test_extra_reanudacion_sin_runner_rechazada(db, coord):
    """Reanudar un mundo sin runner se rechaza (no se persiste 'running'
    sin nadie que ejecute ticks) y la pausa sin runner es solo de fila."""
    bare_registry = RunnerRegistry()  # sin runners registrados

    async def scenario():
        with pytest.raises(TransitionError, match="without a runner"):
            await resume_world(
                W_A, session_factory=db, registry=bare_registry,
                coordinator=coord,
            )
        assert _status(db) == "running"  # el rechazo no tocó la fila
        assert not coord._lock_for(W_A).locked()
        # la pausa, en cambio, es válida como transición solo de fila
        res = await pause_world(
            W_A, session_factory=db, registry=bare_registry,
            coordinator=coord,
        )
        assert res.applied is True
        assert _status(db) == "paused"

    asyncio.run(scenario())
    assert coord.reconcile_errors == []
