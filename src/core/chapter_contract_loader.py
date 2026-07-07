from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from src.core.exceptions import ConfigError


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_ROOT = REPO_ROOT / "reports"
_ALLOWED_EDITIONS = {"condensed", "deck", "detailed", "focused", "lookback"}
_DIMENSION_ID_OVERRIDES = {
    "Scenarios / STG Sign-Off": "scenarios_stg_signoff",
    "Repairs & Safety": "repairs_safety",
}


@dataclass(frozen=True, slots=True)
class ChapterDefinition:
    id: str
    title: str
    priority: int
    include_in: tuple[str, ...]
    show_in_jump_list: bool
    dimensions: tuple[str, ...]
    subtitle: str | None = None
    chapter_owner: str | None = None
    chapter_exempt: bool = False
    include_low_risk_dimensions: bool = True


@dataclass(frozen=True, slots=True)
class ChapterContract:
    schema_version: str
    chapters: tuple[ChapterDefinition, ...]
    dimension_lookup: dict[str, tuple[str, str]]
    unmapped_dimensions: tuple[str, ...] = ()

    def chapters_for(self, edition_type: str) -> tuple[ChapterDefinition, ...]:
        return tuple(
            sorted(
                (
                    chapter
                    for chapter in self.chapters
                    if edition_type in chapter.include_in
                ),
                key=lambda chapter: chapter.priority,
            )
        )

    def resolve_dimension(self, dimension_id: str) -> tuple[str, str] | None:
        return self.dimension_lookup.get(dimension_id)


def get_chapter_contract_path(edition_name: str, reports_root: Path = REPORTS_ROOT) -> Path:
    return reports_root / edition_name / "chapter_contract.yaml"


def load_chapter_contract_for_edition(
    edition_name: str,
    *,
    scorecards: Iterable[tuple[str, tuple[str, ...]]],
    reports_root: Path = REPORTS_ROOT,
    chapter_namespace: str | None = None,
) -> ChapterContract | None:
    path = get_chapter_contract_path(edition_name, reports_root=reports_root)
    if not path.exists():
        return None
    return load_chapter_contract(path, scorecards=tuple(scorecards), chapter_namespace=chapter_namespace)


def load_chapter_contract(
    path: Path,
    *,
    scorecards: tuple[tuple[str, tuple[str, ...]], ...],
    chapter_namespace: str | None = None,
) -> ChapterContract:
    with path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    if document.get("schema_version") != "1.0":
        raise ConfigError(f"Unsupported chapter contract schema version in {path}")

    raw_chapters = document.get("chapters")
    if not isinstance(raw_chapters, list) or not raw_chapters:
        raise ConfigError(f"chapters must be a non-empty list in {path}")

    dimension_lookup = _build_dimension_lookup(scorecards, chapter_namespace=chapter_namespace)
    chapters: list[ChapterDefinition] = []
    chapter_ids: set[str] = set()
    priorities: set[int] = set()
    referenced_dimensions: set[str] = set()
    for raw_chapter in raw_chapters:
        if not isinstance(raw_chapter, dict):
            raise ConfigError(f"Each chapter entry must be a mapping in {path}")

        chapter_id = _required_string(raw_chapter.get("id"), path, "chapters[].id")
        if chapter_id in chapter_ids:
            raise ConfigError(f"Duplicate chapter id {chapter_id!r} in {path}")
        chapter_ids.add(chapter_id)

        priority = raw_chapter.get("priority")
        if not isinstance(priority, int) or priority < 1:
            raise ConfigError(f"chapters[].priority must be a positive integer in {path}")
        if priority in priorities:
            raise ConfigError(f"Duplicate chapter priority {priority} in {path}")
        priorities.add(priority)

        include_in = _parse_include_in(raw_chapter.get("include_in"), path)
        dimensions = _parse_dimensions(raw_chapter.get("dimensions"), path)
        for dimension_id in dimensions:
            if dimension_id not in dimension_lookup:
                raise ConfigError(
                    f"Unknown chapter dimension {dimension_id!r} in {path}. "
                    f"Add the dimension to config.yaml scorecards or fix the namespaced id."
                )
            referenced_dimensions.add(dimension_id)

        chapters.append(
            ChapterDefinition(
                id=chapter_id,
                title=_required_string(raw_chapter.get("title"), path, f"chapters[{chapter_id}].title"),
                priority=priority,
                include_in=include_in,
                show_in_jump_list=_parse_bool(raw_chapter.get("show_in_jump_list"), default=True, path=path, field_name=f"chapters[{chapter_id}].show_in_jump_list"),
                dimensions=dimensions,
                subtitle=_optional_string(raw_chapter.get("subtitle")),
                chapter_owner=_optional_string(raw_chapter.get("chapter_owner")),
                chapter_exempt=_parse_bool(raw_chapter.get("chapter_exempt"), default=False, path=path, field_name=f"chapters[{chapter_id}].chapter_exempt"),
                include_low_risk_dimensions=_parse_bool(raw_chapter.get("include_low_risk_dimensions"), default=True, path=path, field_name=f"chapters[{chapter_id}].include_low_risk_dimensions"),
            )
        )

    unmapped_dimensions = tuple(sorted(set(dimension_lookup) - referenced_dimensions))
    return ChapterContract(
        schema_version="1.0",
        chapters=tuple(sorted(chapters, key=lambda chapter: chapter.priority)),
        dimension_lookup=dimension_lookup,
        unmapped_dimensions=unmapped_dimensions,
    )


