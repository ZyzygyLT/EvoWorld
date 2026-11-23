"""F7-C: safe cancellation (CONTRACTS §16.10, §16.12, §16.17 recorrido D,
Ejemplos A/D/G).

Covers: ``pending → canceled`` + ``released`` atomically, every refusal
condition (executing, confirmed, foreign principal, evidence of effects),
terminal irreversibility, coverage discontinuity registered by the durable
row itself, fault injection at the cancellation commit, and admission of
the next operation after release (Ejemplo G: no partial-release window).
"""

from __future__ import annotations

import pytest

from worldsim.server.operation_service import OperationParams, WorldBlocked


def _params(world_id: int, months: int = 1) -> OperationParams:
    return OperationParams(world_id=world_id, operation_type="advance", months=months)


def _coverage(env, world_id: int) -> dict:
    """Live coverage through the SAME function the endpoint exposes."""
    import worldsim.server.app as appmod

    with env.Session() as s:
        return appmod._coverage_metadata(s, world_id)


# ---------------------------------------------------------------------------
# Recorrido D / Ejemplo A — safe cancellation releases continuity
# ---------------------------------------------------------------------------


def test_cancel_pending_releases_and_next_operation_is_admitted(f7c):
    """Ejemplo A: OP20 confirmed → OP21 pending canceled with release →
    OP22 admitted at the next sequence; the canceled row is permanent."""
    wid = f7c.create_world()
    op20 = f7c.seed_op(wid, 20, status="confirmed")
    f7c.seed_events(wid, op20, 20, 5)  # E1–E5
    op21 = f7c.service.reserve_operation(wid, 0, "K21", _params(wid)).operation
    assert op21.operation_sequence == 21

    assert f7c.service.cancel_operation(wid, op21.operation_id, 0) is True

    row = f7c.op_row(wid, op21.operation_id)
    assert row.status == "canceled"
    assert row.continuity_state == "released"

    # World enabled for OP22 (step 5).
    op22 = f7c.service.reserve_operation(wid, 0, "K22", _params(wid)).operation
    assert op22.operation_sequence == 22

    # History: the canceled row is NEVER deleted or rewritten.
    assert [o.operation_sequence for o in f7c.op_rows(wid)] == [20, 21, 22]


def test_cancellation_registers_coverage_discontinuity(f7c):
    """Ejemplo A step 7: coverage is continuous up to seq 20, seq 21 is the
    permanent discontinuity (``canceled``), seq 22 is a posterior segment —
    continuity is never declared THROUGH the cancellation."""
    wid = f7c.create_world()
    op20 = f7c.seed_op(wid, 1, status="confirmed")
    f7c.seed_events(wid, op20, 1, 2)
    op21 = f7c.service.reserve_operation(wid, 0, "K21", _params(wid)).operation
    f7c.service.cancel_operation(wid, op21.operation_id, 0)
    op22 = f7c.service.reserve_operation(wid, 0, "K22", _params(wid)).operation
    # OP22 confirmed with its own checkpoint (T2 evidence).
    f7c.set_op(wid, op22.operation_id, "confirmed", "active",
               checkpoint_id=f7c.seed_checkpoint(wid, month=2))

    cov = _coverage(f7c, wid)
    assert cov["status"] == "potentially_incomplete"
    assert cov["discontinuity_sequence"] == 2
    assert cov["discontinuity_reason"] == "canceled"
    assert cov["last_confirmed_sequence"] == 1


def test_cancellation_of_unconfirmed_without_release_keeps_world_blocked(f7c):
    """Without the release the world stays blocked: a canceled+blocked
    combination rejects the next T1 (recorrido C)."""
    wid = f7c.create_world()
    op = f7c.seed_op(wid, 1, status="canceled", continuity_state="blocked")
    with pytest.raises(WorldBlocked):
        f7c.service.reserve_operation(wid, 0, "K-next", _params(wid))
    assert len(f7c.op_rows(wid)) == 1


# ---------------------------------------------------------------------------
# Refusals — cancellation is only allowed with durable proof of no effects
# ---------------------------------------------------------------------------


def test_cancel_refuses_executing(f7c):
    wid = f7c.create_world()
    op = f7c.service.reserve_operation(wid, 0, "K1", _params(wid)).operation
    f7c.service.mark_executing(wid, op.operation_id)

    assert f7c.service.cancel_operation(wid, op.operation_id, 0) is False
    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "executing"
    assert row.continuity_state == "active"


def test_cancel_refuses_confirmed(f7c):
    """``confirmed`` is irreversible: cancellation must never rewrite it."""
    wid = f7c.create_world()
    op_id = f7c.seed_op(wid, 1, status="confirmed")
    assert f7c.service.cancel_operation(wid, op_id, 0) is False
    row = f7c.op_row(wid, op_id)
    assert row.status == "confirmed"
    assert row.continuity_state == "active"


