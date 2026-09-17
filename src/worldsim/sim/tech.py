"""Technology tree data: 22 nodes in 4 branches with data-driven effects.

Effects are plain additive bonuses aggregated per kingdom (see
SimEngine._refresh_mods); the engine never branches on individual tech ids
except for unlock flags. All choices use the seeded RNG (deterministic).
"""

from __future__ import annotations

# effect keys and how they combine (documented for balance tuning):
#   army, popcap, growth, tech  -> additive, used as (1 + sum)
#   plague                      -> additive mortality delta (negative helps)
#   colony                      -> additive to the 0.25 colony roll chance
#   capture                     -> additive to the 0.35 base capture chance
#   influence                   -> additive tiles to claim radius
#   golden                      -> additive to the golden-age roll chance
BRANCHES = ("warfare", "craft", "trade", "lore")

TECHS: dict[str, dict] = {
    # --- warfare ---------------------------------------------------------
    "ambush": {
        "branch": "warfare", "era": 0, "cost": 0.6, "requires": [],
        "effects": {"army": 0.05},
    },
    "bronze_arms": {
        "branch": "warfare", "era": 1, "cost": 0.8, "requires": ["ambush"],
        "effects": {"army": 0.10},
    },
    "iron_arms": {
        "branch": "warfare", "era": 2, "cost": 1.0, "requires": ["bronze_arms"],
        "effects": {"army": 0.15},
    },
    "phalanx": {
        "branch": "warfare", "era": 2, "cost": 1.0, "requires": ["iron_arms"],
        "effects": {"army": 0.10},
    },
    "siege": {
        "branch": "warfare", "era": 3, "cost": 1.2, "requires": ["phalanx"],
        "effects": {"capture": 0.10},
    },
    "steel": {
        "branch": "warfare", "era": 4, "cost": 1.4, "requires": ["siege"],
        "effects": {"army": 0.15},
    },
    # --- craft / sustenance ------------------------------------------------
    "agriculture": {
        "branch": "craft", "era": 0, "cost": 0.6, "requires": [],
        "effects": {"popcap": 0.10, "growth": 0.10},
    },
    "irrigation": {
        "branch": "craft", "era": 1, "cost": 0.8, "requires": ["agriculture"],
        "effects": {"popcap": 0.15},
    },
    "pottery": {
        "branch": "craft", "era": 1, "cost": 0.8, "requires": ["agriculture"],
        "effects": {"plague": -0.15},
    },
    "metalworking": {
        "branch": "craft", "era": 2, "cost": 1.0,
        "requires": ["pottery", "bronze_arms"],
        "effects": {"popcap": 0.10, "army": 0.05},
    },
    "guilds": {
        "branch": "craft", "era": 3, "cost": 1.2, "requires": ["metalworking"],
        "effects": {"growth": 0.15, "popcap": 0.10},
    },
    "architecture": {
        "branch": "craft", "era": 4, "cost": 1.4, "requires": ["guilds"],
        "effects": {"popcap": 0.20},
    },
    # --- trade / sea --------------------------------------------------------
    "fishing": {
        "branch": "trade", "era": 0, "cost": 0.6, "requires": [],
        "effects": {"growth": 0.08, "popcap": 0.05},
    },
    "coastal_trade": {
        "branch": "trade", "era": 1, "cost": 0.8, "requires": ["fishing"],
        "effects": {"colony": 0.10, "growth": 0.05},
    },
    "currency": {
        "branch": "trade", "era": 2, "cost": 1.0, "requires": ["coastal_trade"],
        "effects": {"growth": 0.10, "popcap": 0.05},
    },
    "merchant_fleet": {
        "branch": "trade", "era": 3, "cost": 1.2, "requires": ["currency"],
        "effects": {"colony": 0.15, "growth": 0.05},
    },
    "cartography": {
        "branch": "trade", "era": 4, "cost": 1.4, "requires": ["merchant_fleet"],
        "effects": {"influence": 2.0, "popcap": 0.05},
    },
    # --- lore / governance ---------------------------------------------------
    "oral_tradition": {
        "branch": "lore", "era": 0, "cost": 0.6, "requires": [],
        "effects": {"tech": 0.08},
    },
    "writing": {
        "branch": "lore", "era": 1, "cost": 0.8, "requires": ["oral_tradition"],
        "effects": {"tech": 0.12},
    },
    "priesthood": {
        "branch": "lore", "era": 2, "cost": 1.0, "requires": ["writing"],
        "effects": {"golden": 0.0005, "plague": -0.10},
    },
    "philosophy": {
        "branch": "lore", "era": 3, "cost": 1.2, "requires": ["priesthood"],
        "effects": {"tech": 0.15},
    },
    "engineering": {
        "branch": "lore", "era": 4, "cost": 1.4,
        "requires": ["philosophy", "metalworking"],
        "effects": {"popcap": 0.15, "army": 0.10},
    },
    # --- renaissance (era 5) -------------------------------------------------
    "gunpowder": {
        "branch": "warfare", "era": 5, "cost": 1.6, "requires": ["steel"],
        "effects": {"army": 0.15, "capture": 0.05},
    },
    "banking": {
        "branch": "trade", "era": 5, "cost": 1.6, "requires": ["cartography"],
        "effects": {"colony": 0.10, "growth": 0.10, "popcap": 0.05},
    },
    "printing": {
        "branch": "lore", "era": 5, "cost": 1.6, "requires": ["engineering"],
        "effects": {"tech": 0.20},
    },
    "renaissance_arts": {
        "branch": "craft", "era": 5, "cost": 1.6, "requires": ["architecture"],
        "effects": {"popcap": 0.15, "golden": 0.0005},
    },
    # --- enlightenment (era 6) -----------------------------------------------
    "artillery": {
        "branch": "warfare", "era": 6, "cost": 1.8, "requires": ["gunpowder"],
        "effects": {"army": 0.20},
    },
    "steam_power": {
        "branch": "craft", "era": 6, "cost": 1.8, "requires": ["banking", "printing"],
        "effects": {"popcap": 0.25, "growth": 0.15},
    },
    "encyclopedia": {
        "branch": "lore", "era": 6, "cost": 1.8, "requires": ["printing"],
        "effects": {"tech": 0.25, "plague": -0.10},
    },
}

