from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from src.core.action_tracker import append_action, load_actions
from src.core.assumption_tracker import load_assumptions, save_assumptions
from src.core.archive_store import write_skipped_issue
from src.core.claim_tracker import append_claim_entry, append_claim_status_update, append_decision_ask, load_claim_status_updates, load_open_claims, load_open_decision_asks
from src.core.decision_register import load_decisions, save_decisions
from src.core.dependency_graph import load_dependencies, save_dependencies
from src.core.milestone_engine import load_milestones, save_milestones
from src.core.models_v2 import (
    ActionItem,
    ActionSourceType,
    ActionStatus,
    ADOCoverageRequirement,
    Assumption,
    AssumptionStatus,
    ClaimEntry,
    ClaimStatusUpdate,
    DecisionAsk,
    DecisionEntry,
    DecisionStatus,
    ResurfacingPolicy,
    Dependency,
    DependencyADOQuery,
    DependencyStatus,
    DependencyType,
    EmailThreadSource,
    Milestone,
    MilestoneStatus,
    RiskCategory,
    RiskEntry,
    RiskImpact,
    RiskProbability,
    RiskStatus,
    TeamsChat,
    TeamsMeetingSeries,
    Workstream,
    WorkstreamSignalSources,
)
from src.core.program_fact_store import (
    FactLifecycleState,
    FactPrecedence,
    ProgramFactInput,
    ProgramFactStore,
    build_fact_id,
    build_natural_key,
    load_program_facts,
    persist_program_fact_snapshot,
    project_action_items,
    project_assumptions,
    project_baseline_trust_events,
    project_claim_entries,
    project_claim_status_updates,
    project_decision_asks,
    project_decision_entries,
    project_dependencies,
    project_milestones,
    project_risk_entries,
    project_skip_issues,
    project_workstreams,
    resolve_fact_sor_mode,
)
from src.core.fact_sor_state import save_fact_sor_state
from src.core.risk_register_engine import load_risk_register, save_risk_register
from src.core.trusted_baseline_store import advance_trusted_baseline, load_trusted_baseline_for_program, record_untrusted_issue
from src.core.workstream_documents import save_workstreams_document


def _programs_root(tmp_path: Path) -> Path:
    root = tmp_path / "programs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _strip_action_meta(items: tuple) -> tuple:
    return tuple(replace(a, fact_id=None, last_validated_at=None) for a in items)


def _strip_claim_meta(items: tuple) -> tuple:
    return tuple(items)


def _strip_decision_ask_meta(items: tuple) -> tuple:
    return tuple(items)


def _strip_risk_meta(items: tuple) -> tuple:
    return tuple(replace(r, fact_id=None, last_validated_at=None) for r in items)


def _strip_decision_meta(items: tuple) -> tuple:
    return tuple(replace(d, fact_id=None, last_validated_at=None) for d in items)


def test_append_fact_creates_program_scoped_revision_and_snapshot(tmp_path) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)
    created_at = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    result = store.append_fact(
        ProgramFactInput(
            fact_type="risk",
            scope="workstream:deployment",
            entity_refs=("WS:deployment",),
            payload={"risk": "high", "summary": "Launch gate still blocked."},
            source_signal_ids=("signal-1",),
        ),
        recorded_at=created_at,
    )

    snapshot = store.snapshot(as_of=created_at)

    assert result.action == "created"
    assert len(snapshot.facts) == 1
    assert snapshot.facts[0].fact_id == result.revision.fact_id
    assert snapshot.facts[0].natural_key == build_natural_key(
        "risk",
        entity_refs=("WS:deployment",),
        scope="workstream:deployment",
    )
    assert snapshot.facts[0].write_authority == "human"


def test_first_fact_id_is_deterministic_and_program_scoped(tmp_path: Path) -> None:
    fact = ProgramFactInput(
        fact_type="risk",
        scope="workstream:deployment",
        entity_refs=("WS:deployment",),
        payload={"risk": "high"},
    )
    natural_key = build_natural_key(
        fact.fact_type,
        entity_refs=fact.entity_refs,
        scope=fact.scope,
    )
    first = ProgramFactStore("acme", db_root=tmp_path / "first").append_fact(fact)
    replay = ProgramFactStore("acme", db_root=tmp_path / "replay").append_fact(fact)
    other_program = ProgramFactStore("other", db_root=tmp_path / "other").append_fact(fact)

    assert first.revision.fact_id == build_fact_id("acme", natural_key)
    assert replay.revision.fact_id == first.revision.fact_id
    assert other_program.revision.fact_id != first.revision.fact_id


def test_append_fact_persists_write_authority_round_trip(tmp_path: Path) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)

    result = store.append_fact(
        ProgramFactInput(
            fact_type="action.item",
            entity_refs=("WI:123",),
            payload={"source": "ado", "title": "Backfilled action"},
            write_authority="bridge",
        ),
        recorded_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )

    snapshot = store.snapshot()

    assert result.revision.write_authority == "bridge"
    assert snapshot.facts[0].write_authority == "bridge"


