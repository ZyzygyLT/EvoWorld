"""Database models for the worldsim server (worlds, predictions, chat).

.. autofunction:: ensure_schema
.. autofunction:: _ensure_challenge_participants_constraints
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import (CheckConstraint, Column, DateTime, Float, ForeignKey, ForeignKeyConstraint, Index, Integer, LargeBinary, String, Text, UniqueConstraint, create_engine, event, inspect, text)
from sqlalchemy.orm import DeclarativeBase, sessionmaker


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
    name = Column(String, default="")
    mode = Column(String, default="single")  # single | league
    season_years = Column(Integer, default=300)  # league: champion at N years
    season_over = Column(Integer, default=0)  # bool flag once the season ends
    enabled_races_json = Column(String, default="")  # JSON list, "" = all
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    sea_level = Column(Float, default=0.2)  # ocean threshold used at generation
    status = Column(String(20), nullable=False, default="running", server_default="running")
    last_checkpoint_at = Column(String, nullable=True)
    checkpoint_count = Column(Integer, nullable=False, default=0, server_default="0")
    checkpoint_version = Column(Integer, nullable=True)
    visibility = Column(String(7), nullable=False, default="private", server_default="private")
    next_operation_sequence = Column(Integer, nullable=False, default=1, server_default="1")
    deleted_at = Column(DateTime, nullable=True)  # NULL: mundo activo; timestamp: eliminado lógicamente

    __table_args__ = (CheckConstraint("visibility IN ('private', 'public')", name="ck_worlds_visibility"),)


class ClaimRow(Base):
    """Dynasty League: a user claims ONE kingdom in a world."""

    __tablename__ = "claims"

    id = Column(Integer, primary_key=True, autoincrement=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), index=True)
    kingdom_id = Column(Integer)
    user_token = Column(String, index=True)
    user_name = Column(String)
    month = Column(Integer)  # when the claim was sealed (scoring starts here)
    points = Column(Integer, default=0)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)


class PredictionRow(Base):
    __tablename__ = "predictions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), index=True)
    user_token = Column(String, index=True)
    user_name = Column(String)
    kind = Column(String)  # war_victory | era_reach | collapse | rebellion_any | ...
    params_json = Column(String)  # {"kingdom": 0, "era": 2, ...}
    month = Column(Integer)  # month when the prophecy was sealed
    deadline_months = Column(Integer)
    status = Column(String, default="pending")  # pending|won|lost|expired
    points = Column(Integer, default=0)
    resolved_month = Column(Integer, nullable=True)
    resolved_text = Column(String, nullable=True)


class ChatRow(Base):
    __tablename__ = "chat"

    id = Column(Integer, primary_key=True, autoincrement=True)
    world_id = Column(Integer, ForeignKey("worlds.id"), index=True)
    user_token = Column(String)
    user_name = Column(String)
    text = Column(String)
    month = Column(Integer)  # in-world month when it was said


class UserRow(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, index=True)
    pw_hash = Column(String)  # pbkdf2-sha256(salt, password), hex
    created_at = Column(String)
    is_admin = Column(Integer, nullable=False, default=0, server_default="0")


class SessionRow(Base):
    __tablename__ = "sessions"

    token = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    created_at = Column(String)


# Challenge row types (CONTRACTS §17, DEC-F7D)
# These are re-exported from worldsim.server.db so tests can import them


class ChallengeRow(Base):
    __tablename__ = "challenges"

    id = Column(Integer, primary_key=True)
    organizer_user_id = Column(Integer, nullable=False)
    name = Column(String, nullable=False)
    config_json = Column(Text, nullable=False)
    rules_version = Column(String, nullable=False)
    duration_ticks = Column(Integer, nullable=False)
    state = Column(String, default="draft")
    created_at = Column(String, nullable=False)
    client_idempotency_key = Column(String, nullable=False)
    request_hash = Column(String, nullable=False)


class ChallengeParticipantRow(Base):
    __tablename__ = "challenge_participants"

    id = Column(Integer, primary_key=True)
    challenge_id = Column(Integer, nullable=False)
    user_id = Column(Integer, nullable=False)
    world_id = Column(Integer, nullable=False)
    public_alias = Column(String, nullable=False)
    result_json = Column(Text, nullable=False)
    result_status = Column(String, default="persisted")
    result_verified = Column(Integer, default=0)
    state = Column(String, default="activo")


class ChallengeOperationRow(Base):
    __tablename__ = "challenge_operations"

    id = Column(Integer, primary_key=True)
    challenge_id = Column(Integer, nullable=False)
    operation_id = Column(String, nullable=False)
    status = Column(String, nullable=False)
    continuity_state = Column(String, nullable=False)
    created_at = Column(String, nullable=False)
    requesting_user_id = Column(Integer, nullable=False)
    client_idempotency_key = Column(String, nullable=False)
    request_hash = Column(String, nullable=False)
    engine_version = Column(String, default="1.0")
    checkpoint_id = Column(Integer, nullable=False, default=0)
    recovery_generation = Column(Integer, default=2)



class WorldOperation(Base):
    __tablename__ = "world_operations"
    __table_args__ = (
        UniqueConstraint("world_id", "operation_id", name="uq_world_operation_id"),
        UniqueConstraint("world_id", "operation_sequence", name="uq_world_operation_sequence"),
        UniqueConstraint("world_id", "requesting_user_id", "client_idempotency_key", name="uq_world_operation_idempotency"),
        CheckConstraint("status IN ('pending','executing','confirmed','blocked','failed','canceled')", name="ck_world_operation_status"),
        CheckConstraint("continuity_state IN ('active','blocked','released')", name="ck_world_operation_continuity"),
        CheckConstraint("((status = 'confirmed' AND continuity_state = 'active') OR (status IN ('pending','executing','blocked') AND continuity_state = 'active') OR (status IN ('failed','canceled') AND continuity_state IN ('blocked','released')))", name="ck_world_operation_status_continuity"),
        Index("ix_world_operations_world_id", "world_id"),
        ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    world_id = Column(Integer, nullable=False)
    operation_id = Column(String(64), nullable=False)
    operation_sequence = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False)
    continuity_state = Column(String(20), nullable=False)
    created_at = Column(String, nullable=False)
    confirmed_at = Column(String, nullable=True)
    requesting_user_id = Column(Integer, nullable=False)
    client_idempotency_key = Column(String(128), nullable=False)
    request_hash = Column(String(64), nullable=False)
    engine_version = Column(String(32), nullable=True, default="1.0")
    checkpoint_id = Column(Integer, ForeignKey("world_checkpoints.id"), nullable=True)
    payload_ref = Column(Text, nullable=True)
    rng_state_ref = Column(Text, nullable=True)
    recovery_state = Column(Text, nullable=True)
    initial_checkpoint_id = Column(Integer, ForeignKey("world_checkpoints.id"), nullable=True)
    recovery_attempt_id = Column(String(36), nullable=True)
    recovery_generation = Column(Integer, nullable=False, default=0, server_default="0")


class WorldEvent(Base):
    __tablename__ = "world_events"
    __table_args__ = (
        ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["world_id", "operation_id"], ["world_operations.world_id", "world_operations.operation_id"], ondelete="CASCADE", name="fk_world_event_operation"),
        UniqueConstraint("world_id", "operation_id", "position", name="uq_world_event_operation_position"),
        CheckConstraint("position >= 0", name="ck_world_event_position"),
        Index("ix_world_events_world_sequence", "world_id", "operation_sequence"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    world_id = Column(Integer, nullable=False)
    operation_id = Column(String(64), nullable=False)
    operation_sequence = Column(Integer, nullable=False)
    position = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    event_type = Column(String, nullable=False)
    event_text = Column(String, nullable=False)
    kingdom_ids = Column(Text, nullable=False)
    event_params = Column(Text, nullable=False)
    msg_key = Column(String, nullable=False)
    created_at = Column(String, nullable=False)


class OperationResolution(Base):
    __tablename__ = "operation_resolutions"
    __table_args__ = (
        ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["world_id", "operation_id"], ["world_operations.world_id", "world_operations.operation_id"], ondelete="CASCADE", name="fk_operation_resolution_operation"),
        UniqueConstraint("world_id", "operation_id", "idempotency_key", name="uq_operation_resolution_idempotency"),
        CheckConstraint("resolution_type IN ('confirmed_no_effects','continue_with_gap','remain_blocked')", name="ck_operation_resolution_type"),
        Index("ix_operation_resolutions_world_id", "world_id"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    world_id = Column(Integer, nullable=False)
    operation_id = Column(String(64), nullable=False)
    operation_sequence = Column(Integer, nullable=False)
    admin_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    resolution_type = Column(String(32), nullable=False)
    reason = Column(Text, nullable=False)
    evidence_refs = Column(Text, nullable=True)
    resolution_version = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(String, nullable=False)
    idempotency_key = Column(String(128), nullable=False)


class WorldCheckpoint(Base):
    __tablename__ = "world_checkpoints"
    __table_args__ = (
        UniqueConstraint("world_id", "sequence", name="uq_world_checkpoint_seq"),
        ForeignKeyConstraint(["world_id"], ["worlds.id"], ondelete="CASCADE"),
        Index("ix_world_checkpoints_world_id", "world_id"),
        Index("ix_world_checkpoints_world_month", "world_id", "month"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    world_id = Column(Integer, nullable=False)
    sequence = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    checkpoint_type = Column(String(20), nullable=False)
    payload = Column(LargeBinary, nullable=False)
    checksum = Column(String(64), nullable=False)
    created_at = Column(String, nullable=False)
    is_valid = Column(Integer, nullable=False, default=1, server_default="1")
    checkpoint_version = Column(Integer, nullable=False, default=2, server_default="2")



# Persistence: sqlite by default; set WORLDSIM_DB to any SQLAlchemy URL to
# switch (e.g. postgresql+psycopg://user:pass@host/worldsim for PostgreSQL).
_DB_URL = os.environ.get("WORLDSIM_DB")
if _DB_URL:
    engine_db = create_engine(_DB_URL, pool_pre_ping=True)
else:
    DB_PATH = Path(__file__).resolve().parent / "worlds.db"
    engine_db = create_engine(
        f"sqlite:///{DB_PATH}",
        connect_args={"check_same_thread": False},
    )

SessionLocal = sessionmaker(bind=engine_db)
logger = None  # type: ignore

_F7C_TABLES = ("world_operations", "world_events", "operation_resolutions")
_F7C_REQUIRED_COLUMNS = {
    "world_operations": {"world_id", "operation_id", "operation_sequence", "status", "continuity_state", "created_at", "confirmed_at", "requesting_user_id", "client_idempotency_key", "request_hash", "engine_version", "checkpoint_id", "payload_ref", "rng_state_ref", "recovery_state", "initial_checkpoint_id", "recovery_attempt_id", "recovery_generation"},
    "world_events": {"world_id", "operation_id", "operation_sequence", "position", "month", "event_type", "event_text", "kingdom_ids", "event_params", "msg_key", "created_at"},
    "operation_resolutions": {"world_id", "operation_id", "operation_sequence", "admin_user_id", "resolution_type", "reason", "evidence_refs", "resolution_version", "created_at", "idempotency_key"},
}


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _configure_engine(target_engine) -> None:
    if target_engine.dialect.name == "sqlite" and not event.contains(target_engine, "connect", _enable_sqlite_foreign_keys):
        event.listen(target_engine, "connect", _enable_sqlite_foreign_keys)


def _replace_empty_legacy_f7c_tables(target_engine) -> None:
    """Replace obsolete F7-C tables only after proving the complete family empty."""
    inspector = inspect(target_engine)
    existing = set(inspector.get_table_names())
    obsolete = any(
        table in existing
        and not required.issubset({c["name"] for c in inspector.get_columns(table)})
        for table, required in _F7C_REQUIRED_COLUMNS.items()
    )
    if not obsolete:
        return

    with target_engine.begin() as conn:
        counts = {
            table: int(conn.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()) if table in existing else 0
            for table in _F7C_TABLES
        }
        if any(counts.values()):
            raise RuntimeError(
                "Cannot migrate legacy F7-C schema: historical rows exist; "
                f"refusing destructive replacement (counts={counts})"
            )
        for table in ("world_events", "operation_resolutions", "world_operations"):
            if table in existing:
                conn.execute(text(f'DROP TABLE "{table}"'))


def ensure_schema(target_engine=None) -> None:
    """Create and explicitly migrate schema (DEC02; no import-time side effects).

    Existing worlds are not rebuilt: the F7-C allocator is added in place.
    Legacy F7-C tables are replaced only when every affected table is empty.
    """
    target = target_engine or engine_db
    _configure_engine(target)
    if target.dialect.name == "sqlite":
        _replace_empty_legacy_f7c_tables(target)

    Base.metadata.create_all(target)
    if target.dialect.name != "sqlite":
        return

    with target.begin() as conn:
        user_columns = {c["name"]: c for c in inspect(conn).get_columns("users")}
        if "is_admin" not in user_columns:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
            ))
        columns = {c["name"]: c for c in inspect(conn).get_columns("worlds")}
        if "next_operation_sequence" not in columns:
            conn.execute(text("ALTER TABLE worlds ADD COLUMN next_operation_sequence INTEGER NOT NULL DEFAULT 1"))
        elif columns["next_operation_sequence"]["nullable"]:
            raise RuntimeError(
                "worlds.next_operation_sequence exists but is nullable; "
                "refusing an implicit SQLite table rebuild"
            )

        conn.execute(text(
            "UPDATE worlds SET next_operation_sequence = "
            "(SELECT COALESCE(MAX(operation_sequence), 0) + 1 FROM world_operations "
            "WHERE world_operations.world_id = worlds.id) "
            "WHERE next_operation_sequence <= "
            "(SELECT COALESCE(MAX(operation_sequence), 0) FROM world_operations "
            "WHERE world_operations.world_id = worlds.id)"
        ))
        violations = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
        if violations:
            raise RuntimeError(f"Foreign-key violations after schema migration: {violations[:10]}")
