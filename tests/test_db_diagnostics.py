from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "diagnose_worlds_db.py"
spec = importlib.util.spec_from_file_location("diagnose_worlds_db", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, pw_hash TEXT);
        CREATE TABLE worlds (id INTEGER PRIMARY KEY, deleted_at TEXT, next_operation_sequence INTEGER,
                             visibility TEXT, status TEXT);
        CREATE TABLE schema_checks (state TEXT CHECK (state IN ('ok', 'bad')));
        CREATE TABLE world_operations (id INTEGER PRIMARY KEY, world_id INTEGER, operation_id TEXT,
            operation_sequence INTEGER, status TEXT, continuity_state TEXT,
            FOREIGN KEY(world_id) REFERENCES worlds(id));
        CREATE TABLE world_events (world_id INTEGER, operation_sequence INTEGER,
                                   PRIMARY KEY(world_id, operation_sequence)) WITHOUT ROWID;
        CREATE TABLE challenges (id INTEGER PRIMARY KEY, state TEXT);
        CREATE TABLE challenge_participants (id INTEGER PRIMARY KEY, challenge_id INTEGER,
            user_id INTEGER, world_id INTEGER, state TEXT,
            FOREIGN KEY(world_id) REFERENCES worlds(id));
        CREATE TABLE challenge_operations (id INTEGER PRIMARY KEY, challenge_id INTEGER,
            operation TEXT, actor_user_id INTEGER, outcome TEXT, client_idempotency_key TEXT);
        CREATE TABLE operation_resolutions (id INTEGER PRIMARY KEY, world_id INTEGER,
            operation_id TEXT, resolution TEXT, resolved_at TEXT);
        CREATE INDEX ix_world_operations_world ON world_operations(world_id);
        CREATE TRIGGER trg_worlds_noop AFTER UPDATE ON worlds BEGIN SELECT 1; END;
    """)
    conn.execute("INSERT INTO users VALUES (1, 'private-user', 'secret-hash')")
    conn.execute("INSERT INTO worlds VALUES (1, NULL, 2, 'private', 'active')")
    conn.execute("INSERT INTO world_operations VALUES (1, 1, 'op-a', 1, 'confirmed', 'active')")
    conn.execute("INSERT INTO world_events VALUES (1, 2)")
    conn.execute("INSERT INTO challenges VALUES (1, 'draft')")
    # Deliberately seed invalid historical references to verify detection.
    conn.execute("INSERT INTO challenge_participants VALUES (1, 1, 1, 0, 'inscrito')")
    conn.execute("INSERT INTO challenge_participants VALUES (2, 1, 1, NULL, 'inscrito')")
    conn.commit()
    conn.close()


def test_diagnosis_detects_f7d_conflicts_without_exporting_rows(tmp_path: Path) -> None:
    db = tmp_path / "snapshot.db"
    make_db(db)
    report = module.diagnose(db)
    d = report["diagnostics"]["challenge_participants"]
    assert d["null_world_id_rows"] == 1
    assert d["zero_world_id_rows"] == 1
    assert d["duplicate_challenge_user_groups"] == 1
    assert report["input"]["unchanged_during_diagnosis"] is True
    serialized = json.dumps(report)
    assert "private-user" not in serialized
    assert "secret-hash" not in serialized
    assert "op-a" not in serialized


def test_diagnosis_reports_schema_and_counts(tmp_path: Path) -> None:
    db = tmp_path / "snapshot.db"
    make_db(db)
    report = module.diagnose(db)
    assert report["inventory"]["row_counts"]["worlds"] == 1
    assert "trg_worlds_noop" in report["inventory"]["triggers"]
    assert "CHECK" in report["inventory"]["schema_sql"]["schema_checks"]
    assert report["diagnostics"]["field_presence"]["world_operations"]["requesting_user_id"] is False
    assert report["diagnostics"]["operation_integrity"]["events_without_matching_operation_rows"] == 1
    assert report["diagnostics"]["field_presence"]["worlds"]["deleted_at"] is True
    assert report["sqlite"]["integrity_check"] == ["ok"]


def test_read_only_connection_does_not_change_database_bytes(tmp_path: Path) -> None:
    db = tmp_path / "snapshot.db"
    make_db(db)
    before = db.read_bytes()
    module.diagnose(db)
    assert db.read_bytes() == before


def test_missing_optional_tables_are_tolerated(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE worlds (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    report = module.diagnose(db)
    assert report["inventory"]["row_counts"] == {"worlds": 0}
    assert report["diagnostics"]["challenge_participants"] == {}


def test_nonexistent_database_is_rejected(tmp_path: Path) -> None:
    try:
        module.diagnose(tmp_path / "missing.db")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("missing database should be rejected")


def test_nonempty_wal_is_rejected(tmp_path: Path) -> None:
    db = tmp_path / "snapshot.db"
    make_db(db)
    Path(str(db) + "-wal").write_bytes(b"wal-pending")
    try:
        module.diagnose(db)
    except ValueError as exc:
        assert "WAL" in str(exc)
    else:
        raise AssertionError("database with WAL sidecar should be rejected")


def test_markdown_report_is_sanitized(tmp_path: Path) -> None:
    db = tmp_path / "snapshot.db"
    make_db(db)
    markdown = module.render_markdown(module.diagnose(db))
    assert "Table counts" in markdown
    assert "private-user" not in markdown
    assert "secret-hash" not in markdown


def test_schema_diagnostics_do_not_leak_unknown_state_values(tmp_path: Path) -> None:
    db = tmp_path / "snapshot.db"
    make_db(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE worlds SET status='user-specific-sensitive-value'")
    conn.commit()
    conn.close()
    report = module.diagnose(db)
    encoded = json.dumps(report)
    assert "user-specific-sensitive-value" not in encoded
    assert report["diagnostics"]["state_frequencies"]["worlds"]["status"]["unknown_value_rows"] == 1


def test_cli_writes_json_and_markdown_outside_input_directory(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    db = input_dir / "snapshot.db"
    make_db(db)
    out = tmp_path / "reports"
    assert module.main(["--db", str(db), "--out", str(out)]) == 0
    payload = json.loads((out / "worlds_db_diagnostic.json").read_text(encoding="utf-8"))
    markdown = (out / "worlds_db_diagnostic.md").read_text(encoding="utf-8")
    assert payload["safety"]["read_only_uri"] is True
    assert "Table counts" in markdown
    assert "secret-hash" not in markdown
