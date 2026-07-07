from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.core.ado_proposal import read_proposal_manifest
from src.core.assumption_tracker import check_validation_due
from src.core.edition_resolver import PROGRAMS_ROOT, get_program_output_dir, _output_subdir, _OUTPUT_SUBDIR_LEGACY
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import ActionItem, Assumption, AssumptionStatus, DecisionAsk, Milestone, RiskEntry, RiskStatus
from src.core.risk_register_engine import assess_risk_staleness, compute_risk_score


@dataclass(frozen=True, slots=True)
class OwnerPackVitalitySummary:
    composite_score: int
    total_items: int
    fresh_items: int
    avg_richness: float
    total_leakage: int
    workiq_signal_count: int


@dataclass(frozen=True, slots=True)
class OwnerPackProposalEntry:
    proposal_id: str
    edition_id: str | None
    proposal_status: str
    work_item_id: int
    action: str
    field_or_tag: str
    entry_status: str


@dataclass(frozen=True, slots=True)
class OwnerPackMilestoneContribution:
    milestone_id: str
    name: str
    target_date: date
    status: str
    relation: str
    notes: str | None = None
    computed_status: str | None = None
    schedule_summary: str | None = None
    target_history_summary: str | None = None
    completion_history_summary: str | None = None


@dataclass(frozen=True, slots=True)
class OwnerPackCalibrationSummary:
    owner_alias: str
    claim_accuracy: float | None
    sample_size: int
    met: int
    contradicted: int
    stale: int
    slip_modifier: float


@dataclass(frozen=True, slots=True)
class OwnerPack:
    program_id: str
    owner_alias: str
    generated_at: datetime
    items: tuple[WorkItem, ...]
    open_risks: tuple[WorkItem, ...]
    risk_register_entries: tuple[RiskEntry, ...]
    milestone_contributions: tuple[OwnerPackMilestoneContribution, ...]
    stale_items: tuple[WorkItem, ...]
    open_actions: tuple[ActionItem, ...]
    open_assumptions: tuple[Assumption, ...]
    overdue_assumption_ids: frozenset[str]
    resolution_candidate_action_ids: frozenset[str]
    open_decision_asks: tuple[DecisionAsk, ...]
    proposal_entries: tuple[OwnerPackProposalEntry, ...]
    vitality_summary: OwnerPackVitalitySummary | None = None
    telemetry_summary: str | None = None
    calibration_summary: OwnerPackCalibrationSummary | None = None


