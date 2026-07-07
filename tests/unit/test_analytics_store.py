from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3

import yaml

from src.core import analytics_store as analytics_store_module
from src.core.analytics_store import AutonomyAuditArchiveArtifacts, AutonomyAuditRecord, ConfirmedDecisionProjection, append_autonomy_audit_record, archive_autonomy_audit_records, get_program_analytics_store_path, get_program_autonomy_audit_archive_path, get_program_autonomy_audit_path, load_autonomy_audit_records, load_contradiction_state, rebuild_program_analytics, replace_contradiction_state
from src.core.claim_tracker import append_claim_entry
from src.core.context_snapshot_store import write_context_snapshot
from src.core.decision_register import save_decisions
from src.core.models import ConfirmedDimension, EditionType, RiskLevel, Snapshot, SnapshotItem
from src.core.models import Confidence
from src.core.models_v2 import ClaimEntry, Contradiction, ContradictionPacket, DataSourceType, DecisionEntry, DecisionStatus, ResolvedContradiction
from src.core.snapshot_store import write_confirmed


def test_append_autonomy_audit_record_writes_primary_and_projection(tmp_path: Path) -> None:
    programs_root = _seed_program_layout(tmp_path)
    applied_at = datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc)

    path = append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id="demo",
            action_id="action-1",
            level="l2",
            author_alias="operator",
            subject_alias="alex",
            evidence_refs=("WI:1001", "signal:1"),
            policy_rule="vitality_nudge",
            accepted=True,
            applied_at=applied_at,
        ),
        programs_root=programs_root,
    )

    assert path == get_program_autonomy_audit_path("demo", programs_root=programs_root)
    payloads = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert payloads == [
        {
            "accepted": True,
            "action_id": "action-1",
            "applied_at": applied_at.isoformat(),
            "author_alias": "operator",
            "evidence_refs": ["WI:1001", "signal:1"],
            "level": "l2",
            "policy_rule": "vitality_nudge",
            "schema_version": "1.0",
            "subject_alias": "alex",
        }
    ]

    connection = sqlite3.connect(get_program_analytics_store_path("demo", programs_root=programs_root))
    try:
        row = connection.execute(
            "SELECT action_id, level, author_alias, subject_alias, accepted FROM autonomy_audit"
        ).fetchone()
    finally:
        connection.close()

    assert row == ("action-1", "l2", "operator", "alex", 1)


