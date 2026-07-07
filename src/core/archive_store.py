from __future__ import annotations

from contextlib import nullcontext
import json
import os
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
import shutil
from typing import Any, Literal

from src.core.edition_resolver import PROGRAMS_ROOT, list_editions_for_program
from src.core.models import ArchiveEntry, ArchiveIndex, RunManifest, Snapshot
from src.core.models_v2 import SkippedIssueEntry, VitalityArchiveEntry
from src.core.snapshot_store import ARCHIVE_ROOT, ArchiveLock, find_orphaned_staging, get_archive_root, read_snapshot
from src.core.vitality_reporting import parse_vitality_archive_entry


@dataclass(frozen=True, slots=True)
class ConfirmedIssueArchivePaths:
    snapshot_path: Path
    eml_path: Path | None
    html_path: Path
    md_path: Path
    manifest_path: Path
    index_path: Path
    scorecards_path: Path
    overrides_path: Path | None
    review_path: Path | None
    narratives_path: Path | None
    continuation_contract_path: Path | None = None
    vitality_path: Path | None = None
    chart_cache_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class _RollbackEntry:
    final_path: Path
    backup_path: Path | None
    is_dir: bool


def read_archive_index(
    edition: str,
    archive_root: Path = ARCHIVE_ROOT,
) -> ArchiveIndex:
    path = get_archive_root(edition, archive_root) / "index.json"
    if not path.exists():
        return ArchiveIndex(edition=edition, issues=())
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ArchiveIndex(
        edition=str(payload.get("edition", edition)),
        issues=tuple(
            ArchiveEntry(
                issue_number=int(entry["issue_number"]),
                generated_at=datetime.fromisoformat(entry["generated_at"]),
                kind=_coerce_kind(entry.get("kind", "confirmed")),
                eml_path=_optional_string(entry.get("eml_path")),
                html_path=_optional_string(entry.get("html_path")),
                md_path=_optional_string(entry.get("md_path")),
                snapshot_path=_optional_string(entry.get("snapshot_path")),
                manifest_path=_optional_string(entry.get("manifest_path")),
                reason=_optional_string(entry.get("reason")),
                metadata=_optional_mapping(entry.get("metadata")),
            )
            for entry in payload.get("issues", [])
        ),
    )


def load_skipped_issues_for_program(
    program_id: str,
    *,
    editions_root: Path | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    archive_root: Path = ARCHIVE_ROOT,
) -> tuple[SkippedIssueEntry, ...]:
    del editions_root
    entries: list[SkippedIssueEntry] = []
    for edition_id in list_editions_for_program(program_id, programs_root=programs_root):
        archive_index = read_archive_index(edition_id, archive_root=archive_root)
        entries.extend(
            SkippedIssueEntry(
                edition_id=edition_id,
                issue_number=entry.issue_number,
                generated_at=entry.generated_at,
                reason=entry.reason,
            )
            for entry in archive_index.issues
            if entry.kind == "skipped"
        )
    return tuple(sorted(entries, key=lambda entry: (entry.edition_id, entry.issue_number)))


def find_latest_confirmed_entry(
    index: ArchiveIndex,
    *,
    before_issue_number: int | None = None,
) -> ArchiveEntry | None:
    candidates = [
        entry
        for entry in index.issues
        if entry.kind == "confirmed"
        and (before_issue_number is None or entry.issue_number < before_issue_number)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda entry: entry.issue_number)


def load_previous_confirmed_snapshot(
    edition: str,
    issue_number: int,
    archive_root: Path = ARCHIVE_ROOT,
) -> tuple[Snapshot | None, int | None]:
    previous_entry = find_latest_confirmed_entry(
        read_archive_index(edition, archive_root),
        before_issue_number=issue_number,
    )
    if previous_entry is None or previous_entry.snapshot_path is None:
        return None, None
    snapshot_path = Path(previous_entry.snapshot_path)
    if not snapshot_path.exists():
        return None, None
    return read_snapshot(snapshot_path), previous_entry.issue_number


