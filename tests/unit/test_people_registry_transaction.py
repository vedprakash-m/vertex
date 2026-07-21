"""specs/people.md Phase 1, PPL-W1.4/PPL-W1.5: tests for the staged
transaction primitive (src/core/people_registry_transaction.py). Crash-
injection recovery tests live in
tests/contracts/test_shared_registry_concurrency.py (the file specs/people.md
§9.1 names for PPL-W1.5's verification bar) -- this file covers the
prepare half plus commit-half unit-level cases (success, fencing
mismatch) that don't need real concurrency."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.people_registry_identity import bootstrap_registry_identity, load_registry_manifest
from src.core.people_registry_lease import (
    RegistryLeaseFencingTokenStale,
    acquire_registry_lease,
    force_release_registry_lease,
    read_registry_lease_state,
)
from src.core.people_registry_transaction import (
    PreparedRegistryTransaction,
    RegistryFieldOperation,
    RegistryGenerationStale,
    RegistryOperationKind,
    RegistryTransactionValidationError,
    SyntheticRegistryRecord,
    abort_prepared_registry_transaction,
    commit_registry_transaction,
    prepare_registry_transaction,
    synthetic_records_path,
    transactions_root,
)

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _bootstrap(knowledge_root: Path) -> None:
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)


def _valid_record(record_id: str = "rec-1", **overrides: object) -> SyntheticRegistryRecord:
    defaults = dict(
        record_id=record_id,
        value="a value",
        source="graph",
        observed_at=_NOW,
        actor="tester",
    )
    defaults.update(overrides)
    return SyntheticRegistryRecord(**defaults)  # type: ignore[arg-type]


def _upsert(record: SyntheticRegistryRecord) -> RegistryFieldOperation:
    return RegistryFieldOperation(kind=RegistryOperationKind.UPSERT, record=record)


def test_prepare_requires_bootstrap_first(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    with pytest.raises(ConfigError, match="has not been bootstrapped"):
        prepare_registry_transaction(knowledge_root, (_upsert(_valid_record()),), owner="writer-1")

    assert read_registry_lease_state(knowledge_root=knowledge_root) is None


def test_prepare_succeeds_stages_files_and_holds_the_lease(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)

    prepared = prepare_registry_transaction(knowledge_root, (_upsert(_valid_record()),), owner="writer-1", as_of=_NOW)

    assert isinstance(prepared, PreparedRegistryTransaction)
    assert prepared.staged_dir.exists()
    assert (prepared.staged_dir / "registry_synthetic_records.yaml").exists()
    assert (prepared.staged_dir / "operations.json").exists()
    assert prepared.final_records == (_valid_record(),)
    # The lease is still held -- prepare never releases it.
    held = read_registry_lease_state(knowledge_root=knowledge_root)
    assert held is not None
    assert held.owner == "writer-1"
    # Never touches a live file.
    assert not synthetic_records_path(knowledge_root).exists()

    abort_prepared_registry_transaction(prepared, knowledge_root=knowledge_root)


def test_prepare_manifest_preview_carries_fencing_token_and_lineage(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    manifest = load_registry_manifest(knowledge_root)
    assert manifest is not None

    prepared = prepare_registry_transaction(knowledge_root, (_upsert(_valid_record()),), owner="writer-1", as_of=_NOW)

    assert prepared.manifest_preview.prior_generation == manifest.generation_id
    assert prepared.manifest_preview.fencing_token == prepared.lease.fencing_token
    assert prepared.manifest_preview.transaction_id == prepared.transaction_id
    assert prepared.manifest_preview.source_hashes[0][0] == "registry_synthetic_records.yaml"

    abort_prepared_registry_transaction(prepared, knowledge_root=knowledge_root)


def test_prepare_rejects_stale_expected_generation_and_releases_lease(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)

    with pytest.raises(RegistryGenerationStale):
        prepare_registry_transaction(
            knowledge_root,
            (_upsert(_valid_record()),),
            owner="writer-1",
            expected_generation_id="registry-generation:not-the-real-one",
        )

    assert read_registry_lease_state(knowledge_root=knowledge_root) is None


def test_abort_prepared_transaction_releases_lease_and_removes_staged_dir(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    prepared = prepare_registry_transaction(knowledge_root, (_upsert(_valid_record()),), owner="writer-1", as_of=_NOW)

    abort_prepared_registry_transaction(prepared, knowledge_root=knowledge_root)

    assert read_registry_lease_state(knowledge_root=knowledge_root) is None
    assert not prepared.staged_dir.exists()


def test_deactivate_of_nonexistent_record_is_a_no_op_not_a_violation(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)

    prepared = prepare_registry_transaction(
        knowledge_root,
        (RegistryFieldOperation(kind=RegistryOperationKind.DEACTIVATE, record_id="does-not-exist"),),
        owner="writer-1",
        as_of=_NOW,
    )

    assert prepared.final_records == ()
    abort_prepared_registry_transaction(prepared, knowledge_root=knowledge_root)


@pytest.mark.parametrize(
    "build_operations, match",
    [
        pytest.param(lambda: (_upsert(_valid_record(value="")),), "schema:", id="empty-value"),
        pytest.param(lambda: (_upsert(_valid_record(record_id="")),), "schema:", id="empty-record-id"),
        pytest.param(
            lambda: (_upsert(_valid_record(record_id="Alice")), _upsert(_valid_record(record_id="alice"))),
            "uniqueness:",
            id="casefold-collision",
        ),
        pytest.param(lambda: (_upsert(_valid_record(parent_id="ghost")),), "reference:", id="missing-parent"),
        pytest.param(
            lambda: (
                _upsert(_valid_record(record_id="rec-a", parent_id="rec-b")),
                _upsert(_valid_record(record_id="rec-b", parent_id="rec-a")),
            ),
            "cycle:",
            id="mutual-cycle",
        ),
        pytest.param(lambda: (_upsert(_valid_record(restricted=True, policy_approved=False)),), "policy:", id="restricted-not-approved"),
        pytest.param(lambda: (_upsert(_valid_record(source="")),), "source metadata:", id="missing-source"),
        pytest.param(lambda: (_upsert(_valid_record(actor="")),), "source metadata:", id="missing-actor"),
        pytest.param(lambda: (_upsert(_valid_record(observed_at=None)),), "source metadata:", id="missing-observed-at"),
    ],
)
def test_each_validation_failure_path_aborts_before_any_live_file_is_touched(tmp_path: Path, build_operations, match: str) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)

    with pytest.raises(RegistryTransactionValidationError, match=match):
        prepare_registry_transaction(knowledge_root, build_operations(), owner="writer-1", as_of=_NOW)

    # No live file touched, no staged transaction directory left behind, lease released.
    assert not synthetic_records_path(knowledge_root).exists()
    assert not transactions_root(knowledge_root).exists()
    assert read_registry_lease_state(knowledge_root=knowledge_root) is None


def test_lifecycle_violation_when_reactivating_a_baseline_inactive_record(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    # Write the baseline directly rather than through a real prepare/commit cycle --
    # this test only needs the precondition (an inactive record already live), not a
    # realistic transaction history.
    from src.core.people_registry_transaction import _write_synthetic_records

    inactive_record = _valid_record(active=False)
    _write_synthetic_records(synthetic_records_path(knowledge_root), (inactive_record,))

    with pytest.raises(RegistryTransactionValidationError, match="lifecycle:"):
        prepare_registry_transaction(knowledge_root, (_upsert(_valid_record(active=True)),), owner="writer-1", as_of=_NOW)

    assert read_registry_lease_state(knowledge_root=knowledge_root) is None


# ---------------------------------------------------------------------------
# PPL-W1.5: commit-half unit tests. Real-concurrency and crash-injection
# cases live in tests/contracts/test_shared_registry_concurrency.py.
# ---------------------------------------------------------------------------


def test_commit_writes_live_files_matching_the_manifest_preview_and_releases_lease(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    prepared = prepare_registry_transaction(knowledge_root, (_upsert(_valid_record()),), owner="writer-1", as_of=_NOW)

    committed = commit_registry_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW)

    assert committed.manifest.generation_id == prepared.manifest_preview.generation_id
    live_manifest = load_registry_manifest(knowledge_root)
    assert live_manifest is not None
    assert live_manifest.generation_id == prepared.manifest_preview.generation_id
    assert live_manifest.transaction_id == prepared.transaction_id
    assert synthetic_records_path(knowledge_root).exists()
    assert read_registry_lease_state(knowledge_root=knowledge_root) is None


def test_commit_raises_on_fencing_mismatch_when_lease_was_taken_over(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap(knowledge_root)
    config_path = knowledge_root / "registry.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "directory_steward_principals: []",
            "directory_steward_principals:\n- ACME\\authorized_steward\n",
        ),
        encoding="utf-8",
    )
    prepared = prepare_registry_transaction(knowledge_root, (_upsert(_valid_record()),), owner="writer-1", as_of=_NOW)
    # Simulate the original writer's lease being force-released and taken over by
    # someone else while its transaction was still being prepared/about to commit.
    force_release_registry_lease(authorized_principal="ACME\\authorized_steward", reason="writer-1 appeared hung", knowledge_root=knowledge_root)

    with pytest.raises(RegistryLeaseFencingTokenStale):
        commit_registry_transaction(prepared, knowledge_root=knowledge_root, as_of=_NOW)

    # No live file was touched -- the fencing check happens before the irreversible replace.
    assert not synthetic_records_path(knowledge_root).exists()
