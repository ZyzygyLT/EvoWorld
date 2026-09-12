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
