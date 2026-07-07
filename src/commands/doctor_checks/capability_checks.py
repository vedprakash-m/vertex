from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from src.commands.doctor_checks.models import DoctorCheck
from src.core.capability_status import latest_program_capability_reviewed_on, load_program_capability_status


def run_capability_review_check(
    program_id: str | None,
    programs_root: Path,
    *,
    warn_on_incomplete: bool = False,
) -> DoctorCheck | None:
    if program_id is None:
        return None
    status_path = programs_root / program_id / "capability_status.yaml"
    if not status_path.exists():
        return DoctorCheck("Capability Reviews", "ok", "No persisted capability review state recorded yet.")

    document = yaml.safe_load(status_path.read_text(encoding="utf-8")) or {}
    raw_capabilities = document.get("capabilities")
    if raw_capabilities is None:
        raw_capabilities = ()
    authored_capability_ids = tuple(
        str(entry.get("id") or "").strip()
        for entry in raw_capabilities
        if isinstance(entry, dict) and str(entry.get("id") or "").strip()
    )
    if not authored_capability_ids:
        return DoctorCheck("Capability Reviews", "ok", "No authored capability review entries recorded yet.")

    statuses = tuple(
        status
        for status in load_program_capability_status(program_id, programs_root=programs_root)
        if status.capability_id in authored_capability_ids
    )
    latest_reviewed_on = latest_program_capability_reviewed_on(statuses)
    incomplete_statuses = tuple(status for status in statuses if status.status != "complete")
    missing_review_labels = tuple(status.label for status in statuses if status.last_reviewed_on is None)
    latest_detail = capability_review_latest_detail(latest_reviewed_on)
    metadata = {
        "authored_capability_count": len(statuses),
        "latest_reviewed_on": latest_reviewed_on.isoformat() if latest_reviewed_on is not None else None,
        "warn_on_incomplete": warn_on_incomplete,
        "missing_review_labels": list(missing_review_labels),
        "incomplete_capabilities": [
            {
                "capability_id": status.capability_id,
                "label": status.label,
                "status": status.status,
                "last_reviewed_on": status.last_reviewed_on.isoformat() if status.last_reviewed_on is not None else None,
            }
            for status in incomplete_statuses
        ],
    }

    if missing_review_labels:
        missing_detail = ", ".join(missing_review_labels)
        return DoctorCheck(
            "Capability Reviews",
            "warn",
            f"Persisted capability review dates missing for {missing_detail}.{latest_detail}",
            metadata=metadata,
        )

    if not incomplete_statuses:
        return DoctorCheck(
            "Capability Reviews",
            "ok",
            f"All persisted capabilities are complete.{latest_detail}",
            metadata=metadata,
        )

    status_summary = "; ".join(
        f"{status.label} {status.status.replace('_', ' ')}"
        for status in incomplete_statuses
    )
    return DoctorCheck(
        "Capability Reviews",
        "warn" if warn_on_incomplete else "ok",
        (
            f"Persisted capability state still incomplete: {status_summary}.{latest_detail}"
            if warn_on_incomplete
            else f"Tracked capability state: {status_summary}.{latest_detail}"
        ),
        metadata=metadata,
    )


def capability_review_latest_detail(latest_reviewed_on: date | None) -> str:
    if latest_reviewed_on is None:
        return " No capability review date recorded."
    return f" Latest review: {latest_reviewed_on.isoformat()}."
