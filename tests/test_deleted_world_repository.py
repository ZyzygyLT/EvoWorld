"""F7-E.2-I B3-1: logical-deletion filters in the repository layer (R1-R5).

Every test builds its own SQLite schema via ``Base.metadata.create_all``:
``ensure_schema()`` is never imported/called (preexisting signature defect)
and ``WorldRow(status=..., visibility=...)`` is never constructed (preexisting
TypeError: those columns are not mapped on the ORM model).

R2 limitation (documented, NOT presented as SQL execution): the query text
``WorldRow.status == status`` cannot be built at runtime because ``WorldRow``
has no mapped ``status`` column (preexisting defect, out of scope for B3-1).
R2 is therefore verified structurally (AST) only: the function must keep its
status predicate AND add the ``WorldRow.deleted_at.is_(None)`` exclusion
inside its ``.where(...)``.  This is a structural check of the exclusion
predicate, never a proof that SQL ran.
"""

from __future__ import annotations

import ast
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from worldsim.server.db import Base, WorldCheckpoint, WorldRow
from worldsim.server.repository import CheckpointRepository, WorldRepository

REPO_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "worldsim"
    / "server"
    / "repository.py"
)

# Columns read back verbatim from SQLite to prove a deleted row was not
# modified by any write operation (raw SELECT -> no ORM identity-map effects).
_SNAP_SQL = text(
    "SELECT id, seed, width, height, months_simulated, land_pct, state_json,"
    " created_at, name, mode, season_years, season_over, enabled_races_json,"
    " owner_user_id, sea_level, deleted_at, last_checkpoint_at, checkpoint_count"
    " FROM worlds WHERE id = :i"
)

_MONTHS_SIMULATED = 4  # index of months_simulated in _SNAP_SQL


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def repo_session_factory(tmp_path):
    """Isolated SQLite with the real migrated worlds schema (see module doc).

    ``ALTER TABLE`` mirrors the columns that exist in the real ``worlds.db``
    (and are added by ``ensure_schema()`` migrations) but are not mapped on
    ``WorldRow``: ``save_checkpoint()``'s raw SQL UPDATE needs
    ``last_checkpoint_at``/``checkpoint_count`` to exist.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'b31_repository.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _make_world(session, *, name: str = "w", deleted: bool) -> int:
    """Insert a world row.  Never passes unmapped kwargs (status/visibility)."""
    row = WorldRow(
        seed=7,
        width=10,
        height=10,
        months_simulated=5,
        land_pct=50.0,
        state_json="{}",
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name=name,
        mode="single",
        sea_level=0.2,
        deleted_at=datetime.now(timezone.utc) if deleted else None,
    )
    session.add(row)
    session.commit()
    return row.id


def _snapshot(session, world_id: int) -> tuple:
    """Raw-SQL snapshot of every worlds column used by these tests."""
    return tuple(session.execute(_SNAP_SQL, {"i": world_id}).one())


def _checkpoint_count(session, world_id: int) -> int:
    return session.execute(
        text("SELECT COUNT(*) FROM world_checkpoints WHERE world_id = :i"),
        {"i": world_id},
    ).scalar()


def _attr_chain(node) -> list[str] | None:
    """Dotted chain (['WorldRow', 'deleted_at', 'is_']) or None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return list(reversed(parts))
    return None


def _fn(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _is_is_none_on_deleted_at(call: ast.Call) -> bool:
    """True for ``WorldRow.deleted_at.is_(None)`` exactly."""
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "is_"
        and _attr_chain(func) == ["WorldRow", "deleted_at", "is_"]
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value is None
    )


# ---------------------------------------------------------------------------
# R1 - get_world()
# ---------------------------------------------------------------------------

def test_r1_get_world_returns_alive_world(repo_session_factory):
    s = repo_session_factory()
    world_id = _make_world(s, name="alive", deleted=False)

    row = WorldRepository(s).get_world(world_id)

    assert row is not None
    assert row.id == world_id
    assert row.name == "alive"


