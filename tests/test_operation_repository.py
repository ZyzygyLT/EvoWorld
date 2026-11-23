"""F7-C: repository layer for operations, events, resolutions and the
relocated checkpoint helpers (CONTRACTS §16.2, §16.10).

Regression focus: ``next_sequence``/``mark_checkpoint_invalid`` must live
on ``CheckpointRepository`` (F7-C-I1-V1 defect: they had been orphaned into
``ResolutionRepository``, breaking ``recovery_manager`` and
``persistence_service``), and the durable sequence allocator must never
commit inside the repository (T1 owns the transaction).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from worldsim.server.db import WorldOperation
from worldsim.server.repository import (
    CheckpointRepository,
    EventRepository,
    OperationRepository,
    ResolutionRepository,
)


def _params_hash(f7c, wid: int) -> str:
    from worldsim.server.operation_service import OperationParams

    return f7c.service.compute_request_hash(
        OperationParams(world_id=wid, operation_type="advance", months=1)
    )


# ---------------------------------------------------------------------------
# OperationRepository
# ---------------------------------------------------------------------------


def test_create_operation_and_lookup_by_idempotency_triplet(f7c):
    wid = f7c.create_world()
    h = _params_hash(f7c, wid)
    with f7c.Session() as s:
        repo = OperationRepository(s)
        op = repo.create_operation(
            world_id=wid,
            operation_id=str(uuid.uuid4()),
            operation_sequence=1,
            requesting_user_id=7,
            client_idempotency_key="key-a",
            request_hash=h,
            engine_version="1.0",
            rng_state_ref=None,
            initial_checkpoint_id=None,
        )
        s.commit()
        found = repo.get_operation_by_idempotency_key(wid, 7, "key-a")
        assert found.operation_id == op.operation_id
        # Scope is (world, user, key): other user or other world → no match.
        assert repo.get_operation_by_idempotency_key(wid, 8, "key-a") is None
        assert repo.get_operation_by_idempotency_key(wid + 1, 7, "key-a") is None


def test_unique_constraints_enforced(f7c):
    wid = f7c.create_world()
    with f7c.Session() as s:
        repo = OperationRepository(s)
        op_id = str(uuid.uuid4())
        repo.create_operation(
            wid, op_id, 1, 0, "k1", "h", "1.0", None, None
        )
        s.commit()
        # Same (world, sequence) → rejected.
        with pytest.raises(IntegrityError):
            repo.create_operation(wid, str(uuid.uuid4()), 1, 0, "k2", "h", "1.0", None, None)
            s.commit()
        s.rollback()
        # Same (world, user, key) → rejected.
        with pytest.raises(IntegrityError):
            repo.create_operation(wid, str(uuid.uuid4()), 2, 0, "k1", "h", "1.0", None, None)
            s.commit()
        s.rollback()


def test_get_latest_and_get_operations_in_total_order(f7c):
    wid = f7c.create_world()
    f7c.seed_op(wid, 1, status="confirmed")
    f7c.seed_op(wid, 2, status="pending")
    with f7c.Session() as s:
        repo = OperationRepository(s)
        assert repo.get_latest_operation(wid).operation_sequence == 2
        assert [
            o.operation_sequence for o in repo.get_operations(wid)
        ] == [1, 2]
        assert repo.get_latest_operation(wid + 1) is None
        assert repo.get_operations(wid + 1) == []


def test_get_unresolved_operation_positive_list_only(f7c):
    """Only ``pending``/``executing`` are recoverable (§16.11 positive
    list): blocked/failed/canceled/confirmed are never returned."""
    wid = f7c.create_world()
    op_blocked = f7c.seed_op(wid, 1, status="blocked")
    # Highest unresolved wins over older rows.
    op_pending = f7c.seed_op(wid, 2, status="pending")
    with f7c.Session() as s:
        repo = OperationRepository(s)
        assert repo.get_unresolved_operation(wid).operation_id == op_pending

    f7c.set_op(wid, op_pending, "executing", "active")
    with f7c.Session() as s:
        repo = OperationRepository(s)
        assert repo.get_unresolved_operation(wid).operation_id == op_pending

    # Resolving both leaves nothing recoverable.
    f7c.set_op(wid, op_pending, "confirmed", "active")
    with f7c.Session() as s:
        repo = OperationRepository(s)
        assert repo.get_unresolved_operation(wid) is None
        assert repo.get_unresolved_operation(wid + 1) is None
    assert op_blocked  # seeded row still present, never deleted


def test_get_operations_by_status(f7c):
    wid = f7c.create_world()
    f7c.seed_op(wid, 1, status="confirmed")
    f7c.seed_op(wid, 2, status="canceled", continuity_state="released")
    with f7c.Session() as s:
        repo = OperationRepository(s)
        assert [
            o.operation_sequence
            for o in repo.get_operations_by_status(wid, "canceled")
        ] == [2]


def test_update_operation_status_optional_fields(f7c):
    wid = f7c.create_world()
    op_id = f7c.seed_op(wid, 1, status="executing")
    cp_id = f7c.seed_checkpoint(wid, month=0)
    with f7c.Session() as s:
        repo = OperationRepository(s)
        assert repo.update_operation_status(
            wid, op_id, "confirmed",
            continuity_state="active",
            confirmed_at="2026-01-01 00:00:00",
            checkpoint_id=cp_id,
        ) is True
        s.commit()
        assert repo.update_operation_status(
            wid + 1, op_id, "confirmed"
        ) is False
    row = f7c.op_row(wid, op_id)
    assert row.status == "confirmed"
    assert row.confirmed_at == "2026-01-01 00:00:00"
    assert row.checkpoint_id == cp_id


def test_sequence_allocator_reads_and_increments_without_own_commit(f7c):
    """``increment_operation_sequence`` runs inside the CALLER'S
    transaction and never commits: T1 commits reservation + counter +
    insert atomically (§16.11 step 3.6)."""
    wid = f7c.create_world()
    with f7c.Session() as s:
        repo = OperationRepository(s)
        assert repo.get_next_operation_sequence(wid) == 1
        repo.increment_operation_sequence(wid)
        # NOT committed yet: another connection still sees 1.
        with f7c.Session() as other:
            assert int(other.execute(
                text("SELECT next_operation_sequence FROM worlds WHERE id = :w"),
                {"w": wid},
            ).scalar()) == 1
        s.rollback()
    # Rollback reverted the increment.
    with f7c.Session() as s:
        assert int(s.execute(
            text("SELECT next_operation_sequence FROM worlds WHERE id = :w"),
            {"w": wid},
    ).scalar()) == 1

    # Commit path: increment is durable.
    with f7c.Session() as s:
        repo = OperationRepository(s)
        repo.increment_operation_sequence(wid)
        s.commit()
    with f7c.Session() as s:
        assert int(s.execute(
            text("SELECT next_operation_sequence FROM worlds WHERE id = :w"),
            {"w": wid},
        ).scalar()) == 2


def test_sequence_allocator_unknown_world_raises(f7c):
    with f7c.Session() as s:
        repo = OperationRepository(s)
        with pytest.raises(ValueError, match="does not exist"):
            repo.get_next_operation_sequence(99999)


# ---------------------------------------------------------------------------
# EventRepository (visibility/order are covered in test_event_repository)
# ---------------------------------------------------------------------------


def test_get_events_by_operation_ordered_by_position(f7c):
    wid = f7c.create_world()
    op_id = f7c.seed_op(wid, 1, status="confirmed")
    f7c.seed_events(wid, op_id, 1, 4)
    with f7c.Session() as s:
        rows = EventRepository(s).get_events_by_operation(wid, op_id)
        assert [r.position for r in rows] == [0, 1, 2, 3]
        assert EventRepository(s).get_events_by_operation(wid, "nope") == []


# ---------------------------------------------------------------------------
# ResolutionRepository
# ---------------------------------------------------------------------------


def test_create_and_lookup_resolution(f7c):
    wid = f7c.create_world()
    admin = f7c.create_user("root")
    op_id = f7c.seed_op(
        wid, 1, status="failed", continuity_state="blocked"
    )
    with f7c.Session() as s:
        repo = ResolutionRepository(s)
        res = repo.create_resolution(
            world_id=wid,
            operation_id=op_id,
            operation_sequence=1,
            admin_user_id=admin,
            resolution_type="remain_blocked",
            reason="no evidence",
            evidence_refs=None,
            idempotency_key="res-1",
        )
        s.commit()
        assert res.id is not None
        found = repo.get_resolution(wid, op_id, "res-1")
        assert found.id == res.id
        assert repo.get_resolution(wid, op_id, "other") is None
        assert [
            r.id for r in repo.get_resolutions_for_operation(wid, op_id)
        ] == [res.id]
        # Audit trail ordering: a second resolution appends.
        repo.create_resolution(
            wid, op_id, 1, admin, "remain_blocked",
            "still no evidence", None, "res-2",
        )
        s.commit()
        assert len(repo.get_resolutions_for_operation(wid, op_id)) == 2


def test_resolution_unique_and_check_constraints(f7c):
    wid = f7c.create_world()
    admin = f7c.create_user("root2")
    op_id = f7c.seed_op(wid, 1, status="failed", continuity_state="blocked")
    with f7c.Session() as s:
        repo = ResolutionRepository(s)
        repo.create_resolution(
            wid, op_id, 1, admin, "remain_blocked", "r", None, "res-1"
        )
        s.commit()
        with pytest.raises(IntegrityError):
            repo.create_resolution(
                wid, op_id, 1, admin, "remain_blocked", "r", None, "res-1"
            )
            s.commit()
        s.rollback()
        with pytest.raises(IntegrityError):
            repo.create_resolution(
                wid, op_id, 1, admin, "invented_type", "r", None, "res-x"
            )
            s.commit()
        s.rollback()


def test_resolution_admin_user_is_foreign_key(f7c):
    """``admin_user_id`` references ``users.id``: a fabricated principal is
    rejected by the database itself (§16.11.3 authority record)."""
    wid = f7c.create_world()
    op_id = f7c.seed_op(wid, 1, status="failed", continuity_state="blocked")
    with f7c.Session() as s:
        repo = ResolutionRepository(s)
        with pytest.raises(IntegrityError):
            repo.create_resolution(
                wid, op_id, 1, 999999, "remain_blocked", "r", None, "res-1"
            )
            s.commit()


# ---------------------------------------------------------------------------
# CheckpointRepository — relocated helpers (Block 0 regression)
# ---------------------------------------------------------------------------


def test_checkpoint_next_sequence_is_per_world_monotonic(f7c):
    wid = f7c.create_world()
    wid2 = f7c.create_world(name="w2")
    with f7c.Session() as s:
        repo = CheckpointRepository(s)
        assert repo.next_sequence(wid) == 1
        assert repo.next_sequence(wid2) == 1
        cp1 = repo.save_checkpoint(
            wid, 1, 0, "periodic", b"{}", "0" * 64, 2
        )
        assert cp1 is not None
        assert repo.next_sequence(wid) == 2
        assert repo.next_sequence(wid2) == 1  # independent per world


def test_mark_checkpoint_invalid_lives_on_checkpoint_repository(f7c):
    """The method must exist on ``CheckpointRepository`` (it is called by
    ``recovery_manager`` and ``persistence_service``) and must report
    honestly whether the row existed."""
    assert hasattr(CheckpointRepository, "mark_checkpoint_invalid")
    assert hasattr(CheckpointRepository, "next_sequence")
    assert not hasattr(ResolutionRepository, "mark_checkpoint_invalid")
    assert not hasattr(ResolutionRepository, "next_sequence")

    wid = f7c.create_world()
    cp = f7c.seed_checkpoint(wid, month=3)
    with f7c.Session() as s:
        repo = CheckpointRepository(s)
        assert repo.mark_checkpoint_invalid(cp) is True
        s.commit()
        assert repo.mark_checkpoint_invalid(999999) is False
    row = f7c.cp_rows(wid)[0]
    assert row.is_valid == 0


def test_get_latest_valid_checkpoint_skips_invalid_and_corrupt(f7c):
    wid = f7c.create_world()
    good = f7c.seed_checkpoint(wid, month=1, sequence=1)
    with f7c.Session() as s:
        repo = CheckpointRepository(s)
        # Newer row marked invalid is skipped in favor of the valid one.
        bad = repo.save_checkpoint(wid, 2, 2, "periodic", b"{}", "0" * 64, 2)
        repo.mark_checkpoint_invalid(bad)
        s.commit()
        assert repo.get_latest_valid_checkpoint(wid).id == good
