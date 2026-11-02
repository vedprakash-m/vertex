"""ADF-W5.12 (Appendix A.8): the proposal_classes block additively
extending src/core/maturity_engine.py's earned_autonomy_state.yaml."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.core.exceptions import ConfigError
import src.core.maturity_engine as maturity_engine

from src.core.maturity_engine import (
    EarnedAutonomyState,
    ProposalClassAutonomyState,
    ProposalClassCounters,
    load_earned_autonomy_state,
    try_advance_earned_autonomy,
    write_proposal_class_state,
)

_NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


def test_absent_proposal_classes_block_defaults_to_empty(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert load_earned_autonomy_state("samplealpha", programs_root=programs_root) is None


def test_write_proposal_class_state_round_trips(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    entry = ProposalClassAutonomyState(
        level="l2",
        promoted_at=_NOW,
        demoted_at=None,
        last_change_reason="test promotion",
        evidence_window_start=_NOW,
        counters=ProposalClassCounters(proposals=12, accepted=11, rejected=1),
        sample_rate=0.5,
    )
    write_proposal_class_state("samplealpha", "risk", entry, programs_root=programs_root)

    state = load_earned_autonomy_state("samplealpha", programs_root=programs_root)
    assert state is not None
    assert state.proposal_classes["risk"] == entry


def test_write_proposal_class_state_preserves_other_classes(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    entry_a = ProposalClassAutonomyState(
        level="l1", promoted_at=_NOW, demoted_at=None, last_change_reason="a",
        evidence_window_start=_NOW,
    )
    entry_b = ProposalClassAutonomyState(
        level="l2", promoted_at=_NOW, demoted_at=None, last_change_reason="b",
        evidence_window_start=_NOW,
    )
    write_proposal_class_state("samplealpha", "risk", entry_a, programs_root=programs_root)
    write_proposal_class_state("samplealpha", "meeting_action", entry_b, programs_root=programs_root)

    state = load_earned_autonomy_state("samplealpha", programs_root=programs_root)
    assert state.proposal_classes["risk"].level == "l1"
    assert state.proposal_classes["meeting_action"].level == "l2"


def test_write_proposal_class_state_preserves_existing_global_tier_fields(tmp_path: Path) -> None:
    """FR-SG-39's earned_tier/maturity_score must survive a proposal_classes
    write untouched -- these are a materially different, older mechanism."""
    programs_root = tmp_path / "programs"
    try_advance_earned_autonomy("samplealpha", programs_root=programs_root)  # initializes earned_tier=0
    before = load_earned_autonomy_state("samplealpha", programs_root=programs_root)

    entry = ProposalClassAutonomyState(
        level="l1", promoted_at=_NOW, demoted_at=None, last_change_reason="x",
        evidence_window_start=_NOW,
    )
    write_proposal_class_state("samplealpha", "risk", entry, programs_root=programs_root)

    after = load_earned_autonomy_state("samplealpha", programs_root=programs_root)
    assert after.earned_tier == before.earned_tier
    assert after.maturity_score == before.maturity_score
    assert after.proposal_classes["risk"].level == "l1"


def test_default_counters_are_all_zero() -> None:
    counters = ProposalClassCounters()
    assert counters.proposals == counters.accepted == counters.edited == 0
    assert counters.rejected == counters.reversals == counters.material_errors == 0


@pytest.mark.parametrize("content", [
    b"proposal_classes: [\n", b"\xff", b"- invalid\n",
    b"earned_tier: private-marker\n", b"maturity_score: private-marker\n",
    b"proposal_classes: invalid\n", b"proposal_classes:\n  risk: invalid\n",
    b"proposal_classes:\n  risk:\n    counters: invalid\n",
    b"proposal_classes:\n  risk:\n    counters:\n      accepted: private-marker\n",
    b"proposal_classes:\n  risk:\n    sample_rate: private-marker\n",
    b"earned_tier: .inf\n", b"promoted_at: 42\n",
    b"proposal_classes:\n  risk:\n    promoted_at: 42\n",
])
def test_invalid_earned_state_is_safe_config_error(tmp_path: Path, content: bytes) -> None:
    path = maturity_engine.get_earned_autonomy_state_path("samplealpha", programs_root=tmp_path)
    path.parent.mkdir()
    path.write_bytes(content)
    with pytest.raises(ConfigError) as raised:
        load_earned_autonomy_state("samplealpha", programs_root=tmp_path)
    assert "earned_autonomy_state.yaml" in str(raised.value)
    assert "samplealpha" in str(raised.value)
    assert "private-marker" not in str(raised.value)
    assert str(tmp_path) not in str(raised.value)
    assert path.read_bytes() == content


def test_earned_state_directory_is_safe_config_error(tmp_path: Path) -> None:
    path = maturity_engine.get_earned_autonomy_state_path("samplealpha", programs_root=tmp_path)
    path.mkdir(parents=True)
    with pytest.raises(ConfigError):
        load_earned_autonomy_state("samplealpha", programs_root=tmp_path)
    assert path.is_dir()


@pytest.mark.parametrize("content", ["", "{}", "proposal_classes: {}", "proposal_classes: null"])
def test_earned_state_empty_defaults_stay_compatible(tmp_path: Path, content: str) -> None:
    path = maturity_engine.get_earned_autonomy_state_path("samplealpha", programs_root=tmp_path)
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")
    state = load_earned_autonomy_state("samplealpha", programs_root=tmp_path)
    assert state is not None and state.proposal_classes == {}
    assert state.earned_tier == 0 and state.maturity_score == 0.0


def test_earned_state_unexpected_reader_error_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = maturity_engine.get_earned_autonomy_state_path("samplealpha", programs_root=tmp_path)
    path.parent.mkdir()
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(maturity_engine.yaml, "safe_load", Mock(side_effect=RuntimeError("implementation")))
    with pytest.raises(RuntimeError):
        load_earned_autonomy_state("samplealpha", programs_root=tmp_path)


def test_earned_state_stat_error_is_safe_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    path = Mock()
    path.stat.side_effect = OSError("private-marker")
    monkeypatch.setattr(maturity_engine, "get_earned_autonomy_state_path", lambda *a, **k: path)
    with pytest.raises(ConfigError) as raised:
        load_earned_autonomy_state("samplealpha")
    assert "OSError" in str(raised.value) and "private-marker" not in str(raised.value)
    path.read_text.assert_not_called()


def test_yaml_timestamp_constructor_error_is_normalized(tmp_path: Path) -> None:
    content = "last_evaluated_at: 2026-13-01\n"
    with pytest.raises(ValueError):
        maturity_engine.yaml.safe_load(content)
    path = maturity_engine.get_earned_autonomy_state_path("samplealpha", programs_root=tmp_path)
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError) as raised:
        load_earned_autonomy_state("samplealpha", programs_root=tmp_path)
    assert isinstance(raised.value.__cause__, ValueError)
    assert "2026-13-01" not in str(raised.value)
    assert path.read_text(encoding="utf-8") == content


def test_unexpected_read_text_value_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    path = Mock()
    error = ValueError("unexpected reader bug")
    path.read_text.side_effect = error
    monkeypatch.setattr(maturity_engine, "get_earned_autonomy_state_path", lambda *a, **k: path)
    with pytest.raises(ValueError) as raised:
        load_earned_autonomy_state("samplealpha")
    assert raised.value is error


def test_earned_state_existing_date_and_pair_conversion_defaults_remain(tmp_path: Path) -> None:
    path = maturity_engine.get_earned_autonomy_state_path("samplealpha", programs_root=tmp_path)
    path.parent.mkdir()
    path.write_text(
        "promoted_at: 'invalid-existing-date'\nproposal_classes:\n"
        "  - [risk, {level: l1, counters: null, evidence_window_start: 'invalid-existing-date'}]\n",
        encoding="utf-8",
    )
    state = load_earned_autonomy_state("samplealpha", programs_root=tmp_path)
    assert state.promoted_at is not None and state.promoted_at.tzinfo is timezone.utc
    assert state.proposal_classes["risk"].level == "l1"
    assert state.proposal_classes["risk"].counters == ProposalClassCounters()


@pytest.mark.parametrize("caller", ["write", "advance", "promote", "demote", "review", "global"])
def test_invalid_earned_state_refuses_actual_writer_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller: str,
) -> None:
    from src.core import proposal_autonomy_ladder as ladder
    from src.commands import ai_proposals

    path = maturity_engine.get_earned_autonomy_state_path("samplealpha", programs_root=tmp_path)
    path.parent.mkdir()
    content = b"proposal_classes: [\n"
    path.write_bytes(content)
    writer = Mock(side_effect=AssertionError("No replacement allowed"))
    monkeypatch.setattr(maturity_engine, "_write_earned_autonomy_state", writer)
    audit = Mock(side_effect=AssertionError("No approval allowed"))
    monkeypatch.setattr(ai_proposals, "load_proposals", audit)
    calls = {
        "write": lambda: write_proposal_class_state(
            "samplealpha", "risk",
            ProposalClassAutonomyState("l1", None, None, "", _NOW), programs_root=tmp_path,
        ),
        "advance": lambda: ladder.advance_proposal_class_autonomy("samplealpha", "risk", now=_NOW, programs_root=tmp_path),
        "promote": lambda: ladder.promote_proposal_class_explicit("samplealpha", "risk", "l1", "manual", now=_NOW, programs_root=tmp_path),
        "demote": lambda: ladder.demote_proposal_class_explicit("samplealpha", "risk", "manual", now=_NOW, programs_root=tmp_path),
        "review": lambda: ai_proposals.run_review_batch("samplealpha", "risk", programs_root=tmp_path),
        "global": lambda: try_advance_earned_autonomy("samplealpha", programs_root=tmp_path),
    }
    with pytest.raises(ConfigError):
        calls[caller]()
    writer.assert_not_called()
    audit.assert_not_called()
    assert path.read_bytes() == content
    assert list(path.parent.iterdir()) == [path]