def _build_dimension_lookup(
    scorecards: tuple[tuple[str, tuple[str, ...]], ...],
    *,
    chapter_namespace: str | None = None,
) -> dict[str, tuple[str, str]]:
    lookup: dict[str, tuple[str, str]] = {}
    for scorecard_name, dimension_names in scorecards:
        for dimension_name in dimension_names:
            key = canonical_dimension_binding_id(
                scorecard_name,
                dimension_name,
                chapter_namespace=chapter_namespace,
            )
            lookup[key] = (scorecard_name, dimension_name)
    return lookup


def canonical_dimension_binding_id(
    scorecard_name: str,
    dimension_name: str,
    *,
    chapter_namespace: str | None = None,
) -> str:
    return f"{_scorecard_prefix(scorecard_name, chapter_namespace=chapter_namespace)}.{_dimension_id(dimension_name)}"


def _scorecard_prefix(scorecard_name: str, *, chapter_namespace: str | None = None) -> str:
    # Returns the namespace prefix used in chapter dimension IDs.
    # Programs may explicitly configure a chapter namespace; otherwise the scorecard slug is used.
    scorecard_slug = _slug_identifier(scorecard_name)
    namespace_slug = _slug_identifier(chapter_namespace) if chapter_namespace else ""
    if namespace_slug and (scorecard_slug == namespace_slug or scorecard_slug.startswith(f"{namespace_slug}_")):
        return namespace_slug
    if scorecard_slug.startswith("dd_pf_") or scorecard_slug.startswith("dd_on_pf_"):
        return "dd"
    return scorecard_slug


def _dimension_id(dimension_name: str) -> str:
    override = _DIMENSION_ID_OVERRIDES.get(dimension_name)
    if override is not None:
        return override
    return _slug_identifier(dimension_name)


def _slug_identifier(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower())
    return normalized.strip("_")


def _required_string(value: object, path: Path, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field_name} must be a non-empty string in {path}")
    return value.strip()


def _optional_string(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip() or None


def _parse_include_in(value: object, path: Path) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"chapters[].include_in must be a non-empty list in {path}")
    editions: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or entry.strip() not in _ALLOWED_EDITIONS:
            allowed = ", ".join(sorted(_ALLOWED_EDITIONS))
            raise ConfigError(f"chapters[].include_in entries must be one of {allowed} in {path}")
        editions.append(entry.strip())
    if len(set(editions)) != len(editions):
        raise ConfigError(f"chapters[].include_in contains duplicate entries in {path}")
    return tuple(editions)


def _parse_dimensions(value: object, path: Path) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"chapters[].dimensions must be a list in {path}")
    dimensions: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(f"chapters[].dimensions entries must be non-empty strings in {path}")
        dimensions.append(entry.strip())
    if len(set(dimensions)) != len(dimensions):
        raise ConfigError(f"chapters[].dimensions contains duplicate entries in {path}")
    return tuple(dimensions)


def _parse_bool(value: object, *, default: bool, path: Path, field_name: str) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{field_name} must be a boolean in {path}")