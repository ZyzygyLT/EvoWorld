"""Load races/governments from the original df-style-worldgen .txt formats."""

from __future__ import annotations

import re
from pathlib import Path

from worldsim.sim.models import GovernmentType, Race

_RACE_LINE = re.compile(r"^\[(\w+)\](.*)$")
_GOV_LINE = re.compile(r"^\[(\w+)\](.*)$")


def _parse_blocks(path: Path) -> list[dict[str, str]]:
    """Parse `[TAG]value` runs into record dicts."""
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _RACE_LINE.match(line)
        if not m:
            continue
        tag, value = m.group(1).upper(), m.group(2).strip()
        if tag == "RACE" or tag == "TYPE":
            if current:
                records.append(current)
            current = {"NAME": value}
        else:
            current[tag] = value
    if current:
        records.append(current)
    return records


def load_races(path: str | Path) -> list[Race]:
    path = Path(path)
    races = []
    for rec in _parse_blocks(path):
        biomes = tuple(int(s) for s in rec.get("BIOME", "").split() if s.isdigit())
        races.append(
            Race(
                name=rec["NAME"],
                preferred_biomes=biomes,
                strength=int(rec.get("STRENGTH", rec.get("STRENGHT", 1))),
                size=int(rec.get("SIZE", 1)),
                reproduction=int(rec.get("REPRODUCTION", 100)),
                aggressiveness=int(rec.get("AGGRESSIVENESS", 1)),
                form=rec.get("FORM", "civilized").lower(),
            )
        )
    return races


def load_governments(path: str | Path) -> list[GovernmentType]:
    path = Path(path)
    govs = []
    for rec in _parse_blocks(path):
        govs.append(
            GovernmentType(
                name=rec["NAME"],
                description=rec.get("DESCRIPTION", ""),
                aggressiveness=int(rec.get("AGGRESSIVENESS", 1)),
                militarization=int(rec.get("MILITARIZATION", 20)),
                tech_bonus=int(rec.get("TECH", 0)),
            )
        )
    return govs


__all__ = ["load_races", "load_governments"]
