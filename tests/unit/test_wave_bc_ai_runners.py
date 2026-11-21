from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from src.ai.ai_mode import AIMode
from src.ai.anticipation_engine import (
    POLICY_VERSION as ANTICIPATION_POLICY_VERSION,
    anticipate_questions,
    anticipate_questions_via_runner,
)
from src.ai.decision_brief_advisor import (
    POLICY_VERSION as BRIEF_POLICY_VERSION,
    advise_on_decision_brief,
    advise_on_decision_brief_via_runner,
)
from src.ai.intent_router import IntentRouter, IntentRouterError, PROMPT_VERSION as INTENT_PROMPT_VERSION
from src.ai.m365_topic_router import M365TopicRouter, PROMPT_VERSION as M365_PROMPT_VERSION
from src.ai.program_synthesizer import (
    POLICY_VERSION as PROG_SYNTH_POLICY_VERSION,
    generate_program_synthesis,
    generate_program_synthesis_via_runner,
)
from src.ai.structured_feature_runner import RunnerContext
from src.ai.summary_generator import (
    POLICY_VERSION as SUMMARY_POLICY_VERSION,
    SummaryGenerator,
    SummaryGeneratorError,
)
from src.ai.synthesizer import (
    POLICY_VERSION as SYNTH_POLICY_VERSION,
    SynthesizerError,
    WorkstreamSynthesizer,
)
from src.commands import ask as ask_cmd
from src.commands import decision_brief as decision_brief_cmd
from src.commands import summarize as summarize_cmd
from src.commands import synthesize as synthesize_cmd
from src.core.decision_brief_engine import DecisionBrief, DecisionItem, DecisionSignal
from src.core.exceptions import StateError
from src.core.ledger.event_log import read_events
from src.core.models import Confidence, ReviewState, RiskLevel, WorkItem
from src.core.models_v2 import LeadershipReader, Program, Signal, Workstream
from src.core.program_synthesis import ProgramSynthesisRequest, SynthesisInputItem
from src.core.trajectory_analyzer import DriftPattern
from src.core.view_models import WorkstreamData


class _StaticClient:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str, str | None]] = []
        self.deployment = "synthetic-deployment"

    def structured(
        self,
        system: str,
        user: str,
        *,
        parser,
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ):
        del max_tokens
        self.calls.append((system, user, prompt_version))
        return parser(self.payload)

    def chat(self, system: str, user: str, **kwargs: object) -> str:
        del system, user, kwargs
        return ""


def _release_terminals(program_id: str, programs_root: Path) -> list[dict[str, object]]:
    return [
        dict(event.payload)
        for event in read_events(program_id, programs_root=programs_root)
        if event.event_type == "ai.release_decision.v1"
    ]


def _ctx(program_id: str, policy_version: str) -> RunnerContext:
    return RunnerContext(
        program_id=program_id,
        run_id="run-bc-1",
        policy_version=policy_version,
        snapshot_id="a" * 64,
        principal="operator",
        classification="public",
    )


