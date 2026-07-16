from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import re

from src.core.models import Confidence, WorkItem
from src.core.models_v2 import (
    ClaimEntry,
    Contradiction,
    ContradictionPacket,
    DataSourceType,
    Dependency,
    ForecastCalibrationModifier,
    ResolvedContradiction,
    RiskEntry,
    Milestone,
    ActionItem,
    Signal,
    Workstream,
)
from src.core.workstream_path_resolver import resolve_workstream_id_loose_longest as _resolve_workstream_id


_ISO_DATE_PATTERN = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_MONTH_DATE_PATTERN = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2})(?:,\s*(20\d{2}))?\b",
    re.IGNORECASE,
)
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


@dataclass(frozen=True, slots=True)
class ContradictionRule:
    name: str
    requires: frozenset[DataSourceType]


_CLAIM_VS_ADO_RULE = ContradictionRule(
    name="claim_vs_ado_target_date",
    requires=frozenset({DataSourceType.ADO, DataSourceType.JOURNAL}),
)
_SIGNAL_VS_ADO_RULE = ContradictionRule(
    name="signal_vs_ado_target_date",
    requires=frozenset({DataSourceType.ADO}),
)
_CLAIM_VS_ADO_OWNER_RULE = ContradictionRule(
    name="claim_vs_ado_owner",
    requires=frozenset({DataSourceType.ADO, DataSourceType.JOURNAL}),
)
_CLAIM_VS_DEPENDENCY_STATUS_RULE = ContradictionRule(
    name="claim_vs_dependency_status",
    requires=frozenset({DataSourceType.ADO, DataSourceType.JOURNAL}),
)
# ADF-W2.10 P6 (Section 8.10.9): the only vocabulary `_evaluate_claim_vs_
# dependency_status` recognizes -- mirrors `DependencyStatus`'s own values
# exactly, so a claim must name one of these three or the comparison is
# skipped (mapping freer narrative wording onto this vocabulary is the
# extractor/prompt's job, not this rule's).
_RECOGNIZED_DEPENDENCY_STATUS_VALUES = frozenset({"active", "resolved", "broken"})
_CLAIM_VS_RISK_STATUS_RULE = ContradictionRule(
    name="claim_vs_risk_status",
    requires=frozenset({DataSourceType.ADO, DataSourceType.JOURNAL}),
)
_CLAIM_VS_MILESTONE_STATUS_RULE = ContradictionRule(
    name="claim_vs_milestone_status",
    requires=frozenset({DataSourceType.ADO, DataSourceType.JOURNAL}),
)
_CLAIM_VS_ACTION_STATUS_RULE = ContradictionRule(
    name="claim_vs_action_status",
    requires=frozenset({DataSourceType.ADO, DataSourceType.JOURNAL}),
)
# ADF-W2.10 P7 (Section 8.10.9): mirrors RiskStatus / MilestoneStatus /
# ActionStatus enum values exactly so a claim must name one of a family's
# recognized values or the comparison is skipped -- mapping freer narrative
# wording onto this vocabulary is the extractor/prompt's job, not the
# comparison rule's. Each family compares status only (risk probability/
# impact, milestone computed-health, action closure reasons are separate
# axes that would each need their own schema decision).
_RECOGNIZED_RISK_STATUS_VALUES = frozenset({"open", "mitigated", "accepted", "closed", "escalated"})
_RECOGNIZED_MILESTONE_STATUS_VALUES = frozenset({"on_track", "at_risk", "missed", "completed", "deferred", "unknown"})
_RECOGNIZED_ACTION_STATUS_VALUES = frozenset({"proposed", "open", "in_progress", "done", "cancelled"})


