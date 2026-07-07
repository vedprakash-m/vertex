from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from src.ai.synthesizer import PROMPT_VERSION, SynthesizerError, WorkstreamSynthesizer
from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import Contradiction, ContradictionPacket, DataSourceType, Program, ResolvedContradiction, Workstream


class _FakeAIClient:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.last_prompt_version: str | None = None
        self.last_user: str | None = None
        self.calls = 0

    def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
        self.calls += 1
        self.last_user = user
        self.last_prompt_version = prompt_version
        del system, max_tokens
        try:
            payload = json.loads(self.response_text)
        except json.JSONDecodeError as error:
            from src.ai.client import AIClientError

            raise AIClientError(f"Azure OpenAI structured response returned invalid JSON: {error}") from error
        if not isinstance(payload, dict):
            from src.ai.client import AIClientError

            raise AIClientError("Azure OpenAI structured response returned a non-object payload.")
        return parser(payload)


def test_workstream_synthesizer_parses_structured_json(tmp_path) -> None:
    client = _FakeAIClient(
        json.dumps(
            {
                "overall_assessment": "Networking remains the gating lane until the servicing validation closes.",
                "proposed_risk": "high",
                "confidence": "medium",
                "key_findings": ["Target date slipped again."],
                "evidence_refs": ["sig-1", "sig-2"],
                "open_questions": ["Who owns the final validation sign-off?"],
                "recommended_actions": ["Lock the next validation checkpoint and owner."],
            }
        )
    )

    result = WorkstreamSynthesizer(client=client).generate(
        program=_program(),
        workstream=_workstream(),
        signals=(_signal("sig-1"), _signal("sig-2")),
        drift_patterns=(),
        programs_root=tmp_path,
    )

    assert result is not None
    assert result.prompt_version == PROMPT_VERSION
    assert client.last_prompt_version == PROMPT_VERSION
    assert result.synthesis.proposed_risk is RiskLevel.HIGH
    assert result.synthesis.confidence is Confidence.MEDIUM
    assert result.synthesis.evidence_refs == ("sig-1", "sig-2")


def test_workstream_synthesizer_rejects_invalid_json(tmp_path) -> None:
    with pytest.raises(SynthesizerError, match="invalid JSON"):
        WorkstreamSynthesizer(client=_FakeAIClient("not-json")).generate(
            program=_program(),
            workstream=_workstream(),
            signals=(_signal("sig-1"),),
            drift_patterns=(),
            programs_root=tmp_path,
        )


def test_workstream_synthesizer_rejects_invalid_proposed_risk(tmp_path) -> None:
    client = _FakeAIClient(
        json.dumps(
            {
                "overall_assessment": "Networking remains the gating lane until the servicing validation closes.",
                "proposed_risk": "urgent",
                "confidence": "medium",
                "key_findings": ["Target date slipped again."],
                "evidence_refs": ["sig-1"],
                "open_questions": [],
                "recommended_actions": [],
            }
        )
    )

    with pytest.raises(SynthesizerError, match="proposed_risk must be one of"):
        WorkstreamSynthesizer(client=client).generate(
            program=_program(),
            workstream=_workstream(),
            signals=(_signal("sig-1"),),
            drift_patterns=(),
            programs_root=tmp_path,
        )


def test_workstream_synthesizer_rejects_invalid_confidence(tmp_path) -> None:
    client = _FakeAIClient(
        json.dumps(
            {
                "overall_assessment": "Networking remains the gating lane until the servicing validation closes.",
                "proposed_risk": "high",
                "confidence": "certain",
                "key_findings": ["Target date slipped again."],
                "evidence_refs": ["sig-1"],
                "open_questions": [],
                "recommended_actions": [],
            }
        )
    )

    with pytest.raises(SynthesizerError, match="confidence must be one of"):
        WorkstreamSynthesizer(client=client).generate(
            program=_program(),
            workstream=_workstream(),
            signals=(_signal("sig-1"),),
            drift_patterns=(),
            programs_root=tmp_path,
        )


def test_workstream_synthesizer_rejects_blank_evidence_ref_entry(tmp_path) -> None:
    client = _FakeAIClient(
        json.dumps(
            {
                "overall_assessment": "Networking remains the gating lane until the servicing validation closes.",
                "proposed_risk": "high",
                "confidence": "medium",
                "key_findings": ["Target date slipped again."],
                "evidence_refs": ["sig-1", "   "],
                "open_questions": [],
                "recommended_actions": [],
            }
        )
    )

    with pytest.raises(SynthesizerError, match="evidence_refs must contain non-empty strings only"):
        WorkstreamSynthesizer(client=client).generate(
            program=_program(),
            workstream=_workstream(),
            signals=(_signal("sig-1"),),
            drift_patterns=(),
            programs_root=tmp_path,
        )


