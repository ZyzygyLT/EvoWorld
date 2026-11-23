"""F7-C: WorldRunner integration (CONTRACTS §16.11, §16.14, §16.16).

Covers ``_tick_durable`` and the ``_run`` loop: recovery-first BEFORE any
new T1, baseline checkpoint before the first T1, ONE durable operation per
tick (even with ``speed > 1``), ``WorldBlocked``/durable failure pausing the
runner with NO second tick over an unresolved operation, the recovery that
corrects a failed T2 without double-advancing the engine, the confirmed-key
inconsistency guard, and the plain ``eng.tick()`` path when no
``operation_service`` is installed.
"""

from __future__ import annotations

import asyncio
import time

from worldsim.server.operation_service import OperationParams
from worldsim.server.runner import WorldRunner, RunnerRegistry
import worldsim.server.runner as runnermod


def _params(world_id: int, months: int = 1) -> OperationParams:
    """Same canonical payload the runner uses for its internal ticks."""
    return OperationParams(world_id=world_id, operation_type="advance",
                           months=months)


def _mk(f7c, engine, wid, service="default", **kw) -> WorldRunner:
    svc = f7c.service if service == "default" else service
    return WorldRunner(wid, 42, 200, 80, engine=engine,
                       operation_service=svc, **kw)


def _quiet(monkeypatch) -> None:
    """Neutralize prophecy/league/season coupling (DEC07 pattern) so the
    tests observe ONLY the tick route."""
    monkeypatch.setattr(runnermod, "resolve_pending", lambda wid, eng: None)
    monkeypatch.setattr(WorldRunner, "_broadcast_league_scores",
                        lambda self: None)
    monkeypatch.setattr(WorldRunner, "_check_season_end", lambda self: None)


def _counting_broadcast(monkeypatch, limit: int) -> list:
    """Replace the tick broadcast with a counter that pauses the runner
    after ``limit`` iterations — deterministic stop, no fixed sleeps."""
    calls: list = []

    def _b(self):
        calls.append(self.eng.month)
        if len(calls) >= limit:
            self.paused = True

    monkeypatch.setattr(WorldRunner, "_broadcast_tick", _b)
    return calls


