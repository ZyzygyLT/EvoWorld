"""F7-C: schema creation and migration of a pre-F7-C database
(CONTRACTS §16.2, §16.10).

Covers: ``ensure_schema`` on an old DB — F7-C tables land, the
``next_operation_sequence`` column is added and backfilled, FK and CHECK
constraints are ENFORCED on the migrated engine, the broken-allocator
repair (NULL or ≤ MAX) fixes the COUNTER ONLY without renumbering any
operation, and a second run is idempotent.

All databases are isolated temp files; the persistent ``worlds.db`` is
never opened.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, inspect, text


def _build_pre_f7c_db(path) -> None:
    """Hand-build the schema AS IT EXISTED BEFORE F7-C: no F7-C tables and
    no ``worlds.next_operation_sequence`` column."""
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT UNIQUE,
            pw_hash TEXT, created_at TEXT
        );
        CREATE TABLE worlds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seed INTEGER, width INTEGER, height INTEGER,
            months_simulated INTEGER, land_pct FLOAT,
            state_json TEXT, created_at TEXT, name TEXT,
            mode TEXT, season_years INTEGER, season_over INTEGER,
            enabled_races_json TEXT, owner_user_id INTEGER,
            sea_level FLOAT, status TEXT,
            last_checkpoint_at TEXT, checkpoint_count INTEGER
        );
        INSERT INTO worlds (
            seed, width, height, months_simulated, state_json, created_at,
            name, mode, status, checkpoint_count
        ) VALUES (1, 100, 50, 3, '{}', '2026-01-01', 'W', 'single',
                  'running', 0);
        INSERT INTO users (id, username, pw_hash, created_at)
        VALUES (7, 'adm', 'x', '2026-01-01');
        """
    )
    raw.commit()
    raw.close()


def _migrated_engine(tmp_path, name="old.db"):
    db_file = tmp_path / name
    _build_pre_f7c_db(db_file)
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    import worldsim.server.db as dbmod

    dbmod.ensure_schema(engine)  # run 1
    dbmod.ensure_schema(engine)  # run 2 must be idempotent
    return engine


# ---------------------------------------------------------------------------
# Tables + columns
# ---------------------------------------------------------------------------


def test_migration_creates_f7c_tables_and_column(tmp_path):
    engine = _migrated_engine(tmp_path)
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert {"world_operations", "world_events", "operation_resolutions"} <= tables
    cols = {c["name"] for c in insp.get_columns("worlds")}
    assert "next_operation_sequence" in cols


def test_migration_backfills_null_counter_to_one(tmp_path):
    engine = _migrated_engine(tmp_path)
    with engine.connect() as conn:
        value = conn.execute(
            text("SELECT next_operation_sequence FROM worlds WHERE id = 1")
        ).scalar()
    assert value == 1  # first T1 reserves sequence 1


def test_migration_run_is_idempotent(tmp_path):
    """Running ensure_schema again changes nothing (no counter drift, no
    column re-add, no data rewrite)."""
    import worldsim.server.db as dbmod

    engine = _migrated_engine(tmp_path, "idem.db")
    with engine.connect() as conn:
        before = conn.execute(
            text(
                "SELECT id, seed, name, next_operation_sequence "
                "FROM worlds ORDER BY id"
            )
        ).fetchall()
    dbmod.ensure_schema(engine)
    dbmod.ensure_schema(engine)
    with engine.connect() as conn:
        after = conn.execute(
            text(
                "SELECT id, seed, name, next_operation_sequence "
                "FROM worlds ORDER BY id"
            )
        ).fetchall()
    assert after == before


# ---------------------------------------------------------------------------
# Constraints enforced on the MIGRATED engine
# ---------------------------------------------------------------------------


