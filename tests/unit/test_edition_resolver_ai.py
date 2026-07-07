from __future__ import annotations

import pytest

from src.core.edition_resolver import _parse_ai, _parse_m365
from src.core.exceptions import ConfigError


def test_parse_ai_reads_requests_per_minute() -> None:
    ai = _parse_ai(
        {
            "enabled": True,
            "budget_usd_per_run": 0.5,
            "blurb_deployment": "primary-blurb",
            "requests_per_minute": 12,
        }
    )

    assert ai is not None
    assert ai.requests_per_minute == 12


def test_parse_m365_reads_workiq_enrich_schedule() -> None:
    m365 = _parse_m365(
        {
            "enabled": True,
            "prefer_agency": True,
            "workiq_enrich_schedule": "pre_report",
            "workiq": {
                "newsletter_search": "find rollout updates",
            },
        }
    )

    assert m365 is not None
    assert m365.workiq_enrich_schedule == "pre_report"


def test_parse_m365_reads_typed_workiq_retrieval_without_polluting_queries() -> None:
    m365 = _parse_m365(
        {
            "enabled": True,
            "workiq": {"newsletter_search": "find rollout updates"},
            "retrieval": {
                "discovery_mode": "structured_json",
                "discovery_union_runs": 3,
                "discovery_lookback_days": 21,
                "per_thread_extraction": True,
                "per_thread_top_k": 4,
                "per_thread_one_hop": False,
                "max_calls_per_cycle": 20,
                "max_wall_clock_seconds": 900,
            },
        }
    )

    assert m365 is not None and m365.retrieval is not None
    assert m365.retrieval.discovery_mode == "structured_json"
    assert m365.retrieval.discovery_union_runs == 3
    assert m365.retrieval.discovery_lookback_days == 21
    assert m365.retrieval.per_thread_extraction is True
    assert m365.retrieval.per_thread_top_k == 4
    assert m365.retrieval.max_calls_per_cycle == 20
    assert m365.retrieval.max_wall_clock_seconds == 900
    assert m365.workiq_queries == {"newsletter_search": "find rollout updates"}


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("discovery_mode", "magic"),
        ("discovery_union_runs", 0),
        ("discovery_lookback_days", True),
        ("per_thread_extraction", "yes"),
        ("per_thread_top_k", 0),
        ("max_calls_per_cycle", 201),
        ("max_wall_clock_seconds", 10),
    ),
)
def test_parse_m365_rejects_invalid_workiq_retrieval(field: str, value: object) -> None:
    with pytest.raises(ConfigError):
        _parse_m365({"enabled": True, "retrieval": {field: value}})
