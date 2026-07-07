"""REV (Program-Context Intelligence) acceptance-gate + architecture contracts.

These tests enforce the load-bearing invariants from
``specs/program-context-intelligence.md`` §5.7/§5.9/§5.10 and the acceptance
gates in §7:

* **RV-E1** — no M365-sourced ``CandidateEvent`` persists without
  ``evidence_refs`` + ``vault_hash`` on each ref (QG-DM-8). Staging a candidate
  with empty ``evidence_refs`` must be rejected at the source-ref/vault level.
* **RV-VP1** — verification-at-intake: under the ``rev_verified`` profile,
  ``triage approve``/``triage edit`` reject an unverified candidate (exit 7);
  the gate is a no-op under ``legacy_nl``/no-config (backward compatible).
* **RV-V1** — the quote/span verifier blocks a seeded fabricated fact
  (``canonical_text[start:end] != excerpt_text`` ⇒ quote_span FAIL).
* Append-only ``VerificationAssertion`` ledger: effective state is *derived*
  (QG-DM-2); legacy migration tags old candidates ``legacy_unverified``.
* Zone boundary: Zone A (``src/core/rev``) imports neither ``src.m365`` nor
  ``src.ai`` nor ``src.commands`` (INV-1).

The tests use the deterministic extractor + ``FakeRevGraphClient`` walking
skeleton so they run with no live M365 consent.
"""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ai.rev.extractor import (
    DETERMINISTIC_MODEL,
    DeterministicRevExtractor,
    EvidenceSpan,
    ExtractedClaim,
    EXTRACTION_SCHEMA_VERSION,
)
from src.ai.rev.verification import check_quote_span, run_layered_verification
from src.core.jsonl_utils import read_jsonl_records
from src.core.ledger.candidate_store import (
    CandidateEvent,
    append_candidate,
    load_pending_candidates,
)
from src.core.ledger.rev_evidence import (
    REV_EVIDENCE_METADATA_SCHEMA_VERSION,
    EvidenceRef,
    build_metadata_defaults,
    evidence_refs_from_dict,
    evidence_refs_to_dict,
    load_rev_evidence_metadata,
    store_admitted_excerpt,
)
from src.core.ledger.source_refs import EmailRef, source_document_key, validate_typed_source_ref
from src.core.ledger.verification_assertions import (
    CHECK_ENTITY_DATE_VALUE,
    CHECK_HUMAN,
    CHECK_MATERIALITY,
    CHECK_QUOTE_SPAN,
    LEGACY_POLICY_VERSION,
    STATUS_ADVISORY,
    STATUS_DEFERRED,
    STATUS_FAIL,
    STATUS_PASS,
    VERIFIED_STATES,
    VerificationAssertion,
    append_verification_assertion,
    assertions_for_candidate,
    effective_verification_state,
    human_pass_assertion,
    is_candidate_verified,
    legacy_assertion,
    load_verification_assertions,
    STATE_HUMAN_VERIFIED,
    STATE_LEGACY_UNVERIFIED,
    STATE_SOURCE_VERIFIED,
    STATE_UNVERIFIED,
)
from src.core.rev.entity_types import EntityType
from src.core.rev.governor import BudgetLimits, Governor
from src.core.rev.identity import (
    CanonicalItemIdentity,
    HydrationLocator,
    IdentityResolutionError,
    ItemToRouteBinder,
    RouteMetadata,
)
from src.core.rev.normalizer import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    NORMALIZATION_VERSION,
    chunk_canonical,
    dedupe_core_hash_for,
    merge_chunk_evidence,
    normalize,
)
from src.core.rev.ports import Chunk, EnumeratedCandidate, HydratedContent
from src.core.rev.privacy import (
    CredentialFinding,
    LocalCheckResult,
    run_local_checks,
    scrub_pii,
    scrubber_version,
)
from src.core.rev.query_planner import (
    CapabilityTable,
    EventQueryCompiler,
    MessageQueryCompiler,
    QueryCompileError,
    RetrievalIntent,
    SharePointQueryCompiler,
    TeamsQueryCompiler,
    compiler_for,
)
from src.core.rev.result import (
    Forbidden,
    Incomplete,
    PortResult,
    RateLimited,
    Success,
    Unsupported,
    is_success,
    outcome_category,
)
from src.core.rev.run_state import (
    DURABLE_STATES,
    EPHEMERAL_STATES,
    TERMINAL_STATES,
    RunState,
    advance,
    crash_revert,
    current_state_by_candidate,
    is_ephemeral,
    state_distribution,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REV_ZONE_A = REPO_ROOT / "src" / "core" / "rev"
REV_LEDGER = REPO_ROOT / "src" / "core" / "ledger"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)


def _email_ref(vault_hash: str, message_id: str = "msg-1") -> EmailRef:
    return EmailRef(
        subject="Deployment complete",
        sent_at=NOW,
        sender="owner@example.com",
        message_id=message_id,
        vault_hash=vault_hash,
    )


def _staged_candidate(
    *,
    program_id: str,
    programs_root: Path,
    evidence_refs: tuple[EvidenceRef, ...],
    vault_hash: str = "sha256:evidence-1",
) -> CandidateEvent:
    source_ref = _email_ref(vault_hash)
    candidate = CandidateEvent(
        candidate_id="cand-1",
        program_id=program_id,
        proposed_event_type="milestone.completed.v1",
        proposed_payload={"milestone_id": "m1", "completed_on": "2026-06-23", "evidence": "deployed"},
        proposed_occurred_at=NOW,
        proposed_temporal_confidence="exact",
        proposed_confidence="medium",
        source_ref=source_ref,
        pipeline="rev_mail",
        extraction_confidence=0.8,
        entity_resolution=(),
        dedupe_key="cand-1",
        dedupe_core_hash="sha256:core",
        source_document_key=source_document_key(source_ref),
        corroborating_refs=(),
        batch_id="rev:test",
        staged_at=NOW,
        schema_version="1",
        evidence_refs=evidence_refs,
    )
    append_candidate(candidate, programs_root=programs_root)
    return candidate


# ===========================================================================
# RV-E1 — no M365 candidate persists without evidence_refs + vault_hash
# ===========================================================================


class TestRVE1EvidenceRequired:
    """RV-E1: an external-origin EmailRef without vault_hash is rejected, and a
    REV candidate carries evidence_refs populated from the vault."""

    def test_email_ref_without_vault_hash_rejected(self) -> None:
        """QG-DM-8 — EmailRef is in the mandatory vault set."""
        ref = EmailRef(
            subject="x", sent_at=NOW, sender="a@example.com", message_id="m",
        )
        assert ref.vault_hash is None
        with pytest.raises(ValueError, match="vault_hash"):
            validate_typed_source_ref(ref)

    def test_email_ref_with_vault_hash_accepted(self) -> None:
        ref = _email_ref("sha256:evidence-1")
        validate_typed_source_ref(ref)  # no raise

    def test_candidate_round_trip_preserves_evidence_refs(self, tmp_path: Path) -> None:
        program_id = "test-rev-e1-rt"
        ref = EvidenceRef(
            vault_hash="sha256:evidence-1",
            representation_version=NORMALIZATION_VERSION,
            start_codepoint=0,
            end_codepoint=10,
            excerpt_hash="sha256:excerpt",
            normalized_source_hash="sha256:source",
        )
        candidate = _staged_candidate(
            program_id=program_id, programs_root=tmp_path, evidence_refs=(ref,),
        )
        loaded = load_pending_candidates(program_id, programs_root=tmp_path)
        assert len(loaded) == 1
        assert loaded[0].schema_version == "1"
        assert len(loaded[0].evidence_refs) == 1
        assert loaded[0].evidence_refs[0].vault_hash == "sha256:evidence-1"
        assert loaded[0].evidence_refs[0].representation_version == NORMALIZATION_VERSION

    def test_old_record_without_evidence_refs_parses_empty(self, tmp_path: Path) -> None:
        """Backward-compatible migration: JSONL records without evidence_refs/schema_version
        are imported into SQLite and round-trip correctly via load_pending_candidates."""
        from src.core.ledger.candidate_store import get_pending_path
        from src.core.ledger.source_refs import source_ref_to_dict

        program_id = "test-rev-e1-legacy"
        # Write a legacy JSONL record (no evidence_refs / schema_version) directly —
        # bypassing append_candidate so that the SQLite DB stays absent.
        source_ref = _email_ref("sha256:evidence-1")
        legacy_record = {
            "candidate_id": "cand-legacy-1",
            "program_id": program_id,
            "proposed_event_type": "milestone.completed.v1",
            "proposed_payload": {"milestone_id": "m1", "completed_on": "2026-06-23", "evidence": "deployed"},
            "proposed_occurred_at": NOW.isoformat(),
            "proposed_temporal_confidence": "exact",
            "proposed_confidence": "medium",
            "source_ref": source_ref_to_dict(source_ref),
            "pipeline": "rev_mail",
            "extraction_confidence": 0.8,
            "entity_resolution": [],
            "dedupe_key": "sha256:legacydedupe",
            "dedupe_core_hash": "sha256:core",
            "source_document_key": "email:sha256:legacy:msg-1",
            "corroborating_refs": [],
            "batch_id": "rev:test",
            "staged_at": NOW.isoformat(),
            # Intentionally omitting evidence_refs + schema_version (pre-v2 format).
        }
        pending_path = get_pending_path(program_id, programs_root=tmp_path)
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(json.dumps(legacy_record, sort_keys=True) + "\n", encoding="utf-8")

        # load_pending_candidates auto-migrates JSONL → SQLite on first use.
        loaded = load_pending_candidates(program_id, programs_root=tmp_path)
        assert len(loaded) == 1
        assert loaded[0].evidence_refs == ()
        assert loaded[0].schema_version == "1"


# ===========================================================================
# RV-VP1 — verification-at-intake gate
# ===========================================================================


