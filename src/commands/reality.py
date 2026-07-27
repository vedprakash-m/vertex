from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timezone
import json
from pathlib import Path
from typing import Any, Literal

import typer

from src.commands.hypothesis import append_confirmation_event
from src.core.ado_client import ADOClient
from src.core.delivery_date_evaluator import DeliveryDateSnapshot
from src.core.edition_resolver import PROGRAMS_ROOT, get_program_output_root, load_program
from src.core.hypothesis_models import ChallengeState, Hypothesis, HypothesisStatus, RealityChallenge
from src.core.metric_models import MetricDefinition
from src.core.metric_registry import load_metric_definition_map
from src.core.reality_reconciler import reconcile_reality
from src.core.reality_store import RealityStore
from src.core.source_models import MaintenanceWindow
from src.render.reality_digest_renderer import read_digest_payload, render_reality_digest_json, render_reality_digest_text


app = typer.Typer(help="Inspect and act on L1 reality state.")
maintenance_app = typer.Typer(help="Author maintenance windows for reality suppression.")
app.add_typer(maintenance_app, name="maintenance")

_SCOPE_KIND_MAP: dict[str, Literal["program", "metric", "binding", "workstream"]] = {
    "program": "program",
    "metric": "metric",
    "binding": "binding",
    "workstream": "workstream",
}


