"""Unit tests for ADF-W0.8 cockpit models, serialization, and the builder."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

import src.core.cockpit_builder as cockpit_builder
from src.core.cockpit_builder import build_cockpit_snapshot
from src.core.cockpit_models import (
    CockpitFinding,
    CockpitSnapshot,
    EconomicsCockpitSummary,
    IntelligenceCockpitSummary,
    ProgramCockpitSummary,
    ReliabilityCockpitSummary,
    SourceCockpitSummary,
    ValueCockpitSummary,
    cockpit_history_filename,
    cockpit_snapshot_from_json_dict,
    cockpit_snapshot_to_json_dict,
    compute_cockpit_input_hash,
    finalize_cockpit_snapshot,
)
from src.core.exceptions import ConfigError
from src.core.models_v2 import RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus

_NOW = datetime(2026, 7, 12, 3, 4, 5, tzinfo=timezone.utc)


@pytest.mark.parametrize("error", [
    ConfigError("Invalid governance mode"), OSError("Configuration read failed"),
    UnicodeError("Invalid encoding"), RuntimeError("Unexpected implementation failure"),
])
def test_config_errors_propagate_before_downstream_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    loader = Mock(side_effect=error)
    downstream = Mock(side_effect=AssertionError("Downstream read must not run after config failure"))
    monkeypatch.setattr(cockpit_builder, "load_arch_data_fix", loader)
    monkeypatch.setattr(cockpit_builder, "_build_economics_summary", downstream)

    with pytest.raises(type(error)) as raised:
        build_cockpit_snapshot("fixture_prog", programs_root=tmp_path, now=_NOW)

    assert raised.value is error
    loader.assert_called_once_with("fixture_prog", programs_root=tmp_path)
    downstream.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("content", [
    b"arch_data_fix: [\n",
    b"- not-a-program-mapping\n",
    b"arch_data_fix:\n  mode: invalid\n",
    b"arch_data_fix:\n  mode: off\n",
    b"arch_data_fix:\n  channels:\n    ado:\n      total_budget_seconds: -1\n",
    b"arch_data_fix:\n  retention:\n    ai_telemetry_days: 1\n",
    b"\xff",
])
def test_malformed_config_never_becomes_governance_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes,
) -> None:
    path = tmp_path / "fixture_prog" / "program.yaml"
    path.parent.mkdir()
    path.write_bytes(content)
    downstream = Mock(side_effect=AssertionError("Downstream read must not run after config failure"))
    monkeypatch.setattr(cockpit_builder, "_build_economics_summary", downstream)

    with pytest.raises(ConfigError):
        build_cockpit_snapshot("fixture_prog", programs_root=tmp_path, now=_NOW)

    downstream.assert_not_called()
    assert path.read_bytes() == content
    assert sorted(p.name for p in path.parent.iterdir()) == ["program.yaml"]


@pytest.mark.parametrize(("content", "off", "required"), [
    (None, True, 0),
    ("", True, 0),
    ("id: fixture_prog\n", True, 0),
    ("arch_data_fix: {}\n", True, 0),
    ('arch_data_fix:\n  mode: "off"\n', True, 0),
    ("arch_data_fix:\n  mode: observe\n  channels:\n    ado:\n      required: true\n", False, 1),
    ("arch_data_fix:\n  mode: enforce\n  channels:\n    ado:\n      required: true\n", False, 1),
])
def test_valid_config_and_documented_defaults_remain_compatible(
    tmp_path: Path, content: str | None, off: bool, required: int,
) -> None:
    if content is not None:
        path = tmp_path / "fixture_prog" / "program.yaml"
        path.parent.mkdir()
        path.write_text(content, encoding="utf-8")

    snapshot = build_cockpit_snapshot("fixture_prog", programs_root=tmp_path, now=_NOW)

    assert any(f.finding_id == "platform.governance.mode_off" for f in snapshot.findings) is off
    assert snapshot.source_summary.required_total == required
    assert snapshot.generated_at == _NOW


def _bare_snapshot(**overrides: object) -> CockpitSnapshot:
    base = CockpitSnapshot(
        schema_version="1",
        program_id="fixture_prog",
        edition_id="fixture_prog_weekly",
        generated_at=_NOW,
        as_of=_NOW,
        program_summary=ProgramCockpitSummary(
            overall_risk="green", readiness_percent=None, blocker_count=0, top_three_candidates=(), next_action=None
        ),
        source_summary=SourceCockpitSummary(
            required_healthy=0, required_total=0, stale_sources=(), degraded_sources=(), manual_sources=(), newest_watermarks={}
        ),
        intelligence_summary=IntelligenceCockpitSummary(
            lineage_coverage=None, verification_coverage=None, extraction_quality=(), contradiction_count=0
        ),
        economics_summary=EconomicsCockpitSummary(
            frontier_avoidance=None, frontier_cost_usd=0.0, cache_hit_rate=None, context_tokens_in=0
        ),
        value_summary=ValueCockpitSummary(metrics=(), time_savings_certification=None),
        reliability_summary=ReliabilityCockpitSummary(
            outbox_pending=0, uncertain_remote_state=0, dead_letter_count=0, duplicate_preventions=0, audit_coverage=None
        ),
        findings=(),
        input_hash="",
    )
    return replace(base, **overrides)


def test_cockpit_finding_rejects_invalid_status() -> None:
    with pytest.raises(ValueError):
        CockpitFinding(
            finding_id="x",
            area="program",
            status="not-a-status",
            summary="s",
            detail="d",
            owner=None,
            next_command=None,
            evidence_refs=(),
            observed_at=_NOW,
        )


def test_cockpit_finding_rejects_invalid_area() -> None:
    with pytest.raises(ValueError):
        CockpitFinding(
            finding_id="x",
            area="not-an-area",
            status="ok",
            summary="s",
            detail="d",
            owner=None,
            next_command=None,
            evidence_refs=(),
            observed_at=_NOW,
        )


def test_serialization_uses_iso8601_and_enum_values() -> None:
    snapshot = finalize_cockpit_snapshot(_bare_snapshot())
    payload = cockpit_snapshot_to_json_dict(snapshot)
    assert payload["generated_at"] == "2026-07-12T03:04:05+00:00"
    assert isinstance(payload["findings"], list)
    assert payload["program_summary"]["overall_risk"] == "green"


def test_input_hash_excludes_generated_at_and_is_stable() -> None:
    snapshot_a = finalize_cockpit_snapshot(_bare_snapshot())
    snapshot_b = finalize_cockpit_snapshot(_bare_snapshot(generated_at=_NOW.replace(hour=9)))
    assert snapshot_a.input_hash == snapshot_b.input_hash


def test_input_hash_changes_when_content_changes() -> None:
    snapshot_a = finalize_cockpit_snapshot(_bare_snapshot())
    snapshot_b = finalize_cockpit_snapshot(
        _bare_snapshot(program_summary=replace(_bare_snapshot().program_summary, overall_risk="red"))
    )
    assert snapshot_a.input_hash != snapshot_b.input_hash


def test_input_hash_matches_direct_computation() -> None:
    snapshot = _bare_snapshot()
    expected = compute_cockpit_input_hash(snapshot)
    finalized = finalize_cockpit_snapshot(snapshot)
    assert finalized.input_hash == expected


def test_history_filename_format() -> None:
    snapshot = _bare_snapshot(generated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    assert cockpit_history_filename(snapshot) == "20260102T030405Z.json"


def test_build_cockpit_snapshot_on_empty_fixture_program_never_raises(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    snapshot = build_cockpit_snapshot("fixture_prog", programs_root=programs_root, now=_NOW)
    assert snapshot.program_id == "fixture_prog"
    assert snapshot.schema_version == "1"
    assert snapshot.program_summary.overall_risk == "green"
    assert snapshot.economics_summary.frontier_cost_usd == 0.0
    assert len(snapshot.input_hash) == 64
    # Every empty/unavailable summary field is backed by an explanatory finding.
    assert any(finding.finding_id == "economics.ai_telemetry.empty" for finding in snapshot.findings)
    assert any(finding.finding_id == "source.health.not_probed" for finding in snapshot.findings)
    assert any(finding.finding_id == "value.metrics.formula_derived_retired" for finding in snapshot.findings)
    assert any(finding.finding_id == "value.report_wall_time.insufficient_history" for finding in snapshot.findings)
    assert any(finding.finding_id == "reliability.outbox.not_wired" for finding in snapshot.findings)
    assert any(finding.finding_id == "intelligence.lineage.no_facts" for finding in snapshot.findings)
    assert snapshot.intelligence_summary.lineage_coverage is None


def test_build_cockpit_snapshot_is_deterministic_for_same_inputs(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    first = build_cockpit_snapshot("fixture_prog", programs_root=programs_root, now=_NOW)
    second = build_cockpit_snapshot("fixture_prog", programs_root=programs_root, now=_NOW)
    assert first.input_hash == second.input_hash


def _risk_entry(risk_id: str, *, status: RiskStatus = RiskStatus.OPEN) -> RiskEntry:
    return RiskEntry(
        id=risk_id, program_id="fixture_prog", title="Synthetic risk", description="Synthetic evidence",
        probability=RiskProbability.LIKELY, impact=RiskImpact.HIGH, category=RiskCategory.TECHNICAL,
        owner_alias="fixture_owner", mitigation_plan=None, mitigation_due_date=None,
        linked_workstream_ids=(), linked_work_item_ids=(), linked_milestone_ids=(),
        linked_claim_ids=(), linked_action_ids=(), status=status, identified_date=_NOW.date(),
        identified_in_vertex_issue=None, last_reviewed_date=None, entity_refs=(),
    )


@pytest.mark.parametrize(
    "error",
    [OSError("private diagnostic"), ConfigError("private diagnostic"), UnicodeError("private diagnostic")],
)
def test_risk_read_failure_is_unknown_not_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fail_read(*args: object, **kwargs: object) -> tuple[RiskEntry, ...]:
        raise error

    monkeypatch.setattr(cockpit_builder, "load_risk_register", fail_read)
    snapshot = build_cockpit_snapshot("fixture_prog", programs_root=tmp_path / "programs", now=_NOW)

    assert snapshot.program_summary.overall_risk == "unknown"
    assert snapshot.program_summary.blocker_count is None
    finding = next(f for f in snapshot.findings if f.finding_id == "program.risk.read_failed")
    assert finding.status == "blocked"
    assert finding.observed_at == _NOW
    assert snapshot.program_summary.next_action == finding.next_command
    assert finding.next_command == "vertex risks list --program fixture_prog"
    assert "private diagnostic" not in str(cockpit_snapshot_to_json_dict(snapshot))
    assert "source.health.not_probed" in {f.finding_id for f in snapshot.findings}


@pytest.mark.parametrize("error", [KeyError("private diagnostic"), TypeError("private diagnostic"), ValueError("private diagnostic")])
def test_failed_risk_score_is_visible_and_retains_other_risk_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    risks = (_risk_entry("broken"), _risk_entry("high"))
    monkeypatch.setattr(cockpit_builder, "load_risk_register", lambda *args, **kwargs: risks)

    def score(risk: RiskEntry) -> int:
        if risk.id == "broken":
            raise error
        return 9

    monkeypatch.setattr(cockpit_builder, "compute_risk_score", score)
    snapshot = build_cockpit_snapshot("fixture_prog", programs_root=tmp_path / "programs", now=_NOW)

    assert snapshot.program_summary.overall_risk == "unknown"
    assert snapshot.program_summary.blocker_count is None
    failed = next(f for f in snapshot.findings if f.finding_id == "program.risk.score_failed")
    assert failed.status == "blocked"
    assert failed.evidence_refs == ("broken",)
    high = next(f for f in snapshot.findings if f.finding_id == "program.risk.high_active_count")
    assert high.evidence_refs == ("high",)
    assert "private diagnostic" not in str(cockpit_snapshot_to_json_dict(snapshot))


@pytest.mark.parametrize("failure", ["read", "score"])
def test_unexpected_risk_failure_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def fail(*args: object, **kwargs: object) -> tuple[RiskEntry, ...]:
        raise RuntimeError("unexpected implementation error")

    monkeypatch.setattr(cockpit_builder, "load_risk_register", lambda *args, **kwargs: (_risk_entry("risk"),))
    monkeypatch.setattr(cockpit_builder, "load_risk_register" if failure == "read" else "compute_risk_score", fail)
    with pytest.raises(RuntimeError, match="unexpected implementation error"):
        build_cockpit_snapshot("fixture_prog", programs_root=tmp_path / "programs", now=_NOW)


@pytest.mark.parametrize(
    ("risks", "expected_risk", "expected_count"),
    [
        ((), "green", 0),
        ((_risk_entry("closed", status=RiskStatus.CLOSED),), "green", 0),
        ((replace(_risk_entry("low"), probability=RiskProbability.UNLIKELY),), "yellow", 0),
        ((_risk_entry("high"),), "red", 1),
        ((_risk_entry("escalated", status=RiskStatus.ESCALATED),), "red", 1),
    ],
)
def test_successful_risk_summary_preserves_existing_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    risks: tuple[RiskEntry, ...], expected_risk: str, expected_count: int,
) -> None:
    monkeypatch.setattr(cockpit_builder, "load_risk_register", lambda *args, **kwargs: risks)
    snapshot = build_cockpit_snapshot("fixture_prog", programs_root=tmp_path / "programs", now=_NOW)
    assert snapshot.program_summary.overall_risk == expected_risk
    assert snapshot.program_summary.blocker_count == expected_count
    assert not any(f.finding_id.endswith("_failed") for f in snapshot.findings)


def test_unknown_risk_count_survives_json_history_and_all_renderers(tmp_path: Path) -> None:
    from src.commands.cockpit import (
        find_nearest_history_snapshot, persist_cockpit_snapshot,
        render_cockpit_comparison, render_cockpit_terminal,
    )
    from src.commands.cockpit_tui import _render_summary
    from src.core.cockpit_html import render_cockpit_html

    earlier = finalize_cockpit_snapshot(_bare_snapshot())
    unknown = finalize_cockpit_snapshot(replace(
        earlier,
        program_summary=replace(earlier.program_summary, overall_risk="unknown", blocker_count=None),
    ))
    payload = cockpit_snapshot_to_json_dict(unknown)
    assert payload["program_summary"]["blocker_count"] is None
    assert cockpit_snapshot_from_json_dict(payload) == unknown
    assert cockpit_snapshot_from_json_dict(cockpit_snapshot_to_json_dict(earlier)) == earlier
    assert unknown.input_hash != earlier.input_hash
    persist_cockpit_snapshot(unknown, programs_root=tmp_path)
    assert find_nearest_history_snapshot("fixture_prog", _NOW, programs_root=tmp_path) == unknown
    assert "UNKNOWN (unknown high-risk item(s))" in render_cockpit_terminal(unknown)
    assert "unknown blocker(s)" in render_cockpit_html(unknown)
    assert "Overall risk: UNKNOWN" in _render_summary(unknown)
    assert "Blocker count:     0 -> unknown" in render_cockpit_comparison(earlier, unknown)


def test_malformed_risk_register_is_unknown_in_cockpit_build_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from typer.testing import CliRunner
    import src.commands.cockpit as cockpit_command

    root = tmp_path / "programs"
    program = root / "fixture_prog"
    program.mkdir(parents=True)
    (program / "risk_register.yaml").write_text("schema_version: [", encoding="utf-8")
    monkeypatch.setattr(cockpit_command, "PROGRAMS_ROOT", root)
    result = CliRunner().invoke(cockpit_command.app, ["build", "--program", "fixture_prog"])

    assert result.exit_code == 0, result.output
    payload = json.loads((program / "runtime" / "cockpit" / "latest.json").read_text(encoding="utf-8"))
    assert payload["program_summary"]["overall_risk"] == "unknown"
    assert payload["program_summary"]["blocker_count"] is None
    assert any(f["finding_id"] == "program.risk.read_failed" for f in payload["findings"])
    assert "UNKNOWN" in (program / "runtime" / "cockpit" / "cockpit.html").read_text(encoding="utf-8")


@pytest.mark.parametrize(("total", "lineaged", "defects", "status"), [(10, 7, 2, "warn"), (20, 18, 1, "info")])
def test_build_cockpit_snapshot_reports_real_lineage_coverage(
    monkeypatch, tmp_path: Path, total: int, lineaged: int, defects: int, status: str,
) -> None:
    """ADF-W2.4/W2.5: lineage_coverage is a real measured ratio, not a
    placeholder, once the fact store has facts."""
    from src.core.fact_lineage_coverage import LineageCoverageReport

    report = LineageCoverageReport(
        program_id="fixture_prog",
        total_count=total,
        lineaged_count=lineaged,
        waived_count=1,
        defect_count=defects,
        sample_defect_natural_keys=("risk:item-1",),
        computed_at=_NOW,
    )
    monkeypatch.setattr("src.core.cockpit_builder.compute_lineage_coverage", lambda *args, **kwargs: report)

    programs_root = tmp_path / "programs"
    snapshot = build_cockpit_snapshot("fixture_prog", programs_root=programs_root, now=_NOW)

    assert snapshot.intelligence_summary.lineage_coverage == lineaged / total
    defect_finding = next(f for f in snapshot.findings if f.finding_id == "intelligence.lineage.defects_present")
    assert defect_finding.status == status
    assert "risk:item-1" in defect_finding.evidence_refs


def test_build_cockpit_snapshot_lineage_ok_status_when_no_defects(monkeypatch, tmp_path: Path) -> None:
    from src.core.fact_lineage_coverage import LineageCoverageReport

    report = LineageCoverageReport(
        program_id="fixture_prog",
        total_count=5,
        lineaged_count=5,
        waived_count=0,
        defect_count=0,
        sample_defect_natural_keys=(),
        computed_at=_NOW,
    )
    monkeypatch.setattr("src.core.cockpit_builder.compute_lineage_coverage", lambda *args, **kwargs: report)

    programs_root = tmp_path / "programs"
    snapshot = build_cockpit_snapshot("fixture_prog", programs_root=programs_root, now=_NOW)

    assert snapshot.intelligence_summary.lineage_coverage == 1.0
    assert any(f.finding_id == "intelligence.lineage.fully_covered" and f.status == "ok" for f in snapshot.findings)


@pytest.mark.parametrize("error", [
    OSError("private diagnostic"), ConfigError("private diagnostic"),
    UnicodeError("private diagnostic"), sqlite3.OperationalError("private diagnostic"),
])
def test_lineage_read_failure_is_blocked_not_empty_and_preserves_known_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    from src.commands.cockpit import render_cockpit_terminal
    from src.core.cockpit_html import render_cockpit_html

    monkeypatch.setattr(cockpit_builder, "compute_lineage_coverage", Mock(side_effect=error))
    monkeypatch.setattr(cockpit_builder, "load_risk_register", Mock(return_value=(_risk_entry("risk-1"),)))
    monkeypatch.setattr(cockpit_builder, "_latest_released_program_synthesis_kwargs", Mock(return_value={
        "program_synthesis_through_line": "Synthetic released synthesis",
        "program_synthesis_generated_at": _NOW.isoformat(),
        "program_synthesis_ai_run_id": "synthetic-run",
    }))

    snapshot = build_cockpit_snapshot("fixture_prog", programs_root=tmp_path, now=_NOW)

    assert snapshot.intelligence_summary.lineage_coverage is None
    assert snapshot.intelligence_summary.program_synthesis_through_line == "Synthetic released synthesis"
    assert snapshot.program_summary.overall_risk == "red"
    assert snapshot.program_summary.blocker_count == 1
    finding = next(f for f in snapshot.findings if f.finding_id == "intelligence.lineage.read_failed")
    assert finding.status == "blocked"
    assert type(error).__name__ in finding.detail
    assert "Inspect" in finding.detail
    assert finding.evidence_refs == ()
    assert finding.observed_at == _NOW
    assert not any(f.finding_id == "intelligence.lineage.no_facts" for f in snapshot.findings)
    payload = cockpit_snapshot_to_json_dict(snapshot)
    assert payload["intelligence_summary"]["lineage_coverage"] is None
    assert cockpit_snapshot_from_json_dict(payload) == snapshot
    terminal = render_cockpit_terminal(snapshot)
    html = render_cockpit_html(snapshot)
    assert "lineage unavailable (read failed)" in terminal
    assert "Program health:        RED (1 high-risk item(s))" in terminal
    assert "finding-blocked" in html
    assert finding.summary in html
    assert finding.detail in html
    assert "private diagnostic" not in str(payload) + terminal + html


def test_malformed_lineage_waiver_is_not_reported_as_no_program_facts(tmp_path: Path) -> None:
    path = tmp_path / "fixture_prog" / "fact_lineage_waivers.yaml"
    path.parent.mkdir()
    content = "schema_version: [\n"
    path.write_text(content, encoding="utf-8")

    snapshot = build_cockpit_snapshot("fixture_prog", programs_root=tmp_path, now=_NOW)

    assert snapshot.intelligence_summary.lineage_coverage is None
    assert any(f.finding_id == "intelligence.lineage.read_failed" and f.status == "blocked" for f in snapshot.findings)
    assert not any(f.finding_id == "intelligence.lineage.no_facts" for f in snapshot.findings)
    assert path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("error", [
    RuntimeError("Implementation defect"), ValueError("Invalid internal value"),
    sqlite3.ProgrammingError("Invalid SQL parameters"),
])
def test_lineage_implementation_errors_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    monkeypatch.setattr(cockpit_builder, "compute_lineage_coverage", Mock(side_effect=error))
    with pytest.raises(type(error)) as raised:
        build_cockpit_snapshot("fixture_prog", programs_root=tmp_path, now=_NOW)
    assert raised.value is error


@pytest.mark.parametrize("arguments", [
    ["show", "--no-persist"],
    ["show", "--format", "json", "--no-persist"],
    ["build"],
    ["explain", "--finding", "intelligence.lineage.read_failed"],
])
def test_fresh_cli_preserves_lineage_failure_distinction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str],
) -> None:
    import json

    from typer.testing import CliRunner

    import src.commands.cockpit as cockpit_command

    program = tmp_path / "fixture_prog"
    program.mkdir()
    waiver = program / "fact_lineage_waivers.yaml"
    waiver.write_text("schema_version: [\n", encoding="utf-8")
    monkeypatch.setattr(cockpit_command, "PROGRAMS_ROOT", tmp_path)
    monkeypatch.setattr(cockpit_command, "_emit_projection_lag_alert_best_effort", Mock())

    result = CliRunner().invoke(cockpit_command.app, [*arguments, "--program", "fixture_prog"])

    assert result.exit_code == 0, result.output
    if arguments[0] == "build":
        directory = program / "runtime" / "cockpit"
        payload = json.loads((directory / "latest.json").read_text(encoding="utf-8"))
        html = (directory / "cockpit.html").read_text(encoding="utf-8")
        assert payload["intelligence_summary"]["lineage_coverage"] is None
        assert any(
            f["finding_id"] == "intelligence.lineage.read_failed" and f["status"] == "blocked"
            for f in payload["findings"]
        )
        assert "Lineage evaluation failed" in html
    elif arguments == ["show", "--no-persist"]:
        assert "lineage unavailable (read failed)" in result.output
    else:
        assert "intelligence.lineage.read_failed" in result.output
        assert "Inspect fact-store access and lineage waivers" in result.output
    assert "No program facts recorded yet" not in result.output
    assert waiver.read_text(encoding="utf-8") == "schema_version: [\n"


def test_failed_lineage_terminal_retains_three_action_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.commands.cockpit import render_cockpit_terminal

    monkeypatch.setattr(cockpit_builder, "compute_lineage_coverage", Mock(side_effect=ConfigError("Invalid waiver")))
    snapshot = build_cockpit_snapshot("fixture_prog", programs_root=tmp_path, now=_NOW)
    failure = next(f for f in snapshot.findings if f.finding_id == "intelligence.lineage.read_failed")
    commands = (
        "vertex cockpit show --program fixture_prog --format json",
        "vertex cockpit explain --program fixture_prog --finding intelligence.lineage.read_failed",
        "vertex cockpit show --program fixture_prog --no-persist",
        "vertex cockpit show --program fixture_prog --no-persist --format json",
    )
    actions = tuple(
        replace(failure, finding_id=f"synthetic.action.{index}", next_command=command)
        for index, command in enumerate(commands)
    )

    terminal = render_cockpit_terminal(replace(snapshot, findings=(failure, *actions)))

    assert "lineage unavailable (read failed)" in terminal
    assert sum(line.startswith("  - ") for line in terminal.splitlines()) == 3
    assert commands[-1] not in terminal
