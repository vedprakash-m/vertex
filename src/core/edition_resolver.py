from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Literal, cast

from src.core.exceptions import ConfigError, VertexMigrationError
from src.core.models_v2 import ADOCoverageRequirement, ADOConfig, AIConfig, AuthorConfig, DependencyADOQuery, DistributionConfig, EditionConfig, EmailThreadSource, LegacyDependency
from src.core.models_v2 import GatherActivationConfig, KustoConfig, KustoQuery, LeadershipReader, M365Config, Program, Scorecard, ScorecardDimension
from src.core.models_v2 import TeamsChat, TeamsMeetingSeries, ToneCalibration, WorkstreamFilter, Workstream, WorkstreamSignalSources, WritingStyle
from src.core.models_v2 import WORKIQ_DISCOVERY_MODES, WorkIQRetrievalConfig
from src.core.models_v2 import (
    REV_AUTH_SCOPE_TIERS,
    REV_EVIDENCE_EXCERPT_VAULTED,
    REV_EVIDENCE_METADATA_ONLY,
    REV_EVIDENCE_POLICIES,
    REV_GROUNDEDNESS_ADVISORY,
    REV_GROUNDEDNESS_GATE,
    REV_GROUNDEDNESS_MODES,
    REV_GROUNDEDNESS_OFF,
    REV_HYDRATION_DROP,
    REV_HYDRATION_METADATA_ONLY_FLAGGED,
    REV_HYDRATION_POLICIES,
    REV_PROFILE_LEGACY_NL,
    REV_PROFILES,
    REV_STRUCTURED_OUTPUTS_JSON_OBJECT,
    REV_STRUCTURED_OUTPUTS_OFF,
    REV_STRUCTURED_OUTPUTS_PROBE,
    REV_STRUCTURED_OUTPUTS_MODES,
    RevBudgets,
    RevRetrievalProfile,
)
from src.core.yaml_utils import load_yaml_mapping


REPO_ROOT = Path(__file__).resolve().parents[2]
# Legacy compatibility alias. Edition YAMLs now live under programs/<id>/editions/.
EDITIONS_ROOT = REPO_ROOT / "editions"
PROGRAMS_ROOT = Path(os.environ.get("VERTEX_PROGRAMS_ROOT", str(REPO_ROOT / "programs")))
_EXAMPLE_EDITION_PREFIX = "example_"
_TEMPLATE_PROGRAM_PREFIX = "template:"

# --- Edition workspace layout (output/ → publications/) ---
# Phase 3 flip: change _OUTPUT_SUBDIR_DEFAULT to "publications".
# _OUTPUT_SUBDIR_LEGACY is intentionally separate and never changes; it always
# points at the historical "output/" tree for migration tooling and NQ-10.
_OUTPUT_SUBDIR_DEFAULT = "publications"
_OUTPUT_SUBDIR_LEGACY = "output"
_LAYOUT_MARKER_FILENAME = ".edition_layout.json"
_output_subdir_cached: str | None = None


def _output_subdir() -> str:
    """Canonical output subfolder name.

    VERTEX_OUTPUT_SUBDIR is a transition-window escape hatch only (removed in Phase 5).
    Cached at first call — safe for short-lived CLI processes.
    NOTE: If Vertex is ever embedded in a long-lived daemon this cache would need
    a mechanism to respond to live env-var updates; as a short-lived CLI tool a
    module-load-time read is correct and sufficient.
    """
    global _output_subdir_cached
    if _output_subdir_cached is None:
        raw = os.environ.get("VERTEX_OUTPUT_SUBDIR", _OUTPUT_SUBDIR_DEFAULT)
        if "/" in raw or "\\" in raw or ".." in raw or raw == "":
            raise ConfigError(
                f"VERTEX_OUTPUT_SUBDIR contains invalid characters: {raw!r}. "
                "Must be a plain directory name (e.g., 'output' or 'publications')."
            )
        _output_subdir_cached = raw
    return _output_subdir_cached


def _reset_output_subdir_cache() -> None:
    """Reset the module-level cache.

    Call in test teardown when VERTEX_OUTPUT_SUBDIR is set per-test to prevent
    cross-test leakage.  See conftest.py fixture 'reset_output_subdir_cache'.
    NOTE: If Vertex is ever embedded in a long-lived daemon, this cache would need
    a mechanism to respond to live env-var updates. As a short-lived CLI tool, a
    module-load-time read is correct and sufficient.
    """
    global _output_subdir_cached
    _output_subdir_cached = None


def _resolve_output_dir(program_dir: Path, edition_id: str) -> Path:
    """Shared resolution logic used by get_program_output_dir() and resolve_edition_paths().

    Hard-fails on split-brain (both output/ and publications/ present simultaneously).
    Falls back to legacy path when canonical absent (transition window: code updated,
    disk not yet renamed).
    NOTE: In Phase 2, canonical == legacy always (both = "output"), so the split-brain
    check is intentionally a no-op. It becomes active in Phase 3 after the flip.
    """
    canonical = program_dir / _output_subdir() / edition_id
    legacy = program_dir / _OUTPUT_SUBDIR_LEGACY / edition_id

    if canonical.exists() and legacy.exists() and canonical != legacy:
        raise VertexMigrationError(
            f"Split-brain layout: both '{canonical}' and '{legacy}' exist. "
            f"Run: python scripts/migrate_edition_output.py --program {program_dir.name} --verify"
        )
    if not canonical.exists() and legacy.exists():
        return legacy  # transition window fallback
    return canonical


