from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.commands.gather_pipeline.channel_state_stage import (
    build_gather_channel_states,
    build_uil_ado_channel_state,
)


def _format_optional_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def test_build_gather_channel_states_preserves_uil_metadata_from_previous_state(monkeypatch, tmp_path: Path) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir(parents=True)
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
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VERTEX_UIL_KUSTO", "1")
    previous_channels = {
        "kusto": {
            "active": False,
            "signal_count": 0,
            "expected_min": 10,
            "meets_expected_min": False,
            "reason_not_active": "flag_not_passed",
            "uil_enabled": True,
            "uil_registry_file_present": True,
            "uil_health": "ok",
            "uil_registry_size": 3,
            "uil_last_delta_summary": "+1 -0 ~0 =2",
        }
    }

    states = build_gather_channel_states(
        program_id="demo",
        programs_root=tmp_path,
        workstreams=(),
        ado_signals=(),
        kusto_signals=(),
        workiq_signals=(),
        icm_signals=(),
        gather_flags={"kusto": False, "workiq": False, "icm": False},
        previous_channels=previous_channels,
        format_optional_datetime=_format_optional_datetime,
    )

    assert states["kusto"]["uil_enabled"] is True
    assert states["kusto"]["uil_registry_size"] == 3
    assert states["kusto"]["uil_last_delta_summary"] == "+1 -0 ~0 =2"


def test_build_uil_ado_channel_state_reports_registry_health(monkeypatch, tmp_path: Path) -> None:
    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.integration_types import (
        ChannelRegistration,
        DiscoveredRef,
        DiscoveryCompleteness,
        DiscoveryResult,
        RegistrationBinding,
        RegistrationStatus,
    )

    program_dir = tmp_path / "demo"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            [
                "schema_version: '3.0'",
                "id: demo",
                "name: Demo",
                "channels:",
                "  ado:",
                "    enabled: true",
                "    organization: your-org",
                "    project: One",
                "    scopes:",
                "      - mode: wiql_saved_query",
                "        workstream_id: demo.slice",
                "        label: Demo slice",
                "        saved_query_id: 11111111-1111-1111-1111-111111111111",
            ]
        ),
        encoding="utf-8",
    )
    current_time = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    store = ChannelRegistryStore(tmp_path / "demo" / "channel_registry.sqlite3", "demo")
    store.apply_discovery_result(
        DiscoveryResult(
            channel="ado",
            program_id="demo",
            discovered_refs=(
                DiscoveredRef(
                    registration=ChannelRegistration(
                        channel="ado",
                        program_id="demo",
                        provider_instance_id="default",
                        ref_id="101",
                        ref_kind="work_item",
                        status=RegistrationStatus.ACTIVE,
                        first_discovered_at=current_time,
                        last_seen_at=current_time,
                        confidence=1.0,
                        confidence_source="wiql_saved_query",
                        workstream_ids=("demo.slice",),
                    ),
                    bindings=(
                        RegistrationBinding(
                            workstream_id="demo.slice",
                            scope_id="scope",
                            source_type="wiql_saved_query",
                            confidence=1.0,
                            confidence_source="wiql_saved_query",
                        ),
                    ),
                ),
            ),
            completeness=DiscoveryCompleteness.FULL,
            scope_statuses={},
            scope_state_updates={},
            errors=(),
            computed_at=current_time,
        )
    )
    monkeypatch.setenv("VERTEX_UIL_ADO", "1")

    state = build_uil_ado_channel_state(
        "demo",
        programs_root=tmp_path,
        format_optional_datetime=_format_optional_datetime,
    )

    assert state["uil_enabled"] is True
    assert state["uil_registry_file_present"] is True
    assert state["uil_health"] == "ok"
    assert state["uil_registry_size"] == 1
