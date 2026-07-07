"""Typed result union for REV capability ports (Zone A).

Every capability port (``CandidateEnumerator``, ``ContentHydrator``,
``ChangeFeed``, ``SemanticChunkRetriever``, ``EvidenceVerifier``) returns a
``PortResult[T]`` rather than raising or returning a bare ``RetrievedItem``.
This keeps unsupported / forbidden / rate-limited / incomplete outcomes
first-class so the governor and run-state machine can record them categorically
(G-enum) instead of collapsing them into ``None``.

Design (specs/program-context-intelligence.md §5.10):
* ``Success[T]`` — the capability produced a value of type ``T``.
* ``Unsupported`` — the tenant/config does not enable this capability for this
  entity type (reported, not retried).
* ``Forbidden`` — consent/auth denied for this scope; visible degrade, no retry.
* ``RateLimited`` — 429 / throttle; the caller already honored Retry-After via
  ``src/core/retry.py``; surfaced so the governor counts it.
* ``Incomplete`` — partial result (budget stop, page cut, payload over the
  per-item ceiling); the value carried is what was salvaged.

The union is closed and pattern-matched via ``isinstance``; no bare exceptions
cross the port boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeGuard, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Success(Generic[T]):
    value: T


@dataclass(frozen=True, slots=True)
class Unsupported:
    entity_type: str
    reason: str


@dataclass(frozen=True, slots=True)
class Forbidden:
    scope: str
    reason: str


@dataclass(frozen=True, slots=True)
class RateLimited:
    provider: str
    retry_after_seconds: float | None


@dataclass(frozen=True, slots=True)
class Incomplete(Generic[T]):
    value: T
    reason: str  # "budget_stop" | "page_cut" | "item_over_ceiling" | ...


PortResult = Success[T] | Unsupported | Forbidden | RateLimited | Incomplete[T]
"""The closed result union returned by every capability port."""


def is_success(result: object) -> TypeGuard[Success[Any]]:
    """Type-narrowing guard: returns True iff ``result`` is a ``Success``."""
    return isinstance(result, Success)


def unwrap_success(result: object) -> object:
    """Return the carried value for a ``Success``/``Incomplete``; raise otherwise."""
    if isinstance(result, (Success, Incomplete)):
        return result.value
    raise TypeError(f"expected Success or Incomplete, got {type(result).__name__}")


def outcome_category(result: object) -> str:
    """Map a port result to a G-enum category (§5.13).

    ``complete`` for ``Success``; ``truncated_by_budget`` / ``provider_limited``
    for ``Incomplete`` (by reason); ``provider_limited`` for ``RateLimited`` /
    ``Forbidden``; ``unsupported`` for ``Unsupported``.
    """
    if isinstance(result, Success):
        return "complete"
    if isinstance(result, Incomplete):
        if result.reason in ("budget_stop", "page_cut", "item_over_ceiling"):
            return "truncated_by_budget"
        return "provider_limited"
    if isinstance(result, RateLimited):
        return "provider_limited"
    if isinstance(result, Forbidden):
        return "provider_limited"
    if isinstance(result, Unsupported):
        return "unsupported"
    return "failed"