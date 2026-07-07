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

    def structured(self, system: str, user: str, *, parser, max_tokens: int = 0, prompt_version: str | None = None):
        del system, user, max_tokens, prompt_version
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