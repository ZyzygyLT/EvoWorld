"""Serialización completa y versionada de SimEngine para recuperación de checkpoints.

Formato: JSON (estándar de Python, sin dependencias externas) con codificación
explícita para arrays NumPy, generadores aleatorios, sets, tuples, enums y
dataclasses. engine_to_dict se conserva inalterado para la vista del cliente;
este módulo es exclusivamente para restauración exacta del motor.

Estructura del documento:
    {
        "_checkpoint_version": 1,
        "_checkpoint_type": "simengine",
        "month": int,
        "seed": int,
        "world_config": {...},
        "world_maps": {name: ndarray_encoded},
        "rng_state": {...},
        "races": [...],
        "governments": [...],
        "kingdoms": [...],
        "wars": [...],
        "beasts": [...],
        "relations": {"0|1": "PEACE", ...},
        "chronicle": {"_next_event_id": int, "events": [...], "_by_kingdom": {...}},
        "fallen": [...],
        "_next_war_id": int, "_next_leader_id": int, "_next_hero_id": int,
        "_next_beast_id": int, "_next_notable_id": int,
        "_milestones": [...], "_world_techs": [...], "_color_counter": int,
        "_occupancy": ndarray_encoded, "_battle_heat": ndarray_encoded
    }
"""

from __future__ import annotations

import json
import math
from enum import Enum
from typing import Any

import numpy as np

from worldsim.sim.engine import SimEngine
from worldsim.sim.models import (
    Beast,
    Chronicle,
    Event,
    EventType,
    GovernmentType,
    Hero,
    Kingdom,
    Leader,
    Notable,
    Race,
    Relation,
    Site,
    TRIBAL_GOVERNMENT,
    War,
)
from worldsim.worldgen import World, WorldConfig

CHECKPOINT_VERSION = 2


# ---------------------------------------------------------------------------
# Codificadores / decodificadores de bajo nivel
# ---------------------------------------------------------------------------

def _encode_ndarray(arr: np.ndarray) -> dict[str, Any]:
    """Codifica un array NumPy como diccionario JSON-serializable.

    Usa el marcador reservado ``__checkpoint_array__`` en lugar de
    ``__type__`` para evitar colisiones con claves legítimas de diccionarios
    de parámetros de eventos.
    """
    return {
        "__checkpoint_array__": 1,
        "dtype": str(arr.dtype),
        "shape": list(arr.shape),
        "data": arr.tolist(),
    }


def _is_encoded_ndarray(d: Any) -> bool:
    """Devuelve True si ``d`` es un diccionario que representa un array
    NumPy codificado con el marcador reservado."""
    return (
        isinstance(d, dict)
        and d.get("__checkpoint_array__") == 1
        and "dtype" in d
        and "shape" in d
        and "data" in d
    )


def _decode_ndarray(d: dict[str, Any]) -> np.ndarray:
    """Reconstruye un array NumPy desde su codificación."""
    if not _is_encoded_ndarray(d):
        raise ValueError(
            "Formato de array NumPy codificado inválido: "
            "se esperaba el marcador '__checkpoint_array__'"
        )
    return np.array(d["data"], dtype=np.dtype(d["dtype"])).reshape(d["shape"])


def _encode_rng_state(rng: np.random.Generator) -> dict[str, Any]:
    """Codifica el estado del generador aleatorio NumPy."""
    bg = rng.bit_generator
    raw = bg.state
    bit_gen_name = raw["bit_generator"]
    state_inner = raw["state"]

    # state_inner es un dict como {"state": ndarray, "inc": int} para PCG64
    encoded_inner: dict[str, Any] = {}
    for key, val in state_inner.items():
        if isinstance(val, np.ndarray):
            encoded_inner[key] = _encode_ndarray(val)
        else:
            encoded_inner[key] = val

    return {
        "bit_generator": bit_gen_name,
        "state": encoded_inner,
        "has_uint32": int(raw["has_uint32"]),
        "uinteger": int(raw["uinteger"]),
    }


# Tipos de BitGenerator compatibles para reconstrucción del RNG.
_COMPATIBLE_BIT_GENERATORS = {"PCG64"}


def _decode_rng_state(d: dict[str, Any]) -> np.random.Generator:
    """Reconstruye un generador aleatorio desde su estado codificado.

    Valida el tipo de BitGenerator antes de restaurar el estado, para evitar
    instalar estados de generadores desconocidos o incompatibles.
    """
    bit_gen_name = d["bit_generator"]
    if bit_gen_name not in _COMPATIBLE_BIT_GENERATORS:
        raise ValueError(
            f"BitGenerator no compatible en checkpoint: {bit_gen_name!r}. "
            f"Tipos soportados: {sorted(_COMPATIBLE_BIT_GENERATORS)}"
        )

    rng = np.random.default_rng(0)  # semilla dummy; se sobreescribe el estado
    inner: dict[str, Any] = {}
    for key, val in d["state"].items():
        if key == "state" and _is_encoded_ndarray(val):
            inner[key] = _decode_ndarray(val)
        else:
            inner[key] = val
    state = {
        "bit_generator": bit_gen_name,
        "state": inner,
        "has_uint32": d["has_uint32"],
        "uinteger": d["uinteger"],
    }
    rng.bit_generator.state = state
    return rng


