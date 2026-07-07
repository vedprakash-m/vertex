from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.core.config_loader import ScorecardDimensionSettings
from src.core.models import Comment, ConfirmedDimension, EditionType, Revision, RiskLevel, Snapshot, SnapshotItem
from src.core.models import WorkItem
from src.core.scorecard_engine import assign_dimension_items, build_scorecard, matches_filter
from src.core.slice_contract_loader import (
    SliceAdoSourceContract,
    SliceContract,
    SliceFilterDefinition,
    SlicePredicateDefinition,
)
from tests.support.slice_contract_fixtures import build_test_slice_contract


def _build_work_item(
    work_item_id: int,
    *,
    item_type: str,
    area_path: str,
    tags: list[str],
    risk_level: RiskLevel,
    title: str | None = None,
    state: str = "Active",
    assigned_to: str | None = "Vertex Maintainer",
    target_date: date | None = None,
    revisions: list[Revision] | None = None,
    comments: list[Comment] | None = None,
    blocked: bool = False,
) -> WorkItem:
    return WorkItem(
        id=work_item_id,
        type=item_type,
        title=title or f"Item {work_item_id}",
        state=state,
        assigned_to=assigned_to,
        assigned_to_email="operator@example.com" if assigned_to else None,
        area_path=area_path,
        iteration_path="One\\FY26\\Q4",
        target_date=target_date,
        risk_level=risk_level,
        tags=tags,
        custom_fields={"blocked": blocked},
        revisions=revisions or [],
        comments=comments or [],
        fetched_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
    )


def _build_slice_contract(
    *,
    scorecard_name: str,
    title: str,
    filter_definition: SliceFilterDefinition | None = None,
    explicit_work_item_ids: tuple[int, ...] = (),
    saved_queries: tuple[str, ...] = (),
    assignment_mode: str = "auto",
    blank_filter_is_error: bool = True,
) -> SliceContract:
    return build_test_slice_contract(
        contract_id=f"{scorecard_name.lower()}::{title.lower()}".replace(" ", "-"),
        scorecard_name=scorecard_name,
        section="scorecard",
        workstream="acme",
        title=title,
        primary_owner="Vertex Maintainer",
        support_tpm="Vertex Maintainer",
        ado=SliceAdoSourceContract(
            saved_queries=saved_queries,
            filters=filter_definition,
            explicit_work_item_ids=explicit_work_item_ids,
            required_fields=("state", "assigned_to"),
        ),
        warn_days=5,
        block_days=10,
        blank_filter_is_error=blank_filter_is_error,
        missing_target_date="warn",
        stale_owner_comment="warn",
        remediation_template=None,
        assignment_mode=assignment_mode,
    )


