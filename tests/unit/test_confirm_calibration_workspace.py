from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import src.commands.confirm as confirm
from src.commands.confirm_stages import baseline_followthrough
from src.core.calibration_engine import read_calibration_prior, write_calibration_prior
from src.core.exceptions import ConfigError
from src.core.models_v2 import WorkstreamCalibration
from src.core.overrides_store import get_overrides_path
from tests.support.synthetic_report_source import synthetic_report_root as repo_root
from tests.unit.test_commands_confirm import EDITION_NAME, _prepare_confirmable_weekly_issue


def _edition(root: Path, owner: str = "acme", *, program_id: str | None = None) -> None:
    path = root / owner / "editions" / f"{EDITION_NAME}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"program_id: {program_id or owner}\n", encoding="utf-8")


def _files(root: Path) -> dict[str, bytes | None] | None:
    if not root.exists():
        return None
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def _decoy(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "decoy"
    programs = root / "programs"
    _edition(programs)
    prior = programs / "acme" / "archive" / EDITION_NAME / "review" / "issue_001.calibration.json"
    prior.parent.mkdir(parents=True, exist_ok=True)
    prior.write_bytes(b'{"sentinel": "unchanged"}\n')
    return root / "archive", prior


@pytest.mark.parametrize(
    ("explicit_archive", "pilot", "dry_run"),
    [(False, True, False), (True, True, False), (False, False, False), (False, True, True)],
)
def test_confirm_calibration_stays_in_selected_workspace(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_archive: bool,
    pilot: bool,
    dry_run: bool,
) -> None:
    edition = repo_root / "programs" / "acme" / "editions" / f"{EDITION_NAME}.yaml"
    payload = yaml.safe_load(edition.read_text(encoding="utf-8"))
    payload["calibration_pilot"] = pilot
    edition.write_text(yaml.safe_dump(payload), encoding="utf-8")
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "vertex-db"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    selected_archive = programs_root / "acme" / "archive" / EDITION_NAME
    selected_prior = selected_archive / "review" / "issue_001.calibration.json"
    decoy_archive, _ = _decoy(tmp_path)
    decoy_before = _files(decoy_archive.parent)
    selected_before = _files(selected_archive)
    monkeypatch.setattr(confirm, "ARCHIVE_ROOT", decoy_archive)

    result = confirm.confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        dry_run=dry_run,
        force=True,
        legacy_regex_extractor=True,
        reports_root=reports_root,
        archive_root=archive_root if explicit_archive else None,
        programs_root=programs_root,
    )

    assert result.exit_code == 0, result.failures
    assert _files(decoy_archive.parent) == decoy_before
    if dry_run:
        assert _files(selected_archive) == selected_before
    elif pilot:
        assert result.archive_paths is not None
        assert result.archive_paths.snapshot_path.is_relative_to(selected_archive)
        assert json.loads(selected_prior.read_text(encoding="utf-8")) == {
            "edition": EDITION_NAME,
            "issue_number": 1,
            "workstreams": [],
        }
        assert not any(warning.startswith("CalibrationPrior skipped") for warning in result.warnings)
    else:
        assert not selected_prior.exists()