def test_append_fact_persists_gather_run_id_round_trip(tmp_path: Path) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)

    result = store.append_fact(
        ProgramFactInput(
            fact_type="action.item",
            entity_refs=("WI:456",),
            payload={"source": "ado", "title": "Gather-stamped action"},
            gather_run_id="gather-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        ),
        recorded_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )

    snapshot = store.snapshot()

    assert result.revision.gather_run_id == "gather-01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert snapshot.facts[0].gather_run_id == "gather-01ARZ3NDEKTSV4RRFFQ69G5FAV"


def test_load_program_facts_can_exclude_non_committed_gather_runs(tmp_path: Path) -> None:
    from src.core.gather_run_manifest import (
        GatherRunManifest,
        GatherRunStatus,
        RequiredScopeStatus,
        commit_staging_run,
        create_staging_manifest,
    )

    programs_root = _programs_root(tmp_path)
    started = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    running = GatherRunManifest(
        "gather-running",
        GatherRunStatus.RUNNING,
        "acme",
        "interactive",
        "test",
        1,
        started,
        started,
        RequiredScopeStatus.FULL,
    )
    committed = GatherRunManifest(
        "gather-committed",
        GatherRunStatus.RUNNING,
        "acme",
        "interactive",
        "test",
        2,
        started,
        started,
        RequiredScopeStatus.FULL,
    )
    create_staging_manifest(running, programs_root=programs_root)
    create_staging_manifest(committed, programs_root=programs_root)
    commit_staging_run(committed, finished_at=started, programs_root=programs_root)

    store = ProgramFactStore("acme", db_root=tmp_path)
    for entity_ref, gather_run_id in (
        ("WI:1", running.run_id),
        ("WI:2", committed.run_id),
        ("WI:3", None),
    ):
        store.append_fact(
            ProgramFactInput(
                fact_type="action.item",
                entity_refs=(entity_ref,),
                payload={"source": "ado", "title": entity_ref},
                gather_run_id=gather_run_id,
            ),
            recorded_at=started,
        )

    snapshot = load_program_facts(
        "acme",
        programs_root=programs_root,
        require_committed_gather_run=True,
    )

    assert {fact.gather_run_id for fact in snapshot.facts} == {committed.run_id, None}


def test_load_program_facts_bounds_unstamped_facts_by_legacy_cutoff(tmp_path: Path) -> None:
    """§4.17 step 5: once a legacy-cutoff manifest exists, an unstamped fact
    is grandfathered only up to the cutoff; unstamped facts recorded after
    the cutoff are excluded under enforcement."""
    from src.core.gather_run_manifest import create_legacy_cutoff_manifest

    programs_root = _programs_root(tmp_path)
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    create_legacy_cutoff_manifest("acme", legacy_cutoff_at=cutoff, programs_root=programs_root)

    store = ProgramFactStore("acme", db_root=tmp_path)
    for entity_ref, recorded_at in (
        ("WI:1", datetime(2025, 6, 1, tzinfo=timezone.utc)),  # unstamped, pre-cutoff -> visible
        ("WI:2", datetime(2026, 6, 1, tzinfo=timezone.utc)),  # unstamped, post-cutoff -> excluded
    ):
        store.append_fact(
            ProgramFactInput(
                fact_type="action.item",
                entity_refs=(entity_ref,),
                payload={"source": "ado", "title": entity_ref},
                gather_run_id=None,
            ),
            recorded_at=recorded_at,
        )

    snapshot = load_program_facts(
        "acme",
        programs_root=programs_root,
        require_committed_gather_run=True,
    )

    assert {fact.entity_refs for fact in snapshot.facts} == {("WI:1",)}


def test_load_program_facts_keeps_unstamped_facts_when_no_legacy_cutoff_exists(tmp_path: Path) -> None:
    """Backward compatibility: programs that haven't bootstrapped a
    legacy-cutoff manifest keep today's behavior — unstamped facts remain
    visible unconditionally, regardless of when they were recorded."""
    programs_root = _programs_root(tmp_path)

    store = ProgramFactStore("acme", db_root=tmp_path)
    for entity_ref, recorded_at in (
        ("WI:1", datetime(2025, 6, 1, tzinfo=timezone.utc)),
        ("WI:2", datetime(2026, 6, 1, tzinfo=timezone.utc)),
    ):
        store.append_fact(
            ProgramFactInput(
                fact_type="action.item",
                entity_refs=(entity_ref,),
                payload={"source": "ado", "title": entity_ref},
                gather_run_id=None,
            ),
            recorded_at=recorded_at,
        )

    snapshot = load_program_facts(
        "acme",
        programs_root=programs_root,
        require_committed_gather_run=True,
    )

    assert {fact.entity_refs for fact in snapshot.facts} == {("WI:1",), ("WI:2",)}


def test_load_program_facts_primary_mode_disables_shim_merge(tmp_path: Path, monkeypatch) -> None:
    programs_root = _programs_root(tmp_path)
    actions_path = programs_root / "acme" / "journal" / "actions.jsonl"
    actions_path.parent.mkdir(parents=True, exist_ok=True)
    actions_path.write_text(
        "{\"record_type\":\"action\",\"id\":\"action-1\",\"program_id\":\"acme\",\"text\":\"Follow up\",\"owner_alias\":\"alex\",\"due_date\":\"2026-05-20\",\"status\":\"open\",\"source_signal_id\":\"signal-1\",\"source_type\":\"manual\",\"linked_work_item_ids\":[],\"linked_claim_id\":null,\"linked_risk_id\":null,\"workstream_id\":null,\"created_at\":\"2026-05-01T09:00:00+00:00\",\"resolved_at\":null,\"resolution_note\":null}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VERTEX_FACT_SOR", "primary")

    snapshot = load_program_facts("acme", programs_root=programs_root)

    assert snapshot.facts == ()


def test_load_program_facts_shadow_mode_keeps_shim_merge(tmp_path: Path, monkeypatch) -> None:
    programs_root = _programs_root(tmp_path)
    append_action(
        "acme",
        ActionItem(
            id="action-1",
            program_id="acme",
            text="Follow up",
            owner_alias="alex",
            due_date=date(2026, 5, 20),
            status=ActionStatus.OPEN,
            source_signal_id="signal-1",
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id=None,
            created_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )
    monkeypatch.setenv("VERTEX_FACT_SOR", "shadow")

    snapshot = load_program_facts("acme", programs_root=programs_root)

    assert len(project_action_items(snapshot)) == 1


def test_claim_entry_projection_tracks_current_open_claims_only(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    open_claim = ClaimEntry(
        id="claim-open",
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=12,
        workstream_id=None,
        text="Launch gate remains blocked on partner signoff.",
        entity_refs=("WI:123",),
        claim_date=date(2026, 5, 29),
        owner_alias="alex",
        due_date=date(2026, 6, 3),
    )
    closed_claim = ClaimEntry(
        id="claim-closed",
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=12,
        workstream_id="core",
        text="Mitigation plan is drafted.",
        entity_refs=(),
        claim_date=date(2026, 5, 29),
        owner_alias="operator",
        due_date=None,
    )
    append_claim_entry(open_claim, programs_root=programs_root)
    append_claim_entry(closed_claim, programs_root=programs_root)
    append_claim_status_update(
        "acme",
        ClaimStatusUpdate(
            claim_id="claim-closed",
            new_status="resolved",
            updated_at=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
            updated_by="operator",
            note="Closed after mitigation review.",
        ),
        programs_root=programs_root,
    )

    snapshot = load_program_facts("acme", programs_root=programs_root)

    assert _strip_claim_meta(project_claim_entries(snapshot)) == load_open_claims("acme", programs_root=programs_root)
    assert {claim.id for claim in project_claim_entries(snapshot)} == {"claim-open"}


def test_decision_ask_projection_tracks_current_open_asks_only(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    open_ask = DecisionAsk(
        id="ask-open",
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=12,
        text="Need LT decision on launch sequence.",
        entity_refs=("WI:124",),
        ask_date=date(2026, 5, 29),
        owner_alias="alex",
        expiry_date=date(2026, 6, 6),
        resurfacing_policy=ResurfacingPolicy(watch_days=3, nudge_days=6, escalate_days=9),
    )
    closed_ask = DecisionAsk(
        id="ask-closed",
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=12,
        text="Need approval for mitigation option.",
        entity_refs=(),
        ask_date=date(2026, 5, 29),
        owner_alias="operator",
    )
    append_decision_ask(open_ask, programs_root=programs_root)
    append_decision_ask(closed_ask, programs_root=programs_root)
    append_claim_status_update(
        "acme",
        ClaimStatusUpdate(
            claim_id="ask-open",
            new_status="open",
            updated_at=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
            updated_by="operator",
            note="Ask refreshed after LT follow-up.",
        ),
        programs_root=programs_root,
    )
    append_claim_status_update(
        "acme",
        ClaimStatusUpdate(
            claim_id="ask-closed",
            new_status="resolved",
            updated_at=datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc),
            updated_by="operator",
            note="Ask resolved.",
        ),
        programs_root=programs_root,
    )

    snapshot = load_program_facts("acme", programs_root=programs_root)

    assert _strip_decision_ask_meta(project_decision_asks(snapshot)) == load_open_decision_asks("acme", programs_root=programs_root)
    assert {ask.id for ask in project_decision_asks(snapshot)} == {"ask-open"}
    assert project_decision_asks(snapshot)[0].last_touched_at == datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc)


def test_claim_status_update_projection_tracks_raw_claim_and_ask_history(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=12,
            workstream_id=None,
            text="Launch gate depends on partner readiness.",
            entity_refs=("WI:123",),
            claim_date=date(2026, 5, 29),
            owner_alias="alex",
            due_date=date(2026, 6, 4),
        ),
        programs_root=programs_root,
    )
    append_decision_ask(
        DecisionAsk(
            id="ask-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=12,
            text="Need LT decision on launch sequencing.",
            entity_refs=("WI:124",),
            ask_date=date(2026, 5, 29),
            owner_alias="alex",
        ),
        programs_root=programs_root,
    )
    append_claim_status_update(
        "acme",
        ClaimStatusUpdate(
            claim_id="claim-1",
            new_status="stale",
            updated_at=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
            updated_by="alex",
            note="Need refreshed evidence.",
        ),
        programs_root=programs_root,
    )
    append_claim_status_update(
        "acme",
        ClaimStatusUpdate(
            claim_id="ask-1",
            new_status="resolved",
            updated_at=datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc),
            updated_by="operator",
            note="LT approved the mitigation.",
        ),
        programs_root=programs_root,
    )

    snapshot = load_program_facts("acme", programs_root=programs_root)

    assert project_claim_status_updates(snapshot) == load_claim_status_updates("acme", programs_root=programs_root)
    assert [update.claim_id for update in project_claim_status_updates(snapshot)] == ["claim-1", "ask-1"]


