from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from src.core.config_loader import NarrativeProgramContext, ProgramPerson, ProgramWorkstream
from src.core.freshness_engine import build_dri_summaries, build_freshness_report
from src.core.models import Comment, FreshnessItem, FreshnessReport, NotifiedWorkItemState, PriorNotificationState, Revision, RiskLevel, Snapshot, SnapshotItem, WorkItem


AS_OF = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)


def test_build_freshness_report_emits_core_findings() -> None:
    current_items = (
        _work_item(
            101,
            state="Active",
            target_date=date(2026, 5, 1),
            assigned_to="Jordan Rivera",
            assigned_to_email="jordan@example.com",
            changed_date=AS_OF - timedelta(days=10),
        ),
        _work_item(
            102,
            state="Active",
            target_date=date(2026, 6, 1),
            assigned_to="Jordan Rivera",
            assigned_to_email="jordan@example.com",
            changed_date=AS_OF - timedelta(days=20),
        ),
        _work_item(
            103,
            state="At Risk",
            risk_level=RiskLevel.MEDIUM,
            assigned_to="Priya Mehta",
            assigned_to_email="priya@example.com",
            changed_date=AS_OF - timedelta(days=2),
        ),
        _work_item(
            104,
            state="Done",
            risk_level=RiskLevel.DONE,
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            changed_date=AS_OF - timedelta(days=1),
        ),
        _work_item(
            105,
            state="Active",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            changed_date=AS_OF - timedelta(days=1),
        ),
        _work_item(
            106,
            state="Active",
            assigned_to="Sam Rivera",
            assigned_to_email="sam@example.com",
            changed_date=AS_OF - timedelta(days=1),
            comments=(
                _comment(106, 1, AS_OF - timedelta(days=3), "Investigating rollout"),
                _comment(106, 2, AS_OF - timedelta(days=2), "Validation in progress"),
            ),
            revisions=(
                _revision(106, 2, AS_OF - timedelta(days=1), {"System.State": ("Proposed", "Active")}),
            ),
        ),
        _work_item(
            107,
            state="At Risk",
            risk_level=RiskLevel.MEDIUM,
            assigned_to="Priya Mehta",
            assigned_to_email="priya@example.com",
            changed_date=AS_OF - timedelta(days=1),
            description="WIP, updating soon",
        ),
        _work_item(
            108,
            state="Active",
            target_date=date(2026, 5, 8),
            assigned_to="Priya Mehta",
            assigned_to_email="priya@example.com",
            changed_date=AS_OF - timedelta(days=2),
        ),
        _work_item(
            109,
            state="Active",
            assigned_to=None,
            assigned_to_email=None,
            changed_date=AS_OF - timedelta(days=3),
        ),
    )
    previous_snapshot = _snapshot(
        _snapshot_item(101, state="Active", risk_level=RiskLevel.LOW, target_date=date(2026, 5, 10), assigned_to="Jordan Rivera"),
        _snapshot_item(102, state="Active", risk_level=RiskLevel.LOW, target_date=date(2026, 6, 1), assigned_to="Jordan Rivera"),
        _snapshot_item(103, state="Active", risk_level=RiskLevel.LOW, target_date=None, assigned_to="Priya Mehta"),
        _snapshot_item(104, state="Active", risk_level=RiskLevel.MEDIUM, target_date=None, assigned_to="Vertex Maintainer"),
        _snapshot_item(106, state="Proposed", risk_level=RiskLevel.LOW, target_date=None, assigned_to="Sam Rivera"),
        _snapshot_item(107, state="At Risk", risk_level=RiskLevel.MEDIUM, target_date=None, assigned_to="Priya Mehta"),
        _snapshot_item(108, state="Active", risk_level=RiskLevel.LOW, target_date=date(2026, 5, 8), assigned_to="Priya Mehta"),
        _snapshot_item(109, state="Active", risk_level=RiskLevel.LOW, target_date=None, assigned_to=None),
    )

    report = build_freshness_report(
        current_items=current_items,
        issue_number=8,
        as_of=AS_OF,
        stale_warn_days=14,
        stale_block_days=30,
        previous_snapshot=previous_snapshot,
    )

    findings = {(item.work_item_id, item.rule_id): item for item in report.items}
    assert (101, "FR-21") in findings
    assert (102, "FR-22") in findings
    assert (103, "FR-23") in findings
    assert (103, "FR-24") in findings
    assert (105, "FR-25") in findings
    assert (106, "FR-26") in findings
    assert (107, "FR-42") in findings
    assert (107, "FR-44") in findings
    assert (108, "FR-43") in findings
    assert (109, "FR-46") in findings
    assert findings[(101, "FR-21")].action_label == "Overdue"
    assert findings[(101, "FR-21")].action_message is not None
    assert report.blocks == 3
    assert report.warns >= 6
    assert report.infos == 1


