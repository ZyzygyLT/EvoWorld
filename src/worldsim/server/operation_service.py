"""Operation Service for F7-C: durable T1/T2 protocol (CONTRACTS §16).

Implements the durable operation protocol of ``docs/architecture/CONTRACTS.md``
§16.11 and the recovery protocol of §16.14:

* **T1** — reservation of an operation inside a serialized SQLite write
  transaction (``BEGIN IMMEDIATE``): idempotency-key lookup, admission
  decision over the full ``(status, continuity_state)`` combination of the
  highest-sequence operation, durable sequence allocation from
  ``worlds.next_operation_sequence`` and insert of the operation — all
  committed atomically.  Never ``MAX(operation_sequence)+1`` and never
  ``SELECT ... FOR UPDATE`` (SQLite has no row locks; exclusion comes from
  the serialized write transaction).
* **T2** — ONE transaction that writes the resulting checkpoint, the event
  delta of the operation and the ``executing → confirmed`` transition,
  verifying the recovery fencing generation inside that same transaction.
* **Recovery** — positive-list acquisition (only ``pending``/``executing``),
  determinism verification BEFORE incrementing ``recovery_generation``,
  reconstruction of the engine from ``initial_checkpoint_id`` and
  re-execution confirmed through T2 with the acquired fencing token.
* **Terminal states** — safe cancellation (``pending → canceled``) and
  administrative resolution of ``failed`` operations, each atomic with the
  continuity release it performs.

Session rule used throughout: repositories receive the SAME session instance
they run in (external session → ``flush`` only), and the service owns the
transaction boundary (``BEGIN IMMEDIATE`` … ``commit``/``rollback``).  That is
what makes T1 and T2 single transactions across several repositories.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from worldsim.export.checkpoint import (
    CHECKPOINT_VERSION,
    checkpoint_to_dict,
    engine_from_checkpoint,
)
from worldsim.server.db import (
    OperationResolution,
    WorldCheckpoint,
    WorldEvent,
    WorldOperation,
    WorldRow,
)
from worldsim.server.repository import (
    CheckpointRepository,
    EventRepository,
    OperationRepository,
    ResolutionRepository,
    WorldRepository,
)
from worldsim.sim.engine import SimEngine
from worldsim.sim.models import Event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors (each maps to an explicit contract outcome; never swallowed here)
# ---------------------------------------------------------------------------


class OperationError(RuntimeError):
    """Base class for durable-operation protocol errors."""


class IdempotencyConflict(OperationError):
    """Same ``client_idempotency_key`` with a different ``request_hash``
    (§16.11: HTTP 409 ``idempotency_conflict``)."""

    def __init__(self) -> None:
        super().__init__("idempotency_conflict")


class WorldBlocked(OperationError):
    """T1 admission denied: highest-sequence operation is incompatible
    (§16.11: HTTP 409 ``world_blocked``)."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(f"world_blocked{': ' + detail if detail else ''}")


class FencingError(OperationError):
    """T2 fencing verification failed: the generation this executor holds is
    no longer the durable one (§16.11, Ejemplo C)."""

    def __init__(self, expected: int, current: int) -> None:
        self.expected = expected
        self.current = current
        super().__init__(
            f"fencing_failed: recovery_generation expected {expected}, "
            f"durable value is {current}"
        )


class NotRecoverable(OperationError):
    """Acquisition refused: the operation is not in the positive recoverable
    list (``pending``/``executing``).  No generation is consumed."""


class DeterminismBlocked(OperationError):
    """§16.14 conditions not satisfied: the operation transitioned to
    ``blocked`` (never ``failed``) and the world does not advance."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"operation not reproducible: {reason}")


class InvalidTransition(OperationError):
    """A state transition outside the §16.10 table was attempted."""


class OperationNotFound(OperationError):
    """The operation identity does not exist for that world."""


class ResolutionConflict(OperationError):
    """A new administrative resolution would modify an already-committed
    resolution decision (§16.11: resolutions are immutable)."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationParams:
    """Canonical semantic parameters of a request (input of ``request_hash``).

    Only semantic fields belong here: no timestamps, no connection ids, no
    transport metadata (§16.11.2).
    """

    world_id: int
    operation_type: str  # e.g. "advance"
    months: int
    requested_version: Optional[str] = None  # requested execution version


@dataclass
class T1Result:
    """Result of T1 reservation."""

    operation: WorldOperation
    is_retry: bool  # True: idempotent retry, no new sequence consumed
    existing: bool


@dataclass
class T2Result:
    """Result of T2 confirmation."""

    checkpoint_id: int
    events: list
    operation_confirmed: bool