# ---------------------------------------------------------------------------
# Serialización por dominio
# ---------------------------------------------------------------------------

def _serialize_world_config(cfg: WorldConfig) -> dict[str, Any]:
    return {
        "width": cfg.width,
        "height": cfg.height,
        "seed": cfg.seed,
        "big_hills": cfg.big_hills,
        "small_hills": cfg.small_hills,
        "rivers": cfg.rivers,
        "margin": cfg.margin,
        "sea_level": cfg.sea_level,
    }


def _deserialize_world_config(d: dict[str, Any]) -> WorldConfig:
    return WorldConfig(
        width=d["width"],
        height=d["height"],
        seed=d["seed"],
        big_hills=d.get("big_hills", 250),
        small_hills=d.get("small_hills", 1000),
        rivers=d.get("rivers", 12),
        margin=d.get("margin", 0.1),
        sea_level=d.get("sea_level", 0.2),
    )


def _serialize_race(r: Race) -> dict[str, Any]:
    return {
        "name": r.name,
        "preferred_biomes": list(r.preferred_biomes),
        "strength": r.strength,
        "size": r.size,
        "reproduction": r.reproduction,
        "aggressiveness": r.aggressiveness,
        "form": r.form,
    }


def _deserialize_race(d: dict[str, Any]) -> Race:
    return Race(
        name=d["name"],
        preferred_biomes=tuple(d["preferred_biomes"]),
        strength=d["strength"],
        size=d["size"],
        reproduction=d["reproduction"],
        aggressiveness=d["aggressiveness"],
        form=d["form"],
    )


def _serialize_government(g: GovernmentType) -> dict[str, Any]:
    return {
        "name": g.name,
        "description": g.description,
        "aggressiveness": g.aggressiveness,
        "militarization": g.militarization,
        "tech_bonus": g.tech_bonus,
    }


def _deserialize_government(d: dict[str, Any]) -> GovernmentType:
    return GovernmentType(
        name=d["name"],
        description=d["description"],
        aggressiveness=d["aggressiveness"],
        militarization=d["militarization"],
        tech_bonus=d["tech_bonus"],
    )


def _serialize_site(s: Site) -> dict[str, Any]:
    return {
        "x": s.x,
        "y": s.y,
        "name": s.name,
        "category": s.category,
        "population": float(s.population),
        "popcap": float(s.popcap),
        "is_capital": bool(s.is_capital),
        "traits": {k: float(v) for k, v in s.traits.items()},
    }


def _deserialize_site(d: dict[str, Any]) -> Site:
    return Site(
        x=d["x"],
        y=d["y"],
        name=d["name"],
        category=d["category"],
        population=float(d["population"]),
        popcap=float(d["popcap"]),
        is_capital=bool(d["is_capital"]),
        traits={k: float(v) for k, v in d.get("traits", {}).items()},
    )


def _serialize_leader(l: Leader) -> dict[str, Any]:
    return {
        "id": l.id,
        "name": l.name,
        "kingdom_id": l.kingdom_id,
        "title": l.title,
        "born_month": l.born_month,
        "died_month": l.died_month,
        "expected_death_month": l.expected_death_month,
        "battles_won": l.battles_won,
        "battles_lost": l.battles_lost,
    }


def _deserialize_leader(d: dict[str, Any]) -> Leader:
    return Leader(
        id=d["id"],
        name=d["name"],
        kingdom_id=d["kingdom_id"],
        title=d["title"],
        born_month=d["born_month"],
        died_month=d["died_month"],
        expected_death_month=d["expected_death_month"],
        battles_won=d["battles_won"],
        battles_lost=d["battles_lost"],
    )


def _serialize_hero(h: Hero) -> dict[str, Any]:
    return {
        "id": h.id,
        "name": h.name,
        "kingdom_id": h.kingdom_id,
        "strength": float(h.strength),
        "battle_deeds": list(h.battle_deeds),
    }


def _deserialize_hero(d: dict[str, Any]) -> Hero:
    return Hero(
        id=d["id"],
        name=d["name"],
        kingdom_id=d["kingdom_id"],
        strength=float(d["strength"]),
        battle_deeds=list(d.get("battle_deeds", [])),
    )


def _serialize_notable(n: Notable) -> dict[str, Any]:
    return {
        "id": n.id,
        "name": n.name,
        "kingdom_id": n.kingdom_id,
        "role": n.role,
        "ambition": float(n.ambition),
        "piety": float(n.piety),
        "greed": float(n.greed),
        "born_month": n.born_month,
        "died_month": n.died_month,
    }


