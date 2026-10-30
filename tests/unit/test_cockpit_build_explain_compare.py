"""ADF-W5.5: `vertex cockpit build/explain/compare` CLI + find_nearest_history_snapshot."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

import src.commands.cockpit as cockpit_module
from src.commands.cockpit import (
    app,
    find_nearest_history_snapshot,
    persist_cockpit_snapshot,
    render_cockpit_comparison,
)
from src.core.cockpit_models import (
    CockpitFinding,
    CockpitSnapshot,
    EconomicsCockpitSummary,
    IntelligenceCockpitSummary,
    ProgramCockpitSummary,
    ReliabilityCockpitSummary,
    SourceCockpitSummary,
    ValueCockpitSummary,
    finalize_cockpit_snapshot,
)
from src.core.exceptions import ConfigError


def _exception_findings() -> tuple[CockpitFinding, ...]:
    return tuple(
        CockpitFinding(
            finding_id=identifier, area="program", status=status,
            summary=identifier, detail="Retained consequence", owner=None,
            next_command=command, evidence_refs=("retained-evidence",), observed_at=_T0,
        )
        for identifier, status, command in (
            ("info-first", "info", "informational-command"),
            ("warn-first", "warn", "warning-command"),
            ("blocked-no-command", "blocked", None),
            ("blocked-first", "blocked", "blocking-command"),
            ("blocked-duplicate", "blocked", "blocking-command"),
            ("warn-second", "warn", "second-warning-command"),
            ("warn-third", "warn", "third-warning-command"),
        )
    )

runner = CliRunner()

_T0 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
_T1 = datetime(2026, 7, 8, 10, 0, 0, tzinfo=timezone.utc)


def _snapshot(*, generated_at: datetime, overall_risk: str = "green", readiness: int = 50,
              findings: tuple[CockpitFinding, ...] = ()) -> CockpitSnapshot:
    snap = CockpitSnapshot(
        schema_version="1", program_id="acme", edition_id="acme_weekly",
        generated_at=generated_at, as_of=generated_at,
        program_summary=ProgramCockpitSummary(
            overall_risk=overall_risk, readiness_percent=readiness, blocker_count=0,
            top_three_candidates=(), next_action=None,
        ),
        source_summary=SourceCockpitSummary(
            required_healthy=5, required_total=5, stale_sources=(), degraded_sources=(),
            manual_sources=(), newest_watermarks={},
        ),
        intelligence_summary=IntelligenceCockpitSummary(
            lineage_coverage=0.5, verification_coverage=0.3, extraction_quality=(), contradiction_count=0
        ),
        economics_summary=EconomicsCockpitSummary(
            frontier_avoidance=0.6, frontier_cost_usd=1.5, cache_hit_rate=0.2, context_tokens_in=100
        ),
        value_summary=ValueCockpitSummary(metrics=(), time_savings_certification=None),
        reliability_summary=ReliabilityCockpitSummary(
            outbox_pending=0, uncertain_remote_state=0, dead_letter_count=0,
            duplicate_preventions=0, audit_coverage=None,
        ),
        findings=findings,
        input_hash="",
    )
    return finalize_cockpit_snapshot(snap)


@pytest.fixture()
def programs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "programs"
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", root)
    return root


# --- find_nearest_history_snapshot ---


def test_returns_none_when_no_history(programs_root: Path) -> None:
    assert find_nearest_history_snapshot("acme", _T0, programs_root=programs_root) is None


def test_finds_the_exact_or_nearest_prior_snapshot(programs_root: Path) -> None:
    persist_cockpit_snapshot(_snapshot(generated_at=_T0), programs_root=programs_root)
    persist_cockpit_snapshot(_snapshot(generated_at=_T1, readiness=90), programs_root=programs_root)

    at_between = _T0 + timedelta(days=3)
    found = find_nearest_history_snapshot("acme", at_between, programs_root=programs_root)
    assert found is not None
    assert found.generated_at == _T0  # nearest AT OR BEFORE, not the later one
    assert found.program_summary.readiness_percent == 50


def test_never_returns_a_snapshot_after_the_requested_time(programs_root: Path) -> None:
    persist_cockpit_snapshot(_snapshot(generated_at=_T1), programs_root=programs_root)
    before_any_history = _T0
    assert find_nearest_history_snapshot("acme", before_any_history, programs_root=programs_root) is None


@pytest.mark.parametrize("format", ["human", "html", "json"])
def test_retained_show_is_non_mutating_and_never_rebuilds(
    programs_root: Path, monkeypatch: pytest.MonkeyPatch, format: str,
) -> None:
    snapshot = _snapshot(generated_at=_T0, findings=_exception_findings())
    persist_cockpit_snapshot(snapshot, programs_root=programs_root)
    persist_cockpit_snapshot(_snapshot(generated_at=_T1), programs_root=programs_root)
    before = {p.relative_to(programs_root): p.read_bytes() for p in programs_root.rglob("*") if p.is_file()}
    forbidden = Mock(side_effect=AssertionError("retained inspection must not mutate or rebuild"))
    for name in (
        "build_cockpit_snapshot", "persist_cockpit_snapshot", "record_adoption",
        "_emit_projection_lag_alert_best_effort", "_emit_lineage_regression_alert_best_effort",
        "_emit_value_baseline_freshness_alert_best_effort",
    ):
        monkeypatch.setattr(cockpit_module, name, forbidden)
    monkeypatch.setattr(cockpit_module.typer, "launch", forbidden)

    result = runner.invoke(app, [
        "show", "--program", "acme", "--as-of", (_T0 + timedelta(days=1)).isoformat(), "--format", format,
    ])

    assert result.exit_code == 0, result.output
    forbidden.assert_not_called()
    if format == "json":
        import json
        assert json.loads(result.output)["input_hash"] == snapshot.input_hash
    else:
        assert "Retained cockpit snapshot" in result.output
        assert "Current source and action state not evaluated" in result.output
        expected = ("blocked-no-command", "blocked-first", "blocked-duplicate", "warn-first", "warn-second", "warn-third")
        # IDs are deliberately distinct from commands; every exception is reachable.
        positions = [result.output.index(identifier) for identifier in expected]
        assert positions == sorted(positions)
        assert "info-first" in result.output
        assert "not available in this retained finding" in result.output
    assert before == {p.relative_to(programs_root): p.read_bytes() for p in programs_root.rglob("*") if p.is_file()}


def test_retained_explain_does_not_read_current_evidence(
    programs_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    persist_cockpit_snapshot(_snapshot(generated_at=_T0, findings=_exception_findings()), programs_root=programs_root)
    builder = Mock(side_effect=AssertionError("no current evidence"))
    monkeypatch.setattr(cockpit_module, "build_cockpit_snapshot", builder)
    result = runner.invoke(app, [
        "explain", "--program", "acme", "--as-of", _T0.isoformat(), "--finding", "warn-first",
    ])
    assert result.exit_code == 0, result.output
    assert "Retained cockpit snapshot" in result.output
    assert "Current source and action state not evaluated" in result.output
    assert "retained-evidence" in result.output
    assert "not available in this retained finding" in result.output
    builder.assert_not_called()


@pytest.mark.parametrize("arguments", [
    ["show", "--format", "html"],
    ["show", "--format", "invalid"],
    ["show", "--as-of", "not-a-date"],
    ["show", "--as-of", "2026-01-02"],
    ["show", "--as-of", _T0.isoformat(), "--program", "..\\other"],
    ["explain", "--finding", "x", "--as-of", "bad"],
])
def test_retained_arguments_reject_before_readers(
    programs_root: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str],
) -> None:
    forbidden = Mock(side_effect=AssertionError("invalid input reached a reader"))
    monkeypatch.setattr(cockpit_module, "build_cockpit_snapshot", forbidden)
    monkeypatch.setattr(cockpit_module, "find_nearest_history_snapshot", forbidden)
    monkeypatch.setattr(cockpit_module, "_is_first_cockpit_run", forbidden)
    result = runner.invoke(app, [*arguments, "--program", "acme"] if "--program" not in arguments else arguments)
    assert result.exit_code == 2, result.output
    forbidden.assert_not_called()
    assert not programs_root.exists()


@pytest.mark.parametrize("damage", ["json", "shape", "identity", "unreadable"])
@pytest.mark.parametrize("command", ["show", "explain", "build", "compare"])
def test_corrupt_selected_history_never_falls_back(
    programs_root: Path, monkeypatch: pytest.MonkeyPatch, damage: str, command: str,
) -> None:
    persist_cockpit_snapshot(_snapshot(generated_at=_T0), programs_root=programs_root)
    _, selected = persist_cockpit_snapshot(_snapshot(generated_at=_T1), programs_root=programs_root)
    if damage == "json":
        selected.write_text("{private-value", encoding="utf-8")
    elif damage == "shape":
        selected.write_text("[]", encoding="utf-8")
    elif damage == "identity":
        selected.write_text(selected.read_text(encoding="utf-8").replace('"acme"', '"other"'), encoding="utf-8")
    else:
        original_read = Path.read_text

        def fail_selected(path: Path, *args: object, **kwargs: object) -> str:
            if path == selected:
                raise PermissionError("private-value")
            return original_read(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", fail_selected)
    args = [command, "--program", "acme"]
    args += (
        ["--from", _T0.isoformat(), "--to", _T1.isoformat()]
        if command == "compare" else ["--as-of", _T1.isoformat()]
    )
    if command == "explain":
        args += ["--finding", "any"]
    result = runner.invoke(app, args)
    assert result.exit_code == 2, result.output
    assert "Cannot read retained cockpit snapshot" in result.output
    assert "private-value" not in result.output
    assert str(programs_root) not in result.output
    assert not (selected.parent.parent / "cockpit.html").exists()


@pytest.mark.parametrize("command", ["show", "explain"])
def test_retained_absence_is_unavailable_without_bootstrap(
    programs_root: Path, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    monkeypatch.setattr(cockpit_module, "build_cockpit_snapshot", Mock(side_effect=AssertionError("no rebuild")))
    args = [command, "--program", "acme", "--as-of", _T0.isoformat()]
    if command == "explain":
        args += ["--finding", "any"]
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert "evidence unavailable" in result.output
    assert not programs_root.exists()


def test_retained_empty_and_unknown_are_not_current_clearance(programs_root: Path) -> None:
    from dataclasses import replace

    snapshot = _snapshot(generated_at=_T0)
    snapshot = replace(snapshot, program_summary=replace(snapshot.program_summary, overall_risk="unknown", blocker_count=None))
    persist_cockpit_snapshot(snapshot, programs_root=programs_root)
    for format in ("human", "html"):
        result = runner.invoke(app, ["show", "--program", "acme", "--as-of", _T0.isoformat(), "--format", format])
        assert result.exit_code == 0, result.output
        assert "No blocked/warn findings in this retained snapshot" in result.output
        assert "unknown" in result.output
        assert "Current source and action state not evaluated" in result.output


def test_retained_selection_uses_only_selected_root_and_recorded_time(
    programs_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    persist_cockpit_snapshot(_snapshot(generated_at=_T0), programs_root=programs_root)
    decoy = tmp_path / "decoy" / "programs"
    persist_cockpit_snapshot(_snapshot(generated_at=_T1), programs_root=decoy)
    original_read = Path.read_text
    read_paths: list[Path] = []

    def selected_only(path: Path, *args: object, **kwargs: object) -> str:
        assert path.is_relative_to(programs_root)
        read_paths.append(path)
        return original_read(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", selected_only)
    result = runner.invoke(app, ["show", "--program", "acme", "--as-of", _T1.isoformat(), "--format", "json"])
    assert result.exit_code == 0, result.output
    assert len(read_paths) == 1
    assert "2026-07-01T10:00:00+00:00" in result.output


@pytest.mark.parametrize("program", ["..", "../outside", "C:\\outside", "CON", "COM1", "trailing."])
def test_retained_rejects_path_bearing_and_reserved_programs(
    programs_root: Path, monkeypatch: pytest.MonkeyPatch, program: str,
) -> None:
    reader = Mock(side_effect=AssertionError("invalid identifier reached reader"))
    monkeypatch.setattr(cockpit_module, "find_nearest_history_snapshot", reader)
    result = runner.invoke(app, ["show", "--program", program, "--as-of", _T0.isoformat()])
    assert result.exit_code == 2, result.output
    reader.assert_not_called()


def test_retained_rejects_relative_root_before_reader(programs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", Path("relative"))
    reader = Mock(side_effect=AssertionError("relative root reached reader"))
    monkeypatch.setattr(cockpit_module, "find_nearest_history_snapshot", reader)
    result = runner.invoke(app, ["show", "--program", "acme", "--as-of", _T0.isoformat()])
    assert result.exit_code == 2, result.output
    reader.assert_not_called()


def test_retained_explain_missing_finding_stays_retained(programs_root: Path) -> None:
    persist_cockpit_snapshot(_snapshot(generated_at=_T0), programs_root=programs_root)
    result = runner.invoke(app, ["explain", "--program", "acme", "--as-of", _T0.isoformat(), "--finding", "missing"])
    assert result.exit_code == 1
    assert "retained" in result.output and "current cockpit" not in result.output


def test_retained_glance_uses_three_distinct_prioritized_commands() -> None:
    snapshot = _snapshot(generated_at=_T0, findings=_exception_findings())
    text = cockpit_module.render_cockpit_terminal(snapshot, retained=True)
    glance = text.split("Next actions:\n")[1].split("\nDetail:")[0]
    assert glance.splitlines() == [
        "  - blocking-command", "  - warning-command", "  - second-warning-command",
    ]


@pytest.mark.parametrize("damage", ["directory", "timestamp"])
def test_retained_invalid_history_is_not_absent(
    programs_root: Path, monkeypatch: pytest.MonkeyPatch, damage: str,
) -> None:
    _, selected = persist_cockpit_snapshot(_snapshot(generated_at=_T0), programs_root=programs_root)
    if damage == "directory":
        original_glob = Path.glob

        def fail_glob(path: Path, pattern: str) -> object:
            if path == selected.parent:
                raise PermissionError("private-value")
            return original_glob(path, pattern)

        monkeypatch.setattr(Path, "glob", fail_glob)
    else:
        selected.write_text(
            selected.read_text(encoding="utf-8").replace(_T0.isoformat(), _T1.isoformat()), encoding="utf-8",
        )
    result = runner.invoke(app, ["show", "--program", "acme", "--as-of", _T0.isoformat()])
    assert result.exit_code == 2, result.output
    assert "Cannot read retained cockpit snapshot" in result.output
    assert "private-value" not in result.output


def test_retained_history_directory_cannot_be_a_file(programs_root: Path) -> None:
    history = programs_root / "acme" / "runtime" / "cockpit" / "history"
    history.parent.mkdir(parents=True)
    history.write_text("invalid history directory", encoding="utf-8")
    result = runner.invoke(app, ["show", "--program", "acme", "--as-of", _T0.isoformat()])
    assert result.exit_code == 2, result.output
    assert "Cannot read retained cockpit snapshot" in result.output


def test_retained_history_selection_ignores_non_timestamp_names_and_is_order_independent(
    programs_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, older = persist_cockpit_snapshot(_snapshot(generated_at=_T0), programs_root=programs_root)
    _, newer = persist_cockpit_snapshot(_snapshot(generated_at=_T1), programs_root=programs_root)
    invalid_name = newer.with_name("not-a-timestamp.json")
    invalid_name.write_text("invalid unrelated file", encoding="utf-8")
    original_glob = Path.glob

    def newest_first(path: Path, pattern: str) -> object:
        if path == newer.parent:
            return iter((invalid_name, newer, older))
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", newest_first)
    selected = find_nearest_history_snapshot("acme", _T1, programs_root=programs_root)
    assert selected is not None and selected.generated_at == _T1


@pytest.mark.parametrize("escape_program", [False, True])
def test_retained_rejects_resolved_paths_outside_selected_root(
    programs_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, escape_program: bool,
) -> None:
    _, selected = persist_cockpit_snapshot(_snapshot(generated_at=_T0), programs_root=programs_root)
    original_resolve = Path.resolve
    escaped = programs_root / "acme" if escape_program else selected

    def resolve_outside(path: Path, *args: object, **kwargs: object) -> Path:
        return tmp_path / "outside" if path == escaped else original_resolve(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "resolve", resolve_outside)
    result = runner.invoke(app, ["show", "--program", "acme", "--as-of", _T0.isoformat()])
    assert result.exit_code == 2, result.output


# --- cockpit build --as-of ---


def test_build_as_of_renders_the_retained_snapshot(programs_root: Path) -> None:
    persist_cockpit_snapshot(_snapshot(generated_at=_T0), programs_root=programs_root)
    result = runner.invoke(app, ["build", "--program", "acme", "--as-of", _T0.isoformat()])
    assert result.exit_code == 0, result.output
    html_path = programs_root / "acme" / "runtime" / "cockpit" / "cockpit.html"
    assert html_path.exists()
    assert "acme" in html_path.read_text(encoding="utf-8")


def test_build_as_of_with_no_history_exits_nonzero(programs_root: Path) -> None:
    result = runner.invoke(app, ["build", "--program", "acme", "--as-of", _T0.isoformat()])
    assert result.exit_code == 1


def test_build_rejects_invalid_as_of(programs_root: Path) -> None:
    result = runner.invoke(app, ["build", "--program", "acme", "--as-of", "not-a-date"])
    assert result.exit_code != 0


@pytest.mark.parametrize("arguments", [
    ["show", "--format", "human"],
    ["show", "--format", "json"],
    ["show", "--no-persist"],
    ["build"],
    ["build", "--open"],
    ["explain", "--finding", "platform.governance.mode_off"],
])
@pytest.mark.parametrize("retained", [False, True])
def test_invalid_config_cannot_replace_or_render_a_fresh_cockpit(
    programs_root: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str], retained: bool,
) -> None:
    if retained:
        persist_cockpit_snapshot(_snapshot(generated_at=_T0), programs_root=programs_root)
        (programs_root / "acme" / "runtime" / "cockpit" / "cockpit.html").write_text(
            "Previous synthetic HTML", encoding="utf-8",
        )
    path = programs_root / "acme" / "program.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("arch_data_fix:\n  mode: invalid\n", encoding="utf-8")
    before = {p.relative_to(programs_root): p.read_bytes() for p in programs_root.rglob("*") if p.is_file()}
    persist = Mock(wraps=persist_cockpit_snapshot)
    browser = Mock()
    monkeypatch.setattr(cockpit_module, "persist_cockpit_snapshot", persist)
    monkeypatch.setattr(cockpit_module.typer, "launch", browser)
    monkeypatch.setattr(cockpit_module, "_emit_projection_lag_alert_best_effort", Mock())

    result = runner.invoke(app, [*arguments, "--program", "acme"])

    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigError)
    assert "arch_data_fix.mode" in str(result.exception)
    assert "governance is off" not in result.output
    assert "Wrote " not in result.output
    persist.assert_not_called()
    browser.assert_not_called()
    after = {p.relative_to(programs_root): p.read_bytes() for p in programs_root.rglob("*") if p.is_file()}
    assert after == before


def test_retained_history_render_does_not_rebuild_invalid_current_config(programs_root: Path) -> None:
    persist_cockpit_snapshot(_snapshot(generated_at=_T0), programs_root=programs_root)
    (programs_root / "acme" / "program.yaml").write_text(
        "arch_data_fix:\n  mode: invalid\n", encoding="utf-8",
    )

    result = runner.invoke(app, ["build", "--program", "acme", "--as-of", _T0.isoformat()])

    assert result.exit_code == 0, result.output
    assert (programs_root / "acme" / "runtime" / "cockpit" / "cockpit.html").exists()


@pytest.mark.parametrize("arguments", [
    ["show", "--format", "human"], ["show", "--format", "json"], ["build"],
    ["explain", "--finding", "intelligence.synthesis.read_failed"],
])
def test_cli_retained_synthesis_failure_is_visible_in_actual_fresh_and_retained_results(
    programs_root: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str],
) -> None:
    import json
    from src.core.program_synthesis import persist_program_synthesis
    from src.core.quality_gates.ai_release_audit import ReleaseTerminal, record_ai_release_decision
    from tests.unit.test_program_synthesis import _synthesis

    synthesis = _synthesis(program_id="acme")
    path = persist_program_synthesis(synthesis, programs_root=programs_root)
    assert path is not None
    record_ai_release_decision(
        program_id="acme", ai_run_id=synthesis.ai_run_id, terminal=ReleaseTerminal.RELEASED,
        reason="synthetic control", validator_finding_count=0, programs_root=programs_root,
    )
    path.write_text("{private retained assessment", encoding="utf-8")
    result = runner.invoke(app, [*arguments, "--program", "acme"])
    assert result.exit_code == 0, result.output
    if arguments == ["build"]:
        html_path = programs_root / "acme" / "runtime" / "cockpit" / "cockpit.html"
        assert "Program synthesis unavailable" in html_path.read_text(encoding="utf-8")
    elif arguments[-1] == "json":
        payload = json.loads(result.output)
        assert payload["intelligence_summary"]["program_synthesis_through_line"] is None
        assert any(f["finding_id"] == "intelligence.synthesis.read_failed" for f in payload["findings"])
    else:
        assert "synthesis unavailable" in result.output
    assert "private" not in result.output
    if arguments != ["build"]:
        assert str(programs_root) not in result.output

    if arguments[0] != "explain":
        monkeypatch.setattr(cockpit_module, "build_cockpit_snapshot", Mock(side_effect=AssertionError("retained must not rebuild")))
        retained = runner.invoke(app, [
            "explain", "--program", "acme", "--finding", "intelligence.synthesis.read_failed",
            "--as-of", (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        ])
        assert retained.exit_code == 0, retained.output
        assert "Program synthesis unavailable" in retained.output
        assert "Current source and action state not evaluated" in retained.output


# --- cockpit compare ---


def test_compare_renders_a_diff(programs_root: Path) -> None:
    persist_cockpit_snapshot(_snapshot(generated_at=_T0, overall_risk="yellow", readiness=40), programs_root=programs_root)
    persist_cockpit_snapshot(_snapshot(generated_at=_T1, overall_risk="green", readiness=90), programs_root=programs_root)

    result = runner.invoke(app, ["compare", "--program", "acme", "--from", _T0.isoformat(), "--to", _T1.isoformat()])
    assert result.exit_code == 0, result.output
    assert "yellow -> green" in result.output
    assert "40 -> 90" in result.output


def test_compare_missing_history_exits_nonzero(programs_root: Path) -> None:
    result = runner.invoke(app, ["compare", "--program", "acme", "--from", _T0.isoformat(), "--to", _T1.isoformat()])
    assert result.exit_code == 1


def test_render_cockpit_comparison_reports_new_and_resolved_findings() -> None:
    earlier = _snapshot(
        generated_at=_T0,
        findings=(CockpitFinding(
            finding_id="f-old", area="program", status="warn", summary="s", detail="d",
            owner=None, next_command=None, evidence_refs=(), observed_at=_T0,
        ),),
    )
    later = _snapshot(
        generated_at=_T1,
        findings=(CockpitFinding(
            finding_id="f-new", area="program", status="warn", summary="s2", detail="d2",
            owner=None, next_command=None, evidence_refs=(), observed_at=_T1,
        ),),
    )
    text = render_cockpit_comparison(earlier, later)
    assert "New findings: f-new" in text
    assert "Resolved/removed findings: f-old" in text


# --- cockpit explain ---


def test_explain_renders_finding_detail(programs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot(
        generated_at=_T0,
        findings=(CockpitFinding(
            finding_id="f1", area="source", status="blocked", summary="ADO is stale",
            detail="No ADO gather in 10 days.", owner="alex", next_command="vertex gather --program acme",
            evidence_refs=("sig-1",), observed_at=_T0,
        ),),
    )
    monkeypatch.setattr(cockpit_module, "build_cockpit_snapshot", lambda *a, **k: snapshot)

    result = runner.invoke(app, ["explain", "--program", "acme", "--finding", "f1"])
    assert result.exit_code == 0, result.output
    assert "ADO is stale" in result.output
    assert "alex" in result.output
    assert "vertex gather --program acme" in result.output
    assert "sig-1" in result.output


def test_explain_unknown_finding_exits_nonzero(programs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot(generated_at=_T0, findings=())
    monkeypatch.setattr(cockpit_module, "build_cockpit_snapshot", lambda *a, **k: snapshot)

    result = runner.invoke(app, ["explain", "--program", "acme", "--finding", "does-not-exist"])
    assert result.exit_code == 1
