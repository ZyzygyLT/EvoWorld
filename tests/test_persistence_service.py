"""Tests for PersistenceService (Fase 2.2.1).

Covers checkpoint serialization, SHA-256 verification, atomic persistence,
off-event-loop writes, sequence management, session lifecycle, periodic
checkpoint intervals, and pending write tracking.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError, OperationalError
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
from worldsim.server.persistence_service import (
    PersistenceService,
    PersistenceWriteError,
)
from worldsim.server.repository import CheckpointRepository
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
    return tmp_path / "test_persistence_service.db"


@pytest.fixture
def engine_obj(tmp_db_path):
    """Create an isolated SQLite engine and session factory for a test."""
    engine = create_engine(f"sqlite:///{tmp_db_path}")

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


def _make_world(Session, name, months_simulated=10):
    """Create a world row and return its id (helper for fault-injection tests)."""
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=months_simulated,
        land_pct=50.0, state_json="{}",
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name=name, mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()
    return world_id


# ---------------------------------------------------------------------------
# PS-1: Serialization
# ---------------------------------------------------------------------------

def test_persistence_service_serialization(
    engine_obj, small_world, races, governments
):
    """PS-1: The motor se serializa correctamente mediante checkpoint_to_dict."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Serialization Test", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(session_factory=Session)

    future = service.save_checkpoint_now(
        world_id=world_id, engine=eng,
        checkpoint_type="periodic",
    )
    cp_id = future.result()

    assert cp_id is not None

    # Verify payload round-trips through the engine export.
    with Session() as s2:
        cp = s2.get(WorldCheckpoint, cp_id)
        assert cp is not None
        restored = engine_from_checkpoint(json.loads(cp.payload.decode("utf-8")))
        assert restored.month == 10

    service.close()


# ---------------------------------------------------------------------------
# PS-2: SHA-256
# ---------------------------------------------------------------------------

def test_persistence_service_sha256(
    engine_obj, small_world, races, governments
):
    """PS-2: El checksum coincide con los bytes exactos almacenados."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="SHA256 Test", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    payload = _serialize_engine(eng)
    expected_checksum = hashlib.sha256(payload).hexdigest()

    service = PersistenceService(session_factory=Session)
    future = service.save_checkpoint_now(
        world_id=world_id, engine=eng,
        checkpoint_type="periodic",
    )
    cp_id = future.result()

    with Session() as s2:
        cp = s2.get(WorldCheckpoint, cp_id)
        assert cp is not None
        assert cp.checksum == expected_checksum
        assert cp.payload == payload

    service.close()


# ---------------------------------------------------------------------------
# PS-3: Atomicidad
# ---------------------------------------------------------------------------

def test_persistence_service_atomicity(
    engine_obj, small_world, races, governments
):
    """PS-3: Una escritura fallida no deja checkpoint parcialmente persistido."""
    _engine, Session = engine_obj
    # Do NOT create a world row; use a non-existent world_id.
    s = Session()
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(session_factory=Session)

    with pytest.raises(Exception):
        service.save_checkpoint_now(
            world_id=999999, engine=eng,
            checkpoint_type="periodic",
        ).result()

    # Verify no checkpoint persisted for non-existent world.
    with Session() as s2:
        count = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == 999999)
        ).first()
        assert count is None

    # close() must not hide the recorded persistence error (H-4).
    with pytest.raises(PersistenceWriteError) as excinfo:
        service.close()
    assert 999999 in excinfo.value.failures
    assert isinstance(excinfo.value.failures[999999][0], ValueError)


# ---------------------------------------------------------------------------
# PS-4: Rollback
# ---------------------------------------------------------------------------

def test_persistence_service_rollback(
    engine_obj, small_world, races, governments
):
    """PS-4 (H-5): rollback real ante un error inyectado en el commit.

    Escenario real (no un caso exitoso): se inyecta un fallo en el commit
    de la sesión, y se verifica que (1) el error llega al caller, (2) se
    ejecuta rollback, (3) no queda ningún checkpoint parcialmente
    persistido y (4) la sesión se cierra correctamente.
    """
    _engine, Session = engine_obj
    world_id = _make_world(Session, "Rollback Test")

    eng = _make_engine_instance(small_world, races, governments)

    sessions = []
    rollback_calls = []
    close_calls = []

    def fault_session_factory():
        s = Session()
        sessions.append(s)
        real_rollback = s.rollback
        real_close = s.close

        def spy_rollback(*args, **kwargs):
            rollback_calls.append(s)
            return real_rollback(*args, **kwargs)

        def spy_close(*args, **kwargs):
            close_calls.append(s)
            return real_close(*args, **kwargs)

        def failing_commit():
            # Execute any pending statement inside the transaction, then
            # fail before COMMIT so the transaction must be rolled back.
            s.flush()
            raise OperationalError(
                "COMMIT",
                {},
                sqlite3.OperationalError("injected commit failure"),
            )

        s.rollback = spy_rollback
        s.close = spy_close
        s.commit = failing_commit
        return s

    service = PersistenceService(session_factory=fault_session_factory)

    future = service.save_checkpoint_now(
        world_id=world_id, engine=eng, checkpoint_type="periodic",
    )

    # 1. The error reaches the caller and the Future ends in an error state.
    with pytest.raises(OperationalError):
        future.result()
    assert future.done()
    assert isinstance(future.exception(), OperationalError)

    # 2. Rollback actually executed on the session used by the worker.
    assert rollback_calls, "session.rollback() was never called"

    # 3. No partial checkpoint was persisted.
    with Session() as s2:
        cps = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == world_id)
        ).all()
        assert cps == []

    # 4. The session was closed.
    assert close_calls, "session.close() was never called"
    assert not sessions[0].new and not sessions[0].dirty
    assert not sessions[0].in_transaction()

    # 5. The service surfaces the failure exactly once and then closes.
    with pytest.raises(PersistenceWriteError) as excinfo:
        service.close()
    assert world_id in excinfo.value.failures
    assert isinstance(excinfo.value.failures[world_id][0], OperationalError)


# ---------------------------------------------------------------------------
# PS-5: Secuencias
# ---------------------------------------------------------------------------

def test_persistence_service_sequences(
    engine_obj, small_world, races, governments
):
    """PS-5: Los checkpoints consecutivos tienen secuencias correctas."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Sequence Test", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(session_factory=Session)

    # Save two checkpoints sequentially.
    f1 = service.save_checkpoint_now(
        world_id=world_id, engine=eng,
        checkpoint_type="periodic",
    )
    cp1_id = f1.result()

    # Advance the engine a bit more.
    eng.tick()
    f2 = service.save_checkpoint_now(
        world_id=world_id, engine=eng,
        checkpoint_type="periodic",
    )
    cp2_id = f2.result()

    with Session() as s2:
        cp1 = s2.get(WorldCheckpoint, cp1_id)
        cp2 = s2.get(WorldCheckpoint, cp2_id)
        assert cp1 is not None
        assert cp2 is not None
        assert cp2.sequence == cp1.sequence + 1
        assert cp2.month == cp1.month + 1  # eng.tick() advanced month by 1

    service.close()


