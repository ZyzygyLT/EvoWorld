"""Name generation for kingdoms/sites — syllable engine ported from jice_fantasy.cfg."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_START = [
    "Aer", "An", "Ar", "Ban", "Bar", "Ber", "Beth", "Bett", "Cut", "Dan",
    "Dar", "Dell", "Der", "Edr", "Er", "Eth", "Ett", "Fin", "Ian", "Iarr",
    "Ill", "Jed", "Kan", "Kar", "Ker", "Kurr", "Kyr", "Man", "Mar", "Mer",
    "Mir", "Tsal", "Tser", "Tsir", "Van", "Var", "Yur", "Yyr",
]
_MID = ["al", "an", "ar", "el", "en", "ess", "ian", "onn", "or"]
_END = ["ai", "an", "ar", "ath", "en", "eo", "ian", "is", "u", "or"]
_ILLEGAL = ("orar", "arrar")


class NameGenerator:
    """Deterministic-per-RNG fantasy name generator (port of libtcod namegen with jice rules)."""

    def __init__(self, rng) -> None:
        self.rng = rng

    def name(self) -> str:
        for _ in range(50):
            s = self.rng.choice(_START)
            if self.rng.random() < 0.5:
                m = self.rng.choice(_MID)
                e = self.rng.choice(_END)
                name = s + m + e
            else:
                e = self.rng.choice(_END)
                name = s + e
            if not any(bad in name.lower() for bad in _ILLEGAL):
                return name
        return s + self.rng.choice(_END)  # type: ignore[possibly-undefined]

    def kingdom_name(self, form: str) -> str:
        base = self.name()
        suffix = "Tribe" if form == "tribal" else "Kingdom"
        return f"{base} {suffix}"

    def site_name(self) -> str:
        return self.name()


@lru_cache(maxsize=1)
def default_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "namegen" / "jice_fantasy.cfg"


__all__ = ["NameGenerator"]
