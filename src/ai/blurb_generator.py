from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.provider import LLMProvider
from src.ai.tiered_router import TierResult, route_through_tiers
from src.core.ai_result_cache import AIResultCacheKey, canonical_input_hash, get_ai_result, put_ai_result
from src.core.ai_schema_gateway import SchemaGatewayError, validate_bounded_payload
from src.core.ban_list_validator import find_ban_list_violations
from src.core.config_loader import EditorialRules, NarrativeProgramContext
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.evidence_models import SourceRef, extract_icm_ids
from src.core.models import Confidence, DeltaKind, EditionType, EvidencePacket, ItemDelta, WorkItem
from src.core.models_v2 import Signal, WorkstreamEvidenceBundle
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_release_decision,
    record_ai_run_lifecycle,
)
from src.core.telemetry_summary import build_approved_telemetry_summary
from src.core.verbosity_enforcer import enforce_verbosity, split_sentences
from src.core.voice_validator import build_writing_contract_prompt_lines, has_decision_or_delta_lead
from src.core.voice_validator import starts_with_synthetic_delta_token, uses_authentic_voice
from src.core.policy_loader import load_ai_feature_policy


PROMPT_VERSION = "workstream_blurb.v1"
POLICY_VERSION = "workstream_blurb.v1"
from src.ai.prompt_registry import load_prompt
_FEATURE = "blurb_generator"
_OUTPUT_SCHEMA_VERSION = "1"
_DELTA_PREFIXES = ("NEW", "CLOSED", "RISK_UP", "RISK_DOWN", "ETA", "OWNER")
_MAX_ELIGIBLE_ITEM_LINES = 8
_MAX_ELIGIBLE_ITEM_LINE_CHARS = 260
_MAX_EVIDENCE_LINES = 16
_MAX_EVIDENCE_CHARS = 2200
_MAX_SUPPLEMENTAL_LINES = 8
_MAX_SUPPLEMENTAL_CHARS = 1800


class BlurbGenerationError(Exception):
    """Raised when a generated workstream blurb violates the editorial contract."""


@dataclass(frozen=True, slots=True)
class WorkstreamBlurb:
    text: str
    prompt_version: str
    cited_work_item_ids: tuple[int, ...]
    ai_confidence: Confidence
    # P4-1 (§19.1): source refs cited by the evidence feeding this blurb — required for
    # the multi-source citation success criterion. Empty for the ADO-only baseline.
    cited_source_refs: tuple[SourceRef, ...] = ()


