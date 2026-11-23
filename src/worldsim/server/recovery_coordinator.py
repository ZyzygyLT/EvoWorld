"""RecoveryCoordinator (DEC01): orquesta la recuperación y el ciclo de startup.

El coordinador es el DUEÑO del proceso de recuperación y del ciclo de vida
del startup, no de la reconstrucción de motores: ``RecoveryManager`` sigue
siendo responsable de reconstruir el motor y de determinar el resultado de
la recuperación, y ``PersistenceService`` de persistir checkpoints.  Aquí se
concentra la orquestación:

Etapa A — recuperar y registrar (secuencial, NADA se inicia):

    por cada mundo persistido -> ``RecoveryManager.recover(world_id)`` ->
    si ``ok``, construir ``WorldRunner`` con ``engine=`` EXACTO (DEC12) ->
    ``RunnerRegistry.register_existing()`` (DEC11) -> sin ``start()``.
    Un resultado ``not_recoverable``/``no_checkpoints`` se registra como
    resultado operativo y NO crea runner ni motor sustituto (DEC04).
    Fallos individuales se aislan y continúan; una excepción (error de
    infraestructura) aborta el startup y dispara el rollback.

Etapa B — activar (solo cuando la A terminó por completo):

    iniciar la tarea de TODOS los runners recuperados — ``running`` y
    ``paused`` (Etapa 1B-R1 / DEC06): un runner pausado queda registrado,
    con su tarea principal viva y ``runner.paused=True``; ``_run`` comprueba
    la pausa en cada iteración y hace ``continue`` SIN tick, así que la
    tarea existe pero la simulación no avanza.  Verificar que las tareas
    creadas siguen vivas, que todos los runners esperados siguen
    registrados y que ningún runner pausado quedaría sin pausa.  Solo
    entonces → ``COMPLETED``.

Rollback (§7) ante cualquier fallo — o CANCELACIÓN — de la A o la B:
cancelar y ESPERAR las tareas principales, drenar las tareas auxiliares,
desregistrar todos los runners del intento, cerrar SOLO recursos de
propiedad exclusiva (con ``asyncio.to_thread`` para no bloquear el loop y
con la referencia al trabajo retenida hasta que termine), acumular los
errores de cleanup y terminar en ``FAILED`` conservando y re-propagando la
excepción original.  El cleanup corre en su propia tarea protegida con
``asyncio.shield`` + espera explícita (ver :meth:`_rollback_guarded`), de
modo que una cancelación adicional no lo interrumpe ni lo abandona.

DEC04: los resultados ``not_recoverable`` viven SOLO en memoria
(:attr:`not_recoverable`), por instancia y sin persistirlos:
``WorldRow.status`` no adquiere valores nuevos, no se crea runner sustituto,
y una instancia nueva vuelve a evaluar la recuperación en su startup.  La
consulta HTTP 409/503 sobre estos resultados corresponde a Etapa 5.

Ownership (§10): el coordinador solo cierra ``PersistenceService`` cuando
``owns_persistence=True`` (propiedad exclusiva verificable).  El shutdown
global (drenaje + cierre verificados → ``CLOSED``) pertenece a la Etapa 6 /
DEC17: aquí se define la máquina de estados y los métodos de transición que
aquella etapa usará, pero NO se cierra ningún recurso en un shutdown
normal de esta etapa.

Concurrencia: sin locks.  El startup corre sobre el event loop único de
uvicorn y el guard de estado al inicio de :meth:`startup` rechaza cualquier
segunda ejecución sobre la misma instancia (mismo criterio monoproceso
documentado en ``RunnerRegistry``); no se observó ninguna condición de
carrera real que justifique un mecanismo adicional.

Cancelación (Etapa 1B-R1 / Corrección B): un ``asyncio.CancelledError``
durante el startup — sea entregado en un punto de suspensión sea detectado
como petición pendiente vía :meth:`_check_cancelled` (la Etapa A es
síncrona y ``WorldRunner.start()`` nunca cede el loop, así que un
``cancel()`` ahí no inyecta ninguna excepción)— ejecuta el rollback COMPLETO
antes de propagarse: los recursos del intento se liberan, el estado queda
``FAILED`` y la MISMA instancia de ``CancelledError`` se vuelve a lanzar
después del cleanup.  La cancelación nunca se convierte en éxito ni se
absorbe como error ordinario; los errores de cleanup se acumulan aparte y
no sustituyen a la cancelación original.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import select

from worldsim.server import db
from worldsim.server.db import WorldRow
from worldsim.server.persistence_service import PersistenceService
from worldsim.server.recovery_manager import RecoveryManager, RecoveryResult
from worldsim.server.runner import RunnerRegistry, WorldRunner

logger = logging.getLogger(__name__)

#: Timeout for cancelling/draining tasks during rollback (never blocks forever).
ROLLBACK_TIMEOUT = 5.0


class CoordinatorState(str, Enum):
    """States of the startup/shutdown lifecycle (DEC01 §4)."""

    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    CLOSED = "CLOSED"
    CLOSE_INCOMPLETE = "CLOSE_INCOMPLETE"


#: Legal transitions.  ``CLOSED`` and ``CLOSE_INCOMPLETE`` are terminal.
_TRANSITIONS: dict[CoordinatorState, frozenset[CoordinatorState]] = {
    CoordinatorState.NOT_STARTED: frozenset({CoordinatorState.IN_PROGRESS}),
    CoordinatorState.IN_PROGRESS: frozenset(
        {CoordinatorState.COMPLETED, CoordinatorState.FAILED}
    ),
    # FAILED -> NOT_STARTED is legal ONLY after a clean rollback with live,
    # verified original resources (checked in startup(), see _can_retry()).
    CoordinatorState.FAILED: frozenset({CoordinatorState.NOT_STARTED}),
    CoordinatorState.COMPLETED: frozenset({CoordinatorState.SHUTTING_DOWN}),
    CoordinatorState.SHUTTING_DOWN: frozenset(
        {CoordinatorState.CLOSED, CoordinatorState.CLOSE_INCOMPLETE}
    ),
    CoordinatorState.CLOSED: frozenset(),
    CoordinatorState.CLOSE_INCOMPLETE: frozenset(),
}


class StartupError(RuntimeError):
    """A startup invariant was violated (verification stage of Etapa B)."""


@dataclass(frozen=True)
class StartupSummary:
    """Observable outcome of a successful :meth:`RecoveryCoordinator.startup`."""

    recovered: tuple[int, ...]
    """Worlds whose recovery returned ``ok`` and got a registered runner."""

    activated: tuple[int, ...]
    """Worlds whose main task was started in Etapa B — ``running`` AND
    ``paused`` (Etapa 1B-R1: los pausados también inician su tarea)."""

    paused_registered: tuple[int, ...]
    """``paused`` worlds registered with their task started but gated
    (``runner.paused=True`` → zero ticks)."""

    not_recoverable: tuple[int, ...]
    """Worlds recorded as ``not_recoverable`` (DEC04, in-memory only)."""

    no_checkpoints: tuple[int, ...]
    """Worlds with no valid checkpoint to recover from."""


def _parse_enabled_races(raw: str | None) -> list[str] | None:
    """Parse ``WorldRow.enabled_races_json`` with the app's semantics."""
    try:
        return json.loads(raw or "[]") or None
    except (json.JSONDecodeError, TypeError):
        return None