def test_build_scorecard_aggregates_signals_and_carries_prior_risk() -> None:
    stale_revision = Revision(
        work_item_id=1,
        rev_number=1,
        changed_by="Alice",
        changed_by_email="alice@example.com",
        changed_date=datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc),
        fields_changed={"State": ("New", "Active")},
    )
    active_comment = Comment(
        work_item_id=2,
        comment_id=1,
        created_by="Bob",
        created_by_email="bob@example.com",
        created_date=datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc),
        text="Recent update.",
    )
    high_activity_revisions = [
        Revision(
            work_item_id=3,
            rev_number=index,
            changed_by="Carol",
            changed_by_email="carol@example.com",
            changed_date=datetime(2026, 5, 18 + index, 9, 0, tzinfo=timezone.utc),
            fields_changed={"State": ("Active", "Active")},
        )
        for index in range(3)
    ]
    items = (
        _build_work_item(
            1,
            item_type="Feature",
            area_path="One\\Adventure\\Deployment",
            tags=["blocked"],
            risk_level=RiskLevel.HIGH,
            revisions=[stale_revision],
            blocked=True,
        ),
        _build_work_item(
            2,
            item_type="Risk",
            area_path="One\\Adventure\\Networking",
            tags=["SCHIE"],
            risk_level=RiskLevel.MEDIUM,
            assigned_to=None,
            target_date=date(2026, 5, 1),
            comments=[active_comment],
        ),
        _build_work_item(
            3,
            item_type="Risk",
            area_path="One\\Adventure\\Deployment",
            tags=["Velocity"],
            risk_level=RiskLevel.LOW,
            revisions=high_activity_revisions,
        ),
    )
    prev_snapshot = Snapshot(
        issue_number=77,
        generated_at=datetime(2026, 5, 12, 9, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=1,
                type="Feature",
                title="Old",
                state="Active",
                assigned_to="Vertex Maintainer",
                area_path="One\\Adventure\\Deployment",
                target_date=None,
                risk_level=RiskLevel.LOW,
                tags=[],
            ),
        ),
        scorecards=(
            ConfirmedDimension(
                scorecard_name="Acme Readiness",
                name="Deployment Velocity",
                risk=RiskLevel.MEDIUM,
                prior_risk=RiskLevel.LOW,
                item_count=2,
                ado_query_url="https://dev.azure.com/query",
            ),
        ),
    )
    dimensions = (
        ScorecardDimensionSettings(
            name="Deployment Velocity",
            description="Deployment health",
            ado_filter="area_path contains 'Deployment' OR type eq 'Risk'",
        ),
    )

    packets = build_scorecard(items, dimensions, prev_snapshot, scorecard_name="Acme Readiness")

    assert len(packets) == 1
    packet = packets[0]
    assert packet.total_items == 3
    assert packet.items_by_risk == {"high": 1, "medium": 1, "low": 1}
    assert packet.stale_items == (1,)
    assert packet.overdue_items == (2,)
    assert packet.blocked_items == (1,)
    assert packet.unowned_items == (2,)
    assert packet.high_activity_items == (3,)
    assert packet.prior_confirmed_risk == RiskLevel.MEDIUM
    assert packet.derived_risk == RiskLevel.HIGH
    assert packet.author_risk is None
    assert packet.item_links[0].endswith("/1")


@pytest.mark.parametrize(
    ("filter_expression", "item", "expected"),
    (
        (
            "tag contains 'SCHIE'",
            _build_work_item(10, item_type="Risk", area_path="One\\X", tags=["SCHIE", "MAP"], risk_level=RiskLevel.HIGH),
            True,
        ),
        (
            "type eq 'Risk'",
            _build_work_item(11, item_type="Feature", area_path="One\\X", tags=[], risk_level=RiskLevel.HIGH),
            False,
        ),
        (
            "tag contains 'SCHIE' AND type eq 'Risk'",
            _build_work_item(12, item_type="Risk", area_path="One\\X", tags=["SCHIE"], risk_level=RiskLevel.HIGH),
            True,
        ),
        (
            "tag contains 'A' OR tag contains 'B'",
            _build_work_item(13, item_type="Risk", area_path="One\\X", tags=["B"], risk_level=RiskLevel.HIGH),
            True,
        ),
        (
            "",
            _build_work_item(14, item_type="Feature", area_path="One\\X", tags=[], risk_level=RiskLevel.LOW),
            True,
        ),
    ),
)
def test_ado_filter_parser_cases(filter_expression: str, item: WorkItem, expected: bool) -> None:
    assert matches_filter(item, filter_expression) is expected


def test_ado_filter_malformed_expression_raises() -> None:
    item = _build_work_item(15, item_type="Risk", area_path="One\\X", tags=[], risk_level=RiskLevel.HIGH)
    with pytest.raises(ValueError, match="Malformed filter"):
        matches_filter(item, "tag blah 'X'")


def test_ado_filter_unknown_field_raises() -> None:
    item = _build_work_item(16, item_type="Risk", area_path="One\\X", tags=[], risk_level=RiskLevel.HIGH)
    with pytest.raises(ValueError, match="Unknown field name"):
        matches_filter(item, "foo contains 'X'")


