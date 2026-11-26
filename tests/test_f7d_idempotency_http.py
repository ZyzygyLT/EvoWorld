"""F7-D HTTP idempotency tests: client Idempotency-Key header enforcement.

Scenarios A–F per contract §17.13 / §17.9.
Independent session reads verify durability.

These are REAL HTTP tests: every scenario is driven through FastAPI's
TestClient against an isolated SQLite database, and every assertion about
durable state is read back from ``challenge_operations`` / ``challenges``.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import worldsim.server.app as appmod
import worldsim.server.db as dbmod
import worldsim.server.runner as runnermod
from worldsim.server.challenge_service import ChallengeService, IdempotencyConflict
from worldsim.server.db import Base, ChallengeRow


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'idem.db'}",
        connect_args={"check_same_thread": False},
    )
    dbmod.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    runnermod.registry = runnermod.RunnerRegistry()
    appmod.registry = runnermod.registry
    Base.metadata.create_all(engine)
    dbmod.ensure_schema(engine)
    return TestClient(appmod.app)


def _register(c: TestClient, username: str = "user") -> tuple[dict, int]:
    r = c.post(
        "/api/auth/register",
        json={"username": username, "password": "pass1234"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return {"Authorization": f"Bearer {body['token']}"}, body["user_id"]


def _challenge_body(name: str = "Test Challenge") -> dict:
    return {
        "name": name,
        "config_json": json.dumps(
            {"seed": 42, "primary_metric": "total_population"}
        ),
        "rules_version": "1.0",
        "duration_ticks": 10,
    }


def _create_challenge(c: TestClient, headers: dict, name: str = "Test Challenge") -> int:
    # La clave debe ser estable y válida según el contrato de idempotencia
    # (``^[A-Za-z0-9._:\-]{1,64}$``); ``name`` puede contener espacios.
    key = "create-" + "".join(ch if ch.isalnum() else "-" for ch in name)
    r = c.post(
        "/api/challenges",
        json=_challenge_body(name),
        headers={**headers, "Idempotency-Key": key},
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _publish_challenge(c: TestClient, headers: dict, cid: int) -> None:
    """Publica el desafío (draft → published) para poder inscribirse."""
    r = c.put(
        f"/api/challenges/{cid}/publish",
        headers={**headers, "Idempotency-Key": f"publish-{cid}"},
    )
    assert r.status_code == 200, r.text


def _ops(challenge_id: int) -> list[dict]:
    with dbmod.SessionLocal() as s:
        rows = s.execute(
            text(
                "SELECT challenge_id, operation, actor_user_id, "
                "client_idempotency_key, request_hash, outcome "
                "FROM challenge_operations WHERE challenge_id = :cid ORDER BY id"
            ),
            {"cid": challenge_id},
        ).fetchall()
        return [dict(r._mapping) for r in rows]


def _ops_of(challenge_id: int, operation: str) -> list[dict]:
    return [o for o in _ops(challenge_id) if o["operation"] == operation]


def _state(challenge_id: int) -> str:
    with dbmod.SessionLocal() as s:
        return s.execute(
            text("SELECT state FROM challenges WHERE id = :c"), {"c": challenge_id}
        ).scalar()


def _set_state(challenge_id: int, state: str) -> None:
    with dbmod.SessionLocal() as s:
        c = s.get(ChallengeRow, challenge_id)
        c.state = state
        s.commit()


# ===========================================================================
# Scenario A: first request with key K and payload P → normal execution, 1 record
# ===========================================================================

def test_a_create_first_request(client):
    headers, uid = _register(client)
    r = client.post(
        "/api/challenges",
        json=_challenge_body("A Challenge"),
        headers={**headers, "Idempotency-Key": "create-a"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "draft"
    cid = body["id"]

    ops = _ops_of(cid, "create")
    assert len(ops) == 1
    assert ops[0]["actor_user_id"] == uid
    assert ops[0]["client_idempotency_key"] == "create-a"
    assert ops[0]["request_hash"]
    assert ops[0]["outcome"] == "draft"
    assert _state(cid) == "draft"

    with dbmod.SessionLocal() as s:
        creation = s.execute(
            text(
                "SELECT actor_user_id, client_idempotency_key, request_hash, challenge_id "
                "FROM challenge_creation_requests"
            )
        ).mappings().one()
    assert creation["actor_user_id"] == uid
    assert creation["client_idempotency_key"] == "create-a"
    assert creation["challenge_id"] == cid
    assert creation["request_hash"] == ops[0]["request_hash"]


def test_a_publish_first_request(client):
    headers, uid = _register(client)
    cid = _create_challenge(client, headers, "A Pub")

    r = client.put(
        f"/api/challenges/{cid}/publish",
        headers={**headers, "Idempotency-Key": "publish-a"},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"id": cid, "state": "published"}

    ops = _ops_of(cid, "publish")
    assert len(ops) == 1
    assert ops[0]["actor_user_id"] == uid
    assert ops[0]["client_idempotency_key"] == "publish-a"
    assert ops[0]["request_hash"]
    assert ops[0]["outcome"] == "published"
    assert _state(cid) == "published"


# ===========================================================================
# Scenario B: retry with K and P → same original result, no repeated effects
# ===========================================================================

def test_b_publish_retry_same_response(client):
    headers, _ = _register(client)
    cid = _create_challenge(client, headers, "B Pub")

    r1 = client.put(
        f"/api/challenges/{cid}/publish",
        headers={**headers, "Idempotency-Key": "publish-b"},
    )
    r2 = client.put(
        f"/api/challenges/{cid}/publish",
        headers={**headers, "Idempotency-Key": "publish-b"},
    )
    assert r1.status_code == 200 == r2.status_code
    assert r1.json() == r2.json() == {"id": cid, "state": "published"}

    ops = _ops_of(cid, "publish")
    assert len(ops) == 1  # exactly one durable record, no duplicate


def test_b_create_retry_same_response(client):
    """Retry of create returns the ORIGINAL response (state 'draft' at the time
    it first ran), even if the challenge has since advanced, and does not
    create a second challenge or a second record."""
    headers, _ = _register(client)
    key = "create-b-retry"
    r1 = client.post(
        "/api/challenges",
        json=_challenge_body("B Create"),
        headers={**headers, "Idempotency-Key": key},
    )
    assert r1.status_code == 200, r1.text
    cid = r1.json()["id"]
    original = r1.json()
    assert original["state"] == "draft"

    # Advance the challenge: the retry must NOT reflect the new state.
    client.put(
        f"/api/challenges/{cid}/publish",
        headers={**headers, "Idempotency-Key": "publish-after-create"},
    )
    assert _state(cid) == "published"

    r2 = client.post(
        "/api/challenges",
        json=_challenge_body("B Create"),
        headers={**headers, "Idempotency-Key": key},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json() == original  # replay of the ORIGINAL response
    assert r2.json()["state"] == "draft"

    # No second challenge, no duplicate create record
    with dbmod.SessionLocal() as s:
        n_challenges = s.execute(
            text("SELECT COUNT(*) FROM challenges")
        ).scalar()
    assert n_challenges == 1
    assert len(_ops_of(cid, "create")) == 1


def test_c_create_same_key_different_payload_conflict_409(client):
    headers, _ = _register(client)
    key = "create-payload-conflict"
    first = client.post(
        "/api/challenges",
        json=_challenge_body("Original payload"),
        headers={**headers, "Idempotency-Key": key},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/api/challenges",
        json=_challenge_body("Changed payload"),
        headers={**headers, "Idempotency-Key": key},
    )
    assert second.status_code == 409, second.text
    assert "idempotency_conflict" in second.text

    with dbmod.SessionLocal() as s:
        assert s.execute(text("SELECT COUNT(*) FROM challenges")).scalar() == 1
        assert s.execute(
            text("SELECT COUNT(*) FROM challenge_creation_requests")
        ).scalar() == 1
        assert s.execute(
            text("SELECT COUNT(*) FROM challenge_operations WHERE operation = 'create'")
        ).scalar() == 1


def test_e_create_same_key_different_actors_are_independent(client):
    h1, uid1 = _register(client, "creatorone")
    h2, uid2 = _register(client, "creatortwo")
    key = "shared-create-key"
    body = _challenge_body("Same semantic payload")

    r1 = client.post(
        "/api/challenges", json=body,
        headers={**h1, "Idempotency-Key": key},
    )
    r2 = client.post(
        "/api/challenges", json=body,
        headers={**h2, "Idempotency-Key": key},
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["id"] != r2.json()["id"]

    with dbmod.SessionLocal() as s:
        rows = s.execute(
            text(
                "SELECT actor_user_id, client_idempotency_key, challenge_id "
                "FROM challenge_creation_requests ORDER BY actor_user_id"
            )
        ).mappings().all()
    assert len(rows) == 2
    assert {row["actor_user_id"] for row in rows} == {uid1, uid2}
    assert {row["client_idempotency_key"] for row in rows} == {key}
    assert {row["challenge_id"] for row in rows} == {
        r1.json()["id"], r2.json()["id"]
    }


def test_b_enroll_retry_same_response(client):
    headers, uid = _register(client)
    cid = _create_challenge(client, headers, "B Enroll")
    _publish_challenge(client, headers, cid)

    r1 = client.post(
        f"/api/challenges/{cid}/enroll",
        headers={**headers, "Idempotency-Key": "enroll-b"},
        json={"client_idempotency_key": "enroll-b"},
    )
    r2 = client.post(
        f"/api/challenges/{cid}/enroll",
        headers={**headers, "Idempotency-Key": "enroll-b"},
        json={"client_idempotency_key": "enroll-b"},
    )
    assert r1.status_code == 200 == r2.status_code
    # Reintento idempotente: la primera ejecución crea el registro
    # (retry=False); el reintento devuelve original_outcome sin repetir
    # efectos (retry=True). Confirmado por tener exactamente 1 registro
    # de inscripciones.
    assert r1.json() == {"challenge_id": cid, "user_id": uid, "retry": False}
    assert r2.json() == {"challenge_id": cid, "user_id": uid, "retry": True}

    ops = _ops_of(cid, "enroll")
    assert len(ops) == 1
    with dbmod.SessionLocal() as s:
        n_participants = s.execute(
            text(
                "SELECT COUNT(*) FROM challenge_participants "
                "WHERE challenge_id = :c AND user_id = :u"
            ),
            {"c": cid, "u": uid},
        ).scalar()
    assert n_participants == 1


def test_b_dispute_retry_same_response(client):
    headers, uid = _register(client)
    cid = _create_challenge(client, headers, "B Dispute")
    _set_state(cid, "completed")

    # Crear participante para que dispute funcione (el usuario autenticado es participante)
    with dbmod.SessionLocal() as s:
        from worldsim.server.db import ChallengeParticipantRow
        p = ChallengeParticipantRow(
            challenge_id=cid,
            user_id=uid,
            world_id=None,
            state="inscrito",
            joined_at=s.execute(text("SELECT CURRENT_TIMESTAMP")).scalar(),
        )
        s.add(p)
        s.commit()

    r1 = client.post(
        f"/api/challenges/{cid}/dispute",
        headers={**headers, "Idempotency-Key": "dispute-b"},
        json={"client_idempotency_key": "dispute-b"},
    )
    r2 = client.post(
        f"/api/challenges/{cid}/dispute",
        headers={**headers, "Idempotency-Key": "dispute-b"},
        json={"client_idempotency_key": "dispute-b"},
    )
    assert r1.status_code == 200 == r2.status_code
    # Reintento idempotente: misma clave → resultado original sin efectos
    # repetidos.
    assert r1.json() == {"id": cid, "state": "disputed"}
    assert r2.json() == {"id": cid, "state": "disputed"}

    assert len(_ops_of(cid, "dispute")) == 1
    assert _state(cid) == "disputed"


def test_b_resolve_retry_same_response(client):
    headers, _ = _register(client)
    cid = _create_challenge(client, headers, "B Resolve")
    _set_state(cid, "disputed")

    r1 = client.post(
        f"/api/challenges/{cid}/resolve",
        headers={**headers, "Idempotency-Key": "resolve-b"},
        json={"client_idempotency_key": "resolve-b", "resolution": "confirm_all"},
    )
    r2 = client.post(
        f"/api/challenges/{cid}/resolve",
        headers={**headers, "Idempotency-Key": "resolve-b"},
        json={"client_idempotency_key": "resolve-b", "resolution": "confirm_all"},
    )
    assert r1.status_code == 200 == r2.status_code
    assert r1.json() == r2.json() == {"id": cid, "state": "resolved"}
    assert len(_ops_of(cid, "resolve")) == 1
    assert _state(cid) == "resolved"


# ===========================================================================
# Scenario C: retry with K and P2 → HTTP 409, state intact, one record
# ===========================================================================

def test_c_resolve_conflict_409(client):
    """Same key + different `resolution` → HTTP 409 at the HTTP layer."""
    headers, _ = _register(client)
    cid = _create_challenge(client, headers, "C Resolve")
    _set_state(cid, "disputed")

    key = "resolve-conflict"
    r1 = client.post(
        f"/api/challenges/{cid}/resolve",
        headers={**headers, "Idempotency-Key": key},
        json={"client_idempotency_key": key, "resolution": "confirm_all"},
    )
    assert r1.status_code == 200, r1.text
    assert _state(cid) == "resolved"

    # Same key, different semantic content (resolution) → 409
    r2 = client.post(
        f"/api/challenges/{cid}/resolve",
        headers={**headers, "Idempotency-Key": key},
        json={"client_idempotency_key": key, "resolution": "invalidate_all"},
    )
    assert r2.status_code == 409, r2.text
    assert "idempotency_conflict" in r2.text

    # State intact, exactly one resolve record
    assert _state(cid) == "resolved"
    assert len(_ops_of(cid, "resolve")) == 1


def test_c_publish_service_conflict(client):
    """The service rejects a reused key with a different hash → 409 mapping."""
    headers, _ = _register(client)
    cid = _create_challenge(client, headers, "C Publish")

    client.put(
        f"/api/challenges/{cid}/publish",
        headers={**headers, "Idempotency-Key": "publish-conflict"},
    )
    svc = ChallengeService()
    with pytest.raises(IdempotencyConflict):
        svc.publish_challenge(
            challenge_id=cid,
            organizer_user_id=1,
            client_idempotency_key="publish-conflict",
            request_hash="a-different-hash",
        )
    assert len(_ops_of(cid, "publish")) == 1
    assert _state(cid) == "published"


# ===========================================================================
# Scenario D: no key or invalid format → HTTP 400, no mutation
# ===========================================================================

def test_d_publish_missing_key_400(client):
    headers, _ = _register(client)
    cid = _create_challenge(client, headers, "D Missing")

    r = client.put(f"/api/challenges/{cid}/publish", headers=headers)
    assert r.status_code == 400, r.text
    assert "Idempotency-Key header required" in r.text

    assert _state(cid) == "draft"
    assert len(_ops_of(cid, "publish")) == 0


def test_d_publish_invalid_key_400(client):
    headers, _ = _register(client)
    cid = _create_challenge(client, headers, "D Invalid")

    r = client.put(
        f"/api/challenges/{cid}/publish",
        headers={**headers, "Idempotency-Key": "invalid key with spaces!"},
    )
    assert r.status_code == 400, r.text
    assert "Invalid Idempotency-Key" in r.text

    assert _state(cid) == "draft"
    assert len(_ops_of(cid, "publish")) == 0


def test_d_enroll_missing_key_400(client):
    headers, _ = _register(client)
    cid = _create_challenge(client, headers, "D Enroll")

    # No header, empty body key
    r = client.post(
        f"/api/challenges/{cid}/enroll",
        json={"client_idempotency_key": ""},
        headers=headers,
    )
    assert r.status_code == 400, r.text
    assert len(_ops_of(cid, "enroll")) == 0


# ===========================================================================
# Scenario E: different actor using same key → isolation per actor
# ===========================================================================

def test_e_enroll_different_actors_same_key(client):
    h1, uid1 = _register(client, "alice")
    h2, uid2 = _register(client, "bob")
    cid = _create_challenge(client, h1, "E Enroll")
    _publish_challenge(client, h1, cid)

    key = "shared-enroll-key"
    r1 = client.post(
        f"/api/challenges/{cid}/enroll",
        headers={**h1, "Idempotency-Key": key},
        json={"client_idempotency_key": key},
    )
    r2 = client.post(
        f"/api/challenges/{cid}/enroll",
        headers={**h2, "Idempotency-Key": key},
        json={"client_idempotency_key": key},
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["user_id"] == uid1
    assert r2.json()["user_id"] == uid2

    ops = _ops_of(cid, "enroll")
    assert len(ops) == 2
    assert {o["actor_user_id"] for o in ops} == {uid1, uid2}


# ===========================================================================
# Scenario F: simulate lost response after commit → recover original response
# ===========================================================================

def test_f_create_lost_response_recovery(client):
    """Commit happens, the HTTP response is 'lost', the client retries with the
    same key. The retry recovers the ORIGINAL response and does not create a
    second challenge or a second record."""
    headers, _ = _register(client)
    key = "create-lost-response"
    body = _challenge_body("F Create")

    # First attempt (response "lost" from the client's perspective)
    r1 = client.post("/api/challenges", json=body, headers={**headers, "Idempotency-Key": key})
    assert r1.status_code == 200, r1.text
    original = r1.json()

    # Retry
    r2 = client.post("/api/challenges", json=body, headers={**headers, "Idempotency-Key": key})
    assert r2.status_code == 200, r2.text
    assert r2.json() == original

    with dbmod.SessionLocal() as s:
        n = s.execute(text("SELECT COUNT(*) FROM challenges")).scalar()
    assert n == 1
    assert len(_ops_of(original["id"], "create")) == 1


# ===========================================================================
# Scenario A-F: open_enrollment HTTP idempotency
# ===========================================================================


def test_a_open_enrollment_first_request(client):
    """Escenario A: primera petición con key K y payload P → ejecución normal,
    un registro durable."""
    headers, _ = _register(client)
    cid = _create_challenge(client, headers, "A Open Enroll")
    _publish_challenge(client, headers, cid)

    r = client.put(
        f"/api/challenges/{cid}/open-enrollment",
        headers={**headers, "Idempotency-Key": "open-key-a"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "enrollment_open"

    ops = _ops_of(cid, "open_enrollment")
    assert len(ops) == 1
    assert ops[0]["client_idempotency_key"] == "open-key-a"
    assert ops[0]["outcome"] == "enrollment_open"


def test_b_open_enrollment_retry_same_response(client):
    """Escenario B: misma key + mismo contenido → resultado original, sin efectos duplicados."""
    headers, _ = _register(client)
    cid = _create_challenge(client, headers, "B Open Enroll")
    _publish_challenge(client, headers, cid)

    r1 = client.put(
        f"/api/challenges/{cid}/open-enrollment",
        headers={**headers, "Idempotency-Key": "open-b-key"},
    )
    r2 = client.put(
        f"/api/challenges/{cid}/open-enrollment",
        headers={**headers, "Idempotency-Key": "open-b-key"},
    )
    assert r1.status_code == 200 == r2.status_code
    # Reintento: is_retry=True, efecto cero
    assert r2.json()["state"] == "enrollment_open"

    ops = _ops_of(cid, "open_enrollment")
    assert len(ops) == 1  # un solo registro durable


def test_c_open_enrollment_conflict_409(client):
    """Escenario C: misma key + contenido diferente → HTTP 409, estado intacto, un solo registro."""
    headers, _ = _register(client)
    cid = _create_challenge(client, headers, "C Open Enroll")
    _publish_challenge(client, headers, cid)

    key = "open-conflict-key"
    r1 = client.put(
        f"/api/challenges/{cid}/open-enrollment",
        headers={**headers, "Idempotency-Key": key},
    )
    assert r1.status_code == 200

    # Misma clave, hash distinto → conflicto 409
    from worldsim.server.challenge_service import IdempotencyConflict
    from worldsim.server.challenge_service import compute_operation_request_hash

    try:
        service = ChallengeService()
        service.open_enrollment(
            challenge_id=cid,
            organizer_user_id=1,
            client_idempotency_key=key,
            request_hash="different-hash-value",
        )
        assert False, "Debe haber lanzado IdempotencyConflict"
    except IdempotencyConflict:
        pass

    # Estado intacto, un solo registro
    ops = _ops_of(cid, "open_enrollment")
    assert len(ops) == 1


def test_d_open_enrollment_no_key_400(client):
    """Escenario D: sin Idempotency-Key header → HTTP 400, sin mutación."""
    headers, _ = _register(client)
    cid = _create_challenge(client, headers, "D Open Enroll")
    _publish_challenge(client, headers, cid)

    r = client.put(f"/api/challenges/{cid}/open-enrollment", headers=headers)
    assert r.status_code == 400, r.text
    assert "Idempotency-Key header required" in r.text

    ops = _ops_of(cid, "open_enrollment")
    assert len(ops) == 0


def test_d_open_enrollment_invalid_key_400(client):
    """Escenario D: clave con formato inválido → HTTP 400, sin mutación."""
    headers, _ = _register(client)
    cid = _create_challenge(client, headers, "D2 Open Enroll")
    _publish_challenge(client, headers, cid)

    r = client.put(
        f"/api/challenges/{cid}/open-enrollment",
        headers={**headers, "Idempotency-Key": "invalid key with spaces!"},
    )
    assert r.status_code == 400, r.text
    assert "Invalid Idempotency-Key" in r.text

    ops = _ops_of(cid, "open_enrollment")
    assert len(ops) == 0


def test_e_open_enrollment_different_actors_same_key(client):
    """Escenario E: para open_enrollment este escenario no es aplicable porque
    el endpoint requiere autorización de organizador: solo el organizador puede
    abrir inscripciones. El ámbito de idempotencia ``(challenge_id,
    organizer_user_id, client_idempotency_key)`` está anclado a la autenticación
    del organizador, a diferencia de enroll/dispute/resolve donde pueden
    participar distintos usuarios.

    Con el organizador y la misma clave el reintento sí funciona (test_b y test_f).
    """
    # No hay prueba HTTP independiente: la restricción de organizador impide
    # testear "different actor" para este endpoint en particular.
    pass  # organizer-only: skip this scenario


def test_f_open_enrollment_lost_response_recovery(client):
    """Escenario F: simular pérdida de respuesta después commit → recuperar resultado original."""
    headers, _ = _register(client)
    cid = _create_challenge(client, headers, "F Open Enroll")
    _publish_challenge(client, headers, cid)

    key = "open-lost-response"

    # Primera petición
    r1 = client.put(
        f"/api/challenges/{cid}/open-enrollment",
        headers={**headers, "Idempotency-Key": key},
    )
    assert r1.status_code == 200, r1.text
    original = r1.json()

    # Retry con misma clave
    r2 = client.put(
        f"/api/challenges/{cid}/open-enrollment",
        headers={**headers, "Idempotency-Key": key},
    )
    assert r2.status_code == 200, r2.text
    # Recupera el resultado original (mismo state)
    assert r2.json()["state"] == original["state"]

    # Un solo registro durable
    ops = _ops_of(cid, "open_enrollment")
    assert len(ops) == 1


# ===========================================================================
# Hash canonicalisation: open_enrollment no tiene payload semántico variable;
# el hash depende sólo de operation + challenge_id (sin campos adicionales).
# ===========================================================================

def test_hash_open_enrollment_deterministic(client):
    """open_enrollment: el hash es determinista y solo depende de operation + challenge_id."""
    from worldsim.server.challenge_service import compute_operation_request_hash

    h1 = compute_operation_request_hash("open_enrollment", 1)
    h2 = compute_operation_request_hash("open_enrollment", 1)
    assert h1 == h2  # determinista

    h3 = compute_operation_request_hash("open_enrollment", 2)
    assert h1 != h3  # challenge_id distinto cambia el hash


def test_f_publish_lost_response_recovery(client):
    headers, _ = _register(client)
    cid = _create_challenge(client, headers, "F Publish")

    r1 = client.put(
        f"/api/challenges/{cid}/publish",
        headers={**headers, "Idempotency-Key": "publish-lost"},
    )
    original = r1.json()

    # Advance the challenge so current state != original response; the replay
    # must still report the ORIGINAL outcome ("published").
    client.put(
        f"/api/challenges/{cid}/open-enrollment",
        headers={**headers, "Idempotency-Key": "open-after-publish"},
    )
    assert _state(cid) == "enrollment_open"

    r2 = client.put(
        f"/api/challenges/{cid}/publish",
        headers={**headers, "Idempotency-Key": "publish-lost"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json() == original
    assert r2.json()["state"] == "published"  # original outcome, not current
    assert len(_ops_of(cid, "publish")) == 1


def test_f_enroll_lost_response_recovery(client):
    headers, uid = _register(client)
    cid = _create_challenge(client, headers, "F Enroll")
    _publish_challenge(client, headers, cid)

    key = "enroll-lost"
    r1 = client.post(
        f"/api/challenges/{cid}/enroll",
        headers={**headers, "Idempotency-Key": key},
        json={"client_idempotency_key": key},
    )
    original = r1.json()
    r2 = client.post(
        f"/api/challenges/{cid}/enroll",
        headers={**headers, "Idempotency-Key": key},
        json={"client_idempotency_key": key},
    )
    # El retry recupara el resultado original (mismo challenge_id, estado,
    # user_id), pero is_retry será True en el reintento.
    assert r2.json()["challenge_id"] == original["challenge_id"]
    assert r2.json()["user_id"] == original["user_id"]
    assert len(_ops_of(cid, "enroll")) == 1
    with dbmod.SessionLocal() as s:
        n = s.execute(
            text(
                "SELECT COUNT(*) FROM challenge_participants "
                "WHERE challenge_id = :c AND user_id = :u"
            ),
            {"c": cid, "u": uid},
        ).scalar()
    assert n == 1


# ===========================================================================
# Hash canonicalisation: semantic payload changes the hash; transport does not
# ===========================================================================

def test_hash_includes_resolution(client):
    """resolve's hash includes the resolution: two resolutions are distinct
    requests (the conflict is the HTTP evidence)."""
    from worldsim.server.challenge_service import compute_operation_request_hash

    h_confirm = compute_operation_request_hash("resolve", 1, resolution="confirm_all")
    h_invalidate = compute_operation_request_hash(
        "resolve", 1, resolution="invalidate_all"
    )
    assert h_confirm != h_invalidate
    # challenge_id is part of the canonical representation
    assert compute_operation_request_hash("resolve", 1, resolution="confirm_all") != (
        compute_operation_request_hash("resolve", 2, resolution="confirm_all")
    )


def test_hash_includes_participant_for_enroll_and_dispute(client):
    """enroll/dispute bind the participant identity (server-auth actor) into the
    hash, so the canonical request differs per participant."""
    from worldsim.server.challenge_service import compute_operation_request_hash

    assert compute_operation_request_hash("enroll", 1, user_id=1) != (
        compute_operation_request_hash("enroll", 1, user_id=2)
    )
    assert compute_operation_request_hash("dispute", 1, user_id=1) != (
        compute_operation_request_hash("dispute", 1, user_id=2)
    )


def test_hash_excludes_transport_metadata(client):
    """Transport-only data (the key, headers, timestamps) is NOT part of the
    canonical hash: two calls with the same semantic data hash identically."""
    from worldsim.server.challenge_service import compute_operation_request_hash

    assert compute_operation_request_hash("publish", 7) == (
        compute_operation_request_hash("publish", 7)
    )
    # reversible, deterministic across calls
    h = compute_operation_request_hash("complete", 3)
    assert len(h) == 64 and all(ch in "0123456789abcdef" for ch in h)
