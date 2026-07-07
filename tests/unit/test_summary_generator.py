from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.ai.client import AIClientError
from src.ai.llm_trace import AITraceContext
from src.ai.summary_generator import PROMPT_VERSION, SummaryGenerator, SummaryGeneratorError
from src.core.models import Confidence
from src.core.models_v2 import AIConfig, Program, Signal, Workstream
from src.core.trajectory_analyzer import DriftPattern


class _FakeAIClient:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.last_system: str | None = None
        self.last_user: str | None = None
        self.last_prompt_version: str | None = None

    def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
        del max_tokens
        self.last_system = system
        self.last_user = user
        self.last_prompt_version = prompt_version
        return parser({"text": self.response_text})


class _MalformedPayloadAIClient:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
        del system, user, max_tokens, prompt_version
        return parser(self.payload)


def test_summary_generator_builds_markdown_summary_from_context() -> None:
    client = _FakeAIClient(
        "## Current State\nDeployment velocity improved for WI:1234.\n\n## New Since Last Summary\nApproved ADO and WorkIQ signals confirm the blocker is reduced.\n\n## Risks And Watchouts\nETA drift remains the main watch item."
    )
    generator = SummaryGenerator(client=client)
    program = Program(schema_version="2.0", id="acme", name="Adventure + DD on PF", objective="Keep weekly readiness accurate.")
    workstream = Workstream(id="acme", name="Deployment Readiness", description="Deployment readiness and ramp blockers.")
    signals = (
        Signal(
            id="sig-1",
            timestamp=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
            source="ado/revision",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI:1234",),
            text="ADO#1234 target date changed from 2026-05-12 to 2026-05-17.",
            raw_ref="wi:1234",
            confidence=Confidence.HIGH,
        ),
    )
    drift_patterns = (
        DriftPattern(
            work_item_id=1234,
            pattern="eta_drift",
            severity="medium",
            detail="Target date slipped 2 times in the last 90 days.",
            occurrences=2,
            window_days=90,
        ),
    )

    result = generator.generate(
        program=program,
        workstream=workstream,
        prior_summary="## Current State\nPrior summary text.",
        signals=signals,
        drift_patterns=drift_patterns,
    )

    assert result is not None
    assert result.prompt_version == PROMPT_VERSION
    assert client.last_prompt_version == PROMPT_VERSION
    assert client.last_user is not None and "Prior summary text." in client.last_user
    assert client.last_user is not None and "ADO#1234 target date changed" in client.last_user
    assert client.last_user is not None and "eta_drift" in client.last_user


def test_summary_generator_rejects_overlong_output() -> None:
    client = _FakeAIClient("word " * 501)
    generator = SummaryGenerator(client=client)

    with pytest.raises(SummaryGeneratorError, match="exceeded 500 words"):
        generator.generate(
            program=Program(schema_version="2.0", id="acme", name="Adventure + DD on PF"),
            workstream=Workstream(id="acme", name="Deployment Readiness"),
            prior_summary=None,
            signals=(
                Signal(
                    id="sig-1",
                    timestamp=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
                    source="ado/revision",
                    program_id="acme",
                    workstream_id="acme",
                    entity_refs=("WI:1234",),
                    text="ADO#1234 target date changed from 2026-05-12 to 2026-05-17.",
                    raw_ref="wi:1234",
                    confidence=Confidence.HIGH,
                ),
            ),
            drift_patterns=(),
        )


def test_summary_generator_runs_safety_pipeline() -> None:
    client = _FakeAIClient("Deployment moved due to vendor follow-up. Contact foo@gmail.com.")
    generator = SummaryGenerator(client=client)

    result = generator.generate(
        program=Program(schema_version="2.0", id="acme", name="Adventure + DD on PF"),
        workstream=Workstream(id="acme", name="Deployment Readiness"),
        prior_summary=None,
        signals=(
            Signal(
                id="sig-1",
                timestamp=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
                source="ado/revision",
                program_id="acme",
                workstream_id="acme",
                entity_refs=("WI:1234",),
                text="ADO#1234 target date changed from 2026-05-12 to 2026-05-17.",
                raw_ref="wi:1234",
                confidence=Confidence.HIGH,
            ),
        ),
        drift_patterns=(),
    )

    assert result is not None
    assert result.text == "Deployment moved after vendor follow-up. Contact [PII-FILTERED-EMAIL]."


