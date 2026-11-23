"""F7-C: administrative resolution of terminal failures (CONTRACTS §16.11.3,
§16.12, §16.17 recorridos F/M/N, Ejemplos B/E/F).

Covers: liberating resolutions (``confirmed_no_effects`` /
``continue_with_gap``) releasing continuity atomically with the audit
record, ``remain_blocked`` keeping the world blocked, idempotent replay,
immutability once released (``ResolutionConflict``), the correction trail
via ``previous_resolution_id`` in ``evidence_refs``, the admin FK, and the
coverage discontinuity that survives the release.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.exc import IntegrityError

from worldsim.server.operation_service import (
    InvalidTransition,
    OperationNotFound,
    OperationParams,
    ResolutionConflict,
    WorldBlocked,
)


def _params(world_id: int, months: int = 1) -> OperationParams:
    return OperationParams(world_id=world_id, operation_type="advance", months=months)


def _coverage(env, world_id: int) -> dict:
    import worldsim.server.app as appmod

    with env.Session() as s:
        return appmod._coverage_metadata(s, world_id)


def _failed_op(env, wid, seq=1, continuity="blocked"):
    """Seed a terminal ``failed`` operation (state reached by the durable
    failure path; never fabricated as anything else)."""
    return env.seed_op(wid, seq, status="failed", continuity_state=continuity)


# ---------------------------------------------------------------------------
# Ejemplo B / recorrido F — liberating resolution
# ---------------------------------------------------------------------------


def test_continue_with_gap_releases_continuity_atomically(f7c):
    """Ejemplo B step 5–6 / recorrido F: resolution row + release commit in
    ONE transaction; ``status`` stays ``failed`` forever."""
    wid = f7c.create_world()
    admin = f7c.create_user("root", is_admin=True)
    op_id = _failed_op(f7c, wid, seq=1)

    res = f7c.service.resolve_failed_operation(
        wid, op_id, admin, "continue_with_gap",
        "evidence reviewed", None, "res-1",
    )

    row = f7c.op_row(wid, op_id)
    assert row.status == "failed"                    # inalterado (Ejemplo B.7)
    assert row.continuity_state == "released"
    assert res.resolution_type == "continue_with_gap"
    assert res.admin_user_id == admin
    assert res.id is not None

    # After release the world admits the next operation (recorrido F step 4).
    nxt = f7c.service.reserve_operation(wid, 0, "K-next", _params(wid)).operation
    assert nxt.operation_sequence == 2


def test_confirmed_no_effects_releases_too(f7c):
    wid = f7c.create_world()
    admin = f7c.create_user("root", is_admin=True)
    op_id = _failed_op(f7c, wid, seq=1)

    f7c.service.resolve_failed_operation(
        wid, op_id, admin, "confirmed_no_effects", "no effects found",
        json.dumps(["checkpoint:1"]), "res-1",
    )
    assert f7c.op_row(wid, op_id).continuity_state == "released"
    nxt = f7c.service.reserve_operation(wid, 0, "K-next", _params(wid)).operation
    assert nxt.operation_sequence == 2


def test_released_history_keeps_discontinuity_in_coverage(f7c):
    """Ejemplo E step 6: the release never repairs the history — coverage
    still reports the permanent discontinuity at the failed sequence."""
    wid = f7c.create_world()
    admin = f7c.create_user("root", is_admin=True)
    op1 = f7c.seed_op(wid, 1, status="confirmed")
    f7c.seed_events(wid, op1, 1, 2)
    op2 = _failed_op(f7c, wid, seq=2)

    f7c.service.resolve_failed_operation(
        wid, op2, admin, "continue_with_gap", "gap accepted", None, "res-1"
    )
    # Posterior segment confirmed (recorrido F step 5).
    op3 = f7c.service.reserve_operation(wid, 0, "K3", _params(wid)).operation
    f7c.set_op(wid, op3.operation_id, "confirmed", "active",
               checkpoint_id=f7c.seed_checkpoint(wid, month=3))

    cov = _coverage(f7c, wid)
    assert cov["status"] == "potentially_incomplete"
    assert cov["discontinuity_sequence"] == 2
    assert cov["discontinuity_reason"] == "failed"
    assert cov["last_confirmed_sequence"] == 1


# ---------------------------------------------------------------------------
# Ejemplo F / recorrido N — non-liberating resolution
# ---------------------------------------------------------------------------


def test_remain_blocked_keeps_world_blocked(f7c):
    """Ejemplo F: ``remain_blocked`` records the decision but leaves
    ``continuity_state='blocked'``; the next T1 is rejected with 409."""
    wid = f7c.create_world()
    admin = f7c.create_user("root", is_admin=True)
    op_id = _failed_op(f7c, wid, seq=1)

    res = f7c.service.resolve_failed_operation(
        wid, op_id, admin, "remain_blocked", "insufficient evidence",
        None, "res-1",
    )

    row = f7c.op_row(wid, op_id)
    assert row.status == "failed"
    assert row.continuity_state == "blocked"
    assert res.resolution_type == "remain_blocked"

    with pytest.raises(WorldBlocked):
        f7c.service.reserve_operation(wid, 0, "K-next", _params(wid))
    assert len(f7c.op_rows(wid)) == 1


# ---------------------------------------------------------------------------
# Authority + validation
# ---------------------------------------------------------------------------


def test_resolution_requires_existing_admin_principal(f7c):
    """``admin_user_id`` must reference an existing user with is_admin=True:
    a non-existent or non-admin user is rejected before the DB FK constraint."""
    wid = f7c.create_world()
    op_id = _failed_op(f7c, wid, seq=1)

    from worldsim.server.operation_service import InvalidTransition

    with pytest.raises(InvalidTransition):
        f7c.service.resolve_failed_operation(
            wid, op_id, 999999, "continue_with_gap", "r", None, "res-1"
        )

    # Nothing was written: continuity untouched, no resolution recorded.
    row = f7c.op_row(wid, op_id)
    assert row.continuity_state == "blocked"
    with f7c.Session() as s:
        from worldsim.server.db import OperationResolution

        assert s.query(OperationResolution).count() == 0


def test_resolution_requires_failed_status(f7c):
    wid = f7c.create_world()
    admin = f7c.create_user("root", is_admin=True)
    op_id = f7c.seed_op(wid, 1, status="confirmed")

    with pytest.raises(InvalidTransition, match="failed"):
        f7c.service.resolve_failed_operation(
            wid, op_id, admin, "continue_with_gap", "r", None, "res-1"
        )
    with pytest.raises(OperationNotFound):
        f7c.service.resolve_failed_operation(
            wid, "no-such-op", admin, "continue_with_gap", "r", None, "res-1"
        )


def test_invalid_resolution_type_is_rejected(f7c):
    wid = f7c.create_world()
    admin = f7c.create_user("root", is_admin=True)
    op_id = _failed_op(f7c, wid, seq=1)
    with pytest.raises(ValueError, match="resolution_type"):
        f7c.service.resolve_failed_operation(
            wid, op_id, admin, "invented_type", "r", None, "res-1"
        )
    with pytest.raises(ValueError, match="idempotency_key"):
        f7c.service.resolve_failed_operation(
            wid, op_id, admin, "remain_blocked", "r", None, ""
        )


# ---------------------------------------------------------------------------
# Idempotency + immutability
# ---------------------------------------------------------------------------


def test_resolution_same_key_is_idempotent(f7c):
    """Replaying the same decision returns the ORIGINAL record unmodified."""
    wid = f7c.create_world()
    admin = f7c.create_user("root", is_admin=True)
    op_id = _failed_op(f7c, wid, seq=1)

    first = f7c.service.resolve_failed_operation(
        wid, op_id, admin, "continue_with_gap", "r1", None, "res-1"
    )
    replay = f7c.service.resolve_failed_operation(
        wid, op_id, admin, "continue_with_gap", "different reason text",
        None, "res-1",
    )

    assert replay.id == first.id
    assert replay.reason == first.reason            # original untouched
    assert replay.resolution_type == first.resolution_type
    with f7c.Session() as s:
        from worldsim.server.db import OperationResolution

        assert s.query(OperationResolution).count() == 1


def test_second_resolution_with_new_key_after_release_conflicts(f7c):
    """Once released by a committed resolution the decision is immutable:
    another key can never revoke or re-decide it (ResolutionConflict)."""
    wid = f7c.create_world()
    admin = f7c.create_user("root", is_admin=True)
    op_id = _failed_op(f7c, wid, seq=1)

    f7c.service.resolve_failed_operation(
        wid, op_id, admin, "continue_with_gap", "r1", None, "res-1"
    )
    with pytest.raises(ResolutionConflict):
        f7c.service.resolve_failed_operation(
            wid, op_id, admin, "remain_blocked", "change of mind",
            None, "res-2",
        )
    # The released state and the single record stand.
    assert f7c.op_row(wid, op_id).continuity_state == "released"
    with f7c.Session() as s:
        from worldsim.server.db import OperationResolution

        assert s.query(OperationResolution).count() == 1


def test_correction_while_not_released_references_previous_record(f7c):
    """A follow-up decision BEFORE any release appends to the audit trail
    and embeds ``previous_resolution_id`` in ``evidence_refs`` (the schema
    has no dedicated column for it)."""
    wid = f7c.create_world()
    admin = f7c.create_user("root", is_admin=True)
    op_id = _failed_op(f7c, wid, seq=1)

    first = f7c.service.resolve_failed_operation(
        wid, op_id, admin, "remain_blocked", "first pass", None, "res-1"
    )
    second = f7c.service.resolve_failed_operation(
        wid, op_id, admin, "remain_blocked", "second pass",
        json.dumps(["evidence:B"]), "res-2",
    )

    assert second.id != first.id
    refs = json.loads(second.evidence_refs or "[]")
    assert {"previous_resolution_id": first.id} in refs
    assert "evidence:B" in refs
    with f7c.Session() as s:
        from worldsim.server.db import OperationResolution

        assert s.query(OperationResolution).count() == 2
    # Still blocked: neither decision was liberating.
    assert f7c.op_row(wid, op_id).continuity_state == "blocked"


def test_blocked_operation_is_not_recoverable_after_failure(f7c):
    """A ``failed`` operation never re-enters the recovery positive list."""
    wid = f7c.create_world()
    op_id = _failed_op(f7c, wid, seq=1)
    assert f7c.service.get_unresolved_operation(wid) is None
    with pytest.raises((WorldBlocked, InvalidTransition)):
        # Neither T1 nor mark_executing may revive it.
        f7c.service.mark_executing(wid, op_id)


# ---------------------------------------------------------------------------
# Atomicity — fault injection at the resolution commit
# ---------------------------------------------------------------------------


def test_resolution_commit_failure_writes_nothing(f7c, monkeypatch):
    """Fault injection AT the commit: the resolution row and the continuity
    update must both vanish — release never happens without its record."""
    from sqlalchemy.orm import Session

    wid = f7c.create_world()
    admin = f7c.create_user("root", is_admin=True)
    op_id = _failed_op(f7c, wid, seq=1)

    original_commit = Session.commit

    def _boom(self):
        raise RuntimeError("injected resolution commit failure")

    Session.commit = _boom
    try:
        with pytest.raises(RuntimeError, match="injected resolution commit"):
            f7c.service.resolve_failed_operation(
                wid, op_id, admin, "continue_with_gap", "r", None, "res-1"
            )
    finally:
        Session.commit = original_commit

    assert f7c.op_row(wid, op_id).continuity_state == "blocked"  # untouched
    with f7c.Session() as s:
        from worldsim.server.db import OperationResolution

        assert s.query(OperationResolution).count() == 0

    # The same decision succeeds afterwards (nothing half-committed).
    f7c.service.resolve_failed_operation(
        wid, op_id, admin, "continue_with_gap", "r", None, "res-1"
    )
    assert f7c.op_row(wid, op_id).continuity_state == "released"


def test_resolution_sequence_matches_the_failed_operation(f7c):
    """The audit record carries the operation's sequence so the
    discontinuity it justifies is unambiguous."""
    wid = f7c.create_world()
    admin = f7c.create_user("root", is_admin=True)
    op_id = _failed_op(f7c, wid, seq=7)

    res = f7c.service.resolve_failed_operation(
        wid, op_id, admin, "continue_with_gap", "r", None, "res-1"
    )
    assert res.operation_sequence == 7
    assert res.resolution_version == 1
    assert res.created_at