def generate_workstream_blurb(
    *,
    client: LLMProvider,
    workstream_name: str,
    items: tuple[WorkItem, ...] | list[WorkItem],
    evidence_by_item: dict[int, EvidencePacket],
    deltas: tuple[ItemDelta, ...] | list[ItemDelta],
    editorial_rules: EditorialRules,
    program_id: str,
    edition_type: EditionType | None = None,
    program_context: NarrativeProgramContext | None = None,
    supplemental_context: tuple[str, ...] = (),
    workstream_evidence_bundle: WorkstreamEvidenceBundle | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> WorkstreamBlurb | None:
    """Runs the same AISchemaGateway/QG-29 safety lifecycle established by
    ``risk_proposal_generator.py`` (ADF-W5.1, P7): bounds-checked
    request/response payloads, the five ``AIRunState`` transitions, cache
    wiring via ``AIResultCacheKey``, and a durable QG-29 terminal release
    decision recorded before any caller may consume the result. Preserves
    this feature's existing raise-on-rejection contract
    (``BlurbGenerationError``) rather than switching to a
    return-``None``-on-rejection convention, since ``report_ai.py``'s
    deployment-fallback loop already depends on ``BlurbGenerationError``
    propagating -- same reasoning as ``exec_summary_drafter.py``'s
    migration."""
    missing_item_ids = tuple(item.id for item in items if evidence_by_item.get(item.id) is None)
    if missing_item_ids:
        raise BlurbGenerationError(
            "Workstream blurb items missing evidence context: "
            + ", ".join(str(item_id) for item_id in dict.fromkeys(missing_item_ids))
        )

    eligible_items = tuple(
        item
        for item in items
        if evidence_by_item.get(item.id) is not None and evidence_by_item[item.id].confidence != Confidence.NONE
    )
    if not eligible_items:
        return None

    eligible_ids = {item.id for item in eligible_items}
    relevant_deltas = tuple(delta for delta in deltas if delta.work_item_id in eligible_ids)
    if not relevant_deltas:
        return None

    prompt_template = _load_prompt_template()
    system_prompt = prompt_template.format(workstream_name=workstream_name)
    max_words = editorial_rules.verbosity.workstream_blurb_max_words_for(edition_type)
    user_prompt = _build_user_prompt(
        workstream_name=workstream_name,
        eligible_items=eligible_items,
        evidence_by_item=evidence_by_item,
        deltas=relevant_deltas,
        editorial_rules=editorial_rules,
        program_context=program_context,
        supplemental_context=supplemental_context,
        max_words=max_words,
        workstream_evidence_bundle=workstream_evidence_bundle,
    )

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

    request_payload = {
        "program_id": program_id,
        "workstream_name": workstream_name,
        "eligible_item_ids": [item.id for item in eligible_items],
        "supplemental_context": list(supplemental_context),
        "max_words": max_words,
    }
    try:
        validate_bounded_payload(request_payload)
    except SchemaGatewayError as error:
        reason = f"AISchemaGateway rejected the outbound request: {error}"
        _discard(ReleaseTerminal.DISCARDED, reason)
        raise BlurbGenerationError(reason) from error

    _lifecycle(AIRunState.REQUESTED)

    policy = load_ai_feature_policy(_FEATURE)
    cache_key = AIResultCacheKey(
        program_id=program_id,
        feature=_FEATURE,
        canonical_input_hash=canonical_input_hash(user_prompt),
        prompt_version=PROMPT_VERSION,
        policy_version=POLICY_VERSION,
        model_deployment=_resolve_model_deployment(client),
        context_manifest_hash=canonical_input_hash(
            "|".join(str(item.id) for item in eligible_items)
        ),
        output_schema_version=_OUTPUT_SCHEMA_VERSION,
    )
    raw = route_through_tiers(
        _FEATURE,
        deterministic_fn=lambda: None,
        frontier_fn=lambda: client.structured(
            system_prompt,
            user_prompt,
            parser=lambda payload: payload,
            max_tokens=policy.max_tokens,
            prompt_version=PROMPT_VERSION,
        ),
        policy=policy,
        cache_lookup_fn=lambda: _cached_response(cache_key, programs_root=programs_root),
        cache_store_fn=lambda value: put_ai_result(cache_key, value, programs_root=programs_root),
    ).value

    _lifecycle(AIRunState.RESPONDED)

    try:
        validate_bounded_payload(raw)
    except SchemaGatewayError as error:
        reason = f"AISchemaGateway rejected the response: {error}"
        _discard(ReleaseTerminal.REJECTED, reason)
        raise BlurbGenerationError(reason) from error

    _lifecycle(AIRunState.SCHEMA_VALIDATED)

    try:
        raw_text = _parse_generated_blurb_text(cast(dict[str, object], raw))
    except BlurbGenerationError as error:
        _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)
        _discard(ReleaseTerminal.REJECTED, str(error))
        raise
    if not raw_text:
        _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)
        _discard(ReleaseTerminal.REJECTED, "generated workstream blurb text was empty.")
        return None

    try:
        grounded = process_generated_text(raw_text, allowed_items=eligible_items)
    except AIPipelineError as error:
        _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)
        _discard(ReleaseTerminal.REJECTED, str(error))
        raise BlurbGenerationError(str(error)) from error

    if not grounded.text:
        _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)
        _discard(ReleaseTerminal.REJECTED, "processed text pipeline produced empty output.")
        return None

    try:
        _validate_delta_lead(
            grounded.text,
            program_context=program_context,
            editorial_rules=editorial_rules,
        )
        _validate_editorial_rules(
            grounded.text,
            workstream_name,
            editorial_rules,
            edition_type=edition_type,
        )
    except BlurbGenerationError as error:
        _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)
        _discard(ReleaseTerminal.REJECTED, str(error))
        raise

    _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)
    _discard(ReleaseTerminal.RELEASED, "passed AISchemaGateway bounds and editorial-rules validation")

    return WorkstreamBlurb(
        text=grounded.text,
        prompt_version=PROMPT_VERSION,
        cited_work_item_ids=grounded.cited_work_item_ids,
        ai_confidence=_derive_ai_confidence(evidence_by_item, grounded.cited_work_item_ids),
        cited_source_refs=_bundle_source_refs(workstream_evidence_bundle),
    )


