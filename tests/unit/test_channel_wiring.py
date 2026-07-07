from __future__ import annotations

from pathlib import Path

from src.commands import channel_wiring
from src.commands.channel_wiring import resolve_channel_binding, resolve_channel_bindings, resolve_channel_config, resolve_channel_configs
from src.core.models_v2 import ADOConfig, KustoConfig, Program


def test_resolve_channel_configs_returns_no_uil_channels_when_channels_missing(tmp_path: Path) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir()
    (program_dir / "program.yaml").write_text("schema_version: '3.0'\nid: demo\nname: Demo\n", encoding="utf-8")
    program = Program(
        schema_version="3.0",
        id="demo",
        name="Demo",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Demo",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
        ),
    )

    configs = resolve_channel_configs(program, programs_root=tmp_path)

    assert configs == ()


def test_resolve_channel_bindings_returns_empty_when_channels_missing(tmp_path: Path) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir()
    (program_dir / "program.yaml").write_text("schema_version: '3.0'\nid: demo\nname: Demo\n", encoding="utf-8")
    program = Program(
        schema_version="3.0",
        id="demo",
        name="Demo",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Demo",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
        ),
    )

    bindings = resolve_channel_bindings(program, (), programs_root=tmp_path)

    assert bindings == ()


def test_resolve_channel_configs_rejects_unregistered_channel(tmp_path: Path) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir()
    (program_dir / "program.yaml").write_text(
        "schema_version: '3.0'\nid: demo\nname: Demo\nchannels:\n  slack:\n    enabled: true\n",
        encoding="utf-8",
    )
    program = Program(schema_version="3.0", id="demo", name="Demo")

    try:
        resolve_channel_configs(program, programs_root=tmp_path)
    except ValueError as error:
        assert "Unsupported integration channel 'slack'" in str(error)
    else:
        raise AssertionError("Expected unsupported channel to fail")


def test_resolve_channel_configs_preserves_additional_flat_channel_settings(tmp_path: Path) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir()
    (program_dir / "program.yaml").write_text(
        "\n".join(
            [
                "schema_version: '3.0'",
                "id: demo",
                "name: Demo",
                "channels:",
                "  ado:",
                "    enabled: true",
                "    discovery_threshold_hours: 24",
                "    ttl_days: 30",
                "    ref_title_visible: false",
                "    schema_introspection_enabled: true",
            ]
        ),
        encoding="utf-8",
    )
    program = Program(schema_version="3.0", id="demo", name="Demo")

    configs = resolve_channel_configs(program, programs_root=tmp_path)

    assert configs[0].extra == {
        "ref_title_visible": False,
        "schema_introspection_enabled": True,
    }


def test_resolve_channel_bindings_uses_provider_factories(tmp_path: Path) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir()
    (program_dir / "program.yaml").write_text(
        "\n".join(
            [
                "schema_version: '3.0'",
                "id: demo",
                "name: Demo",
                "channels:",
                "  ado:",
                "    enabled: true",
                "    discovery_threshold_hours: 24",
                "    ttl_days: 30",
                "    extra:",
                "      instance_id: instance-a",
            ]
        ),
        encoding="utf-8",
    )
    (program_dir / "slice_contracts.yaml").write_text("schema_version: '1.0'\nslices: []\n", encoding="utf-8")
    program = Program(
        schema_version="3.0",
        id="demo",
        name="Demo",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Demo",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
        ),
    )

    from src.commands import channel_wiring

    monkey_discovery = ("discovery-provider", "discovery-config")
    monkey_hydration = ("hydration-provider", "hydration-config")
    original_ado_discovery = channel_wiring.ADODiscoveryProvider.from_program
    original_ado_hydration = channel_wiring.ADOHydrationProvider.from_program
    channel_wiring.ADODiscoveryProvider.from_program = classmethod(lambda cls, program, config, workstreams, programs_root: monkey_discovery)
    channel_wiring.ADOHydrationProvider.from_program = classmethod(lambda cls, program, config, workstreams, programs_root: monkey_hydration)
    try:
        bindings = resolve_channel_bindings(program, (), programs_root=tmp_path)
    finally:
        channel_wiring.ADODiscoveryProvider.from_program = original_ado_discovery
        channel_wiring.ADOHydrationProvider.from_program = original_ado_hydration

    assert len(bindings) == 1
    assert bindings[0].config.channel == "ado"
    assert bindings[0].discovery_provider == "discovery-provider"
    assert bindings[0].discovery_config == "discovery-config"
    assert bindings[0].hydration_provider == "hydration-provider"
    assert bindings[0].hydration_config == "hydration-config"


