from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.core.decision_register import assess_decision_review_staleness, assess_proposed_decision_staleness, load_decisions, read_governance_decisions_from_overrides, save_decisions, upsert_decisions
from src.core.models_v2 import DecisionEntry, DecisionStatus
from src.core.overrides_store import DecisionRecord, OverridesDocument
from src.core.program_fact_store import load_program_facts, project_decision_entries


def test_save_and_load_decisions_round_trip(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    entry = DecisionEntry(
        id="decision-1",
        program_id="demo",
        title="Choose rollout path",
        context="Two rollout options remain.",
        decision="Proceed with the guarded rollout.",
        rationale="It minimizes blast radius.",
        alternatives_considered=("pause", "full rollout"),
        decided_by="demo",
        decision_date=date(2026, 5, 10),
        status=DecisionStatus.DECIDED,
        superseded_by=None,
        linked_claim_id="ask-1",
        linked_risk_id="risk-1",
        linked_action_ids=("action-1",),
        workstream_id="ws_demo",
        entity_refs=("WI:1001",),
        review_by=date(2026, 6, 1),
    )

    save_decisions("demo", (entry,), programs_root=programs_root)

    assert load_decisions("demo", programs_root=programs_root) == (entry,)
    snapshot = load_program_facts("demo", as_of=datetime.now(timezone.utc), db_root=programs_root.parent)
    assert len(project_decision_entries(snapshot)) == 1
    assert project_decision_entries(snapshot)[0].id == entry.id


def test_load_decisions_accepts_empty_store(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    save_decisions("demo", (), programs_root=programs_root)

    assert load_decisions("demo", programs_root=programs_root) == ()


def test_save_decisions_closes_removed_fact_entries(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    existing = DecisionEntry(
        id="decision-1",
        program_id="demo",
        title="Choose rollout path",
        context="Two rollout options remain.",
        decision="Proceed with the guarded rollout.",
        rationale=None,
        alternatives_considered=(),
        decided_by="demo",
        decision_date=date(2026, 5, 1),
        status=DecisionStatus.PROPOSED,
        superseded_by=None,
        linked_claim_id=None,
        linked_risk_id=None,
        linked_action_ids=(),
        workstream_id="ws_demo",
        entity_refs=("WI:1001",),
    )

    save_decisions("demo", (existing,), programs_root=programs_root)
    save_decisions("demo", (), programs_root=programs_root)

    snapshot = load_program_facts("demo", as_of=datetime.now(timezone.utc), db_root=programs_root.parent)

    assert project_decision_entries(snapshot) == ()


def test_assess_proposed_decision_staleness_flags_old_proposals() -> None:
    stale_entry = DecisionEntry(
        id="decision-1",
        program_id="demo",
        title="Choose rollout path",
        context="Two rollout options remain.",
        decision="Proceed with the guarded rollout.",
        rationale=None,
        alternatives_considered=(),
        decided_by="demo",
        decision_date=date(2026, 5, 1),
        status=DecisionStatus.PROPOSED,
        superseded_by=None,
        linked_claim_id=None,
        linked_risk_id=None,
        linked_action_ids=(),
        workstream_id=None,
        entity_refs=(),
    )

    assert assess_proposed_decision_staleness(stale_entry, date(2026, 5, 20)) is True
    assert assess_proposed_decision_staleness(stale_entry, date(2026, 5, 10)) is False


def test_assess_decision_review_staleness_flags_overdue_decided_entries() -> None:
    entry = DecisionEntry(
        id="decision-1",
        program_id="demo",
        title="Choose rollout path",
        context="Two rollout options remain.",
        decision="Proceed with the guarded rollout.",
        rationale=None,
        alternatives_considered=(),
        decided_by="demo",
        decision_date=date(2026, 5, 1),
        status=DecisionStatus.DECIDED,
        superseded_by=None,
        linked_claim_id=None,
        linked_risk_id=None,
        linked_action_ids=(),
        workstream_id=None,
        entity_refs=(),
        review_by=date(2026, 5, 15),
    )

    assert assess_decision_review_staleness(entry, date(2026, 5, 20)) is True
    assert assess_decision_review_staleness(entry, date(2026, 5, 10)) is False


def test_upsert_decisions_keeps_existing_entry_when_duplicate_id_reappears(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    existing = DecisionEntry(
        id="decision-1",
        program_id="demo",
        title="Choose rollout path",
        context="Two rollout options remain.",
        decision="Proceed with the guarded rollout.",
        rationale=None,
        alternatives_considered=(),
        decided_by="demo",
        decision_date=date(2026, 5, 1),
        status=DecisionStatus.PROPOSED,
        superseded_by=None,
        linked_claim_id=None,
        linked_risk_id=None,
        linked_action_ids=(),
        workstream_id="ws_demo",
        entity_refs=("WI:1001",),
    )
    duplicate = DecisionEntry(
        id="decision-1",
        program_id="demo",
        title="Changed title",
        context="Changed context.",
        decision="Changed decision.",
        rationale=None,
        alternatives_considered=(),
        decided_by="auto-extracted",
        decision_date=date(2026, 5, 2),
        status=DecisionStatus.PROPOSED,
        superseded_by=None,
        linked_claim_id=None,
        linked_risk_id=None,
        linked_action_ids=(),
        workstream_id="ws_other",
        entity_refs=("WI:2002",),
    )

    save_decisions("demo", (existing,), programs_root=programs_root)
    upsert_decisions("demo", (duplicate,), programs_root=programs_root)

    assert load_decisions("demo", programs_root=programs_root) == (existing,)


def test_read_governance_decisions_from_overrides_preserves_work_item_refs_from_source_ref() -> None:
    overrides = OverridesDocument(
        issue_number=78,
        top_3_now=(),
        scorecards=(),
        decisions=(
            DecisionRecord(
                id="dec-ado-url",
                workstream="velocity",
                type="gate",
                statement="Proceed with guarded rollout.",
                source_type="ado",
                source_ref="https://dev.azure.com/contoso/one/_workitems/edit/12345",
                owner="owner",
                status="active",
                effective_date=date(2026, 5, 1),
            ),
            DecisionRecord(
                id="dec-explicit",
                workstream="velocity",
                type="gate",
                statement="Hold until mitigation lands.",
                source_type="manual",
                source_ref="Discussed under WI:23456 and bug 34567",
                owner="owner",
                status="proposed",
                effective_date=date(2026, 5, 2),
            ),
        ),
    )

    entries = read_governance_decisions_from_overrides(overrides, program_id="demo")

    assert [entry.id for entry in entries] == ["dec-ado-url", "dec-explicit"]
    assert entries[0].entity_refs == ("WI:12345",)
    assert entries[1].entity_refs == ("WI:23456", "WI:34567")


def test_load_decisions_rejects_non_string_status(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "demo" / "decisions.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema_version: '1.0'\ndecisions:\n  - id: decision-1\n    program_id: demo\n    title: Choose rollout path\n    context: Two rollout options remain.\n    decision: Proceed with the guarded rollout.\n    decided_by: demo\n    decision_date: '2026-05-10'\n    status: 1\n",
        encoding="utf-8",
    )

    try:
        load_decisions("demo", programs_root=programs_root)
        raise AssertionError("Expected ConfigError")
    except Exception as error:
        assert "status must be a string" in str(error)


def test_load_decisions_rejects_non_string_entity_ref(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "demo" / "decisions.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema_version: '1.0'\ndecisions:\n  - id: decision-1\n    program_id: demo\n    title: Choose rollout path\n    context: Two rollout options remain.\n    decision: Proceed with the guarded rollout.\n    decided_by: demo\n    decision_date: '2026-05-10'\n    status: decided\n    entity_refs:\n      - 123\n",
        encoding="utf-8",
    )

    try:
        load_decisions("demo", programs_root=programs_root)
        raise AssertionError("Expected ConfigError")
    except Exception as error:
        assert "entity_refs must contain strings only" in str(error)


def test_load_decisions_rejects_non_string_workstream_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "demo" / "decisions.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema_version: '1.0'\ndecisions:\n  - id: decision-1\n    program_id: demo\n    title: Choose rollout path\n    context: Two rollout options remain.\n    decision: Proceed with the guarded rollout.\n    decided_by: demo\n    decision_date: '2026-05-10'\n    status: decided\n    workstream_id: 123\n",
        encoding="utf-8",
    )

    try:
        load_decisions("demo", programs_root=programs_root)
        raise AssertionError("Expected ConfigError")
    except Exception as error:
        assert "workstream_id must be a string" in str(error)