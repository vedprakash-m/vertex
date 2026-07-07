"""vertex enrich: on-demand email/transcript evidence extraction.

Fetches fresh WorkIQ content and runs ContentExtractionAgent to populate
WorkstreamEvidence for each lane. Complements `vertex gather` which only
captures 255-char email previews.

Zone: This command lives in src/commands/ (not zone-restricted).
It may call Zone B (ContentExtractionAgent) and Zone C (AgencyBridge).
Zone A (evidence_models) may be imported freely.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.evidence_models import WorkstreamEvidence
    from src.core.circuit_breaker import CircuitBreaker
    from src.core.cost_guard import CostGuard
from uuid import uuid4
import typer

from src.core.edition_resolver import get_program_output_dir, resolve_edition
from src.core.program_context import load_program_context

log = logging.getLogger(__name__)

_PROGRAMS_ROOT = Path("programs")
_EDITIONS_ROOT = Path("editions")
_WORKIQ_ZERO_YIELD_SUSPEND_THRESHOLD = 3
_WORKIQ_ESTIMATED_CALL_FLOOR_USD = 0.01
_WORKIQ_ESTIMATED_INPUT_COST_PER_1K_CHARS = 0.004
_WORKIQ_ESTIMATED_OUTPUT_COST_PER_1K_CHARS = 0.008


def enrich_command(
    edition: str = typer.Option(..., "--edition", help="Edition name (e.g. acme_weekly)"),
    lane: Optional[str] = typer.Option(None, "--lane", help="Target a single lane ID. Omit for all lanes."),
    since: str = typer.Option("7d", "--since", help="Lookback window (e.g. 7d, 14d)"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    accept: bool = typer.Option(False, "--accept", help="Also update workiq_latest in registry with AI summary."),
    output_format: str = typer.Option("human", "--format", help="Output format: human | json"),
    batch: bool = typer.Option(True, "--batch/--no-batch", help="P4-17: batch lanes sharing a meeting series into one WorkIQ call."),
) -> None:
    """Extract structured evidence from emails and transcripts via WorkIQ."""
    programs_root = _PROGRAMS_ROOT
    editions_root = _EDITIONS_ROOT

    resolved = resolve_edition(edition, editions_root=editions_root, programs_root=programs_root)
    if resolved is None:
        typer.echo(f"ERROR: Edition '{edition}' not found.", err=True)
        raise typer.Exit(1)

    program_id = resolved.paths.program_id
    _ctx = load_program_context(program_id, programs_root=programs_root, editions_root=editions_root)

    # Parse since window
    since_days = _parse_since(since)
    as_of = datetime.now(timezone.utc)
    since_dt = as_of - timedelta(days=since_days)
    workiq_cost_guard = _build_workiq_cost_guard(
        edition=edition,
        program=resolved.program,
        started_at=as_of,
        programs_root=programs_root,
    )

    # Load registry lanes
    registry_entries = _load_registry_lanes(program_id, programs_root)
    if lane:
        registry_entries = [e for e in registry_entries if e.get("id") == lane]
        if not registry_entries:
            typer.echo(f"ERROR: Lane '{lane}' not found in registry.", err=True)
            raise typer.Exit(1)

    # Initialize AgencyBridge
    try:
        from src.m365.agency_bridge import AgencyBridge
        bridge = AgencyBridge(
            workiq_breaker=_build_workiq_breaker(
                edition=edition,
                programs_root=programs_root,
            )
        )
        caps = bridge.probe()
        if not caps.available or not caps.has_workiq:
            typer.echo("ERROR: WorkIQ access unavailable.", err=True)
            raise typer.Exit(1)
    except ImportError:
        typer.echo("ERROR: AgencyBridge not available.", err=True)
        raise typer.Exit(1)

    from src.ai.content_extractor import ContentExtractionAgent, ExtractionContext
    from src.commands.gather_pipeline.evidence_extraction_stage import persist_evidence

    agent = ContentExtractionAgent(ask_ai_fn=lambda prompt: bridge.ask_workiq(prompt))  # type: ignore[arg-type, return-value]
    results: list[dict] = []

    # P4-17: cluster lanes that share meeting-series names into a single WorkIQ call
    # to reduce API budget from N lanes to K clusters (K << N for programs with shared meetings).
    # Each cluster issues ONE WorkIQ call; ContentExtractionAgent runs per-lane on the result.
    m365_config = getattr(resolved.program, "m365", None)
    retrieval_config = getattr(m365_config, "retrieval", None)
    per_thread_enabled = bool(retrieval_config and retrieval_config.per_thread_extraction)
    if batch and not lane and not per_thread_enabled:
        cluster_cache = _build_cluster_response_cache(
            registry_entries=registry_entries,
            since_dt=since_dt,
            edition=edition,
            programs_root=programs_root,
            bridge=bridge,
            workiq_cost_guard=workiq_cost_guard,
        )
    else:
        cluster_cache = {}

    for entry in registry_entries:
        lane_id = entry.get("id", "?")
        lane_name = entry.get("name", lane_id)
        typer.echo(f"  Extracting: {lane_id}...", nl=False)

        if _is_workiq_lane_suspended(
            lane_entry=entry,
            lane_id=lane_id,
            edition=edition,
            programs_root=programs_root,
        ):
            typer.echo(" [skipped — suspended after 3 zero-yield runs]")
            continue

        if per_thread_enabled and retrieval_config is not None:
            per_thread_evidence = _extract_per_thread_evidence(
                bridge=bridge,
                entry=entry,
                since_dt=since_dt,
                as_of=as_of,
                retrieval_config=retrieval_config,
                edition=edition,
                programs_root=programs_root,
            )
            _record_workiq_lane_result(
                lane_id=lane_id,
                edition=edition,
                program_id=program_id,
                programs_root=programs_root,
                observed_yield=len(per_thread_evidence),
                as_of=as_of,
                last_result="per_thread_evidence" if per_thread_evidence else "no_structured_evidence",
            )
            for evidence in per_thread_evidence:
                results.append({
                    "lane_id": lane_id,
                    "source_id": evidence.source_refs[0].canonical_id if evidence.source_refs else None,
                    "risk_level": evidence.risk_level.value,
                    "etas_found": len(evidence.etas),
                    "blocking_found": len(evidence.blocking_items),
                    "confidence": round(evidence.confidence, 2),
                })
                if dry_run:
                    continue
                signal_id = _enrich_signal_id(lane_id=lane_id, evidence=evidence, as_of=as_of)
                wrote = persist_evidence(
                    evidence,
                    program_id=program_id,
                    programs_root=programs_root,
                    backing_signal_ids=(signal_id,),
                )
                if wrote:
                    _emit_enrich_signal(
                        signal_id=signal_id,
                        lane_id=lane_id,
                        program_id=program_id,
                        evidence=evidence,
                        query="per-thread WorkIQ extraction",
                        as_of=as_of,
                        programs_root=programs_root,
                    )
                    from src.core.workiq_freshness import mark_workiq_freshness_success

                    source_id = evidence.source_refs[0].canonical_id if evidence.source_refs else None
                    if source_id:
                        mark_workiq_freshness_success(
                            _workiq_thread_freshness_path(edition=edition, programs_root=programs_root),
                            source_id,
                        )
            typer.echo(f" sources={len(per_thread_evidence)}")
            continue

        # P4-17: check if a batch response is available for this lane's cluster key.
        # If so, reuse it without issuing another WorkIQ call. Otherwise fall through
        # to the per-lane path (single-lane mode, or lanes not in any cluster).
        cluster_key = _lane_cluster_key(entry)
        if cluster_key in cluster_cache:
            cached = cluster_cache[cluster_key]
            if cached is None:
                # Cluster query failed; mark this lane as zero-yield and continue.
                _record_workiq_lane_result(
                    lane_id=lane_id,
                    edition=edition,
                    program_id=program_id,
                    programs_root=programs_root,
                    observed_yield=0,
                    as_of=as_of,
                    last_result="cluster_query_failed",
                )
                typer.echo(" [cluster query failed]")
                continue
            raw_response = cached
        else:
            # No cluster cache entry — issue a per-lane WorkIQ call.
            query = _build_lane_query(entry, since_dt=since_dt)
            if not query:
                typer.echo(" [skipped — no email/transcript query configured]")
                continue
            estimated_query_cost_usd = _estimate_workiq_query_cost_usd(query=query)
            if workiq_cost_guard is not None:
                from src.ai.client import BudgetExceeded

                try:
                    workiq_cost_guard.check(estimated_query_cost_usd)
                except BudgetExceeded as exc:
                    typer.echo(f" [stopped — WorkIQ budget exceeded: {exc}]")
                    break

            try:
                raw_response = bridge.ask_workiq(query)  # type: ignore[assignment]
            except Exception as exc:
                if workiq_cost_guard is not None:
                    workiq_cost_guard.record_actual(estimated_query_cost_usd)
                _record_workiq_lane_result(
                    lane_id=lane_id,
                    edition=edition,
                    program_id=program_id,
                    programs_root=programs_root,
                    observed_yield=0,
                    as_of=as_of,
                    last_result=f"query_error:{type(exc).__name__}",
                )
                typer.echo(f" [ERROR: {exc}]")
                continue

            if workiq_cost_guard is not None:
                workiq_cost_guard.record_actual(
                    _estimate_workiq_query_cost_usd(query=query, response_text=raw_response),
                )

        if raw_response is None:
            _record_workiq_lane_result(
                lane_id=lane_id,
                edition=edition,
                program_id=program_id,
                programs_root=programs_root,
                observed_yield=0,
                as_of=as_of,
                last_result="query_error:no_response",
            )
            typer.echo(" [ERROR: no WorkIQ response]")
            continue

        from src.core.models import Enrichment
        enrichment = Enrichment(
            source="mail",        # "workiq_email" is not a valid Literal; "mail" is correct
            source_id=f"enrich:{lane_id}:{as_of.date().isoformat()}",  # required field
            author="workiq",      # required field; NL response has no specific author
            timestamp=as_of,
            excerpt=f"enrich:{lane_id}",  # per-lane id; query not available in batch path
            permalink=None,
            body_text=raw_response if isinstance(raw_response, str) else str(raw_response),
        )

        ctx_extract = ExtractionContext(
            lane_id=lane_id,
            lane_name=lane_name,
            lane_why="",      # not available in enrich context; extraction uses body_text
            lane_what="",
            enrichments=(enrichment,),   # enrichments go inside ctx, NOT as second arg
        )
        try:
            extracted_evidence = agent.extract(ctx_extract)   # single arg; enrichments are in ctx
        except Exception as exc:
            _record_workiq_lane_result(
                lane_id=lane_id,
                edition=edition,
                program_id=program_id,
                programs_root=programs_root,
                observed_yield=0,
                as_of=as_of,
                last_result=f"extraction_error:{type(exc).__name__}",
            )
            typer.echo(f" [extraction ERROR: {exc}]")
            continue

        if extracted_evidence is None:
            _record_workiq_lane_result(
                lane_id=lane_id,
                edition=edition,
                program_id=program_id,
                programs_root=programs_root,
                observed_yield=0,
                as_of=as_of,
                last_result="no_structured_evidence",
            )
            typer.echo(" [no structured evidence found]")
            continue

        evidence = extracted_evidence
        _record_workiq_lane_result(
            lane_id=lane_id,
            edition=edition,
            program_id=program_id,
            programs_root=programs_root,
            observed_yield=1,
            as_of=as_of,
            last_result="structured_evidence",
        )

        results.append({
            "lane_id": lane_id,
            "risk_level": evidence.risk_level.value,
            "etas_found": len(evidence.etas),
            "blocking_found": len(evidence.blocking_items),
            "confidence": round(evidence.confidence, 2),
        })

        if not dry_run:
            # P4-0 (§17.8 Option A): create a PENDING journal Signal that gates this
            # evidence for blurb synthesis. The signal id is content+day-derived so an
            # identical re-extraction resolves to the same id. persist_evidence dedups
            # the evidence (P4-3); we only emit the signal when a NEW evidence record was
            # actually written, so re-runs do not spam the review queue.
            signal_id = _enrich_signal_id(lane_id=lane_id, evidence=evidence, as_of=as_of)
            wrote = persist_evidence(
                evidence,
                program_id=program_id,
                programs_root=programs_root,
                backing_signal_ids=(signal_id,),
            )
            if wrote:
                _emit_enrich_signal(
                    signal_id=signal_id,
                    lane_id=lane_id,
                    program_id=program_id,
                    evidence=evidence,
                    query=query,  # type: ignore[arg-type]
                    as_of=as_of,
                    programs_root=programs_root,
                )
                # P4-2: provenance + quality records on the enrich path (were gather-only).
                _record_enrich_provenance_and_quality(
                    lane_id=lane_id,
                    program_id=program_id,
                    evidence=evidence,
                    raw_response=raw_response,
                    as_of=as_of,
                    programs_root=programs_root,
                )
            if accept:
                _update_workiq_latest(
                    lane_id=lane_id,
                    program_id=program_id,
                    programs_root=programs_root,
                    evidence=evidence,
                    as_of=as_of,
                )

        typer.echo(f" risk={evidence.risk_level.value} conf={evidence.confidence:.2f}")

    # Summary output
    if output_format == "json":
        typer.echo(json.dumps(results, indent=2))
    else:
        typer.echo(f"\nDone. {len(results)} lanes extracted.")
        if dry_run:
            typer.echo("(dry-run: no files written)")


def _parse_since(since: str) -> int:
    """Parse '7d' → 7, '14d' → 14. Defaults to 7."""
    since = since.strip().lower()
    if since.endswith("d"):
        try:
            return int(since[:-1])
        except ValueError:
            pass
    return 7


def _extract_per_thread_evidence(
    *,
    bridge: Any,
    entry: dict[str, Any],
    since_dt: datetime,
    as_of: datetime,
    retrieval_config: Any,
    edition: str,
    programs_root: Path,
) -> tuple["WorkstreamEvidence", ...]:
    """Run source-keyed FQ-02 retrieval without mixing approval across threads."""

    from dataclasses import replace
    from src.ai.content_extractor import ContentExtractionAgent, ExtractionContext
    from src.core.evidence_models import ExtractionMethod, VerificationState
    from src.m365.workiq_ask_support import DiscoveryRequest
    from src.m365.workiq_retriever import retrieve_workiq_threads

    lane_id = str(entry.get("id") or "").strip()
    lane_name = str(entry.get("name") or lane_id).strip()
    terms = _per_thread_lane_terms(entry)
    if not lane_id or not terms:
        return ()
    request = DiscoveryRequest(
        lane_name=lane_name,
        terms=terms,
        window_start=since_dt.date(),
        window_end=as_of.date(),
        limit=max(8, retrieval_config.per_thread_top_k),
    )
    cache_path = _workiq_thread_freshness_path(edition=edition, programs_root=programs_root)
    retrieval = retrieve_workiq_threads(
        bridge=bridge,
        request=request,
        top_k=retrieval_config.per_thread_top_k,
        max_calls=retrieval_config.max_calls_per_cycle,
        max_wall_clock_seconds=retrieval_config.max_wall_clock_seconds,
        cache_path=cache_path,
        one_hop=retrieval_config.per_thread_one_hop,
    )
    evidences: list[WorkstreamEvidence] = []
    for enrichment in retrieval.enrichments:
        # The retriever already requested structured extraction. Reuse the parser
        # without issuing a second model call.
        def _return_retrieved_body(_prompt: str, body: str | None = enrichment.body_text) -> str | None:
            return body

        parser = ContentExtractionAgent(ask_ai_fn=_return_retrieved_body)
        parsed_evidence = parser.extract(
            ExtractionContext(
                lane_id=lane_id,
                lane_name=lane_name,
                lane_why="",
                lane_what="",
                enrichments=(enrichment,),
            )
        )
        if parsed_evidence is None:
            continue
        method: ExtractionMethod = "one_hop" if retrieval_config.per_thread_one_hop else "two_hop"
        evidence = replace(
            parsed_evidence,
            source_refs=tuple(replace(ref, extraction_method=method) for ref in parsed_evidence.source_refs),
            verification_state=VerificationState.MODEL_SELF_ATTESTED,
        )
        evidences.append(evidence)
    return tuple(evidences)


def _per_thread_lane_terms(entry: dict[str, Any]) -> tuple[str, ...]:
    sources = entry.get("signal_sources") if isinstance(entry.get("signal_sources"), dict) else {}
    terms = [str(entry.get("name") or "").strip()]
    for key in ("workiq_keywords", "workiq_exclude_keywords"):
        values = sources.get(key) if isinstance(sources, dict) else None
        if isinstance(values, list):
            terms.extend(str(value).strip() for value in values if str(value).strip())
    return tuple(dict.fromkeys(term for term in terms if term))


def _workiq_thread_freshness_path(*, edition: str, programs_root: Path) -> Path:
    return get_program_output_dir(edition, programs_root=programs_root) / ".workiq_thread_freshness.json"


def _load_registry_lanes(program_id: str, programs_root: Path) -> list[dict]:
    import yaml
    registry_path = programs_root / program_id / "workstream_registry.yaml"
    if not registry_path.exists():
        return []
    with open(registry_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("workstreams", [])


def _build_workiq_breaker(*, edition: str, programs_root: Path) -> "CircuitBreaker":
    from src.core.circuit_breaker import CircuitBreaker

    return CircuitBreaker(state_path=_workiq_breaker_path(edition=edition, programs_root=programs_root))


def _build_workiq_cost_guard(
    *,
    edition: str,
    program: Any,
    started_at: datetime,
    programs_root: Path,
) -> "CostGuard | None":
    ai_config = getattr(program, "ai", None)
    if ai_config is None or not bool(getattr(ai_config, "enabled", False)):
        return None
    budget_usd = float(getattr(ai_config, "budget_usd_per_run", 0.0) or 0.0)
    if budget_usd <= 0:
        return None

    from src.ai.cost_guard import CostGuard

    return CostGuard(
        edition=edition,
        run_id=_build_workiq_enrich_run_id(edition=edition, started_at=started_at),
        budget_usd=budget_usd,
        programs_root=programs_root,
    )


def _build_workiq_enrich_run_id(*, edition: str, started_at: datetime) -> str:
    timestamp = started_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{edition}:enrich:workiq:{timestamp}:{uuid4().hex[:8]}"


def _estimate_workiq_query_cost_usd(*, query: str, response_text: object | None = None) -> float:
    """Best-effort WorkIQ spend estimate when the bridge exposes no token telemetry.

    We deliberately record this as an estimate, not exact billing. The pre-call check
    uses the query-only floor to stop obvious runaway spend; after the response arrives
    we persist a fuller estimate that scales with both prompt and response size.
    """
    prompt_chars = len(query)
    response_chars = 0
    if response_text is not None:
        response_chars = len(response_text) if isinstance(response_text, str) else len(str(response_text))
    estimated = (
        _WORKIQ_ESTIMATED_CALL_FLOOR_USD
        + (prompt_chars / 1000.0) * _WORKIQ_ESTIMATED_INPUT_COST_PER_1K_CHARS
        + (response_chars / 1000.0) * _WORKIQ_ESTIMATED_OUTPUT_COST_PER_1K_CHARS
    )
    return round(max(estimated, 0.0), 6)


def _workiq_breaker_path(*, edition: str, programs_root: Path) -> Path:
    return get_program_output_dir(edition, programs_root=programs_root) / ".workiq_breaker.json"


def _workiq_lane_state_path(*, edition: str, programs_root: Path) -> Path:
    return get_program_output_dir(edition, programs_root=programs_root) / "workiq_enrich_state.json"


def _is_workiq_lane_suspended(
    *,
    lane_entry: dict[str, Any],
    lane_id: str,
    edition: str,
    programs_root: Path,
) -> bool:
    override = lane_entry.get("workiq_query_suspended")
    if isinstance(override, bool):
        return override
    lane_state = _load_workiq_lane_state(edition=edition, programs_root=programs_root).get(lane_id, {})
    return bool(lane_state.get("workiq_query_suspended"))


def _load_workiq_lane_state(*, edition: str, programs_root: Path) -> dict[str, dict[str, Any]]:
    path = _workiq_lane_state_path(edition=edition, programs_root=programs_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}
    lanes = payload.get("lanes")
    if not isinstance(lanes, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for lane_id, raw_state in lanes.items():
        if not isinstance(lane_id, str) or not lane_id.strip() or not isinstance(raw_state, dict):
            continue
        normalized[lane_id.strip()] = dict(raw_state)
    return normalized


def _record_workiq_lane_result(
    *,
    lane_id: str,
    edition: str,
    program_id: str,
    programs_root: Path,
    observed_yield: int,
    as_of: datetime,
    last_result: str,
) -> Path:
    path = _workiq_lane_state_path(edition=edition, programs_root=programs_root)
    existing = _load_workiq_lane_state(edition=edition, programs_root=programs_root)
    previous = existing.get(lane_id, {})
    next_state = _next_workiq_lane_state(previous, observed_yield=observed_yield, as_of=as_of, last_result=last_result)
    existing[lane_id] = next_state
    payload = {
        "schema_version": "1.0",
        "program_id": program_id,
        "edition": edition,
        "updated_at": as_of.isoformat(),
        "lanes": existing,
    }
    _write_json_atomic(path, payload)
    return path


def _next_workiq_lane_state(
    previous: dict[str, Any],
    *,
    observed_yield: int,
    as_of: datetime,
    last_result: str,
) -> dict[str, Any]:
    yield_last_3 = _roll_signal_yield_window(previous.get("yield_last_3"), observed=observed_yield)
    zero_yield_streak = _consecutive_zero_yield_streak(yield_last_3)
    return {
        "yield_last_3": list(yield_last_3),
        "zero_yield_streak": zero_yield_streak,
        "workiq_query_suspended": zero_yield_streak >= _WORKIQ_ZERO_YIELD_SUSPEND_THRESHOLD,
        "last_attempted_at": as_of.isoformat(),
        "last_result": last_result,
        "last_observed_yield": int(observed_yield),
    }


def _roll_signal_yield_window(previous: Any, *, observed: int) -> tuple[int, int, int]:
    prior_values: list[int] = []
    if isinstance(previous, (list, tuple)):
        for item in previous[:3]:
            try:
                prior_values.append(max(0, int(item)))
            except (TypeError, ValueError):
                prior_values.append(0)
    while len(prior_values) < 3:
        prior_values.append(0)
    return (max(0, int(observed)), prior_values[0], prior_values[1])


def _consecutive_zero_yield_streak(yield_last_3: tuple[int, int, int]) -> int:
    streak = 0
    for value in yield_last_3:
        if value == 0:
            streak += 1
            continue
        break
    return streak


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temp_path.replace(path)


def _build_lane_query(entry: dict, *, since_dt: datetime) -> str | None:
    """Build a WorkIQ search query for a registry lane based on signal_sources."""
    sources = entry.get("signal_sources", {})
    meeting_series = sources.get("teams_meeting_series", [])
    teams_chats = sources.get("teams_chats", [])
    lane_name = entry.get("name", entry.get("id", ""))
    area_paths = entry.get("area_paths", [])

    since_str = since_dt.strftime("%Y-%m-%d")
    parts = []
    if meeting_series:
        series_names = [s["display_name"] for s in meeting_series if s.get("display_name")]
        parts.append(f"meeting series: {', '.join(series_names)}")
    if teams_chats:
        chat_names = [c["display_name"] for c in teams_chats if c.get("display_name")]
        parts.append(f"Teams chats: {', '.join(chat_names)}")
    if area_paths:
        parts.append(f"ADO area paths: {', '.join(area_paths)}")

    if not parts:
        return None

    # Use a proper JSON skeleton to guide the LLM response format.
    json_schema = (
        '{"risk_level": "<blocked|high|medium|low|done|unknown>", '
        '"etas": [{"label": "<item>", "eta_date": "<YYYY-MM-DD>", "owner": "<alias or null>"}], '
        '"blocking_items": ["<ADO:NNNNN or IcM:NNNNN>"], '
        '"owners": ["<alias>"], '
        '"narrative_summary": "<1-2 sentence summary>", '
        '"confidence": <0.0-1.0>}'
    )
    return (
        f"Read emails and meeting transcripts from {since_str} to today about '{lane_name}'. "
        f"Sources: {'; '.join(parts)}. "
        f"Extract: risk level (blocked/high/medium/low/done/unknown), blocking items "
        f"(format: ADO:NNNNN or IcM:NNNNN), ETA dates with owners, and any go/no-go decisions. "
        f"Return ONLY valid JSON matching this schema: {json_schema}"
    )


def _update_workiq_latest(
    *,
    lane_id: str,
    program_id: str,
    programs_root: Path,
    evidence: "WorkstreamEvidence",
    as_of: datetime,
) -> None:
    """Update workiq_latest in workstream_registry.yaml with AI-generated summary."""
    import yaml
    registry_path = programs_root / program_id / "workstream_registry.yaml"
    if not registry_path.exists():
        return
    with open(registry_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    date_prefix = as_of.strftime("%Y-%m-%d")
    summary = evidence.narrative_summary or f"Risk: {evidence.risk_level.value}."
    new_value = f"{date_prefix}: {summary} Source: vertex enrich {date_prefix}."

    for ws in data.get("workstreams", []):
        if ws.get("id") == lane_id:
            ws["workiq_latest"] = new_value
            ws["last_reviewed_date"] = date_prefix
            break

    with open(registry_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _enrich_signal_id(*, lane_id: str, evidence: "WorkstreamEvidence", as_of: datetime) -> str:
    """P4-0: stable, content+day-derived id for the gating PENDING signal.

    Two extractions with identical lane/date/narrative/blocking/confidence resolve to
    the same id, so a re-run that dedups the evidence (P4-3) also resolves to the same
    backing-signal id (and we skip emitting a second signal in that case).
    """
    import hashlib

    canonical = "|".join((
        lane_id,
        as_of.strftime("%Y-%m-%d"),
        ",".join(ref.canonical_id or "" for ref in evidence.source_refs),
        evidence.narrative_summary or "",
        ",".join(evidence.blocking_items),
        str(round(evidence.confidence, 4)),
    ))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"workiq/enrich/{lane_id}/{as_of.strftime('%Y-%m-%d')}/{digest}"


def _emit_enrich_signal(
    *,
    signal_id: str,
    lane_id: str,
    program_id: str,
    evidence: "WorkstreamEvidence",
    query: str,
    as_of: datetime,
    programs_root: Path,
) -> None:
    """P4-0 (§17.8 Option A): append a PENDING journal Signal gating this evidence.

    ``source="workiq/email"`` is intentionally NOT auto-approved by
    ``signal_can_be_auto_approved`` → review_policy defaults to PENDING, so a human
    must approve (or FR-SG-38 auto-approve on sufficient approval mass) before the
    evidence is admitted to blurb synthesis via ``load_approved_evidence_by_lane``.
    This closes the §17.8 evidence-approval bypass: enrich no longer persists
    ungated evidence.
    """
    from src.core.journal import append_signal
    from src.core.models import Confidence
    from src.core.models_v2 import Signal

    text = (evidence.narrative_summary or query or "")[:500]
    sig = Signal(
        id=signal_id,
        timestamp=as_of,
        source="workiq/email",
        program_id=program_id,
        workstream_id=lane_id,
        entity_refs=tuple(evidence.blocking_items),
        text=text,
        raw_ref=f"enrich:{lane_id}:{as_of.date().isoformat()}",
        confidence=Confidence.MEDIUM,
        metadata={
            "source_type": "enrich",
            "lane_id": lane_id,
            "evidence_confidence": evidence.confidence,
            "risk_level": evidence.risk_level.value,
            "etas_found": len(evidence.etas),
        },
        review_policy=None,   # default PENDING per signal_review.default_review_policy
    )
    append_signal(sig, programs_root=programs_root, partition_at=as_of)


def _record_enrich_provenance_and_quality(
    *,
    lane_id: str,
    program_id: str,
    evidence: "WorkstreamEvidence",
    raw_response: object,
    as_of: datetime,
    programs_root: Path,
) -> None:
    """P4-2: write provenance + quality records on the ME-03 enrich path (were gather-only).

    Parity with the ME-02 gather path's ME-05 quality recording so doctor/dashboards
    can audit every evidence-bearing run regardless of which path produced it.
    """
    from src.core.evidence_provenance import make_provenance_record, record_provenance
    from src.core.evidence_quality import EvidenceQualityRecord, record_evidence_quality

    fields_populated = tuple(
        name for name, present in {
            "risk_level": bool(evidence.risk_level),
            "etas": bool(evidence.etas),
            "blocking_items": bool(evidence.blocking_items),
            "owners": bool(evidence.owners),
            "narrative_summary": bool(evidence.narrative_summary),
            "source_refs": bool(evidence.source_refs),
        }.items()
        if present
    )
    prov = make_provenance_record(
        lane_id=lane_id,
        source_type="workiq_email",
        source_id=f"enrich:{lane_id}:{as_of.date().isoformat()}",
        source_date=as_of.date().isoformat(),
        confidence=evidence.confidence,
        fields_populated=fields_populated,
        operator="auto",
        run_at=as_of,
    )
    try:
        record_provenance(prov, program_id=program_id, programs_root=programs_root)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to record enrich provenance for lane %s: %s", lane_id, exc)

    body_chars = len(raw_response) if isinstance(raw_response, str) else len(str(raw_response))
    qrec = EvidenceQualityRecord(
        run_at=as_of,
        lane_id=lane_id,
        confidence=evidence.confidence,
        etas_found=len(evidence.etas),
        owners_found=len(evidence.owners),
        blocking_found=len(evidence.blocking_items),
        body_text_chars=body_chars,
        source_type="workiq_email",
        extractor="ContentExtractionAgent",
    )
    try:
        record_evidence_quality(qrec, program_id=program_id, programs_root=programs_root)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to record enrich quality for lane %s: %s", lane_id, exc)


# ── P4-17: Lane-batching helpers ──────────────────────────────────────────────
#
# Instead of one WorkIQ NL call per lane (15 calls for 15 lanes), cluster lanes
# that share the same Teams meeting series into a single broader query. One
# WorkIQ call per cluster; ContentExtractionAgent still runs per-lane so each
# lane gets its own structured evidence. Reduces cost/latency by K/N where K is
# the number of unique meeting-series clusters and N is the total lane count.
#
# Cluster key: sorted frozenset of `teams_meeting_series.display_name` values
# from the lane's `signal_sources`. Lanes with no meeting series get a unique
# key (their lane id) and are not batched.


def _lane_cluster_key(entry: dict) -> str:
    """Return a stable cluster key for a lane entry (§16.4 P4-17).

    Lanes that share the same ``teams_meeting_series`` display names are placed
    into the same cluster so they share a single WorkIQ NL call.
    """
    sources = entry.get("signal_sources") or {}
    series = sources.get("teams_meeting_series") or []
    names = frozenset(
        s.get("display_name", "").strip()
        for s in series
        if s.get("display_name", "").strip()
    )
    if not names:
        # No meeting series → unique key so this lane is never batched.
        return f"__solo__{entry.get('id', '')}"
    return ":".join(sorted(names))


def _build_batched_cluster_query(
    cluster_entries: list[dict],
    *,
    since_dt: datetime,
) -> str | None:
    """Build one combined NL query for all lanes in a cluster (§16.4 P4-17).

    The query asks WorkIQ about a shared set of meeting series / Teams chats that
    all lanes in the cluster attend, covering all area paths together. The response
    is a combined narrative about all the cluster's lanes; ContentExtractionAgent
    then runs per-lane on this combined text.
    """
    since_str = since_dt.strftime("%Y-%m-%d")

    # Union of meeting series across all cluster lanes (they all share the same names).
    all_series_names: list[str] = []
    all_chat_names: list[str] = []
    all_lane_names: list[str] = []
    all_area_paths: list[str] = []

    seen_series: set[str] = set()
    seen_chats: set[str] = set()
    seen_area_paths: set[str] = set()

    for entry in cluster_entries:
        lane_name = entry.get("name", entry.get("id", ""))
        if lane_name:
            all_lane_names.append(lane_name)
        sources = entry.get("signal_sources") or {}
        for series in sources.get("teams_meeting_series") or []:
            name = (series.get("display_name") or "").strip()
            if name and name not in seen_series:
                all_series_names.append(name)
                seen_series.add(name)
        for chat in sources.get("teams_chats") or []:
            name = (chat.get("display_name") or "").strip()
            if name and name not in seen_chats:
                all_chat_names.append(name)
                seen_chats.add(name)
        for path in entry.get("area_paths") or []:
            p = (path or "").strip()
            if p and p not in seen_area_paths:
                all_area_paths.append(p)
                seen_area_paths.add(p)

    parts: list[str] = []
    if all_series_names:
        parts.append(f"meeting series: {', '.join(all_series_names)}")
    if all_chat_names:
        parts.append(f"Teams chats: {', '.join(all_chat_names)}")
    if all_area_paths:
        parts.append(f"ADO area paths: {', '.join(all_area_paths)}")

    if not parts:
        return None

    lane_list = "; ".join(all_lane_names[:10])  # cap display length
    return (
        f"Read emails and meeting transcripts from {since_str} to today "
        f"about the following workstreams: {lane_list}. "
        f"Sources: {'; '.join(parts)}. "
        f"For each workstream, summarize risk level, blocking items (format: ADO:NNNNN or IcM:NNNNN), "
        f"ETA dates with owners, and any go/no-go decisions made. "
        f"Cover all workstreams in your response — do not skip any."
    )


def _build_cluster_response_cache(
    *,
    registry_entries: list[dict],
    since_dt: datetime,
    edition: str,
    programs_root: Path,
    bridge: Any,
    workiq_cost_guard: Any,
) -> dict[str, str | None]:
    """Pre-fetch WorkIQ responses for clusters of lanes (§16.4 P4-17).

    Returns a mapping from cluster_key → raw WorkIQ response (or None on failure).
    Only clusters with ≥2 lanes are batched; solo lanes are excluded (they will
    fall through to the per-lane path in the main loop).
    """
    from collections import defaultdict

    by_cluster: dict[str, list[dict]] = defaultdict(list)
    for entry in registry_entries:
        key = _lane_cluster_key(entry)
        by_cluster[key].append(entry)

    cache: dict[str, str | None] = {}
    for cluster_key, cluster_entries in by_cluster.items():
        # Only batch clusters with ≥2 lanes sharing a meeting series.
        if len(cluster_entries) < 2 or cluster_key.startswith("__solo__"):
            continue  # Excluded → fall through to per-lane path in main loop.

        query = _build_batched_cluster_query(cluster_entries, since_dt=since_dt)
        if not query:
            continue

        # Budget check before the batched call.
        estimated_cost = _estimate_workiq_query_cost_usd(query=query)
        if workiq_cost_guard is not None:
            try:
                from src.ai.client import BudgetExceeded
                workiq_cost_guard.check(estimated_cost)
            except (BudgetExceeded, Exception):
                for entry in cluster_entries:
                    cache[cluster_key] = None
                break

        try:
            raw_response = bridge.ask_workiq(query)
            if workiq_cost_guard is not None:
                workiq_cost_guard.record_actual(
                    _estimate_workiq_query_cost_usd(query=query, response_text=raw_response)
                )
        except Exception as exc:
            log.warning("Cluster WorkIQ call failed for key %r: %s", cluster_key, exc)
            raw_response = None
            if workiq_cost_guard is not None:
                workiq_cost_guard.record_actual(estimated_cost)

        cache[cluster_key] = raw_response

    return cache
