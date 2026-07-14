"""Nudge command — thin orchestrator for the full-hygiene EML staging engine.

This module owns: CLI interface, locking, orchestration, rendering, EML writing, and audit.
Config, query, state, registry, and model logic live in src/core/.
"""
from __future__ import annotations

import json
import os
import uuid
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
import hashlib
from pathlib import Path
from typing import Any, Callable, IO, Iterable, Mapping, cast

import portalocker
import typer
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound, select_autoescape

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai._pipeline import process_generated_text
from src.commands import gather as gather_helpers
from src.core.ado_client import ADOClient
from src.core.ado_semantics import latest_meaningful_ado_update
from src.core.business_days import business_days_since
from src.core.context_gap_store import append_context_gap
from src.core.edition_resolver import PROGRAMS_ROOT, get_legacy_nudge_output, get_nudge_paths, get_program_output_dir
from src.core.eml_writer import build_eml_bytes, write_eml_atomic
from src.core.exceptions import AuthError, ConfigError, QueryError, RenderError, StateError
from src.core.jsonl_utils import append_jsonl_line
from src.core.knowledge_store import load_program_knowledge
from src.core.models import WorkItem
from src.core.models_v2 import Program, Workstream
from src.core.nudge_config import load_nudge_config, parse_stale_overrides
from src.core.nudge_models import (
    NUDGE_AUDIT_MAX_BYTES,
    NUDGE_AUDIT_SCHEMA_VERSION,
    NUDGE_CANDIDATE_WORKERS_DEFAULT,
    NUDGE_CANDIDATE_WORKERS_MAX,
    NUDGE_COMMENT_FETCH_LIMIT_DEFAULT,
    NUDGE_COMMENT_WALL_CLOCK_SECONDS,
    NUDGE_DRAFT_RETAIN,
    NUDGE_PUBLISHED_INDEX_SCHEMA_VERSION,
    NUDGE_STATE_LOCK_TIMEOUT_SECONDS,
    NUDGE_TITLE_CACHE_MAX_ENTRIES,
    FullHygieneArtifacts,
    FullHygieneRow,
    FullHygieneSection,
    FullHygieneWorkstreamGroup,
    NudgeAuditEvent,
    NudgeAuditSection,
    NudgeConfig,
    NudgeSectionFetchResult,
    NudgeSectionSpec,
    ResolvedRecipient,
    build_audit_line,
    make_run_id,
)
from src.core.nudge_query import NudgeADOClient, fetch_section_candidates
from src.core.nudge_resolution import build_subject_prefix, resolve_sections
from src.core.nudge_state_store import (
    compute_prune_before,
    record_nudge_state,
    reset_nudge_item_state,
    update_nudge_state,
)
from src.core.program_fact_store import append_nudge_event
from src.core.program_reality import ProgramReality
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES
from src.core.workstream_registry import load_authored_workstream_registry
from src.core.yaml_utils import load_yaml_mapping


REPO_ROOT = Path(__file__).resolve().parents[2]

# Re-export models for backward compatibility with existing test imports
__all__ = [
    "FullHygieneArtifacts",
    "FullHygieneRow",
    "FullHygieneSection",
    "FullHygieneWorkstreamGroup",
    "ResolvedRecipient",
    "generate_full_hygiene_nudges",
    "_word_truncate_title",
    "_comment_has_keyword",
    "_ai_batch_compress_titles",
    "_compress_titles_batch",
]