def test_migrated_engine_enforces_foreign_keys(tmp_path):
    """The FK PRAGMA listener must be registered for the EXPLICIT engine
    passed to ensure_schema, not only for the module default."""
    engine = _migrated_engine(tmp_path, "fk.db")
    with engine.begin() as conn:
        with pytest.raises(Exception, match="FOREIGN KEY"):
            conn.execute(
                text(
                    "INSERT INTO world_operations ("
                    "world_id, operation_id, operation_sequence, status, "
                    "continuity_state, created_at, requesting_user_id, "
                    "client_idempotency_key, request_hash, engine_version, "
                    "recovery_generation"
                    ") VALUES (9999, 'x', 1, 'pending', 'active', 't', 1, "
                    "'k', 'h', '1.0', 0)"
                )
            )


def test_migrated_engine_enforces_check_constraints(tmp_path):
    engine = _migrated_engine(tmp_path, "check.db")
    with engine.begin() as conn:
        # Invalid status value.
        with pytest.raises(Exception, match="CHECK"):
            conn.execute(
                text(
                    "INSERT INTO world_operations ("
                    "world_id, operation_id, operation_sequence, status, "
                    "continuity_state, created_at, requesting_user_id, "
                    "client_idempotency_key, request_hash, engine_version, "
                    "recovery_generation"
                    ") VALUES (1, 'y', 1, 'bogus', 'active', 't', 1, "
                    "'k2', 'h', '1.0', 0)"
                )
            )
    with engine.begin() as conn:
        # Invalid status+continuity combination (failed+active).
        with pytest.raises(Exception, match="CHECK"):
            conn.execute(
                text(
                    "INSERT INTO world_operations ("
                    "world_id, operation_id, operation_sequence, status, "
                    "continuity_state, created_at, requesting_user_id, "
                    "client_idempotency_key, request_hash, engine_version, "
                    "recovery_generation"
                    ") VALUES (1, 'z', 2, 'failed', 'active', 't', 1, "
                    "'k3', 'h', '1.0', 0)"
                )
            )


# ---------------------------------------------------------------------------
# Allocator repair — counter only, never the operations
# ---------------------------------------------------------------------------

_OP_INSERT = (
    "INSERT INTO world_operations ("
    "world_id, operation_id, operation_sequence, status, continuity_state, "
    "created_at, requesting_user_id, client_idempotency_key, request_hash, "
    "engine_version, recovery_generation"
    ") VALUES (:w, :oid, :seq, :status, :continuity, 't', 1, :key, 'h', "
    "'1.0', 0)"
)


def _seed_ops(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(_OP_INSERT),
            {"w": 1, "oid": "op-a", "seq": 1, "status": "confirmed",
             "continuity": "active", "key": "ka"},
        )
        conn.execute(
            text(_OP_INSERT),
            {"w": 1, "oid": "op-b", "seq": 2, "status": "canceled",
             "continuity": "released", "key": "kb"},
        )
        # Simulate the V1 defect: a counter that lags behind MAX(seq).
        conn.execute(
            text("UPDATE worlds SET next_operation_sequence = 1 WHERE id = 1")
        )


def test_broken_allocator_is_repaired_to_max_plus_one(tmp_path):
    """A counter at or below MAX(operation_sequence) is repaired to
    MAX+1 for that world — the counter ONLY."""
    engine = _migrated_engine(tmp_path, "repair.db")
    _seed_ops(engine)

    import worldsim.server.db as dbmod

    dbmod.ensure_schema(engine)

    with engine.connect() as conn:
        counter = conn.execute(
            text("SELECT next_operation_sequence FROM worlds WHERE id = 1")
        ).scalar()
        rows = conn.execute(
            text(
                "SELECT operation_sequence, status FROM world_operations "
                "WHERE world_id = 1 ORDER BY operation_sequence"
            )
        ).fetchall()
    assert counter == 3  # MAX(2) + 1
    # The operations themselves are NEVER renumbered, rewritten or deleted.
    assert rows == [(1, "confirmed"), (2, "canceled")]


