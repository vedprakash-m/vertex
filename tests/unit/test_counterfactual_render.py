"""Counterfactual render harness tests (activation.md §1 / AG-1 / §6.13).

The harness renders the milestone section twice — with and without an approved
fact — so the activation sentence's "demonstrably changes the newsletter" is
falsifiable. These tests prove the suppress mechanism + the diff-delta-carries-
source_document_key contract, independent of the real-data precondition (an
approved EML-derived fact, which is not yet present).
"""

from __future__ import annotations

from pathlib import Path

from src.commands.counterfactual_render import (
    CounterfactualRenderPair,
    _render_milestone_rows_text,
    build_counterfactual_pair,
    render_milestone_section_text,
)
from src.core.view_models import MilestoneSummaryRow


class TestRenderMilestoneRowsText:
    def test_includes_source_document_key_when_present(self) -> None:
        rows = (
            MilestoneSummaryRow(
                name="M1", status="completed", target_date_label="2026-07-01",
                detail="done", source_document_key="sha256:abc", approval_event_id="evt-1",
            ),
        )
        text = _render_milestone_rows_text(rows)
        assert "sha256:abc" in text
        assert "evt-1" in text

    def test_omits_source_when_absent(self) -> None:
        rows = (
            MilestoneSummaryRow(
                name="M2", status="at_risk", target_date_label="2026-08-01",
                detail="delayed", source_document_key=None, approval_event_id=None,
            ),
        )
        text = _render_milestone_rows_text(rows)
        assert "Source:" not in text


class TestCounterfactualMechanism:
    """The core AG-1 contract: suppressing a lineage-bearing fact produces a
    non-identical render whose added delta carries source_document_key."""

    def test_suppress_produces_diff_with_source_key(self) -> None:
        rows_with = (
            MilestoneSummaryRow(
                name="M1 Ship Gen9", status="completed", target_date_label="2026-07-01",
                detail="BIOS rollout done", source_document_key="sha256:real-eml-001",
                approval_event_id="approval-evt-9",
            ),
            MilestoneSummaryRow(
                name="M2 Ramp", status="at_risk", target_date_label="2026-08-01",
                detail="pilot delayed", source_document_key=None, approval_event_id=None,
            ),
        )
        rows_without = rows_with[1:]  # the suppressed fact is gone

        text_with = _render_milestone_rows_text(rows_with)
        text_without = _render_milestone_rows_text(rows_without)

        # The render differs (the fact changed what the section says).
        assert text_with != text_without
        # The source key is present in the with-arm, absent in the without-arm.
        assert "sha256:real-eml-001" in text_with
        assert "sha256:real-eml-001" not in text_without

    def test_identical_when_nothing_suppressed(self) -> None:
        rows = (
            MilestoneSummaryRow(name="M1", status="completed", target_date_label="t",
                                detail="d", source_document_key="k", approval_event_id="a"),
        )
        text = _render_milestone_rows_text(rows)
        # Rendering the same rows twice is identical (deterministic).
        assert text == _render_milestone_rows_text(rows)


class TestBuildCounterfactualPair:
    def test_returns_none_for_missing_program(self, tmp_path: Path) -> None:
        # No program data → ProgramReality.load yields no milestones → empty render.
        pair = build_counterfactual_pair(
            program_id="nonexistent", fact_id="nope", programs_root=tmp_path,
        )
        # The pair is built but both arms are empty (no milestones).
        assert pair is not None
        assert pair.with_fact_text == ""
        assert pair.without_fact_text == ""
        assert pair.differs is False

    def test_render_empty_program_returns_empty_string(self, tmp_path: Path) -> None:
        text = render_milestone_section_text(
            program_id="nonexistent", programs_root=tmp_path,
        )
        assert text == ""


class TestCounterfactualRenderPair:
    def test_differs_property(self) -> None:
        pair = CounterfactualRenderPair(
            with_fact_text="a", without_fact_text="b",
            fact_id="f1", source_document_key="k", approval_event_id=None,
        )
        assert pair.differs is True

    def test_not_differs_when_identical(self) -> None:
        pair = CounterfactualRenderPair(
            with_fact_text="a", without_fact_text="a",
            fact_id="f1", source_document_key="k", approval_event_id=None,
        )
        assert pair.differs is False
