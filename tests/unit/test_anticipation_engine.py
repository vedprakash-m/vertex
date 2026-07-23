from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json

import pytest

from src.ai.anticipation_engine import anticipate_questions
from src.core.anticipation_detector import AnticipationFinding
from src.core.models import Confidence, ReviewState, RiskLevel, WorkItem
from src.core.models_v2 import LeadershipReader
from src.core.trajectory_analyzer import DriftPattern
from src.core.view_models import WorkstreamData


def test_anticipate_questions_uses_deterministic_fallback_without_client() -> None:
    anticipated = anticipate_questions(
        readers=(LeadershipReader(name="Jordan Lee", cares_about=("ramp timeline",)),),
        signals=(),
        drift_patterns=(
            DriftPattern(
                work_item_id=900001,
                pattern="eta_drift",
                severity="high",
                detail="Target date slipped 3 times in the last 90 days.",
                occurrences=3,
                window_days=90,
            ),
        ),
        summaries={"deployment_readiness": "Ramp timeline remains conditional."},
        workstreams=(
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
                        assigned_to="Vertex Maintainer",
                        assigned_to_email="maintainer@example.com",
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
        ),
    )

    eta_drift_question = next(question for question in anticipated if question.confidence == Confidence.HIGH)

    assert eta_drift_question.reader == "Jordan Lee"
    assert eta_drift_question.question.startswith("Why has")


@dataclass
class _FakeClient:
    response: str
    calls: int = 0

    def structured(self, system: str, user: str, *, parser, max_tokens: int = 0, prompt_version: str | None = None):
        del system, user, max_tokens, prompt_version
        self.calls += 1
        try:
            payload = json.loads(self.response)
        except json.JSONDecodeError as error:
            from src.ai.client import AIClientError

            raise AIClientError(f"Azure OpenAI structured response returned invalid JSON: {error}") from error
        if not isinstance(payload, dict):
            from src.ai.client import AIClientError

            raise AIClientError("Azure OpenAI structured response returned a non-object payload.")
        return parser(payload)


@dataclass
class _MalformedPayloadClient:
    payload: object

    def structured(self, system: str, user: str, *, parser, max_tokens: int = 0, prompt_version: str | None = None):
        del system, user, max_tokens, prompt_version
        return parser(self.payload)


def test_anticipate_questions_uses_ai_response_when_client_returns_json() -> None:
    anticipated = anticipate_questions(
        readers=(LeadershipReader(name="Jordan Lee", cares_about=("ramp timeline",)),),
        signals=(),
        drift_patterns=(
            DriftPattern(
                work_item_id=900001,
                pattern="eta_drift",
                severity="high",
                detail="Target date slipped 3 times in the last 90 days.",
                occurrences=3,
                window_days=90,
            ),
        ),
        summaries={"deployment_readiness": "Ramp timeline remains conditional."},
        workstreams=(
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
                        assigned_to="Vertex Maintainer",
                        assigned_to_email="maintainer@example.com",
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
        ),
        client=_FakeClient(
            '{"question": "What moved the ramp timeline again?", "suggested_response": "Name the blocker, owner, checkpoint, and consequence."}'
        ),
    )

    eta_drift_question = next(question for question in anticipated if question.confidence == Confidence.HIGH)

    assert eta_drift_question.question == "What moved the ramp timeline again?"
    assert eta_drift_question.suggested_response == "Name the blocker, owner, checkpoint, and consequence."


def test_anticipate_questions_falls_back_when_ai_payload_is_non_object() -> None:
    anticipated = anticipate_questions(
        readers=(LeadershipReader(name="Jordan Lee", cares_about=("ramp timeline",)),),
        signals=(),
        drift_patterns=(
            DriftPattern(
                work_item_id=900001,
                pattern="eta_drift",
                severity="high",
                detail="Target date slipped 3 times in the last 90 days.",
                occurrences=3,
                window_days=90,
            ),
        ),
        summaries={"deployment_readiness": "Ramp timeline remains conditional."},
        workstreams=(
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
                        assigned_to="Vertex Maintainer",
                        assigned_to_email="maintainer@example.com",
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
        ),
        client=_MalformedPayloadClient([]),
    )

    eta_drift_question = next(question for question in anticipated if question.confidence == Confidence.HIGH)

    assert eta_drift_question.question.startswith("Why has")


