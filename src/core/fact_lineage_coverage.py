"""ADF-W2.4/W2.5 (specs/arch-data-fix.md Section 8.14.1/8.14.2): the fact
lineage coverage denominator.

This module measures whether accepted program facts carry traceable
provenance -- it does not (yet) retrofit lineage population into every
fact-acceptance call site. Section 8.14.1 requires *new* material facts to
be lineage-full; Section 8.14.2 requires the *existing* population to be
split into fully lineaged / explicitly waived / unlineaged defect. Building
that visibility is a prerequisite for closing the gap it measures, and is
this module's whole job.

A fact revision counts as "lineaged" if it carries ANY real provenance:
either the newer 12-field envelope (``domain_event_id``/``candidate_id``/
``source_document_key``/``evidence_ref``/``source_hash`` -- populated today
mainly by the REV/EML pipeline) or the broader, older
``source_signal_ids`` link back to the signal(s) that produced it (the
common case for ADO/Kusto-sourced facts via ``signal_promotion.py``). It
deliberately does NOT re-resolve every signal id to verify its own
``raw_ref`` is non-empty -- that would require a second full read of the
program's signal store on every coverage computation; a fact that names at
least one signal id already has a traceable path an operator can follow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.fact_lineage_waiver_store import FactLineageWaiver, load_fact_lineage_waivers
from src.core.program_fact_store import ProgramFactRevision, load_program_facts

LineageState = Literal["lineaged", "waived", "defect"]


@dataclass(frozen=True, slots=True)
class LineageCoverageReport:
    program_id: str
    total_count: int
    lineaged_count: int
    waived_count: int
    defect_count: int
    #: Bounded sample for operator triage, not a complete list.
    sample_defect_natural_keys: tuple[str, ...]
    computed_at: datetime

    @property
    def coverage_ratio(self) -> float | None:
        """Section 8.14.2: "the lineage denominator distinguishes" all
        three states -- the ratio is lineaged over the FULL denominator
        (lineaged + waived + defect), matching "only fully lineaged facts
        satisfy ADF-OM2" (a waiver is an accepted gap, not a pass)."""
        if self.total_count == 0:
            return None
        return self.lineaged_count / self.total_count


def has_fact_provenance(revision: ProgramFactRevision) -> bool:
    if revision.source_signal_ids:
        return True
    return any(
        value is not None
        for value in (
            revision.domain_event_id,
            revision.candidate_id,
            revision.source_document_key,
            revision.evidence_ref,
            revision.source_hash,
        )
    )


def classify_fact_lineage(
    revision: ProgramFactRevision,
    *,
    waivers_by_natural_key: dict[str, FactLineageWaiver],
    as_of: date,
) -> LineageState:
    if has_fact_provenance(revision):
        return "lineaged"
    waiver = waivers_by_natural_key.get(revision.natural_key)
    if waiver is not None and waiver.is_active(as_of=as_of):
        return "waived"
    return "defect"


def compute_lineage_coverage(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    now: datetime | None = None,
) -> LineageCoverageReport:
    resolved_now = now or datetime.now(timezone.utc)
    snapshot = load_program_facts(program_id, programs_root=programs_root)
    waivers = load_fact_lineage_waivers(program_id, programs_root=programs_root)
    waivers_by_key = {waiver.natural_key: waiver for waiver in waivers}

    lineaged_count = 0
    waived_count = 0
    defect_keys: list[str] = []
    for fact in snapshot.facts:
        state = classify_fact_lineage(fact, waivers_by_natural_key=waivers_by_key, as_of=resolved_now.date())
        if state == "lineaged":
            lineaged_count += 1
        elif state == "waived":
            waived_count += 1
        else:
            defect_keys.append(fact.natural_key)

    return LineageCoverageReport(
        program_id=program_id,
        total_count=len(snapshot.facts),
        lineaged_count=lineaged_count,
        waived_count=waived_count,
        defect_count=len(defect_keys),
        sample_defect_natural_keys=tuple(sorted(defect_keys)[:10]),
        computed_at=resolved_now,
    )


__all__ = [
    "LineageCoverageReport",
    "LineageState",
    "classify_fact_lineage",
    "compute_lineage_coverage",
    "has_fact_provenance",
]
