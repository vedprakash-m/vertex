from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.evidence_models import WorkstreamEvidence

import typer

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.commands.doctor_checks.yaml_support import check_schema_versions, load_yaml_document
from src.core.context_gap_store import append_context_gap, load_context_gaps, rank_context_gaps
from src.core.edition_resolver import resolve_edition
from src.core.ncfl_proposal_store import conflicting_pending_proposals, load_proposals, stale_pending_proposals
from src.commands.doctor_checks.evidence_checks import check_eta_slippage, check_false_done_lanes, check_evidence_quality_drift
from src.core.evidence_models import parse_workiq_latest_date
from src.core.exceptions import ConfigError
from src.core.program_context import InvariantSeverity, load_program_context


_FIX_HINT_MAP: dict[str, str] = {
    "WS-01": "Fix: ensure sub_program_id in workstream_registry.yaml matches a sub_program id in program.yaml sub_programs[].",
    "WS-02": "Fix: add the workstream id to workstream_registry.yaml workstreams[], or remove it from workstreams.yaml.",
    "WS-03": "Fix: add the workstream id to workstreams.yaml, or remove it from workstream_registry.yaml.",
    "MS-01": "Fix: ensure the milestone's workstream_id matches an entry in workstreams.yaml.",
    "MS-02": "Fix: set target_date to a valid ISO-8601 date in milestones.yaml.",
    "MS-03": "Fix: replace stub WI IDs (9xxxxx placeholder range) with real ADO work item IDs in milestones.yaml.",
    "RISK-01": "Fix: ensure the risk's workstream_id matches an entry in workstreams.yaml.",
    "DEP-01": "Fix: ensure the dependency's program_id matches a known program directory.",
    "DEP-02": "Fix: ensure the dependency's workstream_id matches an entry in workstreams.yaml.",
    "ACT-01": "Fix: ensure the action's workstream_id matches an entry in workstreams.yaml.",
    "DEC-01": "Fix: ensure the decision's workstream_id matches an entry in workstreams.yaml.",
    "STK-01": "Fix: add the alias to stakeholder_register in program.yaml, or correct the RACI reference.",
    "STK-02": "Fix: add the alias to stakeholder_register in program.yaml, or correct the role reference.",
    "STK-04": "Fix: reconcile the stakeholder's role/email between top-level stakeholder_register and charter.stakeholder_register in program.yaml -- charter is canonical (specs/people.md §5.6 item 2); update or remove the top-level entry.",
    "DATE-01": "Fix: replace stub WI IDs (9xxxxx placeholder range) with real ADO work item IDs in milestones.yaml linked_work_item_ids.",
    "FILTER-01": "Fix: replace informal OData filter values with their formal enum equivalents (e.g. 'done' -> 'Completed').",
    "KB-01": "Fix: ensure the referenced file path exists under programs/<prog>/; update program.yaml if the path has changed.",
    "KB-02": "Fix: set schema_version in the file header; check specs/program-context-maturity.md §4 for required versions.",
    "KB-03": "Fix: ensure the edition YAML references a valid program_id that matches a directory under programs/.",
}


def run_ranked_gaps(*, edition_name: str | None, programs_root: Path, editions_root: Path) -> None:
    """Render ranked context gap report from _feedback/context_gaps.jsonl."""
    program_id = _resolve_context_program_id(
        edition_name=edition_name,
        programs_root=programs_root,
        editions_root=editions_root,
    )
    if program_id is None:
        return

    gaps = load_context_gaps(program_id, programs_root=programs_root)
    if not gaps:
        typer.echo(typer.style("No context gaps recorded yet.", fg="green"))
        typer.echo("Run gather or propose to accumulate gap signals in _feedback/context_gaps.jsonl.")
        return

    ranked = rank_context_gaps(gaps)

    typer.echo(typer.style("TOP CONTEXT GAPS (by impact)", bold=True, fg="cyan"))
    typer.echo()

    current_impact: str | None = None
    for gap in ranked:
        if gap.impact_estimate != current_impact:
            current_impact = gap.impact_estimate
            label = gap.impact_estimate.upper()
            color = {"high": "red", "medium": "yellow", "low": "green"}.get(gap.impact_estimate, "white")
            typer.echo(typer.style(f"  [{label}]", bold=True, fg=color))

        lane_part = f" / {gap.lane}" if gap.lane else ""
        typer.echo(f"    {gap.program}{lane_part}  {gap.field} — missing for {gap.count} gather cycle{'s' if gap.count != 1 else ''}")
        typer.echo(f"    > Impact: {gap.message}")
        typer.echo(f"    > Fix: {gap.fix_hint}")
        typer.echo()

    typer.echo(f"Total: {len(ranked)} unique gaps across {len(gaps)} total observations.")


