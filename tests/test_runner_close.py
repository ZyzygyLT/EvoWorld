"""Etapa 2 — WorldRunner.close() (DEC18, §11 "WorldRunner.close()": 1-15).

Pruebas deterministas: eventos/barreras (``cancel_seen``/``release``),
verificación explícita de orden/identidad y ``asyncio.sleep(0)`` únicamente
como yield de planificación (nunca sleeps arbitrarios de coordinación).  El
timeout de cierre se reduce con monkeypatch de ``runnermod.CLOSE_TIMEOUT``.
No depende de FastAPI (DEC07).

Numeración según Etapa 2 §11:
  1  cierre de runner no iniciado
  2  cierre de runner ejecutándose
  3  cierre repetido (idempotente, sin repetir operaciones)
  4  dos llamadas concurrentes (una única operación de cierre)
  5  start() después del cierre
  6  cierre con tarea principal ya terminada
  7  cierre con tarea principal bloqueada
  8  timeout de tarea principal
  9  cierre con tareas auxiliares
  10 fallo de tarea auxiliar
  11 timeout de tarea auxiliar
  12 cancelación del llamador
  13 segunda cancelación durante cleanup
  14 conservación de referencias a tareas pendientes
  15 ausencia de tareas huérfanas después del cierre correcto
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

import worldsim.server.runner as runnermod
from worldsim.server.runner import RunnerState, WorldRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runner(world_id: int = 1) -> WorldRunner:
    """Runner with an injected engine (never builds one, no worldgen cost)."""
    return WorldRunner(
        world_id, seed=1, width=64, height=32,
        engine=SimpleNamespace(month=0),
    )


async def _sleep_forever() -> None:
    await asyncio.sleep(3600)


class _BlockedMain:
    """Main-task body blocked on an event; honours cancellation (§11.7)."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def run(self) -> None:
        self.entered.set()
        await asyncio.Event().wait()  # blocked until cancelled


class _StubbornMain:
    """Main-task body that does NOT honour cancellation until released.

    Records when it received its first cancellation (``cancel_seen``), so
    tests can position themselves deterministically while the cleanup is
    waiting on it (§11.8, §11.12-14).
    """

    def __init__(self) -> None:
        self.cancel_seen = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self) -> None:
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                # ignore the cancellation: this task does NOT terminate
                # until the test releases it (timeout/no-honour scenario)


async def _stubborn_aux(release: asyncio.Event) -> None:
    while not release.is_set():
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass  # ignores cancellation until released


def _pending_tasks() -> set[asyncio.Task]:
    me = asyncio.current_task()
    return {t for t in asyncio.all_tasks() if t is not me and not t.done()}


# ---------------------------------------------------------------------------
# §11.1-5 — lifecycle basics
# ---------------------------------------------------------------------------

def test_01_close_unstarted_runner():
    """(1) Cierre de runner no iniciado: igual de definitivo e idempotente."""
    r = _make_runner(101)

    async def scenario():
        assert r.state is RunnerState.NOT_STARTED
        await r.close()
        assert r.state is RunnerState.CLOSED
        assert r._task is None
        assert r._started is False  # nunca se inició y ya no podrá

    asyncio.run(scenario())


def test_02_close_running_runner():
    """(2) Cierre de runner ejecutándose: terminación real de la tarea."""
    r = _make_runner(102)

    async def scenario():
        task = await r.start()
        assert r.state is RunnerState.RUNNING
        assert not task.done()
        await r.close()
        assert r.state is RunnerState.CLOSED
        # real termination awaited — not merely cancel() requested
        assert task.done() and task.cancelled()
        # new ticks are impossible: task dead + paused gate (§3.1)
        assert r.paused is True

    asyncio.run(scenario())


