from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from src.commands.doctor_checks import context_checks
from src.core.program_context import InvariantSeverity


def _diagnostic_workspace(tmp_path: Path) -> Path:
    from tests.unit.test_program_context import _reviewed_program, _write_yaml

    root = _reviewed_program(tmp_path)
    program_dir = root / "test_prog"
    _write_yaml(program_dir / "scorecards.yaml", {
        "schema_version": "1.0", "scorecards": [], "last_reviewed_date": date.today().isoformat(),
    })
    _write_yaml(program_dir / "editions" / "test_weekly.yaml", {
        "schema_version": "2.0", "id": "test_weekly", "program_id": "test_prog",
        "name": "Test Weekly", "type": "detailed", "altitude": "newsletter", "cadence": "weekly",
    })
    _write_yaml(program_dir / "editorial_rules.yaml", {
        "schema_version": "1.0", "voice_contract": {
            "applies_to_editions": ["test_weekly"],
        }, "last_reviewed_date": date.today().isoformat(),
    })
    return root


def _diagnostic_doctor_cli(monkeypatch, tmp_path: Path, root: Path, format: str):
    from cli import app
    from src.commands import doctor
    from typer.testing import CliRunner

    for name, path in {
        "PROGRAMS_ROOT": root, "EDITIONS_ROOT": root / "test_prog" / "editions",
        "REPORTS_ROOT": tmp_path / "reports", "ARCHIVE_ROOT": tmp_path / "archive",
        "REPO_ROOT": tmp_path,
    }.items():
        monkeypatch.setattr(doctor, name, path)
    return CliRunner().invoke(app, [
        "--no-catchup", "doctor", "--context", "--edition", "test_weekly", "--format", format,
    ])


def test_context_doctor_discloses_absent_optional_registry_without_changing_health(tmp_path: Path) -> None:
    from src.commands.doctor_checks.models import DoctorReport
    from src.core.context_gap_store import load_context_gaps

    root = _diagnostic_workspace(tmp_path)
    program = root / "test_prog"
    registry = program / "workstream_registry.yaml"
    registry.unlink()
    authored = (program / "workstreams.yaml").read_bytes()

    report = context_checks.run_context_doctor(
        edition_name="test_weekly", programs_root=root, editions_root=program / "editions",
    )

    check, = [check for check in report.checks if check.label == "Registry coverage"]
    assert check.status == "info"
    assert check.detail == (
        "Optional workstream_registry.yaml is absent; registry-based workstream gap checks were not evaluated."
    )
    baseline = DoctorReport(report.edition, tuple(c for c in report.checks if c is not check))
    assert (report.warnings, report.failures, report.overall) == (
        baseline.warnings, baseline.failures, baseline.overall,
    )
    assert any("[WS-02]" in check.detail for check in report.checks)
    assert not registry.exists()
    assert (program / "workstreams.yaml").read_bytes() == authored
    assert not any(
        gap.field.startswith("deep_context.")
        for gap in load_context_gaps("test_prog", programs_root=root)
    )


@pytest.mark.parametrize("format", ["human", "json", "csv"])
def test_context_doctor_absent_registry_is_visible_in_each_cli_format(
    monkeypatch, tmp_path: Path, format: str,
) -> None:
    root = _diagnostic_workspace(tmp_path)
    (root / "test_prog" / "workstream_registry.yaml").unlink()

    result = _diagnostic_doctor_cli(monkeypatch, tmp_path, root, format)

    assert result.exit_code == 0, (result.output, result.exception)
    if format == "human":
        assert "INFO Registry coverage" in result.output
        assert "registry-based workstream gap checks were not evaluated" in result.output
    elif format == "json":
        payload = json.loads(result.output)
        check, = [check for check in payload["checks"] if check["label"] == "Registry coverage"]
        assert check["status"] == "info"
        assert "gap checks were not evaluated" in check["detail"]
    else:
        import csv

        rows = list(csv.DictReader(result.output.splitlines()))
        check, = [row for row in rows if row["label"] == "Registry coverage"]
        assert check["status"] == "info"
        assert "gap checks were not evaluated" in check["detail"]
    assert not (root / "test_prog" / "workstream_registry.yaml").exists()


