from __future__ import annotations

from dataclasses import dataclass

from src.core.ado_status import _area_path_matches
from src.core.claim_tracker import ClaimEntry
from src.core.config_loader import ScorecardSettings
from src.core.models import RiskLevel, ScorecardEvidencePacket, WorkItem
from src.core.models_v2 import Workstream
from src.core.overrides_store import OverridesDocument
from src.core.scorecard_engine import build_scorecard


@dataclass(frozen=True, slots=True)
class ADOReconcileDiscrepancy:
    kind: str
    work_item_id: int
    context: str
    vertex_value: str
    ado_value: str
    note: str


@dataclass(frozen=True, slots=True)
class ADOReconcileReport:
    program_id: str
    override_issue_number: int | None
    discrepancies: tuple[ADOReconcileDiscrepancy, ...]


def build_ado_reconcile_report(
    *,
    program_id: str,
    items: tuple[WorkItem, ...],
    workstreams: tuple[Workstream, ...],
    scorecards: tuple[ScorecardSettings, ...],
    overrides_document: OverridesDocument | None,
    open_claims: tuple[ClaimEntry, ...],
) -> ADOReconcileReport:
    item_by_id = {item.id: item for item in items}
    discrepancies: list[ADOReconcileDiscrepancy] = []
    if overrides_document is not None:
        packet_map = _build_packet_map(items, scorecards)
        for scorecard in overrides_document.scorecards:
            for dimension in scorecard.dimensions:
                if dimension.risk is None:
                    continue
                packet = packet_map.get((scorecard.name, dimension.name))
                if packet is None:
                    continue
                for work_item_id in packet.item_ids:
                    item = item_by_id.get(work_item_id)
                    if item is None or item.risk_level == dimension.risk:
                        continue
                    discrepancies.append(
                        ADOReconcileDiscrepancy(
                            kind="override_risk",
                            work_item_id=item.id,
                            context=f"{scorecard.name} / {dimension.name}",
                            vertex_value=dimension.risk.value,
                            ado_value=item.risk_level.value,
                            note="stale override?",
                        )
                    )

    workstream_area_paths = {workstream.id: workstream.area_paths for workstream in workstreams}
    for claim in open_claims:
        for work_item_id in _claim_work_item_ids(claim):
            item = item_by_id.get(work_item_id)
            if item is None:
                continue
            if claim.due_date is not None and item.target_date != claim.due_date:
                discrepancies.append(
                    ADOReconcileDiscrepancy(
                        kind="claim_eta",
                        work_item_id=item.id,
                        context=claim.id,
                        vertex_value=claim.due_date.isoformat(),
                        ado_value=item.target_date.isoformat() if item.target_date is not None else "none",
                        note="claim contradicted",
                    )
                )
            if claim.workstream_id is None:
                continue
            expected_paths = workstream_area_paths.get(claim.workstream_id)
            if not expected_paths:
                continue
            if any(_area_path_matches(item.area_path, area_path) for area_path in expected_paths):
                continue
            discrepancies.append(
                ADOReconcileDiscrepancy(
                    kind="workstream_area",
                    work_item_id=item.id,
                    context=claim.id,
                    vertex_value=claim.workstream_id,
                    ado_value=item.area_path,
                    note="area mismatch",
                )
            )

    return ADOReconcileReport(
        program_id=program_id,
        override_issue_number=overrides_document.issue_number if overrides_document is not None else None,
        discrepancies=tuple(sorted(discrepancies, key=lambda entry: (entry.work_item_id, entry.kind, entry.context))),
    )


def render_ado_reconcile_report(report: ADOReconcileReport) -> str:
    lines = [f"Reconciliation: {report.program_id} | {len(report.discrepancies)} discrepancies found"]
    if report.override_issue_number is not None:
        lines.append(f"Overrides issue: {report.override_issue_number}")
    lines.append("")
    if not report.discrepancies:
        lines.append("  No stale overrides or contradicted claims found.")
        return "\n".join(lines)

    for discrepancy in report.discrepancies:
        if discrepancy.kind == "override_risk":
            lines.append(
                f"  WI:{discrepancy.work_item_id}  Vertex override ({discrepancy.context}): {discrepancy.vertex_value} | ADO risk: {discrepancy.ado_value}  ({discrepancy.note})"
            )
            continue
        if discrepancy.kind == "claim_eta":
            lines.append(
                f"  WI:{discrepancy.work_item_id}  Claim ETA ({discrepancy.context}): {discrepancy.vertex_value} | ADO TargetDate: {discrepancy.ado_value}  ({discrepancy.note})"
            )
            continue
        lines.append(
            f"  WI:{discrepancy.work_item_id}  Vertex ws ({discrepancy.context}): {discrepancy.vertex_value} | ADO area: {discrepancy.ado_value}  ({discrepancy.note})"
        )
    return "\n".join(lines)


def _build_packet_map(
    items: tuple[WorkItem, ...],
    scorecards: tuple[ScorecardSettings, ...],
) -> dict[tuple[str, str], ScorecardEvidencePacket]:
    packets: dict[tuple[str, str], ScorecardEvidencePacket] = {}
    for scorecard in scorecards:
        for packet in build_scorecard(items, scorecard.dimensions, prev_confirmed=None, scorecard_name=scorecard.name):
            packets[(scorecard.name, packet.dimension_name)] = packet
    return packets


def _claim_work_item_ids(claim: ClaimEntry) -> tuple[int, ...]:
    work_item_ids: list[int] = []
    for entity_ref in claim.entity_refs:
        if not entity_ref.upper().startswith("WI:"):
            continue
        _, _, suffix = entity_ref.partition(":")
        if suffix.isdigit():
            work_item_ids.append(int(suffix))
    return tuple(work_item_ids)