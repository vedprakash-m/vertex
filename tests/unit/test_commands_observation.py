from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.core.metric_models import MetricObservation, MetricQualityState
from src.core.reality_store import RealityStore


runner = CliRunner()


def test_observation_inject_command_writes_manual_observation(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "observation",
            "inject",
            "--program",
            "acme",
            "--metric",
            "acme.cluster_count",
            "--value",
            "123",
            "--measurement-period-start",
            "2026-05-20T09:00:00Z",
            "--measurement-period-end",
            "2026-05-20T10:00:00Z",
            "--observed-at",
            "2026-05-20T10:05:00Z",
            "--dimension",
            "region=eastus2",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    observations = store.list_metric_observations("acme.cluster_count")

    assert result.exit_code == 0
    assert len(observations) == 1
    assert observations[0].quality_state.value == "manual"
    assert observations[0].value_num == 123.0
    assert observations[0].dimensions_json == '{"region":"eastus2"}'


def test_observation_pin_and_unpin_commands_update_manual_observation(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json='{"region":"eastus2"}',
            measurement_period_start=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
            value_num=123.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.MANUAL,
        )
    )

    pin_result = runner.invoke(
        app,
        [
            "observation",
            "pin",
            "--program",
            "acme",
            "--metric",
            "acme.cluster_count",
            "--measurement-period-end",
            "2026-05-20T10:00:00Z",
            "--dimension",
            "region=eastus2",
            "--reason",
            "DRI confirmed outage telemetry is stale",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    pinned = store.list_metric_observations("acme.cluster_count")[0]

    assert pin_result.exit_code == 0
    assert pinned.is_pinned is True
    assert pinned.pin_reason == "DRI confirmed outage telemetry is stale"

    unpin_result = runner.invoke(
        app,
        [
            "observation",
            "unpin",
            "--program",
            "acme",
            "--metric",
            "acme.cluster_count",
            "--measurement-period-end",
            "2026-05-20T10:00:00Z",
            "--dimension",
            "region=eastus2",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    unpinned = store.list_metric_observations("acme.cluster_count")[0]

    assert unpin_result.exit_code == 0
    assert unpinned.is_pinned is False
    assert unpinned.pinned_at is None
    assert unpinned.pin_reason is None


def test_observation_inject_command_confirms_and_overwrites_existing_manual_observation(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
            value_num=123.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.MANUAL,
            is_pinned=True,
            pinned_at=datetime(2026, 5, 20, 10, 6, tzinfo=timezone.utc),
            pin_reason="Temporary override",
        )
    )

    result = runner.invoke(
        app,
        [
            "observation",
            "inject",
            "--program",
            "acme",
            "--metric",
            "acme.cluster_count",
            "--value",
            "456",
            "--measurement-period-start",
            "2026-05-20T09:00:00Z",
            "--measurement-period-end",
            "2026-05-20T10:00:00Z",
            "--observed-at",
            "2026-05-20T10:10:00Z",
            "--db-root",
            str(tmp_path / "db"),
        ],
        input="y\n",
    )

    observations = store.list_metric_observations("acme.cluster_count")

    assert result.exit_code == 0
    assert "Overwrote manual observation obs-001" in result.output
    assert len(observations) == 1
    assert observations[0].observation_id == "obs-001"
    assert observations[0].value_num == 456.0
    assert observations[0].is_pinned is False
    assert observations[0].pin_reason is None


def test_observation_inject_command_force_overwrites_existing_manual_observation(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
            value_num=123.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.MANUAL,
        )
    )

    result = runner.invoke(
        app,
        [
            "observation",
            "inject",
            "--program",
            "acme",
            "--metric",
            "acme.cluster_count",
            "--value",
            "789",
            "--measurement-period-start",
            "2026-05-20T09:00:00Z",
            "--measurement-period-end",
            "2026-05-20T10:00:00Z",
            "--observed-at",
            "2026-05-20T10:15:00Z",
            "--force",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    observations = store.list_metric_observations("acme.cluster_count")

    assert result.exit_code == 0
    assert "Overwrote manual observation obs-001" in result.output
    assert len(observations) == 1
    assert observations[0].value_num == 789.0