def _deserialize_notable(d: dict[str, Any]) -> Notable:
    return Notable(
        id=d["id"],
        name=d["name"],
        kingdom_id=d["kingdom_id"],
        role=d["role"],
        ambition=float(d["ambition"]),
        piety=float(d["piety"]),
        greed=float(d["greed"]),
        born_month=d["born_month"],
        died_month=d["died_month"],
    )


def _serialize_kingdom(k: Kingdom) -> dict[str, Any]:
    return {
        "id": k.id,
        "name": k.name,
        "race": k.race.name,
        "government": k.government.name,
        "color": list(k.color),
        "sites": [_serialize_site(s) for s in k.sites],
        "total_population": float(k.total_population),
        "at_war": bool(k.at_war),
        "army_size": float(k.army_size),
        "army_pos": list(k.army_pos) if k.army_pos is not None else None,
        "war_exhaustion": int(k.war_exhaustion),
        "years_since_founding": int(k.years_since_founding),
        "leader_id": k.leader.id if k.leader is not None else None,
        "hero_id": k.hero.id if k.hero is not None else None,
        "notable_ids": [n.id for n in k.notables],
        "founded_month": int(k.founded_month),
        "population_history": [float(p) for p in k.population_history],
        "last_gov_shift_month": int(k.last_gov_shift_month),
        "tech_level": int(k.tech_level),
        "tech_progress": float(k.tech_progress),
        "techs": sorted(k.techs),
        "mods": {ak: float(av) for ak, av in k.mods.items()},
        "culture": {ak: float(av) for ak, av in k.culture.items()},
        "civilized": bool(k.civilized),
        "golden_age_until": int(k.golden_age_until),
        "drought_until": int(k.drought_until),
        "happiness": float(k.happiness),
        "faith": k.faith,
        "temples": sorted(k.temples),
        "trade_routes": sorted(k.trade_routes),
        "trader_kindred": {str(pid): int(m) for pid, m in k.trader_kindred.items()},
        "_last_disaster_month": int(k._last_disaster_month),
        "_last_plague_month": int(k._last_plague_month),
        "wars_won": int(k.wars_won),
        "wars_lost": int(k.wars_lost),
        "collapsed": bool(k.collapsed),
    }


def _deserialize_kingdom(
    d: dict[str, Any],
    races_by_name: dict[str, Race],
    governments_by_name: dict[str, GovernmentType],
    leaders_by_id: dict[int, Leader],
    heroes_by_id: dict[int, Hero],
    notables_by_id: dict[int, Notable],
) -> Kingdom:
    sites = [_deserialize_site(sd) for sd in d["sites"]]
    race = races_by_name[d["race"]]
    government = governments_by_name[d["government"]]
    color = tuple(d["color"])

    k = Kingdom(
        id=d["id"],
        name=d["name"],
        race=race,
        government=government,
        color=color,
        sites=sites,
        total_population=float(d["total_population"]),
        at_war=bool(d["at_war"]),
        army_size=float(d["army_size"]),
        army_pos=tuple(d["army_pos"]) if d["army_pos"] is not None else None,
        war_exhaustion=int(d["war_exhaustion"]),
        years_since_founding=int(d["years_since_founding"]),
        leader=None,  # se asigna después
        hero=None,  # se asigna después
        notables=[],  # se asigna después
        founded_month=int(d["founded_month"]),
        population_history=[float(p) for p in d.get("population_history", [])],
        last_gov_shift_month=int(d["last_gov_shift_month"]),
        tech_level=int(d["tech_level"]),
        tech_progress=float(d["tech_progress"]),
        techs=set(d.get("techs", [])),
        mods={ak: float(av) for ak, av in d.get("mods", {}).items()},
        culture={ak: float(av) for ak, av in d.get("culture", {}).items()},
        civilized=bool(d["civilized"]),
        golden_age_until=int(d["golden_age_until"]),
        drought_until=int(d["drought_until"]),
        happiness=float(d["happiness"]),
        faith=d["faith"],
        temples=set(d.get("temples", [])),
        trade_routes=set(d.get("trade_routes", [])),
        trader_kindred={int(pid): int(m) for pid, m in d.get("trader_kindred", {}).items()},
        _last_disaster_month=int(d.get("_last_disaster_month", -9999)),
        _last_plague_month=int(d.get("_last_plague_month", -9999)),
        wars_won=int(d["wars_won"]),
        wars_lost=int(d["wars_lost"]),
        collapsed=bool(d["collapsed"]),
    )

    # Re-enlazar referencias
    if d.get("leader_id") is not None:
        k.leader = leaders_by_id[d["leader_id"]]
    if d.get("hero_id") is not None:
        k.hero = heroes_by_id[d["hero_id"]]
    k.notables = [notables_by_id[nid] for nid in d.get("notable_ids", [])]

    return k