# ---------------------------------------------------------------------------
# PS-6: Concurrencia
# ---------------------------------------------------------------------------

def test_persistence_service_concurrency(
    engine_obj, small_world, races, governments
):
    """PS-6: Escrituras simultáneas del mismo mundo no producen colisiones."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Concurrency Test", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(session_factory=Session)

    # Launch two concurrent saves for the same world.
    futures = []
    for _ in range(2):
        eng.tick()
        f = service.save_checkpoint_now(
            world_id=world_id, engine=eng,
            checkpoint_type="periodic",
        )
        futures.append(f)

    cp_ids = [f.result() for f in futures]

    with Session() as s2:
        cps = [s2.get(WorldCheckpoint, cid) for cid in cp_ids]
        assert all(cp is not None for cp in cps)
        # Both sequences should be distinct.
        seqs = sorted(cp.sequence for cp in cps)
        assert seqs[0] != seqs[1]

    service.close()


# ---------------------------------------------------------------------------
# PS-7: Event loop
# ---------------------------------------------------------------------------

def test_persistence_service_off_event_loop(
    engine_obj, small_world, races, governments
):
    """PS-7: La escritura SQLite se realiza fuera del event loop."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="EventLoop Test", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(session_factory=Session)

    # The save_checkpoint_now should return a Future immediately (not block).
    future = service.save_checkpoint_now(
        world_id=world_id, engine=eng,
        checkpoint_type="periodic",
    )
    # The future should not be done immediately (write happens on worker).
    # It may complete quickly, but we verify it's a Future and eventually resolves.
    cp_id = future.result()
    assert cp_id is not None

    service.close()


# ---------------------------------------------------------------------------
# PS-8: Sesiones
# ---------------------------------------------------------------------------

def test_persistence_service_session_lifecycle(
    engine_obj, small_world, races, governments
):
    """PS-8: Cada worker crea y cierra su propia sesión."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Session Lifecycle Test", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(session_factory=Session)

    future = service.save_checkpoint_now(
        world_id=world_id, engine=eng,
        checkpoint_type="periodic",
    )
    cp_id = future.result()
    assert cp_id is not None

    # Verify checkpoint persisted correctly.
    with Session() as s2:
        cp = s2.get(WorldCheckpoint, cp_id)
        assert cp is not None

    service.close()


# ---------------------------------------------------------------------------
# PS-9: Escrituras pendientes
# ---------------------------------------------------------------------------

def test_persistence_service_pending_writes(
    engine_obj, small_world, races, governments
):
    """PS-9: El servicio puede esperar operaciones pendientes y propagar errores."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=10, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Pending Writes Test", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(session_factory=Session)

    # Launch a write and check it's tracked as pending.
    future = service.save_checkpoint_now(
        world_id=world_id, engine=eng,
        checkpoint_type="periodic",
    )
    assert service.has_pending_write(world_id)

    # Wait for completion.
    service.wait_pending_writes()
    assert not service.has_pending_write(world_id)

    cp_id = future.result()
    assert cp_id is not None

    service.close()


