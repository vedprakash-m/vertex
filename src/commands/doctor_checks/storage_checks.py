from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport, directory_size, format_bytes
from src.commands.gather_pipeline.lifecycle_policy import load_gather_runtime_policy
from src.core.exceptions import StateError
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.edition_resolver import (
    get_program_output_dir,
    resolve_edition,
    PROGRAMS_ROOT,
    _OUTPUT_SUBDIR_LEGACY,
    _LAYOUT_MARKER_FILENAME,
    _output_subdir,
)
from src.core.gather_run_manifest import is_mismatched_oracle_result, is_weak_oracle_result, resolve_latest_committed_manifest
from src.core.journal import (
    get_program_journal_archive_dir,
    get_program_journal_dir,
    get_reviews_path,
    get_signal_threads_path,
    get_week_key,
)
from src.ai.cost_guard import load_latest_run_state
from src.ai.edit_learner import get_edit_patterns_path
from src.core.action_tracker import get_actions_path
from src.core.ai_proposal_store import (
    AI_PROPOSAL_TTL_DAYS,
    get_ai_proposals_path,
    load_ai_proposals,
    oldest_pending_proposal_age_days,
)
from src.core.claim_tracker import claim_log_checksum_matches, get_claims_path, list_claim_quarantine_paths
from src.core.decision_register import get_decisions_path
from src.core.dependency_graph import get_dependencies_path
from src.core.models_v2 import AIProposalStatus
from src.core.jsonl_utils import jsonl_checksum_matches, list_jsonl_quarantine_paths
from src.core.milestone_engine import get_milestones_path
from src.core.program_fact_store import resolve_fact_sor_mode
from src.core.reality_store import get_program_reality_db_path
from src.core.risk_register_engine import get_risk_register_path, get_risk_updates_path
from src.core.sqlite_stores import get_program_sqlite_store_path
from src.core.store_factory import resolve_storage_backend
from src.core.trajectory import get_program_trajectory_dir, list_trajectory_quarantine_paths, trajectory_checksum_matches
from src.core.workstream_documents import get_workstreams_path
from src.core.program_paths import (
    ROOT_WHITELIST,
    ROOT_T2_FILES,
    RUNTIME_ARTIFACTS,
    RUNTIME_ARTIFACTS_BY_NAME,
    RUNTIME_SUBDIR,
    get_runtime_dir,
)


