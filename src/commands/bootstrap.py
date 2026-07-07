from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from uuid import uuid4

import typer

from src.core.assumption_tracker import get_assumptions_path
from src.core.claim_tracker import load_open_claims
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.hypothesis_models import AssertionOperator, Hypothesis, HypothesisKind, HypothesisStatus, TelemetryAssertion
from src.core.kusto_query_loader import load_kpi_queries
from src.core.models_v2 import Milestone
from src.core.models_v2 import Assumption, AssumptionStatus, ClaimEntry, KustoQuery
from src.core.program_fact_store import load_program_facts, project_assumptions, project_milestones
from src.core.reality_store import RealityStore
from src.core.source_models import SourceKind, SourceRef


_WI_REF_PATTERN = re.compile(r"WI:(\d+)", re.IGNORECASE)
_STARTER_ASSUMPTIONS_TEMPLATE = """schema_version: '1.0'
assumptions: []

# starter assumption - edit and remove this comment block when you add real entries
# - id: assumption-001
#   program_id: {program_id}
#   text: Describe a current condition this program depends on.
#   validation_method: Explain how you will validate the assumption.
#   validation_due: YYYY-MM-DD
#   status: unvalidated
#   linked_risk_id: null
#   linked_milestone_id: null
#   owner_alias: <alias>
#   identified_date: YYYY-MM-DD
#   entity_refs: []
#
# - id: assumption-002
#   program_id: {program_id}
#   text: Capture a second assumption only if it materially affects delivery.
#   validation_method: Note the evidence source you will check.
#   validation_due: YYYY-MM-DD
#   status: unvalidated
#   linked_risk_id: null
#   linked_milestone_id: null
#   owner_alias: <alias>
#   identified_date: YYYY-MM-DD
#   entity_refs: []
"""


def bootstrap_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview bootstrap proposals without writing them."),
    limit: int = typer.Option(500, "--limit", min=1, max=500, help="Maximum number of proposals to seed."),
    db_root: Path | None = typer.Option(None, hidden=True),
    programs_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    program_id = program.strip()
    if not program_id:
        raise typer.BadParameter("--program must be non-empty")

    resolved_programs_root = programs_root or PROGRAMS_ROOT
    target_store = RealityStore(program_id, db_root=db_root)
    target_store.initialize()

    claims = tuple(sorted(load_open_claims(program_id, programs_root=resolved_programs_root), key=_claim_sort_key))
    assumptions = tuple(
        sorted(
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
                if entry.status is AssumptionStatus.UNVALIDATED
            ),
            key=_assumption_sort_key,
        )
    )
    milestones = tuple(
        sorted(
            project_milestones(
                load_program_facts(
                    program_id,
                    db_root=resolved_programs_root.parent,
                    programs_root=resolved_programs_root,
                    fact_types=("milestone.entry",),
                )
            ),
            key=_milestone_sort_key,
        )
    )
    queries = tuple(sorted(load_kpi_queries(program_id, programs_root=resolved_programs_root), key=_query_sort_key))

    if dry_run:
        with TemporaryDirectory() as temp_dir_name:
            preview_store = _clone_store_for_preview(program_id, source_store=target_store, preview_root=Path(temp_dir_name))
            created = _bootstrap_hypotheses(
                store=preview_store,
                claims=claims,
                assumptions=assumptions,
                milestones=milestones,
                queries=queries,
                limit=limit,
                proposed_at=datetime.now(timezone.utc),
            )
        _echo_bootstrap_summary(program_id, created, dry_run=True, starter_assumptions_path=None)
        raise typer.Exit(code=0)

    created = _bootstrap_hypotheses(
        store=target_store,
        claims=claims,
        assumptions=assumptions,
        milestones=milestones,
        queries=queries,
        limit=limit,
        proposed_at=datetime.now(timezone.utc),
    )
    starter_assumptions_path = _seed_starter_assumptions_if_needed(
        program_id,
        programs_root=resolved_programs_root,
        claims=claims,
        assumptions=assumptions,
    )
    _echo_bootstrap_summary(program_id, created, dry_run=False, starter_assumptions_path=starter_assumptions_path)
    raise typer.Exit(code=0)


