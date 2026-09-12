"""Database models for the worldsim server (worlds, predictions, chat)."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Column, Float, ForeignKey, Integer, String, Text, create_engine
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

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, unique=True, index=True)
    pw_hash = Column(String)  # pbkdf2-sha256(salt, password), hex
    created_at = Column(String)


class SessionRow(Base):
    __tablename__ = "sessions"

    token = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    created_at = Column(String)


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


def ensure_schema() -> None:
    """Create tables + migrate old DBs (dev worlds.db predates league columns).

    create_all only creates MISSING tables; SQLite has no ADD COLUMN via
    SQLAlchemy migrations, so new columns on existing tables are added
    explicitly. Without this, any select on an old DB raises
    OperationalError (which once killed the shared runner task silently).
    """
    from sqlalchemy import inspect, text

    Base.metadata.create_all(engine_db)
    if engine_db.dialect.name != "sqlite":
        return
    insp = inspect(engine_db)
    with engine_db.begin() as conn:
        for table_name, table in Base.metadata.tables.items():
            if table_name not in insp.get_table_names():
                continue
            existing = {c["name"] for c in insp.get_columns(table_name)}
            for col in table.columns:
                if col.name not in existing and col.name != "id":
                    default = f" DEFAULT {col.server_default.arg}" if col.server_default else ""
                    conn.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col.type}{default}")
                    )


ensure_schema()  # migrate old dev DBs in place (ADD COLUMN is idempotent-safe)
