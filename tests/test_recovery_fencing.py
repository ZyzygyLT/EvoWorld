"""F7-C: recovery, determinism and fencing (CONTRACTS §16.11, §16.14,
§16.15 scenarios B/C/D/G1/G5/J, §16.17 Ejemplo C).

Covers: full recovery of ``pending``/``executing`` operations from the
durable checkpoint (in-place engine restoration per DEC12), discarding
volatile mutations, crash-during-T2 recovery, re-acquisition with a NEW
generation after a previous attempt (G1), rival recoverers with fencing
(Ejemplo C: one batch of events, one checkpoint, no duplication), the
positive recoverable list, no generation consumed on determinism failures,
and a reference-engine comparison proving re-execution determinism.
"""

from __future__ import annotations

import json

import pytest

from worldsim.server.operation_service import (
    DeterminismBlocked,
    FencingError,
    NotRecoverable,
    OperationParams,
    WorldBlocked,
)


def _params(world_id: int, months: int = 1) -> OperationParams:
    return OperationParams(world_id=world_id, operation_type="advance", months=months)


def _coverage(env, world_id: int) -> dict:
    import worldsim.server.app as appmod

    with env.Session() as s:
        return appmod._coverage_metadata(s, world_id)


def _prepare_pending(f7c, engine, key="K1"):
    """World + baseline + T1 reserved, execution NOT started (scenario B)."""
    wid = f7c.create_world()
    baseline = f7c.service.ensure_baseline_checkpoint(wid, engine)
    op = f7c.service.reserve_operation(wid, 0, key, _params(wid)).operation
    return wid, op, baseline


def _prepare_executing(f7c, engine, key="K1", mutate: bool = False):
    """World + baseline + T1 + executing; optionally mutate memory first
    (scenario C/D: volatile state that recovery must discard)."""
    wid, op, baseline = _prepare_pending(f7c, engine, key)
    f7c.service.mark_executing(wid, op.operation_id)
    if mutate:
        engine.tick()  # volatile mutation, never confirmed
    return wid, op, baseline


# ---------------------------------------------------------------------------
# Scenario B — crash after T1, before execution
# ---------------------------------------------------------------------------


def test_recover_pending_operation_reconstructs_and_confirms(f7c, engine):
    wid, op, baseline = _prepare_pending(f7c, engine)
    month_before = engine.month

    eng, t2 = f7c.service.recover_operation(
        wid, op.operation_id, _params(wid), target_engine=engine
    )

    assert eng is engine  # DEC12: restored IN PLACE, one canonical engine
    assert engine.month == month_before + 1
    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "confirmed"
    assert row.continuity_state == "active"
    assert row.recovery_generation == 1
    assert row.checkpoint_id == t2.checkpoint_id
    # Baseline + T2 checkpoints; events are exactly the re-executed delta.
    assert len(f7c.cp_rows(wid)) == 2
    assert f7c.cp_rows(wid)[-1].month == engine.month
    for e in f7c.event_rows(wid):
        assert e.operation_sequence == op.operation_sequence


def test_recover_never_reexecutes_confirmed_operation(f7c, engine):
    """Scenario E/F/G6/G7: a ``confirmed`` operation is irreversible —
    recovery refuses it (positive list) and nothing is re-executed."""
    wid, op, _ = _prepare_pending(f7c, engine)
    f7c.service.recover_operation(
        wid, op.operation_id, _params(wid), target_engine=engine
    )
    cps = [(c.id, c.month) for c in f7c.cp_rows(wid)]
    events = [(e.id, e.position) for e in f7c.event_rows(wid)]
    month = engine.month

    with pytest.raises(NotRecoverable):
        f7c.service.recover_operation(
            wid, op.operation_id, _params(wid), target_engine=engine
        )

    assert engine.month == month  # no tick executed
    assert [(c.id, c.month) for c in f7c.cp_rows(wid)] == cps
    assert [(e.id, e.position) for e in f7c.event_rows(wid)] == events


# ---------------------------------------------------------------------------
# Scenario C — crash after mutating the engine in memory
# ---------------------------------------------------------------------------


