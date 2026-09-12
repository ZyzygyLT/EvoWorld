"""Canonical world clock: one shared engine per world, many spectators.

Each WorldRunner owns THE engine instance for a saved world. Spectators
subscribe to its broadcast channel, so every user watching the same world
sees the same month at the same time (required for multiplayer predictions).

Also resolves prophecy deadlines against the chronicle as time advances.
"""

from __future__ import annotations

import asyncio
import json
import weakref
from dataclasses import dataclass

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

    def __init__(self, world_id: int, seed: int, width: int, height: int) -> None:
        self.world_id = world_id
        self.eng = self._build_engine(seed, width, height)
        self.speed = 1
        self.paused = True
        self.subscribers: list[weakref.ref] = []
        self._task: asyncio.Task | None = None
        self.selected_stats = None  # replaced by per-connection selection
        self._lock = asyncio.Lock()

    def _build_engine(self, seed: int, width: int, height: int) -> SimEngine:
        cfg = WorldConfig(seed=seed, width=width, height=height)
        world = generate_world(cfg)
        data_dir = self._data_dir()
        races = load_races(data_dir / "Races.txt")
        govs = load_governments(data_dir / "CivilizedGovernment.txt")
        return SimEngine(world, races, govs, seed=seed)

    @staticmethod
    def _data_dir():
        from pathlib import Path

        return Path(__file__).resolve().parents[3] / "data"

    def subscribe(self, ws) -> None:
        self.subscribers.append(weakref.ref(ws))

    def unsubscribe(self, ws) -> None:
        self.subscribers = [s for s in self.subscribers if s() is not ws and s() is not None]

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(PUSH_INTERVAL)
            if self.paused:
                continue
            for _ in range(self.speed):
                self.eng.tick()
            resolved = resolve_pending(self.world_id, self.eng)
            if resolved:
                for r in resolved:
                    self._broadcast_system(
                        f"★ Prophecy resolved: {r['user_name']} — {r['text']} "
                        f"(+{r['points']} pts)"
                    )
            self._broadcast_tick()

    # -- broadcast ---------------------------------------------------------

    def _broadcast_tick(self) -> None:
        from worldsim.server.app import _tick_payload

        payload = json.dumps(_tick_payload(self.eng))
        self._send_all(payload)

    def _broadcast_system(self, text: str) -> None:
        payload = json.dumps({"type": "system", "text": text})
        self._send_all(payload)

    def _send_all(self, payload: str) -> None:
        dead = []
        for ref in self.subscribers:
            ws = ref()
            if ws is None:
                dead.append(ref)
                continue
            try:
                asyncio.create_task(ws.send_text(payload))
            except Exception:
                dead.append(ref)
        for ref in dead:
            self.subscribers = [s for s in self.subscribers if s is not ref]


class RunnerRegistry:
    """world_id -> WorldRunner singleton (per server process)."""

    def __init__(self) -> None:
        self._runners: dict[int, WorldRunner] = {}

    def get(self, world_id: int, seed: int, width: int, height: int) -> WorldRunner:
        if world_id not in self._runners:
            runner = WorldRunner(world_id, seed, width, height)
            self._runners[world_id] = runner
        return self._runners[world_id]

    def get_existing(self, world_id: int) -> WorldRunner | None:
        return self._runners.get(world_id)


registry = RunnerRegistry()
