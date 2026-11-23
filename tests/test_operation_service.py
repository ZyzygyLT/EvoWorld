"""F7-C: durable T1/T2 protocol core (CONTRACTS §16.10–§16.11).

Covers: T1 reservation/identity/atomicity, the full admission decision
table, the ``pending → executing`` transition, T2 atomic confirmation with
delta events, T2 fencing, T2 fault injection (rollback), the baseline
checkpoint of §16.14.1 and the contractual operation representation.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from worldsim.server.operation_service import (
    InvalidTransition,
    IdempotencyConflict,
    OperationNotFound,
    OperationParams,
    WorldBlocked,
    admission_allowed,
)
from worldsim.server.repository import OperationRepository


def _params(world_id: int, months: int = 1) -> OperationParams:
    return OperationParams(world_id=world_id, operation_type="advance", months=months)


def _counter(env, world_id: int) -> int:
    with env.Session() as s:
        return int(
            s.execute(
                text("SELECT next_operation_sequence FROM worlds WHERE id = :w"),
                {"w": world_id},
            ).scalar()
        )


# ---------------------------------------------------------------------------
# T1 — reservation
# ---------------------------------------------------------------------------


def test_t1_reserves_first_sequence_and_persists_identity(f7c):
    wid = f7c.create_world()
    t1 = f7c.service.reserve_operation(wid, 0, "k1", _params(wid))

    op = t1.operation
    assert t1.is_retry is False and t1.existing is False
    assert op.operation_sequence == 1
    assert op.status == "pending"
    assert op.continuity_state == "active"
    assert op.recovery_generation == 0
    assert op.recovery_attempt_id is None
    assert op.operation_id
    assert op.client_idempotency_key == "k1"
    assert op.engine_version == "1.0"
    assert op.request_hash == f7c.service.compute_request_hash(_params(wid))
    # Reservation + counter increment + insert committed atomically.
    assert _counter(f7c, wid) == 2
    assert len(f7c.op_rows(wid)) == 1


def test_t1_requires_durable_principal(f7c):
    """Operations that need a durable identity demand an authenticated
    principal (§16.11: no session → 401, at service level a refusal)."""
    wid = f7c.create_world()
    with pytest.raises(ValueError):
        f7c.service.reserve_operation(wid, None, "k1", _params(wid))
    assert f7c.op_rows(wid) == []


def test_t1_references_latest_valid_checkpoint(f7c):
    """``initial_checkpoint_id``/``rng_state_ref`` point at the durable state
    the operation starts from (§16.14 conditions 1 and 3)."""
    wid = f7c.create_world()
    cp = f7c.seed_checkpoint(wid, month=0)
    op = f7c.service.reserve_operation(wid, 0, "k1", _params(wid)).operation
    assert op.initial_checkpoint_id == cp
    assert op.rng_state_ref == str(cp)


def test_t1_allocates_from_durable_counter_not_max_plus_one(f7c):
    """The allocator is ``worlds.next_operation_sequence`` (§16.2): a
    counter deliberately ahead of MAX(sequence) is honored — the value is
    never derived from the rows."""
    wid = f7c.create_world()
    f7c.seed_op(wid, 1)
    f7c.seed_op(wid, 2)
    f7c.seed_op(wid, 3)
    with f7c.Session() as s:
        s.execute(
            text("UPDATE worlds SET next_operation_sequence = 10 WHERE id = :w"),
            {"w": wid},
        )
        s.commit()

    op = f7c.service.reserve_operation(wid, 0, "k1", _params(wid)).operation
    assert op.operation_sequence == 10
    assert _counter(f7c, wid) == 11


def test_t1_failure_consumes_no_sequence(f7c, monkeypatch):
    """Rollback of T1 reverts reservation, counter increment and insert
    together: a failed T1 consumes nothing (§16.11 atomic commit)."""
    wid = f7c.create_world()
    original = OperationRepository.create_operation

    def _boom(*args, **kwargs):
        raise RuntimeError("injected T1 insert failure")

    monkeypatch.setattr(OperationRepository, "create_operation", _boom)
    with pytest.raises(RuntimeError, match="injected"):
        f7c.service.reserve_operation(wid, 0, "k1", _params(wid))
    monkeypatch.undo()

    assert f7c.op_rows(wid) == []
    assert _counter(f7c, wid) == 1
    # The key and the sequence remain usable afterwards.
    op = f7c.service.reserve_operation(wid, 0, "k1", _params(wid)).operation
    assert op.operation_sequence == 1


# ---------------------------------------------------------------------------
# T1 — admission decision table (§16.11, combinación completa)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,continuity,allowed",
    [
        ("pending", "active", False),
        ("executing", "active", False),
        ("blocked", "active", False),
        ("confirmed", "active", True),
        ("failed", "blocked", False),
        ("failed", "released", True),
        ("canceled", "blocked", False),
        ("canceled", "released", True),
    ],
)
def test_admission_table_full_combination(status, continuity, allowed):
    assert admission_allowed(status, continuity) is allowed


def test_t1_rejects_new_key_while_pending(f7c):
    """Recorrido B: ``pending + active`` → 409 world_blocked, no sequence
    consumed for the rejected request."""
    wid = f7c.create_world()
    f7c.seed_op(wid, 1, status="pending", continuity_state="active")
    with pytest.raises(WorldBlocked):
        f7c.service.reserve_operation(wid, 0, "other-key", _params(wid))
    assert len(f7c.op_rows(wid)) == 1
    assert _counter(f7c, wid) == 2


def test_t1_rejects_new_key_while_failed_unreleased(f7c):
    """Recorrido E: ``failed + blocked`` → world_blocked."""
    wid = f7c.create_world()
    f7c.seed_op(wid, 1, status="failed", continuity_state="blocked")
    with pytest.raises(WorldBlocked):
        f7c.service.reserve_operation(wid, 0, "other-key", _params(wid))
    assert len(f7c.op_rows(wid)) == 1


def test_t1_admits_new_key_after_confirmed(f7c):
    """Recorrido A: ``confirmed + active`` → the next sequence is admitted."""
    wid = f7c.create_world()
    f7c.seed_op(wid, 1, status="confirmed", continuity_state="active")
    op = f7c.service.reserve_operation(wid, 0, "k-next", _params(wid)).operation
    assert op.operation_sequence == 2
    assert op.status == "pending"


def test_t1_admits_new_key_after_released_terminal(f7c):
    """Recorridos D/F/H: ``canceled|failed + released`` → admitted."""
    wid = f7c.create_world()
    f7c.seed_op(wid, 1, status="canceled", continuity_state="released")
    op = f7c.service.reserve_operation(wid, 0, "k-next", _params(wid)).operation
    assert op.operation_sequence == 2

    wid2 = f7c.create_world()
    f7c.seed_op(wid2, 1, status="failed", continuity_state="released")
    op2 = f7c.service.reserve_operation(wid2, 0, "k-next", _params(wid2)).operation
    assert op2.operation_sequence == 2


# ---------------------------------------------------------------------------
# pending → executing
# ---------------------------------------------------------------------------


def test_mark_executing_transitions_and_is_idempotent(f7c):
    wid = f7c.create_world()
    op = f7c.service.reserve_operation(wid, 0, "k1", _params(wid)).operation
    f7c.service.mark_executing(wid, op.operation_id)
    assert f7c.op_row(wid, op.operation_id).status == "executing"
    # Re-entry while already executing is a no-op, not an error.
    f7c.service.mark_executing(wid, op.operation_id)
    assert f7c.op_row(wid, op.operation_id).status == "executing"


def test_mark_executing_refuses_terminal_states(f7c):
    wid = f7c.create_world()
    op_id = f7c.seed_op(wid, 1, status="confirmed")
    with pytest.raises(InvalidTransition):
        f7c.service.mark_executing(wid, op_id)
    with pytest.raises(OperationNotFound):
        f7c.service.mark_executing(wid, "no-such-op")


# ---------------------------------------------------------------------------
# T2 — atomic confirmation
# ---------------------------------------------------------------------------


def _prepare_executing(f7c, engine, key="k1"):
    wid = f7c.create_world()
    baseline = f7c.service.ensure_baseline_checkpoint(wid, engine)
    op = f7c.service.reserve_operation(wid, 0, key, _params(wid)).operation
    f7c.service.mark_executing(wid, op.operation_id)
    return wid, op, baseline


def test_t2_confirms_checkpoint_events_and_status_atomically(f7c, engine):
    wid, op, baseline = _prepare_executing(f7c, engine)
    marker = len(engine.chronicle.events)
    month_before = engine.month
    engine.tick()
    delta = list(engine.chronicle.events[marker:])
    assert engine.month == month_before + 1

    t2 = f7c.service.confirm_operation(
        wid, op.operation_id, engine, delta, expected_generation=0
    )

    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "confirmed"
    assert row.continuity_state == "active"
    assert row.confirmed_at is not None
    assert row.checkpoint_id == t2.checkpoint_id
    assert t2.operation_confirmed is True

    # Checkpoint of the resulting state, same transaction.
    cps = f7c.cp_rows(wid)
    assert [c.sequence for c in cps] == [1, 2]  # baseline + T2
    assert cps[-1].month == engine.month
    assert cps[-1].is_valid == 1

    # Event DELTA with 0-based positions (§16.13), operation_sequence bound.
    evs = f7c.event_rows(wid)
    assert len(evs) == len(delta)
    assert [e.position for e in evs] == list(range(len(evs)))
    assert all(e.operation_sequence == op.operation_sequence for e in evs)
    assert [e.event_type for e in evs] == [d.type.name for d in delta]
    assert [e.event_text for e in evs] == [d.text for d in delta]


def test_t2_empty_delta_is_valid(f7c, engine):
    """An operation whose tick produced no events confirms with an empty
    result — no fictitious event at position 0 (§16.13)."""
    wid, op, _ = _prepare_executing(f7c, engine)
    f7c.service.confirm_operation(wid, op.operation_id, engine, [])
    assert f7c.event_rows(wid) == []
    assert f7c.op_row(wid, op.operation_id).status == "confirmed"


def test_t2_requires_executing_status(f7c, engine):
    wid = f7c.create_world()
    f7c.service.ensure_baseline_checkpoint(wid, engine)
    op = f7c.service.reserve_operation(wid, 0, "k1", _params(wid)).operation
    with pytest.raises(InvalidTransition, match="executing"):
        f7c.service.confirm_operation(wid, op.operation_id, engine, [])
    with pytest.raises(OperationNotFound):
        f7c.service.confirm_operation(wid, "no-such-op", engine, [])


def test_t2_fencing_mismatch_rolls_back_completely(f7c, engine):
    """A stale fencing generation cannot confirm: checkpoint, events and
    the status transition are all rolled back together (§16.11 T2)."""
    wid, op, _ = _prepare_executing(f7c, engine)
    engine.tick()
    from worldsim.server.operation_service import FencingError

    with pytest.raises(FencingError):
        f7c.service.confirm_operation(
            wid, op.operation_id, engine, list(engine.chronicle.events),
            expected_generation=1,  # durable generation is 0
        )

    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "executing"  # unchanged
    assert row.confirmed_at is None
    assert row.checkpoint_id is None
    assert len(f7c.cp_rows(wid)) == 1  # only the baseline
    assert f7c.event_rows(wid) == []


def test_t2_commit_failure_rolls_back_checkpoint_events_and_status(f7c, engine, monkeypatch):
    """Fault injection AT the T2 commit: every write of the transaction is
    reverted — the operation stays ``executing`` for later recovery
    (scenario D / §16.11)."""
    wid, op, _ = _prepare_executing(f7c, engine)
    engine.tick()

    from sqlalchemy.orm import Session

    original_commit = Session.commit

    def _boom(self):
        raise RuntimeError("injected T2 commit failure")

    Session.commit = _boom
    try:
        with pytest.raises(RuntimeError, match="injected T2 commit"):
            f7c.service.confirm_operation(
                wid, op.operation_id, engine, list(engine.chronicle.events)
            )
    finally:
        Session.commit = original_commit

    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "executing"
    assert row.confirmed_at is None
    assert row.checkpoint_id is None
    assert len(f7c.cp_rows(wid)) == 1  # baseline only: checkpoint rolled back
    assert f7c.event_rows(wid) == []


def test_t2_checkpoint_failure_rolls_back_before_any_write(f7c, engine, monkeypatch):
    """Fault injection INSIDE T2 before commit: the staged checkpoint
    insert is rolled back with the rest of the transaction."""
    from worldsim.server.repository import CheckpointRepository

    wid, op, _ = _prepare_executing(f7c, engine)
    engine.tick()

    original = CheckpointRepository.save_checkpoint

    def _boom(self, *args, **kwargs):
        raise RuntimeError("injected checkpoint failure")

    monkeypatch.setattr(CheckpointRepository, "save_checkpoint", _boom)
    with pytest.raises(RuntimeError, match="injected checkpoint"):
        f7c.service.confirm_operation(
            wid, op.operation_id, engine, list(engine.chronicle.events)
        )
    monkeypatch.undo()

    assert f7c.op_row(wid, op.operation_id).status == "executing"
    assert len(f7c.cp_rows(wid)) == 1
    assert f7c.event_rows(wid) == []


# ---------------------------------------------------------------------------
# Baseline checkpoint (§16.14 condition 1)
# ---------------------------------------------------------------------------


def test_baseline_checkpoint_created_once_before_first_t1(f7c, engine):
    wid = f7c.create_world()
    cp1 = f7c.service.ensure_baseline_checkpoint(wid, engine)
    assert cp1 is not None
    cps = f7c.cp_rows(wid)
    assert len(cps) == 1
    assert cps[0].checkpoint_type == "pre_tick"
    assert cps[0].month == engine.month
    # Idempotent: never overwrites the existing reconstruction point.
    assert f7c.service.ensure_baseline_checkpoint(wid, engine) is None
    assert len(f7c.cp_rows(wid)) == 1


def test_baseline_not_created_when_valid_checkpoint_exists(f7c, engine):
    wid = f7c.create_world()
    existing = f7c.seed_checkpoint(wid, month=7)
    assert f7c.service.ensure_baseline_checkpoint(wid, engine) is None
    assert [c.id for c in f7c.cp_rows(wid)] == [existing]


# ---------------------------------------------------------------------------
# Contractual representation (§16.11 response table)
# ---------------------------------------------------------------------------


def test_representation_of_confirmed_includes_delta_result(f7c):
    wid = f7c.create_world()
    op_id = f7c.seed_op(
        wid, 1, status="confirmed", confirmed_at="2026-01-01 00:00:00"
    )
    f7c.seed_events(wid, op_id, 1, 3)

    op = f7c.op_row(wid, op_id)
    rep = f7c.service.get_operation_representation(op)
    assert rep["operation_id"] == op_id
    assert rep["operation_sequence"] == 1
    assert rep["status"] == "confirmed"
    assert rep["continuity_state"] == "active"
    assert rep["created_at"]
    assert rep["confirmed_at"] == "2026-01-01 00:00:00"
    assert len(rep["events"]) == 3
    assert [e["position"] for e in rep["events"]] == [0, 1, 2]
    assert rep["events"][0]["event_text"] == "event 1:0"


def test_representation_of_pending_is_identity_only(f7c):
    wid = f7c.create_world()
    op = f7c.service.reserve_operation(wid, 0, "k1", _params(wid)).operation
    rep = f7c.service.get_operation_representation(op)
    assert rep["status"] == "pending"
    assert "events" not in rep
    assert "checkpoint_id" not in rep
    assert rep["operation_sequence"] == 1
