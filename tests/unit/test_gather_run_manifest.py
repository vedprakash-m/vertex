from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.gather_run_manifest import (
    ACTOR_IDENTITY_SYNTHETIC,
    LEGACY_CUTOFF_RUN_ID_PREFIX,
    ChannelOutcomeEntry,
    FailedRefEntry,
    GatherRunManifest,
    GatherRunStatus,
    QueryResultEntry,
    RequiredScopeStatus,
    commit_staging_run,
    compute_manifest_hash,
    create_legacy_cutoff_manifest,
    create_staging_manifest,
    fail_staging_run,
    get_committed_root,
    get_committed_run_dir,
    get_failed_run_dir,
    get_latest_full_pointer_path,
    get_latest_pointer_path,
    get_legacy_cutoff_at,
    get_quarantine_run_dir,
    get_staging_run_dir,
    get_verified_committed_run_ids,
    hash_ado_items,
    hash_query_results,
    quarantine_abandoned_staging_runs,
    read_manifest,
    resolve_latest_committed_manifest,
    resolve_latest_full_committed_manifest,
    validate_pinned_gather_run,
    write_ado_items,
    write_query_results_sidecar,
)
from src.core.workspace_lease import acquire_lease
import src.core.gather_run_manifest as gather_run_manifest


def _now() -> datetime:
    return datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _make_manifest(
    *,
    run_id: str = "gather-01ABCXYZ",
    program_id: str = "demo",
    required_scope_status: RequiredScopeStatus = RequiredScopeStatus.FULL,
    query_results: tuple[QueryResultEntry, ...] = (),
    channel_outcomes: tuple[ChannelOutcomeEntry, ...] = (),
    failed_refs: tuple[FailedRefEntry, ...] = (),
    alert_ids: tuple[str, ...] = (),
    lease_owner: str = "host-a",
    lease_fencing_token: int = 1,
) -> GatherRunManifest:
    return GatherRunManifest(
        run_id=run_id,
        status=GatherRunStatus.RUNNING,
        program_id=program_id,
        actor_identity_type="interactive",
        lease_owner=lease_owner,
        lease_fencing_token=lease_fencing_token,
        started_at=_now(),
        scope_as_of=_now(),
        required_scope_status=required_scope_status,
        query_results=query_results,
        channel_outcomes=channel_outcomes,
        failed_refs=failed_refs,
        alert_ids=alert_ids,
    )


