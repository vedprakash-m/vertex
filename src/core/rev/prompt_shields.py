"""REV Prompt Shields — injection classifier port (Zone A).

specs/program-context-intelligence.md §5.7. Prompt Shields is an **external,
non-deterministic** classifier (Azure AI Content Safety) that scans content
**before external transmission** (LLM extraction) for prompt-injection /
jailbreak patterns. Because it is external and probabilistic, it is never the
sole gate: the deterministic local checks (``privacy.run_local_checks``) run
**first** and fail-closed on credentials; Prompt Shields runs **after** the
local gate, chunk-by-chunk, and its absence is a **visible degrade** (never
silent).

P1 ships ``LocalOnlyPromptShields`` — the local checks only, with the external
verdict reported as ``unavailable`` and a ``degrade_reason`` recorded so the
evidence metadata and doctor surface it. The real Azure-backed classifier
(``AzurePromptShields``) is P0 operator-gated (live Azure AI credential).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.core.rev.entity_types import EntityType
from src.core.rev.ports import Chunk
from src.core.rev.privacy import LocalCheckResult, run_local_checks
from src.core.rev.result import (
    Forbidden,
    Incomplete,
    PortResult,
    RateLimited,
    Success,
    Unsupported,
)

PROMPT_SHIELDS_POLICY_VERSION = "prompt_shields.v1"
LOCAL_ONLY_POLICY_VERSION = "prompt_shields.local_only.v1"

# External verdicts (§5.7). "unavailable" = visible degrade (local-only path).
VERDICT_CLEAN = "clean"
VERDICT_FLAGGED = "flagged"
VERDICT_UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ChunkShieldResult:
    """Per-chunk shield outcome (§5.7)."""

    chunk_id: str
    local_check: LocalCheckResult
    external_verdict: str        # "clean" | "flagged" | "unavailable"
    degrade_reason: str = ""     # why the external classifier was not run
    policy_version: str = LOCAL_ONLY_POLICY_VERSION

    @property
    def admitted(self) -> bool:
        """A chunk is admitted to extraction only if local checks pass AND the
        external verdict is not ``flagged`` (``unavailable`` is a visible
        degrade, not a block — local checks remain the deterministic gate)."""
        return self.local_check.passed and self.external_verdict != VERDICT_FLAGGED


class PromptShields(Protocol):
    """FR-PCI-7 — chunk-by-chunk injection classifier (§5.7)."""

    policy_version: str

    def scan_chunks(
        self,
        chunks: tuple[Chunk, ...],
        *,
        source_type: EntityType,
        correlation_id: str,
    ) -> PortResult[tuple[ChunkShieldResult, ...]]:
        ...


class LocalOnlyPromptShields:
    """P1 default — local checks only; external verdict ``unavailable`` (visible degrade).

    Runs ``privacy.run_local_checks`` on each chunk. Credential hits fail-closed
    (``local_check.passed=False`` → not admitted). The external Azure classifier
    is not invoked, so every chunk's ``external_verdict`` is ``unavailable`` with
    a ``degrade_reason`` — recorded on evidence metadata and surfaced by
    ``doctor --rev-health`` so the operator sees the guard is in local-only mode.
    """

    policy_version = LOCAL_ONLY_POLICY_VERSION

    def __init__(self, *, max_chunk_bytes: int = 1_048_576) -> None:
        self._max_chunk_bytes = max_chunk_bytes

    def scan_chunks(
        self,
        chunks: tuple[Chunk, ...],
        *,
        source_type: EntityType,
        correlation_id: str,
    ) -> PortResult[tuple[ChunkShieldResult, ...]]:
        if not chunks:
            return Success(())
        results: list[ChunkShieldResult] = []
        for chunk in chunks:
            local = run_local_checks(
                chunk.text,
                source_type=source_type,
                max_bytes=self._max_chunk_bytes,
            )
            results.append(
                ChunkShieldResult(
                    chunk_id=chunk.chunk_id,
                    local_check=local,
                    external_verdict=VERDICT_UNAVAILABLE,
                    degrade_reason="local_only_mode:azure_prompt_shields_not_configured",
                    policy_version=self.policy_version,
                )
            )
        return Success(tuple(results))


def admit_chunks(
    results: tuple[ChunkShieldResult, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split shield results into (admitted chunk_ids, blocked chunk_ids).

    Callers use this to decide which chunks proceed to extraction. Blocked
    chunks (credential hit or external ``flagged``) are dropped; the candidate
    may still stage from the remaining admitted chunks, or fall to
    metadata-only if none are admitted (§5.7 visible degrade).
    """
    admitted: list[str] = []
    blocked: list[str] = []
    for r in results:
        if r.admitted:
            admitted.append(r.chunk_id)
        else:
            blocked.append(r.chunk_id)
    return tuple(admitted), tuple(blocked)


__all__ = [
    "PromptShields",
    "LocalOnlyPromptShields",
    "ChunkShieldResult",
    "admit_chunks",
    "PROMPT_SHIELDS_POLICY_VERSION",
    "LOCAL_ONLY_POLICY_VERSION",
    "VERDICT_CLEAN",
    "VERDICT_FLAGGED",
    "VERDICT_UNAVAILABLE",
]