@pytest.mark.parametrize("selected_present", [False, True])
def test_context_doctor_uses_selected_registry_not_ambient_registry(
    monkeypatch, tmp_path: Path, selected_present: bool,
) -> None:
    selected = _diagnostic_workspace(tmp_path / "selected")
    decoy = _diagnostic_workspace(tmp_path / "decoy")
    (decoy if selected_present else selected).joinpath("test_prog", "workstream_registry.yaml").unlink()
    monkeypatch.setattr(context_checks, "PROGRAMS_ROOT", decoy)

    report = context_checks.run_context_doctor(
        edition_name="test_weekly", programs_root=selected, editions_root=selected / "test_prog" / "editions",
    )

    assert any(check.label == "Registry coverage" for check in report.checks) is not selected_present
    assert (selected / "test_prog" / "workstream_registry.yaml").exists() is selected_present


@pytest.mark.parametrize("empty", [False, True])
def test_context_doctor_present_registry_keeps_existing_empty_failure(tmp_path: Path, empty: bool) -> None:
    root = _diagnostic_workspace(tmp_path)
    program = root / "test_prog"
    if empty:
        (program / "workstream_registry.yaml").write_text(
            'schema_version: "1.0"\nworkstreams: []\n', encoding="utf-8",
        )

    report = context_checks.run_context_doctor(
        edition_name="test_weekly", programs_root=root, editions_root=program / "editions",
    )

    assert not any(check.label == "Registry coverage" for check in report.checks)
    if empty:
        check, = [check for check in report.checks if check.label == "Registry" and check.status == "fail"]
        assert "0 active workstreams" in check.detail
        assert "populate" in check.detail.lower() and "manually" in check.detail.lower()
        assert "registry scaffold" not in check.detail


@pytest.mark.parametrize("content", ["workstreams: [\n", "[]\n", "null\n"])
def test_context_doctor_invalid_registry_is_not_reported_as_optional_absence(
    tmp_path: Path, content: str,
) -> None:
    root = _diagnostic_workspace(tmp_path)
    program = root / "test_prog"
    registry = program / "workstream_registry.yaml"
    registry.write_text(content, encoding="utf-8")

    report = context_checks.run_context_doctor(
        edition_name="test_weekly", programs_root=root, editions_root=program / "editions",
    )

    assert report.failures
    assert not any(check.label == "Registry coverage" for check in report.checks)
    assert registry.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("format", ["human", "json", "csv"])
def test_context_diagnostic_evaluation_does_not_add_a_warning(monkeypatch, tmp_path: Path, format: str) -> None:
    root = _diagnostic_workspace(tmp_path)
    report = context_checks.run_context_doctor(
        edition_name="test_weekly", programs_root=root, editions_root=root / "test_prog" / "editions",
    )
    evaluation = next(check for check in report.checks if check.label == "Context evaluation")
    baseline_checks = tuple(check for check in report.checks if check.label != "Context evaluation")
    from src.commands.doctor_checks.models import DoctorReport

    baseline = DoctorReport(report.edition, baseline_checks)
    assert report.warnings == baseline.warnings
    assert report.failures == baseline.failures
    assert report.overall == baseline.overall
    assert evaluation.status == "ok"
    result = _diagnostic_doctor_cli(monkeypatch, tmp_path, root, format)
    assert result.exit_code == (1 if baseline.failures else 0)
    if format == "json":
        payload = json.loads(result.output)
        assert payload["warnings"] == baseline.warnings
        assert payload["failures"] == baseline.failures
        assert payload["overall"] == baseline.overall
    elif format == "human":
        assert "OK   Context evaluation" in result.output
    else:
        import csv

        rows = list(csv.DictReader(result.output.splitlines()))
        row = next(row for row in rows if row["label"] == "Context evaluation")
        assert row["status"] == "ok"
        assert int(row["warnings"]) == baseline.warnings


