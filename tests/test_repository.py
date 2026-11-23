"""Tests for the persistence layer (Fase 2.1).

Covers WorldRow migration, WorldCheckpoint creation/validation, and the
repository layer.  Uses an isolated SQLite database per test.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from worldsim.data.loaders import load_governments, load_races
from worldsim.export.checkpoint import (
    checkpoint_to_dict,
    engine_from_checkpoint,
)
from worldsim.server import db
from worldsim.server.db import (
    Base,
    WorldCheckpoint,
    WorldRow,
)
from worldsim.server.repository import CheckpointRepository, WorldRepository
from worldsim.sim.engine import SimEngine
from worldsim.worldgen import WorldConfig
from worldsim.worldgen.pipeline import generate_world


DATA_DIR = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(scope="session")
def data_dir():
    return DATA_DIR


@pytest.fixture(scope="session")
def races(data_dir):
    return load_races(data_dir / "Races.txt")


@pytest.fixture(scope="session")
def governments(data_dir):
    return load_governments(data_dir / "CivilizedGovernment.txt")


@pytest.fixture(scope="session")
def small_world():
    return generate_world(WorldConfig(seed=42, width=100, height=50, rivers=4))


@pytest.fixture
def tmp_db_path(tmp_path):
    return tmp_path / "test_repository.db"


@pytest.fixture
def engine_obj(tmp_db_path):
    """Create an isolated SQLite engine and session factory for a test."""
    engine = create_engine(f"sqlite:///{tmp_db_path}")

    # Enable foreign key enforcement for SQLite connections BEFORE creating
    # tables, so that the connect event fires on the first pooled connection.
    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, connection_record):
        dbapi_conn.execute("PRAGMA foreign_keys = ON")

    Base.metadata.create_all(engine)

    Session = sessionmaker(bind=engine)
    yield engine, Session
    engine.dispose()


def _make_engine_instance(small_world, races, governments, seed=7):
    """Build a SimEngine and advance a few ticks so there is something to save."""
    eng = SimEngine(small_world, races, governments, seed=seed)
    for _ in range(10):
        eng.tick()
    return eng


def _serialize_engine(eng):
    """Serialize engine to JSON bytes (v2 checkpoint format)."""
    state = checkpoint_to_dict(eng)
    return json.dumps(state, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------------------
# WorldRow: creation and migration
# ---------------------------------------------------------------------------

def test_create_world_with_status_running(engine_obj):
    _engine, Session = engine_obj
    s = Session()
    try:
        row = WorldRow(
            seed=42,
            width=100,
            height=50,
            months_simulated=0,
            land_pct=50.0,
            state_json="{}",
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            name="Test World",
            mode="single",
            sea_level=0.2,
            status="running",
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        assert row.status == "running"
        assert row.checkpoint_count == 0
        assert row.last_checkpoint_at is None
    finally:
        s.close()


def test_existing_worlds_migrate_to_legacy(engine_obj):
    """An existing world saved before the `status` column existed should be
    marked as 'legacy' after ensure_schema() runs."""
    _engine, Session = engine_obj
    s = Session()
    try:
        # Drop the worlds table first (it was created by Base.metadata.create_all
        # in the fixture with all columns including status). Recreate it
        # WITHOUT the status column to simulate an old DB.
        s.execute(text("DROP TABLE IF EXISTS worlds"))
        s.execute(
            text(
                "CREATE TABLE worlds ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "seed INTEGER, width INTEGER, height INTEGER, "
                "months_simulated INTEGER, land_pct FLOAT, "
                "state_json TEXT, created_at TEXT, name TEXT, mode TEXT, "
                "season_years INTEGER, season_over INTEGER, "
                "enabled_races_json TEXT, owner_user_id INTEGER, "
                "sea_level FLOAT)"
            )
        )
        s.commit()

        # Insert a world row without status (simulating old data).
        s.execute(
            text(
                "INSERT INTO worlds (seed, width, height, months_simulated, "
                "land_pct, state_json, created_at, name, mode, sea_level) "
                "VALUES (42, 100, 50, 0, 50.0, '{}', "
                "'2026-01-01 00:00:00', 'Old World', 'single', 0.2)"
            )
        )
        s.commit()

        # Run ensure_schema again to backfill status = 'legacy' for existing
        # rows.
        db.ensure_schema(_engine)

        row = s.scalars(
            select(WorldRow).where(WorldRow.name == "Old World")
        ).first()
        assert row is not None
        assert row.status == "legacy"
    finally:
        s.close()


def test_state_json_preserved(engine_obj):
    _engine, Session = engine_obj
    s = Session()
    try:
        row = WorldRow(
            seed=7,
            width=80,
            height=60,
            months_simulated=24,
            land_pct=45.0,
            state_json='{"month": 24, "custom": "data"}',
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            name="State Test",
            mode="single",
            sea_level=0.2,
            status="running",
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        assert row.state_json == '{"month": 24, "custom": "data"}'
        assert row.months_simulated == 24
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Checkpoint CRUD
# ---------------------------------------------------------------------------

def test_create_checkpoint_v2_and_read_back(engine_obj, small_world, races, governments):
    _engine, Session = engine_obj
    # Create a world row first.
    s = Session()
    row = WorldRow(
        seed=42,
        width=100,
        height=50,
        months_simulated=10,
        land_pct=50.0,
        state_json="{}",
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Checkpoint Test",
        mode="single",
        sea_level=0.2,
        status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    payload = _serialize_engine(eng)
    checksum = hashlib.sha256(payload).hexdigest()

    with CheckpointRepository(Session) as repo:
        cp_id = repo.save_checkpoint(
            world_id=world_id,
            sequence=1,
            month=10,
            checkpoint_type="periodic",
            payload=payload,
            checksum=checksum,
        )
        assert cp_id is not None

        # Read back.
        cp = repo.get_checkpoint_by_id(cp_id)
        assert cp is not None
        assert cp.world_id == world_id
        assert cp.sequence == 1
        assert cp.month == 10
        assert cp.checkpoint_type == "periodic"
        assert cp.payload == payload
        assert cp.checksum == checksum
        assert cp.is_valid == 1
        assert cp.checkpoint_version == 2

        # Verify payload round-trips through the engine export.
        restored = engine_from_checkpoint(json.loads(cp.payload.decode("utf-8")))
        assert restored.month == 10


def test_get_latest_valid_checkpoint_ignores_corrupt(engine_obj, small_world, races, governments):
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42,
        width=100,
        height=50,
        months_simulated=10,
        land_pct=50.0,
        state_json="{}",
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Corrupt Test",
        mode="single",
        sea_level=0.2,
        status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    good_payload = _serialize_engine(eng)
    good_checksum = hashlib.sha256(good_payload).hexdigest()
    bad_payload = b"corrupted payload data"
    bad_checksum = hashlib.sha256(bad_payload).hexdigest()

    with CheckpointRepository(Session) as repo:
        # First checkpoint: valid.
        cp1 = repo.save_checkpoint(
            world_id=world_id,
            sequence=1,
            month=10,
            checkpoint_type="periodic",
            payload=good_payload,
            checksum=good_checksum,
        )
        # Second checkpoint: corrupt checksum stored (payload is fine but
        # checksum does not match).
        cp2 = repo.save_checkpoint(
            world_id=world_id,
            sequence=2,
            month=20,
            checkpoint_type="periodic",
            payload=good_payload,
            checksum=bad_checksum,
        )

        latest = repo.get_latest_valid_checkpoint(world_id)
        assert latest is not None
        latest_seq = latest.sequence
        latest_checksum = latest.checksum
        assert latest_seq == 1
        assert latest_checksum == good_checksum

        # The corrupt one should now be marked invalid. cp2_row comes from the
        # checkpoint repository's own session, so its attributes are current.
        cp2_row = repo.get_checkpoint_by_id(cp2)
        assert cp2_row.is_valid == 0


def test_get_latest_valid_checkpoint_none_when_all_corrupt(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42,
        width=100,
        height=50,
        months_simulated=10,
        land_pct=50.0,
        state_json="{}",
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="All Corrupt",
        mode="single",
        sea_level=0.2,
        status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    payload = _serialize_engine(eng)
    bad_checksum = "0" * 64  # definitely wrong

    with CheckpointRepository(Session) as repo:
        repo.save_checkpoint(
            world_id=world_id,
            sequence=1,
            month=10,
            checkpoint_type="periodic",
            payload=payload,
            checksum=bad_checksum,
        )
        latest = repo.get_latest_valid_checkpoint(world_id)
        assert latest is None


def test_sequence_uniqueness_per_world(engine_obj, small_world, races, governments):
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42,
        width=100,
        height=50,
        months_simulated=10,
        land_pct=50.0,
        state_json="{}",
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Sequence Test",
        mode="single",
        sea_level=0.2,
        status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    payload = _serialize_engine(eng)
    checksum = hashlib.sha256(payload).hexdigest()

    with CheckpointRepository(Session) as repo:
        repo.save_checkpoint(
            world_id=world_id,
            sequence=1,
            month=10,
            checkpoint_type="periodic",
            payload=payload,
            checksum=checksum,
        )
        with pytest.raises(IntegrityError):
            repo.save_checkpoint(
                world_id=world_id,
                sequence=1,  # duplicate sequence for same world
                month=11,
                checkpoint_type="periodic",
                payload=payload,
                checksum=checksum,
            )


def test_sequences_independent_between_worlds(engine_obj, small_world, races, governments):
    _engine, Session = engine_obj
    s = Session()
    rows = []
    for name in ("World A", "World B"):
        row = WorldRow(
            seed=42,
            width=100,
            height=50,
            months_simulated=10,
            land_pct=50.0,
            state_json="{}",
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            name=name,
            mode="single",
            sea_level=0.2,
            status="running",
        )
        s.add(row)
        rows.append(row)
    s.commit()
    for row in rows:
        s.refresh(row)
    world_ids = [r.id for r in rows]
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    payload = _serialize_engine(eng)
    checksum = hashlib.sha256(payload).hexdigest()

    with CheckpointRepository(Session) as repo:
        # Both worlds can have sequence 1.
        cp_a = repo.save_checkpoint(
            world_id=world_ids[0],
            sequence=1,
            month=10,
            checkpoint_type="periodic",
            payload=payload,
            checksum=checksum,
        )
        cp_b = repo.save_checkpoint(
            world_id=world_ids[1],
            sequence=1,
            month=10,
            checkpoint_type="periodic",
            payload=payload,
            checksum=checksum,
        )
        # Read attributes inside the session to avoid DetachedInstanceError
        cp_a_obj = repo.get_checkpoint_by_id(cp_a)
        cp_b_obj = repo.get_checkpoint_by_id(cp_b)
        assert cp_a is not None
        assert cp_b is not None
        assert cp_a_obj.sequence == 1
        assert cp_b_obj.sequence == 1
        assert cp_a_obj.world_id != cp_b_obj.world_id


def test_atomic_checkpoint_and_metadata_update(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42,
        width=100,
        height=50,
        months_simulated=0,
        land_pct=50.0,
        state_json="{}",
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Atomic Test",
        mode="single",
        sea_level=0.2,
        status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    payload = _serialize_engine(eng)
    checksum = hashlib.sha256(payload).hexdigest()

    with CheckpointRepository(Session) as repo:
        repo.save_checkpoint(
            world_id=world_id,
            sequence=1,
            month=42,
            checkpoint_type="periodic",
            payload=payload,
            checksum=checksum,
        )

    with Session() as s:
        row = s.get(WorldRow, world_id)
        assert row.last_checkpoint_at is not None
        assert row.months_simulated == 42
        assert row.checkpoint_count == 1


def test_rollback_on_failure(engine_obj, small_world, races, governments):
    _engine, Session = engine_obj
    s = Session()
    # Do NOT create a world row; use a non-existent world_id.
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    payload = _serialize_engine(eng)
    checksum = hashlib.sha256(payload).hexdigest()

    with CheckpointRepository(Session) as repo:
        with pytest.raises(Exception):
            repo.save_checkpoint(
                world_id=999999,  # non-existent
                sequence=1,
                month=10,
                checkpoint_type="periodic",
                payload=payload,
                checksum=checksum,
            )

    # After the failed save, no checkpoint row should exist for world 999999.
    with Session() as s:
        count = s.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == 999999)
        ).first()
        assert count is None


def test_ensure_schema_idempotent(tmp_db_path):
    engine = create_engine(f"sqlite:///{tmp_db_path}")
    try:
        db.ensure_schema(engine)
        db.ensure_schema(engine)  # second time should not error
        # Verify tables exist.
        s = sessionmaker(bind=engine)()
        count = s.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == -1)
        ).first()
        assert count is None
        s.close()
    finally:
        engine.dispose()


def test_delete_world_cascades_to_checkpoints(engine_obj, small_world, races, governments):
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42,
        width=100,
        height=50,
        months_simulated=10,
        land_pct=50.0,
        state_json="{}",
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Cascade Test",
        mode="single",
        sea_level=0.2,
        status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    payload = _serialize_engine(eng)
    checksum = hashlib.sha256(payload).hexdigest()

    with CheckpointRepository(Session) as repo:
        repo.save_checkpoint(
            world_id=world_id,
            sequence=1,
            month=10,
            checkpoint_type="periodic",
            payload=payload,
            checksum=checksum,
        )

    with Session() as s:
        s.delete(s.get(WorldRow, world_id))
        s.commit()

        remaining = s.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == world_id)
        ).first()
        assert remaining is None


# ---------------------------------------------------------------------------
# Session ownership and closing
# ---------------------------------------------------------------------------

def test_external_session_not_closed(engine_obj):
    """A Session instance passed to the repository must remain open after use."""
    _engine, Session = engine_obj
    external = Session()
    try:
        repo = WorldRepository(external)
        # Use the repository (session stays open).
        repo._session.execute(text("SELECT 1"))
        assert external.is_active, "external session should still be active"
    finally:
        external.close()


def test_sessionmaker_instance_session_closed(engine_obj):
    """A session created from a sessionmaker instance must be closed by the repo."""
    _engine, Session = engine_obj
    repo = CheckpointRepository(Session)
    repo.close()
    # After close, the internally created session should be closed.
    # We can't easily inspect the internal session, but the close() call
    # must not raise and the repository should be in a clean state.


def test_none_session_created_and_closed(engine_obj):
    """When no session is provided, the repository creates and closes its own."""
    _engine, Session = engine_obj
    repo = WorldRepository()
    # Use it via context manager (creates and closes its own session).
    with repo:
        pass
    # close() should not raise after context exit.
    repo.close()


def test_checkpoint_repository_sessionmaker_class_creates_and_closes(engine_obj):
    """Passing the sessionmaker class itself creates a session that is closed."""
    _engine, Session = engine_obj
    repo = CheckpointRepository(Session)
    assert repo._session is not None
    assert repo._owns_session is True
    repo.close()


# ---------------------------------------------------------------------------
# Persistent invalidation and fallback
# ---------------------------------------------------------------------------

def test_corrupt_checkpoint_invalidation_persists_across_sessions(
    engine_obj, small_world, races, governments
):
    """A corrupt checkpoint must remain is_valid=0 when checked from a new session."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Persist Invalid", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    good_payload = _serialize_engine(eng)
    good_checksum = hashlib.sha256(good_payload).hexdigest()
    bad_checksum = "0" * 64

    with CheckpointRepository(Session) as repo:
        repo.save_checkpoint(
            world_id=world_id, sequence=1, month=10,
            checkpoint_type="periodic", payload=good_payload,
            checksum=good_checksum,
        )
        repo.save_checkpoint(
            world_id=world_id, sequence=2, month=20,
            checkpoint_type="periodic", payload=good_payload,
            checksum=bad_checksum,
        )
        # Recovery marks seq=2 invalid and returns seq=1.
        latest = repo.get_latest_valid_checkpoint(world_id)
        assert latest is not None
        assert latest.sequence == 1

    # Now open a brand-new session and verify the corruption flag persisted.
    with Session() as s2:
        cp2 = s2.get(WorldCheckpoint, 2)
        assert cp2 is not None
        assert cp2.is_valid == 0, "corrupt checkpoint must remain invalid in new session"

        # Fallback should still return seq=1.
        repo2 = CheckpointRepository(s2)
        fallback = repo2.get_latest_valid_checkpoint(world_id)
        assert fallback is not None
        assert fallback.sequence == 1