def test_confirm_cli_uses_captured_external_workspace_for_calibration(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    edition = repo_root / "programs" / "acme" / "editions" / f"{EDITION_NAME}.yaml"
    payload = yaml.safe_load(edition.read_text(encoding="utf-8"))
    payload["calibration_pilot"] = True
    edition.write_text(yaml.safe_dump(payload), encoding="utf-8")
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "vertex-db"))
    reports_root, _, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    isolated_home = tmp_path / "home"
    environment = dict(
        os.environ,
        HOME=str(isolated_home),
        USERPROFILE=str(isolated_home),
        VERTEX_PROGRAMS_ROOT=str(programs_root),
    )
    source = Path(__file__).resolve().parents[2]

    result = subprocess.run(
        [
            sys.executable, "-B", "cli.py", "confirm", "--edition", EDITION_NAME,
            "--issue", "1", "--force", "--legacy-regex-extractor",
        ],
        cwd=source,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    selected = programs_root / "acme" / "archive" / EDITION_NAME
    assert (selected / "snapshots" / "issue_001.snapshot.json").is_file()
    prior = selected / "review" / "issue_001.calibration.json"
    assert json.loads(prior.read_text(encoding="utf-8"))["edition"] == EDITION_NAME
    assert "CalibrationPrior skipped" not in result.stdout + result.stderr


def test_qg8_block_does_not_write_calibration(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    edition = repo_root / "programs" / "acme" / "editions" / f"{EDITION_NAME}.yaml"
    payload = yaml.safe_load(edition.read_text(encoding="utf-8"))
    payload["calibration_pilot"] = True
    edition.write_text(yaml.safe_dump(payload), encoding="utf-8")
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "vertex-db"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    reports_root, _, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    overrides = get_overrides_path(EDITION_NAME, reports_root, issue_number=1, programs_root=programs_root)
    document = yaml.safe_load(overrides.read_text(encoding="utf-8"))
    for scorecard in document["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = None
    overrides.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    selected_archive = programs_root / "acme" / "archive" / EDITION_NAME
    decoy_archive, _ = _decoy(tmp_path)
    selected_before, decoy_before = _files(selected_archive), _files(decoy_archive.parent)
    monkeypatch.setattr(confirm, "ARCHIVE_ROOT", decoy_archive)

    result = confirm.confirm_issue(
        EDITION_NAME, 1, force=True, legacy_regex_extractor=True,
        reports_root=reports_root, programs_root=programs_root,
    )

    assert result.exit_code == 3
    assert any("Missing risk levels" in failure for failure in result.failures)
    assert _files(selected_archive) == selected_before
    assert _files(decoy_archive.parent) == decoy_before


def test_calibration_prior_selected_root_round_trips_without_touching_decoy(tmp_path: Path) -> None:
    selected = tmp_path / "selected" / "programs"
    _edition(selected)
    decoy_archive, _ = _decoy(tmp_path)
    decoy_before = _files(decoy_archive.parent)
    calibrations = (WorkstreamCalibration(workstream_id="deployment", met=4, contradicted=1, stale=1),)

    path = write_calibration_prior(EDITION_NAME, 1, calibrations, decoy_archive, programs_root=selected)
    loaded = read_calibration_prior(EDITION_NAME, 1, decoy_archive, programs_root=selected)

    assert path == selected / "acme" / "archive" / EDITION_NAME / "review" / "issue_001.calibration.json"
    assert loaded == calibrations
    assert _files(decoy_archive.parent) == decoy_before


@pytest.mark.parametrize("operation", ["read", "write"])
@pytest.mark.parametrize("invalid", ["missing", "malformed", "ambiguous", "relative"])
def test_calibration_prior_rejects_invalid_selected_root_without_fallback(
    tmp_path: Path, operation: str, invalid: str,
) -> None:
    selected = tmp_path / "selected" / "programs"
    selected.mkdir(parents=True)
    if invalid == "malformed":
        _edition(selected, program_id="other")
    elif invalid == "ambiguous":
        _edition(selected)
        _edition(selected, "other")
    elif invalid == "relative":
        selected = Path("relative-programs")
    decoy_archive, _ = _decoy(tmp_path)
    before = _files(tmp_path)

    with pytest.raises(ConfigError):
        if operation == "read":
            read_calibration_prior(EDITION_NAME, 1, decoy_archive, programs_root=selected)
        else:
            write_calibration_prior(EDITION_NAME, 1, (), decoy_archive, programs_root=selected)

    assert _files(tmp_path) == before


def test_calibration_prior_omitted_root_keeps_flat_legacy_archive(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    calibrations = (WorkstreamCalibration(workstream_id="deployment", met=4, contradicted=1, stale=1),)

    path = write_calibration_prior(EDITION_NAME, 1, calibrations, archive)

    assert path == archive / EDITION_NAME / "review" / "issue_001.calibration.json"
    assert read_calibration_prior(EDITION_NAME, 1, archive) == calibrations


@pytest.mark.parametrize("flat_fallback", [False, True])
def test_followthrough_warns_instead_of_writing_to_decoy_when_selected_edition_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flat_fallback: bool,
) -> None:
    selected = tmp_path / "selected" / "programs"
    selected.mkdir(parents=True)
    if flat_fallback:
        decoy_archive = selected.parent / "archive"
        prior = decoy_archive / EDITION_NAME / "review" / "issue_001.calibration.json"
        prior.parent.mkdir(parents=True)
        prior.write_bytes(b'{"sentinel": "unchanged"}\n')
    else:
        decoy_archive, _ = _decoy(tmp_path)
    before = _files(tmp_path)
    monkeypatch.setattr(baseline_followthrough, "advance_trusted_baseline", lambda *args, **kwargs: None)

    warnings = baseline_followthrough.apply_baseline_followthrough(
        edition_name=EDITION_NAME,
        issue_number=1,
        confirmed_at=datetime(2026, 6, 6, tzinfo=timezone.utc),
        confirmed_by="synthetic",
        warnings=(),
        archive_root=decoy_archive,
        editions_root=selected.parent / "editions",
        programs_root=selected,
        resolved_v2=SimpleNamespace(
            program=SimpleNamespace(id="acme"),
            edition=SimpleNamespace(calibration_pilot=True),
        ),
        items=(),
        untrusted=False,
        untrusted_reason=None,
    )

    assert any(warning.startswith("CalibrationPrior skipped:") for warning in warnings)
    assert _files(tmp_path) == before
