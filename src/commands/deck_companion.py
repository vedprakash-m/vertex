from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from pathlib import Path

import typer

from src.commands.confirm import _deserialize_items, _load_draft_state
from src.commands.report_deck import _build_deck_telemetry_confidence
from src.commands.report_deck import _build_deck_dependency_proposal_rows
from src.commands.report import _ado_item_base_url, _build_deck_ask_rows, _build_deck_assumption_rows, _build_deck_charter_lines, _build_deck_decision_rows, _build_deck_issue_rows, _build_deck_risk_rows, _build_scorecard_data, _build_scorecard_packets, _format_edition_title, _load_eta_forecasts, _load_previous_snapshot, _write_output_text
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.action_tracker import assess_action_staleness
from src.core.claim_tracker import load_open_claims, load_open_decision_asks
from src.core.delta_engine import build_deltas
from src.core.forecast_engine import ETAForecast
from src.core.edition_resolver import get_program_output_dir, resolve_edition
from src.core.evidence_engine import build_evidence
from src.core.freshness_engine import build_freshness_report
from src.core.issue_projection import build_issue_projection
from src.core.jinja_filters import delta_label
from src.core.manifest_writer import get_manifest_path
from src.core.models import DeltaKind, DimensionRisk, ItemDelta, RiskLevel, WorkItem
from src.core.overrides_store import OverridesDocument, Top3NowEntry, load_overrides
from src.core.program_reality import ProgramReality
from src.core.config_loader import REPORTS_ROOT, load_bundle
from src.core.deck_renderer import DeckAssumptionRow, DeckChangeRow, DeckDataRow, DeckDecisionRow, DeckHealthRow, DeckIssueRow, DeckRenderContext, DeckRenderer, DeckTopRiskRow
from src.core.signal_ranking import signal_source_family
from src.core.snapshot_store import ARCHIVE_ROOT
from src.core.store_factory import build_signal_store_for_program_id
from src.core.telemetry_summary import build_program_telemetry_summary
from src.core.trusted_baseline_store import load_trusted_baseline_issue


@dataclass(frozen=True, slots=True)
class DeckCompanionArtifacts:
    issue_number: int
    markdown_path: Path


def deck_companion_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. myprogram_weekly."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number to render. Defaults to the active issue."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    artifacts = generate_deck_companion(
        edition_name=edition,
        issue_number=issue,
    )
    if format == "human":
        typer.echo(f"Deck companion generated for Issue {artifacts.issue_number:03d}.")
        typer.echo(f"Markdown: {artifacts.markdown_path}")
    else:
        typer.echo(render_deck_companion_output(edition, artifacts, format=format), nl=False)
    raise typer.Exit(code=0)


