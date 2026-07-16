"""ADF-W4.7 remainder: unit tests for src/ai/governance_decision_brief_generator.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.ai.governance_decision_brief_generator import generate_governance_decision_brief
from src.core.governance_decision_brief import GovernanceDecisionRequest
from src.core.quality_gates.ai_release_audit import released_terminal_for_run


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


def _request() -> GovernanceDecisionRequest:
    return GovernanceDecisionRequest(
        program_id="xpf",
        decision_ask_id="ask-1",
        decision_text="Should we escalate the vendor delay to leadership?",
        evidence_texts=("Vendor confirmed a two-week slip.",),
        evidence_refs=("sig-1",),
    )


def _valid_response() -> dict[str, Any]:
    return {
        "decision": "Escalate the vendor delay to leadership.",
        "context": "Vendor X has slipped its delivery date twice.",
        "options": [
            {"label": "Escalate now", "tradeoffs": "Faster resolution, may strain the relationship."},
            {"label": "Wait one more sprint", "tradeoffs": "Preserves the relationship, risks the milestone."},
        ],
        "recommendation": "Escalate now given the milestone risk.",
        "consequences_of_delay": "The Q3 milestone slips further with each week of delay.",
        "owner": "alex",
        "due_date": "2026-07-15",
        "evidence_refs": ["sig-1"],
    }


def test_valid_response_is_released(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    proposal = generate_governance_decision_brief(_request(), client=client, programs_root=programs_root)

    assert proposal is not None
    assert proposal.decision == "Escalate the vendor delay to leadership."
    assert len(proposal.options) == 2
    assert proposal.owner_alias == "alex"
    assert released_terminal_for_run(proposal.ai_run_id, program_id="xpf", programs_root=programs_root).value == "released"


def test_single_option_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["options"] = [{"label": "Escalate now", "tradeoffs": "x"}]
    client = _FakeClient(response=response)

    proposal = generate_governance_decision_brief(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_empty_recommendation_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["recommendation"] = ""
    client = _FakeClient(response=response)

    proposal = generate_governance_decision_brief(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_evidence_ref_outside_ask_evidence_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["evidence_refs"] = ["sig-999"]
    client = _FakeClient(response=response)

    proposal = generate_governance_decision_brief(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_option_with_empty_tradeoffs_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["options"][1]["tradeoffs"] = ""
    client = _FakeClient(response=response)

    proposal = generate_governance_decision_brief(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_invalid_due_date_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["due_date"] = "not-a-date"
    client = _FakeClient(response=response)

    proposal = generate_governance_decision_brief(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_null_owner_and_due_date_are_accepted(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["owner"] = None
    response["due_date"] = None
    client = _FakeClient(response=response)

    proposal = generate_governance_decision_brief(_request(), client=client, programs_root=programs_root)
    assert proposal is not None
    assert proposal.owner_alias is None
    assert proposal.due_date is None


def test_provider_exception_is_discarded_not_raised(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(error=RuntimeError("transport failed"))

    proposal = generate_governance_decision_brief(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_oversized_request_is_discarded_before_calling_the_provider(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    oversized_request = GovernanceDecisionRequest(
        program_id="xpf", decision_ask_id="ask-1", decision_text="x" * 200_001,
        evidence_texts=(), evidence_refs=(),
    )
    client = _FakeClient(response=_valid_response())

    proposal = generate_governance_decision_brief(oversized_request, client=client, programs_root=programs_root)
    assert proposal is None
    assert client.calls == 0


def test_repeat_identical_request_hits_the_cache_no_second_provider_call(tmp_path: Path) -> None:
    # ADF-W5.1: governance_decision_brief_generator is the fourth live cache adopter.
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    first = generate_governance_decision_brief(_request(), client=client, programs_root=programs_root)
    second = generate_governance_decision_brief(_request(), client=client, programs_root=programs_root)

    assert first is not None
    assert second is not None
    assert client.calls == 1  # second call served from the AI result cache
    assert second.decision == first.decision
    assert second.ai_run_id != first.ai_run_id


def test_different_decision_ask_does_not_hit_the_cache(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    other_request = GovernanceDecisionRequest(
        program_id="xpf", decision_ask_id="ask-2", decision_text="Should we replace the vendor entirely?",
        evidence_texts=("Vendor has slipped three times.",), evidence_refs=("sig-9",),
    )

    generate_governance_decision_brief(_request(), client=client, programs_root=programs_root)
    generate_governance_decision_brief(other_request, client=client, programs_root=programs_root)

    assert client.calls == 2
