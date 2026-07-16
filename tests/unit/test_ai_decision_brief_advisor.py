from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.ai.decision_brief_advisor import (
    _FALLBACK_PROMPT,
    _build_evidence_spans,
    _build_user_prompt,
    _load_prompt_template,
    _parse_advice,
    _program_synthesis_context_lines,
    advise_on_decision_brief,
    advise_on_decision_brief_via_context_gateway,
)
from src.ai.prompt_registry import PromptRegistryError
from src.core.decision_brief_engine import DecisionBrief, DecisionItem, DecisionSignal


def test_parse_advice_scrubs_pii_from_reasoning_and_suggested_text() -> None:
    advice = _parse_advice(
        {
            "verdict": "REVISE",
            "reasoning": "Ask foo@gmail.com to confirm whether the risk is still open.",
            "suggested_text": "Escalate the dependency owner at foo@gmail.com before Friday.",
        }
    )

    assert advice is not None
    assert advice.verdict == "REVISE"
    assert "foo@gmail.com" not in advice.reasoning
    assert "foo@gmail.com" not in (advice.suggested_text or "")
    assert "[PII-FILTERED-EMAIL]" in advice.reasoning
    assert "[PII-FILTERED-EMAIL]" in (advice.suggested_text or "")


def test_parse_advice_downgrades_revise_without_suggested_text() -> None:
    advice = _parse_advice(
        {
            "verdict": "REVISE",
            "reasoning": "The section needs revision but no usable replacement was provided.",
            "suggested_text": None,
        }
    )

    assert advice is not None
    assert advice.verdict == "DEFER"
    assert advice.suggested_text is None


def test_load_prompt_template_falls_back_when_registry_resolution_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.ai.decision_brief_advisor.load_prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PromptRegistryError("missing")),
    )

    assert _load_prompt_template() == _FALLBACK_PROMPT


