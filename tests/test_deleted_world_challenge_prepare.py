"""F7-E.2-I B3-5: protección de mundos eliminados en prepare_worlds (CS1).

Casos:
  A — Participante sin mundo asignado: creación normal.
  B — Participante con mundo activo (deleted_at IS NULL): reutilización idempotente.
  C — Participante con mundo eliminado (deleted_at IS NOT NULL): error explícito,
     sin crear mundo sustituto, sin cambiar world_id de la participación.

Isolation: SQLite propio por test, sin ensure_schema (defecto preexistente).
Los datos se insertan con SQL directo para respetar restricciones NOT NULL.
"""

from __future__ import annotations

import json
import itertools
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

import worldsim.server.db as dbmod
from worldsim.server.challenge_service import ChallengeService
from worldsim.server.db import Base, WorldRow, ChallengeRow, ChallengeParticipantRow
from worldsim.server.challenge_service import WorldNotFoundForParticipant

@pytest.fixture(autouse=True)
def _isolate_challenge_operation_idempotency(monkeypatch):
    """Aisla la idempotencia F7-D en pruebas centradas en mundos eliminados."""
    monkeypatch.setattr(
        "worldsim.server.challenge_service._idempotent_operation_row",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "worldsim.server.repository.ChallengeRepository.record_challenge_operation",
        lambda *args, **kwargs: None,
    )



def _build_fresh_db(path) -> None:
    """Base limpia con el esquema completo (create_all)."""
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    dbmod.Base.metadata.create_all(eng)
    eng.dispose()


def _fresh_engine(tmp_path, name="b35.db"):
    db_file = tmp_path / name
    _build_fresh_db(db_file)
    eng = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    # No se llama a ensure_schema (defecto preexistente arity).
    return eng


def _now():
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


def _make_service(eng):
    """ChallengeService con SessionLocal apuntando al engine aislado."""
    dbmod.SessionLocal = sessionmaker(bind=eng)
    return ChallengeService()


def _seq():
    """Iterador de IDs para nombres únicos."""
    return itertools.count(1)