def build_contradiction_packets(
    *,
    items: tuple[WorkItem, ...],
    claims: tuple[ClaimEntry, ...],
    signals: tuple[Signal, ...],
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
    calibration_modifier: ForecastCalibrationModifier | None = None,
    dependencies: tuple[Dependency, ...] = (),
    risks: tuple[RiskEntry, ...] = (),
    milestones: tuple[Milestone, ...] = (),
    actions: tuple[ActionItem, ...] = (),
) -> tuple[ContradictionPacket, ...]:
    item_lookup = {item.id: item for item in items}
    # Keyed upper() because `ClaimExtractor._parse_entity_refs` upper-cases
    # the entire `DEP:<id>`/`RISK:<id>`/`MS:<id>`/`ACTION:<id>` ref
    # (including the id segment) when normalizing claim entity refs, but the
    # record id itself may be authored in any case -- match case-insensitively.
    dependency_lookup = {dependency.id.upper(): dependency for dependency in dependencies}
    risk_lookup = {risk.id.upper(): risk for risk in risks}
    milestone_lookup = {milestone.id.upper(): milestone for milestone in milestones}
    action_lookup = {action.id.upper(): action for action in actions}
    contradictions_by_item: dict[int, list[Contradiction]] = {}
    recommendations: dict[int, ResolvedContradiction] = {}
    generated_at = _ensure_utc(as_of)

    for claim in claims:
        claim_contradictions = (
            _evaluate_claim_vs_ado_target(claim=claim, item_lookup=item_lookup),
            _evaluate_claim_vs_ado_owner(claim=claim, item_lookup=item_lookup),
            _evaluate_claim_vs_dependency_status(
                claim=claim, dependency_lookup=dependency_lookup, item_lookup=item_lookup
            ),
            _evaluate_claim_vs_risk_status(
                claim=claim, risk_lookup=risk_lookup, item_lookup=item_lookup
            ),
            _evaluate_claim_vs_milestone_status(
                claim=claim, milestone_lookup=milestone_lookup, item_lookup=item_lookup
            ),
            _evaluate_claim_vs_action_status(
                claim=claim, action_lookup=action_lookup, item_lookup=item_lookup
            ),
        )
        for contradiction in claim_contradictions:
            if contradiction is None:
                continue
            contradictions_by_item.setdefault(contradiction[0], []).append(contradiction[1])
            recommendation = _recommend_resolution(
                item=item_lookup[contradiction[0]],
                contradiction=contradiction[1],
                workstreams=workstreams,
                calibration_modifier=calibration_modifier,
            )
            if recommendation is not None:
                recommendations[contradiction[0]] = recommendation

    for signal in signals:
        contradiction = _evaluate_signal_vs_ado_target(
            signal=signal,
            item_lookup=item_lookup,
            reference_date=generated_at.date(),
        )
        if contradiction is None:
            continue
        contradictions_by_item.setdefault(contradiction[0], []).append(contradiction[1])
        recommendation = _recommend_resolution(
            item=item_lookup[contradiction[0]],
            contradiction=contradiction[1],
            workstreams=workstreams,
            calibration_modifier=calibration_modifier,
        )
        if recommendation is not None:
            recommendations[contradiction[0]] = recommendation

    packets: list[ContradictionPacket] = []
    for work_item_id, contradictions in sorted(contradictions_by_item.items()):
        item = item_lookup[work_item_id]
        workstream_id = _resolve_workstream_id(item.area_path, workstreams)
        confidence = max((entry.confidence for entry in contradictions), key=_confidence_rank)
        packets.append(
            ContradictionPacket(
                work_item_id=work_item_id,
                workstream_id=workstream_id,
                contradictions=tuple(sorted(contradictions, key=lambda entry: (entry.field, entry.source_a, entry.source_b, entry.summary))),
                confidence=confidence,
                recommended_resolution=recommendations.get(work_item_id),
                generated_at=generated_at,
            )
        )
    return tuple(packets)


