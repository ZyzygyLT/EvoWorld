"""F7-D FIX10-A-FINAL: procedencia durable de operaciones de desafío.

Contrato aplicado:

- CONTRACTS §17.7 — resolución administrativa auditable: actor, fecha,
  identidad de operación, resultado e idempotencia.
- CONTRACTS §17.13 — operaciones globales atómicas con identidad de petición
  idempotente; ámbito ``(challenge_id, requesting_user_id,
  client_idempotency_key)``; misma clave + mismo contenido → resultado
  original; misma clave + contenido distinto → conflicto 409.
- CONTRACTS §17.9 — errores: 409 para conflicto de idempotencia.

Todo registro se lee desde una CONEXIÓN NUEVA e independiente de la que
ejecutó la operación (durabilidad real, sin caché de sesión). La identidad y
la secuencia T1/T2 de operaciones por mundo NO se reutilizan: la procedencia
vive en su propia tabla ``challenge_operations``.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

import worldsim.server.db as dbmod
from worldsim.server.challenge_service import (
    ChallengeService,
    IdempotencyConflict,
)
from worldsim.server.db import ChallengeParticipantRow, WorldRow


# ---------------------------------------------------------------------------
# Infraestructura de pruebas
# ---------------------------------------------------------------------------


def _fresh_engine(tmp_path, name="f7d_prov.db"):
    db_file = tmp_path / name
    eng0 = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    dbmod.Base.metadata.create_all(eng0)
    eng0.dispose()
    eng = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    dbmod.ensure_schema(eng)
    return eng


def _seed_users(conn) -> None:
    for uid, name in ((1, "alice"), (2, "bob"), (3, "carol")):
        conn.exec_driver_sql(
            "INSERT INTO users (id, username, pw_hash, created_at) "
            f"VALUES ({uid}, '{name}', 'h', 't')"
        )


def _challenge_json() -> str:
    return json.dumps({"seed": 42, "primary_metric": "total_population"})


def _insert_challenge(conn, state="completed", cid=1) -> None:
    conn.execute(
        text(
            "INSERT INTO challenges (id, organizer_user_id, name, config_json, "
            "rules_version, duration_ticks, state, created_at) "
            "VALUES (:cid, 1, 'c', :config, 'v1', 10, :state, 't')"
        ),
        {"cid": cid, "config": _challenge_json(), "state": state},
    )


def _insert_participant(
    conn, user_id, result_status="verified", result_verified=1, cid=1
) -> None:
    conn.execute(
        text(
            "INSERT INTO challenge_participants (challenge_id, user_id, state, "
            "joined_at, result_json, result_verified, result_status, public_alias) "
            "VALUES (:cid, :uid, 'finalizado', 't', :rj, :rv, :rs, :alias)"
        ),
        {
            "cid": cid,
            "uid": user_id,
            "rj": json.dumps({"total_population": 100}),
            "rv": result_verified,
            "rs": result_status,
            "alias": uuid.uuid4().hex,
        },
    )


def _service(eng) -> ChallengeService:
    return ChallengeService(session_factory=lambda: Session(eng))


def _set_admin(eng, user_id: int) -> None:
    with eng.connect() as conn:
        conn.execute(
            text("UPDATE users SET is_admin = 1 WHERE id = :u"), {"u": user_id}
        )
        conn.commit()


def _setup(tmp_path, challenge_state="completed", participant_status="verified"):
    """BD con usuarios 1/2/3, desafío 1 y participante usuario 1."""
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, challenge_state)
        _insert_participant(conn, 1, participant_status, 1 if participant_status in ("verified", "confirmed") else 0)
        conn.commit()
    return eng


# --- lecturas desde conexión nueva e independiente -------------------------


def _new_conn(eng):
    """Conexión NUEVA al mismo archivo, distinta de la sesión ejecutora."""
    return create_engine(
        f"sqlite:///{eng.url.database}", connect_args={"check_same_thread": False}
    )


def _read_records(eng) -> list[dict]:
    new_eng = _new_conn(eng)
    try:
        with new_eng.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT challenge_id, operation, actor_user_id, "
                    "client_idempotency_key, request_hash, outcome, created_at "
                    "FROM challenge_operations ORDER BY id"
                )
            ).fetchall()
        return [dict(r._mapping) for r in rows]
    finally:
        new_eng.dispose()


def _read_challenge_state(eng, cid=1) -> str:
    new_eng = _new_conn(eng)
    try:
        with new_eng.connect() as conn:
            state = conn.execute(
                text("SELECT state FROM challenges WHERE id = :c"), {"c": cid}
            ).scalar()
        assert state is not None
        return state
    finally:
        new_eng.dispose()


def _read_participant(eng, user_id=1, cid=1):
    new_eng = _new_conn(eng)
    try:
        with new_eng.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT result_status, result_verified FROM "
                    "challenge_participants WHERE challenge_id = :c AND user_id = :u"
                ),
                {"c": cid, "u": user_id},
            ).fetchone()
        assert row is not None
        return row[0], row[1]
    finally:
        new_eng.dispose()


def _count(eng, table: str) -> int:
    new_eng = _new_conn(eng)
    try:
        with new_eng.connect() as conn:
            return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
    finally:
        new_eng.dispose()


# ---------------------------------------------------------------------------
# Esquema de la tabla de procedencia
# ---------------------------------------------------------------------------


def test_challenge_operations_schema(tmp_path):
    """Campos exigidos por el contrato y ámbito UNIQUE de §17.13, sin
    identidad ni secuencia T1/T2 de operaciones por mundo."""
    eng = _fresh_engine(tmp_path)
    insp = inspect(eng)
    tables = set(insp.get_table_names())
    assert "challenge_operations" in tables

    cols = {c["name"]: c for c in insp.get_columns("challenge_operations")}
    assert set(cols) == {
        "id",
        "challenge_id",
        "operation",
        "actor_user_id",
        "client_idempotency_key",
        "request_hash",
        "outcome",
        "created_at",
    }
    for required in (
        "challenge_id",
        "operation",
        "actor_user_id",
        "client_idempotency_key",
        "request_hash",
        "created_at",
    ):
        assert cols[required]["nullable"] is False, required
    assert cols["outcome"]["nullable"] is True

    # Ámbito de idempotencia §17.13: (challenge_id, actor, key)
    uniques = {u["name"] for u in insp.get_unique_constraints("challenge_operations")}
    assert "uq_challenge_operation_idem" in uniques

    fks = insp.get_foreign_keys("challenge_operations")
    fk_targets = {(fk["referred_table"], tuple(fk["referred_columns"])) for fk in fks}
    assert ("challenges", ("id",)) in fk_targets
    assert ("users", ("id",)) in fk_targets

    # Procedencia propia: NO reutiliza T1/T2 por mundo
    assert "operation_id" not in cols
    assert "operation_sequence" not in cols
    assert "world_id" not in cols
    eng.dispose()


def test_challenge_operation_idem_scope_includes_actor(tmp_path):
    """El ámbito UNIQUE incluye al actor autenticado (requesting_user_id):
    misma clave de otro actor en el mismo desafío NO colisiona."""
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        _insert_challenge(conn, "completed")
        conn.execute(
            text(
                "INSERT INTO challenge_operations (challenge_id, operation, "
                "actor_user_id, client_idempotency_key, request_hash, outcome, created_at) "
                "VALUES (1, 'dispute', 1, 'shared-key', 'h', 'disputed', 't')"
            )
        )
        conn.commit()
        # Mismo desafío + misma clave + OTRO actor → permitido (ámbito §17.13)
        conn.execute(
            text(
                "INSERT INTO challenge_operations (challenge_id, operation, "
                "actor_user_id, client_idempotency_key, request_hash, outcome, created_at) "
                "VALUES (1, 'dispute', 2, 'shared-key', 'h', 'disputed', 't')"
            )
        )
        conn.commit()
        # Mismo desafío + MISMO actor + MISMA clave → rechazado por UNIQUE
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO challenge_operations (challenge_id, operation, "
                    "actor_user_id, client_idempotency_key, request_hash, outcome, created_at) "
                    "VALUES (1, 'confirm', 1, 'shared-key', 'h', 'confirmed', 't')"
                )
            )
    eng.dispose()


# ---------------------------------------------------------------------------
# Registros durable de confirmación / disputa / resolución
# ---------------------------------------------------------------------------


def test_provenance_confirm_record_durable(tmp_path):
    """confirm → registro durable leído desde una conexión nueva."""
    eng = _setup(tmp_path)
    svc = _service(eng)
    res = svc.confirm_results(1, 1, "prov-confirm-1", request_hash="h-confirm-1")
    assert res.is_retry is False

    records = _read_records(eng)
    assert len(records) == 1
    r = records[0]
    assert r["challenge_id"] == 1
    assert r["operation"] == "confirm"
    assert r["actor_user_id"] == 1  # organizador autenticado
    assert r["client_idempotency_key"] == "prov-confirm-1"
    assert r["request_hash"] == "h-confirm-1"
    assert r["outcome"] == "confirmed"
    assert r["created_at"]  # timestamp durable presente
    assert _read_participant(eng, 1) == ("confirmed", 1)
    eng.dispose()


def test_provenance_dispute_record_durable(tmp_path):
    """disputa → registro durable con el participante como actor."""
    eng = _setup(tmp_path)
    svc = _service(eng)
    res = svc.dispute_challenge(1, 1, "prov-dispute-1", request_hash="h-dispute-1")
    assert res.is_retry is False

    records = _read_records(eng)
    assert len(records) == 1
    r = records[0]
    assert r["challenge_id"] == 1
    assert r["operation"] == "dispute"
    assert r["actor_user_id"] == 1  # participante que presentó la disputa
    assert r["client_idempotency_key"] == "prov-dispute-1"
    assert r["request_hash"] == "h-dispute-1"
    assert r["outcome"] == "disputed"
    assert r["created_at"]
    assert _read_challenge_state(eng) == "disputed"
    eng.dispose()


def test_provenance_resolve_record_durable_admin_actor(tmp_path):
    """resolución → registro auditable con el administrador como actor
    (§17.7: actor, fecha, identidad de operación, resultado, idempotencia)."""
    eng = _setup(tmp_path, challenge_state="disputed", participant_status="disputed")
    _set_admin(eng, 2)
    svc = _service(eng)
    res = svc.resolve_challenge(
        1, 2, "prov-resolve-1", request_hash="h-resolve-1",
        resolution="confirm_all",
    )
    assert res.is_retry is False

    records = _read_records(eng)
    assert len(records) == 1
    r = records[0]
    assert r["challenge_id"] == 1
    assert r["operation"] == "resolve"
    assert r["actor_user_id"] == 2  # administrador is_admin verificado en servidor
    assert r["client_idempotency_key"] == "prov-resolve-1"
    assert r["request_hash"] == "h-resolve-1"
    assert r["outcome"] == "confirm_all"  # resultado de la resolución
    assert r["created_at"]
    assert _read_challenge_state(eng) == "resolved"
    assert _read_participant(eng, 1) == ("confirmed", 1)
    eng.dispose()


# ---------------------------------------------------------------------------
# Atomicidad: fallo antes del commit → sin estado parcial ni registro huérfano
# ---------------------------------------------------------------------------


def _commit_failure_session(eng, monkeypatch):
    sess = Session(eng)

    def _boom():
        raise RuntimeError("commit falló")

    monkeypatch.setattr(sess, "commit", _boom)
    return sess


def test_provenance_atomic_confirm_commit_failure(tmp_path, monkeypatch):
    eng = _setup(tmp_path)
    sess = _commit_failure_session(eng, monkeypatch)
    svc = ChallengeService(session_factory=lambda: sess)
    with pytest.raises(RuntimeError, match="commit falló"):
        svc.confirm_results(1, 1, "prov-atom-confirm", request_hash="h1")
    sess.close()

    # Conexión nueva: ni registro huérfano ni cambio de estado parcial
    assert _read_records(eng) == []
    assert _read_challenge_state(eng) == "completed"
    assert _read_participant(eng, 1) == ("verified", 1)
    eng.dispose()


def test_provenance_atomic_dispute_commit_failure(tmp_path, monkeypatch):
    eng = _setup(tmp_path)
    sess = _commit_failure_session(eng, monkeypatch)
    svc = ChallengeService(session_factory=lambda: sess)
    with pytest.raises(RuntimeError, match="commit falló"):
        svc.dispute_challenge(1, 1, "prov-atom-dispute", request_hash="h1")
    sess.close()

    assert _read_records(eng) == []
    assert _read_challenge_state(eng) == "completed"
    assert _read_participant(eng, 1) == ("verified", 1)
    eng.dispose()


def test_provenance_atomic_resolve_commit_failure(tmp_path, monkeypatch):
    eng = _setup(tmp_path, challenge_state="disputed", participant_status="disputed")
    sess = _commit_failure_session(eng, monkeypatch)
    svc = ChallengeService(session_factory=lambda: sess)
    with pytest.raises(RuntimeError, match="commit falló"):
        svc.resolve_challenge(
            1, 1, "prov-atom-resolve", request_hash="h1", resolution="confirm_all"
        )
    sess.close()

    assert _read_records(eng) == []
    assert _read_challenge_state(eng) == "disputed"
    assert _read_participant(eng, 1) == ("disputed", 0)
    eng.dispose()


# ---------------------------------------------------------------------------
# Idempotencia: reintento sin duplicar; clave reutilizada con payload distinto
# ---------------------------------------------------------------------------


def test_provenance_confirm_idempotent_retry_no_duplicate(tmp_path):
    eng = _setup(tmp_path)
    svc = _service(eng)
    r1 = svc.confirm_results(1, 1, "prov-idem-c", request_hash="h-same")
    r2 = svc.confirm_results(1, 1, "prov-idem-c", request_hash="h-same")
    assert r1.is_retry is False
    assert r2.is_retry is True

    records = _read_records(eng)
    assert len(records) == 1  # sin duplicado
    assert records[0]["operation"] == "confirm"
    assert _read_participant(eng, 1) == ("confirmed", 1)  # estado no alterado
    eng.dispose()


def test_provenance_dispute_idempotent_retry_no_duplicate(tmp_path):
    eng = _setup(tmp_path)
    svc = _service(eng)
    r1 = svc.dispute_challenge(1, 1, "prov-idem-d", request_hash="h-same")
    r2 = svc.dispute_challenge(1, 1, "prov-idem-d", request_hash="h-same")
    assert r1.is_retry is False
    assert r2.is_retry is True

    records = _read_records(eng)
    assert len(records) == 1
    assert _read_challenge_state(eng) == "disputed"
    assert _read_participant(eng, 1) == ("disputed", 0)
    eng.dispose()


def test_provenance_resolve_idempotent_retry_no_duplicate(tmp_path):
    eng = _setup(tmp_path, challenge_state="disputed", participant_status="disputed")
    svc = _service(eng)
    r1 = svc.resolve_challenge(
        1, 1, "prov-idem-r", request_hash="h-same", resolution="confirm_all"
    )
    r2 = svc.resolve_challenge(
        1, 1, "prov-idem-r", request_hash="h-same", resolution="confirm_all"
    )
    assert r1.is_retry is False
    assert r2.is_retry is True

    records = _read_records(eng)
    assert len(records) == 1
    assert _read_challenge_state(eng) == "resolved"
    eng.dispose()


def test_provenance_confirm_conflict_same_key_different_payload(tmp_path):
    """Misma clave + contenido distinto → 409 (IdempotencyConflict, §17.13),
    sin registro nuevo y sin mutar el estado ya confirmado."""
    eng = _setup(tmp_path)
    svc = _service(eng)
    svc.confirm_results(1, 1, "prov-conf-c", request_hash="h-original")

    with pytest.raises(IdempotencyConflict):
        svc.confirm_results(1, 1, "prov-conf-c", request_hash="h-distinto")

    records = _read_records(eng)
    assert len(records) == 1  # el conflicto no añade registro
    assert records[0]["request_hash"] == "h-original"
    assert _read_participant(eng, 1) == ("confirmed", 1)
    eng.dispose()


def test_provenance_dispute_conflict_same_key_different_payload(tmp_path):
    eng = _setup(tmp_path)
    svc = _service(eng)
    svc.dispute_challenge(1, 1, "prov-conf-d", request_hash="h-original")

    with pytest.raises(IdempotencyConflict):
        svc.dispute_challenge(1, 1, "prov-conf-d", request_hash="h-distinto")

    records = _read_records(eng)
    assert len(records) == 1
    assert records[0]["request_hash"] == "h-original"
    assert _read_challenge_state(eng) == "disputed"
    eng.dispose()


def test_provenance_resolve_conflict_same_key_different_payload(tmp_path):
    eng = _setup(tmp_path, challenge_state="disputed", participant_status="disputed")
    svc = _service(eng)
    svc.resolve_challenge(
        1, 1, "prov-conf-r", request_hash="h-original", resolution="confirm_all"
    )

    # Misma clave, contenido distinto → conflicto, aunque el estado ya sea
    # el objetivo (el ámbito de §17.13 se evalúa antes del retorno por estado).
    with pytest.raises(IdempotencyConflict):
        svc.resolve_challenge(
            1, 1, "prov-conf-r", request_hash="h-distinto", resolution="confirm_all"
        )

    records = _read_records(eng)
    assert len(records) == 1
    assert records[0]["request_hash"] == "h-original"
    assert records[0]["outcome"] == "confirm_all"
    assert _read_challenge_state(eng) == "resolved"
    eng.dispose()


# ---------------------------------------------------------------------------
# Ciclo de vida: registros de las operaciones globales con registro contractual
# ---------------------------------------------------------------------------


def test_provenance_lifecycle_records(tmp_path):
    """create/publish/open_enrollment/enroll/close_enrollment/cancel dejan
    registro durable con actor, clave, hash, outcome y timestamp."""
    eng = _fresh_engine(tmp_path)
    with eng.connect() as conn:
        _seed_users(conn)
        conn.commit()
    svc = _service(eng)

    ops = [
        dict(
            call=lambda: svc.create_challenge(
                organizer_user_id=1,
                name="Desafío",
                config_json=_challenge_json(),
                rules_version="1.0",
                duration_ticks=30,
                client_idempotency_key="lc-create",
                request_hash="h-create",
            ),
            operation="create",
            actor=1,
            outcome="draft",
        ),
        dict(
            call=lambda: svc.publish_challenge(
                challenge_id=1, organizer_user_id=1,
                client_idempotency_key="lc-publish", request_hash="h-publish",
            ),
            operation="publish",
            actor=1,
            outcome="published",
        ),
        dict(
            call=lambda: svc.open_enrollment(
                challenge_id=1, organizer_user_id=1,
                client_idempotency_key="lc-open", request_hash="h-open",
            ),
            operation="open_enrollment",
            actor=1,
            outcome="enrollment_open",
        ),
        dict(
            call=lambda: svc.enroll_participant(
                challenge_id=1, user_id=2,
                client_idempotency_key="lc-enroll", request_hash="h-enroll",
            ),
            operation="enroll",
            actor=2,
            outcome="enrolled",
        ),
        dict(
            call=lambda: svc.close_enrollment(
                challenge_id=1, organizer_user_id=1,
                client_idempotency_key="lc-close", request_hash="h-close",
            ),
            operation="close_enrollment",
            actor=1,
            outcome="enrollment_closed",
        ),
        dict(
            call=lambda: svc.cancel_challenge(
                challenge_id=1, organizer_user_id=1,
                client_idempotency_key="lc-cancel", request_hash="h-cancel",
            ),
            operation="cancel",
            actor=1,
            outcome="canceled",
        ),
    ]

    for op in ops:
        result = op["call"]()
        assert result.is_retry is False, op["operation"]

    records = _read_records(eng)
    assert [r["operation"] for r in records] == [
        "create", "publish", "open_enrollment", "enroll",
        "close_enrollment", "cancel",
    ]
    for op, r in zip(ops, records):
        assert r["challenge_id"] == 1
        assert r["actor_user_id"] == op["actor"]
        assert r["outcome"] == op["outcome"]
        assert r["created_at"]
    # Identidad de petición persistida por operación
    assert [r["client_idempotency_key"] for r in records] == [
        "lc-create", "lc-publish", "lc-open", "lc-enroll", "lc-close", "lc-cancel",
    ]
    assert [r["request_hash"] for r in records] == [
        "h-create", "h-publish", "h-open", "h-enroll", "h-close", "h-cancel",
    ]

    # Sin reutilización de la identidad/secuencia T1/T2 por mundo
    assert _count(eng, "world_operations") == 0
    assert _count(eng, "world_events") == 0
    eng.dispose()


def test_provenance_complete_record_no_t1_reuse(tmp_path):
    """complete deja registro de procedencia sin crear operaciones T1/T2."""
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

    svc = _service(eng)
    res = svc.complete_challenge(1, 1, "prov-complete-1", request_hash="h-complete")
    assert res.is_retry is False

    records = _read_records(eng)
    assert len(records) == 1
    assert records[0]["operation"] == "complete"
    assert records[0]["actor_user_id"] == 1
    assert records[0]["outcome"] == "completed"
    assert records[0]["request_hash"] == "h-complete"
    assert records[0]["created_at"]
    assert _read_challenge_state(eng) == "completed"
    assert _read_participant(eng, 1)[0] == "verified"

    # La procedencia NO reutiliza identidad ni secuencia T1/T2 por mundo
    assert _count(eng, "world_operations") == 0
    assert _count(eng, "world_events") == 0
    eng.dispose()