def _bootstrap_hypotheses(
    *,
    store: RealityStore,
    claims: tuple[ClaimEntry, ...],
    assumptions: tuple[Assumption, ...],
    milestones: tuple[Milestone, ...],
    queries: tuple[KustoQuery, ...],
    limit: int,
    proposed_at: datetime,
) -> tuple[Hypothesis, ...]:
    created: list[Hypothesis] = []
    existing_hypotheses = store.list_hypotheses()
    existing_claim_ids = {hypothesis.linked_claim_id for hypothesis in existing_hypotheses if hypothesis.linked_claim_id}
    existing_assumption_ids = {
        hypothesis.linked_assumption_id
        for hypothesis in existing_hypotheses
        if hypothesis.linked_assumption_id
    }
    existing_milestone_ids = {
        source_ref.ref
        for hypothesis in existing_hypotheses
        for source_ref in hypothesis.source_refs
        if source_ref.kind is SourceKind.MILESTONE
    }
    existing_query_assertion_pairs = {
        (source_ref.ref, hypothesis.telemetry_assertion_id)
        for hypothesis in existing_hypotheses
        for source_ref in hypothesis.source_refs
        if source_ref.kind is SourceKind.KPI_QUERY and hypothesis.telemetry_assertion_id is not None
    }
    active_assertions = {
        assertion.id: assertion
        for assertion in store.list_active_telemetry_assertions()
    }

    for claim in claims:
        if len(created) >= limit:
            break
        if claim.id in existing_claim_ids:
            continue
        hypothesis = _build_claim_hypothesis(store=store, claim=claim, proposed_at=proposed_at)
        store.upsert_hypothesis(hypothesis)
        created.append(hypothesis)
        existing_claim_ids.add(claim.id)

    for assumption in assumptions:
        if len(created) >= limit:
            break
        if assumption.id in existing_assumption_ids:
            continue
        hypothesis = _build_assumption_hypothesis(store=store, assumption=assumption, proposed_at=proposed_at)
        store.upsert_hypothesis(hypothesis)
        created.append(hypothesis)
        existing_assumption_ids.add(assumption.id)

    for milestone in milestones:
        if len(created) >= limit:
            break
        if milestone.id in existing_milestone_ids:
            continue
        hypothesis = _build_milestone_hypothesis(store=store, milestone=milestone, proposed_at=proposed_at)
        store.upsert_hypothesis(hypothesis)
        created.append(hypothesis)
        existing_milestone_ids.add(milestone.id)

    for query in queries:
        if len(created) >= limit:
            break
        if not query.assertion_ids:
            continue
        for assertion_id in query.assertion_ids:
            if len(created) >= limit:
                break
            assertion = active_assertions.get(assertion_id)
            if assertion is None:
                continue
            pair = (query.id, assertion_id)
            if pair in existing_query_assertion_pairs:
                continue
            if assertion.linked_hypothesis_id:
                continue
            if query.metric_id is not None and query.metric_id != assertion.metric_id:
                continue
            hypothesis = _build_kpi_query_hypothesis(
                store=store,
                query=query,
                assertion=assertion,
                proposed_at=proposed_at,
            )
            store.upsert_hypothesis(hypothesis)
            store.upsert_telemetry_assertion(replace(assertion, linked_hypothesis_id=hypothesis.id))
            created.append(hypothesis)
            existing_query_assertion_pairs.add(pair)

    return tuple(created)


def _build_claim_hypothesis(*, store: RealityStore, claim: ClaimEntry, proposed_at: datetime) -> Hypothesis:
    if claim.due_date is not None:
        kind = HypothesisKind.DELIVERY_DATE
        expected_value: float | str | None = claim.due_date.isoformat()
    else:
        kind = HypothesisKind.SCALAR_FACT
        expected_value = None
    return Hypothesis(
        id=str(uuid4()),
        short_id=store.next_hypothesis_short_id(),
        program_id=claim.program_id,
        kind=kind,
        statement=claim.text,
        expected_value=expected_value,
        as_of_date=claim.claim_date,
        telemetry_assertion_id=None,
        source_refs=(SourceRef(kind=SourceKind.CLAIM, ref=claim.id),),
        workstream_id=claim.workstream_id,
        proposed_by="bootstrap:claim",
        proposed_at=proposed_at,
        status=HypothesisStatus.PROPOSED,
        linked_claim_id=claim.id,
        linked_ado_item_id=_extract_linked_ado_item_id(claim),
    )


