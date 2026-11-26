"""Servicio de desafíos F7-D: operaciones globales atómicas e idempotentes.

Implementa el ciclo de vida del desafío (CONTRACTS §17.3, DEC-F7D-04) y las
operaciones globales de ámbito desafío (DEC-F7D-15).

Cada operación de desafío ejecuta una transacción SQL atómica con identidad
de petición idempotente (client_idempotency_key + request_hash SHA-256).
Nunca duplica el protocolo T1/T2 de F7-C; las operaciones que afectan a un
mundo individual usan la infraestructura F7-C existente.

Sesión: los repositorios reciben la MISMA instancia de sesión que ejecutan;
el servicio es dueño del límite transaccional.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from worldsim.server import db as db_module
from worldsim.server.db import (
    ChallengeParticipantRow,
    ChallengeRow,
    ChallengeOperationRow,
    WorldRow,
)
from worldsim.server.repository import ChallengeParticipantRepository, ChallengeRepository

logger = logging.getLogger(__name__)

# Métricas válidas para clasificación estadística (CONTRACTS §17.4).
# SOLO las métricas que EXISTEN en el modelo y tienen definición determinista.
# gold: NO EXISTE en el modelo → EXCLUIDA (F7-D-R1-D).
# influence_max: se computa como máximo de tiles bajo influencia por reino.
VALID_METRICS = ("total_population", "wars_won", "tech_level", "influence_max")

# Orden de prioridad y dirección (DESC = mayor es mejor, ASC = menor es mejor)
METRIC_SORT = {
    "total_population": "desc",
    "wars_won": "desc",
    "tech_level": "desc",
    "influence_max": "desc",
}

# Desempates: tech_level desc, wars_won desc, total_population desc, user_id asc
TIEBREAKER_ORDER = ("tech_level", "wars_won", "total_population", "user_id")


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------


class ChallengeError(RuntimeError):
    """Error base de operaciones de desafío."""


class ChallengeNotFound(ChallengeError):
    """El desafío no existe."""


class ParticipantNotFound(ChallengeError):
    """El participante no existe en el desafío."""


class WorldNotFoundForParticipant(ChallengeError):
    """El participante no tiene mundo asociado (preparación incompleta)."""


class ChallengeStateError(ChallengeError):
    """Transición de estado no válida para el desafío."""


class IdempotencyConflict(ChallengeError):
    """Misma clave de idempotencia con contenido distinto (HTTP 409)."""

    def __init__(self) -> None:
        super().__init__("idempotency_conflict")


class ChallengeLocked(ChallengeError):
    """El desafío está en un estado que no admite la operación solicitada."""


# ---------------------------------------------------------------------------
# Tipos de datos
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChallengeParams:
    """Parámetros canónicos de una petición de desafío (input de request_hash).

    Sólo campos semánticos; no incluye marcas de tiempo de recepción ni
    metadatos de transporte (§16.11.2 aplicado por analogía).
    """

    challenge_id: int
    organizer_user_id: int
    name: str
    config_json: str
    rules_version: str
    duration_ticks: int


@dataclass
class ChallengeResult:
    """Resultado de una operación de desafío."""

    challenge: ChallengeRow
    is_retry: bool = False  # True: reintento idempotente, sin nuevos efectos
    # §17.13 / FIX10-A-IDEM: ``outcome`` durable de la operación original
    # recuperado en un reintento (p. ej. "preparing" aunque el desafío ya
    # haya avanzado a "running"): permite reconstruir el RESULTADO original,
    # no el estado actual del desafío.
    original_outcome: str | None = None


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def _canonical_challenge_request(params: ChallengeParams) -> bytes:
    """Representación canónica versionada hasheada en request_hash."""
    canonical = {
        "v": 1,
        "challenge_id": params.challenge_id,
        "organizer_user_id": params.organizer_user_id,
        "name": params.name,
        "config_json": params.config_json,
        "rules_version": params.rules_version,
        "duration_ticks": params.duration_ticks,
    }
    return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _compute_request_hash(params: ChallengeParams) -> str:
    return hashlib.sha256(_canonical_challenge_request(params)).hexdigest()


def compute_operation_request_hash(
    operation: str, challenge_id: int, **fields: Any
) -> str:
    """SHA-256 de la representación canónica versionada de los datos
    semánticamente relevantes de una petición de operación global (§17.13).

    F7-D-I1-FIX10-A-IDEM: input de ``request_hash`` para las operaciones
    cuyo contenido canónico es la operación + ``challenge_id`` (más campos
    explícitos de la petición, p. ej. ``resolution`` en ``resolve``). El
    ámbito ``(challenge_id, actor, key)`` vive fuera del hash y el actor
    nunca se deriva del payload: se toma de la autenticación del servidor.
    """
    canonical: dict[str, Any] = {
        "v": 1,
        "operation": operation,
        "challenge_id": challenge_id,
    }
    canonical.update(fields)
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _require_state(challenge: ChallengeRow, allowed: set[str], operation: str) -> None:
    """Verifica que el desafío esté en uno de los estados permitidos."""
    if challenge.state not in allowed:
        raise ChallengeStateError(
            f"Desafío {challenge.id} en estado '{challenge.state}'; "
            f"se requiere {'/'.join(sorted(allowed))} para {operation}"
        )


def _idempotent_operation_row(
    session,
    challenge_id: int,
    actor_user_id: int,
    client_idempotency_key: str,
    request_hash: str | None,
) -> ChallengeOperationRow | None:
    """Ámbito de idempotencia CONTRACTS §17.13 sobre ``challenge_operations``.

    Se evalúa ANTES de cualquier retorno temprano por estado, para que una
    clave reutilizada con contenido distinto reciba siempre el conflicto 409
    exigido por el contrato, y no un ``is_retry`` silencioso.

    - sin fila → ``None`` (primera ejecución de la clave en este ámbito);
    - fila + mismo hash → devuelve la fila: el caller recupera el RESULTADO
      ORIGINAL (``original_outcome``) y retorna SIN repetir efectos, antes de
      cualquier lógica por estado;
    - fila + hash distinto → ``IdempotencyConflict`` (HTTP 409).
    """
    existing = session.scalars(
        select(ChallengeOperationRow).where(
            ChallengeOperationRow.challenge_id == challenge_id,
            ChallengeOperationRow.actor_user_id == actor_user_id,
            ChallengeOperationRow.client_idempotency_key
            == client_idempotency_key,
        )
    ).first()
    if existing is None:
        return None
    if (existing.request_hash or "") != (request_hash or ""):
        raise IdempotencyConflict()
    return existing


# ---------------------------------------------------------------------------
# Servicio
# ---------------------------------------------------------------------------


class ChallengeService:
    """Operaciones globales de desafío: atómicas e idempotentes.

    Cada método ejecuta una única transacción SQL. Los repositorios reciben
    la misma sesión que el servicio; el servicio es dueño del límite
    transaccional (commit/rollback).
    """

    def __init__(self, session_factory=None) -> None:
        # FIX10-A-IDEM: la fábrica por defecto se resuelve en tiempo de
        # ejecución sobre ``db.SessionLocal`` (mismo patrón que app.py y
        # auth.py), en lugar de enlazar el símbolo al importar el módulo.
        # En producción el objeto es el mismo; permite que los pruebas
        # sustituyan la base aislada sin tocar el servicio.
        self.session_factory = (
            session_factory if session_factory is not None else db_module.SessionLocal
        )

    # ------------------------------------------------------------------
    # Creación
    # ------------------------------------------------------------------

    def create_challenge(
        self,
        organizer_user_id: int,
        name: str,
        config_json: str,
        rules_version: str,
        duration_ticks: int,
        client_idempotency_key: str,
        request_hash: str | None = None,
        session=None,
    ) -> ChallengeResult:
        """Crea un desafío en estado ``draft`` (idempotente).

        Si ya existe una operación con la misma clave de idempotencia y el
        mismo request_hash, devuelve el desafío original sin crear uno nuevo.
        """
        own_session = session is None
        if own_session:
            session = self.session_factory()
        try:
            return self._create_challenge(
                session,
                organizer_user_id,
                name,
                config_json,
                rules_version,
                duration_ticks,
                client_idempotency_key,
                request_hash,
            )
        except Exception:
            # El servicio es dueño del límite transaccional incluso cuando
            # recibe una Session creada por el caller.
            session.rollback()
            raise
        finally:
            if own_session:
                session.close()

    def _create_challenge(
        self,
        session,
        organizer_user_id: int,
        name: str,
        config_json: str,
        rules_version: str,
        duration_ticks: int,
        client_idempotency_key: str,
        request_hash: str | None,
    ) -> ChallengeResult:
        repo = ChallengeRepository(session)

        # El hash de creación se define antes de asignar el ID persistido.
        # El endpoint HTTP ya lo calcula con challenge_id=0; el servicio
        # conserva ese valor exacto y usa la misma regla si no se suministra.
        canonical_hash = request_hash or _compute_request_hash(
            ChallengeParams(
                challenge_id=0,
                organizer_user_id=organizer_user_id,
                name=name,
                config_json=config_json,
                rules_version=rules_version,
                duration_ticks=duration_ticks,
            )
        )

        # La tabla challenge_creation_requests es la única fuente de verdad
        # para la idempotencia de creación (actor + clave).
        existing_request = repo.get_challenge_creation_request(
            organizer_user_id, client_idempotency_key
        )
        if existing_request is not None:
            if existing_request.request_hash != canonical_hash:
                raise IdempotencyConflict()
            existing = session.get(ChallengeRow, existing_request.challenge_id)
            if existing is None:
                raise ChallengeError(
                    "challenge_creation_request_without_challenge"
                )
            op_row = repo.get_challenge_operation(
                existing.id, organizer_user_id, client_idempotency_key
            )
            result = ChallengeResult(
                challenge=existing,
                is_retry=True,
                original_outcome=op_row.outcome if op_row is not None else None,
            )
            session.commit()
            return result

        # Validar parámetros antes de crear cualquier fila.
        if duration_ticks <= 0:
            raise ChallengeError("duration_ticks debe ser > 0")
        if rules_version not in ("1.0",):
            raise ChallengeError(f"rules_version no soportado: {rules_version}")

        challenge = ChallengeRow(
            organizer_user_id=organizer_user_id,
            name=name,
            config_json=config_json,
            rules_version=rules_version,
            duration_ticks=duration_ticks,
            state="draft",
            created_at=self._now(),
        )
        session.add(challenge)
        session.flush()
        session.refresh(challenge)

        # INSERT protegido por UNIQUE(actor_user_id, client_idempotency_key).
        # Si otra transacción ganó la carrera, revertimos TODO lo insertado
        # por esta solicitud y releemos en una transacción limpia.
        try:
            repo.record_challenge_creation_request(
                actor_user_id=organizer_user_id,
                client_idempotency_key=client_idempotency_key,
                request_hash=canonical_hash,
                challenge_id=challenge.id,
            )
        except IntegrityError:
            session.rollback()
            winner_request = repo.get_challenge_creation_request(
                organizer_user_id, client_idempotency_key
            )
            if winner_request is None:
                # No era una colisión de la clave idempotente; preservar el
                # error original (p. ej. FK u otra restricción).
                raise
            if winner_request.request_hash != canonical_hash:
                raise IdempotencyConflict()
            winner = session.get(ChallengeRow, winner_request.challenge_id)
            if winner is None:
                raise ChallengeError(
                    "challenge_creation_request_without_challenge"
                )
            winner_op = repo.get_challenge_operation(
                winner.id, organizer_user_id, client_idempotency_key
            )
            result = ChallengeResult(
                challenge=winner,
                is_retry=True,
                original_outcome=(
                    winner_op.outcome if winner_op is not None else None
                ),
            )
            session.commit()
            return result

        # La operación original se conserva separada de la identidad de
        # creación. Las tres filas se confirman en el mismo COMMIT.
        repo.record_challenge_operation(
            organizer_user_id=organizer_user_id,
            client_idempotency_key=client_idempotency_key,
            request_hash=canonical_hash,
            operation="create",
            challenge_id=challenge.id,
            outcome="draft",
        )

        session.commit()
        return ChallengeResult(challenge=challenge, is_retry=False)

    # ------------------------------------------------------------------
    # Publicación (congela configuración)
    # ------------------------------------------------------------------

    def publish_challenge(
        self,
        challenge_id: int,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None = None,
        session=None,
    ) -> ChallengeResult:
        """Publica un desafío (draft → published). Congela la configuración."""
        own_session = session is None
        if own_session:
            session = self.session_factory()
        try:
            return self._publish_challenge(
                session, challenge_id, organizer_user_id,
                client_idempotency_key, request_hash,
            )
        finally:
            if own_session:
                session.close()

    def _publish_challenge(
        self,
        session,
        challenge_id: int,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None,
    ) -> ChallengeResult:
        repo = ChallengeRepository(session)
        challenge = repo.get_challenge(challenge_id)
        if challenge is None:
            raise ChallengeNotFound(f"Desafío {challenge_id} no existe")
        if challenge.organizer_user_id != organizer_user_id:
            raise ChallengeError("No es el organizador del desafío")

        # Ámbito de idempotencia §17.13 (FIX10-A-IDEM): clave+hash comparados
        # ANTES de cualquier retorno temprano por estado. Misma clave + mismo
        # hash → resultado original sin repetir efectos; hash distinto → 409.
        row = _idempotent_operation_row(
            session, challenge.id, organizer_user_id,
            client_idempotency_key, request_hash,
        )
        if row is not None:
            return ChallengeResult(
                challenge=challenge, is_retry=True, original_outcome=row.outcome
            )

        # Idempotencia: si ya está en el estado objetivo, devolver como reintento
        if challenge.state == "published":
            return ChallengeResult(challenge=challenge, is_retry=True)

        _require_state(challenge, {"draft"}, "publicar")

        # Validar primary_metric en config inmutable
        import json
        try:
            config = json.loads(challenge.config_json)
            primary = config.get("primary_metric")
            allowed = {"total_population", "wars_won", "tech_level", "influence_max"}
            if primary not in allowed:
                raise ChallengeError(f"primary_metric inválido o ausente: {primary}. Valores permitidos: {allowed}")
        except json.JSONDecodeError:
            raise ChallengeError("config_json no es JSON válido")
        except ChallengeError:
            raise
        except Exception as e:
            raise ChallengeError(f"Error validando configuración: {e}")

        challenge.state = "published"
        session.flush()

        # Procedencia durable de la publicación (§17.13 lista "publicar" como
        # operación global de desafío; era el único call site sin registro).
        repo.record_challenge_operation(
            organizer_user_id=organizer_user_id,
            client_idempotency_key=client_idempotency_key,
            request_hash=request_hash or "",
            operation="publish",
            challenge_id=challenge.id,
            outcome="published",
        )

        session.commit()
        return ChallengeResult(challenge=challenge, is_retry=False)

    # ------------------------------------------------------------------
    # Abrir inscripciones (published → enrollment_open)
    # ------------------------------------------------------------------

    def open_enrollment(
        self,
        challenge_id: int,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None = None,
        session=None,
    ) -> ChallengeResult:
        """Abrir inscripciones (published → enrollment_open)."""
        own_session = session is None
        if own_session:
            session = self.session_factory()
        try:
            return self._open_enrollment(
                session, challenge_id, organizer_user_id,
                client_idempotency_key, request_hash,
            )
        finally:
            if own_session:
                session.close()

    def _open_enrollment(
        self,
        session,
        challenge_id: int,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None,
    ) -> ChallengeResult:
        repo = ChallengeRepository(session)
        challenge = repo.get_challenge(challenge_id)
        if challenge is None:
            raise ChallengeNotFound(f"Desafío {challenge_id} no existe")
        if challenge.organizer_user_id != organizer_user_id:
            raise ChallengeError("No es el organizador del desafío")

        # Ámbito de idempotencia §17.13 (FIX10-A-IDEM): ANTES del retorno
        # temprano por estado.
        row = _idempotent_operation_row(
            session, challenge.id, organizer_user_id,
            client_idempotency_key, request_hash,
        )
        if row is not None:
            return ChallengeResult(
                challenge=challenge, is_retry=True, original_outcome=row.outcome
            )

        # Idempotencia: si ya está en el estado objetivo, devolver como reintento
        if challenge.state == "enrollment_open":
            return ChallengeResult(challenge=challenge, is_retry=True)

        _require_state(challenge, {"published"}, "abrir inscripciones")

        challenge.state = "enrollment_open"
        session.flush()

        challenge_repo = ChallengeRepository(session)
        challenge_repo.record_challenge_operation(
            organizer_user_id=organizer_user_id,
            client_idempotency_key=client_idempotency_key,
            request_hash=request_hash or "",
            operation="open_enrollment",
            challenge_id=challenge.id,
            outcome="enrollment_open",
        )

        session.commit()
        return ChallengeResult(challenge=challenge, is_retry=False)

    def enroll_participant(
        self,
        challenge_id: int,
        user_id: int,
        client_idempotency_key: str,
        request_hash: str | None = None,
        session=None,
    ) -> ChallengeResult:
        """Inscrive a un participante (idempotente).

        Registra la participación sin crear el mundo todavía. El mundo se
        crea en la fase ``preparing``.
        """
        own_session = session is None
        if own_session:
            session = self.session_factory()
        try:
            return self._enroll_participant(
                session, challenge_id, user_id,
                client_idempotency_key, request_hash,
            )
        finally:
            if own_session:
                session.close()

    def _enroll_participant(
        self,
        session,
        challenge_id: int,
        user_id: int,
        client_idempotency_key: str,
        request_hash: str | None,
    ) -> ChallengeResult:
        repo = ChallengeParticipantRepository(session)
        challenge_repo = ChallengeRepository(session)

        challenge = challenge_repo.get_challenge(challenge_id)
        if challenge is None:
            raise ChallengeNotFound(f"Desafío {challenge_id} no existe")

        # Ámbito de idempotencia §17.13 (FIX10-A-IDEM): ANTES de cualquier retorno temprano por estado.
        row = _idempotent_operation_row(
            session, challenge.id, user_id,
            client_idempotency_key, request_hash,
        )
        if row is not None:
            return ChallengeResult(
                challenge=challenge, is_retry=True, original_outcome=row.outcome
            )

        _require_state(challenge, {"published", "enrollment_open"}, "inscribirse")

        # Verificar que el usuario no esté ya inscrito en este desafío
        existing_participant = repo.get_participant(challenge_id, user_id)
        if existing_participant is not None:
            # Ya inscrito: idempotente sin registro de procedencia (estado existente)
            return ChallengeResult(
                challenge=challenge, is_retry=True
            )

        participant = ChallengeParticipantRow(
            challenge_id=challenge_id,
            user_id=user_id,
            # NULL representa "sin mundo asignado". Evita violar la FK a
            # worlds.id y permite múltiples participantes antes de preparing.
            # El valor 0 solo se tolera al leer registros legacy existentes.
            world_id=None,
            public_alias=f"participant-{uuid.uuid4().hex}",
            state="inscrito",
            joined_at=self._now(),
        )
        session.add(participant)
        session.flush()
        session.refresh(participant)

        challenge_repo.record_challenge_operation(
            organizer_user_id=user_id,
            client_idempotency_key=client_idempotency_key,
            request_hash=request_hash or "",
            operation="enroll",
            challenge_id=challenge_id,
            outcome="enrolled",
        )

        session.commit()
        return ChallengeResult(challenge=challenge, is_retry=False)

    # ------------------------------------------------------------------
    # Cierre de inscripciones
    # ------------------------------------------------------------------

    def close_enrollment(
        self,
        challenge_id: int,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None = None,
        session=None,
    ) -> ChallengeResult:
        """Cierra inscripciones (enrollment_open → enrollment_closed)."""
        own_session = session is None
        if own_session:
            session = self.session_factory()
        try:
            return self._close_enrollment(
                session, challenge_id, organizer_user_id,
                client_idempotency_key, request_hash,
            )
        finally:
            if own_session:
                session.close()

    def _close_enrollment(
        self,
        session,
        challenge_id: int,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None,
    ) -> ChallengeResult:
        repo = ChallengeRepository(session)
        challenge = repo.get_challenge(challenge_id)
        if challenge is None:
            raise ChallengeNotFound(f"Desafío {challenge_id} no existe")
        if challenge.organizer_user_id != organizer_user_id:
            raise ChallengeError("No es el organizador del desafío")

        # Ámbito de idempotencia §17.13 (FIX10-A-IDEM): ANTES de cualquier retorno temprano por estado.
        row = _idempotent_operation_row(
            session, challenge.id, organizer_user_id,
            client_idempotency_key, request_hash,
        )
        if row is not None:
            return ChallengeResult(
                challenge=challenge, is_retry=True, original_outcome=row.outcome
            )

        # Idempotencia: si ya está en el estado objetivo, devolver como reintento
        if challenge.state == "enrollment_closed":
            return ChallengeResult(challenge=challenge, is_retry=True)

        _require_state(challenge, {"enrollment_open"}, "cerrar inscripciones")

        challenge.state = "enrollment_closed"
        challenge.closed_at = self._now()
        session.flush()

        repo.record_challenge_operation(
            organizer_user_id=organizer_user_id,
            client_idempotency_key=client_idempotency_key,
            request_hash=request_hash or "",
            operation="close_enrollment",
            challenge_id=challenge.id,
            outcome="enrollment_closed",
        )

        session.commit()
        return ChallengeResult(challenge=challenge, is_retry=False)

    # ------------------------------------------------------------------
    # Preparación (crear mundos derivados)
    # ------------------------------------------------------------------

    def prepare_worlds(
        self,
        challenge_id: int,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None = None,
        session=None,
    ) -> ChallengeResult:
        """Crea los mundos derivados para cada participante (enrollment_closed → preparing).

        La creación de cada mundo es atómica: si falla uno, se hace rollback
        completo y no quedan mundos parciales.
        """
        own_session = session is None
        if own_session:
            session = self.session_factory()
        try:
            return self._prepare_worlds(
                session, challenge_id, organizer_user_id,
                client_idempotency_key, request_hash,
            )
        finally:
            if own_session:
                session.close()

    def _prepare_worlds(
        self,
        session,
        challenge_id: int,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None,
    ) -> ChallengeResult:
        repo = ChallengeParticipantRepository(session)
        challenge_repo = ChallengeRepository(session)

        challenge = challenge_repo.get_challenge(challenge_id)
        if challenge is None:
            raise ChallengeNotFound(f"Desafío {challenge_id} no existe")
        if challenge.organizer_user_id != organizer_user_id:
            raise ChallengeError("No es el organizador del desafío")

        # Ámbito de idempotencia §17.13 (FIX10-A-IDEM): ANTES de cualquier retorno temprano por estado.
        row = _idempotent_operation_row(
            session, challenge.id, organizer_user_id,
            client_idempotency_key, request_hash,
        )
        if row is not None:
            return ChallengeResult(
                challenge=challenge, is_retry=True, original_outcome=row.outcome
            )

        _require_state(challenge, {"enrollment_closed"}, "preparar")

        # Idempotencia: si ya está preparado, devolver como reintento
        if challenge.state == "preparing":
            return ChallengeResult(challenge=challenge, is_retry=True)

        participants = repo.get_participants(challenge_id)
        if not participants:
            raise ChallengeError("No hay participantes inscritos para preparar")

        # Crear un mundo derivado para cada participante
        from worldsim.server.db import WorldRow

        config = json.loads(challenge.config_json)

        for participant in participants:
            # NULL es el estado normal "sin mundo asignado"; 0 se acepta
            # únicamente como sentinel legacy. Un ID positivo ya está asignado.
            if participant.world_id is not None and participant.world_id != 0:
                # F7-E.2 (B3-5): verificar el mundo asignado
                row = session.get(WorldRow, participant.world_id)
                if row is None:
                    # world_id persistido pero fila no existe: error
                    raise WorldNotFoundForParticipant(
                        f"Participante {participant.user_id} tiene world_id {participant.world_id} "
                        "pero la fila del mundo no existe"
                    )
                if row.deleted_at is not None:
                    # Mundo eliminado: fallar sin crear sustituto, sin cambiar
                    # el world_id de la participación, sin runner.
                    raise WorldNotFoundForParticipant(
                        f"Participante {participant.user_id} tiene mundo {participant.world_id} "
                        "asignado pero lógicamente eliminado (deleted_at IS NOT NULL)"
                    )
                # Mundo activo: idempotente, reutilizar asignación existente
                continue

            world = WorldRow(
                seed=int(config.get("seed", 42)),
                width=int(config.get("width", 200)),
                height=int(config.get("height", 80)),
                months_simulated=0,
                land_pct=0.0,
                state_json="{}",
                created_at=self._now(),
                name=f"{challenge.name} - Participante {participant.user_id}",
                mode=config.get("mode", "single"),
                season_years=int(config.get("season_years", 300)),
                season_over=0,
                enabled_races_json=json.dumps(config.get("enabled_races", [])),
                owner_user_id=participant.user_id,
                sea_level=float(config.get("sea_level", 0.2)),
                # NOTE: visibility NO se pasa aquí por preexistente TypeError
                # (WorldRow.visibility no está mapeado; B3-3 bloquea corregirlo).
                # El valor por defecto del modelo es 'private' (fail-closed).
                next_operation_sequence=1,
            )
            session.add(world)
            session.flush()
            session.refresh(world)

            participant.world_id = world.id
            participant.state = "preparando"
            session.flush()

        challenge.state = "preparing"
        session.flush()

        challenge_repo.record_challenge_operation(
            organizer_user_id=organizer_user_id,
            client_idempotency_key=client_idempotency_key,
            request_hash=request_hash or "",
            operation="prepare",
            challenge_id=challenge.id,
            outcome="preparing",
        )

        session.commit()
        return ChallengeResult(challenge=challenge, is_retry=False)

    # ------------------------------------------------------------------
    # Inicio de ejecución
    # ------------------------------------------------------------------

    def start_challenge(
        self,
        challenge_id: int,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None = None,
        session=None,
    ) -> ChallengeResult:
        """Inicia la ejecución (preparing → running).

        Solo puede iniciarse cuando todos los participantes tienen mundo
        preparado y asociado.
        """
        own_session = session is None
        if own_session:
            session = self.session_factory()
        try:
            return self._start_challenge(
                session, challenge_id, organizer_user_id,
                client_idempotency_key, request_hash,
            )
        finally:
            if own_session:
                session.close()

    def _start_challenge(
        self,
        session,
        challenge_id: int,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None,
    ) -> ChallengeResult:
        repo = ChallengeParticipantRepository(session)
        challenge_repo = ChallengeRepository(session)

        challenge = challenge_repo.get_challenge(challenge_id)
        if challenge is None:
            raise ChallengeNotFound(f"Desafío {challenge_id} no existe")
        if challenge.organizer_user_id != organizer_user_id:
            raise ChallengeError("No es el organizador del desafío")

        # Ámbito de idempotencia §17.13 (FIX10-A-IDEM): ANTES de cualquier retorno temprano por estado.
        row = _idempotent_operation_row(
            session, challenge.id, organizer_user_id,
            client_idempotency_key, request_hash,
        )
        if row is not None:
            return ChallengeResult(
                challenge=challenge, is_retry=True, original_outcome=row.outcome
            )

        # Idempotencia: si ya está en el estado objetivo, devolver como reintento
        if challenge.state == "running":
            return ChallengeResult(challenge=challenge, is_retry=True)

        _require_state(challenge, {"preparing"}, "iniciar")

        participants = repo.get_participants(challenge_id)
        # Verificar que todos tienen mundo asignado (world_id > 0)
        for participant in participants:
            if participant.world_id is None or participant.world_id == 0:
                raise ChallengeError(
                    f"Participante {participant.user_id} no tiene mundo preparado"
                )

        challenge.state = "running"
        session.flush()

        challenge_repo.record_challenge_operation(
            organizer_user_id=organizer_user_id,
            client_idempotency_key=client_idempotency_key,
            request_hash=request_hash or "",
            operation="start",
            challenge_id=challenge.id,
            outcome="running",
        )

        session.commit()
        return ChallengeResult(challenge=challenge, is_retry=False)

    # ------------------------------------------------------------------
    # Cancelación
    # ------------------------------------------------------------------

    def cancel_challenge(
        self,
        challenge_id: int,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None = None,
        session=None,
    ) -> ChallengeResult:
        """Cancela un desafío (solo antes de running)."""
        own_session = session is None
        if own_session:
            session = self.session_factory()
        try:
            return self._cancel_challenge(
                session, challenge_id, organizer_user_id,
                client_idempotency_key, request_hash,
            )
        finally:
            if own_session:
                session.close()

    def _cancel_challenge(
        self,
        session,
        challenge_id: int,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None,
    ) -> ChallengeResult:
        repo = ChallengeRepository(session)
        challenge = repo.get_challenge(challenge_id)
        if challenge is None:
            raise ChallengeNotFound(f"Desafío {challenge_id} no existe")
        if challenge.organizer_user_id != organizer_user_id:
            raise ChallengeError("No es el organizador del desafío")

        # Ámbito de idempotencia §17.13 (FIX10-A-IDEM): ANTES de cualquier retorno temprano por estado.
        row = _idempotent_operation_row(
            session, challenge.id, organizer_user_id,
            client_idempotency_key, request_hash,
        )
        if row is not None:
            return ChallengeResult(
                challenge=challenge, is_retry=True, original_outcome=row.outcome
            )

        # Idempotencia: si ya está en el estado objetivo, devolver como reintento
        if challenge.state == "canceled":
            return ChallengeResult(challenge=challenge, is_retry=True)

        _require_state(
            challenge,
            {"published", "enrollment_open", "enrollment_closed", "preparing"},
            "cancelar",
        )

        challenge.state = "canceled"
        session.flush()

        repo.record_challenge_operation(
            organizer_user_id=organizer_user_id,
            client_idempotency_key=client_idempotency_key,
            request_hash=request_hash or "",
            operation="cancel",
            challenge_id=challenge.id,
            outcome="canceled",
        )

        session.commit()
        return ChallengeResult(challenge=challenge, is_retry=False)

    # ------------------------------------------------------------------
    # Completar desafío y calcular resultados
    # ------------------------------------------------------------------

    def complete_challenge(
        self,
        challenge_id: int,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None = None,
        session=None,
    ) -> ChallengeResult:
        """Completa el desafío (running → completed): ejecuta duration_ticks,
        calcula métricas y persiste resultados (idempotente)."""
        own_session = session is None
        if own_session:
            session = self.session_factory()
        try:
            return self._complete_challenge(
                session, challenge_id, organizer_user_id,
                client_idempotency_key, request_hash,
            )
        finally:
            if own_session:
                session.close()

    def _complete_challenge(
        self,
        session,
        challenge_id: int,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None,
    ) -> ChallengeResult:
        repo = ChallengeParticipantRepository(session)
        challenge_repo = ChallengeRepository(session)

        challenge = challenge_repo.get_challenge(challenge_id)
        if challenge is None:
            raise ChallengeNotFound(f"Desafío {challenge_id} no existe")
        if challenge.organizer_user_id != organizer_user_id:
            raise ChallengeError("No es el organizador del desafío")

        # Ámbito de idempotencia §17.13 (FIX10-A-IDEM): ANTES de cualquier retorno temprano por estado.
        row = _idempotent_operation_row(
            session, challenge.id, organizer_user_id,
            client_idempotency_key, request_hash,
        )
        if row is not None:
            return ChallengeResult(
                challenge=challenge, is_retry=True, original_outcome=row.outcome
            )

        # Idempotencia: si ya está en el estado objetivo, devolver como reintento
        if challenge.state == "completed":
            return ChallengeResult(challenge=challenge, is_retry=True)

        _require_state(challenge, {"running"}, "completar")

        participants = repo.get_participants(challenge_id)
        if not participants:
            raise ChallengeError("No hay participantes para completar")

        # Verificar que todos tienen mundo
        for p in participants:
            if p.world_id is None:
                raise ChallengeError(f"Participante {p.user_id} sin mundo asociado")

        # Ejecutar duration_ticks en cada mundo y calcular métricas
        from worldsim.server.operation_service import OperationService
        from worldsim.server.runner import registry
        from worldsim.sim.engine import SimEngine
        from worldsim.worldgen.pipeline import generate_world
        from worldsim.worldgen import WorldConfig
        from worldsim.data.loaders import load_races, load_governments

        config = json.loads(challenge.config_json)
        duration_ticks = challenge.duration_ticks
        engine_version = config.get("engine_version", "1.0")

        operation_service = OperationService(self.session_factory, engine_version=engine_version)
        races = load_races(Path(__file__).resolve().parents[3] / "data" / "Races.txt")
        govs = load_governments(Path(__file__).resolve().parents[3] / "data" / "CivilizedGovernment.txt")

        for participant in participants:
            # Cargar estado actual del mundo
            world_row = session.get(WorldRow, participant.world_id)
            if world_row is None:
                raise WorldNotFoundForParticipant(
                    f"Mundo {participant.world_id} no encontrado para participante {participant.user_id}"
                )
            if world_row.deleted_at is not None:
                raise WorldNotFoundForParticipant(
                    f"Mundo {participant.world_id} eliminado logicamente "
                    f"para participante {participant.user_id}"
                )

            # Reconstruir engine desde state_json
            state = json.loads(world_row.state_json or "{}")
            eng = self._rebuild_engine_from_state(state, config, races, govs)

            # Ejecutar ticks restantes hasta duration_ticks
            # Nota: months_simulated ya cuenta los ticks ejecutados
            current_ticks = world_row.months_simulated
            if current_ticks < duration_ticks:
                for _ in range(duration_ticks - current_ticks):
                    eng.tick()
                world_row.months_simulated = duration_ticks
                from worldsim.export.state import engine_to_dict
                world_row.state_json = json.dumps(engine_to_dict(eng))

            # Calcular métricas del participante (mejor reino del participante)
            metrics = self._calculate_participant_metrics(eng, participant.user_id)
            participant.result_json = json.dumps(metrics)
            # calculated → persisted (DEC-F7D-19 / CONTRACTS §17.14): el
            # resultado queda escrito en result_json. La durabilidad se
            # consolida con el COMMIT final de esta misma transacción; no se
            # fabrica una transición artificial fuera de la completitud.
            participant.result_status = "persisted"
            participant.result_verified = 0
            participant.state = "finalizado"
            session.flush()

        challenge.state = "completed"
        challenge.completed_at = self._now()
        session.flush()

        challenge_repo.record_challenge_operation(
            organizer_user_id=organizer_user_id,
            client_idempotency_key=client_idempotency_key,
            request_hash=request_hash or "",
            operation="complete",
            challenge_id=challenge.id,
            outcome="completed",
        )

        # persisted → verified (DEC-F7D-19 / CONTRACTS §17.14): los resultados
        # han sido escritos y la completitud/precondiciones del mundo se han
        # comprobado en esta transacción, con la operación de procedencia
        # "complete" registrada de forma durable en el mismo ámbito. El estado
        # final se consolida en el COMMIT; result_verified se sincroniza aquí.
        for participant in participants:
            if participant.result_status == "persisted":
                participant.result_status = "verified"
                participant.result_verified = 1
        session.flush()

        session.commit()
        return ChallengeResult(challenge=challenge, is_retry=False)

    def confirm_results(
        self,
        challenge_id: int,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None = None,
        session=None,
    ) -> ChallengeResult:
        own_session = session is None
        if own_session:
            session = self.session_factory()
        try:
            return self._confirm_results(session, challenge_id, organizer_user_id, client_idempotency_key, request_hash)
        finally:
            if own_session:
                session.close()

    def _confirm_results(
        self,
        session,
        challenge_id: int,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None,
    ) -> ChallengeResult:
        repo = ChallengeParticipantRepository(session)
        challenge_repo = ChallengeRepository(session)

        challenge = challenge_repo.get_challenge(challenge_id)
        if challenge is None:
            raise ChallengeNotFound(f"Desafío {challenge_id} no existe")
        if challenge.organizer_user_id != organizer_user_id:
            from worldsim.server.db import UserRow
            user = session.get(UserRow, organizer_user_id)
            if user is None or not user.is_admin:
                raise ChallengeError("No autorizado para confirmar resultados")

        # Ámbito de idempotencia §17.13: clave reutilizada con contenido
        # distinto → 409 ANTES del retorno temprano por estado.
        _idempotent_operation_row(
            session, challenge.id, organizer_user_id,
            client_idempotency_key, request_hash,
        )

        # Idempotencia: si ya confirmado, devolver
        participants = repo.get_participants(challenge_id)
        if all(p.result_status == "confirmed" for p in participants):
            return ChallengeResult(challenge=challenge, is_retry=True)

        _require_state(challenge, {"completed"}, "confirmar resultados")

        # Solo confirmar resultados verificados
        for p in participants:
            if p.result_status == "verified":
                p.result_status = "confirmed"
                p.result_verified = 1

        session.flush()
        challenge_repo.record_challenge_operation(
            organizer_user_id=organizer_user_id,
            client_idempotency_key=client_idempotency_key,
            request_hash=request_hash or "",
            operation="confirm",
            challenge_id=challenge.id,
            outcome="confirmed",
        )
        session.commit()
        return ChallengeResult(challenge=challenge, is_retry=False)

    def _rebuild_engine_from_state(self, state: dict, config: dict, races, govs) -> SimEngine:
        """Reconstruye un SimEngine desde state_json + config_json."""
        from worldsim.export.checkpoint import engine_from_checkpoint
        from worldsim.sim.engine import SimEngine
        from worldsim.worldgen.pipeline import generate_world
        from worldsim.worldgen import WorldConfig

        # Si el state es un checkpoint completo (tiene _checkpoint_type), usar engine_from_checkpoint
        if state.get("_checkpoint_type") == "simengine":
            return engine_from_checkpoint(state)

        # Fallback: generar mundo nuevo desde config (no debería ocurrir en operación normal)
        cfg = WorldConfig(
            seed=config.get("seed", 42),
            width=config.get("width", 200),
            height=config.get("height", 80),
            sea_level=config.get("sea_level", 0.2),
        )
        world = generate_world(cfg)
        return SimEngine(
            world, races, govs, seed=cfg.seed,
            n_civilized=config.get("n_civilized", 2),
            n_tribal=config.get("n_tribal", 2),
            n_beasts=config.get("n_beasts", 2),
            enabled_races=config.get("enabled_races"),
            kingdom_races=config.get("kingdom_races"),
        )

    def _calculate_participant_metrics(self, eng: SimEngine, user_id: int) -> dict:
        """Calcula métricas deterministas para un participante (CONTRACTS §17.4).

        Métricas válidas (solo las que existen en el modelo):
        - total_population: máximo total_population entre reinos del participante
        - wars_won: máximo wars_won entre reinos del participante
        - tech_level: máximo tech_level entre reinos del participante
        - influence_max: máximo sobre reinos de count(tile) donde influence_map(tile) == kingdom.id

        Orden: total_population desc, wars_won desc, tech_level desc, influence_max desc
        Desempates: tech_level desc, wars_won desc, total_population desc, user_id asc

        NOTA: gold NO existe en el modelo → excluida (F7-D-R1-D).
        """
        if not eng.kingdoms:
            return {
                "total_population": 0,
                "wars_won": 0,
                "tech_level": 0,
                "influence_max": 0,
            }

        # En desafíos, cada participante tiene UN reino (o el mejor si hay varios)
        # Para comparabilidad, usamos el mejor reino por cada métrica
        best_population = max(k.total_population for k in eng.kingdoms)
        best_wars_won = max(k.wars_won for k in eng.kingdoms)
        best_tech_level = max(k.tech_level for k in eng.kingdoms)

        # Calcular influence_max por reino usando influence_map
        influence_map = eng.influence_map()
        best_influence = 0
        for k in eng.kingdoms:
            influence = int((influence_map == k.id).sum())
            best_influence = max(best_influence, influence)

        return {
            "total_population": int(best_population),
            "wars_won": int(best_wars_won),
            "tech_level": int(best_tech_level),
            "influence_max": int(best_influence),
        }

    # ------------------------------------------------------------------
    # Disputas y resolución
    # ------------------------------------------------------------------

    def dispute_challenge(
        self,
        challenge_id: int,
        user_id: int,
        client_idempotency_key: str,
        request_hash: str | None = None,
        session=None,
    ) -> ChallengeResult:
        """Participante presenta disputa sobre su propio resultado."""
        own_session = session is None
        if own_session:
            session = self.session_factory()
        try:
            return self._dispute_challenge(
                session, challenge_id, user_id,
                client_idempotency_key, request_hash,
            )
        finally:
            if own_session:
                session.close()

    def _dispute_challenge(
        self,
        session,
        challenge_id: int,
        user_id: int,
        client_idempotency_key: str,
        request_hash: str | None,
    ) -> ChallengeResult:
        repo = ChallengeParticipantRepository(session)
        challenge_repo = ChallengeRepository(session)

        challenge = challenge_repo.get_challenge(challenge_id)
        if challenge is None:
            raise ChallengeNotFound(f"Desafío {challenge_id} no existe")

        # Ámbito de idempotencia §17.13: clave reutilizada con contenido
        # distinto → 409 ANTES del retorno temprano por estado.
        row = _idempotent_operation_row(
            session, challenge.id, user_id,
            client_idempotency_key, request_hash,
        )
        if row is not None:
            return ChallengeResult(
                challenge=challenge, is_retry=True, original_outcome=row.outcome
            )

        # Idempotencia: si ya está en el estado objetivo, devolver como reintento
        if challenge.state == "disputed":
            return ChallengeResult(challenge=challenge, is_retry=True)

        _require_state(challenge, {"completed"}, "disputar")

        participant = repo.get_participant(challenge_id, user_id)
        if participant is None:
            raise ParticipantNotFound(f"Usuario {user_id} no participante en desafío {challenge_id}")

        # Mark participant result as disputed if verified or confirmed
        if participant.result_status in ("verified", "confirmed"):
            participant.result_status = "disputed"
            participant.result_verified = 0  # not confirmed now

        # El estado del participante permanece 'finalizado' (CONTRACTS §17.2).
        # Solo el desafío pasa a 'disputed'; los resultados se congelan.

        challenge.state = "disputed"
        session.flush()

        challenge_repo.record_challenge_operation(
            organizer_user_id=user_id,
            client_idempotency_key=client_idempotency_key,
            request_hash=request_hash or "",
            operation="dispute",
            challenge_id=challenge.id,
            outcome="disputed",
        )

        session.commit()
        return ChallengeResult(challenge=challenge, is_retry=False)

    def resolve_challenge(
        self,
        challenge_id: int,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None = None,
        session=None,
        resolution: str = "confirm_all",
    ) -> ChallengeResult:
        """Resuelve disputa (organizador/admin): disputed → resolved."""
        own_session = session is None
        if own_session:
            session = self.session_factory()
        try:
            return self._resolve_challenge(
                session, challenge_id, organizer_user_id,
                client_idempotency_key, request_hash, resolution,
            )
        finally:
            if own_session:
                session.close()

    def _resolve_challenge(
        self,
        session,
        challenge_id: int,
        organizer_user_id: int,
        client_idempotency_key: str,
        request_hash: str | None,
        resolution: str = "confirm_all",
    ) -> ChallengeResult:
        repo = ChallengeParticipantRepository(session)
        challenge_repo = ChallengeRepository(session)

        challenge = challenge_repo.get_challenge(challenge_id)
        if challenge is None:
            raise ChallengeNotFound(f"Desafío {challenge_id} no existe")

        # Ámbito de idempotencia §17.13: clave reutilizada con contenido
        # distinto → 409 ANTES del retorno temprano por estado.
        _idempotent_operation_row(
            session, challenge.id, organizer_user_id,
            client_idempotency_key, request_hash,
        )

        # Idempotencia: si ya está en el estado objetivo, devolver como reintento
        if challenge.state == "resolved":
            return ChallengeResult(challenge=challenge, is_retry=True)

        _require_state(challenge, {"disputed"}, "resolver")

        # Verificar autorización: organizador o admin
        from worldsim.server.db import UserRow
        user = session.get(UserRow, organizer_user_id)
        if user is None:
            raise ChallengeError("Usuario organizador no existe")
        if challenge.organizer_user_id != organizer_user_id and not user.is_admin:
            raise ChallengeError("No autorizado para resolver disputa")

        # Update disputed participants based on resolution
        participants = repo.get_participants(challenge_id)
        for p in participants:
            if p.result_status == "disputed":
                if resolution == "confirm_all":
                    p.result_status = "confirmed"
                    p.result_verified = 1
                elif resolution == "invalidate_all":
                    p.result_status = "invalidated"
                    p.result_verified = 0
                else:
                    raise ChallengeError(f"Invalid resolution: {resolution}")

        challenge.state = "resolved"
        session.flush()

        # Los resultados de los participantes se mantienen (no se sobrescriben silenciosamente)
        # Una corrección crearía un nuevo registro que referencie al anterior (DEC-F7D-08/19)

        challenge_repo.record_challenge_operation(
            organizer_user_id=organizer_user_id,
            client_idempotency_key=client_idempotency_key,
            request_hash=request_hash or "",
            operation="resolve",
            challenge_id=challenge.id,
            outcome=resolution,
        )

        session.commit()
        return ChallengeResult(challenge=challenge, is_retry=False)

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------

    @staticmethod
    def _now() -> str:
        import time

        return time.strftime("%Y-%m-%d %H:%M:%S")

    # Métodos estáticos para compatibilidad con app.py
    @staticmethod
    def _canonical_challenge_request(params: 'ChallengeParams') -> bytes:
        """Representación canónica versionada hasheada en request_hash."""
        canonical = {
            "v": 1,
            "challenge_id": params.challenge_id,
            "organizer_user_id": params.organizer_user_id,
            "name": params.name,
            "config_json": params.config_json,
            "rules_version": params.rules_version,
            "duration_ticks": params.duration_ticks,
        }
        return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _compute_request_hash(params: 'ChallengeParams') -> str:
        return hashlib.sha256(ChallengeService._canonical_challenge_request(params)).hexdigest()
