from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.client import AIClientError
from src.ai.provider import LLMProvider
from src.ai.tiered_router import TierResult, route_through_tiers
from src.core.ai_result_cache import AIResultCacheKey, canonical_input_hash, get_ai_result, put_ai_result
from src.core.ai_schema_gateway import SchemaGatewayError, validate_bounded_payload
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import ActionItem, ContradictionPacket, Program, RiskEntry, Signal, Workstream, WorkstreamSynthesis
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_release_decision,
    record_ai_run_lifecycle,
)
from src.core.trajectory_analyzer import DriftPattern
from src.core.policy_loader import load_ai_feature_policy


PROMPT_VERSION = "synthesizer.v1"
from src.ai.prompt_registry import load_prompt
POLICY_VERSION = "synthesizer.v1"
_FEATURE = "synthesizer"
_OUTPUT_SCHEMA_VERSION = "1"


class SynthesizerError(Exception):
    """Raised when workstream synthesis cannot complete safely."""


@dataclass(frozen=True, slots=True)
class SynthesizedProposalDraft:
    synthesis: WorkstreamSynthesis
    prompt_version: str


class WorkstreamSynthesizer:
    def __init__(self, *, client: LLMProvider) -> None:
        self._client = client

    def generate(
        self,
        *,
        program: Program,
        workstream: Workstream,
        signals: tuple[Signal, ...],
        drift_patterns: tuple[DriftPattern, ...],
        open_risks: tuple[RiskEntry, ...] = (),
        open_actions: tuple[ActionItem, ...] = (),
        contradictions: tuple[ContradictionPacket, ...] = (),
        programs_root: Path = PROGRAMS_ROOT,
    ) -> SynthesizedProposalDraft | None:
        """Runs the same AISchemaGateway/QG-29 safety lifecycle established
        by ``risk_proposal_generator.py`` (ADF-W5.1, P7): bounds-checked
        request/response payloads, the five ``AIRunState`` transitions, and
        a durable QG-29 terminal release decision recorded before any
        caller may consume the result. ``_parse_synthesis_payload``'s
        existing field-by-field checks already combine schema-shape and
        semantic/grounding validation (e.g. "evidence_refs must cite only
        approved signal ids") in a single raise-on-first-violation pass --
        unlike a feature with a clean schema/semantic split, this one
        records ``SCHEMA_VALIDATED``/``SEMANTICALLY_VALIDATED`` back to
        back around that single pass rather than forcing an artificial
        two-phase split of tightly-coupled checks. Preserves this
        feature's existing raise-on-rejection contract
        (``SynthesizerError``) rather than switching to the newer
        generators' return-``None``-on-rejection convention, matching the
        same reasoning as ``summary_generator.py``'s migration."""
        if not signals:
            return None

        ai_run_id = new_ai_run_id()
        program_id = program.id

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

        def _discard(terminal: ReleaseTerminal, reason: str, finding_count: int = 0) -> None:
            record_ai_release_decision(
                program_id=program_id,
                ai_run_id=ai_run_id,
                terminal=terminal,
                reason=reason,
                validator_finding_count=finding_count,
                programs_root=programs_root,
            )

        _lifecycle(AIRunState.PLANNED)

        valid_signal_ids = tuple(signal.id for signal in signals)
        request_payload = {
            "program_id": program.id,
            "workstream_id": workstream.id,
            "signal_ids": list(valid_signal_ids),
            "signal_texts": [signal.text for signal in signals],
            "drift_pattern_ids": [pattern.work_item_id for pattern in drift_patterns],
            "open_risk_ids": [risk.id for risk in open_risks],
            "open_action_ids": [action.id for action in open_actions],
        }
        try:
            validate_bounded_payload(request_payload)
        except SchemaGatewayError as error:
            reason = f"AISchemaGateway rejected the outbound request: {error}"
            _discard(ReleaseTerminal.DISCARDED, reason)
            raise SynthesizerError(reason) from error

        _lifecycle(AIRunState.REQUESTED)

        user_prompt = _build_user_prompt(
            program=program,
            workstream=workstream,
            signals=signals,
            drift_patterns=drift_patterns,
            open_risks=open_risks,
            open_actions=open_actions,
            contradictions=contradictions,
        )
        policy = load_ai_feature_policy(_FEATURE)
        cache_key = AIResultCacheKey(
            program_id=program_id,
            feature=_FEATURE,
            canonical_input_hash=canonical_input_hash(user_prompt),
            prompt_version=PROMPT_VERSION,
            policy_version=POLICY_VERSION,
            model_deployment=_resolve_model_deployment(self._client),
            context_manifest_hash=canonical_input_hash("|".join(valid_signal_ids)),
            output_schema_version=_OUTPUT_SCHEMA_VERSION,
        )
        try:
            client = self._client
            raw = route_through_tiers(
                _FEATURE,
                deterministic_fn=lambda: None,
                frontier_fn=lambda: client.structured(
                    _load_prompt(),
                    user_prompt,
                    parser=lambda payload: payload,
                    max_tokens=policy.max_tokens,
                    prompt_version=PROMPT_VERSION,
                ),
                policy=policy,
                cache_lookup_fn=lambda: _cached_response(cache_key, programs_root=programs_root),
                cache_store_fn=lambda value: put_ai_result(cache_key, value, programs_root=programs_root),
            ).value
        except AIClientError as error:
            reason = f"Workstream synthesis failed for {workstream.id}: {error}"
            _discard(ReleaseTerminal.DISCARDED, reason)
            raise SynthesizerError(reason) from error

        _lifecycle(AIRunState.RESPONDED)

        try:
            validate_bounded_payload(raw)
        except SchemaGatewayError as error:
            reason = f"AISchemaGateway rejected the response: {error}"
            _discard(ReleaseTerminal.REJECTED, reason)
            raise SynthesizerError(reason) from error

        try:
            synthesis = _parse_synthesis_payload(
                cast(dict[str, object], raw),
                workstream_id=workstream.id,
                valid_signal_ids=valid_signal_ids,
            )
        except SynthesizerError as error:
            _lifecycle(AIRunState.SCHEMA_VALIDATED)
            _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)
            _discard(ReleaseTerminal.REJECTED, str(error))
            raise

        _lifecycle(AIRunState.SCHEMA_VALIDATED)
        _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)
        _discard(
            ReleaseTerminal.RELEASED,
            "passed AISchemaGateway bounds and workstream-synthesis field validation",
        )
        return SynthesizedProposalDraft(synthesis=synthesis, prompt_version=PROMPT_VERSION)


