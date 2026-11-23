"""Tests for RecoveryManager (Fase 2.2.2).

Cubre recuperación de mundos desde checkpoints persistidos, validación de
integridad criptográfica, fallback ante corrupción y comportamiento por
estado de mundo.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from worldsim.data.loaders import load_governments, load_races
from worldsim.export.checkpoint import (
    CHECKPOINT_VERSION,
    checkpoint_to_dict,
    engine_from_checkpoint,
)
from worldsim.server.db import Base, WorldCheckpoint, WorldRow
from worldsim.server.persistence_service import PersistenceWriteError
from worldsim.server.recovery_manager import RecoveryManager
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
    return tmp_path / "test_recovery_manager.db"


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


def _make_world(Session, name, months_simulated=10, status="running"):
    """Create a world row and return its id."""
    s = Session()
    row = WorldRow(
        seed=42, width=100, height=50, months_simulated=months_simulated,
        land_pct=50.0, state_json="{}", created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name=name, mode="single", sea_level=0.2, status=status,
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    world_id = row.id
    s.close()
    return world_id


def _save_checkpoint(Session, world_id, engine, sequence, month,
                     checkpoint_type="periodic"):
    """Persist a checkpoint and return its row id."""
    s = Session()
    try:
        state = checkpoint_to_dict(engine, checkpoint_version=CHECKPOINT_VERSION)
        payload = json.dumps(state, ensure_ascii=False).encode("utf-8")
        checksum = hashlib.sha256(payload).hexdigest()
        repo = CheckpointRepository(s)
        cp_id = repo.save_checkpoint(
            world_id=world_id,
            sequence=sequence,
            month=month,
            checkpoint_type=checkpoint_type,
            payload=payload,
            checksum=checksum,
            checkpoint_version=CHECKPOINT_VERSION,
        )
        s.commit()
        return cp_id
    finally:
        s.close()


def _raw_checkpoint_row(Session, world_id, sequence, month, payload, checksum,
                        checkpoint_type="periodic", is_valid=1,
                        checkpoint_version=CHECKPOINT_VERSION):
    """Insert a raw checkpoint row (bypassing sequence checks) and return id."""
    s = Session()
    try:
        cp = WorldCheckpoint(
            world_id=world_id,
            sequence=sequence,
            month=month,
            checkpoint_type=checkpoint_type,
            payload=payload,
            checksum=checksum,
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


# ---------------------------------------------------------------------------
# RM-01: recuperar un mundo con un único checkpoint válido
# ---------------------------------------------------------------------------


def test_rm_01_single_valid_checkpoint(engine_obj, small_world, races, governments):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-01 World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)
    month = int(eng.month)

    cp_id = _save_checkpoint(Session, world_id, eng, sequence=1, month=month)

    result = RecoveryManager(session_factory=Session).recover(world_id)

    assert result.status == "ok"
    assert result.world_id == world_id
    assert result.engine is not None
    assert result.world_status == "running"
    assert result.checkpoint_sequence == 1
    assert result.checkpoint_month == month
    assert result.checkpoint_type == "periodic"
    assert result.checkpoint_id == cp_id
    assert result.engine.month == month
    # No se inició ningún tick: el mes se conserva del checkpoint.
    assert result.engine.month == month


# ---------------------------------------------------------------------------
# RM-02: recuperar el checkpoint más reciente cuando existen varios válidos
# ---------------------------------------------------------------------------


def test_rm_02_most_recent_valid_checkpoint(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-02 World", months_simulated=20)
    eng = _make_engine_instance(small_world, races, governments)

    # Dos checkpoints válidos, secuencias 1 y 2.
    cp1_id = _save_checkpoint(Session, world_id, eng, sequence=1, month=10)
    eng.tick()
    cp2_id = _save_checkpoint(Session, world_id, eng, sequence=2, month=11)

    result = RecoveryManager(session_factory=Session).recover(world_id)

    assert result.status == "ok"
    assert result.checkpoint_sequence == 2
    assert result.checkpoint_month == 11
    assert result.checkpoint_id == cp2_id
    assert result.engine.month == 11
    # El más reciente se prefiere sobre el anterior.
    assert result.checkpoint_id != cp1_id


# ---------------------------------------------------------------------------
# RM-03: conservar paused=True para un mundo pausado
# ---------------------------------------------------------------------------


def test_rm_03_paused_world_keeps_paused_status(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-03 World", months_simulated=10, status="paused")
    eng = _make_engine_instance(small_world, races, governments)
    month = int(eng.month)

    _save_checkpoint(Session, world_id, eng, sequence=1, month=month)

    result = RecoveryManager(session_factory=Session).recover(world_id)

    assert result.status == "ok"
    assert result.engine is not None
    assert result.world_status == "paused"
    assert result.engine.month == month
    # El motor reconstruido conserva la pausa.
    assert result.engine.paused is True


# ---------------------------------------------------------------------------
# RM-04: reconstruir un mundo running sin iniciar un runner
# ---------------------------------------------------------------------------


def test_rm_04_running_world_no_runner_started(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-04 World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)
    month = int(eng.month)

    _save_checkpoint(Session, world_id, eng, sequence=1, month=month)

    result = RecoveryManager(session_factory=Session).recover(world_id)

    assert result.status == "ok"
    assert result.engine is not None
    assert result.world_status == "running"
    # RecoveryManager no inicia ticks: el mes no avanza.
    assert result.engine.month == month
    # El motor de un mundo running no está pausado.
    assert result.engine.paused is False


# ---------------------------------------------------------------------------
# RM-05: rechazar un mundo stopped
# ---------------------------------------------------------------------------


def test_rm_05_stopped_world_not_recoverable(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-05 World", months_simulated=10, status="stopped")

    result = RecoveryManager(session_factory=Session).recover(world_id)

    assert result.status == "not_recoverable"
    assert result.world_id == world_id
    assert result.world_status == "stopped"
    assert result.engine is None
    assert "not recoverable" in (result.diagnostics or "").lower()


# ---------------------------------------------------------------------------
# RM-06: rechazar un mundo legacy
# ---------------------------------------------------------------------------


def test_rm_06_legacy_world_not_recoverable(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-06 World", months_simulated=10, status="legacy")

    result = RecoveryManager(session_factory=Session).recover(world_id)

    assert result.status == "not_recoverable"
    assert result.world_id == world_id
    assert result.world_status == "legacy"
    assert result.engine is None


# ---------------------------------------------------------------------------
# RM-07: rechazar un mundo error
# ---------------------------------------------------------------------------


def test_rm_07_error_world_not_recoverable(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-07 World", months_simulated=10, status="error")

    result = RecoveryManager(session_factory=Session).recover(world_id)

    assert result.status == "not_recoverable"
    assert result.world_id == world_id
    assert result.world_status == "error"
    assert result.engine is None


# ---------------------------------------------------------------------------
# RM-08: informar correctamente cuando no existen checkpoints
# ---------------------------------------------------------------------------


def test_rm_08_no_checkpoints(engine_obj, small_world, races, governments):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-08 World", months_simulated=10)

    result = RecoveryManager(session_factory=Session).recover(world_id)

    assert result.status == "no_checkpoints"
    assert result.world_id == world_id
    assert result.world_status == "running"
    assert result.engine is None
    assert "no checkpoints" in (result.diagnostics or "").lower()


# ---------------------------------------------------------------------------
# RM-09: checksum incorrecto invalida el checkpoint y activa fallback
# ---------------------------------------------------------------------------


def test_rm_09_bad_checksum_invalidates_and_falls_back(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-09 World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)

    # Checkpoint válido (secuencia 1) + checkpoint con checksum incorrecto (secuencia 2).
    _save_checkpoint(Session, world_id, eng, sequence=1, month=10)

    bad_payload = json.dumps({"not": "a checkpoint"}).encode("utf-8")
    bad_checksum = hashlib.sha256(b"wrong checksum").hexdigest()
    _raw_checkpoint_row(Session, world_id, sequence=2, month=11,
                        payload=bad_payload, checksum=bad_checksum)

    result = RecoveryManager(session_factory=Session).recover(world_id)

    assert result.status == "ok"
    assert result.checkpoint_sequence == 1
    assert result.checkpoint_month == 10

    # El checkpoint corrupto quedó marcado como inválido.
    with Session() as s2:
        corrupt = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.sequence == 2)
        ).first()
        assert corrupt is not None
        assert corrupt.is_valid == 0


# ---------------------------------------------------------------------------
# RM-10: JSON inválido con checksum correcto invalida y activa fallback
# ---------------------------------------------------------------------------


def test_rm_10_invalid_json_invalidates_and_falls_back(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-10 World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)

    _save_checkpoint(Session, world_id, eng, sequence=1, month=10)

    # Checkpoint con payload que no es JSON válido pero con checksum correcto.
    not_json = b"{not valid json"
    not_json_checksum = hashlib.sha256(not_json).hexdigest()
    _raw_checkpoint_row(Session, world_id, sequence=2, month=11,
                        payload=not_json, checksum=not_json_checksum)

    result = RecoveryManager(session_factory=Session).recover(world_id)

    assert result.status == "ok"
    assert result.checkpoint_sequence == 1

    with Session() as s2:
        corrupt = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.sequence == 2)
        ).first()
        assert corrupt is not None
        assert corrupt.is_valid == 0


# ---------------------------------------------------------------------------
# RM-11: versión no soportada invalida y activa fallback
# ---------------------------------------------------------------------------


def test_rm_11_unsupported_version_invalidates_and_falls_back(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-11 World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)

    _save_checkpoint(Session, world_id, eng, sequence=1, month=10)

    # Checkpoint con versión 99 y checksum correcto sobre sus bytes.
    state = {"_checkpoint_version": 99, "_checkpoint_type": "simengine"}
    bad_ver_payload = json.dumps(state).encode("utf-8")
    bad_ver_checksum = hashlib.sha256(bad_ver_payload).hexdigest()
    _raw_checkpoint_row(Session, world_id, sequence=2, month=11,
                        payload=bad_ver_payload, checksum=bad_ver_checksum,
                        checkpoint_version=99)

    result = RecoveryManager(session_factory=Session).recover(world_id)

    assert result.status == "ok"
    assert result.checkpoint_sequence == 1

    with Session() as s2:
        corrupt = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.sequence == 2)
        ).first()
        assert corrupt is not None
        assert corrupt.is_valid == 0


# ---------------------------------------------------------------------------
# RM-12: estructura semánticamente inválida invalida y activa fallback
# ---------------------------------------------------------------------------


def test_rm_12_semantically_invalid_invalidates_and_falls_back(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-12 World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)

    _save_checkpoint(Session, world_id, eng, sequence=1, month=10)

    # Checkpoint con versión y tipo correctos, pero faltan claves obligatorias
    # (world_config, world_maps, etc.) para reconstruir el motor.
    incomplete_state = {
        "_checkpoint_version": CHECKPOINT_VERSION,
        "_checkpoint_type": "simengine",
        "month": 11,
    }
    incomplete_payload = json.dumps(incomplete_state).encode("utf-8")
    incomplete_checksum = hashlib.sha256(incomplete_payload).hexdigest()
    _raw_checkpoint_row(Session, world_id, sequence=2, month=11,
                        payload=incomplete_payload, checksum=incomplete_checksum)

    result = RecoveryManager(session_factory=Session).recover(world_id)

    assert result.status == "ok"
    assert result.checkpoint_sequence == 1

    with Session() as s2:
        corrupt = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.sequence == 2)
        ).first()
        assert corrupt is not None
        assert corrupt.is_valid == 0


# ---------------------------------------------------------------------------
# RM-13: varios checkpoints inválidos seguidos hasta encontrar uno válido
# ---------------------------------------------------------------------------


def test_rm_13_multiple_invalid_then_valid(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-13 World", months_simulated=20)
    eng = _make_engine_instance(small_world, races, governments)

    # Secuencia 1: válido (mes 10). Será el fallback.
    _save_checkpoint(Session, world_id, eng, sequence=1, month=10)

    # Secuencias 2 y 3: ambas inválidas (checksum incorrecto).
    for seq, month in ((2, 11), (3, 12)):
        bad_payload = json.dumps({"bad": seq}).encode("utf-8")
        bad_checksum = hashlib.sha256(b"wrong").hexdigest()
        _raw_checkpoint_row(Session, world_id, sequence=seq, month=month,
                            payload=bad_payload, checksum=bad_checksum)

    result = RecoveryManager(session_factory=Session).recover(world_id)

    assert result.status == "ok"
    assert result.checkpoint_sequence == 1
    assert result.checkpoint_month == 10

    # Los dos corruptos quedaron invalidados.
    with Session() as s2:
        c2 = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.sequence == 2)
        ).first()
        c3 = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.sequence == 3)
        ).first()
        assert c2.is_valid == 0
        assert c3.is_valid == 0


# ---------------------------------------------------------------------------
# RM-14: todos los checkpoints inválidos producen recuperación fallida explícita
# ---------------------------------------------------------------------------


def test_rm_14_all_invalid_explicit_failure(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-14 World", months_simulated=10)

    # Todos los checkpoints tienen checksum incorrecto.
    for seq in (1, 2, 3):
        bad_payload = json.dumps({"bad": seq}).encode("utf-8")
        bad_checksum = hashlib.sha256(b"wrong").hexdigest()
        _raw_checkpoint_row(Session, world_id, sequence=seq, month=seq * 10,
                            payload=bad_payload, checksum=bad_checksum)

    result = RecoveryManager(session_factory=Session).recover(world_id)

    assert result.status == "not_recoverable"
    assert result.world_id == world_id
    assert result.engine is None
    assert result.diagnostics is not None
    assert "no recoverable" in result.diagnostics.lower()

    # Todos quedaron invalidados.
    with Session() as s2:
        invalid_count = s2.scalars(
            select(WorldCheckpoint).where(
                WorldCheckpoint.world_id == world_id,
                WorldCheckpoint.is_valid == 0,
            )
        ).all()
        assert len(invalid_count) == 3


# ---------------------------------------------------------------------------
# RM-15: no modificar checkpoints pertenecientes a otro mundo
# ---------------------------------------------------------------------------


def test_rm_15_no_touch_other_world_checkpoints(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    w1 = _make_world(Session, "RM-15 World 1", months_simulated=10)
    w2 = _make_world(Session, "RM-15 World 2", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)

    # w1 tiene un checkpoint válido; w2 tiene uno corrupto.
    _save_checkpoint(Session, w1, eng, sequence=1, month=10)
    bad_payload = json.dumps({"bad": 1}).encode("utf-8")
    bad_checksum = hashlib.sha256(b"wrong").hexdigest()
    _raw_checkpoint_row(Session, w2, sequence=1, month=11,
                        payload=bad_payload, checksum=bad_checksum)

    # Recuperar w1: no debe invalidar nada de w2.
    result = RecoveryManager(session_factory=Session).recover(w1)
    assert result.status == "ok"
    assert result.world_id == w1

    with Session() as s2:
        c_w2 = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == w2)
        ).all()
        assert len(c_w2) == 1
        assert c_w2[0].is_valid == 1  # sin tocar


# ---------------------------------------------------------------------------
# RM-16: un error de base de datos no se confunde con corrupción del checkpoint
# ---------------------------------------------------------------------------


def test_rm_16_db_error_not_checkpoint_corruption(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-16 World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)

    # Checkpoint válido, pero fallamos la consulta de checkpoints válidos
    # mediante un error simulado en el repositorio.
    _save_checkpoint(Session, world_id, eng, sequence=1, month=10)

    def failing_get_valid_checkpoints_for_world(self, world_id):
        raise sqlite3.OperationalError("database connection lost", {}, None)

    with mock.patch.object(
        CheckpointRepository, "get_valid_checkpoints_for_world",
        failing_get_valid_checkpoints_for_world,
    ):
        with pytest.raises(sqlite3.OperationalError):
            RecoveryManager(session_factory=Session).recover(world_id)

    # El checkpoint válido no fue invalidado (no se tocó por corrupción).
    with Session() as s2:
        cps = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == world_id)
        ).all()
        assert len(cps) == 1
        assert cps[0].is_valid == 1


# ---------------------------------------------------------------------------
# RM-17: una falla al marcar un checkpoint inválido no se oculta
# ---------------------------------------------------------------------------


def test_rm_17_invalidation_failure_not_hidden(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-17 World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)

    # Checkpoint válido (secuencia 1) + checkpoint corrupto (secuencia 2).
    _save_checkpoint(Session, world_id, eng, sequence=1, month=10)
    bad_payload = json.dumps({"bad": 1}).encode("utf-8")
    bad_checksum = hashlib.sha256(b"wrong").hexdigest()
    _raw_checkpoint_row(Session, world_id, sequence=2, month=11,
                        payload=bad_payload, checksum=bad_checksum)

    # Fallar el commit al invalidar (simulado en el repo).
    def failing_commit_or_flush(self, s):
        if self._owns_session:
            raise OperationalError("COMMIT", {}, sqlite3.OperationalError("disk full"))

    with mock.patch.object(
        CheckpointRepository, "_commit_or_flush", failing_commit_or_flush
    ):
        # La invalidación falla; el error de infraestructura se propaga.
        with pytest.raises(OperationalError):
            RecoveryManager(session_factory=Session).recover(world_id)

    # El checkpoint corrupto no fue marcado inválido (el commit falló).
    with Session() as s2:
        c2 = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.sequence == 2)
        ).first()
        assert c2 is not None
        assert c2.is_valid == 1


# ---------------------------------------------------------------------------
# RM-18: sesiones y transacciones quedan correctamente cerradas ante excepciones
# ---------------------------------------------------------------------------


def test_rm_18_sessions_closed_on_exception(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-18 World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)

    _save_checkpoint(Session, world_id, eng, sequence=1, month=10)

    # Forzar una excepción durante la recuperación (p. ej. en la evaluación
    # del checkpoint) y verificar que las sesiones se cierran.
    def failing_evaluate(cp):
        raise RuntimeError("simulated evaluation failure")

    recovery = RecoveryManager(session_factory=Session)
    with mock.patch.object(recovery, "_evaluate_checkpoint", failing_evaluate):
        with pytest.raises(RuntimeError, match="simulated evaluation failure"):
            recovery.recover(world_id)

    # Sesiones cerradas: se puede abrir una nueva sesión sin problemas.
    with Session() as s2:
        row = s2.get(WorldRow, world_id)
        assert row is not None
        cps = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == world_id)
        ).all()
        assert len(cps) == 1


# ---------------------------------------------------------------------------
# RM-19: state_json no se utiliza como fallback
# ---------------------------------------------------------------------------


def test_rm_19_state_json_not_used_as_fallback(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-19 World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)

    # Un mundo con state_json lleno pero SIN checkpoints.
    s = Session()
    row = s.get(WorldRow, world_id)
    row.state_json = json.dumps({"some": "lightweight view"})
    s.commit()
    s.close()

    result = RecoveryManager(session_factory=Session).recover(world_id)

    assert result.status == "no_checkpoints"
    assert result.engine is None
    assert "no checkpoints" in (result.diagnostics or "").lower()


# ---------------------------------------------------------------------------
# RM-20: la recuperación no inicia ticks ni crea runners
# ---------------------------------------------------------------------------


def test_rm_20_no_ticks_no_runners(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-20 World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)
    month = int(eng.month)

    _save_checkpoint(Session, world_id, eng, sequence=1, month=month)

    result = RecoveryManager(session_factory=Session).recover(world_id)

    assert result.status == "ok"
    assert result.engine is not None
    # El mes se conserva exactamente; no se avanzó ni un tick.
    assert result.engine.month == month
    # No hay referencia a runners ni hilos iniciados por RecoveryManager.
    assert result.diagnostics is not None
    assert "recovered" in result.diagnostics.lower()


# ---------------------------------------------------------------------------
# RM-R4: checkpoints previamente inválidos no se reevalúan
# ---------------------------------------------------------------------------


def test_rm_r4_invalidated_not_reevaluated(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-R4 World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)

    # Checkpoint válido (secuencia 1) + checkpoint corrupto (secuencia 2).
    _save_checkpoint(Session, world_id, eng, sequence=1, month=10)
    bad_payload = json.dumps({"bad": 1}).encode("utf-8")
    bad_checksum = hashlib.sha256(b"wrong").hexdigest()
    _raw_checkpoint_row(Session, world_id, sequence=2, month=11,
                        payload=bad_payload, checksum=bad_checksum)

    recovery = RecoveryManager(session_factory=Session)

    # Primera recuperación: evalúa 2 checkpoints (corrupto, luego válido)
    # e invalida el corrupto.
    evaluate_calls_1 = []

    original_evaluate = recovery._evaluate_checkpoint

    def wrapped_evaluate(cp):
        evaluate_calls_1.append(cp.id)
        return original_evaluate(cp)

    with mock.patch.object(recovery, "_evaluate_checkpoint", wrapped_evaluate):
        result_1 = recovery.recover(world_id)

    assert result_1.status == "ok"
    assert result_1.checkpoint_sequence == 1
    # Se evaluó el corrupto (seq 2) y el válido (seq 1).
    assert sorted(evaluate_calls_1) == [1, 2]

    # El corrupto quedó invalidado.
    with Session() as s2:
        c2 = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.sequence == 2)
        ).first()
        assert c2.is_valid == 0

    # Segunda recuperación: solo se evalúa el válido (seq 1), el corrupto
    # (seq 2) fue excluido por get_valid_checkpoints_for_world y no se
    # vuelve a procesar.
    evaluate_calls_2 = []

    def wrapped_evaluate_2(cp):
        evaluate_calls_2.append(cp.id)
        return original_evaluate(cp)

    with mock.patch.object(recovery, "_evaluate_checkpoint", wrapped_evaluate_2):
        result_2 = recovery.recover(world_id)

    assert result_2.status == "ok"
    assert result_2.checkpoint_sequence == 1
    assert evaluate_calls_2 == [1], evaluate_calls_2


# ---------------------------------------------------------------------------
# RM-R7: motor nuevo comienza con paused=False
# ---------------------------------------------------------------------------


def test_rm_r7_new_engine_paused_false(
    engine_obj, small_world, races, governments
):
    _engine, Session = engine_obj
    eng = SimEngine(small_world, races, governments, seed=7)
    assert eng.paused is False


# ---------------------------------------------------------------------------
# RM-R1 / RM-R2 / RM-R3 / RM-R8 / RM-R9: contrato de estados y operación pública
# ---------------------------------------------------------------------------


def test_rm_r1_result_states_are_only_ok_no_checkpoints_not_recoverable(
    engine_obj, small_world, races, governments
):
    """RM-R1: RecoveryResult solo usa estados realmente devueltos; nunca
    devuelve 'error' (los errores de infraestructura se propagan como
    excepción)."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-R1 World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)
    _save_checkpoint(Session, world_id, eng, sequence=1, month=10)

    result = RecoveryManager(session_factory=Session).recover(world_id)
    assert result.status in {"ok", "no_checkpoints", "not_recoverable"}
    assert result.status != "error"


