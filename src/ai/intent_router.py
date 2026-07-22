from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.client import AIClientError
from src.ai.deployment_fallback import FallbackStructuredClient, LEGACY_DEPLOYMENT_ALIAS_NOTICE, resolve_ai_deployments_for_feature
from src.ai.provider import LLMProvider
from src.ai.tiered_router import TierResult, route_through_tiers
from src.core.ai_schema_gateway import SchemaGatewayError, validate_bounded_payload
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.exceptions import ConfigError
from src.core.quality_gates.ai_release_audit import (
    AIRunState,
    ReleaseTerminal,
    new_ai_run_id,
    record_ai_release_decision,
    record_ai_run_lifecycle,
)
from src.core.yaml_utils import load_yaml_mapping
from src.core.policy_loader import load_ai_feature_policy


PROMPT_VERSION = "intent_router.v1"
from src.ai.prompt_registry import load_prompt
_FEATURE = "intent_router"
_INTENT_ROUTES_PATH = Path(__file__).resolve().parents[2] / "vertex" / "intent_routes.yaml"
_INTENT_ROUTE_SCHEMA_VERSION = "1"
_INTENT_ROUTE_CATALOG: dict[str, "IntentRouteCommandSpec"] | None = None
_ONLY_CLAUSE_PATTERN = re.compile(r"\b(?:with|for)\s+([^.,;!?]+?\s+only)\b", re.IGNORECASE)
_ISSUE_PATTERN = re.compile(r"\bissue\s+(\d{1,4})\b", re.IGNORECASE)
_COMPARE_ISSUES_PATTERN = re.compile(
    r"\b(?:diff|compare)\b.*?\bissue\s+(\d{1,4})\b.*?\b(?:and|to|vs\.?|versus)\b.*?\bissue\s+(\d{1,4})\b",
    re.IGNORECASE,
)
_SEARCH_ARCHIVE_PATTERN = re.compile(r"\b(?:search|find)\b.*?\barchive\b.*?\bfor\b\s+(.+)", re.IGNORECASE)
_SEARCH_SEMANTIC_HISTORY_PATTERN = re.compile(
    r"\b(?:search|find|show)\b.*?\b(?:semantic|similar)\b.*?\b(?:history|archive)\b.*?\bfor\b\s+(.+)",
    re.IGNORECASE,
)
_ISO_WEEK_PATTERN = re.compile(r"\b(\d{4}-W\d{2})\b", re.IGNORECASE)
_ADO_WORK_ITEM_PATTERN = re.compile(r"\b(?:wi|work item)\s*[:#]?\s*(\d{1,8})\b", re.IGNORECASE)
_INVESTIGATE_ICM_PATTERN = re.compile(r"\binvestigate\b.*?\b(?:icm|incident)\s*[:#]?\s*(\d{1,8})\b", re.IGNORECASE)
_INVESTIGATE_ACCOUNT_PATTERN = re.compile(r"\binvestigate\b.*?\baccount\s+([A-Za-z0-9._:-]+)\b", re.IGNORECASE)
_OWNER_PACK_FOR_PATTERN = re.compile(r"\bowner\s+pack\b.*?\bfor\s+([A-Za-z][A-Za-z0-9._-]*)\b", re.IGNORECASE)
_OWNER_PACK_POSSESSIVE_PATTERN = re.compile(r"\b([A-Za-z][A-Za-z0-9._-]*)['’]s\s+owner\s+pack\b", re.IGNORECASE)
_TRANSCRIPT_PATTERN = re.compile(r"\btranscript\s+([A-Za-z0-9][A-Za-z0-9._:-]{2,})\b", re.IGNORECASE)
_MEETING_ID_PATTERN = re.compile(r"\bmeeting(?:\s+id)?\s+([A-Za-z0-9][A-Za-z0-9._:-]{2,})\b", re.IGNORECASE)


class IntentRouterError(Exception):
    """Raised when a natural-language request cannot be mapped safely."""


@dataclass(frozen=True, slots=True)
class RoutedInvocation:
    command: str
    args: tuple[str, ...]
    warnings: tuple[str, ...]
    prompt_version: str | None


@dataclass(frozen=True, slots=True)
class IntentRouteLeafSpec:
    flags: frozenset[str]
    value_options: frozenset[str]


@dataclass(frozen=True, slots=True)
class IntentRouteCommandSpec:
    callback: IntentRouteLeafSpec | None
    subcommands: dict[str, IntentRouteLeafSpec]


@runtime_checkable
class _StructuredProvider(Protocol):
    def structured(
        self,
        system: str,
        user: str,
        *,
        parser: Any,
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ) -> Any: ...


