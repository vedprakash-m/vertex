"""WI-6.2: Extract report_pipeline/assemble_stage.py from report.py.

Usage: python scripts/extract_report_pipeline.py
"""
from __future__ import annotations
import os
from pathlib import Path

REPO = Path(__file__).parent.parent
REPORT_PY = REPO / "src/commands/report.py"
PIPELINE_DIR = REPO / "src/commands/report_pipeline"

# Line ranges to MOVE to assemble_stage.py (0-indexed half-open: [start, end))
# Order matters — we'll concatenate in order for assemble_stage.py
MOVE_RANGES: list[tuple[int, int]] = [
    (324, 387),    # DraftArtifacts, DraftState, ProgramFactSnapshotDraftState
    (450, 735),    # small helpers (_pin_program_fact_snapshot … _derive_risk_sparkline)
    (1677, 2297),  # _generate_report_draft_from_context
    (2298, 2323),  # _write_report_adaptive_cards
    (2324, 2885),  # _generate_lookback_draft
    (2902, 3950),  # DI wrappers (L2903-3950)
]

# Names of all moved items (for re-export block in report.py)
MOVED_NAMES = """DraftArtifacts
DraftState
ProgramFactSnapshotDraftState
_pin_program_fact_snapshot
_runtime_db_root_for_reports
_is_decision_type
_is_risk_type
_normalize_section_filter_ids
_compute_healthy_streak
_compute_read_time_minutes
_format_prior_date_label
_build_v2_vitality_snapshot
_resolve_vitality_workstream_id
_vitality_owner_alias
_count_new_high_dimensions
_has_severe_freshness_signals
_truncate_words
_decision_strip_ack_required
_derive_vector_label
_risk_rank
_spark_char
_derive_risk_sparkline
_generate_report_draft_from_context
_write_report_adaptive_cards
_generate_lookback_draft
_load_eta_forecasts
_load_draft_ai_context
_load_report_signal_context
_load_guarded_review_evidence
_synthesize_v2_ai_content
_build_disabled_ai_synthesis_result
_iter_ai_generated_sections
_build_newsletter_scoped_items
_build_newsletter_narrative_covered_item_ids
_create_ai_client
_load_live_work_items
_build_scorecard_data
_apply_scorecard_trend_annotation
_build_exec_summary_text
_build_exec_summary_severe_signal_seeds
_build_continuity_exec_summary_template
_build_workstream_templates
_visible_detail_section_ids
_iter_detail_sections
_skipped_review_sections
_build_workstream_data
_attach_kpi_tiles_to_workstreams
_kpi_tiles_for_section
_section_workstream_id
_kpi_tile_from_signal
_kpi_tile_from_query
_signal_result_payload
_build_continuity_workstream_data
_build_continuity_render_data
_order_continuity_dimensions
_higher_risk
_rest_call_count
_read_git_sha""".strip().splitlines()


def _build_moved_set(lines: list[str]) -> set[int]:
    moved: set[int] = set()
    for start, end in MOVE_RANGES:
        for i in range(start, end):
            moved.add(i)
    return moved


def _build_reexport_block() -> str:
    indent = "    "
    names = ",\n".join(f"{indent}{n}" for n in MOVED_NAMES)
    return (
        "# re-export all assembly-stage identifiers so existing import sites\n"
        "# (confirm.py, diff.py, prep.py, evidence.py, …) continue to work\n"
        "# unchanged after WI-6.2 extraction.\n"
        "from src.commands.report_pipeline.assemble_stage import (\n"
        + names
        + ",\n)\n"
    )


def main() -> None:
    with open(REPORT_PY, "r", encoding="utf-8") as fh:
        lines = fh.readlines()

    print(f"report.py: {len(lines)} lines")

    # --- build moved_lines: the body of assemble_stage.py (without imports) ---
    moved_set = _build_moved_set(lines)
    moved_body: list[str] = []
    for start, end in MOVE_RANGES:
        moved_body.extend(lines[start:end])

    # assemble_stage.py header: re-use report.py's full import block (L0-299) so
    # every dependency the moved code needs is available without hunting imports.
    imports_section = "".join(lines[0:299])
    assemble_src = (
        '"""Assembly-stage helpers extracted from report.py (WI-6.2).\n\n'
        "Contains the core report/lookback assembly functions and all DI-injection\n"
        "wrapper functions that the stage classes inject into StageContext.\n"
        "External callers should continue importing from src.commands.report (which\n"
        "re-exports every name from this module) so no import sites need updating.\n"
        '"""\n'
        + imports_section
        + "\n\n"
        + "".join(moved_body)
    )

    # --- build new report.py: remove moved lines, add re-export block ---
    new_lines: list[str] = []
    for i, line in enumerate(lines):
        if i not in moved_set:
            new_lines.append(line)

    # Insert re-export block right after the last import line (line 298, 0-indexed)
    # Lines 0-298 are imports; line 299 (0-indexed) is the first blank/constant line.
    # We insert AFTER index 298 in new_lines.
    # Since we removed some lines, find the insertion point by scanning for "from src.m365":
    insert_after = 0
    for idx, line in enumerate(new_lines):
        if "from src.m365.graph_send_client" in line:
            insert_after = idx
            break

    reexport_block = "\n" + _build_reexport_block() + "\n"
    new_lines.insert(insert_after + 1, reexport_block)

    # --- write files ---
    PIPELINE_DIR.mkdir(parents=True, exist_ok=True)

    init_path = PIPELINE_DIR / "__init__.py"
    if not init_path.exists():
        init_path.write_text(
            '"""report_pipeline — assembly-stage subpackage (WI-6.2)."""\n',
            encoding="utf-8",
        )
        print(f"Created {init_path}")

    assemble_path = PIPELINE_DIR / "assemble_stage.py"
    assemble_path.write_text(assemble_src, encoding="utf-8")
    print(f"Created {assemble_path} ({assemble_src.count(chr(10))} lines)")

    with open(REPORT_PY, "w", encoding="utf-8") as fh:
        fh.writelines(new_lines)
    new_loc = len(new_lines)
    print(f"Updated {REPORT_PY} → {new_loc} lines")

    if new_loc <= 1500:
        print("✓ LOC ratchet satisfied (≤1,500)")
    else:
        print(f"⚠ LOC ratchet NOT yet satisfied ({new_loc} > 1,500)")


if __name__ == "__main__":
    main()
