from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.commands.doctor_checks.dependency_checks import (
    count_legacy_dependencies,
    load_dependency_milestone_ids,
    run_dependency_doctor,
    validate_dependency_references,
)


def test_count_legacy_dependencies_counts_mapping_entries(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_root = programs_root / "demo"
    program_root.mkdir(parents=True)
    (program_root / "program.yaml").write_text(
        'schema_version: "1.0"\n'
        "key_dependencies:\n"
        "  - owner: alpha\n"
        "    label: one\n"
        "  - not-a-mapping\n"
        "  - owner: beta\n"
        "    label: two\n",
        encoding="utf-8",
    )

    assert count_legacy_dependencies("demo", programs_root=programs_root) == 2


def test_load_dependency_milestone_ids_returns_none_when_milestones_file_missing(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "demo").mkdir(parents=True)

    assert load_dependency_milestone_ids("demo", programs_root=programs_root) is None


def test_load_dependency_milestone_ids_reads_milestone_ids(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_root = programs_root / "demo"
    program_root.mkdir(parents=True)
    (program_root / "milestones.yaml").write_text(
        'schema_version: "1.0"\n'
        "milestones:\n"
        "  - id: m1\n"
        "    program_id: demo\n"
        "    name: Demo Milestone\n"
        "    target_date: 2026-05-30\n"
        "    owner_alias: demo\n"
        "    status: on_track\n"
        "    exit_criteria:\n"
        "      - Demo gate met\n"
        "    linked_workstream_ids: [ws_demo]\n"
        "    linked_work_item_ids: [1001]\n",
        encoding="utf-8",
    )

    assert load_dependency_milestone_ids("demo", programs_root=programs_root) == ("m1",)


def test_validate_dependency_references_reports_unknown_workstream_and_missing_milestones(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "fromprog").mkdir(parents=True)
    (programs_root / "toprog").mkdir(parents=True)
    dependencies = (
        SimpleNamespace(
            id="dep-1",
            from_program_id="fromprog",
            from_workstream_id="ws-missing",
            from_milestone_id=None,
            to_program_id="toprog",
            to_workstream_id=None,
            to_milestone_id="m-missing",
        ),
    )

    problems = validate_dependency_references(
        dependencies,
        programs_root=programs_root,
        load_dependency_workstream_ids_fn=lambda program_id: ("ws-known",),
        load_dependency_milestone_ids_fn=lambda program_id: None if program_id == "toprog" else ("m1",),
    )

    assert problems == [
        "Unknown from_workstream_id 'ws-missing' referenced by dependency 'dep-1'.",
        "programs/toprog/milestones.yaml is missing but dependency 'dep-1' references milestone 'm-missing'.",
    ]


def test_run_dependency_doctor_fails_when_edition_is_missing(tmp_path) -> None:
    report = run_dependency_doctor(
        edition_name="missing_weekly",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        count_legacy_dependencies_fn=lambda program_id: 0,
        validate_dependency_references_fn=lambda dependencies: [],
        get_dependencies_path_fn=lambda program_id: tmp_path / "programs" / program_id / "dependencies.yaml",
    )

    assert report.checks[0].label == "Dependencies"
    assert report.checks[0].status == "fail"
