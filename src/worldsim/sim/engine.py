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
from worldsim.i18n import render as _render_msg
from worldsim.sim.models import (
    TRIBAL_GOVERNMENT,
    Beast,
    Chronicle,
    EventType,
    Hero,
    Kingdom,
    Leader,
    Notable,
    Race,
    Relation,
    Site,
    War,
)
from worldsim.sim.tech import (
    GOV_BRANCH_AFFINITY,
    TECHS,
    available_techs,
)
from worldsim.worldgen import World

EXPANSION_DISTANCE = 10
WAR_DISTANCE = 28  # contact radius for diplomacy; 20 left kingdoms in static peace
MAX_SITES = 20
CIVILIZED_CIVS = 2
TRIBAL_CIVS = 2
WAR_DURATION_MAX = 60  # months
GOV_SHIFT_MIN_YEARS = 10  # stability floor before government can change
GOV_SHIFT_COOLDOWN_YEARS = 25  # no golden-age flip-flopping between shifts
EXPANSIONIST_WAR_CHANCE = 0.006  # per month per saturated kingdom pair in contact

# leaders
LEADER_REIGN_MEAN = 40 * 12  # months
LEADER_REIGN_STD = 10 * 12
LEADER_REIGN_MIN = 5 * 12
# heroes
HERO_EMERGE_CHANCE = 0.02  # per battle won
HERO_DEATH_CHANCE = 0.04  # per battle fought (win or lose) — halved for balance
# beasts
N_BEASTS = 2
BEAST_HUNT_POWER = 150  # population-power needed to slay one beast unit
BEAST_RAID_RANGE = 3  # manhattan distance to hit a site
BEAST_TERRITORY_RADIUS = 6  # giants claim land: no new sites may be founded inside

# rebellions
REBELLION_MIN_SITES = 8  # big realms can split
REBELLION_CHANCE_MONTHLY = 0.0008  # per month once strained (runaway guard below)
MAX_KINGDOMS = 12  # hard cap: rebellions stop splitting at this count

# tech eras (the world starts in the Stone Age and advances over centuries)
ERA_NAMES = ["Stone Age", "Bronze Age", "Iron Age", "Classical Era", "Medieval Era", "Renaissance", "Enlightenment"]
ERA_POPCAP_BONUS = 0.25  # +25% site popcap per era (urbanization)
ERA_ARMY_BONUS = 0.15  # +15% military power per era
ERA_INFLUENCE_RADIUS = 4  # base claim radius at Stone Age ...
ERA_INFLUENCE_PER_LEVEL = 2  # ... +2 tiles per era
TECH_BASE_YEARS_PER_ERA = 90  # legacy constant (see _progress_tech formula)
TECH_GOLDEN_AGE_YEARS = 25  # golden age duration
TECH_GOLDEN_AGE_CHANCE = 0.0015  # monthly chance while prosperous
PLAGUE_MIN_DENSITY_POP = 2000  # plagues strike earlier (was 8000)
PLAGUE_CHANCE_MONTHLY = 0.0004
PLAGUE_MORTALITY = (0.10, 0.30)

# natural disasters (see _maybe_disasters)
DROUGHT_CHANCE = 0.0006        # monthly per kingdom — withers crops
DROUGHT_DURATION = (6, 18)     # months
DROUGHT_MORTALITY = (0.02, 0.06)
FLOOD_CHANCE = 0.0004          # monthly per riverside kingdom
FLOOD_MORTALITY = (0.03, 0.08)
FAMINE_CHANCE = 0.0005         # monthly per kingdom after drought/overcrowding
FAMINE_MORTALITY = (0.05, 0.15)
EARTHQUAKE_CHANCE = 0.0002     # monthly per kingdom — rare, devastating
EARTHQUAKE_MORTALITY = (0.08, 0.25)

# happiness (see _update_happiness)
GOV_HAPPINESS: dict[str, float] = {
    "Tribal": 0.0,
    "Absolute Monarchy": -2.0,
    "Theocracy": 1.0,
    "Military Dictatorship": -10.0,
    "Constitutional monarchy": 6.0,
    "Plutocracy": 4.0,
    "Fanatic Theocracy": -6.0,
    "Democracy": 10.0,
    "Technocracy": 5.0,
}

# migration (see _maybe_migration)
MIGRATION_CHANCE = 0.04        # per 3-month check when conditions met
MIGRATION_SHARE = (0.02, 0.05)  # % of source pop that moves
MIGRATION_MIN_POP = 50        # need at least this many to send migrants

# faith & religion (see _update_faith / temples)
FAITHS = ("Animism", "Polytheism", "Monotheism", "Skepticism")
_FAITH_POS = {"Animism": 0, "Polytheism": 1, "Monotheism": 2, "Skepticism": 3}
FAITH_TEMPLE_CHANCE = 0.06    # yearly per-kingdom chance to raise a temple
FAITH_TEMPLE_HAPPINESS = 3.0  # flat happiness per temple
FAITH_TEMPLE_GROWTH = 0.02    # +2% growth per temple
FAITH_CONVERSION_CHANCE = 0.06  # yearly per exposed realm
FAITH_TENSION_WAR_BONUS = 0.03  # extra war chance when faiths distant (× distance)
FAITH_TENSION_TRADE_PENALTY = 0.12  # trade form chance penalty per distance
HOLY_WAR_HERO_CHANCE = 0.35   # zealot heroes rise faster in holy wars
HOLY_WAR_ARMY_BONUS = 0.08    # attacker zeal multiplier when faiths oppose
# theological distance (linear chain Animism–Polytheism–Monotheism–Skepticism), capped at 2
def _faith_distance(a: str, b: str) -> int:
    return min(2, abs(_FAITH_POS.get(a, 0) - _FAITH_POS.get(b, 0)))

# trade (see _update_trade)
TRADE_FORM_CHANCE = 0.22      # yearly chance for a qualifying pair to open a route
TRADE_BONUS_HAPPINESS = 4.0   # flat happiness bonus per active route
TRADE_BONUS_GROWTH = 0.04     # +4% growth per route
TRADE_MIN_PROSPER = 0.45      # need avg prosperity above this to trade
TRADE_MAX_ROUTES = 2          # cap per kingdom (keeps network sparse)
COLONY_FOUNDED_MIN_SITES = 12  # saturated-ish realms found colonies
COLONY_DISTANCE = (15, 30)
COLONY_INDEPENCE_DISTANCE = 45  # colonies this far declare peaceful independence

# government availability by era: no Democracies at year zero.
# Every realm starts Tribal; institutions unlock as knowledge advances.
GOV_MIN_ERA = {
    "Tribal": 0,
    "Absolute Monarchy": 1,
    "Theocracy": 1,
    "Military Dictatorship": 1,
    "Constitutional monarchy": 2,
    "Plutocracy": 2,
    "Fanatic Theocracy": 2,
    "Democracy": 3,
    "Technocracy": 4,
}


def manhattan(ax: int, ay: int, bx: int, by: int) -> int:
    return abs(bx - ax) + abs(by - ay)


