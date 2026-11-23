"""Pruebas de serialización completa del SimEngine (checkpoints).

Prueba la ronda completa: serializar -> restaurar -> continuar,
verificando que el motor restaurado evoluciona de forma idéntica al
motor original ininterrumpido.
"""

from __future__ import annotations

import json

import pytest

from worldsim.data.loaders import load_governments, load_races
from worldsim.export.checkpoint import (
    CHECKPOINT_VERSION,
    checkpoint_to_dict,
    engine_from_checkpoint,
    engine_from_json,
    checkpoint_to_json,
)
from worldsim.sim.engine import SimEngine
from worldsim.sim.models import EventType
from worldsim.worldgen import WorldConfig
from worldsim.worldgen.pipeline import generate_world


DATA = __import__("pathlib").Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(scope="session")
def data_dir():
    return DATA


@pytest.fixture(scope="session")
def races(data_dir):
    return load_races(data_dir / "Races.txt")


@pytest.fixture(scope="session")
def governments(data_dir):
    return load_governments(data_dir / "CivilizedGovernment.txt")


@pytest.fixture()
def small_world():
    return generate_world(WorldConfig(seed=42, width=100, height=50, rivers=4))


@pytest.fixture()
def engine(small_world, races, governments):
    return SimEngine(small_world, races, governments, seed=42)


# ---------------------------------------------------------------------------
# 1. Serializar un mundo recién creado
# ---------------------------------------------------------------------------

def test_serialize_fresh_engine(engine):
    state = checkpoint_to_dict(engine)
    assert state["_checkpoint_version"] == CHECKPOINT_VERSION
    assert state["_checkpoint_type"] == "simengine"
    assert state["month"] == 0
    assert state["seed"] == 42
    assert "_next_war_id" in state
    assert "_next_leader_id" in state
    assert "_next_hero_id" in state
    assert "_next_beast_id" in state
    assert "_next_notable_id" in state
    assert "_occupancy" in state
    assert "_battle_heat" in state
    assert "rng_state" in state
    assert "chronicle" in state


# ---------------------------------------------------------------------------
# 2. Restaurarlo y comprobar su mes
# ---------------------------------------------------------------------------

def test_restore_preserves_month(engine):
    # Avanzar algunos ticks
    for _ in range(30):
        engine.tick()
    original_month = engine.month

    state = checkpoint_to_dict(engine)
    restored = engine_from_checkpoint(state)
    assert restored.month == original_month


# ---------------------------------------------------------------------------
# 3. Comparar reinos, población, sitios, guerras y crónica
# ---------------------------------------------------------------------------

def test_restore_matches_original_state(small_world, races, governments):
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(100):
        e1.tick()

    state = checkpoint_to_dict(e1)
    e2 = engine_from_checkpoint(state)

    # Mes
    assert e1.month == e2.month

    # Rejos: mismo número y mismos atributos clave
    assert len(e1.kingdoms) == len(e2.kingdoms)
    for k1, k2 in zip(sorted(e1.kingdoms, key=lambda k: k.id), sorted(e2.kingdoms, key=lambda k: k.id)):
        assert k1.id == k2.id
        assert k1.name == k2.name
        assert k1.race.name == k2.race.name
        assert k1.government.name == k2.government.name
        assert k1.total_population == k2.total_population
        assert k1.at_war == k2.at_war
        assert k1.happiness == k2.happiness
        assert k1.faith == k2.faith
        assert k1.tech_level == k2.tech_level
        assert k1.civilized == k2.civilized
        assert k1.collapsed == k2.collapsed
        assert len(k1.sites) == len(k2.sites)
        for s1, s2 in zip(sorted(k1.sites, key=lambda s: (s.x, s.y)),
                          sorted(k2.sites, key=lambda s: (s.x, s.y))):
            assert s1.x == s2.x and s1.y == s2.y
            assert s1.name == s2.name
            assert s1.population == s2.population
            assert s1.popcap == s2.popcap
            assert s1.is_capital == s2.is_capital
            assert s1.traits == s2.traits

    # Guerras
    assert len(e1.wars) == len(e2.wars)
    for w1, w2 in zip(sorted(e1.wars, key=lambda w: w.id), sorted(e2.wars, key=lambda w: w.id)):
        assert w1.id == w2.id
        assert w1.name == w2.name
        assert w1.start_month == w2.start_month
        assert w1.end_month == w2.end_month
        assert w1.battles == w2.battles
        assert w1.total_dead == w2.total_dead
        assert (w1.victor.id if w1.victor else None) == (w2.victor.id if w2.victor else None)

    # Crónica
    assert len(e1.chronicle.events) == len(e2.chronicle.events)
    for ev1, ev2 in zip(e1.chronicle.events, e2.chronicle.events):
        assert ev1.id == ev2.id
        assert ev1.month == ev2.month
        assert ev1.type == ev2.type
        assert ev1.text == ev2.text
        assert ev1.kingdom_ids == ev2.kingdom_ids
        assert ev1.msg_key == ev2.msg_key


# ---------------------------------------------------------------------------
# 4. Avanzar ambos motores la misma cantidad de meses y comparar
# ---------------------------------------------------------------------------

def test_continued_simulation_matches(small_world, races, governments):
    """Prueba crítica: motor A avanza N, checkpoint, motor B restaura,
    ambos avanzan M más, y los estados deben ser idénticos."""
    e1 = SimEngine(small_world, races, governments, seed=11)

    # Fase 1: A avanza 80 meses
    for _ in range(80):
        e1.tick()

    # Checkpoint de A en mes 80
    state = checkpoint_to_dict(e1)

    # Fase 2: B restaura desde checkpoint
    e2 = engine_from_checkpoint(state)

    # Fase 3: Ambos avanzan 60 meses más (mes 140 total)
    for _ in range(60):
        e1.tick()
        e2.tick()

    # Comparar estados relevantes
    assert e1.month == e2.month == 140

    assert len(e1.kingdoms) == len(e2.kingdoms)
    for k1, k2 in zip(sorted(e1.kingdoms, key=lambda k: k.id), sorted(e2.kingdoms, key=lambda k: k.id)):
        assert k1.total_population == k2.total_population, f"Población difiere en {k1.name}"
        assert k1.at_war == k2.at_war
        assert k1.happiness == k2.happiness
        assert k1.tech_level == k2.tech_level
        assert len(k1.sites) == len(k2.sites)

    assert len(e1.wars) == len(e2.wars)
    assert len(e1.chronicle.events) == len(e2.chronicle.events)
    for ev1, ev2 in zip(e1.chronicle.events, e2.chronicle.events):
        assert ev1.text == ev2.text
        assert ev1.month == ev2.month

    # Bestias
    assert len(e1.beasts) == len(e2.beasts)
    for b1, b2 in zip(sorted(e1.beasts, key=lambda b: b.id), sorted(e2.beasts, key=lambda b: b.id)):
        assert b1.x == b2.x and b1.y == b2.y
        assert b1.hp == b2.hp

    # Relaciones
    assert e1.relations == e2.relations

    # Contadores internos
    assert e1._next_war_id == e2._next_war_id
    assert e1._next_leader_id == e2._next_leader_id
    assert e1._next_hero_id == e2._next_hero_id
    assert e1._next_beast_id == e2._next_beast_id
    assert e1._next_notable_id == e2._next_notable_id
    assert e1._color_counter == e2._color_counter

    # Mapas
    assert (e1._occupancy == e2._occupancy).all()
    assert (e1._battle_heat == e2._battle_heat).all()


# ---------------------------------------------------------------------------
# 5. Verificar que el RNG continúa desde exactamente el mismo punto
# ---------------------------------------------------------------------------

def test_rng_continues_exactly(small_world, races, governments):
    e1 = SimEngine(small_world, races, governments, seed=13)
    for _ in range(50):
        e1.tick()

    # Capturar el estado del RNG ANTES de consumir más números
    state = checkpoint_to_dict(e1)
    e2 = engine_from_checkpoint(state)

    # Ambos motores están en el mismo estado del RNG.
    # El siguiente número aleatorio debe ser idéntico en ambos.
    rng_val_before = float(e1.rng.random())
    rng_val_after = float(e2.rng.random())

    assert rng_val_before == rng_val_after, (
        "El RNG no continuó desde el mismo punto tras la restauración"
    )

    # Consumir otro y verificar que siguen sincronizados
    rng_val_before_2 = float(e1.rng.random())
    rng_val_after_2 = float(e2.rng.random())
    assert rng_val_before_2 == rng_val_after_2

    # Avanzar ambos un tick más y verificar que siguen sincronizados
    e1.tick()
    e2.tick()
    assert e1.month == e2.month
    assert len(e1.chronicle.events) == len(e2.chronicle.events)


# ---------------------------------------------------------------------------
# 6. Verificar que los IDs nuevos no colisionan
# ---------------------------------------------------------------------------