def _bind_engine(session_factory):
    """Engine bound to ``session_factory`` (so ensure_schema targets THAT DB)."""
    session = session_factory()
    try:
        return session.get_bind()
    finally:
        session.close()


class RecoveryCoordinator:
    """Owns the recovery process and the startup lifecycle (DEC01).

    Dependencies are injected explicitly — no hidden globals:

    - ``persistence``: the :class:`PersistenceService` used by this process.
    - ``recovery_manager``: rebuilds engines and decides recovery results.
    - ``registry``: runners are registered through DEC11's API.
    - ``session_factory``: used to enumerate the persisted worlds.
    - ``owns_persistence``: whether the coordinator is the EXCLUSIVE owner of
      ``persistence`` and therefore allowed to close it during rollback.
    - ``runner_factory``: defaults to :class:`WorldRunner`; overridable so
      tests can substitute runner behaviour without touching the module.
    """

    def __init__(
        self,
        *,
        persistence: PersistenceService,
        recovery_manager: RecoveryManager,
        registry: RunnerRegistry,
        session_factory,
        owns_persistence: bool = False,
        runner_factory=WorldRunner,
    ) -> None:
        self.persistence = persistence
        self.recovery_manager = recovery_manager
        self.registry = registry
        self.session_factory = session_factory
        self.owns_persistence = owns_persistence
        self.runner_factory = runner_factory if runner_factory is not None else WorldRunner

        # -- state machine ------------------------------------------------
        self._state = CoordinatorState.NOT_STARTED
        self._rollback_completed = False
        self._rollback_errors: list[BaseException] = []
        self._persistence_close_attempted = False

        # -- per-attempt bookkeeping (reset on every startup attempt) ------
        self._attempt_runners: dict[int, WorldRunner] = {}
        self._attempt_status: dict[int, str] = {}
        self._attempt_tasks: dict[int, asyncio.Task] = {}
        self._activation_attempted: set[int] = set()
        self._stage_b_started = False

        # -- operational records, kept for diagnostics / future HTTP (Etapa 5)
        self.recovery_results: dict[int, RecoveryResult] = {}
        self.not_recoverable: dict[int, RecoveryResult] = {}

        # Strong references to work that outlived an await (regla 5).
        self._lingering: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    @property
    def state(self) -> CoordinatorState:
        return self._state

    @property
    def rollback_errors(self) -> list[BaseException]:
        """Cleanup errors accumulated by the last rollback (empty if clean)."""
        return list(self._rollback_errors)

    def _transition(self, target: CoordinatorState) -> None:
        allowed = _TRANSITIONS[self._state]
        if target not in allowed:
            raise RuntimeError(
                f"RecoveryCoordinator: invalid transition "
                f"{self._state.value} -> {target.value}"
            )
        logger.debug("coordinator transition %s -> %s", self._state.value, target.value)
        self._state = target

    def _resources_alive(self) -> bool:
        """Verify the original resources are still alive and usable (rule 5)."""
        if self._persistence_close_attempted:
            # This instance already tore the service down (or tried to):
            # resources are no longer the pristine originals.
            return False
        if getattr(self.persistence, "_closed", False):
            return False
        executor = getattr(self.persistence, "executor", None)
        if executor is not None and getattr(executor, "_shutdown", False):
            return False
        return True

    def _can_retry(self) -> bool:
        """FAILED -> NOT_STARTED only with a clean rollback + live resources."""
        return (
            self._rollback_completed
            and not self._rollback_errors
            and self._resources_alive()
        )

    def _check_cancelled(self) -> None:
        """Detect a cancellation requested while running without suspension.

        La Etapa A es enteramente síncrona y ``WorldRunner.start()`` nunca
        cede el event loop, así que un ``task.cancel()`` hecho durante esas
        secciones solo fija la bandera de cancelación pendiente del task
        (``Task.cancelling() >= 1``) sin que se inyecte nunca una
        ``CancelledError`` en la corrutina.  Esta comprobación convierte esa
        petición pendiente en el camino estándar de cancelación — rollback y
        propagación — ANTES de que ocurra nada más (p. ej. antes de activar
        runners en la Etapa B).

        Se llama al entrada, después de la Etapa A y después de la Etapa B.
        """
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError(
                "RecoveryCoordinator: startup cancelled "
                "(pending cancellation detected)"
            )

    # ------------------------------------------------------------------
    # Startup (DEC06: two stages)
    # ------------------------------------------------------------------

    async def startup(self) -> StartupSummary:
        """Run the full startup: Etapa A (recover+register), then B (activate).

        Raises:
            RuntimeError: when a second/duplicated startup is attempted, when
                the instance is in a terminal state, or when a retry from
                ``FAILED`` is not allowed (rollback unclean or resources no
                longer alive — a NEW coordinator with a NEW resource set is
                then required, rules 4-6).
            Exception: the original startup failure, re-raised AFTER the
                rollback (including its cleanup) has finished.
            asyncio.CancelledError: a cancellation during the startup runs
                the FULL rollback first and then re-raises the SAME
                ``CancelledError`` instance (Etapa 1B-R1); the state ends
                ``FAILED`` and a retry obeys the usual ``_can_retry()`` gate.
        """
        # -- entry guards (rule 7: no two runtimes on one instance) -------
        if self._state is CoordinatorState.IN_PROGRESS:
            raise RuntimeError(
                "RecoveryCoordinator: startup already in progress on this instance"
            )
        if self._state is CoordinatorState.FAILED:
            if not self._can_retry():
                raise RuntimeError(
                    "RecoveryCoordinator: retry rejected — the rollback did not "
                    "finish cleanly or the original resources are no longer "
                    "alive and verified; build a new resource set and a new "
                    "coordinator instance"
                )
            self._transition(CoordinatorState.NOT_STARTED)  # FAILED -> NOT_STARTED
        elif self._state is not CoordinatorState.NOT_STARTED:
            raise RuntimeError(
                f"RecoveryCoordinator: cannot start up from state "
                f"{self._state.value}"
            )
        self._transition(CoordinatorState.IN_PROGRESS)

        # -- fresh attempt bookkeeping ------------------------------------
        self._attempt_runners.clear()
        self._attempt_status.clear()
        self._attempt_tasks.clear()
        self._activation_attempted.clear()
        self._stage_b_started = False
        self._rollback_completed = False
        self._rollback_errors = []
        self.recovery_results.clear()
        self.not_recoverable.clear()

        stage = "A"
        try:
            # Corrección B: una cancelación pendiente ANTES de empezar no
            # debe crear nada (se detecta sin necesidad de un punto de
            # suspensión, vía Task.cancelling()).
            self._check_cancelled()
            self._stage_a()
            # La Etapa A es síncrona: un cancel() hecho dentro de ella (p.
            # ej. desde un hook de recover) solo fija la bandera pendiente;
            # se detecta AQUÍ, antes de activar nada, y se rueda todo lo
            # registrado en la A.
            self._check_cancelled()
            stage = "B"
            await self._stage_b()
            # cinturón y tiradores: cancelaciones pedidas en las secciones
            # síncronas de la B nunca deben alcanzar COMPLETED.
            self._check_cancelled()
        except (asyncio.CancelledError, Exception) as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            logger.error(
                "startup %s during stage %s: %r",
                "cancelled" if cancelled else "failed",
                stage, exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            # Rollback ALWAYS runs — también ante cancelación — protegido
            # contra cancelaciones adicionales; FAILED se establece aunque
            # el cleanup falle; la excepción ORIGINAL (CancelledError o
            # Exception) se propaga SIN sustituir tras el cleanup (§7/§3).
            self._rollback_errors = await self._rollback_guarded()
            self._rollback_completed = True
            self._transition(CoordinatorState.FAILED)
            for err in self._rollback_errors:
                logger.error(
                    "startup rollback cleanup error: %r",
                    err,
                    exc_info=(type(err), err, err.__traceback__),
                )
            raise  # original exception instance, original traceback

        self._transition(CoordinatorState.COMPLETED)
        return StartupSummary(
            recovered=tuple(
                sorted(
                    wid for wid, r in self.recovery_results.items()
                    if r.status == "ok"
                )
            ),
            activated=tuple(sorted(self._attempt_tasks)),
            paused_registered=tuple(
                sorted(
                    wid for wid, st in self._attempt_status.items()
                    if st == "paused"
                )
            ),
            not_recoverable=tuple(sorted(self.not_recoverable)),
            no_checkpoints=tuple(
                sorted(
                    wid for wid, r in self.recovery_results.items()
                    if r.status == "no_checkpoints"
                )
            ),
        )

    # -- Etapa A ---------------------------------------------------------

    def _stage_a(self) -> None:
        """Recover every persisted world and register runners (no starts).

        Eligibility is delegated to ``RecoveryManager`` (single source of
        truth for the recoverable statuses): worlds in any other status get
        an explicit ``not_recoverable`` result without inventing a recovery
        and without touching the persisted row.
        """
        for row in self._list_world_rows():
            world_id = row.id
            try:
                result = self.recovery_manager.recover(world_id)
            except Exception:
                # Infrastructure error: isolate nothing, abort + rollback.
                logger.exception("infrastructure failure recovering world %s", world_id)
                raise
            self.recovery_results[world_id] = result

            if result.status != "ok":
                if result.status == "not_recoverable":
                    self.not_recoverable[world_id] = result
                logger.warning(
                    "world %s not recovered at startup: %s (%s)",
                    world_id, result.status, result.diagnostics,
                )
                continue

            if result.engine is None or result.world_status not in ("running", "paused"):
                raise StartupError(
                    f"world {world_id}: recovery returned 'ok' without a usable "
                    f"engine/status (world_status={result.world_status!r})"
                )

            from worldsim.server.operation_service import OperationService
            operation_service = OperationService(self.session_factory, engine_version="1.0")
            runner = self.runner_factory(
                world_id,
                row.seed,
                row.width,
                row.height,
                _parse_enabled_races(row.enabled_races_json),
                row.sea_level if row.sea_level is not None else 0.2,
                engine=result.engine,  # DEC12: exact instance, never rebuilt
                operation_service=operation_service,
            )
            # In-memory pause mirrors the persisted status (source of truth).
            # This is NOT pause persistence (DEC14/Etapa 4): no row is written.
            runner.paused = result.world_status == "paused"

            # DEC11: register the ALREADY-BUILT runner; duplicates rejected,
            # no implicit start, no silent replacement.
            self.registry.register_existing(world_id, runner)
            self._attempt_runners[world_id] = runner
            self._attempt_status[world_id] = result.world_status
            logger.info(
                "recovered world %s (%s) from checkpoint seq %s (month %s)",
                world_id, result.world_status,
                result.checkpoint_sequence, result.checkpoint_month,
            )

    def _list_world_rows(self) -> list[WorldRow]:
        with self.session_factory() as session:
            # F7-E.2: startup never recovers nor registers runners for
            # logically deleted worlds (DEC-PRE-2, decision 2); the
            # exclusion is part of the SQL query, not an in-memory filter.
            rows = session.scalars(
                select(WorldRow)
                .where(WorldRow.deleted_at.is_(None))
                .order_by(WorldRow.id)
            ).all()
            # Attributes are already loaded; detached access is safe.
            return list(rows)

    # -- Etapa B ---------------------------------------------------------

    async def _stage_b(self) -> None:
        """Activate EVERY runner recovered in Etapa A, then verify (1B-R1).

        ``running`` and ``paused`` worlds both get their main task started:
        DEC06 requires paused runners to be recovered, registered AND
        started, staying paused with zero ticks.  ``WorldRunner._run``
        checks ``self.paused`` on every iteration and ``continue``s before
        ticking, so a started paused runner cannot advance the simulation.
        """
        self._stage_b_started = True

        for world_id, runner in list(self._attempt_runners.items()):
            # Record the activation ATTEMPT before start(): if start() creates
            # its main task and then raises, the rollback must still find it
            # (§7 — never rely on a list updated only after start() returns).
            self._activation_attempted.add(world_id)
            task = await runner.start()  # running AND paused (1B-R1)
            self._attempt_tasks[world_id] = task

        # -- verification (only after ALL of this -> COMPLETED) -----------
        for world_id, runner in self._attempt_runners.items():
            if self.registry.get_existing(world_id) is not runner:
                raise StartupError(
                    f"world {world_id}: runner lost from the registry during activation"
                )
            # Common invariant: main task created, alive, and the one the
            # runner itself holds (the same task the rollback would stop).
            task = self._attempt_tasks.get(world_id)
            own_task = getattr(runner, "_task", None)
            if (
                task is None
                or own_task is None
                or task.done()
                or own_task.done()
            ):
                raise StartupError(
                    f"world {world_id}: main task missing or already finished"
                )
            if self._attempt_status[world_id] == "running":
                if runner.paused:
                    raise StartupError(
                        f"world {world_id}: runner of a 'running' world is paused"
                    )
            else:
                # "Ningún runner pausado produce ticks": la tarea está viva
                # pero la bandera de pausa (puerta de _run) debe seguir
                # puesta — es lo que impide cualquier tick.
                if not runner.paused:
                    raise StartupError(
                        f"world {world_id}: paused world runner would tick"
                    )

    # ------------------------------------------------------------------
    # Rollback (§7)
    # ------------------------------------------------------------------

    async def _rollback(self) -> list[BaseException]:
        """Undo the attempt across BOTH stages.  Never raises.

        Returns the accumulated cleanup errors (the original startup
        exception is propagated by the caller, not by this method).
        """
        errors: list[BaseException] = []

        def _note(context: str, exc: BaseException) -> None:
            errors.append(exc)
            logger.error(
                "rollback: %s: %r", context, exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

        # 1. Stop every runner of this attempt (covers Etapa B activations
        #    AND a task created by a start() that raised afterwards, since we
        #    read the runner's own main task rather than a post-hoc list).
        for world_id, runner in list(self._attempt_runners.items()):
            task = getattr(runner, "_task", None)
            if task is not None and not task.done():
                task.cancel()
                _done, pending = await asyncio.wait(
                    [task], timeout=ROLLBACK_TIMEOUT
                )
                if pending:
                    # Cancellation was REQUESTED but not finished: never claim
                    # termination — record it and keep a strong reference.
                    _note(
                        f"world {world_id}: main task did not terminate",
                        RuntimeError(
                            f"world {world_id}: main task did not terminate "
                            f"within {ROLLBACK_TIMEOUT}s after cancel()"
                        ),
                    )
                    self._hold(task)
            try:
                drained = await runner.wait_aux_tasks(timeout=ROLLBACK_TIMEOUT)
            except Exception as exc:  # defensive: helper is non-raising
                _note(f"world {world_id}: auxiliary drain failed", exc)
            else:
                if not drained:
                    _note(
                        f"world {world_id}: auxiliary tasks still pending",
                        RuntimeError(
                            f"world {world_id}: auxiliary tasks still pending "
                            f"after {ROLLBACK_TIMEOUT}s"
                        ),
                    )
                    for aux in list(getattr(runner, "_aux_tasks", ())):
                        if not aux.done():
                            self._hold(aux)

        # 2. Unregister every runner incorporated during Etapa A (no residual
        #    references in the registry; shared resources are NOT closed here).
        for world_id in list(self._attempt_runners):
            try:
                self.registry.unregister(world_id)
            except Exception as exc:
                _note(f"world {world_id}: unregister failed", exc)

        # 3. Drain + close EXCLUSIVELY owned resources.  External/shared
        #    resources are left untouched (their owner closes them).
        if self.owns_persistence:
            # Flag BEFORE closing: even a failed close leaves the service in
            # a torn-down state, so a retry on this instance must be refused.
            self._persistence_close_attempted = True
            try:
                await self._run_blocking(self.persistence.wait_pending_writes)
            except Exception as exc:
                _note("persistence drain failed", exc)
            try:
                await self._run_blocking(self.persistence.close)
            except Exception as exc:
                _note("persistence close failed", exc)

        # 4. Drop per-attempt bookkeeping; keep operational records
        #    (recovery_results / not_recoverable) as diagnostics.
        self._attempt_runners.clear()
        self._attempt_status.clear()
        self._attempt_tasks.clear()
        self._activation_attempted.clear()
        self._stage_b_started = False
        return errors

    async def _rollback_guarded(self) -> list[BaseException]:
        """Run :meth:`_rollback` immune to further cancellation (1B-R1).

        El cleanup corre en su PROPIA tarea (referencia fuerte retenida en
        ``_lingering`` hasta que termine), de modo que una cancelación
        adicional del startup no puede interrumpirlo.  ``asyncio.shield``
        protege la espera — pero shield NO espera por sí solo a que termine
        la operación protegida, así que se re-espera explícitamente en un
        bucle, consumiendo cualquier ``CancelledError`` extra que llegue
        mientras el cleanup sigue vivo.  Nunca se abandona el cleanup: el
        bucle solo sale con la tarea de cleanup terminada.

        Returns:
            Los errores de cleanup acumulados (nunca lanza): la excepción
            original — ``CancelledError`` o ``Exception`` — la propaga el
            llamador, que es quien hizo ``raise``.
        """
        errors: list[BaseException] = []
        cleanup = asyncio.ensure_future(self._rollback())
        self._hold(cleanup)  # referencia fuerte hasta que termine (regla 5)
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                if cleanup.done():
                    # La propia tarea de cleanup terminó cancelada: se
                    # registra abajo como fallo de cleanup (nunca limpio).
                    break
                # Cancelación EXTRA de ESTA tarea: el cleanup sigue corriendo
                # en su propia tarea — se vuelve a esperar (no se abandona).
                logger.warning(
                    "extra cancellation received while rollback was running — "
                    "continuing to wait for the cleanup to finish"
                )
                continue
            except BaseException as exc:
                # _rollback() está documentado como no-lanzadora; cinturón y
                # tiradores: se registra para bloquear cualquier retry.
                errors.append(exc)
                break
        # Recoger el resultado exactamente una vez, con la tarea terminada.
        if cleanup.cancelled():
            errors.append(
                RuntimeError(
                    "rollback cleanup task was cancelled before finishing"
                )
            )
        else:
            exc = cleanup.exception()
            if exc is not None:
                errors.append(exc)
            else:
                errors.extend(cleanup.result())
        return errors

    def _hold(self, task: asyncio.Task) -> None:
        """Keep a strong reference until ``task`` actually finishes (regla 5)."""
        self._lingering.add(task)
        task.add_done_callback(self._lingering.discard)

    async def _run_blocking(self, func, *args):
        """Run blocking cleanup off the loop, immune to caller cancellation.

        ``asyncio.shield`` keeps the underlying work running if the awaiting
        task is cancelled, and ``_lingering`` holds the reference until the
        wrapper task finishes — the operation is never abandoned mid-flight.
        """
        work = asyncio.ensure_future(asyncio.to_thread(func, *args))
        self._hold(work)
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            raise
        return work.result()

    # ------------------------------------------------------------------
    # Shutdown entry points (transitions ONLY — real shutdown is Etapa 6)
    # ------------------------------------------------------------------

    def begin_shutdown(self) -> None:
        """``COMPLETED -> SHUTTING_DOWN``.

        The actual drain/close sequence belongs to Etapa 6 / DEC17; this
        method only opens the transition so the state machine is explicit.
        """
        self._transition(CoordinatorState.SHUTTING_DOWN)

    def finish_shutdown(self, *, complete: bool) -> None:
        """``SHUTTING_DOWN -> CLOSED`` (verified) or ``-> CLOSE_INCOMPLETE``.

        Both are terminal for this instance (rule 3).  Resources are closed
        by the Etapa 6 shutdown sequence, not by this transition.
        """
        if self._state is not CoordinatorState.SHUTTING_DOWN:
            raise RuntimeError(
                f"RecoveryCoordinator: finish_shutdown() requires SHUTTING_DOWN, "
                f"got {self._state.value}"
            )
        self._transition(
            CoordinatorState.CLOSED if complete
            else CoordinatorState.CLOSE_INCOMPLETE
        )

    # ------------------------------------------------------------------
    # Global shutdown sequence (DEC17 / Etapa 6)
    # ------------------------------------------------------------------

    async def shutdown(self) -> list[BaseException]:
        """Cierre global ordenado de los recursos del coordinator (DEC17).

        Transición: ``COMPLETED -> SHUTTING_DOWN -> CLOSED | CLOSE_INCOMPLETE``.

        Secuencia:

        1. Cierra todos los runners registrados mediante
           :meth:`WorldRunner.close` (DEC18: asíncrono, idempotente,
           definitivo, no reiniciable, espera la terminación real de sus
           tareas). Un fallo individual no abandona los demás cierres.
        2. Drena las writes pendientes de persistencia
           (``wait_pending_writes``) sin generar un checkpoint final
           automático (DEC05).
        3. Si es dueño exclusivo de la persistencia, la cierra
           (``PersistenceService.close``: drena writes + cierra el executor).

        Los errores se acumulan y nunca se silencian: el estado final es
        ``CLOSED`` solo si todo cerró limpio, ``CLOSE_INCOMPLETE`` en caso
        contrario.  Una cancelación del llamador no abandona las tareas de
        cierre en curso (cada una conserva su referencia fuerte).

        Returns:
            Los errores de cierre acumulados (vacío si todo cerró limpio).

        Raises:
            RuntimeError: si el coordinator no está en un estado desde el
                que se pueda cerrar (salvo los estados en que no hay nada
                que hacer, que retornan ``[]``).
        """
        errors: list[BaseException] = []

        # -- guard: estados en que no hay nada que cerrar ------------------
        # NOT_STARTED / IN_PROGRESS / FAILED: el startup no completó, su
        # rollback ya cerró los recursos de ese intento.  CLOSED /
        # CLOSE_INCOMPLETE: ya se cerró (idempotente, no hacer nada).
        if self._state in (
            CoordinatorState.NOT_STARTED,
            CoordinatorState.IN_PROGRESS,
            CoordinatorState.FAILED,
            CoordinatorState.CLOSED,
            CoordinatorState.CLOSE_INCOMPLETE,
        ):
            return errors

        # -- abrir la transición de cierre si aún no está abierta ----------
        if self._state is not CoordinatorState.SHUTTING_DOWN:
            # Solo COMPLETED puede pasar a SHUTTING_DOWN.
            self.begin_shutdown()  # COMPLETED -> SHUTTING_DOWN

        # -- 1. Cerrar todos los runners registrados (DEC18) ---------------
        for runner in self.registry.all_runners():
            try:
                await runner.close()
            except BaseException as exc:
                # Un fallo individual no abandona los demás cierres (§3.4).
                errors.append(exc)
                logger.error(
                    "shutdown: runner world %s close failed: %r",
                    runner.world_id, exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        # -- 2. Drenar writes pendientes de persistencia (sin checkpoint) --
        # DEC05: no se genera un checkpoint final automático al cerrar.
        try:
            await self._run_blocking(self.persistence.wait_pending_writes)
        except BaseException as exc:
            errors.append(exc)
            logger.error(
                "shutdown: persistence drain failed: %r", exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

        # -- 3. Cerrar persistencia si somos dueños exclusivos -------------
        if self.owns_persistence:
            try:
                await self._run_blocking(self.persistence.close)
            except BaseException as exc:
                errors.append(exc)
                logger.error(
                    "shutdown: persistence close failed: %r", exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        # -- 4. Transición terminal ----------------------------------------
        self.finish_shutdown(complete=(len(errors) == 0))
        return errors


async def run_startup(
    *,
    session_factory,
    registry: RunnerRegistry,
) -> RecoveryCoordinator:
    """Full startup sequence invoked by the FastAPI lifespan (DEC01/DEC02/DEC06).

    Explicit order:

    1. ``db.ensure_schema()`` — executed HERE, explicitly, BEFORE any world
       query or any recovery (DEC02; no import-time side effects remain).
    2. Create the services with clear ownership.
    3. Build the :class:`RecoveryCoordinator`.
    4. ``await coordinator.startup()`` — Etapas A + B; on failure OR
       cancellation the guarded rollback runs and the ORIGINAL exception
       (incluido ``CancelledError``) propagates, so the lifespan (and
       therefore the application) never comes up as if recovery had
       finished.

    The coordinator created here owns the PersistenceService exclusively
    (``owns_persistence=True``): it was built for this startup and nobody
    else can close it.
    """
    engine = _bind_engine(session_factory)
    db.ensure_schema(engine)  # DEC02: explicit, before everything else

    persistence = PersistenceService(session_factory=session_factory)
    recovery_manager = RecoveryManager(session_factory=session_factory)
    coordinator = RecoveryCoordinator(
        persistence=persistence,
        recovery_manager=recovery_manager,
        registry=registry,
        session_factory=session_factory,
        owns_persistence=True,
    )
    await coordinator.startup()
    return coordinator