def _serialize_war(w: War) -> dict[str, Any]:
    return {
        "id": w.id,
        "side_a_id": w.side_a.id,
        "side_b_id": w.side_b.id,
        "start_month": int(w.start_month),
        "name": w.name,
        "cause": w.cause,
        "end_month": w.end_month,
        "victor_id": w.victor.id if w.victor is not None else None,
        "battles": int(w.battles),
        "total_dead": int(w.total_dead),
        "battle_sites": [list(bs) for bs in w.battle_sites],
        "cobelligerents_a": [c.id for c in w.cobelligerents_a],
        "cobelligerents_b": [c.id for c in w.cobelligerents_b],
    }


def _deserialize_war(
    d: dict[str, Any],
    kingdoms_by_id: dict[int, Kingdom],
) -> War:
    side_a = kingdoms_by_id[d["side_a_id"]]
    side_b = kingdoms_by_id[d["side_b_id"]]
    victor = kingdoms_by_id[d["victor_id"]] if d.get("victor_id") is not None else None

    w = War(
        id=d["id"],
        side_a=side_a,
        side_b=side_b,
        start_month=int(d["start_month"]),
        name=d["name"],
        cause=d["cause"],
        end_month=d["end_month"],
        victor=victor,
        battles=int(d["battles"]),
        total_dead=int(d["total_dead"]),
        battle_sites=[tuple(bs) for bs in d.get("battle_sites", [])],
        cobelligerents_a=[kingdoms_by_id[cid] for cid in d.get("cobelligerents_a", [])],
        cobelligerents_b=[kingdoms_by_id[cid] for cid in d.get("cobelligerents_b", [])],
    )
    return w


def _serialize_beast(b: Beast) -> dict[str, Any]:
    return {
        "id": b.id,
        "name": b.name,
        "race": b.race.name,
        "x": b.x,
        "y": b.y,
        "hp": int(b.hp),
        "power": int(b.power),
        "aggression": int(b.aggression),
    }


def _deserialize_beast(d: dict[str, Any], races_by_name: dict[str, Race]) -> Beast:
    return Beast(
        id=d["id"],
        name=d["name"],
        race=races_by_name[d["race"]],
        x=d["x"],
        y=d["y"],
        hp=int(d["hp"]),
        power=int(d["power"]),
        aggression=int(d["aggression"]),
    )


def _encode_params(params: dict) -> dict[str, Any]:
    """Codifica los parámetros de un evento para serialización JSON segura.

    Convierte tipos NumPy y otros valores no primitivos a sus equivalentes
    Python puros. Rechaza floats no finitos (NaN, Infinity, -Infinity).
    Lanza ValueError si encuentra un tipo que no puede
    serializarse de forma segura.

    Cada diccionario de usuario se representa de manera inequívoca mediante
    un envoltorio tipado con clave ``__checkpoint_kind__`` igual a ``"dict"``
    y una lista de pares ``[clave, valor]`` en ``items``. Esto garantiza que
    ningún diccionario de usuario pueda confundirse con un array NumPy
    codificado ni con ninguna otra estructura interna.
    """
    encoded: dict[str, Any] = {}
    for key, val in params.items():
        if isinstance(val, np.ndarray):
            encoded[str(key)] = _encode_ndarray(val)
        elif isinstance(val, np.integer):
            encoded[str(key)] = int(val)
        elif isinstance(val, np.floating):
            fval = float(val)
            if not math.isfinite(fval):
                raise ValueError(
                    f"Valor float no finito en parámetro {key!r}: {fval!r}"
                )
            encoded[str(key)] = fval
        elif isinstance(val, np.bool_):
            encoded[str(key)] = bool(val)
        elif isinstance(val, bool):
            encoded[str(key)] = val
        elif isinstance(val, int):
            encoded[str(key)] = val
        elif isinstance(val, float):
            if not math.isfinite(val):
                raise ValueError(
                    f"Valor float no finito en parámetro {key!r}: {val!r}"
                )
            encoded[str(key)] = val
        elif isinstance(val, str):
            encoded[str(key)] = val
        elif val is None:
            encoded[str(key)] = None
        elif isinstance(val, list):
            encoded[str(key)] = [_encode_params_item(v) for v in val]
        elif isinstance(val, dict):
            encoded[str(key)] = {
                "__checkpoint_kind__": "dict",
                "items": [
                    [str(sk), _encode_params_item(sv)] for sk, sv in val.items()
                ],
            }
        else:
            raise ValueError(
                f"Tipo de parámetro no serializable en evento: "
                f"{type(val).__name__} para clave {key!r}"
            )
    return encoded


