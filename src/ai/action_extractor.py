from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.client import AIClientError
from src.ai.deployment_fallback import FallbackStructuredClient, LEGACY_DEPLOYMENT_ALIAS_NOTICE, resolve_ai_deployments_for_feature
from src.ai.llm_trace import AITraceContext
from src.ai.provider import DisabledStructuredProvider, LLMProvider
from src.ai.tiered_router import TierResult, route_through_tiers
from src.core.action_extractor_basic import extract_actions_from_signals
from src.core.action_tracker import build_action_id
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, Program, Signal
from src.core.policy_loader import load_ai_feature_policy


PROMPT_VERSION = "action_extractor.v1"
from src.ai.prompt_registry import load_prompt
_FEATURE = "action_extractor"


class ActionExtractorError(Exception):
    """Raised when AI-backed action extraction cannot complete."""


@dataclass(frozen=True, slots=True)
class ExtractedActionCandidate:
    text: str
    owner_alias: str
    due_date: date | None
    linked_work_item_ids: tuple[int, ...]


class ActionExtractor:
    """Extracts proposed action items from transcript-like signals."""

    def __init__(self, *, client: LLMProvider) -> None:
        self._client = client

    @classmethod
    def from_program(
        cls,
        program: Program,
        *,
        trace_context: AITraceContext | None = None,
    ) -> "ActionExtractor":
        if get_ai_mode() == AIMode.DISABLED:
            return cls(client=DisabledStructuredProvider(
                feature_name="ActionExtractor",
                empty_structured_payload={"actions": []},
            ))
        temperature = load_ai_feature_policy(_FEATURE).temperature
        budget_usd = 0.25
        if program.ai is not None:
            temperature = program.ai.temperature if program.ai.temperature is not None else temperature
            budget_usd = program.ai.budget_usd_per_run
        deployments = resolve_ai_deployments_for_feature(
            feature_name=_FEATURE,
            primary_candidates=(
                program.ai.blurb_deployment if program.ai is not None else None,
                program.ai.exec_summary_deployment if program.ai is not None else None,
            ),
            backup_candidates=(
                program.ai.blurb_backup_deployment if program.ai is not None else None,
                program.ai.exec_summary_backup_deployment if program.ai is not None else None,
            ),
            primary_fallback_envs=("VERTEX_AI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"),
            backup_fallback_envs=("VERTEX_AI_BACKUP_DEPLOYMENT",),
        )
        if not deployments:
            raise ActionExtractorError(
                "VERTEX_AI_DEPLOYMENT or AZURE_OPENAI_DEPLOYMENT not set. "
                f"{LEGACY_DEPLOYMENT_ALIAS_NOTICE} Configure Azure OpenAI to extract AI actions."
            )
        client = FallbackStructuredClient(
            deployments=deployments,
            temperature=temperature,
            budget_usd=budget_usd,
            requests_per_minute=program.ai.requests_per_minute if program.ai is not None else None,
            trace_context=trace_context,
        )
        return cls(client=client)

    def extract_actions(self, *, program_id: str, signals: tuple[Signal, ...]) -> tuple[ActionItem, ...]:
        transcript_signals = tuple(signal for signal in signals if _supports_ai_extraction(signal))
        if not transcript_signals:
            return ()
        deterministic_actions, deterministic_confidence = _extract_deterministic_actions(
            program_id=program_id,
            signals=transcript_signals,
        )

        def _frontier_extract() -> tuple[ActionItem, ...]:
            system_prompt = _load_prompt()
            actions: list[ActionItem] = []
            seen_ids: set[str] = set()
            for signal in transcript_signals:
                user_prompt = _build_user_prompt(signal=signal)
                try:
                    candidates = self._client.structured(
                        system_prompt,
                        user_prompt,
                        parser=lambda payload: _parse_extracted_actions(
                            payload=payload,
                            signal=signal,
                        ),
                        max_tokens=load_ai_feature_policy(_FEATURE).max_tokens,
                        prompt_version=PROMPT_VERSION,
                    )
                except AIClientError as error:
                    raise ActionExtractorError(f"AI action extraction failed: {error}") from error
                for candidate in candidates:
                    action = _to_action_item(candidate=candidate, program_id=program_id, signal=signal)
                    if action.id in seen_ids:
                        continue
                    seen_ids.add(action.id)
                    actions.append(action)
            return tuple(actions)

        outcome = route_through_tiers(
            _FEATURE,
            deterministic_fn=lambda: (
                TierResult(value=deterministic_actions, confidence=deterministic_confidence)
                if deterministic_actions is not None
                else None
            ),
            frontier_fn=_frontier_extract,
            policy=load_ai_feature_policy(_FEATURE),
        )
        return outcome.value or ()


def _extract_deterministic_actions(
    *,
    program_id: str,
    signals: tuple[Signal, ...],
) -> tuple[tuple[ActionItem, ...] | None, float]:
    explicit_actions = _extract_explicit_deterministic_actions(
        program_id=program_id,
        signals=signals,
    )
    if explicit_actions is not None:
        return explicit_actions, 1.0
    heuristic_actions = extract_actions_from_signals(signals, program_id)
    if heuristic_actions:
        return tuple(_normalize_heuristic_action_item(action) for action in heuristic_actions), 0.95
    return None, 0.0