def run_storage_doctor(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    archive_root: Path,
    reality_db_root: Path | None,
) -> DoctorReport:
    resolved = resolve_edition(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if resolved is None:
        return DoctorReport(edition=edition_name, checks=(DoctorCheck("Storage", "fail", f"Edition '{edition_name}' could not be resolved."),))

    program_id = resolved.program.id
    storage_backend = resolve_storage_backend(resolved.program.storage_backend)
    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    confirmed_entries = tuple(entry for entry in archive_index.issues if entry.kind == "confirmed")
    latest_confirmed = find_latest_confirmed_entry(archive_index)

    checks = (
        _edition_workspace_layout_check(program_id, programs_root=programs_root),
        _storage_retention_check(
            program_id,
            confirmed_entries=confirmed_entries,
            latest_confirmed=latest_confirmed,
            programs_root=programs_root,
        ),
        _sidecar_health_check(program_id, programs_root=programs_root),
        _trajectory_storage_check(program_id, programs_root=programs_root),
        _program_sqlite_storage_check(
            program_id,
            storage_backend=storage_backend,
            confirmed_issue_count=len(confirmed_entries),
            programs_root=programs_root,
        ),
        _reality_db_storage_check(
            program_id,
            confirmed_issue_count=len(confirmed_entries),
            db_root=reality_db_root,
        ),
        _stray_fact_store_database_check(
            program_id,
            programs_root=programs_root,
            db_root=reality_db_root,
        ),
        _state_authority_gate_check(
            program_id,
            programs_root=programs_root,
            db_root=reality_db_root,
        ),
        _fact_lineage_coverage_check(program_id, programs_root=programs_root),
        _render_manifest_sor_consistency_check(
            program_id,
            edition_name=edition_name,
            programs_root=programs_root,
            latest_confirmed_issue_number=(latest_confirmed.issue_number if latest_confirmed is not None else None),
        ),
        _gather_freshness_check(program_id, programs_root=programs_root),
        _gather_completeness_oracle_check(program_id, programs_root=programs_root),
        _rev_extraction_precision_regression_check(program_id, programs_root=programs_root),
        _fact_store_authority_check(program_id, programs_root=programs_root),
        _cost_ledger_storage_check(edition_name, programs_root=programs_root),
        _ai_proposal_queue_check(program_id, programs_root=programs_root),
        _dc01_root_cleanliness_check(program_id, programs_root=programs_root),
        _dc02_runtime_layout_check(program_id, programs_root=programs_root),
        _dc03_docs_directory_check(program_id, programs_root=programs_root),
    )
    return DoctorReport(edition=edition_name, checks=checks)


def _edition_workspace_layout_check(program_id: str, *, programs_root: Path) -> DoctorCheck:
    """PO-01: Check that the edition workspace directory uses the canonical layout.

    Multi-state severity (specs/move-output-newsletter.md §8.1):
    - OK:    publications/<edition>/ only, marker says publications
    - WARN:  output/<edition>/ only, marker absent (legacy layout, not yet migrated)
    - ERROR: Both output/ and publications/ exist (split-brain)
    - ERROR: output/ only but marker says publications (disk/marker mismatch)
    - INFO:  Neither path exists (fresh program) or partial migration in progress
    """
    import json as _json

    program_dir = programs_root / program_id
    canonical_root = program_dir / _output_subdir()
    legacy_root = program_dir / _OUTPUT_SUBDIR_LEGACY
    marker_path = program_dir / _LAYOUT_MARKER_FILENAME

    canonical_exists = canonical_root.exists()
    legacy_exists = legacy_root.exists()

    # Read marker state
    marker_layout: str | None = None
    if marker_path.exists():
        try:
            marker_data = _json.loads(marker_path.read_text(encoding="utf-8"))
            marker_layout = marker_data.get("edition_workspace_layout")
        except Exception:
            marker_layout = None

    # Split-brain: both exist simultaneously
    if canonical_exists and legacy_exists and canonical_root != legacy_root:
        return DoctorCheck(
            "PO-01 Edition Layout",
            "fail",
            (
                f"Split-brain: both '{canonical_root.name}/' and '{legacy_root.name}/' exist under "
                f"programs/{program_id}/. Run: python scripts/migrate_edition_output.py "
                f"--program {program_id} --verify"
            ),
            metadata={"program_id": program_id, "state": "split_brain"},
        )

    # Canonical layout is active
    if canonical_exists and not legacy_exists:
        if _output_subdir() == _OUTPUT_SUBDIR_LEGACY:
            # Phase 2: canonical == legacy, disk has only one dir — pass
            return DoctorCheck(
                "PO-01 Edition Layout",
                "ok",
                f"Edition workspace layout is canonical ('{canonical_root.name}/').",
                metadata={"program_id": program_id, "state": "canonical", "layout": _output_subdir()},
            )
        return DoctorCheck(
            "PO-01 Edition Layout",
            "ok",
            f"Edition workspace layout is canonical ('{canonical_root.name}/').",
            metadata={"program_id": program_id, "state": "canonical", "layout": _output_subdir()},
        )

    # Legacy only — check for marker mismatch
    if legacy_exists and not canonical_exists:
        if marker_layout == _output_subdir() and _output_subdir() != _OUTPUT_SUBDIR_LEGACY:
            # Marker says canonical, disk has legacy — mismatch
            return DoctorCheck(
                "PO-01 Edition Layout",
                "fail",
                (
                    f"Marker/disk mismatch: marker declares '{_output_subdir()}/' layout but only "
                    f"'{legacy_root.name}/' exists. Re-run migration script: "
                    f"python scripts/migrate_edition_output.py --program {program_id}"
                ),
                metadata={"program_id": program_id, "state": "mismatch", "marker_layout": marker_layout},
            )
        # Standard legacy warning — not yet migrated
        return DoctorCheck(
            "PO-01 Edition Layout",
            "warn",
            (
                f"Legacy '{legacy_root.name}/' layout. Migrate with: "
                f"python scripts/migrate_edition_output.py --program {program_id}"
            ),
            metadata={"program_id": program_id, "state": "legacy"},
        )

    # Neither path exists — fresh program or partial migration
    return DoctorCheck(
        "PO-01 Edition Layout",
        "ok",
        f"No edition workspace yet for programs/{program_id} (fresh program).",
        metadata={"program_id": program_id, "state": "fresh"},
    )


def _storage_retention_check(
    program_id: str,
    *,
    confirmed_entries: tuple[Any, ...],
    latest_confirmed: Any | None,
    programs_root: Path,
) -> DoctorCheck:
    journal_dir = get_program_journal_dir(program_id, programs_root)
    archive_dir = get_program_journal_archive_dir(program_id, programs_root)
    active_weekly_paths = tuple(sorted(path for path in journal_dir.glob("????-W??.jsonl") if path.is_file()))
    archived_weekly_paths = tuple(sorted(path for path in archive_dir.glob("????-W??.jsonl") if path.is_file()))
    active_size = sum(path.stat().st_size for path in active_weekly_paths)
    archived_size = sum(path.stat().st_size for path in archived_weekly_paths)
    auxiliary_paths = tuple(
        path
        for path in (
            get_reviews_path(program_id, programs_root),
            get_signal_threads_path(program_id, programs_root),
        )
        if path.exists()
    )
    auxiliary_size = sum(path.stat().st_size for path in auxiliary_paths)

    detail = (
        f"{len(active_weekly_paths)} active weekly partition(s) ({format_bytes(active_size)}), "
        f"{len(archived_weekly_paths)} archived ({format_bytes(archived_size)}), "
        f"aux logs {format_bytes(auxiliary_size)}, "
        f"{len(confirmed_entries)} confirmed issue(s)."
    )
    if latest_confirmed is None:
        return DoctorCheck("Storage Retention", "ok", detail)

    latest_confirmed_week = get_week_key(latest_confirmed.generated_at)
    archiveable_paths = tuple(path for path in active_weekly_paths if path.stem < latest_confirmed_week)
    if len(confirmed_entries) >= 8 and archiveable_paths:
        oldest = archiveable_paths[0].stem
        newest = archiveable_paths[-1].stem
        detail = (
            f"{detail} {len(archiveable_paths)} active partition(s) predate the latest confirmed week "
            f"{latest_confirmed_week} ({oldest}..{newest}); run "
            f"`vertex archive-journals --program {program_id} --before {latest_confirmed_week}`."
        )
        return DoctorCheck(
            "Storage Retention",
            "warn",
            detail,
            metadata={
                "archiveable_partition_count": len(archiveable_paths),
                "confirmed_issue_count": len(confirmed_entries),
                "latest_confirmed_week": latest_confirmed_week,
                "program_id": program_id,
            },
        )

    return DoctorCheck(
        "Storage Retention",
        "ok",
        detail,
        metadata={
            "active_partition_count": len(active_weekly_paths),
            "archived_partition_count": len(archived_weekly_paths),
            "confirmed_issue_count": len(confirmed_entries),
            "program_id": program_id,
        },
    )


def _sidecar_health_check(program_id: str, *, programs_root: Path) -> DoctorCheck:
    claim_quarantines = list_claim_quarantine_paths(program_id, programs_root=programs_root)
    checksum_matches = claim_log_checksum_matches(program_id, programs_root=programs_root)
    trajectory_dir = get_program_trajectory_dir(program_id, programs_root=programs_root)
    trajectory_quarantines = list_trajectory_quarantine_paths(program_id, programs_root=programs_root)
    trajectory_paths = tuple(sorted(path for path in trajectory_dir.glob("*.jsonl") if path.is_file()))
    trajectory_checksum_failures = [
        path.name
        for path in trajectory_paths
        if trajectory_checksum_matches(program_id, int(path.stem), programs_root=programs_root) is False
    ]
    if claim_quarantines:
        latest = claim_quarantines[-1]
        return DoctorCheck(
            "Sidecar Health",
            "warn",
            f"{len(claim_quarantines)} quarantined claims log file(s); latest {latest.name} under programs/{program_id}/journal/quarantine.",
            metadata={
                "claim_quarantine_count": len(claim_quarantines),
                "latest_claim_quarantine": str(latest),
                "program_id": program_id,
            },
        )

    if trajectory_quarantines:
        latest = trajectory_quarantines[-1]
        return DoctorCheck(
            "Sidecar Health",
            "warn",
            f"{len(trajectory_quarantines)} quarantined trajectory file(s); latest {latest.name} under programs/{program_id}/trajectories/quarantine.",
            metadata={
                "claim_quarantine_count": 0,
                "trajectory_quarantine_count": len(trajectory_quarantines),
                "latest_trajectory_quarantine": str(latest),
                "program_id": program_id,
            },
        )

    if checksum_matches is False:
        claims_path = get_claims_path(program_id, programs_root=programs_root)
        return DoctorCheck(
            "Sidecar Health",
            "warn",
            f"Claims log checksum is missing or mismatched for programs/{program_id}/journal/{claims_path.name}.",
            metadata={"claim_quarantine_count": 0, "claims_checksum_ok": False, "program_id": program_id},
        )

    if trajectory_checksum_failures:
        return DoctorCheck(
            "Sidecar Health",
            "warn",
            f"{len(trajectory_checksum_failures)} trajectory checksum file(s) are missing or mismatched; latest {trajectory_checksum_failures[-1]}.",
            metadata={
                "claim_quarantine_count": 0,
                "claims_checksum_ok": checksum_matches,
                "trajectory_checksum_failures": tuple(trajectory_checksum_failures),
                "program_id": program_id,
            },
        )

    # Phase 5: extended sidecar health for actions, ai_proposals, edit_patterns, risk_updates
    journal_quarantine_dir = get_program_journal_dir(program_id, programs_root) / "quarantine"
    extended_quarantines: list[str] = []
    extended_checksum_failures: list[str] = []
    for label, path, checksum_path in (
        ("actions", get_actions_path(program_id, programs_root), get_actions_path(program_id, programs_root).with_suffix(".sha256")),
        ("ai_proposals", get_ai_proposals_path(program_id, programs_root), get_ai_proposals_path(program_id, programs_root).with_suffix(".sha256")),
        ("edit_patterns", get_edit_patterns_path(program_id, programs_root), get_edit_patterns_path(program_id, programs_root).with_suffix(".sha256")),
        ("risk_updates", get_risk_updates_path(program_id, programs_root), get_risk_updates_path(program_id, programs_root).with_suffix(".sha256")),
    ):
        quarantined = list_jsonl_quarantine_paths(journal_quarantine_dir, stem=path.stem)
        if quarantined:
            extended_quarantines.append(f"{label}:{len(quarantined)}")
        if path.exists() and jsonl_checksum_matches(path, checksum_path) is False:
            extended_checksum_failures.append(label)
    if extended_quarantines:
        return DoctorCheck(
            "Sidecar Health",
            "warn",
            f"Quarantined sidecars detected: {', '.join(extended_quarantines)}.",
            metadata={
                "claim_quarantine_count": 0,
                "claims_checksum_ok": checksum_matches,
                "trajectory_checksum_failures": (),
                "extended_quarantines": tuple(extended_quarantines),
                "program_id": program_id,
            },
        )
    if extended_checksum_failures:
        return DoctorCheck(
            "Sidecar Health",
            "warn",
            f"Checksum mismatch for: {', '.join(extended_checksum_failures)}.",
            metadata={
                "claim_quarantine_count": 0,
                "claims_checksum_ok": checksum_matches,
                "trajectory_checksum_failures": (),
                "extended_checksum_failures": tuple(extended_checksum_failures),
                "program_id": program_id,
            },
        )

    return DoctorCheck(
        "Sidecar Health",
        "ok",
        "No quarantined sidecar files detected.",
        metadata={
            "claim_quarantine_count": 0,
            "claims_checksum_ok": checksum_matches,
            "trajectory_checksum_failures": (),
            "program_id": program_id,
        },
    )


def _trajectory_storage_check(program_id: str, *, programs_root: Path) -> DoctorCheck:
    trajectory_dir = get_program_trajectory_dir(program_id, programs_root=programs_root)
    trajectory_paths = tuple(sorted(path for path in trajectory_dir.glob("*.jsonl") if path.is_file()))
    if not trajectory_paths:
        return DoctorCheck("Trajectory Storage", "ok", "No trajectory files are stored for this program yet.")

    size_bytes = directory_size(trajectory_dir)
    detail = (
        f"{len(trajectory_paths)} work-item trajectory file(s), "
        f"{format_bytes(size_bytes)} under programs/{program_id}/trajectories."
    )
    return DoctorCheck(
        "Trajectory Storage",
        "ok",
        detail,
        metadata={
            "file_count": len(trajectory_paths),
            "path": str(trajectory_dir),
            "program_id": program_id,
            "size_bytes": size_bytes,
        },
    )


def _program_sqlite_storage_check(
    program_id: str,
    *,
    storage_backend: str,
    confirmed_issue_count: int,
    programs_root: Path,
) -> DoctorCheck:
    db_path = get_program_sqlite_store_path(program_id, programs_root=programs_root)
    if not db_path.exists():
        status = "warn" if storage_backend == "sqlite" and confirmed_issue_count > 0 else "ok"
        detail = (
            f"storage_backend={storage_backend}; program SQLite store is not initialized at {db_path}."
            if storage_backend == "sqlite"
            else f"storage_backend={storage_backend}; no program SQLite store at {db_path}."
        )
        return DoctorCheck("Program SQLite", status, detail)
    return _sqlite_storage_check(
        "Program SQLite",
        db_path,
        expected_location=None,
        prefix=f"storage_backend={storage_backend}; ",
    )


def _reality_db_storage_check(
    program_id: str,
    *,
    confirmed_issue_count: int,
    db_root: Path | None,
) -> DoctorCheck:
    db_path = get_program_reality_db_path(program_id, db_root=db_root)
    expected_root = Path.home() / ".vertex"
    if not db_path.exists():
        status = "ok" if confirmed_issue_count == 0 else "warn"
        return DoctorCheck(
            "Reality DB",
            status,
            f"Program fact-store DB is not initialized at {db_path}.",
            metadata={"expected_root": str(expected_root), "path": str(db_path), "program_id": program_id},
        )
    return _sqlite_storage_check(
        "Reality DB",
        db_path,
        expected_location=expected_root,
        prefix="Program fact-store DB. ",
    )


def _stray_fact_store_database_check(
    program_id: str,
    *,
    programs_root: Path,
    db_root: Path | None,
) -> DoctorCheck:
    """Track K (fix-data-flow.md §6.11, resolves PS-14's symptom): detect
    when more than one candidate ``vertex.sqlite3`` exists for a program.

    Resolves the canonical database path the same way production code does
    (``get_program_reality_db_path`` with the same ``db_root`` every report
    stage threads), then checks the other plausible-but-non-canonical
    locations a caller that omitted ``db_root``/``programs_root`` could have
    silently created: the home-directory fallback (``~/.vertex/<id>/...``,
    reachable via ``_resolve_reality_db_root``'s silent-fallback path before
    this track's root-cause fix) and the ``programs_root``-relative location
    (as opposed to ``programs_root.parent``-relative, the canonical
    resolution — see ``program_fact_store.py:457``'s
    ``resolved_db_root = programs_root.parent``). If a stray database is
    found, WARNs with both paths and their row counts so an operator can
    investigate and clean up (see the multi-DB cleanup procedure documented
    in this track's design) before it causes silent confusion.
    """
    canonical_path = get_program_reality_db_path(program_id, db_root=db_root)
    candidate_paths: dict[str, Path] = {
        "home_fallback": Path.home() / ".vertex" / program_id / "vertex.sqlite3",
        "programs_root_relative": programs_root / program_id / "vertex.sqlite3",
    }
    stray: dict[str, dict[str, object]] = {}
    for label, candidate_path in candidate_paths.items():
        if candidate_path == canonical_path:
            continue
        if not candidate_path.exists():
            continue
        stray[label] = {
            "path": str(candidate_path),
            "row_count": _count_fact_store_rows(candidate_path),
        }

    if not stray:
        return DoctorCheck(
            "Fact Store Location",
            "ok",
            f"canonical fact-store path is the only vertex.sqlite3 found for {program_id!r}: {canonical_path}",
            metadata={"canonical_path": str(canonical_path), "stray_databases": {}},
        )

    canonical_row_count = _count_fact_store_rows(canonical_path) if canonical_path.exists() else 0
    detail_parts = [
        f"{label}={info['path']} ({info['row_count']} rows)"
        for label, info in stray.items()
    ]
    return DoctorCheck(
        "Fact Store Location",
        "warn",
        (
            f"{len(stray)} stray vertex.sqlite3 file(s) found for {program_id!r} besides the canonical "
            f"path ({canonical_path}, {canonical_row_count} rows): {'; '.join(detail_parts)}. "
            "This is the PS-14 split-brain hazard — a caller that omits programs_root/db_root could "
            "silently read/write one of these instead of the canonical database. Investigate and clean "
            "up (archive or delete) once confirmed non-canonical and unread by any production call path."
        ),
        metadata={
            "canonical_path": str(canonical_path),
            "canonical_row_count": canonical_row_count,
            "stray_databases": stray,
        },
    )


def _state_authority_gate_check(
    program_id: str,
    *,
    programs_root: Path,
    db_root: Path | None,
) -> DoctorCheck:
    """ADF-W1.9: QG-37 as a hard-fail doctor check (Section 12.1's "...and
    fails doctor"), distinct from the pre-existing "Fact Store Location"
    check above (which stays informational/warn -- this one carries the
    gate's actual pass/fail verdict). Delegates its detection to
    ``state_authority.py`` so the two never define stray-database candidates
    differently.
    """
    from src.core.quality_gates.state_authority import evaluate_state_authority_gate

    evaluation = evaluate_state_authority_gate(program_id, programs_root=programs_root, db_root=db_root)
    return DoctorCheck(
        "QG-37 State Authority",
        "ok" if evaluation.passed else "fail",
        evaluation.message,
        metadata={"gate_id": evaluation.gate_id, "passed": evaluation.passed, "program_id": program_id},
    )


#: ADF-W2.4/W2.5: matches cockpit_builder.py's _LINEAGE_DEFECT_WARN_RATIO --
#: kept as a separate constant (not imported) since doctor_checks/ and
#: cockpit_builder.py are independent consumers of the same underlying
#: fact_lineage_coverage.py measurement, not coupled to each other.
_LINEAGE_DEFECT_WARN_RATIO = 0.10


def _fact_lineage_coverage_check(program_id: str, *, programs_root: Path) -> DoctorCheck:
    """ADF-W2.4/W2.5 (Section 8.14.2): "surface all waivers in cockpit and
    quality gates" -- the doctor half of the same measurement
    cockpit_builder.py's intelligence summary uses."""
    from src.core.fact_lineage_coverage import compute_lineage_coverage

    try:
        report = compute_lineage_coverage(program_id, programs_root=programs_root)
    except Exception as error:  # noqa: BLE001 -- this check must never crash `vertex doctor`
        return DoctorCheck(
            "Fact Lineage Coverage",
            "warn",
            f"could not compute fact lineage coverage for {program_id!r}: {error}",
            metadata={"program_id": program_id},
        )

    if report.total_count == 0:
        return DoctorCheck(
            "Fact Lineage Coverage",
            "ok",
            f"no facts recorded yet for {program_id!r}; nothing to measure.",
            metadata={"program_id": program_id, "total_count": 0},
        )

    metadata = {
        "program_id": program_id,
        "total_count": report.total_count,
        "lineaged_count": report.lineaged_count,
        "waived_count": report.waived_count,
        "defect_count": report.defect_count,
        "sample_defect_natural_keys": report.sample_defect_natural_keys,
        "coverage_ratio": report.coverage_ratio,
    }
    if report.defect_count == 0:
        return DoctorCheck(
            "Fact Lineage Coverage",
            "ok",
            f"all {report.total_count} fact(s) for {program_id!r} are lineaged or explicitly waived "
            f"({report.lineaged_count} lineaged, {report.waived_count} waived).",
            metadata=metadata,
        )

    defect_ratio = report.defect_count / report.total_count
    status = "warn" if defect_ratio > _LINEAGE_DEFECT_WARN_RATIO else "ok"
    return DoctorCheck(
        "Fact Lineage Coverage",
        status,
        (
            f"{report.defect_count}/{report.total_count} fact(s) for {program_id!r} have no traceable "
            f"provenance and no active waiver ({report.lineaged_count} lineaged, {report.waived_count} waived). "
            f"Sample: {', '.join(report.sample_defect_natural_keys) or 'none'}. "
            "Backfill lineage or grant a waiver via programs/<id>/fact_lineage_waivers.yaml."
        ),
        metadata=metadata,
    )


# Family -> its real authority family (source_authority.yaml's family_map).
# See docs/contributing/migrate-fact-family.md.
_MANIFEST_FAMILY_TO_AUTHORITY: dict[str, str] = {
    "milestone": "workitem.state",
    "dependency": "workitem.state",
    "risk": "judgment",
    "assumption": "judgment",
}


def _render_manifest_sor_consistency_check(
    program_id: str,
    *,
    edition_name: str,
    programs_root: Path,
    latest_confirmed_issue_number: int | None,
) -> DoctorCheck:
    """Track K (fix-data-flow.md §6.11): compares the per-issue render
    manifest's recorded ``family_read_paths`` (which read path each family
    actually used at render time, written by `validation_stage.py`'s
    `_build_family_read_paths_metadata`) against what `resolve_family_sor_mode`
    resolves to *right now* for the same families. A mismatch means either
    the SoR config changed after that issue rendered (expected, informational)
    or — more concerning — a family's stage silently isn't honoring the
    declared SoR mode at all (the exact class of drift PS-11/PS-14's own
    manual verification took hours to notice).
    """
    if latest_confirmed_issue_number is None:
        return DoctorCheck(
            "Render Manifest SoR Consistency",
            "ok",
            "no confirmed issue yet for this edition — nothing to cross-check.",
            metadata={},
        )

    from src.core.manifest_writer import get_manifest_path

    manifest_path = get_manifest_path(edition_name, latest_confirmed_issue_number, programs_root=programs_root)
    if not manifest_path.exists():
        return DoctorCheck(
            "Render Manifest SoR Consistency",
            "ok",
            f"no render manifest found at {manifest_path} for issue {latest_confirmed_issue_number} — nothing to cross-check.",
            metadata={"manifest_path": str(manifest_path)},
        )

    try:
        manifest_doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return DoctorCheck(
            "Render Manifest SoR Consistency",
            "warn",
            f"render manifest at {manifest_path} could not be parsed.",
            metadata={"manifest_path": str(manifest_path)},
        )

    recorded_read_paths = ((manifest_doc.get("metadata") or {}).get("family_read_paths")) or {}
    if not recorded_read_paths:
        return DoctorCheck(
            "Render Manifest SoR Consistency",
            "ok",
            f"render manifest for issue {latest_confirmed_issue_number} has no family_read_paths recorded (older manifest, predates this check).",
            metadata={"manifest_path": str(manifest_path)},
        )

    mismatches: dict[str, dict[str, str]] = {}
    for family, recorded_path in recorded_read_paths.items():
        authority_family = _MANIFEST_FAMILY_TO_AUTHORITY.get(family)
        if authority_family is None:
            continue
        try:
            from src.core.fact_sor_state import resolve_family_sor_mode

            current_family_mode = resolve_family_sor_mode(program_id, authority_family, programs_root=programs_root)
        except Exception:  # noqa: BLE001 -- this check must never crash `vertex doctor`
            continue
        current_path = "legacy" if current_family_mode == "legacy" else "reality"
        if current_path != recorded_path:
            mismatches[family] = {"manifest_recorded": recorded_path, "current": current_path}

    if mismatches:
        return DoctorCheck(
            "Render Manifest SoR Consistency",
            "warn",
            (
                f"issue {latest_confirmed_issue_number}'s render manifest recorded a different read path than "
                f"the current SoR config resolves to for {len(mismatches)} family/families: {mismatches}. "
                "If the SoR config changed since that issue rendered, this is expected/informational; if not, "
                "investigate whether the family's stage is honoring its declared SoR mode."
            ),
            metadata={"manifest_path": str(manifest_path), "mismatches": mismatches},
        )
    return DoctorCheck(
        "Render Manifest SoR Consistency",
        "ok",
        f"issue {latest_confirmed_issue_number}'s render manifest read paths match the current SoR config for all recorded families.",
        metadata={"manifest_path": str(manifest_path)},
    )


# Track K (fix-data-flow.md §6.11): surface when a program's reality hasn't
# been refreshed via `vertex gather` in over this many hours.
_GATHER_FRESHNESS_THRESHOLD_HOURS = 24


def _gather_freshness_check(program_id: str, *, programs_root: Path) -> DoctorCheck:
    """Track K (§6.11 item a): last-gather-timestamp freshness check.

    Reads `run_telemetry.jsonl` (already populated by every `vertex gather`
    run, per WS-17) and WARNs when the most recent recorded gather run
    finished more than `_GATHER_FRESHNESS_THRESHOLD_HOURS` hours ago, or when
    no gather run has ever been recorded at all.
    """
    from src.core.run_telemetry import read_run_telemetry

    try:
        records = read_run_telemetry(program_id, programs_root=programs_root, window=1)
    except Exception:  # noqa: BLE001 -- this check must never crash `vertex doctor`
        records = ()

    if not records:
        return DoctorCheck(
            "Gather Freshness",
            "warn",
            f"no gather run has ever been recorded for {program_id!r} (run_telemetry.jsonl is absent or empty).",
            metadata={"last_gather_finished_at": None},
        )

    last_record = records[-1]
    now = datetime.now(timezone.utc)
    finished_at = last_record.finished_at
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=timezone.utc)
    age_hours = (now - finished_at).total_seconds() / 3600.0

    if age_hours > _GATHER_FRESHNESS_THRESHOLD_HOURS:
        return DoctorCheck(
            "Gather Freshness",
            "warn",
            (
                f"last gather run for {program_id!r} finished {age_hours:.1f}h ago "
                f"(threshold {_GATHER_FRESHNESS_THRESHOLD_HOURS}h) — reality may be stale."
            ),
            metadata={"last_gather_finished_at": finished_at.isoformat(), "age_hours": round(age_hours, 1)},
        )
    return DoctorCheck(
        "Gather Freshness",
        "ok",
        f"last gather run for {program_id!r} finished {age_hours:.1f}h ago (within {_GATHER_FRESHNESS_THRESHOLD_HOURS}h threshold).",
        metadata={"last_gather_finished_at": finished_at.isoformat(), "age_hours": round(age_hours, 1)},
    )


