from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from worldsim.server.db import ensure_schema


def _legacy_f7d_schema(engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql("""
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
        """)
        conn.exec_driver_sql("""
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
        """)
        conn.exec_driver_sql("""
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
        """)


def test_i1c_rebuilds_empty_legacy_f7d_tables_and_is_idempotent():
    engine = create_engine("sqlite:///:memory:")
    _legacy_f7d_schema(engine)

    ensure_schema(engine)

    inspector = inspect(engine)
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
    for table, columns in expected.items():
        assert {column["name"] for column in inspector.get_columns(table)} == columns

    participant_columns = {
        column["name"]: column
        for column in inspector.get_columns("challenge_participants")
    }
    assert participant_columns["world_id"]["nullable"] is True
    assert participant_columns["result_json"]["nullable"] is True
    assert participant_columns["result_status"]["nullable"] is True
    assert "client_idempotency_key" not in {
        column["name"] for column in inspector.get_columns("challenges")
    }

    # A second startup must preserve the now-current schema.
    ensure_schema(engine)
    assert {column["name"] for column in inspect(engine).get_columns("challenges")} == expected["challenges"]
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    engine.dispose()


def test_i1c_refuses_to_rebuild_if_any_legacy_f7d_rows_exist():
    engine = create_engine("sqlite:///:memory:")
    _legacy_f7d_schema(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("""
            INSERT INTO challenges
                (id, organizer_user_id, name, config_json, rules_version,
                 duration_ticks, state, created_at, client_idempotency_key,
                 request_hash)
            VALUES (7, 1, 'legacy', '{}', '1.0', 10, 'draft', '2026-01-01',
                    'key-1', 'hash-1')
        """)

    with pytest.raises(RuntimeError, match="historical rows exist"):
        ensure_schema(engine)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM challenges")).scalar_one() == 1
        assert conn.execute(
            text("SELECT client_idempotency_key FROM challenges WHERE id = 7")
        ).scalar_one() == "key-1"
        assert "challenge_creation_requests" not in inspect(conn).get_table_names()
    engine.dispose()