class _FakeClient:
    """ADF-W2.9 P5: minimal LLMProvider double for the context-gateway pilot path."""

    def __init__(self, *, response: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls = 0
        self.last_user: str | None = None

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
        self.last_user = user
        if self.error is not None:
            raise self.error
        return parser(self.response)


def _item(**overrides: Any) -> DecisionItem:
    defaults: dict[str, Any] = dict(
        section_id="risks",
        section_title="Risks",
        current_text="The vendor delivery risk remains open.",
        proposed_text="Escalate the vendor delivery risk to leadership.",
        evidence_delta_lines=("Vendor confirmed a two-week slip.",),
        top_signals=(
            DecisionSignal(signal_id="sig-1", text="Vendor email confirms slip.", timestamp="2026-06-01", source="email"),
            DecisionSignal(signal_id="sig-2", text="Standup note: vendor blocked.", timestamp="2026-06-02", source="meeting"),
        ),
        vitality_summary="declining",
        confidence="medium",
        kpi_summary="On-time delivery: 62% (down from 80%).",
        stale_claims=("Vendor said delivery was on track (superseded).",),
        accept_command="vertex accept --id risks",
        reject_command="vertex reject --id risks",
        accept_modified_command="vertex accept --id risks --modified",
    )
    defaults.update(overrides)
    return DecisionItem(**defaults)


def _brief(*items: DecisionItem) -> DecisionBrief:
    return DecisionBrief(
        issue_number=12,
        edition_name="acme_weekly",
        generated_at="2026-06-06 12:00",
        items=tuple(items),
        total_pending=len(items),
        ai_enriched=False,
    )


def _valid_response() -> dict[str, Any]:
    return {
        "verdict": "ACCEPT",
        "reasoning": "Evidence and stale-claim resolution support acceptance.",
        "suggested_text": None,
    }


def test_build_evidence_spans_separates_required_from_optional() -> None:
    required, optional = _build_evidence_spans(_item())

    required_ids = {span.evidence_id for span in required}
    assert required_ids == {"current_text", "evidence_delta", "prior_ai_proposal", "stale_claims"}
    assert all(span.required for span in required)

    optional_ids = {span.evidence_id for span in optional}
    assert optional_ids == {"signal_sig-1", "signal_sig-2", "kpi_summary", "vitality_summary"}
    assert all(not span.required for span in optional)
    # Signals decay in salience by arrival order (approximates the old [:6] truncation intent).
    signal_spans = sorted((s for s in optional if s.source_family == "signal"), key=lambda s: s.evidence_id)
    assert signal_spans[0].salience_inputs["recency"] > signal_spans[1].salience_inputs["recency"]


def test_build_evidence_spans_includes_program_synthesis_context() -> None:
    required, optional = _build_evidence_spans(
        _item(), supplemental_context=("Open strategic risk: Vendor delay risk.",),
    )

    assert all(span.evidence_id != "program_synthesis_0" for span in required)
    context_spans = [span for span in optional if span.source_family == "program_synthesis"]
    assert len(context_spans) == 1
    assert context_spans[0].text == "Open strategic risk: Vendor delay risk."
    assert context_spans[0].required is False


def test_build_user_prompt_renders_program_context_section() -> None:
    prompt = _build_user_prompt(_item(), supplemental_context=("Unresolved contradiction: Milestone date disagreement.",))

    assert "PROGRAM CONTEXT:" in prompt
    assert "Milestone date disagreement." in prompt


def test_build_user_prompt_omits_program_context_section_when_empty() -> None:
    prompt = _build_user_prompt(_item())

    assert "PROGRAM CONTEXT:" not in prompt


def test_program_synthesis_context_lines_covers_three_categories(monkeypatch, tmp_path: Path) -> None:
    # ADF-W2.9: program-wide analog of report_ai's exec-summary enrichment
    # -- DecisionItem carries no workstream field, so this stays unscoped.
    from datetime import datetime, timezone

    from src.core.program_synthesis import ProgramSynthesisRequest, SynthesisInputItem

    request = ProgramSynthesisRequest(
        program_id="demo",
        as_of=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
        items=(
            SynthesisInputItem(category="strategic_risk", item_id="risk-1", summary="Vendor delay risk.", severity="high"),
            SynthesisInputItem(category="contradiction", item_id="conf-1", summary="Milestone date disagreement."),
            SynthesisInputItem(category="critical_path_milestone", item_id="ms-1", summary="M1 code complete | status=at_risk"),
            SynthesisInputItem(category="kusto_slo_breach", item_id="slo-1", summary="Latency SLO breached."),
        ),
    )
    monkeypatch.setattr(
        "src.core.program_synthesis.assemble_program_synthesis_request",
        lambda program_id, **kwargs: request,
    )

    lines = _program_synthesis_context_lines("demo", tmp_path)

    assert any("Vendor delay risk." in line and "[high]" in line for line in lines)
    assert any("Milestone date disagreement." in line for line in lines)
    assert any("M1 code complete" in line for line in lines)
    assert not any("Latency SLO breached." in line for line in lines)


def test_program_synthesis_context_lines_degrades_on_failure(monkeypatch, tmp_path: Path) -> None:
    def _raise(program_id, **kwargs):
        raise RuntimeError("fact store unavailable")

    monkeypatch.setattr("src.core.program_synthesis.assemble_program_synthesis_request", _raise)

    lines = _program_synthesis_context_lines("demo", tmp_path)

    assert lines == ()


def test_advise_on_decision_brief_baseline_prompt_includes_program_synthesis_context(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "src.ai.decision_brief_advisor._program_synthesis_context_lines",
        lambda program_id, programs_root, **kwargs: ("Open strategic risk: Vendor delay risk.",),
    )
    client = _FakeClient(response=_valid_response())

    advise_on_decision_brief(client=client, brief=_brief(_item()), program_id="acme", programs_root=tmp_path)

    assert client.last_user is not None
    assert "Vendor delay risk." in client.last_user


def test_context_gateway_happy_path_populates_advice(tmp_path: Path) -> None:
    client = _FakeClient(response=_valid_response())

    result = advise_on_decision_brief_via_context_gateway(
        client=client, brief=_brief(_item()), program_id="xpf", programs_root=tmp_path / "programs",
    )

    assert result.ai_enriched is True
    assert client.calls == 1
    enriched = result.items[0]
    assert enriched.verdict == "ACCEPT"
    assert "acceptance" in enriched.verdict_reasoning
    # The compiled prompt (not the old ad hoc builder) was sent to the client.
    assert client.last_user is not None
    assert "REQUIRED current_text" in client.last_user or "current_text" in client.last_user


def test_context_gateway_degrades_gracefully_when_reserved_tokens_exceed_budget(tmp_path: Path) -> None:
    client = _FakeClient(response=_valid_response())
    # Varied words (not a single repeated char) so the tokenizer can't
    # collapse the run into a handful of tokens -- this must genuinely
    # blow the 6000-token input budget.
    words = " ".join(f"word{i}" for i in range(20_000))
    huge_item = _item(current_text=words)

    result = advise_on_decision_brief_via_context_gateway(
        client=client, brief=_brief(huge_item), program_id="xpf", programs_root=tmp_path / "programs",
    )

    assert result.ai_enriched is True  # brief-level flag still flips even if this item degrades
    assert client.calls == 0  # ContextCompileRejected short-circuits before any provider call
    assert result.items[0].verdict is None


def test_context_gateway_rejects_response_via_schema_gateway(tmp_path: Path) -> None:
    oversized_response = {
        "verdict": "ACCEPT",
        "reasoning": "x" * 200_001,
        "suggested_text": None,
    }
    client = _FakeClient(response=oversized_response)

    result = advise_on_decision_brief_via_context_gateway(
        client=client, brief=_brief(_item()), program_id="xpf", programs_root=tmp_path / "programs",
    )

    assert client.calls == 1
    assert result.items[0].verdict is None


def test_advise_on_decision_brief_records_released_terminal_on_success(tmp_path: Path) -> None:
    # ADF-W5.1/P7: decision_brief_advisor's baseline (production) path must
    # record a durable QG-29 "released" terminal per item, same as
    # risk_proposal_generator's release-audit contract.
    from src.core.ledger.event_log import read_events

    client = _FakeClient(response=_valid_response())

    result = advise_on_decision_brief(
        client=client, brief=_brief(_item()), program_id="acme", programs_root=tmp_path,
    )

    assert result.items[0].verdict == "ACCEPT"
    events = read_events("acme", programs_root=tmp_path)
    release_decisions = [event for event in events if event.event_type == "ai.release_decision.v1"]
    assert release_decisions
    assert release_decisions[-1].payload["terminal"] == "released"


def test_advise_on_decision_brief_repeat_identical_request_hits_the_cache(tmp_path: Path) -> None:
    # ADF-W5.1/P7: identical item content should be served from the AI
    # result cache on the second call -- only the audit trail (ai_run_id,
    # lifecycle events, release decision) is fresh per call.
    client = _FakeClient(response=_valid_response())
    brief = _brief(_item())

    first = advise_on_decision_brief(client=client, brief=brief, program_id="acme", programs_root=tmp_path)
    second = advise_on_decision_brief(client=client, brief=brief, program_id="acme", programs_root=tmp_path)

    assert first.items[0].verdict == "ACCEPT"
    assert second.items[0].verdict == "ACCEPT"
    assert client.calls == 1


def test_advise_on_decision_brief_different_items_do_not_hit_the_cache(tmp_path: Path) -> None:
    client = _FakeClient(response=_valid_response())

    advise_on_decision_brief(
        client=client,
        brief=_brief(_item(section_id="risks", current_text="The vendor delivery risk remains open.")),
        program_id="acme",
        programs_root=tmp_path,
    )
    advise_on_decision_brief(
        client=client,
        brief=_brief(_item(section_id="dependencies", current_text="A completely different section body.")),
        program_id="acme",
        programs_root=tmp_path,
    )

    assert client.calls == 2


def test_advise_on_decision_brief_oversized_request_is_discarded_gracefully(tmp_path: Path) -> None:
    # ADF-W5.1/P7: AISchemaGateway bounds must reject an oversized request
    # payload before ever invoking the frontier provider -- and since this
    # feature's existing contract is a graceful per-item degrade (never
    # raises), the discard shows up as an unchanged item, not an exception.
    client = _FakeClient(response=_valid_response())
    oversized_item = _item(current_text="x" * 200_001)

    result = advise_on_decision_brief(
        client=client, brief=_brief(oversized_item), program_id="acme", programs_root=tmp_path,
    )

    assert client.calls == 0
    assert result.items[0].verdict is None