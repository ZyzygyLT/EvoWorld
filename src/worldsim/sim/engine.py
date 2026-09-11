"""Headless simulation engine (GDD §2 loop).

Port of ProcessCivs population/expansion/war-trigger logic, extended per GDD:
  * wars are actually RESOLVED (aggregate battle resolution, site capture)
  * relations are a 4-level spectrum (alliance/peace/tension/war)
  * governments can shift under pressure (war exhaustion, prosperity)
  * every notable change is logged to the Chronicle
"""

from __future__ import annotations

import math

import numpy as np

from worldsim.data.namegen import NameGenerator
from worldsim.sim.models import (
    TRIBAL_GOVERNMENT,
    Chronicle,
    EventType,
    Kingdom,
    Race,
    Relation,
    Site,
    War,
)
from worldsim.worldgen import SEA_LEVEL, World

EXPANSION_DISTANCE = 10
WAR_DISTANCE = 20  # original was 8, which kings never reached; 20 yields real contact
MAX_SITES = 20
CIVILIZED_CIVS = 2
TRIBAL_CIVS = 2
WAR_DURATION_MIN = 12  # months
WAR_DURATION_MAX = 60  # months
GOV_SHIFT_MIN_YEARS = 10  # stability floor before government can change


def manhattan(ax: int, ay: int, bx: int, by: int) -> int:
    return abs(bx - ax) + abs(by - ay)


