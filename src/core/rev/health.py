"""REV health report (Zone A) — FR-PCI-12 / doctor --rev-health.

specs/program-context-intelligence.md §5.13 + specs/gaps.md REV-G8b. Summarizes
the REV subsystem state for one program: candidate run-state distribution,
verification-state distribution, evidence-vault counts + retention-class
breakdown, the Prompt-Shields mode (local-only visible degrade), hydration
fallback rate, pending-queue age/size, enumeration-completion distribution, and
budget utilization. **REV-G8b (P2-7) local-import telemetry:** last-cycle
summary from ``_rev/last_cycle.json`` (actual ``shield_degrade`` runtime value,
``llm_fallback_count``, ``wall_clock_seconds``), LLM-fallback trend across the
last 3 cycles from ``_rev/cycle_history.jsonl``, inbox staleness + quarantine
count/reasons from the local-import inbox tree, circuit-breaker +
quality-floor-not-established + vault-size warnings. Pure read-only
aggregation over the append-only ledgers + the local-import filesystem state —
no mutation, no external calls.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.jsonl_utils import read_jsonl_records
from src.core.ledger.candidate_store import PROGRAMS_ROOT, load_pending_candidates, load_triage_decisions
from src.core.ledger.rev_evidence import RETENTION_CLASS_ACCEPTED_EVENT, load_rev_evidence_metadata
from src.core.ledger.verification_assertions import (
    assertion_state_distribution,
    load_verification_assertions,
)
from src.core.rev.run_state import RunState, load_run_states, state_distribution
from src.core.reality_completeness import (
    RealityCompletenessVector,
    compute_reality_completeness_vector,
    render_completeness_vector_human,
)

log = logging.getLogger(__name__)

REV_HEALTH_SCHEMA_VERSION = "rev_health.v3"
DEFAULT_INBOX_STALE_DAYS = 14
DEFAULT_QUARANTINE_PURGE_DAYS = 30
QUARANTINE_ALERT_THRESHOLD = 20
VAULT_COUNT_WARN_THRESHOLD = 1000
QUALITY_FLOOR_CYCLES_TRIGGER = 10
_EML_GLOB = "*.eml"

# Cycle-history fields written by the pipeline (see _write_cycle_checkpoint).
_HIST_FALLBACK_KEY = "llm_fallback_count"
_HIST_ENUMERATED_KEY = "enumerated"
_HIST_STOP_KEY = "stop_category"


@dataclass(frozen=True, slots=True)
class LastCycleSummary:
    """Single-cycle checkpoint read from ``_rev/last_cycle.json`` (schema 1.0)."""

    correlation_id: str | None = None
    stop_category: str | None = None
    candidates_staged: int | None = None
    enumerated: int | None = None
    llm_fallback_count: int | None = None
    shield_degrade: bool | None = None
    wall_clock_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class InboxTelemetry:
    """Local-import inbox + quarantine filesystem state (REV-G8b)."""

    inbox_path: str | None = None
    inbox_newest_age_days: float | None = None     # None = no pending files
    inbox_stale: bool = False                      # newest file older than threshold
    quarantine_file_count: int = 0
    quarantine_top_reasons: tuple[tuple[str, int], ...] = ()
    quarantine_purge_candidates: int = 0            # files >30d old, not crash_loop


def _rev_dir(program_id: str, programs_root: Path) -> Path:
    return programs_root / program_id / "_rev"


def _inbox_dir(program_id: str, programs_root: Path) -> Path:
    return programs_root / program_id / "rev_inbox"


def _read_last_cycle(program_id: str, programs_root: Path) -> LastCycleSummary | None:
    path = _rev_dir(program_id, programs_root) / "last_cycle.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("rev-health: last_cycle.json unreadable for %s: %s", program_id, exc)
        return None
    return LastCycleSummary(
        correlation_id=data.get("correlation_id"),
        stop_category=data.get("stop_category"),
        candidates_staged=data.get("candidates_staged"),
        enumerated=data.get("enumerated"),
        llm_fallback_count=data.get("llm_fallback_count"),
        shield_degrade=data.get("shield_degrade"),
        wall_clock_seconds=data.get("wall_clock_seconds"),
    )


def _read_cycle_history(program_id: str, programs_root: Path) -> list[dict[str, Any]]:
    path = _rev_dir(program_id, programs_root) / "cycle_history.jsonl"
    if not path.exists():
        return []
    try:
        return [r for r in read_jsonl_records(path) if isinstance(r, dict)]
    except OSError as exc:
        log.warning("rev-health: cycle_history.jsonl unreadable for %s: %s", program_id, exc)
        return []


def _inbox_telemetry(
    program_id: str,
    programs_root: Path,
    *,
    stale_days: int,
    purge_days: int,
) -> InboxTelemetry:
    inbox = _inbox_dir(program_id, programs_root)
    if not inbox.exists():
        return InboxTelemetry(inbox_path=None)

    # Pending intake = .eml in the inbox root (drop zone) + claimed/ (in-flight).
    pending_paths: list[Path] = []
    try:
        pending_paths.extend(sorted(inbox.glob(_EML_GLOB)))
        claimed = inbox / "claimed"
        if claimed.exists():
            pending_paths.extend(sorted(claimed.glob(_EML_GLOB)))
    except OSError as exc:
        log.warning("rev-health: inbox scan failed for %s: %s", program_id, exc)
        return InboxTelemetry(inbox_path=str(inbox))

    now = datetime.now(timezone.utc).timestamp()
    newest_age_days: float | None = None
    if pending_paths:
        mtimes: list[float] = []
        for p in pending_paths:
            try:
                mtimes.append(p.stat().st_mtime)
            except OSError:
                continue
        if mtimes:
            newest_age_days = max(0.0, (now - max(mtimes)) / 86400.0)

    # Quarantine count + reasons + purge candidates.
    quarantine = inbox / "quarantine"
    q_count = 0
    reason_counter: Counter[str] = Counter()
    purge_candidates = 0
    if quarantine.exists():
        try:
            for qf in quarantine.glob(_EML_GLOB):
                q_count += 1
                reason_path = qf.with_suffix(".reason.txt")
                reason = "unknown"
                if reason_path.exists():
                    try:
                        raw = reason_path.read_text(encoding="utf-8").strip()
                        # Reason format: "<prefix>: <detail>" — bucket by prefix.
                        reason = raw.split(":", 1)[0].strip() or raw
                    except OSError:
                        reason = "unreadable"
                reason_counter[reason] += 1
                # Purge candidate if older than purge_days AND not a crash_loop.
                if reason != "crash_loop":
                    try:
                        age_days = (now - qf.stat().st_mtime) / 86400.0
                        if age_days > purge_days:
                            purge_candidates += 1
                    except OSError:
                        pass
        except OSError as exc:
            log.warning("rev-health: quarantine scan failed for %s: %s", program_id, exc)

    return InboxTelemetry(
        inbox_path=str(inbox),
        inbox_newest_age_days=newest_age_days,
        inbox_stale=bool(newest_age_days is not None and newest_age_days > stale_days),
        quarantine_file_count=q_count,
        quarantine_top_reasons=tuple(reason_counter.most_common(3)),
        quarantine_purge_candidates=purge_candidates,
    )


def _quality_floor_corpus_present(program_id: str, programs_root: Path) -> bool:
    return (programs_root / program_id / "_quality" / "rev_labeled_corpus.jsonl").exists()


@dataclass(frozen=True, slots=True)
class RevHealthReport:
    program_id: str
    schema_version: str = REV_HEALTH_SCHEMA_VERSION
    candidates_pending: int = 0
    run_state_distribution: dict[str, int] = field(default_factory=dict)
    verification_state_distribution: dict[str, int] = field(default_factory=dict)
    evidence_vault_count: int = 0
    evidence_retention_distribution: dict[str, int] = field(default_factory=dict)
    content_safety_unavailable: int = 0
    prompt_shields_mode: str = "local_only"   # P1 default; Azure is P0-gated
    shield_degrade: bool = True               # actual runtime value once a cycle ran
    # §5.13 extended metrics
    hydration_fallback_count: int = 0         # how many items hit metadata_only_flagged
    hydration_fallback_rate: float = 0.0      # fallback / total hydrated
    enumeration_completion: dict[str, int] = field(default_factory=dict)
    pending_queue_age_p50_seconds: float | None = None
    pending_queue_age_max_seconds: float | None = None
    legacy_unverified_count: int = 0          # pre-REV candidates without assertions
    # REV-G8b local-import telemetry (P2-7)
    last_cycle: LastCycleSummary | None = None
    llm_fallback_trend: tuple[int, ...] = ()           # last ≤3 cycles' fallback counts
    inbox: InboxTelemetry | None = None
    circuit_breaker_warn: bool = False                  # LLM fallback runaway
    quality_floor_not_established_warn: bool = False   # ≥10 cycles, no labeled corpus
    vault_size_warn: bool = False                      # evidence vault count > 1000
    warnings: tuple[str, ...] = ()                      # aggregated human-readable alerts
    # W3-1: Platform-level completeness vector (§6.1)
    completeness_vector: RealityCompletenessVector | None = None

    def to_dict(self) -> dict[str, Any]:
        last = None
        if self.last_cycle is not None:
            last = {
                "correlation_id": self.last_cycle.correlation_id,
                "stop_category": self.last_cycle.stop_category,
                "candidates_staged": self.last_cycle.candidates_staged,
                "enumerated": self.last_cycle.enumerated,
                "llm_fallback_count": self.last_cycle.llm_fallback_count,
                "shield_degrade": self.last_cycle.shield_degrade,
                "wall_clock_seconds": self.last_cycle.wall_clock_seconds,
            }
        inbox = None
        if self.inbox is not None:
            inbox = {
                "inbox_path": self.inbox.inbox_path,
                "inbox_newest_age_days": self.inbox.inbox_newest_age_days,
                "inbox_stale": self.inbox.inbox_stale,
                "quarantine_file_count": self.inbox.quarantine_file_count,
                "quarantine_top_reasons": [
                    [r, c] for r, c in self.inbox.quarantine_top_reasons
                ],
                "quarantine_purge_candidates": self.inbox.quarantine_purge_candidates,
            }
        return {
            "program_id": self.program_id,
            "schema_version": self.schema_version,
            "candidates_pending": self.candidates_pending,
            "run_state_distribution": self.run_state_distribution,
            "verification_state_distribution": self.verification_state_distribution,
            "evidence_vault_count": self.evidence_vault_count,
            "evidence_retention_distribution": self.evidence_retention_distribution,
            "content_safety_unavailable": self.content_safety_unavailable,
            "prompt_shields_mode": self.prompt_shields_mode,
            "shield_degrade": self.shield_degrade,
            "hydration_fallback_count": self.hydration_fallback_count,
            "hydration_fallback_rate": self.hydration_fallback_rate,
            "enumeration_completion": self.enumeration_completion,
            "pending_queue_age_p50_seconds": self.pending_queue_age_p50_seconds,
            "pending_queue_age_max_seconds": self.pending_queue_age_max_seconds,
            "legacy_unverified_count": self.legacy_unverified_count,
            "last_cycle": last,
            "llm_fallback_trend": list(self.llm_fallback_trend),
            "inbox": inbox,
            "circuit_breaker_warn": self.circuit_breaker_warn,
            "quality_floor_not_established_warn": self.quality_floor_not_established_warn,
            "vault_size_warn": self.vault_size_warn,
            "warnings": list(self.warnings),
            "completeness_vector": self.completeness_vector.to_dict() if self.completeness_vector is not None else None,
        }


def build_rev_health_report(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    inbox_stale_days: int = DEFAULT_INBOX_STALE_DAYS,
    quarantine_purge_days: int = DEFAULT_QUARANTINE_PURGE_DAYS,
) -> RevHealthReport:
    """Aggregate the REV subsystem health for one program (read-only)."""
    run_dist = state_distribution(program_id, programs_root=programs_root)
    assertions = load_verification_assertions(program_id, programs_root=programs_root)
    ver_dist = assertion_state_distribution(assertions)
    candidates = load_pending_candidates(program_id, programs_root=programs_root)
    decisions = load_triage_decisions(program_id, programs_root=programs_root)
    decided_ids = {d.candidate_id for d in decisions}
    pending_candidates = [c for c in candidates if c.candidate_id not in decided_ids]
    candidates_pending = len(pending_candidates)

    # §5.13 pending queue age (p50 + max) — measures queue staleness.
    now = datetime.now(timezone.utc)
    pending_ages: list[float] = []
    for c in pending_candidates:
        if hasattr(c, "staged_at") and c.staged_at is not None:
            age = (now - c.staged_at.astimezone(timezone.utc)).total_seconds()
            pending_ages.append(age)
    pending_queue_age_p50: float | None = None
    pending_queue_age_max: float | None = None
    if pending_ages:
        sorted_ages = sorted(pending_ages)
        mid = len(sorted_ages) // 2
        pending_queue_age_p50 = sorted_ages[mid]
        pending_queue_age_max = sorted_ages[-1]

    # Evidence vault breakdown — iterate every candidate's evidence_refs and
    # load each excerpt's REV metadata sidecar for retention + content-safety.
    retention_dist: dict[str, int] = {}
    content_safety_unavailable = 0
    vault_seen: set[str] = set()
    vault_count = 0
    for candidate in candidates:
        for ref in candidate.evidence_refs:
            if ref.vault_hash in vault_seen:
                continue
            vault_seen.add(ref.vault_hash)
            vault_count += 1
            meta = load_rev_evidence_metadata(
                program_id=program_id, vault_hash=ref.vault_hash, programs_root=programs_root,
            )
            if meta is None:
                continue
            rclass = meta.retention_class or "unknown"
            retention_dist[rclass] = retention_dist.get(rclass, 0) + 1
            if meta.content_safety_result == "unavailable":
                content_safety_unavailable += 1

    # §5.13 hydration fallback rate — from run-state distribution.
    metadata_only_count = run_dist.get(RunState.METADATA_ONLY_STAGED.value, 0)
    total_hydrated = sum(
        run_dist.get(s.value, 0)
        for s in (
            RunState.METADATA_ONLY_STAGED,
            RunState.CANDIDATE_STAGED,
            RunState.CANDIDATE_VERIFIED,
            RunState.ACCEPTED,
            RunState.REJECTED,
        )
    )
    hydration_fallback_rate = metadata_only_count / total_hydrated if total_hydrated > 0 else 0.0

    # §5.13 enumeration completion distribution — derived from run-state counts.
    enumeration_completion = {
        "enumerated": run_dist.get(RunState.ENUMERATED.value, 0),
        "locator_resolved": run_dist.get(RunState.LOCATOR_RESOLVED.value, 0),
        "hydration_required": run_dist.get(RunState.HYDRATION_REQUIRED.value, 0),
        "quarantined": run_dist.get(RunState.QUARANTINED.value, 0),
        "metadata_only": metadata_only_count,
        "candidate_staged": run_dist.get(RunState.CANDIDATE_STAGED.value, 0),
        "candidate_verified": run_dist.get(RunState.CANDIDATE_VERIFIED.value, 0),
        "accepted": run_dist.get(RunState.ACCEPTED.value, 0),
        "rejected": run_dist.get(RunState.REJECTED.value, 0),
    }

    # §5.13 legacy_unverified count — from verification-state distribution.
    legacy_unverified_count = ver_dist.get("legacy_unverified", 0)
    candidates_with_assertions = {a.candidate_id for a in assertions}
    legacy_no_assertion_count = sum(1 for c in candidates if c.candidate_id not in candidates_with_assertions)
    legacy_unverified_count = max(legacy_unverified_count, legacy_no_assertion_count)

    # ----- REV-G8b local-import telemetry (P2-7) -----
    last_cycle = _read_last_cycle(program_id, programs_root)
    history = _read_cycle_history(program_id, programs_root)
    # Fallback trend = last ≤3 cycles' llm_fallback_count (oldest→newest).
    fallback_trend = tuple(
        int(r.get(_HIST_FALLBACK_KEY, 0) or 0)
        for r in history[-3:]
    )
    inbox = _inbox_telemetry(
        program_id, programs_root,
        stale_days=inbox_stale_days, purge_days=quarantine_purge_days,
    )

    # shield_degrade: actual runtime value from last_cycle.json if a cycle ran.
    shield_degrade = True
    if last_cycle is not None and last_cycle.shield_degrade is not None:
        shield_degrade = bool(last_cycle.shield_degrade)

    # Circuit breaker: >50% fallback in last cycle OR ≥3 consecutive cycles
    # each with any fallback.
    circuit_breaker = False
    if last_cycle is not None and last_cycle.llm_fallback_count is not None:
        enum_n = last_cycle.enumerated or 0
        if enum_n > 0 and last_cycle.llm_fallback_count > 0.5 * enum_n:
            circuit_breaker = True
    if len(fallback_trend) >= 3 and all(c > 0 for c in fallback_trend):
        circuit_breaker = True

    # Quality floor not established: ≥10 cycles completed + no labeled corpus.
    floor_warn = bool(
        len(history) >= QUALITY_FLOOR_CYCLES_TRIGGER
        and not _quality_floor_corpus_present(program_id, programs_root)
    )

    vault_size_warn = vault_count > VAULT_COUNT_WARN_THRESHOLD

    warnings: list[str] = []
    if circuit_breaker:
        warnings.append(
            "circuit_breaker: LLM fallback is runaway (>50% last cycle or ≥3 consecutive "
            "fallback cycles) — tune prompt/budget or check VERTEX_AI_DEPLOYMENT"
        )
    if floor_warn:
        warnings.append(
            "quality_floor_not_established: ≥10 cycles completed but no "
            "_quality/rev_labeled_corpus.jsonl — annotate via `vertex ledger triage list`"
        )
    if vault_size_warn:
        warnings.append(
            f"evidence_vault_size: {vault_count} excerpts exceeds {VAULT_COUNT_WARN_THRESHOLD}"
        )
    if inbox is not None and inbox.inbox_stale:
        warnings.append(
            f"inbox_stale: no new .eml in >{inbox_stale_days}d "
            f"(newest {inbox.inbox_newest_age_days:.1f}d ago)"
        )
    if inbox is not None and inbox.quarantine_file_count > QUARANTINE_ALERT_THRESHOLD:
        warnings.append(
            f"quarantine_count: {inbox.quarantine_file_count} > {QUARANTINE_ALERT_THRESHOLD} "
            f"(top reasons: {', '.join(f'{r}={c}' for r, c in inbox.quarantine_top_reasons) or 'none'})"
        )

    # W3-1/2: Compute the platform-level RealityCompletenessVector (incremental —
    # pass already-loaded inbox data to avoid double I/O).
    inbox_newest_age = inbox.inbox_newest_age_days if inbox is not None else None
    inbox_is_stale = inbox.inbox_stale if inbox is not None else False
    try:
        completeness_vector = compute_reality_completeness_vector(
            program_id,
            programs_root=programs_root,
            last_cycle_stop=last_cycle.stop_category if last_cycle else None,
            last_cycle_enumerated=last_cycle.enumerated if last_cycle else None,
            inbox_newest_age_days=inbox_newest_age,
            inbox_stale=inbox_is_stale,
        )
    except Exception as exc:
        log.warning("rev-health: failed to compute completeness vector for %s: %s", program_id, exc)
        completeness_vector = None

    return RevHealthReport(
        program_id=program_id,
        candidates_pending=candidates_pending,
        run_state_distribution=dict(run_dist),
        verification_state_distribution=dict(ver_dist),
        evidence_vault_count=vault_count,
        evidence_retention_distribution=dict(sorted(retention_dist.items())),
        content_safety_unavailable=content_safety_unavailable,
        prompt_shields_mode="local_only",
        shield_degrade=shield_degrade,
        hydration_fallback_count=metadata_only_count,
        hydration_fallback_rate=round(hydration_fallback_rate, 4),
        enumeration_completion=enumeration_completion,
        pending_queue_age_p50_seconds=pending_queue_age_p50,
        pending_queue_age_max_seconds=pending_queue_age_max,
        legacy_unverified_count=legacy_unverified_count,
        last_cycle=last_cycle,
        llm_fallback_trend=fallback_trend,
        inbox=inbox,
        circuit_breaker_warn=circuit_breaker,
        quality_floor_not_established_warn=floor_warn,
        vault_size_warn=vault_size_warn,
        warnings=tuple(warnings),
        completeness_vector=completeness_vector,
    )


def render_rev_health_human(report: RevHealthReport) -> str:
    """Human-readable REV health summary for ``doctor --rev-health``."""
    lines: list[str] = []
    lines.append(f"REV health - program '{report.program_id}'")
    lines.append(f"  prompt_shields_mode: {report.prompt_shields_mode} (shield_degrade={report.shield_degrade})")
    lines.append(f"  candidates pending triage: {report.candidates_pending}")
    if report.pending_queue_age_p50_seconds is not None:
        p50_h = report.pending_queue_age_p50_seconds / 3600
        max_h = (report.pending_queue_age_max_seconds or 0) / 3600
        lines.append(f"  pending queue age: p50={p50_h:.1f}h  max={max_h:.1f}h")
    if report.legacy_unverified_count:
        lines.append(f"  legacy_unverified candidates: {report.legacy_unverified_count} (pre-REV; require rehydration or human approval)")

    # REV-G8b last cycle + fallback trend.
    lc = report.last_cycle
    if lc is not None:
        lines.append(
            f"  last cycle: stop={lc.stop_category} staged={lc.candidates_staged} "
            f"enumerated={lc.enumerated} llm_fallback={lc.llm_fallback_count} "
            f"wall={lc.wall_clock_seconds:.1f}s shield_degrade={lc.shield_degrade}"
            if lc.wall_clock_seconds is not None
            else f"  last cycle: stop={lc.stop_category} staged={lc.candidates_staged} "
            f"enumerated={lc.enumerated} llm_fallback={lc.llm_fallback_count}"
        )
        lines.append(f"    correlation: {lc.correlation_id}")
    else:
        lines.append("  last cycle: (none — no REV cycle run yet)")
    if report.llm_fallback_trend:
        lines.append(
            f"  llm_fallback trend (last {len(report.llm_fallback_trend)} cycles): "
            f"{list(report.llm_fallback_trend)}"
        )

    # REV-G8b inbox + quarantine telemetry.
    if report.inbox is not None:
        ib = report.inbox
        if ib.inbox_path is None:
            lines.append("  local inbox: (not found at canonical programs/<id>/rev_inbox path)")
        else:
            if ib.inbox_newest_age_days is not None:
                lines.append(
                    f"  inbox: newest pending file {ib.inbox_newest_age_days:.1f}d ago"
                    + ("  [STALE]" if ib.inbox_stale else "")
                )
            else:
                lines.append("  inbox: no pending .eml files")
            lines.append(
                f"  quarantine: {ib.quarantine_file_count} file(s)"
                + (f"  purge_candidates={ib.quarantine_purge_candidates}" if ib.quarantine_purge_candidates else "")
            )
            if ib.quarantine_top_reasons:
                reasons = ", ".join(f"{r}={c}" for r, c in ib.quarantine_top_reasons)
                lines.append(f"    top reasons: {reasons}")

    if report.run_state_distribution:
        lines.append("  run-state distribution:")
        for state, count in sorted(report.run_state_distribution.items()):
            lines.append(f"    {state}: {count}")
    else:
        lines.append("  run-state distribution: (none — no REV runs yet)")
    if report.verification_state_distribution:
        lines.append("  verification-state distribution:")
        for state, count in sorted(report.verification_state_distribution.items()):
            lines.append(f"    {state}: {count}")
    else:
        lines.append("  verification-state distribution: (none)")
    lines.append(f"  evidence vault excerpts: {report.evidence_vault_count}")
    if report.evidence_retention_distribution:
        lines.append("  evidence retention breakdown:")
        for rclass, count in sorted(report.evidence_retention_distribution.items()):
            label = rclass + (" (ledger-governed, not purged)" if rclass == RETENTION_CLASS_ACCEPTED_EVENT else "")
            lines.append(f"    {label}: {count}")
    if report.hydration_fallback_count:
        lines.append(
            f"  hydration fallback: {report.hydration_fallback_count} items "
            f"({report.hydration_fallback_rate * 100:.1f}%) fell back to metadata_only — "
            "body unavailable or blocked by privacy gate."
        )
    if report.enumeration_completion:
        lines.append("  enumeration pipeline breakdown:")
        for stage, count in report.enumeration_completion.items():
            if count:
                lines.append(f"    {stage}: {count}")
    if report.content_safety_unavailable:
        lines.append(
            f"  content_safety=unavailable on {report.content_safety_unavailable} excerpt(s) - "
            "Azure Prompt Shields not configured (P0 operator-gated); local checks remain the deterministic gate."
        )
    if report.warnings:
        lines.append("  WARNINGS:")
        for w in report.warnings:
            lines.append(f"    - {w}")
    # W3-3: completeness vector section
    if report.completeness_vector is not None:
        lines.append(render_completeness_vector_human(report.completeness_vector).rstrip("\n"))
    return "\n".join(lines) + "\n"


__all__ = [
    "RevHealthReport",
    "LastCycleSummary",
    "InboxTelemetry",
    "build_rev_health_report",
    "render_rev_health_human",
    "REV_HEALTH_SCHEMA_VERSION",
    "DEFAULT_INBOX_STALE_DAYS",
    # W3-1: Re-exported for callers that import from health
    "RealityCompletenessVector",
]