class TestRVVP1VerificationGate:
    """RV-VP1: triage approve rejects unverified candidates under rev_verified;
    the gate is a no-op under legacy_nl / no REV config (backward compatible)."""

    def test_unverified_candidate_is_not_verified(self, tmp_path: Path) -> None:
        program_id = "test-rev-vp1-unverified"
        _staged_candidate(
            program_id=program_id, programs_root=tmp_path,
            evidence_refs=(EvidenceRef(
                vault_hash="sha256:ev1", representation_version=NORMALIZATION_VERSION,
                start_codepoint=0, end_codepoint=5, excerpt_hash="sha256:x",
                normalized_source_hash="sha256:s",
            ),),
        )
        assert is_candidate_verified(program_id, "cand-1", programs_root=tmp_path) is False
        state = effective_verification_state(
            assertions_for_candidate(program_id, "cand-1", programs_root=tmp_path)
        )
        assert state == STATE_UNVERIFIED

    def test_human_pass_makes_candidate_verified(self, tmp_path: Path) -> None:
        program_id = "test-rev-vp1-human"
        _staged_candidate(
            program_id=program_id, programs_root=tmp_path,
            evidence_refs=(EvidenceRef(
                vault_hash="sha256:ev1", representation_version=NORMALIZATION_VERSION,
                start_codepoint=0, end_codepoint=5, excerpt_hash="sha256:x",
                normalized_source_hash="sha256:s",
            ),),
        )
        append_verification_assertion(
            human_pass_assertion("cand-1", actor="operator", evidence_refs=("sha256:ev1",)),
            program_id=program_id, programs_root=tmp_path,
        )
        assert is_candidate_verified(program_id, "cand-1", programs_root=tmp_path) is True
        state = effective_verification_state(
            assertions_for_candidate(program_id, "cand-1", programs_root=tmp_path)
        )
        assert state == STATE_HUMAN_VERIFIED

    def test_source_verified_for_non_material_claim(self, tmp_path: Path) -> None:
        """Non-material claim reaches source_verified via quote_span + consistency."""
        program_id = "test-rev-vp1-source"
        _staged_candidate(
            program_id=program_id, programs_root=tmp_path,
            evidence_refs=(EvidenceRef(
                vault_hash="sha256:ev1", representation_version=NORMALIZATION_VERSION,
                start_codepoint=0, end_codepoint=5, excerpt_hash="sha256:x",
                normalized_source_hash="sha256:s",
            ),),
        )
        now = datetime.now(timezone.utc)
        for assertion in (
            VerificationAssertion("cand-1", None, CHECK_QUOTE_SPAN, STATUS_PASS, "v1", ("sha256:ev1",), "test", now),
            VerificationAssertion("cand-1", None, CHECK_ENTITY_DATE_VALUE, STATUS_PASS, "v1", ("sha256:ev1",), "test", now),
            # materiality ADVISORY ⇒ non-material (no human required)
            VerificationAssertion("cand-1", None, CHECK_MATERIALITY, STATUS_ADVISORY, "v1", ("sha256:ev1",), "test", now),
        ):
            append_verification_assertion(assertion, program_id=program_id, programs_root=tmp_path)
        state = effective_verification_state(
            assertions_for_candidate(program_id, "cand-1", programs_root=tmp_path)
        )
        assert state == STATE_SOURCE_VERIFIED
        assert is_candidate_verified(program_id, "cand-1", programs_root=tmp_path) is True

    def test_legacy_migration_tags_legacy_unverified(self, tmp_path: Path) -> None:
        program_id = "test-rev-vp1-legacy"
        _staged_candidate(
            program_id=program_id, programs_root=tmp_path, evidence_refs=(),
        )
        append_verification_assertion(
            legacy_assertion("cand-1", set_at=NOW),
            program_id=program_id, programs_root=tmp_path,
        )
        state = effective_verification_state(
            assertions_for_candidate(program_id, "cand-1", programs_root=tmp_path)
        )
        assert state == STATE_LEGACY_UNVERIFIED
        # legacy_unverified is NOT in the verified set → cannot be triaged.
        assert state not in VERIFIED_STATES
        assert is_candidate_verified(program_id, "cand-1", programs_root=tmp_path) is False


# ===========================================================================
# RV-V1 — quote/span verifier blocks seeded fabricated fact
# ===========================================================================


def _hydrated(canonical_text: str, *, source_type: EntityType = EntityType.MESSAGE) -> HydratedContent:
    identity = CanonicalItemIdentity(
        source_type=source_type, tenant_id="t", principal_mailbox="u@x.com",
        container="inbox", resource_id="msg-1",
    )
    chunks = chunk_canonical(canonical_text)
    return HydratedContent(
        identity=identity,
        canonical_text=canonical_text,
        normalized_source_hash="sha256:" + canonical_text.encode("utf-8").hex()[:64],
        chunks=tuple(chunks),
        route_metadata={},
        correlation_id="cid",
    )


class TestRVV1QuoteSpanVerifier:
    """RV-V1: the quote/span verifier rejects a fabricated excerpt."""

    def test_honest_span_passes(self) -> None:
        text = "The deployment completed on 2026-06-23 successfully."
        hydrated = _hydrated(text)
        claim = ExtractedClaim(
            event_type="deployment.completed",
            payload={"status": "completed", "date": "2026-06-23"},
            evidence_spans=(__import__("src.ai.rev.extractor", fromlist=["EvidenceSpan"]).EvidenceSpan(
                chunk_id=hydrated.chunks[0].chunk_id,
                start_codepoint=text.index("deployment completed"),
                end_codepoint=text.index("deployment completed") + len("deployment completed"),
                excerpt_text="deployment completed",
            ),),
            extraction_confidence=0.8,
            extraction_model=DETERMINISTIC_MODEL,
            extraction_schema_version=EXTRACTION_SCHEMA_VERSION,
        )
        assert check_quote_span(claim, hydrated) is True

    def test_fabricated_excerpt_blocked(self) -> None:
        """A span whose excerpt_text != canonical_text[start:end] fails quote_span."""
        text = "The deployment completed on 2026-06-23 successfully."
        hydrated = _hydrated(text)
        fabricated = ExtractedClaim(
            event_type="deployment.completed",
            payload={"status": "completed", "date": "2026-06-23"},
            evidence_spans=(__import__("src.ai.rev.extractor", fromlist=["EvidenceSpan"]).EvidenceSpan(
                chunk_id=hydrated.chunks[0].chunk_id,
                start_codepoint=0,
                end_codepoint=len("deployment completed"),
                # Fabricated excerpt that does NOT match the canonical text.
                excerpt_text="rollback failed catastrophically",
            ),),
            extraction_confidence=0.8,
            extraction_model=DETERMINISTIC_MODEL,
            extraction_schema_version=EXTRACTION_SCHEMA_VERSION,
        )
        assert check_quote_span(fabricated, hydrated) is False

    def test_out_of_bounds_span_blocked(self) -> None:
        text = "short"
        hydrated = _hydrated(text)
        claim = ExtractedClaim(
            event_type="deployment.completed",
            payload={"date": "2026-06-23"},
            evidence_spans=(__import__("src.ai.rev.extractor", fromlist=["EvidenceSpan"]).EvidenceSpan(
                chunk_id="x", start_codepoint=0, end_codepoint=9999, excerpt_text="x",
            ),),
            extraction_confidence=0.5,
            extraction_model=DETERMINISTIC_MODEL,
            extraction_schema_version=EXTRACTION_SCHEMA_VERSION,
        )
        assert check_quote_span(claim, hydrated) is False


# ===========================================================================
# Effective verification state derivation (QG-DM-2)
# ===========================================================================


class TestEffectiveStateDerivation:
    """The effective state is derived from the full assertion set, never stored."""

    def test_empty_assertions_is_unverified(self) -> None:
        assert effective_verification_state(()) == STATE_UNVERIFIED

    def test_quote_span_fail_blocks_source_verified(self) -> None:
        now = datetime.now(timezone.utc)
        assertions = (
            VerificationAssertion("c", None, CHECK_QUOTE_SPAN, STATUS_FAIL, "v1", (), "t", now),
            VerificationAssertion("c", None, CHECK_ENTITY_DATE_VALUE, STATUS_PASS, "v1", (), "t", now),
            VerificationAssertion("c", None, CHECK_MATERIALITY, STATUS_ADVISORY, "v1", (), "t", now),
        )
        assert effective_verification_state(assertions) == STATE_UNVERIFIED

    def test_material_claim_without_human_is_unverified(self) -> None:
        now = datetime.now(timezone.utc)
        assertions = (
            VerificationAssertion("c", None, CHECK_QUOTE_SPAN, STATUS_PASS, "v1", (), "t", now),
            VerificationAssertion("c", None, CHECK_ENTITY_DATE_VALUE, STATUS_PASS, "v1", (), "t", now),
            # materiality PASS ⇒ material ⇒ human required
            VerificationAssertion("c", None, CHECK_MATERIALITY, STATUS_PASS, "v1", (), "t", now),
        )
        assert effective_verification_state(assertions) == STATE_UNVERIFIED

    def test_material_claim_with_human_is_human_verified(self) -> None:
        now = datetime.now(timezone.utc)
        assertions = (
            VerificationAssertion("c", None, CHECK_QUOTE_SPAN, STATUS_PASS, "v1", (), "t", now),
            VerificationAssertion("c", None, CHECK_ENTITY_DATE_VALUE, STATUS_PASS, "v1", (), "t", now),
            VerificationAssertion("c", None, CHECK_MATERIALITY, STATUS_PASS, "v1", (), "t", now),
            VerificationAssertion("c", None, CHECK_HUMAN, STATUS_PASS, "v1", (), "operator", now),
        )
        assert effective_verification_state(assertions) == STATE_HUMAN_VERIFIED


# ===========================================================================
# VerificationAssertion ledger append-only / round-trip
# ===========================================================================


class TestVerificationAssertionLedger:
    def test_assertion_round_trip(self, tmp_path: Path) -> None:
        program_id = "test-rev-va-rt"
        assertion = VerificationAssertion(
            candidate_id="c1", resulting_event_id=None, check_type=CHECK_QUOTE_SPAN,
            status=STATUS_PASS, policy_version="v1", evidence_refs=("sha256:h1",),
            set_by="test", set_at=NOW,
        )
        append_verification_assertion(assertion, program_id=program_id, programs_root=tmp_path)
        loaded = load_verification_assertions(program_id, programs_root=tmp_path)
        assert len(loaded) == 1
        assert loaded[0].candidate_id == "c1"
        assert loaded[0].evidence_refs == ("sha256:h1",)
        assert loaded[0].set_at == NOW

    def test_assertion_state_distribution(self, tmp_path: Path) -> None:
        from src.core.ledger.verification_assertions import assertion_state_distribution

        program_id = "test-rev-va-dist"
        now = datetime.now(timezone.utc)
        # candidate A: human pass → human_verified
        append_verification_assertion(
            VerificationAssertion("A", None, CHECK_HUMAN, STATUS_PASS, "v1", (), "op", now),
            program_id=program_id, programs_root=tmp_path,
        )
        # candidate B: quote_span fail + non-material materiality → unverified
        append_verification_assertion(
            VerificationAssertion("B", None, CHECK_QUOTE_SPAN, STATUS_FAIL, "v1", (), "t", now),
            program_id=program_id, programs_root=tmp_path,
        )
        append_verification_assertion(
            VerificationAssertion("B", None, CHECK_MATERIALITY, STATUS_ADVISORY, "v1", (), "t", now),
            program_id=program_id, programs_root=tmp_path,
        )
        dist = assertion_state_distribution(load_verification_assertions(program_id, programs_root=tmp_path))
        # Only candidates that *have* assertions appear in the distribution.
        assert dist.get(STATE_HUMAN_VERIFIED) == 1
        assert dist.get(STATE_UNVERIFIED) == 1


# ===========================================================================
# Zone boundary contract (INV-1)
# ===========================================================================


_BANNED_ZONE_A_IMPORTS = ("src.m365", "src.ai", "src.commands")


def _module_imports(source: str) -> list[str]:
    tree = ast.parse(source)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
    return imports


class TestZoneBoundary:
    def test_zone_a_rev_does_not_import_upper_zones(self) -> None:
        for path in REV_ZONE_A.glob("*.py"):
            if path.name == "__init__.py":
                continue
            source = path.read_text(encoding="utf-8")
            for module in _module_imports(source):
                for banned in _BANNED_ZONE_A_IMPORTS:
                    assert not module.startswith(banned), (
                        f"Zone-A module {path.name} imports banned {module}"
                    )

    def test_zone_a_ledger_rev_modules_do_not_import_upper_zones(self) -> None:
        for name in ("rev_evidence.py", "verification_assertions.py"):
            source = (REV_LEDGER / name).read_text(encoding="utf-8")
            for module in _module_imports(source):
                for banned in _BANNED_ZONE_A_IMPORTS:
                    assert not module.startswith(banned), (
                        f"ledger module {name} imports banned {module}"
                    )


# ===========================================================================
# Identity resolution (§5.4)
# ===========================================================================


