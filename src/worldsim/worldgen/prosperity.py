"""Prosperity: how good a tile is for settlement (port of original Prosperity())."""

from __future__ import annotations

import numpy as np

IDEAL_PRECIP = 0.6
IDEAL_TEMP = 0.5


def prosperity(
    precipitation: np.ndarray, temperature: np.ndarray, drainage: np.ndarray
) -> np.ndarray:
    """Mean of: closeness to ideal precip, closeness to ideal temp, drainage."""
    p = 1.0 - np.abs(precipitation - IDEAL_PRECIP)
    t = 1.0 - np.abs(temperature - IDEAL_TEMP)
    d = drainage
    return np.clip((p + t + d) / 3.0, 0.0, 1.0)
