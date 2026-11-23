"""Pruebas funcionales de snapshots independientes e inmutables (Etapa 3).

Cubre los puntos obligatorios de la §7:

1. independencia frente a mutaciones posteriores del motor;
2. independencia frente a mutaciones posteriores de los datos de origen;
3. inmutabilidad profunda de las estructuras anidadas;
4. JSON canónico determinista en UTF-8;
5. rechazo explícito de valores no serializables/inválidos (con ruta);
6. compatibilidad byte a byte con el contrato de checkpoints existente;
7. captura consistente bajo la concurrencia disponible (hilo de event loop +
   hilos con lock del llamador);
8. ausencia de escrituras/operaciones SQLite durante la captura;
(extra) captura de solo lectura sobre el motor.

Las mediciones de rendimiento separadas (§7.9 / DEC19) están en
``tests/test_snapshot_perf.py``: aquí solo se ejecutan pruebas funcionales.
"""

from __future__ import annotations

import asyncio
import json
import threading
import types
from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from worldsim.export.checkpoint import (
    CHECKPOINT_VERSION,
    checkpoint_to_dict,
    checkpoint_to_json,
    engine_from_json,
)
from worldsim.export.snapshot import (
    Snapshot,
    SnapshotError,
    capture_snapshot,
    freeze_state,
    snapshot_from_state,
    snapshot_to_bytes,
    snapshot_to_json,
)
from worldsim.sim.engine import SimEngine
from worldsim.sim.models import EventType
from worldsim.worldgen import WorldConfig
from worldsim.worldgen.pipeline import generate_world


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _private_engine(races, governments, seed: int = 42) -> SimEngine:
    """Motor con mundo privado.

    Los tests que mutan el mapa no pueden usar el fixture de sesión
    ``small_world`` (compartido entre módulos): generan su propio mundo.
    """
    world = generate_world(WorldConfig(seed=42, width=100, height=50, rivers=4))
    return SimEngine(world, races, governments, seed=seed)


def _walk_immutable(node, path: str = "$") -> None:
    """Recorre el árbol congelado exigiendo inmutabilidad en cada nivel."""
    if isinstance(node, types.MappingProxyType):
        with pytest.raises(TypeError):
            node["__imposible__"] = None
        for key, value in node.items():
            _walk_immutable(value, f"{path}.{key}")
    elif isinstance(node, tuple):
        # Asignar a una tupla falla siempre (TypeError; IndexError si vacía).
        with pytest.raises((TypeError, IndexError)):
            node[0] = None
        for index, value in enumerate(node):
            _walk_immutable(value, f"{path}[{index}]")
    else:
        assert node is None or type(node) in (bool, int, float, str), (
            f"valor no inmutable en {path}: {type(node).__name__}"
        )


