from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.core.integration_types import RegistryFeedbackEvent

from src.core.fs_utils import _is_network_filesystem_path
from src.core.integration_types import (
    ChannelRegistration,
    DiscoveredRef,
    DiscoveryCompleteness,
    DiscoveryResult,
    RegistryDelta,
    RegistrationBinding,
    RegistrationStatus,
    ScopeState,
    ScopeStatus,
    ScopeStatusKind,
)


SCHEMA_VERSION = "2"
_AUTO_UPGRADABLE_SCHEMA_VERSIONS = frozenset({"1", "2"})
STALE_BACKOFF_HOURS = 2
DELTA_HISTORY_MAX_ROWS = 1000
DELTA_HISTORY_RETENTION_DAYS = 30
_LIVE_BINDING_STATUSES = (
    RegistrationStatus.ACTIVE.value,
    RegistrationStatus.STALE.value,
    RegistrationStatus.EXPIRED.value,
)

_REFID_VALIDATORS: dict[str, re.Pattern[str]] = {
    "work_item": re.compile(r"^\d{1,10}$"),
    "thread": re.compile(r"^[a-zA-Z0-9_\-:@.]{1,512}$"),
    "meeting_series": re.compile(r"^[a-zA-Z0-9_\-:@.]{1,512}$"),
    "kusto_query": re.compile(r"^[a-zA-Z0-9_\-]{1,256}$"),
    "kusto_table": re.compile(r"^[a-zA-Z0-9_.]{1,256}$"),
    "incident": re.compile(r"^\d{1,12}$"),
}


class RegistryError(Exception):
    pass


class RegistryMetadataError(RegistryError):
    pass


class SchemaVersionError(RegistryError):
    pass


class RegistryConcurrencyError(RegistryError):
    def __init__(self, channel: str, original: Exception):
        super().__init__(f"Registry locked by another gather process for channel '{channel}'. Ensure only one gather runs per program.")
        self.channel = channel
        self.original = original


class ShrinkageGuardError(RegistryError):
    def __init__(self, shrinkage_pct: float, computed_delta: RegistryDelta):
        super().__init__(f"Registry shrinkage guard triggered: {shrinkage_pct:.0%}")
        self.shrinkage_pct = shrinkage_pct
        self.computed_delta = computed_delta


def compute_registry_delta(previous_refs: tuple[DiscoveredRef, ...], result: DiscoveryResult) -> RegistryDelta:
    result = normalize_discovery_result_provider_instance(result)
    previous_by_key = {_ref_key(ref.registration): ref for ref in previous_refs}
    current_by_key = {_ref_key(ref.registration): ref for ref in result.discovered_refs}
    added_keys = tuple(key for key in current_by_key if key not in previous_by_key)
    removed_keys: tuple[tuple[str, str, str, str, str], ...] = ()
    if result.completeness is DiscoveryCompleteness.FULL:
        removed_keys = tuple(key for key in previous_by_key if key not in current_by_key)
    elif result.completeness is DiscoveryCompleteness.PARTIAL:
        successful_full_scopes = {
            scope_id
            for scope_id, status in result.scope_statuses.items()
            if status.status is ScopeStatusKind.SUCCESS and status.completeness is DiscoveryCompleteness.FULL
        }
        removed: list[tuple[str, str, str, str, str]] = []
        for key, previous in previous_by_key.items():
            if key in current_by_key:
                continue
            bindings = previous.bindings
            if bindings and all(binding.scope_id in successful_full_scopes for binding in bindings):
                removed.append(key)
        removed_keys = tuple(removed)

    updated_keys = tuple(
        key
        for key in current_by_key.keys() & previous_by_key.keys()
        if _comparison_projection(current_by_key[key]) != _comparison_projection(previous_by_key[key])
    )
    unchanged_count = len(current_by_key.keys() & previous_by_key.keys()) - len(updated_keys)
    previous_active_count = len(previous_by_key)
    shrinkage_pct = (len(removed_keys) / previous_active_count) if previous_active_count else 0.0
    failed_scopes = {
        scope_id: status
        for scope_id, status in result.scope_statuses.items()
        if status.status is not ScopeStatusKind.SUCCESS
    }
    previous_discovery_at = max((ref.registration.last_seen_at for ref in previous_refs), default=None)
    return RegistryDelta(
        channel=result.channel,
        program_id=result.program_id,
        computed_at=result.computed_at,
        previous_discovery_at=previous_discovery_at,
        completeness=result.completeness,
        added=tuple(current_by_key[key].registration for key in added_keys),
        removed=tuple(previous_by_key[key].registration for key in removed_keys),
        updated=tuple(current_by_key[key].registration for key in updated_keys),
        unchanged_count=max(unchanged_count, 0),
        failed_scopes=failed_scopes,
        shrinkage_pct=shrinkage_pct,
    )