def test_03_repeated_close_is_idempotent():
    """(3) Cierre repetido: mismas operaciones, jamás re-ejecutadas."""
    r = _make_runner(103)

    async def scenario():
        task = await r.start()
        calls = 0
        orig = r._do_close

        async def counting() -> None:
            nonlocal calls
            calls += 1
            await orig()

        r._do_close = counting  # instance attribute shadows the method
        await r.close()
        op = r._close_op
        await r.close()
        await r.close()
        assert calls == 1  # the cleanup ran EXACTLY once
        assert r._close_op is op  # same shared operation, never recreated
        assert r.state is RunnerState.CLOSED
        assert task.done()

    asyncio.run(scenario())


def test_04_two_concurrent_close_calls():
    """(4) Dos o más llamadas concurrentes: una única operación de cierre."""
    r = _make_runner(104)

    async def scenario():
        task = await r.start()
        calls = 0
        orig = r._do_close

        async def counting() -> None:
            nonlocal calls
            calls += 1
            await orig()

        r._do_close = counting
        results = await asyncio.gather(r.close(), r.close(), r.close())
        assert results == [None, None, None]  # clean close for every caller
        assert calls == 1  # single shared close operation (§5)
        assert r.state is RunnerState.CLOSED
        assert task.done() and task.cancelled()

    asyncio.run(scenario())


def test_05_start_after_close_rejected():
    """(5) start() tras el cierre — y también mientras cierra (§2.4)."""
    r = _make_runner(105)

    async def scenario():
        # (a) after a completed close
        await r.close()
        assert r.state is RunnerState.CLOSED
        with pytest.raises(RuntimeError, match="cannot start a runner whose"):
            await r.start()
        assert r._task is None  # nothing was (re)started

        # (b) DURING the close: refusal is explicit from CLOSING onward
        r2 = _make_runner(1052)
        main = _StubbornMain()
        r2._run = main.run  # instance-level shadow: start() drives THIS body
        await r2.start()
        closer = asyncio.create_task(r2.close())
        await main.cancel_seen.wait()  # cleanup is waiting on the main task
        assert r2.state is RunnerState.CLOSING
        with pytest.raises(RuntimeError, match="cannot start a runner whose"):
            await r2.start()
        main.release.set()
        await closer
        assert r2.state is RunnerState.CLOSED

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# §11.6-8 — main task outcomes
# ---------------------------------------------------------------------------

def test_06_close_with_main_task_already_finished():
    """(6) Tarea principal ya terminada: no se cancela innecesariamente."""
    r = _make_runner(106)

    async def scenario():
        # (a) finished cleanly
        async def finished() -> None:
            return None

        r._run = finished
        task = await r.start()
        await task  # done before close ever runs
        assert task.done()
        await r.close()
        assert r.state is RunnerState.CLOSED  # nothing of ours was running
        assert not task.cancelled()  # never cancelled needlessly (§3.9)

        # (b) finished with a PRE-EXISTING failure: observable (logged) but
        # the close itself is still correct — a retry could not fix it.
        r2 = _make_runner(1062)

        async def failing() -> None:
            raise OSError("main died before close")

        r2._run = failing
        task2 = await r2.start()
        with pytest.raises(OSError, match="main died before close"):
            await task2
        await r2.close()
        assert r2.state is RunnerState.CLOSED

    asyncio.run(scenario())


def test_07_close_with_main_task_blocked():
    """(7) Tarea principal bloqueada: se espera su terminación REAL."""
    r = _make_runner(107)
    main = _BlockedMain()

    async def scenario():
        r._run = main.run
        task = await r.start()
        await main.entered.wait()  # the main task is really blocked now
        assert not task.done()
        await r.close()
        # close() RETURNED ⇒ the blocked task really terminated (§3.3)
        assert task.done() and task.cancelled()
        assert r.state is RunnerState.CLOSED

    asyncio.run(scenario())