def _assert_coherent(snapshot: Snapshot) -> None:
    """Comprueba coherencia interna de un snapshot capturado.

    Índices, contadores, referencias cruzadas y mapas deben ser internamente
    consistentes: es la evidencia de que la copia no quedó a medias.
    """
    state = snapshot.state
    assert type(state) is types.MappingProxyType
    assert state["_checkpoint_version"] == CHECKPOINT_VERSION
    assert state["_checkpoint_type"] == "simengine"

    month = state["month"]
    cfg = state["world_config"]
    assert cfg["width"] > 0 and cfg["height"] > 0
    for name in (
        "height", "temperature", "precipitation", "drainage",
        "biome", "prosperity", "rivers",
    ):
        shape = state["world_maps"][name]["shape"]
        assert list(shape) == [cfg["height"], cfg["width"]]

    kingdoms = state["kingdoms"]
    kingdom_ids = {k["id"] for k in kingdoms}
    leader_ids = {x["id"] for x in state["leaders"]}
    hero_ids = {x["id"] for x in state["heroes"]}
    notable_ids = {x["id"] for x in state["notables"]}

    for k in kingdoms:
        if k["leader_id"] is not None:
            assert k["leader_id"] in leader_ids
        if k["hero_id"] is not None:
            assert k["hero_id"] in hero_ids
        assert set(k["notable_ids"]) <= notable_ids

    for w in state["wars"]:
        assert w["side_a_id"] in kingdom_ids
        assert w["side_b_id"] in kingdom_ids
        if w["victor_id"] is not None:
            assert w["victor_id"] in kingdom_ids
        assert set(w["cobelligerents_a"]) <= kingdom_ids
        assert set(w["cobelligerents_b"]) <= kingdom_ids

    for key in state["relations"]:
        a_id, b_id = key.split("|")
        assert int(a_id) in kingdom_ids and int(b_id) in kingdom_ids

    chronicle = state["chronicle"]
    events = chronicle["events"]
    event_ids = {e["id"] for e in events}
    assert chronicle["_next_event_id"] > max(event_ids, default=0)
    for e in events:
        assert e["month"] <= month
        assert set(e["kingdom_ids"]) <= kingdom_ids
    for kid, ids in chronicle["_by_kingdom"].items():
        assert int(kid) in kingdom_ids
        assert set(ids) <= event_ids

    assert state["_next_war_id"] > max(
        (w["id"] for w in state["wars"]), default=0
    )
    assert state["_next_leader_id"] > max(leader_ids, default=0)
    assert state["_next_hero_id"] > max(hero_ids, default=0)
    assert state["_next_notable_id"] > max(notable_ids, default=0)
    assert state["_next_beast_id"] > max(
        (b["id"] for b in state["beasts"]), default=0
    )


# ---------------------------------------------------------------------------
# §7.1 — Independencia frente a mutaciones posteriores del motor
# ---------------------------------------------------------------------------

def test_snapshot_independent_of_later_engine_mutations(engine):
    for _ in range(60):
        engine.tick()
    snap = capture_snapshot(engine)
    before = snap.to_bytes()

    # Mutaciones posteriores sobre el motor: ticks, estado y colecciones.
    for _ in range(60):
        engine.tick()
    engine.kingdoms[0].total_population += 10**9
    engine.kingdoms[0].mods["zz_mutado"] = 3.5
    engine._milestones.add("zz_mutado")

    after = snapshot_from_state(checkpoint_to_dict(engine)).to_bytes()
    assert after != before, "el motor debió avanzar entre ambas capturas"
    # El snapshot capturado no cambia ni un byte.
    assert snap.to_bytes() == before
    assert snap.to_json().encode("utf-8") == before
    _assert_coherent(snap)


# ---------------------------------------------------------------------------
# §7.2 — Independencia frente a mutaciones posteriores de los datos de origen
# ---------------------------------------------------------------------------

def test_snapshot_independent_of_later_source_data_mutations(races, governments):
    eng = _private_engine(races, governments)
    for _ in range(40):
        eng.tick()
    assert eng.kingdoms and eng.chronicle.events

    snap = capture_snapshot(eng)
    before = snap.to_bytes()

    # Mutaciones in-place sobre estructuras de origen compartidas/internas.
    eng.fallen.append({"name": "zz_colapsado", "month": eng.month})
    eng.chronicle.events[0].params["zz_mutado"] = "sí"
    eng.chronicle.log(
        eng.month, EventType.MILESTONE, "evento posterior", (), {}, "zz"
    )
    kingdom = eng.kingdoms[0]
    kingdom.population_history.append(10**9)
    kingdom.culture["zz_mutado"] = 1.0
    kingdom.mods["zz_mutado"] = 2.0
    kingdom.techs.add("zz_tech")
    eng._world_techs.add("zz_tech")
    eng._milestones.add("zz_marco")
    eng.world.height[0, 0] = -12345.0
    eng._battle_heat[0, 0] += 1.0
    eng.rng.random()

    changed = capture_snapshot(eng).to_bytes()
    assert changed != before, "las mutaciones debieron reflejarse en una nueva captura"
    # El snapshot previo sigue idéntico: no comparte referencias con el origen.
    assert snap.to_bytes() == before
    _assert_coherent(snap)


# ---------------------------------------------------------------------------
# §7.3 — Inmutabilidad profunda de estructuras anidadas
# ---------------------------------------------------------------------------