def test_recover_discards_volatile_mutations(f7c, engine):
    wid, op, baseline = _prepare_executing(f7c, engine)
    checkpoint_month = f7c.cp_rows(wid)[0].month
    # Volatile mutation: TWO unconfirmed ticks (engine races ahead of the
    # durable checkpoint — the classic crash-after-mutation state).
    engine.tick()
    engine.tick()
    mutated_month = engine.month
    assert mutated_month == checkpoint_month + 2

    eng, _ = f7c.service.recover_operation(
        wid, op.operation_id, _params(wid), target_engine=engine
    )

    # The mutation is discarded: reconstruction starts from the durable
    # checkpoint month and advances exactly ONE month.
    assert engine.month == checkpoint_month + 1
    assert engine.month < mutated_month
    assert f7c.op_row(wid, op.operation_id).status == "confirmed"
    # Only ONE batch of events (the re-executed tick) was persisted.
    assert len(f7c.cp_rows(wid)) == 2


def test_recovery_preserves_paused_mirror(f7c, engine):
    """DEC12: the runner's operational ``paused`` flag survives the
    in-place reconstruction."""
    wid, op, _ = _prepare_pending(f7c, engine)
    engine.paused = True

    eng, _ = f7c.service.recover_operation(
        wid, op.operation_id, _params(wid), target_engine=engine
    )
    assert eng is engine
    assert engine.paused is True


# ---------------------------------------------------------------------------
# Scenario D / G5 — crash during T2 before commit
# ---------------------------------------------------------------------------


def test_recover_after_t2_commit_failure_reexecutes_once(f7c, engine, monkeypatch):
    """The T2 commit fails (crash before durable confirmation): nothing is
    written, the operation stays ``executing`` and recovery re-executes it
    from the checkpoint — exactly one confirmed batch afterwards."""
    from sqlalchemy.orm import Session

    wid, op, baseline = _prepare_executing(f7c, engine, mutate=True)
    engine.tick()  # engine already advanced (volatile)

    original_commit = Session.commit

    def _boom(self):
        raise RuntimeError("crash during T2 commit")

    Session.commit = _boom
    try:
        with pytest.raises(RuntimeError, match="crash during T2"):
            f7c.service.confirm_operation(
                wid, op.operation_id, engine, list(engine.chronicle.events)
            )
    finally:
        Session.commit = original_commit

    # Nothing durable changed (rollback of the whole T2 transaction).
    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "executing"
    assert row.checkpoint_id is None
    assert len(f7c.cp_rows(wid)) == 1  # baseline only
    assert f7c.event_rows(wid) == []

    f7c.service.recover_operation(
        wid, op.operation_id, _params(wid), target_engine=engine
    )
    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "confirmed"
    assert row.recovery_generation == 1
    assert len(f7c.cp_rows(wid)) == 2  # baseline + exactly one T2


# ---------------------------------------------------------------------------
# Scenario G1 — crash after acquiring a generation, before reconstructing
# ---------------------------------------------------------------------------


def test_reacquire_after_previous_attempt_uses_new_generation(f7c, engine):
    wid, op, _ = _prepare_executing(f7c, engine)

    # First attempt acquires generation 1 / RA1, then the process dies
    # before reconstructing anything.
    first = f7c.service.acquire_recovery(wid, op.operation_id, _params(wid))
    assert first.generation == 1
    assert first.attempt_id
    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "executing"          # pending → executing at acquire
    assert row.recovery_generation == 1
    assert row.recovery_attempt_id == first.attempt_id

    # Restart: recovery re-acquires with a NEW generation (G+1).
    eng, t2 = f7c.service.recover_operation(
        wid, op.operation_id, _params(wid), target_engine=engine
    )
    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "confirmed"
    assert row.recovery_generation == 2       # monotonic: 1 → 2
    assert row.recovery_attempt_id != first.attempt_id
    assert t2.operation_confirmed is True


# ---------------------------------------------------------------------------
# Ejemplo C — rival recoverers with fencing
# ---------------------------------------------------------------------------