def render_deck_companion_output(edition: str, artifacts: DeckCompanionArtifacts, *, format: str) -> str:
    payload = {
        "edition_name": edition,
        "issue_number": artifacts.issue_number,
        "markdown_path": str(artifacts.markdown_path),
    }
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("edition_name", "issue_number", "markdown_path"))
        writer.writerow((payload["edition_name"], payload["issue_number"], payload["markdown_path"]))
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def generate_deck_companion(
    edition_name: str,
    issue_number: int | None = None,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
    output_root: Path | None = None,
) -> DeckCompanionArtifacts:
    resolved_reports_root = reports_root or REPORTS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT
    resolved_programs_root = resolved_reports_root.parent / "programs"
    resolved_editions_root = resolved_reports_root.parent / "editions"

    bundle = load_bundle(
        edition_name,
        reports_root=resolved_reports_root,
        programs_root=resolved_programs_root,
    )
    resolved_v2 = resolve_edition(
        edition_name,
        programs_root=resolved_programs_root,
    )
    if resolved_v2 is None:
        raise typer.BadParameter(f"Edition '{edition_name}' could not be resolved.")
    resolved_issue_number = _resolve_issue_number(
        edition_name=edition_name,
        issue_number=issue_number,
        archive_root=resolved_archive_root,
    )
    draft_state = _load_draft_state(edition_name, resolved_issue_number, programs_root=resolved_programs_root)
    manifest_path = get_manifest_path(edition_name, resolved_issue_number, programs_root=resolved_programs_root)
    if not manifest_path.exists():
        raise typer.BadParameter(
            f"Draft manifest not found at {manifest_path}. Run `vertex report --dry-run --edition {edition_name} --issue {resolved_issue_number}` first."
        )

    overrides_document = load_overrides(edition_name, reports_root=resolved_reports_root)
    if overrides_document is None or overrides_document.issue_number != resolved_issue_number:
        raise typer.BadParameter(
            f"overrides.yaml is not initialized for Issue {resolved_issue_number:03d}. Run `vertex report --dry-run --edition {edition_name}` first."
        )

    items = _deserialize_items(tuple(draft_state.get("items", [])))
    data_as_of = datetime.fromisoformat(str(draft_state["ado_data_as_of"]))
    generated_at = datetime.fromisoformat(str(draft_state["generated_at"]))
    trusted_baseline_issue_number = load_trusted_baseline_issue(
        edition_name,
        before_issue_number=resolved_issue_number,
        editions_root=resolved_editions_root,
        programs_root=resolved_programs_root,
    )
    previous_snapshot, previous_issue_number = _load_previous_snapshot(
        edition_name=edition_name,
        issue_number=resolved_issue_number,
        archive_root=resolved_archive_root,
        trusted_issue_number=trusted_baseline_issue_number,
    )

    evidence_window_start = data_as_of - __import__("datetime", fromlist=["timedelta"]).timedelta(days=bundle.config.ado.date_window_days)
    evidence_by_item = {
        item.id: build_evidence(item, evidence_window_start, data_as_of)
        for item in items
    }
    deltas = build_deltas(
        current_items=items,
        previous_snapshot=previous_snapshot,
        issue_number=resolved_issue_number,
        previous_issue_number=previous_issue_number,
        evidence_by_item=evidence_by_item,
    )
    scorecard_packets = _build_scorecard_packets(bundle, items, previous_snapshot)
    _, dimension_risks, scorecard_deltas = _build_scorecard_data(
        bundle=bundle,
        items=items,
        evidence_by_item=evidence_by_item,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
        edition_name=edition_name,
        reports_root=resolved_reports_root,
    )
    try:
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest_payload = {}
    if not isinstance(manifest_payload, dict):
        manifest_payload = {}
    latest_confirmed_entry = find_latest_confirmed_entry(
        read_archive_index(edition_name, archive_root=resolved_archive_root),
        before_issue_number=resolved_issue_number,
    )
    open_ask_rows, closed_ask_rows = _build_deck_ask_rows(
        program_id=resolved_v2.program.id,
        issue_number=resolved_issue_number,
        as_of=generated_at,
        last_confirmed_at=(latest_confirmed_entry.generated_at if latest_confirmed_entry is not None else None),
        programs_root=resolved_programs_root,
    )
    freshness_report = build_freshness_report(
        current_items=items,
        issue_number=resolved_issue_number,
        as_of=data_as_of,
        stale_warn_days=bundle.editorial_rules.stale_warn_days,
        stale_block_days=bundle.editorial_rules.stale_block_days,
        previous_snapshot=previous_snapshot,
        previous_notification_state=None,
        program_context=bundle.program_context,
        workstream_narrative_history={},
    )
    eta_forecasts = _load_eta_forecasts(
        edition_name=edition_name,
        items=items,
        as_of=data_as_of,
        reports_root=resolved_reports_root,
    )
    signal_store = build_signal_store_for_program_id(
        resolved_v2.program.id,
        programs_root=resolved_programs_root,
    )
    _reality = ProgramReality.load(
        resolved_v2.program.id,
        programs_root=resolved_programs_root,
    )
    issue_projections = build_issue_projection(
        items=items,
        freshness_report=freshness_report,
        icm_signals=tuple(
            signal
            for signal in signal_store.read(
                resolved_v2.program.id,
                end=data_as_of,
            )
            if signal_source_family(signal.source) == "icm"
        ),
        open_asks=load_open_decision_asks(resolved_v2.program.id, programs_root=resolved_programs_root),
        overdue_actions=assess_action_staleness(
            tuple(a.record for a in _reality.actions()),
            data_as_of.date(),
        ),
        open_claims=load_open_claims(resolved_v2.program.id, programs_root=resolved_programs_root),
        risk_entries=tuple(a.record for a in _reality.risks()),
        ado_item_base_url=_ado_item_base_url(bundle),
    )

    deck_context = _build_deck_context(
        issue_number=resolved_issue_number,
        data_as_of=data_as_of,
        generated_at=generated_at,
        dimension_risks=dimension_risks,
        top_risks=overrides_document.top_3_now,
        deltas=deltas,
        scorecard_deltas=scorecard_deltas,
        items=items,
        manifest_id=str(manifest_payload.get("manifest_id", "")),
        source_label=f"ADO {bundle.config.ado.organization}/{bundle.config.ado.project}",
        area_path_count=len(bundle.config.ado.area_paths),
        title=_format_edition_title(bundle, resolved_issue_number, data_as_of),
        eta_forecasts=eta_forecasts,
        raw_program=resolved_v2.raw_program if resolved_v2 is not None else {},
        program_id=resolved_v2.program.id,
        programs_root=resolved_programs_root,
        reality=_reality,
        open_issue_rows=_build_deck_issue_rows(issue_projections, eta_forecasts=eta_forecasts),
        key_decision_rows=_build_deck_decision_rows(
            program_id=resolved_v2.program.id,
            as_of=data_as_of,
            programs_root=resolved_programs_root,
            reality=_reality,
        ),
        key_assumption_rows=_build_deck_assumption_rows(
            program_id=resolved_v2.program.id,
            as_of=data_as_of,
            programs_root=resolved_programs_root,
            reality=_reality,
        ),
        open_ask_rows=open_ask_rows,
        closed_ask_rows=closed_ask_rows,
    )
    markdown_body = DeckRenderer(edition_name, reports_root=resolved_reports_root).render(deck_context)
    markdown_path = _write_output_text(
        get_program_output_dir(edition_name, programs_root=resolved_programs_root) / f"issue_{resolved_issue_number:03d}" / f"issue_{resolved_issue_number:03d}.deck.md",
        markdown_body,
    )
    return DeckCompanionArtifacts(issue_number=resolved_issue_number, markdown_path=markdown_path)