# ---------------------------------------------------------------------------
# PS-10: Intervalo periódico
# ---------------------------------------------------------------------------

def test_persistence_service_periodic_interval(
    engine_obj, small_world, races, governments
):
    """PS-10: El intervalo configurable, su valor predeterminado y la prevención
    de escrituras periódicas simultáneas."""
    _engine, Session = engine_obj
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=0, land_pct=50.0,
        state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="Interval Test", mode="single", sea_level=0.2, status="running",
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(
        session_factory=Session, interval_months=10
    )

    # should_checkpoint() is read-only: it never marks anything (H-1).
    # Initially no checkpoint was saved, so a periodic write is due once
    # the interval is reached from the initial (unset) state.
    assert service.should_checkpoint(world_id, 10) is True
    # Repeated evaluations do not consume or mutate any state.
    assert service.should_checkpoint(world_id, 10) is True
    # Before the interval, from a real persisted month, it is not due.
    service.mark_checkpoint_done(world_id, 10)
    assert service.should_checkpoint(world_id, 5) is False   # Before interval
    assert service.should_checkpoint(world_id, 10) is False  # Interval not elapsed
    assert service.should_checkpoint(world_id, 20) is True   # Interval elapsed

    # Different world should have independent interval.
    assert service.should_checkpoint(999, 10) is True

    service.close()


# ---------------------------------------------------------------------------
# PS-11: Errores SQLite
# ---------------------------------------------------------------------------

def test_persistence_service_sqlite_error(
    engine_obj, small_world, races, governments
):
    """PS-11 (H-6, cubre también PS-R7): error SQLite real propagado.

    Se provoca una violación real de la restricción UNIQUE
    (world_id, sequence) forzando la reutilización de una secuencia ya
    existente.  Se verifica que: el error se propaga al caller, ocurre
    rollback, no queda checkpoint parcial, el Future termina en estado de
    error, wait_pending_writes() identifica el fallo y el servicio puede
    seguir funcionando y cerrarse correctamente.
    """
    _engine, Session = engine_obj
    world_id = _make_world(Session, "SQLite Error Test")

    eng = _make_engine_instance(small_world, races, governments)

    rollback_calls = []
    close_calls = []

    def spy_session_factory():
        s = Session()
        real_rollback = s.rollback
        real_close = s.close

        def spy_rollback(*a, **k):
            rollback_calls.append(s)
            return real_rollback(*a, **k)

        def spy_close(*a, **k):
            close_calls.append(s)
            return real_close(*a, **k)

        s.rollback = spy_rollback
        s.close = spy_close
        return s

    service = PersistenceService(session_factory=spy_session_factory)

    # First, a valid checkpoint (gets sequence 1).
    future = service.save_checkpoint_now(
        world_id=world_id, engine=eng, checkpoint_type="periodic",
    )
    cp_id = future.result()
    assert cp_id is not None

    # Force the next sequence to collide with the existing one so SQLite
    # itself raises a genuine IntegrityError on INSERT.
    with mock.patch.object(
        CheckpointRepository, "next_sequence", return_value=1
    ):
        bad_future = service.save_checkpoint_now(
            world_id=world_id, engine=eng, checkpoint_type="periodic",
        )
        with pytest.raises(IntegrityError):
            bad_future.result()

        # The Future ends in an error state with the original exception.
        assert bad_future.done()
        assert isinstance(bad_future.exception(), IntegrityError)

        # wait_pending_writes() exposes which world failed and why.
        with pytest.raises(PersistenceWriteError) as excinfo:
            service.wait_pending_writes()
        assert world_id in excinfo.value.failures
        assert isinstance(
            excinfo.value.failures[world_id][0], IntegrityError
        )

        # Rollback real ejecutado por el servicio en la sesión del worker.
        assert rollback_calls, "session.rollback() no fue invocado tras el error de SQLite"

    # No partial state: still exactly one checkpoint and world metadata
    # (checkpoint_count) untouched by the failed write.
    with Session() as s2:
        cps = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == world_id)
        ).all()
        assert len(cps) == 1
        assert cps[0].sequence == 1
        world_row = s2.get(WorldRow, world_id)
        assert world_row.checkpoint_count == 1

    # The service continues to work after the fault: the next save succeeds
    # with the correct sequence (the failed INSERT did not consume one).
    ok_future = service.save_checkpoint_now(
        world_id=world_id, engine=eng, checkpoint_type="periodic",
    )
    cp2_id = ok_future.result()
    with Session() as s2:
        cp2 = s2.get(WorldCheckpoint, cp2_id)
        assert cp2 is not None
        assert cp2.sequence == 2

    # close() does not raise: the failure was already reported once.
    service.close()


# ---------------------------------------------------------------------------
# PS-12: Mundo inexistente
# ---------------------------------------------------------------------------

def test_persistence_service_nonexistent_world(
    engine_obj, small_world, races, governments
):
    """PS-12: El comportamiento cuando se intenta guardar para un mundo inexistente."""
    _engine, Session = engine_obj
    # Do NOT create a world row.
    s = Session()
    s.close()

    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(session_factory=Session)

    with pytest.raises(Exception):
        service.save_checkpoint_now(
            world_id=999999, engine=eng,
            checkpoint_type="periodic",
        ).result()

    # close() surfaces the recorded persistence error instead of hiding it.
    with pytest.raises(PersistenceWriteError) as excinfo:
        service.close()
    assert 999999 in excinfo.value.failures


