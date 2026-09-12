"""Dynasty League: users claim kingdoms; the chronicle scores their fates.

Scoring table (sealed with the claim month — a late joiner never earns the
past; that's what makes claiming early a real decision):

    positive                          negative
    -------------------------------   --------------------------
    TECH_ERA            +10           WAR lost               -10
    WAR won             +15           PLAGUE                 -10
    City founded         +8           KINGDOM_COLLAPSED      -25
    HERO_EMERGED        +10
    BEAST_SLAIN         +10
    MILESTONE           +20
    GOLDEN_AGE           +5
    SITE captured         +5
"""

from __future__ import annotations

import json

from sqlalchemy import select

from worldsim.server import db
from worldsim.server.db import ClaimRow, WorldRow
from worldsim.sim.models import EventType

SCORE_TABLE: dict[str, int] = {
    str(EventType.TECH_ERA): 10,
    str(EventType.HERO_EMERGED): 10,
    str(EventType.BEAST_SLAIN): 10,
    str(EventType.GOLDEN_AGE): 5,
    str(EventType.SITE_CAPTURED): 5,  # capturing side only (first kingdom id)
    str(EventType.PLAGUE): -10,
    str(EventType.KINGDOM_COLLAPSED): -25,
}

# events resolved from the wars list rather than the chronicle directly
WAR_WON_POINTS = 15
WAR_LOST_POINTS = -10
CITY_FOUNDED_POINTS = 8
MILESTONE_POINTS = 20


def _session():
    # resolved via the db module (not a direct import) so tests can rebind
    # db.SessionLocal to an isolated database in one place
    return db.SessionLocal()  # type: ignore[call-arg]


def get_claims(world_id: int) -> list[dict]:
    with _session() as db:
        rows = db.scalars(
            select(ClaimRow).where(ClaimRow.world_id == world_id)
        ).all()
        return [
            {
                "id": r.id,
                "kingdom_id": r.kingdom_id,
                "user_token": r.user_token,
                "user_name": r.user_name,
                "month": r.month,
                "points": r.points,
            }
            for r in rows
        ]