def test_claim_and_decision_ask_projections_primary_mode_follow_fact_store_status_updates(tmp_path: Path, monkeypatch) -> None:
    programs_root = _programs_root(tmp_path)
    append_claim_entry(
        ClaimEntry(
            id="claim-open",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=12,
            workstream_id=None,
            text="Open claim remains active.",
            entity_refs=("WI:125",),
            claim_date=date(2026, 5, 29),
            owner_alias="alex",
            due_date=date(2026, 6, 4),
        ),
        programs_root=programs_root,
    )
    append_claim_entry(
        ClaimEntry(
            id="claim-stale",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=12,
            workstream_id=None,
            text="Claim needs refreshed evidence.",
            entity_refs=("WI:126",),
            claim_date=date(2026, 5, 29),
            owner_alias="operator",
            due_date=date(2026, 6, 4),
        ),
        programs_root=programs_root,
    )
    append_decision_ask(
        DecisionAsk(
            id="ask-open",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=12,
            text="Need LT decision on launch sequencing.",
            entity_refs=("WI:124",),
            ask_date=date(2026, 5, 29),
            owner_alias="alex",
        ),
        programs_root=programs_root,
    )
    append_decision_ask(
        DecisionAsk(
            id="ask-resolved",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=12,
            text="Need approval for mitigation option.",
            entity_refs=(),
            ask_date=date(2026, 5, 29),
            owner_alias="operator",
        ),
        programs_root=programs_root,
    )
    append_claim_status_update(
        "acme",
        ClaimStatusUpdate(
            claim_id="claim-stale",
            new_status="stale",
            updated_at=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
            updated_by="operator",
            note="Need refreshed evidence.",
        ),
        programs_root=programs_root,
    )
    append_claim_status_update(
        "acme",
        ClaimStatusUpdate(
            claim_id="ask-open",
            new_status="open",
            updated_at=datetime(2026, 5, 30, 9, 30, tzinfo=timezone.utc),
            updated_by="operator",
            note="Ask refreshed after follow-up.",
        ),
        programs_root=programs_root,
    )
    append_claim_status_update(
        "acme",
        ClaimStatusUpdate(
            claim_id="ask-resolved",
            new_status="resolved",
            updated_at=datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc),
            updated_by="operator",
            note="Ask resolved.",
        ),
        programs_root=programs_root,
    )
    monkeypatch.setenv("VERTEX_FACT_SOR", "primary")

    snapshot = load_program_facts("acme", programs_root=programs_root)

    assert {claim.id for claim in project_claim_entries(snapshot)} == {"claim-open"}
    assert {ask.id for ask in project_decision_asks(snapshot)} == {"ask-open"}
    assert project_decision_asks(snapshot)[0].last_touched_at == datetime(2026, 5, 30, 9, 30, tzinfo=timezone.utc)


