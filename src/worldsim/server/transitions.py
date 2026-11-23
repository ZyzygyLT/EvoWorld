"""Transiciones de pausa y reanudación por mundo (Etapa 4 — DEC08/DEC14/DEC15).

Mecanismo coordinado que mantiene coherentes los tres estados de una pausa o
reanudación:

* ``WorldRow.status`` — el estado persistido de referencia (DEC14);
* ``WorldRunner.paused`` — el control operativo de la ejecución;
* ``SimEngine.paused`` — espejo del estado operativo, nunca autoridad
  independiente.

Contrato de la transición (§4 del alcance de la Etapa 4):

* **Exclusión por mundo** (§4.1): cada mundo tiene su propio
  ``asyncio.Lock`` (FIFO).  No existe lock global: mundos distintos nunca
  se bloquean entre sí.  La adquisición es cancel-safe (una cancelación
  mientras se espera el lock no lo adquiere) y la liberación ocurre en
  ``finally``.  El lock de transición es una *hoja* en el orden de
  adquisición: nunca se adquiere el lock de eliminación del registro
  (solo se LEE ``is_deleting``), por lo que no puede haber interbloqueo
  con ``safe_delete_world``.
* **Identidad y generación** (§4.2): cada operación recibe una generación
  monotónica por mundo al ENTRAR (``begin``).  Antes de aplicar cambios en
  memoria se comprueba la vigencia (``_apply_in_memory``): una operación
  antigua nunca sobrescribe el estado producido por una más reciente.  El
  orden de finalización de tareas nunca es la única garantía.
* **Escritura persistida** (§4.3/§4.4): la escritura corre en un worker
  thread (``asyncio.to_thread``) sobre el contrato público de
  ``WorldRepository``; la operación conserva una referencia fuerte al task
  de la escritura (``_writes``) hasta su finalización real.
* **Cancelación y reconciliación** (§4.5): ``asyncio.shield`` protege la
  espera; si el llamador se cancela, la operación espera la finalización
  REAL de la escritura (consumiendo cualquier cancelación repetida),
  determina su resultado efectivo, reconcilia el estado persistido y
  operativo, libera el lock y SOLO ENTONCES propaga la ``CancelledError``
  original.  Un ``Future.cancel()`` nunca se interpreta como fin del
  trabajo del executor: el task se observa hasta ``done()``.

  La reconciliación tiene dos formas:

  1. La escritura **confirmó** y la puerta de aplicado la acepta → el
     estado operativo (runner + espejo) se actualiza para coincidir con lo
     persistido (también si el llamador estaba cancelado).
  2. La escritura **confirmó** pero la puerta de aplicado la RECHAZA
     (el mundo empezó a eliminarse, el runner comenzó ``close()`` o la
     operación quedó obsoleta) → se RESTAURA el valor previo de
     ``WorldRow.status`` en un worker thread antes de liberar el lock, de
     modo que un mundo que sobreviva nunca quede con fila y operativo en
     desacuerdo.  Si la fila ya no existe (la eliminación ganó), no hay
     nada que reconciliar.  Si la restauración misma falla, el error se
     conserva en :attr:`TransitionCoordinator.reconcile_errors` y en el
     log (nunca se oculta) y se lanza al llamador si este no estaba
     cancelado.
  3. La escritura **falló** → no se tocó nada (la sesión se descarta sin
     confirmar), no hay nada que reconciliar; el error original llega al
     llamador.  Si el llamador estaba cancelado (no puede verlo), se
     conserva en ``reconcile_errors`` + log.

* **Eliminación y cierre** (§4.6): las validaciones de vigencia y el
  arranque de la escritura ocurren en un mismo bloque SIN ``await``
  intermedio (atómico en el event loop), y la puerta de aplicado se
  re-evalúa de forma síncrona después de cada suspensión: si
  ``begin_delete`` alcanzó al mundo o el runner comenzó ``close()``, la
  transición NO modifica el estado operativo (y revierte la fila si hizo
  una escritura).  :meth:`TransitionCoordinator.shutdown` impide nuevas
  transiciones y espera a las que están en curso —una transición ya
  identificada con ``begin()`` se deja terminar— sin abandonar escrituras.
* **Sin segunda ruta de ticks** (§4.3): la pausa solo fija
  ``WorldRunner.paused``; ``_run`` sigue siendo el único camino de ticks y
  ya comprueba esa bandera antes de cada tanda (DEC06 intacto).  La
  reanudación nunca llama a ``start()`` ni crea tareas.

Secuencia de una transición (orden exacto)::

    begin(generación) → lock por mundo → validaciones de vigencia
    (atómicas) → escritura en worker thread → (si el llamador se cancela:
    espera real) → resultado efectivo → puerta de aplicado (generación
    vigente, sin eliminación, runner utilizable) → reconciliación si la
    puerta rechazó → liberar lock → devolver resultado o propagar error.

El formato de checkpoint, el contrato de snapshots (DEC13), DEC19, la
semántica de recuperación (Etapa 1A/1B), la política de runners pausados
(DEC06), el contrato de ``WorldRunner.close()`` (DEC18) y el protocolo de
eliminación (DEC10: ``begin_delete``/``end_delete``) no se tocan.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from worldsim.server.repository import WorldRepository
from worldsim.server.runner import RunnerRegistry, RunnerState, WorldNotFoundError

logger = logging.getLogger(__name__)

#: Valores persistidos de ``WorldRow.status`` (contrato existente).
PAUSED_STATUS = "paused"
RUNNING_STATUS = "running"

_VALID_STATUSES = (PAUSED_STATUS, RUNNING_STATUS)

#: Estados de runner en los que una transición ya no puede modificar nada
#: (el ``close()`` de DEC18 es definitivo y fija ``paused=True`` él mismo).
_CLOSED_STATES = (
    RunnerState.CLOSING,
    RunnerState.CLOSED,
    RunnerState.CLOSE_INCOMPLETE,
)


class TransitionError(RuntimeError):
    """Transición rechazada por vigencia (mundo eliminándose, shutdown,
    runner cerrado/inexistente o estado destino inválido).

    Es un rechazo de coordinación, nunca un fallo silencioso: el estado
    persistido y operativo quedan intactos y el lock queda liberado.
    """


@dataclass(frozen=True)
class TransitionResult:
    """Resultado observable de una transición completada.

    Attributes:
        world_id: mundo afectado.
        status: estado destino (``"paused"``/``"running"``) ya persistido.
        generation: generación (identidad) de ESTA operación.
        applied: ``True`` cuando el estado operativo (runner + espejo del
            engine) quedó actualizado por esta operación; ``False`` cuando
            la puerta de vigencia lo rechazó (p. ej. la eliminación del
            mundo comenzó durante la escritura) — en tal caso la fila fue
            restaurada a su valor previo antes de liberar el lock.
    """

    world_id: int
    status: str
    generation: int
    applied: bool


# ---------------------------------------------------------------------------
# Escritura y reconciliación en worker threads (§4.5)
# ---------------------------------------------------------------------------


def _write_status(session_factory, world_id: int, status: str) -> tuple[str | None, bool]:
    """Escribe ``WorldRow.status``; corre en un WORKER THREAD (nunca en el loop).

    Usa el contrato público del repositorio (sin atributos privados).  La
    sesión externa solo hace ``flush`` en ``update_status``, así que el
    ``commit`` es explícito aquí, dentro de la misma transacción.

    Returns:
        ``(previous_status, wrote)`` — el valor previo de la fila (para
        poder reconciliar) y si se llegó a modificar la fila (``False``
        cuando ya estaba en el destino: nada que revertir jamás).

    Raises:
        WorldNotFoundError: la fila desapareció (p. ej. una eliminación
            concurrente ganó la carrera) — se propaga sin traducción.
        Exception: cualquier fallo de base de datos original, sin traducir
            (la sesión se descarta sin confirmar: la fila queda intacta).
    """
    with session_factory() as session:
        repo = WorldRepository(session)  # sesión externa: sin commit propio
        row = repo.get_world(world_id)
        if row is None:
            raise WorldNotFoundError(world_id)
        previous = row.status
        if previous == status:
            return previous, False  # ya en el destino: escritura innecesaria
        if not repo.update_status(world_id, status):
            raise WorldNotFoundError(world_id)
        session.commit()
        return previous, True


def _revert_status(session_factory, world_id: int, previous: str) -> bool:
    """Restaura ``previous`` en la fila; corre en un WORKER THREAD.

    Es la reconciliación de la vía 2 (§4.5): se usa cuando la escritura
    confirmó pero la puerta de aplicado rechazó, para que un mundo que
    sobreviva no quede con fila y estado operativo en desacuerdo.

    Returns:
        ``True`` si la fila quedó restaurada; ``False`` si la fila ya no
        existe (la eliminación ganó: no hay nada que reconciliar).

    Raises:
        Exception: el fallo original de la restauración, sin traducir.
    """
    with session_factory() as session:
        repo = WorldRepository(session)
        if not repo.update_status(world_id, previous):
            return False  # fila ausente: mundo eliminado, nada que hacer
        session.commit()
        return True


async def _await_real_end(task: asyncio.Task) -> asyncio.CancelledError | None:
    """Espera la finalización REAL de ``task`` absorbiendo cancelaciones.

    ``asyncio.shield`` NO es finalización: si el llamador se cancela, el
    task sigue vivo en su worker thread y aquí se sigue esperando hasta
    ``done()``, consumiendo cualquier cancelación repetida (§4.5.2).

    Returns:
        La PRIMERA ``CancelledError`` entregada a este llamador (la
        original, §4.5.6), o ``None`` si el llamador no fue cancelado.
        Cualquier cancelación posterior se descarta: se propaga la
        original.
    """
    first: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if task.cancelled():
                break  # el task propio terminó cancelado (defensivo)
            if first is None:
                first = exc  # conservar la cancelación ORIGINAL
        except BaseException:
            break  # el task falló: su resultado se lee abajo, nunca en silencio
    if not task.cancelled():
        task.exception()  # consumido aquí (nunca queda "unobserved")
    return first


def _task_exception(task: asyncio.Task) -> BaseException | None:
    """Resultado efectivo de un task terminado (``None`` si confirmó)."""
    if task.cancelled():
        return asyncio.CancelledError("worker task cancelled before completion")
    return task.exception()


class TransitionCoordinator:
    """Estado compartido de las transiciones por mundo (§4.1, §4.2, §4.6).

    Un único :class:`asyncio.Lock` por mundo (creación perezosa, sin lock
    global), generaciones monotónicas por mundo, control de operaciones en
    curso para el shutdown y referencia fuerte a los tasks de escritura.

    Nota de crecimiento (misma justificación H-7 que los locks del
    registro): los locks/generaciones por mundo no se eliminan — quitar un
    lock exige probar que nadie está a punto de adquirirlo y permitiría
    dos locks distintos para el mismo mundo (pérdida de exclusión mutua).
    """

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._generation: dict[int, int] = {}
        self._applied_generation: dict[int, int] = {}
        # Operaciones registradas (begin sin end) — vigiladas por shutdown.
        self._active = 0
        self._idle = asyncio.Event()
        self._idle.set()
        # False en cuanto shutdown() comienza: rechaza nuevas transiciones.
        self._accepting = True
        # Referencias fuertes a los tasks de escritura/reconciliación en
        # vuelo (regla 5): nunca un task huérfano.
        self._writes: set[asyncio.Task] = set()
        #: Fallos que NADIE pudo ver (el llamador estaba cancelado cuando
        #: la escritura o la reconciliación fallaron): conservados aquí +
        #: log, nunca descartados en silencio.  Solo se añaden.
        self.reconcile_errors: list[BaseException] = []

    # -- identidad y generación (§4.2) ------------------------------------

    def begin(self, world_id: int) -> int:
        """Registra la entrada de una nueva transición y devuelve su generación.

        Se ejecuta SÍNCRONAMENTE antes de esperar el lock, de modo que una
        operación en cola ya quedó identificada; la generación es monotónica
        por mundo.  Lanza :class:`TransitionError` si el shutdown ya comenzó
        (las operaciones nuevas se rechazan en el mismo punto de entrada).
        """
        if not self._accepting:
            raise TransitionError(
                f"world {world_id}: transition refused; shutdown has begun"
            )
        gen = self._generation.get(world_id, 0) + 1
        self._generation[world_id] = gen
        self._active += 1
        self._idle.clear()
        return gen

    def end(self, world_id: int) -> None:
        """Cierra una entrada de :meth:`begin` (siempre desde ``finally``)."""
        self._active = max(0, self._active - 1)
        if self._active == 0:
            self._idle.set()

    def generation(self, world_id: int) -> int:
        """Última generación emitida para ``world_id`` (0 si ninguna)."""
        return self._generation.get(world_id, 0)

    def applied_generation(self, world_id: int) -> int:
        """Generación cuyo estado operativo está aplicado (0 si ninguna)."""
        return self._applied_generation.get(world_id, 0)

    def _lock_for(self, world_id: int) -> asyncio.Lock:
        """Lock exclusivo de ``world_id`` (FIFO, cancel-safe, sin global)."""
        lock = self._locks.get(world_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[world_id] = lock
        return lock

    # -- referencias y errores invisibles (regla 5 / §4.5) ----------------

    def _hold(self, task: asyncio.Task) -> None:
        """Referencia fuerte hasta la finalización real + observador.

        El task nunca puede volverse huérfano mientras la operación viva
        (regla 5); al terminar, el callback suelta la referencia — la
        operación ya leyó (o leerá) su resultado antes de liberar el lock,
        así que ningún resultado queda sin reconciliar.
        """
        self._writes.add(task)
        task.add_done_callback(self._on_write_done)

    def _on_write_done(self, task: asyncio.Task) -> None:
        """Suelta la referencia fuerte al task terminado (idempotente)."""
        self._writes.discard(task)

    def _record_unseen(self, world_id: int, what: str, exc: BaseException) -> None:
        """Conserva un error que el llamador cancelado no pudo ver (§4.5)."""
        self.reconcile_errors.append(exc)
        logger.error(
            "world %s: transition %s failed while the caller was cancelled: %r",
            world_id, what, exc,
            exc_info=(type(exc), exc, getattr(exc, "__traceback__", None)),
        )

    # -- puerta de aplicado (§4.2, §4.6) ----------------------------------

    def _apply_in_memory(
        self, world_id: int, generation: int, status: str,
        registry: RunnerRegistry,
    ) -> bool:
        """Aplica SOLO si la operación sigue vigente.  Síncrona: entre la
        comprobación y la asignación no hay ningún ``await``, por lo que es
        atómica en el event loop (idéntico a la marca de ``begin_delete``).

        Returns:
            ``True`` si el estado operativo quedó actualizado (o no había
            runner que actualizar y la transición es solo de fila);
            ``False`` si la operación quedó obsoleta o el mundo pasó a
            estar en eliminación/cierre — en ese caso el llamador debe
            reconciliar la fila antes de liberar el lock.
        """
        if generation <= self._applied_generation.get(world_id, 0):
            logger.warning(
                "world %s: stale transition (generation=%s, applied=%s) "
                "refused the operational apply",
                world_id, generation, self._applied_generation.get(world_id, 0),
            )
            return False
        if registry.is_deleting(world_id):
            logger.warning(
                "world %s: deletion began during the transition; the "
                "operational apply is skipped (the row write will be reverted)",
                world_id,
            )
            return False
        runner = registry.get_existing(world_id)
        if runner is not None:
            if runner.state in _CLOSED_STATES:
                logger.warning(
                    "world %s: runner close began during the transition "
                    "(state=%s); operational apply skipped (the row write "
                    "will be reverted)",
                    world_id, runner.state.value,
                )
                return False
            value = status == PAUSED_STATUS
            runner.paused = value
            runner.eng.paused = value  # espejo, nunca autoridad (DEC14)
        self._applied_generation[world_id] = generation
        return True

    # -- shutdown (§4.6) ---------------------------------------------------

    async def shutdown(self) -> None:
        """Impide nuevas transiciones y ESPERA a las que están en curso.

        Idempotente y cancel-safe: una transición ya registrada con
        ``begin()`` se deja terminar (el shutdown no la aborta a mitad:
        abortarla dejaría la fila escrita sin reconciliar).  Si el llamador
        se cancela, la espera continúa hasta la finalización real de las
        operaciones pendientes (los tasks de escritura conservan referencia
        fuerte mientras tanto) y SOLO DESPUÉS se propaga la
        ``CancelledError`` — ninguna escritura queda abandonada.
        """
        self._accepting = False  # síncrono: sin await antes de rechazar
        cancelled: asyncio.CancelledError | None = None
        while self._active:
            waiter = asyncio.ensure_future(self._idle.wait())
            try:
                await asyncio.shield(waiter)
            except asyncio.CancelledError as exc:
                if cancelled is None:
                    cancelled = exc  # conservar la original y volver a esperar
                continue
            finally:
                if waiter.done() and not waiter.cancelled():
                    waiter.exception()  # nunca observar en silencio
        if cancelled is not None:
            raise cancelled


# ---------------------------------------------------------------------------
# Núcleo de la transición
# ---------------------------------------------------------------------------


async def _run_transition(
    world_id: int,
    status: str,
    *,
    session_factory,
    registry: RunnerRegistry,
    coordinator: TransitionCoordinator,
) -> TransitionResult:
    """Secuencia completa: identificar → lock → sección crítica → liberar."""
    if status not in _VALID_STATUSES:
        raise TransitionError(
            f"invalid transition target {status!r}; expected one of "
            f"{list(_VALID_STATUSES)}"
        )
    # Cancelación pendiente ANTES de identificar la operación: nada cambia.
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError(
            f"transition for world {world_id} cancelled before it began"
        )
    gen = coordinator.begin(world_id)
    try:
        async with coordinator._lock_for(world_id):
            return await _critical_section(
                world_id, gen, status,
                session_factory=session_factory, registry=registry,
                coordinator=coordinator,
            )
    finally:
        coordinator.end(world_id)


async def _critical_section(
    world_id: int,
    gen: int,
    status: str,
    *,
    session_factory,
    registry: RunnerRegistry,
    coordinator: TransitionCoordinator,
) -> TransitionResult:
    """Sección crítica bajo el lock del mundo: valida, escribe, reconcilia.

    Entre las validaciones y el arranque de la escritura NO hay ningún
    ``await``: es un bloque atómico en el event loop, de modo que la marca
    de ``begin_delete`` (también síncrona) no puede insertarse en medio
    (§4.6).  Tras CADA suspensión la vigencia se re-evalúa antes de tocar
    el estado operativo.
    """
    # -- validaciones de vigencia (§4.4, §4.6): síncronas, sin await ------
    if registry.is_deleting(world_id):
        raise TransitionError(
            f"world {world_id} is being deleted; refusing transition to "
            f"{status!r}"
        )
    runner = registry.get_existing(world_id)
    if runner is not None and runner.state in _CLOSED_STATES:
        raise TransitionError(
            f"world {world_id}: runner close has begun "
            f"(state={runner.state.value}); refusing transition to {status!r}"
        )
    if runner is None and status == RUNNING_STATUS:
        raise TransitionError(
            f"world {world_id}: cannot resume a world without a runner"
        )
    # Cancelación pendiente justo antes de iniciar la escritura (§4.5.1):
    # si aún no escribió, la operación aborta sin tocar nada.
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError(
            f"transition for world {world_id} cancelled before its write "
            "started"
        )

    # -- escritura persistida en worker thread (§4.5.1) --------------------
    write = asyncio.ensure_future(
        asyncio.to_thread(_write_status, session_factory, world_id, status)
    )
    coordinator._hold(write)  # referencia fuerte hasta terminar (regla 5)
    cancel_exc = await _await_real_end(write)
    write_exc = _task_exception(write)

    previous: str | None = None
    wrote = False
    if write_exc is None:
        previous, wrote = write.result()

    # -- resultado efectivo + reconciliación ANTES de liberar (§4.5.3-5) ---
    applied = False
    reconcile_exc: BaseException | None = None
    if write_exc is None:
        applied = coordinator._apply_in_memory(
            world_id, gen, status, registry,
        )
        if not applied and wrote:
            # Puerta rechazó una escritura confirmada: restaurar la fila
            # para que un mundo superviviente no quede contradictorio.
            seen, reconcile_exc = await _reconcile(
                session_factory, world_id, previous, coordinator,
            )
            if cancel_exc is None:
                cancel_exc = seen  # primera cancelación = original

    # -- propagación (§4.5.6): primero lo invisible, luego lo visible ------
    if reconcile_exc is not None and cancel_exc is not None:
        coordinator._record_unseen(world_id, "reconciliation", reconcile_exc)
    if cancel_exc is not None:
        if write_exc is not None:
            coordinator._record_unseen(world_id, "status write", write_exc)
        raise cancel_exc  # cancelación original, tras reconciliar (§4.5.6)
    if reconcile_exc is not None:
        raise reconcile_exc  # visible para este llamador: no se oculta
    if write_exc is not None:
        raise write_exc  # sin cancelación: el error original llega al llamador

    return TransitionResult(
        world_id=world_id,
        status=status,
        generation=gen,
        applied=applied,
    )


async def _reconcile(
    session_factory,
    world_id: int,
    previous: str | None,
    coordinator: TransitionCoordinator,
) -> tuple[asyncio.CancelledError | None, BaseException | None]:
    """Restaura el estado previo de la fila en un worker thread (§4.5.4-5).

    Returns:
        ``(cancelación_del_llamador, error_de_reconciliación)`` — ambos
        ``None`` cuando la restauración confirmó y el llamador no fue
        cancelado.  Un fallo aquí se devuelve para que el llamador lo
        registre/lance: jamás se descarta en silencio.
    """
    if previous is None:  # defensivo: wrote=True implica previous no nulo
        return None, None
    revert = asyncio.ensure_future(
        asyncio.to_thread(_revert_status, session_factory, world_id, previous)
    )
    coordinator._hold(revert)
    seen = await _await_real_end(revert)
    exc = _task_exception(revert)
    if exc is None and not revert.result():
        logger.info(
            "world %s: row vanished while reconciling (world deleted); "
            "nothing left to restore",
            world_id,
        )
    return seen, exc


async def pause_world(
    world_id: int,
    *,
    session_factory,
    registry: RunnerRegistry,
    coordinator: TransitionCoordinator | None = None,
) -> TransitionResult:
    """Transiciona ``world_id`` a pausado (persistido + operativo + espejo).

    El runner deja de ejecutar ticks en su próxima vuelta (``_run`` es la
    única ruta de ticks, DEC06/DEC14); ``WorldRunner.paused`` y el espejo
    ``SimEngine.paused`` quedan en ``True`` y ``WorldRow.status`` en
    ``"paused"``.  Un mundo sin runner solo recibe la escritura de fila
    (no hay estado operativo que actualizar).

    Raises:
        TransitionError: vigencia no satisfecha (mundo eliminándose,
            shutdown iniciado, runner cerrado o destino inválido).
        WorldNotFoundError: la fila desapareció durante la escritura.
        BaseException: el error original de la escritura de base de datos.
        asyncio.CancelledError: el llamador fue cancelado; se propaga SOLO
            tras reconciliar el resultado efectivo de la escritura.
    """
    return await _run_transition(
        world_id, PAUSED_STATUS,
        session_factory=session_factory, registry=registry,
        coordinator=coordinator or transition_coordinator,
    )


async def resume_world(
    world_id: int,
    *,
    session_factory,
    registry: RunnerRegistry,
    coordinator: TransitionCoordinator | None = None,
) -> TransitionResult:
    """Transiciona ``world_id`` a en ejecución (persistido + operativo + espejo).

    Comprueba que el runner sigue siendo válido y utilizable (§4.4): no
    puede haber comenzado su ``close()`` ni estar en eliminación, y debe
    existir (reanimar una fila sin runner dejaría ``"running"`` persistido
    sin nadie que ejecute ticks — se rechaza).  No llama a ``start()`` ni
    crea tareas: la tarea principal continúa por la ruta ordinaria de
    ``_run`` si ya existía, por lo que nunca se duplica una ejecución.

    Misma semántica de errores/cancelación que :func:`pause_world`.
    """
    return await _run_transition(
        world_id, RUNNING_STATUS,
        session_factory=session_factory, registry=registry,
        coordinator=coordinator or transition_coordinator,
    )


#: Instancia por defecto (la usan el WebSocket y el lifespan de la app;
#: las pruebas crean las suyas).
transition_coordinator = TransitionCoordinator()