def claim_kingdom(
    world_id: int, kingdom_id: int, user_token: str, user_name: str, month: int,
    user_id: int | None = None,
) -> dict | str:
    """Claim a free kingdom (one per user per world). Returns payload or error."""
    with _session() as db:
        row = db.scalars(
            select(WorldRow).where(WorldRow.id == world_id)
        ).first()
        if row is None:
            return "world not found"
        if row.mode != "league":
            return "this world is not a league world"

        # one kingdom per user
        mine = db.scalars(
            select(ClaimRow).where(
                ClaimRow.world_id == world_id, ClaimRow.user_token == user_token
            )
        ).first()
        if mine is not None:
            return f"you already claimed {mine.kingdom_id}"
        taken = db.scalars(
            select(ClaimRow).where(
                ClaimRow.world_id == world_id, ClaimRow.kingdom_id == kingdom_id
            )
        ).first()
        if taken is not None:
            return f"already claimed by {taken.user_name}"

        row = ClaimRow(
            world_id=world_id,
            kingdom_id=kingdom_id,
            user_token=user_token,
            user_name=user_name,
            month=month,
            points=0,
            user_id=user_id,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        payload = {
            "id": row.id,
            "kingdom_id": kingdom_id,
            "user_token": user_token,
            "user_name": user_name,
            "month": month,
            "points": 0,
        }
        # attach the claimed kingdom's name for toasts
        kingdoms = db.scalars(
            select(WorldRow).where(WorldRow.id == world_id)
        ).first()
        if kingdoms is not None:
            try:
                state = json.loads(kingdoms.state_json)
                for k in state.get("kingdoms", []):
                    if k["id"] == kingdom_id:
                        payload["kingdom_name"] = k["name"]
                        break
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        return payload


def standings(world_id: int) -> list[dict]:
    """League scoreboard: points per user + their kingdom."""
    claims = get_claims(world_id)
    board = [
        {
            "user": c["user_name"],
            "user_token": c["user_token"],
            "kingdom_id": c["kingdom_id"],
            "points": c["points"],
        }
        for c in claims
    ]
    board.sort(key=lambda u: -u["points"])
    for rank, u in enumerate(board, 1):
        u["rank"] = rank
    return board


def score_new_events(
    world_id: int, eng, since_month: int
) -> list[str]:
    """Score chronicle events newer than since_month for claimed kingdoms.

    Returns toast-ready messages. Points are added to the owning claim.
    """
    claims = get_claims(world_id)
    if not claims:
        return []
    by_kid = {c["kingdom_id"]: c for c in claims}
    messages: list[str] = []

    # chronicle events since the cursor
    for ev in eng.chronicle.events:
        if ev.month <= since_month:
            continue
        for kid in ev.kingdom_ids:
            claim = by_kid.get(kid)
            if claim is None:
                continue
            # claims only score events AFTER they were sealed
            if ev.month <= claim["month"]:
                continue
            delta = _event_delta(ev, kid)
            if delta == 0:
                continue
            _apply_points(claim["id"], delta)
            claim["points"] += delta
            messages.append(_toast_text(claim["user_name"], delta, ev.text))
            break  # score once per event per claim

    # war outcomes + city foundings need extra parsing
    for claim in claims:
        for w in eng.wars:
            if not w.is_over or w.end_month is None or w.end_month <= since_month:
                continue
            if w.end_month <= claim["month"]:
                continue
            if w.victor is not None:
                loser = w.side_b if w.victor is w.side_a else w.side_a
                if w.victor.id == claim["kingdom_id"] and w.victor.id in by_kid:
                    _apply_points(claim["id"], WAR_WON_POINTS)
                    by_kid[claim["kingdom_id"]]["points"] += WAR_WON_POINTS
                    messages.append(
                        _toast_text(
                            claim["user_name"], WAR_WON_POINTS,
                            f"{w.victor.name} won {w.name}",
                        )
                    )
                elif loser.id == claim["kingdom_id"]:
                    _apply_points(claim["id"], WAR_LOST_POINTS)
                    by_kid[claim["kingdom_id"]]["points"] += WAR_LOST_POINTS
                    messages.append(
                        _toast_text(
                            claim["user_name"], WAR_LOST_POINTS,
                            f"{loser.name} lost {w.name}",
                        )
                    )
        # city foundings are scored through _event_delta (SITE_FOUNDED "(City)")
    return messages


def _event_delta(ev, kid: int) -> int:
    t = ev.type
    if t == EventType.TECH_ERA:
        return SCORE_TABLE[str(EventType.TECH_ERA)]
    if t == EventType.HERO_EMERGED:
        return SCORE_TABLE[str(EventType.HERO_EMERGED)]
    if t == EventType.BEAST_SLAIN:
        return SCORE_TABLE[str(EventType.BEAST_SLAIN)]
    if t == EventType.GOLDEN_AGE:
        return SCORE_TABLE[str(EventType.GOLDEN_AGE)]
    if t == EventType.PLAGUE:
        return SCORE_TABLE[str(EventType.PLAGUE)]
    if t == EventType.KINGDOM_COLLAPSED:
        return SCORE_TABLE[str(EventType.KINGDOM_COLLAPSED)]
    if t == EventType.SITE_CAPTURED:
        # only the capturing side (first kingdom id) scores
        if ev.kingdom_ids and ev.kingdom_ids[0] == kid:
            return SCORE_TABLE[str(EventType.SITE_CAPTURED)]
        return 0
    if t == EventType.MILESTONE:
        return MILESTONE_POINTS
    if t == EventType.SITE_FOUNDED and "(City)" in ev.text:
        return CITY_FOUNDED_POINTS
    return 0


def _apply_points(claim_id: int, delta: int) -> None:
    with _session() as db:
        row = db.get(ClaimRow, claim_id)
        row.points += delta
        db.commit()


def _toast_text(user: str, delta: int, text: str) -> str:
    sign = f"+{delta}" if delta > 0 else str(delta)
    return f"👑 {user} {sign} — {text}"


def maybe_end_season(world_id: int, eng) -> str | None:
    """At season_years, freeze the world and declare a champion."""
    with _session() as db:
        row = db.scalars(select(WorldRow).where(WorldRow.id == world_id)).first()
        if row is None or row.mode != "league" or row.season_over:
            return None
        limit_month = row.season_years * 12
        if eng.month < limit_month:
            return None
        row.season_over = 1
        db.commit()
    board = standings(world_id)
    if not board:
        return "The age of the league has ended."
    champ = board[0]
    return (
        f"🏆 THE LEAGUE HAS ENDED — {champ['user']}'s kingdom is crowned "
        f"champion with {champ['points']} points!"
    )