def test_healthy_counter_is_never_touched(tmp_path):
    """A counter already ahead of MAX+1 (the T1 invariant) survives the
    migration untouched — no silent rewind."""
    engine = _migrated_engine(tmp_path, "healthy.db")
    _seed_ops(engine)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE worlds SET next_operation_sequence = 10 WHERE id = 1")
        )

    import worldsim.server.db as dbmod

    dbmod.ensure_schema(engine)

    with engine.connect() as conn:
        counter = conn.execute(
            text("SELECT next_operation_sequence FROM worlds WHERE id = 1")
        ).scalar()
    assert counter == 10


def test_counter_below_max_repaired_to_max_plus_one(tmp_path):
    """Counter <= MAX with existing operations is repaired to MAX+1,
    not left as-is — otherwise T1 would collide with existing sequences."""
    engine = _migrated_engine(tmp_path, "lowc.db")
    _seed_ops(engine)  # creates ops with sequences 1 and 2
    with engine.begin() as conn:
        # Set counter to 1 (less than MAX=2) to simulate inconsistency
        conn.execute(
            text("UPDATE worlds SET next_operation_sequence = 1 WHERE id = 1")
        )

    import worldsim.server.db as dbmod

    dbmod.ensure_schema(engine)

    with engine.connect() as conn:
        counter = conn.execute(
            text("SELECT next_operation_sequence FROM worlds WHERE id = 1")
        ).scalar()
    assert counter == 3  # MAX(1,2) + 1 = 3


def test_counter_repair_is_per_world(tmp_path):
    """Each world is repaired against ITS OWN operations: a healthy world
    is unaffected by a broken neighbour."""
    engine = _migrated_engine(tmp_path, "multi.db")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO worlds (seed, width, height, months_simulated, "
                "state_json, created_at, name, mode, status, "
                "checkpoint_count, next_operation_sequence) "
                "VALUES (2, 64, 32, 0, '{}', 't', 'w2', 'single', "
                "'running', 0, 7)"
            )
        )
    _seed_ops(engine)  # world 1 has ops 1..2 with a broken counter

    import worldsim.server.db as dbmod

    dbmod.ensure_schema(engine)

    with engine.connect() as conn:
        w1 = conn.execute(
            text("SELECT next_operation_sequence FROM worlds WHERE id = 1")
        ).scalar()
        w2 = conn.execute(
            text("SELECT next_operation_sequence FROM worlds WHERE id = 2")
        ).scalar()
    assert w1 == 3   # repaired
    assert w2 == 7   # untouched (no operations for world 2)


# ---------------------------------------------------------------------------
# End-to-end: T1 works on the migrated database
# ---------------------------------------------------------------------------


def test_t1_allocates_correctly_after_migration(tmp_path):
    """The migrated database behaves exactly like a fresh one: T1 reserves
    sequence 1 on an empty world and the counter advances."""
    from sqlalchemy.orm import sessionmaker

    from worldsim.server.operation_service import OperationParams

    engine = _migrated_engine(tmp_path, "t1.db")
    Session = sessionmaker(bind=engine)
    from worldsim.server.operation_service import OperationService

    service = OperationService(Session, engine_version="1.0")
    params = OperationParams(world_id=1, operation_type="advance", months=1)

    t1 = service.reserve_operation(1, 7, "mig-key", params)
    assert t1.operation.operation_sequence == 1
    assert t1.operation.status == "pending"

    with Session() as conn:
        counter = conn.execute(
            text("SELECT next_operation_sequence FROM worlds WHERE id = 1")
        ).scalar()
    assert counter == 2


# ---------------------------------------------------------------------------
# FIX3: NOT NULL migration for existing nullable next_operation_sequence
# ---------------------------------------------------------------------------


