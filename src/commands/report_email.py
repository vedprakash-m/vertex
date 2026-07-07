from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from src.core.eml_writer import build_eml_bytes
from src.core.jinja_filters import risk_label
from src.core.view_models import HealthSummary, Top3Item
from src.m365.graph_send_client import GraphMailMessage, GraphSendClient


def _build_email_subject(
    title: str,
    health: HealthSummary,
    subject_signal: str,
) -> str:
    state_label = "HEALTHY" if health.high_count == 0 and health.medium_count == 0 else risk_label(health.overall_risk).upper()
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
    lead = bluf or (top_items[0].text if top_items else "No new leadership decisions this week.")
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
) -> bytes:
    return build_eml_bytes(
        to=(bundle.config.author.email,),
        cc=(),
        subject=_resolve_email_subject(
            suggested_subject=suggested_subject,
            default_subject=bundle.config.edition.name if suggested_subject.strip() else bundle.config.edition.name,
        ),
        html_body=html_body,
        text_body=markdown_body,
        from_display_name=bundle.config.author.display_name,
        from_email=bundle.config.author.email,
        generated_at=generated_at,
        mark_as_draft=True,
    )


def _send_draft_email(message: GraphMailMessage) -> None:
    GraphSendClient().send_mail(message)


def _build_draft_email_sender() -> Callable[[GraphMailMessage], None]:
    return _send_draft_email