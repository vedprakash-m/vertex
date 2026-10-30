from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.core import archive_store, snapshot_store
from src.core.baseline_lock import BaselineLockError
from src.core.exceptions import ConfigError, StateError
from src.core.models import ArchiveEntry, ArchiveIndex, RiskLevel
from src.core.models_v2 import VitalityArchiveEntry
from tests.unit.test_archive_store import _build_manifest, _build_snapshot


EDITION = "acme_weekly"


def _bytes(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    selected = tmp_path / "chosen-home"
    archive = tmp_path / "legacy" / "archive"
    decoy = archive.parent / "programs"
    for root in (selected, decoy):
        editions = root / "acme" / "editions"
        editions.mkdir(parents=True)
        (editions / f"{EDITION}.yaml").write_text("program_id: acme\n", encoding="utf-8")
    return selected, archive, decoy


def _seed(root: Path, issue: int = 78) -> Path:
    snapshot = _build_snapshot(issue)
    snapshot = replace(snapshot, scorecards=tuple(replace(row, risk=RiskLevel.LOW) for row in snapshot.scorecards))
    edition_root = root / "acme" / "archive" / EDITION
    snapshot_path = edition_root / "snapshots" / f"issue_{issue:03d}.snapshot.json"
    html = edition_root / "html" / f"issue_{issue:03d}.html"
    markdown = edition_root / "md" / f"issue_{issue:03d}.md"
    manifest = edition_root / "manifests" / f"issue_{issue:03d}.json"
    snapshot_store._write_atomic_json(snapshot_path, snapshot_store._to_jsonable(snapshot))
    for path in (html, markdown, manifest):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic archived artifact", encoding="utf-8")
    index = ArchiveIndex(
        edition=EDITION,
        issues=(ArchiveEntry(
            issue_number=issue, generated_at=snapshot.generated_at, kind="confirmed",
            snapshot_path=str(snapshot_path), html_path=str(html), md_path=str(markdown),
            manifest_path=str(manifest),
        ),),
    )
    archive_store._write_atomic_json(edition_root / "index.json", archive_store._to_jsonable(index))
    archive_store._write_atomic_json(edition_root / "scorecards.json", {"entries": list(archive_store._append_scorecard_history((), snapshot))})
    return edition_root


def test_selected_archive_reads_ignore_populated_decoys(tmp_path: Path) -> None:
    selected, archive, decoy = _workspace(tmp_path)
    edition_root = _seed(selected)
    _seed(decoy, 77)
    before = _bytes(decoy)
    kwargs: dict[str, Any] = {"archive_root": archive, "programs_root": selected}
    assert snapshot_store.get_archive_root(EDITION, **kwargs) == edition_root
    assert archive_store.read_archive_index(EDITION, **kwargs).issues[0].issue_number == 78
    assert archive_store.is_issue_confirmed(EDITION, 78, **kwargs)
    assert not archive_store.is_issue_confirmed(EDITION, 77, **kwargs)
    snapshot, issue = archive_store.load_previous_confirmed_snapshot(EDITION, 79, **kwargs)
    assert snapshot is not None and snapshot.issue_number == issue == 78
    assert archive_store.read_scorecard_history(EDITION, **kwargs)[0]["issue_number"] == 78
    assert archive_store.read_vitality_history(EDITION, **kwargs) == ()
    assert archive_store.get_dimension_history(EDITION, "Deployment Velocity", **kwargs)[0]["issue_number"] == 78
    assert archive_store.get_all_green_streak(EDITION, **kwargs) == 1
    assert archive_store.find_archive_index_inconsistencies(EDITION, **kwargs) == ()
    assert archive_store.verify_archive_integrity(EDITION, **kwargs).ok
    assert _bytes(decoy) == before


def test_empty_selected_archive_does_not_read_populated_decoy(tmp_path: Path) -> None:
    selected, archive, decoy = _workspace(tmp_path)
    _seed(decoy, 77)
    before = _bytes(tmp_path)
    assert archive_store.read_archive_index(EDITION, archive, programs_root=selected).issues == ()
    assert archive_store.read_scorecard_history(EDITION, archive, programs_root=selected) == ()
    assert archive_store.load_previous_confirmed_snapshot(EDITION, 79, archive, programs_root=selected) == (None, None)
    assert archive_store.get_all_green_streak(EDITION, archive_root=archive, programs_root=selected) == 0
    assert _bytes(tmp_path) == before


def test_vitality_history_reads_selected_archive_not_decoy(tmp_path: Path) -> None:
    selected, archive, decoy = _workspace(tmp_path)
    _seed(selected, 78)
    _seed(decoy, 77)
    confirmed_at = datetime(2040, 1, 1, tzinfo=timezone.utc)
    for root, issue_number in ((selected, 78), (decoy, 77)):
        vitality_path = root / "acme" / "archive" / EDITION / "vitality.json"
        snapshot_store._write_atomic_json(
            vitality_path,
            snapshot_store._to_jsonable({
                "entries": [VitalityArchiveEntry(
                    issue_number=issue_number,
                    confirmed_at=confirmed_at,
                    aggregate_score=issue_number,
                    items_total=10,
                    items_fresh=8,
                    avg_richness=70,
                    leakage_events=0,
                    per_workstream={},
                )],
            }),
        )
    decoy_before = _bytes(decoy)

    history = archive_store.read_vitality_history(
        EDITION, archive_root=archive, programs_root=selected,
    )

    assert tuple(entry.issue_number for entry in history) == (78,)
    assert _bytes(decoy) == decoy_before


def test_selected_snapshot_staging_and_write_share_root(tmp_path: Path) -> None:
    selected, archive, decoy = _workspace(tmp_path)
    before = _bytes(decoy)
    kwargs: dict[str, Any] = {"archive_root": archive, "programs_root": selected}
    staged = snapshot_store.write_confirmed(EDITION, 78, _build_snapshot(), promote=False, **kwargs)
    assert staged == selected / "acme" / "archive" / EDITION / "staging" / "snapshots" / "issue_078.snapshot.json"
    assert snapshot_store.find_orphaned_staging(EDITION, **kwargs) == staged.parents[1]
    with pytest.raises(StateError, match="Incomplete confirm"):
        snapshot_store.write_confirmed(EDITION, 79, _build_snapshot(79), **kwargs)
    staged.unlink()
    staged.parent.rmdir()
    final = snapshot_store.write_confirmed(EDITION, 79, _build_snapshot(79), **kwargs)
    assert snapshot_store.read_snapshot(final).issue_number == 79
    assert snapshot_store.find_orphaned_staging(EDITION, **kwargs) is None
    assert _bytes(decoy) == before


def test_selected_confirmed_hardlock_uses_selected_index(tmp_path: Path) -> None:
    selected, archive, decoy = _workspace(tmp_path)
    _seed(selected)
    baseline = selected / "acme" / "trusted_baseline.yaml"
    baseline.write_text("trusted_issue_number: 78\n", encoding="utf-8")
    before = _bytes(tmp_path)
    assert archive_store.is_issue_confirmed(EDITION, 78, archive_root=archive, programs_root=selected)
    with pytest.raises(BaselineLockError):
        snapshot_store.write_confirmed(EDITION, 78, _build_snapshot(), archive_root=archive, programs_root=selected)
    assert _bytes(tmp_path) == before


def test_selected_archive_transaction_retains_history_and_index(tmp_path: Path) -> None:
    from src.core.models_v2 import VitalityArchiveEntry

    selected, archive, decoy = _workspace(tmp_path)
    _seed(selected)
    _seed(decoy, 77)
    before = _bytes(decoy)
    kwargs: dict[str, Any] = {"archive_root": archive, "programs_root": selected}
    paths = archive_store.write_confirmed_issue(
        EDITION, 79, _build_snapshot(79), "next html", "next markdown", _build_manifest(79),
        vitality_record=VitalityArchiveEntry(
            issue_number=79, confirmed_at=datetime(2040, 1, 1, tzinfo=timezone.utc),
            aggregate_score=70, items_total=10, items_fresh=7, avg_richness=70, leakage_events=0,
            per_workstream={}, per_owner={},
        ),
        **kwargs,
    )
    assert paths.index_path == selected / "acme" / "archive" / EDITION / "index.json"
    assert [row.issue_number for row in archive_store.read_archive_index(EDITION, **kwargs).issues] == [78, 79]
    assert [row["issue_number"] for row in archive_store.read_scorecard_history(EDITION, **kwargs)] == [78, 79]
    assert archive_store.read_vitality_history(EDITION, **kwargs)[0].issue_number == 79
    assert archive_store.verify_archive_integrity(EDITION, **kwargs).ok
    assert _bytes(decoy) == before


OPERATIONS = (
    "root", "index", "staging", "snapshot", "hardlock", "previous", "scorecards", "vitality",
    "dimension", "empty_dimension", "streak", "inconsistencies", "integrity", "transaction", "skipped", "metadata",
)


def _operation(operation: str, root: Path | None, archive: Path):
    kwargs: dict[str, Any] = {"archive_root": archive, "programs_root": root}
    return {
        "root": lambda: snapshot_store.get_archive_root(EDITION, **kwargs),
        "index": lambda: archive_store.read_archive_index(EDITION, **kwargs),
        "staging": lambda: snapshot_store.find_orphaned_staging(EDITION, **kwargs),
        "snapshot": lambda: snapshot_store.write_confirmed(EDITION, 79, _build_snapshot(79), **kwargs),
        "hardlock": lambda: archive_store.is_issue_confirmed(EDITION, 78, **kwargs),
        "previous": lambda: archive_store.load_previous_confirmed_snapshot(EDITION, 79, **kwargs),
        "scorecards": lambda: archive_store.read_scorecard_history(EDITION, **kwargs),
        "vitality": lambda: archive_store.read_vitality_history(EDITION, **kwargs),
        "dimension": lambda: archive_store.get_dimension_history(EDITION, "Deployment Velocity", **kwargs),
        "empty_dimension": lambda: archive_store.get_dimension_history(EDITION, "Deployment Velocity", last_n=0, **kwargs),
        "streak": lambda: archive_store.get_all_green_streak(EDITION, **kwargs),
        "inconsistencies": lambda: archive_store.find_archive_index_inconsistencies(EDITION, **kwargs),
        "integrity": lambda: archive_store.verify_archive_integrity(EDITION, **kwargs),
        "transaction": lambda: archive_store.write_confirmed_issue(EDITION, 79, _build_snapshot(79), "html", "md", _build_manifest(79), **kwargs),
        "skipped": lambda: archive_store.write_skipped_issue(EDITION, 79, "reason", **kwargs),
        "metadata": lambda: archive_store.update_archive_issue_metadata(EDITION, 78, {"selected": True}, **kwargs),
    }[operation]()


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("kind", ("missing", "relative", "file", "flat", "ambiguous", "malformed", "denied"))
def test_selected_archive_mutations_reject_invalid_context_before_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, kind: str,
) -> None:
    selected, archive, _ = _workspace(tmp_path)
    edition = selected / "acme" / "editions" / f"{EDITION}.yaml"
    if kind == "missing":
        selected = tmp_path / "absent"
    elif kind == "relative":
        selected = Path("relative-programs")
    elif kind == "file":
        selected = edition
    elif kind == "flat":
        edition.unlink()
        flat = selected.parent / "editions"
        flat.mkdir()
        (flat / edition.name).write_text("program_id: acme\n", encoding="utf-8")
    elif kind == "ambiguous":
        other = selected / "other" / "editions"
        other.mkdir(parents=True)
        (other / edition.name).write_text("program_id: other\n", encoding="utf-8")
    elif kind == "malformed":
        edition.write_text("program_id: ../escape\n", encoding="utf-8")
    else:
        original = Path.glob

        def denied(path: Path, pattern: str, **kwargs):
            if path == selected:
                raise PermissionError("synthetic inaccessible root")
            return original(path, pattern, **kwargs)

        monkeypatch.setattr(Path, "glob", denied)
    before = _bytes(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid context reached lock or hardlock access")

    monkeypatch.setattr(snapshot_store.ArchiveLock, "__enter__", forbidden)
    monkeypatch.setattr(snapshot_store, "assert_issue_unlocked", forbidden)
    with pytest.raises(ConfigError):
        _operation(operation, selected, archive)
    assert _bytes(tmp_path) == before


def test_selected_archive_transaction_failure_restores_selected_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected, archive, decoy = _workspace(tmp_path)
    _seed(selected)
    _seed(decoy, 77)
    before = _bytes(tmp_path)
    promote = archive_store._promote_file_with_rollback
    calls = 0

    def fail_after_promotions(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("synthetic promotion failure")
        return promote(*args, **kwargs)

    monkeypatch.setattr(archive_store, "_promote_file_with_rollback", fail_after_promotions)
    with pytest.raises(OSError, match="synthetic promotion"):
        archive_store.write_confirmed_issue(
            EDITION, 78, _build_snapshot(), "replacement", "replacement", _build_manifest(),
            archive_root=archive, programs_root=selected,
        )
    assert calls == 4
    assert _bytes(tmp_path) == before
    assert not (selected / "acme" / "archive" / EDITION / "staging").exists()


def test_selected_skipped_and_metadata_updates_preserve_other_archives(tmp_path: Path) -> None:
    selected, archive, decoy = _workspace(tmp_path)
    _seed(selected)
    _seed(decoy, 77)
    before = _bytes(decoy)
    kwargs: dict[str, Any] = {"archive_root": archive, "programs_root": selected}
    archive_store.write_skipped_issue(EDITION, 79, "selected reason", **kwargs)
    archive_store.update_archive_issue_metadata(EDITION, 78, {"selected": True}, **kwargs)
    index = archive_store.read_archive_index(EDITION, **kwargs)
    assert index.issues[0].metadata == {"selected": True}
    assert index.issues[1].reason == "selected reason"
    assert archive_store.load_skipped_issues_for_program("acme", programs_root=selected)[0].issue_number == 79
    assert archive_store.load_skipped_issues_for_program("acme", programs_root=selected, archive_root=archive) == ()
    assert _bytes(decoy) == before


@pytest.mark.parametrize("selection", ("omitted", "none"))
def test_omitted_and_none_selection_preserve_explicit_legacy_archive_root(tmp_path: Path, selection: str) -> None:
    archive = tmp_path / "legacy-archive"
    kwargs: dict[str, Any] = {"programs_root": None} if selection == "none" else {}
    root = snapshot_store.get_archive_root(EDITION, archive, **kwargs)
    assert root == archive / EDITION
    snapshot_store.write_confirmed(EDITION, 78, _build_snapshot(), archive_root=archive, **kwargs)
    archive_store.write_skipped_issue(EDITION, 79, "legacy", archive_root=archive, **kwargs)
    assert archive_store.read_archive_index(EDITION, archive, **kwargs).issues[0].reason == "legacy"
    assert snapshot_store.find_orphaned_staging(EDITION, archive, **kwargs) is None


def test_shared_resolver_alias_preserves_existing_artifact_admission(tmp_path: Path) -> None:
    from src.core.edition_resolver import resolve_artifact_edition
    from src.core.overrides_store import _resolve_artifact_edition

    selected, archive, _ = _workspace(tmp_path)
    assert _resolve_artifact_edition is resolve_artifact_edition
    assert _resolve_artifact_edition(edition=EDITION, reports_root=archive, programs_root=selected).program_dir == selected / "acme"
    assert resolve_artifact_edition(EDITION, archive, selected).program_dir == selected / "acme"
    assert resolve_artifact_edition("flat", tmp_path / "reports", None) is None


@pytest.mark.parametrize("kind", ("missing_resolution", "escaping_program"))
def test_shared_resolver_rejects_invalid_resolved_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    from src.core import edition_resolver

    selected, archive, _ = _workspace(tmp_path)
    resolved = edition_resolver.resolve_edition_paths(EDITION, programs_root=selected)
    assert resolved is not None
    result = None if kind == "missing_resolution" else replace(resolved, program_dir=tmp_path / "outside")
    monkeypatch.setattr(edition_resolver, "resolve_edition_paths", lambda *args, **kwargs: result)
    with pytest.raises(ConfigError, match="escapes"):
        snapshot_store.get_archive_root(EDITION, archive, programs_root=selected)


def test_zero_dimension_window_preserves_selected_and_legacy_admission(tmp_path: Path) -> None:
    selected, archive, _ = _workspace(tmp_path)
    assert archive_store.get_dimension_history(EDITION, "dimension", last_n=0, archive_root=archive, programs_root=selected) == ()
    assert archive_store.get_dimension_history(EDITION, "dimension", last_n=0, archive_root=archive) == ()


def test_explicit_archive_selection_preserves_template_layout(tmp_path: Path) -> None:
    root = tmp_path / "named-home"
    editions = root / "_templates" / "acme" / "editions"
    editions.mkdir(parents=True)
    (editions / f"{EDITION}.yaml").write_text("program_id: template:acme\n", encoding="utf-8")
    assert snapshot_store.get_archive_root(EDITION, programs_root=root) == root / "_templates" / "acme" / "archive" / EDITION


def test_selected_staged_snapshot_promotes_under_existing_selected_lock(tmp_path: Path) -> None:
    selected, archive, decoy = _workspace(tmp_path)
    _seed(selected)
    _seed(decoy, 77)
    before = _bytes(decoy)
    snapshot = _build_snapshot(79)
    staged = snapshot_store.write_confirmed(EDITION, 79, snapshot, archive_root=archive, programs_root=selected, promote=False)
    edition_root = selected / "acme" / "archive" / EDITION
    with snapshot_store.ArchiveLock(edition_root):
        paths = archive_store.write_confirmed_issue(
            EDITION, 79, snapshot, "next html", "next markdown", _build_manifest(79),
            snapshot_source=staged, snapshot_is_staged=True, acquire_lock=False,
            archive_root=archive, programs_root=selected,
        )
    assert paths.snapshot_path == edition_root / "snapshots" / "issue_079.snapshot.json"
    assert snapshot_store.read_snapshot(paths.snapshot_path).issue_number == 79
    assert archive_store.verify_archive_integrity(EDITION, archive, programs_root=selected).ok
    assert _bytes(decoy) == before
