from __future__ import annotations

from datetime import datetime, timezone

from src.commands.doctor_checks.semantic_index_checks import (
    build_semantic_index_checks,
    run_semantic_index_doctor,
    semantic_index_enabled,
)


def test_run_semantic_index_doctor_fails_when_edition_is_missing(tmp_path) -> None:
    report = run_semantic_index_doctor(
        edition_name="missing_weekly",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive",
        semantic_index_enabled_fn=lambda raw_program: False,
        build_semantic_index_checks_fn=lambda **kwargs: (),
    )

    assert report.checks[0].label == "Semantic Index"
    assert report.checks[0].status == "fail"


def test_semantic_index_enabled_reads_ai_flag() -> None:
    assert semantic_index_enabled({"ai": {"semantic_index": True}}) is True
    assert semantic_index_enabled({"ai": {}}) is False


def test_build_semantic_index_checks_keeps_dirty_ok_when_index_exists_without_state(monkeypatch, tmp_path) -> None:
    edition_name = "demo_weekly"
    archive_root = tmp_path / "archive"
    index_path = archive_root / edition_name / "semantic_index.sqlite3"
    index_path.parent.mkdir(parents=True)
    index_path.write_bytes(b"x" * (55 * 1024 * 1024))
    monkeypatch.setattr(
        "src.commands.doctor_checks.semantic_index_checks.read_archive_index",
        lambda edition_name, *, archive_root: object(),
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.semantic_index_checks.find_latest_confirmed_entry",
        lambda archive_index: type(
            "Entry",
            (),
            {
                "issue_number": 7,
                "generated_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
            },
        )(),
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.semantic_index_checks.load_semantic_index_state",
        lambda edition_name, *, archive_root: None,
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.semantic_index_checks.get_semantic_index_path",
        lambda edition_name, *, archive_root: index_path,
    )

    checks = {check.label: check for check in build_semantic_index_checks(edition_name=edition_name, archive_root=archive_root)}

    assert checks["Semantic Freshness"].status == "warn"
    assert checks["Semantic Dirty"].status == "ok"
    assert checks["Semantic Optimize"].status == "warn"


def test_build_semantic_index_checks_keeps_optimize_ok_when_large_index_was_optimized(monkeypatch, tmp_path) -> None:
    edition_name = "demo_weekly"
    archive_root = tmp_path / "archive"
    index_path = archive_root / edition_name / "semantic_index.sqlite3"
    index_path.parent.mkdir(parents=True)
    index_path.write_bytes(b"x" * (55 * 1024 * 1024))
    state = type(
        "State",
        (),
        {
            "last_built_at": datetime(2026, 6, 2, tzinfo=timezone.utc),
            "semantic_index_dirty": False,
            "dirty_reason": None,
            "last_optimized_document_count": 0,
            "indexed_document_count": 0,
        },
    )()
    monkeypatch.setattr(
        "src.commands.doctor_checks.semantic_index_checks.read_archive_index",
        lambda edition_name, *, archive_root: object(),
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.semantic_index_checks.find_latest_confirmed_entry",
        lambda archive_index: type(
            "Entry",
            (),
            {
                "issue_number": 7,
                "generated_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
            },
        )(),
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.semantic_index_checks.load_semantic_index_state",
        lambda edition_name, *, archive_root: state,
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.semantic_index_checks.get_semantic_index_path",
        lambda edition_name, *, archive_root: index_path,
    )

    checks = {check.label: check for check in build_semantic_index_checks(edition_name=edition_name, archive_root=archive_root)}

    assert checks["Semantic Freshness"].status == "ok"
    assert checks["Semantic Dirty"].status == "ok"
    assert checks["Semantic Optimize"].status == "ok"
