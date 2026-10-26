from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import typer

from src.commands import doctor, gather
from src.commands.doctor_checks.doctor_support_checks import resolve_doctor_roots
from src.core.exceptions import StateError


def test_gather_outcome_retains_command_required_scope_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = gather.GatherArtifacts("fixture", 0, 0, 0, 0, 0, 0, 0)
    required = Mock(return_value=True)
    monkeypatch.setattr(gather, "_has_required_scope_degradation", required)
    assert gather._semantic_gather_exit_code(artifacts) == 3
    required.assert_called_once_with(artifacts)


def test_gather_scope_retains_command_ado_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    artifacts = gather.GatherArtifacts("fixture", 0, 0, 0, 0, 0, 0, 0)
    required = Mock(return_value=True)
    monkeypatch.setattr(gather, "_has_required_ado_degradation", required)
    assert gather._has_required_scope_degradation(artifacts)
    required.assert_called_once_with(artifacts)


@pytest.mark.parametrize("error", [OSError("unreadable"), StateError("invalid"), ValueError("malformed")])
def test_alert_ledger_failure_remains_observable_not_fatal(
    monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    reader = Mock(side_effect=error)
    monkeypatch.setattr(gather, "_read_alerts", reader)
    root = Path("synthetic-selected")
    assert gather._alert_ledger_state("fixture", programs_root=root) == ({}, True)
    reader.assert_called_once_with("fixture", programs_root=root, include_resolved=True)


def test_alert_ledger_preserves_resolved_lifecycle_and_root(monkeypatch: pytest.MonkeyPatch) -> None:
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    reader = Mock(return_value=(SimpleNamespace(
        alert_id="fixture-alert", occurrence_count=2, last_seen=timestamp, resolved_at=timestamp,
    ),))
    monkeypatch.setattr(gather, "_read_alerts", reader)
    root = Path("synthetic-selected")
    assert gather._alert_ledger_state("fixture", programs_root=root) == (
        {"fixture-alert": (2, timestamp, timestamp)}, False,
    )
    reader.assert_called_once_with("fixture", programs_root=root, include_resolved=True)


def test_rev_health_retains_command_resolution_and_missing_program_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = Mock(return_value=None)
    monkeypatch.setattr(doctor, "_resolve_edition_paths", resolver)
    with pytest.raises(typer.Exit) as error:
        doctor._run_rev_health(
            edition="fixture", rev_program=None, programs_root=Path("selected-programs"),
            editions_root=Path("selected-editions"), format="json",
        )
    assert error.value.exit_code == 2
    resolver.assert_called_once_with(
        "fixture", editions_root=Path("selected-editions"), programs_root=Path("selected-programs"),
    )


@pytest.mark.parametrize("selected", ["programs_root", "reports_root", "archive_root"])
def test_doctor_root_extraction_preserves_precedence_and_defaults(selected: str) -> None:
    roots = dict.fromkeys(("programs_root", "reports_root", "archive_root", "editions_root"))
    roots[selected] = Path("selected") / selected.removesuffix("_root")
    assert resolve_doctor_roots(
        **roots, default_editions_root=Path("default-editions"),
        default_archive_root=Path("default-archive"), default_reports_root=Path("default-reports"),
    ) == tuple(Path("selected") / name for name in ("editions", "archive", "reports", "programs"))


def test_doctor_edition_resolution_retains_command_discovery_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    discover = Mock(return_value=("fixture",))
    monkeypatch.setattr(doctor, "discover_report_editions", discover)
    assert doctor._resolve_edition_name(None, Path("selected-reports")) == "fixture"
    discover.assert_called_once_with(reports_root=Path("selected-reports"))
