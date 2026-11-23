"""Etapa 2 — Eliminación segura de mundos (DEC10, §11 "Eliminación de mundos": 1-10).

Pruebas deterministas sobre ``safe_delete_world`` y las garantías de
coordinación del ``RunnerRegistry``: eventos/barreras (nunca sleeps
arbitrarios), verificación exacta de orden (close → unregister → persist) y
bloqueo del commit con ``threading.Event`` para las fases de cancelación.
Sin FastAPI (DEC07): la ruta HTTP que traduce a estos resultados es
``app.delete_world`` y se verifica por separado (py_compile).

Numeración según Etapa 2 §11:
  1 eliminación sin runner
  2 eliminación con runner activo
  3 el runner se cierra antes del marcado lógico persistente
  4 fallo del cierre: persistencia intacta
  5 fallo de persistencia después del cierre
  6 dos eliminaciones simultáneas
  7 eliminación concurrente con acceso al runner
  8 no se crea un runner nuevo mientras el mundo se elimina
  9 cancelación durante el cierre
  10 cancelación durante la persistencia de la marca lógica

F7-E.2-I Bloque 2c — pruebas de comportamiento de la eliminación lógica:
 11 (2c.1) la fila permanece en el almacén y deleted_at queda establecido
 12 (2c.2) repetición idempotente: la primera marca no cambia jamás
 13 (2c.3) una marca previa no se sobrescribe
 14 (2c.4) timestamp generado consciente de zona horaria y en UTC
 15 (2c.5) fila físicamente inexistente → WorldNotFoundError, sin commit
 16 (2c.6) fallo de commit: excepción propagada, sin marca persistida
 17 (2c.7) cancelación con el worker real de persistencia en vuelo
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import worldsim.server.runner as runnermod
from worldsim.server.runner import (
    RunnerRegistry,
    RunnerState,
    WorldNotFoundError,
    WorldRunner,
    safe_delete_world,
)


# ---------------------------------------------------------------------------
# Helpers: fakes deterministas de persistencia
# ---------------------------------------------------------------------------

class _FakeRow:
    def __init__(self, world_id: int) -> None:
        self.id = world_id
        self.owner_user_id = None
        self.deleted_at = None  # eliminación lógica: None = mundo activo


class _DeleteHarness:
    """Session-factory double over an in-memory ``store`` dict.

    ``events`` records the delete-flow milestones (order assertions);
    ``ops`` records get/mark calls.  ``commit_probe`` runs INSIDE commit
    on the worker thread (order/state assertions), ``commit_started``/
    ``commit_release`` block the worker thread deterministically,
    ``commit_second`` fires on the SECOND commit arrival (barrier for the
    in-flight-worker scenario, 2c.7), and ``commit_error`` makes
    persistence fail AFTER the close (§11.5).
    """

    def __init__(self) -> None:
        self.store: dict[int, _FakeRow] = {}
        self.events: list[str] = []
        self.ops: list[tuple] = []
        self.commit_probe = None          # callable, runs on the worker thread
        self.commit_error: BaseException | None = None
        self.commit_started: threading.Event | None = None
        self.commit_release: threading.Event | None = None
        self.commit_second: threading.Event | None = None  # 2ª llegada (2c.7)
        self.commit_arrivals = 0          # commits vistos (barrera 2c.7)

    def __call__(self) -> "_FakeSession":
        return _FakeSession(self)

    def on_commit(self) -> None:
        if self.commit_probe is not None:
            self.commit_probe()
        self.commit_arrivals += 1
        if self.commit_started is not None:
            self.commit_started.set()
        if self.commit_arrivals == 2 and self.commit_second is not None:
            self.commit_second.set()
        if self.commit_release is not None:
            released = self.commit_release.wait(timeout=10)
            assert released, "commit was never released by the test"
        if self.commit_error is not None:
            raise self.commit_error  # rollback: la marca lógica no persiste


class _FakeSession:
    """Logical-delete session double: the mark persists, rows are NEVER popped.

    The real ``_delete_world_row`` assigns ``row.deleted_at`` directly on the
    object returned by ``get()`` (SQLAlchemy sets the attribute immediately
    and emits the UPDATE at commit).  This double models the session life
    cycle around that:

    * ``get()`` snapshots the row's mark (the undo point);
    * ``commit()`` persists the logical mark — the row REMAINS in the store;
      the ``("mark", id)`` op is recorded only when the mark actually
      changed (an idempotent repeat records nothing);
    * a failing commit rolls the mark back through ``__exit__``, mirroring
      SQLAlchemy's implicit rollback, so the row stays fully intact.

    There is deliberately NO ``delete()`` method: physical deletion is
    never simulated (F7-E.2-I contract).
    """

    def __init__(self, harness: _DeleteHarness) -> None:
        self._h = harness
        self._undo: tuple[_FakeRow, object] | None = None

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, exc_type, *exc) -> bool:
        if exc_type is not None and self._undo is not None:
            row, previous = self._undo
            row.deleted_at = previous  # rollback: la marca no persiste
        return False

    def get(self, model, world_id: int) -> _FakeRow | None:
        self._h.ops.append(("get", world_id))
        row = self._h.store.get(world_id)
        if row is not None:
            self._undo = (row, row.deleted_at)
        return row

    def commit(self) -> None:
        self._h.events.append("commit")
        self._h.on_commit()  # probe / block / raise (worker thread)
        if self._undo is not None:
            row, previous = self._undo
            if row.deleted_at != previous:
                self._h.ops.append(("mark", row.id))  # marca lógica persistida
            self._undo = None


class _SpyRegistry(RunnerRegistry):
    """Registry recording unregister calls into the shared events list."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def unregister(self, world_id: int) -> WorldRunner | None:
        self.events.append(f"unregister:{world_id}")
        return super().unregister(world_id)