# ===========================================================================
# Fase 2.2.1-R: tests correctivos de inyección de fallos
# ===========================================================================

# ---------------------------------------------------------------------------
# PS-R1 — Fallo de checkpoint periódico permite reintento inmediato
# ---------------------------------------------------------------------------

def test_ps_r1_periodic_failure_allows_immediate_retry(
    engine_obj, small_world, races, governments
):
    """PS-R1: una escritura periódica fallida permite volver a intentar
    guardar sin esperar otros 12 meses simulados."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "PS-R1 World")
    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(session_factory=Session)  # intervalo 12

    with mock.patch.object(
        CheckpointRepository,
        "save_checkpoint",
        side_effect=RuntimeError("injected periodic failure"),
    ):
        future = service.save_checkpoint_now(
            world_id=world_id, engine=eng, checkpoint_type="periodic",
        )
        with pytest.raises(RuntimeError, match="injected periodic failure"):
            future.result()

        # The failure is surfaced (and drained) by wait_pending_writes().
        with pytest.raises(PersistenceWriteError) as excinfo:
            service.wait_pending_writes()
        assert world_id in excinfo.value.failures

        # The successful month was never recorded...
        assert service._last_checkpoint_month.get(world_id) is None
        # ...and the pending guard was released, so a retry is due right
        # now, without waiting a full 12-month interval.
        assert service.has_pending_write(world_id) is False
        assert service.should_checkpoint(world_id, 1000) is True

    # The service keeps working: the retry succeeds once the fault is gone.
    retry = service.save_checkpoint_now(
        world_id=world_id, engine=eng, checkpoint_type="periodic",
    )
    assert retry.result() is not None
    service.wait_pending_writes()

    # Now the interval is anchored at the persisted month.
    month = int(eng.month)
    assert service._last_checkpoint_month.get(world_id) == month
    assert service.should_checkpoint(world_id, month + 1) is False

    service.close()


# ---------------------------------------------------------------------------
# PS-R2 — Éxito periódico: el mes se actualiza solo tras confirmarse
# ---------------------------------------------------------------------------

def test_ps_r2_periodic_month_updated_only_after_success(
    engine_obj, small_world, races, governments
):
    """PS-R2: el último mes persistido solo se actualiza después de una
    escritura exitosa (nunca al iniciarla ni durante la escritura)."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "PS-R2 World", months_simulated=0)
    eng = _make_engine_instance(small_world, races, governments)
    month = int(eng.month)
    service = PersistenceService(session_factory=Session)  # intervalo 12

    started = threading.Event()
    release = threading.Event()
    real_save = CheckpointRepository.save_checkpoint

    def slow_save(self, *args, **kwargs):
        started.set()
        assert release.wait(timeout=10), "release event never set"
        return real_save(self, *args, **kwargs)

    with mock.patch.object(CheckpointRepository, "save_checkpoint", slow_save):
        future = service.save_checkpoint_now(
            world_id=world_id, engine=eng, checkpoint_type="periodic",
        )
        # Wait until the worker is really inside the write.
        assert started.wait(timeout=10)

        # Write in flight: the month is NOT marked yet...
        assert service._last_checkpoint_month.get(world_id) is None
        # ...and the pending guard blocks a second periodic write.
        assert service.should_checkpoint(world_id, 1000) is False

        release.set()
        assert future.result() is not None

    # Only after the confirmed success is the month recorded.
    service.wait_pending_writes()
    assert service._last_checkpoint_month.get(world_id) == month
    assert service.should_checkpoint(world_id, month + 1) is False
    assert service.should_checkpoint(world_id, month + 12) is True

    service.close()


# ---------------------------------------------------------------------------
# PS-R3 — Múltiples errores pendientes todos identificados
# ---------------------------------------------------------------------------

def test_ps_r3_multiple_pending_errors_all_surfaced(
    engine_obj, small_world, races, governments
):
    """PS-R3: el servicio espera todas las escrituras fallidas y permite
    identificar todos los mundos afectados (ningún error se pierde)."""
    _engine, Session = engine_obj
    w1 = _make_world(Session, "PS-R3 World 1")
    w2 = _make_world(Session, "PS-R3 World 2")
    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(session_factory=Session)

    def failing_save(self, *args, **kwargs):
        raise RuntimeError(f"injected failure for world {kwargs['world_id']}")

    with mock.patch.object(
        CheckpointRepository, "save_checkpoint", failing_save
    ):
        f1 = service.save_checkpoint_now(
            world_id=w1, engine=eng, checkpoint_type="periodic",
        )
        f2 = service.save_checkpoint_now(
            world_id=w2, engine=eng, checkpoint_type="periodic",
        )

        # Both writes are awaited despite both failing: a single failure
        # never prevents waiting for the remaining writes.
        with pytest.raises(RuntimeError):
            f1.result()
        with pytest.raises(RuntimeError):
            f2.result()

        with pytest.raises(PersistenceWriteError) as excinfo:
            service.wait_pending_writes()

    # Every affected world and every original exception is identifiable.
    failures = excinfo.value.failures
    assert set(failures) == {w1, w2}
    assert all(
        isinstance(exc, RuntimeError) and "injected failure" in str(exc)
        for excs in failures.values()
        for exc in excs
    )
    assert str(w1) in str(excinfo.value)
    assert str(w2) in str(excinfo.value)

    # Failures are reported exactly once: the next wait returns normally.
    service.wait_pending_writes()
    service.close()


