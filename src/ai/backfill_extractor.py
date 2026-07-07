from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
import re
from typing import Any

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai.client import AIClientError
from src.ai.deployment_fallback import FallbackStructuredClient, LEGACY_DEPLOYMENT_ALIAS_NOTICE, resolve_ai_deployments_for_feature
from src.ai.llm_trace import AITraceContext
from src.ai.provider import LLMProvider
from src.ai.tiered_router import TierResult, route_through_tiers
from src.core.policy_loader import load_ai_feature_policy


PROMPT_VERSION = "backfill_extractor.v1"
from src.ai.prompt_registry import load_prompt
_FEATURE = "backfill_extractor"


class BackfillExtractorError(Exception):
    """Raised when newsletter backfill extraction cannot complete."""


@dataclass(frozen=True, slots=True)
class ExtractedDimensionRisk:
    scorecard_name: str | None
    dimension_name: str
    risk: str | None


@dataclass(frozen=True, slots=True)
class ExtractedWorkstreamBlurb:
    workstream_name: str
    summary: str


@dataclass(frozen=True, slots=True)
class ExtractedWritingStyleSample:
    executive_summary_paragraphs: tuple[str, ...]
    workstream_blurbs: tuple[str, ...]
    risk_framing_examples: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExtractedNewsletterIssue:
    source_path: str
    issue_number: int | None
    issue_date: str | None
    edition_type: str | None
    title: str | None
    executive_summary: str | None
    scorecard_dimensions: tuple[ExtractedDimensionRisk, ...]
    workstream_blurbs: tuple[ExtractedWorkstreamBlurb, ...]
    style_sample: ExtractedWritingStyleSample
    structural_notes: tuple[str, ...]
    prompt_version: str


class BackfillExtractor:
    """Extracts structured newsletter history from prior prose artifacts."""

    def __init__(self, *, client: LLMProvider | None = None) -> None:
        self._client = client

    @classmethod
    def from_environment(
        cls,
        *,
        trace_context: AITraceContext | None = None,
    ) -> "BackfillExtractor":
        if get_ai_mode() == AIMode.DISABLED:
            return cls()
        deployments = resolve_ai_deployments_for_feature(
            feature_name=_FEATURE,
            primary_candidates=(),
            backup_candidates=(),
            primary_fallback_envs=("VERTEX_AI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"),
            backup_fallback_envs=("VERTEX_AI_BACKUP_DEPLOYMENT",),
        )
        if not deployments:
            raise BackfillExtractorError(
                "VERTEX_AI_DEPLOYMENT or AZURE_OPENAI_DEPLOYMENT not set. "
                f"{LEGACY_DEPLOYMENT_ALIAS_NOTICE} Configure Azure OpenAI or continue with discovery-only backfill."
            )
        client = FallbackStructuredClient(
            deployments=deployments,
            temperature=load_ai_feature_policy(_FEATURE).temperature,
            budget_usd=0.5,
            trace_context=trace_context,
        )
        return cls(client=client)

    def extract_newsletter(self, source_path: Path) -> ExtractedNewsletterIssue:
        if not source_path.exists() or not source_path.is_file():
            raise BackfillExtractorError(f"Newsletter source not found: {source_path}")
        if get_ai_mode() == AIMode.DISABLED or self._client is None:
            return _empty_extracted_issue(source_path)

        system_prompt = _load_prompt()
        normalized_text = _normalize_newsletter_text(source_path)
        user_prompt = _build_user_prompt(source_path=source_path, normalized_text=normalized_text)
        try:
            client = self._client
            outcome = route_through_tiers(
                _FEATURE,
                deterministic_fn=lambda: None,
                frontier_fn=lambda: client.structured(
                    system_prompt,
                    user_prompt,
                    parser=lambda payload: _parse_extracted_issue(payload=payload, source_path=source_path),
                    max_tokens=load_ai_feature_policy(_FEATURE).max_tokens,
                    prompt_version=PROMPT_VERSION,
                ),
                policy=load_ai_feature_policy(_FEATURE),
            )
        except AIClientError as error:
            raise BackfillExtractorError(f"Newsletter extraction failed for {source_path.name}: {error}") from error
        if outcome.value is None:
            return _empty_extracted_issue(source_path)
        return outcome.value

    def extract_newsletters(self, source_paths: tuple[Path, ...]) -> tuple[ExtractedNewsletterIssue, ...]:
        return tuple(self.extract_newsletter(path) for path in source_paths)


