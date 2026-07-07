"""WI-6.1: NullAdapter — legal empty-yield SourceAdapter implementation.

O-7: A NullAdapter that returns an empty tuple of signals is always legal.
Used as a fallback, for testing, and when a source channel is disabled.

Zone A module (INV-1 applies).
"""
from __future__ import annotations

from datetime import datetime

from src.core.integration_types import ExtractionResult, RunContext
from src.core.integration_protocol import ActuationResult, SourceAdapter


class NullAdapter:
    """WI-6.1: Null object implementation of SourceAdapter.

    Returns empty signals, no errors, no trajectory points. Always safe to use.
    Empty-yield is legal per O-7.
    """

    def __init__(self, channel: str = "null") -> None:
        self._channel = channel

    @property
    def channel(self) -> str:
        return self._channel

    def fetch(
        self,
        program_id: str,
        config: object,
        since: datetime,
        run_ctx: RunContext = RunContext(),
    ) -> ExtractionResult:
        return ExtractionResult(
            channel=self._channel,
            signals=(),
            trajectory_points=(),
            side_artifacts={},
            errors=(),
        )


class NullActuationAdapter:
    """WI-6.1: Null object implementation of ActuationAdapter.

    Dry-run safe; always returns success=True, no external side effects.
    """

    def execute(
        self,
        action_type: str,
        payload: dict[str, object],
        *,
        dry_run: bool = False,
    ) -> ActuationResult:
        return ActuationResult(
            success=True,
            external_ref=None,
            dry_run=dry_run,
            error_message=None,
        )


def build_source_adapter(channel: str, config: object = None) -> SourceAdapter:
    """WI-6.1: Factory — returns a SourceAdapter for the given channel.

    Currently only 'null' is wired. Future adapters register here.
    Unknown channels return a NullAdapter so callers always get a valid
    SourceAdapter without crashing.

    Args:
        channel: Channel identifier (e.g. 'ado', 'teams', 'null').
        config: Optional adapter config object.

    Returns:
        A SourceAdapter instance.
    """
    if channel == "null" or channel is None:
        return NullAdapter(channel="null")
    # Unknown channel: return NullAdapter to avoid hard failures
    return NullAdapter(channel=channel)
