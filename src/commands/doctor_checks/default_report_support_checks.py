from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

from src.commands.doctor_checks.models import DoctorCheck
from src.core.analytics_store import get_override_streaks, get_program_autonomy_audit_path, get_recurring_gate_failures
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.external_dependency import load_external_dependencies
from src.core.gather_state_store import build_gather_integration_summary, load_gather_state
from src.core.ledger.candidate_store import active_candidates, active_count
from src.core.ledger.event_log import read_events
from src.core.ledger.program_views import PROJECTION_SCHEMA_VERSION, canonical_projection_dump, get_current_projection_path
from src.core.ledger.source_refs import SourceRef, source_ref_requires_vault_hash
from src.core.projections.snapshot_manager import get_snapshot_dir
from src.core.protection.supersession import apply_supersession
from src.core.section_proposal_store import load_archived_stale_claim_ids, load_stale_claim_ids
from src.core.snapshot_store import get_archive_root
from src.core.source_health import build_slice_telemetry_runtime_summary


def latest_gather_integration_check(program_id: str | None, programs_root: Path) -> DoctorCheck | None:
    if program_id is None:
        return None
    gather_state = load_gather_state(program_id, programs_root=programs_root)
    if gather_state is None:
        return DoctorCheck("Gather", "ok", "No gather state recorded yet.")
    if gather_state.integration_errors <= 0:
        return DoctorCheck("Gather", "ok", "Latest gather recorded no optional integration failures.")
    summary = build_gather_integration_summary(gather_state)
    return DoctorCheck(
        "Gather",
        "warn",
        f"Latest gather recorded {summary}" if summary is not None else "Latest gather recorded optional integration failures.",
        metadata={
            "integration_errors": gather_state.integration_errors,
            "gathered_at": gather_state.gathered_at.isoformat(),
            "integration_error_details": [
                {
                    "source": detail.source,
                    "stage": detail.stage,
                    "retryable": detail.retryable,
                    "message": detail.message,
                    "operator_action": detail.operator_action,
                }
                for detail in gather_state.integration_error_details
            ],
        },
    )


def slice_telemetry_runtime_check(
    slice_contracts: Any,
    program_id: str | None,
    programs_root: Path,
) -> DoctorCheck | None:
    if program_id is None or not slice_contracts:
        return None
    gather_state = load_gather_state(program_id, programs_root=programs_root)
    if gather_state is None:
        return None
    summary = build_slice_telemetry_runtime_summary(slice_contracts, gather_state)
    if summary is None:
        return None

    detail_fragments: list[str] = []
    if summary.failed_contracts:
        detail_fragments.append(
            "failed telemetry query state -> "
            + ", ".join(f"{entry['slice_id']} ({entry['query_id']})" for entry in summary.failed_contracts[:3])
        )
    if summary.stale_contracts:
        detail_fragments.append(
            "stale against slice freshness SLA -> "
            + ", ".join(
                f"{entry['slice_id']} ({entry['query_id']}, {entry['age_hours']:.1f}h > {entry['freshness_sla_hours']}h)"
                for entry in summary.stale_contracts[:3]
            )
        )
    metadata = {
        "failed_contracts": list(summary.failed_contracts),
        "stale_contracts": list(summary.stale_contracts),
        "gathered_at": summary.gathered_at.isoformat(),
    }
    return DoctorCheck("Slice Telemetry", "warn", "; ".join(detail_fragments) + ".", metadata=metadata)


def audit_archive_settings(raw_program: dict[str, Any] | None) -> tuple[int, int]:
    if not isinstance(raw_program, dict):
        return 50000, 365
    audit_config = raw_program.get("audit")
    if not isinstance(audit_config, dict):
        return 50000, 365
    threshold_rows = audit_config.get("archive_threshold_rows")
    retention_days = audit_config.get("retention_days")
    resolved_threshold = threshold_rows if isinstance(threshold_rows, int) and threshold_rows > 0 else 50000
    resolved_retention = retention_days if isinstance(retention_days, int) and retention_days > 0 else 365
    return resolved_threshold, resolved_retention


