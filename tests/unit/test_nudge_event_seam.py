"""Tests for append_nudge_event — Phase 0 §6.7 (D-6 fact single-seam).

Verifies that append_nudge_event:
  1. Delegates to append_program_event (not a direct DB write)
  2. Applies the correct FactPrecedence for each nudge fact type
  3. Accepts unknown fact types (defaults to RAW_TELEMETRY)
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core.program_fact_store import append_nudge_event
from src.core.program_fact_store import FactPrecedence


# ---------------------------------------------------------------------------
# Precedence mapping (mirrors _NUDGE_PRECEDENCE in the implementation)
# ---------------------------------------------------------------------------


EXPECTED_PRECEDENCE = {
    "event.nudge.generated": FactPrecedence.RAW_TELEMETRY,
    "event.nudge.sent_attested": FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
    "event.nudge.evaluated": FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
    "event.nudge.waiver_created": FactPrecedence.ACTIVE_PM_JUDGMENT,
}


class TestAppendNudgeEventPrecedence:
    """Verify fact-type → FactPrecedence mappings without touching the DB."""

    def test_delegates_to_append_program_event(self):
        with patch("src.core.program_fact_store.append_program_event") as mock_ape:
            mock_ape.return_value = MagicMock()
            append_nudge_event("nova", "event.nudge.generated", {"run_id": "r1"}, db_root=Path("/tmp/fake"))
        mock_ape.assert_called_once()

    def test_generated_fact_type_calls_through(self):
        with patch("src.core.program_fact_store.append_program_event") as mock_ape:
            mock_ape.return_value = MagicMock()
            append_nudge_event("nova", "event.nudge.generated", {"run_id": "r2"}, db_root=Path("/tmp/fake"))
        assert mock_ape.call_args.kwargs["precedence"] is FactPrecedence.RAW_TELEMETRY

    def test_sent_attested_fact_type_calls_through(self):
        with patch("src.core.program_fact_store.append_program_event") as mock_ape:
            mock_ape.return_value = MagicMock()
            append_nudge_event("nova", "event.nudge.sent_attested", {"run_id": "r3"}, db_root=Path("/tmp/fake"))
        assert mock_ape.call_args.kwargs["precedence"] is FactPrecedence.CONFIRMED_GOVERNANCE_DECISION

    def test_evaluated_fact_type_calls_through(self):
        with patch("src.core.program_fact_store.append_program_event") as mock_ape:
            mock_ape.return_value = MagicMock()
            append_nudge_event("nova", "event.nudge.evaluated", {"run_id": "r4"}, db_root=Path("/tmp/fake"))
        assert mock_ape.call_args.kwargs["precedence"] is FactPrecedence.VERIFIED_SYSTEM_SIGNAL

    def test_waiver_created_fact_type_calls_through(self):
        with patch("src.core.program_fact_store.append_program_event") as mock_ape:
            mock_ape.return_value = MagicMock()
            append_nudge_event("nova", "event.nudge.waiver_created", {"run_id": "r5"}, db_root=Path("/tmp/fake"))
        assert mock_ape.call_args.kwargs["precedence"] is FactPrecedence.ACTIVE_PM_JUDGMENT

    def test_unknown_fact_type_still_calls_through(self):
        with patch("src.core.program_fact_store.append_program_event") as mock_ape:
            mock_ape.return_value = MagicMock()
            append_nudge_event("nova", "event.nudge.unknown", {"run_id": "r6"}, db_root=Path("/tmp/fake"))
        assert mock_ape.call_args.kwargs["precedence"] is FactPrecedence.RAW_TELEMETRY

    def test_program_event_receives_program_id(self):
        with patch("src.core.program_fact_store.append_program_event") as mock_ape:
            mock_ape.return_value = MagicMock()
            append_nudge_event("armada", "event.nudge.generated", {"run_id": "r7"}, db_root=Path("/tmp/fake"))
        first_positional_arg = mock_ape.call_args[0][0]
        assert first_positional_arg == "armada"

    def test_program_event_receives_fact_type_in_metadata(self):
        from src.core.program_fact_store import ProgramEvent
        captured_event: list[ProgramEvent] = []

        def _capture(program_id, event, *, precedence=None, recorded_at=None, db_root=None, home_root=None):
            captured_event.append(event)
            return MagicMock()

        with patch("src.core.program_fact_store.append_program_event", side_effect=_capture):
            append_nudge_event("nova", "event.nudge.generated", {"run_id": "r8"}, db_root=Path("/tmp/fake"))

        assert len(captured_event) == 1
        evt = captured_event[0]
        assert evt.fact_type == "event.nudge.generated"
        assert evt.metadata.get("run_id") == "r8"
        assert evt.metadata.get("fact_type") == "event.nudge.generated"

    def test_explicit_precedence_override(self):
        """Caller can override precedence."""
        with patch("src.core.program_fact_store.append_program_event") as mock_ape:
            mock_ape.return_value = MagicMock()
            append_nudge_event(
                "nova",
                "event.nudge.generated",
                {"run_id": "r9"},
                precedence=FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
                db_root=Path("/tmp/fake"),
            )
        assert mock_ape.call_args.kwargs["precedence"] is FactPrecedence.CONFIRMED_GOVERNANCE_DECISION
