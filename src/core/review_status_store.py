from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.core.edition_resolver import get_program_output_dir, resolve_edition_paths
from src.core.models import ReviewSection, ReviewState, ReviewStatus
from src.core.snapshot_store import ARCHIVE_ROOT, get_archive_root


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_ROOT = REPO_ROOT / "reports"


def get_review_status_path(edition: str, reports_root: Path = REPORTS_ROOT) -> Path:
    resolved_paths = resolve_edition_paths(
        edition,
        programs_root=reports_root.parent / "programs",
    )
    if resolved_paths is not None:
        return get_program_output_dir(edition, programs_root=reports_root.parent / "programs") / "review_status.yaml"
    return reports_root / edition / "review_status.yaml"


def load_review_status(
    edition: str,
    reports_root: Path = REPORTS_ROOT,
) -> ReviewStatus | None:
    path = get_review_status_path(edition, reports_root)
    if not path.exists():
        return None
    raw_document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw_document, dict):
        raise TypeError("review status document must be a mapping")
    raw_sections = raw_document.get("sections")
    if not isinstance(raw_sections, list):
        raise TypeError("sections must be a list")
    return ReviewStatus(
        issue_number=_required_int(raw_document.get("issue_number"), field_name="issue_number"),
        sections=tuple(
            ReviewSection(
                section_id=_required_string(_require_mapping(section).get("section_id"), field_name="section_id"),
                state=ReviewState.from_string(_required_string(_require_mapping(section).get("state"), field_name="state")),
                reviewer=_optional_string(_require_mapping(section).get("reviewer"), field_name="reviewer"),
                note=_optional_string(_require_mapping(section).get("note"), field_name="note"),
                updated_at=_parse_datetime(_require_mapping(section).get("updated_at")),
                manifest_id=_optional_string(_require_mapping(section).get("manifest_id"), field_name="manifest_id"),
            )
            for section in raw_sections
        ),
    )


def save_review_status(
    edition: str,
    review_status: ReviewStatus,
    reports_root: Path = REPORTS_ROOT,
) -> Path:
    path = get_review_status_path(edition, reports_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "issue_number": review_status.issue_number,
        "sections": [_serialize_section(section) for section in review_status.sections],
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def archive_review_status(
    edition: str,
    issue_number: int,
    review_status: ReviewStatus,
    archive_root: Path = ARCHIVE_ROOT,
) -> Path:
    archive_path = get_archive_root(edition, archive_root) / "review" / f"issue_{issue_number:03d}.review.yaml"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "issue_number": review_status.issue_number,
        "sections": [_serialize_section(section) for section in review_status.sections],
    }
    archive_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return archive_path


def reset_review_status(
    edition: str,
    issue_number: int,
    section_ids: tuple[str, ...],
    reports_root: Path = REPORTS_ROOT,
) -> ReviewStatus:
    review_status = ReviewStatus(
        issue_number=issue_number,
        sections=tuple(
            ReviewSection(
                section_id=section_id,
                state=ReviewState.PENDING,
                reviewer=None,
                note=None,
                updated_at=None,
                manifest_id=None,
            )
            for section_id in section_ids
        ),
    )
    save_review_status(edition, review_status, reports_root)
    return review_status


def _serialize_section(section: ReviewSection) -> dict[str, str | None]:
    return {
        "section_id": section.section_id,
        "state": section.state.value,
        "reviewer": section.reviewer,
        "note": section.note,
        "updated_at": _serialize_datetime(section.updated_at),
        "manifest_id": section.manifest_id,
    }


def _optional_string(value: Any, *, field_name: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _required_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    return value


def _required_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _require_aware_datetime(value)
    if not isinstance(value, str):
        raise TypeError("updated_at must be a string")
    return _require_aware_datetime(datetime.fromisoformat(value))


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _require_aware_datetime(value).isoformat()


def _require_aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("updated_at must include timezone information")
    return value.astimezone(timezone.utc)


def _require_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("sections entries must be mappings")
    return value
