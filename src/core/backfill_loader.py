from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.edition_resolver import resolve_edition_paths
from src.core.exceptions import ConfigError
from src.core.yaml_utils import load_yaml_mapping


REPO_ROOT = Path(__file__).resolve().parents[2]
_ALLOWED_SEARCH_STRATEGIES = {"offline", "m365", "hybrid"}


@dataclass(frozen=True, slots=True)
class BackfillSourceDefinition:
    kind: str
    glob: str


@dataclass(frozen=True, slots=True)
class BackfillExtractSettings:
    workstream_blurbs: bool
    scorecard_dimensions: bool


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    sources: tuple[BackfillSourceDefinition, ...]
    extract: BackfillExtractSettings
    output: str | None
    newsletter_source_categories: tuple[str, ...]  # source kinds treated as newsletter inputs for AI extraction


@dataclass(frozen=True, slots=True)
class BackfillDirection:
    question: str | None
    source: str | None
    filter: str | None
    date_range: str | None
    description: str | None
    extract: str | None


@dataclass(frozen=True, slots=True)
class BackfillDirectionGroup:
    search_strategy: str | None
    directions: tuple[BackfillDirection, ...]


@dataclass(frozen=True, slots=True)
class BackfillConfig:
    newsletters: BackfillDirectionGroup
    feedback: BackfillDirectionGroup
    meetings: BackfillDirectionGroup
    people_intelligence: BackfillDirectionGroup


def get_backfill_plan_path(edition_name: str, repo_root: Path = REPO_ROOT) -> Path:
    return _resolve_backfill_program_dir(edition_name, repo_root=repo_root) / "backfill.yaml"


def get_backfill_config_path(edition_name: str, repo_root: Path = REPO_ROOT) -> Path:
    return _resolve_backfill_program_dir(edition_name, repo_root=repo_root) / "backfill_config.yaml"


def load_backfill_plan_for_edition(
    edition_name: str,
    repo_root: Path = REPO_ROOT,
) -> BackfillPlan | None:
    path = get_backfill_plan_path(edition_name, repo_root=repo_root)
    if not path.exists():
        return None
    return load_backfill_plan(path)


def load_backfill_config_for_edition(
    edition_name: str,
    repo_root: Path = REPO_ROOT,
) -> BackfillConfig | None:
    path = get_backfill_config_path(edition_name, repo_root=repo_root)
    if not path.exists():
        return None
    return load_backfill_config(path)


def _resolve_backfill_program_dir(edition_name: str, *, repo_root: Path) -> Path:
    resolved_paths = resolve_edition_paths(
        edition_name,
        programs_root=repo_root / "programs",
    )
    if resolved_paths is None:
        raise ConfigError(f"Unknown edition: {edition_name}")
    return resolved_paths.program_dir


def load_backfill_plan(path: Path) -> BackfillPlan:
    document = load_yaml_mapping(path)
    raw_sources = document.get("sources", [])
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigError(f"sources must be a non-empty list in {path}")
    sources: list[BackfillSourceDefinition] = []
    for index, raw_source in enumerate(raw_sources):
        if not isinstance(raw_source, dict):
            raise ConfigError(f"sources[{index}] must be a mapping in {path}")
        sources.append(
            BackfillSourceDefinition(
                kind=_require_non_empty_string(raw_source.get("kind"), path, f"sources[{index}].kind"),
                glob=_require_non_empty_string(raw_source.get("glob"), path, f"sources[{index}].glob"),
            )
        )
    raw_extract = document.get("extract", {})
    if raw_extract is None:
        raw_extract = {}
    if not isinstance(raw_extract, dict):
        raise ConfigError(f"extract must be a mapping in {path}")
    raw_newsletter_cats = document.get("newsletter_source_categories")
    if raw_newsletter_cats is None:
        # Derive from sources: any kind that is not a known non-newsletter category.
        _NON_NEWSLETTER_KINDS = frozenset({"lt_decks", "transcripts", "chats", "reviews", "meetings", "feedback", "people_intelligence"})
        newsletter_source_categories: tuple[str, ...] = tuple(
            s.kind for s in sources if s.kind not in _NON_NEWSLETTER_KINDS
        )
    elif isinstance(raw_newsletter_cats, list):
        newsletter_source_categories = tuple(str(c) for c in raw_newsletter_cats if c)
    else:
        raise ConfigError(f"newsletter_source_categories must be a list in {path}")

    return BackfillPlan(
        sources=tuple(sources),
        extract=BackfillExtractSettings(
            workstream_blurbs=bool(raw_extract.get("workstream_blurbs", False)),
            scorecard_dimensions=bool(raw_extract.get("scorecard_dimensions", False)),
        ),
        output=_optional_string(document.get("output")),
        newsletter_source_categories=newsletter_source_categories,
    )


def load_backfill_config(path: Path) -> BackfillConfig:
    document = load_yaml_mapping(path)
    return BackfillConfig(
        newsletters=_parse_direction_group(document.get("newsletters"), path, "newsletters", allow_search_strategy=True),
        feedback=_parse_direction_group(document.get("feedback"), path, "feedback"),
        meetings=_parse_direction_group(document.get("meetings"), path, "meetings"),
        people_intelligence=_parse_direction_group(document.get("people_intelligence"), path, "people_intelligence"),
    )


def _parse_direction_group(
    raw_group: Any,
    path: Path,
    group_name: str,
    *,
    allow_search_strategy: bool = False,
) -> BackfillDirectionGroup:
    if raw_group is None:
        return BackfillDirectionGroup(search_strategy=None, directions=())

    search_strategy: str | None = None
    raw_directions: Any
    if isinstance(raw_group, list):
        raw_directions = raw_group
    elif isinstance(raw_group, dict):
        raw_directions = raw_group.get("directions", [])
        if allow_search_strategy:
            search_strategy = _optional_string(raw_group.get("search_strategy"))
            if search_strategy is not None:
                search_strategy = search_strategy.lower()
                if search_strategy not in _ALLOWED_SEARCH_STRATEGIES:
                    supported = ", ".join(sorted(_ALLOWED_SEARCH_STRATEGIES))
                    raise ConfigError(f"{group_name}.search_strategy must be one of {supported} in {path}")
    else:
        raise ConfigError(f"{group_name} must be a list or mapping in {path}")

    if not isinstance(raw_directions, list):
        raise ConfigError(f"{group_name}.directions must be a list in {path}")

    directions = tuple(
        _parse_direction(direction, path, f"{group_name}[{index}]")
        for index, direction in enumerate(raw_directions)
    )
    return BackfillDirectionGroup(search_strategy=search_strategy, directions=directions)


def _parse_direction(raw_direction: Any, path: Path, field_name: str) -> BackfillDirection:
    if not isinstance(raw_direction, dict):
        raise ConfigError(f"{field_name} must be a mapping in {path}")
    direction = BackfillDirection(
        question=_optional_string(raw_direction.get("question")),
        source=_optional_string(raw_direction.get("source")),
        filter=_optional_string(raw_direction.get("filter")),
        date_range=_optional_string(raw_direction.get("date_range")),
        description=_optional_string(raw_direction.get("description")),
        extract=_optional_string(raw_direction.get("extract")),
    )
    if direction.question is None and direction.filter is None:
        raise ConfigError(f"{field_name} must define either question or filter in {path}")
    return direction


def _require_non_empty_string(value: Any, path: Path, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field_name} must be a non-empty string in {path}")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
