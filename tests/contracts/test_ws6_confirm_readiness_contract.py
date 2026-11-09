"""WS-6 contract tests: doctor --confirm-readiness check.

Verifies that ``run_confirm_readiness_doctor`` correctly:
- Returns FAIL when no overrides file exists
- Returns FAIL when any dimension has 'Needs Input' risk
- Returns OK when overrides have all confirmed risks + fresh gather state
- Returns WARN when gather state is stale
- Returns FAIL when no gather state at all
- Archives info check is present (info-only, not a hard fail)
- run_doctor accepts confirm_readiness=True
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
import yaml
from typer.testing import CliRunner

from src.commands.doctor_checks.confirm_readiness_checks import run_confirm_readiness_doctor
from src.commands.doctor_checks.models import DoctorCheck, DoctorReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_gather_state(program_dir: Path, *, gathered_at: datetime) -> None:
    gs_path = program_dir / "gather_state.json"
    gs_path.parent.mkdir(parents=True, exist_ok=True)
    gs_path.write_text(
        json.dumps({
            "program_id": program_dir.name,
            "gathered_at": gathered_at.isoformat(),
            "scanned_items": 5,
            "discovered_signals": 2,
            "new_signals": 1,
            "pending_review": 0,
            "trajectory_updates": 0,
            "auto_reviews_written": 0,
            "ado_calls": 3,
            "archived_journal_files": 0,
            "background_proposals": 0,
        }),
        encoding="utf-8",
    )


def _write_overrides(program_dir: Path, dimensions: dict[str, str]) -> None:
    overrides_dir = program_dir / "overrides"
    overrides_dir.mkdir(parents=True, exist_ok=True)
    doc = {"dimensions": {name: {"risk": risk} for name, risk in dimensions.items()}}
    (overrides_dir / "issue_001.yaml").write_text(yaml.dump(doc), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_confirm_readiness_fails_no_overrides(tmp_path: Path) -> None:
    """FAIL when there is no overrides file for the program."""
    programs_root = tmp_path / "programs"
    (programs_root / "myprog").mkdir(parents=True)
    now = datetime.now(timezone.utc)
    _write_gather_state(programs_root / "myprog", gathered_at=now)
    report = run_confirm_readiness_doctor(
        edition_name="test_ed",
        program_id="myprog",
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=tmp_path / "archive",
        now=now,
    )
    assert report.failures >= 1
    overrides_check = next(c for c in report.checks if c.label == "Overrides")
    assert overrides_check.status == "fail"


def test_confirm_readiness_fails_needs_input_dimension(tmp_path: Path) -> None:
    """FAIL when any dimension has Needs Input risk."""
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "myprog"
    now = datetime.now(timezone.utc)
    _write_gather_state(prog_dir, gathered_at=now)
    _write_overrides(prog_dir, {"performance": "❓ Needs input", "reliability": "medium"})
    report = run_confirm_readiness_doctor(
        edition_name="test_ed",
        program_id="myprog",
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=tmp_path / "archive",
        now=now,
    )
    assert report.failures >= 1
    overrides_check = next(c for c in report.checks if c.label == "Overrides")
    assert overrides_check.status == "fail"
    assert overrides_check.metadata is not None
    assert "performance" in overrides_check.metadata["needs_input_dimensions"]
    assert overrides_check.metadata["action_category"] == "pm-decision-required"
    assert overrides_check.metadata["owner"] == ["pm"]


def test_confirm_readiness_names_each_nested_scorecard_dimension_needing_risk_input(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "myprog"
    now = datetime.now(timezone.utc)
    _write_gather_state(prog_dir, gathered_at=now)
    overrides_dir = prog_dir / "overrides"
    overrides_dir.mkdir(parents=True, exist_ok=True)
    (overrides_dir / "issue_001.yaml").write_text(
        yaml.safe_dump(
            {
                "scorecards": {
                    "Weekly": {
                        "Runtime": {"risk": "❓ Needs input"},
                        "Buildouts": {"risk": "High"},
                        "Service Fabric": {"risk": "needs_input"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    report = run_confirm_readiness_doctor(
        edition_name="test_ed",
        program_id="myprog",
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=tmp_path / "archive",
        now=now,
    )

    overrides_check = next(c for c in report.checks if c.label == "Overrides")
    assert overrides_check.status == "fail"
    assert overrides_check.metadata is not None
    assert overrides_check.metadata["needs_input_dimensions"] == [
        "Weekly / Runtime",
        "Weekly / Service Fabric",
    ]
    assert str(overrides_dir / "issue_001.yaml") in overrides_check.detail


def test_confirm_readiness_ok_all_confirmed(tmp_path: Path) -> None:
    """OK when overrides have confirmed risks and gather state is fresh."""
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "myprog"
    now = datetime.now(timezone.utc)
    _write_gather_state(prog_dir, gathered_at=now - timedelta(days=1))
    _write_overrides(prog_dir, {"performance": "high", "reliability": "medium"})
    report = run_confirm_readiness_doctor(
        edition_name="test_ed",
        program_id="myprog",
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=tmp_path / "archive",
        cadence="weekly",
        now=now,
    )
    overrides_check = next(c for c in report.checks if c.label == "Overrides")
    gather_check = next(c for c in report.checks if c.label == "Gather State")
    assert overrides_check.status == "ok"
    assert gather_check.status == "ok"
    # No hard failures → overall is ok or warn
    assert report.failures == 0


def test_confirm_readiness_warn_stale_gather(tmp_path: Path) -> None:
    """WARN (not FAIL) when gather state is older than 2× cadence."""
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "myprog"
    now = datetime.now(timezone.utc)
    _write_gather_state(prog_dir, gathered_at=now - timedelta(days=20))  # 20d > 2×7=14
    _write_overrides(prog_dir, {"performance": "high"})
    report = run_confirm_readiness_doctor(
        edition_name="test_ed",
        program_id="myprog",
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=tmp_path / "archive",
        cadence="weekly",
        now=now,
    )
    gather_check = next(c for c in report.checks if c.label == "Gather State")
    assert gather_check.status == "warn"
    assert gather_check.metadata is not None
    assert gather_check.metadata["age_days"] >= 20


def test_confirm_readiness_fails_no_gather_state(tmp_path: Path) -> None:
    """FAIL when there is no gather state at all."""
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "myprog"
    now = datetime.now(timezone.utc)
    _write_overrides(prog_dir, {"performance": "high"})
    # No gather_state.json written
    report = run_confirm_readiness_doctor(
        edition_name="test_ed",
        program_id="myprog",
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=tmp_path / "archive",
        now=now,
    )
    gather_check = next(c for c in report.checks if c.label == "Gather State")
    assert gather_check.status == "fail"


def test_confirm_readiness_archive_info_only(tmp_path: Path) -> None:
    """Archive check is info-only (no failure) when no confirmed issues yet."""
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "myprog"
    now = datetime.now(timezone.utc)
    _write_gather_state(prog_dir, gathered_at=now)
    _write_overrides(prog_dir, {"performance": "high"})
    report = run_confirm_readiness_doctor(
        edition_name="test_ed",
        program_id="myprog",
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=tmp_path / "archive",
        now=now,
    )
    archive_check = next(c for c in report.checks if c.label == "Archive")
    assert archive_check.status == "info"
    # An info check must NOT be counted as a failure
    assert report.failures == 0


def test_confirm_readiness_archive_ok_when_index_exists(tmp_path: Path) -> None:
    """Archive check is OK when archive index has confirmed issues."""
    programs_root = tmp_path / "programs"
    prog_dir = programs_root / "myprog"
    now = datetime.now(timezone.utc)
    _write_gather_state(prog_dir, gathered_at=now)
    _write_overrides(prog_dir, {"performance": "high"})
    # Create a legacy archive index in the format consumed by the archive store.
    archive_dir = tmp_path / "archive" / "test_ed"
    archive_dir.mkdir(parents=True)
    (archive_dir / "index.json").write_text(
        json.dumps({
            "edition": "test_ed",
            "issues": [
                {"issue_number": 1, "generated_at": now.isoformat()},
                {"issue_number": 2, "generated_at": now.isoformat()},
            ],
        }),
        encoding="utf-8",
    )
    report = run_confirm_readiness_doctor(
        edition_name="test_ed",
        program_id="myprog",
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=tmp_path / "archive",
        now=now,
    )
    archive_check = next(c for c in report.checks if c.label == "Archive")
    assert archive_check.status == "ok"
    assert archive_check.metadata is not None
    assert archive_check.metadata["confirmed_count"] == 2


def test_confirm_readiness_run_doctor_has_parameter() -> None:
    """run_doctor must accept confirm_readiness as a keyword argument."""
    import inspect
    from src.commands.doctor import run_doctor

    sig = inspect.signature(run_doctor)
    assert "confirm_readiness" in sig.parameters
    assert sig.parameters["confirm_readiness"].default is False


@pytest.fixture
def readiness_workspace(tmp_path: Path) -> tuple[Path, Path, datetime]:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "myprog"
    now = datetime.now(timezone.utc)
    _write_gather_state(program_dir, gathered_at=now)
    (program_dir / "overrides").mkdir(parents=True)
    return programs_root, program_dir, now


def _readiness_report(
    tmp_path: Path,
    workspace: tuple[Path, Path, datetime],
) -> DoctorReport:
    programs_root, _, now = workspace
    return run_confirm_readiness_doctor(
        edition_name="synthetic_weekly",
        program_id="myprog",
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=tmp_path / "archive",
        now=now,
    )


def _override_check(report: DoctorReport) -> DoctorCheck:
    return next(check for check in report.checks if check.label == "Overrides")


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"scorecards: [synthetic_content\n", id="invalid-yaml"),
        pytest.param(b"scorecards: \xff\n", id="invalid-utf8"),
        pytest.param(b"scorecards: " + b"[" * 2000 + b"]" * 2000, id="excessive-yaml-depth"),
        pytest.param(b"- synthetic_content\n", id="non-mapping-document"),
        pytest.param(b"scorecards: synthetic_content\n", id="non-collection-scorecards"),
        pytest.param(b"scorecards: {Weekly: synthetic_content}\n", id="non-mapping-card"),
        pytest.param(b"scorecards: [synthetic_content]\n", id="non-mapping-list-card"),
        pytest.param(b"scorecards: [{name: Weekly, dimensions: bad}]\n", id="non-list-dimensions"),
        pytest.param(b"scorecards: [{name: Weekly, dimensions: [bad]}]\n", id="non-mapping-dimension"),
        pytest.param(
            b"scorecards: [{name: {synthetic_content: value}, dimensions: [{name: Runtime, risk: unknown}]}]\n",
            id="non-string-card-name",
        ),
        pytest.param(
            b"scorecards: [{name: Weekly, dimensions: [{name: {synthetic_content: value}, risk: unknown}]}]\n",
            id="non-string-dimension-name",
        ),
        pytest.param(
            yaml.safe_dump({"scorecards": {b"synthetic_content": {"Runtime": {"risk": "unknown"}}}}).encode(),
            id="non-string-card-key",
        ),
        pytest.param(
            yaml.safe_dump({"scorecards": {"Weekly": {b"synthetic_content": {"risk": "unknown"}}}}).encode(),
            id="non-string-dimension-key",
        ),
        pytest.param(
            yaml.safe_dump({"dimensions": {b"synthetic_content": {"risk": "unknown"}}}).encode(),
            id="non-string-flat-dimension-key",
        ),
        pytest.param(b"scorecards: {'': {Runtime: low}}\n", id="blank-mapping-card-name"),
        pytest.param(b"scorecards: {' ': {Runtime: low}}\n", id="whitespace-mapping-card-name"),
        pytest.param(b"scorecards: {Weekly: {'': low}}\n", id="blank-mapping-dimension-name"),
        pytest.param(b"scorecards: {Weekly: {' ': low}}\n", id="whitespace-mapping-dimension-name"),
        pytest.param(
            b"scorecards: [{dimensions: [{name: Runtime, risk: low}]}]\n",
            id="missing-list-card-name",
        ),
        pytest.param(
            b"scorecards: [{name: ' ', dimensions: [{name: Runtime, risk: low}]}]\n",
            id="whitespace-list-card-name",
        ),
        pytest.param(
            b"scorecards: [{name: Weekly, dimensions: [{risk: low}]}]\n",
            id="missing-list-dimension-name",
        ),
        pytest.param(
            b"scorecards: [{name: Weekly, dimensions: [{name: ' ', risk: low}]}]\n",
            id="whitespace-list-dimension-name",
        ),
        pytest.param(b"dimensions: {'': low}\n", id="blank-flat-dimension-name"),
        pytest.param(b"dimensions: {' ': low}\n", id="whitespace-flat-dimension-name"),
        pytest.param(
            yaml.safe_dump({"scorecards": {"synthetic_content\x1b[2J": {"Runtime": "unknown"}}}).encode(),
            id="control-mapping-card-name",
        ),
        pytest.param(
            yaml.safe_dump({"scorecards": {"Weekly": {"synthetic_content\nline": "unknown"}}}).encode(),
            id="control-mapping-dimension-name",
        ),
        pytest.param(
            yaml.safe_dump({"scorecards": [{
                "name": "synthetic_content\rline",
                "dimensions": [{"name": "Runtime", "risk": "low"}],
            }]}).encode(),
            id="control-list-card-name",
        ),
        pytest.param(
            yaml.safe_dump({"scorecards": [{
                "name": "Weekly",
                "dimensions": [{"name": "synthetic_content\x1b[2J", "risk": "unknown"}],
            }]}).encode(),
            id="control-list-dimension-name",
        ),
        pytest.param(
            yaml.safe_dump({"dimensions": {"synthetic_content\tcolumn": "unknown"}}).encode(),
            id="control-flat-dimension-name",
        ),
        pytest.param(
            yaml.safe_dump({"scorecards": {"synthetic_content\x9b31m": {"Runtime": "unknown"}}}).encode(),
            id="c1-control-card-name",
        ),
        pytest.param(
            yaml.safe_dump({"dimensions": {"synthetic_content\u2028line": "unknown"}}).encode(),
            id="unicode-line-separator-dimension-name",
        ),
        pytest.param(
            yaml.safe_dump({"dimensions": {"synthetic_content\u2029paragraph": "unknown"}}).encode(),
            id="unicode-paragraph-separator-dimension-name",
        ),
        pytest.param(
            yaml.safe_dump({"dimensions": {"synthetic_content\ud800": "unknown"}}).encode(),
            id="unpaired-surrogate-dimension-name",
        ),
        pytest.param(b"dimensions: [synthetic_content]\n", id="non-mapping-flat-dimensions"),
        pytest.param(
            b"scorecards: {Weekly: {Runtime: {risk: low, eta: synthetic_content}}}\n",
            id="invalid-consumed-override-field",
        ),
        pytest.param(
            b"scorecards: {Weekly: {Runtime: {risk: low, hide_from_scorecard: invalid}}}\n",
            id="invalid-hidden-flag",
        ),
    ],
)
def test_confirm_readiness_reports_unreadable_or_invalid_overrides(
    tmp_path: Path,
    readiness_workspace: tuple[Path, Path, datetime],
    payload: bytes,
) -> None:
    _, program_dir, _ = readiness_workspace
    (program_dir / "overrides" / "issue_001.yaml").write_bytes(payload)

    report = _readiness_report(tmp_path, readiness_workspace)
    check = _override_check(report)

    assert check.status == "fail"
    assert report.failures >= 1
    assert "synthetic_content" not in check.detail
    assert "All dimensions have confirmed" not in check.detail
    assert next(item for item in report.checks if item.label == "Gather State").status == "ok"
    assert (program_dir / "overrides" / "issue_001.yaml").read_bytes() == payload


def test_confirm_readiness_reports_override_read_failure_without_error_details(
    tmp_path: Path,
    readiness_workspace: tuple[Path, Path, datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, program_dir, _ = readiness_workspace
    override_path = program_dir / "overrides" / "issue_001.yaml"
    override_path.write_text("scorecards: {}\n", encoding="utf-8")
    read_text = Path.read_text

    def unreadable(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == override_path:
            raise PermissionError("synthetic_content")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    report = _readiness_report(tmp_path, readiness_workspace)
    check = _override_check(report)

    assert check.status == "fail"
    assert "synthetic_content" not in check.detail
    assert next(item for item in report.checks if item.label == "Gather State").status == "ok"


def test_confirm_readiness_reports_override_enumeration_failure(
    tmp_path: Path,
    readiness_workspace: tuple[Path, Path, datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, program_dir, _ = readiness_workspace
    overrides_dir = program_dir / "overrides"
    glob = Path.glob

    def unreadable(path: Path, pattern: str) -> Any:
        if path == overrides_dir:
            raise PermissionError("synthetic_content")
        return glob(path, pattern)

    monkeypatch.setattr(Path, "glob", unreadable)
    report = _readiness_report(tmp_path, readiness_workspace)
    check = _override_check(report)

    assert check.status == "fail"
    assert "synthetic_content" not in check.detail
    assert next(item for item in report.checks if item.label == "Gather State").status == "ok"


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"null\n",
        b"{}\n",
        b"issue_number: 1\n",
        b"scorecards: null\n",
        b"scorecards: {}\n",
        b"scorecards: []\n",
        b"dimensions: null\n",
        b"dimensions: {}\n",
        b"scorecards: {Weekly: {footnote: synthetic}}\n",
    ],
)
def test_confirm_readiness_warns_when_no_visible_override_risks_are_assessed(
    tmp_path: Path,
    readiness_workspace: tuple[Path, Path, datetime],
    payload: bytes,
) -> None:
    _, program_dir, _ = readiness_workspace
    (program_dir / "overrides" / "issue_001.yaml").write_bytes(payload)

    check = _override_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == "warn"
    assert "All dimensions have confirmed" not in check.detail


def _scorecard_payload(form: str, risk: object) -> dict[str, object]:
    if form == "mapping":
        return {"scorecards": {"Weekly": {"Runtime": {"risk": risk}}}}
    if form == "scalar":
        return {"scorecards": {"Weekly": {"Runtime": risk}}}
    return {
        "scorecards": [
            {"name": "Weekly", "dimensions": [{"name": "Runtime", "risk": risk}]}
        ]
    }


@pytest.mark.parametrize("form", ["mapping", "scalar", "list"])
@pytest.mark.parametrize(
    "risk",
    ["unknown", " UNKNOWN ", "❓ Needs input", "invalid-level", False, 7, None],
)
def test_confirm_readiness_blocks_unresolved_risks_in_supported_scorecards(
    tmp_path: Path,
    readiness_workspace: tuple[Path, Path, datetime],
    form: str,
    risk: object,
) -> None:
    from src.commands.report_scorecards import _resolve_dimension_risk
    from src.core.models import (
        AttributionTier,
        Confidence,
        DimensionRisk,
        EvidencePacket,
        RiskLevel,
    )
    from src.core.overrides_store import load_latest_program_overrides
    from src.core.quality_gates import _evaluate_risk_input_gate

    programs_root, program_dir, _ = readiness_workspace
    (program_dir / "overrides" / "issue_001.yaml").write_text(
        yaml.safe_dump(_scorecard_payload(form, risk), allow_unicode=True),
        encoding="utf-8",
    )
    document = load_latest_program_overrides("myprog", programs_root=programs_root)
    assert document is not None
    override = document.scorecards[0].dimensions[0]
    effective_risk = _resolve_dimension_risk(RiskLevel.UNKNOWN, override)
    evidence = EvidencePacket(0, (), (), (), Confidence.NONE, AttributionTier.TIER3, "")
    gate = _evaluate_risk_input_gate((DimensionRisk("Runtime", effective_risk, "", evidence),))
    assert not gate.passed

    check = _override_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == "fail"
    assert check.metadata is not None
    assert check.metadata["needs_input_dimensions"] == ["Weekly / Runtime"]


@pytest.mark.parametrize("form", ["mapping", "scalar", "list"])
@pytest.mark.parametrize("risk", ["blocked", "High", " MEDIUM ", "low", "done"])
def test_confirm_readiness_accepts_known_risks_in_supported_scorecards(
    tmp_path: Path,
    readiness_workspace: tuple[Path, Path, datetime],
    form: str,
    risk: str,
) -> None:
    _, program_dir, _ = readiness_workspace
    (program_dir / "overrides" / "issue_001.yaml").write_text(
        yaml.safe_dump(_scorecard_payload(form, risk)),
        encoding="utf-8",
    )

    check = _override_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == "ok"
    assert "All dimensions have confirmed" not in check.detail


@pytest.mark.parametrize("form", ["mapping", "list", "flat"])
def test_confirm_readiness_preserves_nonblank_names_with_whitespace(
    tmp_path: Path, readiness_workspace: tuple[Path, Path, datetime], form: str,
) -> None:
    _, program_dir, _ = readiness_workspace
    card_name = "  Weekly program  "
    dimension_name = "  Runtime health  "
    if form == "mapping":
        payload = {"scorecards": {card_name: {dimension_name: "unknown"}}}
    elif form == "list":
        payload = {"scorecards": [{
            "name": card_name,
            "dimensions": [{"name": dimension_name, "risk": "unknown"}],
        }]}
    else:
        payload = {"dimensions": {dimension_name: "unknown"}}
    (program_dir / "overrides" / "issue_001.yaml").write_text(
        yaml.safe_dump(payload), encoding="utf-8",
    )

    check = _override_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == "fail"
    assert check.metadata is not None
    assert check.metadata["needs_input_dimensions"] == [
        dimension_name if form == "flat" else f"{card_name} / {dimension_name}",
    ]


def test_confirm_readiness_preserves_unicode_names_without_terminal_controls(
    tmp_path: Path, readiness_workspace: tuple[Path, Path, datetime],
) -> None:
    _, program_dir, _ = readiness_workspace
    card_name = "\u9031\u6b21\u00a0status"
    dimension_name = "\u72b6\u6cc1 \U0001f469\u200d\U0001f4bb"
    (program_dir / "overrides" / "issue_001.yaml").write_text(
        yaml.safe_dump({"scorecards": {card_name: {dimension_name: "unknown"}}}),
        encoding="utf-8",
    )

    check = _override_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == "fail"
    assert check.metadata is not None
    assert check.metadata["needs_input_dimensions"] == [f"{card_name} / {dimension_name}"]


def test_confirm_readiness_does_not_change_override_loader_name_compatibility(
    tmp_path: Path, readiness_workspace: tuple[Path, Path, datetime],
) -> None:
    from src.core.overrides_store import load_latest_program_overrides

    programs_root, program_dir, _ = readiness_workspace
    (program_dir / "overrides" / "issue_001.yaml").write_text(
        "scorecards: {'': {Runtime: low}}\n", encoding="utf-8",
    )

    document = load_latest_program_overrides("myprog", programs_root=programs_root)
    assert document is not None
    assert document.scorecards[0].name == ""
    assert _override_check(_readiness_report(tmp_path, readiness_workspace)).status == "fail"


@pytest.mark.parametrize("form", ["mapping", "scalar"])
def test_confirm_readiness_checks_legacy_flat_risk_values(
    tmp_path: Path,
    readiness_workspace: tuple[Path, Path, datetime],
    form: str,
) -> None:
    _, program_dir, _ = readiness_workspace
    value = {"risk": "invalid-level"} if form == "mapping" else "invalid-level"
    (program_dir / "overrides" / "issue_001.yaml").write_text(
        yaml.safe_dump({"dimensions": {"Runtime": value}}),
        encoding="utf-8",
    )

    check = _override_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == "fail"
    assert check.metadata is not None
    assert check.metadata["needs_input_dimensions"] == ["Runtime"]


@pytest.mark.parametrize(
    ("legacy_risk", "scorecard_risk", "expected_status"),
    [("low", "unknown", "fail"), ("unknown", "low", "ok")],
)
def test_confirm_readiness_prefers_program_scorecards_to_legacy_dimensions(
    tmp_path: Path,
    readiness_workspace: tuple[Path, Path, datetime],
    legacy_risk: str,
    scorecard_risk: str,
    expected_status: str,
) -> None:
    _, program_dir, _ = readiness_workspace
    payload = {
        "dimensions": {"Legacy": {"risk": legacy_risk}},
        "scorecards": {"Weekly": {"Runtime": {"risk": scorecard_risk}}},
    }
    (program_dir / "overrides" / "issue_001.yaml").write_text(
        yaml.safe_dump(payload), encoding="utf-8",
    )

    check = _override_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == expected_status


@pytest.mark.parametrize("include_visible", [False, True])
def test_confirm_readiness_excludes_hidden_scorecard_dimensions(
    tmp_path: Path,
    readiness_workspace: tuple[Path, Path, datetime],
    include_visible: bool,
) -> None:
    _, program_dir, _ = readiness_workspace
    scorecard: dict[str, object] = {
        "Hidden": {"risk": None, "hide_from_scorecard": True},
    }
    if include_visible:
        scorecard["Visible"] = {"risk": "low"}
    (program_dir / "overrides" / "issue_001.yaml").write_text(
        yaml.safe_dump({"scorecards": {"Weekly": scorecard}}), encoding="utf-8",
    )

    check = _override_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == ("ok" if include_visible else "warn")


def test_confirm_readiness_selects_the_same_latest_numeric_issue_as_the_override_loader(
    tmp_path: Path,
    readiness_workspace: tuple[Path, Path, datetime],
) -> None:
    from src.core.overrides_store import load_latest_program_overrides

    programs_root, program_dir, _ = readiness_workspace
    overrides_dir = program_dir / "overrides"
    for filename, risk in (
        ("issue_999.yaml", "low"),
        ("issue_1000.yaml", "unknown"),
        ("issue_notes.yaml", "low"),
    ):
        (overrides_dir / filename).write_text(
            yaml.safe_dump(_scorecard_payload("mapping", risk)), encoding="utf-8",
        )
    document = load_latest_program_overrides("myprog", programs_root=programs_root)
    assert document is not None
    assert document.scorecards[0].dimensions[0].risk is None

    check = _override_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == "fail"
    assert check.metadata is not None
    assert Path(check.metadata["overrides_path"]).name == "issue_1000.yaml"


def test_confirm_readiness_rejects_missing_canonical_issue_after_numeric_selection(
    tmp_path: Path,
    readiness_workspace: tuple[Path, Path, datetime],
) -> None:
    _, program_dir, _ = readiness_workspace
    (program_dir / "overrides" / "issue_0001.yaml").write_text(
        yaml.safe_dump(_scorecard_payload("mapping", "low")), encoding="utf-8",
    )

    check = _override_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == "fail"


@pytest.mark.parametrize("output_format", ["human", "json", "csv"])
@pytest.mark.parametrize(
    ("payload", "expected_exit", "expected_text"),
    [
        ("scorecards: [synthetic_content\n", 1, "could not be read or validated"),
        ("scorecards: [{dimensions: [{name: Runtime, risk: low}]}]\n", 1, "could not be read or validated"),
        (
            'scorecards: {"synthetic_content\\u001b[2J": {Runtime: unknown}}\n',
            1,
            "could not be read or validated",
        ),
        ("scorecards: {Weekly: {Runtime: {risk: invalid-level}}}\n", 1, "Weekly / Runtime"),
        ("scorecards: [{name: Weekly, dimensions: [{name: Runtime, risk: unknown}]}]\n", 1, "Weekly / Runtime"),
        ("scorecards: {}\n", 0, "No visible risk overrides"),
        ("scorecards: {Weekly: {Runtime: {risk: low}}}\n", 0, "No unresolved visible risk"),
    ],
)
def test_confirm_readiness_cli_reports_override_status_safely(
    tmp_path: Path,
    readiness_workspace: tuple[Path, Path, datetime],
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
    payload: str,
    expected_exit: int,
    expected_text: str,
) -> None:
    import src.commands.doctor as doctor

    programs_root, program_dir, _ = readiness_workspace
    (program_dir / "overrides" / "issue_001.yaml").write_text(
        payload, encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(doctor, "REPORTS_ROOT", tmp_path / "reports")
    monkeypatch.setattr(doctor, "ARCHIVE_ROOT", tmp_path / "archive")
    monkeypatch.setattr(doctor, "EDITIONS_ROOT", tmp_path / "editions")
    monkeypatch.setattr(doctor, "_resolve_edition_paths", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        doctor,
        "resolve_edition",
        lambda *args, **kwargs: SimpleNamespace(
            program=SimpleNamespace(id="myprog"),
            edition=SimpleNamespace(cadence="weekly"),
        ),
    )
    app = typer.Typer()
    app.command()(doctor.doctor_command)

    result = CliRunner().invoke(
        app, ["--edition", "synthetic_weekly", "--confirm-readiness", "--format", output_format],
    )

    assert result.exit_code == expected_exit, result.output
    assert "synthetic_content" not in result.output
    assert "Overrides" in result.output
    assert expected_text in result.output


def test_confirm_readiness_cli_help_does_not_claim_confirmation_is_guaranteed() -> None:
    import inspect

    import src.commands.doctor as doctor

    option = inspect.signature(doctor.doctor_command).parameters["confirm_readiness"].default
    assert "Returns 0 only when confirm would succeed" not in option.help

    app = typer.Typer()
    app.command()(doctor.doctor_command)
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0


def _write_readiness_edition(
    tmp_path: Path, program_dir: Path, *, cadence: str = "weekly", legacy: bool = False,
) -> None:
    edition_dir = tmp_path / "editions" if legacy else program_dir / "editions"
    edition_dir.mkdir(parents=True, exist_ok=True)
    (edition_dir / "synthetic_weekly.yaml").write_text(
        yaml.safe_dump({
            "id": "synthetic_weekly",
            "program_id": program_dir.name,
            "name": "Synthetic edition",
            "type": "detailed",
            "altitude": "detailed",
            "cadence": cadence,
        }),
        encoding="utf-8",
    )
    (program_dir / "program.yaml").write_text(
        f"id: {program_dir.name}\nname: Synthetic program\n", encoding="utf-8",
    )
    (program_dir / "workstreams.yaml").write_text("workstreams: []\n", encoding="utf-8")
    (program_dir / "scorecards.yaml").write_text("scorecards: []\n", encoding="utf-8")


def _archive_check(report: DoctorReport) -> DoctorCheck:
    return next(check for check in report.checks if check.label == "Archive")


def _gather_check(report: DoctorReport) -> DoctorCheck:
    return next(check for check in report.checks if check.label == "Gather State")


def _link_directory(link: Path, target: Path) -> None:
    import os
    import subprocess

    if os.name == "nt":
        subprocess.run(
            [os.environ["COMSPEC"], "/c", "mklink", "/J", str(link), str(target)],
            check=True, capture_output=True,
        )
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize(
    ("cadence", "age_days", "expected_status"),
    [
        ("daily", 2, "ok"),
        ("daily", 3, "warn"),
        ("weekly", 3, "ok"),
        ("biweekly", 20, "ok"),
        ("monthly", 30, "ok"),
        ("quarterly", 100, "ok"),
        ("ad-hoc", 20, "warn"),
    ],
)
def test_confirm_readiness_uses_resolved_cadence_and_injected_time(
    tmp_path: Path, cadence: str, age_days: int, expected_status: str,
) -> None:
    from src.commands.doctor import run_doctor

    programs_root = tmp_path / "programs"
    program_dir = programs_root / "myprog"
    program_dir.mkdir(parents=True)
    _write_readiness_edition(tmp_path, program_dir, cadence=cadence)
    now = datetime(2030, 5, 1, tzinfo=timezone.utc)
    _write_gather_state(program_dir, gathered_at=now - timedelta(days=age_days))
    _write_overrides(program_dir, {"health": "low"})

    report = run_doctor(
        edition_name="synthetic_weekly",
        confirm_readiness=True,
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=tmp_path / "archive",
        reports_root=tmp_path / "reports",
        now=now,
    )

    check = _gather_check(report)
    assert check.status == expected_status
    assert check.metadata is not None
    assert check.metadata["age_days"] == age_days


@pytest.mark.parametrize(
    "payload",
    [
        b'{"gathered_at": "synthetic_private_content"',
        b"[]",
        b'{"gathered_at": "not-a-date"}',
        b'{"program_id": "synthetic_private_content", "gathered_at": "2030-01-01T00:00:00Z"}',
        b"\xff",
        b'{"gathered_at": "2030-01-01T00:00:00Z", "scanned_items": 1e309}',
        b'{"gathered_at": "2030-01-01T00:00:00Z", "scanned_items": ' + b"9" * 5000 + b"}",
        b'{"gathered_at": "2030-01-01T00:00:00Z", "channels": ' + b"[" * 1100 + b"0" + b"]" * 1100 + b"}",
    ],
)
def test_confirm_readiness_reports_invalid_gather_state_safely(
    tmp_path: Path, readiness_workspace: tuple[Path, Path, datetime], payload: bytes,
) -> None:
    _, program_dir, _ = readiness_workspace
    _write_overrides(program_dir, {"health": "low"})
    state_path = program_dir / "gather_state.json"
    state_path.write_bytes(payload)

    report = _readiness_report(tmp_path, readiness_workspace)

    check = _gather_check(report)
    assert check.status == "fail"
    assert "synthetic_private_content" not in repr(check)
    assert _override_check(report).status == "ok"
    assert _archive_check(report).status == "info"
    assert state_path.read_bytes() == payload


def test_confirm_readiness_reports_gather_read_failure_safely(
    tmp_path: Path,
    readiness_workspace: tuple[Path, Path, datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, program_dir, _ = readiness_workspace
    state_path = program_dir / "gather_state.json"
    read_text = Path.read_text

    def unreadable(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == state_path:
            raise PermissionError("synthetic_private_content")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    report = _readiness_report(tmp_path, readiness_workspace)

    assert _gather_check(report).status == "fail"
    assert "synthetic_private_content" not in repr(report)
    assert _archive_check(report).status == "info"


@pytest.mark.parametrize("layout", ["canonical", "legacy-edition", "legacy-archive"])
def test_confirm_readiness_counts_selected_archive_confirmations(
    tmp_path: Path, readiness_workspace: tuple[Path, Path, datetime], layout: str,
) -> None:
    from src.core.archive_store import read_archive_index

    programs_root, program_dir, now = readiness_workspace
    _write_overrides(program_dir, {"health": "low"})
    if layout != "legacy-archive":
        _write_readiness_edition(tmp_path, program_dir, legacy=layout == "legacy-edition")
        archive_dir = program_dir / "archive" / "synthetic_weekly"
    else:
        archive_dir = tmp_path / "archive" / "synthetic_weekly"
    archive_dir.mkdir(parents=True)
    entries = [
        {"issue_number": 1, "generated_at": now.isoformat(), "kind": "confirmed"},
        {"issue_number": 2, "generated_at": now.isoformat(), "kind": "skipped"},
        {"issue_number": 3, "generated_at": now.isoformat(), "kind": "confirmed"},
    ]
    index_path = archive_dir / "index.json"
    index_path.write_text(
        json.dumps({"edition": "synthetic_weekly", "issues": entries}), encoding="utf-8",
    )
    decoy_dir = tmp_path / "archive" / "synthetic_weekly"
    decoy_dir.mkdir(parents=True, exist_ok=True)
    (decoy_dir / "archive_index.json").write_text(
        json.dumps({"issues": [{"issue_number": 99}]}), encoding="utf-8",
    )
    expected_index = read_archive_index(
        "synthetic_weekly",
        archive_root=tmp_path / "archive",
        programs_root=programs_root if layout == "canonical" else None,
    )
    before = index_path.read_bytes()

    check = _archive_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == "ok"
    assert check.metadata is not None
    assert check.metadata["confirmed_count"] == sum(
        entry.kind == "confirmed" for entry in expected_index.issues
    ) == 2
    assert index_path.read_bytes() == before


@pytest.mark.parametrize("entries", [[], [{"issue_number": 1, "generated_at": "2030-01-01T00:00:00Z", "kind": "skipped"}]])
def test_confirm_readiness_empty_or_skipped_only_archive_is_informational(
    tmp_path: Path, readiness_workspace: tuple[Path, Path, datetime], entries: list[dict[str, Any]],
) -> None:
    _, program_dir, _ = readiness_workspace
    _write_readiness_edition(tmp_path, program_dir)
    archive_dir = program_dir / "archive" / "synthetic_weekly"
    archive_dir.mkdir(parents=True)
    (archive_dir / "index.json").write_text(
        json.dumps({"edition": "synthetic_weekly", "issues": entries}), encoding="utf-8",
    )

    check = _archive_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == "info"
    assert "No confirmed" in check.detail


def test_confirm_readiness_absent_archive_index_is_informational(
    tmp_path: Path, readiness_workspace: tuple[Path, Path, datetime],
) -> None:
    _, program_dir, _ = readiness_workspace
    _write_readiness_edition(tmp_path, program_dir)

    check = _archive_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == "info"


@pytest.mark.parametrize(
    ("kind", "expected_status"),
    [("CONFIRMED", "ok"), (" confirmed ", "ok"), ("SKIPPED", "info"), (" skipped ", "info")],
)
def test_confirm_readiness_preserves_valid_archive_kind_normalization(
    tmp_path: Path, readiness_workspace: tuple[Path, Path, datetime], kind: str, expected_status: str,
) -> None:
    _, program_dir, now = readiness_workspace
    _write_readiness_edition(tmp_path, program_dir)
    archive_dir = program_dir / "archive" / "synthetic_weekly"
    archive_dir.mkdir(parents=True)
    (archive_dir / "index.json").write_text(json.dumps({
        "issues": [{"issue_number": 1, "generated_at": now.isoformat(), "kind": kind}],
    }), encoding="utf-8")

    check = _archive_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == expected_status


@pytest.mark.parametrize("kind", ["synthetic_private_content", "", None, True, 0, [], {}])
def test_confirm_readiness_does_not_count_an_invalid_archive_kind(
    tmp_path: Path, readiness_workspace: tuple[Path, Path, datetime], kind: object,
) -> None:
    _, program_dir, now = readiness_workspace
    _write_readiness_edition(tmp_path, program_dir)
    archive_dir = program_dir / "archive" / "synthetic_weekly"
    archive_dir.mkdir(parents=True)
    index_path = archive_dir / "index.json"
    payload = json.dumps({
        "issues": [{"issue_number": 1, "generated_at": now.isoformat(), "kind": kind}],
    }).encode("utf-8")
    index_path.write_bytes(payload)

    check = _archive_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == "warn"
    assert check.metadata is None or "confirmed_count" not in check.metadata
    assert "synthetic_private_content" not in repr(check)
    assert index_path.read_bytes() == payload


def test_confirm_readiness_describes_archive_entries_without_claiming_integrity(
    tmp_path: Path, readiness_workspace: tuple[Path, Path, datetime],
) -> None:
    _, program_dir, now = readiness_workspace
    _write_readiness_edition(tmp_path, program_dir)
    archive_dir = program_dir / "archive" / "synthetic_weekly"
    archive_dir.mkdir(parents=True)
    entry = {"issue_number": 1, "generated_at": now.isoformat(), "kind": "confirmed"}
    (archive_dir / "index.json").write_text(
        json.dumps({"issues": [entry, entry]}), encoding="utf-8",
    )

    check = _archive_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == "ok"
    assert "Confirmed archive entries: 2" in check.detail
    assert "archive integrity was not assessed" in check.detail


def test_confirm_readiness_recognizes_an_actual_skipped_issue(
    tmp_path: Path, readiness_workspace: tuple[Path, Path, datetime],
) -> None:
    from src.core.archive_store import write_skipped_issue

    programs_root, program_dir, now = readiness_workspace
    _write_readiness_edition(tmp_path, program_dir)
    index_path = write_skipped_issue(
        "synthetic_weekly", 1, "Synthetic skip", archive_root=tmp_path / "archive",
        programs_root=programs_root, generated_at=now,
    )
    before = index_path.read_bytes()

    check = _archive_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == "info"
    assert index_path.read_bytes() == before


@pytest.mark.parametrize(
    "payload",
    [
        b'{"issues": "synthetic_private_content"',
        b"[]",
        b"null",
        b"{}",
        b'{"issues": {}}',
        b'{"issues": ""}',
        b'{"issues": null}',
        b'{"issues": [null]}',
        b'{"issues": [{"issue_number": 1}]}',
        b'{"issues": [{"issue_number": "synthetic_private_content", "generated_at": "2030-01-01T00:00:00Z"}]}',
        b'{"edition": "other", "issues": []}',
        b"\xff",
        b'{"issues": ' + b"[" * 1100 + b"0" + b"]" * 1100 + b"}",
    ],
)
def test_confirm_readiness_warns_when_archive_index_cannot_be_assessed(
    tmp_path: Path, readiness_workspace: tuple[Path, Path, datetime], payload: bytes,
) -> None:
    _, program_dir, _ = readiness_workspace
    _write_readiness_edition(tmp_path, program_dir)
    archive_dir = program_dir / "archive" / "synthetic_weekly"
    archive_dir.mkdir(parents=True)
    index_path = archive_dir / "index.json"
    index_path.write_bytes(payload)

    check = _archive_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == "warn"
    assert "synthetic_private_content" not in repr(check)
    assert index_path.read_bytes() == payload


@pytest.mark.parametrize("error_type", [PermissionError, FileNotFoundError])
def test_confirm_readiness_warns_on_archive_read_failure(
    tmp_path: Path,
    readiness_workspace: tuple[Path, Path, datetime],
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[OSError],
) -> None:
    _, program_dir, _ = readiness_workspace
    _write_readiness_edition(tmp_path, program_dir)
    archive_dir = program_dir / "archive" / "synthetic_weekly"
    archive_dir.mkdir(parents=True)
    index_path = archive_dir / "index.json"
    index_path.write_text('{"issues": []}', encoding="utf-8")
    read_text = Path.read_text

    def unreadable(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == index_path:
            raise error_type("synthetic_private_content")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    check = _archive_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == "warn"
    assert "synthetic_private_content" not in repr(check)


def test_confirm_readiness_warns_on_dangling_archive_index_link(
    tmp_path: Path, readiness_workspace: tuple[Path, Path, datetime],
) -> None:
    _, program_dir, _ = readiness_workspace
    _write_readiness_edition(tmp_path, program_dir)
    archive_dir = program_dir / "archive" / "synthetic_weekly"
    archive_dir.mkdir(parents=True)
    target = archive_dir / "missing-target"
    target.mkdir()
    _link_directory(archive_dir / "index.json", target)
    target.rmdir()

    check = _archive_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == "warn"
    assert check.metadata is None or "confirmed_count" not in check.metadata


@pytest.mark.parametrize("conflict", ["workspace", "program"])
def test_confirm_readiness_does_not_assess_a_different_archive_authority(
    tmp_path: Path, readiness_workspace: tuple[Path, Path, datetime], conflict: str,
) -> None:
    programs_root, program_dir, now = readiness_workspace
    _write_readiness_edition(tmp_path, program_dir, legacy=conflict == "workspace")
    report = run_confirm_readiness_doctor(
        edition_name="synthetic_weekly",
        program_id="other" if conflict == "program" else "myprog",
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=(tmp_path / "other" / "archive") if conflict == "workspace" else tmp_path / "archive",
        now=now,
    )

    check = _archive_check(report)
    assert check.status == "warn"
    assert check.metadata is None or "confirmed_count" not in check.metadata


def test_confirm_readiness_honors_explicit_program_root_with_other_archive_root(tmp_path: Path) -> None:
    programs_root = tmp_path / "chosen-home"
    program_dir = programs_root / "myprog"
    program_dir.mkdir(parents=True)
    _write_readiness_edition(tmp_path, program_dir)
    now = datetime.now(timezone.utc)
    _write_gather_state(program_dir, gathered_at=now)
    _write_overrides(program_dir, {"health": "low"})
    archive_dir = program_dir / "archive" / "synthetic_weekly"
    archive_dir.mkdir(parents=True)
    (archive_dir / "index.json").write_text(json.dumps({
        "edition": "synthetic_weekly",
        "issues": [{"issue_number": 1, "generated_at": now.isoformat(), "kind": "confirmed"}],
    }), encoding="utf-8")
    decoy_dir = tmp_path / "legacy" / "archive" / "synthetic_weekly"
    decoy_dir.mkdir(parents=True)
    (decoy_dir / "index.json").write_text(json.dumps({
        "edition": "synthetic_weekly",
        "issues": [
            {"issue_number": number, "generated_at": now.isoformat(), "kind": "confirmed"}
            for number in (90, 91)
        ],
    }), encoding="utf-8")

    report = run_confirm_readiness_doctor(
        edition_name="synthetic_weekly",
        program_id="myprog",
        programs_root=programs_root,
        editions_root=tmp_path / "editions",
        archive_root=tmp_path / "legacy" / "archive",
        now=now,
    )

    check = _archive_check(report)
    assert check.status == "ok"
    assert check.metadata is not None
    assert check.metadata["confirmed_count"] == 1


def test_confirm_readiness_accepts_a_selected_program_root_directory_link(tmp_path: Path) -> None:
    actual_root = tmp_path / "actual" / "programs"
    program_dir = actual_root / "myprog"
    program_dir.mkdir(parents=True)
    selected_root = tmp_path / "selected"
    _link_directory(selected_root, actual_root)
    _write_readiness_edition(tmp_path, selected_root / "myprog")
    now = datetime.now(timezone.utc)
    _write_gather_state(selected_root / "myprog", gathered_at=now)
    _write_overrides(selected_root / "myprog", {"health": "low"})
    archive_dir = program_dir / "archive" / "synthetic_weekly"
    archive_dir.mkdir(parents=True)
    (archive_dir / "index.json").write_text(json.dumps({
        "edition": "synthetic_weekly",
        "issues": [{"issue_number": 1, "generated_at": now.isoformat()}],
    }), encoding="utf-8")

    report = run_confirm_readiness_doctor(
        edition_name="synthetic_weekly", program_id="myprog", programs_root=selected_root,
        editions_root=tmp_path / "editions", archive_root=tmp_path / "legacy" / "archive", now=now,
    )

    check = _archive_check(report)
    assert check.status == "ok"
    assert check.metadata is not None
    assert check.metadata["confirmed_count"] == 1


def test_confirm_readiness_rejects_an_archive_directory_link_outside_the_program(
    tmp_path: Path, readiness_workspace: tuple[Path, Path, datetime],
) -> None:
    _, program_dir, now = readiness_workspace
    _write_readiness_edition(tmp_path, program_dir)
    outside = tmp_path / "outside"
    archive_dir = outside / "synthetic_weekly"
    archive_dir.mkdir(parents=True)
    (archive_dir / "index.json").write_text(json.dumps({
        "edition": "synthetic_weekly",
        "issues": [{"issue_number": 1, "generated_at": now.isoformat()}],
    }), encoding="utf-8")
    _link_directory(program_dir / "archive", outside)

    check = _archive_check(_readiness_report(tmp_path, readiness_workspace))

    assert check.status == "warn"
    assert check.metadata is None or "confirmed_count" not in check.metadata


@pytest.mark.parametrize("output_format", ["human", "json", "csv"])
@pytest.mark.parametrize("invalid_gather", [False, True])
@pytest.mark.parametrize("invalid_archive", [False, True])
def test_confirm_readiness_cli_uses_real_edition_cadence_and_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output_format: str,
    invalid_gather: bool, invalid_archive: bool,
) -> None:
    import src.commands.doctor as doctor

    programs_root = tmp_path / "programs"
    program_dir = programs_root / "myprog"
    program_dir.mkdir(parents=True)
    _write_readiness_edition(tmp_path, program_dir, cadence="daily")
    _write_overrides(program_dir, {"health": "low"})
    _write_gather_state(program_dir, gathered_at=datetime.now(timezone.utc) - timedelta(days=3))
    if invalid_gather:
        (program_dir / "gather_state.json").write_text(
            '{"gathered_at": "synthetic_private_content"', encoding="utf-8",
        )
    archive_dir = program_dir / "archive" / "synthetic_weekly"
    archive_dir.mkdir(parents=True)
    (archive_dir / "index.json").write_text(
        json.dumps({"edition": "synthetic_weekly", "issues": [
            {
                "issue_number": 1,
                "generated_at": "2030-01-01T00:00:00Z",
                "kind": "synthetic_private_content" if invalid_archive else "confirmed",
            },
            {"issue_number": 2, "generated_at": "2030-01-01T00:00:00Z", "kind": "skipped"},
        ]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(doctor, "REPORTS_ROOT", tmp_path / "reports")
    monkeypatch.setattr(doctor, "ARCHIVE_ROOT", tmp_path / "archive")
    monkeypatch.setattr(doctor, "EDITIONS_ROOT", tmp_path / "editions")
    monkeypatch.setattr(doctor, "_surface_alert_banner", lambda *args, **kwargs: None)
    app = typer.Typer()
    app.command()(doctor.doctor_command)

    result = CliRunner().invoke(
        app, ["--edition", "synthetic_weekly", "--confirm-readiness", "--format", output_format],
    )

    assert result.exit_code == (1 if invalid_gather else 0), result.output
    assert "synthetic_private_content" not in result.output
    assert (
        "could not be read or assessed" if invalid_archive else "Confirmed archive entries: 1"
    ) in result.output
    assert ("could not be read or validated" if invalid_gather else "stale after 2d") in result.output
