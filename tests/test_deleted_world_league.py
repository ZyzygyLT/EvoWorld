"""F7-E.2-I B3-4: logical-deletion filters at the three WorldRow reads of
``league.py`` (claim gate, toast metadata read, season transition read).

Scope / isolation notes:
- Only ``league.py`` production code is exercised here; ``app.py`` is used
  read-only through HTTP for the end-to-end 404 gate (never modified).
- Isolation: per-test SQLite via monkeypatched ``dbmod.SessionLocal``
  (``league._session()`` resolves ``db.SessionLocal`` at call time) +
  ``Base.metadata.create_all``; no lifespan, no ``ensure_schema``.
- ``WorldRow(status=..., visibility=...)`` is never constructed (preexisting
  TypeError): rows are inserted with mapped columns only.
- Preexisting blocker, NOT fixed here: ``WorldRow.visibility`` is not
  mapped, so the HTTP claim path on an ACTIVE world dies inside
  ``_visibility()`` (AttributeError).  Active-world behaviour is therefore
  proven at function level (real ``claim_kingdom`` / ``maybe_end_season``
  with real SQL); the HTTP tests cover the deleted/missing gate, which
  short-circuits (404) BEFORE that defect is reached.
- ``get_claims()`` / ``standings()`` read ``ClaimRow`` only — they are NOT
  among the three inventoried ``WorldRow`` points and are left untouched
  (their HTTP exposure is gated by B3-3's filtered ``_load_row``).
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

import worldsim.server.app as appmod
import worldsim.server.db as dbmod
import worldsim.server.runner as runnermod
from worldsim.server.db import Base, WorldRow

LEAGUE_PATH = Path(dbmod.__file__).parent / "league.py"
LEAGUE_SOURCE = LEAGUE_PATH.read_text(encoding="utf-8")

_seq = itertools.count(1)


# ---------------------------------------------------------------------------
# Fixture / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated DB bound to ``league._session()`` (and the app, for HTTP)."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'b34_league.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(dbmod, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(runnermod, "registry", runnermod.RunnerRegistry())
    monkeypatch.setattr(appmod, "registry", runnermod.registry)
    Base.metadata.create_all(engine)
    client = TestClient(appmod.app)  # sin `with`: sin lifespan (sin startup)
    return SimpleNamespace(client=client, engine=engine)


def _insert_world(
    *,
    mode: str = "league",
    deleted: bool = False,
    season_years: int = 100,
    season_over: int = 0,
    state_json: str = '{"kingdoms": [{"id": 0, "name": "Avalon"}]}',
) -> int:
    """Insert a world row directly (mapped columns only; never status/)."""
    from datetime import datetime, timezone

    with dbmod.SessionLocal() as s:
        row = WorldRow(
            seed=42,
            width=10,
            height=10,
            months_simulated=0,
            land_pct=50.0,
            state_json=state_json,
            created_at="2026-01-01 00:00:00",
            name="b34",
            mode=mode,
            sea_level=0.2,
            season_years=season_years,
            season_over=season_over,
            deleted_at=datetime.now(timezone.utc) if deleted else None,
        )
        s.add(row)
        s.commit()
        return row.id


def _raw(world_id: int, column: str):
    with dbmod.SessionLocal() as s:
        return s.execute(
            text(f"SELECT {column} FROM worlds WHERE id = :i"), {"i": world_id}
        ).scalar()


def _claim_count(world_id: int) -> int:
    with dbmod.SessionLocal() as s:
        return s.execute(
            text("SELECT COUNT(*) FROM claims WHERE world_id = :i"),
            {"i": world_id},
        ).scalar()


def _user() -> tuple[str, str]:
    i = next(_seq)
    return f"tok{i:04d}-b34", f"Oracle{i}"


def _eng(month: int) -> SimpleNamespace:
    """Minimal engine stand-in for maybe_end_season (only ``month`` is read)."""
    return SimpleNamespace(month=month)


class _Capture:
    """Collects every statement executed on the engine (SQL evidence)."""

    def __init__(self, engine):
        self.engine = engine
        self.statements: list[str] = []

    def _hook(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(statement)

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._hook)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._hook)

    def worlds_selects(self) -> list[str]:
        return [
            s for s in self.statements
            if "SELECT" in s and "FROM worlds" in s
        ]


# ---------------------------------------------------------------------------
# Point 1 — claim_kingdom() gate read
# ---------------------------------------------------------------------------

def test_p1_claim_on_deleted_world_returns_world_not_found(env):
    """Ordinary access: claiming on a logically deleted world is
    indistinguishable from a missing one ("world not found"), and NO claim
    row is ever inserted."""
    wid = _insert_world(deleted=True)
    token, name = _user()

    from worldsim.server.league import claim_kingdom

    result = claim_kingdom(wid, 0, token, name, month=5)

    assert result == "world not found"
    assert _claim_count(wid) == 0


def test_p1_claim_on_missing_world_same_contract(env):
    from worldsim.server.league import claim_kingdom

    token, name = _user()
    assert claim_kingdom(999999999, 0, token, name, month=5) == "world not found"


def test_p1_p2_claim_on_active_league_world_preserved(env):
    """Active worlds keep the preexisting behaviour end to end: the claim is
    persisted and the SECOND WorldRow read (toast metadata, point 2)
    delivers ``kingdom_name`` from ``state_json``."""
    from worldsim.server.league import claim_kingdom

    wid = _insert_world(deleted=False)
    token, name = _user()

    payload = claim_kingdom(wid, 0, token, name, month=5)

    assert isinstance(payload, dict), payload
    assert payload["kingdom_id"] == 0
    assert payload["points"] == 0
    assert payload["kingdom_name"] == "Avalon"  # point 2 read for ACTIVE row
    assert _claim_count(wid) == 1

    # duplicate rules preserved (one kingdom per user / per kingdom):
    assert claim_kingdom(wid, 1, token, name, month=6) == f"you already claimed 0"
    token2, name2 = _user()
    assert claim_kingdom(wid, 0, token2, name2, month=6) == f"already claimed by {name}"


def test_p1_p2_sql_of_both_world_reads_carries_predicate(env):
    """Execution evidence (req. 4): a successful claim executes exactly TWO
    ``SELECT ... FROM worlds`` statements (points 1 and 2) and BOTH contain
    ``deleted_at IS NULL`` in the SQL actually sent to SQLite."""
    from worldsim.server.league import claim_kingdom

    wid = _insert_world(deleted=False)
    token, name = _user()

    with _Capture(env.engine) as cap:
        claim_kingdom(wid, 0, token, name, month=5)

    selects = cap.worlds_selects()
    assert len(selects) == 2, f"expected the two WorldRow reads, got {len(selects)}"
    for stmt in selects:
        assert "deleted_at IS NULL" in stmt, stmt


# ---------------------------------------------------------------------------
# Point 3 — maybe_end_season() read (transition gate)
# ---------------------------------------------------------------------------

def test_p3_maybe_end_season_deleted_world_no_transition_no_write(env):
    """A logically deleted league world past its season limit is never
    frozen: no return message AND no write (``season_over`` and the
    established ``deleted_at`` mark stay byte-identical)."""
    from worldsim.server.league import maybe_end_season

    wid = _insert_world(deleted=True, season_years=1, season_over=0)
    mark_before = _raw(wid, "deleted_at")

    msg = maybe_end_season(wid, _eng(month=24))

    assert msg is None
    assert _raw(wid, "season_over") == 0        # no transition executed
    assert _raw(wid, "deleted_at") == mark_before  # mark never rewritten


def test_p3_maybe_end_season_missing_world_same_contract(env):
    from worldsim.server.league import maybe_end_season

    assert maybe_end_season(999999999, _eng(month=24)) is None


def test_p3_maybe_end_season_active_world_ends_and_second_call_noop(env):
    """Active worlds keep the preexisting semantics: freeze + champion
    message at the limit, then a no-op on the second call."""
    from worldsim.server.league import maybe_end_season

    wid = _insert_world(deleted=False, season_years=1, season_over=0)
    # a claim exists so the champion branch (empty board -> alternate text)
    # is not the one exercised here:
    with dbmod.SessionLocal() as s:
        from worldsim.server.db import ClaimRow

        s.add(ClaimRow(
            world_id=wid, kingdom_id=0, user_token="tok-champ",
            user_name="Champ", month=1, points=7,
        ))
        s.commit()

    msg = maybe_end_season(wid, _eng(month=24))  # 24 >= 12 months
    assert msg is not None
    assert "\U0001f3c6" in msg  # 🏆
    assert "Champ" in msg
    assert _raw(wid, "season_over") == 1

    # below the limit on another world: no end yet
    wid2 = _insert_world(deleted=False, season_years=100, season_over=0)
    assert maybe_end_season(wid2, _eng(month=24)) is None
    assert _raw(wid2, "season_over") == 0

    # already over -> no-op
    assert maybe_end_season(wid, _eng(month=24)) is None


def test_p3_sql_of_season_read_carries_predicate(env):
    """Execution evidence (req. 4): the SELECT executed by
    ``maybe_end_season`` contains ``deleted_at IS NULL``."""
    from worldsim.server.league import maybe_end_season

    wid = _insert_world(deleted=False, season_years=100)

    with _Capture(env.engine) as cap:
        maybe_end_season(wid, _eng(month=1))

    selects = cap.worlds_selects()
    assert len(selects) == 1, f"expected one WorldRow read, got {len(selects)}"
    assert "deleted_at IS NULL" in selects[0]


# ---------------------------------------------------------------------------
# Requirement 3 — mixed collection: only the active worlds are affected
# ---------------------------------------------------------------------------

def test_mixed_active_and_deleted_only_active_affected(env):
    """Active + deleted worlds in the same DB: claims and season
    transitions act exclusively on the active world; the deleted world's
    row and mark remain untouched."""
    from worldsim.server.league import claim_kingdom, maybe_end_season

    active = _insert_world(deleted=False, season_years=1, season_over=0)
    deleted = _insert_world(deleted=True, season_years=1, season_over=0)
    deleted_mark = _raw(deleted, "deleted_at")

    # claims: active succeeds, deleted rejected with the same contract
    token, name = _user()
    assert isinstance(
        claim_kingdom(active, 0, token, name, month=5), dict
    )
    token2, name2 = _user()
    assert claim_kingdom(deleted, 0, token2, name2, month=5) == "world not found"
    assert _claim_count(active) == 1
    assert _claim_count(deleted) == 0

    # season: active ends, deleted is skipped without any write
    assert maybe_end_season(active, _eng(month=24)) is not None
    assert maybe_end_season(deleted, _eng(month=24)) is None
    assert _raw(active, "season_over") == 1
    assert _raw(deleted, "season_over") == 0
    assert _raw(deleted, "deleted_at") == deleted_mark


# ---------------------------------------------------------------------------
# Requirement 5 — ordering / aggregation / classification intact for actives
# ---------------------------------------------------------------------------

def test_standings_order_and_classification_intact_for_active(env):
    """``standings`` (untouched code) still sorts points descending and
    assigns 1-based ranks over the ACTIVE claims."""
    from worldsim.server.league import standings

    wid = _insert_world(deleted=False)
    with dbmod.SessionLocal() as s:
        from worldsim.server.db import ClaimRow

        for token, name, pts in (
            ("t1", "Low", 10),
            ("t2", "High", 50),
            ("t3", "Mid", 30),
        ):
            s.add(ClaimRow(
                world_id=wid, kingdom_id=0, user_token=token,
                user_name=name, month=1, points=pts,
            ))
        s.commit()

    board = standings(wid)

    assert [u["user"] for u in board] == ["High", "Mid", "Low"]
    assert [u["rank"] for u in board] == [1, 2, 3]
    assert [u["points"] for u in board] == [50, 30, 10]


def test_scoring_helpers_untouched_for_active_data(env):
    """``score_new_events`` over an active world with no new events returns
    [] (no claims mutated) — the scoring pipeline still runs for actives."""
    from worldsim.server.league import score_new_events

    wid = _insert_world(deleted=False)
    eng = SimpleNamespace(
        chronicle=SimpleNamespace(events=[]),
        wars=[],
    )
    assert score_new_events(wid, eng, since_month=0) == []


# ---------------------------------------------------------------------------
# End-to-end HTTP gate (deleted world indistinguishable from missing)
# ---------------------------------------------------------------------------

def test_http_claims_on_deleted_world_are_404_like_missing(env):
    """Real HTTP requests: POST/GET claims on a deleted world return the
    same 404 contract as a world that never existed (the B3-3 filtered
    ``_load_row`` gate fires BEFORE the preexisting ``_visibility``
    AttributeError, so this path is executable)."""
    wid = _insert_world(deleted=True)
    token, name = _user()

    r_post = env.client.post(
        f"/api/worlds/{wid}/claims",
        json={"kingdom_id": 0, "user_token": token, "user_name": name},
    )
    r_post_missing = env.client.post(
        "/api/worlds/999999999/claims",
        json={"kingdom_id": 0, "user_token": token, "user_name": name},
    )
    r_get = env.client.get(f"/api/worlds/{wid}/claims")
    r_get_missing = env.client.get("/api/worlds/999999999/claims")

    assert r_post.status_code == 404
    assert r_post.json() == {"detail": f"world {wid} not found"}
    assert r_post_missing.status_code == 404
    assert r_get.status_code == 404
    assert r_get.json() == {"detail": f"world {wid} not found"}
    assert r_get_missing.status_code == 404
    # no claim was ever recorded for the deleted world:
    assert _claim_count(wid) == 0


# ---------------------------------------------------------------------------
# Static checks — exactly three WorldRow reads, all filtered, no escape hatch
# ---------------------------------------------------------------------------

def test_static_three_world_reads_all_filtered_in_sql():
    """AST (complementary evidence, NOT a behavioural substitute): league.py
    contains exactly three ``select(WorldRow)`` queries and each one's
    WHERE carries ``WorldRow.deleted_at.is_(None)``; no ``include_deleted``
    / ``historical`` parameter exists and ``deleted_at`` is never written."""
    tree = ast.parse(LEAGUE_SOURCE)

    selects = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "select"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "WorldRow"
    ]
    assert len(selects) == 3, f"expected 3 WorldRow queries, got {len(selects)}"

    for sel in selects:
        # walk up to the enclosing .where(...) call
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        where_call = None
        cur = sel
        while cur in parents:
            cur = parents[cur]
            if (
                isinstance(cur, ast.Call)
                and isinstance(cur.func, ast.Attribute)
                and cur.func.attr == "where"
            ):
                where_call = cur
                break
        assert where_call is not None, "each select(WorldRow) must be .where()-filtered"
        segment = ast.unparse(where_call)
        assert "WorldRow.deleted_at.is_(None)" in segment, segment

    # no escape hatches anywhere in league.py:
    for banned in ("include_deleted", "allow_deleted"):
        assert banned not in LEAGUE_SOURCE
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
    # never written (no restoration):
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
                assert parts[-1] != "deleted_at", "league.py must never write deleted_at"
