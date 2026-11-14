"""Fail-closed privacy boundary for rich WorkIQ evidence persistence."""
from __future__ import annotations

import re
from dataclasses import replace

from src.ai.safety.pii_scrubber import scan_text
from src.core.evidence_models import WorkstreamEvidence


_CREDENTIAL_PATTERNS = (
    ("password", re.compile(r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*\S+")),
    ("client_secret", re.compile(r"(?i)\b(?:client[_-]?secret|api[_-]?key)\s*[:=]\s*\S+")),
    ("sas_token", re.compile(r"(?i)(?:[?&]|\b)(?:sig|se|sp|sv)=\S+")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)
_SENSITIVITY_PATTERN = re.compile(
    r"(?i)\b(?:highly confidential|do not distribute|sensitivity\s*:\s*(?:confidential|restricted))\b"
)


class UnsafeWorkIQEvidenceError(ValueError):
    def __init__(self, signal_types: tuple[str, ...]) -> None:
        self.signal_types = signal_types
        super().__init__("WorkIQ evidence quarantined: " + ", ".join(signal_types))


def sanitize_workiq_evidence(evidence: WorkstreamEvidence) -> WorkstreamEvidence:
    """Scrub ordinary PII and quarantine credential/sensitivity-marked content."""

    workiq_refs = tuple(ref for ref in evidence.source_refs if ref.source_type.startswith("workiq_"))
    if (
        not workiq_refs
        or any(not isinstance(ref.canonical_id, str) or not ref.canonical_id.strip() for ref in workiq_refs)
        or any(ref.canonical_id is not None and not isinstance(ref.canonical_id, str) for ref in evidence.source_refs)
    ):
        raise UnsafeWorkIQEvidenceError(("missing_source_identity",))

    if any(
        not isinstance(eta.label, str)
        or (eta.owner is not None and not isinstance(eta.owner, str))
        for eta in evidence.etas
    ):
        raise UnsafeWorkIQEvidenceError(("invalid_eta_text",))

    persistent_text = [
        evidence.narrative_summary,
        *evidence.raw_excerpts,
        *evidence.owners,
        *(eta.label for eta in evidence.etas),
        *(eta.owner or "" for eta in evidence.etas),
        *(ref.description for ref in evidence.source_refs),
        *(ref.author or "" for ref in evidence.source_refs),
    ]
    detected = tuple(
        name
        for text in persistent_text
        for name, pattern in _CREDENTIAL_PATTERNS
        if pattern.search(text or "")
    )
    if any(_SENSITIVITY_PATTERN.search(text or "") for text in persistent_text):
        detected += ("sensitivity_marking",)
    if detected:
        raise UnsafeWorkIQEvidenceError(tuple(dict.fromkeys(detected)))

    def scrub(value: str | None) -> str | None:
        return scan_text(value or "").scrubbed_text if value is not None else None

    refs = tuple(
        replace(ref, description=scrub(ref.description) or "", author=scrub(ref.author))
        for ref in evidence.source_refs
    )
    etas = tuple(
        replace(eta, label=scrub(eta.label) or "", owner=scrub(eta.owner))
        for eta in evidence.etas
    )
    return replace(
        evidence,
        etas=etas,
        owners=tuple(scrub(owner) or "" for owner in evidence.owners),
        raw_excerpts=tuple(scrub(excerpt) or "" for excerpt in evidence.raw_excerpts),
        narrative_summary=scrub(evidence.narrative_summary) or "",
        source_refs=refs,
        privacy_classification="confidential",
    )
