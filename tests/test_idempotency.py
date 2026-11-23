"""F7-C: idempotency of T1 (CONTRACTS §16.11, §16.17 recorridos G–P).

Covers: retries in every state (pending, executing, confirmed, canceled,
failed±released), same key with a different payload → 409, no sequence
consumption on retries, the post-commit/pre-ACK retry (crash between T1
commit and the client ACK), and two concurrent requests with the same key
resolving to ONE identity and ONE sequence (recorrido P).
"""

from __future__ import annotations

import threading

import pytest

from worldsim.server.operation_service import (
    IdempotencyConflict,
    OperationParams,
    WorldBlocked,
)


def _params(world_id: int, months: int = 1) -> OperationParams:
    return OperationParams(world_id=world_id, operation_type="advance", months=months)


def _counter(env, world_id: int) -> int:
    from sqlalchemy import text

    with env.Session() as s:
        return int(
            s.execute(
                text("SELECT next_operation_sequence FROM worlds WHERE id = :w"),
                {"w": world_id},
            ).scalar()
        )


def _retry(env, wid, key, params=None, user=0):
    """Client retry with the same idempotency key (same canonical payload)."""
    return env.service.reserve_operation(
        wid, user, key, params if params is not None else _params(wid)
    )


# ---------------------------------------------------------------------------
# Recorridos I / J — retry while active
# ---------------------------------------------------------------------------


def test_retry_in_pending_returns_same_operation(f7c):
    """Recorrido I: retry on ``pending`` returns the SAME operation with its
    state — no new sequence, no engine execution."""
    wid = f7c.create_world()
    t1 = f7c.service.reserve_operation(wid, 0, "K1", _params(wid))
    op = t1.operation

    retry = _retry(f7c, wid, "K1")
    assert retry.is_retry is True and retry.existing is True
    assert retry.operation.operation_id == op.operation_id
    assert retry.operation.status == "pending"
    assert retry.operation.operation_sequence == 1
    assert len(f7c.op_rows(wid)) == 1
    assert _counter(f7c, wid) == 2  # nothing consumed by the retry


def test_retry_in_executing_returns_same_operation(f7c):
    """Recorrido J: retry on ``executing`` returns the SAME operation — no
    second tick, no second sequence."""
    wid = f7c.create_world()
    op = f7c.service.reserve_operation(wid, 0, "K1", _params(wid)).operation
    f7c.service.mark_executing(wid, op.operation_id)

    retry = _retry(f7c, wid, "K1")
    assert retry.is_retry is True
    assert retry.operation.operation_id == op.operation_id
    assert retry.operation.status == "executing"
    assert len(f7c.op_rows(wid)) == 1
    assert _counter(f7c, wid) == 2


# ---------------------------------------------------------------------------
# Recorrido K — post-commit / pre-ACK retry (transversal steps 3–4)
# ---------------------------------------------------------------------------


def test_post_commit_pre_ack_retry_returns_confirmed_without_reexecution(
    f7c, engine
):
    """Recorrido K: T1 committed, operation executed and T2 confirmed, then
    the client retries (the ACK was lost).  The retry returns the persisted
    result — no new sequence, no second checkpoint, no re-execution."""
    wid = f7c.create_world()
    f7c.service.ensure_baseline_checkpoint(wid, engine)
    op = f7c.service.reserve_operation(wid, 0, "K1", _params(wid)).operation
    f7c.service.mark_executing(wid, op.operation_id)
    marker = len(engine.chronicle.events)
    engine.tick()
    delta = list(engine.chronicle.events[marker:])
    t2 = f7c.service.confirm_operation(wid, op.operation_id, engine, delta)
    cps_after_t2 = len(f7c.cp_rows(wid))

    # --- client lost the ACK and retries with the same key ---
    retry = _retry(f7c, wid, "K1")

    assert retry.is_retry is True
    assert retry.operation.operation_id == op.operation_id
    assert retry.operation.status == "confirmed"
    assert retry.operation.checkpoint_id == t2.checkpoint_id
    # No new sequence, no new operation, no second checkpoint.
    assert len(f7c.op_rows(wid)) == 1
    assert _counter(f7c, wid) == 2
    assert len(f7c.cp_rows(wid)) == cps_after_t2


# ---------------------------------------------------------------------------
# Recorridos G / H / L — canceled
# ---------------------------------------------------------------------------