def _evaluate_claim_vs_ado_target(
    *,
    claim: ClaimEntry,
    item_lookup: dict[int, WorkItem],
) -> tuple[int, Contradiction] | None:
    item = _first_referenced_item(claim.entity_refs, item_lookup)
    if item is None or item.target_date is None or claim.due_date is None or item.target_date == claim.due_date:
        return None
    days_delta = abs((item.target_date - claim.due_date).days)
    confidence = Confidence.HIGH if days_delta >= 14 else Confidence.MEDIUM
    summary = (
        f"ADO target date {item.target_date.isoformat()} disagrees with open claim due date {claim.due_date.isoformat()}"
    )
    return (
        item.id,
        Contradiction(
            field="target_date",
            source_a="ado/target_date",
            source_b="journal/claim",
            summary=summary,
            confidence=confidence,
            evidence_refs=tuple(sorted({*claim.entity_refs, claim.id})),
        ),
    )


def _evaluate_claim_vs_ado_owner(
    *,
    claim: ClaimEntry,
    item_lookup: dict[int, WorkItem],
) -> tuple[int, Contradiction] | None:
    """ADF-W2.10 (Section 8.10.9): the same "structured (ADO) vs
    narrative-derived (claim) representation" comparison as
    ``_evaluate_claim_vs_ado_target``, extended to the owner field --
    ``ClaimEntry.owner_alias`` is already populated by claim extraction
    (Section 8.10.4's ``owner`` field of the action schema), so this needs
    no new extraction, only a new comparison."""
    item = _first_referenced_item(claim.entity_refs, item_lookup)
    if item is None or claim.owner_alias is None:
        return None
    claim_owner = _normalize_alias(claim.owner_alias)
    item_owner = _normalize_alias(item.assigned_to_email or item.assigned_to)
    if claim_owner is None or item_owner is None or claim_owner == item_owner:
        return None
    summary = f"ADO owner '{item_owner}' disagrees with open claim owner '{claim_owner}'"
    return (
        item.id,
        Contradiction(
            field="owner",
            source_a="ado/assigned_to",
            source_b="journal/claim",
            summary=summary,
            confidence=Confidence.MEDIUM,
            evidence_refs=tuple(sorted({*claim.entity_refs, claim.id})),
        ),
    )


def _evaluate_claim_vs_dependency_status(
    *,
    claim: ClaimEntry,
    dependency_lookup: dict[str, Dependency],
    item_lookup: dict[int, WorkItem],
) -> tuple[int, Contradiction] | None:
    """ADF-W2.10 P6 (Section 8.10.9): extends the claim-vs-structured-fact
    comparison to dependency status. A claim asserts a dependency's status
    via the generic ``claimed_status_family``/``claimed_status_value``
    fields (Section 8.10.9's other three deferred fact families -- risk,
    milestone status, action status -- have no comparison rule yet and are
    not evaluated here) plus a ``DEP:<id>`` entity ref naming which
    dependency. Attaches the resulting contradiction to whichever end of
    the dependency (``from_item_id``/``to_item_id``) is a known work item,
    since ``ContradictionPacket`` is keyed by work item id, not dependency
    id -- if neither end resolves to a known work item, there is nowhere
    to attach the packet and the claim is skipped, not raised."""
    if claim.claimed_status_family != "dependency" or claim.claimed_status_value is None:
        return None
    dependency_id = _first_referenced_dependency_id(claim.entity_refs)
    if dependency_id is None:
        return None
    dependency = dependency_lookup.get(dependency_id.upper())
    if dependency is None:
        return None
    work_item_id = next(
        (candidate for candidate in (dependency.from_item_id, dependency.to_item_id) if candidate in item_lookup),
        None,
    )
    if work_item_id is None:
        return None
    claimed_value = claim.claimed_status_value
    if claimed_value not in _RECOGNIZED_DEPENDENCY_STATUS_VALUES or claimed_value == dependency.status.value:
        return None
    summary = (
        f"Dependency {dependency.id} status is '{dependency.status.value}' per the dependency graph "
        f"but an open claim states '{claimed_value}'"
    )
    return (
        work_item_id,
        Contradiction(
            field="dependency_status",
            source_a="dependency_graph/status",
            source_b="journal/claim",
            summary=summary,
            confidence=Confidence.MEDIUM,
            evidence_refs=tuple(sorted({*claim.entity_refs, claim.id})),
        ),
    )


