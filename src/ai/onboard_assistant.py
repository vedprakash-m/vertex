from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.client import AIClientError
from src.ai.deployment_fallback import FallbackStructuredClient, LEGACY_DEPLOYMENT_ALIAS_NOTICE, resolve_ai_deployments_for_feature
from src.ai.llm_trace import AITraceContext
from src.ai.prompt_registry import load_prompt
from src.ai.tiered_router import TierResult, route_through_tiers
from src.core.ado_client import ADOClient
from src.core.exceptions import AuthError, QueryError
from src.core.query_builder import build_odata_filter
from src.core.policy_loader import load_ai_feature_policy


PROMPT_VERSION_STRUCTURE = "onboard_structure_assistant.v1"
PROMPT_VERSION_STYLE = "onboard_style_assistant.v1"
_FEATURE = "onboard_assistant"

class _StructuredProvider(Protocol):
    def structured(
        self,
        system: str,
        user: str,
        *,
        parser: Callable[[dict[str, Any]], Any],
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ) -> Any: ...


class ADOReadClient(Protocol):
    def suggest_area_paths(self, program_name: str) -> tuple[str, ...]: ...

    def query_all(
        self,
        filter_expression: str,
        select_fields: tuple[str, ...],
        top: int = 1000,
    ) -> list[dict[str, str]]: ...


AdoClientFactory = Callable[[str, str, int], ADOReadClient]
NowProvider = Callable[[], datetime]


class OnboardAssistantError(Exception):
    """Raised when onboarding suggestions cannot be produced."""


@dataclass(frozen=True, slots=True)
class SuggestedDimension:
    name: str
    description: str | None
    ado_filter: str


@dataclass(frozen=True, slots=True)
class SuggestedScorecard:
    name: str
    dimensions: tuple[SuggestedDimension, ...]


@dataclass(frozen=True, slots=True)
class StructureSuggestions:
    scorecards: tuple[SuggestedScorecard, ...]
    prompt_version: str


@dataclass(frozen=True, slots=True)
class StyleSuggestions:
    voice: str | None
    structure: str | None
    risk_framing_improving: str | None
    risk_framing_stuck: str | None
    risk_framing_escalation: str | None
    risk_framing_new_risk: str | None
    preferred_patterns: tuple[str, ...]
    prompt_version: str


