"""Export legacy EvoWorld SQLite snapshots to a local, read-only archive package.

The source database is opened with SQLite mode=ro. This tool never imports data
into the live server database and intentionally exports only the historical
worlds table (not users, sessions, chat, or other tables).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

FORMAT = "evoworld-historical-archive"
FORMAT_VERSION = 1
REQUIRED_COLUMNS = {
    "id", "seed", "months_simulated", "land_pct", "created_at", "state_json",
}


class ArchiveExportError(ValueError):
    """The source snapshot does not match the supported legacy format."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_state(raw: str, *, source: str, world_id: int) -> dict:
    try:
        state = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ArchiveExportError(
            f"{source}: world {world_id} has invalid state_json"
        ) from exc
    if not isinstance(state, dict):
        raise ArchiveExportError(
            f"{source}: world {world_id} state_json must be an object"
        )

    world = state.get("world")
    if not isinstance(world, dict):
        raise ArchiveExportError(
            f"{source}: world {world_id} is missing the world object"
        )
    width, height = world.get("width"), world.get("height")
    if (not isinstance(width, int) or isinstance(width, bool) or width <= 0
            or not isinstance(height, int) or isinstance(height, bool) or height <= 0):
        raise ArchiveExportError(
            f"{source}: world {world_id} has invalid dimensions"
        )

    for key in ("biome", "rivers"):
        matrix = world.get(key)
        if (not isinstance(matrix, list) or len(matrix) != height
                or any(not isinstance(row, list) or len(row) != width for row in matrix)):
            raise ArchiveExportError(
                f"{source}: world {world_id} has invalid world.{key} dimensions"
            )

    elevation = world.get("elevation")
    if not isinstance(elevation, list) or len(elevation) != width * height:
        raise ArchiveExportError(
            f"{source}: world {world_id} has invalid world.elevation length"
        )

    kingdoms = state.get("kingdoms")
    if not isinstance(kingdoms, list):
        raise ArchiveExportError(
            f"{source}: world {world_id} kingdoms must be a list"
        )
    for index, kingdom in enumerate(kingdoms):
        if not isinstance(kingdom, dict) or not isinstance(kingdom.get("sites", []), list):
            raise ArchiveExportError(
                f"{source}: world {world_id} has invalid kingdom at index {index}"
            )

    chronicle = state.get("chronicle", [])
    if not isinstance(chronicle, list):
        raise ArchiveExportError(
            f"{source}: world {world_id} chronicle must be a list"
        )
    return state


def read_snapshot(snapshot_path: str | Path) -> dict:
    path = Path(snapshot_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ArchiveExportError(f"not a file: {path}")

    uri = path.as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ArchiveExportError(f"{path.name}: SQLite integrity_check failed: {integrity}")

            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "worlds" not in tables:
                raise ArchiveExportError(f"{path.name}: missing worlds table")

            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(worlds)")
            }
            missing = REQUIRED_COLUMNS - columns
            if missing:
                raise ArchiveExportError(
                    f"{path.name}: unsupported worlds schema; missing {sorted(missing)}"
                )

            selected = [
                "id", "seed", "months_simulated", "land_pct", "created_at", "state_json"
            ]
            if "name" in columns:
                selected.append("name")
            query = "SELECT " + ", ".join(selected) + " FROM worlds ORDER BY id"
            rows = connection.execute(query).fetchall()
            worlds = []
            for row in rows:
                world_id = int(row["id"])
                state = _validate_state(
                    row["state_json"], source=path.name, world_id=world_id
                )
                worlds.append({
                    "source_world_id": world_id,
                    "seed": row["seed"],
                    "months_simulated": row["months_simulated"],
                    "land_pct": row["land_pct"],
                    "created_at": row["created_at"],
                    "name": row["name"] if "name" in selected else "",
                    "state": state,
                })
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ArchiveExportError(f"{path.name}: SQLite read failed: {exc}") from exc

    return {
        "snapshot_name": path.name,
        "sha256": _sha256(path),
        "world_count": len(worlds),
        "worlds": worlds,
    }


def build_archive(snapshot_paths: list[str | Path]) -> dict:
    if not snapshot_paths:
        raise ArchiveExportError("at least one snapshot is required")
    sources = [read_snapshot(path) for path in snapshot_paths]
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "description": "Static historical snapshots; not restorable simulations.",
        "sources": sources,
    }


def export_archive(snapshot_paths: list[str | Path], output_path: str | Path) -> Path:
    output = Path(output_path).expanduser().resolve()
    source_paths = {Path(p).expanduser().resolve() for p in snapshot_paths}
    if output in source_paths:
        raise ArchiveExportError("output path must not overwrite a source snapshot")

    archive = build_archive(snapshot_paths)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(archive, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export historical EvoWorld SQLite snapshots without modifying them."
    )
    parser.add_argument("--snapshot", action="append", required=True,
                        help="SQLite snapshot path; repeat for multiple snapshots")
    parser.add_argument("--out", required=True, help="output JSON path")
    args = parser.parse_args(argv)
    try:
        output = export_archive(args.snapshot, args.out)
    except (ArchiveExportError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"archive written: {output}")
    print(f"sources: {len(args.snapshot)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