def test_summary_generator_rejects_unknown_work_item_reference() -> None:
    client = _FakeAIClient("Deployment velocity improved for WI:9999.")
    generator = SummaryGenerator(client=client)

    with pytest.raises(SummaryGeneratorError, match="outside approved signals: 9999"):
        generator.generate(
            program=Program(schema_version="2.0", id="acme", name="Adventure + DD on PF"),
            workstream=Workstream(id="acme", name="Deployment Readiness"),
            prior_summary=None,
            signals=(
                Signal(
                    id="sig-1",
                    timestamp=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
                    source="ado/revision",
                    program_id="acme",
                    workstream_id="acme",
                    entity_refs=("WI:1234",),
                    text="ADO#1234 target date changed from 2026-05-12 to 2026-05-17.",
                    raw_ref="wi:1234",
                    confidence=Confidence.HIGH,
                ),
            ),
            drift_patterns=(),
        )


def test_summary_generator_allows_work_item_reference_from_drift_pattern() -> None:
    client = _FakeAIClient("ETA drift remains the main watch item for WI:5678.")
    generator = SummaryGenerator(client=client)

    result = generator.generate(
        program=Program(schema_version="2.0", id="acme", name="Adventure + DD on PF"),
        workstream=Workstream(id="acme", name="Deployment Readiness"),
        prior_summary=None,
        signals=(
            Signal(
                id="sig-1",
                timestamp=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
                source="ado/revision",
                program_id="acme",
                workstream_id="acme",
                entity_refs=("WI:1234",),
                text="ADO#1234 target date changed from 2026-05-12 to 2026-05-17.",
                raw_ref="wi:1234",
                confidence=Confidence.HIGH,
            ),
        ),
        drift_patterns=(
            DriftPattern(
                work_item_id=5678,
                pattern="eta_drift",
                severity="medium",
                detail="Target date slipped twice in the last 90 days.",
                occurrences=2,
                window_days=90,
            ),
        ),
    )

    assert result is not None
    assert result.text == "ETA drift remains the main watch item for WI:5678."


def test_summary_generator_rejects_non_object_payload() -> None:
    generator = SummaryGenerator(client=_MalformedPayloadAIClient([]))

    with pytest.raises(SummaryGeneratorError, match="Rolling summary payload must be an object"):
        generator.generate(
            program=Program(schema_version="2.0", id="acme", name="Adventure + DD on PF"),
            workstream=Workstream(id="acme", name="Deployment Readiness"),
            prior_summary=None,
            signals=(
                Signal(
                    id="sig-1",
                    timestamp=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
                    source="ado/revision",
                    program_id="acme",
                    workstream_id="acme",
                    entity_refs=("WI:1234",),
                    text="ADO#1234 target date changed from 2026-05-12 to 2026-05-17.",
                    raw_ref="wi:1234",
                    confidence=Confidence.HIGH,
                ),
            ),
            drift_patterns=(),
        )


def test_summary_generator_rejects_non_string_text_payload() -> None:
    generator = SummaryGenerator(client=_MalformedPayloadAIClient({"text": ["bad-text"]}))

    with pytest.raises(SummaryGeneratorError, match="Rolling summary payload must include text as a string"):
        generator.generate(
            program=Program(schema_version="2.0", id="acme", name="Adventure + DD on PF"),
            workstream=Workstream(id="acme", name="Deployment Readiness"),
            prior_summary=None,
            signals=(
                Signal(
                    id="sig-1",
                    timestamp=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
                    source="ado/revision",
                    program_id="acme",
                    workstream_id="acme",
                    entity_refs=("WI:1234",),
                    text="ADO#1234 target date changed from 2026-05-12 to 2026-05-17.",
                    raw_ref="wi:1234",
                    confidence=Confidence.HIGH,
                ),
            ),
            drift_patterns=(),
        )


def test_summary_generator_rejects_blank_text_payload() -> None:
    generator = SummaryGenerator(client=_MalformedPayloadAIClient({"text": "   ```\n\n```   "}))

    with pytest.raises(SummaryGeneratorError, match="Rolling summary payload text must be non-empty"):
        generator.generate(
            program=Program(schema_version="2.0", id="acme", name="Adventure + DD on PF"),
            workstream=Workstream(id="acme", name="Deployment Readiness"),
            prior_summary=None,
            signals=(
                Signal(
                    id="sig-1",
                    timestamp=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
                    source="ado/revision",
                    program_id="acme",
                    workstream_id="acme",
                    entity_refs=("WI:1234",),
                    text="ADO#1234 target date changed from 2026-05-12 to 2026-05-17.",
                    raw_ref="wi:1234",
                    confidence=Confidence.HIGH,
                ),
            ),
            drift_patterns=(),
        )