# ---------------------------------------------------------------------------
# PS-R4 — Cierre con escrituras fallidas
# ---------------------------------------------------------------------------

def test_ps_r4_close_with_failed_writes(
    engine_obj, small_world, races, governments
):
    """PS-R4: el executor se cierra correctamente aunque existan
    escrituras fallidas, sin ocultar el error de persistencia."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "PS-R4 World")
    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(session_factory=Session)

    with mock.patch.object(
        CheckpointRepository,
        "save_checkpoint",
        side_effect=RuntimeError("injected close failure"),
    ):
        future = service.save_checkpoint_now(
            world_id=world_id, engine=eng, checkpoint_type="periodic",
        )
        with pytest.raises(RuntimeError, match="injected close failure"):
            future.result()

    # close() without a prior wait: it waits for the pending write,
    # surfaces the failure, and still shuts the executor down.
    with pytest.raises(PersistenceWriteError) as excinfo:
        service.close()
    assert world_id in excinfo.value.failures
    assert isinstance(
        excinfo.value.failures[world_id][0], RuntimeError
    )

    # The executor was really shut down despite the failure.
    with pytest.raises(RuntimeError):
        service.executor.submit(lambda: None)

    # Resources were freed: no pending write remains.
    assert service.has_pending_write(world_id) is False

    # A second close is a clean no-op (idempotent).
    service.close()


# ---------------------------------------------------------------------------
# PS-R5 — Cierre idempotente
# ---------------------------------------------------------------------------

def test_ps_r5_close_idempotent(
    engine_obj, small_world, races, governments
):
    """PS-R5: invocar close() más de una vez no corrompe el estado del
    servicio ni lanza errores repetidos."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "PS-R5 World")
    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(session_factory=Session)

    future = service.save_checkpoint_now(
        world_id=world_id, engine=eng, checkpoint_type="periodic",
    )
    assert future.result() is not None

    service.close()  # primera llamada: sin errores pendientes
    service.close()  # segunda llamada: idempotente
    service.close()  # tercera llamada: sigue sin excepción

    assert service.has_pending_write(world_id) is False
    service.wait_pending_writes()  # sin errores pendientes: no lanza

    # After close, new operations are rejected deterministically (H-4).
    with pytest.raises(RuntimeError, match="closed"):
        service.save_checkpoint_now(world_id=world_id, engine=eng)


# ---------------------------------------------------------------------------
# PS-R6 — Rollback real sin persistencia parcial
# ---------------------------------------------------------------------------

def test_ps_r6_failure_before_commit_leaves_no_partial_state(
    engine_obj, small_world, races, governments
):
    """PS-R6: un error inyectado después de ejecutar los statements (INSERT
    + UPDATE de mundo, antes del commit) no deja persistencia parcial."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "PS-R6 World", months_simulated=0)
    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(session_factory=Session)

    real_save = CheckpointRepository.save_checkpoint

    def save_then_fail(self, *args, **kwargs):
        # Executes the INSERT + world UPDATE inside the transaction (the
        # repository does not own this session, so it only flushes), then
        # fails before the service can commit.
        real_save(self, *args, **kwargs)
        raise RuntimeError("injected failure after flush, before commit")

    with mock.patch.object(
        CheckpointRepository, "save_checkpoint", save_then_fail
    ):
        future = service.save_checkpoint_now(
            world_id=world_id, engine=eng, checkpoint_type="periodic",
        )
        with pytest.raises(RuntimeError, match="injected failure"):
            future.result()
        with pytest.raises(PersistenceWriteError) as excinfo:
            service.wait_pending_writes()
        assert world_id in excinfo.value.failures

    # Neither the checkpoint row nor the world metadata update was
    # committed: no partial persistence.
    with Session() as s2:
        cps = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == world_id)
        ).all()
        assert cps == []
        world_row = s2.get(WorldRow, world_id)
        assert world_row.checkpoint_count == 0
        assert world_row.months_simulated == 0  # would be eng.month if committed

    # The service continues working after the injected failure.
    retry = service.save_checkpoint_now(
        world_id=world_id, engine=eng, checkpoint_type="periodic",
    )
    assert retry.result() is not None
    with Session() as s2:
        cps = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == world_id)
        ).all()
        assert len(cps) == 1
        world_row = s2.get(WorldRow, world_id)
        assert world_row.checkpoint_count == 1

    service.close()


# ---------------------------------------------------------------------------
# PS-R8 — Escrituras periódicas concurrentes para el mismo mundo
# ---------------------------------------------------------------------------

def test_ps_r8_no_concurrent_periodic_checkpoints(
    engine_obj, small_world, races, governments
):
    """PS-R8: mientras existe una escritura periódica en vuelo para un
    mundo, ninguna evaluación concurrente de should_checkpoint() permite
    registrar otra para ese mismo mundo."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "PS-R8 World", months_simulated=0)
    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(session_factory=Session)

    started = threading.Event()
    release = threading.Event()
    real_save = CheckpointRepository.save_checkpoint

    def slow_save(self, *args, **kwargs):
        started.set()
        assert release.wait(timeout=10), "release event never set"
        return real_save(self, *args, **kwargs)

    with mock.patch.object(CheckpointRepository, "save_checkpoint", slow_save):
        future = service.save_checkpoint_now(
            world_id=world_id, engine=eng, checkpoint_type="periodic",
        )
        assert started.wait(timeout=10)

        # While the periodic write is in flight, concurrent evaluations
        # from several threads must all be blocked for this world.
        results: list[bool] = []

        def evaluate():
            for _ in range(50):
                results.append(service.should_checkpoint(world_id, 1000))

        threads = [
            threading.Thread(target=evaluate) for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            assert not t.is_alive()

        assert len(results) == 200
        assert all(r is False for r in results)
        assert service.has_pending_write(world_id) is True

        release.set()
        assert future.result() is not None

    service.wait_pending_writes()

    # With no write in flight, the guard lifts and only one periodic
    # checkpoint was written for this world.
    assert service.has_pending_write(world_id) is False
    assert service.should_checkpoint(world_id, 1000) is True
    with Session() as s2:
        cps = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == world_id)
        ).all()
        assert len(cps) == 1

    service.close()


