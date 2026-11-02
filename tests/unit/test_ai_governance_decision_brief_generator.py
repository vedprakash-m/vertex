"""ADF-W4.7 remainder: unit tests for src/ai/governance_decision_brief_generator.py."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import pytest

import src.ai.governance_decision_brief_generator as brief_generator
from src.ai.governance_decision_brief_generator import generate_governance_decision_brief
from src.core.governance_decision_brief import GovernanceDecisionRequest
from src.core.ledger.event_log import read_events
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
        program_id="acme",
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
            {
                "label": "Escalate now",
                "tradeoffs": "Faster resolution, may strain the relationship.",
                "evidence_refs": ["sig-1"],
                "unknowns": [],
            },
            {
                "label": "Wait one more sprint",
                "tradeoffs": "Preserves the relationship, risks the milestone.",
                "evidence_refs": [],
                "unknowns": ["Whether the vendor will slip again next sprint."],
            },
        ],
        "recommendation": "Escalate now given the milestone risk.",
        "consequences_of_delay": "The Q3 milestone slips further with each week of delay.",
        "owner": "alex",
        "due_date": "2026-07-15",
        "follow_up_action": "Send the escalation email to leadership by Friday.",
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
    assert proposal.follow_up_action == "Send the escalation email to leadership by Friday."
    assert proposal.options[0].evidence_refs == ("sig-1",)
    assert proposal.options[1].unknowns == ("Whether the vendor will slip again next sprint.",)
    assert released_terminal_for_run(proposal.ai_run_id, program_id="acme", programs_root=programs_root).value == "released"


@pytest.mark.parametrize(
    ("evidence_texts", "evidence_refs", "response_ref"),
    [
        (("Vendor confirmed a two-week slip.",), ("sig-1", "orphan-ref"), "orphan-ref"),
        (("Vendor confirmed a two-week slip.", "orphan text"), ("sig-1",), "sig-1"),
    ],
)
def test_unpaired_evidence_is_discarded_before_policy_cache_or_provider(
    evidence_texts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    response_ref: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    programs_root = tmp_path / "programs"
    run_id = "unpaired-brief-run"
    request = replace(_request(), evidence_texts=evidence_texts, evidence_refs=evidence_refs)
    response = _valid_response()
    response["evidence_refs"] = [response_ref]
    response["options"][0]["evidence_refs"] = [response_ref]
    client = _FakeClient(response=response)
    policy_calls: list[object] = []
    cache_calls: list[object] = []
    load_policy = brief_generator.load_ai_feature_policy
    cached_response = brief_generator._cached_response

    def track_policy(*args: object, **kwargs: object) -> object:
        policy_calls.append(args)
        return load_policy(*args, **kwargs)

    def track_cache(*args: object, **kwargs: object) -> object:
        cache_calls.append(args)
        return cached_response(*args, **kwargs)

    monkeypatch.setattr(brief_generator, "new_ai_run_id", lambda: run_id)
    monkeypatch.setattr(brief_generator, "load_ai_feature_policy", track_policy)
    monkeypatch.setattr(brief_generator, "_cached_response", track_cache)

    assert generate_governance_decision_brief(request, client=client, programs_root=programs_root) is None
    assert client.calls == 0
    assert policy_calls == []
    assert cache_calls == []
    events = [event for event in read_events("acme", programs_root=programs_root) if event.payload.get("ai_run_id") == run_id]
    assert [event.payload["state"] for event in events if event.event_type == "ai.run_lifecycle.v1"] == ["planned"]
    assert [event.payload["terminal"] for event in events if event.event_type == "ai.release_decision.v1"] == ["discarded"]


def test_both_empty_evidence_is_supported(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["evidence_refs"] = []
    response["options"][0]["evidence_refs"] = []
    response["options"][0]["unknowns"] = ["Evidence was not supplied."]
    client = _FakeClient(response=response)

    proposal = generate_governance_decision_brief(
        replace(_request(), evidence_texts=(), evidence_refs=()),
        client=client,
        programs_root=programs_root,
    )

    assert proposal is not None
    assert proposal.evidence_refs == ()
    assert client.calls == 1


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


def test_null_owner_is_rejected(tmp_path: Path) -> None:
    # RELO-W4.6 (Sec 8.4.7): "editorial review without these fields is not a
    # decision brief" -- owner_alias is now enforced at release, reversing
    # the pre-W4.6 behavior where a null owner was silently accepted.
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["owner"] = None
    client = _FakeClient(response=response)

    proposal = generate_governance_decision_brief(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_null_due_date_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["due_date"] = None
    client = _FakeClient(response=response)

    proposal = generate_governance_decision_brief(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_missing_follow_up_action_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["follow_up_action"] = ""
    client = _FakeClient(response=response)

    proposal = generate_governance_decision_brief(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_option_with_neither_evidence_nor_unknowns_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["options"][0]["evidence_refs"] = []
    response["options"][0]["unknowns"] = []
    client = _FakeClient(response=response)

    proposal = generate_governance_decision_brief(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_option_evidence_ref_outside_ask_evidence_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["options"][0]["evidence_refs"] = ["sig-999"]
    client = _FakeClient(response=response)

    proposal = generate_governance_decision_brief(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_provider_exception_is_discarded_not_raised(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(error=RuntimeError("transport failed"))

    proposal = generate_governance_decision_brief(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_oversized_request_is_discarded_before_calling_the_provider(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    oversized_request = GovernanceDecisionRequest(
        program_id="acme", decision_ask_id="ask-1", decision_text="x" * 200_001,
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
        program_id="acme", decision_ask_id="ask-2", decision_text="Should we replace the vendor entirely?",
        evidence_texts=("Vendor has slipped three times.",), evidence_refs=("sig-9",),
    )

    generate_governance_decision_brief(_request(), client=client, programs_root=programs_root)
    generate_governance_decision_brief(other_request, client=client, programs_root=programs_root)

    assert client.calls == 2
