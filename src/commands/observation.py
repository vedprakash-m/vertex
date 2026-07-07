from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

import typer

from src.core.metric_registry import load_metric_definition_map
from src.core.metric_models import MetricObservation, MetricQualityState
from src.core.reality_store import RealityStore


app = typer.Typer(help="Inject manual telemetry observations into L1 reality state.")


@app.command("inject")
def observation_inject_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    metric: str = typer.Option(..., "--metric", help="Metric id to record."),
    value: float = typer.Option(..., "--value", help="Numeric observation value."),
    measurement_period_start: str = typer.Option(..., "--measurement-period-start", help="Measurement window start in ISO-8601."),
    measurement_period_end: str = typer.Option(..., "--measurement-period-end", help="Measurement window end in ISO-8601."),
    observed_at: str | None = typer.Option(None, "--observed-at", help="Observation timestamp in ISO-8601. Defaults to now."),
    dimension: list[str] = typer.Option(None, "--dimension", help="Repeat as key=value to capture dimensions."),
    sample_count: int = typer.Option(1, "--sample-count", min=1, help="Optional sample count."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing manual observation for the same metric, dimensions, and period."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    program_id = program.strip()
    metric_id = metric.strip()
    if not program_id:
        raise typer.BadParameter("--program must be non-empty")
    if not metric_id:
        raise typer.BadParameter("--metric must be non-empty")

    definitions = load_metric_definition_map()
    if definitions and metric_id not in definitions:
        typer.echo(f"Warning: {metric_id} is not in the metric registry. Proceeding with injection.", err=True)

    period_start = _parse_iso_datetime(measurement_period_start, option_name="--measurement-period-start")
    period_end = _parse_iso_datetime(measurement_period_end, option_name="--measurement-period-end")
    if period_end < period_start:
        raise typer.BadParameter("--measurement-period-end must be on or after --measurement-period-start")

    observed = datetime.now(timezone.utc) if observed_at is None else _parse_iso_datetime(observed_at, option_name="--observed-at")
    dimensions_json = _parse_dimensions(dimension)

    store = RealityStore(program_id, db_root=db_root)
    store.initialize()
    observation_id = str(uuid.uuid4())
    observation = MetricObservation(
        observation_id=observation_id,
        program_id=program_id,
        metric_id=metric_id,
        dimensions_json=dimensions_json,
        measurement_period_start=period_start,
        measurement_period_end=period_end,
        observed_at=observed,
        value_num=value,
        value_text=None,
        sample_count=sample_count,
        quality_state=MetricQualityState.MANUAL,
    )
    existing_manual = store.find_manual_observation(
        metric_id,
        measurement_period_end=period_end,
        dimensions_json=dimensions_json,
    )
    if existing_manual is not None:
        if not force:
            confirmed = typer.confirm(
                f"Replacing previous manual injection of {existing_manual.value_num} at {existing_manual.inserted_at.isoformat()}. Proceed?",
                default=False,
            )
            if not confirmed:
                typer.echo("Manual observation injection aborted.")
                raise typer.Exit(code=1)
        observation_id = store.overwrite_manual_observation(existing_manual.observation_id, observation)
        typer.echo(f"Overwrote manual observation {observation_id} for {metric_id}")
        raise typer.Exit(code=0)
    store.write_metric_observation(observation)
    typer.echo(f"Injected manual observation {observation_id} for {metric_id}")
    raise typer.Exit(code=0)


@app.command("pin")
def observation_pin_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    metric: str = typer.Option(..., "--metric", help="Metric id to pin."),
    measurement_period_end: str = typer.Option(..., "--measurement-period-end", help="Measurement window end in ISO-8601."),
    dimension: list[str] = typer.Option(None, "--dimension", help="Repeat as key=value to identify dimensions."),
    reason: str = typer.Option(..., "--reason", help="Why this manual observation should override telemetry."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    store = RealityStore(program.strip(), db_root=db_root)
    store.initialize()
    observation = _resolve_manual_observation(
        store=store,
        metric_id=metric.strip(),
        measurement_period_end=measurement_period_end,
        dimension=dimension,
    )
    if observation.is_pinned:
        typer.echo(f"Manual observation {observation.observation_id} is already pinned for {observation.metric_id}")
        raise typer.Exit(code=0)
    store.update_observation_pin(
        observation.observation_id,
        pinned=True,
        reason=reason,
        pinned_at=datetime.now(timezone.utc),
    )
    typer.echo(f"Pinned manual observation {observation.observation_id} for {observation.metric_id}")
    raise typer.Exit(code=0)


@app.command("unpin")
def observation_unpin_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    metric: str = typer.Option(..., "--metric", help="Metric id to unpin."),
    measurement_period_end: str = typer.Option(..., "--measurement-period-end", help="Measurement window end in ISO-8601."),
    dimension: list[str] = typer.Option(None, "--dimension", help="Repeat as key=value to identify dimensions."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    store = RealityStore(program.strip(), db_root=db_root)
    store.initialize()
    observation = _resolve_manual_observation(
        store=store,
        metric_id=metric.strip(),
        measurement_period_end=measurement_period_end,
        dimension=dimension,
    )
    if not observation.is_pinned:
        typer.echo(f"Manual observation {observation.observation_id} is not pinned for {observation.metric_id}")
        raise typer.Exit(code=0)
    store.update_observation_pin(observation.observation_id, pinned=False)
    typer.echo(f"Unpinned manual observation {observation.observation_id} for {observation.metric_id}")
    raise typer.Exit(code=0)


def _parse_iso_datetime(value: str, *, option_name: str) -> datetime:
    text = value.strip()
    if not text:
        raise typer.BadParameter(f"{option_name} must be non-empty")
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise typer.BadParameter(f"Invalid {option_name} value: {value}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_dimensions(values: list[str] | None) -> str:
    parsed: dict[str, str] = {}
    for raw_value in (values or []):
        text = raw_value.strip()
        if not text or "=" not in text:
            raise typer.BadParameter("--dimension entries must be in key=value form")
        key, raw_dimension_value = text.split("=", 1)
        key = key.strip()
        dimension_value = raw_dimension_value.strip()
        if not key or not dimension_value:
            raise typer.BadParameter("--dimension entries must be in key=value form")
        if key in parsed:
            raise typer.BadParameter(f"Duplicate --dimension key: {key}")
        parsed[key] = dimension_value
    return json.dumps(parsed, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _resolve_manual_observation(
    *,
    store: RealityStore,
    metric_id: str,
    measurement_period_end: str,
    dimension: list[str],
) -> MetricObservation:
    if not metric_id:
        raise typer.BadParameter("--metric must be non-empty")
    observation = store.find_manual_observation(
        metric_id,
        measurement_period_end=_parse_iso_datetime(measurement_period_end, option_name="--measurement-period-end"),
        dimensions_json=_parse_dimensions(dimension),
    )
    if observation is None:
        raise typer.BadParameter(
            f"No manual observation found for {metric_id} at {measurement_period_end} with the requested dimensions"
        )
    return observation