from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.identity_provider_port import (
    FieldObservation,
    IdentityLookupRequest,
    IdentityObservation,
    IdentityProviderConfig,
    ObservationState,
    ProviderBatchResult,
)
from src.core.identity_provider_validation import (
    ACCEPTED,
    QUARANTINED,
    REJECTED,
    UNRESOLVED,
    IdentitySourceAuthorityPolicy,
    load_identity_source_authority_policy,
    quarantine_field_observation,
    validate_and_route_observations,
)
from src.core.people_change_journal import STREAM_PEOPLE_CONFLICTS, read_journal_records

NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def _provider_config(*, allowed_fields: tuple[str, ...]) -> IdentityProviderConfig:
    return IdentityProviderConfig(
        name="acme_directory_export", provider_type="local_directory_export", tenant_id="acme-tenant",
        endpoint_profile=None, capability_contract_version="1.0", allowed_fields=allowed_fields,
        secret_ref=None, timeout_seconds=30, rate_limit_per_minute=60, enabled=False,
    )


def _policy(threshold: float = 0.95) -> IdentitySourceAuthorityPolicy:
    return IdentitySourceAuthorityPolicy(schema_version="1", provider_priority=(), auto_accept_confidence_threshold=threshold)


def _field(name: str, *, confidence: float = 1.0, value: object = "value") -> FieldObservation:
    return FieldObservation(field_name=name, state=ObservationState.PRESENT, value=value, source_ref=None, source_version=None, observed_at=NOW, confidence=confidence)


def _observation(request_id: str, *, fields: tuple[FieldObservation, ...], state: ObservationState = ObservationState.PRESENT) -> IdentityObservation:
    return IdentityObservation(
        request_id=request_id, provider="acme_directory_export", tenant_id="acme-tenant", provider_subject_id="jdoe",
        source_version=None, etag=None, entity_type="person", state=state, complete=True,
        complete_fields=tuple(f.field_name for f in fields), provider_status_raw=None, fields=fields,
    )


def _batch(observations: tuple[IdentityObservation, ...]) -> ProviderBatchResult:
    return ProviderBatchResult(
        provider="acme_directory_export", tenant_id="acme-tenant", capability_version="1.0", fetched_at=NOW,
        snapshot_id="abc", complete=True, observations=observations, memberships=(), errors=(),
    )


def test_high_confidence_allowlisted_field_is_accepted() -> None:
    config = _provider_config(allowed_fields=("display_name",))
    requests = (IdentityLookupRequest(request_id="r1", entity_id="person:1", provider_subject_id="jdoe", alias_hint=None, requested_fields=()),)
    batch = _batch((_observation("r1", fields=(_field("display_name", confidence=1.0),)),))

    results = validate_and_route_observations(batch, requests=requests, provider_config=config, policy=_policy())

    assert len(results) == 1
    assert results[0].outcome == ACCEPTED
    assert results[0].entity_id == "person:1"


def test_low_confidence_allowlisted_field_is_quarantined() -> None:
    config = _provider_config(allowed_fields=("display_name",))
    requests = (IdentityLookupRequest(request_id="r1", entity_id="person:1", provider_subject_id="jdoe", alias_hint=None, requested_fields=()),)
    batch = _batch((_observation("r1", fields=(_field("display_name", confidence=0.5),)),))

    results = validate_and_route_observations(batch, requests=requests, provider_config=config, policy=_policy())

    assert results[0].outcome == QUARANTINED
    assert "below the auto-accept threshold" in results[0].reason


def test_field_outside_allowlist_is_rejected() -> None:
    config = _provider_config(allowed_fields=("title",))
    requests = (IdentityLookupRequest(request_id="r1", entity_id="person:1", provider_subject_id="jdoe", alias_hint=None, requested_fields=()),)
    batch = _batch((_observation("r1", fields=(_field("display_name", confidence=1.0),)),))

    results = validate_and_route_observations(batch, requests=requests, provider_config=config, policy=_policy())

    assert results[0].outcome == REJECTED
    assert "allowed_fields" in results[0].reason