def test_build_dri_summaries_groups_findings_by_owner() -> None:
    current_items = (
        _work_item(
            201,
            state="Active",
            target_date=date(2026, 5, 1),
            assigned_to="Jordan Rivera",
            assigned_to_email="jordan@example.com",
            changed_date=AS_OF - timedelta(days=16),
        ),
        _work_item(
            202,
            state="At Risk",
            risk_level=RiskLevel.MEDIUM,
            assigned_to="Jordan Rivera",
            assigned_to_email="jordan@example.com",
            changed_date=AS_OF - timedelta(days=1),
            description="Need update",
        ),
        _work_item(
            203,
            state="Active",
            assigned_to="Priya Mehta",
            assigned_to_email="priya@example.com",
            changed_date=AS_OF - timedelta(days=18),
        ),
    )

    report = build_freshness_report(
        current_items=current_items,
        issue_number=9,
        as_of=AS_OF,
        stale_warn_days=14,
        stale_block_days=30,
        previous_snapshot=_snapshot(_snapshot_item(201), _snapshot_item(202), _snapshot_item(203)),
    )
    summaries = build_dri_summaries(
        report,
        current_items,
        program_context=NarrativeProgramContext(
            schema_version="1.0",
            program_name="Adventure + DD on PF",
            objective="Objective",
            mission=None,
            pillars=(),
            glossary={},
            workstreams=(),
            people=(
                ProgramPerson(
                    email="jordan@example.com",
                    display_name="Jordan Rivera",
                    role=None,
                    workstreams=(),
                ),
                ProgramPerson(
                    email="priya@example.com",
                    display_name="Priya Mehta",
                    role=None,
                    workstreams=(),
                ),
            ),
        ),
    )

    by_email = {summary.dri_email: summary for summary in summaries}
    assert by_email["jordan@example.com"].dri_name == "Jordan Rivera"
    assert by_email["jordan@example.com"].open_count == 2
    assert by_email["jordan@example.com"].overdue_count == 1
    assert by_email["jordan@example.com"].stale_count == 1
    assert by_email["priya@example.com"].dri_name == "Priya Mehta"
    assert by_email["priya@example.com"].open_count == 1


def test_build_dri_summaries_routes_non_responder_to_alternate_owner() -> None:
    current_items = (
        _work_item(
            301,
            state="Active",
            assigned_to="Priya Mehta",
            assigned_to_email="priya@example.com",
            changed_date=AS_OF - timedelta(days=18),
            area_path="One\\Adventure\\Acme\\Deployment",
        ),
    )
    report = FreshnessReport(
        issue_number=9,
        items=(
            FreshnessItem(
                work_item_id=301,
                rule_id="FR-22",
                severity="warn",
                message="No RiskComment, Discussion, or State activity in 18 days.",
                suggested_fix="Ask the owner to refresh the item in ADO before the next published update.",
            ),
            FreshnessItem(
                work_item_id=301,
                rule_id="FR-45",
                severity="warn",
                message="Primary DRI has not responded after notification.",
                suggested_fix="Follow up before the publish deadline.",
            ),
        ),
        blocks=0,
        warns=2,
        infos=0,
    )

    summaries = build_dri_summaries(report, current_items, program_context=_program_context(alternate_owner="backup@example.com"))

    by_email = {summary.dri_email: summary for summary in summaries}
    assert "priya@example.com" not in by_email
    assert by_email["backup@example.com"].dri_name == "Vertex Maintainer"
    assert by_email["backup@example.com"].open_count == 1
    assert [finding.rule_id for finding in by_email["backup@example.com"].items] == ["FR-22", "FR-45"]
    routed_message = next(
        finding.message for finding in by_email["backup@example.com"].items if finding.rule_id == "FR-45"
    )
    assert "Alternate owner: Vertex Maintainer <backup@example.com>" in routed_message
    assert "backup for Priya Mehta <priya@example.com>" in routed_message


