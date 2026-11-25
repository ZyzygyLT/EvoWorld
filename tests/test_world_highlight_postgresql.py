"""PostgreSQL integration tests for concurrent world-highlight lifecycle operations.

These tests require an explicitly configured, dedicated test database:
EVOWORLD_TEST_POSTGRES_URL=postgresql+psycopg://.../evoworld_test

Each test gets a random schema. Cleanup drops only that schema; the fixture
never reads WORLDSIM_DB and refuses database names other than evoworld_test.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from worldsim.server import db
from worldsim.server.db import UserRow, WorldCheckpoint, WorldHighlight, WorldRow
from worldsim.server.highlight_service import (
    ActiveHighlightExists,
    NoActiveHighlight,
    grant_highlight,
    revoke_highlight,
)


@pytest.fixture
def postgres_highlight_env():
    """Create an isolated PostgreSQL schema and a minimal eligible world."""
    database_url = os.environ.get("EVOWORLD_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("EVOWORLD_TEST_POSTGRES_URL is not configured")

    pytest.importorskip("psycopg")
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("EVOWORLD_TEST_POSTGRES_URL must use PostgreSQL")
    if url.database != "evoworld_test":
        pytest.fail(
            "refusing PostgreSQL integration tests unless the database is "
            "explicitly named evoworld_test"
        )

    schema = f"evoworld_e3_{uuid.uuid4().hex}"
    admin_engine = create_engine(url, pool_pre_ping=True)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_engine(
        url,
        connect_args={"options": f"-csearch_path={schema}"},
        pool_pre_ping=True,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        db.ensure_schema(engine)
        with factory.begin() as session:
            admin = UserRow(
                username=f"e3-admin-{uuid.uuid4().hex}",
                pw_hash="test",
                created_at="2026-11-24T00:00:00Z",
                is_admin=1,
            )
            owner = UserRow(
                username=f"e3-owner-{uuid.uuid4().hex}",
                pw_hash="test",
                created_at="2026-11-24T00:00:00Z",
                is_admin=0,
            )
            session.add_all([admin, owner])
            session.flush()

            world = WorldRow(
                seed=42,
                width=8,
                height=8,
                months_simulated=0,
                land_pct=0.5,
                state_json="{}",
                created_at="2026-11-24T00:00:00Z",
                owner_user_id=owner.id,
                visibility="public",
                status="running",
                deleted_at=None,
            )
            session.add(world)
            session.flush()

            payload = b'{"postgres-highlight-checkpoint":true}'
            session.add(
                WorldCheckpoint(
                    world_id=world.id,
                    sequence=1,
                    month=0,
                    checkpoint_type="baseline",
                    payload=payload,
                    checksum=hashlib.sha256(payload).hexdigest(),
                    created_at="2026-11-24T00:00:00Z",
                    is_valid=1,
                    checkpoint_version=2,
                )
            )
            ids = (admin.id, owner.id, world.id)

        yield engine, factory, ids
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


def test_postgresql_concurrent_grants_create_exactly_one_active_highlight(
    postgres_highlight_env,
):
    _engine, factory, (admin_id, _owner_id, world_id) = postgres_highlight_env
    barrier = Barrier(2)

    def compete(reason: str):
        barrier.wait(timeout=10)
        try:
            row = grant_highlight(
                world_id,
                admin_id,
                reason,
                session_factory=factory,
                now=datetime(2026, 11, 24, tzinfo=timezone.utc),
            )
            return ("granted", row.id)
        except ActiveHighlightExists:
            return ("conflict", None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(compete, ("concurrent-A", "concurrent-B")))

    assert sorted(result[0] for result in results) == ["conflict", "granted"]
    with factory() as session:
        rows = session.scalars(
            select(WorldHighlight).where(WorldHighlight.world_id == world_id)
        ).all()
        assert len(rows) == 1
        assert sum(row.is_active == 1 for row in rows) == 1


def test_postgresql_concurrent_grant_and_revoke_preserve_serial_history(
    postgres_highlight_env,
):
    _engine, factory, (admin_id, _owner_id, world_id) = postgres_highlight_env
    original = grant_highlight(
        world_id,
        admin_id,
        "initial highlight",
        session_factory=factory,
        now=datetime(2026, 11, 24, tzinfo=timezone.utc),
    )
    barrier = Barrier(2)

    def compete_grant():
        barrier.wait(timeout=10)
        try:
            row = grant_highlight(
                world_id,
                admin_id,
                "concurrent replacement",
                session_factory=factory,
                now=datetime(2026, 11, 24, 1, tzinfo=timezone.utc),
            )
            return ("granted", row.id)
        except ActiveHighlightExists:
            return ("conflict", None)

    def compete_revoke():
        barrier.wait(timeout=10)
        try:
            row = revoke_highlight(
                world_id,
                admin_id,
                session_factory=factory,
                now=datetime(2026, 11, 24, 2, tzinfo=timezone.utc),
            )
            return ("revoked", row.id)
        except NoActiveHighlight:
            return ("no_active", None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        grant_future = executor.submit(compete_grant)
        revoke_future = executor.submit(compete_revoke)
        grant_result = grant_future.result(timeout=20)
        revoke_result = revoke_future.result(timeout=20)

    assert grant_result[0] in {"granted", "conflict"}
    assert revoke_result[0] == "revoked"

    with factory() as session:
        rows = session.scalars(
            select(WorldHighlight)
            .where(WorldHighlight.world_id == world_id)
            .order_by(WorldHighlight.id)
        ).all()

    assert rows[0].id == original.id
    assert rows[0].is_active == 0
    assert rows[0].revoked_at is not None
    assert rows[0].revoked_by_admin_user_id == admin_id
    assert sum(row.is_active == 1 for row in rows) <= 1

    if grant_result[0] == "granted":
        assert revoke_result[0] == "revoked"
        assert len(rows) == 2
        assert rows[1].is_active == 1
        assert rows[1].revoked_at is None
        assert rows[1].revoked_by_admin_user_id is None
    else:
        assert grant_result[0] == "conflict"
        assert len(rows) == 1