def _extract_explicit_deterministic_actions(
    *,
    program_id: str,
    signals: tuple[Signal, ...],
) -> tuple[ActionItem, ...] | None:
    actions: list[ActionItem] = []
    for signal in signals:
        for raw_line in signal.text.splitlines():
            line = raw_line.strip()
            if not line or not _starts_with_action_marker(line):
                continue
            candidate = _parse_deterministic_action_line(line=line, signal=signal)
            if candidate is None:
                return None
            actions.append(_to_action_item(candidate=candidate, program_id=program_id, signal=signal))
    if not actions:
        return None
    return tuple(actions)


def _normalize_heuristic_action_item(action: ActionItem) -> ActionItem:
    return ActionItem(
        id=action.id,
        program_id=action.program_id,
        text=action.text,
        owner_alias=action.owner_alias,
        due_date=action.due_date,
        status=action.status,
        source_signal_id=action.source_signal_id,
        source_type=ActionSourceType.MEETING_TRANSCRIPT,
        linked_work_item_ids=action.linked_work_item_ids,
        linked_claim_id=action.linked_claim_id,
        linked_risk_id=action.linked_risk_id,
        workstream_id=action.workstream_id,
        created_at=action.created_at,
        resolved_at=action.resolved_at,
        resolution_note=action.resolution_note,
    )


def _starts_with_action_marker(line: str) -> bool:
    return line[:7].lower() == "action:"


def _parse_deterministic_action_line(*, line: str, signal: Signal) -> ExtractedActionCandidate | None:
    body = line[len("Action:") :].strip()
    if not body:
        return None
    segments = [segment.strip() for segment in body.split("|")]
    if not segments or not segments[0]:
        return None
    payload: dict[str, str] = {"text": segments[0]}
    for segment in segments[1:]:
        if "=" not in segment:
            return None
        key, value = segment.split("=", 1)
        normalized_key = key.strip().lower()
        if normalized_key not in {"owner", "due", "refs"}:
            return None
        payload[normalized_key] = value.strip()
    try:
        return ExtractedActionCandidate(
            text=_parse_action_text(payload["text"]),
            owner_alias=_parse_optional_owner_alias(payload.get("owner")) or _owner_alias_from_signal(signal) or "unknown",
            due_date=_parse_optional_date(payload.get("due")),
            linked_work_item_ids=_parse_ref_ids(payload.get("refs")) or _work_item_ids_from_signal(signal),
        )
    except ActionExtractorError:
        return None


def _parse_ref_ids(value: object) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, str):
        return ()
    work_item_ids: list[int] = []
    for part in value.split(","):
        normalized = part.strip().upper()
        if not normalized:
            continue
        if not normalized.startswith("WI:"):
            raise ActionExtractorError("Deterministic action refs must use WI:<id> syntax.")
        try:
            work_item_id = int(normalized.split(":", 1)[1])
        except ValueError as error:
            raise ActionExtractorError("Deterministic action refs must use WI:<id> syntax.") from error
        if work_item_id not in work_item_ids:
            work_item_ids.append(work_item_id)
    return tuple(work_item_ids)


def _supports_ai_extraction(signal: Signal) -> bool:
    normalized_source = signal.source.strip().lower()
    return "teams" in normalized_source or "transcript" in normalized_source or "meeting" in normalized_source


def _load_prompt() -> str:
    return load_prompt(PROMPT_VERSION, error_factory=ActionExtractorError)


def _build_user_prompt(*, signal: Signal) -> str:
    entity_refs = ", ".join(signal.entity_refs) if signal.entity_refs else "(none)"
    sender_alias = None
    if signal.metadata is not None:
        metadata_sender = signal.metadata.get("sender_alias")
        if isinstance(metadata_sender, str) and metadata_sender.strip():
            sender_alias = metadata_sender.strip()

    lines = [
        f"Signal id: {signal.id}",
        f"Signal source: {signal.source}",
        f"Workstream id: {signal.workstream_id or '(none)'}",
        f"Entity refs: {entity_refs}",
        f"Sender alias: {sender_alias or '(unknown)'}",
        "",
        "Signal text:",
        signal.text.strip(),
    ]
    return "\n".join(lines)