@dataclass
class RecoveryAcquire:
    """Durable acquisition of a recovery generation (fencing token)."""

    world_id: int
    operation_id: str
    operation_sequence: int
    generation: int
    attempt_id: str
    initial_checkpoint_id: int | None
    #: Decoded payload of the initial checkpoint (loaded once, reused by
    #: :meth:`OperationService.recover_operation` for reconstruction).
    initial_state: dict = field(default_factory=dict)


def admission_allowed(status: str, continuity_state: str) -> bool:
    """T1 admission decision over the FULL combination (§16.11 table).

    * ``confirmed`` + ``active``      → allow
    * ``failed``/``canceled`` + ``released`` → allow
    * everything else (``pending``, ``executing``, ``blocked``, and any
      terminal without operational release) → deny
    """
    if status == "confirmed" and continuity_state == "active":
        return True
    if status in ("failed", "canceled") and continuity_state == "released":
        return True
    return False


def _json_default(obj: Any) -> Any:
    """JSON fallback for event params (numpy scalars → native, else str)."""
    if hasattr(obj, "dtype") and hasattr(obj, "item"):
        try:
            return obj.item()
        except Exception:
            return str(obj)
    return str(obj)


def _begin_immediate(session) -> None:
    """Start the serialized SQLite write transaction used by T1 and by the
    transitions that release continuity (§16.11)."""
    session.execute(text("BEGIN IMMEDIATE"))


def _detach(session, obj):
    """Return ``obj`` fully loaded and detached from ``session``.

    ``commit()``/``rollback()`` expire ORM attributes; the session is closed
    right after, so any later attribute access on the returned instance
    would raise ``DetachedInstanceError``.  Refresh while still attached,
    then expunge: callers receive a plain value object they can read freely
    (this is what T1 results, recovery lookups and resolutions hand back).
    """
    session.refresh(obj)
    session.expunge(obj)
    return obj


