"""specs/people.md Phase 4, PPL-W4.1: `IdentityDirectoryProvider` port
(§6.8, verbatim binding shapes).

§6.8's own Python code block is the binding contract -- every dataclass
field name/type/default below is transcribed exactly as specified, not
redesigned. This module is the provider-neutral Zone A port; concrete
adapters (Zone C) implement `IdentityDirectoryProvider` without this
module knowing which adapter is in use. PPL-W4.2 ships the first
(local file-import) adapter; a future live-API adapter implements the
SAME `Protocol` with no change here, per §6.8's own accepted first-
adapter decision (2026-07-20): Microsoft IT permanently blocks delegated
Graph API scopes for custom Entra app registrations in this tenant,
confirmed to include directory/people-read scopes, so a live Microsoft
Graph adapter is not viable as the first adapter.

`identity_providers.yaml` (§6.6/§6.8/§6.9, schema 1.0) stores provider
type, tenant identifier, endpoint/profile name, allowed fields,
capability-contract version, secret REFERENCES, and timeout/rate
budgets -- NEVER tokens or client secrets (confirmed by reading the real
`knowledge/identity_providers.example.yaml` fixture before writing this
loader: its `secret_ref` field is a `keyvault://...` reference string,
never a resolved value). `load_identity_providers_document` round-trips
that exact fixture.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Protocol

from src.core.exceptions import ConfigError
from src.core.yaml_utils import fast_safe_load

IDENTITY_PROVIDERS_SCHEMA_VERSION = "1.0"


class ObservationState(str, Enum):
    PRESENT = "present"
    NOT_FOUND = "not_found"
    DEPARTED = "departed"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    provider: str
    contract_version: str
    supported_entity_types: tuple[str, ...]
    supported_fields: tuple[str, ...]
    supports_membership_snapshot: bool
    supports_delta: bool
    authoritative_lifecycle: bool


@dataclass(frozen=True, slots=True)
class IdentityLookupRequest:
    request_id: str
    entity_id: str | None
    provider_subject_id: str | None
    alias_hint: str | None
    requested_fields: tuple[str, ...]


FieldValue = str | int | float | bool | datetime | tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class FieldObservation:
    field_name: str
    state: ObservationState
    value: FieldValue
    source_ref: str | None
    source_version: str | None
    observed_at: datetime
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class IdentityObservation:
    request_id: str
    provider: str
    tenant_id: str
    provider_subject_id: str | None
    source_version: str | None
    etag: str | None
    entity_type: str
    state: ObservationState
    complete: bool
    complete_fields: tuple[str, ...]
    provider_status_raw: str | None
    fields: tuple[FieldObservation, ...]


@dataclass(frozen=True, slots=True)
class MembershipObservation:
    provider: str
    tenant_id: str
    person_subject_id: str
    team_subject_id: str
    source_version: str | None
    state: ObservationState
    snapshot_id: str | None
    team_snapshot_complete: bool
    role: str | None
    observed_at: datetime
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    source_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderItemError:
    request_id: str | None
    code: str
    retryable: bool
    detail: str
    retry_after_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ProviderBatchResult:
    provider: str
    tenant_id: str
    capability_version: str
    fetched_at: datetime
    snapshot_id: str | None
    complete: bool
    observations: tuple[IdentityObservation, ...]
    memberships: tuple[MembershipObservation, ...]
    errors: tuple[ProviderItemError, ...]
    continuation_token: str | None = None
    delta_token: str | None = None
    retry_after_seconds: float | None = None


class IdentityDirectoryProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...

    def fetch_people(
        self,
        requests: tuple[IdentityLookupRequest, ...],
        *,
        continuation_token: str | None = None,
        delta_token: str | None = None,
    ) -> ProviderBatchResult: ...

    def fetch_team_memberships(
        self,
        team_subject_ids: tuple[str, ...],
        *,
        continuation_token: str | None = None,
        delta_token: str | None = None,
    ) -> ProviderBatchResult: ...


# ---------------------------------------------------------------------------
# identity_providers.yaml (§6.6/§6.8/§6.9), schema 1.0.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IdentityProviderConfig:
    name: str
    provider_type: str
    tenant_id: str
    endpoint_profile: str | None
    capability_contract_version: str
    allowed_fields: tuple[str, ...]
    secret_ref: str | None
    timeout_seconds: int
    rate_limit_per_minute: int
    enabled: bool


@dataclass(frozen=True, slots=True)
class IdentityProvidersDocument:
    schema_version: str
    providers: tuple[IdentityProviderConfig, ...]


def _provider_from_payload(raw: dict, *, path: Path) -> IdentityProviderConfig:
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ConfigError(f"{path}: a provider entry is missing 'name'.")
    provider_type = str(raw.get("provider_type") or "").strip()
    if not provider_type:
        raise ConfigError(f"{path}: provider {name!r} is missing 'provider_type'.")
    tenant_id = str(raw.get("tenant_id") or "").strip()
    if not tenant_id:
        raise ConfigError(f"{path}: provider {name!r} is missing 'tenant_id'.")
    capability_contract_version = str(raw.get("capability_contract_version") or "").strip()
    if not capability_contract_version:
        raise ConfigError(f"{path}: provider {name!r} is missing 'capability_contract_version'.")
    return IdentityProviderConfig(
        name=name,
        provider_type=provider_type,
        tenant_id=tenant_id,
        endpoint_profile=raw.get("endpoint_profile"),
        capability_contract_version=capability_contract_version,
        allowed_fields=tuple(raw.get("allowed_fields") or ()),
        secret_ref=raw.get("secret_ref"),
        timeout_seconds=int(raw.get("timeout_seconds", 30)),
        rate_limit_per_minute=int(raw.get("rate_limit_per_minute", 60)),
        enabled=bool(raw.get("enabled", False)),
    )


def _provider_to_payload(provider: IdentityProviderConfig) -> dict:
    return {
        "name": provider.name,
        "provider_type": provider.provider_type,
        "tenant_id": provider.tenant_id,
        "endpoint_profile": provider.endpoint_profile,
        "capability_contract_version": provider.capability_contract_version,
        "allowed_fields": list(provider.allowed_fields),
        "secret_ref": provider.secret_ref,
        "timeout_seconds": provider.timeout_seconds,
        "rate_limit_per_minute": provider.rate_limit_per_minute,
        "enabled": provider.enabled,
    }


def load_identity_providers_document(path: Path) -> IdentityProvidersDocument | None:
    if not path.exists():
        return None
    raw = fast_safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    schema_version = str(raw.get("schema_version") or "")
    if schema_version.split(".", 1)[0] != IDENTITY_PROVIDERS_SCHEMA_VERSION.split(".", 1)[0]:
        raise ConfigError(
            f"{path}: expected identity_providers.yaml schema_version major "
            f"{IDENTITY_PROVIDERS_SCHEMA_VERSION.split('.', 1)[0]}, got {schema_version or '<missing>'}."
        )
    raw_providers = raw.get("providers") or []
    if not isinstance(raw_providers, list):
        raise ConfigError(f"{path}: 'providers' must be a list.")
    providers = tuple(_provider_from_payload(entry, path=path) for entry in raw_providers)
    names_seen: set[str] = set()
    for provider in providers:
        if provider.name in names_seen:
            raise ConfigError(f"{path}: duplicate provider name {provider.name!r}.")
        names_seen.add(provider.name)
    return IdentityProvidersDocument(schema_version=schema_version, providers=providers)


def find_provider_config(document: IdentityProvidersDocument | None, name: str) -> IdentityProviderConfig | None:
    if document is None:
        return None
    return next((provider for provider in document.providers if provider.name == name), None)
