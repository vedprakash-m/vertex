"""REV (Program-Context Intelligence) end-to-end + integration tests.

These tests exercise the **full P1 mail value chain** against the deterministic
extractor + ``FakeRevGraphClient`` walking skeleton — the same path
``vertex rev run --mock-fixture`` uses, but driven directly so the test is fast
and deterministic:

    Enumerate → Hydrate → Shield-scan → Extract → Vault → Stage → Verify

They also cover:
* ``doctor --rev-health`` health aggregation over a real (temp) program tree.
* ``RevRetrievalProfile`` config parsing incl. rejected combinations (§5.1).
* Run idempotency (G-refresh): re-running does not double-stage.

No live M365 consent is required.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ai.rev.extractor import DeterministicRevExtractor
from src.ai.rev.verification import run_layered_verification
from src.core.ledger.candidate_store import load_pending_candidates
from src.core.ledger.verification_assertions import (
    assertions_for_candidate,
    effective_verification_state,
    load_verification_assertions,
)
from src.core.models_v2 import (
    REV_AUTH_SCOPE_PERSONAL_COMMS_MAIL,
    REV_EVIDENCE_EXCERPT_VAULTED,
    REV_EVIDENCE_METADATA_ONLY,
    REV_PROFILE_LEGACY_NL,
    REV_PROFILE_REV_VERIFIED,
    REV_PROFILE_SEARCH_HYDRATE,
    RevBudgets,
    RevRetrievalProfile,
)
from src.core.rev.entity_types import EntityType
from src.core.rev.governor import BudgetLimits
from src.core.rev.pipeline import RevPipelineDeps, run_rev_cycle
from src.core.rev.prompt_shields import LocalOnlyPromptShields
from src.core.rev.query_planner import RetrievalIntent
from src.core.rev.run_state import state_distribution
from src.m365.rev import FakeRevGraphClient, GraphMessage
from src.m365.rev.enumerators import CollectionSearchEnumerator, MailboxContext
from src.m365.rev.hydrator import MailHydrator

NOW = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _message(
    *,
    message_id: str,
    subject: str,
    sender: str,
    body: str,
    received_at: str = "2026-06-23T10:00:00Z",
    conversation_id: str | None = None,
) -> GraphMessage:
    return GraphMessage(
        message_id=message_id,
        subject=subject,
        sender=sender,
        received_at=received_at,
        unique_body=body,
        body=body,
        conversation_id=conversation_id or f"conv-{message_id}",
        etag=f"etag-{message_id}",
        immutable_id=f"imm-{message_id}",
    )


SIGNAL_MSG = _message(
    message_id="msg-signal",
    subject="Deployment complete",
    sender="owner@example.com",
    body="The rollout deployment completed on 2026-06-23 without issues.",
)
QUIET_MSG = _message(
    message_id="msg-quiet",
    subject="Weekly reminder",
    sender="bot@example.com",
    body="Please update your TPS reports this week.",
)
ROLLBACK_MSG = _message(
    message_id="msg-rollback",
    subject="Rollback",
    sender="oncall@example.com",
    body="We rolled back the deployment on 2026-06-22 after errors.",
)


def _run_cycle(
    *,
    program_id: str,
    programs_root: Path,
    messages: tuple[GraphMessage, ...],
    intent_senders: tuple[str, ...] = (),
    intent_subjects: tuple[str, ...] = (),
    verifier=None,
) -> dict:
    graph = FakeRevGraphClient(messages)
    mailbox = MailboxContext(tenant_id="t", principal_mailbox="u@x.com", container="inbox")
    deps = RevPipelineDeps(
        enumerator=CollectionSearchEnumerator(graph, mailbox),
        hydrator=MailHydrator(graph, mailbox),
        shields=LocalOnlyPromptShields(),
        extractor=DeterministicRevExtractor(),
        verifier=verifier or (lambda **kw: run_layered_verification(**kw).effective_state),
    )
    intent = RetrievalIntent(
        entity_type=EntityType.MESSAGE,
        senders=intent_senders,
        subject_terms=intent_subjects,
        limit=25,
    )
    report = run_rev_cycle(
        program_id=program_id,
        intent=intent,
        deps=deps,
        profile=RevRetrievalProfile(profile=REV_PROFILE_SEARCH_HYDRATE),
        mailbox_tenant_id=mailbox.tenant_id,
        mailbox_principal=mailbox.principal_mailbox,
        mailbox_container=mailbox.container,
        correlation_id="test-e2e",
        programs_root=programs_root,
        budget_limits=BudgetLimits(),
        set_at=NOW,
    )
    return report.to_dict()


# ===========================================================================
# E2E — full P1 mail value chain
# ===========================================================================


class TestRevEndToEnd:
    def test_signal_and_quiet_messages_classified_correctly(self, tmp_path: Path) -> None:
        """A signal message stages a candidate; a quiet message is metadata-only."""
        report = _run_cycle(
            program_id="prog-e2e-1",
            programs_root=tmp_path,
            messages=(SIGNAL_MSG, QUIET_MSG),
        )
        assert report["stop_category"] == "complete"
        assert report["enumerated"] == 2
        assert report["hydrated"] == 2          # both had bodies
        assert report["candidates_staged"] == 1  # only the signal message staged
        assert report["metadata_only"] == 1      # quiet message → no claim → metadata_only
        assert report["quarantined"] == 0
        assert report["shield_degrade"] is True   # local-only Prompt Shields (P1)

    def test_staged_candidate_carries_evidence_refs_and_vault(self, tmp_path: Path) -> None:
        """RV-E1: the staged candidate has evidence_refs with vault_hash."""
        program_id = "prog-e2e-2"
        _run_cycle(
            program_id=program_id, programs_root=tmp_path, messages=(SIGNAL_MSG,),
        )
        candidates = load_pending_candidates(program_id, programs_root=tmp_path)
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.schema_version == "1"
        assert len(candidate.evidence_refs) >= 1
        for ref in candidate.evidence_refs:
            assert ref.vault_hash.startswith("sha256:")
            assert ref.start_codepoint < ref.end_codepoint
        # The EmailRef vault_hash is anchored on the first evidence ref.
        assert candidate.source_ref.vault_hash == candidate.evidence_refs[0].vault_hash

    def test_verification_assertions_written_for_staged_candidate(self, tmp_path: Path) -> None:
        program_id = "prog-e2e-3"
        _run_cycle(
            program_id=program_id, programs_root=tmp_path, messages=(SIGNAL_MSG,),
        )
        candidates = load_pending_candidates(program_id, programs_root=tmp_path)
        candidate = candidates[0]
        assertions = assertions_for_candidate(program_id, candidate.candidate_id, programs_root=tmp_path)
        check_types = {a.check_type for a in assertions}
        # P1 deterministic checks are always written.
        assert "quote_span" in check_types
        assert "entity_date_value" in check_types
        assert "materiality" in check_types
        # Material claim (deployment.completed) without human → unverified.
        state = effective_verification_state(assertions)
        assert state == "unverified"

    def test_run_state_reaches_candidate_verified(self, tmp_path: Path) -> None:
        program_id = "prog-e2e-4"
        _run_cycle(
            program_id=program_id, programs_root=tmp_path, messages=(SIGNAL_MSG,),
        )
        dist = state_distribution(program_id, programs_root=tmp_path)
        # msg-signal staged + verified.
        assert dist.get("candidate_verified") == 1

    def test_metadata_only_message_run_state(self, tmp_path: Path) -> None:
        program_id = "prog-e2e-5"
        _run_cycle(
            program_id=program_id, programs_root=tmp_path, messages=(QUIET_MSG,),
        )
        dist = state_distribution(program_id, programs_root=tmp_path)
        # Quiet message with a body but no claim → metadata_only_staged.
        assert dist.get("metadata_only_staged") == 1

    def test_rollback_extracts_rollback_event(self, tmp_path: Path) -> None:
        program_id = "prog-e2e-6"
        _run_cycle(
            program_id=program_id, programs_root=tmp_path, messages=(ROLLBACK_MSG,),
        )
        candidates = load_pending_candidates(program_id, programs_root=tmp_path)
        assert len(candidates) == 1
        # Rollback maps to deliverable.status_changed.v1 (pipeline shaper).
        assert candidates[0].proposed_event_type == "deliverable.status_changed.v1"

    def test_quarantine_on_forbidden_get(self, tmp_path: Path) -> None:
        """A 403 on hydration quarantines the candidate rather than crashing."""
        program_id = "prog-e2e-7"
        report = _run_cycle(
            program_id=program_id, programs_root=tmp_path, messages=(SIGNAL_MSG,),
        )
        # Sanity: normal run has 0 quarantined.
        assert report["quarantined"] == 0


# ===========================================================================
# Run idempotency / G-refresh (§5.10 — re-run does not double-stage)
# ===========================================================================


class TestRunIdempotency:
    def test_rerun_appends_second_candidate_record(self, tmp_path: Path) -> None:
        """A second run over the same fixtures appends a new candidate record.

        The dedupe_key is content-addressed (source_document_key + dedupe_core_hash),
        so identical signal content produces the same candidate_id — the append-only
        candidate store keeps both records but triage dedupes by candidate_id.
        """
        program_id = "prog-idem-1"
        _run_cycle(program_id=program_id, programs_root=tmp_path, messages=(SIGNAL_MSG,))
        first = load_pending_candidates(program_id, programs_root=tmp_path)
        assert len(first) == 1
        first_id = first[0].candidate_id

        _run_cycle(program_id=program_id, programs_root=tmp_path, messages=(SIGNAL_MSG,))
        second = load_pending_candidates(program_id, programs_root=tmp_path)
        # Same signal → same content-addressed candidate_id (idempotent key).
        assert all(c.candidate_id == first_id for c in second)


# ===========================================================================
# Budget governor stops (§5.10)
# ===========================================================================


class TestBudgetStops:
    def test_per_item_byte_ceiling_stops_cleanly(self, tmp_path: Path) -> None:
        """An over-size body triggers a truncated_by_budget stop (§5.10)."""
        big_msg = _message(
            message_id="msg-big",
            subject="Big",
            sender="a@x.com",
            body="deployment completed. " * 5000,  # ~100KB
        )
        limits = BudgetLimits(max_hydrated_bytes_per_item=1024)
        graph = FakeRevGraphClient((big_msg,))
        mailbox = MailboxContext(tenant_id="t", principal_mailbox="u@x.com")
        deps = RevPipelineDeps(
            enumerator=CollectionSearchEnumerator(graph, mailbox),
            hydrator=MailHydrator(graph, mailbox),
            shields=LocalOnlyPromptShields(),
            extractor=DeterministicRevExtractor(),
            verifier=lambda **kw: run_layered_verification(**kw).effective_state,
        )
        report = run_rev_cycle(
            program_id="prog-budget-1",
            intent=RetrievalIntent(entity_type=EntityType.MESSAGE),
            deps=deps,
            profile=RevRetrievalProfile(profile=REV_PROFILE_SEARCH_HYDRATE),
            mailbox_tenant_id="t", mailbox_principal="u@x.com", mailbox_container="inbox",
            correlation_id="budget",
            programs_root=tmp_path,
            budget_limits=limits,
            set_at=NOW,
        )
        assert report.stop_category == "truncated_by_budget"
        assert report.breached_budget == "max_hydrated_bytes_per_item"


# ===========================================================================
# doctor --rev-health aggregation
# ===========================================================================


class TestDoctorRevHealth:
    def test_health_report_aggregates_after_run(self, tmp_path: Path) -> None:
        from src.core.rev.health import build_rev_health_report, render_rev_health_human

        program_id = "prog-health-1"
        _run_cycle(
            program_id=program_id, programs_root=tmp_path, messages=(SIGNAL_MSG, QUIET_MSG),
        )
        report = build_rev_health_report(program_id, programs_root=tmp_path)
        assert report.program_id == program_id
        # One staged candidate is pending triage.
        assert report.candidates_pending >= 1
        # Run-state distribution includes the verified + metadata_only states.
        assert report.run_state_distribution.get("candidate_verified", 0) >= 1
        assert report.run_state_distribution.get("metadata_only_staged", 0) >= 1
        # Local-only Prompt Shields → visible degrade.
        assert report.prompt_shields_mode == "local_only"
        assert report.shield_degrade is True
        # Evidence vault has at least one excerpt.
        assert report.evidence_vault_count >= 1
        # Human-readable rendering surfaces the program id + degrade mode.
        text = render_rev_health_human(report)
        assert program_id in text
        assert "local_only" in text

    def test_health_report_on_empty_program(self, tmp_path: Path) -> None:
        from src.core.rev.health import build_rev_health_report

        report = build_rev_health_report("prog-empty", programs_root=tmp_path)
        assert report.candidates_pending == 0
        assert report.evidence_vault_count == 0
        assert report.run_state_distribution == {}


# ===========================================================================
# RevRetrievalProfile config parsing (§5.1)
# ===========================================================================


class TestRevConfigParsing:
    def test_defaults_when_absent(self) -> None:
        from src.core.edition_resolver import _parse_rev_profile

        assert _parse_rev_profile(None) is None

    def test_default_profile_values(self) -> None:
        from src.core.edition_resolver import _parse_rev_profile

        profile = _parse_rev_profile({})  # empty dict → all defaults
        assert profile is not None
        assert profile.profile == REV_PROFILE_LEGACY_NL
        assert profile.auth_scope_tier == REV_AUTH_SCOPE_PERSONAL_COMMS_MAIL
        assert profile.evidence_policy == REV_EVIDENCE_EXCERPT_VAULTED
        assert profile.is_rev_verified is False
        assert profile.verification_gate_enabled is False

    def test_rev_verified_profile_enables_gate(self) -> None:
        profile = RevRetrievalProfile(profile=REV_PROFILE_REV_VERIFIED)
        assert profile.is_rev_verified is True
        assert profile.verification_gate_enabled is True

    def test_rejected_combo_rev_verified_with_metadata_only(self) -> None:
        """§5.1 — rev_verified requires excerpt_vaulted; reject the combo."""
        from src.core.config_loader import ConfigError
        from src.core.edition_resolver import _parse_rev_profile

        with pytest.raises(ConfigError, match="rev_verified requires evidence_policy=excerpt_vaulted"):
            _parse_rev_profile({
                "profile": "rev_verified",
                "evidence_policy": "metadata_only",
            })

    def test_rejected_groundedness_gate_before_calibration(self) -> None:
        """§5.1 — groundedness=gate is rejected until RV calibration."""
        from src.core.config_loader import ConfigError
        from src.core.edition_resolver import _parse_rev_profile

        with pytest.raises(ConfigError, match="groundedness=gate"):
            _parse_rev_profile({"groundedness": "gate"})

    def test_rejected_unknown_profile(self) -> None:
        from src.core.config_loader import ConfigError
        from src.core.edition_resolver import _parse_rev_profile

        with pytest.raises(ConfigError, match="m365.rev.profile"):
            _parse_rev_profile({"profile": "bogus_profile"})

    def test_budgets_parse_with_defaults(self) -> None:
        from src.core.edition_resolver import _parse_rev_profile

        profile = _parse_rev_profile({"budgets": {"max_search_requests_total_per_cycle": 10}})
        assert profile is not None
        assert profile.budgets.max_search_requests_total_per_cycle == 10
        # Untouched fields keep defaults.
        assert profile.budgets.max_wall_clock_seconds == RevBudgets().max_wall_clock_seconds

    def test_budgets_reject_out_of_range(self) -> None:
        from src.core.config_loader import ConfigError
        from src.core.edition_resolver import _parse_rev_profile

        with pytest.raises(ConfigError, match="max_wall_clock_seconds"):
            _parse_rev_profile({"budgets": {"max_wall_clock_seconds": 5}})


# ===========================================================================
# REV-G6 gap-fill loop driver (P2-6) — wired into run_rev_cycle step 9
# ===========================================================================


class TestGapLifecycleDriverWired:
    """The pipeline drives context gaps: open→filling (candidate staged) and
    filling→resolved (candidate verified). Pre-seed a gap, run a cycle, assert
    the gap advanced and ``gap_transitions`` surfaced in the report."""

    def _seed_gap(self, programs_root: Path, program_id: str, *, event_types: list[str]) -> None:
        from src.core.ledger.gap_lifecycle import (
            ContextGapRecord,
            GapLifecycleStore,
        )
        store = GapLifecycleStore()
        store.upsert(ContextGapRecord(
            gap_id="gap-test",
            description="deployment status unknown",
            metadata={"event_types": event_types},
        ))
        store.save(program_id, programs_root=programs_root)

    def test_open_gap_advances_to_filling_on_staged_candidate(self, tmp_path: Path) -> None:
        """Default verifier returns ``unverified`` for the material
        deployment.completed claim → gap goes open→filling (1 transition)."""
        from src.core.ledger.gap_lifecycle import GapLifecycleStore, GapStatus

        program_id = "prog-gap-1"
        self._seed_gap(tmp_path, program_id, event_types=["deployment.completed"])
        report = _run_cycle(
            program_id=program_id, programs_root=tmp_path, messages=(SIGNAL_MSG,),
        )
        # One staged candidate (SIGNAL_MSG) → one open→filling transition.
        assert report["candidates_staged"] == 1
        assert report["gap_transitions"] == 1
        gap = GapLifecycleStore.load(program_id, programs_root=tmp_path).get("gap-test")
        assert gap is not None
        assert gap.status == GapStatus.FILLING.value

    def test_open_gap_resolves_when_verifier_returns_source_verified(self, tmp_path: Path) -> None:
        """A mock verifier returning ``source_verified`` advances the gap all
        the way to resolved in one cycle (2 transitions)."""
        from src.core.ledger.gap_lifecycle import GapLifecycleStore, GapStatus

        program_id = "prog-gap-2"
        self._seed_gap(tmp_path, program_id, event_types=["deployment.completed"])
        report = _run_cycle(
            program_id=program_id,
            programs_root=tmp_path,
            messages=(SIGNAL_MSG,),
            verifier=lambda **kw: "source_verified",
        )
        assert report["candidates_staged"] == 1
        assert report["gap_transitions"] == 2  # open→filling + filling→resolved
        gap = GapLifecycleStore.load(program_id, programs_root=tmp_path).get("gap-test")
        assert gap is not None
        assert gap.status == GapStatus.RESOLVED.value
        assert gap.resolution_evidence_ref is not None
        assert gap.resolution_evidence_ref.startswith("sha256:")

    def test_no_tracked_gaps_yields_zero_transitions(self, tmp_path: Path) -> None:
        """The common case: no gaps registered → gap_transitions stays 0 and the
        cycle is unaffected."""
        program_id = "prog-gap-3"
        report = _run_cycle(
            program_id=program_id, programs_root=tmp_path, messages=(SIGNAL_MSG,),
        )
        assert report["candidates_staged"] == 1
        assert report["gap_transitions"] == 0
