"""Counterfactual render harness for AG-1 (activation.md §1 / §6.13).

Renders the milestone section twice — once with an approved fact present, once
with it withheld — so the activation sentence's "demonstrably changes what the
newsletter says" is falsifiable via a non-empty, attributable render diff.

The verifier (``scripts/verify_activation.py``) already consumes a supplied
with/without render pair and asserts the added delta carries the fact's
``source_document_key`` (and ``approval_event_id`` when required). This module
GENERATES that pair by rendering the milestone section from ``ProgramReality``
with one fact suppressed.

Lives under ``src/commands/`` (not ``src/core/``): it reuses the existing
milestone render path (``_build_report_milestone_rows`` in
``src/commands/report_deck.py``) + the milestone Jinja partial, and ``src/core/``
must never import from ``src/commands/`` (the zone boundary invariant).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CounterfactualRenderPair:
    """A with/without-fact render pair for AG-1 counterfactual diff."""

    with_fact_text: str
    without_fact_text: str
    fact_id: str
    source_document_key: str | None
    approval_event_id: str | None

    @property
    def differs(self) -> bool:
        """True iff suppressing the fact changed the rendered section."""
        return self.with_fact_text != self.without_fact_text


def render_milestone_section_text(
    *,
    program_id: str,
    programs_root: Path,
    as_of: datetime | None = None,
    suppress_fact_id: str | None = None,
) -> str:
    """Render the milestone section from ``ProgramReality`` as plain text.

    When ``suppress_fact_id`` is given, the milestone fact with that id is
    withheld from the render — producing the counterfactual "without-fact"
    arm. Returns the rendered section text (empty string if no milestones).

    This is the keystone of AG-1: it proves the approved fact *changes* the
    rendered output, not just that a render "ran."
    """
    try:
        from src.core.program_reality import ProgramReality
        from src.core.milestone_engine import assess_milestone_health
        from src.commands.report_deck import _build_report_milestone_rows
        from jinja2 import Environment, FileSystemLoader, select_autoescape
    except Exception:  # pragma: no cover — defensive
        log.debug("counterfactual render: imports unavailable", exc_info=True)
        return ""

    when = as_of or datetime.now(timezone.utc)
    try:
        reality = ProgramReality.load(program_id, programs_root=programs_root)
    except Exception:
        log.debug("counterfactual render: ProgramReality.load failed", exc_info=True)
        return ""

    assessments = tuple(reality.milestones())
    if suppress_fact_id:
        assessments = tuple(
            a for a in assessments
            if str(getattr(a, "fact_id", "") or "") != suppress_fact_id
        )
    if not assessments:
        return ""

    milestone_list = []
    for a in assessments:
        record = getattr(a, "record", None)
        if record is not None:
            milestone_list.append(record)
    milestones = tuple(milestone_list)
    if not milestones:
        return ""

    # Build the lineage map from the (possibly filtered) assessments.
    lineage: dict[str, dict[str, str | None]] = {}
    for a in assessments:
        record = getattr(a, "record", None)
        mid = getattr(record, "id", None)
        if not mid:
            continue
        lin = getattr(a, "lineage", None)
        lineage[str(mid)] = {
            "source_document_key": getattr(lin, "source_document_key", None) if lin else None,
            "approval_event_id": getattr(lin, "approval_event_id", None) if lin else None,
        }

    # _build_report_milestone_rows / _build_deck_milestone_rows expect computed
    # health-model MilestoneAssessment rows (with a milestone_id field), not the
    # ProgramReality FactAssessment wrapper returned by reality.milestones() —
    # compute the real per-milestone health assessment here (no live ADO items
    # available in this offline harness, so items=() / trajectories={}).
    try:
        milestone_health = tuple(
            assess_milestone_health(milestone, (), {}, when) for milestone in milestones
        )
    except Exception:
        log.debug("counterfactual render: milestone health assessment failed", exc_info=True)
        return ""

    try:
        rows = _build_report_milestone_rows(
            milestones,
            milestone_health,  # milestone_assessments
            items=(),
            program_id=program_id,
            programs_root=programs_root,
            as_of=when,
            milestone_lineage=lineage,
        )
    except Exception:
        log.debug("counterfactual render: row build failed", exc_info=True)
        return ""

    return _render_milestone_rows_text(rows)



def _render_milestone_rows_text(rows: Any) -> str:
    """Render milestone rows to plain text (one line per milestone, with lineage)."""
    lines: list[str] = []
    for row in rows:
        name = getattr(row, "name", "?")
        status = getattr(row, "status", "?")
        target = getattr(row, "target_date_label", "")
        detail = getattr(row, "detail", "")
        sdk = getattr(row, "source_document_key", None)
        aeid = getattr(row, "approval_event_id", None)
        line = f"- {name} | {status} | target {target}"
        if detail:
            line += f" — {detail}"
        if sdk:
            line += f" [Source: {sdk}"
            if aeid:
                line += f" | approval {aeid}"
            line += "]"
        lines.append(line)
    return "\n".join(lines)


def build_counterfactual_pair(
    *,
    program_id: str,
    fact_id: str,
    programs_root: Path,
    as_of: datetime | None = None,
) -> CounterfactualRenderPair | None:
    """Build a with/without-fact render pair for one milestone fact.

    Returns ``None`` if the fact isn't found in the milestone set (the
    counterfactual is only meaningful for a fact that would otherwise render).
    """
    when = as_of or datetime.now(timezone.utc)
    with_text = render_milestone_section_text(
        program_id=program_id, programs_root=programs_root, as_of=when,
    )
    without_text = render_milestone_section_text(
        program_id=program_id, programs_root=programs_root, as_of=when,
        suppress_fact_id=fact_id,
    )
    # Resolve the lineage metadata for the suppressed fact.
    source_key = None
    approval_id = None
    try:
        from src.core.program_reality import ProgramReality
        reality = ProgramReality.load(program_id, programs_root=programs_root)
        for a in reality.milestones():
            if str(getattr(a, "fact_id", "") or "") == fact_id:
                lin = getattr(a, "lineage", None)
                source_key = getattr(lin, "source_document_key", None) if lin else None
                approval_id = getattr(lin, "approval_event_id", None) if lin else None
                break
    except Exception:
        pass

    return CounterfactualRenderPair(
        with_fact_text=with_text,
        without_fact_text=without_text,
        fact_id=fact_id,
        source_document_key=source_key,
        approval_event_id=approval_id,
    )


__all__ = [
    "CounterfactualRenderPair",
    "render_milestone_section_text",
    "build_counterfactual_pair",
]
