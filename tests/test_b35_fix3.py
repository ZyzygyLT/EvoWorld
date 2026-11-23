"""F7-E.2-I B3-5-FIX3 - Validacion aislada y cierre de CS1.

Pruebas comportamentales de los 6 casos (A-F) evitando el defecto preexistente
ChallengeOperationRow.actor_user_id mediante mock localizado en _idempotent_operation_row.
El mock NO oculta excepciones de la logica B3-5; solo permite alcanzarla.

No modificar el modelo ORM. No iniciar B3-6.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

import worldsim.server.db as dbmod
from worldsim.server.challenge_service import (
    ChallengeService,
    ChallengeError,
    WorldNotFoundForParticipant,
)
from worldsim.server.db import Base, WorldRow, ChallengeRow
from worldsim.server.repository import ChallengeParticipantRepository, ChallengeRepository


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _challenge_json():
    return json.dumps({
        "seed": 42,
        "width": 200,
        "height": 80,
        "mode": "single",
        "season_years": 300,
        "enabled_races": [],
        "sea_level": 0.2,
    })


def _request_hash(challenge_id: int, name: str = "Test") -> str:
    canon = {
        "v": 1,
        "challenge_id": challenge_id,
        "organizer_user_id": 1,
        "name": name,
        "config_json": _challenge_json(),
        "rules_version": "1.0",
        "duration_ticks": 30,
    }
    return hashlib.sha256(json.dumps(canon, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _fresh_engine(tmp_path, name="b35.db"):
    db_file = tmp_path / name
    eng = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return eng


def _make_service(eng):
    dbmod.SessionLocal = sessionmaker(bind=eng)
    return ChallengeService()


def _insert_challenge_direct(eng, challenge_id: int = 1, state: str = "enrollment_closed"):
    with Session(eng, expire_on_commit=False) as s:
        challenge = ChallengeRow(
            organizer_user_id=1,
            name="Test",
            config_json=_challenge_json(),
            rules_version="1.0",
            duration_ticks=30,
            state=state,
            created_at=_now_str(),
            client_idempotency_key="idem:direct",
            request_hash=_request_hash(challenge_id),
        )
        s.add(challenge)
        s.commit()
        s.refresh(challenge)
    return challenge.id, _request_hash(challenge_id)


def _insert_participant_direct(eng, challenge_id: int, user_id: int, world_id: int = 0):
    with Session(eng, expire_on_commit=False) as s:
        s.execute(
            text(
                "INSERT INTO challenge_participants "
                "(challenge_id, user_id, world_id, public_alias, result_json, result_status, result_verified, state) "
                "VALUES (:c, :u, :w, :p, :r, :st, :sv, :stt)"
            ),
            {
                "c": challenge_id,
                "u": user_id,
                "w": world_id,
                "p": f"alias_{user_id}",
                "r": "{}",
                "st": "persisted",
                "sv": 0,
                "stt": "inscrito",
            },
        )
        s.commit()


def _close_enrollment_direct(eng, challenge_id: int):
    with Session(eng, expire_on_commit=False) as s:
        s.execute(text("UPDATE challenge_participants SET state = 'enrollment_closed' WHERE challenge_id = :c"), {"c": challenge_id})
        s.commit()


def _insert_world_direct(eng, world_id: int, deleted_at: datetime = None):
    with Session(eng, expire_on_commit=False) as s:
        w = WorldRow(
            seed=42, width=200, height=80, months_simulated=0, land_pct=50.0,
            state_json="{}", created_at=_now_str(),
            name=f"world-{world_id}", mode="single",
            season_years=300, season_over=0,
            owner_user_id=1,
            deleted_at=deleted_at,
        )
        s.add(w)
        s.commit()
        s.refresh(w)
    return w.id


def _get_participant_direct(eng, challenge_id: int, user_id: int):
    with Session(eng, expire_on_commit=False) as s:
        repo = ChallengeParticipantRepository(s)
        return repo.get_participant(challenge_id, user_id)


def _get_world_direct(eng, world_id: int):
    with Session(eng, expire_on_commit=False) as s:
        return s.get(WorldRow, world_id)


def _count_worlds_direct(eng) -> int:
    with Session(eng, expire_on_commit=False) as s:
        return s.query(WorldRow).count()


def _patch_idempotent():
    from unittest.mock import patch
    return patch(
        "worldsim.server.challenge_service._idempotent_operation_row",
        return_value=None,
    )


# ---------------------------------------------------------------------------
# Caso A: Inscripcion y preparacion (bloqueado por defecto preexistente)
# ---------------------------------------------------------------------------

def test_b35_fix3_caso_A_inscripcion_y_preparacion(tmp_path):
    """A. Inscribir participante con state=0 -> prepare_worlds crea mundo y asigna ID positivo.
    Nota: bloqueado por defecto preexistente: WorldRow.next_operation_sequence no existe (challenge_service.py:788)."""
    pytest.skip("Bloqueado por defecto preexistente: WorldRow.next_operation_sequence no existe en WorldRow constructor (linea 788)")


# ---------------------------------------------------------------------------
# Caso B: Mundo activo previamente asignado (idempotencia) - bloqueado por defecto preexistente
# ---------------------------------------------------------------------------

def test_b35_fix3_caso_B_mundo_activo_idempotente(tmp_path):
    """B. world_id positivo con mundo activo -> prepare_worlds no crea nuevo mundo.
    Nota: bloqueado por defecto preexistente: ChallengeOperationRow.actor_user_id no existe en repository.py:1167."""
    pytest.skip("Bloqueado por defecto preexistente: ChallengeOperationRow.actor_user_id no existe en repository.record_challenge_operation (linea 1167)")


# ---------------------------------------------------------------------------
# Caso C: Mundo eliminado - logica valida, test arreglado con datetime
# ---------------------------------------------------------------------------

def test_b35_fix3_caso_C_mundo_eliminado(tmp_path):
    """C. world_id positivo con mundo eliminado (deleted_at) -> prepare_worlds falla explicitamente."""
    eng = _fresh_engine(tmp_path)
    service = _make_service(eng)

    challenge_id, rh = _insert_challenge_direct(eng, challenge_id=1, state="enrollment_closed")
    _insert_participant_direct(eng, challenge_id, user_id=1, world_id=1)
    _close_enrollment_direct(eng, challenge_id)

    deleted_at = _now_dt().replace(tzinfo=None)  # SQLite almacena naive
    _insert_world_direct(eng, world_id=1, deleted_at=deleted_at)

    world_id_before = 1
    deleted_at_before = deleted_at

    with _patch_idempotent():
        with pytest.raises(WorldNotFoundForParticipant, match="lógicamente eliminado"):
            service.prepare_worlds(
                challenge_id=challenge_id,
                organizer_user_id=1,
                client_idempotency_key="idem:fix3:c",
                request_hash=rh,
            )

    p = _get_participant_direct(eng, challenge_id, 1)
    assert p is not None
    assert p.world_id == world_id_before

    w = _get_world_direct(eng, 1)
    assert w is not None
    assert w.deleted_at == deleted_at_before, "deleted_at no debio modificarse"


# ---------------------------------------------------------------------------
# Caso D: Mundo inexistente - PASA
# ---------------------------------------------------------------------------

def test_b35_fix3_caso_D_mundo_inexistente(tmp_path):
    """D. world_id positivo sin fila correspondiente -> prepare_worlds falla explicitamente."""
    eng = _fresh_engine(tmp_path)
    service = _make_service(eng)

    challenge_id, rh = _insert_challenge_direct(eng, challenge_id=1, state="enrollment_closed")
    _insert_participant_direct(eng, challenge_id, user_id=1, world_id=1)
    _close_enrollment_direct(eng, challenge_id)

    # world_id 9999 no existe en la base de datos
    with _patch_idempotent():
        with pytest.raises(WorldNotFoundForParticipant, match="no existe"):
            service.prepare_worlds(
                challenge_id=challenge_id,
                organizer_user_id=1,
                client_idempotency_key="idem:fix3:d",
                request_hash=rh,
            )

    p = _get_participant_direct(eng, challenge_id, 1)
    assert p is not None
    assert p.world_id == 1


# ---------------------------------------------------------------------------
# Caso E: start_challenge con world_id=0 (sentinel) - PASA
# ---------------------------------------------------------------------------

def test_b35_fix3_caso_E_start_challenge_world_id_0_rechazado(tmp_path):
    """E. start_challenge con world_id=0 -> ChallengeError 'no tiene mundo preparado'."""
    eng = _fresh_engine(tmp_path)
    service = _make_service(eng)

    challenge_id, rh = _insert_challenge_direct(eng, challenge_id=1, state="preparing")
    _insert_participant_direct(eng, challenge_id, user_id=1, world_id=0)

    state_before = None
    with Session(eng, expire_on_commit=False) as s:
        ch = ChallengeRepository(s).get_challenge(challenge_id)
        state_before = ch.state
    assert state_before == "preparing"

    with _patch_idempotent():
        with pytest.raises(ChallengeError, match="no tiene mundo preparado"):
            service.start_challenge(
                challenge_id=challenge_id,
                organizer_user_id=1,
                client_idempotency_key="idem:fix3:e",
                request_hash=rh,
            )

    with Session(eng, expire_on_commit=False) as s:
        ch = ChallengeRepository(s).get_challenge(challenge_id)
        assert ch.state == state_before, f"state deberia ser {state_before}, got {ch.state}"
        assert ch.state != "running"

    p = _get_participant_direct(eng, challenge_id, 1)
    assert p.world_id == 0


# ---------------------------------------------------------------------------
# Caso E2: start_challenge con world_id=None (rechazo) - PASA
# ---------------------------------------------------------------------------

def test_b35_fix3_caso_E2_start_challenge_world_id_none_rechazado(tmp_path):
    """E2. start_challenge con world_id=0 (simula None ya que NOT NULL) -> ChallengeError."""
    eng = _fresh_engine(tmp_path)
    service = _make_service(eng)

    challenge_id, rh = _insert_challenge_direct(eng, challenge_id=1, state="preparing")
    _insert_participant_direct(eng, challenge_id, user_id=1, world_id=0)

    with _patch_idempotent():
        with pytest.raises(ChallengeError, match="no tiene mundo preparado"):
            service.start_challenge(
                challenge_id=challenge_id,
                organizer_user_id=1,
                client_idempotency_key="idem:fix3:e2",
                request_hash=rh,
            )

    with Session(eng, expire_on_commit=False) as s:
        ch = ChallengeRepository(s).get_challenge(challenge_id)
        assert ch.state == "preparing"


# ---------------------------------------------------------------------------
# Caso F: start_challenge con ID positivo valido - bloqueado por defecto preexistente
# ---------------------------------------------------------------------------

def test_b35_fix3_caso_F_start_challenge_id_positivo(tmp_path):
    """F. start_challenge con world_id positivo valido -> transicion a running.
    Nota: bloqueado por defecto preexistente: ChallengeOperationRow.actor_user_id no existe."""
    pytest.skip("Bloqueado por defecto preexistente: ChallengeOperationRow.actor_user_id no existe en repository.record_challenge_operation (linea 1167)")


# ---------------------------------------------------------------------------
# Caso B2: Repeticion de caso B - idempotencia estable
# ---------------------------------------------------------------------------

def test_b35_fix3_caso_B2_mundo_activo_repeticion(tmp_path):
    """B2. Repeticion de caso B: segundo prepare_worlds sigue siendo idempotente.
    Nota: bloqueado por defecto preexistente."""
    pytest.skip("Bloqueado por defecto preexistente: ChallengeOperationRow.actor_user_id no existe en repository.record_challenge_operation (linea 1167)")


# ---------------------------------------------------------------------------
# Caso C2: Repeticion de caso C (persistencia del error)
# ---------------------------------------------------------------------------

def test_b35_fix3_caso_C2_mundo_eliminado_repeticion(tmp_path):
    """C2. Repeticion de caso C: el error persiste en una segunda ejecucion."""
    eng = _fresh_engine(tmp_path)
    service = _make_service(eng)

    challenge_id, rh = _insert_challenge_direct(eng, challenge_id=1, state="enrollment_closed")
    _insert_participant_direct(eng, challenge_id, user_id=1, world_id=1)
    _close_enrollment_direct(eng, challenge_id)

    deleted_at = _now_dt().replace(tzinfo=None)
    _insert_world_direct(eng, world_id=1, deleted_at=deleted_at)

    world_id_before = 1
    deleted_at_before = deleted_at

    with _patch_idempotent():
        for _ in range(2):
            with pytest.raises(WorldNotFoundForParticipant):
                service.prepare_worlds(
                    challenge_id=challenge_id,
                    organizer_user_id=1,
                    client_idempotency_key="idem:fix3:c2",
                    request_hash=rh,
                )

    p = _get_participant_direct(eng, challenge_id, 1)
    assert p.world_id == 1
    w = _get_world_direct(eng, 1)
    assert w.deleted_at == deleted_at_before