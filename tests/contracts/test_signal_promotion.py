"""WI-3.2a contract tests: signal_promotion.py

Contracts:
1. AST scan — write_confirmed is NEVER called in signal_promotion.py
2. Provisional sources produce review_state=PROPOSED facts
3. Non-provisional sources produce review_state=ACCEPTED facts
4. Suspended sources are blocked (return action="suspended")
5. Re-promoting same observation emits fact.reconfirmation (action="reconfirmed")
6. `batch_promote_observations` promotes multiple observations
7. `is_provisional_signal` classifies correctly
"""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from src.core.program_fact_store import FactReviewState

_MODULE_PATH = Path(__file__).resolve().parents[2] / "src" / "core" / "signal_promotion.py"


# ---------------------------------------------------------------------------
# AST contract: write_confirmed must never appear in signal_promotion.py
# ---------------------------------------------------------------------------


class TestNoWriteConfirmedInSignalPromotion:
    def test_write_confirmed_not_called(self) -> None:
        """signal_promotion.py must never call write_confirmed (INV-2 enforcement)."""
        source = _MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "write_confirmed"
        ]
        assert calls == [], (
            f"signal_promotion.py calls write_confirmed at lines "
            f"{[c.lineno for c in calls]} — this is FORBIDDEN (INV-2)"
        )

    def test_write_confirmed_not_imported(self) -> None:
        """write_confirmed must not be imported either."""
        source = _MODULE_PATH.read_text(encoding="utf-8")
        assert "write_confirmed" not in source, (
            "signal_promotion.py imports or references write_confirmed — FORBIDDEN"
        )


# ---------------------------------------------------------------------------
# is_provisional_signal
# ---------------------------------------------------------------------------


class TestIsProvisionalSignal:
    def test_provisional_families(self) -> None:
        from src.core.signal_promotion import is_provisional_signal

        for family in ("workiq", "teams", "transcript", "human_comms"):
            assert is_provisional_signal(family), f"{family} should be provisional"

    def test_non_provisional_families(self) -> None:
        from src.core.signal_promotion import is_provisional_signal

        for family in ("ado", "kusto", "icm", "pagerduty"):
            assert not is_provisional_signal(family), f"{family} should NOT be provisional"

    def test_case_insensitive(self) -> None:
        from src.core.signal_promotion import is_provisional_signal

        assert is_provisional_signal("WORKIQ")
        assert is_provisional_signal("Teams")
        assert not is_provisional_signal("ADO")


# ---------------------------------------------------------------------------
# promote_observation (in-memory / temp DB)
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path


class TestPromoteObservation:
    def test_non_provisional_review_state_accepted(self, tmp_db: Path) -> None:
        from src.core.signal_promotion import promote_observation

        result = promote_observation(
            program_id="test_prog",
            fact_type="action.item",
            entity_refs=("action:test_action_1",),
            payload={"title": "Test action", "status": "open"},
            source_family="ado",
            db_root=tmp_db,
        )
        assert result.action == "created"
        assert result.fact_write is not None
        assert result.fact_write.revision.review_state == FactReviewState.ACCEPTED

    def test_provisional_review_state_proposed(self, tmp_db: Path) -> None:
        from src.core.signal_promotion import promote_observation

        result = promote_observation(
            program_id="test_prog",
            fact_type="action.item",
            entity_refs=("action:teams_action_1",),
            payload={"title": "Teams action", "status": "open"},
            source_family="teams",
            db_root=tmp_db,
        )
        assert result.action == "created"
        assert result.fact_write is not None
        assert result.fact_write.revision.review_state == FactReviewState.PROPOSED

    def test_suspended_source_blocked(self, tmp_db: Path) -> None:
        from src.core.signal_promotion import promote_observation
        from src.core.truth_model import TruthContext

        truth_ctx = TruthContext(
            baseline_locked_keys=frozenset(),
            corroborated_keys=frozenset(),
            suspended_sources=frozenset({"ado"}),
        )
        result = promote_observation(
            program_id="test_prog",
            fact_type="action.item",
            entity_refs=("action:suspended_1",),
            payload={"title": "Should not be promoted"},
            source_family="ado",
            truth_ctx=truth_ctx,
            db_root=tmp_db,
        )
        assert result.action == "suspended"
        assert result.fact_write is None

    def test_reconfirmation_on_second_promote(self, tmp_db: Path) -> None:
        from src.core.signal_promotion import promote_observation

        common_kwargs = dict(
            program_id="test_prog",
            fact_type="action.item",
            entity_refs=("action:reconf_test_1",),
            payload={"title": "Repeated action", "status": "open"},
            source_family="ado",
            db_root=tmp_db,
        )
        first = promote_observation(**common_kwargs)
        assert first.action == "created"

        second = promote_observation(**common_kwargs)
        assert second.action == "reconfirmed"
        assert second.reconfirmation_write is not None
        assert second.reconfirmation_write.revision.fact_type == "fact.reconfirmation"

    def test_natural_key_in_result(self, tmp_db: Path) -> None:
        from src.core.signal_promotion import promote_observation

        result = promote_observation(
            program_id="test_prog",
            fact_type="risk.entry",
            entity_refs=("risk:R001",),
            payload={"title": "Risk"},
            source_family="ado",
            db_root=tmp_db,
        )
        assert result.natural_key != ""
        assert isinstance(result.natural_key, str)

    def test_non_suspended_source_not_blocked(self, tmp_db: Path) -> None:
        from src.core.signal_promotion import promote_observation
        from src.core.truth_model import TruthContext

        truth_ctx = TruthContext(
            baseline_locked_keys=frozenset(),
            corroborated_keys=frozenset(),
            suspended_sources=frozenset({"kusto"}),  # ado is NOT suspended
        )
        result = promote_observation(
            program_id="test_prog",
            fact_type="action.item",
            entity_refs=("action:ok_src",),
            payload={"title": "Fine"},
            source_family="ado",
            truth_ctx=truth_ctx,
            db_root=tmp_db,
        )
        assert result.action in ("created", "noop", "reconfirmed")  # not suspended


# ---------------------------------------------------------------------------
# batch_promote_observations
# ---------------------------------------------------------------------------


class TestBatchPromoteObservations:
    def test_batch_length_matches_input(self, tmp_db: Path) -> None:
        from src.core.signal_promotion import batch_promote_observations

        observations = [
            {
                "fact_type": "action.item",
                "entity_refs": [f"action:batch_{i}"],
                "payload": {"title": f"Action {i}"},
                "source_family": "ado",
            }
            for i in range(3)
        ]
        results = batch_promote_observations(
            observations, program_id="test_batch_prog", db_root=tmp_db
        )
        assert len(results) == 3

    def test_batch_respects_provisional_state(self, tmp_db: Path) -> None:
        from src.core.signal_promotion import batch_promote_observations

        observations = [
            {
                "fact_type": "action.item",
                "entity_refs": ["action:batch_human"],
                "payload": {"title": "Human comms action"},
                "source_family": "human_comms",
            }
        ]
        results = batch_promote_observations(
            observations, program_id="test_batch_prog", db_root=tmp_db
        )
        assert results[0].fact_write is not None
        assert results[0].fact_write.revision.review_state == FactReviewState.PROPOSED