def test_new_ids_do_not_collide(small_world, races, governments):
    # Usar seed que genere guerras (seed=7 produce guerras dentro de 320 ticks)
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(200):
        e1.tick()

    state = checkpoint_to_dict(e1)
    e2 = engine_from_checkpoint(state)

    # Capturar IDs máximos antes
    max_war_1 = max((w.id for w in e1.wars), default=0)
    max_leader_1 = max((k.leader.id for k in e1.kingdoms if k.leader), default=0)

    # Avanzar ambos para generar nuevos IDs
    for _ in range(120):
        e1.tick()
        e2.tick()

    # Verificar que no hay IDs duplicados en guerras
    war_ids_1 = [w.id for w in e1.wars]
    war_ids_2 = [w.id for w in e2.wars]
    assert len(war_ids_1) == len(set(war_ids_1)), "IDs de guerra duplicados en A"
    assert len(war_ids_2) == len(set(war_ids_2)), "IDs de guerra duplicados en B"

    # Si hay guerras, los IDs deben ser continuos desde 1 sin colisiones
    if war_ids_2:
        assert min(war_ids_2) >= 1
        assert max(war_ids_2) == len(war_ids_2)  # IDs secuenciales desde 1

    # Los líderes deben tener IDs únicos y consistentes
    leader_ids_2 = [k.leader.id for k in e2.kingdoms if k.leader is not None]
    assert len(leader_ids_2) == len(set(leader_ids_2)), "IDs de líder duplicados"


# ---------------------------------------------------------------------------
# 7. Verificar mapas y arrays NumPy
# ---------------------------------------------------------------------------

def test_numpy_arrays_restored(small_world, races, governments):
    e1 = SimEngine(small_world, races, governments, seed=19)
    for _ in range(60):
        e1.tick()

    state = checkpoint_to_dict(e1)
    e2 = engine_from_checkpoint(state)

    # _occupancy
    assert e1._occupancy.dtype == e2._occupancy.dtype
    assert e1._occupancy.shape == e2._occupancy.shape
    assert (e1._occupancy == e2._occupancy).all()

    # _battle_heat
    assert e1._battle_heat.dtype == e2._battle_heat.dtype
    assert e1._battle_heat.shape == e2._battle_heat.shape
    assert (e1._battle_heat == e2._battle_heat).all()

    # Mapas del mundo
    for attr in ("height", "temperature", "precipitation", "drainage", "biome", "prosperity", "rivers"):
        a1 = getattr(e1.world, attr)
        a2 = getattr(e2.world, attr)
        assert a1.dtype == a2.dtype, f"Dtype difiere en {attr}"
        assert a1.shape == a2.shape, f"Shape difiere en {attr}"
        assert (a1 == a2).all(), f"Contenido difiere en {attr}"


# ---------------------------------------------------------------------------
# 8. Verificar referencias entre objetos restaurados
# ---------------------------------------------------------------------------

def test_object_references_restored(small_world, races, governments):
    e1 = SimEngine(small_world, races, governments, seed=23)
    for _ in range(300):
        e1.tick()

    state = checkpoint_to_dict(e1)
    e2 = engine_from_checkpoint(state)

    # Las guerras deben referenciar reinos válidos
    for w in e2.wars:
        assert w.side_a in e2.kingdoms or any(k.id == w.side_a.id for k in e2.kingdoms)
        assert w.side_b in e2.kingdoms or any(k.id == w.side_b.id for k in e2.kingdoms)
        if w.victor is not None:
            assert any(k.id == w.victor.id for k in e2.kingdoms)

    # Los reinos con líder deben tener el líder vinculado
    for k in e2.kingdoms:
        if k.leader is not None:
            assert k.leader.kingdom_id == k.id
        if k.hero is not None:
            assert k.hero.kingdom_id == k.id
        for n in k.notables:
            assert n.kingdom_id == k.id

    # Los eventos de crónica con kingdom_ids deben referenciar reinos existentes
    kingdom_ids = {k.id for k in e2.kingdoms}
    for ev in e2.chronicle.events:
        for kid in ev.kingdom_ids:
            assert kid in kingdom_ids, f"Evento {ev.id} referencia reino inexistente {kid}"


# ---------------------------------------------------------------------------
# 9. Verificar que una versión de checkpoint incompatible produce error
# ---------------------------------------------------------------------------

def test_incompatible_version_raises(engine):
    state = checkpoint_to_dict(engine)
    state["_checkpoint_version"] = 999
    with pytest.raises(ValueError, match="incompatible"):
        engine_from_checkpoint(state)


def test_invalid_type_raises(engine):
    state = checkpoint_to_dict(engine)
    state["_checkpoint_type"] = "not_simengine"
    with pytest.raises(ValueError, match="inválido"):
        engine_from_checkpoint(state)


# ---------------------------------------------------------------------------
# 10. Verificar que un checkpoint incompleto no se restaura silenciosamente
# ---------------------------------------------------------------------------

def test_incomplete_checkpoint_raises(engine):
    state = checkpoint_to_dict(engine)
    # Eliminar campo obligatorio
    del state["rng_state"]
    with pytest.raises((KeyError, ValueError)):
        engine_from_checkpoint(state)


def test_missing_month_raises(engine):
    state = checkpoint_to_dict(engine)
    del state["month"]
    with pytest.raises((KeyError, ValueError)):
        engine_from_checkpoint(state)


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------

def test_json_roundtrip(engine):
    for _ in range(40):
        engine.tick()
    json_str = checkpoint_to_json(engine)
    # Debe ser JSON válido
    parsed = json.loads(json_str)
    assert parsed["_checkpoint_version"] == CHECKPOINT_VERSION
    restored = engine_from_json(json_str)
    assert restored.month == engine.month


# ---------------------------------------------------------------------------
# Métodos de instancia en SimEngine
# ---------------------------------------------------------------------------

def test_engine_instance_methods(small_world, races, governments):
    e1 = SimEngine(small_world, races, governments, seed=29)
    for _ in range(50):
        e1.tick()

    state = e1.to_checkpoint_state()
    assert state["_checkpoint_version"] == CHECKPOINT_VERSION

    e2 = SimEngine.from_checkpoint_state(state)
    assert e2.month == e1.month
    assert len(e2.kingdoms) == len(e1.kingdoms)


# ---------------------------------------------------------------------------
# Aislamiento entre motores independientes (pruebas de Corrección 1.1)
# ---------------------------------------------------------------------------

def test_two_engines_independent_before_and_after_restore(
    small_world, races, governments
):
    """Dos motores independientes: restaurar uno no altera al otro."""
    e1 = SimEngine(small_world, races, governments, seed=31)
    e2 = SimEngine(small_world, races, governments, seed=37)

    # Fase 1: avanzar ambos 50 ticks
    for _ in range(50):
        e1.tick()
        e2.tick()

    # Capturar estado concreto de ANTES del checkpoint
    e1_month_before = e1.month
    e1_event_ids_before = [ev.id for ev in e1.chronicle.events]
    e1_next_event_id_before = e1.chronicle._next_event_id

    e2_month_before = e2.month
    e2_event_ids_before = [ev.id for ev in e2.chronicle.events]
    e2_next_event_id_before = e2.chronicle._next_event_id

    # Fase 2: serializar y restaurar SOLO e1
    state1 = checkpoint_to_dict(e1)
    e1_restored = engine_from_checkpoint(state1)

    # Fase 3: verificar que e2 NO cambió
    assert e2.month == e2_month_before, (
        "El motor e2 cambió su mes sin ser restaurado"
    )
    assert [ev.id for ev in e2.chronicle.events] == e2_event_ids_before, (
        "El motor e2 cambió sus eventos sin ser restaurado"
    )
    assert e2.chronicle._next_event_id == e2_next_event_id_before, (
        "El motor e2 cambió su contador de eventos sin ser restaurado"
    )

    # Fase 4: verificar que e1 restaurado conserva su estado
    assert e1_restored.month == e1_month_before
    assert [ev.id for ev in e1_restored.chronicle.events] == e1_event_ids_before
    assert e1_restored.chronicle._next_event_id == e1_next_event_id_before

    # Fase 5: avanzar ambos y verificar independencia continua
    e1_restored.tick()
    e2.tick()
    assert e1_restored.month == e1_month_before + 1
    assert e2.month == e2_month_before + 1


