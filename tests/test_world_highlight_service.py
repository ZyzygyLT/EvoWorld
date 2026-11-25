from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from worldsim.server import db
from worldsim.server.db import Base, UserRow, WorldCheckpoint, WorldHighlight, WorldRow
from worldsim.server.highlight_service import (
    ActiveHighlightExists,
    WorldNotEligible,
    grant_highlight,
    revoke_highlight,
)


@pytest.fixture
def highlight_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'highlight-service.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    db.ensure_schema(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        admin = UserRow(username="admin", pw_hash="x", created_at="now", is_admin=1)
        owner = UserRow(username="owner", pw_hash="x", created_at="now", is_admin=0)
        session.add_all([admin, owner])
        session.flush()
        world = WorldRow(
            seed=42, width=20, height=20, months_simulated=0, land_pct=0.5,
            state_json="{}", created_at="now", owner_user_id=owner.id,
            visibility="public", status="running", deleted_at=None,
        )
        session.add(world)
        session.flush()
        good_payload = b'{"checkpoint":"good"}'
        bad_payload = b'{"checkpoint":"bad"}'
        session.add_all([
            WorldCheckpoint(
                world_id=world.id, sequence=1, month=0, checkpoint_type="baseline",
                payload=good_payload, checksum=hashlib.sha256(good_payload).hexdigest(),
                created_at="now", is_valid=1, checkpoint_version=2,
            ),
            WorldCheckpoint(
                world_id=world.id, sequence=2, month=1, checkpoint_type="periodic",
                payload=bad_payload, checksum="0" * 64,
                created_at="now", is_valid=1, checkpoint_version=2,
            ),
        ])
        ids = (admin.id, owner.id, world.id)
    yield engine, factory, ids
    engine.dispose()


def test_grant_uses_newest_intact_checkpoint_without_mutating_candidates(highlight_env):
    engine, factory, (admin_id, _owner_id, world_id) = highlight_env
    now = datetime(2026, 1, 31, 12, 0, tzinfo=timezone.utc)

    row = grant_highlight(
        world_id, admin_id, "featured for event", 1,
        session_factory=factory, now=now,
    )

    assert row.expires_at == "2026-02-28T12:00:00Z"
    with factory() as session:
        checkpoints = session.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == world_id)
            .order_by(WorldCheckpoint.sequence)
        ).all()
        assert [checkpoint.is_valid for checkpoint in checkpoints] == [1, 1]
        assert checkpoints[0].checksum == hashlib.sha256(checkpoints[0].payload).hexdigest()
        assert session.scalar(
            select(WorldHighlight.id).where(WorldHighlight.id == row.id)
        ) == row.id


def test_grant_rejects_existing_unexpired_highlight(highlight_env):
    _engine, factory, (admin_id, _owner_id, world_id) = highlight_env
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    grant_highlight(world_id, admin_id, "first", session_factory=factory, now=now)

    with pytest.raises(ActiveHighlightExists):
        grant_highlight(world_id, admin_id, "second", session_factory=factory, now=now)


def test_grant_closes_expired_row_without_faking_revocation(highlight_env):
    _engine, factory, (admin_id, _owner_id, world_id) = highlight_env
    first_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    second_now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    first = grant_highlight(
        world_id, admin_id, "first", 1, session_factory=factory, now=first_now
    )
    second = grant_highlight(
        world_id, admin_id, "second", session_factory=factory, now=second_now
    )

    with factory() as session:
        old = session.get(WorldHighlight, first.id)
        new = session.get(WorldHighlight, second.id)
        assert old.is_active == 0
        assert old.revoked_at is None
        assert old.revoked_by_admin_user_id is None
        assert new.is_active == 1
        assert session.scalar(select(func.count()).select_from(WorldHighlight)) == 2


def test_concurrent_grants_serialize_to_one_active_highlight(highlight_env):
    _engine, factory, (admin_id, _owner_id, world_id) = highlight_env
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)

    def attempt(reason):
        try:
            return grant_highlight(
                world_id, admin_id, reason, session_factory=factory, now=now
            )
        except ActiveHighlightExists:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["concurrent A", "concurrent B"]))

    assert sum(result is not None for result in results) == 1
    with factory() as session:
        active = session.scalars(
            select(WorldHighlight).where(
                WorldHighlight.world_id == world_id,
                WorldHighlight.is_active == 1,
            )
        ).all()
        assert len(active) == 1


def test_revoke_is_auditable_and_keeps_history(highlight_env):
    _engine, factory, (admin_id, _owner_id, world_id) = highlight_env
    now = datetime(2026, 4, 1, tzinfo=timezone.utc)
    granted = grant_highlight(world_id, admin_id, "reason", session_factory=factory, now=now)
    revoked = revoke_highlight(
        world_id, admin_id, session_factory=factory,
        now=datetime(2026, 4, 2, tzinfo=timezone.utc),
    )

    assert revoked.id == granted.id
    assert revoked.is_active == 0
    assert revoked.revoked_at == "2026-04-02T00:00:00Z"
    assert revoked.revoked_by_admin_user_id == admin_id
    with factory() as session:
        assert session.scalar(
            select(WorldHighlight.id).where(WorldHighlight.id == granted.id)
        ) == granted.id


