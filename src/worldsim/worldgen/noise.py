"""Deterministic noise utilities built on numpy RNG (no external noise lib needed)."""

from __future__ import annotations

import numpy as np


def _smooth(t: np.ndarray) -> np.ndarray:
    """Quintic smoothstep (Perlin's improved interpolation)."""
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


def _fade2(a: np.ndarray, b: np.ndarray, t: np.ndarray) -> np.ndarray:
    return a + _smooth(t) * (b - a)


class ValueNoise2D:
    """Tileable value-noise on an arbitrary lattice resolution.

    Smooth, cheap, and perfectly deterministic from a seed — used both for
    heightmap fBm layers and climate noise layers.
    """

    def __init__(self, rng: np.random.Generator, res: int) -> None:
        self.res = res
        self.lattice = rng.random((res, res))

    def sample(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        """Bilinear-interpolate lattice at normalized coords in [0,1)."""
        res = self.res
        fx = np.clip(xs, 0.0, 1.0 - 1e-9) * res
        fy = np.clip(ys, 0.0, 1.0 - 1e-9) * res
        x0 = np.floor(fx).astype(np.intp)
        y0 = np.floor(fy).astype(np.intp)
        x1 = (x0 + 1) % res
        y1 = (y0 + 1) % res
        tx = fx - x0
        ty = fy - y0

        top = _fade2(self.lattice[y0, x0], self.lattice[y0, x1], tx)
        bottom = _fade2(self.lattice[y1, x0], self.lattice[y1, x1], tx)
        return _fade2(top, bottom, ty)


def fbm(
    rng: np.random.Generator,
    shape: tuple[int, int],
    octaves: int = 5,
    base_res: int = 4,
    persistence: float = 0.5,
    lacunarity: float = 2.0,
) -> np.ndarray:
    """Fractal Brownian motion in [0,1], shape (H, W)."""
    h, w = shape
    ys, xs = np.meshgrid(
        np.linspace(0.0, 1.0, h, endpoint=False),
        np.linspace(0.0, 1.0, w, endpoint=False),
        indexing="ij",
    )
    out = np.zeros(shape, dtype=np.float64)
    amp = 1.0
    total = 0.0
    res = base_res
    for _ in range(octaves):
        layer = ValueNoise2D(rng, res)
        out += amp * layer.sample(xs, ys)
        total += amp
        amp *= persistence
        res = int(res * lacunarity)
    out /= total
    return out


def perlin_like(
    rng: np.random.Generator,
    shape: tuple[int, int],
    octaves: int = 4,
    base_res: int = 4,
    persistence: float = 0.5,
    lacunarity: float = 2.0,
) -> np.ndarray:
    """Perlin-style fBm with full [0,1] dynamic range.

    Value-noise fBm (above) averages towards the middle (histogram clusters
    around 0.4-0.7). The original libtcod Perlin fbm reaches the extremes,
    which the biome rules depend on (precip < 0.10, >= 0.75 etc.).
    This variant stretches the fBm via a rank/percentile remap, restoring
    a uniform dynamic range without any spatial artifacts.
    """
    raw = fbm(rng, shape, octaves=octaves, base_res=base_res,
              persistence=persistence, lacunarity=lacunarity)
    flat = raw.ravel()
    # rank -> uniform [0,1] (histogram equalization)
    order = flat.argsort().argsort().astype(np.float64)
    out = order / (flat.size - 1)
    return out.reshape(shape)