def _build_assumption_hypothesis(*, store: RealityStore, assumption: Assumption, proposed_at: datetime) -> Hypothesis:
    return Hypothesis(
        id=str(uuid4()),
        short_id=store.next_hypothesis_short_id(),
        program_id=assumption.program_id,
        kind=HypothesisKind.SCALAR_FACT,
        statement=assumption.text,
        expected_value=None,
        as_of_date=assumption.identified_date,
        telemetry_assertion_id=None,
        source_refs=(SourceRef(kind=SourceKind.ASSUMPTION, ref=assumption.id),),
        proposed_by="bootstrap:assumption",
        proposed_at=proposed_at,
        status=HypothesisStatus.PROPOSED,
        review_due=assumption.validation_due,
        linked_assumption_id=assumption.id,
    )


def _build_milestone_hypothesis(*, store: RealityStore, milestone, proposed_at: datetime) -> Hypothesis:
    return Hypothesis(
        id=str(uuid4()),
        short_id=store.next_hypothesis_short_id(),
        program_id=milestone.program_id,
        kind=HypothesisKind.DELIVERY_DATE,
        statement=f"{milestone.name} will complete by {milestone.target_date.isoformat()}.",
        expected_value=milestone.target_date.isoformat(),
        as_of_date=proposed_at.date(),
        telemetry_assertion_id=None,
        source_refs=(SourceRef(kind=SourceKind.MILESTONE, ref=milestone.id),),
        workstream_id=milestone.linked_workstream_ids[0] if milestone.linked_workstream_ids else None,
        proposed_by="bootstrap:milestone",
        proposed_at=proposed_at,
        status=HypothesisStatus.PROPOSED,
        review_due=milestone.target_date,
    )


def _build_kpi_query_hypothesis(
    *,
    store: RealityStore,
    query: KustoQuery,
    assertion: TelemetryAssertion,
    proposed_at: datetime,
) -> Hypothesis:
    return Hypothesis(
        id=str(uuid4()),
        short_id=store.next_hypothesis_short_id(),
        program_id=assertion.program_id,
        kind=HypothesisKind.SCALAR_FACT,
        statement=_render_assertion_statement(assertion),
        expected_value=float(assertion.threshold),
        as_of_date=proposed_at.date(),
        telemetry_assertion_id=assertion.id,
        source_refs=(SourceRef(kind=SourceKind.KPI_QUERY, ref=query.id),),
        workstream_id=query.workstream_ids[0] if query.workstream_ids else None,
        proposed_by="bootstrap:kpi_query",
        proposed_at=proposed_at,
        status=HypothesisStatus.PROPOSED,
        review_due=assertion.re_evaluate_by,
    )


def _clone_store_for_preview(program_id: str, *, source_store: RealityStore, preview_root: Path) -> RealityStore:
    preview_store = RealityStore(program_id, db_root=preview_root)
    source_path = source_store.db_path
    preview_path = preview_store.db_path
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    if source_path.exists():
        shutil.copy2(source_path, preview_path)
    preview_store.initialize()
    return preview_store