def test_manager_alias_is_unresolved_not_rejected_or_quarantined() -> None:
    config = _provider_config(allowed_fields=("manager_alias",))
    requests = (IdentityLookupRequest(request_id="r1", entity_id="person:1", provider_subject_id="jdoe", alias_hint=None, requested_fields=()),)
    batch = _batch((_observation("r1", fields=(_field("manager_alias", confidence=1.0, value="mgr1"),)),))

    results = validate_and_route_observations(batch, requests=requests, provider_config=config, policy=_policy())

    assert results[0].outcome == UNRESOLVED


def test_unregistered_field_within_allowlist_is_rejected() -> None:
    config = _provider_config(allowed_fields=("not_a_real_field",))
    requests = (IdentityLookupRequest(request_id="r1", entity_id="person:1", provider_subject_id="jdoe", alias_hint=None, requested_fields=()),)
    batch = _batch((_observation("r1", fields=(_field("not_a_real_field", confidence=1.0),)),))

    results = validate_and_route_observations(batch, requests=requests, provider_config=config, policy=_policy())

    assert results[0].outcome == REJECTED
    assert "not a registered Zone A person field" in results[0].reason


def test_out_of_range_confidence_is_rejected() -> None:
    config = _provider_config(allowed_fields=("display_name",))
    requests = (IdentityLookupRequest(request_id="r1", entity_id="person:1", provider_subject_id="jdoe", alias_hint=None, requested_fields=()),)
    batch = _batch((_observation("r1", fields=(_field("display_name", confidence=1.5),)),))

    results = validate_and_route_observations(batch, requests=requests, provider_config=config, policy=_policy())

    assert results[0].outcome == REJECTED
    assert "valid [0.0, 1.0] range" in results[0].reason


def test_not_found_observation_produces_no_field_results() -> None:
    config = _provider_config(allowed_fields=("display_name",))
    requests = (IdentityLookupRequest(request_id="r1", entity_id=None, provider_subject_id="ghost", alias_hint=None, requested_fields=()),)
    batch = _batch((_observation("r1", fields=(), state=ObservationState.NOT_FOUND),))

    results = validate_and_route_observations(batch, requests=requests, provider_config=config, policy=_policy())

    assert results == ()


def test_load_identity_source_authority_policy_platform_default() -> None:
    policy = load_identity_source_authority_policy()

    assert policy.auto_accept_confidence_threshold == 0.95
    assert policy.provider_priority == ()


def test_load_identity_source_authority_policy_applies_knowledge_root_override(tmp_path: Path) -> None:
    policies_dir = tmp_path / "policies"
    policies_dir.mkdir()
    (policies_dir / "identity_source_authority.yaml").write_text(
        (
            "identity_source_authority_override:\n"
            "  auto_accept_confidence_threshold: 0.8\n"
            "  provider_priority:\n"
            "    - acme_directory_export\n"
        ),
        encoding="utf-8",
    )

    policy = load_identity_source_authority_policy(knowledge_root=tmp_path)

    assert policy.auto_accept_confidence_threshold == 0.8
    assert policy.provider_priority == ("acme_directory_export",)


def test_load_identity_source_authority_policy_no_override_file_uses_platform_default(tmp_path: Path) -> None:
    policy = load_identity_source_authority_policy(knowledge_root=tmp_path)

    assert policy.auto_accept_confidence_threshold == 0.95


def test_quarantine_field_observation_appends_a_real_conflict_record(tmp_path: Path) -> None:
    observation = validate_and_route_observations(
        _batch((_observation("r1", fields=(_field("display_name", confidence=0.4),)),)),
        requests=(IdentityLookupRequest(request_id="r1", entity_id="person:1", provider_subject_id="jdoe", alias_hint=None, requested_fields=()),),
        provider_config=_provider_config(allowed_fields=("display_name",)),
        policy=_policy(),
    )[0]
    assert observation.outcome == QUARANTINED

    record = quarantine_field_observation(
        tmp_path, workspace_id="workspace:1", provider="acme_directory_export", refresh_run_id="run-1",
        observation=observation, actor="steward@example.com", as_of=NOW,
    )

    assert record["decision"] == "quarantined"
    assert record["entity_id"] == "person:1"
    records = read_journal_records(tmp_path, STREAM_PEOPLE_CONFLICTS)
    assert len(records) == 1
    assert records[0]["conflict_id"] == "run-1-r1-display_name"