def _parse_extracted_actions(
    *,
    payload: dict[str, object],
    signal: Signal,
) -> tuple[ExtractedActionCandidate, ...]:
    if not isinstance(payload, dict):
        raise ActionExtractorError("AI action payload must be an object.")
    raw_actions = payload.get("actions")
    if not isinstance(raw_actions, list):
        raise ActionExtractorError("AI action payload must include an actions list.")

    fallback_owner_alias = _owner_alias_from_signal(signal)
    fallback_work_item_ids = _work_item_ids_from_signal(signal)
    extracted: list[ExtractedActionCandidate] = []
    for raw_action in raw_actions:
        if not isinstance(raw_action, dict):
            raise ActionExtractorError("AI action entries must be objects.")
        text = _parse_action_text(raw_action.get("text"))
        if "owner_alias" not in raw_action:
            raise ActionExtractorError("AI action entries must include owner_alias as an alias string or null.")
        owner_alias = _parse_optional_owner_alias(raw_action.get("owner_alias")) or fallback_owner_alias or "unknown"
        if "due_date" not in raw_action:
            raise ActionExtractorError("AI action entries must include due_date as a YYYY-MM-DD string or null.")
        due_date = _parse_optional_date(raw_action.get("due_date"))
        if "linked_work_item_ids" not in raw_action:
            raise ActionExtractorError("AI action entries must include linked_work_item_ids as a list of integers.")
        linked_work_item_ids = _parse_linked_work_item_ids(
            raw_action.get("linked_work_item_ids"),
            allowed_work_item_ids=fallback_work_item_ids,
        ) or fallback_work_item_ids
        extracted.append(
            ExtractedActionCandidate(
                text=text,
                owner_alias=owner_alias,
                due_date=due_date,
                linked_work_item_ids=linked_work_item_ids,
            )
        )
    return tuple(extracted)


def _to_action_item(*, candidate: ExtractedActionCandidate, program_id: str, signal: Signal) -> ActionItem:
    action_id = build_action_id(
        program_id,
        text=candidate.text,
        owner_alias=candidate.owner_alias,
        due_date=candidate.due_date,
        source_signal_id=signal.id,
        workstream_id=signal.workstream_id,
        linked_work_item_ids=candidate.linked_work_item_ids,
    )
    return ActionItem(
        id=action_id,
        program_id=program_id,
        text=candidate.text,
        owner_alias=candidate.owner_alias,
        due_date=candidate.due_date,
        status=ActionStatus.PROPOSED,
        source_signal_id=signal.id,
        source_type=ActionSourceType.MEETING_TRANSCRIPT,
        linked_work_item_ids=candidate.linked_work_item_ids,
        linked_claim_id=None,
        linked_risk_id=None,
        workstream_id=signal.workstream_id,
        created_at=signal.timestamp,
        resolved_at=None,
        resolution_note=None,
    )


def _owner_alias_from_signal(signal: Signal) -> str | None:
    if signal.metadata is None:
        return None
    sender_alias = signal.metadata.get("sender_alias")
    return _normalize_alias(sender_alias)


def _work_item_ids_from_signal(signal: Signal) -> tuple[int, ...]:
    work_item_ids: list[int] = []
    for ref in signal.entity_refs:
        if not ref.upper().startswith("WI:"):
            continue
        try:
            work_item_id = int(ref.split(":", 1)[1])
        except ValueError:
            continue
        if work_item_id not in work_item_ids:
            work_item_ids.append(work_item_id)
    return tuple(work_item_ids)


def _normalize_alias(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    alias = value.strip().lower()
    if not alias:
        return None
    if "@" in alias:
        alias = alias.split("@", 1)[0]
    normalized = "".join(character for character in alias if character.isalnum() or character in {".", "_", "-"})
    return normalized or None


def _parse_action_text(value: object) -> str:
    if not isinstance(value, str):
        raise ActionExtractorError("AI action entries must include text as a string.")
    normalized = " ".join(value.split()).strip(" -\t")
    if not normalized:
        raise ActionExtractorError("AI action entries must include non-empty text.")
    try:
        processed = process_generated_text(normalized)
    except AIPipelineError as error:
        raise ActionExtractorError(f"AI action text rejected by safety pipeline: {error}") from error
    if not processed.text:
        raise ActionExtractorError("AI action entries must include non-empty text.")
    return processed.text


def _parse_optional_owner_alias(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ActionExtractorError("AI action owner_alias must be a non-empty alias string when provided.")
    normalized = _normalize_alias(value)
    if not normalized:
        raise ActionExtractorError("AI action owner_alias must be a non-empty alias string when provided.")
    return normalized


def _parse_optional_date(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ActionExtractorError("AI action due_date must be a YYYY-MM-DD string when provided.")
    normalized = value.strip()
    if not normalized:
        raise ActionExtractorError("AI action due_date must be a YYYY-MM-DD string when provided.")
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        raise ActionExtractorError("AI action due_date must be a YYYY-MM-DD string when provided.")


def _parse_linked_work_item_ids(
    value: object,
    *,
    allowed_work_item_ids: tuple[int, ...],
) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ActionExtractorError("AI action linked_work_item_ids must be a list of integers when provided.")
    allowed_work_item_id_set = set(allowed_work_item_ids)
    work_item_ids: list[int] = []
    for raw_id in value:
        if isinstance(raw_id, bool):
            raise ActionExtractorError("AI action linked_work_item_ids must contain integers only.")
        try:
            work_item_id = int(raw_id)
        except (TypeError, ValueError):
            raise ActionExtractorError("AI action linked_work_item_ids must contain integers only.")
        if work_item_id not in allowed_work_item_id_set:
            raise ActionExtractorError(
                f"AI action linked_work_item_ids contains work item {work_item_id} outside the allowed signal refs."
            )
        if work_item_id not in work_item_ids:
            work_item_ids.append(work_item_id)
    return tuple(work_item_ids)
