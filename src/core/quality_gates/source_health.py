"""Source-health quality gate (QG-SG-01).

Extracted from the ``src/core/quality_gates`` module (D-09 / Phase 3, §10.8
``source_health.py``). Evaluates whether the configured slice/source contracts
and the transcript channel are healthy enough to confirm (FR-SG-03), emitting a
forceable or hard QG-SG-01 result. Self-contained: depends only on the gate
value objects, gather state, slice contracts, and the ``source_health`` summary
builders. Re-exported from the package ``__init__``.
"""

from __future__ import annotations

from typing import Any

from src.core.gather_state_store import GatherState
from src.core.quality_gates.models import GateEvaluation, QualityGateReport
from src.core.slice_contract_loader import SliceContract
from src.core.source_health import (
    build_slice_source_health_summary,
    build_transcript_source_health,
)


def evaluate_source_health_gates(
    *,
    program_id: str | None,
    edition_name: str,
    slice_contracts: tuple[SliceContract, ...] | None,
    gather_state: GatherState | None,
    waivers: tuple[Any, ...] = (),
    function_name: str = "newsletter",
) -> QualityGateReport:
    if program_id is None:
        return QualityGateReport(results=())
    resolved_slice_contracts = tuple(slice_contracts or ())
    if gather_state is None:
        if not resolved_slice_contracts:
            return QualityGateReport(results=())
        return QualityGateReport(
            results=(
                GateEvaluation(
                    gate_id="QG-SG-01",
                    passed=False,
                    message=(
                        f"Source health gate unavailable: no gather state recorded for program '{program_id}'. "
                        f"Run `vertex gather --edition {edition_name}` before confirm."
                    ),
                    exit_code=1,
                ),
            )
        )
    # FR-SG-03: transcript fail-loud — check even if no slice contracts configured
    transcript_health = build_transcript_source_health(gather_state, tuple(waivers))
    if not resolved_slice_contracts:
        if transcript_health is not None and transcript_health.blocks_confirm:
            return QualityGateReport(
                results=(
                    GateEvaluation(
                        gate_id="QG-SG-01",
                        passed=False,
                        message=(
                            f"Transcript channel is configured but unhealthy (state={transcript_health.state}). "
                            f"Ensure all workstream series IDs are set before confirming."
                        ),
                        exit_code=1,
                        forceable=transcript_health.state in {"zero_yield", "auth_failed"},
                    ),
                )
            )
        return QualityGateReport(results=())
    summary = build_slice_source_health_summary(
        resolved_slice_contracts,
        gather_state,
        waivers=tuple(waivers),
        function_name=function_name,
    )
    if summary is None:
        return QualityGateReport(results=())
    blocking_roles = tuple(role for role in summary.unhealthy_roles if role.blocks_confirm)
    waived_roles = tuple(role for role in summary.unhealthy_roles if role.waiver is not None)
    # Merge transcript failures into blocking/waived roles
    if transcript_health is not None:
        if transcript_health.blocks_confirm:
            blocking_roles = (*blocking_roles, transcript_health)
        if transcript_health.waiver is not None:
            waived_roles = (*waived_roles, transcript_health)
    forceable_block = bool(blocking_roles) and all(role.state != "unbound" for role in blocking_roles)
    if not blocking_roles:
        message = f"Source health gate passed for {summary.contract_count} slice source contract(s)."
        if waived_roles:
            sampled_waivers = ", ".join(
                sorted(f"{role.contract_id}:{role.role}={role.state}" for role in waived_roles[:3])
            )
            message = (
                f"Source health gate passed with {len(waived_roles)} active waiver(s)"
                f" ({sampled_waivers})."
            )
        return QualityGateReport(
            results=(
                GateEvaluation(
                    gate_id="QG-SG-01",
                    passed=True,
                    message=f"{summary.function.title()} {message[0].lower()}{message[1:]}",
                    exit_code=3,
                    forceable=True,
                ),
            )
        )
    sampled_roles = ", ".join(sorted(f"{role.contract_id}:{role.role}={role.state}" for role in blocking_roles[:3]))
    sampled_summary = f" ({sampled_roles})" if sampled_roles else ""
    remediation_hint = (
        "Fix the slice/source binding before confirming."
        if not forceable_block
        else f"Run `vertex gather --edition {edition_name}` or inspect `vertex doctor --channels` before confirming."
    )
    return QualityGateReport(
        results=(
            GateEvaluation(
                gate_id="QG-SG-01",
                passed=False,
                message=(
                    f"{summary.function.title()} source health gate failed with {len(blocking_roles)} blocking source role(s)"
                    f"{sampled_summary}. {remediation_hint}"
                ),
                exit_code=3,
                forceable=forceable_block,
            ),
        )
    )
