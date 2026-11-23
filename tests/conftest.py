"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from worldsim.data.loaders import load_governments, load_races
from worldsim.sim.engine import SimEngine
from worldsim.worldgen import WorldConfig
from worldsim.worldgen.pipeline import generate_world

DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return DATA


@pytest.fixture(scope="session")
def races(data_dir):
    return load_races(data_dir / "Races.txt")


@pytest.fixture(scope="session")
def governments(data_dir):
    return load_governments(data_dir / "CivilizedGovernment.txt")


@pytest.fixture(scope="session")
def small_world():
    return generate_world(WorldConfig(seed=42, width=100, height=50, rivers=4))


@pytest.fixture()
def engine(small_world, races, governments):
    return SimEngine(small_world, races, governments, seed=42)


# ---------------------------------------------------------------------------
# F7-C fixtures (Historias Compartidas, CONTRACTS §16) — ADDITIVE.
#
# Every fixture binds to an ISOLATED temp SQLite file created per test; the
# persistent ``worlds.db`` is never opened by this suite.  Nothing here
# modifies existing fixtures.
# ---------------------------------------------------------------------------


class F7CEnv:
    """Isolated SQLite DB + ``OperationService`` + durable-row helpers.

    The database goes through the REAL ``ensure_schema`` migration path, so
    the F7-C tables, constraints and the ``next_operation_sequence``
    allocator are exactly what the server uses.
    """

    def __init__(self, db_path: Path) -> None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        import worldsim.server.db as dbmod
        from worldsim.server.operation_service import OperationService

        self.dbmod = dbmod
        self.engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
        )
        dbmod.ensure_schema(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.service = OperationService(self.Session, engine_version="1.0")

    # -- principals and worlds ------------------------------------------

    def create_user(self, username: str = "admin", is_admin: bool = False) -> int:
        from worldsim.server.db import UserRow

        with self.Session() as s:
            u = UserRow(
                username=username,
                pw_hash="x",
                created_at="2026-01-01 00:00:00",
                is_admin=is_admin,
            )
            s.add(u)
            s.commit()
            return int(u.id)

    def create_world(
        self,
        *,
        visibility: str = "public",
        owner_user_id: int | None = None,
        name: str = "w",
    ) -> int:
        from worldsim.server.db import WorldRow

        with self.Session() as s:
            w = WorldRow(
                seed=1,
                width=64,
                height=32,
                months_simulated=0,
                land_pct=50.0,
                state_json="{}",
                created_at="2026-01-01 00:00:00",
                name=name,
                visibility=visibility,
                owner_user_id=owner_user_id,
            )
            s.add(w)
            s.commit()
            return int(w.id)

    # -- durable row helpers --------------------------------------------

    def op_rows(self, world_id: int) -> list:
        from worldsim.server.db import WorldOperation

        with self.Session() as s:
            return list(
                s.query(WorldOperation)
                .filter_by(world_id=world_id)
                .order_by(WorldOperation.operation_sequence)
                .all()
            )

    def op_row(self, world_id: int, operation_id: str):
        from worldsim.server.db import WorldOperation

        with self.Session() as s:
            return (
                s.query(WorldOperation)
                .filter_by(world_id=world_id, operation_id=operation_id)
                .one()
            )

    def event_rows(self, world_id: int) -> list:
        from worldsim.server.db import WorldEvent

        with self.Session() as s:
            return list(
                s.query(WorldEvent)
                .filter_by(world_id=world_id)
                .order_by(
                    WorldEvent.operation_sequence, WorldEvent.position
                )
                .all()
            )

    def cp_rows(self, world_id: int) -> list:
        from worldsim.server.db import WorldCheckpoint

        with self.Session() as s:
            return list(
                s.query(WorldCheckpoint)
                .filter_by(world_id=world_id)
                .order_by(WorldCheckpoint.sequence)
                .all()
            )

    def set_op(
        self,
        world_id: int,
        operation_id: str,
        status: str,
        continuity_state: str,
        **extra,
    ) -> None:
        """Force an operation into a contract-defined state (test setup for
        states reached through paths outside the current stage, e.g.
        ``failed``).  Never used to fabricate history the protocol itself
        produces."""
        from worldsim.server.db import WorldOperation

        with self.Session() as s:
            op = (
                s.query(WorldOperation)
                .filter_by(world_id=world_id, operation_id=operation_id)
                .one()
            )
            op.status = status
            op.continuity_state = continuity_state
            for k, v in extra.items():
                setattr(op, k, v)
            s.commit()

    def seed_checkpoint(self, world_id: int, month: int = 0, sequence: int | None = None) -> int:
        """Insert a valid ``WorldCheckpoint`` row (payload ``b"{}"``)."""
        import hashlib

        from worldsim.server.db import WorldCheckpoint

        with self.Session() as s:
            if sequence is None:
                seq = (
                    s.query(WorldCheckpoint)
                    .filter_by(world_id=world_id)
                    .count()
                    + 1
                )
            else:
                seq = sequence
            cp = WorldCheckpoint(
                world_id=world_id,
                sequence=seq,
                month=month,
                checkpoint_type="periodic",
                payload=b"{}",
                checksum=hashlib.sha256(b"{}").hexdigest(),
                created_at="2026-01-01 00:00:00",
                is_valid=1,
                checkpoint_version=2,
            )
            s.add(cp)
            s.commit()
            return int(cp.id)

    def seed_op(
        self,
        world_id: int,
        sequence: int,
        status: str = "confirmed",
        continuity_state: str = "active",
        *,
        user_id: int = 0,
        operation_id: str | None = None,
        with_checkpoint: bool = True,
        request_hash: str | None = None,
        **extra,
    ) -> str:
        """Insert an operation row directly (seed/history setup).

        Returns the ``operation_id``.  For ``confirmed`` operations a
        checkpoint is seeded too, so coverage sees the atomic T2 evidence.
        """
        import uuid
        import hashlib

        from sqlalchemy import text

        from worldsim.server.db import WorldOperation

        op_id = operation_id or str(uuid.uuid4())
        if request_hash is None:
            request_hash = hashlib.sha256(
                f"f7c-seed:{world_id}:{sequence}:{op_id}".encode("utf-8")
            ).hexdigest()
        with self.Session() as s:
            cp_id = None
            if with_checkpoint and status == "confirmed":
                cp_id = self.seed_checkpoint(world_id, month=sequence - 1)
            op = WorldOperation(
                world_id=world_id,
                operation_id=op_id,
                operation_sequence=sequence,
                status=status,
                continuity_state=continuity_state,
                created_at="2026-01-01 00:00:00",
                requesting_user_id=user_id,
                client_idempotency_key=f"seed-{sequence}-{op_id[:8]}",
                request_hash=request_hash,
                engine_version="1.0",
                checkpoint_id=cp_id,
                recovery_generation=0,
                **extra,
            )
            s.add(op)
            # Keep the durable allocator consistent with the seeded row
            # (T1 invariant: counter == MAX(sequence)+1 after every insert).
            s.execute(
                text(
                    "UPDATE worlds SET next_operation_sequence = :v "
                    "WHERE id = :w AND next_operation_sequence < :v"
                ),
                {"v": sequence + 1, "w": world_id},
            )
            s.commit()
        return op_id

    def seed_events(
        self,
        world_id: int,
        operation_id: str,
        sequence: int,
        count: int,
        *,
        month: int = 0,
        event_type: str = "BATTLE",
        kingdom_ids: list | None = None,
        msg_key: str = "battle",
    ) -> None:
        """Insert ``count`` events for an operation.

        Positions APPEND after whatever already exists for that operation
        (the unique constraint is ``(world, operation, sequence, position)``
        and positions are contiguous 0-based inside each operation), so the
        helper may be called several times for the same operation.  Month
        values run ``month + position``.
        """
        import json

        from worldsim.server.db import WorldEvent

        kids = kingdom_ids if kingdom_ids is not None else []
        with self.Session() as s:
            base = (
                s.query(WorldEvent)
                .filter_by(
                    world_id=world_id,
                    operation_id=operation_id,
                    operation_sequence=sequence,
                )
                .count()
            )
            for i in range(count):
                pos = base + i
                s.add(
                    WorldEvent(
                        world_id=world_id,
                        operation_id=operation_id,
                        operation_sequence=sequence,
                        position=pos,
                        month=month + pos,
                        event_type=event_type,
                        event_text=f"event {sequence}:{pos}",
                        kingdom_ids=json.dumps([int(k) for k in kids]),
                        event_params=json.dumps({"i": pos}),
                        msg_key=msg_key,
                        created_at="2026-01-01 00:00:00",
                    )
                )
            s.commit()


@pytest.fixture()
def f7c(tmp_path) -> F7CEnv:
    """One isolated F7-C database + OperationService for the test."""
    return F7CEnv(tmp_path / "f7c.db")


@pytest.fixture()
def f7c_api(f7c, monkeypatch):
    """Isolated F7-C database + HTTP client bound to the SAME database.

    The app reads through ``db.SessionLocal`` (module attribute), so the
    fixture rebinds it to the fixture's session factory.  No lifespan runs,
    hence no RecoveryCoordinator unless a test installs one — exactly the
    pattern of ``tests/test_etapa5_http.py``.
    """
    from fastapi.testclient import TestClient

    import worldsim.server.app as appmod
    import worldsim.server.runner as runnermod
    from worldsim.server import db as dbmod

    monkeypatch.setattr(dbmod, "SessionLocal", f7c.Session)
    monkeypatch.setattr(appmod.app.state, "coordinator", None, raising=False)
    runnermod.registry = runnermod.RunnerRegistry()
    appmod.registry = runnermod.registry
    return TestClient(appmod.app), f7c
