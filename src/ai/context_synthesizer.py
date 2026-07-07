"""NCFL Phase 5 — Zone B knowledge-document synthesis (§24.6).

This is the **only** Zone B surface in the NCFL pipeline. It reads:

  - accepted NCFL proposals (operator-confirmed Plane 1 updates), and
  - the published narrative markdown for an issue,

and asks an LLM to draft a proposed patch for the program-context knowledge
document (``knowledge/<program>_program_context.md``). The output is **always a
``ContextUpdateProposal``** with ``target_store=knowledge_doc``; it is never
auto-applied. The operator reviews it via ``vertex context proposals`` and
applies it via ``vertex context apply`` (§24.4), reusing the same NCFL apply
engine as the Zone A proposals.

Guardrails (§24.6):
  - Available only when ≥1 Zone A proposal is accepted for the issue.
  - Output is always a proposal, never a mutation.
  - Ban-list enforcement runs before staging (A-NC-7).
  - Zone B only: this module lives in ``src/ai/`` and must never be reached from
    the post-confirm hook (INV / §25.2.1) — only via explicit
    ``vertex context synthesize``.

The synthesizer is **degrading**: when no LLM is configured or the frontier is
blocked (AIMode.DISABLED / budget), it returns ``None`` rather than raising, so
the CLI can surface a clean "synthesis unavailable" message. It never blocks.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.client import AIClientError
from src.ai.prompt_registry import load_prompt
from src.ai.provider import LLMProvider
from src.ai.tiered_router import route_through_tiers
from src.core.ncfl_models import (
    EXTRACTION_METHOD_CONFIDENCE,
    NCFL_EXTRACTOR_VERSION,
    ContextUpdateProposal,
)
from src.core.ncfl_store_policy import is_ncfl_target_store
from src.core.policy_loader import load_ai_feature_policy
from src.core.published_narrative_store import load_published_narratives

PROMPT_VERSION = "context_synthesizer.v1"
_FEATURE = "context_synthesizer"
KNOWLEDGE_DOC_DEFAULT_NAME = "xpf_program_context.md"


class ContextSynthesizerError(Exception):
    """Raised when knowledge-doc synthesis cannot complete safely (parse/contract)."""


@dataclass(frozen=True, slots=True)
class SynthesisInputs:
    """Grounding inputs gathered before the LLM call (Zone A read)."""

    program_id: str
    edition_id: str
    issue_number: int
    accepted_proposals: tuple[ContextUpdateProposal, ...]
    published_narrative: str
    knowledge_doc_name: str
    knowledge_doc_path: Path
    current_knowledge_doc: str | None


@dataclass(frozen=True, slots=True)
class KnowledgeDocDraft:
    """The parsed, sanitized synthesis payload (pre-ban-list)."""

    summary: str
    highlights: tuple[str, ...]
    open_risks: tuple[str, ...]
    next_milestones: tuple[str, ...]
    as_of_date: str

    def render_markdown(self) -> str:
        """Render the draft as a markdown patch for the knowledge doc."""
        lines = [
            f"# Program Context (as of {self.as_of_date})",
            "",
            self.summary,
            "",
        ]
        if self.highlights:
            lines.append("## Highlights")
            for item in self.highlights:
                lines.append(f"- {item}")
            lines.append("")
        if self.open_risks:
            lines.append("## Open Risks")
            for item in self.open_risks:
                lines.append(f"- {item}")
            lines.append("")
        if self.next_milestones:
            lines.append("## Next Milestones / ETAs")
            for item in self.next_milestones:
                lines.append(f"- {item}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    """Outcome of a synthesis attempt."""

    proposal: ContextUpdateProposal | None
    draft: KnowledgeDocDraft | None
    note: str

    @property
    def available(self) -> bool:
        return self.proposal is not None and self.draft is not None


class ContextSynthesizer:
    """Zone B knowledge-doc synthesis engine.

    Construct with a configured ``LLMProvider`` (frontier) or a
    ``DisabledStructuredProvider`` (degrades to ``None``).
    """

    def __init__(self, *, client: LLMProvider) -> None:
        self._client = client

    def synthesize(self, inputs: SynthesisInputs) -> SynthesisResult:
        # Guardrail 1 (§24.6): require ≥1 accepted Zone A proposal.
        if not inputs.accepted_proposals:
            return SynthesisResult(
                proposal=None,
                draft=None,
                note="no accepted Zone A proposals — Zone B synthesis unavailable",
            )

        # Guardrail 2 (§24.6): published narrative must carry some signal.
        if not inputs.published_narrative.strip():
            return SynthesisResult(
                proposal=None,
                draft=None,
                note="no published narrative for the issue — nothing to synthesize from",
            )

        try:
            outcome = route_through_tiers(
                _FEATURE,
                deterministic_fn=lambda: None,
                frontier_fn=lambda: self._client.structured(
                    load_prompt(PROMPT_VERSION, error_factory=ContextSynthesizerError),
                    _build_user_prompt(inputs),
                    parser=lambda payload: _parse_payload(payload, issue_number=inputs.issue_number),
                    max_tokens=load_ai_feature_policy(_FEATURE).max_tokens,
                    prompt_version=PROMPT_VERSION,
                ),
                policy=load_ai_feature_policy(_FEATURE),
            )
        except AIClientError as error:
            # Degrade, never raise: synthesis is advisory.
            return SynthesisResult(
                proposal=None,
                draft=None,
                note=f"synthesis frontier unavailable: {error}",
            )
        except ContextSynthesizerError as error:
            return SynthesisResult(
                proposal=None,
                draft=None,
                note=f"synthesis parse/contract error: {error}",
            )

        if outcome.value is None:
            return SynthesisResult(
                proposal=None,
                draft=None,
                note="synthesis skipped (frontier blocked or no lower-tier hit)",
            )

        draft = outcome.value
        return SynthesisResult(
            proposal=_build_knowledge_doc_proposal(inputs, draft),
            draft=draft,
            note="synthesis produced a knowledge_doc proposal",
        )


def build_synthesizer_from_client(client: LLMProvider) -> ContextSynthesizer:
    return ContextSynthesizer(client=client)


# ---------------------------------------------------------------------------
# Input gathering (Zone A reads)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _GatherConfig:
    knowledge_doc_name: str
    programs_root: Path
    archive_root: Path


def gather_synthesis_inputs(
    *,
    program_id: str,
    edition_id: str,
    issue_number: int,
    accepted_proposals: tuple[ContextUpdateProposal, ...],
    programs_root: Path,
    archive_root: Path | None = None,
    knowledge_doc_name: str = KNOWLEDGE_DOC_DEFAULT_NAME,
) -> SynthesisInputs:
    """Assemble the Zone A grounding inputs for synthesis.

    Reads the published narrative markdown for the issue and the current
    knowledge-doc content (if any). The accepted proposals are supplied by the
    caller (loaded from the NCFL proposal store).
    """
    from src.core.snapshot_store import ARCHIVE_ROOT

    resolved_archive = archive_root or ARCHIVE_ROOT
    narratives = load_published_narratives(
        edition_id,
        issue_number,
        archive_root=resolved_archive,
    )
    # Concatenate narrative markdown deterministically (sorted by filename so
    # the prompt is reproducible across runs).
    narrative_text = "\n\n".join(
        f"<!-- {name} -->\n{text}" for name, text in sorted(narratives.items())
    )

    knowledge_doc_path = programs_root / program_id / "knowledge" / knowledge_doc_name
    current_knowledge_doc: str | None = None
    if knowledge_doc_path.exists():
        current_knowledge_doc = knowledge_doc_path.read_text(encoding="utf-8")

    return SynthesisInputs(
        program_id=program_id,
        edition_id=edition_id,
        issue_number=issue_number,
        accepted_proposals=accepted_proposals,
        published_narrative=narrative_text,
        knowledge_doc_name=knowledge_doc_name,
        knowledge_doc_path=knowledge_doc_path,
        current_knowledge_doc=current_knowledge_doc,
    )


# ---------------------------------------------------------------------------
# Prompt construction + payload parsing
# ---------------------------------------------------------------------------


def _build_user_prompt(inputs: SynthesisInputs) -> str:
    lines = [
        f"Program: {inputs.program_id}",
        f"Edition: {inputs.edition_id}",
        f"Issue: {inputs.issue_number:03d}",
        "",
        "Accepted NCFL proposals (operator-confirmed Plane 1 updates):",
    ]
    if inputs.accepted_proposals:
        for proposal in inputs.accepted_proposals:
            lines.append(
                f"- {proposal.target_store}.{proposal.target_key}.{proposal.target_field} "
                f"= {proposal.source_value} (confidence={proposal.confidence})"
            )
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("Published narrative markdown for the issue:")
    if inputs.published_narrative.strip():
        # Bound the narrative fed to the model to keep the prompt within budget.
        lines.append(inputs.published_narrative.strip()[:8000])
    else:
        lines.append("(none)")
    lines.append("")
    if inputs.current_knowledge_doc:
        lines.append("Current knowledge document (for continuity; do not copy verbatim):")
        lines.append(inputs.current_knowledge_doc.strip()[:4000])
        lines.append("")
    lines.append("Return JSON only.")
    return "\n".join(lines)


def _parse_payload(payload: dict[str, object], *, issue_number: int) -> KnowledgeDocDraft:
    if not isinstance(payload, dict):
        raise ContextSynthesizerError("Synthesis returned a non-object payload.")

    summary = _sanitize_text(_require_string(payload, "summary"), field_name="summary")
    if len(summary.split()) > 120:
        raise ContextSynthesizerError("summary must be 120 words or fewer.")

    highlights = _sanitize_list(
        _require_field(payload, "highlights"), field_name="highlights", max_items=5, max_chars=200
    )
    open_risks = _sanitize_list(
        _require_field(payload, "open_risks"), field_name="open_risks", max_items=5, max_chars=200
    )
    next_milestones = _sanitize_list(
        _require_field(payload, "next_milestones"),
        field_name="next_milestones",
        max_items=5,
        max_chars=200,
    )
    as_of_date = _require_iso_date(_require_string(payload, "as_of_date"))

    return KnowledgeDocDraft(
        summary=summary,
        highlights=highlights,
        open_risks=open_risks,
        next_milestones=next_milestones,
        as_of_date=as_of_date,
    )


# ---------------------------------------------------------------------------
# Ban-list enforcement (A-NC-7) + proposal construction
# ---------------------------------------------------------------------------


def enforce_ban_list(draft: KnowledgeDocDraft, *, programs_root: Path, program_id: str) -> KnowledgeDocDraft:
    """Strip banned phrases from the draft before staging (A-NC-7).

    Uses the program's ``editorial_rules.yaml`` ban-list when present. Banned
    phrases are removed (not just flagged) from every field so a proposal never
    carries disallowed language. If the rules cannot be loaded, the draft is
    returned unchanged (best-effort, never raises).
    """
    try:
        from src.core.ban_list_validator import find_ban_list_violations
        from src.core.config_loader import load_editorial_rules

        rules_path = programs_root / program_id / "editorial_rules.yaml"
        if not rules_path.exists():
            return draft
        editorial_rules = load_editorial_rules(rules_path)
    except Exception:  # noqa: BLE001 — best-effort ban-list enforcement
        return draft

    rendered = {
        "summary": draft.summary,
        "highlights": "\n".join(draft.highlights),
        "open_risks": "\n".join(draft.open_risks),
        "next_milestones": "\n".join(draft.next_milestones),
    }
    violations = find_ban_list_violations(rendered, editorial_rules)
    if not violations:
        return draft

    # Build a case-insensitive set of matched phrases to strip.
    phrases = {v.phrase.lower() for v in violations if v.phrase}
    return KnowledgeDocDraft(
        summary=_strip_phrases(draft.summary, phrases),
        highlights=tuple(_strip_phrases(item, phrases) for item in draft.highlights),
        open_risks=tuple(_strip_phrases(item, phrases) for item in draft.open_risks),
        next_milestones=tuple(_strip_phrases(item, phrases) for item in draft.next_milestones),
        as_of_date=draft.as_of_date,
    )


def _strip_phrases(text: str, phrases: set[str]) -> str:
    import re

    result = text
    for phrase in phrases:
        if not phrase:
            continue
        # Case-insensitive removal (ban-list matching is case-insensitive).
        result = re.sub(re.escape(phrase), "", result, flags=re.IGNORECASE)
    # Collapse whitespace left by removals; drop now-empty bullets.
    return " ".join(result.split())


def _build_knowledge_doc_proposal(inputs: SynthesisInputs, draft: KnowledgeDocDraft) -> ContextUpdateProposal:
    """Construct the ``knowledge_doc`` CUP from a sanitized draft.

    The proposal's ``source_value`` is the rendered markdown patch; the apply
    engine writes it to ``knowledge/<doc>.md`` with a dated ``.bak``.
    """
    from hashlib import sha256

    if not is_ncfl_target_store("knowledge_doc"):
        # Defensive: knowledge_doc must be a recognized target store.
        raise ContextSynthesizerError("knowledge_doc is not a recognized NCFL target store.")

    method = "knowledge_synthesis"
    confidence = EXTRACTION_METHOD_CONFIDENCE.get(method, "low")
    source_value = draft.render_markdown()
    target_key = inputs.knowledge_doc_name
    target_field = "body"
    source_artifact = f"published_narratives/issue_{inputs.issue_number:03d}"
    source_field = f"synthesis:{PROMPT_VERSION}"

    proposal_core = (
        f"knowledge_doc|{target_key}|{target_field}|{source_artifact}|{source_field}|"
        f"{inputs.issue_number}|{draft.as_of_date}"
    )
    conflict_core = f"knowledge_doc|{target_key}|{target_field}"
    current_value = inputs.current_knowledge_doc
    current_value_hash = (
        sha256(current_value.encode("utf-8")).hexdigest() if current_value is not None else None
    )

    return ContextUpdateProposal(
        proposal_id=sha256(proposal_core.encode("utf-8")).hexdigest()[:16],
        program_id=inputs.program_id,
        issue_number=inputs.issue_number,
        edition_id=inputs.edition_id,
        source_type="published_narrative",
        extracted_at=datetime.now(timezone.utc),
        extractor_version=NCFL_EXTRACTOR_VERSION,
        source_artifact=source_artifact,
        source_field=source_field,
        extraction_method=method,
        target_store="knowledge_doc",
        target_key=target_key,
        target_field=target_field,
        source_value=source_value,
        current_value=current_value,
        current_value_hash=current_value_hash,
        confidence=confidence,
        batch_eligible=False,  # Zone B proposals are never batch-eligible (§23.4)
        extraction_method_rationale=(
            "Zone B AI synthesis of accepted proposals + published narrative; "
            "always a proposal, never auto-applied."
        ),
        conflict_key=sha256(conflict_core.encode("utf-8")).hexdigest()[:16],
    )


# ---------------------------------------------------------------------------
# Sanitization helpers (mirror synthesizer.py discipline)
# ---------------------------------------------------------------------------


def _sanitize_text(value: str, *, field_name: str) -> str:
    try:
        processed = process_generated_text(value)
    except AIPipelineError as error:
        raise ContextSynthesizerError(f"{field_name} failed the AI safety pipeline: {error}") from error
    normalized = processed.text.strip()
    if not normalized:
        raise ContextSynthesizerError(f"{field_name} must not be empty.")
    return normalized


def _sanitize_list(
    value: object, *, field_name: str, max_items: int, max_chars: int
) -> tuple[str, ...]:
    items = _string_list(value, field_name=field_name)
    if len(items) > max_items:
        raise ContextSynthesizerError(f"{field_name} supports at most {max_items} entries.")
    sanitized: list[str] = []
    for item in items:
        text = _sanitize_text(item, field_name=field_name)
        if len(text) > max_chars:
            text = text[:max_chars].rstrip()
        if text:
            sanitized.append(text)
    return tuple(sanitized)


def _string_list(value: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ContextSynthesizerError(f"{field_name} must be a list of strings.")
    items: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ContextSynthesizerError(f"{field_name} must contain only strings.")
        normalized = entry.strip()
        if not normalized:
            continue
        items.append(normalized)
    return tuple(items)


def _require_string(payload: dict[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ContextSynthesizerError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _require_field(payload: dict[str, object], field_name: str) -> object:
    if field_name not in payload:
        raise ContextSynthesizerError(f"{field_name} must be provided.")
    return payload.get(field_name)


def _require_iso_date(value: str) -> str:
    from datetime import date

    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ContextSynthesizerError(f"as_of_date must be ISO 8601 (YYYY-MM-DD): {error}") from error
    return value
