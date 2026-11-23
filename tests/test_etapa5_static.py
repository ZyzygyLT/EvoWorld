"""Etapa 5 static verification (DEC09 / DEC16).

RUNS WITHOUT FastAPI/httpx:

- AST/source checks of app.py: no WS tick/fast-forward, Etapa-4 pause/resume
  preserved, recoverable-before-row guards, DEC16 exception handlers, the
  only tick routes are create_world/get_kingdom_stats, runner._run still
  ticks.
- Unit tests of the pure errors.py module (is_infra_error, ws_close_code,
  not_recoverable_reason, forget_not_recoverable).
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1] / "src" / "worldsim" / "server"
APP_PATH = ROOT / "app.py"
RUNNER_PATH = ROOT / "runner.py"
ERRORS_PATH = ROOT / "errors.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_source(path), filename=str(path))


def _func_source(tree: ast.Module, name: str) -> str:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(_source(APP_PATH), node) or ""
    raise AssertionError(f"function {name!r} not found in app.py")


def _contains(source: str, *needles: str) -> bool:
    return any(n in source for n in needles)


# ---------------------------------------------------------------------------
# app.py source-level checks (no import — runs without FastAPI)
# ---------------------------------------------------------------------------

APP_SRC = _source(APP_PATH)
APP_TREE = _tree(APP_PATH)


def test_app_has_no_ws_tick_or_fast_forward():
    """DEC09: ws_world contains no tick() call and no fast-forward loop."""
    ws = _func_source(APP_TREE, "ws_world")
    assert "runner.eng.tick" not in ws, "ws_world must not call tick()"
    assert ".tick(" not in ws, "ws_world must not call any tick()"
    assert "months_simulated" not in ws, "ws_world must not fast-forward from months_simulated"
    # the comment documents the absence; the code itself must not tick
    assert "for _ in range" not in ws, "ws_world must not fast-forward with a range loop"


def test_ws_preserves_etapa4_pause_resume():
    """Etapa 4 / DEC14: ws_world still routes pause/resume via the coordinator."""
    ws = _func_source(APP_TREE, "ws_world")
    assert "pause_world(" in ws
    assert "resume_world(" in ws
    assert "TransitionError" in ws


def test_ws_recoverable_guard_before_row_and_runner():
    """§5/DEC16 + DEC20/§14.3: existence + visibility (404) run BEFORE the
    DEC16 guard (409), and both precede registry.get (no lazy runner)."""
    ws = _func_source(APP_TREE, "ws_world")
    i_row = ws.index("_load_row(world_id)")
    i_vis = ws.index("_require_visible(row, user)")
    i_req = ws.index("_require_recoverable(world_id)")
    i_get = ws.index("registry.get(")
    assert i_row < i_vis < i_req, (
        "ws_world must gate 404 (existence) then visibility before 409"
    )
    assert i_req < i_get, "_require_recoverable must precede registry.get("


def test_http_routes_guarded():
    """post_prediction / post_chat / post_claim: DEC20/§14.3 order —
    existence+visibility (404) FIRST, then the DEC16 guard (409)."""
    for func in ("post_prediction", "post_chat", "post_claim"):
        src = _func_source(APP_TREE, func)
        assert "_require_recoverable(world_id)" in src, f"{func} needs the 409 guard"
        assert "_load_visible_row(world_id" in src, (
            f"{func} needs the 404+visibility guard"
        )
        i_vis = src.index("_load_visible_row(world_id")
        i_req = src.index("_require_recoverable(world_id)")
        assert i_vis < i_req, (
            f"{func}: existence+visibility must precede the 409 guard"
        )


def test_delete_exempt_and_forgets():
    """delete_world is DEC10-exempt (no 409) but forgets on confirmed delete."""
    delete = _func_source(APP_TREE, "delete_world")
    assert "_require_recoverable" not in delete, "DELETE must not 409 (DEC10 cleanup)"
    assert "_forget_recoverable(world_id)" in delete, "DELETE must forget on confirmed delete"


def test_dec16_exception_handlers_registered():
    """DEC16: 400 validation + 503 infrastructure handlers are installed."""
    assert "@app.exception_handler(RequestValidationError)" in APP_SRC
    assert "@app.exception_handler(OperationalError)" in APP_SRC
    assert "@app.exception_handler(InterfaceError)" in APP_SRC


def test_only_http_tick_routes():
    """DEC09: the only functions calling .tick() are create_world and
    get_kingdom_stats (both HTTP, out of DEC09 scope)."""
    ticked = set()
    for node in APP_TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            src = ast.get_source_segment(APP_SRC, node) or ""
            if ".tick(" in src:
                ticked.add(node.name)
    assert ticked == {"create_world", "get_kingdom_stats"}, ticked


def test_runner_run_still_ticks():
    """DEC09: the only ordinary tick route remains WorldRunner._run()."""
    runner_src = _source(RUNNER_PATH)
    runner_tree = ast.parse(runner_src, filename=str(RUNNER_PATH))
    run_src = ""
    for node in runner_tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "WorldRunner":
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "_run":
                    run_src = ast.get_source_segment(runner_src, item) or ""
                    break
    assert run_src, "WorldRunner._run not found"
    assert "eng.tick()" in run_src or "self.eng.tick()" in run_src


# ---------------------------------------------------------------------------
# errors.py unit tests (pure module, importable without FastAPI)
# ---------------------------------------------------------------------------

errors_mod = importlib.import_module("worldsim.server.errors")


class _FakeResult:
    def __init__(self, status: str, detail: str = "") -> None:
        self.status = status
        self.detail = detail


class _FakeCoord:
    def __init__(self, table: dict | None) -> None:
        self.not_recoverable = table


@pytest.mark.parametrize(
    "exc, expected",
    [
        (OSError("boom"), False),
        (ConnectionError(), True),
        (TimeoutError(), True),
        (ValueError("nope"), False),
        (TypeError("nope"), False),
        (RuntimeError("nope"), False),
    ],
)
def test_is_infra_error(exc, expected):
    assert errors_mod.is_infra_error(exc) is expected


def test_is_infra_error_uses_class_names_duck_typing():
    """sqlalchemy.exc.OperationalError/InterfaceError classify by name
    (duck-typing on type(exc).__name__, no fastapi needed)."""
    from sqlalchemy.exc import InterfaceError, OperationalError

    assert errors_mod.is_infra_error(OperationalError("stmt", (), None)) is True
    assert errors_mod.is_infra_error(InterfaceError("stmt", (), None)) is True


@pytest.mark.parametrize(
    "status, code",
    [
        (404, 4404),
        (409, 4409),
        (400, 4400),
        (503, 4503),
        (418, 4418),
    ],
)
def test_ws_close_code(status, code):
    assert errors_mod.ws_close_code(status) == code


def test_not_recoverable_reason_absent_coordinator():
    assert errors_mod.not_recoverable_reason(None, 1) is None


def test_not_recoverable_reason_empty_table():
    assert errors_mod.not_recoverable_reason(_FakeCoord({}), 1) is None


def test_not_recoverable_reason_records_not_recoverable():
    coord = _FakeCoord({7: _FakeResult("not_recoverable", "no checkpoint")})
    assert errors_mod.not_recoverable_reason(coord, 7) == "no checkpoint"


def test_not_recoverable_reason_other_status_not_blocking():
    # no_checkpoints is NOT recorded as not_recoverable (only status ==
    # "not_recoverable" is), so lazy registry.get is still allowed for it.
    coord = _FakeCoord({3: _FakeResult("no_checkpoints")})
    assert errors_mod.not_recoverable_reason(coord, 3) is None


def test_forget_not_recoverable():
    table = {5: _FakeResult("not_recoverable")}
    coord = _FakeCoord(table)
    errors_mod.forget_not_recoverable(coord, 5)
    assert 5 not in coord.not_recoverable
    # missing id is a no-op
    errors_mod.forget_not_recoverable(coord, 999)
    errors_mod.forget_not_recoverable(None, 1)