def build_owner_pack(
    *,
    program_id: str,
    owner_alias: str,
    items: tuple[WorkItem, ...],
    risk_register_entries: tuple[RiskEntry, ...],
    milestones: tuple[Milestone, ...],
    open_actions: tuple[ActionItem, ...],
    assumptions: tuple[Assumption, ...],
    resolution_candidate_action_ids: frozenset[str] = frozenset(),
    open_decision_asks: tuple[DecisionAsk, ...],
    scoped_workstream_ids: tuple[str, ...] = (),
    scoped_item_ids: tuple[int, ...] = (),
    generated_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
    stale_after_days: int = 14,
    vitality_summary: OwnerPackVitalitySummary | None = None,
    telemetry_summary: str | None = None,
    calibration_summary: OwnerPackCalibrationSummary | None = None,
) -> OwnerPack:
    normalized_owner = _normalize_owner_alias(owner_alias)
    scoped_workstream_id_set = set(scoped_workstream_ids)
    scoped_item_id_set = set(scoped_item_ids)
    has_raci_scope = bool(scoped_workstream_id_set or scoped_item_id_set)
    owner_items = tuple(
        sorted(
            (
                item
                for item in items
                if _normalize_owner_alias(item.assigned_to_email or item.assigned_to) == normalized_owner
                or item.id in scoped_item_id_set
            ),
            key=lambda item: (item.risk_level.value != RiskLevel.HIGH.value, item.id),
        )
    )
    owner_item_ids = {item.id for item in owner_items}
    open_risks = tuple(
        item
        for item in owner_items
        if item.risk_level in {RiskLevel.HIGH, RiskLevel.MEDIUM}
    )
    owner_risk_entries = tuple(
        sorted(
            (
                entry
                for entry in risk_register_entries
                if (
                    _normalize_owner_alias(entry.owner_alias) == normalized_owner
                    or _entry_matches_scope(
                        entry=entry,
                        scoped_workstream_ids=scoped_workstream_id_set,
                        scoped_item_ids=scoped_item_id_set,
                    )
                )
                and entry.status in {RiskStatus.OPEN, RiskStatus.ESCALATED}
            ),
            key=lambda entry: (-compute_risk_score(entry), entry.id),
        )
    )
    owner_milestones = tuple(
        sorted(
            (
                OwnerPackMilestoneContribution(
                    milestone_id=milestone.id,
                    name=milestone.name,
                    target_date=milestone.target_date,
                    status=milestone.status.value,
                    relation=relation,
                    notes=milestone.notes,
                )
                for milestone in milestones
                if (
                    relation := _resolve_milestone_relation(
                        milestone,
                        normalized_owner,
                        owner_item_ids,
                        scoped_workstream_id_set,
                    )
                ) is not None
            ),
            key=lambda milestone: (milestone.target_date, milestone.milestone_id),
        )
    )
    owner_milestone_ids = {milestone.milestone_id for milestone in owner_milestones}
    stale_cutoff = _ensure_utc(generated_at) - timedelta(days=stale_after_days)
    stale_items = tuple(
        item
        for item in owner_items
        if (_changed_at := _item_changed_at(item)) is None or _changed_at <= stale_cutoff
    )
    owner_actions = tuple(
        sorted(
            (
                action
                for action in open_actions
                if _normalize_owner_alias(action.owner_alias) == normalized_owner
                or (action.workstream_id is not None and action.workstream_id in scoped_workstream_id_set)
            ),
            key=lambda action: (action.due_date or datetime.max.date(), action.id),
        )
    )
    open_assumptions = tuple(
        assumption
        for assumption in assumptions
        if assumption.status is AssumptionStatus.UNVALIDATED
        and (
            _normalize_owner_alias(assumption.owner_alias) == normalized_owner
            or (assumption.linked_milestone_id is not None and assumption.linked_milestone_id in owner_milestone_ids)
            or _assumption_matches_item_scope(assumption, scoped_item_id_set)
        )
    )
    overdue_assumption_ids = frozenset(
        entry.id for entry in check_validation_due(open_assumptions, _ensure_utc(generated_at).date())
    )
    owner_assumptions = tuple(
        sorted(
            open_assumptions,
            key=lambda entry: (
                0 if entry.id in overdue_assumption_ids else 1,
                entry.validation_due or date.max,
                entry.id,
            ),
        )
    )
    decision_asks = tuple(
        ask
        for ask in sorted(open_decision_asks, key=lambda entry: (entry.issue_number, entry.id))
        if _normalize_owner_alias(ask.owner_alias) == normalized_owner
        or (has_raci_scope and _ask_matches_item_scope(ask, scoped_item_id_set))
    )
    proposal_entries = load_owner_proposal_entries(
        tuple(item.id for item in owner_items),
        programs_root=programs_root,
    )
    return OwnerPack(
        program_id=program_id,
        owner_alias=normalized_owner,
        generated_at=_ensure_utc(generated_at),
        items=owner_items,
        open_risks=open_risks,
        risk_register_entries=owner_risk_entries,
        milestone_contributions=owner_milestones,
        stale_items=stale_items,
        open_actions=owner_actions,
        open_assumptions=owner_assumptions,
        overdue_assumption_ids=overdue_assumption_ids,
        resolution_candidate_action_ids=frozenset(
            action_id for action_id in resolution_candidate_action_ids if any(action.id == action_id for action in owner_actions)
        ),
        open_decision_asks=decision_asks,
        proposal_entries=proposal_entries,
        vitality_summary=vitality_summary,
        telemetry_summary=telemetry_summary,
        calibration_summary=calibration_summary,
    )


