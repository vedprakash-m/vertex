from __future__ import annotations

from datetime import date

from src.core.slice_contract_loader import (
    SliceAdoSourceContract,
    SliceContract,
    SliceDecisionSource,
    SliceDegradation,
    SliceFilterDefinition,
    SliceFreshness,
    SliceOwners,
    SlicePredicateDefinition,
    SliceSourceContract,
    SliceTelemetryContract,
)
from tests.support.decision_source_fixtures import build_structured_decision_sources


def build_test_ado_source_contract(
    *,
    saved_queries: tuple[str, ...] = ("query-1",),
    filters: SliceFilterDefinition | None = None,
    explicit_work_item_ids: tuple[int, ...] = (),
    required_fields: tuple[str, ...] = ("state",),
    intentional_filter_only: bool = False,
    intentional_filter_only_expires_on: date | None = None,
) -> SliceAdoSourceContract:
    return SliceAdoSourceContract(
        saved_queries=saved_queries,
        filters=filters,
        explicit_work_item_ids=explicit_work_item_ids,
        required_fields=required_fields,
        intentional_filter_only=intentional_filter_only,
        intentional_filter_only_expires_on=intentional_filter_only_expires_on,
    )



def build_test_slice_contract(
    *,
    contract_id: str = "demo.slice",
    scorecard_name: str = "Demo Scorecard",
    section: str = "demo",
    workstream: str = "demo",
    slice_kind: str = "scorecard_dimension",
    title: str = "Demo Slice",
    source_of_truth: str = "ado_primary",
    primary_owner: str = "owner@example.com",
    support_tpm: str | None = None,
    ado: SliceAdoSourceContract | None = None,
    telemetry: SliceTelemetryContract | None = None,
    fallback_sources: tuple[str, ...] = (),
    decision_sources: tuple[SliceDecisionSource, ...] = (),
    warn_days: int = 5,
    block_days: int = 14,
    blank_filter_is_error: bool = False,
    missing_target_date: str | None = None,
    stale_owner_comment: str | None = None,
    remediation_template: str | None = "Describe current posture.",
    assignment_mode: str = "auto",
    required: bool = True,
) -> SliceContract:
    return SliceContract(
        id=contract_id,
        scorecard_name=scorecard_name,
        section=section,
        workstream=workstream,
        slice_kind=slice_kind,
        title=title,
        source_of_truth=source_of_truth,
        owners=SliceOwners(primary=primary_owner, support_tpm=support_tpm),
        source_contract=SliceSourceContract(
            ado=ado,
            telemetry=telemetry,
            fallback_sources=fallback_sources,
            decision_sources=decision_sources,
        ),
        freshness=SliceFreshness(warn_days=warn_days, block_days=block_days),
        degradation=SliceDegradation(
            blank_filter_is_error=blank_filter_is_error,
            missing_target_date=missing_target_date,
            stale_owner_comment=stale_owner_comment,
        ),
        remediation_template=remediation_template,
        assignment_mode=assignment_mode,
        required=required,
    )


def build_test_source_health_slice_contract(
    *,
    contract_id: str = "demo.slice",
    scorecard_name: str = "Demo",
    section: str = "demo",
    workstream: str = "demo",
    title: str = "Demo Slice",
    source_of_truth: str,
    primary_owner: str = "owner@example.com",
    include_ado: bool = True,
    include_telemetry: bool = True,
    fallback_sources: tuple[str, ...] = (),
    decision_sources: tuple[SliceDecisionSource, ...] = (),
    populate_structured_decision_sources: bool = True,
    required: bool = True,
) -> SliceContract:
    ado = (
        build_test_ado_source_contract(
            saved_queries=("shared-query",),
            filters=SliceFilterDefinition(
                any_of=(
                    SlicePredicateDefinition(
                        field="tag",
                        op="contains",
                        value="Demo",
                    ),
                )
            ),
            explicit_work_item_ids=(),
            required_fields=("state",),
        )
        if include_ado
        else None
    )
    telemetry = (
        SliceTelemetryContract(
            query_id="velocity-p50",
            expected_grain="weekly",
            freshness_sla_hours=24,
            confidence_threshold="high",
            fallback_behavior="warn",
            cross_check_rules=("ado_delta",),
        )
        if include_telemetry
        else None
    )
    resolved_decision_sources = (
        decision_sources
        if decision_sources or not populate_structured_decision_sources
        else build_structured_decision_sources(fallback_sources)
    )
    return build_test_slice_contract(
        contract_id=contract_id,
        scorecard_name=scorecard_name,
        section=section,
        workstream=workstream,
        title=title,
        source_of_truth=source_of_truth,
        primary_owner=primary_owner,
        ado=ado,
        telemetry=telemetry,
        fallback_sources=fallback_sources,
        decision_sources=resolved_decision_sources,
        remediation_template=None,
        required=required,
    )
