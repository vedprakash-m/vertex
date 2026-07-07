"""Continuity gate cluster extracted from ``src/core/quality_gates``.

This leaf owns the continuity-mode HTML validation rules (CG-01..CG-09) while
the package ``__init__`` continues to re-export the public entry point.
"""
from __future__ import annotations

import re

from src.core.quality_gates.models import GateEvaluation, QualityGateReport
from src.core.quality_gates.rendering import continuity_block_positions, find_visible_tool_attribution, visible_html_text


def evaluate_continuity_gates(
    *,
    html_content: str,
    issue_number: int,
    scorecard_band_block_ids: tuple[str, ...] = ("scorecard-band-primary", "scorecard-band-secondary"),
) -> QualityGateReport:
    """Evaluate all continuity gates against the rendered HTML.

    `scorecard_band_block_ids` is the ordered list of scorecard section block IDs
    for this program (first = primary, second = secondary, etc.). Gates CG-01/05/06
    skip gracefully when fewer than the expected number of bands are configured,
    allowing programs with a single scorecard or no scorecards to pass.
    """
    block_positions = continuity_block_positions(html_content)
    visible_text = visible_html_text(html_content)
    primary_band = scorecard_band_block_ids[0] if scorecard_band_block_ids else ""
    secondary_band = scorecard_band_block_ids[1] if len(scorecard_band_block_ids) >= 2 else ""
    results = (
        _evaluate_continuity_health_banner_gate(html_content, block_positions, primary_band_id=primary_band),
        _evaluate_continuity_decision_strip_gate(html_content),
        _evaluate_continuity_what_changed_gate(html_content),
        _evaluate_continuity_nav_gate(html_content),
        _evaluate_continuity_scorecard_order_gate(block_positions, primary_band_id=primary_band, secondary_band_id=secondary_band),
        _evaluate_continuity_cadence_order_gate(block_positions, primary_band_id=primary_band, secondary_band_id=secondary_band),
        _evaluate_continuity_tool_visibility_gate(visible_text),
        _evaluate_continuity_snapshot_ribbon_gate(html_content, block_positions),
        _evaluate_continuity_issue_number_gate(visible_text, issue_number),
    )
    return QualityGateReport(results=results)


def _block_position(block_positions: dict[str, list[int]], block_id: str) -> int:
    return block_positions.get(block_id, [-1])[0]


def _chapter_positions(block_positions: dict[str, list[int]]) -> tuple[int, ...]:
    return tuple(
        position
        for block_id, positions in block_positions.items()
        if block_id.startswith("chapter-")
        for position in positions
    )


def _evaluate_continuity_health_banner_gate(
    html_content: str,
    block_positions: dict[str, list[int]],
    *,
    primary_band_id: str,
) -> GateEvaluation:
    if not primary_band_id:
        return GateEvaluation("CG-01", True, "Continuity health-banner gate passed (no scorecard bands configured).", 3)
    scorecard_cutoff = _block_position(block_positions, primary_band_id)
    scope = html_content[: scorecard_cutoff if scorecard_cutoff >= 0 else len(html_content)]
    if 'id="health"' in scope or re.search(r"Program Health", visible_html_text(scope), re.IGNORECASE):
        return GateEvaluation(
            "CG-01",
            False,
            "CONTINUITY GATE [CG-01]: Health Banner detected above scorecards. Move health signals inside the Executive Summary section (§A11) or set layout_mode: dashboard.",
            3,
        )
    return GateEvaluation("CG-01", True, "Continuity health-banner gate passed.", 3)


def _evaluate_continuity_decision_strip_gate(html_content: str) -> GateEvaluation:
    if 'id="top-3"' in html_content or "DECISIONS &amp; SIGNALS" in html_content:
        return GateEvaluation(
            "CG-02",
            False,
            "CONTINUITY GATE [CG-02]: Decision Strip rendered as standalone section. In continuity mode, decisions appear inside the Executive Summary callout (§A1.7).",
            3,
        )
    return GateEvaluation("CG-02", True, "Continuity decision-strip gate passed.", 3)


def _evaluate_continuity_what_changed_gate(html_content: str) -> GateEvaluation:
    if 'id="changes"' in html_content or re.search(r"\bWHAT CHANGED\b", html_content):
        return GateEvaluation(
            "CG-03",
            False,
            "CONTINUITY GATE [CG-03]: What Changed card feed found in published output. Move to reviewer pane (§A4.5).",
            3,
        )
    return GateEvaluation("CG-03", True, "Continuity what-changed gate passed.", 3)