def test_build_scorecard_uses_slice_contract_filters_instead_of_blank_ado_filter() -> None:
    items = (
        _build_work_item(
            21,
            item_type="Feature",
            area_path="One\\Adventure\\XDirect\\Storage",
            tags=["DDPFPilot", "PerfTesting"],
            risk_level=RiskLevel.HIGH,
            title="[Acme-DD] Performance Signoff",
        ),
        _build_work_item(
            22,
            item_type="Feature",
            area_path="One\\Adventure\\HWHealth\\XSSE",
            tags=["DDPFXSSE"],
            risk_level=RiskLevel.MEDIUM,
            title="[DD on Acme] GDCO Ticket Automation Validation",
        ),
        _build_work_item(
            23,
            item_type="Feature",
            area_path="One\\Adventure\\XDirect\\Storage",
            tags=["DDPFPilot"],
            risk_level=RiskLevel.LOW,
            title="Unrelated DD item",
        ),
    )
    dimensions = (
        ScorecardDimensionSettings(name="Performance", description=None, ado_filter=""),
    )
    slice_contract = _build_slice_contract(
        scorecard_name="Contoso Pilot Readiness",
        title="Performance",
        filter_definition=SliceFilterDefinition(
            any_of=(
                SlicePredicateDefinition(field="title", op="contains", value="Performance"),
                SlicePredicateDefinition(field="title", op="contains", value="Signoff"),
            )
        ),
    )

    packets = build_scorecard(
        items,
        dimensions,
        None,
        scorecard_name="Contoso Pilot Readiness",
        slice_contracts={(slice_contract.scorecard_name, slice_contract.title): slice_contract},
    )

    assert packets[0].total_items == 1
    assert packets[0].items_by_risk == {"high": 1}
    assert packets[0].derived_risk == RiskLevel.HIGH
    assert "title%20contains%20%27Performance%27" in packets[0].ado_query_url


def test_assign_dimension_items_prefers_saved_query_url_when_slice_is_anchored() -> None:
    items = (
        _build_work_item(
            41,
            item_type="Feature",
            area_path="One\\Adventure\\Deployment",
            tags=["Acme Deployment"],
            risk_level=RiskLevel.MEDIUM,
            title="Deployment blocker",
        ),
    )
    dimension = ScorecardDimensionSettings(name="Deployment Velocity", description=None, ado_filter="tag contains 'Acme Deployment'")
    slice_contract = _build_slice_contract(
        scorecard_name="Acme Readiness",
        title="Deployment Velocity",
        filter_definition=SliceFilterDefinition(
            any_of=(
                SlicePredicateDefinition(field="tag", op="contains", value="Acme Deployment"),
            )
        ),
        saved_queries=("a772129c-ec88-4fb6-a7bc-d2f2d8d5fd25",),
    )

    assignment = assign_dimension_items(
        items,
        dimension,
        slice_contract=slice_contract,
        ado_query_base_url="https://dev.azure.com/your-org/One/_queries/query",
    )

    assert assignment.items == items
    assert assignment.ado_query_url == "https://dev.azure.com/your-org/One/_queries/query/a772129c-ec88-4fb6-a7bc-d2f2d8d5fd25"


def test_assign_dimension_items_limits_saved_query_slices_to_their_query_scope() -> None:
    items = (
        _build_work_item(
            41,
            item_type="Feature",
            area_path="One\\Adventure\\Deployment",
            tags=["Acme Deployment"],
            risk_level=RiskLevel.MEDIUM,
            title="Deployment blocker",
        ),
        _build_work_item(
            42,
            item_type="Feature",
            area_path="One\\Rome\\ShiftLeft",
            tags=["Acme Deployment"],
            risk_level=RiskLevel.LOW,
            title="K8s deployment per team",
        ),
        _build_work_item(
            43,
            item_type="Feature",
            area_path="One\\Adventure\\Deployment",
            tags=["Acme Deployment"],
            risk_level=RiskLevel.LOW,
            title="Deployment item missing provenance",
        ),
    )
    items[0].custom_fields["saved_query_ids"] = ("a772129c-ec88-4fb6-a7bc-d2f2d8d5fd25",)
    items[1].custom_fields["saved_query_ids"] = ("9f49512b-7037-49dd-ade4-bc1a8a9222d0",)

    dimension = ScorecardDimensionSettings(name="Deployment Velocity", description=None, ado_filter="tag contains 'Acme Deployment'")
    slice_contract = _build_slice_contract(
        scorecard_name="Acme Readiness",
        title="Deployment Velocity",
        filter_definition=SliceFilterDefinition(
            any_of=(
                SlicePredicateDefinition(field="tag", op="contains", value="Acme Deployment"),
            )
        ),
        saved_queries=("a772129c-ec88-4fb6-a7bc-d2f2d8d5fd25",),
    )

    assignment = assign_dimension_items(items, dimension, slice_contract=slice_contract)

    assert tuple(item.id for item in assignment.items) == (41,)