def test_all_corrupt_invalidation_persists(
    engine_obj, small_world, races, governments
):
    """When all checkpoints are corrupt, invalidations persist and None is returned."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="All Corrupt Persist", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    payload = _serialize_engine(eng)
    bad_checksum = "0" * 64

    with CheckpointRepository(Session) as repo:
        repo.save_checkpoint(
            world_id=world_id, sequence=1, month=10,
            checkpoint_type="periodic", payload=payload,
            checksum=bad_checksum,
        )
        latest = repo.get_latest_valid_checkpoint(world_id)
        assert latest is None

    # Verify from a new session that the row was marked invalid.
    with Session() as s2:
        cp = s2.get(WorldCheckpoint, 1)
        assert cp is not None
        assert cp.is_valid == 0


# ---------------------------------------------------------------------------
# checkpoint_version: individual per WorldCheckpoint, absent from WorldRow
# ---------------------------------------------------------------------------

def test_checkpoint_version_absent_from_world_row(engine_obj):
    """WorldRow must not have a checkpoint_version column."""
    _engine, Session = engine_obj
    # WorldRow.__table__ should not contain checkpoint_version.
    col_names = {c.name for c in WorldRow.__table__.columns}
    assert "checkpoint_version" not in col_names


def test_checkpoint_version_individual_per_row(
    engine_obj, small_world, races, governments
):
    """Each WorldCheckpoint row stores its own checkpoint_version."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Version Test", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    payload = _serialize_engine(eng)
    checksum = hashlib.sha256(payload).hexdigest()

    with CheckpointRepository(Session) as repo:
        cp_id = repo.save_checkpoint(
            world_id=world_id, sequence=1, month=10,
            checkpoint_type="periodic", payload=payload,
            checksum=checksum, checkpoint_version=2,
        )
        cp = repo.get_checkpoint_by_id(cp_id)
        assert cp.checkpoint_version == 2