def test_snapshot_state_is_deeply_immutable(engine):
    for _ in range(30):
        engine.tick()
    snap = capture_snapshot(engine)

    # Raíz: MappingProxyType y recorrido recursivo por todo el árbol.
    assert type(snap.state) is types.MappingProxyType
    with pytest.raises(TypeError):
        snap.state["month"] = 0
    _walk_immutable(snap.state)

    # Niveles concretos: crónica, eventos, parámetros y mapas del mundo.
    chronicle = snap.state["chronicle"]
    assert type(chronicle) is types.MappingProxyType
    with pytest.raises(TypeError):
        chronicle["events"] = ()
    events = chronicle["events"]
    assert type(events) is tuple
    with pytest.raises((TypeError, IndexError)):
        events[0] = None
    first_event = events[0]
    assert type(first_event) is types.MappingProxyType
    with pytest.raises(TypeError):
        first_event["params"] = {}

    height_data = snap.state["world_maps"]["height"]["data"]
    assert type(height_data) is tuple
    with pytest.raises((TypeError, IndexError)):
        height_data[0] = None
    if height_data and isinstance(height_data[0], tuple):
        with pytest.raises(TypeError):
            height_data[0][0] = 0.0

    # El propio Snapshot es inmutable (dataclass frozen) y no expone mutables.
    with pytest.raises(FrozenInstanceError):
        snap.state = {}
    with pytest.raises(FrozenInstanceError):
        snap.month = 1
    assert not isinstance(snap.state, dict)


# ---------------------------------------------------------------------------
# §7.4 — JSON canónico determinista en UTF-8
# ---------------------------------------------------------------------------

def test_snapshot_json_is_deterministic_utf8(races, governments):
    e1 = _private_engine(races, governments, seed=7)
    e2 = _private_engine(races, governments, seed=7)
    for _ in range(50):
        e1.tick()
        e2.tick()

    snap1 = capture_snapshot(e1)
    snap2 = capture_snapshot(e2)

    # Dos motores con la misma semilla -> exactamente los mismos bytes.
    assert snap1.to_bytes() == snap2.to_bytes()
    assert snap1.to_json() == snap2.to_json()

    # Serialización repetible y UTF-8 sin BOM.
    text1 = snap1.to_json()
    assert text1 == snap1.to_json()
    assert snapshot_to_json(snap1) == text1
    assert snap1.to_bytes() == text1.encode("utf-8")
    assert snapshot_to_bytes(snap1) == text1.encode("utf-8")
    assert not text1.startswith("\ufeff")

    # Estructura JSON válida y con los campos del contrato de checkpoints.
    parsed = json.loads(text1)
    assert parsed["_checkpoint_version"] == CHECKPOINT_VERSION
    assert parsed["_checkpoint_type"] == "simengine"
    assert parsed["month"] == e1.month

    # La congelación conserva exactamente el mismo árbol que la captura.
    assert json.loads(snapshot_to_json(snapshot_from_state(checkpoint_to_dict(e1)))) == parsed


# ---------------------------------------------------------------------------
# §7.5 — Rechazo explícito de valores no serializables/inválidos
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("bad", "fragment"),
    [
        ({"a": b"xy"}, "tipo no serializable en $.a: bytes"),
        ({"a": {1, 2}}, "tipo no serializable en $.a: set"),
        ({"a": object()}, "tipo no serializable en $.a: object"),
        ({"a": float("nan")}, "float no finito en $.a"),
        ({"a": float("inf")}, "float no finito en $.a"),
        ({1: "x"}, "clave no str en $"),
        ({"a": {1: "x"}}, "clave no str en $.a"),
        ({"a": np.int64(7)}, "tipo no serializable en $.a: int64"),
        ({"a": np.arange(3)}, "tipo no serializable en $.a: ndarray"),
        ({"a": {"b": [1, [object()]]}}, "$.a.b[1][0]"),
    ],
)
def test_freeze_rejects_non_serializable_values(bad, fragment):
    with pytest.raises(SnapshotError) as excinfo:
        freeze_state(bad)
    message = str(excinfo.value)
    assert fragment in message, message
    assert isinstance(excinfo.value, ValueError)