def test_anticipation_runner_program_and_unbound_parity(tmp_path: Path) -> None:
    readers = (LeadershipReader(name="Jordan Lee", cares_about=("ramp timeline",)),)
    drift = (
        DriftPattern(
            work_item_id=900001,
            pattern="eta_drift",
            severity="high",
            detail="Target date slipped 3 times in the last 90 days.",
            occurrences=3,
            window_days=90,
        ),
    )
    workstreams = (
        WorkstreamData(
            section_id="deployment_readiness",
            title="Deployment Readiness",
            blurb="Ramp timeline remains conditional.",
            dependency_cascades=(),
            items=(
                WorkItem(
                    id=900001,
                    type="Feature",
                    title="UD chunking rollout",
                    state="Active",
                    assigned_to="Operator",
                    assigned_to_email="operator@example.com",
                    area_path="One\\Adventure\\Acme\\Deployment",
                    iteration_path="FY26\\Sprint 20",
                    target_date=None,
                    risk_level=RiskLevel.MEDIUM,
                    tags=[],
                    custom_fields={},
                    revisions=[],
                    comments=[],
                    fetched_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                ),
            ),
            citations=(),
            review_state=ReviewState.PENDING,
            risk=RiskLevel.MEDIUM,
            prior_risk=RiskLevel.MEDIUM,
            total_items=1,
        ),
    )
    payload = {
        "question": "Why did UD chunking rollout slip three times before ramp?",
        "suggested_response": "Review the latest ramp dependency exit dates.",
    }
    legacy = anticipate_questions(
        readers=readers,
        signals=(),
        drift_patterns=drift,
        summaries={"deployment_readiness": "Ramp timeline remains conditional."},
        workstreams=workstreams,
        client=_StaticClient(payload),
        program_id="demo",
        programs_root=tmp_path / "legacy",
    )
    runner_root = tmp_path / "runner"
    runner = anticipate_questions_via_runner(
        context=_ctx("demo", ANTICIPATION_POLICY_VERSION),
        readers=readers,
        signals=(),
        drift_patterns=drift,
        summaries={"deployment_readiness": "Ramp timeline remains conditional."},
        workstreams=workstreams,
        client=_StaticClient(payload),
        program_id="demo",
        programs_root=runner_root,
    )
    assert runner == legacy
    assert _release_terminals("demo", runner_root)[-1]["terminal"] == "released"

    # Unbound program_id=None executes without audit trail and falls back deterministically on malformed AI output.
    unbound_client = _StaticClient({"question": "Missing question mark", "suggested_response": "Ok"})
    unbound = anticipate_questions_via_runner(
        context=_ctx("unbound-review", ANTICIPATION_POLICY_VERSION),
        readers=readers,
        signals=(),
        drift_patterns=drift,
        summaries={"deployment_readiness": "Ramp timeline remains conditional."},
        workstreams=workstreams,
        client=unbound_client,
        program_id=None,
        programs_root=tmp_path / "unbound",
    )
    assert len(unbound) == len(legacy)
    assert all(q.question.endswith("?") for q in unbound)


def test_decision_brief_advisor_runner_parity_and_command_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = DecisionItem(
        section_id="risks",
        section_title="Risks",
        current_text="Vendor delivery risk remains open.",
        proposed_text="Escalate vendor delivery risk.",
        evidence_delta_lines=("Vendor confirmed a two-week slip.",),
        top_signals=(DecisionSignal(signal_id="sig-1", text="Vendor slip confirmed.", timestamp="2026-06-01", source="email"),),
        vitality_summary="declining",
        confidence="medium",
        kpi_summary="On-time: 62%",
        stale_claims=(),
        accept_command="vertex accept --id risks",
        reject_command="vertex reject --id risks",
        accept_modified_command="vertex accept --id risks --modified",
    )
    brief = DecisionBrief(
        issue_number=12,
        edition_name="acme_weekly",
        generated_at="2026-06-06 12:00",
        items=(item,),
        total_pending=1,
        ai_enriched=False,
    )
    payload = {"verdict": "ACCEPT", "reasoning": "Evidence aligns.", "suggested_text": None}

    legacy = advise_on_decision_brief(
        client=_StaticClient(payload), brief=brief, program_id="demo", programs_root=tmp_path / "legacy",
    )
    runner_root = tmp_path / "runner"
    runner = advise_on_decision_brief_via_runner(
        context=_ctx("demo", BRIEF_POLICY_VERSION),
        client=_StaticClient(payload),
        brief=brief,
        program_id="demo",
        programs_root=runner_root,
    )
    assert runner == legacy
    assert _release_terminals("demo", runner_root)[-1]["terminal"] == "released"

    monkeypatch.delenv("VERTEX_OPERATOR_PRINCIPAL", raising=False)
    monkeypatch.delenv("VERTEX_OPERATOR_MACHINE", raising=False)
    monkeypatch.setattr(decision_brief_cmd, "get_ai_mode", lambda: AIMode.ACTIVE)
    cmd_root = tmp_path / "cmd"
    cmd_client = _StaticClient(payload)
    enriched = decision_brief_cmd._enrich_with_ai(
        brief=brief,
        bundle=SimpleNamespace(),
        program_id="demo",
        create_ai_client=lambda _bundle: cmd_client,
        programs_root=cmd_root,
        use_structured_runner=True,
    )
    assert enriched.ai_enriched is True
    assert enriched.items[0].verdict == "ACCEPT"