def test_baseline_trust_event_projection_tracks_history_entries(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    editions_root = tmp_path / "editions"
    editions_root.mkdir(parents=True, exist_ok=True)
    (editions_root / "acme_weekly.yaml").write_text("program_id: acme\n", encoding="utf-8")
    advance_trusted_baseline(
        "acme_weekly",
        12,
        established_at=datetime(2026, 5, 29, 9, 0, tzinfo=timezone.utc),
        established_by="alex",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    record_untrusted_issue(
        "acme_weekly",
        13,
        recorded_at=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
        recorded_by="operator",
        reason="Issue 013 should not advance the trusted baseline.",
        editions_root=editions_root,
        programs_root=programs_root,
    )

    snapshot = load_program_facts("acme", programs_root=programs_root)
    baseline = load_trusted_baseline_for_program("acme", programs_root=programs_root)

    assert baseline is not None
    assert project_baseline_trust_events(snapshot) == baseline.history
    assert [entry.action for entry in project_baseline_trust_events(snapshot)] == ["established", "untrusted"]


def test_skip_issue_projection_tracks_archive_skip_entries(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    archive_root = tmp_path / "archive"
    edition_dir = programs_root / "acme" / "editions"
    edition_dir.mkdir(parents=True, exist_ok=True)
    (edition_dir / "acme_weekly.yaml").write_text("program_id: acme\n", encoding="utf-8")
    write_skipped_issue(
        "acme_weekly",
        7,
        "Holiday week",
        archive_root=archive_root,
        acquire_lock=False,
    )

    snapshot = load_program_facts(
        "acme",
        programs_root=programs_root,
        archive_root=archive_root,
    )

    assert len(project_skip_issues(snapshot)) == 1
    assert project_skip_issues(snapshot)[0].edition_id == "acme_weekly"
    assert project_skip_issues(snapshot)[0].issue_number == 7
    assert project_skip_issues(snapshot)[0].reason == "Holiday week"


def test_program_event_skip_issue_round_trip(tmp_path: Path) -> None:
    """Phase 6 §22 Step 7: a `ProgramEvent(fact_type="event.issue.skip")`
    written via `append_program_event` is round-trippable through the
    projection — `project_skip_issues` returns the skip entry, the
    payload (metadata) is preserved, and a legacy `skip.issue` fact
    for the same edition+issue is deduped.
    """
    from src.core.program_fact_store import ProgramEvent, append_program_event

    programs_root = _programs_root(tmp_path)
    archive_root = tmp_path / "archive"
    edition_dir = programs_root / "acme" / "editions"
    edition_dir.mkdir(parents=True, exist_ok=True)
    (edition_dir / "acme_weekly.yaml").write_text("program_id: acme\n", encoding="utf-8")
    # Write a `event.issue.skip` event (the new write path).
    append_program_event(
        "acme",
        ProgramEvent(
            fact_type="event.issue.skip",
            natural_key="skip:acme_weekly:42",
            metadata={
                "edition_id": "acme_weekly",
                "issue_number": 42,
                "generated_at": "2026-06-08T00:00:00+00:00",
                "reason": "Operator unavailable",
            },
        ),
        recorded_at=datetime(2026, 6, 8, tzinfo=timezone.utc),
        db_root=programs_root.parent,
    )
    # Also seed a legacy `skip.issue` fact for the SAME skip to
    # exercise the dedupe in `project_skip_issues`.
    write_skipped_issue(
        "acme_weekly",
        42,
        "Operator unavailable",
        archive_root=archive_root,
        acquire_lock=False,
    )

    snapshot = load_program_facts(
        "acme",
        programs_root=programs_root,
        archive_root=archive_root,
    )

    skip_entries = project_skip_issues(snapshot)
    # Exactly one entry — the dedupe kicked in.
    assert len(skip_entries) == 1, (
        f"Phase 6 §22 Step 7: expected 1 skip entry after dedupe, "
        f"got {len(skip_entries)}: {skip_entries!r}"
    )
    assert skip_entries[0].edition_id == "acme_weekly"
    assert skip_entries[0].issue_number == 42
    assert skip_entries[0].reason == "Operator unavailable"


def test_resolve_fact_sor_mode_falls_back_to_legacy_for_unknown_value(monkeypatch) -> None:
    monkeypatch.setenv("VERTEX_FACT_SOR", "unexpected")

    assert resolve_fact_sor_mode() == "legacy"


def test_resolve_fact_sor_mode_uses_persisted_program_state_when_env_is_unset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    programs_root = _programs_root(tmp_path)
    save_fact_sor_state(
        "acme",
        mode="primary",
        recorded_at=datetime(2026, 6, 7, 9, 0, tzinfo=timezone.utc),
        recorded_by="operator",
        programs_root=programs_root,
    )

    assert resolve_fact_sor_mode(program_id="acme", programs_root=programs_root) == "primary"


def test_lower_precedence_write_becomes_proposed_revision(tmp_path) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)
    initial_time = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    later_time = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)

    accepted = store.append_fact(
        ProgramFactInput(
            fact_type="risk",
            scope="workstream:deployment",
            entity_refs=("WS:deployment",),
            payload={"risk": "high"},
            precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
            source_signal_ids=("pm-judgment",),
        ),
        recorded_at=initial_time,
    )
    proposed = store.append_fact(
        ProgramFactInput(
            fact_type="risk",
            scope="workstream:deployment",
            entity_refs=("WS:deployment",),
            payload={"risk": "low"},
            precedence=FactPrecedence.RAW_TELEMETRY,
            source_signal_ids=("telemetry-1",),
        ),
        recorded_at=later_time,
    )

    current_snapshot = store.snapshot(as_of=later_time)
    proposed_revisions = store.list_proposed_revisions()

    assert accepted.action == "created"
    assert proposed.action == "proposed_revision"
    assert len(current_snapshot.facts) == 1
    assert current_snapshot.facts[0].payload["risk"] == "high"
    assert len(proposed_revisions) == 1
    assert proposed_revisions[0].payload["risk"] == "low"
    assert proposed_revisions[0].proposed_against_revision_id == accepted.revision.revision_id


def test_higher_precedence_write_supersedes_prior_revision_and_supports_as_of(tmp_path) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)
    t1 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)

    first = store.append_fact(
        ProgramFactInput(
            fact_type="decision",
            scope="program",
            entity_refs=("PROGRAM:acme",),
            payload={"decision": "Gate remains closed"},
            precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
        ),
        recorded_at=t1,
    )
    second = store.append_fact(
        ProgramFactInput(
            fact_type="decision",
            scope="program",
            entity_refs=("PROGRAM:acme",),
            payload={"decision": "Gate approved"},
            precedence=FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
        ),
        recorded_at=t2,
    )

    snapshot_before = store.snapshot(as_of=t1)
    snapshot_after = store.snapshot(as_of=t2)

    assert first.action == "created"
    assert second.action == "superseded"
    assert snapshot_before.facts[0].payload["decision"] == "Gate remains closed"
    assert snapshot_after.facts[0].payload["decision"] == "Gate approved"
    assert snapshot_before.facts[0].fact_id == snapshot_after.facts[0].fact_id


def test_snapshot_pin_detects_later_accepted_drift(tmp_path) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)
    t1 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)

    store.append_fact(
        ProgramFactInput(
            fact_type="action",
            scope="workitem:123",
            entity_refs=("WI:123",),
            payload={"status": "open"},
        ),
        recorded_at=t1,
    )
    pin = store.pin_snapshot(
        created_at=t1,
        metadata={"edition_name": "acme_weekly", "issue_number": 78},
    )
    store.append_fact(
        ProgramFactInput(
            fact_type="action",
            scope="workitem:123",
            entity_refs=("WI:123",),
            payload={"status": "closed"},
            precedence=FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
        ),
        recorded_at=t2,
    )

    drift = store.detect_drift(pin.snapshot_id)

    assert pin.pinned_revision_count == 1
    assert len(drift) == 1
    assert drift[0].payload["status"] == "closed"


