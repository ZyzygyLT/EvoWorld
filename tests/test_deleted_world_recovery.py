"""F7-E.2-I B3-2: startup exclusion of logically deleted worlds (DEC-PRE-2).

Isolation notes (no shared fixtures, no global schema changes):
- Schema: ``Base.metadata.create_all`` on a per-test SQLite file.  The
  ``ensure_schema()`` function is NEVER called (preexisting arity defect:
  ``run_startup`` calls ``db.ensure_schema(engine)`` against a zero-arg
  signature, which is why the FULL startup flow cannot execute — see the
  report's limitations section).
- ``WorldRow(status=..., visibility=...)`` is never constructed (preexisting
  TypeError: not mapped on the ORM model).
- The complete production flow (``run_startup`` -> Etapa A + B) is therefore
  not runnable; these tests exercise the REAL ``_list_world_rows()`` (real
  SQL) and the REAL ``RecoveryCoordinator._stage_a()`` loop, stubbing only
  the collaborators blocked by preexisting defects or used as observation
  points (eligibility engine, registry, runner factory).  Structural checks
  are labelled as such and are never presented as full-flow execution.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from worldsim.server.db import Base, WorldRow
from worldsim.server.recovery_coordinator import RecoveryCoordinator
from worldsim.server.recovery_manager import RecoveryManager, RecoveryResult

REPO_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "worldsim"
    / "server"
    / "recovery_coordinator.py"
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path):
    """Isolated engine + session factory (own SQLite file per test)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'b32_recovery.db'}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _make_world(session, *, deleted: bool) -> int:
    """Insert a world row.  Never passes unmapped kwargs (status/visibility)."""
    row = WorldRow(
        seed=7,
        width=10,
        height=10,
        months_simulated=5,
        land_pct=50.0,
        state_json="{}",
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        name="w",
        mode="single",
        sea_level=0.2,
        deleted_at=datetime.now(timezone.utc) if deleted else None,
    )
    session.add(row)
    session.commit()
    return row.id


class _StubPersistence:
    """_stage_a never touches persistence; placeholder for __init__."""


class _RecordingRegistry:
    """Observation double: records register_existing() attempts (no isinstance)."""

    def __init__(self) -> None:
        self.registered: list[int] = []

    def register_existing(self, world_id, runner):
        self.registered.append(world_id)
        return runner


