from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.provider import LLMProvider
from src.ai.tiered_router import TierResult, route_through_tiers
from src.core.ban_list_validator import find_ban_list_violations
from src.core.config_loader import EditorialRules, NarrativeProgramContext
from src.core.models import Confidence, DeltaKind, EditionType, ItemDelta, RiskLevel, WorkItem
from src.core.verbosity_enforcer import enforce_verbosity
from src.core.voice_validator import build_writing_contract_prompt_lines
from src.core.policy_loader import load_ai_feature_policy


PROMPT_VERSION = "exec_summary_drafter.v1"
from src.ai.prompt_registry import load_prompt
_FEATURE = "exec_summary_drafter"
_DEFAULT_EXEC_SUMMARY_MAX_WORDS = 150


class ExecSummaryDraftError(Exception):
    """Raised when a generated exec summary violates the editorial contract."""


@dataclass(frozen=True, slots=True)
class ExecSummaryDraft:
    text: str
    prompt_version: str
    cited_work_item_ids: tuple[int, ...]
    ai_confidence: Confidence


@dataclass(frozen=True, slots=True)
class _RankedChange:
    work_item_id: int
    priority: int
    label: str
    summary: str
    confidence: Confidence


def draft_exec_summary(
    *,
    client: LLMProvider,
    items: tuple[WorkItem, ...] | list[WorkItem],
    deltas: Any,
    editorial_rules: EditorialRules,
    edition_type: EditionType | None = None,
    program_context: NarrativeProgramContext | None = None,
    supplemental_context: tuple[str, ...] = (),
) -> ExecSummaryDraft | None:
    ranked_changes = _rank_changes(tuple(items), deltas)
    if not ranked_changes:
        return None

    items_by_id = {item.id: item for item in items}
    missing_item_ids = tuple(change.work_item_id for change in ranked_changes if change.work_item_id not in items_by_id)
    if missing_item_ids:
        raise ExecSummaryDraftError(
            "Exec summary ranked changes missing work item context: "
            + ", ".join(str(item_id) for item_id in dict.fromkeys(missing_item_ids))
        )
    allowed_items = tuple(items_by_id[change.work_item_id] for change in ranked_changes)
    prompt_template = _load_prompt_template()
    system_prompt = prompt_template
    user_prompt = _build_user_prompt(
        ranked_changes=ranked_changes,
        editorial_rules=editorial_rules,
        program_context=program_context,
        supplemental_context=supplemental_context,
        max_words=editorial_rules.verbosity.exec_summary_max_words_for(
            edition_type,
            default=_DEFAULT_EXEC_SUMMARY_MAX_WORDS,
        ) or _DEFAULT_EXEC_SUMMARY_MAX_WORDS,
    )
    raw_text = route_through_tiers(
        _FEATURE,
        deterministic_fn=lambda: None,
        frontier_fn=lambda: client.structured(
            system_prompt,
            user_prompt,
            parser=_parse_generated_exec_summary_text,
            max_tokens=load_ai_feature_policy(_FEATURE).max_tokens,
            prompt_version=PROMPT_VERSION,
        ),
        policy=load_ai_feature_policy(_FEATURE),
    ).value
    if not raw_text:
        return None

    try:
        grounded = process_generated_text(raw_text, allowed_items=allowed_items)
    except AIPipelineError as error:
        raise ExecSummaryDraftError(str(error)) from error

    if not grounded.text:
        return None

    _validate_editorial_rules(grounded.text, editorial_rules, edition_type=edition_type)

    return ExecSummaryDraft(
        text=grounded.text,
        prompt_version=PROMPT_VERSION,
        cited_work_item_ids=grounded.cited_work_item_ids,
        ai_confidence=_derive_ai_confidence(ranked_changes, grounded.cited_work_item_ids),
    )


def _load_prompt_template() -> str:
    return load_prompt(PROMPT_VERSION, error_factory=ExecSummaryDraftError)


def _build_user_prompt(
    *,
    ranked_changes: tuple[_RankedChange, ...],
    editorial_rules: EditorialRules,
    program_context: NarrativeProgramContext | None,
    supplemental_context: tuple[str, ...],
    max_words: int,
) -> str:
    lines = ["Top ranked candidate changes:"]
    if program_context is not None:
        lines.insert(0, f"Program: {program_context.program_name}")
        objective = getattr(program_context, "objective", None)
        if objective:
            lines.insert(1, f"Objective: {objective}")
        lines[2:2] = list(
            build_writing_contract_prompt_lines(
                program_context,
                editorial_rules=editorial_rules,
            )
        )
    for change in ranked_changes:
        lines.append(f"- priority={change.priority} | {change.label} | {change.summary}")
    if supplemental_context:
        lines.append("Supplemental context:")
        lines.extend(f"- {entry}" for entry in supplemental_context if entry.strip())
    lines.append(f"Write one prose paragraph of at most {max_words} words. Cite every claim with [#WI].")
    return "\n".join(lines)