def test_summary_generator_from_program_falls_back_to_backup_deployment_when_primary_fails(monkeypatch) -> None:
    attempts: list[str] = []

    class _RuntimeAIClient:
        def __init__(self, *, deployment: str, temperature: float, budget_usd: float) -> None:
            del temperature, budget_usd
            self.deployment = deployment

        def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
            del system, user, max_tokens, prompt_version
            attempts.append(self.deployment)
            if self.deployment == "summary-primary":
                raise AIClientError("primary deployment failed")
            return parser({"text": "## Current State\nFallback summary text."})

    monkeypatch.setattr("src.ai.deployment_fallback.AIClient", _RuntimeAIClient)

    generator = SummaryGenerator.from_program(
        Program(
            schema_version="2.0",
            id="acme",
            name="Adventure + DD on PF",
            ai=AIConfig(
                enabled=True,
                budget_usd_per_run=0.5,
                blurb_deployment="summary-primary",
                blurb_backup_deployment="summary-backup",
                temperature=0.2,
            ),
        )
    )

    result = generator.generate(
        program=Program(schema_version="2.0", id="acme", name="Adventure + DD on PF"),
        workstream=Workstream(id="acme", name="Deployment Readiness"),
        prior_summary=None,
        signals=(
            Signal(
                id="sig-1",
                timestamp=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
                source="ado/revision",
                program_id="acme",
                workstream_id="acme",
                entity_refs=("WI:1234",),
                text="ADO#1234 target date changed from 2026-05-12 to 2026-05-17.",
                raw_ref="wi:1234",
                confidence=Confidence.HIGH,
            ),
        ),
        drift_patterns=(),
    )

    assert result is not None
    assert result.text == "## Current State\nFallback summary text."
    assert attempts == ["summary-primary", "summary-backup"]


def test_summary_generator_from_program_surfaces_vertex_ai_alias_in_missing_env_error(monkeypatch) -> None:
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    monkeypatch.delenv("VERTEX_AI_DEPLOYMENT", raising=False)
    monkeypatch.delenv("VERTEX_AI_DEPLOYMENT", raising=False)

    with pytest.raises(SummaryGeneratorError, match="VERTEX_AI_DEPLOYMENT or AZURE_OPENAI_DEPLOYMENT not set"):
        SummaryGenerator.from_program(
            Program(
                schema_version="2.0",
                id="acme",
                name="Adventure + DD on PF",
                ai=AIConfig(enabled=True, budget_usd_per_run=0.5),
            )
        )


def test_summary_generator_from_program_passes_trace_context_to_runtime_clients(monkeypatch) -> None:
    seen_trace_contexts: list[object] = []

    class _RuntimeAIClient:
        def __init__(self, *, deployment: str, temperature: float, budget_usd: float, trace_context=None) -> None:
            del deployment, temperature, budget_usd
            seen_trace_contexts.append(trace_context)

        def structured(self, system: str, user: str, *, parser, max_tokens: int = 800, prompt_version: str | None = None):
            del system, user, max_tokens, prompt_version
            return parser({"text": "## Current State\nTrace-aware summary text."})

    monkeypatch.setattr("src.ai.deployment_fallback.AIClient", _RuntimeAIClient)

    trace_context = AITraceContext(
        edition="acme",
        run_id="acme:summarize:all:20260510T120000Z",
        caller="src.commands.summarize.summarize_program",
        metadata={"run_budget_usd": 0.5},
    )
    generator = SummaryGenerator.from_program(
        Program(
            schema_version="2.0",
            id="acme",
            name="Adventure + DD on PF",
            ai=AIConfig(
                enabled=True,
                budget_usd_per_run=0.5,
                blurb_deployment="summary-primary",
                temperature=0.2,
            ),
        ),
        trace_context=trace_context,
    )

    result = generator.generate(
        program=Program(schema_version="2.0", id="acme", name="Adventure + DD on PF"),
        workstream=Workstream(id="acme", name="Deployment Readiness"),
        prior_summary=None,
        signals=(
            Signal(
                id="sig-1",
                timestamp=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
                source="ado/revision",
                program_id="acme",
                workstream_id="acme",
                entity_refs=("WI:1234",),
                text="ADO#1234 target date changed from 2026-05-12 to 2026-05-17.",
                raw_ref="wi:1234",
                confidence=Confidence.HIGH,
            ),
        ),
        drift_patterns=(),
    )

    assert result is not None
    assert seen_trace_contexts == [trace_context]