def read_scorecard_history(
    edition: str,
    archive_root: Path = ARCHIVE_ROOT,
) -> tuple[dict[str, Any], ...]:
    path = get_archive_root(edition, archive_root) / "scorecards.json"
    if not path.exists():
        return ()
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("entries", []) if isinstance(payload, dict) else payload
    return tuple(entry for entry in entries if isinstance(entry, dict))


def read_vitality_history(
    edition: str,
    archive_root: Path = ARCHIVE_ROOT,
) -> tuple[VitalityArchiveEntry, ...]:
    path = get_archive_root(edition, archive_root) / "vitality.json"
    if not path.exists():
        return ()
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("entries", []) if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return ()
    parsed: list[VitalityArchiveEntry] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        parsed_entry = parse_vitality_archive_entry(entry)
        if parsed_entry is None:
            continue
        parsed.append(parsed_entry)
    return tuple(parsed)


def get_dimension_history(
    edition: str,
    dimension_name: str,
    *,
    archive_root: Path = ARCHIVE_ROOT,
    last_n: int = 5,
) -> tuple[dict[str, Any], ...]:
    if last_n <= 0:
        return ()
    normalized = dimension_name.strip().lower()
    matches = [
        entry
        for entry in read_scorecard_history(edition, archive_root=archive_root)
        if str(entry.get("dimension", "")).strip().lower() == normalized
    ]
    return tuple(matches[-last_n:])


def get_all_green_streak(
    edition: str,
    *,
    archive_root: Path = ARCHIVE_ROOT,
    before_issue_number: int | None = None,
) -> int:
    confirmed_issue_numbers = [
        entry.issue_number
        for entry in sorted(read_archive_index(edition, archive_root).issues, key=lambda entry: entry.issue_number, reverse=True)
        if entry.kind == "confirmed"
        and (before_issue_number is None or entry.issue_number < before_issue_number)
    ]
    if not confirmed_issue_numbers:
        return 0

    risks_by_issue: dict[int, list[str]] = {}
    for entry in read_scorecard_history(edition, archive_root=archive_root):
        try:
            raw_issue_number = entry.get("issue_number")
            if raw_issue_number is None:
                continue
            issue_number = int(raw_issue_number)
        except (TypeError, ValueError):
            continue
        risk = str(entry.get("risk", "")).strip().lower()
        if not risk:
            continue
        risks_by_issue.setdefault(issue_number, []).append(risk)

    streak = 0
    for issue_number in confirmed_issue_numbers:
        risks = risks_by_issue.get(issue_number, ())
        if not risks:
            break
        if any(risk not in {"low", "done"} for risk in risks):
            break
        streak += 1
    return streak


def find_archive_index_inconsistencies(
    edition: str,
    archive_root: Path = ARCHIVE_ROOT,
) -> tuple[str, ...]:
    edition_root = get_archive_root(edition, archive_root)
    index = read_archive_index(edition, archive_root)
    inconsistencies: list[str] = []
    seen_issue_numbers: set[int] = set()
    expected_names: dict[str, set[str]] = {
        "snapshots": set(),
        "eml": set(),
        "html": set(),
        "md": set(),
        "manifests": set(),
    }

    for entry in index.issues:
        if entry.issue_number in seen_issue_numbers:
            inconsistencies.append(f"Issue {entry.issue_number:03d} appears multiple times in archive index")
        seen_issue_numbers.add(entry.issue_number)

        if entry.kind != "confirmed":
            continue

        required_paths = {
            "snapshot": entry.snapshot_path,
            "html": entry.html_path,
            "markdown": entry.md_path,
            "manifest": entry.manifest_path,
        }
        for label, path_value in required_paths.items():
            if not path_value:
                inconsistencies.append(f"Issue {entry.issue_number:03d} is missing a {label} path in archive index")
                continue
            path = Path(path_value)
            if not path.exists():
                inconsistencies.append(f"Issue {entry.issue_number:03d} {label} file is missing: {path}")

        expected_names["snapshots"].add(f"issue_{entry.issue_number:03d}.snapshot.json")
        if entry.eml_path is not None:
            eml_path = Path(entry.eml_path)
            if not eml_path.exists():
                inconsistencies.append(f"Issue {entry.issue_number:03d} eml file is missing: {eml_path}")
            expected_names["eml"].add(f"issue_{entry.issue_number:03d}.eml")
        published_eml_path = None
        if entry.metadata is not None:
            published_eml_value = entry.metadata.get("published_eml_path")
            if isinstance(published_eml_value, str) and published_eml_value.strip():
                published_eml_path = Path(published_eml_value)
        if published_eml_path is not None:
            if not published_eml_path.exists():
                inconsistencies.append(
                    f"Issue {entry.issue_number:03d} published eml file is missing: {published_eml_path}"
                )
            expected_names["eml"].add(published_eml_path.name)
        expected_names["html"].add(f"issue_{entry.issue_number:03d}.html")
        expected_names["md"].add(f"issue_{entry.issue_number:03d}.md")
        expected_names["manifests"].add(f"issue_{entry.issue_number:03d}.json")

    for folder_name, expected in expected_names.items():
        folder_path = edition_root / folder_name
        if not folder_path.exists():
            continue
        actual = {
            child.name
            for child in folder_path.iterdir()
            if child.is_file() and child.name.startswith("issue_")
        }
        for filename in sorted(actual - expected):
            inconsistencies.append(f"Archive {folder_name} contains unindexed file {filename}")

    return tuple(inconsistencies)