def _build_fix1_db(path) -> None:
    """Hand-build the FIX1 intermediate schema: next_operation_sequence
    exists but is nullable (added via ALTER TABLE in FIX1 migration)."""
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT UNIQUE,
            pw_hash TEXT, created_at TEXT
        );
        CREATE TABLE worlds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seed INTEGER, width INTEGER, height INTEGER,
            months_simulated INTEGER, land_pct FLOAT,
            state_json TEXT, created_at TEXT, name TEXT,
            mode TEXT, season_years INTEGER, season_over INTEGER,
            enabled_races_json TEXT, owner_user_id INTEGER,
            sea_level FLOAT, status TEXT,
            last_checkpoint_at TEXT, checkpoint_count INTEGER
        );
        -- FIX1 added this column as nullable DEFAULT 1 (no NOT NULL)
        ALTER TABLE worlds ADD COLUMN next_operation_sequence INTEGER DEFAULT 1;
        INSERT INTO worlds (
            seed, width, height, months_simulated, state_json, created_at,
            name, mode, status, checkpoint_count, next_operation_sequence
        ) VALUES (1, 100, 50, 3, '{}', '2026-01-01', 'W', 'single',
                  'running', 0, 1);
        INSERT INTO users (id, username, pw_hash, created_at)
        VALUES (7, 'adm', 'x', '2026-01-01');
        """
    )
    raw.commit()
    raw.close()


def _fix1_engine(tmp_path, name="fix1.db"):
    db_file = tmp_path / name
    _build_fix1_db(db_file)
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    import worldsim.server.db as dbmod

    dbmod.ensure_schema(engine)  # run 1
    dbmod.ensure_schema(engine)  # run 2 must be idempotent
    return engine


def test_fix1_nullable_column_becomes_not_null(tmp_path):
    """V3-SCH-01: FIX1 nullable next_operation_sequence becomes NOT NULL."""
    engine = _fix1_engine(tmp_path)
    insp = inspect(engine)
    col = next(c for c in insp.get_columns("worlds") if c["name"] == "next_operation_sequence")
    assert col["nullable"] is False
    assert str(col["default"]) in ("1", "'1'")


def test_fix1_null_counter_repaired_to_max_plus_one(tmp_path):
    """NULL next_operation_sequence is repaired to MAX(operation_sequence)+1
    when operations exist for the world (realistic FIX1 DB)."""
    import worldsim.server.db as dbmod

    db_file = tmp_path / "fix1_null.db"
    raw = sqlite3.connect(db_file)
    raw.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT UNIQUE,
            pw_hash TEXT, created_at TEXT
        );
        CREATE TABLE worlds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seed INTEGER, width INTEGER, height INTEGER,
            months_simulated INTEGER, land_pct FLOAT,
            state_json TEXT, created_at TEXT, name TEXT,
            mode TEXT, status TEXT, checkpoint_count INTEGER
        );
        ALTER TABLE worlds ADD COLUMN next_operation_sequence INTEGER DEFAULT 1;
        CREATE TABLE world_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            world_id INTEGER NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
            operation_id VARCHAR(64) NOT NULL,
            operation_sequence INTEGER NOT NULL,
            status VARCHAR(20) NOT NULL,
            continuity_state VARCHAR(20) NOT NULL,
            created_at VARCHAR NOT NULL,
            requesting_user_id INTEGER,
            client_idempotency_key VARCHAR(64),
            request_hash VARCHAR(64),
            engine_version VARCHAR(32),
            recovery_generation INTEGER NOT NULL DEFAULT 0,
            UNIQUE (world_id, operation_id),
            UNIQUE (world_id, operation_sequence)
        );
        INSERT INTO worlds (
            id, seed, width, height, months_simulated, land_pct, state_json,
            created_at, name, mode, status, checkpoint_count,
            next_operation_sequence
        ) VALUES (1, 1, 8, 8, 0, 0.0, '{}', 't', 'W', 'single',
                  'running', 0, NULL);
        INSERT INTO world_operations (
            world_id, operation_id, operation_sequence, status,
            continuity_state, created_at, requesting_user_id,
            client_idempotency_key, request_hash, engine_version,
            recovery_generation
        ) VALUES (1, 'op1', 5, 'confirmed', 'active', 't', 1, 'k1', 'h1',
                  '1.0', 0);
        """
    )
    raw.commit()
    raw.close()

    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    # Confirm the pre-migration state: nullable column with NULL value.
    insp = inspect(engine)
    col = next(
        c for c in insp.get_columns("worlds")
        if c["name"] == "next_operation_sequence"
    )
    assert col["nullable"] is True

    dbmod.ensure_schema(engine)

    insp = inspect(engine)
    col = next(
        c for c in insp.get_columns("worlds")
        if c["name"] == "next_operation_sequence"
    )
    assert col["nullable"] is False
    with engine.connect() as conn:
        counter = conn.execute(
            text("SELECT next_operation_sequence FROM worlds WHERE id = 1")
        ).scalar()
    assert counter == 6  # MAX(5) + 1


