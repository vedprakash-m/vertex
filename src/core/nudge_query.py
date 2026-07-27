"""Nudge WIQL query building and candidate retrieval.

Public API:
    escape_wiql_literal(value, *, field_name) -> str
    build_nudge_wiql(*, program, section) -> str
    fetch_section_candidates(*, program, section, authored_registry, workstreams, client, as_of) -> NudgeSectionFetchResult
    NudgeADOClient (Protocol)
"""
from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Any, Protocol

from src.core.exceptions import ConfigError, QueryError, QueryTimeoutError
from src.core.nudge_models import (
    NUDGE_BATCH_SIZE,
    NudgeCandidate,
    NudgeSectionFetchResult,
    NudgeSectionSpec,
)
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES_ADO


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class NudgeADOClient(Protocol):
    def execute_wiql(self, wiql: str) -> list[int]: ...
    def query_work_items_batch(self, ids: list[int], fields: tuple[str, ...]) -> list[dict[str, object]]: ...
    def list_work_item_comments(self, work_item_id: int) -> list[dict[str, object]]: ...


# ---------------------------------------------------------------------------
# ADO fields fetched for each candidate
# ---------------------------------------------------------------------------

NUDGE_BATCH_FIELDS: tuple[str, ...] = (
    "System.Id",
    "System.WorkItemType",
    "System.Title",
    "System.State",
    "System.AssignedTo",
    "System.AreaPath",
    "System.ChangedDate",
    "System.Description",
    "Microsoft.VSTS.Scheduling.TargetDate",
    "System.Tags",
    "Custom.RiskAssessment",
    "Custom.RiskAssessmentComment",
    "Custom.CommitmentStatus",
)


# ---------------------------------------------------------------------------
# WIQL literal escaping
# ---------------------------------------------------------------------------


def escape_wiql_literal(value: str, *, field_name: str = "") -> str:
    """Escape a string for inclusion in a WIQL literal.

    Doubles apostrophes and rejects control characters. Raises ConfigError on
    invalid input so the caller does not inject unsafe SQL-like strings.
    """
    stripped = value.strip()
    if not stripped:
        raise ConfigError(
            f"WIQL literal for field {field_name!r} must not be empty after stripping whitespace"
        )
    for ch in stripped:
        cp = ord(ch)
        if (0x0000 <= cp <= 0x001F) or cp == 0x007F:
            raise ConfigError(
                f"WIQL literal for field {field_name!r} contains control character U+{cp:04X}"
            )
    return stripped.replace("'", "''")


# ---------------------------------------------------------------------------
# WIQL builder
# ---------------------------------------------------------------------------


def build_nudge_wiql(*, program: Any, section: NudgeSectionSpec) -> str:
    """Build a WIQL query for a tag or area_path section.

    Registry sections do not use WIQL; calling this for source=registry raises ConfigError.
    """
    crit = section.criteria
    if crit.source == "registry":
        raise ConfigError(
            f"Section {section.id!r}: registry sections do not execute WIQL; "
            "use batch hydration of key_ado_items instead."
        )

    ado = getattr(program, "ado", None)
    if ado is None:
        raise ConfigError(f"program.ado is required for WIQL sections (section {section.id!r})")

    parts: list[str] = [
        "SELECT [System.Id] FROM WorkItems",
        "WHERE [System.TeamProject] = @project",
    ]

    # Work-item types predicate — section-level override takes precedence over
    # the shared program.ado default (see NudgeSectionCriteria.work_item_types).
    wit_raw = crit.work_item_types or (getattr(ado, "work_item_types", None) or ())
    wit_types: list[str] = [str(w).strip() for w in wit_raw if str(w).strip()]
    if wit_types:
        escaped = [f"'{escape_wiql_literal(w, field_name='work_item_type')}'" for w in wit_types]
        if len(escaped) == 1:
            parts.append(f"AND [System.WorkItemType] = {escaped[0]}")
        else:
            parts.append(f"AND [System.WorkItemType] IN ({', '.join(escaped)})")
    else:
        parts.append("AND [System.WorkItemType] <> 'Task'")

    # Excluded states predicate — always include the full terminal-state set as a minimum
    # so the WIQL filter is never more permissive than the Python post-filter.
    # Program-specific excluded_states extend (not replace) the terminal baseline.
    excl_raw = getattr(ado, "excluded_states", None) or ()
    prog_excl = {str(s).strip() for s in excl_raw if str(s).strip()}
    excl_states: list[str] = sorted(prog_excl | set(TERMINAL_WORK_ITEM_STATES_ADO))
    escaped_states = [f"'{escape_wiql_literal(s, field_name='state')}'" for s in excl_states]
    parts.append(f"AND [System.State] NOT IN ({', '.join(escaped_states)})")

    # Area paths predicate
    if crit.legacy_scope_override and crit.area_path_filter:
        area_paths_to_use = list(crit.area_path_filter)
    else:
        prog_areas = getattr(ado, "area_paths", None) or ()
        area_paths_to_use = [str(ap).strip() for ap in (crit.area_path_filter or prog_areas) if str(ap).strip()]

    if area_paths_to_use:
        area_clauses = [
            f"[System.AreaPath] UNDER '{escape_wiql_literal(ap, field_name='area_path')}'"
            for ap in area_paths_to_use
        ]
        if len(area_clauses) == 1:
            parts.append(f"AND ({area_clauses[0]})")
        else:
            parts.append(f"AND ({' OR '.join(area_clauses)})")

    # Tag predicate (single OR clause for all tags)
    if crit.source == "tag" and crit.tags:
        tag_clauses = [
            f"[System.Tags] CONTAINS '{escape_wiql_literal(tag, field_name='tag')}'"
            for tag in crit.tags
        ]
        if len(tag_clauses) == 1:
            parts.append(f"AND ({tag_clauses[0]})")
        else:
            parts.append(f"AND ({' OR '.join(tag_clauses)})")

    parts.append("ORDER BY [System.ChangedDate] ASC, [System.Id] ASC")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Candidate fetcher
