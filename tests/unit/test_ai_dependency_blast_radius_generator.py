"""ADF-W4.5 remainder: unit tests for src/ai/dependency_blast_radius_generator.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.ai.dependency_blast_radius_generator import (
    generate_dependency_blast_radius_proposal,
    generate_dependency_blast_radius_proposal_via_runner,
)
from src.core.dependency_blast_radius import DependencyBlastRadiusRequest
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


def _request() -> DependencyBlastRadiusRequest:
    return DependencyBlastRadiusRequest(
        program_id="acme",
        dependency_id="dep-1",
        from_summary="program=acme, workstream=deployment, WI:1001",
        to_summary="program=contoso, workstream=platform, milestone=ms-1",
        risk_if_broken="Contoso's platform milestone slips by two sprints.",
        current_status="active",
        evidence_texts=("Vendor confirmed the API contract review date.",),
        evidence_refs=("sig-1",),
    )


def _valid_response() -> dict[str, Any]:
    return {
        "next_proving_event": "The platform API contract review scheduled for next sprint.",
        "blast_radius_narrative": "If unresolved, Contoso's platform milestone slips, cascading to two downstream teams.",
        "evidence_refs": ["sig-1"],
    }


def test_valid_response_is_released(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    proposal = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)

    assert proposal is not None
    assert "API contract review" in proposal.next_proving_event
    assert "cascading" in proposal.blast_radius_narrative
    assert proposal.evidence_refs == ("sig-1",)
    assert released_terminal_for_run(proposal.ai_run_id, program_id="acme", programs_root=programs_root).value == "released"


def test_empty_next_proving_event_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["next_proving_event"] = ""
    client = _FakeClient(response=response)

    proposal = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_empty_narrative_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["blast_radius_narrative"] = ""
    client = _FakeClient(response=response)

    proposal = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_evidence_ref_outside_dependency_evidence_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["evidence_refs"] = ["sig-999"]
    client = _FakeClient(response=response)

    proposal = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_provider_exception_is_discarded_not_raised(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(error=RuntimeError("transport failed"))

    proposal = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_oversized_request_is_discarded_before_calling_the_provider(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    oversized_request = DependencyBlastRadiusRequest(
        program_id="acme", dependency_id="dep-1", from_summary="x", to_summary="x" * 200_001,
        risk_if_broken="x", current_status="active", evidence_texts=(), evidence_refs=(),
    )
    client = _FakeClient(response=_valid_response())

    proposal = generate_dependency_blast_radius_proposal(oversized_request, client=client, programs_root=programs_root)
    assert proposal is None
    assert client.calls == 0


def test_repeat_identical_request_hits_the_cache_no_second_provider_call(tmp_path: Path) -> None:
    # ADF-W5.1: dependency_blast_radius_generator is the fifth live cache adopter.
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    first = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    second = generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)

    assert first is not None
    assert second is not None
    assert client.calls == 1  # second call served from the AI result cache
    assert second.next_proving_event == first.next_proving_event
    assert second.ai_run_id != first.ai_run_id


def test_different_dependency_does_not_hit_the_cache(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    other_request = DependencyBlastRadiusRequest(
        program_id="acme", dependency_id="dep-2", from_summary="program=acme, workstream=infra, WI:2002",
        to_summary="program=contoso, workstream=network, milestone=ms-2",
        risk_if_broken="A completely different downstream milestone slips.",
        current_status="active", evidence_texts=("Different evidence.",), evidence_refs=("sig-9",),
    )

    generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    generate_dependency_blast_radius_proposal(other_request, client=client, programs_root=programs_root)

    assert client.calls == 2


# ---------------------------------------------------------------------------
# RELO-W1.3 pilot: generate_dependency_blast_radius_proposal_via_runner.
# Mirrors every test above exactly, proving behavioral equivalence with the
# original implementation now that dispatch goes through
# StructuredFeatureRunner instead of route_through_tiers/AIResultCache directly.
# ---------------------------------------------------------------------------


def test_via_runner_valid_response_is_released(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    proposal = generate_dependency_blast_radius_proposal_via_runner(_request(), client=client, programs_root=programs_root)

    assert proposal is not None
    assert "API contract review" in proposal.next_proving_event
    assert "cascading" in proposal.blast_radius_narrative
    assert proposal.evidence_refs == ("sig-1",)
    assert released_terminal_for_run(proposal.ai_run_id, program_id="acme", programs_root=programs_root).value == "released"


def test_via_runner_empty_next_proving_event_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["next_proving_event"] = ""
    client = _FakeClient(response=response)

    proposal = generate_dependency_blast_radius_proposal_via_runner(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_via_runner_empty_narrative_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["blast_radius_narrative"] = ""
    client = _FakeClient(response=response)

    proposal = generate_dependency_blast_radius_proposal_via_runner(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_via_runner_evidence_ref_outside_dependency_evidence_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["evidence_refs"] = ["sig-999"]
    client = _FakeClient(response=response)

    proposal = generate_dependency_blast_radius_proposal_via_runner(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_via_runner_provider_exception_is_discarded_not_raised(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(error=RuntimeError("transport failed"))

    proposal = generate_dependency_blast_radius_proposal_via_runner(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_via_runner_oversized_request_is_discarded_before_calling_the_provider(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    oversized_request = DependencyBlastRadiusRequest(
        program_id="acme", dependency_id="dep-1", from_summary="x", to_summary="x" * 200_001,
        risk_if_broken="x", current_status="active", evidence_texts=(), evidence_refs=(),
    )
    client = _FakeClient(response=_valid_response())

    proposal = generate_dependency_blast_radius_proposal_via_runner(oversized_request, client=client, programs_root=programs_root)
    assert proposal is None
    assert client.calls == 0


def test_via_runner_repeat_identical_request_hits_the_cache_no_second_provider_call(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    first = generate_dependency_blast_radius_proposal_via_runner(_request(), client=client, programs_root=programs_root)
    second = generate_dependency_blast_radius_proposal_via_runner(_request(), client=client, programs_root=programs_root)

    assert first is not None
    assert second is not None
    assert client.calls == 1  # second call served from the runner's ReleasedResultCache
    assert second.next_proving_event == first.next_proving_event
    assert second.ai_run_id != first.ai_run_id


def test_via_runner_different_dependency_does_not_hit_the_cache(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    other_request = DependencyBlastRadiusRequest(
        program_id="acme", dependency_id="dep-2", from_summary="program=acme, workstream=infra, WI:2002",
        to_summary="program=contoso, workstream=network, milestone=ms-2",
        risk_if_broken="A completely different downstream milestone slips.",
        current_status="active", evidence_texts=("Different evidence.",), evidence_refs=("sig-9",),
    )

    generate_dependency_blast_radius_proposal_via_runner(_request(), client=client, programs_root=programs_root)
    generate_dependency_blast_radius_proposal_via_runner(other_request, client=client, programs_root=programs_root)

    assert client.calls == 2


def test_via_runner_and_original_are_independent_caches(tmp_path: Path) -> None:
    # The two implementations must not silently share cache state -- a
    # request already cached by the original path is still a genuine cache
    # miss (and a real provider call) via the runner path, and vice versa.
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    generate_dependency_blast_radius_proposal(_request(), client=client, programs_root=programs_root)
    assert client.calls == 1

    generate_dependency_blast_radius_proposal_via_runner(_request(), client=client, programs_root=programs_root)
    assert client.calls == 2
