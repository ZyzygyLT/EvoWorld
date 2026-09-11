"""Biome classification — faithful port of the original if-chain.

The original applies climate rules as independent `if`s (later rules overwrite
earlier ones) and then applies OVERRIDES last (ocean, ice, mountain, peak),
so overrides always win. We keep exactly that order/semantics, vectorized.

Biome IDs (from BiomeIDs.txt):
    0  deep ocean       1  swamp          2  marsh         3  grassland
    4  desert           5  conifer forest 6  broadleaf     8  badlands
    9  mountain        10  peak          11-13 ice        14 small hills
   15  shrubland       16  hill
"""

from __future__ import annotations

from enum import IntEnum

import numpy as np

from worldsim.worldgen import MOUNTAIN_LEVEL, PEAK_LEVEL, SEA_LEVEL


class Biome(IntEnum):
    OCEAN = 0
    SWAMP = 1
    MARSH = 2
    GRASSLAND = 3
    DESERT = 4
    CONIFER_FOREST = 5
    BROADLEAF_FOREST = 6
    BADLANDS = 8
    MOUNTAIN = 9
    PEAK = 10
    ICE = 11
    SMALL_HILLS = 14
    SHRUBLAND = 15
    HILL = 16


def classify(
    height: np.ndarray,
    temperature: np.ndarray,
    precipitation: np.ndarray,
    drainage: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    """(H, W) int8 Biome map, same rule order/precedence as the original."""
    shape = height.shape
    land = height > SEA_LEVEL
    biome = np.full(shape, Biome.OCEAN, dtype=np.int8)

    roll = rng.random(shape)

    # ---- climate rules (original if-chain order; later rules overwrite) ----
    m = land & (precipitation >= 0.10) & (precipitation < 0.33) & (drainage < 0.5)
    biome[m] = Biome.GRASSLAND
    m &= roll >= 0.5
    biome[m] = Biome.HILL

    m = land & (precipitation >= 0.10) & (precipitation > 0.33)
    biome[m] = Biome.MARSH
    m &= precipitation >= 0.66
    biome[m] = Biome.SWAMP

    m = land & (precipitation >= 0.33) & (precipitation < 0.66) & (drainage >= 0.33)
    biome[m] = Biome.SHRUBLAND
    m &= roll >= 0.8
    biome[m] = Biome.CONIFER_FOREST

    m = (
        land
        & (temperature > 0.2)
        & (precipitation >= 0.66)
        & (drainage > 0.33)
    )
    biome[m] = Biome.CONIFER_FOREST
    m &= precipitation >= 0.75
    biome[m] = Biome.BROADLEAF_FOREST
    m = (
        land
        & (temperature > 0.2)
        & (precipitation >= 0.66)
        & (drainage > 0.33)
        & (precipitation < 0.75)
        & (roll >= 0.8)
    )
    biome[m] = Biome.SHRUBLAND

    m = land & (precipitation >= 0.10) & (precipitation < 0.33) & (drainage >= 0.5)
    biome[m] = Biome.HILL
    m &= roll >= 0.5
    biome[m] = Biome.SMALL_HILLS

    m = land & (precipitation < 0.10)
    biome[m] = Biome.DESERT
    m &= drainage > 0.5
    biome[m] = Biome.HILL
    m &= roll >= 0.5
    biome[m] = Biome.SMALL_HILLS
    m = land & (precipitation < 0.10) & (drainage >= 0.66)
    biome[m] = Biome.BADLANDS

    # ---- overrides (original applies these last, so they always win) ----
    biome[height <= SEA_LEVEL] = Biome.OCEAN
    cold_land = (temperature <= 0.2) & (height > 0.15)
    biome[cold_land] = Biome.ICE
    biome[height > MOUNTAIN_LEVEL] = Biome.MOUNTAIN
    biome[height > PEAK_LEVEL] = Biome.PEAK

    return biome


__all__ = ["Biome", "classify"]
