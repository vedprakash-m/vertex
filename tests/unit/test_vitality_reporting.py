from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.models import Comment, Revision, RiskLevel, WorkItem
from src.core.models_v2 import PersonDirectory, VitalityAggregate, VitalityArchiveEntry, VitalityArchiveWorkstream, VitalityScore, Workstream
from src.core.vitality_reporting import build_vitality_archive_entry, build_vitality_section, build_vitality_snapshot, effective_vitality_exempt_aliases, vitality_settings_from_program


def test_build_vitality_section_formats_trend_and_best_documented_copy() -> None:
    scores = (
        VitalityScore(
            work_item_id=900001,
            owner_alias="operator",
            workstream_id="deployment_readiness",
            freshness_days=2,
            freshness_grade="green",
            richness_score=90,
            richness_missing=(),
            leakage_events=0,
            workiq_signal_count=3,
            composite_score=97,
            suggested_update=None,
        ),
        VitalityScore(
            work_item_id=900002,
            owner_alias="alex",
            workstream_id="deployment_readiness",
            freshness_days=5,
            freshness_grade="green",
            richness_score=60,
            richness_missing=("next_step",),
            leakage_events=1,
            workiq_signal_count=3,
            composite_score=78,
            suggested_update="Add a dated next step.",
        ),
    )
    snapshot = build_vitality_snapshot(
        scores,
        (
            VitalityAggregate(
                scope_id="deployment_readiness",
                scope_type="workstream",
                total_items=2,
                fresh_items=2,
                avg_richness=75.0,
                total_leakage=1,
                workiq_signal_count=6,
                leakage_ratio=0.17,
                composite_score=87,
                trend=None,
            ),
        ),
    )
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    items = (
        WorkItem(
            id=900001,
            type="Feature",
            title="Deployment readiness evidence capture",
            state="Active",
            assigned_to="Vertex Maintainer",
            assigned_to_email="operator@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 20),
            risk_level=RiskLevel.MEDIUM,
            tags=("acme",),
            custom_fields={},
            revisions=(
                Revision(900001, 1, "Vertex Maintainer", "operator@example.com", as_of, {"State": ("New", "Active")}),
                Revision(900001, 2, "Vertex Maintainer", "operator@example.com", as_of.replace(day=8), {"Tags": (None, "acme")}),
            ),
            comments=(
                Comment(900001, 1, "Vertex Maintainer", "operator@example.com", as_of.replace(day=9), "Added deployment proof."),
            ),
            fetched_at=as_of,
        ),
        WorkItem(
            id=900002,
            type="Risk",
            title="Capacity gate remains open",
            state="At Risk",
            assigned_to="Alex Doe",
            assigned_to_email="alex@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 21),
            risk_level=RiskLevel.HIGH,
            tags=(),
            custom_fields={},
            revisions=(
                Revision(900002, 1, "Alex Doe", "alex@example.com", as_of.replace(day=6), {"State": ("Active", "At Risk")}),
            ),
            comments=(),
            fetched_at=as_of,
        ),
    )
    history_entries = (
        VitalityArchiveEntry(74, as_of.replace(day=1), 58, 52, 30, 61, 7, {"deployment_readiness": VitalityArchiveWorkstream(60, 10, 6)}),
        VitalityArchiveEntry(75, as_of.replace(day=2), 63, 52, 33, 64, 6, {"deployment_readiness": VitalityArchiveWorkstream(65, 10, 7)}),
        VitalityArchiveEntry(76, as_of.replace(day=3), 71, 52, 37, 69, 5, {"deployment_readiness": VitalityArchiveWorkstream(72, 10, 8)}),
    )

    section = build_vitality_section(
        snapshot,
        current_issue_number=77,
        history_entries=history_entries,
        items=items,
        workstreams=(
            Workstream(
                id="deployment_readiness",
                name="Deployment Readiness",
                pm_owner="operator",
            ),
        ),
        include_individual_praise=False,
    )

    assert snapshot.aggregate_score == 87
    assert section.items_updated == 2
    assert section.items_total == 2
    assert section.best_documented_label == "WI:900001"
    assert section.best_documented_detail == "3 ADO touches in the last 7 days"
    assert section.trend_summary == "58% -> 63% -> 71% -> 87% (improving over 4 issues)"
    assert len(section.accountability_rows) == 1
    assert section.accountability_rows[0].workstream == "Deployment Readiness"
    assert section.accountability_rows[0].owners_and_assignee == "WS owner: operator | ADO assignee: Alex Doe"
    assert section.accountability_rows[0].ado_label == "ADO#900002"
    assert section.accountability_rows[0].fields_to_update == ("Next step",)


