"""WI-7.1b: Contract tests — claim-derived actuation proposals (§6.11.4).

Three required contract tests per spec:
  - ref-required (free text never proposes)
  - commitment proposal
  - ambiguous-direction-asks

Plus supplementary coverage for direction inference, opt-in gate, and Zone A
purity.
"""
from __future__ import annotations

import ast
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core.claim_actuation import (
    infer_commitment_direction,
    is_commitment_shaped,
    load_claim_actuation_enabled,
    propose_from_claims,
)
from src.core.program_reality import ActuationProposal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_claim(
    *,
    entity_refs: tuple[str, ...] = (),
    owner_alias: str | None = None,
    due_date: date | None = None,
    text: str = "some narrative text",
    claim_id: str = "c-001",
    program_id: str = "test_prog",
) -> MagicMock:
    claim = MagicMock()
    claim.entity_refs = entity_refs
    claim.owner_alias = owner_alias
    claim.due_date = due_date
    claim.text = text
    claim.id = claim_id
    claim.program_id = program_id
    return claim


def _make_registry(
    *,
    entity_type: str | None = None,
) -> MagicMock:
    """Return a registry mock that resolves any alias to an entity of given type."""
    registry = MagicMock()
    if entity_type is None:
        registry.resolve.return_value = None
    else:
        entity = MagicMock()
        entity.entity_type = entity_type
        registry.resolve.return_value = entity
    return registry


_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Required: ref-required (free text never proposes)
# ---------------------------------------------------------------------------

class TestRefRequired:
    def test_free_text_claim_no_entity_refs_never_proposes(self) -> None:
        """A claim with empty entity_refs must never produce a proposal."""
        claim = _make_claim(entity_refs=())
        registry = _make_registry(entity_type="person")
        result = propose_from_claims(
            (claim,), registry, "prog", enabled=True, as_of=_NOW
        )
        assert result == ()

    def test_free_text_with_owner_and_due_date_still_never_proposes(self) -> None:
        """Even a commitment-shaped claim without entity_refs must not propose."""
        claim = _make_claim(
            entity_refs=(),
            owner_alias="alice",
            due_date=date(2025, 9, 1),
        )
        registry = _make_registry(entity_type="person")
        result = propose_from_claims(
            (claim,), registry, "prog", enabled=True, as_of=_NOW
        )
        assert result == ()

    def test_claim_with_entity_refs_produces_proposal(self) -> None:
        """A claim with at least one entity_ref produces a proposal when enabled."""
        claim = _make_claim(entity_refs=("WI:12345",))
        registry = _make_registry(entity_type=None)
        result = propose_from_claims(
            (claim,), registry, "prog", enabled=True, as_of=_NOW
        )
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Required: commitment proposal
# ---------------------------------------------------------------------------

class TestCommitmentProposal:
    def test_commitment_shaped_claim_proposes_commitment_entry_create(self) -> None:
        """A claim with owner_alias and due_date proposes commitment_entry_create."""
        claim = _make_claim(
            entity_refs=("WI:100",),
            owner_alias="teamA",
            due_date=date(2025, 10, 15),
        )
        registry = _make_registry(entity_type="team")
        result = propose_from_claims(
            (claim,), registry, "prog", enabled=True, as_of=_NOW
        )
        assert len(result) == 1
        proposal = result[0]
        assert isinstance(proposal, ActuationProposal)
        assert proposal.operation == "commitment_entry_create"
        assert proposal.payload["source_type"] == "claim_extraction"
        assert proposal.payload["owner_alias"] == "teamA"
        assert proposal.payload["due_date"] == "2025-10-15"
        assert proposal.rule_id == "claim_extraction"
        assert proposal.adapter == "claim"

    def test_commitment_proposal_payload_has_claim_id_and_text(self) -> None:
        """Claim identity and text are surfaced in the proposal payload."""
        claim = _make_claim(
            entity_refs=("WI:200",),
            owner_alias="alice",
            due_date=date(2025, 11, 1),
            claim_id="c-test-99",
            text="Alice will deliver report by Q3.",
        )
        registry = _make_registry(entity_type="person")
        result = propose_from_claims(
            (claim,), registry, "prog", enabled=True, as_of=_NOW
        )
        assert len(result) == 1
        assert result[0].payload["claim_id"] == "c-test-99"
        assert result[0].payload["claim_text"] == "Alice will deliver report by Q3."

    def test_regular_claim_proposes_action_item(self) -> None:
        """A claim with entity_refs but no owner/due_date proposes action_item."""
        claim = _make_claim(entity_refs=("WI:300",))
        registry = _make_registry(entity_type=None)
        result = propose_from_claims(
            (claim,), registry, "prog", enabled=True, as_of=_NOW
        )
        assert len(result) == 1
        assert result[0].operation == "action_item"
        assert result[0].payload["source_type"] == "claim_extraction"

    def test_missing_owner_claim_not_commitment_shaped(self) -> None:
        """A claim with due_date but no owner is not commitment-shaped."""
        claim = _make_claim(
            entity_refs=("WI:400",),
            due_date=date(2025, 12, 1),
        )
        registry = _make_registry(entity_type="person")
        result = propose_from_claims(
            (claim,), registry, "prog", enabled=True, as_of=_NOW
        )
        assert result[0].operation == "action_item"

    def test_outbound_direction_internal_team(self) -> None:
        """Owner resolving to internal team entity → direction=outbound."""
        claim = _make_claim(
            entity_refs=("WI:500",),
            owner_alias="my_team",
            due_date=date(2025, 10, 1),
        )
        registry = _make_registry(entity_type="team")
        result = propose_from_claims(
            (claim,), registry, "prog", enabled=True, as_of=_NOW
        )
        assert result[0].payload["direction"] == "outbound"
        assert "direction_ambiguous" not in result[0].payload

    def test_inbound_direction_external_vendor(self) -> None:
        """Owner resolving to vendor entity → direction=inbound."""
        claim = _make_claim(
            entity_refs=("WI:600",),
            owner_alias="acme_corp",
            due_date=date(2025, 10, 1),
        )
        registry = _make_registry(entity_type="vendor")
        result = propose_from_claims(
            (claim,), registry, "prog", enabled=True, as_of=_NOW
        )
        assert result[0].payload["direction"] == "inbound"

    def test_inbound_direction_external_partner(self) -> None:
        """Owner resolving to partner entity → direction=inbound."""
        claim = _make_claim(
            entity_refs=("WI:601",),
            owner_alias="partner_x",
            due_date=date(2025, 10, 1),
        )
        registry = _make_registry(entity_type="partner")
        result = propose_from_claims(
            (claim,), registry, "prog", enabled=True, as_of=_NOW
        )
        assert result[0].payload["direction"] == "inbound"

    def test_approval_ttl_hours_propagated(self) -> None:
        """approval_ttl_hours parameter is carried through to proposal payload."""
        claim = _make_claim(entity_refs=("WI:700",))
        registry = _make_registry(entity_type=None)
        result = propose_from_claims(
            (claim,), registry, "prog", enabled=True,
            approval_ttl_hours=48, as_of=_NOW,
        )
        assert result[0].payload["approval_ttl_hours"] == 48


