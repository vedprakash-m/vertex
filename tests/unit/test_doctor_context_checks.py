from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from src.commands.doctor_checks import context_checks
from src.core.program_context import InvariantSeverity


def test_run_context_doctor_fails_when_programs_are_missing(tmp_path: Path) -> None:
    report = context_checks.run_context_doctor(
        edition_name=None,
        programs_root=tmp_path / "programs",
        editions_root=tmp_path / "editions",
    )

    assert report.checks == (
        context_checks.DoctorCheck("Context", "fail", "No program.yaml found in programs/."),
    )


def test_run_context_doctor_emits_fix_hints_and_coverage(monkeypatch, tmp_path: Path) -> None:
    program_dir = tmp_path / "programs" / "alpha"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (program_dir / "workstream_registry.yaml").write_text("workstreams: []\n", encoding="utf-8")
    (program_dir / "kpis.yaml").write_text("kpis: []\n", encoding="utf-8")

    ctx = SimpleNamespace(
        maturity_level=SimpleNamespace(value=1),
        maturity_blockers=("Need better data",),
        invariant_violations=(
            SimpleNamespace(severity=InvariantSeverity.ERROR, code="WS-01", detail="Broken workstream"),
            SimpleNamespace(severity=InvariantSeverity.WARN, code="MS-01", detail="Warn milestone"),
        ),
        staleness_flags=(
            SimpleNamespace(file="workstreams.yaml", entity_id="ws-1", days_stale=8, field="last_reviewed"),
        ),
    )

    monkeypatch.setattr(context_checks, "load_program_context", lambda *args, **kwargs: ctx)
    monkeypatch.setattr(context_checks, "check_schema_versions", lambda program_dir: (True, []))
    monkeypatch.setattr(
        context_checks,
        "load_yaml_document",
        lambda path: (
            {"workstreams": [{"id": "ws-1", "deep_context": {}, "roles": [{"role": "primary_owner"}]}]}
            if path.name == "workstream_registry.yaml"
            else {"kpis": [{"id": "kpi-1", "validated": False}]}
        ),
    )
    emitted: list[tuple[str, str | None, str]] = []
    monkeypatch.setattr(
        context_checks,
        "append_context_gap",
        lambda **kwargs: emitted.append((kwargs["field"], kwargs["lane"], kwargs["impact_estimate"])),
    )

    report = context_checks.run_context_doctor(
        edition_name=None,
        programs_root=tmp_path / "programs",
        editions_root=tmp_path / "editions",
        fix_hints=True,
    )

    labels = [check.label for check in report.checks]
    assert "Maturity level" in labels
    assert "Cross-file invariants" in labels
    assert "Staleness" in labels
    assert "Coverage" in labels
    assert labels.count("Fix hint") == 3
    assert ("deep_context.why", "ws-1", "high") in emitted
    assert ("kpis.validated", None, "medium") in emitted


def test_run_ranked_gaps_reports_empty_store(monkeypatch, tmp_path: Path, capsys) -> None:
    program_dir = tmp_path / "programs" / "alpha"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text("schema_version: 1\n", encoding="utf-8")

    monkeypatch.setattr(context_checks, "load_context_gaps", lambda program_id, programs_root: [])

    context_checks.run_ranked_gaps(
        edition_name=None,
        programs_root=tmp_path / "programs",
        editions_root=tmp_path / "editions",
    )

    captured = capsys.readouterr()
    assert "No context gaps recorded yet." in captured.out


def test_emit_context_gaps_records_expected_signals(monkeypatch) -> None:
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(context_checks, "append_context_gap", lambda **kwargs: captured.append(kwargs))

    context_checks.emit_context_gaps(
        program_id="alpha",
        registry_entries=[
            {
                "id": "ws-1",
                "deep_context": {},
                "roles": [{"role": "primary_owner"}],
                "workiq_latest": f"{(date.today() - timedelta(days=10)).isoformat()}: note",
            }
        ],
        kpis_list=[{"id": "kpi-1", "validated": False}],
        today=date.today(),
    )

    fields = {(item["field"], item["lane"]) for item in captured}
    assert ("deep_context.why", "ws-1") in fields
    assert ("deep_context.what", "ws-1") in fields
    assert ("roles.primary_owner.email", "ws-1") in fields
    assert ("workiq_latest", "ws-1") in fields
    assert ("kpis.validated", None) in fields


