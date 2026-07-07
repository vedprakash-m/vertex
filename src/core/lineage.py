from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.archive_store import get_dimension_history
from src.core.forecast_engine import ForecastAssessment
from src.core.jinja_filters import build_anchor, delta_label, risk_label
from src.core.models import Confidence, DeltaKind, DeltaSet, EditionType, EvidencePacket, ItemDelta, RiskLevel, Snapshot, WorkItem
from src.core.overrides_store import OverridesDocument
from src.core.view_models import WorkstreamData


@dataclass(frozen=True, slots=True)
class LineageEntry:
    label: str
    value: str
    href: str | None = None


@dataclass(frozen=True, slots=True)
class LineageClaim:
    claim_id: str
    section_id: str
    title: str
    statement: str
    confidence: Confidence
    narrative_path: str
    narrative_line: int
    source_item_ids: tuple[int, ...]
    lines: tuple[LineageEntry, ...]
    published_issue_number: int
    edition_type: EditionType


@dataclass(frozen=True, slots=True)
class LineageLookup:
    claims: tuple[LineageClaim, ...]

    def claims_for_section(self, section_id: str) -> tuple[LineageClaim, ...]:
        normalized = section_id.strip().lower()
        return tuple(claim for claim in self.claims if claim.section_id.lower() == normalized)

    def find_claim(self, claim_key: str) -> LineageClaim | None:
        normalized = claim_key.strip().lower()
        for claim in self.claims:
            claim_id = claim.claim_id.lower()
            if claim_id == normalized or claim_id.endswith(normalized):
                return claim
        return None

    def claims_for_work_item(self, work_item_id: int) -> tuple[LineageClaim, ...]:
        return tuple(claim for claim in self.claims if work_item_id in claim.source_item_ids)


def build_lineage_lookup(
    *,
    edition_name: str,
    issue_number: int,
    edition_type: EditionType,
    workstreams: tuple[WorkstreamData, ...],
    items: tuple[WorkItem, ...],
    deltas: DeltaSet,
    evidence_by_item: dict[int, EvidencePacket],
    overrides_document: OverridesDocument,
    previous_snapshot: Snapshot | None,
    archive_root: Path,
    forecast: ForecastAssessment | None = None,
) -> LineageLookup:
    delta_lookup = _group_deltas_by_item(deltas)
    claims: list[LineageClaim] = [
        _build_exec_summary_claim(
            issue_number=issue_number,
            edition_type=edition_type,
            items=items,
            evidence_by_item=evidence_by_item,
            delta_lookup=delta_lookup,
            previous_snapshot=previous_snapshot,
        )
    ]
    for workstream in workstreams:
        claims.append(
            _build_workstream_claim(
                edition_name=edition_name,
                issue_number=issue_number,
                edition_type=edition_type,
                workstream=workstream,
                evidence_by_item=evidence_by_item,
                delta_lookup=delta_lookup,
                overrides_document=overrides_document,
                previous_snapshot=previous_snapshot,
                archive_root=archive_root,
            )
        )
    if forecast is not None:
        claims.append(_build_forecast_claim(issue_number=issue_number, edition_type=edition_type, forecast=forecast))
    return LineageLookup(claims=tuple(claims))


def build_ado_lineage_text(
    *,
    work_item: WorkItem,
    evidence: EvidencePacket | None,
    item_url: str | None,
    related_claims: tuple[LineageClaim, ...],
    deltas: DeltaSet,
) -> str:
    delta_entries = _group_deltas_by_item(deltas).get(work_item.id, ())
    lines = [
        f"ADO Item: #{work_item.id} {work_item.title}",
        f"State: {work_item.state} | Risk: {risk_label(work_item.risk_level)}",
        f"Source Fields: {_format_source_fields(evidence, delta_entries)}",
        f"Diff vs prior issue: {_format_diff_summary(delta_entries)}",
        f"Last edited: {_format_timestamp(_latest_evidence_timestamp(evidence))}",
        f"Confidence: {(evidence.confidence.value if evidence is not None else Confidence.NONE.value).upper()}",
        f"Claims: {', '.join(claim.claim_id for claim in related_claims) or 'none'}",
    ]
    if item_url:
        lines.append(f"ADO URL: {item_url}")
    if evidence is not None:
        lines.extend(["", "Evidence Summary:", evidence.summary_for_reviewer])
    return "\n".join(lines).rstrip() + "\n"


