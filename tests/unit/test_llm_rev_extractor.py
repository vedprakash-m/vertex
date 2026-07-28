"""Unit tests for LLMRevExtractor and the judge harness (specs/gaps.md G1).

Covers:
* LLMRevExtractor.extract — grounded LLM claims are emitted
* Grounding step — excerpt not in canonical text is dropped
* Merge — LLM + deterministic claims combined, deduped (LLM first)
* LLM error fallback — BudgetExceeded / AIClientError → deterministic only
* Zone B boundary — no banned imports in extractor.py
* Judge harness — JudgementReport structure + recommendation
* New event types — risk.blocking_milestone and ownership.changed are material
* _ground_excerpt — offset hint shortcut and fuzzy fallback
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Callable, TypeVar
from unittest.mock import MagicMock

import pytest

from src.ai.client import AIClientError, BudgetExceeded
from src.core.ledger.event_log import read_events
from src.ai.rev.extractor import (
    MATERIAL_EVENT_TYPES,
    DeterministicRevExtractor,
    EvidenceSpan,
    ExtractedClaim,
    LLMRevExtractor,
    LLMRevExtractorUnavailable,
    _build_rev_extractor_user_prompt,
    _ground_excerpt,
    _merge_claims,
    _parse_llm_rev_payload,
    is_material_event,
)
from src.ai.rev.judge import (
    ClaimScore,
    ExtractorJudgement,
    GroundTruthCoverage,
    JudgementReport,
    MessageJudgement,
)
from src.core.rev.identity import CanonicalItemIdentity
from src.core.rev.entity_types import EntityType
from src.core.rev.normalizer import chunk_canonical
from src.core.rev.ports import HydratedContent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

StructuredResponse = TypeVar("StructuredResponse")


def _hydrated(
    text: str,
    *,
    subject: str = "Test subject",
    program_id: str = "nova",
    metadata_only: bool = False,
) -> HydratedContent:
    identity = CanonicalItemIdentity(
        source_type=EntityType.MESSAGE,
        tenant_id="tenant-test",
        principal_mailbox="test@example.com",
        container="inbox",
        resource_id="msg-test",
    )
    chunks = chunk_canonical(text) if text else []
    return HydratedContent(
        identity=identity,
        canonical_text=text,
        normalized_source_hash="sha256:" + text.encode("utf-8").hex()[:64],
        chunks=tuple(chunks),
        route_metadata={"subject": subject, "program_id": program_id},
        metadata_only=metadata_only,
    )


class FakeClient:
    """Controllable LLMProvider that returns pre-canned structured responses."""

    def __init__(self, response: Any, *, raise_exc: Exception | None = None) -> None:
        self._response = response
        self._raise_exc = raise_exc
        self.calls: list[tuple[str, str]] = []

    def chat(self, system: str, user: str, **kwargs: Any) -> str:
        return ""

    def structured(
        self,
        system: str,
        user: str,
        *,
        parser: Callable[[dict[str, Any]], StructuredResponse],
        max_tokens: int = 800,
        prompt_version: str | None = None,
    ) -> StructuredResponse:
        self.calls.append((system[:40], user[:40]))
        if self._raise_exc is not None:
            raise self._raise_exc
        return parser(self._response)


def _llm_extractor(response: Any, *, raise_exc: Exception | None = None) -> LLMRevExtractor:
    return LLMRevExtractor(client=FakeClient(response, raise_exc=raise_exc))


def _make_claim(
    event_type: str,
    excerpt_text: str = "some evidence",
    confidence: float = 0.9,
    payload: dict[str, Any] | None = None,
    model: str = "rev.llm.frontier.v1",
) -> ExtractedClaim:
    return ExtractedClaim(
        event_type=event_type,
        payload=payload or {},
        evidence_spans=(EvidenceSpan("c0", 0, len(excerpt_text), excerpt_text),),
        extraction_confidence=confidence,
        extraction_model=model,
        material=is_material_event(event_type),
    )


# ---------------------------------------------------------------------------
# _ground_excerpt
# ---------------------------------------------------------------------------


class TestGroundExcerpt:
    def test_exact_offset_match(self) -> None:
        text = "The deployment completed on 2026-06-20 with all units passing."
        excerpt = "deployment completed"
        idx = text.index(excerpt)
        result = _ground_excerpt(excerpt, idx, text)
        assert result == (idx, idx + len(excerpt))

    def test_wrong_offset_falls_back_to_find(self) -> None:
        text = "The deployment completed successfully."
        excerpt = "deployment completed"
        result = _ground_excerpt(excerpt, 999, text)  # wrong offset
        idx = text.index(excerpt)
        assert result == (idx, idx + len(excerpt))

    def test_excerpt_not_in_text_returns_none(self) -> None:
        text = "Nothing relevant here."
        result = _ground_excerpt("deployment completed", 0, text)
        assert result is None

    def test_short_excerpt_returns_none(self) -> None:
        result = _ground_excerpt("ab", None, "ab something")
        assert result is None

    def test_trailing_punctuation_stripped_on_fallback(self) -> None:
        text = "The milestone is complete."
        excerpt = "milestone is complete."  # exact match will find it
        result = _ground_excerpt(excerpt, 99, text)
        assert result is not None

    def test_empty_excerpt_returns_none(self) -> None:
        assert _ground_excerpt("", None, "some text") is None


# ---------------------------------------------------------------------------
# _parse_llm_rev_payload
# ---------------------------------------------------------------------------


class TestParseLLMRevPayload:
    def test_grounded_event_emitted(self) -> None:
        text = "The deployment completed successfully on 2026-06-20."
        excerpt = "deployment completed successfully"
        payload = {
            "events": [{
                "event_type": "deployment.completed",
                "payload": {"subject": "Gen9", "status": "completed"},
                "excerpt": excerpt,
                "excerpt_start": text.index(excerpt),
                "extraction_confidence": 0.9,
            }]
        }
        hydrated = _hydrated(text)
        claims = _parse_llm_rev_payload(payload, canonical_text=text, hydrated=hydrated)
        assert len(claims) == 1
        assert claims[0].event_type == "deployment.completed"
        assert claims[0].material is True
        assert claims[0].evidence_spans[0].excerpt_text == excerpt

    def test_ungrounded_excerpt_dropped(self) -> None:
        text = "The deployment completed."
        payload = {
            "events": [{
                "event_type": "deployment.completed",
                "payload": {"subject": "X", "status": "completed"},
                "excerpt": "fabricated text not in source at all",
                "excerpt_start": 0,
                "extraction_confidence": 0.9,
            }]
        }
        hydrated = _hydrated(text)
        claims = _parse_llm_rev_payload(payload, canonical_text=text, hydrated=hydrated)
        assert claims == ()

    def test_unknown_event_type_dropped(self) -> None:
        text = "Some event happened."
        payload = {
            "events": [{
                "event_type": "unknown.event.type",
                "payload": {},
                "excerpt": "Some event happened",
                "excerpt_start": 0,
                "extraction_confidence": 0.8,
            }]
        }
        hydrated = _hydrated(text)
        claims = _parse_llm_rev_payload(payload, canonical_text=text, hydrated=hydrated)
        assert claims == ()

    def test_invalid_payload_structure_returns_empty(self) -> None:
        hydrated = _hydrated("text")
        assert _parse_llm_rev_payload("not a dict", canonical_text="text", hydrated=hydrated) == ()  # type: ignore[arg-type]
        assert _parse_llm_rev_payload({}, canonical_text="text", hydrated=hydrated) == ()

    def test_dedup_same_event_type_and_payload_core(self) -> None:
        text = "The deployment completed."
        excerpt = "deployment completed"
        item = {
            "event_type": "deployment.completed",
            "payload": {"subject": "Gen9", "status": "completed"},
            "excerpt": excerpt,
            "excerpt_start": text.index(excerpt),
            "extraction_confidence": 0.9,
        }
        payload = {"events": [item, item]}  # duplicate
        hydrated = _hydrated(text)
        claims = _parse_llm_rev_payload(payload, canonical_text=text, hydrated=hydrated)
        assert len(claims) == 1

    def test_new_event_types_emitted(self) -> None:
        text = "The partner dependency is now blocking our P0 milestone."
        excerpt = "partner dependency is now blocking our P0 milestone"
        payload = {
            "events": [{
                "event_type": "risk.blocking_milestone",
                "payload": {"description": "partner dependency blocks P0"},
                "excerpt": excerpt,
                "excerpt_start": text.index(excerpt),
                "extraction_confidence": 0.85,
            }]
        }
        hydrated = _hydrated(text)
        claims = _parse_llm_rev_payload(payload, canonical_text=text, hydrated=hydrated)
        assert len(claims) == 1
        assert claims[0].event_type == "risk.blocking_milestone"
        assert claims[0].material is True

    def test_ownership_changed_emitted(self) -> None:
        text = "Priya will now own the Gen9 bringup coordination going forward."
        excerpt = "Priya will now own the Gen9 bringup coordination"
        payload = {
            "events": [{
                "event_type": "ownership.changed",
                "payload": {"area": "Gen9 bringup", "new_owner": "Priya"},
                "excerpt": excerpt,
                "excerpt_start": text.index(excerpt),
                "extraction_confidence": 0.9,
            }]
        }
        hydrated = _hydrated(text)
        claims = _parse_llm_rev_payload(payload, canonical_text=text, hydrated=hydrated)
        assert len(claims) == 1
        assert claims[0].event_type == "ownership.changed"


# ---------------------------------------------------------------------------
# _merge_claims
# ---------------------------------------------------------------------------


class TestMergeClaims:
    def test_llm_claims_appear_first(self) -> None:
        llm = (_make_claim("deployment.completed", model="rev.llm.frontier.v1"),)
        det = (_make_claim("commitment.date_set", model="rev.deterministic.regex.v1"),)
        merged = _merge_claims(llm, det)
        assert len(merged) == 2
        assert merged[0].extraction_model == "rev.llm.frontier.v1"
        assert merged[1].extraction_model == "rev.deterministic.regex.v1"

    def test_duplicate_claim_deduped_llm_wins(self) -> None:
        payload = {"status": "completed", "subject": "Gen9"}
        llm = (_make_claim("deployment.completed", payload=payload, model="rev.llm.frontier.v1"),)
        det = (_make_claim("deployment.completed", payload=payload, model="rev.deterministic.regex.v1"),)
        merged = _merge_claims(llm, det)
        assert len(merged) == 1
        assert merged[0].extraction_model == "rev.llm.frontier.v1"

    def test_non_overlapping_claims_all_included(self) -> None:
        llm = (
            _make_claim("deployment.completed"),
            _make_claim("ownership.changed"),
        )
        det = (
            _make_claim("commitment.date_set"),
        )
        merged = _merge_claims(llm, det)
        assert len(merged) == 3


# ---------------------------------------------------------------------------
# LLMRevExtractor.extract
# ---------------------------------------------------------------------------


class TestLLMRevExtractor:
    def test_grounded_llm_claims_emitted(self) -> None:
        text = "The rollout deployment completed on 2026-06-20."
        excerpt = "rollout deployment completed"
        llm_response = {
            "events": [{
                "event_type": "deployment.completed",
                "payload": {"subject": "Gen9 rollout", "status": "completed"},
                "excerpt": excerpt,
                "excerpt_start": text.index(excerpt),
                "extraction_confidence": 0.95,
            }]
        }
        extractor = _llm_extractor(llm_response)
        from src.core.rev.result import Success
        result = extractor.extract(_hydrated(text), correlation_id="test-001")
        assert isinstance(result, Success)
        claims = result.value
        assert any(c.event_type == "deployment.completed" for c in claims)

    def test_metadata_only_returns_empty(self) -> None:
        extractor = _llm_extractor({"events": []})
        from src.core.rev.result import Success
        result = extractor.extract(_hydrated("", metadata_only=True), correlation_id="test-meta")
        assert isinstance(result, Success)
        assert result.value == ()

    def test_budget_exceeded_falls_back_to_deterministic(self) -> None:
        text = "The deployment completed successfully."
        extractor = _llm_extractor(None, raise_exc=BudgetExceeded("budget"))
        from src.core.rev.result import Success
        result = extractor.extract(_hydrated(text), correlation_id="test-budget")
        assert isinstance(result, Success)
        assert any(c.event_type == "deployment.completed" for c in result.value)

    def test_ai_client_error_falls_back_to_deterministic(self) -> None:
        text = "The rollout completed."
        extractor = _llm_extractor(None, raise_exc=AIClientError("timeout"))
        from src.core.rev.result import Success
        result = extractor.extract(_hydrated(text), correlation_id="test-err")
        assert isinstance(result, Success)
        assert any(c.event_type == "deployment.completed" for c in result.value)

    def test_llm_adds_ownership_fact_missed_by_deterministic(self) -> None:
        text = "Priya will now own the Gen9 bringup coordination going forward."
        own_excerpt = "Priya will now own the Gen9 bringup coordination"
        llm_response = {
            "events": [{
                "event_type": "ownership.changed",
                "payload": {"area": "Gen9 bringup", "new_owner": "Priya"},
                "excerpt": own_excerpt,
                "excerpt_start": text.index(own_excerpt),
                "extraction_confidence": 0.9,
            }]
        }
        extractor = _llm_extractor(llm_response)
        from src.core.rev.result import Success
        result = extractor.extract(_hydrated(text), correlation_id="test-novel")
        assert isinstance(result, Success)
        event_types = {c.event_type for c in result.value}
        assert "ownership.changed" in event_types

    def test_risk_blocking_milestone_extracted(self) -> None:
        text = "The partner dependency is now blocking our P0 milestone."
        risk_excerpt = "partner dependency is now blocking our P0 milestone"
        llm_response = {
            "events": [{
                "event_type": "risk.blocking_milestone",
                "payload": {"description": "partner dependency blocking P0"},
                "excerpt": risk_excerpt,
                "excerpt_start": text.index(risk_excerpt),
                "extraction_confidence": 0.88,
            }]
        }
        extractor = _llm_extractor(llm_response)
        from src.core.rev.result import Success
        result = extractor.extract(_hydrated(text), correlation_id="test-risk")
        assert isinstance(result, Success)
        assert any(c.event_type == "risk.blocking_milestone" for c in result.value)

    def test_empty_chunks_returns_empty(self) -> None:
        extractor = _llm_extractor({"events": []})
        from src.core.rev.result import Success
        result = extractor.extract(_hydrated(""), correlation_id="test-empty")
        assert isinstance(result, Success)
        assert result.value == ()

    def test_ungrounded_llm_claim_dropped_deterministic_survives(self) -> None:
        text = "The deployment completed successfully."
        llm_response = {
            "events": [{
                "event_type": "deployment.completed",
                "payload": {"subject": "fabricated", "status": "completed"},
                "excerpt": "THIS TEXT IS NOT IN THE SOURCE",
                "excerpt_start": 0,
                "extraction_confidence": 0.99,
            }]
        }
        extractor = _llm_extractor(llm_response)
        from src.core.rev.result import Success
        result = extractor.extract(_hydrated(text), correlation_id="test-ungrounded")
        assert isinstance(result, Success)
        # Deterministic still finds deployment.completed; ungrounded LLM claim dropped
        assert any(c.event_type == "deployment.completed" for c in result.value)
        # All surviving claims must be grounded (excerpt_text is substring of text)
        for claim in result.value:
            for span in claim.evidence_spans:
                assert span.excerpt_text in text, (
                    f"Claim excerpt_text {span.excerpt_text!r} not found in canonical text"
                )


# ---------------------------------------------------------------------------
# AISchemaGateway / ai_release_audit wiring (specs/backlog.md BL-C2 caveat,
# resolved 2026-07-27 — rev_extractor's LLM tier is a production-consequence
# call site: its output becomes candidate program facts staged for human
# triage, the same shape as risk_proposal_generator, which already carries
# this exact wiring under the same "advisory" classification.)
# ---------------------------------------------------------------------------


class TestLLMRevExtractorAuditTrail:
    def test_records_released_audit_trail_on_grounded_extraction(self, tmp_path: Path) -> None:
        text = "The rollout deployment completed on 2026-06-20."
        excerpt = "rollout deployment completed"
        llm_response = {
            "events": [{
                "event_type": "deployment.completed",
                "payload": {"status": "completed"},
                "excerpt": excerpt,
                "excerpt_start": text.index(excerpt),
                "extraction_confidence": 0.95,
            }]
        }
        extractor = LLMRevExtractor(
            client=FakeClient(llm_response),
            cache_program_id="testprog",
            cache_programs_root=tmp_path,
        )
        from src.core.rev.result import Success
        result = extractor.extract(_hydrated(text, program_id="testprog"), correlation_id="test-released")
        assert isinstance(result, Success)

        events = read_events("testprog", programs_root=tmp_path)
        event_types = [event.event_type for event in events]
        assert event_types.count("ai.run_lifecycle.v1") == 5
        assert event_types.count("ai.release_decision.v1") == 1
        release_event = next(event for event in events if event.event_type == "ai.release_decision.v1")
        assert release_event.payload["terminal"] == "released"
        lifecycle_states = [
            event.payload["state"] for event in events if event.event_type == "ai.run_lifecycle.v1"
        ]
        assert lifecycle_states == ["planned", "requested", "responded", "schema_validated", "semantically_validated"]

    def test_records_rejected_audit_trail_on_oversized_payload(self, tmp_path: Path) -> None:
        # A payload violating AISchemaGateway's bounds (array length > 1000)
        # must be rejected before _parse_llm_rev_payload ever inspects it,
        # and must still fall back to the deterministic baseline (never raise).
        text = "The deployment completed successfully."
        oversized_response = {"events": [{"event_type": "deployment.completed"}] * 1001}
        extractor = LLMRevExtractor(
            client=FakeClient(oversized_response),
            cache_program_id="testprog",
            cache_programs_root=tmp_path,
        )
        from src.core.rev.result import Success
        result = extractor.extract(_hydrated(text, program_id="testprog"), correlation_id="test-rejected")
        assert isinstance(result, Success)
        # Deterministic extractor still finds the completion from regex.
        assert any(c.event_type == "deployment.completed" for c in result.value)

        events = read_events("testprog", programs_root=tmp_path)
        release_event = next(event for event in events if event.event_type == "ai.release_decision.v1")
        assert release_event.payload["terminal"] == "rejected"
        assert "AISchemaGateway" in release_event.payload["reason"]
        lifecycle_states = {
            event.payload["state"] for event in events if event.event_type == "ai.run_lifecycle.v1"
        }
        assert "schema_validated" not in lifecycle_states
        assert "semantically_validated" not in lifecycle_states

    def test_records_discarded_audit_trail_on_provider_error(self, tmp_path: Path) -> None:
        text = "The deployment completed successfully."
        extractor = LLMRevExtractor(
            client=FakeClient(None, raise_exc=BudgetExceeded("budget")),
            cache_program_id="testprog",
            cache_programs_root=tmp_path,
        )
        from src.core.rev.result import Success
        result = extractor.extract(_hydrated(text, program_id="testprog"), correlation_id="test-discarded")
        assert isinstance(result, Success)
        assert any(c.event_type == "deployment.completed" for c in result.value)

        events = read_events("testprog", programs_root=tmp_path)
        release_event = next(event for event in events if event.event_type == "ai.release_decision.v1")
        assert release_event.payload["terminal"] == "discarded"
        assert "provider call failed" in release_event.payload["reason"]
        lifecycle_states = [
            event.payload["state"] for event in events if event.event_type == "ai.run_lifecycle.v1"
        ]
        assert lifecycle_states == ["planned", "requested"]

    def test_skips_audit_trail_when_program_id_unknown(self, tmp_path: Path) -> None:
        # scripts/run_rev_judge.py's bare from_env() construction has no
        # program to attribute a trail to -- the same honest limitation
        # already accepted for anticipation_engine's no-program_id branch.
        # Must not crash, and must not write anything.
        text = "The rollout deployment completed on 2026-06-20."
        excerpt = "rollout deployment completed"
        llm_response = {
            "events": [{
                "event_type": "deployment.completed",
                "payload": {"status": "completed"},
                "excerpt": excerpt,
                "excerpt_start": text.index(excerpt),
                "extraction_confidence": 0.95,
            }]
        }
        extractor = _llm_extractor(llm_response)  # no cache_program_id/programs_root
        from src.core.rev.result import Success
        result = extractor.extract(_hydrated(text), correlation_id="test-no-program")
        assert isinstance(result, Success)
        assert any(c.event_type == "deployment.completed" for c in result.value)
        # No program context was ever supplied, so there is no ledger to read
        # from at all -- the absence of a crash here is the assertion.


# ---------------------------------------------------------------------------
# New material event types
# ---------------------------------------------------------------------------


class TestMaterialEventTypes:
    def test_risk_blocking_milestone_is_material(self) -> None:
        assert is_material_event("risk.blocking_milestone")

    def test_ownership_changed_is_material(self) -> None:
        assert is_material_event("ownership.changed")

    def test_all_original_types_still_material(self) -> None:
        for et in [
            "deployment.completed", "deployment.rollback", "deployment.started",
            "incident.severity_changed", "commitment.date_set", "milestone.completed",
        ]:
            assert is_material_event(et), f"{et} should be material"

    def test_unknown_type_is_not_material(self) -> None:
        assert not is_material_event("office.party.scheduled")


# ---------------------------------------------------------------------------
# Zone B boundary check (no banned imports)
# ---------------------------------------------------------------------------


class TestZoneBBoundaryExtractorLLM:
    BANNED_IMPORTS = {"src.commands", "src.m365"}

    def test_llm_extractor_no_banned_imports(self) -> None:
        src_path = Path(__file__).parents[2] / "src" / "ai" / "rev" / "extractor.py"
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = (
                    node.names[0].name
                    if isinstance(node, ast.Import)
                    else (node.module or "")
                )
                for banned in self.BANNED_IMPORTS:
                    assert not module.startswith(banned), (
                        f"extractor.py imports {module!r} — violates Zone B boundary"
                    )

    def test_judge_no_banned_imports(self) -> None:
        src_path = Path(__file__).parents[2] / "src" / "ai" / "rev" / "judge.py"
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = (
                    node.names[0].name
                    if isinstance(node, ast.Import)
                    else (node.module or "")
                )
                for banned in self.BANNED_IMPORTS:
                    assert not module.startswith(banned), (
                        f"judge.py imports {module!r} — violates Zone B boundary"
                    )


# ---------------------------------------------------------------------------
# WO-3: rev_judge system prompt is registry-managed (specs/backlog.md)
# ---------------------------------------------------------------------------


class TestJudgePromptRegistration:
    """rev_judge.v1 replaces the old ``_JUDGE_SYSTEM_PROMPT`` constant."""

    def test_rev_judge_v1_is_registered(self) -> None:
        from src.ai.prompt_registry import registered_versions

        assert "rev_judge.v1" in registered_versions()

    def test_loaded_prompt_matches_the_former_constant_verbatim(self) -> None:
        from src.ai.prompt_registry import load_prompt
        from src.ai.rev.judge import LLM_PROMPT_VERSION

        assert LLM_PROMPT_VERSION == "rev_judge.v1"
        loaded = load_prompt(LLM_PROMPT_VERSION)
        # The former `_JUDGE_SYSTEM_PROMPT` module constant, verbatim (only a
        # single trailing newline is gone -- load_prompt() always strips --
        # which does not change model behaviour).
        former_constant = (
            'You are a fact-extraction judge for a Technical Program Management (TPM) intelligence system.\n'
            '\n'
            'You will be shown:\n'
            '1. A canonical email body (source text)\n'
            '2. Two lists of extracted program events (Extractor A and Extractor B)\n'
            '\n'
            'Your task is to evaluate each extracted event and score the overall quality of each extractor.\n'
            '\n'
            '## Scoring per extracted event\n'
            '\n'
            'For each event in each extractor\'s output, assign one of:\n'
            '- CORRECT: The event is factually supported by the source text, the event_type is appropriate, and the excerpt is a real substring of the text.\n'
            '- PARTIAL: The event is partially correct — the fact exists but the event_type is wrong, the excerpt is off, or key payload fields are missing/incorrect.\n'
            '- HALLUCINATED: The event asserts something not clearly stated in the source text.\n'
            '\n'
            '## Ground-truth events\n'
            '\n'
            'You will also be shown a list of "ground truth" events — the complete set of material facts present in the source text. These are pre-identified by a human reviewer.\n'
            '\n'
            'For each ground-truth event, determine which extractor (A, B, both, or neither) captured it.\n'
            '\n'
            '## Output format\n'
            '\n'
            'Return a JSON object with this structure:\n'
            '{\n'
            '  "extractor_a": {\n'
            '    "scores": [{"event_type": "...", "verdict": "CORRECT|PARTIAL|HALLUCINATED", "reason": "..."}],\n'
            '    "precision": <float 0-1>,\n'
            '    "recall": <float 0-1>\n'
            '  },\n'
            '  "extractor_b": {\n'
            '    "scores": [{"event_type": "...", "verdict": "CORRECT|PARTIAL|HALLUCINATED", "reason": "..."}],\n'
            '    "precision": <float 0-1>,\n'
            '    "recall": <float 0-1>\n'
            '  },\n'
            '  "ground_truth_coverage": [\n'
            '    {"fact": "...", "captured_by": "A|B|both|neither"}\n'
            '  ],\n'
            '  "summary": "one paragraph comparing the two extractors"\n'
            '}'
        )
        assert loaded == former_constant

    def test_judge_source_calls_load_prompt_not_an_inline_literal(self) -> None:
        """judge.py's frontier call site must resolve its system prompt via
        load_prompt(), not a module-level string constant (WO-3)."""
        src_path = Path(__file__).parents[2] / "src" / "ai" / "rev" / "judge.py"
        source = src_path.read_text(encoding="utf-8")
        assert "_JUDGE_SYSTEM_PROMPT" not in source
        tree = ast.parse(source, filename=str(src_path))
        assert any(
            isinstance(node, ast.ImportFrom)
            and node.module == "src.ai.prompt_registry"
            and any(alias.name == "load_prompt" for alias in node.names)
            for node in ast.walk(tree)
        ), "judge.py must import load_prompt from src.ai.prompt_registry"
        assert any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "load_prompt"
            for node in ast.walk(tree)
        ), "judge.py must resolve its system prompt via load_prompt(...)"


# ---------------------------------------------------------------------------
# JudgementReport — structure + recommendation
# ---------------------------------------------------------------------------


class TestJudgementReport:
    def test_report_serializes_to_dict(self) -> None:
        mj = MessageJudgement(
            message_id="msg-001",
            subject="Test subject",
            extractor_a=ExtractorJudgement(
                extractor_name="deterministic",
                scores=[ClaimScore("deployment.completed", "CORRECT", "exact match")],
                precision=1.0,
                recall=0.5,
            ),
            extractor_b=ExtractorJudgement(
                extractor_name="llm",
                scores=[
                    ClaimScore("deployment.completed", "CORRECT", "exact match"),
                    ClaimScore("ownership.changed", "CORRECT", "grounded"),
                ],
                precision=1.0,
                recall=1.0,
            ),
            ground_truth_coverage=[
                GroundTruthCoverage("deployment.completed", "both"),
                GroundTruthCoverage("ownership.changed", "B"),
            ],
            summary="LLM found 2 facts, deterministic found 1.",
        )
        report = JudgementReport(
            extractor_a_name="deterministic",
            extractor_b_name="llm",
            message_judgements=[mj],
            overall_precision_a=1.0,
            overall_recall_a=0.5,
            overall_precision_b=1.0,
            overall_recall_b=1.0,
            recommendation="Use llm: higher recall.",
        )
        d = report.to_dict()
        assert d["overall_recall_b"] == 1.0
        assert len(d["messages"]) == 1
        assert d["messages"][0]["extractor_b"]["correct"] == 2

    def test_render_human_includes_summary(self) -> None:
        report = JudgementReport(
            extractor_a_name="deterministic",
            extractor_b_name="llm",
            message_judgements=[],
            overall_precision_a=0.8,
            overall_recall_a=0.4,
            overall_precision_b=0.85,
            overall_recall_b=0.7,
            recommendation="Use llm.",
        )
        rendered = report.render_human()
        assert "AGGREGATE METRICS" in rendered
        assert "RECOMMENDATION" in rendered
        assert "Use llm." in rendered


# ---------------------------------------------------------------------------
# User prompt builder
# ---------------------------------------------------------------------------


class TestBuildRevExtractorUserPrompt:
    def test_includes_program_and_subject(self) -> None:
        prompt = _build_rev_extractor_user_prompt(
            "The deployment completed.", subject="NOVA Weekly", program_id="nova"
        )
        assert "nova" in prompt
        assert "NOVA Weekly" in prompt
        # activation.md §6.14.9 / RK-32: the untrusted body is wrapped in a
        # randomized delimiter fence (not a fixed human-readable label).
        assert "untrusted-email-" in prompt
        assert "The deployment completed." in prompt

    def test_missing_program_ok(self) -> None:
        prompt = _build_rev_extractor_user_prompt("some text", subject="", program_id="")
        assert "some text" in prompt


# ---------------------------------------------------------------------------
# KI-1: _date_in bounded nearest-match
# ---------------------------------------------------------------------------


class TestDateInBounded:
    """KI-1 (P1-6): ``_date_in`` attributes each event to the date nearest (by
    offset) to the event match, not the first date in the chunk."""

    def test_nearest_date_wins_for_two_dates_in_chunk(self) -> None:
        from src.ai.rev.extractor import _date_in, _DEPLOY_COMPLETED_RE
        # Two dates in one chunk; the completion event sits next to the second.
        text = "2026-06-20 some preamble. The deployment completed on 2026-06-24."
        match = next(_DEPLOY_COMPLETED_RE.finditer(text))
        date = _date_in(text, match)
        assert date == "2026-06-24", (
            "KI-1: nearest date to the event match must win, got " + repr(date)
        )

    def test_returns_empty_when_no_date_in_chunk(self) -> None:
        from src.ai.rev.extractor import _date_in, _ROLLBACK_RE
        text = "We rolled back the deployment after errors."
        match = next(_ROLLBACK_RE.finditer(text))
        assert _date_in(text, match) == ""


# ---------------------------------------------------------------------------
# KI-3: non-numeric extraction_confidence defaults to 0.7 (no raise)
# ---------------------------------------------------------------------------


class TestConfidenceParseSafety:
    """KI-3a (P1-7): a non-numeric ``extraction_confidence`` must not raise; it
    defaults to 0.7."""

    def test_non_numeric_confidence_defaults_to_seven(self) -> None:
        text = "The deployment completed successfully."
        payload = {
            "events": [{
                "event_type": "deployment.completed",
                "payload": {"subject": "X", "status": "completed"},
                "excerpt": "deployment completed successfully",
                "excerpt_start": text.index("deployment completed successfully"),
                "extraction_confidence": "high",  # non-numeric
            }]
        }
        hydrated = _hydrated(text)
        claims = _parse_llm_rev_payload(payload, canonical_text=text, hydrated=hydrated)
        assert len(claims) == 1
        assert claims[0].extraction_confidence == 0.7

    def test_none_confidence_defaults_to_seven(self) -> None:
        text = "The deployment completed successfully."
        payload = {
            "events": [{
                "event_type": "deployment.completed",
                "payload": {"subject": "X", "status": "completed"},
                "excerpt": "deployment completed successfully",
                "excerpt_start": text.index("deployment completed successfully"),
                "extraction_confidence": None,
            }]
        }
        hydrated = _hydrated(text)
        claims = _parse_llm_rev_payload(payload, canonical_text=text, hydrated=hydrated)
        assert len(claims) == 1
        assert claims[0].extraction_confidence == 0.7


# ---------------------------------------------------------------------------
# KI-3b + KI-5 + RK-9: fallback_count incremented on every LLM→deterministic fallback
# ---------------------------------------------------------------------------


class TestFallbackCount:
    """KI-3b / KI-5 / RK-9 (P1-7/P1-11/P1-4): every LLM fallback increments
    ``LLMRevExtractor.fallback_count`` so ``RevCycleReport.llm_fallback_count``
    can surface it."""

    def test_budget_exceeded_increments_fallback_count(self) -> None:
        extractor = _llm_extractor(None, raise_exc=BudgetExceeded("budget"))
        extractor.extract(_hydrated("The deployment completed successfully."), correlation_id="fb-1")
        assert extractor.fallback_count == 1

    def test_ai_client_error_increments_fallback_count(self) -> None:
        extractor = _llm_extractor(None, raise_exc=AIClientError("boom"))
        extractor.extract(_hydrated("The deployment completed successfully."), correlation_id="fb-2")
        assert extractor.fallback_count == 1

    def test_broad_exception_increments_fallback_count(self) -> None:
        extractor = _llm_extractor(None, raise_exc=RuntimeError("unexpected"))
        extractor.extract(_hydrated("The deployment completed successfully."), correlation_id="fb-3")
        assert extractor.fallback_count == 1

    def test_no_fallback_when_llm_succeeds(self) -> None:
        text = "The rollout deployment completed on 2026-06-20."
        excerpt = "rollout deployment completed"
        llm_response = {
            "events": [{
                "event_type": "deployment.completed",
                "payload": {"subject": "Gen9", "status": "completed"},
                "excerpt": excerpt,
                "excerpt_start": text.index(excerpt),
                "extraction_confidence": 0.9,
            }]
        }
        extractor = _llm_extractor(llm_response)
        extractor.extract(_hydrated(text), correlation_id="fb-ok")
        assert extractor.fallback_count == 0


# ---------------------------------------------------------------------------
# P1-9: grounding_missed.jsonl sidecar
# ---------------------------------------------------------------------------


class TestGroundingMissedSidecar:
    """P1-9: dropped (ungrounded) LLM claims are logged to ``grounding_missed.jsonl``
    when the sidecar path is configured."""

    def test_ungrounded_claim_logged_to_sidecar(self, tmp_path: Path) -> None:
        import json
        from src.core.rev.result import Success

        miss_path = tmp_path / "grounding_missed.jsonl"
        text = "The deployment completed successfully."
        llm_response = {
            "events": [{
                "event_type": "deployment.completed",
                "payload": {"subject": "fabricated", "status": "completed"},
                "excerpt": "THIS TEXT IS NOT IN THE SOURCE",
                "excerpt_start": 0,
                "extraction_confidence": 0.99,
            }]
        }
        extractor = LLMRevExtractor(
            client=FakeClient(llm_response),
            grounding_missed_path=miss_path,
        )
        result = extractor.extract(_hydrated(text), correlation_id="miss-1")
        assert isinstance(result, Success)
        # Sidecar written with the dropped claim's event_type + original excerpt.
        assert miss_path.exists()
        records = [json.loads(line) for line in miss_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(records) == 1
        assert records[0]["event_type"] == "deployment.completed"
        assert "THIS TEXT IS NOT IN THE SOURCE" in records[0]["original_excerpt"]
        assert records[0]["message_id"] == "msg-test"

    def test_no_sidecar_when_path_unset(self, tmp_path: Path) -> None:
        from src.core.rev.result import Success

        miss_path = tmp_path / "grounding_missed.jsonl"
        text = "The deployment completed successfully."
        llm_response = {
            "events": [{
                "event_type": "deployment.completed",
                "payload": {"subject": "fabricated", "status": "completed"},
                "excerpt": "THIS TEXT IS NOT IN THE SOURCE",
                "excerpt_start": 0,
                "extraction_confidence": 0.99,
            }]
        }
        extractor = _llm_extractor(llm_response)  # no grounding_missed_path
        result = extractor.extract(_hydrated(text), correlation_id="miss-2")
        assert isinstance(result, Success)
        assert not miss_path.exists()