@pytest.mark.parametrize("format", ["human", "json", "csv"])
def test_context_diagnostic_actual_cli_keeps_evaluation_and_limitations(
    monkeypatch, tmp_path: Path, format: str,
) -> None:
    from src.core.program_context import load_program_context

    root = _diagnostic_workspace(tmp_path)
    path = root / "test_prog" / "program.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace(
        f"last_reviewed_date: '{date.today().isoformat()}'", "last_reviewed_date: not-a-date",
    ), encoding="utf-8")
    compiled = []

    def load(*args, **kwargs):
        ctx = load_program_context(*args, **kwargs)
        compiled.append(ctx)
        return ctx

    monkeypatch.setattr(context_checks, "load_program_context", load)
    result = _diagnostic_doctor_cli(monkeypatch, tmp_path, root, format)

    assert result.exit_code == 0, (result.output, result.exception)
    ctx, = compiled
    assert any(flag.days_stale is None for flag in ctx.staleness_flags)
    assert ctx.loaded_at.isoformat() in result.output
    assert "compatibility ladder" in result.output
    assert "Context trend unknown" in result.output
    assert "unknown" in result.output
    if format == "json":
        payload = json.loads(result.output)
        check = next(item for item in payload["checks"] if item["label"] == "Maturity level")
        metadata = check["metadata"]
        assert metadata["context_evaluated_at"] == ctx.loaded_at.isoformat()
        assert metadata["context_maturity_level"] == ctx.maturity_level.value
        assert metadata["context_invariant_errors"] == ctx.total_invariant_errors
        assert metadata["context_stale_file_count"] == len(ctx.staleness_flags)
        assert all(blocker in metadata["context_limitations"] for blocker in ctx.maturity_blockers)


@pytest.mark.parametrize("format", ["human", "json", "csv"])
def test_context_diagnostic_actual_cli_refuses_read_failure_safely(
    monkeypatch, tmp_path: Path, format: str,
) -> None:
    root = _diagnostic_workspace(tmp_path)
    (root / "test_prog" / "editorial_rules.yaml").write_text("[private-invalid-value]\n", encoding="utf-8")

    result = _diagnostic_doctor_cli(monkeypatch, tmp_path, root, format)

    assert result.exit_code == 1, (result.output, result.exception)
    assert "Context unavailable" in result.output
    assert "private-invalid-value" not in result.output
    assert str(root) not in result.output
    if format == "json":
        check, = json.loads(result.output)["checks"]
        metadata = check["metadata"]
        assert metadata["context_maturity_level"] is None
        assert metadata["context_invariant_errors"] is None
        assert metadata["context_stale_file_count"] is None
        assert metadata["context_evaluated_at"] is None


@pytest.mark.parametrize("error_type", [
    OSError, ValueError, sqlite3.OperationalError, sqlite3.DataError,
])
def test_context_diagnostic_expected_load_failure_is_unavailable(
    monkeypatch, tmp_path: Path, error_type,
) -> None:
    root = _diagnostic_workspace(tmp_path)

    def fail(*args, **kwargs):
        raise error_type("private error payload")

    monkeypatch.setattr(context_checks, "load_program_context", fail)
    report = context_checks.run_context_doctor(
        edition_name=None, programs_root=root, editions_root=root / "test_prog" / "editions",
    )
    check, = report.checks
    assert check.status == "fail"
    assert "Context unavailable" in check.detail
    assert "private error payload" not in check.detail
    assert check.metadata["context_evaluated_at"] is None