def test_restore_two_worlds_reverse_order_isolation(
    small_world, races, governments
):
    """Restaurar dos mundos en orden inverso debe ser seguro."""
    e1 = SimEngine(small_world, races, governments, seed=41)
    e2 = SimEngine(small_world, races, governments, seed=43)

    for _ in range(80):
        e1.tick()
        e2.tick()

    state1 = checkpoint_to_dict(e1)
    state2 = checkpoint_to_dict(e2)

    # Restaurar en orden inverso: primero e2, luego e1
    e2_restored = engine_from_checkpoint(state2)
    e1_restored = engine_from_checkpoint(state1)

    # Ambos deben conservar su mes y sus IDs de evento
    assert e1_restored.month == e1.month == 80
    assert e2_restored.month == e2.month == 80

    e1_event_ids = [ev.id for ev in e1_restored.chronicle.events]
    e2_event_ids = [ev.id for ev in e2_restored.chronicle.events]
    assert len(e1_event_ids) == len(set(e1_event_ids)), "IDs duplicados en e1 restaurado"
    assert len(e2_event_ids) == len(set(e2_event_ids)), "IDs duplicados en e2 restaurado"

    # Ambos pueden seguir avanzando de forma independiente
    e1_restored.tick()
    e2_restored.tick()
    assert e1_restored.month == 81
    assert e2_restored.month == 81


def test_event_ids_unique_within_each_world(small_world, races, governments):
    """Los IDs de eventos deben ser únicos dentro de cada mundo restaurado."""
    e1 = SimEngine(small_world, races, governments, seed=47)
    e2 = SimEngine(small_world, races, governments, seed=53)

    for _ in range(100):
        e1.tick()
        e2.tick()

    r1 = engine_from_checkpoint(checkpoint_to_dict(e1))
    r2 = engine_from_checkpoint(checkpoint_to_dict(e2))

    ids_1 = [ev.id for ev in r1.chronicle.events]
    ids_2 = [ev.id for ev in r2.chronicle.events]

    assert len(ids_1) == len(set(ids_1)), f"IDs duplicados en mundo 1: {ids_1}"
    assert len(ids_2) == len(set(ids_2)), f"IDs duplicados en mundo 2: {ids_2}"
    assert min(ids_1) >= 1
    assert min(ids_2) >= 1

    # Los IDs deben ser secuenciales desde 1 dentro de cada mundo
    assert sorted(ids_1) == list(range(1, len(ids_1) + 1))
    assert sorted(ids_2) == list(range(1, len(ids_2) + 1))


def test_event_ids_unique_within_each_world(small_world, races, governments):
    """Los IDs de eventos deben ser únicos dentro de cada mundo restaurado."""
    e1 = SimEngine(small_world, races, governments, seed=47)
    e2 = SimEngine(small_world, races, governments, seed=53)

    for _ in range(100):
        e1.tick()
        e2.tick()

    r1 = engine_from_checkpoint(checkpoint_to_dict(e1))
    r2 = engine_from_checkpoint(checkpoint_to_dict(e2))

    ids_1 = [ev.id for ev in r1.chronicle.events]
    ids_2 = [ev.id for ev in r2.chronicle.events]

    assert len(ids_1) == len(set(ids_1)), f"IDs duplicados en mundo 1: {ids_1}"
    assert len(ids_2) == len(set(ids_2)), f"IDs duplicados en mundo 2: {ids_2}"
    assert min(ids_1) >= 1
    assert min(ids_2) >= 1

    # Los IDs deben ser secuenciales desde 1 dentro de cada mundo
    assert sorted(ids_1) == list(range(1, len(ids_1) + 1))
    assert sorted(ids_2) == list(range(1, len(ids_2) + 1))


def test_war_to_kingdom_reference_identity(small_world, races, governments):
    """Las guerras restauradas deben referenciar las mismas instancias de reino."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(200):
        e1.tick()

    if not e1.wars:
        pytest.skip("No se generaron guerras con esta semilla en 200 ticks")

    state = checkpoint_to_dict(e1)
    e2 = engine_from_checkpoint(state)

    kingdoms_by_id_e2 = {k.id: k for k in e2.kingdoms}

    for w in e2.wars:
        # side_a y side_b deben ser instancias de los reinos de e2
        assert w.side_a.id in kingdoms_by_id_e2
        assert w.side_b.id in kingdoms_by_id_e2
        # La instancia debe ser la misma que está en la lista de reinos
        assert w.side_a is kingdoms_by_id_e2[w.side_a.id]
        assert w.side_b is kingdoms_by_id_e2[w.side_b.id]
        if w.victor is not None:
            assert w.victor.id in kingdoms_by_id_e2
            assert w.victor is kingdoms_by_id_e2[w.victor.id]


def test_kingdom_to_leader_reference_identity(small_world, races, governments):
    """Los reinos restaurados deben referenciar la misma instancia de líder."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(200):
        e1.tick()

    state = checkpoint_to_dict(e1)
    e2 = engine_from_checkpoint(state)

    for k in e2.kingdoms:
        if k.leader is not None:
            # El líder del reino debe tener kingdom_id consistente
            assert k.leader.kingdom_id == k.id
        if k.hero is not None:
            assert k.hero.kingdom_id == k.id
        for n in k.notables:
            assert n.kingdom_id == k.id


def test_namegenerator_restored_with_rng(small_world, races, governments):
    """El NameGenerator del motor restaurado debe usar el RNG restaurado."""
    e1 = SimEngine(small_world, races, governments, seed=61)
    for _ in range(50):
        e1.tick()

    # Crear el checkpoint ANTES de consumir n�meros extra del RNG
    state = checkpoint_to_dict(e1)
    e2 = engine_from_checkpoint(state)

    # El RNG de e2 debe estar en el mismo estado que e1 (misma secuencia)
    # No se llama a namegen.name() antes porque consume n�meros del RNG.
    rng_val_1 = float(e1.rng.random())
    rng_val_2 = float(e2.rng.random())
    assert rng_val_1 == rng_val_2, (
        "El RNG del NameGenerator no est� sincronizado tras la restauraci�n"
    )

    # El NameGenerator debe poder generar nombres usando el RNG restaurado
    name1 = e1.namegen.name()
    name2 = e2.namegen.name()
    assert isinstance(name1, str) and len(name1) > 0
    assert isinstance(name2, str) and len(name2) > 0
    # Ambos NameGenerator comparten el mismo RNG, por lo que generan
    # la misma secuencia de nombres
    assert name1 == name2, (
        f"NameGenerator divergi�: {name1!r} vs {name2!r}"
    )


def test_event_params_preserve_types(small_world, races, governments):
    """Los parámetros de los eventos deben conservar sus tipos tras la restauración."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(200):
        e1.tick()

    state = checkpoint_to_dict(e1)
    e2 = engine_from_checkpoint(state)

    for ev1, ev2 in zip(e1.chronicle.events, e2.chronicle.events):
        assert ev1.params.keys() == ev2.params.keys(), (
            f"Claves de params difieren en evento {ev1.id}"
        )
        for key in ev1.params:
            v1, v2 = ev1.params[key], ev2.params[key]
            assert type(v1) is type(v2), (
                f"Tipo de param '{key}' difiere en evento {ev1.id}: "
                f"{type(v1)} vs {type(v2)}"
            )
            if isinstance(v1, float):
                assert v1 == v2, f"Valor float difiere en evento {ev1.id}, key {key}"
            else:
                assert v1 == v2, f"Valor difiere en evento {ev1.id}, key {key}"


def test_worldconfig_all_fields_preserved(small_world, races, governments):
    """Todos los campos de WorldConfig deben conservarse en el checkpoint."""
    e1 = SimEngine(small_world, races, governments, seed=67)
    for _ in range(30):
        e1.tick()

    state = checkpoint_to_dict(e1)
    cfg = state["world_config"]

    expected_fields = {
        "width", "height", "seed", "big_hills", "small_hills",
        "rivers", "margin", "sea_level",
    }
    assert set(cfg.keys()) == expected_fields, (
        f"Campos de WorldConfig faltantes o extra: {set(cfg.keys()) ^ expected_fields}"
    )

    e2 = engine_from_checkpoint(state)
    assert e2.world.config.width == cfg["width"]
    assert e2.world.config.height == cfg["height"]
    assert e2.world.config.seed == cfg["seed"]
    assert e2.world.config.big_hills == cfg["big_hills"]
    assert e2.world.config.small_hills == cfg["small_hills"]
    assert e2.world.config.rivers == cfg["rivers"]
    assert e2.world.config.margin == cfg["margin"]
    assert e2.world.config.sea_level == cfg["sea_level"]


def test_complete_kingdom_field_comparison(small_world, races, governments):
    """Comparar todos los campos relevantes de reinos tras restauración."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(150):
        e1.tick()

    state = checkpoint_to_dict(e1)
    e2 = engine_from_checkpoint(state)

    for k1, k2 in zip(sorted(e1.kingdoms, key=lambda k: k.id),
                      sorted(e2.kingdoms, key=lambda k: k.id)):
        # Campos escalares
        assert k1.id == k2.id
        assert k1.name == k2.name
        assert k1.race.name == k2.race.name
        assert k1.government.name == k2.government.name
        assert k1.color == k2.color
        assert k1.total_population == k2.total_population
        assert k1.at_war == k2.at_war
        assert k1.army_size == k2.army_size
        assert k1.army_pos == k2.army_pos
        assert k1.war_exhaustion == k2.war_exhaustion
        assert k1.years_since_founding == k2.years_since_founding
        assert k1.founded_month == k2.founded_month
        assert k1.last_gov_shift_month == k2.last_gov_shift_month
        assert k1.tech_level == k2.tech_level
        assert k1.tech_progress == k2.tech_progress
        assert k1.techs == k2.techs
        assert k1.mods == k2.mods
        assert k1.culture == k2.culture
        assert k1.civilized == k2.civilized
        assert k1.golden_age_until == k2.golden_age_until
        assert k1.drought_until == k2.drought_until
        assert k1.happiness == k2.happiness
        assert k1.faith == k2.faith
        assert k1.temples == k2.temples
        assert k1.trade_routes == k2.trade_routes
        assert k1.trader_kindred == k2.trader_kindred
        assert k1._last_disaster_month == k2._last_disaster_month
        assert k1._last_plague_month == k2._last_plague_month
        assert k1.wars_won == k2.wars_won
        assert k1.wars_lost == k2.wars_lost
        assert k1.collapsed == k2.collapsed

        # Sitios
        assert len(k1.sites) == len(k2.sites)
        for s1, s2 in zip(sorted(k1.sites, key=lambda s: (s.x, s.y)),
                          sorted(k2.sites, key=lambda s: (s.x, s.y))):
            assert s1.x == s2.x and s1.y == s2.y
            assert s1.name == s2.name
            assert s1.category == s2.category
            assert s1.population == s2.population
            assert s1.popcap == s2.popcap
            assert s1.is_capital == s2.is_capital
            assert s1.traits == s2.traits

        # Población histórica
        assert k1.population_history == k2.population_history


