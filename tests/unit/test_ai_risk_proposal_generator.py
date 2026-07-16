"""ADF-W4.5: unit tests for src/ai/risk_proposal_generator.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.ai.risk_proposal_generator import RiskProposalSemanticValidator, generate_risk_proposal
from src.core.ai_schema_gateway import SemanticValidator
from src.core.quality_gates.ai_release_audit import released_terminal_for_run
from src.core.risk_proposal import RiskProposalRequest


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


def _request() -> RiskProposalRequest:
    return RiskProposalRequest(
        program_id="xpf",
        candidate_risk_id="risk-1",
        candidate_title="Vendor delay signal",
        candidate_description="Multiple signals mention a vendor delay.",
        evidence_texts=("Vendor X reported a delay.",),
        evidence_refs=("sig-1",),
    )


def _valid_response() -> dict[str, Any]:
    return {
        "causal_title": "Vendor X's staffing shortfall is delaying delivery",
        "why_it_matters": "This threatens the Q3 milestone.",
        "probability": "likely",
        "impact": "high",
        "category": "external",
        "mitigation": "Escalate to vendor management.",
        "owner": "alex",
        "by_when": "2026-08-01",
        "fallback": "Engage a backup vendor.",
        "evidence_refs": ["sig-1"],
    }


def test_valid_response_is_released(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    proposal = generate_risk_proposal(_request(), client=client, programs_root=programs_root)

    assert proposal is not None
    assert proposal.causal_title == "Vendor X's staffing shortfall is delaying delivery"
    assert proposal.probability.value == "likely"
    assert proposal.impact.value == "high"
    assert proposal.category.value == "external"
    assert proposal.owner_alias == "alex"
    assert proposal.evidence_refs == ("sig-1",)
    assert released_terminal_for_run(proposal.ai_run_id, program_id="xpf", programs_root=programs_root).value == "released"


def test_invalid_probability_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["probability"] = "definitely"
    client = _FakeClient(response=response)

    proposal = generate_risk_proposal(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_empty_mitigation_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["mitigation"] = ""
    client = _FakeClient(response=response)

    proposal = generate_risk_proposal(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_evidence_ref_outside_candidate_evidence_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["evidence_refs"] = ["sig-999"]
    client = _FakeClient(response=response)

    proposal = generate_risk_proposal(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_provider_exception_is_discarded_not_raised(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(error=RuntimeError("transport failed"))

    proposal = generate_risk_proposal(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_invalid_by_when_date_is_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["by_when"] = "not-a-date"
    client = _FakeClient(response=response)

    proposal = generate_risk_proposal(_request(), client=client, programs_root=programs_root)
    assert proposal is None


def test_null_by_when_and_owner_are_accepted(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    response = _valid_response()
    response["by_when"] = None
    response["owner"] = None
    client = _FakeClient(response=response)

    proposal = generate_risk_proposal(_request(), client=client, programs_root=programs_root)
    assert proposal is not None
    assert proposal.by_when is None
    assert proposal.owner_alias is None


def test_oversized_request_is_discarded_before_calling_the_provider(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    oversized_request = RiskProposalRequest(
        program_id="xpf", candidate_risk_id="risk-1", candidate_title="x",
        candidate_description="x" * 200_001, evidence_texts=(), evidence_refs=(),
    )
    client = _FakeClient(response=_valid_response())

    proposal = generate_risk_proposal(oversized_request, client=client, programs_root=programs_root)
    assert proposal is None
    assert client.calls == 0


def test_repeat_identical_request_hits_the_cache_no_second_provider_call(tmp_path: Path) -> None:
    # ADF-W5.2/W5.1: risk_proposal_generator is the first live cache adopter.
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())

    first = generate_risk_proposal(_request(), client=client, programs_root=programs_root)
    second = generate_risk_proposal(_request(), client=client, programs_root=programs_root)

    assert first is not None
    assert second is not None
    assert client.calls == 1  # second call served from the AI result cache
    assert second.causal_title == first.causal_title
    # A fresh, distinct ai_run_id/id still applies per invocation (each call
    # runs the full lifecycle/release-audit trail even on a cache hit) --
    # only the expensive provider call itself is skipped.
    assert second.ai_run_id != first.ai_run_id


class TestRiskProposalSemanticValidator:
    """ADF-W2.8 pilot: the first concrete SemanticValidator (Section 8.9.3),
    unit-tested directly so its itemized findings are verified independent
    of the full generate_risk_proposal pipeline."""

    def test_conforms_to_semantic_validator_protocol(self) -> None:
        validator: SemanticValidator = RiskProposalSemanticValidator()
        assert validator.validator_id == "risk_proposal_generator.v1"

    def test_valid_payload_has_no_findings(self) -> None:
        validator = RiskProposalSemanticValidator(known_evidence_refs=frozenset({"sig-1"}))
        findings = validator.validate(_valid_response())
        assert findings == ()

    def test_reports_every_finding_not_just_the_first(self) -> None:
        # Two independent problems -> two independent, itemized findings --
        # not a single generic rejection message.
        validator = RiskProposalSemanticValidator(known_evidence_refs=frozenset({"sig-1"}))
        payload = _valid_response()
        payload["mitigation"] = ""
        payload["probability"] = "definitely"
        findings = validator.validate(payload)
        assert len(findings) == 2
        assert any("mitigation" in f for f in findings)
        assert any("probability" in f for f in findings)

    def test_unknown_evidence_ref_is_a_named_finding(self) -> None:
        validator = RiskProposalSemanticValidator(known_evidence_refs=frozenset({"sig-1"}))
        payload = _valid_response()
        payload["evidence_refs"] = ["sig-999"]
        findings = validator.validate(payload)
        assert len(findings) == 1
        assert "sig-999" in findings[0]

    def test_invalid_by_when_is_a_named_finding(self) -> None:
        validator = RiskProposalSemanticValidator(known_evidence_refs=frozenset({"sig-1"}))
        payload = _valid_response()
        payload["by_when"] = "not-a-date"
        findings = validator.validate(payload)
        assert len(findings) == 1
        assert "by_when" in findings[0]

    def test_null_by_when_has_no_finding(self) -> None:
        validator = RiskProposalSemanticValidator(known_evidence_refs=frozenset({"sig-1"}))
        payload = _valid_response()
        payload["by_when"] = None
        assert validator.validate(payload) == ()


def test_different_candidate_does_not_hit_the_cache(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    client = _FakeClient(response=_valid_response())
    other_request = RiskProposalRequest(
        program_id="xpf", candidate_risk_id="risk-2", candidate_title="A completely different candidate",
        candidate_description="Unrelated evidence.", evidence_texts=("Different evidence.",), evidence_refs=("sig-9",),
    )

    generate_risk_proposal(_request(), client=client, programs_root=programs_root)
    generate_risk_proposal(other_request, client=client, programs_root=programs_root)

    assert client.calls == 2
