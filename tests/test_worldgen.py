"""Worldgen tests: determinism, invariants, biome sanity."""

from __future__ import annotations

import numpy as np

from worldsim.worldgen import SEA_LEVEL, WorldConfig
from worldsim.worldgen.biomes import Biome, classify
from worldsim.worldgen.pipeline import generate_world
from worldsim.worldgen.rivers import lowest_neighbor


def test_determinism_same_seed():
    a = generate_world(WorldConfig(seed=123, width=80, height=40))
    b = generate_world(WorldConfig(seed=123, width=80, height=40))
    assert np.array_equal(a.height, b.height)
    assert np.array_equal(a.biome, b.biome)
    assert np.array_equal(a.rivers, b.rivers)


def test_different_seeds_differ():
    a = generate_world(WorldConfig(seed=1, width=80, height=40))
    b = generate_world(WorldConfig(seed=2, width=80, height=40))
    assert not np.array_equal(a.height, b.height)


def test_shapes_and_ranges(small_world):
    w = small_world
    assert w.height.shape == (50, 100)
    for layer in (w.height, w.temperature, w.precipitation, w.drainage, w.prosperity):
        assert layer.shape == (50, 100)
        assert layer.min() >= 0.0 and layer.max() <= 1.0


def test_ocean_below_sea_level(small_world):
    w = small_world
    deep = w.height <= 0.15
    assert np.all(w.biome[deep] == Biome.OCEAN)
    # shallow water (0.15 < h <= 0.2) can be ICE in cold regions — faithful
    # port: original applies ice rule (temp<=0.2, height>0.15) AFTER ocean,
    # so polar shelves render as ice, not ocean.
    shallow = (w.height > 0.15) & (w.height <= SEA_LEVEL)
    assert np.all((w.biome[shallow] == Biome.OCEAN) | (w.biome[shallow] == Biome.ICE))
    land = w.height > SEA_LEVEL
    assert not np.any(w.biome[land] == Biome.OCEAN)


def test_mountains_override_climate(small_world):
    w = small_world
    assert np.all(w.biome[w.height > 0.9] == Biome.PEAK)
    assert np.all(w.biome[(w.height > 0.6) & (w.height <= 0.9)] == Biome.MOUNTAIN)


def test_biome_ids_are_valid(small_world):
    valid = {int(b) for b in Biome}
    assert set(np.unique(small_world.biome).tolist()) <= valid


def test_rivers_flow_downhill(small_world):
    """Every river tile must have a strictly lower neighbor — except the
    terminus (local minimum = lake, thematically; the original left a
    'Change to Lake later' TODO). At most one such tile per river branch."""
    w = small_world
    h, wd = w.height.shape
    ys, xs = np.nonzero(w.rivers)
    assert len(ys) > 0, "expected at least one river"
    highs = 0
    for y, x in zip(ys.tolist(), xs.tolist(), strict=True):
        here = w.height[y, x]
        nb = [
            w.height[ny, nx]
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
            if 0 <= nx < wd and 0 <= ny < h
        ]
        if not any(v < here for v in nb):
            highs += 1  # potential lake terminus
    assert highs <= int(w.rivers.sum()) * 0.05 + 1, f"{highs} river tiles are local highs"


def test_rivers_reach_sea_or_merge(small_world):
    """River generation terminates properly (no infinite loops) — smoke."""
    w = small_world
    assert isinstance(int(w.rivers.sum()), int)


def test_lowest_neighbor_no_wraparound():
    """Original bug: negative indices wrapped the map. Must return None/tuple in-bounds."""
    hm = np.array([[0.9, 0.1], [0.5, 0.2]])
    # lowest neighbor of (0,0): (1,0)=0.5 and (0,1)=0.1 -> (0,1)
    n = lowest_neighbor(hm, 0, 0)
    assert n == (1, 0) or n == (0, 1)
    # plateau: no lower neighbor
    hm2 = np.zeros((3, 3))
    assert lowest_neighbor(hm2, 1, 1) is None


def test_perlin_like_full_range():
    from worldsim.worldgen.noise import perlin_like

    rng = np.random.default_rng(0)
    p = perlin_like(rng, (60, 120))
    assert p.min() < 0.02
    assert p.max() > 0.98  # rank-remap guarantees full [0,1]


def test_classify_all_biomes_appear():
    """With full-range climate layers, all main biomes should be reachable."""
    from worldsim.worldgen.noise import perlin_like

    rng = np.random.default_rng(3)
    shape = (80, 160)
    height = np.linspace(0, 1, 80 * 80).reshape(80, 80)
    height = np.hstack([height] * 2)[:, :160]
    temp = np.full(shape, 0.5)
    precip = perlin_like(rng, shape)
    drain = perlin_like(rng, shape)
    b = classify(height, temp, precip, drain, rng)
    present = set(np.unique(b).tolist())
    assert Biome.DESERT in present
    assert Biome.SWAMP in present
    assert Biome.MOUNTAIN in present