def test_build_dri_summaries_flags_missing_alternate_for_non_responder() -> None:
    current_items = (
        _work_item(
            302,
            state="Active",
            assigned_to="Priya Mehta",
            assigned_to_email="priya@example.com",
            changed_date=AS_OF - timedelta(days=18),
        ),
    )
    report = FreshnessReport(
        issue_number=9,
        items=(
            FreshnessItem(
                work_item_id=302,
                rule_id="FR-45",
                severity="warn",
                message="Primary DRI has not responded after notification.",
                suggested_fix="Follow up before the publish deadline.",
            ),
        ),
        blocks=0,
        warns=1,
        infos=0,
    )

    summaries = build_dri_summaries(report, current_items, program_context=_program_context(alternate_owner=None))

    by_email = {summary.dri_email: summary for summary in summaries}
    assert by_email["priya@example.com"].dri_name == "Priya Mehta"
    assert by_email["priya@example.com"].items[0].message.endswith("Owner on PTO — no alternate assigned.")


def test_build_freshness_report_emits_non_responder_findings_from_previous_notify() -> None:
    current_items = (
        _work_item(
            303,
            state="Active",
            assigned_to="Priya Mehta",
            assigned_to_email="priya@example.com",
            changed_date=AS_OF - timedelta(days=5),
            area_path="One\\Adventure\\Acme\\Deployment",
        ),
    )

    report = build_freshness_report(
        current_items=current_items,
        issue_number=10,
        as_of=AS_OF,
        stale_warn_days=14,
        stale_block_days=30,
        previous_snapshot=_snapshot(_snapshot_item(303, assigned_to="Priya Mehta")),
        previous_notification_state=PriorNotificationState(
            notified_at=AS_OF - timedelta(days=2),
            items=(
                NotifiedWorkItemState(
                    work_item_id=303,
                    dri_email="priya@example.com",
                    notified_at=AS_OF - timedelta(days=2),
                ),
            ),
        ),
        program_context=_program_context(alternate_owner=None),
    )

    findings = {(item.work_item_id, item.rule_id): item for item in report.items}
    assert (303, "FR-45") in findings
    assert "previous notify run" in findings[(303, "FR-45")].message
    assert (303, "FR-47") in findings
    assert findings[(303, "FR-47")].message == "Owner on PTO — no alternate assigned."


def test_build_freshness_report_skips_non_responder_after_response_activity() -> None:
    current_items = (
        _work_item(
            304,
            state="Active",
            assigned_to="Priya Mehta",
            assigned_to_email="priya@example.com",
            changed_date=AS_OF - timedelta(days=5),
            comments=(
                _comment(304, 1, AS_OF - timedelta(days=1), "Updated mitigation and next steps"),
            ),
        ),
    )

    report = build_freshness_report(
        current_items=current_items,
        issue_number=10,
        as_of=AS_OF,
        stale_warn_days=14,
        stale_block_days=30,
        previous_snapshot=_snapshot(_snapshot_item(304, assigned_to="Priya Mehta")),
        previous_notification_state=PriorNotificationState(
            notified_at=AS_OF - timedelta(days=2),
            items=(
                NotifiedWorkItemState(
                    work_item_id=304,
                    dri_email="priya@example.com",
                    notified_at=AS_OF - timedelta(days=2),
                ),
            ),
        ),
        program_context=_program_context(alternate_owner=None),
    )

    assert {(item.work_item_id, item.rule_id) for item in report.items} == set()


