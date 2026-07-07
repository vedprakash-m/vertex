"""LLM-judge library tests (activation.md v1.25 §6.16).

Covers the fail-closed contract, structured-response parsing, AMBIGUOUS
escalation completeness, judge-independence enforcement, and the invariant that
authority flips are never auto-executable (the highest-blast-radius event stays
a human action — AG-4/AG-18).
"""

from __future__ import annotations

from typing import Any, Callable

from src.ai.activation_judge import (
    JUDGE_PROMPT_VERSION,
    STATUS_AMBIGUOUS,
    STATUS_FAIL,
    STATUS_JUDGE_UNAVAILABLE,
    STATUS_PASS,
    assess_activation,
    assess_activation_deterministic,
    build_judge_user_prompt,
    parse_judge_response,
)


class FakeJudgeClient:
    """Stub LLMProvider returning a pre-canned structured response (or raising)."""

    def __init__(self, response: Any, *, raise_exc: Exception | None = None) -> None:
        self._response = response
        self._raise_exc = raise_exc
        self.calls: list[tuple[str, str, str | None]] = []

    def chat(self, system: str, user: str, **kw: Any) -> str:
        return ""

    def structured(
        self,
        system: str,
        user: str,
        *,
        parser: Callable[[dict[str, Any]], Any],
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ) -> Any:
        self.calls.append((system[:30], user[:30], prompt_version))
        if self._raise_exc is not None:
            raise self._raise_exc
        return parser(self._response if isinstance(self._response, dict) else {})


def _sample_report() -> dict[str, Any]:
    return {
        "program": "xpf",
        "keystone_family": "milestone.completed",
        "git_sha": "abc123",
        "dirty_worktree": False,
        "checks": [
            {"check_id": "AG-1-COUNTERFACTUAL-DIFF", "status": "fail",
             "summary": "no counterfactual render artifacts supplied",
             "details": {"source_document_key": None}},
            {"check_id": "AG-2-CORPUS-CERTIFICATION", "status": "fail",
             "summary": "0 keystone dual labels, kappa null",
             "details": {"kappa": None, "g_xtract_prec_ci_low": 0.62}},
            {"check_id": "AG-9-CONFLICT-WIRED", "status": "pass",
             "summary": "conflict detector invoked on the REV finalize path",
             "details": {"conflict_wired": True}},
        ],
        "family_matrix": [],
    }


class TestParseJudgeResponse:
    def test_parses_well_formed_response(self) -> None:
        raw = {
            "verdicts": [
                {"gate_id": "AG-1", "status": "FAIL", "confidence": 0.95,
                 "bar": "A", "blocker_type": "data",
                 "reasoning": "no render artifacts", "evidence_refs": ["0 artifacts"]},
                {"gate_id": "AG-9", "status": "PASS", "confidence": 0.9,
                 "bar": "B", "blocker_type": "none", "reasoning": "wired"},
            ],
            "sequence_recommendation": ["acquire raw EMLs", "annotate corpus"],
            "human_decisions": [],
            "summary": "2 gates assessed.",
        }
        verdicts, sequence, human, summary = parse_judge_response(raw)
        assert len(verdicts) == 2
        assert verdicts[0].status == STATUS_FAIL
        assert verdicts[0].confidence == 0.95
        assert verdicts[1].status == STATUS_PASS
        assert sequence == ("acquire raw EMLs", "annotate corpus")
        assert summary == "2 gates assessed."

    def test_drops_unrecognized_status(self) -> None:
        # A status the schema doesn't recognize must be dropped — never inferred PASS.
        raw = {"verdicts": [{"gate_id": "X", "status": "MAYBE"}]}
        verdicts, _, _, _ = parse_judge_response(raw)
        assert verdicts == ()

    def test_tolerates_missing_fields(self) -> None:
        raw = {"verdicts": [{"gate_id": "AG-1", "status": "fail"}]}
        verdicts, _, _, _ = parse_judge_response(raw)
        assert len(verdicts) == 1
        assert verdicts[0].confidence == 0.0
        assert verdicts[0].reasoning == ""

    def test_clamps_confidence_to_unit_interval(self) -> None:
        raw = {"verdicts": [{"gate_id": "X", "status": "pass", "confidence": 1.5}]}
        verdicts, _, _, _ = parse_judge_response(raw)
        assert verdicts[0].confidence == 1.0