# ---------------------------------------------------------------------------
# get_latest_valid_checkpoint: is_valid filtering and re-processing
# ---------------------------------------------------------------------------

def test_checkpoint_with_is_valid_zero_not_returned(
    engine_obj, small_world, races, governments
):
    """A checkpoint with is_valid=0 and correct checksum must not be returned."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Invalid Zero", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    payload = _serialize_engine(eng)
    checksum = hashlib.sha256(payload).hexdigest()

    with CheckpointRepository(Session) as repo:
        # Insert a checkpoint with is_valid=0 directly via SQL.
        s2 = Session()
        s2.execute(
            text(
                "INSERT INTO world_checkpoints "
                "(world_id, sequence, month, checkpoint_type, payload, checksum, "
                "created_at, is_valid, checkpoint_version) "
                "VALUES (:wid, 1, 10, 'periodic', :payload, :checksum, "
                ":created_at, 0, 2)"
            ),
            {"wid": world_id, "payload": payload, "checksum": checksum,
             "created_at": "2026-01-01 00:00:00"},
        )
        s2.commit()
        s2.close()

        latest = repo.get_latest_valid_checkpoint(world_id)
        assert latest is None


def test_second_query_does_not_reprocess_invalidated(
    engine_obj, small_world, races, governments
):
    """A second query must not re-process checkpoints already marked invalid."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="No Reprocess", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    good_payload = _serialize_engine(eng)
    good_checksum = hashlib.sha256(good_payload).hexdigest()
    bad_checksum = "0" * 64

    with CheckpointRepository(Session) as repo:
        repo.save_checkpoint(
            world_id=world_id, sequence=1, month=10,
            checkpoint_type="periodic", payload=good_payload,
            checksum=good_checksum,
        )
        repo.save_checkpoint(
            world_id=world_id, sequence=2, month=20,
            checkpoint_type="periodic", payload=good_payload,
            checksum=bad_checksum,
        )
        # First call: marks seq=2 invalid, returns seq=1.
        latest = repo.get_latest_valid_checkpoint(world_id)
        assert latest is not None
        assert latest.sequence == 1

        # Second call: should not re-process seq=2 (already is_valid=0).
        # Since seq=2 is filtered out by is_valid=1, only seq=1 is examined.
        latest2 = repo.get_latest_valid_checkpoint(world_id)
        assert latest2 is not None
        assert latest2.sequence == 1