def _derive_ai_confidence(
    evidence_by_item: dict[int, EvidencePacket],
    cited_work_item_ids: tuple[int, ...],
) -> Confidence:
    missing_item_ids = tuple(item_id for item_id in cited_work_item_ids if evidence_by_item.get(item_id) is None)
    if missing_item_ids:
        raise BlurbGenerationError(
            "Workstream blurb cited work items missing evidence confidence: "
            + ", ".join(str(item_id) for item_id in missing_item_ids)
        )
    confidences = [evidence_by_item[item_id].confidence for item_id in cited_work_item_ids]
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


def _load_prompt_template() -> str:
    return load_prompt(PROMPT_VERSION, error_factory=BlurbGenerationError)


def _build_user_prompt(
    *,
    workstream_name: str,
    eligible_items: tuple[WorkItem, ...],
    evidence_by_item: dict[int, EvidencePacket],
    deltas: tuple[ItemDelta, ...],
    editorial_rules: EditorialRules,
    program_context: NarrativeProgramContext | None,
    supplemental_context: tuple[str, ...],
    max_words: int | None,
    workstream_evidence_bundle: WorkstreamEvidenceBundle | None = None,
) -> str:
    lines = [f"Workstream: {workstream_name}"]
    if program_context is not None:
        lines.append(f"Program: {program_context.program_name}")
        objective = getattr(program_context, "objective", None)
        if objective:
            lines.append(f"Objective: {objective}")
        lines.extend(
            build_writing_contract_prompt_lines(
                program_context,
                editorial_rules=editorial_rules,
                workstream_name=workstream_name,
            )
        )

    lines.append("Eligible items (confidence != NONE):")
    eligible_item_lines: list[str] = []
    for item in eligible_items:
        evidence = evidence_by_item[item.id]
        item_deltas = ", ".join(delta.kind.value for delta in deltas if delta.work_item_id == item.id) or "none"
        eligible_item_lines.append(
            f"- #{item.id} | title={item.title} | risk={item.risk_level.value} | deltas={item_deltas} | evidence={evidence.summary_for_reviewer}"
        )
    lines.extend(
        _apply_prompt_context_budget(
            eligible_item_lines,
            max_lines=_MAX_ELIGIBLE_ITEM_LINES,
            max_chars=_MAX_ELIGIBLE_ITEM_LINES * _MAX_ELIGIBLE_ITEM_LINE_CHARS,
            omission_prefix="- eligible_items_omitted:",
        )
    )

    # P4-1: structured multi-source evidence (M365 + ADO comments) — the headline
    # addition that lets blurbs reflect Teams/email/quantitative context, not just
    # ADO EvidencePackets. Each source is already approval-gated upstream (§17.8).
    evidence_lines = _bundle_evidence_prompt_lines(workstream_evidence_bundle)
    if evidence_lines:
        lines.append("Structured lane evidence (approved, cite where relevant):")
        lines.extend(
            _apply_prompt_context_budget(
                evidence_lines,
                max_lines=_MAX_EVIDENCE_LINES,
                max_chars=_MAX_EVIDENCE_CHARS,
                omission_prefix="- evidence_context_omitted:",
            )
        )

    if supplemental_context:
        lines.append("Supplemental context:")
        lines.extend(
            _apply_prompt_context_budget(
                tuple(f"- {entry}" for entry in supplemental_context if entry.strip()),
                max_lines=_MAX_SUPPLEMENTAL_LINES,
                max_chars=_MAX_SUPPLEMENTAL_CHARS,
                omission_prefix="- supplemental_context_omitted:",
            )
        )

    if max_words is not None:
        lines.append(f"Keep the blurb within {max_words} words.")
    lines.append("Only cite the supplied work item ids using [#WI].")
    return "\n".join(lines)


