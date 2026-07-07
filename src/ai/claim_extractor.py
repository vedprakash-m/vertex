from __future__ import annotations

from datetime import date
from pathlib import Path
import re
from typing import Any

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.client import AIClientError, BudgetExceeded
from src.ai.deployment_fallback import FallbackStructuredClient, LEGACY_DEPLOYMENT_ALIAS_NOTICE, resolve_ai_deployments_for_feature
from src.ai.llm_trace import AITraceContext
from src.ai.provider import DisabledStructuredProvider, LLMProvider
from src.ai.tiered_router import TierResult, route_through_tiers
from src.core.ado_status import _area_path_matches
from src.core.claim_tracker import ClaimExtractionResult, extract_claims_from_confirmed_narratives
from src.core.models import WorkItem
from src.core.models_v2 import ClaimEntry, DecisionAsk, Program
from src.core.policy_loader import load_ai_feature_policy


PROMPT_VERSION = "claim_extractor.v1"
from src.ai.prompt_registry import load_prompt
_FEATURE = "claim_extractor"


class ClaimExtractorError(Exception):
    """Raised when AI-backed claim extraction cannot complete."""


class ClaimExtractorSafetyError(ClaimExtractorError):
    """Raised when AI output is rejected by the shared safety pipeline."""


class ClaimExtractorBudgetError(ClaimExtractorError):
    """Raised when the AI cost guard blocks claim extraction."""


