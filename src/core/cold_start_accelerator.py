"""FR-SG-44: Cold-start accelerator for new program onboarding.

Bootstraps a new program from existing artefacts (prior newsletter text,
program.yaml workstream config) to cut onboarding from weeks to ~1 day.

All seeded items are marked as *cold-start candidates* — they are NOT
written to the canonical risk/decision registers by this module.
Callers must review and promote candidates explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.core.config_loader import PROGRAMS_ROOT


# ---------------------------------------------------------------------------
# Candidate data models (not canonical register entries)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RiskCandidate:
    """A candidate risk inferred from newsletter prose or ADO data.

    Source = 'cold-start'; must be reviewed before promoting to risk_register.
    """

    title: str
    description: str
    source_excerpt: str     # the sentence or fragment that surfaced this risk
    workstream_hint: str | None  # workstream inferred from section context
    source: str = "cold-start"  # always 'cold-start' for provenance clarity


@dataclass(frozen=True, slots=True)
class DecisionCandidate:
    """A candidate decision inferred from newsletter prose.

    Source = 'cold-start'; must be reviewed before promoting to decision_register.
    """

    text: str
    source_excerpt: str
    workstream_hint: str | None
    source: str = "cold-start"


@dataclass(frozen=True, slots=True)
class WorkstreamInference:
    """A workstream inferred from program.yaml or newsletter section headers."""

    workstream_id: str
    display_name: str
    inferred_from: str   # 'program_yaml', 'newsletter_h2', 'newsletter_h3'


@dataclass(frozen=True, slots=True)
class ColdStartSeedResult:
    """Result of a cold-start bootstrap run.

    Contains candidate risks/decisions and inferred workstreams for human review.
    Callers must explicitly promote candidates to canonical registers.
    """

    program_id: str
    inferred_workstreams: tuple[WorkstreamInference, ...]
    risk_candidates: tuple[RiskCandidate, ...]
    decision_candidates: tuple[DecisionCandidate, ...]
    seeded_at: datetime
    notes: tuple[str, ...]


# ---------------------------------------------------------------------------
# Newsletter bootstrap
# ---------------------------------------------------------------------------

# Sentence patterns that suggest a risk mention
_RISK_PATTERNS = (
    re.compile(r"\b(blocked?|blocking|at risk|risks?|concern|escalat\w+|slip|delay\w*|critical)\b", re.IGNORECASE),
)

# Sentence patterns that suggest a decision mention
_DECISION_PATTERNS = (
    re.compile(r"\b(decided|decision|agreed|approved|commit\w*|resolved|signed off|go-ahead|green.?lit)\b", re.IGNORECASE),
)

_H2_PATTERN = re.compile(r"^##\s+(.+)$", re.MULTILINE)
_H3_PATTERN = re.compile(r"^###\s+(.+)$", re.MULTILINE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def bootstrap_from_newsletter(
    program_id: str,
    newsletter_text: str,
    *,
    max_risk_candidates: int = 20,
    max_decision_candidates: int = 10,
) -> ColdStartSeedResult:
    """Bootstrap program candidates from a prior newsletter (Markdown or HTML).

    Parses section headers to infer workstreams, extracts risk-indicative
    sentences as RiskCandidates, and decision-indicative sentences as
    DecisionCandidates.

    All returned items are candidates — no data is written to any register.
    """
    # Strip HTML if present (basic: remove tags)
    text = _strip_html(newsletter_text)

    workstream_inferences = _infer_workstreams_from_headers(text)

    # Build per-section context for sentence extraction
    sections = _split_into_sections(text)

    risk_candidates: list[RiskCandidate] = []
    decision_candidates: list[DecisionCandidate] = []

    for section_title, section_body in sections:
        ws_hint = _slug(section_title) if section_title else None
        sentences = _extract_sentences(section_body)

        for sentence in sentences:
            if len(risk_candidates) < max_risk_candidates and _matches_any(sentence, _RISK_PATTERNS):
                risk_candidates.append(
                    RiskCandidate(
                        title=sentence[:120].rstrip(".,;"),
                        description=sentence[:500],
                        source_excerpt=sentence,
                        workstream_hint=ws_hint,
                    )
                )
            if len(decision_candidates) < max_decision_candidates and _matches_any(sentence, _DECISION_PATTERNS):
                decision_candidates.append(
                    DecisionCandidate(
                        text=sentence[:500],
                        source_excerpt=sentence,
                        workstream_hint=ws_hint,
                    )
                )

    notes: list[str] = [
        f"Bootstrapped from newsletter ({len(text)} chars). "
        f"All items are cold-start candidates — review before promoting to registers."
    ]
    if not workstream_inferences:
        notes.append("No section headers found; workstream inference skipped.")

    return ColdStartSeedResult(
        program_id=program_id,
        inferred_workstreams=tuple(workstream_inferences),
        risk_candidates=tuple(risk_candidates),
        decision_candidates=tuple(decision_candidates),
        seeded_at=datetime.now(timezone.utc),
        notes=tuple(notes),
    )


def infer_workstreams_from_program_yaml(
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[WorkstreamInference, ...]:
    """Infer workstream IDs from program.yaml workstream config.

    Reads ``programs/<program_id>/program.yaml`` and extracts the
    ``workstreams`` list (id + display_name). Returns an empty tuple
    if the file does not exist or has no workstream list.
    """
    path = programs_root / program_id / "program.yaml"
    if not path.exists():
        return ()

    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return ()

    workstreams_raw = raw.get("workstreams") or []
    if not isinstance(workstreams_raw, list):
        return ()

    inferences: list[WorkstreamInference] = []
    for ws in workstreams_raw:
        if not isinstance(ws, dict):
            continue
        ws_id = str(ws.get("id") or ws.get("workstream_id") or "").strip()
        if not ws_id:
            continue
        display = str(ws.get("name") or ws.get("display_name") or ws_id).strip()
        inferences.append(
            WorkstreamInference(
                workstream_id=ws_id,
                display_name=display,
                inferred_from="program_yaml",
            )
        )
    return tuple(inferences)


def compute_signal_density_thresholds(
    signal_counts_by_workstream: dict[str, int],
    *,
    global_min: float = 0.5,
    percentile_factor: float = 0.75,
) -> dict[str, float]:
    """Compute per-workstream signal density thresholds from observed counts.

    A new program has no historical baselines. This function uses the first
    gather's signal distribution to set conservative (75th percentile) per-
    workstream thresholds, floored at ``global_min``.

    Returns a mapping of workstream_id → threshold (signals/day).
    """
    if not signal_counts_by_workstream:
        return {}

    counts = sorted(signal_counts_by_workstream.values())
    p75_index = max(0, int(len(counts) * percentile_factor) - 1)
    p75_value = counts[p75_index]

    return {
        ws_id: max(global_min, count * percentile_factor)
        for ws_id, count in signal_counts_by_workstream.items()
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _strip_html(text: str) -> str:
    """Remove HTML tags; keep text content."""
    return re.sub(r"<[^>]+>", " ", text)


def _slug(title: str) -> str:
    """Convert a section title to a workstream-style slug."""
    return re.sub(r"\W+", "_", title.strip().lower()).strip("_")[:40]


def _infer_workstreams_from_headers(text: str) -> list[WorkstreamInference]:
    inferences: list[WorkstreamInference] = []
    seen: set[str] = set()

    for match in _H2_PATTERN.finditer(text):
        title = match.group(1).strip()
        ws_id = _slug(title)
        if ws_id and ws_id not in seen:
            seen.add(ws_id)
            inferences.append(WorkstreamInference(ws_id, title, "newsletter_h2"))

    for match in _H3_PATTERN.finditer(text):
        title = match.group(1).strip()
        ws_id = _slug(title)
        if ws_id and ws_id not in seen:
            seen.add(ws_id)
            inferences.append(WorkstreamInference(ws_id, title, "newsletter_h3"))

    return inferences


def _split_into_sections(text: str) -> list[tuple[str, str]]:
    """Split newsletter text into (section_title, body) pairs by H2 headers."""
    parts = re.split(r"(?m)^##\s+(.+)$", text)
    sections: list[tuple[str, str]] = []

    # parts[0] is preamble before first header
    if parts[0].strip():
        sections.append(("", parts[0]))

    i = 1
    while i + 1 < len(parts):
        sections.append((parts[i].strip(), parts[i + 1]))
        i += 2

    return sections


def _extract_sentences(text: str) -> list[str]:
    """Split text into sentences, returning non-trivial ones."""
    sentences = _SENTENCE_SPLIT.split(text)
    result: list[str] = []
    for s in sentences:
        s = s.strip()
        if len(s) > 20:
            result.append(s)
    return result


def _matches_any(sentence: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(p.search(sentence) for p in patterns)
