"""Full simulation-state serialization: kingdoms, sites, wars, chronicle, history."""

from __future__ import annotations

import json
from pathlib import Path

from worldsim.sim.engine import SimEngine


def engine_to_dict(engine: SimEngine) -> dict:
    """Complete, JSON-serializable snapshot of a simulation."""
    world = engine.world
    return {
        "month": engine.month,
        "year": engine.month // 12,
        "seed": world.config.seed,
        "world": {
            "width": world.width,
            "height": world.height_tiles,
            "biome": world.biome.tolist(),
            "rivers": world.rivers.astype(int).tolist(),
            "battle_heat": engine.battle_heat().round(2).tolist(),
            # quantized height 0..255 for client-side hillshade/coasts/lakes
            "elevation": ((world.height * 255).clip(0, 255)).astype(int).tolist(),
        },
        "kingdoms": [
            {
                "id": k.id,
                "name": k.name,
                "race": k.race.name,
                "government": k.government.name,
                "color": list(k.color),
                "population": int(k.total_population),
                "at_war": k.at_war,
                "sites": [
                    {
                        "name": s.name,
                        "x": s.x,
                        "y": s.y,
                        "category": s.category,
                        "population": int(s.population),
                        "is_capital": s.is_capital,
                    }
                    for s in k.sites
                ],
                "leader": (
                    {
                        "name": k.leader.name,
                        "title": k.leader.title,
                        "age_years": round(k.leader.reign_years(engine.month), 1),
                        "reign_years": round(k.leader.reign_years(engine.month), 1),
                        "battles_won": k.leader.battles_won,
                        "battles_lost": k.leader.battles_lost,
                    }
                    if k.leader
                    else None
                ),
                "hero": (
                    {
                        "name": k.hero.name,
                        "strength": round(k.hero.strength, 2),
                        "deeds": len(k.hero.battle_deeds),
                    }
                    if k.hero
                    else None
                ),
                "population_history": [int(p) for p in k.population_history],
                "tech_level": k.tech_level,
                "golden_age": k.golden_age_until > engine.month,
                "founded_month": k.founded_month,
                "territory": (engine.territory_map() == k.id).astype(int).tolist(),
            }
            for k in engine.kingdoms
        ],
        "influence": engine.influence_map().tolist(),
        "wars": [
            {
                "id": w.id,
                "name": w.name,
                "cause": w.cause,
                "a": w.side_a.name,
                "b": w.side_b.name,
                "start_month": w.start_month,
                "end_month": w.end_month,
                "victor": w.victor.name if w.victor else None,
                "battles": w.battles,
                "total_dead": w.total_dead,
            }
            for w in engine.wars
        ],
        "beasts": [
            {"name": b.name, "race": b.race.name, "x": b.x, "y": b.y, "alive": b.hp > 0}
            for b in engine.beasts
        ],
        "relations": {
            f"{a}|{b}": rel.name
            for (a, b), rel in engine.relations.items()
        },
        "chronicle": [
            {
                "id": e.id,
                "month": e.month,
                "type": e.type.name,
                "text": e.text,
                "kingdoms": list(e.kingdom_ids),
                "msg_key": e.msg_key,
                "params": e.params,
            }
            for e in engine.chronicle.events
        ],
    }


def engine_to_json(engine: SimEngine, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(engine_to_dict(engine)), encoding="utf-8")
    return path


__all__ = ["engine_to_dict", "engine_to_json"]
