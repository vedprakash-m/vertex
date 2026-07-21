"""specs/people.md Phase 1, PPL-W1.2: tests for the workspace-global
fenced registry lease (src/core/people_registry_lease.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.people_registry_identity import bootstrap_registry_identity
from src.core.people_registry_lease import (
    RegistryLeaseFencingTokenStale,
    RegistryLeaseHeldByAnotherOwner,
    acquire_registry_lease,
    force_release_registry_lease,
    is_registry_lease_expired,
    read_force_release_audit_records,
    read_registry_lease_state,
    release_registry_lease,
    renew_registry_lease,
)


def test_acquire_lease_when_none_held_starts_at_fencing_token_one(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    handle = acquire_registry_lease("owner-a", knowledge_root=knowledge_root)

    assert handle.fencing_token == 1
    assert handle.owner == "owner-a"


def test_acquire_lease_by_same_owner_is_a_renewal_that_increments_token(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    first = acquire_registry_lease("owner-a", knowledge_root=knowledge_root)

    second = acquire_registry_lease("owner-a", knowledge_root=knowledge_root)

    assert second.fencing_token == first.fencing_token + 1


def test_acquire_lease_by_different_owner_while_held_and_not_expired_raises(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    acquire_registry_lease("owner-a", ttl_seconds=300, knowledge_root=knowledge_root)

    with pytest.raises(RegistryLeaseHeldByAnotherOwner):
        acquire_registry_lease("owner-b", knowledge_root=knowledge_root)


def test_acquire_lease_by_different_owner_after_expiry_succeeds(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    acquire_registry_lease("owner-a", ttl_seconds=-1, knowledge_root=knowledge_root)  # Already expired.

    handle = acquire_registry_lease("owner-b", knowledge_root=knowledge_root)

    assert handle.owner == "owner-b"
    assert handle.fencing_token == 2  # Monotonic -- did not reset to 1.


def test_renew_lease_requires_current_fencing_token(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    handle = acquire_registry_lease("owner-a", knowledge_root=knowledge_root)

    renewed = renew_registry_lease(handle, knowledge_root=knowledge_root)

    assert renewed.fencing_token == handle.fencing_token
    assert renewed.expires_at > handle.expires_at or renewed.expires_at == handle.expires_at


def test_renew_lease_with_stale_token_raises(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    handle = acquire_registry_lease("owner-a", ttl_seconds=-1, knowledge_root=knowledge_root)
    # A second acquire (post-expiry) supersedes the first handle's token.
    acquire_registry_lease("owner-b", knowledge_root=knowledge_root)

    with pytest.raises(RegistryLeaseFencingTokenStale):
        renew_registry_lease(handle, knowledge_root=knowledge_root)


def test_release_lease_is_idempotent_and_frees_it_for_others(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    handle = acquire_registry_lease("owner-a", knowledge_root=knowledge_root)

    release_registry_lease(handle, knowledge_root=knowledge_root)
    release_registry_lease(handle, knowledge_root=knowledge_root)  # Idempotent no-op.

    assert read_registry_lease_state(knowledge_root=knowledge_root) is None
    # Now available for a different owner without waiting for TTL.
    new_handle = acquire_registry_lease("owner-b", knowledge_root=knowledge_root)
    assert new_handle.owner == "owner-b"


def test_release_lease_with_superseded_token_is_a_no_op(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    stale_handle = acquire_registry_lease("owner-a", ttl_seconds=-1, knowledge_root=knowledge_root)
    current_handle = acquire_registry_lease("owner-b", knowledge_root=knowledge_root)

    release_registry_lease(stale_handle, knowledge_root=knowledge_root)  # Must not release owner-b's lease.

    still_held = read_registry_lease_state(knowledge_root=knowledge_root)
    assert still_held is not None
    assert still_held.owner == "owner-b"
    assert still_held.fencing_token == current_handle.fencing_token


def test_is_registry_lease_expired() -> None:
    from datetime import datetime, timedelta, timezone

    from src.core.people_registry_lease import RegistryLeaseHandle

    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    handle = RegistryLeaseHandle(owner="x", fencing_token=1, acquired_at=now, expires_at=now + timedelta(seconds=10))

    assert is_registry_lease_expired(handle, at=now + timedelta(seconds=20)) is True
    assert is_registry_lease_expired(handle, at=now + timedelta(seconds=5)) is False


def _bootstrap_with_steward(knowledge_root: Path, *, steward_principal: str) -> None:
    bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id="acme-corp", apply=True)
    # Directly edit the just-written registry.yaml to add a steward, since
    # PPL-W1.1's bootstrap doesn't accept a steward list (a later work item's
    # scope) -- this test only needs the config parseable with the field set.
    config_path = knowledge_root / "registry.yaml"
    text = config_path.read_text(encoding="utf-8")
    text = text.replace("directory_steward_principals: []", f"directory_steward_principals:\n- {steward_principal}\n")
    config_path.write_text(text, encoding="utf-8")


def test_force_release_requires_authorized_steward_principal(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap_with_steward(knowledge_root, steward_principal="ACME\\authorized_steward")
    acquire_registry_lease("stuck-owner", knowledge_root=knowledge_root)

    with pytest.raises(ConfigError, match="not an authorized directory steward"):
        force_release_registry_lease(authorized_principal="ACME\\random_user", reason="testing", knowledge_root=knowledge_root)


def test_force_release_requires_a_reason(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap_with_steward(knowledge_root, steward_principal="ACME\\authorized_steward")

    with pytest.raises(ConfigError, match="reason"):
        force_release_registry_lease(authorized_principal="ACME\\authorized_steward", reason="", knowledge_root=knowledge_root)


def test_force_release_increments_fencing_and_appends_audit_record(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap_with_steward(knowledge_root, steward_principal="ACME\\authorized_steward")
    stuck_handle = acquire_registry_lease("stuck-owner", knowledge_root=knowledge_root)

    force_release_registry_lease(authorized_principal="ACME\\authorized_steward", reason="stuck writer", knowledge_root=knowledge_root)

    # The stuck owner's original token is now permanently stale.
    with pytest.raises(RegistryLeaseFencingTokenStale):
        renew_registry_lease(stuck_handle, knowledge_root=knowledge_root)

    # Fencing continues monotonically -- the next acquire does not restart at 1.
    new_handle = acquire_registry_lease("new-owner", knowledge_root=knowledge_root)
    assert new_handle.fencing_token > stuck_handle.fencing_token

    records = read_force_release_audit_records(knowledge_root)
    assert len(records) == 1
    assert records[0]["authorized_principal"] == "ACME\\authorized_steward"
    assert records[0]["reason"] == "stuck writer"
    assert records[0]["prior_owner"] == "stuck-owner"


def test_force_release_with_no_lease_held_still_authorizes_and_audits(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _bootstrap_with_steward(knowledge_root, steward_principal="ACME\\authorized_steward")

    force_release_registry_lease(authorized_principal="ACME\\authorized_steward", reason="preemptive", knowledge_root=knowledge_root)

    records = read_force_release_audit_records(knowledge_root)
    assert len(records) == 1
    assert records[0]["prior_owner"] is None