# ---------------------------------------------------------------------------


def fetch_section_candidates(
    *,
    program: Any,
    section: NudgeSectionSpec,
    authored_registry: tuple[Any, ...],  # tuple[WorkstreamRegistryEntry, ...]
    workstreams: tuple[Any, ...],
    client: NudgeADOClient,
    as_of: datetime,
) -> NudgeSectionFetchResult:
    """Fetch and hydrate candidates for one section.

    Returns NudgeSectionFetchResult with candidates sorted by item ID.
    query_error=True when the ADO query fails after retry exhaustion.
    """
    crit = section.criteria

    try:
        if crit.source == "registry":
            return _fetch_registry_candidates(program, section, authored_registry, workstreams, client, as_of)
        elif crit.source in ("tag", "area_path"):
            return _fetch_wiql_candidates(program, section, authored_registry, workstreams, client, as_of)
        else:
            raise ConfigError(f"Section {section.id!r}: unknown criteria source {crit.source!r}")
    except (QueryError, QueryTimeoutError) as exc:
        detail = _scrub_error(str(exc))
        return NudgeSectionFetchResult(
            section_id=section.id,
            candidates=(),
            query_error=True,
            error_details=detail,
        )


def _fetch_registry_candidates(
    program: Any,
    section: NudgeSectionSpec,
    authored_registry: tuple[Any, ...],
    workstreams: tuple[Any, ...],
    client: NudgeADOClient,
    as_of: datetime,
) -> NudgeSectionFetchResult:
    from src.core.ado_hydration import _work_item_from_batch_row  # noqa: PLC0415

    crit = section.criteria

    # Collect unique key_ado_items from registry in authored order (first workstream wins)
    item_to_ws: dict[int, str | None] = {}
    for entry in authored_registry:
        if getattr(entry, "lifecycle_state", "active") != "active":
            continue  # e.g. "paused" — kept in registry but suppressed from nudge output
        for item_id in getattr(entry, "key_ado_items", ()):
            if isinstance(item_id, int) and item_id > 0 and item_id not in item_to_ws:
                item_to_ws[item_id] = getattr(entry, "id", None)

    if not item_to_ws:
        return NudgeSectionFetchResult(section_id=section.id, candidates=())

    unique_ids = sorted(item_to_ws.keys())
    rows = _batch_hydrate(unique_ids, client)

    # Determine area paths for post-filter
    if crit.legacy_scope_override and crit.area_path_filter:
        filter_areas: frozenset[str] | None = frozenset(
            a.lower() for a in crit.area_path_filter
        )
    else:
        filter_areas = None  # no area filter for registry

    candidates: list[NudgeCandidate] = []
    closed_counts: dict[str | None, int] = {}
    for row in rows:
        item = _work_item_from_batch_row(row, revision_rows=[], comment_rows=[], fetched_at=as_of)
        if item.id <= 0:
            continue
        # Post-filter area for legacy scope
        if filter_areas is not None:
            item_area = (item.area_path or "").lower()
            if not any(item_area.startswith(fa) or item_area == fa for fa in filter_areas):
                continue
        # required_tags filter (registry ∩ tag) — applies uniformly to open and
        # closed items, so the closed tally below reflects the same scope.
        if crit.required_tags:
            item_tags_lower = frozenset(t.strip().lower() for t in item.tags)
            required_lower = frozenset(t.strip().lower() for t in crit.required_tags)
            if not required_lower.issubset(item_tags_lower):
                # Check if ANY required tag matches (OR semantics for multiple required_tags)
                if not (required_lower & item_tags_lower):
                    continue

        ws_id = item_to_ws.get(item.id)
        state_lower = item.state.strip().lower()
        if state_lower in _terminal_states_lower():
            closed_counts[ws_id] = closed_counts.get(ws_id, 0) + 1
            continue
        candidates.append(NudgeCandidate(item=item, workstream_id=ws_id))

    # Sort by item ID
    candidates.sort(key=lambda c: getattr(c.item, "id", 0))
    return NudgeSectionFetchResult(
        section_id=section.id,
        candidates=tuple(candidates),
        closed_counts_by_workstream=tuple(closed_counts.items()),
    )


