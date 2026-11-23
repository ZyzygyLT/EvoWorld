#!/usr/bin/env python3
"""Read-only, privacy-preserving diagnostic for an EvoWorld SQLite snapshot.

Input must be a quiescent, consistent SQLite backup, not a live database.
Uses only the standard library and never imports the application or ORM.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_ALLOWLIST = {
    "worlds": {"status": {"active", "paused", "closed", "running", "stopped"},
               "visibility": {"public", "private"}},
    "world_operations": {"status": {"pending", "executing", "confirmed", "blocked", "failed", "canceled"},
                         "continuity_state": {"active", "blocked", "gap"}},
    "challenges": {"state": {"draft", "published", "enrollment_open", "enrollment_closed",
                               "preparing", "running", "completed", "canceled", "disputed",
                               "resolved", "invalidated"}},
    "challenge_participants": {"state": {"inscrito", "preparando", "ejecutando", "finalizado",
                                          "abandonado"}},
    "challenge_operations": {"operation": {"create", "publish", "open_enrollment", "enroll",
        "close_enrollment", "prepare", "start", "cancel", "complete", "confirm", "dispute", "resolve"}},
}


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecar_guard(path: Path) -> None:
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError(
                f"Input has a non-empty SQLite {suffix[1:].upper()} sidecar; "
                "create a consistent backup first"
            )


def _columns(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(f"PRAGMA table_info({quote_ident(table)})")]


def _has(columns: list[dict[str, Any]], name: str) -> bool:
    return any(col["name"] == name for col in columns)


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0])


def _grouped_allowlisted(conn: sqlite3.Connection, table: str, column: str,
                         allowed: set[str]) -> dict[str, Any]:
    rows = conn.execute(
        f"SELECT {quote_ident(column)}, COUNT(*) FROM {quote_ident(table)} "
        f"GROUP BY {quote_ident(column)}"
    ).fetchall()
    result = {value: 0 for value in sorted(allowed)}
    unknown = nulls = 0
    for value, count in rows:
        if value is None:
            nulls += int(count)
        elif isinstance(value, str) and value in allowed:
            result[value] += int(count)
        else:
            unknown += int(count)
    return {"known_values": result, "unknown_value_rows": unknown, "null_rows": nulls}


def diagnose(db_path: str | Path) -> dict[str, Any]:
    path = Path(db_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("Database path must point to a regular file")
    _sidecar_guard(path)
    before_hash, before_stat = sha256_file(path), path.stat()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    try:
        objects = conn.execute(
            "SELECT name, type, sql FROM sqlite_master "
            "WHERE type IN ('table','view','index','trigger') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()
        ddl_by_name = {row["name"]: row["sql"] for row in objects}
        tables = [row["name"] for row in objects if row["type"] == "table"]
        views = [row["name"] for row in objects if row["type"] == "view"]
        triggers = [row["name"] for row in objects if row["type"] == "trigger"]
        schema: dict[str, Any] = {}
        counts: dict[str, int] = {}
        table_columns: dict[str, list[dict[str, Any]]] = {}
        for table in tables:
            cols = table_columns[table] = _columns(conn, table)
            indexes = []
            for idx in conn.execute(f"PRAGMA index_list({quote_ident(table)})"):
                info = [dict(x) for x in conn.execute(
                    f"PRAGMA index_info({quote_ident(idx['name'])})"
                )]
                indexes.append({"name": idx["name"], "unique": bool(idx["unique"]),
                                "origin": idx["origin"], "partial": bool(idx["partial"]),
                                "columns": [x["name"] for x in info]})
            fks = [dict(x) for x in conn.execute(
                f"PRAGMA foreign_key_list({quote_ident(table)})"
            )]
            schema[table] = {
                "create_sql": ddl_by_name.get(table),
                "columns": [{"name": c["name"], "type": c["type"],
                             "notnull": bool(c["notnull"]), "primary_key_position": c["pk"]}
                            for c in cols],
                "indexes": indexes,
                "foreign_keys": [{"table": fk["table"], "from": fk["from"], "to": fk["to"],
                                  "on_update": fk["on_update"], "on_delete": fk["on_delete"]}
                                 for fk in fks],
            }
            counts[table] = _count(conn, table)

        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        fk_violations = [dict(row) for row in conn.execute("PRAGMA foreign_key_check")]
        diagnostics: dict[str, Any] = {
            "challenge_participants": {}, "world_sequences": {},
            "operation_integrity": {}, "field_presence": {}, "state_frequencies": {},
        }
        required = {
            "worlds": ["deleted_at", "visibility", "status", "next_operation_sequence"],
            "world_operations": ["operation_id", "operation_sequence", "requesting_user_id",
                                 "client_idempotency_key", "request_hash", "status"],
            "world_events": ["operation_id", "operation_sequence"],
            "operation_resolutions": ["resolution", "resolved_at", "resolution_type", "idempotency_key"],
            "challenges": ["closed_at", "completed_at", "state"],
            "challenge_participants": ["world_id", "joined_at", "result_json", "result_verified", "state"],
            "challenge_operations": ["operation", "actor_user_id", "outcome", "client_idempotency_key"],
            "users": ["is_admin"],
        }
        for table, wanted in required.items():
            diagnostics["field_presence"][table] = {
                col: table in table_columns and _has(table_columns[table], col) for col in wanted
            }

        if "challenge_participants" in tables:
            cols, q = table_columns["challenge_participants"], '"challenge_participants"'
            if _has(cols, "world_id"):
                diagnostics["challenge_participants"].update({
                    "null_world_id_rows": int(conn.execute(
                        f"SELECT COUNT(*) FROM {q} WHERE world_id IS NULL").fetchone()[0]),
                    "zero_world_id_rows": int(conn.execute(
                        f"SELECT COUNT(*) FROM {q} WHERE world_id = 0").fetchone()[0]),
                })
                if all(_has(cols, c) for c in ("challenge_id", "user_id")):
                    diagnostics["challenge_participants"]["duplicate_challenge_user_groups"] = int(
                        conn.execute(f"SELECT COUNT(*) FROM (SELECT challenge_id,user_id FROM {q} "
                                     "GROUP BY challenge_id,user_id HAVING COUNT(*)>1").fetchone()[0])
                if "worlds" in tables:
                    diagnostics["challenge_participants"]["orphan_world_id_rows"] = int(
                        conn.execute(f"SELECT COUNT(*) FROM {q} p LEFT JOIN worlds w ON w.id=p.world_id "
                                     "WHERE p.world_id IS NOT NULL AND w.id IS NULL").fetchone()[0])
                    diagnostics["challenge_participants"]["duplicate_world_id_groups"] = int(
                        conn.execute(f"SELECT COUNT(*) FROM (SELECT world_id FROM {q} "
                                     "WHERE world_id IS NOT NULL GROUP BY world_id HAVING COUNT(*)>1)"
                                     ).fetchone()[0])

        if "worlds" in tables and "world_operations" in tables:
            wc, oc = table_columns["worlds"], table_columns["world_operations"]
            if _has(wc, "next_operation_sequence") and all(
                _has(oc, c) for c in ("world_id", "operation_sequence")
            ):
                diagnostics["world_sequences"] = {
                    "null_or_missing_counter_worlds": int(conn.execute(
                        "SELECT COUNT(*) FROM worlds WHERE next_operation_sequence IS NULL"
                    ).fetchone()[0]),
                    "counter_not_ahead_of_max_sequence_worlds": int(conn.execute(
                        "SELECT COUNT(*) FROM worlds w WHERE next_operation_sequence <= "
                        "COALESCE((SELECT MAX(o.operation_sequence) FROM world_operations o "
                        "WHERE o.world_id=w.id),0)"
                    ).fetchone()[0]),
                }
            if _has(oc, "operation_id"):
                diagnostics["operation_integrity"]["null_or_empty_operation_id_rows"] = int(
                    conn.execute("SELECT COUNT(*) FROM world_operations "
                                 "WHERE operation_id IS NULL OR operation_id=''").fetchone()[0])
            if all(_has(oc, c) for c in ("world_id", "operation_sequence")):
                diagnostics["operation_integrity"]["duplicate_world_sequence_groups"] = int(
                    conn.execute("SELECT COUNT(*) FROM (SELECT world_id,operation_sequence "
                                 "FROM world_operations GROUP BY world_id,operation_sequence "
                                 "HAVING COUNT(*)>1)").fetchone()[0])
            if all(_has(oc, c) for c in ("world_id", "operation_id")):
                diagnostics["operation_integrity"]["duplicate_world_operation_id_groups"] = int(
                    conn.execute("SELECT COUNT(*) FROM (SELECT world_id,operation_id "
                                 "FROM world_operations GROUP BY world_id,operation_id "
                                 "HAVING COUNT(*)>1)").fetchone()[0])
            if all(_has(oc, c) for c in ("world_id", "requesting_user_id", "client_idempotency_key")):
                diagnostics["operation_integrity"]["duplicate_idempotency_groups"] = int(
                    conn.execute("SELECT COUNT(*) FROM (SELECT world_id,requesting_user_id,"
                                 "client_idempotency_key FROM world_operations GROUP BY world_id,"
                                 "requesting_user_id,client_idempotency_key HAVING COUNT(*)>1"
                                 ).fetchone()[0])

        if "world_events" in tables and "world_operations" in tables:
            ec, oc = table_columns["world_events"], table_columns["world_operations"]
            if all(_has(ec, c) for c in ("world_id", "operation_id")) and all(
                _has(oc, c) for c in ("world_id", "operation_id")
            ):
                diagnostics["operation_integrity"]["events_without_matching_operation_rows"] = int(
                    conn.execute("SELECT COUNT(*) FROM world_events e WHERE NOT EXISTS "
                                 "(SELECT 1 FROM world_operations o WHERE "
                                 "o.world_id=e.world_id AND o.operation_id=e.operation_id)"
                                 ).fetchone()[0])
            elif all(_has(ec, c) for c in ("world_id", "operation_sequence")) and all(
                _has(oc, c) for c in ("world_id", "operation_sequence")
            ):
                diagnostics["operation_integrity"]["events_without_matching_operation_rows"] = int(
                    conn.execute("SELECT COUNT(*) FROM world_events e WHERE NOT EXISTS "
                                 "(SELECT 1 FROM world_operations o WHERE "
                                 "o.world_id=e.world_id AND "
                                 "o.operation_sequence=e.operation_sequence)"
                                 ).fetchone()[0])

        if "challenge_operations" in tables:
            cc = table_columns["challenge_operations"]
            if all(_has(cc, c) for c in ("challenge_id", "actor_user_id", "client_idempotency_key")):
                diagnostics["operation_integrity"]["duplicate_challenge_idempotency_groups"] = int(
                    conn.execute("SELECT COUNT(*) FROM (SELECT challenge_id,actor_user_id,"
                                 "client_idempotency_key FROM challenge_operations GROUP BY "
                                 "challenge_id,actor_user_id,client_idempotency_key HAVING COUNT(*)>1"
                                 ).fetchone()[0])

        for table, columns in STATE_ALLOWLIST.items():
            if table in table_columns:
                for col, allowed in columns.items():
                    if _has(table_columns[table], col):
                        diagnostics["state_frequencies"].setdefault(table, {})[col] = (
                            _grouped_allowlisted(conn, table, col, allowed)
                        )

        after_hash, after_stat = sha256_file(path), path.stat()
        unchanged = (before_hash == after_hash and before_stat.st_size == after_stat.st_size
                     and before_stat.st_mtime_ns == after_stat.st_mtime_ns)
        if not unchanged:
            raise RuntimeError("Input database changed during diagnosis; discard this report")
        return {
            "report_version": 1,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "input": {"filename": path.name, "size_bytes": before_stat.st_size,
                      "sha256": before_hash, "unchanged_during_diagnosis": unchanged},
            "sqlite": {"version": sqlite3.sqlite_version, "integrity_check": integrity,
                       "foreign_key_violation_count": len(fk_violations),
                       "foreign_key_violations": fk_violations},
            "inventory": {"tables": tables, "views": views, "triggers": triggers,
                          "row_counts": counts, "schema": schema,
                          "schema_sql": {row["name"]: row["sql"] for row in objects}},
            "diagnostics": diagnostics,
            "safety": {"read_only_uri": True, "query_only": True,
                       "application_imported": False, "data_rows_exported": False,
                       "migrations_executed": False},
        }
    finally:
        conn.close()


def render_markdown(report: dict[str, Any]) -> str:
    inv = report["inventory"]
    lines = ["# EvoWorld SQLite diagnostic", "", f"- Generated: {report['generated_at_utc']}",
             f"- Input: {report['input']['filename']}",
             f"- SHA-256: {report['input']['sha256']}", "", "## Integrity", "",
             f"- integrity_check: {', '.join(report['sqlite']['integrity_check'])}",
             f"- Foreign-key violations: {report['sqlite']['foreign_key_violation_count']}",
             "", "## Table counts", "", "| Table | Rows |", "|---|---:|"]
    lines.extend(f"| {name} | {count} |" for name, count in inv["row_counts"].items())
    lines.extend(["", "## Structural inventory", "", f"- Tables: {len(inv['tables'])}",
                  f"- Views: {len(inv['views'])}", f"- Triggers: {len(inv['triggers'])}",
                  "", "## Diagnostics", "", json.dumps(report["diagnostics"], ensure_ascii=False, indent=2),
                  "", "## Safety", "", "- Read-only connection; query-only enabled.",
                  "- No application/ORM imports; no row-level data exported.",
                  "- No DDL/DML or migrations executed.", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path,
                        help="Path to a quiescent, consistent SQLite backup")
    parser.add_argument("--out", required=True, type=Path,
                        help="Output directory outside the input database directory")
    args = parser.parse_args(argv)
    try:
        db = args.db.expanduser().resolve(strict=True)
        out = args.out.expanduser().resolve()
        if out == db.parent or db.parent in out.parents:
            raise ValueError("Output directory must be outside the input database directory")
        report = diagnose(db)
        out.mkdir(parents=True, exist_ok=True)
        (out / "worlds_db_diagnostic.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (out / "worlds_db_diagnostic.md").write_text(render_markdown(report), encoding="utf-8")
        print(f"Diagnostic complete: {out}")
        print(f"Tables: {len(report['inventory']['tables'])}; "
              f"FK violations: {report['sqlite']['foreign_key_violation_count']}")
        return 0
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        print(f"Diagnostic refused/failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