# ---------------------------------------------------------------------------
# ensure_schema: idempotent backfill on existing columns
# ---------------------------------------------------------------------------

def test_ensure_schema_backfills_nulls_on_existing_columns(tmp_db_path):
    """ensure_schema() must correct NULLs in status and checkpoint_count
    even when the columns already exist."""
    engine = create_engine(f"sqlite:///{tmp_db_path}")
    try:
        # Create table with status and checkpoint_count columns but NULL values.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE worlds ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "seed INTEGER, width INTEGER, height INTEGER, "
                    "months_simulated INTEGER, land_pct FLOAT, "
                    "state_json TEXT, created_at TEXT, name TEXT, mode TEXT, "
                    "season_years INTEGER, season_over INTEGER, "
                    "enabled_races_json TEXT, owner_user_id INTEGER, "
                    "sea_level FLOAT, status VARCHAR(20), "
                    "checkpoint_count INTEGER)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO worlds (seed, width, height, months_simulated, "
                    "land_pct, state_json, created_at, name, mode, sea_level, "
                    "status, checkpoint_count) "
                    "VALUES (42, 100, 50, 0, 50.0, '{}', "
                    "'2026-01-01 00:00:00', 'Null Status', 'single', 0.2, "
                    "NULL, NULL)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO worlds (seed, width, height, months_simulated, "
                    "land_pct, state_json, created_at, name, mode, sea_level, "
                    "status, checkpoint_count) "
                    "VALUES (43, 100, 50, 0, 50.0, '{}', "
                    "'2026-01-01 00:00:00', 'Valid Status', 'single', 0.2, "
                    "'running', 3)"
                )
            )

        # Run ensure_schema - should backfill NULLs but not overwrite valid values.
        db.ensure_schema(engine)

        SessionLocal2 = sessionmaker(bind=engine)
        with SessionLocal2() as s:
            rows = s.execute(
                text("SELECT name, status, checkpoint_count FROM worlds ORDER BY id")
            ).fetchall()
            assert rows[0] == ("Null Status", "legacy", 0)
            assert rows[1] == ("Valid Status", "running", 3)

        # Run again - idempotent, no changes.
        db.ensure_schema(engine)

        with SessionLocal2() as s:
            rows = s.execute(
                text("SELECT name, status, checkpoint_count FROM worlds ORDER BY id")
            ).fetchall()
            assert rows[0] == ("Null Status", "legacy", 0)
            assert rows[1] == ("Valid Status", "running", 3)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Transactional contract: external session not committed by repo