def test_08_main_task_close_timeout(monkeypatch, caplog):
    """(8) Timeout de la tarea principal: cierre incompleto + refs. fuertes."""
    monkeypatch.setattr(runnermod, "CLOSE_TIMEOUT", 0.05)
    r = _make_runner(108)
    main = _StubbornMain()

    async def scenario():
        r._run = main.run
        task = await r.start()
        with caplog.at_level(logging.ERROR, logger="worldsim.server.runner"):
            with pytest.raises(RuntimeError, match="did not terminate"):
                await r.close()
        assert r.state is RunnerState.CLOSE_INCOMPLETE
        # strong reference retained; never claimed as closed (§3.7-3.8)
        assert r._task is task
        assert not task.done()
        # re-close: NO unsafe repeat, NO success claim (§6)
        op = r._close_op
        with pytest.raises(RuntimeError, match="did not terminate"):
            await r.close()
        assert r._close_op is op
        assert r.state is RunnerState.CLOSE_INCOMPLETE
        # the error is observable via logging too (§3.7)
        assert any("did not terminate" in rec.getMessage() for rec in caplog.records)
        # release the stubborn task so the loop ends clean
        main.release.set()
        await task

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# §11.9-11 — auxiliary tasks
# ---------------------------------------------------------------------------

def test_09_close_with_auxiliary_tasks():
    """(9) Cierre con tareas auxiliares: drenaje completo de todas."""
    r = _make_runner(109)

    async def scenario():
        a1 = r._spawn(_sleep_forever())
        a2 = r._spawn(_sleep_forever())
        assert a1 in r._aux_tasks and a2 in r._aux_tasks
        await r.close()
        assert r.state is RunnerState.CLOSED
        assert a1.done() and a1.cancelled()
        assert a2.done() and a2.cancelled()
        assert r._aux_tasks == set()  # every auxiliary task drained

    asyncio.run(scenario())


def test_10_auxiliary_task_failure_during_close(caplog):
    """(10) Fallo de una tarea auxiliar: observable y no bloquea a las demás."""
    r = _make_runner(110)

    async def scenario():
        async def failing() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise OSError("aux broke during close") from None

        bad = r._spawn(failing())
        good = r._spawn(_sleep_forever())
        with caplog.at_level(logging.WARNING, logger="worldsim.server.runner"):
            with pytest.raises(OSError, match="aux broke during close"):
                await r.close()
        # an incomplete close is never presented as a correct one (§2.8)
        assert r.state is RunnerState.CLOSE_INCOMPLETE
        # the failure never prevented cleaning the OTHER auxiliary task (§4)
        assert good.done() and good.cancelled()
        assert bad.done() and not bad.cancelled()  # ended with OSError
        assert r._aux_tasks == set()
        # logged once by the done-callback (regla 11), never silenced
        assert any(
            "auxiliary task failed" in rec.getMessage() for rec in caplog.records
        )

    asyncio.run(scenario())


def test_11_auxiliary_task_close_timeout(monkeypatch):
    """(11) Timeout de tarea auxiliar: referencia conservada, cierre incompleto."""
    monkeypatch.setattr(runnermod, "CLOSE_TIMEOUT", 0.05)
    r = _make_runner(111)
    release = asyncio.Event()

    async def scenario():
        aux = r._spawn(_stubborn_aux(release))
        with pytest.raises(RuntimeError, match="auxiliary task .* did not terminate"):
            await r.close()
        assert r.state is RunnerState.CLOSE_INCOMPLETE
        # strong reference retained among OUR tasks (§4)
        assert aux in r._aux_tasks
        assert not aux.done()
        # cleanup of the test: release and let it finish
        release.set()
        await aux
        assert aux not in r._aux_tasks  # done-callback drained it afterwards

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# §11.12-13 — caller cancellation
# ---------------------------------------------------------------------------

