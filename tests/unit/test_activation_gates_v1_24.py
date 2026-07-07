"""Activation gate wiring tests (activation.md v1.24 — AG-11 / AG-15).

These tests lock in the v1.24 change of heart: the scaffolds that previously
reported PASS while their machinery was defined-but-never-invoked are now
genuinely WIRED into the production candidate→fact path.

AG-15 (§6.14.6) — entity resolution at candidate staging:
  * ``_stage_candidates`` populates ``entity_resolution`` via the program
    ``EntityRegistry`` instead of the empty ``()`` placeholder.
  * A program WITHOUT ``knowledge/entities.yaml`` (current XPF state) yields
    honest all-UNRESOLVED bindings — the S-6 gate convention — never an error.

AG-11 (§6.14.11) — composite privacy gate at the projection chokepoint:
  * ``_projection_privacy_gate`` (ledger.py) re-runs ``run_local_checks`` on the
    payload of an ACCEPTED (``OPERATOR_CONFIRMED``) fact before it is written.
  * A credential hit blocks projection (fail-closed); a clean payload proceeds.
  * PROPOSED facts are not re-gated (the ingest layer owns them; the trust root
    is the operator approval, not the AI extraction).
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ai.rev.extractor import DeterministicRevExtractor
from src.ai.rev.verification import run_layered_verification
from src.core.ledger.candidate_store import active_candidates
from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence
from src.core.ledger.source_refs import EmailRef
from src.core.models_v2 import REV_PROFILE_SEARCH_HYDRATE, RevRetrievalProfile
from src.core.rev.entity_types import EntityType
from src.core.rev.governor import BudgetLimits
from src.core.rev.pipeline import (
    RevPipelineDeps,
    _resolve_candidate_entities,
    run_rev_cycle,
)
from src.core.rev.prompt_shields import LocalOnlyPromptShields
from src.core.rev.query_planner import RetrievalIntent
from src.m365.rev.eml_enumerator import EmlEnumerator
from src.m365.rev.eml_hydrator import EmlHydrator

NOW = datetime(2026, 6, 24, 10, 0, 0, tzinfo=timezone.utc)


def _write_eml(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def _build_deps(eml_inbox: Path) -> RevPipelineDeps:
    return RevPipelineDeps(
        enumerator=EmlEnumerator(
            inbox_root=eml_inbox,
            mailbox_tenant_id="t",
            principal_mailbox="u@x.com",
            container="inbox",
        ),
        hydrator=EmlHydrator(
            mailbox_tenant_id="t",
            principal_mailbox="u@x.com",
            container="inbox",
            attachment_denied_path=eml_inbox / "attachment_denied.jsonl",
        ),
        shields=LocalOnlyPromptShields(),
        extractor=DeterministicRevExtractor(),
        verifier=lambda **kw: run_layered_verification(**kw).effective_state,
    )


# A commitment EML exercises a person-ref payload field (owner_person_id), so the
# entity-resolution gate has a ref slot to resolve. Phrasing matches the
# deterministic extractor's _COMMITMENT_RE (will/plan to/scheduled to ... by/on).
_COMMITMENT_EML = """\
From: lead@example.com
To: tpm@example.com
Subject: Gen9 BIOS deployment schedule
Date: Tue, 24 Jun 2026 10:00:00 +0000
Message-ID: <commit-001@example.com>
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