@app.command("pending-review")
def reality_pending_review_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    interactive: bool = typer.Option(False, "--interactive", help="Prompt through proposed hypotheses one at a time."),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Reviewer alias used for accept or reject actions."),
    format: str = typer.Option("text", "--format", help="Output format when not interactive: text or json."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    normalized_program = program.strip()
    if not normalized_program:
        raise typer.BadParameter("--program must be non-empty")

    store = RealityStore(normalized_program, db_root=db_root)
    store.initialize()
    hypotheses = store.list_hypotheses(statuses=(HypothesisStatus.PROPOSED,))
    if not hypotheses:
        typer.echo("No proposed hypotheses pending review.")
        raise typer.Exit(code=0)

    if interactive:
        accepted, rejected, skipped = _review_pending_hypotheses_interactive(
            store,
            hypotheses,
            reviewer=_normalize_reviewer(reviewer),
        )
        typer.echo(f"Pending review complete: accepted={accepted}, rejected={rejected}, skipped={skipped}")
        raise typer.Exit(code=0)

    normalized_format = format.strip().lower()
    if normalized_format not in {"text", "json"}:
        raise typer.BadParameter("--format must be one of: text, json")
    if normalized_format == "json":
        typer.echo(_render_pending_review_json(hypotheses))
    else:
        typer.echo(_render_pending_review_text(hypotheses))
    raise typer.Exit(code=0)


@app.command("digest")
def reality_digest_command(
    program: str | None = typer.Option(None, "--program", help="Program id, e.g. myprogram."),
    all_programs: bool = typer.Option(False, "--all-programs", help="Aggregate reality digests across all configured programs."),
    refresh: bool = typer.Option(False, "--refresh", help="Recompute the digest before rendering it."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    db_root: Path | None = typer.Option(None, hidden=True),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
) -> None:
    normalized_format = format.strip().lower()
    if normalized_format not in {"text", "json"}:
        raise typer.BadParameter("--format must be one of: text, json")

    normalized_program = program.strip() if isinstance(program, str) and program.strip() else None
    if all_programs:
        if normalized_program is not None:
            raise typer.BadParameter("--program cannot be combined with --all-programs")
        program_ids = _discover_program_ids(programs_root)
        if not program_ids:
            raise typer.BadParameter(f"No programs with program.yaml found under {programs_root}")
        payload = _build_reality_digest_rollup(
            program_ids=program_ids,
            refresh=refresh,
            normalized_format=normalized_format,
            db_root=db_root,
            programs_root=programs_root,
        )
        if normalized_format == "json":
            typer.echo(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        else:
            typer.echo(_render_reality_digest_rollup_text(payload))
        raise typer.Exit(code=0)

    if normalized_program is None:
        raise typer.BadParameter("--program is required unless --all-programs is used")

    store = RealityStore(normalized_program, db_root=db_root)
    store.initialize()
    cache_row = store.read_digest_cache_row()
    if refresh or cache_row is None:
        as_of = datetime.now(timezone.utc)
        reconcile_reality(
            store=store,
            as_of=as_of,
            l1_observations_written=0,
            delivery_date_snapshot_provider=_build_delivery_date_snapshot_provider(normalized_program, programs_root=programs_root),
            metric_definitions_by_id=_build_metric_definition_map(as_of=as_of),
            expected_gather_cadence_hours=_load_expected_gather_cadence_hours(normalized_program, programs_root=programs_root),
        )
        cache_row = store.read_digest_cache_row()
        if cache_row is None:
            typer.echo("No digest available after refresh.")
            raise typer.Exit(code=1)

    payload_json = read_digest_payload(cache_row)
    if normalized_format == "json":
        typer.echo(render_reality_digest_json(payload_json))
    else:
        typer.echo(render_reality_digest_text(payload_json))
    raise typer.Exit(code=0)


def _build_reality_digest_rollup(
    *,
    program_ids: tuple[str, ...],
    refresh: bool,
    normalized_format: str,
    db_root: Path | None,
    programs_root: Path,
) -> dict[str, object]:
    del normalized_format
    program_payloads: list[dict[str, Any]] = []
    for program_id in program_ids:
        store = RealityStore(program_id, db_root=db_root)
        store.initialize()
        cache_row = store.read_digest_cache_row()
        if refresh or cache_row is None:
            as_of = datetime.now(timezone.utc)
            reconcile_reality(
                store=store,
                as_of=as_of,
                l1_observations_written=0,
                delivery_date_snapshot_provider=_build_delivery_date_snapshot_provider(program_id, programs_root=programs_root),
                metric_definitions_by_id=_build_metric_definition_map(as_of=as_of),
                expected_gather_cadence_hours=_load_expected_gather_cadence_hours(program_id, programs_root=programs_root),
            )
            cache_row = store.read_digest_cache_row()
        if cache_row is None:
            continue
        payload = json.loads(read_digest_payload(cache_row))
        program_payloads.append(payload)

    as_of_values = [str(payload["as_of"]) for payload in program_payloads]
    health_counts: dict[str, int] = {}
    total_confirmed = 0
    total_challenged = 0
    total_stale = 0
    total_proposed = 0
    total_open_challenges = 0
    for payload in program_payloads:
        health = str(payload["health"])
        health_counts[health] = health_counts.get(health, 0) + 1
        total_confirmed += int(payload.get("confirmed_count", 0))
        total_challenged += int(payload.get("challenged_count", 0))
        total_stale += int(payload.get("stale_count", 0))
        total_proposed += int(payload.get("proposed_count", 0))
        total_open_challenges += len(payload.get("open_challenges", []))

    overall_health = _rollup_health(tuple(str(payload["health"]) for payload in program_payloads))
    return {
        "scope": "all_programs",
        "program_count": len(program_payloads),
        "program_ids": [payload["program_id"] for payload in program_payloads],
        "as_of": max(as_of_values) if as_of_values else None,
        "health": overall_health,
        "health_counts": health_counts,
        "confirmed_count": total_confirmed,
        "challenged_count": total_challenged,
        "stale_count": total_stale,
        "proposed_count": total_proposed,
        "open_challenge_count": total_open_challenges,
        "programs": [
            {
                "program_id": payload["program_id"],
                "as_of": payload["as_of"],
                "health": payload["health"],
                "confirmed_count": payload.get("confirmed_count", 0),
                "challenged_count": payload.get("challenged_count", 0),
                "stale_count": payload.get("stale_count", 0),
                "proposed_count": payload.get("proposed_count", 0),
                "open_challenge_count": len(payload.get("open_challenges", [])),
            }
            for payload in sorted(program_payloads, key=lambda item: str(item["program_id"]))
        ],
    }


def _render_reality_digest_rollup_text(payload: dict[str, Any]) -> str:
    lines = [
        "Reality Digest Rollup - all programs",
        "-----------------------------------",
        f"Health: {payload['health']}",
        f"As of: {payload['as_of']}",
        (
            "Counts: "
            f"confirmed={payload['confirmed_count']} "
            f"challenged={payload['challenged_count']} "
            f"stale={payload['stale_count']} "
            f"proposed={payload['proposed_count']} "
            f"open_challenges={payload['open_challenge_count']}"
        ),
    ]
    programs = payload.get("programs", [])
    if programs:
        lines.append("")
        lines.append("Programs:")
        for entry in programs:
            lines.append(
                f"- {entry['program_id']}: {entry['health']} | confirmed={entry['confirmed_count']} | challenged={entry['challenged_count']} | stale={entry['stale_count']} | proposed={entry['proposed_count']} | open_challenges={entry['open_challenge_count']}"
            )
    return "\n".join(lines)


def _rollup_health(healths: tuple[str, ...]) -> str:
    if not healths:
        return "uninitialized"
    if any(health == "red" for health in healths):
        return "red"
    if any(health == "amber" for health in healths):
        return "amber"
    if all(health == "green" for health in healths):
        return "green"
    return "uninitialized"


def _discover_program_ids(programs_root: Path) -> tuple[str, ...]:
    program_ids: list[str] = []
    if not programs_root.exists():
        return ()
    for child in sorted(programs_root.iterdir(), key=lambda item: item.name.lower()):
        if child.is_dir() and (child / "program.yaml").exists():
            program_ids.append(child.name)
    return tuple(program_ids)


@app.command("challenges")
def reality_challenges_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    severity: str | None = typer.Option(None, "--severity", help="Optional severity filter: info, warn, alert."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    normalized_format = format.strip().lower()
    if normalized_format not in {"text", "json"}:
        raise typer.BadParameter("--format must be one of: text, json")

    normalized_severity = severity.strip().lower() if isinstance(severity, str) and severity.strip() else None
    if normalized_severity is not None and normalized_severity not in {"info", "warn", "alert"}:
        raise typer.BadParameter("--severity must be one of: info, warn, alert")

    store = RealityStore(program.strip(), db_root=db_root)
    store.initialize()
    challenges = store.list_open_challenges()
    if normalized_severity is not None:
        challenges = tuple(challenge for challenge in challenges if challenge.severity.value == normalized_severity)

    if normalized_format == "json":
        typer.echo(json.dumps([_serialize_reality_challenge(store, challenge) for challenge in challenges], ensure_ascii=True, indent=2))
    else:
        typer.echo(_render_reality_challenges_text(store, challenges))
    raise typer.Exit(code=0)


@app.command("snooze")
def reality_snooze_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    challenge_id: str = typer.Option(..., "--challenge-id", help="Challenge id to snooze."),
    until: str = typer.Option(..., "--until", help="Snooze-until date or timestamp in ISO-8601."),
    reason: str = typer.Option(..., "--reason", help="Why the challenge is being snoozed."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise typer.BadParameter("--reason must be non-empty")

    snoozed_until = _parse_snoozed_until(until)
    store = RealityStore(program.strip(), db_root=db_root)
    store.initialize()

    challenge = store.get_challenge(challenge_id.strip())
    if challenge is None:
        raise typer.BadParameter(f"Unknown challenge id: {challenge_id}")

    store.update_challenge_state(
        challenge.id,
        ChallengeState.SNOOZED,
        datetime.now(timezone.utc),
        reason=normalized_reason,
        snoozed_until=snoozed_until,
        snooze_reason=normalized_reason,
    )
    typer.echo(f"Snoozed {challenge.id} until {snoozed_until.isoformat()}")
    raise typer.Exit(code=0)


@app.command("dismiss")
def reality_dismiss_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    challenge_id: str = typer.Option(..., "--challenge-id", help="Challenge id to dismiss."),
    reason: str = typer.Option(..., "--reason", help="Why the challenge is being dismissed."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise typer.BadParameter("--reason must be non-empty")

    store = RealityStore(program.strip(), db_root=db_root)
    store.initialize()
    challenge = store.get_challenge(challenge_id.strip())
    if challenge is None:
        raise typer.BadParameter(f"Unknown challenge id: {challenge_id}")
    if challenge.current_state not in {
        ChallengeState.OPEN,
        ChallengeState.ACKNOWLEDGED,
        ChallengeState.REOPENED,
        ChallengeState.SNOOZED,
    }:
        raise typer.BadParameter(f"Challenge {challenge.id} is not active and cannot be dismissed.")

    store.update_challenge_state(
        challenge.id,
        ChallengeState.DISMISSED,
        datetime.now(timezone.utc),
        reason=normalized_reason,
    )
    typer.echo(f"Dismissed {challenge.id}")
    raise typer.Exit(code=0)


@app.command("reopen")
def reality_reopen_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    challenge_id: str = typer.Option(..., "--challenge-id", help="Challenge id to reopen."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    store = RealityStore(program.strip(), db_root=db_root)
    store.initialize()
    challenge = store.get_challenge(challenge_id.strip())
    if challenge is None:
        raise typer.BadParameter(f"Unknown challenge id: {challenge_id}")
    if challenge.current_state not in {ChallengeState.DISMISSED, ChallengeState.SNOOZED}:
        raise typer.BadParameter(f"Challenge {challenge.id} must be dismissed or snoozed before reopening.")

    store.update_challenge_state(
        challenge.id,
        ChallengeState.REOPENED,
        datetime.now(timezone.utc),
        reason="reopened_by_pm",
        snoozed_until=None,
        snooze_reason=None,
    )
    typer.echo(f"Reopened {challenge.id}")
    raise typer.Exit(code=0)


def _parse_snoozed_until(value: str) -> datetime:
    text = value.strip()
    if not text:
        raise typer.BadParameter("--until must be non-empty")
    try:
        parsed_date = date.fromisoformat(text)
    except ValueError:
        parsed_date = None
    if parsed_date is not None:
        return datetime.combine(parsed_date, time(23, 59, 59), tzinfo=timezone.utc)

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise typer.BadParameter(f"Invalid --until value: {value}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _render_reality_challenges_text(store: RealityStore, challenges: tuple[RealityChallenge, ...]) -> str:
    if not challenges:
        return "No open challenges."
    lines = [f"Open challenges: {len(challenges)}"]
    for challenge in challenges:
        hypothesis = store.get_hypothesis(challenge.hypothesis_id)
        hypothesis_label = hypothesis.short_id if hypothesis is not None else challenge.hypothesis_id
        lines.append(
            f"- {challenge.id} | {challenge.severity.value} | {challenge.challenge_kind.value} | {hypothesis_label} | {challenge.source}"
        )
    return "\n".join(lines)


def _serialize_reality_challenge(store: RealityStore, challenge: RealityChallenge) -> dict[str, object]:
    hypothesis = store.get_hypothesis(challenge.hypothesis_id)
    return {
        "id": challenge.id,
        "hypothesis_id": challenge.hypothesis_id,
        "hypothesis_short_id": hypothesis.short_id if hypothesis is not None else None,
        "assertion_id": challenge.assertion_id,
        "challenge_kind": challenge.challenge_kind.value,
        "severity": challenge.severity.value,
        "source": challenge.source,
        "current_state": challenge.current_state.value,
        "detected_at": challenge.detected_at.isoformat(),
        "note": challenge.note,
    }


def _review_pending_hypotheses_interactive(
    store: RealityStore,
    hypotheses: tuple[Hypothesis, ...],
    *,
    reviewer: str,
) -> tuple[int, int, int]:
    accepted = 0
    rejected = 0
    skipped = 0
    for index, hypothesis in enumerate(hypotheses):
        typer.echo("")
        typer.echo(f"{hypothesis.short_id} | {hypothesis.kind.value} | {hypothesis.statement}")
        if hypothesis.expected_value is not None:
            typer.echo(f"Expected: {hypothesis.expected_value}")
        if hypothesis.telemetry_assertion_id is not None:
            typer.echo(f"Assertion: {hypothesis.telemetry_assertion_id}")
        choice = typer.prompt("Decision [a]ccept/[r]eject/[s]kip/[q]uit", default="s").strip().lower()
        if choice == "q":
            skipped += len(hypotheses) - index
            break
        if choice == "a":
            _accept_hypothesis(store, hypothesis, reviewer=reviewer)
            typer.echo(f"Accepted {hypothesis.short_id}")
            accepted += 1
            continue
        if choice == "r":
            rejection_reason = typer.prompt("Reject reason", default="", show_default=False).strip()
            if not rejection_reason:
                raise typer.BadParameter("Reject reason must be non-empty")
            _reject_hypothesis(store, hypothesis, reviewer=reviewer, rejection_reason=rejection_reason)
            typer.echo(f"Rejected {hypothesis.short_id}")
            rejected += 1
            continue
        skipped += 1
    return accepted, rejected, skipped


def _accept_hypothesis(store: RealityStore, hypothesis: Hypothesis, *, reviewer: str) -> None:
    now = datetime.now(timezone.utc)
    store.upsert_hypothesis(
        replace(
            hypothesis,
            status=HypothesisStatus.CONFIRMED,
            confirmed_by=reviewer,
            confirmed_at=now,
            rejection_reason=None,
        )
    )
    store.set_hypothesis_state(
        hypothesis.id,
        HypothesisStatus.CONFIRMED,
        now,
        actor=reviewer,
        reason="pending_review_accept",
    )
    confirmed = store.get_hypothesis(hypothesis.id)
    if confirmed is not None:
        append_confirmation_event(
            store,
            event_type="confirmed",
            hypothesis=confirmed,
            actor=reviewer,
            recorded_at=now,
            reason="pending_review_accept",
        )


def _reject_hypothesis(
    store: RealityStore,
    hypothesis: Hypothesis,
    *,
    reviewer: str,
    rejection_reason: str,
) -> None:
    now = datetime.now(timezone.utc)
    store.upsert_hypothesis(
        replace(
            hypothesis,
            status=HypothesisStatus.REJECTED,
            rejection_reason=rejection_reason,
        )
    )
    store.set_hypothesis_state(
        hypothesis.id,
        HypothesisStatus.REJECTED,
        now,
        actor=reviewer,
        reason=f"pending_review_reject:{rejection_reason}",
    )


def _render_pending_review_text(hypotheses: tuple[Hypothesis, ...]) -> str:
    lines = [f"Proposed hypotheses pending review: {len(hypotheses)}"]
    for hypothesis in hypotheses:
        line = f"- {hypothesis.short_id} | {hypothesis.kind.value} | {hypothesis.statement}"
        details: list[str] = []
        if hypothesis.expected_value is not None:
            details.append(f"expected={hypothesis.expected_value}")
        if hypothesis.telemetry_assertion_id is not None:
            details.append(f"assertion={hypothesis.telemetry_assertion_id}")
        if details:
            line += " | " + ", ".join(details)
        lines.append(line)
    return "\n".join(lines)


def _render_pending_review_json(hypotheses: tuple[Hypothesis, ...]) -> str:
    payload = []
    for hypothesis in hypotheses:
        payload.append(
            {
                "id": hypothesis.id,
                "short_id": hypothesis.short_id,
                "kind": hypothesis.kind.value,
                "statement": hypothesis.statement,
                "expected_value": hypothesis.expected_value,
                "telemetry_assertion_id": hypothesis.telemetry_assertion_id,
                "linked_ado_item_id": hypothesis.linked_ado_item_id,
                "review_due": hypothesis.review_due.isoformat() if hypothesis.review_due is not None else None,
            }
        )
    return json.dumps(payload, ensure_ascii=True, indent=2)


def _normalize_reviewer(value: str | None) -> str:
    if value is None or not value.strip():
        return "vertex/reality_pending_review"
    return value.strip()


def _parse_iso_datetime(value: str, *, end_of_day: bool, option_name: str) -> datetime:
    text = value.strip()
    if not text:
        raise typer.BadParameter(f"{option_name} must be non-empty")
    try:
        parsed_date = date.fromisoformat(text)
    except ValueError:
        parsed_date = None
    if parsed_date is not None:
        clock = time(23, 59, 59) if end_of_day else time(0, 0, 0)
        return datetime.combine(parsed_date, clock, tzinfo=timezone.utc)

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise typer.BadParameter(f"Invalid {option_name} value: {value}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

@maintenance_app.command("schedule")
def reality_maintenance_schedule_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    title: str = typer.Option(..., "--title", help="Maintenance window title."),
    starts_at: str = typer.Option(..., "--starts-at", help="Window start in ISO-8601."),
    ends_at: str = typer.Option(..., "--ends-at", help="Window end in ISO-8601."),
    scope_kind: str = typer.Option("program", "--scope-kind", help="One of: program, metric, binding, workstream."),
    scope_value: str = typer.Option("*", "--scope-value", help="Scope value for non-program windows."),
    reference: str | None = typer.Option(None, "--reference", help="Optional change or incident reference."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    normalized_title = title.strip()
    if not normalized_title:
        raise typer.BadParameter("--title must be non-empty")
    normalized_scope_kind = scope_kind.strip().lower()
    if normalized_scope_kind not in {"program", "metric", "binding", "workstream"}:
        raise typer.BadParameter("--scope-kind must be one of: program, metric, binding, workstream")

    starts = _parse_iso_datetime(starts_at, end_of_day=False, option_name="--starts-at")
    ends = _parse_iso_datetime(ends_at, end_of_day=False, option_name="--ends-at")
    if ends <= starts:
        raise typer.BadParameter("--ends-at must be after --starts-at")
    if normalized_scope_kind != "program" and not scope_value.strip():
        raise typer.BadParameter("--scope-value must be non-empty for non-program windows")

    store = RealityStore(program.strip(), db_root=db_root)
    store.initialize()
    window = MaintenanceWindow(
        id=f"mw-{int(datetime.now(timezone.utc).timestamp() * 1000000)}",
        program_id=program.strip(),
        title=normalized_title,
        starts_at=starts,
        ends_at=ends,
        scope_kind=_SCOPE_KIND_MAP[normalized_scope_kind],
        scope_value="*" if normalized_scope_kind == "program" else scope_value.strip(),
        created_by="vertex/reality",
        created_at=datetime.now(timezone.utc),
        reference=reference.strip() if isinstance(reference, str) and reference.strip() else None,
    )
    store.upsert_maintenance_window(window)
    typer.echo(f"Scheduled maintenance window {window.id} for {window.program_id}")
    raise typer.Exit(code=0)


@app.command("status")
def reality_status_command(
    program: str | None = typer.Option(None, "--program", help="Program id, e.g. myprogram."),
    all_programs: bool = typer.Option(False, "--all-programs", help="Show status for all programs (fleet default, WI-5.1)."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    force: bool = typer.Option(False, "--force", help="Override advisory (QG-27, forceable) gates."),
    db_root: Path | None = typer.Option(None, hidden=True),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
) -> None:
    """Show truth-level status for a program and run QG-27 gate check (WI-5.1 / WI-3.9)."""
    from src.core.program_fact_store import load_program_facts
    from src.core.truth_model import build_trust_context_from_snapshot, derive_truth_level as _derive
    from src.core.quality_gates.qg27 import QG27Input, evaluate_qg27
    from src.commands.reality_checks import run_reality_checks
    from src.core.fact_sor_state import load_fact_sor_state

    normalized_format = format.strip().lower()
    if normalized_format not in {"text", "json"}:
        raise typer.BadParameter("--format must be one of: text, json")

    normalized_program = program.strip() if isinstance(program, str) and program.strip() else None

    if all_programs:
        if normalized_program is not None:
            raise typer.BadParameter("--program cannot be combined with --all-programs")
        program_ids = _discover_program_ids(programs_root)
        if not program_ids:
            raise typer.BadParameter(f"No programs found under {programs_root}")
        fleet_payload = _build_fleet_status_payload(
            program_ids=program_ids,
            programs_root=programs_root,
            db_root=db_root,
        )
        if normalized_format == "json":
            typer.echo(json.dumps(fleet_payload, ensure_ascii=True, indent=2, sort_keys=True))
        else:
            typer.echo(
                "Fleet Reality Status | "
                f"programs={fleet_payload['program_count']} | "
                f"attention={fleet_payload['attention_count']} | "
                f"open_conflicts={fleet_payload['open_conflict_count']} | "
                f"pending_actuations={fleet_payload['pending_actuation_count']}"
            )
            for p in fleet_payload["programs"]:
                if "error" in p:
                    typer.echo(f"  {p['program_id']}: ERROR - {p['error']}")
                else:
                    qg = p.get("qg27", {})
                    typer.echo(
                        f"  {p['program_id']}: facts={p.get('fact_count', '?')} | "
                        f"mode={p.get('sor_mode', '?')} | "
                        f"misses_7d={p.get('ask_miss_count_7d', '?')} | "
                        f"sync_executed={p.get('executed_sync_count', '?')} | "
                        f"qg27={'PASS' if qg.get('passed') else 'FAIL'}"
                    )
        raise typer.Exit(code=0)

    if normalized_program is None:
        raise typer.BadParameter("--program is required (or use --all-programs for fleet overview)")

    as_of = datetime.now(timezone.utc)
    payload = _build_program_status_payload(
        normalized_program,
        programs_root=programs_root,
        db_root=db_root,
    )

    if normalized_format == "json":
        typer.echo(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        qg = payload.get("qg27", {})
        reality_check_lines = [
            f"  [{rc['check_id']}] {rc['status'].upper()}: {rc['message']}"
            for rc in payload.get("reality_checks", [])
        ]
        o16 = payload.get("o16_contradiction_summary", {})
        o16_str = (
            f"contradictions={o16.get('total_contradictions', 0)} | "
            f"suspended_sources={o16.get('suspended_source_count', 0)}"
        )
        lines = [
            f"Reality Status - {payload['program_id']}",
            f"As of: {payload['as_of']}",
            f"SoR mode: {payload.get('sor_mode', 'legacy')}",
            (
                f"Facts: {payload['fact_count']} total | "
                f"auto_approved={payload['auto_approved_count']} | "
                f"provisional={payload['provisional_count']} | "
                f"material_conflicts={payload['material_conflict_count']}"
            ),
            "Truth levels: " + " | ".join(
                f"{k}={v}" for k, v in sorted(payload.get("truth_level_counts", {}).items())
            ),
            f"O-16 regret: {o16_str}",
            f"Executed syncs: {payload.get('executed_sync_count', 0)} | "
            f"Ask misses (7d): {payload.get('ask_miss_count_7d', 0)}",
            f"QG-27: {'PASSED' if qg.get('passed') else 'FAILED'} - {qg.get('message', '')}",
        ]
        if reality_check_lines:
            lines.append("Reality checks:")
            lines.extend(reality_check_lines)
        output = "\n".join(lines)
        _UNICODE_REPLACEMENTS = str.maketrans({"—": "-", "–": "-", "→": "->", "←": "<-", "≤": "<=", "≥": ">=", "§": "sec.", "✓": "OK", "✗": "FAIL", "✔": "OK", "✘": "FAIL"})
        typer.echo(output.translate(_UNICODE_REPLACEMENTS))

    qg_result_passed = payload.get("qg27", {}).get("passed", True)
    qg_result_forceable = payload.get("qg27", {}).get("forceable", False)
    qg_exit_code = payload.get("qg27", {}).get("exit_code", 0)

    if not qg_result_passed:
        if qg_result_forceable and force:
            typer.echo("[QG-27] Advisory gate overridden with --force.")
            raise typer.Exit(code=0)
        raise typer.Exit(code=qg_exit_code)
    raise typer.Exit(code=0)


@app.command("explain")
def reality_explain_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    fact_id: str = typer.Option(..., "--fact-id", help="Fact id to explain."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
) -> None:
    """Explain why one fact is believed, disputed, or provisional."""
    from src.core.program_reality import ProgramReality

    normalized_program = program.strip()
    normalized_fact_id = fact_id.strip()
    normalized_format = format.strip().lower()

    if not normalized_program:
        raise typer.BadParameter("--program must be non-empty")
    if not normalized_fact_id:
        raise typer.BadParameter("--fact-id must be non-empty")
    if normalized_format not in {"text", "json"}:
        raise typer.BadParameter("--format must be one of: text, json")

    reality = ProgramReality.load(normalized_program, programs_root=programs_root)
    explanation = reality.explain(normalized_fact_id)
    if explanation is None:
        raise typer.BadParameter(f"Unknown fact id: {normalized_fact_id}")

    payload = {
        "program_id": explanation.program_id,
        "fact_id": explanation.fact_id,
        "fact_type": explanation.fact_type,
        "natural_key": explanation.natural_key,
        "truth_level": explanation.truth_level.value,
        "disputed": explanation.disputed,
        "stale": explanation.stale,
        "provisional_inputs": explanation.provisional_inputs,
        "source_signal_ids": list(explanation.source_signal_ids),
        "entity_refs": list(explanation.entity_refs),
        "evidence": [
            {
                "signal_id": ref.signal_id,
                "entity_ref": ref.entity_ref,
                "source": ref.source,
            }
            for ref in explanation.evidence
        ],
        "open_conflicts": [
            {
                "conflict_id": conflict.conflict_id,
                "entity_refs": list(conflict.entity_refs),
                "family": conflict.family,
                "open": conflict.open,
                "description": conflict.description,
                "winning_source": conflict.winning_source,
                "losing_source": conflict.losing_source,
                "winning_value": conflict.winning_value,
                "losing_value": conflict.losing_value,
                "resolution": conflict.resolution,
                "detected_at": conflict.detected_at,
            }
            for conflict in explanation.open_conflicts
        ],
    }

    if normalized_format == "json":
        typer.echo(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        lines = [
            f"Reality Explain - {explanation.program_id}",
            f"Fact: {explanation.fact_id} ({explanation.fact_type})",
            f"Natural key: {explanation.natural_key}",
            (
                f"Truth: {explanation.truth_level.value} | disputed={str(explanation.disputed).lower()} | "
                f"stale={str(explanation.stale).lower()} | provisional_inputs={str(explanation.provisional_inputs).lower()}"
            ),
            f"Signals: {', '.join(explanation.source_signal_ids) if explanation.source_signal_ids else '-'}",
            f"Entities: {', '.join(explanation.entity_refs) if explanation.entity_refs else '-'}",
            f"Open conflicts: {len(explanation.open_conflicts)}",
        ]
        for conflict in explanation.open_conflicts:
            lines.append(
                f"- {conflict.conflict_id} | family={conflict.family} | {conflict.description}"
            )
            if conflict.winning_source or conflict.losing_source:
                lines.append(
                    f"    {conflict.winning_source or '?'} ({conflict.winning_value or '?'}) beat "
                    f"{conflict.losing_source or '?'} ({conflict.losing_value or '?'})"
                    + (f" -- {conflict.resolution}" if conflict.resolution else "")
                )
        typer.echo("\n".join(lines))


def _build_program_status_payload(
    program_id: str,
    *,
    programs_root: Path,
    db_root: Path | None,
) -> dict[str, Any]:
    """Build the full WI-5.1 status payload for one program."""
    from src.core.program_fact_store import load_program_facts
    from src.core.truth_model import build_trust_context_from_snapshot, derive_truth_level as _derive
    from src.core.quality_gates.qg27 import QG27Input, evaluate_qg27
    from src.commands.reality_checks import run_reality_checks
    from src.core.fact_sor_state import load_fact_sor_state

    as_of = datetime.now(timezone.utc)
    snapshot = load_program_facts(program_id, programs_root=programs_root, db_root=db_root)
    truth_ctx = build_trust_context_from_snapshot(snapshot)

    truth_levels = {
        fact.natural_key: _derive(fact, truth_ctx)
        for fact in snapshot.facts
    }

    qg_result = evaluate_qg27(QG27Input(snapshot=snapshot, truth_levels=truth_levels))

    counts: dict[str, int] = {}
    for tl in truth_levels.values():
        counts[tl.value] = counts.get(tl.value, 0) + 1

    auto_approved = sum(
        1 for f in snapshot.facts
        if hasattr(f, "review_state") and str(f.review_state) == "accepted"
    )
    provisional = sum(
        1 for f in snapshot.facts
        if hasattr(f, "review_state") and str(f.review_state) == "proposed"
    )
    material_conflicts = sum(
        1 for f in snapshot.facts
        if f.fact_type == "fact.conflict"
        and f.payload.get("is_material", False)
        and not f.payload.get("resolved", False)
    )

    # WI-5.1: O-16 contradiction summary from trust.source_score facts.
    total_contradictions = sum(
        int(f.payload.get("contradiction_count", 0))
        for f in snapshot.facts
        if f.fact_type == "trust.source_score"
    )
    suspended_source_count = sum(
        1 for f in snapshot.facts
        if f.fact_type == "trust.source_score" and f.payload.get("suspended", False)
    )

    # WI-5.1: Executed sync count (fact.source_sync events in snapshot).
    executed_sync_count = sum(1 for f in snapshot.facts if f.fact_type == "fact.source_sync")

    # WI-5.1: Ask-miss count from last 7 days.
    ask_miss_count_7d = _count_ask_misses_7d(program_id=program_id, programs_root=programs_root)

    # WI-5.1: SoR mode.
    sor_state = load_fact_sor_state(program_id, programs_root=programs_root)
    sor_mode = sor_state.mode if sor_state is not None else "legacy"
    family_modes = dict(sor_state.family_modes) if sor_state is not None else {}

    # WI-5.1: Reality checks.
    checks = run_reality_checks(program_id, programs_root=programs_root, as_of=as_of)

    return {
        "program_id": program_id,
        "as_of": as_of.isoformat(),
        "sor_mode": sor_mode,
        "family_modes": family_modes,
        "fact_count": len(snapshot.facts),
        "auto_approved_count": auto_approved,
        "provisional_count": provisional,
        "material_conflict_count": material_conflicts,
        "truth_level_counts": counts,
        "o16_contradiction_summary": {
            "total_contradictions": total_contradictions,
            "suspended_source_count": suspended_source_count,
        },
        "executed_sync_count": executed_sync_count,
        "ask_miss_count_7d": ask_miss_count_7d,
        "qg27": {
            "passed": qg_result.passed,
            "exit_code": qg_result.exit_code,
            "message": qg_result.message,
            "forceable": qg_result.forceable,
        },
        "reality_checks": [
            {
                "check_id": rc.check_id,
                "status": rc.status,
                "message": rc.message,
                "details": rc.details,
            }
            for rc in checks
        ],
    }


def _build_fleet_status_payload(
    *,
    program_ids: tuple[str, ...],
    programs_root: Path,
    db_root: Path | None,
) -> dict[str, Any]:
    from src.core.program_reality import FleetReality, ProgramReality

    loaded_programs = []
    program_payloads: list[dict[str, Any]] = []
    for program_id in program_ids:
        try:
            loaded_programs.append(ProgramReality.load(program_id, programs_root=programs_root))
            program_payloads.append(
                _build_program_status_payload(
                    program_id,
                    programs_root=programs_root,
                    db_root=db_root,
                )
            )
        except Exception as exc:
            program_payloads.append({"program_id": program_id, "error": str(exc)})

    fleet = FleetReality(tuple(loaded_programs))
    return {
        "scope": "fleet_status",
        "program_count": len(program_ids),
        "loaded_program_count": len(loaded_programs),
        "program_ids": list(program_ids),
        "attention_count": len(fleet.attention()),
        "open_conflict_count": len(fleet.conflicts(open_only=True)),
        "pending_actuation_count": len(fleet.pending_actuations()),
        "freshness_record_count": len(fleet.freshness()),
        "programs": program_payloads,
    }


def _count_ask_misses_7d(*, program_id: str, programs_root: Path) -> int:
    """Count ask-miss log entries from the last 7 days (WI-5.1)."""
    from datetime import timedelta
    from src.core.jsonl_utils import parse_jsonl_line
    miss_log = get_program_output_root(program_id, programs_root=programs_root) / "ask_misses.jsonl"
    if not miss_log.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    count = 0
    try:
        for line in miss_log.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = parse_jsonl_line(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if not isinstance(entry, dict):
                continue
            logged_at_raw = entry.get("logged_at", "")
            if not logged_at_raw:
                count += 1  # no timestamp → count conservatively
                continue
            try:
                logged_at = datetime.fromisoformat(str(logged_at_raw).replace("Z", "+00:00"))
            except ValueError:
                count += 1
                continue
            if logged_at.tzinfo is None:
                logged_at = logged_at.replace(tzinfo=timezone.utc)
            if logged_at >= cutoff:
                count += 1
    except OSError:
        return 0
    return count


def _build_delivery_date_snapshot_provider(program_id: str, *, programs_root: Path = PROGRAMS_ROOT):
    program = load_program(program_id, programs_root=programs_root)
    if program is None or program.ado is None:
        return None

    ado_config = program.ado
    client: ADOClient | None = None

    def _load_snapshot(work_item_id: int) -> DeliveryDateSnapshot | None:
        nonlocal client
        if client is None:
            client = ADOClient(
                organization=ado_config.organization,
                project=ado_config.project,
                timeout=ado_config.api_timeout_seconds,
                show_progress=False,
            )
        rows = client.get_work_items(
            [work_item_id],
            fields=(
                "System.Id",
                "System.State",
                "Microsoft.VSTS.Scheduling.TargetDate",
                "Microsoft.VSTS.Common.ClosedDate",
            ),
        )
        if not rows:
            return None
        row = rows[0]
        raw_fields = row.get("fields")
        fields = raw_fields if isinstance(raw_fields, dict) else {}
        return DeliveryDateSnapshot(
            work_item_id=int(fields.get("System.Id") or work_item_id),
            state=str(fields.get("System.State") or "Active"),
            target_date=_parse_optional_ado_date(fields.get("Microsoft.VSTS.Scheduling.TargetDate")),
            closed_at=_parse_optional_ado_datetime(fields.get("Microsoft.VSTS.Common.ClosedDate")),
        )

    return _load_snapshot


def _build_metric_definition_map(*, as_of: datetime) -> dict[str, MetricDefinition]:
    return load_metric_definition_map(as_of=as_of)


def _load_expected_gather_cadence_hours(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> float | None:
    program = load_program(program_id, programs_root=programs_root)
    if program is None:
        return None
    return program.expected_gather_cadence_hours


def _parse_optional_ado_date(value: object) -> date | None:
    parsed = _parse_optional_ado_datetime(value)
    if parsed is None:
        return None
    return parsed.date()


def _parse_optional_ado_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            return datetime.combine(date.fromisoformat(text[:10]), time(0, 0, 0), tzinfo=timezone.utc)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# WI-7.4: `vertex reality export`  (§6.12.2)
# ---------------------------------------------------------------------------

_EXPORT_MAX_TIMESERIES_FRAMES_DEFAULT = 60  # policy cap (§6.12.2)


@app.command("export")
def reality_export_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON envelope."),
    timeseries: bool = typer.Option(False, "--timeseries", help="Emit an array of historical frames using as_of replay."),
    interval: int = typer.Option(7, "--interval", help="Days between frames for --timeseries."),
    since: str | None = typer.Option(None, "--since", help="Earliest date (ISO) for --timeseries. Defaults to 60 * interval days ago."),
    max_frames: int = typer.Option(_EXPORT_MAX_TIMESERIES_FRAMES_DEFAULT, "--max-frames", hidden=True),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
    actor: str = typer.Option("cli", "--actor", hidden=True, help="Who is performing the export (audit)."),
) -> None:
    """Export program reality as a versioned JSON envelope.

    Without --timeseries: exports the current snapshot.
    With --timeseries: exports an array of historical frames.

    Every export appends to the edition-scoped audit log and writes a
    cursor manifest at publications/<program>/reality_export_cursor.json.
    """
    normalized_program = program.strip()
    if not normalized_program:
        raise typer.BadParameter("--program must be non-empty")

    if timeseries:
        payload = _build_timeseries_export(
            program_id=normalized_program,
            interval_days=interval,
            since_str=since,
            max_frames=max_frames,
            programs_root=programs_root,
        )
    else:
        from src.core.program_reality import ProgramReality
        reality = ProgramReality.load(normalized_program, programs_root=programs_root)
        payload = reality.to_dict()

    # Write cursor manifest (per-program, not shared across programs)
    cursor_dir = get_program_output_root(normalized_program, programs_root=programs_root)
    cursor_dir.mkdir(parents=True, exist_ok=True)
    cursor_path = cursor_dir / "reality_export_cursor.json"
    _write_cursor_manifest(cursor_path, program_id=normalized_program, payload=payload)

    # Append audit event
    audit_path = cursor_dir / "reality_export_audit.jsonl"
    _append_export_audit(
        audit_path,
        program_id=normalized_program,
        actor=actor,
        timeseries=timeseries,
        max_classification=payload.get("max_classification", "internal") if isinstance(payload, dict) else "internal",
    )

    if json_out:
        typer.echo(json.dumps(payload, ensure_ascii=True, indent=2, default=str))
    else:
        typer.echo(_render_export_text(payload))


def _build_timeseries_export(
    *,
    program_id: str,
    interval_days: int,
    since_str: str | None,
    max_frames: int,
    programs_root: Path,
) -> dict[str, Any]:
    """Build a timeseries export payload (§6.12.2 --timeseries)."""
    from src.core.program_reality import ProgramReality as _ProgramReality
    from datetime import timedelta

    as_of_now = datetime.now(timezone.utc)

    if since_str is not None:
        since_dt = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
    else:
        since_dt = as_of_now - timedelta(days=interval_days * max_frames)

    # Clamp total frames to policy cap
    total_days = int((as_of_now - since_dt).total_seconds() / 86400)
    n_frames = min(max_frames, max(1, total_days // max(1, interval_days)))

    frames: list[dict[str, Any]] = []
    prev_non_replayable: frozenset[str] | None = None

    for i in range(n_frames):
        frame_as_of = as_of_now - timedelta(days=interval_days * (n_frames - 1 - i))
        reality = _ProgramReality.load(program_id, programs_root=programs_root, as_of=frame_as_of)
        frame_dict = reality.to_dict()

        # non_replayable_families per frame — diff against self gives the set
        delta = reality.diff(reality)
        current_non_replayable = frozenset(delta.non_replayable_families)

        # sor_flip_boundary: true when replayability set changes vs. previous frame (v3.2)
        sor_flip = False
        flipped_families: list[str] = []
        if prev_non_replayable is not None:
            added = sorted(current_non_replayable - prev_non_replayable)
            removed = sorted(prev_non_replayable - current_non_replayable)
            flipped_families = added + removed
            sor_flip = bool(flipped_families)

        frame_dict["non_replayable_families"] = sorted(current_non_replayable)
        if sor_flip:
            frame_dict["sor_flip_boundary"] = True
            frame_dict["sor_flip_families"] = flipped_families

        frames.append(frame_dict)
        prev_non_replayable = current_non_replayable

    return {
        "reality_schema_version": "1",
        "export_kind": "timeseries",
        "program_id": program_id,
        "interval_days": interval_days,
        "frame_count": len(frames),
        "max_frames_policy": max_frames,
        "generated_at": as_of_now.isoformat(),
        "frames": frames,
    }


def _write_cursor_manifest(path: Path, *, program_id: str, payload: dict[str, Any]) -> None:
    """Write per-edition cursor manifest (§6.12.2, explicitly per-edition)."""
    generated_at = datetime.now(timezone.utc).isoformat()
    manifest: dict[str, Any] = {
        "schema_version": "1",
        "program_id": program_id,
        "generated_at": generated_at,
        "export_kind": payload.get("export_kind", "snapshot"),
    }
    if "as_of" in payload:
        manifest["last_snapshot_as_of"] = payload["as_of"]
    if "frame_count" in payload:
        manifest["frame_count"] = payload["frame_count"]
    path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2), encoding="utf-8")


def _append_export_audit(
    path: Path,
    *,
    program_id: str,
    actor: str,
    timeseries: bool,
    max_classification: str,
) -> None:
    """Append a journal event for each export (§6.12.2 audit)."""
    from src.core.jsonl_utils import append_jsonl_line
    entry = {
        "event": "reality_export",
        "program_id": program_id,
        "actor": actor,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "scope": "timeseries" if timeseries else "snapshot",
        "classification_ceiling": max_classification,
    }
    append_jsonl_line(path, json.dumps(entry, ensure_ascii=True) + "\n")


def _render_export_text(payload: Any) -> str:
    """Human-readable summary of an export payload."""
    if not isinstance(payload, dict):
        return str(payload)
    if payload.get("export_kind") == "timeseries":
        return (
            f"Timeseries export: {payload.get('program_id')} | "
            f"{payload.get('frame_count')} frames | "
            f"interval={payload.get('interval_days')}d | "
            f"generated={payload.get('generated_at', '?')}\n"
            "Use --json for full output."
        )
    return (
        f"Reality export: {payload.get('program_id')} | "
        f"as_of={payload.get('as_of', '?')} | "
        f"sor_mode={payload.get('sor_mode', '?')} | "
        f"schema_version={payload.get('reality_schema_version', '?')}\n"
        "Use --json for full output."
    )
