"""Export world/sim state to PNG and JSON."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from worldsim.worldgen import World
from worldsim.worldgen.biomes import Biome

# RGB per biome (port of NormalMap ColorDictionary)
BIOME_COLORS: dict[int, tuple[int, int, int]] = {
    int(Biome.OCEAN): (13, 103, 196),
    int(Biome.SWAMP): (68, 158, 53),
    int(Biome.MARSH): (131, 212, 82),
    int(Biome.GRASSLAND): (131, 212, 82),
    int(Biome.DESERT): (255, 218, 90),
    int(Biome.CONIFER_FOREST): (68, 158, 53),
    int(Biome.BROADLEAF_FOREST): (34, 120, 42),
    int(Biome.BADLANDS): (204, 159, 81),
    int(Biome.MOUNTAIN): (185, 192, 162),
    int(Biome.PEAK): (230, 230, 230),
    int(Biome.ICE): (176, 223, 215),
    int(Biome.SMALL_HILLS): (131, 212, 82),
    int(Biome.SHRUBLAND): (150, 190, 90),
    int(Biome.HILL): (150, 190, 90),
}

_MODES = {
    "biome": "biome",
    "height": "height",
    "temperature": "temperature",
    "precipitation": "precipitation",
    "drainage": "drainage",
    "prosperity": "prosperity",
}


def world_to_rgb(world: World, mode: str = "biome", rivers: bool = True) -> np.ndarray:
    """(H, W, 3) uint8 RGB of the requested layer."""
    if mode == "biome":
        h, w = world.biome.shape
        rgb = np.zeros((h, w, 3), dtype=np.uint8)
        for bval, color in BIOME_COLORS.items():
            mask = world.biome == bval
            rgb[mask] = color
        if rivers and world.rivers.any():
            rgb[world.rivers] = (60, 140, 255)
        return rgb
    if mode == "height":
        g = (np.clip(world.height, 0, 1) * 255).astype(np.uint8)
        return np.stack([g, g, g], axis=-1)
    layer = getattr(world, _MODES[mode])
    # gradient white -> mode color
    color = {
        "temperature": (255, 60, 40),
        "precipitation": (60, 120, 255),
        "drainage": (160, 110, 60),
        "prosperity": (60, 180, 80),
    }[mode]
    base = np.full(layer.shape + (3,), 245, dtype=np.float64)
    col = np.array(color, dtype=np.float64)
    for c in range(3):
        base[..., c] = base[..., c] * (1 - layer) + col[c] * layer
    return base.astype(np.uint8)


def save_png(rgb: np.ndarray, path: str | Path) -> Path:
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(
            "PNG export requires pillow: uv sync --extra png (or pip install pillow)"
        ) from e
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb).save(path)
    return path


def world_to_json(world: World, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "seed": world.config.seed,
        "width": world.width,
        "height": world.height_tiles,
        "biome": world.biome.tolist(),
        "rivers": world.rivers.astype(int).tolist(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


__all__ = ["BIOME_COLORS", "world_to_rgb", "save_png", "world_to_json"]
