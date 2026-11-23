"""RecoveryManager: recuperación de mundos desde checkpoints persistidos.

Recupera el estado completo de un mundo simulado a partir de los
checkpoints almacenados en ``world_checkpoints``, validando integridad
criptográfica y estructura antes de reconstruir el motor.

El componente no inicia simulaciones, no crea runners y no modifica el
ciclo de vida del servidor. Su responsabilidad termina al devolver el
motor reconstruido (o un resultado de fallo explícito) junto con los
metadatos necesarios para la integración posterior con el runner.

Diseñado para ser testeable sin iniciar el servidor web y sin depender
de FastAPI ni de WorldRunner.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from worldsim.export.checkpoint import (
    CHECKPOINT_VERSION,
    checkpoint_to_dict,
    engine_from_checkpoint,
)
from worldsim.server.db import WorldCheckpoint, WorldRow
from worldsim.server.repository import CheckpointRepository, WorldRepository


@dataclass(frozen=True)
class RecoveryResult:
    """Resultado de una operación de recuperación de mundo."""

    status: str
    """Estado de la recuperación: 'ok', 'no_checkpoints',
    'not_recoverable'."""

    world_id: int
    engine: Any | None = None
    world_status: str | None = None
    checkpoint_sequence: int | None = None
    checkpoint_month: int | None = None
    checkpoint_type: str | None = None
    checkpoint_id: int | None = None
    diagnostics: str | None = None


# Estados del mundo que permiten recuperación automática.
_RECOVERABLE_STATUSES = {"running", "paused"}


def _sha256(data: bytes) -> str:
    """Devuelve el SHA-256 hex digest de los bytes exactos."""
    return hashlib.sha256(data).hexdigest()


class RecoveryManager:
    """Recupera un mundo desde sus checkpoints persistidos.

    Para cada mundo:

    1. Lee el estado del mundo (status).
    2. Si el estado no es recuperable, devuelve un resultado explícito sin
       tocar los checkpoints.
    3. Consulta los checkpoints del mundo ordenados por secuencia descendente.
    4. Evalúa cada uno desde el más reciente: checksum, versión, estructura
       y reconstrucción semántica del motor.
    5. Si un checkpoint falla, lo marca como inválido y prueba el anterior.
    6. Devuelve el primer motor que se reconstruya correctamente, o un
       resultado de fallo si ninguno es válido.
    """

    def __init__(
        self,
        session_factory: Any | None = None,
    ) -> None:
        self.session_factory = session_factory

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def recover(self, world_id: int) -> RecoveryResult:
        """Recupera el mundo identificado por ``world_id``.

        Returns:
            Un :class:`RecoveryResult` con el motor reconstruido cuando la
            recuperación tiene éxito, o un estado explícito de fallo.
        """
        # 1. Leer el estado del mundo.
        world_row: WorldRow | None = None
        with self._repo_session(WorldRepository) as world_repo:
            world_row = world_repo.get_world(world_id)

        if world_row is None:
            return RecoveryResult(
                status="not_recoverable",
                world_id=world_id,
                diagnostics=f"World {world_id} does not exist",
            )

        world_status = world_row.status
        if world_status not in _RECOVERABLE_STATUSES:
            return RecoveryResult(
                status="not_recoverable",
                world_id=world_id,
                world_status=world_status,
                diagnostics=(
                    f"World {world_id} status '{world_status}' is not "
                    "recoverable"
                ),
            )

        # 2. Obtener checkpoints válidos del mundo, ordenados por secuencia
        #    desc. Los checkpoints ya invalidados (is_valid=0) se excluyen
        #    para no reevaluarlos en recuperaciones posteriores.
        checkpoints: list[WorldCheckpoint] = []
        with self._repo_session(CheckpointRepository) as cp_repo:
            checkpoints = cp_repo.get_valid_checkpoints_for_world(world_id)

            if not checkpoints:
                # Distinguir "no hay registros históricos" de "existen
                # registros pero todos están invalidados" sin reevaluar
                # ninguno.
                all_checkpoints = cp_repo.get_checkpoints_for_world(world_id)
                if not all_checkpoints:
                    return RecoveryResult(
                        status="no_checkpoints",
                        world_id=world_id,
                        world_status=world_status,
                        diagnostics=(
                            f"World {world_id} has no checkpoints to "
                            "recover from"
                        ),
                    )
                return RecoveryResult(
                    status="not_recoverable",
                    world_id=world_id,
                    world_status=world_status,
                    diagnostics=(
                        f"World {world_id} has {len(all_checkpoints)} "
                        "historical checkpoint(s) but none are valid "
                        "(all are marked invalid)"
                    ),
                )

            # 3. Evaluar cada checkpoint desde el más reciente.
            last_error: str | None = None
            for cp in checkpoints:
                try:
                    engine = self._evaluate_checkpoint(cp)
                except _RecoverableCheckpointError as exc:
                    # Checkpoint corrupto: marcar inválido y continuar.
                    cp_repo.mark_checkpoint_invalid(cp.id)
                    last_error = exc.message
                    continue
                except Exception as exc:
                    # Error de infraestructura (BD, etc.): no marcar inválido,
                    # propagar para que el caller decida.
                    last_error = f"infrastructure error: {exc!r}"
                    raise

                # 4. Éxito: devolver el motor reconstruido.
                # La pausa se toma del estado persistido del mundo (fuente de
                # verdad), no del contenido del checkpoint.
                engine.paused = world_status == "paused"
                return RecoveryResult(
                    status="ok",
                    world_id=world_id,
                    engine=engine,
                    world_status=world_status,
                    checkpoint_sequence=cp.sequence,
                    checkpoint_month=cp.month,
                    checkpoint_type=cp.checkpoint_type,
                    checkpoint_id=cp.id,
                    diagnostics=(
                        f"Recovered from checkpoint sequence {cp.sequence}"
                        f" (month {cp.month})"
                    ),
                )

        # 5. Ningún checkpoint fue recuperable.
        return RecoveryResult(
            status="not_recoverable",
            world_id=world_id,
            world_status=world_status,
            diagnostics=(
                f"No recoverable checkpoint for world {world_id}; "
                f"last error: {last_error}"
            ),
        )

    # ------------------------------------------------------------------
    # Evaluación de un checkpoint individual
    # ------------------------------------------------------------------

    def _evaluate_checkpoint(self, cp: WorldCheckpoint) -> Any:
        """Evalúa y reconstruye un checkpoint.

        Lanza :class:`_RecoverableCheckpointError` si el checkpoint es
        inválido por corrupción de contenido (checksum, versión, estructura,
        reconstrucción fallida). Lanza otras excepciones para errores de
        infraestructura.
        """
        # 5.2 Validación del checksum sobre los bytes exactos almacenados.
        actual = _sha256(cp.payload)
        if actual != cp.checksum:
            raise _RecoverableCheckpointError(
                f"checksum mismatch for checkpoint {cp.id} "
                f"(sequence {cp.sequence}): expected {cp.checksum}, "
                f"got {actual}"
            )

        # 5.3 Decodificar payload como UTF-8 y parsear JSON.
        try:
            payload_text = cp.payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _RecoverableCheckpointError(
                f"checkpoint {cp.id} payload is not valid UTF-8: {exc!r}"
            ) from exc

        try:
            state = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise _RecoverableCheckpointError(
                f"checkpoint {cp.id} payload is not valid JSON: {exc!r}"
            ) from exc

        # Validar versión del checkpoint.
        state_version = state.get("_checkpoint_version")
        if state_version != CHECKPOINT_VERSION:
            raise _RecoverableCheckpointError(
                f"checkpoint {cp.id} unsupported version "
                f"{state_version!r}; expected {CHECKPOINT_VERSION}"
            )

        # 5.3 Reconstruir el motor utilizando las funciones existentes.
        # engine_from_checkpoint valida versión, tipo y lanza ValueError
        # si el formato es incompatible o el checkpoint está corrupto.
        try:
            engine = engine_from_checkpoint(state)
        except (ValueError, KeyError, TypeError) as exc:
            raise _RecoverableCheckpointError(
                f"checkpoint {cp.id} failed semantic reconstruction: "
                f"{exc!r}"
            ) from exc

        # Comprobar que la reconstrucción sea semánticamente válida.
        if engine is None:
            raise _RecoverableCheckpointError(
                f"checkpoint {cp.id} produced no engine"
            )

        return engine

    # ------------------------------------------------------------------
    # Sesiones
    # ------------------------------------------------------------------

    def _repo_session(self, repo_class: type):
        """Context manager que provee un repositorio con sesión propia."""
        if self.session_factory is not None:
            repo = repo_class(session=self.session_factory)
        else:
            repo = repo_class()
        return _RepoSession(repo)


class _RepoSession:
    """Context manager que cierra el repositorio si posee la sesión."""

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    def __enter__(self) -> Any:
        return self._repo

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._repo.close()


class _RecoverableCheckpointError(Exception):
    """Error de corrupción de un checkpoint individual (no infraestructura)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