@pytest.mark.parametrize("error_type", [
    RuntimeError, TypeError, AttributeError, KeyError, sqlite3.ProgrammingError, sqlite3.DatabaseError,
])
def test_context_diagnostic_unexpected_load_failure_propagates(
    monkeypatch, tmp_path: Path, error_type,
) -> None:
    root = _diagnostic_workspace(tmp_path)

    def fail(*args, **kwargs):
        raise error_type("unexpected")

    monkeypatch.setattr(context_checks, "load_program_context", fail)
    with pytest.raises(error_type):
        context_checks.run_context_doctor(
            edition_name=None, programs_root=root, editions_root=root / "test_prog" / "editions",
        )


@pytest.mark.parametrize("level", [0, 4])
def test_context_diagnostic_measured_zero_is_not_unavailable(monkeypatch, tmp_path: Path, level: int) -> None:
    from src.core.program_context import load_program_context
    from tests.unit.test_program_context import _write_yaml

    root = _diagnostic_workspace(tmp_path)
    if level == 0:
        for filename in ("workstreams.yaml", "workstream_registry.yaml"):
            _write_yaml(root / "test_prog" / filename, {
                "schema_version": "1.0", "workstreams": [], "last_reviewed_date": date.today().isoformat(),
            })
    ctx = load_program_context("test_prog", programs_root=root, raise_on_error=False)
    assert ctx.maturity_level.value == level
    monkeypatch.setattr(context_checks, "load_program_context", lambda *args, **kwargs: ctx)
    report = context_checks.run_context_doctor(
        edition_name=None, programs_root=root, editions_root=root / "test_prog" / "editions",
    )
    check = next(check for check in report.checks if check.label == "Maturity level")
    assert check.metadata["context_maturity_level"] == level
    assert check.metadata["context_invariant_errors"] == 0
    assert check.metadata["context_stale_file_count"] == 0
    assert check.metadata["context_evaluated_at"] == ctx.loaded_at.isoformat()


def test_context_diagnostic_material_gap_and_projection_failure(monkeypatch, tmp_path: Path) -> None:
    from dataclasses import replace
    from src.core.program_context import ContextGapInfo, load_program_context

    root = _diagnostic_workspace(tmp_path)
    ctx = load_program_context("test_prog", programs_root=root, raise_on_error=False)
    ctx = replace(ctx, context_gaps=(
        ContextGapInfo("report", None, "purpose", "quality_degraded", "high", "Purpose missing"),
        ContextGapInfo("gather", "ws1", "owner", "quality_degraded", "medium", "Owner missing"),
    ))
    monkeypatch.setattr(context_checks, "load_program_context", lambda *args, **kwargs: ctx)
    report = context_checks.run_context_doctor(
        edition_name=None, programs_root=root, editions_root=root / "test_prog" / "editions",
    )
    check = next(check for check in report.checks if check.label == "Maturity level")
    assert "report / program / purpose: Purpose missing" in check.metadata["context_limitations"]
    assert "gather / ws1 / owner: Owner missing" in check.metadata["context_limitations"]
    from src.core.program_context import summarize_context_limitations

    assert check.metadata["context_limitations"] == list(summarize_context_limitations(ctx))
    monkeypatch.setattr(context_checks, "load_program_context", lambda *args, **kwargs: object())
    with pytest.raises(AttributeError):
        context_checks.run_context_doctor(
            edition_name=None, programs_root=root, editions_root=root / "test_prog" / "editions",
        )


