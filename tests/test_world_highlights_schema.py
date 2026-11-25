from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from worldsim.server import db


def _schema_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'highlights.db'}")
    db.ensure_schema(engine)
    return engine


def test_world_highlights_schema_and_partial_indexes(tmp_path):
    engine = _schema_engine(tmp_path)
    try:
        inspector = inspect(engine)
        assert "world_highlights" in inspector.get_table_names()
        columns = {column["name"]: column for column in inspector.get_columns("world_highlights")}
        assert {
            "id", "world_id", "admin_user_id", "granted_at", "expires_at",
            "reason", "is_active", "revoked_at", "revoked_by_admin_user_id",
        } <= set(columns)
        assert columns["is_active"]["nullable"] is False
        assert str(columns["is_active"]["default"]) in ("1", "'1'")

        foreign_keys = inspector.get_foreign_keys("world_highlights")
        world_fk = next(fk for fk in foreign_keys if fk["constrained_columns"] == ["world_id"])
        assert world_fk["referred_table"] == "worlds"
        assert world_fk["options"].get("ondelete") == "RESTRICT"
        assert any(fk["constrained_columns"] == ["admin_user_id"] for fk in foreign_keys)
        assert any(fk["constrained_columns"] == ["revoked_by_admin_user_id"] for fk in foreign_keys)

        indexes = {index["name"]: index for index in inspector.get_indexes("world_highlights")}
        assert indexes["uq_world_highlights_active"]["unique"] == 1
        assert "ix_world_highlights_world_id" in indexes
        assert "ix_world_highlights_active_expiry" in indexes

        active_index = next(
            index for index in db.WorldHighlight.__table__.indexes
            if index.name == "uq_world_highlights_active"
        )
        postgres_ddl = str(CreateIndex(active_index).compile(dialect=postgresql.dialect()))
        assert "WHERE is_active = 1" in postgres_ddl
    finally:
        engine.dispose()


def test_world_highlights_partial_uniqueness_and_world_retention(tmp_path):
    engine = _schema_engine(tmp_path)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (id, username) VALUES (1, 'admin')"))
            conn.execute(text(
                "INSERT INTO worlds (id, visibility, status, next_operation_sequence) "
                "VALUES (1, 'public', 'running', 1)"
            ))
            conn.execute(text(
                "INSERT INTO world_highlights "
                "(world_id, admin_user_id, granted_at, reason, is_active) "
                "VALUES (1, 1, '2026-11-24T00:00:00Z', 'featured', 1)"
            ))

        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO world_highlights "
                    "(world_id, admin_user_id, granted_at, reason, is_active) "
                    "VALUES (1, 1, '2026-11-24T00:01:00Z', 'duplicate', 1)"
                ))

        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE world_highlights SET is_active = 0 WHERE world_id = 1"
            ))
            conn.execute(text(
                "INSERT INTO world_highlights "
                "(world_id, admin_user_id, granted_at, reason, is_active) "
                "VALUES (1, 1, '2026-11-24T00:02:00Z', 'replacement', 1)"
            ))

        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM worlds WHERE id = 1"))

        with engine.connect() as conn:
            assert conn.execute(text(
                "SELECT COUNT(*) FROM world_highlights WHERE world_id = 1"
            )).scalar_one() == 2
    finally:
        engine.dispose()