# ---------------------------------------------------------------------------
# Required: ambiguous-direction-asks
# ---------------------------------------------------------------------------

class TestAmbiguousDirectionAsks:
    def test_unresolvable_owner_direction_ambiguous(self) -> None:
        """Unresolvable owner_alias → direction=ambiguous, direction_ambiguous=True."""
        claim = _make_claim(
            entity_refs=("WI:800",),
            owner_alias="unknown_party",
            due_date=date(2025, 10, 1),
        )
        registry = _make_registry(entity_type=None)  # resolves to None
        result = propose_from_claims(
            (claim,), registry, "prog", enabled=True, as_of=_NOW
        )
        assert len(result) == 1
        assert result[0].payload["direction"] == "ambiguous"
        assert result[0].payload.get("direction_ambiguous") is True

    def test_none_owner_alias_direction_ambiguous(self) -> None:
        """None owner_alias → direction=ambiguous."""
        assert infer_commitment_direction(None, _make_registry(entity_type="person")) == "ambiguous"

    def test_empty_owner_alias_direction_ambiguous(self) -> None:
        """Empty string owner_alias → direction=ambiguous."""
        assert infer_commitment_direction("", _make_registry(entity_type="person")) == "ambiguous"

    def test_none_registry_direction_ambiguous(self) -> None:
        """None registry → direction=ambiguous (safe degradation)."""
        assert infer_commitment_direction("alice", None) == "ambiguous"

    def test_unknown_entity_type_direction_ambiguous(self) -> None:
        """Entity with unknown type → direction=ambiguous."""
        direction = infer_commitment_direction("alice", _make_registry(entity_type="widget"))
        assert direction == "ambiguous"

    def test_ambiguous_proposal_still_contains_proposal(self) -> None:
        """Ambiguous direction still produces a proposal (not suppressed)."""
        claim = _make_claim(
            entity_refs=("WI:900",),
            owner_alias="mystery_corp",
            due_date=date(2025, 10, 1),
        )
        registry = _make_registry(entity_type=None)
        result = propose_from_claims(
            (claim,), registry, "prog", enabled=True, as_of=_NOW
        )
        assert len(result) == 1
        assert result[0].operation == "commitment_entry_create"


# ---------------------------------------------------------------------------
# Opt-in gate
# ---------------------------------------------------------------------------

