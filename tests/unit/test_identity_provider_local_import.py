from __future__ import annotations

import json
from pathlib import Path

from src.core.identity_provider_local_import import LocalDirectoryExportProvider
from src.core.identity_provider_port import IdentityLookupRequest, ObservationState

CSV_EXPORT = (
    "alias,display_name,title,department,manager_alias,email,teams\n"
    "jdoe,Jane Doe,Principal TPM,Platform,mgr1,jdoe@example.com,platform-core;platform-infra\n"
    "asmith,Amy Smith,EM,Growth,jdoe,asmith@example.com,growth-team\n"
    "norow,,,,,,,\n"  # malformed: too many columns, but has no alias so it's a row-missing-alias case below
)

JSON_EXPORT = [
    {
        "alias": "jdoe",
        "display_name": "Jane Doe",
        "title": "Principal TPM",
        "department": "Platform",
        "manager_alias": "mgr1",
        "email": "jdoe@example.com",
        "teams": ["platform-core", "platform-infra"],
    },
    {
        "alias": "asmith",
        "display_name": "Amy Smith",
        "teams": "growth-team",
    },
    {"display_name": "No Alias Person"},
]


def _write_csv(tmp_path: Path) -> Path:
    path = tmp_path / "export.csv"
    path.write_text(
        "alias,display_name,title,department,manager_alias,email,teams\n"
        "jdoe,Jane Doe,Principal TPM,Platform,mgr1,jdoe@example.com,platform-core;platform-infra\n"
        "asmith,Amy Smith,EM,Growth,jdoe,asmith@example.com,growth-team\n",
        encoding="utf-8",
    )
    return path


def _write_json(tmp_path: Path) -> Path:
    path = tmp_path / "export.json"
    path.write_text(json.dumps(JSON_EXPORT), encoding="utf-8")
    return path


def test_capabilities_declare_no_delta_support(tmp_path: Path) -> None:
    provider = LocalDirectoryExportProvider(export_path=_write_csv(tmp_path), provider_name="acme_directory_export", tenant_id="acme-tenant")

    caps = provider.capabilities()

    assert caps.supports_delta is False
    assert caps.supported_entity_types == ("person",)
    assert "display_name" in caps.supported_fields
    assert "manager_alias" in caps.supported_fields


def test_fetch_people_maps_csv_row_to_observation(tmp_path: Path) -> None:
    provider = LocalDirectoryExportProvider(export_path=_write_csv(tmp_path), provider_name="acme_directory_export", tenant_id="acme-tenant")
    request = IdentityLookupRequest(request_id="r1", entity_id=None, provider_subject_id="jdoe", alias_hint=None, requested_fields=())

    result = provider.fetch_people((request,))

    assert result.complete is True
    assert len(result.observations) == 1
    observation = result.observations[0]
    assert observation.state == ObservationState.PRESENT
    assert observation.provider_subject_id == "jdoe"
    field_values = {field.field_name: field.value for field in observation.fields}
    assert field_values["display_name"] == "Jane Doe"
    assert field_values["contacts"] == "jdoe@example.com"
    assert field_values["manager_alias"] == "mgr1"
    assert result.continuation_token is None
    assert result.delta_token is None


def test_fetch_people_from_json_export_matches_csv_shape(tmp_path: Path) -> None:
    provider = LocalDirectoryExportProvider(export_path=_write_json(tmp_path), provider_name="acme_directory_export", tenant_id="acme-tenant")
    request = IdentityLookupRequest(request_id="r1", entity_id=None, provider_subject_id="jdoe", alias_hint=None, requested_fields=())

    result = provider.fetch_people((request,))

    assert len(result.observations) == 1
    field_values = {field.field_name: field.value for field in result.observations[0].fields}
    assert field_values["display_name"] == "Jane Doe"


def test_fetch_people_lookup_is_case_insensitive_and_uses_alias_hint_fallback(tmp_path: Path) -> None:
    provider = LocalDirectoryExportProvider(export_path=_write_csv(tmp_path), provider_name="acme_directory_export", tenant_id="acme-tenant")
    request = IdentityLookupRequest(request_id="r1", entity_id=None, provider_subject_id=None, alias_hint="JDoe", requested_fields=())

    result = provider.fetch_people((request,))

    assert result.observations[0].provider_subject_id == "jdoe"


def test_fetch_people_unknown_alias_returns_not_found_not_an_error(tmp_path: Path) -> None:
    provider = LocalDirectoryExportProvider(export_path=_write_csv(tmp_path), provider_name="acme_directory_export", tenant_id="acme-tenant")
    request = IdentityLookupRequest(request_id="r1", entity_id=None, provider_subject_id="ghost", alias_hint=None, requested_fields=())

    result = provider.fetch_people((request,))

    assert result.errors == ()
    assert result.observations[0].state == ObservationState.NOT_FOUND