def _golden_angle_color(index: int) -> tuple[int, int, int]:
    """Distinct, readable kingdom colors via the golden-angle hue walk.

    Fixed S/L keeps every color legible on dark backgrounds (the old
    random RGB produced near-identical dark purples).
    """
    import colorsys

    hue = (index * 0.618033988749895) % 1.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.55, 0.55)
    return (int(r * 255), int(g * 255), int(b * 255))


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
        enabled_races: list[str] | None = None,
        kingdom_races: list[str] | None = None,
    ) -> None:
        self.world = world
        self.races = (
            [r for r in races if r.name in enabled_races] if enabled_races else list(races)
        )
        # a world needs at least one civilized + one tribal race to spawn
        has_civ = any(r.form == "civilized" for r in self.races)
        has_tribal = any(r.form == "tribal" for r in self.races)
        if not has_civ or not has_tribal:
            self.races = list(races)
        self.governments = governments
        self.rng = rng or np.random.default_rng(seed)
        self.namegen = NameGenerator(self.rng)
        self.month: int = 0
        self.chronicle = Chronicle()
        self.kingdoms: list[Kingdom] = []
        self.wars: list[War] = []
        self.beasts: list[Beast] = []
        self.relations: dict[tuple[int, int], Relation] = {}
        self._occupancy = np.zeros(world.height.shape, dtype=np.int32)  # kingdom id +1
        self._battle_heat = np.zeros(world.height.shape, dtype=np.float32)
        self._next_war_id = 1
        self._next_leader_id = 1
        self._next_hero_id = 1
        self._next_beast_id = 1
        self._next_notable_id = 1
        self._milestones: set[str] = set()  # each milestone fires exactly once
        self._world_techs: set[str] = set()  # techs discovered by anyone
        self.fallen: list[dict] = []  # memorial of collapsed realms
        self._color_counter = 0  # golden-angle hue assignment
        self._spawn_kingdoms(n_civilized, n_tribal, kingdom_races)
        self._spawn_beasts(n_beasts)

    # -- setup ----------------------------------------------------------------

    def _ev(
        self,
        type_,
        key: str,
        params: dict,
        kids: tuple[int, ...] = (),
    ) -> None:
        """Log a structured, i18n-renderable chronicle event.

        Text is rendered in English at log time (canonical stored text);
        clients re-render other languages from key+params.
        """
        self.chronicle.log(
            self.month, type_, _render_msg("en", key, params), kids, params, key
        )

    def _suitable_tiles(self, race: Race) -> list[tuple[int, int]]:
        w = self.world
        sl = w.config.sea_level
        mask = np.isin(w.biome, race.preferred_biomes) & (w.height > sl)
        ys, xs = np.nonzero(mask)
        return list(zip(xs.tolist(), ys.tolist(), strict=True))

    def _spawn_kingdoms(
        self, n_civilized: int, n_tribal: int,
        kingdom_races: list[str] | None = None,
    ) -> None:
        # If the user picked exact races, spawn them directly.
        if kingdom_races:
            race_map = {r.name: r for r in self.races}
            full_map = {r.name: r for r in self.races}
            # fallback to full list if name not in filtered set
            for idx, name in enumerate(kingdom_races):
                race = race_map.get(name) or full_map.get(name)
                if race:
                    self._found_kingdom(race, TRIBAL_GOVERNMENT, f"#{idx + 1}")
            return
        # Stone Age: EVERYONE starts tribal (Chiefdom). Civilized governments
        # unlock through tech eras — no Democracies at year zero.
        # Races are dealt without replacement (shuffled cycle) so neighbors
        # are always distinct: with 2 civ slots you never get Dwarf+Dwarf.
        civ_races = [r for r in self.races if r.form == "civilized"]
        order = self.rng.permutation(len(civ_races)).tolist() if civ_races else []
        for i in range(n_civilized):
            if not civ_races:
                break
            race = civ_races[order[i % len(order)]]
            self._found_kingdom(race, TRIBAL_GOVERNMENT, f"#{i + 1}")
        tribal_races = [r for r in self.races if r.form == "tribal"]
        torder = self.rng.permutation(len(tribal_races)).tolist() if tribal_races else []
        for i in range(n_tribal):
            if not tribal_races:
                break
            race = tribal_races[torder[i % len(torder)]]
            self._found_kingdom(race, TRIBAL_GOVERNMENT, f"#{i + 1}")

    def _found_kingdom(self, race: Race, gov, tag: str) -> Kingdom | None:
        candidates = self._suitable_tiles(race)
        if not candidates:
            return None
        free = [(x, y) for x, y in candidates if self._occupancy[y, x] == 0]
        if not free:
            return None
        # Spread kingdoms apart: prefer tiles far from existing capitals
        if self.kingdoms and len(free) > 20:
            existing = [(k.sites[0].x, k.sites[0].y) for k in self.kingdoms if k.sites]
            scored = []
            for fx, fy in free:
                md = min(manhattan(fx, fy, ex, ey) for ex, ey in existing) if existing else 999
                scored.append((md, fx, fy))
            scored.sort(key=lambda t: -t[0])
            # pick from the farther third so there is still variety
            top = scored[: max(1, len(scored) // 3)]
            _, x, y = top[int(self.rng.integers(0, len(top)))]
        else:
            x, y = free[int(self.rng.integers(0, len(free)))]
        k = Kingdom(
            id=len(self.kingdoms),
            name=self.namegen.kingdom_name(race.form),
            race=race,
            government=gov,
            color=_golden_angle_color(self._color_counter),
        )
        self._color_counter += 1
        # evolvable culture: slight random drift around uniform weights
        # (wide enough that sibling realms research different branches first)
        base = {"martial": 0.25, "mercantile": 0.25, "pious": 0.25, "curious": 0.25}
        k.culture = {
            axis: max(0.05, w + float(self.rng.normal(0, 0.12)))
            for axis, w in base.items()
        }
        self._refresh_mods(k)
        site = self._make_site(k, x, y, is_capital=True)
        k.sites.append(site)
        self._crown_leader(k)
        self._appoint_notable(k, "shaman")
        k.founded_month = self.month
        self.kingdoms.append(k)
        self._occupancy[y, x] = k.id + 1
        # totals must be valid from month 0 (fresh paused worlds show real pops)
        k.total_population = sum(s.population for s in k.sites)
        self._ev(
            EventType.KINGDOM_FOUNDED, "kingdom_founded",
            {"kingdom": k.name, "race": race.name,
             "government": gov.name, "site": site.name},
            (k.id,),
        )
        return k

    def _make_site(
        self, k: Kingdom, x: int, y: int, is_capital: bool = False,
        parent: Site | None = None,
    ) -> Site:
        w = self.world
        prosper = w.prosperity[y, x]
        # original: PopCap = 4 * ReproductionSpeed + Prosperity*150 (x1.5 river, x2 capital)
        popcap = 4 * k.race.reproduction + prosper * 150
        if w.rivers[y, x]:
            popcap *= 1.5
        if is_capital:
            popcap *= 2
        # urbanization: later eras sustain larger settlements
        popcap *= self._era_popcap_mult(k)
        category = "Capital" if is_capital else "Village"
        # heritable site character: children resemble the parent settlement
        # (±0.05 mutation), founders roll fresh traits
        if parent is not None and parent.traits:
            traits = {
                t: min(1.0, max(0.0, v + float(self.rng.normal(0, 0.05))))
                for t, v in parent.traits.items()
            }
        else:
            traits = {
                "aggression": float(self.rng.uniform(0.3, 0.7)),
                "fertility": float(self.rng.uniform(0.3, 0.7)),
                "tradition": float(self.rng.uniform(0.3, 0.7)),
            }
        return Site(
            x=x,
            y=y,
            name=self.namegen.site_name(),
            category=category,
            popcap=math.floor(popcap),
            is_capital=is_capital,
            population=float(self.rng.integers(10, 21)),  # primitive start: 10-20 souls
            traits=traits,
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
            self._ev(
                EventType.BEAST_SIGHTED, "beast_sighted", {"name": name},
            )

    # -- leaders & heroes -------------------------------------------------------

    def _crown_leader(self, k: Kingdom) -> None:
        title = "King" if k.government is not TRIBAL_GOVERNMENT else "Chief"
        reign = max(
            LEADER_REIGN_MIN,
            float(self.rng.normal(LEADER_REIGN_MEAN, LEADER_REIGN_STD)),
        )
        leader = Leader(
            id=self._next_leader_id,
            name=self.namegen.name(),
            kingdom_id=k.id,
            title=title,
            born_month=self.month,
            expected_death_month=self.month + int(reign),
        )
        self._next_leader_id += 1
        k.leader = leader
        self._ev(
            EventType.LEADER_CROWNED, "leader_crowned",
            {"leader": leader.name, "title": title, "kingdom": k.name},
            (k.id,),
        )

    def _appoint_notable(self, k: Kingdom, role: str) -> Notable | None:
        """Appoint a court figure with random personality traits."""
        if any(n.role == role and n.died_month is None for n in k.notables):
            return None
        n = Notable(
            id=self._next_notable_id,
            name=self.namegen.name(),
            kingdom_id=k.id,
            role=role,
            ambition=float(self.rng.random()),
            piety=float(self.rng.random()),
            greed=float(self.rng.random()),
            born_month=self.month,
        )
        self._next_notable_id += 1
        k.notables.append(n)
        return n

    def _living_notables(self, k: Kingdom, role: str | None = None) -> list[Notable]:
        return [
            n for n in k.notables
            if n.died_month is None and (role is None or n.role == role)
        ]

    def _process_notables(self) -> None:
        """Age the courts: die ~50y, arrive on milestones (merchant at 800 pop...)."""
        for k in self.kingdoms:
            if k.collapsed or not k.sites:
                continue
            for n in self._living_notables(k):
                age_months = self.month - n.born_month
                # ~50y lifespan with wide variance
                if age_months > 12 * (30 + self.rng.integers(0, 40)):
                    n.died_month = self.month
            # merchant arrives with prosperity, warlord with war or iron
            if k.total_population > 800:
                made = self._appoint_notable(k, "merchant")
                if made is not None:
                    self._ev(
                        EventType.HERO_EMERGED, "merchant_rose",
                        {"name": made.name, "kingdom": k.name},
                        (k.id,),
                    )
            if (k.at_war or k.tech_level >= 2) and k.total_population > 400:
                made = self._appoint_notable(k, "warlord")
                if made is not None:
                    self._ev(
                        EventType.HERO_EMERGED, "warlord_rose",
                        {"name": made.name, "kingdom": k.name},
                        (k.id,),
                    )

    def _succession(self, k: Kingdom) -> None:
        """Leaders die on their pre-rolled death month and are succeeded."""
        leader = k.leader
        if leader is None or leader.died_month is not None:
            return
        if leader.expected_death_month is None:
            # migration path for leaders created before the field existed
            reign = max(
                LEADER_REIGN_MIN,
                float(self.rng.normal(LEADER_REIGN_MEAN, LEADER_REIGN_STD)),
            )
            leader.expected_death_month = self.month + int(reign)
        if self.month >= leader.expected_death_month:
            leader.died_month = self.month
            self._ev(
                EventType.LEADER_DIED, "leader_died",
                {"title": leader.title, "leader": leader.name,
                 "kingdom": k.name,
                 "age": int(leader.reign_years(self.month)),
                 "victories": leader.battles_won},
                (k.id,),
            )
            self._crown_leader(k)
            # a new reign shifts the culture slightly (evolvable weights)
            if k.culture:
                axes = list(k.culture.keys())
                a, b = self.rng.choice(axes, size=2, replace=False).tolist()
                shift = float(self.rng.uniform(0.02, 0.06))
                k.culture[a] = max(0.05, k.culture[a] - shift)
                k.culture[b] = min(2.0, k.culture[b] + shift)

    def _maybe_emerge_hero(self, k: Kingdom) -> None:
        """Battle-hardened kingdoms sometimes produce a champion."""
        if k.hero is not None:
            return
        # warlords spot talent: ambitious ones breed more heroes
        warlords = self._living_notables(k, "warlord")
        chance = HERO_EMERGE_CHANCE * (
            1.0 + (max(n.ambition for n in warlords) if warlords else 0.0)
        )
        # holy wars quicken zealots
        if k.at_war:
            foe = next((w.side_b if w.side_a is k else w.side_a for w in self.wars if not w.is_over and k in (w.side_a, w.side_b)), None)
            if foe and _faith_distance(k.faith, foe.faith) >= 2:
                chance *= 1.0 + HOLY_WAR_HERO_CHANCE
        if self.rng.random() < chance and k.at_war:
            hero = Hero(
                id=self._next_hero_id,
                name=self.namegen.name(),
                kingdom_id=k.id,
                strength=Hero.roll_strength(self.rng),
            )
            self._next_hero_id += 1
            k.hero = hero
            self._ev(
                EventType.HERO_EMERGED, "hero_emerged",
                {"hero": hero.name, "kingdom": k.name,
                 "strength": f"{hero.strength:.1f}"},
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
            self._ev(
                EventType.HERO_FELL, "hero_fell",
                {"hero": hero.name, "kingdom": k.name,
                 "deeds": len(hero.battle_deeds)},
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
            sl = self.world.config.sea_level
            if 0 <= nx < w and 0 <= ny < h and self.world.height[ny, nx] > sl:
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

            hunt_power = target_k.total_population * target_k.government.militarization / 100
            # beasts take damage from hunts; chip away until dead
            if beast.aggression == 0 or hunt_power > BEAST_HUNT_POWER:
                # damage scales with surplus hunt power; even weak hunts chip 1 HP
                dmg = max(1, int((hunt_power - BEAST_HUNT_POWER * 0.5) / 40))
                # 15% chance for a decisive finishing blow
                if self.rng.random() < 0.15:
                    dmg = beast.hp
                beast.hp -= dmg
                if beast.hp <= 0:
                    beast.hp = 0
                    self._ev(
                        EventType.BEAST_SLAIN, "beast_slain",
                        {"kingdom": target_k.name, "beast": beast.name,
                         "site": target.name},
                        (target_k.id,),
                    )
            elif beast.aggression >= 1:
                # raid: losses capped to not wipe a site in one hit
                dead = max(1, int(beast.power * self.rng.uniform(0.5, 1.2)))
                dead = min(dead, max(1, int(target.population * 0.4)))
                target.population = max(0, target.population - dead)
                self._battle_heat[target.y, target.x] += dead
                self._ev(
                    EventType.BEAST_RAMPAGE, "beast_rampage",
                    {"beast": beast.name, "site": target.name, "dead": dead},
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
            if k.collapsed:
                continue
            self._resettle(k)
            self._grow_kingdom(k)
            self._succession(k)
        self._update_happiness()
        self._maybe_migration()
        self._process_notables()
        self._process_beasts()
        self._update_relations()
        self._update_trade()
        self._resolve_wars()
        for k in self.kingdoms:
            if k.collapsed:
                continue
            self._maybe_shift_government(k)
        self._maybe_rebellion()
        self._tech_and_events()
        self._cleanup_collapsed()
        self._sample_history()
        self._check_milestones()
        # war declaration can happen inside _update_relations/_resolve_wars,
        # so cut newly-broken trade routes right after
        self._cut_wartime_trade()

    def _check_milestones(self) -> None:
        """Fire world-historic achievements exactly once (GDD §7 highlights)."""
        m = self._milestones

        def milestone(key: str, msg_key: str, params: dict, k: Kingdom | None = None) -> None:
            if key in m:
                return
            m.add(key)
            ids = (k.id,) if k else ()
            self._ev(EventType.MILESTONE, msg_key, params, ids)

        # firsts
        cities = [s for k in self.kingdoms for s in k.sites if s.category == "City"]
        if cities:
            owner = next(k for k in self.kingdoms if cities[0] in k.sites)
            milestone(
                "first_city", "milestone_first_city",
                {"kingdom": owner.name, "site": cities[0].name},
                owner,
            )
        if any(w.battles > 0 for w in self.wars):
            milestone("first_battle", "milestone_first_battle", {})
        heroes = [k for k in self.kingdoms if k.hero]
        if heroes:
            milestone(
                "first_hero", "milestone_first_hero",
                {"hero": heroes[0].hero.name},
                heroes[0],
            )
        if any(e.type == EventType.BEAST_SLAIN for e in self.chronicle.events):
            milestone("first_beast_slain", "milestone_first_beast_slain", {})
        # empires
        empire = next((k for k in self.kingdoms if len(k.sites) >= MAX_SITES), None)
        if empire:
            milestone(
                "first_empire", "milestone_first_empire",
                {"kingdom": empire.name, "n": MAX_SITES},
                empire,
            )
        populous = next((k for k in self.kingdoms if k.total_population >= 10_000), None)
        if populous:
            milestone(
                "ten_thousand", "milestone_ten_thousand",
                {"kingdom": populous.name},
                populous,
            )
        # world age
        if self.month >= 100 * 12:
            milestone("century", "milestone_century", {})

    def _sample_history(self) -> None:
        if self.month % 12 == 0:
            for k in self.kingdoms:
                k.population_history.append(k.total_population)

    def _climate_factor(self) -> float:
        """Slow global climate cycle (±8%, ~50y period): harvests breathe.

        Pure function of month+seed (no RNG consumed): bountiful decades
        let cities swell, harsh ones shrink them back. Visible life even
        in total peace.
        """
        phase = (self.world.config.seed % 360) * math.pi / 180
        return 1.0 + 0.08 * math.sin(2 * math.pi * self.month / 600 + phase)

    def _site_trait_avg(self, k: Kingdom, trait: str) -> float:
        """Realm-wide average of a heritable site trait (selection pressure gauge)."""
        if not k.sites:
            return 0.5
        return sum(s.traits.get(trait, 0.5) for s in k.sites) / len(k.sites)

    def _grow_kingdom(self, k: Kingdom) -> None:
        if not k.sites:
            return
        repro = k.race.reproduction
        k.total_population = 0
        if not k.at_war and k.war_exhaustion > 0:
            # exponential decay ~ halves every ~4 months, clears 30 in ~20 peace months
            k.war_exhaustion = max(0, int(k.war_exhaustion * 0.85))
        for site in k.sites:
            # slow ancient growth (~2%/month at repro 100 compounded, quartered
            # above half popcap). Population is float: fractional growth
            # accumulates instead of truncating to zero (which would freeze
            # small villages forever). Fertile bloodlines grow ~±20% faster.
            fertility = site.traits.get("fertility", 0.5)
            growth = (
                site.population * repro / 4000
                * (0.8 + 0.4 * fertility)
                * (1 + k.mods.get("growth", 0.0))
            )
            # a greedy merchant in court squeezes a bit more from the capital
            if site.is_capital and any(
                n.role == "merchant" for n in self._living_notables(k)
            ):
                growth *= 1.0 + 0.3 * max(
                    n.greed for n in self._living_notables(k) if n.role == "merchant"
                )
            # drought: crops fail, growth withers
            if k.drought_until > self.month:
                growth *= 0.45
            # happiness: content realms grow, miserable ones stagnate
            if k.happiness >= 65:
                growth *= 1.10
            elif k.happiness < 35:
                growth *= 0.80 if k.happiness >= 25 else 0.65
            # active trade routes boost growth (merchants move food & tools)
            if k.trade_routes:
                growth *= 1.0 + TRADE_BONUS_GROWTH * len(k.trade_routes)
            # temples feed and heal
            if k.temples:
                growth *= 1.0 + FAITH_TEMPLE_GROWTH * min(4, len(k.temples))
            if site.population > site.popcap / 2:
                growth /= 2.5  # overcrowding slows growth (was /4)
            site.population = site.population + growth
            site.update_category()

            # original clamps to popcap FIRST (unconditionally), then halves
            # and founds a new site only if below MAX_SITES.
            # The clamp uses the EFFECTIVE cap: golden ages (+50%) and the
            # climate cycle (±8%) breathe through it (boom/bust years).
            cap = site.popcap
            if k.golden_age_until > self.month:
                cap *= 1.5
            cap *= self._climate_factor()
            if site.population > cap:
                site.population = cap
                if len(k.sites) < MAX_SITES:
                    if self._expand(k, site):
                        site.population = cap / 2

            k.total_population += site.population

        k.years_since_founding = self.month // 12

    def _expand(self, k: Kingdom, origin: Site) -> bool:
        candidates = self._suitable_tiles(k.race)
        for _ in range(200):
            x, y = candidates[int(self.rng.integers(0, len(candidates)))]
            if self._occupancy[y, x]:
                continue
            if manhattan(origin.x, origin.y, x, y) > EXPANSION_DISTANCE:
                continue
            # giants claim land: no settlement may be founded in their territory
            if any(
                b.hp > 0 and manhattan(b.x, b.y, x, y) <= BEAST_TERRITORY_RADIUS
                for b in self.beasts
            ):
                continue
            site = self._make_site(k, x, y, parent=origin)
            k.sites.append(site)
            self._occupancy[y, x] = k.id + 1
            self._ev(
                EventType.SITE_FOUNDED, "site_founded",
                {"kingdom": k.name, "site": site.name,
                 "category": site.category},
                (k.id,),
            )
            return True
        return False

    # -- happiness & migration ------------------------------------------------

    def _update_happiness(self) -> None:
        """Recompute 0..100 happiness for every living realm (bi-monthly, smooth).

        Factors: government doctrine, war/peace, recent disasters, prosperity,
        overcrowding and golden ages. Lerps toward target to avoid wild swings.
        """
        if self.month % 3 != 0:
            return
        for k in self.kingdoms:
            if k.collapsed or not k.sites:
                continue
            target = 50.0
            # government doctrine
            target += GOV_HAPPINESS.get(k.government.name, 0.0)
            # war is miserable
            if k.at_war:
                ongoing = sum(1 for w in self.wars if not w.is_over and k in (w.side_a, w.side_b))
                target -= 16 * max(1, ongoing)
                # war exhaustion makes it worse
                if k.war_exhaustion > 15:
                    target -= 6
            # recent disasters linger
            months_since_disaster = self.month - k._last_disaster_month
            if months_since_disaster < 24:
                target -= (1 - months_since_disaster / 24) * 14
            months_since_plague = self.month - k._last_plague_month
            if months_since_plague < 18:
                target -= (1 - months_since_plague / 18) * 10
            # drought in progress
            if k.drought_until > self.month:
                target -= 8
            # long peace feels good
            if not k.at_war and k.war_exhaustion == 0 and self.month > k.founded_month + 12:
                target += 6
            # prosperity: fertile lands make people content
            avg_prosper = sum(self.world.prosperity[s.y, s.x] for s in k.sites) / max(1, len(k.sites))
            target += (avg_prosper - 0.5) * 14
            # overcrowding breeds discontent
            crowded = sum(1 for s in k.sites if s.population > s.popcap * 0.85)
            if crowded > 0:
                target -= min(12, crowded * 4)
            # golden age lifts spirits
            if k.golden_age_until > self.month:
                target += 10
            # trade brings quiet contentment
            if k.trade_routes:
                target += TRADE_BONUS_HAPPINESS * len(k.trade_routes)
            # temples bring spiritual contentment
            if k.temples:
                target += FAITH_TEMPLE_HAPPINESS * min(3, len(k.temples))
            # low population / few sites feels precarious
            if k.total_population < 80:
                target -= 8
            if len(k.sites) == 1 and k.total_population < 40:
                target -= 6
            # clamp & lerp (responsive: half-life ~4 months)
            target = max(0, min(100, target))
            k.happiness = k.happiness * 0.68 + target * 0.32
            k.happiness = max(0, min(100, k.happiness))

    def _maybe_migration(self) -> None:
        """People flee misery for greener pastures.

        Every 3 months, unhappy/war-torn realms lose a small share of
        population to a nearby happier neighbor. Even peaceful realms
        exchange people if the prosperity gap is stark.
        """
        if self.month % 3 != 0:
            return
        living = [k for k in self.kingdoms if not k.collapsed and k.sites]
        if len(living) < 2:
            return
        for src in living:
            if src.total_population < MIGRATION_MIN_POP:
                continue
            # push factors: war is the strongest driver
            push = 0.0
            if src.at_war:
                push += 0.7
            if src.happiness < 40:
                push += (40 - src.happiness) / 40 * 0.9
            if src.drought_until > self.month:
                push += 0.35
            # prosperity migration: poor lands bleed people
            if src.happiness < 42 and not src.at_war:
                push += 0.15
            if push < 0.30:
                continue
            if self.rng.random() >= MIGRATION_CHANCE * (1 + push):
                continue
            # find best nearby destination: happier, within contact range
            best = None
            best_score = -1
            for dst in living:
                if dst is src or not dst.sites:
                    continue
                # at war -> not attractive (unless push is extreme)
                if dst.at_war and push < 0.8:
                    continue
                # must be in contact (same radius as diplomacy)
                in_contact = any(
                    manhattan(ss.x, ss.y, ds.x, ds.y) < WAR_DISTANCE
                    for ss in src.sites for ds in dst.sites
                )
                if not in_contact:
                    continue
                # need a meaningful happiness gap
                if dst.happiness <= src.happiness + 3:
                    continue
                score = dst.happiness + dst.total_population / 800
                if dst.golden_age_until > self.month:
                    score += 8
                if score > best_score:
                    best_score, best = score, dst
            if best is None:
                continue
            share = float(self.rng.uniform(*MIGRATION_SHARE))
            if src.happiness < 25:
                share *= 1.6
            moving = max(1, int(src.total_population * share))
            # donors: take proportionally from all source sites
            remaining = moving
            for s in src.sites:
                if remaining <= 0:
                    break
                take = min(s.population * 0.35, remaining * (s.population / max(1, src.total_population)))
                take = max(0, min(s.population - 1, take))
                s.population -= take
                remaining -= take
            actually_moved = moving - remaining
            if actually_moved < 1:
                continue
            # arrive at the capital of the destination
            dst_cap = best.ensure_capital()
            if dst_cap:
                dst_cap.population += actually_moved
            src.total_population = sum(s.population for s in src.sites)
            best.total_population = sum(s.population for s in best.sites)
            self._ev(
                EventType.MIGRATION, "migration",
                {"n": int(actually_moved), "src": src.name, "dst": best.name},
                (src.id, best.id),
            )

    # -- trade ----------------------------------------------------------------

    def _update_trade(self) -> None:
        """Yearly formation of trade routes between peaceful neighbors.
        Pruning of broken routes (war/collapse) happens immediately via
        _cut_wartime_trade() each tick.
        """
        if self.month % 12 != 0:
            return
        living = [k for k in self.kingdoms if not k.collapsed and k.sites]
        if len(living) < 2:
            return

        # prune routes whose partner is gone/at war/collapsed (also called from _cut_wartime_trade monthly)
        for k in living:
            for partner_id in list(k.trade_routes):
                partner = next((q for q in self.kingdoms if q.id == partner_id), None)
                if partner is None or partner.collapsed or not partner.sites or k.at_war or partner.at_war:
                    k.trade_routes.discard(partner_id)
                    if partner is not None and partner.trade_routes is not None:
                        partner.trade_routes.discard(k.id)
                        partner.trader_kindred.pop(k.id, None)
                    k.trader_kindred.pop(partner_id, None)
                    dst_name = partner.name if partner else f"realm {partner_id}"
                    self._ev(EventType.TRADE_CUT, "trade_cut",
                             {"a": k.name, "b": dst_name}, (k.id,))

        # try to open new routes (merchants and coastal techs help)
        # precompute prosperous enough
        def prosper_ok(k: Kingdom) -> bool:
            avg = sum(self.world.prosperity[s.y, s.x] for s in k.sites) / max(1, len(k.sites))
            return avg >= TRADE_MIN_PROSPER

        candidates: list[tuple[Kingdom, Kingdom]] = []
        for i, a in enumerate(living):
            if len(a.trade_routes) >= TRADE_MAX_ROUTES:
                continue
            for b in living[i + 1:]:
                if len(b.trade_routes) >= TRADE_MAX_ROUTES:
                    continue
                if b.id in a.trade_routes:
                    continue
                if a.at_war or b.at_war:
                    continue
                rel = self.get_relation(a, b)
                if rel not in (Relation.ALLIANCE, Relation.PEACE):
                    continue
                # contact?
                contact = any(manhattan(sa.x, sa.y, sb.x, sb.y) < WAR_DISTANCE
                              for sa in a.sites for sb in b.sites)
                if not contact:
                    continue
                # at least one side should be reasonably prosperous
                if not (prosper_ok(a) or prosper_ok(b)):
                    continue
                candidates.append((a, b))

        for a, b in candidates:
            if len(a.trade_routes) >= TRADE_MAX_ROUTES or len(b.trade_routes) >= TRADE_MAX_ROUTES:
                continue
            if b.id in a.trade_routes:
                continue
            # merchants boost the chance; trade-tech helps
            best_greed = 0.0
            for k in (a, b):
                for n in self._living_notables(k, "merchant"):
                    best_greed = max(best_greed, n.greed)
            trade_bonus = max(a.mods.get("colony", 0.0), b.mods.get("colony", 0.0))
            chance = TRADE_FORM_CHANCE * (1 + 0.8 * best_greed + 1.2 * trade_bonus)
            if a.tech_level >= 1:
                chance *= 1.4
            # distant faiths trade less eagerly
            faith_gap = _faith_distance(a.faith, b.faith)
            chance *= max(0.35, 1 - faith_gap * FAITH_TENSION_TRADE_PENALTY)
            if self.rng.random() >= chance:
                continue
            a.trade_routes.add(b.id)
            b.trade_routes.add(a.id)
            a.trader_kindred[b.id] = 0
            b.trader_kindred[a.id] = 0
            self._ev(EventType.TRADE_ROUTE, "trade_route",
                     {"a": a.name, "b": b.name}, (a.id, b.id))

        # bump tenure for surviving routes
        for k in living:
            for pid in list(k.trade_routes):
                k.trader_kindred[pid] = k.trader_kindred.get(pid, 0) + 1

    # -- faith & temples ----------------------------------------------------

    def _maybe_temple(self, k: Kingdom) -> None:
        """Yearly: faithful realms may raise a temple on one of their sites."""
        if self.month % 12 != 0:
            return
        if k.collapsed or not k.sites or len(k.temples) >= len(k.sites):
            return
        if self.rng.random() >= FAITH_TEMPLE_CHANCE:
            return
        # need at least one town/city or capital to justify a temple
        candidates = [s for s in k.sites if s.name not in k.temples and (s.is_capital or s.category in ("Town", "City"))]
        if not candidates:
            candidates = [s for s in k.sites if s.name not in k.temples]
        if not candidates:
            return
        site = candidates[int(self.rng.integers(0, len(candidates)))]
        k.temples.add(site.name)
        self._ev(EventType.FAITH_TEMPLE, "faith_temple",
                 {"kingdom": k.name, "site": site.name, "faith": k.faith}, (k.id,))

    def _maybe_faith_invention(self) -> None:
        """Yearly: temple-heavy realms may spontaneously reform a new faith."""
        if self.month % 12 != 0:
            return
        if self.month < 10 * 12:
            return
        for k in self.kingdoms:
            if k.collapsed or not k.sites or k.faith != "Animism":
                continue
            if len(k.temples) < 1 or len(k.sites) < 3:
                continue
            piety = max((n.piety for n in self._living_notables(k, "shaman")), default=0.0)
            if k.tech_level < 1 and piety < 0.45:
                continue
            if self.rng.random() >= 0.022 * (1 + piety + 0.35 * k.tech_level):
                continue
            # reform: Polytheism most common, Monotheism rarer, Skepticism later/rarer
            choices = ["Polytheism"] * 5 + ["Monotheism"] * 3 + (["Skepticism"] if k.tech_level >= 1 else [])
            new_faith = str(self.rng.choice(choices))
            old = k.faith
            k.faith = new_faith
            self._ev(EventType.FAITH_CONVERSION, "faith_conversion",
                     {"kingdom": k.name, "old": old, "faith": k.faith, "src": "revelation"},
                     (k.id,))

    def _maybe_faith_conversion(self) -> None:
        """Yearly: temples radiate faith; neighbours may convert."""
        if self.month % 12 != 0:
            return
        living = [k for k in self.kingdoms if not k.collapsed and k.sites]
        if len(living) < 2:
            return
        for dst in living:
            # don't overwrite theocratic founders immediately; small grace
            if dst.tech_level >= 1 and dst.government.name in ("Theocracy", "Fanatic Theocracy") and len(dst.temples) > 0:
                continue
            # find templed neighbours that preach another faith (broader than war contact)
            sources: list[Kingdom] = []
            for src in living:
                if src is dst or not src.temples or src.faith == dst.faith:
                    continue
                # faith spreads via pilgrims further than armies look
                if any(manhattan(sa.x, sa.y, sb.x, sb.y) < WAR_DISTANCE + 18 for sa in src.sites for sb in dst.sites):
                    sources.append(src)
            if not sources:
                continue
            # weight by number of temples + piety
            best_src = max(sources, key=lambda s: (len(s.temples), max((n.piety for n in self._living_notables(s, "shaman")), default=0.5)))
            piety = max((n.piety for n in self._living_notables(dst, "shaman")), default=0.0)
            chance = FAITH_CONVERSION_CHANCE * (0.7 + 0.75 * min(1.0, len(best_src.temples) / 2))
            chance *= (1 - 0.35 * piety)
            # evolved faiths resist regressing to Animism
            if dst.faith != "Animism" and best_src.faith == "Animism":
                chance *= 0.45
            if dst.faith == "Skepticism":
                chance *= 0.6
            if self.rng.random() >= chance:
                continue
            old = dst.faith
            dst.faith = best_src.faith
            self._ev(EventType.FAITH_CONVERSION, "faith_conversion",
                     {"kingdom": dst.name, "old": old, "faith": dst.faith, "src": best_src.name},
                     (dst.id, best_src.id))

    def _cut_wartime_trade(self) -> None:
        """Break active routes touched by any fresh war declaration this tick."""
        if not any(not w.is_over for w in self.wars):
            return
        war_ids = set()
        for w in self.wars:
            if w.is_over:
                continue
            war_ids.add(w.side_a.id)
            war_ids.add(w.side_b.id)
        for k in self.kingdoms:
            if k.collapsed or not k.trade_routes:
                continue
            for pid in list(k.trade_routes):
                if pid in war_ids or k.id in war_ids:
                    partner = next((q for q in self.kingdoms if q.id == pid), None)
                    if partner and (k.at_war or partner.at_war):
                        k.trade_routes.discard(pid)
                        if partner:
                            partner.trade_routes.discard(k.id)
                            partner.trader_kindred.pop(k.id, None)
                        k.trader_kindred.pop(pid, None)
                        self._ev(EventType.TRADE_CUT, "trade_cut",
                                 {"a": k.name, "b": partner.name if partner else f"realm {pid}"}, (k.id,))

    # -- relations & war ------------------------------------------------------

    def _update_relations(self) -> None:
        """Scan neighbor kingdoms; degrade relations toward tension/war (GDD §6)."""
        active = [k for k in self.kingdoms if k.sites]
        # Precompute bounding boxes for spatial index (avoids O(n²·sites²) when kingdoms far apart)
        bboxes = {}
        for k in active:
            xs = [s.x for s in k.sites]
            ys = [s.y for s in k.sites]
            bboxes[k.id] = (min(xs), max(xs), min(ys), max(ys))
        for i, a in enumerate(active):
            for b in active[i + 1 :]:
                current = self.get_relation(a, b)
                if current == Relation.WAR:
                    continue
                # spatial index: if bounding boxes separated by > WAR_DISTANCE, no contact possible
                ax0, ax1, ay0, ay1 = bboxes[a.id]
                bx0, bx1, by0, by1 = bboxes[b.id]
                if ax1 + WAR_DISTANCE < bx0 or bx1 + WAR_DISTANCE < ax0:
                    continue
                if ay1 + WAR_DISTANCE < by0 or by1 + WAR_DISTANCE < ay0:
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

                # power disparity: the strong eye the weak (new dynamics)
                pop_a = max(1, a.total_population)
                pop_b = max(1, b.total_population)
                power_ratio = max(pop_a, pop_b) / min(pop_a, pop_b)
                disparity_bonus = 0.0
                if power_ratio >= 1.5:
                    stronger = a if pop_a > pop_b else b
                    if stronger.aggression >= 3:
                        disparity_bonus = 0.05 * min(2.0, power_ratio - 0.5)

                # expansionist pressure: saturated kingdoms want their
                # neighbor's land (GDD: worlds must stay dynamic after fill-up)
                expansionist = (
                    len(a.sites) >= MAX_SITES and len(b.sites) >= MAX_SITES
                )
                expansionist_chance = (
                    EXPANSIONIST_WAR_CHANCE * (1 + 0.5 * max(a.tech_level, b.tech_level))
                    if expansionist else 0.0
                )

                roll = self.rng.random()
                base_war_chance = 0.10 if combined_aggr >= 4 else (
                    0.08 if combined_aggr >= 3 else 0.03
                )
                # aggressive bloodlines push their realms toward war
                blood_bonus = (
                    self._site_trait_avg(a, "aggression")
                    + self._site_trait_avg(b, "aggression")
                    - 1.0
                ) * 0.05
                # faith tension: distant creeds fight more
                faith_tension = _faith_distance(a.faith, b.faith) * FAITH_TENSION_WAR_BONUS
                war_chance = (
                    base_war_chance + disparity_bonus + expansionist_chance + blood_bonus + faith_tension
                )
                if roll < war_chance:
                    self._declare_war(a, b)
                elif roll < war_chance + 0.08:
                    self._set_relation(a, b, Relation.TENSION)
                elif roll < war_chance + 0.10:
                    self._set_relation(a, b, Relation.ALLIANCE)
                    self._ev(
                        EventType.ALLIANCE_FORMED, "alliance_formed",
                        {"a": a.name, "b": b.name},
                        (a.id, b.id),
                    )
                else:
                    self._set_relation(a, b, Relation.TENSION if roll < 0.5 else Relation.PEACE)

    def _war_name(self, a: Kingdom, b: Kingdom) -> tuple[str, str, str]:
        """Named wars: prefer a border site the aggressor covets; else leader-based.

        Returns (war_name, cause_key, cause_site) — cause_key is one of
        "border" | "disputed" for i18n rendering.
        """
        # find the closest site pair between the two realms
        best: Site | None = None
        best_d = 1 << 30
        for sa in a.sites:
            for sb in b.sites:
                d = manhattan(sa.x, sa.y, sb.x, sb.y)
                if d < best_d:
                    best_d, best = d, sb
        if best is not None and self.rng.random() < 0.6:
            return f"The War of {best.name}", "disputed", best.name
        if a.leader and b.leader and self.rng.random() < 0.5:
            return f"The War of {b.leader.name}'s Wrath", "border", ""
        return f"The {a.name}-{b.name} War", "border", ""

    def _declare_war(self, a: Kingdom, b: Kingdom) -> None:
        name, cause_key, cause_site = self._war_name(a, b)
        cause = (
            f"disputed lands around {cause_site}"
            if cause_key == "disputed"
            else "border friction"
        )
        war = War(
            id=self._next_war_id,
            side_a=a,
            side_b=b,
            start_month=self.month,
            name=name,
            cause=cause,
        )
        self._next_war_id += 1
        self.wars.append(war)
        self._set_relation(a, b, Relation.WAR)
        a.at_war = True
        b.at_war = True
        self._ev(
            EventType.WAR_DECLARED, "war_declared",
            {"war": war.name, "a": a.name, "b": b.name,
             "cause_key": cause_key, "site": cause_site},
            (a.id, b.id),
        )
        if _faith_distance(a.faith, b.faith) >= 2:
            self._ev(
                EventType.HOLY_WAR, "holy_war",
                {"war": war.name, "a": a.name, "b": b.name,
                 "faith_a": a.faith, "faith_b": b.faith},
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
        # cobelligerent: ally joins the existing war without a separate War
        # object (avoids double-counting battles/losses for the same conflict)
        if joiner not in war.cobelligerents_a and joiner not in war.cobelligerents_b:
            if war.side_a is enemy:
                war.cobelligerents_a.append(joiner)
            else:
                war.cobelligerents_b.append(joiner)
        self._ev(
            EventType.ALLIANCE_JOINED_WAR, "alliance_joined_war",
            {"joiner": joiner.name, "war": war.name, "enemy": enemy.name},
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
            # armies = militarization % of population, seasoned by race strength,
            # era and tech-tree weapons. Ambitious warlords drill harder hosts.
            era_mult = (1.0 + ERA_ARMY_BONUS * a.tech_level) * (1 + a.mods.get("army", 0.0))
            era_mult_b = (1.0 + ERA_ARMY_BONUS * b.tech_level) * (1 + b.mods.get("army", 0.0))
            warlords_a = self._living_notables(a, "warlord")
            warlords_b = self._living_notables(b, "warlord")
            drill_a = 1.0 + (0.2 * max(n.ambition for n in warlords_a) if warlords_a else 0.0)
            drill_b = 1.0 + (0.2 * max(n.ambition for n in warlords_b) if warlords_b else 0.0)
            pow_a = (
                a.total_population * a.government.militarization / 100
                * a.race.strength * era_mult * drill_a
            )
            pow_b = (
                b.total_population * b.government.militarization / 100
                * b.race.strength * era_mult_b * drill_b
            )
            if a.hero:
                pow_a *= 1.0 + (a.hero.strength - 1.0) * 0.5
            if b.hero:
                pow_b *= 1.0 + (b.hero.strength - 1.0) * 0.5
            # holy war zeal: distant creeds fight harder
            faith_gap = _faith_distance(a.faith, b.faith)
            if faith_gap >= 2:
                # both sides surge, the slightly more pious one surges harder
                pa = max((n.piety for n in self._living_notables(a, "shaman")), default=0.0)
                pb = max((n.piety for n in self._living_notables(b, "shaman")), default=0.0)
                pow_a *= 1.0 + HOLY_WAR_ARMY_BONUS * (0.7 + 0.6 * pa)
                pow_b *= 1.0 + HOLY_WAR_ARMY_BONUS * (0.7 + 0.6 * pb)
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
            # cobelligerents share the burden
            for cob in list(war.cobelligerents_a) + list(war.cobelligerents_b):
                if cob.sites:
                    cob.war_exhaustion += 1
                    self._maybe_emerge_hero(cob)
            # war exhaustion forces white peace even before the 60-month cap
            if not war.is_over and a.war_exhaustion > 30 and b.war_exhaustion > 30 and war_duration > 30:
                war.end_month = self.month
                self._ev(
                    EventType.WAR_ENDED, "war_white_peace",
                    {"war": war.name, "a": a.name, "b": b.name},
                    (a.id, b.id),
                )
                self._end_war(war)
                continue
            if war_duration > WAR_DURATION_MAX:
                # white peace after too long
                war.end_month = self.month
                self._ev(
                    EventType.WAR_ENDED, "war_white_peace",
                    {"war": war.name, "a": a.name, "b": b.name},
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
        # diplomacy resets: former enemies can fight again or become allies
        a_still_war = any(
            w is not war and not w.is_over and (
                (war.side_a is w.side_a and war.side_b is w.side_b)
                or (war.side_a is w.side_b and war.side_b is w.side_a)
            )
            for w in self.wars
        )
        if not a_still_war:
            self._set_relation(war.side_a, war.side_b, Relation.PEACE)
        # cobelligerents: clear their at_war and relations if no other war
        for coblist, enemy in [(war.cobelligerents_a, war.side_b), (war.cobelligerents_b, war.side_a)]:
            for cob in coblist:
                cob.at_war = any(
                    w is not war and not w.is_over and cob in (w.side_a, w.side_b)
                    or w is not war and not w.is_over and (
                        cob in w.cobelligerents_a or cob in w.cobelligerents_b
                    )
                    for w in self.wars
                )
                still_vs_enemy = any(
                    w is not war and not w.is_over and (
                        (cob is w.side_a and enemy is w.side_b)
                        or (cob is w.side_b and enemy is w.side_a)
                        or (cob in w.cobelligerents_a and enemy is w.side_b)
                        or (cob in w.cobelligerents_b and enemy is w.side_a)
                    )
                    for w in self.wars
                )
                if not still_vs_enemy:
                    self._set_relation(cob, enemy, Relation.PEACE)
        war.cobelligerents_a = []
        war.cobelligerents_b = []
        # war record bookkeeping (drives the W/L stats)
        if war.victor is not None:
            loser = war.side_b if war.victor is war.side_a else war.side_a
            war.victor.wars_won += 1
            loser.wars_lost += 1

    def _log_war_end(self, war: War, extra: str = "") -> None:
        v = war.victor
        if v:
            months = (war.end_month or self.month) - war.start_month
            self._ev(
                EventType.WAR_ENDED, "war_won",
                {"victor": v.name, "war": war.name,
                 "dead": war.total_dead, "months": months},
                (war.side_a.id, war.side_b.id),
            )
        else:
            self._ev(
                EventType.WAR_ENDED, "war_ended", {"war": war.name},
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

        if winner.hero:
            self._ev(
                EventType.BATTLE, "battle_champion",
                {"winner": winner.name, "loser": loser.name,
                 "dead": loss, "champion": winner.hero.name},
                (winner.id, loser.id),
            )
        else:
            self._ev(
                EventType.BATTLE, "battle",
                {"winner": winner.name, "loser": loser.name, "dead": loss},
                (winner.id, loser.id),
            )
        if len(loser.sites) > 1 and self.rng.random() < 0.35 + winner.mods.get(
            "capture", 0.0
        ):
            # prefer border sites (within WAR_DISTANCE of a winner site)
            border = [
                s for s in loser.sites if not s.is_capital
                and any(manhattan(s.x, s.y, ws.x, ws.y) < WAR_DISTANCE for ws in winner.sites)
            ]
            pool = border if border else [s for s in loser.sites if not s.is_capital]
            if not pool:
                return
            site = pool[int(self.rng.integers(0, len(pool)))]
            if not site.is_capital:
                loser.sites.remove(site)
                self._occupancy[site.y, site.x] = 0
                if len(winner.sites) < MAX_SITES:
                    winner.sites.append(site)
                    self._occupancy[site.y, site.x] = winner.id + 1
                    # temple goes with the land (conqueror inherits the building)
                    if site.name in loser.temples:
                        loser.temples.discard(site.name)
                        winner.temples.add(site.name)
                    self._ev(
                        EventType.SITE_CAPTURED, "site_captured",
                        {"winner": winner.name, "site": site.name,
                         "loser": loser.name},
                        (winner.id, loser.id),
                    )
                else:
                    self._ev(
                        EventType.SITE_CAPTURED, "site_razed",
                        {"winner": winner.name, "site": site.name,
                         "loser": loser.name},
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

    def _resettle(self, k: Kingdom) -> None:
        """Migrants flow from populous sites to empty ones (recovery path).

        Without this, a site at 0 population grows 0 forever (0 x rate),
        freezing realms that lose everything but their land.
        """
        empties = [s for s in k.sites if s.population <= 0]
        if not empties:
            return
        for target in empties:
            donors = [s for s in k.sites if s is not target and s.population > 50]
            if not donors:
                break
            donor = max(donors, key=lambda s: s.population)
            move = min(15.0, donor.population * 0.1)
            donor.population -= move
            target.population += move

    def _cleanup_collapsed(self) -> None:
        for k in self.kingdoms:
            if k.total_population > 0:
                continue
            if k.collapsed:
                continue
            k.collapsed = True
            # cut all trade routes touching the dead realm
            for pid in list(k.trade_routes):
                partner = next((q for q in self.kingdoms if q.id == pid), None)
                if partner:
                    partner.trade_routes.discard(k.id)
                    partner.trader_kindred.pop(k.id, None)
            k.trade_routes.clear()
            k.trader_kindred.clear()
            # release its land so the map doesn't fill with ghost claims
            for s in k.sites:
                if self._occupancy[s.y, s.x] == k.id + 1:
                    self._occupancy[s.y, s.x] = 0
            k.sites.clear()
            self.fallen.append({
                "name": k.name,
                "race": k.race.name,
                "color": list(k.color),
                "month": self.month,
                "at_war": k.at_war,
            })
            self._ev(
                EventType.KINGDOM_COLLAPSED, "kingdom_collapsed",
                {"kingdom": k.name},
                (k.id,),
            )
            # end its wars
            for war in self.wars:
                if not war.is_over and k in (war.side_a, war.side_b):
                    other = war.side_b if war.side_a is k else war.side_a
                    war.end_month = self.month
                    war.victor = other
                    self._end_war(war)

    # -- rebellions -----------------------------------------------------------

    def _maybe_rebellion(self) -> None:
        """Big realms under strain can split: distant sites secede.

        Trigger conditions (any): prolonged war exhaustion, or a saturated
        militarized regime. The new realm takes ~half the sites. Capped at
        MAX_KINGDOMS to avoid runaway fragmentation (41-kingdom worlds).
        """
        if len(self.kingdoms) >= MAX_KINGDOMS:
            return
        for k in list(self.kingdoms):
            if len(k.sites) < REBELLION_MIN_SITES:
                continue
            # ambitious warlords plot coups: high ambition adds strain
            warlords = self._living_notables(k, "warlord")
            coup_risk = max([n.ambition for n in warlords] + [0.0])
            # misery breeds revolt: low happiness is its own strain
            strain = (
                k.war_exhaustion > 24
                or k.happiness < 25
                or (
                    len(k.sites) >= MAX_SITES and k.government.militarization >= 40
                )
                or (coup_risk > 0.85 and len(k.sites) >= REBELLION_MIN_SITES)
            )
            if not strain:
                continue
            # miserable realms rebel more often
            happiness_mult = 1.0 if k.happiness >= 35 else 2.5
            if self.rng.random() >= REBELLION_CHANCE_MONTHLY * (1.0 + coup_risk) * happiness_mult:
                continue

            # the farthest half of sites secedes (distance to capital)
            cap = k.ensure_capital()
            if cap is None:
                continue
            ranked = sorted(
                k.sites,
                key=lambda s: -manhattan(cap.x, cap.y, s.x, s.y),
            )
            n_secede = max(2, len(k.sites) // 2)
            seceders = [s for s in ranked[:n_secede] if not s.is_capital]
            if len(seceders) < 2:
                continue

            new_k = Kingdom(
                id=len(self.kingdoms),
                name=self.namegen.kingdom_name(k.race.form),
                race=k.race,
                government=k.government,
                color=_golden_angle_color(self._color_counter),
                civilized=k.civilized,
                tech_level=k.tech_level,
            )
            self._color_counter += 1
            for s in seceders:
                k.sites.remove(s)
                new_k.sites.append(s)
                self._occupancy[s.y, s.x] = new_k.id + 1
            new_k.total_population = sum(s.population for s in new_k.sites)
            k.total_population = sum(s.population for s in k.sites)
            # faith & temples split with the land
            new_k.faith = k.faith
            for s in new_k.sites:
                if s.name in k.temples:
                    k.temples.discard(s.name)
                    new_k.temples.add(s.name)
            # rebels carry the parent's knowledge and culture, drifted
            new_k.techs = set(k.techs)
            new_k.culture = {
                axis: max(0.05, w + float(self.rng.normal(0, 0.05)))
                for axis, w in (k.culture or {}).items()
            } or dict(k.culture or {})
            self._refresh_mods(new_k)
            self._crown_leader(new_k)
            new_k.founded_month = self.month
            self.kingdoms.append(new_k)
            self._ev(
                EventType.REBELLION, "rebellion",
                {"n": len(seceders), "kingdom": k.name,
                 "new_kingdom": new_k.name, "title": new_k.leader.title,
                 "leader": new_k.leader.name},
                (k.id, new_k.id),
            )
            # the old realm wants its land back: immediate war
            self._declare_war(k, new_k)

    # -- tech eras, golden ages, plagues, colonies -----------------------------

    def _tech_and_events(self) -> None:
        for k in list(self.kingdoms):
            if k.collapsed or not k.sites:
                continue
            self._progress_tech(k)
            self._maybe_golden_age(k)
            self._maybe_plague(k)
            self._maybe_drought(k)
            self._maybe_flood(k)
            self._maybe_famine(k)
            self._maybe_earthquake(k)
            self._maybe_temple(k)
            self._maybe_colony(k)
        self._maybe_faith_invention()
        self._maybe_faith_conversion()

    def _refresh_mods(self, k: Kingdom) -> None:
        """Aggregate tech effects + derive era level from acquired techs."""
        mods: dict[str, float] = {
            "army": 0.0, "popcap": 0.0, "growth": 0.0, "tech": 0.0,
            "plague": 0.0, "colony": 0.0, "capture": 0.0,
            "influence": 0.0, "golden": 0.0,
        }
        for tid in k.techs:
            for eff, val in TECHS[tid]["effects"].items():
                mods[eff] = mods.get(eff, 0.0) + val
        k.mods = mods
        if k.techs:
            k.tech_level = max(TECHS[tid]["era"] for tid in k.techs)

    def _branch_affinity(self, k: Kingdom, branch: str) -> float:
        """Research taste: government doctrine + evolved culture."""
        gov = GOV_BRANCH_AFFINITY.get(k.government.name, {})
        gov_w = gov.get(branch, 0.2)
        cult = k.culture or {}
        cult_w = {
            "warfare": cult.get("martial", 0.25),
            "craft": cult.get("mercantile", 0.25) * 0.5 + cult.get("curious", 0.25) * 0.5,
            "trade": cult.get("mercantile", 0.25),
            "lore": cult.get("curious", 0.25) + cult.get("pious", 0.25) * 0.5,
        }.get(branch, 0.2)
        return gov_w + cult_w

    def _progress_tech(self, k: Kingdom) -> None:
        """Yearly research: accumulate insight, acquire one tech, diffuse.

        Paced for a very ancient feel (~150y to the first techs), with
        knowledge compounding slightly each era. Neighbors ahead share
        knowledge (catch-up diffusion).
        """
        if self.month % 12 != 0:
            return
        # insight: tile prosperity (0..1) + government tech bonus, compounding.
        # a pious shaman in court speeds the work of scholars a little.
        prosper = sum(self.world.prosperity[s.y, s.x] for s in k.sites) / max(
            1, len(k.sites)
        )
        yearly = (0.008 + 0.008 * prosper + k.government.tech_bonus * 0.0004)
        yearly *= 1 + 0.12 * k.tech_level + k.mods.get("tech", 0.0)
        shamans = self._living_notables(k, "shaman")
        if shamans:
            yearly *= 1.0 + 0.2 * max(n.piety for n in shamans)
        # diffusion: realms behind their neighbors learn faster
        others_max = 0
        for o in self.kingdoms:
            if o is not k and o.sites:
                others_max = max(others_max, len(o.techs))
        if others_max > len(k.techs):
            yearly *= 1.5
        k.tech_progress += yearly

        # research choice: cheapest cost adjusted by taste (gov + culture)
        avail = available_techs(k.techs, k.tech_level)
        if not avail:
            return
        def _score(tid: str) -> float:
            t = TECHS[tid]
            taste = self._branch_affinity(k, t["branch"])
            return t["cost"] / (1.0 + taste + 0.3 * self.rng.random())

        pick = min(avail, key=_score)
        if k.tech_progress < TECHS[pick]["cost"]:
            return
        k.tech_progress -= TECHS[pick]["cost"]
        old_level = k.tech_level
        k.techs.add(pick)
        self._refresh_mods(k)
        # world-first discovery is newsworthy
        if pick not in self._world_techs:
            self._world_techs.add(pick)
            firsts = [o for o in self.kingdoms if pick in o.techs]
            _ = firsts  # (the acquirer is k by construction here)
            self._ev(
                EventType.TECH_DISCOVERY, "tech_discovery",
                {"kingdom": k.name, "tech": pick},
                (k.id,),
            )
        # entering a new max era rescales settlements + may civilize
        if k.tech_level > old_level:
            era = ERA_NAMES[k.tech_level]
            # urbanization: existing settlements can now sustain more people
            mult = self._era_popcap_mult(k)
            base_mult = 0.7 + ERA_POPCAP_BONUS * (k.tech_level - 1)
            for s in k.sites:
                s.popcap = math.floor(s.popcap * mult / base_mult)
            self._ev(
                EventType.TECH_ERA, "tech_era",
                {"kingdom": k.name, "era": era},
                (k.id,),
            )
            # tribes entering the Iron Age become civilizations — and so does
            # any realm still under a Tribal council: the Iron Age invents
            # the state itself
            if k.tech_level >= 2 and k.government is TRIBAL_GOVERNMENT:
                k.civilized = True
                allowed = self._allowed_governments(k, exclude_tribal=True)
                gov = allowed[int(self.rng.integers(0, len(allowed)))] if allowed else k.government
                old = k.government.name
                k.government = gov
                k.last_gov_shift_month = self.month
                self._ev(
                    EventType.GOVERNMENT_CHANGED, "government_civilized",
                    {"kingdom": k.name, "old": old,
                     "government": gov.name, "era": era},
                    (k.id,),
                )
        elif not avail and k.tech_progress >= 1.0:
            # tree exhausted: continuous urban growth (~+2%/year, scaled by era)
            k.tech_progress = 0.0
            era_scale = 1.0 + 0.15 * k.tech_level
            for s in k.sites:
                s.popcap = math.floor(s.popcap * (1 + 0.02 * era_scale))

    def _era_popcap_mult(self, k: Kingdom) -> float:
        # primitive agriculture sustains less (Stone x0.7); each era adds 25%;
        # tech-tree sustenance techs stack on top
        mult = (0.7 + ERA_POPCAP_BONUS * k.tech_level) * (1 + k.mods.get("popcap", 0.0))
        if k.golden_age_until > self.month:
            mult *= 1.5
        return mult

    def _maybe_golden_age(self, k: Kingdom) -> None:
        if k.golden_age_until > self.month:
            return
        prosperous = (
            sum(s.popcap for s in k.sites) > 800
            and not k.at_war
            and k.tech_level >= 1
        )
        # pious courts attract golden ages a little more often
        shamans = self._living_notables(k, "shaman")
        piety_boost = 1.0 + (max(n.piety for n in shamans) if shamans else 0.0)
        if prosperous and self.rng.random() < TECH_GOLDEN_AGE_CHANCE * piety_boost * (
            1 + k.mods.get("golden", 0.0)
        ):
            k.golden_age_until = self.month + TECH_GOLDEN_AGE_YEARS * 12
            self._ev(
                EventType.GOLDEN_AGE, "golden_age", {"kingdom": k.name}, (k.id,),
            )

    def _maybe_plague(self, k: Kingdom) -> None:
        """Dense realms are struck by pestilence (big-population pressure valve)."""
        if k.total_population < PLAGUE_MIN_DENSITY_POP:
            return
        if self.rng.random() >= PLAGUE_CHANCE_MONTHLY:
            return
        rate = float(self.rng.uniform(*PLAGUE_MORTALITY))
        rate *= 1.0 + k.mods.get("plague", 0.0)  # pottery etc. soften plagues
        # pious shamans organize quarantines and rites: fewer dead
        shamans = self._living_notables(k, "shaman")
        if shamans:
            rate *= 1.0 - 0.3 * max(n.piety for n in shamans)
        dead = int(k.total_population * rate)
        self._apply_population_loss(k, dead)
        for s in k.sites:
            self._battle_heat[s.y, s.x] += s.population * rate * 0.5
        k._last_plague_month = self.month
        self._ev(
            EventType.PLAGUE, "plague",
            {"kingdom": k.name, "dead": dead},
            (k.id,),
        )

    # -- natural disasters ---------------------------------------------------

    def _maybe_drought(self, k: Kingdom) -> None:
        """A prolonged dry spell: crops fail, growth halved for months."""
        if k.drought_until > self.month:
            return  # already in drought
        if k.total_population < 80:
            return  # too small to notice
        if self.rng.random() >= DROUGHT_CHANCE:
            return
        duration = int(self.rng.integers(*DROUGHT_DURATION))
        k.drought_until = self.month + duration
        # immediate toll: a fraction die from thirst/hunger
        rate = float(self.rng.uniform(*DROUGHT_MORTALITY))
        dead = max(1, int(k.total_population * rate))
        self._apply_population_loss(k, dead)
        for s in k.sites:
            self._battle_heat[s.y, s.x] += s.population * rate * 0.3
        k._last_disaster_month = self.month
        self._ev(
            EventType.DROUGHT, "drought",
            {"kingdom": k.name, "dead": dead, "months": duration},
            (k.id,),
        )

    def _maybe_flood(self, k: Kingdom) -> None:
        """River settlements are swept away."""
        if self.rng.random() >= FLOOD_CHANCE:
            return
        riverside = [s for s in k.sites if self.world.rivers[s.y, s.x]]
        if not riverside:
            return
        site = riverside[int(self.rng.integers(0, len(riverside)))]
        rate = float(self.rng.uniform(*FLOOD_MORTALITY))
        dead = max(1, int(site.population * rate))
        site.population = max(1, site.population - dead)
        k.total_population = sum(s.population for s in k.sites)
        self._battle_heat[site.y, site.x] += dead
        k._last_disaster_month = self.month
        self._ev(
            EventType.FLOOD, "flood",
            {"kingdom": k.name, "site": site.name, "dead": dead},
            (k.id,),
        )

    def _maybe_famine(self, k: Kingdom) -> None:
        """Follows drought or extreme overcrowding: mass starvation."""
        in_drought = k.drought_until > self.month
        overcrowded = any(s.population > s.popcap * 0.85 for s in k.sites)
        if not (in_drought or overcrowded):
            return
        if k.total_population < 100:
            return
        if self.rng.random() >= FAMINE_CHANCE * (2.0 if in_drought else 1.0):
            return
        rate = float(self.rng.uniform(*FAMINE_MORTALITY))
        dead = max(1, int(k.total_population * rate))
        self._apply_population_loss(k, dead)
        for s in k.sites:
            self._battle_heat[s.y, s.x] += s.population * rate * 0.3
        k._last_disaster_month = self.month
        self._ev(
            EventType.FAMINE, "famine",
            {"kingdom": k.name, "dead": dead},
            (k.id,),
        )

    def _maybe_earthquake(self, k: Kingdom) -> None:
        """Rare: the ground shakes, devastating 1-3 nearby settlements."""
        if self.rng.random() >= EARTHQUAKE_CHANCE:
            return
        if len(k.sites) < 1:
            return
        epicenter = k.sites[int(self.rng.integers(0, len(k.sites)))]
        # affect sites within 4 tiles of epicenter (including itself)
        affected = [s for s in k.sites if manhattan(s.x, s.y, epicenter.x, epicenter.y) <= 4]
        if not affected:
            affected = [epicenter]
        total_dead = 0
        for site in affected:
            rate = float(self.rng.uniform(*EARTHQUAKE_MORTALITY))
            # epicenter hit hardest
            if site is epicenter:
                rate = min(0.35, rate * 1.4)
            dead = max(1, int(site.population * rate))
            dead = min(dead, max(1, int(site.population * 0.9)))
            site.population = max(0, site.population - dead)
            total_dead += dead
            self._battle_heat[site.y, site.x] += dead
        k.total_population = sum(s.population for s in k.sites)
        epicenter_name = epicenter.name
        k._last_disaster_month = self.month
        self._ev(
            EventType.EARTHQUAKE, "earthquake",
            {"kingdom": k.name, "site": epicenter_name, "dead": total_dead,
             "n": len(affected)},
            (k.id,),
        )

    def _maybe_colony(self, k: Kingdom) -> None:
        """Saturated realms send settlers far away; distant colonies go independent."""
        if len(k.sites) < COLONY_FOUNDED_MIN_SITES or len(k.sites) >= MAX_SITES:
            return
        if self.month % 6 != 0:  # colonization attempts every 6 months
            return
        # greedy merchants fund expeditions
        merchants = self._living_notables(k, "merchant")
        greed_boost = 1.0 + (max(n.greed for n in merchants) if merchants else 0.0)
        if self.rng.random() >= 0.25 * greed_boost * (1 + k.mods.get("colony", 0.0)):
            return
        cap = k.ensure_capital()
        if cap is None:
            return
        candidates = self._suitable_tiles(k.race)
        for _ in range(60):
            x, y = candidates[int(self.rng.integers(0, len(candidates)))]
            dist = manhattan(cap.x, cap.y, x, y)
            if not (COLONY_DISTANCE[0] <= dist <= COLONY_DISTANCE[1]):
                continue
            if self._occupancy[y, x] or any(
                b.hp > 0 and manhattan(b.x, b.y, x, y) <= BEAST_TERRITORY_RADIUS
                for b in self.beasts
            ):
                continue
            site = self._make_site(k, x, y, parent=cap)
            k.sites.append(site)
            self._occupancy[y, x] = k.id + 1
            if dist >= COLONY_INDEPENCE_DISTANCE and len(self.kingdoms) < MAX_KINGDOMS:
                # far colony declares peaceful independence
                k.sites.remove(site)
                form = (
                    "civilized"
                    if k.civilized or k.race.form == "civilized"
                    else k.race.form
                )
                new_k = Kingdom(
                    id=len(self.kingdoms),
                    name=self.namegen.kingdom_name(form),
                    race=k.race,
                    government=k.government,
                    color=_golden_angle_color(self._color_counter),
                    civilized=k.civilized,
                )
                self._color_counter += 1
                new_k.tech_level = max(0, k.tech_level - 1)
                new_k.faith = k.faith
                # settlers carry a subset of the parent's knowledge + culture
                new_k.techs = {
                    tid for tid in k.techs
                    if TECHS[tid]["era"] <= new_k.tech_level
                }
                new_k.culture = {
                    axis: max(0.05, w + float(self.rng.normal(0, 0.05)))
                    for axis, w in (k.culture or {}).items()
                } or dict(k.culture or {})
                self._refresh_mods(new_k)
                new_k.sites.append(site)
                self._occupancy[site.y, site.x] = new_k.id + 1
                self._crown_leader(new_k)
                new_k.total_population = site.population
                new_k.founded_month = self.month
                self.kingdoms.append(new_k)
                self._ev(
                    EventType.INDEPENDENCE, "independence",
                    {"site": site.name, "kingdom": k.name,
                     "new_kingdom": new_k.name},
                    (k.id, new_k.id),
                )
            else:
                self._ev(
                    EventType.COLONY_FOUNDED, "colony_founded",
                    {"kingdom": k.name, "site": site.name},
                    (k.id,),
                )
            return

    # -- government evolution (GDD §4) -----------------------------------------

    def _allowed_governments(self, k: Kingdom, exclude_tribal: bool = True) -> list:
        """Governments this realm may adopt at its current tech level."""
        out = []
        for g in self.governments:
            if g is TRIBAL_GOVERNMENT:
                continue
            if GOV_MIN_ERA.get(g.name, 0) > k.tech_level:
                continue
            out.append(g)
        if not exclude_tribal:
            out.append(TRIBAL_GOVERNMENT)
        return out

    def _maybe_shift_government(self, k: Kingdom) -> None:
        # Stone-Age tribes under a council don't shift institutions yet
        if k.government is TRIBAL_GOVERNMENT and k.tech_level < 2:
            return
        if not k.sites:
            return
        if k.years_since_founding < GOV_SHIFT_MIN_YEARS:
            return
        if self.month % 12 != 0:  # evaluate yearly
            return
        civ_govs = self._allowed_governments(k)

        # prolonged losing war -> military dictatorship (survival, Bronze+)
        losing_war = any(
            not w.is_over
            and k in (w.side_a, w.side_b)
            and w.battles > 6
            and (
                k.total_population
                < (w.side_b if w.side_a is k else w.side_a).total_population * 0.5
            )
            for w in self.wars
        )
        if (
            losing_war
            and k.government.name != "Military Dictatorship"
            and k.tech_level >= 1
        ):
            new_gov = next(
                (g for g in civ_govs if g.name == "Military Dictatorship"), None
            )
            if new_gov is not None:
                self._change_government(k, new_gov, "losing_war")
                return

        # cooldown: no flip-flopping between golden-age shifts; traditional
        # realms take even longer to change their ways
        tradition_years = int(self._site_trait_avg(k, "tradition") * 10)
        if (
            self.month - k.last_gov_shift_month
            >= (GOV_SHIFT_COOLDOWN_YEARS + tradition_years) * 12
        ):
            self._golden_age_or_peace_shifts(k, civ_govs)

        # military dictatorship reverts after its war ends
        if k.government.name == "Military Dictatorship" and not k.at_war:
            won_war = any(
                w.is_over and w.victor is k for w in self.wars
            )
            if won_war or k.war_exhaustion == 0:
                new_gov = next(
                    (g for g in civ_govs if g.name == "Absolute Monarchy"),
                    None,
                )
                if new_gov is None:
                    # Stone Age has no monarchies: dissolve back into a council
                    new_gov = TRIBAL_GOVERNMENT
                self._change_government(
                    k, new_gov, "junta_ceded"
                )

    def _golden_age_or_peace_shifts(self, k: Kingdom, civ_govs: list) -> None:
        """Golden-age modernization and long-peace constitutional shifts.

        Options always come from the era-allowed set: a Bronze-Age golden
        age crowns kings and priests, not democrats.
        """
        # high prosperity + size + peace -> the most advanced allowed form
        prosperous = sum(s.popcap for s in k.sites) > 800 and not k.at_war
        if prosperous and k.government not in civ_govs:
            # prefer high-tech governments when the era allows them
            for want in ("Technocracy", "Democracy", "Constitutional monarchy",
                         "Plutocracy", "Fanatic Theocracy", "Theocracy",
                         "Absolute Monarchy", "Military Dictatorship"):
                options = [g for g in civ_govs if g.name == want]
                if options:
                    break
            else:
                options = [g for g in civ_govs if g is not k.government] or civ_govs
            if not options:
                return
            choice = options[int(self.rng.integers(0, len(options)))]
            self._change_government(k, choice, "golden_age")
            return
        # stagnant absolute monarchy under long peace -> constitutional monarchy
        long_peace = not k.at_war and k.war_exhaustion == 0 and self.month > 24 * 12
        is_absolute_monarchy = k.government.name == "Absolute Monarchy"
        if long_peace and is_absolute_monarchy:
            new_gov = next(
                (g for g in civ_govs if g.name == "Constitutional monarchy"), None
            )
            if new_gov is not None:
                self._change_government(k, new_gov, "decades_peace")

    def _change_government(self, k: Kingdom, new_gov, reason_key: str) -> None:
        """reason_key is one of the fixed reason ids (translated at render)."""
        old = k.government.name
        k.government = new_gov
        k.war_exhaustion = 0
        k.last_gov_shift_month = self.month
        # theocratic governments anchor the realm's faith
        if new_gov.name == "Theocracy":
            if k.faith not in ("Polytheism", "Monotheism"):
                k.faith = str(self.rng.choice(["Polytheism", "Monotheism"]))
        elif new_gov.name == "Fanatic Theocracy":
            if k.faith != "Monotheism":
                old_faith = k.faith
                k.faith = "Monotheism"
                self._ev(EventType.FAITH_CONVERSION, "faith_conversion",
                         {"kingdom": k.name, "old": old_faith, "faith": k.faith, "src": "fanatic zeal"},
                         (k.id,))
        elif new_gov.name == "Technocracy" and self.rng.random() < 0.45:
            if k.faith != "Skepticism":
                old_faith = k.faith
                k.faith = "Skepticism"
                self._ev(EventType.FAITH_CONVERSION, "faith_conversion",
                         {"kingdom": k.name, "old": old_faith, "faith": k.faith, "src": "enlightenment"},
                         (k.id,))
        self._ev(
            EventType.GOVERNMENT_CHANGED, "government_changed",
            {"kingdom": k.name, "old": old,
             "government": new_gov.name, "reason_key": reason_key},
            (k.id,),
        )

    # -- queries ---------------------------------------------------------------

    def kingdom_stats(self, k: Kingdom) -> dict:
        """Rich per-kingdom statistics for the UI inspector panel."""
        era_mult = (1.0 + ERA_ARMY_BONUS * k.tech_level) * (1 + k.mods.get("army", 0.0))
        army_size = k.total_population * k.government.militarization / 100
        military = army_size * k.race.strength * era_mult
        if k.hero:
            military *= 1.0 + (k.hero.strength - 1.0) * 0.5
        territory = int((self.territory_map() == k.id).sum())
        influence = int((self.influence_map() == k.id).sum())

        # war record
        wars_fought = [w for w in self.wars if k in (w.side_a, w.side_b)]
        # diplomacy: current relations with every other living kingdom
        relations_out = []
        for other in self.kingdoms:
            if other is k or not other.sites:
                continue
            rel = self.get_relation(k, other)
            relations_out.append(
                {"name": other.name, "relation": rel.name, "color": list(other.color)}
            )

        # history from the chronicle (recent events involving this kingdom)
        my_events = [
            {"month": e.month, "type": e.type.name, "text": e.text}
            for e in self.chronicle.of_kingdom(k.id)[-15:]
        ]

        pop_hist = k.population_history
        peak = max(pop_hist) if pop_hist else k.total_population

        return {
            "id": k.id,
            "name": k.name,
            "race": k.race.name,
            "government": k.government.name,
            "color": list(k.color),
            "era": ERA_NAMES[k.tech_level],
            "era_progress": round(min(1.0, k.tech_progress), 3),
            "population": k.total_population,
            "peak_population": peak,
            "sites": len(k.sites),
            "territory_tiles": territory,
            "influence_tiles": influence,
            "army_size": round(army_size, 1),
            "military_power": round(military, 1),
            "militarization_pct": k.government.militarization,
            "sites_list": [
                {"name": s.name, "x": s.x, "y": s.y, "category": s.category,
                 "population": s.population, "is_capital": s.is_capital}
                for s in k.sites
            ],
            "wars_won": k.wars_won,
            "wars_lost": k.wars_lost,
            "wars_total": len(wars_fought),
            "ongoing_wars": sum(1 for w in wars_fought if not w.is_over),
            "golden_age": k.golden_age_until > self.month,
            "drought": k.drought_until > self.month,
            "happiness": round(k.happiness, 1),
            "faith": k.faith,
            "temples": sorted(k.temples),
            "trade_routes": [
                {
                    "partner": next((q.name for q in self.kingdoms if q.id == pid), f"#{pid}"),
                    "partner_id": pid,
                    "months": k.trader_kindred.get(pid, 0),
                    "color": list(next((q.color for q in self.kingdoms if q.id == pid), (180, 180, 180))),
                }
                for pid in sorted(k.trade_routes)
            ],
            "civilized": k.civilized,
            "leader": (
                {
                    "name": k.leader.name,
                    "title": k.leader.title,
                    "reign_years": round(k.leader.reign_years(self.month), 1),
                    "battles_won": k.leader.battles_won,
                    "battles_lost": k.leader.battles_lost,
                }
                if k.leader else None
            ),
            "hero": (
                {
                    "name": k.hero.name,
                    "strength": round(k.hero.strength, 1),
                    "deeds": len(k.hero.battle_deeds),
                }
                if k.hero else None
            ),
            "relations": relations_out,
            "population_history": pop_hist,
            "recent_events": my_events,
            "techs": sorted(k.techs),
            "culture": {axis: round(w, 3) for axis, w in k.culture.items()},
        }

    def territory_map(self) -> np.ndarray:
        """(H, W) kingdom id (-1 none) for political overlay."""
        if hasattr(self, "_terr_cache_month") and self._terr_cache_month == self.month and hasattr(self, "_terr_cache"):
            return self._terr_cache
        out = self._occupancy - 1
        self._terr_cache = out
        self._terr_cache_month = self.month
        return out

    def influence_map(self) -> np.ndarray:
        """(H, W) kingdom id per land tile, claimed by the NEAREST site.

        Claim radius grows with tech era (a Medieval realm projects power
        much further than a Stone Age tribe). Produces province-like
        political maps (GDD: heatmap of the realms).
        """
        # cache per-month + tech state — called repeatedly per tick
        tech_sig = tuple((k.tech_level, k.mods.get("influence", 0.0)) for k in self.kingdoms)
        if hasattr(self, "_inf_cache_month") and self._inf_cache_month == self.month and hasattr(self, "_inf_cache_sig") and self._inf_cache_sig == tech_sig and hasattr(self, "_inf_cache"):
            return self._inf_cache
        h, w = self.world.height.shape
        best_d = np.full((h, w), np.iinfo(np.int32).max, dtype=np.int32)
        out = np.full((h, w), -1, dtype=np.int16)
        land = self.world.height > self.world.config.sea_level
        for k in self.kingdoms:
            if not k.sites:
                continue
            radius = int(
                ERA_INFLUENCE_RADIUS
                + ERA_INFLUENCE_PER_LEVEL * k.tech_level
                + k.mods.get("influence", 0.0)
            )
            for s in k.sites:
                x0, x1 = max(0, s.x - radius), min(w, s.x + radius + 1)
                y0, y1 = max(0, s.y - radius), min(h, s.y + radius + 1)
                yy, xx = np.mgrid[y0:y1, x0:x1]
                d = np.abs(xx - s.x) + np.abs(yy - s.y)
                window_d = np.where(d <= radius, d, np.iinfo(np.int32).max)
                # update where this site is closer than the current best
                upd = window_d < best_d[y0:y1, x0:x1]
                sub_d = best_d[y0:y1, x0:x1]
                sub_o = out[y0:y1, x0:x1]
                sub_d[upd] = window_d[upd]
                sub_o[upd] = k.id
        out[~land] = -1
        self._inf_cache = out
        self._inf_cache_month = self.month
        self._inf_cache_sig = tech_sig
        return out

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