def _gather_completeness_oracle_check(program_id: str, *, programs_root: Path) -> DoctorCheck:
    """Armada D-19/AG-2.12: surface the completeness-oracle posture of the
    last committed gather run.

    §4.3's preferred proof order for discovery completeness is (1) an
    independent Kusto/OData validation query [deferred — ARM-GATHER-15],
    (2) an operator-recorded sanitized ADO source-export/UI count, or (3) a
    same-endpoint rerun, which is only a weak consistency check. This check
    never blocks (`vertex doctor` never fails on it) — it exists so a
    same-endpoint rerun is not silently treated as sufficient completeness
    evidence forever; it warns when required scopes still rest only on that
    weak proof, and when an operator-recorded source export disagreed with
    the discovered count.
    """
    run_manifest_mode = load_gather_runtime_policy(program_id, programs_root=programs_root).run_manifest_mode
    if run_manifest_mode == "off":
        return DoctorCheck(
            "Gather Completeness Oracle",
            "ok",
            f"gather-run manifest mode is 'off' for {program_id!r}; completeness-oracle reconciliation is not applicable.",
            metadata={"run_manifest_mode": "off"},
        )

    manifest = resolve_latest_committed_manifest(program_id, programs_root=programs_root)
    if manifest is None:
        return DoctorCheck(
            "Gather Completeness Oracle",
            "warn",
            f"no committed gather run yet for {program_id!r}; completeness-oracle posture unknown.",
            metadata={"committed_run": None},
        )

    if not manifest.query_results:
        return DoctorCheck(
            "Gather Completeness Oracle",
            "ok",
            f"run {manifest.run_id} recorded no ADO query results; completeness-oracle reconciliation is not applicable.",
            metadata={"run_id": manifest.run_id},
        )

    mismatched_scopes = sorted(
        {result.scope_id for result in manifest.query_results if is_mismatched_oracle_result(result.oracle_result)}
    )
    if mismatched_scopes:
        return DoctorCheck(
            "Gather Completeness Oracle",
            "warn",
            (
                f"run {manifest.run_id} has {len(mismatched_scopes)} scope(s) where the operator-recorded "
                f"source-export count disagreed with discovery: {', '.join(mismatched_scopes[:5])}."
            ),
            metadata={"run_id": manifest.run_id, "mismatched_scopes": mismatched_scopes},
        )

    weak_scopes = sorted(
        {result.scope_id for result in manifest.query_results if is_weak_oracle_result(result.oracle_result)}
    )
    if weak_scopes:
        return DoctorCheck(
            "Gather Completeness Oracle",
            "warn",
            (
                f"{len(weak_scopes)}/{len(manifest.query_results)} scope(s) in run {manifest.run_id} rely only on a "
                "same-endpoint rerun; record `vertex gather --source-export <scope_id>=<count>` for stronger evidence."
            ),
            metadata={"run_id": manifest.run_id, "same_endpoint_rerun_scopes": weak_scopes},
        )

    return DoctorCheck(
        "Gather Completeness Oracle",
        "ok",
        f"all {len(manifest.query_results)} scope(s) in run {manifest.run_id} carry an operator source-export reconciliation.",
        metadata={"run_id": manifest.run_id},
    )