def _load_prompt() -> str:
    return load_prompt(PROMPT_VERSION, error_factory=BackfillExtractorError)


def _empty_extracted_issue(source_path: Path) -> ExtractedNewsletterIssue:
    return ExtractedNewsletterIssue(
        source_path=str(source_path),
        issue_number=None,
        issue_date=None,
        edition_type=None,
        title=None,
        executive_summary=None,
        scorecard_dimensions=(),
        workstream_blurbs=(),
        style_sample=ExtractedWritingStyleSample(
            executive_summary_paragraphs=(),
            workstream_blurbs=(),
            risk_framing_examples=(),
        ),
        structural_notes=(),
        prompt_version=PROMPT_VERSION,
    )


def _build_user_prompt(*, source_path: Path, normalized_text: str) -> str:
    return "\n".join(
        [
            f"Source path: {source_path.name}",
            "Extract the structured newsletter history from the content below.",
            "Newsletter content:",
            normalized_text,
        ]
    )


def _parse_extracted_issue(*, payload: dict[str, object], source_path: Path) -> ExtractedNewsletterIssue:
    if not isinstance(payload, dict):
        raise BackfillExtractorError(f"Newsletter extraction returned a non-object payload for {source_path.name}.")

    for field_name in ("issue_number", "issue_date", "edition_type", "title", "executive_summary"):
        if field_name not in payload:
            raise BackfillExtractorError(
                f"Newsletter extraction must include {field_name} for {source_path.name}."
            )
    if "scorecard_dimensions" not in payload:
        raise BackfillExtractorError(f"Newsletter extraction must include scorecard_dimensions for {source_path.name}.")
    if "workstream_blurbs" not in payload:
        raise BackfillExtractorError(f"Newsletter extraction must include workstream_blurbs for {source_path.name}.")
    if "style_sample" not in payload:
        raise BackfillExtractorError(f"Newsletter extraction must include style_sample for {source_path.name}.")
    if "structural_notes" not in payload:
        raise BackfillExtractorError(f"Newsletter extraction must include structural_notes for {source_path.name}.")

    raw_dimensions = payload.get("scorecard_dimensions")
    raw_workstreams = payload.get("workstream_blurbs")
    raw_style = payload.get("style_sample")
    if not isinstance(raw_dimensions, list):
        raise BackfillExtractorError(f"scorecard_dimensions must be a list for {source_path.name}.")
    if not isinstance(raw_workstreams, list):
        raise BackfillExtractorError(f"workstream_blurbs must be a list for {source_path.name}.")
    if not isinstance(raw_style, dict):
        raise BackfillExtractorError(f"style_sample must be an object for {source_path.name}.")
    if "executive_summary_paragraphs" not in raw_style:
        raise BackfillExtractorError(
            f"style_sample must include executive_summary_paragraphs for {source_path.name}."
        )
    if "workstream_blurbs" not in raw_style:
        raise BackfillExtractorError(f"style_sample must include workstream_blurbs for {source_path.name}.")
    if "risk_framing_examples" not in raw_style:
        raise BackfillExtractorError(f"style_sample must include risk_framing_examples for {source_path.name}.")

    dimensions = tuple(_parse_dimension(entry) for entry in raw_dimensions)
    workstreams = tuple(_parse_workstream(entry) for entry in raw_workstreams)
    style_sample = ExtractedWritingStyleSample(
        executive_summary_paragraphs=_parse_string_list(
            raw_style.get("executive_summary_paragraphs"),
            field_name="style_sample.executive_summary_paragraphs",
        ),
        workstream_blurbs=_parse_string_list(
            raw_style.get("workstream_blurbs"),
            field_name="style_sample.workstream_blurbs",
        ),
        risk_framing_examples=_parse_string_list(
            raw_style.get("risk_framing_examples"),
            field_name="style_sample.risk_framing_examples",
        ),
    )

    issue_number = payload.get("issue_number")
    expected_issue_number = _expected_issue_number_from_source_path(source_path)
    if issue_number is not None:
        if isinstance(issue_number, bool):
            raise BackfillExtractorError(f"issue_number must be an integer for {source_path.name}.")
        if not isinstance(issue_number, (int, str)):
            raise BackfillExtractorError(f"issue_number must be an integer for {source_path.name}.")
        try:
            issue_number = int(issue_number)
        except (TypeError, ValueError) as error:
            raise BackfillExtractorError(f"issue_number must be an integer for {source_path.name}.") from error
    if expected_issue_number is not None:
        if issue_number is None:
            issue_number = expected_issue_number
        elif issue_number != expected_issue_number:
            raise BackfillExtractorError(
                f"issue_number must match source path issue {expected_issue_number} for {source_path.name}."
            )

    return ExtractedNewsletterIssue(
        source_path=str(source_path),
        issue_number=issue_number,
        issue_date=_optional_extracted_string(payload.get("issue_date"), field_name="issue_date"),
        edition_type=_optional_extracted_string(payload.get("edition_type"), field_name="edition_type"),
        title=_optional_extracted_string(payload.get("title"), field_name="title"),
        executive_summary=_optional_extracted_string(payload.get("executive_summary"), field_name="executive_summary"),
        scorecard_dimensions=dimensions,
        workstream_blurbs=workstreams,
        style_sample=style_sample,
        structural_notes=_parse_string_list(payload.get("structural_notes"), field_name="structural_notes"),
        prompt_version=PROMPT_VERSION,
    )


