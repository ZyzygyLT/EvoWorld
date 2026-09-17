"""FastAPI server: world generation + live simulation streaming + multiplayer.

Run (from worldsim/):
    uv run uvicorn worldsim.server.app:app --port 8420

Endpoints:
    GET  /api/meta                          — government/race static data
    GET  /api/worlds                        — list saved worlds
    POST /api/worlds                        — generate (+ optional pre-simulated months)
    GET  /api/worlds/{id}                   — full state snapshot (JSON)
    DELETE /api/worlds/{id}                 — delete a saved world
    GET  /api/worlds/{id}/stats/{kid}       — rich stats for one kingdom
    GET  /api/worlds/{id}/predictions       — prophecies for this world
    POST /api/worlds/{id}/predictions       — seal a new prophecy
    GET  /api/worlds/{id}/leaderboard       — per-world scores
    GET  /api/worlds/{id}/chat              — chat history (in-world month stamped)
    POST /api/worlds/{id}/chat              — say something in the room
    WS   /api/ws/{world_id}                 — shared-clock live stream + chat/system

Multiplayer model: shared WorldRunner per world (canonical clock) +
prophecy game (users read the emergent history and bet on outcomes) +
chat presence. The simulation stays fully autonomous (GDD pillars intact).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path

logger = logging.getLogger(__name__)

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import select

from worldsim.data.loaders import load_governments, load_races
from worldsim.export.state import engine_to_dict
from worldsim.server import db
from worldsim.server.db import (
    Base,
    ChatRow,
    WorldRow,
    engine_db,
)
from worldsim.server.prediction_resolver import (
    KINDS,
    create_prediction,
    leaderboard,
    list_predictions,
)
from worldsim.server.runner import registry
from worldsim.sim.engine import SimEngine
from worldsim.sim.models import EventType, TRIBAL_GOVERNMENT
from worldsim.worldgen import WorldConfig
from worldsim.worldgen.pipeline import generate_world

DATA_DIR = Path(__file__).resolve().parents[3] / "data"

Base.metadata.create_all(engine_db)

app = FastAPI(title="worldsim", version="0.4.0")


class CreateWorldRequest(BaseModel):
    seed: int = Field(default=42)
    width: int = Field(default=200, ge=32, le=1024)
    height: int = Field(default=80, ge=32, le=512)
    months: int = Field(default=0, ge=0, le=12000)
    n_civilized: int = Field(default=2, ge=0, le=8)
    n_tribal: int = Field(default=2, ge=0, le=8)
    n_beasts: int = Field(default=2, ge=0, le=8)
    name: str = Field(default="", max_length=48)
    mode: str = Field(default="single")  # single | league
    season_years: int = Field(default=300, ge=12, le=2000)
    enabled_races: list[str] | None = Field(default=None)
    kingdom_races: list[str] | None = Field(default=None, max_length=16)
    sea_level: float = Field(default=0.2, ge=0.05, le=0.5)


class PredictionRequest(BaseModel):
    kind: str
    params: dict = Field(default_factory=dict)
    # anonymous identity: token lives in the browser, name is chosen by user
    user_token: str = Field(min_length=8, max_length=64)
    user_name: str = Field(min_length=1, max_length=24)


class ChatRequest(BaseModel):
    user_token: str = Field(min_length=8, max_length=64)
    user_name: str = Field(min_length=1, max_length=24)
    text: str = Field(min_length=1, max_length=300)


class ClaimRequest(BaseModel):
    kingdom_id: int
    user_token: str = Field(min_length=8, max_length=64)
    user_name: str = Field(min_length=1, max_length=24)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=24)
    password: str = Field(min_length=4, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


def _auth_user(authorization: str | None):
    """Resolve 'Bearer <token>' to a user row (None = anonymous)."""
    from worldsim.server import auth as authmod

    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authmod.user_from_token(authorization[7:].strip())


@app.post("/api/auth/register")
def api_register(req: RegisterRequest) -> dict:
    from worldsim.server import auth as authmod

    result = authmod.register(req.username, req.password)
    if isinstance(result, str):
        raise HTTPException(400, result)
    return result


@app.post("/api/auth/login")
def api_login(req: LoginRequest) -> dict:
    from worldsim.server import auth as authmod

    result = authmod.login(req.username, req.password)
    if isinstance(result, str):
        raise HTTPException(401, result)
    return result


@app.get("/api/auth/me")
def api_me(authorization: str | None = Header(default=None)) -> dict:
    user = _auth_user(authorization)
    if user is None:
        raise HTTPException(401, "not logged in")
    return {"username": user.username, "user_id": user.id}


@app.post("/api/auth/logout")
def api_logout(authorization: str | None = Header(default=None)) -> dict:
    from worldsim.server import auth as authmod

    if authorization and authorization.startswith("Bearer "):
        authmod.logout(authorization[7:].strip())
    return {"ok": True}


def _build_engine(
    seed: int, width: int, height: int,
    n_civilized: int = 2, n_tribal: int = 2, n_beasts: int = 2,
    enabled_races: list[str] | None = None,
    kingdom_races: list[str] | None = None,
    sea_level: float = 0.2,
) -> SimEngine:
    cfg = WorldConfig(seed=seed, width=width, height=height, sea_level=sea_level)
    world = generate_world(cfg)
    races = load_races(DATA_DIR / "Races.txt")
    govs = load_governments(DATA_DIR / "CivilizedGovernment.txt")
    return SimEngine(
        world, races, govs, seed=seed,
        n_civilized=n_civilized, n_tribal=n_tribal, n_beasts=n_beasts,
        enabled_races=enabled_races,
        kingdom_races=kingdom_races,
    )


@app.get("/api/meta")
def get_meta() -> dict:
    """Static game data: government types (with descriptions) and races."""
    races = load_races(DATA_DIR / "Races.txt")
    govs = load_governments(DATA_DIR / "CivilizedGovernment.txt")
    out = {
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
                "name": TRIBAL_GOVERNMENT.name,
                "description": TRIBAL_GOVERNMENT.description,
                "aggressiveness": TRIBAL_GOVERNMENT.aggressiveness,
                "militarization": TRIBAL_GOVERNMENT.militarization,
                "tech_bonus": TRIBAL_GOVERNMENT.tech_bonus,
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
        "prediction_kinds": KINDS,
    }
    return out


# -- worlds ----------------------------------------------------------------------


@app.get("/api/worlds")
def list_worlds(
    mine: bool = False,
    authorization: str | None = Header(default=None),
) -> list[dict]:
    owner_id = None
    if mine:
        user = _auth_user(authorization)
        if user is None:
            raise HTTPException(401, "log in to list your worlds")
        owner_id = user.id
    with db.SessionLocal() as db_:  # type: ignore[call-arg]
        q = select(WorldRow).order_by(WorldRow.id.desc()).limit(50)
        if owner_id is not None:
            q = q.where(WorldRow.owner_user_id == owner_id)
        rows = db_.scalars(q).all()
        return [
            {
                "id": r.id,
                "seed": r.seed,
                "months_simulated": r.months_simulated,
                "land_pct": r.land_pct,
                "created_at": r.created_at,
                "name": r.name or f"World {r.seed}",
                "mode": r.mode or "single",
                "season_years": r.season_years or 300,
                "season_over": bool(r.season_over),
                "mine": owner_id is not None and r.owner_user_id == owner_id,
            }
            for r in rows
        ]


@app.post("/api/worlds")
def create_world(
    req: CreateWorldRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    """Generate a world, optionally pre-simulate `months`, persist, return id+state."""
    if req.mode not in ("single", "league"):
        raise HTTPException(400, "mode must be 'single' or 'league'")
    if req.enabled_races:
        known = {r.name for r in load_races(DATA_DIR / "Races.txt")}
        bad = [n for n in req.enabled_races if n not in known]
        # only reject if SOME names are valid but others aren't; all-nonsense
        # is the fallback path (engine restores the full set)
        if bad and len(bad) < len(req.enabled_races):
            raise HTTPException(400, f"unknown race(s): {', '.join(bad)}")
    if req.kingdom_races:
        known = {r.name for r in load_races(DATA_DIR / "Races.txt")}
        bad = [n for n in req.kingdom_races if n not in known]
        if bad:
            raise HTTPException(400, f"unknown kingdom race(s): {', '.join(bad)}")
        # only civilized/tribal races allowed for kingdoms, not beasts
        form_map = {r.name: r.form for r in load_races(DATA_DIR / "Races.txt")}
        bad_form = [n for n in req.kingdom_races if form_map.get(n) == "site"]
        if bad_form:
            raise HTTPException(400, f"beast races not allowed for kingdoms: {', '.join(bad_form)}")
    owner = _auth_user(authorization)
    eng = _build_engine(
        req.seed, req.width, req.height,
        req.n_civilized, req.n_tribal, req.n_beasts,
        req.enabled_races,
        req.kingdom_races, req.sea_level,
    )
    for _ in range(req.months):
        eng.tick()
    state = engine_to_dict(eng)

    with db.SessionLocal() as db_:  # type: ignore[call-arg]
        row = WorldRow(
            seed=req.seed,
            width=req.width,
            height=req.height,
            months_simulated=req.months,
            land_pct=float((eng.world.height > req.sea_level).mean() * 100),
            state_json=json.dumps(state),
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            name=req.name or f"World {req.seed}",
            mode=req.mode,
            season_years=req.season_years,
            season_over=0,
            enabled_races_json=json.dumps(req.enabled_races or []),
            owner_user_id=owner.id if owner else None,
            sea_level=req.sea_level,
        )
        db_.add(row)
        db_.commit()
        db_.refresh(row)
        # seed the runner so POST's engine == canonical runner engine
        runner = registry.get(
            row.id, req.seed, req.width, req.height,
            req.enabled_races, req.sea_level,
        )
        runner.eng = eng
        return {"id": row.id, "state": state}


def _load_row(world_id: int) -> WorldRow:
    with db.SessionLocal() as db_:  # type: ignore[call-arg]
        row = db_.get(WorldRow, world_id)
        if row is None:
            raise HTTPException(404, f"world {world_id} not found")
        return row


@app.get("/api/worlds/{world_id}")
def get_world(world_id: int) -> dict:
    return json.loads(_load_row(world_id).state_json)


@app.delete("/api/worlds/{world_id}")
def delete_world(
    world_id: int,
    authorization: str | None = Header(default=None),
) -> dict:
    user = _auth_user(authorization)
    with db.SessionLocal() as db_:  # type: ignore[call-arg]
        row = db_.get(WorldRow, world_id)
        if row is None:
            raise HTTPException(404, f"world {world_id} not found")
        # auth gate: owner-only; anonymous worlds are open to anyone
        if row.owner_user_id is not None:
            if user is None or user.id != row.owner_user_id:
                raise HTTPException(403, "only the owner may delete this world")
        db_.delete(row)
        db_.commit()
        return {"deleted": world_id}


@app.get("/api/worlds/{world_id}/stats/{kingdom_id}")
def get_kingdom_stats(world_id: int, kingdom_id: int) -> dict:
    """Rich stats for one kingdom (deterministic replay to the saved month)."""
    row = _load_row(world_id)
    state = json.loads(row.state_json)
    try:
        enabled = json.loads(row.enabled_races_json or "[]") or None
    except (json.JSONDecodeError, TypeError):
        enabled = None
    sea = row.sea_level if row.sea_level is not None else 0.2
    eng = _build_engine(row.seed, row.width, row.height,
                        enabled_races=enabled, sea_level=sea)
    for _ in range(state["month"]):
        eng.tick()
    k = next((k for k in eng.kingdoms if k.id == kingdom_id), None)
    if k is None:
        raise HTTPException(404, f"kingdom {kingdom_id} not found")
    return eng.kingdom_stats(k)


# -- multiplayer: predictions + chat ----------------------------------------------


@app.get("/api/worlds/{world_id}/predictions")
def get_predictions(world_id: int) -> list[dict]:
    return list_predictions(world_id)


@app.post("/api/worlds/{world_id}/predictions")
def post_prediction(world_id: int, req: PredictionRequest) -> dict:
    _load_row(world_id)  # 404 guard
    # seal with the CANONICAL current month (prophecies can't cover the past)
    runner = registry.get_existing(world_id)
    month = runner.eng.month if runner else 0
    row = create_prediction(
        world_id, req.user_token, req.user_name, req.kind, req.params, month
    )
    if row is None:
        raise HTTPException(400, f"unknown prediction kind {req.kind!r}")
    return row


@app.get("/api/worlds/{world_id}/leaderboard")
def get_leaderboard(world_id: int) -> list[dict]:
    return leaderboard(world_id)


@app.get("/api/worlds/{world_id}/chat")
def get_chat(world_id: int) -> list[dict]:
    with db.SessionLocal() as db_:  # type: ignore[call-arg]
        rows = db_.scalars(
            select(ChatRow)
            .where(ChatRow.world_id == world_id)
            .order_by(ChatRow.id.desc())
            .limit(80)
        ).all()
        return [
            {
                "user_name": r.user_name,
                "text": r.text,
                "month": r.month,
            }
            for r in reversed(list(rows))
        ]


@app.post("/api/worlds/{world_id}/chat")
def post_chat(world_id: int, req: ChatRequest) -> dict:
    _load_row(world_id)
    runner = registry.get_existing(world_id)
    month = runner.eng.month if runner else 0
    with db.SessionLocal() as db_:  # type: ignore[call-arg]
        row = ChatRow(
            world_id=world_id,
            user_token=req.user_token,
            user_name=req.user_name,
            text=req.text,
            month=month,
        )
        db_.add(row)
        db_.commit()
        msg = {"user_name": req.user_name, "text": req.text, "month": month}
    if runner:
        runner._broadcast_system(f"💬 {req.user_name}: {req.text}")
    return msg


# -- dynasty league: claims + standings ------------------------------------------


@app.get("/api/worlds/{world_id}/claims")
def get_claims(world_id: int) -> list[dict]:
    from worldsim.server.league import get_claims as _claims

    _load_row(world_id)
    return _claims(world_id)


@app.post("/api/worlds/{world_id}/claims")
def post_claim(
    world_id: int, req: ClaimRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    from worldsim.server.league import claim_kingdom

    user = _auth_user(authorization)
    result = claim_kingdom(
        world_id, req.kingdom_id, req.user_token, req.user_name,
        _canonical_month(world_id),
        user_id=user.id if user else None,
    )
    if isinstance(result, str):  # error string
        raise HTTPException(400, result)
    runner = registry.get_existing(world_id)
    if runner:
        runner._broadcast_system(
            f"👑 {req.user_name} claimed {result.get('kingdom_name', 'a realm')}!"
        )
    return result


@app.get("/api/worlds/{world_id}/standings")
def get_standings(world_id: int) -> list[dict]:
    from worldsim.server.league import standings

    _load_row(world_id)
    return standings(world_id)


def _canonical_month(world_id: int) -> int:
    """Month of the shared clock (or the saved snapshot if never streamed)."""
    runner = registry.get_existing(world_id)
    if runner:
        return runner.eng.month
    with db.SessionLocal() as db_:  # type: ignore[call-arg]
        row = db_.get(WorldRow, world_id)
        return row.months_simulated if row else 0


# -- live stream ------------------------------------------------------------------


def _tick_payload(eng: SimEngine) -> dict:
    """Compact per-month payload for the shared live stream.

    Per-spectator selection stats go through the private 'stats' channel
    (see runner.WorldRunner._broadcast_tick), not through this payload.
    """
    heat = eng.battle_heat()
    return {
        "type": "tick",
        "month": eng.month,
        "kingdoms": [
            {
                "id": k.id,
                "name": k.name,
                "race": k.race.name,
                "government": k.government.name,
                "population": int(k.total_population),
                "sites": [
                    {"name": s.name, "x": s.x, "y": s.y,
                     "population": int(s.population), "is_capital": s.is_capital}
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
                "population_history": [int(p) for p in k.population_history],
                "tech_level": k.tech_level,
                "army_size": round(eng.kingdom_stats(k)["army_size"], 1),
                "military_power": round(eng.kingdom_stats(k)["military_power"], 1),
                "militarization_pct": k.government.militarization,
                "techs": sorted(k.techs),
                "golden_age": k.golden_age_until > eng.month,
                "drought": k.drought_until > eng.month,
                "happiness": round(k.happiness, 1),
                "faith": k.faith,
                "temples": sorted(k.temples),
                "trade_routes": [
                    {"partner_id": pid,
                     "partner": next((q.name for q in eng.kingdoms if q.id == pid), f"#{pid}"),
                     "months": k.trader_kindred.get(pid, 0)}
                    for pid in sorted(k.trade_routes)
                ],
                "founded_month": k.founded_month,
            }
            for k in eng.kingdoms
        ],
        "influence": eng.influence_map().tolist(),
        "wars_ongoing": [
            {"name": w.name, "a": w.side_a.name, "b": w.side_b.name,
             "battles": w.battles, "dead": w.total_dead}
            for w in eng.wars if not w.is_over
        ],
        "beasts": [
            {"name": b.name, "x": b.x, "y": b.y, "alive": b.hp > 0}
            for b in eng.beasts
        ],
        "fallen": eng.fallen,
        "trade_routes": [
            {"a": min(a.name, b.name), "b": max(a.name, b.name),
             "a_id": min(a.id, b.id), "b_id": max(a.id, b.id),
             "months": min(a.trader_kindred.get(b.id, 0), b.trader_kindred.get(a.id, 0))}
            for a in eng.kingdoms for b in eng.kingdoms
            if b.id in a.trade_routes and a.id < b.id
        ],
        "battle_heat": {
            "max": float(heat.max()),
            "cells": [
                {"x": int(x), "y": int(y), "v": float(heat[y, x])}
                for y, x in zip(heat.nonzero()[0], heat.nonzero()[1], strict=True)
            ],
        },
        "events": [
            {"month": e.month, "type": e.type.name, "text": e.text,
             "msg_key": e.msg_key, "params": e.params}
            for e in eng.chronicle.recent(40)
        ],
        # full milestone timeline for the Hitos tab (rare by design)
        "milestones": [
            {"month": e.month, "type": e.type.name, "text": e.text,
             "msg_key": e.msg_key, "params": e.params}
            for e in eng.chronicle.events
            if e.type == EventType.MILESTONE
        ],
        # recent finished wars for records (longest war etc.)
        "wars_recent": [
            {"name": w.name, "a": w.side_a.name, "b": w.side_b.name,
             "battles": w.battles, "dead": w.total_dead,
             "victor": w.victor.name if w.victor else None,
             "start_month": w.start_month, "end_month": w.end_month}
            for w in eng.wars if w.is_over
        ][-10:],
    }


@app.websocket("/api/ws/{world_id}")
async def ws_world(ws: WebSocket, world_id: int) -> None:
    """Shared-clock stream: every spectator of a world sees the same month.

    Client messages (optional, JSON):
        {"speed": n}      — months per push (1..120); last writer wins
        {"pause": bool}   — anyone can pause/resume the shared clock
        {"selected": id}  — private: stats streamed back just for this socket
    """
    await ws.accept()
    try:
        row = _load_row(world_id)
        try:
            enabled = json.loads(row.enabled_races_json or "[]") or None
        except (json.JSONDecodeError, TypeError):
            enabled = None
        sea = row.sea_level if row.sea_level is not None else 0.2
        runner = registry.get(world_id, row.seed, row.width, row.height, enabled, sea)
        # fast-forward the shared engine to the saved month on first attach
        if runner.eng.month == 0 and row.months_simulated > 0:
            for _ in range(row.months_simulated):
                runner.eng.tick()
        runner.subscribe(ws)
        await runner.start()

        selected_id: int | None = None
        # send an immediate snapshot so the client doesn't wait 0.5s
        await ws.send_text(json.dumps(_tick_payload(runner.eng)))

        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=0.01)
                if "speed" in msg:
                    runner.speed = max(1, min(120, int(msg["speed"])))
                if "pause" in msg:
                    runner.paused = bool(msg["pause"])
                if "selected" in msg:
                    selected_id = (
                        int(msg["selected"]) if msg["selected"] is not None else None
                    )
                    runner.set_selected(ws, selected_id)
                    if selected_id is None:
                        await ws.send_text(json.dumps({"type": "stats", "stats": None}))
                    else:
                        k = next(
                            (k for k in runner.eng.kingdoms if k.id == selected_id), None
                        )
                        if k is not None:
                            await ws.send_text(
                                json.dumps(
                                    {"type": "stats", "stats": runner.eng.kingdom_stats(k)}
                                )
                            )
            except TimeoutError:
                pass
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        runner.unsubscribe(ws)
        return
    except HTTPException:
        await ws.close(code=4404)
        return


@app.get("/api/identity")
def make_identity() -> dict:
    """Anonymous identity token for multiplayer (browser stores it)."""
    return {"token": secrets.token_hex(16)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8420)