#: specs/backlog.md BL-A3: the frozen 2026-07-07 preliminary, single-annotator
#: g_xtract_prec measurement that BL-A2 permanently accepted as XPF's
#: operating-tier ceiling (programs/xpf/_quality/rev_quality_metrics.json).
_XTRACT_PREC_BASELINE = 0.8667
#: BL-A3 action item 4's required explicit, falsifiable delta: an absolute
#: drop of more than this below the baseline is a regression worth a warn.
#: Chosen (not derived) as a round number comfortably larger than normal
#: run-to-run noise on a ~15-candidate preliminary sample; revisit once a
#: larger, fleet-scale corpus makes the noise floor itself measurable.
_XTRACT_PREC_REGRESSION_DELTA = 0.05
_XTRACT_PREC_REGRESSION_FLOOR = round(_XTRACT_PREC_BASELINE - _XTRACT_PREC_REGRESSION_DELTA, 4)


def _rev_extraction_precision_regression_check(program_id: str, *, programs_root: Path) -> DoctorCheck:
    """BL-A3: compensating monitor for BL-A2's accepted precision ceiling.

    BL-A2 permanently accepted `recommended_v1_authoritative` as XPF's
    operating tier on the strength of a preliminary, single-annotator
    measurement -- defensible, but accepting a ceiling with no ongoing
    measurement means a real-world regression below it would be invisible.
    Warn-only, never blocks (mirrors `_gather_completeness_oracle_check`'s
    non-blocking pattern): this is a monitor, not a new publish gate.
    """
    metrics_path = programs_root / program_id / "_quality" / "rev_quality_metrics.json"
    if not metrics_path.exists():
        return DoctorCheck(
            "REV Extraction Precision",
            "ok",
            f"no {metrics_path.name} published yet for {program_id!r}; nothing to compare against the BL-A3 baseline.",
            metadata={"metrics_present": False},
        )
    try:
        data = json.loads(metrics_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return DoctorCheck(
            "REV Extraction Precision",
            "warn",
            f"{metrics_path} could not be parsed: {exc}",
            metadata={"metrics_present": True, "parse_error": str(exc)},
        )
    current = data.get("g_xtract_prec")
    if not isinstance(current, (int, float)):
        return DoctorCheck(
            "REV Extraction Precision",
            "ok",
            f"{metrics_path.name} has no numeric g_xtract_prec field yet.",
            metadata={"metrics_present": True},
        )
    if current < _XTRACT_PREC_REGRESSION_FLOOR:
        return DoctorCheck(
            "REV Extraction Precision",
            "warn",
            (
                f"g_xtract_prec={current:.4f} has dropped {_XTRACT_PREC_REGRESSION_DELTA:.2f}+ below the "
                f"2026-07-07 baseline {_XTRACT_PREC_BASELINE:.4f} (floor {_XTRACT_PREC_REGRESSION_FLOOR:.4f}). "
                "Re-run scripts/rev_quality_check.py and review the labeled corpus for a real extraction regression."
            ),
            metadata={
                "g_xtract_prec": current,
                "baseline": _XTRACT_PREC_BASELINE,
                "floor": _XTRACT_PREC_REGRESSION_FLOOR,
            },
        )
    return DoctorCheck(
        "REV Extraction Precision",
        "ok",
        f"g_xtract_prec={current:.4f} is at or above the BL-A3 regression floor {_XTRACT_PREC_REGRESSION_FLOOR:.4f}.",
        metadata={
            "g_xtract_prec": current,
            "baseline": _XTRACT_PREC_BASELINE,
            "floor": _XTRACT_PREC_REGRESSION_FLOOR,
        },
    )


def _count_fact_store_rows(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    try:
        with sqlite3.connect(db_path) as connection:
            table_row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'program_fact_revisions'",
            ).fetchone()
            if table_row is None:
                return 0
            row = connection.execute("SELECT COUNT(*) FROM program_fact_revisions").fetchone()
            return int(row[0]) if row is not None else 0
    except sqlite3.DatabaseError:
        return 0


def _fact_store_authority_check(program_id: str, *, programs_root: Path) -> DoctorCheck:
    sor_mode = resolve_fact_sor_mode(program_id=program_id, programs_root=programs_root)
    legacy_mutable_paths = tuple(
        path
        for path in (
            get_actions_path(program_id, programs_root),
            get_claims_path(program_id, programs_root),
            get_decisions_path(program_id, programs_root),
            get_dependencies_path(program_id, programs_root),
            get_milestones_path(program_id, programs_root),
            get_risk_register_path(program_id, programs_root),
            get_workstreams_path(program_id, programs_root),
        )
        if path.exists()
    )
    authority = "authoritative" if sor_mode == "primary" else "legacy"
    shadow_write_retention = "enabled" if legacy_mutable_paths else "disabled"
    status = "ok" if authority == "authoritative" else "warn"
    return DoctorCheck(
        "Fact Store Authority",
        status,
        (
            f"fact_store_authority={authority}, sor_mode={sor_mode}, "
            f"shadow_write_retention={shadow_write_retention}, legacy_mutable_paths={len(legacy_mutable_paths)}"
        ),
        metadata={
            "fact_store_authority": authority,
            "legacy_mutable_paths": tuple(str(path) for path in legacy_mutable_paths),
            "program_id": program_id,
            "shadow_write_retention": shadow_write_retention,
            "sor_mode": sor_mode,
        },
    )


def _sqlite_storage_check(
    label: str,
    db_path: Path,
    *,
    expected_location: Path | None,
    prefix: str,
) -> DoctorCheck:
    wal_path = Path(str(db_path) + "-wal")
    location_warning = expected_location is not None and not _path_is_within(db_path, expected_location)

    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            journal_mode_row = connection.execute("PRAGMA journal_mode").fetchone()
            journal_mode = str(journal_mode_row[0]).lower() if journal_mode_row else "unknown"
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            integrity_result = str(integrity_rows[0][0]).lower() if integrity_rows else "unknown"
    except sqlite3.DatabaseError as error:
        return DoctorCheck(label, "fail", f"{prefix}Unable to read {db_path} ({error}).")

    status = "ok"
    findings: list[str] = []
    if journal_mode != "wal":
        status = "warn"
        findings.append(f"journal_mode={journal_mode}")
    if integrity_result != "ok":
        status = "fail"
        findings.append(f"integrity_check={integrity_result}")
    if location_warning:
        status = "warn" if status == "ok" else status
        findings.append(f"path outside {expected_location}")

    detail = f"{prefix}{db_path} ({format_bytes(db_path.stat().st_size)})"
    if wal_path.exists():
        detail = f"{detail}; WAL {format_bytes(wal_path.stat().st_size)}"
    detail = f"{detail}; journal_mode={journal_mode}; integrity_check={integrity_result}"
    if findings:
        detail = f"{detail}; {'; '.join(findings)}"
    return DoctorCheck(
        label,
        status,
        detail,
        metadata={
            "db_path": str(db_path),
            "integrity_check": integrity_result,
            "journal_mode": journal_mode,
            "location_warning": location_warning,
            "size_bytes": db_path.stat().st_size,
            "wal_size_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
        },
    )


def _cost_ledger_storage_check(edition_name: str, *, programs_root: Path = PROGRAMS_ROOT) -> DoctorCheck:
    ai_dir = get_program_output_dir(edition_name, programs_root=programs_root) / "ai"
    ledger_path = ai_dir / "cost_guard.sqlite3"
    projection_path = ai_dir / "cost_guard.json"

    if not ledger_path.exists() and not projection_path.exists():
        return DoctorCheck(
            "AI Cost Ledger",
            "ok",
            f"No AI cost ledger artifacts are present yet under {ai_dir}.",
            metadata={"cost_ledger_dual_written": False, "edition": edition_name},
        )

    try:
        state = load_latest_run_state(edition_name, programs_root=programs_root)
    except StateError as error:
        return DoctorCheck(
            "AI Cost Ledger",
            "fail",
            f"AI cost ledger state invalid for {edition_name}: {error}",
            metadata={
                "cost_ledger_dual_written": ledger_path.exists() and projection_path.exists(),
                "edition": edition_name,
            },
        )

    dual_written = ledger_path.exists() and projection_path.exists()
    if state is None:
        status = "ok" if dual_written else "warn"
        detail = (
            f"AI cost ledger artifacts exist for {edition_name} but contain no run rows yet; "
            f"ledger={'present' if ledger_path.exists() else 'missing'}, projection={'present' if projection_path.exists() else 'missing'}."
        )
        return DoctorCheck(
            "AI Cost Ledger",
            status,
            detail,
            metadata={"cost_ledger_dual_written": dual_written, "edition": edition_name},
        )

    status = "ok" if dual_written else "warn"
    detail = (
        f"AI cost ledger latest run {state.run_id}: ${state.spent_usd:.3f} / ${state.budget_usd:.2f} across {state.ai_calls} AI call(s); "
        f"ledger={'present' if ledger_path.exists() else 'missing'}, projection={'present' if projection_path.exists() else 'missing'}."
    )
    return DoctorCheck(
        "AI Cost Ledger",
        status,
        detail,
        metadata={
            "ai_calls": state.ai_calls,
            "cost_ledger_dual_written": dual_written,
            "edition": edition_name,
            "latest_run_id": state.run_id,
            "spent_usd": state.spent_usd,
        },
    )


def _ai_proposal_queue_check(program_id: str, *, programs_root: Path) -> DoctorCheck:
    age_days = oldest_pending_proposal_age_days(program_id, programs_root=programs_root)
    pending_count = sum(
        1
        for _ in load_ai_proposals(
            program_id,
            status=AIProposalStatus.PENDING,
            programs_root=programs_root,
        )
    )
    if age_days is None:
        return DoctorCheck(
            "AI Proposal Queue",
            "ok",
            "No pending AI proposals.",
            metadata={"pending_count": 0, "oldest_age_days": None, "program_id": program_id},
        )
    # D-30: warn at the same threshold as the TTL. The synthesize
    # pipeline expires stale proposals on its next run, so this
    # check is the operator's signal that the queue is "stuck".
    if age_days >= AI_PROPOSAL_TTL_DAYS:
        return DoctorCheck(
            "AI Proposal Queue",
            "warn",
            (
                f"Oldest pending AI proposal is {age_days} day(s) old; "
                f"TTL is {AI_PROPOSAL_TTL_DAYS} day(s). The next synthesize "
                f"run will expire it (resolved_by=system:ttl)."
            ),
            metadata={
                "pending_count": pending_count,
                "oldest_age_days": age_days,
                "ttl_days": AI_PROPOSAL_TTL_DAYS,
                "program_id": program_id,
            },
        )
    return DoctorCheck(
        "AI Proposal Queue",
        "ok",
        f"Oldest pending AI proposal is {age_days} day(s) old.",
        metadata={
            "pending_count": pending_count,
            "oldest_age_days": age_days,
            "ttl_days": AI_PROPOSAL_TTL_DAYS,
            "program_id": program_id,
        },
    )


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Program-directory declutter doctor checks (specs/declutter.md §7).
#
# These checks emit a CANONICAL base status ("ok"/"warn"/"fail"/"info") in the
# ``status`` field and carry the sub-state ("partial"/"missing"/"stale"/
# "split-brain"/"pre_migration") in ``metadata.detail`` (§6 1-D / R-10). The
# DoctorReport aggregators count by exact status string, so a blocking state
# MUST be ``status="fail"`` — never ``status="error"`` (counted by the hardened
# model) and never a compound string like ``"WARN-stale"`` (invisible to both).
# ---------------------------------------------------------------------------

_CLUTTER_SUFFIXES = (".bak", ".bak2", ".bak3", ".bak4", ".lock", ".cp1252bak")
# A .lock file younger than this is considered active (portalocker); older is
# stale clutter. Matches the inventory script's _CLUTTER_SUFFIXES policy.
_STALE_LOCK_AGE_SECONDS = 5 * 60


def _dc01_root_cleanliness_check(program_id: str, *, programs_root: Path) -> DoctorCheck:
    """DC-01: program-root cleanliness (declutter.md §7).

    Three sub-states combined into one check:
    * DC-01-a: stale ``*.bak*``/``*.lock``/``*.cp1252bak`` at root → warn.
    * DC-01-b: ``_spike/`` size → info if > 50 files (suggest prune).
    * DC-01-c: unrecognized root-level entries (not in the registry-derived
      ``ROOT_WHITELIST``) → warn, with ``metadata.t2_root_count`` feeding the
      Phase 3 ``state/`` trigger.
    """
    program_dir = programs_root / program_id
    if not program_dir.exists():
        return DoctorCheck(
            "DC-01 Root Cleanliness",
            "ok",
            f"programs/{program_id}/ absent (fresh).",
            metadata={"program_id": program_id, "detail": "missing"},
        )

    import time as _time

    now = _time.time()
    stale_files: list[str] = []
    for entry in program_dir.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if not any(name.endswith(suffix) for suffix in _CLUTTER_SUFFIXES):
            continue
        # A recent .lock may be an active portalocker file; only flag stale ones.
        if name.endswith(".lock"):
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                age = 0.0
            if age < _STALE_LOCK_AGE_SECONDS:
                continue
        stale_files.append(name)

    # _spike/ size (DC-01-b).
    spike_dir = program_dir / "_spike"
    spike_count = 0
    if spike_dir.exists():
        spike_count = sum(1 for _ in spike_dir.rglob("*") if _.is_file())

    # Unrecognized root entries (DC-01-c). The whitelist is the registry union
    # (ROOT_WHITELIST); runtime-artifact legacy filenames are whitelisted during
    # the transition so they do not show as unrecognized pre-migration.
    unrecognized: list[str] = []
    t2_root_count = 0
    for entry in program_dir.iterdir():
        name = entry.name
        if name in ROOT_WHITELIST:
            if name in ROOT_T2_FILES:
                t2_root_count += 1
            continue
        # A clutter suffix is already covered by DC-01-a; don't double-report.
        if any(name.endswith(suffix) for suffix in _CLUTTER_SUFFIXES) and name not in stale_files:
            continue
        unrecognized.append(name)

    if stale_files:
        return DoctorCheck(
            "DC-01 Root Cleanliness",
            "warn",
            f"{len(stale_files)} stale backup/lock file(s) at root: {', '.join(sorted(stale_files))}. "
            "Delete manually after lock verification (runtime clutter only; not .bak/.lock of config).",
            metadata={
                "program_id": program_id,
                "detail": "stale_backup_lock",
                "files": sorted(stale_files),
                "unrecognized": sorted(unrecognized),
                "t2_root_count": t2_root_count,
            },
        )
    if unrecognized:
        return DoctorCheck(
            "DC-01 Root Cleanliness",
            "warn",
            f"{len(unrecognized)} unrecognized root entry(ies): {', '.join(sorted(unrecognized))}. "
            "Review against the program-directory taxonomy (declutter.md §5).",
            metadata={
                "program_id": program_id,
                "detail": "unrecognized",
                "entries": sorted(unrecognized),
                "t2_root_count": t2_root_count,
            },
        )
    detail = f"Root clean (t2_root_count={t2_root_count})."
    status = "ok"
    meta_detail = "clean"
    if spike_count > 50:
        status = "info"
        meta_detail = "spike_prune"
        detail = f"Root clean; _spike/ has {spike_count} files — consider pruning."
    return DoctorCheck(
        "DC-01 Root Cleanliness",
        status,
        detail,
        metadata={
            "program_id": program_id,
            "detail": meta_detail,
            "spike_count": spike_count,
            "t2_root_count": t2_root_count,
        },
    )


def _dc02_runtime_layout_check(
    program_id: str, *, programs_root: Path, strict: bool = False
) -> DoctorCheck:
    """DC-02: runtime-directory layout (declutter.md §6 1-D, §7).

    Per-runtime-artifact layout classification aggregated into one check:
    * clean      — all runtime files under ``runtime/``; no legacy at root → ok
    * partial    — some migrated, others at root (transition) → info
    * pre_migration — files at root, ``runtime/`` not yet in use → info
    * missing    — file absent in both locations (fresh program) → info
    * stale / split-brain — legacy AND canonical both present → warn
      (non-strict) / fail (strict). A live ``-wal``/``-shm`` sidecar at root
      while the canonical exists is split-brain evidence (R-2′).

    ``strict`` escalates the both-present state from ``warn`` (stale, transition
    window) to ``fail`` (split-brain, blocks). Default non-strict keeps the
    documented transition window non-blocking.
    """
    program_dir = programs_root / program_id
    if not program_dir.exists():
        return DoctorCheck(
            "DC-02 Runtime Layout",
            "info",
            f"programs/{program_id}/ absent (fresh).",
            metadata={"program_id": program_id, "detail": "missing"},
        )

    runtime_dir = get_runtime_dir(program_id, programs_root=programs_root)
    at_root: list[str] = []
    at_runtime: list[str] = []
    both: list[str] = []
    missing: list[str] = []
    live_sidecars: list[str] = []

    for art in RUNTIME_ARTIFACTS:
        legacy = program_dir / art.filename
        canonical = runtime_dir / art.filename
        l = legacy.exists()
        c = canonical.exists()
        if l and c:
            both.append(art.name)
        elif l and not c:
            at_root.append(art.name)
        elif c and not l:
            at_runtime.append(art.name)
        else:
            missing.append(art.name)
        # Live SQLite sidecar at root while canonical exists → split-brain evidence.
        if art.filename.endswith(".sqlite3") and c:
            for sidecar_suffix in ("-wal", "-shm"):
                sidecar = legacy.with_name(legacy.name + sidecar_suffix)
                if sidecar.exists():
                    live_sidecars.append(sidecar.name)

    total = len(RUNTIME_ARTIFACTS)
    runtime_exists = runtime_dir.exists()

    def _emit(status: str, detail_key: str, detail: str, **extra: object) -> DoctorCheck:
        meta: dict[str, object] = {
            "program_id": program_id,
            "detail": detail_key,
            "at_root": sorted(at_root),
            "at_runtime": sorted(at_runtime),
            "both": sorted(both),
            "missing": sorted(missing),
            "live_sidecars": sorted(live_sidecars),
            "strict": strict,
        }
        meta.update(extra)
        return DoctorCheck("DC-02 Runtime Layout", status, detail, metadata=meta)

    if both or live_sidecars:
        evidence = sorted(set(both) | set(live_sidecars))
        if strict:
            return _emit(
                "fail",
                "split-brain",
                f"Split-brain: {len(evidence)} runtime artifact(s) present at BOTH root and "
                f"runtime/ (or live sidecar): {', '.join(evidence)}. Run "
                f"python scripts/migrate_runtime_dir.py --program {program_id} --verify",
            )
        return _emit(
            "warn",
            "stale",
            f"Stale duplicate(s): {', '.join(evidence)} present at root and runtime/. "
            "Transition window — run --cleanup-legacy after 2 clean DC-02 runs.",
        )
    if not at_root and at_runtime and not missing:
        return _emit("ok", "clean", f"All {total} runtime artifact(s) under runtime/; no legacy at root.")
    if at_root and at_runtime and not missing:
        return _emit("info", "partial", f"Partial migration: {len(at_root)} at root, {len(at_runtime)} under runtime/.")
    if at_root and not at_runtime and not runtime_exists:
        return _emit(
            "info",
            "pre_migration",
            f"Pre-migration: {len(at_root)} runtime file(s) at root; runtime/ not yet in use. "
            f"Run: python scripts/migrate_runtime_dir.py --program {program_id} --all --execute",
        )
    if not at_root and not at_runtime and missing:
        return _emit("info", "missing", f"No runtime artifacts yet (fresh program, {total} absent).")
    # Mixed incl. missing (e.g. fresh program with one canonical file).
    return _emit(
        "info",
        "partial",
        f"Mixed layout: {len(at_root)} at root, {len(at_runtime)} under runtime/, {len(missing)} absent.",
    )


def _dc03_docs_directory_check(program_id: str, *, programs_root: Path) -> DoctorCheck:
    """DC-03: docs/ directory health (declutter.md §7).

    ``docs/`` is for one-time human documents (T-8). A file whose name matches a
    platform runtime/state/registry pattern (``*.jsonl``, ``*_state.*``,
    ``*_registry.*``) has likely been misplaced (operator dropped a platform
    artifact into docs/) → warn. docs/ absent or present-and-clean are both ok.
    """
    docs_dir = programs_root / program_id / "docs"
    if not docs_dir.exists():
        return DoctorCheck(
            "DC-03 Docs Directory",
            "ok",
            f"docs/ absent for programs/{program_id} (optional).",
            metadata={"program_id": program_id, "detail": "absent"},
        )
    misplaced: list[str] = []
    for path in docs_dir.rglob("*"):
        if not path.is_file():
            continue
        name = path.name
        if name.endswith(".jsonl"):
            misplaced.append(name)
            continue
        if "_state." in name or name.endswith("_state"):
            misplaced.append(name)
            continue
        if "_registry." in name or name.endswith("_registry"):
            misplaced.append(name)
            continue
    if misplaced:
        return DoctorCheck(
            "DC-03 Docs Directory",
            "warn",
            f"{len(misplaced)} docs/ file(s) match a platform filename pattern: "
            f"{', '.join(sorted(set(misplaced)))}. docs/ is for one-time human documents, "
            "not platform runtime/state/registry artifacts.",
            metadata={"program_id": program_id, "detail": "platform_pattern", "files": sorted(set(misplaced))},
        )
    return DoctorCheck(
        "DC-03 Docs Directory",
        "ok",
        f"docs/ present and clean ({sum(1 for _ in docs_dir.rglob('*') if _.is_file())} file(s)).",
        metadata={"program_id": program_id, "detail": "clean"},
    )