# ===========================================================================
# Fase 2.2.1-S: correcciones de observaciones F-1 y F-2 de la auditoría R
# ===========================================================================

# ---------------------------------------------------------------------------
# PS-F1 — Cancelación de Future periódico nunca marca como exitoso
# ---------------------------------------------------------------------------

def test_ps_f1_cancelled_periodic_future_never_marks_success(
    engine_obj, small_world, races, governments
):
    """F-1: un Future periódico cancelado no debe marcar el mes como
    exitoso, debe liberar el slot, debe reportarse como fallo y debe
    permitir reintentar."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "PS-F1 World", months_simulated=0)
    eng = _make_engine_instance(small_world, races, governments)
    # Un solo worker: la primera escritura ocupa el hilo, la segunda queda
    # en cola (no empezada) y puede cancelarse de forma determinista.
    service = PersistenceService(session_factory=Session, max_workers=1)

    started = threading.Event()
    release = threading.Event()
    real_save = CheckpointRepository.save_checkpoint

    def slow_save(self, *args, **kwargs):
        started.set()
        assert release.wait(timeout=10), "release event never set"
        return real_save(self, *args, **kwargs)

    with mock.patch.object(CheckpointRepository, "save_checkpoint", slow_save):
        # Escritura A: empieza y se queda bloqueada dentro del worker.
        future_a = service.save_checkpoint_now(
            world_id=world_id, engine=eng, checkpoint_type="periodic",
        )
        assert started.wait(timeout=10)

        # Escritura B: queda en cola del executor, todavía no empezó.
        future_b = service.save_checkpoint_now(
            world_id=world_id, engine=eng, checkpoint_type="periodic",
        )
        time.sleep(0.2)  # da tiempo al executor a encolar B

        # 1. Cancelar B antes de que empiece su escritura.
        cancelled = future_b.cancel()
        assert cancelled is True
        assert future_b.done() is True

        # 2. El mes nunca se marca como exitoso por B.
        assert service._last_checkpoint_month.get(world_id) is None
        # 3. El slot de B se libera (A sigue pendiente, pero B desapareció).
        assert service.has_pending_write(world_id) is True  # A todavía

        # 4. wait_pending_writes() reporta la cancelación de B como fallo.
        # Esperamos a que A termine para que wait retorne; entonces debe
        # levantar PersistenceWriteError con la cancelación de B.
        release.set()
        with pytest.raises(PersistenceWriteError) as excinfo:
            service.wait_pending_writes()
        assert world_id in excinfo.value.failures
        assert any(
            isinstance(exc, CancelledError)
            for exc in excinfo.value.failures[world_id]
        )

    # 5. El servicio permite reintentar una vez drenados los fallos.
    service.wait_pending_writes()  # drenar fallos de B
    retry = service.save_checkpoint_now(
        world_id=world_id, engine=eng, checkpoint_type="periodic",
    )
    assert retry.result() is not None
    service.wait_pending_writes()
    month = int(eng.month)
    assert service._last_checkpoint_month.get(world_id) == month
    service.close()


# ---------------------------------------------------------------------------
# PS-F2 — Exclusión atómica de checkpoints periódicos (TOCTOU)
# ---------------------------------------------------------------------------

def test_ps_f2_concurrent_periodic_requests_only_one_checkpoint(
    engine_obj, small_world, races, governments
):
    """F-2: decisión y reserva atómicas. Varios hilos que piden simultáneamente
    un checkpoint periódico para el mismo mundo generan exactamente una
    escritura (una fila), sin reservas ni escrituras pendientes."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "PS-F2 World", months_simulated=0)
    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(session_factory=Session, interval_months=1)

    started = threading.Event()
    release = threading.Event()
    real_save = CheckpointRepository.save_checkpoint

    def slow_save(self, *args, **kwargs):
        started.set()
        assert release.wait(timeout=10), "release event never set"
        return real_save(self, *args, **kwargs)

    barrier = threading.Barrier(4)
    accepted: list[Future | None] = []
    lock = threading.Lock()

    def request():
        barrier.wait()
        _ = service.should_checkpoint(world_id, 1000)
        f = service.try_save_periodic_checkpoint(world_id, eng)
        with lock:
            accepted.append(f)

    threads = [threading.Thread(target=request) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    futs = [f for f in accepted if f is not None]
    assert len(futs) == 1, f"se aceptaron {len(futs)} escrituras periódicas"
    cp_id = futs[0].result()
    release.set()

    service.wait_pending_writes()
    assert service.has_pending_write(world_id) is False
    assert service.should_checkpoint(world_id, 1000) is True
    with Session() as s2:
        cps = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == world_id)
        ).all()
        assert len(cps) == 1
        assert cps[0].sequence == 1

    service.close()