class TestAssessActivation:
    def test_fail_closed_when_client_none(self) -> None:
        # No client → every gate is JUDGE_UNAVAILABLE, deterministic finding preserved.
        report = assess_activation(
            activation_report=_sample_report(),
            program_artifacts={},
            client=None,
            system_prompt="sys",
            judge_model="unavailable",
            git_sha="abc123",
        )
        assert report.judge_available is False
        assert all(v.status == STATUS_JUDGE_UNAVAILABLE for v in report.verdicts)
        # The deterministic finding is preserved in the reasoning.
        assert any("no counterfactual render" in v.reasoning for v in report.verdicts)

    def test_fail_closed_when_llm_raises(self) -> None:
        client = FakeJudgeClient({}, raise_exc=RuntimeError("LLM down"))
        report = assess_activation(
            activation_report=_sample_report(),
            program_artifacts={},
            client=client,
            system_prompt="sys",
            judge_model="frontier",
            git_sha="abc123",
        )
        assert report.judge_available is False
        assert all(v.status == STATUS_JUDGE_UNAVAILABLE for v in report.verdicts)

    def test_parses_judge_verdicts(self) -> None:
        client = FakeJudgeClient({
            "verdicts": [
                {"gate_id": "AG-1-COUNTERFACTUAL-DIFF", "status": "FAIL",
                 "confidence": 0.9, "bar": "A", "blocker_type": "data",
                 "reasoning": "no real render artifacts exist",
                 "evidence_refs": ["details.source_document_key=None"]},
                {"gate_id": "AG-9-CONFLICT-WIRED", "status": "PASS",
                 "confidence": 0.85, "bar": "B", "blocker_type": "none",
                 "reasoning": "detector is invoked in _finalize_report"},
            ],
            "sequence_recommendation": ["acquire EMLs first"],
            "human_decisions": [],
            "summary": "judge ran.",
        })
        report = assess_activation(
            activation_report=_sample_report(),
            program_artifacts={},
            client=client,
            system_prompt="sys",
            judge_model="frontier",
            git_sha="abc123",
        )
        assert report.judge_available is True
        # The judge returned 2; the 3rd (AG-2) is gap-filled as FAIL.
        statuses = {v.gate_id: v.status for v in report.verdicts}
        assert statuses["AG-1-COUNTERFACTUAL-DIFF"] == STATUS_FAIL
        assert statuses["AG-9-CONFLICT-WIRED"] == STATUS_PASS
        assert statuses["AG-2-CORPUS-CERTIFICATION"] == STATUS_FAIL  # gap-filled

    def test_missing_gate_verdict_fails_closed(self) -> None:
        # If the judge omits a requested gate, it must be FAIL — never inferred PASS.
        client = FakeJudgeClient({
            "verdicts": [{"gate_id": "AG-9-CONFLICT-WIRED", "status": "PASS",
                          "confidence": 0.9, "bar": "B", "blocker_type": "none",
                          "reasoning": "wired"}],
            "summary": "partial.",
        })
        report = assess_activation(
            activation_report=_sample_report(),
            program_artifacts={},
            client=client,
            system_prompt="sys",
            judge_model="frontier",
            git_sha="abc123",
        )
        # AG-1 and AG-2 were requested (in the report) but omitted → FAIL.
        omitted = {v.gate_id: v for v in report.verdicts if v.status == STATUS_FAIL}
        assert "AG-1-COUNTERFACTUAL-DIFF" in omitted
        assert "evidence-absent" in omitted["AG-1-COUNTERFACTUAL-DIFF"].reasoning

    def test_ambiguous_escalation_carries_full_context(self) -> None:
        client = FakeJudgeClient({
            "verdicts": [
                {"gate_id": "AG-1-COUNTERFACTUAL-DIFF", "status": "AMBIGUOUS",
                 "confidence": 0.5, "bar": "A", "blocker_type": "data",
                 "reasoning": "could run under pilot-degrade exception",
                 "alternatives": ["run proof under RK-1 exception",
                                   "wait for Azure CS"],
                 "decision_context": "Azure CS is unprovisioned; RK-1 ADR allows "
                 "proof-only degrade. Counterfactual render still missing.",
                 "recommendation": "run under RK-1 exception"},
            ],
            "human_decisions": ["AG-1-COUNTERFACTUAL-DIFF: run proof under degrade or wait?"],
            "summary": "ambiguous.",
        })
        report = assess_activation(
            activation_report=_sample_report(),
            program_artifacts={},
            client=client,
            system_prompt="sys",
            judge_model="frontier",
            git_sha="abc123",
            target_gates=("AG-1-COUNTERFACTUAL-DIFF",),
        )
        packets = report.human_decision_packets()
        assert len(packets) == 1
        assert packets[0].status == STATUS_AMBIGUOUS
        assert len(packets[0].alternatives) == 2
        assert "degrade" in packets[0].decision_context