class _RecordingRunnerFactory:
    """Observation double for the runner construction point."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(
        self, world_id, seed, width, height, races, sea_level, *, engine, operation_service
    ):
        self.calls.append(world_id)
        return SimpleNamespace(world_id=world_id, paused=False)


class _StubRecoveryManager:
    """Eligibility double (the REAL RecoveryManager reads ``world_row.status``,
    a preexisting unmapped attribute, so the real one cannot process an
    active row — see report limitations).  Records every recover() call."""

    def __init__(self, ok_world_ids) -> None:
        self.ok_world_ids = set(ok_world_ids)
        self.recover_calls: list[int] = []

    def recover(self, world_id: int) -> RecoveryResult:
        self.recover_calls.append(world_id)
        if world_id in self.ok_world_ids:
            return RecoveryResult(
                status="ok",
                world_id=world_id,
                engine=object(),
                world_status="running",
                checkpoint_sequence=1,
                checkpoint_month=10,
            )
        return RecoveryResult(
            status="not_recoverable",
            world_id=world_id,
            diagnostics="stub: not recoverable",
        )


class _SpyRecoveryManager(RecoveryManager):
    """Real RecoveryManager with call recording (subclass only in tests)."""

    def __init__(self, session_factory) -> None:
        super().__init__(session_factory)
        self.recover_calls: list[int] = []

    def recover(self, world_id: int) -> RecoveryResult:
        self.recover_calls.append(world_id)
        return super().recover(world_id)


def _make_coordinator(*, session_factory, recovery_manager, registry, runner_factory):
    return RecoveryCoordinator(
        persistence=_StubPersistence(),
        recovery_manager=recovery_manager,
        registry=registry,
        session_factory=session_factory,
        runner_factory=runner_factory,
    )


def _list_rows_via(session_factory):
    """Call the REAL _list_world_rows() on a minimal real coordinator."""
    coordinator = _make_coordinator(
        session_factory=session_factory,
        recovery_manager=_StubRecoveryManager(ok_world_ids=()),
        registry=_RecordingRegistry(),
        runner_factory=_RecordingRunnerFactory(),
    )
    return coordinator._list_world_rows()


# ---------------------------------------------------------------------------
# 1. The exclusion predicate is part of the executed SQL
# ---------------------------------------------------------------------------

def test_list_world_rows_sql_contains_deleted_at_is_null(env):
    """Evidence that the filter runs INSIDE SQL: capture the statement that
    _list_world_rows() actually sends to SQLite and assert it contains
    ``deleted_at IS NULL`` (an in-memory filter would not appear here)."""
    engine, session_factory = env
    s = session_factory()
    _make_world(s, deleted=True)
    active_id = _make_world(s, deleted=False)

    captured: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if "FROM worlds" in statement:
            captured.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        rows = _list_rows_via(session_factory)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert captured, "expected _list_world_rows() to execute a SELECT on worlds"
    assert any("deleted_at IS NULL" in stmt for stmt in captured), (
        "the executed SQL must contain the deleted_at IS NULL predicate"
    )
    # Result is consistent with the SQL predicate: only the active row.
    assert [row.id for row in rows] == [active_id]


# ---------------------------------------------------------------------------
# 2. An active row appears in the results
# ---------------------------------------------------------------------------

def test_list_world_rows_returns_active_row(env):
    _engine, session_factory = env
    s = session_factory()
    world_id = _make_world(s, deleted=False)

    rows = _list_rows_via(session_factory)

    assert [row.id for row in rows] == [world_id]
    assert rows[0].deleted_at is None


# ---------------------------------------------------------------------------
# 3. A deleted row is excluded
# ---------------------------------------------------------------------------

def test_list_world_rows_excludes_deleted_row(env):
    _engine, session_factory = env
    s = session_factory()
    _make_world(s, deleted=True)

    assert _list_rows_via(session_factory) == []


# ---------------------------------------------------------------------------
# 4. Mixed collection -> only the active rows, order preserved
# ---------------------------------------------------------------------------

def test_list_world_rows_mixed_returns_only_active_in_order(env):
    _engine, session_factory = env
    s = session_factory()
    active_a = _make_world(s, deleted=False)
    _make_world(s, deleted=True)
    active_b = _make_world(s, deleted=False)

    rows = _list_rows_via(session_factory)

    assert [row.id for row in rows] == [active_a, active_b]  # ascending id order
    assert all(row.deleted_at is None for row in rows)


# ---------------------------------------------------------------------------
# 5. Recovery never attempts to recover/register runners for deleted rows
# ---------------------------------------------------------------------------

def test_stage_a_does_not_recover_or_register_deleted_rows(env):
    """Real RecoveryCoordinator._stage_a() + real _list_world_rows() (real
    SQL) over an active+deleted collection.  Only the observation/eligibility
    collaborators are stubbed (documented in the module docstring).

    Consumer-point verification: recover() is attempted ONLY for the active
    id and register_existing() is attempted ONLY for the active id; the
    deleted id never reaches either point.
    """
    _engine, session_factory = env
    s = session_factory()
    active_id = _make_world(s, deleted=False)
    deleted_id = _make_world(s, deleted=True)

    manager = _StubRecoveryManager(ok_world_ids={active_id})
    registry = _RecordingRegistry()
    runner_factory = _RecordingRunnerFactory()
    coordinator = _make_coordinator(
        session_factory=session_factory,
        recovery_manager=manager,
        registry=registry,
        runner_factory=runner_factory,
    )

    coordinator._stage_a()

    # Eligibility attempted only for the active world.
    assert manager.recover_calls == [active_id]
    assert deleted_id not in manager.recover_calls
    # Runner construction + registration attempted only for the active world.
    assert runner_factory.calls == [active_id]
    assert registry.registered == [active_id]
    # Bookkeeping contains only the active world.
    assert set(coordinator.recovery_results) == {active_id}
    assert set(coordinator._attempt_runners) == {active_id}
    assert deleted_id not in coordinator.recovery_results
    assert deleted_id not in coordinator._attempt_runners


def test_stage_a_with_real_recovery_manager_makes_zero_attempts_on_deleted(env):
    """Real RecoveryManager (spy subclass, test-only) + deleted-only DB:
    the real recovery flow completes with ZERO recover() attempts and no
    runner registration, because enumeration never yields the deleted row.

    Plus the documented double barrier (RM1): even a direct real
    recover(deleted_id) call returns not_recoverable (B3-1 filter) without
    touching the row or registering anything.
    """
    _engine, session_factory = env
    s = session_factory()
    deleted_id = _make_world(s, deleted=True)

    manager = _SpyRecoveryManager(session_factory)
    registry = _RecordingRegistry()
    coordinator = _make_coordinator(
        session_factory=session_factory,
        recovery_manager=manager,
        registry=registry,
        runner_factory=_RecordingRunnerFactory(),
    )

    coordinator._stage_a()  # real Etapa A body over a real (empty) enumeration

    assert manager.recover_calls == []
    assert coordinator.recovery_results == {}
    assert coordinator.not_recoverable == {}
    assert registry.registered == []
    assert coordinator._attempt_runners == {}

    # Double barrier: direct real-manager read of the deleted world.
    result = manager.recover(deleted_id)
    assert result.status == "not_recoverable"
    assert result.diagnostics == f"World {deleted_id} does not exist"
    assert registry.registered == []
    assert coordinator.recovery_results == {}


# ---------------------------------------------------------------------------
# The production filter lives in _list_world_rows() only (structural note)
# ---------------------------------------------------------------------------

def test_startup_filter_is_in_list_world_rows_query():
    """Structural corroboration (NOT flow execution): the only startup
    enumeration point must carry the SQL predicate inside its query."""
    import ast

    source = REPO_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_list_world_rows"
    )

    # The .is_(None) call must be rooted at WorldRow.deleted_at.
    guarded = False
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "is_"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value is None
        ):
            value = node.func.value
            guarded = (
                isinstance(value, ast.Attribute)
                and value.attr == "deleted_at"
                and isinstance(value.value, ast.Name)
                and value.value.id == "WorldRow"
            )
    assert guarded, (
        "_list_world_rows() must filter with WorldRow.deleted_at.is_(None) in SQL"
    )