class TestManifestHashing:
    def test_hash_is_deterministic_across_equal_manifests(self) -> None:
        manifest_a = _make_manifest()
        manifest_b = _make_manifest()
        assert compute_manifest_hash(manifest_a) == compute_manifest_hash(manifest_b)

    def test_hash_excludes_manifest_hash_field_itself(self) -> None:
        manifest = _make_manifest()
        hash_before = compute_manifest_hash(manifest)
        from dataclasses import replace

        manifest_with_hash = replace(manifest, manifest_hash="sha256:whatever")
        assert compute_manifest_hash(manifest_with_hash) == hash_before

    def test_verified_committed_run_ids_excludes_tampered_manifest(self, tmp_path: Path) -> None:
        from dataclasses import replace

        manifest = _make_manifest()
        create_staging_manifest(manifest, programs_root=tmp_path)
        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)

        assert gather_run_manifest.get_verified_committed_run_ids(
            "demo", programs_root=tmp_path
        ) == frozenset({committed.run_id})

        gather_run_manifest.write_manifest(
            get_committed_run_dir("demo", committed.run_id, programs_root=tmp_path),
            replace(committed, manifest_hash="sha256:tampered"),
        )

        assert gather_run_manifest.get_verified_committed_run_ids(
            "demo", programs_root=tmp_path
        ) == frozenset()

    def test_hash_is_order_independent_for_query_results(self) -> None:
        entry_a = QueryResultEntry(
            query_id="q1",
            scope_id="s1",
            wiql_hash="h1",
            captured_at=_now(),
            raw_count=10,
            membership_ids=("1", "2"),
            membership_hash="mh1",
            cap_reached=False,
            completeness_state="FULL",
        )
        entry_b = QueryResultEntry(
            query_id="q2",
            scope_id="s1",
            wiql_hash="h2",
            captured_at=_now(),
            raw_count=5,
            membership_ids=("3",),
            membership_hash="mh2",
            cap_reached=False,
            completeness_state="FULL",
        )
        manifest_forward = _make_manifest(query_results=(entry_a, entry_b))
        manifest_reversed = _make_manifest(query_results=(entry_b, entry_a))
        assert compute_manifest_hash(manifest_forward) == compute_manifest_hash(manifest_reversed)

    def test_hash_is_order_independent_for_channel_outcomes_and_failed_refs_and_alerts(self) -> None:
        outcome_a = ChannelOutcomeEntry(channel="ado", degraded=False, degrade_reason=None, elapsed_seconds=1.0)
        outcome_b = ChannelOutcomeEntry(channel="teams", degraded=True, degrade_reason="timeout", elapsed_seconds=2.0)
        ref_a = FailedRefEntry(ref_kind="work_item", ref_id="100", reason="not_found")
        ref_b = FailedRefEntry(ref_kind="work_item", ref_id="200", reason="throttled")
        manifest_forward = _make_manifest(
            channel_outcomes=(outcome_a, outcome_b),
            failed_refs=(ref_a, ref_b),
            alert_ids=("alert-z", "alert-a"),
        )
        manifest_reversed = _make_manifest(
            channel_outcomes=(outcome_b, outcome_a),
            failed_refs=(ref_b, ref_a),
            alert_ids=("alert-a", "alert-z"),
        )
        assert compute_manifest_hash(manifest_forward) == compute_manifest_hash(manifest_reversed)

    def test_hash_changes_when_a_stable_field_changes(self) -> None:
        manifest_a = _make_manifest()
        manifest_b = _make_manifest(run_id="gather-DIFFERENT")
        assert compute_manifest_hash(manifest_a) != compute_manifest_hash(manifest_b)

    def test_hash_ado_items_is_order_independent(self) -> None:
        rows_forward = [{"work_item_id": "200", "title": "b"}, {"work_item_id": "100", "title": "a"}]
        rows_reversed = [{"work_item_id": "100", "title": "a"}, {"work_item_id": "200", "title": "b"}]
        assert hash_ado_items(rows_forward) == hash_ado_items(rows_reversed)

    def test_hash_query_results_is_order_independent(self) -> None:
        entry_a = QueryResultEntry(
            query_id="q1", scope_id="s1", wiql_hash="h1", captured_at=_now(), raw_count=1,
            membership_ids=(), membership_hash="mh1", cap_reached=False, completeness_state="FULL",
        )
        entry_b = QueryResultEntry(
            query_id="q2", scope_id="s1", wiql_hash="h2", captured_at=_now(), raw_count=2,
            membership_ids=(), membership_hash="mh2", cap_reached=False, completeness_state="FULL",
        )
        assert hash_query_results((entry_a, entry_b)) == hash_query_results((entry_b, entry_a))