def test_program_synthesizer_runner_parity_and_rejection(tmp_path: Path) -> None:
    request = ProgramSynthesisRequest(
        program_id="demo",
        as_of=datetime(2026, 7, 1, tzinfo=timezone.utc),
        items=(SynthesisInputItem(category="strategic_risk", item_id="risk-1", summary="Vendor delay."),),
        coverage_notes=("note",),
    )
    payload = {
        "through_line": "Vendor delay threatens target date.",
        "long_poles": ["risk-1"],
        "facts": ["Vendor X reported a delay."],
        "inferences": ["The delay may push the milestone."],
        "recommendations": [{"text": "Escalate vendor delay.", "evidence_refs": ["risk-1"]}],
    }
    legacy = generate_program_synthesis(
        request, client=_StaticClient(payload), programs_root=tmp_path / "legacy",
    )
    runner_root = tmp_path / "runner"
    runner = generate_program_synthesis_via_runner(
        request,
        context=_ctx("demo", PROG_SYNTH_POLICY_VERSION),
        client=_StaticClient(payload),
        programs_root=runner_root,
    )
    assert legacy.released and runner.released
    assert runner.synthesis is not None and legacy.synthesis is not None
    assert runner.synthesis.through_line == legacy.synthesis.through_line
    assert _release_terminals("demo", runner_root)[-1]["terminal"] == "released"

    rejected = generate_program_synthesis_via_runner(
        request,
        context=_ctx("demo", PROG_SYNTH_POLICY_VERSION),
        client=_StaticClient({"through_line": "", "recommendations": []}),
        programs_root=tmp_path / "rejected",
    )
    assert not rejected.released
    assert rejected.synthesis is None


def test_intent_and_m365_topic_router_runners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VERTEX_OPERATOR_PRINCIPAL", raising=False)
    monkeypatch.delenv("VERTEX_OPERATOR_MACHINE", raising=False)

    # 1. IntentRouter via runner + ask command opt-in.
    prog_dir = tmp_path / "demo"
    prog_dir.mkdir(parents=True)
    (prog_dir / "program.yaml").write_text("schema_version: '2.0'\nid: demo\nname: Demo\n", encoding="utf-8")
    (prog_dir / "editions").mkdir()
    (prog_dir / "editions" / "demo_weekly.yaml").write_text(
        "schema_version: '2.0'\nid: demo_weekly\nprogram_id: demo\n",
        encoding="utf-8",
    )

    ai_payload = {
        "command": "status",
        "args": ["--edition", "demo_weekly"],
        "warnings": [],
    }
    router_client = _StaticClient(ai_payload)
    inv = IntentRouter(client=router_client).route(
        "Please summarize current open concerns for leadership review.",
        default_edition="demo_weekly",
        programs_root=tmp_path,
        use_structured_runner=True,
    )
    assert inv.command == "status"
    assert _release_terminals("demo", tmp_path)[-1]["terminal"] == "released"

    bad_client = _StaticClient({"command": "nonexistent-command", "args": [], "confidence": "high", "reasoning": "x"})
    with pytest.raises(IntentRouterError):
        IntentRouter(client=bad_client).route(
            "Unrecognized request that falls through.",
            default_edition="demo_weekly",
            programs_root=tmp_path,
            use_structured_runner=True,
            runner_context=_ctx("demo", INTENT_PROMPT_VERSION),
        )

    fake_router = Mock()
    fake_router.route.return_value = inv
    monkeypatch.setattr(ask_cmd, "_build_router", lambda: fake_router)
    rendered = ask_cmd.route_request("status", edition="demo_weekly", use_structured_runner=True)
    assert "demo_weekly" in rendered
    assert fake_router.route.call_args.kwargs["use_structured_runner"] is True

    # 2. M365TopicRouter via runner.
    workstream = Workstream(id="security", name="Security & Auth", description="Auth hardening")
    m365_client = _StaticClient(
        {
            "workstream_id": "security",
            "confidence": 0.91,
            "topics": ["auth"],
            "reasoning": "Matches security workstream.",
        }
    )
    m365_root = tmp_path / "m365"
    topic_router = M365TopicRouter(
        client=m365_client,
        program_id="demo",
        programs_root=m365_root,
        use_structured_runner=True,
        runner_context=_ctx("demo", M365_PROMPT_VERSION),
    )
    decision = topic_router.route_artifact(
        display_name="Weekly security review",
        subject_or_title="Review auth agenda",
        participant_aliases=("operator",),
        sample_text="Discussed token hardening work item status.",
        workstream_profiles=(workstream,),
    )
    assert decision.workstream_id == "security"
    assert _release_terminals("demo", m365_root)[-1]["terminal"] == "released"


