"""Post-confirm support helpers for confirm.

Extracted from ``src/commands/confirm.py`` (D-25 / Phase 3). These helpers are
support code around the main confirm transaction: issue numbering, output-path
resolution, EML assembly, next-issue narrative seeding, and best-effort context
snapshot writes.

WS-13 PB-29 (Tier-1 silent-swallow audit): the previous revision of
``write_context_snapshot_for_issue`` wrapped 5 critical reads + 1 write in
bare ``except Exception: pass``. A failing Plane-1 read OR a failed context
snapshot write now logs the exception via ``log.error`` and re-raises, so a
degraded confirm surfaces in gather_state.json + doctor instead of silently
producing an incomplete snapshot.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import typer

from src.commands.report_email import _distribution_to, _resolve_email_subject
from src.core.archive_store import ArchiveEntry
from src.core.context_snapshot_store import load_context_snapshot, write_context_snapshot
from src.core.eml_writer import build_eml_bytes
from src.core.models import ReportData
from src.core.plane1_changelog import load_plane1_changes
from src.core.program_context import load_program_context as _load_program_context
from src.core.edition_resolver import get_program_output_dir, PROGRAMS_ROOT
from src.core.program_fact_store import (
    load_current_decision_entries,
    load_current_milestones,
    load_current_risk_entries,
    load_current_workstreams,
)


log = logging.getLogger(__name__)


def write_context_snapshot_for_issue(
    *,
    program_id: str,
    edition_id: str,
    issue_number: int,
    confirmed_at: datetime,
    archive_root: Path,
    programs_root: Path,
    prior_issue_entry: ArchiveEntry | None,
) -> None:
    """
    §22 E2: Write a context snapshot capturing the Plane 1 program model at confirm time.
    §13.2: Recompute maturity level and emit WARN if it regressed vs prior snapshot.
    """
    try:
        milestones = list(load_current_milestones(program_id, programs_root=programs_root))
        risks = list(load_current_risk_entries(program_id, programs_root=programs_root))
        decisions = list(load_current_decision_entries(program_id, programs_root=programs_root))
        workstreams_list = list(load_current_workstreams(program_id, programs_root=programs_root))
    except Exception as exc:
        # WS-13 PB-29 (Tier-1): the Plane-1 reads are the substrate for the
        # context snapshot. A failure here must NOT silently produce an
        # empty snapshot; log + re-raise so the caller (confirm stage) can
        # record a degraded-cycle entry in gather_state.json.
        log.error(
            "WS-13 PB-29: Plane-1 read for context snapshot failed "
            "(program=%s, issue=%s): %s",
            program_id, issue_number, exc,
        )
        raise

    plane1_change_count = 0
    if prior_issue_entry is not None:
        prior_confirmed_at = prior_issue_entry.generated_at
        try:
            prior_changes = load_plane1_changes(
                program_id,
                programs_root=programs_root,
                since=prior_confirmed_at,
            )
            plane1_change_count = len(prior_changes)
        except Exception as exc:
            log.error(
                "WS-13 PB-29: load_plane1_changes(since=%s) failed: %s",
                prior_confirmed_at, exc,
            )
            raise

    context_maturity_level = 0
    try:
        context_maturity_level = _load_program_context(
            program_id, programs_root=programs_root, raise_on_error=False
        ).maturity_level.value
    except Exception as exc:
        log.error(
            "WS-13 PB-29: load_program_context for issue %s failed: %s",
            issue_number, exc,
        )
        raise
    if prior_issue_entry is not None:
        try:
            prior_snapshot = load_context_snapshot(
                program_id, edition_id, prior_issue_entry.issue_number, archive_root=programs_root
            )
            if prior_snapshot is not None and prior_snapshot.context_maturity_level > context_maturity_level:
                typer.echo(
                    f"⚠  Context maturity regression: L{prior_snapshot.context_maturity_level}"
                    f" → L{context_maturity_level} (issue {prior_issue_entry.issue_number}"
                    f" → {issue_number}). Run `vertex doctor --context` for details.",
                    err=True,
                )
        except Exception as exc:
            log.error(
                "WS-13 PB-29: load_context_snapshot(prior=%s) failed: %s",
                prior_issue_entry.issue_number, exc,
            )
            raise

    try:
        write_context_snapshot(
            program_id=program_id,
            edition_id=edition_id,
            issue_number=issue_number,
            milestones=milestones,
            risks=risks,
            workstreams=workstreams_list,
            decisions=decisions,
            confirmed_at=confirmed_at,
            plane1_change_count_since_prior=plane1_change_count,
            archive_root=programs_root,
            context_maturity_level=context_maturity_level,
        )
    except Exception as exc:
        # WS-13 PB-29 (Tier-1): the context snapshot is a governance write;
        # silently skipping it would leave the archive inconsistent. Log
        # + re-raise so the confirm stage records a degraded entry.
        log.error(
            "WS-13 PB-29: write_context_snapshot(issue=%s) failed: %s",
            issue_number, exc,
        )
        raise


def build_confirmed_eml_bytes(
    bundle,
    *,
    issue_number: int,
    as_of: datetime,
    html_body: str,
    markdown_body: str,
    suggested_subject: str,
    generated_at: datetime,
    format_edition_title_fn,
) -> bytes:
    return build_eml_bytes(
        to=_distribution_to(bundle),
        cc=bundle.config.distribution.cc,
        subject=_resolve_email_subject(
            suggested_subject=suggested_subject,
            default_subject=format_edition_title_fn(bundle, issue_number, as_of),
        ),
        html_body=html_body,
        text_body=markdown_body,
        from_display_name=bundle.config.author.display_name,
        from_email=bundle.config.author.email,
        generated_at=generated_at,
        mark_as_draft=True,
    )


def load_draft_continuation_contract_path(
    edition_name: str,
    issue_number: int,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path | None:
    path = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.continuation_contract.json"
    return path if path.exists() else None


def next_issue_number(index: Any) -> int:
    if not index.issues:
        return 1
    return max(entry.issue_number for entry in index.issues) + 1


def next_issue_narrative_templates(
    report: ReportData,
    bundle,
    *,
    is_continuity_layout_fn,
) -> dict[str, str]:
    templates = {"exec_summary.md": report.exec_summary_text}
    prefix = "chapter_" if is_continuity_layout_fn(bundle) else "ws_"
    templates.update({f"{prefix}{section_id}.md": blurb for section_id, blurb in report.workstream_blurbs.items()})
    return templates