def _encode_params_item(val: Any) -> Any:
    """Codifica un ítem individual dentro de una lista de parámetros."""
    if isinstance(val, np.ndarray):
        return _encode_ndarray(val)
    if isinstance(val, np.integer):
        return int(val)
    if isinstance(val, np.floating):
        fval = float(val)
        if not math.isfinite(fval):
            raise ValueError(f"Valor float no finito en ítem de lista: {fval!r}")
        return fval
    if isinstance(val, np.bool_):
        return bool(val)
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        if not math.isfinite(val):
            raise ValueError(f"Valor float no finito en ítem de lista: {val!r}")
        return val
    if isinstance(val, str):
        return val
    if val is None:
        return None
    if isinstance(val, list):
        return [_encode_params_item(v) for v in val]
    if isinstance(val, dict):
        return {
            "__checkpoint_kind__": "dict",
            "items": [
                [str(sk), _encode_params_item(sv)] for sk, sv in val.items()
            ],
        }
    raise ValueError(f"Tipo de parámetro no serializable: {type(val).__name__}")


def _encode_params_v1(params: dict) -> dict[str, Any]:
    """Codifica parámetros en formato v1: diccionarios planos, sin envoltorio tipado.

    Se usa para checkpoints versión 1. Ningún diccionario se envuelve con
    __checkpoint_kind__; todos se recorren recursivamente como dicts planos.
    """
    encoded: dict[str, Any] = {}
    for key, val in params.items():
        if isinstance(val, np.ndarray):
            encoded[str(key)] = _encode_ndarray(val)
        elif isinstance(val, np.integer):
            encoded[str(key)] = int(val)
        elif isinstance(val, np.floating):
            fval = float(val)
            if not math.isfinite(fval):
                raise ValueError(
                    f"Valor float no finito en parámetro {key!r}: {fval!r}"
                )
            encoded[str(key)] = fval
        elif isinstance(val, np.bool_):
            encoded[str(key)] = bool(val)
        elif isinstance(val, bool):
            encoded[str(key)] = val
        elif isinstance(val, int):
            encoded[str(key)] = val
        elif isinstance(val, float):
            if not math.isfinite(val):
                raise ValueError(
                    f"Valor float no finito en parámetro {key!r}: {val!r}"
                )
            encoded[str(key)] = val
        elif isinstance(val, str):
            encoded[str(key)] = val
        elif val is None:
            encoded[str(key)] = None
        elif isinstance(val, list):
            encoded[str(key)] = [_encode_params_item_v1(v) for v in val]
        elif isinstance(val, dict):
            encoded[str(key)] = _encode_params_v1(val)
        else:
            raise ValueError(
                f"Tipo de parámetro no serializable en evento: "
                f"{type(val).__name__} para clave {key!r}"
            )
    return encoded


def _encode_params_item_v1(val: Any) -> Any:
    """Codifica un ítem individual dentro de una lista (formato v1)."""
    if isinstance(val, np.ndarray):
        return _encode_ndarray(val)
    if isinstance(val, np.integer):
        return int(val)
    if isinstance(val, np.floating):
        fval = float(val)
        if not math.isfinite(fval):
            raise ValueError(f"Valor float no finito en ítem de lista: {fval!r}")
        return fval
    if isinstance(val, np.bool_):
        return bool(val)
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        if not math.isfinite(val):
            raise ValueError(f"Valor float no finito en ítem de lista: {val!r}")
        return val
    if isinstance(val, str):
        return val
    if val is None:
        return None
    if isinstance(val, list):
        return [_encode_params_item_v1(v) for v in val]
    if isinstance(val, dict):
        return _encode_params_v1(val)
    raise ValueError(f"Tipo de parámetro no serializable: {type(val).__name__}")


def _decode_params(params: dict | None, checkpoint_version: int = 2) -> dict:
    """Decodifica los parámetros de un evento restaurados desde JSON.

    Reconstruye arrays NumPy y preserva los tipos primitivos originales.

    El parámetro ``checkpoint_version`` determina cómo interpretar los
    diccionarios anidados:

    * v1 (checkpoints antiguos): todos los diccionarios son planos y se
      recorren recursivamente. No se interpreta ``__checkpoint_kind__`` como
      marcador interno.
    * v2 (checkpoints nuevos): los diccionarios de usuario se representan
      con un envoltorio tipado (clave ``__checkpoint_kind__`` igual a
      ``"dict"`` y lista de pares en ``items``).
    """
    if params is None:
        return {}
    if checkpoint_version == 1:
        return _decode_params_v1(params)
    elif checkpoint_version == 2:
        return _decode_params_v2(params)
    else:
        raise ValueError(
            f"Versión de parámetros no soportada: {checkpoint_version}"
        )


def _decode_params_v1(params: dict | None) -> dict:
    """Decoder para checkpoints versión 1.

    Todos los diccionarios son planos; ningún marcador interno se interpreta.
    """
    if params is None:
        return {}
    decoded: dict = {}
    for key, val in params.items():
        if isinstance(val, dict):
            decoded[key] = _decode_params_v1(val)
        elif isinstance(val, list):
            decoded[key] = [_decode_params_item_v1(v) for v in val]
        else:
            decoded[key] = val
    return decoded