def test_summary_generator_and_synthesizer_runners_and_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VERTEX_OPERATOR_PRINCIPAL", raising=False)
    monkeypatch.delenv("VERTEX_OPERATOR_MACHINE", raising=False)

    program = Program(schema_version="2.0", id="demo", name="Demo")
    workstream = Workstream(id="networking", name="Networking", description="Networking lane")
    signal = Signal(
        id="sig-1",
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="manual",
        program_id="demo",
        workstream_id="networking",
        entity_refs=("WI:1234",),
        text="Servicing validation moved to 2026-05-17.",
        raw_ref=None,
        confidence=Confidence.HIGH,
    )

    # 1. SummaryGenerator parity + tamper failure.
    sum_payload = {"text": "Networking tracking WI:1234 moved target date cleanly."}
    legacy_sum = SummaryGenerator(client=_StaticClient(sum_payload)).generate(
        program=program,
        workstream=workstream,
        prior_summary=None,
        signals=(signal,),
        drift_patterns=(),
        programs_root=tmp_path / "sum_legacy",
    )
    sum_root = tmp_path / "sum_runner"
    runner_sum = SummaryGenerator(client=_StaticClient(sum_payload)).generate_via_runner(
        context=_ctx("demo", SUMMARY_POLICY_VERSION),
        program=program,
        workstream=workstream,
        prior_summary=None,
        signals=(signal,),
        drift_patterns=(),
        programs_root=sum_root,
    )
    assert runner_sum == legacy_sum
    assert _release_terminals("demo", sum_root)[-1]["terminal"] == "released"

    cache_db = sum_root / "demo" / "runtime" / "ai_released_result_cache.sqlite3"
    with sqlite3.connect(cache_db) as conn:
        conn.execute("UPDATE released_result_cache SET payload = ?", (json.dumps({"text": "Tampered"}),))
        conn.commit()
    with pytest.raises(StateError):
        SummaryGenerator(client=_StaticClient(sum_payload)).generate_via_runner(
            context=_ctx("demo", SUMMARY_POLICY_VERSION),
            program=program,
            workstream=workstream,
            prior_summary=None,
            signals=(signal,),
            drift_patterns=(),
            programs_root=sum_root,
        )

    # summarize._summarize_workstream opt-in calls generate_via_runner.
    fake_gen = Mock(spec=SummaryGenerator)
    fake_gen.generate_via_runner.return_value = runner_sum
    monkeypatch.setattr(summarize_cmd, "_load_drift_patterns", lambda **_kwargs: ())
    monkeypatch.setattr(summarize_cmd, "save_summary", lambda *args, **kwargs: None)
    sum_cmd_res = summarize_cmd._summarize_workstream(
        program_id="demo",
        program=program,
        workstream=workstream,
        current_time=datetime(2026, 5, 11, tzinfo=timezone.utc),
        approved_signals=(signal,),
        prior_summary=None,
        reset=False,
        people_directory=(),
        programs_root=tmp_path / "sum_cmd",
        generator=fake_gen,
        use_structured_runner=True,
    )
    assert sum_cmd_res.status == "written"
    assert fake_gen.generate_via_runner.call_count == 1

    # 2. WorkstreamSynthesizer parity + empty refusal + synthesize command opt-in.
    synth_payload = {
        "overall_assessment": "Networking remains the gating lane.",
        "proposed_risk": "high",
        "confidence": "medium",
        "key_findings": ["Target date slipped again."],
        "evidence_refs": ["sig-1"],
        "open_questions": ["Who owns sign-off?"],
        "recommended_actions": ["Lock next validation checkpoint."],
    }
    legacy_synth = WorkstreamSynthesizer(client=_StaticClient(synth_payload)).generate(
        program=program,
        workstream=workstream,
        signals=(signal,),
        drift_patterns=(),
        programs_root=tmp_path / "synth_legacy",
    )
    synth_root = tmp_path / "synth_runner"
    runner_synth = WorkstreamSynthesizer(client=_StaticClient(synth_payload)).generate_via_runner(
        context=_ctx("demo", SYNTH_POLICY_VERSION),
        program=program,
        workstream=workstream,
        signals=(signal,),
        drift_patterns=(),
        programs_root=synth_root,
    )
    assert legacy_synth is not None and runner_synth is not None
    assert runner_synth.synthesis.overall_assessment == legacy_synth.synthesis.overall_assessment
    assert _release_terminals("demo", synth_root)[-1]["terminal"] == "released"

    with pytest.raises(SynthesizerError):
        WorkstreamSynthesizer(client=_StaticClient({"overall_assessment": ""})).generate_via_runner(
            context=_ctx("demo", SYNTH_POLICY_VERSION),
            program=program,
            workstream=workstream,
            signals=(signal,),
            drift_patterns=(),
            programs_root=tmp_path / "synth_bad",
        )
    assert SummaryGeneratorError is not SynthesizerError


