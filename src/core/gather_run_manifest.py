"""Gather-run manifest: the ``gather-run.v1`` model, canonical hashing, and the
atomic staged/committed/failed/quarantine artifact layout (specs/armada.md
D-13 through D-19, Sec 4.6).

This module owns:

* The immutable ``GatherRunManifest`` model and its nested ``query_results[]``
  / ``channel_outcomes[]`` / ``failed_refs[]`` rows (Sec 4.6 field list).
* Canonical-JSON manifest hashing that reuses ``manifest_writer.hash_content``
  and follows D-16's array-sort-before-hash rules.
* The ``programs/<program>/runtime/gather_runs/{staging,committed,failed,
  quarantine}/<run_id>/`` layout (D-14), including the ``latest.json`` /
  ``latest_full.json`` pointers and their corrupt-pointer fallback scan.
* The commit/fail lifecycle helpers implementing Sec 4.6's numbered commit
  algorithm, and the D-13 rule-8 recovery scan that quarantines abandoned
  ``running`` manifests once their lease is confirmed no longer current.

Wiring this into ``vertex gather`` itself (lease acquisition, ULID minting,
stamping signals/facts with ``gather_run_id``) is separate integration work
(``arm-gather-run-lifecycle``); this module provides the primitive only, the
same layering ``workspace_lease.py`` uses for the lease primitive it exposes.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from src.core.manifest_writer import hash_content
from src.core.program_paths import PROGRAMS_ROOT, get_runtime_dir
from src.core.workspace_lease import is_lease_expired, read_lease_state

SCHEMA_VERSION = "gather-run.v1"

#: D-13's mutation domain for the gather workspace lease (workspace_lease.py).
GATHER_MUTATION_DOMAIN = "gather"

#: D-14 directory layout, all rooted at ``runtime/gather_runs/``.
GATHER_RUNS_SUBDIR = "gather_runs"
STAGING_SUBDIR = "staging"
COMMITTED_SUBDIR = "committed"
FAILED_SUBDIR = "failed"
QUARANTINE_SUBDIR = "quarantine"

MANIFEST_FILENAME = "manifest.json"
ADO_ITEMS_FILENAME = "ado_items.jsonl"
QUERY_RESULTS_FILENAME = "query_results.json"
ORPHAN_INDEX_FILENAME = "orphan_index.json"
LATEST_POINTER_FILENAME = "latest.json"
LATEST_FULL_POINTER_FILENAME = "latest_full.json"

# Windows can transiently hold a just-closed file handle while the directory
# promotion happens. Retrying only the atomic rename (never a copy/delete)
# retains the all-or-nothing promotion contract.
_PROMOTION_RETRY_ATTEMPTS = 3
_PROMOTION_RETRY_DELAY_SECONDS = 0.05

#: Sec 4.6 ``actor_identity_type`` values — not yet a closed enum in the spec,
#: but these two are the only identities gather can run under today.
ACTOR_IDENTITY_INTERACTIVE = "interactive"
ACTOR_IDENTITY_SCHEDULED = "scheduled"
#: §4.17 step 5's synthetic legacy-cutoff manifest is not produced by a real
#: gather invocation, so it carries its own actor identity rather than
#: pretending to be an interactive/scheduled run.
ACTOR_IDENTITY_SYNTHETIC = "synthetic"

#: §4.17 step 5: prefix for the synthetic legacy-cutoff run ID
#: (``gather-legacy-<ULID>``), distinguishing it from real ``gather-<ULID>``
#: runs at a glance.
LEGACY_CUTOFF_RUN_ID_PREFIX = "gather-legacy-"


class GatherRunStatus(str, Enum):
    """D-13's four run states: ``running -> {committed, failed, quarantined}``."""

    RUNNING = "running"
    COMMITTED = "committed"
    FAILED = "failed"
    QUARANTINED = "quarantined"


class RequiredScopeStatus(str, Enum):
    """D-18: ``PARTIAL`` when first-to-last query capture skew exceeds 5 minutes."""

    FULL = "FULL"
    PARTIAL = "PARTIAL"


@dataclass(frozen=True, slots=True)
class QueryResultEntry:
    """One ``query_results[]`` row: D-14's per-``(query_id, scope_id)`` capture."""

    query_id: str
    scope_id: str
    wiql_hash: str
    captured_at: datetime
    raw_count: int
    membership_ids: tuple[str, ...]
    membership_hash: str
    cap_reached: bool
    completeness_state: str
    oracle_result: str | None = None
    failure_category: str | None = None


