from __future__ import annotations

from pathlib import Path

from src.commands.doctor_checks.milestone_checks import load_current_milestones, run_milestone_doctor


def test_load_current_milestones_reads_program_fact_milestones(tmp_path: Path) -> None:
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

    milestones = load_current_milestones("demo", programs_root=programs_root)

    assert len(milestones) == 1
    assert milestones[0].id == "m1"


def test_run_milestone_doctor_fails_when_edition_is_missing(tmp_path) -> None:
    report = run_milestone_doctor(
        edition_name="missing_weekly",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive",
        load_current_milestones_fn=lambda program_id: (),
        load_milestone_owner_aliases_fn=lambda program_id: (),
        build_milestone_health_warning_fn=lambda edition_name, program_id, milestones: None,
    )

    assert report.checks[0].label == "Milestones"
    assert report.checks[0].status == "fail"
