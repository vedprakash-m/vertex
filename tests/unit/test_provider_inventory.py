from __future__ import annotations

from src.commands.channel_wiring import build_provider_registry


def test_bundled_provider_registry_registers_expected_channels() -> None:
    registry = build_provider_registry()

    assert set(registry.channels()) == {"ado", "kusto", "teams", "email", "icm"}
