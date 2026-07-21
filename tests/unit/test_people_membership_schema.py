"""specs/people.md Phase 2a, PPL-W2A.3: tests for memberships.yaml schema
1.0 + hot/cold partitioning (src/core/people_membership_schema.py).

specs/people.md §9.1's own verification bar: "Idempotent re-observation
produces no duplicate membership; `--as-of` reads hot+archived correctly."
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.people_membership_schema import (
    DEFAULT_HOT_WINDOW_DAYS,
    MembershipStatus,
    archive_expired_memberships,
    compute_membership_id,
    load_memberships,
    memberships_path,
    normalize_role,
    observe_membership,
    read_all_memberships,
    read_memberships_as_of,
    write_memberships,
)

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def test_load_memberships_reads_the_real_example_fixture() -> None:
    path = Path(__file__).resolve().parents[2] / "knowledge" / "memberships.example.yaml"

    memberships = load_memberships(path)

    assert len(memberships) == 1
    assert memberships[0].role == "member"
    assert memberships[0].status == MembershipStatus.ACTIVE


def test_load_memberships_returns_empty_tuple_when_missing(tmp_path: Path) -> None:
    assert load_memberships(tmp_path / "memberships.yaml") == ()


def test_normalize_role_passes_through_known_tokens() -> None:
    assert normalize_role("Member", provider="ado") == "member"
    assert normalize_role("LEAD", provider="ado") == "lead"


def test_normalize_role_maps_unknown_to_provider_namespaced() -> None:
    assert normalize_role("contributor", provider="ado") == "ado::contributor"


def test_normalize_role_none_becomes_unknown() -> None:
    assert normalize_role(None, provider="ado") == "unknown"


def test_compute_membership_id_is_deterministic_and_provider_scoped() -> None:
    first = compute_membership_id(
        provider="ado", tenant_id="tenant-1", person_entity_id="person:1", team_entity_id="team:1",
        role="member", valid_from_or_first_observed=_NOW,
    )
    second = compute_membership_id(
        provider="ado", tenant_id="tenant-1", person_entity_id="person:1", team_entity_id="team:1",
        role="member", valid_from_or_first_observed=_NOW,
    )
    different_tenant = compute_membership_id(
        provider="ado", tenant_id="tenant-2", person_entity_id="person:1", team_entity_id="team:1",
        role="member", valid_from_or_first_observed=_NOW,
    )

    assert first == second
    assert first.startswith("membership:")
    assert first != different_tenant


def test_observe_membership_first_observation_creates_a_new_record() -> None:
    updated, membership = observe_membership(
        (), provider="ado", tenant_id=None, person_entity_id="person:1", team_entity_id="team:1",
        raw_role="member", valid_from=_NOW, valid_until=None, source_ref=None, observed_at=_NOW, verified_at=_NOW,
    )

    assert len(updated) == 1
    assert membership.role == "member"


def test_idempotent_reobservation_produces_no_duplicate_membership() -> None:
    # specs/people.md §9.1's exact PPL-W2A.3 verification.
    first_set, first_membership = observe_membership(
        (), provider="ado", tenant_id=None, person_entity_id="person:1", team_entity_id="team:1",
        raw_role="member", valid_from=_NOW, valid_until=None, source_ref="ref-1", observed_at=_NOW, verified_at=_NOW,
    )

    later = _NOW + timedelta(days=1)
    second_set, second_membership = observe_membership(
        first_set, provider="ado", tenant_id=None, person_entity_id="person:1", team_entity_id="team:1",
        raw_role="member", valid_from=_NOW, valid_until=None, source_ref="ref-1", observed_at=later, verified_at=later,
    )

    assert len(second_set) == 1  # No duplicate.
    assert second_membership.membership_id == first_membership.membership_id
    assert second_membership.verified_at == later  # Refreshed.


def test_reobservation_with_no_valid_from_uses_stable_first_observed_timestamp() -> None:
    first_set, first_membership = observe_membership(
        (), provider="ado", tenant_id=None, person_entity_id="person:1", team_entity_id="team:1",
        raw_role="member", valid_from=None, valid_until=None, source_ref=None, observed_at=_NOW, verified_at=_NOW,
    )

    later = _NOW + timedelta(days=5)
    second_set, second_membership = observe_membership(
        first_set, provider="ado", tenant_id=None, person_entity_id="person:1", team_entity_id="team:1",
        raw_role="member", valid_from=None, valid_until=None, source_ref=None, observed_at=later, verified_at=later,
    )

    assert len(second_set) == 1
    assert second_membership.membership_id == first_membership.membership_id
    assert second_membership.observed_at == _NOW  # Stable first-observed timestamp preserved.


def test_different_role_produces_a_distinct_membership() -> None:
    first_set, first_membership = observe_membership(
        (), provider="ado", tenant_id=None, person_entity_id="person:1", team_entity_id="team:1",
        raw_role="member", valid_from=_NOW, valid_until=None, source_ref=None, observed_at=_NOW, verified_at=_NOW,
    )
    second_set, second_membership = observe_membership(
        first_set, provider="ado", tenant_id=None, person_entity_id="person:1", team_entity_id="team:1",
        raw_role="lead", valid_from=_NOW, valid_until=None, source_ref=None, observed_at=_NOW, verified_at=_NOW,
    )

    assert len(second_set) == 2
    assert first_membership.membership_id != second_membership.membership_id


def test_write_then_load_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "memberships.yaml"
    _, membership = observe_membership(
        (), provider="ado", tenant_id=None, person_entity_id="person:1", team_entity_id="team:1",
        raw_role="member", valid_from=_NOW, valid_until=None, source_ref=None, observed_at=_NOW, verified_at=_NOW,
    )

    write_memberships(path, (membership,))
    reloaded = load_memberships(path)

    assert reloaded == (membership,)


def test_archive_moves_expired_memberships_out_of_the_hot_file(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    long_ago_end = _NOW - timedelta(days=DEFAULT_HOT_WINDOW_DAYS + 30)
    recent_end = _NOW - timedelta(days=5)
    expired = observe_membership(
        (), provider="ado", tenant_id=None, person_entity_id="person:1", team_entity_id="team:1",
        raw_role="member", valid_from=_NOW - timedelta(days=400), valid_until=long_ago_end,
        source_ref=None, observed_at=_NOW, verified_at=_NOW,
    )[1]
    still_hot = observe_membership(
        (), provider="ado", tenant_id=None, person_entity_id="person:2", team_entity_id="team:1",
        raw_role="member", valid_from=_NOW - timedelta(days=10), valid_until=recent_end,
        source_ref=None, observed_at=_NOW, verified_at=_NOW,
    )[1]
    write_memberships(memberships_path(knowledge_root), (expired, still_hot))

    result = archive_expired_memberships(knowledge_root, as_of=_NOW)

    assert result.archived_count == 1
    assert result.remaining_hot_count == 1
    remaining = load_memberships(memberships_path(knowledge_root))
    assert remaining == (still_hot,)


def test_archiving_never_loses_a_record(tmp_path: Path) -> None:
    # §7.6: "archiving never deletes the only historical record."
    knowledge_root = tmp_path / "knowledge"
    long_ago_end = _NOW - timedelta(days=DEFAULT_HOT_WINDOW_DAYS + 30)
    expired = observe_membership(
        (), provider="ado", tenant_id=None, person_entity_id="person:1", team_entity_id="team:1",
        raw_role="member", valid_from=_NOW - timedelta(days=400), valid_until=long_ago_end,
        source_ref=None, observed_at=_NOW, verified_at=_NOW,
    )[1]
    write_memberships(memberships_path(knowledge_root), (expired,))

    archive_expired_memberships(knowledge_root, as_of=_NOW)

    all_records = read_all_memberships(knowledge_root)
    assert all_records == (expired,)


def test_archive_is_a_no_op_when_nothing_is_expired(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    still_hot = observe_membership(
        (), provider="ado", tenant_id=None, person_entity_id="person:1", team_entity_id="team:1",
        raw_role="member", valid_from=_NOW, valid_until=None, source_ref=None, observed_at=_NOW, verified_at=_NOW,
    )[1]
    write_memberships(memberships_path(knowledge_root), (still_hot,))

    result = archive_expired_memberships(knowledge_root, as_of=_NOW)

    assert result.archived_count == 0
    assert load_memberships(memberships_path(knowledge_root)) == (still_hot,)


def test_as_of_reads_hot_plus_archived_correctly(tmp_path: Path) -> None:
    # specs/people.md §9.1's exact PPL-W2A.3 verification.
    knowledge_root = tmp_path / "knowledge"
    long_ago_start = _NOW - timedelta(days=500)
    long_ago_end = _NOW - timedelta(days=DEFAULT_HOT_WINDOW_DAYS + 30)
    expired = observe_membership(
        (), provider="ado", tenant_id=None, person_entity_id="person:1", team_entity_id="team:1",
        raw_role="member", valid_from=long_ago_start, valid_until=long_ago_end,
        source_ref=None, observed_at=_NOW, verified_at=_NOW,
    )[1]
    still_hot = observe_membership(
        (), provider="ado", tenant_id=None, person_entity_id="person:2", team_entity_id="team:1",
        raw_role="member", valid_from=_NOW - timedelta(days=10), valid_until=None,
        source_ref=None, observed_at=_NOW, verified_at=_NOW,
    )[1]
    write_memberships(memberships_path(knowledge_root), (expired, still_hot))
    archive_expired_memberships(knowledge_root, as_of=_NOW)  # expired moves to the archive.

    # An as-of query during the expired membership's validity window must still find it
    # (in the archive) even though it's no longer in the hot file.
    as_of_during_expired = read_memberships_as_of(knowledge_root, as_of=long_ago_start + timedelta(days=10))
    assert expired in as_of_during_expired
    assert still_hot not in as_of_during_expired  # Not yet valid at that time.

    as_of_now = read_memberships_as_of(knowledge_root, as_of=_NOW)
    assert still_hot in as_of_now
    assert expired not in as_of_now  # No longer valid.


def test_read_all_memberships_with_no_files_returns_empty(tmp_path: Path) -> None:
    assert read_all_memberships(tmp_path / "knowledge") == ()
