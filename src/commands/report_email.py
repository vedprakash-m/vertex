from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Callable, Sequence

from src.core.archive_store import read_archive_index
from src.core.communication_budget import (
    CommunicationBudgetFinding,
    build_communication_audience_manifest,
    communication_budget_policy_from_edition,
    evaluate_communication_budget,
)
from src.core.edition_resolver import resolve_edition
from src.core.eml_writer import build_eml_bytes
from src.core.exceptions import StateError
from src.core.jinja_filters import risk_label
from src.core.models import DRISummary, NotifyPreview
from src.core.notification_state_store import load_confirmed_notification_events
from src.core.view_models import HealthSummary, Top3Item
from src.core.vitality_reporting import vitality_settings_from_program
from src.m365.graph_send_client import GraphMailMessage
from src.m365.outbound_dispatch import dispatch_email_via_outbox


def _build_email_subject(
    title: str,
    health: HealthSummary,
    subject_signal: str,
) -> str:
    state_label = "HEALTHY" if health.health_state == "HEALTHY" else risk_label(health.overall_risk).upper()
    trajectory_label = health.trajectory.upper()
    prefix = title
    if len(prefix) > 42:
        prefix = prefix[:42].rstrip()
    subject = f"{prefix}: {state_label}, {trajectory_label} - {subject_signal}".strip()
    return subject[:80].rstrip(" -")


def _build_email_preheader(
    health: HealthSummary,
    bluf: str | None,
    top_items: tuple[Top3Item, ...],
) -> str:
    direction_phrase = {
        "improving": "improving",
        "degrading": "worsening",
        "stable": "stable",
    }[health.trajectory]
    lead = bluf or (top_items[0].text if top_items else (
        health.leadership_ask if health.needs_input_count or not health.total_count
        else "No new leadership decisions this week."
    ))
    if health.risk_load_incomplete:
        return f"{health.high_count} High risks; assessment coverage needs input. {lead}"[:150].rstrip()
    preheader = (
        f"{health.high_count} High risks, Risk Load {health.risk_load:.1f} {direction_phrase}. {lead}"
    )
    return preheader[:150].rstrip()


def _resolve_email_subject(
    *,
    suggested_subject: str | None,
    default_subject: str,
) -> str:
    if suggested_subject is not None and suggested_subject.strip():
        return suggested_subject.strip()
    return default_subject


def _distribution_to(bundle: Any) -> tuple[str, ...]:
    if bundle.config.distribution.to:
        return bundle.config.distribution.to
    return (bundle.config.author.email,)


def _build_draft_email_message(bundle: Any, artifacts: Any) -> GraphMailMessage:
    return GraphMailMessage(
        to=(bundle.config.author.email,),
        cc=(),
        subject=_resolve_email_subject(
            suggested_subject=None,
            default_subject=artifacts.title or artifacts.report.edition_name,
        ),
        html_body=artifacts.html_body,
    )


def _build_preview_eml_bytes(
    bundle: Any,
    *,
    issue_number: int,
    as_of: datetime,
    html_body: str,
    markdown_body: str,
    suggested_subject: str,
    generated_at: datetime,
    blocking_gate_ids: tuple[str, ...] = (),
) -> bytes:
    blocked = bool(blocking_gate_ids)
    subject = _resolve_email_subject(
        suggested_subject=suggested_subject,
        default_subject=bundle.config.edition.name if suggested_subject.strip() else bundle.config.edition.name,
    )
    if blocked:
        subject = f"[BLOCKED DRAFT] {subject}"
        gate_summary = ", ".join(blocking_gate_ids)
        warning = f"BLOCKED DRAFT — publication is prohibited until quality gates pass: {gate_summary}."
        markdown_body = f"{warning}\n\n{markdown_body}"
        html_body = f"<p><strong>{warning}</strong></p>{html_body}"
    return build_eml_bytes(
        to=() if blocked else (bundle.config.author.email,),
        cc=(),
        subject=subject,
        html_body=html_body,
        text_body=markdown_body,
        from_display_name=bundle.config.author.display_name,
        from_email=bundle.config.author.email,
        generated_at=generated_at,
        mark_as_draft=True,
    )


def _send_draft_email(message: GraphMailMessage) -> None:
    dispatch_email_via_outbox(message)


def _build_draft_email_sender() -> Callable[[GraphMailMessage], None]:
    return _send_draft_email


def _ensure_aware_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None or ts.utcoffset() is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def format_communication_budget_observations(
    findings: Sequence[CommunicationBudgetFinding],
) -> tuple[str, ...]:
    """Format observe-only communication budget findings without mutating or blocking."""
    return tuple(
        f"[observe] Communication budget ({finding.code}): {finding.audience_id} - {finding.detail}"
        for finding in findings
    )