def test_rival_recoverers_single_batch_no_duplication(f7c, engine):
    """R1 acquires gen 1, R2 acquires gen 2 (R1 obsolete); R1's T2 with the
    stale generation rolls back COMPLETELY; R2 confirms with gen 2 → one
    batch of events, one checkpoint, one confirmation."""
    wid, op, baseline = _prepare_executing(f7c, engine)

    r1 = f7c.service.acquire_recovery(wid, op.operation_id, _params(wid))
    assert r1.generation == 1
    r2 = f7c.service.acquire_recovery(wid, op.operation_id, _params(wid))
    assert r2.generation == 2  # rival bumped the durable generation

    # R1 (stale) ticks and tries to confirm with generation 1.
    engine.tick()
    with pytest.raises(FencingError):
        f7c.service.confirm_operation(
            wid, op.operation_id, engine, list(engine.chronicle.events),
            expected_generation=r1.generation,
        )
    # Full rollback: R1 published NOTHING.
    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "executing"
    assert row.recovery_generation == 2       # R2's acquisition stands
    assert row.checkpoint_id is None
    assert len(f7c.cp_rows(wid)) == 1         # baseline only
    assert f7c.event_rows(wid) == []

    # R2 reconstructs from the durable checkpoint, re-executes, confirms
    # with generation 2.
    from worldsim.export.checkpoint import engine_from_checkpoint

    cp_row = f7c.cp_rows(wid)[0]
    recon = engine_from_checkpoint(json.loads(cp_row.payload))
    engine.__dict__.update(recon.__dict__)
    marker = len(engine.chronicle.events)
    engine.tick()
    delta = list(engine.chronicle.events[marker:])
    t2 = f7c.service.confirm_operation(
        wid, op.operation_id, engine, delta,
        expected_generation=r2.generation,
    )

    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "confirmed"
    assert row.recovery_generation == 2
    assert row.checkpoint_id == t2.checkpoint_id
    # ONE batch: baseline + exactly one T2 checkpoint, one event delta.
    assert len(f7c.cp_rows(wid)) == 2
    assert len(f7c.event_rows(wid)) == len(delta)

    # Step 9: a later client retry observes ``confirmed`` and returns the
    # persisted result without re-executing.
    retry = f7c.service.reserve_operation(wid, 0, "K1", _params(wid))
    assert retry.is_retry is True
    assert retry.operation.status == "confirmed"
    assert len(f7c.cp_rows(wid)) == 2  # unchanged


def test_fencing_mismatch_rolls_back_all_t2_writes(f7c, engine):
    """T2 verifies fencing INSIDE its transaction: on mismatch the
    checkpoint, the events and the status transition all roll back."""
    wid, op, _ = _prepare_executing(f7c, engine)
    engine.tick()

    with pytest.raises(FencingError):
        f7c.service.confirm_operation(
            wid, op.operation_id, engine, list(engine.chronicle.events),
            expected_generation=7,  # durable value is 0
        )
    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "executing"
    assert row.recovery_generation == 0       # nothing acquired
    assert row.confirmed_at is None
    assert row.checkpoint_id is None
    assert len(f7c.cp_rows(wid)) == 1
    assert f7c.event_rows(wid) == []


# ---------------------------------------------------------------------------
# Scenario J — non-reproducible operation
# ---------------------------------------------------------------------------


def test_non_reproducible_operation_blocks_without_consuming_generation(
    f7c, engine
):
    """§16.14 failure → durable ``blocked`` (never ``failed``), generation
    NOT consumed, world blocked for new T1, coverage discontinuity."""
    wid = f7c.create_world()
    f7c.service.ensure_baseline_checkpoint(wid, engine)
    op = f7c.service.reserve_operation(wid, 0, "K1", _params(wid)).operation
    f7c.service.mark_executing(wid, op.operation_id)

    with pytest.raises(DeterminismBlocked):
        # Params differ from the recorded request_hash → condition 2 fails.
        f7c.service.acquire_recovery(wid, op.operation_id, _params(wid, months=7))

    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "blocked"            # never 'failed'
    assert row.continuity_state == "active"
    assert row.recovery_generation == 0       # NOT consumed on failure
    assert row.recovery_attempt_id is None
    state = json.loads(row.recovery_state or "{}")
    assert state["deterministic"] is False
    assert state["blocked_reason"] == "request_params_ambiguous"

    # Not in the positive list anymore.
    assert f7c.service.get_unresolved_operation(wid) is None
    with pytest.raises(NotRecoverable):
        f7c.service.acquire_recovery(wid, op.operation_id, _params(wid))
    # And the world refuses new T1 (409).
    with pytest.raises(WorldBlocked):
        f7c.service.reserve_operation(wid, 0, "K2", _params(wid))

    # Coverage: discontinuity at S with reason 'blocked'.
    cov = _coverage(f7c, wid)
    assert cov["status"] == "potentially_incomplete"
    assert cov["discontinuity_sequence"] == op.operation_sequence
    assert cov["discontinuity_reason"] == "blocked"
    assert cov["last_confirmed_sequence"] is None


