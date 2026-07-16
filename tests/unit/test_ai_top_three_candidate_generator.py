"""ADF-W4.7: unit tests for src/ai/top_three_candidate_generator.py."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.ai.top_three_candidate_generator import generate_top_three_candidates
from src.core.program_synthesis import ProgramSynthesisRequest, SynthesisInputItem
from src.core.quality_gates.ai_release_audit import released_terminal_for_run

_AS_OF = datetime(2026, 7, 1, tzinfo=timezone.utc)


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


def _request(items: tuple[SynthesisInputItem, ...] | None = None) -> ProgramSynthesisRequest:
    if items is None:
        items = (
            SynthesisInputItem(category="strategic_risk", item_id="risk-1", summary="Vendor delay."),
            SynthesisInputItem(category="kusto_slo_breach", item_id="sig-1", summary="Safety pass rate breach."),
            SynthesisInputItem(category="critical_path_milestone", item_id="ms-1", summary="Milestone at risk."),
        )
    return ProgramSynthesisRequest(program_id="xpf", as_of=_AS_OF, items=items)


def _valid_response() -> dict[str, Any]:
    return {
        "candidates": [
            {
                "item_id": "risk-1",
                "reason": "Vendor delay threatens Q3.",
                "evidence_refs": ["risk-1"],
                "urgency": "high",
                "decision_or_action_needed": "Escalate to vendor management.",
                "owner": "alex",
                "confidence": "high",
            },
            {
                "item_id": "sig-1",
                "reason": "Safety metric breaching SLO.",
                "evidence_refs": ["sig-1"],
                "urgency": "medium",
                "decision_or_action_needed": "Review safety dashboard.",
                "owner": None,
                "confidence": "medium",
            },
        ]
    }


def test_empty_request_short_circuits_without_calling_the_provider(tmp_path: Path) -> None:
    client = _FakeClient(response={"candidates": []})
    result = generate_top_three_candidates(_request(items=()), client=client, programs_root=tmp_path / "programs")
    assert result == ()
    assert client.calls == 0


def test_valid_response_is_released(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    candidates = generate_top_three_candidates(_request(), client=client, programs_root=programs_root)

    assert len(candidates) == 2
    assert candidates[0].item_id == "risk-1"
    assert candidates[0].urgency == "high"
    assert candidates[0].owner_alias == "alex"
    assert candidates[1].owner_alias is None
    assert released_terminal_for_run(candidates[0].ai_run_id, program_id="xpf", programs_root=programs_root).value == "released"


def test_more_than_three_candidates_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["candidates"].append(
        {
            "item_id": "ms-1", "reason": "x", "evidence_refs": ["ms-1"], "urgency": "low",
            "decision_or_action_needed": "y", "owner": None, "confidence": "low",
        }
    )
    response["candidates"].append(
        {
            "item_id": "risk-1", "reason": "z", "evidence_refs": ["risk-1"], "urgency": "low",
            "decision_or_action_needed": "w", "owner": None, "confidence": "low",
        }
    )
    client = _FakeClient(response=response)

    candidates = generate_top_three_candidates(_request(), client=client, programs_root=programs_root)
    assert candidates == ()


def test_candidate_citing_unknown_item_id_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = {
        "candidates": [
            {
                "item_id": "not-a-real-item", "reason": "x", "evidence_refs": ["not-a-real-item"],
                "urgency": "high", "decision_or_action_needed": "y", "owner": None, "confidence": "high",
            }
        ]
    }
    client = _FakeClient(response=response)

    candidates = generate_top_three_candidates(_request(), client=client, programs_root=programs_root)
    assert candidates == ()


def test_duplicate_item_id_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = {
        "candidates": [
            {"item_id": "risk-1", "reason": "a", "evidence_refs": ["risk-1"], "urgency": "high", "decision_or_action_needed": "b", "owner": None, "confidence": "high"},
            {"item_id": "risk-1", "reason": "c", "evidence_refs": ["risk-1"], "urgency": "low", "decision_or_action_needed": "d", "owner": None, "confidence": "low"},
        ]
    }
    client = _FakeClient(response=response)

    candidates = generate_top_three_candidates(_request(), client=client, programs_root=programs_root)
    assert candidates == ()


def test_candidate_with_no_evidence_refs_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = {"candidates": [{"item_id": "risk-1", "reason": "a", "evidence_refs": [], "urgency": "high", "decision_or_action_needed": "b", "owner": None, "confidence": "high"}]}
    client = _FakeClient(response=response)

    candidates = generate_top_three_candidates(_request(), client=client, programs_root=programs_root)
    assert candidates == ()


def test_no_candidates_produced_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response={"candidates": []})
    candidates = generate_top_three_candidates(_request(), client=client, programs_root=programs_root)
    assert candidates == ()


def test_provider_exception_is_discarded_not_raised(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(error=RuntimeError("transport failed"))
    candidates = generate_top_three_candidates(_request(), client=client, programs_root=programs_root)
    assert candidates == ()


def test_repeat_identical_request_hits_the_cache_no_second_provider_call(tmp_path: Path) -> None:
    # ADF-W5.1: top_three_candidate_generator is the third live cache adopter.
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    first = generate_top_three_candidates(_request(), client=client, programs_root=programs_root)
    second = generate_top_three_candidates(_request(), client=client, programs_root=programs_root)

    assert len(first) == 2
    assert len(second) == 2
    assert client.calls == 1  # second call served from the AI result cache
    assert second[0].item_id == first[0].item_id
    assert second[0].ai_run_id != first[0].ai_run_id


def test_different_request_items_does_not_hit_the_cache(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    other_request = _request(
        items=(
            SynthesisInputItem(category="strategic_risk", item_id="risk-9", summary="A totally different risk."),
        )
    )

    generate_top_three_candidates(_request(), client=client, programs_root=programs_root)
    generate_top_three_candidates(other_request, client=client, programs_root=programs_root)

    assert client.calls == 2