def count_nonblank_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def audit_hygiene_check(
    *,
    program_id: str | None,
    raw_program: dict[str, Any] | None,
    programs_root: Path,
) -> DoctorCheck | None:
    if program_id is None:
        return None
    threshold_rows, retention_days = audit_archive_settings(raw_program)
    path = get_program_autonomy_audit_path(program_id, programs_root=programs_root)
    row_count = count_nonblank_lines(path)
    detail = (
        f"Active autonomy audit rows: {row_count} within threshold {threshold_rows}; "
        f"configured retention {retention_days} day(s)."
    )
    metadata = {
        "program_id": program_id,
        "path": str(path),
        "row_count": row_count,
        "archive_threshold_rows": threshold_rows,
        "retention_days": retention_days,
    }
    if row_count > threshold_rows:
        return DoctorCheck(
            "Audit Hygiene",
            "warn",
            f"Active autonomy audit rows: {row_count} exceeds threshold {threshold_rows}; configured retention {retention_days} day(s). "
            f"Run `vertex audit archive --program {program_id} --before <YYYY-MM-DD>`.",
            metadata=metadata,
        )
    return DoctorCheck("Audit Hygiene", "ok", detail, metadata=metadata)


def recurring_gate_failures_check(program_id: str | None, programs_root: Path) -> DoctorCheck | None:
    """FR-SG-40: warn when any gate has failed >= 3 consecutive times."""
    if program_id is None:
        return None
    failures = get_recurring_gate_failures(program_id, min_occurrences=3, programs_root=programs_root)
    if not failures:
        return None
    detail = "; ".join(f"{failure.gate_id}: {failure.cause} ({failure.occurrence_count}x)" for failure in failures[:3])
    return DoctorCheck("Recurring Gate Failures", "warn", f"{len(failures)} gate(s) failing repeatedly: {detail}")


def override_streak_check(program_id: str | None, programs_root: Path) -> DoctorCheck | None:
    """FR-SG-37: warn when any dimension has been manually overridden >= 3 consecutive times."""
    if program_id is None:
        return None
    streaks = get_override_streaks(program_id, min_streak=3, programs_root=programs_root)
    if not streaks:
        return None
    detail = "; ".join(f"{streak.dimension} ({streak.streak_count}x)" for streak in streaks[:3])
    return DoctorCheck("Override Streaks", "warn", f"{len(streaks)} dimension(s) repeatedly overridden: {detail}")


def external_dependencies_check(program_id: str | None, programs_root: Path) -> DoctorCheck | None:
    """FR-SG-16: surface cross-team ExternalDependency records and freshness in doctor."""
    if program_id is None:
        return None
    dependencies = load_external_dependencies(program_id, programs_root=programs_root)
    if not dependencies:
        return None
    now = datetime.now(timezone.utc)
    stale_threshold_days = 14
    stale_dependencies = [
        dependency
        for dependency in dependencies
        if dependency.last_seen is None or (now - dependency.last_seen).days > stale_threshold_days
    ]
    if stale_dependencies:
        stale_detail = "; ".join(
            f"{dependency.dep_id} ({dependency.team}, last_seen: {dependency.last_seen.date().isoformat() if dependency.last_seen else 'never'})"
            for dependency in stale_dependencies[:3]
        )
        return DoctorCheck(
            "External Dependencies",
            "warn",
            f"{len(dependencies)} external dep(s) tracked; {len(stale_dependencies)} stale (>{stale_threshold_days}d): {stale_detail}",
            metadata={"total": len(dependencies), "stale": len(stale_dependencies)},
        )
    return DoctorCheck(
        "External Dependencies",
        "ok",
        f"{len(dependencies)} external dep(s) tracked; all refreshed within {stale_threshold_days}d",
        metadata={"total": len(dependencies), "stale": 0},
    )