def test_load_program_facts_includes_current_state_shim_for_live_reads(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VERTEX_FACT_SOR", "legacy")
    programs_root = _programs_root(tmp_path)
    (programs_root / "acme").mkdir(parents=True, exist_ok=True)
    (programs_root / "acme" / "program.yaml").write_text("schema_version: '1.0'\nid: acme\nname: Acme\n", encoding="utf-8")

    action = ActionItem(
        id="act-1",
        program_id="acme",
        text="Follow up on launch blocker",
        owner_alias="operator",
        due_date=date(2026, 6, 5),
        status=ActionStatus.OPEN,
        source_signal_id="signal-1",
        source_type=ActionSourceType.MANUAL,
        linked_work_item_ids=(12345,),
        linked_claim_id="claim-1",
        linked_risk_id="risk-1",
        workstream_id="ws-launch",
        created_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        resolved_at=None,
        resolution_note=None,
    )
    append_action("acme", action, programs_root=programs_root)

    risk = RiskEntry(
        id="risk-1",
        program_id="acme",
        title="Launch gate remains blocked",
        description="Critical dependency remains unresolved.",
        probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.DEPENDENCY,
        owner_alias="operator",
        mitigation_plan="Escalate daily until resolved.",
        mitigation_due_date=date(2026, 6, 7),
        linked_workstream_ids=("ws-launch",),
        linked_work_item_ids=(12345,),
        linked_milestone_ids=("m1",),
        linked_claim_ids=("claim-1",),
        linked_action_ids=("act-1",),
        status=RiskStatus.OPEN,
        identified_date=date(2026, 6, 1),
        identified_in_vertex_issue=78,
        last_reviewed_date=date(2026, 6, 2),
        entity_refs=("WI:12345",),
    )
    save_risk_register("acme", (risk,), programs_root=programs_root)

    decision = DecisionEntry(
        id="decision-1",
        program_id="acme",
        title="Approve launch exception",
        context="Partner dependency remains open.",
        decision="Proceed with mitigation plan.",
        rationale="Leadership accepted the temporary risk.",
        alternatives_considered=("Delay by one week",),
        decided_by="lt",
        decision_date=date(2026, 6, 3),
        status=DecisionStatus.DECIDED,
        superseded_by=None,
        linked_claim_id="claim-1",
        linked_risk_id="risk-1",
        linked_action_ids=("act-1",),
        workstream_id="ws-launch",
        entity_refs=("WI:12345",),
        review_by=date(2026, 6, 17),
        linked_milestone_ids=("m1",),
        last_reviewed_date=date(2026, 6, 4),
    )
    save_decisions("acme", (decision,), programs_root=programs_root)

    assumption = Assumption(
        id="assumption-1",
        program_id="acme",
        text="Partner schema lands before launch cutoff.",
        validation_method="Review partner schedule",
        validation_due=date(2026, 6, 8),
        status=AssumptionStatus.UNVALIDATED,
        category="schedule",
        linked_risk_id="risk-1",
        linked_workstream_ids=("ws-launch",),
        linked_milestone_id="m1",
        owner_alias="operator",
        identified_date=date(2026, 6, 1),
        entity_refs=("WI:12345",),
        linked_milestone_ids=("m1",),
        last_reviewed_date=date(2026, 6, 5),
    )
    save_assumptions("acme", (assumption,), programs_root=programs_root)

    dependency = Dependency(
        id="dep-1",
        from_program_id="acme",
        from_workstream_id="ws-launch",
        from_item_id=12345,
        from_milestone_id=None,
        to_program_id="shared",
        to_workstream_id="ws-partner",
        to_item_id=67890,
        to_milestone_id=None,
        dependency_type=DependencyType.BLOCKS,
        risk_if_broken="Launch slips by two weeks.",
        mitigation="Escalate through partner PMs.",
        status=DependencyStatus.ACTIVE,
        owner_alias="operator",
        resolution_path="Partner milestone",
        planned_resolution_date=date(2026, 6, 10),
        schedule_status=None,
        linked_risk_ids=("risk-1",),
    )
    save_dependencies("acme", (dependency,), programs_root=programs_root)

    milestone = Milestone(
        id="m1",
        program_id="acme",
        name="Launch readiness",
        target_date=date(2026, 6, 10),
        owner_alias="operator",
        status=MilestoneStatus.ON_TRACK,
        exit_criteria=("Dry run complete",),
        linked_workstream_ids=("ws-launch",),
        linked_work_item_ids=(12345,),
        notes="Awaiting final rehearsal.",
        last_reviewed_date=date(2026, 6, 6),
    )
    save_milestones("acme", (milestone,), programs_root=programs_root)

    snapshot = load_program_facts("acme", db_root=tmp_path, programs_root=programs_root)

    assert _strip_action_meta(project_action_items(snapshot)) == load_actions("acme", programs_root=programs_root)
    assert project_assumptions(snapshot) == load_assumptions("acme", programs_root=programs_root)
    assert _strip_decision_meta(project_decision_entries(snapshot)) == load_decisions("acme", programs_root=programs_root)
    assert _strip_risk_meta(project_risk_entries(snapshot)) == load_risk_register("acme", programs_root=programs_root)
    assert project_dependencies(snapshot) == load_dependencies("acme", programs_root=programs_root)
    assert project_milestones(snapshot) == load_milestones("acme", programs_root=programs_root)


def test_decision_entry_projection_ignores_closed_fact_revisions(tmp_path: Path) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)
    recorded_at = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    payload = {
        "id": "decision-1",
        "program_id": "acme",
        "title": "Approve launch exception",
        "context": "Partner dependency remains open.",
        "decision": "Proceed with mitigation plan.",
        "rationale": None,
        "alternatives_considered": [],
        "decided_by": "lt",
        "decision_date": "2026-06-03",
        "status": "decided",
        "superseded_by": None,
        "linked_claim_id": None,
        "linked_risk_id": None,
        "linked_action_ids": [],
        "workstream_id": None,
        "entity_refs": [],
        "review_by": None,
        "linked_milestone_ids": [],
        "last_reviewed_date": None,
    }

    store.append_fact(
        ProgramFactInput(
            fact_type="decision.entry",
            scope="program",
            entity_refs=("DECISION:decision-1",),
            payload=payload,
            natural_key="decision.entry|decision-1|DECISION:decision-1",
        ),
        recorded_at=recorded_at,
    )
    store.append_fact(
        ProgramFactInput(
            fact_type="decision.entry",
            scope="program",
            entity_refs=("DECISION:decision-1",),
            payload=payload,
            natural_key="decision.entry|decision-1|DECISION:decision-1",
            lifecycle_state=FactLifecycleState.CLOSED,
            valid_until=recorded_at,
        ),
        recorded_at=recorded_at,
    )

    snapshot = store.snapshot(as_of=recorded_at)

    assert project_decision_entries(snapshot) == ()


def test_dependency_projection_ignores_closed_fact_revisions(tmp_path: Path) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)
    recorded_at = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    payload = {
        "id": "dep-1",
        "from_program_id": "acme",
        "from_workstream_id": "ws-launch",
        "from_item_id": 12345,
        "from_milestone_id": None,
        "to_program_id": "fabrikam",
        "to_workstream_id": "ws-buildouts",
        "to_item_id": 67890,
        "to_milestone_id": None,
        "dependency_type": "blocks",
        "risk_if_broken": "Launch slips.",
        "mitigation": "Escalate through partner PMs.",
        "status": "active",
        "owner_alias": "operator",
        "resolution_path": "Weekly sync",
        "planned_resolution_date": "2026-06-10",
        "schedule_status": "at_risk",
        "linked_risk_ids": ["risk-1"],
    }

    store.append_fact(
        ProgramFactInput(
            fact_type="dependency.link",
            scope="program",
            entity_refs=("DEPENDENCY:dep-1",),
            payload=payload,
            natural_key=build_natural_key("dependency.link", entity_refs=("DEPENDENCY:dep-1",), scope="program"),
        ),
        recorded_at=recorded_at,
    )
    store.append_fact(
        ProgramFactInput(
            fact_type="dependency.link",
            scope="program",
            entity_refs=("DEPENDENCY:dep-1",),
            payload=payload,
            natural_key=build_natural_key("dependency.link", entity_refs=("DEPENDENCY:dep-1",), scope="program"),
            lifecycle_state=FactLifecycleState.CLOSED,
            valid_until=recorded_at,
        ),
        recorded_at=recorded_at,
    )

    snapshot = store.snapshot(as_of=recorded_at)

    assert project_dependencies(snapshot) == ()