def _first_referenced_dependency_id(entity_refs: tuple[str, ...]) -> str | None:
    for ref in entity_refs:
        if ref.upper().startswith("DEP:"):
            dependency_id = ref.split(":", 1)[1].strip()
            if dependency_id:
                return dependency_id
    return None


def _first_referenced_prefixed_id(entity_refs: tuple[str, ...], prefix: str) -> str | None:
    """Generic ``<PREFIX>:<id>`` ref finder shared by the risk/milestone/
    action evaluators. ``prefix`` must include the trailing colon and be
    upper-case (e.g. ``"RISK:"``), matching how `_parse_entity_refs`
    normalizes refs."""
    for ref in entity_refs:
        if ref.upper().startswith(prefix):
            record_id = ref.split(":", 1)[1].strip()
            if record_id:
                return record_id
    return None


def _evaluate_claim_vs_risk_status(
    *,
    claim: ClaimEntry,
    risk_lookup: dict[str, RiskEntry],
    item_lookup: dict[int, WorkItem],
) -> tuple[int, Contradiction] | None:
    """ADF-W2.10 P7 (Section 8.10.9): extends the claim-vs-structured-fact
    comparison to risk status. A claim asserts a risk's status via the
    generic ``claimed_status_family``/``claimed_status_value`` fields plus a
    ``RISK:<id>`` entity ref naming which risk. Compares status only;
    probability/impact are separate axes. Attaches the resulting
    contradiction to the first of the risk's ``linked_work_item_ids`` that
    is a known work item (``ContradictionPacket`` is keyed by work item id,
    not risk id) -- if none resolves, the claim is skipped, not raised."""
    if claim.claimed_status_family != "risk" or claim.claimed_status_value is None:
        return None
    risk_id = _first_referenced_prefixed_id(claim.entity_refs, "RISK:")
    if risk_id is None:
        return None
    risk = risk_lookup.get(risk_id.upper())
    if risk is None:
        return None
    work_item_id = next((candidate for candidate in risk.linked_work_item_ids if candidate in item_lookup), None)
    if work_item_id is None:
        return None
    claimed_value = claim.claimed_status_value
    if claimed_value not in _RECOGNIZED_RISK_STATUS_VALUES or claimed_value == risk.status.value:
        return None
    summary = (
        f"Risk {risk.id} status is '{risk.status.value}' per the risk register "
        f"but an open claim states '{claimed_value}'"
    )
    return (
        work_item_id,
        Contradiction(
            field="risk_status",
            source_a="risk_register/status",
            source_b="journal/claim",
            summary=summary,
            confidence=Confidence.MEDIUM,
            evidence_refs=tuple(sorted({*claim.entity_refs, claim.id})),
        ),
    )


def _evaluate_claim_vs_milestone_status(
    *,
    claim: ClaimEntry,
    milestone_lookup: dict[str, Milestone],
    item_lookup: dict[int, WorkItem],
) -> tuple[int, Contradiction] | None:
    """ADF-W2.10 P7 (Section 8.10.9): the milestone-status analog of
    `_evaluate_claim_vs_risk_status`. A claim asserts a milestone's status
    via ``claimed_status_family``/``claimed_status_value`` plus a
    ``MS:<id>`` entity ref. Attaches to the first known linked work item."""
    if claim.claimed_status_family != "milestone" or claim.claimed_status_value is None:
        return None
    milestone_id = _first_referenced_prefixed_id(claim.entity_refs, "MS:")
    if milestone_id is None:
        return None
    milestone = milestone_lookup.get(milestone_id.upper())
    if milestone is None:
        return None
    work_item_id = next((candidate for candidate in milestone.linked_work_item_ids if candidate in item_lookup), None)
    if work_item_id is None:
        return None
    claimed_value = claim.claimed_status_value
    if claimed_value not in _RECOGNIZED_MILESTONE_STATUS_VALUES or claimed_value == milestone.status.value:
        return None
    summary = (
        f"Milestone {milestone.id} status is '{milestone.status.value}' per the milestone register "
        f"but an open claim states '{claimed_value}'"
    )
    return (
        work_item_id,
        Contradiction(
            field="milestone_status",
            source_a="milestones/status",
            source_b="journal/claim",
            summary=summary,
            confidence=Confidence.MEDIUM,
            evidence_refs=tuple(sorted({*claim.entity_refs, claim.id})),
        ),
    )


