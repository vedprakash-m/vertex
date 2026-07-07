from __future__ import annotations

from pathlib import Path
from typing import Any

from src.commands.doctor_checks.models import DoctorCheck
from src.core.knowledge_store import load_program_knowledge


def run_hygiene_nudge_check(*, resolved: Any, programs_root: Path) -> DoctorCheck | None:
    if resolved is None or str(resolved.edition.type).strip().lower() != "nudge":
        return None

    raw_hygiene = resolved.raw_edition.get("hygiene")
    if not isinstance(raw_hygiene, dict):
        return DoctorCheck("Hygiene Nudge", "fail", "Nudge edition is missing the hygiene configuration block.")

    stale_business_days = raw_hygiene.get("stale_business_days", 3)
    if not isinstance(stale_business_days, int) or stale_business_days < 1:
        return DoctorCheck(
            "Hygiene Nudge",
            "fail",
            "hygiene.stale_business_days must be a positive integer for nudge editions.",
        )

    send_day = str(resolved.raw_edition.get("send_day") or "").strip().lower()
    distribution = resolved.raw_edition.get("distribution")
    author = resolved.raw_edition.get("author")
    distribution_channels = tuple(
        str(entry).strip().lower()
        for entry in (distribution.get("channels", []) if isinstance(distribution, dict) else ())
        if str(entry).strip()
    )
    author_email = str(author.get("email") or "").strip().lower() if isinstance(author, dict) else ""

    coverage_alerts_enabled = bool(raw_hygiene.get("workstream_coverage_alerts", True))
    metadata: dict[str, Any] = {
        "stale_business_days": stale_business_days,
        "send_day": send_day or None,
        "distribution_channels": list(distribution_channels),
        "author_email": author_email or None,
        "coverage_alerts_enabled": coverage_alerts_enabled,
    }

    valid_send_days = {"monday", "tuesday", "wednesday", "thursday", "friday"}
    config_problems: list[str] = []
    if send_day and send_day not in valid_send_days:
        config_problems.append(f"send_day '{send_day}' is not a valid weekday (mon–fri)")
    if "email" not in distribution_channels:
        config_problems.append("distribution.channels must include email")
    if not author_email:
        config_problems.append("author.email is required for fallback hygiene routing")
    if config_problems:
        return DoctorCheck(
            "Hygiene Nudge",
            "warn",
            f"stale_business_days={stale_business_days}; {'; '.join(config_problems)}.",
            metadata=metadata,
        )

    if not coverage_alerts_enabled:
        return DoctorCheck(
            "Hygiene Nudge",
            "ok",
            f"stale_business_days={stale_business_days}; workstream coverage alerts disabled.",
            metadata=metadata,
        )

    knowledge = load_program_knowledge(resolved.program.id, programs_root=programs_root)
    coverage_workstreams: list[str] = []
    unroutable_workstreams: list[str] = []
    for workstream in resolved.workstreams:
        signal_sources = workstream.signal_sources
        if signal_sources is None:
            continue
        coverage = signal_sources.ado_coverage
        if coverage is not None and coverage.suppress_coverage_alert:
            continue
        coverage_workstreams.append(workstream.id)
        _, lead_email = resolve_hygiene_workstream_lead_contact(workstream=workstream, knowledge=knowledge)
        if lead_email is None:
            unroutable_workstreams.append(workstream.id)

    metadata["coverage_workstreams"] = coverage_workstreams
    metadata["missing_workstream_leads"] = unroutable_workstreams
    if not coverage_workstreams:
        return DoctorCheck(
            "Hygiene Nudge",
            "ok",
            f"stale_business_days={stale_business_days}; no workstreams currently author coverage-alert signal sources.",
            metadata=metadata,
        )

    if unroutable_workstreams:
        return DoctorCheck(
            "Hygiene Nudge",
            "warn",
            (
                f"stale_business_days={stale_business_days}; coverage alerts enabled for {len(coverage_workstreams)} workstream"
                f"{'s' if len(coverage_workstreams) != 1 else ''}; missing lead email resolution for {', '.join(unroutable_workstreams)}."
            ),
            metadata=metadata,
        )

    return DoctorCheck(
        "Hygiene Nudge",
        "ok",
        (
            f"stale_business_days={stale_business_days}; coverage alerts enabled for {len(coverage_workstreams)} workstream"
            f"{'s' if len(coverage_workstreams) != 1 else ''}; all coverage-alert workstreams have routable leads."
        ),
        metadata=metadata,
    )


def resolve_hygiene_workstream_lead_contact(*, workstream: Any, knowledge: Any) -> tuple[str | None, str | None]:
    if workstream.dri_email:
        email = workstream.dri_email.strip().lower()
        return email.split("@", 1)[0], email
    if workstream.alternate_owner:
        normalized = workstream.alternate_owner.strip().lower()
        for person in getattr(knowledge, "people_directory", ()):
            alias = str(getattr(person, "alias", "") or "").strip().lower()
            email = str(getattr(person, "email", "") or "").strip().lower()
            if alias == normalized and email:
                return alias, email
    return None, None
