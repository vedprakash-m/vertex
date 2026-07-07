from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.commands.integration_format import _format_optional_datetime
from src.commands.integration_support import _registry_path
from src.core.channel_registry_store import ChannelRegistryStore
from src.core.gather_state_store import load_gather_state, write_gather_state


def _registry_channels(store: ChannelRegistryStore) -> tuple[str, ...]:
    return store.registered_channels()


def _refresh_gather_state_discovery_health(
    program: str,
    *,
    programs_root: Path,
    channel: str,
    provider_instance_id: str | None,
    store: ChannelRegistryStore,
) -> None:
    existing = load_gather_state(program, programs_root=programs_root)
    last_delta = next(
        iter(store.recent_deltas(channel, limit=1, provider_instance_id=provider_instance_id)),
        None,
    )
    channels = dict(existing.channels) if existing is not None else {}
    channels[channel] = {
        "uil_enabled": True,
        "uil_registry_file_present": _registry_path(program, programs_root).exists(),
        "uil_health": "ok",
        "uil_registry_size": store.registration_count(
            channel,
            provider_instance_id=provider_instance_id,
        ),
        "uil_last_discovery_at": _format_optional_datetime(
            store.last_discovery_at(channel, provider_instance_id=provider_instance_id)
        ),
        "uil_last_delta_summary": last_delta.summary if last_delta is not None else None,
        "uil_last_delta_shrinkage_pct": last_delta.shrinkage_pct if last_delta is not None else None,
        "uil_last_delta_computed_at": (
            _format_optional_datetime(last_delta.computed_at)
            if last_delta is not None
            else None
        ),
        "uil_discovery_completeness": last_delta.completeness.value if last_delta is not None else None,
        "uil_scope_health": store.recent_scope_health(
            channel,
            provider_instance_id=provider_instance_id,
        ),
    }
    write_gather_state(
        program,
        gathered_at=existing.gathered_at if existing is not None else datetime.now(timezone.utc),
        scanned_items=existing.scanned_items if existing is not None else 0,
        discovered_signals=existing.discovered_signals if existing is not None else 0,
        new_signals=existing.new_signals if existing is not None else 0,
        pending_review=existing.pending_review if existing is not None else 0,
        trajectory_updates=existing.trajectory_updates if existing is not None else 0,
        auto_reviews_written=existing.auto_reviews_written if existing is not None else 0,
        ado_calls=existing.ado_calls if existing is not None else 0,
        archived_journal_files=existing.archived_journal_files if existing is not None else 0,
        background_proposals=existing.background_proposals if existing is not None else 0,
        integration_errors=existing.integration_errors if existing is not None else 0,
        integration_error_details=existing.integration_error_details if existing is not None else (),
        gather_flags=existing.gather_flags if existing is not None else {},
        channels=channels,
        m365_discovery=existing.m365_discovery if existing is not None else {},
        previous_gathered_at=existing.previous_gathered_at if existing is not None else None,
        previous_query_states=existing.previous_query_states if existing is not None else {},
        previous_channels=existing.previous_channels if existing is not None else {},
        previous_m365_discovery=existing.previous_m365_discovery if existing is not None else {},
        query_states=existing.query_states if existing is not None else {},
        programs_root=programs_root,
    )