class OnboardAssistant:
    """Provides optional Stage 2/3/5 onboarding suggestions."""

    def __init__(
        self,
        *,
        client: _StructuredProvider | None,
        ado_client_factory: AdoClientFactory | None = None,
        now_provider: NowProvider | None = None,
    ) -> None:
        self._client = client
        self._ado_client_factory = ado_client_factory or _default_ado_client_factory
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    @classmethod
    def from_environment(
        cls,
        *,
        trace_context: AITraceContext | None = None,
    ) -> "OnboardAssistant":
        if get_ai_mode() == AIMode.DISABLED:
            return cls(client=None)
        deployments = resolve_ai_deployments_for_feature(
            feature_name=_FEATURE,
            primary_candidates=(),
            backup_candidates=(),
            primary_fallback_envs=("VERTEX_AI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"),
            backup_fallback_envs=("VERTEX_AI_BACKUP_DEPLOYMENT",),
        )
        if not deployments:
            raise OnboardAssistantError(
                "VERTEX_AI_DEPLOYMENT or AZURE_OPENAI_DEPLOYMENT not set. "
                f"{LEGACY_DEPLOYMENT_ALIAS_NOTICE} Configure Azure OpenAI or omit --ai."
            )
        client = FallbackStructuredClient(
            deployments=deployments,
            temperature=load_ai_feature_policy(_FEATURE).temperature,
            budget_usd=0.5,
            trace_context=trace_context,
        )
        return cls(client=client)

    def suggest_area_paths(
        self,
        *,
        program_name: str,
        organization: str,
        project: str,
        api_timeout_seconds: int,
    ) -> tuple[str, ...]:
        try:
            ado_client = self._ado_client_factory(organization, project, api_timeout_seconds)
            return ado_client.suggest_area_paths(program_name)
        except (AuthError, QueryError) as error:
            raise OnboardAssistantError(f"Unable to suggest area paths from ADO: {error}") from error

    def suggest_scorecards(
        self,
        *,
        program_name: str,
        objective: str,
        edition_type: str,
        organization: str,
        project: str,
        area_paths: tuple[str, ...],
        work_item_types: tuple[str, ...],
        excluded_states: tuple[str, ...],
        date_window_days: int,
        api_timeout_seconds: int,
    ) -> StructureSuggestions:
        if get_ai_mode() == AIMode.DISABLED or self._client is None:
            return StructureSuggestions(scorecards=(), prompt_version=PROMPT_VERSION_STRUCTURE)
        system_prompt = load_prompt(PROMPT_VERSION_STRUCTURE, error_factory=OnboardAssistantError)
        sample_items = self._load_work_item_samples(
            organization=organization,
            project=project,
            area_paths=area_paths,
            work_item_types=work_item_types,
            excluded_states=excluded_states,
            date_window_days=date_window_days,
            api_timeout_seconds=api_timeout_seconds,
        )
        user_prompt = _build_structure_prompt(
            program_name=program_name,
            objective=objective,
            edition_type=edition_type,
            area_paths=area_paths,
            work_item_types=work_item_types,
            sample_items=sample_items,
        )
        try:
            client = self._client
            outcome = route_through_tiers(
                _FEATURE,
                deterministic_fn=lambda: None,
                frontier_fn=lambda: client.structured(
                    system_prompt,
                    user_prompt,
                    parser=lambda payload: _parse_structure_suggestions(payload, prompt_version=PROMPT_VERSION_STRUCTURE),
                    max_tokens=load_ai_feature_policy(_FEATURE).structure_max_tokens or load_ai_feature_policy(_FEATURE).max_tokens,
                    prompt_version=PROMPT_VERSION_STRUCTURE,
                ),
                policy=load_ai_feature_policy(_FEATURE),
            )
        except AIClientError as error:
            raise OnboardAssistantError(f"AI structure suggestion failed: {error}") from error
        if outcome.value is None:
            return StructureSuggestions(scorecards=(), prompt_version=PROMPT_VERSION_STRUCTURE)
        return outcome.value

    def analyze_style_sample(self, sample_paragraph: str) -> StyleSuggestions:
        normalized_sample = sample_paragraph.strip()
        if not normalized_sample:
            raise OnboardAssistantError("A sample paragraph is required for AI style analysis.")
        if get_ai_mode() == AIMode.DISABLED or self._client is None:
            return StyleSuggestions(
                voice=None,
                structure=None,
                risk_framing_improving=None,
                risk_framing_stuck=None,
                risk_framing_escalation=None,
                risk_framing_new_risk=None,
                preferred_patterns=(),
                prompt_version=PROMPT_VERSION_STYLE,
            )
        system_prompt = load_prompt(PROMPT_VERSION_STYLE, error_factory=OnboardAssistantError)
        user_prompt = _build_style_prompt(normalized_sample)
        try:
            client = self._client
            outcome = route_through_tiers(
                _FEATURE,
                deterministic_fn=lambda: None,
                frontier_fn=lambda: client.structured(
                    system_prompt,
                    user_prompt,
                    parser=lambda payload: _parse_style_suggestions(payload, prompt_version=PROMPT_VERSION_STYLE),
                    max_tokens=load_ai_feature_policy(_FEATURE).style_max_tokens or load_ai_feature_policy(_FEATURE).max_tokens,
                    prompt_version=PROMPT_VERSION_STYLE,
                ),
                policy=load_ai_feature_policy(_FEATURE),
            )
        except AIClientError as error:
            raise OnboardAssistantError(f"AI style analysis failed: {error}") from error
        if outcome.value is None:
            return StyleSuggestions(
                voice=None,
                structure=None,
                risk_framing_improving=None,
                risk_framing_stuck=None,
                risk_framing_escalation=None,
                risk_framing_new_risk=None,
                preferred_patterns=(),
                prompt_version=PROMPT_VERSION_STYLE,
            )
        return outcome.value

    def _load_work_item_samples(
        self,
        *,
        organization: str,
        project: str,
        area_paths: tuple[str, ...],
        work_item_types: tuple[str, ...],
        excluded_states: tuple[str, ...],
        date_window_days: int,
        api_timeout_seconds: int,
    ) -> tuple[dict[str, str], ...]:
        try:
            ado_client = self._ado_client_factory(organization, project, api_timeout_seconds)
            since = self._now_provider() - timedelta(days=max(date_window_days, 7))
            filter_expression = build_odata_filter(
                area_paths=area_paths,
                work_item_types=work_item_types,
                since=since,
                states_excluded=excluded_states,
            )
            raw_items = ado_client.query_all(
                filter_expression,
                select_fields=("WorkItemId", "Title", "WorkItemType", "State"),
                top=25,
            )
        except (AuthError, QueryError):
            return ()

        samples: list[dict[str, str]] = []
        for item in raw_items:
            work_item_id = item.get("WorkItemId")
            title = _optional_text(item.get("Title"))
            work_item_type = _optional_text(item.get("WorkItemType"))
            if work_item_id is None or title is None or work_item_type is None:
                continue
            samples.append(
                {
                    "id": str(work_item_id),
                    "title": title,
                    "type": work_item_type,
                    "state": _optional_text(item.get("State")) or "unknown",
                }
            )
        return tuple(samples)


