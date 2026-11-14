from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import sys

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
    get_verified_committed_run_state,
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


def _write_nested_manifest(path: Path, phase: str) -> bytes:
    import json

    if phase == "decode":
        text = "[" * 10000 + "0" + "]" * 10000
        with pytest.raises(RecursionError):
            json.loads(text)
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["actor_identity_type"] = "__nested_actor__"
        limit = sys.getrecursionlimit()
        depth = limit + 100 if sys.version_info >= (3, 12) else limit * 3 // 4
        text = json.dumps(payload).replace(
            '"__nested_actor__"', "[" * depth + "0" + "]" * depth,
        )
        assert isinstance(json.loads(text), dict)
    path.write_text(text, encoding="utf-8")
    if phase == "hash":
        with pytest.raises(RecursionError):
            compute_manifest_hash(read_manifest(path.parent))
    return path.read_bytes()


@pytest.mark.parametrize("backend", ["file", "sqlite"])
@pytest.mark.parametrize("cutoff_state", ["absent", "valid", "invalid"])
@pytest.mark.parametrize("read_only", [False, True])
def test_signal_store_committed_filter_matches_manifest_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    cutoff_state: str,
    read_only: bool,
) -> None:
    from src.core.file_stores import FileSignalStore
    from src.core.models import Confidence
    from src.core.models_v2 import Signal, SignalThreadLink
    from src.core.sqlite_stores import SQLiteSignalStore, get_program_sqlite_store_path

    programs_root = tmp_path / "programs"
    cutoff = _now() - timedelta(days=1)
    if cutoff_state != "absent":
        legacy = create_legacy_cutoff_manifest("demo", legacy_cutoff_at=cutoff, programs_root=programs_root)
        if cutoff_state == "invalid":
            gather_run_manifest.write_manifest(
                get_committed_run_dir("demo", legacy.run_id, programs_root=programs_root),
                replace(legacy, manifest_hash="tampered"),
            )

    full = _make_manifest(run_id="gather-full")
    partial = _make_manifest(run_id="gather-partial", required_scope_status=RequiredScopeStatus.PARTIAL)
    tampered = _make_manifest(run_id="gather-tampered")
    for manifest in (full, partial, tampered):
        create_staging_manifest(manifest, programs_root=programs_root)
        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=programs_root)
        if manifest is tampered:
            gather_run_manifest.write_manifest(
                get_committed_run_dir("demo", manifest.run_id, programs_root=programs_root),
                replace(committed, manifest_hash="tampered"),
            )
    running = _make_manifest(run_id="gather-running")
    failed = _make_manifest(run_id="gather-failed")
    create_staging_manifest(running, programs_root=programs_root)
    create_staging_manifest(failed, programs_root=programs_root)
    fail_staging_run(failed, finished_at=_now(), programs_root=programs_root)

    store = FileSignalStore(programs_root=programs_root) if backend == "file" else SQLiteSignalStore(programs_root=programs_root)
    entries = (
        ("legacy-before", None, cutoff - timedelta(seconds=1)),
        ("legacy-at", None, cutoff),
        ("legacy-after", None, cutoff + timedelta(seconds=1)),
        ("full", full.run_id, _now()),
        ("partial", partial.run_id, _now()),
        ("running", running.run_id, _now()),
        ("failed", failed.run_id, _now()),
        ("tampered", tampered.run_id, _now()),
    )
    for label, run_id, timestamp in entries:
        store.append(
            Signal(
                label, timestamp, "manual", "demo", "ws", (f"WI:{label}",),
                label, None, Confidence.HIGH, gather_run_id=run_id,
                workstream_ids=("ws", "shared") if label == "full" else ("ws",),
            )
        )
    store.append_thread("demo", SignalThreadLink("full", "thread-1", _now(), "reviewer"))
    database_path = get_program_sqlite_store_path("demo", programs_root=programs_root)
    before_read = database_path.read_bytes() if backend == "sqlite" and read_only else None
    scan = gather_run_manifest.get_verified_committed_run_state
    scans: list[str] = []

    def count_scan(program_id: str, *, programs_root: Path):
        scans.append(program_id)
        return scan(program_id, programs_root=programs_root)

    monkeypatch.setattr(gather_run_manifest, "get_verified_committed_run_state", count_scan)

    assert {signal.id for signal in store.read("demo", read_only=read_only)} == {
        entry[0] for entry in entries
    }
    assert scans == []
    visible = store.read("demo", read_only=read_only, require_committed_gather_run=True)
    assert scans == ["demo"]
    expected = {"full", "partial", "legacy-before", "legacy-at"}
    if cutoff_state != "valid":
        expected.add("legacy-after")
    assert {signal.id for signal in visible} == expected
    selected = store.read(
        "demo", start=_now(), end=_now(), workstream_id="shared",
        read_only=read_only, require_committed_gather_run=True,
    )
    assert [(signal.id, signal.thread_id) for signal in selected] == [("full", "thread-1")]
    assert scans == ["demo", "demo"]
    assert {signal.id for signal in store.read(
        "demo", read_only=read_only, require_committed_gather_run=False,
    )} == {entry[0] for entry in entries}
    assert scans == ["demo", "demo"]
    if before_read is not None:
        assert database_path.read_bytes() == before_read


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

    @pytest.mark.parametrize(("field", "value"), [
        ("program_id", "foreign"),
        ("run_id", "gather-foreign"),
        ("schema_version", "gather-run.unsupported"),
    ])
    @pytest.mark.parametrize("surface", ["state", "signals", "facts"])
    def test_committed_scan_binds_identity_for_signal_and_fact_readers(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, field: str, value: str, surface: str,
    ) -> None:
        from src.core.journal import append_signal, read_signals
        from src.core.models import Confidence
        from src.core.models_v2 import Signal
        from src.core.program_fact_store import ProgramFactInput, ProgramFactStore, load_program_facts

        programs_root = tmp_path / "programs"
        cutoff = _now() - timedelta(days=2)
        legacy = create_legacy_cutoff_manifest("demo", legacy_cutoff_at=cutoff, programs_root=programs_root)
        full = _make_manifest(run_id="gather-full")
        partial = _make_manifest(run_id="gather-partial", required_scope_status=RequiredScopeStatus.PARTIAL)
        for manifest in (full, partial):
            create_staging_manifest(manifest, programs_root=programs_root)
            commit_staging_run(manifest, finished_at=_now(), programs_root=programs_root)

        invalid_dir = get_committed_run_dir("demo", "gather-invalid", programs_root=programs_root)
        invalid = replace(
            _make_manifest(run_id=invalid_dir.name),
            status=GatherRunStatus.COMMITTED,
            legacy_cutoff_at=cutoff - timedelta(days=2),
            **{field: value},
        )
        invalid = replace(invalid, manifest_hash=compute_manifest_hash(invalid))
        gather_run_manifest.write_manifest(invalid_dir, invalid)

        store = ProgramFactStore("demo", db_root=tmp_path)
        for label, run_id, timestamp in (
            ("full", full.run_id, _now()),
            ("partial", partial.run_id, _now()),
            ("invalid", invalid.run_id, _now()),
            ("legacy-before", None, cutoff - timedelta(days=1)),
            ("legacy-after", None, _now()),
        ):
            append_signal(
                Signal(
                    label, timestamp, "ado", "demo", "ws", (f"WI:{label}",),
                    label, None, Confidence.HIGH, gather_run_id=run_id,
                ),
                programs_root=programs_root,
                partition_at=timestamp,
            )
            store.append_fact(
                ProgramFactInput(
                    fact_type="action.item", entity_refs=(f"WI:{label}",),
                    payload={"title": label}, gather_run_id=run_id,
                ),
                recorded_at=timestamp,
            )

        with caplog.at_level("WARNING", logger="src.core.gather_run_manifest"):
            if surface == "state":
                run_ids, selected_cutoff = get_verified_committed_run_state("demo", programs_root=programs_root)
                assert run_ids == frozenset({legacy.run_id, full.run_id, partial.run_id})
                assert selected_cutoff == cutoff
            elif surface == "signals":
                assert {signal.id for signal in read_signals("demo", programs_root=programs_root)} == {
                    "full", "partial", "invalid", "legacy-before", "legacy-after",
                }
                assert {
                    signal.id for signal in read_signals(
                        "demo", programs_root=programs_root, require_committed_gather_run=True,
                    )
                } == {"full", "partial", "legacy-before"}
            else:
                facts = load_program_facts(
                    "demo", db_root=tmp_path, programs_root=programs_root,
                    as_of=_now() + timedelta(days=1), require_committed_gather_run=True,
                )
                assert {fact.payload["title"] for fact in facts.facts} == {"full", "partial", "legacy-before"}
        assert "invalid committed gather manifest" in caplog.text.lower()
        assert "gather-invalid" not in caplog.text
        assert "foreign" not in caplog.text

    def test_committed_scan_rejects_malformed_hash_inputs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        import json

        valid = _make_manifest(run_id="gather-valid")
        create_staging_manifest(valid, programs_root=tmp_path)
        commit_staging_run(valid, finished_at=_now(), programs_root=tmp_path)
        invalid_dir = get_committed_run_dir("demo", "gather-invalid", programs_root=tmp_path)
        invalid_dir.mkdir(parents=True)
        valid_path = get_committed_run_dir("demo", valid.run_id, programs_root=tmp_path) / "manifest.json"
        payload = json.loads(valid_path.read_text(encoding="utf-8"))
        payload["run_id"] = invalid_dir.name
        payload["alert_ids"] = ["valid", 1]
        (invalid_dir / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

        with caplog.at_level("WARNING", logger="src.core.gather_run_manifest"):
            assert get_verified_committed_run_ids("demo", programs_root=tmp_path) == frozenset({valid.run_id})
        assert "invalid committed gather manifest" in caplog.text.lower()
        assert "gather-invalid" not in caplog.text

    @pytest.mark.parametrize("phase", ["decode", "hash"])
    def test_committed_scan_rejects_excessively_nested_manifest(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, phase: str,
    ) -> None:
        valid = _make_manifest(run_id="gather-valid")
        invalid = _make_manifest(run_id="gather-invalid")
        for manifest in (valid, invalid):
            create_staging_manifest(manifest, programs_root=tmp_path)
            commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)
        path = get_committed_run_dir("demo", invalid.run_id, programs_root=tmp_path) / "manifest.json"
        before = _write_nested_manifest(path, phase)

        with caplog.at_level("WARNING", logger="src.core.gather_run_manifest"):
            assert get_verified_committed_run_ids("demo", programs_root=tmp_path) == frozenset({valid.run_id})
        assert "invalid committed gather manifest" in caplog.text.lower()
        assert "gather-invalid" not in caplog.text
        assert path.read_bytes() == before

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

    @pytest.mark.parametrize("full_only", [False, True])
    @pytest.mark.parametrize("selector", ["forward", "backward", "absolute"])
    def test_path_shaped_pointer_recovers_without_selecting_staged_manifest(
        self, tmp_path: Path, full_only: bool, selector: str,
    ) -> None:
        manifest = _make_manifest(run_id="gather-valid")
        create_staging_manifest(manifest, programs_root=tmp_path)
        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)

        staged_dir = get_staging_run_dir("demo", "gather-outside", programs_root=tmp_path)
        staged = replace(
            _make_manifest(run_id=staged_dir.name),
            status=GatherRunStatus.COMMITTED,
            finished_at=_now() + timedelta(minutes=5),
        )
        staged = replace(staged, manifest_hash=compute_manifest_hash(staged))
        staged_path = gather_run_manifest.write_manifest(staged_dir, staged)
        run_id = {
            "forward": "../staging/gather-outside",
            "backward": r"..\staging\gather-outside",
            "absolute": str(staged_dir),
        }[selector]
        pointer = (
            get_latest_full_pointer_path("demo", programs_root=tmp_path)
            if full_only else get_latest_pointer_path("demo", programs_root=tmp_path)
        )
        gather_run_manifest._write_pointer(pointer, run_id=run_id, manifest_hash=staged.manifest_hash)
        pointer_before, staged_before = pointer.read_bytes(), staged_path.read_bytes()

        resolver = resolve_latest_full_committed_manifest if full_only else resolve_latest_committed_manifest
        assert resolver("demo", programs_root=tmp_path) == committed
        assert pointer.read_bytes() == pointer_before
        assert staged_path.read_bytes() == staged_before

    @pytest.mark.parametrize("run_id", [None, 42, False, [], {}])
    def test_nonstring_pointer_run_id_is_invalid(self, tmp_path: Path, run_id: object) -> None:
        import json

        pointer = tmp_path / "latest.json"
        pointer.write_text(json.dumps({"run_id": run_id, "manifest_hash": "hash"}), encoding="utf-8")
        assert gather_run_manifest._read_pointer(pointer) is None

    @pytest.mark.parametrize("full_only", [False, True])
    def test_resolve_latest_recovers_from_excessively_nested_pointer(
        self, tmp_path: Path, full_only: bool,
    ) -> None:
        manifest = _make_manifest()
        create_staging_manifest(manifest, programs_root=tmp_path)
        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)
        pointer = (
            get_latest_full_pointer_path("demo", programs_root=tmp_path)
            if full_only else get_latest_pointer_path("demo", programs_root=tmp_path)
        )
        text = "[" * 10000 + "0" + "]" * 10000
        pointer.write_text(text, encoding="utf-8")
        before = pointer.read_bytes()

        resolver = resolve_latest_full_committed_manifest if full_only else resolve_latest_committed_manifest
        assert resolver("demo", programs_root=tmp_path) == committed
        assert pointer.read_bytes() == before

    @pytest.mark.parametrize("phase", ["decode", "hash"])
    def test_nested_selected_manifest_keeps_existing_resolution_policies(
        self, tmp_path: Path, phase: str,
    ) -> None:
        full = _make_manifest(run_id="gather-full")
        newer = _make_manifest(run_id="gather-newer")
        for manifest, finished_at in ((full, _now()), (newer, _now() + timedelta(minutes=1))):
            create_staging_manifest(manifest, programs_root=tmp_path)
            commit_staging_run(manifest, finished_at=finished_at, programs_root=tmp_path)
        path = get_committed_run_dir("demo", newer.run_id, programs_root=tmp_path) / "manifest.json"
        before = _write_nested_manifest(path, phase)

        with pytest.raises(ValueError, match="Invalid committed gather evidence"):
            resolve_latest_committed_manifest("demo", programs_root=tmp_path)
        resolved_full = resolve_latest_full_committed_manifest("demo", programs_root=tmp_path)
        assert resolved_full is not None
        assert resolved_full.run_id == full.run_id
        assert path.read_bytes() == before

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

    @pytest.mark.parametrize("invalid", ["failed", "foreign", "unsupported"])
    def test_resolve_latest_full_committed_manifest_rejects_invalid_pointer_candidate(
        self, tmp_path: Path, invalid: str,
    ) -> None:
        manifest = _make_manifest(run_id="gather-INVALID")
        create_staging_manifest(manifest, programs_root=tmp_path)
        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)
        manifest_path = get_committed_run_dir(
            "demo", committed.run_id, programs_root=tmp_path,
        ) / "manifest.json"
        values = {
            "failed": {"status": GatherRunStatus.FAILED},
            "foreign": {"program_id": "other"},
            "unsupported": {"schema_version": "gather-run.unsupported"},
        }[invalid]
        candidate = replace(committed, **values)
        candidate = replace(candidate, manifest_hash=compute_manifest_hash(candidate))
        gather_run_manifest.write_manifest(manifest_path.parent, candidate)
        gather_run_manifest._write_pointer(
            get_latest_full_pointer_path("demo", programs_root=tmp_path),
            run_id=committed.run_id,
            manifest_hash=candidate.manifest_hash,
        )

        with pytest.raises(ValueError, match="program/run/status/schema mismatch"):
            resolve_latest_full_committed_manifest("demo", programs_root=tmp_path)

    @pytest.mark.parametrize("damage", ["missing_run", "missing_manifest", "corrupt", "invalid_hash"])
    def test_resolve_latest_full_recovers_when_pointed_manifest_is_unreadable(
        self, tmp_path: Path, damage: str,
    ) -> None:
        full = _make_manifest(run_id="gather-FULL")
        create_staging_manifest(full, programs_root=tmp_path)
        full_committed = commit_staging_run(full, finished_at=_now(), programs_root=tmp_path)

        newer = _make_manifest(run_id="gather-NEWER")
        create_staging_manifest(newer, programs_root=tmp_path)
        newer_committed = commit_staging_run(
            newer, finished_at=_now() + timedelta(minutes=5), programs_root=tmp_path,
        )
        newer_dir = get_committed_run_dir("demo", newer_committed.run_id, programs_root=tmp_path)
        manifest_path = newer_dir / "manifest.json"
        if damage == "missing_run":
            shutil.rmtree(newer_dir)
        elif damage == "missing_manifest":
            manifest_path.unlink()
        elif damage == "invalid_hash":
            invalid = replace(newer_committed, manifest_hash="sha256:invalid")
            gather_run_manifest.write_manifest(newer_dir, invalid)
            gather_run_manifest._write_pointer(
                get_latest_full_pointer_path("demo", programs_root=tmp_path),
                run_id=invalid.run_id,
                manifest_hash=invalid.manifest_hash,
            )
        else:
            manifest_path.write_text("{not valid json", encoding="utf-8")

        with pytest.raises(ValueError):
            gather_run_manifest._read_committed_candidate("demo", newer_dir, pointer_selected=True)

        resolved = resolve_latest_full_committed_manifest("demo", programs_root=tmp_path)

        assert resolved is not None
        assert resolved.run_id == full_committed.run_id

    def test_resolve_latest_full_scan_excludes_hash_valid_failed_manifest(self, tmp_path: Path) -> None:
        full = _make_manifest(run_id="gather-FULL")
        create_staging_manifest(full, programs_root=tmp_path)
        full_committed = commit_staging_run(full, finished_at=_now(), programs_root=tmp_path)

        newer = _make_manifest(run_id="gather-FAILED")
        create_staging_manifest(newer, programs_root=tmp_path)
        failed = commit_staging_run(
            newer, finished_at=_now() + timedelta(minutes=5), programs_root=tmp_path,
        )
        failed = replace(failed, status=GatherRunStatus.FAILED, manifest_hash=None)
        failed = replace(failed, manifest_hash=compute_manifest_hash(failed))
        gather_run_manifest.write_manifest(
            get_committed_run_dir("demo", failed.run_id, programs_root=tmp_path), failed,
        )
        get_latest_full_pointer_path("demo", programs_root=tmp_path).write_text(
            "{not valid json", encoding="utf-8",
        )

        resolved = resolve_latest_full_committed_manifest("demo", programs_root=tmp_path)

        assert resolved is not None
        assert resolved.run_id == full_committed.run_id

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

    @pytest.mark.parametrize("selector", ["forward", "backward", "absolute"])
    @pytest.mark.parametrize("include_hash", [False, True])
    def test_path_shaped_pinned_run_cannot_admit_staged_evidence(
        self, tmp_path: Path, selector: str, include_hash: bool,
    ) -> None:
        get_committed_root("demo", programs_root=tmp_path).mkdir(parents=True)
        staged_dir = get_staging_run_dir("demo", "gather-outside", programs_root=tmp_path)
        run_id = {
            "forward": "../staging/gather-outside",
            "backward": r"..\staging\gather-outside",
            "absolute": str(staged_dir),
        }[selector]
        staged = replace(
            _make_manifest(run_id=run_id), status=GatherRunStatus.COMMITTED, finished_at=_now(),
        )
        staged = replace(staged, manifest_hash=compute_manifest_hash(staged))
        staged_path = gather_run_manifest.write_manifest(staged_dir, staged)
        before = staged_path.read_bytes()

        failures = validate_pinned_gather_run(
            "demo", gather_run_id=run_id,
            gather_run_hash=staged.manifest_hash if include_hash else None,
            programs_root=tmp_path,
        )
        assert len(failures) == 1
        assert failures[0].startswith("BLOCKED:")
        assert "invalid identifier" in failures[0]
        assert staged_path.read_bytes() == before

    @pytest.mark.parametrize("run_id", ["", ".", "..", "C:run", "gather/*", "gather[1]", 0, False, [], {}])
    def test_invalid_pinned_selector_is_blocked_before_path_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_id: object,
    ) -> None:
        def no_path_lookup(*args, **kwargs):
            pytest.fail("Invalid pinned identifiers must not be used as filesystem paths")

        monkeypatch.setattr(gather_run_manifest, "get_committed_run_dir", no_path_lookup)
        failures = validate_pinned_gather_run(
            "demo", gather_run_id=run_id, gather_run_hash=None, programs_root=tmp_path,
        )
        assert len(failures) == 1
        assert failures[0].startswith("BLOCKED:")
        assert "invalid identifier" in failures[0]

    @pytest.mark.parametrize(("field", "value"), [
        ("program_id", "other"),
        ("run_id", "gather-other"),
        ("status", GatherRunStatus.RUNNING),
        ("status", GatherRunStatus.FAILED),
        ("status", GatherRunStatus.QUARANTINED),
        ("schema_version", "gather-run.unsupported"),
    ])
    @pytest.mark.parametrize("include_hash", [False, True])
    def test_hash_valid_foreign_manifest_is_blocked_before_latest_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        field: str, value: str, include_hash: bool,
    ) -> None:
        from dataclasses import replace

        manifest = _make_manifest()
        create_staging_manifest(manifest, programs_root=tmp_path)
        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)
        invalid = replace(committed, **{field: value})
        invalid = replace(invalid, manifest_hash=compute_manifest_hash(invalid))
        gather_run_manifest.write_manifest(
            get_committed_run_dir("demo", committed.run_id, programs_root=tmp_path), invalid,
        )

        def no_latest(*args, **kwargs):
            pytest.fail("Invalid pinned evidence must not consult latest evidence")

        monkeypatch.setattr(gather_run_manifest, "resolve_latest_full_committed_manifest", no_latest)
        failures = validate_pinned_gather_run(
            "demo", gather_run_id=committed.run_id,
            gather_run_hash=invalid.manifest_hash if include_hash else None,
            programs_root=tmp_path,
        )
        assert len(failures) == 1
        assert failures[0].startswith("BLOCKED:")
        assert "program/run/status/schema mismatch" in failures[0]

    def test_valid_pin_without_optional_hash_passes(self, tmp_path: Path) -> None:
        manifest = _make_manifest()
        create_staging_manifest(manifest, programs_root=tmp_path)
        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)
        assert validate_pinned_gather_run(
            "demo", gather_run_id=committed.run_id, gather_run_hash=None, programs_root=tmp_path,
        ) == ()

    @pytest.mark.parametrize("schema", [True, False, 1, 1.0, None, [], {}, "1"])
    def test_wrong_type_or_numeric_schema_is_rejected(self, tmp_path: Path, schema: object) -> None:
        from dataclasses import replace

        manifest = _make_manifest()
        create_staging_manifest(manifest, programs_root=tmp_path)
        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)
        invalid = replace(committed, schema_version=schema)
        invalid = replace(invalid, manifest_hash=compute_manifest_hash(invalid))
        gather_run_manifest.write_manifest(
            get_committed_run_dir("demo", committed.run_id, programs_root=tmp_path), invalid,
        )
        failures = validate_pinned_gather_run(
            "demo", gather_run_id=committed.run_id, gather_run_hash=invalid.manifest_hash,
            programs_root=tmp_path,
        )
        assert failures
        assert "program/run/status/schema mismatch" in failures[0]

    @pytest.mark.parametrize("field", ["program_id", "run_id", "status", "schema_version"])
    def test_missing_identity_field_is_rejected(self, tmp_path: Path, field: str) -> None:
        import json

        manifest = _make_manifest()
        create_staging_manifest(manifest, programs_root=tmp_path)
        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)
        path = get_committed_run_dir("demo", committed.run_id, programs_root=tmp_path) / "manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload[field]
        path.write_text(json.dumps(payload), encoding="utf-8")
        failures = validate_pinned_gather_run(
            "demo", gather_run_id=committed.run_id, gather_run_hash=committed.manifest_hash,
            programs_root=tmp_path,
        )
        assert failures
        assert failures[0].startswith("BLOCKED:")

    @pytest.mark.parametrize(("field", "value", "diagnostic"), [
        (None, None, "invalid: no readable"),
        (None, [], "invalid: no readable"),
        ("query_results", None, "invalid: no readable"),
        ("query_results", [None], "invalid: no readable"),
        ("query_results", [
            {
                "query_id": "query", "scope_id": "scope", "wiql_hash": "hash",
                "captured_at": "2026-01-15T12:00:00+00:00", "raw_count": 0,
                "membership_ids": None, "membership_hash": "hash", "cap_reached": False,
                "completeness_state": "FULL",
            },
        ], "invalid: no readable"),
        ("query_results", [
            {
                "query_id": query_id, "scope_id": "scope", "wiql_hash": "hash",
                "captured_at": "2026-01-15T12:00:00+00:00", "raw_count": 0,
                "membership_ids": [], "membership_hash": "hash", "cap_reached": False,
                "completeness_state": "FULL",
            }
            for query_id in ("query", 1)
        ], "hash verification"),
        ("channel_outcomes", None, "invalid: no readable"),
        ("channel_outcomes", [None], "invalid: no readable"),
        ("failed_refs", None, "invalid: no readable"),
        ("failed_refs", [None], "invalid: no readable"),
        ("started_at", 1, "invalid: no readable"),
        ("finished_at", [], "invalid: no readable"),
        ("alert_ids", None, "invalid: no readable"),
        ("alert_ids", ["alert", 1], "hash verification"),
        ("manifest_hash", None, "hash verification"),
        ("channel_outcomes", [
            {"channel": "ado", "degraded": False, "elapsed_seconds": 0},
            {"channel": 1, "degraded": False, "elapsed_seconds": 0},
        ], "hash verification"),
        ("failed_refs", [
            {"ref_kind": "work_item", "ref_id": "1", "reason": "error"},
            {"ref_kind": "work_item", "ref_id": 2, "reason": "error"},
        ], "hash verification"),
    ])
    @pytest.mark.parametrize("include_hash", [False, True])
    def test_malformed_pinned_manifest_is_blocked_without_latest_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        field: str | None, value: object, diagnostic: str, include_hash: bool,
    ) -> None:
        import json

        manifest = _make_manifest()
        create_staging_manifest(manifest, programs_root=tmp_path)
        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)
        path = get_committed_run_dir("demo", committed.run_id, programs_root=tmp_path) / "manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if field is None:
            payload = value
        else:
            payload[field] = value
        path.write_text(json.dumps(payload), encoding="utf-8")

        def no_latest(*args, **kwargs):
            pytest.fail("Malformed pinned evidence must not consult latest evidence")

        monkeypatch.setattr(gather_run_manifest, "resolve_latest_full_committed_manifest", no_latest)
        failures = validate_pinned_gather_run(
            "demo", gather_run_id=committed.run_id,
            gather_run_hash=committed.manifest_hash if include_hash else None,
            programs_root=tmp_path,
        )
        assert len(failures) == 1
        assert failures[0].startswith("BLOCKED:")
        assert diagnostic in failures[0]

    def test_manifest_hash_value_error_is_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manifest = _make_manifest()
        create_staging_manifest(manifest, programs_root=tmp_path)
        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)

        def fail_hash(*args, **kwargs):
            raise ValueError("invalid hash input")

        monkeypatch.setattr(gather_run_manifest, "compute_manifest_hash", fail_hash)
        failures = validate_pinned_gather_run(
            "demo", gather_run_id=committed.run_id,
            gather_run_hash=committed.manifest_hash, programs_root=tmp_path,
        )
        assert len(failures) == 1
        assert failures[0].startswith("BLOCKED:")
        assert "hash verification" in failures[0]

    @pytest.mark.parametrize(("phase", "diagnostic"), [
        ("decode", "invalid: no readable"),
        ("hash", "hash verification"),
    ])
    @pytest.mark.parametrize("include_hash", [False, True])
    def test_excessively_nested_pin_is_blocked_without_latest_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        phase: str, diagnostic: str, include_hash: bool,
    ) -> None:
        manifest = _make_manifest()
        create_staging_manifest(manifest, programs_root=tmp_path)
        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)
        path = get_committed_run_dir("demo", committed.run_id, programs_root=tmp_path) / "manifest.json"
        before = _write_nested_manifest(path, phase)

        def no_latest(*args, **kwargs):
            pytest.fail("Invalid pinned evidence must not consult latest evidence")

        monkeypatch.setattr(gather_run_manifest, "resolve_latest_full_committed_manifest", no_latest)
        failures = validate_pinned_gather_run(
            "demo", gather_run_id=committed.run_id,
            gather_run_hash=committed.manifest_hash if include_hash else None,
            programs_root=tmp_path,
        )
        assert len(failures) == 1
        assert failures[0].startswith("BLOCKED:")
        assert diagnostic in failures[0]
        assert path.read_bytes() == before

    def test_unexpected_manifest_hash_error_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        manifest = _make_manifest()
        create_staging_manifest(manifest, programs_root=tmp_path)
        committed = commit_staging_run(manifest, finished_at=_now(), programs_root=tmp_path)

        def fail_hash(*args, **kwargs):
            raise RuntimeError("unexpected hashing error")

        monkeypatch.setattr(gather_run_manifest, "compute_manifest_hash", fail_hash)
        with pytest.raises(RuntimeError, match="unexpected hashing error"):
            validate_pinned_gather_run(
                "demo", gather_run_id=committed.run_id,
                gather_run_hash=committed.manifest_hash, programs_root=tmp_path,
            )

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

    @pytest.mark.parametrize("invalid_kind", ["hash", "program", "run", "schema", "status", "actor"])
    def test_cutoff_bootstrap_reverifies_matching_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_kind: str,
    ) -> None:
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        valid = create_legacy_cutoff_manifest("demo", legacy_cutoff_at=cutoff, programs_root=tmp_path)
        root = get_committed_root("demo", programs_root=tmp_path)
        valid_dir = get_committed_run_dir("demo", valid.run_id, programs_root=tmp_path)
        invalid_dir = root / f"{LEGACY_CUTOFF_RUN_ID_PREFIX}invalid"
        invalid = replace(valid, run_id=invalid_dir.name, manifest_hash=None)
        if invalid_kind == "program":
            invalid = replace(invalid, program_id="foreign")
        elif invalid_kind == "run":
            invalid = replace(invalid, run_id=f"{LEGACY_CUTOFF_RUN_ID_PREFIX}foreign")
        elif invalid_kind == "schema":
            invalid = replace(invalid, schema_version="gather-run.unsupported")
        elif invalid_kind == "status":
            invalid = replace(invalid, status=GatherRunStatus.FAILED)
        elif invalid_kind == "actor":
            invalid = replace(invalid, actor_identity_type="interactive")
        invalid = replace(
            invalid,
            manifest_hash="tampered" if invalid_kind == "hash" else compute_manifest_hash(invalid),
        )
        gather_run_manifest.write_manifest(invalid_dir, invalid)
        original_iterdir = Path.iterdir

        def ordered_iterdir(path: Path):
            return iter((invalid_dir, valid_dir)) if path == root else original_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", ordered_iterdir)
        selected = create_legacy_cutoff_manifest(
            "demo", legacy_cutoff_at=cutoff + timedelta(days=1), programs_root=tmp_path,
        )
        assert selected == valid

    @pytest.mark.parametrize("disappearance", ["empty", "removed"])
    def test_cutoff_bootstrap_fails_if_verified_manifest_disappears(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disappearance: str,
    ) -> None:
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        valid = create_legacy_cutoff_manifest("demo", legacy_cutoff_at=cutoff, programs_root=tmp_path)
        root = get_committed_root("demo", programs_root=tmp_path)
        valid_dir = get_committed_run_dir("demo", valid.run_id, programs_root=tmp_path)
        original_iterdir = Path.iterdir
        scans = 0

        def disappearing_iterdir(path: Path):
            nonlocal scans
            if path != root:
                return original_iterdir(path)
            scans += 1
            if scans == 1:
                return iter((valid_dir,))
            if disappearance == "removed":
                raise FileNotFoundError("committed directory disappeared")
            return iter(())

        monkeypatch.setattr(Path, "iterdir", disappearing_iterdir)
        with pytest.raises(ValueError, match="verified legacy cutoff"):
            create_legacy_cutoff_manifest(
                "demo", legacy_cutoff_at=cutoff + timedelta(days=1), programs_root=tmp_path,
            )
        assert tuple(original_iterdir(root)) == (valid_dir,)

    def test_cutoff_bootstrap_fails_if_verified_cutoff_has_no_synthetic_manifest(self, tmp_path: Path) -> None:
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        run = replace(_make_manifest(), legacy_cutoff_at=cutoff)
        create_staging_manifest(run, programs_root=tmp_path)
        commit_staging_run(run, finished_at=_now(), programs_root=tmp_path)

        with pytest.raises(ValueError, match="verified legacy cutoff"):
            create_legacy_cutoff_manifest(
                "demo", legacy_cutoff_at=cutoff + timedelta(days=1), programs_root=tmp_path,
            )
        assert {path.name for path in get_committed_root("demo", programs_root=tmp_path).iterdir()} == {run.run_id}

    def test_invalid_cutoff_preserves_absent_cutoff_compatibility(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        from src.core.journal import append_signal, read_signals
        from src.core.models import Confidence
        from src.core.models_v2 import Signal
        from src.core.program_fact_store import ProgramFactInput, ProgramFactStore, load_program_facts

        programs_root = tmp_path / "programs"
        cutoff = _now() - timedelta(days=1)
        invalid = create_legacy_cutoff_manifest("demo", legacy_cutoff_at=cutoff, programs_root=programs_root)
        gather_run_manifest.write_manifest(
            get_committed_run_dir("demo", invalid.run_id, programs_root=programs_root),
            replace(invalid, manifest_hash="tampered"),
        )
        append_signal(
            Signal("legacy", _now(), "manual", "demo", "ws", ("WI:1",), "legacy", None, Confidence.HIGH),
            programs_root=programs_root, partition_at=_now(),
        )
        ProgramFactStore("demo", db_root=tmp_path).append_fact(
            ProgramFactInput(fact_type="action.item", entity_refs=("WI:1",), payload={"title": "legacy"}),
            recorded_at=_now(),
        )

        with caplog.at_level("WARNING", logger="src.core.gather_run_manifest"):
            assert tuple(
                signal.id for signal in read_signals(
                    "demo", programs_root=programs_root, require_committed_gather_run=True,
                )
            ) == ("legacy",)
            facts = load_program_facts(
                "demo", db_root=tmp_path, programs_root=programs_root,
                as_of=_now() + timedelta(days=1), require_committed_gather_run=True,
            )
            assert {fact.payload["title"] for fact in facts.facts} == {"legacy"}
        assert "invalid committed gather manifest" in caplog.text.lower()

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

    def test_verified_committed_run_state_reads_ids_and_cutoff_in_one_directory_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        manifest = create_legacy_cutoff_manifest("demo", legacy_cutoff_at=cutoff, programs_root=tmp_path)
        committed_root = get_committed_root("demo", programs_root=tmp_path)
        original_iterdir = Path.iterdir
        scans = 0

        def _tracked_iterdir(path: Path):
            nonlocal scans
            if path == committed_root:
                scans += 1
            return original_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", _tracked_iterdir)

        run_ids, selected_cutoff = get_verified_committed_run_state("demo", programs_root=tmp_path)

        assert run_ids == frozenset({manifest.run_id})
        assert selected_cutoff == cutoff
        assert scans == 1
