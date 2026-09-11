"""Climate layers: temperature, precipitation, drainage.

Ports of the original Temperature() / Percipitaion() / drainage fBm stages.
"""

from __future__ import annotations

import numpy as np

from worldsim.worldgen.noise import perlin_like


def temperature(height: np.ndarray) -> np.ndarray:
    """Latitude gradient modified by altitude (port of original Temperature()).

    Equator hot, poles cold; high mountains colder, deep ocean milder.
    """
    h, w = height.shape
    ys = np.linspace(0.0, 1.0, h)[:, None]
    lat = 1.0 - np.abs(2.0 * ys - 1.0)  # 0 at poles, 1 at equator
    lat = np.broadcast_to(lat, (h, w))

    # altitude effect: oceans (<= .2) mild, peaks much colder
    alt = np.zeros_like(height)
    ocean = height <= 0.2
    alt[ocean] = 0.05
    mid = (height > 0.2) & (height <= 0.8)
    alt[mid] = 0.0
    peaks = height > 0.8
    alt[peaks] = (height[peaks] - 0.8) * 3.0

    temp = np.clip(lat * 1.15 - alt, 0.0, 1.0)
    return temp


def precipitation(rng: np.random.Generator, shape: tuple[int, int]) -> np.ndarray:
    """fBm precipitation, biased wetter at the equator (port of Percipitaion).

    Uses perlin_like (full dynamic range) so wet/dry thresholds of the biome
    rules (<0.10, >=0.75) actually trigger.
    """
    precip = perlin_like(rng, shape, octaves=4, base_res=4)
    h, w = shape
    ys = np.linspace(0.0, 1.0, h)[:, None]
    equator_bias = 1.0 - np.abs(2.0 * ys - 1.0)
    precip = precip * (0.6 + 0.4 * equator_bias)
    return np.clip(precip / precip.max(), 0.0, 1.0)


def drainage(rng: np.random.Generator, shape: tuple[int, int]) -> np.ndarray:
    """Independent fBm drainage layer."""
    d = perlin_like(rng, shape, octaves=4, base_res=4)
    return d / d.max()
