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

from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import Path
import re
from typing import Any

import typer
import yaml

from src.core.edition_resolver import find_edition_yaml, get_program_output_dir, PROGRAMS_ROOT
from src.core.draft_artifact_store import load_draft_state as load_optional_draft_state
from src.core.draft_artifact_store import load_draft_manifest as load_optional_draft_manifest
from src.core.exceptions import ConfigError, StateError
from src.core.manifest_writer import load_run_evidence_snapshot
from src.core.models import ReviewSection, ReviewState, ReviewStatus, RunEvidenceSnapshot
from src.core.overrides_store import OverridesDocument, load_overrides, merge_overrides
from src.core.review_status_store import load_review_status


@dataclass(frozen=True, slots=True)
class ConfirmDraftManifest:
    edition_name: str
    issue_number: int
    programs_root: Path
    manifest_id: str
    config_hash: str
    ai_safety: dict[str, Any] | None
    draft_readiness: dict[str, Any] | None
    milestone_assessments: Any
    milestone_trajectory_snapshot: Any
    run_evidence_snapshot: RunEvidenceSnapshot | None = None


def load_confirm_draft_manifest(
    edition_name: str, issue_number: int, *, draft_state: Mapping[str, Any], programs_root: Path = PROGRAMS_ROOT,
) -> ConfirmDraftManifest:
    payload = _load_manifest(edition_name, issue_number, programs_root=programs_root)
    manifest_id = _require_manifest_id(payload, edition_name, issue_number, programs_root=programs_root)
    config_hash = payload.get("config_hash") if payload is not None else None
    if not isinstance(config_hash, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", config_hash) is None:
        raise typer.BadParameter(
            f"Invalid or missing draft config hash for edition {edition_name!r}, issue {issue_number:03d}. "
            "Preserve the existing artifacts and re-run `vertex report --dry-run` before confirming."
        )
    try:
        capture = load_run_evidence_snapshot(payload or {}, draft_state)
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    return ConfirmDraftManifest(
        edition_name=edition_name,
        issue_number=issue_number,
        programs_root=programs_root.absolute(),
        manifest_id=manifest_id,
        config_hash=config_hash,
        run_evidence_snapshot=capture,
        ai_safety=_project_draft_metadata_field(payload, "ai_safety", edition_name, issue_number),
        draft_readiness=_project_draft_metadata_field(payload, "draft_readiness", edition_name, issue_number),
        milestone_assessments=_project_draft_metadata_value(
            payload, "milestone_assessments", edition_name, issue_number,
        ),
        milestone_trajectory_snapshot=_project_draft_metadata_value(
            payload, "milestone_trajectory_snapshot", edition_name, issue_number,
        ),
    )


def load_confirm_overrides(
    edition_name: str,
    issue_number: int,
    bundle,
    reports_root: Path,
    *,
    programs_root: Path | None = None,
) -> OverridesDocument:
    existing = load_overrides(
        edition_name,
        reports_root=reports_root,
        issue_number=issue_number,
        **({"programs_root": programs_root} if programs_root is not None else {}),
    )
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
    return _load_draft_metadata_field(
        "draft_readiness", edition_name=edition_name, issue_number=issue_number, programs_root=programs_root,
    )


def _load_manifest(
    edition_name: str, issue_number: int, *, programs_root: Path,
) -> dict[str, Any] | None:
    try:
        return load_optional_draft_manifest(edition_name, issue_number, programs_root=programs_root)
    except StateError as error:
        raise typer.BadParameter(str(error)) from error


def _load_draft_metadata_field(
    field: str, *, edition_name: str, issue_number: int, programs_root: Path,
) -> dict[str, Any] | None:
    payload = _load_manifest(edition_name, issue_number, programs_root=programs_root)
    return _project_draft_metadata_field(payload, field, edition_name, issue_number)


def _project_draft_metadata_field(
    payload: dict[str, Any] | None, field: str, edition_name: str, issue_number: int,
) -> dict[str, Any] | None:
    if payload is None:
        return None
    metadata = payload.get("metadata")
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise typer.BadParameter(
            f"Invalid draft manifest metadata for edition {edition_name!r}, issue {issue_number:03d}: "
            "expected a JSON object. Preserve the existing artifact and review it before retrying."
        )
    value = metadata.get(field)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise typer.BadParameter(
            f"Invalid draft manifest {field} metadata for edition {edition_name!r}, issue {issue_number:03d}: "
            "expected a JSON object. Preserve the existing artifact and review it before retrying."
        )
    return dict(value)


def _project_draft_metadata_value(
    payload: dict[str, Any] | None, field: str, edition_name: str, issue_number: int,
) -> Any:
    if payload is None:
        return None
    metadata = payload.get("metadata")
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise typer.BadParameter(
            f"Invalid draft manifest metadata for edition {edition_name!r}, issue {issue_number:03d}: "
            "expected a JSON object. Preserve the existing artifact and review it before retrying."
        )
    return metadata.get(field)


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
        program_path = programs_root / program_id / "program.yaml"
        program_document = load_optional_yaml_mapping(program_path) or {}
        readiness_document = program_document.get("readiness")
        if readiness_document is not None:
            if not isinstance(readiness_document, dict):
                raise typer.BadParameter(
                    f"Invalid readiness settings in {program_path}: expected a mapping. "
                    "Preserve the file and review it before retrying."
                )
            program_enabled = _readiness_gate_enabled(readiness_document, "gate", program_path)
            snapshot_max_age_days = _readiness_age(readiness_document, "snapshot_max_age_days", program_path)

    edition_yaml_path = editions_root / f"{edition_name}.yaml"
    edition_document = load_optional_yaml_mapping(edition_yaml_path)
    if edition_document is None:
        edition_yaml_path = find_edition_yaml(edition_name, programs_root=programs_root)
        edition_document = load_optional_yaml_mapping(edition_yaml_path) or {}
    edition_enabled = _readiness_gate_enabled(edition_document, "readiness_gate", edition_yaml_path)
    if snapshot_max_age_days is None:
        snapshot_max_age_days = _readiness_age(edition_document, "readiness_snapshot_max_age_days", edition_yaml_path)

    return program_enabled or edition_enabled, snapshot_max_age_days


def load_optional_yaml_mapping(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise typer.BadParameter(
            f"Cannot read readiness settings at {path} ({type(error).__name__}). "
            "Preserve the file; check access and UTF-8 before retrying."
        ) from error
    try:
        document = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError) as error:
        raise typer.BadParameter(
            f"Cannot parse readiness settings at {path} ({type(error).__name__}). "
            "Preserve the file; check YAML syntax and values before retrying."
        ) from error
    if not isinstance(document, dict):
        raise typer.BadParameter(
            f"Invalid readiness settings at {path}: expected a YAML mapping. "
            "Preserve the file and review it before retrying."
        )
    return document


def _readiness_gate_enabled(document: dict[str, Any], field: str, path: Path) -> bool:
    value = document.get(field)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise typer.BadParameter(
            f"Invalid readiness setting {field} in {path}: expected a boolean. "
            "Preserve the file and review it before retrying."
        )
    return value


def _readiness_age(document: dict[str, Any], field: str, path: Path) -> int | None:
    value = document.get(field)
    age = coerce_optional_int(value)
    if value is not None and age is None:
        raise typer.BadParameter(
            f"Invalid readiness setting {field} in {path}: expected an integer age in days. "
            "Preserve the file and review it before retrying."
        )
    return age


def coerce_optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def load_draft_ai_safety_metadata(
    *,
    edition_name: str,
    issue_number: int,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, Any] | None:
    return _load_draft_metadata_field(
        "ai_safety", edition_name=edition_name, issue_number=issue_number, programs_root=programs_root,
    )


def load_confirm_review_status(
    edition_name: str,
    issue_number: int,
    workstream_section_ids: tuple[str, ...],
    reports_root: Path,
    *,
    programs_root: Path | None = None,
) -> ReviewStatus:
    expected_section_ids = ("exec_summary",) + tuple(f"ws:{section_id}" for section_id in workstream_section_ids)
    existing = load_review_status(
        edition_name,
        reports_root=reports_root,
        **({"programs_root": programs_root} if programs_root is not None else {}),
    )
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
    try:
        payload = load_optional_draft_state(edition_name, issue_number, programs_root=programs_root)
    except StateError as error:
        raise typer.BadParameter(str(error)) from error
    if payload is None:
        path = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.draft.json"
        raise typer.BadParameter(
            f"Draft state not found at {path}. Run `vertex report --dry-run --edition {edition_name} --issue {issue_number}` first."
        )
    return payload


def load_current_draft_manifest_id(edition_name: str, issue_number: int, *, programs_root: Path = PROGRAMS_ROOT) -> str:
    payload = _load_manifest(edition_name, issue_number, programs_root=programs_root)
    return _require_manifest_id(payload, edition_name, issue_number, programs_root=programs_root)


def _require_manifest_id(
    payload: dict[str, Any] | None, edition_name: str, issue_number: int, *, programs_root: Path,
) -> str:
    if payload is None:
        path = get_program_output_dir(edition_name, programs_root=programs_root) / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.manifest.json"
        raise typer.BadParameter(
            f"Manifest not found at {path}. Run `vertex report --dry-run --edition {edition_name} --issue {issue_number}` first."
        )
    manifest_id = payload.get("manifest_id")
    if not isinstance(manifest_id, str) or not manifest_id.strip():
        raise typer.BadParameter(
            f"Invalid draft manifest for edition {edition_name!r}, issue {issue_number:03d}: "
            "manifest_id must be a nonblank string. Preserve the existing artifact and review it before retrying."
        )
    return manifest_id
