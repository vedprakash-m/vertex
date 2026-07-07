from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from typer.testing import CliRunner
import yaml

from cli import app
from src.commands.registry import RegistryIdCandidate
from src.core.gather_state_store import load_gather_state
from src.m365.teams_reader import TeamsMessagePage, TeamsMessageRecord
from src.core.m365_registry_store import M365RegistryArtifact, ensure_m365_registry_bootstrap, is_current_m365_registry_promotion_candidate, load_m365_registry, read_m365_routing_feedback_events, upsert_m365_registry_artifacts
from src.core.models_v2 import TeamsChat, TeamsMeetingSeries, Workstream, WorkstreamSignalSources
from src.m365.agency_bridge import AgencyCapabilities


runner = CliRunner()


def test_registry_list_supports_human_json_and_csv(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id="meeting-1"),),
                    teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id="thread-1"),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    human = runner.invoke(app, ["registry", "list", "--program", "acme"])
    json_result = runner.invoke(app, ["registry", "list", "--program", "acme", "--format", "json"])
    csv_result = runner.invoke(app, ["registry", "list", "--program", "acme", "--format", "csv"])

    assert human.exit_code == 0
    assert "meet:acme-acme-weekly-ops-review" in human.stdout
    assert "chan:acme-acme-eng-core-chat" in human.stdout

    assert json_result.exit_code == 0
    assert '"artifact_id": "meet:acme-acme-weekly-ops-review"' in json_result.stdout
    assert '"workstream_id": "acme"' in json_result.stdout

    assert csv_result.exit_code == 0
    assert "artifact_id,artifact_type,display_name,workstream_id,confidence,confidence_source,pm_confirmed,promoted,topics" in csv_result.stdout
    assert "meet:acme-acme-weekly-ops-review,meeting_series,Acme Weekly Ops Review,acme,1.0,pm_confirmed,True,True,SCHIE" in csv_result.stdout


def test_registry_confirm_reject_and_reassign_mutate_registry(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id="thread-1"),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    confirm_result = runner.invoke(
        app,
        [
            "registry",
            "confirm",
            "chan:acme-acme-eng-core-chat",
            "--program",
            "acme",
            "--pm-alias",
            "operator",
            "--topics",
            "pilot-ready,eng-core",
            "--reason",
            "High-signal channel.",
        ],
    )
    reassign_result = runner.invoke(
        app,
        [
            "registry",
            "reassign",
            "chan:acme-acme-eng-core-chat",
            "--program",
            "acme",
            "--pm-alias",
            "operator",
            "--workstream-id",
            "dd_on_pf",
            "--reason",
            "Use for DD pilot status.",
        ],
    )
    reject_result = runner.invoke(
        app,
        [
            "registry",
            "reject",
            "chan:acme-acme-eng-core-chat",
            "--program",
            "acme",
            "--pm-alias",
            "operator",
            "--reason",
            "No longer relevant.",
        ],
    )

    registry = load_m365_registry("acme", programs_root)
    artifact = next(artifact for artifact in registry.artifacts if artifact.artifact_id == "chan:acme-acme-eng-core-chat")
    events = read_m365_routing_feedback_events("acme", programs_root)

    assert confirm_result.exit_code == 0
    assert reassign_result.exit_code == 0
    assert reject_result.exit_code == 0
    assert artifact.inferred_workstream == "dd_on_pf"
    assert artifact.confidence == 0.05
    assert artifact.confidence_source == "pm_rejected"
    assert artifact.pm_confirmed is False
    assert len(events) == 3
    assert [event.action for event in events] == ["confirm", "reassign", "reject"]
    assert events[1].prior_workstream_id == "acme"


def test_registry_set_id_updates_registry_and_appends_feedback(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id=None),),
                    teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    series_result = runner.invoke(
        app,
        [
            "registry",
            "set-id",
            "meet:acme-acme-weekly-ops-review",
            "--program",
            "acme",
            "--pm-alias",
            "operator",
            "--series-id",
            "series-123",
        ],
    )
    thread_result = runner.invoke(
        app,
        [
            "registry",
            "set-id",
            "chan:acme-acme-eng-core-chat",
            "--program",
            "acme",
            "--pm-alias",
            "operator",
            "--thread-id",
            "thread-123",
        ],
    )

    registry = load_m365_registry("acme", programs_root)
    series_artifact = next(artifact for artifact in registry.artifacts if artifact.artifact_id == "meet:acme-acme-weekly-ops-review")
    chat_artifact = next(artifact for artifact in registry.artifacts if artifact.artifact_id == "chan:acme-acme-eng-core-chat")
    events = read_m365_routing_feedback_events("acme", programs_root)

    assert series_result.exit_code == 0
    assert thread_result.exit_code == 0
    assert series_artifact.series_id == "series-123"
    assert chat_artifact.thread_id == "thread-123"
    assert [event.action for event in events] == ["set_series_id", "set_thread_id"]


def test_registry_set_id_accepts_teams_urls_and_normalizes_ids(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id=None),),
                    teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    series_result = runner.invoke(
        app,
        [
            "registry",
            "set-id",
            "meet:acme-acme-weekly-ops-review",
            "--program",
            "acme",
            "--pm-alias",
            "operator",
            "--series-id",
            "https://teams.microsoft.com/l/meeting/details?eventId=AAMkExampleEventId%3d%3d",
        ],
    )
    thread_result = runner.invoke(
        app,
        [
            "registry",
            "set-id",
            "chan:acme-acme-eng-core-chat",
            "--program",
            "acme",
            "--pm-alias",
            "operator",
            "--thread-id",
            "https://teams.microsoft.com/l/message/19:thread-id@thread.v2/1776716474489?context=%7B%22contextType%22:%22chat%22%7D",
        ],
    )

    registry = load_m365_registry("acme", programs_root)
    series_artifact = next(artifact for artifact in registry.artifacts if artifact.artifact_id == "meet:acme-acme-weekly-ops-review")
    chat_artifact = next(artifact for artifact in registry.artifacts if artifact.artifact_id == "chan:acme-acme-eng-core-chat")

    assert series_result.exit_code == 0
    assert thread_result.exit_code == 0
    assert series_artifact.series_id == "AAMkExampleEventId=="
    assert chat_artifact.thread_id == "19:thread-id@thread.v2"


