from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from typer import Typer
from typer.testing import CliRunner

from src.commands import diff, propose, report, report_history
from src.commands.confirm_stages import draft_loaders
from src.core.exceptions import StateError
from src.core.models import ArchiveIndex
from src.core.pipeline import StageContext
from src.core.stages import resolution_stage


def _draft(root: Path, content: bytes) -> Path:
    path = root / "fixture" / "publications" / "fixture" / "issue_007" / "issue_007.draft.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return path


def _tree(root: Path) -> dict[Path, bytes | None]:
    return {p.relative_to(root): p.read_bytes() if p.is_file() else None for p in root.rglob("*")}


def test_load_previous_dry_run_state_reads_cached_draft_payload(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    payload = {"issue_number": 7, "items": [], "edition_type": "detailed"}
    _draft(root, json.dumps(payload).encode("utf-8"))
    assert report._load_previous_dry_run_state(edition_name="fixture", issue_number=7, programs_root=root) == payload


def test_load_previous_dry_run_state_rejects_malformed_cached_draft_payload(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    _draft(root, b"{")
    with pytest.raises(StateError):
        report._load_previous_dry_run_state(edition_name="fixture", issue_number=7, programs_root=root)


@pytest.mark.parametrize("reader", [resolution_stage, report_history])
@pytest.mark.parametrize("content", [b"", b"{PRIVATE-DRAFT", b"\xff", b"[]", b"null", b"42", b'""', b"true"])
def test_failed_history_is_not_absence(tmp_path: Path, reader: ModuleType, content: bytes) -> None:
    root = tmp_path / "programs"
    _draft(root, content)
    before = _tree(tmp_path)
    with pytest.raises(StateError, match="draft.*fixture.*007") as caught:
        reader._load_previous_dry_run_state(edition_name="fixture", issue_number=7, programs_root=root)
    assert "PRIVATE-DRAFT" not in str(caught.value)
    assert "Preserve" in str(caught.value)
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("reader", [resolution_stage, report_history])
@pytest.mark.parametrize("content", [None, b"{}", b'{"items":[]}'])
def test_missing_and_valid_empty_history_are_distinct(
    tmp_path: Path, reader: ModuleType, content: bytes | None,
) -> None:
    root = tmp_path / "programs"
    if content is not None:
        _draft(root, content)
    before = _tree(tmp_path)
    result = reader._load_previous_dry_run_state(edition_name="fixture", issue_number=7, programs_root=root)
    assert result == (None if content is None else json.loads(content))
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("reader", [resolution_stage, report_history])
@pytest.mark.parametrize("failure", [PermissionError, OSError, IsADirectoryError, RuntimeError, ValueError])
def test_expected_read_errors_are_typed_but_unexpected_errors_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: ModuleType, failure: type[Exception],
) -> None:
    root = tmp_path / "programs"
    path = _draft(root, b"{}")
    original = Path.read_text

    def read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if self == path:
            raise failure("PRIVATE-READ-DETAIL")
        return original(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)
    expected = StateError if issubclass(failure, OSError) else failure
    with pytest.raises(expected) as caught:
        reader._load_previous_dry_run_state(edition_name="fixture", issue_number=7, programs_root=root)
    if expected is StateError:
        assert failure.__name__ in str(caught.value)
        assert "PRIVATE-READ-DETAIL" not in str(caught.value)
    assert path.read_bytes() == b"{}"


def test_corrupt_history_stops_resolution_before_reseed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "programs"
    _draft(root, b"{")
    monkeypatch.setattr(resolution_stage, "load_bundle", Mock(return_value=SimpleNamespace()))
    monkeypatch.setattr(resolution_stage, "find_edition_yaml", Mock(return_value=None))
    monkeypatch.setattr(resolution_stage, "read_archive_index", Mock(return_value=ArchiveIndex("fixture", ())))
    later = {name: Mock() for name in [
        "load_trusted_baseline_issue", "delete_narratives_dir", "delete_seedable_overrides", "seed_overrides_from_prior",
    ]}
    for name, dependency in later.items():
        monkeypatch.setattr(resolution_stage, name, dependency)
    before = _tree(tmp_path)

    with pytest.raises(StateError):
        resolution_stage.ResolutionStage().execute(StageContext(
            edition_name="fixture", issue_number=7, reseed=True, dry_run=True,
            programs_root=root, reports_root=tmp_path / "reports", archive_root=tmp_path / "archive",
        ))

    for dependency in later.values():
        dependency.assert_not_called()
    assert _tree(tmp_path) == before


def test_failed_history_cannot_become_successful_empty_diff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "programs"
    _draft(root, b"{")
    monkeypatch.setattr(diff, "_load_draft_state", Mock(return_value={}))
    bundle = Mock(side_effect=AssertionError("Comparison must not continue after failed history"))
    monkeypatch.setattr(diff, "load_bundle", bundle)
    with pytest.raises(StateError):
        diff.build_offline_diff_summary(
            edition_name="fixture", issue_number=7, section=None, reports_root=tmp_path / "reports", programs_root=root,
        )
    bundle.assert_not_called()


@pytest.mark.parametrize("since", ["last-draft", "last-confirmed"])
def test_diff_cli_reports_state_failure_not_success_payload(monkeypatch: pytest.MonkeyPatch, since: str) -> None:
    builder = Mock(side_effect=StateError("Cannot read draft fixture 007; preserve evidence."))
    monkeypatch.setattr(diff, "build_offline_diff_summary", builder)
    monkeypatch.setattr(diff, "build_publish_diff_summary", builder)
    app = Typer()
    app.command()(diff.diff_command)
    result = CliRunner().invoke(app, ["--edition", "fixture", "--issue", "7", "--since", since, "--format", "json"])
    assert result.exit_code == 2
    assert "Cannot read draft" in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("since", ["last-draft", "last-confirmed"])
@pytest.mark.parametrize("format", ["human", "json", "csv"])
def test_valid_diff_cli_formats_remain_compatible(
    monkeypatch: pytest.MonkeyPatch, since: str, format: str,
) -> None:
    builder = Mock(return_value="Synthetic comparison")
    monkeypatch.setattr(diff, "build_offline_diff_summary", builder)
    monkeypatch.setattr(diff, "build_publish_diff_summary", builder)
    app = Typer()
    app.command()(diff.diff_command)
    result = CliRunner().invoke(app, ["--edition", "fixture", "--issue", "7", "--since", since, "--format", format])
    assert result.exit_code == 0, result.output
    assert "Synthetic comparison" in result.stdout
    assert not result.stderr
    if format == "json":
        assert json.loads(result.stdout)["mode"] == ("last-draft" if since == "last-draft" else "publish")


def test_proposal_cli_reports_state_failure_without_success_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(propose, "generate_section_revision_proposals", Mock(side_effect=StateError("Cannot read draft fixture 007.")))
    app = Typer()
    app.command()(propose.propose_command)
    result = CliRunner().invoke(app, ["--edition", "fixture", "--dry-run"])
    assert result.exit_code == 2
    assert "Cannot read draft" in result.stderr
    assert not result.stdout


def test_valid_proposal_preview_remains_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(propose, "generate_section_revision_proposals", Mock(return_value=SimpleNamespace(
        issue_number=7, proposal_count=0, proposals_path=None, warnings=(), preview_lines=(),
    )))
    app = Typer()
    app.command()(propose.propose_command)
    result = CliRunner().invoke(app, ["--edition", "fixture", "--dry-run"])
    assert result.exit_code == 0
    assert "Issue 007 | proposals: 0" in result.stdout
    assert not result.stderr


def test_report_cli_reports_state_failure_on_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report, "PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr(report, "REPORTS_ROOT", tmp_path / "reports")
    monkeypatch.setattr(report, "ARCHIVE_ROOT", tmp_path / "archive")
    monkeypatch.setattr(report, "load_bundle_with_mode", Mock(return_value=SimpleNamespace(
        bundle=SimpleNamespace(config=SimpleNamespace(edition=SimpleNamespace(type="detailed"))),
    )))
    monkeypatch.setattr(report, "archive_integrity_waived", lambda: True)
    monkeypatch.setattr(report, "_maybe_auto_run_workiq_enrich", Mock())
    monkeypatch.setattr(report, "generate_report_draft", Mock(side_effect=StateError("Cannot read draft fixture 007.")))
    app = Typer()
    app.command()(report.report_command)
    result = CliRunner().invoke(app, ["--edition", "fixture", "--dry-run", "--offline", "--stdout"])
    assert result.exit_code == 2
    assert "Cannot read draft" in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("content", [b"{", b"[]", b"\xff"])
def test_required_confirm_adapter_keeps_explicit_failure(tmp_path: Path, content: bytes) -> None:
    import typer

    root = tmp_path / "programs"
    path = _draft(root, content)
    with pytest.raises(typer.BadParameter):
        draft_loaders.load_draft_state("fixture", 7, programs_root=root)
    assert path.read_bytes() == content
