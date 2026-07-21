from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.workspace_lease import (
    LeaseFencingTokenStale,
    LeaseHeldByAnotherOwner,
    LeaseRenewalHeartbeat,
    acquire_lease,
    is_lease_expired,
    read_lease_state,
    release_lease,
    renew_lease,
)


def test_first_acquire_gets_fencing_token_one(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    handle = acquire_lease("acme", "host-a", programs_root=programs_root)
    assert handle.fencing_token == 1
    assert handle.owner == "host-a"


def test_second_host_cannot_acquire_a_live_lease(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    acquire_lease("acme", "host-a", ttl_seconds=300, programs_root=programs_root)
    with pytest.raises(LeaseHeldByAnotherOwner) as exc_info:
        acquire_lease("acme", "host-b", programs_root=programs_root)
    assert exc_info.value.holder == "host-a"


def test_same_owner_can_reacquire_and_bumps_fencing_token(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    first = acquire_lease("acme", "host-a", programs_root=programs_root)
    second = acquire_lease("acme", "host-a", programs_root=programs_root)
    assert second.fencing_token == first.fencing_token + 1


def test_another_host_can_acquire_after_expiry(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    acquire_lease("acme", "host-a", ttl_seconds=0, programs_root=programs_root)
    time.sleep(0.05)
    handle = acquire_lease("acme", "host-b", programs_root=programs_root)
    assert handle.owner == "host-b"
    assert handle.fencing_token == 2


def test_renew_extends_expiry_with_matching_fencing_token(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    handle = acquire_lease("acme", "host-a", ttl_seconds=1, programs_root=programs_root)
    renewed = renew_lease(handle, ttl_seconds=300, programs_root=programs_root)
    assert renewed.fencing_token == handle.fencing_token
    assert renewed.expires_at > handle.expires_at


def test_renew_with_stale_fencing_token_raises(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    stale_handle = acquire_lease("acme", "host-a", ttl_seconds=0, programs_root=programs_root)
    time.sleep(0.05)
    acquire_lease("acme", "host-b", programs_root=programs_root)  # takes over after expiry

    with pytest.raises(LeaseFencingTokenStale):
        renew_lease(stale_handle, programs_root=programs_root)


def test_renewal_heartbeat_renews_in_background_and_exposes_latest_handle(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    handle = acquire_lease("acme", "host-a", programs_root=programs_root)
    renewed = threading.Event()

    def _renew(current, *, programs_root: Path):
        updated = renew_lease(current, ttl_seconds=300, programs_root=programs_root)
        renewed.set()
        return updated

    heartbeat = LeaseRenewalHeartbeat(
        handle,
        programs_root=programs_root,
        interval_seconds=0.01,
        renew_fn=_renew,
    )
    heartbeat.start()
    assert renewed.wait(timeout=2), "expected the heartbeat to renew the lease"
    heartbeat.stop()

    assert heartbeat.handle.fencing_token == handle.fencing_token
    assert heartbeat.handle.expires_at > handle.expires_at


def test_release_clears_lease_allowing_immediate_reacquire_by_another_host(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    handle = acquire_lease("acme", "host-a", ttl_seconds=300, programs_root=programs_root)
    release_lease(handle, programs_root=programs_root)

    handle_b = acquire_lease("acme", "host-b", programs_root=programs_root)
    assert handle_b.owner == "host-b"


def test_release_with_stale_fencing_token_is_a_no_op(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    stale_handle = acquire_lease("acme", "host-a", ttl_seconds=0, programs_root=programs_root)
    time.sleep(0.05)
    current = acquire_lease("acme", "host-b", programs_root=programs_root)

    release_lease(stale_handle, programs_root=programs_root)  # must not raise, must not touch host-b's lease

    state = read_lease_state("acme", programs_root=programs_root)
    assert state is not None
    assert state.owner == "host-b"
    assert state.fencing_token == current.fencing_token


def test_read_lease_state_without_any_lease_is_none(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert read_lease_state("acme", programs_root=programs_root) is None


def test_is_lease_expired(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    handle = acquire_lease("acme", "host-a", ttl_seconds=300, programs_root=programs_root)
    assert not is_lease_expired(handle)
    future = datetime.now(timezone.utc) + timedelta(seconds=301)
    assert is_lease_expired(handle, at=future)


def test_concurrent_acquire_attempts_serialize_to_exactly_one_winner(tmp_path: Path) -> None:
    """arch-fix.md H4: BEGIN IMMEDIATE must make the read-decide-write
    sequence atomic across connections, so concurrent acquisition attempts
    from multiple threads (simulating multiple hosts) never both succeed."""
    programs_root = tmp_path / "programs"
    winners: list[str] = []
    losers: list[str] = []
    lock = threading.Lock()

    def _attempt(owner: str) -> None:
        try:
            acquire_lease("acme", owner, ttl_seconds=300, programs_root=programs_root)
            with lock:
                winners.append(owner)
        except LeaseHeldByAnotherOwner:
            with lock:
                losers.append(owner)

    threads = [threading.Thread(target=_attempt, args=(f"host-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(winners) == 1, f"expected exactly one winner, got {winners}"
    assert len(losers) == 7
