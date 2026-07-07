from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, cast

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.client import AIClientError
from src.ai.deployment_fallback import FallbackStructuredClient, LEGACY_DEPLOYMENT_ALIAS_NOTICE, resolve_ai_deployments_for_feature
from src.ai.draft_reviewer import ReviewSuggestion, ReviewSuggestionOutcome, SuggestionTrackingReport
from src.ai.llm_trace import AITraceContext
from src.ai.tiered_router import TierResult, route_through_tiers
from src.core.config_loader import EditorialRules
from src.core.models import Confidence
from src.core.policy_loader import load_ai_feature_policy


PROMPT_VERSION = "learning_distiller.v1"
from src.ai.prompt_registry import load_prompt
_FEATURE = "learning_distiller"
_APPEND_TARGETS = {"banned_phrases", "banned_openings"}
_TRACKING_SUGGESTION_CATEGORIES = {"data_gap", "leadership_question", "cross_issue", "structural"}
_TRACKING_OUTCOMES = {"accepted", "dismissed"}
_TRACKING_FILENAME_PATTERN = re.compile(r"issue_(\d+)\.review_tracking\.json$")
_SET_TARGETS = {
    "verbosity.workstream_blurb_max_sentences",
    "verbosity.workstream_blurb_max_words",
    "verbosity.exec_bullet_max_words",
    "verbosity.exec_max_bullets",
    "verbosity.scorecard_summary_max_sentences",
}


class LearningDistillerError(Exception):
    """Raised when continuous-learning distillation cannot complete."""


@dataclass(frozen=True, slots=True)
class EditorialRuleProposal:
    target: str
    action: str
    value: str | int
    rationale: str
    supporting_issue_numbers: tuple[int, ...]
    supporting_examples: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LearningDistillation:
    tracked_issue_numbers: tuple[int, ...]
    proposals: tuple[EditorialRuleProposal, ...]
    prompt_version: str


class ReviewTrackingStore(Protocol):
    def read_reports(self) -> tuple[SuggestionTrackingReport, ...]: ...


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


