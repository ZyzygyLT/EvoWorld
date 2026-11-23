"""Persistence service for Fase 2.2.1.

Provides atomic, off-event-loop checkpoint persistence for worlds.

Serializes ``SimEngine`` via ``checkpoint_to_dict``, computes SHA-256 over the
exact stored bytes, and persists via ``CheckpointRepository`` in a single
SQLite transaction executed on a worker thread to avoid blocking the
``asyncio`` event loop.

Documented limitations (postponed in Fase 2.2.1-R, per the audit):

- H-3: serialization still runs on the *calling* thread while holding the
  per-world engine lock, so a heavy ``checkpoint_to_dict`` blocks its caller
  for the duration of the serialization.  Moving it into the worker requires
  a consistent snapshot mechanism on the engine and was not authorized here.
- H-8: ``max_workers`` is fixed at construction time (default 4); there is
  no dynamic sizing based on load.

Lock dictionary growth (H-7): ``_engine_locks`` and ``_persistence_locks``
grow monotonically with the number of distinct world ids ever seen by the
service.  No cleanup is performed *on purpose*.  Removing a lock requires
proving that no thread holds it and that no thread is about to acquire it;
the lock helpers hand the lock object out to callers, so the service cannot
observe "about to acquire".  Deleting a lock while a waiter could still
fetch it would allow two distinct lock objects for the same world and break
mutual exclusion -- a far worse defect than bounded memory growth.  In the
MVP a single process handles a small, bounded number of worlds, so the
growth is acceptable for now.  Cleanup will be reconsidered only with a
provable design (e.g. reference-counted lock registry) in a later phase.
"""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from typing import Any

from sqlalchemy.orm import sessionmaker

from worldsim.export.checkpoint import checkpoint_to_dict
from worldsim.server import db
from worldsim.server.repository import CheckpointRepository


class PersistenceWriteError(Exception):
    """One or more checkpoint writes failed.

    Raised by :meth:`PersistenceService.wait_pending_writes` and by
    :meth:`PersistenceService.close` *after* every pending write has been
    awaited.  All failures are aggregated in :attr:`failures` so the caller
    can identify every affected world; a single failure never prevents the
    remaining writes from being awaited.

    Attributes:
        failures: mapping of ``world_id`` to the list of original exceptions
            raised by the worker thread(s) for that world.  Every recorded
            failure appears exactly once.
    """

    def __init__(self, failures: dict[int, list[BaseException]]) -> None:
        self.failures: dict[int, list[BaseException]] = {
            world_id: list(excs) for world_id, excs in failures.items()
        }
        worlds = ", ".join(str(w) for w in self.failures)
        total = sum(len(v) for v in self.failures.values())
        super().__init__(
            f"{total} checkpoint write(s) failed for world(s): {worlds}"
        )


