from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    emitted_roots: list[Path] = []
    def capture_gap(**kwargs) -> None:
        emitted.append((kwargs["field"], kwargs["lane"], kwargs["impact_estimate"]))
        emitted_roots.append(kwargs["programs_root"])

    monkeypatch.setattr(
        context_checks,
        "append_context_gap",
        capture_gap,
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
    assert emitted_roots and all(root == tmp_path / "programs" for root in emitted_roots)


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


@pytest.mark.parametrize("entity_id", [None, "ws-1"])
def test_context_doctor_displays_unknown_review_age(monkeypatch, tmp_path: Path, entity_id: str | None) -> None:
    program_dir = tmp_path / "programs" / "alpha"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    ctx = SimpleNamespace(
        maturity_level=SimpleNamespace(value=2),
        maturity_blockers=("Review metadata unknown",),
        invariant_violations=(),
        staleness_flags=(SimpleNamespace(
            file="workstreams.yaml", entity_id=entity_id, days_stale=None, field="last_reviewed_date",
        ),),
    )
    monkeypatch.setattr(context_checks, "load_program_context", lambda *args, **kwargs: ctx)
    monkeypatch.setattr(context_checks, "check_schema_versions", lambda path: (True, []))
    monkeypatch.setattr(context_checks, "load_yaml_document", lambda path: {})
    monkeypatch.setattr(context_checks, "emit_context_gaps", lambda **kwargs: None)
    monkeypatch.setattr(context_checks, "load_proposals", lambda *args, **kwargs: ())
    monkeypatch.setattr(context_checks, "conflicting_pending_proposals", lambda *args, **kwargs: {})
    monkeypatch.setattr(context_checks, "stale_pending_proposals", lambda *args, **kwargs: ())

    report = context_checks.run_context_doctor(
        edition_name=None, programs_root=tmp_path / "programs", editions_root=tmp_path / "editions",
    )
    check, = [c for c in report.checks if c.label == "Staleness"]
    assert check.status == "warn"
    assert "unknown" in check.detail.lower()
    assert "Noned" not in check.detail
    assert "workstreams.yaml" in check.detail
    assert "last_reviewed_date" in check.detail


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


def test_detector_forwards_selected_root_and_ranked_gaps_reopen(tmp_path: Path, capsys) -> None:
    from src.core.context_gap_store import load_context_gaps

    programs = tmp_path / "programs"
    program = programs / "sample"
    program.mkdir(parents=True)
    (program / "program.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    registry = program / "workstream_registry.yaml"
    registry.write_text("workstreams:\n- id: lane\n  deep_context: {}\n", encoding="utf-8")
    context_checks.emit_context_gaps(
        program_id="sample", registry_entries=[{"id": "lane", "deep_context": {}}],
        kpis_list=[], today=date(2026, 10, 26), programs_root=programs,
    )
    observations = program / "_feedback" / "context_gaps.jsonl"
    before = observations.read_bytes()
    assert {gap.feature for gap in load_context_gaps("sample", programs_root=programs)} == {"doctor --context"}
    for content, active in [("{}", True), ("{why: Purpose, what: Scope}", False), ("{}", True)]:
        registry.write_text(f"workstreams:\n- id: lane\n  deep_context: {content}\n", encoding="utf-8")
        context_checks.run_ranked_gaps(
            edition_name=None, programs_root=programs, editions_root=tmp_path / "editions",
        )
        output = capsys.readouterr().out
        assert ("deep_context.why" in output) is active
        assert ("deep_context.what" in output) is active
        assert observations.read_bytes() == before


def test_ranked_gaps_without_program_are_nonmutating(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(context_checks, "_resolve_context_program_id", lambda **kwargs: None)
    context_checks.run_ranked_gaps(
        edition_name=None, programs_root=tmp_path / "programs", editions_root=tmp_path / "editions",
    )
    assert not capsys.readouterr().out
    assert not list(tmp_path.iterdir())


def test_ranked_gaps_preserves_unknown_conditions_with_same_impact(monkeypatch, tmp_path: Path, capsys) -> None:
    from datetime import datetime, timezone
    from src.core.context_gap_store import ContextGapRecord

    records = [
        ContextGapRecord(
            ts=datetime(2026, 10, 26, tzinfo=timezone.utc), feature="unknown", program="sample",
            lane=None, field=field, severity="quality_degraded", message="Unknown condition",
            impact_estimate="medium",
        )
        for field in ("one", "two")
    ]
    monkeypatch.setattr(context_checks, "_resolve_context_program_id", lambda **kwargs: "sample")
    monkeypatch.setattr(context_checks, "load_context_gaps", lambda *args, **kwargs: records)
    context_checks.run_ranked_gaps(
        edition_name=None, programs_root=tmp_path / "programs", editions_root=tmp_path / "editions",
    )
    output = capsys.readouterr().out
    assert "one" in output and "two" in output and "2 unique gaps" in output
    assert not list(tmp_path.iterdir())


def test_ranked_gaps_rejects_foreign_observations_before_projection(monkeypatch, tmp_path: Path) -> None:
    from datetime import datetime, timezone
    from src.core.context_gap_store import ContextGapRecord
    from src.core.exceptions import ConfigError

    foreign = ContextGapRecord(
        ts=datetime(2026, 10, 26, tzinfo=timezone.utc), feature="doctor --context", program="other",
        lane="lane", field="deep_context.why", severity="quality_degraded",
        message="Foreign observation", impact_estimate="medium",
    )
    monkeypatch.setattr(context_checks, "_resolve_context_program_id", lambda **kwargs: "sample")
    monkeypatch.setattr(context_checks, "load_context_gaps", lambda *args, **kwargs: [foreign])
    with pytest.raises(ConfigError, match="foreign program"):
        context_checks.run_ranked_gaps(
            edition_name=None, programs_root=tmp_path / "programs", editions_root=tmp_path / "editions",
        )
    assert not list(tmp_path.iterdir())


def test_detector_complete_values_and_invalid_source_date_do_not_add_structural_gaps(
    tmp_path: Path, monkeypatch,
) -> None:
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(context_checks, "append_context_gap", lambda **kwargs: observed.append(kwargs))
    context_checks.emit_context_gaps(
        program_id="sample", registry_entries=[
            {"id": "lane", "deep_context": {"why": "Purpose", "what": "Scope"},
             "workiq_latest": "2026-invalid"},
            {"id": "other", "deep_context": {"why": "Purpose", "what": "Scope"},
             "workiq_latest": "2026-10-26"},
        ], kpis_list=[{"validated": True}, {}], today=date(2026, 10, 26),
        programs_root=tmp_path,
    )
    assert not observed


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


@pytest.mark.parametrize("later_action", ["dismiss", "apply"])
def test_actual_context_doctor_keeps_old_proposal_stale_after_later_decision(
    tmp_path: Path, later_action: str,
) -> None:
    from dataclasses import replace
    import hashlib

    from src.core.ncfl_apply import apply_proposal
    from src.core.ncfl_proposal_store import get_proposals_path, save_proposals, update_proposal_status
    from tests.support.synthetic_report_source import build_synthetic_report_source
    from tests.unit.test_ncfl_flow import _proposal

    root = build_synthetic_report_source(tmp_path / "selected")
    programs = root / "programs"
    authored = programs / "acme" / "assumptions.yaml"
    authored.write_text(
        'schema_version: "1.0"\nassumptions:\n- id: later\n  text: baseline\n'
        '  status: active\n  identified_date: "2026-01-01"\n',
        encoding="utf-8",
    )
    old = _proposal("old", issue_number=1, conflict_key="old", source_value="high")
    later = replace(
        _proposal("later", issue_number=4, conflict_key="assumptions:later:text", source_value="reviewed"),
        target_store="assumptions", target_key="later", target_field="text",
        current_value="baseline", current_value_hash=hashlib.sha256(b"baseline").hexdigest(),
    )
    save_proposals("acme", 1, (old,), programs_root=programs)
    save_proposals("acme", 4, (later,), programs_root=programs)
    old_path = get_proposals_path("acme", 1, programs_root=programs)
    old_bytes = old_path.read_bytes()

    before = context_checks.run_context_doctor(
        edition_name="acme_weekly", programs_root=programs, editions_root=root / "editions",
    )
    before_check = next(check for check in before.checks if check.label == "NCFL proposals")
    assert "1 proposal is stale (>2 issues old) from issue [001]." in before_check.detail
    decided = update_proposal_status(
        "acme", proposal_id="later", issue_number=4, actor="synthetic-reviewer",
        new_status="dismissed" if later_action == "dismiss" else "accepted",
        programs_root=programs,
    )
    if later_action == "apply":
        result = apply_proposal(decided, actor="synthetic-reviewer", programs_root=programs)
        assert result.action == "applied", result.note
        assert "reviewed" in authored.read_text(encoding="utf-8")

    after = context_checks.run_context_doctor(
        edition_name="acme_weekly", programs_root=programs, editions_root=root / "editions",
    )
    after_check = next(check for check in after.checks if check.label == "NCFL proposals")
    assert "1 pending context proposal for issue [001]." in after_check.detail
    assert "1 proposal is stale (>2 issues old) from issue [001]." in after_check.detail
    assert old_path.read_bytes() == old_bytes
