"""DEC20 / DEC21 (F7-A): visibility system — full §12 checklist.

Requires FastAPI + httpx; the whole module SKIPs without them (DEC07).

Covered:
  * Persistence & migration: preexisting → 'public', new → 'private',
    repeatable, no NULL/invalid rows, data preserved, ownerless stay
    public (no invented owners), fail-closed on later startups, DB CHECK.
  * AuthN/AuthZ: anonymous create 401, owner from session, client-supplied
    owner ignored, publish/withdraw matrix (401/403/404/200/400), no
    private-world revelation on ANY route.
  * Public representation: CLOSED allowlist (exact keys), no state_json /
    snapshots / battle_heat, ``mine`` derived from identity.
  * HTTP: list/detail/aux gating, 404-before-409 precedence, per-request
    visibility reads (withdrawal takes effect immediately).
  * WebSocket: private third party → 4404, public third party → bounded
    allowlist message only (never subscribed → no later-message leak),
    owner → full Etapa 5 stream, withdrawal with a live connection.
  * DEC21: competitive identity from the authenticated session only,
    user_token is not identity, no client impersonation, no global
    competitive endpoints exist yet (F7-B scope).
"""

from __future__ import annotations

import itertools
import logging
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketDisconnect

import worldsim.server.app as appmod
import worldsim.server.db as dbmod
import worldsim.server.runner as runnermod
from worldsim.server.db import Base
from worldsim.server.errors import ws_close_code

_seq = itertools.count(1)

# closed allowlists under test (must mirror app.py — asserted independently)
FICHA_KEYS = {
    "id", "name", "mode", "seed", "months_simulated", "land_pct",
    "created_at", "season_years", "season_over", "visibility", "mine",
    "is_highlighted",
}
MAP_KEYS = {"width", "height", "biome", "elevation", "rivers"}


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

def _fresh(tmp_path: Path, name: str = "vis.db") -> TestClient:
    engine = create_engine(
        f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False}
    )
    dbmod.SessionLocal = sessionmaker(bind=engine)
    runnermod.registry = runnermod.RunnerRegistry()
    appmod.registry = runnermod.registry
    Base.metadata.create_all(engine)
    return TestClient(appmod.app)