def load_owner_proposal_entries(
    owner_item_ids: tuple[int, ...],
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[OwnerPackProposalEntry, ...]:
    item_ids = set(owner_item_ids)
    entries: list[OwnerPackProposalEntry] = []
    # Transition-window: also check legacy path if canonical yields nothing
    _canonical_paths = sorted(programs_root.glob(f"*/{_output_subdir()}/*/ado_proposals/*.json"))
    _all_paths = _canonical_paths or (
        sorted(programs_root.glob(f"*/{_OUTPUT_SUBDIR_LEGACY}/*/ado_proposals/*.json"))
        if _output_subdir() != _OUTPUT_SUBDIR_LEGACY else []
    )
    for path in _all_paths:
        proposal, proposal_status = read_proposal_manifest(path)
        for entry in proposal.entries:
            if entry.work_item_id not in item_ids:
                continue
            entries.append(
                OwnerPackProposalEntry(
                    proposal_id=proposal.id,
                    edition_id=proposal.edition_id,
                    proposal_status=proposal_status,
                    work_item_id=entry.work_item_id,
                    action=entry.action,
                    field_or_tag=entry.field_or_tag,
                    entry_status=entry.entry_status,
                )
            )
    return tuple(
        sorted(
            entries,
            key=lambda entry: (entry.proposal_id, entry.work_item_id, entry.action),
        )
    )


def render_owner_pack_markdown(pack: OwnerPack) -> str:
    lines = [
        f"# Owner Pack - {pack.owner_alias}",
        "",
        f"Program: {pack.program_id}",
        f"Generated: {pack.generated_at.isoformat()}",
        f"Items: {len(pack.items)} | Open risks: {len(pack.open_risks)} | Risk register entries: {len(pack.risk_register_entries)} | Milestone contributions: {len(pack.milestone_contributions)} | Stale items: {len(pack.stale_items)} | Open actions: {len(pack.open_actions)} | Open assumptions: {len(pack.open_assumptions)} | Open asks: {len(pack.open_decision_asks)} | Proposed ADO updates: {len(pack.proposal_entries)}",
        "",
        "## Current Items",
    ]
    if pack.items:
        for item in pack.items:
            target_date = item.target_date.isoformat() if item.target_date is not None else "-"
            lines.append(
                f"- WI:{item.id} | {item.risk_level.value} | {item.state} | target {target_date} | {item.title}"
            )
    else:
        lines.append("- none")

    lines.extend(("", "## Vitality Summary"))
    if pack.vitality_summary is not None:
        lines.append(
            f"- Composite {pack.vitality_summary.composite_score}% | {pack.vitality_summary.fresh_items}/{pack.vitality_summary.total_items} fresh | avg richness {pack.vitality_summary.avg_richness:.1f} | leakage {pack.vitality_summary.total_leakage}/{pack.vitality_summary.workiq_signal_count}"
        )
    else:
        lines.append("- none")

    lines.extend(("", "## Telemetry"))
    if pack.telemetry_summary is not None:
        lines.append(f"- {pack.telemetry_summary}")
    else:
        lines.append("- none")

    lines.extend(("", "## Calibration Profile"))
    if pack.calibration_summary is not None:
        lines.append(
            f"- {pack.calibration_summary.owner_alias}: {_format_percent(pack.calibration_summary.claim_accuracy)} met ({pack.calibration_summary.met}/{pack.calibration_summary.sample_size}) | {pack.calibration_summary.contradicted} contradicted | {pack.calibration_summary.stale} stale | slip modifier +{pack.calibration_summary.slip_modifier:.2f}"
        )
    else:
        lines.append("- none")

    lines.extend(("", "## Open Risks"))
    if pack.open_risks:
        for item in pack.open_risks:
            lines.append(f"- WI:{item.id} | {item.risk_level.value} | {item.title}")
    else:
        lines.append("- none")

    lines.extend(("", "## Risk Register Entries"))
    if pack.risk_register_entries:
        for entry in pack.risk_register_entries:
            due_label = entry.mitigation_due_date.isoformat() if entry.mitigation_due_date is not None else "-"
            stale_label = " | review stale" if assess_risk_staleness(entry, _ensure_utc(pack.generated_at).date()) else ""
            lines.append(
                f"- {entry.id} | {entry.status.value} | {entry.probability.value} x {entry.impact.value} | mitigation due {due_label}{stale_label} | {entry.title}"
            )
            lines.append(f"  {entry.description}")
            if entry.mitigation_plan:
                lines.append(f"  mitigation: {entry.mitigation_plan}")
    else:
        lines.append("- none")

    lines.extend(("", "## Milestone Contributions"))
    if pack.milestone_contributions:
        for milestone in pack.milestone_contributions:
            milestone_line = f"- {milestone.milestone_id} | target {milestone.target_date.isoformat()} | {milestone.status}"
            if milestone.computed_status:
                milestone_line += f" | computed {milestone.computed_status}"
            milestone_line += f" | {milestone.relation} | {milestone.name}"
            lines.append(milestone_line)
            if milestone.notes:
                lines.append(f"  notes: {milestone.notes}")
            if milestone.schedule_summary:
                lines.append(f"  schedule: {milestone.schedule_summary}")
            if milestone.target_history_summary:
                lines.append(f"  {milestone.target_history_summary}")
            if milestone.completion_history_summary:
                lines.append(f"  {milestone.completion_history_summary}")
    else:
        lines.append("- none")

    lines.extend(("", "## Stale Items"))
    if pack.stale_items:
        for item in pack.stale_items:
            changed_at = _item_changed_at(item)
            changed_label = changed_at.date().isoformat() if changed_at is not None else "unknown"
            lines.append(f"- WI:{item.id} | last changed {changed_label} | {item.title}")
    else:
        lines.append("- none")

    lines.extend(("", "## Open Actions"))
    if pack.open_actions:
        for action in pack.open_actions:
            due_label = action.due_date.isoformat() if action.due_date is not None else "-"
            candidate_label = " | candidate for resolution" if action.id in pack.resolution_candidate_action_ids else ""
            lines.append(f"- {action.id} | {action.status.value} | due {due_label}{candidate_label} | {action.text}")
    else:
        lines.append("- none")

    lines.extend(("", "## Open Assumptions"))
    if pack.open_assumptions:
        for assumption in pack.open_assumptions:
            due_label = assumption.validation_due.isoformat() if assumption.validation_due is not None else "-"
            owner_label = assumption.owner_alias or "-"
            recency_label = "overdue" if assumption.id in pack.overdue_assumption_ids else "current"
            lines.append(
                f"- {assumption.id} | {assumption.status.value} | {recency_label} | due {due_label} | owner {owner_label} | {assumption.text}"
            )
            details: list[str] = []
            if assumption.validation_method:
                details.append(f"method {assumption.validation_method}")
            if assumption.linked_milestone_id:
                details.append(f"milestone {assumption.linked_milestone_id}")
            if assumption.linked_risk_id:
                details.append(f"risk {assumption.linked_risk_id}")
            if details:
                lines.append(f"  {' | '.join(details)}")
    else:
        lines.append("- none")

    lines.extend(("", "## Open Asks"))
    if pack.open_decision_asks:
        for ask in pack.open_decision_asks:
            lines.append(f"- {ask.id} | issue #{ask.issue_number} | {ask.text}")
    else:
        lines.append("- none")

    lines.extend(("", "## Proposed ADO Updates"))
    if pack.proposal_entries:
        for prop_entry in pack.proposal_entries:
            lines.append(
                f"- {prop_entry.proposal_id} | WI:{prop_entry.work_item_id} | {prop_entry.action} {prop_entry.field_or_tag} | proposal={prop_entry.proposal_status} | entry={prop_entry.entry_status}"
            )
    else:
        lines.append("- none")

    return "\n".join(lines)


def get_owner_pack_output_path(
    program_id: str,
    owner_alias: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    return programs_root / program_id / "owner_packs" / f"{_normalize_owner_alias(owner_alias)}.md"


def write_owner_pack(pack: OwnerPack, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    path = get_owner_pack_output_path(pack.program_id, pack.owner_alias, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_owner_pack_markdown(pack), encoding="utf-8")
    return path


def _item_changed_at(item: WorkItem) -> datetime | None:
    raw_value = item.custom_fields.get("changed_date")
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    parsed = datetime.fromisoformat(raw_value)
    return _ensure_utc(parsed)


def _normalize_owner_alias(value: str | None) -> str:
    if value is None:
        return "unassigned"
    normalized = value.strip().lower()
    if "@" in normalized:
        normalized = normalized.split("@", 1)[0]
    return normalized or "unassigned"


def _ensure_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _format_percent(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{round(value * 100):d}%"


def _resolve_milestone_relation(
    milestone: Milestone,
    normalized_owner: str,
    owner_item_ids: set[int],
    scoped_workstream_ids: set[str],
) -> str | None:
    parts: list[str] = []
    if _normalize_owner_alias(milestone.owner_alias) == normalized_owner:
        parts.append("owner")
    linked_owner_item_ids = tuple(item_id for item_id in milestone.linked_work_item_ids if item_id in owner_item_ids)
    if linked_owner_item_ids:
        parts.append("linked " + ", ".join(f"WI:{item_id}" for item_id in linked_owner_item_ids))
    linked_scoped_workstreams = tuple(
        workstream_id
        for workstream_id in milestone.linked_workstream_ids
        if workstream_id in scoped_workstream_ids
    )
    if linked_scoped_workstreams:
        parts.append("raci " + ", ".join(linked_scoped_workstreams))
    if not parts:
        return None
    return " + ".join(parts)


def _entry_matches_scope(
    *,
    entry: RiskEntry,
    scoped_workstream_ids: set[str],
    scoped_item_ids: set[int],
) -> bool:
    return bool(
        scoped_workstream_ids.intersection(entry.linked_workstream_ids)
        or scoped_item_ids.intersection(entry.linked_work_item_ids)
    )


def _ask_matches_item_scope(ask: DecisionAsk, scoped_item_ids: set[int]) -> bool:
    if not scoped_item_ids:
        return False
    for entity_ref in ask.entity_refs:
        if not entity_ref.upper().startswith("WI:"):
            continue
        candidate = entity_ref.split(":", 1)[1].strip()
        if candidate.isdigit() and int(candidate) in scoped_item_ids:
            return True
    return False


def _assumption_matches_item_scope(assumption: Assumption, scoped_item_ids: set[int]) -> bool:
    if not scoped_item_ids:
        return False
    for entity_ref in assumption.entity_refs:
        if not entity_ref.upper().startswith("WI:"):
            continue
        candidate = entity_ref.split(":", 1)[1].strip()
        if candidate.isdigit() and int(candidate) in scoped_item_ids:
            return True
    return False