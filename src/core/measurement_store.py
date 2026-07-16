"""Append-only measurement stores for the arch-data-fix closure feature (ADF-W0.7).

These stores own high-volume, non-business-fact measurement records: tier
decisions, AI call telemetry, channel execution, and run telemetry. They are
*durable for their declared metric windows* but are not business-fact
authorities (Section 9.6). Gaps or corruption invalidate the affected metric
rather than being silently reconstructed.

Conventions:

- Every row carries a ``record_checksum`` (Appendix A.4): sha256 of the canonical
  JSON payload excluding the ``record_checksum`` field itself.
- Writes route through :mod:`src.core.jsonl_utils` (locking, rotation,
  quarantine) per the PB-37 contract.
- Rotation is sized per Section 9.7 to preserve the declared retention window.
- Schema versions are carried on every row for forward-compatible readers.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records

#: Schema version for measurement rows emitted by this module.
MEASUREMENT_SCHEMA_VERSION = "1"

#: Default rotation cap (bytes) per measurement file. Sized so that ~30 days of
#: single-program rows survive rotation; the canonical 45/90-day windows are the
#: retention *floor* enforced by :mod:`src.core.adf_config`, not a rotation size.
#: Phase-0 ratification (ADF-W0.6) may tune this via config without code change.
DEFAULT_MEASUREMENT_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB

#: Store paths relative to ``programs/<id>/`` (Section 9.6).
TIER_DECISIONS_REL = "runtime/tier_decisions.jsonl"
RUN_TELEMETRY_REL = "runtime/run_telemetry.jsonl"
AI_TELEMETRY_REL = "_state/ai_telemetry.jsonl"


# --------------------------------------------------------------------------------------
# Checksum
# --------------------------------------------------------------------------------------


def compute_record_checksum(payload: dict[str, Any]) -> str:
    """sha256 over the canonical JSON of ``payload`` minus its checksum field.

    Keys are sorted; whitespace is stripped so the digest is stable across
    machines and Python versions.
    """
    body = {k: v for k, v in payload.items() if k != "record_checksum"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_record_checksum(payload: dict[str, Any]) -> bool:
    """Return True if the row's ``record_checksum`` matches its body."""
    stored = payload.get("record_checksum")
    if not isinstance(stored, str):
        return False
    return stored == compute_record_checksum(payload)


# --------------------------------------------------------------------------------------
# Measurement record (TierDecisionRecord, Appendix A.4 / Section 9.4)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TierDecisionRecord:
    """One persisted routing decision (Section 9.4, Appendix A.4).

    Reuses the existing ``Tier`` enum plus the new ``CACHE`` member, and the
    existing ``RouteOutcome`` plus ``CACHE_HIT`` (Appendix A.4). The canonical
    serialization adds ``schema_version``, ``execution_mode``, and
    ``record_checksum`` on top of the W0.7 decision payload.
    """

    schema_version: str
    program_id: str
    edition_id: str | None
    run_id: str
    feature: str
    chosen_tier: str
    outcome: str
    confidence: float | None
    frontier_eligible: bool
    frontier_called: bool
    cache_hit: bool
    policy_version: str
    model_version: str | None
    deployment_id: str | None
    context_hash: str | None
    latency_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    execution_mode: str
    recorded_at: str

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "program_id": self.program_id,
            "edition_id": self.edition_id,
            "run_id": self.run_id,
            "feature": self.feature,
            "chosen_tier": self.chosen_tier,
            "outcome": self.outcome,
            "confidence": self.confidence,
            "frontier_eligible": self.frontier_eligible,
            "frontier_called": self.frontier_called,
            "cache_hit": self.cache_hit,
            "policy_version": self.policy_version,
            "model_version": self.model_version,
            "deployment_id": self.deployment_id,
            "context_hash": self.context_hash,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "execution_mode": self.execution_mode,
            "recorded_at": self.recorded_at,
        }
        payload["record_checksum"] = compute_record_checksum(payload)
        return payload


def tier_decision_store_path(program_id: str, *, programs_root: Path) -> Path:
    return programs_root / program_id / TIER_DECISIONS_REL


