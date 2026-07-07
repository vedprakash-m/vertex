from __future__ import annotations

import json
from html import escape
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import typer

from src.commands.freshness import FreshnessArtifacts, _record_confirmed_notify_run, generate_freshness_report
from src.core.config_loader import load_bundle
from src.core.edition_resolver import get_program_output_dir, PROGRAMS_ROOT
from src.core.eml_writer import build_eml_bytes, write_eml
from src.core.exceptions import AuthError, QueryError, StateError
from src.core.knowledge_store import KnowledgeStore, load_program_knowledge
from src.core.models import NotificationRecord, NotifyPreview
from src.core.models_v2 import DecisionAsk
from src.m365.adaptive_card_renderer import AdaptiveCardRenderer
from src.m365.graph_send_client import GraphMailMessage, GraphSendClient
from src.m365.teams_webhook_client import TeamsWebhookClient


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_ROOT = REPO_ROOT / "reports"
ARCHIVE_ROOT = REPO_ROOT / "archive"

NotifySender = Callable[[NotifyPreview], NotificationRecord]
NotifyCardSender = Callable[[str, str, dict[str, Any]], NotificationRecord]


@dataclass(frozen=True, slots=True)
class AdaptiveCardDraft:
    recipient_email: str
    subject: str
    path: Path
    payload: dict[str, Any]