def _decode_params_item_v1(val: Any) -> Any:
    """Decodifica un ítem individual (formato v1)."""
    if isinstance(val, dict):
        return _decode_params_v1(val)
    if isinstance(val, list):
        return [_decode_params_item_v1(v) for v in val]
    return val


def _decode_params_v2(params: dict | None) -> dict:
    """Decoder para checkpoints versión 2.

    Los diccionarios de usuario se representan con un envoltorio tipado
    (clave ``__checkpoint_kind__`` igual a ``"dict"`` y lista de pares en
    ``items``).
    """
    if params is None:
        return {}
    decoded: dict = {}
    for key, val in params.items():
        if isinstance(val, dict) and val.get("__checkpoint_kind__") == "dict":
            items = val.get("items", [])
            decoded[key] = {str(k): _decode_params_item_v2(v) for k, v in items}
        elif isinstance(val, dict) and _is_encoded_ndarray(val):
            decoded[key] = _decode_ndarray(val)
        elif isinstance(val, dict):
            decoded[key] = _decode_params_v2(val)
        elif isinstance(val, list):
            decoded[key] = [_decode_params_item_v2(v) for v in val]
        else:
            decoded[key] = val
    return decoded


def _decode_params_item_v2(val: Any) -> Any:
    """Decodifica un ítem individual (formato v2)."""
    if isinstance(val, dict) and val.get("__checkpoint_kind__") == "dict":
        items = val.get("items", [])
        return {str(k): _decode_params_item_v2(v) for k, v in items}
    if isinstance(val, dict) and _is_encoded_ndarray(val):
        return _decode_ndarray(val)
    if isinstance(val, dict):
        return _decode_params_v2(val)
    if isinstance(val, list):
        return [_decode_params_item_v2(v) for v in val]
    return val


def _serialize_event(e: Event, checkpoint_version: int = 2) -> dict[str, Any]:
    if checkpoint_version == 1:
        params_encoded = _encode_params_v1(e.params)
    elif checkpoint_version == 2:
        params_encoded = _encode_params(e.params)
    else:
        raise ValueError(
            f"Versión de parámetros no soportada: {checkpoint_version}"
        )
    return {
        "id": e.id,
        "month": e.month,
        "type": e.type.name,
        "text": e.text,
        "kingdom_ids": list(e.kingdom_ids),
        "params": params_encoded,
        "msg_key": e.msg_key,
    }


def _deserialize_event(d: dict[str, Any], checkpoint_version: int = 2) -> Event:
    return Event(
        id=d["id"],
        month=d["month"],
        type=EventType[d["type"]],
        text=d["text"],
        kingdom_ids=tuple(d.get("kingdom_ids", [])),
        params=_decode_params(d.get("params", {}), checkpoint_version),
        msg_key=d.get("msg_key", ""),
    )


# ---------------------------------------------------------------------------
# API principal
# ---------------------------------------------------------------------------