def _fetch_wiql_candidates(
    program: Any,
    section: NudgeSectionSpec,
    authored_registry: tuple[Any, ...],
    workstreams: tuple[Any, ...],
    client: NudgeADOClient,
    as_of: datetime,
) -> NudgeSectionFetchResult:
    from src.core.ado_hydration import _work_item_from_batch_row  # noqa: PLC0415
    from src.core.workstream_path_resolver import resolve_workstream_id_loose_longest as _resolve_ws  # noqa: PLC0415

    wiql = build_nudge_wiql(program=program, section=section)
    ids = client.execute_wiql(wiql)
    if not ids:
        return NudgeSectionFetchResult(section_id=section.id, candidates=())

    rows = _batch_hydrate(ids, client)

    # Build registry map for workstream resolution
    item_to_ws: dict[int, str] = {}
    for entry in authored_registry:
        for item_id in getattr(entry, "key_ado_items", ()):
            if isinstance(item_id, int) and item_id > 0 and item_id not in item_to_ws:
                item_to_ws[item_id] = getattr(entry, "id", "")

    crit = section.criteria
    if crit.legacy_scope_override and crit.area_path_filter:
        filter_areas: frozenset[str] | None = frozenset(
            a.lower() for a in crit.area_path_filter
        )
    else:
        if crit.area_path_filter:
            filter_areas = frozenset(a.lower() for a in crit.area_path_filter)
        else:
            filter_areas = None  # rely on WIQL scope

    seen: set[int] = set()
    candidates: list[NudgeCandidate] = []
    for row in rows:
        item = _work_item_from_batch_row(row, revision_rows=[], comment_rows=[], fetched_at=as_of)
        if item.id <= 0 or item.id in seen:
            continue
        seen.add(item.id)
        state_lower = item.state.strip().lower()
        if state_lower in _terminal_states_lower():
            continue
        # Defensive post-filter for area
        if filter_areas is not None:
            item_area = (item.area_path or "").lower()
            if not any(item_area.startswith(fa) or item_area == fa for fa in filter_areas):
                continue

        ws_id = item_to_ws.get(item.id)
        if ws_id is None:
            ws_id = _resolve_ws(item.area_path, workstreams)
        candidates.append(NudgeCandidate(item=item, workstream_id=ws_id or None))

    # Supplemental registry hydration: merge in key_ado_items for workstreams
    # whose items don't reliably carry this section's tag(s) (e.g. armada_core_runtime).
    if crit.supplemental_workstream_ids:
        supp_ids = frozenset(crit.supplemental_workstream_ids)
        for cand in _fetch_supplemental_registry_candidates(supp_ids, authored_registry, client, as_of):
            cand_item_id = getattr(cand.item, "id", 0)
            if cand_item_id not in seen:
                seen.add(cand_item_id)
                candidates.append(cand)

    candidates.sort(key=lambda c: getattr(c.item, "id", 0))
    return NudgeSectionFetchResult(section_id=section.id, candidates=tuple(candidates))


def _batch_hydrate(ids: list[int], client: NudgeADOClient) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for start in range(0, len(ids), NUDGE_BATCH_SIZE):
        batch = ids[start : start + NUDGE_BATCH_SIZE]
        rows.extend(client.query_work_items_batch(batch, NUDGE_BATCH_FIELDS))
    return rows


def _fetch_supplemental_registry_candidates(
    workstream_ids: frozenset[str],
    authored_registry: tuple[Any, ...],
    client: NudgeADOClient,
    as_of: datetime,
) -> list[NudgeCandidate]:
    """Hydrate key_ado_items for specific workstream(s) to merge into a tag/area_path
    section's live results (see NudgeSectionCriteria.supplemental_workstream_ids).

    Used for workstreams whose ADO items don't reliably carry the section's tag(s),
    so a pure live-query fetch would otherwise silently drop them.
    """
    from src.core.ado_hydration import _work_item_from_batch_row  # noqa: PLC0415

    item_to_ws: dict[int, str] = {}
    for entry in authored_registry:
        if getattr(entry, "lifecycle_state", "active") != "active":
            continue
        entry_id = getattr(entry, "id", None)
        if entry_id not in workstream_ids:
            continue
        for item_id in getattr(entry, "key_ado_items", ()):
            if isinstance(item_id, int) and item_id > 0 and item_id not in item_to_ws:
                item_to_ws[item_id] = entry_id

    if not item_to_ws:
        return []

    rows = _batch_hydrate(sorted(item_to_ws.keys()), client)
    out: list[NudgeCandidate] = []
    for row in rows:
        item = _work_item_from_batch_row(row, revision_rows=[], comment_rows=[], fetched_at=as_of)
        if item.id <= 0:
            continue
        if item.state.strip().lower() in _terminal_states_lower():
            continue
        out.append(NudgeCandidate(item=item, workstream_id=item_to_ws.get(item.id)))
    return out


def _terminal_states_lower() -> frozenset[str]:
    from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES  # noqa: PLC0415
    return frozenset(s.lower() for s in TERMINAL_WORK_ITEM_STATES)


def _scrub_error(text: str) -> str:
    scrubbed = text.replace("\n", " ").replace("\r", " ")
    return scrubbed[:500]
