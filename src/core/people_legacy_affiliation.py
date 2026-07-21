"""specs/people.md Phase 0c (§9): a narrow, alias-only "read-only value
slice" proving the cross-program affiliation-query concept before the
identity/registry layer (Phase 1+) exists. This module deliberately does
NOT use EntityRegistry or any canonical identity -- it groups by raw,
casefolded alias string, which is exactly the "practical key" limitation
specs/people.md §3.2 gap #1 names. Every caller-facing surface (CLI,
JSON payload) must carry the `legacy_alias` confidence-mode caveat per
§8.2; this module itself only produces the underlying edges.

Scope (Phase 0c only -- see specs/people.md §7.3.1's full emitter
inventory for what Phase 3's typed extractors will eventually cover):
charter stakeholders (dual-read top-level and `charter`-nested per gap
#4), workstream RACI/notify fields, and `workstream_registry.yaml`
routing stakeholders. Milestone/risk/action/decision/dependency owners
are deferred to Phase 3, which has richer typed-extractor requirements
(§7.3) this slice does not attempt to replicate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.core.config_loader import PROGRAMS_ROOT
from src.core.workstream_registry import load_authored_workstream_registry
from src.core.yaml_utils import load_yaml_mapping

#: Mirrors specs/people.md §7.3's governed relation-type vocabulary for
#: the subset of emitters this Phase 0c slice implements.
RELATION_CHARTER_STAKEHOLDER = "charter_stakeholder"
RELATION_RACI_ACCOUNTABLE = "raci_accountable"
RELATION_RACI_RESPONSIBLE = "raci_responsible"
RELATION_RACI_CONSULTED = "raci_consulted"
RELATION_RACI_INFORMED = "raci_informed"
RELATION_WORKSTREAM_ROUTING = "workstream_routing"


@dataclass(frozen=True, slots=True)
class LegacyAffiliationEdge:
    """One alias-to-program accountability reference, as currently
    authored in alias-keyed program config. Not a `ProgramAffiliation`
    (specs/people.md §7.3) -- that type requires canonical `entity_id`,
    which does not exist until Phase 2a."""

    alias: str
    program_id: str
    relation_type: str
    source_path: str
    workstream_id: str | None = None


def _normalize_alias(value: str) -> str:
    return value.strip().casefold()


def _charter_stakeholder_edges(program_id: str, raw_program: dict) -> tuple[LegacyAffiliationEdge, ...]:
    edges: list[LegacyAffiliationEdge] = []
    charter = raw_program.get("charter")
    if isinstance(charter, dict):
        for entry in charter.get("stakeholder_register", None) or []:
            alias = _entry_alias(entry)
            if alias:
                edges.append(
                    LegacyAffiliationEdge(
                        alias=alias,
                        program_id=program_id,
                        relation_type=RELATION_CHARTER_STAKEHOLDER,
                        source_path="program.yaml#charter.stakeholder_register",
                    )
                )
    # Legacy top-level form, dual-read per specs/people.md §5.6 item 2 / gap #4.
    for entry in raw_program.get("stakeholder_register", None) or []:
        alias = _entry_alias(entry)
        if alias:
            edges.append(
                LegacyAffiliationEdge(
                    alias=alias,
                    program_id=program_id,
                    relation_type=RELATION_CHARTER_STAKEHOLDER,
                    source_path="program.yaml#stakeholder_register (legacy top-level)",
                )
            )
    return tuple(edges)


def _entry_alias(entry: object) -> str | None:
    if isinstance(entry, dict):
        alias = entry.get("alias")
        return str(alias).strip() if alias else None
    if isinstance(entry, str) and entry.strip():
        return entry.strip()
    return None


def _workstream_raci_edges(program_id: str, raw_workstreams: dict) -> tuple[LegacyAffiliationEdge, ...]:
    edges: list[LegacyAffiliationEdge] = []
    entries = raw_workstreams.get("workstreams", None)
    if not isinstance(entries, list):
        return ()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        workstream_id = str(entry.get("id") or "").strip() or None
        source = f"workstreams.yaml#{workstream_id or '?'}"
        accountable = entry.get("accountable_owner")
        if isinstance(accountable, str) and accountable.strip():
            edges.append(
                LegacyAffiliationEdge(
                    alias=accountable.strip(),
                    program_id=program_id,
                    relation_type=RELATION_RACI_ACCOUNTABLE,
                    source_path=f"{source}.accountable_owner",
                    workstream_id=workstream_id,
                )
            )
        for field, relation in (
            ("responsible_owners", RELATION_RACI_RESPONSIBLE),
            ("consulted_owners", RELATION_RACI_CONSULTED),
            ("informed_owners", RELATION_RACI_INFORMED),
            ("always_notify", RELATION_RACI_INFORMED),
        ):
            for alias in entry.get(field, None) or []:
                if isinstance(alias, str) and alias.strip():
                    edges.append(
                        LegacyAffiliationEdge(
                            alias=alias.strip(),
                            program_id=program_id,
                            relation_type=relation,
                            source_path=f"{source}.{field}",
                            workstream_id=workstream_id,
                        )
                    )
    return tuple(edges)


def _workstream_registry_routing_edges(program_id: str, *, programs_root: Path) -> tuple[LegacyAffiliationEdge, ...]:
    edges: list[LegacyAffiliationEdge] = []
    for registry_entry in load_authored_workstream_registry(program_id=program_id, programs_root=programs_root):
        for stakeholder in registry_entry.stakeholders:
            if stakeholder.alias and stakeholder.alias.strip():
                edges.append(
                    LegacyAffiliationEdge(
                        alias=stakeholder.alias.strip(),
                        program_id=program_id,
                        relation_type=RELATION_WORKSTREAM_ROUTING,
                        source_path=f"workstream_registry.yaml#{registry_entry.id}.stakeholders",
                        workstream_id=registry_entry.id,
                    )
                )
    return tuple(edges)


def scan_program_legacy_affiliations(
    program_id: str, *, programs_root: Path = PROGRAMS_ROOT
) -> tuple[LegacyAffiliationEdge, ...]:
    """Best-effort scan of one program's alias-keyed accountability
    fields (Phase 0c scope only). A missing/malformed source file
    degrades to an empty contribution from that file rather than
    failing the whole scan -- this is a read-only query surface, not a
    validator (doctor's DIR-* checks own that job in later phases)."""
    program_dir = programs_root / program_id
    edges: list[LegacyAffiliationEdge] = []

    raw_program = load_yaml_mapping(program_dir / "program.yaml", required=False)
    edges.extend(_charter_stakeholder_edges(program_id, raw_program))

    raw_workstreams = load_yaml_mapping(program_dir / "workstreams.yaml", required=False)
    edges.extend(_workstream_raci_edges(program_id, raw_workstreams))

    try:
        edges.extend(_workstream_registry_routing_edges(program_id, programs_root=programs_root))
    except Exception:
        pass  # noqa: BLE001 -- best-effort per this module's own docstring contract.

    return tuple(edges)


def discover_program_ids(*, programs_root: Path = PROGRAMS_ROOT) -> tuple[str, ...]:
    """Every program directory with a `program.yaml`, mirroring
    `fleet.py::build_fleet_report`'s established enumeration pattern."""
    if not programs_root.exists():
        return ()
    return tuple(
        sorted(
            (
                entry.name
                for entry in programs_root.iterdir()
                if entry.is_dir() and not entry.name.startswith("_") and (entry / "program.yaml").exists()
            ),
            key=str.lower,
        )
    )


def scan_all_legacy_affiliations(*, programs_root: Path = PROGRAMS_ROOT) -> tuple[LegacyAffiliationEdge, ...]:
    edges: list[LegacyAffiliationEdge] = []
    for program_id in discover_program_ids(programs_root=programs_root):
        edges.extend(scan_program_legacy_affiliations(program_id, programs_root=programs_root))
    return tuple(edges)


def find_alias_edges(alias: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[LegacyAffiliationEdge, ...]:
    """Every edge for one alias across all programs, matched by Unicode
    casefold per specs/people.md §7.2a's normalization rule (exact
    alias-string comparison only -- no fuzzy matching in Phase 0c)."""
    target = _normalize_alias(alias)
    return tuple(
        edge for edge in scan_all_legacy_affiliations(programs_root=programs_root) if _normalize_alias(edge.alias) == target
    )


def find_cross_program_overlaps(
    *, programs_root: Path = PROGRAMS_ROOT, program_id: str | None = None
) -> tuple[tuple[str, tuple[LegacyAffiliationEdge, ...]], ...]:
    """Aliases whose edges span >=2 distinct programs. When `program_id`
    is given, results are scoped to aliases that reference that program
    at least once, but each alias's full cross-program edge set is
    still returned (the point of "overlap" is showing every OTHER
    program too). Sorted by casefolded alias for stable output."""
    by_alias: dict[str, list[LegacyAffiliationEdge]] = {}
    for edge in scan_all_legacy_affiliations(programs_root=programs_root):
        by_alias.setdefault(_normalize_alias(edge.alias), []).append(edge)

    results: list[tuple[str, tuple[LegacyAffiliationEdge, ...]]] = []
    for _normalized, edges in by_alias.items():
        program_ids = {edge.program_id for edge in edges}
        if len(program_ids) < 2:
            continue
        if program_id is not None and program_id not in program_ids:
            continue
        display_alias = edges[0].alias
        results.append((display_alias, tuple(edges)))

    results.sort(key=lambda item: item[0].casefold())
    return tuple(results)
