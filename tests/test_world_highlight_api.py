from __future__ import annotations

import hashlib
import itertools
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import worldsim.server.app as appmod
import worldsim.server.db as dbmod
import worldsim.server.runner as runnermod
from worldsim.server.db import Base, UserRow, WorldCheckpoint, WorldHighlight, WorldRow

_seq = itertools.count(1)


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'highlight-api.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    dbmod.ensure_schema(engine)
    monkeypatch.setattr(
        dbmod, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False)
    )
    registry = runnermod.RunnerRegistry()
    monkeypatch.setattr(runnermod, "registry", registry)
    monkeypatch.setattr(appmod, "registry", registry)
    # Do not enter TestClient's lifespan: it targets the process-global
    # production engine, while this test intentionally uses only its temp DB.
    test_client = TestClient(appmod.app)
    yield test_client
    engine.dispose()


def _register(client: TestClient, *, admin: bool = False):
    n = next(_seq)
    response = client.post(
        "/api/auth/register",
        json={"username": f"highlight{n}", "password": "pass1234"},
    )
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['token']}"}
    if admin:
        with dbmod.SessionLocal() as session:
            user = session.scalar(
                select(UserRow).where(UserRow.username == response.json()["username"])
            )
            user.is_admin = 1
            session.commit()
    return headers


def _public_world(client: TestClient, headers: dict, *, checkpoint: bool = True) -> int:
    response = client.post(
        "/api/worlds",
        headers=headers,
        json={"seed": 31, "width": 80, "height": 50, "months": 0, "name": "Featured"},
    )
    assert response.status_code == 200, response.text
    world_id = response.json()["id"]
    response = client.put(
        f"/api/worlds/{world_id}/visibility",
        headers=headers,
        json={"visibility": "public"},
    )
    assert response.status_code == 200, response.text
    if checkpoint:
        payload = b'{"month":0,"fixture":"highlight"}'
        with dbmod.SessionLocal() as session:
            session.add(WorldCheckpoint(
                world_id=world_id,
                sequence=1,
                month=0,
                checkpoint_type="baseline",
                payload=payload,
                checksum=hashlib.sha256(payload).hexdigest(),
                created_at="2026-11-24T00:00:00Z",
                is_valid=1,
                checkpoint_version=2,
            ))
            session.commit()
    return world_id


def test_admin_grant_duplicate_conflict_public_projection_and_revoke(client):
    admin = _register(client, admin=True)
    owner = _register(client)
    world_id = _public_world(client, owner)

    granted = client.post(
        f"/api/admin/worlds/{world_id}/highlight",
        headers=admin,
        json={"reason": "featured launch", "duration_months": 1},
    )
    assert granted.status_code == 201, granted.text
    highlight_id = granted.json()["world_highlight_id"]

    duplicate = client.post(
        f"/api/admin/worlds/{world_id}/highlight",
        headers=admin,
        json={"reason": "second grant"},
    )
    assert duplicate.status_code == 409

    gallery = client.get("/api/worlds")
    assert gallery.status_code == 200
    item = next(world for world in gallery.json()["worlds"] if world["id"] == world_id)
    assert item["is_highlighted"] is True

    public_detail = client.get(f"/api/worlds/{world_id}")
    assert public_detail.status_code == 200
    assert public_detail.json()["is_highlighted"] is True
    assert "state_json" not in public_detail.json()
    assert "admin_user_id" not in public_detail.json()

    private_projection = client.get("/api/worlds?mine=1", headers=admin)
    assert private_projection.status_code == 200
    owned = next(world for world in private_projection.json()["worlds"] if world["id"] == world_id)
    assert "is_highlighted" not in owned

    revoked = client.post(
        f"/api/admin/worlds/{world_id}/unhighlight",
        headers=admin,
    )
    assert revoked.status_code == 200
    assert revoked.json() == {"revoked": True}

    after_revoke = client.get("/api/worlds")
    item_after = next(world for world in after_revoke.json()["worlds"] if world["id"] == world_id)
    assert item_after["is_highlighted"] is False

    detail = client.get(f"/api/admin/world_highlights/{highlight_id}", headers=admin)
    assert detail.status_code == 200
    assert detail.json()["is_active"] is False
    assert detail.json()["revoked_by_admin_user_id"] is not None


def test_highlight_admin_authorization_and_duration_validation(client):
    ordinary = _register(client)
    admin = _register(client, admin=True)
    owner = _register(client)
    world_id = _public_world(client, owner)

    anonymous = client.post(
        f"/api/admin/worlds/{world_id}/highlight",
        json={"reason": "test"},
    )
    assert anonymous.status_code == 401

    forbidden = client.post(
        f"/api/admin/worlds/{world_id}/highlight",
        headers=ordinary,
        json={"reason": "test"},
    )
    assert forbidden.status_code == 403

    invalid_duration = client.post(
        f"/api/admin/worlds/{world_id}/highlight",
        headers=admin,
        json={"reason": "test", "duration_months": 0},
    )
    assert invalid_duration.status_code == 400

    empty_reason = client.post(
        f"/api/admin/worlds/{world_id}/highlight",
        headers=admin,
        json={"reason": "   "},
    )
    assert empty_reason.status_code == 400


def test_ineligible_world_cannot_be_highlighted(client):
    admin = _register(client, admin=True)
    owner = _register(client)
    world_id = _public_world(client, owner, checkpoint=False)

    response = client.post(
        f"/api/admin/worlds/{world_id}/highlight",
        headers=admin,
        json={"reason": "missing checkpoint"},
    )
    assert response.status_code == 400
    with dbmod.SessionLocal() as session:
        assert session.scalar(
            select(WorldHighlight.id).where(WorldHighlight.world_id == world_id)
        ) is None


def test_unhighlight_without_active_row_is_404(client):
    admin = _register(client, admin=True)
    owner = _register(client)
    world_id = _public_world(client, owner)

    response = client.post(
        f"/api/admin/worlds/{world_id}/unhighlight",
        headers=admin,
    )
    assert response.status_code == 404

def test_world_owner_cannot_highlight_own_world_even_if_admin(client):
    admin_owner = _register(client, admin=True)
    world_id = _public_world(client, admin_owner)

    response = client.post(
        f"/api/admin/worlds/{world_id}/highlight",
        headers=admin_owner,
        json={"reason": "self grant"},
    )
    assert response.status_code == 400