def test_build_freshness_report_ignores_vertex_comments_for_staleness() -> None:
    current_items = (
        _work_item(
            401,
            state="Active",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            changed_date=AS_OF - timedelta(days=1),
            comments=(
                Comment(
                    work_item_id=401,
                    comment_id=1,
                    created_by="Vertex Bot",
                    created_by_email="vertex-bot@example.com",
                    created_date=AS_OF - timedelta(days=1),
                    text="📋 Vertex Vitality Check — WI:401",
                ),
            ),
            revisions=(
                _revision(401, 1, AS_OF - timedelta(days=18), {"System.State": ("Proposed", "Active")}),
            ),
        ),
    )

    report = build_freshness_report(
        current_items=current_items,
        issue_number=10,
        as_of=AS_OF,
        stale_warn_days=14,
        stale_block_days=30,
        previous_snapshot=_snapshot(_snapshot_item(401, assigned_to="Vertex Maintainer")),
    )

    assert any(item.work_item_id == 401 and item.rule_id == "FR-22" for item in report.items)


def test_build_freshness_report_emits_ghost_change_when_workstream_blurb_is_unchanged() -> None:
    current_items = (
        _work_item(
            305,
            state="Active",
            target_date=date(2026, 5, 20),
            assigned_to="Priya Mehta",
            assigned_to_email="priya@example.com",
            changed_date=AS_OF - timedelta(days=1),
            area_path="One\\Adventure\\Acme\\Deployment",
        ),
    )

    report = build_freshness_report(
        current_items=current_items,
        issue_number=10,
        as_of=AS_OF,
        stale_warn_days=14,
        stale_block_days=30,
        previous_snapshot=_snapshot(
            _snapshot_item(
                305,
                state="Proposed",
                target_date=date(2026, 5, 15),
                assigned_to="Priya Mehta",
            )
        ),
        program_context=_program_context(alternate_owner=None),
        workstream_narrative_history={
            "Acme": (
                "No material change in rollout posture.",
                "No material change in rollout posture.",
                "No material change in rollout posture.",
            )
        },
    )

    findings = {(item.work_item_id, item.rule_id): item for item in report.items}
    assert (305, "FR-26a") in findings


def test_build_freshness_report_emits_copy_paste_warning_from_previous_issue_text() -> None:
    current_items = (
        _work_item(
            306,
            state="Active",
            assigned_to="Priya Mehta",
            assigned_to_email="priya@example.com",
            changed_date=AS_OF - timedelta(days=1),
            description="<p>Rolled to 10 percent monitoring with no new blockers.</p>",
            revisions=(
                _revision(
                    306,
                    2,
                    AS_OF - timedelta(days=1),
                    {
                        "System.Description": (
                            "Rolled to 10 percent monitoring with no new blockers",
                            "<p>Rolled to 10 percent monitoring with no new blockers.</p>",
                        )
                    },
                ),
            ),
        ),
    )

    report = build_freshness_report(
        current_items=current_items,
        issue_number=10,
        as_of=AS_OF,
        stale_warn_days=14,
        stale_block_days=30,
        previous_snapshot=_snapshot(_snapshot_item(306, assigned_to="Priya Mehta")),
    )

    findings = {(item.work_item_id, item.rule_id): item for item in report.items}
    assert (306, "FR-42a") in findings


def test_build_freshness_report_emits_data_unavailable_when_activity_timestamp_is_missing() -> None:
    current_items = (
        WorkItem(
            id=307,
            type="Feature",
            title="Work item 307",
            state="Active",
            assigned_to="Priya Mehta",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="FY26\\Sprint 20",
            target_date=None,
            risk_level=RiskLevel.LOW,
            tags=[],
            custom_fields={},
            revisions=[],
            comments=[],
            fetched_at=AS_OF,
        ),
    )

    report = build_freshness_report(
        current_items=current_items,
        issue_number=10,
        as_of=AS_OF,
        stale_warn_days=14,
        stale_block_days=30,
        previous_snapshot=_snapshot(_snapshot_item(307, assigned_to="Priya Mehta")),
    )

    findings = {(item.work_item_id, item.rule_id): item for item in report.items}
    assert (307, "FR-20") in findings
    assert findings[(307, "FR-20")].severity == "warn"
    assert findings[(307, "FR-20")].action_label == "Data unavailable"
    assert (307, "FR-22") not in findings