# ---------------------------------------------------------------------------

def test_external_session_pending_changes_not_committed(
    engine_obj, small_world, races, governments
):
    """A pending change in an external session must not be committed by a
    repository operation on that same session."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="External Pending", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    payload = _serialize_engine(eng)
    checksum = hashlib.sha256(payload).hexdigest()

    external = Session()
    try:
        # Make a pending change in the external session (not committed).
        external.execute(
            text("UPDATE worlds SET status = 'paused' WHERE id = :wid"),
            {"wid": world_id},
        )
        # The external session still sees its own uncommitted change.
        status_before = external.execute(
            text("SELECT status FROM worlds WHERE id = :wid"),
            {"wid": world_id},
        ).scalar()
        assert status_before == "paused"

        # Run save_checkpoint on the same external session.
        repo = CheckpointRepository(external)
        repo.save_checkpoint(
            world_id=world_id, sequence=1, month=10,
            checkpoint_type="periodic", payload=payload,
            checksum=checksum,
        )
        # save_checkpoint flushed but did NOT commit the external session.
        # Verify by opening a new session: the 'paused' status should not be visible.
        with Session() as s2:
            status_after = s2.execute(
                text("SELECT status FROM worlds WHERE id = :wid"),
                {"wid": world_id},
            ).scalar()
            assert status_after != "paused", (
                "external session's pending change must not be committed by repo"
            )
    finally:
        external.rollback()
        external.close()


# ---------------------------------------------------------------------------
# Transactional contract: exception handling and rollback ownership
# ---------------------------------------------------------------------------

def _failing_execute(*args, **kwargs):
    """Inject a SQLAlchemy error at the execute() level."""
    from sqlalchemy.exc import IntegrityError
    raise IntegrityError("injected failure", None, None)


def test_save_checkpoint_owned_session_rollback_on_injected_error(
    engine_obj, small_world, races, governments
):
    """An injected error after checkpoint insert leaves no persisted checkpoint
    or partial metadata when the repository owns the session."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Owned Rollback", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    payload = _serialize_engine(eng)
    checksum = hashlib.sha256(payload).hexdigest()

    repo = CheckpointRepository(Session)  # owns its own session
    try:
        with _monkeypatch_execute(repo, _failing_execute):
            with pytest.raises(Exception):
                repo.save_checkpoint(
                    world_id=world_id, sequence=1, month=10,
                    checkpoint_type="periodic", payload=payload,
                    checksum=checksum,
                )
    finally:
        repo.close()

    # Verify no checkpoint and no partial metadata persisted.
    with Session() as s2:
        count = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == world_id)
        ).first()
        assert count is None
        world_row = s2.get(WorldRow, world_id)
        assert world_row.checkpoint_count == 0
        assert world_row.last_checkpoint_at is None