def candidate_queue_backlog_check(program_id: str | None, programs_root: Path) -> DoctorCheck | None:
    if program_id is None:
        return None
    current_active_count = active_count(program_id, programs_root=programs_root)
    current_active = active_candidates(program_id, programs_root=programs_root)
    if current_active_count <= 0:
        return DoctorCheck("Candidate Triage Latency", "ok", "No active ledger candidates pending triage.")
    oldest_staged_at = min(
        (candidate.staged_at for candidate in current_active if candidate.staged_at is not None),
        default=None,
    )
    if oldest_staged_at is None:
        return DoctorCheck(
            "Candidate Triage Latency",
            "warn",
            f"{current_active_count} active ledger candidate(s) pending triage; oldest staging time is unavailable.",
            metadata={"active_count": current_active_count},
        )
    age_days = (datetime.now(timezone.utc) - oldest_staged_at).days
    metadata = {
        "active_count": current_active_count,
        "oldest_staged_at": oldest_staged_at.isoformat(),
        "oldest_age_days": age_days,
    }
    if current_active_count > 100 or age_days > 14:
        return DoctorCheck(
            "Candidate Triage Latency",
            "warn",
            f"{current_active_count} active ledger candidate(s) pending triage; oldest staged {age_days} day(s) ago.",
            metadata=metadata,
        )
    return DoctorCheck(
        "Candidate Triage Latency",
        "ok",
        f"{current_active_count} active ledger candidate(s); oldest staged {age_days} day(s) ago.",
        metadata=metadata,
    )


def _parse_optional_utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _parse_optional_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    return date.fromisoformat(value)


def _field_lock_health_summary(program_id: str, programs_root: Path) -> DoctorCheck | None:
    projection_path = get_current_projection_path(program_id, programs_root=programs_root)
    if not projection_path.exists():
        return None
    projection = canonical_projection_dump(projection_path)
    locks = projection.get("field_locks", [])
    if not locks:
        return None

    now = datetime.now(timezone.utc)
    expiring: list[str] = []
    expired: list[str] = []
    for row in locks:
        valid_until = _parse_optional_utc_datetime(row.get("valid_until"))
        if valid_until is None:
            continue
        lock_label = f"{row.get('entity_id')}.{row.get('field')}"
        if valid_until <= now:
            expired.append(f"{lock_label} ({(now - valid_until).days}d overdue)")
            continue
        remaining_days = (valid_until - now).days
        if remaining_days <= 7:
            expiring.append(f"{lock_label} ({remaining_days}d left)")

    if not expired and not expiring:
        return None

    detail_parts: list[str] = []
    if expired:
        detail_parts.append("expired field lock(s) still present in the current projection: " + ", ".join(expired[:3]))
    if expiring:
        detail_parts.append("field lock(s) expiring within 7d: " + ", ".join(expiring[:3]))
    return DoctorCheck(
        "Ledger Health",
        "warn",
        "; ".join(detail_parts),
        metadata={
            "expired_field_locks": expired,
            "expiring_field_locks": expiring,
            "projection_path": str(projection_path),
        },
    )


def _gap_health_summary(program_id: str, programs_root: Path) -> DoctorCheck | None:
    projection_path = get_current_projection_path(program_id, programs_root=programs_root)
    if not projection_path.exists():
        return None
    projection = canonical_projection_dump(projection_path)
    gaps = [row for row in projection.get("gaps", []) if not row.get("acknowledged")]
    if not gaps:
        return None
    listed_windows = []
    for row in gaps[:3]:
        window_start = row.get("window_start") or "?"
        window_end = row.get("window_end") or "?"
        listed_windows.append(f"{row.get('pipeline')}:{row.get('gap_kind')} ({window_start}->{window_end})")
    return DoctorCheck(
        "Ledger Health",
        "warn",
        f"{len(gaps)} unacknowledged ledger gap(s): " + ", ".join(listed_windows),
        metadata={
            "unacknowledged_gap_ids": [str(row.get("event_id")) for row in gaps],
            "projection_path": str(projection_path),
        },
    )