# ---------------------------------------------------------------------------
# Compatibilidad con checkpoints antiguos (sin _next_event_id)
# ---------------------------------------------------------------------------

def test_restore_checkpoint_without_next_event_id(small_world, races, governments):
    """Un checkpoint antiguo sin _next_event_id debe recuperarse calculando
    max(event.id) + 1."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(100):
        e1.tick()

    state = checkpoint_to_dict(e1)
    # Eliminar _next_event_id para simular checkpoint antiguo
    del state["chronicle"]["_next_event_id"]

    restored = engine_from_checkpoint(state)
    max_id = max((ev.id for ev in e1.chronicle.events), default=0)
    assert restored.chronicle._next_event_id == max_id + 1


def test_restore_checkpoint_with_events_but_no_counter(
    small_world, races, governments
):
    """Checkpoint con eventos pero sin contador: calcular desde max ID."""
    e1 = SimEngine(small_world, races, governments, seed=11)
    for _ in range(50):
        e1.tick()

    state = checkpoint_to_dict(e1)
    chronicle = state["chronicle"]
    del chronicle["_next_event_id"]
    # Asegurar que hay eventos
    assert len(chronicle["events"]) > 0

    restored = engine_from_checkpoint(state)
    max_id = max((ev.id for ev in e1.chronicle.events), default=0)
    assert restored.chronicle._next_event_id == max_id + 1


def test_register_new_event_after_restore_no_collision(
    small_world, races, governments
):
    """Tras restaurar, registrar un nuevo evento no debe colisionar con IDs
    existentes."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(100):
        e1.tick()

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    existing_ids = {ev.id for ev in restored.chronicle.events}
    next_id = restored.chronicle._next_event_id

    # Registrar un evento nuevo directamente en la crónica restaurada
    restored.chronicle.log(
        restored.month,
        EventType.KINGDOM_FOUNDED,
        "test event after restore",
        (),
        {},
        "",
    )
    new_event = restored.chronicle.events[-1]
    assert new_event.id == next_id
    assert new_event.id not in existing_ids - {new_event.id}
    assert restored.chronicle._next_event_id == next_id + 1


def test_restore_checkpoint_with_inconsistent_counter(
    small_world, races, governments
):
    """Si el contador guardado es <= max(event.id), usar max + 1."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(100):
        e1.tick()

    state = checkpoint_to_dict(e1)
    # Establecer contador inconsistente (inferior al máximo ID existente)
    max_id = max((ev.id for ev in e1.chronicle.events), default=0)
    state["chronicle"]["_next_event_id"] = 1  # inconsistente

    restored = engine_from_checkpoint(state)
    assert restored.chronicle._next_event_id == max_id + 1


def test_restore_checkpoint_with_negative_counter(
    small_world, races, governments
):
    """Un contador negativo debe tratarse como inconsistente."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    state = checkpoint_to_dict(e1)
    state["chronicle"]["_next_event_id"] = -5

    restored = engine_from_checkpoint(state)
    max_id = max((ev.id for ev in e1.chronicle.events), default=0)
    assert restored.chronicle._next_event_id == max_id + 1


# ---------------------------------------------------------------------------
# Parámetros de eventos con tipos variados
# ---------------------------------------------------------------------------

def test_event_params_various_types(small_world, races, governments):
    """Los parámetros de eventos pueden incluir strings, ints, floats,
    booleanos, listas y diccionarios anidados."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(200):
        e1.tick()

    # Buscar un evento y verificar que sus params son JSON-serializables
    state = checkpoint_to_dict(e1)
    json_str = checkpoint_to_json(e1)  # lanza si no es serializable
    parsed = json.loads(json_str)

    for ev_dict in parsed["chronicle"]["events"]:
        params = ev_dict["params"]
        for key, val in params.items():
            assert isinstance(val, (str, int, float, bool, type(None), list, dict)), (
                f"Parámetro {key!r} de tipo {type(val).__name__} no es JSON-safe"
            )
            if isinstance(val, list):
                for item in val:
                    assert isinstance(item, (str, int, float, bool, type(None), list, dict)), (
                        f"Ítem de lista {item!r} de tipo {type(item).__name__} no es JSON-safe"
                    )


def test_event_params_nested_structures(small_world, races, governments):
    """Verificar roundtrip de parámetros anidados en eventos."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(200):
        e1.tick()

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    for ev1, ev2 in zip(e1.chronicle.events, restored.chronicle.events):
        assert ev1.params == ev2.params, (
            f"Params difieren en evento {ev1.id}"
        )


# ---------------------------------------------------------------------------
# Validación de BitGenerator del RNG
# ---------------------------------------------------------------------------

def test_rng_bit_generator_is_pcg64(small_world, races, governments):
    """El BitGenerator del RNG del motor debe ser PCG64."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    bg_name = e1.rng.bit_generator.state["bit_generator"]
    assert bg_name == "PCG64", f"BitGenerator inesperado: {bg_name}"


def test_restore_rejects_unknown_bit_generator(
    small_world, races, governments
):
    """Un checkpoint con BitGenerator desconocido debe rechazar la restauración."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    state = checkpoint_to_dict(e1)
    state["rng_state"]["bit_generator"] = "MT19937"  # no soportado

    from worldsim.export.checkpoint import _COMPATIBLE_BIT_GENERATORS
    assert "MT19937" not in _COMPATIBLE_BIT_GENERATORS

    with pytest.raises(ValueError, match="no compatible"):
        engine_from_checkpoint(state)


# ---------------------------------------------------------------------------
# Continuación determinista completa (prueba central)
# ---------------------------------------------------------------------------

def test_deterministic_continuation_complete(
    small_world, races, governments
):
    """Prueba central: motor A avanza N meses, checkpoint, motor B restaura,
    ambos avanzan M meses más, y TODOS los estados deben ser idénticos."""
    e1 = SimEngine(small_world, races, governments, seed=11)

    # Fase 1: A avanza 100 meses
    for _ in range(100):
        e1.tick()

    # Fase 2: checkpoint y restauración
    state = checkpoint_to_dict(e1)
    e2 = engine_from_checkpoint(state)

    # Fase 3: Ambos avanzan 80 meses más (mes 180 total)
    for _ in range(80):
        e1.tick()
        e2.tick()

    # Comparación completa normalizada
    _assert_engines_identical(e1, e2)


