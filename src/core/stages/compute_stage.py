from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from pathlib import Path
from typing import Any

from src.core.evidence_engine import build_evidence
from src.core.journal import classify_signal, get_weekly_journal_path, _signal_to_record
from src.core.jsonl_utils import append_jsonl_lines
from src.core.models import EditionType, Snapshot
from src.core.overrides_store import load_overrides, merge_overrides, save_overrides
from src.core.pipeline import StageContext
from src.core.plane1_changelog import load_plane1_changes, plane1_change_to_signal


class ComputeStage:
    def name(self) -> str:
        return "compute"

    def execute(self, ctx: StageContext) -> StageContext:
        if (
            ctx.bundle is None
            or ctx.reports_root is None
            or ctx.archive_root is None
            or ctx.resolved_issue_number is None
            or ctx.resolved_edition_type is None
            or ctx.data_as_of is None
            or ctx.stage_support is None
        ):
            raise RuntimeError("ResolutionStage must execute before ComputeStage.")
        if ctx.resolved_edition_type == EditionType.LOOKBACK or ctx.default_exec_summary is not None:
            return ctx

        support = ctx.stage_support
        eta_forecasts = support.load_eta_forecasts(
            edition_name=ctx.edition_name,
            items=ctx.items,
            as_of=ctx.data_as_of,
            reports_root=ctx.reports_root,
        )
        evidence_window_start = ctx.data_as_of - timedelta(days=ctx.bundle.config.ado.date_window_days)
        evidence_by_item = {
            item.id: build_evidence(item, evidence_window_start, ctx.data_as_of)
            for item in ctx.items
        }
        continuity_snapshot = ctx.previous_snapshot if _has_usable_continuity_baseline(ctx.previous_snapshot) else None
        continuity_previous_issue_number = ctx.previous_issue_number if continuity_snapshot is not None else None
        deltas = support.build_continuity_deltas(
            current_items=ctx.items,
            previous_snapshot=continuity_snapshot,
            issue_number=ctx.resolved_issue_number,
            previous_issue_number=continuity_previous_issue_number,
            evidence_by_item=evidence_by_item,
        )
        expected_scorecards = {
            scorecard.name: tuple(dimension.name for dimension in scorecard.dimensions)
            for scorecard in ctx.bundle.config.scorecards
        }
        overrides_document, _ = merge_overrides(
            issue_number=ctx.resolved_issue_number,
            expected_scorecards=expected_scorecards,
            existing=load_overrides(ctx.edition_name, reports_root=ctx.reports_root, issue_number=ctx.resolved_issue_number),
        )
        override_snapshot = support.build_override_snapshot(overrides_document)
        top_3_now = tuple(entry.text.strip() for entry in overrides_document.top_3_now if entry.text.strip())
        overrides_path = save_overrides(ctx.edition_name, overrides_document, reports_root=ctx.reports_root)
        scorecard_packets = support.build_scorecard_packets(
            ctx.bundle,
            ctx.items,
            continuity_snapshot,
            edition_name=ctx.edition_name,
            archive_root=ctx.archive_root,
            trusted_issue_number=ctx.trusted_baseline_issue_number,
            overrides_document=overrides_document,
        )
        scorecards, dimension_risks, scorecard_deltas = support.build_scorecard_data(
            bundle=ctx.bundle,
            items=ctx.items,
            evidence_by_item=evidence_by_item,
            scorecard_packets=scorecard_packets,
            overrides_document=overrides_document,
            edition_name=ctx.edition_name,
            archive_root=ctx.archive_root,
            trusted_issue_number=ctx.trusted_baseline_issue_number,
            reports_root=ctx.reports_root,
        )
        signal_context = support.load_report_signal_context(
            edition_name=ctx.edition_name,
            bundle=ctx.bundle,
            items=ctx.items,
            as_of=ctx.data_as_of,
            previous_snapshot=ctx.previous_snapshot,
            reports_root=ctx.reports_root,
        )
        default_exec_summary = support.build_exec_summary_text(
            ctx.bundle,
            ctx.items,
            dimension_risks,
            deltas,
            dependency_cascades=(signal_context.dependency_cascades if signal_context is not None else ()),
            baseline_available=continuity_snapshot is not None,
        )

        updated_ctx = replace(
            ctx,
            eta_forecasts=eta_forecasts,
            evidence_by_item=evidence_by_item,
            continuity_snapshot=continuity_snapshot,
            continuity_previous_issue_number=continuity_previous_issue_number,
            deltas=deltas,
            overrides_document=overrides_document,
            override_snapshot=override_snapshot,
            top_3_now=top_3_now,
            overrides_path=overrides_path,
            scorecard_packets=scorecard_packets,
            scorecards=scorecards,
            dimension_risks=dimension_risks,
            scorecard_deltas=scorecard_deltas,
            signal_context=signal_context,
            default_exec_summary=default_exec_summary,
        )

        # §22 E3: Inject Plane 1 config changes as auto-approved signals into the signal store.
        # Must happen after the context is updated (so latest_confirmed_entry is available) but
        # before NarrativeStage reads the signals.
        return _inject_plane1_change_signals(updated_ctx)


def _has_usable_continuity_baseline(previous_snapshot: Snapshot | None) -> bool:
    return previous_snapshot is not None and bool(previous_snapshot.items)


def _inject_plane1_change_signals(ctx: StageContext) -> StageContext:
    """
    §22 E3: Load Plane 1 config changes since the last confirmed issue and inject
    them as auto-approved signals into the signal store.

    This enables the newsletter proposal to surface authored config changes (e.g. a
    milestone slipping to at_risk) even when there is no coincident ADO signal noise.
    """
    # Guard: need program context and a confirmed baseline to compute the time window
    if ctx.resolved_v2 is None or ctx.programs_root is None:
        return ctx
    if ctx.latest_confirmed_entry is None:
        # No prior confirmed issue — nothing to diff against yet
        return ctx
    program_id = ctx.resolved_v2.program.id
    since_dt = ctx.latest_confirmed_entry.generated_at

    # Load changelog entries since the last confirmed issue
    changes = load_plane1_changes(program_id, programs_root=ctx.programs_root, since=since_dt)
    if not changes:
        return ctx

    # Build and batch-append synthetic signals.
    # Previously, each signal was appended individually (one fsync per signal).
    # With 6000+ changelog entries this caused a ~19-minute stall.
    # Group serialized records by target weekly file and write each group in one
    # append_jsonl_lines call (one fsync per file instead of one per signal).
    program = ctx.resolved_v2.program
    now = datetime.now(timezone.utc)
    lines_by_path: dict[Path, list[str]] = {}
    for change in changes:
        try:
            signal = plane1_change_to_signal(change, program_id=program_id)
            target = get_weekly_journal_path(program_id, now, ctx.programs_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            record = _signal_to_record(classify_signal(signal))
            lines_by_path.setdefault(target, []).append(
                json.dumps(record, ensure_ascii=False) + "\n"
            )
        except Exception:
            pass
    for path, lines in lines_by_path.items():
        try:
            append_jsonl_lines(path, lines)
        except Exception:
            pass

    return ctx