# ---------------------------------------------------------------------------
# WS-1 archive integrity gate
# ---------------------------------------------------------------------------
#
# verify_archive_integrity() is a thin structured wrapper around
# find_archive_index_inconsistencies() that pre-flight gates report/confirm.
# When inconsistencies are detected and the caller has not waived the gate
# (via --force + ARCHIVE_INTEGRITY_WAIVER env var or per-call argument), the
# caller should fail loud (exit 3) before any destructive write.
#
# The function NEVER mutates the archive. It is a read-only structural check.
# A separate `scripts/reconcile_archive_index.py` (WS-1 step 3) is the
# remediation path; it is the ONLY supported way to repair a broken archive.


@dataclass(frozen=True, slots=True)
class ArchiveIntegrityResult:
    """Structured result of verify_archive_integrity.

    Attributes:
        ok: True if and only if `inconsistencies` is empty.
        edition: The edition that was checked.
        inconsistencies: Tuple of human-readable inconsistency strings.
        checked_paths: Tuple of paths that were verified to exist (for
            audit / debug).
    """

    ok: bool
    edition: str
    inconsistencies: tuple[str, ...]
    checked_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "edition": self.edition,
            "inconsistencies": list(self.inconsistencies),
            "checked_paths": list(self.checked_paths),
        }


def verify_archive_integrity(
    edition: str,
    archive_root: Path = ARCHIVE_ROOT,
) -> ArchiveIntegrityResult:
    """WS-1: read-only structural integrity check for an edition's archive.

    Returns an ArchiveIntegrityResult whose `ok` is True iff every confirmed
    entry's snapshot/html/md/manifest (and eml, if recorded) path exists
    on disk. This is the gate that report.py and confirm.py call as a
    pre-flight (exit 3 on failure unless waived).
    """
    inconsistencies = find_archive_index_inconsistencies(edition, archive_root)
    checked: list[str] = []
    edition_root = get_archive_root(edition, archive_root)
    if edition_root.exists():
        for child in sorted(edition_root.iterdir()):
            if child.is_dir():
                checked.append(child.name)
    return ArchiveIntegrityResult(
        ok=not inconsistencies,
        edition=edition,
        inconsistencies=inconsistencies,
        checked_paths=tuple(checked),
    )


def archive_integrity_waived(env: dict[str, str] | None = None) -> bool:
    """WS-1: return True iff the caller has explicitly waived the gate.

    The waiver is intentionally narrow: it requires both an env var
    (`VERTEX_ARCHIVE_INTEGRITY_WAIVER=1`) AND a per-call flag (e.g.
    `report --force-archive-integrity`). Returning False here means the
    caller MUST fail loud on inconsistencies.
    """
    _env: dict[str, str] = env if env is not None else dict(os.environ)
    return _env.get("VERTEX_ARCHIVE_INTEGRITY_WAIVER") == "1"


