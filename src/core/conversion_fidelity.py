"""FR-SG-43: Per-function Conversion Fidelity.

Measures what fraction of required inputs for each Vertex function arrive as
automatic, source-traceable facts vs. needing manual authoring/override.

  ConversionFidelity = required_inputs_sourced / required_inputs_total

Denominator definitions (per FR-SG-52):
  - newsletter:  ADO channel active + at least one approved signal per workstream
  - nudge:       actions with owner_alias and linked_work_item_ids populated
  - risk:        risk entries with source_signal_ids or linked_claim_ids
  - action:      actions with owner_alias AND (linked_work_item_ids or due_date)
  - review:      approved signals with non-empty entity_refs
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from src.core.journal import PROGRAMS_ROOT


class FunctionName(StrEnum):
    NEWSLETTER = "newsletter"
    NUDGE = "nudge"
    RISK = "risk"
    ACTION = "action"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class ConversionFidelityEntry:
    function: str
    required_inputs: int
    sourced_inputs: int
    score: float  # sourced_inputs / required_inputs; 0.0 when required_inputs == 0
    computed_at: datetime


def _fidelity_score(sourced: int, total: int) -> float:
    return round(sourced / total, 4) if total > 0 else 0.0


def compute_newsletter_fidelity(gather_state: object) -> ConversionFidelityEntry:
    """Fidelity for newsletter function.

    Required inputs: number of workstreams declared in gather_state.
    Sourced inputs: workstreams that have at least one active/healthy channel.
    """
    channels = getattr(gather_state, "channels", None) or {}
    if not isinstance(channels, dict):
        channels = {}

    ado_ok = bool(channels.get("ado", {}).get("active"))
    required = max(1, len(channels))
    sourced = sum(
        1
        for ch_data in channels.values()
        if isinstance(ch_data, dict) and ch_data.get("active")
    )
    if ado_ok:
        sourced = max(sourced, 1)
    return ConversionFidelityEntry(
        function=FunctionName.NEWSLETTER,
        required_inputs=required,
        sourced_inputs=min(sourced, required),
        score=_fidelity_score(min(sourced, required), required),
        computed_at=datetime.now(timezone.utc),
    )


def compute_nudge_fidelity(actions: tuple[Any, ...]) -> ConversionFidelityEntry:
    """Fidelity for nudge function.

    Required inputs: all actions.
    Sourced inputs: actions with owner_alias AND linked_work_item_ids populated.
    """
    total = len(actions)
    sourced = sum(
        1
        for a in actions
        if getattr(a, "owner_alias", None) and getattr(a, "linked_work_item_ids", None)
    )
    return ConversionFidelityEntry(
        function=FunctionName.NUDGE,
        required_inputs=total,
        sourced_inputs=sourced,
        score=_fidelity_score(sourced, total),
        computed_at=datetime.now(timezone.utc),
    )


def compute_risk_fidelity(risk_entries: tuple[Any, ...]) -> ConversionFidelityEntry:
    """Fidelity for risk function.

    Required inputs: all active risk entries.
    Sourced inputs: risks with source_signal_ids or linked_claim_ids.
    """
    total = len(risk_entries)
    sourced = sum(
        1
        for r in risk_entries
        if getattr(r, "source_signal_ids", None) or getattr(r, "linked_claim_ids", None)
    )
    return ConversionFidelityEntry(
        function=FunctionName.RISK,
        required_inputs=total,
        sourced_inputs=sourced,
        score=_fidelity_score(sourced, total),
        computed_at=datetime.now(timezone.utc),
    )


def compute_action_fidelity(actions: tuple[Any, ...]) -> ConversionFidelityEntry:
    """Fidelity for action tracking function.

    Required inputs: all actions.
    Sourced inputs: actions with owner_alias AND (linked_work_item_ids or due_date).
    """
    total = len(actions)
    sourced = sum(
        1
        for a in actions
        if getattr(a, "owner_alias", None)
        and (getattr(a, "linked_work_item_ids", None) or getattr(a, "due_date", None))
    )
    return ConversionFidelityEntry(
        function=FunctionName.ACTION,
        required_inputs=total,
        sourced_inputs=sourced,
        score=_fidelity_score(sourced, total),
        computed_at=datetime.now(timezone.utc),
    )


def compute_review_fidelity(signals: tuple[Any, ...]) -> ConversionFidelityEntry:
    """Fidelity for review function.

    Required inputs: all approved signals.
    Sourced inputs: approved signals with non-empty entity_refs.
    """
    total = len(signals)
    sourced = sum(1 for s in signals if getattr(s, "entity_refs", None))
    return ConversionFidelityEntry(
        function=FunctionName.REVIEW,
        required_inputs=total,
        sourced_inputs=sourced,
        score=_fidelity_score(sourced, total),
        computed_at=datetime.now(timezone.utc),
    )


def _get_fidelity_path(program_id: str, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "metrics" / "conversion_fidelity.yaml"


def persist_conversion_fidelity(
    program_id: str,
    entries: tuple[ConversionFidelityEntry, ...],
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    path = _get_fidelity_path(program_id, programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "function": e.function,
            "required_inputs": e.required_inputs,
            "sourced_inputs": e.sourced_inputs,
            "score": e.score,
            "computed_at": e.computed_at.isoformat(),
        }
        for e in entries
    ]
    path.write_text(yaml.safe_dump({"entries": records}, sort_keys=False), encoding="utf-8")


def load_conversion_fidelity(
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[ConversionFidelityEntry, ...]:
    path = _get_fidelity_path(program_id, programs_root)
    if not path.exists():
        return ()
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries = []
        for r in doc.get("entries") or []:
            entries.append(ConversionFidelityEntry(
                function=str(r["function"]),
                required_inputs=int(r["required_inputs"]),
                sourced_inputs=int(r["sourced_inputs"]),
                score=float(r["score"]),
                computed_at=datetime.fromisoformat(str(r["computed_at"])),
            ))
        return tuple(entries)
    except (KeyError, ValueError, yaml.YAMLError):
        return ()