class _StubbornMain:
    """Main-task body that does NOT honour cancellation until released.

    ``cancel_seen`` fires once the close has cancelled it, so tests can
    position themselves deterministically inside ``await runner.close()``.
    """

    def __init__(self) -> None:
        self.cancel_seen = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self) -> None:
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()  # keep running until released


def _runner_for(world_id: int) -> WorldRunner:
    return WorldRunner(
        world_id, seed=1, width=64, height=32,
        engine=SimpleNamespace(month=0),
    )


def _assert_guard_released(reg: RunnerRegistry) -> None:
    assert not reg.is_deleting(7)
    assert reg._delete_users == {}
    assert reg._delete_locks == {}


# ---------------------------------------------------------------------------
# §11.1-5 — sequence and failure semantics
# ---------------------------------------------------------------------------

def test_del_01_delete_without_runner():
    """(1) Eliminación sin runner: validación y marcado lógico persistente."""
    h = _DeleteHarness()
    h.store[7] = _FakeRow(7)
    reg = RunnerRegistry()

    async def scenario():
        await safe_delete_world(7, session_factory=h, registry=reg)
        # la fila PERMANECE almacenada y queda marcada con valor no nulo
        assert 7 in h.store and h.store[7].deleted_at is not None
        assert reg.get_existing(7) is None
        _assert_guard_released(reg)
        assert runnermod._lingering_delete_work == set()  # no held work left

    asyncio.run(scenario())
    assert ("get", 7) in h.ops and ("mark", 7) in h.ops


def test_del_02_delete_with_active_runner():
    """(2) Eliminación con runner activo: cerrado, retirado y marcado."""
    h = _DeleteHarness()
    h.store[7] = _FakeRow(7)
    reg = RunnerRegistry()
    r = _runner_for(7)
    reg.register_existing(7, r)

    async def scenario():
        task = await r.start()
        assert r.state is RunnerState.RUNNING
        await safe_delete_world(7, session_factory=h, registry=reg)
        # runner closed for real, then withdrawn, then the row marked
        assert r.state is RunnerState.CLOSED
        assert task.done() and task.cancelled()
        assert reg.get_existing(7) is None
        assert 7 in h.store and h.store[7].deleted_at is not None
        _assert_guard_released(reg)

    asyncio.run(scenario())