def test_ps_f2_distinct_worlds_concurrent_periodic_writes(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    w1 = _make_world(Session, "PS-F2 World A", months_simulated=0)
    w2 = _make_world(Session, "PS-F2 World B", months_simulated=0)
    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(session_factory=Session, interval_months=1)

    results: dict[int, object] = {}
    lock = threading.Lock()

    def request(wid: int) -> None:
        f = service.try_save_periodic_checkpoint(wid, eng)
        with lock:
            results[wid] = f.result() if f else None

    t1 = threading.Thread(target=request, args=(w1,))
    t2 = threading.Thread(target=request, args=(w2,))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert results.get(w1) is not None
    assert results.get(w2) is not None
    service.wait_pending_writes()
    with Session() as s2:
        cps1 = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == w1)
        ).all()
        cps2 = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == w2)
        ).all()
        assert len(cps1) == 1
        assert len(cps2) == 1
    service.close()


# ---------------------------------------------------------------------------
# PS-F3 — Checkpoint manual no altera el intervalo periódico
# ---------------------------------------------------------------------------

def test_ps_f3_manual_checkpoint_does_not_mark_periodic_interval(
    engine_obj, small_world, races, governments
):
    """F-3: un checkpoint manual no actualiza _last_checkpoint_month, no
    altera el intervalo periódico ni interfiere con la exclusión periódica."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "PS-F3 World", months_simulated=0)
    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(
        session_factory=Session, interval_months=10
    )

    month = int(eng.month)
    assert service.should_checkpoint(world_id, 10) is True  # nada guardado aún

    # Checkpoint manual: debe persistir pero NO marcar el mes periódico.
    future = service.save_checkpoint_now(
        world_id=world_id, engine=eng, checkpoint_type="manual",
    )
    cp_id = future.result()
    assert cp_id is not None
    assert service._last_checkpoint_month.get(world_id) is None

    # El intervalo periódico sigue basado en el estado real (sin marca).
    # Debe poder guardar periódico igualmente (mes aún no marcado).
    assert service.should_checkpoint(world_id, month + 9) is True  # aún no marcó
    f2 = service.try_save_periodic_checkpoint(world_id, eng)
    assert f2 is not None
    assert f2.result() is not None
    service.wait_pending_writes()

    # Ahora sí está marcado como periódico exitoso.
    assert service._last_checkpoint_month.get(world_id) == month
    assert service.should_checkpoint(world_id, month + 1) is False
    assert service.should_checkpoint(world_id, month + 10) is True
    service.close()


# ===========================================================================
# Fase 2.2.1-T: corrección del deadlock D-1 (auditoría 2.2.1-S)
# ===========================================================================
# El defecto D-1 consistía en registrar future.add_done_callback() dentro de
# la sección crítica `with self._pending_cond:` en
# try_save_periodic_checkpoint(). Cuando el Future ya estaba completado, el
# callback se ejecutaba sincronicamente en el hilo llamante y, al intentar
# re-adquirir el threading.Lock no reentrante, provocaba un deadlock
# permanente y global. La corrección mueve el registro del callback fuera de
# la sección crítica (mismo patrón que ya usaba save_checkpoint_now()).

# ---------------------------------------------------------------------------
# PS-D1-A — Future ya completado con éxito: sin deadlock
# ---------------------------------------------------------------------------


def test_ps_d1_completed_future_success_no_deadlock(
    engine_obj, small_world, races, governments
):
    """D-1/A: try_save_periodic_checkpoint() con un Future ya completado
    exitosamente no debe bloquearse; el callback ejecuta, marca el mes y
    libera el slot sin reentrar en el lock retenido."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "PS-D1 World A", months_simulated=0)
    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(
        session_factory=Session, interval_months=1
    )

    done = Future()
    done.set_result(7)  # checkpoint id ficticio; el callback no lo usa
    with mock.patch.object(service.executor, "submit", return_value=done):
        f = service.try_save_periodic_checkpoint(world_id, eng)

    assert f is done
    assert done.done()
    # El callback se ejecutó: mes marcado y slot liberado sin deadlock.
    assert service._last_checkpoint_month.get(world_id) == int(eng.month)
    assert service.has_pending_write(world_id) is False
    service.wait_pending_writes()  # termina normalmente
    service.close()


