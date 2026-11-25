"""Administrative lifecycle for auditable world highlights (F7-E.1 / F7-E.3)."""

from __future__ import annotations

import calendar
import hashlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from worldsim.server import db
from worldsim.server.db import UserRow, WorldCheckpoint, WorldHighlight, WorldRow


class HighlightError(Exception):
    """Base error for highlight lifecycle operations."""


class WorldNotEligible(HighlightError):
    pass


class ActiveHighlightExists(HighlightError):
    pass


class NoActiveHighlight(HighlightError):
    pass


class HighlightNotFound(HighlightError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def add_calendar_months(value: datetime, months: int) -> datetime:
    """Add calendar months, clamping the day to the destination month's end."""
    month_index = value.year * 12 + (value.month - 1) + months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _begin_write_transaction(session) -> None:
    # SQLite has no SELECT FOR UPDATE. BEGIN IMMEDIATE serializes grant/revoke
    # writers before the eligibility and active-row checks. PostgreSQL uses
    # the row-level lock acquired by the caller's SELECT ... FOR UPDATE.
    if session.get_bind().dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def _eligible_world(session, world_id: int) -> WorldRow:
    world = session.scalars(
        select(WorldRow).where(WorldRow.id == world_id).with_for_update()
    ).first()
    if (
        world is None
        or world.deleted_at is not None
        or world.visibility != "public"
        or world.owner_user_id is None
        or world.status not in ("running", "paused")
    ):
        raise WorldNotEligible("world is not eligible for highlighting")

    owner_exists = session.scalar(
        select(UserRow.id).where(UserRow.id == world.owner_user_id)
    )
    if owner_exists is None:
        raise WorldNotEligible("world owner account is not valid")

    # This is deliberately read-only: do not call CheckpointRepository's
    # mutating corruption path and do not invoke recovery.
    candidates = session.scalars(
        select(WorldCheckpoint)
        .where(
            WorldCheckpoint.world_id == world_id,
            WorldCheckpoint.is_valid == 1,
        )
        .order_by(WorldCheckpoint.sequence.desc())
    ).all()
    for checkpoint in candidates:
        actual = hashlib.sha256(bytes(checkpoint.payload)).hexdigest()
        if actual == str(checkpoint.checksum).lower():
            return world
    raise WorldNotEligible("world has no intact valid checkpoint")


def grant_highlight(
    world_id: int,
    admin_user_id: int,
    reason: str,
    duration_months: int | None = None,
    *,
    session_factory=None,
    now: datetime | None = None,
) -> WorldHighlight:
    factory = session_factory or db.SessionLocal
    instant = now or utc_now()
    granted_at = utc_iso(instant)
    expires_at = utc_iso(add_calendar_months(instant, duration_months)) if duration_months is not None else None

    with factory() as session:
        try:
            _begin_write_transaction(session)
            _eligible_world(session, world_id)

            current = session.scalars(
                select(WorldHighlight)
                .where(
                    WorldHighlight.world_id == world_id,
                    WorldHighlight.is_active == 1,
                )
                .with_for_update()
            ).first()
            if current is not None:
                if current.expires_at is None or current.expires_at > granted_at:
                    raise ActiveHighlightExists("world already has an active highlight")
                # Expiration closes the active row but is not an administrative
                # revocation: preserve revoked_at/revoked_by as NULL.
                current.is_active = 0

            highlight = WorldHighlight(
                world_id=world_id,
                admin_user_id=admin_user_id,
                granted_at=granted_at,
                expires_at=expires_at,
                reason=reason.strip(),
                is_active=1,
            )
            session.add(highlight)
            session.flush()
            session.commit()
            session.refresh(highlight)
            return highlight
        except IntegrityError as exc:
            session.rollback()
            raise ActiveHighlightExists("world already has an active highlight") from exc
        except Exception:
            session.rollback()
            raise


def revoke_highlight(
    world_id: int,
    admin_user_id: int,
    *,
    session_factory=None,
    now: datetime | None = None,
) -> WorldHighlight:
    factory = session_factory or db.SessionLocal
    revoked_at = utc_iso(now or utc_now())
    with factory() as session:
        try:
            _begin_write_transaction(session)
            # Lock the world row to serialize with grants even when no active
            # highlight row exists yet.
            world = session.scalars(
                select(WorldRow).where(WorldRow.id == world_id).with_for_update()
            ).first()
            if world is None:
                raise NoActiveHighlight("no active highlight")
            current = session.scalars(
                select(WorldHighlight)
                .where(
                    WorldHighlight.world_id == world_id,
                    WorldHighlight.is_active == 1,
                )
                .with_for_update()
            ).first()
            if current is None:
                raise NoActiveHighlight("no active highlight")
            current.is_active = 0
            current.revoked_at = revoked_at
            current.revoked_by_admin_user_id = admin_user_id
            session.commit()
            session.refresh(current)
            return current
        except Exception:
            session.rollback()
            raise


def get_highlight_detail(highlight_id: int, *, session_factory=None) -> dict:
    factory = session_factory or db.SessionLocal
    with factory() as session:
        row = session.get(WorldHighlight, highlight_id)
        if row is None:
            raise HighlightNotFound("highlight not found")
        return {
            "id": row.id,
            "world_id": row.world_id,
            "admin_user_id": row.admin_user_id,
            "granted_at": row.granted_at,
            "expires_at": row.expires_at,
            "reason": row.reason,
            "is_active": bool(row.is_active),
            "revoked_at": row.revoked_at,
            "revoked_by_admin_user_id": row.revoked_by_admin_user_id,
        }