def test_anticipate_questions_falls_back_when_ai_payload_question_is_not_string() -> None:
    anticipated = anticipate_questions(
        readers=(LeadershipReader(name="Jordan Lee", cares_about=("ramp timeline",)),),
        signals=(),
        drift_patterns=(
            DriftPattern(
                work_item_id=900001,
                pattern="eta_drift",
                severity="high",
                detail="Target date slipped 3 times in the last 90 days.",
                occurrences=3,
                window_days=90,
            ),
        ),
        summaries={"deployment_readiness": "Ramp timeline remains conditional."},
        workstreams=(
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
                        assigned_to="Vertex Maintainer",
                        assigned_to_email="maintainer@example.com",
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
        ),
        client=_MalformedPayloadClient({"question": ["bad"], "suggested_response": "Name the blocker."}),
    )

    eta_drift_question = next(question for question in anticipated if question.confidence == Confidence.HIGH)

    assert eta_drift_question.question.startswith("Why has")


def test_anticipate_questions_falls_back_when_ai_payload_response_is_blank() -> None:
    anticipated = anticipate_questions(
        readers=(LeadershipReader(name="Jordan Lee", cares_about=("ramp timeline",)),),
        signals=(),
        drift_patterns=(
            DriftPattern(
                work_item_id=900001,
                pattern="eta_drift",
                severity="high",
                detail="Target date slipped 3 times in the last 90 days.",
                occurrences=3,
                window_days=90,
            ),
        ),
        summaries={"deployment_readiness": "Ramp timeline remains conditional."},
        workstreams=(
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
                        assigned_to="Vertex Maintainer",
                        assigned_to_email="maintainer@example.com",
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
        ),
        client=_MalformedPayloadClient({"question": "What moved the ramp timeline again?", "suggested_response": "   "}),
    )

    eta_drift_question = next(question for question in anticipated if question.confidence == Confidence.HIGH)

    assert eta_drift_question.question.startswith("Why has")


def test_anticipate_questions_no_program_id_falls_back_when_response_lacks_question_mark() -> None:
    # specs/backlog.md BL-C2: the no-program_id branch previously never
    # called _validate_semantics at all, so an AI response that lost its
    # question mark would have been silently accepted. Now it is checked
    # the same way the program_id branch already does.
    anticipated = anticipate_questions(
        **_base_kwargs(
            client=_MalformedPayloadClient(
                {"question": "What moved the ramp timeline", "suggested_response": "Name the blocker."}
            )
        )
    )

    eta_drift_question = next(question for question in anticipated if question.confidence == Confidence.HIGH)
    assert eta_drift_question.question.startswith("Why has")  # deterministic seed, not the ungrounded AI text