def _projection_schema_health_summary(program_id: str, programs_root: Path) -> DoctorCheck | None:
    projection_path = get_current_projection_path(program_id, programs_root=programs_root)
    if not projection_path.exists():
        return None
    projection = canonical_projection_dump(projection_path)
    projection_meta = next(iter(projection.get("projection_meta", [])), None)
    if not isinstance(projection_meta, dict):
        return None
    schema_version = projection_meta.get("schema_version")
    if schema_version == PROJECTION_SCHEMA_VERSION:
        return None
    return DoctorCheck(
        "Ledger Health",
        "warn",
        f"Current projection schema version {schema_version} does not match engine version {PROJECTION_SCHEMA_VERSION}; run `vertex ledger replay --program {program_id}`.",
        metadata={
            "projection_schema_version": schema_version,
            "engine_schema_version": PROJECTION_SCHEMA_VERSION,
            "projection_path": str(projection_path),
        },
    )


def _refs_missing_required_vault_hash(events: tuple[Any, ...]) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for event in events:
        refs: list[tuple[str, SourceRef]] = [("source_ref", event.source_ref)]
        refs.extend((f"corroborating_refs[{index}]", ref) for index, ref in enumerate(event.corroborating_refs))
        for ref_role, ref in refs:
            ref_type = getattr(ref, "ref_type", None)
            if ref_type is None or not source_ref_requires_vault_hash(ref):
                continue
            vault_hash = getattr(ref, "vault_hash", None)
            if isinstance(vault_hash, str) and vault_hash.strip():
                continue
            missing.append(
                {
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "ref_role": ref_role,
                    "ref_type": str(ref_type),
                }
            )
    return missing


def _stale_operator_assertions_without_ttl(events: tuple[Any, ...]) -> list[dict[str, str]]:
    now = datetime.now(timezone.utc)
    stale: list[dict[str, str]] = []
    for event in apply_supersession(events):
        ref_type = getattr(event.source_ref, "ref_type", None)
        if ref_type != "operator_assertion":
            continue
        if isinstance(event.payload.get("valid_until"), str) and str(event.payload.get("valid_until")).strip():
            continue
        age_days = (now - event.recorded_at).days
        if age_days <= 180:
            continue
        stale.append(
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "actor": event.actor,
                "recorded_at": event.recorded_at.isoformat(),
                "age_days": str(age_days),
            }
        )
    return stale