class TestIdentityResolution:
    def test_mail_route_binds_via_conversation_id(self) -> None:
        item = CanonicalItemIdentity(
            source_type=EntityType.MESSAGE, tenant_id="t", principal_mailbox="u@x.com",
            container="inbox", resource_id="msg-1", immutable_id="imm-1",
        )
        route = ItemToRouteBinder().bind(item, RouteMetadata(conversation_id="conv-1"))
        assert route.route_key == "conv-1"
        assert route.is_registry_eligible is True

    def test_calendar_route_uses_series_master_id(self) -> None:
        item = CanonicalItemIdentity(
            source_type=EntityType.EVENT, tenant_id="t", principal_mailbox="u@x.com",
            container="calendar", resource_id="evt-1",
        )
        route = ItemToRouteBinder().bind(item, RouteMetadata(series_master_id="series-1"))
        assert route.route_key == "series-1"

    def test_sharepoint_route_binds_via_site_library(self) -> None:
        """§5.4 + specs/gaps.md G4 — DRIVE_ITEM binds to normalized site+library route key."""
        item = CanonicalItemIdentity(
            source_type=EntityType.DRIVE_ITEM, tenant_id="t", principal_mailbox="",
            container="site/lib", resource_id="item-1",
        )
        route = ItemToRouteBinder().bind(item, RouteMetadata(site_library="https://tenant.sharepoint.com/sites/NOVA/Docs"))
        assert route.source_type is EntityType.DRIVE_ITEM
        assert route.route_key == "https://tenant.sharepoint.com/sites/NOVA/Docs"
        assert route.normalized_id == "/sites/nova/docs"
        assert route.is_registry_eligible is True

    def test_sharepoint_plain_path_route_binds(self) -> None:
        """Plain site+library path (no scheme) normalizes cleanly."""
        item = CanonicalItemIdentity(
            source_type=EntityType.DRIVE_ITEM, tenant_id="t", principal_mailbox="",
            container="site/lib", resource_id="item-2",
        )
        route = ItemToRouteBinder().bind(item, RouteMetadata(site_library="/sites/NOVA/Docs"))
        assert route.normalized_id == "/sites/nova/docs"

    def test_list_item_route_still_unsupported(self) -> None:
        """LIST_ITEM has no §13.5 route type — binding must raise."""
        item = CanonicalItemIdentity(
            source_type=EntityType.LIST_ITEM, tenant_id="t", principal_mailbox="",
            container="site/list", resource_id="list-item-1",
        )
        with pytest.raises(IdentityResolutionError):
            ItemToRouteBinder().bind(item, RouteMetadata(site_library="site/list"))

    def test_missing_route_field_raises(self) -> None:
        item = CanonicalItemIdentity(
            source_type=EntityType.MESSAGE, tenant_id="t", principal_mailbox="u@x.com",
            container="inbox", resource_id="msg-1",
        )
        with pytest.raises(IdentityResolutionError):
            ItemToRouteBinder().bind(item, RouteMetadata())  # no conversation_id

    def test_cache_key_prefers_immutable_id(self) -> None:
        item = CanonicalItemIdentity(
            source_type=EntityType.MESSAGE, tenant_id="t", principal_mailbox="u@x.com",
            container="inbox", resource_id="msg-1", immutable_id="imm-1",
        )
        assert "imm-1" in item.cache_key
        assert "msg-1" not in item.cache_key.split("|")[-1]


# ===========================================================================
# Query planner (§5.2) — capability tables + unsupported rejection
# ===========================================================================


class TestQueryPlanner:
    def test_message_compiler_emits_kql(self) -> None:
        intent = RetrievalIntent(
            entity_type=EntityType.MESSAGE,
            senders=("owner@example.com",),
            subject_terms=("deployment",),
        )
        plan = MessageQueryCompiler().compile(intent)
        assert plan.kql
        assert "from:" in plan.kql
        assert "subject:" in plan.kql
        assert plan.query_hash.startswith("sha256:")
        assert plan.capability_doc_version

    def test_teams_compiler_rejects_created_date_time(self) -> None:
        """§5.2 — createdDateTime>= is NOT a documented Teams Search scope term."""
        from datetime import date

        intent = RetrievalIntent(
            entity_type=EntityType.CHAT_MESSAGE,
            senders=("a@x.com",),
            from_date=date(2026, 1, 1),
        )
        plan = TeamsQueryCompiler().compile(intent)
        # The unsupported restriction is recorded, not silently dropped, and
        # NOT emitted into the KQL string.
        assert "createdDateTime>=" in plan.unsupported_requested
        assert "createdDateTime" not in plan.kql

    def test_compiler_for_returns_correct_type(self) -> None:
        assert isinstance(compiler_for(EntityType.MESSAGE), MessageQueryCompiler)
        assert isinstance(compiler_for(EntityType.EVENT), EventQueryCompiler)
        assert isinstance(compiler_for(EntityType.CHAT_MESSAGE), TeamsQueryCompiler)
        assert isinstance(compiler_for(EntityType.DRIVE_ITEM), SharePointQueryCompiler)

    def test_message_compiler_rejects_wrong_entity(self) -> None:
        intent = RetrievalIntent(entity_type=EntityType.EVENT)
        with pytest.raises(QueryCompileError):
            MessageQueryCompiler().compile(intent)


# ===========================================================================
# Privacy / PII scrubbing + credential fail-closed (§5.7 Stage 1)
# ===========================================================================


class TestPrivacyGate:
    def test_pii_scrub_redacts_email_and_phone(self) -> None:
        text = "Contact owner@example.com or call +1-555-123-4567."
        scrubbed = scrub_pii(text)
        assert "owner@example.com" not in scrubbed
        assert "[EMAIL_REDACTED]" in scrubbed
        assert "+1-555-123-4567" not in scrubbed

    def test_iso_date_surives_phone_scrub(self) -> None:
        """Date-protected regex: ISO dates are signal, not PII (§5.7)."""
        text = "Targeting 2026-06-23 for completion."
        scrubbed = scrub_pii(text)
        assert "2026-06-23" in scrubbed

    def test_credential_hit_fail_closed(self) -> None:
        result = run_local_checks(
            "Bearer eyJhbGci.eyJzdWI.sflKxwRJSMeKKF2QT4",
            source_type=EntityType.MESSAGE,
        )
        assert result.passed is False
        assert result.quarantined is True

    def test_sensitivity_denied_fails_gate(self) -> None:
        result = run_local_checks(
            "harmless text", source_type=EntityType.MESSAGE, sensitivity_label="restricted",
        )
        assert result.passed is False
        assert result.sensitivity_denied is True

    def test_size_exceeded_fails_gate(self) -> None:
        # Low-entropy text (short words, no long token runs) so the credential
        # detector does not fire before the size ceiling is checked.
        text = "a b c d e f g h i j k l m n o p q r s t u v w x y z " * 5
        result = run_local_checks(
            text, source_type=EntityType.MESSAGE, max_bytes=10,
        )
        assert result.passed is False
        assert result.size_exceeded is True

    def test_scrubber_version_stable(self) -> None:
        assert scrubber_version() == "scrub.v1"


# ===========================================================================
# Normalizer / chunker (§5.6) — fixed order, overlap, reproducibility
# ===========================================================================


class TestNormalizer:
    def test_normalize_strips_quoted_replies(self) -> None:
        body = "<p>New contribution here.</p>\nFrom: someone@example.com\nQuoted text."
        result = normalize(body, is_html=True)
        assert "New contribution here." in result.canonical_text
        assert "Quoted text." not in result.canonical_text

    def test_normalize_is_reproducible(self) -> None:
        body = "<p>The deployment completed on 2026-06-23.</p>"
        a = normalize(body, is_html=True)
        b = normalize(body, is_html=True)
        assert a.canonical_text == b.canonical_text
        assert a.normalized_source_hash == b.normalized_source_hash
        assert a.chunks == b.chunks

    def test_chunks_have_stable_ids_and_offsets(self) -> None:
        text = "Sentence one. Sentence two. Sentence three." * 30
        result = normalize(text, is_html=False)
        assert len(result.chunks) >= 1
        for chunk in result.chunks:
            assert chunk.chunk_id.startswith("chunk:")
            assert chunk.start_codepoint < chunk.end_codepoint
            # The chunk text must be a substring of the canonical text at its offsets.
            assert result.canonical_text[chunk.start_codepoint:chunk.end_codepoint].strip() == chunk.text

    def test_chunk_overlap_default(self) -> None:
        assert DEFAULT_CHUNK_OVERLAP == 500

    def test_dedupe_core_hash_stable(self) -> None:
        text = "The deployment completed."
        h1 = dedupe_core_hash_for(text, "deployment.completed")
        h2 = dedupe_core_hash_for(text, "deployment.completed")
        assert h1 == h2
        # Different event type → different hash.
        assert h1 != dedupe_core_hash_for(text, "incident.severity_changed")

    def test_merge_chunk_evidence_unions_and_flags_contradiction(self) -> None:
        outcome = merge_chunk_evidence(
            ("sha256:a",), ("sha256:b",),
            existing_payload={"status": "completed"},
            incoming_payload={"status": "rolled_back"},
        )
        assert outcome.merged is True
        assert outcome.contradiction is True
        assert set(outcome.unioned_evidence_refs) == {"sha256:a", "sha256:b"}

    def test_merge_chunk_evidence_no_contradiction_when_consistent(self) -> None:
        outcome = merge_chunk_evidence(
            ("sha256:a",), ("sha256:b",),
            existing_payload={"status": "completed"},
            incoming_payload={"status": "completed"},
        )
        assert outcome.contradiction is False


# ===========================================================================
# Governor — multi-budget enforcement (§5.10)
# ===========================================================================


class TestGovernor:
    def test_search_budget_enforced(self) -> None:
        limits = replace(BudgetLimits(), max_search_requests_total_per_cycle=2)
        gov = Governor(limits)
        assert gov.record_search("message").continue_run is True
        assert gov.record_search("message").continue_run is True
        decision = gov.record_search("message")
        assert decision.continue_run is False
        assert decision.category == "truncated_by_budget"
        assert decision.breached_budget == "max_search_requests_total_per_cycle"

    def test_per_entity_budget_separate_from_total(self) -> None:
        limits = replace(BudgetLimits(), max_search_requests_per_entity_per_cycle=1)
        gov = Governor(limits)
        assert gov.record_search("message").continue_run is True
        # The same entity again breaches the per-entity budget.
        d1 = gov.record_search("message")
        assert d1.continue_run is False
        # A different entity is still allowed (not starved).
        assert gov.record_search("event").continue_run is True

    def test_hydration_per_item_ceiling(self) -> None:
        limits = replace(BudgetLimits(), max_hydrated_bytes_per_item=100)
        gov = Governor(limits)
        d = gov.record_hydration(item_bytes=200, chunk_count=1)
        assert d.continue_run is False
        assert d.breached_budget == "max_hydrated_bytes_per_item"

    def test_decide_for_rate_limited_stops_run(self) -> None:
        gov = Governor(BudgetLimits())
        d = gov.decide_for_port_result(RateLimited(provider="graph", retry_after_seconds=1.0))
        assert d.continue_run is False
        assert d.category == "provider_limited"

    def test_decide_for_forbidden_stops_run(self) -> None:
        gov = Governor(BudgetLimits())
        d = gov.decide_for_port_result(Forbidden(scope="mail", reason="x"))
        assert d.continue_run is False
        assert d.category == "provider_limited"

    def test_decide_for_unsupported_continues_run(self) -> None:
        """Unsupported is a per-capability skip, not a run stop (§5.10)."""
        gov = Governor(BudgetLimits())
        d = gov.decide_for_port_result(Unsupported(entity_type="chatMessage", reason="phase2"))
        assert d.continue_run is True


# ===========================================================================
# Result union (§5.3/§5.10)
# ===========================================================================


class TestResultUnion:
    def test_is_success(self) -> None:
        assert is_success(Success(1)) is True
        assert is_success(Incomplete(1, reason="x")) is False
        assert is_success(Unsupported("m", "r")) is False

    def test_outcome_categories(self) -> None:
        assert outcome_category(Success(1)) == "complete"
        assert outcome_category(Incomplete(1, reason="budget_stop")) == "truncated_by_budget"
        assert outcome_category(Incomplete(1, reason="page_cut")) == "truncated_by_budget"
        assert outcome_category(RateLimited("p", 1.0)) == "provider_limited"
        assert outcome_category(Forbidden("s", "r")) == "provider_limited"
        assert outcome_category(Unsupported("m", "r")) == "unsupported"


