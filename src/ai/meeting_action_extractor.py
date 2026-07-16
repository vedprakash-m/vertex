"""ADF-W3.3 (specs/arch-data-fix.md Section 8.10.4, step 2): LLM extraction
of meeting actions from residual transcript content -- the content a
deterministic marker line (``src/core/meeting_action.py``) did not already
cover.

Runs the same AI safety lifecycle ADF-W2.8/W2.9 established for this
codebase's other structured-extraction AI features: AISchemaGateway bounds
checks on request/response, the five ``AIRunState`` transitions, a concrete
``SemanticValidator``-shaped check (every extracted action must cite a
``source_span`` that appears verbatim in the residual text -- the same
"no unsupported causal claim" discipline as ``program_synthesizer.py``),
and a QG-29 terminal release decision before any caller may consume the
result. Tier dispatch itself uses ``route_through_tiers`` with no
deterministic tier (mirrors ``decision_brief_advisor.py``/
``program_synthesizer.py`` -- there is no cheaper-than-LLM way to do
residual-content extraction; the deterministic tier already ran separately
in ``src/core/meeting_action.py``).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.prompt_registry import PromptRegistryError, load_prompt
from src.ai.provider import LLMProvider
from src.ai.tiered_router import route_through_tiers
from src.core.ai_result_cache import AIResultCacheKey, canonical_input_hash, get_ai_result, put_ai_result
from src.core.ai_schema_gateway import SchemaGatewayError, validate_bounded_payload
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.meeting_action import (
    MeetingAction,
    MeetingActionExtractionResult,
    extract_deterministic_meeting_actions,
    merge_meeting_actions,
    validate_meeting_actions,
)
from src.core.models import WorkItem
from src.core.policy_loader import load_ai_feature_policy
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_release_decision,
    record_ai_run_lifecycle,
)

_OUTPUT_SCHEMA_VERSION = "1"

PROMPT_VERSION = "meeting_action_extractor.v1"
POLICY_VERSION = "meeting_action_extractor.v1"
_FEATURE = "meeting_action_extractor"


def extract_llm_meeting_actions(
    *,
    program_id: str,
    meeting_ref: str,
    residual_text: str,
    items: tuple[WorkItem, ...],
    client: LLMProvider,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[MeetingAction, ...]:
    """Returns the released LLM-extracted actions, or an empty tuple if the
    run was discarded/rejected (Section 8.9.4: never consume an
    unreleased AI output). Callers that need to know WHY nothing came back
    should inspect the ledger via ``ai_release_audit`` directly -- this
    function's contract, matching every other extraction call site in this
    codebase, is "empty means nothing usable," not an exception."""
    if not residual_text.strip():
        return ()

    ai_run_id = new_ai_run_id()

    def _lifecycle(state: AIRunState) -> None:
        record_ai_run_lifecycle(
            program_id=program_id,
            ai_run_id=ai_run_id,
            feature=_FEATURE,
            state=state,
            prompt_version=PROMPT_VERSION,
            policy_version=POLICY_VERSION,
            programs_root=programs_root,
        )

    def _discard(terminal: ReleaseTerminal, reason: str, finding_count: int = 0) -> tuple[MeetingAction, ...]:
        record_ai_release_decision(
            program_id=program_id,
            ai_run_id=ai_run_id,
            terminal=terminal,
            reason=reason,
            validator_finding_count=finding_count,
            programs_root=programs_root,
        )
        return ()

    _lifecycle(AIRunState.PLANNED)

    request_payload = {
        "residual_text": residual_text,
        "allowed_work_item_ids": [item.id for item in items],
    }
    try:
        validate_bounded_payload(request_payload)
    except SchemaGatewayError as error:
        return _discard(ReleaseTerminal.DISCARDED, f"AISchemaGateway rejected the outbound request: {error}")

    _lifecycle(AIRunState.REQUESTED)

    prompt_template = _load_prompt_template()
    policy = load_ai_feature_policy(_FEATURE)
    cache_key = AIResultCacheKey(
        program_id=program_id,
        feature=_FEATURE,
        canonical_input_hash=canonical_input_hash(residual_text),
        prompt_version=PROMPT_VERSION,
        policy_version=POLICY_VERSION,
        model_deployment=_resolve_model_deployment(client),
        context_manifest_hash=canonical_input_hash("|".join(str(item.id) for item in items)),
        output_schema_version=_OUTPUT_SCHEMA_VERSION,
    )
    try:
        raw = route_through_tiers(
            _FEATURE,
            deterministic_fn=lambda: None,
            frontier_fn=lambda: client.structured(
                prompt_template,
                _build_user_prompt(residual_text=residual_text, items=items),
                parser=lambda payload: payload,
                max_tokens=policy.max_tokens or 1200,
                prompt_version=PROMPT_VERSION,
            ),
            policy=policy,
            cache_lookup_fn=lambda: _cached_response(cache_key, programs_root=programs_root),
            cache_store_fn=lambda value: put_ai_result(cache_key, value, programs_root=programs_root),
        ).value
    except Exception as error:
        return _discard(ReleaseTerminal.DISCARDED, f"provider call failed: {error}")

    _lifecycle(AIRunState.RESPONDED)

    if not isinstance(raw, dict):
        return _discard(ReleaseTerminal.DISCARDED, "no structured response returned by the provider.")

    try:
        validate_bounded_payload(raw)
    except SchemaGatewayError as error:
        return _discard(ReleaseTerminal.REJECTED, f"AISchemaGateway rejected the response: {error}")

    _lifecycle(AIRunState.SCHEMA_VALIDATED)

    parsed = _parse_actions(raw, program_id=program_id, meeting_ref=meeting_ref)
    if parsed is None:
        return _discard(ReleaseTerminal.REJECTED, "response actions list missing or malformed.")

    findings = _validate_semantics(parsed, residual_text=residual_text, items=items)
    _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)

    if findings:
        return _discard(ReleaseTerminal.REJECTED, "; ".join(findings), finding_count=len(findings))

    record_ai_release_decision(
        program_id=program_id,
        ai_run_id=ai_run_id,
        terminal=ReleaseTerminal.RELEASED,
        reason="passed AISchemaGateway bounds and meeting-action semantic validation",
        validator_finding_count=0,
        programs_root=programs_root,
    )
    return parsed


def run_meeting_action_extraction_pipeline(
    *,
    program_id: str,
    meeting_ref: str,
    transcript_text: str,
    items: tuple[WorkItem, ...],
    client: LLMProvider,
    programs_root: Path = PROGRAMS_ROOT,
) -> MeetingActionExtractionResult:
    """Section 8.10.4's full five-step pipeline in one entry point: (1)
    deterministic markers, (2) LLM extraction on the residual content only
    (marker lines removed), (3) merge and deduplicate, (4) validate ->
    invalid/ambiguous proposals rejected (never dropped silently), (5) the
    result is "staged for review" -- every non-rejected action's
    ``status`` is ``"staged"``, meaning a human reviewer, not this
    pipeline, makes the next call."""
    deterministic = extract_deterministic_meeting_actions(
        program_id=program_id, meeting_ref=meeting_ref, transcript_text=transcript_text
    )
    residual_text = _compute_residual_text(transcript_text, deterministic)
    llm = extract_llm_meeting_actions(
        program_id=program_id,
        meeting_ref=meeting_ref,
        residual_text=residual_text,
        items=items,
        client=client,
        programs_root=programs_root,
    )
    merged = merge_meeting_actions(deterministic, llm)
    validated = validate_meeting_actions(merged.actions, transcript_text=transcript_text, items=items)
    return MeetingActionExtractionResult(actions=validated, warnings=merged.warnings)


def _compute_residual_text(transcript_text: str, deterministic_actions: tuple[MeetingAction, ...]) -> str:
    """Removes every deterministic marker line (its ``source_span`` IS the
    exact line) from the transcript, leaving only the content the LLM tier
    should scan -- Section 8.10.4 step 2's "residual content," not the
    whole transcript again."""
    matched_lines = {action.source_span.strip() for action in deterministic_actions}
    kept_lines = [line for line in transcript_text.splitlines() if line.strip() not in matched_lines]
    return "\n".join(kept_lines)


def _validate_semantics(
    actions: tuple[MeetingAction, ...], *, residual_text: str, items: tuple[WorkItem, ...]
) -> tuple[str, ...]:
    """The same grounding discipline as ``program_synthesizer.py``'s
    validator: every action's ``source_span`` must appear verbatim in the
    residual text it was extracted from, and any cited work item must be
    in the allowed set -- otherwise the whole run is rejected (fail
    closed, not per-action filtering, since a hallucination-prone response
    is treated as untrustworthy as a whole)."""
    findings: list[str] = []
    allowed_ids = {item.id for item in items}
    for index, action in enumerate(actions):
        if not action.source_span.strip():
            findings.append(f"action[{index}] has an empty source_span.")
            continue
        if action.source_span.strip() not in residual_text:
            findings.append(f"action[{index}] source_span does not appear verbatim in the residual text (possible hallucination).")
        if action.linked_work_item_id is not None and action.linked_work_item_id not in allowed_ids:
            findings.append(f"action[{index}] cites linked_work_item WI:{action.linked_work_item_id} outside the allowed set.")
    return tuple(findings)


def _build_user_prompt(*, residual_text: str, items: tuple[WorkItem, ...]) -> str:
    allowed_ids = ", ".join(str(item.id) for item in items) or "(none)"
    return (
        f"Allowed work item ids: {allowed_ids}\n\n"
        "RESIDUAL TRANSCRIPT CONTENT (content not already captured by a deterministic marker):\n"
        f"{residual_text.strip()}"
    )


def _parse_actions(
    raw: dict[str, Any], *, program_id: str, meeting_ref: str
) -> tuple[MeetingAction, ...] | None:
    raw_actions = raw.get("actions")
    if not isinstance(raw_actions, list):
        return None
    parsed: list[MeetingAction] = []
    for index, entry in enumerate(raw_actions, start=1):
        action = _parse_one_action(entry, program_id=program_id, meeting_ref=meeting_ref, index=index)
        if action is None:
            return None
        parsed.append(action)
    return tuple(parsed)


def _parse_one_action(entry: Any, *, program_id: str, meeting_ref: str, index: int) -> MeetingAction | None:
    if not isinstance(entry, dict):
        return None
    commitment_raw = str(entry.get("commitment", "")).strip()
    source_span_raw = str(entry.get("source_span", "")).strip()
    if not commitment_raw or not source_span_raw:
        return None
    try:
        commitment = process_generated_text(commitment_raw).text
    except AIPipelineError:
        return None
    if not commitment:
        return None

    owner_alias = _normalize_owner_alias(entry.get("owner"))
    raw_due_date = _parse_due(entry.get("due"))
    if raw_due_date is _INVALID:
        return None
    assert raw_due_date is None or isinstance(raw_due_date, date)
    due_date = raw_due_date
    raw_linked_work_item_id = _parse_wi(entry.get("linked_work_item"))
    if raw_linked_work_item_id is _INVALID:
        return None
    assert raw_linked_work_item_id is None or isinstance(raw_linked_work_item_id, int)
    linked_work_item_id = raw_linked_work_item_id
    blocks_raw = entry.get("blocks")
    blocks = tuple(str(ref).strip() for ref in blocks_raw if isinstance(ref, (str, int)) and str(ref).strip()) if isinstance(blocks_raw, list) else ()

    return MeetingAction(
        id=f"llm-action-{meeting_ref}-{index}",
        program_id=program_id,
        meeting_ref=meeting_ref,
        commitment=commitment,
        owner_alias=owner_alias,
        due_date=due_date,
        linked_work_item_id=linked_work_item_id,
        blocks=blocks,
        source_span=source_span_raw,
        extraction_method="llm",
    )


_INVALID = object()


def _normalize_owner_alias(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if "@" in normalized:
        normalized = normalized.split("@", 1)[0]
    normalized = "".join(character for character in normalized if character.isalnum() or character in {".", "_", "-"})
    return normalized or None


def _parse_due(value: Any) -> date | None | object:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return _INVALID
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return _INVALID


def _parse_wi(value: Any) -> int | None | object:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not value.strip():
        return _INVALID
    normalized = value.strip().upper()
    if normalized.startswith("WI:"):
        normalized = normalized[3:]
    try:
        return int(normalized)
    except ValueError:
        return _INVALID


def _resolve_model_deployment(client: LLMProvider) -> str:
    """Best-effort model/deployment identifier for the cache key (Section
    8.8.3), same degrade-to-sentinel discipline as
    ``risk_proposal_generator.py``: a wrong/missing deployment id only
    under-shares the cache (never over-shares, since program_id/feature/
    input hash are still exact-matched), so this never raises."""
    for attr in ("deployment", "primary_deployment", "model", "deployment_id"):
        value = getattr(client, attr, None)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _cached_response(key: AIResultCacheKey, *, programs_root: Path) -> dict[str, Any] | None:
    hit = get_ai_result(key, programs_root=programs_root)
    return hit.value if hit is not None else None


def _load_prompt_template() -> str:
    try:
        return load_prompt(PROMPT_VERSION)
    except PromptRegistryError:
        return _FALLBACK_PROMPT


_FALLBACK_PROMPT = (
    "You extract meeting action items from residual transcript content (content a deterministic "
    "marker parser already scanned and did not catch). For each action, respond with JSON: "
    '{"actions": [{"commitment": str, "owner": str|null, "due": "YYYY-MM-DD"|null, '
    '"linked_work_item": int|null, "blocks": [str, ...], "source_span": str}]}. '
    "source_span MUST be copied verbatim from the residual text -- never paraphrase it. "
    "Only cite linked_work_item ids from the allowed list you were given. "
    "If you cannot find a verbatim source_span for a candidate action, omit it."
)


__all__ = [
    "POLICY_VERSION",
    "PROMPT_VERSION",
    "extract_llm_meeting_actions",
    "run_meeting_action_extraction_pipeline",
]
