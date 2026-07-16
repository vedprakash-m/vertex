"""ADF-W2.9: unit tests for src/ai/program_synthesizer.py -- the Zone B
generator that runs the full ADF-W2.8 AI safety lifecycle for the first
live feature to call it."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from src.ai.program_synthesizer import generate_program_synthesis, generate_program_synthesis_via_context_gateway
from src.core.program_synthesis import ProgramSynthesisRequest, SynthesisInputItem, load_latest_released_program_synthesis
from src.core.quality_gates.ai_release_audit import ReleaseTerminal, released_terminal_for_run

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


def _request(items: tuple[SynthesisInputItem, ...] = ()) -> ProgramSynthesisRequest:
    if not items:
        items = (SynthesisInputItem(category="strategic_risk", item_id="risk-1", summary="Vendor delay."),)
    return ProgramSynthesisRequest(program_id="xpf", as_of=_AS_OF, items=items, coverage_notes=("note",))


def _valid_response() -> dict[str, Any]:
    return {
        "through_line": "The program is broadly healthy but vendor delay threatens the date.",
        "long_poles": ["risk-1"],
        "facts": ["Vendor X reported a delay."],
        "inferences": ["The delay may push the milestone."],
        "recommendations": [{"text": "Escalate vendor delay to leadership.", "evidence_refs": ["risk-1"]}],
    }


def test_valid_response_is_released_and_persisted(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    outcome = generate_program_synthesis(_request(), client=client, programs_root=programs_root)

    assert outcome.released is True
    assert outcome.findings == ()
    assert client.calls == 1
    assert outcome.synthesis is not None
    assert outcome.synthesis.through_line.startswith("The program is broadly healthy")
    assert outcome.synthesis.recommendations[0].evidence_refs == ("risk-1",)
    assert released_terminal_for_run(outcome.ai_run_id, program_id="xpf", programs_root=programs_root) is ReleaseTerminal.RELEASED

    reloaded = load_latest_released_program_synthesis("xpf", programs_root=programs_root)
    assert reloaded is not None
    assert reloaded.ai_run_id == outcome.ai_run_id


def test_recommendation_missing_evidence_refs_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["recommendations"] = [{"text": "Do something.", "evidence_refs": []}]
    client = _FakeClient(response=response)

    outcome = generate_program_synthesis(_request(), client=client, programs_root=programs_root)

    assert outcome.released is False
    assert outcome.synthesis is None
    assert any("no evidence_refs" in finding for finding in outcome.findings)
    assert released_terminal_for_run(outcome.ai_run_id, program_id="xpf", programs_root=programs_root) is ReleaseTerminal.REJECTED


def test_recommendation_citing_unknown_item_id_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["recommendations"] = [{"text": "Do something.", "evidence_refs": ["not-a-real-item"]}]
    client = _FakeClient(response=response)

    outcome = generate_program_synthesis(_request(), client=client, programs_root=programs_root)

    assert outcome.released is False
    assert any("unsupported causal claim" in finding for finding in outcome.findings)


def test_empty_through_line_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["through_line"] = ""
    client = _FakeClient(response=response)

    outcome = generate_program_synthesis(_request(), client=client, programs_root=programs_root)

    assert outcome.released is False
    assert outcome.synthesis is None


def test_no_recommendations_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["recommendations"] = []
    client = _FakeClient(response=response)

    outcome = generate_program_synthesis(_request(), client=client, programs_root=programs_root)

    assert outcome.released is False
    assert any("no recommendations produced" in finding for finding in outcome.findings)


def test_provider_exception_is_discarded_not_raised(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(error=RuntimeError("transport failed"))

    outcome = generate_program_synthesis(_request(), client=client, programs_root=programs_root)

    assert outcome.released is False
    assert outcome.synthesis is None
    assert released_terminal_for_run(outcome.ai_run_id, program_id="xpf", programs_root=programs_root) is ReleaseTerminal.DISCARDED


def test_non_dict_response_is_discarded(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=None)

    outcome = generate_program_synthesis(_request(), client=client, programs_root=programs_root)

    assert outcome.released is False
    assert released_terminal_for_run(outcome.ai_run_id, program_id="xpf", programs_root=programs_root) is ReleaseTerminal.DISCARDED


def test_oversized_request_is_discarded_before_calling_the_provider(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    oversized_item = SynthesisInputItem(category="strategic_risk", item_id="risk-huge", summary="x" * 200_001)
    client = _FakeClient(response=_valid_response())

    outcome = generate_program_synthesis(_request(items=(oversized_item,)), client=client, programs_root=programs_root)

    assert outcome.released is False
    assert client.calls == 0
    assert released_terminal_for_run(outcome.ai_run_id, program_id="xpf", programs_root=programs_root) is ReleaseTerminal.DISCARDED


def test_repeat_identical_request_hits_the_cache_no_second_provider_call(tmp_path: Path) -> None:
    # ADF-W5.1: program_synthesizer is the sixth (and last AISchemaGateway-
    # pattern generator) live cache adopter.
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    first = generate_program_synthesis(_request(), client=client, programs_root=programs_root)
    second = generate_program_synthesis(_request(), client=client, programs_root=programs_root)

    assert first.released is True
    assert second.released is True
    assert client.calls == 1  # second call served from the AI result cache
    assert second.synthesis is not None
    assert first.synthesis is not None
    assert second.synthesis.through_line == first.synthesis.through_line
    assert second.ai_run_id != first.ai_run_id


def test_different_request_items_does_not_hit_the_cache(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    other_items = (SynthesisInputItem(category="strategic_risk", item_id="risk-9", summary="A totally different risk."),)

    generate_program_synthesis(_request(), client=client, programs_root=programs_root)
    generate_program_synthesis(_request(items=other_items), client=client, programs_root=programs_root)

    assert client.calls == 2


# ---------------------------------------------------------------------------
# ADF-W2.9: the ContextCompiler candidate path. Same lifecycle and outcome
# shape as the baseline, so it is a drop-in for blind A/B comparison.
# ---------------------------------------------------------------------------


def test_context_gateway_path_releases_a_valid_synthesis(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    outcome = generate_program_synthesis_via_context_gateway(_request(), client=client, programs_root=programs_root)

    assert outcome.released is True
    assert outcome.synthesis is not None
    assert outcome.synthesis.through_line == _valid_response()["through_line"]
    assert client.calls == 1
    assert released_terminal_for_run(outcome.ai_run_id, program_id="xpf", programs_root=programs_root) is ReleaseTerminal.RELEASED


def test_context_gateway_path_runs_the_same_semantic_validator(tmp_path: Path) -> None:
    """The QG-29 semantic validator (source-backed recommendations) runs on
    the candidate path exactly as on the baseline -- a recommendation citing
    an unknown item_id is rejected, not released."""
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["recommendations"] = [{"text": "Invented.", "evidence_refs": ["does-not-exist"]}]
    client = _FakeClient(response=response)

    outcome = generate_program_synthesis_via_context_gateway(_request(), client=client, programs_root=programs_root)

    assert outcome.released is False
    assert released_terminal_for_run(outcome.ai_run_id, program_id="xpf", programs_root=programs_root) is ReleaseTerminal.REJECTED


def test_context_gateway_path_oversized_request_discarded_before_provider(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    oversized_item = SynthesisInputItem(category="strategic_risk", item_id="risk-huge", summary="x" * 200_001)
    client = _FakeClient(response=_valid_response())

    outcome = generate_program_synthesis_via_context_gateway(
        _request(items=(oversized_item,)), client=client, programs_root=programs_root
    )

    assert outcome.released is False
    assert client.calls == 0
    assert released_terminal_for_run(outcome.ai_run_id, program_id="xpf", programs_root=programs_root) is ReleaseTerminal.DISCARDED


def test_context_gateway_path_provider_exception_discarded(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(error=RuntimeError("transport failed"))

    outcome = generate_program_synthesis_via_context_gateway(_request(), client=client, programs_root=programs_root)

    assert outcome.released is False
    assert released_terminal_for_run(outcome.ai_run_id, program_id="xpf", programs_root=programs_root) is ReleaseTerminal.DISCARDED