# ===========================================================================
# Run-state machine (§5.10) — ephemeral crash-revert, durable checkpoints
# ===========================================================================


class TestRunStateMachine:
    def test_valid_forward_transition(self, tmp_path: Path) -> None:
        program_id = "test-rev-rs-forward"
        advance(program_id, "c1", RunState.ENUMERATED, RunState.LOCATOR_RESOLVED,
                programs_root=tmp_path, set_at=NOW)
        advance(program_id, "c1", RunState.LOCATOR_RESOLVED, RunState.HYDRATION_REQUIRED,
                programs_root=tmp_path, set_at=NOW)
        current = current_state_by_candidate(program_id, programs_root=tmp_path)
        assert current["c1"].state == "hydration_required"

    def test_ephemeral_state_crash_reverts_to_hydration_required(self, tmp_path: Path) -> None:
        """§5.10 — a candidate left in an ephemeral state reverts to hydration_required."""
        program_id = "test-rev-rs-crash"
        advance(program_id, "c1", RunState.ENUMERATED, RunState.LOCATOR_RESOLVED, programs_root=tmp_path, set_at=NOW)
        advance(program_id, "c1", RunState.LOCATOR_RESOLVED, RunState.HYDRATION_REQUIRED, programs_root=tmp_path, set_at=NOW)
        advance(program_id, "c1", RunState.HYDRATION_REQUIRED, RunState.HYDRATED, programs_root=tmp_path, set_at=NOW)
        advance(program_id, "c1", RunState.HYDRATED, RunState.SCANNED, programs_root=tmp_path, set_at=NOW)
        # Current derived state, with crash-revert applied.
        current = current_state_by_candidate(program_id, programs_root=tmp_path, apply_crash_revert=True)
        assert current["c1"].state == "hydration_required"
        assert "crash_revert_from:scanned" in current["c1"].note
        # Without revert, the raw ephemeral state is visible.
        raw = current_state_by_candidate(program_id, programs_root=tmp_path, apply_crash_revert=False)
        assert raw["c1"].state == "scanned"

    def test_durable_state_survives(self, tmp_path: Path) -> None:
        program_id = "test-rev-rs-durable"
        advance(program_id, "c1", RunState.ENUMERATED, RunState.LOCATOR_RESOLVED, programs_root=tmp_path, set_at=NOW)
        advance(program_id, "c1", RunState.LOCATOR_RESOLVED, RunState.HYDRATION_REQUIRED, programs_root=tmp_path, set_at=NOW)
        advance(program_id, "c1", RunState.HYDRATION_REQUIRED, RunState.HYDRATED, programs_root=tmp_path, set_at=NOW)
        advance(program_id, "c1", RunState.HYDRATED, RunState.SCANNED, programs_root=tmp_path, set_at=NOW)
        advance(program_id, "c1", RunState.SCANNED, RunState.EXTRACTED_EPHEMERALLY, programs_root=tmp_path, set_at=NOW)
        advance(program_id, "c1", RunState.EXTRACTED_EPHEMERALLY, RunState.EXCERPTS_VAULTED, programs_root=tmp_path, set_at=NOW)
        current = current_state_by_candidate(program_id, programs_root=tmp_path)
        # excerpts_vaulted is durable — NOT reverted.
        assert current["c1"].state == "excerpts_vaulted"

    def test_invalid_transition_raises(self, tmp_path: Path) -> None:
        program_id = "test-rev-rs-invalid"
        with pytest.raises(ValueError):
            advance(program_id, "c1", RunState.ENUMERATED, RunState.ACCEPTED, programs_root=tmp_path)

    def test_terminal_state_cannot_advance(self, tmp_path: Path) -> None:
        program_id = "test-rev-rs-terminal"
        advance(program_id, "c1", RunState.ENUMERATED, RunState.LOCATOR_RESOLVED, programs_root=tmp_path, set_at=NOW)
        advance(program_id, "c1", RunState.LOCATOR_RESOLVED, RunState.HYDRATION_REQUIRED, programs_root=tmp_path, set_at=NOW)
        advance(program_id, "c1", RunState.HYDRATION_REQUIRED, RunState.QUARANTINED, programs_root=tmp_path, set_at=NOW)
        with pytest.raises(ValueError):
            advance(program_id, "c1", RunState.QUARANTINED, RunState.ACCEPTED, programs_root=tmp_path)

    def test_state_distribution(self, tmp_path: Path) -> None:
        program_id = "test-rev-rs-dist"
        for cid in ("c1", "c2"):
            advance(program_id, cid, RunState.ENUMERATED, RunState.LOCATOR_RESOLVED, programs_root=tmp_path, set_at=NOW)
        dist = state_distribution(program_id, programs_root=tmp_path)
        assert dist.get("locator_resolved") == 2

    def test_ephemeral_states_set_is_correct(self) -> None:
        assert EPHEMERAL_STATES == frozenset({
            RunState.HYDRATED, RunState.SCANNED, RunState.EXTRACTED_EPHEMERALLY,
        })


# ===========================================================================
# Evidence vault two-stage lifecycle (§5.7)
# ===========================================================================


class TestEvidenceVault:
    def test_store_and_read_admitted_excerpt(self, tmp_path: Path) -> None:
        program_id = "test-rev-ev-1"
        excerpt = "deployment completed on 2026-06-23"
        full_source = f"Subject: Update\n\n{excerpt}\n\nRegards,\nThe team."
        meta = build_metadata_defaults(
            tenant_id="t", principal_mailbox="u@x.com", container="inbox",
            canonical_item_id="msg-1", canonical_route_id="conv-1",
            retrieval_timestamp=NOW, profile=None,
            extraction_model=DETERMINISTIC_MODEL,
            extraction_schema_version=EXTRACTION_SCHEMA_VERSION,
        )
        ref = store_admitted_excerpt(
            program_id=program_id, excerpt_text=excerpt,
            normalized_source_text=full_source, metadata=meta, programs_root=tmp_path,
        )
        assert ref.vault_hash.startswith("sha256:")
        assert ref.start_codepoint == 0
        assert ref.end_codepoint == len(excerpt)
        # The excerpt hash (over just the excerpt) differs from the full source hash.
        assert ref.excerpt_hash != ref.normalized_source_hash

        loaded_meta = load_rev_evidence_metadata(
            program_id=program_id, vault_hash=ref.vault_hash, programs_root=tmp_path,
        )
        assert loaded_meta is not None
        assert loaded_meta.schema_version == REV_EVIDENCE_METADATA_SCHEMA_VERSION
        assert loaded_meta.extraction_model == DETERMINISTIC_MODEL
        assert loaded_meta.canonical_item_id == "msg-1"

    def test_metadata_round_trip(self) -> None:
        from src.core.ledger.rev_evidence import RevEvidenceMetadata

        meta = RevEvidenceMetadata(
            tenant_id_hash="sha256:t",
            canonical_item_id="msg-1",
            canonical_route_id="conv-1",
            retrieval_timestamp=NOW,
            content_safety_result="unavailable",
            retention_class="pending",
        )
        roundtripped = RevEvidenceMetadata.from_dict(meta.to_dict())
        assert roundtripped == meta

    def test_evidence_ref_dict_round_trip(self) -> None:
        ref = EvidenceRef(
            vault_hash="sha256:h", representation_version="norm.v1",
            start_codepoint=0, end_codepoint=10,
            excerpt_hash="sha256:e", normalized_source_hash="sha256:s",
        )
        refs = (ref,)
        assert evidence_refs_from_dict(evidence_refs_to_dict(refs)) == refs

    def test_retention_by_reference_accepted_event_never_purged(self) -> None:
        from src.core.ledger.rev_evidence import (
            RETENTION_CLASS_ACCEPTED_EVENT,
            compute_purge_deadline,
            retention_class_for,
        )

        assert retention_class_for(
            has_candidate=True, has_assertion=True, decision_kind=None,
            resulting_event_id="evt-1",
        ) == RETENTION_CLASS_ACCEPTED_EVENT
        assert compute_purge_deadline(retention_class=RETENTION_CLASS_ACCEPTED_EVENT, as_of=NOW) is None


# ===========================================================================
# Deterministic extractor (§5.8) — materiality predicate + span capture
# ===========================================================================


class TestDeterministicExtractor:
    def _hydrated(self, text: str) -> HydratedContent:
        return _hydrated(text)

    def test_extracts_deployment_completed(self) -> None:
        text = "The rollout deployment completed on 2026-06-23 without issues."
        hydrated = self._hydrated(text)
        claims = DeterministicRevExtractor().extract(hydrated, correlation_id="cid").value
        types = {c.event_type for c in claims}
        assert "deployment.completed" in types
        deploy = next(c for c in claims if c.event_type == "deployment.completed")
        assert deploy.material is True  # material predicate
        assert deploy.extraction_model == DETERMINISTIC_MODEL
        assert deploy.evidence_spans

    def test_extracts_rollback(self) -> None:
        text = "We rolled back the deployment on 2026-06-22."
        hydrated = self._hydrated(text)
        claims = DeterministicRevExtractor().extract(hydrated, correlation_id="cid").value
        assert any(c.event_type == "deployment.rollback" for c in claims)

    def test_empty_extraction_on_no_signal(self) -> None:
        """A document with no extractable facts yields empty (valid, §5.8)."""
        text = "Reminder: please update your TPS reports."
        hydrated = self._hydrated(text)
        claims = DeterministicRevExtractor().extract(hydrated, correlation_id="cid").value
        assert claims == ()

    def test_status_table_done_cell_is_not_a_completion(self) -> None:
        """v2.23 precision guard: a pasted status table where 'Deployment' (a
        column header) and 'Done' (a cell value) land on separate lines must
        NOT be extracted as a deployment.completed event. This was the dominant
        false-positive in the NOVA corpus (21/23 false-positives)."""
        text = (
            "Deployment\n"
            "Safety\n"
            "Data Plane\n"
            "\n"
            "Done\n"
            "Done\n"
            "Done\n"
        )
        hydrated = self._hydrated(text)
        claims = DeterministicRevExtractor().extract(hydrated, correlation_id="cid").value
        assert all(c.event_type != "deployment.completed" for c in claims), (
            "status-table 'Done' cells must not be extracted as deployment.completed"
        )

    def test_status_table_bare_done_in_window_is_rejected(self) -> None:
        """A real-looking completion near bare status cells is still rejected
        when the surrounding window is clearly a table."""
        text = (
            "Status update:\n"
            "Done\n"
            "Done\n"
            "The rollout deployment completed.\n"
        )
        hydrated = self._hydrated(text)
        claims = DeterministicRevExtractor().extract(hydrated, correlation_id="cid").value
        assert all(c.event_type != "deployment.completed" for c in claims)

    def test_metadata_only_yields_empty(self) -> None:
        hydrated = HydratedContent(
            identity=CanonicalItemIdentity(
                source_type=EntityType.MESSAGE, tenant_id="t", principal_mailbox="u@x.com",
                container="inbox", resource_id="msg-1",
            ),
            canonical_text="", normalized_source_hash="sha256:x", chunks=(),
            route_metadata={}, metadata_only=True, correlation_id="cid",
        )
        claims = DeterministicRevExtractor().extract(hydrated, correlation_id="cid").value
        assert claims == ()

    def test_materiality_predicate_deterministic(self) -> None:
        from src.ai.rev.extractor import is_material_event

        assert is_material_event("deployment.completed") is True
        assert is_material_event("commitment.date_set") is True
        assert is_material_event("nonexistent.type") is False