def test_rebuild_program_analytics_rebuilds_confirmed_rows(tmp_path: Path) -> None:
    programs_root = _seed_program_layout(tmp_path)
    archive_root = programs_root / "demo" / "archive"
    snapshot = Snapshot(
        issue_number=1,
        generated_at=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 19, 17, 0, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=1001,
                type="Feature",
                title="Demo item",
                state="Active",
                assigned_to="owner@example.com",
                area_path="One\\Demo",
                target_date=date(2026, 6, 1),
                risk_level=RiskLevel.HIGH,
                tags=["demo"],
            ),
        ),
        scorecards=(
            ConfirmedDimension(
                scorecard_name="Demo",
                name="Readiness",
                risk=RiskLevel.HIGH,
                prior_risk=RiskLevel.MEDIUM,
                item_count=1,
                ado_query_url="https://ado/demo",
            ),
        ),
    )
    snapshot_path = write_confirmed(
        edition="demo_weekly",
        issue_number=1,
        snapshot=snapshot,
        archive_root=archive_root,
        promote=True,
    )
    edition_root = archive_root / "demo_weekly"
    (edition_root / "index.json").write_text(
        json.dumps(
            {
                "edition": "demo_weekly",
                "issues": [
                    {
                        "issue_number": 1,
                        "generated_at": snapshot.generated_at.isoformat(),
                        "kind": "confirmed",
                        "snapshot_path": str(snapshot_path),
                        "manifest_path": None,
                    }
                ],
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            program_id="demo",
            edition_id="demo_weekly",
            issue_number=1,
            workstream_id="ws_demo",
            text="Expected by 2026-06-01",
            entity_refs=("WI:1001",),
            claim_date=date(2026, 5, 19),
            owner_alias="operator",
            due_date=date(2026, 6, 1),
        ),
        programs_root=programs_root,
    )
    save_decisions(
        "demo",
        (
            DecisionEntry(
                id="decision-1",
                program_id="demo",
                title="Promote demo gate",
                context="Context",
                decision="Proceed",
                rationale=None,
                alternatives_considered=(),
                decided_by="operator",
                decision_date=date(2026, 5, 19),
                status=DecisionStatus.DECIDED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id=None,
                linked_action_ids=(),
                workstream_id="ws_demo",
                entity_refs=("WI:1001",),
            ),
        ),
        programs_root=programs_root,
    )

    artifacts = rebuild_program_analytics(program_id="demo", programs_root=programs_root)

    assert artifacts.confirmed_risks == 1
    assert artifacts.confirmed_claims == 1
    assert artifacts.confirmed_decisions == 1
    assert artifacts.program_fact_decisions == 0
    assert artifacts.context_snapshot_decisions == 0
    assert artifacts.raw_decision_fallbacks == 1
    assert artifacts.low_fidelity_decisions == 1
    connection = sqlite3.connect(artifacts.database_path)
    try:
        risk_count = connection.execute("SELECT COUNT(*) FROM confirmed_risks").fetchone()[0]
        claim_count = connection.execute("SELECT COUNT(*) FROM confirmed_claims").fetchone()[0]
        decision_count = connection.execute("SELECT COUNT(*) FROM confirmed_decisions").fetchone()[0]
    finally:
        connection.close()

    assert risk_count == 1
    assert claim_count == 1
    assert decision_count == 1


def test_decision_rows_as_of_prefers_program_facts_when_available(monkeypatch, tmp_path: Path) -> None:
    snapshot = object()
    decision = DecisionEntry(
        id="decision-1",
        program_id="demo",
        title="Promote demo gate",
        context="Context",
        decision="Proceed",
        rationale=None,
        alternatives_considered=(),
        decided_by="operator",
        decision_date=date(2026, 5, 19),
        status=DecisionStatus.DECIDED,
        superseded_by=None,
        linked_claim_id=None,
        linked_risk_id=None,
        linked_action_ids=(),
        workstream_id="ws_demo",
        entity_refs=("WI:1001",),
    )
    captured: list[tuple[str, datetime, Path]] = []

    monkeypatch.setattr(
        analytics_store_module,
        "load_program_facts",
        lambda program_id, *, as_of, programs_root: captured.append((program_id, as_of, programs_root)) or snapshot,
    )
    monkeypatch.setattr(
        analytics_store_module,
        "project_decision_entries",
        lambda loaded_snapshot: (decision,) if loaded_snapshot is snapshot else (),
    )

    rows = analytics_store_module._decision_rows_as_of(
        program_id="demo",
        edition_id="demo_weekly",
        issue_number=1,
        as_of=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
        programs_root=tmp_path / "programs",
    )

    assert rows == [
        ConfirmedDecisionProjection(
            decision_id="decision-1",
            issue_number=1,
            text="Promote demo gate",
            owner="operator",
            status="decided",
            resolved_at="2026-05-19",
            source_tier="program_facts",
        )
    ]
    assert captured == [("demo", datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc), tmp_path / "programs")]


