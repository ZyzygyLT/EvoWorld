from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import worldsim.server.db as dbmod
from worldsim.server.challenge_service import (
    ChallengeService,
    IdempotencyConflict,
)


@pytest.fixture
def postgres_creation_engine():
    """Esquema PostgreSQL aislado para carreras de creación F7-D."""
    database_url = os.environ.get("EVOWORLD_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("EVOWORLD_TEST_POSTGRES_URL is not configured")

    pytest.importorskip("psycopg")
    schema = f"evoworld_f7d_create_{uuid.uuid4().hex}"
    admin_engine = create_engine(database_url)
    with admin_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        dbmod.Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, username, pw_hash, created_at) "
                    "VALUES (1, 'race_user', 'hash', '2026-01-01')"
                )
            )
        yield engine
    finally:
        engine.dispose()
        with admin_engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


def _race_create(engine, name: str, barrier: Barrier):
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    service = ChallengeService(session_factory=factory)
    barrier.wait(timeout=10)
    return service.create_challenge(
        organizer_user_id=1,
        name=name,
        config_json='{"seed":42,"primary_metric":"total_population"}',
        rules_version="1.0",
        duration_ticks=10,
        client_idempotency_key="concurrent-create-key",
    )


def test_postgresql_concurrent_same_creation_request_returns_one_challenge(
    postgres_creation_engine,
):
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_race_create, postgres_creation_engine, "Same request", barrier)
            for _ in range(2)
        ]
        results = [future.result(timeout=30) for future in futures]

    assert results[0].challenge.id == results[1].challenge.id
    assert sum(result.is_retry for result in results) == 1

    with postgres_creation_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM challenges")).scalar_one() == 1
        assert conn.execute(
            text("SELECT COUNT(*) FROM challenge_creation_requests")
        ).scalar_one() == 1
        assert conn.execute(
            text("SELECT COUNT(*) FROM challenge_operations WHERE operation = 'create'")
        ).scalar_one() == 1


def test_postgresql_concurrent_different_payload_same_key_conflicts(
    postgres_creation_engine,
):
    barrier = Barrier(2)

    def run(name: str):
        try:
            return _race_create(postgres_creation_engine, name, barrier)
        except IdempotencyConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(run, "Payload A"),
            pool.submit(run, "Payload B"),
        ]
        results = [future.result(timeout=30) for future in futures]

    assert sum(result == "conflict" for result in results) == 1
    assert sum(result != "conflict" for result in results) == 1

    with postgres_creation_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM challenges")).scalar_one() == 1
        assert conn.execute(
            text("SELECT COUNT(*) FROM challenge_creation_requests")
        ).scalar_one() == 1
        assert conn.execute(
            text("SELECT COUNT(*) FROM challenge_operations WHERE operation = 'create'")
        ).scalar_one() == 1