def test_runner_bridge_error_branches_and_cache_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.ai import _runner_bridge as bridge
    from src.ai import summary_generator as sg_mod
    from src.ai._pipeline import AIPipelineError

    monkeypatch.setattr(bridge, "capture_operator_identity", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("no id")))
    with pytest.raises(StateError, match="native OS principal"):
        bridge.build_pinned_runner_context(program_id="demo", caller="test", feature="f", policy_version="v1", programs_root=tmp_path)

    monkeypatch.setattr(bridge, "capture_operator_identity", lambda *_a, **_kw: SimpleNamespace(principal="  "))
    with pytest.raises(StateError, match="native OS principal"):
        bridge.build_pinned_runner_context(program_id="demo", caller="test", feature="f", policy_version="v1", programs_root=tmp_path)

    monkeypatch.setattr(bridge, "capture_operator_identity", lambda *_a, **_kw: SimpleNamespace(principal="op@example.com"))
    monkeypatch.setattr(
        bridge.ProgramFactStore,
        "pin_snapshot",
        lambda self, **_kw: (_ for _ in ()).throw(ValueError("bad pin")),
    )
    with pytest.raises(StateError, match="snapshot could not be pinned"):
        bridge.build_pinned_runner_context(program_id="demo", caller="test", feature="f", policy_version="v1", programs_root=tmp_path)

    monkeypatch.setattr(
        bridge.ProgramFactStore,
        "pin_snapshot",
        lambda self, **_kw: SimpleNamespace(program_id="other", content_sha256="a" * 64, snapshot_id="s1"),
    )
    monkeypatch.setattr(bridge.ProgramFactStore, "detect_drift", lambda self, _sid: False)
    with pytest.raises(StateError, match="does not match"):
        bridge.build_pinned_runner_context(program_id="demo", caller="test", feature="f", policy_version="v1", programs_root=tmp_path)

    monkeypatch.setattr(
        bridge.ProgramFactStore,
        "pin_snapshot",
        lambda self, **_kw: SimpleNamespace(program_id="demo", content_sha256="a" * 64, snapshot_id="s1"),
    )
    monkeypatch.setattr(bridge.ProgramFactStore, "detect_drift", lambda self, _sid: True)
    with pytest.raises(StateError, match="changed before"):
        bridge.build_pinned_runner_context(program_id="demo", caller="test", feature="f", policy_version="v1", programs_root=tmp_path)

    program = Program(schema_version="2.0", id="demo", name="Demo")
    workstream = Workstream(id="networking", name="Networking", description="Networking lane")
    signal = Signal(
        id="sig-1",
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="manual",
        program_id="demo",
        workstream_id="networking",
        entity_refs=("WI:1234",),
        text="Servicing validation moved to 2026-05-17.",
        raw_ref=None,
        confidence=Confidence.HIGH,
    )

    assert SummaryGenerator(client=None).generate_via_runner(
        context=_ctx("demo", SUMMARY_POLICY_VERSION), program=program, workstream=workstream,
        prior_summary=None, signals=(signal,), drift_patterns=(), programs_root=tmp_path,
    ) is None
    assert SummaryGenerator(client=_StaticClient({"text": "ok"})).generate_via_runner(
        context=_ctx("demo", SUMMARY_POLICY_VERSION), program=program, workstream=workstream,
        prior_summary="  ", signals=(), drift_patterns=(), programs_root=tmp_path,
    ) is None

    monkeypatch.setattr(sg_mod, "process_generated_text", lambda _t: (_ for _ in ()).throw(AIPipelineError("bad ai text")))
    with pytest.raises(SummaryGeneratorError, match="bad ai text"):
        SummaryGenerator(client=_StaticClient({"text": "Networking status WI:1234."})).generate_via_runner(
            context=_ctx("demo", SUMMARY_POLICY_VERSION), program=program, workstream=workstream,
            prior_summary=None, signals=(signal,), drift_patterns=(), programs_root=tmp_path / "sg_err",
        )
    monkeypatch.setattr(sg_mod, "process_generated_text", lambda t: SimpleNamespace(text=""))
    assert SummaryGenerator(client=_StaticClient({"text": "Networking status WI:1234."})).generate_via_runner(
        context=_ctx("demo", SUMMARY_POLICY_VERSION), program=program, workstream=workstream,
        prior_summary=None, signals=(signal,), drift_patterns=(), programs_root=tmp_path / "sg_empty",
    ) is None
    monkeypatch.setattr(sg_mod, "process_generated_text", lambda t: SimpleNamespace(text=t))
    monkeypatch.setattr(sg_mod, "_validate_semantics", lambda _t, **_kw: ["semantic finding"])
    with pytest.raises(SummaryGeneratorError, match="semantic finding"):
        SummaryGenerator(client=_StaticClient({"text": "Networking status WI:1234."})).generate_via_runner(
            context=_ctx("demo", SUMMARY_POLICY_VERSION), program=program, workstream=workstream,
            prior_summary=None, signals=(signal,), drift_patterns=(), programs_root=tmp_path / "sg_sem",
        )

    readers = (LeadershipReader(name="Jordan Lee", cares_about=("ramp timeline",)),)
    assert anticipate_questions_via_runner(
        context=_ctx("demo", ANTICIPATION_POLICY_VERSION),
        readers=readers,
        signals=(),
        drift_patterns=(),
        summaries={},
        workstreams=(),
        client=None,
        program_id="demo",
        programs_root=tmp_path,
    ) == ()
    drift = (
        DriftPattern(
            work_item_id=900001,
            pattern="eta_drift",
            severity="high",
            detail="Target date slipped 3 times in the last 90 days.",
            occurrences=3,
            window_days=90,
        ),
    )

    workstreams = (
        WorkstreamData(
            section_id="deployment_readiness",
            title="Deployment Readiness",
            blurb="Ramp timeline remains conditional.",
            dependency_cascades=(),
            items=(
                WorkItem(
                    id=900001,
                    type="Feature",
                    title="UD chunking rollout",
                    state="Active",
                    assigned_to="Operator",
                    assigned_to_email="operator@example.com",
                    area_path="One\\Adventure\\Acme\\Deployment",
                    iteration_path="FY26\\Sprint 20",
                    target_date=None,
                    risk_level=RiskLevel.MEDIUM,
                    tags=[],
                    custom_fields={},
                    revisions=[],
                    comments=[],
                    fetched_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                ),
            ),
            citations=(),
            review_state=ReviewState.PENDING,
            risk=RiskLevel.MEDIUM,
            prior_risk=RiskLevel.MEDIUM,
            total_items=1,
        ),
    )

    class _BoomClient:
        deployment = "synthetic-deployment"

        def structured(self, *_a, **_kw):
            raise RuntimeError("provider down")

    out = anticipate_questions_via_runner(
        context=_ctx("demo", ANTICIPATION_POLICY_VERSION),
        readers=readers,
        signals=(),
        drift_patterns=drift,
        summaries={"deployment_readiness": "Ramp timeline remains conditional."},
        workstreams=workstreams,
        client=_BoomClient(),
        program_id="demo",
        programs_root=tmp_path / "ant",
    )
    assert len(out) >= 1

    replay_root = tmp_path / "replay"
    client = _StaticClient({"text": "Networking tracking WI:1234 moved target date cleanly."})
    monkeypatch.setattr(sg_mod, "_validate_semantics", lambda _t, **_kw: [])
    first = SummaryGenerator(client=client).generate_via_runner(
        context=_ctx("demo", SUMMARY_POLICY_VERSION), program=program, workstream=workstream,
        prior_summary=None, signals=(signal,), drift_patterns=(), programs_root=replay_root,
    )
    assert first is not None and len(client.calls) == 1
    second = SummaryGenerator(client=client).generate_via_runner(
        context=_ctx("demo", SUMMARY_POLICY_VERSION), program=program, workstream=workstream,
        prior_summary=None, signals=(signal,), drift_patterns=(), programs_root=replay_root,
    )
    assert second is not None and second.text == first.text and len(client.calls) == 1