def test_registry_set_id_normalizes_short_meeting_link_and_chat_path(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Ramp Standup", series_id=None),),
                    teams_chats=(TeamsChat(display_name="Acme Leads Sync", thread_id=None),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    series_result = runner.invoke(
        app,
        [
            "registry",
            "set-id",
            "meet:acme-acme-ramp-standup",
            "--program",
            "acme",
            "--pm-alias",
            "operator",
            "--series-id",
            "https://teams.microsoft.com/meet/258356881302011?p=LOPGasWbahdOPtbWK9",
        ],
    )
    thread_result = runner.invoke(
        app,
        [
            "registry",
            "set-id",
            "chan:acme-acme-leads-sync",
            "--program",
            "acme",
            "--pm-alias",
            "operator",
            "--thread-id",
            "https://teams.microsoft.com/l/chat/19:8c5eec9e0e4f4daa8b5e8c32fec26b7a@thread.v2/conversations?context=%7B%22contextType%22%3A%22chat%22%7D",
        ],
    )

    registry = load_m365_registry("acme", programs_root)
    series_artifact = next(artifact for artifact in registry.artifacts if artifact.artifact_id == "meet:acme-acme-ramp-standup")
    chat_artifact = next(artifact for artifact in registry.artifacts if artifact.artifact_id == "chan:acme-acme-leads-sync")

    assert series_result.exit_code == 0
    assert thread_result.exit_code == 0
    assert series_artifact.series_id == "258356881302011"
    assert chat_artifact.thread_id == "19:8c5eec9e0e4f4daa8b5e8c32fec26b7a@thread.v2"


def test_registry_set_id_reassigns_placeholder_uil_registration(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    _make_uil_registration(
        program_dir,
        "acme",
        channel="teams",
        ref_id="chan:acme-acme-eng-core-chat",
        ref_kind="teams_channel",
        workstream_id="acme",
        ref_title="Acme Eng Core Chat",
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(
        app,
        [
            "registry",
            "set-id",
            "chan:acme-acme-eng-core-chat",
            "--program",
            "acme",
            "--pm-alias",
            "operator",
            "--thread-id",
            "thread-123",
        ],
    )

    from src.core.channel_registry_store import ChannelRegistryStore

    store = ChannelRegistryStore(program_dir / "channel_registry.sqlite3", "acme", ensure_schema=False)
    registrations = {r.ref_id: r for r in store.all_registrations("teams")}
    events = store.load_feedback_events("teams", "chan:acme-acme-eng-core-chat", "teams_channel")

    assert result.exit_code == 0
    assert "chan:acme-acme-eng-core-chat" not in registrations
    assert "thread-123" in registrations
    assert registrations["thread-123"].ref_kind == "teams_channel"
    assert [event.action for event in events] == ["set_ref_id"]


def test_registry_discover_ids_reports_candidates(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    captured: dict[str, tuple[str, ...]] = {}
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                pm_owner="operator@example.com",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id=None),),
                    teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.registry.project_workstreams",
        lambda snapshot: (
            Workstream(
                id="acme",
                name="Acme",
                pm_owner="operator@example.com",
                signal_sources=WorkstreamSignalSources(),
            ),
        ),
    )
    def _discover_meeting(display_name, *, limit, topics=(), owner_aliases=(), bridge=None, match_aliases=()):
        del limit, topics, bridge
        captured["owner_aliases"] = owner_aliases
        return (
            RegistryIdCandidate("meeting-123", display_name, "https://teams.microsoft.com/l/meeting/details?eventId=meeting-123", True),
        )
    monkeypatch.setattr(
        "src.commands.registry._discover_meeting_id_candidates",
        _discover_meeting,
    )
    monkeypatch.setattr(
        "src.commands.registry._discover_thread_id_candidates",
        lambda display_name, *, limit, topics=(), bridge=None, match_aliases=(): (
            RegistryIdCandidate("19:thread-id@thread.v2", display_name, "https://teams.microsoft.com/l/message/19:thread-id@thread.v2/1", True),
        ),
    )

    result = runner.invoke(app, ["registry", "discover-ids", "--program", "acme"])

    assert result.exit_code == 0
    assert "meet:acme-acme-weekly-ops-review" in result.stdout
    assert "meeting-123" in result.stdout
    assert "chan:acme-acme-eng-core-chat" in result.stdout
    assert "19:thread-id@thread.v2" in result.stdout
    assert captured["owner_aliases"] == ("operator@example.com",)


def test_registry_discover_ids_applies_unique_exact_matches(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id=None),),
                    teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.registry._discover_meeting_id_candidates",
        lambda display_name, *, limit, topics=(), owner_aliases=(), bridge=None, match_aliases=(): (
            RegistryIdCandidate("meeting-123", display_name, "https://teams.microsoft.com/l/meeting/details?eventId=meeting-123", True),
        ),
    )
    monkeypatch.setattr(
        "src.commands.registry._discover_thread_id_candidates",
        lambda display_name, *, limit, topics=(), bridge=None, match_aliases=(): (
            RegistryIdCandidate("19:thread-id@thread.v2", display_name, "https://teams.microsoft.com/l/message/19:thread-id@thread.v2/1", True),
        ),
    )

    result = runner.invoke(app, ["registry", "discover-ids", "--program", "acme", "--apply", "--pm-alias", "operator"])

    registry = load_m365_registry("acme", programs_root)
    series_artifact = next(artifact for artifact in registry.artifacts if artifact.artifact_id == "meet:acme-acme-weekly-ops-review")
    chat_artifact = next(artifact for artifact in registry.artifacts if artifact.artifact_id == "chan:acme-acme-eng-core-chat")

    assert result.exit_code == 0
    assert "Applied 2 discovered id(s)." in result.stdout
    assert series_artifact.series_id == "meeting-123"
    assert chat_artifact.thread_id == "19:thread-id@thread.v2"