def build_synthesizer_from_client(client: LLMProvider) -> WorkstreamSynthesizer:
    return WorkstreamSynthesizer(client=client)


def _load_prompt() -> str:
    return load_prompt(PROMPT_VERSION, error_factory=SynthesizerError)


def _build_user_prompt(
    *,
    program: Program,
    workstream: Workstream,
    signals: tuple[Signal, ...],
    drift_patterns: tuple[DriftPattern, ...],
    open_risks: tuple[RiskEntry, ...],
    open_actions: tuple[ActionItem, ...],
    contradictions: tuple[ContradictionPacket, ...],
) -> str:
    lines = [
        f"Program: {program.name}",
        f"Workstream id: {workstream.id}",
        f"Workstream name: {workstream.name}",
    ]
    if workstream.description:
        lines.append(f"Description: {workstream.description}")
    if workstream.why_it_matters:
        lines.append(f"Why it matters: {workstream.why_it_matters}")
    if workstream.current_blocker:
        lines.append(f"Current blocker: {workstream.current_blocker}")

    lines.append("Approved signals (cite only these ids in evidence_refs):")
    for signal in signals:
        lines.append(
            f"- {signal.id} | {signal.timestamp.isoformat()} | {signal.source} | refs={','.join(signal.entity_refs) or '-'} | {signal.text}"
        )

    if drift_patterns:
        lines.append("Drift patterns:")
        for pattern in drift_patterns:
            lines.append(
                f"- WI:{pattern.work_item_id} | {pattern.pattern} | {pattern.severity} | {pattern.detail}"
            )

    if open_risks:
        lines.append("Linked active risks:")
        for risk in open_risks:
            lines.append(
                f"- {risk.id} | {risk.impact.value}/{risk.probability.value} | owner={risk.owner_alias} | {risk.title}"
            )

    if open_actions:
        lines.append("Linked active actions:")
        for action in open_actions:
            due_label = action.due_date.isoformat() if action.due_date is not None else "-"
            lines.append(
                f"- {action.id} | {action.status.value} | due={due_label} | owner={action.owner_alias} | {action.text}"
            )

    if contradictions:
        lines.append("Source contradictions (address the most significant in overall_assessment):")
        for packet in contradictions:
            recommendation = ""
            if packet.recommended_resolution is not None:
                recommendation = (
                    f" | recommended={packet.recommended_resolution.winning_source.value}"
                    f" ({packet.recommended_resolution.confidence.value})"
                )
            for contradiction in packet.contradictions:
                lines.append(
                    f"- WI:{packet.work_item_id} | {contradiction.source_a} vs {contradiction.source_b}"
                    f" | {contradiction.field} | {contradiction.summary}{recommendation}"
                )

    lines.append("Return JSON only.")
    return "\n".join(lines)


