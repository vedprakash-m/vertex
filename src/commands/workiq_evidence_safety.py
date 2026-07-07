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

    persistent_text = [
        evidence.narrative_summary,
        *evidence.raw_excerpts,
        *evidence.owners,
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
    return replace(
        evidence,
        owners=tuple(scrub(owner) or "" for owner in evidence.owners),
        raw_excerpts=tuple(scrub(excerpt) or "" for excerpt in evidence.raw_excerpts),
        narrative_summary=scrub(evidence.narrative_summary) or "",
        source_refs=refs,
        privacy_classification="confidential",
    )
