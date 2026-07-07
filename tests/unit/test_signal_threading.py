from __future__ import annotations

from datetime import datetime, timezone

from src.commands.report import _signal_context_lines
from src.core.models import Confidence
from src.core.models_v2 import Signal


def test_signal_context_lines_group_threaded_signals() -> None:
    threaded_older = Signal(
        id="sig-001",
        timestamp=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WI:1234",),
        text="Partner mail flagged the rollout dependency.",
        raw_ref="msg-1",
        confidence=Confidence.MEDIUM,
        metadata={"message_id": "msg-1"},
        thread_id="rollout-risk",
    )
    threaded_newer = Signal(
        id="sig-002",
        timestamp=datetime(2026, 5, 8, 13, 0, tzinfo=timezone.utc),
        source="manual",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WI:1234",),
        text="Manual follow-up confirmed the same concern.",
        raw_ref=None,
        confidence=Confidence.HIGH,
        metadata={"author": "maintainer"},
        thread_id="rollout-risk",
    )
    unthreaded = Signal(
        id="sig-003",
        timestamp=datetime(2026, 5, 8, 14, 0, tzinfo=timezone.utc),
        source="ado/revision",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WI:5678",),
        text="Target date slipped to June 30.",
        raw_ref=None,
        confidence=Confidence.HIGH,
        metadata={"field": "TargetDate"},
    )

    lines = _signal_context_lines(
        (threaded_older, threaded_newer, unthreaded),
        item_ids={1234, 5678},
        workstream_ids=("acme",),
        limit=5,
    )

    assert len(lines) == 2
    assert lines[0].startswith("Approved signal 2026-05-08T14:00:00+00:00 [ado/revision]: Target date slipped to June 30.")
    assert lines[1].startswith("Approved signal thread rollout-risk: ")
    assert "Manual follow-up confirmed the same concern." in lines[1]
    assert "Partner mail flagged the rollout dependency." in lines[1]