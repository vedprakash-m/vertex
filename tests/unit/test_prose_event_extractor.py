"""Unit tests for OSD-7 Wave 1 + Wave 2: prose_event_extractor."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

import pytest

from src.ai.discovery.prose_event_extractor import (
    PROMPT_VERSION,
    PROMPT_VERSION_V2,
    ExtractedProseCandidateBatch,
    ProseEventExtractorError,
    extract_prose_event_candidates,
)
from src.core.ledger.candidate_store import CandidateEvent
from src.core.ledger.event_log import ConfidenceTier
from src.core.ledger.source_refs import NewsletterRef
from src.core.ledger.ulid import new_ulid


_DEFAULT_OCCURRED_AT = datetime(2025, 6, 1, tzinfo=timezone.utc)
_PROGRAM_ID = "acme"
_BATCH_ID = "batch-prose-001"
_SOURCE_REF = NewsletterRef(
    file_path="newsletters/2025/issue-042.eml",
    publication_date=_DEFAULT_OCCURRED_AT.date(),
    issue_number=42,
)


StructuredResponse = TypeVar("StructuredResponse")


class _FakeAIClient:
    def __init__(self, events_payload: list[dict[str, Any]]) -> None:
        self._events_payload = events_payload
        self.calls = 0
        self.last_prompt_version: str | None = None

    def chat(self, system: str, user: str, *, max_tokens: int = 800, prompt_version: str | None = None) -> str:
        raise AssertionError("chat() should not be called by prose extractor")

    def structured(
        self,
        system: str,
        user: str,
        *,
        parser: Callable[[dict[str, Any]], StructuredResponse],
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ) -> StructuredResponse:
        self.calls += 1
        self.last_prompt_version = prompt_version
        return parser({"events": self._events_payload})


def _extract(
    *,
    prose_text: str = "Acme Update: team decided to proceed with Gen8 BIOS rollout.",
    client: _FakeAIClient | None = None,
    events_payload: list[dict[str, Any]] | None = None,
) -> ExtractedProseCandidateBatch:
    effective_client = client or _FakeAIClient(events_payload or [])
    return extract_prose_event_candidates(
        prose_text=prose_text,
        program_id=_PROGRAM_ID,
        source_ref=_SOURCE_REF,
        batch_id=_BATCH_ID,
        default_occurred_at=_DEFAULT_OCCURRED_AT,
        client=effective_client,
    )


# ── happy-path tests ──────────────────────────────────────────────────────────

def test_extract_decision_event() -> None:
    batch = _extract(
        events_payload=[
            {
                "event_type": "decision.made.v1",
                "payload": {
                    "decision_id": "dec-gen8-bios-rollout",
                    "title": "Proceed with Gen8 BIOS AP rollout",
                    "decision_text": "Team decided to proceed with Gen8 BIOS AP deployment by Q3 2025.",
                    "decided_by": ["program-lt"],
                    "forum": "Acme LT Review",
                },
                "occurred_at": "2025-06-01",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.90,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "decision.made.v1"
    assert candidate.proposed_payload["decision_id"] == "dec-gen8-bios-rollout"
    assert candidate.proposed_payload["decided_by"] == ["program-lt"]
    assert candidate.proposed_confidence == ConfidenceTier.AI_EXTRACTED.value
    assert candidate.proposed_temporal_confidence == "approximate"
    assert candidate.extraction_confidence == 0.85  # capped
    assert candidate.program_id == _PROGRAM_ID
    assert candidate.batch_id == _BATCH_ID
    assert candidate.pipeline == "prose_extract"


def test_extract_risk_event() -> None:
    batch = _extract(
        events_payload=[
            {
                "event_type": "risk.raised.v1",
                "payload": {
                    "risk_id": "risk-wingtip-supply",
                    "title": "Wingtip supply chain delay",
                    "severity": "high",
                    "description": "Component lead times extended by 6 weeks.",
                },
                "occurred_at": None,
                "temporal_confidence": "estimated",
                "extraction_confidence": 0.80,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "risk.raised.v1"
    assert candidate.proposed_payload["severity"] == "high"
    assert candidate.proposed_temporal_confidence == "estimated"
    assert candidate.proposed_occurred_at == _DEFAULT_OCCURRED_AT


def test_extract_milestone_created_event() -> None:
    batch = _extract(
        events_payload=[
            {
                "event_type": "milestone.created.v1",
                "payload": {
                    "milestone_id": "ms-gen8-bios-q3",
                    "name": "Gen8 BIOS AP Q3 Target",
                    "target_date": "2025-09-30",
                },
                "occurred_at": "2025-06-01",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.85,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "milestone.created.v1"
    assert candidate.proposed_payload["target_date"] == "2025-09-30"


def test_extract_milestone_completed_event() -> None:
    batch = _extract(
        events_payload=[
            {
                "event_type": "milestone.completed.v1",
                "payload": {
                    "milestone_id": "ms-gen7-bios-ga",
                    "completed_on": "2025-05-15",
                    "evidence": "Rollout reached 100% on all Gen7 platforms.",
                },
                "occurred_at": "2025-05-15",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.92,
            }
        ]
    )
    assert len(batch.candidates) == 1
    assert batch.candidates[0].proposed_payload["completed_on"] == "2025-05-15"


def test_extract_metric_observed_event() -> None:
    batch = _extract(
        events_payload=[
            {
                "event_type": "metric.observed.v1",
                "payload": {
                    "kpi_id": "bios-ap-rollout-gen8",
                    "value": 67.3,
                    "unit": "percent",
                    "window_end": "2025-06-01",
                },
                "occurred_at": "2025-06-01",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.95,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "metric.observed.v1"
    assert candidate.proposed_payload["value"] == 67.3
    assert candidate.extraction_confidence == 0.85  # capped at 0.85


def test_extract_multiple_events() -> None:
    batch = _extract(
        events_payload=[
            {
                "event_type": "decision.made.v1",
                "payload": {
                    "decision_id": "dec-a",
                    "title": "Decision A",
                    "decision_text": "We decided A.",
                    "decided_by": ["team-a"],
                },
                "occurred_at": "2025-06-01",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.80,
            },
            {
                "event_type": "risk.raised.v1",
                "payload": {
                    "risk_id": "risk-b",
                    "title": "Risk B",
                    "severity": "medium",
                },
                "occurred_at": None,
                "temporal_confidence": "estimated",
                "extraction_confidence": 0.70,
            },
        ]
    )
    assert len(batch.candidates) == 2
    assert batch.candidates[0].proposed_event_type == "decision.made.v1"
    assert batch.candidates[1].proposed_event_type == "risk.raised.v1"


def test_prompt_version_is_passed_to_client() -> None:
    client = _FakeAIClient([])
    _extract(client=client)
    assert client.last_prompt_version == "prose_event_extractor.v1"


# ── guard tests ───────────────────────────────────────────────────────────────

def test_empty_prose_text_returns_empty_batch() -> None:
    batch = _extract(prose_text="   ", events_payload=[])
    assert batch.candidates == ()
    assert batch.warnings == ()


def test_no_client_returns_empty_batch_with_warning() -> None:
    batch = extract_prose_event_candidates(
        prose_text="Some program update text here.",
        program_id=_PROGRAM_ID,
        source_ref=_SOURCE_REF,
        batch_id=_BATCH_ID,
        default_occurred_at=_DEFAULT_OCCURRED_AT,
        client=None,
    )
    assert batch.candidates == ()
    assert len(batch.warnings) == 1
    assert "skipped" in batch.warnings[0].lower()


def test_unsupported_event_type_is_skipped_with_warning() -> None:
    batch = _extract(
        events_payload=[
            {
                "event_type": "workstream.created.v1",  # not in Wave 1 types
                "payload": {"workstream_id": "ws-1", "name": "WS 1"},
                "occurred_at": None,
                "temporal_confidence": "estimated",
                "extraction_confidence": 0.70,
            }
        ]
    )
    assert batch.candidates == ()
    assert any("Unsupported" in w for w in batch.warnings)


def test_missing_required_payload_field_is_skipped_with_warning() -> None:
    batch = _extract(
        events_payload=[
            {
                "event_type": "decision.made.v1",
                "payload": {
                    "decision_id": "dec-x",
                    # missing: title, decision_text, decided_by
                },
                "occurred_at": None,
                "temporal_confidence": "estimated",
                "extraction_confidence": 0.60,
            }
        ]
    )
    assert batch.candidates == ()
    assert len(batch.warnings) > 0


def test_missing_payload_dict_is_skipped() -> None:
    batch = _extract(
        events_payload=[
            {
                "event_type": "decision.made.v1",
                "payload": None,  # invalid
                "occurred_at": None,
                "temporal_confidence": "estimated",
                "extraction_confidence": 0.70,
            }
        ]
    )
    assert batch.candidates == ()
    assert any("payload" in w.lower() for w in batch.warnings)


def test_invalid_temporal_confidence_defaults_to_estimated() -> None:
    batch = _extract(
        events_payload=[
            {
                "event_type": "risk.raised.v1",
                "payload": {
                    "risk_id": "risk-test",
                    "title": "Test risk",
                    "severity": "low",
                },
                "occurred_at": "2025-06-01",
                "temporal_confidence": "nonsense_value",
                "extraction_confidence": 0.75,
            }
        ]
    )
    assert len(batch.candidates) == 1
    assert batch.candidates[0].proposed_temporal_confidence == "estimated"


def test_invalid_occurred_at_falls_back_to_default() -> None:
    batch = _extract(
        events_payload=[
            {
                "event_type": "risk.raised.v1",
                "payload": {
                    "risk_id": "risk-test",
                    "title": "Test risk",
                    "severity": "low",
                },
                "occurred_at": "not-a-date",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.75,
            }
        ]
    )
    assert len(batch.candidates) == 1
    assert batch.candidates[0].proposed_occurred_at == _DEFAULT_OCCURRED_AT
    assert batch.candidates[0].proposed_temporal_confidence == "estimated"


def test_extraction_confidence_capped_at_085() -> None:
    batch = _extract(
        events_payload=[
            {
                "event_type": "risk.raised.v1",
                "payload": {
                    "risk_id": "risk-capped",
                    "title": "Confidence cap test",
                    "severity": "high",
                },
                "occurred_at": None,
                "temporal_confidence": "estimated",
                "extraction_confidence": 0.99,
            }
        ]
    )
    assert batch.candidates[0].extraction_confidence == 0.85


def test_extraction_confidence_low_value_preserved() -> None:
    batch = _extract(
        events_payload=[
            {
                "event_type": "risk.raised.v1",
                "payload": {
                    "risk_id": "risk-low-conf",
                    "title": "Low confidence risk",
                    "severity": "low",
                },
                "occurred_at": None,
                "temporal_confidence": "reconstructed",
                "extraction_confidence": 0.40,
            }
        ]
    )
    assert batch.candidates[0].extraction_confidence == pytest.approx(0.40)


def test_dedupe_key_is_deterministic() -> None:
    payload = [
        {
            "event_type": "decision.made.v1",
            "payload": {
                "decision_id": "dec-stable",
                "title": "Stable decision",
                "decision_text": "We will do X.",
                "decided_by": ["team-x"],
            },
            "occurred_at": "2025-06-01",
            "temporal_confidence": "approximate",
            "extraction_confidence": 0.80,
        }
    ]
    batch_a = _extract(events_payload=payload)
    batch_b = _extract(events_payload=payload)
    assert batch_a.candidates[0].dedupe_key == batch_b.candidates[0].dedupe_key


def test_batch_id_flows_to_candidates() -> None:
    batch = _extract(
        events_payload=[
            {
                "event_type": "risk.raised.v1",
                "payload": {"risk_id": "risk-z", "title": "Z risk", "severity": "low"},
                "occurred_at": None,
                "temporal_confidence": "estimated",
                "extraction_confidence": 0.60,
            }
        ]
    )
    assert batch.candidates[0].batch_id == _BATCH_ID
    assert batch.batch_id == _BATCH_ID


def test_llm_not_called_when_prose_text_empty() -> None:
    client = _FakeAIClient([])
    _extract(prose_text="", client=client)
    assert client.calls == 0


def test_valid_occurred_at_is_parsed() -> None:
    batch = _extract(
        events_payload=[
            {
                "event_type": "milestone.completed.v1",
                "payload": {
                    "milestone_id": "ms-xyz",
                    "completed_on": "2025-04-10",
                },
                "occurred_at": "2025-04-10",
                "temporal_confidence": "exact",
                "extraction_confidence": 0.92,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_occurred_at == datetime(2025, 4, 10, tzinfo=timezone.utc)
    assert candidate.proposed_temporal_confidence == "exact"


def test_partial_batch_valid_events_pass_invalid_skipped() -> None:
    batch = _extract(
        events_payload=[
            {
                "event_type": "decision.made.v1",
                "payload": {
                    "decision_id": "dec-ok",
                    "title": "Good decision",
                    "decision_text": "Proceed with X.",
                    "decided_by": ["team-y"],
                },
                "occurred_at": "2025-06-01",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.80,
            },
            {
                "event_type": "bad_type.unknown.v1",  # invalid
                "payload": {},
                "occurred_at": None,
                "temporal_confidence": "estimated",
                "extraction_confidence": 0.50,
            },
        ]
    )
    assert len(batch.candidates) == 1  # only the valid one
    assert len(batch.warnings) == 1   # one warning for the skipped one
    assert batch.candidates[0].proposed_payload["decision_id"] == "dec-ok"


# ── Wave 2 tests ──────────────────────────────────────────────────────────────

def _extract_wave2(
    *,
    prose_text: str = "Acme entered Phase 2 ramp-readiness stage in Q2 2025.",
    events_payload: list[dict[str, Any]] | None = None,
) -> ExtractedProseCandidateBatch:
    client = _FakeAIClient(events_payload or [])
    return extract_prose_event_candidates(
        prose_text=prose_text,
        program_id=_PROGRAM_ID,
        source_ref=_SOURCE_REF,
        batch_id=_BATCH_ID,
        default_occurred_at=_DEFAULT_OCCURRED_AT,
        wave=2,
        client=client,
    )


def test_wave2_phase_entered_event() -> None:
    batch = _extract_wave2(
        events_payload=[
            {
                "event_type": "program.phase_entered.v1",
                "payload": {
                    "phase_id": "phase-ramp-readiness",
                    "phase_name": "Ramp Readiness",
                    "entry_criteria_met": ["Gen8 validation complete", "Kusto baseline set"],
                },
                "occurred_at": "2025-04-01",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.82,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "program.phase_entered.v1"
    assert candidate.proposed_payload["phase_id"] == "phase-ramp-readiness"
    assert candidate.proposed_payload["phase_name"] == "Ramp Readiness"
    assert candidate.proposed_confidence == "ai_extracted"


def test_wave2_scope_changed_event() -> None:
    batch = _extract_wave2(
        events_payload=[
            {
                "event_type": "program.scope_changed.v1",
                "payload": {
                    "change_kind": "expansion",
                    "description": "Added Wingtip fleet to Acme scope for Gen9 readiness.",
                    "affected_entities": ["ws-wingtip-fleet-safety"],
                },
                "occurred_at": "2025-03-15",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.78,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "program.scope_changed.v1"
    assert candidate.proposed_payload["change_kind"] == "expansion"


def test_wave2_workstream_status_changed_event() -> None:
    batch = _extract_wave2(
        events_payload=[
            {
                "event_type": "workstream.status_changed.v1",
                "payload": {
                    "workstream_id": "ws-bios-ap-rollout",
                    "new_status": "at-risk",
                    "prior_status": "on-track",
                    "reason": "Gen8 BIOS supply chain delay extended timeline by 6 weeks.",
                },
                "occurred_at": "2025-05-01",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.80,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "workstream.status_changed.v1"
    assert candidate.proposed_payload["new_status"] == "at-risk"


def test_wave2_workstream_created_event() -> None:
    batch = _extract_wave2(
        events_payload=[
            {
                "event_type": "workstream.created.v1",
                "payload": {
                    "workstream_id": "ws-gen9-platform-safety",
                    "name": "Gen9 Platform Safety",
                    "owner_person_id": "acme-safety-team",
                },
                "occurred_at": None,
                "temporal_confidence": "estimated",
                "extraction_confidence": 0.75,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "workstream.created.v1"
    assert candidate.proposed_payload["workstream_id"] == "ws-gen9-platform-safety"


def test_wave2_uses_v2_prompt_version() -> None:
    client = _FakeAIClient([])
    extract_prose_event_candidates(
        prose_text="Acme entered ramp phase.",
        program_id=_PROGRAM_ID,
        source_ref=_SOURCE_REF,
        batch_id=_BATCH_ID,
        default_occurred_at=_DEFAULT_OCCURRED_AT,
        wave=2,
        client=client,
    )
    assert client.last_prompt_version == PROMPT_VERSION_V2
    assert PROMPT_VERSION_V2 == "prose_event_extractor.v2"


def test_wave1_uses_v1_prompt_version() -> None:
    client = _FakeAIClient([])
    extract_prose_event_candidates(
        prose_text="Team decided to deploy.",
        program_id=_PROGRAM_ID,
        source_ref=_SOURCE_REF,
        batch_id=_BATCH_ID,
        default_occurred_at=_DEFAULT_OCCURRED_AT,
        wave=1,
        client=client,
    )
    assert client.last_prompt_version == PROMPT_VERSION
    assert PROMPT_VERSION == "prose_event_extractor.v1"


def test_wave2_rejects_wave1_event_types() -> None:
    batch = _extract_wave2(
        events_payload=[
            {
                "event_type": "decision.made.v1",  # Wave 1 type, not valid in wave=2
                "payload": {
                    "decision_id": "dec-x",
                    "title": "Some decision",
                    "decision_text": "We decided X.",
                    "decided_by": ["team-x"],
                },
                "occurred_at": "2025-06-01",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.80,
            }
        ]
    )
    assert batch.candidates == ()
    assert any("Unsupported" in w for w in batch.warnings)


def test_wave1_rejects_wave2_event_types() -> None:
    batch = _extract(
        events_payload=[
            {
                "event_type": "program.phase_entered.v1",  # Wave 2 type, not valid in wave=1
                "payload": {"phase_id": "phase-x", "phase_name": "Phase X"},
                "occurred_at": None,
                "temporal_confidence": "estimated",
                "extraction_confidence": 0.70,
            }
        ]
    )
    assert batch.candidates == ()
    assert any("Unsupported" in w for w in batch.warnings)


def test_unsupported_wave_number_raises() -> None:
    with pytest.raises(ProseEventExtractorError, match="Unsupported extraction wave"):
        extract_prose_event_candidates(
            prose_text="Some text.",
            program_id=_PROGRAM_ID,
            source_ref=_SOURCE_REF,
            batch_id=_BATCH_ID,
            default_occurred_at=_DEFAULT_OCCURRED_AT,
            wave=99,
            client=_FakeAIClient([]),
        )


def test_wave2_charter_revised_with_unknown_prior() -> None:
    batch = _extract_wave2(
        events_payload=[
            {
                "event_type": "program.charter_revised.v1",
                "payload": {
                    "charter_id": "charter-acme-2025",
                    "revision_summary": "Expanded scope to include Gen9 platform readiness.",
                    "changed_fields": {"scope": "expanded", "timeline": "extended by 2 quarters"},
                    "prior_charter_event_id": "unknown-prior",
                },
                "occurred_at": "2025-01-15",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.72,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "program.charter_revised.v1"
    assert candidate.proposed_payload["prior_charter_event_id"] == "unknown-prior"


# ── Wave 3 tests ──────────────────────────────────────────────────────────────

from src.ai.discovery.prose_event_extractor import PROMPT_VERSION_V3  # noqa: E402


def _extract_wave3(
    *,
    prose_text: str = "The team committed to delivering Gen8 BIOS readiness by end of Q2 2025.",
    events_payload: list[dict[str, Any]] | None = None,
) -> ExtractedProseCandidateBatch:
    client = _FakeAIClient(events_payload or [])
    return extract_prose_event_candidates(
        prose_text=prose_text,
        program_id=_PROGRAM_ID,
        source_ref=_SOURCE_REF,
        batch_id=_BATCH_ID,
        default_occurred_at=_DEFAULT_OCCURRED_AT,
        wave=3,
        client=client,
    )


def test_wave3_commitment_made_event() -> None:
    batch = _extract_wave3(
        events_payload=[
            {
                "event_type": "commitment.made.v1",
                "payload": {
                    "commitment_id": "commit-gen8-bios-readiness-q2-2025",
                    "text": "Gen8 BIOS readiness gate passed by end of Q2 2025.",
                    "owner_person_id": "acme-bios-team",
                    "due_date": "2025-06-30",
                    "made_in": "acme-weekly-issue-085",
                },
                "occurred_at": "2025-04-15",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.83,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "commitment.made.v1"
    assert candidate.proposed_payload["commitment_id"] == "commit-gen8-bios-readiness-q2-2025"
    assert candidate.proposed_payload["due_date"] == "2025-06-30"
    assert candidate.proposed_confidence == "ai_extracted"


def test_wave3_commitment_slipped_event() -> None:
    batch = _extract_wave3(
        events_payload=[
            {
                "event_type": "commitment.slipped.v1",
                "payload": {
                    "commitment_id": "commit-gen8-bios-readiness-q2-2025",
                    "new_due_date": "2025-09-30",
                    "reason": "Supply chain delay extended BIOS validation by 6 weeks.",
                },
                "occurred_at": "2025-06-01",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.80,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "commitment.slipped.v1"
    assert candidate.proposed_payload["new_due_date"] == "2025-09-30"


def test_wave3_assumption_stated_event() -> None:
    batch = _extract_wave3(
        events_payload=[
            {
                "event_type": "assumption.stated.v1",
                "payload": {
                    "assumption_id": "assumption-supply-chain-stable-q3",
                    "statement": "Supply chain constraints will ease by Q3 2025, allowing Gen8 volume ramp.",
                    "validation_plan": "Track supply forecasts monthly; escalate if BIOS lead time > 8 weeks.",
                },
                "occurred_at": None,
                "temporal_confidence": "estimated",
                "extraction_confidence": 0.77,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "assumption.stated.v1"
    assert candidate.proposed_payload["assumption_id"] == "assumption-supply-chain-stable-q3"
    assert "Supply chain" in candidate.proposed_payload["statement"]


def test_wave3_assumption_invalidated_event() -> None:
    batch = _extract_wave3(
        events_payload=[
            {
                "event_type": "assumption.invalidated.v1",
                "payload": {
                    "assumption_id": "assumption-supply-chain-stable-q3",
                    "evidence": "BIOS lead times exceeded 12 weeks as of July 2025.",
                    "impact": "Gen8 volume ramp delayed by one quarter.",
                },
                "occurred_at": "2025-07-15",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.82,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "assumption.invalidated.v1"
    assert "lead times" in candidate.proposed_payload["evidence"]


def test_wave3_dependency_declared_event() -> None:
    batch = _extract_wave3(
        events_payload=[
            {
                "event_type": "dependency.declared.v1",
                "payload": {
                    "dependency_id": "dep-ws-bios-on-supply-chain-q3",
                    "from_entity": "ws-bios-ap-rollout",
                    "to_entity": "acme-supply-chain-team",
                    "description": "BIOS AP rollout depends on supply chain delivering Gen8 units.",
                    "needed_by": "2025-09-30",
                },
                "occurred_at": "2025-05-01",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.78,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "dependency.declared.v1"
    assert candidate.proposed_payload["from_entity"] == "ws-bios-ap-rollout"
    assert candidate.proposed_payload["to_entity"] == "acme-supply-chain-team"


def test_wave3_incident_opened_event() -> None:
    batch = _extract_wave3(
        events_payload=[
            {
                "event_type": "incident.opened.v1",
                "payload": {
                    "incident_id": "incident-gen8-bios-flash-failure-q2-2025",
                    "severity": "Sev2",
                    "title": "Gen8 BIOS flash failure on validation batch.",
                    "impacted_entities": ["ws-bios-ap-rollout", "acme-gen8-fleet"],
                },
                "occurred_at": "2025-05-20",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.85,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "incident.opened.v1"
    assert candidate.proposed_payload["severity"] == "Sev2"
    assert len(candidate.proposed_payload["impacted_entities"]) == 2


def test_wave3_incident_resolved_event() -> None:
    batch = _extract_wave3(
        events_payload=[
            {
                "event_type": "incident.resolved.v1",
                "payload": {
                    "incident_id": "incident-gen8-bios-flash-failure-q2-2025",
                    "resolved_on": "2025-05-21",
                    "mttr_minutes": 1440,
                    "root_cause": "Firmware signing cert expired; renewed and redeployed.",
                },
                "occurred_at": "2025-05-21",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.84,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "incident.resolved.v1"
    assert candidate.proposed_payload["mttr_minutes"] == 1440


def test_wave3_uses_v3_prompt_version() -> None:
    client = _FakeAIClient([])
    extract_prose_event_candidates(
        prose_text="Team committed to delivering Gen9 by Q4.",
        program_id=_PROGRAM_ID,
        source_ref=_SOURCE_REF,
        batch_id=_BATCH_ID,
        default_occurred_at=_DEFAULT_OCCURRED_AT,
        wave=3,
        client=client,
    )
    assert client.last_prompt_version == PROMPT_VERSION_V3
    assert PROMPT_VERSION_V3 == "prose_event_extractor.v3"


def test_wave3_rejects_wave1_event_types() -> None:
    batch = _extract_wave3(
        events_payload=[
            {
                "event_type": "decision.made.v1",
                "payload": {
                    "decision_id": "dec-x",
                    "title": "Some decision",
                    "decision_text": "We decided X.",
                    "decided_by": ["team-x"],
                },
                "occurred_at": "2025-06-01",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.80,
            }
        ]
    )
    assert batch.candidates == ()
    assert any("Unsupported" in w for w in batch.warnings)


def test_wave3_rejects_wave2_event_types() -> None:
    batch = _extract_wave3(
        events_payload=[
            {
                "event_type": "program.phase_entered.v1",
                "payload": {"phase_id": "phase-x", "phase_name": "Phase X"},
                "occurred_at": None,
                "temporal_confidence": "estimated",
                "extraction_confidence": 0.70,
            }
        ]
    )
    assert batch.candidates == ()
    assert any("Unsupported" in w for w in batch.warnings)


def test_wave3_commitment_fulfilled_with_evidence() -> None:
    batch = _extract_wave3(
        events_payload=[
            {
                "event_type": "commitment.fulfilled.v1",
                "payload": {
                    "commitment_id": "commit-gen8-bios-readiness-q2-2025",
                    "fulfilled_on": "2025-09-28",
                    "evidence": "Gen8 BIOS validation sign-off recorded in issue-095.",
                },
                "occurred_at": "2025-09-28",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.88,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "commitment.fulfilled.v1"
    assert "Gen8" in candidate.proposed_payload["evidence"]


# ── Wave 4 tests ──────────────────────────────────────────────────────────────

from src.ai.discovery.prose_event_extractor import PROMPT_VERSION_V4  # noqa: E402


def _extract_wave4(
    *,
    prose_text: str = "Acme team published the Gen8 BIOS validation guide to the internal wiki.",
    events_payload: list[dict[str, Any]] | None = None,
) -> ExtractedProseCandidateBatch:
    client = _FakeAIClient(events_payload or [])
    return extract_prose_event_candidates(
        prose_text=prose_text,
        program_id=_PROGRAM_ID,
        source_ref=_SOURCE_REF,
        batch_id=_BATCH_ID,
        default_occurred_at=_DEFAULT_OCCURRED_AT,
        wave=4,
        client=client,
    )


def test_wave4_knowledge_article_added_event() -> None:
    batch = _extract_wave4(
        events_payload=[
            {
                "event_type": "knowledge.article_added.v1",
                "payload": {
                    "article_id": "wiki-gen8-bios-validation-guide",
                    "title": "Gen8 BIOS Validation Guide",
                    "location": "https://wiki.example.com/acme/gen8-bios-validation",
                    "topics": ["bios", "gen8", "validation", "platform-readiness"],
                },
                "occurred_at": "2025-03-10",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.82,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "knowledge.article_added.v1"
    assert candidate.proposed_payload["article_id"] == "wiki-gen8-bios-validation-guide"
    assert "bios" in candidate.proposed_payload["topics"]
    assert candidate.proposed_confidence == "ai_extracted"


def test_wave4_sku_generation_added_event() -> None:
    batch = _extract_wave4(
        events_payload=[
            {
                "event_type": "sku_generation.added.v1",
                "payload": {
                    "sku_generation_id": "gen9",
                    "name": "Gen9 Platform",
                    "first_deployment_date": "2026-Q1",
                    "products": ["acme-gen9-server", "acme-gen9-compute"],
                },
                "occurred_at": "2025-04-01",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.85,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "sku_generation.added.v1"
    assert candidate.proposed_payload["sku_generation_id"] == "gen9"
    assert len(candidate.proposed_payload["products"]) == 2


def test_wave4_kpi_defined_event() -> None:
    batch = _extract_wave4(
        events_payload=[
            {
                "event_type": "kpi.defined.v1",
                "payload": {
                    "kpi_id": "kpi-bios-ap-rollout-pct",
                    "name": "BIOS AP Rollout Percentage",
                    "definition": "Percentage of Gen8 fleet with BIOS AP enabled.",
                    "unit": "percent",
                    "owner_person_id": "acme-bios-team",
                    "thresholds": {"red": "< 80", "yellow": "80-90", "green": "> 90"},
                },
                "occurred_at": None,
                "temporal_confidence": "estimated",
                "extraction_confidence": 0.78,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "kpi.defined.v1"
    assert candidate.proposed_payload["kpi_id"] == "kpi-bios-ap-rollout-pct"
    assert candidate.proposed_payload["unit"] == "percent"


def test_wave4_kpi_threshold_crossed_event() -> None:
    batch = _extract_wave4(
        events_payload=[
            {
                "event_type": "kpi.threshold_crossed.v1",
                "payload": {
                    "kpi_id": "kpi-bios-ap-rollout-pct",
                    "threshold": "90%",
                    "direction": "above",
                    "observed_value": 92.4,
                },
                "occurred_at": "2025-09-15",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.87,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "kpi.threshold_crossed.v1"
    assert candidate.proposed_payload["direction"] == "above"
    assert candidate.proposed_payload["observed_value"] == 92.4


def test_wave4_uses_v4_prompt_version() -> None:
    client = _FakeAIClient([])
    extract_prose_event_candidates(
        prose_text="Gen9 platform added to the Acme scope.",
        program_id=_PROGRAM_ID,
        source_ref=_SOURCE_REF,
        batch_id=_BATCH_ID,
        default_occurred_at=_DEFAULT_OCCURRED_AT,
        wave=4,
        client=client,
    )
    assert client.last_prompt_version == PROMPT_VERSION_V4
    assert PROMPT_VERSION_V4 == "prose_event_extractor.v4"


def test_wave4_rejects_wave1_event_types() -> None:
    batch = _extract_wave4(
        events_payload=[
            {
                "event_type": "milestone.created.v1",
                "payload": {
                    "milestone_id": "ms-gen8-ga",
                    "title": "Gen8 GA",
                    "due_date": "2025-06-30",
                },
                "occurred_at": "2025-03-01",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.80,
            }
        ]
    )
    assert batch.candidates == ()
    assert any("Unsupported" in w for w in batch.warnings)


def test_wave4_kpi_decommissioned_event() -> None:
    batch = _extract_wave4(
        events_payload=[
            {
                "event_type": "kpi.decommissioned.v1",
                "payload": {
                    "kpi_id": "kpi-old-bios-flash-rate",
                    "reason": "Replaced by kpi-bios-ap-rollout-pct after Gen8 transition.",
                },
                "occurred_at": "2025-10-01",
                "temporal_confidence": "approximate",
                "extraction_confidence": 0.75,
            }
        ]
    )
    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate.proposed_event_type == "kpi.decommissioned.v1"
    assert "bios-ap" in candidate.proposed_payload["reason"]