def run_context_doctor(
    *,
    edition_name: str | None,
    programs_root: Path,
    editions_root: Path,
    fix_hints: bool = False,
) -> DoctorReport:
    """Validate cross-file program context invariants and staleness policy."""
    today = datetime.now(timezone.utc).date()
    checks: list[DoctorCheck] = []

    if edition_name:
        try:
            resolved = resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)
            if resolved is None:
                return DoctorReport(
                    edition=edition_name,
                    checks=(DoctorCheck("Context", "fail", f"Edition '{edition_name}' could not be resolved."),),
                )
            program_id = resolved.paths.program_id
        except (ConfigError, OSError, ValueError):
            return DoctorReport(
                edition=edition_name or "unknown",
                checks=(DoctorCheck("Context", "fail", f"Could not resolve edition '{edition_name}'."),),
            )
    else:
        program_dirs = [p for p in programs_root.iterdir() if (p / "program.yaml").exists()] if programs_root.exists() else []
        if len(program_dirs) == 1:
            program_id = program_dirs[0].name
        elif not program_dirs:
            return DoctorReport(
                edition="context",
                checks=(DoctorCheck("Context", "fail", "No program.yaml found in programs/."),),
            )
        else:
            return DoctorReport(
                edition="context",
                checks=(DoctorCheck("Context", "fail", "Specify --edition when multiple programs exist."),),
            )

    program_dir = programs_root / program_id

    try:
        ctx = load_program_context(
            program_id,
            programs_root=programs_root,
            editions_root=editions_root,
            today=today,
            raise_on_error=False,
        )
    except (OSError, ValueError) as exc:
        return DoctorReport(
            edition=program_id,
            checks=(DoctorCheck("Context", "fail", f"Failed to load program context: {exc}"),),
        )

    maturity_level = ctx.maturity_level.value
    checks.append(DoctorCheck(
        "Maturity level",
        "ok" if maturity_level >= 2 else "warn",
        f"L{maturity_level} ({level_name(maturity_level)})"
        + (
            f" — {len(ctx.maturity_blockers)} blocker{'s' if len(ctx.maturity_blockers) != 1 else ''} to L{maturity_level + 1}"
            if ctx.maturity_blockers else " — no blockers"
        ),
    ))
    for blocker in ctx.maturity_blockers[:3]:
        checks.append(DoctorCheck("Maturity blocker", "warn", blocker))
    if len(ctx.maturity_blockers) > 3:
        checks.append(DoctorCheck("Maturity blocker", "warn", f"+{len(ctx.maturity_blockers) - 3} more blockers to next level"))

    _, schema_errors = check_schema_versions(program_dir)
    if schema_errors:
        checks.append(DoctorCheck("Schema", "fail", "; ".join(schema_errors[:3])))
    else:
        checks.append(DoctorCheck("Schema", "ok", "All program files have valid schema_version."))

    error_violations = [violation for violation in ctx.invariant_violations if violation.severity == InvariantSeverity.ERROR]
    warn_violations = [violation for violation in ctx.invariant_violations if violation.severity == InvariantSeverity.WARN]

    if error_violations:
        top = error_violations[:3]
        remaining = len(error_violations) - 3
        summary = "; ".join(f"[{violation.code}] {violation.detail}" for violation in top)
        if remaining > 0:
            summary += f"; +{remaining} more error{'s' if remaining != 1 else ''}"
        checks.append(DoctorCheck(
            "Cross-file invariants",
            "fail",
            f"{len(error_violations)} error{'s' if len(error_violations) != 1 else ''}: {summary}",
        ))
    else:
        checks.append(DoctorCheck("Cross-file invariants", "ok", "0 errors"))

    if warn_violations:
        first = warn_violations[0]
        checks.append(DoctorCheck(
            "Invariant warnings",
            "warn",
            f"{len(warn_violations)} warning{'s' if len(warn_violations) != 1 else ''}: [{first.code}] {first.detail}"
            + (f"; +{len(warn_violations) - 1} more" if len(warn_violations) > 1 else ""),
        ))

    stale_flags = ctx.staleness_flags
    if stale_flags:
        first_flag = stale_flags[0]
        entity = f" / {first_flag.entity_id}" if first_flag.entity_id else ""
        flag_desc = f"{first_flag.file}{entity}: {first_flag.days_stale}d stale ({first_flag.field})"
        checks.append(DoctorCheck(
            "Staleness",
            "warn",
            f"{len(stale_flags)} stale {'file' if len(stale_flags) == 1 else 'files'}: {flag_desc}"
            + (f"; +{len(stale_flags) - 1} more" if len(stale_flags) > 1 else ""),
        ))
    else:
        checks.append(DoctorCheck("Staleness", "ok", "0 warnings"))

    pending_context_proposals = load_proposals(
        program_id,
        status_filter={"pending"},
        programs_root=programs_root,
    )
    pending_conflicts = conflicting_pending_proposals(program_id, programs_root=programs_root)
    stale_pending = stale_pending_proposals(program_id, programs_root=programs_root)
    if pending_context_proposals:
        issue_numbers = sorted({proposal.issue_number for proposal in pending_context_proposals})
        issue_list = ", ".join(f"{issue:03d}" for issue in issue_numbers[:5])
        if len(issue_numbers) > 5:
            issue_list += f", +{len(issue_numbers) - 5} more"
        detail = (
            f"{len(pending_context_proposals)} pending context proposal"
            f"{'s' if len(pending_context_proposals) != 1 else ''} for issue"
            f"{'s' if len(issue_numbers) != 1 else ''} [{issue_list}]. "
            "Review with `vertex context proposals --edition <name>`."
        )
        if pending_conflicts:
            detail += f" {len(pending_conflicts)} cross-issue conflict key{'s' if len(pending_conflicts) != 1 else ''} need resolution."
        if stale_pending:
            stale_issue_numbers = sorted({proposal.issue_number for proposal in stale_pending})
            stale_issue_list = ", ".join(f"{issue:03d}" for issue in stale_issue_numbers[:5])
            if len(stale_issue_numbers) > 5:
                stale_issue_list += f", +{len(stale_issue_numbers) - 5} more"
            detail += (
                f" {len(stale_pending)} proposal{'s are' if len(stale_pending) != 1 else ' is'}"
                f" stale (>2 issues old) from issue{'s' if len(stale_issue_numbers) != 1 else ''} [{stale_issue_list}]."
            )
            append_context_gap(
                feature="doctor --context",
                program=program_id,
                lane=None,
                field="ncfl.pending_proposals",
                severity="quality_degraded",
                message=(
                    f"{len(stale_pending)} NCFL proposal{'s are' if len(stale_pending) != 1 else ' is'} pending"
                    f" for more than 2 issues; oldest pending issue {min(stale_issue_numbers):03d}."
                ),
                impact_estimate="medium",
                programs_root=programs_root,
            )
        checks.append(DoctorCheck("NCFL proposals", "warn", detail))
    else:
        checks.append(DoctorCheck("NCFL proposals", "ok", "0 pending context proposals"))

    registry_path = program_dir / "workstream_registry.yaml"
    registry_entries = (
        load_yaml_document(registry_path).get("workstreams", [])
        if registry_path.exists() else []
    )

    # BL-09: detect empty workstream_registry
    if registry_path.exists() and not registry_entries:
        checks.append(DoctorCheck(
            "Registry",
            "fail",
            "workstream_registry.yaml exists but defines 0 active workstreams. "
            "Run `vertex registry scaffold --program <prog>` or populate manually.",
        ))

    # BL-10/BL-11: validate name field on registry lanes
    lanes_missing_name = [entry.get("id", "?") for entry in registry_entries if not entry.get("name")]
    if lanes_missing_name:
        checks.append(DoctorCheck(
            "Registry",
            "warn",
            f"{len(lanes_missing_name)} registry {'lane is' if len(lanes_missing_name) == 1 else 'lanes are'} "
            f"missing field 'name': {', '.join(lanes_missing_name[:3])}"
            + (f"; +{len(lanes_missing_name) - 3} more" if len(lanes_missing_name) > 3 else "")
            + ". Add `name: \"<Lane Name>\"` to avoid report-time errors if slice derivation fails.",
        ))

    kpis_list = (
        load_yaml_document(program_dir / "kpis.yaml").get("kpis", [])
        if (program_dir / "kpis.yaml").exists() else []
    )
    emit_context_gaps(
        program_id=program_id,
        registry_entries=registry_entries,
        kpis_list=kpis_list,
        today=today,
    )

    cadence_issues = check_evidence_cadence_gaps(registry_entries, as_of=today)
    checks.extend(cadence_issues)

    slippage_issues = check_eta_slippage(registry_entries, as_of=today)
    checks.extend(slippage_issues)

    false_done_issues = check_false_done_lanes(registry_entries, enrichments_by_lane={}, as_of=today)
    checks.extend(false_done_issues)

    # ME-02: load extracted evidence (if available) and pass to doctor checks
    evidence_by_lane = _load_evidence_store(program_dir)
    if evidence_by_lane:
        checks.extend(check_eta_slippage(
            registry_entries, as_of=today, evidence_override=evidence_by_lane,
        ))

    # ME-05: quality drift detection
    quality_issues = check_evidence_quality_drift(
        program_id=program_id,
        programs_root=programs_root,
        as_of=today,
    )
    checks.extend(quality_issues)

    # P4-4 (spec §14.1): WorkIQ/M365 doctor gates QG-WIQ-5/6/8/9.
    checks.extend(_run_workiq_doctor_checks(
        program_id=program_id,
        programs_root=programs_root,
        registry_entries=registry_entries,
        evidence_by_lane=evidence_by_lane,
        today=today,
    ))

    empty_deep = [entry.get("id", "?") for entry in registry_entries if not entry.get("deep_context", {}).get("why")]
    if empty_deep:
        checks.append(DoctorCheck(
            "Coverage",
            "info",
            f"{len(empty_deep)} registry {'entries' if len(empty_deep) != 1 else 'entry'} with empty deep_context: {', '.join(empty_deep[:3])}"
            + (f"; +{len(empty_deep) - 3} more" if len(empty_deep) > 3 else ""),
        ))

    unvalidated_kpis = [kpi.get("id", "?") for kpi in kpis_list if not kpi.get("validated", True)]
    if unvalidated_kpis:
        checks.append(DoctorCheck(
            "Coverage",
            "info",
            f"{len(unvalidated_kpis)} KPI{'s' if len(unvalidated_kpis) != 1 else ''} with validated=false: {', '.join(unvalidated_kpis[:3])}",
        ))

    if fix_hints:
        for violation in ctx.invariant_violations:
            hint = _FIX_HINT_MAP.get(violation.code, f"Fix: review [{violation.code}] {violation.detail}")
            checks.append(DoctorCheck("Fix hint", "info", f"[{violation.code}] {hint}"))
        for flag in ctx.staleness_flags:
            entity_label = f" / {flag.entity_id}" if flag.entity_id else ""
            checks.append(DoctorCheck(
                "Fix hint",
                "info",
                f"[Staleness] {flag.file}{entity_label}: set {flag.field} to today's date after reviewing.",
            ))

    return DoctorReport(edition=program_id, checks=tuple(checks))