def test_fix1_rejects_explicit_null_insert(tmp_path):
    """After migration, explicit NULL inserts are rejected by SQLite."""
    engine = _fix1_engine(tmp_path, "fix1_reject.db")
    with engine.begin() as conn:
        with pytest.raises(Exception, match="NOT NULL"):
            conn.execute(
                text(
                    "INSERT INTO worlds ("
                    "seed, width, height, months_simulated, state_json, "
                    "created_at, name, mode, status, checkpoint_count, "
                    "next_operation_sequence"
                    ") VALUES (2, 10, 10, 0, '{}', 't', 'w2', 'single', "
                    "'running', 0, NULL)"
                )
            )


def test_fix1_idempotent(tmp_path):
    """Running ensure_schema multiple times on FIX1 DB is idempotent."""
    engine = _fix1_engine(tmp_path, "fix1_idem.db")

    import worldsim.server.db as dbmod

    with engine.connect() as conn:
        before = conn.execute(
            text(
                "SELECT id, seed, name, next_operation_sequence "
                "FROM worlds ORDER BY id"
            )
        ).fetchall()

    dbmod.ensure_schema(engine)
    dbmod.ensure_schema(engine)

    with engine.connect() as conn:
        after = conn.execute(
            text(
                "SELECT id, seed, name, next_operation_sequence "
                "FROM worlds ORDER BY id"
            )
        ).fetchall()
    assert after == before
    # Foreign keys must still be enforced
    with engine.begin() as conn:
        with pytest.raises(Exception, match="FOREIGN KEY"):
            conn.execute(
                text(
                    "INSERT INTO world_operations ("
                    "world_id, operation_id, operation_sequence, status, "
                    "continuity_state, created_at, requesting_user_id, "
                    "client_idempotency_key, request_hash, engine_version, "
                    "recovery_generation"
                    ") VALUES (9999, 'x', 1, 'pending', 'active', 't', 1, "
                    "'k', 'h', '1.0', 0)"
                )
            )
    # Integrity must hold
    with engine.connect() as conn:
        fk_check = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
        assert len(fk_check) == 0
        integrity = conn.execute(text("PRAGMA integrity_check")).fetchone()
        assert integrity[0] == "ok"


# ---------------------------------------------------------------------------
# FIX4: reconstrucción atómica y preservación íntegra del esquema (V4/V5)
# ---------------------------------------------------------------------------

