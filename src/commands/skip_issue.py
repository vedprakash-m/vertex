from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from src.core.edition_resolver import EDITIONS_ROOT, PROGRAMS_ROOT, resolve_edition
from src.core.archive_store import read_archive_index, write_skipped_issue
from src.core.program_fact_store import ProgramEvent, append_program_event
from src.core.snapshot_store import ARCHIVE_ROOT


def skip_issue(
    reason: str,
    edition_name: str | None = None,
    archive_root: Path | None = None,
    *,
    editions_root: Path = EDITIONS_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
    db_root: Path | None = None,
) -> tuple[str, int, Path]:
    resolved_edition = edition_name or _default_edition_name()
    resolved_archive_root = archive_root or ARCHIVE_ROOT
    resolved = resolve_edition(
        resolved_edition,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if resolved is None:
        raise ValueError(f"Edition '{resolved_edition}' could not be resolved.")
    archive_index = read_archive_index(resolved_edition, archive_root=resolved_archive_root)
    issue_number = _next_issue_number(archive_index)
    generated_at = datetime.now(timezone.utc)
    # Phase 6 §22 Step 7: write a `ProgramEvent` (fact_type="event.issue.skip")
    # to the Fact Store. The legacy `skip.issue` fact_type remains
    # readable for the migration window (see `project_skip_issues` and
    # `load_current_skip_issues`).
    append_program_event(
        resolved.program.id,
        ProgramEvent(
            fact_type="event.issue.skip",
            natural_key=f"skip:{resolved_edition}:{issue_number}",
            metadata={
                "edition_id": resolved_edition,
                "issue_number": issue_number,
                "generated_at": generated_at.isoformat(),
                "reason": reason,
            },
        ),
        recorded_at=generated_at,
        db_root=db_root,
    )
    index_path = write_skipped_issue(
        edition=resolved_edition,
        issue_number=issue_number,
        reason=reason,
        archive_root=resolved_archive_root,
        generated_at=generated_at,
    )
    return resolved_edition, issue_number, index_path


def _default_edition_name() -> str:
    return os.environ.get("VERTEX_DEFAULT_EDITION", "")


def _next_issue_number(index) -> int:
    if not index.issues:
        return 1
    return max(entry.issue_number for entry in index.issues) + 1