def test_assign_dimension_items_keeps_explicit_ids_outside_saved_query_scope() -> None:
    items = (
        _build_work_item(
            61,
            item_type="Feature",
            area_path="One\\Adventure\\Deployment",
            tags=["Acme Deployment"],
            risk_level=RiskLevel.MEDIUM,
            title="Scoped deployment blocker",
        ),
        _build_work_item(
            62,
            item_type="Feature",
            area_path="One\\Adventure\\Networking",
            tags=["Acme Deployment"],
            risk_level=RiskLevel.LOW,
            title="Explicit networking follow-through",
        ),
    )
    items[0].custom_fields["saved_query_ids"] = ("a772129c-ec88-4fb6-a7bc-d2f2d8d5fd25",)
    items[1].custom_fields["saved_query_ids"] = ("9f49512b-7037-49dd-ade4-bc1a8a9222d0",)

    dimension = ScorecardDimensionSettings(name="Deployment Velocity", description=None, ado_filter="tag contains 'Acme Deployment'")
    slice_contract = _build_slice_contract(
        scorecard_name="Acme Readiness",
        title="Deployment Velocity",
        filter_definition=SliceFilterDefinition(
            any_of=(
                SlicePredicateDefinition(field="tag", op="contains", value="Acme Deployment"),
            )
        ),
        explicit_work_item_ids=(62,),
        saved_queries=("a772129c-ec88-4fb6-a7bc-d2f2d8d5fd25",),
    )

    assignment = assign_dimension_items(items, dimension, slice_contract=slice_contract)

    assert tuple(item.id for item in assignment.items) == (61, 62)


def test_assign_dimension_items_intersects_dimension_and_slice_contract_filters() -> None:
    items = (
        _build_work_item(
            51,
            item_type="Feature",
            area_path="One\\Adventure\\Deployment",
            tags=["Acme Deployment"],
            risk_level=RiskLevel.MEDIUM,
            title="Deployment blocker",
        ),
        _build_work_item(
            52,
            item_type="Feature",
            area_path="One\\Rome\\ShiftLeft",
            tags=["Acme Deployment"],
            risk_level=RiskLevel.LOW,
            title="K8s deployment per team",
        ),
    )
    items[0].custom_fields["saved_query_ids"] = ("a772129c-ec88-4fb6-a7bc-d2f2d8d5fd25",)
    items[1].custom_fields["saved_query_ids"] = ("a772129c-ec88-4fb6-a7bc-d2f2d8d5fd25",)

    dimension = ScorecardDimensionSettings(
        name="Deployment Velocity",
        description=None,
        ado_filter="area_path contains 'Deployment' AND type eq 'Feature'",
    )
    slice_contract = _build_slice_contract(
        scorecard_name="Acme Readiness",
        title="Deployment Velocity",
        filter_definition=SliceFilterDefinition(
            any_of=(
                SlicePredicateDefinition(field="title", op="contains", value="Deployment"),
            )
        ),
        saved_queries=("a772129c-ec88-4fb6-a7bc-d2f2d8d5fd25",),
    )

    assignment = assign_dimension_items(items, dimension, slice_contract=slice_contract)

    assert tuple(item.id for item in assignment.items) == (51,)


def test_assign_dimension_items_raises_for_blank_auto_slice_when_enforced() -> None:
    dimension = ScorecardDimensionSettings(name="Performance", description=None, ado_filter="")
    slice_contract = _build_slice_contract(
        scorecard_name="Contoso Pilot Readiness",
        title="Performance",
        filter_definition=None,
    )

    with pytest.raises(ValueError, match="blank assignment rules"):
        assign_dimension_items((), dimension, slice_contract=slice_contract)