async def _run_until(runner: WorldRunner, predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "runner loop did not reach the goal"
        await asyncio.sleep(0.005)


# ---------------------------------------------------------------------------
# _tick_durable — direct calls
# ---------------------------------------------------------------------------


def test_tick_durable_full_cycle_baseline_then_t1_then_t2(f7c, engine):
    """Baseline checkpoint lands BEFORE the first T1 (§16.14.1), then
    T1 → executing → tick → T2 confirm: one operation, one month, two
    checkpoints (baseline + T2)."""
    wid = f7c.create_world()
    runner = _mk(f7c, engine, wid)

    assert runner._tick_durable() is True

    rows = f7c.op_rows(wid)
    assert len(rows) == 1
    op = rows[0]
    assert op.status == "confirmed"
    assert op.continuity_state == "active"
    assert op.operation_sequence == 1
    assert op.checkpoint_id is not None

    assert engine.month == 1
    cps = f7c.cp_rows(wid)
    assert len(cps) == 2
    assert cps[0].month == 0   # baseline, created before T1
    assert cps[1].month == 1   # T2 evidence of the tick
    # T1 bound the operation to the baseline that already existed at
    # reservation time (condition 1 evidence).
    assert op.initial_checkpoint_id == cps[0].id
    # The event delta of the tick was persisted with the confirmation.
    for e in f7c.event_rows(wid):
        assert e.operation_sequence == 1
    # ``paused`` is owned by the Etapa 4 transitions: a successful durable
    # tick never flips it (it only ever SETS it on failure).
    assert runner.paused is True


def test_tick_durable_advances_exactly_one_month_per_call(f7c, engine):
    wid = f7c.create_world()
    runner = _mk(f7c, engine, wid)

    assert runner._tick_durable() is True
    assert runner._tick_durable() is True

    assert engine.month == 2
    rows = f7c.op_rows(wid)
    assert [r.operation_sequence for r in rows] == [1, 2]
    assert all(r.status == "confirmed" for r in rows)
    assert len(f7c.cp_rows(wid)) == 3  # baseline + 2 T2


def test_tick_durable_recovery_runs_before_any_new_t1(f7c, engine):
    """An unresolved operation is recovered FIRST: it gets confirmed
    without consuming a new sequence (§16.16 — recovery is part of the
    runner, no T1 may start over an unresolved operation)."""
    wid = f7c.create_world()
    f7c.service.ensure_baseline_checkpoint(wid, engine)
    op = f7c.service.reserve_operation(
        wid, 0, "K-pending", _params(wid)
    ).operation
    assert op.status == "pending"
    runner = _mk(f7c, engine, wid)

    assert runner._tick_durable() is True

    rows = f7c.op_rows(wid)
    assert len(rows) == 1                      # NO new T1 was reserved
    assert rows[0].operation_id == op.operation_id
    assert rows[0].status == "confirmed"
    assert rows[0].recovery_generation == 1
    assert engine.month == 1
    assert len(f7c.cp_rows(wid)) == 2          # baseline + recovered T2


def test_tick_durable_world_blocked_pauses_without_ticking(f7c, engine):
    """Admission denied (409 semantics at the service level) → the runner
    pauses and the engine does NOT advance."""
    wid = f7c.create_world()
    f7c.seed_op(wid, 1, status="failed", continuity_state="blocked")
    runner = _mk(f7c, engine, wid)
    month = engine.month

    assert runner._tick_durable() is False
    assert runner.paused is True
    assert engine.month == month               # no tick ran
    assert len(f7c.op_rows(wid)) == 1          # nothing new reserved

    # A second call never ticks either (still blocked, still paused).
    assert runner._tick_durable() is False
    assert engine.month == month


def test_tick_durable_t2_failure_pauses_then_recovery_without_double_tick(
    f7c, engine, monkeypatch
):
    """Fault injection at T2: the durable failure pauses the runner with
    the operation left ``executing`` and NOTHING published.  The next call
    recovers it from the checkpoint — the engine ends at the SAME month a
    single tick would have produced: no double tick over the unresolved
    operation (§16.16)."""
    wid = f7c.create_world()
    runner = _mk(f7c, engine, wid)

    svc = f7c.service
    original_confirm = svc.confirm_operation

    def _boom(*args, **kwargs):
        raise RuntimeError("injected T2 failure")

    svc.confirm_operation = _boom
    try:
        assert runner._tick_durable() is False
    finally:
        svc.confirm_operation = original_confirm

    assert runner.paused is True
    assert engine.month == 1                   # the volatile tick happened
    row = f7c.op_rows(wid)[0]
    assert row.status == "executing"           # unresolved, never confirmed
    assert row.checkpoint_id is None
    assert len(f7c.cp_rows(wid)) == 1          # baseline only: nothing published
    assert f7c.event_rows(wid) == []

    # Next call: recovery-first reconstructs from the baseline and
    # re-executes — one month TOTAL, not two.
    assert runner._tick_durable() is True
    row = f7c.op_rows(wid)[0]
    assert row.status == "confirmed"
    assert engine.month == 1                   # NOT 2: no double tick
    assert len(f7c.cp_rows(wid)) == 2          # baseline + exactly one T2
    assert len(f7c.op_rows(wid)) == 1          # still one operation


def test_tick_durable_determinism_failure_pauses_and_blocks(f7c, engine):
    """A non-reproducible operation during recovery → pause, durable
    ``blocked``, no generation consumed, engine untouched."""
    wid = f7c.create_world()
    f7c.service.ensure_baseline_checkpoint(wid, engine)
    op = f7c.service.reserve_operation(
        wid, 0, "K1", _params(wid)
    ).operation
    # Corrupt a determinism input (request params ambiguity).
    f7c.set_op(wid, op.operation_id, "pending", "active",
               request_hash="deadbeef" * 8)
    runner = _mk(f7c, engine, wid)
    month = engine.month

    assert runner._tick_durable() is False
    assert runner.paused is True
    assert engine.month == month               # no tick ran

    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "blocked"             # never 'failed' by the runner
    assert row.recovery_generation == 0        # not consumed


def test_tick_durable_confirmed_key_is_never_ticked_over(f7c, engine):
    """An idempotency key already CONFIRMED while the in-memory engine
    never observed that tick: the runner refuses to tick over the
    inconsistency and pauses (no substitute execution, no second row)."""
    wid = f7c.create_world()
    month0 = engine.month
    key = f"internal-tick-{wid}-{month0}"
    h = f7c.service.compute_request_hash(_params(wid))
    op_id = f7c.seed_op(wid, 1, status="confirmed")
    f7c.set_op(wid, op_id, "confirmed", "active",
               client_idempotency_key=key, request_hash=h,
               requesting_user_id=0)
    runner = _mk(f7c, engine, wid)

    assert runner._tick_durable() is False
    assert runner.paused is True
    assert engine.month == month0              # never ticked
    assert len(f7c.op_rows(wid)) == 1          # no new operation
    assert f7c.op_row(wid, op_id).status == "confirmed"  # untouched


def test_tick_durable_without_service_fails_closed(f7c, engine):
    """``_tick_durable`` is the durable path: without a service there is no
    protocol to run, and the failure is contained — paused + ``False``,
    never a crash of the loop and never a plain tick (the plain path lives
    in ``_run``, tested below)."""
    wid = f7c.create_world()
    runner = _mk(f7c, engine, wid, service=None)
    month = engine.month

    assert runner._tick_durable() is False
    assert runner.paused is True
    assert engine.month == month               # never ticked
    assert f7c.op_rows(wid) == []


# ---------------------------------------------------------------------------
# _run — the real loop
# ---------------------------------------------------------------------------


def test_run_loop_one_durable_operation_per_tick(f7c, engine, monkeypatch):
    """Each loop iteration performs ONE durable operation — even with
    ``speed = 3`` (the speed multiplier never multiplies ticks inside a
    single durable operation; each inner step is its own T1→T2)."""
    wid = f7c.create_world()
    runner = _mk(f7c, engine, wid)
    runner.speed = 3
    _quiet(monkeypatch)
    calls = _counting_broadcast(monkeypatch, limit=2)
    monkeypatch.setattr(runnermod, "PUSH_INTERVAL", 0.005)

    async def scenario():
        runner.paused = False
        await runner.start()
        await _run_until(runner, lambda: len(calls) >= 2)
        await runner.close()

    asyncio.run(scenario())

    assert engine.month == 6                   # 2 iterations × speed 3
    rows = f7c.op_rows(wid)
    assert len(rows) == 6                      # ONE durable op per tick
    assert [r.operation_sequence for r in rows] == list(range(1, 7))
    assert all(r.status == "confirmed" for r in rows)
    assert len(f7c.cp_rows(wid)) == 7          # baseline + 6 T2
    assert runner.state is not None            # closed for real


def test_run_loop_recovers_existing_operation_before_new_t1(f7c, engine,
                                                            monkeypatch):
    """On start, an operation left ``pending`` by a crash is recovered by
    the FIRST iteration — no second operation is ever reserved."""
    wid = f7c.create_world()
    f7c.service.ensure_baseline_checkpoint(wid, engine)
    op = f7c.service.reserve_operation(
        wid, 0, "K-leftover", _params(wid)
    ).operation
    runner = _mk(f7c, engine, wid)
    _quiet(monkeypatch)
    calls = _counting_broadcast(monkeypatch, limit=1)
    monkeypatch.setattr(runnermod, "PUSH_INTERVAL", 0.005)

    async def scenario():
        runner.paused = False
        await runner.start()
        await _run_until(runner, lambda: len(calls) >= 1)
        await runner.close()

    asyncio.run(scenario())

    rows = f7c.op_rows(wid)
    assert len(rows) == 1                      # recovered, not duplicated
    assert rows[0].operation_id == op.operation_id
    assert rows[0].status == "confirmed"
    assert engine.month == 1


def test_run_loop_pauses_when_world_blocked(f7c, engine, monkeypatch):
    """Admission denied inside the loop → the runner pauses itself: no
    further ticks, no new operations, engine frozen at its month."""
    wid = f7c.create_world()
    f7c.seed_op(wid, 1, status="failed", continuity_state="blocked")
    runner = _mk(f7c, engine, wid)
    _quiet(monkeypatch)
    calls = _counting_broadcast(monkeypatch, limit=1)
    monkeypatch.setattr(runnermod, "PUSH_INTERVAL", 0.005)
    month = engine.month

    async def scenario():
        runner.paused = False
        await runner.start()
        await _run_until(runner, lambda: len(calls) >= 1)
        # Give the loop a chance to wrongly tick again before closing.
        await asyncio.sleep(0.05)
        await runner.close()

    asyncio.run(scenario())

    assert runner.paused is True
    assert engine.month == month               # frozen
    assert len(f7c.op_rows(wid)) == 1          # nothing reserved
    # Baseline may exist, but no T2 ever did: never ticked.
    assert all(
        cp.checkpoint_type != "periodic" or cp.month == month
        for cp in f7c.cp_rows(wid)
    )


def test_run_loop_plain_tick_without_operation_service(f7c, engine,
                                                       monkeypatch):
    """Without ``operation_service`` the loop keeps the Etapa 5 plain
    ``eng.tick()`` route: no operations, no checkpoints, no T1/T2."""
    wid = f7c.create_world()
    runner = _mk(f7c, engine, wid, service=None)
    _quiet(monkeypatch)
    calls = _counting_broadcast(monkeypatch, limit=2)
    monkeypatch.setattr(runnermod, "PUSH_INTERVAL", 0.005)

    async def scenario():
        runner.paused = False
        await runner.start()
        await _run_until(runner, lambda: len(calls) >= 2)
        await runner.close()

    asyncio.run(scenario())

    assert engine.month == 2
    assert f7c.op_rows(wid) == []              # no durable protocol
    assert f7c.cp_rows(wid) == []              # no baseline, no T2


def test_run_loop_durable_failure_pauses_and_stops_ticking(f7c, engine,
                                                           monkeypatch):
    """A durable failure inside the loop pauses the runner: the loop keeps
    running but never ticks again (``paused`` short-circuit)."""
    wid = f7c.create_world()
    runner = _mk(f7c, engine, wid)
    _quiet(monkeypatch)
    calls = _counting_broadcast(monkeypatch, limit=1)
    monkeypatch.setattr(runnermod, "PUSH_INTERVAL", 0.005)

    svc = f7c.service
    original = svc.reserve_operation

    def _boom(*args, **kwargs):
        raise RuntimeError("injected durable failure")

    svc.reserve_operation = _boom
    try:
        async def scenario():
            runner.paused = False
            await runner.start()
            await _run_until(runner, lambda: len(calls) >= 1)
            await asyncio.sleep(0.05)          # a wrong extra tick would show
            await runner.close()

        asyncio.run(scenario())
    finally:
        svc.reserve_operation = original

    assert runner.paused is True
    assert engine.month == 0                   # never ticked
    assert f7c.op_rows(wid) == []              # no operation was reserved