def write_confirmed_issue(
    edition: str,
    issue_number: int,
    snapshot: Snapshot,
    html_body: str,
    markdown_body: str,
    manifest: RunManifest,
    eml_bytes: bytes | None = None,
    snapshot_source: Path | None = None,
    snapshot_is_staged: bool = False,
    overrides_source: Path | None = None,
    review_status_source: Path | None = None,
    narratives_source_dir: Path | None = None,
    continuation_contract_source: Path | None = None,
    vitality_record: VitalityArchiveEntry | None = None,
    archive_metadata: dict[str, Any] | None = None,
    chart_cache_entries: dict[str, dict[str, Any]] | None = None,
    archive_root: Path = ARCHIVE_ROOT,
    acquire_lock: bool = True,
) -> ConfirmedIssueArchivePaths:
    edition_root = get_archive_root(edition, archive_root)
    staging_root = edition_root / "staging"
    rollback_root = staging_root / ".rollback"
    snapshot_filename = f"issue_{issue_number:03d}.snapshot.json"
    eml_filename = f"issue_{issue_number:03d}.eml"
    html_filename = f"issue_{issue_number:03d}.html"
    markdown_filename = f"issue_{issue_number:03d}.md"
    manifest_filename = f"issue_{issue_number:03d}.json"
    overrides_filename = f"issue_{issue_number:03d}.yaml"
    review_filename = f"issue_{issue_number:03d}.review.yaml"
    narratives_dirname = f"issue_{issue_number:03d}"
    continuation_contract_filename = f"issue_{issue_number:03d}.continuation_contract.json"

    final_paths = ConfirmedIssueArchivePaths(
        snapshot_path=edition_root / "snapshots" / snapshot_filename,
        eml_path=(edition_root / "eml" / eml_filename) if eml_bytes is not None else None,
        html_path=edition_root / "html" / html_filename,
        md_path=edition_root / "md" / markdown_filename,
        manifest_path=edition_root / "manifests" / manifest_filename,
        index_path=edition_root / "index.json",
        scorecards_path=edition_root / "scorecards.json",
        overrides_path=(edition_root / "overrides" / overrides_filename) if overrides_source is not None else None,
        review_path=(edition_root / "review" / review_filename) if review_status_source is not None else None,
        narratives_path=(edition_root / "narratives" / narratives_dirname) if narratives_source_dir is not None else None,
        continuation_contract_path=(edition_root / "continuation_contracts" / continuation_contract_filename) if continuation_contract_source is not None else None,
        vitality_path=(edition_root / "vitality.json") if vitality_record is not None else None,
        chart_cache_dir=(edition_root / "chart_cache") if chart_cache_entries else None,
    )

    lock_context = ArchiveLock(edition_root) if acquire_lock else nullcontext()
    with lock_context:
        if acquire_lock:
            orphaned_staging = find_orphaned_staging(edition, archive_root)
            if orphaned_staging is not None:
                raise RuntimeError(f"Incomplete confirm detected at {orphaned_staging}")

        rollback_entries: list[_RollbackEntry] = []
        try:
            if snapshot_source is None:
                _write_atomic_json(staging_root / "snapshots" / snapshot_filename, _with_schema(_to_jsonable(snapshot)))
            if eml_bytes is not None:
                _write_atomic_bytes(staging_root / "eml" / eml_filename, eml_bytes)
            _write_atomic_text(staging_root / "html" / html_filename, html_body)
            _write_atomic_text(staging_root / "md" / markdown_filename, markdown_body)
            _write_atomic_json(staging_root / "manifests" / manifest_filename, _with_schema(_to_jsonable(manifest)))

            if overrides_source is not None:
                _stage_copy(overrides_source, staging_root / "overrides" / overrides_filename)
            if review_status_source is not None:
                _stage_copy(review_status_source, staging_root / "review" / review_filename)
            if narratives_source_dir is not None:
                staged_narratives_dir = staging_root / "narratives" / narratives_dirname
                if staged_narratives_dir.exists():
                    shutil.rmtree(staged_narratives_dir)
                staged_narratives_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(narratives_source_dir, staged_narratives_dir)
            if continuation_contract_source is not None:
                _stage_copy(
                    continuation_contract_source,
                    staging_root / "continuation_contracts" / continuation_contract_filename,
                )

            updated_index = _append_archive_entry(
                read_archive_index(edition, archive_root),
                ArchiveEntry(
                    issue_number=issue_number,
                    generated_at=snapshot.generated_at,
                    kind="confirmed",
                    eml_path=(str(final_paths.eml_path) if final_paths.eml_path is not None else None),
                    html_path=str(final_paths.html_path),
                    md_path=str(final_paths.md_path),
                    snapshot_path=str(final_paths.snapshot_path),
                    manifest_path=str(final_paths.manifest_path),
                    metadata=dict(archive_metadata or {}),
                ),
            )
            _write_atomic_json(staging_root / "index.json", _with_schema(_to_jsonable(updated_index)))
            updated_scorecard_history = _append_scorecard_history(
                read_scorecard_history(edition, archive_root),
                snapshot,
            )
            _write_atomic_json(
                staging_root / "scorecards.json",
                {"schema_version": "1.0", "entries": list(updated_scorecard_history)},
            )
            if vitality_record is not None:
                updated_vitality_history = _append_vitality_history(
                    read_vitality_history(edition, archive_root),
                    vitality_record,
                )
                _write_atomic_json(
                    staging_root / "vitality.json",
                    {"schema_version": "1.0", "entries": _to_jsonable(list(updated_vitality_history))},
                )

            # P4-3: archive chart cache JSON sidecar for confirmed lineage
            if chart_cache_entries:
                chart_cache_staging_dir = staging_root / "chart_cache"
                chart_cache_staging_dir.mkdir(parents=True, exist_ok=True)
                for query_id, entry_data in chart_cache_entries.items():
                    chart_entry_path = chart_cache_staging_dir / f"{query_id}.json"
                    _write_atomic_json(chart_entry_path, entry_data)

            if snapshot_source is None:
                _promote_file_with_rollback(
                    staging_root / "snapshots" / snapshot_filename,
                    final_paths.snapshot_path,
                    rollback_root,
                    rollback_entries,
                )
            elif snapshot_is_staged:
                _promote_file_with_rollback(
                    snapshot_source,
                    final_paths.snapshot_path,
                    rollback_root,
                    rollback_entries,
                )

            if eml_bytes is not None and final_paths.eml_path is not None:
                _promote_file_with_rollback(
                    staging_root / "eml" / eml_filename,
                    final_paths.eml_path,
                    rollback_root,
                    rollback_entries,
                )
            _promote_file_with_rollback(staging_root / "html" / html_filename, final_paths.html_path, rollback_root, rollback_entries)
            _promote_file_with_rollback(staging_root / "md" / markdown_filename, final_paths.md_path, rollback_root, rollback_entries)
            _promote_file_with_rollback(
                staging_root / "manifests" / manifest_filename,
                final_paths.manifest_path,
                rollback_root,
                rollback_entries,
            )
            _promote_file_with_rollback(staging_root / "index.json", final_paths.index_path, rollback_root, rollback_entries)
            _promote_file_with_rollback(
                staging_root / "scorecards.json",
                final_paths.scorecards_path,
                rollback_root,
                rollback_entries,
            )
            if vitality_record is not None and final_paths.vitality_path is not None:
                _promote_file_with_rollback(
                    staging_root / "vitality.json",
                    final_paths.vitality_path,
                    rollback_root,
                    rollback_entries,
                )

            if overrides_source is not None and final_paths.overrides_path is not None:
                _promote_file_with_rollback(
                    staging_root / "overrides" / overrides_filename,
                    final_paths.overrides_path,
                    rollback_root,
                    rollback_entries,
                )
            if review_status_source is not None and final_paths.review_path is not None:
                _promote_file_with_rollback(
                    staging_root / "review" / review_filename,
                    final_paths.review_path,
                    rollback_root,
                    rollback_entries,
                )
            if continuation_contract_source is not None and final_paths.continuation_contract_path is not None:
                _promote_file_with_rollback(
                    staging_root / "continuation_contracts" / continuation_contract_filename,
                    final_paths.continuation_contract_path,
                    rollback_root,
                    rollback_entries,
                )
            if narratives_source_dir is not None and final_paths.narratives_path is not None:
                _promote_directory_with_rollback(
                    staging_root / "narratives" / narratives_dirname,
                    final_paths.narratives_path,
                    rollback_root,
                    rollback_entries,
                )
            # P4-3: promote chart cache sidecar directory
            if chart_cache_entries and final_paths.chart_cache_dir is not None:
                _promote_directory_with_rollback(
                    staging_root / "chart_cache",
                    final_paths.chart_cache_dir,
                    rollback_root,
                    rollback_entries,
                )

            shutil.rmtree(staging_root, ignore_errors=True)
            return final_paths
        except Exception:
            _rollback_promotions(rollback_entries)
            shutil.rmtree(staging_root, ignore_errors=True)
            raise


