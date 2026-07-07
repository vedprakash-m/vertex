"""Bridge (continuity-graduation) quality gates (QG-B1 / QG-B2 / QG-B3).

Extracted from the ``src/core/quality_gates`` module (D-09 / Phase 3). These
gates guard structural drift across the continuity "bridge": section-roster
changes (QG-B1), scorecard-composition changes (QG-B2), and seeded-narrative
revision (QG-B3). Self-contained: depends only on the gate value objects, the
continuation contract, review status/state, and two pure formatting helpers.
Re-exported from the package ``__init__``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from src.core.continuation_contract import ContinuationContract
from src.core.jinja_filters import build_anchor
from src.core.models import ReviewState, ReviewStatus
from src.core.narrative_store import strip_scaffold_comments
from src.core.quality_gates.models import GateEvaluation, QualityGateReport


def evaluate_bridge_gates(
    *,
    continuation_contract: ContinuationContract | None,
    narratives: Mapping[str, str],
    review_status: ReviewStatus,
    bridge_graduated: bool = False,
) -> QualityGateReport:
    if continuation_contract is None:
        return QualityGateReport(results=())

    results = [
        _evaluate_bridge_section_roster_gate(continuation_contract, bridge_graduated=bridge_graduated),
        _evaluate_bridge_scorecard_composition_gate(continuation_contract, bridge_graduated=bridge_graduated),
    ]
    if not bridge_graduated:
        results.append(
            _evaluate_bridge_seeded_narrative_gate(
                continuation_contract=continuation_contract,
                narratives=narratives,
                review_status=review_status,
            )
        )
    return QualityGateReport(results=tuple(results))


def _evaluate_bridge_section_roster_gate(
    continuation_contract: ContinuationContract,
    *,
    bridge_graduated: bool,
) -> GateEvaluation:
    removed_by_dimension_override = {
        _bridge_detail_section_id(scorecard_name, dimension_name)
        for scorecard_name, dimension_name in continuation_contract.scorecard_composition.removed_by_override
    }
    added_sections = tuple(sorted(set(continuation_contract.section_roster.added_sections)))
    missing_sections = tuple(
        sorted(set(continuation_contract.section_roster.removed_sections) - removed_by_dimension_override)
    )
    if not added_sections and not missing_sections:
        return GateEvaluation("QG-B1", True, "Bridge section-roster gate passed.", 2, forceable=True)

    details: list[str] = []
    if added_sections:
        details.append(f"added sections: {', '.join(added_sections)}")
    if missing_sections:
        details.append(f"missing prior sections: {', '.join(missing_sections)}")
    return _bridge_structural_gate_failure(
        gate_id="QG-B1",
        bridge_graduated=bridge_graduated,
        blocking_message="Bridge section-roster drift requires explicit author action: " + "; ".join(details),
        advisory_message="Bridge section-roster drift remains advisory after graduation: " + "; ".join(details),
    )


def _evaluate_bridge_scorecard_composition_gate(
    continuation_contract: ContinuationContract,
    *,
    bridge_graduated: bool,
) -> GateEvaluation:
    additions = continuation_contract.scorecard_composition.proposed_additions
    removals = continuation_contract.scorecard_composition.proposed_removals
    if not additions and not removals:
        return GateEvaluation("QG-B2", True, "Bridge scorecard-composition gate passed.", 2, forceable=True)

    details: list[str] = []
    if additions:
        details.append(
            "proposed additions: "
            + ", ".join(_format_scorecard_dimension(scorecard_name, dimension_name) for scorecard_name, dimension_name in additions)
        )
    if removals:
        details.append(
            "proposed removals: "
            + ", ".join(_format_scorecard_dimension(scorecard_name, dimension_name) for scorecard_name, dimension_name in removals)
        )
    return _bridge_structural_gate_failure(
        gate_id="QG-B2",
        bridge_graduated=bridge_graduated,
        blocking_message="Bridge scorecard composition drift requires explicit author action: " + "; ".join(details),
        advisory_message="Bridge scorecard composition drift remains advisory after graduation: " + "; ".join(details),
    )


def _bridge_structural_gate_failure(
    *,
    gate_id: str,
    bridge_graduated: bool,
    blocking_message: str,
    advisory_message: str,
) -> GateEvaluation:
    if bridge_graduated:
        return GateEvaluation(gate_id, False, advisory_message, 1)
    return GateEvaluation(gate_id, False, blocking_message, 2, forceable=True)


def _evaluate_bridge_seeded_narrative_gate(
    *,
    continuation_contract: ContinuationContract,
    narratives: Mapping[str, str],
    review_status: ReviewStatus,
) -> GateEvaluation:
    if not continuation_contract.narrative_seeding.seeded:
        return GateEvaluation("QG-B3", True, "Bridge seeded-narrative revision gate passed.", 2, forceable=True)

    source_hashes = continuation_contract.narrative_seeding.source_hashes or {}
    unchanged_sections: list[str] = []
    for filename in continuation_contract.narrative_seeding.files_seeded:
        current_content = narratives.get(filename)
        source_hash = source_hashes.get(filename)
        if current_content is None or source_hash is None:
            continue
        normalized_content = strip_scaffold_comments(current_content)
        current_hash = f"sha256:{hashlib.sha256(normalized_content.encode('utf-8')).hexdigest()}"
        if current_hash != source_hash:
            continue
        review_section_id = _review_section_id_for_seeded_filename(filename)
        if review_section_id is not None and _review_section_is_approved(review_status, review_section_id):
            continue
        unchanged_sections.append(review_section_id or filename)

    if not unchanged_sections:
        return GateEvaluation("QG-B3", True, "Bridge seeded-narrative revision gate passed.", 2, forceable=True)
    return GateEvaluation(
        "QG-B3",
        False,
        "Seeded narratives remain unchanged from the trusted baseline without explicit review approval: "
        + ", ".join(sorted(unchanged_sections)),
        2,
        forceable=True,
    )


def _bridge_detail_section_id(scorecard_name: str, dimension_name: str) -> str:
    return build_anchor(f"{scorecard_name}-{dimension_name}")


def _format_scorecard_dimension(scorecard_name: str, dimension_name: str) -> str:
    return f"{scorecard_name} / {dimension_name}"


def _review_section_id_for_seeded_filename(filename: str) -> str | None:
    if filename == "exec_summary.md":
        return "exec_summary"
    if filename.startswith("ws_") and filename.endswith(".md"):
        return f"ws:{filename[3:-3]}"
    if filename.startswith("chapter_") and filename.endswith(".md"):
        return filename[8:-3]
    return None


def _review_section_is_approved(review_status: ReviewStatus, section_id: str) -> bool:
    for section in review_status.sections:
        if section.section_id == section_id:
            return section.state == ReviewState.APPROVED
    return False
