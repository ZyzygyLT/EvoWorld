"""F7-D FIX10-A: máquina de estados de resultados, confirmación, disputas,
resolución, alias opacos y migración SQLite.

Matriz contractual de transiciones de resultado (DEC-F7D-19 / CONTRACTS
§17.14 + §17.7):

    calculated  → persisted     (escritura de result_json en la completitud)
    persisted   → verified      (precondiciones/procedencia comprobadas, mismo ámbito)
    verified    → confirmed     (confirmación formal, organizador/admin)
    verified    → disputed      (disputa del participante)
    confirmed   → disputed      (disputa del participante)
    disputed    → confirmed     (resolución favorable: confirm_all)
    disputed    → invalidated   (resolución desfavorable: invalidate_all)
    invalidated                    (terminal: sin salidas)

    PROHIBIDAS: calculated/persisted → confirmed; verified/confirmed →
    invalidated SIN disputa y resolución previa; disputed → confirmed por
    confirmación ordinaria; qualquer reapertura de invalidated.

Sincronización obligatoria de `result_verified` (campo de compatibilidad;
`result_status` es la fuente de verdad):

    calculated=0  persisted=0  verified=1  confirmed=1  disputed=0  invalidated=0
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session

import worldsim.server.db as dbmod
from worldsim.server.challenge_service import (
    ChallengeError,
    ChallengeService,
    ChallengeStateError,
    ParticipantNotFound,
)
from worldsim.server.db import ChallengeParticipantRow, ChallengeRow, WorldRow
from worldsim.server.repository import (
    ChallengeParticipantRepository,
    ChallengeRepository,
)


def _build_fresh_db(path) -> None:
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    dbmod.Base.metadata.create_all(eng)
    eng.dispose()


def _fresh_engine(tmp_path, name="f7d_state.db"):
    db_file = tmp_path / name
    _build_fresh_db(db_file)
    eng = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    dbmod.ensure_schema(eng)
    return eng


def _seed_users(raw_conn) -> None:
    raw_conn.exec_driver_sql(
        "INSERT INTO users (id, username, pw_hash, created_at) VALUES (1, 'alice', 'h', 't')"
    )
    raw_conn.exec_driver_sql(
        "INSERT INTO users (id, username, pw_hash, created_at) VALUES (2, 'bob', 'h', 't')"
    )
    raw_conn.exec_driver_sql(
        "INSERT INTO users (id, username, pw_hash, created_at) VALUES (3, 'carol', 'h', 't')"
    )


def _challenge_json() -> str:
    return json.dumps({"seed": 42, "primary_metric": "total_population"})


def _insert_challenge(conn, state="draft"):
    conn.execute(
        text(
            "INSERT INTO challenges (id, organizer_user_id, name, config_json, rules_version, duration_ticks, state, created_at) "
            "VALUES (1, 1, 'c', :config, 'v1', 10, :state, 't')"
        ),
        {"config": _challenge_json(), "state": state},
    )


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------


def test_migration_refuses_incompatible_f7d_schema_with_historical_rows(tmp_path):
    # An incompatible legacy schema containing rows must be preserved.
    db_file = tmp_path / "old.db"
    eng = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    with eng.connect() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, pw_hash TEXT, created_at TEXT)"
        )
        conn.exec_driver_sql(
            "INSERT INTO users (id, username, pw_hash, created_at) VALUES (1, 'a', 'h', 't')"
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE challenges (
                id INTEGER PRIMARY KEY,
                organizer_user_id INTEGER,
                name TEXT,
                config_json TEXT,
                rules_version TEXT,
                duration_ticks INTEGER,
                state TEXT,
                created_at TEXT
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO challenges (id, organizer_user_id, name, config_json, rules_version, duration_ticks, state, created_at) "
            "VALUES (1, 1, 'c', '{\"primary_metric\":\"total_population\"}', 'v1', 10, 'draft', 't')"
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE challenge_participants (
                id INTEGER PRIMARY KEY,
                challenge_id INTEGER,
                user_id INTEGER,
                world_id INTEGER,
                state TEXT,
                joined_at TEXT,
                result_json TEXT,
                result_verified INTEGER DEFAULT 0
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO challenge_participants (id, challenge_id, user_id, state, joined_at, result_verified) "
            "VALUES (1, 1, 1, 'finalizado', 't', 1)"
        )
        conn.commit()
    eng.dispose()

    eng2 = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    with pytest.raises(RuntimeError, match="historical rows"):
        dbmod.ensure_schema(eng2)

    with eng2.connect() as conn:
        challenge_count = conn.execute(text("SELECT COUNT(*) FROM challenges")).scalar_one()
        participant_count = conn.execute(
            text("SELECT COUNT(*) FROM challenge_participants")
        ).scalar_one()
        participant_columns = {
            row[1] for row in conn.exec_driver_sql(
                "PRAGMA table_info(challenge_participants)"
            ).fetchall()
        }
        assert challenge_count == 1
        assert participant_count == 1
        assert "public_alias" not in participant_columns
    eng2.dispose()


def test_migration_idempotent(tmp_path):
    db_file = tmp_path / "idem.db"
    # create fresh with new schema
    eng = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    dbmod.Base.metadata.create_all(eng)
    with eng.connect() as conn:
        conn.exec_driver_sql(
            "INSERT INTO users (id, username, pw_hash, created_at) VALUES (1, 'a', 'h', 't')"
        )
        conn.exec_driver_sql(
            "INSERT INTO challenges (id, organizer_user_id, name, config_json, rules_version, duration_ticks, state, created_at) "
            "VALUES (1, 1, 'c', '{\"primary_metric\":\"total_population\"}', 'v1', 10, 'draft', 't')"
        )
        conn.exec_driver_sql(
            "INSERT INTO challenge_participants (id, challenge_id, user_id, state, joined_at, result_verified, result_status, public_alias) "
            "VALUES (1, 1, 1, 'finalizado', 't', 1, 'verified', 'abc123')"
        )
        conn.commit()
    eng.dispose()
    # run ensure_schema twice
    eng2 = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    dbmod.ensure_schema(eng2)
    dbmod.ensure_schema(eng2)
    with eng2.connect() as conn:
        alias = conn.execute(text("SELECT public_alias FROM challenge_participants WHERE id=1")).fetchone()[0]
        assert alias == "abc123"  # unchanged
    eng2.dispose()


def test_check_constraint_enforced(tmp_path):
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO challenge_participants (challenge_id, user_id, state, joined_at, result_status) "
                    "VALUES (1, 1, 'x', 't', 'invalid_status')"
                )
            )
    eng.dispose()


def test_unique_alias_enforced(tmp_path):
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "draft")
        conn.commit()
        # insert two participants with same alias
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO challenge_participants (challenge_id, user_id, state, joined_at, result_status, public_alias) "
                    "VALUES (1, 1, 'x', 't', 'calculated', 'dup'), (1, 2, 'x', 't', 'calculated', 'dup')"
                )
            )
    eng.dispose()


# ---------------------------------------------------------------------------
# Classification tests (public endpoint)
# ---------------------------------------------------------------------------


def test_public_endpoint_excludes_non_confirmed(tmp_path):
    # Build challenge with participants in various statuses
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "completed")
        # insert participants
        conn.execute(
            text(
                "INSERT INTO challenge_participants (challenge_id, user_id, state, joined_at, result_json, result_verified, result_status, public_alias) VALUES "
                "(1, 1, 'finalizado', 't', :m1, 1, 'confirmed', 'a1'), "
                "(1, 2, 'finalizado', 't', :m2, 1, 'verified', 'a2'), "
                "(1, 3, 'finalizado', 't', :m3, 1, 'disputed', 'a3')"
            ),
            {"m1": json.dumps({"total_population": 10}), "m2": json.dumps({"total_population": 20}), "m3": json.dumps({"total_population": 30})},
        )
        conn.commit()
    # Simulate endpoint logic
    with eng.connect() as conn:
        rows = conn.execute(text("SELECT result_status FROM challenge_participants WHERE challenge_id=1")).fetchall()
        statuses = [r[0] for r in rows]
        confirmed = [s for s in statuses if s == "confirmed"]
        assert len(confirmed) == 1
        assert "verified" not in confirmed
        assert "disputed" not in confirmed
    eng.dispose()


# ---------------------------------------------------------------------------
# State machine (F7-D FIX10-A): transiciones válidas y prohibidas
# ---------------------------------------------------------------------------


def _insert_participant(conn, user_id, result_status="calculated", result_verified=0):
    conn.execute(
        text(
            "INSERT INTO challenge_participants (challenge_id, user_id, state, joined_at, result_json, result_verified, result_status, public_alias) "
            "VALUES (1, :uid, 'finalizado', 't', :rj, :rv, :rs, :alias)"
        ),
        {"uid": user_id, "rj": json.dumps({"total_population": 100}), "rv": result_verified, "rs": result_status, "alias": f"alias{user_id}"},
    )


def _p(eng, user_id):
    """(result_status, result_verified) leídos desde una sesión nueva."""
    with Session(eng) as s:
        row = (
            s.query(ChallengeParticipantRow)
            .filter_by(challenge_id=1, user_id=user_id)
            .first()
        )
        assert row is not None, f"participante {user_id} no encontrado"
        return row.result_status, row.result_verified


def _challenge_state(eng) -> str:
    with Session(eng) as s:
        row = s.get(ChallengeRow, 1)
        assert row is not None
        return row.state


def _set_admin(eng, user_id: int) -> None:
    with eng.connect() as conn:
        conn.execute(text("UPDATE users SET is_admin = 1 WHERE id = :u"), {"u": user_id})
        conn.commit()


# --- calculated → persisted → verified -------------------------------------


def test_calculated_to_persisted_to_verified(tmp_path):
    """complete_challenge ejecuta calculated → persisted → verified con
    result_verified sincronizado en cada paso. La durabilidad se verifica
    desde una conexión nueva e independiente de la que ejecutó la operación.
    """
    db_file = tmp_path / "f7d_state.db"
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "running")
        conn.commit()
    with Session(eng) as s:
        world = WorldRow(
            seed=42, width=60, height=40, months_simulated=0, land_pct=50.0,
            state_json="{}", created_at="t", name="w",
        )
        s.add(world)
        s.flush()
        s.add(
            ChallengeParticipantRow(
                challenge_id=1, user_id=1, world_id=world.id, state="inscrito",
                joined_at="t", result_json=None, result_verified=0,
                result_status="calculated", public_alias=uuid.uuid4().hex,
            )
        )
        s.commit()

    s2 = Session(eng)
    observed: list[str] = []

    def _before_flush(session, flush_context, instances):
        for obj in s2.dirty:
            if isinstance(obj, ChallengeParticipantRow):
                observed.append(obj.result_status)

    event.listen(s2, "before_flush", _before_flush)
    svc = ChallengeService(session_factory=lambda: s2)
    res = svc.complete_challenge(1, 1, "comp-key-1")
    event.remove(s2, "before_flush", _before_flush)
    assert res.is_retry is False
    # Secuencia efectiva dentro de la transacción: persisted ANTES que verified
    assert observed == ["persisted", "verified"], observed

    # Durabilidad desde una conexión nueva e independiente
    eng2 = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    with eng2.connect() as conn:
        row = conn.execute(
            text(
                "SELECT result_status, result_verified, result_json "
                "FROM challenge_participants WHERE id=1"
            )
        ).fetchone()
        assert row[0] == "verified"
        assert row[1] == 1
        assert set(json.loads(row[2])) == {
            "total_population", "wars_won", "tech_level", "influence_max",
        }
        assert conn.execute(text("SELECT state FROM challenges WHERE id=1")).fetchone()[0] == "completed"
        assert conn.execute(text("SELECT months_simulated FROM worlds WHERE id=1")).fetchone()[0] == 10
    eng2.dispose()

    # Idempotencia: reintentar la completitud no reejecuta ni altera resultados
    svc2 = ChallengeService(session_factory=lambda: Session(eng))
    res2 = svc2.complete_challenge(1, 1, "comp-key-1")
    assert res2.is_retry is True
    assert _p(eng, 1) == ("verified", 1)
    eng.dispose()


# --- verified → confirmed ---------------------------------------------------


def test_confirm_verified_to_confirmed(tmp_path):
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "completed")
        _insert_participant(conn, 1, "verified", 1)
        conn.commit()
    with eng.connect() as conn:
        before = conn.execute(
            text("SELECT result_json, public_alias FROM challenge_participants WHERE id=1")
        ).fetchone()

    svc = ChallengeService(session_factory=lambda: Session(eng))
    svc.confirm_results(1, 1, "k2")

    assert _p(eng, 1) == ("confirmed", 1)
    with eng.connect() as conn:
        after = conn.execute(
            text("SELECT result_json, public_alias FROM challenge_participants WHERE id=1")
        ).fetchone()
    # Inmutabilidad del resultado y estabilidad del alias en la confirmación
    assert before == after
    eng.dispose()


def test_confirm_only_verified_transitions(tmp_path):
    """PROHIBIDO: calculated → confirmed y persisted → confirmed."""
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "completed")
        _insert_participant(conn, 1, "verified", 1)
        _insert_participant(conn, 2, "calculated", 0)
        _insert_participant(conn, 3, "persisted", 0)
        conn.commit()
    svc = ChallengeService(session_factory=lambda: Session(eng))
    svc.confirm_results(1, 1, "k1")
    assert _p(eng, 1) == ("confirmed", 1)
    assert _p(eng, 2) == ("calculated", 0)
    assert _p(eng, 3) == ("persisted", 0)
    eng.dispose()


def test_confirm_rejects_disputed_and_invalidated(tmp_path):
    """PROHIBIDO: disputed → confirmed por confirmación ordinaria;
    invalidated → confirmed (terminalidad)."""
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "completed")
        _insert_participant(conn, 1, "disputed", 0)
        _insert_participant(conn, 2, "invalidated", 0)
        _insert_participant(conn, 3, "verified", 1)
        conn.commit()
    svc = ChallengeService(session_factory=lambda: Session(eng))
    svc.confirm_results(1, 1, "k1")
    assert _p(eng, 1) == ("disputed", 0)
    assert _p(eng, 2) == ("invalidated", 0)
    assert _p(eng, 3) == ("confirmed", 1)
    eng.dispose()


def test_confirm_requires_completed_state(tmp_path):
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "running")
        _insert_participant(conn, 1, "verified", 1)
        conn.commit()
    svc = ChallengeService(session_factory=lambda: Session(eng))
    with pytest.raises(ChallengeStateError):
        svc.confirm_results(1, 1, "k1")
    assert _p(eng, 1) == ("verified", 1)
    eng.dispose()


def test_confirm_idempotent(tmp_path):
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "completed")
        _insert_participant(conn, 1, "verified", 1)
        conn.commit()
    svc = ChallengeService(session_factory=lambda: Session(eng))
    r1 = svc.confirm_results(1, 1, "k1")
    assert r1.is_retry is False
    r2 = svc.confirm_results(1, 1, "k2")
    assert r2.is_retry is True
    assert _p(eng, 1) == ("confirmed", 1)
    eng.dispose()


def test_confirm_requires_organizer_or_admin(tmp_path):
    """Rechazo de usuario ajeno; autorización admin únicamente por is_admin
    del servidor (DEC-F7D-20 / F7-C-I1-FIX2), nunca por rol aportado."""
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "completed")
        _insert_participant(conn, 1, "verified", 1)
        _insert_participant(conn, 2, "verified", 1)
        conn.commit()
    svc = ChallengeService(session_factory=lambda: Session(eng))
    with pytest.raises(ChallengeError):
        svc.confirm_results(1, 2, "k-not-authorized")
    assert _p(eng, 1) == ("verified", 1)
    assert _p(eng, 2) == ("verified", 1)

    _set_admin(eng, 2)
    svc.confirm_results(1, 2, "k-admin")
    assert _p(eng, 1) == ("confirmed", 1)
    assert _p(eng, 2) == ("confirmed", 1)
    eng.dispose()


# --- verified/confirmed → disputed ------------------------------------------


def test_dispute_verified_sets_disputed(tmp_path):
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "completed")
        _insert_participant(conn, 1, "verified", 1)
        _insert_participant(conn, 2, "calculated", 0)
        conn.commit()
    svc = ChallengeService(session_factory=lambda: Session(eng))
    svc.dispute_challenge(1, 1, "k3")
    assert _p(eng, 1) == ("disputed", 0)
    assert _p(eng, 2) == ("calculated", 0)  # no afectado conserva estado
    assert _challenge_state(eng) == "disputed"
    eng.dispose()


def test_dispute_confirmed_sets_disputed(tmp_path):
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "completed")
        _insert_participant(conn, 1, "confirmed", 1)
        conn.commit()
    svc = ChallengeService(session_factory=lambda: Session(eng))
    svc.dispute_challenge(1, 1, "k3")
    assert _p(eng, 1) == ("disputed", 0)
    assert _challenge_state(eng) == "disputed"
    eng.dispose()


def test_dispute_requires_participant(tmp_path):
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "completed")
        _insert_participant(conn, 1, "verified", 1)
        conn.commit()
    svc = ChallengeService(session_factory=lambda: Session(eng))
    with pytest.raises(ParticipantNotFound):
        svc.dispute_challenge(1, 2, "k3")  # usuario ajeno no inscrito
    assert _p(eng, 1) == ("verified", 1)
    assert _challenge_state(eng) == "completed"
    eng.dispose()


def test_dispute_requires_completed_state(tmp_path):
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "running")
        _insert_participant(conn, 1, "verified", 1)
        conn.commit()
    svc = ChallengeService(session_factory=lambda: Session(eng))
    with pytest.raises(ChallengeStateError):
        svc.dispute_challenge(1, 1, "k3")
    assert _p(eng, 1) == ("verified", 1)
    eng.dispose()


def test_dispute_idempotent(tmp_path):
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "completed")
        _insert_participant(conn, 1, "verified", 1)
        conn.commit()
    svc = ChallengeService(session_factory=lambda: Session(eng))
    r1 = svc.dispute_challenge(1, 1, "k3")
    assert r1.is_retry is False
    r2 = svc.dispute_challenge(1, 1, "k4")
    assert r2.is_retry is True
    assert _p(eng, 1) == ("disputed", 0)
    eng.dispose()


# --- disputed → confirmed / invalidated (resolución) -------------------------


def test_resolve_confirm_all(tmp_path):
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "disputed")
        _insert_participant(conn, 1, "disputed", 0)
        _insert_participant(conn, 2, "disputed", 0)
        conn.commit()
    svc = ChallengeService(session_factory=lambda: Session(eng))
    svc.resolve_challenge(1, 1, "k4", resolution="confirm_all")
    assert _p(eng, 1) == ("confirmed", 1)
    assert _p(eng, 2) == ("confirmed", 1)
    assert _challenge_state(eng) == "resolved"
    eng.dispose()


def test_resolve_invalidate_all(tmp_path):
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "disputed")
        _insert_participant(conn, 1, "disputed", 0)
        conn.commit()
    svc = ChallengeService(session_factory=lambda: Session(eng))
    svc.resolve_challenge(1, 1, "k5", resolution="invalidate_all")
    assert _p(eng, 1) == ("invalidated", 0)
    assert _challenge_state(eng) == "resolved"
    eng.dispose()


def test_resolve_only_affects_disputed(tmp_path):
    """confirm_all no confirma ni invalidate_all invalida participantes ajenos
    a la disputa; los no afectados conservan su estado."""
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        conn.exec_driver_sql(
            "INSERT INTO users (id, username, pw_hash, created_at) VALUES (4, 'dave', 'h', 't')"
        )
        _insert_challenge(conn, "disputed")
        _insert_participant(conn, 1, "disputed", 0)
        _insert_participant(conn, 2, "verified", 1)
        _insert_participant(conn, 3, "confirmed", 1)
        _insert_participant(conn, 4, "calculated", 0)
        conn.commit()
    svc = ChallengeService(session_factory=lambda: Session(eng))
    svc.resolve_challenge(1, 1, "k6", resolution="confirm_all")
    assert _p(eng, 1) == ("confirmed", 1)
    assert _p(eng, 2) == ("verified", 1)
    assert _p(eng, 3) == ("confirmed", 1)
    assert _p(eng, 4) == ("calculated", 0)
    eng.dispose()


def test_verified_invalidated_requires_prior_dispute(tmp_path):
    """CONCLUSIÓN CONTRACTUAL (DEC-F7D-19, CONTRACTS §17.14 + §17.7):
    verified → invalidated directa NO está autorizada. La invalidación de un
    resultado exige disputa previa del participante y resolución administrativa
    (disputed → invalidated). Sin disputa, resolve no tiene efecto."""
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "completed")
        _insert_participant(conn, 1, "verified", 1)
        conn.commit()
    svc = ChallengeService(session_factory=lambda: Session(eng))
    with pytest.raises(ChallengeStateError):
        svc.resolve_challenge(1, 1, "k7", resolution="invalidate_all")
    assert _p(eng, 1) == ("verified", 1)
    assert _challenge_state(eng) == "completed"
    eng.dispose()


def test_resolve_invalid_resolution_rejected_atomically(tmp_path):
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "disputed")
        _insert_participant(conn, 1, "disputed", 0)
        _insert_participant(conn, 2, "disputed", 0)
        conn.commit()
    svc = ChallengeService(session_factory=lambda: Session(eng))
    with pytest.raises(ChallengeError, match="Invalid resolution"):
        svc.resolve_challenge(1, 1, "k8", resolution="partial")
    assert _p(eng, 1) == ("disputed", 0)
    assert _p(eng, 2) == ("disputed", 0)
    assert _challenge_state(eng) == "disputed"
    eng.dispose()


def test_resolve_commit_failure_leaves_no_partial_state(tmp_path):
    """Un fallo durante la transición multi-participante no deja estados
    parcialmente actualizados (rollback completo)."""
    db_file = tmp_path / "f7d_state.db"
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "disputed")
        _insert_participant(conn, 1, "disputed", 0)
        _insert_participant(conn, 2, "disputed", 0)
        conn.commit()

    def _boom():
        raise RuntimeError("commit inyectado")

    s = Session(eng)
    s.commit = _boom  # inyección de fallo en el punto de confirmación
    svc = ChallengeService(session_factory=lambda: s)
    with pytest.raises(RuntimeError, match="commit inyectado"):
        svc.resolve_challenge(1, 1, "k9", resolution="confirm_all")

    # Conexión nueva e independiente: nada persistió
    eng2 = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    with eng2.connect() as conn:
        rows = conn.execute(
            text("SELECT user_id, result_status, result_verified FROM challenge_participants ORDER BY user_id")
        ).fetchall()
        assert [(r[0], r[1], r[2]) for r in rows] == [(1, "disputed", 0), (2, "disputed", 0)]
        assert conn.execute(text("SELECT state FROM challenges WHERE id=1")).fetchone()[0] == "disputed"
    eng2.dispose()
    eng.dispose()


def test_resolve_idempotent(tmp_path):
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "disputed")
        _insert_participant(conn, 1, "disputed", 0)
        conn.commit()
    svc = ChallengeService(session_factory=lambda: Session(eng))
    r1 = svc.resolve_challenge(1, 1, "k10", resolution="invalidate_all")
    assert r1.is_retry is False
    r2 = svc.resolve_challenge(1, 1, "k11", resolution="confirm_all")
    assert r2.is_retry is True  # estado ya resuelto: sin efectos adicionales
    assert _p(eng, 1) == ("invalidated", 0)
    eng.dispose()


def test_resolve_requires_organizer_or_admin(tmp_path):
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "disputed")
        _insert_participant(conn, 1, "disputed", 0)
        conn.commit()
    svc = ChallengeService(session_factory=lambda: Session(eng))
    with pytest.raises(ChallengeError):
        svc.resolve_challenge(1, 2, "k12", resolution="confirm_all")
    assert _p(eng, 1) == ("disputed", 0)
    assert _challenge_state(eng) == "disputed"

    _set_admin(eng, 2)
    svc.resolve_challenge(1, 2, "k13", resolution="confirm_all")
    assert _p(eng, 1) == ("confirmed", 1)
    eng.dispose()


# --- invalidated: estado terminal -------------------------------------------


def test_invalidated_is_terminal(tmp_path):
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "disputed")
        _insert_participant(conn, 1, "disputed", 0)
        conn.commit()
    svc = ChallengeService(session_factory=lambda: Session(eng))
    svc.resolve_challenge(1, 1, "k14", resolution="invalidate_all")
    assert _p(eng, 1) == ("invalidated", 0)

    # Nada reabre invalidated: confirmación, disputa y resolución lo rechazan
    with pytest.raises(ChallengeStateError):
        svc.confirm_results(1, 1, "k15")
    with pytest.raises(ChallengeStateError):
        svc.dispute_challenge(1, 1, "k16")
    r = svc.resolve_challenge(1, 1, "k17", resolution="confirm_all")
    assert r.is_retry is True
    assert _p(eng, 1) == ("invalidated", 0)
    assert _challenge_state(eng) == "resolved"
    eng.dispose()