def claim_freshness_check(
    program_id: str | None,
    edition_name: str,
    archive_root: Path,
    programs_root: Path,
) -> DoctorCheck | None:
    if program_id is None:
        return None
    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    latest_confirmed_entry = find_latest_confirmed_entry(archive_index)
    if latest_confirmed_entry is not None:
        issue_number = latest_confirmed_entry.issue_number
        archive_narratives_dir = get_archive_root(edition_name, archive_root=archive_root) / "narratives" / f"issue_{issue_number:03d}"
        stale_claim_ids = load_archived_stale_claim_ids(archive_narratives_dir)
        if stale_claim_ids:
            return DoctorCheck(
                "Claim Freshness",
                "warn",
                f"Latest confirmed issue {issue_number:03d} cites {len(stale_claim_ids)} stale claim(s): "
                + ", ".join(stale_claim_ids[:3]),
                metadata={
                    "issue_number": issue_number,
                    "stale_claim_ids": list(stale_claim_ids[:20]),
                    "evidence_source": "archive_accepted_proposals",
                },
            )
        if archive_narratives_dir.exists():
            return DoctorCheck(
                "Claim Freshness",
                "ok",
                f"Latest confirmed issue {issue_number:03d} has no stale claim citations in archived accepted proposal evidence.",
                metadata={
                    "issue_number": issue_number,
                    "stale_claim_ids": [],
                    "evidence_source": "archive_accepted_proposals",
                },
            )
    narratives_root = programs_root / program_id / "narratives"
    latest_issue_with_proposals: int | None = None
    if not narratives_root.exists():
        return None
    for path in narratives_root.glob("issue_*/proposals.jsonl"):
        try:
            discovered_issue = int(path.parent.name.removeprefix("issue_"))
        except ValueError:
            continue
        latest_issue_with_proposals = discovered_issue if latest_issue_with_proposals is None else max(latest_issue_with_proposals, discovered_issue)
    if latest_issue_with_proposals is None:
        return None
    stale_claim_ids = load_stale_claim_ids(program_id, latest_issue_with_proposals, programs_root=programs_root)
    if not stale_claim_ids:
        return DoctorCheck(
            "Claim Freshness",
            "ok",
            f"Latest proposal-backed issue {latest_issue_with_proposals:03d} has no stale claim citations in persisted section evidence.",
            metadata={
                "issue_number": latest_issue_with_proposals,
                "stale_claim_ids": [],
                "evidence_source": "live_proposals",
            },
        )
    return DoctorCheck(
        "Claim Freshness",
        "warn",
        f"Latest proposal-backed issue {latest_issue_with_proposals:03d} cites {len(stale_claim_ids)} stale claim(s): "
        + ", ".join(stale_claim_ids[:3]),
        metadata={
            "issue_number": latest_issue_with_proposals,
            "stale_claim_ids": list(stale_claim_ids[:20]),
            "evidence_source": "live_proposals",
        },
    )


def coverage_range_check(
    program_id: str | None,
    raw_program: dict[str, Any] | None,
    programs_root: Path,
) -> DoctorCheck | None:
    if program_id is None or not isinstance(raw_program, dict):
        return None
    raw_start_date = raw_program.get("start_date")
    if raw_start_date is None and isinstance(raw_program.get("program"), dict):
        raw_start_date = raw_program["program"].get("start_date")
    if raw_start_date is None:
        return None
    try:
        program_start_date = _parse_optional_date(raw_start_date)
    except ValueError:
        return DoctorCheck(
            "Coverage Range",
            "warn",
            f"programs/{program_id}/program.yaml has invalid start_date {raw_start_date!r}; coverage-range backfill check skipped.",
            metadata={"raw_start_date": raw_start_date},
        )
    if program_start_date is None:
        return None
    projection_path = get_current_projection_path(program_id, programs_root=programs_root)
    if not projection_path.exists():
        return None
    projection = canonical_projection_dump(projection_path)
    projection_meta = next(iter(projection.get("projection_meta", [])), None)
    if not isinstance(projection_meta, dict):
        return None
    coverage_earliest = _parse_optional_utc_datetime(projection_meta.get("coverage_earliest"))
    if coverage_earliest is None:
        return None
    threshold_date = program_start_date + timedelta(days=60)
    metadata = {
        "program_start_date": program_start_date.isoformat(),
        "coverage_earliest": coverage_earliest.isoformat(),
        "projection_path": str(projection_path),
        "threshold_date": threshold_date.isoformat(),
    }
    if coverage_earliest.date() > threshold_date:
        gap_days = (coverage_earliest.date() - program_start_date).days
        return DoctorCheck(
            "Coverage Range",
            "warn",
            f"Coverage earliest {coverage_earliest.date().isoformat()} trails program start date {program_start_date.isoformat()} by {gap_days} day(s); potential ledger backfill gap.",
            metadata=metadata,
        )
    return DoctorCheck(
        "Coverage Range",
        "ok",
        f"Coverage earliest {coverage_earliest.date().isoformat()} is within 60 day(s) of program start date {program_start_date.isoformat()}.",
        metadata=metadata,
    )