# ===========================================================================
# Layered verification end-to-end (§5.9)
# ===========================================================================


class TestLayeredVerification:
    def test_honest_claim_yields_assertions(self, tmp_path: Path) -> None:
        program_id = "test-rev-lv-1"
        text = "The rollout deployment completed on 2026-06-23."
        hydrated = _hydrated(text)
        claims = DeterministicRevExtractor().extract(hydrated, correlation_id="cid").value
        assert claims
        outcome = run_layered_verification(
            program_id=program_id, candidate_id="c1", claims=claims,
            hydrated=hydrated, evidence_refs=("sha256:h1",),
            set_at=NOW, programs_root=tmp_path,
        )
        # Material claim without human → unverified, but assertions were written.
        assert outcome.assertions_written > 0
        assert outcome.effective_state == STATE_UNVERIFIED

    def test_human_pass_after_verification_reaches_human_verified(self, tmp_path: Path) -> None:
        program_id = "test-rev-lv-2"
        text = "The rollout deployment completed on 2026-06-23."
        hydrated = _hydrated(text)
        claims = DeterministicRevExtractor().extract(hydrated, correlation_id="cid").value
        run_layered_verification(
            program_id=program_id, candidate_id="c2", claims=claims,
            hydrated=hydrated, evidence_refs=("sha256:h1",),
            set_at=NOW, programs_root=tmp_path,
        )
        append_verification_assertion(
            human_pass_assertion("c2", actor="operator", evidence_refs=("sha256:h1",)),
            program_id=program_id, programs_root=tmp_path,
        )
        state = effective_verification_state(
            assertions_for_candidate(program_id, "c2", programs_root=tmp_path)
        )
        assert state == STATE_HUMAN_VERIFIED


# ===========================================================================
# P1-0b: REV-G1b — ledger projection completeness + multi-claim staging
# ===========================================================================


class TestLedgerProjectionCompleteness:
    """All 8 MATERIAL_EVENT_TYPES must map to a registered ledger event type.

    Prior to P1-0b, ``deployment.started``, ``risk.blocking_milestone``, and
    ``ownership.changed`` were silently dropped (data-loss defect, REV-G1b).
    """

    ALL_8_TYPES = [
        "deployment.completed",
        "deployment.rollback",
        "deployment.started",
        "incident.severity_changed",
        "commitment.date_set",
        "milestone.completed",
        "risk.blocking_milestone",
        "ownership.changed",
    ]

    REGISTERED_LEDGER_EVENTS = {
        "milestone.completed.v1",
        "deployment.completed.v1",
        "deployment.rollback.v1",
        "deployment.started.v1",
        "incident.severity_changed.v1",
        "commitment.made.v1",
        "risk.raised.v1",
        "workstream.owner_changed.v1",
    }

    def test_all_8_event_types_have_ledger_projection(self) -> None:
        from src.core.rev.pipeline import _CLAIM_TO_LEDGER_EVENT

        for event_type in self.ALL_8_TYPES:
            assert event_type in _CLAIM_TO_LEDGER_EVENT, (
                f"REV-G1b: {event_type!r} missing from _CLAIM_TO_LEDGER_EVENT — "
                "claims of this type are silently dropped (data loss)"
            )
            mapped = _CLAIM_TO_LEDGER_EVENT[event_type]
            assert mapped in self.REGISTERED_LEDGER_EVENTS, (
                f"{event_type!r} → {mapped!r} is not a registered ledger event"
            )

    def test_material_event_types_match_claim_map(self) -> None:
        from src.ai.rev.extractor import MATERIAL_EVENT_TYPES
        from src.core.rev.pipeline import _CLAIM_TO_LEDGER_EVENT

        missing = MATERIAL_EVENT_TYPES - set(_CLAIM_TO_LEDGER_EVENT.keys())
        assert missing == frozenset(), (
            f"MATERIAL_EVENT_TYPES not covered by _CLAIM_TO_LEDGER_EVENT: {missing}"
        )

    def test_shape_ledger_event_all_types_produce_valid_payload(self) -> None:
        from src.core.rev.pipeline import _shape_ledger_event

        text = "The partner dependency is now blocking our P0 milestone."
        hydrated = _hydrated(text)

        class _Claim:
            def __init__(self, event_type: str) -> None:
                self.event_type = event_type
                self.extraction_confidence = 0.9
                self.evidence_spans: list[object] = []
                self.payload: dict[str, object] = {
                    "blocker_description": "partner dependency",
                    "new_owner": "Priya",
                    "severity": "high",
                }

        for event_type in self.ALL_8_TYPES:
            claim = _Claim(event_type)
            et, payload = _shape_ledger_event(claim, hydrated, NOW)
            assert et in self.REGISTERED_LEDGER_EVENTS, (
                f"_shape_ledger_event({event_type!r}) returned unregistered event type {et!r}"
            )
            assert isinstance(payload, dict) and payload, (
                f"_shape_ledger_event({event_type!r}) returned empty or non-dict payload"
            )

    def test_risk_raised_payload_has_required_fields(self) -> None:
        from src.core.rev.pipeline import _shape_ledger_event

        hydrated = _hydrated("The partner dependency blocks our P0 milestone.")

        class _Claim:
            event_type = "risk.blocking_milestone"
            extraction_confidence = 0.9
            evidence_spans: list[object] = []
            payload: dict[str, object] = {
                "blocker_description": "partner dependency",
                "severity": "critical",
            }

        _, payload = _shape_ledger_event(_Claim(), hydrated, NOW)
        assert "risk_id" in payload
        assert "title" in payload
        assert "severity" in payload

    def test_workstream_owner_changed_payload_has_required_fields(self) -> None:
        from src.core.rev.pipeline import _shape_ledger_event

        hydrated = _hydrated("Priya will own Gen9 bringup going forward.")

        class _Claim:
            event_type = "ownership.changed"
            extraction_confidence = 0.9
            evidence_spans: list[object] = []
            payload: dict[str, object] = {"new_owner": "Priya"}

        _, payload = _shape_ledger_event(_Claim(), hydrated, NOW)
        assert "workstream_id" in payload
        assert "new_owner_person_id" in payload

    def test_deployment_started_maps_to_deployment_started_type(self) -> None:
        # v2.22 (ADR-0006 R2): deployment.started maps to its own faithful type,
        # not deliverable.status_changed.v1 (which is Phase-2 scope + wrong type).
        from src.core.rev.pipeline import _shape_ledger_event

        hydrated = _hydrated("BIOS AP deployment started on Gen9 fleet.")

        class _Claim:
            event_type = "deployment.started"
            extraction_confidence = 0.9
            evidence_spans: list[object] = []
            payload: dict[str, object] = {}

        event_type, payload = _shape_ledger_event(_Claim(), hydrated, NOW)
        assert event_type == "deployment.started.v1"
        assert "deployment_id" in payload
        assert "artifact_name" in payload
        assert "started_on" in payload

    def test_deployment_rollback_maps_to_deployment_rollback_type(self) -> None:
        # v2.22 (ADR-0006 R2): deployment.rollback maps to its own faithful type,
        # not deliverable.status_changed.v1.
        from src.core.rev.pipeline import _shape_ledger_event

        hydrated = _hydrated("The Gen9 rollout was rolled back due to a regression.")

        class _Claim:
            event_type = "deployment.rollback"
            extraction_confidence = 0.9
            evidence_spans: list[object] = []
            payload: dict[str, object] = {}

        event_type, payload = _shape_ledger_event(_Claim(), hydrated, NOW)
        assert event_type == "deployment.rollback.v1"
        assert "deployment_id" in payload
        assert "reason" in payload

    def test_incident_severity_changed_maps_to_faithful_type(self) -> None:
        # v2.22 (ADR-0006 R2): incident.severity_changed maps to its own type,
        # not incident.opened.v1 (a severity change is not an incident opening).
        from src.core.rev.pipeline import _shape_ledger_event

        hydrated = _hydrated("Sev 2 for the XStore incident was raised.")

        class _Claim:
            event_type = "incident.severity_changed"
            extraction_confidence = 0.9
            evidence_spans: list[object] = []
            payload: dict[str, object] = {"severity": "2", "prior_severity": "3"}

        event_type, payload = _shape_ledger_event(_Claim(), hydrated, NOW)
        assert event_type == "incident.severity_changed.v1"
        assert "incident_id" in payload
        assert "new_severity" in payload


class TestMultiClaimStaging:
    """_stage_candidates stages ALL claims, not just the strongest (REV-G1b)."""

    def _make_claims(self, event_types: list[str]) -> tuple[object, ...]:
        from src.ai.rev.extractor import EvidenceSpan, ExtractedClaim, is_material_event
        result = []
        for i, et in enumerate(event_types):
            span_text = f"evidence for {et}"
            result.append(ExtractedClaim(
                event_type=et,
                payload={"date": "2026-06-24"},
                evidence_spans=(EvidenceSpan(f"c{i}", 0, len(span_text), span_text),),
                extraction_confidence=0.7 + i * 0.05,
                extraction_model="rev.deterministic.regex.v1",
                material=is_material_event(et),
            ))
        return tuple(result)

    def _fake_evidence_ref(self) -> "EvidenceRef":
        from src.core.ledger.rev_evidence import EvidenceRef
        return EvidenceRef(
            vault_hash="sha256:test-vault",
            representation_version="1",
            start_codepoint=0,
            end_codepoint=10,
            excerpt_hash="sha256:excerpt-hash",
            normalized_source_hash="sha256:source-hash",
        )

    def test_all_claims_staged_not_just_strongest(self, tmp_path: Path) -> None:
        from src.core.rev.pipeline import _stage_candidates
        from src.core.rev.identity import CanonicalItemIdentity

        identity = CanonicalItemIdentity(
            source_type=EntityType.MESSAGE,
            tenant_id="tenant-test",
            principal_mailbox="tpm@example.com",
            container="inbox",
            resource_id="msg-multi-001",
        )
        text = "Deployment completed. Priya is now the owner."
        hydrated = HydratedContent(
            identity=identity,
            canonical_text=text,
            normalized_source_hash="sha256:" + text[:32],
            chunks=(),
            route_metadata={
                "subject": "NOVA Weekly Update",
                "sender": "lead@example.com",
                "received_at": "2026-06-24T10:00:00Z",
            },
        )
        claims = self._make_claims(["deployment.completed", "ownership.changed"])
        evidence_ref = self._fake_evidence_ref()

        staged, _iso_fails = _stage_candidates(
            program_id="nova",
            hydrated=hydrated,
            claims=claims,
            evidence_refs=(evidence_ref,),
            set_at=NOW,
            programs_root=tmp_path,
        )
        assert len(staged) == 2, (
            f"Expected 2 candidates (one per claim), got {len(staged)}. "
            "REV-G1b: _stage_candidates must stage ALL claims, not just strongest."
        )
        event_types_staged = {c.proposed_event_type for c in staged}
        assert len(event_types_staged) == 2

    def test_single_claim_stages_one_candidate(self, tmp_path: Path) -> None:
        from src.core.rev.pipeline import _stage_candidates
        from src.core.rev.identity import CanonicalItemIdentity

        identity = CanonicalItemIdentity(
            source_type=EntityType.MESSAGE,
            tenant_id="t",
            principal_mailbox="u@x.com",
            container="inbox",
            resource_id="msg-001",
        )
        hydrated = HydratedContent(
            identity=identity,
            canonical_text="Deployment completed.",
            normalized_source_hash="sha256:test",
            chunks=(),
            route_metadata={"subject": "Update", "sender": "x@x.com", "received_at": "2026-06-24T10:00:00Z"},
        )
        claims = self._make_claims(["deployment.completed"])
        evidence_ref = self._fake_evidence_ref()
        staged, _iso_fails = _stage_candidates(
            program_id="nova",
            hydrated=hydrated,
            claims=claims,
            evidence_refs=(evidence_ref,),
            set_at=NOW,
            programs_root=tmp_path,
        )
        assert len(staged) == 1

    def test_empty_claims_returns_empty_list(self, tmp_path: Path) -> None:
        from src.core.rev.pipeline import _stage_candidates
        from src.core.rev.identity import CanonicalItemIdentity

        identity = CanonicalItemIdentity(
            source_type=EntityType.MESSAGE,
            tenant_id="t",
            principal_mailbox="u@x.com",
            container="inbox",
            resource_id="msg-empty",
        )
        hydrated = HydratedContent(
            identity=identity,
            canonical_text="",
            normalized_source_hash="sha256:empty",
            chunks=(),
            route_metadata={},
        )
        staged, _iso_fails = _stage_candidates(
            program_id="nova",
            hydrated=hydrated,
            claims=(),
            evidence_refs=(),
            set_at=NOW,
            programs_root=tmp_path,
        )
        assert staged == []