def test_runner_bridge_unbound_and_lifecycle_failure_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.ai import _runner_bridge as bridge
    SchemaGatewayError = bridge.SchemaGatewayError

    res_unbound = bridge.run_structured_feature_via_runner(
        feature="f",
        task_class="synthesis",
        context=_ctx("unbound", "v1"),
        program_id=None,
        prompt_version="v1",
        policy_version="v1",
        programs_root=tmp_path / "u1",
        system_prompt="sys",
        user_prompt="usr",
        output_schema_text="{}",
        request_payload={"k": "v"},
        deployment_fingerprint="dep",
        validator_version="v1",
        invoke_provider=lambda _c: {"ok": True},
        parse_and_validate=lambda r: r,
        serialize_result=lambda _r, v: v,
        error_factory=RuntimeError,
    )
    assert res_unbound == {"ok": True}

    res_empty = bridge.run_structured_feature_via_runner(
        feature="f",
        task_class="synthesis",
        context=_ctx("demo", "v1"),
        program_id="demo",
        prompt_version="v1",
        policy_version="v1",
        programs_root=tmp_path / "e1",
        system_prompt="sys",
        user_prompt="usr",
        output_schema_text="{}",
        request_payload={"k": "v"},
        deployment_fingerprint="dep",
        validator_version="v1",
        invoke_provider=lambda _c: {"ok": False},
        parse_and_validate=lambda _r: None,
        serialize_result=lambda r, _v: r,
        error_factory=RuntimeError,
        raise_on_rejection=False,
        empty_value=None,
    )
    assert res_empty is None

    with pytest.raises(RuntimeError, match="provider boom"):
        bridge.run_structured_feature_via_runner(
            feature="f",
            task_class="synthesis",
            context=_ctx("demo", "v1"),
            program_id="demo",
            prompt_version="v1",
            policy_version="v1",
            programs_root=tmp_path / "p1",
            system_prompt="sys",
            user_prompt="usr",
            output_schema_text="{}",
            request_payload={"k": "v"},
            deployment_fingerprint="dep",
            validator_version="v1",
            invoke_provider=lambda _c: (_ for _ in ()).throw(RuntimeError("provider boom")),
            parse_and_validate=lambda r: r,
            serialize_result=lambda _r, v: v,
            error_factory=RuntimeError,
            provider_error_map=None,
        )

    monkeypatch.setattr(bridge, "record_ai_run_lifecycle", lambda **_kw: (_ for _ in ()).throw(RuntimeError("lc down")))
    with pytest.raises(RuntimeError, match="lc down"):
        bridge.run_structured_feature_via_runner(
            feature="f",
            task_class="synthesis",
            context=_ctx("demo", "v1"),
            program_id="demo",
            prompt_version="v1",
            policy_version="v1",
            programs_root=tmp_path / "lc1",
            system_prompt="sys",
            user_prompt="usr",
            output_schema_text="{}",
            request_payload={"k": "v"},
            deployment_fingerprint="dep",
            validator_version="v1",
            invoke_provider=lambda _c: {"ok": True},
            parse_and_validate=lambda r: r,
            serialize_result=lambda _r, v: v,
            error_factory=RuntimeError,
        )
    monkeypatch.undo()
    monkeypatch.setattr(bridge, "validate_bounded_payload", lambda _p: (_ for _ in ()).throw(SchemaGatewayError("bad gw")))
    with pytest.raises(RuntimeError, match="bad gw"):
        bridge.run_structured_feature_via_runner(
            feature="f",
            task_class="synthesis",
            context=_ctx("demo", "v1"),
            program_id="demo",
            prompt_version="v1",
            policy_version="v1",
            programs_root=tmp_path / "sgw1",
            system_prompt="sys",
            user_prompt="usr",
            output_schema_text="{}",
            request_payload={"k": "v"},
            deployment_fingerprint="dep",
            validator_version="v1",
            invoke_provider=lambda _c: {"ok": True},
            parse_and_validate=lambda r: r,
            serialize_result=lambda _r, v: v,
            error_factory=RuntimeError,
        )
    monkeypatch.undo()

    # Anticipation engine runner state mismatch propagation & non-state fallback logging.
    from src.ai import anticipation_engine as ae_mod

    readers = (LeadershipReader(name="Jordan Lee", cares_about=("ramp timeline",)),)
    drift = (
        DriftPattern(
            work_item_id=900001,
            pattern="eta_drift",
            severity="high",
            detail="Target date slipped 3 times in the last 90 days.",
            occurrences=3,
            window_days=90,
        ),
    )
    workstreams = (
        WorkstreamData(
            section_id="deployment_readiness",
            title="Deployment Readiness",
            blurb="Ramp timeline remains conditional.",
            dependency_cascades=(),
            items=(
                WorkItem(
                    id=900001,
                    type="Feature",
                    title="UD chunking rollout",
                    state="Active",
                    assigned_to="Operator",
                    assigned_to_email="operator@example.com",
                    area_path="One\\Adventure\\Acme\\Deployment",
                    iteration_path="FY26\\Sprint 20",
                    target_date=None,
                    risk_level=RiskLevel.MEDIUM,
                    tags=[],
                    custom_fields={},
                    revisions=[],
                    comments=[],
                    fetched_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                ),
            ),
            citations=(),
            review_state=ReviewState.PENDING,
            risk=RiskLevel.MEDIUM,
            prior_risk=RiskLevel.MEDIUM,
            total_items=1,
        ),
    )
    with pytest.raises(StateError, match="program must match"):
        anticipate_questions_via_runner(
            context=_ctx("other", ANTICIPATION_POLICY_VERSION),
            readers=readers,
            signals=(),
            drift_patterns=drift,
            summaries={"deployment_readiness": "Ramp timeline remains conditional."},
            workstreams=workstreams,
            client=_StaticClient({"question": "Why?", "suggested_response": "Check dates."}),
            program_id="demo",
            programs_root=tmp_path / "ae_mismatch",
        )
    monkeypatch.setattr(ae_mod, "run_structured_feature_via_runner", lambda **_kw: (_ for _ in ()).throw(RuntimeError("soft")))
    fallback_qs = anticipate_questions_via_runner(
        context=_ctx("demo", ANTICIPATION_POLICY_VERSION),
        readers=readers,
        signals=(),
        drift_patterns=drift,
        summaries={"deployment_readiness": "Ramp timeline remains conditional."},
        workstreams=workstreams,
        client=_StaticClient({"question": "Why?", "suggested_response": "Check dates."}),
        program_id="demo",
        programs_root=tmp_path / "ae_soft",
    )
    assert len(fallback_qs) >= 1