def _work_item(
    work_item_id: int,
    *,
    state: str = "Active",
    risk_level: RiskLevel = RiskLevel.LOW,
    target_date: date | None = None,
    assigned_to: str | None = None,
    assigned_to_email: str | None = None,
    area_path: str = "One\\Adventure\\Acme\\Deployment",
    changed_date: datetime | None = None,
    description: str | None = None,
    comments: tuple[Comment, ...] = (),
    revisions: tuple[Revision, ...] = (),
) -> WorkItem:
    custom_fields: dict[str, object] = {}
    if changed_date is not None:
        custom_fields["changed_date"] = changed_date.isoformat()
    if description is not None:
        custom_fields["description"] = description
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title=f"Work item {work_item_id}",
        state=state,
        assigned_to=assigned_to,
        assigned_to_email=assigned_to_email,
        area_path=area_path,
        iteration_path="FY26\\Sprint 20",
        target_date=target_date,
        risk_level=risk_level,
        tags=[],
        custom_fields=custom_fields,
        revisions=list(revisions),
        comments=list(comments),
        fetched_at=AS_OF,
    )


def _snapshot(*items: SnapshotItem) -> Snapshot:
    return Snapshot(
        issue_number=7,
        generated_at=AS_OF - timedelta(days=7),
        ado_data_as_of=AS_OF - timedelta(days=7),
        edition_type="detailed",
        items=items,
        scorecards=(),
    )


def _snapshot_item(
    work_item_id: int,
    *,
    state: str = "Active",
    risk_level: RiskLevel = RiskLevel.LOW,
    target_date: date | None = None,
    assigned_to: str | None = None,
) -> SnapshotItem:
    return SnapshotItem(
        id=work_item_id,
        type="Feature",
        title=f"Work item {work_item_id}",
        state=state,
        assigned_to=assigned_to,
        area_path="One\\Adventure\\Acme\\Deployment",
        target_date=target_date,
        risk_level=risk_level,
        tags=[],
    )


def _comment(work_item_id: int, comment_id: int, created_date: datetime, text: str) -> Comment:
    return Comment(
        work_item_id=work_item_id,
        comment_id=comment_id,
        created_by="Vertex Maintainer",
        created_by_email="maintainer@example.com",
        created_date=created_date,
        text=text,
    )


def _revision(work_item_id: int, rev_number: int, changed_date: datetime, fields_changed: dict[str, tuple[str | None, str | None]]) -> Revision:
    return Revision(
        work_item_id=work_item_id,
        rev_number=rev_number,
        changed_by="Vertex Maintainer",
        changed_by_email="maintainer@example.com",
        changed_date=changed_date,
        fields_changed=fields_changed,
    )


def _program_context(alternate_owner: str | None) -> NarrativeProgramContext:
    return NarrativeProgramContext(
        schema_version="1.0",
        program_name="Adventure + DD on PF",
        objective="Objective",
        mission=None,
        pillars=(),
        glossary={},
        workstreams=(
            ProgramWorkstream(
                name="Acme",
                aliases=("acme",),
                area_paths=("One\\Adventure\\Acme",),
                dri_email="priya@example.com",
                alternate_owner=alternate_owner,
                description=None,
            ),
        ),
        people=(
            ProgramPerson(
                email="priya@example.com",
                display_name="Priya Mehta",
                role=None,
                workstreams=("Acme",),
            ),
            ProgramPerson(
                email="backup@example.com",
                display_name="Vertex Maintainer",
                role=None,
                workstreams=("Acme",),
            ),
        ),
    )