def test_workstream_synthesizer_rejects_unknown_evidence_ref(tmp_path) -> None:
    client = _FakeAIClient(
        json.dumps(
            {
                "overall_assessment": "Networking remains the gating lane until the servicing validation closes.",
                "proposed_risk": "high",
                "confidence": "medium",
                "key_findings": ["Target date slipped again."],
                "evidence_refs": ["sig-2"],
                "open_questions": [],
                "recommended_actions": [],
            }
        )
    )

    with pytest.raises(SynthesizerError, match="evidence_refs must cite only approved signal ids"):
        WorkstreamSynthesizer(client=client).generate(
            program=_program(),
            workstream=_workstream(),
            signals=(_signal("sig-1"),),
            drift_patterns=(),
            programs_root=tmp_path,
        )


def test_workstream_synthesizer_rejects_missing_required_list_fields(tmp_path) -> None:
    missing_fields = ("key_findings", "evidence_refs", "open_questions", "recommended_actions")

    for field_name in missing_fields:
        payload = {
            key: value
            for key, value in {
                "overall_assessment": "Networking remains the gating lane until the servicing validation closes.",
                "proposed_risk": "high",
                "confidence": "medium",
                "key_findings": ["Target date slipped again."],
                "evidence_refs": ["sig-1"],
                "open_questions": [],
                "recommended_actions": [],
            }.items()
            if key != field_name
        }
        client = _FakeAIClient(json.dumps(payload))

        with pytest.raises(SynthesizerError, match=rf"{field_name} must be provided"):
            WorkstreamSynthesizer(client=client).generate(
                program=_program(),
                workstream=_workstream(),
                signals=(_signal("sig-1"),),
                drift_patterns=(),
                programs_root=tmp_path / field_name,
            )


def test_workstream_synthesizer_rejects_null_required_list_fields(tmp_path) -> None:
    null_fields = ("key_findings", "evidence_refs", "open_questions", "recommended_actions")

    for field_name in null_fields:
        payload = {
            "overall_assessment": "Networking remains the gating lane until the servicing validation closes.",
            "proposed_risk": "high",
            "confidence": "medium",
            "key_findings": ["Target date slipped again."],
            "evidence_refs": ["sig-1"],
            "open_questions": [],
            "recommended_actions": [],
        }
        payload[field_name] = None
        client = _FakeAIClient(json.dumps(payload))

        with pytest.raises(SynthesizerError, match=rf"{field_name} must be a list of strings"):
            WorkstreamSynthesizer(client=client).generate(
                program=_program(),
                workstream=_workstream(),
                signals=(_signal("sig-1"),),
                drift_patterns=(),
                programs_root=tmp_path / field_name,
            )


def test_workstream_synthesizer_includes_contradictions_in_prompt(tmp_path) -> None:
    client = _FakeAIClient(
        json.dumps(
            {
                "overall_assessment": "Networking remains the gating lane because source disagreement is unresolved.",
                "proposed_risk": "high",
                "confidence": "medium",
                "key_findings": ["Claim and ADO dates still disagree."],
                "evidence_refs": ["sig-1"],
                "open_questions": [],
                "recommended_actions": [],
            }
        )
    )

    WorkstreamSynthesizer(client=client).generate(
        program=_program(),
        workstream=_workstream(),
        signals=(_signal("sig-1"),),
        drift_patterns=(),
        programs_root=tmp_path,
        contradictions=(
            ContradictionPacket(
                work_item_id=1234,
                workstream_id="networking",
                contradictions=(
                    Contradiction(
                        field="target_date",
                        source_a="journal",
                        source_b="ado",
                        summary="Claim says 2026-05-17 while ADO shows 2026-05-12.",
                        confidence=Confidence.HIGH,
                        evidence_refs=("sig-1",),
                    ),
                ),
                confidence=Confidence.HIGH,
                recommended_resolution=ResolvedContradiction(
                    winning_source=DataSourceType.WORKIQ,
                    confidence=Confidence.HIGH,
                    rationale="Prefer the external source while the DRI slip bias remains elevated.",
                    evidence_refs=("sig-1",),
                ),
                generated_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
            ),
        ),
    )

    assert client.last_user is not None
    assert "Source contradictions (address the most significant in overall_assessment):" in client.last_user
    assert "- WI:1234 | journal vs ado | target_date | Claim says 2026-05-17 while ADO shows 2026-05-12. | recommended=workiq (high)" in client.last_user