def test_snapshot_construction_requires_valid_frozen_state(engine):
    # snapshot_from_state solo acepta dict de checkpoint.
    with pytest.raises(SnapshotError):
        snapshot_from_state([1, 2, 3])
    with pytest.raises(SnapshotError):
        snapshot_from_state({"a": object()})
    # Snapshot no se puede construir con datos mutables.
    with pytest.raises(SnapshotError):
        Snapshot(state={"month": 1})
    # Control positivo: el camino válido funciona.
    snap = snapshot_from_state(checkpoint_to_dict(engine))
    assert type(snap.state) is types.MappingProxyType
    assert snap.state["_checkpoint_version"] == CHECKPOINT_VERSION
    # Representación congelada exacta de una estructura pequeña.
    frozen = freeze_state({"a": {"b": [1, 2.5, None, True, "s"]}})
    assert type(frozen) is types.MappingProxyType
    inner = frozen["a"]
    assert type(inner) is types.MappingProxyType
    assert type(inner["b"]) is tuple
    assert inner["b"] == (1, 2.5, None, True, "s")


# ---------------------------------------------------------------------------
# §7.5 (complemento) — Garantías de la ruta rápida de congelación (Etapa 3-R1)
# ---------------------------------------------------------------------------

def test_freeze_fast_path_builds_exact_immutable_tuples():
    """Ruta rápida (lista 100% escalar): estructura e inmutabilidad intactas."""
    frozen = freeze_state({"a": [1.0, 2, None, True, "s"], "b": ("x", 3.5)})
    assert type(frozen["a"]) is tuple
    assert frozen["a"] == (1.0, 2, None, True, "s")
    assert type(frozen["b"]) is tuple
    assert frozen["b"] == ("x", 3.5)

    # Payloads anidados tipo ndarray (listas de listas) -> tuplas en todo el árbol.
    nested = freeze_state({"data": [[1.0, 2.0], [3.0, 4.0]]})
    assert type(nested["data"]) is tuple
    assert type(nested["data"][0]) is tuple
    assert nested["data"] == ((1.0, 2.0), (3.0, 4.0))

    assert freeze_state({"a": []})["a"] == ()


def test_freeze_fast_path_is_independent_of_source_list():
    """La ruta rápida no deja vivir contenedores del origen (§3.1)."""
    source = {"rows": [1.0, 2.0], "nested": [[3.0, 4.0]]}
    frozen = freeze_state(source)

    source["rows"].append(99.0)
    source["rows"][0] = -1.0
    source["nested"][0][0] = -1.0
    source["nested"].append([5.0, 6.0])

    assert frozen["rows"] == (1.0, 2.0)
    assert frozen["nested"] == ((3.0, 4.0),)


def test_freeze_fast_path_still_rejects_non_finite_floats_with_path():
    with pytest.raises(SnapshotError) as excinfo:
        freeze_state({"a": [1.0, 2.0, float("nan"), 4.0]})
    assert "$.a[2]" in str(excinfo.value)
    assert isinstance(excinfo.value, ValueError)

    with pytest.raises(SnapshotError) as excinfo:
        freeze_state({"a": [1.0, [float("-inf")]]})
    assert "$.a[1][0]" in str(excinfo.value)

    with pytest.raises(SnapshotError) as excinfo:
        freeze_state({"a": [float("inf")]})
    assert "$.a[0]" in str(excinfo.value)


def test_freeze_fast_path_rejects_numpy_scalars_hiding_in_lists():
    """Un np.float64/np.int64 no debe colarse: no es JSON-serializable."""
    with pytest.raises(SnapshotError) as excinfo:
        freeze_state({"a": [1.0, np.float64(2.0)]})
    assert "$.a[1]" in str(excinfo.value)
    assert "float64" in str(excinfo.value)

    with pytest.raises(SnapshotError) as excinfo:
        freeze_state({"a": [1.0, np.int64(3)]})
    assert "$.a[1]" in str(excinfo.value)
    assert "int64" in str(excinfo.value)