def test_retry_in_canceled_released_returns_historic_operation(f7c):
    """Recorridos G/L: retry after safe cancellation returns the historic
    ``canceled`` operation; no OP-new, no sequence consumed."""
    wid = f7c.create_world()
    op = f7c.service.reserve_operation(wid, 0, "K1", _params(wid)).operation
    assert f7c.service.cancel_operation(wid, op.operation_id, 0) is True

    retry = _retry(f7c, wid, "K1")
    assert retry.is_retry is True
    assert retry.operation.operation_id == op.operation_id
    assert retry.operation.status == "canceled"
    assert retry.operation.continuity_state == "released"
    assert len(f7c.op_rows(wid)) == 1
    assert _counter(f7c, wid) == 2

    # New key after release (recorrido H step 2): admitted at seq 2.
    next_op = f7c.service.reserve_operation(wid, 0, "K2", _params(wid)).operation
    assert next_op.operation_sequence == 2


# ---------------------------------------------------------------------------
# Recorridos M / N — failed
# ---------------------------------------------------------------------------


def test_retry_in_failed_released_returns_historic_operation(f7c):
    """Recorrido M: retry after a liberating resolution returns the historic
    ``failed`` operation without reopening it; new key → admitted."""
    wid = f7c.create_world()
    op_id = f7c.seed_op(
        wid, 1, status="failed", continuity_state="released", request_hash=None
    )
    # The seeded row must carry a real request hash for the retry to match.
    params = _params(wid)
    h = f7c.service.compute_request_hash(params)
    f7c.set_op(wid, op_id, "failed", "released", request_hash=h,
               client_idempotency_key="K1", requesting_user_id=0)

    retry = _retry(f7c, wid, "K1")
    assert retry.is_retry is True
    assert retry.operation.operation_id == op_id
    assert retry.operation.status == "failed"
    assert retry.operation.continuity_state == "released"
    assert len(f7c.op_rows(wid)) == 1

    next_op = f7c.service.reserve_operation(wid, 0, "K2", params).operation
    assert next_op.operation_sequence == 2


def test_retry_in_failed_blocked_returns_historic_but_world_stays_blocked(f7c):
    """Recorrido N: retry with the SAME key returns the historic operation
    even while the world is blocked; a NEW key is rejected with 409."""
    wid = f7c.create_world()
    op_id = f7c.seed_op(wid, 1, status="failed", continuity_state="blocked")
    params = _params(wid)
    h = f7c.service.compute_request_hash(params)
    f7c.set_op(wid, op_id, "failed", "blocked", request_hash=h,
               client_idempotency_key="K1", requesting_user_id=0)

    retry = _retry(f7c, wid, "K1")
    assert retry.is_retry is True
    assert retry.operation.operation_id == op_id
    assert retry.operation.status == "failed"
    assert retry.operation.continuity_state == "blocked"
    assert len(f7c.op_rows(wid)) == 1

    # New key → world blocked (409 at service level).
    with pytest.raises(WorldBlocked):
        f7c.service.reserve_operation(wid, 0, "K2", params)
    assert len(f7c.op_rows(wid)) == 1
    assert _counter(f7c, wid) == 2


# ---------------------------------------------------------------------------
# Recorrido O — same key, different payload
# ---------------------------------------------------------------------------


def test_same_key_with_different_request_hash_is_409(f7c):
    """Recorrido O: key reuse with a semantically different payload →
    IdempotencyConflict (HTTP 409 at the API layer); no new operation, no
    sequence consumed, existing operation untouched."""
    wid = f7c.create_world()
    op = f7c.service.reserve_operation(wid, 0, "K1", _params(wid)).operation

    with pytest.raises(IdempotencyConflict):
        f7c.service.reserve_operation(
            wid, 0, "K1", _params(wid, months=7)  # different canonical payload
        )

    assert len(f7c.op_rows(wid)) == 1
    assert _counter(f7c, wid) == 2
    row = f7c.op_row(wid, op.operation_id)
    assert row.status == "pending"          # untouched
    assert row.operation_sequence == 1


def test_same_key_conflict_even_when_world_would_be_blocked(f7c):
    """The hash check precedes everything else: a conflicting retry reports
    the 409 idempotency conflict, never a different error."""
    wid = f7c.create_world()
    f7c.service.reserve_operation(wid, 0, "K1", _params(wid))
    f7c.service.mark_executing(wid, f7c.op_rows(wid)[0].operation_id)

    with pytest.raises(IdempotencyConflict):
        f7c.service.reserve_operation(wid, 0, "K1", _params(wid, months=2))


# ---------------------------------------------------------------------------
# Retries across scopes
# ---------------------------------------------------------------------------