def write_skipped_issue(
    edition: str,
    issue_number: int,
    reason: str,
    archive_root: Path = ARCHIVE_ROOT,
    acquire_lock: bool = True,
    generated_at: datetime | None = None,
) -> Path:
    edition_root = get_archive_root(edition, archive_root)
    staging_root = edition_root / "staging"
    rollback_root = staging_root / ".rollback"
    final_path = edition_root / "index.json"

    lock_context = ArchiveLock(edition_root) if acquire_lock else nullcontext()
    with lock_context:
        if acquire_lock:
            orphaned_staging = find_orphaned_staging(edition, archive_root)
            if orphaned_staging is not None:
                raise RuntimeError(f"Incomplete confirm detected at {orphaned_staging}")

        archive_index = read_archive_index(edition, archive_root)
        if any(entry.issue_number == issue_number for entry in archive_index.issues):
            raise RuntimeError(f"Issue {issue_number:03d} is already recorded in the archive index.")

        rollback_entries: list[_RollbackEntry] = []
        try:
            updated_index = _append_archive_entry(
                archive_index,
                ArchiveEntry(
                    issue_number=issue_number,
                    generated_at=generated_at or datetime.now(timezone.utc),
                    kind="skipped",
                    reason=reason,
                ),
            )
            _write_atomic_json(staging_root / "index.json", _with_schema(_to_jsonable(updated_index)))
            _promote_file_with_rollback(staging_root / "index.json", final_path, rollback_root, rollback_entries)
            shutil.rmtree(staging_root, ignore_errors=True)
            return final_path
        except Exception:
            _rollback_promotions(rollback_entries)
            shutil.rmtree(staging_root, ignore_errors=True)
            raise


