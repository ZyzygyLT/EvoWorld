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
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.exc import InterfaceError, OperationalError

from worldsim.data.loaders import load_governments, load_races
from worldsim.export.state import engine_to_dict
from worldsim.server import db
from worldsim.server import highlight_service
from worldsim.server.db import (
    Base,  # noqa: F401 — referenced by tests as appmod.Base
    ChatRow,
    WorldRow,
    WorldOperation,
    WorldEvent,
    WorldCheckpoint,
    WorldHighlight,
)
from worldsim.server.errors import (
    forget_not_recoverable,
    not_recoverable_reason,
    ws_close_code,
)
from worldsim.server.repository import EventRepository, ChallengeRepository, ChallengeParticipantRepository
from worldsim.server.challenge_service import (
    ChallengeError,
    ChallengeNotFound,
    ChallengeService,
    IdempotencyConflict,
    compute_operation_request_hash,
)
from worldsim.server.prediction_resolver import (
    KINDS,
    create_prediction,
    leaderboard,
    list_predictions,
)
from worldsim.server.recovery_coordinator import run_startup
from worldsim.server.runner import WorldNotFoundError, registry, safe_delete_world
from worldsim.server.transitions import (
    TransitionError,
    pause_world,
    resume_world,
    transition_coordinator,
)
from worldsim.sim.engine import SimEngine
from worldsim.sim.models import EventType, TRIBAL_GOVERNMENT
from worldsim.worldgen import WorldConfig
from worldsim.worldgen.pipeline import generate_world