def test_resolve_channel_bindings_supports_kusto(tmp_path: Path) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir()
    (program_dir / "program.yaml").write_text(
        "\n".join(
            [
                "schema_version: '3.0'",
                "id: demo",
                "name: Demo",
                "channels:",
                "  kusto:",
                "    enabled: true",
                "    discovery_threshold_hours: 168",
                "    schema_introspection_enabled: false",
            ]
        ),
        encoding="utf-8",
    )
    program = Program(
        schema_version="3.0",
        id="demo",
        name="Demo",
        kusto=KustoConfig(enabled=True),
    )

    bindings = resolve_channel_bindings(program, (), programs_root=tmp_path)

    assert len(bindings) == 1
    assert bindings[0].config.channel == "kusto"


def test_resolve_channel_binding_returns_requested_channel(tmp_path: Path) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir()
    (program_dir / "program.yaml").write_text(
        "\n".join(
            [
                "schema_version: '3.0'",
                "id: demo",
                "name: Demo",
                "channels:",
                "  kusto:",
                "    enabled: true",
                "    discovery_threshold_hours: 168",
                "    schema_introspection_enabled: false",
            ]
        ),
        encoding="utf-8",
    )
    (program_dir / "slice_contracts.yaml").write_text("schema_version: '1.0'\nslices: []\n", encoding="utf-8")
    program = Program(
        schema_version="3.0",
        id="demo",
        name="Demo",
        kusto=KustoConfig(enabled=True),
    )

    binding = resolve_channel_binding(program, (), "kusto", programs_root=tmp_path)

    assert binding is not None
    assert binding.config.channel == "kusto"


def test_resolve_channel_binding_returns_none_when_channel_is_not_enabled(tmp_path: Path) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir()
    (program_dir / "program.yaml").write_text(
        "\n".join(
            [
                "schema_version: '3.0'",
                "id: demo",
                "name: Demo",
                "channels:",
                "  ado:",
                "    enabled: false",
                "    discovery_threshold_hours: 24",
                "    ttl_days: 30",
            ]
        ),
        encoding="utf-8",
    )
    program = Program(
        schema_version="3.0",
        id="demo",
        name="Demo",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Demo",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
        ),
    )

    binding = resolve_channel_binding(program, (), "ado", programs_root=tmp_path)

    assert binding is None


def test_resolve_channel_config_returns_requested_channel_even_if_disabled(tmp_path: Path) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir()
    (program_dir / "program.yaml").write_text(
        "\n".join(
            [
                "schema_version: '3.0'",
                "id: demo",
                "name: Demo",
                "channels:",
                "  ado:",
                "    enabled: false",
                "    discovery_threshold_hours: 24",
                "    ttl_days: 30",
                "    extra:",
                "      instance_id: instance-a",
            ]
        ),
        encoding="utf-8",
    )
    program = Program(
        schema_version="3.0",
        id="demo",
        name="Demo",
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Demo",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
        ),
    )

    config = resolve_channel_config(program, "ado", programs_root=tmp_path)

    assert config is not None
    assert config.enabled is False
    assert config.extra == {"instance_id": "instance-a"}


def test_resolve_channel_configs_uses_legacy_registry_when_env_requests_legacy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir()
    (program_dir / "program.yaml").write_text(
        "schema_version: '3.0'\nid: demo\nname: Demo\nchannels:\n  ado:\n    enabled: true\n",
        encoding="utf-8",
    )
    program = Program(schema_version="3.0", id="demo", name="Demo")
    monkeypatch.setenv("VERTEX_PROVIDER_REGISTRY", "legacy")
    monkeypatch.setattr(channel_wiring, "build_provider_registry", lambda: (_ for _ in ()).throw(AssertionError("registry bootstrap should not run in legacy mode")))

    configs = resolve_channel_configs(program, programs_root=tmp_path)

    assert len(configs) == 1
    assert configs[0].channel == "ado"