def _evaluate_claim_vs_action_status(
    *,
    claim: ClaimEntry,
    action_lookup: dict[str, ActionItem],
    item_lookup: dict[int, WorkItem],
) -> tuple[int, Contradiction] | None:
    """ADF-W2.10 P7 (Section 8.10.9): the action-status analog of
    `_evaluate_claim_vs_risk_status`. Note this is ``ActionItem.status``
    (the proposed/open/in_progress/done/cancelled progress axis, Section
    8.10.9's "action status"), NOT ``MeetingAction.status`` (the
    staged/approved/rejected review-workflow status, a different lifecycle).
    A claim asserts an action's status via ``claimed_status_family``/
    ``claimed_status_value`` plus an ``ACTION:<id>`` entity ref. Attaches to
    the first known linked work item."""
    if claim.claimed_status_family != "action" or claim.claimed_status_value is None:
        return None
    action_id = _first_referenced_prefixed_id(claim.entity_refs, "ACTION:")
    if action_id is None:
        return None
    action = action_lookup.get(action_id.upper())
    if action is None:
        return None
    work_item_id = next((candidate for candidate in action.linked_work_item_ids if candidate in item_lookup), None)
    if work_item_id is None:
        return None
    claimed_value = claim.claimed_status_value
    if claimed_value not in _RECOGNIZED_ACTION_STATUS_VALUES or claimed_value == action.status.value:
        return None
    summary = (
        f"Action {action.id} status is '{action.status.value}' per the action journal "
        f"but an open claim states '{claimed_value}'"
    )
    return (
        work_item_id,
        Contradiction(
            field="action_status",
            source_a="actions/status",
            source_b="journal/claim",
            summary=summary,
            confidence=Confidence.MEDIUM,
            evidence_refs=tuple(sorted({*claim.entity_refs, claim.id})),
        ),
    )


def _evaluate_signal_vs_ado_target(
    *,
    signal: Signal,
    item_lookup: dict[int, WorkItem],
    reference_date: date,
) -> tuple[int, Contradiction] | None:
    data_source = _signal_source_type(signal.source)
    if data_source is None:
        return None
    item = _first_referenced_item(signal.entity_refs, item_lookup)
    if item is None or item.target_date is None:
        return None
    signal_date = _extract_signal_date(signal, reference_date=reference_date)
    if signal_date is None:
        return None
    days_delta = abs((signal_date - item.target_date).days)
    if days_delta < 7:
        return None
    confidence = Confidence.HIGH if days_delta >= 14 else Confidence.MEDIUM
    return (
        item.id,
        Contradiction(
            field="target_date",
            source_a="ado/target_date",
            source_b=f"{data_source.value}/signal",
            summary=f"{signal.source} implies {signal_date.isoformat()} while ADO target date is {item.target_date.isoformat()}",
            confidence=confidence,
            evidence_refs=tuple(sorted({*signal.entity_refs, signal.id})),
        ),
    )


