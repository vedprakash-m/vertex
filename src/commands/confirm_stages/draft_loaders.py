"""Read-only draft/readiness state loaders for confirm.

Extracted from ``src/commands/confirm.py`` (D-25 / Phase 3). Every function here
*reads* persisted draft artifacts (draft JSON, manifest JSON, overrides, review
status, program/edition YAML) and returns in-memory objects. None of them write
state, which is why they are safe to lift out of the confirm transaction module
ahead of the write path. ``confirm.py`` imports the seven entry points it calls
under their historical private aliases; ``load_optional_yaml_mapping`` and
``coerce_optional_int`` are internal helpers for the readiness-gate loader.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
import yaml

from src.core.edition_resolver import find_edition_yaml, get_program_output_dir, PROGRAMS_ROOT
from src.core.models import ReviewSection, ReviewState, ReviewStatus
from src.core.overrides_store import OverridesDocument, load_overrides, merge_overrides
from src.core.review_status_store import load_review_status


def load_confirm_overrides(edition_name: str, issue_number: int, bundle, reports_root: Path) -> OverridesDocument:
    existing = load_overrides(edition_name, reports_root=reports_root, issue_number=issue_number)
    if existing is None:
        raise typer.BadParameter("overrides.yaml is missing. Run `vertex report --dry-run` first.")
    expected_scorecards = {
        scorecard.name: tuple(dimension.name for dimension in scorecard.dimensions)
        for scorecard in bundle.config.scorecards
    }
    merged, _ = merge_overrides(
        issue_number=issue_number,
        expected_scorecards=expected_scorecards,
        existing=existing,
    )
    return merged


def load_draft_readiness_metadata(
    *,
    edition_name: str,
    issue_number: int,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, Any] | None:
    manifest_path = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.manifest.json"
    if not manifest_path.exists():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    draft_readiness = metadata.get("draft_readiness")
    return dict(draft_readiness) if isinstance(draft_readiness, dict) else None


def load_readiness_gate_settings(
    *,
    edition_name: str,
    program_id: str | None,
    editions_root: Path,
    programs_root: Path,
) -> tuple[bool, int | None]:
    program_enabled = False
    snapshot_max_age_days: int | None = None

    if program_id is not None:
        program_document = load_optional_yaml_mapping(programs_root / program_id / "program.yaml")
        readiness_document = program_document.get("readiness")
        if isinstance(readiness_document, dict):
            program_enabled = bool(readiness_document.get("gate", False))
            snapshot_max_age_days = coerce_optional_int(readiness_document.get("snapshot_max_age_days"))

    edition_yaml_path = editions_root / f"{edition_name}.yaml"
    if not edition_yaml_path.exists():
        # Fallback to the programs tree when the legacy flat editions_root is empty
        # (editions now live under programs/<id>/editions/).
        candidate = find_edition_yaml(edition_name, programs_root=programs_root)
        if candidate.exists():
            edition_yaml_path = candidate
    edition_document = load_optional_yaml_mapping(edition_yaml_path)
    edition_enabled = bool(edition_document.get("readiness_gate", False))
    if snapshot_max_age_days is None:
        snapshot_max_age_days = coerce_optional_int(edition_document.get("readiness_snapshot_max_age_days"))

    return program_enabled or edition_enabled, snapshot_max_age_days


def load_optional_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return document if isinstance(document, dict) else {}


def coerce_optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def load_draft_ai_safety_metadata(
    *,
    edition_name: str,
    issue_number: int,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, Any] | None:
    manifest_path = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.manifest.json"
    if not manifest_path.exists():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    ai_safety = metadata.get("ai_safety")
    return dict(ai_safety) if isinstance(ai_safety, dict) else None


def load_confirm_review_status(
    edition_name: str,
    issue_number: int,
    workstream_section_ids: tuple[str, ...],
    reports_root: Path,
) -> ReviewStatus:
    expected_section_ids = ("exec_summary",) + tuple(f"ws:{section_id}" for section_id in workstream_section_ids)
    existing = load_review_status(edition_name, reports_root=reports_root)
    if existing is not None and existing.issue_number == issue_number:
        existing_sections = {section.section_id: section for section in existing.sections}
        return ReviewStatus(
            issue_number=issue_number,
            sections=tuple(
                existing_sections.get(
                    section_id,
                    ReviewSection(
                        section_id=section_id,
                        state=ReviewState.PENDING,
                        reviewer=None,
                        note=None,
                        updated_at=None,
                    ),
                )
                for section_id in expected_section_ids
            ),
        )
    return ReviewStatus(
        issue_number=issue_number,
        sections=tuple(
            ReviewSection(
                section_id=section_id,
                state=ReviewState.PENDING,
                reviewer=None,
                note=None,
                updated_at=None,
            )
            for section_id in expected_section_ids
        ),
    )


def load_draft_state(edition_name: str, issue_number: int, *, programs_root: Path = PROGRAMS_ROOT) -> dict[str, Any]:
    path = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.draft.json"
    if not path.exists():
        raise typer.BadParameter(
            f"Draft state not found at {path}. Run `vertex report --dry-run --edition {edition_name} --issue {issue_number}` first."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise typer.BadParameter(f"Draft state at {path} is invalid.") from error
    if not isinstance(payload, dict):
        raise typer.BadParameter(f"Draft state at {path} is invalid.")
    return payload


def load_current_draft_manifest_id(edition_name: str, issue_number: int, *, programs_root: Path = PROGRAMS_ROOT) -> str:
    path = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.manifest.json"
    if not path.exists():
        raise typer.BadParameter(
            f"Manifest not found at {path}. Run `vertex report --dry-run --edition {edition_name} --issue {issue_number}` first."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise typer.BadParameter(f"Manifest at {path} is invalid.") from error
    manifest_id = payload.get("manifest_id") if isinstance(payload, dict) else None
    if not manifest_id:
        raise typer.BadParameter(f"Manifest at {path} is invalid.")
    return str(manifest_id)
