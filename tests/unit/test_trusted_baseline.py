from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.core.archive_store import write_confirmed_issue
from src.core.models import ConfirmedDimension, EditionType, RunManifest, RiskLevel, Snapshot, SnapshotItem
from src.core.pipeline import StageContext
from src.core.stages.resolution_stage import ResolutionStage
from src.core.trusted_baseline_store import (
    advance_trusted_baseline,
    load_trusted_baseline,
    mark_bridge_graduated,
    record_rollback_drill_passed,
    record_untrusted_issue,
)
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace


EDITION_NAME = "acme_weekly"


def test_advance_trusted_baseline_is_idempotent_for_same_issue(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)

    first = advance_trusted_baseline(
        EDITION_NAME,
        77,
        established_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )
    second = advance_trusted_baseline(
        EDITION_NAME,
        77,
        established_at=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        established_by="other",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    loaded = load_trusted_baseline(
        EDITION_NAME,
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    assert first is not None
    assert second is not None
    assert loaded is not None
    assert loaded.trusted_issue_number == 77
    assert len(loaded.history) == 1
    assert loaded.history[0].action == "established"
    assert loaded.established_by == "operator"


def test_record_untrusted_issue_preserves_existing_trusted_baseline(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"

    advance_trusted_baseline(
        EDITION_NAME,
        77,
        established_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    record_untrusted_issue(
        EDITION_NAME,
        78,
        recorded_at=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        recorded_by="operator",
        reason="Issue 078 is archived for audit, but it should not advance the trusted continuation baseline.",
        editions_root=editions_root,
        programs_root=programs_root,
    )

    loaded = load_trusted_baseline(
        EDITION_NAME,
        editions_root=editions_root,
        programs_root=programs_root,
    )

    assert loaded is not None
    assert loaded.trusted_issue_number == 77
    assert loaded.last_untrusted is not None
    assert loaded.last_untrusted.issue == 78
    assert loaded.history[-1].action == "untrusted"


def test_resolution_stage_prefers_trusted_baseline_issue_when_present(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    _write_confirmed_issue(archive_root, issue_number=1)
    _write_confirmed_issue(archive_root, issue_number=2)
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    ctx = ResolutionStage().execute(
        StageContext(
            edition_name=EDITION_NAME,
            issue_number=3,
            as_of=datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=(tmp_path / "programs"),
        )
    )

    assert ctx.previous_snapshot is not None
    assert ctx.previous_snapshot.issue_number == 1
    assert ctx.previous_issue_number == 1


def test_resolution_stage_falls_back_to_latest_confirmed_issue_when_trusted_baseline_absent(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    _write_confirmed_issue(archive_root, issue_number=1)
    _write_confirmed_issue(archive_root, issue_number=2)

    ctx = ResolutionStage().execute(
        StageContext(
            edition_name=EDITION_NAME,
            issue_number=3,
            as_of=datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=(tmp_path / "programs"),
        )
    )

    assert ctx.trusted_baseline_issue_number == 2
    assert ctx.overrides_seeding is not None and not ctx.overrides_seeding.seeded
    assert ctx.previous_snapshot is not None
    assert ctx.previous_snapshot.issue_number == 2
    assert ctx.previous_issue_number == 2


def _write_confirmed_issue(archive_root: Path, *, issue_number: int) -> None:
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=issue_number,
        snapshot=_build_snapshot(issue_number),
        html_body="<html><body>Rendered</body></html>",
        markdown_body="# Rendered",
        manifest=_build_manifest(issue_number),
        archive_root=archive_root,
    )


def _build_snapshot(issue_number: int) -> Snapshot:
    return Snapshot(
        issue_number=issue_number,
        generated_at=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 5, 8, 45, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=100 + issue_number,
                type="Feature",
                title=f"Deployment readiness {issue_number}",
                state="Active",
                assigned_to="Vertex Maintainer",
                area_path="One\\Adventure\\Acme",
                target_date=date(2026, 6, 30),
                risk_level=RiskLevel.MEDIUM,
                tags=["acme"],
            ),
        ),
        scorecards=(
            ConfirmedDimension(
                scorecard_name="Acme Readiness",
                name="Deployment Velocity",
                risk=RiskLevel.MEDIUM,
                prior_risk=RiskLevel.LOW,
                item_count=1,
                ado_query_url="https://dev.azure.com/your-org/One/_queries/query-id",
            ),
        ),
    )


def _build_manifest(issue_number: int) -> RunManifest:
    return RunManifest(
        manifest_id=f"manifest-{issue_number}",
        issue_number=issue_number,
        edition=EDITION_NAME,
        started_at=datetime(2026, 5, 5, 8, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
        config_hash="config",
        snapshot_hash="snapshot",
        html_hash="html",
        md_hash="md",
        ado_calls=1,
        ai_calls=0,
        ai_cost_usd=0.0,
        freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
        qg_results={"QG-4": True, "QG-5": True, "QG-6": True, "QG-8": True},
        git_sha=None,
    )


# ---------------------------------------------------------------------------
# D-10 / spec §22 Step 9: baseline promotion as a fact-store event transaction
# ---------------------------------------------------------------------------


def test_advance_trusted_baseline_appends_baseline_trust_event_fact_in_shadow_mode(
    repo_root: Path, tmp_path: Path
) -> None:
    """Spec §22 Step 9: when the program is in ``shadow`` SoR mode (set
    explicitly here so we are not at the default ``legacy`` mode), every
    ``advance_trusted_baseline`` call also appends a ``baseline.trust_event``
    fact revision so the audit trail is replay-able through the fact store.
    """
    from src.core.fact_sor_state import save_fact_sor_state
    from src.core.program_fact_store import (
        load_program_facts,
        project_baseline_trust_events,
    )

    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"
    db_root = tmp_path / "vertex-db"

    save_fact_sor_state(
        "acme",
        mode="shadow",
        recorded_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
        recorded_by="test",
        programs_root=programs_root,
    )

    advance_trusted_baseline(
        EDITION_NAME,
        78,
        established_at=datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=editions_root,
        programs_root=programs_root,
    )

    snapshot = load_program_facts(
        "acme",
        programs_root=programs_root,
        editions_root=editions_root,
        archive_root=tmp_path / "archive",
        db_root=db_root,
        fact_types=("baseline.trust_event",),
    )
    events = project_baseline_trust_events(snapshot)
    matching = [event for event in events if event.issue == 78]
    assert len(matching) == 1
    assert matching[0].action == "established"
    assert matching[0].by == "operator"


def test_advance_trusted_baseline_does_not_write_fact_in_legacy_mode(
    repo_root: Path, tmp_path: Path
) -> None:
    """In ``legacy`` SoR mode (the pre-Phase-6 default), baseline promotion
    is YAML-only — the fact store is not yet authoritative so no
    ``baseline.trust_event`` fact is appended.  This preserves the
    pre-rev-320 contract for any program that has not yet opted into
    shadow / primary mode.

    Note: the snapshot returned by ``load_program_facts`` may still contain
    shim-produced ``baseline.trust_event`` facts (the shim reads from the
    legacy YAML), so we assert against the **SQLite-backed** store directly
    to prove that ``advance_trusted_baseline`` did *not* open the fact
    store for writes in legacy mode.
    """
    from src.core.fact_sor_state import load_fact_sor_state
    from src.core.program_fact_store import ProgramFactStore

    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"
    db_root = tmp_path / "vertex-db"

    pre_state = load_fact_sor_state("acme", programs_root=programs_root)
    assert pre_state is None or pre_state.mode == "legacy"

    advance_trusted_baseline(
        EDITION_NAME,
        78,
        established_at=datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=editions_root,
        programs_root=programs_root,
    )

    store = ProgramFactStore("acme", db_root=db_root)
    store.initialize()
    snapshot = store.snapshot()
    direct_facts = tuple(
        fact for fact in snapshot.facts if fact.fact_type == "baseline.trust_event"
    )
    assert direct_facts == ()


# ---------------------------------------------------------------------------
# D-10 / spec §22 Step 9 (rev. 322): baseline.trust_event for the 3 remaining
# write paths (record_untrusted_issue / mark_bridge_graduated /
# record_rollback_drill_passed).  Mirror the rev. 320 advance_* tests —
# shadow mode emits a fact, legacy mode is a strict no-op.
# ---------------------------------------------------------------------------


def test_record_untrusted_issue_appends_baseline_trust_event_fact_in_shadow_mode(
    repo_root: Path, tmp_path: Path
) -> None:
    """Spec §22 Step 9: ``record_untrusted_issue`` also appends a
    ``baseline.trust_event`` fact revision with action="untrusted" when
    the program is in ``shadow`` SoR mode.
    """
    from src.core.fact_sor_state import save_fact_sor_state
    from src.core.program_fact_store import (
        load_program_facts,
        project_baseline_trust_events,
    )

    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"
    db_root = tmp_path / "vertex-db"

    save_fact_sor_state(
        "acme",
        mode="shadow",
        recorded_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
        recorded_by="test",
        programs_root=programs_root,
    )

    advance_trusted_baseline(
        EDITION_NAME,
        78,
        established_at=datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    record_untrusted_issue(
        EDITION_NAME,
        79,
        recorded_at=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
        recorded_by="operator",
        reason="Issue 079 is archived for audit, but it should not advance the trusted continuation baseline.",
        editions_root=editions_root,
        programs_root=programs_root,
    )

    snapshot = load_program_facts(
        "acme",
        programs_root=programs_root,
        editions_root=editions_root,
        archive_root=tmp_path / "archive",
        db_root=db_root,
        fact_types=("baseline.trust_event",),
    )
    events = project_baseline_trust_events(snapshot)
    untrusted = [event for event in events if event.action == "untrusted" and event.issue == 79]
    assert len(untrusted) == 1
    assert untrusted[0].by == "operator"
    assert untrusted[0].reason is not None
    assert "should not advance" in untrusted[0].reason


def test_mark_bridge_graduated_appends_baseline_trust_event_fact_in_shadow_mode(
    repo_root: Path, tmp_path: Path
) -> None:
    """Spec §22 Step 9: ``mark_bridge_graduated`` also appends a
    ``baseline.trust_event`` fact revision with action="graduated" when
    the program is in ``shadow`` SoR mode.
    """
    from src.core.fact_sor_state import save_fact_sor_state
    from src.core.program_fact_store import (
        load_program_facts,
        project_baseline_trust_events,
    )

    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"
    db_root = tmp_path / "vertex-db"

    save_fact_sor_state(
        "acme",
        mode="shadow",
        recorded_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
        recorded_by="test",
        programs_root=programs_root,
    )

    advance_trusted_baseline(
        EDITION_NAME,
        78,
        established_at=datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    mark_bridge_graduated(
        EDITION_NAME,
        80,
        graduated_at=datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc),
        graduated_by="operator",
        editions_root=editions_root,
        programs_root=programs_root,
    )

    snapshot = load_program_facts(
        "acme",
        programs_root=programs_root,
        editions_root=editions_root,
        archive_root=tmp_path / "archive",
        db_root=db_root,
        fact_types=("baseline.trust_event",),
    )
    events = project_baseline_trust_events(snapshot)
    graduated = [event for event in events if event.action == "graduated" and event.issue == 80]
    assert len(graduated) == 1
    assert graduated[0].by == "operator"


def test_record_rollback_drill_passed_appends_baseline_trust_event_fact_in_shadow_mode(
    repo_root: Path, tmp_path: Path
) -> None:
    """Spec §22 Step 9: ``record_rollback_drill_passed`` also appends a
    ``baseline.trust_event`` fact revision with action="rollback_drill_passed"
    when the program is in ``shadow`` SoR mode.  The event is attached to
    the current trusted_issue_number, not the called issue (the function
    signature doesn't take an issue — it records against the live baseline).
    """
    from src.core.fact_sor_state import save_fact_sor_state
    from src.core.program_fact_store import (
        load_program_facts,
        project_baseline_trust_events,
    )

    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"
    db_root = tmp_path / "vertex-db"

    save_fact_sor_state(
        "acme",
        mode="shadow",
        recorded_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
        recorded_by="test",
        programs_root=programs_root,
    )

    advance_trusted_baseline(
        EDITION_NAME,
        78,
        established_at=datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    record_rollback_drill_passed(
        EDITION_NAME,
        recorded_at=datetime(2026, 5, 30, 11, 0, tzinfo=timezone.utc),
        recorded_by="operator",
        checkpoint_name="pre-flip-78",
        rollback_exit_code=0,
        consistency_exit_code=0,
        editions_root=editions_root,
        programs_root=programs_root,
    )

    snapshot = load_program_facts(
        "acme",
        programs_root=programs_root,
        editions_root=editions_root,
        archive_root=tmp_path / "archive",
        db_root=db_root,
        fact_types=("baseline.trust_event",),
    )
    events = project_baseline_trust_events(snapshot)
    drills = [event for event in events if event.action == "rollback_drill_passed"]
    assert len(drills) == 1
    assert drills[0].issue == 78
    assert drills[0].by == "operator"
    assert drills[0].reason is not None
    assert "pre-flip-78" in drills[0].reason


def test_record_untrusted_issue_does_not_write_fact_in_legacy_mode(
    repo_root: Path, tmp_path: Path
) -> None:
    """In ``legacy`` SoR mode, ``record_untrusted_issue`` is YAML-only — no
    fact is appended (mirror of rev. 320's
    ``test_advance_trusted_baseline_does_not_write_fact_in_legacy_mode``).
    """
    from src.core.program_fact_store import ProgramFactStore

    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"
    db_root = tmp_path / "vertex-db"

    advance_trusted_baseline(
        EDITION_NAME,
        78,
        established_at=datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    record_untrusted_issue(
        EDITION_NAME,
        79,
        recorded_at=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
        recorded_by="operator",
        reason="Audit-only retention; not a baseline advance.",
        editions_root=editions_root,
        programs_root=programs_root,
    )

    store = ProgramFactStore("acme", db_root=db_root)
    store.initialize()
    snapshot = store.snapshot()
    direct_facts = tuple(
        fact for fact in snapshot.facts if fact.fact_type == "baseline.trust_event"
    )
    assert direct_facts == ()


def test_mark_bridge_graduated_does_not_write_fact_in_legacy_mode(
    repo_root: Path, tmp_path: Path
) -> None:
    """In ``legacy`` SoR mode, ``mark_bridge_graduated`` is YAML-only — no
    fact is appended.
    """
    from src.core.program_fact_store import ProgramFactStore

    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"
    db_root = tmp_path / "vertex-db"

    advance_trusted_baseline(
        EDITION_NAME,
        78,
        established_at=datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    mark_bridge_graduated(
        EDITION_NAME,
        80,
        graduated_at=datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc),
        graduated_by="operator",
        editions_root=editions_root,
        programs_root=programs_root,
    )

    store = ProgramFactStore("acme", db_root=db_root)
    store.initialize()
    snapshot = store.snapshot()
    direct_facts = tuple(
        fact for fact in snapshot.facts if fact.fact_type == "baseline.trust_event"
    )
    assert direct_facts == ()


def test_record_rollback_drill_passed_does_not_write_fact_in_legacy_mode(
    repo_root: Path, tmp_path: Path
) -> None:
    """In ``legacy`` SoR mode, ``record_rollback_drill_passed`` is YAML-only
    — no fact is appended.
    """
    from src.core.program_fact_store import ProgramFactStore

    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"
    db_root = tmp_path / "vertex-db"

    advance_trusted_baseline(
        EDITION_NAME,
        78,
        established_at=datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    record_rollback_drill_passed(
        EDITION_NAME,
        recorded_at=datetime(2026, 5, 30, 11, 0, tzinfo=timezone.utc),
        recorded_by="operator",
        checkpoint_name="pre-flip-78",
        rollback_exit_code=0,
        consistency_exit_code=0,
        editions_root=editions_root,
        programs_root=programs_root,
    )

    store = ProgramFactStore("acme", db_root=db_root)
    store.initialize()
    snapshot = store.snapshot()
    direct_facts = tuple(
        fact for fact in snapshot.facts if fact.fact_type == "baseline.trust_event"
    )
    assert direct_facts == ()