def get_program_output_root(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    """Return the program-level publications/output root (no edition suffix).

    Used by callers that scan all editions or write program-scoped artifacts:
    - brief.py: cost-guard scan (iterdir) and brief artifact write
    - reality.py: ask_misses.jsonl read, cursor/audit write
    Unlike get_program_output_dir(), does NOT append any id segment.
    """
    program_dir = programs_root / program_id
    canonical = program_dir / _output_subdir()
    legacy = program_dir / _OUTPUT_SUBDIR_LEGACY

    if canonical.exists() and legacy.exists() and canonical != legacy:
        raise VertexMigrationError(
            f"Split-brain layout: both '{canonical}' and '{legacy}' exist. "
            f"Run: python scripts/migrate_edition_output.py --program {program_id} --verify"
        )
    if not canonical.exists() and legacy.exists():
        return legacy
    return canonical


def is_example_edition_id(edition_id: str) -> bool:
    return edition_id.startswith(_EXAMPLE_EDITION_PREFIX)


@dataclass(frozen=True, slots=True)
class NudgePaths:
    """Canonical path bundle for a program's nudge workspace under programs/<id>/nudge/."""
    nudge_root: Path
    state_path: Path
    audit_path: Path
    audit_lock_path: Path
    title_cache_path: Path
    run_lock_path: Path
    drafts_dir: Path
    published_eml_dir: Path
    published_eml_index_path: Path


def get_legacy_nudge_output(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    """Return the legacy nudge output dir (pre-migration path).

    Hardcodes _OUTPUT_SUBDIR_LEGACY so it always points at the historical output/
    tree regardless of the publications/ default.  Used exclusively by NQ-10.
    Also corrects a pre-existing phantom-segment bug: the previous delegation to
    get_program_output_dir(program_id) returned programs/<id>/output/<id>/<id>_nudge
    (with a phantom <id> segment).  The canonical legacy path is programs/<id>/output/<id>_nudge.
    Remove in Phase 4 when fallback is retired.
    """
    return programs_root / program_id / _OUTPUT_SUBDIR_LEGACY / f"{program_id}_nudge"


def get_nudge_paths(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> NudgePaths:
    """Return the canonical NudgePaths for *program_id*.

    All nudge artefacts live under ``programs/<program_id>/nudge/``:
    - ``nudge_state.json`` — cooldown state
    - ``nudge_audit.jsonl`` — append-only audit log
    - ``title_cache.json`` — AI-compressed title cache
    - ``.run.lock`` — live-run exclusion lock
    - ``drafts/`` — generated EML files (pre-send)
    - ``published_eml/`` — sent EML files (trusted baseline)
      - ``index.json`` — schema 1.0 manifest of sent nudges
    """
    nudge_root = programs_root / program_id / "nudge"
    return NudgePaths(
        nudge_root=nudge_root,
        state_path=nudge_root / "nudge_state.json",
        audit_path=nudge_root / "nudge_audit.jsonl",
        audit_lock_path=nudge_root / "nudge_audit.jsonl.lock",
        title_cache_path=nudge_root / "title_cache.json",
        run_lock_path=nudge_root / ".run.lock",
        drafts_dir=nudge_root / "drafts",
        published_eml_dir=nudge_root / "published_eml",
        published_eml_index_path=nudge_root / "published_eml" / "index.json",
    )


def get_program_output_dir(edition_id_or_program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    edition_path = find_edition_yaml(edition_id_or_program_id, programs_root=programs_root)
    if edition_path.exists():
        raw_edition = load_yaml_mapping(edition_path)
        raw_program_id = raw_edition.get("program_id")
        if isinstance(raw_program_id, str):
            program_dir = _program_dir_for_reference(raw_program_id, programs_root=programs_root)
            return _resolve_output_dir(program_dir, edition_id_or_program_id)

    # Fallback/default: treat as program_id
    program_dir = _program_dir_for_reference(edition_id_or_program_id, programs_root=programs_root)
    return _resolve_output_dir(program_dir, edition_id_or_program_id)


@dataclass(frozen=True, slots=True)
class ResolvedEditionPaths:
    edition_id: str
    edition_path: Path
    program_id: str
    program_dir: Path
    knowledge_dir: Path
    archive_dir: Path
    publications_dir: Path


# Deprecated alias — remove in Phase 5 after P5-5 confirms all callers migrated.
ResolvedEditionPaths.output_dir = property(lambda self: self.publications_dir)  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class ResolvedEdition:
    paths: ResolvedEditionPaths
    edition: EditionConfig
    program: Program
    workstreams: tuple[Workstream, ...]
    scorecards: tuple[Scorecard, ...]
    raw_edition: dict[str, Any]
    raw_program: dict[str, Any]
    raw_workstreams: dict[str, Any]
    raw_scorecards: dict[str, Any]


def resolve_edition_paths(
    edition_id: str,
    *,
    editions_root: Path | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> ResolvedEditionPaths | None:
    # Primary path: scan the programs tree (programs/<id>/editions/<edition>.yaml).
    # Fallback: a caller-supplied flat ``editions_root`` (legacy/test layout) is honored
    # when the programs-tree glob does not find the edition. This keeps the on-disk
    # canonical layout under ``programs/`` while preserving backwards compatibility for
    # callers/tests that still stage editions in a single ``editions/`` directory.
    edition_path = find_edition_yaml(edition_id, programs_root=programs_root)
    if not edition_path.exists() and editions_root is not None:
        legacy_candidate = editions_root / f"{edition_id}.yaml"
        if legacy_candidate.exists():
            edition_path = legacy_candidate
    if not edition_path.exists():
        return None
    raw_edition = load_yaml_mapping(edition_path)
    raw_program_id = _required_string(raw_edition.get("program_id"), edition_path, "program_id")
    program_id = _normalized_program_id(raw_program_id)
    program_dir = _program_dir_for_reference(raw_program_id, programs_root=programs_root)
    return ResolvedEditionPaths(
        edition_id=edition_id,
        edition_path=edition_path,
        program_id=program_id,
        program_dir=program_dir,
        knowledge_dir=program_dir / "knowledge",
        archive_dir=program_dir / "archive" / edition_id,
        publications_dir=_resolve_output_dir(program_dir, edition_id),
    )


def resolve_edition(
    edition_id: str,
    *,
    editions_root: Path | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> ResolvedEdition | None:
    paths = resolve_edition_paths(
        edition_id,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if paths is None:
        return None

    raw_edition = load_yaml_mapping(paths.edition_path)
    raw_program = load_yaml_mapping(paths.program_dir / "program.yaml")
    raw_workstreams = load_yaml_mapping(paths.program_dir / "workstreams.yaml")
    raw_scorecards = load_yaml_mapping(paths.program_dir / "scorecards.yaml")

    edition = _parse_edition_config(raw_edition, paths.edition_path)
    program = _parse_program(raw_program, paths.program_dir / "program.yaml")
    if edition.ado_fetch_timeout_seconds is not None and program.ado is not None:
        program = replace(
            program,
            ado=replace(program.ado, api_timeout_seconds=edition.ado_fetch_timeout_seconds),
        )
    workstreams = _load_resolved_workstreams(
        paths.program_id,
        programs_root=programs_root,
        raw_workstreams=raw_workstreams,
        workstreams_path=paths.program_dir / "workstreams.yaml",
    )
    scorecards = _parse_scorecards(raw_scorecards, paths.program_dir / "scorecards.yaml")

    return ResolvedEdition(
        paths=paths,
        edition=edition,
        program=program,
        workstreams=workstreams,
        scorecards=scorecards,
        raw_edition=raw_edition,
        raw_program=raw_program,
        raw_workstreams=raw_workstreams,
        raw_scorecards=raw_scorecards,
    )


def _load_resolved_workstreams(
    program_id: str,
    *,
    programs_root: Path,
    raw_workstreams: dict[str, Any],
    workstreams_path: Path,
) -> tuple[Workstream, ...]:
    from src.core.program_fact_store import load_current_workstreams

    authored_order = {
        workstream_id: index
        for index, workstream_id in enumerate(_raw_workstream_ids(raw_workstreams))
    }
    workstreams = load_current_workstreams(program_id, programs_root=programs_root)
    if not workstreams:
        from src.core.workstream_documents import load_workstreams_document

        return load_workstreams_document(raw_workstreams, workstreams_path)
    fallback_index = len(authored_order)
    return tuple(
        sorted(
            workstreams,
            key=lambda workstream: (authored_order.get(workstream.id, fallback_index), workstream.id),
        )
    )


def _raw_workstream_ids(raw_workstreams: dict[str, Any]) -> tuple[str, ...]:
    raw_entries = raw_workstreams.get("workstreams")
    if not isinstance(raw_entries, list):
        return ()
    ids: list[str] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        workstream_id = entry.get("id")
        if isinstance(workstream_id, str):
            normalized = workstream_id.strip()
            if normalized:
                ids.append(normalized)
    return tuple(ids)


def load_program(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Program | None:
    program_path = programs_root / program_id / "program.yaml"
    if not program_path.exists():
        return None
    return _parse_program(load_yaml_mapping(program_path), program_path)


def list_editions_for_program(
    program_id: str,
    *,
    editions_root: Path | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[str, ...]:
    program_dir = _program_dir_for_reference(program_id, programs_root=programs_root)
    edition_dir = program_dir / "editions"
    edition_paths = sorted(edition_dir.glob("*.yaml")) if edition_dir.exists() else []
    # Fallback to a caller-supplied flat ``editions_root`` only when the program's
    # canonical editions directory has no editions (legacy/test layout).
    if not edition_paths and editions_root is not None and editions_root.exists():
        edition_paths = sorted(editions_root.glob("*.yaml"))
    edition_ids: list[str] = []
    for edition_path in edition_paths:
        if is_example_edition_id(edition_path.stem):
            continue
        raw_edition = load_yaml_mapping(edition_path)
        raw_program_id = str(raw_edition.get("program_id", "")).strip()
        if _normalized_program_id(raw_program_id) != program_id:
            continue
        edition_id = str(raw_edition.get("id") or edition_path.stem).strip()
        if edition_id:
            edition_ids.append(edition_id)
    return tuple(edition_ids)


def find_edition_yaml(edition_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    direct_programs = sorted(programs_root.glob(f"*/editions/{edition_id}.yaml"))
    if direct_programs:
        return direct_programs[0]
    template_programs = sorted(programs_root.glob(f"_templates/*/editions/{edition_id}.yaml"))
    if template_programs:
        return template_programs[0]
    # Legacy flat-editions fallback: check sibling editions/ folder (backward compatibility).
    legacy_path = programs_root.parent / "editions" / f"{edition_id}.yaml"
    if legacy_path.exists():
        return legacy_path
    return programs_root / "_missing" / "editions" / f"{edition_id}.yaml"


def _normalized_program_id(program_id: str) -> str:
    if program_id.startswith(_TEMPLATE_PROGRAM_PREFIX):
        return program_id.removeprefix(_TEMPLATE_PROGRAM_PREFIX).strip()
    return program_id.strip()


def _program_dir_for_reference(program_id: str, *, programs_root: Path) -> Path:
    normalized = _normalized_program_id(program_id)
    if program_id.startswith(_TEMPLATE_PROGRAM_PREFIX):
        return programs_root / "_templates" / normalized
    return programs_root / normalized


def resolve_area_paths(
    edition: EditionConfig,
    workstreams: tuple[Workstream, ...],
) -> tuple[str, ...]:
    if edition.ado and isinstance(edition.ado.get("area_paths"), list):
        return tuple(str(path) for path in edition.ado["area_paths"] if str(path).strip())

    filtered_workstreams = filter_workstreams(workstreams, edition.workstream_filter)
    area_paths = sorted({area_path for workstream in filtered_workstreams for area_path in workstream.area_paths})
    return tuple(area_paths)


def filter_workstreams(
    workstreams: tuple[Workstream, ...],
    workstream_filter: WorkstreamFilter | None,
) -> tuple[Workstream, ...]:
    if workstream_filter is None or workstream_filter.mode == "all" or not workstream_filter.workstream_ids:
        return workstreams
    selected = set(workstream_filter.workstream_ids)
    if workstream_filter.mode == "include":
        return tuple(workstream for workstream in workstreams if workstream.id in selected)
    if workstream_filter.mode == "exclude":
        return tuple(workstream for workstream in workstreams if workstream.id not in selected)
    raise ConfigError(f"Unsupported workstream_filter mode: {workstream_filter.mode}")


def _parse_edition_config(raw_edition: dict[str, Any], path: Path) -> EditionConfig:
    return EditionConfig(
        schema_version=str(raw_edition.get("schema_version", "2.0")),
        id=_required_string(raw_edition.get("id"), path, "id"),
        program_id=_required_string(raw_edition.get("program_id"), path, "program_id"),
        name=_required_string(raw_edition.get("name"), path, "name"),
        type=_required_string(raw_edition.get("type"), path, "type"),
        altitude=_required_string(raw_edition.get("altitude"), path, "altitude"),
        cadence=_required_string(raw_edition.get("cadence"), path, "cadence"),
        send_day=_optional_string(raw_edition.get("send_day")),
        send_time_local=_optional_string(raw_edition.get("send_time_local")),
        timezone=_optional_string(raw_edition.get("timezone")),
        author=_parse_author(raw_edition.get("author")),
        distribution=_parse_distribution(raw_edition.get("distribution")),
        ado=_mapping_or_none(raw_edition.get("ado")),
        ai=_mapping_or_none(raw_edition.get("ai")),
        kusto=_mapping_or_none(raw_edition.get("kusto")),
        m365=_mapping_or_none(raw_edition.get("m365")),
        workstream_filter=_parse_workstream_filter(raw_edition.get("workstream_filter")),
        brand_name=_optional_string(raw_edition.get("brand_name")),
        brand_header_url=_optional_string(raw_edition.get("brand_header_url")),
        scope_note=_optional_string(raw_edition.get("scope_note")),
        scorecard_sort=str(raw_edition.get("scorecard_sort", "risk_desc")),
        scorecard_plain_text_only=bool(raw_edition.get("scorecard_plain_text_only", False)),
        layout_mode=str(raw_edition.get("layout_mode", "dashboard")),
        cadence_note=raw_edition.get("cadence_note") if isinstance(raw_edition.get("cadence_note"), dict) else None,
        ado_fetch_timeout_seconds=_optional_int(raw_edition.get("ado_fetch_timeout_seconds")),
        forecast_enabled=bool(raw_edition.get("forecast_enabled", False)),
        mobile_safe_scorecards=_optional_string(raw_edition.get("mobile_safe_scorecards")),
        type_scale_v2=bool(raw_edition.get("type_scale_v2", False)),
        calibration_pilot=bool(raw_edition.get("calibration_pilot", False)),
        audience_scope_ids=tuple(str(scope_id) for scope_id in (raw_edition.get("audience_scope_ids") or ())),
    )


def _parse_program(raw_program: dict[str, Any], path: Path) -> Program:
    return Program(
        schema_version=str(raw_program.get("schema_version", "2.0")),
        id=_required_string(raw_program.get("id"), path, "id"),
        name=_required_string(raw_program.get("name"), path, "name"),
        chapter_namespace=_optional_string(raw_program.get("chapter_namespace")),
        maturity_level=_bounded_int(raw_program.get("maturity_level", 0), path, "maturity_level", minimum=0, maximum=4),
        objective=_optional_string(raw_program.get("objective")),
        mission=_optional_string(raw_program.get("mission")),
        current_phase=_optional_string(raw_program.get("current_phase")),
        pillars=_string_tuple(raw_program.get("pillars", [])),
        glossary=_mapping_of_strings(raw_program.get("glossary")),
        leadership_readers=_parse_leadership_readers(raw_program.get("leadership_readers")),
        writing_style=_parse_writing_style(raw_program.get("writing_style")),
        tone_calibration=_parse_tone_calibration(raw_program.get("tone_calibration")),
        key_dependencies=_parse_dependencies(raw_program.get("key_dependencies")),
        author_defaults=_parse_author(raw_program.get("author_defaults")),
        distribution_defaults=_parse_distribution(raw_program.get("distribution_defaults")),
        ado=_parse_ado(raw_program.get("ado")),
        ai=_parse_ai(raw_program.get("ai")),
        kusto=_parse_kusto(raw_program.get("kusto")),
        m365=_parse_m365(raw_program.get("m365")),
        source_confidence_order=_parse_source_confidence_order(raw_program.get("source_confidence_order"), path),
        storage_backend=_parse_storage_backend(raw_program.get("storage_backend"), path),
        expected_gather_cadence_hours=_parse_expected_gather_cadence_hours(raw_program, path),
        golden_queries=_string_tuple(raw_program.get("golden_queries", [])),
        min_channel_completeness_pct=_parse_min_channel_completeness_pct(raw_program, path),
        backfill_max_days=_parse_backfill_max_days(raw_program, path),
        gather=_parse_gather_activation_config(raw_program.get("gather"), path),
    )


def _parse_gather_activation_config(value: Any, path: Path) -> GatherActivationConfig:
    if value is None:
        return GatherActivationConfig()
    if not isinstance(value, dict):
        raise ConfigError(f"gather must be a mapping in {path}")

    raw_mode = value.get("run_manifest_mode", "shadow")
    if not isinstance(raw_mode, str) or raw_mode.strip().lower() not in {"off", "shadow", "enforce"}:
        raise ConfigError(f"gather.run_manifest_mode must be off, shadow, or enforce in {path}")
    run_manifest_mode = raw_mode.strip().lower()
    raw_scope_source = value.get("committed_scope_source", "gather_run")
    if not isinstance(raw_scope_source, str) or raw_scope_source.strip().lower() != "gather_run":
        raise ConfigError(f"gather.committed_scope_source must be gather_run in {path}")

    cadence = _positive_gather_int(value.get("full_discovery_cadence_hours", 24), field_name="gather.full_discovery_cadence_hours", path=path)
    warn = _positive_gather_int(value.get("freshness_warn_hours", 30), field_name="gather.freshness_warn_hours", path=path)
    block = _positive_gather_int(value.get("freshness_block_hours", 48), field_name="gather.freshness_block_hours", path=path)
    if block < warn:
        raise ConfigError(f"gather.freshness_block_hours must be >= gather.freshness_warn_hours in {path}")
    return GatherActivationConfig(
        run_manifest_mode=cast(Literal["off", "shadow", "enforce"], run_manifest_mode),
        committed_scope_source="gather_run",
        full_discovery_cadence_hours=cadence,
        freshness_warn_hours=warn,
        freshness_block_hours=block,
    )


def _positive_gather_int(value: Any, *, field_name: str, path: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{field_name} must be a positive integer in {path}")
    return value


def _parse_min_channel_completeness_pct(raw_program: dict[str, Any], path: Path) -> int:
    gather_config = raw_program.get("gather")
    value = None
    if isinstance(gather_config, dict) and "min_completeness_pct" in gather_config:
        value = gather_config.get("min_completeness_pct")
    elif "min_channel_completeness_pct" in raw_program:
        value = raw_program.get("min_channel_completeness_pct")
    if value is None:
        return 80
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"gather.min_completeness_pct must be an integer in {path}")
    if value < 0 or value > 100:
        raise ConfigError(f"gather.min_completeness_pct must be between 0 and 100 in {path}")
    return value


def _parse_backfill_max_days(raw_program: dict[str, Any], path: Path) -> int:
    gather_config = raw_program.get("gather")
    value = None
    if isinstance(gather_config, dict) and "backfill_max_days" in gather_config:
        value = gather_config.get("backfill_max_days")
    elif "backfill_max_days" in raw_program:
        value = raw_program.get("backfill_max_days")
    if value is None:
        return 14
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"gather.backfill_max_days must be an integer in {path}")
    if value <= 0:
        raise ConfigError(f"gather.backfill_max_days must be > 0 in {path}")
    return value


def _parse_expected_gather_cadence_hours(raw_program: dict[str, Any], path: Path) -> float | None:
    reality_config = raw_program.get("reality")
    value = raw_program.get("expected_gather_cadence_hours")
    if isinstance(reality_config, dict) and "expected_gather_cadence_hours" in reality_config:
        value = reality_config.get("expected_gather_cadence_hours")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"reality.expected_gather_cadence_hours must be numeric in {path}")
    parsed = float(value)
    if parsed <= 0:
        raise ConfigError(f"reality.expected_gather_cadence_hours must be > 0 in {path}")
    return parsed


def _parse_storage_backend(value: Any, path: Path) -> str:
    if value is None:
        return "file"
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"storage_backend must be a non-empty string in {path}")
    normalized = value.strip().lower()
    if normalized not in {"file", "sqlite"}:
        raise ConfigError(f"Unsupported storage_backend '{value}' in {path}; expected 'file' or 'sqlite'")
    return normalized


def _parse_source_confidence_order(value: Any, path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"source_confidence_order must be a list in {path}")

    allowed = {"ado", "icm", "kusto", "workiq", "ai"}
    parsed: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(f"source_confidence_order entries must be non-empty strings in {path}")
        normalized = entry.strip().lower()
        if normalized not in allowed:
            raise ConfigError(
                f"Unsupported source_confidence_order entry '{entry}' in {path}; expected one of ado, icm, kusto, workiq, ai"
            )
        if normalized not in parsed:
            parsed.append(normalized)
    return tuple(parsed)


def _parse_teams_meeting_series(value: Any, path: Path) -> tuple[TeamsMeetingSeries, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"signal_sources.teams_meeting_series must be a list in {path}")
    parsed: list[TeamsMeetingSeries] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        parsed.append(
            TeamsMeetingSeries(
                display_name=_required_string(entry.get("display_name"), path, "signal_sources.teams_meeting_series[].display_name"),
                series_id=_optional_string(entry.get("series_id")),
                include_transcripts=bool(entry.get("include_transcripts", True)),
                work_item_ids=_parse_work_item_ids(
                    entry.get("work_item_ids"),
                    path,
                    "signal_sources.teams_meeting_series[].work_item_ids",
                ),
                calendar_name=_optional_string(entry.get("calendar_name")),
                vpn_required=bool(entry.get("vpn_required", False)),
            )
        )
    return tuple(parsed)


def _parse_teams_chats(value: Any, path: Path) -> tuple[TeamsChat, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"signal_sources.teams_chats must be a list in {path}")
    parsed: list[TeamsChat] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        parsed.append(
            TeamsChat(
                display_name=_required_string(entry.get("display_name"), path, "signal_sources.teams_chats[].display_name"),
                thread_id=_optional_string(entry.get("thread_id")),
                work_item_ids=_parse_work_item_ids(
                    entry.get("work_item_ids"),
                    path,
                    "signal_sources.teams_chats[].work_item_ids",
                ),
            )
        )
    return tuple(parsed)


def _parse_email_threads(value: Any, path: Path) -> tuple[EmailThreadSource, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"signal_sources.email_threads must be a list in {path}")
    parsed: list[EmailThreadSource] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        parsed.append(
            EmailThreadSource(
                display_name=_required_string(entry.get("display_name"), path, "signal_sources.email_threads[].display_name"),
                thread_id=_required_string(entry.get("thread_id"), path, "signal_sources.email_threads[].thread_id"),
                work_item_ids=_parse_work_item_ids(
                    entry.get("work_item_ids"),
                    path,
                    "signal_sources.email_threads[].work_item_ids",
                ),
            )
        )
    return tuple(parsed)


def _parse_ado_coverage_requirement(value: Any, path: Path) -> ADOCoverageRequirement | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError(f"signal_sources.ado_coverage must be a mapping in {path}")
    min_ado_count = value.get("min_ado_count", 3)
    if not isinstance(min_ado_count, int):
        raise ConfigError(f"signal_sources.ado_coverage.min_ado_count must be an integer in {path}")
    return ADOCoverageRequirement(
        min_ado_count=min_ado_count,
        required_work_item_types=_string_tuple(value.get("required_work_item_types", [])),
        suppress_coverage_alert=bool(value.get("suppress_coverage_alert", False)),
    )


def _parse_dependency_ado_queries(value: Any, path: Path) -> tuple[DependencyADOQuery, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"signal_sources.dependency_ado_queries must be a list in {path}")

    parsed: list[DependencyADOQuery] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ConfigError(f"signal_sources.dependency_ado_queries[] must be a mapping in {path}")
        area_path = _optional_string(entry.get("area_path"))
        work_item_ids = _parse_work_item_ids(
            entry.get("work_item_ids"),
            path,
            "signal_sources.dependency_ado_queries[].work_item_ids",
        )
        if area_path is None and not work_item_ids:
            raise ConfigError(
                f"signal_sources.dependency_ado_queries[] must set area_path or work_item_ids in {path}"
            )
        parsed.append(
            DependencyADOQuery(
                label=_required_string(entry.get("label"), path, "signal_sources.dependency_ado_queries[].label"),
                resolution_path=_required_string(
                    entry.get("resolution_path"),
                    path,
                    "signal_sources.dependency_ado_queries[].resolution_path",
                ),
                area_path=area_path,
                work_item_ids=work_item_ids,
            )
        )
    return tuple(parsed)


def _parse_work_item_ids(value: Any, path: Path, field_name: str) -> tuple[int, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{field_name} must be a list in {path}")
    parsed: list[int] = []
    for item in value:
        if not isinstance(item, int):
            raise ConfigError(f"{field_name}[] must be integers in {path}")
        parsed.append(item)
    return tuple(parsed)


def _parse_signal_sources(value: Any, path: Path) -> WorkstreamSignalSources | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError(f"signal_sources must be a mapping in {path}")
    return WorkstreamSignalSources(
        teams_meeting_series=_parse_teams_meeting_series(value.get("teams_meeting_series"), path),
        teams_chats=_parse_teams_chats(value.get("teams_chats"), path),
        email_subject_filters=_string_tuple(value.get("email_subject_filters", [])),
        workiq_keywords=_string_tuple(value.get("workiq_keywords", [])),
        kusto_query_ids=_string_tuple(value.get("kusto_query_ids", [])),
        ado_coverage=_parse_ado_coverage_requirement(value.get("ado_coverage"), path),
        workiq_exclude_keywords=_string_tuple(value.get("workiq_exclude_keywords", [])),
        workiq_discovery_mode=_parse_optional_workiq_discovery_mode(value.get("workiq_discovery_mode"), path=path),
        workiq_discovery_union_runs=_parse_optional_bounded_int(
            value.get("workiq_discovery_union_runs"),
            field_name="signal_sources.workiq_discovery_union_runs",
            minimum=1,
            maximum=5,
            path=path,
        ),
        workiq_discovery_lookback_days=_parse_optional_bounded_int(
            value.get("workiq_discovery_lookback_days"),
            field_name="signal_sources.workiq_discovery_lookback_days",
            minimum=1,
            maximum=90,
            path=path,
        ),
        email_threads=_parse_email_threads(value.get("email_threads"), path),
        dependency_ado_queries=_parse_dependency_ado_queries(value.get("dependency_ado_queries"), path),
    )


def _parse_scorecards(raw_scorecards: dict[str, Any], path: Path) -> tuple[Scorecard, ...]:
    scorecards_payload = raw_scorecards.get("scorecards", [])
    if not isinstance(scorecards_payload, list):
        raise ConfigError(f"scorecards must be a list in {path}")
    return tuple(
        Scorecard(
            name=_required_string(scorecard.get("name"), path, "scorecards[].name"),
            dimensions=tuple(
                ScorecardDimension(
                    name=_required_string(dimension.get("name"), path, "scorecards[].dimensions[].name"),
                    workstream_id=_required_string(dimension.get("workstream_id"), path, "scorecards[].dimensions[].workstream_id"),
                    description=_optional_string(dimension.get("description")),
                    ado_filter=_optional_string(dimension.get("ado_filter")),
                    slice_contract_ref=_optional_string(dimension.get("slice_contract_ref")),
                    linked_scorecard_name=_optional_string(dimension.get("linked_scorecard")),
                    linked_dimension_name=_optional_string(dimension.get("linked_dimension")),
                )
                for dimension in scorecard.get("dimensions", [])
                if isinstance(dimension, dict)
            ),
        )
        for scorecard in scorecards_payload
        if isinstance(scorecard, dict)
    )


def _parse_author(value: Any) -> AuthorConfig | None:
    if not isinstance(value, dict):
        return None
    display_name = _optional_string(value.get("display_name"))
    email = _optional_string(value.get("email"))
    if display_name is None or email is None:
        return None
    return AuthorConfig(display_name=display_name, email=email)


def _parse_distribution(value: Any) -> DistributionConfig | None:
    if not isinstance(value, dict):
        return None
    to = _string_tuple(value.get("to", []))
    if not to:
        return None
    return DistributionConfig(
        to=to,
        cc=_string_tuple(value.get("cc", [])),
        channels=_string_tuple(value.get("channels", [])),
    )


def _parse_ado(value: Any) -> ADOConfig | None:
    if not isinstance(value, dict):
        return None
    required = ("organization", "project", "area_paths", "work_item_types", "date_window_days")
    if any(key not in value for key in required):
        return None
    return ADOConfig(
        organization=str(value["organization"]),
        project=str(value["project"]),
        area_paths=_string_tuple(value.get("area_paths", [])),
        work_item_types=_string_tuple(value.get("work_item_types", [])),
        excluded_states=_string_tuple(value.get("excluded_states", [])),
        date_window_days=int(value.get("date_window_days", 14)),
        api_timeout_seconds=int(value.get("api_timeout_seconds", 30)),
        proposal_ttl_hours=int(value.get("proposal_ttl_hours", 72)),
        required_tags=_string_tuple(value.get("required_tags", [])),
    )


def _parse_ai(value: Any) -> AIConfig | None:
    if not isinstance(value, dict):
        return None
    if "enabled" not in value:
        return None
    return AIConfig(
        enabled=bool(value.get("enabled", False)),
        budget_usd_per_run=float(value.get("budget_usd_per_run", 0.5)),
        blurb_deployment=_optional_string(value.get("blurb_deployment")),
        blurb_backup_deployment=_optional_string(value.get("blurb_backup_deployment")),
        exec_summary_deployment=_optional_string(value.get("exec_summary_deployment")),
        exec_summary_backup_deployment=_optional_string(value.get("exec_summary_backup_deployment")),
        temperature=float(value["temperature"]) if value.get("temperature") is not None else None,
        requests_per_minute=int(value["requests_per_minute"]) if value.get("requests_per_minute") is not None else None,
    )


def _parse_kusto(value: Any) -> KustoConfig | None:
    if not isinstance(value, dict):
        return None
    queries_payload = value.get("queries", [])
    queries = tuple(
        KustoQuery(
            id=str(entry.get("id", "")),
            cluster=str(entry.get("cluster", "")),
            database=str(entry.get("database", "")),
            kql=str(entry.get("kql", "")),
            section=str(entry.get("section", entry.get("id", ""))),
            render_as=str(entry.get("render_as", "table")),
            confidence=str(entry.get("confidence", "medium")),
            reference_url=_optional_string(entry.get("reference_url")),
            caveats=_string_tuple(entry.get("caveats", [])),
            kusto_section_validates_slice=bool(entry.get("kusto_section_validates_slice", False)),
            program_ids=_string_tuple(entry.get("program_ids", [])),
            workstream_ids=_string_tuple(entry.get("workstream_ids", [])),
            validated=_legacy_validated_flag(entry),
            refresh_on_gather=bool(entry.get("refresh_on_gather", False)),
            label=_optional_string(entry.get("label")),
            result_column=_optional_string(entry.get("result_column")),
        )
        for entry in queries_payload
        if isinstance(entry, dict)
    )
    raw_max_concurrency = value.get("max_concurrency", 1)
    try:
        max_concurrency = max(1, int(raw_max_concurrency))
    except (TypeError, ValueError):
        max_concurrency = 1
    return KustoConfig(enabled=bool(value.get("enabled", False)), queries=queries, max_concurrency=max_concurrency)


def _parse_m365(value: Any) -> M365Config | None:
    if not isinstance(value, dict):
        return None
    workiq_queries = value.get("workiq_queries") if isinstance(value.get("workiq_queries"), dict) else None
    if workiq_queries is None and isinstance(value.get("workiq"), dict):
        workiq_queries = {
            str(key): str(item)
            for key, item in value["workiq"].items()
            if isinstance(item, (str, int, float))
        }
    retrieval = _parse_workiq_retrieval(value.get("retrieval"))
    rev = _parse_rev_profile(value.get("rev"))
    return M365Config(
        enabled=bool(value.get("enabled", False)),
        prefer_agency=bool(value.get("prefer_agency", True)),
        workiq_queries={str(key): str(item) for key, item in workiq_queries.items()} if workiq_queries else None,
        icm_incidents_url=_optional_string(value.get("icm_incidents_url")),
        workiq_enrich_schedule=_optional_string(value.get("workiq_enrich_schedule")),
        retrieval=retrieval,
        rev=rev,
    )


def _parse_workiq_retrieval(value: Any) -> WorkIQRetrievalConfig | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError("m365.retrieval must be a mapping")
    mode = _parse_optional_workiq_discovery_mode(value.get("discovery_mode")) or "legacy_nl"
    union_runs = _parse_optional_bounded_int(
        value.get("discovery_union_runs", 1),
        field_name="m365.retrieval.discovery_union_runs",
        minimum=1,
        maximum=5,
    )
    lookback_days = _parse_optional_bounded_int(
        value.get("discovery_lookback_days", 14),
        field_name="m365.retrieval.discovery_lookback_days",
        minimum=1,
        maximum=90,
    )
    return WorkIQRetrievalConfig(
        discovery_mode=mode,
        discovery_union_runs=union_runs or 1,
        discovery_lookback_days=lookback_days or 14,
        per_thread_extraction=_parse_bool(value.get("per_thread_extraction", False), "m365.retrieval.per_thread_extraction"),
        per_thread_top_k=_parse_optional_bounded_int(
            value.get("per_thread_top_k", 3),
            field_name="m365.retrieval.per_thread_top_k",
            minimum=1,
            maximum=10,
        ) or 3,
        per_thread_one_hop=_parse_bool(value.get("per_thread_one_hop", False), "m365.retrieval.per_thread_one_hop"),
        max_calls_per_cycle=_parse_optional_bounded_int(
            value.get("max_calls_per_cycle", 40),
            field_name="m365.retrieval.max_calls_per_cycle",
            minimum=1,
            maximum=200,
        ) or 40,
        max_wall_clock_seconds=_parse_optional_bounded_int(
            value.get("max_wall_clock_seconds", 600),
            field_name="m365.retrieval.max_wall_clock_seconds",
            minimum=30,
            maximum=7200,
        ) or 600,
    )


def _parse_optional_workiq_discovery_mode(value: Any, *, path: Path | None = None) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or value.strip().lower() not in WORKIQ_DISCOVERY_MODES:
        location = f" in {path}" if path is not None else ""
        expected = ", ".join(sorted(WORKIQ_DISCOVERY_MODES))
        raise ConfigError(f"Unsupported WorkIQ discovery mode {value!r}{location}; expected one of {expected}")
    return value.strip().lower()


def _parse_optional_enum(value: Any, *, allowed: frozenset[str], field_name: str, default: str, path: Path | None = None) -> str:
    if value in (None, ""):
        return default
    if not isinstance(value, str) or value.strip().lower() not in allowed:
        location = f" in {path}" if path is not None else ""
        expected = ", ".join(sorted(allowed))
        raise ConfigError(f"Unsupported {field_name} {value!r}{location}; expected one of {expected}")
    return value.strip().lower()


def _parse_rev_budgets(value: Any, *, path: Path | None = None) -> RevBudgets:
    if value is None:
        return RevBudgets()
    if not isinstance(value, dict):
        raise ConfigError("m365.rev.budgets must be a mapping")

    # ``RevBudgets`` field defaults are read by name so the parser never drifts
    # from the dataclass defaults.
    defaults = RevBudgets()
    return RevBudgets(
        max_search_requests_total_per_cycle=_parse_optional_bounded_int(
            value.get("max_search_requests_total_per_cycle", defaults.max_search_requests_total_per_cycle),
            field_name="m365.rev.budgets.max_search_requests_total_per_cycle", minimum=1, maximum=1000, path=path)
        or defaults.max_search_requests_total_per_cycle,
        max_search_requests_per_entity_per_cycle=_parse_optional_bounded_int(
            value.get("max_search_requests_per_entity_per_cycle", defaults.max_search_requests_per_entity_per_cycle),
            field_name="m365.rev.budgets.max_search_requests_per_entity_per_cycle", minimum=1, maximum=500, path=path)
        or defaults.max_search_requests_per_entity_per_cycle,
        max_hydrated_bytes_per_cycle=_parse_optional_bounded_int(
            value.get("max_hydrated_bytes_per_cycle", defaults.max_hydrated_bytes_per_cycle),
            field_name="m365.rev.budgets.max_hydrated_bytes_per_cycle", minimum=1024, maximum=1_073_741_824, path=path)
        or defaults.max_hydrated_bytes_per_cycle,
        max_hydrated_bytes_per_item=_parse_optional_bounded_int(
            value.get("max_hydrated_bytes_per_item", defaults.max_hydrated_bytes_per_item),
            field_name="m365.rev.budgets.max_hydrated_bytes_per_item", minimum=1024, maximum=67_108_864, path=path)
        or defaults.max_hydrated_bytes_per_item,
        max_chunk_count_per_cycle=_parse_optional_bounded_int(
            value.get("max_chunk_count_per_cycle", defaults.max_chunk_count_per_cycle),
            field_name="m365.rev.budgets.max_chunk_count_per_cycle", minimum=1, maximum=10_000, path=path)
        or defaults.max_chunk_count_per_cycle,
        max_chunk_count_per_item=_parse_optional_bounded_int(
            value.get("max_chunk_count_per_item", defaults.max_chunk_count_per_item),
            field_name="m365.rev.budgets.max_chunk_count_per_item", minimum=1, maximum=2000, path=path)
        or defaults.max_chunk_count_per_item,
        max_model_tokens_in_per_cycle=_parse_optional_bounded_int(
            value.get("max_model_tokens_in_per_cycle", defaults.max_model_tokens_in_per_cycle),
            field_name="m365.rev.budgets.max_model_tokens_in_per_cycle", minimum=1000, maximum=50_000_000, path=path)
        or defaults.max_model_tokens_in_per_cycle,
        max_model_tokens_out_per_cycle=_parse_optional_bounded_int(
            value.get("max_model_tokens_out_per_cycle", defaults.max_model_tokens_out_per_cycle),
            field_name="m365.rev.budgets.max_model_tokens_out_per_cycle", minimum=1000, maximum=10_000_000, path=path)
        or defaults.max_model_tokens_out_per_cycle,
        max_content_safety_requests_per_cycle=_parse_optional_bounded_int(
            value.get("max_content_safety_requests_per_cycle", defaults.max_content_safety_requests_per_cycle),
            field_name="m365.rev.budgets.max_content_safety_requests_per_cycle", minimum=0, maximum=10_000, path=path)
        or defaults.max_content_safety_requests_per_cycle,
        max_monetized_spend_per_cycle_usd=_parse_rev_spend(
            value.get("max_monetized_spend_per_cycle_usd"), defaults=defaults.max_monetized_spend_per_cycle_usd,
            field_name="m365.rev.budgets.max_monetized_spend_per_cycle_usd", path=path),
        max_wall_clock_seconds=_parse_optional_bounded_int(
            value.get("max_wall_clock_seconds", defaults.max_wall_clock_seconds),
            field_name="m365.rev.budgets.max_wall_clock_seconds", minimum=30, maximum=7200, path=path)
        or defaults.max_wall_clock_seconds,
        concurrency_per_provider=_parse_optional_bounded_int(
            value.get("concurrency_per_provider", defaults.concurrency_per_provider),
            field_name="m365.rev.budgets.concurrency_per_provider", minimum=1, maximum=64, path=path)
        or defaults.concurrency_per_provider,
        fleet_concurrency_cap=_parse_optional_bounded_int(
            value.get("fleet_concurrency_cap", defaults.fleet_concurrency_cap),
            field_name="m365.rev.budgets.fleet_concurrency_cap", minimum=1, maximum=256, path=path)
        or defaults.fleet_concurrency_cap,
        per_lane_share=_parse_optional_enum(
            value.get("per_lane_share", defaults.per_lane_share), allowed=frozenset({"equal", "weighted"}),
            field_name="m365.rev.budgets.per_lane_share", default=defaults.per_lane_share, path=path),
    )


def _parse_rev_profile(value: Any, *, path: Path | None = None) -> RevRetrievalProfile | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError("m365.rev must be a mapping")
    location = f" in {path}" if path is not None else ""
    profile = _parse_optional_enum(
        value.get("profile", REV_PROFILE_LEGACY_NL), allowed=REV_PROFILES,
        field_name="m365.rev.profile", default=REV_PROFILE_LEGACY_NL, path=path)
    auth_scope_tier = _parse_optional_enum(
        value.get("auth_scope_tier", "personal_comms_mail"), allowed=REV_AUTH_SCOPE_TIERS,
        field_name="m365.rev.auth_scope_tier", default="personal_comms_mail", path=path)
    fallback_policy = _parse_optional_enum(
        value.get("fallback_policy", "fail_visible"), allowed=frozenset({"fail_visible", "allow_legacy"}),
        field_name="m365.rev.fallback_policy", default="fail_visible", path=path)
    evidence_policy = _parse_optional_enum(
        value.get("evidence_policy", REV_EVIDENCE_EXCERPT_VAULTED), allowed=REV_EVIDENCE_POLICIES,
        field_name="m365.rev.evidence_policy", default=REV_EVIDENCE_EXCERPT_VAULTED, path=path)
    hydration_fallback = _parse_optional_enum(
        value.get("hydration_fallback", REV_HYDRATION_DROP), allowed=REV_HYDRATION_POLICIES,
        field_name="m365.rev.hydration_fallback", default=REV_HYDRATION_DROP, path=path)
    structured_outputs = _parse_optional_enum(
        value.get("structured_outputs", REV_STRUCTURED_OUTPUTS_PROBE), allowed=REV_STRUCTURED_OUTPUTS_MODES,
        field_name="m365.rev.structured_outputs", default=REV_STRUCTURED_OUTPUTS_PROBE, path=path)
    groundedness = _parse_optional_enum(
        value.get("groundedness", REV_GROUNDEDNESS_OFF), allowed=REV_GROUNDEDNESS_MODES,
        field_name="m365.rev.groundedness", default=REV_GROUNDEDNESS_OFF, path=path)
    # Reject unsupported combinations (§5.1).
    if profile == "rev_verified" and evidence_policy == REV_EVIDENCE_METADATA_ONLY:
        raise ConfigError(
            f"m365.rev unsupported combination: profile=rev_verified requires evidence_policy=excerpt_vaulted{location}")
    if groundedness == "gate":
        raise ConfigError(
            f"m365.rev.groundedness=gate is not permitted until RV calibration completes{location}")
    defaults = RevRetrievalProfile()
    budgets = _parse_rev_budgets(value.get("budgets"), path=path)
    return RevRetrievalProfile(
        profile=profile,
        auth_scope_tier=auth_scope_tier,
        fallback_policy=fallback_policy,
        evidence_policy=evidence_policy,
        hydration_fallback=hydration_fallback,
        structured_outputs=structured_outputs,
        groundedness=groundedness,
        budgets=budgets,
        normalization_version=str(value.get("normalization_version", defaults.normalization_version)),
        scrubber_version=str(value.get("scrubber_version", defaults.scrubber_version)),
        chunking_version=str(value.get("chunking_version", defaults.chunking_version)),
        injection_policy_version=str(value.get("injection_policy_version", defaults.injection_policy_version)),
        extraction_policy_version=str(value.get("extraction_policy_version", defaults.extraction_policy_version)),
        content_safety_policy_version=str(value.get("content_safety_policy_version", defaults.content_safety_policy_version)),
        human_materiality_policy_version=str(value.get("human_materiality_policy_version", defaults.human_materiality_policy_version)),
        orphan_ttl_days=_parse_optional_bounded_int(
            value.get("orphan_ttl_days", defaults.orphan_ttl_days),
            field_name="m365.rev.orphan_ttl_days", minimum=1, maximum=365, path=path) or defaults.orphan_ttl_days,
        rejected_review_retention_days=_parse_optional_bounded_int(
            value.get("rejected_review_retention_days", defaults.rejected_review_retention_days),
            field_name="m365.rev.rejected_review_retention_days", minimum=1, maximum=365, path=path) or defaults.rejected_review_retention_days,
        pending_grace_days=_parse_optional_bounded_int(
            value.get("pending_grace_days", defaults.pending_grace_days),
            field_name="m365.rev.pending_grace_days", minimum=0, maximum=365, path=path) or defaults.pending_grace_days,
        fact_bridge_enabled=bool(value.get("fact_bridge_enabled", True)),
    )


def _parse_optional_bounded_int(
    value: Any,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
    path: Path | None = None,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        location = f" in {path}" if path is not None else ""
        raise ConfigError(f"{field_name} must be an integer from {minimum} through {maximum}{location}")
    return value


def _parse_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{field_name} must be a boolean")
    return value


def _parse_optional_bounded_float(
    value: Any,
    *,
    field_name: str,
    minimum: float,
    maximum: float,
    path: Path | None = None,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= float(value) <= maximum:
        location = f" in {path}" if path is not None else ""
        raise ConfigError(f"{field_name} must be a number from {minimum} through {maximum}{location}")
    return float(value)


def _parse_rev_spend(
    value: Any,
    *,
    defaults: float,
    field_name: str,
    path: Path | None = None,
) -> float:
    """Parse ``m365.rev.budgets.max_monetized_spend_per_cycle_usd`` to a ``float``.

    Unlike ``_parse_optional_bounded_float`` this always returns ``float`` (the
    default when the value is absent) so the field's non-optional type holds.
    """
    if value is None:
        return defaults
    parsed = _parse_optional_bounded_float(value, field_name=field_name, minimum=0.0, maximum=1000.0, path=path)
    return parsed if parsed is not None else defaults


def _legacy_validated_flag(entry: dict[str, Any]) -> bool:
    if "validated" not in entry:
        return True
    return bool(entry.get("validated", False))


def _parse_leadership_readers(value: Any) -> tuple[LeadershipReader, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        LeadershipReader(
            name=str(entry.get("name", "")).strip(),
            role=_optional_string(entry.get("role")),
            cares_about=_string_tuple(entry.get("cares_about", [])),
            prefers=_optional_string(entry.get("prefers")),
            pet_peeves=_string_tuple(entry.get("pet_peeves", [])),
        )
        for entry in value
        if isinstance(entry, dict) and str(entry.get("name", "")).strip()
    )


def _parse_writing_style(value: Any) -> WritingStyle | None:
    if not isinstance(value, dict):
        return None
    if not any(value.get(key) for key in ("voice", "structure", "risk_framing", "preferred_patterns")):
        return None
    risk_framing = value.get("risk_framing") if isinstance(value.get("risk_framing"), dict) else None
    return WritingStyle(
        voice=_optional_string(value.get("voice")),
        structure=_optional_string(value.get("structure")),
        risk_framing={str(key): str(item) for key, item in risk_framing.items()} if risk_framing else None,
        preferred_patterns=_string_tuple(value.get("preferred_patterns", [])),
    )


def _parse_tone_calibration(value: Any) -> ToneCalibration | None:
    if not isinstance(value, dict):
        return None
    overrides = value.get("per_theme_override") if isinstance(value.get("per_theme_override"), dict) else None
    if _optional_string(value.get("overall")) is None and not overrides:
        return None
    return ToneCalibration(
        overall=_optional_string(value.get("overall")),
        per_theme_override={str(key): str(item) for key, item in overrides.items()} if overrides else None,
    )


def _parse_dependencies(value: Any) -> tuple[LegacyDependency, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        LegacyDependency(
            from_item=str(entry.get("from_item") or entry.get("from") or "").strip(),
            to_item=str(entry.get("to_item") or entry.get("to") or "").strip(),
            impact=str(entry.get("impact", "")).strip(),
        )
        for entry in value
        if isinstance(entry, dict)
        and str(entry.get("from_item") or entry.get("from") or "").strip()
        and str(entry.get("to_item") or entry.get("to") or "").strip()
        and str(entry.get("impact", "")).strip()
    )


def _parse_workstream_filter(value: Any) -> WorkstreamFilter | None:
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise ConfigError("workstream_filter must be a mapping when present")
    raw_mode = str(value.get("mode", "all"))
    if raw_mode not in ("all", "include", "exclude"):
        raise ConfigError(f"workstream_filter mode must be 'all', 'include', or 'exclude'; got '{raw_mode}'")
    return WorkstreamFilter(
        mode=cast(Literal["all", "include", "exclude"], raw_mode),
        workstream_ids=_string_tuple(value.get("workstream_ids", [])),
    )


def _mapping_or_none(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _mapping_of_strings(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): str(item) for key, item in value.items() if str(key).strip()}


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(entry).strip() for entry in value if str(entry).strip())


def _optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _optional_date(value: Any, path: Path, field_name: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ConfigError(f"{field_name} must be an ISO date string in {path}")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ConfigError(f"{field_name} must be an ISO date string in {path}") from error


def _bounded_int(
    value: Any,
    path: Path,
    field_name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{field_name} must be an integer in {path}") from error
    if resolved < minimum or resolved > maximum:
        raise ConfigError(f"{field_name} must be between {minimum} and {maximum} in {path}")
    return resolved


def _required_string(value: Any, path: Path, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field_name} is required in {path}")
    return value.strip()