class TestOptInGate:
    def test_disabled_by_default_returns_empty(self) -> None:
        """enabled=False returns empty tuple regardless of claim content."""
        claim = _make_claim(
            entity_refs=("WI:1000",),
            owner_alias="teamA",
            due_date=date(2025, 10, 1),
        )
        result = propose_from_claims(
            (claim,), _make_registry(entity_type="team"), "prog",
            enabled=False, as_of=_NOW,
        )
        assert result == ()

    def test_empty_claims_returns_empty(self) -> None:
        """Empty claims tuple returns empty regardless of enabled."""
        result = propose_from_claims(
            (), _make_registry(entity_type="team"), "prog",
            enabled=True, as_of=_NOW,
        )
        assert result == ()

    def test_load_claim_actuation_enabled_missing_file(self, tmp_path: Path) -> None:
        """Missing policy file → disabled (safe default)."""
        result = load_claim_actuation_enabled("no_such_program", programs_root=tmp_path)
        assert result is False

    def test_load_claim_actuation_enabled_explicit_true(self, tmp_path: Path) -> None:
        """Policy file with enabled: true → returns True."""
        prog_dir = tmp_path / "my_prog" / "policies"
        prog_dir.mkdir(parents=True)
        (prog_dir / "claim_actuation.yaml").write_text("enabled: true\n")
        assert load_claim_actuation_enabled("my_prog", programs_root=tmp_path) is True

    def test_load_claim_actuation_enabled_explicit_false(self, tmp_path: Path) -> None:
        """Policy file with enabled: false → returns False."""
        prog_dir = tmp_path / "my_prog" / "policies"
        prog_dir.mkdir(parents=True)
        (prog_dir / "claim_actuation.yaml").write_text("enabled: false\n")
        assert load_claim_actuation_enabled("my_prog", programs_root=tmp_path) is False


# ---------------------------------------------------------------------------
# Direction inference unit tests
# ---------------------------------------------------------------------------

class TestDirectionInference:
    @pytest.mark.parametrize("entity_type", ["person", "team", "internal_team", "dri"])
    def test_internal_types_outbound(self, entity_type: str) -> None:
        assert infer_commitment_direction("x", _make_registry(entity_type=entity_type)) == "outbound"

    @pytest.mark.parametrize("entity_type", ["external_team", "external_person", "vendor", "partner", "customer"])
    def test_external_types_inbound(self, entity_type: str) -> None:
        assert infer_commitment_direction("x", _make_registry(entity_type=entity_type)) == "inbound"


# ---------------------------------------------------------------------------
# Multiple claims
# ---------------------------------------------------------------------------

class TestMultipleClaims:
    def test_multiple_claims_all_proposed(self) -> None:
        """Each claim with entity_refs produces one proposal."""
        claims = tuple(
            _make_claim(entity_refs=(f"WI:{i}",), claim_id=f"c-{i}")
            for i in range(5)
        )
        registry = _make_registry(entity_type=None)
        result = propose_from_claims(claims, registry, "prog", enabled=True, as_of=_NOW)
        assert len(result) == 5

    def test_mixed_claims_filters_free_text(self) -> None:
        """Only claims with entity_refs produce proposals."""
        claims = (
            _make_claim(entity_refs=("WI:1",), claim_id="with-ref"),
            _make_claim(entity_refs=(), claim_id="free-text"),
            _make_claim(entity_refs=("WI:3",), claim_id="also-with-ref"),
        )
        result = propose_from_claims(claims, None, "prog", enabled=True, as_of=_NOW)
        assert len(result) == 2

    def test_proposal_ids_unique(self) -> None:
        """Each proposal gets a distinct UUID."""
        claims = tuple(
            _make_claim(entity_refs=(f"WI:{i}",), claim_id=f"c-{i}")
            for i in range(3)
        )
        result = propose_from_claims(claims, None, "prog", enabled=True, as_of=_NOW)
        ids = {p.proposal_id for p in result}
        assert len(ids) == 3


# ---------------------------------------------------------------------------
# is_commitment_shaped unit tests
# ---------------------------------------------------------------------------

class TestIsCommitmentShaped:
    def test_with_both_fields(self) -> None:
        claim = _make_claim(owner_alias="alice", due_date=date(2025, 10, 1))
        assert is_commitment_shaped(claim) is True

    def test_missing_owner(self) -> None:
        claim = _make_claim(due_date=date(2025, 10, 1))
        assert is_commitment_shaped(claim) is False

    def test_missing_due_date(self) -> None:
        claim = _make_claim(owner_alias="alice")
        assert is_commitment_shaped(claim) is False

    def test_both_none(self) -> None:
        claim = _make_claim()
        assert is_commitment_shaped(claim) is False


# ---------------------------------------------------------------------------
# Zone A purity
# ---------------------------------------------------------------------------

class TestZoneAPurity:
    def test_claim_actuation_no_ai_imports(self) -> None:
        """Zone A: claim_actuation.py must not import from src.ai or src.m365."""
        source_path = Path(__file__).resolve().parents[2] / "src" / "core" / "claim_actuation.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = ""
                if isinstance(node, ast.ImportFrom) and node.module:
                    module = node.module
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        assert not alias.name.startswith("src.ai"), (
                            f"Zone A violation: import src.ai in claim_actuation.py: {alias.name}"
                        )
                        assert not alias.name.startswith("src.m365"), (
                            f"Zone A violation: import src.m365 in claim_actuation.py: {alias.name}"
                        )
                        continue
                if module.startswith("src.ai"):
                    raise AssertionError(
                        f"Zone A violation: import {module} in claim_actuation.py"
                    )
                if module.startswith("src.m365"):
                    raise AssertionError(
                        f"Zone A violation: import {module} in claim_actuation.py"
                    )
