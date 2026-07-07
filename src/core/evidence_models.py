"""Structured evidence model for workstream lanes (BL-21).

Replaces the opaque workiq_latest string with structured extraction targets.
Migration path: workiq_latest strings are parsed deterministically for date,
ADO IDs, and IcM IDs. Full extraction requires ContentExtractionAgent (Phase 3).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from src.core.models import RiskLevel


# ── Supporting types ──────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class EtaRecord:
    """A single ETA commitment extracted from program evidence."""
    label: str
    eta_date: date
    owner: str | None
    status: str               # "open" | "closed" | "missed"
    ado_id: str | None = None


EvidenceSourceType = Literal[
    "workiq_email",
    "workiq_transcript",
    "workiq_teams",
    "local_kb",
    "sharepoint_xlsx",
    "sharepoint_docx",
    "manual",
    "lt_deck",          # SP3-0: native .pptx extraction via lt_deck_extractor.py (ME-02)
    "sharepoint_pptx",  # SP3-0: WorkIQ-sourced .pptx content (ME-03 fallback; distinct from native lt_deck)
]

ExtractionMethod = Literal["one_hop", "two_hop", "transcript", "document", "manual"]


class VerificationState(str, Enum):
    """How strongly the extracted claims are grounded in their source."""

    UNVERIFIED = "unverified"
    MODEL_SELF_ATTESTED = "model_self_attested"
    HUMAN_VERIFIED = "human_verified"
    SOURCE_VERIFIED = "source_verified"

    @property
    def is_grounded(self) -> bool:
        return self in {self.HUMAN_VERIFIED, self.SOURCE_VERIFIED}


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Attribution record: which document/email/transcript provided this evidence."""
    source_type: EvidenceSourceType
    description: str
    source_date: date | None
    author: str | None
    permalink: str | None = None
    extraction_method: ExtractionMethod | None = None
    canonical_id: str | None = None


# ── WorkstreamEvidence ────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class WorkstreamEvidence:
    """Structured companion to workiq_latest. confidence=0.0 means placeholder only."""
    lane_id: str
    synthesized_at: datetime
    risk_level: "RiskLevel"
    etas: tuple[EtaRecord, ...]
    blocking_items: tuple[str, ...]    # "ADO:37777539", "IcM:771996570", "PR:1234", "PIPELINE:98765"
    owners: tuple[str, ...]
    source_refs: tuple[SourceRef, ...]
    raw_excerpts: tuple[str, ...]
    confidence: float                  # 0.0 = placeholder; >0.0 = AI-extracted
    narrative_summary: str
    stale_after: datetime | None = None
    lt_deck_alignment: Literal["aligned", "diverged", "lt_only"] | None = None  # SP3-0: LT deck alignment check
    verification_state: VerificationState = VerificationState.UNVERIFIED
    privacy_classification: str = "internal"


# ── Regex patterns ────────────────────────────────────────────────────────────

_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_ADO_RE = re.compile(r"\bADO[ :#]?(\d{5,8})\b", re.IGNORECASE)
_ICM_RE = re.compile(r"\bI[cC][mM][:#]?\s*(\d{6,12})\b")
_PR_RE = re.compile(r"\b(?:PR|Pull Request)(?:\s*[#:])?\s*(\d{1,10})\b", re.IGNORECASE)
_PIPELINE_RE = re.compile(r"\b(?:Pipeline(?: Run)?|Run)(?:\s*[#:])?\s*(\d{3,12})\b", re.IGNORECASE)


# ── Parser functions ──────────────────────────────────────────────────────────

def parse_workiq_latest_date(workiq_latest: str | None) -> date | None:
    """Extract the date prefix (YYYY-MM-DD) from a workiq_latest string."""
    if not workiq_latest:
        return None
    m = _DATE_PREFIX_RE.match(workiq_latest.strip())
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def extract_ado_ids(text: str) -> tuple[str, ...]:
    """Return deduplicated ADO work item IDs (digits only) found in text."""
    return tuple(dict.fromkeys(_ADO_RE.findall(text)))


def extract_icm_ids(text: str) -> tuple[str, ...]:
    """Return deduplicated IcM incident IDs (digits only) found in text."""
    return tuple(dict.fromkeys(_ICM_RE.findall(text)))


def extract_pr_ids(text: str) -> tuple[str, ...]:
    """Return deduplicated pull request IDs (digits only) found in text."""
    return tuple(dict.fromkeys(_PR_RE.findall(text)))


def extract_pipeline_run_ids(text: str) -> tuple[str, ...]:
    """Return deduplicated pipeline run IDs (digits only) found in text."""
    return tuple(dict.fromkeys(_PIPELINE_RE.findall(text)))


def build_placeholder_evidence(
    lane_id: str,
    workiq_latest: str | None,
) -> WorkstreamEvidence | None:
    """Build a low-confidence placeholder from workiq_latest text.

    Returns None when workiq_latest is absent or lacks a valid date prefix.
    confidence=0.0 signals that ETAs and risk_level have not been AI-extracted;
    all doctor checks guard on this sentinel and skip placeholder evidence.
    """
    # Import here to avoid circular import; models.py does not import evidence_models.
    from src.core.models import RiskLevel  # noqa: PLC0415

    if not workiq_latest:
        return None
    synthesis_date = parse_workiq_latest_date(workiq_latest)
    if synthesis_date is None:
        return None
    synthesized_at = datetime(
        synthesis_date.year,
        synthesis_date.month,
        synthesis_date.day,
        tzinfo=timezone.utc,
    )
    ado_ids = tuple(f"ADO:{id_}" for id_ in extract_ado_ids(workiq_latest))
    icm_ids = tuple(f"IcM:{id_}" for id_ in extract_icm_ids(workiq_latest))
    pr_ids = tuple(f"PR:{id_}" for id_ in extract_pr_ids(workiq_latest))
    pipeline_ids = tuple(f"PIPELINE:{id_}" for id_ in extract_pipeline_run_ids(workiq_latest))
    return WorkstreamEvidence(
        lane_id=lane_id,
        synthesized_at=synthesized_at,
        risk_level=RiskLevel.UNKNOWN,
        etas=(),
        blocking_items=ado_ids + icm_ids + pr_ids + pipeline_ids,
        owners=(),
        source_refs=(),
        raw_excerpts=(),
        confidence=0.0,
        narrative_summary=workiq_latest,
    )
