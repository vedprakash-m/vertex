"""ADF-W2.9 P5 (specs/arch-data-fix.md v1.51 deep-dive plan): the blind A/B
comparison harness for piloting ContextCompiler/AISchemaGateway (ADF-W2.7/
W2.8) into one live AI-authored surface before ever swapping it in as the
production default.

Generic, append-only comparison-recording sidecar -- deliberately not
decision-brief-specific in shape (any future pilot surface can reuse it),
even though today only ``decision_brief_advisor``'s context-gateway pilot
writes to it. Mirrors ``proposal_audit.py``'s established shape: one JSONL
file per program, one record per human judgment, no aggregation logic
baked into the write path.

"Blind" means the two options are always labeled "A"/"B" with the mapping
to baseline/candidate randomized per comparison and never surfaced to the
reviewer -- only ``a_is_candidate`` (recorded, not displayed) lets later
analysis attribute the human's choice to a side.

Zone A -- no AI or M365 imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Literal

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records

ComparisonChoice = Literal["a", "b", "tie", "neither"]

_MAX_BYTES = 10 * 1024 * 1024  # matches proposal_audit.jsonl's rotation cap


def blind_ab_comparison_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "journal" / "blind_ab_comparisons.jsonl"


@dataclass(frozen=True, slots=True)
class ComparisonRecord:
    program_id: str
    surface: str
    item_id: str
    option_a_text: str
    option_b_text: str
    # Which side ("a" or "b") was the new/candidate path -- withheld from
    # the reviewer at judgment time, recorded here purely for analysis.
    a_is_candidate: bool
    choice: ComparisonChoice
    recorded_at: datetime
    notes: str | None = None


def record_comparison(
    *,
    program_id: str,
    surface: str,
    item_id: str,
    option_a_text: str,
    option_b_text: str,
    a_is_candidate: bool,
    choice: ComparisonChoice,
    programs_root: Path = PROGRAMS_ROOT,
    recorded_at: datetime | None = None,
    notes: str | None = None,
) -> None:
    """Append one blind-comparison judgment. Always durable (unlike
    ``record_proposal_event``'s opt-in no-op default) -- this harness has
    no purpose if judgments aren't recorded."""
    record = ComparisonRecord(
        program_id=program_id,
        surface=surface,
        item_id=item_id,
        option_a_text=option_a_text,
        option_b_text=option_b_text,
        a_is_candidate=a_is_candidate,
        choice=choice,
        recorded_at=recorded_at or datetime.now(timezone.utc),
        notes=notes,
    )
    line = json.dumps(_to_jsonable(record), sort_keys=True) + "\n"
    append_jsonl_line(blind_ab_comparison_path(program_id, programs_root=programs_root), line, max_bytes=_MAX_BYTES)


def _to_jsonable(record: ComparisonRecord) -> dict[str, object]:
    return {
        "program_id": record.program_id,
        "surface": record.surface,
        "item_id": record.item_id,
        "option_a_text": record.option_a_text,
        "option_b_text": record.option_b_text,
        "a_is_candidate": record.a_is_candidate,
        "choice": record.choice,
        "recorded_at": record.recorded_at.isoformat(),
        "notes": record.notes,
    }


def read_comparisons(
    program_id: str,
    *,
    surface: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[ComparisonRecord, ...]:
    path = blind_ab_comparison_path(program_id, programs_root=programs_root)
    if not path.exists():
        return ()
    records = []
    for raw in read_jsonl_records(path):
        if surface is not None and raw.get("surface") != surface:
            continue
        records.append(
            ComparisonRecord(
                program_id=raw["program_id"],
                surface=raw["surface"],
                item_id=raw["item_id"],
                option_a_text=raw["option_a_text"],
                option_b_text=raw["option_b_text"],
                a_is_candidate=bool(raw["a_is_candidate"]),
                choice=raw["choice"],
                recorded_at=datetime.fromisoformat(raw["recorded_at"]),
                notes=raw.get("notes"),
            )
        )
    return tuple(records)


@dataclass(frozen=True, slots=True)
class ComparisonSummary:
    surface: str
    total: int
    candidate_wins: int
    baseline_wins: int
    ties: int
    neither: int
    # None when there are no decisive (a/b) comparisons yet -- a rate of
    # 0.0 would otherwise be indistinguishable from "no evidence".
    candidate_win_rate: float | None


def summarize_comparisons(records: tuple[ComparisonRecord, ...], *, surface: str) -> ComparisonSummary:
    candidate_wins = 0
    baseline_wins = 0
    ties = 0
    neither = 0
    for record in records:
        if record.choice == "tie":
            ties += 1
        elif record.choice == "neither":
            neither += 1
        elif (record.choice == "a") == record.a_is_candidate:
            candidate_wins += 1
        else:
            baseline_wins += 1
    decisive = candidate_wins + baseline_wins
    win_rate = candidate_wins / decisive if decisive else None
    return ComparisonSummary(
        surface=surface,
        total=len(records),
        candidate_wins=candidate_wins,
        baseline_wins=baseline_wins,
        ties=ties,
        neither=neither,
        candidate_win_rate=win_rate,
    )


__all__ = [
    "ComparisonChoice",
    "ComparisonRecord",
    "ComparisonSummary",
    "blind_ab_comparison_path",
    "read_comparisons",
    "record_comparison",
    "summarize_comparisons",
]