class TestResolveOracleResult:
    """D-19/AG-2.12: completeness-oracle evidence classification."""

    def test_defaults_to_same_endpoint_rerun_when_no_operator_export_recorded(self) -> None:
        assert gather_run_manifest.resolve_oracle_result("scope-a", 42, {}) == (
            gather_run_manifest.ORACLE_RESULT_SAME_ENDPOINT_RERUN
        )

    def test_matches_when_operator_export_agrees_with_raw_count(self) -> None:
        assert gather_run_manifest.resolve_oracle_result("scope-a", 42, {"scope-a": 42}) == (
            gather_run_manifest.ORACLE_RESULT_OPERATOR_EXPORT_MATCH
        )

    def test_mismatch_when_operator_export_disagrees_with_raw_count(self) -> None:
        result = gather_run_manifest.resolve_oracle_result("scope-a", 42, {"scope-a": 40})
        assert result == "operator_source_export:mismatch:reported=40:observed=42"

    def test_ignores_operator_exports_recorded_for_other_scopes(self) -> None:
        assert gather_run_manifest.resolve_oracle_result("scope-a", 42, {"scope-b": 99}) == (
            gather_run_manifest.ORACLE_RESULT_SAME_ENDPOINT_RERUN
        )

    def test_is_weak_oracle_result_true_for_none_and_same_endpoint_rerun(self) -> None:
        assert gather_run_manifest.is_weak_oracle_result(None) is True
        assert gather_run_manifest.is_weak_oracle_result(gather_run_manifest.ORACLE_RESULT_SAME_ENDPOINT_RERUN) is True

    def test_is_weak_oracle_result_false_for_operator_export_outcomes(self) -> None:
        assert gather_run_manifest.is_weak_oracle_result(gather_run_manifest.ORACLE_RESULT_OPERATOR_EXPORT_MATCH) is False
        assert gather_run_manifest.is_weak_oracle_result("operator_source_export:mismatch:reported=1:observed=2") is False

    def test_is_mismatched_oracle_result_true_only_for_mismatch_outcomes(self) -> None:
        assert gather_run_manifest.is_mismatched_oracle_result(
            "operator_source_export:mismatch:reported=1:observed=2"
        ) is True
        assert gather_run_manifest.is_mismatched_oracle_result(None) is False
        assert gather_run_manifest.is_mismatched_oracle_result(gather_run_manifest.ORACLE_RESULT_SAME_ENDPOINT_RERUN) is False
        assert gather_run_manifest.is_mismatched_oracle_result(gather_run_manifest.ORACLE_RESULT_OPERATOR_EXPORT_MATCH) is False


def test_directory_promotion_retries_transient_windows_permission_denial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "staging"
    destination = tmp_path / "committed"
    source.mkdir()
    original_replace = gather_run_manifest.os.replace
    attempts = 0

    def _flaky_replace(src, dst):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("transient handle lock")
        return original_replace(src, dst)

    monkeypatch.setattr(gather_run_manifest.os, "replace", _flaky_replace)
    monkeypatch.setattr(gather_run_manifest.time, "sleep", lambda _seconds: None)

    gather_run_manifest._promote_dir(source, destination)

    assert attempts == 2
    assert destination.is_dir()
    assert not source.exists()


