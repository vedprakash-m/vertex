from __future__ import annotations

from pathlib import Path
import re

from src.commands.doctor_checks.models import DoctorCheck
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core import journal
from src.core.edition_resolver import resolve_edition
from src.core.review_status_store import load_review_status
from src.core.snapshot_store import get_archive_root
from src.core.trusted_baseline_store import load_trusted_baseline


def consistency_check(
    edition: str,
    *,
    archive_root: Path,
    reports_root: Path,
    editions_root: Path,
    programs_root: Path,
) -> DoctorCheck:
    archive_index = read_archive_index(edition, archive_root)
    latest_confirmed = find_latest_confirmed_entry(archive_index)
    try:
        baseline = load_trusted_baseline(
            edition,
            editions_root=editions_root,
            programs_root=programs_root,
        )
    except ValueError as error:
        return DoctorCheck("Consistency", "fail", f"Trusted baseline is unreadable: {error}")

    try:
        active_review = load_review_status(edition, reports_root=reports_root)
    except ValueError as error:
        return DoctorCheck("Consistency", "fail", f"Active review status is unreadable: {error}")

    manifest_issue = latest_confirmed.issue_number if latest_confirmed is not None else None
    archived_review_issue = latest_archived_review_issue(edition, archive_root=archive_root)
    active_review_issue = active_review.issue_number if active_review is not None else None

    failures: list[str] = []
    if baseline is None:
        failures.append("trusted baseline is missing")
    elif baseline.trusted_issue_number is None:
        failures.append("trusted baseline does not name a confirmed issue")
    elif manifest_issue is None:
        failures.append("archive index has no confirmed issue")
    elif baseline.trusted_issue_number != manifest_issue:
        failures.append(
            f"trusted baseline issue {baseline.trusted_issue_number:03d} does not match latest confirmed archive issue {manifest_issue:03d}"
        )

    if manifest_issue is not None and archived_review_issue is not None and archived_review_issue != manifest_issue:
        failures.append(
            f"latest archived review state issue {archived_review_issue:03d} does not match latest confirmed archive issue {manifest_issue:03d}"
        )

    if active_review_issue is not None and manifest_issue is not None and active_review_issue not in {manifest_issue, manifest_issue + 1}:
        failures.append(
            f"active review state issue {active_review_issue:03d} is not aligned to confirmed issue {manifest_issue:03d} or next draft {manifest_issue + 1:03d}"
        )

    # WS-20: no orphaned vertex/ado_update signals assertion
    resolved = resolve_edition(edition, editions_root=editions_root, programs_root=programs_root)
    if resolved is not None:
        program_id = resolved.paths.program_id
        ado_signals = [s for s in journal.read_signals(program_id, programs_root=programs_root) if s.source == "vertex/ado_update"]
        if ado_signals:
            review_decisions = journal.load_latest_review_decisions(program_id, programs_root=programs_root)
            orphaned = [s for s in ado_signals if s.id not in review_decisions]
            if orphaned:
                oldest_ts = min(s.timestamp for s in orphaned)
                failures.append(
                    f"{len(orphaned)} orphaned vertex/ado_update signal(s) without review decision (oldest: {oldest_ts.date()})"
                )

    metadata = {
        "trusted_issue_number": (baseline.trusted_issue_number if baseline is not None else None),
        "latest_confirmed_issue": manifest_issue,
        "latest_archived_review_issue": archived_review_issue,
        "active_review_issue": active_review_issue,
    }
    if failures:
        return DoctorCheck("Consistency", "fail", "; ".join(failures), metadata=metadata)

    detail_parts = [f"trusted baseline, archive, and review state agree on issue {manifest_issue:03d}"]
    if active_review_issue == (manifest_issue + 1 if manifest_issue is not None else None):
        detail_parts.append(f"active draft review is staged for issue {active_review_issue:03d}")
    return DoctorCheck("Consistency", "ok", "; ".join(detail_parts), metadata=metadata)


def latest_archived_review_issue(edition: str, *, archive_root: Path) -> int | None:
    review_dir = get_archive_root(edition, archive_root) / "review"
    if not review_dir.exists():
        return None
    issue_numbers: list[int] = []
    for path in review_dir.glob("issue_*.review.yaml"):
        match = re.fullmatch(r"issue_(\d+)\.review\.yaml", path.name)
        if match is None:
            continue
        issue_numbers.append(int(match.group(1)))
    if not issue_numbers:
        return None
    return max(issue_numbers)