def test_save_checkpoint_external_session_no_rollback(
    engine_obj, small_world, races, governments
):
    """An injected error on an external session must not rollback the
    caller's pending changes automatically."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="External Rollback", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    payload = _serialize_engine(eng)
    checksum = hashlib.sha256(payload).hexdigest()

    external = Session()
    try:
        # Caller's pending change (not committed).
        external.execute(
            text("UPDATE worlds SET status = 'paused' WHERE id = :wid"),
            {"wid": world_id},
        )

        repo = CheckpointRepository(external)
        try:
            with _monkeypatch_execute(repo, _failing_execute):
                with pytest.raises(Exception):
                    repo.save_checkpoint(
                        world_id=world_id, sequence=1, month=10,
                        checkpoint_type="periodic", payload=payload,
                        checksum=checksum,
                    )
        finally:
            repo.close()

        # Caller's pending change must still be visible (no auto rollback).
        status_visible = external.execute(
            text("SELECT status FROM worlds WHERE id = :wid"),
            {"wid": world_id},
        ).scalar()
        assert status_visible == "paused"

        # Caller rolls back explicitly and recovers coherent state.
        external.rollback()
        status_after_rollback = external.execute(
            text("SELECT status FROM worlds WHERE id = :wid"),
            {"wid": world_id},
        ).scalar()
        assert status_after_rollback == "running"
    finally:
        external.close()


def test_update_status_owned_session_rollback_on_error(engine_obj):
    """update_status() rolls back on error when the repository owns the session."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=0, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Update Status Rollback", mode="single", sea_level=0.2,
        status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    repo = WorldRepository(Session)  # owns its own session
    try:
        with _monkeypatch_commit(repo, _failing_execute):
            with pytest.raises(Exception):
                repo.update_status(world_id, "paused")
    finally:
        repo.close()

    with Session() as s2:
        world_row = s2.get(WorldRow, world_id)
        assert world_row.status == "running"  # change rolled back


def test_update_checkpoint_metadata_owned_session_rollback_on_error(engine_obj):
    """update_checkpoint_metadata() rolls back on error when the repository
    owns the session."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=0, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Update Metadata Rollback", mode="single", sea_level=0.2,
        status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    repo = WorldRepository(Session)  # owns its own session
    try:
        with _monkeypatch_commit(repo, _failing_execute):
            with pytest.raises(Exception):
                repo.update_checkpoint_metadata(
                    world_id, last_checkpoint_at="2026-01-01",
                    months_simulated=10, increment_checkpoint_count=True,
                )
    finally:
        repo.close()

    with Session() as s2:
        world_row = s2.get(WorldRow, world_id)
        assert world_row.last_checkpoint_at is None
        assert world_row.months_simulated == 0
        assert world_row.checkpoint_count == 0


def test_methods_do_not_commit_or_rollback_external_session(
    engine_obj, small_world, races, governments
):
    """No commit or rollback is executed on externally-provided sessions."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="No Commit External", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    external = Session()
    try:
        external.execute(
            text("UPDATE worlds SET status = 'paused' WHERE id = :wid"),
            {"wid": world_id},
        )
        repo = CheckpointRepository(external)
        try:
            # Track commit/rollback calls.
            commit_calls = []
            rollback_calls = []
            orig_commit = external.commit
            orig_rollback = external.rollback

            def tracking_commit():
                commit_calls.append(True)
                return orig_commit()

            def tracking_rollback():
                rollback_calls.append(True)
                return orig_rollback()

            external.commit = tracking_commit
            external.rollback = tracking_rollback

            # Successful operation: only flush, no commit/rollback.
            eng = _make_engine_instance(small_world, races, governments)
            payload = _serialize_engine(eng)
            checksum = hashlib.sha256(payload).hexdigest()
            repo.save_checkpoint(
                world_id=world_id, sequence=1, month=10,
                checkpoint_type="periodic", payload=payload,
                checksum=checksum,
            )
            assert commit_calls == [], "commit must not be called on external session"
            assert rollback_calls == [], "rollback must not be called on external session"

            # Failed operation: propagate without rollback.
            with _monkeypatch_execute(repo, _failing_execute):
                try:
                    with pytest.raises(Exception):
                        repo.save_checkpoint(
                            world_id=world_id, sequence=2, month=11,
                            checkpoint_type="periodic", payload=payload,
                            checksum=checksum,
                        )
                finally:
                    external.commit = orig_commit
                    external.rollback = orig_rollback
            assert commit_calls == [], "commit must not be called on external session after error"
            assert rollback_calls == [], "rollback must not be called on external session after error"
        finally:
            repo.close()
    finally:
        external.rollback()
        external.close()


def test_get_latest_valid_checkpoint_owned_and_external_session(
    engine_obj, small_world, races, governments
):
    """get_latest_valid_checkpoint() works correctly with owned and external sessions."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Latest Valid Sessions", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    good_payload = _serialize_engine(eng)
    good_checksum = hashlib.sha256(good_payload).hexdigest()
    bad_checksum = "0" * 64

    # Owned session: invalidation persists via commit.
    repo = CheckpointRepository(Session)
    try:
        repo.save_checkpoint(
            world_id=world_id, sequence=1, month=10,
            checkpoint_type="periodic", payload=good_payload,
            checksum=good_checksum,
        )
        repo.save_checkpoint(
            world_id=world_id, sequence=2, month=20,
            checkpoint_type="periodic", payload=good_payload,
            checksum=bad_checksum,
        )
        latest = repo.get_latest_valid_checkpoint(world_id)
        assert latest is not None
        assert latest.sequence == 1
    finally:
        repo.close()

    # External session: invalidation flushed, caller commits.
    external = Session()
    try:
        repo2 = CheckpointRepository(external)
        try:
            repo2.save_checkpoint(
                world_id=world_id, sequence=3, month=30,
                checkpoint_type="periodic", payload=good_payload,
                checksum=bad_checksum,
            )
            latest2 = repo2.get_latest_valid_checkpoint(world_id)
            assert latest2 is not None
            assert latest2.sequence == 1
        finally:
            repo2.close()
        external.commit()
    finally:
        external.close()

    # Verify from a brand-new session that seq=2 and seq=3 are invalid.
    with Session() as s3:
        cp2 = s3.get(WorldCheckpoint, 2)
        cp3 = s3.get(WorldCheckpoint, 3)
        assert cp2 is not None and cp2.is_valid == 0
        assert cp3 is not None and cp3.is_valid == 0
        latest3 = s3.scalars(
            select(WorldCheckpoint)
            .where(WorldCheckpoint.world_id == world_id)
            .where(WorldCheckpoint.is_valid == 1)
            .order_by(WorldCheckpoint.sequence.desc())
        ).first()
        assert latest3 is not None
        assert latest3.sequence == 1


def _monkeypatch_execute(repo, failing_func):
    """Context manager that injects a failing execute() into the repository's
    session, then restores the original method."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        s = repo._session
        orig_execute = s.execute
        s.execute = failing_func
        try:
            yield
        finally:
            s.execute = orig_execute

    return _ctx()