class TestStagingCreationAndCommit:
    def test_create_staging_manifest_rejects_non_running_status(self, tmp_path: Path) -> None:
        from dataclasses import replace

        manifest = replace(_make_manifest(), status=GatherRunStatus.COMMITTED)
        with pytest.raises(ValueError, match="status=running"):
            create_staging_manifest(manifest, programs_root=tmp_path)

    def test_create_staging_manifest_writes_running_manifest(self, tmp_path: Path) -> None:
        manifest = _make_manifest()
        path = create_staging_manifest(manifest, programs_root=tmp_path)
        assert path.exists()
        reloaded = read_manifest(get_staging_run_dir("demo", manifest.run_id, programs_root=tmp_path))
        assert reloaded.status is GatherRunStatus.RUNNING
        assert reloaded.run_id == manifest.run_id

    def test_commit_promotes_staging_to_committed_and_updates_pointers(self, tmp_path: Path) -> None:
        manifest = _make_manifest()
        create_staging_manifest(manifest, programs_root=tmp_path)
        write_ado_items(
            get_staging_run_dir("demo", manifest.run_id, programs_root=tmp_path),
            [{"work_item_id": "1", "title": "x"}],
        )

        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)

        assert committed.status is GatherRunStatus.COMMITTED
        assert committed.manifest_hash is not None
        staging_dir = get_staging_run_dir("demo", manifest.run_id, programs_root=tmp_path)
        committed_dir = get_committed_run_dir("demo", manifest.run_id, programs_root=tmp_path)
        assert not staging_dir.exists()
        assert committed_dir.exists()
        assert (committed_dir / "ado_items.jsonl").exists()

        pointer = get_latest_pointer_path("demo", programs_root=tmp_path)
        assert pointer.exists()
        full_pointer = get_latest_full_pointer_path("demo", programs_root=tmp_path)
        assert full_pointer.exists()

    def test_commit_with_partial_scope_does_not_advance_full_pointer(self, tmp_path: Path) -> None:
        manifest = _make_manifest(required_scope_status=RequiredScopeStatus.PARTIAL)
        create_staging_manifest(manifest, programs_root=tmp_path)

        commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)

        assert get_latest_pointer_path("demo", programs_root=tmp_path).exists()
        assert not get_latest_full_pointer_path("demo", programs_root=tmp_path).exists()

    def test_resolve_latest_committed_manifest_uses_pointer(self, tmp_path: Path) -> None:
        manifest = _make_manifest()
        create_staging_manifest(manifest, programs_root=tmp_path)
        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)

        resolved = resolve_latest_committed_manifest("demo", programs_root=tmp_path)
        assert resolved is not None
        assert resolved.run_id == committed.run_id
        assert resolved.manifest_hash == committed.manifest_hash

    def test_resolve_latest_committed_manifest_falls_back_when_pointer_corrupt(self, tmp_path: Path) -> None:
        manifest = _make_manifest()
        create_staging_manifest(manifest, programs_root=tmp_path)
        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)

        pointer_path = get_latest_pointer_path("demo", programs_root=tmp_path)
        pointer_path.write_text("{not valid json", encoding="utf-8")

        resolved = resolve_latest_committed_manifest("demo", programs_root=tmp_path)
        assert resolved is not None
        assert resolved.run_id == committed.run_id

    def test_resolve_latest_full_committed_manifest_falls_back_and_filters_partial(self, tmp_path: Path) -> None:
        full_manifest = _make_manifest(run_id="gather-FULL", required_scope_status=RequiredScopeStatus.FULL)
        create_staging_manifest(full_manifest, programs_root=tmp_path)
        commit_staging_run(full_manifest, finished_at=_now(), programs_root=tmp_path)

        partial_manifest = _make_manifest(run_id="gather-PARTIAL", required_scope_status=RequiredScopeStatus.PARTIAL)
        create_staging_manifest(partial_manifest, programs_root=tmp_path)
        commit_staging_run(partial_manifest, finished_at=_now() + timedelta(minutes=5), programs_root=tmp_path)

        pointer_path = get_latest_full_pointer_path("demo", programs_root=tmp_path)
        pointer_path.write_text("{not valid json", encoding="utf-8")

        resolved = resolve_latest_full_committed_manifest("demo", programs_root=tmp_path)
        assert resolved is not None
        assert resolved.run_id == "gather-FULL"

    def test_resolve_latest_committed_manifest_returns_none_when_absent(self, tmp_path: Path) -> None:
        assert resolve_latest_committed_manifest("demo", programs_root=tmp_path) is None
        assert resolve_latest_full_committed_manifest("demo", programs_root=tmp_path) is None