def test_r1_get_world_excludes_deleted_world(repo_session_factory):
    s = repo_session_factory()
    world_id = _make_world(s, name="gone", deleted=True)

    assert WorldRepository(s).get_world(world_id) is None


def test_r1_get_world_missing_and_deleted_are_indistinguishable(repo_session_factory):
    """DEC-PRE-4: deleted behaves exactly like never-existed (both None)."""
    s = repo_session_factory()
    world_id = _make_world(s, deleted=True)

    assert WorldRepository(s).get_world(world_id) is None
    assert WorldRepository(s).get_world(999_999_999) is None


# ---------------------------------------------------------------------------
# R2 - get_worlds_by_status()  (AST-only; see module docstring limitation)
# ---------------------------------------------------------------------------

def test_r2_get_worlds_by_status_excludes_deleted_worlds_ast():
    """Structural verification of R2's exclusion predicate (NOT SQL execution).

    Runtime execution is impossible: ``WorldRow.status`` is not a mapped
    column (preexisting defect, out of scope).  This test proves the source
    keeps the status predicate AND adds ``WorldRow.deleted_at.is_(None)``
    inside the same ``.where(...)`` call.  It does NOT prove SQL ran.
    """
    tree = ast.parse(REPO_PATH.read_text(encoding="utf-8"))
    fn = _fn(tree, "get_worlds_by_status")

    where_calls = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "where"
    ]
    assert where_calls, "get_worlds_by_status must filter with .where(...)"

    guarded = False
    status_predicate = False
    for where_call in where_calls:
        for node in ast.walk(where_call):
            if isinstance(node, ast.Call) and _is_is_none_on_deleted_at(node):
                guarded = True
            if (
                isinstance(node, ast.Compare)
                and _attr_chain(node.left) == ["WorldRow", "status"]
                and len(node.ops) == 1
                and isinstance(node.ops[0], ast.Eq)
            ):
                status_predicate = True

    assert guarded, "R2 must exclude deleted worlds via WorldRow.deleted_at.is_(None)"
    assert status_predicate, "R2 must keep its original status predicate"


# ---------------------------------------------------------------------------
# R3 - update_status()
# ---------------------------------------------------------------------------

def test_r3_update_status_deleted_world_returns_false_and_row_unchanged(
    repo_session_factory,
):
    s = repo_session_factory()
    world_id = _make_world(s, deleted=True)
    before = _snapshot(s, world_id)

    result = WorldRepository(s).update_status(world_id, "archived")
    s.commit()

    assert result is False
    assert _snapshot(s, world_id) == before


def test_r3_update_status_alive_world_keeps_previous_behavior(repo_session_factory):
    s = repo_session_factory()
    world_id = _make_world(s, deleted=False)

    result = WorldRepository(s).update_status(world_id, "archived")
    s.commit()

    assert result is True


# ---------------------------------------------------------------------------
# R4 - update_checkpoint_metadata()
# ---------------------------------------------------------------------------

def test_r4_update_metadata_deleted_world_returns_false_and_row_unchanged(
    repo_session_factory,
):
    s = repo_session_factory()
    world_id = _make_world(s, deleted=True)
    before = _snapshot(s, world_id)

    result = WorldRepository(s).update_checkpoint_metadata(
        world_id, months_simulated=999
    )
    s.commit()

    assert result is False
    assert _snapshot(s, world_id) == before


def test_r4_update_metadata_alive_world_keeps_previous_behavior(repo_session_factory):
    s = repo_session_factory()
    world_id = _make_world(s, deleted=False)

    result = WorldRepository(s).update_checkpoint_metadata(
        world_id, months_simulated=777
    )
    s.commit()

    assert result is True
    assert _snapshot(s, world_id)[_MONTHS_SIMULATED] == 777


# ---------------------------------------------------------------------------
# R5 - save_checkpoint()
# ---------------------------------------------------------------------------