def checkpoint_to_dict(engine: SimEngine, checkpoint_version: int = CHECKPOINT_VERSION) -> dict[str, Any]:
    """Convierte un SimEngine completo a un diccionario JSON-serializable.

    Incluye todo el estado mutable necesario para reconstruir el motor
    exactamente: RNG, contadores de ID, mapas, crónica, guerras, líderes,
    héroes, bestias, notables, sitios y atributos privados de los reinos.

    El parámetro ``checkpoint_version`` determina el formato de serialización
    de los parámetros de eventos:

    * v1: diccionarios planos, sin envoltorio tipado.
    * v2: diccionarios envueltos con ``__checkpoint_kind__`` y ``items``.
    """
    # Indexar razas y gobiernos por nombre.
    # TRIBAL_GOVERNMENT es una constante de módulo que puede no estar en
    # engine.governments (se usa directamente en _found_kingdom), así que
    # la aseguramos para que el gobierno de los reinos tribales sea hallable.
    all_governments = list(engine.governments)
    if TRIBAL_GOVERNMENT.name not in {g.name for g in all_governments}:
        all_governments.append(TRIBAL_GOVERNMENT)
    races_by_name = {r.name: r for r in engine.races}
    governments_by_name = {g.name: g for g in all_governments}

    # Primera pasada: serializar reinos (sin re-enlazar aún)
    kingdom_dicts = [_serialize_kingdom(k) for k in engine.kingdoms]

    # Indexar líderes, héroes y notables por ID para re-enlace
    leaders_by_id: dict[int, Leader] = {}
    heroes_by_id: dict[int, Hero] = {}
    notables_by_id: dict[int, Notable] = {}

    for k in engine.kingdoms:
        if k.leader is not None:
            leaders_by_id[k.leader.id] = _serialize_leader(k.leader)
        if k.hero is not None:
            heroes_by_id[k.hero.id] = _serialize_hero(k.hero)
        for n in k.notables:
            notables_by_id[n.id] = _serialize_notable(n)

    # Serializar guerras (por ID de reino)
    war_dicts = [_serialize_war(w) for w in engine.wars]

    # Serializar bestias
    beast_dicts = [_serialize_beast(b) for b in engine.beasts]

    # Serializar relaciones
    relations_encoded = {}
    for (a_id, b_id), rel in engine.relations.items():
        relations_encoded[f"{a_id}|{b_id}"] = rel.name

    # Serializar crónica
    event_dicts = [_serialize_event(e, checkpoint_version) for e in engine.chronicle.events]
    by_kingdom = {}
    for kid, ev_list in engine.chronicle._by_kingdom.items():
        by_kingdom[str(kid)] = [e.id for e in ev_list]

    # Siguiente ID de evento del contador de instancia de Chronicle
    next_event_id = int(getattr(engine.chronicle, "_next_event_id", 1))
    # Defensa: si por algún motivo el contador está por debajo del máximo ID
    # existente, adelantarlo para evitar colisiones.
    max_event_id = max((e.id for e in engine.chronicle.events), default=0)
    if next_event_id <= max_event_id:
        next_event_id = max_event_id + 1

    return {
        "_checkpoint_version": checkpoint_version,
        "_checkpoint_type": "simengine",
        "month": engine.month,
        "seed": engine.world.config.seed,
        "world_config": _serialize_world_config(engine.world.config),
        "world_maps": {
            name: _encode_ndarray(getattr(engine.world, name))
            for name in ("height", "temperature", "precipitation", "drainage", "biome", "prosperity", "rivers")
        },
        "rng_state": _encode_rng_state(engine.rng),
        "races": [_serialize_race(r) for r in engine.races],
        "governments": [_serialize_government(g) for g in engine.governments],
        "kingdoms": kingdom_dicts,
        "leaders": list(leaders_by_id.values()),
        "heroes": list(heroes_by_id.values()),
        "notables": list(notables_by_id.values()),
        "wars": war_dicts,
        "beasts": beast_dicts,
        "relations": relations_encoded,
        "chronicle": {
            "_next_event_id": next_event_id,
            "events": event_dicts,
            "_by_kingdom": by_kingdom,
        },
        "fallen": list(engine.fallen),
        "_next_war_id": engine._next_war_id,
        "_next_leader_id": engine._next_leader_id,
        "_next_hero_id": engine._next_hero_id,
        "_next_beast_id": engine._next_beast_id,
        "_next_notable_id": engine._next_notable_id,
        "_milestones": sorted(engine._milestones),
        "_world_techs": sorted(engine._world_techs),
        "_color_counter": engine._color_counter,
        "_occupancy": _encode_ndarray(engine._occupancy),
        "_battle_heat": _encode_ndarray(engine._battle_heat),
    }


