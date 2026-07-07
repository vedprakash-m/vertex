from __future__ import annotations

from datetime import datetime
from typing import Generic, Protocol, TypeVar, runtime_checkable

from src.core.integration_types import (
    ChannelConfig,
    ChannelRegistration,
    DiscoveryResult,
    ExtractionResult,
    HydrationMode,
    HydrationResult,
    ProviderCapability,
    RunContext,
    SidecarResult,
)


ConfigT = TypeVar("ConfigT")
ResourceT = TypeVar("ResourceT")


@runtime_checkable
class DiscoveryProvider(Protocol[ConfigT]):
    @property
    def channel(self) -> str: ...

    @property
    def capability(self) -> ProviderCapability: ...

    def discover(
        self,
        program_id: str,
        config: ConfigT,
        existing: tuple[ChannelRegistration, ...],
        run_ctx: RunContext = RunContext(),
    ) -> DiscoveryResult: ...

    @classmethod
    def from_program(
        cls,
        program: object,
        channel_config: ChannelConfig,
        workstreams: tuple[object, ...],
    ) -> tuple["DiscoveryProvider[ConfigT]", ConfigT]: ...


@runtime_checkable
class HydrationProvider(Protocol[ConfigT, ResourceT]):
    @property
    def channel(self) -> str: ...

    @property
    def capability(self) -> ProviderCapability: ...

    def hydrate(
        self,
        registrations: tuple[ChannelRegistration, ...],
        since: datetime,
        program_id: str,
        config: ConfigT,
        mode: HydrationMode = HydrationMode.FULL,
        run_ctx: RunContext = RunContext(),
    ) -> HydrationResult[ResourceT]: ...

    @classmethod
    def from_program(
        cls,
        program: object,
        channel_config: ChannelConfig,
        workstreams: tuple[object, ...],
    ) -> tuple["HydrationProvider[ConfigT, ResourceT]", ConfigT]: ...


@runtime_checkable
class SignalExtractor(Protocol[ResourceT]):  # type: ignore[misc]
    @property
    def channel(self) -> str: ...

    def extract(self, resources: ResourceT, program_id: str) -> ExtractionResult: ...


@runtime_checkable
class ChannelRegistryReader(Protocol):
    def active_registrations(self, channel: str, **kwargs: object) -> tuple[ChannelRegistration, ...]: ...

    def pullable_registrations(self, channel: str, **kwargs: object) -> tuple[ChannelRegistration, ...]: ...

    def all_registrations(self, channel: str) -> tuple[ChannelRegistration, ...]: ...

    def is_discovery_stale(self, channel: str, threshold_hours: int) -> bool: ...

    def registration_count(self, channel: str, **kwargs: object) -> int: ...

    def get_workstream_map(
        self,
        channel: str,
        ref_pairs: tuple[tuple[str, str], ...],
        **kwargs: object,
    ) -> dict[tuple[str, str], tuple[str, ...]]: ...


@runtime_checkable
class SidecarAdapter(Protocol):
    @property
    def name(self) -> str: ...

    def run(
        self,
        program: object,
        workstreams: tuple[object, ...],
        registry: ChannelRegistryReader,
        since: datetime,
        run_ctx: RunContext,
    ) -> SidecarResult: ...


@runtime_checkable
class SourceAdapter(Protocol):
    """WI-6.1: Unified adapter protocol. New provider = adapter + YAML config.

    Combines discover + hydrate + extract into one interface so that adding
    a new data source only requires: implement SourceAdapter + supply YAML.
    Returning an empty tuple of signals is always legal (O-7 empty-yield).
    """

    @property
    def channel(self) -> str: ...

    def fetch(
        self,
        program_id: str,
        config: object,
        since: datetime,
        run_ctx: RunContext = RunContext(),
    ) -> ExtractionResult: ...


@runtime_checkable
class ActuationAdapter(Protocol):
    """WI-6.1 / WI-7.2: Actuation adapter protocol.

    Concrete implementations (e.g. AdoAdapter) execute approved actuation
    proposals. Dry-run must always be safe; live execution requires
    INV-12 approval gate satisfied upstream.
    """

    def execute(
        self,
        action_type: str,
        payload: dict[str, object],
        *,
        dry_run: bool = False,
    ) -> "ActuationResult": ...


class ActuationResult:
    """Minimal result type for actuation execute(). WI-6.1 / WI-7.2."""

    __slots__ = ("success", "external_ref", "dry_run", "error_message")

    def __init__(
        self,
        *,
        success: bool,
        external_ref: str | None = None,
        dry_run: bool = False,
        error_message: str | None = None,
    ) -> None:
        self.success = success
        self.external_ref = external_ref
        self.dry_run = dry_run
        self.error_message = error_message

    def __repr__(self) -> str:
        return (
            f"ActuationResult(success={self.success}, external_ref={self.external_ref!r}, "
            f"dry_run={self.dry_run}, error_message={self.error_message!r})"
        )