def test_rm_r2_infrastructure_errors_propagate(
    engine_obj, small_world, races, governments
):
    """RM-R2: un error de base de datos al invalidar se propaga, no se
    convierte en not_recoverable ni se oculta."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-R2 World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)

    _save_checkpoint(Session, world_id, eng, sequence=1, month=10)
    bad_payload = json.dumps({"bad": 1}).encode("utf-8")
    bad_checksum = hashlib.sha256(b"wrong").hexdigest()
    _raw_checkpoint_row(Session, world_id, sequence=2, month=11,
                        payload=bad_payload, checksum=bad_checksum)

    def failing_commit_or_flush(self, s):
        if self._owns_session:
            raise OperationalError("COMMIT", {}, sqlite3.OperationalError("disk full"))

    recovery = RecoveryManager(session_factory=Session)
    with mock.patch.object(
        CheckpointRepository, "_commit_or_flush", failing_commit_or_flush
    ):
        with pytest.raises(OperationalError):
            recovery.recover(world_id)


def test_rm_r8_fallback_uses_valid_checkpoints_query(
    engine_obj, small_world, races, governments
):
    """RM-R8: el fallback continúa funcionando usando get_valid_checkpoints_for_world."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-R8 World", months_simulated=20)
    eng = _make_engine_instance(small_world, races, governments)

    _save_checkpoint(Session, world_id, eng, sequence=1, month=10)

    # Dos checkpoints corruptos seguidos del válido (secuencia 1).
    for seq, month in ((2, 11), (3, 12)):
        bad_payload = json.dumps({"bad": seq}).encode("utf-8")
        bad_checksum = hashlib.sha256(b"wrong").hexdigest()
        _raw_checkpoint_row(Session, world_id, sequence=seq, month=month,
                            payload=bad_payload, checksum=bad_checksum)

    result = RecoveryManager(session_factory=Session).recover(world_id)
    assert result.status == "ok"
    assert result.checkpoint_sequence == 1
    assert result.checkpoint_month == 10

    with Session() as s2:
        assert s2.scalars(
            select(WorldCheckpoint).where(
                WorldCheckpoint.sequence == 2,
                WorldCheckpoint.is_valid == 0,
            )
        ).first() is not None
        assert s2.scalars(
            select(WorldCheckpoint).where(
                WorldCheckpoint.sequence == 3,
                WorldCheckpoint.is_valid == 0,
            )
        ).first() is not None