class FileReviewTrackingStore:
    def __init__(self, edition_output_dir: Path) -> None:
        self._edition_output_dir = edition_output_dir

    def read_reports(self) -> tuple[SuggestionTrackingReport, ...]:
        reports: list[SuggestionTrackingReport] = []
        for path in sorted(self._edition_output_dir.glob("issue_*/issue_*.review_tracking.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as error:
                raise LearningDistillerError(f"Tracking payload at {path} is invalid.") from error
            if not isinstance(payload, dict):
                raise LearningDistillerError(f"Tracking payload at {path} is invalid.")
            expected_issue_number = _issue_number_from_tracking_path(path)
            if expected_issue_number is None:
                raise LearningDistillerError(f"Tracking payload path must use canonical issue_NNN.review_tracking.json format: {path}.")
            report = tracking_report_from_payload(payload)
            if report.issue_number != expected_issue_number:
                raise LearningDistillerError(
                    f"Tracking payload issue_number must match path issue {expected_issue_number} at {path}."
                )
            reports.append(report)
        return tuple(reports)


class LearningDistiller:
    """Distills repeated accepted author corrections into editorial-rule proposals."""

    def __init__(self, *, client: _StructuredProvider | None) -> None:
        self._client = client

    @classmethod
    def from_environment(
        cls,
        *,
        trace_context: AITraceContext | None = None,
    ) -> "LearningDistiller":
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
            raise LearningDistillerError(
                "VERTEX_AI_DEPLOYMENT or AZURE_OPENAI_DEPLOYMENT not set. "
                f"{LEGACY_DEPLOYMENT_ALIAS_NOTICE} Configure Azure OpenAI or skip learning distillation."
            )
        client = FallbackStructuredClient(
            deployments=deployments,
            temperature=load_ai_feature_policy(_FEATURE).temperature,
            budget_usd=0.5,
            trace_context=trace_context,
        )
        return cls(client=client)

    def distill(
        self,
        *,
        editorial_rules: EditorialRules,
        tracking_reports: tuple[SuggestionTrackingReport, ...],
    ) -> LearningDistillation:
        tracked_issue_numbers = tuple(report.issue_number for report in tracking_reports)
        if get_ai_mode() == AIMode.DISABLED or self._client is None:
            return LearningDistillation(
                tracked_issue_numbers=tracked_issue_numbers,
                proposals=(),
                prompt_version=PROMPT_VERSION,
            )
        if not tracking_reports:
            return LearningDistillation(tracked_issue_numbers=(), proposals=(), prompt_version=PROMPT_VERSION)

        system_prompt = _load_prompt()
        user_prompt = _build_user_prompt(editorial_rules=editorial_rules, tracking_reports=tracking_reports)
        try:
            client = self._client
            outcome = route_through_tiers(
                _FEATURE,
                deterministic_fn=lambda: None,
                frontier_fn=lambda: client.structured(
                    system_prompt,
                    user_prompt,
                    parser=lambda payload: _parse_proposals(
                        payload=payload,
                        editorial_rules=editorial_rules,
                        valid_issue_numbers=tracked_issue_numbers,
                    ),
                    max_tokens=load_ai_feature_policy(_FEATURE).max_tokens,
                    prompt_version=PROMPT_VERSION,
                ),
                policy=load_ai_feature_policy(_FEATURE),
            )
        except AIClientError as error:
            raise LearningDistillerError(f"Learning distillation failed: {error}") from error
        proposals = outcome.value if outcome.value is not None else ()
        return LearningDistillation(
            tracked_issue_numbers=tracked_issue_numbers,
            proposals=proposals,
            prompt_version=PROMPT_VERSION,
        )


def build_review_tracking_store(edition_output_dir: Path) -> ReviewTrackingStore:
    return FileReviewTrackingStore(edition_output_dir)


def load_tracking_reports(source: Path | ReviewTrackingStore) -> tuple[SuggestionTrackingReport, ...]:
    if isinstance(source, Path):
        return build_review_tracking_store(source).read_reports()
    return source.read_reports()


def _issue_number_from_tracking_path(path: Path) -> int | None:
    match = _TRACKING_FILENAME_PATTERN.search(path.name)
    if match is None:
        return None
    return int(match.group(1))


def tracking_report_from_payload(payload: dict[str, Any]) -> SuggestionTrackingReport:
    if not isinstance(payload, dict):
        raise LearningDistillerError("Tracking payload must be an object.")
    if "issue_number" not in payload:
        raise LearningDistillerError("Tracking payload must include issue_number as an integer.")
    if "suggestions" not in payload:
        raise LearningDistillerError("Tracking payload must include suggestions as a list.")
    if "accepted" not in payload:
        raise LearningDistillerError("Tracking payload must include accepted as an integer.")
    if "dismissed" not in payload:
        raise LearningDistillerError("Tracking payload must include dismissed as an integer.")
    raw_suggestions = payload.get("suggestions")
    if not isinstance(raw_suggestions, list):
        raise LearningDistillerError("Tracking payload suggestions must be a list.")
    suggestions: list[ReviewSuggestionOutcome] = []
    for entry in raw_suggestions:
        if not isinstance(entry, dict):
            raise LearningDistillerError("Tracking payload suggestions[] must be an object.")
        suggestions.append(_tracking_outcome_from_payload(entry))
    issue_number = _coerce_tracking_int(payload.get("issue_number"), field_name="issue_number")
    expected_counts = {
        "accepted": sum(1 for suggestion in suggestions if suggestion.outcome == "accepted"),
        "dismissed": sum(1 for suggestion in suggestions if suggestion.outcome == "dismissed"),
    }
    actual_counts = {
        "accepted": _coerce_tracking_int(payload.get("accepted"), field_name="accepted"),
        "dismissed": _coerce_tracking_int(payload.get("dismissed"), field_name="dismissed"),
    }
    mismatched_counts = [
        key for key in ("accepted", "dismissed") if actual_counts[key] != expected_counts[key]
    ]
    if mismatched_counts:
        raise LearningDistillerError(
            "Tracking payload counts do not match suggestion outcomes: "
            + ", ".join(
                f"{key}={actual_counts[key]} expected {expected_counts[key]}" for key in mismatched_counts
            )
        )
    return SuggestionTrackingReport(
        issue_number=issue_number,
        accepted=actual_counts["accepted"],
        dismissed=actual_counts["dismissed"],
        suggestions=tuple(suggestions),
    )


def _coerce_tracking_int(value: Any, *, field_name: str, message_prefix: str = "Tracking payload ") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LearningDistillerError(f"{message_prefix}{field_name} must be an integer.")
    return value


def render_learning_markdown(distillation: LearningDistillation) -> str:
    lines = [
        "# AI LEARNING DISTILLATION",
        "",
        f"- Prompt version: `{distillation.prompt_version}`",
        f"- Tracked issues: `{', '.join(f'{issue:03d}' for issue in distillation.tracked_issue_numbers) if distillation.tracked_issue_numbers else 'none'}`",
        f"- Proposed rule updates: `{len(distillation.proposals)}`",
    ]
    if not distillation.proposals:
        lines.extend(["", "No durable editorial-rule updates were proposed from the tracked author corrections."])
        return "\n".join(lines) + "\n"

    lines.extend(["", "## Proposed rule updates"])
    for proposal in distillation.proposals:
        value = proposal.value if isinstance(proposal.value, int) else f"`{proposal.value}`"
        lines.extend(
            [
                "",
                f"- `{proposal.action}` `{proposal.target}` -> {value}",
                f"  Rationale: {proposal.rationale}",
                f"  Supporting issues: {', '.join(f'{issue:03d}' for issue in proposal.supporting_issue_numbers) or 'none'}",
            ]
        )
        for example in proposal.supporting_examples:
            lines.append(f"  Example: {example}")
    lines.append("")
    return "\n".join(lines)


def render_learning_summary(distillation: LearningDistillation) -> str:
    return (
        f"AI learning distillation: {len(distillation.proposals)} proposed rule update(s) "
        f"from {len(distillation.tracked_issue_numbers)} tracked issue(s)."
    )


def _load_prompt() -> str:
    return load_prompt(PROMPT_VERSION, error_factory=LearningDistillerError)


def _build_user_prompt(
    *,
    editorial_rules: EditorialRules,
    tracking_reports: tuple[SuggestionTrackingReport, ...],
) -> str:
    current_rules = {
        "banned_phrases": list(editorial_rules.banned_phrases),
        "banned_openings": list(editorial_rules.banned_openings),
        "verbosity": {
            "workstream_blurb_max_sentences": editorial_rules.verbosity.workstream_blurb_max_sentences,
            "workstream_blurb_max_words": editorial_rules.verbosity.workstream_blurb_max_words,
            "exec_bullet_max_words": editorial_rules.verbosity.exec_bullet_max_words,
            "exec_max_bullets": editorial_rules.verbosity.exec_max_bullets,
            "scorecard_summary_max_sentences": editorial_rules.verbosity.scorecard_summary_max_sentences,
        },
    }
    serialized_reports = [
        {
            "issue_number": report.issue_number,
            "accepted": report.accepted,
            "dismissed": report.dismissed,
            "suggestions": [
                {
                    "index": outcome.index,
                    "category": outcome.suggestion.category,
                    "section_id": outcome.suggestion.section_id,
                    "suggestion_text": outcome.suggestion.suggestion_text,
                    "action": outcome.suggestion.action,
                    "outcome": outcome.outcome,
                    "reason": outcome.reason,
                }
                for outcome in report.suggestions
            ],
        }
        for report in tracking_reports
    ]
    return "\n".join(
        [
            "Current editorial rules:",
            json.dumps(current_rules, indent=2),
            "",
            "Tracked author-correction history:",
            json.dumps(serialized_reports, indent=2),
        ]
    )


def _parse_proposals(
    *,
    payload: dict[str, object],
    editorial_rules: EditorialRules,
    valid_issue_numbers: tuple[int, ...],
) -> tuple[EditorialRuleProposal, ...]:
    if not isinstance(payload, dict):
        raise LearningDistillerError("Learning distillation returned a non-object payload.")

    if "proposals" not in payload:
        raise LearningDistillerError("Learning distillation payload must contain a proposals list.")

    raw_proposals = payload.get("proposals")
    if not isinstance(raw_proposals, list):
        raise LearningDistillerError("Learning distillation payload must contain a proposals list.")

    proposals: list[EditorialRuleProposal] = []
    seen_keys: set[tuple[str, str, str | int]] = set()
    for raw_proposal in raw_proposals:
        if not isinstance(raw_proposal, dict):
            raise LearningDistillerError("Learning distillation proposals[] must be an object.")
        proposal = _proposal_from_payload(raw_proposal, valid_issue_numbers=valid_issue_numbers)
        if _proposal_matches_existing_rules(proposal, editorial_rules):
            continue
        dedupe_key = (proposal.target, proposal.action, proposal.value)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        proposals.append(proposal)
    return tuple(proposals)


def _proposal_from_payload(
    payload: dict[str, Any],
    *,
    valid_issue_numbers: tuple[int, ...] = (),
) -> EditorialRuleProposal:
    if not isinstance(payload, dict):
        raise LearningDistillerError("proposal payload must be an object.")
    if "target" not in payload:
        raise LearningDistillerError("proposal payload must include target.")
    if "action" not in payload:
        raise LearningDistillerError("proposal payload must include action.")
    if "rationale" not in payload:
        raise LearningDistillerError("proposal payload must include rationale.")
    if "value" not in payload:
        raise LearningDistillerError("proposal payload must include value.")
    target = _required_string(payload.get("target"), "proposal.target")
    action = _required_string(payload.get("action"), "proposal.action")
    rationale = _required_ai_string(payload.get("rationale"), "proposal.rationale")
    if target not in _APPEND_TARGETS | _SET_TARGETS:
        raise LearningDistillerError(f"Unsupported editorial-rule target: {target}")
    if target in _APPEND_TARGETS and action != "append":
        raise LearningDistillerError(f"Target {target} only supports append actions.")
    if target in _SET_TARGETS and action != "set":
        raise LearningDistillerError(f"Target {target} only supports set actions.")

    raw_value = payload.get("value")
    value: str | int
    if target in _APPEND_TARGETS:
        value = _required_ai_rule_string(raw_value, "proposal.value")
    else:
        if isinstance(raw_value, bool) or not isinstance(raw_value, int) or raw_value <= 0:
            raise LearningDistillerError(f"Target {target} requires a positive integer value.")
        value = raw_value

    if "supporting_issue_numbers" not in payload:
        raise LearningDistillerError("proposal payload must include supporting_issue_numbers as a list.")
    raw_issue_numbers = payload.get("supporting_issue_numbers")
    if not isinstance(raw_issue_numbers, list):
        raise LearningDistillerError("proposal.supporting_issue_numbers must be a list.")
    supporting_issue_numbers = tuple(
        _coerce_learning_int(issue_number, field_name="proposal.supporting_issue_numbers[]")
        for issue_number in raw_issue_numbers
    )
    if valid_issue_numbers:
        allowed_issue_numbers = set(valid_issue_numbers)
        unknown_issue_numbers = tuple(
            issue_number for issue_number in supporting_issue_numbers if issue_number not in allowed_issue_numbers
        )
        if unknown_issue_numbers:
            raise LearningDistillerError(
                "proposal.supporting_issue_numbers contains unknown tracked issues: "
                + ", ".join(str(issue_number) for issue_number in unknown_issue_numbers)
            )
    if "supporting_examples" not in payload:
        raise LearningDistillerError("proposal payload must include supporting_examples as a list.")
    supporting_examples = _ai_string_list(payload.get("supporting_examples"), field_name="proposal.supporting_examples")
    return EditorialRuleProposal(
        target=target,
        action=action,
        value=value,
        rationale=rationale,
        supporting_issue_numbers=supporting_issue_numbers,
        supporting_examples=supporting_examples,
    )


def _proposal_matches_existing_rules(proposal: EditorialRuleProposal, editorial_rules: EditorialRules) -> bool:
    if proposal.target == "banned_phrases":
        return str(proposal.value) in set(editorial_rules.banned_phrases)
    if proposal.target == "banned_openings":
        return str(proposal.value) in set(editorial_rules.banned_openings)
    if proposal.target == "verbosity.workstream_blurb_max_sentences":
        return proposal.value == editorial_rules.verbosity.workstream_blurb_max_sentences
    if proposal.target == "verbosity.workstream_blurb_max_words":
        return proposal.value == editorial_rules.verbosity.workstream_blurb_max_words
    if proposal.target == "verbosity.exec_bullet_max_words":
        return proposal.value == editorial_rules.verbosity.exec_bullet_max_words
    if proposal.target == "verbosity.exec_max_bullets":
        return proposal.value == editorial_rules.verbosity.exec_max_bullets
    if proposal.target == "verbosity.scorecard_summary_max_sentences":
        return proposal.value == editorial_rules.verbosity.scorecard_summary_max_sentences
    return False


def _tracking_outcome_from_payload(payload: dict[str, Any]) -> ReviewSuggestionOutcome:
    if not isinstance(payload, dict):
        raise LearningDistillerError("tracking outcome payload must be an object.")
    if "index" not in payload:
        raise LearningDistillerError("tracking outcome payload must include index as an integer.")
    if "suggestion" not in payload:
        raise LearningDistillerError("tracking outcome payload must include suggestion as an object.")
    suggestion_payload = payload.get("suggestion")
    if not isinstance(suggestion_payload, dict):
        raise LearningDistillerError("tracking suggestion payload must be an object.")
    if "category" not in suggestion_payload:
        raise LearningDistillerError("tracking suggestion payload must include category as a string.")
    if "section_id" not in suggestion_payload:
        raise LearningDistillerError("tracking suggestion payload must include section_id as a string.")
    if "suggestion_text" not in suggestion_payload:
        raise LearningDistillerError("tracking suggestion payload must include suggestion_text as a string.")
    if "confidence" not in suggestion_payload:
        raise LearningDistillerError("tracking suggestion payload must include confidence as a string.")
    if "outcome" not in payload:
        raise LearningDistillerError("tracking outcome payload must include outcome as a string.")
    if "reason" not in payload:
        raise LearningDistillerError("tracking outcome payload must include reason as a string.")
    return ReviewSuggestionOutcome(
        index=_coerce_tracking_int(payload.get("index"), field_name="tracking.index", message_prefix=""),
        suggestion=ReviewSuggestion(
            category=_coerce_tracking_category(suggestion_payload.get("category")),
            section_id=_required_string(suggestion_payload.get("section_id"), "suggestion.section_id"),
            suggestion_text=_required_ai_string(suggestion_payload.get("suggestion_text"), "suggestion.suggestion_text"),
            confidence=_coerce_tracking_confidence(suggestion_payload.get("confidence")),
            reader_name=_optional_ai_string(suggestion_payload.get("reader_name"), "suggestion.reader_name"),
            action=_optional_ai_string(suggestion_payload.get("action"), "suggestion.action"),
        ),
        outcome=_coerce_tracking_outcome(payload.get("outcome")),
        reason=_required_ai_string(payload.get("reason"), "tracking.reason"),
    )


def _string_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise LearningDistillerError(f"{field_name} must be a list.")
    items: list[str] = []
    for entry in value:
        text = _optional_tracking_string(entry, f"{field_name}[]")
        if text is None:
            raise LearningDistillerError(f"{field_name}[] must be a non-empty string.")
        items.append(text)
    return tuple(items)


def _coerce_learning_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise LearningDistillerError(f"{field_name} must be an integer.")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise LearningDistillerError(f"{field_name} must be an integer.") from error


def _coerce_tracking_confidence(value: Any) -> Confidence:
    text = _required_string(value, "suggestion.confidence")
    try:
        return Confidence(text.lower())
    except ValueError as error:
        raise LearningDistillerError("suggestion.confidence must be a valid confidence value.") from error


def _coerce_tracking_category(value: Any) -> Literal["data_gap", "leadership_question", "cross_issue", "structural"]:
    text = _required_string(value, "suggestion.category")
    if text not in _TRACKING_SUGGESTION_CATEGORIES:
        raise LearningDistillerError("suggestion.category must be a valid suggestion category.")
    return cast(Literal["data_gap", "leadership_question", "cross_issue", "structural"], text)


def _coerce_tracking_outcome(value: Any) -> Literal["accepted", "dismissed"]:
    text = _required_string(value, "tracking.outcome")
    if text not in _TRACKING_OUTCOMES:
        raise LearningDistillerError("tracking.outcome must be a valid tracking outcome.")
    return cast(Literal["accepted", "dismissed"], text)


def _required_string(value: Any, field_name: str) -> str:
    text = _optional_string(value)
    if text is None:
        raise LearningDistillerError(f"{field_name} must be a non-empty string.")
    return text


def _required_ai_string(value: Any, field_name: str) -> str:
    text = _optional_ai_string(value, field_name)
    if text is None:
        raise LearningDistillerError(f"{field_name} must be a non-empty string.")
    return text


def _required_ai_rule_string(value: Any, field_name: str) -> str:
    text = _optional_ai_rule_string(value, field_name)
    if text is None:
        raise LearningDistillerError(f"{field_name} must be a non-empty string.")
    return text


def _optional_tracking_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LearningDistillerError(f"{field_name} must be a string when provided.")
    stripped = value.strip()
    return stripped or None


def _optional_ai_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LearningDistillerError(f"{field_name} must be a string when provided.")
    stripped = value.strip()
    if not stripped:
        return None
    try:
        processed = process_generated_text(stripped)
    except AIPipelineError as error:
        raise LearningDistillerError(f"{field_name} rejected by safety pipeline: {error}") from error
    return processed.text or None


def _optional_ai_rule_string(value: Any, field_name: str) -> str | None:
    """Route an AI-generated rule string through the full safety pipeline.

    D-26: must go through ``process_generated_text`` (PII scrub +
    injection detect + causality sanitize) — not a hand-rolled PII+injection
    pair that omits causality sanitization. The text is free-form
    (no work-item grounding required), so ``allowed_items`` is left
    empty and grounding short-circuits inside the pipeline.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise LearningDistillerError(f"{field_name} must be a string when provided.")
    stripped = value.strip()
    if not stripped:
        return None
    try:
        processed = process_generated_text(stripped)
    except AIPipelineError as error:
        raise LearningDistillerError(f"{field_name} rejected by safety pipeline: {error}") from error
    return processed.text or None


def _ai_string_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise LearningDistillerError(f"{field_name} must be a list.")
    items: list[str] = []
    for entry in value:
        text = _optional_ai_string(entry, f"{field_name}[]")
        if text is None:
            raise LearningDistillerError(f"{field_name}[] must be a non-empty string.")
        items.append(text)
    return tuple(items)


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