def assess_notify_communication_budgets(
    *,
    edition_name: str,
    previews: Sequence[NotifyPreview],
    dri_summaries: Sequence[DRISummary],
    programs_root: Path,
    now: datetime,
    cooldown_days: int | None = None,
    max_asks: int | None = None,
) -> tuple[CommunicationBudgetFinding, ...]:
    """Evaluate per-DRI ask budget and cooldown in observe-only mode for notify sends."""
    resolved = resolve_edition(edition_name, programs_root=programs_root)
    fallback_cooldown = (
        vitality_settings_from_program(resolved.raw_program).nudge_cooldown_days
        if resolved is not None
        else 14
    )
    policy = communication_budget_policy_from_edition(
        resolved.raw_edition if resolved is not None else None,
        default_cooldown_days=fallback_cooldown,
    )
    resolved_cooldown = cooldown_days if cooldown_days is not None else policy.cooldown_days
    resolved_max_asks = max_asks if max_asks is not None else policy.max_asks
    events = load_confirmed_notification_events(edition=edition_name, programs_root=programs_root)
    summaries_by_email = {
        summary.dri_email.strip().lower(): summary
        for summary in dri_summaries
        if summary.dri_email.strip().lower() not in {"", "unassigned"}
    }
    findings: list[CommunicationBudgetFinding] = []
    aware_now = _ensure_aware_utc(now)
    for index, preview in enumerate(previews):
        summary = next(
            (
                summaries_by_email[recipient.strip().lower()]
                for recipient in preview.to
                if recipient.strip().lower() in summaries_by_email
            ),
            None,
        )
        audience_id = (
            summary.dri_email
            if summary is not None
            else (", ".join(preview.to) if preview.to else f"recipient_{index + 1}")
        )
        preview_asks = tuple(
            line.strip()
            for line in preview.md_body.splitlines()
            if line.strip().startswith("- ADO#")
        )
        asks = preview_asks or (
            [f"ADO#{item.work_item_id}" for item in summary.items]
            if summary is not None
            else _notify_material_asks(preview)
        )
        prior_sends = [
            _ensure_aware_utc(event.confirmed_at)
            for event in events
            if any(
                note.dri_email.lower() == audience_id.lower()
                or audience_id.lower() in {addr.lower() for addr in note.to}
                for note in event.notifications
            )
        ]
        manifest = build_communication_audience_manifest(
            audience=tuple(preview.to),
            asks=asks,
        )
        findings.extend(
            evaluate_communication_budget(
                audience_id=manifest.audience_id,
                asks=manifest.material_asks,
                prior_sends=prior_sends,
                cooldown_days=resolved_cooldown,
                now=aware_now,
                max_asks=resolved_max_asks,
            )
        )
    return tuple(sorted(findings, key=lambda f: (f.code, f.audience_id, f.detail)))


def _notify_material_asks(preview: NotifyPreview) -> tuple[str, ...]:
    content = f"{preview.md_body}\n{preview.html_body}"
    refs = tuple(dict.fromkeys(re.findall(r"\bADO#\d+\b", content)))
    if refs:
        return refs
    count_match = re.search(r"\b(\d+)\s+items?\s+need\b", preview.subject, re.IGNORECASE)
    if count_match is None:
        return ()
    return tuple(
        f"NOTIFY_ITEM:{index}"
        for index in range(1, int(count_match.group(1)) + 1)
    )


def enforce_notify_communication_budgets(
    *,
    edition_name: str,
    previews: Sequence[NotifyPreview],
    dri_summaries: Sequence[DRISummary],
    programs_root: Path,
    now: datetime,
) -> tuple[CommunicationBudgetFinding, ...]:
    findings = assess_notify_communication_budgets(
        edition_name=edition_name,
        previews=previews,
        dri_summaries=dri_summaries,
        programs_root=programs_root,
        now=now,
    )
    if findings:
        detail = "; ".join(
            f"{finding.audience_id} {finding.code}: {finding.detail}"
            for finding in findings
        )
        raise StateError(f"Communication budget blocks notification send: {detail}")
    return findings


def assess_report_email_communication_budget(
    *,
    edition_name: str,
    audience_id: str,
    top_items: Sequence[Top3Item],
    archive_root: Path,
    now: datetime,
    cooldown_days: int = 7,
    max_asks: int | None = None,
) -> tuple[CommunicationBudgetFinding, ...]:
    """Evaluate leadership ask budget and weekly cooldown in observe-only mode for report email."""
    index = read_archive_index(edition_name, archive_root=archive_root)
    prior_sends = [
        _ensure_aware_utc(entry.generated_at)
        for entry in index.issues
        if entry.kind == "confirmed"
    ]
    asks = [f"TOP3:{item.text}" for item in top_items]
    manifest = build_communication_audience_manifest(
        audience=tuple(part.strip() for part in audience_id.split(",") if part.strip()),
        asks=asks,
    )
    return evaluate_communication_budget(
        audience_id=manifest.audience_id,
        asks=manifest.material_asks,
        prior_sends=prior_sends,
        cooldown_days=cooldown_days,
        now=_ensure_aware_utc(now),
        **({"max_asks": max_asks} if max_asks is not None else {}),
    )


def observe_report_email_communication_budget(
    *,
    bundle: Any,
    edition_name: str,
    top_items: Sequence[Top3Item],
    archive_root: Path,
    manifest_metadata: dict[str, Any],
    now: datetime,
    cooldown_days: int | None = None,
    enforce: bool = False,
) -> tuple[CommunicationBudgetFinding, ...]:
    """Record observe-only report-email communication budget findings onto manifest metadata."""
    audience = _distribution_to(bundle)
    config_cooldown = getattr(bundle.config, "communication_cooldown_days", None)
    policy = communication_budget_policy_from_edition(
        {
            "communication_budget": {
                "max_asks": getattr(bundle.config, "communication_max_asks", 5),
                "cooldown_days": 7 if config_cooldown is None else config_cooldown,
            }
        },
        default_cooldown_days=7,
    )
    audience_id = ", ".join(audience)
    findings = assess_report_email_communication_budget(
        edition_name=edition_name,
        audience_id=audience_id,
        top_items=top_items,
        archive_root=archive_root,
        now=now,
        cooldown_days=cooldown_days if cooldown_days is not None else policy.cooldown_days,
        max_asks=policy.max_asks,
    )
    manifest_metadata["communication_budget_observations"] = [
        {"code": f.code, "audience_id": f.audience_id, "detail": f.detail}
        for f in findings
    ]
    if enforce and findings:
        raise StateError(
            "Communication budget blocks report draft send: "
            + "; ".join(
                f"{finding.audience_id} {finding.code}: {finding.detail}"
                for finding in findings
            )
        )
    return findings