def test_assign_dimension_items_manual_only_uses_explicit_item_ids() -> None:
    items = (
        _build_work_item(
            31,
            item_type="Feature",
            area_path="One\\Adventure\\XDirect\\Storage",
            tags=["DDPFPilot"],
            risk_level=RiskLevel.HIGH,
            title="Performance Signoff",
        ),
        _build_work_item(
            32,
            item_type="Feature",
            area_path="One\\Adventure\\XDirect\\Control",
            tags=["DDPFPilot"],
            risk_level=RiskLevel.MEDIUM,
            title="Manual only item",
        ),
    )
    dimension = ScorecardDimensionSettings(name="Control Plane", description=None, ado_filter="")
    slice_contract = _build_slice_contract(
        scorecard_name="Contoso Pilot Readiness",
        title="Control Plane",
        explicit_work_item_ids=(32,),
        assignment_mode="manual_only",
        blank_filter_is_error=False,
    )

    assignment = assign_dimension_items(items, dimension, slice_contract=slice_contract)

    assert tuple(item.id for item in assignment.items) == (32,)
    assert assignment.ado_query_url == ""



def _dfd_dimension(*, sensitive: bool) -> ScorecardDimensionSettings:
    return ScorecardDimensionSettings(
        name="Deployment Velocity",
        description="Deployment health",
        ado_filter="type eq 'Risk'",
        dfd_proximity_sensitive=sensitive,
    )


def _dfd_item() -> WorkItem:
    return _build_work_item(
        1,
        item_type="Risk",
        area_path="One\\Deployment",
        tags=[],
        risk_level=RiskLevel.MEDIUM,
    )


def test_build_scorecard_dfd_annotation_within_window() -> None:
    from src.core.overrides_store import GovernanceState

    governance = GovernanceState(dfd_date=date(2026, 6, 3))
    packets = build_scorecard(
        (_dfd_item(),),
        (_dfd_dimension(sensitive=True),),
        None,
        governance=governance,
        today=date(2026, 5, 24),
    )
    assert packets[0].dfd_annotation == "DFD: 2026-06-03"
    assert packets[0].escalation_badge == ""


def test_build_scorecard_dfd_annotation_overdue() -> None:
    from src.core.overrides_store import GovernanceState

    governance = GovernanceState(dfd_date=date(2026, 5, 1))
    packets = build_scorecard(
        (_dfd_item(),),
        (_dfd_dimension(sensitive=True),),
        None,
        governance=governance,
        today=date(2026, 5, 24),
    )
    assert packets[0].dfd_annotation == "⚠️ DFD Overdue"


def test_build_scorecard_dfd_annotation_outside_window() -> None:
    from src.core.overrides_store import GovernanceState

    governance = GovernanceState(dfd_date=date(2026, 7, 30))
    packets = build_scorecard(
        (_dfd_item(),),
        (_dfd_dimension(sensitive=True),),
        None,
        governance=governance,
        today=date(2026, 5, 24),
    )
    assert packets[0].dfd_annotation == ""


def test_build_scorecard_dfd_annotation_skipped_when_not_sensitive() -> None:
    from src.core.overrides_store import GovernanceState

    governance = GovernanceState(dfd_date=date(2026, 6, 3))
    packets = build_scorecard(
        (_dfd_item(),),
        (_dfd_dimension(sensitive=False),),
        None,
        governance=governance,
        today=date(2026, 5, 24),
    )
    assert packets[0].dfd_annotation == ""


def test_build_scorecard_escalation_badge_active() -> None:
    from src.core.overrides_store import GovernanceState

    governance = GovernanceState(escalation_active=True, escalation_workstreams=("ws-a",))
    packets = build_scorecard(
        (_dfd_item(),),
        (_dfd_dimension(sensitive=True),),
        None,
        governance=governance,
        today=date(2026, 5, 24),
    )
    assert packets[0].escalation_badge == "⚠️ LT Escalation Active"


def test_build_scorecard_no_governance_no_annotation() -> None:
    packets = build_scorecard(
        (_dfd_item(),),
        (_dfd_dimension(sensitive=True),),
        None,
        today=date(2026, 5, 24),
    )
    assert packets[0].dfd_annotation == ""
    assert packets[0].escalation_badge == ""