We are planning to ship the Gen9 BIOS AP deployment by 2026-07-03.
"""


class TestEntityResolutionHelpers:
    """Unit-level coverage of ``_resolve_candidate_entities`` (AG-15)."""

    def test_no_registry_yields_all_unresolved(self) -> None:
        # A program without entities.yaml → registry=None → every ref UNRESOLVED.
        resolutions = _resolve_candidate_entities(
            payload={"owner_person_id": "Alex Chen"},
            event_type="commitment.made.v1",
            registry=None,
        )
        assert len(resolutions) == 1
        assert resolutions[0].match_kind == "unresolved"
        assert resolutions[0].resolved_entity_id is None
        assert resolutions[0].raw_name == "Alex Chen"

    def test_event_type_without_ref_fields_yields_empty(self) -> None:
        # milestone.completed.v1 has no person-ref slots → empty tuple (not an
        # error, not a phantom unresolved entry).
        resolutions = _resolve_candidate_entities(
            payload={"milestone_id": "m1", "completed_on": "2026-07-06"},
            event_type="milestone.completed.v1",
            registry=None,
        )
        assert resolutions == ()

    def test_empty_owner_value_is_skipped(self) -> None:
        resolutions = _resolve_candidate_entities(
            payload={"owner_person_id": "unknown"},
            event_type="commitment.made.v1",
            registry=None,
        )
        assert resolutions == ()

    def test_registry_resolves_known_entity(self, tmp_path: Path) -> None:
        from src.core.entity_registry import EntityRegistry

        # EntityRegistry.load expects programs_root/<program_id>/knowledge/entities.yaml
        (tmp_path / "prog" / "knowledge").mkdir(parents=True)
        (tmp_path / "prog" / "knowledge" / "entities.yaml").write_text(
            textwrap.dedent(
                """
                entities:
                  - entity_id: annotator1
                    entity_type: person
                    canonical_name: Alex Chen
                    aliases:
                      - Ved
                """
            ),
            encoding="utf-8",
        )
        registry = EntityRegistry.load("prog", programs_root=tmp_path)
        resolutions = _resolve_candidate_entities(
            payload={"owner_person_id": "Alex Chen"},
            event_type="commitment.made.v1",
            registry=registry,
        )
        assert len(resolutions) == 1
        assert resolutions[0].match_kind == "resolved"
        assert resolutions[0].resolved_entity_id == "annotator1"


class TestEntityResolutionWiredIntoCycle:
    """End-to-end: a real REV cycle populates entity_resolution (AG-15)."""

    def test_cycle_populates_entity_resolution_even_when_unresolved(
        self, tmp_path: Path
    ) -> None:
        eml_inbox = tmp_path / "rev_inbox"
        eml_inbox.mkdir()
        _write_eml(eml_inbox / "commit.eml", _COMMITMENT_EML)
        deps = _build_deps(eml_inbox)

        report = run_rev_cycle(
            program_id="prog-ent",
            intent=RetrievalIntent(entity_type=EntityType.MESSAGE, limit=25),
            deps=deps,
            profile=RevRetrievalProfile(profile=REV_PROFILE_SEARCH_HYDRATE),
            mailbox_tenant_id="t",
            mailbox_principal="u@x.com",
            mailbox_container="inbox",
            correlation_id="ent-res",
            programs_root=tmp_path,
            budget_limits=BudgetLimits(),
            set_at=NOW,
        )

        assert report.candidates_staged >= 1
        candidates = active_candidates("prog-ent", programs_root=tmp_path)
        # Find the commitment candidate (it carries the owner_person_id ref slot).
        commitment = next(
            (c for c in candidates if c.proposed_event_type == "commitment.made.v1"),
            None,
        )
        assert commitment is not None, "commitment candidate must be staged"
        # AG-15: entity_resolution is now POPULATED (not the empty () placeholder).
        # With no entities.yaml the binding is UNRESOLVED — honest, not silent.
        assert len(commitment.entity_resolution) >= 1
        assert all(r.match_kind == "unresolved" for r in commitment.entity_resolution)


class TestProjectionPrivacyGate:
    """AG-11: the composite privacy gate at the bridge chokepoint."""

    def _envelope(self, payload: dict, confidence: ConfidenceTier) -> EventEnvelope:
        return EventEnvelope(
            event_id="evt-1",
            program_id="xpf",
            event_type="milestone.completed.v1",
            occurred_at=NOW,
            recorded_at=NOW,
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=confidence,
            actor="operator@example.com",
            payload=payload,
            source_ref=EmailRef(
                subject="s",
                sent_at=NOW,
                sender="a@b.com",
                message_id="m",
                folder="inbox",
            ),
        )

    def test_clean_accepted_fact_proceeds(self) -> None:
        from src.commands.ledger import _projection_privacy_gate

        verdict = _projection_privacy_gate(
            self._envelope(
                {"milestone_id": "m1", "completed_on": "2026-07-06", "evidence": "shipped on time"},
                ConfidenceTier.OPERATOR_CONFIRMED,
            )
        )
        assert verdict is True

    def test_credential_in_accepted_fact_blocks(self) -> None:
        from src.commands.ledger import _projection_privacy_gate

        verdict = _projection_privacy_gate(
            self._envelope(
                # An AWS-looking secret key in the payload — a credential hit.
                {"evidence": "creds: AKIAIOSFODNN7EXAMPLE/wJalrXUtnFEMI/K7MDENG/bPxRfiCY"},
                ConfidenceTier.OPERATOR_CONFIRMED,
            )
        )
        assert verdict is False, "credential hit on an ACCEPTED fact must block projection"

    def test_credential_in_proposed_fact_passes(self) -> None:
        # PROPOSED (AI_EXTRACTED) facts are not the trust root — the ingest gate
        # owns them. The projection gate scopes to OPERATOR_CONFIRMED only.
        from src.commands.ledger import _projection_privacy_gate

        verdict = _projection_privacy_gate(
            self._envelope(
                {"evidence": "creds: AKIAIOSFODNN7EXAMPLE/secret"},
                ConfidenceTier.AI_EXTRACTED,
            )
        )
        assert verdict is True


class TestCrossSourceConflictCheck:
    """AG-9 / §6.14.5: conflict detection wired into the REV cycle finalize path.

    The detector is now *invoked* on the production path (``_finalize_report`` →
    ``_run_cross_source_conflict_check``). It runs over the fact-store snapshot
    and writes ``fact.conflict``/``fact.corroboration`` when an EML-derived
    observation materially contradicts the authoritative state. These tests lock
    the contract: (1) it writes a conflict for a real disagreement, (2) it is
    honest (no false conflict when sources agree or entity keys differ), and
    (3) it is best-effort (never raises even if the store is empty/broken).
    """

    def test_conflict_written_when_eml_contradicts_authoritative_state(
        self, tmp_path: Path
    ) -> None:
        from src.core.rev.pipeline import _run_cross_source_conflict_check

        # programs_root = tmp/programs → the helper opens db_root=tmp.parent
        # which is tmp; the store lives at tmp/<program_id>/. Seed at db_root=tmp.
        programs_root = tmp_path / "programs"
        programs_root.mkdir()
        store = self._store_with_two_milestone_observations(
            tmp_path, program_id="prog-conflict",
            ado_status="completed", eml_status="missed",
        )
        summary = _run_cross_source_conflict_check(
            "prog-conflict", "corr-1", programs_root
        )
        # A real disagreement across provenance classes → ≥1 conflict fact written.
        assert summary["conflicts"] >= 1, summary
        # The written fact.conflict is retrievable and carries the §6.14.5 as_of.
        snap = store.snapshot()
        conflicts = [f for f in snap.facts if f.fact_type == "fact.conflict"]
        assert conflicts, "fact.conflict must be persisted"
        assert "counter_source_as_of" in conflicts[0].payload
        # Schema-required fields present (fact_schema_registry).
        assert "day_bucket" in conflicts[0].payload
        assert "conflicting_signal_ids" in conflicts[0].payload

    def test_no_false_conflict_when_sources_agree(self, tmp_path: Path) -> None:
        from src.core.rev.pipeline import _run_cross_source_conflict_check

        programs_root = tmp_path / "programs"
        programs_root.mkdir()
        self._store_with_two_milestone_observations(
            tmp_path, program_id="prog-agree",
            ado_status="completed", eml_status="completed",
        )
        summary = _run_cross_source_conflict_check(
            "prog-agree", "corr-2", programs_root
        )
        assert summary["conflicts"] == 0, summary

    def test_never_raises_on_empty_store(self, tmp_path: Path) -> None:
        from src.core.rev.pipeline import _run_cross_source_conflict_check

        programs_root = tmp_path / "programs"
        programs_root.mkdir()
        # No facts at all → best-effort returns zeros, never raises.
        summary = _run_cross_source_conflict_check(
            "prog-empty", "corr-3", programs_root
        )
        assert summary["conflicts"] == 0
        assert summary["observations"] == 0

    @staticmethod
    def _store_with_two_milestone_observations(
        db_root: Path, *, program_id: str, ado_status: str, eml_status: str
    ):
        """Seed a fact store with two milestone.entry observations sharing an
        entity_id but differing in source/precedence (distinct natural keys so
        both survive as distinct revisions)."""
        from src.core.program_fact_store import (
            FactLifecycleState,
            FactPrecedence,
            FactReviewState,
            ProgramFactInput,
            ProgramFactStore,
        )

        store = ProgramFactStore(
            program_id, home_root=None, db_root=db_root
        )
        now = datetime.now(timezone.utc)
        # Authoritative (ADO/PM) milestone — distinct entity_refs suffix so its
        # natural_key differs from the EML one and both survive in the snapshot.
        store.append_fact(
            ProgramFactInput(
                fact_type="milestone.entry",
                entity_refs=("MILESTONE:m1", "SOURCE:ado"),
                payload={"id": "m1", "status": ado_status, "source": "ado"},
                scope="program",
                precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
                review_state=FactReviewState.ACCEPTED,
                lifecycle_state=FactLifecycleState.ACTIVE,
                created_by="test",
            ),
            recorded_at=now,
        )
        store.append_fact(
            ProgramFactInput(
                fact_type="milestone.entry",
                entity_refs=("MILESTONE:m1", "SOURCE:teams"),
                payload={"id": "m1", "status": eml_status, "source": "teams"},
                scope="program",
                precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
                review_state=FactReviewState.ACCEPTED,
                lifecycle_state=FactLifecycleState.ACTIVE,
                created_by="test",
            ),
            recorded_at=now,
        )
        return store