# government affinity per branch (research taste); Tribal leans practical
GOV_BRANCH_AFFINITY: dict[str, dict[str, float]] = {
    "Tribal": {"warfare": 0.3, "craft": 0.3, "trade": 0.2, "lore": 0.2},
    "Absolute Monarchy": {"warfare": 0.6, "craft": 0.2, "trade": 0.1, "lore": 0.1},
    "Theocracy": {"warfare": 0.1, "craft": 0.1, "trade": 0.1, "lore": 0.7},
    "Military Dictatorship": {"warfare": 1.0, "craft": 0.0, "trade": 0.0, "lore": 0.0},
    "Constitutional monarchy": {"warfare": 0.3, "craft": 0.2, "trade": 0.3, "lore": 0.2},
    "Plutocracy": {"warfare": 0.1, "craft": 0.3, "trade": 0.5, "lore": 0.1},
    "Fanatic Theocracy": {"warfare": 0.5, "craft": 0.0, "trade": 0.0, "lore": 0.5},
    "Democracy": {"warfare": 0.1, "craft": 0.3, "trade": 0.3, "lore": 0.3},
    "Technocracy": {"warfare": 0.1, "craft": 0.5, "trade": 0.1, "lore": 0.3},
}

# display names (ES/EN kept next to the data so UI + tests share them)
TECH_NAMES: dict[str, dict[str, str]] = {
    "ambush": {"en": "Ambush Tactics", "es": "Tácticas de emboscada"},
    "bronze_arms": {"en": "Bronze Working", "es": "Trabajo del bronce"},
    "iron_arms": {"en": "Iron Working", "es": "Forja del hierro"},
    "phalanx": {"en": "Phalanx", "es": "Falange"},
    "siege": {"en": "Siege Craft", "es": "Arte del asedio"},
    "steel": {"en": "Steel", "es": "Acero"},
    "agriculture": {"en": "Agriculture", "es": "Agricultura"},
    "irrigation": {"en": "Irrigation", "es": "Irrigación"},
    "pottery": {"en": "Pottery", "es": "Alfarería"},
    "metalworking": {"en": "Metalworking", "es": "Metalurgia"},
    "guilds": {"en": "Guilds", "es": "Gremios"},
    "architecture": {"en": "Architecture", "es": "Arquitectura"},
    "fishing": {"en": "Fishing", "es": "Pesca"},
    "coastal_trade": {"en": "Coastal Trade", "es": "Comercio costero"},
    "currency": {"en": "Currency", "es": "Moneda"},
    "merchant_fleet": {"en": "Merchant Fleet", "es": "Flota mercante"},
    "cartography": {"en": "Cartography", "es": "Cartografía"},
    "oral_tradition": {"en": "Oral Tradition", "es": "Tradición oral"},
    "writing": {"en": "Writing", "es": "Escritura"},
    "priesthood": {"en": "Priesthood", "es": "Sacerdocio"},
    "philosophy": {"en": "Philosophy", "es": "Filosofía"},
    "engineering": {"en": "Engineering", "es": "Ingeniería"},
    "gunpowder": {"en": "Gunpowder", "es": "Pólvora"},
    "banking": {"en": "Banking", "es": "Banca"},
    "printing": {"en": "Printing Press", "es": "Imprenta"},
    "renaissance_arts": {"en": "Renaissance Arts", "es": "Artes renacentistas"},
    "artillery": {"en": "Artillery", "es": "Artillería"},
    "steam_power": {"en": "Steam Power", "es": "Máquina de vapor"},
    "encyclopedia": {"en": "Encyclopedia", "es": "Enciclopedia"},
}


def available_techs(owned: set[str], max_era: int) -> list[str]:
    """Tech ids whose prereqs are met and whose era is reachable, sorted."""
    out = [
        tid for tid, t in TECHS.items()
        if tid not in owned
        and t["era"] <= max_era + 1
        and all(r in owned for r in t["requires"])
    ]
    return sorted(out)


__all__ = [
    "BRANCHES", "TECHS", "GOV_BRANCH_AFFINITY", "TECH_NAMES",
    "available_techs",
]