class TestKI7RevExtractorProtocolInZoneA:
    """KI-7: RevExtractor Protocol lives in Zone A (src/core/rev/ports.py)."""

    def test_rev_extractor_protocol_in_ports(self) -> None:
        from src.core.rev.ports import RevExtractor
        assert hasattr(RevExtractor, "extract"), "RevExtractor must have .extract method"
        assert (
            getattr(RevExtractor, "_is_protocol", False)
            or any(
                getattr(base, "__name__", "") == "Protocol"
                for base in getattr(RevExtractor, "__mro__", [])
            )
        ), "RevExtractor must be a typing.Protocol"

    def test_rev_pipeline_deps_fields_not_any(self) -> None:
        from src.core.rev.pipeline import RevPipelineDeps
        import typing
        hints = typing.get_type_hints(RevPipelineDeps)
        for field_name in ("hydrator", "shields", "extractor"):
            ann = hints.get(field_name)
            assert ann is not typing.Any, (
                f"RevPipelineDeps.{field_name} is still typed as Any — KI-7 not fully fixed"
            )

    def test_zone_a_pipeline_does_not_import_zone_b_at_runtime(self) -> None:
        src_path = REPO_ROOT / "src" / "core" / "rev" / "pipeline.py"
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            module = (
                node.names[0].name
                if isinstance(node, ast.Import)
                else (node.module or "")
            )
            assert not module.startswith("src.ai"), (
                f"pipeline.py imports {module!r} from Zone B — violates INV-1"
            )


# ===========================================================================
# P1-0c: append_candidate idempotency on candidate_id (crash-resume safety)
# ===========================================================================


class TestAppendCandidateIdempotency:
    """append_candidate must be idempotent on candidate_id (P1-0c)."""

    def _make_candidate(self, program_id: str, candidate_id: str = "cand-idem-1") -> CandidateEvent:
        return CandidateEvent(
            candidate_id=candidate_id,
            program_id=program_id,
            proposed_event_type="milestone.completed.v1",
            proposed_payload={"milestone_id": "m1", "completed_on": "2026-06-24", "evidence": "done"},
            proposed_occurred_at=NOW,
            proposed_temporal_confidence="exact",
            proposed_confidence="medium",
            source_ref=_email_ref("sha256:vault-idem", message_id="msg-idem"),
            pipeline="rev_mail",
            extraction_confidence=0.85,
            entity_resolution=(),
            dedupe_key=candidate_id,
            dedupe_core_hash="hash-idem",
            source_document_key="sdk-idem",
            corroborating_refs=(),
            batch_id="rev:20260624120000",
            staged_at=NOW,
            schema_version="1",
            evidence_refs=(),
        )

    def test_second_append_is_no_op(self, tmp_path: Path) -> None:
        program_id = "nova-idem-1"
        candidate = self._make_candidate(program_id)

        result1 = append_candidate(candidate, programs_root=tmp_path)
        result2 = append_candidate(candidate, programs_root=tmp_path)

        assert result1 is True, "First append should return True"
        assert result2 is False, "Second append of same candidate_id should return False (no-op)"

    def test_only_one_record_written_on_duplicate(self, tmp_path: Path) -> None:
        program_id = "nova-idem-2"
        candidate = self._make_candidate(program_id)

        append_candidate(candidate, programs_root=tmp_path)
        append_candidate(candidate, programs_root=tmp_path)
        append_candidate(candidate, programs_root=tmp_path)

        loaded = load_pending_candidates(program_id, programs_root=tmp_path)
        assert len(loaded) == 1, (
            f"Expected 1 candidate in pending.jsonl after 3 duplicate appends, got {len(loaded)}"
        )

    def test_different_candidate_ids_both_appended(self, tmp_path: Path) -> None:
        program_id = "nova-idem-3"
        c1 = self._make_candidate(program_id, candidate_id="cand-1")
        c2 = self._make_candidate(program_id, candidate_id="cand-2")

        r1 = append_candidate(c1, programs_root=tmp_path)
        r2 = append_candidate(c2, programs_root=tmp_path)
        r3 = append_candidate(c1, programs_root=tmp_path)  # duplicate

        assert r1 is True
        assert r2 is True
        assert r3 is False

        loaded = load_pending_candidates(program_id, programs_root=tmp_path)
        assert len(loaded) == 2


class TestKI4ShieldBypassBlocked:
    """KI-4 security gate (P1-8): a chunk blocked by Prompt Shields must never
    reach the extractor via ANY path — neither the ``chunks`` tuple nor the
    ``canonical_text`` the LLM extractor sends to the model.

    The pipeline rebuilds ``HydratedContent`` from admitted chunks only and
    re-concatenates ``canonical_text`` from their text (``pipeline.py`` KI-4
    fix). This contract drives ``run_rev_cycle`` with a shield that blocks the
    chunk carrying a secret and a capturing extractor that records exactly what
    it received.
    """

    def test_blocked_chunk_text_absent_from_extractor_input(self, tmp_path: Path) -> None:
        from src.core.models_v2 import REV_PROFILE_SEARCH_HYDRATE, RevRetrievalProfile
        from src.core.rev.pipeline import RevPipelineDeps, run_rev_cycle
        from src.core.rev.prompt_shields import VERDICT_FLAGGED, VERDICT_UNAVAILABLE, ChunkShieldResult

        public_text = "PUBLIC update here"
        secret_text = "SECRET password hunter2 leaked"
        # Original canonical text holds both chunks (offsets into it).
        canonical = public_text + "\n" + secret_text
        chunk_public = Chunk(
            chunk_id="chunk-public",
            text=public_text,
            start_codepoint=0,
            end_codepoint=len(public_text),
        )
        chunk_secret = Chunk(
            chunk_id="chunk-secret",
            text=secret_text,
            start_codepoint=len(public_text) + 1,
            end_codepoint=len(public_text) + 1 + len(secret_text),
        )
        hydrated = HydratedContent(
            identity=CanonicalItemIdentity(
                source_type=EntityType.MESSAGE,
                tenant_id="t",
                principal_mailbox="u@x.com",
                container="inbox",
                resource_id="<ki4-msg@x.com>",
            ),
            canonical_text=canonical,
            normalized_source_hash="sha256:ki4",
            chunks=(chunk_public, chunk_secret),
            route_metadata={"subject": "KI4 test", "sender": "a@x.com", "received_at": "2026-06-24T10:00:00+00:00"},
            hydration_rung="unique_body",
            metadata_only=False,
            retrieved_at=NOW,
            correlation_id="ki4",
        )

        class _BlockingShields:
            policy_version = "test"

            def scan_chunks(self, chunks, *, source_type, correlation_id):
                return Success((
                    ChunkShieldResult(
                        chunk_id="chunk-public",
                        local_check=LocalCheckResult(passed=True),
                        external_verdict=VERDICT_UNAVAILABLE,
                        degrade_reason="test",
                    ),
                    ChunkShieldResult(
                        chunk_id="chunk-secret",
                        local_check=LocalCheckResult(passed=False, reason="credential_hit"),
                        external_verdict=VERDICT_FLAGGED,
                        degrade_reason="blocked",
                    ),
                ))

        received: dict = {}

        class _CapturingExtractor:
            def extract(self, hyd, *, correlation_id):
                received["canonical_text"] = hyd.canonical_text
                received["chunk_ids"] = tuple(c.chunk_id for c in hyd.chunks)
                # One grounded claim from the admitted (public) chunk so staging/vaulting succeed.
                span = EvidenceSpan(
                    chunk_id="chunk-public",
                    start_codepoint=0,
                    end_codepoint=min(10, len(public_text)),
                    excerpt_text=public_text[:10],
                )
                return Success((
                    ExtractedClaim(
                        event_type="deployment.completed",
                        payload={"status": "completed", "date": "2026-06-24", "subject": "KI4 test"},
                        evidence_spans=(span,),
                        extraction_confidence=0.8,
                        extraction_model=DETERMINISTIC_MODEL,
                        material=True,
                    ),
                ))

        candidate = EnumeratedCandidate(
            locator=HydrationLocator(
                source_type=EntityType.MESSAGE,
                tenant_id="t",
                principal_mailbox="u@x.com",
                container="inbox",
                resource_id="<ki4-msg@x.com>",
            ),
            relevance_score=0.9,
            partial_metadata={},  # no eml_path → file finalization is a no-op
            correlation_id="ki4",
            enumerator="test",
            received_at=NOW,
        )

        class _OneShotEnumerator:
            entity_type = EntityType.MESSAGE

            def enumerate(self, intent, *, correlation_id):
                return Success((candidate,))

        class _FixedHydrator:
            def hydrate(self, cand, *, correlation_id):
                return Success(hydrated)

        deps = RevPipelineDeps(
            enumerator=_OneShotEnumerator(),
            hydrator=_FixedHydrator(),
            shields=_BlockingShields(),
            extractor=_CapturingExtractor(),
            verifier=lambda **kw: run_layered_verification(**kw).effective_state,
        )
        intent = RetrievalIntent(entity_type=EntityType.MESSAGE, limit=25)
        report = run_rev_cycle(
            program_id="prog-ki4",
            intent=intent,
            deps=deps,
            profile=RevRetrievalProfile(profile=REV_PROFILE_SEARCH_HYDRATE),
            mailbox_tenant_id="t",
            mailbox_principal="u@x.com",
            mailbox_container="inbox",
            correlation_id="ki4",
            programs_root=tmp_path,
            budget_limits=BudgetLimits(),
            set_at=NOW,
        )

        assert report.candidates_staged == 1, "admitted chunk should stage a candidate"
        # The blocked chunk never reached the extractor via either path.
        assert "chunk-secret" not in received["chunk_ids"], "blocked chunk must not be in extractor chunks"
        assert "SECRET" not in received["canonical_text"], "blocked chunk text must not be in canonical_text"
        assert "hunter2" not in received["canonical_text"]
        assert "chunk-public" in received["chunk_ids"]
        assert "PUBLIC" in received["canonical_text"]


# ===========================================================================
# REV-NC1 — normalizer chunking: no per-character sliding fragments
# ===========================================================================