class SimEngine:
    """One world + kingdoms ticking month by month."""

    def __init__(
        self,
        world: World,
        races: list[Race],
        governments: list,
        rng: np.random.Generator | None = None,
        seed: int = 0,
        n_civilized: int = CIVILIZED_CIVS,
        n_tribal: int = TRIBAL_CIVS,
    ) -> None:
        self.world = world
        self.races = races
        self.governments = governments
        self.rng = rng or np.random.default_rng(seed)
        self.namegen = NameGenerator(self.rng)
        self.month: int = 0
        self.chronicle = Chronicle()
        self.kingdoms: list[Kingdom] = []
        self.wars: list[War] = []
        self.relations: dict[tuple[int, int], Relation] = {}
        self._occupancy = np.zeros(world.height.shape, dtype=np.int16)  # kingdom id +1
        self._next_war_id = 1
        self._spawn_kingdoms(n_civilized, n_tribal)

    # -- setup ----------------------------------------------------------------

    def _suitable_tiles(self, race: Race) -> list[tuple[int, int]]:
        w = self.world
        mask = np.isin(w.biome, race.preferred_biomes) & (w.height > SEA_LEVEL)
        ys, xs = np.nonzero(mask)
        return list(zip(xs.tolist(), ys.tolist(), strict=True))

    def _spawn_kingdoms(self, n_civilized: int, n_tribal: int) -> None:
        for i in range(n_civilized):
            civ_races = [r for r in self.races if r.form == "civilized"]
            race = self.rng.choice(civ_races)
            gov = self.governments[int(self.rng.integers(0, len(self.governments)))]
            self._found_kingdom(race, gov, f"#{i + 1}")
        for i in range(n_tribal):
            tribal_races = [r for r in self.races if r.form == "tribal"]
            race = self.rng.choice(tribal_races)
            self._found_kingdom(race, TRIBAL_GOVERNMENT, f"#{i + 1}")

    def _found_kingdom(self, race: Race, gov, tag: str) -> Kingdom | None:
        candidates = self._suitable_tiles(race)
        if not candidates:
            return None
        free = [(x, y) for x, y in candidates if self._occupancy[y, x] == 0]
        if not free:
            return None
        x, y = free[int(self.rng.integers(0, len(free)))]
        k = Kingdom(
            id=len(self.kingdoms),
            name=self.namegen.kingdom_name(race.form),
            race=race,
            government=gov,
            color=tuple(int(c) for c in self.rng.integers(60, 256, size=3)),
        )
        site = self._make_site(k, x, y, is_capital=True)
        k.sites.append(site)
        self.kingdoms.append(k)
        self._occupancy[y, x] = k.id + 1
        self.chronicle.log(
            self.month,
            EventType.KINGDOM_FOUNDED,
            f"{k.name} ({race.name}, {gov.name}) founded its capital {site.name}.",
            (k.id,),
        )
        return k

    def _make_site(self, k: Kingdom, x: int, y: int, is_capital: bool = False) -> Site:
        w = self.world
        prosper = w.prosperity[y, x]
        # original: PopCap = 4 * ReproductionSpeed + Prosperity*150 (x1.5 river, x2 capital)
        popcap = 4 * k.race.reproduction + prosper * 150
        if w.rivers[y, x]:
            popcap *= 1.5
        if is_capital:
            popcap *= 2
        category = "Capital" if is_capital else "Village"
        return Site(
            x=x,
            y=y,
            name=self.namegen.site_name(),
            category=category,
            popcap=math.floor(popcap),
            is_capital=is_capital,
            population=20,
        )

    # -- relations ------------------------------------------------------------

    def _rel_key(self, a: Kingdom, b: Kingdom) -> tuple[int, int]:
        return (min(a.id, b.id), max(a.id, b.id))

    def get_relation(self, a: Kingdom, b: Kingdom) -> Relation:
        return self.relations.get(self._rel_key(a, b), Relation.PEACE)

    def _set_relation(self, a: Kingdom, b: Kingdom, rel: Relation) -> None:
        self.relations[self._rel_key(a, b)] = rel

    # -- main loop ------------------------------------------------------------

    def tick(self) -> None:
        """Advance one month (GDD §2)."""
        self.month += 1
        for k in self.kingdoms:
            self._grow_kingdom(k)
        self._update_relations()
        self._resolve_wars()
        for k in self.kingdoms:
            self._maybe_shift_government(k)
        self._cleanup_collapsed()

    def _grow_kingdom(self, k: Kingdom) -> None:
        if not k.sites:
            return
        repro = k.race.reproduction
        k.total_population = 0
        if not k.at_war and k.war_exhaustion > 0:
            k.war_exhaustion = max(0, k.war_exhaustion - 1)  # heal in peace
        for site in k.sites:
            # original: NewPop = int(round(Pop * Repro / 1500)); halve above half popcap
            growth = site.population * repro / 1500
            if site.population > site.popcap / 2:
                growth /= 6  # overcrowding slows growth
            site.population = int(site.population + round(growth))
            site.update_category()

            # original clamps to popcap FIRST (unconditionally), then halves
            # and founds a new site only if below MAX_SITES
            if site.population > site.popcap:
                site.population = int(site.popcap)
                if len(k.sites) < MAX_SITES:
                    site.population = int(site.popcap / 2)
                    self._expand(k, site)

            k.total_population += site.population

        k.years_since_founding = self.month // 12

    def _expand(self, k: Kingdom, origin: Site) -> None:
        candidates = self._suitable_tiles(k.race)
        for _ in range(200):
            x, y = candidates[int(self.rng.integers(0, len(candidates)))]
            if self._occupancy[y, x]:
                continue
            if manhattan(origin.x, origin.y, x, y) > EXPANSION_DISTANCE:
                continue
            site = self._make_site(k, x, y)
            k.sites.append(site)
            self._occupancy[y, x] = k.id + 1
            self.chronicle.log(
                self.month,
                EventType.SITE_FOUNDED,
                f"{k.name} founded {site.name} ({site.category}).",
                (k.id,),
            )
            return

    # -- relations & war ------------------------------------------------------

    def _update_relations(self) -> None:
        """Scan neighbor kingdoms; degrade relations toward tension/war (GDD §6)."""
        active = [k for k in self.kingdoms if k.sites]
        for i, a in enumerate(active):
            for b in active[i + 1 :]:
                current = self.get_relation(a, b)
                if current == Relation.WAR:
                    continue
                # contact?
                contact = False
                for sa in a.sites:
                    for sb in b.sites:
                        if manhattan(sa.x, sa.y, sb.x, sb.y) < WAR_DISTANCE:
                            contact = True
                            break
                    if contact:
                        break
                if not contact:
                    # drift back to peace when far apart
                    if current == Relation.TENSION:
                        self._set_relation(a, b, Relation.PEACE)
                    continue
                # in contact: aggression drives the spectrum
                combined_aggr = a.aggression + b.aggression
                if current == Relation.ALLIANCE:
                    continue  # alliances persist (kept simple for v1)
                roll = self.rng.random()
                if combined_aggr >= 4 and roll < 0.10:
                    self._declare_war(a, b)
                elif combined_aggr >= 3 and roll < 0.08:
                    self._set_relation(a, b, Relation.TENSION)
                elif roll < 0.02:
                    self._set_relation(a, b, Relation.ALLIANCE)
                else:
                    self._set_relation(a, b, Relation.TENSION if roll < 0.5 else Relation.PEACE)

    def _declare_war(self, a: Kingdom, b: Kingdom) -> None:
        war = War(
            id=self._next_war_id,
            side_a=a,
            side_b=b,
            start_month=self.month,
        )
        self._next_war_id += 1
        self.wars.append(war)
        self._set_relation(a, b, Relation.WAR)
        a.at_war = True
        b.at_war = True
        self.chronicle.log(
            self.month,
            EventType.WAR_DECLARED,
            f"War declared between {a.name} and {b.name}!",
            (a.id, b.id),
        )

    def _resolve_wars(self) -> None:
        """Resolve ongoing wars by aggregate strength; capture sites (GDD §6)."""
        for war in self.wars:
            if war.is_over:
                continue
            a, b = war.side_a, war.side_b
            if not a.sites or not b.sites:
                war.end_month = self.month
                war.victor = a if a.sites else b
                self._end_war(war)
                continue
            war.battles += 1
            # armies = militarization % of population, seasoned by race strength
            pow_a = a.total_population * a.government.militarization / 100 * a.race.strength
            pow_b = b.total_population * b.government.militarization / 100 * b.race.strength
            a.army_size = pow_a
            b.army_size = pow_b
            total = pow_a + pow_b
            if total <= 0:
                war.end_month = self.month
                self._end_war(war)
                continue
            p_a_wins = pow_a / total
            if self.rng.random() < 0.5:  # battle happens this month
                if self.rng.random() < p_a_wins:
                    self._battle_outcome(war, a, b)
                else:
                    self._battle_outcome(war, b, a)
            war_duration = self.month - war.start_month
            a.war_exhaustion += 1
            b.war_exhaustion += 1
            if war_duration > WAR_DURATION_MAX:
                # white peace after too long
                war.end_month = self.month
                self.chronicle.log(
                    self.month,
                    EventType.WAR_ENDED,
                    f"The war between {a.name} and {b.name} ended in white peace.",
                    (a.id, b.id),
                )
                self._end_war(war)

    def _end_war(self, war: War) -> None:
        """Clear at_war flags when a war closes (both sides, or survivor if one died)."""
        war.side_a.at_war = any(
            w is not war and not w.is_over and war.side_a in (w.side_a, w.side_b)
            for w in self.wars
        )
        war.side_b.at_war = any(
            w is not war and not w.is_over and war.side_b in (w.side_a, w.side_b)
            for w in self.wars
        )

    def _battle_outcome(self, war: War, winner: Kingdom, loser: Kingdom) -> None:
        # loser loses 5-15% population, a border site may flip
        loss = max(1, int(loser.total_population * self.rng.uniform(0.05, 0.15)))
        self._apply_population_loss(loser, loss)
        self.chronicle.log(
            self.month,
            EventType.BATTLE,
            f"{winner.name} defeated {loser.name} in battle ({loss} dead).",
            (winner.id, loser.id),
        )
        if len(loser.sites) > 1 and self.rng.random() < 0.35:
            site = loser.sites[int(self.rng.integers(0, len(loser.sites)))]
            while site.is_capital and len(loser.sites) > 1:
                site = loser.sites[int(self.rng.integers(0, len(loser.sites)))]
            if not site.is_capital:
                loser.sites.remove(site)
                self._occupancy[site.y, site.x] = 0
                if len(winner.sites) < MAX_SITES:
                    winner.sites.append(site)
                    self._occupancy[site.y, site.x] = winner.id + 1
                    self.chronicle.log(
                        self.month,
                        EventType.SITE_CAPTURED,
                        f"{winner.name} captured {site.name} from {loser.name}.",
                        (winner.id, loser.id),
                    )
                else:
                    self.chronicle.log(
                        self.month,
                        EventType.SITE_CAPTURED,
                        f"{winner.name} razed {site.name} — {loser.name} lost the settlement.",
                        (winner.id, loser.id),
                    )
        if not loser.sites or loser.total_population < 10 or (
            war.battles > 3 and self.rng.random() < 0.15
        ):
            war.end_month = self.month
            war.victor = winner
            self.chronicle.log(
                self.month,
                EventType.WAR_ENDED,
                f"{winner.name} won the war against {loser.name}.",
                (winner.id, loser.id),
            )
            self._end_war(war)

    def _apply_population_loss(self, k: Kingdom, loss: int) -> None:
        remaining = loss
        for site in k.sites:
            if remaining <= 0:
                break
            take = min(site.population, remaining)
            site.population -= take
            remaining -= take
        k.total_population = sum(s.population for s in k.sites)

    def _cleanup_collapsed(self) -> None:
        for k in self.kingdoms:
            if k.sites or k.total_population > 0:
                continue
            already_dead = getattr(k, "_collapsed", False)
            if already_dead:
                continue
            k._collapsed = True  # type: ignore[attr-defined]
            self.chronicle.log(
                self.month,
                EventType.KINGDOM_COLLAPSED,
                f"{k.name} has collapsed and is no more.",
                (k.id,),
            )
            # end its wars
            for war in self.wars:
                if not war.is_over and k in (war.side_a, war.side_b):
                    other = war.side_b if war.side_a is k else war.side_a
                    war.end_month = self.month
                    war.victor = other
                    self._end_war(war)

    # -- government evolution (GDD §4) -----------------------------------------

    def _maybe_shift_government(self, k: Kingdom) -> None:
        if k.race.form == "tribal" or not k.sites:
            return
        if k.years_since_founding < GOV_SHIFT_MIN_YEARS:
            return
        if self.month % 12 != 0:  # evaluate yearly
            return
        civ_govs = [g for g in self.governments if g is not TRIBAL_GOVERNMENT]
        # prolonged losing war -> military dictatorship
        losing_war = any(
            not w.is_over
            and k in (w.side_a, w.side_b)
            and w.battles > 6
            and (w.victor is not k)  # victor None while ongoing
            and (
                k.total_population
                < (w.side_b if w.side_a is k else w.side_a).total_population * 0.5
            )
            for w in self.wars
        )
        if losing_war and k.government.name != "Military Dictatorship":
            new_gov = next(g for g in civ_govs if g.name == "Military Dictatorship")
            self._change_government(k, new_gov, "amid a losing war")
            return
        # high prosperity + size + peace -> democracy / technocracy
        prosperous = sum(s.popcap for s in k.sites) > 800 and not k.at_war
        if prosperous and k.government.name not in ("Democracy", "Technocracy"):
            options = [g for g in civ_govs if g.name in ("Democracy", "Technocracy")]
            choice = options[int(self.rng.integers(0, len(options)))]
            self._change_government(k, choice, "in a golden age")
            return
        # stagnant authoritarian regime under long peace -> constitutional monarchy
        long_peace = not k.at_war and k.war_exhaustion == 0 and self.month > 24 * 12
        is_absolute_monarchy = k.government.name == "Absolute Monarchy"
        if long_peace and is_absolute_monarchy:
            new_gov = next(g for g in civ_govs if g.name == "Constitutional monarchy")
            self._change_government(k, new_gov, "after decades of peace")

    def _change_government(self, k: Kingdom, new_gov, reason: str) -> None:
        old = k.government.name
        k.government = new_gov
        k.war_exhaustion = 0
        self.chronicle.log(
            self.month,
            EventType.GOVERNMENT_CHANGED,
            f"{k.name} shifted from {old} to {new_gov.name} {reason}.",
            (k.id,),
        )

    # -- queries ---------------------------------------------------------------

    def territory_map(self) -> np.ndarray:
        """(H, W) kingdom id (-1 none) for political overlay."""
        return self._occupancy - 1

    def summary(self) -> str:
        lines = [f"Month {self.month} ({self.month // 12}y) — {len(self.kingdoms)} kingdoms"]
        for k in self.kingdoms:
            wars = "AT WAR" if k.at_war else "peace"
            lines.append(
                f"  {k.name:<24} {k.race.name:<7} {k.government.name:<24} "
                f"pop={k.total_population:<6} sites={len(k.sites):<3} {wars}"
            )
        return "\n".join(lines)