def degraded_confirm_check(
    program_id: str | None,
    edition_name: str,
    archive_root: Path,
    programs_root: Path,
) -> DoctorCheck | None:
    if program_id is None:
        return None
    latest_confirmed = find_latest_confirmed_entry(read_archive_index(edition_name, archive_root=archive_root))
    if latest_confirmed is None:
        return None

    issue_number = latest_confirmed.issue_number
    snapshot_dir = get_snapshot_dir(program_id, programs_root=programs_root)
    snapshot_manifests = sorted(snapshot_dir.glob(f"issue_{issue_number:03d}-*.manifest.json")) if snapshot_dir.exists() else []
    snapshot_hashes: set[str] = set()
    for manifest_path in snapshot_manifests:
        snapshot_path = manifest_path.parent / manifest_path.name.replace(".manifest.json", ".sqlite3")
        if not snapshot_path.exists():
            continue
        try:
            manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        snapshot_hash = manifest_payload.get("snapshot_hash")
        if isinstance(snapshot_hash, str) and snapshot_hash.strip():
            snapshot_hashes.add(snapshot_hash.strip())

    hardlock_events = [
        event
        for event in read_events(program_id, programs_root=programs_root)
        if event.event_type == "operator.baseline_hardlock.v1" and event.payload.get("issue_number") == issue_number
    ]
    hardlock_hashes = {
        str(event.payload.get("snapshot_hash")).strip()
        for event in hardlock_events
        if isinstance(event.payload.get("snapshot_hash"), str) and str(event.payload.get("snapshot_hash")).strip()
    }

    missing_parts: list[str] = []
    if not snapshot_hashes:
        missing_parts.append("projection snapshot")
    if not hardlock_events:
        missing_parts.append("baseline hardlock event")
    if not missing_parts and hardlock_hashes.isdisjoint(snapshot_hashes):
        missing_parts.append("matching snapshot/hash linkage")

    metadata = {
        "issue_number": issue_number,
        "snapshot_dir": str(snapshot_dir),
        "snapshot_hashes": sorted(snapshot_hashes),
        "hardlock_event_ids": [event.event_id for event in hardlock_events],
        "hardlock_snapshot_hashes": sorted(hardlock_hashes),
    }
    if missing_parts:
        return DoctorCheck(
            "Degraded Confirm",
            "fail",
            f"Latest confirmed issue {issue_number:03d} is missing ledger post-confirm artifact(s): {', '.join(missing_parts)}. Run `vertex ledger replay --program {program_id}` after restoring the issue snapshot/hardlock step.",
            metadata=metadata,
        )
    return DoctorCheck(
        "Degraded Confirm",
        "ok",
        f"Latest confirmed issue {issue_number:03d} has a ledger projection snapshot and matching baseline hardlock event.",
        metadata=metadata,
    )


