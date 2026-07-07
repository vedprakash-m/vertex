"""Weekly summary Adaptive Card posting for confirm.

Extracted from ``src/commands/confirm.py`` (D-25 / Phase 3). This cluster owns
the optional ``--post-weekly-summary-card`` feature: request validation,
rendering the Adaptive Card payload, writing the card JSON sidecar, and posting
it to the configured Teams incoming webhook. It is a self-contained notification
concern, isolated from the confirm transaction (the archive write). The post
helper swallows rendering/posting failures into operator-facing warnings so a
webhook outage never blocks a confirm. ``confirm.py`` imports the two entry
points it calls (``validate_weekly_summary_card_request``,
``post_confirm_weekly_summary_card``) under their historical private aliases.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from src.commands.report import _artifact_url, _build_item_urls, _write_output_json
from src.core.archive_store import ConfirmedIssueArchivePaths
from src.core.edition_resolver import get_program_output_dir, PROGRAMS_ROOT
from src.core.models import EditionType, ReportData
from src.m365.adaptive_card_renderer import AdaptiveCardRenderer
from src.m365.teams_webhook_client import TeamsWebhookClient


def validate_weekly_summary_card_request(*, bundle, draft_state: dict[str, Any]) -> None:
    edition_type = EditionType.from_string(str(draft_state.get("edition_type", bundle.config.edition.type)))
    if bundle.config.edition.cadence.lower() != "weekly" or edition_type in {EditionType.DECK, EditionType.LOOKBACK}:
        raise typer.BadParameter(
            "--post-weekly-summary-card is only supported for weekly non-deck, non-lookback report editions."
        )
    if not bundle.config.m365.teams_incoming_webhook_url:
        raise typer.BadParameter(
            "m365.teams_incoming_webhook_url must be configured in program.yaml before --post-weekly-summary-card can be used."
        )


def post_confirm_weekly_summary_card(
    *,
    bundle,
    edition_name: str,
    issue_number: int,
    report: ReportData,
    programs_root: Path = PROGRAMS_ROOT,
    archive_paths: ConfirmedIssueArchivePaths,
    webhook_url: str,
) -> tuple[Path | None, bool, str | None]:
    try:
        card_path, payload = write_confirm_weekly_summary_card(
            bundle=bundle,
            edition_name=edition_name,
            issue_number=issue_number,
            report=report,
            programs_root=programs_root,
            archive_paths=archive_paths,
        )
    except Exception as exc:
        return None, False, f"Weekly summary card skipped: {exc}"

    try:
        build_confirm_weekly_summary_teams_sender(webhook_url)(payload)
    except Exception as exc:
        return card_path, False, f"Weekly summary card not posted: {exc}"
    return card_path, True, None


def write_confirm_weekly_summary_card(
    *,
    bundle,
    edition_name: str,
    issue_number: int,
    report: ReportData,
    programs_root: Path = PROGRAMS_ROOT,
    archive_paths: ConfirmedIssueArchivePaths,
) -> tuple[Path, dict[str, Any]]:
    program_output_dir = get_program_output_dir(edition_name, programs_root=programs_root)
    output_html_path = program_output_dir / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.html"
    report_html_url = (
        _artifact_url(bundle, output_root=program_output_dir, artifact_path=output_html_path)
        if output_html_path.exists()
        else archive_paths.html_path.resolve().as_uri()
    )
    payload = AdaptiveCardRenderer().render_weekly_summary(
        edition_name=edition_name,
        issue_number=issue_number,
        report=report,
        item_urls=_build_item_urls(bundle, report.items),
        report_html_url=report_html_url,
    )
    card_path = program_output_dir / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.weekly_summary.json"
    return _write_output_json(card_path, payload), payload


def build_confirm_weekly_summary_teams_sender(webhook_url: str):
    client = TeamsWebhookClient(webhook_url=webhook_url)

    def _sender(payload: dict[str, Any]) -> None:
        client.post_card(payload)

    return _sender