class TestValidatePinnedGatherRun:
    """D-17/ARM-GATHER-11 AG-6.2: confirm dry-run/real-confirm gating on a
    pinned gather run's validity, freshness, and scope."""

    def test_none_gather_run_id_is_not_applicable(self, tmp_path: Path) -> None:
        # Pre-D-17 drafts or non-gather-pipeline programs: no failure raised.
        assert validate_pinned_gather_run(
            "demo", gather_run_id=None, gather_run_hash=None, programs_root=tmp_path
        ) == ()

    def test_valid_current_full_run_passes(self, tmp_path: Path) -> None:
        manifest = _make_manifest()
        create_staging_manifest(manifest, programs_root=tmp_path)
        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)

        failures = validate_pinned_gather_run(
            "demo",
            gather_run_id=committed.run_id,
            gather_run_hash=committed.manifest_hash,
            programs_root=tmp_path,
        )

        assert failures == ()

    def test_unknown_run_id_is_invalid(self, tmp_path: Path) -> None:
        failures = validate_pinned_gather_run(
            "demo",
            gather_run_id="gather-does-not-exist",
            gather_run_hash="deadbeef",
            programs_root=tmp_path,
        )

        assert len(failures) == 1
        assert failures[0].startswith("BLOCKED:")
        assert "invalid" in failures[0]

    def test_hash_mismatch_is_rejected(self, tmp_path: Path) -> None:
        manifest = _make_manifest()
        create_staging_manifest(manifest, programs_root=tmp_path)
        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)

        failures = validate_pinned_gather_run(
            "demo",
            gather_run_id=committed.run_id,
            gather_run_hash="not-the-real-hash",
            programs_root=tmp_path,
        )

        assert len(failures) == 1
        assert "hash verification" in failures[0]

    def test_partial_scope_pinned_run_is_rejected(self, tmp_path: Path) -> None:
        manifest = _make_manifest(required_scope_status=RequiredScopeStatus.PARTIAL)
        create_staging_manifest(manifest, programs_root=tmp_path)
        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)

        failures = validate_pinned_gather_run(
            "demo",
            gather_run_id=committed.run_id,
            gather_run_hash=committed.manifest_hash,
            programs_root=tmp_path,
        )

        assert len(failures) == 1
        assert "PARTIAL scope" in failures[0]

    def test_stale_pinned_run_is_rejected_when_newer_full_run_committed(self, tmp_path: Path) -> None:
        older = _make_manifest(run_id="gather-OLDER")
        create_staging_manifest(older, programs_root=tmp_path)
        older_committed = commit_staging_run(older, finished_at=_now(), programs_root=tmp_path)

        newer = _make_manifest(run_id="gather-NEWER")
        create_staging_manifest(newer, programs_root=tmp_path)
        commit_staging_run(newer, finished_at=_now() + timedelta(minutes=30), programs_root=tmp_path)

        failures = validate_pinned_gather_run(
            "demo",
            gather_run_id=older_committed.run_id,
            gather_run_hash=older_committed.manifest_hash,
            programs_root=tmp_path,
        )

        assert len(failures) == 1
        assert "stale" in failures[0]
        assert "gather-NEWER" in failures[0]

    def test_pinning_the_latest_full_run_itself_passes_even_with_a_prior_run(self, tmp_path: Path) -> None:
        older = _make_manifest(run_id="gather-OLDER")
        create_staging_manifest(older, programs_root=tmp_path)
        commit_staging_run(older, finished_at=_now(), programs_root=tmp_path)

        newer = _make_manifest(run_id="gather-NEWER")
        create_staging_manifest(newer, programs_root=tmp_path)
        newer_committed = commit_staging_run(newer, finished_at=_now() + timedelta(minutes=30), programs_root=tmp_path)

        failures = validate_pinned_gather_run(
            "demo",
            gather_run_id=newer_committed.run_id,
            gather_run_hash=newer_committed.manifest_hash,
            programs_root=tmp_path,
        )

        assert failures == ()


class TestFailStagingRun:
    def test_fail_moves_staging_to_failed_and_never_advances_pointers(self, tmp_path: Path) -> None:
        manifest = _make_manifest()
        create_staging_manifest(manifest, programs_root=tmp_path)

        failed = fail_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)

        assert failed.status is GatherRunStatus.FAILED
        assert not get_staging_run_dir("demo", manifest.run_id, programs_root=tmp_path).exists()
        assert get_failed_run_dir("demo", manifest.run_id, programs_root=tmp_path).exists()
        assert not get_latest_pointer_path("demo", programs_root=tmp_path).exists()