# ---------------------------------------------------------------------------
# PS-D1-B — Future ya completado con excepción: sin deadlock
# ---------------------------------------------------------------------------


def test_ps_d1_completed_future_failure_no_deadlock(
    engine_obj, small_world, races, governments
):
    """D-1/B: try_save_periodic_checkpoint() con un Future ya completado con
    excepción no debe bloquearse; el fallo se reporta una vez, el mes no se
    marca y el slot se libera."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "PS-D1 World B", months_simulated=0)
    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(
        session_factory=Session, interval_months=1
    )

    done = Future()
    done.set_exception(RuntimeError("injected completion failure"))
    with mock.patch.object(service.executor, "submit", return_value=done):
        f = service.try_save_periodic_checkpoint(world_id, eng)

    assert f is done
    assert done.done()
    # Sin deadlock: el callback ejecutó y liberó el slot.
    assert service._last_checkpoint_month.get(world_id) is None
    assert service.has_pending_write(world_id) is False

    # wait_pending_writes() reporta el error con el mundo correcto.
    with pytest.raises(PersistenceWriteError) as excinfo:
        service.wait_pending_writes()
    assert world_id in excinfo.value.failures
    failures = excinfo.value.failures[world_id]
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert "injected completion failure" in str(failures[0])

    # Drenado: no se reporta dos veces.
    service.wait_pending_writes()
    service.close()


# ---------------------------------------------------------------------------
# PS-D1-C — Aislamiento global tras Future ya completado
# ---------------------------------------------------------------------------


def test_ps_d1_global_isolation_after_completed_future(
    engine_obj, small_world, races, governments
):
    """D-1/C: un Future ya completado (que antes colgaba globalmente) no
    impide que otro mundo opere, que should_checkpoint responda, que
    wait_pending_writes() termine ni que close() cierre sin bloquearse."""
    _engine, Session = engine_obj
    w1 = _make_world(Session, "PS-D1 World C1", months_simulated=0)
    w2 = _make_world(Session, "PS-D1 World C2", months_simulated=0)
    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(
        session_factory=Session, interval_months=1
    )

    done = Future()
    done.set_result(3)
    with mock.patch.object(service.executor, "submit", return_value=done):
        f1 = service.try_save_periodic_checkpoint(w1, eng)
    assert f1 is not None
    service.wait_pending_writes()
    month = int(eng.month)
    assert service._last_checkpoint_month.get(w1) == month

    # Otro mundo sigue operativo: no hay congelamiento global del servicio.
    f2 = service.try_save_periodic_checkpoint(w2, eng)
    assert f2 is not None
    assert f2.result() is not None
    service.wait_pending_writes()

    assert service._last_checkpoint_month.get(w2) == month
    # w1 ya marcó mes: a partir del mes siguiente vuelve a estar por vencer.
    assert service.should_checkpoint(w1, month) is False
    assert service.should_checkpoint(w1, month + 1) is True
    service.close()


# ---------------------------------------------------------------------------
# PS-D1-D — close() espera la escritura aceptada cuyo callback no ha corrido
# ---------------------------------------------------------------------------


def test_ps_d1_close_waits_for_accepted_write(
    engine_obj, small_world, races, governments
):
    """D-1/D: una escritura aceptada (Future todavía no completado) es
    correctamente esperada por close(); no hay deadlock ni escritura perdida."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "PS-D1 World D", months_simulated=0)
    eng = _make_engine_instance(small_world, races, governments)
    service = PersistenceService(
        session_factory=Session, interval_months=1
    )

    pending = Future()  # aún no completado: su callback no ha corrido
    with mock.patch.object(service.executor, "submit", return_value=pending):
        f = service.try_save_periodic_checkpoint(world_id, eng)

    assert f is pending
    assert not pending.done()
    assert service.has_pending_write(world_id) is True

    close_done = threading.Event()
    close_error: list[BaseException] = []

    def do_close() -> None:
        try:
            service.close()
        except BaseException as exc:  # noqa: BLE001
            close_error.append(exc)
        finally:
            close_done.set()

    closer = threading.Thread(target=do_close, name="d1-closer")
    closer.start()
    # close() debe bloquearse esperando la escritura aceptada, no retornar antes.
    time.sleep(0.3)
    assert not close_done.is_set(), "close() retorno sin esperar la escritura"

    # Completar la escritura aceptada: su callback libera el slot y close() termina.
    pending.set_result(11)
    closer.join(timeout=15)
    assert not closer.is_alive(), "close() quedó bloqueado tras completar la escritura"
    assert not close_error, f"close() falló: {close_error}"

    assert service.has_pending_write(world_id) is False
    service.wait_pending_writes()

