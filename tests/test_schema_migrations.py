"""F7-C schema migration tests against isolated SQLite databases.

The fixture mirrors the inspected pre-F7-C schema, including the already
existing F7-E world columns and checkpoint uniqueness. No persistent database
is opened by these tests.
"""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import IntegrityError


def _build_pre_f7c_db(path) -> None:
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username VARCHAR UNIQUE,
            pw_hash VARCHAR,
            created_at VARCHAR
        );
        INSERT INTO users VALUES (7, 'admin', 'hash', '2026-01-01');
        CREATE TABLE worlds (
            id INTEGER PRIMARY KEY,
            seed INTEGER, width INTEGER, height INTEGER,
            months_simulated INTEGER, land_pct FLOAT, state_json TEXT,
            created_at VARCHAR, name VARCHAR, mode VARCHAR,
            season_years INTEGER, season_over INTEGER,
            enabled_races_json VARCHAR, owner_user_id INTEGER,
            sea_level FLOAT, status VARCHAR(20) NOT NULL,
            last_checkpoint_at VARCHAR, checkpoint_count INTEGER NOT NULL,
            checkpoint_version INTEGER,
            visibility VARCHAR(7) DEFAULT 'private'
                CHECK (visibility IN ('private','public')),
            deleted_at DATETIME,
            FOREIGN KEY(owner_user_id) REFERENCES users(id)
        );
        INSERT INTO worlds (
            id, seed, width, height, months_simulated, state_json,
            created_at, name, mode, owner_user_id, status,
            checkpoint_count, visibility
        ) VALUES (1, 42, 8, 8, 0, '{}', 't', 'legacy-world',
                  'single', 7, 'running', 0, 'private');

        CREATE TABLE world_checkpoints (
            id INTEGER PRIMARY KEY,
            world_id INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            month INTEGER NOT NULL,
            checkpoint_type VARCHAR(20) NOT NULL,
            payload BLOB NOT NULL,
            checksum VARCHAR(64) NOT NULL,
            created_at VARCHAR NOT NULL,
            is_valid INTEGER NOT NULL,
            checkpoint_version INTEGER NOT NULL,
            CONSTRAINT uq_world_checkpoint_seq UNIQUE (world_id, sequence),
            FOREIGN KEY(world_id) REFERENCES worlds(id) ON DELETE CASCADE
        );
        CREATE INDEX ix_world_checkpoints_world_seq
            ON world_checkpoints(world_id, sequence);
        CREATE INDEX ix_world_checkpoints_world_id
            ON world_checkpoints(world_id);
        CREATE INDEX ix_world_checkpoints_world_month
            ON world_checkpoints(world_id, month);

        -- Legacy F7-C tables, exactly in the inspected old shape.
        CREATE TABLE world_operations (
            id INTEGER NOT NULL PRIMARY KEY,
            world_id INTEGER NOT NULL,
            operation_sequence INTEGER NOT NULL,
            status VARCHAR NOT NULL,
            continuity_state VARCHAR NOT NULL,
            created_at VARCHAR NOT NULL,
            requesting_user_id INTEGER NOT NULL,
            client_idempotency_key VARCHAR NOT NULL,
            request_hash VARCHAR NOT NULL,
            engine_version VARCHAR,
            checkpoint_id INTEGER NOT NULL
        );
        CREATE TABLE world_events (
            id INTEGER NOT NULL PRIMARY KEY,
            world_id INTEGER NOT NULL,
            operation_sequence INTEGER NOT NULL,
            position INTEGER NOT NULL,
            month INTEGER NOT NULL,
            event_type VARCHAR NOT NULL,
            event_text VARCHAR NOT NULL,
            kingdom_ids TEXT NOT NULL,
            event_params TEXT NOT NULL,
            msg_key VARCHAR NOT NULL,
            created_at VARCHAR NOT NULL
        );
        CREATE TABLE operation_resolutions (
            id INTEGER NOT NULL PRIMARY KEY,
            world_id INTEGER NOT NULL,
            operation_id VARCHAR NOT NULL,
            resolution VARCHAR NOT NULL,
            resolved_at VARCHAR NOT NULL,
            resolution_version INTEGER
        );
        CREATE INDEX ix_worlds_seed ON worlds(seed);
        """
    )
    raw.commit()
    raw.close()


@pytest.fixture
def migrated(tmp_path):
    db_file = tmp_path / "legacy.db"
    _build_pre_f7c_db(db_file)
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    import worldsim.server.db as dbmod

    dbmod.ensure_schema(engine)
    yield engine
    engine.dispose()


def test_migration_replaces_empty_legacy_tables_and_adds_counter(migrated):
    insp = inspect(migrated)
    assert {"operation_id", "recovery_generation", "initial_checkpoint_id"} <= {
        c["name"] for c in insp.get_columns("world_operations")
    }
    assert {"operation_id"} <= {c["name"] for c in insp.get_columns("world_events")}
    assert {"admin_user_id", "resolution_type", "idempotency_key"} <= {
        c["name"] for c in insp.get_columns("operation_resolutions")
    }
    counter = next(c for c in insp.get_columns("worlds") if c["name"] == "next_operation_sequence")
    assert counter["nullable"] is False
    assert str(counter["default"]) in ("1", "'1'")
    with migrated.connect() as conn:
        assert conn.execute(text("SELECT next_operation_sequence FROM worlds WHERE id=1")).scalar_one() == 1
        assert conn.execute(text("SELECT name FROM worlds WHERE id=1")).scalar_one() == "legacy-world"


def test_migration_is_idempotent(migrated):
    import worldsim.server.db as dbmod

    with migrated.connect() as conn:
        before = conn.execute(text(
            "SELECT id, name, next_operation_sequence FROM worlds ORDER BY id"
        )).fetchall()
        ddl_before = {
            table: conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"
            ), {"name": table}).scalar_one()
            for table in ("world_operations", "world_events", "operation_resolutions")
        }
    dbmod.ensure_schema(migrated)
    with migrated.connect() as conn:
        after = conn.execute(text(
            "SELECT id, name, next_operation_sequence FROM worlds ORDER BY id"
        )).fetchall()
        ddl_after = {
            table: conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"
            ), {"name": table}).scalar_one()
            for table in ("world_operations", "world_events", "operation_resolutions")
        }
    assert after == before
    assert ddl_after == ddl_before


def test_migration_preserves_worlds_and_checkpoint_schema(migrated):
    with migrated.connect() as conn:
        world_ddl = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='worlds'"
        )).scalar_one()
        assert "visibility" in world_ddl
        assert "deleted_at" in world_ddl
        assert "ix_worlds_seed" in {
            row[1] for row in conn.execute(text("PRAGMA index_list(worlds)"))
        }
        checkpoint_ddl = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='world_checkpoints'"
        )).scalar_one()
        assert "uq_world_checkpoint_seq" in checkpoint_ddl
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        assert conn.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"


def test_new_operation_constraints_and_foreign_keys_are_enforced(migrated):
    valid = (
        "INSERT INTO world_operations (world_id, operation_id, operation_sequence, "
        "status, continuity_state, created_at, requesting_user_id, "
        "client_idempotency_key, request_hash, recovery_generation) "
        "VALUES (1, 'op-1', 1, 'pending', 'active', 't', 7, 'key-1', 'hash', 0)"
    )
    with migrated.begin() as conn:
        conn.execute(text(valid))
    with migrated.begin() as conn:
        with pytest.raises(Exception, match="CHECK"):
            conn.execute(text(
                "INSERT INTO world_operations (world_id, operation_id, operation_sequence, "
                "status, continuity_state, created_at, requesting_user_id, "
                "client_idempotency_key, request_hash) "
                "VALUES (1, 'bad', 2, 'failed', 'active', 't', 7, 'key-2', 'hash')"
            ))
    with migrated.begin() as conn:
        with pytest.raises(Exception, match="UNIQUE"):
            conn.execute(text(
                "INSERT INTO world_operations (world_id, operation_id, operation_sequence, "
                "status, continuity_state, created_at, requesting_user_id, "
                "client_idempotency_key, request_hash) "
                "VALUES (1, 'op-duplicate-seq', 1, 'pending', 'active', 't', 7, 'key-3', 'hash')"
            ))
    with migrated.begin() as conn:
        with pytest.raises(Exception, match="FOREIGN KEY"):
            conn.execute(text(
                "INSERT INTO world_operations (world_id, operation_id, operation_sequence, "
                "status, continuity_state, created_at, requesting_user_id, "
                "client_idempotency_key, request_hash) "
                "VALUES (999, 'orphan', 1, 'pending', 'active', 't', 7, 'key-4', 'hash')"
            ))


def test_event_and_resolution_composite_foreign_keys(migrated):
    with migrated.begin() as conn:
        conn.execute(text(
            "INSERT INTO world_operations (world_id, operation_id, operation_sequence, "
            "status, continuity_state, created_at, requesting_user_id, "
            "client_idempotency_key, request_hash) "
            "VALUES (1, 'op-1', 1, 'confirmed', 'active', 't', 7, 'key-1', 'hash')"
        ))
        conn.execute(text(
            "INSERT INTO world_events (world_id, operation_id, operation_sequence, position, "
            "month, event_type, event_text, kingdom_ids, event_params, msg_key, created_at) "
            "VALUES (1, 'op-1', 1, 0, 1, 'birth', 'text', '[]', '{}', 'msg', 't')"
        ))
        conn.execute(text(
            "INSERT INTO operation_resolutions (world_id, operation_id, operation_sequence, "
            "admin_user_id, resolution_type, reason, evidence_refs, resolution_version, "
            "created_at, idempotency_key) "
            "VALUES (1, 'op-1', 1, 7, 'remain_blocked', 'reason', '[]', 1, 't', 'res-key')"
        ))
    with migrated.begin() as conn:
        with pytest.raises(Exception, match="FOREIGN KEY"):
            conn.execute(text(
                "INSERT INTO world_events (world_id, operation_id, operation_sequence, position, "
                "month, event_type, event_text, kingdom_ids, event_params, msg_key, created_at) "
                "VALUES (1, 'missing', 2, 0, 1, 'birth', 'text', '[]', '{}', 'msg', 't')"
            ))
    with migrated.begin() as conn:
        with pytest.raises(Exception, match="CHECK"):
            conn.execute(text(
                "INSERT INTO world_events (world_id, operation_id, operation_sequence, position, "
                "month, event_type, event_text, kingdom_ids, event_params, msg_key, created_at) "
                "VALUES (1, 'op-1', 1, -1, 1, 'birth', 'text', '[]', '{}', 'msg', 't')"
            ))


@pytest.mark.parametrize("table,insert_sql", [
    ("world_operations", "INSERT INTO world_operations VALUES (1,1,1,'pending','active','t',7,'k','h','1.0',0)"),
    ("world_events", "INSERT INTO world_events VALUES (1,1,1,0,1,'birth','x','[]','{}','m','t')"),
    ("operation_resolutions", "INSERT INTO operation_resolutions VALUES (1,1,'op','legacy','t',1)"),
])
def test_nonempty_legacy_table_aborts_without_modifying_schema(tmp_path, table, insert_sql):
    db_file = tmp_path / f"nonempty-{table}.db"
    _build_pre_f7c_db(db_file)
    raw = sqlite3.connect(db_file)
    raw.execute(insert_sql)
    raw.commit()
    before = raw.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]
    raw.close()

    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    import worldsim.server.db as dbmod

    with pytest.raises(RuntimeError, match="historical rows exist"):
        dbmod.ensure_schema(engine)
    with engine.connect() as conn:
        after = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"
        ), {"name": table}).scalar_one()
        assert after == before
        assert conn.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one() == 1
        assert "next_operation_sequence" not in {
            c["name"] for c in inspect(conn).get_columns("worlds")
        }
    engine.dispose()


def test_stale_counter_is_repaired_without_renumbering_operations(migrated):
    with migrated.begin() as conn:
        conn.execute(text(
            "INSERT INTO world_operations (world_id, operation_id, operation_sequence, "
            "status, continuity_state, created_at, requesting_user_id, "
            "client_idempotency_key, request_hash) "
            "VALUES (1, 'op-a', 4, 'confirmed', 'active', 't', 7, 'a', 'h'), "
            "(1, 'op-b', 6, 'confirmed', 'active', 't', 7, 'b', 'h')"
        ))
        conn.execute(text("UPDATE worlds SET next_operation_sequence=1 WHERE id=1"))
    import worldsim.server.db as dbmod

    dbmod.ensure_schema(migrated)
    with migrated.connect() as conn:
        assert conn.execute(text(
            "SELECT next_operation_sequence FROM worlds WHERE id=1"
        )).scalar_one() == 7
        assert conn.execute(text(
            "SELECT operation_sequence FROM world_operations ORDER BY operation_sequence"
        )).fetchall() == [(4,), (6,)]


def test_healthy_counter_is_never_rewound(migrated):
    with migrated.begin() as conn:
        conn.execute(text(
            "INSERT INTO world_operations (world_id, operation_id, operation_sequence, "
            "status, continuity_state, created_at, requesting_user_id, "
            "client_idempotency_key, request_hash) VALUES "
            "(1, 'healthy-1', 1, 'confirmed', 'active', 't', 7, 'h1', 'hash1'), "
            "(1, 'healthy-2', 2, 'confirmed', 'active', 't', 7, 'h2', 'hash2')"
        ))
        conn.execute(text(
            "UPDATE worlds SET next_operation_sequence=10 WHERE id=1"
        ))

    import worldsim.server.db as dbmod
    dbmod.ensure_schema(migrated)

    with migrated.connect() as conn:
        assert conn.execute(text(
            "SELECT next_operation_sequence FROM worlds WHERE id=1"
        )).scalar_one() == 10


def test_counter_repair_is_independent_per_world(migrated):
    with migrated.begin() as conn:
        conn.execute(text(
            "INSERT INTO worlds (id, seed, width, height, months_simulated, "
            "state_json, created_at, name, mode, status, checkpoint_count, "
            "visibility, next_operation_sequence) "
            "VALUES (2, 43, 8, 8, 0, '{}', 't', 'second-world', "
            "'single', 'running', 0, 'private', 7)"
        ))
        conn.execute(text(
            "INSERT INTO world_operations (world_id, operation_id, operation_sequence, "
            "status, continuity_state, created_at, requesting_user_id, "
            "client_idempotency_key, request_hash) VALUES "
            "(1, 'world1-op4', 4, 'confirmed', 'active', 't', 7, 'w1k4', 'hash4'), "
            "(1, 'world1-op6', 6, 'confirmed', 'active', 't', 7, 'w1k6', 'hash6')"
        ))
        conn.execute(text(
            "UPDATE worlds SET next_operation_sequence=1 WHERE id=1"
        ))

    import worldsim.server.db as dbmod
    dbmod.ensure_schema(migrated)

    with migrated.connect() as conn:
        counters = conn.execute(text(
            "SELECT id, next_operation_sequence FROM worlds ORDER BY id"
        )).fetchall()

    assert counters == [(1, 7), (2, 7)]


def test_t1_allocates_sequence_after_schema_migration(migrated):
    from sqlalchemy.orm import sessionmaker
    from worldsim.server.operation_service import OperationParams, OperationService

    Session = sessionmaker(bind=migrated)
    service = OperationService(Session, engine_version="1.0")
    params = OperationParams(
        world_id=1,
        operation_type="advance",
        months=1,
    )

    result = service.reserve_operation(1, 7, "migration-key", params)

    assert result.operation.operation_sequence == 1
    assert result.operation.status == "pending"

    with Session() as session:
        counter = session.execute(text(
            "SELECT next_operation_sequence FROM worlds WHERE id=1"
        )).scalar_one()

    assert counter == 2


def test_migrated_counter_rejects_explicit_null(migrated):
    with pytest.raises(IntegrityError, match="NOT NULL"):
        with migrated.begin() as conn:
            conn.execute(text(
                "INSERT INTO worlds (id, status, checkpoint_count, "
                "next_operation_sequence) VALUES (2, 'running', 0, NULL)"
            ))