def ledger_health_check(program_id: str | None, programs_root: Path) -> DoctorCheck | None:
    if program_id is None:
        return None
    events = read_events(program_id, programs_root=programs_root)
    lock_health_check = _field_lock_health_summary(program_id, programs_root)
    gap_health_check = _gap_health_summary(program_id, programs_root)
    projection_schema_check = _projection_schema_health_summary(program_id, programs_root)
    missing_vault_refs = _refs_missing_required_vault_hash(events)
    stale_operator_assertions = _stale_operator_assertions_without_ttl(events)
    if not events:
        if lock_health_check is not None:
            return lock_health_check
        if gap_health_check is not None:
            return gap_health_check
        if projection_schema_check is not None:
            return projection_schema_check
        return DoctorCheck("Ledger Health", "ok", "No ledger events recorded yet.")

    subsequent_relocks: set[str] = set()
    dangling_unlocks: list[str] = []
    for event in reversed(events):
        if event.event_type not in {"operator.field_lock.v1", "operator.field_unlock.v1"}:
            continue
        session_id = event.payload.get("override_session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            continue
        if event.event_type == "operator.field_lock.v1":
            subsequent_relocks.add(session_id)
            continue
        if session_id not in subsequent_relocks:
            dangling_unlocks.append(session_id)

    if dangling_unlocks:
        listed = ", ".join(sorted(dict.fromkeys(dangling_unlocks))[:3])
        detail = f"Dangling unlock session(s) with no subsequent re-lock: {listed}"
        metadata: dict[str, object] = {"dangling_unlock_session_ids": sorted(dict.fromkeys(dangling_unlocks))}
        if lock_health_check is not None:
            detail = detail + "; " + lock_health_check.detail
            metadata["expired_field_locks"] = lock_health_check.metadata.get("expired_field_locks", []) if lock_health_check.metadata else []
            metadata["expiring_field_locks"] = lock_health_check.metadata.get("expiring_field_locks", []) if lock_health_check.metadata else []
        if gap_health_check is not None:
            detail = detail + "; " + gap_health_check.detail
            metadata["unacknowledged_gap_ids"] = gap_health_check.metadata.get("unacknowledged_gap_ids", []) if gap_health_check.metadata else []
        if projection_schema_check is not None:
            detail = detail + "; " + projection_schema_check.detail
            metadata["projection_schema_version"] = projection_schema_check.metadata.get("projection_schema_version") if projection_schema_check.metadata else None
            metadata["engine_schema_version"] = projection_schema_check.metadata.get("engine_schema_version") if projection_schema_check.metadata else None
        if missing_vault_refs:
            detail = detail + "; external-origin ref(s) missing required vault_hash: " + ", ".join(
                f"{item['event_type']}:{item['ref_role']}" for item in missing_vault_refs[:3]
            )
            metadata["missing_vault_hash_refs"] = missing_vault_refs[:20]
        if stale_operator_assertions:
            detail = detail + "; stale operator assertion(s) missing TTL: " + ", ".join(
                f"{item['event_type']} ({item['age_days']}d)" for item in stale_operator_assertions[:3]
            )
            metadata["stale_operator_assertions"] = stale_operator_assertions[:20]
        return DoctorCheck(
            "Ledger Health",
            "fail",
            detail,
            metadata=metadata,
        )
    if missing_vault_refs:
        return DoctorCheck(
            "Ledger Health",
            "fail",
            "External-origin ref(s) missing required vault_hash: "
            + ", ".join(f"{item['event_type']}:{item['ref_role']}" for item in missing_vault_refs[:3]),
            metadata={"missing_vault_hash_refs": missing_vault_refs[:20]},
        )
    warning_details = [check.detail for check in (lock_health_check, gap_health_check, projection_schema_check) if check is not None]
    if stale_operator_assertions:
        warning_details.append(
            "stale operator assertion(s) missing TTL: "
            + ", ".join(f"{item['event_type']} ({item['age_days']}d)" for item in stale_operator_assertions[:3])
        )
    if warning_details:
        metadata = {
            "expired_field_locks": lock_health_check.metadata.get("expired_field_locks", []) if lock_health_check and lock_health_check.metadata else [],
            "expiring_field_locks": lock_health_check.metadata.get("expiring_field_locks", []) if lock_health_check and lock_health_check.metadata else [],
            "unacknowledged_gap_ids": gap_health_check.metadata.get("unacknowledged_gap_ids", []) if gap_health_check and gap_health_check.metadata else [],
            "projection_schema_version": projection_schema_check.metadata.get("projection_schema_version") if projection_schema_check and projection_schema_check.metadata else None,
            "engine_schema_version": projection_schema_check.metadata.get("engine_schema_version") if projection_schema_check and projection_schema_check.metadata else None,
            "stale_operator_assertions": stale_operator_assertions[:20],
        }
        return DoctorCheck("Ledger Health", "warn", "; ".join(warning_details), metadata=metadata)
    return DoctorCheck("Ledger Health", "ok", "No dangling unlock sessions detected.")