class TestQuarantineRecovery:
    def test_abandoned_running_manifest_with_no_lease_is_quarantined(self, tmp_path: Path) -> None:
        manifest = _make_manifest(lease_owner="ghost-host", lease_fencing_token=99)
        create_staging_manifest(manifest, programs_root=tmp_path)

        quarantined = quarantine_abandoned_staging_runs("demo", finished_at=_now(), programs_root=tmp_path)

        assert len(quarantined) == 1
        assert quarantined[0].status is GatherRunStatus.QUARANTINED
        assert not get_staging_run_dir("demo", manifest.run_id, programs_root=tmp_path).exists()
        quarantine_dir = get_quarantine_run_dir("demo", manifest.run_id, programs_root=tmp_path)
        assert quarantine_dir.exists()
        assert (quarantine_dir / "orphan_index.json").exists()

    def test_running_manifest_with_current_lease_is_left_untouched(self, tmp_path: Path) -> None:
        lease = acquire_lease("demo", "host-a", mutation_domain="gather", programs_root=tmp_path, ttl_seconds=300)
        manifest = _make_manifest(lease_owner=lease.owner, lease_fencing_token=lease.fencing_token)
        create_staging_manifest(manifest, programs_root=tmp_path)

        quarantined = quarantine_abandoned_staging_runs("demo", finished_at=_now(), programs_root=tmp_path)

        assert quarantined == []
        assert get_staging_run_dir("demo", manifest.run_id, programs_root=tmp_path).exists()

    def test_running_manifest_with_stale_fencing_token_is_quarantined(self, tmp_path: Path) -> None:
        lease = acquire_lease("demo", "host-a", mutation_domain="gather", programs_root=tmp_path, ttl_seconds=300)
        # A superseding acquisition bumps the fencing token; the old manifest's
        # recorded token (lease.fencing_token) is now stale.
        acquire_lease("demo", "host-a", mutation_domain="gather", programs_root=tmp_path, ttl_seconds=300)
        manifest = _make_manifest(lease_owner=lease.owner, lease_fencing_token=lease.fencing_token)
        create_staging_manifest(manifest, programs_root=tmp_path)

        quarantined = quarantine_abandoned_staging_runs("demo", finished_at=_now(), programs_root=tmp_path)

        assert len(quarantined) == 1

    def test_injected_is_lease_current_fn_overrides_default_check(self, tmp_path: Path) -> None:
        manifest = _make_manifest()
        create_staging_manifest(manifest, programs_root=tmp_path)

        quarantined = quarantine_abandoned_staging_runs(
            "demo", finished_at=_now(), programs_root=tmp_path, is_lease_current_fn=lambda m: True
        )

        assert quarantined == []
        assert get_staging_run_dir("demo", manifest.run_id, programs_root=tmp_path).exists()

    def test_committed_and_failed_runs_are_ignored_by_quarantine_scan(self, tmp_path: Path) -> None:
        committed_manifest = _make_manifest(run_id="gather-COMMITTED")
        create_staging_manifest(committed_manifest, programs_root=tmp_path)
        commit_staging_run(committed_manifest, finished_at=_now(), programs_root=tmp_path)

        abandoned_manifest = _make_manifest(run_id="gather-ABANDONED", lease_owner="ghost", lease_fencing_token=1)
        create_staging_manifest(abandoned_manifest, programs_root=tmp_path)

        quarantined = quarantine_abandoned_staging_runs("demo", finished_at=_now(), programs_root=tmp_path)

        assert [m.run_id for m in quarantined] == ["gather-ABANDONED"]

    def test_no_staging_dir_returns_empty_list(self, tmp_path: Path) -> None:
        assert quarantine_abandoned_staging_runs("demo", finished_at=_now(), programs_root=tmp_path) == []


class TestSidecarWriters:
    def test_write_query_results_sidecar_persists_sorted_entries(self, tmp_path: Path) -> None:
        manifest = _make_manifest()
        run_dir = create_staging_manifest(manifest, programs_root=tmp_path).parent
        entry_a = QueryResultEntry(
            query_id="q2", scope_id="s1", wiql_hash="h2", captured_at=_now(), raw_count=1,
            membership_ids=(), membership_hash="mh2", cap_reached=False, completeness_state="FULL",
        )
        entry_b = QueryResultEntry(
            query_id="q1", scope_id="s1", wiql_hash="h1", captured_at=_now(), raw_count=2,
            membership_ids=(), membership_hash="mh1", cap_reached=False, completeness_state="FULL",
        )
        path = write_query_results_sidecar(run_dir, (entry_a, entry_b))
        assert path.exists()
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert [row["query_id"] for row in payload] == ["q1", "q2"]


