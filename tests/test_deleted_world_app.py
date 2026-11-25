"""F7-E.2-I B3-3: logical-deletion filters in app.py (A1, A2, A4, A6).

Scope / isolation notes:
- A5 (the DELETE endpoint's internal row read) is deliberately UNFILTERED
  (DEC-PRE-3 idempotency of repeated deletions) and is asserted to stay
  that way; no ordinary filter is added there.
- Isolation: per-test SQLite via monkeypatched ``dbmod.SessionLocal`` +
  ``Base.metadata.create_all``; ``TestClient`` WITHOUT a context manager
  => no lifespan => no ``run_startup`` / ``ensure_schema`` (whose arity
  defect is preexisting and out of scope).
- ``WorldRow(status=..., visibility=...)`` is never constructed (preexisting
  TypeError): world rows are inserted directly with mapped columns only.
- Preexisting blockers, NOT fixed here (declared, never hidden):
  * ``WorldRow.visibility`` is not mapped => ``GET /api/worlds`` cannot
    even build its select (AttributeError).  A1's primary evidence is the
    AST test (structural, labelled as such); the behavioral test exists
    but runs as a NON-STRICT xfail documenting the blockage.
  * GET of an ACTIVE world reaches ``row.visibility`` inside
    ``_require_visible`` (AttributeError).  Active accessibility is proven
    at helper level (``_load_row``) and end-to-end on the owner
    ``PUT /visibility`` path, which never reads ``visibility``.
"""

from __future__ import annotations

import ast
import itertools
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import worldsim.server.app as appmod
import worldsim.server.db as dbmod
import worldsim.server.runner as runnermod
from worldsim.server.db import WorldRow

APP_PATH = Path(appmod.__file__)
APP_SOURCE = APP_PATH.read_text(encoding="utf-8")

_seq = itertools.count(1)


# ---------------------------------------------------------------------------
# Fixture / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated app environment (test_server's _fresh_app pattern, copied:
    tests/test_server.py itself is never modified)."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'b33_app.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(dbmod, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(runnermod, "registry", runnermod.RunnerRegistry())
    monkeypatch.setattr(appmod, "registry", runnermod.registry)
    appmod.Base.metadata.create_all(engine)
    client = TestClient(appmod.app)  # sin `with`: sin lifespan (sin startup)
    return SimpleNamespace(client=client, engine=engine)


