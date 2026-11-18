"""RELO-W4.7 / OD-9: observe-only communication budget and cooldown evaluator."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from src.core.communication_budget import (
    DEFAULT_MAX_ASKS,
    CommunicationBudgetFinding,
    evaluate_communication_budget,
)

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def _codes(findings):
    return [f.code for f in findings]


def test_default_max_asks_is_five():
    assert DEFAULT_MAX_ASKS == 5


def test_clean_artifact_has_no_findings():
    findings = evaluate_communication_budget(
        audience_id="leads",
        asks=["a", "b", "c", "d", "e"],
        prior_sends=[NOW - timedelta(days=15)],
        cooldown_days=14,
        now=NOW,
    )
    assert findings == ()


def test_sixth_ask_is_over_budget():
    findings = evaluate_communication_budget(
        audience_id="leads",
        asks=["a", "b", "c", "d", "e", "f"],
        prior_sends=[],
        cooldown_days=14,
        now=NOW,
    )
    assert _codes(findings) == ["over_ask_budget"]
    assert findings[0].audience_id == "leads"
    assert "6" in findings[0].detail and "5" in findings[0].detail


def test_custom_max_asks():
    findings = evaluate_communication_budget(
        audience_id="leads", asks=["a", "b", "c"], prior_sends=[],
        cooldown_days=14, now=NOW, max_asks=2,
    )
    assert _codes(findings) == ["over_ask_budget"]


def test_recent_prior_send_is_within_cooldown_using_latest_send():
    findings = evaluate_communication_budget(
        audience_id="leads",
        asks=["a"],
        prior_sends=[NOW - timedelta(days=30), NOW - timedelta(days=3)],
        cooldown_days=14,
        now=NOW,
    )
    assert _codes(findings) == ["within_cooldown"]
    assert "2026-11-12" in findings[0].detail


def test_cooldown_boundary_is_exclusive():
    findings = evaluate_communication_budget(
        audience_id="leads", asks=["a"], prior_sends=[NOW - timedelta(days=14)],
        cooldown_days=14, now=NOW,
    )
    assert findings == ()


def test_zero_cooldown_disables_cooldown_check():
    findings = evaluate_communication_budget(
        audience_id="leads", asks=["a"], prior_sends=[NOW],
        cooldown_days=0, now=NOW,
    )
    assert findings == ()


def test_findings_sorted_and_deterministic():
    kwargs = dict(
        audience_id="leads",
        asks=["a", "b", "c", "d", "e", "f"],
        prior_sends=[NOW - timedelta(days=1)],
        cooldown_days=14,
        now=NOW,
    )
    first = evaluate_communication_budget(**kwargs)
    assert _codes(first) == ["over_ask_budget", "within_cooldown"]
    assert first == evaluate_communication_budget(**kwargs)


def test_inputs_not_mutated_and_finding_frozen():
    asks = ["a", "b", "c", "d", "e", "f"]
    sends = [NOW - timedelta(days=1)]
    findings = evaluate_communication_budget(
        audience_id="leads", asks=asks, prior_sends=sends, cooldown_days=14, now=NOW,
    )
    assert asks == ["a", "b", "c", "d", "e", "f"]
    assert sends == [NOW - timedelta(days=1)]
    assert isinstance(findings[0], CommunicationBudgetFinding)
    with pytest.raises(FrozenInstanceError):
        findings[0].code = "x"  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"prior_sends": [datetime(2026, 11, 1)]},
        {"now": datetime(2026, 11, 15)},
        {"cooldown_days": -1},
        {"max_asks": -1},
    ],
)
def test_invalid_inputs_rejected(kwargs):
    base = dict(audience_id="leads", asks=["a"], prior_sends=[], cooldown_days=14, now=NOW)
    base.update(kwargs)
    with pytest.raises(ValueError):
        evaluate_communication_budget(**base)
