"""Reality Completeness Vector — W3-1/2/3 / still-gaps.md §6.1.

Platform-level (cross-source) completeness vector: Kusto + ADO + IcM + REV visibility.
Synthesizes three areas:
  1. Context Coverage   — cross-source acquisition health
  2. Reality Integrity  — projection lineage and SoR activation state
  3. Model Calibration  — corpus / quality floor (corpus-scoped, not live per-program)

Every metric has an explicit unknown-state (None) rather than silently green.
Timer-based TTL invalidation is handled by the caller: the vector should be
re-derived after any fact append / accept / redaction / correction / policy change /
source-health change, and at least once per TTL window.

Zone A only — no Zone B / Zone C imports.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REALITY_COMPLETENESS_SCHEMA_VERSION = "reality_completeness.v1"

# Surfaces ordered: REV local-import → structured-pull
_SURFACE_NAMES: tuple[str, ...] = ("eml", "ics", "ado", "kusto", "icm", "teams")

from src.core.fact_sor_state import AUTHORITY_FAMILIES as _AUTHORITY_FAMILIES  # S-0h: single SoT



# ---------------------------------------------------------------------------
# Area 1 — Context Coverage
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SourceVisibility:
    """Acquisition health for one source surface.

    status values:
      complete    — surface configured + last cycle processed with no terminal failure
      partial     — configured but degraded (fallback / quarantine / no recent cycle)
      unavailable — explicitly disabled or permanently blocked (e.g. Teams unconfirmed)
      unknown     — not enough data to assess (no cycle run yet)
    """

    surface: str
    status: str  # complete | partial | unavailable | unknown
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"surface": self.surface, "status": self.status, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class ContextCoverageArea:
    """Area 1 — cross-source acquisition health.

    enumeration_completeness = observed_required_surfaces / expected_required_surfaces.
    Stable denominator: count of sources configured in program.yaml as non-optional.
    oldest_unprocessed_source_age_seconds: not "queue age" (no scheduler) — age of
    the oldest pending file in the EML inbox root.
    """

    source_visibility: tuple[SourceVisibility, ...]
    observed_required_surfaces: int   # surfaces at status=complete or partial
    expected_required_surfaces: int   # total configured surfaces
    enumeration_completeness: float | None  # None = no cycle run yet
    oldest_unprocessed_source_age_seconds: float | None  # None = unknown

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_visibility": [sv.to_dict() for sv in self.source_visibility],
            "observed_required_surfaces": self.observed_required_surfaces,
            "expected_required_surfaces": self.expected_required_surfaces,
            "enumeration_completeness": self.enumeration_completeness,
            "oldest_unprocessed_source_age_seconds": self.oldest_unprocessed_source_age_seconds,
        }


# ---------------------------------------------------------------------------
# Area 2 — Reality Integrity
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RealityIntegrityArea:
    """Area 2 — projection lineage and SoR activation state.

    lineage_coverage = facts with domain_event_id set / total accepted facts.
    Interim target ≥50%; production target 100% (O-6).
    sor_mode_per_family: current mode for each authority family.
    unresolved_entities: entities the bridge could not bind to an existing entity —
      not yet tracked, always None until W2-9.
    """

    lineage_numerator: int    # accepted facts WITH domain_event_id
    lineage_denominator: int  # total accepted facts
    lineage_coverage: float | None  # None = no facts yet
    sor_mode_per_family: dict[str, str]  # {authority_family: legacy|shadow|primary}
    unresolved_entities: int | None  # None = not yet tracked (W2-9)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lineage_numerator": self.lineage_numerator,
            "lineage_denominator": self.lineage_denominator,
            "lineage_coverage": self.lineage_coverage,
            "sor_mode_per_family": dict(self.sor_mode_per_family),
            "unresolved_entities": self.unresolved_entities,
        }


# ---------------------------------------------------------------------------
# Area 3 — Model Calibration
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ModelCalibrationArea:
    """Area 3 — quality floor (corpus-scoped, not live per-program).

    Values are None when the corpus is absent or the quality check has not been
    run. No single published score until G-floor is calibrated.
    """

    corpus_present: bool
    macro_precision: float | None
    macro_recall: float | None
    macro_f1: float | None
    abstention_rate: float | None
    auto_binding_precision: float | None
    auto_binding_coverage: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_present": self.corpus_present,
            "macro_precision": self.macro_precision,
            "macro_recall": self.macro_recall,
            "macro_f1": self.macro_f1,
            "abstention_rate": self.abstention_rate,
            "auto_binding_precision": self.auto_binding_precision,
            "auto_binding_coverage": self.auto_binding_coverage,
        }


# ---------------------------------------------------------------------------
# Top-level vector
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RealityCompletenessVector:
    """Platform-level cross-source completeness vector (§6.1).

    Composed of three areas. unknown_metrics lists metric names that cannot
    currently be computed (missing data / not yet tracked).

    Invalidation: re-derive on any fact append/accept/redaction/correction/
    policy change/source-health change, or at the configured TTL interval.
    The ``computed_at`` timestamp records when this snapshot was built.
    """

    program_id: str
    computed_at: datetime
    schema_version: str
    context_coverage: ContextCoverageArea
    reality_integrity: RealityIntegrityArea
    model_calibration: ModelCalibrationArea
    unknown_metrics: tuple[str, ...]  # metric names that can't be computed

    def to_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "computed_at": self.computed_at.isoformat(),
            "schema_version": self.schema_version,
            "context_coverage": self.context_coverage.to_dict(),
            "reality_integrity": self.reality_integrity.to_dict(),
            "model_calibration": self.model_calibration.to_dict(),
            "unknown_metrics": list(self.unknown_metrics),
        }


# ---------------------------------------------------------------------------
# Computation helpers
# ---------------------------------------------------------------------------

def _determine_source_visibility(
    program_id: str,
    *,
    programs_root: Path,
    last_cycle_stop: str | None,
    last_cycle_enumerated: int | None,
    inbox_newest_age_days: float | None,
    inbox_stale: bool,
) -> tuple[SourceVisibility, ...]:
    """Build SourceVisibility entries for all known surfaces."""
    from src.core.edition_resolver import load_program  # lazy import, Zone A only

    prog = load_program(program_id, programs_root=programs_root)
    visibilities: list[SourceVisibility] = []

    # --- EML ---
    rev_enabled = (
        prog is not None
        and prog.m365 is not None
        and prog.m365.rev is not None
        and prog.m365.rev.profile != "disabled"
    )
    if not rev_enabled:
        visibilities.append(SourceVisibility("eml", "unavailable", "REV not configured in program.yaml"))
    elif last_cycle_stop is None:
        visibilities.append(SourceVisibility("eml", "unknown", "no REV cycle has run yet"))
    elif last_cycle_stop == "complete" and not inbox_stale:
        visibilities.append(SourceVisibility("eml", "complete"))
    elif inbox_stale:
        age_str = f"{inbox_newest_age_days:.1f}d" if inbox_newest_age_days is not None else "unknown"
        visibilities.append(SourceVisibility("eml", "partial", f"inbox stale ({age_str} since last file)"))
    else:
        visibilities.append(SourceVisibility("eml", "partial", f"last cycle stop_category={last_cycle_stop!r}"))

    # --- ICS ---
    ics_inbox = programs_root / program_id / "rev_ics_inbox"
    if not ics_inbox.exists():
        visibilities.append(SourceVisibility("ics", "unavailable", "rev_ics_inbox directory not found"))
    elif last_cycle_stop is None:
        visibilities.append(SourceVisibility("ics", "unknown", "no REV cycle has run yet"))
    else:
        visibilities.append(SourceVisibility("ics", "partial", "ICS wired but quality floor not established"))

    # --- ADO ---
    ado_enabled = prog is not None and prog.ado is not None
    if ado_enabled:
        visibilities.append(SourceVisibility("ado", "complete", "ADO configured; gather-arm provides structured pull"))
    else:
        visibilities.append(SourceVisibility("ado", "unavailable", "ADO not configured in program.yaml"))

    # --- Kusto ---
    kusto_enabled = prog is not None and prog.kusto is not None
    if kusto_enabled:
        visibilities.append(SourceVisibility("kusto", "complete", "Kusto configured; gather-arm provides structured pull"))
    else:
        visibilities.append(SourceVisibility("kusto", "unavailable", "Kusto not configured in program.yaml"))

    # --- IcM ---
    icm_enabled = (
        prog is not None
        and prog.m365 is not None
        and prog.m365.icm_incidents_url is not None
    )
    if icm_enabled:
        visibilities.append(SourceVisibility("icm", "partial", "IcM configured; integration completeness unverified"))
    else:
        visibilities.append(SourceVisibility("icm", "unavailable", "IcM incidents URL not configured"))

    # --- Teams ---
    # Always unavailable: ZIP export schema unconfirmed (ADR-008 / PS-4).
    visibilities.append(SourceVisibility(
        "teams", "unavailable",
        "Teams local-export ingestion not operational (ZIP export schema unconfirmed)"
    ))

    return tuple(visibilities)


def _compute_lineage_coverage(program_id: str, *, programs_root: Path) -> tuple[int, int, float | None]:
    """Return (numerator, denominator, coverage) from the live accepted fact snapshot."""
    try:
        from src.core.program_fact_store import ProgramFactStore  # lazy; Zone A ok
        store = ProgramFactStore(program_id, db_root=programs_root.parent if programs_root.name == "programs" else programs_root)
        snapshot = store.snapshot()
        facts = snapshot.facts
    except Exception as exc:
        log.warning("reality_completeness: failed to load fact store for %s: %s", program_id, exc)
        return 0, 0, None

    total = len(facts)
    with_lineage = sum(1 for f in facts if f.domain_event_id is not None)
    coverage = with_lineage / total if total > 0 else None
    return with_lineage, total, coverage


def _compute_sor_modes(program_id: str, *, programs_root: Path) -> dict[str, str]:
    """Return current SoR mode for each authority family."""
    try:
        from src.core.fact_sor_state import resolve_family_sor_mode  # lazy; Zone A ok
        return {
            family: resolve_family_sor_mode(program_id, family, programs_root=programs_root)
            for family in _AUTHORITY_FAMILIES
        }
    except Exception as exc:
        log.warning("reality_completeness: failed to load SoR state for %s: %s", program_id, exc)
        return {family: "unknown" for family in _AUTHORITY_FAMILIES}


def _load_quality_metrics(program_id: str, *, programs_root: Path) -> ModelCalibrationArea:
    """Load calibration metrics from _quality/rev_quality_metrics.json if present."""
    corpus_path = programs_root / program_id / "_quality" / "rev_labeled_corpus.jsonl"
    metrics_path = programs_root / program_id / "_quality" / "rev_quality_metrics.json"
    corpus_present = corpus_path.exists()

    if not metrics_path.exists():
        return ModelCalibrationArea(
            corpus_present=corpus_present,
            macro_precision=None,
            macro_recall=None,
            macro_f1=None,
            abstention_rate=None,
            auto_binding_precision=None,
            auto_binding_coverage=None,
        )
    try:
        data = json.loads(metrics_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("reality_completeness: quality metrics unreadable for %s: %s", program_id, exc)
        return ModelCalibrationArea(
            corpus_present=corpus_present,
            macro_precision=None,
            macro_recall=None,
            macro_f1=None,
            abstention_rate=None,
            auto_binding_precision=None,
            auto_binding_coverage=None,
        )

    def _float(key: str) -> float | None:
        v = data.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return ModelCalibrationArea(
        corpus_present=corpus_present,
        macro_precision=_float("macro_precision"),
        macro_recall=_float("macro_recall"),
        macro_f1=_float("macro_f1"),
        abstention_rate=_float("abstention_rate"),
        auto_binding_precision=_float("auto_binding_precision"),
        auto_binding_coverage=_float("auto_binding_coverage"),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_reality_completeness_vector(
    program_id: str,
    *,
    programs_root: Path,
    last_cycle_stop: str | None = None,
    last_cycle_enumerated: int | None = None,
    inbox_newest_age_days: float | None = None,
    inbox_stale: bool = False,
    reference_dt: datetime | None = None,
) -> RealityCompletenessVector:
    """Compute the platform-level reality completeness vector for one program.

    Pass ``last_cycle_stop``, ``last_cycle_enumerated``, ``inbox_newest_age_days``,
    and ``inbox_stale`` from ``RevHealthReport`` to avoid double-loading the
    cycle state (incremental computation — re-derive on any change).

    ``reference_dt`` defaults to UTC now; tests can pass a fixed datetime.
    """
    now = reference_dt or datetime.now(timezone.utc)
    unknown_metrics: list[str] = []

    # --- Area 1: Context Coverage ---
    source_visibility = _determine_source_visibility(
        program_id,
        programs_root=programs_root,
        last_cycle_stop=last_cycle_stop,
        last_cycle_enumerated=last_cycle_enumerated,
        inbox_newest_age_days=inbox_newest_age_days,
        inbox_stale=inbox_stale,
    )

    expected_surfaces = sum(
        1 for sv in source_visibility if sv.status != "unavailable"
    )
    observed_surfaces = sum(
        1 for sv in source_visibility if sv.status in ("complete", "partial")
    )
    completeness: float | None = None
    if expected_surfaces > 0:
        completeness = observed_surfaces / expected_surfaces
    else:
        unknown_metrics.append("enumeration_completeness")

    oldest_age_secs: float | None = None
    if inbox_newest_age_days is not None:
        oldest_age_secs = inbox_newest_age_days * 86400.0

    context_coverage = ContextCoverageArea(
        source_visibility=source_visibility,
        observed_required_surfaces=observed_surfaces,
        expected_required_surfaces=expected_surfaces,
        enumeration_completeness=round(completeness, 4) if completeness is not None else None,
        oldest_unprocessed_source_age_seconds=oldest_age_secs,
    )

    # --- Area 2: Reality Integrity ---
    lin_num, lin_denom, lin_cov = _compute_lineage_coverage(program_id, programs_root=programs_root)
    if lin_denom == 0:
        unknown_metrics.append("lineage_coverage")

    sor_modes = _compute_sor_modes(program_id, programs_root=programs_root)

    reality_integrity = RealityIntegrityArea(
        lineage_numerator=lin_num,
        lineage_denominator=lin_denom,
        lineage_coverage=round(lin_cov, 4) if lin_cov is not None else None,
        sor_mode_per_family=sor_modes,
        unresolved_entities=None,  # W2-9 — not yet tracked
    )
    unknown_metrics.append("unresolved_entities")

    # --- Area 3: Model Calibration ---
    model_calibration = _load_quality_metrics(program_id, programs_root=programs_root)
    if not model_calibration.corpus_present:
        unknown_metrics.extend(["macro_precision", "macro_recall", "macro_f1"])

    return RealityCompletenessVector(
        program_id=program_id,
        computed_at=now,
        schema_version=REALITY_COMPLETENESS_SCHEMA_VERSION,
        context_coverage=context_coverage,
        reality_integrity=reality_integrity,
        model_calibration=model_calibration,
        unknown_metrics=tuple(dict.fromkeys(unknown_metrics)),  # dedup, preserve order
    )


def render_completeness_vector_human(vector: RealityCompletenessVector) -> str:
    """Human-readable summary of the RealityCompletenessVector."""
    lines: list[str] = []
    lines.append(f"  [REV completeness vector] computed_at={vector.computed_at.isoformat()}")

    # Area 1
    cc = vector.context_coverage
    lines.append(f"  Area 1 — Context Coverage:")
    for sv in cc.source_visibility:
        status_tag = f"[{sv.status.upper()}]"
        reason = f"  ({sv.reason})" if sv.reason else ""
        lines.append(f"    {sv.surface:<8} {status_tag}{reason}")
    comp_str = (
        f"{cc.enumeration_completeness * 100:.0f}%"
        if cc.enumeration_completeness is not None else "unknown"
    )
    lines.append(
        f"    enumeration_completeness={comp_str} "
        f"({cc.observed_required_surfaces}/{cc.expected_required_surfaces} surfaces active)"
    )
    if cc.oldest_unprocessed_source_age_seconds is not None:
        age_h = cc.oldest_unprocessed_source_age_seconds / 3600.0
        lines.append(f"    oldest_unprocessed_source_age={age_h:.1f}h")

    # Area 2
    ri = vector.reality_integrity
    lines.append("  Area 2 — Reality Integrity:")
    cov_str = (
        f"{ri.lineage_coverage * 100:.1f}% ({ri.lineage_numerator}/{ri.lineage_denominator} facts)"
        if ri.lineage_coverage is not None else "unknown (no accepted facts yet)"
    )
    lines.append(f"    lineage_coverage={cov_str}")
    lines.append("    sor_mode_per_family:")
    for family, mode in sorted(ri.sor_mode_per_family.items()):
        lines.append(f"      {family}: {mode}")
    lines.append(
        f"    unresolved_entities={'not tracked (W2-9 pending)' if ri.unresolved_entities is None else ri.unresolved_entities}"
    )

    # Area 3
    mc = vector.model_calibration
    lines.append("  Area 3 — Model Calibration:")
    lines.append(f"    corpus_present={mc.corpus_present}")
    if mc.macro_f1 is not None:
        lines.append(
            f"    macro_f1={mc.macro_f1:.3f}  precision={mc.macro_precision}  recall={mc.macro_recall}"
        )
    else:
        lines.append("    quality_floor=NOT ESTABLISHED (no labeled corpus)")

    if vector.unknown_metrics:
        lines.append(f"  unknown_metrics: {', '.join(vector.unknown_metrics)}")

    return "\n".join(lines) + "\n"


__all__ = [
    "RealityCompletenessVector",
    "ContextCoverageArea",
    "RealityIntegrityArea",
    "ModelCalibrationArea",
    "SourceVisibility",
    "compute_reality_completeness_vector",
    "render_completeness_vector_human",
    "REALITY_COMPLETENESS_SCHEMA_VERSION",
]
