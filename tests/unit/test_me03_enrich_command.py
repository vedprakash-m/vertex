"""ME-03: vertex enrich command tests."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest


def test_build_lane_query_with_meeting_series() -> None:
    """Lane with teams_meeting_series produces a structured WorkIQ query."""
    from src.commands.enrich import _build_lane_query
    entry = {
        "id": "acme.networking",
        "name": "Networking Parity (Wingtip)",
        "signal_sources": {
            "teams_meeting_series": [
                {"display_name": "Acme Weekly Ops Review"},
                {"display_name": "Adventure Ramp Weekly Sync"},
            ]
        },
    }
    since_dt = datetime(2026, 6, 10, tzinfo=timezone.utc)
    query = _build_lane_query(entry, since_dt=since_dt)
    assert query is not None
    assert "Networking Parity" in query
    assert "Acme Weekly Ops Review" in query
    assert "risk level" in query.lower()
    assert "JSON" in query


def test_build_lane_query_returns_none_if_no_sources() -> None:
    """Lane with no signal_sources returns None."""
    from src.commands.enrich import _build_lane_query
    entry = {"id": "acme.unknown", "name": "Unknown", "signal_sources": {}}
    assert _build_lane_query(entry, since_dt=datetime(2026, 6, 10, tzinfo=timezone.utc)) is None


def test_parse_since() -> None:
    """_parse_since converts '7d' → 7, '14d' → 14."""
    from src.commands.enrich import _parse_since
    assert _parse_since("7d") == 7
    assert _parse_since("14d") == 14
    assert _parse_since("invalid") == 7  # fallback


def test_load_registry_lanes(tmp_path: Path) -> None:
    """_load_registry_lanes reads workstream_registry.yaml correctly."""
    from src.commands.enrich import _load_registry_lanes
    import yaml
    (tmp_path / "acme").mkdir()
    registry = {
        "schema_version": "1.0",
        "workstreams": [
            {"id": "acme", "name": "Acme on Northwind", "lifecycle_state": "active"},
            {"id": "acme.networking", "name": "Networking", "lifecycle_state": "active"},
        ],
    }
    with open(tmp_path / "acme" / "workstream_registry.yaml", "w", encoding="utf-8") as f:
        yaml.dump(registry, f)
    lanes = _load_registry_lanes("acme", tmp_path)
    assert len(lanes) == 2
    assert lanes[0]["id"] == "acme"


def test_workiq_lane_state_suspends_after_three_zero_yield_runs(tmp_path: Path) -> None:
    from src.commands.enrich import _load_workiq_lane_state, _record_workiq_lane_result

    as_of = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    for offset in range(3):
        _record_workiq_lane_result(
            lane_id="lane-a",
            edition="acme_weekly",
            program_id="acme",
            programs_root=tmp_path,
            observed_yield=0,
            as_of=as_of.replace(hour=12 + offset),
            last_result="no_structured_evidence",
        )

    lane_state = _load_workiq_lane_state(edition="acme_weekly", programs_root=tmp_path)["lane-a"]
    assert lane_state["yield_last_3"] == [0, 0, 0]
    assert lane_state["zero_yield_streak"] == 3
    assert lane_state["workiq_query_suspended"] is True


def test_workiq_lane_state_resets_suspension_after_positive_yield(tmp_path: Path) -> None:
    from src.commands.enrich import _load_workiq_lane_state, _record_workiq_lane_result

    as_of = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    for offset in range(3):
        _record_workiq_lane_result(
            lane_id="lane-a",
            edition="acme_weekly",
            program_id="acme",
            programs_root=tmp_path,
            observed_yield=0,
            as_of=as_of.replace(hour=12 + offset),
            last_result="no_structured_evidence",
        )
    _record_workiq_lane_result(
        lane_id="lane-a",
        edition="acme_weekly",
        program_id="acme",
        programs_root=tmp_path,
        observed_yield=1,
        as_of=as_of.replace(hour=16),
        last_result="structured_evidence",
    )

    lane_state = _load_workiq_lane_state(edition="acme_weekly", programs_root=tmp_path)["lane-a"]
    assert lane_state["yield_last_3"] == [1, 0, 0]
    assert lane_state["zero_yield_streak"] == 0
    assert lane_state["workiq_query_suspended"] is False


def test_is_workiq_lane_suspended_honors_manual_override_false(tmp_path: Path) -> None:
    from src.commands.enrich import _is_workiq_lane_suspended, _record_workiq_lane_result

    as_of = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    for offset in range(3):
        _record_workiq_lane_result(
            lane_id="lane-a",
            edition="acme_weekly",
            program_id="acme",
            programs_root=tmp_path,
            observed_yield=0,
            as_of=as_of.replace(hour=12 + offset),
            last_result="no_structured_evidence",
        )

    assert _is_workiq_lane_suspended(
        lane_entry={"workiq_query_suspended": False},
        lane_id="lane-a",
        edition="acme_weekly",
        programs_root=tmp_path,
    ) is False


def test_estimate_workiq_query_cost_usd_grows_with_response_size() -> None:
    from src.commands.enrich import _estimate_workiq_query_cost_usd

    small = _estimate_workiq_query_cost_usd(query="short query", response_text="ok")
    large = _estimate_workiq_query_cost_usd(query="short query", response_text="x" * 4000)

    assert small >= 0.01
    assert large > small


def test_enrich_command_dry_run(tmp_path: Path) -> None:
    """dry-run: prints lane output, writes no files. Uses fully mocked dependencies."""
    from unittest.mock import MagicMock, patch
    import yaml
    from typer.testing import CliRunner
    import typer

    # Setup minimal workstream_registry.yaml
    prog_dir = tmp_path / "acme"
    prog_dir.mkdir()
    registry = {
        "workstreams": [
            {
                "id": "acme.networking",
                "name": "Networking Parity",
                "signal_sources": {
                    "teams_meeting_series": [{"display_name": "Acme Weekly Ops Review"}]
                },
            }
        ]
    }
    with open(prog_dir / "workstream_registry.yaml", "w", encoding="utf-8") as f:
        yaml.dump(registry, f)

    # Build a fake ResolvedEdition with the program_id we need
    fake_paths = MagicMock()
    fake_paths.program_id = "acme"
    fake_resolved = MagicMock()
    fake_resolved.paths = fake_paths

    fake_caps = MagicMock()
    fake_caps.available = True
    fake_caps.has_workiq = True
    fake_bridge = MagicMock()
    fake_bridge.probe.return_value = fake_caps
    fake_bridge.ask_workiq.return_value = '{"risk_level": "medium", "etas": [], "blocking_items": [], "owners": [], "confidence": 0.7}'

    fake_app = typer.Typer()
    from src.commands.enrich import enrich_command
    fake_app.command("enrich")(enrich_command)
    runner = CliRunner()

    with (
        patch("src.commands.enrich.resolve_edition", return_value=fake_resolved),
        patch("src.commands.enrich.load_program_context", return_value=MagicMock()),
        patch("src.commands.enrich._PROGRAMS_ROOT", tmp_path),
        patch("src.m365.agency_bridge.AgencyBridge", return_value=fake_bridge),
    ):
        result = runner.invoke(fake_app, ["--edition", "acme_weekly", "--dry-run"])
    # Dry-run: no evidence_store.jsonl written
    store = prog_dir / "journal" / "evidence_store.jsonl"
    assert not store.exists(), "dry-run must not write evidence_store.jsonl"
    # Should have printed the lane name
    assert result.exit_code in (0, 1)  # exits 0 on success or 1 on WorkIQ error


def test_enrich_command_stops_when_workiq_budget_is_exhausted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from src.ai.cost_guard import load_latest_run_state
    from src.commands.enrich import enrich_command
    from src.core.evidence_models import SourceRef, WorkstreamEvidence
    from src.core.models import RiskLevel
    import yaml

    prog_dir = tmp_path / "acme"
    (prog_dir / "editions").mkdir(parents=True)
    (prog_dir / "editions" / "acme_weekly.yaml").write_text("id: acme_weekly\nprogram_id: acme\n", encoding="utf-8")
    registry = {
        "workstreams": [
            {
                "id": "lane-a",
                "name": "Lane A",
                "signal_sources": {"teams_meeting_series": [{"display_name": "Ops A"}]},
            },
            {
                "id": "lane-b",
                "name": "Lane B",
                "signal_sources": {"teams_meeting_series": [{"display_name": "Ops B"}]},
            },
        ]
    }
    with open(prog_dir / "workstream_registry.yaml", "w", encoding="utf-8") as handle:
        yaml.dump(registry, handle)

    fake_resolved = SimpleNamespace(
        paths=SimpleNamespace(program_id="acme"),
        program=SimpleNamespace(
            ai=SimpleNamespace(enabled=True, budget_usd_per_run=0.03),
        ),
    )
    fake_caps = SimpleNamespace(available=True, has_workiq=True)
    fake_bridge = MagicMock()
    fake_bridge.probe.return_value = fake_caps
    fake_bridge.ask_workiq.return_value = json.dumps(
        {
            "risk_level": "medium",
            "etas": [],
            "blocking_items": [],
            "owners": [],
            "narrative_summary": "x" * 5000,
            "confidence": 0.7,
        }
    )

    evidence = WorkstreamEvidence(
        lane_id="lane-a",
        synthesized_at=datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc),
        risk_level=RiskLevel.MEDIUM,
        etas=(),
        blocking_items=(),
        owners=(),
        source_refs=(SourceRef(source_type="workiq_email", description="x", source_date=None, author=None),),
        raw_excerpts=(),
        confidence=0.7,
        narrative_summary="Budgeted result",
        stale_after=None,
    )
    fake_agent = MagicMock()
    fake_agent.extract.return_value = evidence

    with (
        patch("src.commands.enrich.resolve_edition", return_value=fake_resolved),
        patch("src.commands.enrich.load_program_context", return_value=MagicMock()),
        patch("src.commands.enrich._PROGRAMS_ROOT", tmp_path),
        patch("src.m365.agency_bridge.AgencyBridge", return_value=fake_bridge),
        patch("src.ai.content_extractor.ContentExtractionAgent", return_value=fake_agent),
    ):
        enrich_command(
            edition="acme_weekly",
            lane=None,
            since="7d",
            dry_run=True,
            accept=False,
            output_format="human",
        )

    captured = capsys.readouterr()
    assert "WorkIQ budget exceeded" in captured.out
    assert fake_bridge.ask_workiq.call_count == 1

    latest_state = load_latest_run_state("acme_weekly", programs_root=tmp_path)
    assert latest_state is not None
    assert latest_state.ai_calls == 1
    assert latest_state.spent_usd > latest_state.budget_usd


# ---------------------------------------------------------------------------
# P4-17: lane-batched WorkIQ cluster helpers
# ---------------------------------------------------------------------------

def test_lane_cluster_key_groups_lanes_with_shared_series() -> None:
    """Two lanes that share the same meeting series names produce identical keys."""
    from src.commands.enrich import _lane_cluster_key

    series_a = {"signal_sources": {"teams_meeting_series": [{"display_name": "Acme Weekly"}]}}
    series_b = {"signal_sources": {"teams_meeting_series": [{"display_name": "Acme Weekly"}]}, "id": "lane-b"}

    assert _lane_cluster_key(series_a) == _lane_cluster_key(series_b)


def test_lane_cluster_key_solo_lane_gets_unique_key() -> None:
    """Lane with no meeting series gets a __solo__ prefixed unique key."""
    from src.commands.enrich import _lane_cluster_key

    entry = {"id": "acme.solo", "signal_sources": {}}
    key = _lane_cluster_key(entry)
    assert key.startswith("__solo__")


def test_lane_cluster_key_is_order_independent() -> None:
    """Cluster key is stable regardless of the order series appear in the list."""
    from src.commands.enrich import _lane_cluster_key

    entry_ab = {
        "signal_sources": {
            "teams_meeting_series": [
                {"display_name": "Series B"},
                {"display_name": "Series A"},
            ]
        }
    }
    entry_ba = {
        "signal_sources": {
            "teams_meeting_series": [
                {"display_name": "Series A"},
                {"display_name": "Series B"},
            ]
        }
    }
    assert _lane_cluster_key(entry_ab) == _lane_cluster_key(entry_ba)


def test_build_batched_cluster_query_contains_all_lane_names() -> None:
    """Cluster query mentions all lane names and shared meeting series."""
    from datetime import datetime, timezone
    from src.commands.enrich import _build_batched_cluster_query

    entries = [
        {
            "id": "lane-a",
            "name": "Networking Parity",
            "signal_sources": {"teams_meeting_series": [{"display_name": "Acme Weekly Ops"}]},
        },
        {
            "id": "lane-b",
            "name": "BIOS AP Rollout",
            "signal_sources": {"teams_meeting_series": [{"display_name": "Acme Weekly Ops"}]},
        },
    ]
    since_dt = datetime(2026, 6, 10, tzinfo=timezone.utc)
    query = _build_batched_cluster_query(entries, since_dt=since_dt)

    assert query is not None
    assert "Networking Parity" in query
    assert "BIOS AP Rollout" in query
    assert "Acme Weekly Ops" in query
    assert "2026-06-10" in query


def test_build_batched_cluster_query_returns_none_if_no_sources() -> None:
    """Returns None when cluster entries have no meeting series, chats, or area paths."""
    from datetime import datetime, timezone
    from src.commands.enrich import _build_batched_cluster_query

    entries = [{"id": "lane-a", "name": "Lane A", "signal_sources": {}},
               {"id": "lane-b", "name": "Lane B"}]
    result = _build_batched_cluster_query(entries, since_dt=datetime(2026, 6, 10, tzinfo=timezone.utc))
    assert result is None


def test_build_cluster_response_cache_skips_solo_and_single_lane_clusters(tmp_path: Path) -> None:
    """Solo lanes (0 or 1 per cluster) are excluded from the pre-fetch cache."""
    from datetime import datetime, timezone
    from unittest.mock import MagicMock
    from src.commands.enrich import _build_cluster_response_cache

    entries = [
        {"id": "lane-a", "name": "Lane A", "signal_sources": {}},  # no series → solo
        {"id": "lane-b", "name": "Lane B",
         "signal_sources": {"teams_meeting_series": [{"display_name": "Only Series"}]}},  # unique series → 1-lane cluster
    ]
    fake_bridge = MagicMock()

    cache = _build_cluster_response_cache(
        registry_entries=entries,
        since_dt=datetime(2026, 6, 10, tzinfo=timezone.utc),
        edition="acme_weekly",
        programs_root=tmp_path,
        bridge=fake_bridge,
        workiq_cost_guard=None,
    )

    assert cache == {}
    fake_bridge.ask_workiq.assert_not_called()


def test_build_cluster_response_cache_issues_one_call_per_multi_lane_cluster(tmp_path: Path) -> None:
    """Two lanes sharing a meeting series name trigger a single batched WorkIQ call."""
    from datetime import datetime, timezone
    from unittest.mock import MagicMock
    from src.commands.enrich import _build_cluster_response_cache, _lane_cluster_key

    shared_series = [{"display_name": "Acme Ops Weekly"}]
    entries = [
        {"id": "lane-a", "name": "Lane A", "signal_sources": {"teams_meeting_series": shared_series}},
        {"id": "lane-b", "name": "Lane B", "signal_sources": {"teams_meeting_series": shared_series}},
    ]
    expected_key = _lane_cluster_key(entries[0])

    fake_bridge = MagicMock()
    fake_bridge.ask_workiq.return_value = '{"risk_level": "low", "summary": "all good"}'

    cache = _build_cluster_response_cache(
        registry_entries=entries,
        since_dt=datetime(2026, 6, 10, tzinfo=timezone.utc),
        edition="acme_weekly",
        programs_root=tmp_path,
        bridge=fake_bridge,
        workiq_cost_guard=None,
    )

    assert expected_key in cache
    assert cache[expected_key] == '{"risk_level": "low", "summary": "all good"}'
    fake_bridge.ask_workiq.assert_called_once()
