"""Tests for accounts: register/login/sessions, world ownership, claim linkage."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import worldsim.server.app as appmod
import worldsim.server.runner as runnermod
from worldsim.server.db import Base


def _fresh(tmp_path: Path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import worldsim.server.db as dbmod

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'test_auth.db'}",
        connect_args={"check_same_thread": False},
    )
    dbmod.SessionLocal = sessionmaker(bind=test_engine)
    runnermod.registry = runnermod.RunnerRegistry()
    appmod.registry = runnermod.registry
    Base.metadata.create_all(test_engine)
    return TestClient(appmod.app)


def test_register_and_login(tmp_path):
    c = _fresh(tmp_path)
    r = c.post("/api/auth/register", json={"username": "merlin", "password": "staff123"})
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    assert len(tok) >= 32

    # duplicate username rejected
    r = c.post("/api/auth/register", json={"username": "merlin", "password": "other"})
    assert r.status_code == 400

    # login works, wrong password rejected
    r = c.post("/api/auth/login", json={"username": "merlin", "password": "staff123"})
    assert r.status_code == 200
    assert r.json()["username"] == "merlin"
    r = c.post("/api/auth/login", json={"username": "merlin", "password": "nope"})
    assert r.status_code == 401

    # me + logout
    r = c.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and r.json()["username"] == "merlin"
    c.post("/api/auth/logout", headers={"Authorization": f"Bearer {tok}"})
    r = c.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


def test_register_validation(tmp_path):
    c = _fresh(tmp_path)
    short_name = c.post("/api/auth/register", json={"username": "ab", "password": "1234"})
    assert short_name.status_code in (400, 422)
    short_pw = c.post("/api/auth/register", json={"username": "okname", "password": "123"})
    assert short_pw.status_code in (400, 422)


def test_world_ownership_and_mine_filter(tmp_path):
    c = _fresh(tmp_path)
    tok = c.post("/api/auth/register", json={"username": "ada", "password": "qubit42"}).json()["token"]

    r = c.post("/api/worlds", json={"seed": 1, "width": 80, "height": 50},
               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    owned = r.json()["id"]
    r = c.post("/api/worlds", json={"seed": 2, "width": 80, "height": 50})
    assert r.status_code == 200
    anon = r.json()["id"]

    mine = c.get("/api/worlds?mine=1", headers={"Authorization": f"Bearer {tok}"}).json()
    assert [w["id"] for w in mine] == [owned]
    assert all(w["mine"] for w in mine)

    # anonymous ?mine=1 is rejected
    assert c.get("/api/worlds?mine=1").status_code == 401
    # full list still shows both
    assert {w["id"] for w in c.get("/api/worlds").json()} == {owned, anon}


def test_claim_links_account(tmp_path):
    c = _fresh(tmp_path)
    tok = c.post("/api/auth/register", json={"username": "gimli", "password": "axe1234"}).json()["token"]
    wid = c.post("/api/worlds", json={"seed": 5, "width": 80, "height": 50, "mode": "league"},
                 headers={"Authorization": f"Bearer {tok}"}).json()["id"]
    r = c.post(f"/api/worlds/{wid}/claims",
               json={"kingdom_id": 0, "user_token": "t" * 16, "user_name": "gimli"},
               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200

    import worldsim.server.db as dbmod
    from worldsim.server.db import ClaimRow

    with dbmod.SessionLocal() as db:
        from sqlalchemy import select

        row = db.scalars(select(ClaimRow).where(ClaimRow.world_id == wid)).first()
        assert row is not None and row.user_id is not None