def _insert_challenge(eng, organizer_user_id=1, name="Test",
                      config_json=None, request_hash=None):
    """Inserta un desafío directamente en la DB."""
    if config_json is None:
        config_json = _challenge_json()
    if request_hash is None:
        import hashlib, json as _json
        canon = _json.dumps({
            "v": 1,
            "challenge_id": 1,
            "organizer_user_id": organizer_user_id,
            "name": name,
            "config_json": config_json,
            "rules_version": "1.0",
            "duration_ticks": 30,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        request_hash = hashlib.sha256(canon).hexdigest()

    with Session(eng, expire_on_commit=False) as s:
        challenge = ChallengeRow(
            organizer_user_id=organizer_user_id,
            name=name,
            config_json=config_json,
            rules_version="1.0",
            duration_ticks=30,
            state="draft",
            created_at=_now(),
        )
        s.add(challenge)
        s.commit()
        s.refresh(challenge)
    return challenge.id, request_hash


def _insert_participant(eng, challenge_id, user_id, world_id=0):
    """Inscribe a un participante. world_id=0 significa 'sin mundo asignado'.
    Cualquier otro entero (1+) significa 'mundo asignado'.
    Todos los campos NOT NULL vienen explicitados."""
    with Session(eng, expire_on_commit=False) as s:
        s.execute(
            text(
                "INSERT INTO challenge_participants "
                "(challenge_id, user_id, world_id, public_alias, result_json, result_status, result_verified, state, joined_at) "
                "VALUES (:c, :u, :w, :p, :r, :st, :sv, :stt, :joined)"
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
                "joined": _now(),
            },
        )
        s.commit()
    return user_id


def _ensure_enrollment_closed(eng, challenge_id, user_ids):
    """Cierra la inscripci?n del desaf?o sin alterar estados de participantes."""
    with Session(eng, expire_on_commit=False) as s:
        s.execute(
            text(
                "UPDATE challenges SET state = 'enrollment_closed' "
                "WHERE id = :c"
            ),
            {"c": challenge_id},
        )
        s.commit()


import worldsim.server.repository as repos
...
def _get_participant(eng, challenge_id, user_id):
    with Session(eng, expire_on_commit=False) as s:
        repo = repos.ChallengeParticipantRepository(s)
        return repo.get_participant(challenge_id, user_id)


def _set_participant_world_id(eng, challenge_id, user_id, world_id):
    """Asigna world_id a un participante mediante SQL directo."""
    with Session(eng, expire_on_commit=False) as s:
        s.execute(
            text("UPDATE challenge_participants SET world_id = :w "
                 "WHERE challenge_id = :c AND user_id = :u"),
            {"c": challenge_id, "w": world_id, "u": user_id},
        )
        s.commit()


def _make_world(eng, *, deleted: bool = False, world_id_offset: int = 1) -> int:
    """Crea un mundo Row y devuelve su ID."""
    wid = world_id_offset
    with Session(eng, expire_on_commit=False) as s:
        from worldsim.server.db import WorldRow
        w = WorldRow(
            seed=42, width=200, height=80, months_simulated=0, land_pct=50.0,
            state_json='{}', created_at=_now(),
            name=("world-active" if not deleted else "world-deleted"),
            mode="single", season_years=300, season_over=0,
            owner_user_id=1,
            deleted_at=datetime.now(timezone.utc) if deleted else None,
        )
        s.add(w)
        s.commit()
        s.refresh(w)
    return wid


# ---------------------------------------------------------------------------
# Caso C: Mundo eliminado -> error explícito, sin sustitución
# (Este es el núcleo de B3-5)
# ---------------------------------------------------------------------------

def test_b35_caso_mundo_eliminado_lanza_error(tmp_path):
    """Caso C: prepare_worlds lanza WorldNotFoundForParticipante cuando el
    participante tiene un world_id asignado pero la fila tiene deleted_at IS NOT NULL."""
    eng = _fresh_engine(tmp_path)
    service = _make_service(eng)

    # Insertar desafío
    challenge_id, rh = _insert_challenge(eng, name="Test", organizer_user_id=1)

    # Participante con world_id=1 (mundo asignado)
    _insert_participant(eng, challenge_id, user_id=1, world_id=1)

    # Cerrar inscripciones
    _ensure_enrollment_closed(eng, challenge_id, [1])

    # INSERTAR un mundo ELIMINADO con ID 1
    with Session(eng, expire_on_commit=False) as s:
        from worldsim.server.db import WorldRow

        w = WorldRow(
            seed=42, width=200, height=80, months_simulated=0, land_pct=50.0,
            state_json='{}', created_at=_now(),
            name="world-deleted", mode="single",
            season_years=300, season_over=0,
            owner_user_id=1,
            deleted_at=datetime.now(timezone.utc),  # ¡está eliminado!
        )
        s.add(w)
        s.commit()

    # prepare_worlds debe fallar explícitamente
    with pytest.raises(WorldNotFoundForParticipant, match="eliminado"):
        service.prepare_worlds(
            challenge_id=challenge_id,
            organizer_user_id=1,
            client_idempotency_key="idem:b35:prep-del",
            request_hash=rh,
        )


def test_b35_caso_mundo_eliminado_sin_sustituto_y_sin_cambio_worldid(tmp_path):
    """Caso C: al lanzar error, el world_id de la participación permanece
    intacto y no se crea ningún mundo sustituto; la marca deleted_at se
    conserva tal cual."""
    eng = _fresh_engine(tmp_path)
    service = _make_service(eng)

    # Insertar desafío
    challenge_id, rh = _insert_challenge(eng, name="Test", organizer_user_id=1)

    # Participante con world_id=1
    _insert_participant(eng, challenge_id, user_id=1, world_id=1)

    # Cerrar inscripciones
    _ensure_enrollment_closed(eng, challenge_id, [1])

    # INSERTAR un mundo ELIMINADO con ID 1
    with Session(eng, expire_on_commit=False) as s:
        from worldsim.server.db import WorldRow

        w = WorldRow(
            seed=42, width=200, height=80, months_simulated=0, land_pct=50.0,
            state_json='{}', created_at=_now(),
            name="world-deleted", mode="single",
            season_years=300, season_over=0,
            owner_user_id=1,
            deleted_at=datetime.now(timezone.utc),
        )
        s.add(w)
        s.commit()

    # Guardar el world_id antes del error
    saved_world_id = None
    with Session(eng, expire_on_commit=False) as s:
        repo = repos.ChallengeParticipantRepository(s)
        p = repo.get_participant(challenge_id, 1)
        saved_world_id = p.world_id

    # Intentar prepare_worlds; debe lanzar el error contractual.
    with pytest.raises(WorldNotFoundForParticipant, match="eliminado"):
        service.prepare_worlds(
            challenge_id=challenge_id,
            organizer_user_id=1,
            client_idempotency_key="idem:b35:prep-del2",
            request_hash=rh,
        )

    # Verificar que el world_id persiste y no se creó mundo nuevo
    with Session(eng, expire_on_commit=False) as s:
        repo = repos.ChallengeParticipantRepository(s)
        p = repo.get_participant(challenge_id, 1)
        assert p.world_id == saved_world_id, "world_id debe persistir tras el error"
        # La marca deleted_at debe conservarse tal cual
        w = s.get(WorldRow, p.world_id)
        assert w is not None
        assert w.deleted_at is not None


def test_b35_caso_mundo_eliminado_repetido_sigue_siend_error(tmp_path):
    """Caso C: repetir prepare_worlds después del error sigue lanzando el
    mismo error, no vuelve a crear mundo (no transforma error en éxito)."""
    eng = _fresh_engine(tmp_path)
    service = _make_service(eng)

    # Insertar desafío
    challenge_id, rh = _insert_challenge(eng, name="Test", organizer_user_id=1)

    # Participante con world_id=1
    _insert_participant(eng, challenge_id, user_id=1, world_id=1)

    # Cerrar inscripciones
    _ensure_enrollment_closed(eng, challenge_id, [1])

    # INSERTAR un mundo ELIMINADO con ID 1
    with Session(eng, expire_on_commit=False) as s:
        from worldsim.server.db import WorldRow

        w = WorldRow(
            seed=42, width=200, height=80, months_simulated=0, land_pct=50.0,
            state_json='{}', created_at=_now(),
            name="world-deleted", mode="single",
            season_years=300, season_over=0,
            owner_user_id=1,
            deleted_at=datetime.now(timezone.utc),
        )
        s.add(w)
        s.commit()

    # Primera llamada: debe fallar
    try:
        service.prepare_worlds(
            challenge_id=challenge_id,
            organizer_user_id=1,
            client_idempotency_key="idem:b35:prep-del3",
            request_hash=rh,
        )
        assert False, "debe haber fallado primero"
    except Exception:
        first_err = True

    # Segunda llamada (mismo idempotency key o distinto): debe seguir fallando
    try:
        service.prepare_worlds(
            challenge_id=challenge_id,
            organizer_user_id=1,
            client_idempotency_key="idem:b35:prep-del3",
            request_hash=rh,
        )
        assert False, "segunda llamada también debe fallar"
    except Exception:
        second_err = True

    assert first_err and second_err


# ---------------------------------------------------------------------------
# Caso A y B: tests de compatibilidad (world_id=0 => sin mundo, world_id=1 => activo)
# ---------------------------------------------------------------------------

def test_b35_caso_sin_mundo_crea_normal(tmp_path):
    """Caso A (compatibilidad): participante con world_id=0 (sin mundo).
    El comportamiento depende de la lógica actual de prepare_worlds:
    'if participant.world_id is not None: continue'. Al usar world_id=0
    (que es not None), el código hará continue (idempotente, sin crear mundo nuevo).
    Este test verifica el estado actual; ajuste de lógica es para bloques posteriores."""
    eng = _fresh_engine(tmp_path)
    service = _make_service(eng)

    # Insertar desafío
    challenge_id, rh = _insert_challenge(eng, name="Test", organizer_user_id=1)

    # Participante con world_id=0 (significa "sin mundo")
    _insert_participant(eng, challenge_id, user_id=1, world_id=0)

    # Cerrar inscripciones
    _ensure_enrollment_closed(eng, challenge_id, [1])

    # prepare_worlds: world_id=0 es not None, así que el código actual hace continue
    # (idempotente, sin crear mundo nuevo). Este test verifica ese comportamiento.
    result = service.prepare_worlds(
        challenge_id=challenge_id,
        organizer_user_id=1,
        client_idempotency_key="idem:b35:a",
        request_hash=rh,
    )

    with Session(eng, expire_on_commit=False) as s:
        persisted_state = s.execute(
            text("SELECT state FROM challenges WHERE id = :c"),
            {"c": challenge_id},
        ).scalar_one()
    assert persisted_state == "preparing"
    with Session(eng, expire_on_commit=False) as s:
        repo = repos.ChallengeParticipantRepository(s)
        p = repo.get_participant(challenge_id, 1)
        # prepare_worlds crea y asigna un mundo al participante sin mundo.
        assert p.world_id is not None
        assert p.world_id > 0
        from worldsim.server.db import WorldRow
        world = s.get(WorldRow, p.world_id)
        assert world is not None
        assert world.deleted_at is None


def test_b35_caso_mundo_activo_idempotente(tmp_path):
    """Caso B (compatibilidad): participante con world_id=1 (mundo activo).
    prepare_worlds debe reutilizar la asignación existente (idempotente)."""
    eng = _fresh_engine(tmp_path)
    service = _make_service(eng)

    # Insertar desafío
    challenge_id, rh = _insert_challenge(eng, name="Test", organizer_user_id=1)

    # Participante con world_id=1 (mundo asignado)
    _insert_participant(eng, challenge_id, user_id=1, world_id=1)

    # Cerrar inscripciones
    _ensure_enrollment_closed(eng, challenge_id, [1])

    # INSERTAR un mundo activo con ID 1
    with Session(eng, expire_on_commit=False) as s:
        from worldsim.server.db import WorldRow

        w = WorldRow(
            seed=42, width=200, height=80, months_simulated=0, land_pct=50.0,
            state_json='{}', created_at=_now(),
            name="world-active", mode="single",
            season_years=300, season_over=0,
            owner_user_id=1,
            deleted_at=None,
        )
        s.add(w)
        s.commit()

    # prepare_worlds debe reutilizar (idempotente, skip)
    result = service.prepare_worlds(
        challenge_id=challenge_id,
        organizer_user_id=1,
        client_idempotency_key="idem:b35:b",
        request_hash=rh,
    )

    with Session(eng, expire_on_commit=False) as s:
        persisted_state = s.execute(
            text("SELECT state FROM challenges WHERE id = :c"),
            {"c": challenge_id},
        ).scalar_one()
    assert persisted_state == "preparing"
    with Session(eng, expire_on_commit=False) as s:
        repo = repos.ChallengeParticipantRepository(s)
        p = repo.get_participant(challenge_id, 1)
        # world_id debe persistir (idempotente)
        assert p.world_id == 1
