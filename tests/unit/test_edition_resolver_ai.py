from __future__ import annotations

from pathlib import Path

import pytest

from src.core.edition_resolver import (
    _parse_ai,
    _parse_edition_config,
    _parse_gather_activation_config,
    _parse_m365,
    load_program,
)
from src.core.exceptions import ConfigError
from src.core.models_v2 import GatherActivationConfig, Program
from src.core.program_reality import _resolve_committed_gather_run_requirement


def test_parse_edition_config_reads_audience_scope_ids() -> None:
    config = _parse_edition_config(
        {
            "id": "full_hygiene", "program_id": "acme", "name": "Full Hygiene", "type": "full_hygiene",
            "altitude": "detailed", "cadence": "weekly", "audience_scope_ids": ["engineering_hygiene"],
        },
        Path("editions/acme.yaml"),
    )

    assert config.audience_scope_ids == ("engineering_hygiene",)


def test_parse_edition_config_defaults_audience_scope_ids_to_empty() -> None:
    config = _parse_edition_config(
        {"id": "full_hygiene", "program_id": "acme", "name": "Full Hygiene", "type": "full_hygiene", "altitude": "detailed", "cadence": "weekly"},
        Path("editions/acme.yaml"),
    )

    assert config.audience_scope_ids == ()


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


def test_parse_gather_activation_config_requires_explicit_enforce() -> None:
    shadow = _parse_gather_activation_config({}, Path("programs/acme/program.yaml"))
    enforce = _parse_gather_activation_config(
        {
            "run_manifest_mode": "enforce",
            "committed_scope_source": "gather_run",
            "full_discovery_cadence_hours": 24,
            "freshness_warn_hours": 30,
            "freshness_block_hours": 48,
        },
        Path("programs/acme/program.yaml"),
    )

    assert shadow.requires_committed_gather_run is False
    assert enforce.requires_committed_gather_run is True


def test_committed_gather_run_reader_activation_is_config_driven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = Program(
        schema_version="3.0",
        id="acme",
        name="Acme",
        gather=GatherActivationConfig(run_manifest_mode="enforce"),
    )
    monkeypatch.setattr("src.core.edition_resolver.load_program", lambda *_args, **_kwargs: program)

    assert _resolve_committed_gather_run_requirement(
        "acme", programs_root=Path("programs"), override=None
    ) is True
    assert _resolve_committed_gather_run_requirement(
        "acme", programs_root=Path("programs"), override=False
    ) is False


def test_armada_declares_the_d24_shadow_timing_policy() -> None:
    programs_root = Path(__file__).resolve().parents[2] / "programs"
    program = load_program("armada", programs_root=programs_root)

    assert program is not None
    assert program.gather.run_manifest_mode == "shadow"
    assert program.gather.full_discovery_cadence_hours == 24
    assert program.gather.freshness_warn_hours == 30
    assert program.gather.freshness_block_hours == 48


@pytest.mark.parametrize(
    "config",
    (
        {"run_manifest_mode": "enabled"},
        {"committed_scope_source": "legacy"},
        {"full_discovery_cadence_hours": 0},
        {"freshness_warn_hours": 49, "freshness_block_hours": 48},
    ),
)
def test_parse_gather_activation_config_rejects_invalid_values(config: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        _parse_gather_activation_config(config, Path("programs/acme/program.yaml"))


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