def _build_full_fix1_db(path) -> None:
    """Construye una base con el esquema real (create_all), la degrada al
    estado intermedio FIX1 (next_operation_sequence nullable) y la puebla con
    datos representativos, FK saliente, índice explícito y trigger."""
    import re as _re
    import worldsim.server.db as dbmod

    eng = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False}
    )
    dbmod.Base.metadata.create_all(eng)
    raw = sqlite3.connect(path)
    ddl = raw.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='worlds'"
    ).fetchone()[0]
    fix1 = _re.sub(
        r'(\bCREATE\s+TABLE\s+)"?worlds"?\s*\(',
        r"\1worlds_n (",
        ddl,
        count=1,
        flags=_re.IGNORECASE,
    ).replace(
        "next_operation_sequence INTEGER DEFAULT '1' NOT NULL",
        "next_operation_sequence INTEGER DEFAULT 1",
    )
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute(fix1)
    cols = [r[1] for r in raw.execute("PRAGMA table_info(worlds)")]
    raw.execute(
        f"INSERT INTO worlds_n ({', '.join(cols)}) SELECT {', '.join(cols)} FROM worlds"
    )
    raw.execute("DROP TABLE worlds")
    raw.execute("ALTER TABLE worlds_n RENAME TO worlds")
    raw.execute("CREATE INDEX ix_worlds_seed ON worlds (seed)")
    raw.execute(
        "CREATE TRIGGER trg_worlds_audit AFTER INSERT ON worlds "
        "BEGIN INSERT INTO world_checkpoints("
        "world_id, sequence, month, checkpoint_type, payload, checksum, "
        "created_at, is_valid, checkpoint_version) "
        "VALUES (NEW.id, 0, 0, 'manual', x'00', 'trg', 't', 1, 2); END"
    )
    raw.executescript(
        """
        INSERT INTO users (id, username, pw_hash, created_at) VALUES (1,'u','h','t'),(2,'u2','h','t');
        INSERT INTO worlds (id, owner_user_id, seed, width, height, months_simulated, state_json,
          created_at, name, mode, season_years, season_over, enabled_races_json, sea_level,
          visibility, status, last_checkpoint_at, checkpoint_count, next_operation_sequence)
          VALUES (1,1,42,8,8,0,'{}','t','W1','single',300,0,'[]',0.2,'private','running','t',3,NULL),
                 (2,2,77,5,5,1,'{}','t','W2','league',300,0,'[]',0.2,'public','stopped','t',0,999);
        INSERT INTO world_operations (world_id, operation_id, operation_sequence, status,
          continuity_state, created_at, requesting_user_id, recovery_generation)
          VALUES (1,'op1',5,'confirmed','active','t',1,0),(1,'op2',7,'confirmed','active','t',1,0);
        INSERT INTO world_events (world_id, operation_id, operation_sequence, position, month,
          event_type, event_text, created_at) VALUES (1,'op1',5,0,5,'birth','x','t');
        INSERT INTO operation_resolutions (world_id, operation_id, operation_sequence, admin_user_id,
          resolution_type, reason, evidence_refs, resolution_version, created_at, idempotency_key)
          VALUES (1,'op1',5,1,'continue_with_gap','r','[]',1,'t','ik');
        INSERT INTO claims (world_id, kingdom_id, user_token, user_name, month, points, user_id)
          VALUES (1,0,'tok1','u1',1,0,1);
        INSERT INTO predictions (world_id, user_token, user_name, kind, params_json, month,
          deadline_months, status, points, resolved_month, resolved_text)
          VALUES (1,'tok1','u1','war_victory','{}',5,10,'pending',0,NULL,NULL);
        INSERT INTO chat (world_id, user_token, user_name, text, month) VALUES (1,'tok1','u1','hi',5);
        """
    )
    raw.commit()
    raw.close()
    eng.dispose()


def _worlds_snapshot(conn) -> dict:
    from sqlalchemy import text

    return {
        "ddl": conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='worlds'")
        ).scalar(),
        "table_info": conn.execute(text("PRAGMA table_info(worlds)")).fetchall(),
        "fk_out": sorted(
            map(tuple, conn.execute(text("PRAGMA foreign_key_list(worlds)")).fetchall())
        ),
        "indexes": sorted(
            (row[1], bool(row[2]))
            for row in conn.execute(text("PRAGMA index_list(worlds)")).fetchall()
        ),
        "triggers": sorted(
            map(tuple, conn.execute(
                text(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type='trigger' AND tbl_name='worlds'"
                )
            ).fetchall())
        ),
    }


