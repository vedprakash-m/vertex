"""ADF-W3.3: unit tests for src/ai/meeting_action_extractor.py -- the
residual-content LLM extraction tier."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.ai.meeting_action_extractor import extract_llm_meeting_actions, run_meeting_action_extraction_pipeline
from src.core.ledger.event_log import read_events
from src.core.models import RiskLevel, WorkItem


class _FakeClient:
    def __init__(self, *, response: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls = 0

    def chat(self, system: str, user: str, *, max_tokens: int = 800, prompt_version: str | None = None) -> str:
        raise NotImplementedError

    def structured(
        self,
        system: str,
        user: str,
        *,
        parser: Callable[[dict[str, Any]], Any],
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return parser(self.response)


def _work_item(item_id: int) -> WorkItem:
    return WorkItem(
        id=item_id, type="Task", title=f"Item {item_id}", state="Active", assigned_to="Priya",
        assigned_to_email="priya@example.com", area_path="One\\Demo", iteration_path="Sprint 1",
        target_date=None, risk_level=RiskLevel.LOW, tags=[], custom_fields={},
    )


def test_empty_residual_text_short_circuits_without_calling_the_provider(tmp_path: Path) -> None:
    client = _FakeClient(response={"actions": []})
    result = extract_llm_meeting_actions(
        program_id="xpf", meeting_ref="m1", residual_text="   ", items=(), client=client, programs_root=tmp_path / "programs"
    )
    assert result == ()
    assert client.calls == 0


def test_valid_response_with_verbatim_source_span_is_released(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    residual_text = "We agreed that Priya will follow up with legal by end of week."
    response = {
        "actions": [
            {
                "commitment": "Follow up with legal",
                "owner": "priya",
                "due": None,
                "linked_work_item": 1001,
                "blocks": [],
                "source_span": "Priya will follow up with legal by end of week.",
            }
        ]
    }
    client = _FakeClient(response=response)
    actions = extract_llm_meeting_actions(
        program_id="xpf", meeting_ref="m1", residual_text=residual_text,
        items=(_work_item(1001),), client=client, programs_root=programs_root,
    )
    assert len(actions) == 1
    assert actions[0].commitment == "Follow up with legal"
    assert actions[0].owner_alias == "priya"
    assert actions[0].linked_work_item_id == 1001
    assert actions[0].extraction_method == "llm"

    events = read_events("xpf", programs_root=programs_root)
    release_decisions = [e for e in events if e.event_type == "ai.release_decision.v1"]
    assert len(release_decisions) == 1
    assert release_decisions[0].payload["terminal"] == "released"


def test_fabricated_source_span_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    residual_text = "Nothing about legal follow-up was actually said here."
    response = {
        "actions": [
            {
                "commitment": "Follow up with legal",
                "owner": "priya",
                "due": None,
                "linked_work_item": None,
                "blocks": [],
                "source_span": "Priya will follow up with legal by end of week.",
            }
        ]
    }
    client = _FakeClient(response=response)
    actions = extract_llm_meeting_actions(
        program_id="xpf", meeting_ref="m1", residual_text=residual_text, items=(), client=client, programs_root=programs_root,
    )
    assert actions == ()

    events = read_events("xpf", programs_root=programs_root)
    release_decisions = [e for e in events if e.event_type == "ai.release_decision.v1"]
    assert release_decisions[0].payload["terminal"] == "rejected"


def test_linked_work_item_outside_allowed_set_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    residual_text = "Alex committed to ship the doc."
    response = {
        "actions": [
            {
                "commitment": "Ship the doc",
                "owner": "alex",
                "due": None,
                "linked_work_item": 9999,
                "blocks": [],
                "source_span": "Alex committed to ship the doc.",
            }
        ]
    }
    client = _FakeClient(response=response)
    actions = extract_llm_meeting_actions(
        program_id="xpf", meeting_ref="m1", residual_text=residual_text,
        items=(_work_item(1001),), client=client, programs_root=programs_root,
    )
    assert actions == ()


def test_provider_exception_is_discarded_not_raised(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(error=RuntimeError("transport failed"))
    actions = extract_llm_meeting_actions(
        program_id="xpf", meeting_ref="m1", residual_text="some content", items=(), client=client, programs_root=programs_root,
    )
    assert actions == ()


def test_non_dict_response_is_discarded(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=None)
    actions = extract_llm_meeting_actions(
        program_id="xpf", meeting_ref="m1", residual_text="some content", items=(), client=client, programs_root=programs_root,
    )
    assert actions == ()


def test_missing_actions_list_is_discarded(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response={"not_actions": []})
    actions = extract_llm_meeting_actions(
        program_id="xpf", meeting_ref="m1", residual_text="some content", items=(), client=client, programs_root=programs_root,
    )
    assert actions == ()


def test_oversized_residual_text_is_discarded_before_calling_the_provider(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response={"actions": []})
    actions = extract_llm_meeting_actions(
        program_id="xpf", meeting_ref="m1", residual_text="x" * 200_001, items=(), client=client, programs_root=programs_root,
    )
    assert actions == ()
    assert client.calls == 0


def test_repeat_identical_request_hits_the_cache_no_second_provider_call(tmp_path: Path) -> None:
    # ADF-W5.1: meeting_action_extractor is the second live cache adopter
    # (after risk_proposal_generator).
    programs_root = tmp_path / "programs"
    residual_text = "We agreed that Priya will follow up with legal by end of week."
    response = {
        "actions": [
            {
                "commitment": "Follow up with legal",
                "owner": "priya",
                "due": None,
                "linked_work_item": 1001,
                "blocks": [],
                "source_span": "Priya will follow up with legal by end of week.",
            }
        ]
    }
    client = _FakeClient(response=response)

    first = extract_llm_meeting_actions(
        program_id="xpf", meeting_ref="m1", residual_text=residual_text,
        items=(_work_item(1001),), client=client, programs_root=programs_root,
    )
    second = extract_llm_meeting_actions(
        program_id="xpf", meeting_ref="m1", residual_text=residual_text,
        items=(_work_item(1001),), client=client, programs_root=programs_root,
    )

    assert len(first) == 1
    assert len(second) == 1
    assert client.calls == 1  # second call served from the AI result cache
    assert second[0].commitment == first[0].commitment


def test_different_residual_text_does_not_hit_the_cache(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = {
        "actions": [
            {
                "commitment": "Follow up with legal",
                "owner": "priya",
                "due": None,
                "linked_work_item": 1001,
                "blocks": [],
                "source_span": "Priya will follow up with legal by end of week.",
            }
        ]
    }
    client = _FakeClient(response=response)

    extract_llm_meeting_actions(
        program_id="xpf", meeting_ref="m1",
        residual_text="We agreed that Priya will follow up with legal by end of week.",
        items=(_work_item(1001),), client=client, programs_root=programs_root,
    )
    extract_llm_meeting_actions(
        program_id="xpf", meeting_ref="m1",
        residual_text="A completely different residual transcript segment about something else.",
        items=(_work_item(1001),), client=client, programs_root=programs_root,
    )

    assert client.calls == 2


# ---------------------------------------------------------------------------
# Full pipeline (deterministic + LLM residual + merge + validate)
# ---------------------------------------------------------------------------


def test_pipeline_merges_deterministic_and_llm_actions(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    transcript = (
        "Action: Ship the deployment doc | owner=alex | wi=1001\n"
        "Separately, Priya mentioned she will follow up with legal on the contract."
    )
    response = {
        "actions": [
            {
                "commitment": "Follow up with legal on the contract",
                "owner": "priya",
                "due": None,
                "linked_work_item": None,
                "blocks": [],
                "source_span": "Priya mentioned she will follow up with legal on the contract.",
            }
        ]
    }
    client = _FakeClient(response=response)
    result = run_meeting_action_extraction_pipeline(
        program_id="xpf", meeting_ref="m1", transcript_text=transcript,
        items=(_work_item(1001),), client=client, programs_root=programs_root,
    )
    assert len(result.actions) == 2
    methods = {action.extraction_method for action in result.actions}
    assert methods == {"deterministic", "llm"}
    assert all(action.status == "staged" for action in result.actions)
    # The deterministic marker line must not have been re-sent to the LLM as residual content.
    assert client.calls == 1


def test_pipeline_rejects_invalid_action_without_dropping_the_valid_one(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    transcript = (
        "Action: Ship the deployment doc | owner=alex | wi=1001\n"
        "Action: Do something | wi=9999"
    )
    client = _FakeClient(response={"actions": []})
    result = run_meeting_action_extraction_pipeline(
        program_id="xpf", meeting_ref="m1", transcript_text=transcript,
        items=(_work_item(1001),), client=client, programs_root=programs_root,
    )
    assert len(result.actions) == 2
    statuses = {action.commitment: action.status for action in result.actions}
    assert statuses["Ship the deployment doc"] == "staged"
    assert statuses["Do something"] == "rejected"


def test_pipeline_with_no_residual_content_does_not_call_the_provider(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    transcript = "Action: Ship the deployment doc | owner=alex | wi=1001"
    client = _FakeClient(response={"actions": []})
    result = run_meeting_action_extraction_pipeline(
        program_id="xpf", meeting_ref="m1", transcript_text=transcript,
        items=(_work_item(1001),), client=client, programs_root=programs_root,
    )
    assert len(result.actions) == 1
    assert client.calls == 0
