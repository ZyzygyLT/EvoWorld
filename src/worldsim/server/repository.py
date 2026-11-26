"""Repository layer for world persistence (Fase 2.1).

Provides a thin, transactional data-access layer over the SQLAlchemy models
defined in ``db.py``.  The repository does *not* own the simulation engine;
it only persists and restores the serialized checkpoint payload produced by
``worldsim.export.checkpoint``.

All public methods accept an optional SQLAlchemy ``Session`` or
``sessionmaker``.  When none is provided, the repository creates its own
session lazily (first time ``_session`` is accessed) and keeps it until
``close()`` is called or the context manager exits.  The repository does NOT
close externally-provided sessions.

Session handling rules:
- If a session instance is passed, it is used directly and NOT closed by
  this repository (the caller owns it).  The repository does NOT commit or
  rollback on externally-provided sessions; the caller is responsible for
  transaction boundaries.
- If a sessionmaker class or instance is passed, a new session is created
  lazily and closed when ``close()`` is called or the context manager exits.
  The repository owns this session and handles commits/rollbacks.
- If no session is provided, a new session is created lazily using
  ``db.SessionLocal`` and closed when ``close()`` is called or the context
  manager exits.  The repository owns this session and handles
  commits/rollbacks.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any, Optional

from sqlalchemy import and_, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from .db import (
    SessionLocal,
    WorldCheckpoint,
    WorldRow,
    WorldOperation,
    WorldEvent,
    OperationResolution,
    ChallengeRow,
    ChallengeParticipantRow,
    ChallengeOperationRow,
    ChallengeCreationRequestRow,
)


def _is_sessionmaker_instance(obj: Any) -> bool:
    """Return True if obj is an instance of sessionmaker (not the class)."""
    return isinstance(obj, sessionmaker) and not isinstance(obj, type)


def _is_sessionmaker_class(obj: Any) -> bool:
    """Return True if obj is the sessionmaker class or a subclass."""
    return isinstance(obj, type) and issubclass(obj, sessionmaker)


class WorldRepository:
    """Read/write access to the ``worlds`` table."""

    def __init__(self, session: Session | sessionmaker | None = None) -> None:
        # Determine session source and ownership based on the object we will
        # actually use, not just on whether the original argument was None.
        if session is None:
            # No session provided: create one internally, we own it.
            self._session_obj: Session | None = None
            self._owns_session = True
        elif _is_sessionmaker_instance(session):
            # sessionmaker instance: create a new session, we own it.
            self._session_obj = session()
            self._owns_session = True
        elif _is_sessionmaker_class(session):
            # sessionmaker class: create a new session, we own it.
            self._session_obj = session()
            self._owns_session = True
        else:
            # Session instance: use it directly, caller owns it.
            self._session_obj = session
            self._owns_session = False

    def __enter__(self) -> "WorldRepository":
        if self._session_obj is None:
            self._session_obj = SessionLocal()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._owns_session and self._session_obj is not None:
            self._session_obj.close()

    def close(self) -> None:
        if self._owns_session and self._session_obj is not None:
            self._session_obj.close()
            self._session_obj = None

    @property
    def _session(self) -> Session:
        """Return the active session, creating one lazily if needed."""
        if self._session_obj is None:
            self._session_obj = SessionLocal()
            self._owns_session = True
        return self._session_obj

    def _commit_or_flush(self, s: Session) -> None:
        """Commit if we own the session, otherwise just flush (caller commits)."""
        if self._owns_session:
            s.commit()
        else:
            s.flush()

    def _rollback(self, s: Session) -> None:
        """Rollback only if we own the session. External sessions are left
        untouched; the caller is responsible for rollback."""
        if self._owns_session:
            s.rollback()

    def get_world(self, world_id: int) -> WorldRow | None:
        """Return the world row by primary key, or None if missing.

        F7-E.2: logically deleted worlds (``deleted_at IS NOT NULL``) are
        treated as missing; ordinary reads never see them (decision 1).
        """
        s = self._session
        return s.scalars(
            select(WorldRow).where(
                WorldRow.id == world_id,
                WorldRow.deleted_at.is_(None),
            )
        ).first()

    def get_worlds_by_status(self, status: str) -> list[WorldRow]:
        """Return all worlds with the given status.

        F7-E.2: logically deleted worlds are excluded (decision 1).
        """
        s = self._session
        return s.scalars(
            select(WorldRow).where(
                WorldRow.status == status,
                WorldRow.deleted_at.is_(None),
            )
        ).all()

    def update_status(self, world_id: int, status: str) -> bool:
        """Set the operational status of a world. Returns False if missing.

        All database operations (query, update, commit/flush) run inside a
        protected block.  On exception:
        - Owned session: rollback then re-raise.
        - External session: re-raise without rollback (caller decides).
        """
        s = self._session
        try:
            # F7-E.2: filtered load; a logically deleted world behaves
            # exactly like a missing one (decision 5 -> return False).
            row = s.scalars(
                select(WorldRow).where(
                    WorldRow.id == world_id,
                    WorldRow.deleted_at.is_(None),
                )
            ).first()
            if row is None:
                return False
            row.status = status
            self._commit_or_flush(s)
            return True
        except Exception:
            self._rollback(s)
            raise

    def update_checkpoint_metadata(
        self,
        world_id: int,
        *,
        last_checkpoint_at: str | None = None,
        months_simulated: int | None = None,
        increment_checkpoint_count: bool = False,
    ) -> bool:
        """Update checkpoint-related metadata on a world row.

        Returns False if the world row does not exist.

        All database operations run inside a protected block.  On exception:
        - Owned session: rollback then re-raise.
        - External session: re-raise without rollback (caller decides).
        """
        s = self._session
        try:
            # F7-E.2: filtered load; a logically deleted world is treated
            # as missing (decision 5 -> return False, no writes).
            row = s.scalars(
                select(WorldRow).where(
                    WorldRow.id == world_id,
                    WorldRow.deleted_at.is_(None),
                )
            ).first()
            if row is None:
                return False
            if last_checkpoint_at is not None:
                row.last_checkpoint_at = last_checkpoint_at
            if months_simulated is not None:
                row.months_simulated = months_simulated
            if increment_checkpoint_count:
                row.checkpoint_count = (row.checkpoint_count or 0) + 1
            self._commit_or_flush(s)
            return True
        except Exception:
            self._rollback(s)
            raise


class CheckpointRepository:
    """Read/write access to the ``world_checkpoints`` table.

    All checkpoint payloads are stored as UTF-8 encoded JSON (v2 format) in
    the ``payload`` BLOB column, accompanied by a SHA-256 checksum so that
    corruption can be detected before restoration.
    """

    def __init__(self, session: Session | sessionmaker | None = None) -> None:
        # Determine session source and ownership based on the object we will
        # actually use, not just on whether the original argument was None.
        if session is None:
            # No session provided: create one internally, we own it.
            self._session_obj: Session | None = None
            self._owns_session = True
        elif _is_sessionmaker_instance(session):
            # sessionmaker instance: create a new session, we own it.
            self._session_obj = session()
            self._owns_session = True
        elif _is_sessionmaker_class(session):
            # sessionmaker class: create a new session, we own it.
            self._session_obj = session()
            self._owns_session = True
        else:
            # Session instance: use it directly, caller owns it.
            self._session_obj = session
            self._owns_session = False

    def __enter__(self) -> "CheckpointRepository":
        if self._session_obj is None:
            self._session_obj = SessionLocal()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._owns_session and self._session_obj is not None:
            self._session_obj.close()

    def close(self) -> None:
        if self._owns_session and self._session_obj is not None:
            self._session_obj.close()
            self._session_obj = None

    @property
    def _session(self) -> Session:
        """Return the active session, creating one lazily if needed."""
        if self._session_obj is None:
            self._session_obj = SessionLocal()
            self._owns_session = True
        return self._session_obj

    def _commit_or_flush(self, s: Session) -> None:
        """Commit if we own the session, otherwise just flush (caller commits)."""
        if self._owns_session:
            s.commit()
        else:
            s.flush()

    def _rollback(self, s: Session) -> None:
        """Rollback only if we own the session. External sessions are left
        untouched; the caller is responsible for rollback."""
        if self._owns_session:
            s.rollback()

    def save_checkpoint(
        self,
        world_id: int,
        sequence: int,
        month: int,
        checkpoint_type: str,
        payload: bytes,
        checksum: str,
        checkpoint_version: int = 2,
    ) -> int:
        """Persist a checkpoint atomically.

        Inserts the checkpoint row and updates the corresponding world row
        (last_checkpoint_at, months_simulated, checkpoint_count) in a single
        transaction.  Returns the new checkpoint row id.

        Raises ValueError if the world does not exist.

        All database operations run inside a protected block.  On exception:
        - Owned session: rollback then re-raise (no partial metadata).
        - External session: re-raise without rollback (caller decides).
        """
        s = self._session
        try:
            # Verify the world exists before writing a checkpoint for it.
            # F7-E.2: filtered load; a logically deleted world raises the
            # same contractual ValueError as a missing one (byte-identical
            # message; no checkpoint is persisted for it).
            world_row = s.scalars(
                select(WorldRow).where(
                    WorldRow.id == world_id,
                    WorldRow.deleted_at.is_(None),
                )
            ).first()
            if world_row is None:
                raise ValueError(f"World {world_id} does not exist")

            checkpoint = WorldCheckpoint(
                world_id=world_id,
                sequence=sequence,
                month=month,
                checkpoint_type=checkpoint_type,
                payload=payload,
                checksum=checksum,
                created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                is_valid=1,
                checkpoint_version=checkpoint_version,
            )
            s.add(checkpoint)
            s.flush()  # assign checkpoint.id before the world update

            # Update world metadata in the same transaction.
            s.execute(
                text(
                    """
                    UPDATE worlds
                    SET last_checkpoint_at = :created_at,
                        months_simulated = :month,
                        checkpoint_count = checkpoint_count + 1
                    WHERE id = :world_id
                    """
                ),
                {
                    "created_at": checkpoint.created_at,
                    "month": month,
                    "world_id": world_id,
                },
            )
            self._commit_or_flush(s)
            return checkpoint.id
        except Exception:
            self._rollback(s)
            raise

    def get_checkpoint_by_id(self, checkpoint_id: int) -> WorldCheckpoint | None:
        s = self._session
        return s.get(WorldCheckpoint, checkpoint_id)

    def get_checkpoints_for_world(
        self, world_id: int, limit: int | None = None
    ) -> list[WorldCheckpoint]:
        s = self._session
        q = select(WorldCheckpoint).where(WorldCheckpoint.world_id == world_id)
        q = q.order_by(WorldCheckpoint.sequence.desc())
        if limit is not None:
            q = q.limit(limit)
        return s.scalars(q).all()

    def get_valid_checkpoints_for_world(
        self, world_id: int
    ) -> list[WorldCheckpoint]:
        """Return valid checkpoints for a world, ordered by sequence desc.

        Only checkpoints with ``is_valid == 1`` are returned.  Invalid
        checkpoints (previously rejected as corrupt) are excluded so they
        are not re-evaluated on subsequent recoveries.
        """
        s = self._session
        q = select(WorldCheckpoint).where(
            WorldCheckpoint.world_id == world_id,
            WorldCheckpoint.is_valid == 1,
        )
        q = q.order_by(WorldCheckpoint.sequence.desc())
        return s.scalars(q).all()

    def get_latest_valid_checkpoint(self, world_id: int) -> WorldCheckpoint | None:
        """Return the newest valid checkpoint for a world, or None.

        Only checkpoints with is_valid = 1 are considered.  The SHA-256
        checksum of every candidate is verified on the exact stored bytes
        before accepting it.  Checkpoints whose payload no longer matches
        their stored checksum are marked invalid (is_valid = 0) and skipped.
        Invalidations are persisted before returning, whether or not a valid
        checkpoint is found.  If no checkpoint is valid, None is returned.

        All database operations run inside a protected block.  On exception:
        - Owned session: rollback then re-raise.
        - External session: re-raise without rollback (caller decides).
        """
        s = self._session
        try:
            rows = s.scalars(
                select(WorldCheckpoint)
                .where(WorldCheckpoint.world_id == world_id)
                .where(WorldCheckpoint.is_valid == 1)
                .order_by(WorldCheckpoint.sequence.desc())
            ).all()

            latest_valid: WorldCheckpoint | None = None
            for row in rows:
                # Verify the checksum on the exact stored bytes before
                # accepting or interpreting the payload.
                actual = hashlib.sha256(row.payload).hexdigest()
                if actual == row.checksum:
                    latest_valid = row
                    break
                # Mark corrupt checkpoints so future lookups skip them quickly.
                row.is_valid = 0
            # Persist all invalidations before returning, whether or not a
            # valid checkpoint was found.
            self._commit_or_flush(s)
        except Exception:
            self._rollback(s)
            raise
        return latest_valid

    def next_sequence(self, world_id: int) -> int:
        """Return the next available checkpoint sequence number for a world.

        Uses MAX(sequence) + 1 guarded by a transaction.  This is the
        CHECKPOINT sequence (``WorldCheckpoint.sequence``), an independent
        monotonic counter per world — it is NOT the operation sequence
        allocator of F7-C (``worlds.next_operation_sequence``), which is
        the only one covered by the contractual prohibition of ``MAX+1``.
        In the MVP (single process, asyncio lock per world) this is
        sufficient to avoid collisions; multi-process deployments should
        use PostgreSQL advisory locks or an equivalent.
        """
        s = self._session
        row = s.scalars(
            select(
                # Use COALESCE so MAX returns 0 when there are no rows.
                func.coalesce(func.max(WorldCheckpoint.sequence), 0)
            ).where(WorldCheckpoint.world_id == world_id)
        ).first()
        return int(row) + 1

    def mark_checkpoint_invalid(self, checkpoint_id: int) -> bool:
        """Mark a checkpoint as invalid (``is_valid = 0``) and persist it.

        Returns True if the checkpoint was found and invalidated, False if
        the checkpoint does not exist.  Read, write and commit errors are
        propagated to the caller; no exception is swallowed.

        Session ownership follows the repository contract: an owned session
        is committed here; an externally-provided session is only flushed
        (the caller commits).
        """
        s = self._session
        try:
            row = s.get(WorldCheckpoint, checkpoint_id)
            if row is None:
                return False
            row.is_valid = 0
            self._commit_or_flush(s)
            return True
        except Exception:
            self._rollback(s)
            raise


class OperationRepository:
    """Read/write access to the ``world_operations`` table (F7-C).

    Manages the durable operation log with T1/T2 protocol support.
    """

    def __init__(self, session: Session | sessionmaker | None = None) -> None:
        if session is None:
            self._session_obj: Session | None = None
            self._owns_session = True
        elif _is_sessionmaker_instance(session):
            self._session_obj = session()
            self._owns_session = True
        elif _is_sessionmaker_class(session):
            self._session_obj = session()
            self._owns_session = True
        else:
            self._session_obj = session
            self._owns_session = False

    def __enter__(self) -> "OperationRepository":
        if self._session_obj is None:
            self._session_obj = SessionLocal()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._owns_session and self._session_obj is not None:
            self._session_obj.close()

    def close(self) -> None:
        if self._owns_session and self._session_obj is not None:
            self._session_obj.close()
            self._session_obj = None

    @property
    def _session(self) -> Session:
        if self._session_obj is None:
            self._session_obj = SessionLocal()
            self._owns_session = True
        return self._session_obj

    def _commit_or_flush(self, s: Session) -> None:
        if self._owns_session:
            s.commit()
        else:
            s.flush()

    def _rollback(self, s: Session) -> None:
        if self._owns_session:
            s.rollback()

    def get_operation_by_idempotency_key(
        self, world_id: int, requesting_user_id: int | None, client_key: str
    ) -> WorldOperation | None:
        """Find an operation by idempotency key (scope: world + user + key)."""
        s = self._session
        return s.scalars(
            select(WorldOperation).where(
                WorldOperation.world_id == world_id,
                WorldOperation.requesting_user_id == requesting_user_id,
                WorldOperation.client_idempotency_key == client_key,
            )
        ).first()

    def get_operation_by_id(self, world_id: int, operation_id: str) -> WorldOperation | None:
        s = self._session
        return s.scalars(
            select(WorldOperation).where(
                WorldOperation.world_id == world_id,
                WorldOperation.operation_id == operation_id,
            )
        ).first()

    def get_latest_operation(self, world_id: int) -> WorldOperation | None:
        """Return the operation with the highest operation_sequence for a world."""
        s = self._session
        return s.scalars(
            select(WorldOperation)
            .where(WorldOperation.world_id == world_id)
            .order_by(WorldOperation.operation_sequence.desc())
        ).first()

    def get_operations(self, world_id: int) -> list[WorldOperation]:
        """All operations of a world in total order (§16.5)."""
        s = self._session
        return list(
            s.scalars(
                select(WorldOperation)
                .where(WorldOperation.world_id == world_id)
                .order_by(WorldOperation.operation_sequence)
            ).all()
        )

    def get_unresolved_operation(self, world_id: int) -> WorldOperation | None:
        """Highest-sequence operation still ``pending``/``executing``.

        Only these two statuses are recoverable (positive list, §16.11);
        ``blocked``/``failed``/``canceled``/``confirmed`` are never returned
        here because they are never re-executed automatically.
        """
        s = self._session
        return s.scalars(
            select(WorldOperation)
            .where(
                WorldOperation.world_id == world_id,
                WorldOperation.status.in_(("pending", "executing")),
            )
            .order_by(WorldOperation.operation_sequence.desc())
        ).first()

    def get_operations_by_status(self, world_id: int, *statuses: str) -> list[WorldOperation]:
        s = self._session
        return s.scalars(
            select(WorldOperation)
            .where(WorldOperation.world_id == world_id)
            .where(WorldOperation.status.in_(statuses))
            .order_by(WorldOperation.operation_sequence)
        ).all()

    def create_operation(
        self,
        world_id: int,
        operation_id: str,
        operation_sequence: int,
        requesting_user_id: int | None,
        client_idempotency_key: str | None,
        request_hash: str | None,
        engine_version: str | None,
        rng_state_ref: str | None,
        initial_checkpoint_id: int | None,
    ) -> WorldOperation:
        """Create a new pending operation (T1 reservation)."""
        s = self._session
        try:
            op = WorldOperation(
                world_id=world_id,
                operation_id=operation_id,
                operation_sequence=operation_sequence,
                status="pending",
                continuity_state="active",
                created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                requesting_user_id=requesting_user_id,
                client_idempotency_key=client_idempotency_key,
                request_hash=request_hash,
                engine_version=engine_version,
                rng_state_ref=rng_state_ref,
                initial_checkpoint_id=initial_checkpoint_id,
                recovery_generation=0,
            )
            s.add(op)
            self._commit_or_flush(s)
            return op
        except Exception:
            self._rollback(s)
            raise

    def update_operation_status(
        self,
        world_id: int,
        operation_id: str,
        status: str,
        continuity_state: str | None = None,
        confirmed_at: str | None = None,
        checkpoint_id: int | None = None,
        recovery_generation: int | None = None,
        recovery_attempt_id: str | None = None,
        recovery_state: str | None = None,
    ) -> bool:
        """Update operation status and optional fields."""
        s = self._session
        try:
            op = s.scalars(
                select(WorldOperation).where(
                    WorldOperation.world_id == world_id,
                    WorldOperation.operation_id == operation_id,
                )
            ).first()
            if op is None:
                return False
            op.status = status
            if continuity_state is not None:
                op.continuity_state = continuity_state
            if confirmed_at is not None:
                op.confirmed_at = confirmed_at
            if checkpoint_id is not None:
                op.checkpoint_id = checkpoint_id
            if recovery_generation is not None:
                op.recovery_generation = recovery_generation
            if recovery_attempt_id is not None:
                op.recovery_attempt_id = recovery_attempt_id
            if recovery_state is not None:
                op.recovery_state = recovery_state
            self._commit_or_flush(s)
            return True
        except Exception:
            self._rollback(s)
            raise

    def get_next_operation_sequence(self, world_id: int) -> int:
        """Read ``worlds.next_operation_sequence`` (F7-C durable allocator).

        This must be called within the T1 ``BEGIN IMMEDIATE`` transaction;
        the serialized write transaction is what provides exclusion (never
        ``SELECT ... FOR UPDATE``, which SQLite does not support, and never
        ``MAX(operation_sequence)+1``).
        """
        row = self._session.execute(
            text("SELECT next_operation_sequence FROM worlds WHERE id = :wid"),
            {"wid": world_id},
        ).first()
        if row is None:
            raise ValueError(f"World {world_id} does not exist")
        return int(row[0])

    def increment_operation_sequence(self, world_id: int) -> None:
        """Increment ``worlds.next_operation_sequence`` by one.

        Runs INSIDE the caller's transaction and never commits: the counter
        increment, the sequence reservation and the operation insert are
        committed atomically by T1 (rollback reverts all three together, so
        a failed T1 consumes no sequence).
        """
        self._session.execute(
            text(
                "UPDATE worlds SET next_operation_sequence = "
                "next_operation_sequence + 1 WHERE id = :wid"
            ),
            {"wid": world_id},
        )

    def get_operations_by_status(
        self, world_id: int, *statuses: str
    ) -> list[WorldOperation]:
        s = self._session
        return s.scalars(
            select(WorldOperation)
            .where(WorldOperation.world_id == world_id)
            .where(WorldOperation.status.in_(statuses))
            .order_by(WorldOperation.operation_sequence)
        ).all()

    def get_pending_executing_operations(self, world_id: int) -> list[WorldOperation]:
        """Get operations that can be recovered (pending or executing)."""
        return self.get_operations_by_status(world_id, "pending", "executing")


class EventRepository:
    """Read/write access to the ``world_events`` table (F7-C)."""

    def __init__(self, session: Session | sessionmaker | None = None) -> None:
        if session is None:
            self._session_obj: Session | None = None
            self._owns_session = True
        elif _is_sessionmaker_instance(session):
            self._session_obj = session()
            self._owns_session = True
        elif _is_sessionmaker_class(session):
            self._session_obj = session()
            self._owns_session = True
        else:
            self._session_obj = session
            self._owns_session = False

    def __enter__(self) -> "EventRepository":
        if self._session_obj is None:
            self._session_obj = SessionLocal()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._owns_session and self._session_obj is not None:
            self._session_obj.close()

    def close(self) -> None:
        if self._owns_session and self._session_obj is not None:
            self._session_obj.close()
            self._session_obj = None

    @property
    def _session(self) -> Session:
        if self._session_obj is None:
            self._session_obj = SessionLocal()
            self._owns_session = True
        return self._session_obj

    def _commit_or_flush(self, s: Session) -> None:
        if self._owns_session:
            s.commit()
        else:
            s.flush()

    def _rollback(self, s: Session) -> None:
        if self._owns_session:
            s.rollback()

    def bulk_insert_events(self, events: list[WorldEvent]) -> int:
        """Bulk insert events (called in T2). Returns count inserted."""
        s = self._session
        try:
            if not events:
                return 0
            s.add_all(events)
            self._commit_or_flush(s)
            return len(events)
        except Exception:
            self._rollback(s)
            raise

    def _confirmed_query(
        self,
        world_id: int,
        month: int | None = None,
        event_type: str | None = None,
        kingdom_id: int | None = None,
    ):
        """Base query: events whose operation reached ``confirmed`` in T2.

        The join with ``world_operations`` keeps the §16.2 invariant explicit
        (an event is confirmed only when its operation is), so a hypothetical
        orphan event row can never leak into the public history.
        """
        q = (
            select(WorldEvent)
            .join(
                WorldOperation,
                and_(
                    WorldEvent.world_id == WorldOperation.world_id,
                    WorldEvent.operation_id == WorldOperation.operation_id,
                ),
            )
            .where(
                WorldEvent.world_id == world_id,
                WorldOperation.status == "confirmed",
            )
        )
        if month is not None:
            q = q.where(WorldEvent.month == month)
        if event_type is not None:
            q = q.where(WorldEvent.event_type == event_type)
        if kingdom_id is not None:
            # ``kingdom_ids`` is a JSON array: structural membership test.
            # A LIKE '%1%' would also match 10, 21, 31... (false positives).
            q = q.where(
                text(
                    "EXISTS (SELECT 1 FROM json_each(world_events.kingdom_ids) "
                    "AS je WHERE je.value = :kingdom_id)"
                )
            ).params(kingdom_id=kingdom_id)
        return q

    def get_events_page(
        self,
        world_id: int,
        limit: int,
        cursor: tuple[int, int] | None = None,
        snapshot_high_watermark: tuple[int, int] | None = None,
        month: int | None = None,
        event_type: str | None = None,
        kingdom_id: int | None = None,
    ) -> tuple[list[WorldEvent], tuple[int, int] | None]:
        """Fetch a page of events with snapshot semantics.

        Order is the total order of §16.5: ``(operation_sequence ASC,
        position ASC)``.  Returns ``(events, next_cursor)`` where
        ``next_cursor`` is the position of the last delivered event ONLY when
        more results exist inside the high-water mark, else ``None``
        (CONTRACTS §16.4).
        """
        s = self._session
        try:
            q = self._confirmed_query(world_id, month, event_type, kingdom_id)

            # Snapshot high-water mark: fixed upper bound of the navigation.
            if snapshot_high_watermark is not None:
                hw_seq, hw_pos = snapshot_high_watermark
                q = q.where(
                    (WorldEvent.operation_sequence < hw_seq)
                    | (
                        (WorldEvent.operation_sequence == hw_seq)
                        & (WorldEvent.position <= hw_pos)
                    )
                )

            # Continuation cursor: strictly after the cut point.
            if cursor is not None:
                cur_seq, cur_pos = cursor
                q = q.where(
                    (WorldEvent.operation_sequence > cur_seq)
                    | (
                        (WorldEvent.operation_sequence == cur_seq)
                        & (WorldEvent.position > cur_pos)
                    )
                )

            q = q.order_by(
                WorldEvent.operation_sequence, WorldEvent.position
            ).limit(limit + 1)

            rows = list(s.scalars(q).all())
            has_more = len(rows) > limit
            if has_more:
                rows = rows[:limit]

            next_cursor = None
            if has_more and rows:
                last = rows[-1]
                next_cursor = (last.operation_sequence, last.position)

            return rows, next_cursor
        except Exception:
            self._rollback(s)
            raise

    def get_last_confirmed_event(
        self,
        world_id: int,
        month: int | None = None,
        event_type: str | None = None,
        kingdom_id: int | None = None,
    ) -> tuple[int, int] | None:
        """Position ``(operation_sequence, position)`` of the LAST confirmed
        event visible for these filters, or ``None`` when there is none.

        Used to fix ``snapshot_high_watermark`` on the first query of a
        navigation (§16.13): the DESCENDING order with ``limit 1`` is what
        makes it the last one, not an arbitrary row.
        """
        s = self._session
        try:
            q = self._confirmed_query(world_id, month, event_type, kingdom_id)
            q = q.order_by(
                WorldEvent.operation_sequence.desc(),
                WorldEvent.position.desc(),
            ).limit(1)
            row = s.scalars(q).first()
            if row is None:
                return None
            return (row.operation_sequence, row.position)
        except Exception:
            self._rollback(s)
            raise

    def get_events_by_operation(
        self, world_id: int, operation_id: str
    ) -> list[WorldEvent]:
        s = self._session
        return s.scalars(
            select(WorldEvent)
            .where(WorldEvent.world_id == world_id)
            .where(WorldEvent.operation_id == operation_id)
            .order_by(WorldEvent.position)
        ).all()


class ResolutionRepository:
    """Administrative resolutions for failed operations (F7-C)."""

    def __init__(self, session: Session | sessionmaker | None = None) -> None:
        if session is None:
            self._session_obj: Session | None = None
            self._owns_session = True
        elif _is_sessionmaker_instance(session):
            self._session_obj = session()
            self._owns_session = True
        elif _is_sessionmaker_class(session):
            self._session_obj = session()
            self._owns_session = True
        else:
            self._session_obj = session
            self._owns_session = False

    def __enter__(self) -> "ResolutionRepository":
        if self._session_obj is None:
            self._session_obj = SessionLocal()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._owns_session and self._session_obj is not None:
            self._session_obj.close()

    def close(self) -> None:
        if self._owns_session and self._session_obj is not None:
            self._session_obj.close()
            self._session_obj = None

    @property
    def _session(self) -> Session:
        if self._session_obj is None:
            self._session_obj = SessionLocal()
            self._owns_session = True
        return self._session_obj

    def _commit_or_flush(self, s: Session) -> None:
        if self._owns_session:
            s.commit()
        else:
            s.flush()

    def _rollback(self, s: Session) -> None:
        if self._owns_session:
            s.rollback()

    def create_resolution(
        self,
        world_id: int,
        operation_id: str,
        operation_sequence: int,
        admin_user_id: int,
        resolution_type: str,
        reason: str,
        evidence_refs: str | None,
        idempotency_key: str,
    ) -> OperationResolution:
        s = self._session
        try:
            resolution = OperationResolution(
                world_id=world_id,
                operation_id=operation_id,
                operation_sequence=operation_sequence,
                admin_user_id=admin_user_id,
                resolution_type=resolution_type,
                reason=reason,
                evidence_refs=evidence_refs,
                resolution_version=1,
                created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                idempotency_key=idempotency_key,
            )
            s.add(resolution)
            self._commit_or_flush(s)
            return resolution
        except Exception:
            self._rollback(s)
            raise

    def get_resolution(
        self, world_id: int, operation_id: str, idempotency_key: str
    ) -> OperationResolution | None:
        s = self._session
        return s.scalars(
            select(OperationResolution).where(
                OperationResolution.world_id == world_id,
                OperationResolution.operation_id == operation_id,
                OperationResolution.idempotency_key == idempotency_key,
            )
        ).first()

    def get_resolutions_for_operation(
        self, world_id: int, operation_id: str
    ) -> list[OperationResolution]:
        """All resolutions recorded for an operation (audit trail)."""
        s = self._session
        return s.scalars(
            select(OperationResolution)
            .where(
                OperationResolution.world_id == world_id,
                OperationResolution.operation_id == operation_id,
            )
            .order_by(OperationResolution.id)
        ).all()


class ChallengeRepository:
    """Read/write access to the ``challenges`` table (F7-D).

    Manages durable challenge operations with idempotency keys and
    request_hash verification (§17.12, DEC-F7D-15).
    """

    def __init__(self, session: Session | sessionmaker | None = None) -> None:
        if session is None:
            self._session_obj: Session | None = None
            self._owns_session = True
        elif _is_sessionmaker_instance(session):
            self._session_obj = session()
            self._owns_session = True
        elif _is_sessionmaker_class(session):
            self._session_obj = session()
            self._owns_session = True
        else:
            self._session_obj = session
            self._owns_session = False

    def __enter__(self) -> "ChallengeRepository":
        if self._session_obj is None:
            self._session_obj = SessionLocal()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._owns_session and self._session_obj is not None:
            self._session_obj.close()

    def close(self) -> None:
        if self._owns_session and self._session_obj is not None:
            self._session_obj.close()
            self._session_obj = None

    @property
    def _session(self) -> Session:
        if self._session_obj is None:
            self._session_obj = SessionLocal()
            self._owns_session = True
        return self._session_obj

    def _commit_or_flush(self, s: Session) -> None:
        if self._owns_session:
            s.commit()
        else:
            s.flush()

    def _rollback(self, s: Session) -> None:
        if self._owns_session:
            s.rollback()

    def get_challenge(self, challenge_id: int) -> ChallengeRow | None:
        s = self._session
        return s.get(ChallengeRow, challenge_id)

    def get_challenge_creation_request(
        self, actor_user_id: int, client_idempotency_key: str
    ) -> ChallengeCreationRequestRow | None:
        """Busca la identidad idempotente de creación por actor + clave."""
        s = self._session
        return s.scalars(
            select(ChallengeCreationRequestRow).where(
                ChallengeCreationRequestRow.actor_user_id == actor_user_id,
                ChallengeCreationRequestRow.client_idempotency_key
                == client_idempotency_key,
            )
        ).first()

    def record_challenge_creation_request(
        self,
        actor_user_id: int,
        client_idempotency_key: str,
        request_hash: str,
        challenge_id: int,
    ) -> ChallengeCreationRequestRow:
        """Registra la clave de creación en la misma transacción del desafío.

        La restricción UNIQUE(actor_user_id, client_idempotency_key) arbitra
        las carreras concurrentes. El llamador controla commit/rollback.
        """
        s = self._session
        row = ChallengeCreationRequestRow(
            actor_user_id=actor_user_id,
            client_idempotency_key=client_idempotency_key,
            request_hash=request_hash,
            challenge_id=challenge_id,
            created_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        )
        s.add(row)
        s.flush()
        return row

    def get_challenge_operation(
        self, challenge_id: int, actor_user_id: int, client_idempotency_key: str
    ) -> ChallengeOperationRow | None:
        """Busca el registro de procedencia dentro del ámbito de idempotencia
        CONTRACTS §17.13: ``(challenge_id, actor, client_idempotency_key)``.

        F7-D-I1-FIX10-A-IDEM: los endpoints lo usan para recuperar el
        resultado original ANTES de cualquier retorno temprano por estado
        (mismo hash → replay del resultado original; hash distinto → 409).
        """
        s = self._session
        return s.scalars(
            select(ChallengeOperationRow).where(
                ChallengeOperationRow.challenge_id == challenge_id,
                ChallengeOperationRow.actor_user_id == actor_user_id,
                ChallengeOperationRow.client_idempotency_key
                == client_idempotency_key,
            )
        ).first()

    def get_challenges_by_state(self, state: str) -> list[ChallengeRow]:
        s = self._session
        return list(
            s.scalars(select(ChallengeRow).where(ChallengeRow.state == state)).all()
        )

    def record_challenge_operation(
        self,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None,
        operation: str,
        challenge_id: int,
        outcome: str | None = None,
    ) -> None:
        """Registra de forma durable una operación global de desafío en la
        MISMA transacción del llamador (F7-D-I1-FIX10-A-FINAL).

        Contrato: CONTRACTS §17.7 (resolución auditable: actor, fecha,
        identidad de operación, resultado, idempotencia) y §17.13 (identidad
        de petición idempotente por ``(challenge_id, actor,
        client_idempotency_key)``, ámbito de idempotencia del contrato).

        El INSERT se hace con ``flush()`` sobre la sesión del llamador, sin
        commit propio: el cambio de estado y este registro se confirman
        juntos en el COMMIT del servicio. Si el COMMIT falla, el rollback
        elimina ambos (sin estado parcial ni registro huérfano).

        Idempotencia defensiva en la propia capa de registro:
        - misma clave + mismo ``request_hash`` → no inserta duplicado
          (reintento idempotente);
        - misma clave + ``request_hash`` distinto → ``IdempotencyConflict``
          (HTTP 409, CONTRACTS §17.9).
        """
        s = self._session
        existing = s.scalars(
            select(ChallengeOperationRow).where(
                ChallengeOperationRow.challenge_id == challenge_id,
                ChallengeOperationRow.actor_user_id == organizer_user_id,
                ChallengeOperationRow.client_idempotency_key
                == client_idempotency_key,
            )
        ).first()
        if existing is not None:
            if (existing.request_hash or "") == (request_hash or ""):
                return  # reintento idempotente: nunca duplicar el registro
            # Import local: challenge_service importa este módulo a nivel de
            # módulo; el import en tiempo de ejecución evita el ciclo.
            from worldsim.server.challenge_service import IdempotencyConflict

            raise IdempotencyConflict()

        s.add(
            ChallengeOperationRow(
                challenge_id=challenge_id,
                operation=operation,
                actor_user_id=organizer_user_id,
                client_idempotency_key=client_idempotency_key,
                request_hash=request_hash or "",
                outcome=outcome,
                created_at=time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.gmtime()
                ),
            )
        )
        s.flush()


class ChallengeParticipantRepository:
    """Read/write access to the ``challenge_participants`` table (F7-D)."""

    def __init__(self, session: Session | sessionmaker | None = None) -> None:
        if session is None:
            self._session_obj: Session | None = None
            self._owns_session = True
        elif _is_sessionmaker_instance(session):
            self._session_obj = session()
            self._owns_session = True
        elif _is_sessionmaker_class(session):
            self._session_obj = session()
            self._owns_session = True
        else:
            self._session_obj = session
            self._owns_session = False

    def __enter__(self) -> "ChallengeParticipantRepository":
        if self._session_obj is None:
            self._session_obj = SessionLocal()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._owns_session and self._session_obj is not None:
            self._session_obj.close()

    def close(self) -> None:
        if self._owns_session and self._session_obj is not None:
            self._session_obj.close()
            self._session_obj = None

    @property
    def _session(self) -> Session:
        if self._session_obj is None:
            self._session_obj = SessionLocal()
            self._owns_session = True
        return self._session_obj

    def _commit_or_flush(self, s: Session) -> None:
        if self._owns_session:
            s.commit()
        else:
            s.flush()

    def _rollback(self, s: Session) -> None:
        if self._owns_session:
            s.rollback()

    def get_participant(
        self, challenge_id: int, user_id: int
    ) -> ChallengeParticipantRow | None:
        s = self._session
        return s.scalars(
            select(ChallengeParticipantRow).where(
                ChallengeParticipantRow.challenge_id == challenge_id,
                ChallengeParticipantRow.user_id == user_id,
            )
        ).first()

    def get_participants(self, challenge_id: int) -> list[ChallengeParticipantRow]:
        s = self._session
        return list(
            s.scalars(
                select(ChallengeParticipantRow).where(
                    ChallengeParticipantRow.challenge_id == challenge_id
                )
            ).all()
        )

    def get_participant_by_world(self, world_id: int) -> ChallengeParticipantRow | None:
        s = self._session
        return s.scalars(
            select(ChallengeParticipantRow).where(
                ChallengeParticipantRow.world_id == world_id
            )
        ).first()

    def get_participants_without_world(
        self, challenge_id: int
    ) -> list[ChallengeParticipantRow]:
        s = self._session
        return list(
            s.scalars(
                select(ChallengeParticipantRow).where(
                    ChallengeParticipantRow.challenge_id == challenge_id,
                    ChallengeParticipantRow.world_id.is_(None),
                )
            ).all()
        )

    def save_participant(self, participant: ChallengeParticipantRow) -> None:
        s = self._session
        try:
            s.add(participant)
            self._commit_or_flush(s)
        except Exception:
            self._rollback(s)
            raise

    def create_participant(
        self,
        challenge_id: int,
        user_id: int,
        state: str = "inscrito",
    ) -> ChallengeParticipantRow:
        s = self._session
        try:
            participant = ChallengeParticipantRow(
                challenge_id=challenge_id,
                user_id=user_id,
                world_id=None,
                state=state,
                joined_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                result_status="calculated",
                public_alias=uuid.uuid4().hex,
            )
            s.add(participant)
            self._commit_or_flush(s)
            return participant
        except Exception:
            self._rollback(s)
            raise