def test_build_vitality_section_skips_unmapped_accountability_rows() -> None:
    scores = (
        VitalityScore(
            work_item_id=900010,
            owner_alias="alex",
            workstream_id=None,
            freshness_days=22,
            freshness_grade="red",
            richness_score=40,
            richness_missing=("target_date", "next_step"),
            leakage_events=0,
            workiq_signal_count=0,
            composite_score=31,
            suggested_update="Refresh the ADO.",
        ),
    )
    snapshot = build_vitality_snapshot(scores, ())
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    items = (
        WorkItem(
            id=900010,
            type="Feature",
            title="Unmapped vitality item",
            state="Active",
            assigned_to="Alex Doe",
            assigned_to_email="alex@example.com",
            area_path="One\\Unknown\\Area",
            iteration_path="FY26\\Sprint 20",
            target_date=None,
            risk_level=RiskLevel.MEDIUM,
            tags=(),
            custom_fields={},
            revisions=(),
            comments=(),
            fetched_at=as_of,
        ),
    )

    section = build_vitality_section(
        snapshot,
        current_issue_number=77,
        history_entries=(),
        items=items,
        workstreams=(),
        include_individual_praise=False,
    )

    assert section.accountability_rows == ()


def test_build_vitality_archive_entry_tracks_aggregate_and_per_workstream() -> None:
    scores = (
        VitalityScore(900001, "operator", "deployment_readiness", 2, "green", 90, (), 0, 5, 97, None),
    )
    snapshot = build_vitality_snapshot(
        scores,
        (
            VitalityAggregate(
                scope_id="deployment_readiness",
                scope_type="workstream",
                total_items=1,
                fresh_items=1,
                avg_richness=90.0,
                total_leakage=0,
                workiq_signal_count=5,
                leakage_ratio=0.0,
                composite_score=97,
                trend=None,
            ),
        ),
    )

    record = build_vitality_archive_entry(
        snapshot,
        issue_number=77,
        confirmed_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
    )

    assert record.aggregate_score == 97
    assert record.items_total == 1
    assert record.per_workstream["deployment_readiness"].score == 97
    assert record.per_owner == {}


def test_build_vitality_archive_entry_includes_per_owner_when_enabled() -> None:
    scores = (
        VitalityScore(900001, "operator", "deployment_readiness", 2, "green", 90, (), 0, 5, 97, None),
    )
    snapshot = build_vitality_snapshot(
        scores,
        (
            VitalityAggregate(
                scope_id="deployment_readiness",
                scope_type="workstream",
                total_items=1,
                fresh_items=1,
                avg_richness=90.0,
                total_leakage=0,
                workiq_signal_count=5,
                leakage_ratio=0.0,
                composite_score=97,
                trend=None,
            ),
        ),
    )

    record = build_vitality_archive_entry(
        snapshot,
        issue_number=77,
        confirmed_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        include_per_owner=True,
    )

    assert record.per_owner["operator"].score == 97
    assert record.per_owner["operator"].items == 1


def test_vitality_settings_from_program_reads_surface_flags_and_exemptions() -> None:
    settings = vitality_settings_from_program(
        {
            "vitality": {
                "surfaces": {
                    "triage": True,
                    "newsletter_aggregate": True,
                    "newsletter_individual_praise": False,
                    "reviewer_pane": True,
                    "ado_nudge_comments": True,
                    "ado_tags": True,
                },
                "nudge_composite_threshold": 35,
                "nudge_stale_days": 10,
                "nudge_cooldown_days": 21,
                "tag_consecutive_gaps": 3,
                "sparse_workiq_threshold": 7,
                "vitality_tag_name": "Needs-Triage",
                "vitality_archive_per_person": True,
                "exempt_aliases": ["director", " gpm "],
            }
        }
    )

    assert settings.triage is True
    assert settings.newsletter_aggregate is True
    assert settings.newsletter_individual_praise is False
    assert settings.reviewer_pane is True
    assert settings.ado_nudge_comments is True
    assert settings.ado_tags is True
    assert settings.nudge_composite_threshold == 35
    assert settings.nudge_stale_days == 10
    assert settings.nudge_cooldown_days == 21
    assert settings.tag_consecutive_gaps == 3
    assert settings.sparse_workiq_threshold == 7
    assert settings.vitality_tag_name == "Needs-Triage"
    assert settings.vitality_archive_per_person is True
    assert settings.exempt_aliases == ("director", "gpm")


def test_effective_vitality_exempt_aliases_include_people_directory_flags() -> None:
    settings = vitality_settings_from_program(
        {
            "vitality": {
                "exempt_aliases": ["director"],
            }
        }
    )

    aliases = effective_vitality_exempt_aliases(
        settings,
        (
            PersonDirectory(alias="operator", exempt_from_vitality=True),
            PersonDirectory(alias="owner", exempt_from_vitality=False),
            PersonDirectory(alias=" director ", exempt_from_vitality=True),
        ),
    )

    assert aliases == ("director", "operator")