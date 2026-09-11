"""River generation: downhill greedy tracing (port of RiverGen, bugs fixed).

Original bugs fixed here:
  * only ONE river was generated per world (call loop `range(1)`)
  * LowestNeighbour read World[X-1][Y] before checking X-1 > 0 — negative
    indices wrapped around the map (Python) creating phantom rivers
  * no lake termination
"""

from __future__ import annotations

import numpy as np

from worldsim.worldgen import SEA_LEVEL

MIN_RIVER_LENGTH = 3
MAX_TRACE = 400


def lowest_neighbor(height: np.ndarray, x: int, y: int) -> tuple[int, int] | None:
    """Lowest of the 4 cardinal neighbors; None if we are the local minimum."""
    h, w = height.shape
    best: tuple[int, int] | None = None
    best_v = height[y, x]
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nx, ny = x + dx, y + dy
        if 0 <= nx < w and 0 <= ny < h and height[ny, nx] < best_v:
            best_v = height[ny, nx]
            best = (nx, ny)
    return best


def trace_river(
    height: np.ndarray, rivers: np.ndarray, rng: np.random.Generator, start: tuple[int, int]
) -> list[tuple[int, int]]:
    """Trace one river from `start` downhill until sea / merge / stall. Marks `rivers`."""
    h, w = height.shape
    x, y = start
    if height[y, x] <= SEA_LEVEL or rivers[y, x]:
        return []

    path: list[tuple[int, int]] = []
    visited = {(x, y)}
    cx, cy = x, y
    for _ in range(MAX_TRACE):
        nxt = lowest_neighbor(height, cx, cy)
        if nxt is None:
            break  # local minimum -> end (lake)
        cx, cy = nxt
        if rivers[cy, cx]:
            # merge into existing river: mark and stop
            path.append((cx, cy))
            break
        if height[cy, cx] <= SEA_LEVEL:
            path.append((cx, cy))
            break
        if (cx, cy) in visited:
            break  # loop protection
        visited.add((cx, cy))
        path.append((cx, cy))

    if len(path) < MIN_RIVER_LENGTH:
        return []
    for px, py in path:
        rivers[py, px] = True
    return path


def generate_rivers(
    height: np.ndarray, rng: np.random.Generator, count: int, max_attempts: int = 4000
) -> np.ndarray:
    """Try to lay `count` rivers from random high-ground starts."""
    h, w = height.shape
    rivers = np.zeros((h, w), dtype=bool)
    laid = 0
    attempts = 0
    while laid < count and attempts < max_attempts:
        attempts += 1
        x = int(rng.integers(0, w))
        y = int(rng.integers(0, h))
        if height[y, x] < 0.8:
            continue
        if trace_river(height, rivers, rng, (x, y)):
            laid += 1
    return rivers