def _parse_dimension(payload: dict[str, Any]) -> ExtractedDimensionRisk:
    if not isinstance(payload, dict):
        raise BackfillExtractorError("scorecard_dimensions entries must be objects.")
    if "scorecard_name" not in payload:
        raise BackfillExtractorError("scorecard_dimensions entries must include scorecard_name.")
    if "dimension_name" not in payload:
        raise BackfillExtractorError("scorecard_dimensions entries must include dimension_name.")
    dimension_name = _optional_string(payload.get("dimension_name"))
    if dimension_name is None:
        raise BackfillExtractorError("scorecard_dimensions entries must include dimension_name.")
    if "risk" not in payload:
        raise BackfillExtractorError("scorecard_dimensions entries must include risk.")
    return ExtractedDimensionRisk(
        scorecard_name=_optional_extracted_string(payload.get("scorecard_name"), field_name="scorecard_name"),
        dimension_name=dimension_name,
        risk=_optional_extracted_string(payload.get("risk"), field_name="risk"),
    )


def _parse_workstream(payload: dict[str, Any]) -> ExtractedWorkstreamBlurb:
    if not isinstance(payload, dict):
        raise BackfillExtractorError("workstream_blurbs entries must be objects.")
    if "workstream_name" not in payload:
        raise BackfillExtractorError("workstream_blurbs entries must include workstream_name.")
    if "summary" not in payload:
        raise BackfillExtractorError("workstream_blurbs entries must include summary.")
    workstream_name = _optional_string(payload.get("workstream_name"))
    summary = _optional_string(payload.get("summary"))
    if workstream_name is None or summary is None:
        raise BackfillExtractorError("workstream_blurbs entries must include workstream_name and summary.")
    return ExtractedWorkstreamBlurb(
        workstream_name=workstream_name,
        summary=summary,
    )


def _parse_string_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise BackfillExtractorError(f"{field_name} must be a list.")
    items: list[str] = []
    for entry in value:
        text = _optional_string(entry)
        if text is None:
            raise BackfillExtractorError(f"{field_name} entries must be non-empty strings.")
        items.append(text)
    return tuple(items)


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        processed = process_generated_text(stripped)
    except AIPipelineError as error:
        raise BackfillExtractorError(f"Generated text rejected by safety pipeline: {error}") from error
    return processed.text or None


def _optional_extracted_string(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    text = _optional_string(value)
    if text is None:
        raise BackfillExtractorError(f"{field_name} must be a string when provided.")
    return text


def _normalize_newsletter_text(source_path: Path) -> str:
    raw_text = source_path.read_text(encoding="utf-8")
    if source_path.suffix.lower() in {".html", ".htm", ".eml"}:
        parser = _HTMLTextExtractor()
        parser.feed(raw_text)
        parser.close()
        return parser.get_text()
    return raw_text.strip()


def _expected_issue_number_from_source_path(source_path: Path) -> int | None:
    match = re.search(r"\bissue_(\d+)\b", source_path.stem, flags=re.IGNORECASE)
    if match is None:
        return None
    return int(match.group(1))


class _HTMLTextExtractor(HTMLParser):
    _BLOCK_TAGS = {
        "article",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "ol",
        "p",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.lower()
        if normalized in {"script", "style"}:
            self._skip_depth += 1
            return
        if normalized in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if normalized in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        text = data.strip()
        if text:
            self._parts.append(text)

    def get_text(self) -> str:
        joined = " ".join(part for part in self._parts if part)
        lines = [" ".join(segment.split()) for segment in joined.splitlines()]
        normalized_lines = [line for line in lines if line]
        return "\n".join(normalized_lines)
