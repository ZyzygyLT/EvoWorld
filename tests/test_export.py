"""Export tests (PNG/JSON)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from worldsim.export.png import save_png, world_to_json, world_to_rgb

pytest.importorskip("PIL")


def test_rgb_shapes(small_world):
    for mode in ("biome", "height", "temperature", "precipitation", "drainage", "prosperity"):
        rgb = world_to_rgb(small_world, mode)
        assert rgb.shape == (50, 100, 3)
        assert rgb.dtype == np.uint8


def test_png_roundtrip(small_world, tmp_path: Path):
    p = save_png(world_to_rgb(small_world), tmp_path / "w.png")
    assert p.exists() and p.stat().st_size > 1000


def test_json_roundtrip(small_world, tmp_path: Path):
    p = world_to_json(small_world, tmp_path / "w.json")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["width"] == 100 and data["height"] == 50
    assert len(data["biome"]) == 50 and len(data["biome"][0]) == 100