def test_del_03_close_happens_before_persisted_delete():
    """(3) Orden obligatorio (§8): close → unregister → marcado persistente."""
    h = _DeleteHarness()
    h.store[7] = _FakeRow(7)
    events: list[str] = []
    reg = _SpyRegistry(events)
    r = _runner_for(7)
    reg.register_existing(7, r)

    async def scenario():
        await r.start()
        orig_close = r.close

        async def close_recording() -> None:
            events.append("close:start")
            await orig_close()
            events.append("close:done")

        r.close = close_recording  # instance attribute shadows the method

        def probe() -> None:  # runs on the worker thread, INSIDE commit
            events.append("commit")
            assert r.state is RunnerState.CLOSED, \
                "the persisted mark started before the close finished"
            assert reg.get_existing(7) is None, \
                "the runner was still registered when the row was marked"

        h.commit_probe = probe
        await safe_delete_world(7, session_factory=h, registry=reg)
        assert events == ["close:start", "close:done", "unregister:7", "commit"]
        assert 7 in h.store and h.store[7].deleted_at is not None

    asyncio.run(scenario())


def test_del_04_close_failure_keeps_persistence_intact(monkeypatch):
    """(4) Fallo del cierre: persistencia intacta, runner con evidencia."""
    monkeypatch.setattr(runnermod, "CLOSE_TIMEOUT", 0.05)
    h = _DeleteHarness()
    h.store[7] = _FakeRow(7)
    reg = RunnerRegistry()
    r = _runner_for(7)
    reg.register_existing(7, r)
    main = _StubbornMain()

    async def scenario():
        r._run = main.run
        task = await r.start()
        with pytest.raises(RuntimeError, match="did not terminate"):
            await safe_delete_world(7, session_factory=h, registry=reg)
        # persisted data NEVER touched (§8): fila presente y SIN marca lógica
        assert 7 in h.store and h.store[7].deleted_at is None
        assert "commit" not in h.events
        # runner NOT silently unregistered; evidence kept (§8)
        assert reg.get_existing(7) is r
        assert r.state is RunnerState.CLOSE_INCOMPLETE
        assert r._task is task and not task.done()
        _assert_guard_released(reg)  # coordination released on failure
        # retry re-raises the recorded failure: no unsafe repeat, no success
        with pytest.raises(RuntimeError, match="did not terminate"):
            await safe_delete_world(7, session_factory=h, registry=reg)
        assert 7 in h.store and h.store[7].deleted_at is None
        assert reg.get_existing(7) is r
        assert r._close_op is not None and r._close_op.done()
        # release the stubborn task so the loop ends clean
        main.release.set()
        await task

    asyncio.run(scenario())
    assert "commit" not in h.events


def test_del_05_persistence_failure_after_close():
    """(5) Fallo de persistencia después del cierre: error conservado,
    runner cerrado jamás recreado ni reiniciado (§8)."""
    h = _DeleteHarness()
    h.store[7] = _FakeRow(7)
    h.commit_error = RuntimeError("disk full")
    reg = RunnerRegistry()
    r = _runner_for(7)
    reg.register_existing(7, r)

    async def scenario():
        task = await r.start()
        with pytest.raises(RuntimeError, match="disk full"):
            await safe_delete_world(7, session_factory=h, registry=reg)
        # the error is preserved and the mark was rolled back (row intact)
        assert 7 in h.store and h.store[7].deleted_at is None
        # the close happened before the failure and is a fact; the runner
        # stays closed and unregistered — never recreated/restarted (§8)
        assert r.state is RunnerState.CLOSED
        assert r._task is task and task.done()
        assert reg.get_existing(7) is None
        _assert_guard_released(reg)
        # retry (no runner) only retries persistence — no restart ever
        h.commit_error = None
        await safe_delete_world(7, session_factory=h, registry=reg)
        assert 7 in h.store and h.store[7].deleted_at is not None
        assert r._task is task and task.done()  # still the same dead task
        assert reg.get_existing(7) is None      # no runner was recreated

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# §11.6-10 — concurrency and cancellation
# ---------------------------------------------------------------------------

