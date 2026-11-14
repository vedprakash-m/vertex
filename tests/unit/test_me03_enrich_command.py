"""ME-03: vertex enrich command tests."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest

from src.core.exceptions import StateError


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
    """Dry-run does not save evidence when dependencies are mocked."""
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


@pytest.mark.parametrize("dry_run", [True, False], ids=["preview", "normal"])
@pytest.mark.parametrize(
    ("outcome", "expected_result"),
    [
        ("query_error", "query_error:RuntimeError"),
        ("no_response", "query_error:no_response"),
        ("extraction_error", "extraction_error:RuntimeError"),
        ("no_evidence", "no_structured_evidence"),
        ("legacy_success", "structured_evidence"),
        ("cluster_failure", "cluster_query_failed"),
        ("thread_empty", "no_structured_evidence"),
        ("thread_success", "per_thread_evidence"),
        ("thread_incomplete", None),
    ],
)
def test_enrich_preview_preserves_lane_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
    outcome: str,
    expected_result: str | None,
) -> None:
    from src.commands.enrich import _lane_cluster_key, enrich_command
    from src.core.evidence_models import SourceRef, WorkstreamEvidence
    from src.core.models import RiskLevel

    entry = {
        "id": "lane-a", "name": "Synthetic lane",
        "signal_sources": {"teams_meeting_series": [{"display_name": "Synthetic meeting"}]},
    }
    per_thread = outcome.startswith("thread_")
    retrieval = SimpleNamespace(per_thread_extraction=True) if per_thread else None
    resolved = SimpleNamespace(
        paths=SimpleNamespace(program_id="synthetic"),
        program=SimpleNamespace(m365=SimpleNamespace(retrieval=retrieval)),
    )
    evidence = WorkstreamEvidence(
        lane_id="lane-a", synthesized_at=datetime(2026, 6, 21, tzinfo=timezone.utc),
        risk_level=RiskLevel.LOW, etas=(), blocking_items=(), owners=(),
        source_refs=(SourceRef("workiq_email", "Synthetic source", None, None, canonical_id="native-thread-1"),),
        raw_excerpts=(), confidence=0.8, narrative_summary="Synthetic evidence", stale_after=None,
    )
    bridge = MagicMock()
    bridge.probe.return_value = SimpleNamespace(available=True, has_workiq=True)
    if outcome == "query_error":
        bridge.ask_workiq.side_effect = RuntimeError("synthetic query failure")
    else:
        bridge.ask_workiq.return_value = None if outcome == "no_response" else "synthetic response"
    agent = MagicMock()
    if outcome == "extraction_error":
        agent.extract.side_effect = RuntimeError("synthetic extraction failure")
    else:
        agent.extract.return_value = evidence if outcome == "legacy_success" else None
    thread_evidence = (evidence,) if outcome == "thread_success" else ()
    state_path = tmp_path / "lane-state.json"
    initial = b'{"lanes":{"lane-a":{"yield_last_3":[1,1,1],"workiq_query_suspended":false}}}\n'
    state_path.write_bytes(initial)
    guard = MagicMock()

    with (
        patch("src.commands.enrich.resolve_edition", return_value=resolved),
        patch("src.commands.enrich.load_program_context"),
        patch("src.commands.enrich._PROGRAMS_ROOT", tmp_path),
        patch("src.commands.enrich._load_registry_lanes", return_value=[entry]),
        patch("src.commands.enrich._build_workiq_cost_guard", return_value=guard),
        patch("src.commands.enrich._workiq_lane_state_path", return_value=state_path),
        patch("src.commands.enrich._build_cluster_response_cache",
              return_value={_lane_cluster_key(entry): None}),
        patch("src.commands.enrich._extract_per_thread_evidence",
              return_value=(thread_evidence, outcome == "thread_incomplete")) as extract_threads,
        patch("src.commands.gather_pipeline.evidence_extraction_stage.persist_evidence",
              return_value=False) as persist,
        patch("src.commands.gather_pipeline.evidence_extraction_stage._persist_evidence",
              return_value=(False, evidence)) as persist_admitted,
        patch("src.m365.agency_bridge.AgencyBridge", return_value=bridge),
        patch("src.ai.content_extractor.ContentExtractionAgent", return_value=agent),
    ):
        enrich_command(
            edition="synthetic", lane=None, since="7d", dry_run=dry_run,
            accept=False, output_format="human", batch=outcome == "cluster_failure",
        )

    if dry_run or expected_result is None:
        assert state_path.read_bytes() == initial
    else:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["lanes"]["lane-a"]["last_result"] == expected_result
    if per_thread:
        assert extract_threads.call_args.kwargs["dry_run"] is dry_run
    if dry_run:
        persist.assert_not_called()
        persist_admitted.assert_not_called()
        captured = capsys.readouterr()
        assert "no files written" not in captured.out
        assert "WorkIQ budget and circuit-breaker state may be written" in captured.err
    elif outcome == "legacy_success":
        persist.assert_called_once()
        persist_admitted.assert_not_called()
    elif outcome == "thread_success":
        persist.assert_not_called()
        persist_admitted.assert_called_once()
    else:
        persist.assert_not_called()
        persist_admitted.assert_not_called()
    if not per_thread and outcome != "cluster_failure":
        guard.check.assert_called_once()
        guard.record_actual.assert_called_once()


@pytest.mark.parametrize("output_format", ["human", "json"])
def test_enrich_preview_warns_before_safety_initialization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], output_format: str,
) -> None:
    from src.commands.enrich import enrich_command

    resolved = SimpleNamespace(
        paths=SimpleNamespace(program_id="synthetic"),
        program=SimpleNamespace(m365=None),
    )
    bridge = MagicMock()
    bridge.probe.return_value = SimpleNamespace(available=True, has_workiq=True)
    prior_output = []

    def build_guard(**_: object) -> None:
        prior_output.append(capsys.readouterr())
        return None

    with (
        patch("src.commands.enrich.resolve_edition", return_value=resolved),
        patch("src.commands.enrich.load_program_context"),
        patch("src.commands.enrich._PROGRAMS_ROOT", tmp_path),
        patch("src.commands.enrich._load_registry_lanes", return_value=[]),
        patch("src.commands.enrich._build_workiq_cost_guard", side_effect=build_guard),
        patch("src.m365.agency_bridge.AgencyBridge", return_value=bridge),
    ):
        enrich_command(
            edition="synthetic", lane=None, since="7d", dry_run=True,
            accept=False, output_format=output_format, batch=False,
        )

    assert len(prior_output) == 1
    assert "WorkIQ budget and circuit-breaker state may be written" in prior_output[0].err
    assert "CLI startup may also write state" in prior_output[0].err
    assert "no files written" not in capsys.readouterr().out


@pytest.mark.parametrize("dry_run", [True, False], ids=["preview", "normal"])
def test_enrich_preview_keeps_workiq_circuit_breaker_active(
    tmp_path: Path, dry_run: bool,
) -> None:
    from src.commands.enrich import enrich_command
    from src.core.circuit_breaker import CircuitBreaker
    from src.m365.agency_bridge import AgencyBridge, AgencyCapabilities

    lane_state = tmp_path / "lane-state.json"
    breaker = CircuitBreaker(state_path=tmp_path / "breaker.json")
    resolved = SimpleNamespace(
        paths=SimpleNamespace(program_id="synthetic"),
        program=SimpleNamespace(m365=None),
    )
    caps = AgencyCapabilities(available=True, has_workiq=True)

    def synthetic_bridge(*, workiq_breaker: CircuitBreaker) -> AgencyBridge:
        bridge = AgencyBridge(workiq_breaker=workiq_breaker)
        bridge.probe = MagicMock(return_value=caps)  # type: ignore[method-assign]
        bridge.invoke_mcp_tool = MagicMock(return_value=None)  # type: ignore[method-assign]
        bridge.inspect_workiq = MagicMock(return_value=SimpleNamespace(error="synthetic failure"))  # type: ignore[method-assign]
        return bridge

    with (
        patch("src.commands.enrich.resolve_edition", return_value=resolved),
        patch("src.commands.enrich.load_program_context"),
        patch("src.commands.enrich._PROGRAMS_ROOT", tmp_path),
        patch("src.commands.enrich._load_registry_lanes", return_value=[{
            "id": "lane-a", "name": "Synthetic lane",
            "signal_sources": {"teams_meeting_series": [{"display_name": "Synthetic meeting"}]},
        }]),
        patch("src.commands.enrich._build_workiq_cost_guard", return_value=None),
        patch("src.commands.enrich._build_workiq_breaker", return_value=breaker),
        patch("src.commands.enrich._workiq_lane_state_path", return_value=lane_state),
        patch("src.m365.agency_bridge.AgencyBridge", side_effect=synthetic_bridge),
    ):
        enrich_command(
            edition="synthetic", lane=None, since="7d", dry_run=dry_run,
            accept=False, output_format="human", batch=False,
        )

    assert breaker.get_state().failure_count == 1
    assert lane_state.exists() is not dry_run


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


@pytest.mark.parametrize("budget", [".nan", ".inf", "-.inf"], ids=["nan", "infinity", "negative-infinity"])
@pytest.mark.parametrize("batch", [True, False], ids=["batch", "single"])
def test_enrich_rejects_nonfinite_enabled_budget_before_bridge(
    tmp_path: Path, budget: str, batch: bool,
) -> None:
    from src.commands.enrich import enrich_command
    from src.core.edition_resolver import _parse_ai
    import yaml

    resolved = SimpleNamespace(
        paths=SimpleNamespace(program_id="acme"),
        program=SimpleNamespace(ai=_parse_ai(yaml.safe_load(f"enabled: true\nbudget_usd_per_run: {budget}\n"))),
    )
    with (
        patch("src.commands.enrich.resolve_edition", return_value=resolved),
        patch("src.commands.enrich.load_program_context"),
        patch("src.commands.enrich._PROGRAMS_ROOT", tmp_path),
        patch("src.commands.enrich._load_registry_lanes", return_value=[]) as load_registry,
        patch("src.m365.agency_bridge.AgencyBridge") as bridge,
        pytest.raises(StateError, match="Invalid cost guard budget_usd value"),
    ):
        enrich_command(
            edition="acme_weekly", lane=None, since="7d", dry_run=True,
            accept=False, output_format="human", batch=batch,
        )
    load_registry.assert_not_called()
    bridge.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("enabled", "budget", "active"),
    [
        (True, 0.0, False),
        (True, -0.5, False),
        (True, 0.5, True),
        (False, 0.5, False),
        (False, float("nan"), False),
        (False, float("inf"), False),
        (False, float("-inf"), False),
    ],
)
def test_workiq_budget_activation_preserves_existing_modes(
    tmp_path: Path, enabled: bool, budget: float, active: bool,
) -> None:
    from src.commands.enrich import _build_workiq_cost_guard
    from src.core.models_v2 import AIConfig

    guard = _build_workiq_cost_guard(
        edition="acme_weekly",
        program=SimpleNamespace(ai=AIConfig(enabled=enabled, budget_usd_per_run=budget)),
        started_at=datetime(2026, 6, 18, tzinfo=timezone.utc),
        programs_root=tmp_path,
    )
    assert (guard is not None) is active
    assert list(tmp_path.iterdir()) == []


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


def _cost_test_clusters() -> list[dict]:
    return [
        {
            "id": f"lane-{cluster}-{lane}",
            "name": f"Lane {cluster} {lane}",
            "signal_sources": {"teams_meeting_series": [{"display_name": f"Series {cluster}"}]},
        }
        for cluster in ("a", "b")
        for lane in (1, 2)
    ]


@pytest.mark.parametrize("error_type", [StateError, OSError, UnicodeError, RuntimeError])
def test_cluster_cost_check_propagates_unexpected_failure_before_provider(
    tmp_path: Path, error_type: type[Exception],
) -> None:
    from src.commands.enrich import _build_cluster_response_cache

    guard = MagicMock()
    guard.check.side_effect = error_type("synthetic cost-state failure")
    bridge = MagicMock()

    with pytest.raises(error_type, match="synthetic cost-state failure"):
        _build_cluster_response_cache(
            registry_entries=_cost_test_clusters(),
            since_dt=datetime(2026, 6, 10, tzinfo=timezone.utc),
            edition="acme_weekly",
            programs_root=tmp_path,
            bridge=bridge,
            workiq_cost_guard=guard,
        )

    guard.check.assert_called_once()
    guard.record_actual.assert_not_called()
    bridge.ask_workiq.assert_not_called()


def test_cluster_cost_check_rejects_malformed_retained_state_before_provider(tmp_path: Path) -> None:
    import sqlite3
    from src.ai.cost_guard import CostGuard
    from src.commands.enrich import _build_cluster_response_cache

    guard = CostGuard(edition="acme_weekly", run_id="cluster", budget_usd=1.0, programs_root=tmp_path)
    guard.record_actual(0.01)
    with sqlite3.connect(guard.ledger_path) as connection:
        connection.execute("UPDATE ai_cost_runs SET spent_usd = ?", ("invalid-synthetic-amount",))
    before = {path: path.read_bytes() for path in (guard.ledger_path, guard.state_path)}
    bridge = MagicMock()

    with pytest.raises(StateError):
        _build_cluster_response_cache(
            registry_entries=_cost_test_clusters(),
            since_dt=datetime(2026, 6, 10, tzinfo=timezone.utc),
            edition="acme_weekly",
            programs_root=tmp_path,
            bridge=bridge,
            workiq_cost_guard=guard,
        )

    bridge.ask_workiq.assert_not_called()
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("error_type", [StateError, OSError, UnicodeError])
@pytest.mark.parametrize("provider_succeeds", [True, False])
def test_cluster_projection_failure_does_not_record_or_dispatch_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception],
    provider_succeeds: bool,
) -> None:
    from src.ai.cost_guard import CostGuard, CostRunState
    from src.commands.enrich import _build_cluster_response_cache, _estimate_workiq_query_cost_usd

    guard = CostGuard(edition="acme_weekly", run_id="cluster", budget_usd=1.0, programs_root=tmp_path)
    original_projection = guard._persist_projection
    projection_calls: list[CostRunState] = []

    def fail_first_projection(state: CostRunState) -> None:
        projection_calls.append(state)
        if len(projection_calls) == 1:
            raise error_type("synthetic projection failure")
        original_projection(state)

    monkeypatch.setattr(guard, "_persist_projection", fail_first_projection)
    bridge = MagicMock()
    if provider_succeeds:
        bridge.ask_workiq.return_value = "synthetic response"
    else:
        bridge.ask_workiq.side_effect = RuntimeError("synthetic provider failure")

    try:
        with pytest.raises(error_type, match="synthetic projection failure"):
            _build_cluster_response_cache(
                registry_entries=_cost_test_clusters(),
                since_dt=datetime(2026, 6, 10, tzinfo=timezone.utc),
                edition="acme_weekly",
                programs_root=tmp_path,
                bridge=bridge,
                workiq_cost_guard=guard,
            )
    finally:
        query = bridge.ask_workiq.call_args_list[0].args[0]
        state = guard.current_state()
        assert (
            bridge.ask_workiq.call_count,
            len(projection_calls),
            state.ai_calls,
            state.spent_usd,
            guard.state_path.exists(),
        ) == (
            1,
            1,
            1,
            pytest.approx(_estimate_workiq_query_cost_usd(
                query=query, response_text="synthetic response" if provider_succeeds else None,
            )),
            False,
        )


@pytest.mark.parametrize("provider_succeeds", [True, False])
def test_cluster_corrupt_projection_is_preserved_after_recording(
    tmp_path: Path, provider_succeeds: bool,
) -> None:
    from src.ai.cost_guard import CostGuard
    from src.commands.enrich import _build_cluster_response_cache, _estimate_workiq_query_cost_usd

    guard = CostGuard(edition="acme_weekly", run_id="cluster", budget_usd=1.0, programs_root=tmp_path)
    guard.record_actual(0.0, ai_calls=0)
    corrupt = b'{"edition": "private-projection"'
    guard.state_path.write_bytes(corrupt)
    bridge = MagicMock()
    if provider_succeeds:
        bridge.ask_workiq.return_value = "synthetic response"
    else:
        bridge.ask_workiq.side_effect = RuntimeError("synthetic provider failure")

    with pytest.raises(StateError, match="Unable to read cost guard projection"):
        _build_cluster_response_cache(
            registry_entries=_cost_test_clusters(),
            since_dt=datetime(2026, 6, 10, tzinfo=timezone.utc),
            edition="acme_weekly",
            programs_root=tmp_path,
            bridge=bridge,
            workiq_cost_guard=guard,
        )

    bridge.ask_workiq.assert_called_once()
    query = bridge.ask_workiq.call_args.args[0]
    state = guard.current_state()
    assert state.ai_calls == 1
    assert state.spent_usd == pytest.approx(_estimate_workiq_query_cost_usd(
        query=query, response_text="synthetic response" if provider_succeeds else None,
    ))
    assert guard.state_path.read_bytes() == corrupt


def test_cluster_budget_refusal_keeps_existing_cache_behavior(tmp_path: Path) -> None:
    from src.ai.client import BudgetExceeded
    from src.commands.enrich import _build_cluster_response_cache, _lane_cluster_key

    entries = _cost_test_clusters()
    guard = MagicMock()
    guard.check.side_effect = BudgetExceeded("synthetic budget refusal")
    bridge = MagicMock()

    cache = _build_cluster_response_cache(
        registry_entries=entries,
        since_dt=datetime(2026, 6, 10, tzinfo=timezone.utc),
        edition="acme_weekly",
        programs_root=tmp_path,
        bridge=bridge,
        workiq_cost_guard=guard,
    )

    assert cache == {_lane_cluster_key(entries[0]): None}
    guard.check.assert_called_once()
    guard.record_actual.assert_not_called()
    bridge.ask_workiq.assert_not_called()


@pytest.mark.parametrize("with_guard", [True, False])
def test_cluster_provider_failure_records_once_and_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, with_guard: bool,
) -> None:
    from src.commands.enrich import (
        _build_cluster_response_cache,
        _estimate_workiq_query_cost_usd,
        _lane_cluster_key,
    )

    marker = "synthetic-private-cluster-error-7f2"
    meeting = "synthetic-private-meeting-7f2"
    entries = _cost_test_clusters()
    for entry in entries[:2]:
        entry["signal_sources"]["teams_meeting_series"][0]["display_name"] = meeting
    guard = MagicMock() if with_guard else None
    bridge = MagicMock()
    bridge.ask_workiq.side_effect = [RuntimeError(marker), "synthetic response"]

    cache = _build_cluster_response_cache(
        registry_entries=entries,
        since_dt=datetime(2026, 6, 10, tzinfo=timezone.utc),
        edition="acme_weekly",
        programs_root=tmp_path,
        bridge=bridge,
        workiq_cost_guard=guard,
    )

    assert cache == {
        _lane_cluster_key(entries[0]): None,
        _lane_cluster_key(entries[2]): "synthetic response",
    }
    assert bridge.ask_workiq.call_count == 2
    assert "Cluster WorkIQ call failed" in caplog.text
    assert marker not in caplog.text
    assert meeting not in caplog.text
    if guard is not None:
        assert guard.check.call_count == guard.record_actual.call_count == 2
        first_query = bridge.ask_workiq.call_args_list[0].args[0]
        second_query = bridge.ask_workiq.call_args_list[1].args[0]
        assert guard.record_actual.call_args_list[0].args[0] == _estimate_workiq_query_cost_usd(query=first_query)
        assert guard.record_actual.call_args_list[1].args[0] == _estimate_workiq_query_cost_usd(
            query=second_query, response_text="synthetic response",
        )


@pytest.mark.parametrize("phase", ["check", "record"])
def test_enrich_stops_before_later_dispatch_on_cluster_cost_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    from src.ai.cost_guard import CostGuard, CostRunState
    from src.commands.enrich import enrich_command

    guard = CostGuard(edition="acme_weekly", run_id="cluster", budget_usd=1.0, programs_root=tmp_path)
    if phase == "check":
        check = MagicMock(side_effect=OSError("synthetic cost-state failure"))
        monkeypatch.setattr(guard, "check", check)
    else:
        original_projection = guard._persist_projection
        projection_calls: list[CostRunState] = []

        def fail_first_projection(state: CostRunState) -> None:
            projection_calls.append(state)
            if len(projection_calls) == 1:
                raise OSError("synthetic cost-state failure")
            original_projection(state)

        monkeypatch.setattr(guard, "_persist_projection", fail_first_projection)

    resolved = SimpleNamespace(
        paths=SimpleNamespace(program_id="acme"),
        program=SimpleNamespace(m365=None),
    )
    bridge = MagicMock()
    bridge.probe.return_value = SimpleNamespace(available=True, has_workiq=True)
    bridge.ask_workiq.return_value = "synthetic response"
    agent = MagicMock()
    agent.extract.return_value = None

    with (
        patch("src.commands.enrich.resolve_edition", return_value=resolved),
        patch("src.commands.enrich.load_program_context"),
        patch("src.commands.enrich._PROGRAMS_ROOT", tmp_path),
        patch("src.commands.enrich._load_registry_lanes", return_value=_cost_test_clusters()),
        patch("src.commands.enrich._build_workiq_cost_guard", return_value=guard),
        patch("src.commands.enrich._record_workiq_lane_result") as record_lane,
        patch("src.m365.agency_bridge.AgencyBridge", return_value=bridge),
        patch("src.ai.content_extractor.ContentExtractionAgent", return_value=agent),
    ):
        try:
            with pytest.raises(OSError, match="synthetic cost-state failure"):
                enrich_command(
                    edition="acme_weekly", lane=None, since="7d", dry_run=True,
                    accept=False, output_format="human", batch=True,
                )
        finally:
            assert bridge.ask_workiq.call_count == (0 if phase == "check" else 1)
            record_lane.assert_not_called()
            if phase == "check":
                check.assert_called_once()
            else:
                assert len(projection_calls) == 1
                assert guard.current_state().ai_calls == 1


@pytest.mark.parametrize("refusal_at", [1, 2])
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("output_format", ["human", "json"])
def test_enrich_stops_all_work_after_cluster_budget_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], refusal_at: int,
    dry_run: bool, output_format: str,
) -> None:
    from src.ai.client import BudgetExceeded
    from src.commands.enrich import enrich_command

    guard = MagicMock()
    checks: list[float] = []

    def check(cost: float) -> None:
        checks.append(cost)
        if len(checks) == refusal_at:
            raise BudgetExceeded("synthetic budget refusal")

    guard.check.side_effect = check
    resolved = SimpleNamespace(
        paths=SimpleNamespace(program_id="acme"),
        program=SimpleNamespace(m365=None),
    )
    bridge = MagicMock()
    bridge.probe.return_value = SimpleNamespace(available=True, has_workiq=True)
    bridge.ask_workiq.return_value = '{"risk_level": "low", "confidence": 0.7}'

    with (
        patch("src.commands.enrich.resolve_edition", return_value=resolved),
        patch("src.commands.enrich.load_program_context"),
        patch("src.commands.enrich._PROGRAMS_ROOT", tmp_path),
        patch("src.commands.enrich._load_registry_lanes", return_value=_cost_test_clusters()),
        patch("src.commands.enrich._build_workiq_cost_guard", return_value=guard),
        patch("src.commands.enrich._is_workiq_lane_suspended", return_value=False),
        patch("src.commands.enrich._record_workiq_lane_result") as record_lane,
        patch("src.commands.gather_pipeline.evidence_extraction_stage.persist_evidence", return_value=False) as persist,
        patch("src.m365.agency_bridge.AgencyBridge", return_value=bridge),
    ):
        enrich_command(
            edition="acme_weekly", lane=None, since="7d", dry_run=dry_run,
            accept=False, output_format=output_format, batch=True,
        )

    assert len(checks) == refusal_at
    assert guard.record_actual.call_count == refusal_at - 1
    assert bridge.ask_workiq.call_count == refusal_at - 1
    record_lane.assert_not_called()
    persist.assert_not_called()
    assert "WorkIQ budget exceeded" in capsys.readouterr().out


@pytest.mark.parametrize("dry_run", [True, False], ids=["preview", "normal"])
def test_enrich_processes_clusters_when_budget_checks_succeed(
    tmp_path: Path, dry_run: bool,
) -> None:
    from src.commands.enrich import enrich_command

    guard = MagicMock()
    resolved = SimpleNamespace(
        paths=SimpleNamespace(program_id="acme"),
        program=SimpleNamespace(m365=None),
    )
    bridge = MagicMock()
    bridge.probe.return_value = SimpleNamespace(available=True, has_workiq=True)
    bridge.ask_workiq.return_value = '{"risk_level": "low", "confidence": 0.7}'

    with (
        patch("src.commands.enrich.resolve_edition", return_value=resolved),
        patch("src.commands.enrich.load_program_context"),
        patch("src.commands.enrich._PROGRAMS_ROOT", tmp_path),
        patch("src.commands.enrich._load_registry_lanes", return_value=_cost_test_clusters()),
        patch("src.commands.enrich._build_workiq_cost_guard", return_value=guard),
        patch("src.commands.enrich._is_workiq_lane_suspended", return_value=False),
        patch("src.commands.enrich._record_workiq_lane_result") as record_lane,
        patch("src.commands.gather_pipeline.evidence_extraction_stage.persist_evidence",
              return_value=False) as persist,
        patch("src.m365.agency_bridge.AgencyBridge", return_value=bridge),
    ):
        enrich_command(
            edition="acme_weekly", lane=None, since="7d", dry_run=dry_run,
            accept=False, output_format="human", batch=True,
        )

    assert guard.check.call_count == 2
    assert guard.record_actual.call_count == 2
    assert bridge.ask_workiq.call_count == 6
    assert record_lane.call_count == (0 if dry_run else 4)
    assert persist.call_count == (0 if dry_run else 4)
