"""Shared import-time program root; selection never creates or probes storage."""

from __future__ import annotations

import os
from pathlib import Path

from src.core.exceptions import ConfigError


REPO_ROOT = Path(__file__).resolve().parents[2]


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