def _register_owner(client) -> dict:
    r = client.post(
        "/api/auth/register",
        json={"username": f"b33_{next(_seq)}", "password": "pass1234"},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _user_id(client, headers: dict) -> int:
    r = client.get("/api/auth/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["user_id"]


def _insert_world(*, owner_user_id, deleted: bool, months: int = 42) -> int:
    """Insert a world row directly (mapped columns only; never status/)."""
    with dbmod.SessionLocal() as s:
        row = WorldRow(
            seed=11,
            width=10,
            height=10,
            months_simulated=months,
            land_pct=50.0,
            state_json='{"month": 5}',
            created_at="2026-01-01 00:00:00",
            name="b33",
            mode="single",
            sea_level=0.2,
            owner_user_id=owner_user_id,
            deleted_at=datetime.now(timezone.utc) if deleted else None,
        )
        s.add(row)
        s.commit()
        return row.id


def _mark_deleted(world_id: int) -> str:
    """Establish the logical mark once (DEC-PRE-3) and return its raw value."""
    with dbmod.SessionLocal() as s:
        row = s.get(WorldRow, world_id)
        assert row.deleted_at is None, "mark already established"
        row.deleted_at = datetime.now(timezone.utc)
        s.commit()
    return _raw_deleted_at(world_id)


def _raw_deleted_at(world_id: int):
    with dbmod.SessionLocal() as s:
        return s.execute(
            text("SELECT deleted_at FROM worlds WHERE id = :i"), {"i": world_id}
        ).scalar()


# ---------------------------------------------------------------------------
# A2 — _load_row(): 404 for deleted worlds (HTTP, real requests)
# ---------------------------------------------------------------------------

def test_a2_http_get_deleted_world_is_404(env):
    world_id = _insert_world(owner_user_id=None, deleted=True)

    r = env.client.get(f"/api/worlds/{world_id}")

    assert r.status_code == 404
    assert r.json() == {"detail": f"world {world_id} not found"}


def test_a2_owner_http_get_deleted_is_identical_404(env):
    """DEC-PRE-4: the authenticated OWNER gets exactly the same 404 as an
    anonymous request — a deleted world is indistinguishable from one that
    never existed (real HTTP request with a real Bearer token)."""
    headers = _register_owner(env.client)
    uid = _user_id(env.client, headers)
    world_id = _insert_world(owner_user_id=uid, deleted=True)

    r_owner = env.client.get(f"/api/worlds/{world_id}", headers=headers)
    r_anon = env.client.get(f"/api/worlds/{world_id}")
    r_missing = env.client.get("/api/worlds/999999999", headers=headers)

    assert r_owner.status_code == 404
    assert r_anon.status_code == 404
    # byte-identical for owner and anonymous on the same id:
    assert r_owner.json() == r_anon.json()
    assert r_owner.json() == {"detail": f"world {world_id} not found"}
    # same contract as a world that never existed:
    assert r_missing.status_code == 404
    assert r_missing.json() == {"detail": "world 999999999 not found"}


# ---------------------------------------------------------------------------
# A3 — active worlds keep the existing access rules
# ---------------------------------------------------------------------------

def test_a3_active_world_keeps_existing_access_rules(env):
    headers = _register_owner(env.client)
    uid = _user_id(env.client, headers)
    world_id = _insert_world(owner_user_id=uid, deleted=False, months=42)

    # helper level: an active row is still loaded (no over-filtering)
    row = appmod._load_row(world_id)
    assert row is not None
    assert row.id == world_id
    assert row.months_simulated == 42

    # end-to-end on the owner PUT path (never reads row.visibility):
    r = env.client.put(
        f"/api/worlds/{world_id}/visibility",
        json={"visibility": "public"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"id": world_id, "visibility": "public"}

    # authorization matrix preserved: anonymous PUT on the active world
    # is rejected BEFORE any load (401).
    r2 = env.client.put(
        f"/api/worlds/{world_id}/visibility", json={"visibility": "public"}
    )
    assert r2.status_code == 401

    # sanity: the missing-world contract at helper level is unchanged.
    with pytest.raises(Exception) as exc:
        appmod._load_row(999999999)
    assert exc.value.status_code == 404
    assert exc.value.detail == "world 999999999 not found"


# ---------------------------------------------------------------------------
# A1 — GET /api/worlds excludes deleted worlds
# ---------------------------------------------------------------------------

def _fn(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _has_deleted_guard(fn: ast.AST) -> bool:
    """True iff the function contains WorldRow.deleted_at.is_(None)."""
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "is_" or len(node.args) != 1:
            continue
        arg = node.args[0]
        if not (isinstance(arg, ast.Constant) and arg.value is None):
            continue
        value = node.func.value
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "deleted_at"
            and isinstance(value.value, ast.Name)
            and value.value.id == "WorldRow"
        ):
            return True
    return False


def test_a1_list_worlds_filters_deleted_in_sql_ast():
    """A1 primary evidence — STRUCTURAL (AST), not SQL execution.

    The endpoint cannot run today: building its select touches
    ``WorldRow.visibility``, which is not mapped (preexisting defect,
    same root as the visibility-suite baseline).  This test proves the
    predicate is part of ``list_worlds``' query construction AND that the
    privacy / owner / pagination predicates were preserved.
    """
    tree = ast.parse(APP_SOURCE)
    fn = _fn(tree, "list_worlds")

    assert _has_deleted_guard(fn), (
        "list_worlds must exclude deleted worlds via WorldRow.deleted_at.is_(None)"
    )

    segment = ast.get_source_segment(APP_SOURCE, fn)
    # privacy predicates preserved (DEC20/DEC-F7B):
    assert 'WorldRow.visibility == "public"' in segment
    assert "WorldRow.owner_user_id == user.id" in segment
    # pagination preserved:
    assert ".limit(page_limit + 1)" in segment
    # no in-memory filtering introduced (filter is chained on the query):
    assert "deleted_at" in segment


@pytest.mark.xfail(
    reason="preexisting: WorldRow.visibility not mapped -> AttributeError al "
    "construir el select; el endpoint no es ejecutable hoy (no cuenta como "
    "validación ejecutada)",
    strict=False,
)
def test_a1_list_worlds_excludes_deleted_behavioral(env):
    """Behavioral requirement 4 — currently blocked by the preexisting
    defect (runs as non-strict xfail; see module docstring)."""
    headers = _register_owner(env.client)
    uid = _user_id(env.client, headers)
    active_id = _insert_world(owner_user_id=uid, deleted=False)
    deleted_id = _insert_world(owner_user_id=uid, deleted=True)

    result = appmod.list_worlds(
        limit=None, cursor=None, search=None, mode=None,
        mine="1", authorization=headers["Authorization"],
    )

    ids = [w["id"] for w in result["worlds"]]
    assert active_id in ids
    assert deleted_id not in ids


# ---------------------------------------------------------------------------
# A4 — put_world_visibility(): second read excludes deleted worlds
# ---------------------------------------------------------------------------

def test_a4_put_visibility_deleted_between_reads_404(env, monkeypatch):
    """TOCTOU window: the row is active at the FIRST read and logically
    deleted before the second read.  The filtered second read must stop
    the operation (404) and the mark must stay byte-identical."""
    headers = _register_owner(env.client)
    uid = _user_id(env.client, headers)
    world_id = _insert_world(owner_user_id=uid, deleted=False)

    stale_row = appmod._load_row(world_id)  # first read (world still active)
    mark_before = _mark_deleted(world_id)   # deleted BETWEEN both reads
    # simulate the window: the first gate already returned the live row.
    monkeypatch.setattr(appmod, "_load_row", lambda _id: stale_row)

    r = env.client.put(
        f"/api/worlds/{world_id}/visibility",
        json={"visibility": "public"},
        headers=headers,
    )

    assert r.status_code == 404
    assert r.json() == {"detail": f"world {world_id} not found"}
    # the operation never completed and the mark was never rewritten:
    assert _raw_deleted_at(world_id) == mark_before


def test_a4_put_visibility_already_deleted_is_404(env):
    """Unpatched path: owner with a valid session on an already-deleted
    world — auth passes, then the (filtered) load returns 404."""
    headers = _register_owner(env.client)
    uid = _user_id(env.client, headers)
    world_id = _insert_world(owner_user_id=uid, deleted=True)
    mark_before = _raw_deleted_at(world_id)

    r = env.client.put(
        f"/api/worlds/{world_id}/visibility",
        json={"visibility": "public"},
        headers=headers,
    )

    assert r.status_code == 404
    assert r.json() == {"detail": f"world {world_id} not found"}
    assert _raw_deleted_at(world_id) == mark_before


# ---------------------------------------------------------------------------
# A6 — _canonical_month(): a deleted row never supplies the fallback
# ---------------------------------------------------------------------------

def test_a6_canonical_month_never_uses_deleted_row(env):
    active_id = _insert_world(owner_user_id=None, deleted=False, months=42)
    deleted_id = _insert_world(owner_user_id=None, deleted=True, months=77)

    assert appmod._canonical_month(active_id) == 42   # active: previous behavior
    assert appmod._canonical_month(deleted_id) == 0   # deleted: like missing
    assert appmod._canonical_month(999999999) == 0    # missing: unchanged


# ---------------------------------------------------------------------------
# A5 — DELETE endpoint: idempotency preserved, read stays UNFILTERED
# ---------------------------------------------------------------------------

def test_a5_delete_endpoint_repeated_delete_is_idempotent(env):
    """DEC-PRE-3: repeating DELETE on an already-deleted row is an HTTP
    success, and the existing mark is never overwritten (A5 read intact)."""
    headers = _register_owner(env.client)
    uid = _user_id(env.client, headers)
    world_id = _insert_world(owner_user_id=uid, deleted=True)
    mark_before = _raw_deleted_at(world_id)

    r = env.client.delete(f"/api/worlds/{world_id}", headers=headers)

    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": world_id}
    # the mark keeps its exact original value (DEC-PRE-3):
    assert _raw_deleted_at(world_id) == mark_before


# ---------------------------------------------------------------------------
# Static checks (A1/A2/A4/A6 + A5 intact + app-wide guarantees)
# ---------------------------------------------------------------------------

def test_static_a2_a4_a6_filters_and_404_messages():
    tree = ast.parse(APP_SOURCE)
    for name in ("_load_row", "put_world_visibility", "_canonical_month"):
        fn = _fn(tree, name)
        assert _has_deleted_guard(fn), f"{name} must filter deleted worlds in SQL"

    load_src = ast.get_source_segment(APP_SOURCE, _fn(tree, "_load_row"))
    put_src = ast.get_source_segment(
        APP_SOURCE, _fn(tree, "put_world_visibility")
    )
    # contractual 404 messages preserved byte-for-byte:
    assert 'f"world {world_id} not found"' in load_src
    assert 'f"world {world_id} not found"' in put_src


def test_static_a5_delete_read_remains_unfiltered():
    """A5 is the authorized exception (DEC-PRE-3 idempotency): the DELETE
    endpoint keeps its direct unfiltered load and gains NO filter."""
    tree = ast.parse(APP_SOURCE)
    fn = _fn(tree, "delete_world")
    segment = ast.get_source_segment(APP_SOURCE, fn)

    assert "db_.get(WorldRow, world_id)" in segment, (
        "A5's unfiltered read must be preserved for repeated deletions"
    )
    assert "deleted_at" not in segment, (
        "no ordinary filter may be added to the DELETE endpoint"
    )


def test_static_app_filters_is_none_no_restoration_no_historical_params():
    """Rule 8 + rule 7 + B3-3 scope: four original ordinary filters plus
    the public-highlight eligibility guard; every ``deleted_at`` read is
    ``.is_(None)``, nothing ever writes it, and no general historical
    access parameter exists anywhere in app.py."""
    source = APP_SOURCE
    tree = ast.parse(source)

    # Four original filters (A1, A2, A4, A6) plus the public-highlight
    # eligibility guard: private/deleted worlds must never be highlighted.
    assert source.count("WorldRow.deleted_at.is_(None)") == 5
    assert source.count("deleted_at") == 5  # no other mention at all

    # no general historical-access mechanism:
    for banned in ("include_deleted", "allow_deleted"):
        assert banned not in source, f"app.py must not expose {banned!r}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            names = (
                [a.arg for a in args.posonlyargs]
                + [a.arg for a in args.args]
                + [a.arg for a in args.kwonlyargs]
            )
            assert "historical" not in names, f"{node.name}() must not accept 'historical'"
            assert "include_deleted" not in names
            assert "allow_deleted" not in names

    # every read of *.deleted_at is inside .is_(None); nothing writes it:
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "deleted_at"
    ]
    assert len(reads) == 5
    for node in reads:
        parent = parents.get(node)
        assert isinstance(parent, ast.Attribute) and parent.attr == "is_"
        grandparent = parents.get(parent)
        assert isinstance(grandparent, ast.Call) and grandparent.func is parent
        assert len(grandparent.args) == 1
        assert isinstance(grandparent.args[0], ast.Constant)
        assert grandparent.args[0].value is None

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        else:
            continue
        for target in targets:
            parts: list[str] = []
            cur = target
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if parts:
                assert parts[-1] != "deleted_at", "app.py must never write deleted_at"