def _resolve_issue_number(
    *,
    edition_name: str,
    issue_number: int | None,
    archive_root: Path,
) -> int:
    if issue_number is not None:
        return issue_number
    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    if not archive_index.issues:
        return 1
    return max(entry.issue_number for entry in archive_index.issues) + 1


def _build_deck_context(
    *,
    issue_number: int,
    data_as_of: datetime,
    generated_at: datetime,
    dimension_risks: tuple[DimensionRisk, ...],
    top_risks: tuple[Top3NowEntry, ...],
    deltas,
    scorecard_deltas,
    items: tuple[WorkItem, ...],
    manifest_id: str,
    source_label: str,
    area_path_count: int,
    title: str,
    eta_forecasts: dict[int, ETAForecast],
    raw_program: dict[str, object],
    program_id: str,
    programs_root: Path,
    reality: ProgramReality | None = None,
    open_issue_rows: tuple[DeckIssueRow, ...],
    key_decision_rows: tuple[DeckDecisionRow, ...],
    key_assumption_rows: tuple[DeckAssumptionRow, ...],
    open_ask_rows,
    closed_ask_rows,
) -> DeckRenderContext:
    item_lookup = {item.id: item for item in items}
    delta_lookup = _build_item_delta_lookup(deltas)
    return DeckRenderContext(
        issue_number=issue_number,
        issue_date_label=_format_issue_date(data_as_of),
        health_rows=_build_health_rows(dimension_risks),
        top_risk_rows=_build_top_risk_rows(top_risks, item_lookup, delta_lookup),
        change_rows=_build_change_rows(deltas, scorecard_deltas),
        data_rows=(
            DeckDataRow(label="Title", value=title),
            DeckDataRow(label="Source", value=f"{source_label}, {area_path_count} area paths, {len(items)} items"),
            DeckDataRow(label="Generated", value=_format_generated_at(generated_at)),
            DeckDataRow(label="Manifest", value=(manifest_id[:8] or "unknown")),
        ),
        telemetry_summary=build_program_telemetry_summary(
            program_id,
            programs_root=programs_root,
            as_of=data_as_of,
        ),
        telemetry_confidence=_build_deck_telemetry_confidence(
            program_id,
            programs_root=programs_root,
            as_of=data_as_of,
        ),
        charter_lines=_build_deck_charter_lines(raw_program),
        open_risk_rows=_build_deck_risk_rows(
            program_id=program_id,
            as_of=data_as_of,
            programs_root=programs_root,
            reality=reality,
        ),
        dependency_proposal_rows=_build_deck_dependency_proposal_rows(
            program_id=program_id,
            programs_root=programs_root,
        ),
        key_decision_rows=key_decision_rows,
        key_assumption_rows=key_assumption_rows,
        open_issue_rows=open_issue_rows,
        open_ask_rows=open_ask_rows,
        closed_ask_rows=closed_ask_rows,
    )


def _build_health_rows(dimension_risks: tuple[DimensionRisk, ...]) -> tuple[DeckHealthRow, ...]:
    return tuple(
        DeckHealthRow(
            dimension_name=dimension.name,
            risk=dimension.risk,
            summary=_truncate_words(dimension.summary, 15),
        )
        for dimension in dimension_risks
    )