def append_measurement(
    path: Path,
    record: Any,
    *,
    max_bytes: int = DEFAULT_MEASUREMENT_MAX_BYTES,
) -> bool:
    """Append one measurement record to *path* as a JSONL line.

    ``record`` must expose ``to_payload()`` returning a dict with a
    ``record_checksum`` field (see :class:`TierDecisionRecord`). Returns
    ``True`` if a rotation was performed.
    """
    payload = record.to_payload()
    line = json.dumps(payload, default=str) + os.linesep
    return append_jsonl_line(path, line, max_bytes=max_bytes)


def append_measurements(
    path: Path,
    records: Iterable[Any],
    *,
    max_bytes: int = DEFAULT_MEASUREMENT_MAX_BYTES,
) -> int:
    """Append several measurement records; returns the count written."""
    count = 0
    for record in records:
        append_measurement(path, record, max_bytes=max_bytes)
        count += 1
    return count


def read_measurements(path: Path) -> tuple[dict[str, Any], ...]:
    """Read measurement rows, quarantining corrupt/unchecksummed lines.

    Corrupt JSON or a checksum mismatch causes the row to be quarantined via
    :mod:`src.core.jsonl_utils`; the returned tuple omits it. Callers treat a
    gap as invalidation of the affected metric (Section 9.6).
    """
    if not path.exists():
        return ()
    return tuple(read_jsonl_records(path))


# --------------------------------------------------------------------------------------
# Tier-decision sink (registered at CLI startup, ADF-W0.7 step 2)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TierDecisionSinkConfig:
    """Context the sink needs to materialize a durable ``TierDecisionRecord``.

    The sink is registered once per CLI run with the current program/edition/run
    identity and the execution mode resolved from :mod:`src.core.adf_config`.
    """

    program_id: str
    edition_id: str | None
    run_id: str
    execution_mode: str
    policy_version: str = "1"
    programs_root: Path = field(default_factory=Path)


def make_tier_decision_sink(config: TierDecisionSinkConfig):
    """Build a ``register_decision_sink``-compatible callable.

    The callable maps an in-memory ``TierDecision`` (``src.ai.tiered_router``,
    Zone B) into a durable :class:`TierDecisionRecord` and appends it to the
    program's tier-decision store. Failures are swallowed so a durable sink can
    never break routing (matches the existing sink contract in
    :func:`src.ai.tiered_router._record`).

    Zone A (this module) must not import Zone B (INV-ADF-17), so the sink
    reads ``decision.tier`` / ``decision.outcome`` by duck-typed ``.value``
    string access instead of importing the ``Tier`` / ``RouteOutcome`` enums.
    """

    store_path = tier_decision_store_path(config.program_id, programs_root=config.programs_root)

    def _sink(decision: Any) -> None:
        try:
            record = TierDecisionRecord(
                schema_version=MEASUREMENT_SCHEMA_VERSION,
                program_id=config.program_id,
                edition_id=config.edition_id,
                run_id=config.run_id,
                feature=decision.feature,
                chosen_tier=decision.tier.value,
                outcome=decision.outcome.value,
                confidence=decision.confidence,
                # TierDecision is the pre-W0.7 in-memory record; frontier_eligible
                # and cache metadata are not yet carried on it, so we derive the
                # durable facts we can and leave the rest None until Slice 5.
                frontier_eligible=False,
                frontier_called=decision.frontier_called,
                cache_hit=decision.outcome.value == "cache_hit",
                policy_version=config.policy_version,
                model_version=None,
                deployment_id=None,
                context_hash=None,
                latency_ms=None,
                input_tokens=None,
                output_tokens=None,
                cost_usd=None,
                execution_mode=config.execution_mode,
                recorded_at=decision.recorded_at,
            )
            append_measurement(store_path, record)
        except Exception:  # pragma: no cover - sink must never break routing
            pass

    _sink.config = config  # type: ignore[attr-defined]
    _sink.store_path = store_path  # type: ignore[attr-defined]
    return _sink


def utc_now_iso() -> str:
    """UTC now as an ISO-8601 string (shared so records and tests agree)."""
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "MEASUREMENT_SCHEMA_VERSION",
    "DEFAULT_MEASUREMENT_MAX_BYTES",
    "TIER_DECISIONS_REL",
    "RUN_TELEMETRY_REL",
    "AI_TELEMETRY_REL",
    "TierDecisionRecord",
    "TierDecisionSinkConfig",
    "compute_record_checksum",
    "verify_record_checksum",
    "tier_decision_store_path",
    "append_measurement",
    "append_measurements",
    "read_measurements",
    "make_tier_decision_sink",
    "utc_now_iso",
]