def _recommend_resolution(
    *,
    item: WorkItem,
    contradiction: Contradiction,
    workstreams: tuple[Workstream, ...],
    calibration_modifier: ForecastCalibrationModifier | None,
) -> ResolvedContradiction | None:
    if calibration_modifier is None:
        return None
    workstream_id = _resolve_workstream_id(item.area_path, workstreams)
    workstream_modifier = 0.0 if workstream_id is None else calibration_modifier.workstream_modifiers.get(workstream_id, 0.0)
    owner_alias = _normalize_alias(item.assigned_to_email or item.assigned_to)
    dri_modifier = 0.0 if owner_alias is None else calibration_modifier.dri_modifiers.get(owner_alias, 0.0)
    applied_modifier = max(workstream_modifier, dri_modifier)
    if applied_modifier < 0.15:
        return None

    # ADF-W2.10: `winning_source` must name the system that's actually being
    # recommended (`contradiction.source_a`), not be assumed. Prior to this
    # fix, any `source_b == "journal/claim"` contradiction was unconditionally
    # labeled "prefer ado" -- correct for target_date/owner (source_a really
    # is "ado/..."), but silently wrong for dependency_status, whose
    # source_a is "dependency_graph/status" (Vertex's own register, not ADO).
    # `DataSourceType` has no member for "the dependency graph" / "the risk
    # register" / "the milestone list" / "the actions journal" -- those are
    # Vertex-internal registers, not one of the four external-origin types
    # this enum models -- so rather than mislabel them as ADO (or invent new
    # enum members under time pressure), no recommendation is produced for
    # those families. This also means risk/milestone/action (ADF-W2.10 P7)
    # correctly get no auto-resolution recommendation today, matching this
    # item's honest original scoping, rather than inheriting the same
    # mislabeling dependency_status had.
    winning_source = None
    if contradiction.source_b == "journal/claim" and contradiction.source_a.startswith("ado/"):
        winning_source = DataSourceType.ADO
    elif contradiction.source_b.startswith("workiq/"):
        winning_source = DataSourceType.WORKIQ
    elif contradiction.source_b.startswith("kusto/"):
        winning_source = DataSourceType.KUSTO
    if winning_source is None:
        return None

    rationale = f"Calibration applies +{applied_modifier:.2f} slip bias for current owner/workstream, so prefer {winning_source.value}."
    return ResolvedContradiction(
        winning_source=winning_source,
        confidence=Confidence.HIGH if applied_modifier >= 0.18 else Confidence.MEDIUM,
        rationale=rationale,
        evidence_refs=contradiction.evidence_refs,
    )


def _first_referenced_item(entity_refs: tuple[str, ...], item_lookup: dict[int, WorkItem]) -> WorkItem | None:
    for ref in entity_refs:
        if not ref.upper().startswith("WI:"):
            continue
        try:
            item_id = int(ref.split(":", 1)[1])
        except ValueError:
            continue
        item = item_lookup.get(item_id)
        if item is not None:
            return item
    return None


def _signal_source_type(source: str) -> DataSourceType | None:
    normalized = source.strip().lower()
    if normalized.startswith("workiq/"):
        return DataSourceType.WORKIQ
    if normalized.startswith("kusto/"):
        return DataSourceType.KUSTO
    return None


def _extract_signal_date(signal: Signal, *, reference_date: date) -> date | None:
    metadata = signal.metadata or {}
    for key in ("target_date", "due_date", "eta", "reported_target_date"):
        raw_value = metadata.get(key)
        if isinstance(raw_value, str):
            parsed = _extract_date(raw_value, reference_date=reference_date)
            if parsed is not None:
                return parsed
    return _extract_date(signal.text, reference_date=reference_date)


def _extract_date(text: str, *, reference_date: date) -> date | None:
    iso_match = _ISO_DATE_PATTERN.search(text)
    if iso_match is not None:
        year, month, day = (int(part) for part in iso_match.groups())
        return date(year, month, day)
    month_match = _MONTH_DATE_PATTERN.search(text)
    if month_match is None:
        return None
    month_name, day_text, year_text = month_match.groups()
    year = int(year_text) if year_text is not None else reference_date.year
    month = _MONTHS[month_name.lower()]
    return date(year, month, int(day_text))


def _normalize_alias(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if "@" in normalized:
        normalized = normalized.split("@", 1)[0]
    return re.sub(r"[^a-z0-9._-]", "", normalized) or None


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _confidence_rank(value: Confidence) -> int:
    ranks = {
        Confidence.NONE: 0,
        Confidence.LOW: 1,
        Confidence.MEDIUM: 2,
        Confidence.HIGH: 3,
    }
    return ranks[value]