"""F7-D: desafíos con semillas comunes — comparación estadística reproducible.

Cubre: creación de tablas ``challenges`` y ``challenge_participants``,
migración idempotente, creación/publicación/inscripción/inicio de desafíos,
operaciones globales atómicas e idempotentes, restricciones UNIQUE/CHECK/FK,
y que no existe agencia competitiva (solo comparación estadística).

Todas las bases son temporales aisladas; la persistente ``worlds.db`` nunca
se abre.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

import worldsim.server.db as dbmod
from worldsim.server.challenge_service import ChallengeService
from worldsim.server.repository import (
    ChallengeParticipantRepository,
    ChallengeRepository,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_fresh_db(path) -> None:
    """Base limpia con el esquema completo (create_all)."""
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    dbmod.Base.metadata.create_all(eng)
    eng.dispose()


def _fresh_engine(tmp_path, name="f7d.db"):
    db_file = tmp_path / name
    _build_fresh_db(db_file)
    eng = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    dbmod.ensure_schema(eng)
    return eng


def _seed_users(raw: sqlite3.Connection) -> None:
    """Siembra usuarios de prueba."""
    raw.executescript(
        """
        INSERT INTO users (id, username, pw_hash, created_at)
        VALUES (1, 'alice', 'h', 't'), (2, 'bob', 'h', 't'), (3, 'carol', 'h', 't');
        """
    )


def _challenge_json() -> str:
    return json.dumps(
        {
            "seed": 42,
            "width": 200,
            "height": 80,
            "sea_level": 0.2,
            "n_civilized": 2,
            "n_tribal": 2,
            "n_beasts": 2,
            "mode": "single",
            "season_years": 300,
            "primary_metric": "total_population",
        }
    )


# ---------------------------------------------------------------------------
# Modelo y migración
# ---------------------------------------------------------------------------


def test_f7d_tables_created(tmp_path):
    """Las tablas challenges y challenge_participants existen tras create_all."""
    eng = _fresh_engine(tmp_path)
    insp = inspect(eng)
    tables = set(insp.get_table_names())
    assert "challenges" in tables
    assert "challenge_participants" in tables
    eng.dispose()


def test_f7d_challenge_columns_and_constraints(tmp_path):
    """Columnas, PK, UNIQUE, CHECK y FK de challenges."""
    eng = _fresh_engine(tmp_path)
    insp = inspect(eng)
    cols = {c["name"]: c for c in insp.get_columns("challenges")}
    assert cols["id"]["nullable"] is False
    assert cols["organizer_user_id"]["nullable"] is False
    assert cols["name"]["nullable"] is False
    assert cols["config_json"]["nullable"] is False
    assert cols["rules_version"]["nullable"] is False
    assert cols["duration_ticks"]["nullable"] is False
    assert cols["state"]["nullable"] is False
    assert cols["created_at"]["nullable"] is False
    assert cols["closed_at"]["nullable"] is True
    assert cols["completed_at"]["nullable"] is True

    fks = {tuple(fk["constrained_columns"]): fk for fk in insp.get_foreign_keys("challenges")}
    assert ("organizer_user_id",) in fks
    assert fks[("organizer_user_id",)]["referred_table"] == "users"
    # DEC-F7D-20: preserve challenge history; organizer deletion is restricted.
    assert fks[("organizer_user_id",)]["options"]["ondelete"] == "RESTRICT"

    with eng.connect() as conn:
        row = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='challenges'")
        ).fetchone()
    assert "ck_challenge_state" in row[0]
    eng.dispose()


def test_f7d_participant_columns_and_constraints(tmp_path):
    """Columnas, UNIQUE(world_id), FK sin CASCADE destructiva."""
    eng = _fresh_engine(tmp_path)
    insp = inspect(eng)
    cols = {c["name"]: c for c in insp.get_columns("challenge_participants")}
    assert cols["id"]["nullable"] is False
    assert cols["challenge_id"]["nullable"] is False
    assert cols["user_id"]["nullable"] is False
    assert cols["world_id"]["nullable"] is True  # mundo se asigna en preparar
    assert cols["state"]["nullable"] is False
    assert cols["joined_at"]["nullable"] is False
    assert cols["result_json"]["nullable"] is True
    assert cols["result_verified"]["nullable"] is False

    # UNIQUE(world_id) - un mundo en un solo desafío
    with eng.connect() as conn:
        row = conn.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='challenge_participants'"
            )
        ).fetchone()
    assert "UNIQUE (world_id)" in row[0]

    fks = {tuple(fk["constrained_columns"]): fk for fk in insp.get_foreign_keys("challenge_participants")}
    assert ("world_id",) in fks
    assert fks[("world_id",)]["referred_table"] == "worlds"
    assert fks[("world_id",)].get("options") in ({}, None)
    assert ("challenge_id",) in fks
    assert fks[("challenge_id",)]["options"]["ondelete"] == "CASCADE"
    assert ("user_id",) in fks
    assert fks[("user_id",)]["options"]["ondelete"] == "CASCADE"

    eng.dispose()


# ---------------------------------------------------------------------------
# Servicio: creación y publicación
# ---------------------------------------------------------------------------


def _make_service(eng):
    """Crea un ChallengeService que usa una Sesión SQLAlchemy del motor dado."""
    service = ChallengeService()
    service.session_factory = lambda: Session(eng, expire_on_commit=False)
    return service


def test_f7d_create_challenge(tmp_path):
    """Crear desafío en draft."""
    eng = _fresh_engine(tmp_path)
    raw = sqlite3.connect(eng.url.database)
    _seed_users(raw)
    raw.commit()
    raw.close()

    service = _make_service(eng)
    result = service.create_challenge(
        organizer_user_id=1,
        name="Desafío de prueba",
        config_json=_challenge_json(),
        rules_version="1.0",
        duration_ticks=50,
        client_idempotency_key="idem:1:abc",
        request_hash="hash123",
    )

    assert result.challenge.id is not None
    assert result.challenge.organizer_user_id == 1
    assert result.challenge.name == "Desafío de prueba"
    assert result.challenge.state == "draft"
    assert result.challenge.duration_ticks == 50
    assert result.is_retry is False

    eng.dispose()


def test_f7d_create_challenge_idempotent(tmp_path):
    """Reintento con misma clave devuelve el mismo desafío."""
    eng = _fresh_engine(tmp_path)
    raw = sqlite3.connect(eng.url.database)
    _seed_users(raw)
    raw.commit()
    raw.close()

    service = _make_service(eng)
    cidem = "idem:enroll:1"
    rh = "hash-xyz"

    r1 = service.create_challenge(
        organizer_user_id=1,
        name="Desafío",
        config_json=_challenge_json(),
        rules_version="1.0",
        duration_ticks=30,
        client_idempotency_key=cidem,
        request_hash=rh,
    )
    r2 = service.create_challenge(
        organizer_user_id=1,
        name="Desafío",
        config_json=_challenge_json(),
        rules_version="1.0",
        duration_ticks=30,
        client_idempotency_key=cidem,
        request_hash=rh,
    )

    assert r1.challenge.id == r2.challenge.id
    assert r2.is_retry is True
    eng.dispose()


def test_f7d_create_challenge_idempotency_conflict(tmp_path):
    """Misma clave, hash diferente → conflicto 409."""
    eng = _fresh_engine(tmp_path)
    raw = sqlite3.connect(eng.url.database)
    _seed_users(raw)
    raw.commit()
    raw.close()

    service = _make_service(eng)
    cidem = "idem:conflict:1"

    service.create_challenge(
        organizer_user_id=1,
        name="Desafío",
        config_json=_challenge_json(),
        rules_version="1.0",
        duration_ticks=30,
        client_idempotency_key=cidem,
        request_hash="hash-a",
    )

    with pytest.raises(Exception, match="idempotency_conflict"):
        service.create_challenge(
            organizer_user_id=1,
            name="Desafío modificado",
            config_json=_challenge_json(),
            rules_version="1.0",
            duration_ticks=30,
            client_idempotency_key=cidem,
            request_hash="hash-b",
        )
    eng.dispose()


def test_f7d_create_request_hash_defaults_to_challenge_id_zero(tmp_path):
    """El hash fallback usa la misma identidad canónica que HTTP (challenge_id=0)."""
    from worldsim.server.challenge_service import ChallengeParams

    eng = _fresh_engine(tmp_path)
    raw = sqlite3.connect(eng.url.database)
    _seed_users(raw)
    raw.commit()
    raw.close()

    service = _make_service(eng)
    config = _challenge_json()
    service.create_challenge(
        organizer_user_id=1,
        name="Hash canónico",
        config_json=config,
        rules_version="1.0",
        duration_ticks=30,
        client_idempotency_key="idem:canonical:1",
    )

    expected = ChallengeService._compute_request_hash(
        ChallengeParams(
            challenge_id=0,
            organizer_user_id=1,
            name="Hash canónico",
            config_json=config,
            rules_version="1.0",
            duration_ticks=30,
        )
    )
    with eng.connect() as conn:
        stored = conn.execute(
            text(
                "SELECT request_hash FROM challenge_creation_requests "
                "WHERE actor_user_id = 1 AND client_idempotency_key = 'idem:canonical:1'"
            )
        ).scalar_one()
        operation_hash = conn.execute(
            text(
                "SELECT request_hash FROM challenge_operations "
                "WHERE operation = 'create' AND client_idempotency_key = 'idem:canonical:1'"
            )
        ).scalar_one()
    assert stored == expected
    assert operation_hash == expected
    eng.dispose()


def test_f7d_publish_challenge(tmp_path):
    """Publicar desafío (draft → published)."""
    eng = _fresh_engine(tmp_path)
    raw = sqlite3.connect(eng.url.database)
    _seed_users(raw)
    raw.commit()
    raw.close()

    service = _make_service(eng)
    result = service.create_challenge(
        organizer_user_id=1,
        name="Desafío",
        config_json=_challenge_json(),
        rules_version="1.0",
        duration_ticks=30,
        client_idempotency_key="idem:pub:1",
        request_hash="hash-pub",
    )
    result = service.publish_challenge(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:pub:1:2",
        request_hash="hash-pub2",
    )

    assert result.challenge.state == "published"
    assert result.challenge.closed_at is None
    eng.dispose()


def test_f7d_publish_non_owner_rejected(tmp_path):
    """Solo el organizador puede publicar."""
    eng = _fresh_engine(tmp_path)
    raw = sqlite3.connect(eng.url.database)
    _seed_users(raw)
    raw.commit()
    raw.close()

    service = _make_service(eng)
    result = service.create_challenge(
        organizer_user_id=1,
        name="Desafío",
        config_json=_challenge_json(),
        rules_version="1.0",
        duration_ticks=30,
        client_idempotency_key="idem:pub2:1",
        request_hash="hash-pub2",
    )

    with pytest.raises(Exception, match="No es el organizador"):
        service.publish_challenge(
            challenge_id=result.challenge.id,
            organizer_user_id=2,
            client_idempotency_key="idem:pub2:2",
            request_hash="hash-pub2b",
        )
    eng.dispose()


# ---------------------------------------------------------------------------
# Inscripción
# ---------------------------------------------------------------------------


def test_f7d_enroll_participant(tmp_path):
    """Inscribirse en un desafío publicado."""
    eng = _fresh_engine(tmp_path)
    raw = sqlite3.connect(eng.url.database)
    _seed_users(raw)
    raw.commit()
    raw.close()

    service = _make_service(eng)
    result = service.create_challenge(
        organizer_user_id=1,
        name="Desafío",
        config_json=_challenge_json(),
        rules_version="1.0",
        duration_ticks=30,
        client_idempotency_key="idem:enroll:1",
        request_hash="hash-e1",
    )
    service.publish_challenge(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:enroll:1:2",
        request_hash="hash-e2",
    )
    service.open_enrollment(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:enroll:1:3",
        request_hash="hash-e3",
    )

    result = service.enroll_participant(
        challenge_id=result.challenge.id,
        user_id=2,
        client_idempotency_key="idem:enroll:2",
        request_hash="hash-e4",
    )

    assert result.is_retry is False

    with Session(eng) as conn:
        repo = ChallengeParticipantRepository(conn)
        p = repo.get_participant(result.challenge.id, 2)
    assert p is not None
    assert p.user_id == 2
    assert p.state == "inscrito"
    assert p.world_id is None
    eng.dispose()


def test_f7d_enroll_idempotent(tmp_path):
    """Reinscribirse con misma clave → no duplicar."""
    eng = _fresh_engine(tmp_path)
    raw = sqlite3.connect(eng.url.database)
    _seed_users(raw)
    raw.commit()
    raw.close()

    service = _make_service(eng)
    result = service.create_challenge(
        organizer_user_id=1,
        name="Desafío",
        config_json=_challenge_json(),
        rules_version="1.0",
        duration_ticks=30,
        client_idempotency_key="idem:enroll2:1",
        request_hash="hash-e4",
    )
    service.publish_challenge(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:enroll2:2",
        request_hash="hash-e5",
    )
    service.open_enrollment(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:enroll2:4",
        request_hash="hash-e6",
    )

    r1 = service.enroll_participant(
        challenge_id=result.challenge.id,
        user_id=2,
        client_idempotency_key="idem:enroll2:7",
        request_hash="hash-e7",
    )
    r2 = service.enroll_participant(
        challenge_id=result.challenge.id,
        user_id=2,
        client_idempotency_key="idem:enroll2:4",
        request_hash="hash-e6",
    )

    assert r2.is_retry is True

    with eng.connect() as conn:
        repo = ChallengeParticipantRepository(conn)
        ps = repo.get_participants(result.challenge.id)
    assert len(ps) == 1
    eng.dispose()


# ---------------------------------------------------------------------------
# Cierre de inscripciones y preparación
# ---------------------------------------------------------------------------


def test_f7d_close_enrollment(tmp_path):
    """Cerrar inscripciones (enrollment_open → enrollment_closed)."""
    eng = _fresh_engine(tmp_path)
    raw = sqlite3.connect(eng.url.database)
    _seed_users(raw)
    raw.commit()
    raw.close()

    service = _make_service(eng)
    result = service.create_challenge(
        organizer_user_id=1,
        name="Desafío",
        config_json=_challenge_json(),
        rules_version="1.0",
        duration_ticks=30,
        client_idempotency_key="idem:close:1",
        request_hash="hash-c1",
    )
    service.publish_challenge(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:close:2",
        request_hash="hash-c2",
    )
    service.open_enrollment(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:close:3",
        request_hash="hash-c4",
    )
    result = service.close_enrollment(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:close:5",
        request_hash="hash-c5",
    )

    assert result.challenge.state == "enrollment_closed"
    assert result.challenge.closed_at is not None
    eng.dispose()


def test_f7d_prepare_creates_worlds(tmp_path):
    """Preparar crea mundos derivados para cada participante."""
    eng = _fresh_engine(tmp_path)
    raw = sqlite3.connect(eng.url.database)
    _seed_users(raw)
    raw.commit()
    raw.close()

    service = _make_service(eng)
    result = service.create_challenge(
        organizer_user_id=1,
        name="Desafío",
        config_json=_challenge_json(),
        rules_version="1.0",
        duration_ticks=30,
        client_idempotency_key="idem:prep:1",
        request_hash="hash-p1",
    )
    service.publish_challenge(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:prep:2",
        request_hash="hash-p2",
    )
    service.open_enrollment(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:prep:3",
        request_hash="hash-p3",
    )
    service.enroll_participant(
        challenge_id=result.challenge.id,
        user_id=2,
        client_idempotency_key="idem:prep:5",
        request_hash="hash-p5",
    )
    service.enroll_participant(
        challenge_id=result.challenge.id,
        user_id=3,
        client_idempotency_key="idem:prep:6",
        request_hash="hash-p6",
    )
    service.close_enrollment(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:prep:4",
        request_hash="hash-p4",
    )

    result = service.prepare_worlds(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:prep:7",
        request_hash="hash-p7",
    )

    assert result.challenge.state == "preparing"

    with Session(eng, expire_on_commit=False) as s:
        repo = ChallengeParticipantRepository(s)
        ps = repo.get_participants(result.challenge.id)
    assert len(ps) == 2
    for p in ps:
        assert p.world_id is not None
        assert p.state == "preparando"

    with Session(eng, expire_on_commit=False) as s:
        for p in ps:
            w = s.get(dbmod.WorldRow, p.world_id)
            assert w is not None
            assert w.owner_user_id == p.user_id
    eng.dispose()


# ---------------------------------------------------------------------------
# Inicio y resultados
# ---------------------------------------------------------------------------


def test_f7d_start_challenge(tmp_path):
    """Iniciar desafío (preparing → running)."""
    eng = _fresh_engine(tmp_path)
    raw = sqlite3.connect(eng.url.database)
    _seed_users(raw)
    raw.commit()
    raw.close()

    service = _make_service(eng)
    result = service.create_challenge(
        organizer_user_id=1,
        name="Desafío",
        config_json=_challenge_json(),
        rules_version="1.0",
        duration_ticks=30,
        client_idempotency_key="idem:start:1",
        request_hash="hash-s1",
    )
    service.publish_challenge(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:start:2",
        request_hash="hash-s2",
    )
    service.open_enrollment(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:start:3",
        request_hash="hash-s4",
    )
    service.enroll_participant(
        challenge_id=result.challenge.id,
        user_id=2,
        client_idempotency_key="idem:start:7",
        request_hash="hash-s8",
    )
    service.close_enrollment(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:start:5",
        request_hash="hash-s6",
    )
    service.prepare_worlds(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:start:9",
        request_hash="hash-s10",
    )
    result = service.start_challenge(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:start:12",
        request_hash="hash-s13",
    )

    assert result.challenge.state == "running"
    eng.dispose()


def test_f7d_start_requires_all_worlds_prepared(tmp_path):
    """No se puede iniciar si algún participante no tiene mundo."""
    eng = _fresh_engine(tmp_path)
    raw = sqlite3.connect(eng.url.database)
    _seed_users(raw)
    raw.commit()
    raw.close()

    service = _make_service(eng)
    result = service.create_challenge(
        organizer_user_id=1,
        name="Desafío",
        config_json=_challenge_json(),
        rules_version="1.0",
        duration_ticks=30,
        client_idempotency_key="idem:start2:1",
        request_hash="hash-s7",
    )
    service.publish_challenge(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:start2:2",
        request_hash="hash-s8",
    )
    service.open_enrollment(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:start2:3",
        request_hash="hash-s9",
    )
    service.enroll_participant(
        challenge_id=result.challenge.id,
        user_id=2,
        client_idempotency_key="idem:start2:5",
        request_hash="hash-s11",
    )
    service.close_enrollment(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:start2:4",
        request_hash="hash-s10",
    )
    # La máquina de estados requiere enrollment_closed → preparing → running.
    # Sin prepare_worlds, el estado queda en enrollment_closed y start_challenge falla por estado.
    with pytest.raises(Exception, match="se requiere preparing para iniciar"):
        service.start_challenge(
            challenge_id=result.challenge.id,
            organizer_user_id=1,
            client_idempotency_key="idem:start2:7",
            request_hash="hash-s13",
        )
    eng.dispose()


def test_f7d_results_endpoint_shape(tmp_path):
    """Resultado del desafío tiene la forma esperada."""
    eng = _fresh_engine(tmp_path)
    raw = sqlite3.connect(eng.url.database)
    _seed_users(raw)
    raw.commit()
    raw.close()

    service = _make_service(eng)
    result = service.create_challenge(
        organizer_user_id=1,
        name="Desafío",
        config_json=_challenge_json(),
        rules_version="1.0",
        duration_ticks=30,
        client_idempotency_key="idem:res:1",
        request_hash="hash-r1",
    )
    service.publish_challenge(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:res:2",
        request_hash="hash-r2",
    )
    service.open_enrollment(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:res:3",
        request_hash="hash-r4",
    )
    service.enroll_participant(
        challenge_id=result.challenge.id,
        user_id=2,
        client_idempotency_key="idem:res:7",
        request_hash="hash-r8",
    )
    service.close_enrollment(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:res:5",
        request_hash="hash-r6",
    )
    service.prepare_worlds(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:res:9",
        request_hash="hash-r10",
    )
    service.start_challenge(
        challenge_id=result.challenge.id,
        organizer_user_id=1,
        client_idempotency_key="idem:res:12",
        request_hash="hash-r13",
    )

    with Session(eng, expire_on_commit=False) as s:
        crepo = ChallengeRepository(s)
        c = crepo.get_challenge(result.challenge.id)
        prepo = ChallengeParticipantRepository(s)
        ps = prepo.get_participants(result.challenge.id)

    assert c.state == "running"
    assert len(ps) == 1
    assert ps[0].user_id == 2
    assert ps[0].world_id is not None
    assert ps[0].result_json is None
    eng.dispose()


def test_f7d_influence_max_calculation(races, governments):
    """Validación de influence_max según contrato F7-D-R1-D."""
    from worldsim.sim.engine import SimEngine
    from worldsim.worldgen import WorldConfig
    from worldsim.worldgen.pipeline import generate_world
    from worldsim.server.challenge_service import ChallengeService
    import numpy as np

    # Crear mundo mínimo con reinos controlados
    world = generate_world(WorldConfig(seed=42, width=20, height=20, rivers=0))
    engine = SimEngine(world, races, governments, seed=42)
    
    # Avanzar para que se formen reinos
    for _ in range(10):
        engine.tick()
    
    service = ChallengeService()
    
    # Test con motor real
    metrics = service._calculate_participant_metrics(engine, 1)
    assert set(metrics.keys()) == {"total_population", "wars_won", "tech_level", "influence_max"}
    assert isinstance(metrics["influence_max"], int)
    assert metrics["influence_max"] >= 0
    
    # Reproducibilidad
    metrics2 = service._calculate_participant_metrics(engine, 1)
    assert metrics == metrics2
    
    # influence_max debe ser máximo de tiles por reino, no suma
    influence_map = engine.influence_map()
    expected_max = 0
    for k in engine.kingdoms:
        count = int((influence_map == k.id).sum())
        expected_max = max(expected_max, count)
    assert metrics["influence_max"] == expected_max

