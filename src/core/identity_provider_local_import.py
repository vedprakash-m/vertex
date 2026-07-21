"""specs/people.md Phase 4, PPL-W4.2: the accepted first `IdentityDirectoryProvider`
adapter -- local file import (§6.8, decision record accepted 2026-07-20).

Reads an operator-exported structured directory snapshot (CSV or JSON,
detected by file extension) from an EXPLICIT operator-supplied path,
mirroring `vertex rev`'s established `--docs-inbox <dir>` convention
(`src/commands/rev.py`) of an explicit path argument rather than a
hardcoded/assumed inbox directory -- confirmed by reading that command's
actual CLI option before choosing this shape, not assumed. No live API
calls, no delegated Graph scopes, no token/credential handling at all;
this module is Zone C-shaped (an adapter) but currently lives beside its
Zone A port for simplicity since it has zero Zone B/C dependencies of its
own (only `csv`/`json`/`hashlib`, stdlib).

Export column/field vocabulary is deliberately the SAME field-name
strings `people_registry_governance.py::_PERSON_FIELDS` already uses for
person-field patches (`display_name`, `title`, `department`, `contacts`),
so a later stage (PPL-W4.4's writer integration) can consume
`FieldObservation.field_name` values directly with no translation table.
Two fields are deliberately named differently from their eventual
canonical target: `manager_alias` (not `manager_entity_id`) and
`teams` (membership, not a person field at all) -- both need identity
RESOLUTION (alias/team-name -> canonical `entity_id`) that requires the
full loaded registry, which this adapter -- a per-file, registry-unaware
reader -- correctly does not have. This mirrors the exact same "observed
but not yet resolved" pattern `people_directory_schema.py`'s legacy
dual-read already established for `manager_alias` (a WARN diagnostic,
"present but not resolved to manager_entity_id" -- PPL-W2A.2), not a new
convention invented here.

`source_version`/`etag` are the export file's content hash (via the
sanctioned `jsonl_utils.compute_file_checksum` seam) and modification
timestamp -- there is no live ETag to track for a local file.
`capabilities().supports_delta` is `False`; `continuation_token`/
`delta_token` are always `None` on both the request and response side,
since a full local file is read in one pass, never paginated.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.identity_provider_port import (
    FieldObservation,
    FieldValue,
    IdentityLookupRequest,
    IdentityObservation,
    MembershipObservation,
    ObservationState,
    ProviderBatchResult,
    ProviderCapabilities,
    ProviderItemError,
)
from src.core.jsonl_utils import compute_file_checksum

CAPABILITY_CONTRACT_VERSION = "1.0"
SUPPORTED_ENTITY_TYPES = ("person",)
#: The exact field-name vocabulary `people_registry_governance.py::_PERSON_FIELDS`
#: already uses -- kept in sync deliberately, not duplicated by accident.
SUPPORTED_PERSON_FIELDS = ("display_name", "title", "department", "contacts")
#: Observed-but-unresolved fields: identity resolution (alias/team-name ->
#: canonical entity_id) is a later stage's job, not this file-reader's.
_UNRESOLVED_FIELDS = ("manager_alias",)
_TEAM_COLUMN = "teams"
_ALIAS_COLUMN = "alias"


@dataclass(frozen=True, slots=True)
class _DirectoryRow:
    row_index: int
    alias: str
    display_name: str | None
    title: str | None
    department: str | None
    manager_alias: str | None
    email: str | None
    teams: tuple[str, ...]


def _split_teams(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    text = str(raw).strip()
    if not text:
        return ()
    return tuple(part.strip() for part in text.replace(",", ";").split(";") if part.strip())


def _row_from_mapping(raw: dict, *, row_index: int) -> _DirectoryRow | None:
    alias = str(raw.get(_ALIAS_COLUMN) or "").strip()
    if not alias:
        return None
    return _DirectoryRow(
        row_index=row_index,
        alias=alias,
        display_name=(str(raw["display_name"]).strip() or None) if raw.get("display_name") else None,
        title=(str(raw["title"]).strip() or None) if raw.get("title") else None,
        department=(str(raw["department"]).strip() or None) if raw.get("department") else None,
        manager_alias=(str(raw["manager_alias"]).strip() or None) if raw.get("manager_alias") else None,
        email=(str(raw["email"]).strip() or None) if raw.get("email") else None,
        teams=_split_teams(raw.get(_TEAM_COLUMN)),
    )


def _parse_export_file(path: Path) -> tuple[tuple[_DirectoryRow, ...], tuple[ProviderItemError, ...]]:
    suffix = path.suffix.lower()
    rows: list[_DirectoryRow] = []
    errors: list[ProviderItemError] = []

    if suffix == ".json":
        try:
            raw_records = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as error:
            errors.append(ProviderItemError(request_id=None, code="export_unreadable", retryable=False, detail=str(error)))
            return (), tuple(errors)
        if not isinstance(raw_records, list):
            errors.append(ProviderItemError(request_id=None, code="export_malformed", retryable=False, detail="Expected a JSON array of directory rows."))
            return (), tuple(errors)
        for index, raw_record in enumerate(raw_records):
            if not isinstance(raw_record, dict):
                errors.append(ProviderItemError(request_id=None, code="row_malformed", retryable=False, detail=f"Row {index} is not a JSON object."))
                continue
            row = _row_from_mapping(raw_record, row_index=index)
            if row is None:
                errors.append(ProviderItemError(request_id=None, code="row_missing_alias", retryable=False, detail=f"Row {index} has no 'alias'."))
                continue
            rows.append(row)
        return tuple(rows), tuple(errors)

    if suffix == ".csv":
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as error:
            errors.append(ProviderItemError(request_id=None, code="export_unreadable", retryable=False, detail=str(error)))
            return (), tuple(errors)
        reader = csv.DictReader(io.StringIO(text))
        for index, raw_record in enumerate(reader):
            row = _row_from_mapping(raw_record, row_index=index)
            if row is None:
                errors.append(ProviderItemError(request_id=None, code="row_missing_alias", retryable=False, detail=f"Row {index} has no 'alias'."))
                continue
            rows.append(row)
        return tuple(rows), tuple(errors)

    errors.append(ProviderItemError(request_id=None, code="export_unsupported_format", retryable=False, detail=f"Unsupported export file extension: {suffix!r} (expected .csv or .json)."))
    return (), tuple(errors)


def _row_to_observation(row: _DirectoryRow, *, request_id: str, provider: str, tenant_id: str, now: datetime) -> IdentityObservation:
    fields: list[FieldObservation] = []
    complete_fields: list[str] = []

    def add(field_name: str, value: FieldValue) -> None:
        if value is None:
            return
        fields.append(
            FieldObservation(
                field_name=field_name, state=ObservationState.PRESENT, value=value,
                source_ref=None, source_version=None, observed_at=now,
            )
        )
        complete_fields.append(field_name)

    add("display_name", row.display_name)
    add("title", row.title)
    add("department", row.department)
    add("contacts", row.email)
    add("manager_alias", row.manager_alias)

    return IdentityObservation(
        request_id=request_id, provider=provider, tenant_id=tenant_id, provider_subject_id=row.alias,
        source_version=None, etag=None, entity_type="person", state=ObservationState.PRESENT,
        complete=True, complete_fields=tuple(complete_fields), provider_status_raw=None, fields=tuple(fields),
    )


class LocalDirectoryExportProvider:
    """The accepted PPL-W4.2 first adapter. `export_path` is supplied by
    the CALLER (PPL-W4.4's CLI command, via an explicit `--import-file`
    option) -- this class never assumes or discovers a file location on
    its own."""

    def __init__(self, *, export_path: Path, provider_name: str, tenant_id: str) -> None:
        self._export_path = export_path
        self._provider_name = provider_name
        self._tenant_id = tenant_id

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self._provider_name, contract_version=CAPABILITY_CONTRACT_VERSION,
            supported_entity_types=SUPPORTED_ENTITY_TYPES,
            supported_fields=SUPPORTED_PERSON_FIELDS + _UNRESOLVED_FIELDS,
            supports_membership_snapshot=True, supports_delta=False, authoritative_lifecycle=False,
        )

    def _source_version(self) -> str | None:
        if not self._export_path.exists():
            return None
        return compute_file_checksum(self._export_path)

    def _etag(self) -> str | None:
        if not self._export_path.exists():
            return None
        mtime = self._export_path.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()

    def fetch_people(
        self,
        requests: tuple[IdentityLookupRequest, ...],
        *,
        continuation_token: str | None = None,
        delta_token: str | None = None,
    ) -> ProviderBatchResult:
        now = datetime.now(timezone.utc)
        if not self._export_path.exists():
            return ProviderBatchResult(
                provider=self._provider_name, tenant_id=self._tenant_id, capability_version=CAPABILITY_CONTRACT_VERSION,
                fetched_at=now, snapshot_id=None, complete=False, observations=(), memberships=(),
                errors=(ProviderItemError(request_id=None, code="export_missing", retryable=True, detail=f"No export file found at {self._export_path}."),),
            )

        rows, parse_errors = _parse_export_file(self._export_path)
        rows_by_alias = {row.alias.casefold(): row for row in rows}

        observations: list[IdentityObservation] = []
        errors: list[ProviderItemError] = list(parse_errors)
        for request in requests:
            lookup_key = (request.provider_subject_id or request.alias_hint or "").strip().casefold()
            if not lookup_key:
                errors.append(ProviderItemError(request_id=request.request_id, code="no_lookup_key", retryable=False, detail="Request has neither provider_subject_id nor alias_hint."))
                continue
            row = rows_by_alias.get(lookup_key)
            if row is None:
                observations.append(
                    IdentityObservation(
                        request_id=request.request_id, provider=self._provider_name, tenant_id=self._tenant_id,
                        provider_subject_id=None, source_version=self._source_version(), etag=self._etag(),
                        entity_type="person", state=ObservationState.NOT_FOUND, complete=True, complete_fields=(),
                        provider_status_raw=None, fields=(),
                    )
                )
                continue
            observations.append(
                _row_to_observation(row, request_id=request.request_id, provider=self._provider_name, tenant_id=self._tenant_id, now=now)
            )

        return ProviderBatchResult(
            provider=self._provider_name, tenant_id=self._tenant_id, capability_version=CAPABILITY_CONTRACT_VERSION,
            fetched_at=now, snapshot_id=self._source_version(), complete=not parse_errors,
            observations=tuple(observations), memberships=(), errors=tuple(errors),
        )

    def fetch_team_memberships(
        self,
        team_subject_ids: tuple[str, ...],
        *,
        continuation_token: str | None = None,
        delta_token: str | None = None,
    ) -> ProviderBatchResult:
        now = datetime.now(timezone.utc)
        if not self._export_path.exists():
            return ProviderBatchResult(
                provider=self._provider_name, tenant_id=self._tenant_id, capability_version=CAPABILITY_CONTRACT_VERSION,
                fetched_at=now, snapshot_id=None, complete=False, observations=(), memberships=(),
                errors=(ProviderItemError(request_id=None, code="export_missing", retryable=True, detail=f"No export file found at {self._export_path}."),),
            )

        rows, parse_errors = _parse_export_file(self._export_path)
        requested = {team_id.casefold() for team_id in team_subject_ids} if team_subject_ids else None
        snapshot_id = self._source_version()

        memberships: list[MembershipObservation] = []
        for row in rows:
            for team_id in row.teams:
                if requested is not None and team_id.casefold() not in requested:
                    continue
                memberships.append(
                    MembershipObservation(
                        provider=self._provider_name, tenant_id=self._tenant_id, person_subject_id=row.alias,
                        team_subject_id=team_id, source_version=snapshot_id, state=ObservationState.PRESENT,
                        snapshot_id=snapshot_id, team_snapshot_complete=not parse_errors, role=None, observed_at=now,
                    )
                )

        return ProviderBatchResult(
            provider=self._provider_name, tenant_id=self._tenant_id, capability_version=CAPABILITY_CONTRACT_VERSION,
            fetched_at=now, snapshot_id=snapshot_id, complete=not parse_errors, observations=(),
            memberships=tuple(memberships), errors=tuple(parse_errors),
        )