def format_claim_text(claim: LineageClaim) -> str:
    lines = [
        f"Claim: {claim.statement}",
        f"Narrative: {claim.narrative_path}:{claim.narrative_line}",
        f"Published in: Issue {claim.published_issue_number:03d}, {claim.edition_type.value} edition",
        f"Confidence: {claim.confidence.value.upper()}",
    ]
    for entry in claim.lines:
        line = f"{entry.label}: {entry.value}"
        if entry.href:
            line += f" ({entry.href})"
        lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


def _build_exec_summary_claim(
    *,
    issue_number: int,
    edition_type: EditionType,
    items: tuple[WorkItem, ...],
    evidence_by_item: dict[int, EvidencePacket],
    delta_lookup: dict[int, tuple[ItemDelta, ...]],
    previous_snapshot: Snapshot | None,
) -> LineageClaim:
    item_ids = tuple(item.id for item in items[:5])
    lines = (
        LineageEntry(label="Source system", value="ADO"),
        LineageEntry(label="Source field", value=_format_source_fields_for_ids(item_ids, evidence_by_item, delta_lookup)),
        LineageEntry(label="Source items", value=_format_source_items(item_ids)),
        LineageEntry(label="Last edited", value=_format_latest_timestamp_for_ids(item_ids, evidence_by_item)),
        LineageEntry(label="Diff vs prior issue", value=_format_diff_for_ids(item_ids, delta_lookup, previous_snapshot)),
        LineageEntry(label="Rule IDs", value=_format_rule_ids_for_ids(item_ids, delta_lookup)),
        LineageEntry(label="Override ID", value="none"),
    )
    return LineageClaim(
        claim_id="exec_summary.summary",
        section_id="exec_summary",
        title="Executive Summary",
        statement="Executive summary narrative backed by current item deltas and evidence packets",
        confidence=_max_confidence(tuple(evidence_by_item.get(item_id) for item_id in item_ids)),
        narrative_path=f"narratives/issue_{issue_number:03d}/exec_summary.md",
        narrative_line=1,
        source_item_ids=item_ids,
        lines=lines,
        published_issue_number=issue_number,
        edition_type=edition_type,
    )


def _build_workstream_claim(
    *,
    edition_name: str,
    issue_number: int,
    edition_type: EditionType,
    workstream: WorkstreamData,
    evidence_by_item: dict[int, EvidencePacket],
    delta_lookup: dict[int, tuple[ItemDelta, ...]],
    overrides_document: OverridesDocument,
    previous_snapshot: Snapshot | None,
    archive_root: Path,
) -> LineageClaim:
    item_ids = tuple(item.id for item in workstream.items)
    lines = [
        LineageEntry(label="Source system", value="ADO"),
        LineageEntry(label="Source field", value=_format_source_fields_for_ids(item_ids, evidence_by_item, delta_lookup)),
        LineageEntry(label="Source items", value=_format_source_items(item_ids)),
        LineageEntry(label="Last edited", value=_format_latest_timestamp_for_ids(item_ids, evidence_by_item)),
        LineageEntry(label="Diff vs prior issue", value=_format_diff_for_ids(item_ids, delta_lookup, previous_snapshot)),
        LineageEntry(label="Rule IDs", value=_format_rule_ids_for_ids(item_ids, delta_lookup)),
        LineageEntry(label="Override ID", value=_override_ids_for_section(overrides_document, issue_number, workstream.section_id)),
        LineageEntry(label="Narrative", value=workstream.edit_path or f"narratives/issue_{issue_number:03d}/ws_{workstream.section_id}.md"),
    ]
    history_note = _dimension_history_note(edition_name, workstream.title, archive_root)
    if history_note is not None:
        lines.append(LineageEntry(label="Lineage", value=history_note))
    if workstream.ado_query_url:
        lines.append(LineageEntry(label="ADO query", value="View evidence in ADO", href=workstream.ado_query_url))
    return LineageClaim(
        claim_id=f"{workstream.section_id}.risk",
        section_id=workstream.section_id,
        title=workstream.title,
        statement=workstream.summary or f"{workstream.title} is {risk_label(workstream.risk or RiskLevel.UNKNOWN)}",
        confidence=_max_confidence(tuple(evidence_by_item.get(item_id) for item_id in item_ids)),
        narrative_path=workstream.edit_path or f"narratives/issue_{issue_number:03d}/ws_{workstream.section_id}.md",
        narrative_line=workstream.edit_line or 1,
        source_item_ids=item_ids,
        lines=tuple(lines),
        published_issue_number=issue_number,
        edition_type=edition_type,
    )