@pytest.mark.parametrize(
    "mutate_row,expected_reason",
    [
        ({"engine_version": "9.9-foreign"}, "engine_version_mismatch"),
        ({"request_hash": "deadbeef" * 8}, "request_params_ambiguous"),
        ({"rng_state_ref": "999999"}, "rng_state_mismatch"),
    ],
)
def test_determinism_data_checked_conditions_block(
    f7c, engine, mutate_row, expected_reason
):
    """Conditions 1/2/3/4/7/8 are verified from durable data; each failure
    blocks without consuming a generation."""
    wid, op, _ = _prepare_executing(f7c, engine)
    f7c.set_op(wid, op.operation_id, "executing", "active", **mutate_row)

    with pytest.raises(DeterminismBlocked):
        f7c.service.acquire_recovery(wid, op.operation_id, _params(wid))

    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "blocked"
    assert row.recovery_generation == 0
    assert json.loads(row.recovery_state)["blocked_reason"] == expected_reason


def test_invalid_initial_checkpoint_blocks(f7c, engine):
    """Condition 1: an invalid (corrupted/revoked) initial checkpoint makes
    the operation non-reproducible → blocked, no generation consumed."""
    wid, op, _ = _prepare_executing(f7c, engine)
    with f7c.Session() as s:
        from worldsim.server.repository import CheckpointRepository

        cp_repo = CheckpointRepository(s)
        cp_repo.mark_checkpoint_invalid(f7c.cp_rows(wid)[0].id)
        s.commit()

    with pytest.raises(DeterminismBlocked):
        f7c.service.acquire_recovery(wid, op.operation_id, _params(wid))
    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "blocked"
    assert row.recovery_generation == 0
    assert json.loads(row.recovery_state)["blocked_reason"] == (
        "initial_checkpoint_invalid"
    )


# ---------------------------------------------------------------------------
# Positive list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,continuity",
    [
        ("confirmed", "active"),
        ("blocked", "active"),
        ("failed", "blocked"),
        ("failed", "released"),
        ("canceled", "blocked"),
        ("canceled", "released"),
    ],
)
def test_positive_list_excludes_terminal_states(f7c, engine, status, continuity):
    """Only ``pending``/``executing`` are recoverable: every other state
    raises NotRecoverable and never increments the generation."""
    wid, op, _ = _prepare_pending(f7c, engine)
    f7c.set_op(wid, op.operation_id, status, continuity)

    with pytest.raises(NotRecoverable):
        f7c.service.acquire_recovery(wid, op.operation_id, _params(wid))
    with pytest.raises(NotRecoverable):
        f7c.service.recover_operation(
            wid, op.operation_id, _params(wid), target_engine=engine
        )
    row = f7c.op_row(wid, op.operation_id)
    assert row.status == status                    # untouched
    assert row.recovery_generation == 0            # no generation consumed


# ---------------------------------------------------------------------------
# Determinism — reference-engine comparison
# ---------------------------------------------------------------------------


def test_recovery_reexecution_matches_reference_engine(f7c, engine, races, governments, small_world):
    """Recovery re-executes from the durable checkpoint and produces the
    SAME outcome as a reference engine that starts from that checkpoint and
    ticks once (conditions 1–4/7/8 data-checked + RNG restored)."""
    from worldsim.sim.engine import SimEngine

    wid, op, baseline = _prepare_executing(f7c, engine, mutate=True)

    # Reference: a brand-new engine seeded exactly like the fixture,
    # advanced once from the checkpoint's starting month.
    reference = SimEngine(small_world, races, governments, seed=42)
    reference_month = baseline and f7c.cp_rows(wid)[0].month
    assert reference_month is not None
    while reference.month < reference_month:
        reference.tick()
    ref_marker = len(reference.chronicle.events)
    reference.tick()
    ref_delta = [
        (e.month, e.type.name, e.text) for e in reference.chronicle.events[ref_marker:]
    ]

    f7c.service.recover_operation(
        wid, op.operation_id, _params(wid), target_engine=engine
    )

    assert engine.month == reference.month
    recovered_delta = [
        (e.month, e.event_type, e.event_text) for e in f7c.event_rows(wid)
    ]
    assert recovered_delta == ref_delta