def update_archive_issue_metadata(
    edition: str,
    issue_number: int,
    metadata_updates: dict[str, Any],
    archive_root: Path = ARCHIVE_ROOT,
    acquire_lock: bool = True,
) -> Path:
    edition_root = get_archive_root(edition, archive_root)
    staging_root = edition_root / "staging"
    rollback_root = staging_root / ".rollback"
    final_path = edition_root / "index.json"

    lock_context = ArchiveLock(edition_root) if acquire_lock else nullcontext()
    with lock_context:
        if acquire_lock:
            orphaned_staging = find_orphaned_staging(edition, archive_root)
            if orphaned_staging is not None:
                raise RuntimeError(f"Incomplete confirm detected at {orphaned_staging}")

        archive_index = read_archive_index(edition, archive_root)
        existing_entry = next((entry for entry in archive_index.issues if entry.issue_number == issue_number), None)
        if existing_entry is None:
            raise RuntimeError(f"Issue {issue_number:03d} is not recorded in the archive index.")

        merged_metadata = dict(existing_entry.metadata or {})
        merged_metadata.update(metadata_updates)
        updated_entry = replace(existing_entry, metadata=merged_metadata)

        rollback_entries: list[_RollbackEntry] = []
        try:
            updated_index = _append_archive_entry(archive_index, updated_entry)
            _write_atomic_json(staging_root / "index.json", _with_schema(_to_jsonable(updated_index)))
            _promote_file_with_rollback(staging_root / "index.json", final_path, rollback_root, rollback_entries)
            shutil.rmtree(staging_root, ignore_errors=True)
            return final_path
        except Exception:
            _rollback_promotions(rollback_entries)
            shutil.rmtree(staging_root, ignore_errors=True)
            raise


