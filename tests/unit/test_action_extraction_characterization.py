"""RELO-W4.5 / OD-12: characterization of the two action-extraction pipelines.

Pins the current deterministic (non-LLM) behavior of:
  A. meeting pipeline -- ``src.core.meeting_action`` (+ ``src.ai.meeting_action_extractor``)
  B. signal pipeline  -- ``src.ai.action_extractor`` (+ ``src.core.action_extractor_basic``)
on the same fixed transcript. These tests record divergences; they are not a
statement that either behavior is correct.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.ai.action_extractor import ActionExtractor
from src.ai.meeting_action_extractor import run_meeting_action_extraction_pipeline
from src.core.action_extraction_parity import (
    ParityFinding,
    check_action_parity,
    normalize_action_items,
    normalize_meeting_actions,
)
from src.core.action_extractor_basic import extract_actions_from_signals
from src.core.meeting_action import extract_deterministic_meeting_actions
from src.core.models import Confidence
from src.core.models_v2 import ActionSourceType, ActionStatus, Signal

TRANSCRIPT = (
    "Kickoff notes\n"
    "Action: Ship ramp packet | owner=Priya@example.com | due=2026-12-01\n"
    "Action: Update rollout plan | owner=alex | wi=WI:42\n"
)
MARKERS_ONLY = "\n".join(line for line in TRANSCRIPT.splitlines() if line.startswith("Action:"))


class _NoCallClient:
    def __init__(self) -> None:
        self.calls = 0

    def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
        del system, user, parser, max_tokens, prompt_version
        self.calls += 1
        raise AssertionError("characterization must stay on the deterministic path")


def _signal(text: str = TRANSCRIPT, source: str = "workiq/teams") -> Signal:
    return Signal(
        id="sig-1",
        timestamp=datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc),
        source=source,
        program_id="acme",
        workstream_id=None,
        entity_refs=(),
        text=text,
        raw_ref=None,
        confidence=Confidence.MEDIUM,
        metadata={"sender_alias": "sam"},
        thread_id=None,
    )


def _row(action) -> tuple:
    return (action.commitment, action.owner_alias, action.due_date, action.linked_work_item_id, action.blocks)


# --- Pipeline A: meeting markers ------------------------------------------------


def test_meeting_pipeline_parses_both_marker_lines_including_wi_key() -> None:
    actions = extract_deterministic_meeting_actions(program_id="acme", meeting_ref="m1", transcript_text=TRANSCRIPT)

    assert [a.id for a in actions] == ["deterministic-action-m1-1", "deterministic-action-m1-2"]
    assert [_row(a) for a in actions] == [
        ("Ship ramp packet", "priya", date(2026, 12, 1), None, ()),
        ("Update rollout plan", "alex", None, 42, ()),
    ]
    assert {a.extraction_method for a in actions} == {"deterministic"}
    assert {a.status for a in actions} == {"staged"}
    assert actions[0].source_span == "Action: Ship ramp packet | owner=Priya@example.com | due=2026-12-01"


def test_meeting_pipeline_skips_refs_key_and_invalid_due_but_counts_index() -> None:
    text = (
        "Action: Bad due | due=next week\n"
        "Action: Uses refs | refs=WI:1\n"
        "Action: Kept | blocks=WI:7, WI:7 ,WI:8\n"
    )

    actions = extract_deterministic_meeting_actions(program_id="acme", meeting_ref="m2", transcript_text=text)

    # "refs=" is not an allowed key (payload rejected before indexing);
    # an invalid due consumes an index slot before being skipped.
    assert [(a.id, a.commitment, a.owner_alias, a.blocks) for a in actions] == [
        ("deterministic-action-m2-2", "Kept", None, ("WI:7", "WI:8")),
    ]


def test_meeting_pipeline_end_to_end_skips_llm_when_no_residual(tmp_path: Path) -> None:
    client = _NoCallClient()

    result = run_meeting_action_extraction_pipeline(
        program_id="acme",
        meeting_ref="m1",
        transcript_text=MARKERS_ONLY,
        items=(),
        client=client,
        programs_root=tmp_path,
    )

    assert client.calls == 0
    assert result.warnings == ()
    assert [(a.commitment, a.status, a.rejection_reason) for a in result.actions] == [
        ("Ship ramp packet", "staged", None),
        (
            "Update rollout plan",
            "rejected",
            "linked_work_item WI:42 is not in the allowed work item set.",
        ),
    ]


# --- Pipeline B: signal extractor ----------------------------------------------


def test_signal_pipeline_marker_with_wi_key_falls_back_to_heuristic_sentence() -> None:
    client = _NoCallClient()

    actions = ActionExtractor(client=client).extract_actions(program_id="acme", signals=(_signal(),))

    assert client.calls == 0
    assert [(a.text, a.owner_alias, a.due_date, a.linked_work_item_ids) for a in actions] == [
        ("Ship ramp packet", "priya", date(2026, 12, 1), ()),
        # "wi=" is not an allowed key here ("refs=" is), so the line is
        # malformed and re-extracted by the heuristic: full line kept as text,
        # "owner=alex" ignored in favor of the sender alias.
        ("Action: Update rollout plan | owner=alex | wi=WI:42", "sam", None, (42,)),
    ]
    assert {a.source_type for a in actions} == {ActionSourceType.MEETING_TRANSCRIPT}
    assert {a.status for a in actions} == {ActionStatus.PROPOSED}
    assert {a.source_signal_id for a in actions} == {"sig-1"}


def test_signal_pipeline_accepts_refs_key_marker() -> None:
    client = _NoCallClient()
    signal = _signal(text="Action: Update rollout plan | owner=alex | refs=WI:42, WI:43")

    actions = ActionExtractor(client=client).extract_actions(program_id="acme", signals=(signal,))

    assert client.calls == 0
    assert [(a.text, a.owner_alias, a.due_date, a.linked_work_item_ids) for a in actions] == [
        ("Update rollout plan", "alex", None, (42, 43)),
    ]


def test_signal_pipeline_ignores_non_meeting_sources() -> None:
    client = _NoCallClient()

    actions = ActionExtractor(client=client).extract_actions(
        program_id="acme", signals=(_signal(source="workiq/email"),)
    )

    assert actions == ()
    assert client.calls == 0


def test_basic_heuristic_keeps_full_marker_line_and_reads_at_domain_as_owner() -> None:
    actions = extract_actions_from_signals((_signal(),), "acme")

    assert [(a.text, a.owner_alias, a.due_date, a.linked_work_item_ids, a.source_type) for a in actions] == [
        (
            "Action: Ship ramp packet | owner=Priya@example.com | due=2026-12-01",
            "example.com",
            date(2026, 12, 1),
            (),
            ActionSourceType.SIGNAL,
        ),
        ("Action: Update rollout plan | owner=alex | wi=WI:42", "sam", None, (42,), ActionSourceType.SIGNAL),
    ]


# --- Cross-pipeline parity (observe-only) -------------------------------------


def test_same_transcript_parity_findings_pin_current_divergence() -> None:
    a = extract_deterministic_meeting_actions(program_id="acme", meeting_ref="m1", transcript_text=TRANSCRIPT)
    b = ActionExtractor(client=_NoCallClient()).extract_actions(program_id="acme", signals=(_signal(),))

    findings = check_action_parity(normalize_meeting_actions(a), normalize_action_items(b))

    wi_line = "action: update rollout plan | owner=alex | wi=wi:42"
    assert findings == (
        ParityFinding(kind="missing_in_a", key=wi_line, a_value=None, b_value=wi_line),
        ParityFinding(kind="missing_in_b", key="update rollout plan", a_value="update rollout plan", b_value=None),
    )