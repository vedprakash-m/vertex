"""Deterministic NCFL extraction engine.

Phase 1 implementation of §24.2. Generates reviewable
``ContextUpdateProposal`` objects without mutating Plane 1 stores.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re

from src.core.context_snapshot_store import ContextSnapshot, load_context_snapshot
from src.core.decision_register import load_decisions
from src.core.edition_resolver import PROGRAMS_ROOT, resolve_edition_paths
from src.core.milestone_engine import load_milestones
from src.core.ncfl_models import (
    ContextUpdateProposal,
    EXTRACTION_METHOD_CONFIDENCE,
    NCFL_EXTRACTOR_VERSION,
)
from src.core.overrides_store import load_overrides
from src.core.risk_register_engine import load_risk_register
from src.core.snapshot_store import read_snapshot
from src.core.workstream_documents import load_workstreams_document
from src.core.yaml_utils import load_yaml_mapping


@dataclass(frozen=True, slots=True)
class ExtractorInputs:
    program_id: str
    edition_id: str
    issue_number: int
    reports_root: Path
    programs_root: Path


class NcflExtractor:
    def __init__(
        self,
        *,
        program_id: str,
        edition_id: str,
        issue_number: int,
        programs_root: Path = PROGRAMS_ROOT,
        reports_root: Path | None = None,
    ) -> None:
        self.inputs = ExtractorInputs(
            program_id=program_id,
            edition_id=edition_id,
            issue_number=issue_number,
            programs_root=programs_root,
            reports_root=reports_root or programs_root.parent / "reports",
        )

    def extract_proposals(self) -> tuple[ContextUpdateProposal, ...]:
        proposals: list[ContextUpdateProposal] = []
        proposals.extend(self._extract_override_risks())
        proposals.extend(self._extract_override_decisions())
        proposals.extend(self._extract_snapshot_scorecards())
        proposals.extend(self._extract_milestone_advancements())
        proposals.extend(self._extract_context_snapshot_diffs())
        deduped = _deduplicate_latest(proposals)
        return tuple(sorted(deduped, key=lambda entry: (entry.conflict_key, entry.proposal_id)))

    def _extract_override_risks(self) -> list[ContextUpdateProposal]:
        overrides = load_overrides(
            self.inputs.edition_id,
            reports_root=self.inputs.reports_root,
            issue_number=self.inputs.issue_number,
        )
        if overrides is None:
            return []

        live_risks = load_risk_register(self.inputs.program_id, programs_root=self.inputs.programs_root)
        proposals: list[ContextUpdateProposal] = []
        for scorecard in overrides.scorecards:
            for dimension in scorecard.dimensions:
                if dimension.risk is None:
                    continue
                current_value = _current_dimension_risk_level(dimension.name, live_risks)
                proposed_value = dimension.risk.value
                if current_value == proposed_value:
                    continue
                proposals.append(
                    _build_proposal(
                        program_id=self.inputs.program_id,
                        edition_id=self.inputs.edition_id,
                        issue_number=self.inputs.issue_number,
                        source_artifact=f"overrides/issue_{self.inputs.issue_number:03d}.yaml",
                        source_field=f"scorecards.{scorecard.name}.{dimension.name}.risk",
                        source_value=proposed_value,
                        extraction_method="overrides_yaml",
                        target_store="risk_register",
                        target_key=dimension.name,
                        target_field="dimension_risk_level",
                        current_value=current_value,
                        rationale=f"Confirmed override sets {dimension.name} risk to {proposed_value}.",
                    )
                )
        return proposals

    def _extract_override_decisions(self) -> list[ContextUpdateProposal]:
        overrides = load_overrides(
            self.inputs.edition_id,
            reports_root=self.inputs.reports_root,
            issue_number=self.inputs.issue_number,
        )
        if overrides is None:
            return []

        live_decisions = load_decisions(self.inputs.program_id, programs_root=self.inputs.programs_root)
        proposals: list[ContextUpdateProposal] = []
        for index, entry in enumerate(overrides.top_3_now, start=1):
            text = entry.text.strip()
            if not text:
                continue
            target_key = _slugify(text)[:80]
            current_value = next(
                (
                    decision.decision
                    for decision in live_decisions
                    if decision.id == target_key or decision.title.strip() == text
                ),
                None,
            )
            if current_value == text:
                continue
            proposals.append(
                _build_proposal(
                    program_id=self.inputs.program_id,
                    edition_id=self.inputs.edition_id,
                    issue_number=self.inputs.issue_number,
                    source_artifact=f"overrides/issue_{self.inputs.issue_number:03d}.yaml",
                    source_field=f"top_3_now.{index}.text",
                    source_value=text,
                    extraction_method="overrides_yaml",
                    target_store="decisions",
                    target_key=target_key,
                    target_field="decision",
                    current_value=current_value,
                    rationale="Confirmed top_3_now entry implies a governance decision or ask worth staging.",
                )
            )
        return proposals

    def _extract_snapshot_scorecards(self) -> list[ContextUpdateProposal]:
        snapshot = _load_issue_snapshot(self.inputs)
        if snapshot is None:
            return []

        live_risks = load_risk_register(self.inputs.program_id, programs_root=self.inputs.programs_root)
        proposals: list[ContextUpdateProposal] = []
        for dimension in snapshot.scorecards:
            current_value = _current_dimension_risk_level(dimension.name, live_risks)
            proposed_value = dimension.risk.value
            if current_value == proposed_value:
                continue
            proposals.append(
                _build_proposal(
                    program_id=self.inputs.program_id,
                    edition_id=self.inputs.edition_id,
                    issue_number=self.inputs.issue_number,
                    source_artifact=f"archive/{self.inputs.edition_id}/snapshots/issue_{self.inputs.issue_number:03d}.snapshot.json",
                    source_field=f"scorecards.{dimension.scorecard_name}.{dimension.name}.risk",
                    source_value=proposed_value,
                    extraction_method="scorecard_data",
                    target_store="risk_register",
                    target_key=dimension.name,
                    target_field="dimension_risk_level",
                    current_value=current_value,
                    rationale="Confirmed snapshot scorecard risk deterministically maps to a risk-register dimension state.",
                )
            )
        return proposals

    def _extract_milestone_advancements(self) -> list[ContextUpdateProposal]:
        snapshot = _load_issue_snapshot(self.inputs)
        if snapshot is None:
            return []

        live_milestones = load_milestones(self.inputs.program_id, programs_root=self.inputs.programs_root)
        item_state = {item.id: item.state.strip().lower() for item in snapshot.items}
        proposals: list[ContextUpdateProposal] = []
        for milestone in live_milestones:
            if milestone.status.value == "completed" or not milestone.linked_work_item_ids:
                continue
            if not all(item_state.get(item_id, "") in {"closed", "done", "resolved", "completed"} for item_id in milestone.linked_work_item_ids):
                continue
            proposals.append(
                _build_proposal(
                    program_id=self.inputs.program_id,
                    edition_id=self.inputs.edition_id,
                    issue_number=self.inputs.issue_number,
                    source_artifact=f"archive/{self.inputs.edition_id}/snapshots/issue_{self.inputs.issue_number:03d}.snapshot.json",
                    source_field=f"items.linked_to.{milestone.id}.state",
                    source_value="completed",
                    extraction_method="ado_snapshot",
                    target_store="milestones",
                    target_key=milestone.id,
                    target_field="status",
                    current_value=milestone.status.value,
                    rationale="All linked ADO items are terminal, so the milestone may be ready to advance.",
                )
            )
        return proposals

    def _extract_context_snapshot_diffs(self) -> list[ContextUpdateProposal]:
        snapshot = load_context_snapshot(
            self.inputs.program_id,
            self.inputs.edition_id,
            self.inputs.issue_number,
            archive_root=self.inputs.programs_root,
        )
        if snapshot is None:
            return []

        proposals: list[ContextUpdateProposal] = []
        proposals.extend(self._diff_snapshot_milestones(snapshot))
        proposals.extend(self._diff_snapshot_risks(snapshot))
        proposals.extend(self._diff_snapshot_workstreams(snapshot))
        return proposals

    def _diff_snapshot_milestones(self, snapshot: ContextSnapshot) -> list[ContextUpdateProposal]:
        live_milestones = {
            entry.id: entry
            for entry in load_milestones(self.inputs.program_id, programs_root=self.inputs.programs_root)
        }
        proposals: list[ContextUpdateProposal] = []
        for record in snapshot.milestones:
            milestone_id = str(record.get("id") or "").strip()
            live = live_milestones.get(milestone_id)
            source_value = _normalized_optional_text(record.get("status"))
            if live is None or source_value is None or source_value == live.status.value:
                continue
            proposals.append(
                _build_proposal(
                    program_id=self.inputs.program_id,
                    edition_id=self.inputs.edition_id,
                    issue_number=self.inputs.issue_number,
                    source_artifact=_context_snapshot_artifact(self.inputs),
                    source_field=f"milestones.{milestone_id}.status",
                    source_value=source_value,
                    extraction_method="context_snapshot_diff",
                    target_store="milestones",
                    target_key=milestone_id,
                    target_field="status",
                    current_value=live.status.value,
                    rationale="Context snapshot diverges from the live milestone status; stage for review.",
                )
            )
        return proposals

    def _diff_snapshot_risks(self, snapshot: ContextSnapshot) -> list[ContextUpdateProposal]:
        live_risks = {
            entry.id: entry
            for entry in load_risk_register(self.inputs.program_id, programs_root=self.inputs.programs_root)
        }
        proposals: list[ContextUpdateProposal] = []
        for record in snapshot.risks:
            risk_id = str(record.get("id") or "").strip()
            live = live_risks.get(risk_id)
            source_value = _normalized_optional_text(record.get("owner_alias"))
            if live is None or source_value is None or source_value == live.owner_alias:
                continue
            proposals.append(
                _build_proposal(
                    program_id=self.inputs.program_id,
                    edition_id=self.inputs.edition_id,
                    issue_number=self.inputs.issue_number,
                    source_artifact=_context_snapshot_artifact(self.inputs),
                    source_field=f"risks.{risk_id}.owner_alias",
                    source_value=source_value,
                    extraction_method="context_snapshot_diff",
                    target_store="risk_register",
                    target_key=risk_id,
                    target_field="owner_alias",
                    current_value=live.owner_alias,
                    rationale="Context snapshot diverges from the live risk owner; stage for review.",
                )
            )
        return proposals

    def _diff_snapshot_workstreams(self, snapshot: ContextSnapshot) -> list[ContextUpdateProposal]:
        resolved = resolve_edition_paths(self.inputs.edition_id, programs_root=self.inputs.programs_root)
        if resolved is None:
            return []

        raw_workstreams = load_yaml_mapping(resolved.program_dir / "workstreams.yaml", required=False, default={})
        live_workstreams = {
            entry.id: entry
            for entry in load_workstreams_document(raw_workstreams, resolved.program_dir / "workstreams.yaml")
        }

        proposals: list[ContextUpdateProposal] = []
        for record in snapshot.workstreams:
            workstream_id = str(record.get("id") or "").strip()
            live = live_workstreams.get(workstream_id)
            if live is None:
                continue
            for field in ("current_blocker", "dri_email"):
                source_value = _normalized_optional_text(record.get(field))
                current_value = _normalized_optional_text(getattr(live, field, None))
                if source_value is None or source_value == current_value:
                    continue
                proposals.append(
                    _build_proposal(
                        program_id=self.inputs.program_id,
                        edition_id=self.inputs.edition_id,
                        issue_number=self.inputs.issue_number,
                        source_artifact=_context_snapshot_artifact(self.inputs),
                        source_field=f"workstreams.{workstream_id}.{field}",
                        source_value=source_value,
                        extraction_method="context_snapshot_diff",
                        target_store="workstreams",
                        target_key=workstream_id,
                        target_field=field,
                        current_value=current_value,
                        rationale=f"Context snapshot diverges from live workstream {field}; stage for review.",
                    )
                )
        return proposals


def extract_proposals(
    program_id: str,
    edition_id: str,
    issue_number: int,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    reports_root: Path | None = None,
) -> tuple[ContextUpdateProposal, ...]:
    return NcflExtractor(
        program_id=program_id,
        edition_id=edition_id,
        issue_number=issue_number,
        programs_root=programs_root,
        reports_root=reports_root,
    ).extract_proposals()


def _build_proposal(
    *,
    program_id: str,
    edition_id: str,
    issue_number: int,
    source_artifact: str,
    source_field: str,
    source_value: str,
    extraction_method: str,
    target_store: str,
    target_key: str,
    target_field: str,
    current_value: str | None,
    rationale: str,
) -> ContextUpdateProposal:
    normalized_current = _normalized_optional_text(current_value)
    proposal_core = f"{target_store}|{target_key}|{target_field}|{source_artifact}|{source_field}|{source_value}"
    conflict_core = f"{target_store}|{target_key}|{target_field}"
    confidence = EXTRACTION_METHOD_CONFIDENCE[extraction_method]
    return ContextUpdateProposal(
        proposal_id=_hash_value(proposal_core)[:16],
        program_id=program_id,
        issue_number=issue_number,
        edition_id=edition_id,
        source_type=_source_type_for_method(extraction_method),
        extracted_at=datetime.now(timezone.utc),
        extractor_version=NCFL_EXTRACTOR_VERSION,
        source_artifact=source_artifact,
        source_field=source_field,
        extraction_method=extraction_method,
        target_store=target_store,
        target_key=target_key,
        target_field=target_field,
        source_value=source_value,
        current_value=normalized_current,
        current_value_hash=(_hash_value(normalized_current) if normalized_current is not None else None),
        confidence=confidence,
        batch_eligible=(confidence == "high" and normalized_current is not None),
        extraction_method_rationale=rationale,
        conflict_key=_hash_value(conflict_core)[:16],
    )


def _current_dimension_risk_level(dimension_name: str, risks: tuple) -> str | None:
    for risk in risks:
        if risk.dimension_id == dimension_name:
            return risk.impact.value
        if dimension_name in risk.linked_workstream_ids:
            return risk.impact.value
        if risk.title.strip() == dimension_name:
            return risk.impact.value
    return None


def _source_type_for_method(method: str) -> str:
    if method == "overrides_yaml":
        return "confirmed_overrides"
    if method == "context_snapshot_diff":
        return "context_snapshot"
    if method == "field_update_email":
        return "field_update_email"
    return "published_narrative"


def _load_issue_snapshot(inputs: ExtractorInputs):
    resolved = resolve_edition_paths(inputs.edition_id, programs_root=inputs.programs_root)
    if resolved is None:
        return None
    path = resolved.archive_dir / "snapshots" / f"issue_{inputs.issue_number:03d}.snapshot.json"
    if not path.exists():
        return None
    return read_snapshot(path)


def _hash_value(value: str | None) -> str:
    return sha256((value or "").encode("utf-8")).hexdigest()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "proposal"


def _context_snapshot_artifact(inputs: ExtractorInputs) -> str:
    return f"archive/{inputs.edition_id}/context_snapshots/issue_{inputs.issue_number:03d}.context.json"


def _normalized_optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _deduplicate_latest(proposals: list[ContextUpdateProposal]) -> tuple[ContextUpdateProposal, ...]:
    latest: dict[tuple[str, str], ContextUpdateProposal] = {}
    for proposal in proposals:
        latest[(proposal.conflict_key, proposal.source_value)] = proposal
    return tuple(latest.values())
