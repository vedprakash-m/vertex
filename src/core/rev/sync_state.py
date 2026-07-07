"""REV delta sync-state store (Zone A) — FR-PCI-4.

specs/program-context-intelligence.md §5.5. Per-provider sync state is keyed by
``(tenant_id, principal_mailbox, container, api_version)`` and stores a
``deltaLink`` plus program-routing watermarks. Each program has its own routing
watermark against a shared mailbox/calendar/drive sync.

**Eviction policy (§5.5)**:
- **TTL eviction**: dormant states (no access for ``eviction_ttl_days``, default
  30) are evicted on the next write sweep.
- **LRU eviction**: when the store exceeds ``max_states``, the least-recently-
  used entries are evicted first.
Both prevent unbounded growth across programs and users.

**Invalidation precedence (§5.10 GLM)**:
  1. ``api_version`` bump → full resync + drop hydrated cache (highest).
  2. Token expiry / wrong-account → full resync, keep vaulted excerpts where
     ``etag`` still matches.
  3. Query-hash change → re-enumerate, preserving the hydrated cache where
     ``etag`` matches (delta is query-independent).

**Tombstone cascade (§5.5)**: when a delta tombstone marks an item deleted in
M365, the caller is responsible for evicting the extraction-cache entry for its
``canonical_id`` — the ``changes`` result surfaces tombstones via the
``deleted`` flag on returned ``EnumeratedCandidate`` records.

The store is serialized as a single JSON file per program under
``programs/<prog>/rev_sync_states.json``. Concurrent multi-process writes are
not needed in the P2 single-process model; for P3 fleet use, add a file lock.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.core.ledger.candidate_store import PROGRAMS_ROOT, get_candidate_dir

log = logging.getLogger(__name__)

SYNC_STATE_SCHEMA_VERSION = "sync_state.v1"
SYNC_STATE_DEFAULT_TTL_DAYS = 30
SYNC_STATE_DEFAULT_MAX_STATES = 500    # LRU ceiling per program


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class SyncState:
    """Per-container sync state for one delta feed.

    ``key`` is the canonical ``SyncStateKey`` serialized as a string.
    ``delta_link`` is the Graph ``deltaLink`` URL to resume from (None = full
    resync required). ``program_watermarks`` maps ``program_id`` to the last
    event timestamp the program has processed up to (used for per-program
    routing without duplicating the shared delta). ``accessed_at`` drives LRU
    eviction; ``created_at`` is immutable for audit.
    """

    key: str
    tenant_id: str
    principal_mailbox: str
    container: str          # folder_id / calendar_id / drive_id
    api_version: str        # "v1.0" | "beta" etc. — bump triggers full resync
    delta_link: str | None
    program_watermarks: dict[str, str] = field(default_factory=dict)  # program_id → ISO timestamp
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    accessed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_page_token: str | None = None  # in-flight paging checkpoint

    def touch(self) -> None:
        self.accessed_at = datetime.now(timezone.utc)

    def is_dormant(self, *, ttl_days: int = SYNC_STATE_DEFAULT_TTL_DAYS) -> bool:
        age = datetime.now(timezone.utc) - self.accessed_at
        return age > timedelta(days=ttl_days)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SYNC_STATE_SCHEMA_VERSION,
            "key": self.key,
            "tenant_id": self.tenant_id,
            "principal_mailbox": self.principal_mailbox,
            "container": self.container,
            "api_version": self.api_version,
            "delta_link": self.delta_link,
            "program_watermarks": dict(self.program_watermarks),
            "created_at": self.created_at.isoformat(),
            "accessed_at": self.accessed_at.isoformat(),
            "last_page_token": self.last_page_token,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SyncState":
        return cls(
            key=str(d["key"]),
            tenant_id=str(d["tenant_id"]),
            principal_mailbox=str(d["principal_mailbox"]),
            container=str(d["container"]),
            api_version=str(d["api_version"]),
            delta_link=d.get("delta_link"),
            program_watermarks=dict(d.get("program_watermarks", {})),
            created_at=datetime.fromisoformat(str(d["created_at"])).astimezone(timezone.utc),
            accessed_at=datetime.fromisoformat(str(d["accessed_at"])).astimezone(timezone.utc),
            last_page_token=d.get("last_page_token"),
        )


def make_sync_state_key(
    tenant_id: str,
    principal_mailbox: str,
    container: str,
    api_version: str,
) -> str:
    """Canonical key for a sync-state entry."""
    return f"{tenant_id}|{principal_mailbox}|{container}|{api_version}"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _store_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_candidate_dir(program_id, programs_root=programs_root) / "rev_sync_states.json"


class SyncStateStore:
    """TTL + LRU-evicting delta-sync-state store (§5.5).

    Usage::

        store = SyncStateStore.load(program_id, programs_root=...)
        state = store.get_or_create(tenant_id, mailbox, container, api_version)
        state.delta_link = new_link
        state.touch()
        store.upsert(state)
        store.save(program_id, programs_root=...)

    Eviction runs automatically on ``save`` — dormant entries (beyond TTL) and
    entries beyond the LRU ceiling are pruned before writing.
    """

    def __init__(
        self,
        states: dict[str, SyncState] | None = None,
        *,
        eviction_ttl_days: int = SYNC_STATE_DEFAULT_TTL_DAYS,
        max_states: int = SYNC_STATE_DEFAULT_MAX_STATES,
    ) -> None:
        self._states: dict[str, SyncState] = states or {}
        self.eviction_ttl_days = eviction_ttl_days
        self.max_states = max_states

    @classmethod
    def load(
        cls,
        program_id: str,
        *,
        programs_root: Path = PROGRAMS_ROOT,
        eviction_ttl_days: int = SYNC_STATE_DEFAULT_TTL_DAYS,
        max_states: int = SYNC_STATE_DEFAULT_MAX_STATES,
    ) -> "SyncStateStore":
        path = _store_path(program_id, programs_root=programs_root)
        if not path.exists():
            return cls(eviction_ttl_days=eviction_ttl_days, max_states=max_states)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("sync_state.load failed for program %s: %s", program_id, exc)
            return cls(eviction_ttl_days=eviction_ttl_days, max_states=max_states)
        states: dict[str, SyncState] = {}
        for entry in raw.get("states", []):
            try:
                state = SyncState.from_dict(entry)
                states[state.key] = state
            except (KeyError, ValueError) as exc:
                log.warning("sync_state.load: skipping malformed entry: %s", exc)
        return cls(states, eviction_ttl_days=eviction_ttl_days, max_states=max_states)

    def save(
        self,
        program_id: str,
        *,
        programs_root: Path = PROGRAMS_ROOT,
    ) -> None:
        """Evict dormant/LRU entries, then persist."""
        self._evict()
        path = _store_path(program_id, programs_root=programs_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema_version": SYNC_STATE_SCHEMA_VERSION,
            "states": [s.to_dict() for s in self._states.values()],
        }
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def get(self, key: str) -> SyncState | None:
        return self._states.get(key)

    def get_or_create(
        self,
        tenant_id: str,
        principal_mailbox: str,
        container: str,
        api_version: str,
    ) -> SyncState:
        key = make_sync_state_key(tenant_id, principal_mailbox, container, api_version)
        if key not in self._states:
            self._states[key] = SyncState(
                key=key,
                tenant_id=tenant_id,
                principal_mailbox=principal_mailbox,
                container=container,
                api_version=api_version,
                delta_link=None,
            )
        state = self._states[key]
        state.touch()
        return state

    def upsert(self, state: SyncState) -> None:
        self._states[state.key] = state

    def delete(self, key: str) -> None:
        self._states.pop(key, None)

    def all_states(self) -> tuple[SyncState, ...]:
        return tuple(self._states.values())

    def invalidate_for_api_version_change(
        self,
        tenant_id: str,
        principal_mailbox: str,
        container: str,
        old_api_version: str,
        new_api_version: str,
    ) -> None:
        """Full resync on apiVersion bump — highest invalidation precedence (§5.10).

        Deletes the old-version state (so the pipeline re-enumerates from scratch)
        and creates a fresh state for the new version with no delta_link.
        """
        old_key = make_sync_state_key(tenant_id, principal_mailbox, container, old_api_version)
        self.delete(old_key)
        # Fresh state: no delta_link = full resync on next ChangeFeed call.
        new_state = SyncState(
            key=make_sync_state_key(tenant_id, principal_mailbox, container, new_api_version),
            tenant_id=tenant_id,
            principal_mailbox=principal_mailbox,
            container=container,
            api_version=new_api_version,
            delta_link=None,
        )
        self.upsert(new_state)

    def invalidate_for_token_expiry(
        self,
        tenant_id: str,
        principal_mailbox: str,
    ) -> None:
        """Full resync on token expiry / wrong-account — preserves vaulted excerpts (§5.10)."""
        to_delete = [
            k for k, s in self._states.items()
            if s.tenant_id == tenant_id and s.principal_mailbox == principal_mailbox
        ]
        for k in to_delete:
            # Clear delta_link to force full resync; preserve program_watermarks.
            state = self._states[k]
            self._states[k] = SyncState(
                key=state.key,
                tenant_id=state.tenant_id,
                principal_mailbox=state.principal_mailbox,
                container=state.container,
                api_version=state.api_version,
                delta_link=None,                    # force resync
                program_watermarks=state.program_watermarks,
                created_at=state.created_at,
                accessed_at=datetime.now(timezone.utc),
            )

    def _evict(self) -> None:
        """TTL eviction then LRU eviction (§5.5)."""
        # TTL pass — remove dormant states.
        dormant = [k for k, s in self._states.items() if s.is_dormant(ttl_days=self.eviction_ttl_days)]
        for k in dormant:
            log.debug("sync_state.evict ttl key=%s", k)
            del self._states[k]

        # LRU pass — if still over ceiling, remove least-recently-accessed.
        while len(self._states) > self.max_states:
            lru_key = min(self._states, key=lambda k: self._states[k].accessed_at)
            log.debug("sync_state.evict lru key=%s", lru_key)
            del self._states[lru_key]

    def stats(self) -> dict[str, Any]:
        """Summary stats for doctor --rev-health."""
        dormant = sum(1 for s in self._states.values() if s.is_dormant(ttl_days=self.eviction_ttl_days))
        return {
            "total_states": len(self._states),
            "dormant_states": dormant,
            "eviction_ttl_days": self.eviction_ttl_days,
            "max_states": self.max_states,
        }


__all__ = [
    "SyncState",
    "SyncStateStore",
    "make_sync_state_key",
    "SYNC_STATE_SCHEMA_VERSION",
    "SYNC_STATE_DEFAULT_TTL_DAYS",
    "SYNC_STATE_DEFAULT_MAX_STATES",
]