def _default_ado_client_factory(organization: str, project: str, timeout: int) -> ADOReadClient:
    return ADOClient(organization=organization, project=project, timeout=timeout)


def _build_structure_prompt(
    *,
    program_name: str,
    objective: str,
    edition_type: str,
    area_paths: tuple[str, ...],
    work_item_types: tuple[str, ...],
    sample_items: tuple[dict[str, str], ...],
) -> str:
    lines = [
        f"Program: {program_name}",
        f"Objective: {objective}",
        f"Edition type: {edition_type}",
        f"Area paths: {', '.join(area_paths) if area_paths else 'none'}",
        f"Work item types: {', '.join(work_item_types) if work_item_types else 'none'}",
    ]
    if sample_items:
        lines.append("Recent work item sample:")
        for item in sample_items:
            lines.append(
                f"- #{item['id']} | type={item['type']} | state={item['state']} | title={item['title']}"
            )
    else:
        lines.append("Recent work item sample: unavailable")
    lines.append("Return JSON only.")
    return "\n".join(lines)


def _build_style_prompt(sample_paragraph: str) -> str:
    return "\n".join(
        (
            "Analyze the following sample output paragraph and extract reusable author preferences.",
            "Return JSON only.",
            "Sample paragraph:",
            sample_paragraph,
        )
    )


def _parse_structure_suggestions(payload: dict[str, object], *, prompt_version: str) -> StructureSuggestions:
    if "scorecards" not in payload:
        raise OnboardAssistantError("AI structure suggestion must return a 'scorecards' array.")
    raw_scorecards = payload.get("scorecards")
    if not isinstance(raw_scorecards, list):
        raise OnboardAssistantError("AI structure suggestion must return a 'scorecards' array.")

    scorecards: list[SuggestedScorecard] = []
    for raw_scorecard in raw_scorecards:
        if not isinstance(raw_scorecard, dict):
            raise OnboardAssistantError("AI structure suggestion scorecards must be objects.")
        scorecard_name = _optional_ai_text(raw_scorecard.get("name"), field_name="name")
        if "dimensions" not in raw_scorecard:
            raise OnboardAssistantError("AI structure suggestion scorecards must include dimensions as a list.")
        raw_dimensions = raw_scorecard.get("dimensions")
        if scorecard_name is None:
            raise OnboardAssistantError("AI structure suggestion scorecards must include name as a string.")
        if not isinstance(raw_dimensions, list):
            raise OnboardAssistantError("AI structure suggestion scorecard dimensions must be a list.")
        dimensions: list[SuggestedDimension] = []
        for raw_dimension in raw_dimensions:
            if not isinstance(raw_dimension, dict):
                raise OnboardAssistantError("AI structure suggestion dimensions must be objects.")
            dimension_name = _optional_ai_text(raw_dimension.get("name"), field_name="dimensions.name")
            ado_filter = _optional_ai_text(raw_dimension.get("ado_filter"), field_name="dimensions.ado_filter")
            if dimension_name is None:
                raise OnboardAssistantError("AI structure suggestion dimensions must include name as a string.")
            if ado_filter is None:
                raise OnboardAssistantError("AI structure suggestion dimensions must include ado_filter as a string.")
            dimensions.append(
                SuggestedDimension(
                    name=dimension_name,
                    description=_optional_ai_text(raw_dimension.get("description"), field_name="dimensions.description"),
                    ado_filter=ado_filter,
                )
            )
        if not dimensions:
            raise OnboardAssistantError("AI returned no usable scorecard suggestions.")
        scorecards.append(SuggestedScorecard(name=scorecard_name, dimensions=tuple(dimensions)))

    if not scorecards:
        raise OnboardAssistantError("AI returned no usable scorecard suggestions.")
    return StructureSuggestions(scorecards=tuple(scorecards), prompt_version=prompt_version)


