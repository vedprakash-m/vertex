from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from src.core.context_compiler import (
    ContentOrigin,
    ContextCompileRequest,
    DeterministicContextCompiler,
    EvidenceSpan,
    ValidationContext,
)
from src.core.models_v2 import LifecycleEventKind
from src.core.program_fact_store import FactPrecedence, ProgramFactInput, ProgramFactStore
from src.core.risk_register_engine import _write_lifecycle_event
from src.core.program_reality import ProgramReality


class FixedEstimator:
    tokenizer_id = "fixed"
    incremental_estimates = True

    def estimate(self, text: str) -> int:
        return max(1, len(text) // 4)


def test_compiler_adds_relevant_bitemporal_outcome_without_prompt_history_dump(
    tmp_path: Path,
) -> None:
    roots = tmp_path / "programs"
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    store = ProgramFactStore("acme", db_root=tmp_path)
    store.append_fact(
        ProgramFactInput(
            fact_type="risk.entry",
            scope="program",
            entity_refs=("RISK:historical-risk",),
            payload={
                "id": "historical-risk",
                "program_id": "acme",
                "title": "Three-year migration risk",
                "description": "The prior launch route failed.",
                "status": "closed",
                "outcome": "Parallel routing removed the launch risk.",
                "probability": "likely",
                "impact": "high",
                "category": "schedule",
                "owner_alias": "owner",
                "mitigation_plan": "Parallel routing.",
                "mitigation_due_date": "2023-01-01",
                "linked_workstream_ids": [],
                "linked_work_item_ids": [],
                "linked_milestone_ids": [],
                "linked_claim_ids": [],
                "linked_action_ids": [],
                "identified_date": "2022-12-01",
                "identified_in_vertex_issue": None,
                "last_reviewed_date": "2023-01-02",
                "entity_refs": [],
            },
            precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
            natural_key="historical-risk",
            created_by="test",
        ),
        recorded_at=now - timedelta(days=365 * 3),
    )

    request = ContextCompileRequest(
        program_id="acme",
        edition_id=None,
        feature="decision_brief_advisor",
        prompt_version="test",
        system_instructions="Use bounded evidence.",
        output_schema_text="{}",
        required_evidence=(
            EvidenceSpan(
                evidence_id="current-risk",
                source_family="risk",
                text="RISK:historical-risk is under review again.",
                required=True,
                origin=ContentOrigin.AUTHORED,
                trust_level="high",
                verification_state="verified",
                injection_screen="pass",
            ),
        ),
        optional_evidence=(),
        max_input_tokens=400,
        reserved_output_tokens=40,
        long_horizon_entity_refs=("RISK:historical-risk",),
        long_horizon_as_of=now,
    )
    compiled = DeterministicContextCompiler(
        token_estimator=FixedEstimator(),
        programs_root=roots,
    ).compile(
        request,
        validation_context=ValidationContext(
            program_id="acme",
            run_id="run-1",
            execution_mode="advisory",
            classification="internal",
        ),
    )

    assert "Parallel routing removed the launch risk." in compiled.prompt_text
    assert any(span.source_family == "long_horizon" for span in compiled.included)
    assert len(compiled.included) <= 2


def test_long_horizon_memory_is_hard_bounded_and_as_of_aware(tmp_path: Path) -> None:
    roots = tmp_path / "programs"
    as_of = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = ProgramFactStore("acme", db_root=tmp_path)
    for index in range(80):
        store.append_fact(
            ProgramFactInput(
                fact_type="decision.entry",
                scope="program",
                entity_refs=(f"DECISION:{index}",),
                payload={
                    "id": f"decision-{index}",
                    "title": f"Decision {index}",
                    "program_id": "acme",
                    "context": "Historical context.",
                    "status": "decided",
                    "decision": f"Outcome {index}",
                    "rationale": None,
                    "alternatives_considered": [],
                    "decided_by": "owner",
                    "decision_date": (as_of - timedelta(days=index + 1)).date().isoformat(),
                    "superseded_by": None,
                    "linked_claim_id": None,
                    "linked_risk_id": None,
                    "linked_action_ids": [],
                    "workstream_id": None,
                    "entity_refs": [],
                    "review_by": None,
                    "linked_milestone_ids": [],
                    "last_reviewed_date": None,
                },
                precedence=FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
                natural_key=f"decision-{index}",
                created_by="test",
            ),
            recorded_at=as_of - timedelta(days=index + 1),
        )
    outcomes = __import__("src.core.program_reality", fromlist=["ProgramReality"]).ProgramReality.load(
        "acme", programs_root=roots, as_of=as_of
    ).long_horizon_outcomes(max_items=4, as_of=as_of)

    assert len(outcomes) == 4
    assert all(outcome.recorded_at <= as_of for outcome in outcomes)


def test_long_horizon_memory_uses_domain_lifecycle_closures(tmp_path: Path) -> None:
    roots = tmp_path / "programs"
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _write_lifecycle_event(
        "acme",
        "action.lifecycle",
        "action-1",
        LifecycleEventKind.CLOSURE,
        actor="owner",
        note="Validation completed.",
        effective=None,
        next_review_at=None,
        evidence_refs=(),
        occurred_at=now,
        programs_root=roots,
    )

    outcomes = __import__("src.core.program_reality", fromlist=["ProgramReality"]).ProgramReality.load(
        "acme", programs_root=roots, as_of=now
    ).long_horizon_outcomes(("ACTION:action-1",), as_of=now)

    assert [(outcome.family, outcome.object_id, outcome.summary) for outcome in outcomes] == [
        ("action", "action-1", "Validation completed.")
    ]


def test_long_horizon_request_fails_when_explicit_history_is_unavailable(
    tmp_path: Path,
) -> None:
    roots = tmp_path / "programs"
    request = ContextCompileRequest(
        program_id="acme",
        edition_id=None,
        feature="decision_brief_advisor",
        prompt_version="test",
        system_instructions="Use bounded evidence.",
        output_schema_text="{}",
        required_evidence=(),
        optional_evidence=(),
        max_input_tokens=400,
        reserved_output_tokens=40,
        long_horizon_entity_refs=("RISK:historical-risk",),
        long_horizon_as_of=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )

    with __import__("pytest").raises(Exception, match="Long-horizon relevance evaluation unavailable"):
        DeterministicContextCompiler(
            token_estimator=FixedEstimator(),
            programs_root=roots,
        ).compile(
            request,
            validation_context=ValidationContext(
                program_id="acme",
                run_id="run-1",
                execution_mode="advisory",
                classification="internal",
            ),
        )


def test_long_horizon_requested_entity_survives_unrelated_history(tmp_path: Path) -> None:
    as_of = datetime(2026, 6, 1, tzinfo=timezone.utc)
    store = ProgramFactStore("acme", db_root=tmp_path)
    store.append_fact(
        ProgramFactInput(
            fact_type="action.item",
            scope="program",
            entity_refs=("ACTION:target",),
            payload={
                "id": "target",
                "program_id": "acme",
                "text": "Requested closure",
                "owner_alias": "owner",
                "status": "done",
                "source_type": "meeting_transcript",
                "created_at": (as_of - timedelta(days=100)).isoformat(),
                "resolved_at": (as_of - timedelta(days=99)).isoformat(),
                "resolution_note": "Requested closure",
            },
            precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
            natural_key="target",
            created_by="test",
        ),
        recorded_at=as_of - timedelta(days=100),
    )
    for index in range(70):
        store.append_fact(
            ProgramFactInput(
                fact_type="decision.entry",
                scope="program",
                entity_refs=(f"DECISION:unrelated-{index}",),
                payload={
                    "id": f"unrelated-{index}",
                    "program_id": "acme",
                    "title": "Other",
                    "context": "Historical.",
                    "status": "decided",
                    "decision": "Other",
                    "rationale": None,
                    "alternatives_considered": [],
                    "decided_by": "owner",
                    "decision_date": (as_of - timedelta(days=index + 1)).date().isoformat(),
                    "superseded_by": None,
                    "linked_claim_id": None,
                    "linked_risk_id": None,
                    "linked_action_ids": [],
                    "workstream_id": None,
                    "entity_refs": [],
                    "review_by": None,
                    "linked_milestone_ids": [],
                    "last_reviewed_date": None,
                },
                precedence=FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
                natural_key=f"unrelated-{index}",
                created_by="test",
            ),
            recorded_at=as_of - timedelta(days=index + 1),
        )

    outcomes = ProgramReality.load(
        "acme", programs_root=tmp_path / "programs", as_of=as_of
    ).long_horizon_outcomes(("ACTION:target",), as_of=as_of)

    assert [(item.family, item.object_id) for item in outcomes] == [
        ("action", "target")
    ]


def test_long_horizon_deduplicates_status_and_lifecycle_as_one_outcome(
    tmp_path: Path,
) -> None:
    roots = tmp_path / "programs"
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    store = ProgramFactStore("acme", db_root=tmp_path)
    store.append_fact(
        ProgramFactInput(
            fact_type="action.item",
            scope="program",
            entity_refs=("ACTION:action-1",),
            payload={
                "id": "action-1",
                "program_id": "acme",
                "text": "Fallback",
                "owner_alias": "owner",
                "status": "done",
                "source_type": "meeting_transcript",
                "created_at": now.isoformat(),
                "resolved_at": now.isoformat(),
                "resolution_note": "Fallback",
            },
            precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
            natural_key="action-1",
            created_by="test",
        ),
        recorded_at=now,
    )
    _write_lifecycle_event(
        "acme",
        "action.lifecycle",
        "action-1",
        LifecycleEventKind.CLOSURE,
        actor="owner",
        note="Validated closure.",
        effective=None,
        next_review_at=None,
        evidence_refs=(),
        occurred_at=now,
        programs_root=roots,
    )

    outcomes = ProgramReality.load(
        "acme", programs_root=roots, as_of=now
    ).long_horizon_outcomes(("ACTION:action-1",), as_of=now)

    assert [(item.family, item.object_id, item.summary) for item in outcomes] == [
        ("action", "action-1", "Validated closure.")
    ]


def test_long_horizon_revision_lookup_rejects_invalid_or_missing_history(
    tmp_path: Path,
) -> None:
    store = ProgramFactStore("missing", db_root=tmp_path)
    as_of = datetime(2026, 6, 1, tzinfo=timezone.utc)

    assert store.list_accepted_revisions_for_types((), as_of=as_of) == ()
    assert store.list_accepted_revisions_for_types(
        ("risk.entry",), as_of=as_of
    ) == ()
    with pytest.raises(ValueError, match="unavailable"):
        store.list_accepted_revisions_for_types(
            ("risk.entry",), as_of=as_of, require_history=True
        )

    with sqlite3.connect(tmp_path / "empty.sqlite3"):
        pass
    empty = ProgramFactStore("empty", db_root=tmp_path)
    assert empty.list_accepted_revisions_for_types(
        ("risk.entry",), as_of=as_of
    ) == ()
    with pytest.raises(ValueError, match="unavailable"):
        empty.list_accepted_revisions_for_types(
            ("risk.entry",), as_of=as_of, require_history=True
        )
