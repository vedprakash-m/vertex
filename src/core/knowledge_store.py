from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Literal

from src.core.exceptions import ConfigError
from src.core.models_v2 import EngMsPage, KustoQuery, PersonDirectory, PersonProfile, Product, Team
from src.core.profile_encryption import load_people_profiles_document
from src.core.yaml_utils import fast_safe_load


REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAMS_ROOT = REPO_ROOT / "programs"
SHARED_KNOWLEDGE_ROOT = REPO_ROOT / "knowledge"


@dataclass(frozen=True, slots=True)
class KnowledgeStore:
    people_directory: tuple[PersonDirectory, ...]
    people_profiles: tuple[PersonProfile, ...]
    teams: tuple[Team, ...]
    products: tuple[Product, ...]
    golden_queries: tuple[KustoQuery, ...]
    engms_pages: tuple[EngMsPage, ...] = ()


@dataclass(frozen=True, slots=True)
class PeopleDirectoryDrift:
    knowledge_only_aliases: tuple[str, ...]
    ado_only_aliases: tuple[str, ...]


_QUERY_CLASSIFICATIONS = {"validation", "analytics_history", "evidence", "hygiene", "retired"}


def _query_classification(value: Any, path: Path) -> str:
    classification = _optional_str(value) or "validation"
    if classification not in _QUERY_CLASSIFICATIONS:
        raise ConfigError(
            f"Unsupported golden query classification {classification!r} in {path}; "
            f"allowed values: {sorted(_QUERY_CLASSIFICATIONS)}"
        )
    return classification