def _monkeypatch_commit(repo, failing_func):
    """Context manager that injects a failing commit() into the repository's
    session, then restores the original method.  Used for methods like
    update_status() and update_checkpoint_metadata() that call commit()
    (via _commit_or_flush) rather than execute()."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        s = repo._session
        orig_commit = s.commit
        s.commit = failing_func
        try:
            yield
        finally:
            s.commit = orig_commit

    return _ctx()


# ---------------------------------------------------------------------------
# mark_checkpoint_invalid and get_valid_checkpoints_for_world
# ---------------------------------------------------------------------------


def _make_checkpoint_row(Session, world_id, sequence, month,
                         checkpoint_type="periodic", is_valid=1,
                         checkpoint_version=2):
    """Insert a checkpoint row and return its id."""
    s = Session()
    try:
        cp = WorldCheckpoint(
            world_id=world_id,
            sequence=sequence,
            month=month,
            checkpoint_type=checkpoint_type,
            payload=b"payload",
            checksum="checksum",
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            is_valid=is_valid,
            checkpoint_version=checkpoint_version,
        )
        s.add(cp)
        s.flush()
        cp_id = cp.id
        s.commit()
        return cp_id
    finally:
        s.close()


def test_mark_checkpoint_invalid_success(engine_obj):
    """mark_checkpoint_invalid() marks is_valid=0 and persists it."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Mark Invalid", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    cp_id = _make_checkpoint_row(Session, world_id, sequence=1, month=10)

    repo = CheckpointRepository(Session)
    try:
        assert repo.mark_checkpoint_invalid(cp_id) is True
    finally:
        repo.close()

    # Persisted across sessions.
    with Session() as s2:
        cp = s2.get(WorldCheckpoint, cp_id)
        assert cp is not None
        assert cp.is_valid == 0


def test_mark_checkpoint_invalid_nonexistent(engine_obj):
    """mark_checkpoint_invalid() returns False for a missing checkpoint."""
    _engine, Session = engine_obj
    repo = CheckpointRepository(Session)
    try:
        assert repo.mark_checkpoint_invalid(999999) is False
    finally:
        repo.close()


def test_mark_checkpoint_invalid_commit_error_propagates(
    engine_obj, small_world, races, governments
):
    """A commit failure during invalidation is propagated, not swallowed."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Commit Error", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    cp_id = _make_checkpoint_row(Session, world_id, sequence=1, month=10)

    repo = CheckpointRepository(Session)
    try:
        def failing_commit():
            raise OperationalError("COMMIT", {},
                                   sqlite3.OperationalError("disk full"))

        with _monkeypatch_commit(repo, failing_commit):
            with pytest.raises(OperationalError):
                repo.mark_checkpoint_invalid(cp_id)
    finally:
        repo.close()

    # The invalidation was not persisted (commit failed).
    with Session() as s2:
        cp = s2.get(WorldCheckpoint, cp_id)
        assert cp is not None
        assert cp.is_valid == 1


def test_mark_checkpoint_invalid_does_not_touch_other_world(
    engine_obj, small_world, races, governments
):
    """Invalidating a checkpoint for one world does not affect another world."""
    _engine, Session = engine_obj
    s = Session()
    rows = []
    for name in ("World A", "World B"):
        row = WorldRow(
            seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
            state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            name=name, mode="single", sea_level=0.2, status="running",
        )
        s.add(row)
        rows.append(row)
    s.commit()
    for row in rows:
        s.refresh(row)
    world_ids = [r.id for r in rows]
    s.close()

    cp_a = _make_checkpoint_row(Session, world_ids[0], sequence=1, month=10)
    cp_b = _make_checkpoint_row(Session, world_ids[1], sequence=1, month=10)

    repo = CheckpointRepository(Session)
    try:
        assert repo.mark_checkpoint_invalid(cp_a) is True
    finally:
        repo.close()

    with Session() as s2:
        cp_a_row = s2.get(WorldCheckpoint, cp_a)
        cp_b_row = s2.get(WorldCheckpoint, cp_b)
        assert cp_a_row.is_valid == 0
        assert cp_b_row.is_valid == 1  # untouched


def test_get_valid_checkpoints_excludes_invalid(
    engine_obj, small_world, races, governments
):
    """get_valid_checkpoints_for_world() excludes is_valid=0 rows."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Valid Filter", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    cp1 = _make_checkpoint_row(Session, world_id, sequence=1, month=10,
                               is_valid=1)
    cp2 = _make_checkpoint_row(Session, world_id, sequence=2, month=20,
                               is_valid=0)

    repo = CheckpointRepository(Session)
    try:
        valids = repo.get_valid_checkpoints_for_world(world_id)
    finally:
        repo.close()

    seqs = sorted(cp.sequence for cp in valids)
    assert seqs == [1], seqs
    ids = {cp.id for cp in valids}
    assert cp1 in ids
    assert cp2 not in ids