def check_evidence_cadence_gaps(
    registry_entries_raw: list[dict],
    as_of: date,
) -> list[DoctorCheck]:
    """Emit WARN when a lane with expected_cadence_days has gone stale beyond cadence × 1.5."""
    issues: list[DoctorCheck] = []
    for entry in registry_entries_raw:
        cadence = entry.get("expected_cadence_days")
        if not isinstance(cadence, int) or cadence <= 0:
            continue
        lane_id = entry.get("id", "?")
        wl = entry.get("workiq_latest", "")
        if not isinstance(wl, str):
            continue
        wl_date = parse_workiq_latest_date(wl)
        if wl_date is None:
            continue
        days_stale = (as_of - wl_date).days
        threshold = int(cadence * 1.5)
        if days_stale > threshold:
            issues.append(DoctorCheck(
                label="Evidence cadence",
                status="warn",
                detail=(
                    f"[EVIDENCE_CADENCE_GAP] Lane '{lane_id}' expected evidence every {cadence}d; "
                    f"last evidence {days_stale}d ago (threshold: {threshold}d). "
                    f"The source may have stopped publishing — investigate."
                ),
            ))
    return issues


def emit_context_gaps(
    *,
    program_id: str,
    registry_entries: list[dict[str, Any]],
    kpis_list: list[dict[str, Any]],
    today: date,
) -> None:
    """Detect context gaps and append them to _feedback/context_gaps.jsonl."""
    feature = "doctor --context"

    for entry in registry_entries:
        ws_id = entry.get("id", "?")
        lane = ws_id
        deep_ctx = entry.get("deep_context", {})
        if not deep_ctx.get("why"):
            append_context_gap(
                feature=feature,
                program=program_id,
                lane=lane,
                field="deep_context.why",
                severity="quality_degraded",
                message="AI proposals for this lane use generic context",
                impact_estimate="high",
            )
        if not deep_ctx.get("what"):
            append_context_gap(
                feature=feature,
                program=program_id,
                lane=lane,
                field="deep_context.what",
                severity="quality_degraded",
                message="AI proposals for this lane lack specific content description",
                impact_estimate="medium",
            )

    for entry in registry_entries:
        ws_id = entry.get("id", "?")
        lane = ws_id
        roles = entry.get("roles", [])
        primary_owner = next((role for role in roles if role.get("role") == "primary_owner"), None)
        if primary_owner and not primary_owner.get("email"):
            append_context_gap(
                feature=feature,
                program=program_id,
                lane=lane,
                field="roles.primary_owner.email",
                severity="quality_degraded",
                message="Nudge routing will use alias fallback instead of direct email",
                impact_estimate="medium",
            )

    for entry in registry_entries:
        ws_id = entry.get("id", "?")
        lane = ws_id
        workiq_latest = entry.get("workiq_latest", "")
        if workiq_latest and isinstance(workiq_latest, str) and workiq_latest.startswith("2"):
            try:
                workiq_date = date.fromisoformat(workiq_latest.split(":")[0].strip()[:10])
                days_stale = (today - workiq_date).days
                if days_stale > 7:
                    append_context_gap(
                        feature=feature,
                        program=program_id,
                        lane=lane,
                        field="workiq_latest",
                        severity="quality_degraded",
                        message=f"M365 meeting evidence is {days_stale} days stale for diagnostics section",
                        impact_estimate="medium",
                    )
            except ValueError:
                pass

    for kpi in kpis_list:
        if not kpi.get("validated", True):
            append_context_gap(
                feature=feature,
                program=program_id,
                lane=None,
                field="kpis.validated",
                severity="quality_degraded",
                message="Live metric fetch may fail silently on gather",
                impact_estimate="medium",
            )


