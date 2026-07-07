from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.calibration_engine import compute_calibration_for_edition, write_calibration_prior
from src.core.exceptions import ConfirmError
from src.core.trusted_baseline_store import advance_trusted_baseline, record_untrusted_issue


def apply_baseline_followthrough(
    *,
    edition_name: str,
    issue_number: int,
    confirmed_at: datetime,
    confirmed_by: str | None,
    warnings: tuple[str, ...],
    archive_root: Path,
    editions_root: Path,
    programs_root: Path,
    resolved_v2: Any,
    items: tuple[Any, ...],
    untrusted: bool,
    untrusted_reason: str | None,
) -> tuple[str, ...]:
    if resolved_v2 is not None and resolved_v2.edition.calibration_pilot:
        try:
            calibrations = compute_calibration_for_edition(
                resolved_v2.program.id,
                items=items,
                programs_root=programs_root,
                as_of_iso=confirmed_at.isoformat(),
            )
            write_calibration_prior(
                edition_name,
                issue_number,
                calibrations,
                archive_root=archive_root,
            )
        except Exception as exc:
            warnings = warnings + (f"CalibrationPrior skipped: {exc}",)

    if resolved_v2 is not None and untrusted:
        try:
            record_untrusted_issue(
                edition_name,
                issue_number,
                recorded_at=confirmed_at,
                recorded_by=confirmed_by,
                reason=(untrusted_reason.strip() if untrusted_reason is not None else ""),
                editions_root=editions_root,
                programs_root=programs_root,
            )
        except Exception as exc:
            raise ConfirmError(
                f"Archived issue {issue_number:03d}, but failed to record the untrusted baseline marker: {exc}"
            ) from exc
    elif resolved_v2 is not None:
        try:
            advance_trusted_baseline(
                edition_name,
                issue_number,
                established_at=confirmed_at,
                established_by=confirmed_by,
                editions_root=editions_root,
                programs_root=programs_root,
            )
        except Exception as exc:
            raise ConfirmError(
                f"Archived issue {issue_number:03d}, but failed to advance the trusted baseline: {exc}"
            ) from exc

    return warnings
