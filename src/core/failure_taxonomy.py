"""WS-17: failure taxonomy for SRE-grade observability.

The taxonomy classifies failures into well-known **categories** so
`vertex doctor --diagnose <last-run>` can explain *what kind* of
failure hit on the last gather, the **operator remediation hint**, and
whether the failure is **retryable** (re-running gather may succeed)
or **persistent** (requires operator action).

Categories are deliberately small and stable. They cover the failure
shapes observed in PB-37/46/47 (the perf/SLO family) without becoming
an unbounded allowlist of exception types.

Why a taxonomy vs ad-hoc string matching?
- **Stable vocabulary.** `transient_auth` covers any 401/403 with a
  rotation hint. New connector exceptions get added once.
- **Operator-actionable.** Every category has a `next_command` the
  operator can run, so the diagnose output is a copy-paste recipe, not
  a wall of text.
- **Retryable hint.** The gather scheduler can use `retryable=True`
  to decide whether to auto-retry or escalate.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any


class FailureCategory(str, Enum):
    """Stable failure categories for SRE-grade observability."""
    TRANSIENT_AUTH = "transient_auth"           # 401/403, token expired
    RATE_LIMIT = "rate_limit"                   # 429, throttled
    NETWORK = "network"                         # ConnectionError, DNS, timeout
    QUERY_TIMEOUT = "query_timeout"             # Kusto/WorkIQ/Graph SLA breach
    SCHEMA_DRIFT = "schema_drift"               # field missing/unexpected
    PERMISSION = "permission"                   # least-privilege scope missing
    RESOURCE = "resource"                       # OOM, disk full, file lock
    CONFIG = "config"                           # missing/invalid program.yaml
    DATA_CORRUPTION = "data_corruption"         # JSONL/JSON parse fail, hash mismatch
    PII_LEAK = "pii_leak"                       # payload contains PII outside slots
    UNKNOWN = "unknown"                         # unclassified


@dataclass(frozen=True, slots=True)
class FailureClassification:
    """The result of running an exception through the failure taxonomy."""
    category: FailureCategory
    retryable: bool
    next_command: str
    detail: str


# Map of category → next-command hint shown in `doctor --diagnose`.
_NEXT_COMMANDS: dict[FailureCategory, str] = {
    FailureCategory.TRANSIENT_AUTH: "vertex doctor --check-auth --edition <name>",
    FailureCategory.RATE_LIMIT: "vertex gather --edition <name>   # auto-retries; if persistent, file connector ticket",
    FailureCategory.NETWORK: "vertex doctor --channels --edition <name>   # inspect channel reachability",
    FailureCategory.QUERY_TIMEOUT: "vertex doctor --kusto --edition <name>   # check cluster SLA + slow query log",
    FailureCategory.SCHEMA_DRIFT: "vertex doctor --channels --edition <name>   # compare field map against expected schema",
    FailureCategory.PERMISSION: "vertex admin scope audit --edition <name>   # check least-privilege scopes vs posture",
    FailureCategory.RESOURCE: "vertex doctor --storage --edition <name>   # inspect disk + journal size",
    FailureCategory.CONFIG: "vertex doctor --kb --edition <name>   # inspect program.yaml / edition.yaml",
    FailureCategory.DATA_CORRUPTION: "vertex doctor --consistency --edition <name>   # inspect archive + hash chain",
    FailureCategory.PII_LEAK: "vertex doctor --privacy --edition <name>   # check PII redaction discipline",
    FailureCategory.UNKNOWN: "vertex doctor --diagnose <name> --format json   # capture full exception chain",
}


_RETRYABLE_CATEGORIES: frozenset[FailureCategory] = frozenset({
    FailureCategory.TRANSIENT_AUTH,
    FailureCategory.RATE_LIMIT,
    FailureCategory.NETWORK,
    FailureCategory.QUERY_TIMEOUT,
})


# Substring patterns used to map an exception message → a category.
# Patterns are matched case-insensitively; the order in this list is
# the priority order (first match wins).
_PATTERN_TABLE: tuple[tuple[FailureCategory, tuple[str, ...], bool], ...] = (
    # category, substring patterns, retryable
    (FailureCategory.RATE_LIMIT, ("rate limit", "429", "too many requests", "throttled"), True),
    (FailureCategory.TRANSIENT_AUTH, ("401", "unauthorized", "token expired", "auth", "credential"), True),
    (FailureCategory.PERMISSION, ("403", "forbidden", "insufficient privileges", "scope"), False),
    (FailureCategory.QUERY_TIMEOUT, ("query timeout", "kusto timeout", "sla breach", "deadline"), True),
    (FailureCategory.NETWORK, ("connection", "timeout", "dns", "ssl", "tls", "reset by peer", "broken pipe"), True),
    (FailureCategory.RESOURCE, ("out of memory", "disk full", "no space", "filelock", "locked"), False),
    (FailureCategory.SCHEMA_DRIFT, ("schema", "missing field", "unexpected field", "field not found", "unknown column"), False),
    (FailureCategory.DATA_CORRUPTION, ("jsonl", "json decode", "checksum mismatch", "invalid manifest", "hash mismatch"), False),
    (FailureCategory.PII_LEAK, ("pii", "redaction", "outside slot"), False),
    (FailureCategory.CONFIG, ("missing required", "config", "yaml", "missing file"), False),
)


def classify_exception(exc: BaseException | str) -> FailureClassification:
    """Map an exception (or its string form) to a FailureClassification.

    Used by `vertex doctor --diagnose` to explain the last failure, and
    by the run-telemetry writer to tag the failure category in the
    per-channel health log."""
    text = _exception_text(exc)
    for category, substrings, retryable in _PATTERN_TABLE:
        for substring in substrings:
            if substring in text:
                return FailureClassification(
                    category=category,
                    retryable=retryable,
                    next_command=_NEXT_COMMANDS[category],
                    detail=f"matched pattern: {substring!r}",
                )
    return FailureClassification(
        category=FailureCategory.UNKNOWN,
        retryable=False,
        next_command=_NEXT_COMMANDS[FailureCategory.UNKNOWN],
        detail="no pattern matched; treat as novel failure",
    )


def classify_message(message: str) -> FailureClassification:
    """Convenience for callers that already have a string (no exception)."""
    return classify_exception(message)


def is_retryable(category: FailureCategory) -> bool:
    """Whether a category suggests auto-retry is safe."""
    return category in _RETRYABLE_CATEGORIES


def all_categories() -> tuple[FailureCategory, ...]:
    """Return the full taxonomy in stable declaration order."""
    return tuple(FailureCategory)


# -------- internals --------


def _exception_text(exc: BaseException | str) -> str:
    if isinstance(exc, str):
        return exc.lower()
    parts = [str(exc).lower()]
    cause = exc.__cause__ or exc.__context__
    if cause is not None and not isinstance(cause, BaseException):
        parts.append(str(cause).lower())
    return " | ".join(parts)


# Whitelist-checked regex for "PII redaction" sub-claim (used by tests
# to ensure the pattern table is exhaustive for known shapes).
def _all_patterns_compile() -> bool:
    """Sanity check that all patterns are valid substrings (always true; here as a hook for future regex upgrades)."""
    for _, substrings, _ in _PATTERN_TABLE:
        for substring in substrings:
            assert isinstance(substring, str) and substring
    return True