def test_run_context_doctor_surfaces_pending_ncfl_proposals(monkeypatch, tmp_path: Path) -> None:
    program_dir = tmp_path / "programs" / "alpha"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (program_dir / "workstream_registry.yaml").write_text("workstreams: []\n", encoding="utf-8")
    (program_dir / "kpis.yaml").write_text("kpis: []\n", encoding="utf-8")

    ctx = SimpleNamespace(
        maturity_level=SimpleNamespace(value=2),
        maturity_blockers=(),
        invariant_violations=(),
        staleness_flags=(),
    )
    pending = (
        SimpleNamespace(issue_number=4),
        SimpleNamespace(issue_number=7),
    )
    monkeypatch.setattr(context_checks, "load_program_context", lambda *args, **kwargs: ctx)
    monkeypatch.setattr(context_checks, "check_schema_versions", lambda program_dir: (True, []))
    monkeypatch.setattr(
        context_checks,
        "load_yaml_document",
        lambda path: {"workstreams": []} if path.name == "workstream_registry.yaml" else {"kpis": []},
    )
    monkeypatch.setattr(context_checks, "emit_context_gaps", lambda **kwargs: None)
    monkeypatch.setattr(context_checks, "load_proposals", lambda *args, **kwargs: pending)
    monkeypatch.setattr(
        context_checks,
        "conflicting_pending_proposals",
        lambda *args, **kwargs: {"conflict-1": ({"proposal_id": "a"}, {"proposal_id": "b"})},
    )

    report = context_checks.run_context_doctor(
        edition_name=None,
        programs_root=tmp_path / "programs",
        editions_root=tmp_path / "editions",
    )

    ncfl_check = next(check for check in report.checks if check.label == "NCFL proposals")
    assert ncfl_check.status == "warn"
    assert "2 pending context proposals" in ncfl_check.detail
    assert "[004, 007]" in ncfl_check.detail
    assert "1 cross-issue conflict key" in ncfl_check.detail


def test_run_context_doctor_flags_stale_pending_ncfl_proposals(monkeypatch, tmp_path: Path) -> None:
    program_dir = tmp_path / "programs" / "alpha"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (program_dir / "workstream_registry.yaml").write_text("workstreams: []\n", encoding="utf-8")
    (program_dir / "kpis.yaml").write_text("kpis: []\n", encoding="utf-8")

    ctx = SimpleNamespace(
        maturity_level=SimpleNamespace(value=2),
        maturity_blockers=(),
        invariant_violations=(),
        staleness_flags=(),
    )
    stale_pending = (
        SimpleNamespace(issue_number=3),
        SimpleNamespace(issue_number=3),
    )
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(context_checks, "load_program_context", lambda *args, **kwargs: ctx)
    monkeypatch.setattr(context_checks, "check_schema_versions", lambda program_dir: (True, []))
    monkeypatch.setattr(
        context_checks,
        "load_yaml_document",
        lambda path: {"workstreams": []} if path.name == "workstream_registry.yaml" else {"kpis": []},
    )
    monkeypatch.setattr(context_checks, "emit_context_gaps", lambda **kwargs: None)
    monkeypatch.setattr(context_checks, "load_proposals", lambda *args, **kwargs: stale_pending + (SimpleNamespace(issue_number=6),))
    monkeypatch.setattr(context_checks, "stale_pending_proposals", lambda *args, **kwargs: stale_pending)
    monkeypatch.setattr(context_checks, "conflicting_pending_proposals", lambda *args, **kwargs: {})
    monkeypatch.setattr(context_checks, "append_context_gap", lambda **kwargs: emitted.append(kwargs))

    report = context_checks.run_context_doctor(
        edition_name=None,
        programs_root=tmp_path / "programs",
        editions_root=tmp_path / "editions",
    )

    ncfl_check = next(check for check in report.checks if check.label == "NCFL proposals")
    assert "2 proposals are stale (>2 issues old) from issue [003]." in ncfl_check.detail
    assert emitted
    assert emitted[0]["field"] == "ncfl.pending_proposals"
    assert "oldest pending issue 003" in str(emitted[0]["message"])