class IntentRouter:
    """Maps natural-language requests to existing Vertex CLI invocations."""

    def __init__(self, *, client: _StructuredProvider | None = None) -> None:
        self._client = client

    @classmethod
    def from_environment(cls) -> "IntentRouter":
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
            raise IntentRouterError(
                "VERTEX_AI_DEPLOYMENT or AZURE_OPENAI_DEPLOYMENT not set. "
                f"{LEGACY_DEPLOYMENT_ALIAS_NOTICE} Configure Azure OpenAI or use only supported deterministic intent routes."
            )
        client = FallbackStructuredClient(
            deployments=deployments,
            temperature=load_ai_feature_policy(_FEATURE).temperature,
            budget_usd=0.25,
        )
        return cls(client=client)

    def route(self, request: str, *, default_edition: str = "", programs_root: Path = PROGRAMS_ROOT) -> RoutedInvocation:
        normalized_request = request.strip()
        if not normalized_request:
            raise IntentRouterError("A natural-language request is required.")

        if self._client is None:
            # No AI client — deterministic-only path.
            routed = _route_known_intent(normalized_request, default_edition=default_edition)
            if routed is not None:
                return routed
            raise IntentRouterError(
                "Unsupported natural-language request for deterministic routing. Try a direct CLI command or configure Azure OpenAI for AI fallback."
            )

        system_prompt = _load_prompt()
        user_prompt = _build_user_prompt(request=normalized_request, default_edition=default_edition)
        _det = _route_known_intent(normalized_request, default_edition=default_edition)
        program_id = _program_id_from_edition(default_edition)

        def _deterministic_fn() -> TierResult[RoutedInvocation] | None:
            if _det is None:
                return None
            return TierResult(value=_det, confidence=1.0)

        try:
            client = self._client
            outcome = route_through_tiers(
                _FEATURE,
                deterministic_fn=_deterministic_fn,
                frontier_fn=lambda: _run_ai_route(
                    client,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    program_id=program_id,
                    programs_root=programs_root,
                ),
                policy=load_ai_feature_policy(_FEATURE),
            )
        except AIClientError as error:
            raise IntentRouterError(f"Intent routing failed: {error}") from error

        if outcome.value is not None:
            return outcome.value
        raise IntentRouterError(
            "Unsupported natural-language request for deterministic routing. Try a direct CLI command or configure Azure OpenAI for AI fallback."
        )