def _rank_changes(items: tuple[WorkItem, ...], deltas: Any) -> tuple[_RankedChange, ...]:
    items_by_id = {item.id: item for item in items}
    owner_changes = tuple(getattr(deltas, "owner_changes", ()))

    candidates: list[_RankedChange] = []
    candidates.extend(
        _RankedChange(
            work_item_id=delta.work_item_id,
            priority=0,
            label="RISK_UP",
            summary=_describe_risk_up(delta, items_by_id.get(delta.work_item_id)),
            confidence=delta.evidence.confidence,
        )
        for delta in deltas.risk_changes
        if delta.kind == DeltaKind.RISK_UP
    )
    candidates.extend(
        _RankedChange(
            work_item_id=delta.work_item_id,
            priority=1,
            label="NEW_HIGH",
            summary=_describe_new_high(delta, items_by_id.get(delta.work_item_id)),
            confidence=delta.evidence.confidence,
        )
        for delta in deltas.new_items
        if delta.new_risk == RiskLevel.HIGH
    )
    candidates.extend(
        _RankedChange(
            work_item_id=delta.work_item_id,
            priority=2,
            label="CLOSED_HIGH",
            summary=_describe_closed_high(delta, items_by_id.get(delta.work_item_id)),
            confidence=delta.evidence.confidence,
        )
        for delta in deltas.closed_items
        if RiskLevel.HIGH in {delta.old_risk, delta.new_risk}
    )
    candidates.extend(
        _RankedChange(
            work_item_id=delta.work_item_id,
            priority=3,
            label="ETA_SLIP",
            summary=_describe_eta_change(delta, items_by_id.get(delta.work_item_id)),
            confidence=delta.evidence.confidence,
        )
        for delta in deltas.eta_changes
    )
    candidates.extend(
        _RankedChange(
            work_item_id=delta.work_item_id,
            priority=4,
            label="OWNER_CHANGE",
            summary=_describe_owner_change(delta, items_by_id.get(delta.work_item_id)),
            confidence=delta.evidence.confidence,
        )
        for delta in owner_changes
        if delta.kind == DeltaKind.OWNER_CHANGED
    )

    deduped: dict[int, _RankedChange] = {}
    for candidate in sorted(candidates, key=lambda change: (change.priority, change.work_item_id)):
        deduped.setdefault(candidate.work_item_id, candidate)
    return tuple(list(deduped.values())[:3])


def _derive_ai_confidence(ranked_changes: tuple[_RankedChange, ...], cited_work_item_ids: tuple[int, ...]) -> Confidence:
    confidence_by_item = {change.work_item_id: change.confidence for change in ranked_changes}
    missing_item_ids = tuple(item_id for item_id in cited_work_item_ids if item_id not in confidence_by_item)
    if missing_item_ids:
        raise ExecSummaryDraftError(
            "Exec summary cited work items missing ranked-change confidence: "
            + ", ".join(str(item_id) for item_id in missing_item_ids)
        )
    confidences = [confidence_by_item[item_id] for item_id in cited_work_item_ids]
    if not confidences:
        return Confidence.NONE
    return min(confidences, key=_confidence_rank)


def _confidence_rank(confidence: Confidence) -> int:
    return {
        Confidence.NONE: 0,
        Confidence.LOW: 1,
        Confidence.MEDIUM: 2,
        Confidence.HIGH: 3,
    }[confidence]


def _describe_risk_up(delta: ItemDelta, item: WorkItem | None) -> str:
    title = item.title if item is not None else f"Work item {delta.work_item_id}"
    return f"RISK_UP #{delta.work_item_id} {title} from {delta.old_risk.value if delta.old_risk is not None else 'unknown'} to {delta.new_risk.value if delta.new_risk is not None else 'unknown'}."


def _describe_new_high(delta: ItemDelta, item: WorkItem | None) -> str:
    title = item.title if item is not None else f"Work item {delta.work_item_id}"
    return f"NEW_HIGH #{delta.work_item_id} {title} entered scope at high risk."


def _describe_closed_high(delta: ItemDelta, item: WorkItem | None) -> str:
    title = item.title if item is not None else f"Work item {delta.work_item_id}"
    return f"CLOSED_HIGH #{delta.work_item_id} {title} closed from high risk."


def _describe_eta_change(delta: ItemDelta, item: WorkItem | None) -> str:
    title = item.title if item is not None else f"Work item {delta.work_item_id}"
    return f"ETA_SLIP #{delta.work_item_id} {title} moved from {delta.old_eta.isoformat() if delta.old_eta is not None else 'none'} to {delta.new_eta.isoformat() if delta.new_eta is not None else 'none'}."


def _describe_owner_change(delta: ItemDelta, item: WorkItem | None) -> str:
    title = item.title if item is not None else f"Work item {delta.work_item_id}"
    previous_owner, current_owner = delta.field_changes.get("assigned_to", (None, None))
    return f"OWNER_CHANGE #{delta.work_item_id} {title} moved from {previous_owner or 'unassigned'} to {current_owner or 'unassigned'}."


def _validate_editorial_rules(
    text: str,
    editorial_rules: EditorialRules,
    *,
    edition_type: EditionType | None,
) -> None:
    if len([paragraph for paragraph in text.strip().splitlines() if paragraph.strip()]) > 1:
        raise ExecSummaryDraftError("Generated exec summary must stay within a single paragraph.")

    violations = find_ban_list_violations({"exec_summary": text}, editorial_rules)
    if violations:
        phrases = ", ".join(sorted({violation.phrase for violation in violations}))
        raise ExecSummaryDraftError(f"Generated exec summary violates the ban-list: {phrases}")

    verbosity_violations = enforce_verbosity(
        workstream_blurbs={},
        exec_summary_text=text,
        scorecard_summaries={},
        subject_line=None,
        verbosity=editorial_rules.verbosity,
        edition_type=edition_type,
    )
    if verbosity_violations:
        messages = "; ".join(violation.message for violation in verbosity_violations)
        raise ExecSummaryDraftError(f"Generated exec summary violates verbosity rules: {messages}")


def _parse_generated_exec_summary_text(payload: dict[str, object]) -> str:
    if not isinstance(payload, dict):
        raise ExecSummaryDraftError("Generated exec summary payload must be an object.")
    text = payload.get("text")
    if not isinstance(text, str):
        raise ExecSummaryDraftError("Generated exec summary payload must include text as a string.")
    normalized = text.strip()
    if not normalized:
        raise ExecSummaryDraftError("Generated exec summary payload text must be non-empty.")
    return normalized