def _bundle_source_refs(bundle: WorkstreamEvidenceBundle | None) -> tuple[SourceRef, ...]:
    """P4-1/§19.1: the source refs cited by the M365 evidence in the bundle."""
    if bundle is None or bundle.m365_evidence is None:
        return ()
    return tuple(bundle.m365_evidence.source_refs)


def _bundle_evidence_prompt_lines(bundle: WorkstreamEvidenceBundle | None) -> tuple[str, ...]:
    """Render the bundle's M365 evidence + ADO-comment signals as prompt context.

    Returns lines (no header); the caller prepends the section header. Empty when
    there is no bundle, no M365 evidence, and no ADO comments.
    """
    if bundle is None:
        return ()
    lines: list[str] = []
    ev = bundle.m365_evidence
    if ev is not None:
        lines.append(f"- risk_level={ev.risk_level.value} | confidence={ev.confidence:.2f}")
        if ev.narrative_summary:
            lines.append(f"- narrative: {ev.narrative_summary}")
        if ev.blocking_items:
            lines.append(f"- blocking_items: {', '.join(ev.blocking_items)}")
        if ev.etas:
            eta_strs = [
                f"{e.label} (due {e.eta_date.isoformat()}, owner={e.owner or 'unassigned'}, status={e.status})"
                for e in ev.etas
            ]
            lines.append(f"- etas: {'; '.join(eta_strs)}")
        if ev.owners:
            lines.append(f"- owners: {', '.join(ev.owners)}")
        if ev.source_refs:
            ref_strs = [
                f"[{r.source_type}] {r.description}"
                + (f" ({r.author})" if r.author else "")
                for r in ev.source_refs
            ]
            lines.append(f"- sources: {'; '.join(ref_strs)}")
    # ADO-comment signals carry the engineer "why" narrative (source="ado/comment").
    for sig in bundle.ado_comments:
        text = (sig.text or "").strip()
        if text:
            lines.append(f"- ado_comment: {text[:280]}")
    ado_telemetry_summary = build_approved_telemetry_summary(bundle.ado_signals)
    if ado_telemetry_summary:
        lines.append(f"- ado_telemetry: {ado_telemetry_summary}")
    # P4-8 (§8.5): IcM incidents as structured blockers — severity, title, id, owner.
    for sig in bundle.icm_blockers:
        text = (sig.text or "").strip()
        if not text:
            continue
        meta = sig.metadata if isinstance(sig.metadata, dict) else {}
        incident_id = _icm_incident_id(sig, meta)
        id_part = f" (id=IcM:{incident_id})" if incident_id else ""
        owner = meta.get("owning_team")
        owner_part = f", owner={owner}" if owner else ""
        lines.append(f"- icm_blocker: {text[:220]}{id_part}{owner_part}")
    # P4-9 (§8.6): Kusto metrics as quantitative ground-truth context.
    for sig in bundle.kusto_metrics:
        text = (sig.text or "").strip()
        if text:
            lines.append(f"- kusto_metric: {text[:220]}")
    for sig in bundle.reference_signals:
        text = (sig.text or "").strip()
        if text:
            lines.append(f"- reference_update: {text[:220]}")
    if bundle.lookback_intelligence:
        for detail in bundle.lookback_intelligence:
            lines.append(f"- trajectory: {detail}")
    if bundle.freshness_by_source:
        freshness_parts = [
            f"{source}={timestamp.date().isoformat()}"
            for source, timestamp in sorted(bundle.freshness_by_source.items())
        ]
        lines.append(f"- source_freshness: {'; '.join(freshness_parts)}")
    if bundle.corroboration_notes:
        for note in bundle.corroboration_notes:
            lines.append(f"- corroboration: {note}")
    return tuple(lines)


