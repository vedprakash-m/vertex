from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.commands import integration
from src.commands import integration_discovery
from src.core.gather_state_store import load_gather_state, write_gather_state
from src.core.channel_registry_store import ChannelRegistryStore
from src.core.discovery_intent import (
    DiscoveryAttempt,
    DiscoveryAttemptOutcome,
    SourceCandidate,
    SourceCandidateStatus,
    SourceIntentStatus,
    SourceRefKind,
    build_discovery_attempt_id,
    build_source_candidate_id,
)
from src.core.integration_types import (
    ChannelBinding,
    ChannelConfig,
    ChannelRegistration,
    DiscoveredRef,
    DiscoveryCompleteness,
    DiscoveryResult,
    RegistrationBinding,
    RegistrationStatus,
    RunContext,
    ScopeStatus,
    ScopeStatusKind,
)
from src.core.models_v2 import ADOConfig, KustoConfig, Program, TeamsChat, TeamsMeetingSeries, Workstream, WorkstreamSignalSources
from src.core.source_candidate_store import SourceCandidateStore, candidate_evidence_json


runner = CliRunner()


def _make_discovery_result(
    *,
    channel: str,
    program_id: str,
    ref_id: str,
    ref_kind: str,
    ref_title: str,
    current_time: datetime,
    workstream_id: str,
    scope_id: str,
    source_type: str,
    provider_instance_id: str = "default",
) -> DiscoveryResult:
    return DiscoveryResult(
        channel=channel,
        program_id=program_id,
        discovered_refs=(
            DiscoveredRef(
                registration=ChannelRegistration(
                    channel=channel,
                    program_id=program_id,
                    provider_instance_id=provider_instance_id,
                    ref_id=ref_id,
                    ref_kind=ref_kind,
                    status=RegistrationStatus.ACTIVE,
                    first_discovered_at=current_time,
                    last_seen_at=current_time,
                    ref_title=ref_title,
                ),
                bindings=(
                    RegistrationBinding(
                        workstream_id=workstream_id,
                        scope_id=scope_id,
                        source_type=source_type,
                        confidence=1.0,
                        confidence_source=source_type,
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


def _make_binding(*, channel: str, threshold_hours: int, ttl_days: int | None, provider: object) -> ChannelBinding:
    return ChannelBinding(
        config=ChannelConfig(
            channel=channel,
            enabled=True,
            discovery_threshold_hours=threshold_hours,
            ttl_days=ttl_days,
            extra={"instance_id": "default"},
        ),
        discovery_provider=provider,
        hydration_provider=object(),
        signal_extractor=object(),
        discovery_config={"channel": channel},
        hydration_config=None,
    )


def test_integration_discover_updates_gather_state_health(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
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
                "    discovery_threshold_hours: 24",
                "    ttl_days: 30",
                "    extra:",
                "      instance_id: default",
            ]
        ),
        encoding="utf-8",
    )
    (program_dir / "slice_contracts.yaml").write_text("schema_version: '1.0'\nslices: []\n", encoding="utf-8")

    gathered_at = datetime(2026, 5, 23, 9, 0, tzinfo=timezone.utc)
    write_gather_state(
        "demo",
        gathered_at=gathered_at,
        scanned_items=10,
        discovered_signals=4,
        new_signals=1,
        pending_review=0,
        trajectory_updates=2,
        auto_reviews_written=0,
        ado_calls=7,
        archived_journal_files=0,
        background_proposals=0,
        channels={},
        programs_root=programs_root,
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
            api_timeout_seconds=30,
        ),
    )
    monkeypatch.setattr(integration, "_load_program", lambda program_id, root: program)
    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)

    class _FakeDiscoveryProvider:
        def discover(self, program_id, config, existing, run_ctx=RunContext()):
            del config, existing, run_ctx
            return _make_discovery_result(
                channel="ado",
                program_id=program_id,
                ref_id="101",
                ref_kind="work_item",
                ref_title="Hydrated item",
                current_time=current_time,
                workstream_id="demo.slice",
                scope_id="scope",
                source_type="wiql_saved_query",
            )

    monkeypatch.setattr(
        integration_discovery,
        "resolve_channel_bindings",
        lambda program_obj, workstreams, programs_root: (_make_binding(channel="ado", threshold_hours=24, ttl_days=30, provider=_FakeDiscoveryProvider()),),
    )

    result = runner.invoke(
        app,
        ["integration", "discover", "--program", "demo", "--force", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0
    state = load_gather_state("demo", programs_root=programs_root)
    assert state is not None
    assert state.gathered_at == gathered_at
    assert state.channels["ado"]["uil_last_delta_summary"] == "+1 -0 ~0 =0"
    assert state.channels["ado"]["uil_discovery_completeness"] == "full"
    assert state.channels["ado"]["uil_registry_size"] == 1


def test_integration_show_filters_by_provider_instance(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    store = integration._store("demo", programs_root)
    store.apply_discovery_result(
        _make_discovery_result(
            channel="ado",
            program_id="demo",
            ref_id="101",
            ref_kind="work_item",
            ref_title="Instance A item",
            current_time=current_time,
            workstream_id="demo.slice",
            scope_id="scope-a",
            source_type="wiql_saved_query",
            provider_instance_id="instance-a",
        )
    )
    store.apply_discovery_result(
        _make_discovery_result(
            channel="ado",
            program_id="demo",
            ref_id="202",
            ref_kind="work_item",
            ref_title="Instance B item",
            current_time=current_time,
            workstream_id="demo.slice",
            scope_id="scope-b",
            source_type="wiql_saved_query",
            provider_instance_id="instance-b",
        )
    )

    result = runner.invoke(
        app,
        [
            "integration",
            "show",
            "--program",
            "demo",
            "--channel",
            "ado",
            "--provider-instance",
            "instance-a",
            "--reveal-titles",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert "provider_instance" in result.stdout
    assert "instance-a" in result.stdout
    assert "Instance A item" in result.stdout
    assert "instance-b" not in result.stdout
    assert "Instance B item" not in result.stdout


def test_integration_discover_normalizes_empty_result_to_binding_provider_instance(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
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
                "    discovery_threshold_hours: 24",
                "    ttl_days: 30",
                "    extra:",
                "      instance_id: instance-a",
            ]
        ),
        encoding="utf-8",
    )
    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    store = integration._store("demo", programs_root)
    store.apply_discovery_result(
        _make_discovery_result(
            channel="ado",
            program_id="demo",
            ref_id="101",
            ref_kind="work_item",
            ref_title="Instance A item",
            current_time=current_time,
            workstream_id="demo.slice",
            scope_id="scope-a",
            source_type="wiql_saved_query",
            provider_instance_id="instance-a",
        )
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
            api_timeout_seconds=30,
        ),
    )
    monkeypatch.setattr(integration, "_load_program", lambda program_id, root: program)

    class _EmptyDiscoveryProvider:
        def discover(self, program_id, config, existing, run_ctx=RunContext()):
            del config, existing, run_ctx
            return DiscoveryResult(
                channel="ado",
                program_id=program_id,
                discovered_refs=(),
                completeness=DiscoveryCompleteness.FULL,
                scope_statuses={},
                scope_state_updates={},
                errors=(),
                computed_at=current_time + timedelta(minutes=5),
            )

    monkeypatch.setattr(
        integration_discovery,
        "resolve_channel_bindings",
        lambda program_obj, workstreams, programs_root: (
            ChannelBinding(
                config=ChannelConfig(
                    channel="ado",
                    enabled=True,
                    discovery_threshold_hours=24,
                    ttl_days=30,
                    extra={"instance_id": "instance-a"},
                ),
                discovery_provider=_EmptyDiscoveryProvider(),
                hydration_provider=object(),
                signal_extractor=object(),
                discovery_config={"channel": "ado"},
                hydration_config=None,
            ),
        ),
    )

    result = runner.invoke(
        app,
        ["integration", "discover", "--program", "demo", "--force", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0
    refreshed_store = integration._store("demo", programs_root)
    assert refreshed_store.active_registrations("ado", provider_instance_id="instance-a") == ()


def test_integration_show_displays_governance_fields(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    store = integration._store("demo", programs_root)
    store.apply_discovery_result(
        _make_discovery_result(
            channel="ado",
            program_id="demo",
            ref_id="101",
            ref_kind="work_item",
            ref_title="Governed item",
            current_time=current_time,
            workstream_id="demo.slice",
            scope_id="scope-a",
            source_type="wiql_saved_query",
        )
    )
    store.confirm("ado", "101", "work_item")
    store.promote("ado", "101", "work_item")
    store.update_signal_yield("ado", "101", "work_item", 3)

    result = runner.invoke(
        app,
        [
            "integration",
            "show",
            "--program",
            "demo",
            "--channel",
            "ado",
            "--reveal-titles",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert "pm_confirmed" in result.stdout
    assert "promoted" in result.stdout
    assert "signal_yield" in result.stdout
    assert "yes" in result.stdout
    assert "3/0/0" in result.stdout


def test_integration_show_without_channel_uses_registered_channels_from_store(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    store = integration._store("demo", programs_root)
    store.apply_discovery_result(
        _make_discovery_result(
            channel="customx",
            program_id="demo",
            ref_id="item-1",
            ref_kind="thread",
            ref_title="Custom channel item",
            current_time=current_time,
            workstream_id="demo.slice",
            scope_id="scope-custom",
            source_type="manual_config",
        )
    )

    result = runner.invoke(
        app,
        [
            "integration",
            "show",
            "--program",
            "demo",
            "--reveal-titles",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert "customx" in result.stdout
    assert "Custom channel item" in result.stdout


def test_integration_diff_filters_by_provider_instance(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    store = integration._store("demo", programs_root)
    store.apply_discovery_result(
        _make_discovery_result(
            channel="ado",
            program_id="demo",
            ref_id="101",
            ref_kind="work_item",
            ref_title="Instance A item",
            current_time=current_time,
            workstream_id="demo.slice",
            scope_id="scope-a",
            source_type="wiql_saved_query",
            provider_instance_id="instance-a",
        )
    )
    store.apply_discovery_result(
        _make_discovery_result(
            channel="ado",
            program_id="demo",
            ref_id="202",
            ref_kind="work_item",
            ref_title="Instance B item",
            current_time=current_time + timedelta(minutes=1),
            workstream_id="demo.slice",
            scope_id="scope-b",
            source_type="wiql_saved_query",
            provider_instance_id="instance-b",
        )
    )

    result = runner.invoke(
        app,
        [
            "integration",
            "diff",
            "--program",
            "demo",
            "--channel",
            "ado",
            "--provider-instance",
            "instance-a",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert "instance=instance-a" in result.stdout
    assert "work_item:101" in result.stdout
    assert "work_item:202" not in result.stdout


def test_integration_retire_targets_provider_instance(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    store = integration._store("demo", programs_root)
    store.apply_discovery_result(
        _make_discovery_result(
            channel="ado",
            program_id="demo",
            ref_id="101",
            ref_kind="work_item",
            ref_title="Instance A item",
            current_time=current_time,
            workstream_id="demo.slice",
            scope_id="scope-a",
            source_type="wiql_saved_query",
            provider_instance_id="instance-a",
        )
    )
    store.apply_discovery_result(
        _make_discovery_result(
            channel="ado",
            program_id="demo",
            ref_id="101",
            ref_kind="work_item",
            ref_title="Instance B item",
            current_time=current_time,
            workstream_id="demo.slice",
            scope_id="scope-b",
            source_type="wiql_saved_query",
            provider_instance_id="instance-b",
        )
    )

    result = runner.invoke(
        app,
        [
            "integration",
            "retire",
            "--program",
            "demo",
            "--channel",
            "ado",
            "--ref-id",
            "101",
            "--ref-kind",
            "work_item",
            "--provider-instance",
            "instance-a",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert "instance=instance-a" in result.stdout
    refreshed_store = integration._store("demo", programs_root)
    assert refreshed_store.active_registrations("ado", provider_instance_id="instance-a") == ()
    assert len(refreshed_store.active_registrations("ado", provider_instance_id="instance-b")) == 1


def test_integration_suppress_targets_provider_instance(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    store = integration._store("demo", programs_root)
    for provider_instance_id in ("instance-a", "instance-b"):
        store.apply_discovery_result(
            _make_discovery_result(
                channel="ado",
                program_id="demo",
                ref_id="101",
                ref_kind="work_item",
                ref_title=f"{provider_instance_id} item",
                current_time=current_time,
                workstream_id="demo.slice",
                scope_id=f"scope-{provider_instance_id}",
                source_type="wiql_saved_query",
                provider_instance_id=provider_instance_id,
            )
        )

    result = runner.invoke(
        app,
        [
            "integration",
            "suppress",
            "--program",
            "demo",
            "--channel",
            "ado",
            "--ref-id",
            "101",
            "--ref-kind",
            "work_item",
            "--provider-instance",
            "instance-a",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert "instance=instance-a" in result.stdout
    refreshed_store = integration._store("demo", programs_root)
    instance_a = refreshed_store.all_registrations("ado", provider_instance_id="instance-a")[0]
    instance_b = refreshed_store.all_registrations("ado", provider_instance_id="instance-b")[0]
    assert instance_a.status is RegistrationStatus.SUPPRESSED
    assert instance_b.status is RegistrationStatus.ACTIVE


def test_integration_confirm_targets_provider_instance(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    store = integration._store("demo", programs_root)
    for provider_instance_id in ("instance-a", "instance-b"):
        store.apply_discovery_result(
            _make_discovery_result(
                channel="ado",
                program_id="demo",
                ref_id="101",
                ref_kind="work_item",
                ref_title=f"{provider_instance_id} item",
                current_time=current_time,
                workstream_id="demo.slice",
                scope_id=f"scope-{provider_instance_id}",
                source_type="wiql_saved_query",
                provider_instance_id=provider_instance_id,
            )
        )

    result = runner.invoke(
        app,
        [
            "integration",
            "confirm",
            "--program",
            "demo",
            "--channel",
            "ado",
            "--ref-id",
            "101",
            "--ref-kind",
            "work_item",
            "--provider-instance",
            "instance-a",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert "instance=instance-a" in result.stdout
    refreshed_store = integration._store("demo", programs_root)
    instance_a = refreshed_store.all_registrations("ado", provider_instance_id="instance-a")[0]
    instance_b = refreshed_store.all_registrations("ado", provider_instance_id="instance-b")[0]
    assert instance_a.pm_confirmed is True
    assert instance_b.pm_confirmed is False


def test_integration_promote_and_signal_yield_target_provider_instance(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    store = integration._store("demo", programs_root)
    for provider_instance_id in ("instance-a", "instance-b"):
        store.apply_discovery_result(
            _make_discovery_result(
                channel="ado",
                program_id="demo",
                ref_id="101",
                ref_kind="work_item",
                ref_title=f"{provider_instance_id} item",
                current_time=current_time,
                workstream_id="demo.slice",
                scope_id=f"scope-{provider_instance_id}",
                source_type="wiql_saved_query",
                provider_instance_id=provider_instance_id,
            )
        )

    promote_result = runner.invoke(
        app,
        [
            "integration",
            "promote",
            "--program",
            "demo",
            "--channel",
            "ado",
            "--ref-id",
            "101",
            "--ref-kind",
            "work_item",
            "--provider-instance",
            "instance-a",
            "--programs-root",
            str(programs_root),
        ],
    )
    yield_result = runner.invoke(
        app,
        [
            "integration",
            "signal-yield",
            "--program",
            "demo",
            "--channel",
            "ado",
            "--ref-id",
            "101",
            "--ref-kind",
            "work_item",
            "--count",
            "3",
            "--provider-instance",
            "instance-a",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert promote_result.exit_code == 0
    assert yield_result.exit_code == 0
    refreshed_store = integration._store("demo", programs_root)
    instance_a = refreshed_store.all_registrations("ado", provider_instance_id="instance-a")[0]
    instance_b = refreshed_store.all_registrations("ado", provider_instance_id="instance-b")[0]
    assert instance_a.promoted is True
    assert instance_b.promoted is False
    assert instance_a.signal_yield_last_3 == (3, 0, 0)
    assert instance_b.signal_yield_last_3 == (0, 0, 0)


def test_integration_backup_creates_named_copy(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    (program_dir / "runtime").mkdir()
    registry_path = integration._registry_path("demo", programs_root)
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(registry_path)
    conn.execute("CREATE TABLE marker (v TEXT)")
    conn.execute("INSERT INTO marker VALUES ('registry-v1')")
    conn.commit()
    conn.close()
    expected_backup = programs_root / "demo" / "registry_backups" / "channel_registry-20260524T120000Z.sqlite3"
    monkeypatch.setattr(integration, "_backup_path", lambda program, root, prefix="channel_registry": expected_backup)

    result = runner.invoke(
        app,
        ["integration", "backup", "--program", "demo", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0
    assert str(expected_backup) in result.stdout
    conn = _sqlite3.connect(expected_backup)
    val = conn.execute("SELECT v FROM marker").fetchone()[0]
    conn.close()
    assert val == "registry-v1"


def test_integration_restore_lists_available_backups(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    backup_dir = programs_root / "demo" / "registry_backups"
    backup_dir.mkdir(parents=True)
    first = backup_dir / "channel_registry-20260524T120000Z.sqlite3"
    second = backup_dir / "channel_registry-20260524T130000Z.sqlite3"
    first.write_text("registry-v1", encoding="utf-8")
    second.write_text("registry-v2", encoding="utf-8")

    result = runner.invoke(
        app,
        ["integration", "restore", "--program", "demo", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0
    assert first.name in result.stdout
    assert second.name in result.stdout


def test_integration_restore_creates_pre_restore_backup_and_restores_selected_snapshot(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    backup_dir = program_dir / "registry_backups"
    backup_dir.mkdir(parents=True)
    (program_dir / "runtime").mkdir()
    registry_path = integration._registry_path("demo", programs_root)
    selected_backup = backup_dir / "channel_registry-20260524T120000Z.sqlite3"
    safety_backup = backup_dir / "channel_registry-pre-restore-20260524T140000Z.sqlite3"

    import sqlite3 as _sqlite3
    for path, marker in [(registry_path, "registry-current"), (selected_backup, "registry-previous")]:
        conn = _sqlite3.connect(path)
        conn.execute("CREATE TABLE marker (v TEXT)")
        conn.execute("INSERT INTO marker VALUES (?)", (marker,))
        conn.commit()
        conn.close()

    monkeypatch.setattr(integration, "_backup_path", lambda program, root, prefix="channel_registry": safety_backup)

    result = runner.invoke(
        app,
        [
            "integration",
            "restore",
            "--program",
            "demo",
            "--backup",
            selected_backup.name,
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert f"Restored: {selected_backup.name}" in result.stdout

    def _read_marker(path: Path) -> str:
        conn = _sqlite3.connect(path)
        val = conn.execute("SELECT v FROM marker").fetchone()[0]
        conn.close()
        return val

    assert _read_marker(registry_path) == "registry-previous"
    assert _read_marker(safety_backup) == "registry-current"


def test_integration_discover_accept_shrinkage_only_creates_backup_for_actual_guarded_shrinkage(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
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
            api_timeout_seconds=30,
        ),
    )
    monkeypatch.setattr(integration, "_load_program", lambda program_id, root: program)

    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    shrink_mode = {"enabled": False}

    class _FakeDiscoveryProvider:
        def discover(self, program_id, config, existing, run_ctx=RunContext()):
            del config, existing, run_ctx
            ref_ids = ("100", "101", "102", "103", "104", "105")
            if shrink_mode["enabled"]:
                ref_ids = ("100",)
            return DiscoveryResult(
                channel="ado",
                program_id=program_id,
                discovered_refs=tuple(
                    DiscoveredRef(
                        registration=ChannelRegistration(
                            channel="ado",
                            program_id=program_id,
                            provider_instance_id="default",
                            ref_id=ref_id,
                            ref_kind="work_item",
                            status=RegistrationStatus.ACTIVE,
                            first_discovered_at=current_time,
                            last_seen_at=current_time,
                            ref_title=f"Item {ref_id}",
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
                    )
                    for ref_id in ref_ids
                ),
                completeness=DiscoveryCompleteness.FULL,
                scope_statuses={},
                scope_state_updates={},
                errors=(),
                computed_at=current_time,
            )

    monkeypatch.setattr(
        integration_discovery,
        "resolve_channel_bindings",
        lambda program_obj, workstreams, programs_root: (
            _make_binding(channel="ado", threshold_hours=24, ttl_days=30, provider=_FakeDiscoveryProvider()),
        ),
    )

    registry_path = integration._registry_path("demo", programs_root)
    backup_dir = programs_root / "demo" / "registry_backups"
    backup_one = backup_dir / "channel_registry-pre-shrinkage-20260524T120000Z.sqlite3"
    backup_two = backup_dir / "channel_registry-pre-shrinkage-20260524T130000Z.sqlite3"
    backup_calls: list[Path] = []

    def _fake_backup_path(program: str, root: Path, prefix: str = "channel_registry") -> Path:
        del program, root, prefix
        backup_path = backup_one if not backup_calls else backup_two
        backup_calls.append(backup_path)
        return backup_path

    monkeypatch.setattr(integration, "_backup_path", _fake_backup_path)

    seed_result = runner.invoke(
        app,
        ["integration", "discover", "--program", "demo", "--force", "--accept-shrinkage", "--programs-root", str(programs_root)],
    )
    assert seed_result.exit_code == 0
    assert registry_path.exists()
    assert "Pre-shrinkage backup:" not in seed_result.stdout
    assert backup_calls == []

    shrink_mode["enabled"] = True
    shrink_result = runner.invoke(
        app,
        ["integration", "discover", "--program", "demo", "--force", "--accept-shrinkage", "--programs-root", str(programs_root)],
    )

    assert shrink_result.exit_code == 0
    assert f"Pre-shrinkage backup: {backup_one}" in shrink_result.stdout
    assert backup_calls == [backup_one]
    backup_store = ChannelRegistryStore(backup_one, "demo")
    current_store = integration._store("demo", programs_root)
    assert len(backup_store.active_registrations("ado")) == 6
    assert len(current_store.active_registrations("ado")) == 1


def test_integration_discover_updates_gather_state_for_configured_provider_instance(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
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
            api_timeout_seconds=30,
        ),
    )
    monkeypatch.setattr(integration, "_load_program", lambda program_id, root: program)

    store = integration._store("demo", programs_root)
    seed_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
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
                        ref_id="900",
                        ref_kind="work_item",
                        status=RegistrationStatus.ACTIVE,
                        first_discovered_at=seed_time,
                        last_seen_at=seed_time,
                        ref_title="Default item",
                    ),
                    bindings=(
                        RegistrationBinding(
                            workstream_id="demo.slice",
                            scope_id="scope-default",
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
            computed_at=seed_time,
        )
    )

    current_time = datetime(2026, 5, 24, 12, 5, tzinfo=timezone.utc)

    class _FakeDiscoveryProvider:
        def discover(self, program_id, config, existing, run_ctx=RunContext()):
            del config, run_ctx
            assert existing == ()
            return DiscoveryResult(
                channel="ado",
                program_id=program_id,
                discovered_refs=(
                    DiscoveredRef(
                        registration=ChannelRegistration(
                            channel="ado",
                            program_id=program_id,
                            provider_instance_id="instance-a",
                            ref_id="101",
                            ref_kind="work_item",
                            status=RegistrationStatus.ACTIVE,
                            first_discovered_at=current_time,
                            last_seen_at=current_time,
                            ref_title="Instance item",
                        ),
                        bindings=(
                            RegistrationBinding(
                                workstream_id="demo.slice",
                                scope_id="scope-a",
                                source_type="wiql_saved_query",
                                confidence=1.0,
                                confidence_source="wiql_saved_query",
                            ),
                        ),
                    ),
                ),
                completeness=DiscoveryCompleteness.FULL,
                scope_statuses={
                    "scope-a": ScopeStatus(
                        scope_id="scope-a",
                        status=ScopeStatusKind.SUCCESS,
                        completeness=DiscoveryCompleteness.FULL,
                        item_count=1,
                    )
                },
                scope_state_updates={},
                errors=(),
                computed_at=current_time,
            )

    monkeypatch.setattr(
        integration_discovery,
        "resolve_channel_bindings",
        lambda program_obj, workstreams, programs_root: (
            ChannelBinding(
                config=ChannelConfig(
                    channel="ado",
                    enabled=True,
                    discovery_threshold_hours=24,
                    ttl_days=30,
                    extra={"instance_id": "instance-a"},
                ),
                discovery_provider=_FakeDiscoveryProvider(),
                hydration_provider=object(),
                signal_extractor=object(),
                discovery_config={"channel": "ado"},
                hydration_config=None,
            ),
        ),
    )

    result = runner.invoke(
        app,
        ["integration", "discover", "--program", "demo", "--force", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0
    state = load_gather_state("demo", programs_root=programs_root)
    assert state is not None
    assert state.channels["ado"]["uil_registry_size"] == 1
    assert state.channels["ado"]["uil_last_delta_summary"] == "+1 -0 ~0 =0"
    assert state.channels["ado"]["uil_scope_health"] == {"scope-a": "ok"}


def test_integration_discover_supports_kusto(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
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
                "    schema_introspection_enabled: false",
            ]
        ),
        encoding="utf-8",
    )

    gathered_at = datetime(2026, 5, 23, 9, 0, tzinfo=timezone.utc)
    write_gather_state(
        "demo",
        gathered_at=gathered_at,
        scanned_items=10,
        discovered_signals=4,
        new_signals=1,
        pending_review=0,
        trajectory_updates=2,
        auto_reviews_written=0,
        ado_calls=7,
        archived_journal_files=0,
        background_proposals=0,
        channels={},
        programs_root=programs_root,
    )

    program = Program(schema_version="3.0", id="demo", name="Demo", kusto=KustoConfig(enabled=True))
    monkeypatch.setattr(integration, "_load_program", lambda program_id, root: program)

    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)

    class _FakeKustoDiscoveryProvider:
        def discover(self, program_id, config, existing, run_ctx=RunContext()):
            del config, existing, run_ctx
            return _make_discovery_result(
                channel="kusto",
                program_id=program_id,
                ref_id="query-a",
                ref_kind="kusto_query",
                ref_title="Kusto Query A",
                current_time=current_time,
                workstream_id="demo.slice",
                scope_id="query-a",
                source_type="manual_config",
            )

    monkeypatch.setattr(
        integration_discovery,
        "resolve_channel_bindings",
        lambda program_obj, workstreams, programs_root: (
            _make_binding(channel="kusto", threshold_hours=168, ttl_days=30, provider=_FakeKustoDiscoveryProvider()),
        ),
    )

    result = runner.invoke(
        app,
        [
            "integration",
            "discover",
            "--program",
            "demo",
            "--channel",
            "kusto",
            "--force",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    state = load_gather_state("demo", programs_root=programs_root)
    assert state is not None
    assert state.channels["kusto"]["uil_last_delta_summary"] == "+1 -0 ~0 =0"
    assert state.channels["kusto"]["uil_registry_size"] == 1


def test_integration_discover_runs_all_enabled_configured_channels_by_default(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
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
                "    discovery_threshold_hours: 24",
                "    ttl_days: 30",
                "  kusto:",
                "    enabled: true",
                "    discovery_threshold_hours: 168",
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
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    monkeypatch.setattr(integration, "_load_program", lambda program_id, root: program)
    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    discover_calls: list[str] = []

    class _FakeADODiscoveryProvider:
        def discover(self, program_id, config, existing, run_ctx=RunContext()):
            del config, existing, run_ctx
            discover_calls.append("ado")
            return _make_discovery_result(
                channel="ado",
                program_id=program_id,
                ref_id="101",
                ref_kind="work_item",
                ref_title="Hydrated item",
                current_time=current_time,
                workstream_id="demo.slice",
                scope_id="scope",
                source_type="wiql_saved_query",
            )

    class _FakeKustoDiscoveryProvider:
        def discover(self, program_id, config, existing, run_ctx=RunContext()):
            del config, existing, run_ctx
            discover_calls.append("kusto")
            return _make_discovery_result(
                channel="kusto",
                program_id=program_id,
                ref_id="query-a",
                ref_kind="kusto_query",
                ref_title="Kusto Query A",
                current_time=current_time,
                workstream_id="demo.slice",
                scope_id="query-a",
                source_type="manual_config",
            )

    monkeypatch.setattr(
        integration_discovery,
        "resolve_channel_bindings",
        lambda program_obj, workstreams, programs_root: (
            _make_binding(channel="ado", threshold_hours=24, ttl_days=30, provider=_FakeADODiscoveryProvider()),
            _make_binding(channel="kusto", threshold_hours=168, ttl_days=30, provider=_FakeKustoDiscoveryProvider()),
        ),
    )

    result = runner.invoke(
        app,
        ["integration", "discover", "--program", "demo", "--force", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0
    assert discover_calls == ["ado", "kusto"]
    state = load_gather_state("demo", programs_root=programs_root)
    assert state is not None
    assert state.channels["ado"]["uil_registry_size"] == 1
    assert state.channels["kusto"]["uil_registry_size"] == 1


def test_integration_discover_skips_disabled_channels_by_default(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
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
                "    discovery_threshold_hours: 24",
                "    ttl_days: 30",
                "  kusto:",
                "    enabled: false",
                "    discovery_threshold_hours: 168",
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
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    monkeypatch.setattr(integration, "_load_program", lambda program_id, root: program)
    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    discover_calls: list[str] = []

    class _FakeADODiscoveryProvider:
        def discover(self, program_id, config, existing, run_ctx=RunContext()):
            del config, existing, run_ctx
            discover_calls.append("ado")
            return _make_discovery_result(
                channel="ado",
                program_id=program_id,
                ref_id="101",
                ref_kind="work_item",
                ref_title="Hydrated item",
                current_time=current_time,
                workstream_id="demo.slice",
                scope_id="scope",
                source_type="wiql_saved_query",
            )

    monkeypatch.setattr(
        integration_discovery,
        "resolve_channel_bindings",
        lambda program_obj, workstreams, programs_root: (_make_binding(channel="ado", threshold_hours=24, ttl_days=30, provider=_FakeADODiscoveryProvider()),),
    )

    result = runner.invoke(
        app,
        ["integration", "discover", "--program", "demo", "--force", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0
    assert discover_calls == ["ado"]
    state = load_gather_state("demo", programs_root=programs_root)
    assert state is not None
    assert "ado" in state.channels
    assert "kusto" not in state.channels


def test_integration_discover_noops_when_no_enabled_uil_channels_configured(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            [
                "schema_version: '3.0'",
                "id: demo",
                "name: Demo",
            ]
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["integration", "discover", "--program", "demo", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0
    assert "No enabled UIL channels configured for demo." in result.stdout


def test_integration_discover_reports_disabled_explicit_channel_cleanly(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            [
                "schema_version: '3.0'",
                "id: demo",
                "name: Demo",
                "channels:",
                "  kusto:",
                "    enabled: false",
                "    discovery_threshold_hours: 168",
            ]
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["integration", "discover", "--program", "demo", "--channel", "kusto", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0
    assert "UIL channel 'kusto' is configured but disabled for demo." in result.stdout


def test_integration_discover_passes_run_context_to_binding_provider(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
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
            api_timeout_seconds=30,
        ),
    )
    monkeypatch.setattr(integration, "_load_program", lambda program_id, root: program)

    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    seen_run_contexts: list[RunContext] = []

    class _FakeDiscoveryProvider:
        def discover(self, program_id, config, existing, run_ctx=RunContext()):
            del config, existing
            seen_run_contexts.append(run_ctx)
            return _make_discovery_result(
                channel="ado",
                program_id=program_id,
                ref_id="101",
                ref_kind="work_item",
                ref_title="Hydrated item",
                current_time=current_time,
                workstream_id="demo.slice",
                scope_id="scope",
                source_type="wiql_saved_query",
            )

    monkeypatch.setattr(
        integration_discovery,
        "resolve_channel_bindings",
        lambda program_obj, workstreams, programs_root: (_make_binding(channel="ado", threshold_hours=24, ttl_days=30, provider=_FakeDiscoveryProvider()),),
    )

    result = runner.invoke(
        app,
        [
            "integration",
            "discover",
            "--program",
            "demo",
            "--force",
            "--dry-run",
            "--accept-shrinkage",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert seen_run_contexts == [RunContext(dry_run=True, force_discovery=True, accept_shrinkage=True)]


def test_integration_discover_dry_run_does_not_create_registry_on_cold_start(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
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
            api_timeout_seconds=30,
        ),
    )
    monkeypatch.setattr(integration, "_load_program", lambda program_id, root: program)

    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)

    class _FakeDiscoveryProvider:
        def discover(self, program_id, config, existing, run_ctx=RunContext()):
            del config, existing, run_ctx
            return _make_discovery_result(
                channel="ado",
                program_id=program_id,
                ref_id="101",
                ref_kind="work_item",
                ref_title="Hydrated item",
                current_time=current_time,
                workstream_id="demo.slice",
                scope_id="scope",
                source_type="wiql_saved_query",
            )

    monkeypatch.setattr(
        integration_discovery,
        "resolve_channel_bindings",
        lambda program_obj, workstreams, programs_root: (_make_binding(channel="ado", threshold_hours=24, ttl_days=30, provider=_FakeDiscoveryProvider()),),
    )

    registry_path = programs_root / "demo" / "channel_registry.sqlite3"
    result = runner.invoke(
        app,
        ["integration", "discover", "--program", "demo", "--force", "--dry-run", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0
    assert "ADO discovery dry-run: +1 -0 ~0 =0" in result.stdout
    assert not registry_path.exists()


def test_integration_discover_records_scope_health_when_shrinkage_guard_blocks_update(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
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
            api_timeout_seconds=30,
        ),
    )
    monkeypatch.setattr(integration, "_load_program", lambda program_id, root: program)

    current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)

    class _FakeDiscoveryProvider:
        def discover(self, program_id, config, existing, run_ctx=RunContext()):
            del config, run_ctx
            if not existing:
                refs = tuple(
                    DiscoveredRef(
                        registration=ChannelRegistration(
                            channel="ado",
                            program_id=program_id,
                            provider_instance_id="instance-a",
                            ref_id=str(ref_id),
                            ref_kind="work_item",
                            status=RegistrationStatus.ACTIVE,
                            first_discovered_at=current_time - timedelta(hours=1),
                            last_seen_at=current_time - timedelta(hours=1),
                            ref_title=f"WI {ref_id}",
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
                    )
                    for ref_id in range(100, 106)
                )
                return DiscoveryResult(
                    channel="ado",
                    program_id=program_id,
                    discovered_refs=refs,
                    completeness=DiscoveryCompleteness.FULL,
                    scope_statuses={
                        "scope": ScopeStatus(
                            scope_id="scope",
                            status=ScopeStatusKind.SUCCESS,
                            completeness=DiscoveryCompleteness.FULL,
                            item_count=len(refs),
                        )
                    },
                    scope_state_updates={},
                    errors=(),
                    computed_at=current_time - timedelta(hours=1),
                )
            return DiscoveryResult(
                channel="ado",
                program_id=program_id,
                discovered_refs=(
                    DiscoveredRef(
                        registration=ChannelRegistration(
                            channel="ado",
                            program_id=program_id,
                            provider_instance_id="instance-a",
                            ref_id="100",
                            ref_kind="work_item",
                            status=RegistrationStatus.ACTIVE,
                            first_discovered_at=current_time,
                            last_seen_at=current_time,
                            ref_title="WI 100",
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
                scope_statuses={
                    "scope": ScopeStatus(
                        scope_id="scope",
                        status=ScopeStatusKind.ERROR,
                        completeness=DiscoveryCompleteness.PARTIAL,
                        item_count=1,
                        error_message="partial failure",
                    )
                },
                scope_state_updates={},
                errors=(),
                computed_at=current_time,
            )

    monkeypatch.setattr(
        integration_discovery,
        "resolve_channel_bindings",
        lambda program_obj, workstreams, programs_root: (
            ChannelBinding(
                config=ChannelConfig(
                    channel="ado",
                    enabled=True,
                    discovery_threshold_hours=24,
                    ttl_days=30,
                    extra={"instance_id": "instance-a"},
                ),
                discovery_provider=_FakeDiscoveryProvider(),
                hydration_provider=object(),
                signal_extractor=object(),
                discovery_config={"channel": "ado"},
                hydration_config=None,
            ),
        ),
    )

    seed_result = runner.invoke(
        app,
        ["integration", "discover", "--program", "demo", "--force", "--accept-shrinkage", "--programs-root", str(programs_root)],
    )
    assert seed_result.exit_code == 0

    result = runner.invoke(
        app,
        ["integration", "discover", "--program", "demo", "--force", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 2
    store = integration._store("demo", programs_root)
    assert store.recent_scope_health("ado", provider_instance_id="instance-a") == {"scope": "error_1x"}


# ---------------------------------------------------------------------------
# migrate command
# ---------------------------------------------------------------------------

def _write_program_yaml(program_dir: Path) -> None:
    (program_dir / "program.yaml").write_text(
        "schema_version: '3.0'\nid: demo\nname: Demo\n",
        encoding="utf-8",
    )


def _write_m365_registry(program_dir: Path, content: str) -> None:
    (program_dir / "m365_registry.yaml").write_text(content, encoding="utf-8")


def test_integrate_migrate_initializes_schema_when_no_m365_registry(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    _write_program_yaml(program_dir)

    result = runner.invoke(app, ["integration", "migrate", "--program", "demo", "--programs-root", str(programs_root)])

    assert result.exit_code == 0
    assert "schema is current" in result.stdout or "No m365_registry.yaml" in result.stdout


def test_integrate_migrate_imports_artifacts_into_uil_teams_channel(tmp_path: Path) -> None:
    from datetime import date
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    _write_program_yaml(program_dir)
    _write_m365_registry(program_dir, "\n".join([
        "schema_version: '1.0'",
        "program_id: demo",
        "last_updated: '2026-01-01T00:00:00+00:00'",
        "artifacts:",
        "  - artifact_id: series-abc123",
        "    artifact_type: meeting_series",
        "    inferred_workstream: demo.slice",
        "    confidence: 0.9",
        "    confidence_source: workiq",
        "    pm_confirmed: true",
        "    promoted_to_workstreams_yaml: false",
        "    first_seen: '2025-06-01'",
        "    last_seen: '2026-01-01'",
        "    display_name: Weekly Sync",
        "    series_id: abc123",
        "    signal_yield_last_3: [3, 2, 1]",
    ]))

    result = runner.invoke(app, ["integration", "migrate", "--program", "demo", "--programs-root", str(programs_root)])

    assert result.exit_code == 0, result.stdout
    assert "Migrating 1 M365 artifacts" in result.stdout
    assert "Migration complete" in result.stdout

    store = integration._store("demo", programs_root)
    regs = store.all_registrations("teams")
    assert len(regs) == 1
    reg = regs[0]
    assert reg.ref_id == "abc123"
    assert reg.ref_kind == "meeting_series"
    assert reg.confidence == 0.9
    assert reg.pm_confirmed is True
    assert reg.ref_title == "Weekly Sync"
    assert reg.signal_yield_last_3 == (3, 2, 1)


def test_integrate_migrate_dry_run_shows_artifacts_without_writing(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    _write_program_yaml(program_dir)
    _write_m365_registry(program_dir, "\n".join([
        "schema_version: '1.0'",
        "program_id: demo",
        "last_updated: '2026-01-01T00:00:00+00:00'",
        "artifacts:",
        "  - artifact_id: thread-xyz456",
        "    artifact_type: chat_thread",
        "    inferred_workstream: demo.slice",
        "    confidence: 0.7",
        "    confidence_source: workiq",
        "    pm_confirmed: false",
        "    promoted_to_workstreams_yaml: false",
        "    first_seen: '2025-06-01'",
        "    last_seen: '2026-01-01'",
        "    thread_id: xyz456",
    ]))

    result = runner.invoke(app, ["integration", "migrate", "--program", "demo", "--dry-run", "--programs-root", str(programs_root)])

    assert result.exit_code == 0, result.stdout
    assert "Migrating 1 M365 artifacts" in result.stdout
    assert "teams_chat:xyz456" in result.stdout
    # dry-run: no registry file created
    registry_path = programs_root / "demo" / "channel_registry.sqlite3"
    assert not registry_path.exists()


def test_integrate_migrate_maps_thread_id_to_teams_chat_ref_kind(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    _write_program_yaml(program_dir)
    _write_m365_registry(program_dir, "\n".join([
        "schema_version: '1.0'",
        "program_id: demo",
        "last_updated: '2026-01-01T00:00:00+00:00'",
        "artifacts:",
        "  - artifact_id: chat-abc",
        "    artifact_type: teams_chat",
        "    inferred_workstream: demo.core",
        "    confidence: 0.8",
        "    confidence_source: workiq",
        "    pm_confirmed: false",
        "    promoted_to_workstreams_yaml: false",
        "    first_seen: '2025-06-01'",
        "    last_seen: '2026-01-01'",
        "    thread_id: tid-abc",
    ]))

    result = runner.invoke(app, ["integration", "migrate", "--program", "demo", "--programs-root", str(programs_root)])

    assert result.exit_code == 0, result.stdout
    store = integration._store("demo", programs_root)
    regs = store.all_registrations("teams")
    assert len(regs) == 1
    assert regs[0].ref_id == "tid-abc"
    assert regs[0].ref_kind == "teams_chat"


def test_integrate_migrate_routes_email_threads_to_email_channel(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    _write_program_yaml(program_dir)
    _write_m365_registry(program_dir, "\n".join([
        "schema_version: '1.0'",
        "program_id: demo",
        "last_updated: '2026-01-01T00:00:00+00:00'",
        "artifacts:",
        "  - artifact_id: thread:auto:mail-1",
        "    artifact_type: email_thread",
        "    inferred_workstream: demo.core",
        "    confidence: 0.8",
        "    confidence_source: workiq",
        "    pm_confirmed: false",
        "    promoted_to_workstreams_yaml: false",
        "    first_seen: '2025-06-01'",
        "    last_seen: '2026-01-01'",
        "    display_name: Mail Thread",
        "    thread_id: mail-thread-123",
    ]))

    result = runner.invoke(app, ["integration", "migrate", "--program", "demo", "--programs-root", str(programs_root)])

    assert result.exit_code == 0, result.stdout
    store = integration._store("demo", programs_root)
    email_regs = store.all_registrations("email")
    assert len(email_regs) == 1
    assert email_regs[0].ref_id == "mail-thread-123"
    assert email_regs[0].ref_kind == "email_thread"


def test_integrate_migrate_round_trips_all_governance_fields(tmp_path: Path) -> None:
    """Load M365RegistryArtifact → write to UIL → read from UIL → compare all governance fields."""
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    _write_program_yaml(program_dir)
    _write_m365_registry(program_dir, "\n".join([
        "schema_version: '1.0'",
        "program_id: demo",
        "last_updated: '2026-01-01T00:00:00+00:00'",
        "artifacts:",
        "  - artifact_id: series-roundtrip",
        "    artifact_type: meeting_series",
        "    inferred_workstream: demo.slice",
        "    confidence: 0.85",
        "    confidence_source: workiq",
        "    pm_confirmed: true",
        "    promoted_to_workstreams_yaml: true",
        "    first_seen: '2025-06-01'",
        "    last_seen: '2026-01-01'",
        "    display_name: Round Trip Sync",
        "    series_id: series-rt-001",
        "    signal_yield_last_3: [5, 4, 3]",
    ]))

    result = runner.invoke(app, ["integration", "migrate", "--program", "demo", "--programs-root", str(programs_root)])

    assert result.exit_code == 0, result.stdout

    store = integration._store("demo", programs_root)
    regs = store.all_registrations("teams")
    assert len(regs) == 1
    reg = regs[0]

    # Identity fields
    assert reg.ref_id == "series-rt-001"
    assert reg.ref_kind == "meeting_series"
    # Governance fields — all must survive round-trip
    assert reg.confidence == 0.85
    assert reg.confidence_source == "workiq"
    assert reg.pm_confirmed is True
    assert reg.promoted is True
    assert reg.ref_title == "Round Trip Sync"
    assert reg.signal_yield_last_3 == (5, 4, 3)
    # Workstream binding must be preserved
    assert "demo.slice" in reg.workstream_ids


def test_integration_show_respects_ref_title_visible_config(tmp_path: Path) -> None:
    """When ref_title_visible: true is set in program.yaml for a channel, show exposes plaintext titles without --reveal-titles."""
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            [
                "schema_version: '3.0'",
                "id: demo",
                "name: Demo",
                "channels:",
                "  teams:",
                "    enabled: true",
                "    discovery_threshold_hours: 48",
                "    ref_title_visible: true",
            ]
        ),
        encoding="utf-8",
    )
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    store = integration._store("demo", programs_root)
    store.apply_discovery_result(
        _make_discovery_result(
            channel="teams",
            program_id="demo",
            ref_id="series-abc",
            ref_kind="meeting_series",
            ref_title="Acme Weekly Sync",
            current_time=now,
            workstream_id="demo.slice",
            scope_id="static_config",
            source_type="m365_migration",
        )
    )

    result = runner.invoke(
        app,
        ["integration", "show", "--program", "demo", "--channel", "teams",
         "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0, result.stdout
    # Title should be shown in plaintext (ref_title_visible=true) without --reveal-titles
    assert "Acme Weekly Sync" in result.stdout


def test_integration_prune_removes_old_retired_registrations(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    old_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = ChannelRegistryStore(program_dir / "runtime" / "channel_registry.sqlite3", "demo")
    # Seed two ADO registrations then retire one
    for ref_id in ("101", "102"):
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
                            ref_id=ref_id,
                            ref_kind="work_item",
                            status=RegistrationStatus.ACTIVE,
                            first_discovered_at=now,
                            last_seen_at=now,
                        ),
                        bindings=(
                            RegistrationBinding(
                                workstream_id="demo.slice",
                                scope_id="scope",
                                source_type="wiql_saved_query",
                                confidence=1.0,
                                confidence_source="manual_config",
                            ),
                        ),
                    ),
                ),
                completeness=DiscoveryCompleteness.FULL,
                scope_statuses={
                    "scope": ScopeStatus(
                        scope_id="scope",
                        status=ScopeStatusKind.SUCCESS,
                        completeness=DiscoveryCompleteness.FULL,
                        item_count=1,
                    )
                },
                scope_state_updates={},
                errors=(),
                computed_at=now,
            )
        )
    store.retire("ado", "101", "work_item")
    # Back-date retired_at for ref 101 to simulate old retirement
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(str(program_dir / "runtime" / "channel_registry.sqlite3"))
    conn.execute("UPDATE registrations SET retired_at = ? WHERE ref_id = '101'", (old_time.strftime("%Y-%m-%dT%H:%M:%S"),))
    conn.commit()
    conn.close()

    result = runner.invoke(
        app,
        ["integration", "prune", "--program", "demo", "--channel", "ado",
         "--older-than-days", "30", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0, result.stdout
    assert "1" in result.stdout  # 1 registration pruned
    regs = store.all_registrations("ado")
    ref_ids = {r.ref_id for r in regs}
    assert "101" not in ref_ids
    assert "102" in ref_ids


def test_integration_prune_dry_run_does_not_delete(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    store = ChannelRegistryStore(program_dir / "channel_registry.sqlite3", "demo")
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
                        first_discovered_at=now,
                        last_seen_at=now,
                    ),
                    bindings=(
                        RegistrationBinding(
                            workstream_id="demo.slice",
                            scope_id="scope",
                            source_type="wiql_saved_query",
                            confidence=1.0,
                            confidence_source="manual_config",
                        ),
                    ),
                ),
            ),
            completeness=DiscoveryCompleteness.FULL,
            scope_statuses={
                "scope": ScopeStatus(
                    scope_id="scope",
                    status=ScopeStatusKind.SUCCESS,
                    completeness=DiscoveryCompleteness.FULL,
                    item_count=1,
                )
            },
            scope_state_updates={},
            errors=(),
            computed_at=now,
        )
    )
    store.retire("ado", "101", "work_item")

    result = runner.invoke(
        app,
        ["integration", "prune", "--program", "demo", "--channel", "ado",
         "--older-than-days", "0", "--dry-run", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0, result.stdout
    assert "dry-run" in result.stdout
    # Nothing deleted
    regs = store.all_registrations("ado")
    assert any(r.ref_id == "101" for r in regs)


def test_integration_reassign_updates_workstream_in_uil_store(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    store = ChannelRegistryStore(program_dir / "runtime" / "channel_registry.sqlite3", "demo")
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
                        first_discovered_at=now,
                        last_seen_at=now,
                    ),
                    bindings=(
                        RegistrationBinding(
                            workstream_id="ws-a",
                            scope_id="scope",
                            source_type="wiql_saved_query",
                            confidence=1.0,
                            confidence_source="manual_config",
                        ),
                    ),
                ),
            ),
            completeness=DiscoveryCompleteness.FULL,
            scope_statuses={
                "scope": ScopeStatus(
                    scope_id="scope",
                    status=ScopeStatusKind.SUCCESS,
                    completeness=DiscoveryCompleteness.FULL,
                    item_count=1,
                )
            },
            scope_state_updates={},
            errors=(),
            computed_at=now,
        )
    )

    result = runner.invoke(
        app,
        ["integration", "reassign", "--program", "demo", "--channel", "ado",
         "--ref-id", "101", "--ref-kind", "work_item", "--workstream", "ws-b",
         "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0, result.stdout
    regs = store.active_registrations("ado")
    assert len(regs) == 1
    assert "ws-b" in regs[0].workstream_ids
    assert "ws-a" not in regs[0].workstream_ids


def test_integration_schema_migrate_noops_when_schema_is_current(tmp_path: Path) -> None:
    """schema-migrate reports nothing to do when the DB is already at the current version."""
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    # Create a registry at current schema version.
    store = ChannelRegistryStore(program_dir / "channel_registry.sqlite3", "demo")
    store.ensure_schema()

    result = runner.invoke(
        app,
        ["integration", "schema-migrate", "--program", "demo", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0, result.stdout
    assert "already at schema version" in result.stdout


def test_integration_schema_migrate_with_force_reinitializes_bad_version(tmp_path: Path) -> None:
    """schema-migrate --force creates a backup and reinitializes a mismatched-version registry."""
    import sqlite3
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    db_path = program_dir / "channel_registry.sqlite3"
    # Create a registry, then forcibly set an unknown schema version.
    store = ChannelRegistryStore(db_path, "demo")
    store.ensure_schema()
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', '99')")
    conn.commit()
    # Add one row so the version guard fires (has_data check)
    conn.execute(
        "INSERT INTO registrations (channel, program_id, provider_instance_id, ref_id, ref_kind, status, "
        "first_discovered_at, last_seen_at, confidence, confidence_source) "
        "VALUES ('ado', 'demo', 'default', '101', 'work_item', 'active', '2026-01-01', '2026-01-01', 1.0, 'manual')"
    )
    conn.commit()
    conn.close()

    result = runner.invoke(
        app,
        ["integration", "schema-migrate", "--program", "demo", "--force", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0, result.stdout
    assert "Backup created" in result.stdout
    assert "re-initialized" in result.stdout
    # A backup file should exist in the registry_backups subdirectory.
    backups = list((program_dir / "registry_backups").glob("channel_registry-*.sqlite3"))
    assert len(backups) >= 1


def test_integrate_migrate_also_migrates_routing_feedback_events(tmp_path: Path) -> None:
    """migrate command reads m365_routing_feedback.jsonl and writes events to registry_feedback."""
    import json as _json
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    (program_dir / "_feedback").mkdir(parents=True)
    feedback_path = program_dir / "_feedback" / "m365_routing_feedback.jsonl"
    feedback_path.write_text(
        _json.dumps({
            "ts": "2026-05-01T10:00:00",
            "artifact_id": "series-abc",
            "action": "reject",
            "pm_alias": "jsmith",
            "workstream_id": "ws-b",
            "prior_workstream_id": "ws-a",
            "series_id": "series-abc",
            "reason": "wrong workstream",
        }) + "\n",
        encoding="utf-8",
    )
    # Write a minimal m365_registry.yaml so migrate proceeds.
    (program_dir / "m365_registry.yaml").write_text(
        'schema_version: "1.0"\nartifacts: []\nfeedback_events: []\n',
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["integration", "migrate", "--program", "demo", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0, result.stdout
    assert "routing feedback" in result.stdout.lower() or "feedback" in result.stdout.lower()
    store = ChannelRegistryStore(program_dir / "runtime" / "channel_registry.sqlite3", "demo")
    events = store.load_feedback_events("teams", "series-abc", "meeting_series")
    assert len(events) == 1
    assert events[0].action == "reject"
    assert events[0].pm_alias == "jsmith"
    assert events[0].workstream_id == "ws-b"
    assert events[0].prior_workstream_id == "ws-a"


def test_integration_ref_id_migrates_registration_atomically(tmp_path: Path) -> None:
    """vertex integration ref-id migrates a registration to a new ref_id in the UIL store."""
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    store = ChannelRegistryStore(program_dir / "runtime" / "channel_registry.sqlite3", "demo")
    store.apply_discovery_result(
        _make_discovery_result(
            channel="teams",
            program_id="demo",
            ref_id="old-thread-id",
            ref_kind="teams_message",
            ref_title="Acme Weekly",
            current_time=datetime(2026, 5, 24, tzinfo=timezone.utc),
            workstream_id="ws-a",
            scope_id="scope-1",
            source_type="teams_channel",
        )
    )

    result = runner.invoke(
        app,
        [
            "integration", "ref-id",
            "--program", "demo",
            "--channel", "teams",
            "--old-ref-id", "old-thread-id",
            "--new-ref-id", "new-thread-id",
            "--ref-kind", "teams_message",
            "--pm", "pm@test",
            "--reason", "thread rotation after meeting series moved",
            "--programs-root", str(programs_root),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "new-thread-id" in result.stdout
    regs = store.active_registrations("teams")
    ref_ids = {r.ref_id for r in regs}
    assert "old-thread-id" not in ref_ids
    assert "new-thread-id" in ref_ids


def test_integration_ref_id_error_on_missing_source(tmp_path: Path) -> None:
    """vertex integration ref-id exits 1 when old_ref_id does not exist in the store."""
    programs_root = tmp_path / "programs"
    (programs_root / "demo").mkdir(parents=True)
    # Ensure schema is initialised but empty.
    ChannelRegistryStore(programs_root / "demo" / "channel_registry.sqlite3", "demo").ensure_schema()

    result = runner.invoke(
        app,
        [
            "integration", "ref-id",
            "--program", "demo",
            "--channel", "teams",
            "--old-ref-id", "does-not-exist",
            "--new-ref-id", "new-ref",
            "--ref-kind", "teams_message",
            "--pm", "pm@test",
            "--programs-root", str(programs_root),
        ],
    )

    assert result.exit_code == 1


def test_integration_candidates_lists_bootstrapped_candidates_as_json(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "demo").mkdir(parents=True)
    candidate_store = SourceCandidateStore(programs_root / "demo" / "runtime" / "channel_registry.sqlite3", "demo")
    as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    candidate_store.bootstrap_intents(
        workstreams=(
            Workstream(
                id="demo.acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Review"),),
                ),
            ),
        ),
        registry_artifacts=(),
        as_of=as_of,
    )
    intent = candidate_store.list_intents(workstream_id="demo.acme")[0]
    candidate = SourceCandidate(
        candidate_id=build_source_candidate_id(
            program_id="demo",
            channel="teams",
            provider_instance_id="default",
            ref_kind=SourceRefKind.MEETING_SERIES,
            ref_id="series-123",
        ),
        program_id="demo",
        channel="teams",
        provider_instance_id="default",
        ref_id="series-123",
        ref_kind=SourceRefKind.MEETING_SERIES,
        display_name="Acme Weekly Review",
        confidence=0.93,
        source_provider="graph_calendar",
        status=SourceCandidateStatus.PENDING,
        evidence_json=candidate_evidence_json({"matched_terms": ["Acme Weekly Review"]}),
        first_discovered_at=as_of,
        last_seen_at=as_of,
    )
    candidate_store.upsert_candidate(candidate, pii_prescrubbed=True)
    candidate_store.link_candidate_to_intent(candidate.candidate_id, intent.intent_id, 0.93)
    monkeypatch.setattr(integration, "_bootstrap_discovery_state", lambda *args, **kwargs: None)

    result = runner.invoke(
        app,
        ["integration", "candidates", "--program", "demo", "--json", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0, result.stdout
    assert '"candidate_id"' in result.stdout
    assert '"ref_id": "series-123"' in result.stdout
    assert '"intent_id"' in result.stdout


def test_integration_seed_id_creates_registration_and_resolves_intent(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "demo").mkdir(parents=True)
    candidate_store = SourceCandidateStore(programs_root / "demo" / "runtime" / "channel_registry.sqlite3", "demo")
    as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    candidate_store.bootstrap_intents(
        workstreams=(
            Workstream(
                id="demo.acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Review"),),
                ),
            ),
        ),
        registry_artifacts=(),
        as_of=as_of,
    )
    intent = candidate_store.list_intents(workstream_id="demo.acme")[0]
    monkeypatch.setattr(integration, "_bootstrap_discovery_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(integration, "_load_program", lambda program_id, root: Program(schema_version="3.0", id="demo", name="Demo"))
    monkeypatch.setattr(
        integration_discovery,
        "resolve_channel_config",
        lambda program, channel, programs_root: ChannelConfig(
            channel=channel,
            enabled=True,
            discovery_threshold_hours=24,
            ttl_days=30,
            extra={"instance_id": "default"},
        ),
    )

    result = runner.invoke(
        app,
        [
            "integration",
            "seed-id",
            "--program",
            "demo",
            "--intent-id",
            intent.intent_id,
            "--ref-id",
            "series-123",
            "--pm-alias",
            "pm@test",
            "--reason",
            "validated from Teams",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "series-123" in result.stdout
    assert candidate_store.get_intent(intent.intent_id).status == SourceIntentStatus.RESOLVED
    candidate = candidate_store.get_candidate_by_ref(ref_id="series-123", ref_kind=SourceRefKind.MEETING_SERIES)
    assert candidate is not None
    assert candidate.status == SourceCandidateStatus.ACCEPTED
    registrations = ChannelRegistryStore(programs_root / "demo" / "runtime" / "channel_registry.sqlite3", "demo").active_registrations("teams")
    assert {registration.ref_id for registration in registrations} == {"series-123"}


def test_integration_seed_plan_reports_unresolved_intents_with_lookup_hints(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "demo" / "runtime").mkdir(parents=True)
    candidate_store = SourceCandidateStore(programs_root / "demo" / "runtime" / "channel_registry.sqlite3", "demo")
    as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    candidate_store.bootstrap_intents(
        workstreams=(
            Workstream(
                id="demo.acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Review"),),
                ),
            ),
        ),
        registry_artifacts=(),
        as_of=as_of,
    )
    intent = candidate_store.list_intents(workstream_id="demo.acme")[0]
    candidate_store.record_attempt(
        DiscoveryAttempt(
            attempt_id=build_discovery_attempt_id(
                program_id="demo",
                intent_id=intent.intent_id,
                source_provider="workiq_calendar",
                query_hash="query-1",
                attempted_at=as_of,
            ),
            program_id="demo",
            intent_id=intent.intent_id,
            workstream_id="demo.acme",
            channel="teams",
            provider_instance_id="default",
            ref_kind=SourceRefKind.MEETING_SERIES,
            source_provider="workiq_calendar",
            query_hash="query-1",
            config_hash="config-1",
            autonomous_run_id=None,
            outcome=DiscoveryAttemptOutcome.NO_CANDIDATES,
            reason="query returned zero candidates",
            result_count=0,
            duration_ms=2100,
            attempted_at=as_of,
            expires_at=as_of + timedelta(days=7),
        )
    )
    monkeypatch.setattr(integration, "_bootstrap_discovery_state", lambda *args, **kwargs: None)

    result = runner.invoke(
        app,
        ["integration", "seed-plan", "--program", "demo", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0, result.stdout
    assert "Found 1 unresolved source intents" in result.stdout
    assert "Acme Weekly Review" in result.stdout
    assert "State: no_candidates" in result.stdout
    assert "Need: series_id" in result.stdout
    assert "seriesMasterId" in result.stdout
    assert f"vertex integration seed-id --program demo --intent-id {intent.intent_id}" in result.stdout


def test_integration_seed_plan_json_includes_graph_request_and_attempt_state(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "demo" / "runtime").mkdir(parents=True)
    candidate_store = SourceCandidateStore(programs_root / "demo" / "runtime" / "channel_registry.sqlite3", "demo")
    as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    candidate_store.bootstrap_intents(
        workstreams=(
            Workstream(
                id="demo.acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_chats=(
                        TeamsChat(display_name="Acme Eng Core Chat"),
                    ),
                ),
            ),
        ),
        registry_artifacts=(),
        as_of=as_of,
    )
    intent = next(item for item in candidate_store.list_intents() if item.ref_kind == SourceRefKind.TEAMS_CHAT)
    monkeypatch.setattr(integration, "_bootstrap_discovery_state", lambda *args, **kwargs: None)

    result = runner.invoke(
        app,
        ["integration", "seed-plan", "--program", "demo", "--json", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert len(payload) == 1
    assert payload[0]["intent_id"] == intent.intent_id
    assert payload[0]["ref_kind"] == "teams_chat"
    assert payload[0]["required_ref_field"] == "thread_id"
    assert payload[0]["graph_request_path"] == "/v1.0/chats?$top=100&$select=id,topic,webUrl"
    assert payload[0]["derived_state"] == "declared"


def test_integration_explain_source_reports_intent_candidates_and_next_action(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "demo").mkdir(parents=True)
    candidate_store = SourceCandidateStore(programs_root / "demo" / "runtime" / "channel_registry.sqlite3", "demo")
    as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    candidate_store.bootstrap_intents(
        workstreams=(
            Workstream(
                id="demo.acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Review"),),
                ),
            ),
        ),
        registry_artifacts=(),
        as_of=as_of,
    )
    intent = candidate_store.list_intents(workstream_id="demo.acme")[0]
    candidate = SourceCandidate(
        candidate_id=build_source_candidate_id(
            program_id="demo",
            channel="teams",
            provider_instance_id="default",
            ref_kind=SourceRefKind.MEETING_SERIES,
            ref_id="series-123",
        ),
        program_id="demo",
        channel="teams",
        provider_instance_id="default",
        ref_id="series-123",
        ref_kind=SourceRefKind.MEETING_SERIES,
        display_name="Acme Weekly Review",
        confidence=0.93,
        source_provider="graph_calendar",
        status=SourceCandidateStatus.PENDING,
        evidence_json=candidate_evidence_json({"matched_terms": ["Acme Weekly Review"]}),
        first_discovered_at=as_of,
        last_seen_at=as_of,
    )
    candidate_store.upsert_candidate(candidate, pii_prescrubbed=True)
    candidate_store.link_candidate_to_intent(candidate.candidate_id, intent.intent_id, 0.93)
    monkeypatch.setattr(integration, "_bootstrap_discovery_state", lambda *args, **kwargs: None)

    result = runner.invoke(
        app,
        [
            "integration",
            "explain-source",
            "--program",
            "demo",
            "--intent-id",
            intent.intent_id,
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "Intent" in result.stdout
    assert intent.intent_id in result.stdout
    assert "series-123" in result.stdout
    assert "Review the pending candidate evidence" in result.stdout


def test_integration_candidate_accept_requires_explicit_intent_when_candidate_matches_multiple(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "demo").mkdir(parents=True)
    candidate_store = SourceCandidateStore(programs_root / "demo" / "channel_registry.sqlite3", "demo")
    as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    candidate_store.bootstrap_intents(
        workstreams=(
            Workstream(
                id="demo.acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Review"),),
                ),
            ),
            Workstream(
                id="demo.ops",
                name="Ops",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Review"),),
                ),
            ),
        ),
        registry_artifacts=(),
        as_of=as_of,
    )
    intents = candidate_store.list_intents(ref_kind=SourceRefKind.MEETING_SERIES)
    candidate = SourceCandidate(
        candidate_id=build_source_candidate_id(
            program_id="demo",
            channel="teams",
            provider_instance_id="default",
            ref_kind=SourceRefKind.MEETING_SERIES,
            ref_id="series-123",
        ),
        program_id="demo",
        channel="teams",
        provider_instance_id="default",
        ref_id="series-123",
        ref_kind=SourceRefKind.MEETING_SERIES,
        display_name="Acme Weekly Review",
        confidence=0.93,
        source_provider="graph_calendar",
        status=SourceCandidateStatus.PENDING,
        evidence_json=candidate_evidence_json({"matched_terms": ["Acme Weekly Review"]}),
        first_discovered_at=as_of,
        last_seen_at=as_of,
    )
    candidate_store.upsert_candidate(candidate, pii_prescrubbed=True)
    for intent in intents:
        candidate_store.link_candidate_to_intent(candidate.candidate_id, intent.intent_id, 0.93)
    monkeypatch.setattr(integration, "_bootstrap_discovery_state", lambda *args, **kwargs: None)

    result = runner.invoke(
        app,
        [
            "integration",
            "candidate-accept",
            candidate.candidate_id,
            "--program",
            "demo",
            "--pm-alias",
            "pm@test",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code != 0


def test_integration_candidate_accept_resolves_selected_intent_and_unlinks_stale_match(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "demo").mkdir(parents=True)
    candidate_store = SourceCandidateStore(programs_root / "demo" / "runtime" / "channel_registry.sqlite3", "demo")
    as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    candidate_store.bootstrap_intents(
        workstreams=(
            Workstream(
                id="demo.acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Review"),),
                ),
            ),
            Workstream(
                id="demo.ops",
                name="Ops",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Review"),),
                ),
            ),
        ),
        registry_artifacts=(),
        as_of=as_of,
    )
    intents = candidate_store.list_intents(ref_kind=SourceRefKind.MEETING_SERIES)
    selected_intent = next(intent for intent in intents if intent.workstream_id == "demo.acme")
    stale_intent = next(intent for intent in intents if intent.workstream_id == "demo.ops")
    candidate = SourceCandidate(
        candidate_id=build_source_candidate_id(
            program_id="demo",
            channel="teams",
            provider_instance_id="default",
            ref_kind=SourceRefKind.MEETING_SERIES,
            ref_id="series-123",
        ),
        program_id="demo",
        channel="teams",
        provider_instance_id="default",
        ref_id="series-123",
        ref_kind=SourceRefKind.MEETING_SERIES,
        display_name="Acme Weekly Review",
        confidence=0.93,
        source_provider="graph_calendar",
        status=SourceCandidateStatus.PENDING,
        evidence_json=candidate_evidence_json({"matched_terms": ["Acme Weekly Review"]}),
        first_discovered_at=as_of,
        last_seen_at=as_of,
    )
    candidate_store.upsert_candidate(candidate, pii_prescrubbed=True)
    for intent in intents:
        candidate_store.link_candidate_to_intent(candidate.candidate_id, intent.intent_id, 0.93)
    monkeypatch.setattr(integration, "_bootstrap_discovery_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(integration, "_load_program", lambda program_id, root: Program(schema_version="3.0", id="demo", name="Demo"))
    monkeypatch.setattr(
        integration_discovery,
        "resolve_channel_config",
        lambda program, channel, programs_root: ChannelConfig(
            channel=channel,
            enabled=True,
            discovery_threshold_hours=24,
            ttl_days=30,
            extra={"instance_id": "default"},
        ),
    )

    result = runner.invoke(
        app,
        [
            "integration",
            "candidate-accept",
            candidate.candidate_id,
            "--program",
            "demo",
            "--intent-id",
            selected_intent.intent_id,
            "--pm-alias",
            "pm@test",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0, result.stdout
    refreshed_candidate = candidate_store.get_candidate(candidate.candidate_id)
    assert refreshed_candidate is not None
    assert refreshed_candidate.status == SourceCandidateStatus.ACCEPTED
    assert candidate_store.get_intent(selected_intent.intent_id).status == SourceIntentStatus.RESOLVED
    assert candidate_store.get_intent(stale_intent.intent_id).status == SourceIntentStatus.DECLARED
    registrations = ChannelRegistryStore(programs_root / "demo" / "runtime" / "channel_registry.sqlite3", "demo").active_registrations("teams")
    assert len(registrations) == 1
    assert registrations[0].workstream_ids == ("demo.acme",)


def test_integration_candidate_reject_suppresses_seeded_registration(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "demo").mkdir(parents=True)
    candidate_store = SourceCandidateStore(programs_root / "demo" / "runtime" / "channel_registry.sqlite3", "demo")
    as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    candidate_store.bootstrap_intents(
        workstreams=(
            Workstream(
                id="demo.acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Review"),),
                ),
            ),
        ),
        registry_artifacts=(),
        as_of=as_of,
    )
    intent = candidate_store.list_intents(workstream_id="demo.acme")[0]
    monkeypatch.setattr(integration, "_bootstrap_discovery_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(integration, "_load_program", lambda program_id, root: Program(schema_version="3.0", id="demo", name="Demo"))
    monkeypatch.setattr(
        integration_discovery,
        "resolve_channel_config",
        lambda program, channel, programs_root: ChannelConfig(
            channel=channel,
            enabled=True,
            discovery_threshold_hours=24,
            ttl_days=30,
            extra={"instance_id": "default"},
        ),
    )

    seed_result = runner.invoke(
        app,
        [
            "integration",
            "seed-id",
            "--program",
            "demo",
            "--intent-id",
            intent.intent_id,
            "--ref-id",
            "series-123",
            "--pm-alias",
            "pm@test",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert seed_result.exit_code == 0, seed_result.stdout
    seeded_candidate = candidate_store.get_candidate_by_ref(ref_id="series-123", ref_kind=SourceRefKind.MEETING_SERIES)
    assert seeded_candidate is not None

    reject_result = runner.invoke(
        app,
        [
            "integration",
            "candidate-reject",
            seeded_candidate.candidate_id,
            "--program",
            "demo",
            "--pm-alias",
            "pm@test",
            "--reason",
            "wrong meeting",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert reject_result.exit_code == 0, reject_result.stdout
    assert candidate_store.get_candidate(seeded_candidate.candidate_id).status == SourceCandidateStatus.REJECTED
    assert candidate_store.get_intent(intent.intent_id).status == SourceIntentStatus.DECLARED
    registrations = ChannelRegistryStore(programs_root / "demo" / "runtime" / "channel_registry.sqlite3", "demo").all_registrations("teams")
    assert len(registrations) == 1
    assert registrations[0].status == RegistrationStatus.SUPPRESSED


def test_integration_candidate_reassign_moves_pending_candidate_to_target_workstream(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "demo").mkdir(parents=True)
    candidate_store = SourceCandidateStore(programs_root / "demo" / "runtime" / "channel_registry.sqlite3", "demo")
    as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    candidate_store.bootstrap_intents(
        workstreams=(
            Workstream(
                id="demo.acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Review"),),
                ),
            ),
            Workstream(
                id="demo.ops",
                name="Ops",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Review"),),
                ),
            ),
        ),
        registry_artifacts=(),
        as_of=as_of,
    )
    old_intent = candidate_store.list_intents(workstream_id="demo.acme")[0]
    new_intent = candidate_store.list_intents(workstream_id="demo.ops")[0]
    candidate = SourceCandidate(
        candidate_id=build_source_candidate_id(
            program_id="demo",
            channel="teams",
            provider_instance_id="default",
            ref_kind=SourceRefKind.MEETING_SERIES,
            ref_id="series-123",
        ),
        program_id="demo",
        channel="teams",
        provider_instance_id="default",
        ref_id="series-123",
        ref_kind=SourceRefKind.MEETING_SERIES,
        display_name="Acme Weekly Review",
        confidence=0.93,
        source_provider="graph_calendar",
        status=SourceCandidateStatus.PENDING,
        evidence_json=candidate_evidence_json({"matched_terms": ["Acme Weekly Review"]}),
        first_discovered_at=as_of,
        last_seen_at=as_of,
    )
    candidate_store.upsert_candidate(candidate, pii_prescrubbed=True)
    candidate_store.link_candidate_to_intent(candidate.candidate_id, old_intent.intent_id, 0.93)
    monkeypatch.setattr(integration, "_bootstrap_discovery_state", lambda *args, **kwargs: None)

    result = runner.invoke(
        app,
        [
            "integration",
            "candidate-reassign",
            candidate.candidate_id,
            "--program",
            "demo",
            "--workstream",
            "demo.ops",
            "--pm-alias",
            "pm@test",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert candidate_store.get_intent(old_intent.intent_id).status == SourceIntentStatus.DECLARED
    assert candidate_store.get_intent(new_intent.intent_id).status == SourceIntentStatus.CANDIDATE_FOUND
    assert candidate_store.list_candidates_for_intent(old_intent.intent_id) == ()
    assert candidate_store.list_candidates_for_intent(new_intent.intent_id)[0].candidate_id == candidate.candidate_id


def test_integration_intent_suppress_and_restore_toggle_resolved_registration(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "demo").mkdir(parents=True)
    candidate_store = SourceCandidateStore(programs_root / "demo" / "runtime" / "channel_registry.sqlite3", "demo")
    as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    candidate_store.bootstrap_intents(
        workstreams=(
            Workstream(
                id="demo.acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Review"),),
                ),
            ),
        ),
        registry_artifacts=(),
        as_of=as_of,
    )
    intent = candidate_store.list_intents(workstream_id="demo.acme")[0]
    monkeypatch.setattr(integration, "_bootstrap_discovery_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(integration, "_load_program", lambda program_id, root: Program(schema_version="3.0", id="demo", name="Demo"))
    monkeypatch.setattr(
        integration_discovery,
        "resolve_channel_config",
        lambda program, channel, programs_root: ChannelConfig(
            channel=channel,
            enabled=True,
            discovery_threshold_hours=24,
            ttl_days=30,
            extra={"instance_id": "default"},
        ),
    )

    seed_result = runner.invoke(
        app,
        [
            "integration",
            "seed-id",
            "--program",
            "demo",
            "--intent-id",
            intent.intent_id,
            "--ref-id",
            "series-123",
            "--pm-alias",
            "pm@test",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert seed_result.exit_code == 0, seed_result.stdout

    suppress_result = runner.invoke(
        app,
        [
            "integration",
            "intent-suppress",
            "--program",
            "demo",
            "--workstream",
            "demo.acme",
            "--kind",
            "meeting_series",
            "--name",
            "Acme Weekly Review",
            "--pm-alias",
            "pm@test",
            "--reason",
            "meeting paused",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert suppress_result.exit_code == 0, suppress_result.stdout
    assert candidate_store.get_intent(intent.intent_id).status == SourceIntentStatus.SUPPRESSED
    suppressed_registration = ChannelRegistryStore(programs_root / "demo" / "runtime" / "channel_registry.sqlite3", "demo").all_registrations("teams")[0]
    assert suppressed_registration.status == RegistrationStatus.SUPPRESSED

    reopen_result = runner.invoke(
        app,
        [
            "integration",
            "intent-clear-suppression",
            "--program",
            "demo",
            "--workstream",
            "demo.acme",
            "--kind",
            "meeting_series",
            "--name",
            "Acme Weekly Review",
            "--pm-alias",
            "pm@test",
            "--programs-root",
            str(programs_root),
        ],
    )
    assert reopen_result.exit_code == 0, reopen_result.stdout
    assert candidate_store.get_intent(intent.intent_id).status == SourceIntentStatus.RESOLVED
    active_registration = ChannelRegistryStore(programs_root / "demo" / "runtime" / "channel_registry.sqlite3", "demo").all_registrations("teams")[0]
    assert active_registration.status == RegistrationStatus.ACTIVE
