"""REV privacy/security local checks (Zone A).

specs/program-context-intelligence.md §5.7 Stage 1. **Local checks run BEFORE
any external transmission** (Prompt Shields, LLM). They are in-process,
dependency-free, and fail-closed on a credential hit (quarantine; do not
transmit). PII is redacted in the canonical text **before offsets/hashes are
computed** (§5.6 fixed-order normalization → reproducibility).

This module is deliberately bounded and deterministic — it is NOT a substitute
for the external Prompt Shields classifier (§5.7), and regex/credential
heuristics alone do **not** qualify as equivalent injection protection.

W5-3 pseudonymization: display names extracted from email headers (From/To/Cc)
are replaced with stable PERSON_N tokens before the text reaches the LLM.
The token→original mapping is preserved in PseudonymTable so entity binding
can resolve tokens back to canonical identities without exposing them to the
external model.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from src.core.rev.entity_types import EntityType


# --- PII redaction patterns (deterministic, token-preserving) ---
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Phone numbers. The digit-boundary guards + date lookahead protect ISO dates
# (YYYY-MM-DD), which the bare digit+separator shape would otherwise
# false-positive on (including mid-date partial matches) — dates are signal,
# not PII, and must survive into the canonical text for extraction.
_PHONE_RE = re.compile(r"(?<!\d)(?!\d{4}-\d{2}-\d{2}\b)\+?\d[\d .\-]{7,}\d(?!\d)")
# Credit-card-like: 13-19 digit groups. Redacted to avoid persisting PANs.
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
# SSN-like (US): redacted.
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# --- Credential / secret detection (fail-closed) ---
_AZURE_KEY_RE = re.compile(r"(?i)[A-Za-z0-9+/]{86}==")  # Azure storage key shape
_CONNECTION_STRING_RE = re.compile(r"(?i)AccountKey=[A-Za-z0-9+/=]+")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")
_GRAPH_TOKEN_RE = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-]{20,}")
_SECRET_ASSIGN_RE = re.compile(r"(?i)(?:secret|password|passwd|api[_-]?key|access[_-]?key|token)\s*[:=]\s*[\"\']?[^\s\"\']{8,}")
# High-entropy long tokens (guarded to avoid flagging normal hashes we emit).
_HIGH_ENTROPY_RE = re.compile(r"\b[A-Za-z0-9+/]{40,}\b")
# URL pattern: used to strip URL tokens before high-entropy check so that
# Outlook message IDs (EWS ItemId ≥130 chars), Teams meeting tokens, and
# SafeLinks payloads embedded in URLs don't false-positive as credentials.
# The specific patterns above (Azure key, JWT, Bearer, connection string) are
# precise enough to remain on the full text.
_URL_STRIP_RE = re.compile(r"https?://\S+")

_SCRUBBER_VERSION = "scrub.v1"

# Sensitivity labels the pipeline will hydrate. Labels outside this set (e.g.,
# "highly-confidential", "restricted") are denied at the local gate.
_DEFAULT_ALLOWED_SENSITIVITY = frozenset({"", "public", "internal", "general"})


@dataclass(frozen=True, slots=True)
class CredentialFinding:
    kind: str               # "azure_key" | "connection_string" | "jwt" | "bearer" | "secret_assignment" | "high_entropy"
    start: int
    end: int
    redacted_snippet: str   # the matched text with the middle redacted


@dataclass(frozen=True, slots=True)
class LocalCheckResult:
    passed: bool
    credential_findings: tuple[CredentialFinding, ...] = ()
    quarantined: bool = False
    sensitivity_denied: bool = False
    size_exceeded: bool = False
    reason: str = ""

    @property
    def has_credential_hit(self) -> bool:
        return bool(self.credential_findings)


def _redact_match(match: re.Match[str]) -> str:
    text = match.group(0)
    if len(text) <= 8:
        return "[REDACTED]"
    return text[:2] + "[REDACTED]" + text[-2:]


def scrub_pii(text: str) -> str:
    """Redact PII (emails, phones, cards, SSNs) in-place to canonical tokens.

    Redaction is deterministic (positional) so the canonical text is stable
    across runs given the same input — offsets/hashes are reproducible (§5.6).
    """
    out = _SSN_RE.sub("[SSN_REDACTED]", text)
    out = _CARD_RE.sub("[CARD_REDACTED]", out)
    out = _EMAIL_RE.sub("[EMAIL_REDACTED]", out)
    out = _PHONE_RE.sub("[PHONE_REDACTED]", out)
    return out


def scan_credentials(text: str) -> tuple[CredentialFinding, ...]:
    """Detect credentials/secrets. Any finding → fail-closed quarantine (§5.7)."""
    findings: list[CredentialFinding] = []
    patterns = (
        ("azure_key", _AZURE_KEY_RE),
        ("connection_string", _CONNECTION_STRING_RE),
        ("jwt", _JWT_RE),
        ("bearer", _GRAPH_TOKEN_RE),
        ("secret_assignment", _SECRET_ASSIGN_RE),
    )
    for kind, pattern in patterns:
        for match in pattern.finditer(text):
            findings.append(CredentialFinding(kind, match.start(), match.end(), _redact_match(match)))
    # High-entropy only flagged when not obviously one of our own sha256 hashes
    # (which we emit as ``sha256:<64>``); those are not secrets.
    # Run on URL-stripped text so Outlook EWS ItemIds (130+ char base64 in
    # `outlook.office.com/mail/id/<ItemId>`), Teams meeting join tokens, and
    # SafeLinks payloads embedded in URLs don't false-positive as credentials.
    _text_no_urls = _URL_STRIP_RE.sub("", text)
    for match in _HIGH_ENTROPY_RE.finditer(_text_no_urls):
        surrounding = _text_no_urls[max(0, match.start() - 8): match.end() + 8]
        if "sha256:" in surrounding:
            continue
        findings.append(CredentialFinding("high_entropy", match.start(), match.end(), _redact_match(match)))
    return tuple(findings)


def scrub_pii_and_credentials(text: str) -> tuple[str, tuple[CredentialFinding, ...]]:
    """Return (canonical-scrubbed text, credential findings).

    PII is redacted into the canonical text; credentials are *reported* (not
    silently redacted) so the caller can fail-closed / quarantine.
    """
    findings = scan_credentials(text)
    scrubbed = scrub_pii(text)
    return scrubbed, findings


def run_local_checks(
    text: str,
    *,
    source_type: EntityType,
    sensitivity_label: str | None = None,
    max_bytes: int = 1_048_576,
    allowed_sensitivity: frozenset[str] = _DEFAULT_ALLOWED_SENSITIVITY,
) -> LocalCheckResult:
    """Stage-1 local gate (§5.7 step 2): credential fail-closed + sensitivity + size.

    Runs in-process, before any external transmission. A credential hit
    quarantines the item (``quarantined=True``, ``passed=False``); a denied
    sensitivity label or an over-size body likewise fails the gate without
    transmitting.
    """
    findings = scan_credentials(text)
    if findings:
        return LocalCheckResult(
            passed=False,
            credential_findings=findings,
            quarantined=True,
            reason=f"credential_hit:{findings[0].kind}",
        )
    label = (sensitivity_label or "").strip()
    if label.lower() not in allowed_sensitivity:
        return LocalCheckResult(
            passed=False,
            sensitivity_denied=True,
            reason=f"sensitivity_denied:{label or 'unknown'}",
        )
    if len(text.encode("utf-8")) > max_bytes:
        return LocalCheckResult(
            passed=False,
            size_exceeded=True,
            reason=f"size_exceeded:{len(text.encode('utf-8'))}>{max_bytes}",
        )
    return LocalCheckResult(passed=True)


def normalized_source_hash(canonical_text: str) -> str:
    return "sha256:" + hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()


def scrubber_version() -> str:
    return _SCRUBBER_VERSION


# ---------------------------------------------------------------------------
# W5-3 — Person/org pseudonymization (display-name → PERSON_N token)
# ---------------------------------------------------------------------------

# Matches "Display Name <email>" in header lines (greedy on the name part).
_DISPLAY_NAME_ANGLE_RE = re.compile(
    r'"?([^"<;,\n@]{2,}?)"?\s*<[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}>',
)
# "Last, First" corporate display-name format (not an email address).
_COMMA_NAME_RE = re.compile(r"\b([A-Z][a-z]{1,30}),\s+([A-Z][a-z]{1,20})\b")

_MIN_NAME_PARTS = 2   # require at least two space-separated words


class PseudonymTable:
    """Bidirectional mapping of display names → stable PERSON_N tokens (W5-3).

    Built from email header display names before normalization.  Stored
    alongside the canonical text so entity binding can resolve tokens back to
    the original display names without the originals ever reaching the LLM.
    Thread-local; not thread-safe for concurrent writes.
    """

    __slots__ = ("_entries", "_lower_to_token", "_person_count")

    def __init__(self) -> None:
        self._entries: list[tuple[str, str]] = []   # (original, token)
        self._lower_to_token: dict[str, str] = {}
        self._person_count: int = 0

    def assign_person(self, name: str) -> str:
        """Return the stable PERSON_N token for *name*, creating one if new."""
        stripped = name.strip()
        if not stripped:
            return name
        key = stripped.lower()
        if key in self._lower_to_token:
            return self._lower_to_token[key]
        self._person_count += 1
        token = f"PERSON_{self._person_count}"
        self._entries.append((stripped, token))
        self._lower_to_token[key] = token
        return token

    def resolve(self, token: str) -> str | None:
        """Resolve a PERSON_N token back to the original display name."""
        for original, t in self._entries:
            if t == token:
                return original
        return None

    def to_dict(self) -> dict[str, str]:
        """Serialisable form: token → original name.  Suitable for JSON storage."""
        return {t: orig for orig, t in self._entries}

    @property
    def is_empty(self) -> bool:
        return not self._entries

    def __repr__(self) -> str:  # pragma: no cover
        return f"PseudonymTable({self.to_dict()!r})"


def _extract_display_names_from_header_value(header_value: str) -> list[str]:
    """Extract display names from a single header value string.

    Handles both "Name <email>" and "Last, First" formats.
    Returns only names with at least two space-separated words (or a
    comma-separated two-part name) to minimise false positives on aliases.
    """
    names: list[str] = []
    # "Name <email>" format — most common in Outlook/Exchange headers.
    for m in _DISPLAY_NAME_ANGLE_RE.finditer(header_value):
        raw = m.group(1).strip().strip('"').strip()
        # Normalise "Last, First" → "First Last" if present.
        comma = _COMMA_NAME_RE.match(raw)
        if comma:
            name = f"{comma.group(2)} {comma.group(1)}"
        else:
            name = raw
        if name and len(name.split()) >= _MIN_NAME_PARTS:
            names.append(name)
    return names


def build_pseudonym_table_from_display_names(display_names: list[str]) -> PseudonymTable:
    """Build a PseudonymTable from a list of raw display-name strings.

    Names are assigned tokens in the order they are encountered.  Names with
    fewer than two space-separated words are skipped (too likely to be
    aliases, role names, or initials that would produce false-positive
    substitutions in the body text).
    """
    table = PseudonymTable()
    seen: set[str] = set()
    for name in display_names:
        name = name.strip()
        if not name:
            continue
        # Skip single-word names (too generic — would match common words).
        if len(name.split()) < _MIN_NAME_PARTS:
            continue
        key = name.lower()
        if key not in seen:
            table.assign_person(name)
            seen.add(key)
    return table


def pseudonymize_text(text: str, table: PseudonymTable) -> str:
    """Replace known display names in *text* with their PERSON_N tokens.

    Replacement is:
    - Case-insensitive (catches "john smith" and "John Smith").
    - Longest-match-first (prevents "John" clobbering part of "John Smith").
    - Word-boundary anchored (avoids subword collisions).

    The original display names are preserved in *table* and never emitted
    in any external-model payload.
    """
    if table.is_empty:
        return text
    # Sort by original-name length descending (longest first).
    entries = sorted(table._entries, key=lambda e: len(e[0]), reverse=True)
    result = text
    for original, token in entries:
        pattern = re.compile(r"\b" + re.escape(original) + r"\b", re.IGNORECASE)
        result = pattern.sub(token, result)
    return result


__all__ = [
    "CredentialFinding",
    "LocalCheckResult",
    "PseudonymTable",
    "build_pseudonym_table_from_display_names",
    "pseudonymize_text",
    "scrub_pii",
    "scan_credentials",
    "scrub_pii_and_credentials",
    "run_local_checks",
    "normalized_source_hash",
    "scrubber_version",
]