class ChannelRegistryStore:
    def __init__(self, db_path: Path, program_id: str, *, ensure_schema: bool = True):
        self.db_path = Path(db_path)
        self.program_id = program_id
        if ensure_schema:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.ensure_schema()

    def active_registrations(
        self,
        channel: str,
        *,
        provider_instance_id: str | None = None,
        workstream_id: str | None = None,
        ref_kind: str | None = None,
        status: RegistrationStatus = RegistrationStatus.ACTIVE,
    ) -> tuple[ChannelRegistration, ...]:
        return self._load_registrations(
            channel,
            provider_instance_id=provider_instance_id,
            workstream_id=workstream_id,
            ref_kind=ref_kind,
            statuses=(status,),
        )

    def pullable_registrations(
        self,
        channel: str,
        *,
        provider_instance_id: str | None = None,
        workstream_id: str | None = None,
    ) -> tuple[ChannelRegistration, ...]:
        now = datetime.now(timezone.utc)
        registrations = self._load_registrations(
            channel,
            provider_instance_id=provider_instance_id,
            workstream_id=workstream_id,
            statuses=(RegistrationStatus.ACTIVE, RegistrationStatus.STALE, RegistrationStatus.EXPIRED),
        )
        return tuple(
            registration
            for registration in registrations
            if registration.status is not RegistrationStatus.STALE
            or registration.last_verified_at is None
            or registration.last_verified_at <= now - timedelta(hours=STALE_BACKOFF_HOURS)
        )

    def all_registrations(
        self,
        channel: str,
        *,
        provider_instance_id: str | None = None,
    ) -> tuple[ChannelRegistration, ...]:
        return self._load_registrations(channel, provider_instance_id=provider_instance_id, statuses=None)

    def registered_channels(self) -> tuple[str, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT channel
                FROM registrations
                WHERE program_id = ?
                ORDER BY channel
                """,
                (self.program_id,),
            ).fetchall()
        return tuple(str(row["channel"]) for row in rows)

    def registration_count(
        self,
        channel: str,
        *,
        status: RegistrationStatus = RegistrationStatus.ACTIVE,
        provider_instance_id: str | None = None,
    ) -> int:
        params: list[object] = [channel, self.program_id, status.value]
        provider_clause = ""
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            params.append(provider_instance_id)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM registrations WHERE channel = ? AND program_id = ? AND status = ?{provider_clause}",
                tuple(params),
            ).fetchone()
        return int(row[0] if row is not None else 0)

    def last_discovery_at(self, channel: str, *, provider_instance_id: str | None = None) -> datetime | None:
        params: list[object] = [channel, self.program_id]
        provider_clause = ""
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            params.append(provider_instance_id)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT MAX(last_seen_at) FROM registrations WHERE channel = ? AND program_id = ?{provider_clause}",
                tuple(params),
            ).fetchone()
        return _parse_datetime(row[0]) if row and row[0] else None

    def is_discovery_stale(
        self,
        channel: str,
        threshold_hours: int,
        *,
        provider_instance_id: str | None = None,
    ) -> bool:
        last_seen = self.last_discovery_at(channel, provider_instance_id=provider_instance_id)
        if last_seen is None:
            return True
        return last_seen <= datetime.now(timezone.utc) - timedelta(hours=threshold_hours)

    def get_workstream_map(
        self,
        channel: str,
        ref_pairs: tuple[tuple[str, str], ...],
        *,
        provider_instance_id: str | None = None,
    ) -> dict[tuple[str, str], tuple[str, ...]]:
        if not ref_pairs:
            return {}
        placeholders = ",".join(["(?, ?)"] * len(ref_pairs))
        status_placeholders = ",".join("?" for _ in _LIVE_BINDING_STATUSES)
        params: list[Any] = [channel, self.program_id]
        params.extend(_LIVE_BINDING_STATUSES)
        params.extend(value for pair in ref_pairs for value in pair)
        provider_clause = ""
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            params.append(provider_instance_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT ref_id, ref_kind, workstream_id
                FROM registration_bindings
                WHERE channel = ? AND program_id = ? AND status IN ({status_placeholders})
                  AND (ref_id, ref_kind) IN ({placeholders}){provider_clause}
                ORDER BY ref_id, ref_kind, workstream_id
                """,
                tuple(params),
            ).fetchall()
        mapped: dict[tuple[str, str], list[str]] = {}
        for row in rows:
            if row["workstream_id"] is None:
                continue
            mapped.setdefault((str(row["ref_id"]), str(row["ref_kind"])), []).append(str(row["workstream_id"]))
        return {key: tuple(dict.fromkeys(values)) for key, values in mapped.items()}

    def load_discovered_refs(self, channel: str, *, provider_instance_id: str | None = None) -> tuple[DiscoveredRef, ...]:
        registrations = self._load_registrations(channel, provider_instance_id=provider_instance_id, statuses=(RegistrationStatus.ACTIVE,))
        if not registrations:
            return ()
        bindings_by_key = self._load_bindings(channel, provider_instance_id=provider_instance_id)
        return tuple(
            DiscoveredRef(registration=registration, bindings=bindings_by_key.get(_ref_key(registration), ()))
            for registration in registrations
        )

    def apply_discovery_result(
        self,
        result: DiscoveryResult,
        *,
        ttl_days: int | None = None,
        accept_shrinkage: bool = False,
        shrinkage_threshold_pct: float = 0.30,
        shrinkage_floor: int = 5,
    ) -> RegistryDelta:
        result = normalize_discovery_result_provider_instance(result)
        provider_instance_id = result.provider_instance_id
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                raise RegistryConcurrencyError(result.channel, error) from error
            previous = self._load_discovered_refs_conn(conn, result.channel, provider_instance_id=provider_instance_id)
            delta = compute_registry_delta(previous, result)
            if delta.is_shrinkage_guarded(threshold_pct=shrinkage_threshold_pct, floor=shrinkage_floor) and not accept_shrinkage:
                conn.rollback()
                raise ShrinkageGuardError(delta.shrinkage_pct, delta)
            for discovered_ref in result.discovered_refs:
                self._upsert_discovered_ref(conn, discovered_ref, ttl_days=ttl_days, seen_at=result.computed_at)
            for removed in delta.removed:
                conn.execute(
                    """
                    UPDATE registrations
                    SET status = ?, retired_at = ?
                    WHERE channel = ? AND program_id = ? AND provider_instance_id = ? AND ref_id = ? AND ref_kind = ?
                    """,
                    (
                        RegistrationStatus.RETIRED.value,
                        _format_datetime(result.computed_at),
                        removed.channel,
                        self.program_id,
                        removed.provider_instance_id,
                        removed.ref_id,
                        removed.ref_kind,
                    ),
                )
                conn.execute(
                    """
                    UPDATE registration_bindings
                    SET status = ?, retired_at = ?
                    WHERE channel = ? AND program_id = ? AND provider_instance_id = ? AND ref_id = ? AND ref_kind = ?
                    """,
                    (
                        RegistrationStatus.RETIRED.value,
                        _format_datetime(result.computed_at),
                        removed.channel,
                        self.program_id,
                        removed.provider_instance_id,
                        removed.ref_id,
                        removed.ref_kind,
                    ),
                )
            for scope_id, status in result.scope_statuses.items():
                self._record_scope_status_conn(conn, result.channel, provider_instance_id, scope_id, status, result.computed_at)
            for scope_id, state in result.scope_state_updates.items():
                self._upsert_scope_state(conn, result.channel, provider_instance_id, scope_id, state)
            self._insert_delta(conn, delta, provider_instance_id=provider_instance_id)
            conn.commit()
        return delta

    def ensure_status_transitions(self, channel: str | None = None) -> None:
        now = _current_utc()
        params: list[Any] = [RegistrationStatus.EXPIRED.value, _format_datetime(now), self.program_id, RegistrationStatus.ACTIVE.value]
        channel_clause = ""
        if channel is not None:
            channel_clause = " AND channel = ?"
            params.append(channel)
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE registrations
                SET status = ?
                WHERE expires_at IS NOT NULL AND expires_at < ? AND program_id = ? AND status = ?{channel_clause}
                """,
                tuple(params),
            )
            conn.execute(
                f"""
                UPDATE registration_bindings
                SET status = ?
                WHERE (channel, program_id, provider_instance_id, ref_id, ref_kind) IN (
                    SELECT channel, program_id, provider_instance_id, ref_id, ref_kind
                    FROM registrations
                    WHERE expires_at IS NOT NULL AND expires_at < ? AND program_id = ? AND status = ?{channel_clause}
                )
                """,
                (
                    RegistrationStatus.EXPIRED.value,
                    _format_datetime(now),
                    self.program_id,
                    RegistrationStatus.EXPIRED.value,
                    *((channel,) if channel is not None else ()),
                ),
            )
            retired_params: list[Any] = [RegistrationStatus.RETIRED.value, _format_datetime(now), self.program_id, RegistrationStatus.EXPIRED.value]
            retired_channel_clause = ""
            if channel is not None:
                retired_channel_clause = " AND channel = ?"
                retired_params.append(channel)
            conn.execute(
                f"""
                UPDATE registrations
                SET status = ?, retired_at = ?
                WHERE expires_at IS NOT NULL
                  AND julianday(?) >= julianday(expires_at) + (julianday(expires_at) - julianday(last_seen_at))
                  AND program_id = ? AND status = ?{retired_channel_clause}
                """,
                tuple(retired_params[:2] + retired_params[1:2] + retired_params[2:]),
            )
            conn.execute(
                f"""
                UPDATE registration_bindings
                SET status = ?, retired_at = ?
                WHERE (channel, program_id, provider_instance_id, ref_id, ref_kind) IN (
                    SELECT channel, program_id, provider_instance_id, ref_id, ref_kind
                    FROM registrations
                    WHERE expires_at IS NOT NULL
                      AND julianday(?) >= julianday(expires_at) + (julianday(expires_at) - julianday(last_seen_at))
                      AND program_id = ? AND status = ?{retired_channel_clause}
                )
                """,
                (
                    RegistrationStatus.RETIRED.value,
                    _format_datetime(now),
                    _format_datetime(now),
                    self.program_id,
                    RegistrationStatus.RETIRED.value,
                    *((channel,) if channel is not None else ()),
                ),
            )

    def mark_verified(
        self,
        channel: str,
        ref_id_kind_pairs: tuple[tuple[str, str], ...],
        verified_at: datetime,
        *,
        provider_instance_id: str | None = None,
    ) -> None:
        if not ref_id_kind_pairs:
            return
        provider_clause = ""
        base_values: list[object] = [_format_datetime(verified_at), _format_datetime(verified_at), channel, self.program_id]
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            base_values.append(provider_instance_id)
        with self._connect() as conn:
            conn.executemany(
                f"""
                UPDATE registrations
                SET last_verified_at = ?, last_hydration_attempt_at = ?, last_hydration_error = NULL,
                    consecutive_hydration_failures = 0,
                    status = CASE WHEN status = 'stale' THEN 'active' ELSE status END
                WHERE channel = ? AND program_id = ?{provider_clause} AND ref_id = ? AND ref_kind = ?
                """,
                [tuple(base_values + [ref_id, ref_kind]) for ref_id, ref_kind in ref_id_kind_pairs],
            )
            conn.executemany(
                f"""
                UPDATE registration_bindings
                SET status = CASE WHEN status = 'stale' THEN 'active' ELSE status END
                WHERE channel = ? AND program_id = ?{provider_clause} AND ref_id = ? AND ref_kind = ?
                """,
                [tuple(base_values[2:] + [ref_id, ref_kind]) for ref_id, ref_kind in ref_id_kind_pairs],
            )

    def mark_hydration_failed(
        self,
        channel: str,
        ref_id_kind_pairs: tuple[tuple[str, str], ...],
        *,
        provider_instance_id: str | None = None,
    ) -> None:
        if not ref_id_kind_pairs:
            return
        now = _format_datetime(datetime.now(timezone.utc))
        provider_clause = ""
        base_values: list[object] = [now, channel, self.program_id]
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            base_values.append(provider_instance_id)
        with self._connect() as conn:
            conn.executemany(
                f"""
                UPDATE registrations
                SET last_hydration_attempt_at = ?, consecutive_hydration_failures = consecutive_hydration_failures + 1,
                    status = CASE WHEN consecutive_hydration_failures + 1 >= 3 THEN 'stale' ELSE status END
                WHERE channel = ? AND program_id = ?{provider_clause} AND ref_id = ? AND ref_kind = ?
                """,
                [tuple(base_values + [ref_id, ref_kind]) for ref_id, ref_kind in ref_id_kind_pairs],
            )
            conn.executemany(
                f"""
                UPDATE registration_bindings
                SET status = CASE WHEN (
                        SELECT consecutive_hydration_failures
                        FROM registrations
                        WHERE channel = ? AND program_id = ?{provider_clause} AND ref_id = ? AND ref_kind = ?
                    ) >= 3 THEN 'stale' ELSE status END
                WHERE channel = ? AND program_id = ?{provider_clause} AND ref_id = ? AND ref_kind = ?
                """,
                [
                    tuple(base_values[1:] + [ref_id, ref_kind] + base_values[1:] + [ref_id, ref_kind])
                    for ref_id, ref_kind in ref_id_kind_pairs
                ],
            )

    def retire(
        self,
        channel: str,
        ref_id: str,
        ref_kind: str,
        *,
        provider_instance_id: str | None = None,
    ) -> None:
        self._set_status(channel, ref_id, ref_kind, RegistrationStatus.RETIRED, provider_instance_id=provider_instance_id)

    def suppress(
        self,
        channel: str,
        ref_id: str,
        ref_kind: str,
        *,
        provider_instance_id: str | None = None,
    ) -> None:
        self._set_status(channel, ref_id, ref_kind, RegistrationStatus.SUPPRESSED, provider_instance_id=provider_instance_id)

    def prune_retired(
        self,
        channel: str,
        *,
        older_than_days: int = 90,
        provider_instance_id: str | None = None,
    ) -> int:
        """Delete RETIRED and SUPPRESSED registrations (and their bindings) whose ``retired_at``
        timestamp is older than ``older_than_days``.

        Returns the number of registrations deleted.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).strftime("%Y-%m-%dT%H:%M:%S")
        params: list[object] = [channel, self.program_id, RegistrationStatus.RETIRED.value, RegistrationStatus.SUPPRESSED.value, cutoff]
        provider_clause = ""
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            params.append(provider_instance_id)
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM registrations WHERE channel = ? AND program_id = ? AND status IN (?, ?) AND retired_at < ?{provider_clause}",
                tuple(params),
            )
            return cursor.rowcount

    def write_feedback_event(
        self,
        channel: str,
        ref_id: str,
        ref_kind: str,
        action: str,
        pm_alias: str,
        *,
        provider_instance_id: str = "default",
        reason: str | None = None,
        workstream_id: str | None = None,
        prior_workstream_id: str | None = None,
        series_id: str | None = None,
        thread_id: str | None = None,
        new_artifact_id: str | None = None,
        detail_json: str | None = None,
        created_at: datetime | None = None,
    ) -> None:
        ts = (created_at or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO registry_feedback
                    (channel, program_id, provider_instance_id, ref_id, ref_kind,
                     action, pm_alias, reason, workstream_id, prior_workstream_id,
                     series_id, thread_id, new_artifact_id, detail_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel, self.program_id, provider_instance_id, ref_id, ref_kind,
                    action, pm_alias, reason, workstream_id, prior_workstream_id,
                    series_id, thread_id, new_artifact_id, detail_json, ts,
                ),
            )

    def load_feedback_events(
        self,
        channel: str,
        ref_id: str,
        ref_kind: str,
        *,
        provider_instance_id: str | None = None,
    ) -> tuple["RegistryFeedbackEvent", ...]:
        from src.core.integration_types import RegistryFeedbackEvent  # local import avoids circular
        params: list[object] = [channel, self.program_id, ref_id, ref_kind]
        provider_clause = ""
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            params.append(provider_instance_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT channel, program_id, provider_instance_id, ref_id, ref_kind,
                           action, pm_alias, reason, workstream_id, prior_workstream_id,
                           series_id, thread_id, new_artifact_id, detail_json, created_at
                    FROM registry_feedback
                    WHERE channel = ? AND program_id = ? AND ref_id = ? AND ref_kind = ?{provider_clause}
                    ORDER BY created_at ASC""",
                tuple(params),
            ).fetchall()
        result = []
        for row in rows:
            try:
                ts = datetime.fromisoformat(str(row["created_at"]).rstrip("Z")).replace(tzinfo=timezone.utc)
            except ValueError:
                ts = datetime.now(timezone.utc)
            result.append(
                RegistryFeedbackEvent(
                    channel=str(row["channel"]),
                    program_id=str(row["program_id"]),
                    ref_id=str(row["ref_id"]),
                    ref_kind=str(row["ref_kind"]),
                    action=str(row["action"]),
                    pm_alias=str(row["pm_alias"]),
                    created_at=ts,
                    provider_instance_id=str(row["provider_instance_id"]),
                    reason=str(row["reason"]) if row["reason"] is not None else None,
                    workstream_id=str(row["workstream_id"]) if row["workstream_id"] is not None else None,
                    prior_workstream_id=str(row["prior_workstream_id"]) if row["prior_workstream_id"] is not None else None,
                    series_id=str(row["series_id"]) if row["series_id"] is not None else None,
                    thread_id=str(row["thread_id"]) if row["thread_id"] is not None else None,
                    new_artifact_id=str(row["new_artifact_id"]) if row["new_artifact_id"] is not None else None,
                    detail_json=str(row["detail_json"]) if row["detail_json"] is not None else None,
                )
            )
        return tuple(result)

    def prune_feedback_events(
        self,
        channel: str,
        *,
        older_than_days: int = 180,
        provider_instance_id: str | None = None,
    ) -> int:
        """Delete feedback events older than ``older_than_days``.

        Returns the number of rows deleted.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).strftime("%Y-%m-%dT%H:%M:%S")
        params: list[object] = [channel, self.program_id, cutoff]
        provider_clause = ""
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            params.append(provider_instance_id)
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM registry_feedback WHERE channel = ? AND program_id = ? AND created_at < ?{provider_clause}",
                tuple(params),
            )
            return cursor.rowcount

    def confirm(
        self,
        channel: str,
        ref_id: str,
        ref_kind: str,
        *,
        provider_instance_id: str | None = None,
    ) -> None:
        params: list[object] = [channel, self.program_id, ref_id, ref_kind]
        provider_clause = ""
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            params.append(provider_instance_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE registration_bindings SET pm_confirmed = 1 WHERE channel = ? AND program_id = ? AND ref_id = ? AND ref_kind = ?{provider_clause}",
                tuple(params),
            )
            self._refresh_governance(conn, channel, ref_id, ref_kind, provider_instance_id=provider_instance_id)

    def promote(
        self,
        channel: str,
        ref_id: str,
        ref_kind: str,
        *,
        provider_instance_id: str | None = None,
    ) -> None:
        params: list[object] = [channel, self.program_id, ref_id, ref_kind]
        provider_clause = ""
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            params.append(provider_instance_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE registration_bindings SET promoted = 1 WHERE channel = ? AND program_id = ? AND ref_id = ? AND ref_kind = ?{provider_clause}",
                tuple(params),
            )
            self._refresh_governance(conn, channel, ref_id, ref_kind, provider_instance_id=provider_instance_id)

    def reassign_workstream(
        self,
        channel: str,
        ref_id: str,
        ref_kind: str,
        new_workstream_id: str,
        *,
        old_workstream_id: str | None = None,
        provider_instance_id: str | None = None,
    ) -> int:
        """Reassign workstream attribution for a registration binding.

        Since workstream_id is part of the binding PK, reassignment is implemented as
        DELETE + INSERT. Returns the number of binding rows migrated.

        If old_workstream_id is None, all workstream bindings for this ref are migrated.
        """
        params: list[object] = [channel, self.program_id, ref_id, ref_kind]
        old_ws_clause = ""
        if old_workstream_id is not None:
            old_ws_clause = " AND workstream_id = ?"
            params.append(old_workstream_id)
        provider_clause = ""
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            params.append(provider_instance_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM registration_bindings WHERE channel = ? AND program_id = ? AND ref_id = ? AND ref_kind = ?{old_ws_clause}{provider_clause}",
                tuple(params),
            ).fetchall()
            migrated = 0
            for row in rows:
                if str(row["workstream_id"] or "") == new_workstream_id:
                    continue  # already correct
                conn.execute(
                    """
                    INSERT OR IGNORE INTO registration_bindings
                    (channel, program_id, provider_instance_id, ref_id, ref_kind, workstream_id, scope_id,
                     source_type, status, confidence, confidence_source, pm_confirmed, promoted,
                     signal_yield_0, signal_yield_1, signal_yield_2, first_seen_at, last_seen_at, retired_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["channel"], row["program_id"], row["provider_instance_id"],
                        row["ref_id"], row["ref_kind"], new_workstream_id, row["scope_id"],
                        row["source_type"], row["status"], row["confidence"],
                        row["confidence_source"], row["pm_confirmed"], row["promoted"],
                        row["signal_yield_0"], row["signal_yield_1"], row["signal_yield_2"],
                        row["first_seen_at"], row["last_seen_at"], row["retired_at"],
                    ),
                )
                conn.execute(
                    f"DELETE FROM registration_bindings WHERE channel = ? AND program_id = ? AND provider_instance_id = ? AND ref_id = ? AND ref_kind = ? AND workstream_id = ? AND scope_id = ?",
                    (row["channel"], row["program_id"], row["provider_instance_id"],
                     row["ref_id"], row["ref_kind"], row["workstream_id"], row["scope_id"]),
                )
                migrated += 1
            if migrated > 0:
                self._refresh_governance(conn, channel, ref_id, ref_kind, provider_instance_id=provider_instance_id)
        return migrated

    def reassign_ref_id(
        self,
        channel: str,
        old_ref_id: str,
        new_ref_id: str,
        ref_kind: str,
        *,
        pm_alias: str,
        reason: str | None = None,
        provider_instance_id: str | None = None,
    ) -> int:
        """Migrate a registration to a new ref_id (e.g. after a Teams thread rotation).

        Since ref_id is part of the PK, this is implemented as INSERT-new + DELETE-old
        with all bindings copied in the same transaction.  Raises RegistryMetadataError
        if old_ref_id does not exist or new_ref_id already exists.

        Returns the number of binding rows migrated (plus 1 for the registration row).
        """
        if old_ref_id == new_ref_id:
            return 0
        provider_clause = ""
        provider_params: list[object] = []
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            provider_params.append(provider_instance_id)
        with self._connect() as conn:
            reg_row = conn.execute(
                f"SELECT * FROM registrations WHERE channel = ? AND program_id = ? AND ref_id = ? AND ref_kind = ?{provider_clause}",
                (channel, self.program_id, old_ref_id, ref_kind, *provider_params),
            ).fetchone()
            if reg_row is None:
                raise RegistryMetadataError(
                    f"reassign_ref_id: source not found: channel={channel} ref_id={old_ref_id!r} ref_kind={ref_kind!r}"
                )
            effective_provider = str(reg_row["provider_instance_id"])
            conflict_row = conn.execute(
                "SELECT 1 FROM registrations WHERE channel = ? AND program_id = ? AND provider_instance_id = ? AND ref_id = ? AND ref_kind = ?",
                (channel, self.program_id, effective_provider, new_ref_id, ref_kind),
            ).fetchone()
            if conflict_row is not None:
                raise RegistryMetadataError(
                    f"reassign_ref_id: new_ref_id already exists: channel={channel} ref_id={new_ref_id!r} ref_kind={ref_kind!r}. "
                    "Merge semantics are not supported; retire the conflicting registration first."
                )
            binding_rows = conn.execute(
                f"SELECT * FROM registration_bindings WHERE channel = ? AND program_id = ? AND ref_id = ? AND ref_kind = ?{provider_clause}",
                (channel, self.program_id, old_ref_id, ref_kind, *provider_params),
            ).fetchall()

            # Insert new registration with new ref_id.
            conn.execute(
                """
                INSERT INTO registrations
                    (channel, program_id, provider_instance_id, ref_id, ref_kind, status,
                     first_discovered_at, last_seen_at, last_verified_at, last_hydration_attempt_at,
                     last_hydration_error, expires_at, retired_at, consecutive_hydration_failures,
                     confidence, confidence_source, pm_confirmed, promoted,
                     signal_yield_0, signal_yield_1, signal_yield_2, ref_title, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reg_row["channel"], reg_row["program_id"], reg_row["provider_instance_id"],
                    new_ref_id, reg_row["ref_kind"], reg_row["status"],
                    reg_row["first_discovered_at"], reg_row["last_seen_at"],
                    reg_row["last_verified_at"], reg_row["last_hydration_attempt_at"],
                    reg_row["last_hydration_error"], reg_row["expires_at"], reg_row["retired_at"],
                    reg_row["consecutive_hydration_failures"],
                    reg_row["confidence"], reg_row["confidence_source"],
                    reg_row["pm_confirmed"], reg_row["promoted"],
                    reg_row["signal_yield_0"], reg_row["signal_yield_1"], reg_row["signal_yield_2"],
                    reg_row["ref_title"], reg_row["metadata_json"],
                ),
            )
            # Copy bindings to new ref_id.
            for binding in binding_rows:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO registration_bindings
                        (channel, program_id, provider_instance_id, ref_id, ref_kind, workstream_id, scope_id,
                         source_type, status, confidence, confidence_source, pm_confirmed, promoted,
                         signal_yield_0, signal_yield_1, signal_yield_2, first_seen_at, last_seen_at, retired_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        binding["channel"], binding["program_id"], binding["provider_instance_id"],
                        new_ref_id, binding["ref_kind"], binding["workstream_id"], binding["scope_id"],
                        binding["source_type"], binding["status"], binding["confidence"],
                        binding["confidence_source"], binding["pm_confirmed"], binding["promoted"],
                        binding["signal_yield_0"], binding["signal_yield_1"], binding["signal_yield_2"],
                        binding["first_seen_at"], binding["last_seen_at"], binding["retired_at"],
                    ),
                )
            # Delete old registration (cascades to its bindings via ON DELETE CASCADE).
            conn.execute(
                f"DELETE FROM registrations WHERE channel = ? AND program_id = ? AND ref_id = ? AND ref_kind = ?{provider_clause}",
                (channel, self.program_id, old_ref_id, ref_kind, *provider_params),
            )
            # Record in audit log.
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            import json as _json
            detail = _json.dumps({"old_ref_id": old_ref_id, "new_ref_id": new_ref_id, "bindings_migrated": len(binding_rows)})
            conn.execute(
                """
                INSERT INTO registry_feedback
                    (channel, program_id, provider_instance_id, ref_id, ref_kind,
                     action, pm_alias, reason, new_artifact_id, detail_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (channel, self.program_id, effective_provider, old_ref_id, ref_kind,
                 "set_ref_id", pm_alias, reason, new_ref_id, detail, ts),
            )
        return 1 + len(binding_rows)

    def update_signal_yield(
        self,
        channel: str,
        ref_id: str,
        ref_kind: str,
        yield_count: int,
        *,
        provider_instance_id: str | None = None,
    ) -> None:
        params: list[object] = [yield_count, channel, self.program_id, ref_id, ref_kind]
        provider_clause = ""
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            params.append(provider_instance_id)
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE registration_bindings
                SET signal_yield_2 = signal_yield_1, signal_yield_1 = signal_yield_0, signal_yield_0 = ?
                WHERE channel = ? AND program_id = ? AND ref_id = ? AND ref_kind = ?{provider_clause}
                """,
                tuple(params),
            )
            self._refresh_governance(conn, channel, ref_id, ref_kind, provider_instance_id=provider_instance_id)

    def record_scope_status(
        self,
        channel: str,
        scope_id: str,
        status: ScopeStatus,
        *,
        provider_instance_id: str = "default",
        recorded_at: datetime | None = None,
    ) -> None:
        with self._connect() as conn:
            self._record_scope_status_conn(
                conn,
                channel,
                provider_instance_id,
                scope_id,
                status,
                recorded_at or datetime.now(timezone.utc),
            )

    def consecutive_scope_failures(self, channel: str, scope_id: str, *, provider_instance_id: str | None = None) -> int:
        params: list[Any] = [channel, self.program_id, scope_id]
        provider_clause = ""
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            params.append(provider_instance_id)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT consecutive_failures FROM scope_health WHERE channel = ? AND program_id = ? AND scope_id = ?{provider_clause}",
                tuple(params),
            ).fetchone()
        return int(row["consecutive_failures"]) if row else 0

    def load_scope_state(self, channel: str, *, provider_instance_id: str | None = None) -> dict[str, ScopeState]:
        params: list[Any] = [channel, self.program_id]
        provider_clause = ""
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            params.append(provider_instance_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM scope_state WHERE channel = ? AND program_id = ?{provider_clause}",
                tuple(params),
            ).fetchall()
        return {str(row["scope_id"]): _scope_state_from_row(row) for row in rows}

    def recent_deltas(
        self,
        channel: str,
        *,
        limit: int = 10,
        provider_instance_id: str | None = None,
    ) -> tuple[RegistryDelta, ...]:
        params: list[Any] = [channel, self.program_id]
        provider_clause = ""
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            params.append(provider_instance_id)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM registry_deltas
                WHERE channel = ? AND program_id = ?{provider_clause}
                ORDER BY computed_at DESC, id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return tuple(_delta_from_row(row) for row in rows)

    def recent_scope_health(
        self,
        channel: str,
        *,
        provider_instance_id: str | None = None,
    ) -> dict[str, str]:
        params: list[Any] = [channel, self.program_id]
        provider_clause = ""
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            params.append(provider_instance_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT scope_id, last_status, consecutive_failures
                FROM scope_health
                WHERE channel = ? AND program_id = ?{provider_clause}
                ORDER BY scope_id
                """,
                tuple(params),
            ).fetchall()
        health: dict[str, str] = {}
        for row in rows:
            last_status = str(row["last_status"])
            if last_status == ScopeStatusKind.SUCCESS.value:
                health[str(row["scope_id"])] = "ok"
                continue
            health[str(row["scope_id"])] = f"{last_status}_{int(row['consecutive_failures'])}x"
        return health

    def ensure_schema(self) -> None:
        version_error: str | None = None
        with self._connect(init=True) as conn:
            conn.executescript(_SCHEMA_SQL)
            _ensure_registrations_additive_columns(conn)
            version = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
            if version is not None:
                stored_version = str(version["value"])
                if stored_version != SCHEMA_VERSION and stored_version not in _AUTO_UPGRADABLE_SCHEMA_VERSIONS:
                    has_data = conn.execute("SELECT COUNT(*) FROM registrations").fetchone()[0] > 0
                    if has_data:
                        version_error = stored_version
            if version_error is None:
                conn.execute(
                    "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
        # Explicitly close to release Windows file lock before raising, so callers that
        # catch SchemaVersionError can delete/replace the file (e.g. schema-migrate --force).
        conn.close()
        if version_error is not None:
            raise SchemaVersionError(f"Unsupported channel registry schema version {version_error}; run `vertex integration schema-migrate --program <id>`.")

    def _connect(self, *, init: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, detect_types=0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        journal_mode = "DELETE" if _is_network_filesystem_path(self.db_path) else "WAL"
        conn.execute(f"PRAGMA journal_mode = {journal_mode}")
        return conn

    def _load_registrations(
        self,
        channel: str,
        *,
        provider_instance_id: str | None = None,
        workstream_id: str | None = None,
        ref_kind: str | None = None,
        statuses: tuple[RegistrationStatus, ...] | None,
    ) -> tuple[ChannelRegistration, ...]:
        with self._connect() as conn:
            return self._load_registrations_conn(
                conn,
                channel,
                provider_instance_id=provider_instance_id,
                workstream_id=workstream_id,
                ref_kind=ref_kind,
                statuses=statuses,
            )

    def _load_registrations_conn(
        self,
        conn: sqlite3.Connection,
        channel: str,
        *,
        provider_instance_id: str | None = None,
        workstream_id: str | None = None,
        ref_kind: str | None = None,
        statuses: tuple[RegistrationStatus, ...] | None,
    ) -> tuple[ChannelRegistration, ...]:
        where = ["r.channel = ?", "r.program_id = ?"]
        params: list[Any] = [channel, self.program_id]
        if provider_instance_id is not None:
            where.append("r.provider_instance_id = ?")
            params.append(provider_instance_id)
        if ref_kind is not None:
            where.append("r.ref_kind = ?")
            params.append(ref_kind)
        if statuses is not None:
            where.append("r.status IN (" + ",".join("?" for _ in statuses) + ")")
            params.extend(status.value for status in statuses)
        if workstream_id is not None:
            status_placeholders = ",".join("?" for _ in _LIVE_BINDING_STATUSES)
            where.append(
                f"""
                EXISTS (
                    SELECT 1 FROM registration_bindings b
                    WHERE b.channel = r.channel AND b.program_id = r.program_id
                      AND b.provider_instance_id = r.provider_instance_id
                      AND b.ref_id = r.ref_id AND b.ref_kind = r.ref_kind
                      AND b.workstream_id = ? AND b.status IN ({status_placeholders})
                )
                """
            )
            params.append(workstream_id)
            params.extend(_LIVE_BINDING_STATUSES)
        rows = conn.execute(
            f"SELECT r.* FROM registrations r WHERE {' AND '.join(where)} ORDER BY r.channel, r.ref_kind, r.ref_id",
            tuple(params),
        ).fetchall()
        registrations = tuple(_registration_from_row(row) for row in rows)
        if not registrations:
            return ()
        workstreams = self.get_workstream_map(
            channel,
            tuple((registration.ref_id, registration.ref_kind) for registration in registrations),
            provider_instance_id=provider_instance_id,
        )
        return tuple(replace(registration, workstream_ids=workstreams.get((registration.ref_id, registration.ref_kind), ())) for registration in registrations)

    def _load_discovered_refs_conn(
        self,
        conn: sqlite3.Connection,
        channel: str,
        *,
        provider_instance_id: str | None = None,
    ) -> tuple[DiscoveredRef, ...]:
        registrations = self._load_registrations_conn(
            conn,
            channel,
            provider_instance_id=provider_instance_id,
            statuses=(RegistrationStatus.ACTIVE,),
        )
        bindings_by_key = self._load_bindings_conn(conn, channel, provider_instance_id=provider_instance_id)
        return tuple(DiscoveredRef(registration=registration, bindings=bindings_by_key.get(_ref_key(registration), ())) for registration in registrations)

    def _load_bindings(self, channel: str, *, provider_instance_id: str | None = None) -> dict[tuple[str, str, str, str, str], tuple[RegistrationBinding, ...]]:
        with self._connect() as conn:
            return self._load_bindings_conn(conn, channel, provider_instance_id=provider_instance_id)

    def _load_bindings_conn(
        self,
        conn: sqlite3.Connection,
        channel: str,
        *,
        provider_instance_id: str | None = None,
    ) -> dict[tuple[str, str, str, str, str], tuple[RegistrationBinding, ...]]:
        params: list[Any] = [channel, self.program_id]
        status_placeholders = ",".join("?" for _ in _LIVE_BINDING_STATUSES)
        params.extend(_LIVE_BINDING_STATUSES)
        provider_clause = ""
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            params.append(provider_instance_id)
        rows = conn.execute(
            f"SELECT * FROM registration_bindings WHERE channel = ? AND program_id = ? AND status IN ({status_placeholders}){provider_clause}",
            tuple(params),
        ).fetchall()
        grouped: dict[tuple[str, str, str, str, str], list[RegistrationBinding]] = {}
        for row in rows:
            key = (str(row["channel"]), str(row["program_id"]), str(row["provider_instance_id"]), str(row["ref_id"]), str(row["ref_kind"]))
            grouped.setdefault(key, []).append(_binding_from_row(row))
        return {key: tuple(values) for key, values in grouped.items()}

    def _upsert_discovered_ref(self, conn: sqlite3.Connection, discovered_ref: DiscoveredRef, *, ttl_days: int | None, seen_at: datetime) -> None:
        registration = discovered_ref.registration
        if registration.program_id != self.program_id:
            registration = replace(registration, program_id=self.program_id)
        _validate_ref_id(registration.ref_kind, registration.ref_id)
        _validate_metadata(registration.metadata or {})
        expires_at = seen_at + timedelta(days=ttl_days) if ttl_days is not None else None
        conn.execute(
            """
            INSERT INTO registrations(
                channel, program_id, provider_instance_id, ref_id, ref_kind, status,
                first_discovered_at, last_seen_at, last_verified_at, expires_at, retired_at,
                consecutive_hydration_failures, confidence, confidence_source, pm_confirmed,
                promoted, signal_yield_0, signal_yield_1, signal_yield_2, ref_title, metadata_json,
                work_item_ids_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel, program_id, provider_instance_id, ref_id, ref_kind) DO UPDATE SET
                status = excluded.status,
                last_seen_at = excluded.last_seen_at,
                expires_at = excluded.expires_at,
                confidence = excluded.confidence,
                confidence_source = excluded.confidence_source,
                pm_confirmed = excluded.pm_confirmed,
                promoted = excluded.promoted,
                signal_yield_0 = excluded.signal_yield_0,
                signal_yield_1 = excluded.signal_yield_1,
                signal_yield_2 = excluded.signal_yield_2,
                ref_title = excluded.ref_title,
                metadata_json = excluded.metadata_json,
                work_item_ids_json = excluded.work_item_ids_json
            """,
            (
                registration.channel,
                self.program_id,
                registration.provider_instance_id,
                registration.ref_id,
                registration.ref_kind,
                registration.status.value,
                _format_datetime(registration.first_discovered_at),
                _format_datetime(seen_at),
                _format_datetime(registration.last_verified_at),
                _format_datetime(expires_at),
                _format_datetime(registration.retired_at),
                registration.consecutive_hydration_failures,
                registration.confidence,
                registration.confidence_source,
                int(registration.pm_confirmed),
                int(registration.promoted),
                registration.signal_yield_last_3[0],
                registration.signal_yield_last_3[1],
                registration.signal_yield_last_3[2],
                registration.ref_title,
                json.dumps(registration.metadata or {}, sort_keys=True),
                json.dumps(list(registration.work_item_ids)),
            ),
        )
        conn.execute(
            """
            UPDATE registration_bindings
            SET status = ?, retired_at = ?
            WHERE channel = ? AND program_id = ? AND provider_instance_id = ? AND ref_id = ? AND ref_kind = ?
            """,
            (RegistrationStatus.RETIRED.value, _format_datetime(seen_at), registration.channel, self.program_id, registration.provider_instance_id, registration.ref_id, registration.ref_kind),
        )
        for binding in discovered_ref.bindings or (RegistrationBinding(None, "default", "unknown", registration.confidence, registration.confidence_source),):
            conn.execute(
                """
                INSERT INTO registration_bindings(
                    channel, program_id, provider_instance_id, ref_id, ref_kind, workstream_id,
                    scope_id, source_type, status, confidence, confidence_source, pm_confirmed,
                    promoted, signal_yield_0, signal_yield_1, signal_yield_2, first_seen_at, last_seen_at, retired_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(channel, program_id, provider_instance_id, ref_id, ref_kind, workstream_id, scope_id) DO UPDATE SET
                    status = excluded.status,
                    confidence = excluded.confidence,
                    confidence_source = excluded.confidence_source,
                    pm_confirmed = MAX(registration_bindings.pm_confirmed, excluded.pm_confirmed),
                    promoted = MAX(registration_bindings.promoted, excluded.promoted),
                    signal_yield_0 = excluded.signal_yield_0,
                    signal_yield_1 = excluded.signal_yield_1,
                    signal_yield_2 = excluded.signal_yield_2,
                    last_seen_at = excluded.last_seen_at,
                    retired_at = NULL
                """,
                (
                    registration.channel,
                    self.program_id,
                    registration.provider_instance_id,
                    registration.ref_id,
                    registration.ref_kind,
                    binding.workstream_id,
                    binding.scope_id,
                    binding.source_type,
                    binding.status.value,
                    binding.confidence,
                    binding.confidence_source,
                    int(binding.pm_confirmed),
                    int(binding.promoted),
                    binding.signal_yield_last_3[0],
                    binding.signal_yield_last_3[1],
                    binding.signal_yield_last_3[2],
                    _format_datetime(seen_at),
                    _format_datetime(seen_at),
                ),
            )
        self._refresh_governance(
            conn,
            registration.channel,
            registration.ref_id,
            registration.ref_kind,
            provider_instance_id=registration.provider_instance_id,
        )

    def _insert_delta(self, conn: sqlite3.Connection, delta: RegistryDelta, *, provider_instance_id: str) -> None:
        detail = {
            "added": [_registration_detail(registration) for registration in delta.added],
            "removed": [_registration_detail(registration) for registration in delta.removed],
            "updated": [_registration_detail(registration) for registration in delta.updated],
            "failed_scopes": {
                scope_id: {
                    "status": status.status.value,
                    "completeness": status.completeness.value,
                    "item_count": status.item_count,
                    "error_message": status.error_message,
                }
                for scope_id, status in delta.failed_scopes.items()
            },
        }
        conn.execute(
            """
            INSERT INTO registry_deltas(channel, program_id, provider_instance_id, computed_at, completeness,
                added_count, removed_count, updated_count, unchanged_count, shrinkage_pct, detail_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delta.channel,
                self.program_id,
                provider_instance_id,
                _format_datetime(delta.computed_at),
                delta.completeness.value,
                len(delta.added),
                len(delta.removed),
                len(delta.updated),
                delta.unchanged_count,
                delta.shrinkage_pct,
                json.dumps(detail, sort_keys=True),
            ),
        )
        self._prune_delta_history(conn, delta.channel, provider_instance_id, keep_after=delta.computed_at)

    def _prune_delta_history(
        self,
        conn: sqlite3.Connection,
        channel: str,
        provider_instance_id: str,
        *,
        keep_after: datetime,
    ) -> None:
        cutoff = keep_after - timedelta(days=DELTA_HISTORY_RETENTION_DAYS)
        conn.execute(
            """
            DELETE FROM registry_deltas
            WHERE channel = ? AND program_id = ? AND provider_instance_id = ? AND computed_at < ?
            """,
            (channel, self.program_id, provider_instance_id, _format_datetime(cutoff)),
        )
        overflow = conn.execute(
            """
            SELECT id FROM registry_deltas
            WHERE channel = ? AND program_id = ? AND provider_instance_id = ?
            ORDER BY computed_at DESC, id DESC
            LIMIT -1 OFFSET ?
            """,
            (channel, self.program_id, provider_instance_id, DELTA_HISTORY_MAX_ROWS),
        ).fetchall()
        if not overflow:
            return
        conn.executemany(
            "DELETE FROM registry_deltas WHERE id = ?",
            [(int(row["id"]),) for row in overflow],
        )

    def _refresh_governance(
        self,
        conn: sqlite3.Connection,
        channel: str,
        ref_id: str,
        ref_kind: str,
        *,
        provider_instance_id: str | None = None,
    ) -> None:
        params: list[object] = [channel, self.program_id, ref_id, ref_kind]
        status_placeholders = ",".join("?" for _ in _LIVE_BINDING_STATUSES)
        params.extend(_LIVE_BINDING_STATUSES)
        provider_clause = ""
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            params.append(provider_instance_id)
        row = conn.execute(
            f"""
            SELECT MAX(confidence) AS confidence,
                   MAX(pm_confirmed) AS pm_confirmed,
                   MAX(promoted) AS promoted,
                   MAX(signal_yield_0) AS y0,
                   MAX(signal_yield_1) AS y1,
                   MAX(signal_yield_2) AS y2
            FROM registration_bindings
            WHERE channel = ? AND program_id = ? AND ref_id = ? AND ref_kind = ? AND status IN ({status_placeholders}){provider_clause}
            """,
            tuple(params),
        ).fetchone()
        if row is None:
            return
        source_row = conn.execute(
            f"""
            SELECT confidence_source
            FROM registration_bindings
            WHERE channel = ? AND program_id = ? AND ref_id = ? AND ref_kind = ? AND status IN ({status_placeholders}){provider_clause}
            ORDER BY confidence DESC, last_seen_at DESC, scope_id ASC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        registration_params: list[object] = [
            float(row["confidence"] or 1.0),
            str(source_row["confidence_source"] if source_row is not None and source_row["confidence_source"] is not None else "manual_config"),
            int(row["pm_confirmed"] or 0),
            int(row["promoted"] or 0),
            int(row["y0"] or 0),
            int(row["y1"] or 0),
            int(row["y2"] or 0),
            channel,
            self.program_id,
            ref_id,
            ref_kind,
        ]
        registration_provider_clause = ""
        if provider_instance_id is not None:
            registration_provider_clause = " AND provider_instance_id = ?"
            registration_params.append(provider_instance_id)
        conn.execute(
            f"""
            UPDATE registrations
            SET confidence = ?, confidence_source = ?, pm_confirmed = ?, promoted = ?,
                signal_yield_0 = ?, signal_yield_1 = ?, signal_yield_2 = ?
            WHERE channel = ? AND program_id = ? AND ref_id = ? AND ref_kind = ?{registration_provider_clause}
            """,
            tuple(registration_params),
        )

    def _record_scope_status_conn(self, conn: sqlite3.Connection, channel: str, provider_instance_id: str, scope_id: str, status: ScopeStatus, run_at: datetime) -> None:
        prior = conn.execute(
            "SELECT consecutive_failures FROM scope_health WHERE channel = ? AND program_id = ? AND provider_instance_id = ? AND scope_id = ?",
            (channel, self.program_id, provider_instance_id, scope_id),
        ).fetchone()
        failed = status.status is not ScopeStatusKind.SUCCESS
        consecutive = (int(prior["consecutive_failures"]) + 1) if prior and failed else (1 if failed else 0)
        conn.execute(
            """
            INSERT INTO scope_health(channel, program_id, provider_instance_id, scope_id, last_status, consecutive_failures, last_run_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel, program_id, provider_instance_id, scope_id) DO UPDATE SET
                last_status = excluded.last_status,
                consecutive_failures = excluded.consecutive_failures,
                last_run_at = excluded.last_run_at
            """,
            (channel, self.program_id, provider_instance_id, scope_id, status.status.value, consecutive, _format_datetime(run_at)),
        )

    def _upsert_scope_state(self, conn: sqlite3.Connection, channel: str, provider_instance_id: str, scope_id: str, state: ScopeState) -> None:
        conn.execute(
            """
            INSERT INTO scope_state(channel, program_id, provider_instance_id, scope_id, cursor_value, watermark_at, last_success_at, tombstone_ids_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel, program_id, provider_instance_id, scope_id) DO UPDATE SET
                cursor_value = excluded.cursor_value,
                watermark_at = excluded.watermark_at,
                last_success_at = excluded.last_success_at,
                tombstone_ids_json = excluded.tombstone_ids_json
            """,
            (
                channel,
                self.program_id,
                provider_instance_id,
                scope_id,
                state.cursor_value,
                _format_datetime(state.watermark_at),
                _format_datetime(state.last_success_at),
                json.dumps(list(state.tombstone_ids)),
            ),
        )

    def upsert_discovered_ref_with_conn(
        self,
        conn: sqlite3.Connection,
        discovered_ref: DiscoveredRef,
        *,
        ttl_days: int | None,
        seen_at: datetime,
    ) -> None:
        self._upsert_discovered_ref(conn, discovered_ref, ttl_days=ttl_days, seen_at=seen_at)

    def _set_status(
        self,
        channel: str,
        ref_id: str,
        ref_kind: str,
        status: RegistrationStatus,
        *,
        provider_instance_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        now = _format_datetime(datetime.now(timezone.utc))
        params: list[object] = [status.value, now if status is RegistrationStatus.RETIRED else None, channel, self.program_id, ref_id, ref_kind]
        provider_clause = ""
        if provider_instance_id is not None:
            provider_clause = " AND provider_instance_id = ?"
            params.append(provider_instance_id)
        if conn is None:
            with self._connect() as store_conn:
                self._set_status(
                    channel,
                    ref_id,
                    ref_kind,
                    status,
                    provider_instance_id=provider_instance_id,
                    conn=store_conn,
                )
            return
        conn.execute(
            f"UPDATE registrations SET status = ?, retired_at = ? WHERE channel = ? AND program_id = ? AND ref_id = ? AND ref_kind = ?{provider_clause}",
            tuple(params),
        )
        conn.execute(
            f"UPDATE registration_bindings SET status = ?, retired_at = ? WHERE channel = ? AND program_id = ? AND ref_id = ? AND ref_kind = ?{provider_clause}",
            tuple(params),
        )


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS registrations (
    channel TEXT NOT NULL,
    program_id TEXT NOT NULL,
    provider_instance_id TEXT NOT NULL DEFAULT 'default',
    ref_id TEXT NOT NULL,
    ref_kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    first_discovered_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_verified_at TEXT,
    last_hydration_attempt_at TEXT,
    last_hydration_error TEXT,
    expires_at TEXT,
    retired_at TEXT,
    consecutive_hydration_failures INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 1.0,
    confidence_source TEXT NOT NULL DEFAULT 'manual_config',
    pm_confirmed INTEGER NOT NULL DEFAULT 0,
    promoted INTEGER NOT NULL DEFAULT 0,
    signal_yield_0 INTEGER NOT NULL DEFAULT 0,
    signal_yield_1 INTEGER NOT NULL DEFAULT 0,
    signal_yield_2 INTEGER NOT NULL DEFAULT 0,
    ref_title TEXT,
    metadata_json TEXT,
    work_item_ids_json TEXT,
    PRIMARY KEY (channel, program_id, provider_instance_id, ref_id, ref_kind)
);
CREATE TABLE IF NOT EXISTS registration_bindings (
    channel TEXT NOT NULL,
    program_id TEXT NOT NULL,
    provider_instance_id TEXT NOT NULL DEFAULT 'default',
    ref_id TEXT NOT NULL,
    ref_kind TEXT NOT NULL,
    workstream_id TEXT,
    scope_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    confidence REAL NOT NULL DEFAULT 1.0,
    confidence_source TEXT NOT NULL DEFAULT 'manual_config',
    pm_confirmed INTEGER NOT NULL DEFAULT 0,
    promoted INTEGER NOT NULL DEFAULT 0,
    signal_yield_0 INTEGER NOT NULL DEFAULT 0,
    signal_yield_1 INTEGER NOT NULL DEFAULT 0,
    signal_yield_2 INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    retired_at TEXT,
    PRIMARY KEY (channel, program_id, provider_instance_id, ref_id, ref_kind, workstream_id, scope_id),
    FOREIGN KEY (channel, program_id, provider_instance_id, ref_id, ref_kind)
        REFERENCES registrations (channel, program_id, provider_instance_id, ref_id, ref_kind) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS scope_health (
    channel TEXT NOT NULL,
    program_id TEXT NOT NULL,
    provider_instance_id TEXT NOT NULL DEFAULT 'default',
    scope_id TEXT NOT NULL,
    last_status TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_run_at TEXT NOT NULL,
    PRIMARY KEY (channel, program_id, provider_instance_id, scope_id)
);
CREATE TABLE IF NOT EXISTS registry_deltas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    program_id TEXT NOT NULL,
    provider_instance_id TEXT NOT NULL DEFAULT 'default',
    computed_at TEXT NOT NULL,
    completeness TEXT NOT NULL,
    added_count INTEGER NOT NULL,
    removed_count INTEGER NOT NULL,
    updated_count INTEGER NOT NULL,
    unchanged_count INTEGER NOT NULL,
    shrinkage_pct REAL NOT NULL,
    detail_json TEXT
);
CREATE TABLE IF NOT EXISTS registry_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    program_id TEXT NOT NULL,
    provider_instance_id TEXT NOT NULL DEFAULT 'default',
    ref_id TEXT NOT NULL,
    ref_kind TEXT NOT NULL,
    action TEXT NOT NULL,
    pm_alias TEXT NOT NULL,
    reason TEXT,
    workstream_id TEXT,
    prior_workstream_id TEXT,
    series_id TEXT,
    thread_id TEXT,
    new_artifact_id TEXT,
    detail_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scope_state (
    channel TEXT NOT NULL,
    program_id TEXT NOT NULL,
    provider_instance_id TEXT NOT NULL DEFAULT 'default',
    scope_id TEXT NOT NULL,
    cursor_value TEXT,
    watermark_at TEXT,
    last_success_at TEXT,
    tombstone_ids_json TEXT,
    PRIMARY KEY (channel, program_id, provider_instance_id, scope_id)
);
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_intents (
    intent_id TEXT PRIMARY KEY,
    program_id TEXT NOT NULL,
    workstream_id TEXT NOT NULL,
    ref_kind TEXT NOT NULL,
    display_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT,
    decision_version INTEGER NOT NULL DEFAULT 0,
    UNIQUE(program_id, workstream_id, ref_kind, normalized_name)
);
CREATE TABLE IF NOT EXISTS source_candidates (
    candidate_id TEXT PRIMARY KEY,
    program_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    provider_instance_id TEXT NOT NULL DEFAULT 'default',
    ref_id TEXT NOT NULL,
    ref_kind TEXT NOT NULL,
    display_name TEXT,
    confidence REAL NOT NULL,
    source_provider TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    first_discovered_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decision_reason TEXT,
    old_status TEXT,
    decision_version INTEGER NOT NULL DEFAULT 0,
    UNIQUE(program_id, channel, provider_instance_id, ref_kind, ref_id)
);
CREATE TABLE IF NOT EXISTS candidate_intent_matches (
    candidate_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    match_confidence REAL NOT NULL,
    PRIMARY KEY (candidate_id, intent_id),
    FOREIGN KEY (candidate_id) REFERENCES source_candidates(candidate_id) ON DELETE CASCADE,
    FOREIGN KEY (intent_id) REFERENCES source_intents(intent_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS discovery_attempts (
    attempt_id TEXT PRIMARY KEY,
    program_id TEXT NOT NULL,
    intent_id TEXT,
    workstream_id TEXT,
    channel TEXT,
    provider_instance_id TEXT NOT NULL DEFAULT 'default',
    ref_kind TEXT,
    source_provider TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    autonomous_run_id TEXT,
    outcome TEXT NOT NULL,
    reason TEXT,
    result_count INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    attempted_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reg_channel_program_status ON registrations(channel, program_id, provider_instance_id, status);
CREATE INDEX IF NOT EXISTS idx_bindings_workstream ON registration_bindings(channel, program_id, workstream_id);
CREATE INDEX IF NOT EXISTS idx_bindings_scope ON registration_bindings(channel, program_id, provider_instance_id, scope_id);
CREATE INDEX IF NOT EXISTS idx_deltas_channel_time ON registry_deltas(channel, program_id, provider_instance_id, computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_ref ON registry_feedback(channel, program_id, provider_instance_id, ref_id, ref_kind);
CREATE INDEX IF NOT EXISTS idx_scope_state_channel ON scope_state(channel, program_id, provider_instance_id);
CREATE INDEX IF NOT EXISTS idx_source_intents_program_status ON source_intents(program_id, status);
CREATE INDEX IF NOT EXISTS idx_source_candidates_program_status_kind ON source_candidates(program_id, status, ref_kind);
CREATE INDEX IF NOT EXISTS idx_discovery_attempts_intent_time ON discovery_attempts(program_id, intent_id, attempted_at DESC);
"""


def _ref_key(registration: ChannelRegistration) -> tuple[str, str, str, str, str]:
    return (registration.channel, registration.program_id, registration.provider_instance_id, registration.ref_id, registration.ref_kind)


def _comparison_projection(ref: DiscoveredRef) -> tuple[Any, ...]:
    bindings = tuple(sorted((binding.workstream_id, binding.scope_id, binding.source_type, binding.confidence, binding.confidence_source, binding.pm_confirmed, binding.promoted, binding.status.value) for binding in ref.bindings))
    registration = ref.registration
    return (
        bindings,
        tuple(sorted(registration.workstream_ids)),
        tuple(sorted(registration.work_item_ids)),
        registration.confidence,
        registration.confidence_source,
        registration.pm_confirmed,
        registration.promoted,
        registration.status.value,
    )


def _result_provider_instance_id(result: DiscoveryResult) -> str:
    return normalize_discovery_result_provider_instance(result).provider_instance_id


def normalize_discovery_result_provider_instance(
    result: DiscoveryResult,
    *,
    expected_provider_instance_id: str | None = None,
) -> DiscoveryResult:
    declared_provider_instance_id = _normalize_provider_instance_id(result.provider_instance_id)
    expected_provider_instance_id = _normalize_provider_instance_id(expected_provider_instance_id)
    explicit_provider_instance_ids = {
        _normalize_provider_instance_id(discovered_ref.registration.provider_instance_id)
        for discovered_ref in result.discovered_refs
        if _normalize_provider_instance_id(discovered_ref.registration.provider_instance_id) != "default"
    }
    if len(explicit_provider_instance_ids) > 1:
        rendered = ", ".join(sorted(explicit_provider_instance_ids))
        raise RegistryMetadataError(
            f"Discovery result for channel '{result.channel}' mixes provider instances: {rendered}"
        )
    if (
        declared_provider_instance_id != "default"
        and explicit_provider_instance_ids
        and declared_provider_instance_id not in explicit_provider_instance_ids
    ):
        rendered = next(iter(explicit_provider_instance_ids))
        raise RegistryMetadataError(
            f"Discovery result for channel '{result.channel}' declared provider instance "
            f"'{declared_provider_instance_id}' but discovered refs used '{rendered}'"
        )
    if expected_provider_instance_id != "default":
        if declared_provider_instance_id != "default" and declared_provider_instance_id != expected_provider_instance_id:
            raise RegistryMetadataError(
                f"Discovery result for channel '{result.channel}' declared provider instance "
                f"'{declared_provider_instance_id}' but binding expected '{expected_provider_instance_id}'"
            )
        if explicit_provider_instance_ids and expected_provider_instance_id not in explicit_provider_instance_ids:
            rendered = ", ".join(sorted(explicit_provider_instance_ids))
            raise RegistryMetadataError(
                f"Discovery result for channel '{result.channel}' used provider instance(s) "
                f"'{rendered}' but binding expected '{expected_provider_instance_id}'"
            )
    effective_provider_instance_id = declared_provider_instance_id
    if expected_provider_instance_id != "default":
        effective_provider_instance_id = expected_provider_instance_id
    if explicit_provider_instance_ids:
        effective_provider_instance_id = next(iter(explicit_provider_instance_ids))
    if expected_provider_instance_id != "default":
        effective_provider_instance_id = expected_provider_instance_id
    normalized_refs = tuple(
        replace(
            discovered_ref,
            registration=replace(
                discovered_ref.registration,
                provider_instance_id=effective_provider_instance_id,
            ),
        )
        for discovered_ref in result.discovered_refs
    )
    return replace(
        result,
        discovered_refs=normalized_refs,
        provider_instance_id=effective_provider_instance_id,
    )


def _normalize_provider_instance_id(value: str | None) -> str:
    if value is None:
        return "default"
    normalized = str(value).strip()
    return normalized or "default"


def _registration_from_row(row: sqlite3.Row) -> ChannelRegistration:
    return ChannelRegistration(
        channel=str(row["channel"]),
        program_id=str(row["program_id"]),
        provider_instance_id=str(row["provider_instance_id"]),
        ref_id=str(row["ref_id"]),
        ref_kind=str(row["ref_kind"]),
        status=RegistrationStatus(str(row["status"])),
        first_discovered_at=_parse_datetime(str(row["first_discovered_at"])) or datetime.now(timezone.utc),
        last_seen_at=_parse_datetime(str(row["last_seen_at"])) or datetime.now(timezone.utc),
        last_verified_at=_parse_datetime(row["last_verified_at"]),
        retired_at=_parse_datetime(row["retired_at"]),
        consecutive_hydration_failures=int(row["consecutive_hydration_failures"]),
        confidence=float(row["confidence"]),
        confidence_source=str(row["confidence_source"]),
        pm_confirmed=bool(row["pm_confirmed"]),
        promoted=bool(row["promoted"]),
        signal_yield_last_3=(int(row["signal_yield_0"]), int(row["signal_yield_1"]), int(row["signal_yield_2"])),
        ref_title=row["ref_title"],
        metadata=json.loads(row["metadata_json"] or "{}"),
        work_item_ids=_parse_work_item_ids_json(row["work_item_ids_json"]),
    )


def _binding_from_row(row: sqlite3.Row) -> RegistrationBinding:
    return RegistrationBinding(
        workstream_id=row["workstream_id"],
        scope_id=str(row["scope_id"]),
        source_type=str(row["source_type"]),
        confidence=float(row["confidence"]),
        confidence_source=str(row["confidence_source"]),
        pm_confirmed=bool(row["pm_confirmed"]),
        promoted=bool(row["promoted"]),
        status=RegistrationStatus(str(row["status"])),
        signal_yield_last_3=(int(row["signal_yield_0"]), int(row["signal_yield_1"]), int(row["signal_yield_2"])),
    )


def _delta_from_row(row: sqlite3.Row) -> RegistryDelta:
    detail = _parse_delta_detail(row["detail_json"])
    added_count = int(row["added_count"])
    removed_count = int(row["removed_count"])
    updated_count = int(row["updated_count"])
    computed_at = _parse_datetime(str(row["computed_at"])) or datetime.now(timezone.utc)
    return RegistryDelta(
        channel=str(row["channel"]),
        program_id=str(row["program_id"]),
        computed_at=computed_at,
        previous_discovery_at=None,
        completeness=DiscoveryCompleteness(str(row["completeness"])),
        added=tuple(_registration_from_detail(entry, row, computed_at=computed_at) for entry in detail.get("added", [])[:added_count]),
        removed=tuple(_registration_from_detail(entry, row, computed_at=computed_at) for entry in detail.get("removed", [])[:removed_count]),
        updated=tuple(_registration_from_detail(entry, row, computed_at=computed_at) for entry in detail.get("updated", [])[:updated_count]),
        unchanged_count=int(row["unchanged_count"]),
        failed_scopes=_failed_scopes_from_detail(detail.get("failed_scopes")),
        shrinkage_pct=float(row["shrinkage_pct"]),
    )


def _registration_from_detail(entry: Any, row: sqlite3.Row, *, computed_at: datetime) -> ChannelRegistration:
    if not isinstance(entry, dict):
        return _placeholder_registration(row, computed_at=computed_at)
    status_raw = str(entry.get("status") or RegistrationStatus.ACTIVE.value)
    try:
        status = RegistrationStatus(status_raw)
    except ValueError:
        status = RegistrationStatus.ACTIVE
    last_seen_at = _parse_datetime(entry.get("last_seen_at")) or computed_at
    return ChannelRegistration(
        channel=str(row["channel"]),
        program_id=str(row["program_id"]),
        provider_instance_id=str(entry.get("provider_instance_id") or row["provider_instance_id"]),
        ref_id=str(entry.get("ref_id") or "_delta_placeholder"),
        ref_kind=str(entry.get("ref_kind") or "delta_placeholder"),
        status=status,
        first_discovered_at=computed_at,
        last_seen_at=last_seen_at,
        confidence=float(entry.get("confidence") or 1.0),
        ref_title=str(entry.get("ref_title") or "") or None,
        workstream_ids=tuple(str(value) for value in entry.get("workstream_ids", []) if str(value).strip()),
        work_item_ids=tuple(
            int(value)
            for value in entry.get("work_item_ids", [])
            if isinstance(value, int)
        ),
    )


def _placeholder_registration(row: sqlite3.Row, *, computed_at: datetime) -> ChannelRegistration:
    return ChannelRegistration(
        channel=str(row["channel"]),
        program_id=str(row["program_id"]),
        provider_instance_id=str(row["provider_instance_id"]),
        ref_id="_delta_placeholder",
        ref_kind="delta_placeholder",
        status=RegistrationStatus.ACTIVE,
        first_discovered_at=computed_at,
        last_seen_at=computed_at,
    )


def _parse_delta_detail(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _failed_scopes_from_detail(raw: Any) -> dict[str, ScopeStatus]:
    if not isinstance(raw, dict):
        return {}
    failed_scopes: dict[str, ScopeStatus] = {}
    for scope_id, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        status_raw = str(payload.get("status") or "").strip()
        completeness_raw = str(payload.get("completeness") or DiscoveryCompleteness.PARTIAL.value).strip()
        if not status_raw:
            continue
        try:
            failed_scopes[str(scope_id)] = ScopeStatus(
                scope_id=str(scope_id),
                status=ScopeStatusKind(status_raw),
                completeness=DiscoveryCompleteness(completeness_raw),
                item_count=int(payload.get("item_count") or 0),
                error_message=str(payload.get("error_message") or "") or None,
            )
        except (TypeError, ValueError):
            continue
    return failed_scopes


def _scope_state_from_row(row: sqlite3.Row) -> ScopeState:
    return ScopeState(
        scope_id=str(row["scope_id"]),
        cursor_value=row["cursor_value"],
        watermark_at=_parse_datetime(row["watermark_at"]),
        last_success_at=_parse_datetime(row["last_success_at"]),
        tombstone_ids=tuple(json.loads(row["tombstone_ids_json"] or "[]")),
    )


def _registration_detail(registration: ChannelRegistration) -> dict[str, Any]:
    return {
        "channel": registration.channel,
        "provider_instance_id": registration.provider_instance_id,
        "ref_id": registration.ref_id,
        "ref_kind": registration.ref_kind,
        "ref_title": registration.ref_title,
        "status": registration.status.value,
        "confidence": registration.confidence,
        "last_seen_at": _format_datetime(registration.last_seen_at),
        "workstream_ids": list(registration.workstream_ids),
        "work_item_ids": list(registration.work_item_ids),
    }


def _ensure_registrations_additive_columns(conn: sqlite3.Connection) -> None:
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(registrations)").fetchall()
    }
    if "work_item_ids_json" not in columns:
        conn.execute("ALTER TABLE registrations ADD COLUMN work_item_ids_json TEXT")


def _parse_work_item_ids_json(value: Any) -> tuple[int, ...]:
    if not isinstance(value, str) or not value.strip():
        return ()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(int(item) for item in parsed if isinstance(item, int))


def _validate_ref_id(ref_kind: str, ref_id: str) -> None:
    pattern = _REFID_VALIDATORS.get(ref_kind)
    if pattern is not None and not pattern.fullmatch(ref_id):
        raise RegistryMetadataError(f"Invalid ref_id '{ref_id}' for ref_kind '{ref_kind}'")


def _validate_metadata(metadata: dict[str, Any]) -> None:
    allowed = (str, int, float, bool, type(None))
    for key, value in metadata.items():
        if not isinstance(key, str):
            raise RegistryMetadataError("Registry metadata keys must be strings")
        if not isinstance(value, allowed):
            raise RegistryMetadataError(f"Registry metadata value for '{key}' is not JSON-flat")


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _current_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
