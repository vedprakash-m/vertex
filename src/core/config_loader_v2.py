from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.dependency_graph import dependency_source_label, dependency_target_label
from src.core.exceptions import ConfigError
from src.core.edition_resolver import PROGRAMS_ROOT, ResolvedEdition, filter_workstreams, resolve_area_paths, resolve_edition
from src.core.knowledge_store import KnowledgeStore, load_program_knowledge
from src.core.program_fact_store import load_program_facts, project_dependencies


@dataclass(frozen=True, slots=True)
class V2BundleInput:
    resolved: ResolvedEdition
    knowledge: KnowledgeStore
    config_document: dict[str, Any]
    program_context_document: dict[str, Any]
    editorial_rules_path: Path
    review_path: Path
    template_contract_path: Path
    slice_contract_path: Path
    chapter_contract_path: Path


def load_edition_bundle(
    edition_id: str,
    *,
    editions_root: Path | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> V2BundleInput | None:
    resolved = resolve_edition(edition_id, editions_root=editions_root, programs_root=programs_root)
    if resolved is None:
        return None
    knowledge = load_program_knowledge(resolved.paths.program_id, programs_root=programs_root)
    return V2BundleInput(
        resolved=resolved,
        knowledge=knowledge,
        config_document=_build_report_config_document(resolved, knowledge),
        program_context_document=_build_program_context_document(resolved, knowledge),
        editorial_rules_path=resolved.paths.program_dir / "editorial_rules.yaml",
        review_path=resolved.paths.program_dir / "review.yaml",
        template_contract_path=resolved.paths.program_dir / "template_contract.yaml",
        slice_contract_path=resolved.paths.program_dir / "slice_contracts.yaml",
        chapter_contract_path=resolved.paths.program_dir / "chapter_contract.yaml",
    )


def _build_report_config_document(resolved: ResolvedEdition, knowledge: KnowledgeStore) -> dict[str, Any]:
    edition_raw = resolved.raw_edition
    program_raw = resolved.raw_program
    filtered_workstreams = filter_workstreams(resolved.workstreams, resolved.edition.workstream_filter)

    author_defaults = _merge_mapping(_mapping(program_raw.get("author_defaults")), _mapping(edition_raw.get("author")))
    distribution_defaults = _merge_mapping(_mapping(program_raw.get("distribution_defaults")), _mapping(edition_raw.get("distribution")))
    ado = _merge_mapping(_mapping(program_raw.get("ado")), _mapping(edition_raw.get("ado")))
    ai = _merge_mapping(_mapping(program_raw.get("ai")), _mapping(edition_raw.get("ai")))
    kusto = _merge_mapping(_mapping(program_raw.get("kusto")), _mapping(edition_raw.get("kusto")))
    m365 = _merge_mapping(_mapping(program_raw.get("m365")), _mapping(edition_raw.get("m365")))

    ado["area_paths"] = list(resolve_area_paths(resolved.edition, filtered_workstreams))
    if resolved.edition.ado_fetch_timeout_seconds is not None:
        ado["api_timeout_seconds"] = resolved.edition.ado_fetch_timeout_seconds

    raw_queries = _queries_from_knowledge(knowledge, program_id=resolved.paths.program_id)
    if raw_queries:
        kusto["queries"] = raw_queries

    config_document = {
        "schema_version": resolved.edition.schema_version,
        "edition": {
            "name": resolved.edition.id,
            "type": resolved.edition.type,
            "title": resolved.edition.name,
            "cadence": resolved.edition.cadence,
            "send_day": resolved.edition.send_day,
            "send_time_local": resolved.edition.send_time_local,
            "timezone": resolved.edition.timezone,
        },
        "layout_mode": resolved.edition.layout_mode,
        "cadence_note": resolved.edition.cadence_note,
        "scorecard_sort": resolved.edition.scorecard_sort,
        "scorecard_plain_text_only": resolved.edition.scorecard_plain_text_only,
        "brand_name": resolved.edition.brand_name,
        "brand_header_url": resolved.edition.brand_header_url,
        "author": author_defaults,
        "distribution": distribution_defaults,
        "ado": ado,
        "scorecards": [
            {
                "name": scorecard.name,
                "dimensions": _build_scorecard_dimension_documents(resolved.scorecards, scorecard.name),
            }
            for scorecard in resolved.scorecards
        ],
        "ai": ai,
        "kusto": kusto,
        "m365": m365,
        "archive": {
            "root": _relative_root(resolved.paths.archive_dir),
        },
        "logging": _mapping(program_raw.get("logging")) or {"level": "INFO", "json": False},
        "ado_fetch_timeout_seconds": resolved.edition.ado_fetch_timeout_seconds or ado.get("api_timeout_seconds"),
        "forecast_enabled": resolved.edition.forecast_enabled,
        "mobile_safe_scorecards": resolved.edition.mobile_safe_scorecards,
        "type_scale_v2": resolved.edition.type_scale_v2,
        "calibration_pilot": resolved.edition.calibration_pilot,
    }
    return config_document


def _build_scorecard_dimension_documents(
    scorecards: tuple[Any, ...],
    scorecard_name: str,
) -> list[dict[str, Any]]:
    dimension_index = {
        (scorecard.name, dimension.name): dimension
        for scorecard in scorecards
        for dimension in scorecard.dimensions
    }
    resolved_documents: dict[tuple[str, str], dict[str, Any]] = {}
    resolving: set[tuple[str, str]] = set()

    def resolve_dimension(document_scorecard_name: str, dimension_name: str) -> dict[str, Any]:
        key = (document_scorecard_name, dimension_name)
        cached = resolved_documents.get(key)
        if cached is not None:
            return cached
        if key in resolving:
            raise ConfigError(
                f"Scorecard dimension link cycle detected at {document_scorecard_name} / {dimension_name}."
            )

        dimension = dimension_index.get(key)
        if dimension is None:
            raise ConfigError(
                f"Scorecard dimension link target {document_scorecard_name} / {dimension_name} does not exist."
            )

        resolving.add(key)
        linked_scorecard_name = dimension.linked_scorecard_name
        linked_dimension_name = dimension.linked_dimension_name
        resolved_description = dimension.description
        resolved_filter = (dimension.ado_filter or "").strip()

        if linked_scorecard_name is not None or linked_dimension_name is not None:
            if not linked_scorecard_name or not linked_dimension_name:
                raise ConfigError(
                    f"Scorecard dimension {document_scorecard_name} / {dimension_name} must declare both linked_scorecard and linked_dimension."
                )
            linked_document = resolve_dimension(linked_scorecard_name, linked_dimension_name)
            if resolved_description is None:
                resolved_description = linked_document.get("description")
            if not resolved_filter:
                resolved_filter = str(linked_document.get("ado_filter", "")).strip()

        document = {
            "name": dimension.name,
            "description": resolved_description,
            "ado_filter": resolved_filter,
            "linked_scorecard": linked_scorecard_name,
            "linked_dimension": linked_dimension_name,
        }
        resolved_documents[key] = document
        resolving.remove(key)
        return document

    scorecard = next((entry for entry in scorecards if entry.name == scorecard_name), None)
    if scorecard is None:
        raise ConfigError(f"Scorecard {scorecard_name} does not exist.")
    return [resolve_dimension(scorecard.name, dimension.name) for dimension in scorecard.dimensions]


def _build_program_context_document(resolved: ResolvedEdition, knowledge: KnowledgeStore) -> dict[str, Any]:
    raw_program = resolved.raw_program
    raw_workstreams = resolved.raw_workstreams
    people_payload = raw_program.get("people")
    if not isinstance(people_payload, list):
        people_payload = [
            {
                "email": person.email,
                "display_name": person.display_name,
                "role": person.title,
                "workstreams": [
                    workstream.name
                    for workstream in resolved.workstreams
                    if workstream.dri_email and person.email and workstream.dri_email.lower() == person.email.lower()
                ],
            }
            for person in knowledge.people_directory
            if person.email
        ]
    return {
        "schema_version": "1.0",
        "program_name": resolved.program.name,
        "objective": resolved.program.objective,
        "mission": resolved.program.mission,
        "current_phase": resolved.program.current_phase,
        "pillars": list(resolved.program.pillars),
        "glossary": dict(resolved.program.glossary or {}),
        "sub_programs": raw_program.get("sub_programs", []),
        "workstreams": [
            {
                "name": workstream.name,
                "aliases": list(workstream.aliases),
                "area_paths": list(workstream.area_paths),
                "dri_email": workstream.dri_email,
                "alternate_owner": workstream.alternate_owner,
                "description": workstream.description,
                "why_it_matters": workstream.why_it_matters,
                "history_summary": workstream.history_summary,
                "leadership_sensitivity": workstream.leadership_sensitivity,
                "current_blocker": workstream.current_blocker,
            }
            for workstream in resolved.workstreams
        ],
        "people": people_payload,
        "leadership_readers": [
            {
                "name": reader.name,
                "role": reader.role,
                "cares_about": list(reader.cares_about),
                "prefers": reader.prefers,
                "pet_peeves": list(reader.pet_peeves),
            }
            for reader in resolved.program.leadership_readers
        ],
        "workstream_owners": raw_workstreams.get("workstream_owners", raw_program.get("workstream_owners", [])),
        "recurring_themes": raw_program.get("recurring_themes", []),
        "writing_style": raw_program.get("writing_style", {}),
        "tone_calibration": raw_program.get("tone_calibration", {}),
        "key_dependency_chain": _build_key_dependency_chain_document(resolved),
        "leadership_personas": raw_program.get("leadership_personas")
        or (raw_program.get("program_intelligence") or {}).get("leadership_personas", {}),
    }


def _build_key_dependency_chain_document(resolved: ResolvedEdition) -> list[dict[str, str]]:
    raw_dependencies = resolved.raw_program.get("key_dependencies")
    if isinstance(raw_dependencies, list) and raw_dependencies:
        return raw_dependencies

    try:
        program_dir = resolved.paths.program_dir
        # For template programs (program_dir under _templates/), the programs_root
        # is two levels up; for regular programs it is one level up.
        if program_dir.parent.name == "_templates":
            inferred_programs_root = program_dir.parent.parent
        else:
            inferred_programs_root = program_dir.parent
        dependencies = _load_current_dependencies(
            resolved.paths.program_id,
            programs_root=inferred_programs_root,
        )
    except ConfigError:
        return []

    return [
        {
            "from_item": dependency_source_label(dependency),
            "to_item": dependency_target_label(dependency),
            "impact": dependency.risk_if_broken,
        }
        for dependency in dependencies
    ]


def _load_current_dependencies(program_id: str, *, programs_root: Path):
    return project_dependencies(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("dependency.link",),
        )
    )


def _queries_from_knowledge(knowledge: KnowledgeStore, *, program_id: str) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    for query in knowledge.golden_queries:
        if query.engine != "kusto":
            continue
        if query.program_ids and program_id not in query.program_ids:
            continue
        queries.append(
            {
                "id": query.id,
                "cluster": query.cluster,
                "database": query.database,
                "kql": query.kql,
                "section": query.section,
                "render_as": query.render_as,
                "confidence": query.confidence,
                "kusto_section_validates_slice": query.kusto_section_validates_slice,
                "caveats": list(query.caveats),
                "reference_url": query.reference_url,
            }
        )
    return queries


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _merge_mapping(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if value is None:
            merged[key] = value
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_mapping(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def _relative_root(path: Path) -> str:
    try:
        return str(path.relative_to(path.parents[2])).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")