def test_idempotency_key_is_scoped_by_world_and_user(f7c):
    """The tripleta is (world, user, key): the same key from another user or
    another world is an independent request, not a retry."""
    wid = f7c.create_world()
    wid2 = f7c.create_world(name="w2")
    admin = f7c.create_user("someone")

    t1 = f7c.service.reserve_operation(wid, 0, "K1", _params(wid))
    assert t1.is_retry is False
    # Release continuity so the world admits further requests (the scope
    # check must run against a world that would otherwise be blocked).
    assert f7c.service.cancel_operation(wid, t1.operation.operation_id, 0) is True

    other_user = f7c.service.reserve_operation(wid, admin, "K1", _params(wid))
    assert other_user.is_retry is False
    assert other_user.operation.operation_id != t1.operation.operation_id
    assert other_user.operation.operation_sequence == 2  # independent request

    other_world = f7c.service.reserve_operation(wid2, 0, "K1", _params(wid2))
    assert other_world.is_retry is False
    assert other_world.operation.operation_sequence == 1  # fresh world

    # And the original tripleta still resolves to the original operation.
    again = _retry(f7c, wid, "K1")
    assert again.is_retry is True
    assert again.operation.operation_id == t1.operation.operation_id


def test_retry_never_creates_a_second_checkpoint_or_event(f7c, engine):
    """Across ALL retryable states the durable history stays byte-for-byte
    identical after a retry (no duplicate events, no duplicate checkpoints)."""
    wid = f7c.create_world()
    f7c.service.ensure_baseline_checkpoint(wid, engine)
    op = f7c.service.reserve_operation(wid, 0, "K1", _params(wid)).operation
    f7c.service.mark_executing(wid, op.operation_id)
    engine.tick()
    f7c.service.confirm_operation(
        wid, op.operation_id, engine, list(engine.chronicle.events)
    )

    before_ops = [(o.operation_id, o.operation_sequence, o.status)
                  for o in f7c.op_rows(wid)]
    before_cps = [(c.id, c.sequence, c.month) for c in f7c.cp_rows(wid)]
    before_events = [(e.id, e.operation_sequence, e.position)
                     for e in f7c.event_rows(wid)]

    for _ in range(3):
        _retry(f7c, wid, "K1")

    assert [(o.operation_id, o.operation_sequence, o.status)
            for o in f7c.op_rows(wid)] == before_ops
    assert [(c.id, c.sequence, c.month) for c in f7c.cp_rows(wid)] == before_cps
    assert [(e.id, e.operation_sequence, e.position)
            for e in f7c.event_rows(wid)] == before_events
    assert _counter(f7c, wid) == 2


# ---------------------------------------------------------------------------
# Recorrido P — two concurrent requests with the same key
# ---------------------------------------------------------------------------


def test_concurrent_same_key_resolves_to_single_identity(f7c):
    """Recorrido P: two concurrent T1 requests with the same key and the
    same canonical payload are serialized by ``BEGIN IMMEDIATE``; the loser
    returns the winner's operation.  ONE identity, ONE sequence."""
    wid = f7c.create_world()
    params = _params(wid)
    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def _call():
        try:
            barrier.wait(timeout=10)
            results.append(
                f7c.service.reserve_operation(wid, 0, "K2", params)
            )
        except BaseException as exc:  # noqa: BLE001 — collected for assertion
            errors.append(exc)

    threads = [threading.Thread(target=_call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == [], errors
    assert len(results) == 2
    ids = {r.operation.operation_id for r in results}
    assert len(ids) == 1, f"two identities created: {ids}"
    assert sum(1 for r in results if r.is_retry) == 1  # exactly one winner
    assert len(f7c.op_rows(wid)) == 1
    assert _counter(f7c, wid) == 2
    assert f7c.op_rows(wid)[0].operation_sequence == 1


def test_concurrent_same_key_with_different_payload_conflicts(f7c):
    """Concurrent reuse with DIFFERENT payloads: one winner is created and
    the other request reports the idempotency conflict — never a duplicate."""
    wid = f7c.create_world()
    params_a = _params(wid, months=1)
    params_b = _params(wid, months=3)
    barrier = threading.Barrier(2)
    outcomes: list = []

    def _call(params):
        try:
            barrier.wait(timeout=10)
            r = f7c.service.reserve_operation(wid, 0, "K3", params)
            outcomes.append(("ok", r.operation.operation_id))
        except IdempotencyConflict:
            outcomes.append(("conflict", None))
        except BaseException as exc:  # noqa: BLE001
            outcomes.append(("error", repr(exc)))

    threads = [
        threading.Thread(target=_call, args=(params_a,)),
        threading.Thread(target=_call, args=(params_b,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    kinds = sorted(k for k, _ in outcomes)
    assert kinds == ["conflict", "ok"], outcomes
    assert len(f7c.op_rows(wid)) == 1
    assert _counter(f7c, wid) == 2