def _program() -> Program:
    return Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
    )


def _workstream() -> Workstream:
    return Workstream(id="networking", name="Networking", description="Networking lane")


def _signal(signal_id: str, *, text: str = "Servicing validation moved to 2026-05-17."):
    from datetime import datetime, timezone

    from src.core.models_v2 import Signal

    return Signal(
        id=signal_id,
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="manual",
        program_id="acme",
        workstream_id="networking",
        entity_refs=("WI:1234",),
        text=text,
        raw_ref=None,
        confidence=Confidence.HIGH,
    )


_VALID_PAYLOAD_TEXT = json.dumps(
    {
        "overall_assessment": "Networking remains the gating lane until the servicing validation closes.",
        "proposed_risk": "high",
        "confidence": "medium",
        "key_findings": ["Target date slipped again."],
        "evidence_refs": ["sig-1"],
        "open_questions": [],
        "recommended_actions": [],
    }
)


def test_workstream_synthesizer_records_released_terminal_on_success(tmp_path) -> None:
    # ADF-W5.1/P7: synthesizer's AISchemaGateway migration must record a
    # durable QG-29 "released" terminal for a successful generation, same
    # as risk_proposal_generator's release-audit contract.
    from src.core.ledger.event_log import read_events

    client = _FakeAIClient(_VALID_PAYLOAD_TEXT)

    result = WorkstreamSynthesizer(client=client).generate(
        program=_program(),
        workstream=_workstream(),
        signals=(_signal("sig-1"),),
        drift_patterns=(),
        programs_root=tmp_path,
    )

    assert result is not None
    events = read_events("acme", programs_root=tmp_path)
    release_decisions = [event for event in events if event.event_type == "ai.release_decision.v1"]
    assert release_decisions
    assert release_decisions[-1].payload["terminal"] == "released"


def test_workstream_synthesizer_repeat_identical_request_hits_the_cache(tmp_path) -> None:
    # ADF-W5.1/P7: same program/workstream/signals/drift_patterns should be
    # served from the AI result cache on the second call -- only the audit
    # trail (ai_run_id, lifecycle events, release decision) is fresh per call.
    client = _FakeAIClient(_VALID_PAYLOAD_TEXT)
    program = _program()
    workstream = _workstream()
    signals = (_signal("sig-1"),)

    first = WorkstreamSynthesizer(client=client).generate(
        program=program,
        workstream=workstream,
        signals=signals,
        drift_patterns=(),
        programs_root=tmp_path,
    )
    second = WorkstreamSynthesizer(client=client).generate(
        program=program,
        workstream=workstream,
        signals=signals,
        drift_patterns=(),
        programs_root=tmp_path,
    )

    assert first is not None
    assert second is not None
    assert client.calls == 1
    assert second.synthesis == first.synthesis


def test_workstream_synthesizer_different_signals_do_not_hit_the_cache(tmp_path) -> None:
    client = _FakeAIClient(_VALID_PAYLOAD_TEXT)
    program = _program()
    workstream = _workstream()

    WorkstreamSynthesizer(client=client).generate(
        program=program,
        workstream=workstream,
        signals=(_signal("sig-1", text="Servicing validation moved to 2026-05-17."),),
        drift_patterns=(),
        programs_root=tmp_path,
    )
    WorkstreamSynthesizer(client=client).generate(
        program=program,
        workstream=workstream,
        signals=(_signal("sig-1", text="A completely different signal about WI:1234."),),
        drift_patterns=(),
        programs_root=tmp_path,
    )

    assert client.calls == 2


def test_workstream_synthesizer_oversized_request_discarded_before_calling_the_provider(tmp_path) -> None:
    # ADF-W5.1/P7: AISchemaGateway bounds must reject an oversized request
    # payload before ever invoking the frontier provider (avoids paying for
    # a call the schema gateway will discard anyway).
    client = _FakeAIClient(_VALID_PAYLOAD_TEXT)

    with pytest.raises(SynthesizerError, match="AISchemaGateway rejected the outbound request"):
        WorkstreamSynthesizer(client=client).generate(
            program=_program(),
            workstream=_workstream(),
            signals=(_signal("sig-1", text="x" * 200_001),),
            drift_patterns=(),
            programs_root=tmp_path,
        )

    assert client.calls == 0