def test_rm_r9_checkpoint_version_unchanged(
    engine_obj, small_world, races, governments
):
    """RM-R9: no se modifica el formato ni versión de checkpoint."""
    from worldsim.export.checkpoint import CHECKPOINT_VERSION as cv
    assert cv == 2

    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-R9 World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)
    _save_checkpoint(Session, world_id, eng, sequence=1, month=10)

    result = RecoveryManager(session_factory=Session).recover(world_id)
    assert result.status == "ok"
    # El checkpoint usado tiene versión 2 (sin incrementar).
    with Session() as s2:
        cp = s2.get(WorldCheckpoint, result.checkpoint_id)
        assert cp.checkpoint_version == 2


# ---------------------------------------------------------------------------
# RM-R1A: mundo sin checkpoints históricos devuelve no_checkpoints
# ---------------------------------------------------------------------------


def test_rm_r1a_no_historical_checkpoints(
    engine_obj, small_world, races, governments
):
    """RM-R1A: un mundo sin filas de checkpoints devuelve no_checkpoints."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-R1A World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)
    # No se guarda ningún checkpoint.

    result = RecoveryManager(session_factory=Session).recover(world_id)

    assert result.status == "no_checkpoints"
    assert result.engine is None
    assert "no checkpoints" in (result.diagnostics or "").lower()


# ---------------------------------------------------------------------------
# RM-R1B: todos los checkpoints invalidados devuelve not_recoverable
# ---------------------------------------------------------------------------


def test_rm_r1b_all_checkpoints_invalidated(
    engine_obj, small_world, races, governments
):
    """RM-R1B: registros históricos todos is_valid=0 -> not_recoverable."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-R1B World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)

    # Dos checkpoints históricos, ambos ya invalidados (is_valid=0).
    bad_payload = json.dumps({"bad": 1}).encode("utf-8")
    bad_checksum = hashlib.sha256(b"wrong").hexdigest()
    _raw_checkpoint_row(Session, world_id, sequence=1, month=10,
                        payload=bad_payload, checksum=bad_checksum,
                        is_valid=0)
    _raw_checkpoint_row(Session, world_id, sequence=2, month=11,
                        payload=bad_payload, checksum=bad_checksum,
                        is_valid=0)

    result = RecoveryManager(session_factory=Session).recover(world_id)

    assert result.status == "not_recoverable"
    assert result.engine is None
    assert "historical" in (result.diagnostics or "").lower()
    assert "invalid" in (result.diagnostics or "").lower()

    # Ningún checkpoint válido quedó (no se reactivó ninguno).
    with Session() as s2:
        valid = s2.scalars(
            select(WorldCheckpoint).where(
                WorldCheckpoint.world_id == world_id,
                WorldCheckpoint.is_valid == 1,
            )
        ).all()
        assert len(valid) == 0