class PersistenceService:
    """Persist SimEngine checkpoints to SQLite atomically and off the event loop."""

    def __init__(
        self,
        session_factory: sessionmaker | None = None,
        interval_months: int = 12,
        max_workers: int = 4,
    ) -> None:
        self.session_factory = session_factory or db.SessionLocal
        self.interval_months = interval_months
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="persistence"
        )

        # Engine lock: protects SimEngine during serialization (threading.Lock
        # because serialization is called from sync code paths).
        # NOTE (H-7): grows per world; no cleanup, see module docstring.
        self._engine_locks: dict[int, threading.Lock] = {}
        self._engine_locks_lock = threading.Lock()

        # Persistence lock: serializes SQLite writes per world (threading.Lock).
        # NOTE (H-7): grows per world; no cleanup, see module docstring.
        self._persistence_locks: dict[int, threading.Lock] = {}
        self._persistence_locks_lock = threading.Lock()

        # Pending writes: world_id -> list of in-flight Futures.  A list (not
        # a single Future) keeps the pending guard accurate when several
        # writes for the same world run concurrently.
        # The condition object shares this lock: callbacks notify it once a
        # write has fully finished (recorded, then unregistered).
        self._pending_writes: dict[int, list[Future]] = {}
        self._pending_writes_lock = threading.Lock()
        self._pending_cond = threading.Condition(self._pending_writes_lock)

        # Failures recorded by the completion callback and not yet reported
        # by wait_pending_writes()/close().  Drained when reported, so no
        # exception is ever lost and none is raised twice.  Guarded by
        # _pending_writes_lock.
        self._failed_writes: list[tuple[int, Future, BaseException]] = []

        # Set once close() starts; from then on no new operation is accepted.
        # Guarded by _pending_writes_lock (via _pending_cond).
        self._closed = False

        # Last checkpoint month per world whose checkpoint was successfully
        # persisted (periodic writes only).  Updated ONLY by the completion
        # callback after a confirmed successful write, never by
        # should_checkpoint() and never before the commit finishes.
        self._last_checkpoint_month: dict[int, int] = {}
        self._last_checkpoint_month_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lock helpers
    # ------------------------------------------------------------------

    def _engine_lock_for(self, world_id: int) -> threading.Lock:
        with self._engine_locks_lock:
            if world_id not in self._engine_locks:
                self._engine_locks[world_id] = threading.Lock()
            return self._engine_locks[world_id]

    def _persistence_lock_for(self, world_id: int) -> threading.Lock:
        with self._persistence_locks_lock:
            if world_id not in self._persistence_locks:
                self._persistence_locks[world_id] = threading.Lock()
            return self._persistence_locks[world_id]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _serialize_engine(
        self, engine: Any, world_id: int, checkpoint_version: int
    ) -> tuple[bytes, str, int]:
        """Serialize ``engine`` to immutable bytes on the calling thread.

        Runs under the per-world engine lock so serialization never overlaps
        for the same world.  Returns ``(payload, checksum, month)``.  The
        bytes are immutable once produced, so they can be handed safely to a
        worker thread without further synchronization.
        """
        eng_lock = self._engine_lock_for(world_id)
        with eng_lock:
            state = checkpoint_to_dict(engine, checkpoint_version=checkpoint_version)
            payload = json.dumps(state, ensure_ascii=False).encode("utf-8")
            checksum = hashlib.sha256(payload).hexdigest()
            month = int(engine.month)
        return payload, checksum, month

    def save_checkpoint_now(
        self,
        world_id: int,
        engine: Any,
        *,
        checkpoint_type: str = "periodic",
        checkpoint_version: int = 2,
    ) -> Future:
        """Persist a checkpoint atomically.

        Serializes ``engine`` on the calling thread (protected by the engine
        lock), then transfers only the immutable bytes to a worker thread
        which performs the SQLite write.

        Returns a :class:`concurrent.futures.Future` that resolves to the
        new checkpoint row id.

        Raises:
            RuntimeError: if ``close()`` has already been started.
        """
        # Fast rejection after close (authoritative check happens again
        # under the pending-write lock at submission time).
        if self._closed:
            raise RuntimeError(
                "PersistenceService is closed; no new writes are accepted"
            )

        payload, checksum, month = self._serialize_engine(engine, world_id, checkpoint_version)

        # --- Steps 7-12: write on worker thread ---
        # Submit and register under the pending lock so that a concurrent
        # wait_pending_writes() can never observe "no pending writes" while
        # this one is being set up, and so close() cannot slip in between.
        with self._pending_cond:
            if self._closed:
                raise RuntimeError(
                    "PersistenceService is closed; no new writes are accepted"
                )
            future = self.executor.submit(
                self._write_checkpoint,
                world_id,
                None,  # sequence: compute inside worker under persistence lock
                month,
                checkpoint_type,
                payload,
                checksum,
                checkpoint_version,
            )
            self._pending_writes.setdefault(world_id, []).append(future)

        # Clean up (and, on success, record the persisted month) when done.
        # Registered after the future is already in _pending_writes, so the
        # callback always finds its own entry (it fires immediately if the
        # future already completed).
        future.add_done_callback(
            lambda f: self._on_write_finished(
                f, world_id, month, checkpoint_type
            )
        )

        return future

    def try_save_periodic_checkpoint(
        self,
        world_id: int,
        engine: Any,
        *,
        checkpoint_version: int = 2,
    ) -> Future | None:
        """Atomically reserve and submit a periodic checkpoint for ``world_id``.

        The decision (interval elapsed since last successful checkpoint) and
        the reservation (no other periodic write already in flight for this
        world) are made inside a single critical section, so two concurrent
        callers can never register two periodic writes for the same world.

        Returns the submitted :class:`~concurrent.futures.Future` when a
        periodic write was reserved and dispatched, or ``None`` when:

        * a periodic write for this world is already pending, or
        * the checkpoint interval has not elapsed since the last successful
          periodic checkpoint for this world.

        Manual checkpoints are unaffected: use :meth:`save_checkpoint_now`
        directly for those.  Serialization runs *before* the reservation lock
        so it never blocks other worlds; the expensive work is therefore not
        held under a global lock.

        Raises:
            RuntimeError: if :meth:`close` has already been started.
        """
        # Serialize outside the pending lock: never holds a lock during the
        # (potentially slow) engine serialization.
        payload, checksum, month = self._serialize_engine(
            engine, world_id, checkpoint_version
        )

        with self._pending_cond:
            if self._closed:
                raise RuntimeError(
                    "PersistenceService is closed; no new writes are accepted"
                )
            # Another periodic write already in flight for this world.
            if self._pending_writes.get(world_id):
                return None
            # Interval not elapsed since the last *successful* checkpoint.
            last = self._last_checkpoint_month.get(world_id, -1)
            if month - last < self.interval_months:
                return None

            future = self.executor.submit(
                self._write_checkpoint,
                world_id,
                None,  # sequence: compute inside worker under persistence lock
                month,
                "periodic",
                payload,
                checksum,
                checkpoint_version,
            )
            self._pending_writes.setdefault(world_id, []).append(future)
        # Registrar el callback FUERA de la sección crítica. Si el Future ya
        # está completado, add_done_callback ejecuta el callback
        # sincronicamente en el hilo llamante; hacerlo dentro de
        # _pending_cond (un threading.Lock no reentrante) produciría un
        # auto-deadlock permanente (defecto D-1 de la auditoría 2.2.1-S).
        # El Future ya quedó registrado antes de poder ejecutarse el
        # callback, así que wait_pending_writes() nunca observa un estado
        # inconsistente.
        future.add_done_callback(
            lambda f: self._on_write_finished(
                f, world_id, month, "periodic"
            )
        )
        return future

    def _write_checkpoint(
        self,
        world_id: int,
        sequence: int | None,
        month: int,
        checkpoint_type: str,
        payload: bytes,
        checksum: str,
        checkpoint_version: int,
    ) -> int:
        """Write a checkpoint to SQLite inside a worker thread.

        Creates its own SQLAlchemy session, computes the next sequence under
        the persistence lock, and persists atomically.  On any error the
        transaction is rolled back, the session is closed, and the original
        exception is re-raised so the Future ends in an error state.
        """
        plock = self._persistence_lock_for(world_id)
        with plock:
            session = self.session_factory()
            try:
                repo = CheckpointRepository(session)
                # Compute next sequence inside the lock.
                if sequence is None:
                    sequence = repo.next_sequence(world_id)
                cp_id = repo.save_checkpoint(
                    world_id=world_id,
                    sequence=sequence,
                    month=month,
                    checkpoint_type=checkpoint_type,
                    payload=payload,
                    checksum=checksum,
                    checkpoint_version=checkpoint_version,
                )
                session.commit()
                return cp_id
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    # ------------------------------------------------------------------
    # Periodic checkpoint interval
    # ------------------------------------------------------------------

    def should_checkpoint(self, world_id: int, current_month: int) -> bool:
        """Return True if a periodic checkpoint is due for ``world_id``.

        Read-only with respect to the interval state: it never marks
        anything.  Returns False while a write is pending for this world
        (preventing two concurrent periodic checkpoints per world) or if the
        interval has not elapsed since the last *successful* checkpoint.

        The last-successful-checkpoint month is updated only by the write
        completion callback (see ``mark_checkpoint_done``), so a failed write
        leaves the state untouched and the next call can retry immediately
        without waiting a full interval.
        """
        with self._pending_cond:
            if self._pending_writes.get(world_id):
                return False

        with self._last_checkpoint_month_lock:
            last = self._last_checkpoint_month.get(world_id, -1)
            return current_month - last >= self.interval_months

    def mark_checkpoint_done(self, world_id: int, month: int) -> None:
        """Record that a checkpoint for ``world_id`` completed successfully.

        Called automatically by the service after a *periodic* write
        resolves without exception; exposed for callers that drive the
        interval manually.  Never call it before the write has been
        confirmed: marking early would delay the next retry by a full
        interval if the write actually failed.
        """
        with self._last_checkpoint_month_lock:
            current = self._last_checkpoint_month.get(world_id, -1)
            if month > current:
                self._last_checkpoint_month[world_id] = month

    # ------------------------------------------------------------------
    # Pending writes tracking
    # ------------------------------------------------------------------

    def _on_write_finished(
        self,
        future: Future,
        world_id: int,
        month: int,
        checkpoint_type: str,
    ) -> None:
        """Completion callback running in the worker thread.

        Order matters:

        1. Record the failure (if any) *before* releasing the pending slot,
           so ``wait_pending_writes()`` can never see an empty pending set
           while a failure is still unrecorded (no lost exceptions).
        2. On success only, record the persisted month (H-1): the month is
           marked after the commit, never before.
        3. Release the pending slot and wake up any waiter.
        """
        cancelled = future.cancelled()
        try:
            exc = None if cancelled else future.exception()
        except BaseException as e:  # pragma: no cover - defensive
            exc = e

        if cancelled or exc is not None:
            failure = (
                exc
                if exc is not None
                else CancelledError("checkpoint future cancelled before completion")
            )
            with self._pending_cond:
                self._failed_writes.append((world_id, future, failure))
                self._unregister_locked(world_id, future)
                self._pending_cond.notify_all()
            return

        # Write confirmed successful: only now is the periodic month updated.
        if checkpoint_type == "periodic":
            self.mark_checkpoint_done(world_id, month)

        with self._pending_cond:
            self._unregister_locked(world_id, future)
            self._pending_cond.notify_all()

    def _unregister_locked(self, world_id: int, future: Future) -> None:
        """Remove ``future`` from the pending set.  Caller holds the lock."""
        pending = self._pending_writes.get(world_id)
        if pending:
            try:
                pending.remove(future)
            except ValueError:  # pragma: no cover - defensive
                pass
            if not pending:
                del self._pending_writes[world_id]

    def has_pending_write(self, world_id: int) -> bool:
        with self._pending_cond:
            return bool(self._pending_writes.get(world_id))

    def wait_pending_writes(self) -> None:
        """Wait for *all* pending writes, then surface every recorded failure.

        Blocks until no write is pending any more, i.e. every in-flight
        write has completed and been processed by its callback.  A failing
        write never prevents the remaining writes from being awaited.

        Error policy (documented per H-2): errors are **aggregated**, not
        re-raised one by one.  If at least one write failed since the last
        call, a :class:`PersistenceWriteError` is raised whose ``failures``
        mapping identifies every affected ``world_id`` (each value is the
        list of original worker exceptions for that world).  If nothing
        failed, the call returns normally.

        Failures are drained when reported: a subsequent call returns
        normally unless new writes failed in the meantime.

        The call blocks indefinitely if a worker never finishes.

        Raises:
            PersistenceWriteError: one or more writes failed.
        """
        with self._pending_cond:
            while any(self._pending_writes.values()):
                self._pending_cond.wait()
            failures: dict[int, list[BaseException]] = {}
            if self._failed_writes:
                for world_id, _future, exc in self._failed_writes:
                    failures.setdefault(world_id, []).append(exc)
                self._failed_writes.clear()

        if failures:
            raise PersistenceWriteError(failures)

    def close(self) -> None:
        """Deterministically shut the service down.

        Sequence:

        1. Mark the service closed: ``save_checkpoint_now()`` starts
           raising ``RuntimeError`` (no new operations are accepted).
        2. Wait for every pending write (``wait_pending_writes()``).
        3. Shut down the ``ThreadPoolExecutor`` **even if** a write failed.
        4. Surface persistence errors to the caller.

        Exception contract: if any not-yet-reported write failed, a
        :class:`PersistenceWriteError` aggregating all failed worlds is
        raised after the executor has been shut down.  If both the write
        wait and the executor shutdown raise, an ``ExceptionGroup``
        containing both errors is raised (``ExceptionGroup`` is native to
        Python >= 3.11; this project requires >= 3.12).  Resources are
        released before any exception propagates.

        Idempotent: subsequent calls wait for nothing, shut down an already
        shut-down executor, and return normally (failures are reported
        exactly once).
        """
        with self._pending_cond:
            self._closed = True

        errors: list[BaseException] = []
        try:
            self.wait_pending_writes()
        except BaseException as exc:
            errors.append(exc)

        try:
            self.executor.shutdown(wait=True)
        except BaseException as exc:
            errors.append(exc)

        if len(errors) == 1:
            raise errors[0]
        if len(errors) > 1:
            raise ExceptionGroup(
                "errors during PersistenceService.close()", errors
            )