def _register(c: TestClient, tag: str = "") -> dict:
    n = next(_seq)
    r = c.post("/api/auth/register",
               json={"username": f"vis{tag}{n}", "password": "pass1234"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _create(c: TestClient, headers: dict, **overrides) -> int:
    body = {"seed": 42, "width": 80, "height": 50, "months": 0}
    body.update(overrides)
    r = c.post("/api/worlds", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _publish(c: TestClient, wid: int, headers: dict,
             value: str = "public") -> dict:
    r = c.put(f"/api/worlds/{wid}/visibility",
              json={"visibility": value}, headers=headers)
    return r


def _ws_url(wid: int, headers: dict) -> str:
    tok = headers["Authorization"].split(" ", 1)[1]
    return f"/api/ws/{wid}?token={tok}"


# ===========================================================================
# 1. Persistence & migration (DEC20 / §14.2)
# ===========================================================================

LEGACY_DDL = """CREATE TABLE worlds (
    id INTEGER PRIMARY KEY,
    seed INTEGER,
    width INTEGER,
    height INTEGER,
    months_simulated INTEGER,
    land_pct FLOAT,
    state_json TEXT,
    created_at TEXT,
    name TEXT,
    mode TEXT,
    season_years INTEGER,
    season_over INTEGER,
    enabled_races_json TEXT,
    owner_user_id INTEGER,
    sea_level FLOAT
)"""

_LEGACY_ROWS = [
    dict(id=1, seed=42, width=80, height=50, months_simulated=24,
         land_pct=33.5, state_json='{"month": 24, "kingdoms": []}',
         created_at="2026-01-01", name="Mundo Viejo", mode="single",
         season_years=300, season_over=0),
    dict(id=2, seed=7, width=64, height=48, months_simulated=120,
         land_pct=41.0, state_json='{"month": 120}', created_at="2026-01-02",
         name="Legacy League", mode="league", season_years=300,
         season_over=0),
]


def _legacy_db(tmp_path: Path):
    """A pre-F7-A world table: no visibility column, no owner on rows."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy.db'}",
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as conn:
        conn.execute(text(LEGACY_DDL))
        for row in _LEGACY_ROWS:
            cols = ", ".join(row)
            ph = ", ".join(f":{k}" for k in row)
            conn.execute(
                text(f"INSERT INTO worlds ({cols}) VALUES ({ph})"), row
            )
    return engine


def _vis_rows(engine):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT id, name, seed, months_simulated, state_json, "
                 "visibility, owner_user_id FROM worlds ORDER BY id")
        ).fetchall()


def test_migration_preexisting_become_public_and_data_preserved(tmp_path):
    engine = _legacy_db(tmp_path)
    dbmod.ensure_schema(engine)

    rows = _vis_rows(engine)
    # preexisting rows → explicit 'public' (decision 3)
    assert [r.visibility for r in rows] == ["public", "public"]
    # data preserved byte-for-byte
    assert rows[0].name == "Mundo Viejo"
    assert rows[0].seed == 42
    assert rows[0].months_simulated == 24
    assert rows[0].state_json == '{"month": 24, "kingdoms": []}'
    assert rows[1].name == "Legacy League"
    # ownerless preexisting rows stay ownerless AND public (no invented owners)
    assert rows[0].owner_user_id is None
    assert rows[1].owner_user_id is None

    # no NULL or invalid values remain
    with engine.connect() as conn:
        nulls = conn.execute(
            text("SELECT COUNT(*) FROM worlds WHERE visibility IS NULL")
        ).scalar()
        invalid = conn.execute(
            text("SELECT COUNT(*) FROM worlds WHERE visibility NOT IN "
                 "('private','public')")
        ).scalar()
    assert nulls == 0 and invalid == 0


def test_migration_is_repeatable(tmp_path):
    engine = _legacy_db(tmp_path)
    dbmod.ensure_schema(engine)
    first = [(r.id, r.visibility, r.name) for r in _vis_rows(engine)]
    # second (and third) run: no error, no data change
    dbmod.ensure_schema(engine)
    dbmod.ensure_schema(engine)
    second = [(r.id, r.visibility, r.name) for r in _vis_rows(engine)]
    assert first == second
    assert all(v == "public" for _, v, _ in second)


def test_migration_new_rows_are_private(tmp_path):
    engine = _legacy_db(tmp_path)
    dbmod.ensure_schema(engine)
    # a row inserted AFTER the migration omits the column → DDL default
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO worlds (id, seed, width, height, "
                 "months_simulated, land_pct, state_json, created_at) "
                 "VALUES (3, 1, 32, 32, 0, 0.0, '{}', 'now')")
        )
    with engine.connect() as conn:
        v = conn.execute(
            text("SELECT visibility FROM worlds WHERE id = 3")
        ).scalar()
    assert v == "private"


def test_migration_fail_closed_null_on_later_startup(tmp_path, caplog):
    """A NULL seen on a LATER startup is an anomaly → forced 'private' + logged."""
    engine = _legacy_db(tmp_path)
    dbmod.ensure_schema(engine)
    # simulate an anomalous explicit NULL after the migration
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO worlds (id, seed, width, height, "
                 "months_simulated, land_pct, state_json, created_at, "
                 "visibility) VALUES (9, 5, 32, 32, 0, 0.0, '{}', 'now', "
                 "NULL)")
        )
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="worldsim.server.db"):
        dbmod.ensure_schema(engine)
    with engine.connect() as conn:
        v = conn.execute(
            text("SELECT visibility FROM worlds WHERE id = 9")
        ).scalar()
    assert v == "private"
    assert any("fail-closed" in rec.message for rec in caplog.records), (
        caplog.records
    )


def test_db_check_rejects_invalid_visibility(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'check.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        with pytest.raises(Exception) as exc:
            conn.execute(
                text("INSERT INTO worlds (id, seed, width, height, "
                     "months_simulated, land_pct, state_json, created_at, "
                     "status, checkpoint_count, visibility) "
                     "VALUES (1, 1, 32, 32, 0, 0.0, '{}', 'x', "
                     "'running', 0, 'banana')")
            )
    assert "ck_worlds_visibility" in str(exc.value) or "CHECK" in str(
        exc.value
    ).upper()


def test_new_schema_column_defaults_private(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO worlds (id, seed, width, height, "
                 "months_simulated, land_pct, state_json, created_at, "
                 "status, checkpoint_count) "
                 "VALUES (1, 1, 32, 32, 0, 0.0, '{}', 'x', 'running', 0)")
        )
        v = conn.execute(text("SELECT visibility FROM worlds WHERE id = 1")
                         ).scalar()
    assert v == "private"


def test_migrated_legacy_worlds_list_publicly(tmp_path):
    """After migration the plain list serves the preexisting worlds (DEC20:
    'el listado de mundos preexistentes conserva el comportamiento')."""
    engine = _legacy_db(tmp_path)
    dbmod.ensure_schema(engine)
    dbmod.SessionLocal = sessionmaker(bind=engine)
    runnermod.registry = runnermod.RunnerRegistry()
    appmod.registry = runnermod.registry
    c = TestClient(appmod.app)
    r = c.get("/api/worlds")
    assert r.status_code == 200
    worlds_page = r.json()
    by_id = {w["id"]: w for w in worlds_page["worlds"]}
    assert set(by_id) == {1, 2}
    assert by_id[1]["name"] == "Mundo Viejo"
    assert by_id[1]["visibility"] == "public"
    assert by_id[1]["mine"] is False


# ===========================================================================
# 1B. Paginated list contract (DEC-F7B-01 to DEC-F7B-05)
# ===========================================================================


def _make_published_world(c, headers, wid_suffix=0, name="World Pub"):
    """Create + publish a world, return its id."""
    body = {"seed": 42 + wid_suffix, "width": 80, "height": 50,
            "months": 0, "name": name}
    r = c.post("/api/worlds", json=body, headers=headers)
    assert r.status_code == 200, r.text
    wid = r.json()["id"]
    r = c.put(f"/api/worlds/{wid}/visibility",
              json={"visibility": "public"}, headers=headers)
    assert r.status_code == 200, r.text
    return wid


def test_paginated_first_page(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    ids = [_make_published_world(c, hdr, i) for i in range(5)]

    r = c.get("/api/worlds", params={"limit": 2})
    assert r.status_code == 200, r.text
    page = r.json()
    assert set(page) == {"worlds", "next_cursor"}
    assert len(page["worlds"]) == 2
    assert page["next_cursor"] is not None
    # order: id DESC (most recent first)
    assert page["worlds"][0]["id"] == max(ids)
    assert page["worlds"][1]["id"] == sorted(ids, reverse=True)[1]


def test_paginated_next_page(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    ids = sorted([_make_published_world(c, hdr, i) for i in range(5)],
                 reverse=True)

    r1 = c.get("/api/worlds", params={"limit": 2})
    assert r1.status_code == 200
    cursor = r1.json()["next_cursor"]
    assert cursor is not None

    r2 = c.get("/api/worlds", params={"limit": 2, "cursor": cursor})
    assert r2.status_code == 200
    page2 = r2.json()
    assert len(page2["worlds"]) == 2
    assert page2["worlds"][0]["id"] == ids[2]
    # no duplicates across pages
    ids_page1 = {w["id"] for w in r1.json()["worlds"]}
    ids_page2 = {w["id"] for w in page2["worlds"]}
    assert ids_page1.isdisjoint(ids_page2)


def test_paginated_last_page_null_cursor(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    _make_published_world(c, hdr)

    r = c.get("/api/worlds", params={"limit": 100})
    assert r.status_code == 200
    page = r.json()
    assert page["next_cursor"] is None


def test_paginated_limit_1(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    _make_published_world(c, hdr)
    _make_published_world(c, hdr, name="World B")

    r = c.get("/api/worlds", params={"limit": 1})
    assert r.status_code == 200
    page = r.json()
    assert len(page["worlds"]) == 1
    assert page["next_cursor"] is not None


def test_paginated_limit_validation(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    _make_published_world(c, hdr)

    r = c.get("/api/worlds", params={"limit": 0})
    assert r.status_code == 400
    r = c.get("/api/worlds", params={"limit": 101})
    assert r.status_code == 400


def test_paginated_cursor_validation(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    _make_published_world(c, hdr)

    assert c.get("/api/worlds", params={"cursor": "abc"}).status_code == 400
    assert c.get("/api/worlds", params={"cursor": "1"}).status_code == 400


def test_paginated_cursor_is_opaque_and_tamper_evident(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    for i in range(3):
        _make_published_world(c, hdr, i)

    first = c.get("/api/worlds", params={"limit": 1})
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    assert cursor.startswith("h1.")

    second = c.get("/api/worlds", params={"limit": 1, "cursor": cursor})
    assert second.status_code == 200
    assert len(second.json()["worlds"]) == 1
    assert second.json()["worlds"][0]["id"] != first.json()["worlds"][0]["id"]

    tampered = cursor[:-5] + ("A" if cursor[-5] != "A" else "B") + cursor[-4:]
    assert c.get("/api/worlds", params={"limit": 1, "cursor": tampered}).status_code == 400


def test_paginated_cursor_is_bound_to_filters_and_limit(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    for i in range(3):
        _make_published_world(c, hdr, i)

    cursor = c.get("/api/worlds", params={"limit": 1}).json()["next_cursor"]
    assert c.get("/api/worlds", params={"limit": 2, "cursor": cursor}).status_code == 400
    assert c.get(
        "/api/worlds", params={"limit": 1, "search": "other", "cursor": cursor}
    ).status_code == 400
    assert c.get(
        "/api/worlds", params={"limit": 1, "mode": "league", "cursor": cursor}
    ).status_code == 400


def test_highlights_sort_before_newer_unhighlighted_worlds_and_cursor_crosses_groups(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    older_id = _make_published_world(c, hdr, name="Older highlighted")
    newer_id = _make_published_world(c, hdr, name="Newer ordinary")

    import worldsim.server.db as dbmod_local
    from worldsim.server.db import WorldHighlight

    with dbmod_local.SessionLocal() as session:
        admin = session.query(dbmod_local.UserRow).first()
        session.add(WorldHighlight(
            world_id=older_id,
            admin_user_id=admin.id,
            granted_at="2026-11-24T00:00:00Z",
            expires_at=None,
            reason="contract test",
            is_active=1,
        ))
        session.commit()

    first = c.get("/api/worlds", params={"limit": 1})
    assert first.status_code == 200
    page1 = first.json()
    assert page1["worlds"][0]["id"] == older_id
    assert page1["worlds"][0]["is_highlighted"] is True
    cursor = page1["next_cursor"]

    second = c.get("/api/worlds", params={"limit": 1, "cursor": cursor})
    assert second.status_code == 200
    assert second.json()["worlds"][0]["id"] == newer_id
    assert second.json()["worlds"][0]["is_highlighted"] is False


def test_expired_highlight_is_not_publicly_active_and_get_is_read_only(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    world_id = _make_published_world(c, hdr, name="Expired highlight")

    import worldsim.server.db as dbmod_local
    from worldsim.server.db import WorldHighlight

    with dbmod_local.SessionLocal() as session:
        admin = session.query(dbmod_local.UserRow).first()
        session.add(WorldHighlight(
            world_id=world_id,
            admin_user_id=admin.id,
            granted_at="2026-01-01T00:00:00Z",
            expires_at="2026-02-01T00:00:00Z",
            reason="expired",
            is_active=1,
        ))
        session.commit()

    response = c.get("/api/worlds")
    assert response.status_code == 200
    world = next(item for item in response.json()["worlds"] if item["id"] == world_id)
    assert world["is_highlighted"] is False
    assert c.get("/api/worlds", params={"highlighted": "1"}).json()["worlds"] == []

    with dbmod_local.SessionLocal() as session:
        row = session.query(WorldHighlight).filter_by(world_id=world_id).one()
        assert row.is_active == 1
        assert row.revoked_at is None
        assert row.revoked_by_admin_user_id is None


def test_highlight_filter_returns_only_current_highlights(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    highlighted_id = _make_published_world(c, hdr, name="Highlighted")
    _make_published_world(c, hdr, name="Ordinary")

    import worldsim.server.db as dbmod_local
    from worldsim.server.db import WorldHighlight

    with dbmod_local.SessionLocal() as session:
        admin = session.query(dbmod_local.UserRow).first()
        session.add(WorldHighlight(
            world_id=highlighted_id,
            admin_user_id=admin.id,
            granted_at="2026-11-24T00:00:00Z",
            expires_at=None,
            reason="contract test",
            is_active=1,
        ))
        session.commit()

    response = c.get("/api/worlds", params={"highlighted": "1"})
    assert response.status_code == 200
    assert [world["id"] for world in response.json()["worlds"]] == [highlighted_id]
    assert response.json()["worlds"][0]["is_highlighted"] is True



def test_paginated_search_exact(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    _make_published_world(c, hdr, name="Alpha World")
    _make_published_world(c, hdr, name="Beta World")

    r = c.get("/api/worlds", params={"search": "Alpha"})
    assert r.status_code == 200
    page = r.json()
    assert len(page["worlds"]) == 1
    assert page["worlds"][0]["name"] == "Alpha World"


def test_paginated_search_case_insensitive(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    _make_published_world(c, hdr, name="Alpha World")

    r = c.get("/api/worlds", params={"search": "alpha"})
    assert r.status_code == 200
    assert len(r.json()["worlds"]) == 1


def test_paginated_search_too_long(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    _make_published_world(c, hdr)

    r = c.get("/api/worlds", params={"search": "a" * 65})
    assert r.status_code == 400


def test_paginated_search_wildcards_escaped(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    _make_published_world(c, hdr, name="100% done")
    _make_published_world(c, hdr, name="plain")

    # search for literal "%" should only match "100% done"
    r = c.get("/api/worlds", params={"search": "100%"})
    assert r.status_code == 200
    page = r.json()
    assert len(page["worlds"]) == 1
    assert page["worlds"][0]["name"] == "100% done"


def test_paginated_mode_filter(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    _make_published_world(c, hdr, name="Single", **{})
    # create a league world and publish
    body = {"seed": 7, "width": 80, "height": 50, "months": 0,
            "name": "League World", "mode": "league"}
    r = c.post("/api/worlds", json=body, headers=hdr)
    assert r.status_code == 200
    wid = r.json()["id"]
    c.put(f"/api/worlds/{wid}/visibility",
          json={"visibility": "public"}, headers=hdr)

    r = c.get("/api/worlds", params={"mode": "league"})
    assert r.status_code == 200
    page = r.json()
    assert len(page["worlds"]) == 1
    assert page["worlds"][0]["mode"] == "league"


def test_paginated_mode_invalid(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    _make_published_world(c, hdr)

    r = c.get("/api/worlds", params={"mode": "invalid"})
    assert r.status_code == 400


def test_mine_requires_auth(tmp_path):
    c = _fresh(tmp_path)
    r = c.get("/api/worlds?mine=1")
    assert r.status_code == 401


def test_mine_invalid_value(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    _make_published_world(c, hdr)

    r = c.get("/api/worlds?mine=0")
    assert r.status_code == 400
    r = c.get("/api/worlds?mine=true")
    assert r.status_code == 400
    r = c.get("/api/worlds?mine=2")
    assert r.status_code == 400


def test_mine_cursor_is_opaque_and_bound_to_owner_filter(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    _make_published_world(c, hdr, name="Mine A")
    _make_published_world(c, hdr, name="Mine B")

    first = c.get("/api/worlds", params={"mine": "1", "limit": 1}, headers=hdr)
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    assert cursor.startswith("h1.")

    second = c.get(
        "/api/worlds", params={"mine": "1", "limit": 1, "cursor": cursor},
        headers=hdr,
    )
    assert second.status_code == 200
    assert len(second.json()["worlds"]) == 1

    # The same token cannot be replayed against the public gallery.
    assert c.get("/api/worlds", params={"limit": 1, "cursor": cursor}).status_code == 400


def test_mine_owner_sees_private_and_public(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    # private world
    r = c.post("/api/worlds",
               json={"seed": 1, "width": 80, "height": 50, "name": "Priv"},
               headers=hdr)
    assert r.status_code == 200
    priv_id = r.json()["id"]
    # public world
    pub_id = _make_published_world(c, hdr, name="Pub")

    r = c.get("/api/worlds?mine=1", headers=hdr)
    assert r.status_code == 200
    page = r.json()
    ids = {w["id"] for w in page["worlds"]}
    assert priv_id in ids
    assert pub_id in ids
    for w in page["worlds"]:
        assert w["mine"] is True


def test_mine_third_party_sees_only_own(tmp_path):
    c = _fresh(tmp_path)
    hdr_a = _register(c, "a")
    hdr_b = _register(c, "b")
    _make_published_world(c, hdr_a, name="A's World")

    # B's ?mine=1 should not include A's world
    r = c.get("/api/worlds?mine=1", headers=hdr_b)
    assert r.status_code == 200
    assert len(r.json()["worlds"]) == 0


def test_list_no_state_json(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    _make_published_world(c, hdr, name="No State")

    r = c.get("/api/worlds")
    assert r.status_code == 200
    for w in r.json()["worlds"]:
        assert "state_json" not in w
        assert "owner_user_id" not in w
        assert set(w) == FICHA_KEYS


# ===========================================================================
# 2. Authentication & authorization matrix (DEC20 / §14.3)
# ===========================================================================

def test_anonymous_create_401(tmp_path):
    c = _fresh(tmp_path)
    r = c.post("/api/worlds", json={"seed": 1, "width": 80, "height": 50})
    assert r.status_code == 401


def test_create_owner_from_session_world_private(tmp_path):
    c = _fresh(tmp_path)
    hdr = _register(c)
    wid = _create(c, hdr)

    # persisted: owner = session user, visibility = explicit 'private'
    import worldsim.server.db as dbmod_local

    with dbmod_local.SessionLocal() as s:
        from worldsim.server.db import UserRow, WorldRow

        row = s.get(WorldRow, wid)
        user = s.query(UserRow).first()
        assert row.owner_user_id == user.id
        assert row.visibility == "private"

    # not in the plain list; present with mine=True in ?mine=1
    assert all(w["id"] != wid for w in c.get("/api/worlds").json()["worlds"])
    mine = c.get("/api/worlds?mine=1", headers=hdr).json()["worlds"]
    assert [w["id"] for w in mine] == [wid]
    assert mine[0]["visibility"] == "private"
    assert mine[0]["mine"] is True


def test_client_cannot_choose_owner(tmp_path):
    """DEC21/DEC20: a client-supplied owner identifier is never accepted
    (Pydantic ignores the extra field; owner comes from the session)."""
    c = _fresh(tmp_path)
    hdr = _register(c)
    r = c.post(
        "/api/worlds",
        json={"seed": 1, "width": 80, "height": 50,
              "owner_user_id": 999, "owner_id": 999, "owner": "mallory"},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    wid = r.json()["id"]
    import worldsim.server.db as dbmod_local

    with dbmod_local.SessionLocal() as s:
        from worldsim.server.db import UserRow, WorldRow

        row = s.get(WorldRow, wid)
        real = s.query(UserRow).first()
        assert row.owner_user_id == real.id  # session user, not 999


def test_put_visibility_matrix(tmp_path):
    c = _fresh(tmp_path)
    owner = _register(c, "o")
    other = _register(c, "t")

    wid = _create(c, owner)  # born private

    # anonymous → 401 (before anything else)
    r = c.put(f"/api/worlds/{wid}/visibility", json={"visibility": "public"})
    assert r.status_code == 401
    # nonexistent world (authenticated) → 404
    r = c.put("/api/worlds/9999/visibility",
              json={"visibility": "public"}, headers=owner)
    assert r.status_code == 404
    # third party on a PRIVATE world → 404 (unrevealed)
    r = _publish(c, wid, other, "public")
    assert r.status_code == 404
    # invalid value (owner) → 400
    r = _publish(c, wid, owner, "banana")
    assert r.status_code == 400
    # owner publishes → 200
    r = _publish(c, wid, owner, "public")
    assert r.status_code == 200 and r.json() == {"id": wid,
                                                 "visibility": "public"}
    # third party on a PUBLIC world → 403
    r = _publish(c, wid, other, "private")
    assert r.status_code == 403
    # owner withdraws → 200
    r = _publish(c, wid, owner, "private")
    assert r.status_code == 200 and r.json()["visibility"] == "private"
    # ownerless (legacy) world → 403 even for an authenticated user
    import worldsim.server.db as dbmod_local

    with dbmod_local.SessionLocal() as s:
        from worldsim.server.db import WorldRow

        s.add(WorldRow(seed=9, width=32, height=32, months_simulated=0,
                       land_pct=0.0, state_json="{}", created_at="x",
                       owner_user_id=None, visibility="public"))
        s.commit()
        legacy_id = s.query(WorldRow).filter(
            WorldRow.owner_user_id.is_(None)).first().id
    r = _publish(c, legacy_id, owner, "private")
    assert r.status_code == 403


def test_private_world_is_404_on_every_route(tmp_path):
    """No route reveals a private world to a third party (authenticated
    or anonymous): same 404 as a nonexistent world, never 403/409/500."""
    c = _fresh(tmp_path)
    owner = _register(c, "o")
    other = _register(c, "t")
    wid = _create(c, owner)  # private, not published

    anon_gets = [
        f"/api/worlds/{wid}",
        f"/api/worlds/{wid}/stats/0",
        f"/api/worlds/{wid}/predictions",
        f"/api/worlds/{wid}/leaderboard",
        f"/api/worlds/{wid}/chat",
        f"/api/worlds/{wid}/claims",
        f"/api/worlds/{wid}/standings",
    ]
    for path in anon_gets:
        for headers in (None, other):
            r = c.get(path, headers=headers)
            assert r.status_code == 404, (path, r.status_code, r.text)
            # error body indistinguishable from a missing world
            import re

            assert re.fullmatch(r"world \d+ not found",
                                r.json()["detail"]), r.json()

    # POSTs: visibility gate first → 404, never 409/400 (no probing)
    posts = [
        (f"/api/worlds/{wid}/predictions",
         {"kind": "beast_slain", "params": {},
          "user_token": "u" * 16, "user_name": "X"}),
        (f"/api/worlds/{wid}/chat",
         {"user_token": "u" * 16, "user_name": "X", "text": "hi"}),
        (f"/api/worlds/{wid}/claims",
         {"kingdom_id": 0, "user_token": "u" * 16, "user_name": "X"}),
    ]
    for path, payload in posts:
        for headers in (None, other):
            r = c.post(path, json=payload, headers=headers)
            assert r.status_code == 404, (path, r.status_code, r.text)

    # visibility change by third party → 404 (unrevealed), never 403
    r = _publish(c, wid, other, "public")
    assert r.status_code == 404
    # DELETE by an authenticated third party → 404 (unrevealed)
    r = c.delete(f"/api/worlds/{wid}", headers=other)
    assert r.status_code == 404
    # DELETE anonymous on an owned world → 401 (P-4 matrix)
    r = c.delete(f"/api/worlds/{wid}")
    assert r.status_code == 401

    # private worlds never appear in the plain list
    assert all(w["id"] != wid for w in c.get("/api/worlds").json()["worlds"])
    # ...nor in another user's ?mine=1
    assert all(w["id"] != wid
               for w in c.get("/api/worlds?mine=1", headers=other).json()["worlds"])


def test_public_third_party_aux_routes_still_work(tmp_path):
    """§14.5: on a PUBLIC world the per-world auxiliary routes keep their
    existing behavior for third parties (visibility gate only)."""
    c = _fresh(tmp_path)
    owner = _register(c, "o")
    other = _register(c, "t")
    wid = _create(c, owner, mode="league", months=5)
    assert _publish(c, wid, owner, "public").status_code == 200

    for path in (
        f"/api/worlds/{wid}/stats/0",
        f"/api/worlds/{wid}/predictions",
        f"/api/worlds/{wid}/leaderboard",
        f"/api/worlds/{wid}/chat",
        f"/api/worlds/{wid}/claims",
        f"/api/worlds/{wid}/standings",
    ):
        r = c.get(path)
        assert r.status_code == 200, (path, r.text)
        r = c.get(path, headers=other)
        assert r.status_code == 200, (path, r.text)

    # writes work too (business rules unchanged)
    r = c.post(f"/api/worlds/{wid}/chat",
               json={"user_token": "u" * 16, "user_name": "X", "text": "hi"})
    assert r.status_code == 200


def test_404_precedes_409_dec16(tmp_path, monkeypatch):
    """§14.3: existence+visibility (404) BEFORE the DEC16 guard (409) —
    a third party must not learn that a private world is not_recoverable."""
    c = _fresh(tmp_path)
    owner = _register(c, "o")
    other = _register(c, "t")
    wid = _create(c, owner)  # private

    class _Res:
        status = "not_recoverable"
        detail = "no checkpoint"

    class _Coord:
        not_recoverable = {wid: _Res()}

    appmod.app.state.coordinator = _Coord()
    try:
        # owner reaches the DEC16 guard → 409
        r = c.post(f"/api/worlds/{wid}/chat",
                   json={"user_token": "u" * 16, "user_name": "X",
                         "text": "hi"},
                   headers=owner)
        assert r.status_code == 409
        # third party is stopped by the visibility gate → 404 (not 409)
        for headers in (None, other):
            r = c.post(f"/api/worlds/{wid}/chat",
                       json={"user_token": "u" * 16, "user_name": "X",
                             "text": "hi"},
                       headers=headers)
            assert r.status_code == 404, r.status_code
    finally:
        del appmod.app.state.coordinator


def test_visibility_read_is_per_request_no_cache(tmp_path):
    """§14.6: every read checks the PERSISTED value — withdrawal is
    effective on the very next request (no visibility cache)."""
    c = _fresh(tmp_path)
    owner = _register(c, "o")
    wid = _create(c, owner)

    # withdrawn → third party 404
    r = c.get(f"/api/worlds/{wid}")
    assert r.status_code == 404
    # published → third party 200 (bounded)
    assert _publish(c, wid, owner, "public").status_code == 200
    r = c.get(f"/api/worlds/{wid}")
    assert r.status_code == 200 and r.json()["visibility"] == "public"
    # withdrawn again → 404 immediately
    assert _publish(c, wid, owner, "private").status_code == 200
    r = c.get(f"/api/worlds/{wid}")
    assert r.status_code == 404
    # interleaved reads always coherent (never a stale public answer)
    for i in range(3):
        assert _publish(c, wid, owner, "public").status_code == 200
        assert c.get(f"/api/worlds/{wid}").status_code == 200
        assert _publish(c, wid, owner, "private").status_code == 200
        assert c.get(f"/api/worlds/{wid}").status_code == 404


def test_delete_matrix_and_dec10_interaction(tmp_path):
    c = _fresh(tmp_path)
    owner = _register(c, "o")
    other = _register(c, "t")

    # owner deletes its own private world → DEC10 flow, 200
    wid = _create(c, owner)
    r = c.delete(f"/api/worlds/{wid}", headers=owner)
    assert r.status_code == 200 and r.json() == {"deleted": wid}
    assert c.get(f"/api/worlds/{wid}", headers=owner).status_code == 404

    # public world: third party authenticated → 403; anonymous → 401
    wid2 = _create(c, owner)
    assert _publish(c, wid2, owner, "public").status_code == 200
    assert c.delete(f"/api/worlds/{wid2}", headers=other).status_code == 403
    assert c.delete(f"/api/worlds/{wid2}").status_code == 401
    # owner still deletes it → 200
    assert c.delete(f"/api/worlds/{wid2}", headers=owner).status_code == 200
    # nonexistent → 404
    assert c.delete(f"/api/worlds/{wid2}", headers=owner).status_code == 404


# ===========================================================================
# 3. Bounded public representation (DEC20 / §14.4)
# ===========================================================================

def test_public_list_allowlist_exact(tmp_path):
    c = _fresh(tmp_path)
    owner = _register(c, "o")
    wid = _create(c, owner, name="Listado")
    assert _publish(c, wid, owner, "public").status_code == 200

    r = c.get("/api/worlds")
    assert r.status_code == 200
    worlds_page = r.json()
    entries = [w for w in worlds_page["worlds"] if w["id"] == wid]
    assert len(entries) == 1
    assert set(entries[0]) == FICHA_KEYS, set(entries[0])
    # forbidden fields never present
    for banned in ("state_json", "kingdoms", "world", "battle_heat",
                   "owner_user_id", "status", "enabled_races_json",
                   "checkpoint_count"):
        assert banned not in entries[0]


def test_public_detail_allowlist_exact(tmp_path):
    c = _fresh(tmp_path)
    owner = _register(c, "o")
    wid = _create(c, owner, months=12)
    assert _publish(c, wid, owner, "public").status_code == 200

    r = c.get(f"/api/worlds/{wid}")  # anonymous third party
    assert r.status_code == 200
    data = r.json()
    assert set(data) == FICHA_KEYS | {"world"}, set(data)
    assert set(data["world"]) == MAP_KEYS, set(data["world"])
    # no full snapshot anywhere in the payload
    blob = str(data)
    for banned in ("state_json", "battle_heat", "kingdoms", "chronicle",
                   "leader", "population"):
        assert banned not in blob, banned

    # another authenticated third party: same bounded shape
    other = _register(c, "t")
    r2 = c.get(f"/api/worlds/{wid}", headers=other)
    assert set(r2.json()) == FICHA_KEYS | {"world"}
    assert r2.json()["mine"] is False

    # owner: FULL state (kingdoms, month, complete world block)
    r3 = c.get(f"/api/worlds/{wid}", headers=owner)
    full = r3.json()
    assert full["month"] == 12
    assert isinstance(full["kingdoms"], list) and full["kingdoms"]
    assert "battle_heat" in full["world"]  # owner gets the complete data


def test_mine_derived_from_identity(tmp_path):
    c = _fresh(tmp_path)
    owner = _register(c, "o")
    other = _register(c, "t")
    wid = _create(c, owner)
    assert _publish(c, wid, owner, "public").status_code == 200

    # detail: third parties get the bounded ficha with mine derived
    assert c.get(f"/api/worlds/{wid}").json()["mine"] is False
    assert c.get(f"/api/worlds/{wid}",
                 headers=other).json()["mine"] is False
    # the OWNER's detail is the FULL state (mine only exists on the
    # bounded representation, which the owner never needs)
    own = c.get(f"/api/worlds/{wid}", headers=owner).json()
    assert "kingdoms" in own and "mine" not in own
    # public list: mine derived from the identity on the request
    anonymous_entry = next(
        w for w in c.get("/api/worlds").json()["worlds"] if w["id"] == wid
    )
    assert anonymous_entry["mine"] is False
    owner_list = c.get("/api/worlds", headers=owner).json()["worlds"]
    owner_entry = next(w for w in owner_list if w["id"] == wid)
    assert owner_entry["mine"] is True
    other_list = c.get("/api/worlds", headers=other).json()["worlds"]
    other_entry = next(w for w in other_list if w["id"] == wid)
    assert other_entry["mine"] is False


def test_fail_closed_null_visibility_on_read(tmp_path, caplog):
    """A row whose visibility is NULL (anomaly) is read as PRIVATE."""
    c = _fresh(tmp_path)
    owner = _register(c, "o")
    wid = _create(c, owner)
    import worldsim.server.db as dbmod_local

    with dbmod_local.SessionLocal() as s:
        from worldsim.server.db import WorldRow

        row = s.get(WorldRow, wid)
        row.visibility = None
        s.commit()

    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="worldsim.server.app"):
        r = c.get(f"/api/worlds/{wid}")  # anonymous
    assert r.status_code == 404
    assert all(w["id"] != wid for w in c.get("/api/worlds").json()["worlds"])
    assert any("fail-closed" in rec.message for rec in caplog.records), (
        [rec.message for rec in caplog.records]
    )
    # a third authenticated user is refused too: never fail-open
    other = _register(c, "t")
    assert c.get(f"/api/worlds/{wid}", headers=other).status_code == 404
    # while the OWNER keeps authorized access to (effectively) private
    r = c.get(f"/api/worlds/{wid}", headers=owner)
    assert r.status_code == 200 and "kingdoms" in r.json()


# ===========================================================================
# 4. WebSocket (DEC20 / §14.4)
# ===========================================================================

def test_ws_private_third_party_4404(tmp_path):
    c = _fresh(tmp_path)
    owner = _register(c, "o")
    other = _register(c, "t")
    wid = _create(c, owner)  # private
    # drop the runner seeded by create_world so we can prove the refused
    # WS attempts never create one
    runnermod.registry.unregister(wid)
    assert runnermod.registry.get_existing(wid) is None

    # anonymous third party → 4404 (same as a missing world)
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect(f"/api/ws/{wid}") as ws:
            ws.receive_json()
    assert exc.value.code == ws_close_code(404)
    # authenticated third party → 4404 too
    tok = other["Authorization"].split(" ", 1)[1]
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect(f"/api/ws/{wid}?token={tok}") as ws:
            ws.receive_json()
    assert exc.value.code == ws_close_code(404)
    # nonexistent world → identical 4404 (no existence leak)
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/ws/424242") as ws:
            ws.receive_json()
    assert exc.value.code == ws_close_code(404)
    # no runner was ever created for the refused attempts
    assert runnermod.registry.get_existing(wid) is None


def test_ws_owner_gets_full_etapa5_stream(tmp_path):
    c = _fresh(tmp_path)
    owner = _register(c, "o")
    wid = _create(c, owner, months=6)

    with c.websocket_connect(_ws_url(wid, owner)) as ws:
        data = ws.receive_json()
        assert data["type"] == "tick"
        assert data["month"] == 6
        assert isinstance(data["kingdoms"], list) and data["kingdoms"]
        runner = runnermod.registry.get_existing(wid)
        assert runner is not None  # owner path may create the runner

    # Authorization header works as well as ?token=
    with c.websocket_connect(f"/api/ws/{wid}",
                             headers=owner) as ws2:
        data = ws2.receive_json()
        assert data["type"] == "tick"


def test_ws_public_third_party_bounded_only(tmp_path):
    """Critical prohibition: a third party on a PUBLIC world gets ONE
    bounded allowlist message and is NEVER subscribed — no tick snapshot,
    no stats, no later-message leak."""
    c = _fresh(tmp_path)
    owner = _register(c, "o")
    wid = _create(c, owner, months=6)
    assert _publish(c, wid, owner, "public").status_code == 200

    with c.websocket_connect(f"/api/ws/{wid}") as ws:
        first = ws.receive_json()
        # bounded representation, explicitly typed
        assert first["type"] == "world"
        assert set(first) - {"type"} == FICHA_KEYS | {"world"}
        assert set(first["world"]) == MAP_KEYS
        assert first["visibility"] == "public"
        assert first["mine"] is False
        # never a full snapshot
        for banned in ("month", "kingdoms", "state_json", "battle_heat",
                       "chronicle", "leader"):
            assert banned not in first, banned

        # selection: explicit empty stats, never kingdom data
        ws.send_json({"selected": 0})
        reply = ws.receive_json()
        assert reply == {"type": "stats", "stats": None}

        # structural guarantee: the socket was never subscribed, so the
        # runner can never push a tick/stats/system message to it
        runner = runnermod.registry.get_existing(wid)
        if runner is not None:
            assert all(sub["ws"] is not ws
                       for sub in runner.subscribers.values())

    # the owner, connected in parallel, DOES get the full stream
    with c.websocket_connect(_ws_url(wid, owner)) as ws_own:
        data = ws_own.receive_json()
        assert data["type"] == "tick"
        assert data["month"] == 6


def test_ws_third_party_speed_never_creates_runner(tmp_path):
    """Spectators may use the shared speed control, but a WS message must
    never create a runner for them (no lazy runner, §12/§5)."""
    c = _fresh(tmp_path)
    owner = _register(c, "o")
    wid = _create(c, owner)
    assert _publish(c, wid, owner, "public").status_code == 200
    runnermod.registry.unregister(wid)  # start with no runner at all

    with c.websocket_connect(f"/api/ws/{wid}") as ws:
        ws.receive_json()  # bounded handshake message
        ws.send_json({"speed": 7, "selected": None})
        reply = ws.receive_json()
        assert reply == {"type": "stats", "stats": None}
    # no runner was created by the spectator's messages
    assert runnermod.registry.get_existing(wid) is None

    # with an owner-driven runner already alive, speed still works for all
    with c.websocket_connect(_ws_url(wid, owner)) as ws_own:
        ws_own.receive_json()
        runner = runnermod.registry.get_existing(wid)
        assert runner is not None
        with c.websocket_connect(f"/api/ws/{wid}") as ws_spec:
            ws_spec.receive_json()
            ws_spec.send_json({"speed": 9})
            # trigger loop iterations with a second message
            ws_spec.send_json({"selected": None})
            ws_spec.receive_json()
        assert runner.speed == 9


def test_ws_withdrawal_with_live_connection(tmp_path):
    """§11/§12: withdrawing publication while a third party is connected —
    the live socket (never subscribed) receives no simulation data, new
    connections are refused 4404, and the owner keeps their full stream."""
    c = _fresh(tmp_path)
    owner = _register(c, "o")
    wid = _create(c, owner, months=3)
    assert _publish(c, wid, owner, "public").status_code == 200

    with c.websocket_connect(f"/api/ws/{wid}") as live:
        handshake = live.receive_json()
        assert handshake["type"] == "world"

        # owner withdraws publication while the connection is live
        assert _publish(c, wid, owner, "private").status_code == 200

        # HTTP reads reflect the withdrawal on the very next request
        assert c.get(f"/api/worlds/{wid}").status_code == 404
        assert all(w["id"] != wid for w in c.get("/api/worlds").json()["worlds"])

        # new third-party connections are refused 4404
        with pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect(f"/api/ws/{wid}") as ws_new:
                ws_new.receive_json()
        assert exc.value.code == ws_close_code(404)

        # the live socket gets NO simulation data: only an explicit empty
        # selection reply, and it is not in the subscriber list
        live.send_json({"selected": 0})
        reply = live.receive_json()
        assert reply == {"type": "stats", "stats": None}
        runner = runnermod.registry.get_existing(wid)
        if runner is not None:
            assert all(sub["ws"] is not live
                       for sub in runner.subscribers.values())

    # the OWNER still receives the full stream after the withdrawal
    with c.websocket_connect(_ws_url(wid, owner)) as ws_own:
        data = ws_own.receive_json()
        assert data["type"] == "tick"
        assert data["month"] == 3


def test_ws_invalid_params_and_inexistent_still_dec16(tmp_path):
    """DEC16 codes preserved on top of the DEC20 gates."""
    c = _fresh(tmp_path)
    owner = _register(c, "o")
    wid = _create(c, owner)
    assert _publish(c, wid, owner, "public").status_code == 200

    # invalid message → 4400 (third party, after the bounded handshake)
    with c.websocket_connect(f"/api/ws/{wid}") as ws:
        ws.receive_json()
        ws.send_text("not json")
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == ws_close_code(400)

    # not_recoverable (owner, published) → 4409, no runner created
    class _Res:
        status = "not_recoverable"
        detail = "no checkpoint"

    class _Coord:
        not_recoverable = {wid: _Res()}

    appmod.app.state.coordinator = _Coord()
    try:
        runnermod.registry.unregister(wid)
        with pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect(_ws_url(wid, owner)) as ws:
                ws.receive_json()
        assert exc.value.code == ws_close_code(409)
        assert runnermod.registry.get_existing(wid) is None
    finally:
        del appmod.app.state.coordinator


# ===========================================================================
# 5. DEC21 — trusted competitive identity
# ===========================================================================

def test_dec21_claim_identity_from_session_only(tmp_path):
    """A claim's user_id comes from the AUTHENTICATED session; the body's
    user_token can never impersonate another account."""
    c = _fresh(tmp_path)

    def _register_with_id(tag: str) -> tuple[dict, int]:
        n = next(_seq)
        r = c.post("/api/auth/register",
                   json={"username": f"vis{tag}{n}", "password": "pass1234"})
        assert r.status_code == 200, r.text
        d = r.json()
        return {"Authorization": f"Bearer {d['token']}"}, d["user_id"]

    alice, alice_id = _register_with_id("a")
    mallory, mallory_id = _register_with_id("m")
    assert alice_id != mallory_id
    wid = _create(c, alice, mode="league")
    assert _publish(c, wid, alice, "public").status_code == 200

    # Mallory claims while sending ALICE's user_token in the body
    r = c.post(f"/api/worlds/{wid}/claims",
               json={"kingdom_id": 0, "user_token": "a" * 16,
                     "user_name": "Alice"},
               headers=mallory)
    assert r.status_code == 200, r.text

    import worldsim.server.db as dbmod_local

    with dbmod_local.SessionLocal() as s:
        from worldsim.server.db import ClaimRow

        claim = s.query(ClaimRow).filter_by(world_id=wid).first()
        # bound to MALLORY's session account, never Alice's
        assert claim.user_id == mallory_id
        assert claim.user_id != alice_id
        assert claim.user_token == "a" * 16  # stored as display metadata


def test_dec21_anonymous_claim_gets_no_user_id(tmp_path):
    """No authenticated session → the claim carries NO competitive identity."""
    c = _fresh(tmp_path)
    owner = _register(c, "o")
    wid = _create(c, owner, mode="league")
    assert _publish(c, wid, owner, "public").status_code == 200

    r = c.post(f"/api/worlds/{wid}/claims",
               json={"kingdom_id": 0, "user_token": "z" * 16,
                     "user_name": "Ghost"})
    assert r.status_code == 200, r.text

    import worldsim.server.db as dbmod_local

    with dbmod_local.SessionLocal() as s:
        from worldsim.server.db import ClaimRow

        claim = s.query(ClaimRow).filter_by(world_id=wid).first()
        assert claim.user_id is None  # unverified identity → no attribution


def test_dec21_no_global_competitive_endpoints(tmp_path):
    """F7-B scope guard: global competitive ranking endpoints do
    NOT exist yet. F7-D Nivel 1 permite /api/challenges para comparación
    estadística reproducible (DEC-F7D-10, §17.9), no para competición global."""
    paths = {getattr(r, "path", "") for r in appmod.app.routes}
    for banned in ("/api/ranking", "/api/leaderboard", "/api/seasons",
                   "/api/tournaments", "/api/global"):
        assert not any(p.startswith(banned) for p in paths), banned
    # /api/challenges SÍ está permitido por F7-D Nivel 1 (comparación estadística)
    assert any(p.startswith("/api/challenges") for p in paths), "F7-D requiere endpoints /api/challenges"


def test_dec21_session_ownership_and_competitive_identity_separate(tmp_path):
    """Session identity, world ownership and competitive identity remain
    three separate things (DEC21 §4): knowing a user_token grants neither
    ownership nor access."""
    c = _fresh(tmp_path)
    owner = _register(c, "o")
    other = _register(c, "t")
    wid = _create(c, owner)  # private

    # `other` knows an arbitrary user_token — it grants nothing
    r = c.get(f"/api/worlds/{wid}",
              headers={**other, "X-User-Token": "o" * 16})
    assert r.status_code == 404
    # and no header spelling an owner id changes anything either
    r = c.get(f"/api/worlds/{wid}", headers={**other, "X-Owner-Id": "1"})
    assert r.status_code == 404
    # the real owner still has access
    assert c.get(f"/api/worlds/{wid}", headers=owner).status_code == 200