# D-19/AG-2.12: the closed set of completeness-oracle outcomes a
# ``QueryResultEntry.oracle_result`` may carry. §4.3's preferred order is
# (1) an independent Kusto/OData validation query -- deferred pending
# ARM-GATHER-15 qualification, not yet a usable oracle; (2) a sanitized ADO
# query export/UI membership count the operator explicitly records; (3) a
# same-endpoint rerun, which is only a weak consistency check and must never
# be the *silent* sole completeness proof. ``resolve_oracle_result`` records
# outcome (3) explicitly (rather than leaving the field ``None``, which would
# read as "not evaluated") so doctor can surface exactly which scopes still
# rest on the weak proof alone.
ORACLE_RESULT_SAME_ENDPOINT_RERUN = "same_endpoint_rerun"
ORACLE_RESULT_OPERATOR_EXPORT_MATCH = "operator_source_export:match"
_ORACLE_RESULT_OPERATOR_EXPORT_MISMATCH_PREFIX = "operator_source_export:mismatch"


def resolve_oracle_result(
    scope_id: str,
    raw_count: int,
    operator_source_export_counts: dict[str, int],
) -> str:
    """D-19/AG-2.12: classify one scope's completeness-oracle evidence.

    Returns ``ORACLE_RESULT_OPERATOR_EXPORT_MATCH`` when the operator
    recorded a sanitized source-export count for ``scope_id`` (e.g. via
    ``vertex gather --source-export <scope_id>=<count>``) and it agrees with
    the discovered ``raw_count``; a descriptive mismatch string (still
    prefixed ``operator_source_export:mismatch``) when it disagrees; or the
    explicit ``ORACLE_RESULT_SAME_ENDPOINT_RERUN`` default when no operator
    export was recorded for this scope.
    """
    if scope_id not in operator_source_export_counts:
        return ORACLE_RESULT_SAME_ENDPOINT_RERUN
    reported_count = operator_source_export_counts[scope_id]
    if reported_count == raw_count:
        return ORACLE_RESULT_OPERATOR_EXPORT_MATCH
    return f"{_ORACLE_RESULT_OPERATOR_EXPORT_MISMATCH_PREFIX}:reported={reported_count}:observed={raw_count}"


def is_weak_oracle_result(oracle_result: str | None) -> bool:
    """True when ``oracle_result`` reflects only the weak same-endpoint
    rerun proof (including the legacy/unset ``None`` case, which predates
    this field being populated)."""
    return oracle_result is None or oracle_result == ORACLE_RESULT_SAME_ENDPOINT_RERUN


def is_mismatched_oracle_result(oracle_result: str | None) -> bool:
    """True when an operator-recorded source export disagreed with the
    discovered raw count for that scope."""
    return oracle_result is not None and oracle_result.startswith(_ORACLE_RESULT_OPERATOR_EXPORT_MISMATCH_PREFIX)


@dataclass(frozen=True, slots=True)
class ChannelOutcomeEntry:
    """One ``channel_outcomes[]`` row, mirroring ``BudgetedCallOutcome``."""

    channel: str
    degraded: bool
    degrade_reason: str | None
    elapsed_seconds: float
    ado_call_count: int = 0
    retry_count: int = 0
    throttle_count: int = 0


@dataclass(frozen=True, slots=True)
class FailedRefEntry:
    """One ``failed_refs[]`` row."""

    ref_kind: str
    ref_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class GatherRunManifest:
    """The ``gather-run.v1`` manifest (Sec 4.6). Immutable — status transitions
    (``commit_staging_run`` / ``fail_staging_run`` / the quarantine recovery
    scan below) return a new instance rather than mutating this one."""

    run_id: str
    status: GatherRunStatus
    program_id: str
    actor_identity_type: str
    lease_owner: str
    lease_fencing_token: int
    started_at: datetime
    scope_as_of: datetime
    required_scope_status: RequiredScopeStatus
    schema_version: str = SCHEMA_VERSION
    replayed_from_run_id: str | None = None
    finished_at: datetime | None = None
    data_as_of: datetime | None = None
    first_query_captured_at: datetime | None = None
    last_query_captured_at: datetime | None = None
    query_capture_skew_seconds: float | None = None
    query_results: tuple[QueryResultEntry, ...] = ()
    discovered_count: int = 0
    hydrated_count: int = 0
    failed_refs: tuple[FailedRefEntry, ...] = ()
    cap_reached: bool = False
    shrinkage_classification: str | None = None
    channel_outcomes: tuple[ChannelOutcomeEntry, ...] = ()
    latency: float = 0.0
    ado_call_count: int = 0
    retry_count: int = 0
    throttle_count: int = 0
    last_successful_full_discovery_at: datetime | None = None
    last_attempt_at: datetime | None = None
    next_expected_run_at: datetime | None = None
    consecutive_failed_runs: int = 0
    freshness_state: str = ""
    alert_ids: tuple[str, ...] = ()
    alert_delivery_failed: bool = False
    privacy_classification: str = ""
    ado_items_hash: str | None = None
    query_results_hash: str | None = None
    manifest_hash: str | None = None
    #: §4.17 step 5: set only on the synthetic legacy-cutoff manifest. Records
    #: with no ``gather_run_id`` are attributed to this run at read time (never
    #: by rewriting historical JSONL) when their timestamp is at or before this
    #: cutoff. ``None`` on every real gather-run manifest.
    legacy_cutoff_at: datetime | None = None