def test_milestone_projection_ignores_closed_fact_revisions(tmp_path: Path) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)
    recorded_at = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    payload = {
        "id": "m1",
        "program_id": "acme",
        "name": "Launch readiness",
        "target_date": "2026-06-10",
        "owner_alias": "operator",
        "status": "on_track",
        "exit_criteria": ["Dry run complete"],
        "linked_workstream_ids": ["ws-launch"],
        "linked_work_item_ids": [12345],
        "notes": "Awaiting final rehearsal.",
        "last_reviewed_date": "2026-06-06",
    }

    store.append_fact(
        ProgramFactInput(
            fact_type="milestone.entry",
            scope="program",
            entity_refs=("MILESTONE:m1",),
            payload=payload,
            natural_key=build_natural_key("milestone.entry", entity_refs=("MILESTONE:m1",), scope="program"),
        ),
        recorded_at=recorded_at,
    )
    store.append_fact(
        ProgramFactInput(
            fact_type="milestone.entry",
            scope="program",
            entity_refs=("MILESTONE:m1",),
            payload=payload,
            natural_key=build_natural_key("milestone.entry", entity_refs=("MILESTONE:m1",), scope="program"),
            lifecycle_state=FactLifecycleState.CLOSED,
            valid_until=recorded_at,
        ),
        recorded_at=recorded_at,
    )

    snapshot = store.snapshot(as_of=recorded_at)

    assert project_milestones(snapshot) == ()


def test_risk_projection_ignores_closed_fact_revisions(tmp_path: Path) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)
    recorded_at = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    payload = {
        "id": "risk-1",
        "program_id": "acme",
        "title": "Launch gate remains blocked",
        "description": "Critical dependency remains unresolved.",
        "probability": "likely",
        "impact": "high",
        "category": "dependency",
        "owner_alias": "operator",
        "mitigation_plan": "Escalate daily until resolved.",
        "mitigation_due_date": "2026-06-07",
        "linked_workstream_ids": ["ws-launch"],
        "linked_work_item_ids": [12345],
        "linked_milestone_ids": ["m1"],
        "linked_claim_ids": ["claim-1"],
        "linked_action_ids": ["act-1"],
        "status": "open",
        "identified_date": "2026-06-01",
        "identified_in_vertex_issue": 78,
        "last_reviewed_date": "2026-06-02",
        "entity_refs": ["WI:12345"],
        "source_signal_ids": [],
    }

    store.append_fact(
        ProgramFactInput(
            fact_type="risk.entry",
            scope="program",
            entity_refs=("RISK:risk-1",),
            payload=payload,
            natural_key=build_natural_key("risk.entry", entity_refs=("RISK:risk-1",), scope="program"),
        ),
        recorded_at=recorded_at,
    )
    store.append_fact(
        ProgramFactInput(
            fact_type="risk.entry",
            scope="program",
            entity_refs=("RISK:risk-1",),
            payload=payload,
            natural_key=build_natural_key("risk.entry", entity_refs=("RISK:risk-1",), scope="program"),
            lifecycle_state=FactLifecycleState.CLOSED,
            valid_until=recorded_at,
        ),
        recorded_at=recorded_at,
    )

    snapshot = store.snapshot(as_of=recorded_at)

    assert project_risk_entries(snapshot) == ()


def test_workstream_projection_ignores_closed_fact_revisions(tmp_path: Path) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)
    recorded_at = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    payload = {
        "id": "ws-launch",
        "name": "Launch",
        "owner_person_id": "person:operator",
        "status": "active",
        "aliases": ["launch"],
        "area_paths": ["One\\Acme\\Launch"],
        "ado_team": "Platform",
        "ado_pipeline_ids": ["42"],
        "ado_repository_ids": ["vertex"],
        "pm_owner": "operator",
        "eng_owner": "eng",
        "accountable_owner": "priya",
        "accountable_email": "priya@example.com",
        "responsible_owners": ["alex", "sam"],
        "consulted_owners": ["lee"],
        "informed_owners": ["jamie"],
        "dri_email": "operator@example.com",
        "alternate_owner": "backup@example.com",
        "always_notify": ["maintainer"],
        "description": "Launch workstream",
        "why_it_matters": "Needed for exit",
        "history_summary": "Was blocked last month",
        "leadership_sensitivity": "high",
        "current_blocker": "Waiting on partner",
        "ado_saved_query_ids": ["query-1"],
        "last_reviewed_date": "2025-01-05",
        "signal_sources": None,
    }

    store.append_fact(
        ProgramFactInput(
            fact_type="workstream.entry",
            scope="program",
            entity_refs=("WS:ws-launch",),
            payload=payload,
            natural_key=build_natural_key("workstream.entry", entity_refs=("WS:ws-launch",), scope="program"),
        ),
        recorded_at=recorded_at,
    )
    store.append_fact(
        ProgramFactInput(
            fact_type="workstream.entry",
            scope="program",
            entity_refs=("WS:ws-launch",),
            payload=payload,
            natural_key=build_natural_key("workstream.entry", entity_refs=("WS:ws-launch",), scope="program"),
            lifecycle_state=FactLifecycleState.CLOSED,
            valid_until=recorded_at,
        ),
        recorded_at=recorded_at,
    )

    snapshot = store.snapshot(as_of=recorded_at)

    assert project_workstreams(snapshot) == ()