def _apply_prompt_context_budget(
    entries: tuple[str, ...] | list[str],
    *,
    max_lines: int,
    max_chars: int,
    omission_prefix: str,
) -> tuple[str, ...]:
    """P4-18: bound prompt context growth before it reaches the LLM."""
    kept: list[str] = []
    total_chars = 0
    omitted_count = 0
    for entry in entries:
        normalized = " ".join(entry.split()).strip()
        if not normalized:
            continue
        budget_remaining = max_chars - total_chars
        if len(kept) >= max_lines or budget_remaining <= 0:
            omitted_count += 1
            continue
        if len(normalized) > budget_remaining:
            if budget_remaining < 48:
                omitted_count += 1
                continue
            normalized = normalized[: max(0, budget_remaining - 3)].rstrip() + "..."
        kept.append(normalized)
        total_chars += len(normalized)
    if omitted_count:
        kept.append(f"{omission_prefix} {omitted_count} additional line(s) trimmed for prompt budget.")
    return tuple(kept)


def _icm_incident_id(signal: Signal, metadata: dict) -> str | None:
    """Best-effort IcM incident id from a signal (metadata first, then refs/text)."""
    raw = metadata.get("incident_id")
    if raw:
        return str(raw)
    blob = " ".join(signal.entity_refs) + " " + (signal.text or "")
    ids = extract_icm_ids(blob)
    return ids[0] if ids else None


def _validate_delta_lead(
    text: str,
    *,
    program_context: NarrativeProgramContext | None,
    editorial_rules: EditorialRules,
) -> None:
    sentences = split_sentences(text)
    if not sentences:
        raise BlurbGenerationError("Generated workstream blurb is empty after grounding.")
    first_sentence = sentences[0].lstrip()
    if uses_authentic_voice(editorial_rules, program_context):
        if starts_with_synthetic_delta_token(first_sentence, editorial_rules):
            raise BlurbGenerationError(
                "Generated workstream blurb must lead with the changed or blocking lane, not a synthetic delta token."
            )
        if not has_decision_or_delta_lead(first_sentence, editorial_rules):
            raise BlurbGenerationError(
                "Generated workstream blurb must lead with the blocking lane or the most meaningful decision delta."
            )
        return
    if not any(first_sentence.startswith(prefix) for prefix in _DELTA_PREFIXES):
        raise BlurbGenerationError(
            "Generated workstream blurb must lead with a delta token (NEW, CLOSED, RISK_UP/DOWN, ETA, OWNER)."
        )


def _validate_editorial_rules(
    text: str,
    workstream_name: str,
    editorial_rules: EditorialRules,
    *,
    edition_type: EditionType | None,
) -> None:
    violations = find_ban_list_violations({f"workstream:{workstream_name}": text}, editorial_rules)
    if violations:
        phrases = ", ".join(sorted({violation.phrase for violation in violations}))
        raise BlurbGenerationError(f"Generated workstream blurb violates the ban-list: {phrases}")

    verbosity_violations = enforce_verbosity(
        workstream_blurbs={workstream_name: text},
        exec_summary_text="",
        scorecard_summaries={},
        subject_line=None,
        verbosity=editorial_rules.verbosity,
        edition_type=edition_type,
    )
    if verbosity_violations:
        messages = "; ".join(violation.message for violation in verbosity_violations)
        raise BlurbGenerationError(f"Generated workstream blurb violates verbosity rules: {messages}")


def _parse_generated_blurb_text(payload: dict[str, object]) -> str:
    if not isinstance(payload, dict):
        raise BlurbGenerationError("Generated workstream blurb payload must be an object.")
    text = payload.get("text")
    if not isinstance(text, str):
        raise BlurbGenerationError("Generated workstream blurb payload must include text as a string.")
    normalized = text.strip()
    if not normalized:
        raise BlurbGenerationError("Generated workstream blurb payload text must be non-empty.")
    return normalized


def _resolve_model_deployment(client: LLMProvider) -> str:
    return getattr(client, "deployment", None) or getattr(client, "model", None) or type(client).__name__


def _cached_response(key: AIResultCacheKey, *, programs_root: Path) -> dict[str, object] | None:
    hit = get_ai_result(key, programs_root=programs_root)
    return hit.value if hit is not None else None
