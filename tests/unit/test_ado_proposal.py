from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

from src.core.ado_proposal import ADOFieldProposalValue, build_comment_proposal, build_field_proposal, build_vitality_nudge_proposal, build_vitality_tag_proposal, load_ado_field_mapping_config, load_confirmed_issue_snapshot, write_proposal_manifest
from src.core.archive_store import write_confirmed_issue
from src.core.coverage_gap import CoverageGap
from src.core.models import ConfirmedDimension, EditionType, RiskLevel, RunManifest, Snapshot, SnapshotItem
from src.core.models import WorkItem
from src.core.models_v2 import VitalityScore


def test_build_comment_proposal_loads_confirmed_issue_and_records_revision_ids(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    programs_root = tmp_path / "programs"
    programs_root.mkdir(parents=True)
    (programs_root / "demo" / "editions").mkdir(parents=True)
    (programs_root / "demo" / "editions" / "demo_weekly.yaml").write_text(
        "id: demo_weekly\nprogram_id: demo\n", encoding="utf-8"
    )
    generated_at = datetime(2026, 5, 12, 18, 0, tzinfo=timezone.utc)

    snapshot = Snapshot(
        issue_number=7,
        generated_at=generated_at,
        ado_data_as_of=generated_at,
        edition_type=EditionType.DETAILED,
        items=tuple(
            SnapshotItem(
                id=1000 + index,
                type="Feature",
                title=f"Tracked item {index}",
                state="Active",
                assigned_to="owner@example.com",
                area_path="One\\Demo\\WS",
                target_date=date(2026, 6, 1 + index),
                risk_level=RiskLevel.HIGH if index % 2 == 0 else RiskLevel.MEDIUM,
                tags=["demo"],
            )
            for index in range(1, 6)
        ),
        scorecards=(
            ConfirmedDimension(
                scorecard_name="Demo Scorecard",
                name="Demo Dimension",
                risk=RiskLevel.HIGH,
                prior_risk=None,
                item_count=5,
                ado_query_url="https://dev.azure.com/query",
            ),
        ),
    )
    manifest = RunManifest(
        manifest_id="manifest-7",
        issue_number=7,
        edition="demo_weekly",
        started_at=generated_at,
        ended_at=generated_at,
        config_hash="config",
        snapshot_hash="snapshot",
        html_hash="html",
        md_hash="md",
        ado_calls=0,
        ai_calls=0,
        ai_cost_usd=0.0,
        freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
        qg_results={},
        git_sha="abc1234",
    )
    write_confirmed_issue(
        edition="demo_weekly",
        issue_number=7,
        snapshot=snapshot,
        html_body="<html><body>Issue 007</body></html>",
        markdown_body="# Issue 007",
        manifest=manifest,
        archive_root=archive_root,
    )

    loaded_snapshot = load_confirmed_issue_snapshot("demo_weekly", 7, archive_root=archive_root)
    proposal = build_comment_proposal(
        program_id="demo",
        edition_id="demo_weekly",
        snapshot=loaded_snapshot,
        current_work_item_rows=[
            {"id": 1001, "fields": {"System.Id": 1001, "System.Rev": 11}},
            {"id": 1002, "fields": {"System.Id": 1002, "System.Rev": 12}},
            {"id": 1003, "fields": {"System.Id": 1003, "System.Rev": 13}},
            {"id": 1004, "fields": {"System.Id": 1004, "System.Rev": 14}},
            {"id": 1005, "fields": {"System.Id": 1005, "System.Rev": 15}},
        ],
        proposal_id="prop-demo",
        created_at=generated_at,
    )
    manifest_path = write_proposal_manifest(proposal, programs_root=programs_root)
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert proposal.id == "prop-demo"
    assert proposal.program_id == "demo"
    assert proposal.update_type == "comment"
    assert proposal.created_at == generated_at
    assert proposal.expires_at == generated_at + timedelta(hours=72)
    assert len(proposal.entries) == 5
    assert all(entry.action == "add_comment" for entry in proposal.entries)
    assert all(entry.field_or_tag == "comment" for entry in proposal.entries)
    assert [entry.revision_id for entry in proposal.entries] == [11, 12, 13, 14, 15]
    assert proposal.entries[0].reason == "Cited in confirmed issue #007."
    assert "Vertex demo_weekly issue #007" in proposal.entries[0].proposed_value
    assert manifest_path == (tmp_path / "programs" / "demo" / "publications") / "demo_weekly" / "ado_proposals" / "prop-demo.json"
    assert manifest_payload["proposal_status"] == "pending"
    assert len(manifest_payload["entries"]) == 5
    assert manifest_payload["entries"][4]["revision_id"] == 15


def test_build_comment_proposal_uses_program_comment_template_when_present(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    template_path = programs_root / "demo" / "ado_comment_template.md"
    template_path.parent.mkdir(parents=True)
    template_path.write_text(
        "Custom demo issue #{issue_number_padded} ({date})\n\nWI {work_item_id}: {one_line_summary}\nStatus: {risk_level}\nTarget: {target_date_or_unknown}",
        encoding="utf-8",
    )
    generated_at = datetime(2026, 5, 12, 18, 0, tzinfo=timezone.utc)
    snapshot = Snapshot(
        issue_number=7,
        generated_at=generated_at,
        ado_data_as_of=generated_at,
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=1001,
                type="Feature",
                title="Tracked item 1",
                state="Active",
                assigned_to="owner@example.com",
                area_path="One\\Demo\\WS",
                target_date=date(2026, 6, 2),
                risk_level=RiskLevel.HIGH,
                tags=["demo"],
            ),
        ),
        scorecards=(),
    )

    proposal = build_comment_proposal(
        program_id="demo",
        edition_id="demo_weekly",
        snapshot=snapshot,
        current_work_item_rows=[{"id": 1001, "fields": {"System.Id": 1001, "System.Rev": 11}}],
        proposal_id="prop-demo-template",
        created_at=generated_at,
        programs_root=programs_root,
    )

    assert proposal.entries[0].proposed_value == (
        "Custom demo issue #007 (2026-05-12)\n\n"
        "WI 1001: Tracked item 1\n"
        "Status: high\n"
        "Target: 2026-06-02"
    )