def _echo_bootstrap_summary(
    program_id: str,
    created: tuple[Hypothesis, ...],
    *,
    dry_run: bool,
    starter_assumptions_path: Path | None,
) -> None:
    if not created:
        message = "Bootstrap dry-run found no new proposals." if dry_run else "No new bootstrap proposals created."
        typer.echo(f"{message} Program: {program_id}")
        if starter_assumptions_path is not None:
            typer.echo(f"Seeded starter assumptions template: {starter_assumptions_path}")
        return

    claim_count = sum(1 for hypothesis in created if hypothesis.linked_claim_id is not None)
    assumption_count = sum(1 for hypothesis in created if hypothesis.linked_assumption_id is not None)
    milestone_count = sum(
        1
        for hypothesis in created
        if any(source_ref.kind is SourceKind.MILESTONE for source_ref in hypothesis.source_refs)
    )
    query_count = sum(1 for hypothesis in created if hypothesis.proposed_by == "bootstrap:kpi_query")
    prefix = "Bootstrap dry-run would create" if dry_run else "Bootstrap created"
    counts = f"claims={claim_count}, assumptions={assumption_count}, milestones={milestone_count}"
    if query_count:
        counts = f"{counts}, queries={query_count}"
    typer.echo(
        f"{prefix} {len(created)} proposal(s) for {program_id} "
        f"({counts})."
    )
    for hypothesis in created:
        typer.echo(f"- {hypothesis.short_id} | {hypothesis.kind.value} | {hypothesis.statement}")


def _seed_starter_assumptions_if_needed(
    program_id: str,
    *,
    programs_root: Path,
    claims: tuple[ClaimEntry, ...],
    assumptions: tuple[Assumption, ...],
) -> Path | None:
    if claims or assumptions:
        return None
    assumptions_path = get_assumptions_path(program_id, programs_root)
    assumptions_path.parent.mkdir(parents=True, exist_ok=True)
    template_text = _STARTER_ASSUMPTIONS_TEMPLATE.format(program_id=program_id)
    if assumptions_path.exists() and assumptions_path.read_text(encoding="utf-8") == template_text:
        return assumptions_path
    assumptions_path.write_text(template_text, encoding="utf-8")
    return assumptions_path


def _claim_sort_key(claim: ClaimEntry) -> tuple[datetime, int, str]:
    claim_datetime = datetime.combine(claim.claim_date, datetime.min.time(), tzinfo=timezone.utc)
    return claim_datetime, claim.issue_number, claim.id


def _assumption_sort_key(assumption: Assumption) -> tuple[datetime, str]:
    identified_datetime = datetime.combine(assumption.identified_date, datetime.min.time(), tzinfo=timezone.utc)
    return identified_datetime, assumption.id


def _milestone_sort_key(milestone) -> tuple[datetime, str]:
    target_datetime = datetime.combine(milestone.target_date, datetime.min.time(), tzinfo=timezone.utc)
    return target_datetime, milestone.id


def _query_sort_key(query: KustoQuery) -> tuple[str, str, tuple[str, ...]]:
    return query.id, query.metric_id or "", query.assertion_ids


def _render_assertion_statement(assertion: TelemetryAssertion) -> str:
    if assertion.description.strip():
        return assertion.description.strip()
    return f"{assertion.metric_id} should {_render_operator_phrase(assertion.operator, assertion.threshold)}."


def _render_operator_phrase(operator: AssertionOperator, threshold: float) -> str:
    if operator is AssertionOperator.GTE:
        return f"stay at or above {threshold:g}"
    if operator is AssertionOperator.LTE:
        return f"stay at or below {threshold:g}"
    if operator is AssertionOperator.EQ:
        return f"equal {threshold:g}"
    if operator is AssertionOperator.NEQ:
        return f"differ from {threshold:g}"
    if operator is AssertionOperator.PCT_IMPROVEMENT:
        return f"improve by at least {threshold:g}%"
    if operator is AssertionOperator.FORECAST_GTE:
        return f"project to at least {threshold:g} over the next window"
    if operator is AssertionOperator.FORECAST_LTE:
        return f"project to at most {threshold:g} over the next window"
    if operator is AssertionOperator.BURN_RATE_GTE:
        return f"burn down by at least {threshold:g} per window"
    if operator is AssertionOperator.BURN_RATE_LTE:
        return f"burn down by no more than {threshold:g} per window"
    return f"regress by no more than {threshold:g}%"


def _extract_linked_ado_item_id(claim: ClaimEntry) -> int | None:
    for entity_ref in claim.entity_refs:
        match = _WI_REF_PATTERN.fullmatch(entity_ref.strip())
        if match is not None:
            return int(match.group(1))
    return None