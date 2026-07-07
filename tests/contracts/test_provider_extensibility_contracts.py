from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.commands import channel_wiring
from src.commands.channel_wiring import resolve_channel_bindings
from src.core.integration_protocol import DiscoveryProvider, HydrationProvider, SignalExtractor
from src.core.integration_types import (
    DiscoveryResult,
    ChannelConfig,
    DiscoveryCompleteness,
    ExtractionResult,
    HydrationResult,
    HydrationMode,
    ProviderCapability,
    RunContext,
)
from src.core.models_v2 import Program
from src.core.provider_registry import ProviderFactory, ProviderRegistration, ProviderRegistry


class _DemoDiscoveryProvider:
    @classmethod
    def from_program(
        cls,
        program: Program,
        channel_config: ChannelConfig,
        workstreams: tuple[object, ...],
        *,
        programs_root: Path,
    ) -> tuple["_DemoDiscoveryProvider", dict[str, object]]:
        del workstreams, programs_root
        return cls(), {"program_id": program.id, "instance_id": (channel_config.extra or {}).get("instance_id")}

    @property
    def channel(self) -> str:
        return "demo"

    @property
    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            channel="demo",
            discovery_modes=(DiscoveryCompleteness.FULL, DiscoveryCompleteness.INCREMENTAL),
            hydration_modes=(HydrationMode.FULL,),
            supports_since=True,
            max_batch_size=25,
            rate_limit_rpm=120,
            retry_max_attempts=2,
            retry_backoff_seconds=0.25,
            privacy_class="fixture_only",
            timeout_seconds=15,
        )

    def discover(
        self,
        program_id: str,
        config: object,
        existing: tuple[object, ...],
        run_ctx: RunContext = RunContext(),
    ) -> DiscoveryResult:
        del config, existing, run_ctx
        return DiscoveryResult(
            channel="demo",
            program_id=program_id,
            discovered_refs=(),
            completeness=DiscoveryCompleteness.FULL,
            scope_statuses={},
            scope_state_updates={},
            errors=(),
            computed_at=datetime.now(timezone.utc),
        )


class _DemoHydrationProvider:
    @classmethod
    def from_program(
        cls,
        program: Program,
        channel_config: ChannelConfig,
        workstreams: tuple[object, ...],
        *,
        programs_root: Path,
    ) -> tuple["_DemoHydrationProvider", dict[str, object]]:
        del workstreams, programs_root
        return cls(), {"program_id": program.id, "enabled": channel_config.enabled}

    @property
    def channel(self) -> str:
        return "demo"

    @property
    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            channel="demo",
            discovery_modes=(DiscoveryCompleteness.FULL,),
            hydration_modes=(HydrationMode.FULL, HydrationMode.FRESHNESS_ONLY),
            supports_since=True,
            max_batch_size=10,
            rate_limit_rpm=60,
            retry_max_attempts=3,
            retry_backoff_seconds=0.5,
            privacy_class="fixture_only",
            timeout_seconds=15,
            supports_replay=True,
            supports_degradation=True,
        )

    def hydrate(
        self,
        registrations: tuple[object, ...],
        since,
        program_id: str,
        config: object,
        mode: HydrationMode = HydrationMode.FULL,
        run_ctx: RunContext = RunContext(),
    ) -> HydrationResult[tuple[object, ...]]:
        del registrations, since, program_id, config, mode, run_ctx
        return HydrationResult(
            channel="demo",
            resources=(),
            api_call_count=0,
            errors=(),
            hydrated_ref_ids=(),
            failed_ref_ids=(),
        )


class _DemoSignalExtractor:
    @property
    def channel(self) -> str:
        return "demo"

    def extract(self, resources: tuple[object, ...], program_id: str) -> ExtractionResult:
        del resources, program_id
        return ExtractionResult(
            channel="demo",
            signals=(),
            trajectory_points=(),
            side_artifacts={},
            errors=(),
        )


def test_generic_channel_wiring_supports_fixture_backed_second_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir()
    (program_dir / "program.yaml").write_text(
        "\n".join(
            [
                "schema_version: '3.0'",
                "id: demo",
                "name: Demo",
                "channels:",
                "  demo:",
                "    enabled: true",
                "    discovery_threshold_hours: 12",
                "    ttl_days: 7",
                "    extra:",
                "      instance_id: fixture-pack",
            ]
        ),
        encoding="utf-8",
    )
    program = Program(schema_version="3.0", id="demo", name="Demo")

    registry = ProviderRegistry()
    registry.register(
        ProviderRegistration(
            channel="demo",
            discovery_cls=_DemoDiscoveryProvider,
            hydration_cls=_DemoHydrationProvider,
            signal_extractor_cls=_DemoSignalExtractor,
        )
    )
    monkeypatch.setattr(channel_wiring, "build_provider_factory", lambda: ProviderFactory.from_registry(registry))

    bindings = resolve_channel_bindings(program, (), programs_root=tmp_path)

    assert len(bindings) == 1
    binding = bindings[0]
    assert binding.config.channel == "demo"
    assert isinstance(binding.discovery_provider, _DemoDiscoveryProvider)
    assert isinstance(binding.hydration_provider, _DemoHydrationProvider)
    assert isinstance(binding.signal_extractor, _DemoSignalExtractor)
    assert isinstance(binding.discovery_provider, DiscoveryProvider)
    assert isinstance(binding.hydration_provider, HydrationProvider)
    assert isinstance(binding.signal_extractor, SignalExtractor)
    assert binding.discovery_provider.capability.supports_since is True
    assert binding.hydration_provider.capability.supports_replay is True
    assert binding.discovery_config == {"program_id": "demo", "instance_id": "fixture-pack"}
    assert binding.hydration_config == {"program_id": "demo", "enabled": True}


def test_provider_pack_can_hold_second_provider_binding_defaults(
    tmp_path: Path,
    monkeypatch,
) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir()
    (program_dir / "program.yaml").write_text(
        "\n".join(
            [
                "schema_version: '3.0'",
                "id: demo",
                "name: Demo",
                "provider_pack:",
                "  channels:",
                "    demo:",
                "      discovery_threshold_hours: 36",
                "      ttl_days: 21",
                "      extra:",
                "        instance_id: provider-pack",
                "        catalog: fixture-v1",
                "channels:",
                "  demo:",
                "    enabled: true",
            ]
        ),
        encoding="utf-8",
    )
    program = Program(schema_version="3.0", id="demo", name="Demo")

    registry = ProviderRegistry()
    registry.register(
        ProviderRegistration(
            channel="demo",
            discovery_cls=_DemoDiscoveryProvider,
            hydration_cls=_DemoHydrationProvider,
            signal_extractor_cls=_DemoSignalExtractor,
        )
    )
    monkeypatch.setattr(channel_wiring, "build_provider_factory", lambda: ProviderFactory.from_registry(registry))

    bindings = resolve_channel_bindings(program, (), programs_root=tmp_path)

    assert len(bindings) == 1
    binding = bindings[0]
    assert binding.config.discovery_threshold_hours == 36
    assert binding.config.ttl_days == 21
    assert binding.config.extra == {
        "instance_id": "provider-pack",
        "catalog": "fixture-v1",
    }
    assert binding.discovery_config == {"program_id": "demo", "instance_id": "provider-pack"}
