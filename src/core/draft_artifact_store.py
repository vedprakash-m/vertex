from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.core.edition_resolver import get_program_output_dir
from src.core.exceptions import StateError
from src.core.workspace_paths import PROGRAMS_ROOT


def load_draft_state(
    edition_name: str, issue_number: int, *, programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, Any] | None:
    path = (
        get_program_output_dir(edition_name, programs_root=programs_root)
        / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.draft.json"
    )
    scope = f"draft state for edition {edition_name!r}, issue {issue_number:03d}"
    return _read_json_object(path, scope)


def load_draft_manifest(
    edition_name: str, issue_number: int, *, programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, Any] | None:
    path = (
        get_program_output_dir(edition_name, programs_root=programs_root)
        / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.manifest.json"
    )
    scope = f"draft manifest for edition {edition_name!r}, issue {issue_number:03d}"
    return _read_json_object(path, scope)


def _read_json_object(path: Path, scope: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StateError(
            f"Cannot read {scope} ({type(error).__name__}). "
            "Preserve the existing artifact; check access, UTF-8 and JSON validity before retrying."
        ) from error
    if not isinstance(payload, dict):
        raise StateError(
            f"Invalid {scope}: expected a JSON object. Preserve the existing artifact and review it before retrying."
        )
    return payload