def test_fetch_people_missing_export_file_returns_retryable_error(tmp_path: Path) -> None:
    provider = LocalDirectoryExportProvider(export_path=tmp_path / "missing.csv", provider_name="acme_directory_export", tenant_id="acme-tenant")
    request = IdentityLookupRequest(request_id="r1", entity_id=None, provider_subject_id="jdoe", alias_hint=None, requested_fields=())

    result = provider.fetch_people((request,))

    assert result.observations == ()
    assert len(result.errors) == 1
    assert result.errors[0].code == "export_missing"
    assert result.errors[0].retryable is True


def test_fetch_people_json_row_without_alias_produces_typed_error_not_crash(tmp_path: Path) -> None:
    provider = LocalDirectoryExportProvider(export_path=_write_json(tmp_path), provider_name="acme_directory_export", tenant_id="acme-tenant")
    request = IdentityLookupRequest(request_id="r1", entity_id=None, provider_subject_id="jdoe", alias_hint=None, requested_fields=())

    result = provider.fetch_people((request,))

    assert any(error.code == "row_missing_alias" for error in result.errors)
    assert result.complete is False


def test_fetch_people_unsupported_extension_produces_typed_error(tmp_path: Path) -> None:
    path = tmp_path / "export.txt"
    path.write_text("alias,display_name\njdoe,Jane\n", encoding="utf-8")
    provider = LocalDirectoryExportProvider(export_path=path, provider_name="acme_directory_export", tenant_id="acme-tenant")
    request = IdentityLookupRequest(request_id="r1", entity_id=None, provider_subject_id="jdoe", alias_hint=None, requested_fields=())

    result = provider.fetch_people((request,))

    assert result.errors[0].code == "export_unsupported_format"
    assert result.observations[0].state == ObservationState.NOT_FOUND


def test_fetch_people_request_without_lookup_key_produces_typed_error(tmp_path: Path) -> None:
    provider = LocalDirectoryExportProvider(export_path=_write_csv(tmp_path), provider_name="acme_directory_export", tenant_id="acme-tenant")
    request = IdentityLookupRequest(request_id="r1", entity_id=None, provider_subject_id=None, alias_hint=None, requested_fields=())

    result = provider.fetch_people((request,))

    assert result.errors[0].code == "no_lookup_key"


def test_fetch_team_memberships_splits_semicolon_delimited_teams(tmp_path: Path) -> None:
    provider = LocalDirectoryExportProvider(export_path=_write_csv(tmp_path), provider_name="acme_directory_export", tenant_id="acme-tenant")

    result = provider.fetch_team_memberships(())

    team_ids = {membership.team_subject_id for membership in result.memberships}
    assert team_ids == {"platform-core", "platform-infra", "growth-team"}
    jdoe_memberships = [m for m in result.memberships if m.person_subject_id == "jdoe"]
    assert {m.team_subject_id for m in jdoe_memberships} == {"platform-core", "platform-infra"}


def test_fetch_team_memberships_filters_by_requested_team_subject_ids(tmp_path: Path) -> None:
    provider = LocalDirectoryExportProvider(export_path=_write_csv(tmp_path), provider_name="acme_directory_export", tenant_id="acme-tenant")

    result = provider.fetch_team_memberships(("platform-core",))

    assert {m.team_subject_id for m in result.memberships} == {"platform-core"}


def test_fetch_team_memberships_from_json_list_form(tmp_path: Path) -> None:
    provider = LocalDirectoryExportProvider(export_path=_write_json(tmp_path), provider_name="acme_directory_export", tenant_id="acme-tenant")

    result = provider.fetch_team_memberships(())

    team_ids = {membership.team_subject_id for membership in result.memberships}
    assert "platform-core" in team_ids
    assert "growth-team" in team_ids


def test_source_version_and_etag_are_stable_for_unchanged_file(tmp_path: Path) -> None:
    provider = LocalDirectoryExportProvider(export_path=_write_csv(tmp_path), provider_name="acme_directory_export", tenant_id="acme-tenant")
    request = IdentityLookupRequest(request_id="r1", entity_id=None, provider_subject_id="jdoe", alias_hint=None, requested_fields=())

    result_a = provider.fetch_people((request,))
    result_b = provider.fetch_people((request,))

    assert result_a.snapshot_id == result_b.snapshot_id
    assert result_a.snapshot_id is not None
