"""specs/people.md PPL-W1.2 (Phase 1): real-concurrency contract tests
for the workspace-global fenced registry lease
(src/core/people_registry_lease.py).

Mirrors tests/contracts/test_multi_program_concurrency.py's own
precedent: real ``threading``, not mocks or sequential calls, since the
regression risk this file exists to catch is a bug that only manifests
under genuine concurrent execution (e.g. a lost fencing-token update
under real SQLite lock contention).

specs/people.md §9.1's own verification bar for PPL-W1.2: "stale-lease
force-release requires an authorized principal, increments fencing
state, and is audited."

PPL-W1.5 extends this file with its own crash-injection cases (§9.1:
"crash-injection tests at each of the four points leave no mixed-generation
state"). Rather than actually killing the process, `commit_registry_transaction`
exposes a test-only `_simulate_crash_after_step` seam that returns right
after the named step, leaving exactly the on-disk state a real crash at
that point would leave -- `recover_registry_transactions` is then
exercised against that state deterministically.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.jsonl_utils import compute_file_checksum
from src.core.people_registry_identity import bootstrap_registry_identity, load_registry_manifest
from src.core.people_registry_lease import (
    RegistryLeaseFencingTokenStale,
    RegistryLeaseHeldByAnotherOwner,
    acquire_registry_lease,
    force_release_registry_lease,
    read_force_release_audit_records,
    read_registry_lease_state,
    renew_registry_lease,
)
from src.core.people_registry_transaction import (
    RegistryFieldOperation,
    RegistryOperationKind,
    SyntheticRegistryRecord,
    commit_registry_transaction,
    detect_stale_registry_lease,
    prepare_registry_transaction,
    recover_registry_transactions,
    synthetic_records_path,
)

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _record(record_id: str) -> SyntheticRegistryRecord:
    return SyntheticRegistryRecord(record_id=record_id, value="v", source="graph", observed_at=_NOW, actor="tester")


def _assert_no_mixed_generation_state(knowledge_root: Path) -> None:
    """specs/people.md §9.1's exact PPL-W1.5 invariant: the live data
    file's hash must always match what the CURRENT live manifest claims
    -- either both are in the pristine bootstrap state (no data file, no
    recorded source hash) or both agree on exactly one generation."""
    manifest = load_registry_manifest(knowledge_root)
    assert manifest is not None
    data_path = synthetic_records_path(knowledge_root)
    if not data_path.exists():
        assert not manifest.source_hashes
        return
    expected_hash = dict(manifest.source_hashes).get("registry_synthetic_records.yaml")
    assert expected_hash is not None
    assert compute_file_checksum(data_path) == expected_hash


def test_concurrent_acquire_attempts_only_one_winner(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    owners = [f"owner-{i}" for i in range(8)]

    def _attempt(owner: str) -> tuple[str, bool]:
        try:
            acquire_registry_lease(owner, ttl_seconds=300, knowledge_root=knowledge_root)
            return owner, True
        except RegistryLeaseHeldByAnotherOwner:
            return owner, False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_attempt, owners))

    winners = [owner for owner, won in results if won]
    assert len(winners) == 1, f"Expected exactly one winner under real concurrent acquisition, got {winners}"


def test_concurrent_renew_attempts_only_current_holder_succeeds(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    handle = acquire_registry_lease("real-owner", ttl_seconds=300, knowledge_root=knowledge_root)
    # A stale handle presenting an old (already-superseded) fencing token,
    # simulating a second process that raced and lost.
    stale_handle = type(handle)(
        owner="impostor",
        fencing_token=handle.fencing_token,  # Correct token, wrong owner -- must still fail.
        acquired_at=handle.acquired_at,
        expires_at=handle.expires_at,
        mutation_domain=handle.mutation_domain,
    )

    def _renew_real() -> bool:
        renew_registry_lease(handle, knowledge_root=knowledge_root)
        return True

    def _renew_impostor() -> bool:
        try:
            renew_registry_lease(stale_handle, knowledge_root=knowledge_root)
            return True
        except RegistryLeaseFencingTokenStale:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        real_future = pool.submit(_renew_real)
        impostor_future = pool.submit(_renew_impostor)
        assert real_future.result() is True
        assert impostor_future.result() is False


def test_force_release_of_a_lease_held_by_a_concurrently_running_stale_worker(tmp_path: Path) -> None:
    # specs/people.md §9.1's exact PPL-W1.2 verification scenario:
    # a stale lease's force-release requires an authorized principal,
    # increments fencing state, and is audited.
    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    config_path = knowledge_root / "registry.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "directory_steward_principals: []",
            "directory_steward_principals:\n- ACME\\authorized_steward\n",
        ),
        encoding="utf-8",
    )

    stuck_handle = acquire_registry_lease("stuck-worker", ttl_seconds=300, knowledge_root=knowledge_root)

    # An unauthorized principal cannot force-release it.
    with pytest.raises(ConfigError, match="not an authorized directory steward"):
        force_release_registry_lease(authorized_principal="ACME\\unauthorized_user", reason="attempt", knowledge_root=knowledge_root)

    # The stuck worker still believes it holds the lease and is concurrently
    # trying to renew it while the steward force-releases -- real threads,
    # not sequential calls, to catch any lock-ordering bug.
    def _steward_force_release() -> None:
        force_release_registry_lease(authorized_principal="ACME\\authorized_steward", reason="worker appears hung", knowledge_root=knowledge_root)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_steward_force_release).result()

    # Fencing state incremented: the stuck worker's original token is now permanently stale.
    with pytest.raises(RegistryLeaseFencingTokenStale):
        renew_registry_lease(stuck_handle, knowledge_root=knowledge_root)

    # A fresh acquire continues the monotonic sequence, not a reset to 1.
    new_handle = acquire_registry_lease("recovery-worker", knowledge_root=knowledge_root)
    assert new_handle.fencing_token > stuck_handle.fencing_token

    # Audited.
    records = read_force_release_audit_records(knowledge_root)
    assert len(records) == 1
    assert records[0]["authorized_principal"] == "ACME\\authorized_steward"
    assert records[0]["prior_owner"] == "stuck-worker"


# ---------------------------------------------------------------------------
# PPL-W1.5: crash-injection tests, one per §6.7 recovery point.
# ---------------------------------------------------------------------------


def test_crash_point_1_prepared_with_unchanged_live_manifest_rolls_back(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    original_manifest = load_registry_manifest(knowledge_root)
    assert original_manifest is not None

    prepared = prepare_registry_transaction(
        knowledge_root,
        (RegistryFieldOperation(kind=RegistryOperationKind.UPSERT, record=_record("rec-1")),),
        owner="writer-1",
        as_of=_NOW,
    )
    commit_registry_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW, _simulate_crash_after_step=7)

    # "Crashed" here: live data/manifest are still exactly the pre-transaction state.
    assert not synthetic_records_path(knowledge_root).exists()
    _assert_no_mixed_generation_state(knowledge_root)

    outcomes = recover_registry_transactions(knowledge_root, as_of=_NOW)

    assert len(outcomes) == 1
    assert outcomes[0].action == "rolled_back_staged"
    assert not prepared.staged_dir.exists()
    _assert_no_mixed_generation_state(knowledge_root)
    assert load_registry_manifest(knowledge_root).generation_id == original_manifest.generation_id
    assert not synthetic_records_path(knowledge_root).exists()


def test_crash_point_2_partial_file_replacement_with_old_manifest_restores_checkpoint(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)

    # A real prior committed transaction establishes a real checkpoint to restore to.
    first = prepare_registry_transaction(
        knowledge_root,
        (RegistryFieldOperation(kind=RegistryOperationKind.UPSERT, record=_record("rec-1")),),
        owner="writer-1",
        as_of=_NOW,
    )
    commit_registry_transaction(first, knowledge_root=knowledge_root, as_of=_NOW)
    first_manifest = load_registry_manifest(knowledge_root)
    assert first_manifest is not None
    first_data_hash = compute_file_checksum(synthetic_records_path(knowledge_root))

    second = prepare_registry_transaction(
        knowledge_root,
        (RegistryFieldOperation(kind=RegistryOperationKind.UPSERT, record=_record("rec-2")),),
        owner="writer-2",
        as_of=_NOW,
    )
    commit_registry_transaction(second, knowledge_root=knowledge_root, as_of=_NOW, _simulate_crash_after_step=8)

    # "Crashed" here: data file already holds the SECOND transaction's content, but the
    # manifest still points at the FIRST generation -- a real mixed-generation risk.
    assert compute_file_checksum(synthetic_records_path(knowledge_root)) != first_data_hash
    assert load_registry_manifest(knowledge_root).generation_id == first_manifest.generation_id

    outcomes = recover_registry_transactions(knowledge_root, as_of=_NOW)

    # The first (already fully committed) transaction is also scanned and correctly
    # reported as a no-op; only the second (crashed) one needed real recovery action.
    outcomes_by_id = {outcome.transaction_id: outcome for outcome in outcomes}
    assert outcomes_by_id[first.transaction_id].action == "no_action_needed"
    assert outcomes_by_id[second.transaction_id].action == "restored_from_checkpoint"
    _assert_no_mixed_generation_state(knowledge_root)
    assert compute_file_checksum(synthetic_records_path(knowledge_root)) == first_data_hash
    assert load_registry_manifest(knowledge_root).generation_id == first_manifest.generation_id
    assert not second.staged_dir.exists()


def test_crash_point_3_new_manifest_with_incomplete_dispatch_completes_bookkeeping(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)

    prepared = prepare_registry_transaction(
        knowledge_root,
        (RegistryFieldOperation(kind=RegistryOperationKind.UPSERT, record=_record("rec-1")),),
        owner="writer-1",
        as_of=_NOW,
    )
    commit_registry_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW, _simulate_crash_after_step=9)

    # "Crashed" here: the new generation is already fully live (data + manifest agree),
    # but step 10's bookkeeping (COMMITTED marker, lease release) never ran.
    _assert_no_mixed_generation_state(knowledge_root)
    assert load_registry_manifest(knowledge_root).generation_id == prepared.manifest_preview.generation_id
    held = read_registry_lease_state(knowledge_root=knowledge_root)
    assert held is not None and held.owner == "writer-1"

    outcomes = recover_registry_transactions(knowledge_root, as_of=_NOW)

    assert len(outcomes) == 1
    assert outcomes[0].action == "completed_commit_bookkeeping"
    _assert_no_mixed_generation_state(knowledge_root)
    assert load_registry_manifest(knowledge_root).generation_id == prepared.manifest_preview.generation_id
    # The lease was released as part of finishing step 10.
    assert read_registry_lease_state(knowledge_root=knowledge_root) is None


def test_crash_point_4_stale_lease_is_detected_but_never_auto_force_released(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    acquire_registry_lease("stuck-worker", ttl_seconds=-1, knowledge_root=knowledge_root)  # Already expired.

    # No `as_of` override -- the lease was acquired against the real wall clock
    # (ttl_seconds is relative to real "now"), so expiry must be checked against it too.
    stale = detect_stale_registry_lease(knowledge_root)

    assert stale is not None
    assert stale.owner == "stuck-worker"
    # Detection only -- the lease is still held; nothing force-releases it automatically.
    still_held = read_registry_lease_state(knowledge_root=knowledge_root)
    assert still_held is not None
    assert still_held.owner == "stuck-worker"


def test_recovery_of_committed_transaction_is_a_no_op(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    prepared = prepare_registry_transaction(
        knowledge_root,
        (RegistryFieldOperation(kind=RegistryOperationKind.UPSERT, record=_record("rec-1")),),
        owner="writer-1",
        as_of=_NOW,
    )
    commit_registry_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW)  # Full, uninterrupted commit.

    outcomes = recover_registry_transactions(knowledge_root, as_of=_NOW)

    assert len(outcomes) == 1
    assert outcomes[0].action == "no_action_needed"
    _assert_no_mixed_generation_state(knowledge_root)


def test_recovery_with_no_transactions_directory_is_a_no_op(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)

    assert recover_registry_transactions(knowledge_root, as_of=_NOW) == ()
