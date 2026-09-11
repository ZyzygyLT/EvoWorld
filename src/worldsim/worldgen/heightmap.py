"""Heightmap generation: hills + fBm, poles, tectonic ridges, rain erosion.

Vectorized numpy port of the hill-stacking / pole / tectonic / erosion stages of
the original MasterWorldGen, with the border bugs fixed (see _lowest_neighbor
note in rivers.py).
"""

from __future__ import annotations

import numpy as np

from worldsim.worldgen.noise import fbm


def normalize(hm: np.ndarray, lo: float = 0.0, hi: float = 1.0) -> np.ndarray:
    min_v = hm.min()
    max_v = hm.max()
    if max_v - min_v < 1e-12:
        return np.full_like(hm, (lo + hi) / 2)
    return lo + (hm - min_v) / (max_v - min_v) * (hi - lo)


def add_hill(hm: np.ndarray, cx: int, cy: int, radius: int, height: float) -> None:
    """Conic hill add (port of libtcod heightmap_add_hill)."""
    h, w = hm.shape
    x0, x1 = max(0, cx - radius), min(w, cx + radius + 1)
    y0, y1 = max(0, cy - radius), min(h, cy + radius + 1)
    if x0 >= x1 or y0 >= y1:
        return
    ys, xs = np.mgrid[y0:y1, x0:x1]
    dist2 = (xs - cx) ** 2 + (ys - cy) ** 2
    r2 = radius * radius
    inside = dist2 < r2
    factor = np.where(inside, height * (r2 - dist2) / r2, 0.0)
    hm[y0:y1, x0:x1] += factor


def base_heightmap(rng: np.random.Generator, cfg, fbm_octaves: int = 5) -> np.ndarray:
    """Big hills + small hills + fBm multiply (original stages 1-2)."""
    w, hgt = cfg.width, cfg.height
    hm = np.zeros((hgt, w), dtype=np.float64)

    mx, my = cfg.mx, cfg.my
    for _ in range(cfg.big_hills):
        add_hill(
            hm,
            cx=int(rng.integers(mx, w - mx)),
            cy=int(rng.integers(my, hgt - my)),
            radius=int(rng.integers(12, 17)),
            height=float(rng.integers(6, 11)),
        )
    for _ in range(cfg.small_hills):
        add_hill(
            hm,
            cx=int(rng.integers(mx, w - mx)),
            cy=int(rng.integers(my, hgt - my)),
            radius=int(rng.integers(2, 5)),
            height=float(rng.integers(6, 11)),
        )

    hm = normalize(hm)

    noise = fbm(rng, hm.shape, octaves=fbm_octaves, base_res=4)
    # Port of heightmap_multiply_hm(hm, noisehm, hm): multiply normalized
    # heightmap by normalized fBm. NOTE: no re-normalization afterwards — the
    # value compression towards low ground is what creates oceans.
    hm = np.clip(hm * noise, 0.0, 1.0)
    return hm


def carve_poles(hm: np.ndarray, rng: np.random.Generator) -> None:
    """Force a wavy ice-shelf band at top and bottom (original PoleGen).

    Original: each pole's depth random-walks within [2,6] rows across x.
    """
    h, w = hm.shape
    edge_n = _wavy_offset(rng, w, 2, 6)
    edge_s = h - _wavy_offset(rng, w, 2, 6)
    for x in range(w):
        hm[: edge_n[x], x] = 0.31
        hm[edge_s[x] :, x] = 0.31


def _wavy_offset(rng: np.random.Generator, w: int, lo: int, hi: int) -> np.ndarray:
    """Random-walk line (port of the original pos += randint(1,5)-3 clamps)."""
    out = np.zeros(w, dtype=int)
    pos = int(rng.integers(lo, hi))
    for x in range(w):
        out[x] = pos
        pos = min(max(pos + int(rng.integers(-2, 2)), lo), hi)
    return out


def tectonic_ridges(hm: np.ndarray, rng: np.random.Generator) -> None:
    """Two random-walk ridges (one horizontal, one vertical) lifting mountains.

    Port of TectonicGen: borders only raise land already above sea level.
    """
    h, w = hm.shape
    # horizontal border
    pos = int(rng.integers(h // 10, h - h // 10))
    for x in range(w):
        pos = min(max(pos + int(rng.integers(-2, 3)), 0), h - 1)
        if hm[pos, x] > 0.3:
            add_hill(hm, x, pos, int(rng.integers(2, 5)), float(rng.uniform(0.15, 0.18)))
    # vertical border
    pos = int(rng.integers(w // 10, w - w // 10))
    for y in range(h):
        pos = min(max(pos + int(rng.integers(-2, 3)), 0), w - 1)
        if hm[y, pos] > 0.3:
            add_hill(hm, pos, y, int(rng.integers(2, 5)), float(rng.uniform(0.15, 0.18)))


def rain_erosion(hm: np.ndarray, rng: np.random.Generator, drops: int | None = None) -> None:
    """Simplified hydraulic erosion (port of libtcod heightmap_rain_erosion).

    Each drop walks to the lowest neighbor, carrying sediment downhill and
    depositing it, smoothing the map. Vectorized-ish: drops are independent.
    """
    h, w = hm.shape
    if drops is None:
        drops = h * w
    xs = rng.integers(1, w - 1, size=drops)
    ys = rng.integers(1, h - 1, size=drops)
    for x, y in zip(xs.tolist(), ys.tolist(), strict=False):
        cx, cy = x, y
        for _ in range(32):
            best_x, best_y, best_v = cx, cy, hm[cy, cx]
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = cx + dx, cy + dy
                if 0 <= nx < w and 0 <= ny < h and hm[ny, nx] < best_v:
                    best_x, best_y, best_v = nx, ny, hm[ny, nx]
            if best_x == cx and best_y == cy:
                break
            hm[best_y, best_x] += (hm[cy, cx] - hm[best_y, best_x]) * 0.5
            cx, cy = best_x, best_y