def test_summarize_context_limitations_preserves_exact_order_without_mutation(tmp_path: Path) -> None:
    from dataclasses import replace
    from src.core.program_context import (
        ContextGapInfo, InvariantViolation, StalenessFlag, load_program_context, summarize_context_limitations,
    )

    root = _diagnostic_workspace(tmp_path)
    ctx = load_program_context("test_prog", programs_root=root, raise_on_error=False)
    ctx = replace(
        ctx, maturity_blockers=("First blocker", "Second blocker"),
        invariant_violations=(
            InvariantViolation("WS-01", InvariantSeverity.ERROR, "workstreams.yaml", None, "Owner absent"),
        ),
        staleness_flags=(
            StalenessFlag("program.yaml", None, "reviewed_at", None, None, 7, 14, "warn"),
            StalenessFlag("registry.yaml", "ws1", "reviewed_at", date(2026, 1, 1), 12, 7, 14, "warn"),
        ),
        context_gaps=(
            ContextGapInfo("report", None, "purpose", "quality_degraded", "high", "Purpose missing"),
            ContextGapInfo("gather", "ws1", "owner", "quality_degraded", "medium", "Owner missing"),
        ),
    )
    before = repr(ctx)
    expected = (
        "First blocker",
        "Second blocker",
        "[WS-01] Owner absent",
        "program.yaml: review age unknown (reviewed_at)",
        "registry.yaml / ws1: 12d stale (reviewed_at)",
        "report / program / purpose: Purpose missing",
        "gather / ws1 / owner: Owner missing",
        "Context compatibility ladder; canonical maturity semantics remain unreconciled.",
        "Context trend unknown; comparable context history is not qualified.",
    )
    assert summarize_context_limitations(ctx) == expected
    assert summarize_context_limitations(ctx) == expected
    assert repr(ctx) == before