def level_name(level: int) -> str:
    return {0: "Skeleton", 1: "Structural", 2: "Operational", 3: "Intelligent", 4: "Self-Sustaining"}.get(level, "Unknown")


def _resolve_context_program_id(
    *,
    edition_name: str | None,
    programs_root: Path,
    editions_root: Path,
) -> str | None:
    if edition_name:
        try:
            resolved = resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)
            if resolved is None:
                typer.echo(typer.style(f"Edition '{edition_name}' could not be resolved.", fg="red"))
                return None
            return resolved.paths.program_id
        except (ConfigError, OSError, ValueError):
            typer.echo(typer.style(f"Could not resolve edition '{edition_name}'.", fg="red"))
            return None

    program_dirs = [path for path in programs_root.iterdir() if (path / "program.yaml").exists()] if programs_root.exists() else []
    if len(program_dirs) == 1:
        return program_dirs[0].name
    if not program_dirs:
        typer.echo(typer.style("No program.yaml found in programs/.", fg="red"))
        return None
    typer.echo(typer.style("Specify --edition when multiple programs exist.", fg="red"))
    return None


def _run_workiq_doctor_checks(
    *,
    program_id: str,
    programs_root: Path,
    registry_entries: list[dict[str, Any]],
    evidence_by_lane: "dict[str, WorkstreamEvidence]",
    today: date,
) -> list[DoctorCheck]:
    """P4-4 (spec §14.1): WorkIQ/M365 doctor gates QG-WIQ-5/6/8/9.

    Emits one ``DoctorCheck`` per failing gate (status ``warn``) plus a single
    ``ok`` summary when all four pass. Stays silent (no checks) when the program has
    no registry lanes — a brand-new program should not surface WIQ noise.
    """
    if not registry_entries:
        return []

    from datetime import datetime, timezone

    from src.core.models_v2 import TeamsMeetingSeries
    from src.core.quality_gates.workiq import (
        evaluate_workiq_latest_divergence_gate,
        evaluate_workiq_signal_recency_gate,
        evaluate_workiq_transcript_extraction_block_gate,
        evaluate_workiq_transcript_identifier_gate,
        is_m365_signal,
    )
    from src.core.store_factory import build_signal_store_for_program_id

    # QG-WIQ-5 / QG-WIQ-8: collect every transcript-enabled meeting series across lanes.
    meeting_series: list[TeamsMeetingSeries] = []
    for entry in registry_entries:
        for raw in (entry.get("signal_sources") or {}).get("teams_meeting_series") or []:
            if not isinstance(raw, dict):
                continue
            meeting_series.append(
                TeamsMeetingSeries(
                    display_name=str(raw.get("display_name") or raw.get("name") or entry.get("id", "")),
                    series_id=raw.get("series_id"),
                    include_transcripts=bool(raw.get("include_transcripts", True)),
                    calendar_name=raw.get("calendar_name"),
                    vpn_required=bool(raw.get("vpn_required", False)),
                )
            )

    # QG-WIQ-6: M365 signals in the recent journal window. Skipped (not warned) when
    # the signal store cannot be loaded — e.g. a stub/invalid program.yaml in a doctor
    # self-check — so incomplete programs don't surface a false "no M365 signals" warning.
    from src.core.exceptions import ConfigError

    m365_signals: tuple = ()
    signals_loaded = False
    try:
        signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
        journal_signals = signal_store.read(program_id)
        m365_signals = tuple(s for s in journal_signals if is_m365_signal(s))
        signals_loaded = True
    except (OSError, ValueError, ConfigError):
        signals_loaded = False

    # QG-WIQ-9: workiq_latest vs synthesized_at divergence (only for AI-extracted evidence).
    workiq_latest_by_lane: dict[str, str | None] = {}
    for entry in registry_entries:
        wl = entry.get("workiq_latest")
        if isinstance(wl, str):
            workiq_latest_by_lane[entry.get("id", "")] = wl
    confident_evidence_by_lane = {
        lane_id: ev for lane_id, ev in evidence_by_lane.items() if ev.confidence > 0.0
    }

    as_of = datetime.now(timezone.utc)
    gate_results = [
        evaluate_workiq_transcript_identifier_gate(meeting_series=meeting_series),
        evaluate_workiq_transcript_extraction_block_gate(meeting_series=meeting_series),
        evaluate_workiq_latest_divergence_gate(
            workiq_latest_by_lane=workiq_latest_by_lane,
            evidence_by_lane=confident_evidence_by_lane,
            as_of=as_of,
        ),
    ]
    if signals_loaded:
        gate_results.append(evaluate_workiq_signal_recency_gate(m365_signals=m365_signals, as_of=as_of))

    checks: list[DoctorCheck] = []
    failures = [g for g in gate_results if not g.passed]
    if failures:
        for gate in failures:
            checks.append(DoctorCheck(
                label="WorkIQ enrichment",
                status="warn",
                detail=f"[{gate.gate_id}] {gate.message}",
            ))
    else:
        checks.append(DoctorCheck(
            label="WorkIQ enrichment",
            status="ok",
            detail="QG-WIQ-5/6/8/9 passed: transcript identifiers seeded, M365 signals present, no extraction blocks, no workiq_latest divergence.",
        ))

    # P4-10 (§7.8): surface cross-source RealityConflicts detected between M365
    # evidence and the IcM/Kusto signals for each lane. Reuses the journal signals
    # already loaded for QG-WIQ-6; skipped when signals could not be loaded.
    if signals_loaded:
        conflict_checks = _detect_workstream_conflict_checks(
            evidence_by_lane=evidence_by_lane,
            journal_signals=journal_signals,
        )
        checks.extend(conflict_checks)
    return checks