def _build_forecast_claim(
    *,
    issue_number: int,
    edition_type: EditionType,
    forecast: ForecastAssessment,
) -> LineageClaim:
    return LineageClaim(
        claim_id="exec_summary.forecast",
        section_id="exec_summary",
        title="Forecast",
        statement=forecast.reviewer_summary,
        confidence=forecast.confidence,
        narrative_path=f"narratives/issue_{issue_number:03d}/exec_summary.md",
        narrative_line=1,
        source_item_ids=forecast.source_item_ids,
        lines=(
            LineageEntry(label="Source system", value="ADO"),
            LineageEntry(label="Source items", value=_format_source_items(forecast.source_item_ids)),
            LineageEntry(label="Forecast formula", value=forecast.formula),
            LineageEntry(label="Diff vs ETA", value=f"Predicted slip +{forecast.slip_days}d from {forecast.current_eta.isoformat()}"),
            LineageEntry(label="Rule IDs", value="forecast.velocity, forecast.eta_churn, forecast.unblocked_ratio"),
            LineageEntry(label="Override ID", value="none"),
        ),
        published_issue_number=issue_number,
        edition_type=edition_type,
    )


def _group_deltas_by_item(deltas: DeltaSet) -> dict[int, tuple[ItemDelta, ...]]:
    grouped: dict[int, list[ItemDelta]] = {}
    for delta in [*deltas.risk_changes, *deltas.new_items, *deltas.eta_changes, *deltas.closed_items]:
        grouped.setdefault(delta.work_item_id, []).append(delta)
    return {work_item_id: tuple(entries) for work_item_id, entries in grouped.items()}


def _max_confidence(evidence_packets: tuple[EvidencePacket | None, ...]) -> Confidence:
    ranking = {Confidence.NONE: 0, Confidence.LOW: 1, Confidence.MEDIUM: 2, Confidence.HIGH: 3}
    best = Confidence.NONE
    for evidence in evidence_packets:
        if evidence is not None and ranking[evidence.confidence] > ranking[best]:
            best = evidence.confidence
    return best


def _format_source_fields_for_ids(
    item_ids: tuple[int, ...],
    evidence_by_item: dict[int, EvidencePacket],
    delta_lookup: dict[int, tuple[ItemDelta, ...]],
) -> str:
    fields = sorted(
        {
            *(field_name for item_id in item_ids for field_name in _source_fields_from_evidence(evidence_by_item.get(item_id))),
            *(field_name for item_id in item_ids for field_name in _source_fields_from_deltas(delta_lookup.get(item_id, ()))),
        }
    )
    return ", ".join(field for field in fields if field) or "Lineage incomplete: missing prior archive"


def _format_source_fields(evidence: EvidencePacket | None, delta_entries: tuple[ItemDelta, ...]) -> str:
    fields = sorted({*_source_fields_from_evidence(evidence), *_source_fields_from_deltas(delta_entries)})
    return ", ".join(field for field in fields if field) or "No tracked source fields"


def _source_fields_from_evidence(evidence: EvidencePacket | None) -> tuple[str, ...]:
    if evidence is None:
        return ()
    return tuple(field_name for revision in evidence.revisions for field_name in revision.fields_changed)


def _source_fields_from_deltas(delta_entries: tuple[ItemDelta, ...]) -> tuple[str, ...]:
    mapped: list[str] = []
    for delta in delta_entries:
        if delta.kind in {DeltaKind.RISK_UP, DeltaKind.RISK_DOWN}:
            mapped.append("Custom.Risk")
        elif delta.kind == DeltaKind.ETA_CHANGED:
            mapped.append("Microsoft.VSTS.Scheduling.TargetDate")
        elif delta.kind in {DeltaKind.NEW, DeltaKind.CLOSED}:
            mapped.append("System.State")
    return tuple(mapped)


