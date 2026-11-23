"""Canonical world clock: one shared engine per world, many spectators.

Each WorldRunner owns THE engine instance for a saved world. Spectators
subscribe to its broadcast channel, so every user watching the same world
sees the same month at the same time (required for multiplayer predictions).

Also resolves prophecy deadlines against the chronicle as time advances.

Etapa 2 / DEC18 adds the definitive ``WorldRunner.close()`` (drains the
main and auxiliary tasks for real, idempotent/concurrency-safe) and
``safe_delete_world()`` (DEC10): deletion closes the runner, verifies the
close, withdraws it from the registry and only THEN deletes persisted data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

from worldsim.data.loaders import load_governments, load_races
from worldsim.server.operation_service import (
    OperationService,
    OperationParams,
    DeterminismBlocked,
    FencingError,
    NotRecoverable,
    WorldBlocked,
)
from worldsim.server.prediction_resolver import resolve_pending
from worldsim.sim.engine import SimEngine
from worldsim.worldgen import WorldConfig
from worldsim.worldgen.pipeline import generate_world

PUSH_INTERVAL = 0.5  # seconds between broadcasts

#: Max time ``close()`` waits for EACH task group (main, auxiliary) to really
#: terminate after ``cancel()`` — never blocks forever (Etapa 2 §3.7).  A
#: close that exceeds it becomes ``CLOSE_INCOMPLETE`` (strong references to
#: the pending tasks are retained).  Tests may monkeypatch this.
CLOSE_TIMEOUT = 5.0

# F7-C: Engine version for reproducibility
ENGINE_VERSION = "1.0"


class RunnerState(str, Enum):
    """Lifecycle states of a WorldRunner (DEC18 / Etapa 2 §2).

    - ``NOT_STARTED`` — abierto y no iniciado.
    - ``RUNNING`` — ``start()`` creó la tarea principal.
    - ``CLOSING`` — ``close()`` ya comenzó (fijado de forma síncrona, antes
      del primer ``await`` de la primera llamada).
    - ``CLOSED`` — cierre correcto y definitivo (terminal).
    - ``CLOSE_INCOMPLETE`` — cierre con errores o timeout (terminal): nunca
      se presenta como ``CLOSED``; el runner conserva sus referencias y su
      evidencia (§2.8, §6).

    ``CLOSED`` y ``CLOSE_INCOMPLETE`` son terminales: un runner en esos
    estados nunca vuelve a ejecutar ticks ni se reinicia (§2).
    """

    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    CLOSE_INCOMPLETE = "CLOSE_INCOMPLETE"


@dataclass
class RunnerConfig:
    seed: int
    width: int
    height: int


class WorldRunner:
    """A live simulation shared by all connected clients.

    Lifecycle contract (Etapa 1A / DEC12, DEC18):

    - new -> ``start()`` -> running.  ``start()`` is idempotent while the
      main task is running (it returns the SAME task; it never spawns a
      second one) and raises ``RuntimeError`` once ``close()`` has begun.
    - the engine is fixed at construction (``engine=``) and cannot be
      replaced after ``start()`` has begun execution (DEC12).
    - ``close()`` (Etapa 2 / DEC18) is async, idempotent, definitive,
      concurrency-safe and non-restartable: it blocks new ticks, cancels
      the main task and the auxiliary tasks, WAITS for their real
      termination (each group bounded by ``CLOSE_TIMEOUT``) and ends in
      ``CLOSED`` (clean) or ``CLOSE_INCOMPLETE`` (any cleanup error or
      timeout — never presented as a correct close).  See :meth:`close`.
    - the explicit lifecycle lives in ``state`` (``RunnerState``): the
      single source of truth for every lifecycle decision (§2).
    - auxiliary tasks (per-socket sends) keep a strong reference in
      ``_aux_tasks`` until they finish (regla 5); failures are logged, never
      swallowed silently (regla 11).  Once the close begins, ``_spawn``
      refuses new auxiliary tasks so none can escape the drain (§4).
    - ownership (§10): ``close()`` touches ONLY this runner's own tasks.
      The PersistenceService, its executor, global DB sessions, the
      RecoveryCoordinator and the RunnerRegistry are NOT closed by it.
    """

    def __init__(self, world_id: int, seed: int, width: int, height: int,
                 enabled_races: list[str] | None = None,
                 sea_level: float = 0.2, *,
                 engine: SimEngine | None = None,
                 operation_service=None) -> None:
        self.world_id = world_id
        self.enabled_races = enabled_races
        self.sea_level = sea_level
        # Lifecycle: _started latches on first start(); _state is the single
        # explicit lifecycle (Etapa 2 / DEC18), owned by start()/close().
        self._started = False
        self._state = RunnerState.NOT_STARTED
        self._task: asyncio.Task | None = None
        # Strong references to auxiliary tasks until completion (regla 5).
        self._aux_tasks: set[asyncio.Task] = set()
        # Shared close operation (Etapa 2 §5): created ONCE by the first
        # close() caller and awaited by every concurrent caller; retained as
        # a strong reference for the runner's lifetime so a cancelled caller
        # can never abandon the cleanup (regla 5).
        self._close_op: asyncio.Task | None = None
        # Cleanup errors recorded by _do_close (§6): drives CLOSE_INCOMPLETE
        # and is re-raised (never re-executed) by later close() calls.
        self._close_errors: list[BaseException] = []
        # DEC12: injected engines are used verbatim; absent an injection the
        # runner builds its own engine exactly as before.
        if engine is None:
            engine = self._build_engine(seed, width, height)
        self.eng = engine
        self.speed = 1
        self.paused = True
        self.subscribers: dict[int, dict] = {}  # id(ws) -> {ws, selected}
        self._last_score_month = 0  # league scoring cursor
        # F7-C: Operation service for T1/T2 protocol
        self.operation_service = operation_service

    @property
    def eng(self) -> SimEngine:
        """The canonical engine instance owned by this runner (DEC12)."""
        return self._eng

    @eng.setter
    def eng(self, engine: SimEngine) -> None:
        # DEC12: the engine is set during construction, before any task
        # starts, and may never be swapped once execution has begun.
        if self._started:
            raise RuntimeError(
                f"WorldRunner({self.world_id}): cannot replace the engine "
                "after start()"
            )
        self._eng = engine

    @property
    def state(self) -> RunnerState:
        """Explicit lifecycle state (Etapa 2 / DEC18 §2)."""
        return self._state

    @property
    def _closed(self) -> bool:
        """True once ``close()`` has begun (Etapa 2 / DEC18 §2.4).

        Derived from the lifecycle: covers ``CLOSING``, ``CLOSED`` and
        ``CLOSE_INCOMPLETE``, so ``start()`` is refused from the very first
        moment of the close — not only after it ends.
        """
        return self._state in (
            RunnerState.CLOSING,
            RunnerState.CLOSED,
            RunnerState.CLOSE_INCOMPLETE,
        )

    def _build_engine(self, seed: int, width: int, height: int) -> SimEngine:
        cfg = WorldConfig(seed=seed, width=width, height=height,
                          sea_level=self.sea_level)
        world = generate_world(cfg)
        data_dir = self._data_dir()
        races = load_races(data_dir / "Races.txt")
        govs = load_governments(data_dir / "CivilizedGovernment.txt")
        return SimEngine(world, races, govs, seed=seed,
                         enabled_races=self.enabled_races)

    @staticmethod
    def _data_dir():
        from pathlib import Path

        return Path(__file__).resolve().parents[3] / "data"

    def subscribe(self, ws) -> None:
        self.subscribers[id(ws)] = {"ws": ws, "selected": None}

    def unsubscribe(self, ws) -> None:
        self.subscribers.pop(id(ws), None)

    def set_selected(self, ws, selected_id: int | None) -> None:
        sub = self.subscribers.get(id(ws))
        if sub is not None:
            sub["selected"] = selected_id

    async def start(self) -> asyncio.Task:
        """Start (or re-return) the main tick task.

        Idempotent while running: repeated calls return the same task and
        never spawn a duplicate.  Raises ``RuntimeError`` once ``close()``
        has begun (§2.4: the refusal covers ``CLOSING``, ``CLOSED`` and
        ``CLOSE_INCOMPLETE`` alike — a closing/closed runner is never
        restartable).

        Returns:
            The main asyncio task driving ``_run``.
        """
        if self._closed:
            raise RuntimeError(
                f"WorldRunner({self.world_id}): cannot start a runner whose "
                "close() has begun (closing/closed runners are never "
                "restartable)"
            )
        if self._task is not None and not self._task.done():
            return self._task  # already running: no duplicate start
        self._started = True
        self._state = RunnerState.RUNNING
        self._task = asyncio.create_task(self._run())
        return self._task

    # -- auxiliary task management (regla 5) ------------------------------

    def _spawn(self, coro) -> asyncio.Task | None:
        """Spawn an auxiliary task holding a strong reference until it ends.

        The reference is dropped only by the done-callback, so an active
        auxiliary task can never be garbage-collected mid-flight, and its
        outcome is always observed (logged on failure, regla 11).

        Once ``close()`` has begun the spawn is REFUSED: the coroutine is
        closed without ever running and ``None`` is returned, so no
        auxiliary task created after the drain could escape the close
        guarantee (§4 — no task of ours is ever abandoned by the close).
        """
        if self._closed:
            close_coro = getattr(coro, "close", None)
            if close_coro is not None:
                close_coro()  # never started: it can never become an orphan
            logger.debug(
                "refused auxiliary task for world %s: close() has begun",
                self.world_id,
            )
            return None
        task = asyncio.create_task(coro)
        self._aux_tasks.add(task)
        task.add_done_callback(self._on_aux_done)
        return task

    def _on_aux_done(self, task: asyncio.Task) -> None:
        self._aux_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning(
                "auxiliary task failed for world %s: %r",
                self.world_id, exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def wait_aux_tasks(self, timeout: float | None = None) -> bool:
        """Wait until every auxiliary task of this runner has finished.

        Returns ``True`` when no auxiliary task remains pending (either none
        were spawned or all completed within ``timeout``).

        Supports the strong-reference contract of Etapa 1A.  ``close()``
        (Etapa 2 / DEC18) performs its OWN drain inside the shared close
        operation (cancel + ``CLOSE_TIMEOUT`` + real-termination wait); this
        helper remains the non-cancelling drain used by the
        RecoveryCoordinator rollback.  Failures were already logged by the
        done-callback; they are not re-raised here (regla 11: registrado una
        vez, nunca silenciado ni sustituido).
        """
        pending = [t for t in self._aux_tasks if not t.done()]
        if not pending:
            return True
        if timeout is None:
            await asyncio.gather(*pending, return_exceptions=True)
            return True
        _done, still_pending = await asyncio.wait(pending, timeout=timeout)
        return not still_pending

    # -- definitive close (Etapa 2 / DEC18) -------------------------------

    async def close(self) -> None:
        """Definitive, idempotent, concurrency-safe close (DEC18, §2-§6).

        Contract:

        - The FIRST caller begins the close SYNCHRONOUSLY (``CLOSING`` and
          the shared cleanup task are set before its first ``await``), so
          every concurrent caller observes and awaits that SAME operation
          (one shared close, §5).
        - A correct close returns ``None``; later calls are no-ops that
          never repeat cleanup operations (idempotent, §2.3).
        - ``CLOSE_INCOMPLETE`` (any cleanup error or timeout) is raised to
          every non-cancelled caller — an incomplete close is never
          presented as success (§2.8).  A later ``close()`` after an
          incomplete close does NOT repeat unsafe operations: it re-raises
          the recorded errors while the evidence (task references,
          ``_close_errors``, ``state``) is kept (§6).
        - A cancelled caller NEVER abandons the cleanup: the shared
          operation keeps running under a strong reference and the caller
          waits for its REAL completion (``asyncio.shield`` is not
          completion — the result is re-awaited explicitly), consuming any
          number of extra cancellations (§5), and finally raises
          ``CancelledError`` — never success (§6).
        - Ownership (§10): only this runner's own main/auxiliary tasks are
          touched.  The PersistenceService, its executor, global DB
          sessions, the RecoveryCoordinator and the RunnerRegistry are NOT
          closed here; the registry is coordinated explicitly by the
          responsible caller (see ``safe_delete_world``, DEC10).

        Returns:
            ``None`` when the close is correct and complete (including
            repeat/idempotent calls).

        Raises:
            asyncio.CancelledError: this caller was cancelled.  The cleanup
                still ran to completion; ``state``/``_close_errors`` remain
                observable and a subsequent call reports the real outcome.
            BaseException: the recorded cleanup failure — the raw error when
                there is exactly one, an ``ExceptionGroup`` when there are
                several (§6).
        """
        op = self._close_op
        if op is None:
            # First caller: begin synchronously (no await above this point),
            # so concurrent callers are guaranteed to observe this same op.
            self._state = RunnerState.CLOSING
            op = asyncio.ensure_future(self._do_close())
            self._close_op = op  # strong ref kept for the runner's lifetime
            op.add_done_callback(self._on_close_done)
        cancelled = False
        while not op.done():
            try:
                await asyncio.shield(op)
            except asyncio.CancelledError:
                if op.cancelled():
                    # The shared cleanup task itself ended cancelled
                    # (defensive: _do_close records it and settles a state).
                    break
                # Caller cancellation (possibly REPEATED, §5): the cleanup
                # keeps running in its own task under the strong reference
                # above — shield is NOT completion, so we explicitly
                # re-wait for the real end of the operation.
                cancelled = True
                if op.done():
                    break
                logger.warning(
                    "close(): caller of world %s was cancelled while the "
                    "cleanup was running — continuing to wait for the "
                    "shared close operation",
                    self.world_id,
                )
                continue
            except BaseException:  # defensive: _do_close never raises
                # Recorded by _on_close_done; the outcome is collected below.
                break
        # Real completion of the shared operation (never inferred from
        # having merely awaited the shield).
        if not op.done():  # unreachable: every exit above requires op.done()
            raise RuntimeError(
                f"WorldRunner({self.world_id}): close operation still pending"
            )
        if cancelled:
            # §6: the caller's cancellation is never transformed into
            # success.  The cleanup DID finish; its outcome stays observable
            # via state/_close_errors and is re-reported by a later call.
            raise asyncio.CancelledError
        if self._state is RunnerState.CLOSED:
            return  # correct close / idempotent repeat: nothing repeated
        if self._state is not RunnerState.CLOSE_INCOMPLETE:
            # Defensive: the operation ended without a terminal state.
            raise RuntimeError(
                f"WorldRunner({self.world_id}): close operation ended in "
                f"state {self._state.value!r}"
            )
        raise self._close_failure()

    def _close_failure(self) -> BaseException:
        """Observable close failure (§6): raw error, or ExceptionGroup."""
        errors = self._close_errors
        if len(errors) == 1:
            return errors[0]
        if errors:
            return ExceptionGroup(
                f"WorldRunner({self.world_id}): close incomplete", errors
            )
        return RuntimeError(
            f"WorldRunner({self.world_id}): close incomplete without "
            "recorded errors"
        )

    def _on_close_done(self, op: asyncio.Task) -> None:
        """Consume/observe the shared operation outcome (regla 11, defensive)."""
        if op.cancelled():
            exc: BaseException | None = RuntimeError(
                "close operation task was cancelled before finishing"
            )
        else:
            exc = op.exception()
        if exc is None:
            return
        self._close_errors.append(exc)  # never swallowed silently
        self._state = RunnerState.CLOSE_INCOMPLETE
        logger.error(
            "close operation for world %s failed: %r", self.world_id, exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    async def _do_close(self) -> None:
        """The single shared cleanup operation (§3-§6).  Never raises.

        Cancels and WAITS for the REAL termination of the main task and of
        every auxiliary task (each group bounded by ``CLOSE_TIMEOUT``),
        logging AND recording every cleanup error so it stays observable to
        every caller.  Always settles a terminal state: ``CLOSED`` when
        nothing failed, ``CLOSE_INCOMPLETE`` otherwise — retaining strong
        task references for everything that did not terminate (§3.8, §4).
        A pre-existing failure of an already-finished task is logged but
        does not make this close incomplete: nothing of ours is running and
        no retry could fix a task that already ended.
        """
        errors: list[BaseException] = []

        def _note(context: str, exc: BaseException) -> None:
            errors.append(exc)
            logger.error(
                "close: %s: %r", context, exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

        try:
            # 1. Block new ticks FIRST (§3.1): even a cancellation-ignoring
            #    main task stops producing ticks from this point on.
            self.paused = True

            # -- main task (§3) -------------------------------------------
            task = self._task
            if task is None:
                pass  # never started: the close is equally definitive (§3)
            elif task.done():
                # Already finished: do NOT cancel it (§3.9).  Its outcome is
                # observed so the exception is never lost; a PRE-EXISTING
                # failure does not make this close incomplete.
                if not task.cancelled():
                    exc = task.exception()
                    if exc is not None:
                        logger.error(
                            "close: main task of world %s had failed before "
                            "close: %r", self.world_id, exc,
                            exc_info=(type(exc), exc, exc.__traceback__),
                        )
            else:
                task.cancel()
                _done, pending = await asyncio.wait(
                    [task], timeout=CLOSE_TIMEOUT,
                )
                if pending:
                    # Timeout (§3.7): strong reference RETAINED (self._task
                    # is never cleared here), close marked incomplete and
                    # never claimed as correct.
                    _note(
                        f"world {self.world_id}: main task did not terminate",
                        RuntimeError(
                            f"world {self.world_id}: main task did not "
                            f"terminate within {CLOSE_TIMEOUT}s after "
                            "cancel(); reference retained"
                        ),
                    )
                elif not task.cancelled():
                    # Terminated: expected cancellation = clean; any other
                    # exception = unexpected failure DURING close (§3.5).
                    exc = task.exception()
                    if exc is not None:
                        _note(
                            f"world {self.world_id}: main task raised during close",
                            exc,
                        )

            # -- auxiliary tasks (§4) -------------------------------------
            pending_aux = [t for t in self._aux_tasks if not t.done()]
            if pending_aux:
                for aux in pending_aux:
                    aux.cancel()
                # ONE wait for ALL of them: a failing or slow auxiliary task
                # never prevents draining the others (§4).
                done_aux, still_pending = await asyncio.wait(
                    pending_aux, timeout=CLOSE_TIMEOUT,
                )
                for aux in done_aux:
                    if not aux.cancelled():
                        exc = aux.exception()
                        if exc is not None:
                            _note(
                                f"world {self.world_id}: auxiliary task "
                                "raised during close",
                                exc,
                            )
                for aux in still_pending:
                    # Strong references remain in self._aux_tasks (§4).
                    _note(
                        f"world {self.world_id}: auxiliary task did not terminate",
                        RuntimeError(
                            f"world {self.world_id}: auxiliary task {aux} "
                            f"did not terminate within {CLOSE_TIMEOUT}s "
                            "after cancel(); reference retained"
                        ),
                    )
        except asyncio.CancelledError:
            # The cleanup TASK itself was cancelled (e.g. loop shutdown):
            # recorded as a cleanup error — never presented as success, and
            # never re-raised as if it were the caller's own cancellation.
            _note(
                "close operation task was interrupted",
                RuntimeError(
                    f"WorldRunner({self.world_id}): close operation task "
                    "was cancelled before finishing"
                ),
            )
        except BaseException as exc:  # defensive: this method never raises
            _note(f"close operation of world {self.world_id} failed", exc)
        finally:
            if errors:
                self._close_errors = errors
                self._state = RunnerState.CLOSE_INCOMPLETE
            else:
                self._state = RunnerState.CLOSED

    def _tick_durable(self) -> bool:
        """Advance ONE month through the durable F7-C protocol (§16.11/§16.16).

        Ordering that the contract fixes:

        1. **Recovery first** — while an operation stays ``pending``/
           ``executing`` the world must not start a new T1: the unresolved
           operation is recovered (acquire → reconstruct → re-execute → T2).
        2. **Baseline checkpoint** — the first T1 of a world gets a valid
           ``initial_checkpoint_id`` (§16.14 condition 1) without importing
           any pre-F7-C history (§16.7).
        3. **T1** → ``mark_executing`` → engine tick with a **delta marker**
           → **T2** with the fencing generation captured at reservation.

        Returns ``True`` when the engine advanced one month.  Returns
        ``False`` when the world must stop (admission denied, determinism
        blocked or any durable failure): in every ``False`` case the world is
        left paused and NO second tick runs over an unresolved operation.
        Any unexpected durable exception is logged with traceback and pauses
        the runner — it is never silently swallowed.
        """
        svc = self.operation_service
        params = OperationParams(
            world_id=self.world_id,
            operation_type="advance",
            months=1,
        )
        try:
            # -- 1. Recovery-first (§16.16: recovery is part of the runner) --
            unresolved = svc.get_unresolved_operation(self.world_id)
            if unresolved is not None:
                try:
                    svc.recover_operation(
                        self.world_id,
                        unresolved.operation_id,
                        params,
                        target_engine=self.eng,
                    )
                    return True
                except NotRecoverable:
                    # Resolved/canceled concurrently: fall through to T1.
                    pass
                except DeterminismBlocked as exc:
                    logger.error(
                        "world %s: operation %s cannot be recovered (%s); "
                        "world paused pending administrative resolution",
                        self.world_id, unresolved.operation_id, exc,
                    )
                    self.paused = True
                    return False
                except FencingError:
                    logger.exception(
                        "world %s: recovery of operation %s lost fencing; "
                        "world paused",
                        self.world_id, unresolved.operation_id,
                    )
                    self.paused = True
                    return False

            # -- 2. Baseline before the first T1 (§16.14.1) ------------------
            svc.ensure_baseline_checkpoint(self.world_id, self.eng)

            # -- 3. T1: durable reservation ---------------------------------
            client_key = f"internal-tick-{self.world_id}-{self.eng.month}"
            t1 = svc.reserve_operation(
                world_id=self.world_id,
                requesting_user_id=0,  # internal system principal (no FK)
                client_idempotency_key=client_key,
                params=params,
            )
            op = t1.operation
            if t1.is_retry:
                # Same key seen before.  Recovery-first already covered the
                # unresolved case; a CONFIRMED op with this key would mean
                # the in-memory engine missed a tick that another executor
                # confirmed — never tick over that inconsistency.
                if op.status in ("pending", "executing"):
                    svc.recover_operation(
                        self.world_id, op.operation_id, params,
                        target_engine=self.eng,
                    )
                    return True
                logger.error(
                    "world %s: idempotency key %s already confirmed while "
                    "the engine is at month %s; world paused",
                    self.world_id, client_key, self.eng.month,
                )
                self.paused = True
                return False

            # -- 4. pending → executing (before the tick, §16.11.4) ---------
            svc.mark_executing(self.world_id, op.operation_id)

            # -- 5. tick + event delta (marker over the cumulative log) -----
            marker = len(self.eng.chronicle.events)
            self.eng.tick()
            delta = list(self.eng.chronicle.events[marker:])

            # -- 6. T2: atomic confirmation with fencing --------------------
            # expected_generation = the generation captured at T1 (0 for an
            # ordinary run): a rival recoverer that bumped the durable
            # generation in between makes this T2 fail closed (Ejemplo C).
            svc.confirm_operation(
                self.world_id,
                op.operation_id,
                self.eng,
                delta,
                expected_generation=int(op.recovery_generation or 0),
                checkpoint_type="periodic",
            )
            return True
        except WorldBlocked:
            logger.info(
                "World %s blocked, skipping tick at month %s",
                self.world_id, self.eng.month,
            )
            self.paused = True
            return False
        except DeterminismBlocked as exc:
            logger.error(
                "world %s: operation blocked by determinism check (%s); "
                "world paused pending administrative resolution",
                self.world_id, exc,
            )
            self.paused = True
            return False
        except Exception:
            # Durable failure (T1/T2/recovery): log with traceback and PAUSE.
            # The operation stays pending/executing and will be recovered on
            # the next start — a second tick over it is never attempted.
            logger.exception(
                "durable tick failed for world %s at month %s; pausing",
                self.world_id, self.eng.month,
            )
            self.paused = True
            return False

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(PUSH_INTERVAL)
            if self.paused:
                continue
            try:
                if self.operation_service is not None:
                    # F7-C (§16.16): ONE durable operation per tick.
                    for _ in range(self.speed):
                        if self.paused:
                            break
                        if not self._tick_durable():
                            break
                else:
                    # No durable protocol installed: plain ticking (Etapa 5).
                    for _ in range(self.speed):
                        self.eng.tick()

                resolved = resolve_pending(self.world_id, self.eng)
                if resolved:
                    if len(resolved) == 1:
                        r = resolved[0]
                        self._broadcast_system(
                            f"★ Prophecy resolved: {r['user_name']} — {r['text']} "
                            f"(+{r['points']} pts)"
                        )
                    else:
                        lines = "\n".join(
                            f"★ {r['user_name']}: {r['text']} (+{r['points']})"
                            for r in resolved
                        )
                        self._broadcast_system(f"{len(resolved)} prophecies resolved:\n{lines}")
                self._broadcast_league_scores()
                self._check_season_end()
                self._broadcast_tick()
            except Exception:
                logger.exception(
                    "tick failed for world %s at month %s", self.world_id, self.eng.month,
                )

    # -- broadcast ---------------------------------------------------------

    def _broadcast_tick(self) -> None:
        from worldsim.server.app import _tick_payload

        payload = json.dumps(_tick_payload(self.eng))
        self._send_all(payload)
        # per-socket stats channel: each spectator's selected kingdom stats
        # are pushed right after the shared tick, so a selection never gets
        # clobbered by the next broadcast (the panel-stays-hidden bug)
        for sub in list(self.subscribers.values()):
            sel = sub["selected"]
            if sel is None:
                continue
            k = next((k for k in self.eng.kingdoms if k.id == sel), None)
            if k is None:
                continue
            stats_msg = json.dumps(
                {"type": "stats", "stats": self.eng.kingdom_stats(k)}
            )
            try:
                self._spawn(sub["ws"].send_text(stats_msg))
            except Exception:
                self.subscribers.pop(id(sub["ws"]), None)

    def _broadcast_system(self, text: str) -> None:
        payload = json.dumps({"type": "system", "text": text})
        self._send_all(payload)

    def _broadcast_league_scores(self) -> None:
        """Score claims since the last cursor and toast the owners (batched)."""
        from worldsim.server.league import score_new_events

        messages = score_new_events(self.world_id, self.eng, self._last_score_month)
        self._last_score_month = self.eng.month
        if len(messages) == 1:
            self._broadcast_system(messages[0])
        elif messages:
            self._broadcast_system("\n".join(messages))

    def _check_season_end(self) -> None:
        """Freeze league worlds at season_years and crown the champion."""
        from worldsim.server.league import maybe_end_season

        msg = maybe_end_season(self.world_id, self.eng)
        if msg is not None:
            self.paused = True
            self._broadcast_system(msg)

    def _send_all(self, payload: str) -> None:
        dead = []
        for key, sub in list(self.subscribers.items()):
            try:
                self._spawn(sub["ws"].send_text(payload))
            except Exception:
                dead.append(key)
        for key in dead:
            self.subscribers.pop(key, None)


class RunnerRegistry:
    """world_id -> WorldRunner singleton (per server process).

    Public API (DEC11, Etapa 1A; DEC10/Etapa 2 en la parte de borrado):

    - :meth:`register_existing` — register an already-built runner; rejects
      duplicates instead of replacing them silently, validates the
      ``world_id``/runner association and never starts the runner.
      REFUSES a world that is being deleted (§7/§9).
    - :meth:`unregister` — remove a runner without closing or touching its
      shared resources; returns the removed runner or ``None``.  Closing is
      coordinated explicitly by the responsible caller (see
      ``safe_delete_world``), never implicitly by the registry (§7).
    - :meth:`get_existing` — return the registered runner or ``None``
      (never creates).  Reads stay allowed during a deletion; restart is
      prevented by the runner's own ``start()``/``_spawn`` guards.
    - :meth:`get` — preserve the existing lazy creation for the flows that
      need it; repeated calls return the same single instance per world.
      REFUSES a world that is being deleted (§7/§9), so no concurrent
      operation can obtain a runner that is being eliminated and restart it.
    - :meth:`begin_delete` / :meth:`end_delete` / :meth:`is_deleting` —
      explicit per-world coordination of safe deletion (DEC10, §8-§9).  The
      registry is NOT an implicit owner of close(): the caller runs the
      close→unregister→persist sequence and pairs begin/end itself.

    Concurrency guarantees (documentación real, §7/§9):

    - Monoprocess + single event loop: the synchronous methods never yield,
      so each one is atomic with respect to the others (no extra locks for
      the registry itself).
    - Simultaneous deletions of the SAME world serialize on a per-world
      ``asyncio.Lock`` (fair FIFO) obtained in :meth:`begin_delete`.  There
      is NO global lock: only the affected world is serialized, and its
      lock is dropped when the last participant ends.
    - The deleting mark is set synchronously at :meth:`begin_delete` — even
      for a caller still waiting for the lock — so ``get()``/
      ``register_existing()`` refuse the world for the WHOLE critical
      section.
    - The mark/lock are released in a ``finally`` (success, close failure,
      persistence failure or cancellation) or, when the caller is cancelled
      while the persisted delete is still running in its worker thread,
      transferred to that work's completion callback so the guard outlives
      the cancellation (§9).
    """

    def __init__(self) -> None:
        self._runners: dict[int, WorldRunner] = {}
        # Explicit deletion coordination (DEC10, Etapa 2 §7-§9): while a
        # world is in ``_deleting``, no runner may be created or handed out
        # for it, and simultaneous deletions of that world serialize on its
        # per-world lock.  ``_delete_users`` counts begin/end pairs so the
        # mark survives until the LAST participant ends.
        self._deleting: set[int] = set()
        self._delete_users: dict[int, int] = {}
        self._delete_locks: dict[int, asyncio.Lock] = {}

    def register_existing(self, world_id: int, runner: WorldRunner) -> WorldRunner:
        """Register an already-built runner for ``world_id`` (DEC11).

        Raises:
            TypeError: if ``world_id`` is not an ``int`` or ``runner`` is not
                a :class:`WorldRunner`.
            ValueError: if ``runner.world_id`` differs from ``world_id``
                (inconsistent association) or a runner is already registered
                for that world (duplicates are rejected; an existing runner
                is never replaced silently).
            RuntimeError: if the world is being deleted (§9) — a runner is
                never registered for a world that is being eliminated.

        The runner is NOT started here; starting is an explicit caller
        decision.  Returns the registered runner.
        """
        if not isinstance(world_id, int) or isinstance(world_id, bool):
            raise TypeError(
                f"world_id must be int, got {type(world_id).__name__}"
            )
        if not isinstance(runner, WorldRunner):
            raise TypeError(
                f"runner must be WorldRunner, got {type(runner).__name__}"
            )
        if world_id in self._deleting:
            raise RuntimeError(
                f"world {world_id} is being deleted; refusing to register a "
                "runner for it (§9)"
            )
        if runner.world_id != world_id:
            raise ValueError(
                f"runner belongs to world {runner.world_id}, not {world_id}"
            )
        if world_id in self._runners:
            raise ValueError(
                f"world {world_id} already has a registered runner; "
                "refusing to replace it silently"
            )
        self._runners[world_id] = runner
        return runner

    def unregister(self, world_id: int) -> WorldRunner | None:
        """Remove the runner registered for ``world_id`` (DEC11).

        Removes the only reference held by the registry (no residual entry)
        and does NOT close or otherwise touch the runner's shared resources
        — closing and its coordination with this withdrawal are the
        caller's responsibility (``safe_delete_world``, DEC10/§7); the
        registry never closes implicitly.

        Returns:
            The removed runner, or ``None`` when nothing was registered for
            that identifier (idempotent, well-defined for absent ids).
        """
        return self._runners.pop(world_id, None)

    # -- explicit deletion coordination (DEC10 / Etapa 2, §7-§9) ----------

    def begin_delete(self, world_id: int) -> asyncio.Lock:
        """Mark ``world_id`` as being deleted; return its per-world lock.

        The mark is set SYNCHRONOUSLY — even for a caller that then waits
        for the lock — so from this moment ``get()`` and
        ``register_existing()`` refuse the world (§9).  Must be paired with
        :meth:`end_delete`; the lock serializes simultaneous deletions of
        the SAME world only (fair FIFO, no global lock).

        Raises:
            TypeError: if ``world_id`` is not an ``int`` (mirrors
                :meth:`register_existing` validation).

        Returns:
            The per-world ``asyncio.Lock`` to hold for the critical section.
        """
        if not isinstance(world_id, int) or isinstance(world_id, bool):
            raise TypeError(
                f"world_id must be int, got {type(world_id).__name__}"
            )
        self._deleting.add(world_id)
        self._delete_users[world_id] = self._delete_users.get(world_id, 0) + 1
        lock = self._delete_locks.get(world_id)
        if lock is None:
            lock = asyncio.Lock()
            self._delete_locks[world_id] = lock
        return lock

    def end_delete(self, world_id: int) -> None:
        """Release one :meth:`begin_delete` participant (§9).

        When the LAST participant ends, the deleting mark and the per-world
        lock are dropped.  Called from ``finally`` (normal end, close
        failure, persistence failure or cancellation) or — when the caller
        was cancelled while the persisted delete kept running in its worker
        thread — from that work's completion callback.  Never raises, and
        is safe to call for an unbalanced/absent id.
        """
        users = self._delete_users.get(world_id, 0) - 1
        if users > 0:
            self._delete_users[world_id] = users
            return
        self._delete_users.pop(world_id, None)
        self._deleting.discard(world_id)
        self._delete_locks.pop(world_id, None)

    def is_deleting(self, world_id: int) -> bool:
        """Whether a deletion of ``world_id`` is currently in progress (§9)."""
        return world_id in self._deleting

    def get(self, world_id: int, seed: int, width: int, height: int,
            enabled_races: list[str] | None = None,
            sea_level: float = 0.2,
            operation_service=None) -> WorldRunner:
        # Refuse FIRST: a world being deleted must never get a runner
        # created for it (or its dying runner handed out to be restarted,
        # §7/§9).  Then: double-checked with a simple lock-free guard —
        # uvicorn runs a single-threaded event loop, so concurrent
        # coroutines cannot interleave inside this sync method.
        if world_id in self._deleting:
            raise RuntimeError(
                f"world {world_id} is being deleted; refusing to create or "
                "hand out its runner (§9)"
            )
        if world_id not in self._runners:
            runner = WorldRunner(world_id, seed, width, height, enabled_races, sea_level,
                                operation_service=operation_service)
            # re-check in case a concurrent task inserted between check and assignment
            self._runners.setdefault(world_id, runner)
        return self._runners[world_id]

    def get_existing(self, world_id: int) -> WorldRunner | None:
        return self._runners.get(world_id)

    def all_runners(self) -> list[WorldRunner]:
        """Devuelve una copia de los runners registrados (DEC11).

        Útil para el cierre global (DEC17): iterar sobre una copia evita
        problemas si el registro se muta durante el cierre.  El registry no
        cierra los runners; el propietario (RecoveryCoordinator / Etapa 6)
        es quien llama a ``WorldRunner.close()`` sobre cada uno.
        """
        return list(self._runners.values())


registry = RunnerRegistry()


# ---------------------------------------------------------------------------
# Safe world deletion (DEC10 / Etapa 2, §8-§9)
# ---------------------------------------------------------------------------

#: Strong references to in-flight persisted deletes (worker threads), so a
#: cancelled caller can never orphan them (regla 5 / §8, "conservar el error").
_lingering_delete_work: set[asyncio.Task] = set()


class WorldNotFoundError(LookupError):
    """The world row no longer exists (raised by :func:`safe_delete_world`).

    The HTTP layer translates it to the EXISTING 404 contract; Etapa 2
    introduces no new HTTP codes (§10 — no 409/503).
    """

    def __init__(self, world_id: int) -> None:
        super().__init__(f"world {world_id} not found")
        self.world_id = world_id


def _release_lingering_delete(work: asyncio.Task) -> None:
    """Drop the hold and consume the outcome of a persisted delete (§8)."""
    _lingering_delete_work.discard(work)
    if work.cancelled():
        logger.error("persisted world delete task was cancelled")
        return
    exc = work.exception()
    if exc is not None:
        logger.error(
            "persisted world delete failed: %r", exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def _delete_world_row(session_factory, world_id: int) -> None:
    """Blocking LOGICAL row delete; runs in a WORKER THREAD (never on the loop).

    The row is NEVER physically removed: ``deleted_at`` is the single
    persisted source of truth (NULL: active; timestamp: logically deleted).
    Idempotent (DEC-PRE-3): an already-marked row keeps its exact original
    value — the assignment happens only when ``deleted_at IS NULL``.
    """
    from datetime import datetime, timezone

    from worldsim.server.db import WorldRow

    with session_factory() as session:
        row = session.get(WorldRow, world_id)
        if row is None:
            raise WorldNotFoundError(world_id)
        if row.deleted_at is None:
            row.deleted_at = datetime.now(timezone.utc)
        session.commit()


async def safe_delete_world(
    world_id: int, *,
    session_factory,
    registry: RunnerRegistry,
) -> None:
    """Delete a world safely: close its runner BEFORE its data (DEC10, §8).

    Mandatory sequence:

    1. Localize the runner (if any): :meth:`RunnerRegistry.begin_delete`
       marks the world FIRST, so incompatible operations (``get()`` /
       ``register_existing()``) are refused for the whole critical section
       (§7, §9 — no new runner for a world being eliminated).
    2. Serialize with simultaneous deletions of the SAME world through its
       per-world lock: no global lock, nothing shared held across I/O,
       other worlds never blocked.  The lock/mark are released in
       ``finally`` (or transferred to the persisted work — step 5), also on
       exceptions and cancellation.
    3. ``await runner.close()`` and VERIFY the result: only ``CLOSED``
       proceeds.  A failed/incomplete close RAISES, keeps the runner
       registered (evidence, never a silent unregister) and leaves the
       persisted data untouched — the world stays recoverable (§8).
    4. Unregister the verified-closed runner.
    5. Only then delete the persisted row, in a worker thread (the event
       loop is never blocked).  The work is shielded AND reference-held:
       if the CALLER is cancelled, the delete still finishes in its thread,
       the guard stays marked until it completes (released by the work's
       callback) and the caller sees ``CancelledError`` — never success.
    6. Nothing recreates or restarts a closed runner: after a close (even
       one whose persistence later failed) the flow never calls ``start()``
       or creates a replacement runner (§8).

    With no runner, the flow continues with the existing validation rules
    (row lookup/404 semantics are preserved for the caller).

    Raises:
        WorldNotFoundError: the row is already gone (e.g. a simultaneous
            delete won the race before this caller reached the lock) — the
            HTTP layer maps it to the existing 404.
        BaseException: whatever ``close()`` recorded (incomplete close) or
            the persisted delete raised — never swallowed and never
            translated into a success (§8; no new HTTP codes, §10).
        asyncio.CancelledError: this caller was cancelled; the observable
            state remains coherent (see step 5).
    """
    lock = registry.begin_delete(world_id)
    guard_transferred = False
    try:
        async with lock:
            runner = registry.get_existing(world_id)
            if runner is not None:
                await runner.close()  # raises on CLOSE_INCOMPLETE (§8.3)
                if runner.state is not RunnerState.CLOSED:
                    # Defensive verification (§8.4): never withdraw or
                    # delete on an unverified close.
                    raise RuntimeError(
                        f"world {world_id}: close verification failed "
                        f"(state={runner.state.value}); persisted data "
                        "untouched"
                    )
                # Withdraw only after a verified close (§8.5).
                registry.unregister(world_id)
            # §8.6: ONLY NOW delete the persisted data (worker thread).
            work = asyncio.ensure_future(
                asyncio.to_thread(_delete_world_row, session_factory, world_id)
            )
            _lingering_delete_work.add(work)
            work.add_done_callback(_release_lingering_delete)
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                # The row delete keeps running in its thread; the world is
                # still being eliminated, so the deletion guard outlives
                # this cancelled caller: transfer the release duty to the
                # work's completion callback (§9).
                work.add_done_callback(
                    lambda _w, _id=world_id: registry.end_delete(_id)
                )
                guard_transferred = True
                raise
    finally:
        if not guard_transferred:
            # No await between the lock release (async with exit) and this
            # point: the mark and the lock drop together, synchronously.
            registry.end_delete(world_id)
