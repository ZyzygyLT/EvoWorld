"""FastAPI server: world generation + live simulation streaming + persistence.

Run (from worldsim/):
    uv run uvicorn worldsim.server.app:app --port 8420

Endpoints:
    GET  /api/worlds                — list saved worlds
    POST /api/worlds                — generate (+ optional pre-simulated months), persist
    GET  /api/worlds/{id}           — full state snapshot (JSON)
    DELETE /api/worlds/{id}         — delete a saved world
    WS   /api/ws/{world_id}         — live month-by-month streaming (deterministic replay)
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import Column, Float, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from worldsim.data.loaders import load_governments, load_races
from worldsim.export.state import engine_to_dict
from worldsim.sim.engine import SimEngine
from worldsim.worldgen import WorldConfig
from worldsim.worldgen.pipeline import generate_world

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
DB_PATH = Path(__file__).resolve().parent / "worlds.db"

engine_db = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine_db)


class Base(DeclarativeBase):
    pass


class WorldRow(Base):
    __tablename__ = "worlds"

    id = Column(Integer, primary_key=True, autoincrement=True)
    seed = Column(Integer, index=True)
    width = Column(Integer)
    height = Column(Integer)
    months_simulated = Column(Integer)
    land_pct = Column(Float)
    state_json = Column(Text)  # full engine_to_dict snapshot
    created_at = Column(String)


Base.metadata.create_all(engine_db)

app = FastAPI(title="worldsim", version="0.2.0")


class CreateWorldRequest(BaseModel):
    seed: int = Field(default=42)
    width: int = Field(default=200, ge=32, le=1024)
    height: int = Field(default=80, ge=32, le=512)
    months: int = Field(default=0, ge=0, le=12000)
    n_civilized: int = Field(default=2, ge=0, le=8)
    n_tribal: int = Field(default=2, ge=0, le=8)
    n_beasts: int = Field(default=2, ge=0, le=8)


def _build_engine(
    seed: int, width: int, height: int,
    n_civilized: int = 2, n_tribal: int = 2, n_beasts: int = 2,
) -> SimEngine:
    cfg = WorldConfig(seed=seed, width=width, height=height)
    world = generate_world(cfg)
    races = load_races(DATA_DIR / "Races.txt")
    govs = load_governments(DATA_DIR / "CivilizedGovernment.txt")
    return SimEngine(
        world, races, govs, seed=seed,
        n_civilized=n_civilized, n_tribal=n_tribal, n_beasts=n_beasts,
    )


@app.get("/api/meta")
def get_meta() -> dict:
    """Static game data: government types (with descriptions) and races."""
    races = load_races(DATA_DIR / "Races.txt")
    govs = load_governments(DATA_DIR / "CivilizedGovernment.txt")
    return {
        "governments": [
            {
                "name": g.name,
                "description": g.description,
                "aggressiveness": g.aggressiveness,
                "militarization": g.militarization,
                "tech_bonus": g.tech_bonus,
            }
            for g in govs
        ]
        + [
            {
                "name": "Tribal",
                "description": "Council of elders; loose authority over kin groups.",
                "aggressiveness": 2,
                "militarization": 50,
                "tech_bonus": 0,
            }
        ],
        "races": [
            {
                "name": r.name,
                "preferred_biomes": list(r.preferred_biomes),
                "strength": r.strength,
                "aggressiveness": r.aggressiveness,
                "form": r.form,
            }
            for r in races
        ],
    }


@app.get("/api/worlds")
def list_worlds() -> list[dict]:
    with SessionLocal() as db:  # type: ignore[call-arg]
        rows = db.scalars(
            select(WorldRow).order_by(WorldRow.id.desc()).limit(50)
        ).all()
        return [
            {
                "id": r.id,
                "seed": r.seed,
                "months_simulated": r.months_simulated,
                "land_pct": r.land_pct,
                "created_at": r.created_at,
            }
            for r in rows
        ]


@app.post("/api/worlds")
def create_world(req: CreateWorldRequest) -> dict:
    """Generate a world, optionally pre-simulate `months`, persist, return id+state."""
    eng = _build_engine(
        req.seed, req.width, req.height,
        req.n_civilized, req.n_tribal, req.n_beasts,
    )
    for _ in range(req.months):
        eng.tick()
    state = engine_to_dict(eng)

    with SessionLocal() as db:  # type: ignore[call-arg]
        row = WorldRow(
            seed=req.seed,
            width=req.width,
            height=req.height,
            months_simulated=req.months,
            land_pct=float((eng.world.height > 0.2).mean() * 100),
            state_json=json.dumps(state),
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"id": row.id, "state": state}


def _load_row(world_id: int) -> WorldRow:
    with SessionLocal() as db:  # type: ignore[call-arg]
        row = db.get(WorldRow, world_id)
        if row is None:
            raise HTTPException(404, f"world {world_id} not found")
        return row


@app.get("/api/worlds/{world_id}")
def get_world(world_id: int) -> dict:
    return json.loads(_load_row(world_id).state_json)


@app.delete("/api/worlds/{world_id}")
def delete_world(world_id: int) -> dict:
    with SessionLocal() as db:  # type: ignore[call-arg]
        row = db.get(WorldRow, world_id)
        if row is None:
            raise HTTPException(404, f"world {world_id} not found")
        db.delete(row)
        db.commit()
        return {"deleted": world_id}


def _tick_payload(eng: SimEngine) -> dict:
    """Compact per-month payload for the live stream."""
    return {
        "type": "tick",
        "month": eng.month,
        "kingdoms": [
            {
                "id": k.id,
                "name": k.name,
                "race": k.race.name,
                "government": k.government.name,
                "population": k.total_population,
                "sites": [
                    {"name": s.name, "x": s.x, "y": s.y,
                     "population": s.population, "is_capital": s.is_capital}
                    for s in k.sites
                ],
                "leader": (
                    {"name": k.leader.name, "title": k.leader.title,
                     "reign_years": round(k.leader.reign_years(eng.month), 1),
                     "battles_won": k.leader.battles_won,
                     "battles_lost": k.leader.battles_lost}
                    if k.leader else None
                ),
                "hero": (
                    {"name": k.hero.name, "strength": round(k.hero.strength, 1),
                     "deeds": len(k.hero.battle_deeds)}
                    if k.hero else None
                ),
                "at_war": k.at_war,
                "color": list(k.color),
            }
            for k in eng.kingdoms
        ],
        "wars_ongoing": [
            {"name": w.name, "a": w.side_a.name, "b": w.side_b.name,
             "battles": w.battles, "dead": w.total_dead}
            for w in eng.wars if not w.is_over
        ],
        "beasts": [
            {"name": b.name, "x": b.x, "y": b.y, "alive": b.hp > 0}
            for b in eng.beasts
        ],
        "events": [
            {"month": e.month, "type": e.type.name, "text": e.text}
            for e in eng.chronicle.recent(12)
        ],
    }


@app.websocket("/api/ws/{world_id}")
async def ws_world(ws: WebSocket, world_id: int) -> None:
    """Stream a saved world live: deterministic replay + tick streaming.

    Client messages (optional, JSON):
        {"speed": n}   — months per push (1..120)
        {"pause": true/false}
    """
    await ws.accept()
    try:
        row = _load_row(world_id)
        state = json.loads(row.state_json)
        # deterministic replay: rebuild from seed and fast-forward
        eng = _build_engine(row.seed, row.width, row.height)
        for _ in range(state["month"]):
            eng.tick()

        speed = 1
        paused = False
        while True:
            # drain any client messages without blocking the stream
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=0.01)
                if "speed" in msg:
                    speed = max(1, min(120, int(msg["speed"])))
                if "pause" in msg:
                    paused = bool(msg["pause"])
            except TimeoutError:
                pass

            if not paused:
                for _ in range(speed):
                    eng.tick()
            await ws.send_json(_tick_payload(eng))
            # one push every half second: x1 = 2 months/sec (a year per 6s),
            # x48 stays a real fast-forward; readable by humans (GDD pillar 1)
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        return


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8420)