def _parse_synthesis_payload(
    payload: dict[str, object],
    *,
    workstream_id: str,
    valid_signal_ids: tuple[str, ...],
) -> WorkstreamSynthesis:
    if not isinstance(payload, dict):
        raise SynthesizerError("Workstream synthesis returned a non-object payload.")

    overall_assessment = _sanitize_text(_require_string(payload, "overall_assessment"), field_name="overall_assessment")
    if len(overall_assessment.split()) > 200:
        raise SynthesizerError("overall_assessment must be 200 words or fewer.")

    key_findings = _sanitize_list(_require_field(payload, "key_findings"), field_name="key_findings", max_items=5)
    evidence_refs = _string_list(_require_field(payload, "evidence_refs"), field_name="evidence_refs")
    _validate_evidence_refs(evidence_refs, valid_signal_ids=valid_signal_ids)
    open_questions = _sanitize_list(_require_field(payload, "open_questions"), field_name="open_questions", max_items=5)
    recommended_actions = _sanitize_list(
        _require_field(payload, "recommended_actions"),
        field_name="recommended_actions",
        max_items=3,
    )

    if key_findings and not evidence_refs:
        raise SynthesizerError("evidence_refs must include at least one signal id when key_findings are present.")

    proposed_risk = _require_supported_enum_string(
        payload,
        field_name="proposed_risk",
        allowed_values=tuple(level.value for level in RiskLevel),
    )
    confidence = _require_supported_enum_string(
        payload,
        field_name="confidence",
        allowed_values=tuple(level.value for level in Confidence),
    )

    return WorkstreamSynthesis(
        workstream_id=workstream_id,
        overall_assessment=overall_assessment,
        proposed_risk=RiskLevel.from_string(proposed_risk),
        confidence=Confidence.from_string(confidence),
        key_findings=key_findings,
        evidence_refs=evidence_refs,
        open_questions=open_questions,
        recommended_actions=recommended_actions,
    )


def _sanitize_text(value: str, *, field_name: str) -> str:
    try:
        processed = process_generated_text(value)
    except AIPipelineError as error:
        raise SynthesizerError(f"{field_name} failed the AI safety pipeline: {error}") from error
    normalized = processed.text.strip()
    if not normalized:
        raise SynthesizerError(f"{field_name} must not be empty.")
    return normalized


def _sanitize_list(value: object, *, field_name: str, max_items: int) -> tuple[str, ...]:
    items = _string_list(value, field_name=field_name)
    if len(items) > max_items:
        raise SynthesizerError(f"{field_name} supports at most {max_items} entries.")
    return tuple(_sanitize_text(item, field_name=field_name) for item in items)


def _string_list(value: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SynthesizerError(f"{field_name} must be a list of strings.")
    items: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise SynthesizerError(f"{field_name} must contain only strings.")
        normalized = entry.strip()
        if not normalized:
            raise SynthesizerError(f"{field_name} must contain non-empty strings only.")
        items.append(normalized)
    return tuple(items)


def _validate_evidence_refs(evidence_refs: tuple[str, ...], *, valid_signal_ids: tuple[str, ...]) -> None:
    allowed = set(valid_signal_ids)
    unknown_refs = [ref for ref in evidence_refs if ref not in allowed]
    if unknown_refs:
        raise SynthesizerError(
            "evidence_refs must cite only approved signal ids; unknown ids: "
            + ", ".join(sorted(dict.fromkeys(unknown_refs)))
        )


def _require_string(payload: dict[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise SynthesizerError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _require_field(payload: dict[str, object], field_name: str) -> object:
    if field_name not in payload:
        raise SynthesizerError(f"{field_name} must be provided.")
    return payload.get(field_name)


def _require_supported_enum_string(
    payload: dict[str, object],
    *,
    field_name: str,
    allowed_values: tuple[str, ...],
) -> str:
    value = _require_string(payload, field_name)
    normalized = value.lower()
    if normalized not in {entry.lower() for entry in allowed_values}:
        raise SynthesizerError(
            f"{field_name} must be one of: {', '.join(allowed_values)}."
        )
    return value


def _resolve_model_deployment(client: LLMProvider) -> str:
    """Best-effort model/deployment identifier for the cache key (Section
    8.8.3), mirroring ``risk_proposal_generator._resolve_model_deployment``.
    A wrong/missing id only under-shares the cache (never over-shares,
    since program_id/feature/input hash are still exact-matched), so this
    degrades safely rather than raising."""
    for attr in ("deployment", "primary_deployment", "model", "deployment_id"):
        value = getattr(client, attr, None)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _cached_response(key: AIResultCacheKey, *, programs_root: Path) -> dict[str, Any] | None:
    hit = get_ai_result(key, programs_root=programs_root)
    return hit.value if hit is not None else None