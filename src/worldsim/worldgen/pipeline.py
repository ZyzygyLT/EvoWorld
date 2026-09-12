"""Master world generation pipeline (port of MasterWorldGen)."""

from __future__ import annotations

import time

import numpy as np

from worldsim.worldgen import World, WorldConfig
from worldsim.worldgen.biomes import classify
from worldsim.worldgen.climate import drainage as drainage_layer
from worldsim.worldgen.climate import precipitation as precipitation_layer
from worldsim.worldgen.climate import temperature as temperature_layer
from worldsim.worldgen.heightmap import (
    base_heightmap,
    carve_poles,
    normalize,
    rain_erosion,
    tectonic_ridges,
)
from worldsim.worldgen.prosperity import prosperity
from worldsim.worldgen.rivers import generate_rivers


def generate_world(cfg: WorldConfig | None = None, verbose: bool = False) -> World:
    """Run the full generation pipeline; deterministic for a given cfg.seed."""
    if cfg is None:
        cfg = WorldConfig()
    rng = np.random.default_rng(cfg.seed)

    t0 = time.perf_counter()

    # 1. heightmap: hills + fBm
    hm = base_heightmap(rng, cfg)

    # 2. poles
    carve_poles(hm, rng)

    # 3. tectonic ridges
    tectonic_ridges(hm, rng)

    # 4. erosion
    rain_erosion(hm, rng)

    # 5. clamp + normalize
    hm = np.clip(hm, 0.0, 1.0)
    hm = normalize(hm)

    # 6. climate
    temp = temperature_layer(hm, sea_level=cfg.sea_level)
    precip = precipitation_layer(rng, hm.shape)
    drain = drainage_layer(rng, hm.shape)

    # 7. rivers
    rivers = generate_rivers(hm, rng, count=cfg.rivers, sea_level=cfg.sea_level)

    # 8. biomes
    biome = classify(hm, temp, precip, drain, rng, sea_level=cfg.sea_level)

    # 9. prosperity
    prosper = prosperity(precip, temp, drain)

    elapsed = time.perf_counter() - t0
    if verbose:
        land_pct = (hm > cfg.sea_level).mean() * 100
        print(
            f"world generated in {elapsed:.2f}s — "
            f"{cfg.width}x{cfg.height}, seed={cfg.seed}, land={land_pct:.0f}%"
        )

    return World(
        config=cfg,
        height=hm,
        temperature=temp,
        precipitation=precip,
        drainage=drain,
        biome=biome,
        prosperity=prosper,
        rivers=rivers,
    )