# ---------------------------------------------------------------------------
# RM-R1C: segunda recuperación tras corrupción total
# ---------------------------------------------------------------------------


def test_rm_r1c_second_recovery_after_total_corruption(
    engine_obj, small_world, races, governments
):
    """RM-R1C: tras invalidecer todos, una segunda recuperación no los
    reevalúa y devuelve not_recoverable con diagnóstico de históricos."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-R1C World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)

    # Dos checkpoints corruptos (checksum incorrecto).
    for seq in (1, 2):
        bad_payload = json.dumps({"bad": seq}).encode("utf-8")
        bad_checksum = hashlib.sha256(b"wrong").hexdigest()
        _raw_checkpoint_row(Session, world_id, sequence=seq, month=seq * 10,
                            payload=bad_payload, checksum=bad_checksum)

    recovery = RecoveryManager(session_factory=Session)

    # Primera recuperación: evalúa ambos, los invalida, devuelve not_recoverable.
    evaluate_calls_1 = []
    original_evaluate = recovery._evaluate_checkpoint

    def wrapped_evaluate(cp):
        evaluate_calls_1.append(cp.id)
        return original_evaluate(cp)

    with mock.patch.object(recovery, "_evaluate_checkpoint", wrapped_evaluate):
        result_1 = recovery.recover(world_id)

    assert result_1.status == "not_recoverable"
    assert sorted(evaluate_calls_1) == [1, 2]

    # Segunda recuperación: no reevalúa checkpoints invalidados.
    evaluate_calls_2 = []

    def wrapped_evaluate_2(cp):
        evaluate_calls_2.append(cp.id)
        return original_evaluate(cp)

    with mock.patch.object(recovery, "_evaluate_checkpoint", wrapped_evaluate_2):
        result_2 = recovery.recover(world_id)

    assert result_2.status == "not_recoverable"
    assert evaluate_calls_2 == [], evaluate_calls_2
    assert "historical" in (result_2.diagnostics or "").lower()
    assert "invalid" in (result_2.diagnostics or "").lower()


# ---------------------------------------------------------------------------
# RM-R3: RecoveryManager usa la API pública de invalidación
# ---------------------------------------------------------------------------


def test_rm_r3_uses_public_invalidation_api(
    engine_obj, small_world, races, governments
):
    """RM-R3: al detectar un checkpoint corrupto, RecoveryManager invoca
    CheckpointRepository.mark_checkpoint_invalid() (API pública) con el
    identificador correcto y mantiene el fallback."""
    _engine, Session = engine_obj
    world_id = _make_world(Session, "RM-R3 World", months_simulated=10)
    eng = _make_engine_instance(small_world, races, governments)

    # Checkpoint válido (secuencia 1) + checkpoint corrupto (secuencia 2).
    _save_checkpoint(Session, world_id, eng, sequence=1, month=10)
    bad_payload = json.dumps({"bad": 1}).encode("utf-8")
    bad_checksum = hashlib.sha256(b"wrong").hexdigest()
    _raw_checkpoint_row(Session, world_id, sequence=2, month=11,
                        payload=bad_payload, checksum=bad_checksum)

    # Espía (no sustituye) el método público real para conservar su lógica.
    real_invalidate = CheckpointRepository.mark_checkpoint_invalid
    calls = []

    def spy_invalidate(self, checkpoint_id):
        calls.append(checkpoint_id)
        return real_invalidate(self, checkpoint_id)

    with mock.patch.object(
        CheckpointRepository, "mark_checkpoint_invalid", spy_invalidate
    ):
        result = RecoveryManager(session_factory=Session).recover(world_id)

    # Fallback funciona: se recupera del válido (secuencia 1).
    assert result.status == "ok"
    assert result.checkpoint_sequence == 1

    # La API pública fue invocada con el id del checkpoint corrupto (2).
    assert calls == [2], calls

    # La invalidación se persistió realmente.
    with Session() as s2:
        c2 = s2.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.sequence == 2)
        ).first()
        assert c2.is_valid == 0