def test_r5_save_checkpoint_deleted_world_raises_contractual_valueerror(
    repo_session_factory,
):
    s = repo_session_factory()
    world_id = _make_world(s, deleted=True)
    before = _snapshot(s, world_id)

    repo = CheckpointRepository(s)
    with pytest.raises(ValueError) as excinfo:
        repo.save_checkpoint(
            world_id,
            sequence=1,
            month=10,
            checkpoint_type="manual",
            payload=b"{}",
            checksum="deadbeef",
        )
    s.commit()

    # Contractual message preserved byte-for-byte (indistinguishable).
    assert str(excinfo.value) == f"World {world_id} does not exist"
    assert _checkpoint_count(s, world_id) == 0
    assert _snapshot(s, world_id) == before


def test_r5_save_checkpoint_alive_world_keeps_previous_behavior(repo_session_factory):
    s = repo_session_factory()
    world_id = _make_world(s, deleted=False)

    checkpoint_id = CheckpointRepository(s).save_checkpoint(
        world_id,
        sequence=1,
        month=10,
        checkpoint_type="manual",
        payload=b"{}",
        checksum="deadbeef",
    )
    s.commit()

    assert isinstance(checkpoint_id, int)
    checkpoint = s.get(WorldCheckpoint, checkpoint_id)
    assert checkpoint is not None
    assert checkpoint.world_id == world_id
    assert checkpoint.month == 10
    assert _checkpoint_count(s, world_id) == 1

    snap = _snapshot(s, world_id)
    assert snap[_MONTHS_SIMULATED] == 10  # raw UPDATE sets months_simulated
    assert snap[16] is not None  # last_checkpoint_at
    assert snap[17] == 1  # checkpoint_count


# ---------------------------------------------------------------------------
# Repository-wide static guarantees (rules of B3-1)
# ---------------------------------------------------------------------------

def test_repository_has_no_historical_access_parameters():
    """``get_world()`` and the whole repository stay strictly ordinary:
    no ``include_deleted``/``allow_deleted``/``historical`` parameter or
    string anywhere (CS1/CS2 exceptions live outside this layer)."""
    source = REPO_PATH.read_text(encoding="utf-8")
    for banned in ("include_deleted", "allow_deleted", "historical"):
        assert banned not in source, f"repository must not expose {banned!r}"

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            names = (
                [a.arg for a in args.posonlyargs]
                + [a.arg for a in args.args]
                + [a.arg for a in args.kwonlyargs]
            )
            for banned in ("include_deleted", "allow_deleted", "historical"):
                assert banned not in names, (
                    f"{node.name}() must not accept {banned!r}"
                )


def test_repository_deleted_at_reads_are_is_none_and_never_assigned():
    """Rule 8 + rule 7: every ``deleted_at`` read in repository.py is
    ``.is_(None)`` (no timestamp comparisons) and nothing ever writes
    ``deleted_at`` (no restoration path in this layer)."""
    source = REPO_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }

    # 1) No assignment to any `*.deleted_at` (restoration is forbidden).
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        else:
            continue
        for target in targets:
            chain = _attr_chain(target)
            if chain is not None:
                assert chain[-1] != "deleted_at", (
                    "repository must never write deleted_at (no restoration)"
                )

    # 2) Every read of `WorldRow.deleted_at` is inside `.is_(None)`.
    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "deleted_at"
    ]
    assert reads, "expected the five R1-R5 filters to read deleted_at"
    for node in reads:
        parent = parents.get(node)
        assert (
            isinstance(parent, ast.Attribute) and parent.attr == "is_"
        ), "deleted_at must only be read through .is_(...)"
        grandparent = parents.get(parent)
        assert isinstance(grandparent, ast.Call) and grandparent.func is parent, (
            "deleted_at.is_(...) must be called"
        )
        assert len(grandparent.args) == 1
        assert isinstance(grandparent.args[0], ast.Constant)
        assert grandparent.args[0].value is None, "only .is_(None) is allowed"