def _detect_workstream_conflict_checks(
    *,
    evidence_by_lane: "dict[str, WorkstreamEvidence]",
    journal_signals: tuple,
) -> list[DoctorCheck]:
    """P4-10: per-lane cross-source conflict detection → DoctorCheck warnings.

    Partitions the journal signals by lane + source and runs
    ``detect_evidence_conflicts`` against each lane's M365 evidence. Emits one
    ``warn`` check per conflict (family-tagged) so the author sees the
    disagreement rather than a silently-merged bundle.
    """
    from src.core.evidence_conflict_detector import detect_evidence_conflicts

    signals_by_lane: dict[str, list] = {}
    for signal in journal_signals:
        lane_id = getattr(signal, "workstream_id", None)
        if not lane_id:
            continue
        signals_by_lane.setdefault(lane_id, []).append(signal)

    checks: list[DoctorCheck] = []
    for lane_id, evidence in evidence_by_lane.items():
        lane_signals = signals_by_lane.get(lane_id, [])
        icm_blockers = tuple(s for s in lane_signals if (s.source or "").lower().startswith("icm"))
        kusto_metrics = tuple(s for s in lane_signals if (s.source or "").lower().startswith("kusto"))
        if not icm_blockers and not kusto_metrics:
            continue
        conflicts = detect_evidence_conflicts(
            m365_evidence=evidence,
            icm_blockers=icm_blockers,
            kusto_metrics=kusto_metrics,
        )
        for conflict in conflicts:
            checks.append(DoctorCheck(
                label="Workstream conflict",
                status="warn",
                detail=f"[{conflict.family}] lane={lane_id}: {conflict.description}",
            ))
    return checks


