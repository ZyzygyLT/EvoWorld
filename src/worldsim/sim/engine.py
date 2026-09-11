"""Headless simulation engine (GDD §2 loop).

Port of ProcessCivs population/expansion/war-trigger logic, extended per GDD:
  * wars are actually RESOLVED (aggregate battle resolution, site capture)
  * relations are a 4-level spectrum (alliance/peace/tension/war)
  * governments can shift under pressure (war exhaustion, prosperity)
  * every notable change is logged to the Chronicle
  * leaders succeed on death; heroes emerge from battle; beasts (races with
    form='site') roam, raid and are hunted by civilizations
"""

from __future__ import annotations

import math

import numpy as np

from worldsim.data.namegen import NameGenerator
from worldsim.sim.models import (
    TRIBAL_GOVERNMENT,
    Beast,
    Chronicle,
    EventType,
    Hero,
    Kingdom,
    Leader,
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
WAR_DURATION_MAX = 60  # months
GOV_SHIFT_MIN_YEARS = 10  # stability floor before government can change

# leaders
LEADER_LIFESPAN_MEAN = 40 * 12  # months (40 years ruling)
LEADER_LIFESPAN_STD = 10 * 12
# heroes
HERO_EMERGE_CHANCE = 0.02  # per battle won
HERO_DEATH_CHANCE = 0.08  # per battle fought (win or lose)
# beasts
N_BEASTS = 2
BEAST_HUNT_POWER = 150  # population-power needed to slay one beast unit
BEAST_RAID_RANGE = 3  # manhattan distance to hit a site


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
        n_beasts: int = N_BEASTS,
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
        self.beasts: list[Beast] = []
        self.relations: dict[tuple[int, int], Relation] = {}
        self._occupancy = np.zeros(world.height.shape, dtype=np.int16)  # kingdom id +1
        self._battle_heat = np.zeros(world.height.shape, dtype=np.float32)
        self._next_war_id = 1
        self._next_leader_id = 1
        self._next_hero_id = 1
        self._next_beast_id = 1
        self._spawn_kingdoms(n_civilized, n_tribal)
        self._spawn_beasts(n_beasts)

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
        self._crown_leader(k)
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

    def _spawn_beasts(self, n_beasts: int) -> None:
        """Spawn lone monsters from races with form='site' (e.g. Giants)."""
        beast_races = [r for r in self.races if r.form == "site"]
        if not beast_races:
            return
        for _ in range(n_beasts):
            race = beast_races[int(self.rng.integers(0, len(beast_races)))]
            candidates = self._suitable_tiles(race)
            if not candidates:
                return
            x, y = candidates[int(self.rng.integers(0, len(candidates)))]
            name = f"{self.namegen.name()} the {race.name}"
            beast = Beast(
                id=self._next_beast_id,
                name=name,
                race=race,
                x=x,
                y=y,
                hp=race.size * 3,
                power=race.strength * 5,
                aggression=race.aggressiveness,
            )
            self._next_beast_id += 1
            self.beasts.append(beast)
            self.chronicle.log(
                self.month,
                EventType.BEAST_SIGHTED,
                f"A monster was sighted: {name} roams the wilds.",
            )

    # -- leaders & heroes -------------------------------------------------------

    def _crown_leader(self, k: Kingdom) -> None:
        title = "Chief" if k.race.form == "tribal" else "King"
        leader = Leader(
            id=self._next_leader_id,
            name=self.namegen.name(),
            kingdom_id=k.id,
            title=title,
            born_month=self.month,
        )
        self._next_leader_id += 1
        k.leader = leader
        self.chronicle.log(
            self.month,
            EventType.LEADER_CROWNED,
            f"{leader.name} becomes {title} of {k.name}.",
            (k.id,),
        )

    def _succession(self, k: Kingdom) -> None:
        """Kill aged leaders and crown heirs."""
        leader = k.leader
        if leader is None or leader.died_month is not None:
            return
        lifespan = float(self.rng.normal(LEADER_LIFESPAN_MEAN, LEADER_LIFESPAN_STD))
        if lifespan < 12:
            lifespan = 12
        if self.month - leader.born_month >= lifespan:
            leader.died_month = self.month
            self.chronicle.log(
                self.month,
                EventType.LEADER_DIED,
                f"{leader.title} {leader.name} of {k.name} died at age "
                f"{int(leader.age_years)} after {leader.battles_won} victories.",
                (k.id,),
            )
            self._crown_leader(k)

    def _maybe_emerge_hero(self, k: Kingdom) -> None:
        """Battle-hardened kingdoms sometimes produce a champion."""
        if k.hero is not None:
            return
        if self.rng.random() < HERO_EMERGE_CHANCE and k.at_war:
            hero = Hero(
                id=self._next_hero_id,
                name=self.namegen.name(),
                kingdom_id=k.id,
                strength=Hero.roll_strength(self.rng),
            )
            self._next_hero_id += 1
            k.hero = hero
            self.chronicle.log(
                self.month,
                EventType.HERO_EMERGED,
                f"The champion {hero.name} rises in {k.name} "
                f"(battle strength x{hero.strength:.1f}).",
                (k.id,),
            )

    def _hero_risk(self, k: Kingdom, won: bool) -> None:
        """Heroes can fall in battle; winners gain deeds."""
        hero = k.hero
        if hero is None:
            return
        if won:
            hero.battle_deeds.append(f"victory at month {self.month}")
        if self.rng.random() < HERO_DEATH_CHANCE:
            k.hero = None
            self.chronicle.log(
                self.month,
                EventType.HERO_FELL,
                f"The champion {hero.name} of {k.name} fell in battle after "
                f"{len(hero.battle_deeds)} deeds.",
                (k.id,),
            )

    # -- beasts ------------------------------------------------------------------

    def _process_beasts(self) -> None:
        """Beasts roam; aggressive ones raid nearby sites; kingdoms hunt them."""
        h, w = self.world.height.shape
        for beast in self.beasts:
            if beast.hp <= 0:
                continue
            # roam one step
            dx, dy = self.rng.integers(-1, 2, size=2).tolist()
            nx, ny = beast.x + dx, beast.y + dy
            if 0 <= nx < w and 0 <= ny < h and self.world.height[ny, nx] > SEA_LEVEL:
                beast.x, beast.y = nx, ny

            # nearest kingdom site in raid range
            target = None
            target_k = None
            for k in self.kingdoms:
                for s in k.sites:
                    if manhattan(beast.x, beast.y, s.x, s.y) <= BEAST_RAID_RANGE:
                        target, target_k = s, k
                        break
                if target:
                    break
            if target is None:
                continue

            # hunt or raid, by relative power
            hunt_power = target_k.total_population * target_k.government.militarization / 100
            if beast.aggression == 0 or hunt_power > BEAST_HUNT_POWER:
                # kingdom slays the beast (maybe)
                if self.rng.random() < 0.15:
                    beast.hp = 0
                    self.chronicle.log(
                        self.month,
                        EventType.BEAST_SLAIN,
                        f"Hunters of {target_k.name} slew the monster {beast.name} "
                        f"near {target.name}.",
                        (target_k.id,),
                    )
            elif beast.aggression >= 1:
                # raid: losses at the nearest site
                dead = max(1, int(beast.power * self.rng.uniform(0.5, 1.5)))
                target.population = max(0, target.population - dead)
                self._battle_heat[target.y, target.x] += dead
                self.chronicle.log(
                    self.month,
                    EventType.BEAST_RAMPAGE,
                    f"The monster {beast.name} ravaged {target.name} "
                    f"({dead} dead).",
                    (target_k.id,),
                )

    # -- relations ------------------------------------------------------------

    def _rel_key(self, a: Kingdom, b: Kingdom) -> tuple[int, int]:
        return (min(a.id, b.id), max(a.id, b.id))

    def get_relation(self, a: Kingdom, b: Kingdom) -> Relation:
        return self.relations.get(self._rel_key(a, b), Relation.PEACE)

    def _set_relation(self, a: Kingdom, b: Kingdom, rel: Relation) -> None:
        self.relations[self._rel_key(a, b)] = rel

    def _allies_of(self, k: Kingdom) -> list[Kingdom]:
        out = []
        for other in self.kingdoms:
            if other is k:
                continue
            if self.get_relation(k, other) == Relation.ALLIANCE:
                out.append(other)
        return out

    # -- main loop ------------------------------------------------------------

    def tick(self) -> None:
        """Advance one month (GDD §2)."""
        self.month += 1
        for k in self.kingdoms:
            self._grow_kingdom(k)
            self._succession(k)
        self._process_beasts()
        self._update_relations()
        self._resolve_wars()
        for k in self.kingdoms:
            self._maybe_shift_government(k)
        self._cleanup_collapsed()
        self._sample_history()

    def _sample_history(self) -> None:
        if self.month % 12 == 0:
            for k in self.kingdoms:
                k.population_history.append(k.total_population)

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
                    continue  # alliances persist while in contact
                roll = self.rng.random()
                if combined_aggr >= 4 and roll < 0.10:
                    self._declare_war(a, b)
                elif combined_aggr >= 3 and roll < 0.08:
                    self._set_relation(a, b, Relation.TENSION)
                elif roll < 0.02:
                    self._set_relation(a, b, Relation.ALLIANCE)
                    self.chronicle.log(
                        self.month,
                        EventType.ALLIANCE_FORMED,
                        f"{a.name} and {b.name} sealed an alliance.",
                        (a.id, b.id),
                    )
                else:
                    self._set_relation(a, b, Relation.TENSION if roll < 0.5 else Relation.PEACE)

    def _war_name(self, a: Kingdom, b: Kingdom) -> str:
        """Named wars: 'The War of <site>' if a capture follows later, else by leaders."""
        if a.leader and b.leader:
            return f"The War of {a.leader.name}'s Fall" if self.rng.random() < 0.5 else (
                f"The War of {b.leader.name}'s Wrath"
            )
        return f"The {a.name}-{b.name} War"

    def _declare_war(self, a: Kingdom, b: Kingdom) -> None:
        war = War(
            id=self._next_war_id,
            side_a=a,
            side_b=b,
            start_month=self.month,
            name=self._war_name(a, b),
            cause="border friction" if self.rng.random() < 0.7 else "disputed lands",
        )
        self._next_war_id += 1
        self.wars.append(war)
        self._set_relation(a, b, Relation.WAR)
        a.at_war = True
        b.at_war = True
        self.chronicle.log(
            self.month,
            EventType.WAR_DECLARED,
            f"{war.name} began: {a.name} and {b.name} clash over {war.cause}!",
            (a.id, b.id),
        )
        # cobeligerant allies join the fight (GDD: active alliances)
        for ally in self._allies_of(a):
            if ally is not b and not ally.at_war:
                self._join_war(war, ally, b)
        for ally in self._allies_of(b):
            if ally is not a and not ally.at_war:
                self._join_war(war, ally, a)

    def _join_war(self, war: War, joiner: Kingdom, enemy: Kingdom) -> None:
        joiner.at_war = True
        self._set_relation(joiner, enemy, Relation.WAR)
        # joiner contributes via its own new war against the enemy
        joined = War(
            id=self._next_war_id,
            side_a=joiner,
            side_b=enemy,
            start_month=self.month,
            name=f"The {war.name} (allied front)",
            cause=f"honoring alliance in {war.name}",
        )
        self._next_war_id += 1
        self.wars.append(joined)
        self.chronicle.log(
            self.month,
            EventType.ALLIANCE_JOINED_WAR,
            f"{joiner.name} joined {war.name} against {enemy.name}, honoring its alliance.",
            (joiner.id, enemy.id),
        )

    def _resolve_wars(self) -> None:
        """Resolve ongoing wars by aggregate strength; capture sites (GDD §6)."""
        for war in list(self.wars):
            if war.is_over:
                continue
            a, b = war.side_a, war.side_b
            if not a.sites or not b.sites:
                war.end_month = self.month
                war.victor = a if a.sites else b
                self._log_war_end(war)
                self._end_war(war)
                continue
            war.battles += 1
            # armies = militarization % of population, seasoned by race strength
            pow_a = a.total_population * a.government.militarization / 100 * a.race.strength
            pow_b = b.total_population * b.government.militarization / 100 * b.race.strength
            if a.hero:
                pow_a *= a.hero.strength
            if b.hero:
                pow_b *= b.hero.strength
            a.army_size = pow_a
            b.army_size = pow_b
            total = pow_a + pow_b
            if total <= 0:
                war.end_month = self.month
                self._end_war(war)
                continue
            p_a_wins = pow_a / total
            if self.rng.random() < 0.5:  # battle happens this month
                winner, loser = (a, b) if self.rng.random() < p_a_wins else (b, a)
                self._battle_outcome(war, winner, loser)
            else:
                # no field battle this month: heroes still risk skirmishes
                if a.hero:
                    self._hero_risk(a, won=False)
                if b.hero:
                    self._hero_risk(b, won=False)
            war_duration = self.month - war.start_month
            a.war_exhaustion += 1
            b.war_exhaustion += 1
            self._maybe_emerge_hero(a)
            self._maybe_emerge_hero(b)
            if war_duration > WAR_DURATION_MAX:
                # white peace after too long
                war.end_month = self.month
                self.chronicle.log(
                    self.month,
                    EventType.WAR_ENDED,
                    f"{war.name} ended in white peace between {a.name} and {b.name}.",
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

    def _log_war_end(self, war: War, extra: str = "") -> None:
        v = war.victor
        text = (
            f"{v.name} won {war.name} ({war.total_dead} dead over "
            f"{(war.end_month or self.month) - war.start_month} months)."
            if v
            else f"{war.name} ended."
        )
        if extra:
            text += " " + extra
        self.chronicle.log(
            self.month,
            EventType.WAR_ENDED,
            text,
            (war.side_a.id, war.side_b.id),
        )

    def _battle_outcome(self, war: War, winner: Kingdom, loser: Kingdom) -> None:
        # loser loses 5-15% population, a border site may flip
        loss = max(1, int(loser.total_population * self.rng.uniform(0.05, 0.15)))
        self._apply_population_loss(loser, loss)
        war.total_dead += loss

        # battle location: near a loser site (for the heatmap)
        if loser.sites:
            site = loser.sites[int(self.rng.integers(0, len(loser.sites)))]
            war.battle_sites.append((site.x, site.y))
            self._battle_heat[site.y, site.x] += loss

        # leader/hero bookkeeping
        if winner.leader:
            winner.leader.battles_won += 1
        if loser.leader:
            loser.leader.battles_lost += 1
        self._hero_risk(winner, won=True)
        self._hero_risk(loser, won=False)

        self.chronicle.log(
            self.month,
            EventType.BATTLE,
            f"{winner.name} defeated {loser.name} in battle ({loss} dead)"
            + (f" — champion {winner.hero.name} led the charge!" if winner.hero else "")
            + ".",
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
            self._log_war_end(war)
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

    def battle_heat(self) -> np.ndarray:
        """(H, W) accumulated battle deaths — where history was written."""
        return self._battle_heat

    def summary(self) -> str:
        lines = [f"Month {self.month} ({self.month // 12}y) — {len(self.kingdoms)} kingdoms"]
        for k in self.kingdoms:
            wars = "AT WAR" if k.at_war else "peace"
            leader = f" [{k.leader.title} {k.leader.name}]" if k.leader else ""
            hero = f" <champion {k.hero.name}>" if k.hero else ""
            lines.append(
                f"  {k.name:<24} {k.race.name:<7} {k.government.name:<24} "
                f"pop={k.total_population:<6} sites={len(k.sites):<3} {wars}{leader}{hero}"
            )
        alive = [b for b in self.beasts if b.hp > 0]
        if alive:
            lines.append(f"  beasts roaming: {len(alive)} ({', '.join(b.name for b in alive)})")
        return "\n".join(lines)