def load_knowledge(
    *,
    knowledge_root: Path,
    fallback_root: Path | None = None,
    document_cache: dict[Path, dict[str, Any]] | None = None,
) -> KnowledgeStore:
    """specs/people.md PPL-W3.5c: `document_cache`, when supplied, is keyed
    by each document's FINAL resolved path (whichever of `knowledge_root`/
    `fallback_root` actually had the file) -- mirroring
    `kb_updates.py::read_program_kb_documents`'s own established
    `document_cache` contract exactly (deepcopy on both hit and miss, so
    a cache hit never hands back the same mutable object twice). `None`
    (the default) is a true no-op: every existing caller keeps its
    original always-read-from-disk behavior byte-for-byte unchanged.
    Added after cProfile against the real 10,000-person/100-program §8.6
    scale fixture showed `_load_optional_yaml`'s `fast_safe_load` call
    still re-parsing the SAME shared-registry file text on every one of
    100 `doctor --kb` per-program `load_knowledge` calls even after
    `kb_updates.py::SharedKnowledgeTempCache` stopped re-DUMPING it --
    the fallback file's resolved path is stable across that whole loop
    (one reused temp directory), so a resolved-path cache correctly hits
    on calls 2 through 100."""
    people_directory_doc = _load_optional_yaml(
        knowledge_root / "people_directory.yaml",
        fallback_path=fallback_root / "people_directory.yaml" if fallback_root is not None else None,
        document_cache=document_cache,
    )
    people_profiles_doc = _load_optional_people_profiles_yaml(
        knowledge_root / "people_profiles.yaml",
        fallback_path=fallback_root / "people_profiles.yaml" if fallback_root is not None else None,
        document_cache=document_cache,
    )
    teams_doc = _load_optional_yaml(
        knowledge_root / "teams.yaml",
        fallback_path=fallback_root / "teams.yaml" if fallback_root is not None else None,
        document_cache=document_cache,
    )
    products_doc = _load_optional_yaml(
        knowledge_root / "products.yaml",
        fallback_path=fallback_root / "products.yaml" if fallback_root is not None else None,
        document_cache=document_cache,
    )
    queries_doc = _load_optional_yaml(
        knowledge_root / "golden_queries.yaml",
        fallback_path=fallback_root / "golden_queries.yaml" if fallback_root is not None else None,
        document_cache=document_cache,
    )
    engms_doc = _load_optional_yaml(
        knowledge_root / "engms_pages.yaml",
        fallback_path=fallback_root / "engms_pages.yaml" if fallback_root is not None else None,
        document_cache=document_cache,
    )

    people_directory = tuple(
        PersonDirectory(
            alias=_require_str(entry, "alias", knowledge_root / "people_directory.yaml"),
            email=_optional_str(entry.get("email")),
            display_name=_optional_str(entry.get("display_name") or entry.get("name")),
            title=_optional_str(entry.get("title")),
            team_ids=_string_tuple(entry.get("team_ids", [])),
            org_chain=_string_tuple(entry.get("org_chain", [])),
            exempt_from_vitality=bool(entry.get("exempt_from_vitality", False)),
            manager_alias=_optional_str(entry.get("manager_alias")),
            department=_optional_str(entry.get("department")),
        )
        for entry in people_directory_doc.get("people", [])
        if isinstance(entry, dict)
    )

    people_profiles = tuple(
        PersonProfile(
            alias=_require_str(entry, "alias", knowledge_root / "people_profiles.yaml"),
            comm_style=_optional_str(entry.get("comm_style")),
            cares_about=_string_tuple(entry.get("cares_about", [])),
            pet_peeves=_string_tuple(entry.get("pet_peeves", [])),
        )
        for entry in people_profiles_doc.get("profiles", [])
        if isinstance(entry, dict)
    )

    teams = tuple(
        Team(
            id=_require_str(entry, "id", knowledge_root / "teams.yaml"),
            name=_optional_str(entry.get("name")) or _require_str(entry, "id", knowledge_root / "teams.yaml"),
            area_paths=_string_tuple(entry.get("area_paths", [])),
            programs=_string_tuple(entry.get("programs", [])),
        )
        for entry in teams_doc.get("teams", [])
        if isinstance(entry, dict)
    )

    products = tuple(
        Product(
            id=_require_str(entry, "id", knowledge_root / "products.yaml"),
            name=_optional_str(entry.get("name")) or _require_str(entry, "id", knowledge_root / "products.yaml"),
            aliases=_string_tuple(entry.get("aliases", [])),
            related_teams=_string_tuple(entry.get("related_teams", [])),
            description=_optional_str(entry.get("description")),
        )
        for entry in products_doc.get("products", [])
        if isinstance(entry, dict)
    )

    golden_queries = tuple(
        KustoQuery(
            id=_require_str(entry, "id", knowledge_root / "golden_queries.yaml"),
            cluster=_optional_str(entry.get("cluster")) or "",
            database=_optional_str(entry.get("database")) or "",
            kql=_optional_str(entry.get("kql")) or (_load_kql_from_file(str(entry.get("kql_file")), knowledge_root.parent) if entry.get("kql_file") else ""),
            section=_optional_str(entry.get("section")) or _require_str(entry, "id", knowledge_root / "golden_queries.yaml"),
            render_as=_optional_str(entry.get("render_as")) or "table",
            confidence=_optional_str(entry.get("confidence")) or "medium",
            reference_url=_optional_str(entry.get("reference_url")),
            caveats=_string_tuple(entry.get("caveats", [])),
            kusto_section_validates_slice=bool(entry.get("kusto_section_validates_slice", False)),
            program_ids=_string_tuple(entry.get("program_ids", [])),
            workstream_ids=_string_tuple(entry.get("workstream_ids", [])),
            validated=_legacy_validated_flag(entry),
            refresh_on_gather=bool(entry.get("refresh_on_gather", False)),
            label=_optional_str(entry.get("label")),
            result_column=_optional_str(entry.get("result_column")),
            metric_id=_optional_str(entry.get("metric_id")),
            unit=_optional_str(entry.get("unit")),
            slo_target=float(entry["slo_target"]) if entry.get("slo_target") is not None else None,
            comparison=_optional_str(entry.get("comparison")),
            assertion_ids=_string_tuple(entry.get("assertion_ids", [])),
            engine=_optional_str(entry.get("engine")) or ("wiql" if _optional_str(entry.get("wiql")) else "kusto"),
            wiql=_optional_str(entry.get("wiql")),
            classification=_query_classification(entry.get("classification"), knowledge_root / "golden_queries.yaml"),
        )
        for entry in queries_doc.get("queries", [])
        if isinstance(entry, dict)
    )

    engms_pages = tuple(
        EngMsPage(
            id=_require_str(entry, "id", knowledge_root / "engms_pages.yaml"),
            title=_optional_str(entry.get("title")) or _require_str(entry, "id", knowledge_root / "engms_pages.yaml"),
            url=_require_url(entry, "url", knowledge_root / "engms_pages.yaml"),
            workstream_ids=_string_tuple(entry.get("workstream_ids", [])),
            program_ids=_string_tuple(entry.get("program_ids", [])),
            tags=_string_tuple(entry.get("tags", [])),
            description=_optional_str(entry.get("description")),
            source_subtype=_optional_source_subtype(entry.get("source_subtype")),  # SP2-2  # type: ignore[arg-type]
            cadence_days=_optional_int(entry.get("cadence_days")),  # SP2-2
        )
        for entry in engms_doc.get("pages", [])
        if isinstance(entry, dict)
    )

    knowledge = KnowledgeStore(
        people_directory=people_directory,
        people_profiles=people_profiles,
        teams=teams,
        products=products,
        golden_queries=golden_queries,
        engms_pages=engms_pages,
    )
    validate_knowledge(knowledge)
    return knowledge