def _load_evidence_store(program_dir: Path) -> "dict[str, WorkstreamEvidence]":
    """Load the latest WorkstreamEvidence per lane from evidence_store.jsonl.

    Last record per lane wins (most recent gather run). Returns {} if file absent.
    """
    import json
    from datetime import date, datetime as _datetime
    from src.core.evidence_models import EtaRecord, WorkstreamEvidence
    from src.core.jsonl_utils import parse_jsonl_line
    from src.core.models import RiskLevel

    store_path = program_dir / "journal" / "evidence_store.jsonl"
    if not store_path.exists():
        return {}
    last_by_lane: dict[str, dict] = {}
    try:
        for line in store_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = parse_jsonl_line(line)
                lane_id = record.get("lane_id")
                if lane_id:
                    last_by_lane[lane_id] = record
            except json.JSONDecodeError:
                continue
    except OSError:
        return {}

    result: dict[str, WorkstreamEvidence] = {}
    for lane_id, record in last_by_lane.items():
        try:
            etas = tuple(
                EtaRecord(
                    label=e.get("label", ""),
                    eta_date=date.fromisoformat(e["eta_date"]),
                    owner=e.get("owner"),
                    status=e.get("status", "open"),
                    ado_id=e.get("ado_id"),
                )
                for e in (record.get("etas") or [])
                if "eta_date" in e
            )
            result[lane_id] = WorkstreamEvidence(
                lane_id=lane_id,
                synthesized_at=_datetime.fromisoformat(record["synthesized_at"]),
                risk_level=RiskLevel.from_string(record.get("risk_level")),
                etas=etas,
                blocking_items=tuple(record.get("blocking_items") or []),
                owners=tuple(record.get("owners") or []),
                source_refs=(),
                raw_excerpts=tuple(record.get("raw_excerpts") or []),
                confidence=float(record.get("confidence", 0.0)),
                narrative_summary=record.get("narrative_summary", ""),
            )
        except (KeyError, ValueError, TypeError):
            continue
    return result