class ClaimExtractor:
    """Extracts claims and decision asks from authored narratives."""

    def __init__(self, *, client: LLMProvider) -> None:
        self._client = client

    @classmethod
    def from_program(
        cls,
        program: Program,
        *,
        trace_context: AITraceContext | None = None,
    ) -> "ClaimExtractor":
        if get_ai_mode() == AIMode.DISABLED:
            return cls(client=DisabledStructuredProvider(
                feature_name="ClaimExtractor",
                empty_structured_payload={"claims": [], "decision_asks": []},
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
            raise ClaimExtractorError(
                "VERTEX_AI_DEPLOYMENT or AZURE_OPENAI_DEPLOYMENT not set. "
                f"{LEGACY_DEPLOYMENT_ALIAS_NOTICE} Configure Azure OpenAI to extract AI claims."
            )
        client = FallbackStructuredClient(
            deployments=deployments,
            temperature=temperature,
            budget_usd=budget_usd,
            requests_per_minute=program.ai.requests_per_minute if program.ai is not None else None,
            trace_context=trace_context,
        )
        return cls(client=client)

    def extract_claims(
        self,
        *,
        program_id: str,
        edition_id: str,
        issue_number: int,
        claim_date: date,
        narratives: dict[str, str],
        items: tuple[WorkItem, ...] = (),
        valid_workstream_ids: tuple[str, ...] = (),
        workstream_area_paths: dict[str, tuple[str, ...]] | None = None,
    ) -> ClaimExtractionResult:
        if not narratives:
            return ClaimExtractionResult(claims=(), decision_asks=(), warnings=())

        def _deterministic_tier() -> TierResult[ClaimExtractionResult] | None:
            result, confidence = _extract_deterministic_claims(
                program_id=program_id,
                edition_id=edition_id,
                issue_number=issue_number,
                claim_date=claim_date,
                narratives=narratives,
                items=items,
                valid_workstream_ids=valid_workstream_ids,
                workstream_area_paths=workstream_area_paths,
            )
            if result is None:
                return None
            return TierResult(value=result, confidence=confidence)

        def _frontier_tier() -> ClaimExtractionResult:
            system_prompt = _load_prompt()
            user_prompt = _build_user_prompt(
                narratives=narratives,
                items=items,
                valid_workstream_ids=valid_workstream_ids,
                workstream_area_paths=workstream_area_paths,
            )
            try:
                return self._client.structured(
                    system_prompt,
                    user_prompt,
                    parser=lambda payload: _parse_claim_extraction_payload(
                        payload=payload,
                        program_id=program_id,
                        edition_id=edition_id,
                        issue_number=issue_number,
                        claim_date=claim_date,
                        items=items,
                        valid_workstream_ids=valid_workstream_ids,
                        workstream_area_paths=workstream_area_paths,
                    ),
                    max_tokens=load_ai_feature_policy(_FEATURE).max_tokens,
                    prompt_version=PROMPT_VERSION,
                )
            except BudgetExceeded as error:
                raise ClaimExtractorBudgetError(f"AOAI cost guard triggered: {error}") from error
            except AIClientError as error:
                raise ClaimExtractorError(f"AI claim extraction failed: {error}") from error

        outcome = route_through_tiers(
            _FEATURE,
            deterministic_fn=_deterministic_tier,
            frontier_fn=_frontier_tier,
        )
        if outcome.value is not None:
            return outcome.value
        return ClaimExtractionResult(claims=(), decision_asks=(), warnings=())


def _extract_deterministic_claims(
    *,
    program_id: str,
    edition_id: str,
    issue_number: int,
    claim_date: date,
    narratives: dict[str, str],
    items: tuple[WorkItem, ...],
    valid_workstream_ids: tuple[str, ...],
    workstream_area_paths: dict[str, tuple[str, ...]] | None = None,
) -> tuple[ClaimExtractionResult | None, float]:
    explicit_result = _extract_explicit_deterministic_claims(
        program_id=program_id,
        edition_id=edition_id,
        issue_number=issue_number,
        claim_date=claim_date,
        narratives=narratives,
        items=items,
        valid_workstream_ids=valid_workstream_ids,
        workstream_area_paths=workstream_area_paths,
    )
    if explicit_result is not None:
        return explicit_result, 1.0
    regex_result = extract_claims_from_confirmed_narratives(
        program_id=program_id,
        edition_id=edition_id,
        issue_number=issue_number,
        claim_date=claim_date,
        narratives=narratives,
        items=items,
        valid_workstream_ids=valid_workstream_ids,
        workstream_area_paths=workstream_area_paths,
    )
    if regex_result.claims or regex_result.decision_asks:
        return regex_result, 0.95
    return None, 0.0


def _extract_explicit_deterministic_claims(
    *,
    program_id: str,
    edition_id: str,
    issue_number: int,
    claim_date: date,
    narratives: dict[str, str],
    items: tuple[WorkItem, ...],
    valid_workstream_ids: tuple[str, ...],
    workstream_area_paths: dict[str, tuple[str, ...]] | None = None,
) -> ClaimExtractionResult | None:
    claims: list[ClaimEntry] = []
    decision_asks: list[DecisionAsk] = []
    claim_index = 0
    ask_index = 0
    for _filename, content in sorted(narratives.items()):
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if _starts_with_marker(line, "Claim:"):
                claim_index += 1
                parsed_claim = _parse_deterministic_claim_line(
                    line=line,
                    program_id=program_id,
                    edition_id=edition_id,
                    issue_number=issue_number,
                    claim_date=claim_date,
                    items=items,
                    valid_workstream_ids=valid_workstream_ids,
                    workstream_area_paths=workstream_area_paths,
                    index=claim_index,
                )
                if parsed_claim is None:
                    return None
                claims.append(parsed_claim)
                continue
            if _starts_with_marker(line, "Decision ask:"):
                ask_index += 1
                parsed_ask = _parse_deterministic_decision_ask_line(
                    line=line,
                    program_id=program_id,
                    edition_id=edition_id,
                    issue_number=issue_number,
                    claim_date=claim_date,
                    items=items,
                    index=ask_index,
                )
                if parsed_ask is None:
                    return None
                decision_asks.append(parsed_ask)
    if not claims and not decision_asks:
        return None
    return ClaimExtractionResult(claims=tuple(claims), decision_asks=tuple(decision_asks), warnings=())


def _parse_deterministic_claim_line(
    *,
    line: str,
    program_id: str,
    edition_id: str,
    issue_number: int,
    claim_date: date,
    items: tuple[WorkItem, ...],
    valid_workstream_ids: tuple[str, ...],
    workstream_area_paths: dict[str, tuple[str, ...]] | None = None,
    index: int,
) -> ClaimEntry | None:
    payload = _parse_deterministic_payload(line, marker="Claim:")
    if payload is None:
        return None
    try:
        text = _parse_processed_text(payload["text"], items=items, field_name=f"deterministic claim entry #{index} text")
        refs = _parse_entity_refs(
            _parse_ref_list(payload.get("refs")),
            items=items,
            fallback_item_ids=_extract_work_item_ids_from_text(text.text),
            field_name=f"deterministic claim entry #{index} entity_refs",
        )
        workstream_id = _parse_optional_workstream_id(
            payload.get("workstream"),
            valid_workstream_ids=valid_workstream_ids,
            field_name=f"deterministic claim entry #{index} workstream_id",
        )
        _validate_claim_workstream_area_paths(
            workstream_id=workstream_id,
            entity_refs=refs,
            items=items,
            workstream_area_paths=workstream_area_paths,
            field_name=f"deterministic claim entry #{index} workstream_id",
        )
        return ClaimEntry(
            id=f"deterministic-claim-{index}",
            program_id=program_id,
            edition_id=edition_id,
            issue_number=issue_number,
            workstream_id=workstream_id,
            text=text.text,
            entity_refs=refs,
            claim_date=claim_date,
            owner_alias=_parse_optional_owner_alias(
                payload.get("owner"),
                field_name=f"deterministic claim entry #{index} owner_alias",
            ),
            due_date=_parse_optional_date(
                payload.get("due"),
                field_name=f"deterministic claim entry #{index} due_date",
            ),
        )
    except ClaimExtractorError:
        return None


def _parse_deterministic_decision_ask_line(
    *,
    line: str,
    program_id: str,
    edition_id: str,
    issue_number: int,
    claim_date: date,
    items: tuple[WorkItem, ...],
    index: int,
) -> DecisionAsk | None:
    payload = _parse_deterministic_payload(line, marker="Decision ask:")
    if payload is None:
        return None
    try:
        text = _parse_processed_text(payload["text"], items=items, field_name=f"deterministic decision ask entry #{index} text")
        refs = _parse_entity_refs(
            _parse_ref_list(payload.get("refs")),
            items=items,
            fallback_item_ids=_extract_work_item_ids_from_text(text.text),
            field_name=f"deterministic decision ask entry #{index} entity_refs",
        )
        return DecisionAsk(
            id=f"deterministic-decision-ask-{index}",
            program_id=program_id,
            edition_id=edition_id,
            issue_number=issue_number,
            text=text.text,
            entity_refs=refs,
            ask_date=claim_date,
            owner_alias=_parse_optional_owner_alias(
                payload.get("owner"),
                field_name=f"deterministic decision ask entry #{index} owner_alias",
            ),
        )
    except ClaimExtractorError:
        return None


def _starts_with_marker(line: str, marker: str) -> bool:
    return line[: len(marker)].lower() == marker.lower()


def _parse_deterministic_payload(line: str, *, marker: str) -> dict[str, str] | None:
    if not _starts_with_marker(line, marker):
        return None
    body = line[len(marker) :].strip()
    if not body:
        return None
    segments = [segment.strip() for segment in body.split("|")]
    if not segments or not segments[0]:
        return None
    payload = {"text": segments[0]}
    for segment in segments[1:]:
        if "=" not in segment:
            return None
        key, value = segment.split("=", 1)
        normalized_key = key.strip().lower()
        if normalized_key not in {"owner", "due", "workstream", "refs"}:
            return None
        payload[normalized_key] = value.strip()
    return payload


def _parse_ref_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    refs = [part.strip() for part in value.split(",") if part.strip()]
    return refs or None


def _load_prompt() -> str:
    return load_prompt(PROMPT_VERSION, error_factory=ClaimExtractorError)


def _build_user_prompt(
    *,
    narratives: dict[str, str],
    items: tuple[WorkItem, ...],
    valid_workstream_ids: tuple[str, ...],
    workstream_area_paths: dict[str, tuple[str, ...]] | None = None,
) -> str:
    lines = ["Allowed work item ids: " + (_render_allowed_work_item_ids(items) or "(none)")]
    lines.append("Allowed workstream ids: " + (", ".join(valid_workstream_ids) if valid_workstream_ids else "(none)"))
    if workstream_area_paths:
        lines.append("Allowed workstream area paths:")
        for workstream_id, area_paths in sorted(workstream_area_paths.items()):
            rendered_paths = ", ".join(area_paths) if area_paths else "(none)"
            lines.append(f"- {workstream_id}: {rendered_paths}")
    lines.append("")
    lines.append("Narratives:")
    for filename, content in sorted(narratives.items()):
        lines.append(f"## {filename}")
        lines.append(content.strip())
        lines.append("")
    return "\n".join(lines).strip()


def _render_allowed_work_item_ids(items: tuple[WorkItem, ...]) -> str:
    work_item_ids = [str(item.id) for item in items]
    return ", ".join(work_item_ids)


def _parse_claim_extraction_payload(
    *,
    payload: dict[str, object],
    program_id: str,
    edition_id: str,
    issue_number: int,
    claim_date: date,
    items: tuple[WorkItem, ...],
    valid_workstream_ids: tuple[str, ...],
    workstream_area_paths: dict[str, tuple[str, ...]] | None = None,
) -> ClaimExtractionResult:
    if not isinstance(payload, dict):
        raise ClaimExtractorError("AI claim payload must be an object.")
    if "claims" not in payload:
        raise ClaimExtractorError("AI claim payload must include a claims list.")
    if "decision_asks" not in payload:
        raise ClaimExtractorError("AI claim payload must include a decision_asks list.")

    raw_claims = payload.get("claims")
    raw_decision_asks = payload.get("decision_asks")
    if not isinstance(raw_claims, list):
        raise ClaimExtractorError("AI claim payload must include a claims list.")
    if not isinstance(raw_decision_asks, list):
        raise ClaimExtractorError("AI claim payload must include a decision_asks list.")

    claims = tuple(
        _parse_claim_entry(
            raw_claim,
            program_id=program_id,
            edition_id=edition_id,
            issue_number=issue_number,
            claim_date=claim_date,
            items=items,
            valid_workstream_ids=valid_workstream_ids,
            workstream_area_paths=workstream_area_paths,
            index=index,
        )
        for index, raw_claim in enumerate(raw_claims, start=1)
    )
    decision_asks = tuple(
        _parse_decision_ask_entry(
            raw_ask,
            program_id=program_id,
            edition_id=edition_id,
            issue_number=issue_number,
            claim_date=claim_date,
            items=items,
            index=index,
        )
        for index, raw_ask in enumerate(raw_decision_asks, start=1)
    )
    return ClaimExtractionResult(claims=claims, decision_asks=decision_asks, warnings=())


def _parse_claim_entry(
    raw_claim: object,
    *,
    program_id: str,
    edition_id: str,
    issue_number: int,
    claim_date: date,
    items: tuple[WorkItem, ...],
    valid_workstream_ids: tuple[str, ...],
    workstream_area_paths: dict[str, tuple[str, ...]] | None = None,
    index: int,
) -> ClaimEntry:
    if not isinstance(raw_claim, dict):
        raise ClaimExtractorError(f"AI claim entry #{index} must be an object.")
    if "text" not in raw_claim:
        raise ClaimExtractorError(f"AI claim entry #{index} must include text.")
    if "entity_refs" not in raw_claim:
        raise ClaimExtractorError(f"AI claim entry #{index} must include entity_refs.")
    if "due_date" not in raw_claim:
        raise ClaimExtractorError(f"AI claim entry #{index} must include due_date.")
    if "owner_alias" not in raw_claim:
        raise ClaimExtractorError(f"AI claim entry #{index} must include owner_alias.")
    if "workstream_id" not in raw_claim:
        raise ClaimExtractorError(f"AI claim entry #{index} must include workstream_id.")

    text = _parse_processed_text(raw_claim.get("text"), items=items, field_name=f"AI claim entry #{index} text")
    entity_refs = _parse_entity_refs(
        raw_claim.get("entity_refs"),
        items=items,
        fallback_item_ids=_extract_work_item_ids_from_text(text.text),
        field_name=f"AI claim entry #{index} entity_refs",
    )
    due_date = _parse_optional_date(raw_claim.get("due_date"), field_name=f"AI claim entry #{index} due_date")
    owner_alias = _parse_optional_owner_alias(raw_claim.get("owner_alias"), field_name=f"AI claim entry #{index} owner_alias")
    workstream_id = _parse_optional_workstream_id(
        raw_claim.get("workstream_id"),
        valid_workstream_ids=valid_workstream_ids,
        field_name=f"AI claim entry #{index} workstream_id",
    )
    _validate_claim_workstream_area_paths(
        workstream_id=workstream_id,
        entity_refs=entity_refs,
        items=items,
        workstream_area_paths=workstream_area_paths,
        field_name=f"AI claim entry #{index} workstream_id",
    )

    return ClaimEntry(
        id=f"ai-claim-{index}",
        program_id=program_id,
        edition_id=edition_id,
        issue_number=issue_number,
        workstream_id=workstream_id,
        text=text.text,
        entity_refs=entity_refs,
        claim_date=claim_date,
        owner_alias=owner_alias,
        due_date=due_date,
    )


def _parse_decision_ask_entry(
    raw_ask: object,
    *,
    program_id: str,
    edition_id: str,
    issue_number: int,
    claim_date: date,
    items: tuple[WorkItem, ...],
    index: int,
) -> DecisionAsk:
    if not isinstance(raw_ask, dict):
        raise ClaimExtractorError(f"AI decision ask entry #{index} must be an object.")
    if "text" not in raw_ask:
        raise ClaimExtractorError(f"AI decision ask entry #{index} must include text.")
    if "entity_refs" not in raw_ask:
        raise ClaimExtractorError(f"AI decision ask entry #{index} must include entity_refs.")
    if "owner_alias" not in raw_ask:
        raise ClaimExtractorError(f"AI decision ask entry #{index} must include owner_alias.")

    text = _parse_processed_text(raw_ask.get("text"), items=items, field_name=f"AI decision ask entry #{index} text")
    entity_refs = _parse_entity_refs(
        raw_ask.get("entity_refs"),
        items=items,
        fallback_item_ids=_extract_work_item_ids_from_text(text.text),
        field_name=f"AI decision ask entry #{index} entity_refs",
    )
    owner_alias = _parse_optional_owner_alias(
        raw_ask.get("owner_alias"),
        field_name=f"AI decision ask entry #{index} owner_alias",
    )

    return DecisionAsk(
        id=f"ai-decision-ask-{index}",
        program_id=program_id,
        edition_id=edition_id,
        issue_number=issue_number,
        text=text.text,
        entity_refs=entity_refs,
        ask_date=claim_date,
        owner_alias=owner_alias,
    )


def _parse_processed_text(value: object, *, items: tuple[WorkItem, ...], field_name: str):
    del items
    if not isinstance(value, str):
        raise ClaimExtractorError(f"{field_name} must be a string.")
    normalized = " ".join(value.split()).strip(" -\t")
    if not normalized:
        raise ClaimExtractorError(f"{field_name} must be non-empty.")
    try:
        return process_generated_text(normalized)
    except AIPipelineError as error:
        raise ClaimExtractorSafetyError(f"{field_name} rejected by safety pipeline: {error}") from error


def _extract_work_item_ids_from_text(text: str) -> tuple[int, ...]:
    work_item_ids: list[int] = []
    for match in re.finditer(r"\bWI:(\d+)\b", text, flags=re.IGNORECASE):
        work_item_id = int(match.group(1))
        if work_item_id not in work_item_ids:
            work_item_ids.append(work_item_id)
    return tuple(work_item_ids)


def _parse_entity_refs(
    value: object,
    *,
    items: tuple[WorkItem, ...],
    fallback_item_ids: tuple[int, ...],
    field_name: str,
) -> tuple[str, ...]:
    allowed_work_item_ids = {item.id for item in items}
    refs: list[str] = []
    if value is None:
        raw_refs: list[object] = []
    elif not isinstance(value, list):
        raise ClaimExtractorError(f"{field_name} must be a list of WI refs.")
    else:
        raw_refs = value

    for raw_ref in raw_refs:
        if not isinstance(raw_ref, str):
            raise ClaimExtractorError(f"{field_name} entries must be WI:<id> strings.")
        normalized = raw_ref.strip().upper()
        if not normalized.startswith("WI:"):
            raise ClaimExtractorError(f"{field_name} entries must be WI:<id> strings.")
        try:
            work_item_id = int(normalized.split(":", 1)[1])
        except ValueError as error:
            raise ClaimExtractorError(f"{field_name} entries must be WI:<id> strings.") from error
        if work_item_id not in allowed_work_item_ids:
            raise ClaimExtractorError(f"{field_name} contains work item {work_item_id} outside the allowed set.")
        ref = f"WI:{work_item_id}"
        if ref not in refs:
            refs.append(ref)

    if not refs:
        for work_item_id in fallback_item_ids:
            if work_item_id in allowed_work_item_ids:
                refs.append(f"WI:{work_item_id}")
    return tuple(refs)


def _parse_optional_date(value: object, *, field_name: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ClaimExtractorError(f"{field_name} must be a YYYY-MM-DD string or null.")
    normalized = value.strip()
    if not normalized:
        raise ClaimExtractorError(f"{field_name} must be a YYYY-MM-DD string or null.")
    try:
        return date.fromisoformat(normalized)
    except ValueError as error:
        raise ClaimExtractorError(f"{field_name} must be a YYYY-MM-DD string or null.") from error


def _parse_optional_owner_alias(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ClaimExtractorError(f"{field_name} must be a non-empty alias string or null.")
    normalized = value.strip().lower()
    if not normalized:
        raise ClaimExtractorError(f"{field_name} must be a non-empty alias string or null.")
    if "@" in normalized:
        normalized = normalized.split("@", 1)[0]
    normalized = "".join(character for character in normalized if character.isalnum() or character in {".", "_", "-"})
    if not normalized:
        raise ClaimExtractorError(f"{field_name} must be a non-empty alias string or null.")
    return normalized


def _parse_optional_workstream_id(
    value: object,
    *,
    valid_workstream_ids: tuple[str, ...],
    field_name: str,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ClaimExtractorError(f"{field_name} must be a string or null.")
    normalized = value.strip()
    if not normalized:
        raise ClaimExtractorError(f"{field_name} must be a string or null.")
    if valid_workstream_ids and normalized not in set(valid_workstream_ids):
        raise ClaimExtractorError(f"{field_name} must be one of the allowed workstream ids.")
    return normalized


def _validate_claim_workstream_area_paths(
    *,
    workstream_id: str | None,
    entity_refs: tuple[str, ...],
    items: tuple[WorkItem, ...],
    workstream_area_paths: dict[str, tuple[str, ...]] | None,
    field_name: str,
) -> None:
    if workstream_id is None or not entity_refs or not workstream_area_paths:
        return
    expected_area_paths = workstream_area_paths.get(workstream_id)
    if not expected_area_paths:
        return
    items_by_id = {item.id: item for item in items}
    for entity_ref in entity_refs:
        if not entity_ref.upper().startswith("WI:"):
            continue
        try:
            work_item_id = int(entity_ref.split(":", 1)[1])
        except ValueError:
            continue
        item = items_by_id.get(work_item_id)
        if item is None:
            continue
        if any(_area_path_matches(item.area_path, area_path) for area_path in expected_area_paths):
            continue
        raise ClaimExtractorError(
            f"{field_name} does not match configured area paths for referenced work item WI:{work_item_id}."
        )