def _assert_engines_identical(e1: SimEngine, e2: SimEngine) -> None:
    """Comprueba que dos motores tienen el estado completo idéntico."""
    # Mes y semilla
    assert e1.month == e2.month, f"Mes difiere: {e1.month} vs {e2.month}"
    assert e1.world.config.seed == e2.world.config.seed

    # RNG: mismo estado
    state1 = e1.rng.bit_generator.state
    state2 = e2.rng.bit_generator.state
    assert state1["bit_generator"] == state2["bit_generator"]
    assert state1["has_uint32"] == state2["has_uint32"]
    assert state1["uinteger"] == state2["uinteger"]
    assert state1["state"]["state"] == state2["state"]["state"]
    assert state1["state"]["inc"] == state2["state"]["inc"]

    # Mapas NumPy
    for attr in ("height", "temperature", "precipitation", "drainage", "biome", "prosperity", "rivers"):
        a1 = getattr(e1.world, attr)
        a2 = getattr(e2.world, attr)
        assert a1.dtype == a2.dtype, f"Dtype difiere en {attr}"
        assert a1.shape == a2.shape, f"Shape difiere en {attr}"
        assert (a1 == a2).all(), f"Contenido difiere en {attr}"

    # _occupancy y _battle_heat
    assert (e1._occupancy == e2._occupancy).all()
    assert (e1._battle_heat == e2._battle_heat).all()

    # Contadores internos
    assert e1._next_war_id == e2._next_war_id
    assert e1._next_leader_id == e2._next_leader_id
    assert e1._next_hero_id == e2._next_hero_id
    assert e1._next_beast_id == e2._next_beast_id
    assert e1._next_notable_id == e2._next_notable_id
    assert e1._color_counter == e2._color_counter

    # Sets
    assert e1._milestones == e2._milestones
    assert e1._world_techs == e2._world_techs

    # Fallen
    assert e1.fallen == e2.fallen

    # Reinos completos
    assert len(e1.kingdoms) == len(e2.kingdoms)
    for k1, k2 in zip(sorted(e1.kingdoms, key=lambda k: k.id),
                      sorted(e2.kingdoms, key=lambda k: k.id)):
        _assert_kingdoms_identical(k1, k2)

    # Guerras completas
    assert len(e1.wars) == len(e2.wars)
    for w1, w2 in zip(sorted(e1.wars, key=lambda w: w.id),
                      sorted(e2.wars, key=lambda w: w.id)):
        _assert_wars_identical(w1, w2)

    # Bestias
    assert len(e1.beasts) == len(e2.beasts)
    for b1, b2 in zip(sorted(e1.beasts, key=lambda b: b.id),
                      sorted(e2.beasts, key=lambda b: b.id)):
        assert b1.id == b2.id
        assert b1.name == b2.name
        assert b1.race.name == b2.race.name
        assert b1.x == b2.x and b1.y == b2.y
        assert b1.hp == b2.hp
        assert b1.power == b2.power
        assert b1.aggression == b2.aggression

    # Relaciones
    assert e1.relations == e2.relations

    # Crónica completa
    assert len(e1.chronicle.events) == len(e2.chronicle.events)
    for ev1, ev2 in zip(e1.chronicle.events, e2.chronicle.events):
        assert ev1.id == ev2.id
        assert ev1.month == ev2.month
        assert ev1.type == ev2.type
        assert ev1.text == ev2.text
        assert ev1.kingdom_ids == ev2.kingdom_ids
        assert ev1.params == ev2.params
        assert ev1.msg_key == ev2.msg_key

    # Contador de crónica
    assert e1.chronicle._next_event_id == e2.chronicle._next_event_id


def _assert_kingdoms_identical(k1, k2) -> None:
    """Comprueba que dos reinos tienen el estado idéntico."""
    assert k1.id == k2.id
    assert k1.name == k2.name
    assert k1.race.name == k2.race.name
    assert k1.government.name == k2.government.name
    assert k1.color == k2.color
    assert k1.total_population == k2.total_population
    assert k1.at_war == k2.at_war
    assert k1.army_size == k2.army_size
    assert k1.army_pos == k2.army_pos
    assert k1.war_exhaustion == k2.war_exhaustion
    assert k1.years_since_founding == k2.years_since_founding
    assert k1.founded_month == k2.founded_month
    assert k1.last_gov_shift_month == k2.last_gov_shift_month
    assert k1.tech_level == k2.tech_level
    assert k1.tech_progress == k2.tech_progress
    assert k1.techs == k2.techs
    assert k1.mods == k2.mods
    assert k1.culture == k2.culture
    assert k1.civilized == k2.civilized
    assert k1.golden_age_until == k2.golden_age_until
    assert k1.drought_until == k2.drought_until
    assert k1.happiness == k2.happiness
    assert k1.faith == k2.faith
    assert k1.temples == k2.temples
    assert k1.trade_routes == k2.trade_routes
    assert k1.trader_kindred == k2.trader_kindred
    assert k1._last_disaster_month == k2._last_disaster_month
    assert k1._last_plague_month == k2._last_plague_month
    assert k1.wars_won == k2.wars_won
    assert k1.wars_lost == k2.wars_lost
    assert k1.collapsed == k2.collapsed

    # Referencia a líder (misma instancia o equivalente)
    if k1.leader is None:
        assert k2.leader is None
    else:
        assert k2.leader is not None
        assert k1.leader.id == k2.leader.id
        assert k1.leader.name == k2.leader.name
        assert k1.leader.title == k2.leader.title
        assert k1.leader.born_month == k2.leader.born_month
        assert k1.leader.died_month == k2.leader.died_month
        assert k1.leader.expected_death_month == k2.leader.expected_death_month
        assert k1.leader.battles_won == k2.leader.battles_won
        assert k1.leader.battles_lost == k2.leader.battles_lost

    # Referencia a héroe
    if k1.hero is None:
        assert k2.hero is None
    else:
        assert k2.hero is not None
        assert k1.hero.id == k2.hero.id
        assert k1.hero.name == k2.hero.name
        assert k1.hero.strength == k2.hero.strength
        assert k1.hero.battle_deeds == k2.hero.battle_deeds

    # Notables
    assert len(k1.notables) == len(k2.notables)
    for n1, n2 in zip(sorted(k1.notables, key=lambda n: n.id),
                      sorted(k2.notables, key=lambda n: n.id)):
        assert n1.id == n2.id
        assert n1.name == n2.name
        assert n1.role == n2.role
        assert n1.ambition == n2.ambition
        assert n1.piety == n2.piety
        assert n1.greed == n2.greed
        assert n1.born_month == n2.born_month
        assert n1.died_month == n2.died_month

    # Sitios
    assert len(k1.sites) == len(k2.sites)
    for s1, s2 in zip(sorted(k1.sites, key=lambda s: (s.x, s.y)),
                      sorted(k2.sites, key=lambda s: (s.x, s.y))):
        assert s1.x == s2.x and s1.y == s2.y
        assert s1.name == s2.name
        assert s1.category == s2.category
        assert s1.population == s2.population
        assert s1.popcap == s2.popcap
        assert s1.is_capital == s2.is_capital
        assert s1.traits == s2.traits

    # Población histórica
    assert k1.population_history == k2.population_history


def _assert_wars_identical(w1, w2) -> None:
    """Comprueba que dos guerras tienen el estado idéntico."""
    assert w1.id == w2.id
    assert w1.name == w2.name
    assert w1.cause == w2.cause
    assert w1.start_month == w2.start_month
    assert w1.end_month == w2.end_month
    assert w1.battles == w2.battles
    assert w1.total_dead == w2.total_dead
    assert (w1.victor.id if w1.victor else None) == \
           (w2.victor.id if w2.victor else None)
    assert w1.battle_sites == w2.battle_sites
    assert [c.id for c in w1.cobelligerents_a] == \
           [c.id for c in w2.cobelligerents_a]
    assert [c.id for c in w1.cobelligerents_b] == \
           [c.id for c in w2.cobelligerents_b]


# ---------------------------------------------------------------------------
# Validación estricta de _next_event_id (Corrección 1.2.1, punto 1)
# ---------------------------------------------------------------------------

def test_next_event_id_accepts_valid_integer(
    small_world, races, governments
):
    """Un entero válido mayor al max ID existente debe conservarse."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(100):
        e1.tick()

    state = checkpoint_to_dict(e1)
    max_id = max((ev.id for ev in e1.chronicle.events), default=0)
    state["chronicle"]["_next_event_id"] = max_id + 100

    restored = engine_from_checkpoint(state)
    assert restored.chronicle._next_event_id == max_id + 100


def test_next_event_id_rejects_true(
    small_world, races, governments
):
    """True debe rechazarse y recuperarse desde max(event.id) + 1."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(100):
        e1.tick()

    state = checkpoint_to_dict(e1)
    state["chronicle"]["_next_event_id"] = True

    restored = engine_from_checkpoint(state)
    max_id = max((ev.id for ev in e1.chronicle.events), default=0)
    assert restored.chronicle._next_event_id == max_id + 1


def test_next_event_id_rejects_false(
    small_world, races, governments
):
    """False debe rechazarse y recuperarse desde max(event.id) + 1."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(100):
        e1.tick()

    state = checkpoint_to_dict(e1)
    state["chronicle"]["_next_event_id"] = False

    restored = engine_from_checkpoint(state)
    max_id = max((ev.id for ev in e1.chronicle.events), default=0)
    assert restored.chronicle._next_event_id == max_id + 1


def test_next_event_id_rejects_float(
    small_world, races, governments
):
    """3.5 debe rechazarse y recuperarse desde max(event.id) + 1."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(100):
        e1.tick()

    state = checkpoint_to_dict(e1)
    state["chronicle"]["_next_event_id"] = 3.5

    restored = engine_from_checkpoint(state)
    max_id = max((ev.id for ev in e1.chronicle.events), default=0)
    assert restored.chronicle._next_event_id == max_id + 1


