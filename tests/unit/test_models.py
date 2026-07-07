from __future__ import annotations

import dataclasses
from datetime import date, datetime, timezone
from typing import get_type_hints

import pytest

from src.core.models import ArchiveEntry, ArchiveIndex, AttributionTier, Comment, Confidence, ConfirmedDimension
from src.core.models import DRISummary, DeltaKind, DeltaSet, DimensionRisk, EditionType, Enrichment
from src.core.models import EvidencePacket, FreshnessItem, FreshnessReport, ItemDelta, LearningEntry
from src.core.models import NotificationRecord, NotifyPreview, PersonProfile, ProgramContext, ReportData
from src.core.models import ReviewSection, ReviewState, ReviewStatus, Revision, RiskLevel, RunManifest
from src.core.models import ScorecardDelta, ScorecardEvidencePacket, Snapshot, SnapshotItem, WorkItem
from src.core.models import Workstream
from src.core.models_v2 import KustoQuery
from src.core.view_models import Citation, DeltaReport, EditionMeta, HealthSummary, ScorecardData
from src.core.view_models import Top3Item, WorkstreamData


FROZEN_MODEL_CLASSES = (
    Revision,
    Comment,
    EvidencePacket,
    Enrichment,
    ScorecardEvidencePacket,
    ItemDelta,
    DeltaSet,
    DimensionRisk,
    ScorecardDelta,
    FreshnessItem,
    FreshnessReport,
    DRISummary,
    ReportData,
    ReviewSection,
    ReviewStatus,
    NotifyPreview,
    NotificationRecord,
    SnapshotItem,
    ConfirmedDimension,
    Snapshot,
    ArchiveEntry,
    ArchiveIndex,
    RunManifest,
    Workstream,
    PersonProfile,
    ProgramContext,
    LearningEntry,
)

FROZEN_VIEW_MODEL_CLASSES = (
    HealthSummary,
    Top3Item,
    Citation,
    WorkstreamData,
    ScorecardData,
    DeltaReport,
    EditionMeta,
)


@pytest.mark.parametrize(
    ("enum_type", "value", "expected"),
    (
        (RiskLevel, "HIGH", RiskLevel.HIGH),
        (RiskLevel, None, RiskLevel.UNKNOWN),
        (Confidence, "medium", Confidence.MEDIUM),
        (Confidence, None, Confidence.NONE),
        (DeltaKind, "risk_up", DeltaKind.RISK_UP),
        (AttributionTier, "TIER2", AttributionTier.TIER2),
        (ReviewState, "approved", ReviewState.APPROVED),
        (EditionType, "focused", EditionType.FOCUSED),
    ),
)
def test_enum_from_string_parses_canonical_values(enum_type: type, value: str | None, expected: object) -> None:
    assert enum_type.from_string(value) == expected


def test_review_state_from_string_rejects_unknown_values() -> None:
    with pytest.raises(ValueError):
        ReviewState.from_string("not_a_state")


@pytest.mark.parametrize("cls", FROZEN_MODEL_CLASSES + FROZEN_VIEW_MODEL_CLASSES)
def test_frozen_dataclass_contracts(cls: type) -> None:
    assert dataclasses.is_dataclass(cls)
    assert cls.__dataclass_params__.frozen is True
    assert hasattr(cls, "__slots__")


def test_work_item_is_slotted_but_mutable_with_independent_defaults() -> None:
    first = WorkItem(
        id=1,
        type="Feature",
        title="Title",
        state="Active",
        assigned_to="Operator",
        assigned_to_email="operator@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 6, 30),
        risk_level=RiskLevel.MEDIUM,
        tags=["acme"],
        custom_fields={"score": 1},
    )
    second = WorkItem(
        id=2,
        type="Risk",
        title="Other",
        state="Active",
        assigned_to=None,
        assigned_to_email=None,
        area_path="One\\Adventure\\Contoso",
        iteration_path="One\\FY26\\Q4",
        target_date=None,
        risk_level=RiskLevel.HIGH,
        tags=[],
        custom_fields={},
    )

    first.revisions.append(
        Revision(
            work_item_id=1,
            rev_number=2,
            changed_by="Operator",
            changed_by_email="operator@example.com",
            changed_date=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
            fields_changed={"State": ("New", "Active")},
        )
    )

    assert dataclasses.is_dataclass(WorkItem)
    assert WorkItem.__dataclass_params__.frozen is False
    assert hasattr(WorkItem, "__slots__")
    assert len(first.revisions) == 1
    assert second.revisions == []
    assert first.comments is not second.comments
    assert first.fetched_at.tzinfo == timezone.utc


def test_model_properties_and_annotations_match_the_spec_contract() -> None:
    freshness = FreshnessReport(issue_number=7, items=(), blocks=0, warns=1, infos=2)
    review_status = ReviewStatus(
        issue_number=7,
        sections=(
            ReviewSection(
                section_id="exec_summary",
                state=ReviewState.APPROVED,
                reviewer="lead@example.com",
                note=None,
                updated_at=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
            ),
        ),
    )

    report_hints = get_type_hints(ReportData)
    confirmed_dimension_hints = get_type_hints(ConfirmedDimension)

    assert freshness.is_clean is True
    assert review_status.all_approved is True
    assert report_hints["exec_summary_text"] is str
    assert confirmed_dimension_hints["scorecard_name"] is str
    assert "overrides.yaml" in (DimensionRisk.__doc__ or "")


def test_kusto_query_supports_phase1_extension_fields_with_safe_defaults() -> None:
    query = KustoQuery(
        id="acme-deployment-p50-p90",
        cluster="https://xdeployment.kusto.windows.net",
        database="Deployment",
        kql="PFDeployments | take 1",
        section="Deployment Velocity",
        render_as="metric_highlight",
        confidence="medium",
    )

    assert query.catalog_source is None
    assert query.validated_at is None
    assert query.owner_alias is None
    assert query.expected_cardinality == "zero_ok"
    assert query.kusto_no_safety is False
    assert query.last_cycle_succeeded is None
    assert query.metric_id is None
    assert query.assertion_ids == ()


def test_kusto_query_accepts_phase1_extension_fields() -> None:
    validated_at = datetime(2026, 5, 17, 14, 2, tzinfo=timezone.utc)

    query = KustoQuery(
        id="acme-readiness-scorecard",
        cluster="https://apdmdata.kusto.windows.net",
        database="DeviceManager",
        kql="print Current=0.0",
        section="Readiness Scorecard",
        render_as="metric_highlight",
        confidence="low",
        catalog_source={"dashboard_name": "Acme Readiness", "page_name": "Readiness Scorecard"},
        validated_at=validated_at,
        owner_alias="testowner",
        expected_cardinality="scalar_required",
        kusto_no_safety=True,
        last_cycle_succeeded=True,
        metric_id="acme.readiness_scorecard",
        assertion_ids=("assertion-001", "assertion-002"),
    )

    assert query.catalog_source == {"dashboard_name": "Acme Readiness", "page_name": "Readiness Scorecard"}
    assert query.validated_at == validated_at
    assert query.owner_alias == "testowner"
    assert query.expected_cardinality == "scalar_required"
    assert query.kusto_no_safety is True
    assert query.last_cycle_succeeded is True
    assert query.metric_id == "acme.readiness_scorecard"
    assert query.assertion_ids == ("assertion-001", "assertion-002")
