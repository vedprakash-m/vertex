"""Launch-readiness quality gates.

Extracted from the ``src/core/quality_gates`` module (D-09 / Phase 3). Evaluates
the configured launch-readiness dimensions against the latest readiness
snapshot, emitting one gate per dimension (unavailable / stale / pass / fail).
Self-contained: depends only on the gate value objects and the readiness engine.
Re-exported from the package ``__init__``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.core.journal import PROGRAMS_ROOT
from src.core.quality_gates.models import GateEvaluation, QualityGateReport
from src.core.readiness_engine import (
    DEFAULT_READINESS_DIMENSIONS,
    is_snapshot_stale,
    load_readiness_config,
    load_readiness_snapshot,
    snapshot_age_days,
)


def evaluate_readiness_gates(
    *,
    program_id: str | None,
    programs_root: Path = PROGRAMS_ROOT,
    max_age_days: int | None = None,
) -> QualityGateReport:
    if program_id is None:
        return QualityGateReport(results=())

    configured_gates, config_warning = _load_configured_readiness_gates(program_id, programs_root=programs_root)
    load_result = load_readiness_snapshot(program_id, programs_root=programs_root)
    snapshot = load_result.snapshot
    warnings = load_result.warnings or ((config_warning,) if config_warning is not None else ())

    if snapshot is None:
        unavailable_message = warnings[0] if warnings else f"Readiness snapshot is unavailable for program '{program_id}'."
        return QualityGateReport(
            results=tuple(
                GateEvaluation(
                    gate_id=gate_id,
                    passed=False,
                    message=f"Launch readiness gate '{name}' unavailable: {unavailable_message}",
                    exit_code=1,
                )
                for gate_id, name in configured_gates
            )
        )

    if is_snapshot_stale(snapshot, max_age_days=max_age_days):
        threshold = snapshot.snapshot_max_age_days if max_age_days is None else max_age_days
        stale_message = (
            f"Readiness snapshot is stale ({snapshot_age_days(snapshot)}d old; max {threshold}d). "
            f"Run `vertex readiness fetch --program {program_id}`."
        )
        gate_pairs = configured_gates or tuple((dimension.gate_id, dimension.name) for dimension in snapshot.dimensions)
        return QualityGateReport(
            results=tuple(
                GateEvaluation(
                    gate_id=gate_id,
                    passed=False,
                    message=f"Launch readiness gate '{name}' unavailable: {stale_message}",
                    exit_code=1,
                )
                for gate_id, name in gate_pairs
            )
        )

    snapshot_by_gate = {dimension.gate_id: dimension for dimension in snapshot.dimensions}
    results: list[GateEvaluation] = []
    seen_gate_ids: set[str] = set()
    for gate_id, name in configured_gates:
        seen_gate_ids.add(gate_id)
        dimension = snapshot_by_gate.get(gate_id)
        if dimension is None:
            results.append(
                GateEvaluation(
                    gate_id=gate_id,
                    passed=False,
                    message=(
                        f"Launch readiness gate '{name}' unavailable: snapshot does not include gate '{gate_id}'. "
                        f"Run `vertex readiness fetch --program {program_id}`."
                    ),
                    exit_code=1,
                )
            )
            continue
        results.append(
            GateEvaluation(
                gate_id=gate_id,
                passed=dimension.passed,
                message=(
                    f"Launch readiness gate '{name}' {'passed' if dimension.passed else 'failed'}: {dimension.summary}"
                ),
                exit_code=1,
            )
        )

    for dimension in snapshot.dimensions:
        if dimension.gate_id in seen_gate_ids:
            continue
        results.append(
            GateEvaluation(
                gate_id=dimension.gate_id,
                passed=dimension.passed,
                message=(
                    f"Launch readiness gate '{dimension.name}' {'passed' if dimension.passed else 'failed'}: {dimension.summary}"
                ),
                exit_code=1,
            )
        )
    return QualityGateReport(results=tuple(results))


def _load_configured_readiness_gates(
    program_id: str,
    *,
    programs_root: Path,
) -> tuple[tuple[tuple[str, str], ...], str | None]:
    try:
        config = load_readiness_config(program_id, programs_root=programs_root)
    except (FileNotFoundError, OSError, yaml.YAMLError, ValueError) as error:
        return _default_readiness_gate_pairs(), str(error)
    except Exception as error:
        return _default_readiness_gate_pairs(), str(error)
    return tuple((dimension.gate_id, dimension.name) for dimension in config.dimensions), None


def _default_readiness_gate_pairs() -> tuple[tuple[str, str], ...]:
    return tuple((gate_id, name) for name, gate_id in DEFAULT_READINESS_DIMENSIONS.values())