def notify_command(
    edition: str = typer.Option("", "--edition", help="Edition used for the notify run."),
    issue: int = typer.Option(..., "--issue", min=1, help="Pending issue number that will receive notify previews."),
    channel: str = typer.Option("eml", "--channel", help="Notification channel. 'eml' writes manual-send email drafts, 'adaptive-card' posts Teams cards when a webhook is configured or writes manual-post card JSON otherwise, and 'email' uses Graph send."),
    since: str | None = typer.Option(None, "--since", help="Relative lookback window, for example 14d."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview notification emails without attempting send."),
) -> None:
    if channel not in {"eml", "adaptive-card", "email"}:
        raise typer.BadParameter("Only '--channel eml', '--channel adaptive-card', and '--channel email' are currently supported.")

    try:
        repo_root = REPORTS_ROOT.parent
        bundle = load_bundle(
            edition,
            reports_root=REPORTS_ROOT,
            programs_root=repo_root / "programs",
        )
        artifacts = build_notify_artifacts(
            edition_name=edition,
            issue_number=issue,
            since=since,
            reports_root=REPORTS_ROOT,
            archive_root=ARCHIVE_ROOT,
        )
        typer.echo(render_notify_preview_plaintext(artifacts.notify_previews))
        typer.echo(f"Markdown: {artifacts.md_path}")
        typer.echo(f"HTML: {artifacts.html_path}")

        if not artifacts.notify_previews:
            typer.echo("No notification emails to send.")
            raise typer.Exit(code=0)

        if dry_run:
            typer.echo("Dry run: no notifications sent.")
            raise typer.Exit(code=0)

        if channel == "eml":
            if not typer.confirm(
                f"Write {len(artifacts.notify_previews)} notification draft EML(s) and record this notify run?",
                default=True,
            ):
                raise typer.Exit(code=1)

            eml_paths = write_notify_preview_emls(
                edition_name=edition,
                issue_number=artifacts.issue_number,
                previews=artifacts.notify_previews,
                programs_root=PROGRAMS_ROOT,
                generated_at=datetime.now(timezone.utc),
            )
            notification_log_path = _record_confirmed_notify_run(
                edition_name=edition,
                issue_number=artifacts.issue_number,
                dri_summaries=artifacts.dri_summaries,
                notify_previews=artifacts.notify_previews,
                programs_root=PROGRAMS_ROOT,
                confirmed_at=datetime.now(timezone.utc),
            )
            typer.echo(f"Notification log: {notification_log_path}")
            for eml_path in eml_paths:
                typer.echo(f"EML: {eml_path}")
            typer.echo(f"Wrote {len(eml_paths)} notification draft EML(s). Send manually via Outlook.")
            raise typer.Exit(code=0)

        if channel == "adaptive-card":
            teams_webhook_url = bundle.config.m365.teams_incoming_webhook_url
            if teams_webhook_url:
                if not typer.confirm(
                    f"Send {len(artifacts.notify_previews)} adaptive card notification(s) via Teams incoming webhook and record this notify run?",
                    default=True,
                ):
                    raise typer.Exit(code=1)

                card_paths, records = send_notify_preview_adaptive_cards(
                    edition_name=edition,
                    issue_number=artifacts.issue_number,
                    dri_summaries=artifacts.dri_summaries,
                    items=artifacts.items,
                    item_urls=artifacts.item_urls,
                    programs_root=PROGRAMS_ROOT,
                    sender=_build_notify_teams_sender(teams_webhook_url),
                )
            else:
                if not typer.confirm(
                    f"Write {len(artifacts.notify_previews)} adaptive card draft JSON file(s) and record this notify run?",
                    default=True,
                ):
                    raise typer.Exit(code=1)

                card_paths = write_notify_preview_adaptive_cards(
                    edition_name=edition,
                    issue_number=artifacts.issue_number,
                    dri_summaries=artifacts.dri_summaries,
                    items=artifacts.items,
                    item_urls=artifacts.item_urls,
                    programs_root=PROGRAMS_ROOT,
                )
                records = ()
            notification_log_path = _record_confirmed_notify_run(
                edition_name=edition,
                issue_number=artifacts.issue_number,
                dri_summaries=artifacts.dri_summaries,
                notify_previews=artifacts.notify_previews,
                programs_root=PROGRAMS_ROOT,
                confirmed_at=datetime.now(timezone.utc),
            )
            typer.echo(f"Notification log: {notification_log_path}")
            for card_path in card_paths:
                typer.echo(f"CARD: {card_path}")
            if teams_webhook_url:
                typer.echo(f"Sent {len(records)} adaptive card notification(s) to Teams.")
            else:
                typer.echo(f"Wrote {len(card_paths)} adaptive card draft JSON file(s). Post manually to Teams.")
            raise typer.Exit(code=0)

        if not typer.confirm(f"Send {len(artifacts.notify_previews)} notification email(s) via Graph?", default=True):
            raise typer.Exit(code=1)

        records = send_notify_previews(artifacts.notify_previews, sender=_build_notify_email_sender())
        notification_log_path = _record_confirmed_notify_run(
            edition_name=edition,
            issue_number=artifacts.issue_number,
            dri_summaries=artifacts.dri_summaries,
            notify_previews=artifacts.notify_previews,
            programs_root=PROGRAMS_ROOT,
            confirmed_at=datetime.now(timezone.utc),
        )
        typer.echo(f"Notification log: {notification_log_path}")
        typer.echo(f"Sent {len(records)} notification email(s).")
        raise typer.Exit(code=0)
    except (AuthError, QueryError, StateError) as error:
        typer.echo(str(error))
        raise typer.Exit(code=2)


def build_notify_artifacts(
    *,
    edition_name: str,
    issue_number: int,
    since: str | None = None,
    reports_root: Path = REPORTS_ROOT,
    archive_root: Path = ARCHIVE_ROOT,
) -> FreshnessArtifacts:
    return generate_freshness_report(
        edition_name=edition_name,
        since=since,
        notify=True,
        expected_issue_number=issue_number,
        reports_root=reports_root,
        archive_root=archive_root,
    )


def send_notify_previews(
    previews: tuple[NotifyPreview, ...],
    *,
    sender: NotifySender,
) -> tuple[NotificationRecord, ...]:
    records: list[NotificationRecord] = []
    for preview in previews:
        record = sender(preview)
        if not record.success:
            error_message = record.error or f"Failed to send notification email to {', '.join(record.to)}."
            raise AuthError(error_message)
        records.append(record)
    return tuple(records)


def _send_notify_email(preview: NotifyPreview) -> NotificationRecord:
    graph_client = GraphSendClient()
    graph_client.send_mail(
        GraphMailMessage(
            to=preview.to,
            cc=preview.cc,
            subject=preview.subject,
            html_body=preview.html_body,
        )
    )
    return NotificationRecord(
        sent_at=datetime.now(timezone.utc),
        channel="email",
        to=preview.to,
        subject=preview.subject,
        message_id=None,
        success=True,
        error=None,
    )


def _build_notify_email_sender() -> NotifySender:
    return _send_notify_email


def write_notify_preview_emls(
    *,
    edition_name: str,
    issue_number: int,
    previews: tuple[NotifyPreview, ...],
    programs_root: Path = PROGRAMS_ROOT,
    generated_at: datetime,
) -> tuple[Path, ...]:
    notifications_dir = get_program_output_dir(edition_name, programs_root=programs_root) / "notifications"
    eml_paths: list[Path] = []
    for index, preview in enumerate(previews, start=1):
        recipient_label = _sanitize_notify_filename_component(preview.to[0] if preview.to else f"recipient_{index}")
        eml_path = notifications_dir / f"issue_{issue_number:03d}.{index:02d}.{recipient_label}.eml"
        eml_paths.append(
            write_eml(
                eml_path,
                eml_bytes=build_eml_bytes(
                    to=preview.to,
                    cc=preview.cc,
                    subject=preview.subject,
                    html_body=preview.html_body,
                    text_body=preview.md_body,
                    from_display_name=None,
                    from_email=preview.cc[0] if preview.cc else None,
                    generated_at=generated_at,
                ),
            )
        )
    return tuple(eml_paths)


def build_decision_ask_nudge_preview(
    *,
    ask: DecisionAsk,
    programs_root: Path,
    context_note: str | None = None,
) -> NotifyPreview:
    repo_root = programs_root.parent
    bundle = load_bundle(
        ask.edition_id,
        reports_root=repo_root / "reports",
        programs_root=programs_root,
    )
    knowledge = load_program_knowledge(ask.program_id, programs_root=programs_root)
    recipients = _resolve_decision_ask_nudge_recipients(
        ask=ask,
        knowledge=knowledge,
        program_context=bundle.program_context,
    )
    if not recipients:
        raise StateError(f"Unable to resolve a nudge recipient for decision ask '{ask.id}' in {ask.program_id}.")

    markdown_body = _build_decision_ask_nudge_markdown(ask, context_note=context_note)
    return NotifyPreview(
        to=recipients,
        cc=(),
        subject=f"[{ask.program_id}] Follow-up needed on decision ask {ask.id}",
        html_body=_render_decision_ask_nudge_html(markdown_body),
        md_body=markdown_body,
        attachments=(),
    )


def write_decision_ask_nudge_emls(
    *,
    ask: DecisionAsk,
    previews: tuple[NotifyPreview, ...],
    programs_root: Path = PROGRAMS_ROOT,
    generated_at: datetime,
) -> tuple[Path, ...]:
    notifications_dir = get_program_output_dir(ask.edition_id, programs_root=programs_root) / "decision_ask_nudges"
    eml_paths: list[Path] = []
    for index, preview in enumerate(previews, start=1):
        recipient_label = _sanitize_notify_filename_component(preview.to[0] if preview.to else f"recipient_{index}")
        eml_path = notifications_dir / f"issue_{ask.issue_number:03d}.{ask.id}.{index:02d}.{recipient_label}.nudge.eml"
        eml_paths.append(
            write_eml(
                eml_path,
                eml_bytes=build_eml_bytes(
                    to=preview.to,
                    cc=preview.cc,
                    subject=preview.subject,
                    html_body=preview.html_body,
                    text_body=preview.md_body,
                    from_display_name=None,
                    from_email=preview.cc[0] if preview.cc else None,
                    generated_at=generated_at,
                ),
            )
        )
    return tuple(eml_paths)


def write_notify_preview_adaptive_cards(
    *,
    edition_name: str,
    issue_number: int,
    dri_summaries: tuple,
    items: tuple,
    item_urls: dict[int, str],
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[Path, ...]:
    drafts = _build_notify_adaptive_card_drafts(
        edition_name=edition_name,
        issue_number=issue_number,
        dri_summaries=dri_summaries,
        items=items,
        item_urls=item_urls,
        programs_root=programs_root,
    )
    for draft in drafts:
        draft.path.parent.mkdir(parents=True, exist_ok=True)
        draft.path.write_text(json.dumps(draft.payload, indent=2), encoding="utf-8")
    return tuple(draft.path for draft in drafts)


def send_notify_preview_adaptive_cards(
    *,
    edition_name: str,
    issue_number: int,
    dri_summaries: tuple,
    items: tuple,
    item_urls: dict[int, str],
    programs_root: Path = PROGRAMS_ROOT,
    sender: NotifyCardSender,
) -> tuple[tuple[Path, ...], tuple[NotificationRecord, ...]]:
    drafts = _build_notify_adaptive_card_drafts(
        edition_name=edition_name,
        issue_number=issue_number,
        dri_summaries=dri_summaries,
        items=items,
        item_urls=item_urls,
        programs_root=programs_root,
    )
    records: list[NotificationRecord] = []
    for draft in drafts:
        draft.path.parent.mkdir(parents=True, exist_ok=True)
        draft.path.write_text(json.dumps(draft.payload, indent=2), encoding="utf-8")
        record = sender(draft.recipient_email, draft.subject, draft.payload)
        if not record.success:
            error_message = record.error or f"Failed to send Teams adaptive card notification for {draft.recipient_email}."
            raise AuthError(error_message)
        records.append(record)
    return tuple(draft.path for draft in drafts), tuple(records)


def _build_notify_adaptive_card_drafts(
    *,
    edition_name: str,
    issue_number: int,
    dri_summaries: tuple,
    items: tuple,
    item_urls: dict[int, str],
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[AdaptiveCardDraft, ...]:
    cards_dir = get_program_output_dir(edition_name, programs_root=programs_root) / "adaptive_cards"
    items_by_id = {item.id: item for item in items}
    renderer = AdaptiveCardRenderer()
    drafts: list[AdaptiveCardDraft] = []

    for index, summary in enumerate(dri_summaries, start=1):
        if summary.dri_email == "unassigned":
            continue
        recipient_label = _sanitize_notify_filename_component(summary.dri_email)
        card_path = cards_dir / f"issue_{issue_number:03d}.{index:02d}.{recipient_label}.freshness_alert.json"
        payload = renderer.render_freshness_alert(
            edition_name=edition_name,
            summary=summary,
            items_by_id=items_by_id,
            item_urls=item_urls,
        )
        drafts.append(
            AdaptiveCardDraft(
                recipient_email=summary.dri_email,
                subject=f"{edition_name} freshness alert",
                path=card_path,
                payload=payload,
            )
        )

    return tuple(drafts)


def _build_notify_teams_sender(webhook_url: str) -> NotifyCardSender:
    client = TeamsWebhookClient(webhook_url=webhook_url)

    def _sender(recipient_email: str, subject: str, payload: dict[str, Any]) -> NotificationRecord:
        client.post_card(payload)
        return NotificationRecord(
            sent_at=datetime.now(timezone.utc),
            channel="teams",
            to=(recipient_email,),
            subject=subject,
            message_id=None,
            success=True,
            error=None,
        )

    return _sender


def _sanitize_notify_filename_component(value: str) -> str:
    cleaned = [character.lower() if character.isalnum() else "_" for character in value.strip()]
    collapsed = "".join(cleaned).strip("_")
    return collapsed or "recipient"


def render_notify_preview_plaintext(previews: tuple[NotifyPreview, ...]) -> str:
    lines = ["NOTIFY PREVIEW"]
    if not previews:
        lines.append("No pending notifications.")
        return "\n".join(lines)

    for index, preview in enumerate(previews, start=1):
        lines.append(f"{index}. To: {', '.join(preview.to)}")
        if preview.cc:
            lines.append(f"   CC: {', '.join(preview.cc)}")
        lines.append(f"   Subject: {preview.subject}")
        lines.append("   Body:")
        for body_line in preview.md_body.splitlines():
            lines.append(f"     {body_line}" if body_line else "")
    return "\n".join(lines)


def _build_decision_ask_nudge_markdown(ask: DecisionAsk, *, context_note: str | None = None) -> str:
    lines = [
        "Hi,",
        "",
        f"This is a follow-up on decision ask {ask.id} from issue #{ask.issue_number:03d} for {ask.program_id}.",
        "",
        ask.text,
        "",
        f"Asked on: {ask.ask_date.isoformat()}",
    ]
    if ask.entity_refs:
        lines.append(f"References: {', '.join(ask.entity_refs)}")
    if ask.affected_milestone_ids:
        lines.append(f"Milestones: {', '.join(ask.affected_milestone_ids)}")
    if ask.expiry_date is not None:
        lines.append(f"Expiry: {ask.expiry_date.isoformat()}")
    if context_note is not None and context_note.strip():
        lines.extend(["", f"Context: {context_note.strip()}"])
    lines.extend(
        [
            "",
            "Please confirm the decision or share an updated timeline for closure.",
            "",
            "Thanks,",
        ]
    )
    return "\n".join(lines)


def _render_decision_ask_nudge_html(markdown_body: str) -> str:
    paragraphs = []
    for paragraph in markdown_body.split("\n\n"):
        if not paragraph.strip():
            continue
        lines = [escape(line) for line in paragraph.splitlines()]
        paragraphs.append(f"<p>{'<br/>'.join(lines)}</p>")
    return "<html><body>" + "".join(paragraphs) + "</body></html>"


def _resolve_decision_ask_nudge_recipients(
    *,
    ask: DecisionAsk,
    knowledge: KnowledgeStore,
    program_context: Any,
) -> tuple[str, ...]:
    owner_recipients = _resolve_decision_ask_contact_values(
        (ask.owner_alias,) if ask.owner_alias is not None else (),
        knowledge=knowledge,
    )
    if owner_recipients:
        return owner_recipients

    leadership_recipients = _resolve_decision_ask_contact_values(
        tuple(
            reader.name
            for reader in getattr(program_context, "leadership_readers", ())
            if getattr(reader, "name", None)
        )
        if program_context is not None
        else (),
        knowledge=knowledge,
    )
    return leadership_recipients


def _resolve_decision_ask_contact_values(values: tuple[str, ...], *, knowledge: KnowledgeStore) -> tuple[str, ...]:
    if not values:
        return ()
    contacts: dict[str, str] = {}
    for person in knowledge.people_directory:
        if person.email is None or not person.email.strip():
            continue
        email = person.email.strip().lower()
        contacts[email] = email
        if person.alias:
            contacts[person.alias.strip().lower()] = email
        if person.display_name:
            contacts[person.display_name.strip().lower()] = email

    resolved: list[str] = []
    for value in values:
        normalized = value.strip().lower()
        if not normalized or normalized == "unassigned":
            continue
        resolved_email = contacts.get(normalized)
        if resolved_email is None and "@" in normalized:
            resolved_email = normalized
        if resolved_email is not None:
            resolved.append(resolved_email)
    return tuple(dict.fromkeys(resolved))