def test_next_event_id_rejects_string(
    small_world, races, governments
):
    """"10" debe rechazarse y recuperarse desde max(event.id) + 1."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(100):
        e1.tick()

    state = checkpoint_to_dict(e1)
    state["chronicle"]["_next_event_id"] = "10"

    restored = engine_from_checkpoint(state)
    max_id = max((ev.id for ev in e1.chronicle.events), default=0)
    assert restored.chronicle._next_event_id == max_id + 1


def test_next_event_id_rejects_none(
    small_world, races, governments
):
    """None debe recuperarse desde max(event.id) + 1."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(100):
        e1.tick()

    state = checkpoint_to_dict(e1)
    state["chronicle"]["_next_event_id"] = None

    restored = engine_from_checkpoint(state)
    max_id = max((ev.id for ev in e1.chronicle.events), default=0)
    assert restored.chronicle._next_event_id == max_id + 1


def test_next_event_id_inconsistent_recovers(
    small_world, races, governments
):
    """Un entero menor o igual al max ID existente debe recuperarse."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(100):
        e1.tick()

    state = checkpoint_to_dict(e1)
    max_id = max((ev.id for ev in e1.chronicle.events), default=0)
    state["chronicle"]["_next_event_id"] = 1  # inconsistente

    restored = engine_from_checkpoint(state)
    assert restored.chronicle._next_event_id == max_id + 1


def test_next_event_id_new_event_no_collision_after_restore(
    small_world, races, governments
):
    """Después de restaurar con contador válido, un nuevo evento no colisiona."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(100):
        e1.tick()

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    existing_ids = {ev.id for ev in restored.chronicle.events}
    next_id = restored.chronicle._next_event_id

    restored.chronicle.log(
        restored.month,
        EventType.KINGDOM_FOUNDED,
        "test new event",
        (),
        {},
        "",
    )
    new_event = restored.chronicle.events[-1]
    assert new_event.id == next_id
    assert new_event.id not in existing_ids


# ---------------------------------------------------------------------------
# JSON estricto en parámetros de eventos (Corrección 1.2.1, punto 2)
# ---------------------------------------------------------------------------

def test_event_params_rejects_nan(small_world, races, governments):
    """NaN en parámetros debe rechazarse al serializar."""
    import math
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    # Inyectar un evento con NaN en params
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test nan", (), {"value": float("nan")}, ""
    )

    with pytest.raises((ValueError, OverflowError)):
        checkpoint_to_json(e1)


def test_event_params_rejects_infinity(small_world, races, governments):
    """Infinity en parámetros debe rechazarse al serializar."""
    import math
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test inf", (), {"value": float("inf")}, ""
    )

    with pytest.raises((ValueError, OverflowError)):
        checkpoint_to_json(e1)


def test_event_params_rejects_negative_infinity(
    small_world, races, governments
):
    """-Infinity en parámetros debe rechazarse al serializar."""
    import math
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test -inf", (), {"value": float("-inf")}, ""
    )

    with pytest.raises((ValueError, OverflowError)):
        checkpoint_to_json(e1)


def test_event_params_numpy_nan_rejected(small_world, races, governments):
    """NaN de NumPy en parámetros debe rechazarse."""
    import numpy as np
    import math
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test np nan", (), {"value": np.float64(float("nan"))}, ""
    )

    with pytest.raises((ValueError, OverflowError)):
        checkpoint_to_json(e1)


def test_event_params_finite_floats_ok(small_world, races, governments):
    """Floats finitos deben serializarse y restaurarse correctamente."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(200):
        e1.tick()

    # Agregar un evento con floats finitos
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test finite", (), {"ratio": 0.5, "count": 42, "name": "test"}, ""
    )

    state = checkpoint_to_dict(e1)
    json_str = checkpoint_to_json(e1)  # debe tener éxito
    restored = engine_from_json(json_str)

    # Verificar que el evento con params finitos se restauró
    last_ev = restored.chronicle.events[-1]
    assert last_ev.params["ratio"] == 0.5
    assert last_ev.params["count"] == 42
    assert last_ev.params["name"] == "test"


# ---------------------------------------------------------------------------
# Marcador inequívoco de ndarray en parámetros (Corrección 1.2.1, punto 3)
# ---------------------------------------------------------------------------

def test_params_dict_with_dunder_type_key_preserved(
    small_world, races, governments
):
    """Un diccionario de usuario con clave __type__ debe conservarse intacto."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    # Diccionario legítimo que contiene __type__ pero no es un array codificado
    user_dict = {"__type__": "ndarray", "custom": "value", "other": 42}
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test dict type", (), {"meta": user_dict}, ""
    )

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    assert last_ev.params["meta"] == user_dict
    assert last_ev.params["meta"]["__type__"] == "ndarray"
    assert last_ev.params["meta"]["custom"] == "value"
    assert last_ev.params["meta"]["other"] == 42


def test_params_dict_with_extra_keys_preserved(
    small_world, races, governments
):
    """Un diccionario con claves adicionales a __type__ debe preservarse."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    user_dict = {"__type__": "custom_type", "dtype": "float64", "data": [1, 2]}
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test extra keys", (), {"meta": user_dict}, ""
    )

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    assert last_ev.params["meta"] == user_dict


def test_params_numpy_array_in_dict_roundtrip(
    small_world, races, governments
):
    """Un array NumPy dentro de un diccionario debe restaurarse correctamente."""
    import numpy as np
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    arr = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float64)
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test array in dict", (), {"matrix": arr}, ""
    )

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    restored_arr = last_ev.params["matrix"]
    assert isinstance(restored_arr, np.ndarray)
    assert restored_arr.dtype == arr.dtype
    assert restored_arr.shape == arr.shape
    assert (restored_arr == arr).all()


def test_params_numpy_array_in_nested_list_roundtrip(
    small_world, races, governments
):
    """Un array NumPy dentro de una lista anidada debe restaurarse."""
    import numpy as np
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    arr = np.array([10, 20, 30], dtype=np.int32)
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test array in list", (), {"items": [arr, "texto", 5]}, ""
    )

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    items = last_ev.params["items"]
    assert isinstance(items[0], np.ndarray)
    assert items[0].dtype == arr.dtype
    assert items[0].shape == arr.shape
    assert (items[0] == arr).all()
    assert items[1] == "texto"
    assert items[2] == 5


def test_params_roundtrip_complete(small_world, races, governments):
    """Roundtrip completo: serializar, restaurar, verificar params idénticos."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(200):
        e1.tick()

    # Varios eventos con distintos tipos de params
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "e1", (), {"a": 1, "b": 2.5, "c": "str", "d": True, "e": None}, ""
    )
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "e2", (), {"nested": {"x": [1, 2, 3]}}, ""
    )

    json_str = checkpoint_to_json(e1)
    restored = engine_from_json(json_str)

    # Verificar JSON es válido y usa allow_nan=False (implícito en checkpoint_to_json)
    import json as _json
    parsed = _json.loads(json_str)
    assert parsed["_checkpoint_version"] == CHECKPOINT_VERSION

    for ev1, ev2 in zip(e1.chronicle.events, restored.chronicle.events):
        assert ev1.params == ev2.params, (
            f"Params difieren en evento {ev1.id}"
        )


# ---------------------------------------------------------------------------
# RNG: estados inválidos generan errores claros (Corrección 1.2.1, punto 4)
# ---------------------------------------------------------------------------

def test_restore_rejects_invalid_rng_state(
    small_world, races, governments
):
    """Un estado de RNG inválido debe generar un error claro."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    state = checkpoint_to_dict(e1)
    # Corromper el estado: eliminar la clave "state" interna
    del state["rng_state"]["state"]["state"]

    with pytest.raises((KeyError, ValueError, TypeError)):
        engine_from_checkpoint(state)


def test_restore_pcg64_valid_state_exact(
    small_world, races, governments
):
    """Un estado PCG64 válido debe restaurarse exactamente."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(100):
        e1.tick()

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    # Ambos motores deben producir la misma secuencia
    v1 = float(e1.rng.random())
    v2 = float(restored.rng.random())
    assert v1 == v2

    v1 = float(e1.rng.random())
    v2 = float(restored.rng.random())
    assert v1 == v2


# ---------------------------------------------------------------------------
# Colisión de diccionarios de usuario con marcador de ndarray (Corrección 1.2.2)
# ---------------------------------------------------------------------------