# ---------------------------------------------------------------------------
# Canonical JSON + hashing (D-16)
# ---------------------------------------------------------------------------


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    return value


def canonical_json(value: Any) -> str:
    """Deterministic JSON text: sorted keys, no incidental whitespace."""
    return json.dumps(_to_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sorted_query_results(entries: tuple[QueryResultEntry, ...]) -> tuple[QueryResultEntry, ...]:
    return tuple(sorted(entries, key=lambda e: (e.query_id, e.scope_id)))


def _sorted_channel_outcomes(entries: tuple[ChannelOutcomeEntry, ...]) -> tuple[ChannelOutcomeEntry, ...]:
    return tuple(sorted(entries, key=lambda e: e.channel))


def _sorted_failed_refs(entries: tuple[FailedRefEntry, ...]) -> tuple[FailedRefEntry, ...]:
    return tuple(sorted(entries, key=lambda e: (e.ref_kind, e.ref_id)))


def compute_manifest_hash(manifest: GatherRunManifest) -> str:
    """D-16: sort arrays, exclude ``manifest_hash`` itself, hash the rest."""
    payload = _to_jsonable(manifest)
    payload.pop("manifest_hash", None)
    payload["query_results"] = _to_jsonable(_sorted_query_results(manifest.query_results))
    payload["channel_outcomes"] = _to_jsonable(_sorted_channel_outcomes(manifest.channel_outcomes))
    payload["failed_refs"] = _to_jsonable(_sorted_failed_refs(manifest.failed_refs))
    payload["alert_ids"] = sorted(manifest.alert_ids)
    if manifest.legacy_cutoff_at is None:
        # §4.17 step 5 backward compatibility: manifests written before this
        # field existed never had it in their hashed payload. Omitting the
        # key when unset (the default for every ordinary gather run) keeps
        # this computation byte-identical to the pre-field version, so
        # historical manifest_hash values keep verifying. The one manifest
        # type that actually sets this field (the synthetic legacy-cutoff
        # manifest) still gets it covered by the hash below.
        payload.pop("legacy_cutoff_at", None)
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hash_content(text)


def hash_ado_items(rows: list[dict[str, Any]]) -> str:
    """D-14/D-16: ``ado_items.jsonl`` is sorted by numeric work-item ID and
    hashed as one canonical JSON document (the persisted file is JSONL; this
    hash covers the same rows in the same sorted order)."""
    sorted_rows = sorted(rows, key=lambda row: int(row["work_item_id"]))
    return hash_content(canonical_json(sorted_rows))


def hash_query_results(entries: tuple[QueryResultEntry, ...]) -> str:
    """D-16: ``query_results.json`` is sorted by query ID/scope ID and hashed
    separately from the manifest's own (also-sorted) ``query_results[]``."""
    return hash_content(canonical_json(_sorted_query_results(entries)))


# ---------------------------------------------------------------------------
# D-14 artifact layout
# ---------------------------------------------------------------------------


def get_gather_runs_dir(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_runtime_dir(program_id, programs_root=programs_root) / GATHER_RUNS_SUBDIR


def get_staging_run_dir(program_id: str, run_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_gather_runs_dir(program_id, programs_root=programs_root) / STAGING_SUBDIR / run_id


def get_committed_run_dir(program_id: str, run_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_gather_runs_dir(program_id, programs_root=programs_root) / COMMITTED_SUBDIR / run_id


def get_failed_run_dir(program_id: str, run_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_gather_runs_dir(program_id, programs_root=programs_root) / FAILED_SUBDIR / run_id


def get_quarantine_run_dir(program_id: str, run_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_gather_runs_dir(program_id, programs_root=programs_root) / QUARANTINE_SUBDIR / run_id


def get_committed_root(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_gather_runs_dir(program_id, programs_root=programs_root) / COMMITTED_SUBDIR


def get_verified_committed_run_ids(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> frozenset[str]:
    """Return only committed run IDs whose manifests still pass hash verification.

    Read paths use this rather than trusting a directory name or status field:
    a partially written, corrupt, or tampered manifest must never make
    gather-stamped signals or facts visible.  Invalid entries are ignored so a
    bad historical artifact cannot take down a program reader.
    """
    committed_root = get_committed_root(program_id, programs_root=programs_root)
    if not committed_root.exists():
        return frozenset()

    run_ids: set[str] = set()
    for run_dir in committed_root.iterdir():
        if not run_dir.is_dir():
            continue
        try:
            manifest = read_manifest(run_dir)
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            continue
        if (
            manifest.status is GatherRunStatus.COMMITTED
            and manifest.manifest_hash
            and compute_manifest_hash(manifest) == manifest.manifest_hash
        ):
            run_ids.add(manifest.run_id)
    return frozenset(run_ids)


def get_legacy_cutoff_at(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> datetime | None:
    """§4.17 step 5: return the program's ratified legacy-cutoff timestamp, or
    ``None`` if no legacy-cutoff manifest has been created yet.

    Only a hash-valid, committed manifest with ``legacy_cutoff_at`` set
    counts — the same tamper-resistance rule as
    ``get_verified_committed_run_ids``. Creation is idempotent
    (``create_legacy_cutoff_manifest``), so normally at most one such manifest
    exists; if more than one is somehow present, the earliest cutoff is used
    so grandfathering stays maximally conservative.
    """
    committed_root = get_committed_root(program_id, programs_root=programs_root)
    if not committed_root.exists():
        return None

    cutoffs: list[datetime] = []
    for run_dir in committed_root.iterdir():
        if not run_dir.is_dir():
            continue
        try:
            manifest = read_manifest(run_dir)
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            continue
        if manifest.legacy_cutoff_at is None:
            continue
        if (
            manifest.status is GatherRunStatus.COMMITTED
            and manifest.manifest_hash
            and compute_manifest_hash(manifest) == manifest.manifest_hash
        ):
            cutoffs.append(manifest.legacy_cutoff_at)
    return min(cutoffs) if cutoffs else None


def create_legacy_cutoff_manifest(
    program_id: str,
    *,
    legacy_cutoff_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
) -> GatherRunManifest:
    """§4.17 step 5: create (once) the synthetic committed
    ``gather-legacy-<ULID>`` manifest that bounds how far back an unstamped
    (pre-run-lifecycle) signal/fact may be attributed to "legacy" at read
    time. This never rewrites historical JSONL — attribution happens only in
    the read path (``journal.read_signals`` / ``program_fact_store.load_program_facts``).

    Idempotent: if a valid legacy-cutoff manifest already exists, it is
    returned unchanged rather than creating a duplicate.
    """
    existing_cutoff = get_legacy_cutoff_at(program_id, programs_root=programs_root)
    if existing_cutoff is not None:
        committed_root = get_committed_root(program_id, programs_root=programs_root)
        for run_dir in committed_root.iterdir():
            if not run_dir.is_dir() or not run_dir.name.startswith(LEGACY_CUTOFF_RUN_ID_PREFIX):
                continue
            try:
                manifest = read_manifest(run_dir)
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                continue
            if manifest.legacy_cutoff_at == existing_cutoff:
                return manifest
        # Defensive: a valid cutoff was found but its manifest could not be
        # re-read (should not happen). Fall through and create a fresh one
        # rather than raise, since idempotency is best-effort convenience.

    from src.core.ledger.ulid import new_ulid

    run_id = f"{LEGACY_CUTOFF_RUN_ID_PREFIX}{new_ulid(legacy_cutoff_at)}"
    manifest = GatherRunManifest(
        run_id=run_id,
        status=GatherRunStatus.COMMITTED,
        program_id=program_id,
        actor_identity_type=ACTOR_IDENTITY_SYNTHETIC,
        lease_owner="legacy-cutoff-bootstrap",
        lease_fencing_token=0,
        started_at=legacy_cutoff_at,
        scope_as_of=legacy_cutoff_at,
        required_scope_status=RequiredScopeStatus.FULL,
        finished_at=legacy_cutoff_at,
        legacy_cutoff_at=legacy_cutoff_at,
    )
    manifest_hash = compute_manifest_hash(manifest)
    manifest = _with_manifest_hash(manifest, manifest_hash)

    committed_dir = get_committed_run_dir(program_id, run_id, programs_root=programs_root)
    write_manifest(committed_dir, manifest)
    return manifest


def get_latest_pointer_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_gather_runs_dir(program_id, programs_root=programs_root) / LATEST_POINTER_FILENAME


def get_latest_full_pointer_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_gather_runs_dir(program_id, programs_root=programs_root) / LATEST_FULL_POINTER_FILENAME


def _manifest_path(run_dir: Path) -> Path:
    return run_dir / MANIFEST_FILENAME


def _ado_items_path(run_dir: Path) -> Path:
    return run_dir / ADO_ITEMS_FILENAME


def _query_results_path(run_dir: Path) -> Path:
    return run_dir / QUERY_RESULTS_FILENAME


def _orphan_index_path(run_dir: Path) -> Path:
    return run_dir / ORPHAN_INDEX_FILENAME


# ---------------------------------------------------------------------------
# Atomic file I/O (temp file + fsync + os.replace, matching manifest_writer.py)
# ---------------------------------------------------------------------------


def _write_atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _write_atomic_json(path: Path, payload: Any) -> None:
    _write_atomic_text(path, json.dumps(_to_jsonable(payload), indent=2, sort_keys=True, ensure_ascii=False))


def write_manifest(run_dir: Path, manifest: GatherRunManifest) -> Path:
    path = _manifest_path(run_dir)
    _write_atomic_json(path, manifest)
    return path


def read_manifest(run_dir: Path) -> GatherRunManifest:
    path = _manifest_path(run_dir)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return _manifest_from_payload(payload)


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _parse_required_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _manifest_from_payload(payload: dict[str, Any]) -> GatherRunManifest:
    query_results = tuple(
        QueryResultEntry(
            query_id=row["query_id"],
            scope_id=row["scope_id"],
            wiql_hash=row["wiql_hash"],
            captured_at=_parse_required_datetime(row["captured_at"]),
            raw_count=row["raw_count"],
            membership_ids=tuple(row["membership_ids"]),
            membership_hash=row["membership_hash"],
            cap_reached=row["cap_reached"],
            completeness_state=row["completeness_state"],
            oracle_result=row.get("oracle_result"),
            failure_category=row.get("failure_category"),
        )
        for row in payload.get("query_results", [])
    )
    channel_outcomes = tuple(
        ChannelOutcomeEntry(
            channel=row["channel"],
            degraded=row["degraded"],
            degrade_reason=row.get("degrade_reason"),
            elapsed_seconds=row["elapsed_seconds"],
            ado_call_count=row.get("ado_call_count", 0),
            retry_count=row.get("retry_count", 0),
            throttle_count=row.get("throttle_count", 0),
        )
        for row in payload.get("channel_outcomes", [])
    )
    failed_refs = tuple(
        FailedRefEntry(ref_kind=row["ref_kind"], ref_id=row["ref_id"], reason=row["reason"])
        for row in payload.get("failed_refs", [])
    )
    return GatherRunManifest(
        run_id=payload["run_id"],
        status=GatherRunStatus(payload["status"]),
        program_id=payload["program_id"],
        actor_identity_type=payload["actor_identity_type"],
        lease_owner=payload["lease_owner"],
        lease_fencing_token=payload["lease_fencing_token"],
        started_at=_parse_required_datetime(payload["started_at"]),
        scope_as_of=_parse_required_datetime(payload["scope_as_of"]),
        required_scope_status=RequiredScopeStatus(payload["required_scope_status"]),
        schema_version=payload.get("schema_version", SCHEMA_VERSION),
        replayed_from_run_id=payload.get("replayed_from_run_id"),
        finished_at=_parse_datetime(payload.get("finished_at")),
        data_as_of=_parse_datetime(payload.get("data_as_of")),
        first_query_captured_at=_parse_datetime(payload.get("first_query_captured_at")),
        last_query_captured_at=_parse_datetime(payload.get("last_query_captured_at")),
        query_capture_skew_seconds=payload.get("query_capture_skew_seconds"),
        query_results=query_results,
        discovered_count=payload.get("discovered_count", 0),
        hydrated_count=payload.get("hydrated_count", 0),
        failed_refs=failed_refs,
        cap_reached=payload.get("cap_reached", False),
        shrinkage_classification=payload.get("shrinkage_classification"),
        channel_outcomes=channel_outcomes,
        latency=payload.get("latency", 0.0),
        ado_call_count=payload.get("ado_call_count", 0),
        retry_count=payload.get("retry_count", 0),
        throttle_count=payload.get("throttle_count", 0),
        last_successful_full_discovery_at=_parse_datetime(payload.get("last_successful_full_discovery_at")),
        last_attempt_at=_parse_datetime(payload.get("last_attempt_at")),
        next_expected_run_at=_parse_datetime(payload.get("next_expected_run_at")),
        consecutive_failed_runs=payload.get("consecutive_failed_runs", 0),
        freshness_state=payload.get("freshness_state", ""),
        alert_ids=tuple(payload.get("alert_ids", ())),
        alert_delivery_failed=payload.get("alert_delivery_failed", False),
        privacy_classification=payload.get("privacy_classification", ""),
        ado_items_hash=payload.get("ado_items_hash"),
        query_results_hash=payload.get("query_results_hash"),
        manifest_hash=payload.get("manifest_hash"),
        legacy_cutoff_at=_parse_datetime(payload.get("legacy_cutoff_at")),
    )


def write_ado_items(run_dir: Path, rows: list[dict[str, Any]]) -> Path:
    """Write D-14's ``ado_items.jsonl``: one globally deduplicated row per work
    item, sorted by numeric work-item ID, written atomically."""
    path = _ado_items_path(run_dir)
    sorted_rows = sorted(rows, key=lambda row: int(row["work_item_id"]))
    text = "\n".join(canonical_json(row) for row in sorted_rows)
    _write_atomic_text(path, text)
    return path


def write_query_results_sidecar(run_dir: Path, entries: tuple[QueryResultEntry, ...]) -> Path:
    """Write D-14's ``query_results.json`` sidecar, sorted by query/scope ID."""
    path = _query_results_path(run_dir)
    _write_atomic_json(path, list(_sorted_query_results(entries)))
    return path


# ---------------------------------------------------------------------------
# Lifecycle (Sec 4.6 commit algorithm)
# ---------------------------------------------------------------------------


def create_staging_manifest(
    manifest: GatherRunManifest,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    """Sec 4.6 steps 1-2: atomically create the ``status=running`` manifest in
    the staging directory. Callers mint ``run_id`` via
    ``"gather-" + new_ulid(started_at)`` and acquire the lease beforehand."""
    if manifest.status is not GatherRunStatus.RUNNING:
        raise ValueError(f"staging manifest must be status=running, got {manifest.status!r}")
    run_dir = get_staging_run_dir(manifest.program_id, manifest.run_id, programs_root=programs_root)
    return write_manifest(run_dir, manifest)


def _promote_dir(src_dir: Path, dst_dir: Path) -> None:
    dst_dir.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(_PROMOTION_RETRY_ATTEMPTS):
        try:
            os.replace(src_dir, dst_dir)
            return
        except PermissionError:
            if attempt + 1 >= _PROMOTION_RETRY_ATTEMPTS:
                raise
            time.sleep(_PROMOTION_RETRY_DELAY_SECONDS)


def _write_pointer(path: Path, *, run_id: str, manifest_hash: str) -> None:
    _write_atomic_json(path, {"run_id": run_id, "manifest_hash": manifest_hash})


def commit_staging_run(
    manifest: GatherRunManifest,
    *,
    finished_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
) -> GatherRunManifest:
    """Sec 4.6 steps 5-8: hash, finalize as ``committed``, atomically promote
    ``staging/<run_id>`` to ``committed/<run_id>``, and update the pointers.
    Step 9 (lease release) is the caller's responsibility — this function has
    no lease dependency beyond recording ``lease_owner``/``lease_fencing_token``
    already present on *manifest*. ``finished_at`` is caller-supplied (this
    Zone-A module never reads the wall clock itself). Callers must set
    *manifest*'s ``ado_items_hash``/``query_results_hash`` (via
    ``hash_ado_items``/``hash_query_results``) before calling this, and must
    have already written any ``ado_items.jsonl``/``query_results.json``
    sidecars into the staging directory so they are carried over by the
    directory promotion."""
    committed = _finalize(manifest, GatherRunStatus.COMMITTED, finished_at=finished_at)
    committed_hash = compute_manifest_hash(committed)
    committed = _with_manifest_hash(committed, committed_hash)

    staging_dir = get_staging_run_dir(manifest.program_id, manifest.run_id, programs_root=programs_root)
    write_manifest(staging_dir, committed)

    committed_dir = get_committed_run_dir(manifest.program_id, manifest.run_id, programs_root=programs_root)
    _promote_dir(staging_dir, committed_dir)

    _write_pointer(
        get_latest_pointer_path(manifest.program_id, programs_root=programs_root),
        run_id=committed.run_id,
        manifest_hash=committed_hash,
    )
    if committed.required_scope_status is RequiredScopeStatus.FULL:
        _write_pointer(
            get_latest_full_pointer_path(manifest.program_id, programs_root=programs_root),
            run_id=committed.run_id,
            manifest_hash=committed_hash,
        )
    return committed


def fail_staging_run(
    manifest: GatherRunManifest,
    *,
    finished_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
) -> GatherRunManifest:
    """Sec 4.6's handled-failure path: write ``status=failed`` and move the
    staging directory to ``failed/<run_id>``. Failed/quarantined runs never
    advance either pointer (D-14). ``finished_at`` is caller-supplied."""
    failed = _finalize(manifest, GatherRunStatus.FAILED, finished_at=finished_at)
    failed = _with_manifest_hash(failed, compute_manifest_hash(failed))

    staging_dir = get_staging_run_dir(manifest.program_id, manifest.run_id, programs_root=programs_root)
    write_manifest(staging_dir, failed)

    failed_dir = get_failed_run_dir(manifest.program_id, manifest.run_id, programs_root=programs_root)
    _promote_dir(staging_dir, failed_dir)
    return failed


def _finalize(manifest: GatherRunManifest, status: GatherRunStatus, *, finished_at: datetime) -> GatherRunManifest:
    return replace(manifest, status=status, finished_at=finished_at)


def _with_manifest_hash(manifest: GatherRunManifest, manifest_hash: str) -> GatherRunManifest:
    return replace(manifest, manifest_hash=manifest_hash)


# ---------------------------------------------------------------------------
# Pointer reads with corrupt-pointer fallback scan (D-14)
# ---------------------------------------------------------------------------


def _read_pointer(path: Path) -> tuple[str, str] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return str(payload["run_id"]), str(payload["manifest_hash"])
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def _scan_newest_hash_valid_committed(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> GatherRunManifest | None:
    committed_root = get_committed_root(program_id, programs_root=programs_root)
    if not committed_root.exists():
        return None
    candidates: list[GatherRunManifest] = []
    for run_dir in committed_root.iterdir():
        if not run_dir.is_dir():
            continue
        try:
            candidate = read_manifest(run_dir)
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            continue
        if candidate.manifest_hash and compute_manifest_hash(candidate) == candidate.manifest_hash:
            candidates.append(candidate)
    if not candidates:
        return None
    return max(candidates, key=lambda m: (m.finished_at or m.started_at))


def resolve_latest_committed_manifest(
    program_id: str, *, programs_root: Path = PROGRAMS_ROOT
) -> GatherRunManifest | None:
    """D-14: newest committed run of any non-corrupt outcome. Falls back to
    scanning ``committed/*/manifest.json`` for the newest hash-valid candidate
    when the ``latest.json`` pointer itself is missing or corrupt."""
    pointer = _read_pointer(get_latest_pointer_path(program_id, programs_root=programs_root))
    if pointer is not None:
        run_id, manifest_hash = pointer
        run_dir = get_committed_run_dir(program_id, run_id, programs_root=programs_root)
        try:
            candidate = read_manifest(run_dir)
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            candidate = None
        if (
            candidate is not None
            and candidate.manifest_hash == manifest_hash
            and compute_manifest_hash(candidate) == candidate.manifest_hash
        ):
            return candidate
    return _scan_newest_hash_valid_committed(program_id, programs_root=programs_root)


def resolve_latest_full_committed_manifest(
    program_id: str, *, programs_root: Path = PROGRAMS_ROOT
) -> GatherRunManifest | None:
    """D-14: newest committed run whose required scope was ``FULL`` at commit
    time. Same corrupt-pointer fallback as ``resolve_latest_committed_manifest``,
    restricted to ``required_scope_status == FULL`` candidates."""
    pointer = _read_pointer(get_latest_full_pointer_path(program_id, programs_root=programs_root))
    if pointer is not None:
        run_id, manifest_hash = pointer
        run_dir = get_committed_run_dir(program_id, run_id, programs_root=programs_root)
        try:
            candidate = read_manifest(run_dir)
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            candidate = None
        if (
            candidate is not None
            and candidate.manifest_hash == manifest_hash
            and candidate.required_scope_status is RequiredScopeStatus.FULL
            and compute_manifest_hash(candidate) == candidate.manifest_hash
        ):
            return candidate
    committed_root = get_committed_root(program_id, programs_root=programs_root)
    if not committed_root.exists():
        return None
    candidates: list[GatherRunManifest] = []
    for run_dir in committed_root.iterdir():
        if not run_dir.is_dir():
            continue
        try:
            candidate = read_manifest(run_dir)
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            continue
        if (
            candidate.manifest_hash
            and candidate.required_scope_status is RequiredScopeStatus.FULL
            and compute_manifest_hash(candidate) == candidate.manifest_hash
        ):
            candidates.append(candidate)
    if not candidates:
        return None
    return max(candidates, key=lambda m: (m.finished_at or m.started_at))


def validate_pinned_gather_run(
    program_id: str,
    *,
    gather_run_id: str | None,
    gather_run_hash: str | None,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[str, ...]:
    """D-17/ARM-GATHER-11 (AG-6.2): confirm must reject invalid/stale/partial
    pinned gather runs rather than silently archiving against one. Returns a
    tuple of ``BLOCKED: ...`` failure messages in the same convention confirm
    already uses for QG/stale-approval failures; empty means the pinned run
    (if any) is valid.

    A ``None`` gather_run_id is treated as not-applicable (not a failure) --
    this covers drafts created before D-17 and programs with no gather-run
    pipeline wired in yet, so pre-existing editions keep working unchanged.
    """
    if gather_run_id is None:
        return ()

    run_dir = get_committed_run_dir(program_id, gather_run_id, programs_root=programs_root)
    try:
        pinned = read_manifest(run_dir)
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return (
            f"BLOCKED: Pinned gather run {gather_run_id} is invalid: no readable "
            "committed manifest found. Re-run 'vertex report' to pin a current run.",
        )

    if compute_manifest_hash(pinned) != pinned.manifest_hash or (
        gather_run_hash is not None and pinned.manifest_hash != gather_run_hash
    ):
        return (
            f"BLOCKED: Pinned gather run {gather_run_id} failed hash verification -- the "
            "committed manifest may have been altered or corrupted. Re-run 'vertex report' "
            "to pin a current, verified run.",
        )

    if pinned.required_scope_status is RequiredScopeStatus.PARTIAL:
        return (
            f"BLOCKED: Pinned gather run {gather_run_id} was PARTIAL scope (query capture "
            "skew exceeded the D-18 tolerance). Confirm requires a FULL-scope committed run; "
            "re-run 'vertex gather' then 'vertex report' before confirming.",
        )

    latest_full = resolve_latest_full_committed_manifest(program_id, programs_root=programs_root)
    if latest_full is not None and latest_full.run_id != gather_run_id:
        return (
            f"BLOCKED: Pinned gather run {gather_run_id} is stale -- a newer FULL-scope "
            f"committed run ({latest_full.run_id}) exists. Re-run 'vertex report' to refresh "
            "the draft's pinned run before confirming.",
        )

    return ()


# ---------------------------------------------------------------------------
# D-13 rule 8: recovery scan for abandoned ``running`` manifests
# ---------------------------------------------------------------------------


def _default_is_lease_current(manifest: GatherRunManifest, *, programs_root: Path) -> bool:
    lease = read_lease_state(
        manifest.program_id,
        mutation_domain=GATHER_MUTATION_DOMAIN,
        programs_root=programs_root,
    )
    if lease is None:
        return False
    if lease.owner != manifest.lease_owner or lease.fencing_token != manifest.lease_fencing_token:
        return False
    return not is_lease_expired(lease)


def quarantine_abandoned_staging_runs(
    program_id: str,
    *,
    finished_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
    is_lease_current_fn: Callable[[GatherRunManifest], bool] | None = None,
) -> list[GatherRunManifest]:
    """D-13 rule 8: at startup, scan ``staging/*`` for ``running`` manifests
    whose lease is no longer current and move them to ``quarantine/<run_id>``,
    writing an ``orphan_index.json`` sidecar and returning the quarantined
    manifests. Manifests whose lease is still current (a genuinely in-flight
    run on another process) are left untouched.

    ``is_lease_current_fn`` defaults to checking the shared ``workspace_lease``
    store for the ``gather`` mutation domain; callers may inject a fake for
    tests or an alternate lease backend. ``finished_at`` is caller-supplied.
    """
    check_fn = is_lease_current_fn or (lambda m: _default_is_lease_current(m, programs_root=programs_root))

    staging_root = get_gather_runs_dir(program_id, programs_root=programs_root) / STAGING_SUBDIR
    if not staging_root.exists():
        return []

    quarantined: list[GatherRunManifest] = []
    for run_dir in sorted(staging_root.iterdir()):
        if not run_dir.is_dir():
            continue
        try:
            manifest = read_manifest(run_dir)
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            continue
        if manifest.status is not GatherRunStatus.RUNNING:
            continue
        if check_fn(manifest):
            continue  # lease still current: a genuinely in-flight run elsewhere

        quarantined_manifest = _finalize(manifest, GatherRunStatus.QUARANTINED, finished_at=finished_at)
        quarantined_manifest = _with_manifest_hash(quarantined_manifest, compute_manifest_hash(quarantined_manifest))
        write_manifest(run_dir, quarantined_manifest)
        _write_atomic_json(
            _orphan_index_path(run_dir),
            {
                "run_id": manifest.run_id,
                "lease_owner": manifest.lease_owner,
                "lease_fencing_token": manifest.lease_fencing_token,
                "reason": "abandoned_running_manifest_lease_not_current",
            },
        )
        quarantine_dir = get_quarantine_run_dir(program_id, manifest.run_id, programs_root=programs_root)
        _promote_dir(run_dir, quarantine_dir)
        quarantined.append(quarantined_manifest)
    return quarantined