def test_registry_discover_ids_supports_json_output(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id=None),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.registry._discover_meeting_id_candidates",
        lambda display_name, *, limit, topics=(), owner_aliases=(), bridge=None, match_aliases=(): (
            RegistryIdCandidate("meeting-123", display_name, "https://teams.microsoft.com/l/meeting/details?eventId=meeting-123", True),
        ),
    )

    result = runner.invoke(app, ["registry", "discover-ids", "--program", "acme", "--format", "json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["program"] == "acme"
    assert payload["apply"] is False
    assert payload["applied_count"] == 0
    assert payload["results"] == [
        {
            "artifact_id": "meet:acme-acme-weekly-ops-review",
            "artifact_type": "meeting_series",
            "candidates": [
                {
                    "discovered_id": "meeting-123",
                    "exact_match": True,
                    "label": "Acme Weekly Ops Review",
                    "source_url": "https://teams.microsoft.com/l/meeting/details?eventId=meeting-123",
                }
            ],
            "display_name": "Acme Weekly Ops Review",
            "reason": None,
            "status": "candidates_found",
        }
    ]


def test_registry_discover_ids_reports_missing_meeting_tools(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id=None),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.registry._discover_meeting_id_candidates", lambda display_name, *, limit, topics=(), owner_aliases=(), bridge=None, match_aliases=(): ())

    class _BridgeWithPartialWorkIQ:
        def probe(self) -> AgencyCapabilities:
            return AgencyCapabilities(
                available=True,
                has_workiq=True,
                server_tools={"workiq": ("search_emails",)},
            )

    monkeypatch.setattr("src.commands.registry.AgencyBridge", _BridgeWithPartialWorkIQ)

    result = runner.invoke(app, ["registry", "discover-ids", "--program", "acme"])

    assert result.exit_code == 0
    assert "no candidates found (WorkIQ missing get_meetings)" in result.stdout


def test_registry_discover_ids_json_reports_no_candidates_reason(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id=None),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.registry._discover_meeting_id_candidates", lambda display_name, *, limit, topics=(), owner_aliases=(), bridge=None, match_aliases=(): ())

    class _BridgeWithPartialWorkIQ:
        def probe(self) -> AgencyCapabilities:
            return AgencyCapabilities(
                available=True,
                has_workiq=True,
                server_tools={"workiq": ("search_emails",)},
            )

    monkeypatch.setattr("src.commands.registry.AgencyBridge", _BridgeWithPartialWorkIQ)

    result = runner.invoke(app, ["registry", "discover-ids", "--program", "acme", "--format", "json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["results"] == [
        {
            "artifact_id": "meet:acme-acme-weekly-ops-review",
            "artifact_type": "meeting_series",
            "candidates": [],
            "display_name": "Acme Weekly Ops Review",
            "reason": "WorkIQ missing get_meetings",
            "status": "no_candidates",
        }
    ]


def test_registry_discover_ids_reports_missing_teams_tool(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.registry._discover_thread_id_candidates", lambda display_name, *, limit, topics=(), bridge=None, match_aliases=(): ())

    class _BridgeWithMeetingOnlyWorkIQ:
        def probe(self) -> AgencyCapabilities:
            return AgencyCapabilities(
                available=True,
                has_workiq=True,
                server_tools={"workiq": ("search_emails", "get_meetings")},
            )

    monkeypatch.setattr("src.commands.registry.AgencyBridge", _BridgeWithMeetingOnlyWorkIQ)

    result = runner.invoke(app, ["registry", "discover-ids", "--program", "acme"])

    assert result.exit_code == 0
    assert "no candidates found (WorkIQ ask_work_iq required for Teams discovery)" in result.stdout


def test_registry_discover_ids_treats_local_workiq_cli_as_available(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id=None),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.registry._discover_meeting_id_candidates", lambda display_name, *, limit, topics=(), owner_aliases=(), bridge=None, match_aliases=(): ())

    class _BridgeWithLocalWorkIQCli:
        def probe(self) -> AgencyCapabilities:
            return AgencyCapabilities(
                available=True,
                has_workiq=False,
                has_workiq_cli=True,
                server_tools={"workiq": ()},
            )

    monkeypatch.setattr("src.commands.registry.AgencyBridge", _BridgeWithLocalWorkIQCli)

    result = runner.invoke(app, ["registry", "discover-ids", "--program", "acme"])

    assert result.exit_code == 0
    assert "no candidates found" in result.stdout
    assert "WorkIQ MCP server unavailable" not in result.stdout


def test_registry_discover_ids_records_completed_discovery_pass(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id=None),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.registry._discover_meeting_id_candidates", lambda display_name, *, limit, topics=(), owner_aliases=(), bridge=None, match_aliases=(): ())

    class _BridgeWithLocalWorkIQCli:
        def __init__(self) -> None:
            self._last_error: str | None = None

        def probe(self) -> AgencyCapabilities:
            return AgencyCapabilities(
                available=True,
                has_workiq=False,
                has_workiq_cli=True,
                server_tools={"workiq": ()},
            )

        def last_mcp_error(self) -> str | None:
            return self._last_error

    monkeypatch.setattr("src.commands.registry.AgencyBridge", _BridgeWithLocalWorkIQCli)

    result = runner.invoke(app, ["registry", "discover-ids", "--program", "acme", "--format", "json"])

    assert result.exit_code == 0
    state = load_gather_state("acme", programs_root=programs_root)
    assert state is not None
    assert state.m365_discovery["active"] is True
    assert state.m365_discovery["first_discovery_completed_at"] is not None
    assert state.m365_discovery["query_plan_count"] == 1
    assert state.m365_discovery["discovery_last_error"] is None


def test_registry_discover_ids_reports_runtime_workiq_failure_detail(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    class _BridgeWithRuntimeFailure:
        def __init__(self) -> None:
            self._last_error: str | None = None

        def probe(self) -> AgencyCapabilities:
            return AgencyCapabilities(
                available=True,
                has_workiq=True,
                server_tools={"workiq": ("ask_work_iq", "search_emails")},
            )

        def ask_workiq(self, question: str, **kwargs) -> dict | None:
            self._last_error = "mcp request timed out"
            return None

        def last_mcp_error(self) -> str | None:
            return self._last_error

    monkeypatch.setattr("src.commands.registry.AgencyBridge", _BridgeWithRuntimeFailure)

    result = runner.invoke(app, ["registry", "discover-ids", "--program", "acme"])

    assert result.exit_code == 0
    assert "no candidates found (WorkIQ Teams discovery failed: mcp request timed out)" in result.stdout


def test_discover_meeting_id_candidates_uses_calendar_meeting_id_and_join_url(monkeypatch) -> None:
    class _FakeDiscovery:
        def discover_candidates(self, display_name: str, *, limit: int, topics=(), owner_aliases=()):
            assert display_name == "Acme Weekly Ops Review"
            assert limit == 5
            assert topics == ()
            assert owner_aliases == ()
            return (
                RegistryIdCandidate(
                    discovered_id="series-master-123",
                    label="Acme Weekly Ops Review",
                    source_url="https://teams.microsoft.com/l/meeting/details?meetingId=meeting-123",
                    exact_match=True,
                ),
            )

    monkeypatch.setattr("src.m365.registry_id_discovery.WorkIQCalendarDiscovery.from_bridge", lambda bridge: _FakeDiscovery())

    candidates = __import__("src.commands.registry", fromlist=["_discover_meeting_id_candidates"])._discover_meeting_id_candidates(
        "Acme Weekly Ops Review",
        limit=5,
    )

    assert candidates == (
        RegistryIdCandidate(
            discovered_id="series-master-123",
            label="Acme Weekly Ops Review",
            source_url="https://teams.microsoft.com/l/meeting/details?meetingId=meeting-123",
            exact_match=True,
        ),
    )


def test_discover_thread_id_candidates_accepts_meeting_id_fallback(monkeypatch) -> None:
    class _FakeBridge:
        def ask_workiq(self, question: str, **kwargs) -> dict | None:
            return {
                "messages": [
                    {
                        "meetingId": "19:thread-id@thread.v2",
                        "channel": "Acme Eng Core Chat",
                    }
                ]
            }

    candidates = __import__("src.commands.registry", fromlist=["_discover_thread_id_candidates"])._discover_thread_id_candidates(
        "Acme Eng Core Chat",
        limit=5,
        bridge=_FakeBridge(),
    )

    assert candidates == (
        RegistryIdCandidate(
            discovered_id="19:thread-id@thread.v2",
            label="Acme Eng Core Chat",
            source_url=None,
            exact_match=True,
            match_score=1.0,
        ),
    )


def test_discover_meeting_id_candidates_uses_topics_and_alias_normalization(monkeypatch) -> None:
    class _FakeDiscovery:
        def discover_candidates(self, display_name: str, *, limit: int, topics=(), owner_aliases=()):
            assert display_name == "Contoso Weekly Review"
            assert limit == 5
            assert topics == ("Direct Drive Northwind", "firmware sign-off")
            assert owner_aliases == ()
            return (
                RegistryIdCandidate(
                    discovered_id="series-contoso-1",
                    label="Direct Drive Northwind Weekly Review",
                    source_url="https://teams.microsoft.com/l/meeting/details?meetingId=series-contoso-1",
                    exact_match=True,
                ),
            )

    monkeypatch.setattr("src.m365.registry_id_discovery.WorkIQCalendarDiscovery.from_bridge", lambda bridge: _FakeDiscovery())

    candidates = __import__("src.commands.registry", fromlist=["_discover_meeting_id_candidates"])._discover_meeting_id_candidates(
        "Contoso Weekly Review",
        limit=5,
        topics=("Direct Drive Northwind", "firmware sign-off"),
    )

    assert candidates == (
        RegistryIdCandidate(
            discovered_id="series-contoso-1",
            label="Direct Drive Northwind Weekly Review",
            source_url="https://teams.microsoft.com/l/meeting/details?meetingId=series-contoso-1",
            exact_match=True,
        ),
    )


def test_discover_email_thread_candidates_uses_mail_thread_id_and_owner_aliases(monkeypatch) -> None:
    class _FakeDiscovery:
        def discover_candidates(self, display_name: str, *, limit: int, topics=(), owner_aliases=()):
            assert display_name == "SCHIE Mail Thread"
            assert limit == 5
            assert topics == ("SCHIE",)
            assert owner_aliases == ("operator@example.com",)
            return (
                RegistryIdCandidate(
                    discovered_id="mail-thread-123",
                    label="SCHIE Mail Thread",
                    source_url="https://outlook.office.com/mail/mail-1",
                    exact_match=True,
                ),
            )

    monkeypatch.setattr("src.m365.registry_id_discovery.WorkIQMailDiscovery.from_bridge", lambda bridge: _FakeDiscovery())

    candidates = __import__("src.commands.registry", fromlist=["_discover_email_thread_candidates"])._discover_email_thread_candidates(
        "SCHIE Mail Thread",
        limit=5,
        topics=("SCHIE",),
        owner_aliases=("operator@example.com",),
    )

    assert candidates == (
        RegistryIdCandidate(
            discovered_id="mail-thread-123",
            label="SCHIE Mail Thread",
            source_url="https://outlook.office.com/mail/mail-1",
            exact_match=True,
        ),
    )


def test_discover_thread_id_candidates_uses_topic_queries_for_channel_matches(monkeypatch) -> None:
    observed_queries: list[str] = []

    class _FakeTeamsReader:
        def __init__(self, bridge) -> None:
            self.bridge = bridge

        def search_messages(
            self,
            *,
            channel: str,
            query: str,
            since: str | None = None,
            limit: int = 25,
            cursor: str | None = None,
            timeout_seconds: int | None = None,
            allow_cli_fallback: bool = True,
        ) -> TeamsMessagePage:
            observed_queries.append(query)
            if query in {"Acme Eng Core Chat", "acme eng core"}:
                return TeamsMessagePage(records=(), next_cursor=None, source="workiq")
            return TeamsMessagePage(
                records=(
                    TeamsMessageRecord(
                        source_id="message-1",
                        channel="Acme Eng Core Chat",
                        sender="owner@example.com",
                        sent_at="2026-05-22T12:00:00Z",
                        web_url="https://teams.microsoft.com/l/message/19:thread-id@thread.v2/1",
                        preview="SCHIE burn-in update",
                        conversation_id="19:thread-id@thread.v2",
                    ),
                ),
                next_cursor=None,
                source="workiq",
            )

    monkeypatch.setattr("src.m365.registry_id_discovery.TeamsReader", _FakeTeamsReader)

    candidates = __import__("src.commands.registry", fromlist=["_discover_thread_id_candidates"])._discover_thread_id_candidates(
        "Acme Eng Core Chat",
        limit=5,
        topics=("SCHIE gaps", "deployment velocity Acme"),
    )

    assert "SCHIE gaps" in observed_queries
    assert candidates == (
        RegistryIdCandidate(
            discovered_id="19:thread-id@thread.v2",
            label="Acme Eng Core Chat",
            source_url="https://teams.microsoft.com/l/message/19:thread-id@thread.v2/1",
            exact_match=True,
            match_score=1.0,
        ),
    )


def test_registry_discover_ids_routes_email_threads_to_email_discovery(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="thread:auto:email-1",
                artifact_type="email_thread",
                display_name="SCHIE Mail Thread",
                thread_id=None,
                inferred_workstream="acme",
                confidence=0.66,
                confidence_source="heuristic",
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                topics=("SCHIE",),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.registry.project_workstreams",
        lambda snapshot: (
            Workstream(
                id="acme",
                name="Acme",
                pm_owner="operator@example.com",
                signal_sources=WorkstreamSignalSources(),
            ),
        ),
    )
    monkeypatch.setattr(
        "src.commands.registry._discover_email_thread_candidates",
        lambda display_name, *, limit, topics=(), owner_aliases=(), bridge=None, match_aliases=(): (
            RegistryIdCandidate(
                discovered_id="mail-thread-123",
                label=display_name,
                source_url="https://outlook.office.com/mail/mail-1",
                exact_match=True,
            ),
        ),
    )

    result = runner.invoke(app, ["registry", "discover-ids", "--program", "acme"])

    assert result.exit_code == 0
    assert "thread:auto:email-1 | SCHIE Mail Thread" in result.stdout
    assert "mail-thread-123" in result.stdout


def test_registry_discover_ids_apply_reassigns_email_uil_registration(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="thread:auto:email-1",
                artifact_type="email_thread",
                display_name="SCHIE Mail Thread",
                thread_id=None,
                inferred_workstream="acme",
                confidence=0.66,
                confidence_source="heuristic",
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                topics=("SCHIE",),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    _make_uil_registration(
        program_dir,
        "acme",
        channel="teams",
        ref_id="thread:auto:email-1",
        ref_kind="email_thread",
        workstream_id="acme",
        ref_title="SCHIE Mail Thread",
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.registry._discover_email_thread_candidates",
        lambda display_name, *, limit, topics=(), owner_aliases=(), bridge=None, match_aliases=(): (
            RegistryIdCandidate(
                discovered_id="mail-thread-123",
                label=display_name,
                source_url="https://outlook.office.com/mail/mail-1",
                exact_match=True,
            ),
        ),
    )

    result = runner.invoke(app, ["registry", "discover-ids", "--program", "acme", "--apply", "--pm-alias", "operator"])

    from src.core.channel_registry_store import ChannelRegistryStore

    store = ChannelRegistryStore(program_dir / "channel_registry.sqlite3", "acme", ensure_schema=False)
    registrations = {r.ref_id: r for r in store.all_registrations("teams")}
    events = store.load_feedback_events("teams", "thread:auto:email-1", "email_thread")

    assert result.exit_code == 0
    assert "Applied 1 discovered id(s)." in result.stdout
    assert "thread:auto:email-1" not in registrations
    assert "mail-thread-123" in registrations
    assert registrations["mail-thread-123"].ref_kind == "email_thread"
    assert [event.action for event in events] == ["set_ref_id"]


def test_registry_discover_ids_does_not_apply_non_exact_candidates(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id=None),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.registry._discover_meeting_id_candidates",
        lambda display_name, *, limit, topics=(), owner_aliases=(), bridge=None, match_aliases=(): (
            RegistryIdCandidate(
                "meeting-heuristic",
                "Acme Ops Review",
                "https://teams.microsoft.com/l/meeting/details?meetingId=meeting-heuristic",
                False,
            ),
        ),
    )

    result = runner.invoke(app, ["registry", "discover-ids", "--program", "acme", "--apply", "--pm-alias", "operator"])

    registry = load_m365_registry("acme", programs_root)
    series_artifact = next(artifact for artifact in registry.artifacts if artifact.artifact_id == "meet:acme-acme-weekly-ops-review")

    assert result.exit_code == 0
    assert "Applied 0 discovered id(s)." in result.stdout
    assert "meet:acme-acme-weekly-ops-review | Acme Weekly Ops Review" in result.stdout
    assert series_artifact.series_id is None


def test_registry_promote_updates_workstreams_yaml_and_marks_registry(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    workstreams_path = programs_root / "acme" / "workstreams.yaml"
    workstreams_path.parent.mkdir(parents=True, exist_ok=True)
    workstreams_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "workstreams": [
                    {
                        "id": "acme",
                        "name": "Acme",
                        "signal_sources": {"workiq_keywords": ["SCHIE"]},
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="chan:acme-acme-eng-core-chat",
                artifact_type="teams_channel",
                display_name="Acme Eng Core Chat",
                thread_id="thread-123",
                inferred_workstream="acme",
                confidence=0.95,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                topics=("SCHIE",),
                signal_yield_last_3=(1, 1, 1),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["registry", "promote", "chan:acme-acme-eng-core-chat", "--program", "acme"])

    registry = load_m365_registry("acme", programs_root)
    artifact = next(item for item in registry.artifacts if item.artifact_id == "chan:acme-acme-eng-core-chat")
    workstreams_document = yaml.safe_load(workstreams_path.read_text(encoding="utf-8"))
    teams_chats = workstreams_document["workstreams"][0]["signal_sources"]["teams_chats"]

    assert result.exit_code == 0
    assert "Promoted chan:acme-acme-eng-core-chat into workstreams.yaml for acme." in result.stdout
    assert artifact.promoted_to_workstreams_yaml is True
    assert teams_chats == [{"display_name": "Acme Eng Core Chat", "thread_id": "thread-123"}]


def test_registry_promote_supports_email_thread_sources(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    workstreams_path = programs_root / "acme" / "workstreams.yaml"
    workstreams_path.parent.mkdir(parents=True, exist_ok=True)
    workstreams_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "workstreams": [
                    {
                        "id": "acme",
                        "name": "Acme",
                        "signal_sources": {"workiq_keywords": ["SCHIE"]},
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="thread:auto:abc12345",
                artifact_type="email_thread",
                display_name="SCHIE Mail Thread",
                thread_id="thread-123",
                inferred_workstream="acme",
                confidence=0.95,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                topics=("SCHIE",),
                signal_yield_last_3=(1, 1, 1),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["registry", "promote", "thread:auto:abc12345", "--program", "acme"])

    registry = load_m365_registry("acme", programs_root)
    artifact = next(item for item in registry.artifacts if item.artifact_id == "thread:auto:abc12345")
    workstreams_document = yaml.safe_load(workstreams_path.read_text(encoding="utf-8"))
    email_threads = workstreams_document["workstreams"][0]["signal_sources"]["email_threads"]

    assert result.exit_code == 0
    assert "Promoted thread:auto:abc12345 into workstreams.yaml for acme." in result.stdout
    assert artifact.promoted_to_workstreams_yaml is True
    assert email_threads == [{"display_name": "SCHIE Mail Thread", "thread_id": "thread-123"}]


def test_registry_promote_confirms_high_confidence_candidates(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    workstreams_path = programs_root / "acme" / "workstreams.yaml"
    workstreams_path.parent.mkdir(parents=True, exist_ok=True)
    workstreams_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "workstreams": [
                    {
                        "id": "acme",
                        "name": "Acme",
                        "signal_sources": {"workiq_keywords": ["SCHIE"]},
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="thread:auto:steady001",
                artifact_type="email_thread",
                display_name="Steady High Confidence Thread",
                thread_id="steady-thread-1",
                inferred_workstream="acme",
                confidence=0.9,
                confidence_source="keyword_router",
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                signal_yield_last_3=(1, 1, 1),
                high_confidence_streak=3,
                topics=("SCHIE",),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(
        app,
        [
            "registry",
            "promote",
            "thread:auto:steady001",
            "--program",
            "acme",
            "--pm-alias",
            "operator",
            "--reason",
            "Consistent routing confidence.",
        ],
    )

    registry = load_m365_registry("acme", programs_root)
    artifact = next(item for item in registry.artifacts if item.artifact_id == "thread:auto:steady001")
    events = read_m365_routing_feedback_events("acme", programs_root)
    workstreams_document = yaml.safe_load(workstreams_path.read_text(encoding="utf-8"))
    email_threads = workstreams_document["workstreams"][0]["signal_sources"]["email_threads"]

    assert result.exit_code == 0
    assert artifact.pm_confirmed is True
    assert artifact.promoted_to_workstreams_yaml is True
    assert [event.action for event in events] == ["confirm"]
    assert events[0].pm_alias == "operator"
    assert email_threads == [{"display_name": "Steady High Confidence Thread", "thread_id": "steady-thread-1"}]


def test_registry_promote_rejects_pm_confirmed_artifacts_without_required_signal_yield(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    workstreams_path = programs_root / "acme" / "workstreams.yaml"
    workstreams_path.parent.mkdir(parents=True, exist_ok=True)
    workstreams_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "workstreams": [
                    {
                        "id": "acme",
                        "name": "Acme",
                        "signal_sources": {"workiq_keywords": ["SCHIE"]},
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="chan:acme-low-yield",
                artifact_type="teams_channel",
                display_name="Low Yield Chat",
                thread_id="thread-low-yield",
                inferred_workstream="acme",
                confidence=1.0,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                signal_yield_last_3=(1, 0, 0),
                topics=("SCHIE",),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["registry", "promote", "chan:acme-low-yield", "--program", "acme"])

    registry = load_m365_registry("acme", programs_root)
    artifact = next(item for item in registry.artifacts if item.artifact_id == "chan:acme-low-yield")

    assert result.exit_code != 0
    assert artifact.promoted_to_workstreams_yaml is False


def test_registry_rename_stabilizes_auto_thread_ids_and_preserves_feedback_history(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="thread:auto:abc12345",
                artifact_type="email_thread",
                display_name="Auto discovered thread",
                thread_id="thread-123",
                inferred_workstream="acme",
                confidence=0.95,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                topics=("SCHIE",),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    reject_result = runner.invoke(
        app,
        [
            "registry",
            "reject",
            "thread:auto:abc12345",
            "--program",
            "acme",
            "--pm-alias",
            "operator",
            "--reason",
            "No longer relevant.",
        ],
    )
    rename_result = runner.invoke(
        app,
        [
            "registry",
            "rename",
            "thread:auto:abc12345",
            "--program",
            "acme",
            "--pm-alias",
            "operator",
            "--display-name",
            "SCHIE Mail Thread",
            "--reason",
            "Stabilize the thread identifier.",
        ],
    )

    registry = load_m365_registry("acme", programs_root)
    artifact = next(item for item in registry.artifacts if item.artifact_id == "thread:named:schie-mail-thread")
    events = read_m365_routing_feedback_events("acme", programs_root)

    assert reject_result.exit_code == 0
    assert rename_result.exit_code == 0
    assert artifact.legacy_artifact_ids == ("thread:auto:abc12345",)
    assert artifact.display_name == "SCHIE Mail Thread"
    assert [event.action for event in events] == ["reject", "rename_artifact"]
    assert events[1].pm_alias == "operator"
    assert events[1].new_artifact_id == "thread:named:schie-mail-thread"
    assert is_current_m365_registry_promotion_candidate(
        artifact,
        feedback_events=events,
        as_of=datetime(2026, 5, 22, 12, 30, tzinfo=timezone.utc),
    ) is False


def test_registry_rename_also_writes_uil_feedback_for_email_sources(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="thread:auto:abc12345",
                artifact_type="email_thread",
                display_name="Auto discovered thread",
                thread_id="thread-123",
                inferred_workstream="acme",
                confidence=0.95,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                topics=("SCHIE",),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    _make_uil_registration(
        program_dir,
        "acme",
        channel="email",
        ref_id="thread-123",
        ref_kind="email_thread",
        workstream_id="acme",
        ref_title="Auto discovered thread",
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(
        app,
        [
            "registry",
            "rename",
            "thread:auto:abc12345",
            "--program",
            "acme",
            "--pm-alias",
            "operator",
            "--display-name",
            "SCHIE Mail Thread",
            "--reason",
            "Stabilize the thread identifier.",
        ],
    )

    from src.core.channel_registry_store import ChannelRegistryStore

    store = ChannelRegistryStore(program_dir / "channel_registry.sqlite3", "acme", ensure_schema=False)
    events = store.load_feedback_events("email", "thread-123", "email_thread")

    assert result.exit_code == 0
    assert [event.action for event in events] == ["rename_artifact"]
    assert events[0].new_artifact_id == "thread:named:schie-mail-thread"


# ---------------------------------------------------------------------------
# UIL bridge tests for `vertex registry list`
# ---------------------------------------------------------------------------

def _make_uil_teams_registry(program_dir: Path, program: str) -> None:
    """Populate channel_registry.sqlite3 with two teams registrations for a program."""
    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.integration_types import (
        ChannelRegistration,
        DiscoveredRef,
        DiscoveryCompleteness,
        DiscoveryResult,
        RegistrationBinding,
        RegistrationStatus,
        ScopeStatus,
        ScopeStatusKind,
    )
    registry_path = program_dir / "channel_registry.sqlite3"
    store = ChannelRegistryStore(registry_path, program)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    discovered_refs = (
        DiscoveredRef(
            registration=ChannelRegistration(
                channel="teams",
                program_id=program,
                provider_instance_id="default",
                ref_id="series-abc",
                ref_kind="meeting_series",
                status=RegistrationStatus.ACTIVE,
                first_discovered_at=now,
                last_seen_at=now,
                confidence=0.9,
                confidence_source="workiq",
                pm_confirmed=True,
                promoted=False,
                signal_yield_last_3=(3, 2, 1),
                ref_title="Weekly Sync",
            ),
            bindings=(
                RegistrationBinding(
                    workstream_id="demo.slice",
                    scope_id="default",
                    source_type="m365_migration",
                    confidence=0.9,
                    confidence_source="workiq",
                    pm_confirmed=True,
                ),
            ),
        ),
        DiscoveredRef(
            registration=ChannelRegistration(
                channel="teams",
                program_id=program,
                provider_instance_id="default",
                ref_id="thread-xyz",
                ref_kind="teams_chat",
                status=RegistrationStatus.ACTIVE,
                first_discovered_at=now,
                last_seen_at=now,
                confidence=0.7,
                confidence_source="workiq",
                pm_confirmed=False,
                promoted=False,
                signal_yield_last_3=(1, 0, 0),
                ref_title="Eng Chat",
            ),
            bindings=(
                RegistrationBinding(
                    workstream_id="demo.core",
                    scope_id="default",
                    source_type="m365_migration",
                    confidence=0.7,
                    confidence_source="workiq",
                ),
            ),
        ),
    )
    store.apply_discovery_result(
        DiscoveryResult(
            channel="teams",
            program_id=program,
            discovered_refs=discovered_refs,
            completeness=DiscoveryCompleteness.FULL,
            scope_statuses={
                "default": ScopeStatus(
                    scope_id="default",
                    status=ScopeStatusKind.SUCCESS,
                    completeness=DiscoveryCompleteness.FULL,
                    item_count=2,
                )
            },
            scope_state_updates={},
            errors=(),
            computed_at=now,
        ),
        accept_shrinkage=True,
    )


def _make_uil_registration(
    program_dir: Path,
    program: str,
    *,
    channel: str,
    ref_id: str,
    ref_kind: str,
    workstream_id: str,
    ref_title: str,
) -> None:
    from src.core.channel_registry_store import ChannelRegistryStore
    from src.core.integration_types import (
        ChannelRegistration,
        DiscoveredRef,
        DiscoveryCompleteness,
        DiscoveryResult,
        RegistrationBinding,
        RegistrationStatus,
        ScopeStatus,
        ScopeStatusKind,
    )

    registry_path = program_dir / "channel_registry.sqlite3"
    store = ChannelRegistryStore(registry_path, program)
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    store.apply_discovery_result(
        DiscoveryResult(
            channel=channel,
            program_id=program,
            discovered_refs=(
                DiscoveredRef(
                    registration=ChannelRegistration(
                        channel=channel,
                        program_id=program,
                        provider_instance_id="default",
                        ref_id=ref_id,
                        ref_kind=ref_kind,
                        status=RegistrationStatus.ACTIVE,
                        first_discovered_at=now,
                        last_seen_at=now,
                        confidence=0.8,
                        confidence_source="m365_migration",
                        pm_confirmed=False,
                        promoted=False,
                        signal_yield_last_3=(1, 0, 0),
                        ref_title=ref_title,
                    ),
                    bindings=(
                        RegistrationBinding(
                            workstream_id=workstream_id,
                            scope_id="default",
                            source_type="m365_migration",
                            confidence=0.8,
                            confidence_source="m365_migration",
                        ),
                    ),
                ),
            ),
            completeness=DiscoveryCompleteness.FULL,
            scope_statuses={
                "default": ScopeStatus(
                    scope_id="default",
                    status=ScopeStatusKind.SUCCESS,
                    completeness=DiscoveryCompleteness.FULL,
                    item_count=1,
                )
            },
            scope_state_updates={},
            errors=(),
            computed_at=now,
        ),
        accept_shrinkage=True,
    )


def test_registry_list_auto_uses_uil_when_registry_exists(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text("schema_version: '3.0'\nid: demo\nname: Demo\n", encoding="utf-8")
    _make_uil_teams_registry(program_dir, "demo")
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["registry", "list", "--program", "demo"])

    assert result.exit_code == 0, result.stdout
    assert "series-abc" in result.stdout
    assert "thread-xyz" in result.stdout
    assert "Weekly Sync" in result.stdout


def test_registry_list_uil_source_explicit(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    _make_uil_teams_registry(program_dir, "demo")
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["registry", "list", "--program", "demo", "--source", "uil"])

    assert result.exit_code == 0, result.stdout
    assert "meeting_series" in result.stdout
    assert "demo.slice" in result.stdout


def test_registry_list_yaml_source_explicit_bypasses_uil(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    # Set up BOTH yaml and UIL registry
    _make_uil_teams_registry(program_dir, "demo")
    ensure_m365_registry_bootstrap(
        "demo",
        workstreams=(
            Workstream(
                id="demo",
                name="Demo",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Demo Weekly", series_id="meeting-1"),),
                    teams_chats=(),
                    workiq_keywords=(),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["registry", "list", "--program", "demo", "--source", "yaml"])

    assert result.exit_code == 0, result.stdout
    # YAML artifacts use artifact_id column, not ref_id
    assert "artifact_id" not in result.stdout  # human format doesn't show headers
    # YAML artifact IDs use meet: prefix
    assert "meet:" in result.stdout


def test_registry_list_uil_json_format(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    _make_uil_teams_registry(program_dir, "demo")
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["registry", "list", "--program", "demo", "--source", "uil", "--format", "json"])

    assert result.exit_code == 0, result.stdout
    data = __import__("json").loads(result.stdout)
    assert len(data) == 2
    ref_ids = {row["ref_id"] for row in data}
    assert ref_ids == {"series-abc", "thread-xyz"}
    assert any(row["pm_confirmed"] for row in data)


def test_registry_confirm_also_updates_uil_store_when_present(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    # Set up YAML registry with an artifact that has a series_id (meeting_series)
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly", series_id="series-abc"),),
                    teams_chats=(),
                    workiq_keywords=(),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc),
    )
    # Also set up UIL registry with the same series_id as ref_id
    _make_uil_teams_registry(program_dir, "acme")
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    # Before confirm: verify UIL has pm_confirmed=False for the thread-xyz entry
    from src.core.channel_registry_store import ChannelRegistryStore
    store = ChannelRegistryStore(program_dir / "channel_registry.sqlite3", "acme", ensure_schema=False)
    before_regs = {r.ref_id: r for r in store.all_registrations("teams")}
    assert before_regs["series-abc"].pm_confirmed is True  # set by _make_uil_teams_registry
    assert before_regs["thread-xyz"].pm_confirmed is False

    result = runner.invoke(app, ["registry", "confirm", "meet:acme-acme-weekly", "--program", "acme", "--pm-alias", "operator"])
    assert result.exit_code == 0, result.stdout

    # After confirm: check that the UIL store reflects the confirmation
    # The artifact's series_id="series-abc", so UIL store's series-abc should still be confirmed
    after_regs = {r.ref_id: r for r in store.all_registrations("teams")}
    assert after_regs["series-abc"].pm_confirmed is True  # confirmed via UIL dual-write


def test_registry_reject_also_suppresses_in_uil_store_when_present(monkeypatch, tmp_path: Path) -> None:
    from src.core.integration_types import RegistrationStatus

    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly", series_id="series-abc"),),
                    teams_chats=(),
                    workiq_keywords=(),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc),
    )
    _make_uil_teams_registry(program_dir, "acme")
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    from src.core.channel_registry_store import ChannelRegistryStore
    store = ChannelRegistryStore(program_dir / "channel_registry.sqlite3", "acme", ensure_schema=False)
    before = {r.ref_id: r for r in store.all_registrations("teams")}
    assert before["series-abc"].status == RegistrationStatus.ACTIVE

    result = runner.invoke(app, ["registry", "reject", "meet:acme-acme-weekly", "--program", "acme", "--pm-alias", "operator"])
    assert result.exit_code == 0, result.stdout

    # Status should now be SUPPRESSED for series-abc in UIL store
    after = {r.ref_id: r for r in store.all_registrations("teams")}
    assert after["series-abc"].status == RegistrationStatus.SUPPRESSED


def test_registry_reassign_also_updates_uil_store_when_present(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly", series_id="series-abc"),),
                    teams_chats=(),
                    workiq_keywords=(),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc),
    )
    _make_uil_teams_registry(program_dir, "acme")
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    from src.core.channel_registry_store import ChannelRegistryStore
    store = ChannelRegistryStore(program_dir / "channel_registry.sqlite3", "acme", ensure_schema=False)

    result = runner.invoke(
        app,
        ["registry", "reassign", "meet:acme-acme-weekly", "--workstream-id", "ws-new", "--program", "acme", "--pm-alias", "operator"],
    )
    assert result.exit_code == 0, result.stdout

    # UIL store's series-abc binding should now be attributed to ws-new, not demo.slice
    registrations = {r.ref_id: r for r in store.all_registrations("teams")}
    assert "series-abc" in registrations
    assert "ws-new" in registrations["series-abc"].workstream_ids
    assert "demo.slice" not in registrations["series-abc"].workstream_ids


def test_registry_confirm_uses_email_channel_for_email_thread_sources(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="thread:auto:steady001",
                artifact_type="email_thread",
                display_name="Steady High Confidence Thread",
                thread_id="steady-thread-1",
                inferred_workstream="acme",
                confidence=0.9,
                confidence_source="keyword_router",
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                signal_yield_last_3=(1, 1, 1),
                topics=("SCHIE",),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    _make_uil_registration(
        program_dir,
        "acme",
        channel="email",
        ref_id="steady-thread-1",
        ref_kind="email_thread",
        workstream_id="acme",
        ref_title="Steady High Confidence Thread",
    )
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(
        app,
        ["registry", "confirm", "thread:auto:steady001", "--program", "acme", "--pm-alias", "operator"],
    )

    from src.core.channel_registry_store import ChannelRegistryStore

    store = ChannelRegistryStore(program_dir / "channel_registry.sqlite3", "acme", ensure_schema=False)
    registrations = {r.ref_id: r for r in store.all_registrations("email")}
    events = store.load_feedback_events("email", "steady-thread-1", "email_thread")

    assert result.exit_code == 0
    assert registrations["steady-thread-1"].pm_confirmed is True
    assert [event.action for event in events] == ["confirm"]


def test_registry_uil_commands_emit_deprecation_warning(monkeypatch, tmp_path: Path) -> None:
    """Commands that dual-write to UIL should emit a deprecation warning on stderr (§9.3)."""
    from src.core.integration_types import RegistrationStatus
    from src.core.channel_registry_store import ChannelRegistryStore

    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly", series_id="series-abc"),),
                    teams_chats=(),
                    workiq_keywords=(),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc),
    )
    _make_uil_teams_registry(program_dir, "acme")
    monkeypatch.setattr("src.commands.registry.PROGRAMS_ROOT", programs_root)

    # `vertex registry list` routing to UIL should warn on stderr
    result = runner.invoke(app, ["registry", "list", "--program", "acme", "--source", "auto"])
    assert result.exit_code == 0, result.output
    assert "deprecation" in result.output.lower()
    assert "vertex integration show" in result.output

    # `vertex registry confirm` dual-writing to UIL should warn on stderr
    result = runner.invoke(
        app,
        ["registry", "confirm", "meet:acme-acme-weekly", "--program", "acme", "--pm-alias", "operator"],
    )
    assert result.exit_code == 0, result.output
    assert "deprecation" in result.output.lower()
    assert "vertex integration confirm" in result.output