class TestLegacyCutoffManifest:
    """§4.17 step 5: the synthetic bootstrap manifest that bounds how far
    back an unstamped (pre-run-lifecycle) signal/fact may be grandfathered
    once a program activates run-aware reads."""

    def test_create_legacy_cutoff_manifest_is_committed_and_synthetic(self, tmp_path: Path) -> None:
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        manifest = create_legacy_cutoff_manifest("demo", legacy_cutoff_at=cutoff, programs_root=tmp_path)

        assert manifest.run_id.startswith(LEGACY_CUTOFF_RUN_ID_PREFIX)
        assert manifest.status is GatherRunStatus.COMMITTED
        assert manifest.actor_identity_type == ACTOR_IDENTITY_SYNTHETIC
        assert manifest.legacy_cutoff_at == cutoff
        assert manifest.manifest_hash is not None
        assert compute_manifest_hash(manifest) == manifest.manifest_hash

        committed_dir = get_committed_run_dir("demo", manifest.run_id, programs_root=tmp_path)
        assert read_manifest(committed_dir).run_id == manifest.run_id

    def test_create_legacy_cutoff_manifest_does_not_touch_latest_pointers(self, tmp_path: Path) -> None:
        create_legacy_cutoff_manifest("demo", legacy_cutoff_at=datetime(2026, 1, 1, tzinfo=timezone.utc), programs_root=tmp_path)

        assert not get_latest_pointer_path("demo", programs_root=tmp_path).exists()
        assert not get_latest_full_pointer_path("demo", programs_root=tmp_path).exists()

    def test_create_legacy_cutoff_manifest_is_idempotent(self, tmp_path: Path) -> None:
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        first = create_legacy_cutoff_manifest("demo", legacy_cutoff_at=cutoff, programs_root=tmp_path)
        second = create_legacy_cutoff_manifest("demo", legacy_cutoff_at=cutoff, programs_root=tmp_path)

        assert first.run_id == second.run_id
        committed_root = get_committed_root("demo", programs_root=tmp_path)
        legacy_dirs = [p for p in committed_root.iterdir() if p.name.startswith(LEGACY_CUTOFF_RUN_ID_PREFIX)]
        assert len(legacy_dirs) == 1

    def test_get_legacy_cutoff_at_returns_none_when_no_manifest_exists(self, tmp_path: Path) -> None:
        assert get_legacy_cutoff_at("demo", programs_root=tmp_path) is None

    def test_get_legacy_cutoff_at_returns_earliest_cutoff_when_multiple_exist(self, tmp_path: Path) -> None:
        earlier = datetime(2025, 1, 1, tzinfo=timezone.utc)
        later = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # Force two distinct synthetic manifests to coexist (idempotency
        # normally prevents this; this simulates a hypothetical drift and
        # asserts the most conservative — earliest — cutoff wins).
        manifest_a = GatherRunManifest(
            run_id=f"{LEGACY_CUTOFF_RUN_ID_PREFIX}A",
            status=GatherRunStatus.COMMITTED,
            program_id="demo",
            actor_identity_type=ACTOR_IDENTITY_SYNTHETIC,
            lease_owner="legacy-cutoff-bootstrap",
            lease_fencing_token=0,
            started_at=later,
            scope_as_of=later,
            required_scope_status=RequiredScopeStatus.FULL,
            legacy_cutoff_at=later,
        )
        manifest_b = GatherRunManifest(
            run_id=f"{LEGACY_CUTOFF_RUN_ID_PREFIX}B",
            status=GatherRunStatus.COMMITTED,
            program_id="demo",
            actor_identity_type=ACTOR_IDENTITY_SYNTHETIC,
            lease_owner="legacy-cutoff-bootstrap",
            lease_fencing_token=0,
            started_at=earlier,
            scope_as_of=earlier,
            required_scope_status=RequiredScopeStatus.FULL,
            legacy_cutoff_at=earlier,
        )
        from src.core.gather_run_manifest import write_manifest

        for manifest in (manifest_a, manifest_b):
            hashed = gather_run_manifest._with_manifest_hash(manifest, compute_manifest_hash(manifest))
            write_manifest(get_committed_run_dir("demo", manifest.run_id, programs_root=tmp_path), hashed)

        assert get_legacy_cutoff_at("demo", programs_root=tmp_path) == earlier

    def test_get_verified_committed_run_ids_includes_legacy_cutoff_manifest(self, tmp_path: Path) -> None:
        manifest = create_legacy_cutoff_manifest("demo", legacy_cutoff_at=datetime(2026, 1, 1, tzinfo=timezone.utc), programs_root=tmp_path)

        assert manifest.run_id in get_verified_committed_run_ids("demo", programs_root=tmp_path)