def test_user_dict_matching_ndarray_structure_preserved(
    small_world, races, governments
):
    """Un diccionario de usuario idéntico al marcador de ndarray se conserva
    como diccionario, no se convierte en array."""
    import numpy as np
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    # Diccionario que coincide exactamente con la estructura de array codificado
    user_dict = {
        "__checkpoint_array__": 1,
        "dtype": "float64",
        "shape": [1],
        "data": [7],
    }
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test collision dict", (), {"meta": user_dict}, ""
    )

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    restored_meta = last_ev.params["meta"]
    assert isinstance(restored_meta, dict), (
        f"Se esperaba un diccionario, se obtuvo {type(restored_meta)}"
    )
    assert restored_meta == user_dict
    assert restored_meta["__checkpoint_array__"] == 1
    assert restored_meta["dtype"] == "float64"
    assert restored_meta["shape"] == [1]
    assert restored_meta["data"] == [7]


def test_user_dict_with_extra_keys_preserved(
    small_world, races, governments
):
    """Un diccionario con el marcador de ndarray y claves adicionales se
    conserva como diccionario."""
    import numpy as np
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    user_dict = {
        "__checkpoint_array__": 1,
        "dtype": "float64",
        "shape": [2, 2],
        "data": [[1, 2], [3, 4]],
        "custom_key": "custom_value",
        "extra": 99,
    }
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test extra keys", (), {"config": user_dict}, ""
    )

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    restored_config = last_ev.params["config"]
    assert isinstance(restored_config, dict)
    assert restored_config == user_dict
    assert restored_config["custom_key"] == "custom_value"
    assert restored_config["extra"] == 99


def test_user_dict_in_nested_list_preserved(
    small_world, races, governments
):
    """Un diccionario que coincida con la estructura de ndarray dentro de una
    lista anidada se conserva como diccionario."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    user_dict = {
        "__checkpoint_array__": 1,
        "dtype": "int32",
        "shape": [3],
        "data": [10, 20, 30],
    }
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test nested list", (), {"items": [user_dict, "texto", 5]}, ""
    )

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    items = last_ev.params["items"]
    assert isinstance(items, list)
    assert isinstance(items[0], dict), (
        f"items[0] debería ser un dict, es {type(items[0])}"
    )
    assert items[0] == user_dict
    assert items[1] == "texto"
    assert items[2] == 5


def test_authentic_ndarray_still_restored(
    small_world, races, governments
):
    """Un array NumPy auténtico sigue restaurándose como ndarray."""
    import numpy as np
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    arr = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float64)
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test authentic array", (), {"matrix": arr}, ""
    )

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    restored_arr = last_ev.params["matrix"]
    assert isinstance(restored_arr, np.ndarray), (
        f"Se esperaba ndarray, se obtuvo {type(restored_arr)}"
    )
    assert restored_arr.dtype == arr.dtype
    assert restored_arr.shape == arr.shape
    assert (restored_arr == arr).all()


def test_dict_containing_authentic_ndarray_roundtrip(
    small_world, races, governments
):
    """Un diccionario que contiene un array NumPy auténtico se conserva
    correctamente, con el array restaurado como ndarray."""
    import numpy as np
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    arr = np.array([10, 20, 30], dtype=np.int32)
    user_dict = {
        "__checkpoint_array__": 1,
        "dtype": "int32",
        "shape": [3],
        "data": [10, 20, 30],
        "matrix": arr,
    }
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test dict with array", (), {"config": user_dict}, ""
    )

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    restored_config = last_ev.params["config"]
    assert isinstance(restored_config, dict)
    assert restored_config["__checkpoint_array__"] == 1
    assert restored_config["dtype"] == "int32"
    assert restored_config["shape"] == [3]
    assert restored_config["data"] == [10, 20, 30]
    restored_matrix = restored_config["matrix"]
    assert isinstance(restored_matrix, np.ndarray)
    assert restored_matrix.dtype == arr.dtype
    assert restored_matrix.shape == arr.shape
    assert (restored_matrix == arr).all()


def test_collision_dict_in_list_with_authentic_array_roundtrip(
    small_world, races, governments
):
    """Lista anidada que contiene tanto un diccionario que coincide con la
    estructura de ndarray como un array auténtico."""
    import numpy as np
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    user_dict = {
        "__checkpoint_array__": 1,
        "dtype": "float64",
        "shape": [1],
        "data": [42],
    }
    authentic_arr = np.array([1, 2, 3], dtype=np.int64)

    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test mixed list", (), {"items": [user_dict, authentic_arr]}, ""
    )

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    items = last_ev.params["items"]
    assert isinstance(items, list)
    assert isinstance(items[0], dict)
    assert items[0] == user_dict
    assert isinstance(items[1], np.ndarray)
    assert (items[1] == authentic_arr).all()


def test_deterministic_continuation_unchanged(
    small_world, races, governments
):
    """La continuación determinista del motor no cambia tras la corrección
    de colisión de diccionarios."""
    e1 = SimEngine(small_world, races, governments, seed=11)

    for _ in range(100):
        e1.tick()

    state = checkpoint_to_dict(e1)
    e2 = engine_from_checkpoint(state)

    for _ in range(80):
        e1.tick()
        e2.tick()

    assert e1.month == e2.month == 180
    assert len(e1.kingdoms) == len(e2.kingdoms)
    assert len(e1.chronicle.events) == len(e2.chronicle.events)
    for ev1, ev2 in zip(e1.chronicle.events, e2.chronicle.events):
        assert ev1.params == ev2.params, (
            f"Params difieren en evento {ev1.id}"
        )


# ---------------------------------------------------------------------------
# Formato tipado inequívoco para diccionarios de usuario (Corrección 1.2.3)
# ---------------------------------------------------------------------------

def test_user_dict_with_old_wrapped_marker_preserved(
    small_world, races, governments
):
    """Un diccionario que contiene exactamente la clave del marcador antiguo
    ``__checkpoint_wrapped_user_dict__`` se preserva como diccionario."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    user_dict = {"__checkpoint_wrapped_user_dict__": 1}
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test old marker", (), {"config": user_dict}, ""
    )

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    restored_config = last_ev.params["config"]
    assert isinstance(restored_config, dict)
    assert restored_config == user_dict


def test_user_dict_with_new_typed_marker_preserved(
    small_world, races, governments
):
    """Un diccionario que contiene exactamente la clave del nuevo marcador
    ``__checkpoint_kind__`` se preserva como diccionario."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    user_dict = {"__checkpoint_kind__": "dict"}
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test new marker", (), {"config": user_dict}, ""
    )

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    restored_config = last_ev.params["config"]
    assert isinstance(restored_config, dict)
    assert restored_config == user_dict


def test_user_dict_with_both_markers_as_data(
    small_world, races, governments
):
    """Un diccionario que contiene simultáneamente el marcador antiguo
    ``__checkpoint_wrapped_user_dict__`` y el nuevo ``__checkpoint_kind__``
    como datos legítimos se preserva completo."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    user_dict = {
        "__checkpoint_wrapped_user_dict__": 1,
        "__checkpoint_kind__": "dict",
        "custom": "valor",
    }
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test both markers", (), {"config": user_dict}, ""
    )

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    restored_config = last_ev.params["config"]
    assert isinstance(restored_config, dict)
    assert restored_config == user_dict
    assert restored_config["__checkpoint_wrapped_user_dict__"] == 1
    assert restored_config["__checkpoint_kind__"] == "dict"
    assert restored_config["custom"] == "valor"


def test_nested_dict_with_collision_keys_preserved(
    small_world, races, governments
):
    """Diccionarios anidados que contienen claves de colisión se preservan
    en todos los niveles."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    nested = {
        "__checkpoint_wrapped_user_dict__": 1,
        "inner": {
            "__checkpoint_kind__": "dict",
            "deep": {"__checkpoint_array__": 1},
        },
    }
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test nested collision", (), {"config": nested}, ""
    )

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    restored_config = last_ev.params["config"]
    assert isinstance(restored_config, dict)
    assert restored_config == nested
    assert restored_config["__checkpoint_wrapped_user_dict__"] == 1
    assert restored_config["inner"]["__checkpoint_kind__"] == "dict"
    assert restored_config["inner"]["deep"]["__checkpoint_array__"] == 1


def test_dict_with_ndarray_key_in_typed_format(
    small_world, races, governments
):
    """Un diccionario de usuario que contiene una clave llamada
    ``__checkpoint_array__`` no se confunde con un array NumPy."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    user_dict = {
        "__checkpoint_array__": "esto es un string, no un array",
        "valor": 42,
    }
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test ndarray key", (), {"config": user_dict}, ""
    )

    state = checkpoint_to_dict(e1)
    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    restored_config = last_ev.params["config"]
    assert isinstance(restored_config, dict)
    assert restored_config == user_dict
    assert restored_config["__checkpoint_array__"] == "esto es un string, no un array"