def test_freeze_fast_path_rejects_invalid_types_with_path():
    with pytest.raises(SnapshotError) as excinfo:
        freeze_state({"a": [1.0, b"bytes"]})
    assert "$.a[1]" in str(excinfo.value)

    with pytest.raises(SnapshotError) as excinfo:
        freeze_state({"a": [1.0, {1, 2}]})
    assert "$.a[1]" in str(excinfo.value)

    with pytest.raises(SnapshotError) as excinfo:
        freeze_state({"a": [1.0, [object()]]})
    assert "$.a[1][0]" in str(excinfo.value)


def test_freeze_fast_path_still_freezes_nested_containers():
    """Una lista mixta (escalar + contenedor) sigue congelándose recursivamente."""
    frozen = freeze_state({"a": [1.0, {"k": 1}, [2.0, 3.0]]})
    assert type(frozen["a"]) is tuple
    assert frozen["a"][0] == 1.0
    assert type(frozen["a"][1]) is types.MappingProxyType
    assert frozen["a"][1] == {"k": 1}
    assert type(frozen["a"][2]) is tuple
    assert frozen["a"][2] == (2.0, 3.0)


# ---------------------------------------------------------------------------
# §7.6 — Compatibilidad con el contrato de checkpoints existente
# ---------------------------------------------------------------------------

def test_snapshot_is_byte_compatible_with_checkpoint_contract(engine):
    for _ in range(60):
        engine.tick()
    snap = capture_snapshot(engine)

    reference = checkpoint_to_json(engine)
    # Byte a byte idéntico al contrato de checkpoints de la Fase 2.2.2.
    assert snap.to_json() == reference
    assert snap.to_bytes() == reference.encode("utf-8")
    assert json.loads(snap.to_json()) == checkpoint_to_dict(engine)
    assert snap.state["_checkpoint_version"] == CHECKPOINT_VERSION


def test_snapshot_round_trip_through_checkpoint_reader(engine):
    for _ in range(40):
        engine.tick()
    snap = capture_snapshot(engine)
    text = snap.to_json()

    # Capture -> serializar -> leer -> reconstruir con el lector existente.
    restored = engine_from_json(text)
    assert restored.month == engine.month
    assert [k.id for k in restored.kingdoms] == [k.id for k in engine.kingdoms]
    assert restored.chronicle._next_event_id == engine.chronicle._next_event_id
    assert len(restored.chronicle.events) == len(engine.chronicle.events)

    d_original = checkpoint_to_dict(engine)
    d_restored = checkpoint_to_dict(restored)
    for key in d_original:
        if key == "governments":
            # Comportamiento existente y aprobado: la restauración añade el
            # gobierno tribal (no forma parte de este cambio).
            original_names = {g["name"] for g in d_original[key]}
            restored_names = {g["name"] for g in d_restored[key]}
            assert original_names <= restored_names
        else:
            assert d_restored[key] == d_original[key], key

    # Estabilización: una segunda restauración produce bytes idénticos.
    j2 = checkpoint_to_json(restored)
    j3 = checkpoint_to_json(engine_from_json(j2))
    assert j2 == j3


# ---------------------------------------------------------------------------
# §7.7 — Captura consistente bajo la concurrencia disponible
# ---------------------------------------------------------------------------

def test_capture_is_consistent_under_async_concurrency(engine):
    """Concurrencia real de este diseño: el event loop de un solo hilo."""
    for _ in range(20):
        engine.tick()

    async def scenario():
        stop = asyncio.Event()
        captured: list[Snapshot] = []

        async def ticker():
            while not stop.is_set():
                engine.tick()
                await asyncio.sleep(0)

        async def capturer():
            while not stop.is_set():
                captured.append(capture_snapshot(engine))
                await asyncio.sleep(0)

        tick_task = asyncio.create_task(ticker())
        capture_task = asyncio.create_task(capturer())
        for _ in range(150):
            await asyncio.sleep(0)
        stop.set()
        await tick_task
        await capture_task
        return captured

    snapshots = asyncio.run(scenario())
    assert len(snapshots) >= 10

    months = [s.state["month"] for s in snapshots]
    assert months == sorted(months), "el mes nunca puede retroceder"
    for snap in snapshots:
        _assert_coherent(snap)