def _run_ai_route(
    client: _StructuredProvider,
    *,
    system_prompt: str,
    user_prompt: str,
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> RoutedInvocation:
    """specs/backlog.md BL-C2: bounds-check the raw response through
    AISchemaGateway and record a durable QG-29 release-audit trail before
    any routed invocation is consumed -- ``intent_router``'s output
    directly determines which CLI command executes, so this is a
    ``production``-classified call site per governance/ai-call-inventory.md.

    ``_parse_routed_invocation``'s own args/route-catalog validation
    already does real semantic checking (a routed command must name an
    enumerated command, its args must satisfy that command's declared
    flags/value_options) -- there is no separate ``SemanticValidator``
    class here because that validation already exists and already raises
    the exact ``IntentRouterError`` this module's callers depend on;
    duplicating it as a second validator would just be two copies of the
    same rule.
    """
    ai_run_id = new_ai_run_id()

    def _lifecycle(state: AIRunState) -> None:
        record_ai_run_lifecycle(
            program_id=program_id,
            ai_run_id=ai_run_id,
            feature=_FEATURE,
            state=state,
            prompt_version=PROMPT_VERSION,
            policy_version=PROMPT_VERSION,
            programs_root=programs_root,
        )

    def _discard(terminal: ReleaseTerminal, reason: str) -> None:
        record_ai_release_decision(
            program_id=program_id,
            ai_run_id=ai_run_id,
            terminal=terminal,
            reason=reason,
            validator_finding_count=0,
            programs_root=programs_root,
        )

    _lifecycle(AIRunState.PLANNED)
    _lifecycle(AIRunState.REQUESTED)
    try:
        raw = client.structured(
            system_prompt,
            user_prompt,
            parser=lambda payload: payload,
            max_tokens=load_ai_feature_policy(_FEATURE).max_tokens,
            prompt_version=PROMPT_VERSION,
        )
    except Exception as error:
        _discard(ReleaseTerminal.DISCARDED, f"provider call failed: {error}")
        raise
    _lifecycle(AIRunState.RESPONDED)

    if not isinstance(raw, dict):
        _discard(ReleaseTerminal.DISCARDED, "no structured response returned by the provider.")
        raise IntentRouterError("Intent routing returned a non-object payload.")

    try:
        validate_bounded_payload(raw)
    except SchemaGatewayError as error:
        _discard(ReleaseTerminal.REJECTED, f"AISchemaGateway rejected the response: {error}")
        raise IntentRouterError(f"Intent routing response rejected by AISchemaGateway: {error}") from error
    _lifecycle(AIRunState.SCHEMA_VALIDATED)

    try:
        invocation = _parse_routed_invocation(raw)
    except IntentRouterError as error:
        _discard(ReleaseTerminal.REJECTED, f"route-catalog/args validation failed: {error}")
        raise
    _lifecycle(AIRunState.SEMANTICALLY_VALIDATED)

    record_ai_release_decision(
        program_id=program_id,
        ai_run_id=ai_run_id,
        terminal=ReleaseTerminal.RELEASED,
        reason="passed AISchemaGateway bounds and route-catalog/args validation",
        validator_finding_count=0,
        programs_root=programs_root,
    )
    return invocation


def render_invocation(invocation: RoutedInvocation) -> str:
    parts = [invocation.command, *invocation.args]
    return "vertex " + " ".join(parts)


def _intent_route_catalog() -> dict[str, IntentRouteCommandSpec]:
    global _INTENT_ROUTE_CATALOG
    if _INTENT_ROUTE_CATALOG is None:
        _INTENT_ROUTE_CATALOG = _load_intent_route_catalog(_INTENT_ROUTES_PATH)
    return _INTENT_ROUTE_CATALOG


def _load_intent_route_catalog(path: Path) -> dict[str, IntentRouteCommandSpec]:
    try:
        document = load_yaml_mapping(path)
    except ConfigError as error:
        raise IntentRouterError(f"Unable to load intent route catalog: {error}") from error

    schema_version = document.get("schema_version")
    if schema_version != _INTENT_ROUTE_SCHEMA_VERSION:
        raise IntentRouterError(
            f"Unsupported intent route catalog schema_version {schema_version!r} in {path}; "
            f"expected {_INTENT_ROUTE_SCHEMA_VERSION!r}."
        )
    raw_commands = document.get("commands")
    if not isinstance(raw_commands, dict) or not raw_commands:
        raise IntentRouterError(f"Intent route catalog at {path} must define commands as a non-empty mapping.")

    catalog: dict[str, IntentRouteCommandSpec] = {}
    for command, raw_spec in sorted(raw_commands.items()):
        if not isinstance(command, str) or not command.strip():
            raise IntentRouterError(f"Intent route catalog at {path} contains an invalid command key.")
        if not isinstance(raw_spec, dict):
            raise IntentRouterError(f"Intent route catalog entry for {command!r} must be a mapping.")
        callback = _parse_route_leaf_spec(
            raw_spec.get("callback"),
            field_name=f"commands.{command}.callback",
            required=False,
        )
        raw_subcommands = raw_spec.get("subcommands", {})
        if not isinstance(raw_subcommands, dict):
            raise IntentRouterError(f"commands.{command}.subcommands must be a mapping when provided.")
        subcommands: dict[str, IntentRouteLeafSpec] = {}
        for subcommand, raw_leaf in sorted(raw_subcommands.items()):
            if not isinstance(subcommand, str) or not subcommand.strip():
                raise IntentRouterError(f"commands.{command}.subcommands contains an invalid subcommand key.")
            leaf = _parse_route_leaf_spec(
                raw_leaf,
                field_name=f"commands.{command}.subcommands.{subcommand}",
                required=True,
            )
            if leaf is None:  # pragma: no cover - required=True guarantees this path is unreachable.
                raise IntentRouterError(f"commands.{command}.subcommands.{subcommand} must be provided.")
            subcommands[subcommand] = leaf
        if callback is None and not subcommands:
            raise IntentRouterError(
                f"commands.{command} must declare a callback spec, subcommands, or both in {path}."
            )
        catalog[command] = IntentRouteCommandSpec(callback=callback, subcommands=subcommands)
    return catalog


def _parse_route_leaf_spec(
    raw_spec: object,
    *,
    field_name: str,
    required: bool,
) -> IntentRouteLeafSpec | None:
    if raw_spec is None:
        if required:
            raise IntentRouterError(f"{field_name} must be provided.")
        return None
    if not isinstance(raw_spec, dict):
        raise IntentRouterError(f"{field_name} must be a mapping.")
    flags = _parse_string_collection(raw_spec.get("flags", ()), field_name=f"{field_name}.flags")
    value_options = _parse_string_collection(
        raw_spec.get("value_options", ()),
        field_name=f"{field_name}.value_options",
    )
    overlap = flags & value_options
    if overlap:
        raise IntentRouterError(f"{field_name} defines the same token in flags and value_options: {sorted(overlap)}")
    return IntentRouteLeafSpec(flags=flags, value_options=value_options)


def _parse_string_collection(value: object, *, field_name: str) -> frozenset[str]:
    if not isinstance(value, list):
        raise IntentRouterError(f"{field_name} must be a list of strings.")
    items: set[str] = set()
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise IntentRouterError(f"{field_name} must contain non-empty strings only.")
        items.add(entry.strip())
    return frozenset(items)


def _route_known_intent(request: str, *, default_edition: str) -> RoutedInvocation | None:
    lowered = request.casefold()
    warnings = list(_unsupported_filter_warnings(request))

    compare_match = _COMPARE_ISSUES_PATTERN.search(request)
    if compare_match is not None:
        older_issue, newer_issue = compare_match.groups()
        return RoutedInvocation(
            command="history",
            args=("--edition", default_edition, "--diff", older_issue, newer_issue),
            warnings=tuple(warnings),
            prompt_version=None,
        )

    search_match = _SEARCH_ARCHIVE_PATTERN.search(request)
    if search_match is not None:
        keyword = search_match.group(1).strip().strip('"')
        if keyword:
            return RoutedInvocation(
                command="history",
                args=("--edition", default_edition, "--search", keyword),
                warnings=tuple(warnings),
                prompt_version=None,
            )

    if "kb changelog" in lowered or "knowledge base changelog" in lowered:
        week_match = _ISO_WEEK_PATTERN.search(request)
        if week_match is not None:
            warnings.extend(_program_scope_warnings(request))
            return RoutedInvocation(
                command="kb",
                args=("changelog", "--program", _program_id_from_edition(default_edition), "--since", week_match.group(1).upper()),
                warnings=tuple(dict.fromkeys(warnings)),
                prompt_version=None,
            )

    investigate_icm_match = _INVESTIGATE_ICM_PATTERN.search(request)
    if investigate_icm_match is not None:
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="investigate",
            args=("--program", _program_id_from_edition(default_edition), "--icm", investigate_icm_match.group(1), "--dry-run"),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    investigate_account_match = _INVESTIGATE_ACCOUNT_PATTERN.search(request)
    if investigate_account_match is not None:
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="investigate",
            args=("--program", _program_id_from_edition(default_edition), "--account", investigate_account_match.group(1), "--dry-run"),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    semantic_search_match = _SEARCH_SEMANTIC_HISTORY_PATTERN.search(request)
    if semantic_search_match is not None:
        keyword = semantic_search_match.group(1).strip().strip('"')
        if keyword:
            return RoutedInvocation(
                command="history",
                args=("--edition", default_edition, "--semantic", keyword),
                warnings=tuple(warnings),
                prompt_version=None,
            )

    issue_number = _extract_issue_number(request)
    ado_work_item_id = _extract_ado_work_item_id(request)
    owner_pack_alias = _extract_owner_pack_alias(request)
    transcript_id = _extract_transcript_or_meeting_id(request)

    if any(token in lowered for token in ("redo", "rerun", "regenerate", "rebuild", "draft again", "draft issue")) and issue_number is not None:
        return RoutedInvocation(
            command="report",
            args=("--edition", default_edition, "--issue", issue_number, "--dry-run"),
            warnings=tuple(warnings),
            prompt_version=None,
        )

    if any(token in lowered for token in ("confirm", "publish issue")) and issue_number is not None:
        return RoutedInvocation(
            command="confirm",
            args=("--edition", default_edition, "--issue", issue_number),
            warnings=tuple(warnings),
            prompt_version=None,
        )

    if "publish gate" in lowered and issue_number is not None:
        return RoutedInvocation(
            command="publish-gate",
            args=("--edition", default_edition, "--issue", issue_number),
            warnings=tuple(warnings),
            prompt_version=None,
        )

    if (
        "review" in lowered
        and issue_number is not None
        and "review sections" not in lowered
        and "proposal review" not in lowered
        and "review proposals" not in lowered
        and "show proposals review" not in lowered
        and "section review status" not in lowered
    ):
        return RoutedInvocation(
            command="review-full",
            args=("--edition", default_edition, "--issue", issue_number, "--open"),
            warnings=tuple(warnings),
            prompt_version=None,
        )

    if "history" in lowered and issue_number is not None:
        return RoutedInvocation(
            command="history",
            args=("--edition", default_edition, "--issue", issue_number),
            warnings=tuple(warnings),
            prompt_version=None,
        )

    if any(phrase in lowered for phrase in ("evidence for", "show evidence", "trace evidence", "lineage for")) and ado_work_item_id is not None:
        return RoutedInvocation(
            command="evidence",
            args=("--edition", default_edition, "--issue", "latest", "--ado", ado_work_item_id),
            warnings=tuple(warnings),
            prompt_version=None,
        )

    if any(phrase in lowered for phrase in ("meeting close", "close meeting", "close transcript", "follow up on transcript", "follow up on meeting")) and transcript_id is not None:
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="meeting-close",
            args=("--program", _program_id_from_edition(default_edition), "--transcript", transcript_id, "--dry-run"),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "catch me up",
            "session catchup",
            "show catchup",
            "run catchup",
            "give me a catchup",
            "show me a catchup",
            "bring me up to speed",
            "bring me up to date",
        )
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="catchup",
            args=("--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(phrase in lowered for phrase in ("show manifest", "current manifest", "manifest for", "latest manifest")):
        manifest_args: tuple[str, ...] = ("--edition", default_edition)
        if issue_number is not None:
            manifest_args = (*manifest_args, "--issue", issue_number)
        return RoutedInvocation(
            command="manifest",
            args=manifest_args,
            warnings=tuple(warnings),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "show contradictions",
            "show me contradictions",
            "reconcile contradictions",
            "contradiction report",
            "contradiction review",
            "what contradictions exist",
        )
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="reconcile",
            args=("--program", _program_id_from_edition(default_edition), "--dry-run"),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "list editions",
            "show editions",
            "configured editions",
            "what editions are available",
            "which editions are configured",
        )
    ):
        return RoutedInvocation(
            command="list",
            args=("editions",),
            warnings=tuple(warnings),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "list workstreams",
            "show workstreams",
            "configured workstreams",
            "what workstreams are there",
            "which workstreams are configured",
        )
    ):
        return RoutedInvocation(
            command="list",
            args=("workstreams", "--edition", default_edition),
            warnings=tuple(warnings),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "show registry",
            "list registry",
            "show m365 registry",
            "list m365 registry",
        )
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="registry",
            args=("list", "--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in ("list dris", "show dris", "show owners", "who are the dris", "who are the owners")
    ):
        return RoutedInvocation(
            command="list",
            args=("dris", "--edition", default_edition),
            warnings=tuple(warnings),
            prompt_version=None,
        )

    if any(phrase in lowered for phrase in ("list milestones", "show milestones", "milestone list")):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="milestones",
            args=("list", "--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "assess milestones",
            "milestone health",
            "milestone assessment",
            "show milestone status",
            "show me milestone status",
            "milestones are at risk",
        )
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="milestones",
            args=("assess", "--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(phrase in lowered for phrase in ("review sections", "show review sections", "section review status")):
        review_sections_args: tuple[str, ...] = ("show", "--edition", default_edition)
        if issue_number is not None:
            review_sections_args = (*review_sections_args, "--issue", issue_number)
        return RoutedInvocation(
            command="review-sections",
            args=review_sections_args,
            warnings=tuple(warnings),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "dependency proposals",
            "inferred dependencies",
            "show dependency proposals",
            "show me the dependency proposals",
            "show dependencies",
            "show me dependencies",
            "give me the dependency proposals",
            "dependencies need review",
            "what dependencies need review",
        )
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="dependencies",
            args=("list", "--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "generate prep brief",
            "prep brief",
            "prepare prep brief",
            "prepare me for the meeting",
            "prep me for the meeting",
            "show me prep brief",
            "show me the prep brief",
            "give me prep brief",
            "give me the prep brief",
            "help me prepare for the meeting",
        )
    ):
        return RoutedInvocation(
            command="prep",
            args=("--edition", default_edition),
            warnings=tuple(warnings),
            prompt_version=None,
        )

    if owner_pack_alias is not None:
        return RoutedInvocation(
            command="owner-pack",
            args=("--program", _program_id_from_edition(default_edition), "--owner", owner_pack_alias),
            warnings=tuple(warnings),
            prompt_version=None,
        )

    if any(phrase in lowered for phrase in ("deck companion", "generate deck companion", "show deck companion")):
        deck_companion_args: tuple[str, ...] = ("--edition", default_edition)
        if issue_number is not None:
            deck_companion_args = (*deck_companion_args, "--issue", issue_number)
        return RoutedInvocation(
            command="deck-companion",
            args=deck_companion_args,
            warnings=tuple(warnings),
            prompt_version=None,
        )

    if any(phrase in lowered for phrase in ("review proposals", "show proposals review", "proposal review")):
        review_proposals_args: tuple[str, ...] = ("--edition", default_edition, "--no-open")
        if issue_number is not None:
            review_proposals_args = ("--edition", default_edition, "--issue", issue_number, "--no-open")
        return RoutedInvocation(
            command="review-proposals",
            args=review_proposals_args,
            warnings=tuple(warnings),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "fleet health",
            "show fleet",
            "fleet summary",
            "program fleet",
            "how is the fleet doing",
            "how are all programs doing",
        )
    ):
        return RoutedInvocation(
            command="fleet",
            args=(),
            warnings=tuple(warnings),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "trust calibration",
            "show trust",
            "trust profile",
            "autonomy trust",
            "how much do we trust the system",
            "how much can we trust the system",
            "how trustworthy is the system",
            "how trustworthy are we",
        )
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="trust",
            args=("--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "audit timeline",
            "show audit",
            "audit log",
            "prompt learning summary",
            "what did we learn from prompts",
            "what have we learned from prompts",
        )
    ):
        warnings.extend(_program_scope_warnings(request))
        audit_args: tuple[str, ...] = ("--program", _program_id_from_edition(default_edition))
        if (
            "prompt learning" in lowered
            or "what did we learn from prompts" in lowered
            or "what have we learned from prompts" in lowered
        ):
            audit_args = (*audit_args, "--prompt-learning-summary")
        return RoutedInvocation(
            command="audit",
            args=audit_args,
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "preview decision debt follow up",
            "preview decision debt follow-up",
            "preview decision debt follow ups",
            "preview decision debt follow-ups",
            "preview stale decision follow up",
            "preview stale decision follow-up",
            "preview stale decision follow ups",
            "preview stale decision follow-ups",
            "preview decision ask follow up",
            "preview decision ask follow-up",
            "preview decision ask follow ups",
            "preview decision ask follow-ups",
            "pending decision follow up",
            "pending decision follow-up",
            "pending decision follow ups",
            "pending decision follow-ups",
            "pending 14-day decision nudges",
            "14-day decision nudges",
        )
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="decisions",
            args=("aging", "--program", _program_id_from_edition(default_edition), "--apply", "--dry-run"),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "apply decision debt follow up",
            "apply decision debt follow-up",
            "apply decision debt follow ups",
            "apply decision debt follow-ups",
            "run decision debt follow up",
            "run decision debt follow-up",
            "run decision debt follow ups",
            "run decision debt follow-ups",
            "apply stale decision follow up",
            "apply stale decision follow-up",
            "apply stale decision follow ups",
            "apply stale decision follow-ups",
            "apply decision ask follow up",
            "apply decision ask follow-up",
            "apply decision ask follow ups",
            "apply decision ask follow-ups",
            "approved decision follow up",
            "approved decision follow-up",
            "approved decision follow ups",
            "approved decision follow-ups",
        )
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="decisions",
            args=("aging", "--program", _program_id_from_edition(default_edition), "--apply"),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "decision debt",
            "aging decisions",
            "old decision asks",
            "stale decisions",
            "decisions are due",
            "decisions need follow-up",
            "decisions need follow up",
            "due decisions",
        )
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="decisions",
            args=("aging", "--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "author salience",
            "show salience",
            "salience model",
            "attention model",
            "how much attention are we paying",
        )
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="salience",
            args=("show", "--program", _program_id_from_edition(default_edition), "--no-refresh"),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(phrase in lowered for phrase in ("what changed", "what's changed", "whats changed")):
        warnings.extend(_conversational_scope_warnings(request))
        return RoutedInvocation(
            command="triage",
            args=("--edition", default_edition),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    normalized_request = lowered.strip(" ?!.")

    if normalized_request == "status" or any(
        phrase in lowered
        for phrase in (
            "our status",
            "status on",
            "status for",
            "status please",
            "give me status",
            "give us status",
            "give me the status",
            "give us the status",
            "show status",
            "show me status",
            "show us status",
            "show me the status",
            "show us the status",
            "show me our status",
            "current status",
            "what is the status",
            "what's the status",
            "what is our status",
            "what's our status",
            "how is the status",
            "how's the status",
            "how is our status",
            "how's our status",
            "status update",
            "where do we stand",
        )
    ):
        warnings.extend(_conversational_scope_warnings(request))
        return RoutedInvocation(
            command="status",
            args=("--edition", default_edition),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "morning brief",
            "today brief",
            "brief me",
            "today's brief",
            "my brief today",
        )
    ):
        brief_warnings = list(_program_scope_warnings(request))
        if "for today" in lowered:
            brief_warnings = [warning for warning in brief_warnings if "topic filters" not in warning]
        warnings.extend(brief_warnings)
        return RoutedInvocation(
            command="brief",
            args=("--program", _program_id_from_edition(default_edition), "--today"),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "next",
            "what should i do",
            "what can i do next",
            "what's next",
            "what is next",
            "what do we do next",
            "what should we do next",
            "what should i work on",
            "what should i focus on",
            "what do i do now",
            "what next",
            "next step",
            "what's my next move",
            "next move",
        )
    ):
        next_warnings = list(_conversational_scope_warnings(request))
        if "what should i work on" in lowered or "what should i focus on" in lowered:
            next_warnings = [warning for warning in next_warnings if "topic filters" not in warning]
        warnings.extend(next_warnings)
        return RoutedInvocation(
            command="next",
            args=("--edition", default_edition),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "readiness",
            "launch readiness",
            "release readiness",
            "are we ready",
            "ready to launch",
            "ready for launch",
            "ready for the launch",
            "ready for release",
            "ready for the release",
            "how ready are we",
            "how ready is launch",
            "how ready is the launch",
            "how ready is the release",
        )
    ):
        readiness_warnings = list(_program_scope_warnings(request))
        if (
            "ready for release" in lowered
            or "ready for the release" in lowered
            or "ready for launch" in lowered
            or "ready for the launch" in lowered
            or "how ready is launch" in lowered
            or "how ready is the launch" in lowered
            or "how ready is the release" in lowered
        ):
            readiness_warnings = [
                warning for warning in readiness_warnings if "topic filters" not in warning
            ]
        warnings.extend(readiness_warnings)
        return RoutedInvocation(
            command="readiness",
            args=("--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "risk register",
            "show risks",
            "show me risks",
            "list risks",
            "open risks",
            "top risks",
            "risks are open",
            "current risks",
            "risks need attention",
        )
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="risks",
            args=("list", "--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "assumptions",
            "show assumptions",
            "show me assumptions",
            "give me assumptions",
            "list assumptions",
            "current assumptions",
            "what assumptions are open",
        )
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="assumptions",
            args=("--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "claims",
            "show me claims",
            "commitments",
            "open claims",
            "tracked claims",
        )
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="claims",
            args=("--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "calibration",
            "claim accuracy",
            "forecast bias",
            "forecast accuracy",
            "show forecast accuracy",
            "show me forecast accuracy",
            "how calibrated are we",
            "how accurate are our forecasts",
        )
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="calibration",
            args=("report", "--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "signals",
            "show signals",
            "show me signals",
            "unreviewed signals",
            "pending signals",
            "show me pending signals",
        )
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="signals",
            args=("--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "actions",
            "show actions",
            "show me actions",
            "open actions",
            "action items",
        )
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="actions",
            args=("--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "vitality",
            "program vitality",
            "freshness score",
            "program health",
            "how healthy is the program",
            "how healthy are we",
            "how fresh is the program",
        )
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="vitality",
            args=("--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(phrase in lowered for phrase in ("ado status", "show ado status", "ado diagnostics", "ado health")):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="ado",
            args=("status", "--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(phrase in lowered for phrase in ("ado reconcile", "show ado reconcile", "reconcile ado drift", "ado drift")):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="ado",
            args=("reconcile", "--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in ("storage stats", "show storage stats", "sqlite stats", "database stats", "how much storage do we have")
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="storage",
            args=("stats", "--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in ("storage check", "check storage health", "check storage integrity", "storage health", "how is storage doing")
    ):
        warnings.extend(_program_scope_warnings(request))
        return RoutedInvocation(
            command="storage",
            args=("check", "--program", _program_id_from_edition(default_edition)),
            warnings=tuple(dict.fromkeys(warnings)),
            prompt_version=None,
        )

    if "backfill" in lowered:
        source = "offline"
        if "hybrid" in lowered:
            source = "hybrid"
        elif "m365" in lowered or "workiq" in lowered:
            source = "m365"
        return RoutedInvocation(
            command="backfill",
            args=("--edition", default_edition, "--source", source, "--dry-run"),
            warnings=tuple(warnings),
            prompt_version=None,
        )

    if (
        "freshness" in lowered
        or "show me freshness" in lowered
        or "stale" in lowered
        or "how fresh is the data" in lowered
        or "how current is the data" in lowered
    ):
        return RoutedInvocation(
            command="freshness",
            args=("--edition", default_edition),
            warnings=tuple(warnings),
            prompt_version=None,
        )

    if any(
        phrase in lowered
        for phrase in (
            "doctor",
            "health check",
            "check health",
            "check the health",
            "are things healthy",
            "is everything healthy",
            "how healthy are things",
            "how healthy is everything",
            "is the edition healthy",
            "how healthy is the edition",
        )
    ):
        return RoutedInvocation(
            command="doctor",
            args=("--edition", default_edition),
            warnings=tuple(warnings),
            prompt_version=None,
        )

    return None


def _extract_issue_number(request: str) -> str | None:
    match = _ISSUE_PATTERN.search(request)
    return match.group(1) if match is not None else None


def _extract_ado_work_item_id(request: str) -> str | None:
    match = _ADO_WORK_ITEM_PATTERN.search(request)
    return match.group(1) if match is not None else None


def _extract_owner_pack_alias(request: str) -> str | None:
    match = _OWNER_PACK_FOR_PATTERN.search(request)
    if match is not None:
        return match.group(1).strip().lower() or None
    possessive_match = _OWNER_PACK_POSSESSIVE_PATTERN.search(request)
    if possessive_match is None:
        return None
    return possessive_match.group(1).strip().lower() or None


def _extract_transcript_or_meeting_id(request: str) -> str | None:
    transcript_match = _TRANSCRIPT_PATTERN.search(request)
    if transcript_match is not None:
        return transcript_match.group(1)
    meeting_match = _MEETING_ID_PATTERN.search(request)
    return meeting_match.group(1) if meeting_match is not None else None


def _unsupported_filter_warnings(request: str) -> tuple[str, ...]:
    warnings: list[str] = []
    for match in _ONLY_CLAUSE_PATTERN.finditer(request):
        clause = " ".join(match.group(1).split())
        warnings.append(f'Current CLI does not support scoped reruns for "{clause}"; routing the nearest full-command equivalent instead.')
    return tuple(dict.fromkeys(warnings))


def _conversational_scope_warnings(request: str) -> tuple[str, ...]:
    lowered = request.casefold()
    warnings: list[str] = []
    if "since" in lowered:
        warnings.append(
            "Current CLI does not support conversational time-scoped status queries; routing the nearest full-command equivalent instead."
        )
    if " on " in lowered or " for " in lowered:
        warnings.append(
            "Current CLI does not support conversational topic filters for status queries; routing the nearest full-command equivalent instead."
        )
    return tuple(dict.fromkeys(warnings))


def _program_scope_warnings(request: str) -> tuple[str, ...]:
    lowered = request.casefold()
    warnings: list[str] = []
    if " on " in lowered or " for " in lowered:
        warnings.append(
            "Current CLI does not support conversational topic filters for this request; routing the nearest program-level command instead."
        )
    return tuple(dict.fromkeys(warnings))


def _program_id_from_edition(edition: str) -> str:
    normalized = edition.strip()
    if not normalized:
        return ""
    return normalized.split("_", 1)[0]


def _load_prompt() -> str:
    return load_prompt(PROMPT_VERSION, error_factory=IntentRouterError)


def _build_user_prompt(*, request: str, default_edition: str) -> str:
    return "\n".join(
        [
            f"Default edition: {default_edition}",
            f"Natural-language request: {request}",
            "Return the safest existing Vertex CLI command for this request.",
        ]
    )


def _parse_routed_invocation(payload: dict[str, object]) -> RoutedInvocation:
    if not isinstance(payload, dict):
        raise IntentRouterError("Intent routing returned a non-object payload.")

    command = _required_string(payload.get("command"), "command")
    route_catalog = _intent_route_catalog()
    route_spec = route_catalog.get(command)
    if route_spec is None:
        raise IntentRouterError(f"Unsupported routed command: {command}")
    if "args" not in payload:
        raise IntentRouterError("Intent routing payload must include args as a list of strings.")
    raw_args = payload.get("args")
    if not isinstance(raw_args, list) or not all(isinstance(arg, str) for arg in raw_args):
        raise IntentRouterError("Intent routing args must be a list of strings.")
    args: list[str] = []
    for arg in raw_args:
        normalized = _sanitize_generated_string(arg, field_name="args")
        if not normalized:
            raise IntentRouterError("Intent routing args must contain non-empty strings only.")
        args.append(normalized)
    _validate_args(command=command, args=tuple(args), route_spec=route_spec)
    if "warnings" not in payload:
        raise IntentRouterError("Intent routing payload must include warnings as a list of strings.")
    raw_warnings = payload.get("warnings")
    if not isinstance(raw_warnings, list):
        raise IntentRouterError("Intent routing warnings must be a list of strings.")
    warnings: list[str] = []
    for warning in raw_warnings:
        if not isinstance(warning, str) or not warning.strip():
            raise IntentRouterError("Intent routing warnings must be a list of non-empty strings.")
        warnings.append(_sanitize_generated_string(warning, field_name="warnings"))
    return RoutedInvocation(
        command=command,
        args=tuple(args),
        warnings=tuple(warnings),
        prompt_version=PROMPT_VERSION,
    )


def _validate_args(*, command: str, args: tuple[str, ...], route_spec: IntentRouteCommandSpec | None = None) -> None:
    resolved_spec = route_spec or _intent_route_catalog().get(command)
    if resolved_spec is None:
        raise IntentRouterError(f"Unsupported routed command: {command}")
    if args and not args[0].startswith("--") and args[0] in resolved_spec.subcommands:
        subcommand = args[0]
        _validate_leaf_args(
            command=f"{command} {subcommand}",
            args=args[1:],
            leaf_spec=resolved_spec.subcommands[subcommand],
        )
        return
    if args and not args[0].startswith("--") and resolved_spec.subcommands and resolved_spec.callback is None:
        raise IntentRouterError(f"Unsupported subcommand for {command}: {args[0]}")
    if resolved_spec.callback is None:
        if args:
            raise IntentRouterError(f"Unsupported subcommand for {command}: {args[0]}")
        return
    _validate_leaf_args(command=command, args=args, leaf_spec=resolved_spec.callback)


def _validate_leaf_args(*, command: str, args: tuple[str, ...], leaf_spec: IntentRouteLeafSpec) -> None:
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("--"):
            raise IntentRouterError(f"Unsupported positional argument for {command}: {token}")
        if token in leaf_spec.flags:
            index += 1
            continue
        if token not in leaf_spec.value_options:
            raise IntentRouterError(f"Unsupported option for {command}: {token}")
        if index + 1 >= len(args):
            raise IntentRouterError(f"Option {token} for {command} requires a value.")
        next_token = args[index + 1]
        if not next_token or next_token.startswith("--"):
            raise IntentRouterError(f"Option {token} for {command} requires a value.")
        index += 2


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IntentRouterError(f"{field_name} must be a non-empty string.")
    return _sanitize_generated_string(value, field_name=field_name)


def _sanitize_generated_string(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        return ""
    try:
        processed = process_generated_text(normalized)
    except AIPipelineError as error:
        raise IntentRouterError(f"{field_name} rejected by safety pipeline: {error}") from error
    return processed.text.strip()
