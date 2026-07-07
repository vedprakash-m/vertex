from __future__ import annotations

from datetime import datetime, timezone

from src.core.integration_protocol import ChannelRegistryReader, DiscoveryProvider, HydrationProvider, SidecarAdapter, SignalExtractor
from src.core.integration_types import (
    DiscoveryCompleteness,
    DiscoveryResult,
    ExtractionResult,
    HydrationMode,
    HydrationResult,
    ProviderCapability,
    RunContext,
    SidecarResult,
)


class _Provider:
    @property
    def channel(self) -> str:
        return "demo"

    @property
    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            channel="demo",
            discovery_modes=(DiscoveryCompleteness.FULL,),
            hydration_modes=(HydrationMode.FULL,),
            supports_since=True,
            max_batch_size=100,
            rate_limit_rpm=None,
            retry_max_attempts=2,
            retry_backoff_seconds=0.1,
            privacy_class="public_ids",
            timeout_seconds=30,
        )

    def discover(self, program_id: str, config: object, existing: tuple[object, ...], run_ctx: RunContext = RunContext()) -> DiscoveryResult:
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

    def hydrate(
        self,
        registrations: tuple[object, ...],
        since: datetime,
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

    def extract(self, resources: tuple[object, ...], program_id: str) -> ExtractionResult:
        del resources, program_id
        return ExtractionResult(channel="demo", signals=(), trajectory_points=(), side_artifacts={}, errors=())

    @classmethod
    def from_program(cls, program: object, channel_config: object, workstreams: tuple[object, ...]) -> tuple["_Provider", object]:
        del program, channel_config, workstreams
        return cls(), object()


class _RegistryReader:
    def active_registrations(self, channel: str, **kwargs: object) -> tuple[object, ...]:
        del channel, kwargs
        return ()

    def pullable_registrations(self, channel: str, **kwargs: object) -> tuple[object, ...]:
        del channel, kwargs
        return ()

    def all_registrations(self, channel: str) -> tuple[object, ...]:
        del channel
        return ()

    def is_discovery_stale(self, channel: str, threshold_hours: int) -> bool:
        del channel, threshold_hours
        return False

    def registration_count(self, channel: str, **kwargs: object) -> int:
        del channel, kwargs
        return 0

    def get_workstream_map(self, channel: str, ref_pairs: tuple[tuple[str, str], ...], **kwargs: object) -> dict[tuple[str, str], tuple[str, ...]]:
        del channel, ref_pairs, kwargs
        return {}


class _Sidecar:
    @property
    def name(self) -> str:
        return "demo-sidecar"

    def run(
        self,
        program: object,
        workstreams: tuple[object, ...],
        registry: ChannelRegistryReader,
        since: datetime,
        run_ctx: RunContext,
    ) -> SidecarResult:
        del program, workstreams, registry, since, run_ctx
        return SidecarResult(name="demo-sidecar", signals=(), errors=(), side_artifacts={})


def test_provider_protocols_are_runtime_checkable_with_channel_property() -> None:
    provider = _Provider()
    registry = _RegistryReader()
    sidecar = _Sidecar()

    assert isinstance(provider, DiscoveryProvider)
    assert isinstance(provider, HydrationProvider)
    assert isinstance(provider, SignalExtractor)
    assert isinstance(registry, ChannelRegistryReader)
    assert isinstance(sidecar, SidecarAdapter)
    assert provider.capability.channel == "demo"


def test_from_program_factory_returns_provider_and_config() -> None:
    provider, config = _Provider.from_program(object(), object(), ())
    assert isinstance(provider, _Provider)
    assert isinstance(provider, DiscoveryProvider)


def test_provider_capability_manifest_has_required_fields() -> None:
    cap = _Provider().capability
    # channel must match provider.channel
    assert cap.channel == "demo"
    # max_batch_size must be a positive int
    assert isinstance(cap.max_batch_size, int) and cap.max_batch_size > 0
    # retry_max_attempts must be >= 1
    assert cap.retry_max_attempts >= 1
    # discovery_modes must be non-empty
    assert len(cap.discovery_modes) >= 1
    # hydration_modes must be non-empty
    assert len(cap.hydration_modes) >= 1
    # timeout_seconds must be positive
    assert cap.timeout_seconds > 0
