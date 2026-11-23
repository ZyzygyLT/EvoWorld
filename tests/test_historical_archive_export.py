from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from tools.export_historical_archive import (
    ArchiveExportError,
    build_archive,
    export_archive,
)


def _state(width: int = 2, height: int = 2) -> dict:
    return {
        "month": 120,
        "year": 10,
        "seed": 7,
        "world": {
            "width": width,
            "height": height,
            "biome": [[3] * width for _ in range(height)],
            "rivers": [[0] * width for _ in range(height)],
            "elevation": [100] * (width * height),
        },
        "kingdoms": [{"id": 0, "name": "A", "sites": []}],
        "chronicle": [{"id": 1, "month": 2, "type": "MILESTONE", "text": "test"}],
    }


def _snapshot(path, state=None):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE worlds (id INTEGER PRIMARY KEY, seed INTEGER, width INTEGER, "
        "height INTEGER, months_simulated INTEGER, land_pct REAL, state_json TEXT, "
        "created_at TEXT)"
    )
    connection.execute(
        "INSERT INTO worlds VALUES (1, 7, 2, 2, 120, 55.5, ?, '2026-01-01')",
        (json.dumps(state or _state()),),
    )
    connection.commit()
    connection.close()
    return path


def test_build_archive_exports_only_world_rows_and_source_hash(tmp_path):
    source = _snapshot(tmp_path / "historic.db")
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    archive = build_archive([source])

    assert archive["format"] == "evoworld-historical-archive"
    assert archive["format_version"] == 1
    assert archive["sources"][0]["sha256"] == before
    assert archive["sources"][0]["world_count"] == 1
    assert archive["sources"][0]["worlds"][0]["state"] == _state()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_exports_legacy_map_when_elevation_is_absent(tmp_path):
    state = _state()
    del state["world"]["elevation"]
    source = _snapshot(tmp_path / "legacy-no-elevation.db", state)

    archive = build_archive([source])

    exported = archive["sources"][0]["worlds"][0]["state"]
    assert "elevation" not in exported["world"]


def test_export_archive_writes_package_without_touching_source(tmp_path):
    source = _snapshot(tmp_path / "historic.db")
    before = source.read_bytes()
    output = export_archive([source], tmp_path / "archive.json")

    package = json.loads(output.read_text(encoding="utf-8"))
    assert package["sources"][0]["worlds"][0]["source_world_id"] == 1
    assert source.read_bytes() == before


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(world={}),
    lambda s: s["world"].update(biome=[[1]]),
    lambda s: s["world"].update(elevation=[1]),
    lambda s: s.update(kingdoms={}),
    lambda s: s.update(chronicle={}),
])
def test_rejects_malformed_historical_payload(tmp_path, mutate):
    state = _state()
    mutate(state)
    source = _snapshot(tmp_path / "bad.db", state)
    with pytest.raises(ArchiveExportError):
        build_archive([source])


def test_rejects_unsupported_database_schema(tmp_path):
    source = tmp_path / "wrong.db"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE worlds (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    with pytest.raises(ArchiveExportError, match="unsupported worlds schema"):
        build_archive([source])


def test_rejects_output_overwriting_source(tmp_path):
    source = _snapshot(tmp_path / "historic.db")
    with pytest.raises(ArchiveExportError, match="must not overwrite"):
        export_archive([source], source)
