"""Shared import-time program root; selection never creates or probes storage."""

from __future__ import annotations

import os
from pathlib import Path

from src.core.exceptions import ConfigError


REPO_ROOT = Path(__file__).resolve().parents[2]
_PROGRAMS_ROOT_CONFIGURED = "VERTEX_PROGRAMS_ROOT" in os.environ


def _resolve_programs_root() -> Path:
    configured = os.environ.get("VERTEX_PROGRAMS_ROOT")
    if configured is None:
        return REPO_ROOT / "programs"
    if not configured.strip() or not configured.isprintable():
        raise ConfigError("VERTEX_PROGRAMS_ROOT must be a nonempty, printable absolute path.")
    root = Path(configured)
    if not root.is_absolute():
        raise ConfigError("VERTEX_PROGRAMS_ROOT must be an absolute path; relative or drive-relative roots are not supported.")
    return root


PROGRAMS_ROOT = _resolve_programs_root()


def validate_new_programs_root(programs_root: Path) -> Path:
    """Admit an explicit private root without creating or opening program state."""
    if not programs_root.is_absolute():
        raise ConfigError("New program workspace roots must be absolute paths.")
    try:
        root = programs_root.resolve()
        for candidate in (programs_root, root):
            if candidate == REPO_ROOT or REPO_ROOT in candidate.parents:
                raise ConfigError("New program workspace must be outside installation assets; configure VERTEX_PROGRAMS_ROOT.")
            if any((ancestor / ".git").exists() for ancestor in (candidate, *candidate.parents)):
                raise ConfigError("New program workspace must be outside every Git checkout, including linked paths.")
        storage = next((ancestor for ancestor in (root, *root.parents) if ancestor.exists()), None)
        if storage is None or not storage.is_dir() or not os.access(storage, os.R_OK | os.W_OK | os.X_OK):
            raise ConfigError("New program workspace requires accessible directory storage.")
    except (OSError, RuntimeError) as error:
        raise ConfigError("Cannot resolve or access the new program workspace.") from error
    return root


def resolve_report_workspace_roots(
    programs_root: Path | None = None,
    reports_root: Path | None = None,
    archive_root: Path | None = None,
) -> tuple[Path, Path, Path]:
    selected_programs_root = PROGRAMS_ROOT if programs_root is None else programs_root
    external = _PROGRAMS_ROOT_CONFIGURED or programs_root is not None
    if external:
        validated_programs_root = validate_new_programs_root(selected_programs_root)
        if selected_programs_root != validated_programs_root:
            selected_programs_root = validated_programs_root
    if reports_root is None:
        reports_root = selected_programs_root.parent / "reports" if external else REPO_ROOT / "reports"
    if archive_root is None:
        archive_root = selected_programs_root.parent / "archive" if external else REPO_ROOT / "archive"
    validate_report_workspace(
        programs_root=selected_programs_root, reports_root=reports_root, archive_root=archive_root,
    )
    return selected_programs_root, reports_root, archive_root


def is_bare_identifier(identifier: str) -> bool:
    return (
        bool(identifier.strip()) and identifier.isprintable() and identifier not in {".", ".."}
        and not identifier.endswith((" ", "."))
        and not any(character in identifier for character in "/\\:*?[]")
    )


def validate_report_workspace(*, programs_root: Path, reports_root: Path, archive_root: Path) -> None:
    if any(not root.is_absolute() for root in (programs_root, reports_root, archive_root)):
        raise ConfigError("Report workspace roots must be absolute paths.")
    if reports_root.parent / "programs" != programs_root or archive_root.parent / "programs" != programs_root:
        raise ConfigError(
            "Conflicting report workspace roots: legacy report/archive paths must select the same program root. "
            "Use program, report and archive roots from one declared workspace; automatic relocation is not supported."
        )