def test_del_06_simultaneous_deletes():
    """(6) Dos eliminaciones simultáneas: serializadas por mundo (§9)."""
    h = _DeleteHarness()
    h.store[7] = _FakeRow(7)
    reg = RunnerRegistry()

    async def scenario():
        first = asyncio.create_task(
            safe_delete_world(7, session_factory=h, registry=reg)
        )
        await asyncio.sleep(0)  # yield: the first caller enters the section
        second = asyncio.create_task(
            safe_delete_world(7, session_factory=h, registry=reg)
        )
        assert await first is None  # wins the race: marks the row
        first_mark = h.store[7].deleted_at  # instante exacto de la primera marca
        # Expectativa adaptada al contrato lógico (F7-E.2-I): sin borrado
        # físico la fila NUNCA desaparece, así que la segunda eliminación no
        # puede recibir WorldNotFoundError por ausencia. Se serializa en el
        # lock del mundo (el shield vive dentro del `async with`) y su UPDATE
        # es un no-op idempotente (DEC-PRE-3) que conserva la primera marca.
        assert await second is None
        assert 7 in h.store and h.store[7].deleted_at is not None
        assert h.store[7].deleted_at is first_mark  # timestamp original intacto
        assert h.events.count("commit") == 2  # marca + no-op idempotente
        assert h.ops.count(("mark", 7)) == 1  # una sola marca NUEVA
        _assert_guard_released(reg)

    asyncio.run(scenario())


def test_del_07_delete_concurrent_with_runner_access():
    """(7) Eliminación concurrente con acceso al runner (§7/§9)."""
    h = _DeleteHarness()
    h.store[7] = _FakeRow(7)
    reg = RunnerRegistry()
    r = _runner_for(7)
    reg.register_existing(7, r)
    main = _StubbornMain()

    async def scenario():
        r._run = main.run
        task = await r.start()
        dele = asyncio.create_task(
            safe_delete_world(7, session_factory=h, registry=reg)
        )
        await main.cancel_seen.wait()  # the deletion is inside close()
        assert reg.is_deleting(7)
        # reads still see the registered runner (never hidden mid-close)
        assert reg.get_existing(7) is r
        # but no operation can obtain it to create/restart anything (§9)
        with pytest.raises(RuntimeError, match="is being deleted"):
            reg.get(7, seed=1, width=64, height=32)
        with pytest.raises(RuntimeError, match="is being deleted"):
            reg.register_existing(7, _runner_for(700))
        assert reg.get_existing(7) is r  # same instance, nothing replaced
        # finish: close → unregister → persist
        main.release.set()
        assert await dele is None
        assert r.state is RunnerState.CLOSED
        assert task.done()
        assert reg.get_existing(7) is None
        assert 7 in h.store and h.store[7].deleted_at is not None
        _assert_guard_released(reg)

    asyncio.run(scenario())


def test_del_08_no_new_runner_while_deleting():
    """(8) No se crea un runner nuevo mientras el mundo se elimina (§9)."""
    h = _DeleteHarness()
    h.store[7] = _FakeRow(7)
    h.commit_started = threading.Event()
    h.commit_release = threading.Event()
    reg = RunnerRegistry()

    async def scenario():
        dele = asyncio.create_task(
            safe_delete_world(7, session_factory=h, registry=reg)
        )
        # deterministic barrier: the worker thread is inside commit now
        assert await asyncio.to_thread(h.commit_started.wait, 10)
        assert reg.is_deleting(7)
        # get() REFUSES instead of lazily creating a runner for the world
        # that is being eliminated (§9)
        with pytest.raises(RuntimeError, match="is being deleted"):
            reg.get(7, seed=1, width=64, height=32)
        assert reg.get_existing(7) is None  # nothing was created
        with pytest.raises(RuntimeError, match="is being deleted"):
            reg.register_existing(7, _runner_for(700))
        assert reg.get_existing(7) is None
        h.commit_release.set()
        assert await dele is None
        assert 7 in h.store and h.store[7].deleted_at is not None
        _assert_guard_released(reg)

    asyncio.run(scenario())