def _evaluate_continuity_nav_gate(html_content: str) -> GateEvaluation:
    if 'href="#health"' in html_content and 'href="#top-3"' in html_content:
        return GateEvaluation(
            "CG-04",
            False,
            "CONTINUITY GATE [CG-04]: Nav bar detected. Replace with Jump to Section block (§A12) in continuity mode.",
            3,
        )
    return GateEvaluation("CG-04", True, "Continuity nav gate passed.", 3)


def _evaluate_continuity_scorecard_order_gate(
    block_positions: dict[str, list[int]],
    *,
    primary_band_id: str,
    secondary_band_id: str,
) -> GateEvaluation:
    if not primary_band_id or not secondary_band_id:
        return GateEvaluation("CG-05", True, "Continuity scorecard-order gate passed (single-band or no-band program).", 3)
    primary_band_pos = _block_position(block_positions, primary_band_id)
    secondary_band_pos = _block_position(block_positions, secondary_band_id)
    exec_position = _block_position(block_positions, "exec-summary")
    jump_position = _block_position(block_positions, "jump-to-section")
    chapter_positions = _chapter_positions(block_positions)
    if (
        primary_band_pos < 0
        or secondary_band_pos < 0
        or exec_position < 0
        or jump_position < 0
        or not chapter_positions
        or not (primary_band_pos < secondary_band_pos < exec_position < jump_position < min(chapter_positions))
    ):
        return GateEvaluation(
            "CG-05",
            False,
            "CONTINUITY GATE [CG-05]: Assembly order violation. Scorecard bands must appear at positions 4-5, before the Executive Summary (§6.2).",
            3,
        )
    return GateEvaluation("CG-05", True, "Continuity scorecard-order gate passed.", 3)


def _evaluate_continuity_cadence_order_gate(
    block_positions: dict[str, list[int]],
    *,
    primary_band_id: str,
    secondary_band_id: str,
) -> GateEvaluation:
    if not primary_band_id or not secondary_band_id:
        return GateEvaluation("CG-06", True, "Continuity cadence-order gate passed (single-band or no-band program).", 3)
    cadence_position = _block_position(block_positions, "cadence-note")
    primary_band_pos = _block_position(block_positions, primary_band_id)
    secondary_band_pos = _block_position(block_positions, secondary_band_id)
    if cadence_position < 0 or primary_band_pos < 0 or secondary_band_pos < 0 or not (cadence_position < primary_band_pos < secondary_band_pos):
        return GateEvaluation(
            "CG-06",
            False,
            "CONTINUITY GATE [CG-06]: Assembly order violation. Cadence note must appear at position 3, before scorecards (§6.2).",
            3,
        )
    return GateEvaluation("CG-06", True, "Continuity cadence-order gate passed.", 3)


def _evaluate_continuity_tool_visibility_gate(visible_text: str) -> GateEvaluation:
    matched_text = find_visible_tool_attribution(visible_text)
    if matched_text is not None:
        return GateEvaluation(
            "CG-07",
            False,
            f'CONTINUITY GATE [CG-07]: Tool attribution found in published HTML ("{matched_text}"). The authoring tool must not be visible to newsletter readers. Move to HTML comment or dry-run artifact (§1.5, §14).',
            3,
        )
    return GateEvaluation("CG-07", True, "Continuity tool-visibility gate passed.", 3)


def _evaluate_continuity_snapshot_ribbon_gate(
    html_content: str,
    block_positions: dict[str, list[int]],
) -> GateEvaluation:
    jump_position = _block_position(block_positions, "jump-to-section")
    chapter_positions = sorted(_chapter_positions(block_positions))
    if jump_position >= 0 and chapter_positions:
        gap_segment = html_content[jump_position:chapter_positions[0]]
        if "Compact Snapshot" in gap_segment or re.search(r'data-vertex-block="snapshot-ribbon"', gap_segment):
            return GateEvaluation(
                "CG-08",
                False,
                "CONTINUITY GATE [CG-08]: Compact ribbon detected between jump list and first chapter. This section was removed in v0.3 (§1.3).",
                3,
            )
    return GateEvaluation("CG-08", True, "Continuity snapshot-ribbon gate passed.", 3)


def _evaluate_continuity_issue_number_gate(visible_text: str, issue_number: int) -> GateEvaluation:
    matches = tuple(re.finditer(rf"Issue\s+0*{issue_number}\b", visible_text, re.IGNORECASE))
    if len(matches) > 1:
        repeated_issue = matches[1].group(0)
        return GateEvaluation(
            "CG-09",
            False,
            f'CONTINUITY GATE [CG-09]: Issue number "{repeated_issue}" appears more than once in visible HTML. Issue number belongs only in the title line (position 1). See §1.5.',
            3,
        )
    return GateEvaluation("CG-09", True, "Continuity issue-number gate passed.", 3)