def test_12_caller_cancellation_during_close(caplog):
    """(12) Cancelación del llamador: cleanup continúa; nunca es éxito."""
    r = _make_runner(112)
    main = _StubbornMain()

    async def scenario():
        r._run = main.run
        task = await r.start()
        closer = asyncio.create_task(r.close())
        await main.cancel_seen.wait()  # cleanup is inside the main-task wait
        closer.cancel()
        for _ in range(5):
            await asyncio.sleep(0)  # yields: let the cancellation land
        # the cleanup was NOT abandoned (§5): caller still waiting, state
        # still CLOSING, main task still held
        assert not closer.done()
        assert r.state is RunnerState.CLOSING
        assert not task.done()
        # finish the cleanup for real
        main.release.set()
        with pytest.raises(asyncio.CancelledError):
            await closer
        # cancellation NEVER becomes success (§6), but the cleanup DID run
        assert closer.cancelled()
        assert r.state is RunnerState.CLOSED
        assert task.done()

    asyncio.run(scenario())
    assert any(
        "was cancelled while the cleanup was running" in rec.getMessage()
        for rec in caplog.records
    )


def test_13_second_cancellation_during_cleanup(caplog):
    """(13) Segunda cancelación mientras el cleanup sigue ejecutándose."""
    r = _make_runner(113)
    main = _StubbornMain()

    async def scenario():
        r._run = main.run
        task = await r.start()
        closer = asyncio.create_task(r.close())
        await main.cancel_seen.wait()

        closer.cancel()  # first cancellation
        await asyncio.sleep(0)
        assert not closer.done()  # consumed: back to waiting on the cleanup
        closer.cancel()  # SECOND cancellation while cleanup is still running
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not closer.done()
        assert r.state is RunnerState.CLOSING
        assert not task.done()

        main.release.set()  # the shared cleanup finishes for real
        with pytest.raises(asyncio.CancelledError):
            await closer
        assert closer.cancelled()  # never success, never a silent swallow
        assert r.state is RunnerState.CLOSED
        assert task.done()
        # afterwards, close() is a clean idempotent no-op
        assert await r.close() is None

    asyncio.run(scenario())
    assert sum(
        1 for rec in caplog.records
        if "was cancelled while the cleanup was running" in rec.getMessage()
    ) == 2  # both cancellations were consumed and observed


# ---------------------------------------------------------------------------
# §11.14-15 — references and orphan-freedom
# ---------------------------------------------------------------------------

def test_14_pending_task_references_retained(monkeypatch):
    """(14) Referencias a tareas pendientes conservadas tras el timeout."""
    monkeypatch.setattr(runnermod, "CLOSE_TIMEOUT", 0.05)
    r = _make_runner(114)
    main = _StubbornMain()
    aux_release = asyncio.Event()

    async def scenario():
        r._run = main.run
        task = await r.start()
        aux = r._spawn(_stubborn_aux(aux_release))
        # multiple failures of different kinds are all preserved (§6)
        with pytest.raises(ExceptionGroup) as excinfo:
            await r.close()
        messages = [str(e) for e in excinfo.value.exceptions]
        assert len(messages) == 2
        assert any("main task" in m for m in messages)
        assert any("auxiliary task" in m for m in messages)
        assert r.state is RunnerState.CLOSE_INCOMPLETE
        # BOTH pending tasks kept under strong references (§3.8, §4) —
        # a timeout never releases references (§6)
        assert r._task is task
        assert not task.done()
        assert aux in r._aux_tasks
        assert not aux.done()
        # release both so the test loop ends without orphans
        main.release.set()
        aux_release.set()
        await asyncio.gather(task, aux)

    asyncio.run(scenario())


def test_15_no_orphan_tasks_after_correct_close():
    """(15) Ausencia de tareas huérfanas después del cierre correcto."""
    r = _make_runner(115)

    async def scenario():
        task = await r.start()
        a1 = r._spawn(_sleep_forever())
        a2 = r._spawn(_sleep_forever())
        await r.close()
        assert r.state is RunnerState.CLOSED
        assert task.done() and a1.done() and a2.done()
        assert r._close_op is not None and r._close_op.done()
        assert r._aux_tasks == set()
        assert _pending_tasks() == set()  # no orphans of any kind
        # spawning during the closed state is refused, never orphaned (§4)
        assert r._spawn(_sleep_forever()) is None
        assert _pending_tasks() == set()
        # an idempotent repeat still leaves nothing behind
        await r.close()
        assert _pending_tasks() == set()

    asyncio.run(scenario())
