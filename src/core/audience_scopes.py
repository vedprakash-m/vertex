"""specs/people.md §7.4, PPL-W5a.1: reusable, program-level audience
scopes -- the schema/loader layer only. Resolution into actual nudge
recipients is PPL-W5a.2's scope.

`audience_scopes.yaml` lives beside `workstream_registry.yaml` (same
per-program directory, same "authored config, loader resolves once"
convention `load_authored_workstream_registry` already established) --
a new dedicated file rather than a new `program.yaml` section, since
audience scopes are their own independently-versioned schema.

Every hand-authored `team_refs`/`include_people`/`exclude_people`
reference is resolved to its CURRENT canonical `entity_id` at LOAD time
via `people_namespace_bridge.resolve_ref_to_canonical_entity_id` -- the
exact same resolver `people_query.py::find_person`/`find_team` already
use, not a second ad hoc lookup. An unresolvable reference raises
`ConfigError` rather than being silently dropped: a silently-shrunk
audience scope is a governance/safety concern (someone who should be
notified quietly isn't), not a case to degrade gracefully past.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.core.exceptions import ConfigError
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.people_entity_schema import CanonicalEntity, EntityRedirect, load_entities_document
from src.core.people_namespace_bridge import resolve_ref_to_canonical_entity_id

AUDIENCE_SCOPES_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class AudienceScope:
    id: str
    team_entity_ids: tuple[str, ...] = ()
    membership_roles: tuple[str, ...] = ()
    require_verified_within_days: int | None = None
    include_person_entity_ids: tuple[str, ...] = ()
    exclude_person_entity_ids: tuple[str, ...] = ()
    allow_external_guests: bool = False


def audience_scopes_path_for_program(program_id: str, *, programs_root: Path) -> Path:
    return programs_root / program_id / "audience_scopes.yaml"


def load_audience_scopes(*, program_id: str, programs_root: Path) -> tuple[AudienceScope, ...]:
    """A missing file returns (). Malformed content or an unresolvable
    reference raises `ConfigError`."""
    path = audience_scopes_path_for_program(program_id, programs_root=programs_root)
    if not path.exists():
        return ()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    schema_version = str(raw.get("schema_version") or AUDIENCE_SCOPES_SCHEMA_VERSION)
    if schema_version.split(".", 1)[0] != AUDIENCE_SCOPES_SCHEMA_VERSION.split(".", 1)[0]:
        raise ConfigError(
            f"{path}: expected audience_scopes.yaml schema_version major "
            f"{AUDIENCE_SCOPES_SCHEMA_VERSION.split('.', 1)[0]}, got {schema_version!r}."
        )
    raw_scopes = raw.get("audience_scopes") or {}
    if not isinstance(raw_scopes, dict):
        raise ConfigError(f"{path}: 'audience_scopes' must be a mapping of scope id -> definition.")

    knowledge_root = get_shared_knowledge_root(programs_root)
    document = load_entities_document(knowledge_root / "entities.yaml")
    entities = document.entities if document is not None else ()
    redirects = document.redirects if document is not None else ()

    return tuple(
        _parse_scope(str(scope_id), raw_scope, path=path, entities=entities, redirects=redirects)
        for scope_id, raw_scope in raw_scopes.items()
    )


def find_audience_scope(scopes: tuple[AudienceScope, ...], scope_id: str) -> AudienceScope | None:
    return next((scope for scope in scopes if scope.id == scope_id), None)


def _resolve_refs(
    refs: Any, *, entities: tuple[CanonicalEntity, ...], redirects: tuple[EntityRedirect, ...],
    path: Path, scope_id: str, field_name: str,
) -> tuple[str, ...]:
    if refs is None:
        return ()
    if not isinstance(refs, list):
        raise ConfigError(f"{path}: audience scope {scope_id!r} {field_name!r} must be a list.")
    resolved: list[str] = []
    for ref in refs:
        resolution = resolve_ref_to_canonical_entity_id(str(ref), entities=entities, redirects=redirects)
        if resolution.canonical_entity_id is None:
            raise ConfigError(
                f"{path}: audience scope {scope_id!r} {field_name} entry {ref!r} does not resolve to a "
                "known canonical entity. Add it to the shared registry first, or correct the reference."
            )
        resolved.append(resolution.canonical_entity_id)
    return tuple(resolved)


def _parse_scope(
    scope_id: str, raw: Any, *, path: Path, entities: tuple[CanonicalEntity, ...], redirects: tuple[EntityRedirect, ...],
) -> AudienceScope:
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: audience scope {scope_id!r} must be a mapping.")
    require_verified_within_days = raw.get("require_verified_within_days")
    return AudienceScope(
        id=scope_id,
        team_entity_ids=_resolve_refs(raw.get("team_refs"), entities=entities, redirects=redirects, path=path, scope_id=scope_id, field_name="team_refs"),
        membership_roles=tuple(str(role) for role in (raw.get("membership_roles") or ())),
        require_verified_within_days=None if require_verified_within_days is None else int(require_verified_within_days),
        include_person_entity_ids=_resolve_refs(raw.get("include_people"), entities=entities, redirects=redirects, path=path, scope_id=scope_id, field_name="include_people"),
        exclude_person_entity_ids=_resolve_refs(raw.get("exclude_people"), entities=entities, redirects=redirects, path=path, scope_id=scope_id, field_name="exclude_people"),
        allow_external_guests=bool(raw.get("allow_external_guests", False)),
    )