def test_load_program_facts_derives_db_root_from_programs_root(monkeypatch, tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    sentinel = object()
    captured: list[Path | None] = []

    class _FakeStore:
        def __init__(self, program_id: str, *, home_root=None, db_root=None) -> None:
            assert program_id == "acme"
            captured.append(db_root)

        def snapshot(self, *, as_of=None):
            assert as_of is not None
            return sentinel

    monkeypatch.setattr("src.core.program_fact_store.ProgramFactStore", _FakeStore)

    snapshot = load_program_facts(
        "acme",
        as_of=datetime(2026, 6, 1, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    assert snapshot is sentinel
    assert captured == [tmp_path]


def test_persist_program_fact_snapshot_writes_current_state_facts_to_store(tmp_path: Path) -> None:
    program_id = "acme"
    programs_root = _programs_root(tmp_path)
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)

    append_action(
        program_id,
        ActionItem(
            id="action-1",
            program_id=program_id,
            text="Follow up",
            owner_alias="alex",
            due_date=date(2026, 5, 20),
            status=ActionStatus.OPEN,
            source_signal_id="signal-1",
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(),
            linked_claim_id=None,
            linked_risk_id="risk-1",
            workstream_id=None,
            created_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )
    save_risk_register(
        program_id,
        (
            RiskEntry(
                id="risk-1",
                program_id=program_id,
                title="Supplier delay",
                description="Vendor milestone is late.",
                probability=RiskProbability.POSSIBLE,
                impact=RiskImpact.HIGH,
                category=RiskCategory.DEPENDENCY,
                owner_alias="alex",
                mitigation_plan="Follow up daily",
                mitigation_due_date=date(2026, 5, 20),
                linked_workstream_ids=(),
                linked_work_item_ids=(),
                linked_milestone_ids=(),
                linked_claim_ids=(),
                linked_action_ids=("action-1",),
                status=RiskStatus.OPEN,
                identified_date=date(2026, 5, 1),
                identified_in_vertex_issue=None,
                last_reviewed_date=date(2026, 5, 2),
                entity_refs=(),
            ),
        ),
        programs_root=programs_root,
    )
    save_dependencies(
        program_id,
        (
            Dependency(
                id="dep-1",
                from_program_id=program_id,
                from_workstream_id="ws-launch",
                from_item_id=12345,
                from_milestone_id=None,
                to_program_id="partner",
                to_workstream_id="ws-ship",
                to_item_id=67890,
                to_milestone_id=None,
                dependency_type=DependencyType.BLOCKS,
                risk_if_broken="Launch slips",
                mitigation="Confirm partner readiness",
                status=DependencyStatus.ACTIVE,
                owner_alias="alex",
                resolution_path="Weekly sync",
                planned_resolution_date=date(2026, 5, 21),
                schedule_status=None,
                linked_risk_ids=("risk-1",),
            ),
        ),
        programs_root=programs_root,
    )
    recorded_at = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
    current_snapshot = load_program_facts(
        program_id,
        fact_types=("action.item", "risk.entry", "dependency.link"),
        programs_root=programs_root,
        db_root=tmp_path,
    )

    write_results = persist_program_fact_snapshot(
        current_snapshot,
        recorded_at=recorded_at,
        db_root=tmp_path,
    )

    assert len(write_results) == 3
    assert {result.revision.fact_type: result.action for result in write_results} == {
        "action.item": "noop",
        "dependency.link": "noop",
        "risk.entry": "noop",
    }

    historical_snapshot = load_program_facts(
        program_id,
        fact_types=("action.item", "risk.entry", "dependency.link"),
        as_of=recorded_at,
        db_root=tmp_path,
    )

    assert [action.id for action in project_action_items(historical_snapshot)] == ["action-1"]
    assert project_risk_entries(historical_snapshot) == ()
    assert project_dependencies(historical_snapshot) == ()


def test_load_program_facts_fact_type_filter_limits_live_shims(monkeypatch, tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    (programs_root / "acme").mkdir(parents=True, exist_ok=True)
    (programs_root / "acme" / "program.yaml").write_text("schema_version: '1.0'\nid: acme\nname: Acme\n", encoding="utf-8")
    risk = RiskEntry(
        id="risk-1",
        program_id="acme",
        title="Launch gate remains blocked",
        description="Critical dependency remains unresolved.",
        probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.DEPENDENCY,
        owner_alias="operator",
        mitigation_plan="Escalate daily until resolved.",
        mitigation_due_date=date(2026, 6, 7),
        linked_workstream_ids=("ws-launch",),
        linked_work_item_ids=(12345,),
        linked_milestone_ids=("m1",),
        linked_claim_ids=("claim-1",),
        linked_action_ids=("act-1",),
        status=RiskStatus.OPEN,
        identified_date=date(2026, 6, 1),
        identified_in_vertex_issue=78,
        last_reviewed_date=date(2026, 6, 2),
        entity_refs=("WI:12345",),
    )
    save_risk_register("acme", (risk,), programs_root=programs_root)

    def _boom(*args, **kwargs):
        raise AssertionError("action shim should not load")

    monkeypatch.setattr("src.core.action_tracker.load_actions", _boom)

    snapshot = load_program_facts("acme", db_root=tmp_path, programs_root=programs_root, fact_types=("risk.entry",))

    assert _strip_risk_meta(project_risk_entries(snapshot)) == (risk,)
    assert project_action_items(snapshot) == ()


def test_load_program_facts_fact_type_filter_limits_assumption_shims(monkeypatch, tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    (programs_root / "acme").mkdir(parents=True, exist_ok=True)
    (programs_root / "acme" / "program.yaml").write_text("schema_version: '1.0'\nid: acme\nname: Acme\n", encoding="utf-8")
    assumption = Assumption(
        id="assumption-1",
        program_id="acme",
        text="Partner schema lands before launch cutoff.",
        validation_method=None,
        validation_due=date(2026, 6, 8),
        status=AssumptionStatus.UNVALIDATED,
        linked_risk_id=None,
        linked_milestone_id=None,
        owner_alias="operator",
        identified_date=date(2026, 6, 1),
        entity_refs=("WI:12345",),
    )
    save_assumptions("acme", (assumption,), programs_root=programs_root)

    def _boom(*args, **kwargs):
        raise AssertionError("action shim should not load")

    monkeypatch.setattr("src.core.action_tracker.load_actions", _boom)

    snapshot = load_program_facts(
        "acme",
        db_root=tmp_path,
        programs_root=programs_root,
        fact_types=("assumption.entry",),
    )

    assert project_assumptions(snapshot) == (assumption,)
    assert project_action_items(snapshot) == ()


def test_load_program_facts_fact_type_filter_projects_milestone_shims(monkeypatch, tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    (programs_root / "acme").mkdir(parents=True, exist_ok=True)
    (programs_root / "acme" / "program.yaml").write_text("schema_version: '1.0'\nid: acme\nname: Acme\n", encoding="utf-8")
    milestone = Milestone(
        id="m1",
        program_id="acme",
        name="Launch readiness",
        target_date=date(2026, 6, 10),
        owner_alias="operator",
        status=MilestoneStatus.AT_RISK,
        exit_criteria=("Dry run complete", "Sign-off received"),
        linked_workstream_ids=("ws-launch",),
        linked_work_item_ids=(12345,),
        notes="Waiting on partner sign-off.",
    )
    save_milestones("acme", (milestone,), programs_root=programs_root)

    def _boom(*args, **kwargs):
        raise AssertionError("action shim should not load")

    monkeypatch.setattr("src.core.action_tracker.load_actions", _boom)

    snapshot = load_program_facts(
        "acme",
        db_root=tmp_path,
        programs_root=programs_root,
        fact_types=("milestone.entry",),
    )

    assert project_milestones(snapshot) == load_milestones("acme", programs_root=programs_root)
    assert project_action_items(snapshot) == ()


def test_load_program_facts_fact_type_filter_projects_workstream_shims(monkeypatch, tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text("schema_version: '1.0'\nid: acme\nname: Acme\n", encoding="utf-8")
    (program_dir / "workstreams.yaml").write_text(
        "\n".join(
            [
                "schema_version: '2.0'",
                "workstreams:",
                "- id: ws-launch",
                "  name: Launch",
                "  aliases: [launch]",
                "  area_paths: ['One\\Acme\\Launch']",
                "  ado_team: Platform",
                "  ado_pipeline_ids: ['42']",
                "  ado_repository_ids: ['vertex']",
                "  pm_owner: operator",
                "  eng_owner: eng",
                "  raci:",
                "    accountable: priya",
                "    accountable_email: priya@example.com",
                "    responsible: ['alex', 'sam']",
                "    consulted: ['lee']",
                "    informed: ['jamie']",
                "  dri_email: operator@example.com",
                "  alternate_owner: backup@example.com",
                "  always_notify: ['maintainer']",
                "  last_reviewed_date: 2025-01-05",
                "  description: Launch workstream",
                "  why_it_matters: Needed for exit",
                "  history_summary: Was blocked last month",
                "  leadership_sensitivity: high",
                "  current_blocker: Waiting on partner",
                "  ado_saved_query_ids: ['query-1']",
                "  signal_sources:",
                "    teams_meeting_series:",
                "    - display_name: Launch sync",
                "      series_id: series-1",
                "      include_transcripts: true",
                "      work_item_ids: [101, 102]",
                "    teams_chats:",
                "    - display_name: Launch chat",
                "      thread_id: thread-1",
                "      work_item_ids: [201]",
                "    email_subject_filters: ['Launch']",
                "    workiq_keywords: ['launch readiness']",
                "    kusto_query_ids: ['kusto-1']",
                "    ado_coverage:",
                "      min_ado_count: 4",
                "      required_work_item_types: ['Feature']",
                "      suppress_coverage_alert: true",
                "    workiq_exclude_keywords: ['noise']",
                "    email_threads:",
                "    - display_name: Launch thread",
                "      thread_id: email-1",
                "      work_item_ids: [301, 302]",
                "    dependency_ado_queries:",
                "    - label: Partner blockers",
                "      resolution_path: Escalation",
                "      area_path: One\\Partner",
                "      work_item_ids: [101, 102]",
            ]
        ),
        encoding="utf-8",
    )

    expected = Workstream(
        id="ws-launch",
        name="Launch",
        aliases=("launch",),
        area_paths=("One\\Acme\\Launch",),
        ado_team="Platform",
        ado_pipeline_ids=("42",),
        ado_repository_ids=("vertex",),
        pm_owner="operator",
        eng_owner="eng",
        accountable_owner="priya",
        accountable_email="priya@example.com",
        responsible_owners=("alex", "sam"),
        consulted_owners=("lee",),
        informed_owners=("jamie",),
        dri_email="operator@example.com",
        alternate_owner="backup@example.com",
        always_notify=("maintainer",),
        last_reviewed_date=date(2025, 1, 5),
        description="Launch workstream",
        why_it_matters="Needed for exit",
        history_summary="Was blocked last month",
        leadership_sensitivity="high",
        current_blocker="Waiting on partner",
        ado_saved_query_ids=("query-1",),
        signal_sources=WorkstreamSignalSources(
            teams_meeting_series=(TeamsMeetingSeries("Launch sync", "series-1", True, (101, 102)),),
            teams_chats=(TeamsChat("Launch chat", "thread-1", (201,)),),
            email_subject_filters=("Launch",),
            workiq_keywords=("launch readiness",),
            kusto_query_ids=("kusto-1",),
            ado_coverage=ADOCoverageRequirement(
                min_ado_count=4,
                required_work_item_types=("Feature",),
                suppress_coverage_alert=True,
            ),
            workiq_exclude_keywords=("noise",),
            email_threads=(EmailThreadSource("Launch thread", "email-1", (301, 302)),),
            dependency_ado_queries=(
                DependencyADOQuery(
                    label="Partner blockers",
                    resolution_path="Escalation",
                    area_path="One\\Partner",
                    work_item_ids=(101, 102),
                ),
            ),
        ),
        owner_person_id=None,
        status="active",
    )

    def _boom(*args, **kwargs):
        raise AssertionError("action shim should not load")

    monkeypatch.setattr("src.core.action_tracker.load_actions", _boom)

    snapshot = load_program_facts(
        "acme",
        db_root=tmp_path,
        programs_root=programs_root,
        fact_types=("workstream.entry",),
    )

    assert project_workstreams(snapshot) == (expected,)
    assert project_action_items(snapshot) == ()


def test_save_workstreams_document_populates_current_fact_projection(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    (programs_root / "acme").mkdir(parents=True, exist_ok=True)

    save_workstreams_document(
        "acme",
        {
            "schema_version": "2.0",
            "workstreams": [
                {
                    "id": "ws-launch",
                    "name": "Launch",
                    "owner_person_id": "person:operator",
                    "status": "blocked",
                    "signal_sources": {
                        "teams_chats": [{"display_name": "Launch chat", "thread_id": "thread-1"}],
                    },
                }
            ],
        },
        programs_root=programs_root,
    )

    snapshot = load_program_facts(
        "acme",
        as_of=datetime.now(timezone.utc),
        db_root=tmp_path,
        programs_root=programs_root,
        fact_types=("workstream.entry",),
    )

    assert len(project_workstreams(snapshot)) == 1
    assert project_workstreams(snapshot)[0].id == "ws-launch"
    assert project_workstreams(snapshot)[0].owner_person_id == "person:operator"
    assert project_workstreams(snapshot)[0].status == "blocked"
    assert project_workstreams(snapshot)[0].signal_sources is not None
    assert project_workstreams(snapshot)[0].signal_sources.teams_chats[0].thread_id == "thread-1"


def test_save_workstreams_document_closes_removed_fact_entries(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    (programs_root / "acme").mkdir(parents=True, exist_ok=True)

    save_workstreams_document(
        "acme",
        {
            "schema_version": "2.0",
            "workstreams": [
                {"id": "ws-launch", "name": "Launch"},
                {"id": "ws-build", "name": "Build"},
            ],
        },
        programs_root=programs_root,
    )
    save_workstreams_document(
        "acme",
        {
            "schema_version": "2.0",
            "workstreams": [
                {"id": "ws-launch", "name": "Launch"},
            ],
        },
        programs_root=programs_root,
    )

    snapshot = load_program_facts(
        "acme",
        as_of=datetime.now(timezone.utc),
        db_root=tmp_path,
        programs_root=programs_root,
        fact_types=("workstream.entry",),
    )

    assert tuple(workstream.id for workstream in project_workstreams(snapshot)) == ("ws-launch",)


def test_load_program_facts_as_of_only_returns_persisted_history(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    (programs_root / "acme").mkdir(parents=True, exist_ok=True)
    append_action(
        "acme",
        ActionItem(
            id="act-1",
            program_id="acme",
            text="Current-state only action",
            owner_alias="operator",
            due_date=None,
            status=ActionStatus.OPEN,
            source_signal_id=None,
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id=None,
            created_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )

    store = ProgramFactStore("acme", db_root=tmp_path)
    recorded_at = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    store.append_fact(
        ProgramFactInput(
            fact_type="decision",
            scope="program",
            entity_refs=("PROGRAM:acme",),
            payload={"decision": "go"},
        ),
        recorded_at=recorded_at,
    )

    snapshot = load_program_facts(
        "acme",
        as_of=recorded_at,
        db_root=tmp_path,
        programs_root=programs_root,
    )

    assert [fact.fact_type for fact in snapshot.facts] == ["decision"]