class TestFlipNeverAutoExecutable:
    """AG-4/AG-18: a shadow→primary flip is the highest-blast-radius event and
    is ALWAYS a human action, even when the judge endorses it."""

    def test_flip_verdict_is_not_auto_executable(self) -> None:
        # Even an endorsed flip must carry auto_executable=False (the prompt
        # instructs the judge, but we verify the parsed verdict honors it).
        client = FakeJudgeClient({
            "verdicts": [{
                "gate_id": "milestone.completed-flip", "status": "PASS",
                "confidence": 0.9, "bar": "B", "blocker_type": "none",
                "reasoning": "5 clean cycles, kappa 0.8, rollback drill passed",
                "auto_executable": False,  # the judge correctly says False
                "flip_assessment": {"flip_safe": True, "human_action_required": True},
            }],
            "summary": "flip endorsed but human action required.",
        })
        report = assess_activation(
            activation_report={"checks": [{"check_id": "milestone.completed-flip",
                                            "status": "pass", "summary": ""}],
                               "family_matrix": [], "git_sha": "x"},
            program_artifacts={},
            client=client,
            system_prompt="sys",
            judge_model="frontier",
            git_sha="x",
        )
        v = report.verdicts[0]
        assert v.status == STATUS_PASS
        assert v.auto_executable is False
        assert v.flip_assessment["human_action_required"] is True


class TestPromptAssembly:
    def test_user_prompt_carries_real_evidence(self) -> None:
        prompt = build_judge_user_prompt(_sample_report(), program_artifacts={"last_cycle": {"enumerated": 0}})
        # The judge receives the real deterministic findings + artifacts.
        assert "AG-1-COUNTERFACTUAL-DIFF" in prompt
        assert "no counterfactual render artifacts supplied" in prompt
        assert "enumerated" in prompt  # artifact data present

    def test_target_gates_filter(self) -> None:
        prompt = build_judge_user_prompt(_sample_report(), target_gates=("AG-2-CORPUS-CERTIFICATION",))
        assert "AG-2-CORPUS-CERTIFICATION" in prompt
        assert "AG-1-COUNTERFACTUAL-DIFF" not in prompt

    def test_truncates_large_artifacts(self) -> None:
        big = {"rows": list(range(1000))}
        prompt = build_judge_user_prompt(_sample_report(), program_artifacts={"corpus": big})
        assert "_truncated" in prompt  # large list trimmed for token budget