def test_del_09_cancellation_during_close():
    """(9) Cancelación durante el cierre: coherentes, retry posible (§8)."""
    h = _DeleteHarness()
    h.store[7] = _FakeRow(7)
    reg = RunnerRegistry()
    r = _runner_for(7)
    reg.register_existing(7, r)
    main = _StubbornMain()

    async def scenario():
        r._run = main.run
        task = await r.start()
        dele = asyncio.create_task(
            safe_delete_world(7, session_factory=h, registry=reg)
        )
        await main.cancel_seen.wait()  # inside await runner.close()
        dele.cancel()
        # the close is NOT abandoned: it waits for real termination and
        # only then propagates the caller's CancelledError (§5)
        main.release.set()
        with pytest.raises(asyncio.CancelledError):
            await dele
        # coherent state (§8): closed but NOT silently unregistered,
        # persisted data intact (sin marca lógica), guard released
        assert r.state is RunnerState.CLOSED
        assert reg.get_existing(7) is r
        assert 7 in h.store and h.store[7].deleted_at is None
        _assert_guard_released(reg)
        # a retry finishes the job without restarting anything (§8)
        assert await safe_delete_world(7, session_factory=h, registry=reg) is None
        assert 7 in h.store and h.store[7].deleted_at is not None
        assert reg.get_existing(7) is None
        assert task.done()
        assert r._task is task  # same task: never recreated

    asyncio.run(scenario())


def test_del_10_cancellation_during_persisted_delete():
    """(10) Cancelación durante la persistencia de la marca lógica: no se
    abandona."""
    h = _DeleteHarness()
    h.store[7] = _FakeRow(7)
    h.commit_started = threading.Event()
    h.commit_release = threading.Event()
    reg = RunnerRegistry()

    async def scenario():
        dele = asyncio.create_task(
            safe_delete_world(7, session_factory=h, registry=reg)
        )
        # deterministic barrier: the worker thread is inside commit now
        assert await asyncio.to_thread(h.commit_started.wait, 10)
        dele.cancel()
        with pytest.raises(asyncio.CancelledError):
            await dele
        # the caller is gone, but the delete was NOT abandoned: the guard
        # stays marked and the work keeps a strong reference (§9/§8)
        assert reg.is_deleting(7)
        # la fila sigue almacenada; la marca ya está aplicada en el objeto
        # (el UPDATE se emite en el commit, que aún está en vuelo)
        assert 7 in h.store and h.store[7].deleted_at is not None
        assert len(runnermod._lingering_delete_work) == 1
        work = next(iter(runnermod._lingering_delete_work))
        # let the worker thread really finish
        h.commit_release.set()
        await work
        for _ in range(3):  # yields: run the completion callbacks (FIFO)
            await asyncio.sleep(0)
        # the logical mark completed in the background; only then was
        # the deletion guard released (§9)
        assert 7 in h.store and h.store[7].deleted_at is not None
        assert not reg.is_deleting(7)
        assert reg._delete_users == {} and reg._delete_locks == {}
        assert runnermod._lingering_delete_work == set()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# F7-E.2-I Bloque 2c — comportamiento de la eliminación lógica (2c.1-2c.7)
# ---------------------------------------------------------------------------

def test_del_11_row_remains_after_logical_delete():
    """(2c.1) La fila PERMANECE en el almacén y ``deleted_at`` queda fijado."""
    h = _DeleteHarness()
    h.store[7] = _FakeRow(7)
    reg = RunnerRegistry()

    async def scenario():
        await safe_delete_world(7, session_factory=h, registry=reg)
        assert list(h.store) == [7]               # la clave nunca desaparece
        assert h.store[7].deleted_at is not None  # marca establecida

    asyncio.run(scenario())
    assert ("get", 7) in h.ops
    assert h.ops.count(("mark", 7)) == 1  # exactamente una marca persistida
    assert h.events.count("commit") == 1


def test_del_12_repeated_delete_is_idempotent():
    """(2c.2) Repetición idempotente: fila presente, timestamp EXACTO de la
    primera eliminación y ninguna marca nueva en la segunda pasada."""
    h = _DeleteHarness()
    h.store[7] = _FakeRow(7)
    reg = RunnerRegistry()

    async def scenario():
        await safe_delete_world(7, session_factory=h, registry=reg)
        first_mark = h.store[7].deleted_at
        assert first_mark is not None
        # segunda eliminación: no-op idempotente (DEC-PRE-3)
        await safe_delete_world(7, session_factory=h, registry=reg)
        assert 7 in h.store
        assert h.store[7].deleted_at is first_mark  # ni un nuevo objeto
        _assert_guard_released(reg)

    asyncio.run(scenario())
    assert h.ops.count(("mark", 7)) == 1  # la 2ª NO genera una marca nueva
    assert h.events.count("commit") == 2  # ambas confirman (2ª: sin cambios)