def engine_from_checkpoint(state: dict[str, Any]) -> SimEngine:
    """Reconstruye un SimEngine desde un diccionario de checkpoint.

    Valida la versión y lanza ValueError si el formato es incompatible o
    el checkpoint está corrupto. No se confía ciegamente en el contenido.
    """
    # --- Validación de versión y tipo ---
    checkpoint_version = state.get("_checkpoint_version")
    if checkpoint_version not in (1, 2):
        raise ValueError(
            f"Versión de checkpoint incompatible: "
            f"esperada 1 o 2, encontrada {checkpoint_version!r}"
        )
    if state.get("_checkpoint_type") != "simengine":
        raise ValueError(
            f"Tipo de checkpoint inválido: "
            f"{state.get('_checkpoint_type')!r}"
        )

    # --- Reconstruir mundo base ---
    world_config = _deserialize_world_config(state["world_config"])
    world_maps_encoded = state["world_maps"]
    world_maps = {name: _decode_ndarray(world_maps_encoded[name]) for name in world_maps_encoded}

    world = World(
        config=world_config,
        height=world_maps["height"],
        temperature=world_maps["temperature"],
        precipitation=world_maps["precipitation"],
        drainage=world_maps["drainage"],
        biome=world_maps["biome"],
        prosperity=world_maps["prosperity"],
        rivers=world_maps["rivers"],
    )

    # --- Reconstruir razas y gobiernos ---
    races = [_deserialize_race(d) for d in state["races"]]
    governments = [_deserialize_government(d) for d in state["governments"]]
    # Asegurar TRIBAL_GOVERNMENT (puede no estar serializada si no fue
    # parte de engine.governments original, pero los reinos la usan)
    if not any(g.name == "Tribal" for g in governments):
        governments.append(TRIBAL_GOVERNMENT)
    races_by_name = {r.name: r for r in races}
    governments_by_name = {g.name: g for g in governments}

    # --- Crear el motor sin ejecutar _spawn_kingdoms ni _spawn_beasts ---
    # Usamos __new__ para evitar __init__ y luego asignamos todo manualmente.
    engine = SimEngine.__new__(SimEngine)
    engine.world = world
    engine.races = races
    engine.governments = governments
    engine.rng = _decode_rng_state(state["rng_state"])
    engine.namegen = None  # se recreará abajo con el RNG restaurado
    engine.month = int(state["month"])
    engine.chronicle = Chronicle()
    engine.kingdoms = []
    engine.wars = []
    engine.beasts = []
    engine.relations = {}
    engine._occupancy = _decode_ndarray(state["_occupancy"])
    engine._battle_heat = _decode_ndarray(state["_battle_heat"])
    engine._next_war_id = int(state["_next_war_id"])
    engine._next_leader_id = int(state["_next_leader_id"])
    engine._next_hero_id = int(state["_next_hero_id"])
    engine._next_beast_id = int(state["_next_beast_id"])
    engine._next_notable_id = int(state["_next_notable_id"])
    engine._milestones = set(state.get("_milestones", []))
    engine._world_techs = set(state.get("_world_techs", []))
    engine.fallen = list(state.get("fallen", []))
    engine._color_counter = int(state.get("_color_counter", 0))

    # Recrear NameGenerator con el RNG restaurado
    from worldsim.data.namegen import NameGenerator
    engine.namegen = NameGenerator(engine.rng)

    # --- Reconstruir líderes, héroes y notables (primero, para re-enlace) ---
    leaders_by_id: dict[int, Leader] = {}
    for d in state.get("leaders", []):
        l = _deserialize_leader(d)
        leaders_by_id[l.id] = l

    heroes_by_id: dict[int, Hero] = {}
    for d in state.get("heroes", []):
        h = _deserialize_hero(d)
        heroes_by_id[h.id] = h

    notables_by_id: dict[int, Notable] = {}
    for d in state.get("notables", []):
        n = _deserialize_notable(d)
        notables_by_id[n.id] = n

    # --- Reconstruir reinos ---
    for d in state.get("kingdoms", []):
        k = _deserialize_kingdom(
            d, races_by_name, governments_by_name,
            leaders_by_id, heroes_by_id, notables_by_id,
        )
        engine.kingdoms.append(k)

    # --- Reconstruir guerras (después de los reinos) ---
    for d in state.get("wars", []):
        w = _deserialize_war(d, {k.id: k for k in engine.kingdoms})
        engine.wars.append(w)

    # --- Reconstruir bestias ---
    for d in state.get("beasts", []):
        b = _deserialize_beast(d, races_by_name)
        engine.beasts.append(b)

    # --- Reconstruir relaciones ---
    for key, rel_name in state.get("relations", {}).items():
        a_str, b_str = key.split("|")
        a_id, b_id = int(a_str), int(b_str)
        engine.relations[(a_id, b_id)] = Relation[rel_name]

    # --- Reconstruir crónica ---
    chronicle_state = state.get("chronicle", {})
    for d in chronicle_state.get("events", []):
        e = _deserialize_event(d, checkpoint_version)
        engine.chronicle.events.append(e)
        for kid in e.kingdom_ids:
            engine.chronicle._by_kingdom.setdefault(kid, []).append(e)

    # Restaurar el contador de instancia de Chronicle (NO tocar el global
    # _EVENT_ID de módulo, para no afectar a otros motores en el proceso).
    # Recuperación segura para checkpoints antiguos que pueden no tener
    # _next_event_id:
    #   - Si existe y es un entero válido (> max id existente), conservarlo.
    #   - Si no existe, es booleano, float, string, o es inconsistente
    #     (<= max id existente), calcular max(event.id) + 1.
    max_event_id = max((e.id for e in engine.chronicle.events), default=0)
    saved_next_id = chronicle_state.get("_next_event_id")
    if (
        saved_next_id is not None
        and isinstance(saved_next_id, int)
        and not isinstance(saved_next_id, bool)
        and saved_next_id > max_event_id
    ):
        engine.chronicle._next_event_id = saved_next_id
    else:
        engine.chronicle._next_event_id = max_event_id + 1

    # Invalidar cachés derivadas (se recomputan bajo demanda)
    engine._terr_cache = None
    engine._terr_cache_month = -1
    engine._inf_cache = None
    engine._inf_cache_month = -1
    engine._inf_cache_sig = None

    return engine


def checkpoint_to_json(engine: SimEngine) -> str:
    """Serializa un SimEngine a una cadena JSON.

    Usa ``allow_nan=False`` para impedir que valores no finitos
    (NaN, Infinity, -Infinity) aparezcan en el checkpoint serializado.
    """
    return json.dumps(
        checkpoint_to_dict(engine), ensure_ascii=False, indent=2, allow_nan=False
    )


def engine_from_json(json_str: str) -> SimEngine:
    """Reconstruye un SimEngine desde una cadena JSON."""
    state = json.loads(json_str)
    return engine_from_checkpoint(state)