def _build_top_risk_rows(
    top_risks: tuple[Top3NowEntry, ...],
    item_lookup: dict[int, WorkItem],
    delta_lookup: dict[int, ItemDelta],
) -> tuple[DeckTopRiskRow, ...]:
    rows: list[DeckTopRiskRow] = []
    for entry in top_risks:
        work_item_id = _extract_work_item_id(entry.ado_link)
        item = item_lookup.get(work_item_id) if work_item_id is not None else None
        item_delta = delta_lookup.get(work_item_id) if work_item_id is not None else None
        rows.append(
            DeckTopRiskRow(
                text=entry.text.strip(),
                risk=(item.risk_level if item is not None else _risk_from_top_item_type(entry.type)),
                delta_text=(_format_item_delta(item_delta) if item_delta is not None else None),
                work_item_id=work_item_id,
            )
        )
    return tuple(rows)


def _build_change_rows(
    deltas,
    scorecard_deltas,
) -> tuple[DeckChangeRow, ...]:
    if deltas.previous_issue_number is None:
        return (DeckChangeRow(text="No prior confirmed snapshot is available; this issue establishes the baseline."),)

    rows: list[DeckChangeRow] = []
    for scorecard_delta in scorecard_deltas:
        rows.append(
            DeckChangeRow(
                text=f"{scorecard_delta.dimension}: {_risk_change_text(scorecard_delta.old_risk, scorecard_delta.new_risk)}"
            )
        )

    counts_summary = _format_delta_counts_summary(deltas)
    if counts_summary is not None:
        rows.append(DeckChangeRow(text=counts_summary))

    if not rows:
        rows.append(DeckChangeRow(text="No material changes were detected against the prior confirmed snapshot."))
    return tuple(rows)


def _build_item_delta_lookup(deltas) -> dict[int, ItemDelta]:
    ordered_deltas = [
        *sorted((delta for delta in deltas.risk_changes if delta.kind == DeltaKind.RISK_UP), key=lambda delta: delta.work_item_id),
        *sorted(deltas.new_items, key=lambda delta: delta.work_item_id),
        *sorted(deltas.eta_changes, key=lambda delta: delta.work_item_id),
        *sorted(tuple(getattr(deltas, "owner_changes", ())), key=lambda delta: delta.work_item_id),
        *sorted((delta for delta in deltas.risk_changes if delta.kind == DeltaKind.RISK_DOWN), key=lambda delta: delta.work_item_id),
        *sorted(deltas.closed_items, key=lambda delta: delta.work_item_id),
    ]
    lookup: dict[int, ItemDelta] = {}
    for delta in ordered_deltas:
        lookup.setdefault(delta.work_item_id, delta)
    return lookup


def _format_delta_counts_summary(deltas) -> str | None:
    counts: list[str] = []
    if deltas.new_items:
        counts.append(_count_label(len(deltas.new_items), "new item"))
    if deltas.closed_items:
        counts.append(_count_label(len(deltas.closed_items), "closed item"))
    if deltas.eta_changes:
        counts.append(_count_label(len(deltas.eta_changes), "ETA shift"))
    owner_changes = tuple(getattr(deltas, "owner_changes", ()))
    if owner_changes:
        counts.append(_count_label(len(owner_changes), "owner change"))
    if not counts:
        return None
    return ", ".join(counts)


def _format_item_delta(delta: ItemDelta) -> str:
    if delta.kind in {DeltaKind.RISK_UP, DeltaKind.RISK_DOWN}:
        return delta_label(delta.kind, delta.old_risk, delta.new_risk)
    if delta.kind == DeltaKind.ETA_CHANGED:
        return delta_label(delta.kind, delta.old_eta, delta.new_eta)
    return delta_label(delta.kind)


def _risk_change_text(old_risk: RiskLevel, new_risk: RiskLevel) -> str:
    return f"{old_risk.value.title()} → {new_risk.value.title()}"


def _format_issue_date(value: datetime) -> str:
    return f"{value.strftime('%B')} {value.day}, {value.year}"


def _format_generated_at(value: datetime) -> str:
    return value.strftime("%b %d %Y, %H:%M UTC")


def _truncate_words(text: str, limit: int) -> str:
    words = text.split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]) + "..."


def _count_label(count: int, singular: str) -> str:
    if count == 1:
        return f"1 {singular}"
    return f"{count} {singular}s"


def _risk_from_top_item_type(item_type: str) -> RiskLevel:
    normalized = item_type.strip().lower()
    if normalized in {"decision", "ask"}:
        return RiskLevel.HIGH
    if normalized in {"risk", "watch"}:
        return RiskLevel.MEDIUM
    if normalized in {"improved", "win"}:
        return RiskLevel.LOW
    return RiskLevel.UNKNOWN


def _extract_work_item_id(ado_link: str) -> int | None:
    if not ado_link.strip():
        return None
    match = re.search(r"/(\d+)(?:[/?#]|$)", ado_link)
    if match is None:
        return None
    return int(match.group(1))