def test_v1_checkpoint_dict_plain_still_works(
    small_world, races, governments
):
    """Un diccionario plano (formato v1, sin __checkpoint_kind__) se
    restaurará correctamente para compatibilidad."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    # Serializar en formato v1: dict plano sin __checkpoint_kind__
    v1_params = {"nombre": "config", "valor": 42, "lista": [1, 2, 3]}
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test v1 compat", (), v1_params, ""
    )

    state = checkpoint_to_dict(e1)
    # Verificar que el estado serializado preserva los valores
    json_str = checkpoint_to_dict(e1)
    events = json_str["chronicle"]["events"]
    target = next(
        (e for e in events if e.get("text") == "test v1 compat"), None
    )
    assert target is not None, "Evento 'test v1 compat' no encontrado"
    params = target["params"]
    # Los valores se preservan directamente (no hay envoltura en el dict
    # completo de params, solo en los valores que son dicts anidados)
    assert params["nombre"] == "config"
    assert params["valor"] == 42
    assert params["lista"] == [1, 2, 3]

    # Restaurar y verificar que los datos se recuperan
    restored = engine_from_checkpoint(state)
    last_ev = restored.chronicle.events[-1]
    assert last_ev.params["nombre"] == "config"
    assert last_ev.params["valor"] == 42
    assert last_ev.params["lista"] == [1, 2, 3]


def test_full_roundtrip_with_collision_keys(
    small_world, races, governments
):
    """Roundtrip completo mediante checkpoint_to_json y restauración,
    preservando diccionarios con claves de colisión."""
    import json as _json

    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(100):
        e1.tick()

    collision_params = {
        "__checkpoint_wrapped_user_dict__": 1,
        "__checkpoint_kind__": "dict",
        "matriz": [[1, 2], [3, 4]],
        "nombre": "test",
    }
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test full roundtrip", (), collision_params, ""
    )

    json_str = checkpoint_to_json(e1)
    # Verificar que es JSON válido
    parsed = _json.loads(json_str)
    assert parsed["_checkpoint_version"] == CHECKPOINT_VERSION

    restored = engine_from_json(json_str)

    last_ev = restored.chronicle.events[-1]
    assert last_ev.params == collision_params
    assert last_ev.params["__checkpoint_wrapped_user_dict__"] == 1
    assert last_ev.params["__checkpoint_kind__"] == "dict"
    assert last_ev.params["matriz"] == [[1, 2], [3, 4]]
    assert last_ev.params["nombre"] == "test"


# ---------------------------------------------------------------------------
# Versionado externo de formatos de parámetros (Corrección 1.2.4)
# ---------------------------------------------------------------------------

def _make_v1_checkpoint_state(engine: SimEngine) -> dict[str, Any]:
    """Crea un estado de checkpoint con versión 1 (formato antiguo)."""
    state = checkpoint_to_dict(engine, checkpoint_version=1)
    state["_checkpoint_version"] = 1
    return state


def test_v1_checkpoint_with_checkpoint_kind_as_data(
    small_world, races, governments
):
    """Un checkpoint v1 con un diccionario que contiene
    ``__checkpoint_kind__: "dict"`` como dato legítimo se restaura íntegro."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    user_params = {
        "__checkpoint_kind__": "dict",
        "items": "esto es un string",
        "nested": {"__checkpoint_kind__": "dict"},
    }
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test v1 with kind key", (), user_params, ""
    )

    state = _make_v1_checkpoint_state(e1)
    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    assert last_ev.params == user_params
    assert last_ev.params["__checkpoint_kind__"] == "dict"
    assert last_ev.params["items"] == "esto es un string"
    assert last_ev.params["nested"]["__checkpoint_kind__"] == "dict"


def test_v1_checkpoint_with_wrapped_user_dict_key(
    small_world, races, governments
):
    """Un checkpoint v1 con __checkpoint_wrapped_user_dict__ se restaura
    como dato, no como marcador de envoltorio."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    user_params = {
        "__checkpoint_wrapped_user_dict__": 1,
        "otra_clave": "valor",
    }
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test v1 with wrapped key", (), user_params, ""
    )

    state = _make_v1_checkpoint_state(e1)
    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    assert last_ev.params == user_params
    assert last_ev.params["__checkpoint_wrapped_user_dict__"] == 1


def test_v1_checkpoint_nested_collision_keys(
    small_world, races, governments
):
    """Checkpoint v1 con diccionarios anidados que contienen claves de
    colisión: se preservan todos los niveles."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    nested = {
        "__checkpoint_kind__": "dict",
        "inner": {
            "__checkpoint_wrapped_user_dict__": 1,
            "deep": ["a", "b"],
        },
    }
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test v1 nested collision", (), nested, ""
    )

    state = _make_v1_checkpoint_state(e1)
    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    assert last_ev.params == nested


def test_v2_checkpoint_with_typed_dicts_roundtrip(
    small_world, races, governments
):
    """Un checkpoint v2 con diccionarios tipados se restaura correctamente."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(100):
        e1.tick()

    typed_params = {
        "config": {"__checkpoint_kind__": "dict", "valor": 42},
        "lista_de_dicts": [
            {"__checkpoint_kind__": "dict", "x": 1},
            {"__checkpoint_kind__": "dict", "y": 2},
        ],
    }
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test v2 typed dicts", (), typed_params, ""
    )

    state = checkpoint_to_dict(e1)
    assert state["_checkpoint_version"] == 2

    restored = engine_from_checkpoint(state)

    last_ev = restored.chronicle.events[-1]
    assert last_ev.params == typed_params


def test_v1_decoder_does_not_interpret_v2_markers(
    small_world, races, governments
):
    """El decoder v1 (checkpoint_version=1) no interpreta
    __checkpoint_kind__ como marcador interno."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test v1 decoder", (), {"__checkpoint_kind__": "dict"}, ""
    )
    state = _make_v1_checkpoint_state(e1)

    from worldsim.export.checkpoint import _decode_params
    params_v1 = _decode_params(state["chronicle"]["events"][-1]["params"], 1)

    assert params_v1["__checkpoint_kind__"] == "dict"


def test_v2_decoder_reconstructs_typed_dicts(
    small_world, races, governments
):
    """El decoder v2 reconstruye correctamente diccionarios tipados."""
    e1 = SimEngine(small_world, races, governments, seed=7)
    for _ in range(50):
        e1.tick()

    typed_params = {
        "user_data": {"__checkpoint_kind__": "dict", "custom": "data"},
    }
    e1.chronicle.log(
        e1.month, EventType.KINGDOM_FOUNDED,
        "test v2 decoder", (), typed_params, ""
    )

    state = checkpoint_to_dict(e1)
    from worldsim.export.checkpoint import _decode_params
    params_v2 = _decode_params(state["chronicle"]["events"][-1]["params"], 2)

    assert params_v2 == typed_params


def test_v2_rejects_unknown_version():
    """Un checkpoint con versión desconocida se rechaza con error claro."""
    from worldsim.export.checkpoint import engine_from_checkpoint
    state = {
        "_checkpoint_version": 3,
        "_checkpoint_type": "simengine",
        "month": 0,
        "seed": 42,
        "world_config": {
            "width": 100, "height": 50, "seed": 42,
            "big_hills": 250, "small_hills": 1000, "rivers": 12,
            "margin": 0.1, "sea_level": 0.2,
        },
        "world_maps": {},
        "rng_state": {},
        "races": [],
        "governments": [],
        "kingdoms": [],
        "leaders": [],
        "heroes": [],
        "notables": [],
        "wars": [],
        "beasts": [],
        "relations": {},
        "chronicle": {"_next_event_id": 1, "events": [], "_by_kingdom": {}},
        "fallen": [],
        "_next_war_id": 1, "_next_leader_id": 1,
        "_next_hero_id": 1, "_next_beast_id": 1,
        "_next_notable_id": 1,
        "_milestones": [], "_world_techs": [],
        "_color_counter": 0,
        "_occupancy": {"__checkpoint_array__": 1, "dtype": "int32", "shape": [1, 1], "data": [[0]]},
        "_battle_heat": {"__checkpoint_array__": 1, "dtype": "float32", "shape": [1, 1], "data": [[0.0]]},
    }
    try:
        engine_from_checkpoint(state)
    except ValueError as exc:
        assert "incompatible" in str(exc).lower() or "no soportada" in str(exc).lower()
    else:
        raise AssertionError("Se esperaba ValueError por versión desconocida")


def test_v2_deterministic_continuation_after_restore(
    small_world, races, governments
):
    """Continuación determinista tras restaurar un checkpoint v2."""
    e1 = SimEngine(small_world, races, governments, seed=11)
    for _ in range(100):
        e1.tick()

    state = checkpoint_to_dict(e1)
    assert state["_checkpoint_version"] == 2
    e2 = engine_from_checkpoint(state)

    for _ in range(80):
        e1.tick()
        e2.tick()

    assert e1.month == e2.month == 180
    assert len(e1.kingdoms) == len(e2.kingdoms)
    assert len(e1.chronicle.events) == len(e2.chronicle.events)
    for ev1, ev2 in zip(e1.chronicle.events, e2.chronicle.events):
        assert ev1.params == ev2.params, (
            f"Params difieren en evento {ev1.id}"
        )