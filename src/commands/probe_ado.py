from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import re

import typer

from src.core.ado_client import ADOClient
from src.core.config_loader import load_report_config
from src.core.query_builder import build_odata_filter


_SINCE_PATTERN = re.compile(r"^(?P<value>\d+)d$", re.IGNORECASE)


def probe_ado(
    area: str = typer.Option(..., "--area", help="ADO area path to probe."),
    since: str = typer.Option("14d", "--since", help="Relative lookback window, for example 14d."),
    edition: str = typer.Option(
        "",
        "--edition",
        help="Edition used for organization, project, and work item type defaults.",
    ),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    config = load_report_config(edition)
    since_datetime = _parse_since(since)
    client = ADOClient(
        organization=config.ado.organization,
        project=config.ado.project,
        timeout=config.ado.api_timeout_seconds or 30,
    )
    scope_matches = client.find_area_scope_matches(area)
    if not scope_matches:
        suggestions = client.suggest_area_paths(area)
        payload = {
            "area": area,
            "auth_method": client.auth_method,
            "diagnosis": "No analytics descendants found under requested prefix",
            "edition": edition,
            "result": "No exact analytics area-path match found",
            "scope_matches": [],
            "since": since,
            "suggestions": list(suggestions[:10]),
            "total": 0,
            "work_item_type_counts": [],
        }
        if format == "human":
            typer.echo(f"Auth\t{client.auth_method}")
            typer.echo(f"Area\t{area}")
            typer.echo("Result\tNo exact analytics area-path match found")
            typer.echo("Diagnosis\tNo analytics descendants found under requested prefix")
            if suggestions:
                typer.echo("Suggestions")
                for suggestion in suggestions[:10]:
                    typer.echo(suggestion)
        else:
            typer.echo(render_probe_ado_output(payload, format=format), nl=False)
        raise typer.Exit(code=2)

    filter_expression = build_odata_filter(
        area_paths=[area],
        work_item_types=config.ado.work_item_types,
        since=since_datetime,
        states_excluded=config.ado.excluded_states,
    )
    items = client.query_work_items(filter_expression)
    counts = Counter(item["WorkItemType"] for item in items)
    sample_ids = [int(item["WorkItemId"]) for item in items[:5]]
    if sample_ids:
        client.probe_rest_batch(sample_ids)

    payload = {
        "area": area,
        "auth_method": client.auth_method,
        "diagnosis": None,
        "edition": edition,
        "filter_expression": filter_expression,
        "result": "ok",
        "scope_matches": list(scope_matches),
        "since": since,
        "suggestions": [],
        "total": len(items),
        "work_item_type_counts": [
            {"count": count, "work_item_type": work_item_type}
            for work_item_type, count in sorted(counts.items())
        ],
    }

    if format == "human":
        typer.echo(f"Auth\t{client.auth_method}")
        typer.echo(f"Area\t{area}")
        typer.echo(f"Since\t{since}")
        for work_item_type, count in sorted(counts.items()):
            typer.echo(f"{work_item_type}\t{count}")
        typer.echo(f"Total\t{len(items)}")
    else:
        typer.echo(render_probe_ado_output(payload, format=format), nl=False)


def render_probe_ado_output(payload: dict[str, object], *, format: str) -> str:
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("entry_type", "auth_method", "edition", "area", "since", "work_item_type", "count", "detail"))
        writer.writerow(("summary", payload["auth_method"], payload["edition"], payload["area"], payload["since"], None, payload["total"], payload["result"]))
        for entry in payload["work_item_type_counts"]:  # type: ignore[attr-defined]
            writer.writerow(("work_item_type", payload["auth_method"], payload["edition"], payload["area"], payload["since"], entry["work_item_type"], entry["count"], None))
        for suggestion in payload["suggestions"]:  # type: ignore[attr-defined]
            writer.writerow(("suggestion", payload["auth_method"], payload["edition"], payload["area"], payload["since"], None, None, suggestion))
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def _parse_since(value: str) -> datetime:
    match = _SINCE_PATTERN.fullmatch(value.strip())
    if match is None:
        raise typer.BadParameter("since must use Nd format, for example 14d")
    days = int(match.group("value"))
    return datetime.now(timezone.utc) - timedelta(days=days)