@pytest.mark.parametrize(
    "changes",
    [
        {"visibility": "private"},
        {"status": "closed"},
        {"deleted_at": datetime(2026, 1, 1)},
        {"owner_user_id": None},
    ],
)
def test_ineligible_world_is_rejected(highlight_env, changes):
    _engine, factory, (admin_id, _owner_id, world_id) = highlight_env
    with factory.begin() as session:
        world = session.get(WorldRow, world_id)
        for key, value in changes.items():
            setattr(world, key, value)

    with pytest.raises(WorldNotEligible):
        grant_highlight(
            world_id, admin_id, "not eligible", session_factory=factory,
            now=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )


def test_world_without_intact_checkpoint_is_rejected(highlight_env):
    _engine, factory, (admin_id, _owner_id, world_id) = highlight_env
    with factory.begin() as session:
        for checkpoint in session.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == world_id)
        ):
            checkpoint.checksum = "f" * 64

    with pytest.raises(WorldNotEligible):
        grant_highlight(
            world_id, admin_id, "no intact checkpoint", session_factory=factory,
            now=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )


# ---------------------------------------------------------------------------
# F7-E.3 P0 closure coverage — additive service tests.
# ---------------------------------------------------------------------------

def test_world_with_missing_owner_account_is_rejected_without_highlight(highlight_env):
    engine, factory, (admin_id, _owner_id, world_id) = highlight_env

    # Create an orphan only inside this isolated test database. The production
    # schema and its foreign-key constraints remain unchanged.
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute(
            "UPDATE worlds SET owner_user_id = ? WHERE id = ?",
            (999999, world_id),
        )
        raw.commit()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    finally:
        raw.close()

    with pytest.raises(WorldNotEligible):
        grant_highlight(
            world_id, admin_id, "orphan owner", session_factory=factory,
            now=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )

    with factory() as session:
        assert session.scalar(
            select(WorldHighlight.id).where(WorldHighlight.world_id == world_id)
        ) is None


def test_paused_world_with_intact_checkpoint_is_eligible(highlight_env):
    _engine, factory, (admin_id, _owner_id, world_id) = highlight_env
    with factory.begin() as session:
        session.get(WorldRow, world_id).status = "paused"

    row = grant_highlight(
        world_id, admin_id, "paused world", session_factory=factory,
        now=datetime(2026, 8, 2, tzinfo=timezone.utc),
    )
    assert row.is_active == 1


def test_invalid_checkpoint_is_ignored_even_when_checksum_matches(highlight_env):
    _engine, factory, (admin_id, _owner_id, world_id) = highlight_env
    with factory.begin() as session:
        checkpoints = session.scalars(
            select(WorldCheckpoint).where(WorldCheckpoint.world_id == world_id)
        ).all()
        for checkpoint in checkpoints:
            checkpoint.is_valid = 0

    with pytest.raises(WorldNotEligible):
        grant_highlight(
            world_id, admin_id, "invalid candidates", session_factory=factory,
            now=datetime(2026, 8, 3, tzinfo=timezone.utc),
        )


def test_payload_mutation_with_original_checksum_is_rejected(highlight_env):
    _engine, factory, (admin_id, _owner_id, world_id) = highlight_env
    with factory.begin() as session:
        checkpoint = session.scalar(
            select(WorldCheckpoint)
            .where(WorldCheckpoint.world_id == world_id)
            .order_by(WorldCheckpoint.sequence)
        )
        checkpoint.payload = b'{"checkpoint":"tampered"}'

    with pytest.raises(WorldNotEligible):
        grant_highlight(
            world_id, admin_id, "tampered payload", session_factory=factory,
            now=datetime(2026, 8, 4, tzinfo=timezone.utc),
        )


def test_multiple_corrupt_candidates_fall_back_to_oldest_intact(highlight_env):
    _engine, factory, (admin_id, _owner_id, world_id) = highlight_env
    payload = b'{"checkpoint":"oldest-intact"}'
    with factory.begin() as session:
        session.add_all([
            WorldCheckpoint(
                world_id=world_id, sequence=3, month=2, checkpoint_type="periodic",
                payload=b"bad-3", checksum="3" * 64, created_at="now",
                is_valid=1, checkpoint_version=2,
            ),
            WorldCheckpoint(
                world_id=world_id, sequence=4, month=3, checkpoint_type="periodic",
                payload=b"bad-4", checksum="4" * 64, created_at="now",
                is_valid=1, checkpoint_version=2,
            ),
        ])
        oldest = session.scalar(
            select(WorldCheckpoint)
            .where(WorldCheckpoint.world_id == world_id, WorldCheckpoint.sequence == 1)
        )
        oldest.payload = payload
        oldest.checksum = hashlib.sha256(payload).hexdigest()

    row = grant_highlight(
        world_id, admin_id, "fallback through corrupt candidates",
        session_factory=factory, now=datetime(2026, 8, 5, tzinfo=timezone.utc),
    )
    assert row.is_active == 1


def test_checkpoint_verification_preserves_all_candidate_fields(highlight_env):
    _engine, factory, (admin_id, _owner_id, world_id) = highlight_env
    fields = ("payload", "checksum", "is_valid", "sequence", "checkpoint_version")

    def snapshot():
        with factory() as session:
            rows = session.scalars(
                select(WorldCheckpoint)
                .where(WorldCheckpoint.world_id == world_id)
                .order_by(WorldCheckpoint.sequence)
            ).all()
            return [
                tuple(getattr(row, field) for field in fields)
                for row in rows
            ]

    before = snapshot()
    grant_highlight(
        world_id, admin_id, "read-only checkpoint validation",
        session_factory=factory, now=datetime(2026, 8, 6, tzinfo=timezone.utc),
    )
    assert snapshot() == before
