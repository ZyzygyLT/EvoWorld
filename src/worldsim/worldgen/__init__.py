"""Worldgen: procedural planet generation pipeline (numpy port of MasterWorldGen)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np

SEA_LEVEL = 0.2
"""Tiles at or below this height are ocean."""

MOUNTAIN_LEVEL = 0.6
PEAK_LEVEL = 0.9


@dataclass(frozen=True)
class WorldConfig:
    """Parameters of one generated world."""

    width: int = 200
    height: int = 80
    seed: int = 0
    big_hills: int = 250
    small_hills: int = 1000
    rivers: int = 12
    margin: float = 0.1  # fraction of each axis kept as guaranteed ocean frame

    @property
    def mx(self) -> int:
        return int(self.width * self.margin)

    @property
    def my(self) -> int:
        return int(self.height * self.margin)


@dataclass
class World:
    """A fully generated world: layered maps + derived fields."""

    config: WorldConfig
    height: np.ndarray  # (H, W) float [0,1]
    temperature: np.ndarray
    precipitation: np.ndarray
    drainage: np.ndarray
    biome: np.ndarray  # (H, W) int Biome
    prosperity: np.ndarray
    rivers: np.ndarray = field(default_factory=lambda: np.zeros((1, 1), dtype=bool))

    def __post_init__(self) -> None:
        h, w = self.height.shape
        if self.rivers.shape != (h, w):
            self.rivers = np.zeros((h, w), dtype=bool)

    @property
    def width(self) -> int:
        return self.height.shape[1]

    @property
    def height_tiles(self) -> int:
        return self.height.shape[0]

    def is_land(self, x: int, y: int) -> bool:
        return self.height[y, x] > SEA_LEVEL

    def to_json_dict(self) -> dict:
        return {
            "config": {
                "width": self.width,
                "height": self.height_tiles,
                "seed": self.config.seed,
            },
            "biome": self.biome.tolist(),
        }


def world_to_json(world: World, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(world.to_json_dict(), f)


__all__ = [
    "SEA_LEVEL",
    "MOUNTAIN_LEVEL",
    "PEAK_LEVEL",
    "WorldConfig",
    "World",
    "world_to_json",
]
