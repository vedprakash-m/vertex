from __future__ import annotations

from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.identity_provider_port import (
    find_provider_config,
    load_identity_providers_document,
)


def test_load_identity_providers_document_round_trips_the_real_example_fixture() -> None:
    path = Path("knowledge/identity_providers.example.yaml")

    document = load_identity_providers_document(path)

    assert document is not None
    assert document.schema_version == "1.0"
    names = {provider.name for provider in document.providers}
    assert names == {"acme_directory_export", "sample_live_directory_provider"}


def test_load_identity_providers_document_never_exposes_a_resolved_secret() -> None:
    path = Path("knowledge/identity_providers.example.yaml")

    document = load_identity_providers_document(path)

    live_provider = find_provider_config(document, "sample_live_directory_provider")
    assert live_provider is not None
    assert live_provider.secret_ref is not None
    assert live_provider.secret_ref.startswith("keyvault://")

    local_provider = find_provider_config(document, "acme_directory_export")
    assert local_provider is not None
    assert local_provider.secret_ref is None
    assert local_provider.provider_type == "local_directory_export"


def test_find_provider_config_returns_none_for_unknown_name() -> None:
    document = load_identity_providers_document(Path("knowledge/identity_providers.example.yaml"))

    assert find_provider_config(document, "nonexistent") is None


def test_load_identity_providers_document_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert load_identity_providers_document(tmp_path / "identity_providers.yaml") is None


def test_load_identity_providers_document_rejects_duplicate_provider_names(tmp_path: Path) -> None:
    path = tmp_path / "identity_providers.yaml"
    path.write_text(
        (
            'schema_version: "1.0"\n'
            "providers:\n"
            '  - name: "dup"\n'
            '    provider_type: "local_directory_export"\n'
            '    tenant_id: "t"\n'
            '    capability_contract_version: "1.0"\n'
            '  - name: "dup"\n'
            '    provider_type: "local_directory_export"\n'
            '    tenant_id: "t"\n'
            '    capability_contract_version: "1.0"\n'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="duplicate provider name"):
        load_identity_providers_document(path)


def test_load_identity_providers_document_rejects_missing_required_field(tmp_path: Path) -> None:
    path = tmp_path / "identity_providers.yaml"
    path.write_text('schema_version: "1.0"\nproviders:\n  - provider_type: "local_directory_export"\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="'name'"):
        load_identity_providers_document(path)


def test_load_identity_providers_document_applies_defaults_for_optional_fields(tmp_path: Path) -> None:
    path = tmp_path / "identity_providers.yaml"
    path.write_text(
        (
            'schema_version: "1.0"\n'
            "providers:\n"
            '  - name: "minimal"\n'
            '    provider_type: "local_directory_export"\n'
            '    tenant_id: "t"\n'
            '    capability_contract_version: "1.0"\n'
        ),
        encoding="utf-8",
    )

    document = load_identity_providers_document(path)

    provider = document.providers[0]
    assert provider.enabled is False
    assert provider.timeout_seconds == 30
    assert provider.rate_limit_per_minute == 60
    assert provider.allowed_fields == ()
    assert provider.secret_ref is None