class TestNormalizerChunkingContract:
    """REV-NC1: chunk_canonical must not generate per-character sliding fragments.

    Root cause (regression guard): when paragraphs are shorter than ``overlap``
    (500 chars), ``best_end - overlap`` can be less than ``cursor``.  The old
    forward-progress guard fell back to ``cursor + 1``, producing 1190 tiny
    fragments from a 44K-char newsletter.  The minimum-stride fix ensures each
    iteration advances by at least ``chunk_size - overlap`` characters.
    """

    def test_short_paragraphs_do_not_explode_chunk_count(self) -> None:
        """100 short paragraphs (50 chars each) must yield < 20 chunks."""
        paras = [f"Section {i}: short content paragraph {i}." for i in range(100)]
        text = "\n\n".join(paras)
        chunks = chunk_canonical(text)
        assert len(chunks) < 20, (
            f"100 short paragraphs produced {len(chunks)} chunks — "
            "per-character sliding fragments have regressed"
        )

    def test_newsletter_sized_body_chunk_count(self) -> None:
        """A 44K-char body must produce at most 50 chunks (not hundreds)."""
        # 60 sections × avg 800 chars ≈ 48K chars
        sections = []
        for i in range(60):
            length = 200 + (i % 5) * 300  # 200-1400 chars per section
            sections.append("X" * length)
        text = "\n\n".join(sections)
        assert len(text) > 40_000, "sanity: text must be > 40K chars"
        chunks = chunk_canonical(text)
        assert len(chunks) <= 50, (
            f"44K-char body produced {len(chunks)} chunks; expected <= 50. "
            "Chunking may be regressing to per-character sliding."
        )

    def test_minimum_chunk_size_is_reasonable(self) -> None:
        """No chunk should be shorter than 50 chars on real-world content."""
        paras = [f"Para {i}: " + "word " * 20 + "end." for i in range(50)]
        text = "\n\n".join(paras)
        chunks = chunk_canonical(text)
        tiny = [c for c in chunks if len(c.text) < 50]
        assert not tiny, (
            f"{len(tiny)} chunks are < 50 chars: {[c.text[:30] for c in tiny[:3]]}"
        )

    def test_chunk_ids_are_unique(self) -> None:
        """All chunk_ids must be unique within a document."""
        text = "\n\n".join([f"Section {i}: content " + "x" * 300 for i in range(20)])
        chunks = chunk_canonical(text)
        ids = [c.chunk_id for c in chunks]
        assert len(ids) == len(set(ids)), "duplicate chunk_ids detected"

    def test_chunks_cover_full_text(self) -> None:
        """G-coverage: every codepoint in the canonical text appears in ≥1 chunk."""
        text = "Para A: " + "x" * 300 + "\n\nPara B: " + "y" * 300 + "\n\nPara C: " + "z" * 300
        chunks = chunk_canonical(text)
        covered: set[int] = set()
        for c in chunks:
            covered.update(range(c.start_codepoint, c.end_codepoint))
        # Every position from 0 to len(text)-1 must be covered (G-coverage).
        missing = set(range(len(text))) - covered
        assert not missing, (
            f"G-coverage: {len(missing)} codepoints uncovered; first at {min(missing)}"
        )

    def test_data_loss_scenario_ps9(self) -> None:
        """PS-9 / G-coverage: short opener then long unbroken block must not drop text.

        Root cause (old code): a paragraph boundary at position ~15 caused
        best_end=15; the min_stride guard then jumped cursor to 1500, silently
        dropping text[15:1500].  Fixed by only using a boundary that is at
        least (chunk_size - overlap) chars from the cursor.
        """
        text = "Short opener.\n\n" + "x" * 3000
        chunks = chunk_canonical(text, chunk_size=DEFAULT_CHUNK_SIZE, overlap=DEFAULT_CHUNK_OVERLAP)
        covered: set[int] = set()
        for c in chunks:
            covered.update(range(c.start_codepoint, c.end_codepoint))
        missing = set(range(len(text))) - covered
        assert not missing, (
            f"PS-9 data-loss regression: {len(missing)} codepoints dropped; "
            f"first missing at {min(missing)}"
        )

    def test_overlap_invariant(self) -> None:
        """G-coverage: consecutive chunks must overlap (next.start ≤ prev.end).

        A gap between chunks would mean source text in that gap is unreachable
        by any downstream extraction, violating G-coverage.
        """
        text = "Para A: " + "x" * 1000 + "\n\nPara B: " + "y" * 1000 + "\n\nPara C: " + "z" * 1000
        chunks = chunk_canonical(text)
        for prev, curr in zip(chunks, chunks[1:]):
            assert curr.start_codepoint <= prev.end_codepoint, (
                f"G-coverage gap: prev ends at {prev.end_codepoint}, "
                f"next starts at {curr.start_codepoint} — {curr.start_codepoint - prev.end_codepoint} "
                f"codepoints dropped between chunks"
            )

    def test_minimum_stride_matches_chunk_minus_overlap(self) -> None:
        """After PS-9 fix: stride ≥ (chunk_size - 2*overlap); coverage is the hard gate.

        The old invariant (stride ≥ chunk_size - overlap) required the min_stride
        guard that caused data loss.  The new invariant is coverage (G-coverage):
        every codepoint is in ≥1 chunk.  Stride can be as small as
        (chunk_size - 2*overlap) when a valid boundary is used exactly at
        cursor + (chunk_size - overlap).
        """
        text = "\n\n".join(["Short para " + str(i) for i in range(200)])
        chunk_size = DEFAULT_CHUNK_SIZE
        overlap = DEFAULT_CHUNK_OVERLAP
        chunks = chunk_canonical(text, chunk_size=chunk_size, overlap=overlap)
        # Hard invariant: coverage (PS-9 fix).
        covered: set[int] = set()
        for c in chunks:
            covered.update(range(c.start_codepoint, c.end_codepoint))
        missing = set(range(len(text))) - covered
        assert not missing, f"PS-9: {len(missing)} codepoints dropped; first at {min(missing)}"
        # Soft invariant: stride ≥ chunk_size - 2*overlap (new lower bound).
        min_stride = chunk_size - 2 * overlap
        for prev, curr in zip(chunks, chunks[1:]):
            stride = curr.start_codepoint - prev.start_codepoint
            assert stride >= min_stride, (
                f"Stride {stride} < {min_stride} (chunk_size - 2*overlap) between "
                f"chunk at {prev.start_codepoint} and {curr.start_codepoint}"
            )


# ===========================================================================
# W1-3: Unknown event type must NOT map to milestone.completed.v1 catch-all
# ===========================================================================


class TestUnknownEventTypeDisposition:
    """W1-3 / PS-13 / G-coverage: unrecognised claim event types are explicitly
    skipped, never silently mapped to ``milestone.completed.v1``."""

    def test_unknown_event_type_returns_none(self) -> None:
        """_shape_ledger_event must return None for types not in _CLAIM_TO_LEDGER_EVENT."""
        from src.core.rev.pipeline import _shape_ledger_event

        class _FakeClaim:
            event_type = "unicorn.event.that.does.not.exist"
            payload: dict = {}
            evidence_spans: tuple = ()

        identity = CanonicalItemIdentity(
            source_type=EntityType.MESSAGE, tenant_id="t",
            principal_mailbox="u@x.com", container="inbox", resource_id="msg-1",
        )
        hydrated = HydratedContent(
            identity=identity,
            canonical_text="some text",
            normalized_source_hash="sha256:x",
            chunks=(),
            route_metadata={"subject": "test", "sender": "a@b.com"},
        )
        result = _shape_ledger_event(_FakeClaim(), hydrated, NOW)
        assert result is None, (
            "W1-3 / PS-13: unknown event type must return None (not milestone fallback)"
        )

    def test_unknown_event_not_staged_as_candidate(self, tmp_path: Path) -> None:
        """_stage_candidates must not stage a candidate for an unknown event type."""
        from src.core.rev.pipeline import _stage_candidates

        identity = CanonicalItemIdentity(
            source_type=EntityType.MESSAGE, tenant_id="t",
            principal_mailbox="u@x.com", container="inbox", resource_id="msg-unknown",
        )
        hydrated = HydratedContent(
            identity=identity,
            canonical_text="Nothing extractable here.",
            normalized_source_hash="sha256:unk",
            chunks=(),
            route_metadata={
                "subject": "test",
                "sender": "a@b.com",
                "received_at": "2026-06-25T10:00:00Z",
            },
        )

        class _UnknownClaim:
            event_type = "future.event.not.in.v1"
            payload: dict = {}
            evidence_spans: tuple = ()
            extraction_confidence = 0.5
            extraction_model = "test"
            material = False

        staged, _ = _stage_candidates(
            program_id="nova-test",
            hydrated=hydrated,
            claims=(_UnknownClaim(),),
            evidence_refs=(),
            set_at=NOW,
            programs_root=tmp_path,
        )
        assert staged == [], (
            "W1-3: candidate must NOT be staged for an unknown event type "
            "(would create a false milestone.completed.v1 fact)"
        )

    def test_known_event_types_still_map_correctly(self) -> None:
        """Known event types are still shaped correctly after the catch-all removal."""
        from src.core.rev.pipeline import _shape_ledger_event, _CLAIM_TO_LEDGER_EVENT

        identity = CanonicalItemIdentity(
            source_type=EntityType.MESSAGE, tenant_id="t",
            principal_mailbox="u@x.com", container="inbox", resource_id="msg-1",
        )
        hydrated = HydratedContent(
            identity=identity,
            canonical_text="Deployment completed on 2026-06-25.",
            normalized_source_hash="sha256:x",
            chunks=(),
            route_metadata={"subject": "Update", "sender": "a@b.com", "conversation_id": "c1"},
        )
        for claim_type in _CLAIM_TO_LEDGER_EVENT:
            class _Claim:
                def __init__(self, et: str) -> None:
                    self.event_type = et
                    self.payload: dict = {"blocker_description": "dep", "severity": "high", "new_owner": "Alice"}
                    self.evidence_spans: tuple = ()

            result = _shape_ledger_event(_Claim(claim_type), hydrated, NOW)
            assert result is not None, (
                f"Known event type {claim_type!r} returned None from _shape_ledger_event"
            )
            et, payload = result
            assert isinstance(et, str) and et
            assert isinstance(payload, dict) and payload


# ===========================================================================
# W1-4 / W1-5: PII interception — no direct identifiers reach LLM prompt
# ===========================================================================