def test_build_vitality_nudge_proposal_filters_recent_nudges_and_uses_neutral_copy() -> None:
    created_at = datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc)
    proposal = build_vitality_nudge_proposal(
        program_id="demo",
        edition_id="demo_weekly",
        issue_number=7,
        items=(
            _work_item(1001, created_at),
            _work_item(1002, created_at),
        ),
        scores=(
            VitalityScore(1001, "owner", "ws_demo", 18, "red", 35, ("recent_comment",), 1, 2, 31, "Add an owner comment"),
            VitalityScore(1002, "owner", "ws_demo", 18, "red", 35, ("recent_comment",), 1, 2, 31, "Add an owner comment"),
        ),
        current_work_item_rows=[
            {"id": 1001, "fields": {"System.Id": 1001, "System.Rev": 11}},
            {"id": 1002, "fields": {"System.Id": 1002, "System.Rev": 12}},
        ],
        proposal_id="prop-vitality-nudge",
        created_at=created_at,
        recent_nudge_item_ids={1002},
    )

    assert proposal.update_type == "vitality_nudge"
    assert [entry.work_item_id for entry in proposal.entries] == [1001]
    assert proposal.entries[0].revision_id == 11
    assert "Recent non-ADO activity was detected for this item." in proposal.entries[0].proposed_value
    assert "Owner comment / next step" in proposal.entries[0].proposed_value
    assert "email" not in proposal.entries[0].proposed_value.lower()


