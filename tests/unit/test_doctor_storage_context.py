from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

import src.commands.doctor_checks.storage_checks as storage


def _seed_database(path: Path, *, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE program_fact_revisions (id INTEGER PRIMARY KEY)")
        connection.executemany("INSERT INTO program_fact_revisions VALUES (?)", [(index,) for index in range(rows)])


def test_selected_storage_warns_about_home_candidate_without_reading_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    canonical = db_root / "synthetic" / "vertex.sqlite3"
    _seed_database(canonical, rows=3)
    home_candidate = Path.home() / ".vertex" / "synthetic" / "vertex.sqlite3"
    original_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: True if path == home_candidate else original_exists(path))
    original_count = storage._count_fact_store_rows

    def count_selected(path: Path) -> int:
        assert path.is_relative_to(tmp_path), "Storage doctor must not read an ambient home database"
        return original_count(path)

    count = Mock(side_effect=count_selected)
    monkeypatch.setattr(storage, "_count_fact_store_rows", count)

    check = storage._stray_fact_store_database_check(
        "synthetic", programs_root=programs_root, db_root=db_root,
    )

    assert check.status == "warn"
    assert check.metadata["canonical_path"] == str(canonical)
    assert check.metadata["canonical_row_count"] == 3
    assert check.metadata["stray_databases"]["home_fallback"] == {
        "path": str(home_candidate),
        "row_count": None,
    }
    assert "row count not read" in check.detail
    count.assert_called_once_with(canonical)


@pytest.mark.parametrize(
    "authority",
    ["selected_default", "configured_directory", "configured_file", "explicit_directory", "explicit_file"],
)
def test_selected_storage_keeps_database_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)
    db_root = None
    if authority == "selected_default":
        expected_root = tmp_path / "vertex-db"
    else:
        expected_root = tmp_path / ("selected.sqlite3" if authority.endswith("file") else "selected-db")
        monkeypatch.setenv("VERTEX_DB_PATH", str(expected_root))
        if authority.startswith("explicit"):
            db_root = expected_root
            monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "unselected-db"))
    canonical = expected_root if expected_root.suffix == ".sqlite3" else expected_root / "synthetic" / "vertex.sqlite3"
    stray = programs_root / "synthetic" / "vertex.sqlite3"
    _seed_database(canonical, rows=3)
    _seed_database(stray, rows=2)
    home_candidate = Path.home() / ".vertex" / "synthetic" / "vertex.sqlite3"
    original_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: False if path == home_candidate else original_exists(path))

    check = storage._stray_fact_store_database_check(
        "synthetic", programs_root=programs_root, db_root=db_root,
    )

    assert check.status == "warn"
    assert check.metadata["canonical_path"] == str(canonical)
    assert check.metadata["canonical_row_count"] == 3
    assert check.metadata["stray_databases"] == {
        "programs_root_relative": {"path": str(stray), "row_count": 2},
    }


def test_storage_row_count_opens_database_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "vertex.sqlite3"
    _seed_database(database, rows=2)
    before = database.read_bytes()
    connect = Mock(wraps=sqlite3.connect)
    monkeypatch.setattr(storage.sqlite3, "connect", connect)

    assert storage._count_fact_store_rows(database) == 2

    assert connect.call_args.kwargs.get("uri") is True
    assert connect.call_args.args[0].endswith("?mode=ro")
    assert database.read_bytes() == before


@pytest.mark.parametrize("state", ["missing", "no_table", "invalid"])
def test_storage_row_count_does_not_initialize_or_repair_database(
    tmp_path: Path,
    state: str,
) -> None:
    database = tmp_path / "vertex.sqlite3"
    if state == "no_table":
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE unrelated (id INTEGER)")
    elif state == "invalid":
        database.write_text("not a sqlite database", encoding="utf-8")
    before = database.read_bytes() if database.exists() else None

    assert storage._count_fact_store_rows(database) == 0

    assert (database.read_bytes() if database.exists() else None) == before


def test_storage_lineage_keeps_explicit_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.core.fact_lineage_coverage as lineage

    programs_root = tmp_path / "programs"
    archive_root = tmp_path / "selected-history" / "archive"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "vertex-db"))
    compute = Mock(wraps=lineage.compute_lineage_coverage)
    monkeypatch.setattr(lineage, "compute_lineage_coverage", compute)

    check = storage._fact_lineage_coverage_check(
        "synthetic", programs_root=programs_root, archive_root=archive_root,
    )

    assert check.status == "ok"
    assert check.metadata["total_count"] == 0
    compute.assert_called_once_with(
        "synthetic", programs_root=programs_root, archive_root=archive_root,
    )
