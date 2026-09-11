"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from worldsim.data.loaders import load_governments, load_races
from worldsim.sim.engine import SimEngine
from worldsim.worldgen import WorldConfig
from worldsim.worldgen.pipeline import generate_world

DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return DATA


@pytest.fixture(scope="session")
def races(data_dir):
    return load_races(data_dir / "Races.txt")


@pytest.fixture(scope="session")
def governments(data_dir):
    return load_governments(data_dir / "CivilizedGovernment.txt")


@pytest.fixture(scope="session")
def small_world():
    return generate_world(WorldConfig(seed=42, width=100, height=50, rivers=4))


@pytest.fixture()
def engine(small_world, races, governments):
    return SimEngine(small_world, races, governments, seed=42)
