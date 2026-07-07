from __future__ import annotations

from datetime import date

from src.commands import gather
from src.commands.gather_pipeline import ado_analytics_primitives, ado_signal_builder_stage, ado_snapshot_stage


def test_parse_date_sk_rejects_invalid_values() -> None:
    assert ado_analytics_primitives.parse_date_sk(None) is None
    assert ado_analytics_primitives.parse_date_sk("abc") is None
    assert ado_analytics_primitives.parse_date_sk(18991231) is None
    assert ado_analytics_primitives.parse_date_sk(20260513) == 20260513


def test_date_from_sk_and_date_to_sk_round_trip() -> None:
    parsed = ado_analytics_primitives.date_from_sk(20260513)

    assert parsed == date(2026, 5, 13)
    assert ado_analytics_primitives.date_to_sk(parsed) == 20260513
    assert ado_analytics_primitives.date_from_sk(20260230) is None


def test_completed_state_matches_existing_categories() -> None:
    assert ado_analytics_primitives.is_completed_state("Closed")
    assert ado_analytics_primitives.is_completed_state(" completed ")
    assert not ado_analytics_primitives.is_completed_state("Active")


def test_modules_share_canonical_ado_analytics_primitives() -> None:
    assert gather._parse_date_sk is ado_analytics_primitives.parse_date_sk
    assert gather._date_from_sk is ado_analytics_primitives.date_from_sk
    assert gather._date_to_sk is ado_analytics_primitives.date_to_sk
    assert gather._is_completed_state is ado_analytics_primitives.is_completed_state
    assert ado_signal_builder_stage._parse_date_sk is ado_analytics_primitives.parse_date_sk
    assert ado_signal_builder_stage._date_from_sk is ado_analytics_primitives.date_from_sk
    assert ado_signal_builder_stage._is_completed_state is ado_analytics_primitives.is_completed_state
    assert ado_snapshot_stage._parse_date_sk is ado_analytics_primitives.parse_date_sk