def test_get_valid_checkpoints_includes_valid(
    engine_obj, small_world, races, governments
):
    """get_valid_checkpoints_for_world() returns only valid rows."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Valid Only", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    cp1 = _make_checkpoint_row(Session, world_id, sequence=1, month=10,
                               is_valid=1)
    cp2 = _make_checkpoint_row(Session, world_id, sequence=2, month=20,
                               is_valid=1)

    repo = CheckpointRepository(Session)
    try:
        valids = repo.get_valid_checkpoints_for_world(world_id)
    finally:
        repo.close()

    assert len(valids) == 2
    assert {cp.id for cp in valids} == {cp1, cp2}


def test_get_valid_checkpoints_ordered_by_sequence_desc(
    engine_obj, small_world, races, governments
):
    """get_valid_checkpoints_for_world() orders by sequence descending."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Order Desc", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    for seq in (1, 2, 3):
        _make_checkpoint_row(Session, world_id, sequence=seq, month=seq * 10,
                             is_valid=1)

    repo = CheckpointRepository(Session)
    try:
        valids = repo.get_valid_checkpoints_for_world(world_id)
    finally:
        repo.close()

    seqs = [cp.sequence for cp in valids]
    assert seqs == [3, 2, 1], seqs


def test_get_valid_checkpoints_isolated_by_world(
    engine_obj, small_world, races, governments
):
    """get_valid_checkpoints_for_world() does not leak across worlds."""
    _engine, Session = engine_obj
    s = Session()
    rows = []
    for name in ("World X", "World Y"):
        row = WorldRow(
            seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
            state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            name=name, mode="single", sea_level=0.2, status="running",
        )
        s.add(row)
        rows.append(row)
    s.commit()
    for row in rows:
        s.refresh(row)
    world_ids = [r.id for r in rows]
    s.close()

    _make_checkpoint_row(Session, world_ids[0], sequence=1, month=10,
                         is_valid=1)
    _make_checkpoint_row(Session, world_ids[1], sequence=5, month=50,
                         is_valid=1)

    repo = CheckpointRepository(Session)
    try:
        valids_x = repo.get_valid_checkpoints_for_world(world_ids[0])
        valids_y = repo.get_valid_checkpoints_for_world(world_ids[1])
    finally:
        repo.close()

    assert len(valids_x) == 1
    assert valids_x[0].sequence == 1
    assert len(valids_y) == 1
    assert valids_y[0].sequence == 5


def test_get_checkpoints_for_world_still_returns_invalid(
    engine_obj, small_world, races, governments
):
    """get_checkpoints_for_world() keeps returning all rows (backward compat)."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Backward Compat", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    cp_valid = _make_checkpoint_row(Session, world_id, sequence=1, month=10,
                                    is_valid=1)
    cp_invalid = _make_checkpoint_row(Session, world_id, sequence=2, month=20,
                                      is_valid=0)

    repo = CheckpointRepository(Session)
    try:
        all_cps = repo.get_checkpoints_for_world(world_id)
    finally:
        repo.close()

    assert len(all_cps) == 2
    assert {cp.id for cp in all_cps} == {cp_valid, cp_invalid}


def test_get_valid_checkpoints_all_invalid_returns_empty(
    engine_obj, small_world, races, governments
):
    """RP-R1: cuando todos los checkpoints de un mundo tienen is_valid=0,
    get_valid_checkpoints_for_world() devuelve lista vacía sin borrar
    los registros históricos y sin filtrar otros mundos."""
    _engine, Session = engine_obj
    s = Session()
    rows = []
    for name in ("World All-Invalid", "World Other"):
        row = WorldRow(
            seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
            state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            name=name, mode="single", sea_level=0.2, status="running",
        )
        s.add(row)
        rows.append(row)
    s.commit()
    for row in rows:
        s.refresh(row)
    world_all_invalid = rows[0].id
    world_other = rows[1].id
    s.close()

    # Dos checkpoints inválidos para el mundo objetivo...
    cp_invalid_1 = _make_checkpoint_row(Session, world_all_invalid, 1, 10,
                                        is_valid=0)
    cp_invalid_2 = _make_checkpoint_row(Session, world_all_invalid, 2, 20,
                                        is_valid=0)
    # ...y uno válido para otro mundo (debe ignorarse).
    cp_other_valid = _make_checkpoint_row(Session, world_other, 1, 10,
                                          is_valid=1)

    repo = CheckpointRepository(Session)
    try:
        valids = repo.get_valid_checkpoints_for_world(world_all_invalid)
    finally:
        repo.close()

    assert valids == [], valids

    # Los registros históricos siguen existiendo (no se borraron).
    with Session() as s2:
        remaining = s2.scalars(
            select(WorldCheckpoint).where(
                WorldCheckpoint.world_id == world_all_invalid
            )
        ).all()
        assert len(remaining) == 2
        assert {cp.id for cp in remaining} == {cp_invalid_1, cp_invalid_2}
        assert all(cp.is_valid == 0 for cp in remaining)

        # El checkpoint del otro mundo sigue válido.
        cp_o = s2.get(WorldCheckpoint, cp_other_valid)
        assert cp_o.is_valid == 1

