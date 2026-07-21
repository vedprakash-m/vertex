"""specs/people.md Phase 2a, PPL-W2A.3: `memberships.yaml` schema 1.0 +
hot/cold partitioning.

§7.2's exact binding `TeamMembership` dataclass, verified to parse the
real `knowledge/memberships.example.yaml` fixture (a Phase 0a artifact
predating this code).

§7.2's exact `membership_id` rule: "a deterministic `membership:<sha256>`
over `(provider, tenant_id, person_entity_id, team_entity_id,
normalized_role, valid_from_or_first_observed)` so identical observations
are idempotent. A provider that cannot supply `valid_from` uses a stable
first-observed timestamp recorded by the canonical writer." `provider`/
`tenant_id` are hash inputs only -- `TeamMembership` itself has no
`tenant_id` field (only `source`/`source_ref`, matching the binding
dataclass exactly); `provider` is stored as `source`.

§7.6's hot/cold rule: "Active and recently ended memberships remain in
`memberships.yaml`. Expired relations older than the configured
hot-history window are moved, through the canonical writer, into
immutable `knowledge/_journal/memberships_archive/<year>.jsonl`
segments. `--as-of` queries read hot plus archived relations; archiving
never deletes the only historical record." Archival appends (via the
sanctioned `jsonl_utils` seam, never raw `open()`/`json.loads()`) rather
than rewrites, bucketed by the year the relation's `valid_until` falls
in -- never by sequence number the way the change journal (PPL-W1.7)
rotates, since there is no ongoing single-stream hash-chain here.

No governed role-vocabulary mapping table exists yet in this codebase
(that lives in policy files this feature's later phases populate) --
`normalize_role` implements §7.2's fallback rule literally ("unknown raw
roles remain in observation provenance and map to `provider::<value>` or
`unknown`") with a minimal built-in set of common role tokens that pass
through unchanged, rather than inventing a governed table that doesn't
exist.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import yaml

from src.core.exceptions import ConfigError
from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records
from src.core.yaml_utils import fast_safe_load

MEMBERSHIPS_SCHEMA_VERSION = "1.0"
MEMBERSHIPS_ARCHIVE_SCHEMA_VERSION = "1.0"

#: §12.2's binding decision doesn't fix this exact number; v1 default,
#: documented here as the value subject to future policy configuration.
DEFAULT_HOT_WINDOW_DAYS = 180

#: Minimal built-in pass-through role vocabulary. Not a governed mapping
#: table (none exists yet) -- anything outside this set becomes
#: `provider::<value>` per §7.2's own fallback rule.
_KNOWN_ROLE_TOKENS = frozenset({"member", "lead", "owner"})


class MembershipStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    TOMBSTONED = "tombstoned"


@dataclass(frozen=True, slots=True)
class TeamMembership:
    membership_id: str
    person_entity_id: str
    team_entity_id: str
    role: str | None
    valid_from: datetime | None
    valid_until: datetime | None
    source: str
    source_ref: str | None
    observed_at: datetime
    verified_at: datetime
    status: MembershipStatus = MembershipStatus.ACTIVE


def normalize_role(raw_role: str | None, *, provider: str) -> str:
    if raw_role is None or not raw_role.strip():
        return "unknown"
    normalized = raw_role.strip().casefold()
    if normalized in _KNOWN_ROLE_TOKENS:
        return normalized
    return f"{provider}::{normalized}"


def compute_membership_id(
    *,
    provider: str,
    tenant_id: str | None,
    person_entity_id: str,
    team_entity_id: str,
    role: str | None,
    valid_from_or_first_observed: datetime,
) -> str:
    """§7.2's exact formula. `role` should already be the NORMALIZED role
    (via `normalize_role`) -- callers that pass a raw provider role get a
    different hash than the canonical one, breaking idempotency."""
    parts = "|".join(
        (
            provider,
            tenant_id or "",
            person_entity_id,
            team_entity_id,
            role or "unknown",
            valid_from_or_first_observed.astimezone(timezone.utc).isoformat(),
        )
    )
    digest = hashlib.sha256(parts.encode("utf-8")).hexdigest()
    return f"membership:{digest}"


def _parse_datetime(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _parse_optional_datetime(value: object) -> datetime | None:
    return None if value is None else _parse_datetime(value)


def _wire_optional_datetime(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _membership_from_payload(raw: dict) -> TeamMembership:
    return TeamMembership(
        membership_id=str(raw["membership_id"]),
        person_entity_id=str(raw["person_entity_id"]),
        team_entity_id=str(raw["team_entity_id"]),
        role=raw.get("role"),
        valid_from=_parse_optional_datetime(raw.get("valid_from")),
        valid_until=_parse_optional_datetime(raw.get("valid_until")),
        source=str(raw["source"]),
        source_ref=raw.get("source_ref"),
        observed_at=_parse_datetime(raw["observed_at"]),
        verified_at=_parse_datetime(raw["verified_at"]),
        status=MembershipStatus(raw.get("status", "active")),
    )


def membership_to_payload(membership: TeamMembership) -> dict:
    return {
        "membership_id": membership.membership_id,
        "person_entity_id": membership.person_entity_id,
        "team_entity_id": membership.team_entity_id,
        "role": membership.role,
        "valid_from": _wire_optional_datetime(membership.valid_from),
        "valid_until": _wire_optional_datetime(membership.valid_until),
        "status": membership.status.value,
        "source": membership.source,
        "source_ref": membership.source_ref,
        "observed_at": membership.observed_at.astimezone(timezone.utc).isoformat(),
        "verified_at": membership.verified_at.astimezone(timezone.utc).isoformat(),
    }


def memberships_path(knowledge_root: Path) -> Path:
    return knowledge_root / "memberships.yaml"


def memberships_archive_path(knowledge_root: Path, year: int) -> Path:
    return knowledge_root / "_journal" / "memberships_archive" / f"{year}.jsonl"


def load_memberships(path: Path) -> tuple[TeamMembership, ...]:
    if not path.exists():
        return ()
    raw = fast_safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    raw_memberships = raw.get("memberships") or []
    if not isinstance(raw_memberships, list):
        raise ConfigError(f"{path}: 'memberships' must be a list")
    return tuple(_membership_from_payload(m) for m in raw_memberships)


def write_memberships(path: Path, memberships: tuple[TeamMembership, ...]) -> None:
    payload = {
        "schema_version": MEMBERSHIPS_SCHEMA_VERSION,
        "memberships": [membership_to_payload(m) for m in sorted(memberships, key=lambda m: m.membership_id)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False, default_flow_style=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def observe_membership(
    hot_memberships: tuple[TeamMembership, ...],
    *,
    provider: str,
    tenant_id: str | None,
    person_entity_id: str,
    team_entity_id: str,
    raw_role: str | None,
    valid_from: datetime | None,
    valid_until: datetime | None,
    source_ref: str | None,
    observed_at: datetime,
    verified_at: datetime,
    status: MembershipStatus = MembershipStatus.ACTIVE,
) -> tuple[tuple[TeamMembership, ...], TeamMembership]:
    """§9.1's exact PPL-W2A.3 verification: "Idempotent re-observation
    produces no duplicate membership." Re-observing the SAME
    (provider, tenant_id, person, team, role, valid_from-or-first-observed)
    tuple always recomputes the SAME `membership_id` and replaces the
    existing record in place (refreshing `verified_at`/`valid_until`/
    `status`) rather than appending a second, duplicate entry. "A
    provider that cannot supply `valid_from` uses a stable first-observed
    timestamp" -- when `valid_from` is None, the FIRST observation's
    `observed_at` becomes that stable timestamp for every subsequent
    re-observation of the same relation (looked up by every other hash
    component matching an existing hot record)."""
    normalized_role = normalize_role(raw_role, provider=provider)

    hash_basis_time = valid_from
    if hash_basis_time is None:
        existing_same_relation = next(
            (
                m
                for m in hot_memberships
                if m.source == provider
                and m.person_entity_id == person_entity_id
                and m.team_entity_id == team_entity_id
                and m.role == normalized_role
                and m.valid_from is None
            ),
            None,
        )
        hash_basis_time = existing_same_relation.observed_at if existing_same_relation is not None else observed_at

    membership_id = compute_membership_id(
        provider=provider,
        tenant_id=tenant_id,
        person_entity_id=person_entity_id,
        team_entity_id=team_entity_id,
        role=normalized_role,
        valid_from_or_first_observed=hash_basis_time,
    )
    # Preserve the ORIGINAL observed_at (the stable first-observed
    # timestamp the hash was built from) across re-observations, not the
    # latest one -- only verified_at/valid_until/status/source_ref refresh.
    existing_by_id = next((m for m in hot_memberships if m.membership_id == membership_id), None)
    resolved_observed_at = existing_by_id.observed_at if existing_by_id is not None else observed_at

    membership = TeamMembership(
        membership_id=membership_id,
        person_entity_id=person_entity_id,
        team_entity_id=team_entity_id,
        role=normalized_role,
        valid_from=valid_from,
        valid_until=valid_until,
        source=provider,
        source_ref=source_ref,
        observed_at=resolved_observed_at,
        verified_at=verified_at,
        status=status,
    )
    if existing_by_id is not None:
        updated = tuple(membership if m.membership_id == membership_id else m for m in hot_memberships)
    else:
        updated = (*hot_memberships, membership)
    return updated, membership


@dataclass(frozen=True, slots=True)
class MembershipArchiveResult:
    archived_count: int
    remaining_hot_count: int


def archive_expired_memberships(
    knowledge_root: Path,
    *,
    as_of: datetime | None = None,
    hot_window_days: int = DEFAULT_HOT_WINDOW_DAYS,
) -> MembershipArchiveResult:
    """§7.6: expired relations older than the hot-history window move
    (never copy-and-leave, never delete) from `memberships.yaml` into
    `_journal/memberships_archive/<year>.jsonl`, bucketed by the year
    `valid_until` falls in. "Archiving never deletes the only historical
    record" -- verified by tests asserting the hot+archived total record
    count is invariant across an archive pass."""
    now = as_of or datetime.now(timezone.utc)
    cutoff = now.timestamp() - hot_window_days * 86400
    hot = load_memberships(memberships_path(knowledge_root))

    still_hot: list[TeamMembership] = []
    to_archive: list[TeamMembership] = []
    for membership in hot:
        if membership.valid_until is not None and membership.valid_until.timestamp() < cutoff:
            to_archive.append(membership)
        else:
            still_hot.append(membership)

    if not to_archive:
        return MembershipArchiveResult(archived_count=0, remaining_hot_count=len(still_hot))

    by_year: dict[int, list[TeamMembership]] = {}
    for membership in to_archive:
        year = membership.valid_until.astimezone(timezone.utc).year  # type: ignore[union-attr]
        by_year.setdefault(year, []).append(membership)

    for year, memberships_for_year in by_year.items():
        archive_path = memberships_archive_path(knowledge_root, year)
        for membership in memberships_for_year:
            payload = membership_to_payload(membership)
            payload["schema_version"] = MEMBERSHIPS_ARCHIVE_SCHEMA_VERSION
            append_jsonl_line(archive_path, json.dumps(payload, sort_keys=True) + "\n")

    write_memberships(memberships_path(knowledge_root), tuple(still_hot))
    return MembershipArchiveResult(archived_count=len(to_archive), remaining_hot_count=len(still_hot))


def _list_archive_years(knowledge_root: Path) -> tuple[int, ...]:
    archive_dir = knowledge_root / "_journal" / "memberships_archive"
    if not archive_dir.exists():
        return ()
    years: list[int] = []
    for path in archive_dir.glob("*.jsonl"):
        try:
            years.append(int(path.stem))
        except ValueError:
            continue
    return tuple(sorted(years))


def read_all_memberships(knowledge_root: Path) -> tuple[TeamMembership, ...]:
    """Hot plus every archived segment, for callers that need the full
    historical set regardless of a specific as-of time."""
    records = list(load_memberships(memberships_path(knowledge_root)))
    for year in _list_archive_years(knowledge_root):
        for raw in read_jsonl_records(memberships_archive_path(knowledge_root, year)):
            records.append(_membership_from_payload(raw))
    return tuple(records)


def read_memberships_as_of(knowledge_root: Path, *, as_of: datetime) -> tuple[TeamMembership, ...]:
    """§7.6: "`--as-of` queries read hot plus archived relations." Returns
    every membership whose validity interval covers `as_of`, regardless
    of whether it currently lives in the hot file or an archived segment."""
    return tuple(
        membership
        for membership in read_all_memberships(knowledge_root)
        if membership.status is MembershipStatus.ACTIVE
        if (membership.valid_from is None or membership.valid_from <= as_of)
        and (membership.valid_until is None or membership.valid_until >= as_of)
    )