def test_decision_rows_as_of_uses_context_snapshot_when_program_facts_are_empty(tmp_path: Path) -> None:
    programs_root = _seed_program_layout(tmp_path)
    write_context_snapshot(
        "demo",
        "demo_weekly",
        1,
        milestones=[],
        risks=[],
        workstreams=[],
        decisions=[
            DecisionEntry(
                id="decision-1",
                program_id="demo",
                title="Promote demo gate",
                context="Context",
                decision="Proceed",
                rationale=None,
                alternatives_considered=(),
                decided_by="operator",
                decision_date=date(2026, 5, 19),
                status=DecisionStatus.DECIDED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id=None,
                linked_action_ids=(),
                workstream_id="ws_demo",
                entity_refs=("WI:1001",),
            ),
        ],
        confirmed_at=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
        plane1_change_count_since_prior=0,
        archive_root=programs_root,
    )

    rows = analytics_store_module._decision_rows_as_of(
        program_id="demo",
        edition_id="demo_weekly",
        issue_number=1,
        as_of=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    assert rows == [
        ConfirmedDecisionProjection(
            decision_id="decision-1",
            issue_number=1,
            text="Promote demo gate",
            owner="operator",
            status="decided",
            resolved_at="2026-05-19",
            source_tier="context_snapshot_1_1",
        )
    ]


def test_rebuild_program_analytics_uses_context_snapshot_decisions_without_program_facts(tmp_path: Path) -> None:
    programs_root = _seed_program_layout(tmp_path)
    archive_root = programs_root / "demo" / "archive"
    snapshot = Snapshot(
        issue_number=1,
        generated_at=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 19, 17, 0, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(),
        scorecards=(),
    )
    snapshot_path = write_confirmed(
        edition="demo_weekly",
        issue_number=1,
        snapshot=snapshot,
        archive_root=archive_root,
        promote=True,
    )
    edition_root = archive_root / "demo_weekly"
    (edition_root / "index.json").write_text(
        json.dumps(
            {
                "edition": "demo_weekly",
                "issues": [
                    {
                        "issue_number": 1,
                        "generated_at": snapshot.generated_at.isoformat(),
                        "kind": "confirmed",
                        "snapshot_path": str(snapshot_path),
                        "manifest_path": None,
                    }
                ],
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    write_context_snapshot(
        "demo",
        "demo_weekly",
        1,
        milestones=[],
        risks=[],
        workstreams=[],
        decisions=[
            DecisionEntry(
                id="decision-1",
                program_id="demo",
                title="Promote demo gate",
                context="Context",
                decision="Proceed",
                rationale=None,
                alternatives_considered=(),
                decided_by="operator",
                decision_date=date(2026, 5, 19),
                status=DecisionStatus.DECIDED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id=None,
                linked_action_ids=(),
                workstream_id="ws_demo",
                entity_refs=("WI:1001",),
            ),
        ],
        confirmed_at=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
        plane1_change_count_since_prior=0,
        archive_root=programs_root,
    )

    artifacts = rebuild_program_analytics(program_id="demo", programs_root=programs_root)

    assert artifacts.confirmed_decisions == 1
    assert artifacts.program_fact_decisions == 0
    assert artifacts.context_snapshot_decisions == 1
    assert artifacts.raw_decision_fallbacks == 0
    assert artifacts.low_fidelity_decisions == 1
    connection = sqlite3.connect(artifacts.database_path)
    try:
        rows = connection.execute(
            "SELECT decision_id, issue_number, edition, text, owner, status, resolved_at, confirmed_at, source_tier FROM confirmed_decisions"
        ).fetchall()
    finally:
        connection.close()

    assert rows == [
        (
            "decision-1",
            1,
            "demo_weekly",
            "Promote demo gate",
            "operator",
            "decided",
            "2026-05-19",
            "2026-05-19T18:00:00+00:00",
            "context_snapshot_1_1",
        )
    ]


def test_rebuild_program_analytics_records_program_fact_decision_source_tier(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_program_layout(tmp_path)
    archive_root = programs_root / "demo" / "archive"
    snapshot = Snapshot(
        issue_number=1,
        generated_at=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 19, 17, 0, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(),
        scorecards=(),
    )
    snapshot_path = write_confirmed(
        edition="demo_weekly",
        issue_number=1,
        snapshot=snapshot,
        archive_root=archive_root,
        promote=True,
    )
    edition_root = archive_root / "demo_weekly"
    (edition_root / "index.json").write_text(
        json.dumps(
            {
                "edition": "demo_weekly",
                "issues": [
                    {
                        "issue_number": 1,
                        "generated_at": snapshot.generated_at.isoformat(),
                        "kind": "confirmed",
                        "snapshot_path": str(snapshot_path),
                        "manifest_path": None,
                    }
                ],
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    program_fact_snapshot = object()
    decision = DecisionEntry(
        id="decision-1",
        program_id="demo",
        title="Promote demo gate",
        context="Context",
        decision="Proceed",
        rationale=None,
        alternatives_considered=(),
        decided_by="operator",
        decision_date=date(2026, 5, 19),
        status=DecisionStatus.DECIDED,
        superseded_by=None,
        linked_claim_id=None,
        linked_risk_id=None,
        linked_action_ids=(),
        workstream_id="ws_demo",
        entity_refs=("WI:1001",),
    )
    monkeypatch.setattr(
        analytics_store_module,
        "load_program_facts",
        lambda program_id, *, as_of, programs_root: program_fact_snapshot,
    )
    monkeypatch.setattr(
        analytics_store_module,
        "project_decision_entries",
        lambda loaded_snapshot: (decision,) if loaded_snapshot is program_fact_snapshot else (),
    )
    artifacts = rebuild_program_analytics(program_id="demo", programs_root=programs_root)

    assert artifacts.confirmed_decisions == 1
    assert artifacts.program_fact_decisions == 1
    assert artifacts.context_snapshot_decisions == 0
    assert artifacts.raw_decision_fallbacks == 0
    assert artifacts.low_fidelity_decisions == 0
    connection = sqlite3.connect(artifacts.database_path)
    try:
        rows = connection.execute(
            "SELECT decision_id, issue_number, edition, text, owner, status, resolved_at, confirmed_at, source_tier FROM confirmed_decisions"
        ).fetchall()
    finally:
        connection.close()

    assert rows == [
        (
            "decision-1",
            1,
            "demo_weekly",
            "Promote demo gate",
            "operator",
            "decided",
            "2026-05-19",
            "2026-05-19T18:00:00+00:00",
            "program_facts",
        )
    ]


def test_rebuild_program_analytics_records_raw_decision_fallback_tier(tmp_path: Path) -> None:
    programs_root = _seed_program_layout(tmp_path)
    archive_root = programs_root / "demo" / "archive"
    snapshot = Snapshot(
        issue_number=1,
        generated_at=datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 19, 17, 0, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(),
        scorecards=(),
    )
    snapshot_path = write_confirmed(
        edition="demo_weekly",
        issue_number=1,
        snapshot=snapshot,
        archive_root=archive_root,
        promote=True,
    )
    edition_root = archive_root / "demo_weekly"
    (edition_root / "index.json").write_text(
        json.dumps(
            {
                "edition": "demo_weekly",
                "issues": [
                    {
                        "issue_number": 1,
                        "generated_at": snapshot.generated_at.isoformat(),
                        "kind": "confirmed",
                        "snapshot_path": str(snapshot_path),
                        "manifest_path": None,
                    }
                ],
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    save_decisions(
        "demo",
        (
            DecisionEntry(
                id="decision-1",
                program_id="demo",
                title="Promote demo gate",
                context="Context",
                decision="Proceed",
                rationale=None,
                alternatives_considered=(),
                decided_by="operator",
                decision_date=date(2026, 5, 19),
                status=DecisionStatus.DECIDED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id=None,
                linked_action_ids=(),
                workstream_id="ws_demo",
                entity_refs=("WI:1001",),
            ),
        ),
        programs_root=programs_root,
    )

    artifacts = rebuild_program_analytics(program_id="demo", programs_root=programs_root)

    assert artifacts.confirmed_decisions == 1
    assert artifacts.program_fact_decisions == 0
    assert artifacts.context_snapshot_decisions == 0
    assert artifacts.raw_decision_fallbacks == 1
    assert artifacts.low_fidelity_decisions == 1
    connection = sqlite3.connect(artifacts.database_path)
    try:
        rows = connection.execute(
            "SELECT decision_id, issue_number, edition, text, owner, status, resolved_at, confirmed_at, source_tier FROM confirmed_decisions"
        ).fetchall()
    finally:
        connection.close()

    assert rows == [
        (
            "decision-1",
            1,
            "demo_weekly",
            "Promote demo gate",
            "operator",
            "decided",
            "2026-05-19",
            "2026-05-19T18:00:00+00:00",
            "raw_decisions",
        )
    ]


def test_archive_autonomy_audit_records_moves_rows_into_year_archive_and_rebuilds_projection(tmp_path: Path) -> None:
    programs_root = _seed_program_layout(tmp_path)
    older_at = datetime(2025, 12, 31, 18, 0, tzinfo=timezone.utc)
    newer_at = datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc)

    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id="demo",
            action_id="action-older",
            level="l2",
            author_alias="operator",
            subject_alias="alex",
            evidence_refs=("WI:1001",),
            policy_rule="vitality_nudge",
            accepted=True,
            applied_at=older_at,
        ),
        programs_root=programs_root,
    )
    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id="demo",
            action_id="action-newer",
            level="l2",
            author_alias="operator",
            subject_alias="alex",
            evidence_refs=("WI:1002",),
            policy_rule="vitality_nudge",
            accepted=False,
            applied_at=newer_at,
        ),
        programs_root=programs_root,
    )

    artifacts = archive_autonomy_audit_records(
        "demo",
        before=date(2026, 1, 1),
        programs_root=programs_root,
    )

    assert artifacts == AutonomyAuditArchiveArtifacts(
        program_id="demo",
        before_date=date(2026, 1, 1),
        archived_count=1,
        remaining_count=1,
        archive_paths=(get_program_autonomy_audit_archive_path("demo", 2025, programs_root=programs_root),),
    )
    assert [record.action_id for record in load_autonomy_audit_records("demo", programs_root=programs_root)] == [
        "action-newer"
    ]

    archived_payloads = [
        json.loads(line)
        for line in get_program_autonomy_audit_archive_path("demo", 2025, programs_root=programs_root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [payload["action_id"] for payload in archived_payloads] == ["action-older"]

    active_payloads = [
        json.loads(line)
        for line in get_program_autonomy_audit_path("demo", programs_root=programs_root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [payload["action_id"] for payload in active_payloads] == ["action-newer"]

    connection = sqlite3.connect(get_program_analytics_store_path("demo", programs_root=programs_root))
    try:
        rows = connection.execute("SELECT action_id FROM autonomy_audit ORDER BY action_id").fetchall()
    finally:
        connection.close()

    assert rows == [("action-newer",)]


def test_replace_contradiction_state_writes_and_reads_packets(tmp_path: Path) -> None:
    programs_root = _seed_program_layout(tmp_path)

    replace_contradiction_state(
        "demo",
        (
            ContradictionPacket(
                work_item_id=1001,
                workstream_id="ws_demo",
                contradictions=(
                    Contradiction(
                        field="target_date",
                        source_a="ado/target_date",
                        source_b="workiq/signal",
                        summary="WorkIQ implies 2026-06-24 while ADO still says 2026-06-10",
                        confidence=Confidence.HIGH,
                        evidence_refs=("WI:1001", "signal-1"),
                    ),
                ),
                confidence=Confidence.HIGH,
                recommended_resolution=ResolvedContradiction(
                    winning_source=DataSourceType.WORKIQ,
                    confidence=Confidence.HIGH,
                    rationale="Prefer WorkIQ because calibration indicates consistent optimism.",
                    evidence_refs=("WI:1001", "signal-1"),
                ),
                generated_at=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
            ),
        ),
        programs_root=programs_root,
    )

    packets = load_contradiction_state("demo", programs_root=programs_root)

    assert len(packets) == 1
    assert packets[0].work_item_id == 1001
    assert packets[0].recommended_resolution is not None
    assert packets[0].recommended_resolution.winning_source == DataSourceType.WORKIQ


def _seed_program_layout(tmp_path: Path) -> Path:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": "demo",
                "name": "Demo Program",
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    return programs_root