def _append_archive_entry(index: ArchiveIndex, new_entry: ArchiveEntry) -> ArchiveIndex:
    issues = [entry for entry in index.issues if entry.issue_number != new_entry.issue_number]
    issues.append(new_entry)
    issues.sort(key=lambda entry: entry.issue_number)
    return ArchiveIndex(edition=index.edition, issues=tuple(issues))


def _append_scorecard_history(
    existing_entries: tuple[dict[str, Any], ...],
    snapshot: Snapshot,
) -> tuple[dict[str, Any], ...]:
    retained_entries = [entry for entry in existing_entries if entry.get("issue_number") != snapshot.issue_number]
    retained_entries.extend(
        {
            "issue_number": snapshot.issue_number,
            "generated_at": snapshot.generated_at.isoformat(),
            "scorecard_name": dimension.scorecard_name,
            "dimension": dimension.name,
            "risk": dimension.risk.value,
        }
        for dimension in snapshot.scorecards
    )
    return tuple(retained_entries)


def _append_vitality_history(
    existing_entries: tuple[VitalityArchiveEntry, ...],
    new_entry: VitalityArchiveEntry,
) -> tuple[VitalityArchiveEntry, ...]:
    retained_entries = [entry for entry in existing_entries if entry.issue_number != new_entry.issue_number]
    retained_entries.append(new_entry)
    retained_entries.sort(key=lambda entry: entry.issue_number)
    return tuple(retained_entries)


def _coerce_kind(value: Any) -> Literal["confirmed", "skipped"]:
    raw = str(value).strip().lower()
    if raw == "skipped":
        return "skipped"
    return "confirmed"


def _optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _optional_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _promote_file(staged_path: Path, final_path: Path) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged_path, final_path)


def _promote_directory(staged_path: Path, final_path: Path) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.exists():
        shutil.rmtree(final_path)
    shutil.move(str(staged_path), str(final_path))


def _promote_file_with_rollback(
    staged_path: Path,
    final_path: Path,
    rollback_root: Path,
    rollback_entries: list[_RollbackEntry],
) -> None:
    backup_path = _backup_existing_path(final_path, rollback_root, is_dir=False)
    rollback_entries.append(_RollbackEntry(final_path=final_path, backup_path=backup_path, is_dir=False))
    _promote_file(staged_path, final_path)


def _promote_directory_with_rollback(
    staged_path: Path,
    final_path: Path,
    rollback_root: Path,
    rollback_entries: list[_RollbackEntry],
) -> None:
    backup_path = _backup_existing_path(final_path, rollback_root, is_dir=True)
    rollback_entries.append(_RollbackEntry(final_path=final_path, backup_path=backup_path, is_dir=True))
    _promote_directory(staged_path, final_path)


def _backup_existing_path(final_path: Path, rollback_root: Path, is_dir: bool) -> Path | None:
    if not final_path.exists():
        return None
    backup_path = rollback_root / final_path.relative_to(final_path.anchor)
    if is_dir:
        if backup_path.exists():
            shutil.rmtree(backup_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(final_path, backup_path)
    else:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(final_path, backup_path)
    return backup_path


def _rollback_promotions(rollback_entries: list[_RollbackEntry]) -> None:
    for entry in reversed(rollback_entries):
        if entry.is_dir:
            if entry.final_path.exists():
                shutil.rmtree(entry.final_path)
            if entry.backup_path is not None and entry.backup_path.exists():
                entry.final_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(entry.backup_path), str(entry.final_path))
            continue

        if entry.final_path.exists():
            entry.final_path.unlink()
        if entry.backup_path is not None and entry.backup_path.exists():
            entry.final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(entry.backup_path, entry.final_path)


def _stage_copy(source_path: Path, staged_path: Path) -> None:
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, staged_path)


def _write_atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    normalized = content if content.endswith("\n") else f"{content}\n"
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(normalized)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _write_atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _with_schema(payload: dict[str, Any]) -> dict[str, Any]:
    if "schema_version" in payload:
        return payload
    return {"schema_version": "1.0", **payload}


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {
            field.name: _to_jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    return value