def test_fix4_preserva_esquema_integral(tmp_path):
    """F4-AUD-02: la reconstrucción preserva FK saliente, CHECK con nombre,
    índice explícito, trigger, datos y relaciones entrantes. El único cambio
    intencional es next_operation_sequence NOT NULL DEFAULT 1."""
    import worldsim.server.db as dbmod
    from sqlalchemy import text

    db_file = tmp_path / "fix4_schema.db"
    _build_full_fix1_db(db_file)
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )

    insp = inspect(engine)
    col = next(
        c for c in insp.get_columns("worlds") if c["name"] == "next_operation_sequence"
    )
    assert col["nullable"] is True  # estado intermedio FIX1

    with engine.connect() as conn:
        before = _worlds_snapshot(conn)

    dbmod.ensure_schema(engine)

    insp = inspect(engine)
    col = next(
        c for c in insp.get_columns("worlds") if c["name"] == "next_operation_sequence"
    )
    assert col["nullable"] is False
    assert str(col["default"]) in ("1", "'1'")

    with engine.connect() as conn:
        after = _worlds_snapshot(conn)

    # FK saliente preservada (owner_user_id -> users(id))
    assert (0, 0, "users", "owner_user_id", "id", "NO ACTION", "NO ACTION", "NONE") in (
        after["fk_out"]
    )
    # CHECK con nombre preservado
    assert "ck_worlds_visibility" in after["ddl"]
    # Índice explícito preservado
    assert any(name == "ix_worlds_seed" for name, _ in after["indexes"])
    # Trigger preservado
    assert any(name == "trg_worlds_audit" for name, _ in after["triggers"])
    # Datos preservados (salvo reparación del contador)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM worlds")).scalar() == 2
        assert conn.execute(text("SELECT count(*) FROM world_operations")).scalar() == 2
        assert conn.execute(
            text("SELECT next_operation_sequence FROM worlds WHERE id = 1")
        ).scalar() == 8  # MAX(7) + 1
        assert conn.execute(
            text("SELECT next_operation_sequence FROM worlds WHERE id = 2")
        ).scalar() == 999  # valor elevado preservado
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        assert conn.execute(text("PRAGMA integrity_check")).scalar() == "ok"
    engine.dispose()


_FIX4_FAIL_FRAGMENTS = [
    "CREATE TABLE worlds_new",
    "INSERT INTO worlds_new",
    "DROP TABLE worlds",
    "ALTER TABLE worlds_new RENAME TO worlds",
    "CREATE INDEX ix_worlds_seed",
]


@pytest.mark.parametrize("fragment", _FIX4_FAIL_FRAGMENTS)
def test_fix4_fallo_entonces_rollback_y_reintento(tmp_path, fragment):
    """F4-AUD-01: un fallo en cualquier fase de la reconstrucción hace rollback
    completo (sin tablas residuales ni pérdida de datos) y un reintento
    posterior termina correctamente con la columna NOT NULL."""
    import worldsim.server.db as dbmod
    from sqlalchemy import event, text

    db_file = tmp_path / "fix4_fail.db"
    _build_full_fix1_db(db_file)
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    fired = {"n": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def inyectar(conn, cursor, statement, parameters, context, executemany):
        if not fired["n"] and fragment.upper() in statement.upper():
            fired["n"] += 1
            raise RuntimeError(f"INYECTADO: {fragment}")

    with pytest.raises(RuntimeError, match="INYECTADO"):
        dbmod.ensure_schema(engine)

    # Tras el fallo: sin tablas residuales, datos intactos, integridad OK.
    with engine.connect() as conn:
        tables = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1"
                )
            ).fetchall()
        ]
        assert "worlds_new" not in tables
        assert conn.execute(text("SELECT count(*) FROM worlds")).scalar() == 2
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        assert conn.execute(text("PRAGMA integrity_check")).scalar() == "ok"

    # Reintento con motor limpio: termina correctamente y deja NOT NULL.
    engine2 = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    dbmod.ensure_schema(engine2)
    with engine2.connect() as conn:
        row = next(
            r
            for r in conn.execute(text("PRAGMA table_info(worlds)")).fetchall()
            if r[1] == "next_operation_sequence"
        )
        assert row[3] == 1  # notnull
        assert conn.execute(
            text("SELECT next_operation_sequence FROM worlds WHERE id = 1")
        ).scalar() == 8
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    engine.dispose()
    engine2.dispose()