def _parse_style_suggestions(payload: dict[str, object], *, prompt_version: str) -> StyleSuggestions:
    if "risk_framing" not in payload:
        raise OnboardAssistantError("AI style suggestion must return risk_framing as an object.")
    risk_framing = payload.get("risk_framing")
    if not isinstance(risk_framing, dict):
        raise OnboardAssistantError("AI style suggestion risk_framing must be an object when provided.")
    if "preferred_patterns" not in payload:
        raise OnboardAssistantError("AI style suggestion must return preferred_patterns as a list.")
    preferred_patterns = payload.get("preferred_patterns")
    if not isinstance(preferred_patterns, list):
        raise OnboardAssistantError("AI style suggestion preferred_patterns must be a list when provided.")

    normalized_patterns: list[str] = []
    for pattern in preferred_patterns:
        if not isinstance(pattern, str):
            raise OnboardAssistantError("AI style suggestion preferred_patterns must contain strings only.")
        normalized = _optional_ai_text(pattern, field_name="preferred_patterns")
        if not normalized:
            raise OnboardAssistantError("AI style suggestion preferred_patterns must contain non-empty strings only.")
        normalized_patterns.append(normalized)

    suggestions = StyleSuggestions(
        voice=_optional_style_text(payload.get("voice"), field_name="voice"),
        structure=_optional_style_text(payload.get("structure"), field_name="structure"),
        risk_framing_improving=_optional_style_text(risk_framing.get("improving"), field_name="risk_framing.improving"),
        risk_framing_stuck=_optional_style_text(risk_framing.get("stuck"), field_name="risk_framing.stuck"),
        risk_framing_escalation=_optional_style_text(risk_framing.get("escalation"), field_name="risk_framing.escalation"),
        risk_framing_new_risk=_optional_style_text(risk_framing.get("new_risk"), field_name="risk_framing.new_risk"),
        preferred_patterns=tuple(normalized_patterns),
        prompt_version=prompt_version,
    )
    if not any(
        (
            suggestions.voice,
            suggestions.structure,
            suggestions.risk_framing_improving,
            suggestions.risk_framing_stuck,
            suggestions.risk_framing_escalation,
            suggestions.risk_framing_new_risk,
            suggestions.preferred_patterns,
        )
    ):
        raise OnboardAssistantError("AI returned no usable style suggestions.")
    return suggestions


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _optional_style_text(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OnboardAssistantError(f"AI style suggestion {field_name} must be a non-empty string when provided.")
    normalized = _optional_ai_text(value, field_name=field_name)
    if normalized is None:
        raise OnboardAssistantError(f"AI style suggestion {field_name} must be a non-empty string when provided.")
    return normalized


def _optional_ai_text(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OnboardAssistantError(f"AI suggestion {field_name} must be a string when provided.")
    normalized = value.strip()
    if not normalized:
        return None
    try:
        processed = process_generated_text(normalized)
    except AIPipelineError as error:
        raise OnboardAssistantError(f"AI suggestion {field_name} rejected by safety pipeline: {error}") from error
    return processed.text or None