class TestJudgeIndependenceGuard:
    def test_verify_judge_independence_rejects_equal_deployments(self, monkeypatch) -> None:
        from src.core.rev.quality_metrics import verify_judge_independence
        monkeypatch.setenv("VERTEX_AI_DEPLOYMENT", "gpt-5")
        monkeypatch.setenv("VERTEX_AI_JUDGE_DEPLOYMENT", "gpt-5")
        ok, msg = verify_judge_independence()
        assert ok is False
        assert "must use a different deployment" in msg

    def test_verify_judge_independence_rejects_unset_judge(self, monkeypatch) -> None:
        from src.core.rev.quality_metrics import verify_judge_independence
        monkeypatch.setenv("VERTEX_AI_DEPLOYMENT", "gpt-5")
        monkeypatch.delenv("VERTEX_AI_JUDGE_DEPLOYMENT", raising=False)
        ok, msg = verify_judge_independence()
        assert ok is False
        assert "VERTEX_AI_JUDGE_DEPLOYMENT not set" in msg


class TestDeterministicAssessor:
    """The no-LLM fallback: same evidence, same falsifiability rules, optimal
    sequence + human decisions always available."""

    def test_classifies_blocker_types(self) -> None:
        report = assess_activation_deterministic(
            activation_report=_sample_report(),
            program_artifacts={},
            git_sha="abc123",
        )
        by_id = {v.gate_id: v for v in report.verdicts}
        # AG-1 is a data blocker (needs real render artifacts).
        assert by_id["AG-1-COUNTERFACTUAL-DIFF"].blocker_type == "data"
        assert by_id["AG-1-COUNTERFACTUAL-DIFF"].status == STATUS_FAIL
        # AG-9 is wired (PASS).
        assert by_id["AG-9-CONFLICT-WIRED"].status == STATUS_PASS

    def test_produces_optimal_sequence(self) -> None:
        report = assess_activation_deterministic(
            activation_report=_sample_report(),
            program_artifacts={},
            git_sha="abc123",
        )
        # The sample report has AG-1 (data) + AG-2 (corpus) red → both in sequence.
        assert len(report.sequence_recommendation) >= 1
        # Corpus annotation appears because AG-2-CORPUS-CERTIFICATION is red.
        assert any("annotator" in s or "P2" in s for s in report.sequence_recommendation)
        # Counterfactual render appears because AG-1-COUNTERFACTUAL-DIFF is red.
        assert any("counterfactual" in s or "P6" in s for s in report.sequence_recommendation)

    def test_summary_breaks_down_blockers(self) -> None:
        report = assess_activation_deterministic(
            activation_report=_sample_report(),
            program_artifacts={},
            git_sha="abc123",
        )
        assert "code gaps" in report.summary
        assert "Zero code gaps" in report.summary or "Code gaps remain" in report.summary

    def test_flip_readiness_fails_on_uncertified_corpus(self) -> None:
        report = assess_activation_deterministic(
            activation_report=_sample_report(),
            program_artifacts={"quality_metrics": {"kappa": None, "g_xtract_prec_ci_low": None}},
            git_sha="abc123",
            flip_family="milestone.completed",
        )
        flip_v = next(v for v in report.verdicts if v.gate_id == "milestone.completed-FLIP-READINESS")
        assert flip_v.status == STATUS_FAIL
        assert flip_v.auto_executable is False  # NEVER auto-flip
        assert flip_v.flip_assessment["human_action_required"] is True

    def test_flip_readiness_passes_on_certified_corpus_but_still_human(self) -> None:
        report = assess_activation_deterministic(
            activation_report=_sample_report(),
            program_artifacts={"quality_metrics": {"kappa": 0.82, "g_xtract_prec_ci_low": 0.85}},
            git_sha="abc123",
            flip_family="milestone.completed",
        )
        flip_v = next(v for v in report.verdicts if v.gate_id == "milestone.completed-FLIP-READINESS")
        assert flip_v.status == STATUS_PASS
        # Even when endorsed, the flip is a human action (AG-4/AG-18).
        assert flip_v.auto_executable is False
        assert flip_v.flip_assessment["human_action_required"] is True
