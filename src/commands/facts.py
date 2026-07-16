"""FR-SG-68: vertex facts export / import / rebuild commands."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
import json
import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import portalocker
from typing import Any
import uuid

import typer

from src.core.action_tracker import load_actions
from src.core.assumption_tracker import load_assumptions
from src.core.archive_store import ARCHIVE_ROOT, load_skipped_issues_for_program
from src.core.claim_tracker import load_claim_status_updates, load_open_claims, load_open_decision_asks
from src.core.decision_register import load_decisions
from src.core.edition_resolver import EDITIONS_ROOT, PROGRAMS_ROOT, load_program
from src.core.dependency_graph import load_dependencies
from src.core.milestone_engine import load_milestones
from src.core.operation_trace import REF_TYPE_FACT, record_trace_link
from src.core.program_fact_store import (
    ProgramFactEnvelope,
    ProgramFactInput,
    ProgramFactStore,
    load_program_facts,
    persist_program_fact_snapshot,
    FactPrecedence,
    FactReviewState,
    FactLifecycleState,
    project_action_items,
    project_assumptions,
    project_baseline_trust_events,
    project_claim_entries,
    project_claim_status_updates,
    project_decision_asks,
    project_decision_entries,
    project_dependencies,
    project_milestones,
    project_risk_entries,
    project_skip_issues,
    project_workstream_associations,
    project_workstreams,
)
from src.core.judgment_backfill import backfill_program
from src.core.risk_register_engine import load_risk_register
from src.core.trusted_baseline_store import load_trusted_baseline_for_program
from src.core.workstream_association_store import read_workstream_association_records
from src.core.workstream_documents import _parse_workstreams
from src.core.yaml_utils import load_yaml_mapping

app = typer.Typer(help="Manage program fact store (export, import, rebuild).")

_SUPPORTED_PARITY_FAMILIES = ("actions", "claims", "claim_status_updates", "decision_asks", "assumptions", "decisions", "risks", "dependencies", "milestones", "workstreams", "workstream_associations", "baseline_trust_events", "skip_issues")
_ZERO_TOLERANCE_FAMILIES = ("actions", "risks")
_PENDING_ZERO_TOLERANCE_FAMILIES = ("claims", "claim_status_updates", "decision_asks", "decisions", "workstream_associations", "baseline_trust_events", "skip_issues")


@dataclass(frozen=True, slots=True)
class FactParityFamilyResult:
    family: str
    legacy_count: int
    fact_store_count: int
    matched_count: int
    total_count: int
    matches: bool


@dataclass(frozen=True, slots=True)
class FactParityAssessment:
    program_id: str
    family_results: tuple[FactParityFamilyResult, ...]
    matched_count: int
    total_count: int
    parity_ratio: float
    zero_tolerance_failures: tuple[str, ...]
    pending_zero_tolerance_families: tuple[str, ...] = _PENDING_ZERO_TOLERANCE_FAMILIES

    @property
    def passed(self) -> bool:
        return self.parity_ratio >= 0.99 and not self.zero_tolerance_failures


@app.command("export")
def facts_export(
    program: str = typer.Option(..., "--program", help="Program id."),
    output: Path | None = typer.Option(None, "--output", help="Output JSON file path (default: stdout)."),
) -> None:
    """Export the current fact snapshot to JSON (ProgramFactEnvelope)."""
    snapshot = load_program_facts(program, programs_root=PROGRAMS_ROOT)
    facts_list: list[dict[str, Any]] = []
    for fact in snapshot.facts:
        facts_list.append(
            {
                "revision_id": fact.revision_id,
                "fact_id": fact.fact_id,
                "natural_key": fact.natural_key,
                "fact_type": fact.fact_type,
                "scope": fact.scope,
                "entity_refs": list(fact.entity_refs),
                "payload": fact.payload,
                "source_signal_ids": list(fact.source_signal_ids),
                "confidence": fact.confidence,
                "precedence": fact.precedence.value,
                "review_state": fact.review_state.value,
                "lifecycle_state": fact.lifecycle_state.value,
                "valid_from": fact.valid_from.isoformat() if fact.valid_from else None,
                "valid_until": fact.valid_until.isoformat() if fact.valid_until else None,
                "recorded_at": fact.recorded_at.isoformat(),
                "created_by": fact.created_by,
                "privacy_classification": fact.privacy_classification,
                "accepted_by": fact.accepted_by,
            }
        )
    envelope: ProgramFactEnvelope = {
        "program_id": snapshot.program_id,
        "as_of": snapshot.as_of.isoformat(),
        "fact_count": len(facts_list),
        "facts": facts_list,
    }
    payload_text = json.dumps(envelope, indent=2, ensure_ascii=False)
    if output is not None:
        output.write_text(payload_text, encoding="utf-8")
        typer.echo(f"Exported {len(facts_list)} facts for {program} → {output}")
    else:
        typer.echo(payload_text)


@app.command("import")
def facts_import(
    program: str = typer.Option(..., "--program", help="Program id."),
    input_file: Path = typer.Option(..., "--input", help="JSON file previously created by 'facts export'."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Parse and validate without writing."),
) -> None:
    """Import facts from a ProgramFactEnvelope JSON file into the fact store."""
    raw = json.loads(input_file.read_text(encoding="utf-8"))
    facts_raw: list[dict[str, Any]] = raw.get("facts") or []
    if dry_run:
        typer.echo(f"Dry-run: would import {len(facts_raw)} facts for {program}.")
        return

    store = ProgramFactStore(program)
    imported = 0
    skipped = 0
    now = datetime.now(timezone.utc)
    # ADF-W2.12: one correlation id for this whole import -- a single
    # invocation can write dozens/hundreds of facts from one file, a real
    # multi-fact chain worth tracing (unlike a single-item CLI mutation).
    correlation_id = uuid.uuid4().hex
    for item in facts_raw:
        try:
            fact_input = ProgramFactInput(
                fact_type=str(item["fact_type"]),
                entity_refs=tuple(item.get("entity_refs") or ()),
                payload=dict(item.get("payload") or {}),
                scope=str(item.get("scope") or "program"),
                source_signal_ids=tuple(item.get("source_signal_ids") or ()),
                confidence=item.get("confidence"),
                precedence=FactPrecedence(item.get("precedence", "active_pm_judgment")),
                review_state=FactReviewState(item.get("review_state", "accepted")),
                lifecycle_state=FactLifecycleState(item.get("lifecycle_state", "active")),
                valid_from=_parse_optional_dt(item.get("valid_from")),
                valid_until=_parse_optional_dt(item.get("valid_until")),
                projection_history=(),
                natural_key=item.get("natural_key"),
                created_by=str(item.get("created_by") or "vertex.facts.import"),
                privacy_classification=str(item.get("privacy_classification") or "internal"),
                accepted_by=item.get("accepted_by"),
            )
            write_result = store.append_fact(fact_input, recorded_at=now)
            imported += 1
            if write_result.action != "noop":
                try:
                    record_trace_link(
                        program_id=program,
                        correlation_id=correlation_id,
                        workflow_id=correlation_id,
                        run_id=correlation_id,
                        stage="fact",
                        ref_type=REF_TYPE_FACT,
                        ref_id=f"{fact_input.fact_type}:{write_result.revision.fact_id}@{now.isoformat()}",
                        programs_root=PROGRAMS_ROOT,
                    )
                except Exception:  # noqa: BLE001 -- a trace link is observability, never a write blocker.
                    pass
        except (KeyError, ValueError, TypeError):
            skipped += 1
    typer.echo(f"Imported {imported} facts for {program} ({skipped} skipped).")


@app.command("rebuild")
def facts_rebuild(
    program: str = typer.Option(..., "--program", help="Program id."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be rebuilt without writing."),
) -> None:
    """Rebuild the fact store from canonical program files."""
    now = datetime.now(timezone.utc)
    snapshot = load_program_facts(program, programs_root=PROGRAMS_ROOT)
    if dry_run:
        typer.echo(f"Dry-run: would rebuild {len(snapshot.facts)} facts for {program}.")
        return
    results = persist_program_fact_snapshot(snapshot, recorded_at=now, accepted_by="vertex.facts.rebuild")
    written = sum(1 for r in results if r.action in ("inserted", "updated"))
    typer.echo(f"Rebuilt {written} fact(s) for {program} ({len(results)} total).")


@app.command("parity-check")
def facts_parity_check(
    program: str = typer.Option(..., "--program", help="Program id."),
) -> None:
    """Compare current legacy projections against current fact-store projections."""
    assessment = run_facts_parity_check(program_id=program, programs_root=PROGRAMS_ROOT)
    pending = ",".join(assessment.pending_zero_tolerance_families) or "none"
    mismatches = ", ".join(result.family for result in assessment.family_results if not result.matches) or "none"
    typer.echo(
        f"Parity {assessment.program_id} | matched {assessment.matched_count}/{assessment.total_count} items | "
        f"ratio={assessment.parity_ratio:.2%} | mismatches={mismatches} | "
        f"zero_tolerance_failures={','.join(assessment.zero_tolerance_failures) or 'none'} | "
        f"pending_zero_tolerance={pending}"
    )
    raise typer.Exit(code=0 if assessment.passed else 1)


def run_facts_parity_check(
    *,
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
    editions_root: Path = EDITIONS_ROOT,
    archive_root: Path = ARCHIVE_ROOT,
    db_root: Path | None = None,
) -> FactParityAssessment:
    program = load_program(program_id, programs_root=programs_root)
    if program is None:
        raise typer.BadParameter(f"Program '{program_id}' was not found.")
    as_of = datetime.now(timezone.utc)
    fact_snapshot = load_program_facts(
        program_id,
        as_of=as_of,
        db_root=db_root,
        programs_root=programs_root,
        editions_root=editions_root,
        archive_root=archive_root,
    )
    workstreams_path = programs_root / program_id / "workstreams.yaml"
    legacy_workstreams = _parse_workstreams(load_yaml_mapping(workstreams_path), workstreams_path) if workstreams_path.exists() else ()
    trusted_baseline = load_trusted_baseline_for_program(program_id, programs_root=programs_root)
    legacy_skip_issues = load_skipped_issues_for_program(
        program_id,
        editions_root=editions_root,
        archive_root=archive_root,
    )
    families = (
        ("actions", load_actions(program_id, programs_root=programs_root), project_action_items(fact_snapshot)),
        ("claims", load_open_claims(program_id, programs_root=programs_root), project_claim_entries(fact_snapshot)),
        (
            "claim_status_updates",
            load_claim_status_updates(program_id, programs_root=programs_root),
            project_claim_status_updates(fact_snapshot),
        ),
        ("decision_asks", load_open_decision_asks(program_id, programs_root=programs_root), project_decision_asks(fact_snapshot)),
        ("assumptions", load_assumptions(program_id, programs_root=programs_root), project_assumptions(fact_snapshot)),
        ("decisions", load_decisions(program_id, programs_root=programs_root), project_decision_entries(fact_snapshot)),
        ("risks", load_risk_register(program_id, programs_root=programs_root), project_risk_entries(fact_snapshot)),
        ("dependencies", load_dependencies(program_id, programs_root=programs_root), project_dependencies(fact_snapshot)),
        ("milestones", load_milestones(program_id, programs_root=programs_root), project_milestones(fact_snapshot)),
        ("workstreams", legacy_workstreams, project_workstreams(fact_snapshot)),
        (
            "workstream_associations",
            read_workstream_association_records(program_id, programs_root=programs_root),
            project_workstream_associations(fact_snapshot),
        ),
        ("baseline_trust_events", (() if trusted_baseline is None else trusted_baseline.history), project_baseline_trust_events(fact_snapshot)),
        ("skip_issues", legacy_skip_issues, project_skip_issues(fact_snapshot)),
    )
    family_results = tuple(_build_family_result(name, legacy_items, fact_store_items) for name, legacy_items, fact_store_items in families)
    matched_count = sum(result.matched_count for result in family_results)
    total_count = sum(result.total_count for result in family_results)
    parity_ratio = 1.0 if total_count == 0 else matched_count / total_count
    zero_tolerance_failures = tuple(
        result.family
        for result in family_results
        if result.family in _ZERO_TOLERANCE_FAMILIES and not result.matches
    )
    return FactParityAssessment(
        program_id=program_id,
        family_results=family_results,
        matched_count=matched_count,
        total_count=total_count,
        parity_ratio=parity_ratio,
        zero_tolerance_failures=zero_tolerance_failures,
    )


@app.command("pin-snapshot")
def facts_pin_snapshot(
    program: str = typer.Option(..., "--program", help="Program id."),
    issue_number: int = typer.Option(..., "--issue-number", help="Confirmed issue number to pin the fact snapshot to."),
    db_root: Path | None = typer.Option(None, "--db-root", hidden=True),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    """Pin the current fact snapshot to a confirmed issue (spec §22, Step 8).

    Creates a row in the ``fact_snapshot_pins`` table with the current
    fact-snapshot ID and the issue number.  ``detect_drift(snapshot_id)`` then
    reports any post-pin fact writes as material drift, so the operator can
    prove that a confirmed issue's fact-state was not retroactively changed.
    """
    store = ProgramFactStore(program, db_root=db_root or programs_root.parent)
    store.initialize()
    pin = store.pin_snapshot(metadata={"issue_number": issue_number})
    typer.echo(f"Pinned fact snapshot for {program} @ issue #{issue_number} → {pin.snapshot_id}")
    raise typer.Exit(code=0)


@app.command("detect-drift")
def facts_detect_drift(
    program: str = typer.Option(..., "--program", help="Program id."),
    snapshot_id: str = typer.Option(..., "--snapshot-id", help="Pin ID returned by `pin-snapshot`."),
    db_root: Path | None = typer.Option(None, "--db-root", hidden=True),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    """List fact revisions that drifted after a pin (spec §22, Step 8)."""
    store = ProgramFactStore(program, db_root=db_root or programs_root.parent)
    drift = store.detect_drift(snapshot_id)
    typer.echo(f"Drift since {snapshot_id}: {len(drift)} revision(s)")
    for revision in drift:
        typer.echo(f"  {revision.fact_type} @ {revision.recorded_at.isoformat()} (revision_id={revision.revision_id})")
    raise typer.Exit(code=0 if not drift else 2)


@app.command("dual-read-log")
def facts_dual_read_log(
    program: str = typer.Option(..., "--program", help="Program id."),
    cycles: int = typer.Option(2, "--cycles", min=1, help="Number of parity-check cycles to run in this window."),
    interval_seconds: float = typer.Option(0.0, "--interval", help="Sleep between cycles (seconds)."),
    quarantine: bool = typer.Option(True, "--quarantine/--no-quarantine", help="Write mismatched family facts to a quarantine JSONL file."),
    db_root: Path | None = typer.Option(None, "--db-root", hidden=True),
    archive_root: Path = typer.Option(ARCHIVE_ROOT, "--archive-root", hidden=True),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
    editions_root: Path = typer.Option(EDITIONS_ROOT, "--editions-root", hidden=True),
) -> None:
    """Sustained dual-read shadow window per spec §22.

    Runs ``cycles`` parity-check passes (legacy + Fact Store) and appends one
    JSONL record per cycle to ``programs/<prog>/fact_store_parity_log.jsonl``.
    Mismatched family items (if --quarantine) are appended to a sibling
    ``fact_store_quarantine.jsonl`` for offline review.  Operator-run, not a
    daemon: the spec calls for a *minimum* of 2 full confirmed cycles, so
    operators run this command between confirm runs as the live proof artifact.
    """
    import time

    quarantine_path = _dual_read_quarantine_path(program, programs_root=programs_root)
    log_path = _dual_read_log_path(program, programs_root=programs_root)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cycle_summaries: list[dict[str, Any]] = []
    for cycle_index in range(1, cycles + 1):
        assessment = run_facts_parity_check(
            program_id=program,
            programs_root=programs_root,
            editions_root=editions_root,
            archive_root=archive_root,
            db_root=db_root,
        )
        mismatches = tuple(result for result in assessment.family_results if not result.matches)
        if quarantine and mismatches:
            _append_dual_read_quarantine(
                quarantine_path,
                program_id=program,
                cycle_index=cycle_index,
                mismatched_families=tuple(
                    {
                        "family": result.family,
                        "legacy_count": result.legacy_count,
                        "fact_store_count": result.fact_store_count,
                        "matched_count": result.matched_count,
                        "total_count": result.total_count,
                    }
                    for result in mismatches
                ),
            )
        record = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "program_id": program,
            "cycle_index": cycle_index,
            "matched_count": assessment.matched_count,
            "total_count": assessment.total_count,
            "parity_ratio": round(assessment.parity_ratio, 4),
            "passed": assessment.passed,
            "zero_tolerance_failures": list(assessment.zero_tolerance_failures),
            "mismatched_families": [result.family for result in mismatches],
            "family_results": [
                {
                    "family": result.family,
                    "legacy_count": result.legacy_count,
                    "fact_store_count": result.fact_store_count,
                    "matched_count": result.matched_count,
                    "total_count": result.total_count,
                    "matches": result.matches,
                }
                for result in assessment.family_results
            ],
        }
        with log_path.open("a", encoding="utf-8") as handle:
            portalocker.lock(handle, portalocker.LOCK_EX)
            try:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                portalocker.unlock(handle)
        cycle_summaries.append(record)
        if cycle_index < cycles and interval_seconds > 0:
            time.sleep(interval_seconds)

    typer.echo(
        f"Dual-read shadow window: program={program} cycles={cycles} "
        f"final_ratio={cycle_summaries[-1]['parity_ratio']:.2%} "
        f"zero_tolerance_failures={','.join(cycle_summaries[-1]['zero_tolerance_failures']) or 'none'}"
    )
    typer.echo(f"Log: {log_path}")
    if quarantine and any(s["mismatched_families"] for s in cycle_summaries):
        typer.echo(f"Quarantine: {quarantine_path}")
    passed_all = all(summary["passed"] for summary in cycle_summaries)
    raise typer.Exit(code=0 if passed_all else 1)


def _dual_read_log_path(program_id: str, *, programs_root: Path) -> Path:
    return programs_root / program_id / "fact_store_parity_log.jsonl"


def _dual_read_quarantine_path(program_id: str, *, programs_root: Path) -> Path:
    return programs_root / program_id / "fact_store_quarantine.jsonl"


def _append_dual_read_quarantine(
    quarantine_path: Path,
    *,
    program_id: str,
    cycle_index: int,
    mismatched_families: tuple[dict[str, Any], ...],
) -> None:
    """Append per-cycle mismatch records to a JSONL quarantine log.

    The quarantine log captures *which* family mismatched in each cycle and the
    matched/legacy/fact_store counts, so an operator can later diff against
    the fact_store_parity_log.jsonl to confirm the parity check is stable
    across the dual-read window.
    """
    if not mismatched_families:
        return
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "program_id": program_id,
        "cycle_index": cycle_index,
        "mismatched_families": list(mismatched_families),
    }
    with quarantine_path.open("a", encoding="utf-8") as handle:
        portalocker.lock(handle, portalocker.LOCK_EX)
        try:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            portalocker.unlock(handle)


def _build_family_result(
    family: str,
    legacy_items: tuple[object, ...],
    fact_store_items: tuple[object, ...],
) -> FactParityFamilyResult:
    legacy_counter = Counter(_canonicalize_items(legacy_items))
    fact_store_counter = Counter(_canonicalize_items(fact_store_items))
    matched_count = sum((legacy_counter & fact_store_counter).values())
    total_count = sum((legacy_counter | fact_store_counter).values())
    return FactParityFamilyResult(
        family=family,
        legacy_count=len(legacy_items),
        fact_store_count=len(fact_store_items),
        matched_count=matched_count,
        total_count=total_count,
        matches=legacy_counter == fact_store_counter,
    )


def _canonicalize_items(items: tuple[object, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            json.dumps(_normalize_value(item), sort_keys=True)
            for item in items
        )
    )


def _normalize_value(value: Any) -> Any:
    if is_dataclass(value):
        return {
            field.name: _normalize_value(getattr(value, field.name))
            for field in fields(value)
            if field.name not in {"fact_id", "last_validated_at"}
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_normalize_value(item) for item in value]
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_value(item) for key, item in value.items()}
    return value


def _parse_optional_dt(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# WI-3.8: vertex facts backfill-observations (non-blocking)
# ---------------------------------------------------------------------------

@app.command("backfill-observations")
def facts_backfill_observations_command(
    program: str = typer.Option(..., "--program", help="Program id."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would be backfilled without writing."),
    limit: int = typer.Option(500, "--limit", help="Max number of facts to backfill in one run."),
    db_root: Path | None = typer.Option(None, hidden=True),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
) -> None:
    """Backfill signal.observation facts from archive/extractor data (WI-3.8).

    Reads existing program facts and re-promotes them as signal.observation
    records. All backfilled facts are:
    - tagged `backfilled: true` in the payload
    - truth-capped at SOURCE_VALIDATED (never CORROBORATED or above)
    - written via append_fact (idempotent)

    This command NEVER blocks Phase 4+ work — it is a convenience backfill
    for populating the observation layer from existing tracked data.
    """
    from src.core.program_fact_store import build_natural_key
    from src.core.signal_promotion import promote_observation

    normalized_program = program.strip()
    if not normalized_program:
        raise typer.BadParameter("--program must be non-empty")

    snapshot = load_program_facts(normalized_program, programs_root=programs_root, db_root=db_root)

    # Collect facts eligible for backfill — management domain facts that have
    # a clear ADO/system source and are not already signal.observation
    backfill_candidates = [
        fact for fact in snapshot.facts
        if fact.fact_type not in (
            "signal.observation",
            "fact.reconfirmation",
            "fact.conflict",
            "fact.corroboration",
            "trust.source_score",
            "trust.bootstrap_grant",
            "entity.alias",
        )
        and str(fact.review_state) == "accepted"
        and str(fact.lifecycle_state) == "active"
    ][:limit]

    if dry_run:
        typer.echo(f"[dry-run] Would backfill {len(backfill_candidates)} observation(s) for {normalized_program}.")
        for fact in backfill_candidates[:5]:
            typer.echo(f"  - {fact.fact_type} | {fact.natural_key}")
        if len(backfill_candidates) > 5:
            typer.echo(f"  ... and {len(backfill_candidates) - 5} more.")
        raise typer.Exit(code=0)

    created = 0
    reconfirmed = 0
    skipped = 0
    for fact in backfill_candidates:
        # Build observation payload with backfill tag
        obs_payload = {
            "fact_type": fact.fact_type,
            "natural_key": fact.natural_key,
            "source_fact_id": fact.fact_id,
            "backfilled": True,
        }
        result = promote_observation(
            program_id=normalized_program,
            fact_type="signal.observation",
            entity_refs=fact.entity_refs,
            payload=obs_payload,
            source_family="ado",
            scope=fact.scope,
            db_root=db_root,
        )
        if result.action == "created":
            created += 1
        elif result.action == "reconfirmed":
            reconfirmed += 1
        else:
            skipped += 1

    typer.echo(
        f"Backfill complete: created={created} reconfirmed={reconfirmed} skipped={skipped} "
        f"(program={normalized_program})"
    )
    raise typer.Exit(code=0)


@app.command("backfill-judgments")
def facts_backfill_judgments(
    program: str = typer.Option(..., "--program", help="Program id."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Discover and print judgments without writing them to the fact store.",
    ),
    decided_by: str = typer.Option(
        "vertex.backfill",
        "--decided-by",
        hidden=True,
        help="Author recorded on each backfilled judgment fact.",
    ),
    home_root: Path | None = typer.Option(None, "--home-root", hidden=True),
    db_root: Path | None = typer.Option(None, "--db-root", hidden=True),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    """GAP-34 (F4): backfill override risk choices into the fact store as ``judgment.dimension`` facts.

    Scans ``programs/<id>/**/overrides/issue_*.yaml`` (including archived
    per-edition overrides), extracts one ``Judgment`` per non-needs-input
    dimension, and — unless ``--dry-run`` — appends each as a
    ``judgment.dimension`` fact via the canonical Program Fact Store append
    path. Re-runs are idempotent: the fact natural key encodes
    ``program | issue | edition | dimension``.
    """
    program_dir = programs_root / program
    if not program_dir.exists():
        typer.echo(f"Program directory not found: {program_dir}")
        raise typer.Exit(code=2)

    extractions = backfill_program(
        program,
        program_dir=program_dir,
        home_root=home_root,
        db_root=db_root,
        apply=not dry_run,
    )
    total = sum(len(ex.judgments) for ex in extractions)

    label = "[dry-run] " if dry_run else ""
    typer.echo(
        f"{label}Discovered {total} judgment(s) across {len(extractions)} "
        f"overrides file(s) for program={program}"
    )
    for ex in extractions:
        for judgment in ex.judgments:
            typer.echo(
                f"  issue={ex.issue_number} edition={ex.edition_id or '-'} "
                f"dimension={judgment.dimension} risk={judgment.risk_level}"
            )

    if not dry_run:
        typer.echo(f"Backfill complete: written={total} (program={program})")
    raise typer.Exit(code=0)