class TestPIIInterceptionContract:
    """W1-4 / W1-5 / G-pii: PII must be scrubbed before any external transmission.

    Covers:
    * LLM user-prompt builder scrubs the email subject (defense-in-depth).
    * EML hydrator canonical text and route_metadata subject are scrubbed.
    """

    def test_subject_pii_absent_from_llm_user_prompt(self) -> None:
        """W1-5: email address in subject must not appear in the LLM user prompt."""
        from src.ai.rev.extractor import _build_rev_extractor_user_prompt

        prompt = _build_rev_extractor_user_prompt(
            "Deployment completed on 2026-06-25.",
            subject="Meeting with alice@example.com",
            program_id="nova",
        )
        assert "alice@example.com" not in prompt, (
            "G-pii / W1-5: email address in subject leaked into LLM user prompt"
        )
        # Scrubbed subject placeholder should appear instead.
        assert "Email subject:" in prompt
        assert "nova" in prompt

    def test_phone_in_subject_absent_from_llm_prompt(self) -> None:
        """W1-5: phone number in subject must not appear in the LLM user prompt."""
        from src.ai.rev.extractor import _build_rev_extractor_user_prompt

        prompt = _build_rev_extractor_user_prompt(
            "Deployment completed.",
            subject="Call +1-555-123-4567 for status",
            program_id="nova",
        )
        assert "+1-555-123-4567" not in prompt, (
            "G-pii / W1-5: phone number in subject leaked into LLM user prompt"
        )

    def test_multiple_pii_types_scrubbed_from_subject(self) -> None:
        """W1-5: emails, phones, SSNs, card numbers all scrubbed from subject."""
        from src.ai.rev.extractor import _build_rev_extractor_user_prompt

        pii_subject = (
            "Contact alice@corp.com or +1-555-987-6543; "
            "SSN 123-45-6789; card 4111-1111-1111-1111"
        )
        prompt = _build_rev_extractor_user_prompt(
            "Status update.",
            subject=pii_subject,
            program_id="nova",
        )
        for pii in ("alice@corp.com", "+1-555-987-6543", "123-45-6789", "4111-1111-1111-1111"):
            assert pii not in prompt, (
                f"G-pii / W1-5: {pii!r} from subject leaked into LLM user prompt"
            )

    def test_eml_canonical_text_scrubbed(self, tmp_path: Path) -> None:
        """W1-4 / PS-24: EML hydrator canonical text is PII-scrubbed.

        Previously EML hydrator called normalize_whitespace + chunk_canonical
        directly, bypassing normalizer.normalize() → scrub_pii_and_credentials().
        After the fix, canonical text must not contain raw emails or phones.
        """
        from src.m365.rev.eml_hydrator import EmlHydrator

        eml_content = (
            "From: sender@external.com\r\n"
            "Subject: Update from alice@corp.com\r\n"
            "Date: Wed, 25 Jun 2026 10:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Deployment completed on 2026-06-25. "
            "Call +1-555-123-4567 for details. "
            "Contact bob@example.com if questions.\r\n"
        )
        eml_path = tmp_path / "pii_test.eml"
        eml_path.write_bytes(eml_content.encode("utf-8"))

        hydrator = EmlHydrator(
            mailbox_tenant_id="t",
            principal_mailbox="tpm@example.com",
        )
        candidate = EnumeratedCandidate(
            locator=HydrationLocator(
                source_type=EntityType.MESSAGE,
                tenant_id="t",
                principal_mailbox="tpm@example.com",
                container="inbox",
                resource_id="msg-pii-001",
            ),
            relevance_score=0.9,
            partial_metadata={"eml_path": str(eml_path), "message_id": "msg-pii-001"},
            correlation_id="pii-test",
            enumerator="test",
            received_at=NOW,
        )
        result = hydrator.hydrate(candidate, correlation_id="pii-test")
        assert is_success(result), f"EmlHydrator returned non-success: {result}"
        hydrated = result.value

        # Body PII must be scrubbed in canonical_text.
        assert "+1-555-123-4567" not in hydrated.canonical_text, (
            "W1-4 / PS-24: phone number leaked into canonical_text"
        )
        assert "bob@example.com" not in hydrated.canonical_text, (
            "W1-4 / PS-24: email address leaked into canonical_text"
        )
        # Non-PII signal (date) must be preserved.
        assert "2026-06-25" in hydrated.canonical_text, (
            "W1-4: deployment date should be preserved in canonical_text"
        )

    def test_eml_subject_scrubbed_in_route_metadata(self, tmp_path: Path) -> None:
        """W1-4 / PS-24: EML hydrator scrubs PII from route_metadata['subject']."""
        from src.m365.rev.eml_hydrator import EmlHydrator

        eml_content = (
            "From: sender@external.com\r\n"
            "Subject: Meeting with alice@corp.com on 2026-06-25\r\n"
            "Date: Wed, 25 Jun 2026 10:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Deployment completed on 2026-06-25.\r\n"
        )
        eml_path = tmp_path / "subj_pii.eml"
        eml_path.write_bytes(eml_content.encode("utf-8"))

        hydrator = EmlHydrator(
            mailbox_tenant_id="t",
            principal_mailbox="tpm@example.com",
        )
        candidate = EnumeratedCandidate(
            locator=HydrationLocator(
                source_type=EntityType.MESSAGE,
                tenant_id="t",
                principal_mailbox="tpm@example.com",
                container="inbox",
                resource_id="msg-subj-001",
            ),
            relevance_score=0.9,
            partial_metadata={"eml_path": str(eml_path), "message_id": "msg-subj-001"},
            correlation_id="subj-test",
            enumerator="test",
            received_at=NOW,
        )
        result = hydrator.hydrate(candidate, correlation_id="subj-test")
        assert is_success(result)
        hydrated = result.value

        subject_in_meta = hydrated.route_metadata.get("subject", "")
        assert "alice@corp.com" not in subject_in_meta, (
            "W1-4 / PS-24: email address leaked into route_metadata['subject']"
        )
        # Date in subject is preserved (dates are signal, not PII).
        assert "2026-06-25" in subject_in_meta


class TestPseudonymizationContract:
    """W5-3 / G-pii: person display names must be pseudonymised before LLM transmission.

    Covers:
    * display-name extraction from email headers
    * stable PERSON_N token assignment (same name → same token)
    * canonical text and chunks carry tokens, not raw names
    * pseudonym_table stored in route_metadata for entity binding
    * non-person signal (dates, work-item titles) preserved
    """

    def test_display_names_replaced_in_canonical_text(self, tmp_path: Path) -> None:
        """W5-3: display names from From/To headers must not appear in canonical_text."""
        from src.m365.rev.eml_hydrator import EmlHydrator

        eml_content = (
            "From: Alice Johnson <alice.johnson@contoso.com>\r\n"
            "To: Bob Williams <bob.williams@contoso.com>\r\n"
            "Subject: Deployment update\r\n"
            "Date: Wed, 25 Jun 2026 10:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Alice Johnson approved the milestone on 2026-06-25. "
            "Bob Williams will follow up next week.\r\n"
        )
        eml_path = tmp_path / "pseudo_test.eml"
        eml_path.write_bytes(eml_content.encode("utf-8"))

        hydrator = EmlHydrator(
            mailbox_tenant_id="t",
            principal_mailbox="tpm@example.com",
        )
        candidate = EnumeratedCandidate(
            locator=HydrationLocator(
                source_type=EntityType.MESSAGE,
                tenant_id="t",
                principal_mailbox="tpm@example.com",
                container="inbox",
                resource_id="msg-pseudo-001",
            ),
            relevance_score=0.9,
            partial_metadata={"eml_path": str(eml_path), "message_id": "msg-pseudo-001"},
            correlation_id="pseudo-test",
            enumerator="test",
            received_at=NOW,
        )
        result = hydrator.hydrate(candidate, correlation_id="pseudo-test")
        assert is_success(result), f"EmlHydrator returned non-success: {result}"
        hydrated = result.value

        # Raw display names must not appear in canonical text.
        assert "Alice Johnson" not in hydrated.canonical_text, (
            "W5-3: 'Alice Johnson' display name leaked into canonical_text"
        )
        assert "Bob Williams" not in hydrated.canonical_text, (
            "W5-3: 'Bob Williams' display name leaked into canonical_text"
        )
        # PERSON_N tokens must appear instead.
        assert "PERSON_1" in hydrated.canonical_text or "PERSON_2" in hydrated.canonical_text, (
            "W5-3: expected PERSON_N token in canonical_text"
        )
        # Non-PII signal (dates) must be preserved.
        assert "2026-06-25" in hydrated.canonical_text, (
            "W5-3: deployment date should be preserved after pseudonymization"
        )

    def test_pseudonym_table_stored_in_route_metadata(self, tmp_path: Path) -> None:
        """W5-3: route_metadata['pseudonym_table'] maps PERSON_N → original name."""
        from src.m365.rev.eml_hydrator import EmlHydrator

        eml_content = (
            "From: Carol Davis <carol.davis@contoso.com>\r\n"
            "To: Dave Evans <dave.evans@contoso.com>\r\n"
            "Subject: Status\r\n"
            "Date: Wed, 25 Jun 2026 10:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Carol Davis raised a risk for Dave Evans to review.\r\n"
        )
        eml_path = tmp_path / "pseudo_meta.eml"
        eml_path.write_bytes(eml_content.encode("utf-8"))

        hydrator = EmlHydrator(
            mailbox_tenant_id="t",
            principal_mailbox="tpm@example.com",
        )
        candidate = EnumeratedCandidate(
            locator=HydrationLocator(
                source_type=EntityType.MESSAGE,
                tenant_id="t",
                principal_mailbox="tpm@example.com",
                container="inbox",
                resource_id="msg-pseudo-002",
            ),
            relevance_score=0.9,
            partial_metadata={"eml_path": str(eml_path), "message_id": "msg-pseudo-002"},
            correlation_id="pseudo-meta",
            enumerator="test",
            received_at=NOW,
        )
        result = hydrator.hydrate(candidate, correlation_id="pseudo-meta")
        assert is_success(result)
        hydrated = result.value

        table = hydrated.route_metadata.get("pseudonym_table")
        assert isinstance(table, dict), (
            "W5-3: route_metadata['pseudonym_table'] must be a dict"
        )
        # All values must be non-empty strings (original display names).
        assert all(isinstance(v, str) and v for v in table.values()), (
            "W5-3: pseudonym_table values must be non-empty original display names"
        )
        # All keys must be PERSON_N tokens.
        assert all(k.startswith("PERSON_") for k in table), (
            "W5-3: pseudonym_table keys must be PERSON_N tokens"
        )
        # Known names must be in the table.
        all_names = set(table.values())
        assert "Carol Davis" in all_names or "Dave Evans" in all_names, (
            "W5-3: known display names must appear as values in pseudonym_table"
        )

    def test_same_name_gets_same_token_within_document(self) -> None:
        """W5-3: a name appearing twice gets the same PERSON_N token (stable)."""
        from src.core.rev.privacy import build_pseudonym_table_from_display_names, pseudonymize_text

        names = ["Eve Harper", "Frank Hill"]
        table = build_pseudonym_table_from_display_names(names)

        text = "Eve Harper spoke. Frank Hill responded. Eve Harper agreed."
        result = pseudonymize_text(text, table)

        # All occurrences of "Eve Harper" must use the same token.
        import re
        eve_tokens = re.findall(r"PERSON_\d+", result)
        assert len(eve_tokens) == 3, f"Expected 3 PERSON_N tokens, got {len(eve_tokens)}: {result!r}"
        # First and third tokens must be the same (both "Eve Harper").
        assert eve_tokens[0] == eve_tokens[2], (
            "W5-3: same name must get the same token across occurrences"
        )
        # Second token (Frank Hill) must be different.
        assert eve_tokens[0] != eve_tokens[1], (
            "W5-3: different names must get different tokens"
        )

    def test_normalize_with_known_display_names(self) -> None:
        """W5-3: normalizer.normalize() replaces known display names with tokens."""
        from src.core.rev.normalizer import normalize

        text = "Grace Kelly approved the milestone on 2026-06-25."
        result = normalize(text, is_html=False, known_display_names=["Grace Kelly"])

        assert "Grace Kelly" not in result.canonical_text, (
            "W5-3: display name must be pseudonymized in canonical_text"
        )
        assert "PERSON_1" in result.canonical_text, (
            "W5-3: PERSON_1 token expected in canonical_text"
        )
        assert "2026-06-25" in result.canonical_text, (
            "W5-3: deployment date must be preserved"
        )
        assert result.pseudonym_table == {"PERSON_1": "Grace Kelly"}, (
            f"W5-3: unexpected pseudonym_table: {result.pseudonym_table!r}"
        )

    def test_normalize_without_display_names_returns_none_table(self) -> None:
        """W5-3 backward compat: normalize() without display names returns pseudonym_table=None."""
        from src.core.rev.normalizer import normalize

        result = normalize("Hello world.", is_html=False)
        assert result.pseudonym_table is None, (
            "W5-3 backward compat: pseudonym_table must be None when no names given"
        )

    def test_single_word_names_not_pseudonymized(self) -> None:
        """W5-3: single-word names (aliases, role names) are not pseudonymized."""
        from src.core.rev.privacy import build_pseudonym_table_from_display_names

        table = build_pseudonym_table_from_display_names(["Alice", "Bob", "TPM"])
        assert table.is_empty, (
            "W5-3: single-word names must not be added to the pseudonym table"
        )