def test_del_13_existing_mark_is_never_overwritten():
    """(2c.3) Fila con ``deleted_at`` previo: el valor no cambia (DEC-PRE-3)."""
    preset = datetime(2020, 5, 4, 3, 2, 1, 987654, tzinfo=timezone.utc)
    h = _DeleteHarness()
    row = _FakeRow(7)
    row.deleted_at = preset
    h.store[7] = row
    reg = RunnerRegistry()

    async def scenario():
        await safe_delete_world(7, session_factory=h, registry=reg)
        assert h.store[7].deleted_at is preset  # idéntico, objeto y todo

    asyncio.run(scenario())
    assert h.ops.count(("mark", 7)) == 0  # sin marca nueva
    assert h.events.count("commit") == 1  # solo la confirmación no-op


def test_del_14_timestamp_is_utc_aware():
    """(2c.4) El valor GENERADO por la operación es ``datetime`` consciente
    de zona horaria y representa UTC (``utcoffset() == timedelta(0)``).

    Verifica el valor producido por la implementación —no una propiedad de
    lectura del backend: SQLite puede devolver ``tzinfo`` naive al releer un
    ``DateTime`` y este test NO afirma nada sobre esa garantía.
    """
    h = _DeleteHarness()
    h.store[7] = _FakeRow(7)
    reg = RunnerRegistry()

    async def scenario():
        await safe_delete_world(7, session_factory=h, registry=reg)
        mark = h.store[7].deleted_at
        assert isinstance(mark, datetime)
        assert mark.tzinfo is not None
        assert mark.utcoffset() == timedelta(0)

    asyncio.run(scenario())


def test_del_15_missing_row_raises_and_commits_nothing():
    """(2c.5) Fila físicamente inexistente: ``WorldNotFoundError`` y ninguna
    modificación confirmada (cero commit, cero marca)."""
    h = _DeleteHarness()  # store vacío: la fila 7 nunca existió
    reg = RunnerRegistry()

    async def scenario():
        with pytest.raises(WorldNotFoundError):
            await safe_delete_world(7, session_factory=h, registry=reg)
        assert 7 not in h.store  # nada fue creado ni tocado
        _assert_guard_released(reg)
        assert runnermod._lingering_delete_work == set()

    asyncio.run(scenario())
    assert ("get", 7) in h.ops
    assert h.events.count("commit") == 0  # ninguna modificación confirmada
    assert h.ops.count(("mark", 7)) == 0  # ninguna marca emitida


def test_del_16_commit_failure_keeps_row_unmarked():
    """(2c.6) Fallo de ``commit()``: la excepción se propaga, la fila sigue
    almacenada, la marca NO queda persistida y jamás se confunde con un
    borrado físico."""
    h = _DeleteHarness()
    h.store[7] = _FakeRow(7)
    h.commit_error = RuntimeError("disk full")
    reg = RunnerRegistry()

    async def scenario():
        with pytest.raises(RuntimeError, match="disk full"):
            await safe_delete_world(7, session_factory=h, registry=reg)
        # el error original llega intacto: nunca se traduce en éxito (§8)
        assert 7 in h.store                      # sigue almacenada
        assert h.store[7].deleted_at is None     # rollback: sin marca
        _assert_guard_released(reg)
        assert runnermod._lingering_delete_work == set()

    asyncio.run(scenario())
    assert h.events.count("commit") == 1  # el commit se intentó y falló
    assert h.ops.count(("mark", 7)) == 0  # sin marca persistida
    assert 7 in h.store                    # cero borrado físico


