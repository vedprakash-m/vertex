"""AITraceSanitizer (arch-fix.md Phase 0, §A.0 corpus prerequisite).

Produces a sanitized, size-bounded excerpt of AI prompt/response text safe
for TTL'd durable capture. Composes two existing safety primitives rather
than re-implementing PII/secret detection:

- ``src.ai.safety.pii_scrubber.scan_text`` — email/phone/SSN redaction.
- ``src.core.rev.privacy.scan_credentials`` — secret/credential detection
  (Azure keys, connection strings, JWTs, bearer tokens, secret assignments,
  high-entropy tokens).

Never returns unredacted raw text. Credentials are redacted (not just
flagged) before PII scrubbing, because a durable capture record must not
carry a live secret even in a truncated excerpt.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.ai.safety.pii_scrubber import scan_text
from src.core.rev.privacy import scan_credentials

DEFAULT_MAX_EXCERPT_BYTES = 4096
_TRUNCATION_MARKER = "...[TRUNCATED]"


@dataclass(frozen=True, slots=True)
class SanitizedExcerpt:
    text: str
    pii_detected: bool
    credential_detected: bool
    truncated: bool
    classification: str
    original_byte_length: int


def _redact_credentials(text: str) -> tuple[str, bool]:
    findings = scan_credentials(text)
    if not findings:
        return text, False
    redacted = text
    # Replace back-to-front so earlier match offsets stay valid as the string
    # shrinks/grows from prior substitutions.
    for finding in sorted(findings, key=lambda f: f.start, reverse=True):
        redacted = redacted[: finding.start] + f"[{finding.kind.upper()}_REDACTED]" + redacted[finding.end :]
    return redacted, True


def sanitize_ai_io(text: str, *, max_excerpt_bytes: int = DEFAULT_MAX_EXCERPT_BYTES) -> SanitizedExcerpt:
    """Sanitize raw AI prompt/response text into a durable-capture-safe excerpt.

    Order: credential redaction first (a secret must never survive even a
    truncated excerpt), then PII scrub, then size bound.
    """
    if not text:
        return SanitizedExcerpt(
            text="",
            pii_detected=False,
            credential_detected=False,
            truncated=False,
            classification="sanitized-excerpt",
            original_byte_length=0,
        )

    original_byte_length = len(text.encode("utf-8"))
    redacted, credential_detected = _redact_credentials(text)
    pii_result = scan_text(redacted)
    sanitized = pii_result.scrubbed_text

    truncated = False
    encoded = sanitized.encode("utf-8")
    if len(encoded) > max_excerpt_bytes:
        marker_bytes = _TRUNCATION_MARKER.encode("utf-8")
        keep = max(0, max_excerpt_bytes - len(marker_bytes))
        sanitized = encoded[:keep].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER
        truncated = True

    return SanitizedExcerpt(
        text=sanitized,
        pii_detected=pii_result.pii_detected,
        credential_detected=credential_detected,
        truncated=truncated,
        classification="sanitized-excerpt",
        original_byte_length=original_byte_length,
    )
