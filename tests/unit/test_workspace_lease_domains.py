"""ADF-W1.13 (Appendix A.11): ``(program_id, mutation_domain)`` lease granularity.

Independent mutation domains on the same program must serialize
independently (one domain's held lease never blocks another domain), the
same domain must still serialize exactly as the pre-ADF-W1.13 single-lease
primitive did, stale fencing tokens remain enforced per domain, and a
pre-existing on-disk singleton-schema database upcasts in place to the
``"workspace"`` domain.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.workspace_lease import (
    DEFAULT_MUTATION_DOMAIN,
    LeaseFencingTokenStale,
    LeaseHeldByAnotherOwner,
    acquire_lease,
    get_workspace_lease_db_path,
    read_lease_state,
    release_lease,
    renew_lease,
)


def test_independent_domains_do_not_contend(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    confirm_handle = acquire_lease("acme", "host-a", mutation_domain="confirm_publish", programs_root=programs_root)
    # A different domain, same program, different owner -- must not raise.
    actuation_handle = acquire_lease("acme", "host-b", mutation_domain="actuation_dispatch", programs_root=programs_root)

    assert confirm_handle.mutation_domain == "confirm_publish"
    assert actuation_handle.mutation_domain == "actuation_dispatch"
    assert confirm_handle.fencing_token == 1
    assert actuation_handle.fencing_token == 1  # independent counters per domain


def test_same_domain_still_serializes(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    acquire_lease("acme", "host-a", mutation_domain="actuation_dispatch", ttl_seconds=300, programs_root=programs_root)
    with pytest.raises(LeaseHeldByAnotherOwner) as exc_info:
        acquire_lease("acme", "host-b", mutation_domain="actuation_dispatch", programs_root=programs_root)
    assert exc_info.value.holder == "host-a"
    assert exc_info.value.mutation_domain == "actuation_dispatch"


def test_default_domain_matches_pre_w1_13_single_lease_behavior(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    handle = acquire_lease("acme", "host-a", programs_root=programs_root)
    assert handle.mutation_domain == DEFAULT_MUTATION_DOMAIN == "workspace"
    with pytest.raises(LeaseHeldByAnotherOwner):
        acquire_lease("acme", "host-b", programs_root=programs_root)


def test_stale_fencing_token_cannot_renew_or_release_its_domain(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    stale_handle = acquire_lease("acme", "host-a", mutation_domain="fact_admin", ttl_seconds=0, programs_root=programs_root)
    time.sleep(0.05)
    current = acquire_lease("acme", "host-b", mutation_domain="fact_admin", programs_root=programs_root)  # takes over after expiry

    with pytest.raises(LeaseFencingTokenStale) as exc_info:
        renew_lease(stale_handle, programs_root=programs_root)
    assert exc_info.value.mutation_domain == "fact_admin"

    release_lease(stale_handle, programs_root=programs_root)  # must not raise, must not touch host-b's lease
    state = read_lease_state("acme", mutation_domain="fact_admin", programs_root=programs_root)
    assert state is not None
    assert state.owner == "host-b"
    assert state.fencing_token == current.fencing_token


def test_read_lease_state_is_scoped_to_its_domain(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    acquire_lease("acme", "host-a", mutation_domain="nudge_send", programs_root=programs_root)
    assert read_lease_state("acme", mutation_domain="nudge_send", programs_root=programs_root) is not None
    assert read_lease_state("acme", mutation_domain="state_migration", programs_root=programs_root) is None


def test_concurrent_acquire_attempts_on_the_same_domain_serialize_to_one_winner(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    winners: list[str] = []
    losers: list[str] = []
    lock = threading.Lock()

    def _attempt(owner: str) -> None:
        try:
            acquire_lease("acme", owner, mutation_domain="actuation_dispatch", ttl_seconds=300, programs_root=programs_root)
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


def test_concurrent_acquire_on_independent_domains_all_succeed(tmp_path: Path) -> None:
    """The defining ADF-W1.13 property: independent domains run concurrently."""
    programs_root = tmp_path / "programs"
    results: dict[str, object] = {}
    lock = threading.Lock()

    def _attempt(domain: str) -> None:
        handle = acquire_lease("acme", f"owner-{domain}", mutation_domain=domain, ttl_seconds=300, programs_root=programs_root)
        with lock:
            results[domain] = handle

    domains = ["confirm_publish", "actuation_dispatch", "fact_admin", "nudge_send", "state_migration", "acquisition:workiq"]
    threads = [threading.Thread(target=_attempt, args=(domain,)) for domain in domains]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert set(results.keys()) == set(domains)
    for domain, handle in results.items():
        assert handle.mutation_domain == domain
        assert handle.fencing_token == 1  # each domain's own independent counter


def test_pre_adf_w1_13_singleton_schema_migrates_in_place(tmp_path: Path) -> None:
    """A database written by the old id=1 singleton schema must upcast to
    the "workspace" domain the next time acquire_lease touches it."""
    programs_root = tmp_path / "programs"
    db_path = get_workspace_lease_db_path("acme", programs_root=programs_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    legacy_connection = sqlite3.connect(str(db_path))
    try:
        legacy_connection.executescript(
            """
            CREATE TABLE workspace_lease (
                id            INTEGER PRIMARY KEY CHECK (id = 1),
                owner         TEXT NOT NULL,
                fencing_token INTEGER NOT NULL,
                acquired_at   TEXT NOT NULL,
                expires_at    TEXT NOT NULL
            );
            """
        )
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        future = "2099-01-01T00:00:00Z"
        legacy_connection.execute(
            "INSERT INTO workspace_lease (id, owner, fencing_token, acquired_at, expires_at) VALUES (1, 'legacy-host', 7, ?, ?)",
            (now, future),
        )
        legacy_connection.commit()
    finally:
        legacy_connection.close()

    # Reading the pre-migration state under the new API is a safe no-op (a
    # read-only connection cannot run the migration itself).
    assert read_lease_state("acme", programs_root=programs_root) is None

    # The legacy row's owner is still live (far-future expiry), so a
    # different owner acquiring the "workspace" domain must be rejected --
    # proving the migration actually preserved the legacy holder, not
    # silently dropped it.
    with pytest.raises(LeaseHeldByAnotherOwner) as exc_info:
        acquire_lease("acme", "new-host", programs_root=programs_root)
    assert exc_info.value.holder == "legacy-host"

    # The legacy owner can still operate under its migrated identity.
    migrated = acquire_lease("acme", "legacy-host", programs_root=programs_root)
    assert migrated.mutation_domain == DEFAULT_MUTATION_DOMAIN
    assert migrated.fencing_token == 8  # legacy token 7, bumped by the re-acquire
