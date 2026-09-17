"""Canonical world clock: one shared engine per world, many spectators.

Each WorldRunner owns THE engine instance for a saved world. Spectators
subscribe to its broadcast channel, so every user watching the same world
sees the same month at the same time (required for multiplayer predictions).

Also resolves prophecy deadlines against the chronicle as time advances.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

from worldsim.data.loaders import load_governments, load_races
from worldsim.server.prediction_resolver import resolve_pending
from worldsim.sim.engine import SimEngine
from worldsim.worldgen import WorldConfig
from worldsim.worldgen.pipeline import generate_world

PUSH_INTERVAL = 0.5  # seconds between broadcasts


@dataclass
class RunnerConfig:
    seed: int
    width: int
    height: int


class WorldRunner:
    """A live simulation shared by all connected clients."""

    def __init__(self, world_id: int, seed: int, width: int, height: int,
                 enabled_races: list[str] | None = None,
                 sea_level: float = 0.2) -> None:
        self.world_id = world_id
        self.enabled_races = enabled_races
        self.sea_level = sea_level
        self.eng = self._build_engine(seed, width, height)
        self.speed = 1
        self.paused = True
        self.subscribers: dict[int, dict] = {}  # id(ws) -> {ws, selected}
        self._task: asyncio.Task | None = None
        self._last_score_month = 0  # league scoring cursor

    def _build_engine(self, seed: int, width: int, height: int) -> SimEngine:
        cfg = WorldConfig(seed=seed, width=width, height=height,
                          sea_level=self.sea_level)
        world = generate_world(cfg)
        data_dir = self._data_dir()
        races = load_races(data_dir / "Races.txt")
        govs = load_governments(data_dir / "CivilizedGovernment.txt")
        return SimEngine(world, races, govs, seed=seed,
                         enabled_races=self.enabled_races)

    @staticmethod
    def _data_dir():
        from pathlib import Path

        return Path(__file__).resolve().parents[3] / "data"

    def subscribe(self, ws) -> None:
        self.subscribers[id(ws)] = {"ws": ws, "selected": None}

    def unsubscribe(self, ws) -> None:
        self.subscribers.pop(id(ws), None)

    def set_selected(self, ws, selected_id: int | None) -> None:
        sub = self.subscribers.get(id(ws))
        if sub is not None:
            sub["selected"] = selected_id

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(PUSH_INTERVAL)
            if self.paused:
                continue
            try:
                for _ in range(self.speed):
                    self.eng.tick()
                resolved = resolve_pending(self.world_id, self.eng)
                if resolved:
                    if len(resolved) == 1:
                        r = resolved[0]
                        self._broadcast_system(
                            f"★ Prophecy resolved: {r['user_name']} — {r['text']} "
                            f"(+{r['points']} pts)"
                        )
                    else:
                        lines = "\n".join(
                            f"★ {r['user_name']}: {r['text']} (+{r['points']})"
                            for r in resolved
                        )
                        self._broadcast_system(f"{len(resolved)} prophecies resolved:\n{lines}")
                self._broadcast_league_scores()
                self._check_season_end()
                self._broadcast_tick()
            except Exception:
                logger.exception(
                    "tick failed for world %s at month %s", self.world_id, self.eng.month,
                )

    # -- broadcast ---------------------------------------------------------

    def _broadcast_tick(self) -> None:
        from worldsim.server.app import _tick_payload

        payload = json.dumps(_tick_payload(self.eng))
        self._send_all(payload)
        # per-socket stats channel: each spectator's selected kingdom stats
        # are pushed right after the shared tick, so a selection never gets
        # clobbered by the next broadcast (the panel-stays-hidden bug)
        for sub in list(self.subscribers.values()):
            sel = sub["selected"]
            if sel is None:
                continue
            k = next((k for k in self.eng.kingdoms if k.id == sel), None)
            if k is None:
                continue
            stats_msg = json.dumps(
                {"type": "stats", "stats": self.eng.kingdom_stats(k)}
            )
            try:
                asyncio.create_task(sub["ws"].send_text(stats_msg))
            except Exception:
                self.subscribers.pop(id(sub["ws"]), None)

    def _broadcast_system(self, text: str) -> None:
        payload = json.dumps({"type": "system", "text": text})
        self._send_all(payload)

    def _broadcast_league_scores(self) -> None:
        """Score claims since the last cursor and toast the owners (batched)."""
        from worldsim.server.league import score_new_events

        messages = score_new_events(self.world_id, self.eng, self._last_score_month)
        self._last_score_month = self.eng.month
        if len(messages) == 1:
            self._broadcast_system(messages[0])
        elif messages:
            self._broadcast_system("\n".join(messages))

    def _check_season_end(self) -> None:
        """Freeze league worlds at season_years and crown the champion."""
        from worldsim.server.league import maybe_end_season

        msg = maybe_end_season(self.world_id, self.eng)
        if msg is not None:
            self.paused = True
            self._broadcast_system(msg)

    def _send_all(self, payload: str) -> None:
        dead = []
        for key, sub in list(self.subscribers.items()):
            try:
                asyncio.create_task(sub["ws"].send_text(payload))
            except Exception:
                dead.append(key)
        for key in dead:
            self.subscribers.pop(key, None)


class RunnerRegistry:
    """world_id -> WorldRunner singleton (per server process)."""

    def __init__(self) -> None:
        self._runners: dict[int, WorldRunner] = {}

    def get(self, world_id: int, seed: int, width: int, height: int,
            enabled_races: list[str] | None = None,
            sea_level: float = 0.2) -> WorldRunner:
        # double-checked with a simple lock-free guard: uvicorn runs single-threaded
        # event loop, so concurrent coroutines cannot interleave inside this sync method
        if world_id not in self._runners:
            runner = WorldRunner(world_id, seed, width, height, enabled_races, sea_level)
            # re-check in case a concurrent task inserted between check and assignment
            self._runners.setdefault(world_id, runner)
        return self._runners[world_id]

    def get_existing(self, world_id: int) -> WorldRunner | None:
        return self._runners.get(world_id)


registry = RunnerRegistry()