def test_capture_with_lock_is_consistent_under_thread_concurrency(engine):
    """Concurrencia entre hilos: el lock del llamador excluye los ticks."""
    for _ in range(10):
        engine.tick()

    lock = threading.Lock()
    stop = threading.Event()

    def mutate() -> None:
        while not stop.is_set():
            with lock:
                engine.tick()

    worker = threading.Thread(target=mutate, daemon=True)
    worker.start()
    try:
        for _ in range(15):
            snap = capture_snapshot(engine, lock=lock)
            _assert_coherent(snap)
    finally:
        stop.set()
        worker.join(timeout=10)

    # Sin interbloqueos: el lock queda libre tras cada captura.
    assert not worker.is_alive()
    assert lock.acquire(blocking=False)
    lock.release()


def test_capture_releases_caller_lock(engine):
    """El lock del llamador se sostiene solo durante la copia (§3.4)."""
    for _ in range(5):
        engine.tick()
    lock = threading.Lock()

    snap = capture_snapshot(engine, lock=lock)
    assert not lock.locked(), "el lock no debe seguir tomado tras la captura"
    text = snap.to_json()
    assert not lock.locked(), "el lock no debe tomarse durante la serialización"
    snap.to_bytes()
    assert not lock.locked()
    # El llamador puede volver a adquirirlo de inmediato.
    assert lock.acquire(blocking=False)
    lock.release()
    assert text == checkpoint_to_json(engine)


# ---------------------------------------------------------------------------
# §7.8 — Ausencia de operaciones SQLite durante la captura
# ---------------------------------------------------------------------------

def test_capture_performs_no_sqlite_operations(engine, monkeypatch):
    import sqlalchemy
    from sqlalchemy import event as sa_event

    from worldsim.server import db
    from worldsim.server import repository as repository_module

    class _UnexpectedSQLOperation(AssertionError):
        """Marca cualquier vía SQLite disparada durante la captura."""

    def _fail(*_args, **_kwargs):
        raise _UnexpectedSQLOperation(
            "operación SQLite inesperada durante la captura del snapshot"
        )

    # Sondeo: engine SQLite en memoria con contador de sentencias ejecutadas.
    probe = sqlalchemy.create_engine("sqlite://")
    statements: list[str] = []

    @sa_event.listens_for(probe, "before_cursor_execute")
    def _count_statements(
        conn, cursor, statement, parameters, context, executemany
    ):
        statements.append(statement)
        return None

    with probe.connect() as conn:
        conn.exec_driver_sql("SELECT 1")
    assert statements, "el instrumento de conteo no detectó la consulta de control"
    statements.clear()

    # Toda vía SQLite queda convertida en error durante la captura.
    monkeypatch.setattr(db, "SessionLocal", _fail)
    monkeypatch.setattr(db, "create_engine", _fail)
    monkeypatch.setattr(sqlalchemy, "create_engine", _fail)
    monkeypatch.setattr(repository_module, "CheckpointRepository", _fail)

    for _ in range(30):
        engine.tick()

    snap = capture_snapshot(engine)
    text = snap.to_json()
    payload = snap.to_bytes()

    assert statements == [], f"sentencias SQL durante la captura: {statements}"
    assert payload == text.encode("utf-8")
    assert text == checkpoint_to_json(engine)
    probe.dispose()


# ---------------------------------------------------------------------------
# (extra) Captura de solo lectura sobre el motor
# ---------------------------------------------------------------------------

def test_capture_is_read_only_on_engine(engine):
    for _ in range(30):
        engine.tick()
    reference = checkpoint_to_json(engine)
    month_before = engine.month

    snap = capture_snapshot(engine)
    snap.to_json()
    snap.to_bytes()

    # La captura ni siquiera roza el estado del motor.
    assert checkpoint_to_json(engine) == reference
    assert engine.month == month_before
    # Y el motor sigue operativo.
    engine.tick()
    assert engine.month > month_before
    _assert_coherent(snap)
