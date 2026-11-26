from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text

import worldsim.server.db as dbmod


@pytest.fixture
def postgres_legacy_f7d_engine():
    """Disposable PostgreSQL schema containing the inspected legacy F7-D shape."""
    database_url = os.environ.get("EVOWORLD_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("EVOWORLD_TEST_POSTGRES_URL is not configured")

    pytest.importorskip("psycopg")

    schema = f"evoworld_f7d_i1c_{uuid.uuid4().hex}"
    admin_engine = create_engine(database_url)
    with admin_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )

    try:
        # Create the surrounding application schema, then replace only the
        # F7-D family with the legacy structures observed during preflight.
        dbmod.Base.metadata.create_all(engine)

        with engine.begin() as conn:
            conn.execute(text('DROP TABLE "challenge_operations"'))
            conn.execute(text('DROP TABLE "challenge_participants"'))
            conn.execute(text('DROP TABLE "challenge_creation_requests"'))
            conn.execute(text('DROP TABLE "challenges"'))

            conn.execute(text("""
                CREATE TABLE challenges (
                    id INTEGER PRIMARY KEY,
                    organizer_user_id INTEGER NOT NULL,
                    name VARCHAR NOT NULL,
                    config_json TEXT NOT NULL,
                    rules_version VARCHAR NOT NULL,
                    duration_ticks INTEGER NOT NULL,
                    state VARCHAR,
                    created_at VARCHAR NOT NULL,
                    client_idempotency_key VARCHAR NOT NULL,
                    request_hash VARCHAR NOT NULL
                )
            """))
            conn.execute(text("""
                CREATE TABLE challenge_participants (
                    id INTEGER PRIMARY KEY,
                    challenge_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    world_id INTEGER NOT NULL,
                    public_alias VARCHAR NOT NULL,
                    result_json TEXT NOT NULL,
                    result_status VARCHAR,
                    result_verified INTEGER,
                    state VARCHAR
                )
            """))
            conn.execute(text("""
                CREATE TABLE challenge_operations (
                    id INTEGER PRIMARY KEY,
                    challenge_id INTEGER NOT NULL,
                    operation_id VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    continuity_state VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL,
                    requesting_user_id INTEGER NOT NULL,
                    client_idempotency_key VARCHAR NOT NULL,
                    request_hash VARCHAR NOT NULL,
                    engine_version VARCHAR,
                    checkpoint_id INTEGER NOT NULL,
                    recovery_generation INTEGER
                )
            """))

        yield engine
    finally:
        engine.dispose()
        with admin_engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


def test_postgresql_i1c_rebuilds_empty_legacy_f7d_tables_and_is_idempotent(
    postgres_legacy_f7d_engine,
):
    engine = postgres_legacy_f7d_engine

    dbmod.ensure_schema(engine)

    expected = {
        "challenges": {
            "id", "organizer_user_id", "name", "config_json", "rules_version",
            "duration_ticks", "state", "created_at", "closed_at", "completed_at",
        },
        "challenge_participants": {
            "id", "challenge_id", "user_id", "world_id", "public_alias", "state",
            "joined_at", "result_json", "result_status", "result_verified",
        },
        "challenge_operations": {
            "id", "challenge_id", "operation", "actor_user_id",
            "client_idempotency_key", "request_hash", "outcome", "created_at",
        },
        "challenge_creation_requests": {
            "id", "actor_user_id", "client_idempotency_key", "request_hash",
            "challenge_id", "created_at",
        },
    }

    inspector = inspect(engine)
    for table, columns in expected.items():
        assert {
            column["name"] for column in inspector.get_columns(table)
        } == columns

    participant_columns = {
        column["name"]: column
        for column in inspector.get_columns("challenge_participants")
    }
    assert participant_columns["world_id"]["nullable"] is True
    assert participant_columns["result_json"]["nullable"] is True
    assert participant_columns["result_status"]["nullable"] is True

    # This helper checks the full migration contract: nullability, CHECKs,
    # UNIQUE constraints and foreign keys, not only the column names.
    with engine.connect() as conn:
        for table in expected:
            assert dbmod._f7d_schema_is_current(conn, table)

    # A second startup must preserve the rebuilt PostgreSQL schema.
    dbmod.ensure_schema(engine)
    with engine.connect() as conn:
        for table in expected:
            assert dbmod._f7d_schema_is_current(conn, table)


def test_postgresql_i1c_refuses_legacy_f7d_rows_and_rolls_back(
    postgres_legacy_f7d_engine,
):
    engine = postgres_legacy_f7d_engine

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO challenges (
                id, organizer_user_id, name, config_json, rules_version,
                duration_ticks, state, created_at, client_idempotency_key,
                request_hash
            ) VALUES (
                7, 1, 'legacy', '{}', '1.0', 10, 'draft', '2026-01-01',
                'key-1', 'hash-1'
            )
        """))

    with pytest.raises(RuntimeError, match="historical rows exist"):
        dbmod.ensure_schema(engine)

    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM challenges")
        ).scalar_one() == 1
        assert conn.execute(text(
            "SELECT client_idempotency_key, request_hash "
            "FROM challenges WHERE id = 7"
        )).one() == ("key-1", "hash-1")
        assert "closed_at" not in {
            column["name"] for column in inspect(conn).get_columns("challenges")
        }
        assert "challenge_creation_requests" not in inspect(conn).get_table_names()
