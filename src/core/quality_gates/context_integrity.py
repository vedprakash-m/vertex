"""Context-integrity gate cluster extracted from ``src/core/quality_gates``.

This leaf owns the single-file config hygiene checks for milestones and
scorecards so the package ``__init__`` can stay a thinner public shim.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from src.core.journal import PROGRAMS_ROOT
from src.core.quality_gates.models import GateEvaluation, QualityGateReport

_STUB_WI_ID_MIN = 900000
_STUB_WI_ID_MAX = 999999
_VALID_ODATA_OPERATORS = frozenset(
    (
        "startswith(",
        "contains(",
        "endswith(",
        "eq ",
        "ne ",
        "gt ",
        "lt ",
        "ge ",
        "le ",
        "and ",
        "or ",
        "not ",
        # Vertex internal filter DSL: "field contains 'value'" (used by scorecard_engine.parse_ado_filter)
        " contains ",
        " eq ",
        " ne ",
    )
)


def is_informal_odata_filter(filter_expr: str) -> bool:
    """Return True if ``filter_expr`` looks like prose, not valid OData."""
    if not filter_expr:
        return False
    stripped = filter_expr.strip()
    for op in _VALID_ODATA_OPERATORS:
        if op in stripped:
            return False
    if "(" not in stripped and any(char.islower() for char in stripped):
        return True
    return False


def evaluate_context_integrity_gates(
    *,
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> QualityGateReport:
    results = (
        _evaluate_ci_date_01(program_id, programs_root),
        _evaluate_ci_filter_01(program_id, programs_root),
    )
    return QualityGateReport(results=results)


def _evaluate_ci_date_01(program_id: str, programs_root: Path) -> GateEvaluation:
    milestones_path = programs_root / program_id / "milestones.yaml"
    if not milestones_path.exists():
        return GateEvaluation(
            "QG-CI-01",
            True,
            "QG-CI-01 DATE-01: milestones.yaml not found — skipping stub WI ID check.",
            exit_code=0,
            forceable=True,
        )

    try:
        document = yaml.safe_load(milestones_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return GateEvaluation(
            "QG-CI-01",
            False,
            f"QG-CI-01 DATE-01: failed to parse milestones.yaml: {exc}",
            exit_code=3,
            forceable=True,
        )

    raw_milestones = document.get("milestones") or ()
    stub_entries: list[str] = []
    for raw in raw_milestones:
        if not isinstance(raw, dict):
            continue
        linked_wis = raw.get("linked_work_item_ids") or ()
        for wi_id in linked_wis:
            if isinstance(wi_id, int) and _STUB_WI_ID_MIN <= wi_id <= _STUB_WI_ID_MAX:
                milestone_name = raw.get("name", raw.get("id", "unknown"))
                stub_entries.append(f"{milestone_name}: WI {wi_id}")

    if stub_entries:
        count = len(stub_entries)
        sample = ", ".join(stub_entries[:3])
        suffix = "..." if count > 3 else ""
        return GateEvaluation(
            "QG-CI-01",
            False,
            f"QG-CI-01 DATE-01: found {count} stub WI ID(s) in range 900000–999999: {sample}{suffix}. "
            f"Replace with real ADO work item IDs. [forceable with --force]",
            exit_code=3,
            forceable=True,
        )

    return GateEvaluation(
        "QG-CI-01",
        True,
        "QG-CI-01 DATE-01: no stub WI IDs found in milestones.yaml.",
        exit_code=0,
        forceable=True,
    )


def _evaluate_ci_filter_01(program_id: str, programs_root: Path) -> GateEvaluation:
    scorecards_path = programs_root / program_id / "scorecards.yaml"
    if not scorecards_path.exists():
        return GateEvaluation(
            "QG-CI-02",
            True,
            "QG-CI-02 FILTER-01: scorecards.yaml not found — skipping filter check.",
            exit_code=0,
            forceable=True,
        )

    try:
        document = yaml.safe_load(scorecards_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return GateEvaluation(
            "QG-CI-02",
            False,
            f"QG-CI-02 FILTER-01: failed to parse scorecards.yaml: {exc}",
            exit_code=3,
            forceable=True,
        )

    raw_scorecards = document.get("scorecards") or ()
    informal_entries: list[str] = []
    for raw_scorecard in raw_scorecards:
        if not isinstance(raw_scorecard, dict):
            continue
        scorecard_name = raw_scorecard.get("name", "unknown scorecard")
        raw_dimensions = raw_scorecard.get("dimensions") or ()
        for raw_dimension in raw_dimensions:
            if not isinstance(raw_dimension, dict):
                continue
            dimension_name = raw_dimension.get("name", "unknown dimension")
            filter_expr = raw_dimension.get("ado_filter") or ""
            if filter_expr and is_informal_odata_filter(str(filter_expr)):
                informal_entries.append(f"{scorecard_name}/{dimension_name}: {filter_expr!r}")

    if informal_entries:
        count = len(informal_entries)
        sample = ", ".join(informal_entries[:2])
        suffix = "..." if count > 2 else ""
        return GateEvaluation(
            "QG-CI-02",
            False,
            f"QG-CI-02 FILTER-01: found {count} informal OData filter(s) in scorecards.yaml "
            f"(valid OData required): {sample}{suffix}. [forceable with --force]",
            exit_code=3,
            forceable=True,
        )

    return GateEvaluation(
        "QG-CI-02",
        True,
        "QG-CI-02 FILTER-01: all scorecard ado_filter expressions are valid OData.",
        exit_code=0,
        forceable=True,
    )
