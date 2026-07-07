from __future__ import annotations

from dataclasses import asdict, replace
from typing import Literal
from datetime import date, datetime, timezone
from enum import Enum
import getpass
import json
import os
from pathlib import Path
from uuid import uuid4

import portalocker
import typer

from src.core.hypothesis_models import (
    AssertionEvaluation,
    CompositeAssertion,
    ChallengeKind,
    ChallengeSeverity,
    ChallengeState,
    Hypothesis,
    HypothesisAnnotation,
    HypothesisKind,
    HypothesisStatus,
    RealityChallenge,
    TelemetryAssertion,
)
from src.core.metric_models import MetricObservation
from src.core.journal import PROGRAMS_ROOT
from src.core.models_v2 import Assumption, AssumptionStatus
from src.core.program_fact_store import load_program_facts, project_assumptions
from src.core.reality_store import RealityStore
from src.core.source_models import SourceKind, SourceRef


app = typer.Typer(help="Manage L1 reality hypotheses.")
annotate_app = typer.Typer(help="Attach annotation-only documents to hypotheses.")
app.add_typer(annotate_app, name="annotate")


@app.command("list")
def list_hypotheses_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    include_terminal: bool = typer.Option(False, "--include-terminal", help="Include rejected, invalidated, and superseded hypotheses."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    store = _open_store(program, db_root=db_root)
    statuses = None
    if not include_terminal:
        statuses = (
            HypothesisStatus.PROPOSED,
            HypothesisStatus.CONFIRMED,
            HypothesisStatus.CHALLENGED,
            HypothesisStatus.STALE,
        )
    hypotheses = store.list_hypotheses(statuses=statuses)
    if format == "json":
        typer.echo(json.dumps([_serialize_hypothesis(item) for item in hypotheses], ensure_ascii=True, indent=2))
        raise typer.Exit(code=0)
    if format != "human":
        raise typer.BadParameter("--format must be one of: human, json")
    if not hypotheses:
        typer.echo("No hypotheses found.")
        raise typer.Exit(code=0)
    for hypothesis in hypotheses:
        typer.echo(f"{hypothesis.short_id}\t{hypothesis.status.value}\t{hypothesis.kind.value}\t{hypothesis.statement}")
    raise typer.Exit(code=0)


@app.command("show")
def show_hypothesis_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    id: str = typer.Option(..., "--id", help="Hypothesis id or short id."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    program_id = _require_text(program, "--program")
    store = _open_store(program_id, db_root=db_root)
    hypothesis = _resolve_hypothesis(store, id)
    assertion = store.get_telemetry_assertion(hypothesis.telemetry_assertion_id) if hypothesis.telemetry_assertion_id is not None else None
    composite_assertion = store.get_composite_assertion(hypothesis.composite_assertion_id) if hypothesis.composite_assertion_id is not None else None
    observations = _load_recent_observations(store, assertion)
    latest_challenge = (
        store.get_latest_challenge_for_composite_assertion(hypothesis.id, hypothesis.composite_assertion_id)
        if hypothesis.composite_assertion_id is not None
        else store.get_latest_challenge_for_hypothesis(hypothesis.id)
    )
    lifecycle = _collect_hypothesis_lifecycle(store, hypothesis)
    annotations = store.list_hypothesis_annotations(hypothesis.id, include_archived=True)

    normalized_format = _require_text(format, "--format").lower()
    if normalized_format == "json":
        typer.echo(
            json.dumps(
                {
                    "hypothesis": _serialize_hypothesis(hypothesis),
                    "lifecycle": [_serialize_hypothesis(item) for item in lifecycle],
                    "assertion": _serialize_assertion(assertion),
                    "composite_assertion": _serialize_composite_assertion(composite_assertion),
                    "latest_challenge": _serialize_challenge(latest_challenge),
                    "recent_observations": [_serialize_metric_observation(item) for item in observations],
                    "annotations": [_serialize_annotation(item) for item in annotations],
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        raise typer.Exit(code=0)
    if normalized_format != "human":
        raise typer.BadParameter("--format must be one of: human, json")
    typer.echo(
        _render_hypothesis_show_text(
            hypothesis,
            lifecycle=lifecycle,
            assertion=assertion,
            composite_assertion=composite_assertion,
            latest_challenge=latest_challenge,
            observations=observations,
            annotations=annotations,
        )
    )
    raise typer.Exit(code=0)


@annotate_app.command("add")
def add_hypothesis_annotation_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    id: str = typer.Option(..., "--id", help="Hypothesis id or short id."),
    kind: str = typer.Option(..., "--kind", help="Annotation kind: pdf, markdown, url, or file."),
    title: str = typer.Option(..., "--title", help="Operator-facing annotation title."),
    locator: str = typer.Option(..., "--locator", help="URL or file/path locator for the artifact."),
    locator_kind: str = typer.Option(..., "--locator-kind", help="Locator kind: url, repo_path, or local_path."),
    media_type: str | None = typer.Option(None, "--media-type", help="Optional MIME type for the artifact."),
    sha256: str | None = typer.Option(None, "--sha256", help="Optional content hash for local artifacts."),
    note: str | None = typer.Option(None, "--note", help="Optional PM-authored context for the annotation."),
    tag: list[str] = typer.Option(None, "--tag", help="Repeatable operator tag for the annotation."),
    added_by: str | None = typer.Option(None, "--added-by", help="Actor alias. Defaults to current OS user."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    program_id = _require_text(program, "--program")
    store = _open_store(program_id, db_root=db_root)
    hypothesis = _resolve_hypothesis(store, id)
    current_time = datetime.now(timezone.utc)
    resolved_locator = _require_text(locator, "--locator")
    annotation = HypothesisAnnotation(
        id=str(uuid4()),
        program_id=program_id,
        hypothesis_id=hypothesis.id,
        kind=_parse_annotation_kind(kind),
        title=_require_text(title, "--title"),
        locator=resolved_locator,
        locator_kind=_parse_annotation_locator_kind(locator_kind),
        media_type=_optional_text(media_type),
        sha256=_optional_text(sha256),
        note=_optional_text(note),
        tags=_normalize_tags(tag),
        source_ref=SourceRef(kind=SourceKind.DOCUMENT, ref=resolved_locator, captured_at=current_time),
        added_by=_default_actor(added_by),
        added_at=current_time,
    )
    store.upsert_hypothesis_annotation(annotation)
    typer.echo(f"Added annotation {annotation.id} to hypothesis {hypothesis.short_id}")
    raise typer.Exit(code=0)


@annotate_app.command("list")
def list_hypothesis_annotations_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    id: str = typer.Option(..., "--id", help="Hypothesis id or short id."),
    include_archived: bool = typer.Option(False, "--include-archived", help="Include archived annotations."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    program_id = _require_text(program, "--program")
    store = _open_store(program_id, db_root=db_root)
    hypothesis = _resolve_hypothesis(store, id)
    annotations = store.list_hypothesis_annotations(hypothesis.id, include_archived=include_archived)
    normalized_format = _require_text(format, "--format").lower()
    if normalized_format == "json":
        typer.echo(json.dumps([_serialize_annotation(item) for item in annotations], ensure_ascii=True, indent=2))
        raise typer.Exit(code=0)
    if normalized_format != "human":
        raise typer.BadParameter("--format must be one of: human, json")
    if not annotations:
        typer.echo(f"No annotations found for {hypothesis.short_id}.")
        raise typer.Exit(code=0)
    for annotation in annotations:
        archived_suffix = " | archived" if annotation.archived_at is not None else ""
        typer.echo(f"{annotation.id}\t{annotation.kind}\t{annotation.title}\t{annotation.locator}{archived_suffix}")
    raise typer.Exit(code=0)


@annotate_app.command("archive")
def archive_hypothesis_annotation_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    annotation_id: str = typer.Option(..., "--annotation-id", help="Annotation id to archive."),
    reason: str = typer.Option(..., "--reason", help="Why the annotation is being archived."),
    archived_by: str | None = typer.Option(None, "--archived-by", help="Actor alias. Defaults to current OS user."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    program_id = _require_text(program, "--program")
    store = _open_store(program_id, db_root=db_root)
    annotation = store.get_hypothesis_annotation(_require_text(annotation_id, "--annotation-id"))
    if annotation is None:
        raise typer.BadParameter(f"Unknown annotation: {annotation_id}")
    if annotation.archived_at is not None:
        typer.echo(f"Annotation {annotation.id} is already archived.")
        raise typer.Exit(code=0)
    store.archive_hypothesis_annotation(
        annotation.id,
        archived_at=datetime.now(timezone.utc),
        archived_by=_default_actor(archived_by),
        archive_reason=_require_text(reason, "--reason"),
    )
    typer.echo(f"Archived annotation {annotation.id}")
    raise typer.Exit(code=0)


@app.command("propose")
def propose_hypothesis_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    kind: str = typer.Option(..., "--kind", help="Hypothesis kind: scalar_fact, trend, or delivery_date."),
    statement: str = typer.Option(..., "--statement", help="PM-readable hypothesis statement."),
    assertion_id: str | None = typer.Option(None, "--assertion-id", help="Telemetry assertion id for scalar_fact or trend hypotheses."),
    composite_assertion_id: str | None = typer.Option(None, "--composite-assertion-id", help="Composite assertion id for scalar_fact or trend hypotheses."),
    expected_value: float | None = typer.Option(None, "--expected-value", help="Expected numeric value for scalar_fact or trend hypotheses."),
    expected_date: str | None = typer.Option(None, "--expected-date", help="Expected ISO date for delivery_date hypotheses."),
    linked_ado_item: int | None = typer.Option(None, "--linked-ado-item", help="ADO work item id for delivery_date hypotheses."),
    depends_on: list[str] = typer.Option(None, "--depends-on", help="Repeatable upstream hypothesis id or short id dependency."),
    review_due: str | None = typer.Option(None, "--review-due", help="Optional YYYY-MM-DD review date."),
    workstream: str | None = typer.Option(None, "--workstream", help="Optional workstream id."),
    proposed_by: str | None = typer.Option(None, "--proposed-by", help="Actor alias. Defaults to current OS user."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    program_id = _require_text(program, "--program")
    statement_text = _require_text(statement, "--statement")
    hypothesis_kind = HypothesisKind.from_string(kind)
    actor = _default_actor(proposed_by)
    store = _open_store(program_id, db_root=db_root)
    current_time = datetime.now(timezone.utc)
    resolved_expected_value, resolved_assertion_id, resolved_composite_assertion_id, resolved_linked_ado_item = _resolve_kind_inputs(
        store,
        hypothesis_kind=hypothesis_kind,
        assertion_id=assertion_id,
        composite_assertion_id=composite_assertion_id,
        expected_value=expected_value,
        expected_date=expected_date,
        linked_ado_item=linked_ado_item,
        allowed_linked_hypothesis_id=None,
    )
    resolved_dependencies = _resolve_dependency_ids(store, depends_on)
    hypothesis_id = str(uuid4())
    short_id = store.next_hypothesis_short_id()
    hypothesis = Hypothesis(
        id=hypothesis_id,
        short_id=short_id,
        program_id=program_id,
        kind=hypothesis_kind,
        statement=statement_text,
        expected_value=resolved_expected_value,
        as_of_date=current_time.date(),
        telemetry_assertion_id=resolved_assertion_id,
        composite_assertion_id=resolved_composite_assertion_id,
        workstream_id=_optional_text(workstream),
        proposed_by=actor,
        proposed_at=current_time,
        status=HypothesisStatus.PROPOSED,
        depends_on=resolved_dependencies,
        review_due=_parse_optional_date(review_due, option_name="--review-due"),
        linked_ado_item_id=resolved_linked_ado_item,
    )
    store.upsert_hypothesis(hypothesis)
    store.set_hypothesis_state(hypothesis_id, HypothesisStatus.PROPOSED, current_time, actor=actor, reason="cli_propose")
    _link_assertion_if_needed(store, resolved_assertion_id, hypothesis_id)
    _link_composite_assertion_if_needed(store, resolved_composite_assertion_id, hypothesis_id)
    typer.echo(f"Proposed hypothesis {short_id} ({hypothesis_id})")
    raise typer.Exit(code=0)


@app.command("from-assumption")
def propose_hypothesis_from_assumption_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    id: str = typer.Option(..., "--id", help="Assumption id to promote."),
    kind: str = typer.Option(..., "--kind", help="Hypothesis kind: scalar_fact, trend, or delivery_date."),
    assertion_id: str | None = typer.Option(None, "--assertion-id", help="Telemetry assertion id for scalar_fact or trend hypotheses."),
    composite_assertion_id: str | None = typer.Option(None, "--composite-assertion-id", help="Composite assertion id for scalar_fact or trend hypotheses."),
    expected_value: float | None = typer.Option(None, "--expected-value", help="Expected numeric value for scalar_fact or trend hypotheses."),
    expected_date: str | None = typer.Option(None, "--expected-date", help="Expected ISO date for delivery_date hypotheses."),
    linked_ado_item: int | None = typer.Option(None, "--linked-ado-item", help="ADO work item id for delivery_date hypotheses."),
    depends_on: list[str] = typer.Option(None, "--depends-on", help="Repeatable upstream hypothesis id or short id dependency."),
    review_due: str | None = typer.Option(None, "--review-due", help="Optional YYYY-MM-DD review date. Defaults to the assumption validation due date."),
    workstream: str | None = typer.Option(None, "--workstream", help="Optional workstream id override."),
    proposed_by: str | None = typer.Option(None, "--proposed-by", help="Actor alias. Defaults to current OS user."),
    db_root: Path | None = typer.Option(None, hidden=True),
    programs_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    program_id = _require_text(program, "--program")
    assumption_id = _require_text(id, "--id")
    hypothesis_kind = HypothesisKind.from_string(kind)
    actor = _default_actor(proposed_by)
    store = _open_store(program_id, db_root=db_root)
    assumption = _resolve_assumption(program_id, assumption_id, programs_root=programs_root)
    if assumption.status is AssumptionStatus.INVALIDATED:
        raise typer.BadParameter(f"Assumption {assumption.id} is invalidated and cannot be promoted.")

    existing = next(
        (item for item in store.list_active_hypotheses(include_proposed=True) if item.linked_assumption_id == assumption.id),
        None,
    )
    if existing is not None:
        raise typer.BadParameter(f"Assumption {assumption.id} is already linked to active hypothesis {existing.short_id}.")

    current_time = datetime.now(timezone.utc)
    resolved_expected_value, resolved_assertion_id, resolved_composite_assertion_id, resolved_linked_ado_item = _resolve_kind_inputs(
        store,
        hypothesis_kind=hypothesis_kind,
        assertion_id=assertion_id,
        composite_assertion_id=composite_assertion_id,
        expected_value=expected_value,
        expected_date=expected_date,
        linked_ado_item=linked_ado_item,
        allowed_linked_hypothesis_id=None,
    )
    resolved_dependencies = _resolve_dependency_ids(store, depends_on)
    review_due_date = _parse_optional_date(review_due, option_name="--review-due") if review_due is not None else assumption.validation_due
    hypothesis_id = str(uuid4())
    short_id = store.next_hypothesis_short_id()
    hypothesis = Hypothesis(
        id=hypothesis_id,
        short_id=short_id,
        program_id=program_id,
        kind=hypothesis_kind,
        statement=assumption.text,
        expected_value=resolved_expected_value,
        as_of_date=current_time.date(),
        telemetry_assertion_id=resolved_assertion_id,
        composite_assertion_id=resolved_composite_assertion_id,
        source_refs=(SourceRef(kind=SourceKind.ASSUMPTION, ref=assumption.id),),
        workstream_id=_optional_text(workstream),
        proposed_by=actor,
        proposed_at=current_time,
        status=HypothesisStatus.PROPOSED,
        depends_on=resolved_dependencies,
        review_due=review_due_date,
        linked_assumption_id=assumption.id,
        linked_ado_item_id=resolved_linked_ado_item,
    )
    store.upsert_hypothesis(hypothesis)
    store.set_hypothesis_state(hypothesis_id, HypothesisStatus.PROPOSED, current_time, actor=actor, reason="from_assumption")
    _link_assertion_if_needed(store, resolved_assertion_id, hypothesis_id)
    _link_composite_assertion_if_needed(store, resolved_composite_assertion_id, hypothesis_id)
    typer.echo(f"Proposed hypothesis {short_id} from assumption {assumption.id}")
    raise typer.Exit(code=0)


@app.command("confirm")
def confirm_hypothesis_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    id: str = typer.Option(..., "--id", help="Hypothesis id or short id."),
    confirmed_by: str | None = typer.Option(None, "--confirmed-by", help="Actor alias. Defaults to current OS user."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    program_id = _require_text(program, "--program")
    actor = _default_actor(confirmed_by)
    store = _open_store(program_id, db_root=db_root)
    current = _resolve_hypothesis(store, id)
    if current.status is not HypothesisStatus.PROPOSED:
        raise typer.BadParameter(f"Hypothesis {id} is not in proposed state.")
    current_time = datetime.now(timezone.utc)
    store.upsert_hypothesis(
        replace(
            current,
            status=HypothesisStatus.CONFIRMED,
            confirmed_by=actor,
            confirmed_at=current_time,
        )
    )
    store.set_hypothesis_state(current.id, HypothesisStatus.CONFIRMED, current_time, actor=actor, reason="cli_confirm")
    confirmed = store.get_hypothesis(current.id)
    if confirmed is not None:
        append_confirmation_event(
            store,
            event_type="confirmed",
            hypothesis=confirmed,
            actor=actor,
            recorded_at=current_time,
            reason="cli_confirm",
        )
    typer.echo(f"Confirmed hypothesis {current.short_id}")
    raise typer.Exit(code=0)


@app.command("reject")
def reject_hypothesis_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    id: str = typer.Option(..., "--id", help="Hypothesis id or short id."),
    reason: str = typer.Option(..., "--reason", help="Why the proposed hypothesis is being rejected."),
    rejected_by: str | None = typer.Option(None, "--rejected-by", help="Actor alias. Defaults to current OS user."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    program_id = _require_text(program, "--program")
    reason_text = _require_text(reason, "--reason")
    actor = _default_actor(rejected_by)
    store = _open_store(program_id, db_root=db_root)
    current = _resolve_hypothesis(store, id)
    if current.status is not HypothesisStatus.PROPOSED:
        raise typer.BadParameter(f"Hypothesis {id} is not in proposed state.")

    current_time = datetime.now(timezone.utc)
    store.upsert_hypothesis(replace(current, status=HypothesisStatus.REJECTED, rejection_reason=reason_text))
    store.set_hypothesis_state(current.id, HypothesisStatus.REJECTED, current_time, actor=actor, reason=reason_text)
    typer.echo(f"Rejected hypothesis {current.short_id}")
    raise typer.Exit(code=0)


@app.command("challenge")
def challenge_hypothesis_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    id: str = typer.Option(..., "--id", help="Hypothesis id or short id."),
    reason: str = typer.Option(..., "--reason", help="Why the hypothesis is being manually challenged."),
    challenged_by: str | None = typer.Option(None, "--challenged-by", help="Actor alias. Defaults to current OS user."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    program_id = _require_text(program, "--program")
    reason_text = _require_text(reason, "--reason")
    actor = _default_actor(challenged_by)
    store = _open_store(program_id, db_root=db_root)
    current = _resolve_hypothesis(store, id)
    if current.status is not HypothesisStatus.CONFIRMED:
        raise typer.BadParameter(f"Hypothesis {id} must be confirmed before challenge.")

    existing_manual = store.get_latest_challenge_for_hypothesis(current.id, challenge_kind=ChallengeKind.MANUAL)
    if existing_manual is not None and existing_manual.current_state in {
        ChallengeState.OPEN,
        ChallengeState.ACKNOWLEDGED,
        ChallengeState.REOPENED,
        ChallengeState.SNOOZED,
    }:
        raise typer.BadParameter(f"Hypothesis {current.short_id} already has active manual challenge {existing_manual.id}.")

    current_time = datetime.now(timezone.utc)
    challenge = RealityChallenge(
        id=str(uuid4()),
        program_id=current.program_id,
        hypothesis_id=current.id,
        assertion_id=None,
        observation_id=None,
        challenge_kind=ChallengeKind.MANUAL,
        observed_value=None,
        expected_value=None,
        delta_magnitude=None,
        severity=ChallengeSeverity.ALERT,
        source=f"pm:{actor}",
        detected_at=current_time,
        note=reason_text,
    )
    store.upsert_challenge(challenge)
    store.set_hypothesis_state(current.id, HypothesisStatus.CHALLENGED, current_time, actor=actor, reason=reason_text)
    typer.echo(f"Challenged hypothesis {current.short_id} with manual challenge {challenge.id}")
    raise typer.Exit(code=0)


@app.command("update")
def update_hypothesis_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    id: str = typer.Option(..., "--id", help="Hypothesis id or short id."),
    reason: str = typer.Option(..., "--reason", help="Why this revision supersedes the prior hypothesis."),
    statement: str | None = typer.Option(None, "--statement", help="Updated statement."),
    assertion_id: str | None = typer.Option(None, "--assertion-id", help="Updated telemetry assertion id."),
    composite_assertion_id: str | None = typer.Option(None, "--composite-assertion-id", help="Updated composite assertion id."),
    expected_value: float | None = typer.Option(None, "--expected-value", help="Updated numeric expected value."),
    expected_date: str | None = typer.Option(None, "--expected-date", help="Updated ISO date for delivery_date hypotheses."),
    linked_ado_item: int | None = typer.Option(None, "--linked-ado-item", help="Updated ADO work item id for delivery_date hypotheses."),
    depends_on: list[str] = typer.Option(None, "--depends-on", help="Updated repeatable upstream hypothesis id or short id dependency list."),
    clear_depends_on: bool = typer.Option(False, "--clear-depends-on", help="Clear all dependency links from the replacement hypothesis."),
    review_due: str | None = typer.Option(None, "--review-due", help="Updated YYYY-MM-DD review date."),
    workstream: str | None = typer.Option(None, "--workstream", help="Updated workstream id."),
    updated_by: str | None = typer.Option(None, "--updated-by", help="Actor alias. Defaults to current OS user."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    program_id = _require_text(program, "--program")
    reason_text = _require_text(reason, "--reason")
    actor = _default_actor(updated_by)
    store = _open_store(program_id, db_root=db_root)
    current = _resolve_hypothesis(store, id)
    if current.status not in {HypothesisStatus.CONFIRMED, HypothesisStatus.CHALLENGED, HypothesisStatus.STALE}:
        raise typer.BadParameter(f"Hypothesis {id} must be confirmed, challenged, or stale before update.")

    resolved_expected_value, resolved_assertion_id, resolved_composite_assertion_id, resolved_linked_ado_item = _resolve_kind_inputs(
        store,
        hypothesis_kind=current.kind,
        assertion_id=assertion_id if assertion_id is not None else current.telemetry_assertion_id,
        composite_assertion_id=composite_assertion_id if composite_assertion_id is not None else current.composite_assertion_id,
        expected_value=expected_value if expected_value is not None else (float(current.expected_value) if isinstance(current.expected_value, (int, float)) else None),
        expected_date=expected_date if expected_date is not None else (str(current.expected_value) if current.kind is HypothesisKind.DELIVERY_DATE and current.expected_value is not None else None),
        linked_ado_item=linked_ado_item if linked_ado_item is not None else current.linked_ado_item_id,
        allowed_linked_hypothesis_id=current.id,
    )
    if clear_depends_on and depends_on:
        raise typer.BadParameter("--clear-depends-on cannot be combined with --depends-on.")
    resolved_dependencies = (
        ()
        if clear_depends_on
        else (
            _resolve_dependency_ids(store, depends_on, current_hypothesis_id=current.id)
            if depends_on
            else current.depends_on
        )
    )
    next_statement = statement.strip() if statement is not None and statement.strip() else current.statement
    next_review_due = _parse_optional_date(review_due, option_name="--review-due") if review_due is not None else current.review_due
    next_workstream = _optional_text(workstream) if workstream is not None else current.workstream_id

    if (
        next_statement == current.statement
        and resolved_assertion_id == current.telemetry_assertion_id
        and resolved_composite_assertion_id == current.composite_assertion_id
        and resolved_expected_value == current.expected_value
        and resolved_linked_ado_item == current.linked_ado_item_id
        and resolved_dependencies == current.depends_on
        and next_review_due == current.review_due
        and next_workstream == current.workstream_id
    ):
        raise typer.BadParameter("No hypothesis changes were provided.")

    current_time = datetime.now(timezone.utc)
    replacement_id = str(uuid4())
    archived_current = replace(
        current,
        short_id=f"superseded:{current.id}",
        status=HypothesisStatus.SUPERSEDED,
        superseded_by=replacement_id,
    )
    replacement = Hypothesis(
        id=replacement_id,
        short_id=current.short_id,
        program_id=current.program_id,
        kind=current.kind,
        statement=next_statement,
        expected_value=resolved_expected_value,
        as_of_date=current_time.date(),
        telemetry_assertion_id=resolved_assertion_id,
        composite_assertion_id=resolved_composite_assertion_id,
        source_refs=current.source_refs,
        workstream_id=next_workstream,
        proposed_by=actor,
        proposed_at=current_time,
        status=HypothesisStatus.CONFIRMED,
        sensitivity_label=current.sensitivity_label,
        depends_on=resolved_dependencies,
        confirmed_by=actor,
        confirmed_at=current_time,
        review_due=next_review_due,
        linked_claim_id=current.linked_claim_id,
        linked_assumption_id=current.linked_assumption_id,
        linked_ado_item_id=resolved_linked_ado_item,
        linked_doc_section_id=current.linked_doc_section_id,
        expected_value_frozen_at=current.expected_value_frozen_at,
        expires_at=current.expires_at,
        policy_version=current.policy_version,
        supersedes_id=current.id,
    )
    store.upsert_hypothesis(archived_current)
    store.set_hypothesis_state(current.id, HypothesisStatus.SUPERSEDED, current_time, actor=actor, reason=reason_text)
    store.upsert_hypothesis(replacement)
    store.set_hypothesis_state(replacement.id, HypothesisStatus.CONFIRMED, current_time, actor=actor, reason=f"supersedes:{current.id}")
    superseded = store.get_hypothesis(current.id)
    if superseded is not None:
        append_confirmation_event(
            store,
            event_type="superseded",
            hypothesis=superseded,
            actor=actor,
            recorded_at=current_time,
            reason=reason_text,
        )
    replacement_current = store.get_hypothesis(replacement.id)
    if replacement_current is not None:
        append_confirmation_event(
            store,
            event_type="confirmed",
            hypothesis=replacement_current,
            actor=actor,
            recorded_at=current_time,
            reason=f"supersedes:{current.id}",
        )
    _link_assertion_if_needed(store, resolved_assertion_id, replacement.id)
    _link_composite_assertion_if_needed(store, resolved_composite_assertion_id, replacement.id)
    moved_count = store.reassign_snoozed_challenges(current.id, replacement.id)
    typer.echo(f"Updated hypothesis {replacement.short_id}; superseded {current.id}; moved {moved_count} snoozed challenge(s).")
    raise typer.Exit(code=0)


@app.command("invalidate")
def invalidate_hypothesis_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    id: str = typer.Option(..., "--id", help="Hypothesis id or short id."),
    reason: str = typer.Option(..., "--reason", help="Why the hypothesis is being retired."),
    invalidated_by: str | None = typer.Option(None, "--invalidated-by", help="Actor alias. Defaults to current OS user."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    program_id = _require_text(program, "--program")
    reason_text = _require_text(reason, "--reason")
    actor = _default_actor(invalidated_by)
    store = _open_store(program_id, db_root=db_root)
    current = _resolve_hypothesis(store, id)
    if current.status not in {HypothesisStatus.CHALLENGED, HypothesisStatus.STALE}:
        raise typer.BadParameter(f"Hypothesis {id} must be challenged or stale before invalidate.")

    current_time = datetime.now(timezone.utc)
    store.upsert_hypothesis(replace(current, status=HypothesisStatus.INVALIDATED))
    store.set_hypothesis_state(current.id, HypothesisStatus.INVALIDATED, current_time, actor=actor, reason=reason_text)
    invalidated = store.get_hypothesis(current.id)
    if invalidated is not None:
        append_confirmation_event(
            store,
            event_type="invalidated",
            hypothesis=invalidated,
            actor=actor,
            recorded_at=current_time,
            reason=reason_text,
        )
    resolved_count = _transition_active_challenges(
        store,
        hypothesis_id=current.id,
        state=ChallengeState.RESOLVED,
        changed_at=current_time,
        actor=actor,
        reason=f"hypothesis_invalidated:{reason_text}",
    )
    typer.echo(f"Invalidated hypothesis {current.short_id}; resolved {resolved_count} active challenge(s).")
    raise typer.Exit(code=0)


@app.command("reinstate")
def reinstate_hypothesis_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    id: str = typer.Option(..., "--id", help="Hypothesis id or short id."),
    reason: str = typer.Option(..., "--reason", help="Why the challenge is considered resolved."),
    reinstated_by: str | None = typer.Option(None, "--reinstated-by", help="Actor alias. Defaults to current OS user."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    program_id = _require_text(program, "--program")
    reason_text = _require_text(reason, "--reason")
    actor = _default_actor(reinstated_by)
    store = _open_store(program_id, db_root=db_root)
    current = _resolve_hypothesis(store, id)
    if current.status is not HypothesisStatus.CHALLENGED:
        raise typer.BadParameter(f"Hypothesis {id} must be challenged before reinstate.")

    current_time = datetime.now(timezone.utc)
    store.upsert_hypothesis(replace(current, status=HypothesisStatus.CONFIRMED))
    store.set_hypothesis_state(current.id, HypothesisStatus.CONFIRMED, current_time, actor=actor, reason=reason_text)
    reinstated = store.get_hypothesis(current.id)
    if reinstated is not None:
        append_confirmation_event(
            store,
            event_type="confirmed",
            hypothesis=reinstated,
            actor=actor,
            recorded_at=current_time,
            reason=reason_text,
        )
    dismissed_count = _transition_active_challenges(
        store,
        hypothesis_id=current.id,
        state=ChallengeState.DISMISSED,
        changed_at=current_time,
        actor=actor,
        reason=f"hypothesis_reinstated:{reason_text}",
    )
    _append_reinstatement_evaluation(store, current, evaluated_at=current_time, note=reason_text)
    typer.echo(f"Reinstated hypothesis {current.short_id}; dismissed {dismissed_count} active challenge(s).")
    raise typer.Exit(code=0)


@app.command("export-confirmations")
def export_confirmations_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    output: Path = typer.Option(..., "--output", help="Destination JSONL path for the confirmation seed export."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    program_id = _require_text(program, "--program")
    store = _open_store(program_id, db_root=db_root)
    hypotheses = tuple(
        sorted(
            store.list_hypotheses(
                statuses=(
                    HypothesisStatus.CONFIRMED,
                    HypothesisStatus.CHALLENGED,
                    HypothesisStatus.STALE,
                )
            ),
            key=lambda item: (item.short_id, item.id),
        )
    )
    assertion_ids = sorted({item.telemetry_assertion_id for item in hypotheses if item.telemetry_assertion_id is not None})
    assertions = tuple(
        assertion
        for assertion_id in assertion_ids
        if (assertion := store.get_telemetry_assertion(assertion_id)) is not None
    )
    exported_hypothesis_ids = {item.id for item in hypotheses}
    challenges = tuple(
        sorted(
            (
                challenge
                for challenge in store.list_active_challenges(include_snoozed=True)
                if challenge.hypothesis_id in exported_hypothesis_ids
            ),
            key=lambda item: (item.hypothesis_id, item.detected_at.isoformat(), item.id),
        )
    )

    records = [
        {
            "record_type": "seed_metadata",
            "schema_version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "program_id": program_id,
            "assertion_count": len(assertions),
            "hypothesis_count": len(hypotheses),
            "challenge_count": len(challenges),
        }
    ]
    records.extend({"record_type": "telemetry_assertion", "record": _serialize_seed_model(item)} for item in assertions)
    records.extend({"record_type": "hypothesis", "record": _serialize_seed_model(item)} for item in hypotheses)
    records.extend({"record_type": "challenge", "record": _serialize_seed_model(item)} for item in challenges)

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True))
            handle.write("\n")

    typer.echo(f"Exported {len(records)} confirmation seed records to {output}")
    raise typer.Exit(code=0)


def _open_store(program: str, *, db_root: Path | None) -> RealityStore:
    program_id = _require_text(program, "--program")
    store = RealityStore(program_id, db_root=db_root)
    store.initialize()
    return store


def _resolve_hypothesis(store: RealityStore, identifier: str) -> Hypothesis:
    value = _require_text(identifier, "--id")
    hypothesis = store.get_hypothesis(value)
    if hypothesis is None:
        hypothesis = store.get_hypothesis_by_short_id(value)
    if hypothesis is None:
        raise typer.BadParameter(f"Unknown hypothesis: {value}")
    return hypothesis


def _resolve_assumption(program_id: str, assumption_id: str, *, programs_root: Path | None) -> Assumption:
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    assumption = next(
        (
            entry
            for entry in project_assumptions(
                load_program_facts(
                    program_id,
                    db_root=resolved_programs_root.parent,
                    programs_root=resolved_programs_root,
                    fact_types=("assumption.entry",),
                )
            )
            if entry.id == assumption_id
        ),
        None,
    )
    if assumption is None:
        raise typer.BadParameter(f"Unknown assumption: {assumption_id}")
    return assumption


def _resolve_kind_inputs(
    store: RealityStore,
    *,
    hypothesis_kind: HypothesisKind,
    assertion_id: str | None,
    composite_assertion_id: str | None,
    expected_value: float | None,
    expected_date: str | None,
    linked_ado_item: int | None,
    allowed_linked_hypothesis_id: str | None,
) -> tuple[float | str | None, str | None, str | None, int | None]:
    if hypothesis_kind is HypothesisKind.DELIVERY_DATE:
        if assertion_id is not None and assertion_id.strip():
            raise typer.BadParameter("--assertion-id is not valid for delivery_date hypotheses.")
        if composite_assertion_id is not None and composite_assertion_id.strip():
            raise typer.BadParameter("--composite-assertion-id is not valid for delivery_date hypotheses.")
        if expected_date is None:
            raise typer.BadParameter("--expected-date is required for delivery_date hypotheses.")
        if linked_ado_item is None:
            raise typer.BadParameter("--linked-ado-item is required for delivery_date hypotheses.")
        return _parse_required_date(expected_date, option_name="--expected-date").isoformat(), None, None, linked_ado_item

    if linked_ado_item is not None:
        raise typer.BadParameter("--linked-ado-item is only valid for delivery_date hypotheses.")
    if expected_date is not None:
        raise typer.BadParameter("--expected-date is only valid for delivery_date hypotheses.")
    if bool(_optional_text(assertion_id)) == bool(_optional_text(composite_assertion_id)):
        raise typer.BadParameter("Provide exactly one of --assertion-id or --composite-assertion-id for scalar_fact and trend hypotheses.")
    if expected_value is None:
        raise typer.BadParameter("--expected-value is required for scalar_fact and trend hypotheses.")
    if _optional_text(assertion_id):
        assertion = _require_assertion(store, assertion_id)
        _ensure_assertion_available(store, assertion.id, allowed_linked_hypothesis_id=allowed_linked_hypothesis_id)
        return float(expected_value), assertion.id, None, None
    composite_assertion = _require_composite_assertion(store, composite_assertion_id)
    _ensure_composite_assertion_available(store, composite_assertion.id, allowed_linked_hypothesis_id=allowed_linked_hypothesis_id)
    return float(expected_value), None, composite_assertion.id, None


def _resolve_dependency_ids(
    store: RealityStore,
    dependencies: list[str] | None,
    *,
    current_hypothesis_id: str | None = None,
) -> tuple[str, ...]:
    resolved: list[str] = []
    seen: set[str] = set()
    for raw_dependency in dependencies or []:
        dependency = _resolve_hypothesis(store, raw_dependency)
        if dependency.status in {HypothesisStatus.REJECTED, HypothesisStatus.INVALIDATED, HypothesisStatus.SUPERSEDED}:
            raise typer.BadParameter(f"Hypothesis {raw_dependency} is not active and cannot be used in --depends-on.")
        if current_hypothesis_id is not None and dependency.id == current_hypothesis_id:
            raise typer.BadParameter("A hypothesis cannot depend on itself.")
        if dependency.id in seen:
            continue
        seen.add(dependency.id)
        resolved.append(dependency.id)
    return tuple(resolved)


def _require_assertion(store: RealityStore, assertion_id: str | None):
    assertion_value = _require_text(assertion_id, "--assertion-id")
    assertion = store.get_telemetry_assertion(assertion_value)
    if assertion is None:
        raise typer.BadParameter(f"Unknown telemetry assertion: {assertion_value}")
    if assertion.valid_until is not None:
        raise typer.BadParameter(f"Telemetry assertion {assertion_value} is archived; use an active assertion id.")
    return assertion


def _ensure_assertion_available(
    store: RealityStore,
    assertion_id: str,
    *,
    allowed_linked_hypothesis_id: str | None,
) -> None:
    assertion = store.get_telemetry_assertion(assertion_id)
    if assertion is None or assertion.linked_hypothesis_id is None:
        return
    if allowed_linked_hypothesis_id is not None and assertion.linked_hypothesis_id == allowed_linked_hypothesis_id:
        return
    linked = store.get_hypothesis(assertion.linked_hypothesis_id)
    if linked is None:
        return
    if linked.status not in {HypothesisStatus.REJECTED, HypothesisStatus.INVALIDATED, HypothesisStatus.SUPERSEDED}:
        raise typer.BadParameter(
            f"Telemetry assertion {assertion_id} is already linked to active hypothesis {linked.short_id}."
        )


def _link_assertion_if_needed(store: RealityStore, assertion_id: str | None, hypothesis_id: str) -> None:
    if assertion_id is None:
        return
    assertion = store.get_telemetry_assertion(assertion_id)
    if assertion is None or assertion.linked_hypothesis_id == hypothesis_id:
        return
    store.upsert_telemetry_assertion(replace(assertion, linked_hypothesis_id=hypothesis_id))


def _require_composite_assertion(store: RealityStore, composite_assertion_id: str | None) -> CompositeAssertion:
    assertion_value = _require_text(composite_assertion_id, "--composite-assertion-id")
    assertion = store.get_composite_assertion(assertion_value)
    if assertion is None:
        raise typer.BadParameter(f"Unknown composite assertion: {assertion_value}")
    if assertion.valid_until is not None:
        raise typer.BadParameter(f"Composite assertion {assertion_value} is archived; use an active composite id.")
    return assertion


def _ensure_composite_assertion_available(
    store: RealityStore,
    composite_assertion_id: str,
    *,
    allowed_linked_hypothesis_id: str | None,
) -> None:
    assertion = store.get_composite_assertion(composite_assertion_id)
    if assertion is None or assertion.linked_hypothesis_id is None:
        return
    if allowed_linked_hypothesis_id is not None and assertion.linked_hypothesis_id == allowed_linked_hypothesis_id:
        return
    linked = store.get_hypothesis(assertion.linked_hypothesis_id)
    if linked is None:
        return
    if linked.status not in {HypothesisStatus.REJECTED, HypothesisStatus.INVALIDATED, HypothesisStatus.SUPERSEDED}:
        raise typer.BadParameter(
            f"Composite assertion {composite_assertion_id} is already linked to active hypothesis {linked.short_id}."
        )


def _link_composite_assertion_if_needed(store: RealityStore, composite_assertion_id: str | None, hypothesis_id: str) -> None:
    if composite_assertion_id is None:
        return
    assertion = store.get_composite_assertion(composite_assertion_id)
    if assertion is None or assertion.linked_hypothesis_id == hypothesis_id:
        return
    store.upsert_composite_assertion(replace(assertion, linked_hypothesis_id=hypothesis_id))


def _load_recent_observations(store: RealityStore, assertion: TelemetryAssertion | None) -> tuple[MetricObservation, ...]:
    if assertion is None:
        return ()
    observations = store.list_metric_observations(assertion.metric_id)
    return tuple(reversed(observations[-5:]))


def _collect_hypothesis_lifecycle(store: RealityStore, hypothesis: Hypothesis) -> tuple[Hypothesis, ...]:
    previous: list[Hypothesis] = []
    seen_ids = {hypothesis.id}
    cursor = hypothesis
    while cursor.supersedes_id is not None:
        parent = store.get_hypothesis(cursor.supersedes_id)
        if parent is None or parent.id in seen_ids:
            break
        previous.append(parent)
        seen_ids.add(parent.id)
        cursor = parent

    following: list[Hypothesis] = []
    cursor = hypothesis
    while cursor.superseded_by is not None:
        child = store.get_hypothesis(cursor.superseded_by)
        if child is None or child.id in seen_ids:
            break
        following.append(child)
        seen_ids.add(child.id)
        cursor = child

    return tuple(reversed(previous)) + (hypothesis,) + tuple(following)


def _transition_active_challenges(
    store: RealityStore,
    *,
    hypothesis_id: str,
    state: ChallengeState,
    changed_at: datetime,
    actor: str,
    reason: str,
) -> int:
    challenges = [
        challenge
        for challenge in store.list_active_challenges(include_snoozed=True)
        if challenge.hypothesis_id == hypothesis_id
    ]
    for challenge in challenges:
        store.update_challenge_state(challenge.id, state, changed_at, actor=actor, reason=reason)
    return len(challenges)


def _append_reinstatement_evaluation(
    store: RealityStore,
    hypothesis: Hypothesis,
    *,
    evaluated_at: datetime,
    note: str,
) -> None:
    if hypothesis.telemetry_assertion_id is None:
        return
    latest_challenge = store.get_latest_challenge_for_assertion(hypothesis.id, hypothesis.telemetry_assertion_id)
    expected_value = float(hypothesis.expected_value) if isinstance(hypothesis.expected_value, (int, float)) else None
    observed_value = latest_challenge.observed_value if latest_challenge is not None else None
    observation_id = latest_challenge.observation_id if latest_challenge is not None else None
    store.append_assertion_evaluation(
        AssertionEvaluation(
            id=str(uuid4()),
            program_id=hypothesis.program_id,
            hypothesis_id=hypothesis.id,
            assertion_id=hypothesis.telemetry_assertion_id,
            observation_id=observation_id,
            evaluated_at=evaluated_at,
            violated=False,
            value_num=observed_value,
            expected_value=expected_value,
            quality_state=None,
            note=f"reinstated:{note}",
        )
    )


def append_confirmation_event(
    store: RealityStore,
    *,
    event_type: str,
    hypothesis: Hypothesis,
    actor: str,
    recorded_at: datetime,
    reason: str | None,
) -> None:
    payload = {
        "event_type": event_type,
        "recorded_at": recorded_at.isoformat(),
        "program_id": hypothesis.program_id,
        "hypothesis_id": hypothesis.id,
        "short_id": hypothesis.short_id,
        "status": hypothesis.status.value,
        "actor": actor,
        "reason": reason,
        "confirmed_at": hypothesis.confirmed_at.isoformat() if hypothesis.confirmed_at is not None else None,
        "supersedes_id": hypothesis.supersedes_id,
        "superseded_by": hypothesis.superseded_by,
    }
    path = store.db_path.parent / "_confirmations.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            portalocker.unlock(handle)


def _serialize_seed_model(model: object) -> dict[str, object]:
    from typing import cast

    payload = _normalize_seed_value(asdict(model))  # type: ignore[call-overload]
    if isinstance(payload, dict) and payload.get("composite_assertion_id") is None:
        payload.pop("composite_assertion_id", None)
    return cast("dict[str, object]", payload)


def _normalize_seed_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _normalize_seed_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_seed_value(item) for item in value]
    return value


def _parse_optional_date(value: str | None, *, option_name: str) -> date | None:
    if value is None:
        return None
    return _parse_required_date(value, option_name=option_name)


def _parse_required_date(value: str, *, option_name: str) -> date:
    text = _require_text(value, option_name)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise typer.BadParameter(f"Invalid {option_name} value: {value}") from exc


def _require_text(value: str | None, option_name: str) -> str:
    if value is None or not value.strip():
        raise typer.BadParameter(f"{option_name} must be non-empty")
    return value.strip()


def _optional_text(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


def _normalize_tags(values: list[str] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in values or []:
        value = raw_value.strip()
        if not value:
            raise typer.BadParameter("--tag values must be non-empty")
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return tuple(normalized)


def _parse_annotation_kind(value: str) -> "Literal['pdf', 'markdown', 'url', 'file']":
    from typing import cast

    normalized = _require_text(value, "--kind").lower()
    if normalized not in {"pdf", "markdown", "url", "file"}:
        raise typer.BadParameter("--kind must be one of: pdf, markdown, url, file")
    return cast("Literal['pdf', 'markdown', 'url', 'file']", normalized)


def _parse_annotation_locator_kind(value: str) -> "Literal['url', 'repo_path', 'local_path']":
    from typing import cast

    normalized = _require_text(value, "--locator-kind").lower()
    if normalized not in {"url", "repo_path", "local_path"}:
        raise typer.BadParameter("--locator-kind must be one of: url, repo_path, local_path")
    return cast("Literal['url', 'repo_path', 'local_path']", normalized)


def _default_actor(explicit_actor: str | None) -> str:
    if explicit_actor is not None and explicit_actor.strip():
        return explicit_actor.strip()
    for candidate in (getpass.getuser(),):
        if candidate and candidate.strip():
            return candidate.strip()
    return "vertex/system"


def _render_hypothesis_show_text(
    hypothesis: Hypothesis,
    *,
    lifecycle: tuple[Hypothesis, ...],
    assertion: TelemetryAssertion | None,
    composite_assertion: CompositeAssertion | None,
    latest_challenge: RealityChallenge | None,
    observations: tuple[MetricObservation, ...],
    annotations: tuple[HypothesisAnnotation, ...],
) -> str:
    lines = [
        f"{hypothesis.short_id} | {hypothesis.status.value} | {hypothesis.kind.value}",
        hypothesis.statement,
        f"id: {hypothesis.id}",
    ]
    if hypothesis.expected_value is not None:
        lines.append(f"expected_value: {hypothesis.expected_value}")
    if hypothesis.linked_ado_item_id is not None:
        lines.append(f"linked_ado_item_id: {hypothesis.linked_ado_item_id}")
    if hypothesis.depends_on:
        lines.append("depends_on: " + ", ".join(hypothesis.depends_on))
    if lifecycle:
        lines.append("lifecycle: " + " -> ".join(f"{item.id}:{item.status.value}" for item in lifecycle))
    if assertion is not None:
        lines.append(
            "assertion: "
            + f"{assertion.id} | metric={assertion.metric_id} | operator={assertion.operator.value} | threshold={assertion.threshold}"
        )
    elif composite_assertion is not None:
        lines.append(
            "assertion: "
            + f"{composite_assertion.id} | composite={composite_assertion.operator.value} | children={', '.join(composite_assertion.child_assertion_ids)}"
        )
    else:
        lines.append("assertion: none")
    if latest_challenge is not None:
        lines.append(
            "latest_challenge: "
            + f"{latest_challenge.id} | {latest_challenge.challenge_kind.value} | {latest_challenge.current_state.value} | {latest_challenge.source}"
        )
    else:
        lines.append("latest_challenge: none")
    if observations:
        lines.append("recent_observations:")
        for observation in observations:
            lines.append(
                "- "
                + f"{observation.measurement_period_end.isoformat()} | value={observation.value_num} | quality={observation.quality_state.value}"
            )
    else:
        lines.append("recent_observations: none")
    if annotations:
        lines.append("annotations:")
        for annotation in annotations:
            archived_suffix = " | archived" if annotation.archived_at is not None else ""
            lines.append(
                "- "
                + f"{annotation.id} | {annotation.kind} | {annotation.title} | {annotation.locator}{archived_suffix}"
            )
    else:
        lines.append("annotations: none")
    return "\n".join(lines)


def _serialize_hypothesis(hypothesis: Hypothesis) -> dict[str, object]:
    payload = asdict(hypothesis)
    payload["kind"] = hypothesis.kind.value
    payload["status"] = hypothesis.status.value
    payload["as_of_date"] = hypothesis.as_of_date.isoformat()
    payload["proposed_at"] = hypothesis.proposed_at.isoformat() if hypothesis.proposed_at is not None else None
    payload["confirmed_at"] = hypothesis.confirmed_at.isoformat() if hypothesis.confirmed_at is not None else None
    payload["review_due"] = hypothesis.review_due.isoformat() if hypothesis.review_due is not None else None
    payload["expires_at"] = hypothesis.expires_at.isoformat() if hypothesis.expires_at is not None else None
    payload["expected_value_frozen_at"] = (
        hypothesis.expected_value_frozen_at.isoformat() if hypothesis.expected_value_frozen_at is not None else None
    )
    payload["source_refs"] = [f"{source_ref.kind.value}:{source_ref.ref}" for source_ref in hypothesis.source_refs]
    return payload


def _serialize_assertion(assertion: TelemetryAssertion | None) -> dict[str, object] | None:
    if assertion is None:
        return None
    return {
        "id": assertion.id,
        "program_id": assertion.program_id,
        "metric_id": assertion.metric_id,
        "operator": assertion.operator.value,
        "threshold": assertion.threshold,
        "cooldown_hours": assertion.cooldown_hours,
        "sustain_min_observations": assertion.sustain_min_observations,
        "linked_hypothesis_id": assertion.linked_hypothesis_id,
        "valid_from": assertion.valid_from.isoformat(),
        "valid_until": assertion.valid_until.isoformat() if assertion.valid_until is not None else None,
    }


def _serialize_composite_assertion(assertion: CompositeAssertion | None) -> dict[str, object] | None:
    if assertion is None:
        return None
    return {
        "id": assertion.id,
        "program_id": assertion.program_id,
        "operator": assertion.operator.value,
        "child_assertion_ids": list(assertion.child_assertion_ids),
        "description": assertion.description,
        "linked_hypothesis_id": assertion.linked_hypothesis_id,
        "valid_from": assertion.valid_from.isoformat(),
        "valid_until": assertion.valid_until.isoformat() if assertion.valid_until is not None else None,
    }


def _serialize_annotation(annotation: HypothesisAnnotation) -> dict[str, object]:
    payload = asdict(annotation)
    payload["added_at"] = annotation.added_at.isoformat()
    payload["archived_at"] = annotation.archived_at.isoformat() if annotation.archived_at is not None else None
    payload["source_ref"] = (
        {
            "kind": annotation.source_ref.kind.value,
            "ref": annotation.source_ref.ref,
            "captured_at": annotation.source_ref.captured_at.isoformat() if annotation.source_ref.captured_at is not None else None,
        }
        if annotation.source_ref is not None
        else None
    )
    return payload


def _serialize_challenge(challenge: RealityChallenge | None) -> dict[str, object] | None:
    if challenge is None:
        return None
    return {
        "id": challenge.id,
        "hypothesis_id": challenge.hypothesis_id,
        "assertion_id": challenge.assertion_id,
        "composite_assertion_id": challenge.composite_assertion_id,
        "challenge_kind": challenge.challenge_kind.value,
        "severity": challenge.severity.value,
        "source": challenge.source,
        "current_state": challenge.current_state.value,
        "detected_at": challenge.detected_at.isoformat(),
        "note": challenge.note,
    }


def _serialize_metric_observation(observation: MetricObservation) -> dict[str, object]:
    return {
        "observation_id": observation.observation_id,
        "metric_id": observation.metric_id,
        "measurement_period_start": observation.measurement_period_start.isoformat(),
        "measurement_period_end": observation.measurement_period_end.isoformat(),
        "observed_at": observation.observed_at.isoformat(),
        "value_num": observation.value_num,
        "value_text": observation.value_text,
        "sample_count": observation.sample_count,
        "quality_state": observation.quality_state.value,
        "source_binding_id": observation.source_binding_id,
    }