def test_build_vitality_tag_proposal_adds_and_removes_tags() -> None:
    created_at = datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc)
    proposal = build_vitality_tag_proposal(
        program_id="demo",
        edition_id="demo_weekly",
        issue_number=7,
        items=(
            _work_item(1001, created_at),
            _work_item(1002, created_at, tags=("Needs-PM-Review",)),
        ),
        scores=(
            VitalityScore(1001, "owner", "ws_demo", 32, "red", 35, (), 0, 0, 28, "Refresh ADO status"),
            VitalityScore(1002, "owner", "ws_demo", 3, "green", 80, (), 0, 0, 92, None),
        ),
        current_work_item_rows=[
            {"id": 1001, "fields": {"System.Id": 1001, "System.Rev": 11}},
            {"id": 1002, "fields": {"System.Id": 1002, "System.Rev": 12}},
        ],
        coverage_gaps=(CoverageGap(work_item_id=1001, title="Tracked item", state="Active", assigned_to="owner@example.com"),),
        proposal_id="prop-vitality-tag",
        created_at=created_at,
    )

    assert proposal.update_type == "vitality_tag"
    assert [(entry.work_item_id, entry.action) for entry in proposal.entries] == [(1001, "add_tag"), (1002, "remove_tag")]
    assert all(entry.proposed_value == "Needs-PM-Review" for entry in proposal.entries)


def test_build_field_proposal_loads_mapping_config_and_skips_noops(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    mapping_path = programs_root / "demo" / "ado_field_map.yaml"
    mapping_path.parent.mkdir(parents=True)
    mapping_path.write_text(
        "\n".join(
            [
                'schema_version: "1.0"',
                "proposal_ttl_hours: 48",
                "mappings:",
                "  - vertex_field: risk_level",
                "    ado_field: Custom.RiskLevel",
                "    direction: vertex_to_ado",
                "    auto_propose: false",
                "  - vertex_field: workstream_id",
                "    ado_field: Custom.Workstream",
                "    direction: vertex_to_ado",
                "    auto_propose: false",
            ]
        ),
        encoding="utf-8",
    )

    mapping_config = load_ado_field_mapping_config("demo", programs_root=programs_root)
    created_at = datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc)
    proposal = build_field_proposal(
        program_id="demo",
        edition_id="demo_weekly",
        issue_number=8,
        mapping_config=mapping_config,
        current_work_item_rows=[
            {
                "id": 1001,
                "fields": {
                    "System.Id": 1001,
                    "System.Rev": 11,
                    "Custom.RiskLevel": "medium",
                    "Custom.Workstream": "legacy_ws",
                },
            },
            {
                "id": 1002,
                "fields": {
                    "System.Id": 1002,
                    "System.Rev": 12,
                    "Custom.RiskLevel": "low",
                    "Custom.Workstream": "ws_demo",
                },
            },
        ],
        field_values_by_item={
            1001: {
                "risk_level": ADOFieldProposalValue("high", "Sync risk_level from latest Vertex override."),
                "workstream_id": ADOFieldProposalValue("ws_demo", "Sync workstream_id from Vertex area-path mapping."),
            },
            1002: {
                "risk_level": ADOFieldProposalValue("low", "Sync risk_level from latest Vertex override."),
                "workstream_id": ADOFieldProposalValue("ws_demo", "Sync workstream_id from Vertex area-path mapping."),
            },
        },
        proposal_id="prop-field",
        created_at=created_at,
    )

    assert proposal.update_type == "field"
    assert proposal.issue_number == 8
    assert proposal.expires_at == created_at + timedelta(hours=48)
    assert [(entry.work_item_id, entry.field_or_tag, entry.proposed_value) for entry in proposal.entries] == [
        (1001, "Custom.RiskLevel", "high"),
        (1001, "Custom.Workstream", "ws_demo"),
    ]
    assert [entry.revision_id for entry in proposal.entries] == [11, 11]
    assert proposal.entries[0].current_value == "medium"
    assert proposal.entries[1].current_value == "legacy_ws"


def _work_item(work_item_id: int, as_of: datetime, *, tags: tuple[str, ...] = ()) -> WorkItem:
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title="Tracked item",
        state="Active",
        assigned_to="owner@example.com",
        assigned_to_email="owner@example.com",
        area_path="One\\Demo\\WS",
        iteration_path="Sprint 1",
        target_date=date(2026, 6, 1),
        risk_level=RiskLevel.MEDIUM,
        tags=list(tags),
        custom_fields={"changed_date": (as_of - timedelta(days=18)).isoformat()},
        revisions=[],
        comments=[],
        fetched_at=as_of,
    )