def nudge_command(
    program: str = typer.Option(..., "--program", help="Program ID, e.g. nova or armada."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Write preview EML to drafts/ without mutating cooldown state."),
    stale_override: list[str] = typer.Option([], "--stale-override", help="Override staleness per section: section_id=days."),
    stale_a: int | None = typer.Option(None, "--stale-a", min=1, hidden=True),
    stale_b: int | None = typer.Option(None, "--stale-b", min=1, hidden=True),
    stale_c: int | None = typer.Option(None, "--stale-c", min=1, hidden=True),
    audit_registry: bool = typer.Option(False, "--audit-registry", help="Read-only registry audit. Prints to stdout; no state change."),
    audit_registry_output: Path | None = typer.Option(None, "--audit-registry-output", help="Write registry audit to this path (requires --audit-registry)."),
    reset_cooldown: bool = typer.Option(False, "--reset-cooldown", help="Preview or confirm cooldown reset."),
    yes: bool = typer.Option(False, "--yes", help="Confirm --reset-cooldown mutation."),
    approve_draft: str | None = typer.Option(None, "--approve-draft", help="Record operator approval for a draft EML by filename or run_id."),
    mark_sent: str | None = typer.Option(None, "--mark-sent", help="Mark a draft EML as sent: copy to published_eml/ and record audit. Pass the draft filename or run_id."),
    import_sent: str | None = typer.Option(None, "--import-sent", help="Import an already-sent published EML into cooldown/publication tracking by filename or run_id."),
    sent_at: str | None = typer.Option(None, "--sent-at", help="Override the attested/imported send timestamp for --mark-sent or --import-sent (ISO-8601, e.g. 2026-06-22T09:00:00Z). Defaults to now."),
    list_drafts: bool = typer.Option(False, "--list-drafts", help="List available draft EML files in drafts/."),
) -> None:
    # Validate mode exclusivity
    mode_flags = [audit_registry, reset_cooldown, bool(approve_draft), bool(mark_sent), bool(import_sent), list_drafts]
    if sum(mode_flags) > 1:
        typer.echo("ERROR: --audit-registry, --reset-cooldown, --approve-draft, --mark-sent, --import-sent, and --list-drafts are mutually exclusive.", err=True)
        raise typer.Exit(code=2)
    if dry_run and (approve_draft or mark_sent or import_sent or list_drafts):
        typer.echo("ERROR: --dry-run cannot be combined with --approve-draft, --mark-sent, --import-sent, or --list-drafts.", err=True)
        raise typer.Exit(code=2)
    if sent_at and not (mark_sent or import_sent):
        typer.echo("ERROR: --sent-at is only valid with --mark-sent or --import-sent.", err=True)
        raise typer.Exit(code=2)
    if yes and not reset_cooldown:
        typer.echo("ERROR: --yes is only valid with --reset-cooldown.", err=True)
        raise typer.Exit(code=2)
    if audit_registry_output and not audit_registry:
        typer.echo("ERROR: --audit-registry-output requires --audit-registry.", err=True)
        raise typer.Exit(code=2)
    if audit_registry and (dry_run or stale_override or stale_a or stale_b or stale_c or reset_cooldown):
        typer.echo("ERROR: --audit-registry forbids --dry-run, stale flags/overrides, and --reset-cooldown.", err=True)
        raise typer.Exit(code=2)

    programs_root = PROGRAMS_ROOT
    nudge_paths = get_nudge_paths(program, programs_root=programs_root)

    if audit_registry:
        _run_registry_audit(program, programs_root=programs_root, output_path=audit_registry_output)
        raise typer.Exit(code=0)

    if list_drafts:
        _cmd_list_drafts(program, nudge_paths=nudge_paths)
        raise typer.Exit(code=0)

    if approve_draft:
        _cmd_approve_draft(
            program, draft_ref=approve_draft, nudge_paths=nudge_paths, programs_root=programs_root,
        )
        raise typer.Exit(code=0)

    if mark_sent:
        sent_at_dt: datetime | None = None
        if sent_at:
            try:
                sent_at_dt = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
                if sent_at_dt.tzinfo is None:
                    sent_at_dt = sent_at_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                typer.echo(f"ERROR: --sent-at must be ISO-8601 (e.g. 2026-06-22T09:00:00Z), got {sent_at!r}", err=True)
                raise typer.Exit(code=2)
        _cmd_mark_sent(
            program, draft_ref=mark_sent, nudge_paths=nudge_paths,
            programs_root=programs_root, sent_at_override=sent_at_dt,
        )
        raise typer.Exit(code=0)

    if import_sent:
        sent_at_dt = None
        if sent_at:
            try:
                sent_at_dt = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
                if sent_at_dt.tzinfo is None:
                    sent_at_dt = sent_at_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                typer.echo(f"ERROR: --sent-at must be ISO-8601 (e.g. 2026-06-22T09:00:00Z), got {sent_at!r}", err=True)
                raise typer.Exit(code=2)
        _cmd_import_sent(
            program, published_ref=import_sent, nudge_paths=nudge_paths,
            programs_root=programs_root, sent_at_override=sent_at_dt,
        )
        raise typer.Exit(code=0)

    if reset_cooldown:
        state_path = _nudge_read_path(nudge_paths.state_path, programs_root / program / "nudge_state.json")
        count = reset_nudge_item_state(state_path, confirmed=False)
        if not yes:
            typer.echo(f"Cooldown reset preview: {count} unique item record(s) would be removed.")
            typer.echo("Re-run with --yes to confirm.")
            raise typer.Exit(code=0)
        removed = reset_nudge_item_state(state_path, confirmed=True)
        typer.echo(f"Cooldown reset: removed {removed} unique item record(s) from {state_path}")
        raise typer.Exit(code=0)

    # Normal generation mode
    if stale_a is not None or stale_b is not None or stale_c is not None:
        for flag in ("--stale-a", "--stale-b", "--stale-c"):
            if locals().get(flag.lstrip("-").replace("-", "_")) is not None:
                warnings.warn(
                    f"{flag} is deprecated; use --stale-override section_id=days instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                typer.echo(f"WARNING: {flag} is deprecated; use --stale-override instead.", err=True)

    try:
        stale_overrides = parse_stale_overrides(stale_override)
    except ValueError as exc:
        raise typer.BadParameter(str(exc))

    try:
        artifacts = generate_full_hygiene_nudges(
            program_id=program,
            dry_run=dry_run,
            stale_overrides=stale_overrides or None,
            stale_a=stale_a,
            stale_b=stale_b,
            stale_c=stale_c,
            programs_root=programs_root,
        )
    except (AuthError, ConfigError, QueryError, StateError, RenderError, typer.BadParameter) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2)

    if artifacts.degraded_section_ids:
        degraded_str = ", ".join(artifacts.degraded_section_ids)
        typer.echo(f"DEGRADED: sections [{degraded_str}] had query or comment-fetch failures.")

    typer.echo(_render_full_hygiene_plaintext(artifacts))
    for path in artifacts.eml_paths:
        typer.echo(f"EML: {path}")
    if artifacts.ai_titles_compressed:
        typer.echo(f"AI compressed {artifacts.ai_titles_compressed} title(s).")

    if dry_run:
        typer.echo(
            f"Dry run: wrote {len(artifacts.eml_paths)} preview EML draft(s); cooldown state not updated."
            if artifacts.eml_paths
            else "Dry run: no items found."
        )

    exit_code = 3 if artifacts.degraded_section_ids else 0
    raise typer.Exit(code=exit_code)


# ---------------------------------------------------------------------------
# Main generation entry point
# ---------------------------------------------------------------------------


def generate_full_hygiene_nudges(
    *,
    program_id: str,
    dry_run: bool,
    stale_overrides: Mapping[str, int] | None = None,
    stale_a: int | None = None,
    stale_b: int | None = None,
    stale_c: int | None = None,
    as_of: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    templates_root: Path | None = None,
    client_factory: Callable[[Any], NudgeADOClient] | None = None,
    candidate_workers: int = NUDGE_CANDIDATE_WORKERS_DEFAULT,
) -> FullHygieneArtifacts:
    candidate_workers = max(1, min(candidate_workers, NUDGE_CANDIDATE_WORKERS_MAX))
    now_utc = _ensure_utc(as_of or datetime.now(timezone.utc))
    run_id = make_run_id(now_utc)

    program, workstreams = gather_helpers._load_program_context(program_id, programs_root)
    if program.ado is None and _program_needs_ado(program_id, programs_root):
        raise ConfigError(f"Program '{program_id}' is missing ado configuration.")

    tpl_root = templates_root or (REPO_ROOT / "templates")
    config = load_nudge_config(
        program_id=program_id,
        program=program,
        programs_root=programs_root,
        templates_root=tpl_root,
    )

    authored_registry = load_authored_workstream_registry(
        program_id=program_id,
        programs_root=programs_root,
    )

    knowledge = load_program_knowledge(program_id, programs_root=programs_root)

    # Path constants — canonical new layout with fallback reads for old layout
    _np = get_nudge_paths(program_id, programs_root=programs_root)
    output_root = _np.nudge_root  # canonical nudge root
    state_path = _nudge_read_path(_np.state_path, programs_root / program_id / "nudge_state.json")
    _legacy_output = get_legacy_nudge_output(program_id, programs_root=programs_root)
    audit_path = _nudge_read_path(_np.audit_path, _legacy_output / "nudge_audit.jsonl")
    audit_lock_path = audit_path.with_suffix(audit_path.suffix + ".lock")
    run_lock_path = _np.run_lock_path
    title_cache_path = _nudge_read_path(_np.title_cache_path, _legacy_output / "title_cache.json")

    # Resolve section stale overrides
    resolved_overrides: dict[str, int] = dict(stale_overrides or {})
    _apply_legacy_stale_flags(resolved_overrides, config, stale_a=stale_a, stale_b=stale_b, stale_c=stale_c)

    # Recipient resolution
    try:
        primary_recipient = _resolve_primary_recipient(
            config.delivery.recipient, knowledge.people_directory, program_id=program_id
        )
    except ConfigError:
        raise

    # Load prior nudge state for audit/analytics only (no longer used for filtering)

    # Acquire live-run lock (live only)
    output_root.mkdir(parents=True, exist_ok=True)

    if not dry_run:
        try:
            run_lock = portalocker.Lock(str(run_lock_path), mode="a+", timeout=0, encoding="utf-8")
            run_lock.acquire()
        except portalocker.exceptions.LockException as exc:
            raise StateError(
                f"Another nudge run is active (lock: {run_lock_path}). "
                "Retry after the active run completes."
            ) from exc
        # Write run metadata. ``portalocker.Lock.fh`` is typed as
        # ``IO[str] | IO[bytes] | None``; after a successful acquire it is a
        # non-None text handle, so we narrow it via cast (the surrounding
        # ``except`` still guards any runtime surprise).
        try:
            lock_fh = cast(IO[str], run_lock.fh)
            lock_fh.truncate(0)
            lock_fh.write(json.dumps({
                "run_id": run_id,
                "pid": os.getpid(),
                "started_at": now_utc.isoformat(),
            }))
            lock_fh.flush()
            os.fsync(lock_fh.fileno())
        except (OSError, AttributeError):
            pass
    else:
        run_lock = None  # type: ignore[assignment]

    try:
        artifacts = _orchestrate(
            run_id=run_id,
            program_id=program_id,
            program=program,
            workstreams=workstreams,
            config=config,
            authored_registry=authored_registry,
            knowledge=knowledge,
            primary_recipient=primary_recipient,
            now_utc=now_utc,
            dry_run=dry_run,
            resolved_overrides=resolved_overrides,
            output_root=output_root,
            state_path=state_path,
            audit_path=audit_path,
            audit_lock_path=audit_lock_path,
            title_cache_path=title_cache_path,
            tpl_root=tpl_root,
            client_factory=client_factory,
            candidate_workers=candidate_workers,
            programs_root=programs_root,
        )
    finally:
        if run_lock is not None:
            try:
                run_lock.release()
            except Exception as _lock_exc:
                typer.echo(f"WARNING: nudge run-lock release failed: {_lock_exc}", err=True)

    return artifacts


def _orchestrate(
    *,
    run_id: str,
    program_id: str,
    program: Any,
    workstreams: tuple[Any, ...],
    config: NudgeConfig,
    authored_registry: tuple[Any, ...],
    knowledge: Any,
    primary_recipient: ResolvedRecipient,
    now_utc: datetime,
    dry_run: bool,
    resolved_overrides: dict[str, int],
    output_root: Path,
    state_path: Path,
    audit_path: Path,
    audit_lock_path: Path,
    title_cache_path: Path,
    tpl_root: Path,
    client_factory: Callable[[Any], NudgeADOClient] | None,
    candidate_workers: int,
    programs_root: Path,
) -> FullHygieneArtifacts:
    warnings_list: list[str] = []
    optional_failures: list[str] = []

    # Load ProgramReality early to compute retired sections before any fetch
    try:
        _pre_reality = ProgramReality.load(program_id, programs_root=programs_root)
    except Exception:
        _pre_reality = None

    _pre_resolved = resolve_sections(config, _pre_reality, program_id=program_id, now_utc=now_utc)
    retired_section_ids: frozenset[str] = frozenset(
        rs.spec.id for rs in _pre_resolved if rs.is_retired
    )
    if retired_section_ids:
        warnings_list.append(
            f"Sections retired (milestone done): {', '.join(sorted(retired_section_ids))}"
        )
    active_sections = tuple(s for s in config.sections if s.id not in retired_section_ids)

    # Fetch candidates for all active (non-retired) sections
    fetch_results = _fetch_all_sections(
        active_sections, program=program, authored_registry=authored_registry,
        workstreams=workstreams, now_utc=now_utc, client_factory=client_factory,
        candidate_workers=candidate_workers,
    )

    # Build membership map: item_id → list of section indices (in YAML order)
    item_memberships: dict[int, list[int]] = defaultdict(list)
    for idx, result in enumerate(fetch_results):
        if not result.query_error:
            for cand in result.candidates:
                item_memberships[getattr(cand.item, "id", 0)].append(idx)

    # Exempt items + waivers (Phase 2: §6.8)
    exempt_ids = config.evaluation.nudge_exempt_item_ids
    # Build active waiver set (non-expired, keyed by work_item_id)
    active_waiver_ids: frozenset[int] = frozenset(
        w.work_item_id for w in config.evaluation.nudge_waivers if not w.expired
    )
    waiver_by_section: dict[int, int] = defaultdict(int)
    exempt_by_section: dict[int, int] = defaultdict(int)

    # State file retention: derive from max stale threshold × 3 (≥30 days)
    # cooldown_days config is preserved for backward-compat but no longer drives filtering.
    max_stale_bd = max(
        (resolved_overrides.get(s.id, s.stale_business_days) for s in active_sections),
        default=7,
    )
    max_effective_cooldown = max(30, max_stale_bd * 3)

    # Assign first-section ownership; apply exemptions and waivers only
    owned_by_section: dict[int, int] = {}  # item_id → owner section index
    total_waiver_filtered = 0
    for item_id, indices in item_memberships.items():
        if not indices:
            continue
        first_idx = min(indices)
        if item_id in exempt_ids:
            exempt_by_section[first_idx] += 1
            continue
        if item_id in active_waiver_ids:
            waiver_by_section[first_idx] += 1
            total_waiver_filtered += 1
            continue
        owned_by_section[item_id] = first_idx

    # Per-section staleness thresholds (parameterisable via --stale-override at run time)
    section_stale_thresholds: dict[int, int] = {
        idx: resolved_overrides.get(sec.id, sec.stale_business_days)
        for idx, sec in enumerate(active_sections)
    }

    # Build candidate map per section, applying staleness filter.
    # An item is included only when it has NOT been updated in ADO for >= stale threshold
    # business days — keeping bothering until compliance, as opposed to a time-based cooldown.
    filtered_candidates: dict[int, list[Any]] = defaultdict(list)  # section_idx → [NudgeCandidate]
    dedup_by_section: dict[int, int] = defaultdict(int)  # G-5: cross-section dedup counter
    stale_filtered_by_section: dict[int, int] = defaultdict(int)  # items too fresh to nag
    for result in fetch_results:
        for idx, sec in enumerate(active_sections):
            if result.section_id == sec.id:
                stale_threshold = section_stale_thresholds[idx]
                for cand in result.candidates:
                    item_id = getattr(cand.item, "id", 0)
                    if owned_by_section.get(item_id) == idx:
                        stale_bd = _business_days_for_item(cand.item, now_utc)
                        if stale_bd < stale_threshold:
                            stale_filtered_by_section[idx] += 1
                            continue
                        filtered_candidates[idx].append(cand)
                    elif item_id not in exempt_ids and item_id not in active_waiver_ids:
                        # Item exists in this section but was claimed by a higher-priority section
                        dedup_by_section[idx] += 1
                break

    # Build overdue set from authored registry
    ado_overdue_ids: frozenset[int] = frozenset().union(
        *(getattr(e, "overdue_ado_item_ids", frozenset()) for e in authored_registry)
    )

    # Comment budget: fetch after filtering, prioritized
    comment_window_days = config.evaluation.comment_window_days
    comment_fetch_limit = config.evaluation.comment_fetch_limit
    status_keywords = config.evaluation.status_keywords
    risk_on_track_values = config.evaluation.risk_on_track_values

    all_final_items: list[tuple[Any, int]] = []  # (item, section_idx)
    for idx in range(len(config.sections)):
        for cand in filtered_candidates.get(idx, []):
            all_final_items.append((cand.item, idx))

    # Sort by comment priority: overdue first, then most stale, then section index, then item_id
    def _comment_priority(tup: tuple[Any, int]) -> tuple:
        item, sidx = tup
        item_id = getattr(item, "id", 0)
        stale_bd = _business_days_for_item(item, now_utc)
        return (0 if item_id in ado_overdue_ids else 1, -stale_bd, sidx, item_id)

    all_final_items.sort(key=_comment_priority)

    # Create comment client
    comment_client: ADOClient | None = None
    if program.ado is not None:
        comment_client = ADOClient(
            organization=program.ado.organization,
            project=program.ado.project,
            timeout=program.ado.api_timeout_seconds,
        )

    comment_cache: dict[int, tuple[bool | None, str | None]] = {}
    comment_fetch_skipped_total = 0
    comment_fetch_errors_total = 0
    evaluated_count = 0
    cutoff = now_utc - timedelta(days=comment_window_days)
    # Phase 4 §6.5: wall-clock deadline — degrade after NUDGE_COMMENT_WALL_CLOCK_SECONDS
    import time as _time  # noqa: PLC0415
    _comment_fetch_start = _time.monotonic()
    _comment_wall_exceeded = False

    for item, _sidx in all_final_items:
        item_id = getattr(item, "id", 0)
        if item_id in comment_cache:
            continue
        if comment_client is None:
            comment_cache[item_id] = (None, None)
            comment_fetch_skipped_total += 1
        elif _comment_wall_exceeded or evaluated_count >= comment_fetch_limit:
            comment_cache[item_id] = (None, None)
            comment_fetch_skipped_total += 1
        else:
            # Check wall-clock budget before each fetch
            if _time.monotonic() - _comment_fetch_start >= NUDGE_COMMENT_WALL_CLOCK_SECONDS:
                _comment_wall_exceeded = True
                comment_cache[item_id] = (None, None)
                comment_fetch_skipped_total += 1
                continue
            try:
                latest_text = _fetch_latest_comment_text(item_id, client=comment_client, cutoff=cutoff)
                comment_cache[item_id] = (latest_text is not None, latest_text)
                evaluated_count += 1
            except QueryError:
                comment_cache[item_id] = (None, None)
                comment_fetch_errors_total += 1

    # Phase 4 §6.5: surface wall-clock degradation as a warning
    if _comment_wall_exceeded:
        warnings_list.append(
            f"Comment fetch wall-clock budget ({NUDGE_COMMENT_WALL_CLOCK_SECONDS}s) exceeded; "
            f"{comment_fetch_skipped_total} item(s) skipped (unknown comment status)."
        )

    # Title compression
    compress_ai = config.presentation.compress_titles_with_ai
    title_items = [(getattr(c.item, "id", 0), getattr(c.item, "title", "")) for c in (
        cand for idx in range(len(config.sections)) for cand in filtered_candidates.get(idx, [])
    )]
    title_map, ai_count = _compress_titles_batch(
        title_items,
        cache_path=title_cache_path,
        enabled=compress_ai,
        program=program if compress_ai else None,
    )

    # Build workstream_by_id from registry for grouping
    workstream_by_id: dict[str, Any] = {}
    for entry in authored_registry:
        ws_id = getattr(entry, "id", None)
        if ws_id:
            workstream_by_id[ws_id] = entry

    # Build FullHygieneSection objects
    sections: list[FullHygieneSection] = []
    audit_sections: list[NudgeAuditSection] = []
    resolved_meta_by_id = {rs.spec.id: rs for rs in _pre_resolved}

    for idx, (sec, result) in enumerate(zip(active_sections, fetch_results)):
        stale_days = resolved_overrides.get(sec.id, sec.stale_business_days)
        # G-8: resolve the deadline actually used for display/beyond-deadline math.
        # sec.deadline is only populated for operator-authored explicit deadlines;
        # sections using deadline_milestone_id must pull the assessed date instead,
        # otherwise milestone-resolved deadlines never surface in the rendered email.
        _rs = resolved_meta_by_id.get(sec.id)
        resolved_deadline = _rs.target_date_ceiling.date if _rs is not None else sec.deadline

        if result.query_error:
            sections.append(FullHygieneSection(
                section_id=sec.id, letter=sec.letter, title=sec.title,
                description=sec.description, stale_threshold_days=stale_days,
                stale_summary_threshold_days=sec.stale_summary_threshold_days,
                deadline=resolved_deadline, groups=(), total_count=0, stale_count=0,
                ready_count=0, unknown_ready_count=0, no_date_count=0,
                past_due_count=0, beyond_deadline_count=0, stale_summary_count=0,
                comment_fetch_skipped=0, comment_fetch_errors=0,
                query_error=True, error_details=result.error_details,
            ))
            audit_sections.append(NudgeAuditSection(
                section_id=sec.id, letter=sec.letter, candidate_count=0,
                candidate_item_ids=(), item_ids=(), item_count=0,
                staleness_filtered=0, exempt_filtered=0,
                comment_fetch_skipped=0, comment_fetch_errors=0,
                query_error=True, error_details=result.error_details,
            ))
            continue

        candidates_here = filtered_candidates.get(idx, [])
        # Build hints index for tag/area_path sections: item_id → workstream_id
        hints_index: dict[int, str] = {}
        for hint in sec.workstream_hints:
            for hint_item_id in hint.ado_item_ids:
                hints_index[hint_item_id] = hint.workstream_id
        # Build rows
        rows: list[FullHygieneRow] = []
        for cand in candidates_here:
            item = cand.item
            item_id = getattr(item, "id", 0)
            ws_id = hints_index.get(item_id) or cand.workstream_id
            ws_entry = workstream_by_id.get(ws_id or "")
            _ws_obj = next((w for w in workstreams if w.id == ws_id), None)
            ws_name: str | None = None
            if _ws_obj is not None:
                ws_name = _ws_obj.name
            elif ws_entry is not None:
                ws_name = getattr(ws_entry, "name", None) or ws_id
            elif ws_id:
                ws_name = ws_id

            compressed_title = title_map.get(item_id, _word_truncate_title(getattr(item, "title", "")))
            has_recent, latest_text = comment_cache.get(item_id, (None, None))
            ckw: bool | None = None
            if has_recent is True:
                ckw = _comment_has_keyword(latest_text, status_keywords)
            elif has_recent is False:
                ckw = False

            rows.append(_build_full_hygiene_row(
                item,
                compressed_title=compressed_title,
                workstream_id=ws_id,
                workstream_name=ws_name,
                as_of=now_utc,
                program=program,
                has_recent_comment=has_recent,
                comment_has_status_keyword=ckw,
                risk_on_track_values=risk_on_track_values,
                ado_overdue_ids=ado_overdue_ids,
                stale_days=stale_days,
            ))

        # Sort rows
        def _row_key(r: FullHygieneRow) -> tuple:
            ws_idx = _ws_registry_index(r.workstream_id, authored_registry)
            return (0 if r.is_overdue else 1, -r.stale_business_days, ws_idx, r.work_item_id)

        rows.sort(key=_row_key)

        groups = _group_rows_by_workstream(
            rows, workstream_by_id=workstream_by_id,
            people=knowledge.people_directory, program_id=program_id,
            owner_roles=config.delivery.owner_roles,
        )

        today = now_utc.date()
        deadline = resolved_deadline
        beyond_deadline = sum(1 for r in rows if r.target_date is not None and deadline is not None and r.target_date > deadline)
        stale_summary = sum(1 for r in rows if r.stale_business_days >= sec.stale_summary_threshold_days)
        # Per-section comment stats
        sec_skip = sum(1 for r in rows if comment_cache.get(r.work_item_id, (None,))[0] is None
                       and r.work_item_id in comment_cache)
        sec_err = 0  # errors are counted globally; degrade only when > 0

        # G-8: deadline uncertainty from resolved section metadata
        if _rs is not None:
            _dl = _rs.target_date_ceiling
            deadline_uncertain = _dl.provisional_inputs or _dl.resolution_status in ("unconfirmed", "unavailable", "none")
        else:
            deadline_uncertain = False

        # G-6: count items that also appear in other section candidate pools
        elevated_from_other_pools = sum(
            1 for cand in candidates_here
            if len(item_memberships.get(getattr(cand.item, "id", 0), [])) > 1
        )

        sections.append(FullHygieneSection(
            section_id=sec.id,
            letter=sec.letter,
            title=sec.title,
            description=sec.description,
            stale_threshold_days=stale_days,
            stale_summary_threshold_days=sec.stale_summary_threshold_days,
            deadline=deadline,
            groups=groups,
            total_count=len(rows),
            stale_count=sum(1 for r in rows if r.stale_business_days >= stale_days),
            ready_count=sum(1 for r in rows if r.is_ready is True),
            unknown_ready_count=sum(1 for r in rows if r.is_ready is None),
            no_date_count=sum(1 for r in rows if not r.has_committed),
            past_due_count=sum(1 for r in rows if r.has_committed and not r.has_valid_target_date),
            beyond_deadline_count=beyond_deadline,
            stale_summary_count=stale_summary,
            comment_fetch_skipped=sec_skip,
            comment_fetch_errors=0,
            deadline_uncertain=deadline_uncertain,
            elevated_from_other_pools=elevated_from_other_pools,
        ))
        candidate_ids_for_section = tuple(sorted(
            getattr(c.item, "id", 0)
            for c in result.candidates
            if not result.query_error
        ))
        audit_sections.append(NudgeAuditSection(
            section_id=sec.id, letter=sec.letter,
            candidate_count=len(result.candidates),
            candidate_item_ids=candidate_ids_for_section,
            item_ids=tuple(r.work_item_id for r in rows),
            item_count=len(rows),
            staleness_filtered=stale_filtered_by_section.get(idx, 0),
            exempt_filtered=exempt_by_section.get(idx, 0),
            comment_fetch_skipped=sec_skip,
            comment_fetch_errors=0,
            query_error=False,
            cross_section_dedup_filtered=dedup_by_section.get(idx, 0),
        ))

    # Resolve recipient list
    to_recipients = _build_recipient_list(
        primary_recipient=primary_recipient,
        sections=sections,
        workstream_by_id=workstream_by_id,
        people=knowledge.people_directory,
        config=config,
        optional_failures=optional_failures,
    )

    degraded_ids = tuple(
        sec.section_id for sec in sections
        if sec.query_error or sec.comment_fetch_errors > 0
    )

    total_items = sum(sec.total_count for sec in sections)
    total_staleness = sum(stale_filtered_by_section.values())
    total_exempt = sum(exempt_by_section.values())

    # EML generation
    eml_paths: tuple[Path, ...] = ()
    event_type: str
    error_str: str | None = None
    error_detail: str | None = None

    all_sections_failed = all(r.query_error for r in fetch_results)

    if all_sections_failed:
        event_type = "query_error"
        error_str = "All section candidate queries failed."
    elif total_items == 0 and not any(r.query_error for r in fetch_results):
        event_type = "no_items"
        if not dry_run:
            # Prune-only update
            prune_before = compute_prune_before(generated_at=now_utc, max_cooldown_days=max_effective_cooldown)
            try:
                update_nudge_state(
                    state_path,
                    item_ids=(),
                    triggered_at=now_utc,
                    prune_before=prune_before,
                    origin="generated",
                    run_id=run_id,
                )
            except StateError as exc:
                error_str = str(exc)[:500]
                event_type = "state_error"
    else:
        # Reuse the ProgramReality already loaded for retirement evaluation
        reality = _pre_reality

        # Resolve section deadlines and action-due dates (reuse pre-computed)
        resolved_sections_meta = _pre_resolved

        brand = config.presentation.brand_label
        subject_label = config.presentation.email_subject_label
        date_label = now_utc.strftime("%Y-%m-%d")
        subject_label = subject_label.replace("{date}", date_label)
        subject_prefix = build_subject_prefix(
            resolved_sections_meta, config, now_date=now_utc.date()
        )
        base_subject = f"{brand} | {subject_label} | {date_label}"
        subject = f"{subject_prefix} {base_subject}".strip() if subject_prefix else base_subject
        preheader = config.presentation.preheader
        template_name = config.presentation.template

        # Apply audience governance before writing a sendable draft.
        to_recipients, bcc_recipients = _apply_audience_policy(
            program_id=program_id,
            config=config,
            primary_recipient=primary_recipient,
            recipients=to_recipients,
            optional_failures=optional_failures,
        )

        # From address
        from_email = program.author_defaults.email if program.author_defaults else None
        from_display = program.author_defaults.display_name if program.author_defaults else None
        cc_emails: tuple[str, ...] = ()
        if from_email and from_email.lower() not in {r.email.lower() for r in to_recipients + bcc_recipients}:
            cc_emails = (from_email,)

        artifacts_for_render = FullHygieneArtifacts(
            run_id=run_id, sections=tuple(sections), recipient=primary_recipient,
            to_recipients=tuple(to_recipients), generated_at=now_utc,
            eml_paths=(), using_snapshot_fallback=False, ai_titles_compressed=ai_count,
            degraded_section_ids=degraded_ids,
        )

        try:
            html_body = _render_full_hygiene_html(
                subject=subject, artifacts=artifacts_for_render,
                brand_label=brand, template_name=template_name,
                preheader=preheader, templates_root=tpl_root,
                comment_window_days=comment_window_days,
            )
        except (ConfigError, RenderError) as exc:
            raise RenderError(str(exc)) from exc

        md_body = _render_full_hygiene_plaintext(artifacts_for_render)
        to_emails = tuple(r.email for r in to_recipients)
        bcc_emails = tuple(r.email for r in bcc_recipients)

        eml_bytes = build_eml_bytes(
            to=to_emails, cc=cc_emails, subject=subject,
            bcc=bcc_emails,
            html_body=html_body, text_body=md_body,
            from_display_name=from_display, from_email=from_email,
            generated_at=now_utc, mark_as_draft=True,
        )

        eml_dir = get_nudge_paths(program_id, programs_root=programs_root).drafts_dir
        eml_dir.mkdir(parents=True, exist_ok=True)
        eml_filename = f"{run_id}.eml"
        eml_path = eml_dir / eml_filename
        _prune_drafts(eml_dir)

        write_eml_atomic(eml_path, eml_bytes=eml_bytes)
        eml_paths = (eml_path,)

        if degraded_ids:
            event_type = "nudge_generated"
        elif dry_run:
            event_type = "dry_run"
        else:
            event_type = "nudge_generated"

        # Non-fatal title cache write (live + EML success only)
        if not dry_run and eml_paths:
            try:
                _write_title_cache(
                    title_cache_path,
                    current_rows=[(r.work_item_id, r.title_original, r.title) for sec in sections for grp in sec.groups for r in grp.rows],
                    old_cache=_load_title_cache(title_cache_path),
                )
            except Exception as exc:
                warnings_list.append(f"Title cache write failed (non-fatal): {exc}")

        if not dry_run and eml_paths:
            try:
                append_nudge_event(
                    program_id,
                    "event.nudge.generated",
                    {
                        "run_id": run_id,
                        "content_hash": hashlib.sha256(eml_bytes).hexdigest(),
                        "draft_path": str(eml_path.relative_to(programs_root / program_id)),
                        "recipient_count": len(to_recipients) + len(bcc_recipients),
                        "item_ids": [r.work_item_id for sec in sections for grp in sec.groups for r in grp.rows],
                    },
                    recorded_at=now_utc,
                    db_root=programs_root.parent,
                )
            except Exception as exc:
                warnings_list.append(f"Fact-store nudge.generated write failed: {exc}")

    # Append audit event
    relative_eml_paths = tuple(
        str(p.relative_to(programs_root / program_id)) for p in eml_paths
    )
    audit_event = NudgeAuditEvent(
        event_type=event_type,  # type: ignore[arg-type]
        schema_version=NUDGE_AUDIT_SCHEMA_VERSION,
        run_id=run_id, program_id=program_id, triggered_at=now_utc,
        dry_run=dry_run, sections=tuple(audit_sections),
        total_items=total_items, total_staleness_filtered=total_staleness,
        total_exempt_filtered=total_exempt,
        total_waiver_filtered=total_waiver_filtered,
        comment_fetch_skipped_total=comment_fetch_skipped_total,
        comment_fetch_errors_total=comment_fetch_errors_total,
        recipient=primary_recipient.email,
        optional_recipient_failures=tuple(optional_failures),
        degraded_section_ids=degraded_ids,
        warnings=tuple(warnings_list),
        eml_paths=relative_eml_paths,
        error=error_str,
    )
    _append_audit(audit_path, audit_lock_path, audit_event)

    if all_sections_failed:
        raise QueryError("All section candidate queries failed. No EML generated.")

    return FullHygieneArtifacts(
        run_id=run_id,
        sections=tuple(sections),
        recipient=primary_recipient,
        to_recipients=tuple(to_recipients),
        generated_at=now_utc,
        eml_paths=eml_paths,
        using_snapshot_fallback=False,
        ai_titles_compressed=ai_count,
        degraded_section_ids=degraded_ids,
    )


# ---------------------------------------------------------------------------
# Path helpers (transition window: new layout writes, old layout fallback reads)
# ---------------------------------------------------------------------------


def _nudge_read_path(new_path: Path, old_path: Path) -> Path:
    """Return *new_path* for writes (and reads when the new file exists).
    Fall back to *old_path* for reads during the transition window."""
    if new_path.exists():
        return new_path
    if old_path.exists():
        return old_path
    return new_path  # new location — will be created on first write


def _prune_drafts(drafts_dir: Path, retain: int = NUDGE_DRAFT_RETAIN) -> None:
    """Keep only the *retain* most-recent EML drafts; delete oldest atomically."""
    try:
        emls = sorted(drafts_dir.glob("*.eml"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in emls[retain:]:
            try:
                old.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def _load_published_index(index_path: Path) -> list[dict[str, Any]]:
    if not index_path.exists():
        return []
    try:
        raw = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return raw if isinstance(raw, list) else []


def _extract_generated_item_ids_for_draft(audit_path: Path, *, draft_filename: str) -> tuple[str | None, tuple[int, ...]]:
    if not audit_path.exists():
        return None, ()
    matched_run_id: str | None = None
    matched_item_ids: tuple[int, ...] = ()
    try:
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                continue
            # Only consider generation events; lifecycle events (approve, mark_sent, import_sent)
            # share the same eml_paths but carry empty sections, which would overwrite the
            # item_ids collected from the generation event with an empty tuple.
            if payload.get("event_type") not in {"dry_run", "nudge_generated"}:
                continue
            eml_paths = payload.get("eml_paths") or []
            if not isinstance(eml_paths, list):
                continue
            if not any(str(path).endswith(draft_filename) for path in eml_paths):
                continue
            sections = payload.get("sections") or []
            item_ids: list[int] = []
            for section in sections:
                if isinstance(section, dict):
                    item_ids.extend(int(v) for v in (section.get("item_ids") or []) if isinstance(v, int))
            matched_run_id = str(payload.get("run_id") or "") or None
            matched_item_ids = tuple(sorted(set(item_ids)))
    except (OSError, json.JSONDecodeError, ValueError):
        return None, ()
    return matched_run_id, matched_item_ids


def _parse_eml_audience_manifest(
    eml_path: Path,
    *,
    prior_index: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    message = BytesParser(policy=policy.default).parsebytes(eml_path.read_bytes())

    def _aliases_from_header(name: str) -> tuple[str, ...]:
        header = message.get_all(name, [])
        values: list[str] = []
        for raw in header:
            for part in str(raw).split(","):
                normalized = _normalize_alias(part)
                if normalized:
                    values.append(normalized)
        return tuple(sorted(set(values)))

    to_aliases = _aliases_from_header("To")
    cc_aliases = _aliases_from_header("Cc")
    bcc_aliases = _aliases_from_header("Bcc")

    previous_manifest = None
    for entry in reversed(prior_index):
        audience = entry.get("audience")
        if isinstance(audience, dict):
            previous_manifest = audience
            break

    previous_aliases = set()
    if previous_manifest is not None:
        previous_aliases = {
            *map(str, previous_manifest.get("to_aliases") or ()),
            *map(str, previous_manifest.get("cc_aliases") or ()),
            *map(str, previous_manifest.get("bcc_aliases") or ()),
        }
    current_aliases = set((*to_aliases, *cc_aliases, *bcc_aliases))
    manifest = {
        "to_aliases": list(to_aliases),
        "cc_aliases": list(cc_aliases),
        "bcc_aliases": list(bcc_aliases),
        "added_since_last_attested": sorted(current_aliases - previous_aliases),
        "removed_since_last_attested": sorted(previous_aliases - current_aliases),
    }
    content_hash = hashlib.sha256(eml_path.read_bytes()).hexdigest()
    return content_hash, manifest


def _approval_index_path(nudge_paths: "Any") -> Path:
    raw = getattr(nudge_paths, "approval_index_path", None)
    if isinstance(raw, Path):
        return raw
    published_dir = getattr(nudge_paths, "published_eml_dir", None)
    if isinstance(published_dir, Path):
        return published_dir.parent / "draft_approvals.json"
    drafts_dir = getattr(nudge_paths, "drafts_dir", None)
    if isinstance(drafts_dir, Path):
        return drafts_dir.parent / "draft_approvals.json"
    raise ConfigError("Could not resolve nudge draft approval index path.")


def _load_draft_approvals(index_path: Path) -> list[dict[str, Any]]:
    if not index_path.exists():
        return []
    try:
        raw = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return raw if isinstance(raw, list) else []


def _update_draft_approval_index(index_path: Path, entry: dict[str, Any]) -> None:
    existing = _load_draft_approvals(index_path)
    updated: list[dict[str, Any]] = []
    replaced = False
    entry_hash = str(entry.get("content_hash") or "")
    entry_filename = str(entry.get("filename") or "")
    for current in existing:
        current_hash = str(current.get("content_hash") or "")
        current_filename = str(current.get("filename") or "")
        if entry_hash and current_hash == entry_hash:
            updated.append(entry)
            replaced = True
            continue
        if entry_filename and current_filename == entry_filename:
            updated.append(entry)
            replaced = True
            continue
        updated.append(current)
    if not replaced:
        updated.append(entry)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _resolve_draft_path(draft_ref: str, *, nudge_paths: "Any") -> Path:
    candidate = draft_ref if draft_ref.endswith(".eml") else f"{draft_ref}.eml"
    draft_path = nudge_paths.drafts_dir / candidate
    if draft_path.exists():
        return draft_path
    matches = list(nudge_paths.drafts_dir.glob(f"*{draft_ref}*.eml")) if nudge_paths.drafts_dir.exists() else []
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        typer.echo(f"ERROR: ambiguous draft ref {draft_ref!r}; matches: {[p.name for p in matches]}", err=True)
        raise typer.Exit(code=2)
    typer.echo(f"ERROR: draft not found: {candidate} in {nudge_paths.drafts_dir}", err=True)
    raise typer.Exit(code=2)


def _resolve_published_path(published_ref: str, *, nudge_paths: "Any") -> Path:
    candidate = published_ref if published_ref.endswith(".eml") else f"{published_ref}.eml"
    published_path = nudge_paths.published_eml_dir / candidate
    if published_path.exists():
        return published_path
    matches = list(nudge_paths.published_eml_dir.glob(f"*{published_ref}*.eml")) if nudge_paths.published_eml_dir.exists() else []
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        typer.echo(f"ERROR: ambiguous published ref {published_ref!r}; matches: {[p.name for p in matches]}", err=True)
        raise typer.Exit(code=2)
    typer.echo(f"ERROR: published EML not found: {candidate} in {nudge_paths.published_eml_dir}", err=True)
    raise typer.Exit(code=2)


def _approval_required_for_manifest(
    program_id: str,
    *,
    programs_root: Path,
    audience_manifest: Mapping[str, Any],
) -> bool:
    try:
        program, _workstreams = gather_helpers._load_program_context(program_id, programs_root)
        config = load_nudge_config(
            program_id=program_id,
            program=program,
            programs_root=programs_root,
            templates_root=REPO_ROOT / "templates",
        )
    except Exception:
        return False
    policy = config.presentation.audience_policy
    if policy is None or not policy.new_recipient_approval:
        return False
    return bool(tuple(audience_manifest.get("added_since_last_attested") or ()))


def _find_draft_approval(
    approvals: list[dict[str, Any]],
    *,
    content_hash: str,
    filename: str,
) -> dict[str, Any] | None:
    for entry in reversed(approvals):
        if str(entry.get("content_hash") or "") == content_hash:
            return entry
        if str(entry.get("filename") or "") == filename:
            return entry
    return None


# ---------------------------------------------------------------------------
# --list-drafts  / --mark-sent  CLI handlers
# ---------------------------------------------------------------------------


def _cmd_list_drafts(program_id: str, *, nudge_paths: "Any") -> None:
    """Print available draft EML files in drafts/."""
    from src.core.edition_resolver import NudgePaths  # noqa: PLC0415
    np: NudgePaths = nudge_paths
    if not np.drafts_dir.exists():
        typer.echo(f"No drafts directory: {np.drafts_dir}")
        return
    emls = sorted(np.drafts_dir.glob("*.eml"), key=lambda p: p.name)
    if not emls:
        typer.echo("No draft EML files found.")
        return
    typer.echo(f"Draft EML files in {np.drafts_dir}:")
    for p in emls:
        size_kb = p.stat().st_size // 1024
        typer.echo(f"  {p.name}  ({size_kb}KB)")


def _cmd_approve_draft(
    program_id: str,
    *,
    draft_ref: str,
    nudge_paths: "Any",
    programs_root: Path,
    note: str | None = None,
) -> None:
    draft_path = _resolve_draft_path(draft_ref, nudge_paths=nudge_paths)
    prior_index = _load_published_index(nudge_paths.published_eml_index_path)
    source_run_id, item_ids = _extract_generated_item_ids_for_draft(
        nudge_paths.audit_path, draft_filename=draft_path.name
    )
    content_hash, audience_manifest = _parse_eml_audience_manifest(
        draft_path,
        prior_index=prior_index,
    )
    approval_index_path = _approval_index_path(nudge_paths)
    approved_at = datetime.now(timezone.utc)
    _update_draft_approval_index(
        approval_index_path,
        {
            "schema_version": "1.0",
            "program_id": program_id,
            "run_id": source_run_id or draft_path.stem,
            "filename": draft_path.name,
            "content_hash": content_hash,
            "audience": audience_manifest,
            "approved_at": approved_at.isoformat(),
            "actor": "operator",
            "item_ids": list(item_ids),
            "note": note or "",
        },
    )
    _append_audit(
        nudge_paths.audit_path,
        nudge_paths.audit_lock_path,
        NudgeAuditEvent(
            event_type="nudge_draft_approved",
            schema_version=NUDGE_AUDIT_SCHEMA_VERSION,
            run_id=f"approve_draft_{approved_at.strftime('%Y%m%dT%H%M%SZ')}",
            program_id=program_id,
            triggered_at=approved_at,
            dry_run=False,
            sections=(),
            total_items=len(item_ids),
            total_staleness_filtered=0,
            total_exempt_filtered=0,
            recipient=None,
            optional_recipient_failures=(),
            degraded_section_ids=(),
            warnings=(),
            eml_paths=(str(draft_path.relative_to(programs_root / program_id)),),
        ),
    )
    try:
        append_nudge_event(
            program_id,
            "event.nudge.draft_approved",
            {
                "run_id": source_run_id or draft_path.stem,
                "content_hash": content_hash,
                "approved_at": approved_at.isoformat(),
                "item_ids": list(item_ids),
                "audience": audience_manifest,
            },
            recorded_at=approved_at,
            db_root=programs_root.parent,
        )
    except Exception as exc:
        typer.echo(f"WARNING: nudge fact write failed during draft approval: {exc}", err=True)
    typer.echo(f"Approved draft: {draft_path}")


def _cmd_mark_sent(
    program_id: str,
    *,
    draft_ref: str,
    nudge_paths: "Any",
    programs_root: Path,
    note: str | None = None,
    sent_at_override: datetime | None = None,
) -> None:
    """Copy a draft to published_eml/ and record the mark-sent audit event.

    sent_at_override: operator-supplied send timestamp (--sent-at); defaults to now.
    """
    import shutil as _shutil  # noqa: PLC0415
    from src.core.edition_resolver import NudgePaths  # noqa: PLC0415
    np: NudgePaths = nudge_paths

    draft_path = _resolve_draft_path(draft_ref, nudge_paths=np)

    now_utc = datetime.now(timezone.utc)
    # Use operator-supplied timestamp if provided (--sent-at); attestation TS stays as now
    claimed_sent_at = sent_at_override if sent_at_override is not None else now_utc
    dest_filename = draft_path.name
    prior_index = _load_published_index(np.published_eml_index_path)
    source_run_id, item_ids = _extract_generated_item_ids_for_draft(np.audit_path, draft_filename=draft_path.name)
    content_hash, audience_manifest = _parse_eml_audience_manifest(draft_path, prior_index=prior_index)
    if _approval_required_for_manifest(
        program_id,
        programs_root=programs_root,
        audience_manifest=audience_manifest,
    ):
        approvals = _load_draft_approvals(_approval_index_path(np))
        if _find_draft_approval(approvals, content_hash=content_hash, filename=draft_path.name) is None:
            typer.echo(
                "ERROR: draft approval required before --mark-sent. "
                f"Run `vertex nudge --program {program_id} --approve-draft {draft_path.stem}` first.",
                err=True,
            )
            raise typer.Exit(code=2)
    np.published_eml_dir.mkdir(parents=True, exist_ok=True)
    dest_path = np.published_eml_dir / dest_filename
    _shutil.copy2(str(draft_path), str(dest_path))

    raw_state_path = getattr(np, "state_path", None)
    state_path = raw_state_path if isinstance(raw_state_path, Path) else (np.published_eml_dir.parent / "nudge_state.json")
    try:
        program, _workstreams = gather_helpers._load_program_context(program_id, programs_root)
        config = load_nudge_config(
            program_id=program_id,
            program=program,
            programs_root=programs_root,
            templates_root=REPO_ROOT / "templates",
        )
        max_stale = max(
            (s.stale_business_days for s in config.sections),
            default=7,
        )
        max_effective_cooldown = max(30, max_stale * 3)
    except Exception:
        max_effective_cooldown = 30
    prune_before = compute_prune_before(
        generated_at=claimed_sent_at,
        max_cooldown_days=max_effective_cooldown,
    )
    try:
        update_nudge_state(
            state_path,
            item_ids=item_ids,
            triggered_at=claimed_sent_at,
            prune_before=prune_before,
            origin="mark_sent",
            run_id=source_run_id or draft_path.stem,
        )
    except StateError as exc:
        typer.echo(f"ERROR: mark-sent cooldown update failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    # Update index.json
    _update_published_index(np.published_eml_index_path, {
        "schema_version": NUDGE_PUBLISHED_INDEX_SCHEMA_VERSION,
        "program_id": program_id,
        "run_id": source_run_id or draft_path.stem,
        "filename": dest_filename,
        "source_draft": str(draft_path.relative_to(programs_root / program_id)),
        "marked_sent_at": now_utc.isoformat(),
        "claimed_sent_at": claimed_sent_at.isoformat(),
        "content_hash": content_hash,
        "audience": audience_manifest,
        "item_ids": list(item_ids),
        "origin": "attested",
        "note": note or "",
    })

    # Emit audit event (non-fatal on failure)
    _np_audit = np.audit_path
    _np_audit_lock = np.audit_lock_path
    run_id = f"mark_sent_{now_utc.strftime('%Y%m%dT%H%M%SZ')}"
    _append_audit_fail_loud(
        _np_audit, _np_audit_lock,
        NudgeAuditEvent(
            event_type="nudge_marked_sent",
            schema_version=NUDGE_AUDIT_SCHEMA_VERSION,
            run_id=run_id,
            program_id=program_id,
            triggered_at=now_utc,
            dry_run=False,
            sections=(),
            total_items=len(item_ids),
            total_staleness_filtered=0,
            total_exempt_filtered=0,
            recipient=None,
            optional_recipient_failures=(),
            degraded_section_ids=(),
            warnings=(),
            eml_paths=(str(dest_path.relative_to(programs_root / program_id)),),
        ),
        context="mark-sent attestation audit",
    )

    try:
        append_nudge_event(
            program_id,
            "event.nudge.sent_attested",
            {
                "run_id": source_run_id or draft_path.stem,
                "content_hash": content_hash,
                "claimed_sent_at": claimed_sent_at.isoformat(),
                "marked_sent_at": now_utc.isoformat(),
                "item_ids": list(item_ids),
                "audience": audience_manifest,
            },
            recorded_at=now_utc,
            db_root=programs_root.parent,
        )
    except Exception as exc:
        typer.echo(f"WARNING: nudge fact write failed during mark-sent: {exc}", err=True)

    typer.echo(f"Marked as sent: {dest_path}")


def _cmd_import_sent(
    program_id: str,
    *,
    published_ref: str,
    nudge_paths: "Any",
    programs_root: Path,
    note: str | None = None,
    sent_at_override: datetime | None = None,
) -> None:
    published_path = _resolve_published_path(published_ref, nudge_paths=nudge_paths)
    now_utc = datetime.now(timezone.utc)
    prior_index = _load_published_index(nudge_paths.published_eml_index_path)
    matched_entry = next(
        (
            entry for entry in reversed(prior_index)
            if str(entry.get("filename") or "") == published_path.name
            or str(entry.get("run_id") or "") == published_path.stem
        ),
        None,
    )
    source_run_id = str(matched_entry.get("run_id") or "") if isinstance(matched_entry, dict) else ""
    matched_item_ids = tuple(
        int(v) for v in ((matched_entry or {}).get("item_ids") or [])
        if isinstance(v, int)
    ) if isinstance(matched_entry, dict) else ()
    if not matched_item_ids:
        audit_run_id, audit_item_ids = _extract_generated_item_ids_for_draft(
            nudge_paths.audit_path,
            draft_filename=published_path.name,
        )
        source_run_id = source_run_id or (audit_run_id or "")
        matched_item_ids = audit_item_ids
    content_hash, audience_manifest = _parse_eml_audience_manifest(
        published_path,
        prior_index=prior_index,
    )
    if sent_at_override is not None:
        claimed_sent_at = sent_at_override
    elif isinstance(matched_entry, dict) and matched_entry.get("claimed_sent_at"):
        claimed_sent_at = _ensure_utc(datetime.fromisoformat(str(matched_entry["claimed_sent_at"]).replace("Z", "+00:00")))
    else:
        claimed_sent_at = _ensure_utc(datetime.fromtimestamp(published_path.stat().st_mtime, tz=timezone.utc))

    raw_state_path = getattr(nudge_paths, "state_path", None)
    state_path = raw_state_path if isinstance(raw_state_path, Path) else (nudge_paths.published_eml_dir.parent / "nudge_state.json")
    prune_before = compute_prune_before(generated_at=claimed_sent_at, max_cooldown_days=14)
    try:
        update_nudge_state(
            state_path,
            item_ids=matched_item_ids,
            triggered_at=claimed_sent_at,
            prune_before=prune_before,
            origin="import_sent",
            run_id=source_run_id or published_path.stem,
        )
    except StateError as exc:
        typer.echo(f"ERROR: import-sent cooldown update failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if matched_entry is None:
        _update_published_index(nudge_paths.published_eml_index_path, {
            "schema_version": NUDGE_PUBLISHED_INDEX_SCHEMA_VERSION,
            "program_id": program_id,
            "run_id": source_run_id or published_path.stem,
            "filename": published_path.name,
            "source_draft": "",
            "marked_sent_at": now_utc.isoformat(),
            "claimed_sent_at": claimed_sent_at.isoformat(),
            "content_hash": content_hash,
            "audience": audience_manifest,
            "item_ids": list(matched_item_ids),
            "note": note or "imported from published_eml",
        })

    _append_audit(
        nudge_paths.audit_path,
        nudge_paths.audit_lock_path,
        NudgeAuditEvent(
            event_type="nudge_imported_sent",
            schema_version=NUDGE_AUDIT_SCHEMA_VERSION,
            run_id=f"import_sent_{now_utc.strftime('%Y%m%dT%H%M%SZ')}",
            program_id=program_id,
            triggered_at=now_utc,
            dry_run=False,
            sections=(),
            total_items=len(matched_item_ids),
            total_staleness_filtered=0,
            total_exempt_filtered=0,
            recipient=None,
            optional_recipient_failures=(),
            degraded_section_ids=(),
            warnings=(),
            eml_paths=(str(published_path.relative_to(programs_root / program_id)),),
        ),
    )
    try:
        append_nudge_event(
            program_id,
            "event.nudge.sent_imported",
            {
                "run_id": source_run_id or published_path.stem,
                "content_hash": content_hash,
                "claimed_sent_at": claimed_sent_at.isoformat(),
                "imported_at": now_utc.isoformat(),
                "item_ids": list(matched_item_ids),
                "audience": audience_manifest,
            },
            recorded_at=now_utc,
            db_root=programs_root.parent,
        )
    except Exception as exc:
        typer.echo(f"WARNING: nudge fact write failed during import-sent: {exc}", err=True)
    typer.echo(f"Imported sent EML: {published_path}")


def _update_published_index(index_path: Path, entry: dict[str, object]) -> None:
    """Append *entry* to the published_eml/index.json manifest."""
    existing: list[dict[str, object]] = []
    if index_path.exists():
        try:
            raw = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                existing = raw
        except (json.JSONDecodeError, OSError):
            pass
    existing.append(entry)
    index_path.write_text(
        json.dumps(existing, indent=2, default=str),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Candidate fetching (bounded parallel or sequential)
# ---------------------------------------------------------------------------


def _fetch_all_sections(
    sections: tuple[NudgeSectionSpec, ...],
    *,
    program: Any,
    authored_registry: tuple[Any, ...],
    workstreams: tuple[Any, ...],
    now_utc: datetime,
    client_factory: Callable[[Any], NudgeADOClient] | None,
    candidate_workers: int,
) -> list[NudgeSectionFetchResult]:
    factory = client_factory or _default_client_factory

    if candidate_workers <= 1 or len(sections) <= 1:
        results: list[NudgeSectionFetchResult] = []
        for sec in sections:
            client = factory(program)
            results.append(fetch_section_candidates(
                program=program, section=sec, authored_registry=authored_registry,
                workstreams=workstreams, client=client, as_of=now_utc,
            ))
        return results

    # Bounded parallel fetch — one client per submitted task
    ordered: list[tuple[int, NudgeSectionFetchResult]] = []
    with ThreadPoolExecutor(max_workers=candidate_workers) as executor:
        future_to_idx: dict[Any, int] = {}
        for idx, sec in enumerate(sections):
            client = factory(program)
            future = executor.submit(
                fetch_section_candidates,
                program=program, section=sec, authored_registry=authored_registry,
                workstreams=workstreams, client=client, as_of=now_utc,
            )
            future_to_idx[future] = idx
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                result = future.result()
            except Exception as exc:
                result = NudgeSectionFetchResult(
                    section_id=sections[idx].id, candidates=(),
                    query_error=True, error_details=str(exc)[:500],
                )
            ordered.append((idx, result))

    ordered.sort(key=lambda t: t[0])
    return [r for _, r in ordered]


def _default_client_factory(program: Any) -> NudgeADOClient:
    if program.ado is None:
        raise ConfigError("Program ADO configuration is required for candidate fetch.")
    return ADOClient(  # type: ignore[return-value]
        organization=program.ado.organization,
        project=program.ado.project,
        timeout=program.ado.api_timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Recipient resolution
# ---------------------------------------------------------------------------


def _resolve_primary_recipient(
    alias: str,
    people: tuple[Any, ...],
    *,
    program_id: str,
) -> ResolvedRecipient:
    alias = alias.strip()
    if not alias:
        raise ConfigError("full_hygiene.recipient must be a non-empty people-directory alias.")
    alias_lower = alias.lower()
    for person in people:
        if (getattr(person, "alias", None) or "").strip().lower() == alias_lower:
            email = getattr(person, "email", None)
            if email and _is_valid_email(email):
                return ResolvedRecipient(
                    alias=alias,
                    email=email.strip().lower(),
                    display_name=getattr(person, "display_name", None) or alias,
                )
    raise ConfigError(
        f"Primary nudge recipient alias {alias!r} not found in people directory "
        f"for program {program_id!r}. Configure a valid alias."
    )


def _resolve_optional_recipient(
    alias: str | None,
    email: str | None,
    people: tuple[Any, ...],
    *,
    failures: list[str],
) -> ResolvedRecipient | None:
    """Try to resolve a workstream owner or assignee. Returns None and appends failure on miss."""
    # Direct email?
    if email and _is_valid_email(email):
        return ResolvedRecipient(alias=alias or email.split("@")[0], email=email.strip().lower(), display_name=alias or "")
    # Alias lookup
    if alias:
        alias_lower = alias.lower()
        for person in people:
            if (getattr(person, "alias", None) or "").strip().lower() == alias_lower:
                pemail = getattr(person, "email", None)
                if pemail and _is_valid_email(pemail):
                    return ResolvedRecipient(
                        alias=alias,
                        email=pemail.strip().lower(),
                        display_name=getattr(person, "display_name", None) or alias,
                    )
        failures.append(f"unresolved alias {alias!r}")
    return None


def _is_valid_email(email: str) -> bool:
    from email.utils import parseaddr  # noqa: PLC0415
    if not email or not email.strip():
        return False
    for ch in email:
        if ord(ch) < 32 or ord(ch) == 127:
            return False
    _, addr = parseaddr(email.strip())
    if not addr or addr != email.strip():
        return False
    parts = addr.split("@")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False
    if parts[1].lower() == "example.com":
        return False
    return True


def _build_recipient_list(
    *,
    primary_recipient: ResolvedRecipient,
    sections: list[FullHygieneSection],
    workstream_by_id: dict[str, Any],
    people: tuple[Any, ...],
    config: NudgeConfig,
    optional_failures: list[str],
) -> list[ResolvedRecipient]:
    seen: dict[str, ResolvedRecipient] = {}  # keyed by lowercase email

    def _add(r: ResolvedRecipient) -> None:
        key = r.email.lower()
        if key not in seen:
            seen[key] = r

    _add(primary_recipient)

    if config.delivery.include_workstream_owners:
        for sec in sections:
            for grp in sec.groups:
                for owner in grp.workstream_owners:
                    _add(owner)

    if config.delivery.include_item_assignees:
        for sec in sections:
            for grp in sec.groups:
                for row in grp.rows:
                    if row.owner_email and _is_valid_email(row.owner_email):
                        r = ResolvedRecipient(
                            alias=row.owner_alias or row.owner_email.split("@")[0],
                            email=row.owner_email.lower(),
                            display_name=row.owner_alias or "",
                        )
                        _add(r)

    return list(seen.values())


def _apply_audience_policy(
    *,
    program_id: str,
    config: NudgeConfig,
    primary_recipient: ResolvedRecipient,
    recipients: list[ResolvedRecipient],
    optional_failures: list[str],
) -> tuple[list[ResolvedRecipient], list[ResolvedRecipient]]:
    policy = config.presentation.audience_policy
    if policy is None:
        return recipients, []

    blocked_domains: list[str] = []
    blocked_opt_out: list[str] = []
    filtered: list[ResolvedRecipient] = []
    allowed_domains = {d.strip().lower() for d in policy.allowed_domains if d.strip()}
    opt_out = {o.strip().lower() for o in policy.opt_out}

    for recipient in recipients:
        email_lower = recipient.email.lower()
        alias_lower = recipient.alias.lower()
        domain = email_lower.split("@", 1)[1] if "@" in email_lower else ""
        if allowed_domains and domain not in allowed_domains:
            blocked_domains.append(recipient.email)
            continue
        if email_lower in opt_out or alias_lower in opt_out:
            blocked_opt_out.append(recipient.email)
            continue
        filtered.append(recipient)

    if blocked_domains:
        raise ConfigError(
            f"Audience policy blocked recipient domain(s) for program {program_id!r}: "
            + ", ".join(sorted(blocked_domains))
        )

    if blocked_opt_out:
        if policy.opt_out_fallback == "gap":
            append_context_gap(
                feature="nudge",
                program=program_id,
                lane=None,
                field="audience_policy.opt_out",
                severity="quality_degraded",
                message="Audience policy opt-out removed recipient(s): " + ", ".join(sorted(blocked_opt_out)),
                impact_estimate="medium",
            )

    if optional_failures and policy.unresolved_owner == "fail":
        raise ConfigError(
            "Audience policy unresolved_owner=fail and some recipients could not be resolved: "
            + "; ".join(optional_failures)
        )

    if not filtered:
        raise ConfigError(f"Audience policy removed all recipients for program {program_id!r}.")

    if len(filtered) > policy.max_recipients:
        raise ConfigError(
            f"Audience policy max_recipients={policy.max_recipients} exceeded "
            f"({len(filtered)} resolved recipients)."
        )

    if policy.delivery_mode == "bcc":
        return [primary_recipient], [r for r in filtered if r.email.lower() != primary_recipient.email.lower()]
    return filtered, []


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------


def _build_full_hygiene_row(
    item: WorkItem,
    *,
    compressed_title: str,
    workstream_id: str | None,
    workstream_name: str | None,
    as_of: datetime,
    program: Any,
    has_recent_comment: bool | None,
    comment_has_status_keyword: bool | None,
    risk_on_track_values: tuple[str, ...],
    ado_overdue_ids: frozenset[int],
    stale_days: int,
) -> FullHygieneRow:
    today = _ensure_utc(as_of).date()
    td = item.target_date
    has_valid_target_date = td is not None and td >= today
    # CommitmentStatus is the authoritative field. No fallback: items without it are not committed.
    _commitment_status = str(item.custom_fields.get("Custom.CommitmentStatus") or "").strip()
    has_committed = _commitment_status.lower().startswith("committed")
    ra = (item.risk_assessment or "").strip()
    has_risk_assessment = bool(ra)
    risk_is_on_track = ra in risk_on_track_values
    if has_risk_assessment and not risk_is_on_track:
        rac = (item.risk_assessment_comment or "").strip()
        has_risk_reason: bool | None = len(rac) > 10
    else:
        has_risk_reason = None

    # Three-valued readiness
    required_checks: list[bool | None] = [
        has_valid_target_date,
        has_risk_assessment,
        has_recent_comment,
    ]
    if comment_has_status_keyword is not None:
        required_checks.append(comment_has_status_keyword)

    is_ready: bool | None
    if any(c is False for c in required_checks):
        is_ready = False
    elif all(c is True for c in required_checks):
        is_ready = True
    else:
        is_ready = None

    stale_bdays = _business_days_for_item(item, as_of)
    url = _work_item_url(program, item.id)

    return FullHygieneRow(
        work_item_id=item.id,
        title=compressed_title,
        title_original=item.title,
        item_url=url,
        item_type=item.type,
        owner_alias=_normalize_alias(item.assigned_to_email),
        owner_email=item.assigned_to_email,
        workstream_id=workstream_id,
        workstream_name=workstream_name,
        has_valid_target_date=has_valid_target_date,
        has_committed=has_committed,
        has_risk_assessment=has_risk_assessment,
        risk_is_on_track=risk_is_on_track,
        has_risk_reason=has_risk_reason,
        has_recent_comment=has_recent_comment,
        comment_has_status_keyword=comment_has_status_keyword,
        is_ready=is_ready,
        stale_business_days=stale_bdays,
        is_overdue=item.id in ado_overdue_ids,
        target_date=td,
    )


# ---------------------------------------------------------------------------
# Workstream grouping
# ---------------------------------------------------------------------------


def _group_rows_by_workstream(
    rows: list[FullHygieneRow],
    *,
    workstream_by_id: dict[str, Any],
    people: tuple[Any, ...],
    program_id: str | None = None,
    owner_roles: tuple[str, ...] = ("tpm_lead", "eng_lead"),
) -> tuple[FullHygieneWorkstreamGroup, ...]:
    groups_map: dict[str | None, list[FullHygieneRow]] = defaultdict(list)
    for row in rows:
        groups_map[row.workstream_id].append(row)

    result: list[FullHygieneWorkstreamGroup] = []
    seen: set[str | None] = set()

    for ws_id, ws_entry in workstream_by_id.items():
        if ws_id in groups_map:
            ws_name = getattr(ws_entry, "name", None) or ws_id
            owners = _workstream_owners_from_registry(
                ws_entry, people, program_id=program_id, owner_roles=owner_roles
            )
            result.append(FullHygieneWorkstreamGroup(
                workstream_id=ws_id,
                workstream_name=ws_name,
                workstream_owners=tuple(owners),
                rows=tuple(groups_map[ws_id]),
            ))
            seen.add(ws_id)

    unclassified: list[FullHygieneRow] = []
    for ws_key, ws_rows in groups_map.items():
        if ws_key not in seen:
            unclassified.extend(ws_rows)
    if unclassified:
        result.append(FullHygieneWorkstreamGroup(
            workstream_id=None,
            workstream_name="Unclassified",
            workstream_owners=(),
            rows=tuple(unclassified),
        ))

    return tuple(result)


def _workstream_owners_from_registry(
    ws_entry: Any,
    people: tuple[Any, ...],
    *,
    program_id: str | None = None,
    owner_roles: tuple[str, ...] = ("tpm_lead", "eng_lead"),
) -> list[ResolvedRecipient]:
    """Resolve stakeholders whose role is in owner_roles to ResolvedRecipient."""
    owners: list[ResolvedRecipient] = []
    owner_role_set = {r.lower() for r in owner_roles}
    stakeholders = getattr(ws_entry, "stakeholders", None) or []
    for stakeholder in stakeholders:
        role = str(getattr(stakeholder, "role", "") or "").lower()
        if role not in owner_role_set:
            continue
        alias = getattr(stakeholder, "alias", None) or ""
        name = getattr(stakeholder, "name", None) or ""
        direct_email = getattr(stakeholder, "email", None)

        resolved = None

        if direct_email and _is_valid_email(direct_email):
            resolved = ResolvedRecipient(
                alias=alias or direct_email.split("@")[0],
                email=direct_email.strip().lower(),
                display_name=name or alias,
            )
        elif alias:
            alias_lower = alias.strip().lower()
            for person in people:
                if (getattr(person, "alias", None) or "").strip().lower() == alias_lower:
                    pemail = getattr(person, "email", None)
                    if pemail and _is_valid_email(pemail):
                        resolved = ResolvedRecipient(
                            alias=alias,
                            email=pemail.strip().lower(),
                            display_name=getattr(person, "display_name", None) or alias,
                        )
                    break
        if resolved is None and name:
            name_lower = name.lower()
            for person in people:
                if (getattr(person, "display_name", None) or "").lower() == name_lower:
                    pemail = getattr(person, "email", None)
                    if pemail and _is_valid_email(pemail):
                        resolved = ResolvedRecipient(
                            alias=alias or _normalize_alias(name) or name,
                            email=pemail.strip().lower(),
                            display_name=name,
                        )
                    break

        if resolved is not None:
            owners.append(resolved)
        else:
            if program_id:
                try:
                    ws_id_str = str(getattr(ws_entry, "id", "") or "")
                    append_context_gap(
                        feature="nudge", program=program_id, lane=ws_id_str or None,
                        field="roles.primary_owner.email", severity="quality_degraded",
                        message=f"primary_owner '{name or alias}' not resolved; no email in directory",
                        impact_estimate="medium",
                    )
                except Exception as _gap_exc:
                    typer.echo(f"WARNING: nudge context-gap append failed: {_gap_exc}", err=True)

    return owners


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _hygiene_template_environment(templates_root: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(templates_root)),
        undefined=StrictUndefined,
        autoescape=select_autoescape(enabled_extensions=("html", "xml", "j2"), default_for_string=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _render_full_hygiene_html(
    *,
    subject: str,
    artifacts: FullHygieneArtifacts,
    brand_label: str,
    template_name: str,
    preheader: str | None,
    templates_root: Path,
    comment_window_days: int,
) -> str:
    env = _hygiene_template_environment(templates_root)
    try:
        partial = env.get_template(template_name)
        base = env.get_template("base.email.j2")
    except TemplateNotFound as error:
        raise ConfigError(f"Missing nudge template: templates/{error}") from error
    try:
        content_html = partial.render(
            sections=artifacts.sections,
            generated_at_iso=artifacts.generated_at.strftime("%Y-%m-%d %H:%M UTC"),
            comment_window_days=comment_window_days,
            degraded_section_ids=list(artifacts.degraded_section_ids),
        )
    except Exception as exc:
        raise RenderError(f"Template render error in {template_name}: {exc}") from exc
    resolved_preheader = preheader or f"{brand_label} — ADO hygiene readiness sweep"
    return base.render(
        title=subject,
        preheader=resolved_preheader,
        header_label=None,
        subtitle=f"Generated {artifacts.generated_at.strftime('%Y-%m-%d')}",
        footer_text="Generated by Vertex. Send questions to the program manager.",
        show_footer=True,
        content_html=content_html,
    )


def _render_full_hygiene_plaintext(artifacts: FullHygieneArtifacts) -> str:
    lines: list[str] = [
        f"Full ADO Readiness - {artifacts.generated_at.strftime('%Y-%m-%d')}",
        "",
    ]
    visible_sections = [s for s in artifacts.sections if s.total_count > 0 or s.query_error]
    for section in artifacts.sections:
        if section.query_error:
            lines.append(f"=== Section {section.letter}: {section.title} === [DEGRADED]")
            lines.append(f"    Data unavailable: {section.error_details or 'query error'}")
            lines.append("")
            continue
        if len(visible_sections) > 1:
            lines.append(f"=== Section {section.letter}: {section.title} ===")
        else:
            lines.append(f"=== {section.title} ===")
        lines.append(
            f"    {section.total_count} items, "
            f"{section.stale_count} stale (>={section.stale_threshold_days}d), "
            f"{section.ready_count} ready"
        )
        if section.unknown_ready_count > 0:
            lines.append(f"    {section.unknown_ready_count} with unknown readiness")
        lines.append("")
        for group in section.groups:
            owner_part = ""
            if group.workstream_owners:
                aliases = "/".join(f"@{o.alias}" for o in group.workstream_owners if o.alias)
                if aliases:
                    owner_part = f"  Owner: {aliases}"
            lines.append(f"  > {group.workstream_name}{owner_part}")
            for row in group.rows:
                stale_str = f"{row.stale_business_days}d"
                rcmt = "Y" if row.has_recent_comment is True else ("N" if row.has_recent_comment is False else "?")
                ckw = "Y" if row.comment_has_status_keyword is True else ("N" if row.comment_has_status_keyword is False else "?")
                checks = " ".join([
                    "Y" if row.has_valid_target_date else "N",
                    "Y" if row.has_committed else "N",
                    "Y" if row.has_risk_assessment else "N",
                    ("Y" if row.has_risk_reason else "N") if row.has_risk_reason is not None else "-",
                    rcmt, ckw,
                ])
                ready_str = "Y" if row.is_ready is True else ("N" if row.is_ready is False else "?")
                alias = f"@{row.owner_alias}" if row.owner_alias else "-"
                lines.append(f"    WI:{row.work_item_id}  {row.title[:45]}  {alias}  {checks}  {stale_str}  {ready_str}")
        lines.append("")
    if artifacts.ai_titles_compressed:
        lines.append(f"[AI compressed {artifacts.ai_titles_compressed} title(s)]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _append_audit(audit_path: Path, audit_lock_path: Path, event: NudgeAuditEvent) -> None:
    """Write audit event; log to stderr on failure (never silent)."""
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(
            str(audit_lock_path), mode="a+", timeout=NUDGE_STATE_LOCK_TIMEOUT_SECONDS, encoding="utf-8"
        ):
            append_jsonl_line(audit_path, build_audit_line(event), max_bytes=NUDGE_AUDIT_MAX_BYTES)
    except Exception as _audit_exc:
        typer.echo(
            f"WARNING: nudge audit write failed ({event.event_type}): {_audit_exc}. "
            "Audit log may be incomplete. Check disk/lock state.",
            err=True,
        )


def _append_audit_fail_loud(
    audit_path: Path,
    audit_lock_path: Path,
    event: NudgeAuditEvent,
    *,
    context: str = "",
) -> None:
    """Write audit event for attestation paths — propagates as a warning + degrades exit.

    Attestation audit failures are surfaced loudly because a silent failure
    here would leave the system without an audit trail of a claimed send.
    """
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(
            str(audit_lock_path), mode="a+", timeout=NUDGE_STATE_LOCK_TIMEOUT_SECONDS, encoding="utf-8"
        ):
            append_jsonl_line(audit_path, build_audit_line(event), max_bytes=NUDGE_AUDIT_MAX_BYTES)
    except Exception as _audit_exc:
        ctx = f" ({context})" if context else ""
        typer.echo(
            f"ERROR: nudge audit write failed{ctx}: {_audit_exc}. "
            "The attestation was recorded in the publication index but the audit log is incomplete. "
            "Reconcile manually: re-run with --list-drafts to verify state.",
            err=True,
        )


# ---------------------------------------------------------------------------
# Title compression (preserved from original)
# ---------------------------------------------------------------------------


def _word_truncate_title(title: str, max_len: int = 50) -> str:
    if len(title) <= max_len:
        return title
    truncated = title[:max_len].rsplit(None, 1)[0]
    return truncated.rstrip("., ") + "…"


def _load_title_cache(cache_path: Path) -> dict[str, str]:
    if not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_title_cache(
    cache_path: Path,
    current_rows: list[tuple[int, str, str]],
    old_cache: dict[str, str],
) -> None:
    import hashlib  # noqa: PLC0415
    next_cache: dict[str, str] = {}
    for wid, orig_title, compressed in current_rows:
        if len(next_cache) >= NUDGE_TITLE_CACHE_MAX_ENTRIES:
            break
        sha = hashlib.sha256(orig_title.encode()).hexdigest()[:16]
        key = f"item:{wid}:{sha}"
        next_cache[key] = compressed

    for old_key, old_val in reversed(list(old_cache.items())):
        if len(next_cache) >= NUDGE_TITLE_CACHE_MAX_ENTRIES:
            break
        if old_key not in next_cache:
            next_cache[old_key] = old_val

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    unique = uuid.uuid4().hex[:8]
    temp_path = cache_path.parent / f".title_cache_{pid}_{unique}.tmp"
    try:
        with temp_path.open("w", encoding="utf-8") as fh:
            json.dump(next_cache, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, cache_path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _compress_titles_batch(
    items: list[tuple[int, str]],
    *,
    cache_path: Path,
    enabled: bool,
    program: Any | None = None,
) -> tuple[dict[int, str], int]:
    import hashlib  # noqa: PLC0415
    cache = _load_title_cache(cache_path)
    result: dict[int, str] = {}
    needs_ai: list[tuple[int, str]] = []
    ai_count = 0
    for wid, title in items:
        if len(title) <= 50:
            result[wid] = title
            continue
        # Try canonical sha256 key first, then legacy prefix key
        sha = hashlib.sha256(title.encode()).hexdigest()[:16]
        canonical_key = f"item:{wid}:{sha}"
        legacy_key = f"{wid}:{title[:50]}"
        if canonical_key in cache:
            result[wid] = cache[canonical_key]
        elif legacy_key in cache:
            result[wid] = cache[legacy_key]
            cache[canonical_key] = cache[legacy_key]
        elif enabled and program is not None:
            needs_ai.append((wid, title))
        else:
            result[wid] = _word_truncate_title(title)
    if needs_ai:
        try:
            compressed = _ai_batch_compress_titles(needs_ai, program=program)
            for wid, orig_title in needs_ai:
                compressed_title = compressed.get(wid) or _word_truncate_title(orig_title)
                result[wid] = compressed_title
                if wid in compressed:
                    ai_count += 1
        except Exception:
            for wid, orig_title in needs_ai:
                result[wid] = _word_truncate_title(orig_title)
    return result, ai_count


def _ai_batch_compress_titles(items: list[tuple[int, str]], *, program: Any) -> dict[int, str]:
    if get_ai_mode() == AIMode.DISABLED:
        return {}
    from src.ai.deployment_fallback import FallbackStructuredClient, resolve_ai_deployments_for_feature  # noqa: PLC0415
    deployments = resolve_ai_deployments_for_feature(
        feature_name="default",
        primary_candidates=(program.ai.blurb_deployment if program.ai is not None else None,),
        backup_candidates=(None,),
        primary_fallback_envs=("VERTEX_AI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"),
        backup_fallback_envs=(),
    )
    if not deployments:
        raise RuntimeError("No AI deployment configured for title compression.")
    client = FallbackStructuredClient(deployments=deployments, temperature=0.0, budget_usd=0.10)
    lines = "\n".join(f"{wid}: {title}" for wid, title in items)
    system_prompt = (
        "You are a technical editor. Compress each work item title to ≤50 characters "
        "while preserving the core technical meaning. Respond with one line per item "
        "in the format: '<id>: <compressed title>'. No preamble."
    )
    user_prompt = f"Compress these ADO work item titles to ≤50 characters:\n{lines}"

    def _parse(payload: dict[str, Any]) -> dict[int, str]:
        parsed: dict[int, str] = {}
        content = str(payload.get("content") or payload.get("text") or "")
        for line in content.strip().splitlines():
            if ":" in line:
                id_part, _, title_part = line.partition(":")
                try:
                    safe_title = process_generated_text(title_part.strip()).text
                    if safe_title:
                        parsed[int(id_part.strip())] = safe_title[:55]
                except ValueError:
                    pass
        return parsed

    return client.structured(system_prompt, user_prompt, parser=_parse, max_tokens=800, prompt_version="title_compress.v1")


# ---------------------------------------------------------------------------
# Registry audit (read-only)
# ---------------------------------------------------------------------------


def _run_registry_audit(
    program_id: str,
    *,
    programs_root: Path,
    output_path: Path | None,
) -> None:
    authored_registry = load_authored_workstream_registry(
        program_id=program_id,
        programs_root=programs_root,
    )
    if not authored_registry:
        typer.echo("No authored registry entries found.")
        return

    # Collect all unique key_ado_items
    all_ids: list[int] = []
    seen: set[int] = set()
    for entry in authored_registry:
        for wid in getattr(entry, "key_ado_items", ()):
            if wid not in seen:
                seen.add(wid)
                all_ids.append(wid)

    # Build lines (TSV)
    header = "workstream_id\tkey_ado_count\thydrated_count\tmissing_count\tmissing_ids"
    lines = [header]
    total_key = 0
    total_hydrated = 0
    total_missing = 0
    total_missing_ids: list[int] = []

    for entry in authored_registry:
        ws_id = getattr(entry, "id", "")
        key_items = list(getattr(entry, "key_ado_items", ()))
        missing_ids: list[int] = []
        # For audit: we can't do live ADO; just report key_ado_count
        key_count = len(key_items)
        hydrated_count = key_count  # approximate without live call
        missing_count = 0
        missing_str = ""
        line = f"{ws_id}\t{key_count}\t{hydrated_count}\t{missing_count}\t{missing_str}"
        lines.append(line)
        total_key += key_count
        total_hydrated += hydrated_count
        total_missing += missing_count
        total_missing_ids.extend(missing_ids)

    lines.append(f"TOTAL\t{total_key}\t{total_hydrated}\t{total_missing}\t{','.join(str(x) for x in sorted(total_missing_ids))}")
    output = "\n".join(lines)
    typer.echo(output)

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
        typer.echo(f"Registry audit written to {output_path}")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _comment_has_keyword(text: str | None, keywords: tuple[str, ...]) -> bool:
    if not text:
        return False
    lower = text.lower()
    return any(kw.lower() in lower for kw in keywords)


def _fetch_latest_comment_text(
    work_item_id: int,
    *,
    client: ADOClient,
    cutoff: datetime,
) -> str | None:
    try:
        comments = client.list_work_item_comments(work_item_id)
    except QueryError:
        raise
    best: datetime | None = None
    best_text: str | None = None
    for comment in comments:
        ts = _comment_timestamp(comment)
        if ts is None or ts < cutoff:
            continue
        if best is None or ts > best:
            best = ts
            best_text = str(comment.get("text") or comment.get("content") or "")
    return best_text


def _comment_timestamp(comment: dict[str, Any]) -> datetime | None:
    if not isinstance(comment, dict):
        return None
    for key in ("createdDate", "modifiedDate", "updatedDate"):
        ts = _parse_datetime(comment.get(key))
        if ts is not None:
            return ts
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _ensure_utc(value)
    try:
        return _ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _business_days_for_item(item: Any, as_of: datetime) -> int:
    from src.core.ado_semantics import latest_meaningful_ado_update  # noqa: PLC0415
    latest = latest_meaningful_ado_update(item)
    if latest is None:
        return 9999
    return business_days_since(latest, as_of)


def _work_item_url(program: Any, work_item_id: int) -> str:
    ado = getattr(program, "ado", None)
    if ado is None:
        return f"https://dev.azure.com/_workitems/edit/{work_item_id}"
    return f"https://dev.azure.com/{ado.organization}/{ado.project}/_workitems/edit/{work_item_id}"


def _normalize_alias(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if "@" in normalized:
        normalized = normalized.split("@", 1)[0]
    return normalized or None


def _ws_registry_index(ws_id: str | None, authored_registry: tuple[Any, ...]) -> int:
    if ws_id is None:
        return 9999
    for i, entry in enumerate(authored_registry):
        if getattr(entry, "id", None) == ws_id:
            return i
    return 9999


def _apply_legacy_stale_flags(
    overrides: dict[str, int],
    config: NudgeConfig,
    *,
    stale_a: int | None,
    stale_b: int | None,
    stale_c: int | None,
) -> None:
    mapping = [(stale_a, 0), (stale_b, 1), (stale_c, 2)]
    for days, section_pos in mapping:
        if days is not None and section_pos < len(config.sections):
            sec_id = config.sections[section_pos].id
            if sec_id not in overrides:
                overrides[sec_id] = days


def _program_needs_ado(program_id: str, programs_root: Path) -> bool:
    """True when the nudge edition references any non-registry sections."""
    edition_path = programs_root / program_id / "editions" / f"{program_id}_nudge.yaml"
    if not edition_path.exists():
        return False
    try:
        raw = load_yaml_mapping(edition_path, required=False)
        fh = raw.get("full_hygiene") or {}
        secs = fh.get("sections") or []
        return any(
            isinstance(s, dict) and str(s.get("criteria", {}).get("source", "")) in ("tag", "area_path")
            for s in (secs if isinstance(secs, list) else [])
        )
    except Exception:
        return False