def test_del_17_cancellation_while_db_worker_in_flight():
    """(2c.7) Cancelación con el worker real de persistencia en vuelo.

    Ejercita el mecanismo REAL de coordinación — ``safe_delete_world()``,
    ``asyncio.to_thread``, ``asyncio.shield``, ``_lingering_delete_work`` y la
    guarda por refcount del registro (no una coroutine ficticia).  Comprueba:

    (1) el worker de base de datos continúa tras cancelar la coroutine que lo
        espera y su marca acaba persistida;
    (2) la cancelación NO libera prematuramente la guarda;
    (3) una segunda eliminación no puede competir con el worker anterior
        mientras este sigue activo: no completa y no altera la primera marca;
    (4) terminado el worker, la guarda se libera y un reintento procede.

    Sincronización determinista con ``threading.Event`` del harness (barreras
    ``commit_started`` / ``commit_second`` y liberación ``commit_release``);
    los ``sleep(0)`` finales solo drenan callbacks FIFO, como en del_10.
    """
    h = _DeleteHarness()
    h.store[7] = _FakeRow(7)
    h.commit_started = threading.Event()   # barrera 1: worker 1 en commit
    h.commit_release = threading.Event()   # liberación única de ambos workers
    h.commit_second = threading.Event()    # barrera 2: worker 2 en commit
    reg = RunnerRegistry()

    def probe() -> None:  # corre DENTRO del commit, en el worker thread
        assert threading.current_thread() is not threading.main_thread(), \
            "the persisted mark must run off the event loop (to_thread)"

    h.commit_probe = probe

    async def scenario():
        assert runnermod._lingering_delete_work == set()  # sin restos previos
        dele = asyncio.create_task(
            safe_delete_world(7, session_factory=h, registry=reg)
        )
        # -- barrera 1: el worker REAL está dentro del commit (to_thread) ----
        assert await asyncio.to_thread(h.commit_started.wait, 10)
        first_mark = h.store[7].deleted_at
        assert first_mark is not None  # marca ya asignada en el objeto
        assert reg._delete_users.get(7) == 1

        # -- cancelar la coroutine que ESPERA: el worker no se toca ----------
        dele.cancel()
        with pytest.raises(asyncio.CancelledError):
            await dele
        # (2) guarda NO liberada: refcount transferido intacto…
        assert reg.is_deleting(7)
        assert reg._delete_users.get(7) == 1  # …ni un decremento prematuro
        # (1) el worker sigue vivo y con referencia fuerte
        assert len(runnermod._lingering_delete_work) == 1
        work1 = next(iter(runnermod._lingering_delete_work))
        assert not work1.done()

        # -- (3) una 2ª eliminación mientras el worker 1 sigue activo --------
        second = asyncio.create_task(
            safe_delete_world(7, session_factory=h, registry=reg)
        )
        # barrera 2: el segundo worker alcanzó su commit — determinista
        assert await asyncio.to_thread(h.commit_second.wait, 10)
        # no COMPLETA mientras el primero persiste…
        assert not second.done()
        # …ni ALTERA la primera marca (guarda de idempotencia en el worker)
        assert h.store[7].deleted_at is first_mark
        assert reg.is_deleting(7)
        assert reg._delete_users.get(7) == 2  # refcount: 2 participantes
        assert len(runnermod._lingering_delete_work) == 2

        # -- liberación: ambos workers reales terminan en sus hilos ----------
        h.commit_release.set()
        await work1
        assert await second is None  # 2ª: no-op idempotente, sin competir
        for _ in range(3):  # yields: drenar callbacks (FIFO)
            await asyncio.sleep(0)
        # (1) la primera marca persistió pese a la cancelación del caller
        assert h.store[7].deleted_at is first_mark
        # (4) guarda liberada…
        _assert_guard_released(reg)
        assert runnermod._lingering_delete_work == set()
        # …y el reintento puede continuar (idempotente)
        assert await safe_delete_world(7, session_factory=h, registry=reg) is None
        assert h.store[7].deleted_at is first_mark
        _assert_guard_released(reg)

    asyncio.run(scenario())
    # una sola marca real en toda la prueba (worker 1); 3 confirmaciones
    assert h.ops.count(("mark", 7)) == 1
    assert h.events.count("commit") == 3