DATA_DIR = Path(__file__).resolve().parents[3] / "data"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Server startup/teardown (DEC01 / DEC02 / DEC06).

    Startup order:

    1. Create or acquire the dependencies with clear ownership
       (:func:`run_startup` builds PersistenceService, RecoveryManager and
       the RecoveryCoordinator — the coordinator owns the service).
    2. Execute ``ensure_schema()`` EXPLICITLY, before any world query or
       any recovery (DEC02: no import-time schema side effects remain).
    3. Create and initialise the services.
    4. Build the RecoveryCoordinator.
    5. Run the full recovery (Etapa A + Etapa B).

    If startup fails, ``run_startup`` performs the rollback and re-raises
    the ORIGINAL error.  This coroutine then never yields, so the
    application never becomes available as if recovery had completed.

    Shutdown: the ``finally`` drains in-flight pause/resume transitions
    (Etapa 4 / DEC14 — refuse new ones, wait for the active ones).  The
    global drain/close (DEC17) still belongs to Etapa 6.
    """
    db.ensure_schema()  # DEC02: schema migration is explicit and precedes recovery.
    coordinator = await run_startup(session_factory=db.SessionLocal, registry=registry)
    app.state.coordinator = coordinator
    try:
        yield
    finally:
        # Etapa 4 / DEC14: drenar las transiciones de pausa/reanudación —
        # impide nuevas y espera a las que ya están en curso, sin abandonar
        # escrituras (§4.6).
        await transition_coordinator.shutdown()
        # Etapa 6 / DEC17: cierre global ordenado (runners + persistencia).
        # Se ejecuta incluso si el cuerpo de la aplicación terminó con una
        # excepción; nunca oculta errores de cierre (regla 8).
        coordinator = getattr(app.state, "coordinator", None)
        if coordinator is not None:
            try:
                await coordinator.shutdown()
            except BaseException as exc:
                # El lifespan debe terminar; se registra y el estado del
                # coordinator (CLOSED / CLOSE_INCOMPLETE) lo refleja.
                logger.error(
                    "lifespan shutdown: coordinator close failed: %r",
                    exc, exc_info=True,
                )


app = FastAPI(title="worldsim", version="0.4.0", lifespan=lifespan)


# -- DEC16: explicit HTTP error classification ------------------------------

@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request, exc: RequestValidationError):
    """Invalid params → 400 (replaces the default 422)."""
    return JSONResponse(status_code=400, content={"detail": exc.errors()})


@app.exception_handler(OperationalError)
@app.exception_handler(InterfaceError)
async def _infra_error_handler(request, exc: BaseException):
    """Temporary infrastructure failure → 503 (logged, never hidden)."""
    logger.error("infrastructure failure: %r", exc, exc_info=True)
    return JSONResponse(status_code=503, content={"detail": "temporary infrastructure failure"})


@app.exception_handler(IdempotencyConflict)
async def _idempotency_conflict_handler(request, exc: IdempotencyConflict):
    """Misma clave de idempotencia con contenido distinto → 409
    (CONTRACTS §17.9 / §17.13 para operaciones de desafío)."""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ChallengeNotFound)
async def _challenge_not_found_handler(request, exc: ChallengeNotFound):
    """Desafío no encontrado → 404 (CONTRACTS §17.9)."""
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ChallengeError)
async def _challenge_error_handler(request, exc: ChallengeError):
    """Transición de estado inválida / regla de dominio del servicio → 400.

    ``IdempotencyConflict`` (subclase) tiene handler propio → 409; la
    precedencia la da la jerarquía de clases vía ``type(exc).__mro__``.
    """
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def _coordinator():
    """RecoveryCoordinator instance from lifespan state, or None when the
    app was built without a lifespan (some tests)."""
    return getattr(app.state, "coordinator", None)


def _require_recoverable(world_id: int) -> None:
    """DEC16/§5: refuse service with 409 for worlds whose
    RecoveryCoordinator recorded a not_recoverable result, BEFORE any
    runner or row is touched.  No lazy runner, no substitute engine."""
    reason = not_recoverable_reason(_coordinator(), world_id)
    if reason is not None:
        raise HTTPException(
            409, f"world {world_id} is not recoverable: {reason}"
        )


def _forget_recoverable(world_id: int) -> None:
    """Drop a not_recoverable record after a confirmed delete, so a reused
    world_id does not inherit a stale 409 (DEC10/§5)."""
    forget_not_recoverable(_coordinator(), world_id)


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


class WorldHighlightRequest(BaseModel):
    reason: str = Field(min_length=1)
    duration_months: int | None = None


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


def _require_highlight_admin(authorization: str | None):
    user = _auth_user(authorization)
    if user is None:
        raise HTTPException(401, "log in as an administrator")
    if not bool(user.is_admin):
        raise HTTPException(403, "administrator privileges required")
    return user


@app.post("/api/admin/worlds/{world_id}/highlight", status_code=201)
def admin_highlight_world(
    world_id: int,
    req: WorldHighlightRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    admin = _require_highlight_admin(authorization)
    if not req.reason.strip():
        raise HTTPException(400, "reason must not be empty")
    if req.duration_months is not None and req.duration_months <= 0:
        raise HTTPException(400, "duration_months must be positive")
    try:
        row = highlight_service.grant_highlight(
            world_id=world_id,
            admin_user_id=admin.id,
            reason=req.reason,
            duration_months=req.duration_months,
        )
    except highlight_service.WorldNotEligible as exc:
        raise HTTPException(400, str(exc)) from None
    except highlight_service.ActiveHighlightExists as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"world_highlight_id": row.id}


@app.post("/api/admin/worlds/{world_id}/unhighlight")
def admin_unhighlight_world(
    world_id: int,
    authorization: str | None = Header(default=None),
) -> dict:
    admin = _require_highlight_admin(authorization)
    try:
        highlight_service.revoke_highlight(
            world_id=world_id,
            admin_user_id=admin.id,
        )
    except highlight_service.NoActiveHighlight:
        raise HTTPException(404, "no active highlight") from None
    return {"revoked": True}


@app.get("/api/admin/world_highlights/{highlight_id}")
def admin_get_world_highlight(
    highlight_id: int,
    authorization: str | None = Header(default=None),
) -> dict:
    _require_highlight_admin(authorization)
    try:
        return highlight_service.get_highlight_detail(highlight_id)
    except highlight_service.HighlightNotFound:
        raise HTTPException(404, "highlight not found") from None


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


def _highlighted_expression(now_iso: str):
    return exists(
        select(1).select_from(WorldHighlight).where(
            WorldHighlight.world_id == WorldRow.id,
            WorldHighlight.is_active == 1,
            or_(
                WorldHighlight.expires_at.is_(None),
                WorldHighlight.expires_at > now_iso,
            ),
        )
    )


def _highlight_cursor_fingerprint(
    mine: str | None,
    search: str | None,
    mode: str | None,
    limit: int,
    owner_user_id: int | None = None,
) -> str:
    canonical = json.dumps(
        {
            "mine": mine,
            "search": search,
            "mode": mode,
            "limit": limit,
            "owner_user_id": owner_user_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _issue_highlight_cursor(
    is_highlighted: bool, world_id: int, fingerprint: str
) -> str:
    payload = {
        "v": 1,
        "is_highlighted": bool(is_highlighted),
        "id": int(world_id),
        "fp": fingerprint,
    }
    body = _b64u(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    secret = os.environ.get("WORLDSIM_HIGHLIGHT_CURSOR_SECRET") or SNAPSHOT_SECRET
    signature = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"h1.{body}.{_b64u(signature)}"


def _parse_highlight_cursor(token: str, fingerprint: str) -> tuple[bool, int]:
    invalid = HTTPException(400, "invalid or incompatible cursor")
    try:
        if len(token) > 2048:
            raise invalid
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != "h1":
            raise invalid
        _, body, signature = parts
        secret = os.environ.get("WORLDSIM_HIGHLIGHT_CURSOR_SECRET") or SNAPSHOT_SECRET
        expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64u(signature), expected):
            raise invalid
        payload = json.loads(_unb64u(body))
        if (
            not isinstance(payload, dict)
            or type(payload.get("v")) is not int
            or payload.get("v") != 1
        ):
            raise invalid
        highlighted_value = payload.get("is_highlighted")
        world_id = payload.get("id")
        if type(highlighted_value) is not bool:
            raise invalid
        if type(world_id) is not int or world_id <= 0 or world_id > 2**63 - 1:
            raise invalid
        if payload.get("fp") != fingerprint:
            raise invalid
        return highlighted_value, world_id
    except HTTPException:
        raise
    except Exception:
        raise invalid from None


@app.get("/api/worlds")
def list_worlds(
    limit: int | None = None,
    cursor: str | None = None,
    search: str | None = None,
    mode: str | None = None,
    mine: str | None = None,
    authorization: str | None = Header(default=None),
) -> dict:
    """F7-B gallery with F7-E.4 public highlight ordering and opaque cursor."""
    user = _auth_user(authorization)

    owner_mode = False
    if mine is not None:
        if mine != "1":
            raise HTTPException(400, "mine must be '1' or absent")
        if user is None:
            raise HTTPException(401, "log in to list your worlds")
        owner_mode = True

    page_limit = 20
    if limit is not None:
        if limit < 1 or limit > 100:
            raise HTTPException(400, "limit must be between 1 and 100")
        page_limit = limit

    search_pattern: str | None = None
    normalized_search: str | None = None
    if search is not None:
        stripped = search.strip()
        if len(stripped) > 64:
            raise HTTPException(400, "search must be at most 64 characters")
        if stripped:
            normalized_search = stripped
            escaped = (
                stripped.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            search_pattern = f"%{escaped}%"

    if mode is not None and mode not in ("single", "league"):
        raise HTTPException(400, "mode must be 'single' or 'league'")

    cursor_id: int | None = None
    cursor_highlighted: bool | None = None
    fingerprint = _highlight_cursor_fingerprint(
        mine, normalized_search.lower() if normalized_search else None,
        mode, page_limit,
        user.id if owner_mode else None,
    )
    if cursor is not None:
        cursor_highlighted, cursor_id = _parse_highlight_cursor(cursor, fingerprint)
        if owner_mode and cursor_highlighted:
            raise HTTPException(400, "incompatible cursor for owner gallery")

    now_iso = highlight_service.utc_iso(highlight_service.utc_now())
    highlighted_expr = _highlighted_expression(now_iso)

    columns = (
        WorldRow.id,
        WorldRow.name,
        WorldRow.mode,
        WorldRow.seed,
        WorldRow.months_simulated,
        WorldRow.land_pct,
        WorldRow.created_at,
        WorldRow.season_years,
        WorldRow.season_over,
        WorldRow.visibility,
        WorldRow.owner_user_id,
    )
    with db.SessionLocal() as db_:
        if owner_mode:
            q = select(*columns).order_by(WorldRow.id.desc())
            q = q.where(WorldRow.owner_user_id == user.id)
        else:
            q = select(*columns, highlighted_expr.label("is_highlighted"))
            q = q.order_by(highlighted_expr.desc(), WorldRow.id.desc())
            q = q.where(WorldRow.visibility == "public")

        if search_pattern is not None:
            q = q.where(
                func.lower(WorldRow.name).like(
                    func.lower(search_pattern), escape="\\"
                )
            )
        if mode is not None:
            q = q.where(WorldRow.mode == mode)

        q = q.where(WorldRow.deleted_at.is_(None))

        if cursor_id is not None:
            if owner_mode:
                q = q.where(WorldRow.id < cursor_id)
            elif cursor_highlighted:
                q = q.where(
                    or_(
                        and_(highlighted_expr.is_(True), WorldRow.id < cursor_id),
                        highlighted_expr.is_(False),
                    )
                )
            else:
                q = q.where(
                    highlighted_expr.is_(False),
                    WorldRow.id < cursor_id,
                )

        rows = db_.execute(q.limit(page_limit + 1)).all()

    has_next = len(rows) > page_limit
    worlds_rows = rows[:page_limit]
    if not has_next:
        next_cursor = None
    elif owner_mode:
        next_cursor = _issue_highlight_cursor(False, worlds_rows[-1].id, fingerprint)
    else:
        last = worlds_rows[-1]
        next_cursor = _issue_highlight_cursor(
            bool(last.is_highlighted), last.id, fingerprint
        )

    if owner_mode:
        worlds = [_public_ficha(row, user) for row in worlds_rows]
    else:
        worlds = [
            _public_ficha(row, user, is_highlighted=bool(row.is_highlighted))
            for row in worlds_rows
        ]

    return {"worlds": worlds, "next_cursor": next_cursor}


@app.post("/api/worlds")
def create_world(
    req: CreateWorldRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    """Generate a world, optionally pre-simulate `months`, persist, return id+state.

    DEC20/P-1: creation REQUIRES an authenticated session (401 otherwise).
    The owner is the session's user_id — a client-supplied owner identifier
    is never accepted — and the world is born explicitly 'private'.
    """
    owner = _auth_user(authorization)
    if owner is None:
        raise HTTPException(401, "log in to create a world")
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
            owner_user_id=owner.id,
            visibility="private",  # DEC20/P-1: explicit, never implicit
            sea_level=req.sea_level,
        )
        db_.add(row)
        db_.commit()
        db_.refresh(row)
        # seed the runner so POST's engine == canonical runner engine
        from worldsim.server.operation_service import OperationService
        operation_service = OperationService(db.SessionLocal, engine_version="1.0")
        runner = registry.get(
            row.id, req.seed, req.width, req.height,
            req.enabled_races, req.sea_level,
            operation_service=operation_service,
        )
        runner.eng = eng
        return {"id": row.id, "state": state}


def _load_row(world_id: int) -> WorldRow:
    with db.SessionLocal() as db_:  # type: ignore[call-arg]
        # F7-E.2 (B3-3): the central ordinary load excludes logically
        # deleted worlds — a deleted world is indistinguishable from a
        # missing one (same 404, even for the authenticated owner).
        row = db_.scalars(
            select(WorldRow).where(
                WorldRow.id == world_id,
                WorldRow.deleted_at.is_(None),
            )
        ).first()
        if row is None:
            raise HTTPException(404, f"world {world_id} not found")
        return row


# -- DEC20 (F7-A): visibility gate + bounded public representation -------------

#: DEC20/§14.4 CLOSED allowlist for the public representation of a world.
#: The public dict is built key-by-key from this contract; it is never
#: derived by serializing a model and dropping fields.  Expanding it
#: requires a new DEC (regla transversal 3).
PUBLIC_WORLD_FIELDS = (
    "id", "name", "mode", "seed", "months_simulated", "land_pct",
    "created_at", "season_years", "season_over", "visibility", "mine",
    "is_highlighted",
)
#: Base-map render block allowed on the public DETAIL (§14.4).  Note
#: ``battle_heat``/``state_json`` are NOT part of it.
PUBLIC_WORLD_MAP_FIELDS = ("width", "height", "biome", "elevation", "rivers")


def _visibility(row: WorldRow) -> str:
    """Effective visibility: exactly 'private' | 'public'.

    DEC20/§14.2.3 fail-closed: NULL or any value outside the domain is
    read as 'private' and logged (regla 8: nunca ocultar).
    """
    v = row.visibility
    if v in ("private", "public"):
        return v
    logger.error(
        "world %s: invalid visibility %r treated as private (fail-closed)",
        row.id, v,
    )
    return "private"


def _is_owner(row: WorldRow, user) -> bool:
    """Ownership proof = authenticated session only (DEC20/§14.3).

    ``user_token`` and any client-supplied identifier are never proof.
    """
    return (
        user is not None
        and row.owner_user_id is not None
        and user.id == row.owner_user_id
    )


def _require_visible(row: WorldRow, user) -> None:
    """DEC20/§14.3: a third party (anonymous OR authenticated non-owner)
    never sees a private world — 404, indistinguishable from 'not found'."""
    if _visibility(row) == "private" and not _is_owner(row, user):
        raise HTTPException(404, f"world {row.id} not found")


def _load_visible_row(world_id: int, authorization: str | None) -> WorldRow:
    """Existence + visibility FIRST, in the order fixed by §14.3:
    (1) 404 gate → (2) DEC16 recoverable guard → (3) business logic.
    Only reads the row; never creates a runner (§12 §5 preserved)."""
    row = _load_row(world_id)  # 404; also the infra probe tests patch this
    _require_visible(row, _auth_user(authorization))
    return row


def _world_is_highlighted(world_id: int) -> bool:
    now_iso = highlight_service.utc_iso(highlight_service.utc_now())
    with db.SessionLocal() as db_:
        return bool(
            db_.scalar(
                select(_highlighted_expression(now_iso)).where(
                    WorldRow.id == world_id,
                    WorldRow.visibility == "public",
                    WorldRow.deleted_at.is_(None),
                )
            )
        )


def _public_ficha(
    row: WorldRow, user, *, is_highlighted: bool | None = None
) -> dict:
    """Bounded public representation (DEC20/§14.4 + DEC-F7E-01)."""
    out = {
        "id": row.id,
        "name": row.name or f"World {row.seed}",
        "mode": row.mode or "single",
        "seed": row.seed,
        "months_simulated": row.months_simulated,
        "land_pct": row.land_pct,
        "created_at": row.created_at,
        "season_years": row.season_years or 300,
        "season_over": bool(row.season_over),
        "visibility": _visibility(row),
        "mine": _is_owner(row, user),
    }
    if is_highlighted is not None:
        out["is_highlighted"] = bool(is_highlighted)
    return out


def _public_detail(row: WorldRow, user) -> dict:
    """Public DETAIL: ficha + explicit base-map block (§14.4).

    Never includes ``state_json``, ``kingdoms``, ``chronicle``,
    ``battle_heat`` or any field outside the two allowlists above."""
    out = _public_ficha(row, user, is_highlighted=_world_is_highlighted(row.id))
    world_block: dict = {}
    try:
        state = json.loads(row.state_json or "{}")
        src = state.get("world") or {}
    except (json.JSONDecodeError, TypeError):
        src = {}
    for key in PUBLIC_WORLD_MAP_FIELDS:
        if key == "width":
            world_block[key] = src.get("width", row.width)
        elif key == "height":
            world_block[key] = src.get("height", row.height)
        else:
            world_block[key] = src.get(key)
    out["world"] = world_block
    return out


@app.get("/api/worlds/{world_id}")
def get_world(
    world_id: int,
    authorization: str | None = Header(default=None),
) -> dict:
    """DEC20/§14.4: the OWNER gets the full saved state; anyone else only
    gets the bounded public representation (never ``state_json``)."""
    row = _load_row(world_id)
    user = _auth_user(authorization)
    _require_visible(row, user)
    if _is_owner(row, user):
        return json.loads(row.state_json)
    return _public_detail(row, user)


class VisibilityRequest(BaseModel):
    # validated strictly inside the handler (after the authorization
    # matrix) so an invalid value can never probe world existence
    visibility: str


@app.put("/api/worlds/{world_id}/visibility")
def put_world_visibility(
    world_id: int,
    req: VisibilityRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    """DEC20/§14.3 publish/withdraw: owner-only, atomic, fail-closed.

    Order: 401 (no session) → 404 (not found) → 403/404 (not the owner,
    private worlds stay unrevealed) → 400 (value outside the domain) →
    200 (persisted atomically).  Reads re-check the persisted value on
    every request — no visibility cache (§14.6).
    """
    user = _auth_user(authorization)
    if user is None:
        raise HTTPException(401, "log in to change world visibility")
    row = _load_row(world_id)
    if row.owner_user_id is None:
        # ownerless (legacy) world: nobody may change it (§14.3)
        raise HTTPException(403, "this world has no owner")
    if not _is_owner(row, user):
        # non-owner: private worlds stay unrevealed (404), public → 403
        if _visibility(row) == "private":
            raise HTTPException(404, f"world {world_id} not found")
        raise HTTPException(403, "only the owner may change visibility")
    if req.visibility not in ("private", "public"):
        raise HTTPException(400, "visibility must be 'private' or 'public'")
    with db.SessionLocal() as db_:  # type: ignore[call-arg]
        # F7-E.2 (B3-3): the second read excludes a world logically
        # deleted between the initial load and this operation (TOCTOU).
        live = db_.scalars(
            select(WorldRow).where(
                WorldRow.id == world_id,
                WorldRow.deleted_at.is_(None),
            )
        ).first()
        if live is None:
            raise HTTPException(404, f"world {world_id} not found")
        live.visibility = req.visibility  # single-column atomic write
        db_.commit()
    return {"id": world_id, "visibility": req.visibility}


@app.delete("/api/worlds/{world_id}")
async def delete_world(
    world_id: int,
    authorization: str | None = Header(default=None),
) -> dict:
    """DEC20/§14.3 + P-4 matrix: anonymous on an OWNED world → 401;
    authenticated non-owner → 403 (public) / 404 (private, unrevealed);
    owner → the DEC10 safe-delete flow.  Ownerless legacy worlds keep
    their existing open-delete behavior (§14.3)."""
    user = _auth_user(authorization)
    with db.SessionLocal() as db_:  # type: ignore[call-arg]
        row = db_.get(WorldRow, world_id)
        if row is None:
            raise HTTPException(404, f"world {world_id} not found")
        if row.owner_user_id is not None:
            if user is None:
                raise HTTPException(401, "log in to delete this world")
            if user.id != row.owner_user_id:
                if _visibility(row) == "private":
                    raise HTTPException(404, f"world {world_id} not found")
                raise HTTPException(403, "only the owner may delete this world")
    # DEC10 (Etapa 2): close the runner, verify the close, unregister it,
    # and ONLY THEN persist the logical deletion (see safe_delete_world).
    # A failed close / failed persistence propagates as-is (existing error
    # contract — no new HTTP codes); a missing row maps to the existing 404.
    try:
        await safe_delete_world(
            world_id, session_factory=db.SessionLocal, registry=registry,
        )
    except WorldNotFoundError:
        raise HTTPException(404, f"world {world_id} not found") from None
    _forget_recoverable(world_id)  # avoid stale 409 if world_id is reused
    return {"deleted": world_id}


@app.get("/api/worlds/{world_id}/stats/{kingdom_id}")
def get_kingdom_stats(
    world_id: int,
    kingdom_id: int,
    authorization: str | None = Header(default=None),
) -> dict:
    """Rich stats for one kingdom (deterministic replay to the saved month)."""
    row = _load_visible_row(world_id, authorization)  # DEC20: 404 gate first
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
def get_predictions(
    world_id: int,
    authorization: str | None = Header(default=None),
) -> list[dict]:
    _load_visible_row(world_id, authorization)  # DEC20: 404 gate first
    return list_predictions(world_id)


@app.post("/api/worlds/{world_id}/predictions")
def post_prediction(
    world_id: int,
    req: PredictionRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    # DEC20/§14.3 order: existence + visibility (404) BEFORE the DEC16
    # guard (409), so a third party can never probe a private world.
    _load_visible_row(world_id, authorization)
    _require_recoverable(world_id)  # DEC16/§5: 409 for the authorized
    # seal with the CANONICAL current month (prophecies can't cover the past)
    month = _canonical_month(world_id)
    row = create_prediction(
        world_id, req.user_token, req.user_name, req.kind, req.params, month
    )
    if row is None:
        raise HTTPException(400, f"unknown prediction kind {req.kind!r}")
    return row


@app.get("/api/worlds/{world_id}/leaderboard")
def get_leaderboard(
    world_id: int,
    authorization: str | None = Header(default=None),
) -> list[dict]:
    _load_visible_row(world_id, authorization)  # DEC20: 404 gate first
    return leaderboard(world_id)


@app.get("/api/worlds/{world_id}/chat")
def get_chat(
    world_id: int,
    authorization: str | None = Header(default=None),
) -> list[dict]:
    _load_visible_row(world_id, authorization)  # DEC20: 404 gate first
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
def post_chat(
    world_id: int,
    req: ChatRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    # DEC20/§14.3 order: visibility (404) before DEC16 (409)
    _load_visible_row(world_id, authorization)
    _require_recoverable(world_id)  # DEC16/§5
    runner = registry.get_existing(world_id)
    month = _canonical_month(world_id)
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
def get_claims(
    world_id: int,
    authorization: str | None = Header(default=None),
) -> list[dict]:
    from worldsim.server.league import get_claims as _claims

    _load_visible_row(world_id, authorization)  # DEC20: 404 gate first
    return _claims(world_id)


@app.post("/api/worlds/{world_id}/claims")
def post_claim(
    world_id: int, req: ClaimRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    from worldsim.server.league import claim_kingdom

    # DEC20/§14.3 order: visibility (404) before DEC16 (409)
    _load_visible_row(world_id, authorization)
    _require_recoverable(world_id)  # DEC16/§5
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
def get_standings(
    world_id: int,
    authorization: str | None = Header(default=None),
) -> list[dict]:
    from worldsim.server.league import standings

    _load_visible_row(world_id, authorization)  # DEC20: 404 gate first
    return standings(world_id)


# -- F7-C: Historias compartidas de evolución -------------------------------------

#: Public allowlist for event representation (DEC20/§14.4 equivalent for events).
#: Never expose kingdom_ids or event_params to third parties.
EVENT_PUBLIC_FIELDS = ("event_id", "month", "event_type", "event_text", "msg_key")
EVENT_OWNER_FIELDS = (
    "event_id", "month", "event_type", "event_text",
    "kingdom_ids", "event_params", "msg_key"
)


class EventsQuery(BaseModel):
    limit: int | None = None
    cursor: str | None = None
    snapshot_token: str | None = None
    month: int | None = None
    event_type: str | None = None
    kingdom_id: int | None = None


def _parse_cursor(cursor: str | None) -> tuple[int, int] | None:
    """Parse an opaque ``operation_sequence:position`` cursor (§16.3).

    Returns ``None`` when no cursor was sent.  Raises :class:`ValueError`
    for any malformed or negative value — the endpoint maps it to 400.
    """
    if cursor is None:
        return None
    parts = cursor.split(":")
    if len(parts) != 2:
        raise ValueError("cursor must be 'operation_sequence:position'")
    try:
        seq = int(parts[0])
        pos = int(parts[1])
    except (ValueError, TypeError) as exc:
        raise ValueError("cursor must be 'operation_sequence:position'") from exc
    if seq < 0 or pos < 0:
        raise ValueError("cursor components must be non-negative")
    return (seq, pos)


# -- Snapshot navigation tokens (§16.3 / §16.13) -----------------------------

#: Payload format version of the snapshot token (bumped on format changes).
SNAPSHOT_TOKEN_VERSION = 1
#: Token lifetime (§16.4: configurable, 5 minutes by default).
SNAPSHOT_TOKEN_TTL_SECONDS = 300
#: Signing secret: per-process random unless shared through the environment
#: (multi-process deployments MUST set WORLDSIM_SNAPSHOT_SECRET to the same
#: value, otherwise tokens issued by another worker fail validation → 400,
#: which is the safe direction: a new navigation is started).
SNAPSHOT_SECRET = os.environ.get("WORLDSIM_SNAPSHOT_SECRET") or secrets.token_hex(32)

#: Valid ``event_type`` values are the persisted EventType names (events are
#: stored as ``event.type.name``, §16.3 "tipo de evento válido").
_EVENT_TYPE_NAMES = frozenset(et.name for et in EventType)


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _filters_fingerprint(
    month: int | None,
    event_type: str | None,
    kingdom_id: int | None,
    limit: int,
) -> str:
    """Fingerprint binding a navigation to its filters (§16.3: month,
    event_type, kingdom_id and limit cannot change between pages)."""
    canonical = json.dumps(
        {
            "month": month,
            "event_type": event_type,
            "kingdom_id": kingdom_id,
            "limit": limit,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _issue_snapshot_token(
    world_id: int, hw: tuple[int, int], fingerprint: str
) -> str:
    """Sign an opaque, versioned, expiring navigation token bound to the
    world, the high-water mark and the filter fingerprint (§16.13 step 4)."""
    now = int(time.time())
    payload = {
        "v": SNAPSHOT_TOKEN_VERSION,
        "wid": world_id,
        "hw": [int(hw[0]), int(hw[1])],
        "fp": fingerprint,
        "iat": now,
        "exp": now + SNAPSHOT_TOKEN_TTL_SECONDS,
    }
    body = _b64u(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    sig = hmac.new(
        SNAPSHOT_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).digest()
    return f"s{SNAPSHOT_TOKEN_VERSION}.{body}.{_b64u(sig)}"


def _parse_snapshot_token(
    token: str, world_id: int, fingerprint: str
) -> tuple[int, int]:
    """Validate a navigation token and return its high-water mark.

    ANY failure — format, signature, version, expiry, world binding or
    filter fingerprint — maps to HTTP 400: the client must start a new
    navigation without a token (§16.3, §16.13 step 1).
    """
    invalid = HTTPException(400, "invalid or expired snapshot_token")
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise invalid
        version_s, body, sig = parts
        if version_s != f"s{SNAPSHOT_TOKEN_VERSION}":
            raise invalid
        expected = hmac.new(
            SNAPSHOT_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_unb64u(sig), expected):
            raise invalid
        payload = json.loads(_unb64u(body))
        if not isinstance(payload, dict):
            raise invalid
        if payload.get("v") != SNAPSHOT_TOKEN_VERSION:
            raise invalid
        if payload.get("wid") != world_id:
            raise invalid
        if payload.get("fp") != fingerprint:
            # Filters (month/event_type/kingdom_id/limit) changed between
            # pages → not the same navigation (§16.3).
            raise invalid
        exp = payload.get("exp")
        if not isinstance(exp, int) or int(time.time()) > exp:
            raise invalid
        hw = payload.get("hw")
        if (
            not isinstance(hw, list)
            or len(hw) != 2
            or not all(isinstance(v, int) and v >= 0 for v in hw)
        ):
            raise invalid
        return (int(hw[0]), int(hw[1]))
    except HTTPException:
        raise
    except Exception:
        # Malformed base64/JSON/etc: same contractual outcome as a bad
        # signature — 400, never a 500 and never a silently accepted token.
        raise invalid from None


def _serialize_event(event: WorldEvent, is_owner: bool) -> dict:
    """Serialize an event through the EXPLICIT allowlist (§16.4): never a
    model dump and never field subtraction.

    Public projection (anonymous or third party on a public world): only
    ``event_id``, ``month``, ``event_type``, ``event_text``, ``msg_key``.
    Owner projection additionally includes ``kingdom_ids`` and
    ``event_params`` (§16.6).
    """
    source: dict = {
        "event_id": event.id,
        "month": event.month,
        "event_type": event.event_type,
        "event_text": event.event_text,
        "msg_key": event.msg_key,
    }
    if is_owner:
        source["kingdom_ids"] = json.loads(event.kingdom_ids or "[]")
        source["event_params"] = json.loads(event.event_params or "{}")
    fields = EVENT_OWNER_FIELDS if is_owner else EVENT_PUBLIC_FIELDS
    return {name: source[name] for name in fields}


def _coverage_metadata(db_session, world_id: int) -> dict:
    """Live coverage computation over durable data (§16.12).

    * ``unknown`` — no operations at all: nothing to characterize.
    * ``confirmed`` — the operation sequence is a contiguous run of
      ``confirmed`` operations from the first one, and each carries its T2
      ``checkpoint_id`` (evidence of the atomic persistence+checkpoint
      commit).  Checkpoint months come from valid checkpoints.
    * ``potentially_incomplete`` — persisted history exists, but a hole or
      a non-confirmed operation (``pending``/``executing``/``blocked``/
      ``failed``/``canceled``) breaks the contiguous confirmed prefix.

    ``discontinuity_sequence`` / ``discontinuity_reason`` /
    ``last_confirmed_sequence`` are included whenever ``status`` is not
    ``confirmed``.  ``discontinuity_reason`` is ``null`` ONLY in the two
    data-inconsistency cases that have no state reason (a missing sequence
    or a ``confirmed`` operation without checkpoint) — a reason is never
    invented, and a later confirmed operation never repairs the prefix
    (§16.10.10).
    """
    ops = list(
        db_session.scalars(
            select(WorldOperation)
            .where(WorldOperation.world_id == world_id)
            .order_by(WorldOperation.operation_sequence)
        ).all()
    )
    cp_row = db_session.execute(
        select(func.min(WorldCheckpoint.month), func.max(WorldCheckpoint.month)).where(
            WorldCheckpoint.world_id == world_id,
            WorldCheckpoint.is_valid == 1,
        )
    ).first()
    first_cp_month = int(cp_row[0]) if cp_row and cp_row[0] is not None else None
    last_cp_month = int(cp_row[1]) if cp_row and cp_row[1] is not None else None

    coverage: dict = {
        "status": "unknown",
        "first_checkpoint_month": first_cp_month,
        "last_checkpoint_month": last_cp_month,
    }

    last_confirmed: int | None = None
    disc_seq: int | None = None
    disc_reason: str | None = None

    if ops:
        expected = int(ops[0].operation_sequence)
        for op in ops:
            seq = int(op.operation_sequence)
            if seq != expected:
                # Sequence hole (impossible by construction: T1 allocates
                # atomically and never reuses) — detected, never hidden.
                disc_seq = expected
                disc_reason = None
                break
            if op.status != "confirmed":
                disc_seq = seq
                disc_reason = op.status
                break
            if op.checkpoint_id is None:
                # 'confirmed' without its T2 checkpoint: the atomic commit
                # cannot be proven for this operation.
                disc_seq = seq
                disc_reason = None
                break
            last_confirmed = seq
            expected = seq + 1
        coverage["status"] = "confirmed" if disc_seq is None else "potentially_incomplete"

    if coverage["status"] != "confirmed":
        coverage["discontinuity_sequence"] = disc_seq
        coverage["discontinuity_reason"] = disc_reason
        coverage["last_confirmed_sequence"] = last_confirmed
    return coverage


@app.get("/api/worlds/{world_id}/events")
def get_world_events(
    world_id: int,
    limit: int | None = None,
    cursor: str | None = None,
    snapshot_token: str | None = None,
    month: int | None = None,
    event_type: str | None = None,
    kingdom_id: int | None = None,
    authorization: str | None = Header(default=None),
) -> dict:
    """F7-C: historical events query with snapshot pagination (CONTRACTS §16).

    Check order fixed by §16.6: 404 (existence + visibility) → 409 (DEC16
    recoverable guard) → 400 (parameter validation).
    """
    # 1. 404 gate: existence + visibility.  A private world of a third
    #    party (authenticated or anonymous) is indistinguishable from a
    #    world that does not exist (§16.6).
    row = _load_row(world_id)
    user = _auth_user(authorization)  # exactly one auth resolution
    _require_visible(row, user)
    # 2. DEC16/§5 guard: not recoverable → 409, before any query.
    _require_recoverable(world_id)

    # 3. Parameter validation (§16.3): every invalid value → 400.
    page_limit = 20
    if limit is not None:
        if limit < 1 or limit > 100:
            raise HTTPException(400, "limit must be between 1 and 100")
        page_limit = limit
    try:
        cursor_pos = _parse_cursor(cursor)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if month is not None and month < 0:
        raise HTTPException(400, "month must be >= 0")
    if event_type is not None and event_type not in _EVENT_TYPE_NAMES:
        raise HTTPException(400, f"unknown event_type: {event_type!r}")
    if kingdom_id is not None and kingdom_id < 0:
        raise HTTPException(400, "kingdom_id must be >= 0")

    fingerprint = _filters_fingerprint(month, event_type, kingdom_id, page_limit)

    # 4. Navigation token: later pages MUST reuse the same token; any
    #    validation failure (signature/expiry/world/filters) → 400 (§16.13).
    hw_pos: tuple[int, int] | None = None
    if snapshot_token is not None:
        hw_pos = _parse_snapshot_token(snapshot_token, world_id, fingerprint)

    is_owner = _is_owner(row, user)
    with db.SessionLocal() as db_:  # type: ignore[call-arg]
        event_repo = EventRepository(db_)

        # First query: fix the immutable high-water mark at the LAST
        # confirmed event visible for these filters (§16.13 step 1).
        if hw_pos is None:
            hw_pos = event_repo.get_last_confirmed_event(
                world_id,
                month=month,
                event_type=event_type,
                kingdom_id=kingdom_id,
            )

        coverage = _coverage_metadata(db_, world_id)

        if hw_pos is None:
            # Navigation cannot start: no confirmed event visible for these
            # filters → no events, no token, no high-water mark (§16.4).
            return {
                "events": [],
                "next_cursor": None,
                "snapshot_token": None,
                "snapshot_high_watermark": None,
                "coverage": coverage,
            }

        # limit+1 page inside the fixed universe (§16.4 / §16.13 step 2).
        events, next_cursor = event_repo.get_events_page(
            world_id=world_id,
            limit=page_limit,
            cursor=cursor_pos,
            snapshot_high_watermark=hw_pos,
            month=month,
            event_type=event_type,
            kingdom_id=kingdom_id,
        )

        events_data = [_serialize_event(e, is_owner=is_owner) for e in events]

        # The token is present in EVERY response of a navigation: the same
        # string on later pages, freshly signed on the first one (§16.4).
        token_out = snapshot_token or _issue_snapshot_token(
            world_id, hw_pos, fingerprint
        )

        return {
            "events": events_data,
            "next_cursor": (
                f"{next_cursor[0]}:{next_cursor[1]}" if next_cursor else None
            ),
            "snapshot_token": token_out,
            "snapshot_high_watermark": f"{hw_pos[0]}:{hw_pos[1]}",
            "coverage": coverage,
        }


def _canonical_month(world_id: int) -> int:
    """Month of the shared clock (or the saved snapshot if never streamed)."""
    runner = registry.get_existing(world_id)
    if runner:
        return runner.eng.month
    with db.SessionLocal() as db_:  # type: ignore[call-arg]
        # F7-E.2 (B3-3): a logically deleted world never supplies the
        # fallback month (falls through to 0 like a missing world).
        row = db_.scalars(
            select(WorldRow).where(
                WorldRow.id == world_id,
                WorldRow.deleted_at.is_(None),
            )
        ).first()
        return row.months_simulated if row else 0


# -- F7-D: Desafíos con semillas comunes (comparación estadística) -----------

class CreateChallengeRequest(BaseModel):
    name: str
    config_json: str  # JSON con configuración inmutable (seed, dimensiones, etc.)
    rules_version: str = "1.0"
    duration_ticks: int


class EnrollChallengeRequest(BaseModel):
    client_idempotency_key: str


class DisputeChallengeRequest(BaseModel):
    client_idempotency_key: str


class ResolveChallengeRequest(BaseModel):
    client_idempotency_key: str
    resolution: str = "confirm_all"


class ChallengeResponse(BaseModel):
    id: int
    organizer_user_id: int
    name: str
    config_json: str
    rules_version: str
    duration_ticks: int
    state: str
    created_at: str
    closed_at: str | None
    completed_at: str | None


class ChallengeParticipantResponse(BaseModel):
    id: int
    challenge_id: int
    user_id: int
    world_id: int | None
    state: str
    joined_at: str
    result_json: str | None
    result_verified: bool


def _challenge_params(
    challenge_id: int,
    organizer_user_id: int,
    name: str,
    config_json: str,
    rules_version: str,
    duration_ticks: int,
) -> ChallengeParams:
    """Construye ChallengeParams canónico para request_hash."""
    from worldsim.server.challenge_service import ChallengeParams
    return ChallengeParams(
        challenge_id=challenge_id,
        organizer_user_id=organizer_user_id,
        name=name,
        config_json=config_json,
        rules_version=rules_version,
        duration_ticks=duration_ticks,
    )

_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")

def _require_idempotency_key(key: str | None) -> str:
    if not key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header required")
    if not _IDEMPOTENCY_KEY_RE.match(key):
        raise HTTPException(status_code=400, detail="Invalid Idempotency-Key")
    return key


@app.post("/api/challenges")
def api_create_challenge(
    req: CreateChallengeRequest,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict:
    """Crear un desafío en estado ``draft`` (F7-D, DEC-F7D-10)."""
    user = _auth_user(authorization)
    if user is None:
        raise HTTPException(401, "log in to create a challenge")
    if req.duration_ticks <= 0:
        raise HTTPException(400, "duration_ticks must be > 0")
    try:
        import json as _json
        _json.loads(req.config_json)
    except Exception:
        raise HTTPException(400, "config_json must be valid JSON")

    key = _require_idempotency_key(idempotency_key)
    service = ChallengeService()
    params = _challenge_params(0, user.id, req.name, req.config_json, req.rules_version, req.duration_ticks)
    rh = ChallengeService._compute_request_hash(params)
    result = service.create_challenge(
        organizer_user_id=user.id,
        name=req.name,
        config_json=req.config_json,
        rules_version=req.rules_version,
        duration_ticks=req.duration_ticks,
        client_idempotency_key=key,
        request_hash=rh,
    )
    c = result.challenge
    # F7-D-I1-FIX10-A-IDEM: en un reintento se reconstruye el RESULTADO ORIGINAL
    # (estado de la operación original), no el estado actual del desafío.
    if result.is_retry and result.original_outcome:
        state = result.original_outcome
        closed_at = None
        completed_at = None
    else:
        state = c.state
        closed_at = c.closed_at
        completed_at = c.completed_at
    return {
        "id": c.id,
        "organizer_user_id": c.organizer_user_id,
        "name": c.name,
        "config_json": c.config_json,
        "rules_version": c.rules_version,
        "duration_ticks": c.duration_ticks,
        "state": state,
        "created_at": c.created_at,
        "closed_at": closed_at,
        "completed_at": completed_at,
    }


@app.get("/api/challenges")
def api_list_challenges() -> dict:
    """Listar desafíos publicados (galería pública, DEC-F7D-09)."""
    with db.SessionLocal() as db_:
        repo = ChallengeRepository(db_)
        challenges = repo.get_challenges_by_state("published")
    worlds = [
        {
            "id": c.id,
            "name": c.name,
            "rules_version": c.rules_version,
            "duration_ticks": c.duration_ticks,
            "state": c.state,
            "created_at": c.created_at,
        }
        for c in challenges
    ]
    return {"challenges": worlds}


@app.get("/api/challenges/{challenge_id}")
def api_get_challenge(
    challenge_id: int,
    authorization: str | None = Header(default=None),
) -> dict:
    """Consultar un desafío (público si está publicado)."""
    user = _auth_user(authorization)
    with db.SessionLocal() as db_:
        repo = ChallengeRepository(db_)
        c = repo.get_challenge(challenge_id)
    if c is None:
        raise HTTPException(404, f"challenge {challenge_id} not found")
    if c.state != "published":
        # Solo el organizador puede ver desafíos no publicados
        if user is None or user.id != c.organizer_user_id:
            raise HTTPException(404, f"challenge {challenge_id} not found")
    return {
        "id": c.id,
        "name": c.name,
        "config_json": c.config_json,
        "rules_version": c.rules_version,
        "duration_ticks": c.duration_ticks,
        "state": c.state,
        "created_at": c.created_at,
        "closed_at": getattr(c, "closed_at", None),
        "completed_at": getattr(c, "completed_at", None),
    }


@app.put("/api/challenges/{challenge_id}/publish")
def api_publish_challenge(
    challenge_id: int,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict:
    """Publicar un desafío (congelar configuración, solo organizador)."""
    user = _auth_user(authorization)
    if user is None:
        raise HTTPException(401, "log in to publish a challenge")
    key = _require_idempotency_key(idempotency_key)
    with db.SessionLocal() as db_:
        repo = ChallengeRepository(db_)
        c = repo.get_challenge(challenge_id)
    if c is None:
        raise HTTPException(404, f"challenge {challenge_id} not found")
    if c.organizer_user_id != user.id:
        raise HTTPException(403, "not the organizer")
    service = ChallengeService()
    rh = compute_operation_request_hash("publish", challenge_id)
    result = service.publish_challenge(
        challenge_id=challenge_id,
        organizer_user_id=user.id,
        client_idempotency_key=key,
        request_hash=rh,
    )
    c = result.challenge
    # Reconstruct original response on retry
    if result.is_retry and result.original_outcome:
        # original_outcome matches state for publish
        c_state = result.original_outcome
    else:
        c_state = c.state
    return {"id": c.id, "state": c_state}


@app.put("/api/challenges/{challenge_id}/open-enrollment")
def api_open_enrollment(
    challenge_id: int,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict:
    """Abrir inscripciones (published → enrollment_open, solo organizador)."""
    user = _auth_user(authorization)
    if user is None:
        raise HTTPException(401, "log in to open enrollments")
    key = _require_idempotency_key(idempotency_key)
    with db.SessionLocal() as db_:
        repo = ChallengeRepository(db_)
        c = repo.get_challenge(challenge_id)
    if c is None:
        raise HTTPException(404, f"challenge {challenge_id} not found")
    if c.organizer_user_id != user.id:
        raise HTTPException(403, "not the organizer")
    service = ChallengeService()
    rh = compute_operation_request_hash("open_enrollment", challenge_id)
    result = service.open_enrollment(
        challenge_id=challenge_id,
        organizer_user_id=user.id,
        client_idempotency_key=key,
        request_hash=rh,
    )
    c = result.challenge
    state = result.original_outcome if (result.is_retry and result.original_outcome) else c.state
    return {"id": c.id, "state": state}


@app.post("/api/challenges/{challenge_id}/enroll")
def api_enroll_challenge(
    challenge_id: int,
    req: EnrollChallengeRequest,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict:
    """Inscribirse en un desafío (idempotente)."""
    user = _auth_user(authorization)
    if user is None:
        raise HTTPException(401, "log in to enroll in a challenge")
    key = idempotency_key or req.client_idempotency_key
    key = _require_idempotency_key(key)
    service = ChallengeService()
    rh = compute_operation_request_hash("enroll", challenge_id)
    result = service.enroll_participant(
        challenge_id=challenge_id,
        user_id=user.id,
        client_idempotency_key=key,
        request_hash=rh,
    )
    return {"challenge_id": challenge_id, "user_id": user.id, "retry": result.is_retry}


@app.get("/api/challenges/{challenge_id}/participants")
def api_list_participants(
    challenge_id: int,
    authorization: str | None = Header(default=None),
) -> dict:
    """Listar participantes según permisos del desafío."""
    user = _auth_user(authorization)
    if user is None:
        raise HTTPException(401, "authentication required")

    with db.SessionLocal() as db_:
        challenge_repo = ChallengeRepository(db_)
        c = challenge_repo.get_challenge(challenge_id)
        if c is None:
            raise HTTPException(404, f"challenge {challenge_id} not found")

        participant_repo = ChallengeParticipantRepository(db_)
        is_organizer = user.id == c.organizer_user_id

        is_admin = False
        if not is_organizer:
            db_user = db_.get(db.UserRow, user.id)
            is_admin = db_user is not None and bool(db_user.is_admin)

        if is_organizer or is_admin:
            participants = participant_repo.get_participants(challenge_id)
        else:
            own_participant = participant_repo.get_participant(
                challenge_id, user.id
            )
            if own_participant is None:
                raise HTTPException(404, "challenge not found")
            participants = [own_participant]

        worlds = [
            {
                "id": p.id,
                "challenge_id": p.challenge_id,
                "user_id": p.user_id,
                "world_id": p.world_id,
                "state": p.state,
                "result_json": p.result_json,
                "result_verified": bool(p.result_verified),
            }
            for p in participants
        ]

    return {"participants": worlds}

@app.put("/api/challenges/{challenge_id}/close-enrollment")
def api_close_enrollment(
    challenge_id: int,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict:
    """Cerrar inscripciones (solo organizador)."""
    user = _auth_user(authorization)
    if user is None:
        raise HTTPException(401, "log in to close enrollments")
    key = _require_idempotency_key(idempotency_key)
    with db.SessionLocal() as db_:
        repo = ChallengeRepository(db_)
        c = repo.get_challenge(challenge_id)
    if c is None:
        raise HTTPException(404, f"challenge {challenge_id} not found")
    if c.organizer_user_id != user.id:
        raise HTTPException(403, "not the organizer")
    service = ChallengeService()
    rh = compute_operation_request_hash("close_enrollment", challenge_id)
    result = service.close_enrollment(
        challenge_id=challenge_id,
        organizer_user_id=user.id,
        client_idempotency_key=key,
        request_hash=rh,
    )
    c = result.challenge
    state = result.original_outcome if (result.is_retry and result.original_outcome) else c.state
    return {"id": c.id, "state": state, "closed_at": c.closed_at}


@app.put("/api/challenges/{challenge_id}/start")
def api_start_challenge(
    challenge_id: int,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict:
    """Iniciar ejecución del desafío (solo organizador)."""
    user = _auth_user(authorization)
    if user is None:
        raise HTTPException(401, "log in to start a challenge")
    key = _require_idempotency_key(idempotency_key)
    with db.SessionLocal() as db_:
        repo = ChallengeRepository(db_)
        c = repo.get_challenge(challenge_id)
    if c is None:
        raise HTTPException(404, f"challenge {challenge_id} not found")
    if c.organizer_user_id != user.id:
        raise HTTPException(403, "not the organizer")
    service = ChallengeService()
    rh = compute_operation_request_hash("start", challenge_id)
    result = service.start_challenge(
        challenge_id=challenge_id,
        organizer_user_id=user.id,
        client_idempotency_key=key,
        request_hash=rh,
    )
    c = result.challenge
    state = result.original_outcome if (result.is_retry and result.original_outcome) else c.state
    return {"id": c.id, "state": state}


@app.get("/api/challenges/{challenge_id}/results")
def api_challenge_results(
    challenge_id: int,
    authorization: str | None = Header(default=None),
) -> dict:
    """Consultar resultados respetando privacidad y permisos por rol."""
    user = _auth_user(authorization)

    import json

    with db.SessionLocal() as db_:
        crepo = ChallengeRepository(db_)
        c = crepo.get_challenge(challenge_id)
        if c is None:
            raise HTTPException(404, f"challenge {challenge_id} not found")

        is_public = c.state == "completed"
        is_organizer = user is not None and user.id == c.organizer_user_id

        is_admin = False
        if user is not None and not is_organizer:
            db_user = db_.get(db.UserRow, user.id)
            is_admin = db_user is not None and bool(db_user.is_admin)

        prepo = ChallengeParticipantRepository(db_)
        own_participant = None
        if user is not None and not is_organizer and not is_admin:
            own_participant = prepo.get_participant(challenge_id, user.id)

        # Organizador y administrador pueden consultar todas las filas.
        if is_organizer or is_admin:
            participants = prepo.get_participants(challenge_id)

        # Un participante inscrito solo puede consultar su propia fila.
        elif own_participant is not None:
            participants = [own_participant]

        # Un visitante o usuario ajeno solo puede consultar el ranking
        # público cuando el desafío terminó.
        elif is_public:
            participants = prepo.get_participants(challenge_id)

        else:
            raise HTTPException(404, f"challenge {challenge_id} not found")

        config = {}
        try:
            config = json.loads(c.config_json or "{}")
        except (TypeError, ValueError):
            pass
        primary_metric = config.get("primary_metric", "total_population")

        def parse_metrics(participant):
            try:
                value = json.loads(participant.result_json or "{}")
                return value if isinstance(value, dict) else {}
            except (TypeError, ValueError):
                return {}

        def sort_key(participant):
            metrics = parse_metrics(participant)
            values = [-metrics.get(primary_metric, 0)]
            for metric in ["tech_level", "wars_won", "total_population"]:
                if metric != primary_metric:
                    values.append(-metrics.get(metric, 0))
            values.append(participant.user_id)
            return tuple(values)

        # Solo los resultados confirmados participan del ranking público.
        confirmed_sorted = sorted(
            [p for p in participants if p.result_status == "confirmed"],
            key=sort_key,
        )

        # Visitantes y usuarios ajenos reciben exclusivamente el ranking
        # anónimo, incluso si están autenticados.
        is_public_view = not (is_organizer or is_admin or own_participant is not None)

        if is_public_view:
            results = []
            for idx, p in enumerate(confirmed_sorted):
                public_metrics = parse_metrics(p)
                results.append({
                    "alias": p.public_alias or f"participant_{p.id}",
                    "state": p.state,
                    "metrics": {
                        key: public_metrics[key]
                        for key in ("total_population", "wars_won", "tech_level", "influence_max")
                        if key in public_metrics
                    },
                    "result_verified": bool(p.result_verified),
                    "position": idx + 1,
                })
        else:
            results = [
                {
                    "user_id": p.user_id,
                    "world_id": p.world_id,
                    "state": p.state,
                    "result_json": p.result_json,
                    "result_status": p.result_status,
                    "result_verified": bool(p.result_verified),
                }
                for p in participants
            ]

    return {
        "challenge_id": challenge_id,
        "state": c.state,
        "primary_metric": primary_metric,
        "participants": results,
    }

@app.put("/api/challenges/{challenge_id}/complete")
def api_complete_challenge(
    challenge_id: int,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict:
    """Completar desafío (running → completed): ejecutar duration_ticks y calcular resultados."""
    user = _auth_user(authorization)
    if user is None:
        raise HTTPException(401, "log in to complete a challenge")
    key = _require_idempotency_key(idempotency_key)
    with db.SessionLocal() as db_:
        repo = ChallengeRepository(db_)
        c = repo.get_challenge(challenge_id)
    if c is None:
        raise HTTPException(404, f"challenge {challenge_id} not found")
    if c.organizer_user_id != user.id:
        raise HTTPException(403, "not the organizer")
    # FIX10-A-IDEM: el estado NO se pre-verifica aquí — el servicio evalúa el
    # ámbito de idempotencia ANTES de cualquier retorno temprano por estado
    # (§17.13); la transición inválida se traduce a HTTP 400 vía el handler de
    # ChallengeError. Un pre-check de estado bloquearía los reintentos
    # idempotentes (la clave ya existe y el servicio devuelve el resultado
    # original o el conflicto 409).
    service = ChallengeService()
    rh = compute_operation_request_hash("complete", challenge_id)
    result = service.complete_challenge(
        challenge_id=challenge_id,
        organizer_user_id=user.id,
        client_idempotency_key=key,
        request_hash=rh,
    )
    c = result.challenge
    state = result.original_outcome if (result.is_retry and result.original_outcome) else c.state
    return {"id": c.id, "state": state, "completed_at": c.completed_at}


@app.put("/api/challenges/{challenge_id}/confirm")
def api_confirm_challenge(
    challenge_id: int,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict:
    """Confirmar resultados del desafío (organizador/admin)."""
    user = _auth_user(authorization)
    if user is None:
        raise HTTPException(401, "log in to confirm results")
    key = _require_idempotency_key(idempotency_key)
    with db.SessionLocal() as db_:
        repo = ChallengeRepository(db_)
        c = repo.get_challenge(challenge_id)
    if c is None:
        raise HTTPException(404, f"challenge {challenge_id} not found")
    if c.organizer_user_id != user.id:
        # Verificar admin (autorización; no es un pre-check de estado)
        with db.SessionLocal() as db2:
            u = db2.get(db.UserRow, user.id)
            if u is None or not u.is_admin:
                raise HTTPException(403, "not authorized to confirm results")
    # FIX10-A-IDEM: sin pre-check de estado — el servicio evalúa la
    # idempotencia antes de la comprobación de estado (§17.13).
    service = ChallengeService()
    rh = compute_operation_request_hash("confirm", challenge_id)
    result = service.confirm_results(
        challenge_id=challenge_id,
        organizer_user_id=user.id,
        client_idempotency_key=key,
        request_hash=rh,
    )
    c = result.challenge
    state = result.original_outcome if (result.is_retry and result.original_outcome) else c.state
    return {"id": c.id, "state": state}


@app.post("/api/challenges/{challenge_id}/dispute")
def api_dispute_challenge(
    challenge_id: int,
    req: DisputeChallengeRequest,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict:
    """Participante presenta disputa sobre su propio resultado (completed → disputed)."""
    user = _auth_user(authorization)
    if user is None:
        raise HTTPException(401, "log in to dispute a challenge result")
    key = idempotency_key or req.client_idempotency_key
    key = _require_idempotency_key(key)
    with db.SessionLocal() as db_:
        repo = ChallengeRepository(db_)
        c = repo.get_challenge(challenge_id)
    if c is None:
        raise HTTPException(404, f"challenge {challenge_id} not found")
    # FIX10-A-IDEM: sin pre-check de estado — el servicio evalúa la
    # idempotencia antes de la comprobación de estado (§17.13).
    # Verificar que el usuario es participante (autorización, no estado)
    with db.SessionLocal() as db_:
        prepo = ChallengeParticipantRepository(db_)
        participant = prepo.get_participant(challenge_id, user.id)
    if participant is None:
        raise HTTPException(403, "only participants can dispute")
    service = ChallengeService()
    rh = compute_operation_request_hash("dispute", challenge_id)
    result = service.dispute_challenge(
        challenge_id=challenge_id,
        user_id=user.id,
        client_idempotency_key=key,
        request_hash=rh,
    )
    c = result.challenge
    state = result.original_outcome if (result.is_retry and result.original_outcome) else c.state
    return {"id": c.id, "state": state}


@app.post("/api/challenges/{challenge_id}/resolve")
def api_resolve_challenge(
    challenge_id: int,
    req: ResolveChallengeRequest,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict:
    """Resolver disputa (organizador/admin): disputed → resolved."""
    user = _auth_user(authorization)
    if user is None:
        raise HTTPException(401, "log in to resolve a challenge dispute")
    key = idempotency_key or req.client_idempotency_key
    key = _require_idempotency_key(key)
    with db.SessionLocal() as db_:
        repo = ChallengeRepository(db_)
        c = repo.get_challenge(challenge_id)
    if c is None:
        raise HTTPException(404, f"challenge {challenge_id} not found")
    # FIX10-A-IDEM: sin pre-check de estado — el servicio evalúa la
    # idempotencia antes de la comprobación de estado (§17.13).
    service = ChallengeService()
    rh = compute_operation_request_hash("resolve", challenge_id, resolution=req.resolution)
    result = service.resolve_challenge(
        challenge_id=challenge_id,
        organizer_user_id=user.id,
        client_idempotency_key=key,
        request_hash=rh,
        resolution=req.resolution,
    )
    c = result.challenge
    state = result.original_outcome if (result.is_retry and result.original_outcome) else c.state
    return {"id": c.id, "state": state}


@app.put("/api/challenges/{challenge_id}/cancel")
def api_cancel_challenge(
    challenge_id: int,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict:
    """Cancelar desafío (solo antes de running, solo organizador)."""
    user = _auth_user(authorization)
    if user is None:
        raise HTTPException(401, "log in to cancel a challenge")
    key = _require_idempotency_key(idempotency_key)
    with db.SessionLocal() as db_:
        repo = ChallengeRepository(db_)
        c = repo.get_challenge(challenge_id)
    if c is None:
        raise HTTPException(404, f"challenge {challenge_id} not found")
    if c.organizer_user_id != user.id:
        raise HTTPException(403, "not the organizer")
    service = ChallengeService()
    rh = compute_operation_request_hash("cancel", challenge_id)
    result = service.cancel_challenge(
        challenge_id=challenge_id,
        organizer_user_id=user.id,
        client_idempotency_key=key,
        request_hash=rh,
    )
    c = result.challenge
    state = result.original_outcome if (result.is_retry and result.original_outcome) else c.state
    return {"id": c.id, "state": state}


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
                "civilized": k.civilized,
                "golden_age": k.golden_age_until > eng.month,
                "drought": k.drought_until > eng.month,
                "happiness": round(k.happiness, 1),
                "faith": k.faith,
                "temples": sorted(k.temples),
                "wars_won": k.wars_won,
                "wars_lost": k.wars_lost,
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

    DEC09: the handler NEVER advances the simulation.  The only tick route
    is WorldRunner._run().  There is no fast-forward, no handler-driven
    tick, and no substitute engine.

    DEC16: nonexistent world → 404 (WS 4404); not_recoverable world → 409
    (WS 4409) and NO lazy runner is created for it.

    DEC20 (F7-A): same privacy policy as HTTP.  Identity comes from the
    ``Authorization`` header or the ``?token=`` query (browsers cannot set
    headers on a WebSocket handshake; the query carries the SESSION token,
    never ``user_token``).  Order: existence + visibility (4404) → DEC16
    (4409) → business.  A third party on a PRIVATE world is closed 4404,
    indistinguishable from a missing world.  A third party on a PUBLIC
    world receives ONLY the bounded allowlist representation (one
    ``world`` message) and is never subscribed to the runner — the
    critical prohibition: no complete snapshot to third parties merely
    because the world is public, on the handshake OR any later message.
    The OWNER gets the full Etapa 5 stream.
    """
    await ws.accept()
    runner = None
    try:
        # DEC20: identity — Authorization header first, ?token= query as
        # the browser-friendly fallback (session token only).
        user = _auth_user(ws.headers.get("authorization"))
        if user is None:
            from worldsim.server import auth as authmod

            query_token = ws.query_params.get("token")
            if query_token:
                user = authmod.user_from_token(query_token)
        # Order fixed by §14.3: existence + visibility (404) BEFORE the
        # DEC16 guard (409), so a private world can never be probed.
        row = _load_row(world_id)  # 404 → 4404
        _require_visible(row, user)  # private + third party → 404 → 4404
        # DEC16/§5: refuse not_recoverable worlds (409/4409) before any
        # runner is touched — no lazy runner, no substitute engine.
        _require_recoverable(world_id)

        if _is_owner(row, user):
            # -- owner: full Etapa 5 shared-clock stream --------------------
            try:
                enabled = json.loads(row.enabled_races_json or "[]") or None
            except (json.JSONDecodeError, TypeError):
                enabled = None
            sea = row.sea_level if row.sea_level is not None else 0.2
            # registry.get creates the runner lazily, but only for
            # recoverable worlds (the guard above already refused them).
            from worldsim.server.operation_service import OperationService
            operation_service = OperationService(db.SessionLocal, engine_version="1.0")
            runner = registry.get(
                world_id, row.seed, row.width, row.height, enabled, sea,
                operation_service=operation_service,
            )
            runner.subscribe(ws)
            await runner.start()
            # send an immediate snapshot so the client doesn't wait 0.5s
            await ws.send_text(json.dumps(_tick_payload(runner.eng)))
        else:
            # -- third party on a PUBLIC world: bounded representation ------
            # Prohibición crítica (§14.4): one explicit allowlist message;
            # the socket is NEVER subscribed, so no tick/stats/system
            # message can ever reach it, and no runner is created for it.
            await ws.send_text(
                json.dumps({"type": "world", **_public_detail(row, user)})
            )

        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=0.01)
            except TimeoutError:
                await asyncio.sleep(0.1)
                continue
            # Parse the client message; anything malformed → 4400 (invalid params).
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                await ws.close(code=ws_close_code(400))
                return
            if not isinstance(msg, dict):
                await ws.close(code=ws_close_code(400))
                return
            if "speed" in msg:
                try:
                    speed_val = max(1, min(120, int(msg["speed"])))
                except (TypeError, ValueError):
                    await ws.close(code=ws_close_code(400))
                    return
                # Owner drives its own runner; a third party may only reach
                # a runner that ALREADY exists (get_existing never creates
                # one — no lazy runner for spectators).
                target = (
                    runner if runner is not None
                    else registry.get_existing(world_id)
                )
                if target is not None:
                    target.speed = speed_val
            if "pause" in msg:
                # Etapa 4 / DEC14: la transición pasa por el coordinador
                # (lock por mundo + escritura persistida + espejo del
                # engine), de modo que fila, runner y engine quedan
                # coherentes incluso si este socket se desconecta o el
                # shutdown comienza a mitad de la transición.
                try:
                    if bool(msg["pause"]):
                        await pause_world(
                            world_id,
                            session_factory=db.SessionLocal,
                            registry=registry,
                        )
                    else:
                        await resume_world(
                            world_id,
                            session_factory=db.SessionLocal,
                            registry=registry,
                        )
                except TransitionError as exc:
                    # Vigencia no satisfecha (mundo eliminándose, runner
                    # cerrando, shutdown): estado intacto, solo aviso.
                    logger.warning(
                        "ws transition refused for world %s: %s",
                        world_id, exc,
                    )
                except WorldNotFoundError:
                    logger.warning(
                        "ws transition: world %s row no longer exists",
                        world_id,
                    )
            if "selected" in msg:
                try:
                    selected_id = (
                        int(msg["selected"])
                        if msg["selected"] is not None else None
                    )
                except (TypeError, ValueError):
                    await ws.close(code=ws_close_code(400))
                    return
                if runner is not None:
                    runner.set_selected(ws, selected_id)
                    if selected_id is None:
                        await ws.send_text(
                            json.dumps({"type": "stats", "stats": None})
                        )
                    else:
                        k = next(
                            (k for k in runner.eng.kingdoms if k.id == selected_id),
                            None,
                        )
                        if k is not None:
                            await ws.send_text(
                                json.dumps(
                                    {"type": "stats",
                                     "stats": runner.eng.kingdom_stats(k)}
                                )
                            )
                else:
                    # Third party (public world): no subscription, no stats
                    # channel — always an explicit empty selection.
                    await ws.send_text(
                        json.dumps({"type": "stats", "stats": None})
                    )
    except asyncio.CancelledError:
        # Cancelación del handler (p. ej. shutdown del servidor) mientras
        # esperaba una transición: la transición ya reconcilió su escritura
        # antes de propagar; retiramos el suscriptor para no dejar residuos
        # y propagamos la cancelación (regla 5 / Etapa 4 §4.6).
        if runner is not None:
            runner.unsubscribe(ws)
        raise
    except WebSocketDisconnect:
        if runner is not None:
            runner.unsubscribe(ws)
        return
    except HTTPException as exc:
        # DEC16: 404 → 4404, 409 → 4409, etc.
        await ws.close(code=ws_close_code(exc.status_code))
        return
    except Exception as exc:
        # Fallo temporal de infraestructura (o inesperado) → 503/4503;
        # se registra, nunca se oculta (regla 8).
        logger.error("ws world %s failed: %r", world_id, exc, exc_info=True)
        await ws.close(code=ws_close_code(503))
        return


@app.get("/api/identity")
def make_identity() -> dict:
    """Anonymous identity token for multiplayer (browser stores it)."""
    return {"token": secrets.token_hex(16)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8420)