def _format_source_items(item_ids: tuple[int, ...]) -> str:
    return ", ".join(f"ADO#{item_id}" for item_id in item_ids[:5]) or "none"


def _format_latest_timestamp_for_ids(item_ids: tuple[int, ...], evidence_by_item: dict[int, EvidencePacket]) -> str:
    timestamps = [
        timestamp
        for item_id in item_ids
        for timestamp in (_latest_evidence_timestamp(evidence_by_item.get(item_id)),)
        if timestamp is not None
    ]
    latest = max(timestamps, default=None)
    return _format_timestamp(latest)


def _format_diff_for_ids(
    item_ids: tuple[int, ...],
    delta_lookup: dict[int, tuple[ItemDelta, ...]],
    previous_snapshot: Snapshot | None,
) -> str:
    if previous_snapshot is None:
        return "No prior archive - establishing baseline"
    deltas = [_delta_summary(delta) for item_id in item_ids for delta in delta_lookup.get(item_id, ())]
    return "; ".join(deltas[:4]) if deltas else "No delta vs prior issue"


def _format_rule_ids_for_ids(item_ids: tuple[int, ...], delta_lookup: dict[int, tuple[ItemDelta, ...]]) -> str:
    rule_ids = sorted({f"DeltaKind.{delta.kind.name}" for item_id in item_ids for delta in delta_lookup.get(item_id, ())})
    return ", ".join(rule_ids) if rule_ids else "none"


def _override_ids_for_section(overrides_document: OverridesDocument, issue_number: int, section_id: str) -> str:
    override_ids: list[str] = []
    for scorecard in overrides_document.scorecards:
        for dimension in scorecard.dimensions:
            if build_anchor(f"{scorecard.name}-{dimension.name}") != section_id:
                continue
            if dimension.risk is not None:
                override_ids.append(f"override:issue_{issue_number:03d}:{section_id}:risk")
            if dimension.summary is not None:
                override_ids.append(f"override:issue_{issue_number:03d}:{section_id}:summary")
            if dimension.eta is not None:
                override_ids.append(f"override:issue_{issue_number:03d}:{section_id}:eta")
            if dimension.hide_details:
                override_ids.append(f"override:issue_{issue_number:03d}:{section_id}:hide_details")
    return ", ".join(override_ids) if override_ids else "none"


def _dimension_history_note(edition_name: str, dimension_name: str, archive_root: Path) -> str | None:
    history = get_dimension_history(edition_name, dimension_name, archive_root=archive_root, last_n=3)
    if not history:
        return "Lineage incomplete: missing prior archive"
    if len(history) < 2:
        return "No prior archive - establishing baseline"
    latest = history[-1]
    prior = history[-2]
    return (
        f"Issue {int(prior.get('issue_number', 0)):03d} {prior.get('risk', 'unknown')} -> "
        f"Issue {int(latest.get('issue_number', 0)):03d} {latest.get('risk', 'unknown')}"
    )


def _delta_summary(delta: ItemDelta) -> str:
    if delta.kind in {DeltaKind.RISK_UP, DeltaKind.RISK_DOWN}:
        return f"Risk {risk_label(delta.old_risk)} -> {risk_label(delta.new_risk)}"
    if delta.kind == DeltaKind.ETA_CHANGED:
        return delta_label(delta.kind, delta.old_eta, delta.new_eta)
    if delta.kind == DeltaKind.NEW:
        return "New item"
    if delta.kind == DeltaKind.CLOSED:
        return "Closed item"
    return delta.kind.value.replace("_", " ")


def _format_diff_summary(delta_entries: tuple[ItemDelta, ...]) -> str:
    return "; ".join(_delta_summary(delta) for delta in delta_entries) or "No delta vs prior issue"


def _latest_evidence_timestamp(evidence: EvidencePacket | None) -> datetime | None:
    if evidence is None:
        return None
    timestamps = [revision.changed_date for revision in evidence.revisions]
    timestamps.extend(comment.created_date for comment in evidence.comments)
    timestamps.extend(enrichment.timestamp for enrichment in evidence.enrichments)
    if not timestamps:
        return None
    return max(timestamps)


def _format_timestamp(timestamp: datetime | None) -> str:
    if timestamp is None:
        return "No evidence in selected window"
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).strftime("%b %d %H:%M UTC")