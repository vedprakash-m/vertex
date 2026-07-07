"""GAP-34: F4 Override→Fact Backfill / ``Judgment`` fact type.

The vision (Tech §9.16) names F1→F4: the F4 milestone is
**Override→Fact Backfill** — translating existing overrides YAML
(`programs/<id>/overrides/issue_NNN.yaml`) into ``judgment.dimension``
facts in the Program Fact Store. Once backfilled, the
``❓ Needs input`` treadmill is closed: the operator's dimensional
risk-level choices become auditable, queryable, and correlatable
facts (not free-text override files).

This module is **pure-Python** + side-effect-isolated:
  - `extract_judgments_from_overrides(overrides_path)` — parse a
    single override file into a tuple of `Judgment` data (no writes).
  - `backfill_judgments_from_overrides(...)` — write the extracted
    judgments into the Program Fact Store as ``judgment.dimension``
    facts (uses the canonical append path).
  - `backfill_program(...)` — convenience wrapper: find all
    overrides files under a program directory, extract, and backfill.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

from src.core.models_v2 import Judgment
from src.core.program_fact_store import append_program_event, ProgramEvent


# Statuses from the overrides file that should NOT become Judgment
# facts (they're explicit "not set yet" markers and would just be
# rejected back into the needs-input treadmill).
_NEEDS_INPUT_MARKERS = frozenset(
    {"❓ Needs input", "Needs input", "needs_input", "unknown", ""}
)

# Common derived "default" risk levels in the overrides file that
# mean "use the system-derived value" — backfilling them is fine but
# adds no information. We backfill them anyway for completeness.

# Edition id extraction from a file path like
#   programs/<prog>/overrides/issue_NNN.yaml
# or
#   programs/<prog>/archive/<edition>/overrides/issue_NNN.yaml
_OVERRIDES_PATH_RE = re.compile(r"[\\/]overrides[\\/](issue_(?P<num>\d+)\.yaml)$")


@dataclass(frozen=True, slots=True)
class OverrideExtraction:
    """Result of parsing a single overrides file."""

    issue_number: int
    edition_id: str
    judgments: tuple[Judgment, ...]


def _extract_edition_id(overrides_path: Path, program_id: str) -> str:
    """Best-effort edition id from the overrides file path."""
    parts = overrides_path.parts
    try:
        idx = parts.index(program_id)
    except ValueError:
        return ""
    # The directory after program_id is "overrides" or "archive/<edition>/overrides"
    if idx + 1 < len(parts) and parts[idx + 1] == "overrides":
        return ""  # program-level overrides; no edition
    if (
        idx + 3 < len(parts)
        and parts[idx + 1] == "archive"
        and parts[idx + 3] == "overrides"
    ):
        return parts[idx + 2]
    return ""


def _extract_issue_number(overrides_path: Path) -> int | None:
    match = _OVERRIDES_PATH_RE.search(str(overrides_path))
    if match is None:
        return None
    return int(match.group("num"))


def extract_judgments_from_overrides(
    overrides_path: Path,
    *,
    program_id: str,
    decided_by: str = "vertex.backfill",
    decided_at: datetime | None = None,
) -> OverrideExtraction:
    """Parse an overrides YAML into a tuple of ``Judgment`` records.

    Skips dimensions whose risk level is a needs-input marker (those
    are not real judgments yet). Dimensions are nested under
    ``scorecards.<workstream>.<dimension>: { risk: ... }``.
    """
    timestamp = (decided_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    issue_number = _extract_issue_number(overrides_path) or 0
    edition_id = _extract_edition_id(overrides_path, program_id)
    judgments: list[Judgment] = []

    if not overrides_path.exists():
        return OverrideExtraction(
            issue_number=issue_number,
            edition_id=edition_id,
            judgments=tuple(judgments),
        )

    with overrides_path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    if not isinstance(document, dict):
        return OverrideExtraction(
            issue_number=issue_number,
            edition_id=edition_id,
            judgments=tuple(judgments),
        )

    scorecards = document.get("scorecards") or {}
    if not isinstance(scorecards, dict):
        return OverrideExtraction(
            issue_number=issue_number,
            edition_id=edition_id,
            judgments=tuple(judgments),
        )

    counter = 0
    for workstream, dimensions in scorecards.items():
        if not isinstance(dimensions, dict):
            continue
        for dimension, value in dimensions.items():
            if not isinstance(value, dict):
                continue
            risk = value.get("risk")
            if not isinstance(risk, str):
                continue
            risk = risk.strip()
            if not risk or risk in _NEEDS_INPUT_MARKERS:
                continue
            counter += 1
            judgment_id = (
                f"judgment-{program_id}-{issue_number}-{counter:03d}"
            )
            judgments.append(
                Judgment(
                    id=judgment_id,
                    program_id=program_id,
                    dimension=dimension,
                    risk_level=risk,
                    edition_id=edition_id,
                    issue_number=issue_number,
                    justification=(
                        f"Backfilled from overrides file "
                        f"{overrides_path.name} (workstream {workstream!r})"
                    ),
                    decided_by=decided_by,
                    decided_at=timestamp,
                )
            )

    return OverrideExtraction(
        issue_number=issue_number,
        edition_id=edition_id,
        judgments=tuple(judgments),
    )


def _judgment_to_event(
    judgment: Judgment,
    *,
    program_id: str,
) -> ProgramEvent:
    """Translate a Judgment into a ProgramEvent for the fact store."""
    natural_key = (
        f"judgment|{program_id}|{judgment.issue_number}|"
        f"{judgment.edition_id}|{judgment.dimension}"
    )
    payload = {
        "judgment_id": judgment.id,
        "dimension": judgment.dimension,
        "risk_level": judgment.risk_level,
        "edition_id": judgment.edition_id,
        "issue_number": judgment.issue_number,
        "justification": judgment.justification,
        "decided_by": judgment.decided_by,
        "decided_at": judgment.decided_at.isoformat(),
    }
    return ProgramEvent(
        fact_type="judgment.dimension",
        natural_key=natural_key,
        metadata=payload,
    )


def backfill_judgments_from_overrides(
    overrides_path: Path,
    *,
    program_id: str,
    home_root: Path | None = None,
    db_root: Path | None = None,
    apply: bool = False,
    decided_by: str = "vertex.backfill",
) -> OverrideExtraction:
    """GAP-34: translate one overrides file into judgment facts.

    ``apply=False`` (default) returns the extraction without writing.
    ``apply=True`` appends each judgment as a fact to the Program
    Fact Store via ``append_program_event``.
    """
    extraction = extract_judgments_from_overrides(
        overrides_path,
        program_id=program_id,
        decided_by=decided_by,
    )
    if apply:
        for judgment in extraction.judgments:
            event = _judgment_to_event(judgment, program_id=program_id)
            append_program_event(
                program_id,
                event,
                recorded_at=judgment.decided_at,
                home_root=home_root,
                db_root=db_root,
            )
    return extraction


def discover_overrides_files(
    program_dir: Path,
) -> tuple[Path, ...]:
    """Find every ``overrides/issue_*.yaml`` under the program dir."""
    files: list[Path] = []
    if not program_dir.exists():
        return tuple(files)
    for overrides_dir in program_dir.glob("**/overrides"):
        if not overrides_dir.is_dir():
            continue
        for path in sorted(overrides_dir.glob("issue_*.yaml")):
            files.append(path)
    return tuple(files)


def backfill_program(
    program_id: str,
    *,
    program_dir: Path,
    home_root: Path | None = None,
    db_root: Path | None = None,
    apply: bool = False,
) -> tuple[OverrideExtraction, ...]:
    """GAP-34: backfill all overrides files for a program.

    Returns one ``OverrideExtraction`` per overrides file found.
    """
    extractions: list[OverrideExtraction] = []
    for path in discover_overrides_files(program_dir):
        extractions.append(
            backfill_judgments_from_overrides(
                path,
                program_id=program_id,
                home_root=home_root,
                db_root=db_root,
                apply=apply,
            )
        )
    return tuple(extractions)