def test_cancel_refuses_already_canceled(f7c):
    """Terminal state: a second cancellation changes nothing (returns False)."""
    wid = f7c.create_world()
    op = f7c.service.reserve_operation(wid, 0, "K1", _params(wid)).operation
    assert f7c.service.cancel_operation(wid, op.operation_id, 0) is True
    assert f7c.service.cancel_operation(wid, op.operation_id, 0) is False
    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "canceled"
    assert row.continuity_state == "released"


def test_cancel_refuses_foreign_principal(f7c):
    """Only the requesting principal may cancel: anyone else gets False and
    the operation is untouched."""
    wid = f7c.create_world()
    other = f7c.create_user("other")
    op = f7c.service.reserve_operation(wid, 0, "K1", _params(wid)).operation

    assert f7c.service.cancel_operation(wid, op.operation_id, other) is False
    assert f7c.op_row(wid, op.operation_id).status == "pending"


def test_cancel_refuses_when_events_were_persisted(f7c):
    """Evidence of effects defeats the safe-cancellation precondition."""
    wid = f7c.create_world()
    op = f7c.service.reserve_operation(wid, 0, "K1", _params(wid)).operation
    f7c.seed_events(wid, op.operation_id, op.operation_sequence, 1)

    assert f7c.service.cancel_operation(wid, op.operation_id, 0) is False
    assert f7c.op_row(wid, op.operation_id).status == "pending"


def test_cancel_refuses_when_t2_result_exists(f7c):
    """A durable T2 result (checkpoint/confirmed_at) means execution began."""
    wid = f7c.create_world()
    cp = f7c.seed_checkpoint(wid, month=1)
    op = f7c.service.reserve_operation(wid, 0, "K1", _params(wid)).operation
    f7c.set_op(wid, op.operation_id, "pending", "active", checkpoint_id=cp)
    assert f7c.service.cancel_operation(wid, op.operation_id, 0) is False
    assert f7c.op_row(wid, op.operation_id).status == "pending"


def test_cancel_unknown_operation_returns_false(f7c):
    wid = f7c.create_world()
    assert f7c.service.cancel_operation(wid, "no-such-op", 0) is False
    assert f7c.op_rows(wid) == []


# ---------------------------------------------------------------------------
# Atomicity — status + continuity land together or not at all
# ---------------------------------------------------------------------------


def test_cancel_commit_failure_leaves_operation_pending(f7c, monkeypatch):
    """Fault injection AT the cancellation commit: nothing is written —
    there is no window in which ``status`` and ``continuity_state`` disagree."""
    from sqlalchemy.orm import Session

    wid = f7c.create_world()
    op = f7c.service.reserve_operation(wid, 0, "K1", _params(wid)).operation

    original_commit = Session.commit

    def _boom(self):
        raise RuntimeError("injected cancel commit failure")

    Session.commit = _boom
    try:
        with pytest.raises(RuntimeError, match="injected cancel commit"):
            f7c.service.cancel_operation(wid, op.operation_id, 0)
    finally:
        Session.commit = original_commit

    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "pending"           # untouched
    assert row.continuity_state == "active"  # untouched
    # And the operation is still cancellable afterwards (no half-state).
    assert f7c.service.cancel_operation(wid, op.operation_id, 0) is True


def test_cancel_then_t1_no_partial_release_window(f7c):
    """Ejemplo G: a T1 arriving while the operation is still ``pending``
    (before the cancel commit) is rejected; after the commit the same T1 is
    admitted.  The durable row flips atomically between the two outcomes."""
    wid = f7c.create_world()
    op = f7c.service.reserve_operation(wid, 0, "K1", _params(wid)).operation

    # BEFORE the cancel: latest is pending+active → 409.
    with pytest.raises(WorldBlocked):
        f7c.service.reserve_operation(wid, 0, "K2", _params(wid))

    assert f7c.service.cancel_operation(wid, op.operation_id, 0) is True

    # AFTER the commit: latest is canceled+released → admitted.
    next_op = f7c.service.reserve_operation(wid, 0, "K2", _params(wid)).operation
    assert next_op.operation_sequence == 2


def test_cancellation_keeps_events_and_checkpoints_of_other_operations(f7c):
    """Cancellation touches ONLY its own row: durable history of neighbors
    stays byte-for-byte identical."""
    wid = f7c.create_world()
    op20 = f7c.seed_op(wid, 1, status="confirmed")
    f7c.seed_events(wid, op20, 1, 3)
    cp_before = [(c.id, c.sequence, c.month) for c in f7c.cp_rows(wid)]
    ev_before = [(e.id, e.position) for e in f7c.event_rows(wid)]

    op21 = f7c.service.reserve_operation(wid, 0, "K21", _params(wid)).operation
    f7c.service.cancel_operation(wid, op21.operation_id, 0)

    assert [(c.id, c.sequence, c.month) for c in f7c.cp_rows(wid)] == cp_before
    assert [(e.id, e.position) for e in f7c.event_rows(wid)] == ev_before
    assert f7c.op_row(wid, op20).status == "confirmed"
