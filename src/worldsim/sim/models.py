"""Simulation domain models: races, governments, sites, kingdoms, wars, chronicle.

Fixes vs. the original:
  * Civ class had MUTABLE CLASS ATTRIBUTES (Sites = [], SuitableSites = [])
    shared across all instances — here every collection is per-instance.
  * Wars were declared but never resolved (armies sat still) — see sim/engine.py.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import IntEnum, auto


class SocialForm(IntEnum):
    CIVILIZED = auto()
    TRIBAL = auto()
    SITE = auto()  # lone beasts / monsters, no government


@dataclass(frozen=True)
class Race:
    name: str
    preferred_biomes: tuple[int, ...]
    strength: int  # 1-3
    size: int  # 1-4
    reproduction: int  # ~40-110 (base per original data)
    aggressiveness: int  # 0-2
    form: str  # "civilized" | "tribal" | "site"

    @property
    def social_form(self) -> SocialForm:
        return SocialForm[self.form.upper()]


@dataclass(frozen=True)
class GovernmentType:
    name: str
    description: str
    aggressiveness: int  # 1-2
    militarization: int  # percentage of population mobilizable
    tech_bonus: int


TRIBAL_GOVERNMENT = GovernmentType(
    name="Tribal",
    description="Council of elders; loose authority over kin groups.",
    aggressiveness=2,
    militarization=50,
    tech_bonus=0,
)


@dataclass
class Site:
    x: int
    y: int
    name: str
    category: str = "Village"  # Village | Town | City (by popcap)
    population: int = 20
    popcap: float = 0.0
    is_capital: bool = False

    def update_category(self) -> None:
        if self.population >= 400:
            self.category = "City"
        elif self.population >= 120:
            self.category = "Town"


# -- Leaders & heroes ------------------------------------------------------------


@dataclass
class Leader:
    """A named figure heading a kingdom (monarch, chief, elder...)."""

    id: int
    name: str
    kingdom_id: int
    title: str  # King / Chief / Elder ...
    born_month: int
    died_month: int | None = None
    battles_won: int = 0
    battles_lost: int = 0

    @property
    def age_years(self) -> float:
        end = self.died_month if self.died_month is not None else self.born_month
        return (end - self.born_month) / 12


@dataclass
class Hero:
    """A champion that can turn the tide of battles (GDD §7)."""

    id: int
    name: str
    kingdom_id: int
    strength: float  # multiplier on army power
    battle_deeds: list[str] = field(default_factory=list)

    @classmethod
    def roll_strength(cls, rng) -> float:
        # heroes are rare and variable: 1.5x .. 3x army multiplier
        return float(1.5 + rng.random() * 1.5)


# -- Kingdoms & wars ---------------------------------------------------------------


@dataclass
class Kingdom:
    id: int
    name: str
    race: Race
    government: GovernmentType
    color: tuple[int, int, int]
    sites: list[Site] = field(default_factory=list)
    total_population: int = 0
    at_war: bool = False
    army_size: float = 0.0
    army_pos: tuple[int, int] | None = None
    war_exhaustion: int = 0  # months at war, drives government shifts
    years_since_founding: int = 0
    leader: Leader | None = None
    hero: Hero | None = None
    population_history: list[int] = field(default_factory=list)  # yearly samples

    @property
    def aggression(self) -> int:
        return self.race.aggressiveness + self.government.aggressiveness

    @property
    def capital(self) -> Site | None:
        for s in self.sites:
            if s.is_capital:
                return s
        return self.sites[0] if self.sites else None


class Relation(IntEnum):
    ALLIANCE = auto()
    PEACE = auto()
    TENSION = auto()
    WAR = auto()


@dataclass
class War:
    id: int
    side_a: Kingdom
    side_b: Kingdom
    start_month: int
    name: str = ""  # "The War of X"
    cause: str = ""  # border friction / site dispute
    end_month: int | None = None
    victor: Kingdom | None = None
    battles: int = 0
    total_dead: int = 0
    battle_sites: list[tuple[int, int]] = field(default_factory=list)

    @property
    def is_over(self) -> bool:
        return self.end_month is not None


@dataclass
class Beast:
    """A lone monster (races with form='site', e.g. Giants) roaming the world.

    Beasts have no government and no diplomacy: they roam, and civilizations
    near them either hunt them or suffer their raids.
    """

    id: int
    name: str
    race: Race
    x: int
    y: int
    hp: int
    power: int  # kills per raid
    aggression: int  # 0: passive grazer, 2: aggressive raider


# -- Chronicle -----------------------------------------------------------------

_EVENT_ID = itertools.count(1)


class EventType(IntEnum):
    WORLD_AGE = auto()
    KINGDOM_FOUNDED = auto()
    SITE_FOUNDED = auto()
    GOVERNMENT_CHANGED = auto()
    WAR_DECLARED = auto()
    BATTLE = auto()
    WAR_ENDED = auto()
    SITE_CAPTURED = auto()
    KINGDOM_COLLAPSED = auto()
    KINGDOM_ABSORBED = auto()
    LEADER_CROWNED = auto()
    LEADER_DIED = auto()
    HERO_EMERGED = auto()
    HERO_FELL = auto()
    BEAST_SIGHTED = auto()
    BEAST_SLAIN = auto()
    BEAST_RAMPAGE = auto()
    ALLIANCE_FORMED = auto()
    ALLIANCE_JOINED_WAR = auto()


@dataclass(frozen=True)
class Event:
    id: int
    month: int
    type: EventType
    text: str
    kingdom_ids: tuple[int, ...] = ()

    @classmethod
    def make(
        cls,
        month: int,
        type_: EventType,
        text: str,
        kingdom_ids: tuple[int, ...] = (),
    ) -> Event:
        return cls(id=next(_EVENT_ID), month=month, type=type_, text=text, kingdom_ids=kingdom_ids)


@dataclass
class Chronicle:
    """Append-only historical log (GDD §7)."""

    events: list[Event] = field(default_factory=list)

    def log(
        self,
        month: int,
        type_: EventType,
        text: str,
        kingdom_ids: tuple[int, ...] = (),
    ) -> Event:
        e = Event.make(month, type_, text, kingdom_ids)
        self.events.append(e)
        return e

    def recent(self, n: int = 50) -> list[Event]:
        return self.events[-n:]

    def of_kingdom(self, kingdom_id: int) -> list[Event]:
        return [e for e in self.events if kingdom_id in e.kingdom_ids]


__all__ = [
    "SocialForm",
    "Race",
    "GovernmentType",
    "TRIBAL_GOVERNMENT",
    "Site",
    "Leader",
    "Hero",
    "Kingdom",
    "Relation",
    "War",
    "Beast",
    "Event",
    "EventType",
    "Chronicle",
]