@pytest.mark.parametrize("content,repaired,expected", [
    ("{broken retained observation\n", False, None),
    ("mixed-before", False, None),
    ("mixed-after", True, None),
    (None, False, "No context gaps recorded yet."),
    ("", False, "No context gaps recorded yet."),
    ("\n \n", False, "No context gaps recorded yet."),
    ("valid", False, "1 unique gaps across 1 total observations"),
    ("valid", True, "No active context gaps; historical observations retained."),
])
def test_actual_ranked_cli_preserves_gap_read_integrity(
    monkeypatch, tmp_path: Path, content: str | None, repaired: bool, expected: str | None,
) -> None:
    from cli import app
    from src.commands import doctor
    from typer.testing import CliRunner

    programs = tmp_path / "programs"
    program = programs / "sample"
    program.mkdir(parents=True)
    (program / "program.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    value = "{why: Purpose, what: Scope}" if repaired else "{}"
    (program / "workstream_registry.yaml").write_text(
        f"workstreams:\n- id: lane\n  deep_context: {value}\n", encoding="utf-8",
    )
    valid = json.dumps({
        "ts": "2026-10-26T12:00:00Z", "feature": "doctor --context", "program": "sample",
        "lane": "lane", "field": "deep_context.why", "severity": "quality_degraded",
        "message": "Missing purpose", "impact_estimate": "high",
    }) + "\n"
    if content == "valid":
        content = valid
    elif content == "mixed-before":
        content = "{broken retained observation\n" + valid
    elif content == "mixed-after":
        content = valid + "{broken retained blocker\n"
    if content is not None:
        (program / "_feedback").mkdir()
        (program / "_feedback" / "context_gaps.jsonl").write_text(content, encoding="utf-8")
    monkeypatch.setattr(doctor, "PROGRAMS_ROOT", programs)
    monkeypatch.setattr(doctor, "EDITIONS_ROOT", tmp_path / "editions")
    before = {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = CliRunner().invoke(app, ["--no-catchup", "doctor", "--context", "--ranked"])
    assert {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    if expected is None:
        assert result.exit_code == 2, (result.output, result.exception)
        assert "Context gap history is invalid JSON; repair it before retrying." in result.output
        assert "No context gaps" not in result.output
        assert "No active context gaps" not in result.output
        assert "broken retained" not in result.output
        assert str(program) not in result.output
    else:
        assert result.exit_code == 0, (result.output, result.exception)
        assert expected in result.output


@pytest.mark.parametrize("damage", ["null", "array", "missing", "timestamp", "invalid-time", "overflow", "lane"])
def test_actual_ranked_cli_reports_invalid_record_without_partial_output(
    monkeypatch, tmp_path: Path, damage: str,
) -> None:
    from cli import app
    from src.commands import doctor
    from typer.testing import CliRunner

    programs = tmp_path / "programs"
    program = programs / "sample"
    feedback = program / "_feedback"
    feedback.mkdir(parents=True)
    (program / "program.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    valid = {
        "ts": "2026-10-26T12:00:00Z", "feature": "synthetic", "program": "sample",
        "lane": None, "field": "purpose", "severity": "quality_degraded",
        "message": "Valid observation", "impact_estimate": "high",
    }
    damaged = {
        "null": None,
        "array": ["synthetic_canary"],
        "missing": {"message": "synthetic_canary"},
        "timestamp": dict(valid, ts=None),
        "invalid-time": dict(valid, ts="synthetic_canary"),
        "overflow": dict(valid, ts="9999-12-31T23:59:59-11:00"),
        "lane": dict(valid, lane=["synthetic_canary"]),
    }[damage]
    history = feedback / "context_gaps.jsonl"
    history.write_text(json.dumps(valid) + "\n" + json.dumps(damaged) + "\n", encoding="utf-8")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.setattr(doctor, "PROGRAMS_ROOT", programs)
    monkeypatch.setattr(doctor, "EDITIONS_ROOT", tmp_path / "editions")

    result = CliRunner().invoke(app, ["--no-catchup", "doctor", "--context", "--ranked"])

    assert result.exit_code == 2, (result.output, result.exception)
    assert "Context gap history contains an invalid record; repair it before retrying." in result.output
    assert "Valid observation" not in result.output and "synthetic_canary" not in result.output
    assert str(program) not in result.output and "No context gaps" not in result.output
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("history", ["none", "legacy", "unsupported", "why", "what"])
def test_actual_ranked_cli_reports_missing_registry_only_when_projection_requires_it(
    monkeypatch, tmp_path: Path, history: str,
) -> None:
    from cli import app
    from src.commands import doctor
    from typer.testing import CliRunner

    programs = tmp_path / "selected" / "programs"
    program = programs / "sample"
    program.mkdir(parents=True)
    (program / "program.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    if history != "none":
        feedback = program / "_feedback"
        feedback.mkdir()
        (feedback / "context_gaps.jsonl").write_text(json.dumps({
            "ts": "2026-10-26T12:00:00Z",
            "feature": "gather" if history == "legacy" else "doctor --context",
            "program": "sample",
            "lane": "lane",
            "field": {
                "legacy": "deep_context.why",
                "unsupported": "deep_context.how",
                "why": "deep_context.why",
                "what": "deep_context.what",
            }[history],
            "severity": "quality_degraded",
            "message": "Synthetic retained content",
            "impact_estimate": "medium",
        }) + "\n", encoding="utf-8")
    decoy = tmp_path / "decoy" / "sample"
    decoy.mkdir(parents=True)
    (decoy / "workstream_registry.yaml").write_text(
        "workstreams:\n- id: lane\n  deep_context: {why: Decoy, what: Decoy}\n", encoding="utf-8",
    )
    monkeypatch.setattr(context_checks, "PROGRAMS_ROOT", decoy.parent)
    for name, value in {
        "PROGRAMS_ROOT": programs,
        "EDITIONS_ROOT": tmp_path / "selected" / "editions",
        "REPORTS_ROOT": tmp_path / "selected" / "reports",
        "ARCHIVE_ROOT": tmp_path / "selected" / "archive",
        "REPO_ROOT": tmp_path / "selected",
    }.items():
        monkeypatch.setattr(doctor, name, value)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    result = CliRunner().invoke(app, ["--no-catchup", "doctor", "--context", "--ranked"])

    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert not (program / "workstream_registry.yaml").exists()
    if history in {"why", "what"}:
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert (
            "Context gap ranking unavailable: recorded structural gaps require the selected "
            "workstream_registry.yaml, which is missing."
        ) in result.output
        assert "No context gaps" not in result.output and "No active context gaps" not in result.output
        assert "Synthetic retained content" not in result.output and str(program) not in result.output
    else:
        assert result.exit_code == 0, (result.output, result.exception)
        assert "ranking unavailable" not in result.output
        if history == "none":
            assert "No context gaps recorded yet." in result.output
        else:
            assert "1 unique gaps across 1 total observations" in result.output


@pytest.mark.parametrize("registry", [
    "workstreams: [\nsynthetic_canary\n",
    "workstreams: !synthetic_canary []\n",
    "workstreams: synthetic_canary\n",
    "workstreams: [{id: another, deep_context: {}}]\n",
    "workstreams: [{id: lane, deep_context: [synthetic_canary]}]\n",
])
def test_actual_ranked_cli_reports_unusable_registry_without_disclosure(
    monkeypatch, tmp_path: Path, registry: str,
) -> None:
    from cli import app
    from src.commands import doctor
    from typer.testing import CliRunner

    programs = tmp_path / "selected" / "programs"
    program = programs / "sample"
    feedback = program / "_feedback"
    feedback.mkdir(parents=True)
    (program / "program.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (program / "workstream_registry.yaml").write_text(registry, encoding="utf-8")
    (feedback / "context_gaps.jsonl").write_text(json.dumps({
        "ts": "2026-10-26T12:00:00Z", "feature": "doctor --context", "program": "sample",
        "lane": "lane", "field": "deep_context.why", "severity": "quality_degraded",
        "message": "Synthetic retained content", "impact_estimate": "medium",
    }) + "\n", encoding="utf-8")
    decoy = tmp_path / "decoy" / "sample" / "workstream_registry.yaml"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("workstreams: [{id: lane, deep_context: {why: Decoy}}]\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "PROGRAMS_ROOT", programs)
    monkeypatch.setattr(doctor, "EDITIONS_ROOT", tmp_path / "selected" / "editions")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    result = CliRunner().invoke(app, ["--no-catchup", "doctor", "--context", "--ranked"])

    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
    assert (
        "Context gap ranking unavailable: inspect the selected workstream_registry.yaml "
        "and recorded structural gap targets."
    ) in result.output
    assert "synthetic_canary" not in result.output and str(program) not in result.output
    assert "Synthetic retained content" not in result.output
    assert "No context gaps" not in result.output and "No active context gaps" not in result.output
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("history", ["none", "legacy"])
def test_actual_ranked_cli_does_not_read_unused_malformed_registry(monkeypatch, tmp_path: Path, history: str) -> None:
    from cli import app
    from src.commands import doctor
    from typer.testing import CliRunner

    programs = tmp_path / "programs"
    program = programs / "sample"
    program.mkdir(parents=True)
    (program / "program.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    registry = program / "workstream_registry.yaml"
    registry.write_text("workstreams: [\nsynthetic_canary\n", encoding="utf-8")
    if history == "legacy":
        feedback = program / "_feedback"
        feedback.mkdir()
        (feedback / "context_gaps.jsonl").write_text(json.dumps({
            "ts": "2026-10-26T12:00:00Z", "feature": "gather", "program": "sample",
            "lane": "lane", "field": "deep_context.why", "severity": "quality_degraded",
            "message": "Synthetic observation", "impact_estimate": "medium",
        }) + "\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "PROGRAMS_ROOT", programs)
    monkeypatch.setattr(doctor, "EDITIONS_ROOT", tmp_path / "editions")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    result = CliRunner().invoke(app, ["--no-catchup", "doctor", "--context", "--ranked"])

    assert result.exit_code == 0, (result.output, result.exception)
    assert "ranking unavailable" not in result.output
    assert "synthetic_canary" not in result.output
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("feature", ["doctor --context", "gather"])
@pytest.mark.parametrize("count", [1, 2])
def test_actual_ranked_cli_labels_grouped_observations_without_inventing_cycles(
    monkeypatch, tmp_path: Path, feature: str, count: int,
) -> None:
    from cli import app
    from src.commands import doctor
    from typer.testing import CliRunner

    programs = tmp_path / "programs"
    program = programs / "sample"
    feedback = program / "_feedback"
    feedback.mkdir(parents=True)
    (program / "program.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (program / "workstream_registry.yaml").write_text(
        "workstreams:\n- id: lane\n  deep_context: {}\n", encoding="utf-8",
    )
    records = [
        {
            "ts": f"2026-10-{26 + index}T12:00:00Z",
            "feature": feature,
            "program": "sample",
            "lane": "lane",
            "field": "deep_context.why",
            "severity": "quality_degraded",
            "message": "Synthetic observation",
            "impact_estimate": "medium",
        }
        for index in range(count)
    ]
    (feedback / "context_gaps.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "PROGRAMS_ROOT", programs)
    monkeypatch.setattr(doctor, "EDITIONS_ROOT", tmp_path / "editions")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    result = CliRunner().invoke(app, ["--no-catchup", "doctor", "--context", "--ranked"])

    assert result.exit_code == 0, (result.output, result.exception)
    assert f"{count} recorded observation{'s' if count != 1 else ''}" in result.output
    assert "gather cycle" not in result.output
    assert f"1 unique gaps across {count} total observations" in result.output
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("kind", ["other-missing", "unscoped-missing", "permission", "value", "unexpected"])
def test_ranked_gap_projection_propagates_unrelated_failures(monkeypatch, tmp_path: Path, capsys, kind: str) -> None:
    from src.core.context_gap_store import ContextGapRecord

    error = {
        "other-missing": FileNotFoundError(2, "Synthetic missing", str(tmp_path / "other.yaml")),
        "unscoped-missing": FileNotFoundError("Synthetic missing"),
        "permission": PermissionError("Synthetic denied"),
        "value": ValueError("Synthetic invalid"),
        "unexpected": RuntimeError("Synthetic unexpected"),
    }[kind]
    record = ContextGapRecord(
        ts=datetime(2026, 10, 26, tzinfo=timezone.utc), feature="doctor --context",
        program="sample", lane="lane", field="deep_context.why", severity="quality_degraded",
        message="Synthetic observation", impact_estimate="medium",
    )
    monkeypatch.setattr(context_checks, "_resolve_context_program_id", lambda **kwargs: "sample")
    monkeypatch.setattr(context_checks, "load_context_gaps", lambda *args, **kwargs: [record])

    def fail_projection(*args, **kwargs):
        raise error

    monkeypatch.setattr(context_checks, "rank_context_gaps", fail_projection)
    with pytest.raises(type(error)) as raised:
        context_checks.run_ranked_gaps(
            edition_name=None, programs_root=tmp_path / "programs", editions_root=tmp_path / "editions",
        )
    assert raised.value is error
    assert not capsys.readouterr().out


@pytest.mark.parametrize("error", [ValueError("timestamp"), KeyError("field"), OSError("read")])
def test_ranked_gap_syntax_diagnostic_does_not_translate_other_failures(
    monkeypatch, tmp_path: Path, error: Exception, capsys,
) -> None:
    monkeypatch.setattr(context_checks, "_resolve_context_program_id", lambda **kwargs: "sample")

    def fail_read(*args, **kwargs):
        raise error

    monkeypatch.setattr(context_checks, "load_context_gaps", fail_read)
    with pytest.raises(type(error)) as raised:
        context_checks.run_ranked_gaps(
            edition_name=None, programs_root=tmp_path, editions_root=tmp_path,
        )
    assert raised.value is error
    assert not capsys.readouterr().out


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
        loaded_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        context_gaps=(),
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
        loaded_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        context_gaps=(),
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
        loaded_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        context_gaps=(),
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
        loaded_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        context_gaps=(),
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