def test_anticipate_questions_no_program_id_falls_back_on_oversized_request() -> None:
    # specs/backlog.md BL-C2: the no-program_id branch previously never
    # bounds-checked the outbound request at all.
    client = _FakeClient(
        '{"question": "What moved the ramp timeline again?", "suggested_response": "Name the blocker, owner, checkpoint, and consequence."}'
    )
    kwargs = _base_kwargs(client=client)
    kwargs["workstreams"] = (
        WorkstreamData(
            section_id="deployment_readiness",
            title="x" * 200_001,
            blurb="Ramp timeline remains conditional.",
            dependency_cascades=(),
            items=(
                WorkItem(
                    id=900001,
                    type="Feature",
                    title="UD chunking rollout",
                    state="Active",
                    assigned_to="Vertex Maintainer",
                    assigned_to_email="maintainer@example.com",
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

    anticipated = anticipate_questions(**kwargs)

    eta_drift_question = next(question for question in anticipated if question.confidence == Confidence.HIGH)
    assert eta_drift_question.question.startswith("Why has")
    assert client.calls == 0


def test_anticipate_questions_rejects_finding_reader_not_in_provided_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.ai.anticipation_engine.detect_anticipated_questions",
        lambda **kwargs: (
            AnticipationFinding(
                reader="Fabricated Reader",
                pattern="eta_drift",
                question_seed="Why did the ramp move?",
                suggested_response_seed="Explain the blocker and next checkpoint.",
                evidence=("Target date slipped 3 times.",),
                confidence=Confidence.HIGH,
            ),
        ),
    )

    with pytest.raises(ValueError, match="Fabricated Reader"):
        anticipate_questions(
            readers=(LeadershipReader(name="Jordan Lee", cares_about=("ramp timeline",)),),
            signals=(),
            drift_patterns=(),
            summaries={},
        )


def _base_kwargs(*, client) -> dict:
    return dict(
        readers=(LeadershipReader(name="Jordan Lee", cares_about=("ramp timeline",)),),
        signals=(),
        drift_patterns=(
            DriftPattern(
                work_item_id=900001,
                pattern="eta_drift",
                severity="high",
                detail="Target date slipped 3 times in the last 90 days.",
                occurrences=3,
                window_days=90,
            ),
        ),
        summaries={"deployment_readiness": "Ramp timeline remains conditional."},
        workstreams=(
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
                        assigned_to="Vertex Maintainer",
                        assigned_to_email="maintainer@example.com",
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
        ),
        client=client,
    )


def test_anticipate_questions_records_released_terminal_when_program_id_provided(tmp_path) -> None:
    # ADF-W5.1/P7: anticipation_engine's AISchemaGateway migration must
    # record a durable QG-29 "released" terminal for a successful
    # AI-rendered question when program_id is supplied.
    from src.core.ledger.event_log import read_events

    client = _FakeClient(
        '{"question": "What moved the ramp timeline again?", "suggested_response": "Name the blocker, owner, checkpoint, and consequence."}'
    )
    anticipated = anticipate_questions(
        **_base_kwargs(client=client), program_id="acme", programs_root=tmp_path
    )

    eta_drift_question = next(question for question in anticipated if question.confidence == Confidence.HIGH)
    assert eta_drift_question.question == "What moved the ramp timeline again?"

    events = read_events("acme", programs_root=tmp_path)
    release_decisions = [event for event in events if event.event_type == "ai.release_decision.v1"]
    assert release_decisions
    assert release_decisions[-1].payload["terminal"] == "released"


def test_anticipate_questions_repeat_identical_request_hits_the_cache(tmp_path) -> None:
    client = _FakeClient(
        '{"question": "What moved the ramp timeline again?", "suggested_response": "Name the blocker, owner, checkpoint, and consequence."}'
    )
    kwargs = _base_kwargs(client=client)

    first = anticipate_questions(**kwargs, program_id="acme", programs_root=tmp_path)
    calls_after_first = client.calls
    second = anticipate_questions(**kwargs, program_id="acme", programs_root=tmp_path)

    first_question = next(question for question in first if question.confidence == Confidence.HIGH)
    second_question = next(question for question in second if question.confidence == Confidence.HIGH)
    assert first_question.question == second_question.question == "What moved the ramp timeline again?"
    # Distinct anticipation findings for this reader/data (eta_drift plus a
    # no-recent-signals finding, since signals=()) each make their own AI
    # call the first time; the second identical invocation must be served
    # entirely from the AI result cache, adding zero new provider calls.
    assert calls_after_first > 0
    assert client.calls == calls_after_first


def test_anticipate_questions_oversized_request_discarded_before_calling_the_provider(tmp_path) -> None:
    # ADF-W5.1/P7: AISchemaGateway bounds must reject an oversized request
    # payload before ever invoking the frontier provider, falling back to
    # the deterministic seed question/response.
    client = _FakeClient(
        '{"question": "What moved the ramp timeline again?", "suggested_response": "Name the blocker, owner, checkpoint, and consequence."}'
    )
    kwargs = _base_kwargs(client=client)
    oversized_title = "x" * 200_001
    kwargs["workstreams"] = (
        WorkstreamData(
            section_id="deployment_readiness",
            title=oversized_title,
            blurb="Ramp timeline remains conditional.",
            dependency_cascades=(),
            items=(
                WorkItem(
                    id=900001,
                    type="Feature",
                    title="UD chunking rollout",
                    state="Active",
                    assigned_to="Vertex Maintainer",
                    assigned_to_email="maintainer@example.com",
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

    anticipated = anticipate_questions(**kwargs, program_id="acme", programs_root=tmp_path)

    eta_drift_question = next(question for question in anticipated if question.confidence == Confidence.HIGH)
    assert oversized_title in eta_drift_question.suggested_response
    assert client.calls == 0