def _is_sqlite_busy(exc: BaseException) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class OperationService:
    """Durable operation protocol (T1/T2 + recovery + terminal states)."""

    #: Bounded T1 contention policy (§16.11: configurable backoff, never an
    #: unlimited loop; contention is never reported as success).
    T1_MAX_ATTEMPTS = 5
    T1_BACKOFF_BASE_SECONDS = 0.02

    def __init__(
        self,
        session_factory,
        engine_version: str = "1.0",
    ) -> None:
        self.session_factory = session_factory
        self.engine_version = engine_version

    # ------------------------------------------------------------------
    # Canonicalization (§16.11.2)
    # ------------------------------------------------------------------

    def canonicalize_request(self, params: OperationParams) -> bytes:
        """Versioned canonical representation hashed into ``request_hash``.

        v1 includes: canonicalization version, ``world_id``, operation type,
        every semantic parameter (``months``) and the requested execution
        version.  It excludes reception timestamps, connection ids and
        transport metadata.
        """
        canonical = {
            "v": 1,
            "world_id": params.world_id,
            "operation_type": params.operation_type,
            "months": params.months,
            "requested_version": params.requested_version,
        }
        return json.dumps(
            canonical, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def compute_request_hash(self, params: OperationParams) -> str:
        """SHA-256 hex digest of the canonical request."""
        return hashlib.sha256(self.canonicalize_request(params)).hexdigest()

    # ------------------------------------------------------------------
    # T1: durable reservation (§16.11)
    # ------------------------------------------------------------------

    def reserve_operation(
        self,
        world_id: int,
        requesting_user_id: int,
        client_idempotency_key: str,
        params: OperationParams,
        request_hash: str | None = None,
    ) -> T1Result:
        """T1: reserve a durable operation inside ``BEGIN IMMEDIATE``.

        Ordering inside the transaction (contract steps 3.1–3.6):

        1. look up ``(world_id, requesting_user_id, client_idempotency_key)``;
        2. if it exists → verify ``request_hash`` and return the ORIGINAL
           operation (no new sequence, no engine execution) or raise
           :class:`IdempotencyConflict`;
        3. else decide admission from the highest-sequence operation's
           ``(status, continuity_state)`` → :class:`WorldBlocked` when denied;
        4. read ``worlds.next_operation_sequence``, increment it and insert
           the operation;
        5. commit — sequence reservation + counter increment + insert are one
           atomic transaction (rollback consumes no sequence).

        Contention (``SQLITE_BUSY``/``SQLITE_LOCKED``) is retried with a
        bounded exponential backoff; a UNIQUE violation caused by a
        concurrent request with the SAME key is resolved by returning the
        winning operation (§16.11 step 3.6).
        """
        if requesting_user_id is None:
            raise ValueError(
                "requesting_user_id is required: operations that need a "
                "durable identity demand an authenticated principal"
            )
        if not client_idempotency_key:
            raise ValueError("client_idempotency_key is required")
        if request_hash is None:
            request_hash = self.compute_request_hash(params)

        last_busy: BaseException | None = None
        for attempt in range(self.T1_MAX_ATTEMPTS):
            try:
                return self._reserve_once(
                    world_id, requesting_user_id, client_idempotency_key,
                    params, request_hash,
                )
            except IntegrityError as exc:
                # Concurrent winner on the idempotency tripleta: consult the
                # winning row and apply the SAME idempotency logic (§16.11).
                winner = self._lookup_after_unique_conflict(
                    world_id, requesting_user_id, client_idempotency_key,
                    request_hash,
                )
                if winner is not None:
                    return winner
                # Not the idempotency constraint (e.g. sequence uniqueness):
                # terminal error, never masked.
                raise exc
            except OperationalError as exc:
                if _is_sqlite_busy(exc) and attempt < self.T1_MAX_ATTEMPTS - 1:
                    last_busy = exc
                    time.sleep(
                        self.T1_BACKOFF_BASE_SECONDS * (2 ** attempt)
                    )
                    continue
                raise
        # Defensive: unreachable while T1_MAX_ATTEMPTS >= 1.
        raise last_busy if last_busy is not None else RuntimeError("T1 failed")

    def _reserve_once(
        self,
        world_id: int,
        requesting_user_id: int,
        client_idempotency_key: str,
        params: OperationParams,
        request_hash: str,
    ) -> T1Result:
        session = self.session_factory()
        try:
            _begin_immediate(session)
            op_repo = OperationRepository(session)  # external session → flush

            # 1. Idempotency key (inside the transaction).
            existing = op_repo.get_operation_by_idempotency_key(
                world_id=world_id,
                requesting_user_id=requesting_user_id,
                client_key=client_idempotency_key,
            )
            if existing is not None:
                session.rollback()
                if existing.request_hash != request_hash:
                    raise IdempotencyConflict()
                return T1Result(
                    operation=_detach(session, existing),
                    is_retry=True,
                    existing=True,
                )

            # 2. Admission over the highest-sequence operation.
            latest = op_repo.get_latest_operation(world_id)
            if latest is not None and not admission_allowed(
                latest.status, latest.continuity_state
            ):
                session.rollback()
                raise WorldBlocked(
                    f"latest operation seq={latest.operation_sequence} is "
                    f"{latest.status}+{latest.continuity_state}"
                )

            # 3. Durable sequence allocator: read, reserve, increment.
            sequence = op_repo.get_next_operation_sequence(world_id)
            op_repo.increment_operation_sequence(world_id)

            # 4. Insert the operation with its full contractual identity.
            op = op_repo.create_operation(
                world_id=world_id,
                operation_id=str(uuid.uuid4()),
                operation_sequence=sequence,
                requesting_user_id=requesting_user_id,
                client_idempotency_key=client_idempotency_key,
                request_hash=request_hash,
                engine_version=self.engine_version,
                rng_state_ref=self._rng_state_ref_for_new_op(world_id),
                initial_checkpoint_id=self._latest_checkpoint_id(world_id),
            )

            # 5. Commit T1 (reservation + counter + insert, atomically).
            session.commit()
            return T1Result(
                operation=_detach(session, op), is_retry=False, existing=False
            )
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def _lookup_after_unique_conflict(
        self,
        world_id: int,
        requesting_user_id: int,
        client_idempotency_key: str,
        request_hash: str,
    ) -> T1Result | None:
        """After a UNIQUE violation, return the winning operation when the
        conflict came from the idempotency tripleta; ``None`` otherwise."""
        session = self.session_factory()
        try:
            op_repo = OperationRepository(session)
            winner = op_repo.get_operation_by_idempotency_key(
                world_id=world_id,
                requesting_user_id=requesting_user_id,
                client_key=client_idempotency_key,
            )
            if winner is None:
                return None
            if winner.request_hash != request_hash:
                raise IdempotencyConflict()
            return T1Result(
                operation=_detach(session, winner), is_retry=True, existing=True
            )
        finally:
            session.close()

    def _latest_checkpoint_id(self, world_id: int) -> int | None:
        cp = self._latest_valid_checkpoint(world_id)
        return cp.id if cp is not None else None

    def _rng_state_ref_for_new_op(self, world_id: int) -> str | None:
        """Reference to the durable RNG state the operation starts from.

        The checkpoint payload contains the engine RNG state; the reference
        points at that checkpoint so §16.14 condition 3 can be verified.
        """
        cp = self._latest_valid_checkpoint(world_id)
        return str(cp.id) if cp is not None else None

    def _latest_valid_checkpoint(self, world_id: int) -> WorldCheckpoint | None:
        session = self.session_factory()
        try:
            cp_repo = CheckpointRepository(session)
            return cp_repo.get_latest_valid_checkpoint(world_id)
        finally:
            session.close()

    # ------------------------------------------------------------------
    # pending → executing (§16.10 / §16.11 step 4)
    # ------------------------------------------------------------------

    def mark_executing(self, world_id: int, operation_id: str) -> None:
        """Durably start execution: ``pending → executing``.

        Called only AFTER the T1 commit and BEFORE the engine tick, so a
        crash in between leaves a ``pending`` operation (recoverable) and a
        crash during the tick leaves ``executing`` (also recoverable).
        """
        session = self.session_factory()
        try:
            _begin_immediate(session)
            op = OperationRepository(session).get_operation_by_id(
                world_id, operation_id
            )
            if op is None:
                raise OperationNotFound(
                    f"operation {operation_id} not found for world {world_id}"
                )
            if op.status == "pending":
                op.status = "executing"
            elif op.status == "executing":
                pass  # idempotent re-entry
            else:
                raise InvalidTransition(
                    f"cannot start execution of {op.status!r} operation"
                )
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------
    # T2: atomic confirmation (§16.11)
    # ------------------------------------------------------------------

    def confirm_operation(
        self,
        world_id: int,
        operation_id: str,
        engine: SimEngine,
        events: list[Event],
        *,
        expected_generation: int = 0,
        checkpoint_type: str = "periodic",
    ) -> T2Result:
        """T2: checkpoint + event delta + ``executing → confirmed``, atomically.

        Everything happens on ONE SQLAlchemy session (one SQLite connection,
        one transaction): the checkpoint repository only flushes, the event
        rows are added to the same session and the operation row is updated
        in place — a single ``commit`` publishes all three or none.

        ``expected_generation`` is the fencing token this executor acquired
        (0 for ordinary execution).  It is compared against the DURABLE
        ``recovery_generation`` read inside this very transaction: a stale
        recoverer (Ejemplo C) fails here and rolls back completely.
        """
        # Serialize the engine OUTSIDE the transaction: serialization is
        # pure computation and must not hold the write lock.
        state = checkpoint_to_dict(engine, checkpoint_version=CHECKPOINT_VERSION)
        payload = json.dumps(state, ensure_ascii=False).encode("utf-8")
        checksum = hashlib.sha256(payload).hexdigest()
        month = int(engine.month)

        session = self.session_factory()
        try:
            _begin_immediate(session)
            op_repo = OperationRepository(session)
            cp_repo = CheckpointRepository(session)

            op = op_repo.get_operation_by_id(world_id, operation_id)
            if op is None:
                raise OperationNotFound(
                    f"operation {operation_id} not found for world {world_id}"
                )
            if op.status != "executing":
                raise InvalidTransition(
                    f"T2 requires status 'executing', operation is "
                    f"{op.status!r}"
                )
            durable_generation = int(op.recovery_generation or 0)
            if durable_generation != int(expected_generation):
                raise FencingError(expected_generation, durable_generation)

            # 1. Resulting checkpoint (same session/transaction).
            cp_id = cp_repo.save_checkpoint(
                world_id=world_id,
                sequence=cp_repo.next_sequence(world_id),
                month=month,
                checkpoint_type=checkpoint_type,
                payload=payload,
                checksum=checksum,
                checkpoint_version=CHECKPOINT_VERSION,
            )

            # 2. Event DELTA of this operation, 0-based positions (§16.13).
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            rows = [
                WorldEvent(
                    world_id=world_id,
                    operation_id=operation_id,
                    operation_sequence=op.operation_sequence,
                    position=i,  # 0-based, stable, unique within the op
                    month=int(event.month),
                    event_type=event.type.name,
                    event_text=event.text,
                    kingdom_ids=json.dumps(
                        [int(k) for k in event.kingdom_ids]
                    ),
                    event_params=json.dumps(
                        event.params, ensure_ascii=False, sort_keys=True,
                        default=_json_default,
                    ),
                    msg_key=event.msg_key,
                    created_at=now,
                )
                for i, event in enumerate(events)
            ]
            if rows:
                session.add_all(rows)

            # 3. Operation confirmation + continuity metadata.
            op.status = "confirmed"
            op.continuity_state = "active"
            op.confirmed_at = now
            op.checkpoint_id = cp_id

            session.commit()
            return T2Result(
                checkpoint_id=cp_id,
                events=list(events),
                operation_confirmed=True,
            )
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Baseline checkpoint (§16.14 condition 1)
    # ------------------------------------------------------------------

    def ensure_baseline_checkpoint(
        self, world_id: int, engine: SimEngine
    ) -> int | None:
        """Guarantee a valid reconstruction point BEFORE the first T1.

        F7-C histories start with no checkpoint at all (§16.7: nothing is
        imported), so the first operation would otherwise have
        ``initial_checkpoint_id = NULL`` and could never satisfy §16.14
        condition 1.  This writes a ``pre_tick`` checkpoint of the ALREADY
        durable engine state (the world row state) — it records no events and
        does not alter history or coverage.

        Returns the checkpoint id, or ``None`` when a valid checkpoint
        already exists.
        """
        session = self.session_factory()
        try:
            cp_repo = CheckpointRepository(session)
            existing = cp_repo.get_latest_valid_checkpoint(world_id)
            if existing is not None:
                return None
            state = checkpoint_to_dict(
                engine, checkpoint_version=CHECKPOINT_VERSION
            )
            payload = json.dumps(state, ensure_ascii=False).encode("utf-8")
            try:
                cp_id = cp_repo.save_checkpoint(
                    world_id=world_id,
                    sequence=cp_repo.next_sequence(world_id),
                    month=int(engine.month),
                    checkpoint_type="pre_tick",
                    payload=payload,
                    checksum=hashlib.sha256(payload).hexdigest(),
                    checkpoint_version=CHECKPOINT_VERSION,
                )
            except IntegrityError:
                # Concurrent baseline for the same world: the winner's
                # checkpoint is the baseline; nothing to invent.
                session.rollback()
                winner = cp_repo.get_latest_valid_checkpoint(world_id)
                return winner.id if winner is not None else None
            session.commit()
            return cp_id
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Recovery (§16.14, §16.15 scenarios B/C/D/G/J)
    # ------------------------------------------------------------------

    def get_unresolved_operation(self, world_id: int) -> WorldOperation | None:
        """Highest-sequence ``pending``/``executing`` operation, if any."""
        session = self.session_factory()
        try:
            op = OperationRepository(session).get_unresolved_operation(world_id)
            return _detach(session, op) if op is not None else None
        finally:
            session.close()

    def acquire_recovery(
        self,
        world_id: int,
        operation_id: str,
        params: OperationParams,
    ) -> RecoveryAcquire:
        """Acquire the current recovery generation (fencing token).

        Single ``BEGIN IMMEDIATE`` transaction that, in this order:

        1. loads the target operation;
        2. checks the POSITIVE recoverable list (``pending``/``executing``);
        3. verifies the §16.14 reproducibility conditions — BEFORE consuming
           any generation;
        4. only then increments ``recovery_generation``, mints a new
           ``recovery_attempt_id`` and persists ``recovery_state``;
        5. transitions ``pending → executing`` (execution may only start
           after T1, §16.11.4) and commits.

        When a condition fails the operation is durably transitioned to
        ``blocked`` (never ``failed``) with ``blocked_reason`` recorded, and
        :class:`DeterminismBlocked` is raised — the world does not advance.
        """
        session = self.session_factory()
        try:
            _begin_immediate(session)
            op_repo = OperationRepository(session)
            op = op_repo.get_operation_by_id(world_id, operation_id)
            if op is None:
                raise OperationNotFound(
                    f"operation {operation_id} not found for world {world_id}"
                )

            # Positive list (§16.11): blocked/failed/canceled/confirmed are
            # never acquired, so they can never increment a generation.
            if op.status not in ("pending", "executing"):
                raise NotRecoverable(
                    f"operation {operation_id} is {op.status!r}; only "
                    "'pending'/'executing' are recoverable"
                )

            ok, reason, state = self._verify_determinism(op, params, session)
            if not ok:
                self._persist_blocked(op, reason)
                session.commit()
                raise DeterminismBlocked(reason)

            # Acquisition (§16.11 steps 5–9): monotonic generation, fresh
            # attempt id, durable recovery_state — then commit.
            prev_attempts = 0
            try:
                prev = json.loads(op.recovery_state or "{}")
                if isinstance(prev, dict):
                    prev_attempts = int(prev.get("attempts") or 0)
            except (ValueError, TypeError, AttributeError):
                prev_attempts = 0

            op.recovery_generation = int(op.recovery_generation or 0) + 1
            op.recovery_attempt_id = str(uuid.uuid4())
            op.recovery_state = json.dumps(
                {
                    "attempts": prev_attempts + 1,
                    "deterministic": True,
                    "acquired_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "attempt_id": op.recovery_attempt_id,
                },
                sort_keys=True,
            )
            if op.status == "pending":
                op.status = "executing"

            acquired = RecoveryAcquire(
                world_id=world_id,
                operation_id=op.operation_id,
                operation_sequence=int(op.operation_sequence),
                generation=int(op.recovery_generation),
                attempt_id=op.recovery_attempt_id,
                initial_checkpoint_id=op.initial_checkpoint_id,
                initial_state=state,
            )
            session.commit()
            return acquired
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def recover_operation(
        self,
        world_id: int,
        operation_id: str,
        params: OperationParams,
        *,
        target_engine: SimEngine | None = None,
        checkpoint_type: str = "recovery",
    ) -> tuple[SimEngine, T2Result]:
        """Full recovery path: acquire → reconstruct → re-execute → T2.

        1. acquire the current generation (determinism verified first);
        2. reconstruct the engine from ``initial_checkpoint_id`` (checksum
           validated; RNG restored from the checkpoint payload);
        3. verify the reconstruction really matches the durable checkpoint;
        4. re-execute the operation's tick, taking the event delta with a
           marker over the cumulative ``Chronicle.events``;
        5. confirm through T2 with the ACQUIRED generation as fencing token.

        When ``target_engine`` is provided the reconstruction is applied
        IN PLACE (same object identity — DEC12 keeps one canonical engine
        per runner), preserving the operational ``paused`` mirror.
        """
        acquired = self.acquire_recovery(world_id, operation_id, params)

        try:
            engine = engine_from_checkpoint(acquired.initial_state)
        except Exception as exc:
            # Data verified by checksum but not reconstructible → the
            # operation cannot be replayed with guarantees → blocked.
            self._block_operation(
                world_id, operation_id,
                f"reconstruction_failed: {exc!r}",
            )
            raise DeterminismBlocked(
                f"reconstruction_failed: {exc!r}"
            ) from exc

        expected_month = acquired.initial_state.get("month")
        if expected_month is not None and int(engine.month) != int(
            expected_month
        ):
            self._block_operation(
                world_id, operation_id, "checkpoint_month_mismatch"
            )
            raise DeterminismBlocked("checkpoint_month_mismatch")

        if target_engine is not None:
            engine.paused = bool(getattr(target_engine, "paused", False))
            # Same object identity (DEC12): copy the reconstructed state
            # onto the runner's canonical engine instance.
            target_engine.__dict__.update(engine.__dict__)
            engine = target_engine
        else:
            engine.paused = False

        # Event delta: Chronicle.events is cumulative, so the marker taken
        # AFTER reconstruction isolates exactly this operation's events.
        marker = len(engine.chronicle.events)
        engine.tick()
        delta = list(engine.chronicle.events[marker:])

        t2 = self.confirm_operation(
            world_id,
            operation_id,
            engine,
            delta,
            expected_generation=acquired.generation,
            checkpoint_type=checkpoint_type,
        )
        return engine, t2

    def _verify_determinism(
        self, op: WorldOperation, params: OperationParams, session
    ) -> tuple[bool, str, dict]:
        """Verify ALL §16.14 reproducibility conditions that are data-checkable.

        Returns ``(ok, reason, decoded_initial_state)``.

        Conditions 5 (deterministic iteration order) and 6 (no external
        time/random sources) are properties of the ENGINE CODE: they are
        guaranteed by condition 4 (``engine_version`` equality — the same
        code runs) together with condition 3 (the engine RNG is restored
        from the checkpoint payload, so no unseeded randomness enters the
        replay).  They cannot be re-derived from row data at runtime.
        """
        # 4. Engine/rules version.
        if not op.engine_version or op.engine_version != self.engine_version:
            return False, "engine_version_mismatch", {}

        # 7. Identity preserved.
        if not op.operation_id or op.operation_sequence is None:
            return False, "operation_identity_missing", {}

        # 2. Canonical parameters reconstructible without ambiguity.
        if not op.request_hash:
            return False, "request_hash_missing", {}
        if self.compute_request_hash(params) != op.request_hash:
            return False, "request_params_ambiguous", {}

        # 1 + 3. Initial durable checkpoint, valid, with its RNG state.
        if op.initial_checkpoint_id is None:
            return False, "initial_checkpoint_missing", {}
        cp_repo = CheckpointRepository(session)
        cp = cp_repo.get_checkpoint_by_id(op.initial_checkpoint_id)
        if cp is None or cp.is_valid != 1:
            return False, "initial_checkpoint_invalid", {}
        if hashlib.sha256(cp.payload).hexdigest() != cp.checksum:
            return False, "initial_checkpoint_corrupt", {}

        if not op.rng_state_ref or op.rng_state_ref != str(cp.id):
            return False, "rng_state_mismatch", {}

        try:
            state = json.loads(cp.payload)
        except (ValueError, TypeError):
            return False, "initial_checkpoint_corrupt", {}
        if not isinstance(state, dict):
            return False, "initial_checkpoint_corrupt", {}

        # 8. Checkpoint/event schema compatibility with the current build.
        if state.get("_checkpoint_version") not in (1, 2):
            return False, "checkpoint_version_incompatible", {}
        if state.get("_checkpoint_type") != "simengine":
            return False, "initial_checkpoint_corrupt", {}
        if "rng_state" not in state:
            return False, "rng_state_missing", {}
        if cp.month is not None and state.get("month") is not None:
            if int(state["month"]) != int(cp.month):
                return False, "checkpoint_month_mismatch", {}

        return True, "", state

    def _persist_blocked(self, op: WorldOperation, reason: str) -> None:
        """Transition to ``blocked`` recording why (still inside the caller's
        transaction — committed together with the failed acquisition)."""
        op.status = "blocked"
        attempts = 0
        try:
            prev = json.loads(op.recovery_state or "{}")
            if isinstance(prev, dict):
                attempts = int(prev.get("attempts") or 0)
        except (ValueError, TypeError, AttributeError):
            attempts = 0
        op.recovery_state = json.dumps(
            {
                "attempts": attempts,
                "deterministic": False,
                "blocked_reason": reason,
                "blocked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            sort_keys=True,
        )

    def _block_operation(
        self, world_id: int, operation_id: str, reason: str
    ) -> None:
        """Durably block an operation outside an active transaction."""
        session = self.session_factory()
        try:
            _begin_immediate(session)
            op = OperationRepository(session).get_operation_by_id(
                world_id, operation_id
            )
            if op is None:
                session.rollback()
                return
            if op.status in ("pending", "executing"):
                self._persist_blocked(op, reason)
                session.commit()
            else:
                session.rollback()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Terminal states: safe cancellation (§16.10.7)
    # ------------------------------------------------------------------

    def cancel_operation(
        self,
        world_id: int,
        operation_id: str,
        requesting_user_id: int,
    ) -> bool:
        """Safe cancellation ``pending → canceled`` + release, atomically.

        Only allowed when it can be DEMONSTRATED durably that execution
        never began: the operation is still ``pending`` (the
        ``pending → executing`` transition happens before the tick), belongs
        to the requesting principal and has no persisted events or T2
        result.  Any uncertainty → ``False`` (the caller must NOT treat it
        as canceled; §16.10.7 sends uncertain cases to ``blocked``).

        Runs under ``BEGIN IMMEDIATE`` (world + operation exclusion).  The
        discontinuity is recorded by the operation row itself: coverage
        (§16.10.10/§16.12) derives ``discontinuity_sequence`` from the first
        non-confirmed operation, so status+continuity in ONE transaction is
        exactly the atomic registration the contract requires.
        """
        session = self.session_factory()
        try:
            _begin_immediate(session)
            op_repo = OperationRepository(session)
            event_repo = EventRepository(session)
            op = op_repo.get_operation_by_id(world_id, operation_id)
            if op is None:
                session.rollback()
                return False
            if op.status != "pending":
                session.rollback()
                return False
            if op.requesting_user_id != requesting_user_id:
                session.rollback()
                return False
            # Durable proof of "no effects": no events were ever persisted
            # for this operation and it has no confirmed T2 result.
            if event_repo.get_events_by_operation(world_id, operation_id):
                session.rollback()
                return False
            if op.checkpoint_id is not None or op.confirmed_at is not None:
                session.rollback()
                return False

            op.status = "canceled"
            op.continuity_state = "released"
            session.commit()
            return True
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Terminal states: administrative resolution (§16.11.3)
    # ------------------------------------------------------------------

    def resolve_failed_operation(
        self,
        world_id: int,
        operation_id: str,
        admin_user_id: int,
        resolution_type: str,
        reason: str,
        evidence_refs: str | None,
        idempotency_key: str,
    ) -> OperationResolution:
        """Register an administrative resolution of a ``failed`` operation.

        Atomic under ``BEGIN IMMEDIATE``: resolution row + continuity update
        commit together (release never happens without its record).

        * Same ``idempotency_key`` → returns the original record (idempotent,
          nothing is modified).
        * ``confirmed_no_effects`` / ``continue_with_gap`` →
          ``continuity_state = 'released'`` (the world may admit a NEW
          operation afterwards; ``status`` stays ``failed`` forever).
        * ``remain_blocked`` → ``continuity_state`` stays/returns
          ``'blocked'`` explicitly; the world does not advance.
        * Once released, no further resolution with another key is accepted
          (resolutions are immutable — a committed decision is never
          silently rewritten).

        NOTE: ``admin_user_id`` must be an existing ``users.id`` (FK) with
        ``is_admin = True``.  The server verifies this before accepting
        the resolution.
        """
        if resolution_type not in (
            "confirmed_no_effects",
            "continue_with_gap",
            "remain_blocked",
        ):
            raise ValueError(f"invalid resolution_type {resolution_type!r}")
        if not idempotency_key:
            raise ValueError("idempotency_key is required")

        session = self.session_factory()
        try:
            _begin_immediate(session)
            op_repo = OperationRepository(session)
            res_repo = ResolutionRepository(session)

            op = op_repo.get_operation_by_id(world_id, operation_id)
            if op is None:
                raise OperationNotFound(
                    f"operation {operation_id} not found for world {world_id}"
                )
            if op.status != "failed":
                raise InvalidTransition(
                    f"resolution requires status 'failed', operation is "
                    f"{op.status!r}"
                )

            # Verify admin authorization: admin_user_id must exist and have is_admin=True
            from worldsim.server.db import UserRow
            admin_user = session.get(UserRow, admin_user_id)
            if admin_user is None:
                session.rollback()
                raise InvalidTransition(
                    f"admin user {admin_user_id} does not exist"
                )
            if not admin_user.is_admin:
                session.rollback()
                raise InvalidTransition(
                    f"user {admin_user_id} is not authorized to perform administrative resolutions"
                )

            existing_same = res_repo.get_resolution(
                world_id, operation_id, idempotency_key
            )
            if existing_same is not None:
                session.rollback()
                # Idempotent replay of the same decision: return the
                # original record unmodified.
                return _detach(session, existing_same)

            previous = res_repo.get_resolutions_for_operation(
                world_id, operation_id
            )
            if op.continuity_state == "released" and previous:
                # A liberating resolution already committed: the world moved
                # on.  It can never be revoked or re-decided under another
                # key (§16.11: a resolution cannot be modified after
                # confirmation).
                session.rollback()
                raise ResolutionConflict(
                    "operation already released by a committed resolution"
                )

            evidence = evidence_refs
            if previous:
                # A correction must reference the previous record; the
                # schema carries no dedicated column, so the reference goes
                # in the audit references (JSON array).
                refs: list[Any]
                try:
                    parsed = json.loads(evidence or "[]")
                    refs = parsed if isinstance(parsed, list) else []
                except (ValueError, TypeError):
                    refs = []
                refs.append(
                    {"previous_resolution_id": previous[-1].id}
                )
                evidence = json.dumps(refs, ensure_ascii=False)

            resolution = res_repo.create_resolution(
                world_id=world_id,
                operation_id=operation_id,
                operation_sequence=op.operation_sequence,
                admin_user_id=admin_user_id,
                resolution_type=resolution_type,
                reason=reason,
                evidence_refs=evidence,
                idempotency_key=idempotency_key,
            )

            # Continuity release/refusal in the SAME transaction (§16.11.4).
            if resolution_type in ("confirmed_no_effects", "continue_with_gap"):
                op.continuity_state = "released"
            else:  # remain_blocked
                op.continuity_state = "blocked"

            session.commit()
            return _detach(session, resolution)
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Representation for idempotent responses (§16.11 response table)
    # ------------------------------------------------------------------

    def get_operation_representation(
        self, operation: WorldOperation, user_is_owner: bool = True
    ) -> dict:
        """Contractual representation of an operation by state.

        The idempotent-retry body of §16.11: identity + state for every
        state (HTTP 200), with the confirmed result (checkpoint + delta
        events) when ``status == 'confirmed'``.
        """
        base: dict[str, Any] = {
            "operation_id": operation.operation_id,
            "operation_sequence": operation.operation_sequence,
            "status": operation.status,
            "continuity_state": operation.continuity_state,
            "created_at": operation.created_at,
        }
        if operation.status == "confirmed":
            base["confirmed_at"] = operation.confirmed_at
            base["checkpoint_id"] = operation.checkpoint_id
            base["events"] = self._operation_events(operation)
        elif operation.status == "blocked":
            if operation.recovery_state:
                base["recovery_state"] = operation.recovery_state
        elif operation.status in ("failed", "canceled"):
            base["recovery_state"] = operation.recovery_state
        return base

    def _operation_events(self, operation: WorldOperation) -> list[dict]:
        session = self.session_factory()
        try:
            rows = EventRepository(session).get_events_by_operation(
                operation.world_id, operation.operation_id
            )
            return [
                {
                    "position": r.position,
                    "month": r.month,
                    "event_type": r.event_type,
                    "event_text": r.event_text,
                    "msg_key": r.msg_key,
                    "kingdom_ids": json.loads(r.kingdom_ids or "[]"),
                    "event_params": json.loads(r.event_params or "{}"),
                }
                for r in rows
            ]
        finally:
            session.close()