def get_shared_knowledge_root(programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root.parent / "knowledge"


def load_program_knowledge(
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
    *,
    shared_knowledge_root: Path | None = None,
    document_cache: dict[Path, dict[str, Any]] | None = None,
) -> KnowledgeStore:
    program_knowledge_root = programs_root / program_id / "knowledge"
    resolved_shared_root = shared_knowledge_root or get_shared_knowledge_root(programs_root)
    if resolved_shared_root.exists():
        fallback_root = program_knowledge_root if program_knowledge_root.exists() else None
        return load_knowledge(knowledge_root=resolved_shared_root, fallback_root=fallback_root, document_cache=document_cache)
    return load_knowledge(knowledge_root=program_knowledge_root, document_cache=document_cache)


def validate_knowledge(knowledge: KnowledgeStore, *, known_program_ids: tuple[str, ...] = ()) -> None:
    aliases = {person.alias for person in knowledge.people_directory}
    team_ids = {team.id for team in knowledge.teams}
    problems: list[str] = []

    for person in knowledge.people_directory:
        for team_id in person.team_ids:
            if team_id not in team_ids:
                problems.append(f"Unknown team_id '{team_id}' referenced by person '{person.alias}'.")

    for profile in knowledge.people_profiles:
        if profile.alias not in aliases:
            problems.append(f"Unknown profile alias '{profile.alias}' in people_profiles.yaml.")

    seen_engms_ids: set[str] = set()
    for page in knowledge.engms_pages:
        if page.id in seen_engms_ids:
            problems.append(f"Duplicate eng.ms page id '{page.id}' in engms_pages.yaml.")
        seen_engms_ids.add(page.id)

    if known_program_ids:
        problems.extend(find_unknown_team_program_references(knowledge, known_program_ids=known_program_ids))
        problems.extend(find_unknown_engms_program_references(knowledge, known_program_ids=known_program_ids))

    if problems:
        raise ConfigError("; ".join(problems))


def find_unknown_team_program_references(
    knowledge: KnowledgeStore,
    *,
    known_program_ids: tuple[str, ...],
) -> tuple[str, ...]:
    if not known_program_ids:
        return ()

    known_program_id_set = set(known_program_ids)
    return tuple(
        f"Unknown program '{program_id}' referenced by team '{team.id}'."
        for team in knowledge.teams
        for program_id in team.programs
        if program_id not in known_program_id_set
    )


def find_unknown_engms_program_references(
    knowledge: KnowledgeStore,
    *,
    known_program_ids: tuple[str, ...],
) -> tuple[str, ...]:
    if not known_program_ids:
        return ()

    known_program_id_set = set(known_program_ids)
    return tuple(
        f"Unknown program '{program_id}' referenced by eng.ms page '{page.id}'."
        for page in knowledge.engms_pages
        for program_id in page.program_ids
        if program_id not in known_program_id_set
    )


def detect_people_directory_drift(
    knowledge: KnowledgeStore,
    ado_identities: tuple[str, ...],
) -> PeopleDirectoryDrift:
    directory_aliases = {
        normalized
        for normalized in (_normalize_alias(person.alias) for person in knowledge.people_directory)
        if normalized is not None
    }
    ado_aliases = {
        normalized
        for normalized in (_normalize_alias(identity) for identity in ado_identities)
        if normalized is not None
    }
    return PeopleDirectoryDrift(
        knowledge_only_aliases=tuple(sorted(directory_aliases - ado_aliases)),
        ado_only_aliases=tuple(sorted(ado_aliases - directory_aliases)),
    )


def select_engms_pages(
    knowledge: KnowledgeStore,
    *,
    program_id: str,
    workstream_ids: tuple[str, ...] = (),
) -> tuple[EngMsPage, ...]:
    target_workstreams = {workstream_id for workstream_id in workstream_ids if workstream_id}

    def program_matches(page: EngMsPage) -> bool:
        return not page.program_ids or program_id in page.program_ids

    def workstream_matches(page: EngMsPage) -> bool:
        if not target_workstreams or not page.workstream_ids:
            return True
        return bool(target_workstreams.intersection(page.workstream_ids))

    selected = [page for page in knowledge.engms_pages if program_matches(page) and workstream_matches(page)]
    selected.sort(key=lambda page: (0 if page.workstream_ids else 1, page.title.lower(), page.id.lower()))
    return tuple(selected)


def select_sharepoint_engms_pages(
    knowledge: KnowledgeStore,
    *,
    program_id: str,
    workstreams: "tuple[Any, ...]" = (),
) -> tuple[EngMsPage, ...]:
    """SP2-6: Select SharePoint EngMsPage entries for a program using UNION routing.

    Resolution rule (§10.1): UNION of:
    - Pages that declare this program in program_ids
    - Pages routed from workstream side via signal_sources.sharepoint_paths

    'workstreams' is typed Any to avoid a circular import with models_v2.Workstream.
    """
    # Collect page IDs declared from the workstream side (sharepoint_paths)
    workstream_declared_ids: set[str] = set()
    for ws in workstreams:
        ss = getattr(ws, "signal_sources", None)
        if ss is not None:
            for page_id in getattr(ss, "sharepoint_paths", ()):
                workstream_declared_ids.add(page_id)
            for page_id in getattr(ss, "engms_paths", ()):
                workstream_declared_ids.add(page_id)

    def _matches(page: EngMsPage) -> bool:
        # Program filter: page must belong to this program (or be undeclared)
        program_ok = not page.program_ids or program_id in page.program_ids
        if not program_ok:
            return False
        # Document-side declaration or workstream-side declaration (UNION)
        return bool(page.workstream_ids) or page.id in workstream_declared_ids or (
            not page.workstream_ids and not workstream_declared_ids
        )

    selected = [page for page in knowledge.engms_pages if _matches(page)]
    selected.sort(key=lambda page: (0 if page.workstream_ids else 1, page.title.lower(), page.id.lower()))
    return tuple(selected)


def _load_optional_yaml(
    path: Path, *, fallback_path: Path | None = None, document_cache: dict[Path, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not path.exists():
        if fallback_path is None or not fallback_path.exists():
            return {}
        path = fallback_path
    if document_cache is not None and path in document_cache:
        return deepcopy(document_cache[path])
    document = fast_safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    if document_cache is not None:
        document_cache[path] = document
        document = deepcopy(document)
    return document


def _load_optional_people_profiles_yaml(
    path: Path, *, fallback_path: Path | None = None, document_cache: dict[Path, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not path.exists():
        if fallback_path is None or not fallback_path.exists():
            return {}
        path = fallback_path
    if document_cache is not None and path in document_cache:
        return deepcopy(document_cache[path])
    document = load_people_profiles_document(path)
    if document_cache is not None:
        document_cache[path] = document
        document = deepcopy(document)
    return document


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(entry).strip() for entry in value if str(entry).strip())


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _legacy_validated_flag(entry: dict[str, Any]) -> bool:
    if "validated" not in entry:
        return True
    return bool(entry.get("validated", False))


def _normalize_alias(value: str | None) -> str | None:
    if value is None:
        return None
    alias = value.strip().lower()
    if not alias:
        return None
    if "@" in alias:
        alias = alias.split("@", 1)[0]
    alias = re.sub(r"[^a-z0-9._-]", "", alias)
    return alias or None


def _require_str(entry: dict[str, Any], key: str, path: Path) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} is required in {path}")
    return value.strip()



def _require_url(entry: dict[str, Any], key: str, path: Path) -> str:
    value = _require_str(entry, key, path)
    if not re.match(r"^https?://", value, flags=re.IGNORECASE):
        raise ConfigError(f"{key} must be an http(s) URL in {path}")
    return value


def _load_kql_from_file(rel_path: str, repo_root: Path) -> str:
    path = repo_root / rel_path
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _optional_source_subtype(value: Any) -> "Literal['lt_deck', 'ref_doc'] | None":
    """SP2-2: Parse source_subtype field — only 'lt_deck' and 'ref_doc' are valid."""
    if value in (None, ""):
        return None
    normalized = str(value).strip().lower()
    if normalized in ("lt_deck", "ref_doc"):
        return normalized  # type: ignore[return-value]
    return None  # silently ignore unknown subtypes


def _optional_int(value: Any) -> int | None:
    """SP2-2: Parse optional integer field (e.g. cadence_days)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
