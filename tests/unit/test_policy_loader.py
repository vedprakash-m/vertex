from __future__ import annotations

from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core import policy_loader


def test_load_ai_request_router_policy_reads_observe_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    policy_path = tmp_path / "defaults.yaml"
    policy_path.write_text(
        'policy_schema_version: "1"\n'
        "ai:\n"
        "  request_router:\n"
        "    observe_only: true\n"
        "  models:\n"
        "    default:\n"
        "      input_cost_per_1k_tokens: 0.1\n"
        "      output_cost_per_1k_tokens: 0.2\n"
        "m365:\n"
        "  routing:\n"
        "    agreement_boost: 0.1\n"
        "    disagreement_cap: 0.8\n"
        "    confidence_ceiling: 0.95\n"
        "    deterministic_confidence_threshold: 0.9\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(policy_loader, "DEFAULT_POLICY_PATH", policy_path)
    policy_loader._load_policy_defaults.cache_clear()
    try:
        policy = policy_loader.load_ai_request_router_policy()
    finally:
        policy_loader._load_policy_defaults.cache_clear()

    assert policy.observe_only is True


def test_load_ai_request_router_policy_rejects_non_boolean_observe_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = tmp_path / "defaults.yaml"
    policy_path.write_text(
        'policy_schema_version: "1"\n'
        "ai:\n"
        "  request_router:\n"
        "    observe_only: maybe\n"
        "  models:\n"
        "    default:\n"
        "      input_cost_per_1k_tokens: 0.1\n"
        "      output_cost_per_1k_tokens: 0.2\n"
        "m365:\n"
        "  routing:\n"
        "    agreement_boost: 0.1\n"
        "    disagreement_cap: 0.8\n"
        "    confidence_ceiling: 0.95\n"
        "    deterministic_confidence_threshold: 0.9\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(policy_loader, "DEFAULT_POLICY_PATH", policy_path)
    policy_loader._load_policy_defaults.cache_clear()
    try:
        with pytest.raises(ConfigError, match="ai.request_router.observe_only must be a boolean"):
            policy_loader.load_ai_request_router_policy()
    finally:
        policy_loader._load_policy_defaults.cache_clear()


def test_load_m365_routing_policy_reads_deterministic_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    policy_path = tmp_path / "defaults.yaml"
    policy_path.write_text(
        'policy_schema_version: "1"\n'
        "ai:\n"
        "  models:\n"
        "    default:\n"
        "      input_cost_per_1k_tokens: 0.1\n"
        "      output_cost_per_1k_tokens: 0.2\n"
        "m365:\n"
        "  routing:\n"
        "    agreement_boost: 0.1\n"
        "    disagreement_cap: 0.8\n"
        "    confidence_ceiling: 0.95\n"
        "    deterministic_confidence_threshold: 0.9\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(policy_loader, "DEFAULT_POLICY_PATH", policy_path)
    policy_loader._load_policy_defaults.cache_clear()
    try:
        policy = policy_loader.load_m365_routing_policy()
    finally:
        policy_loader._load_policy_defaults.cache_clear()

    assert policy.deterministic_confidence_threshold == 0.9


def test_load_freshness_policy_reads_people_registry_stale_after_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """specs/bklg.md BL-E3: the real, governed people-registry freshness SLA."""
    policy_path = tmp_path / "freshness_policy.yaml"
    policy_path.write_text(
        'policy_schema_version: "1"\n'
        "fact_type_ttl_days:\n"
        "  action.item: 7\n"
        "gather_cadence_hours:\n"
        "  ado: 24\n"
        "people_registry:\n"
        "  stale_after_days: 120\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(policy_loader, "FRESHNESS_POLICY_PATH", policy_path)
    policy_loader._load_freshness_policy_document.cache_clear()
    try:
        policy = policy_loader.load_freshness_policy()
    finally:
        policy_loader._load_freshness_policy_document.cache_clear()

    assert policy.people_registry_stale_after_days == 120
    assert policy.fact_type_ttl_days == {"action.item": 7}


def test_load_freshness_policy_defaults_people_registry_stale_after_days_when_section_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_path = tmp_path / "freshness_policy.yaml"
    policy_path.write_text(
        'policy_schema_version: "1"\nfact_type_ttl_days: {}\ngather_cadence_hours: {}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(policy_loader, "FRESHNESS_POLICY_PATH", policy_path)
    policy_loader._load_freshness_policy_document.cache_clear()
    try:
        policy = policy_loader.load_freshness_policy()
    finally:
        policy_loader._load_freshness_policy_document.cache_clear()

    assert policy.people_registry_stale_after_days == 90


def test_load_freshness_policy_override_path_can_override_stale_after_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    policy_path = tmp_path / "freshness_policy.yaml"
    policy_path.write_text(
        'policy_schema_version: "1"\nfact_type_ttl_days: {}\ngather_cadence_hours: {}\npeople_registry:\n  stale_after_days: 90\n',
        encoding="utf-8",
    )
    override_path = tmp_path / "program_freshness_policy.yaml"
    override_path.write_text("people_registry:\n  stale_after_days: 45\n", encoding="utf-8")
    monkeypatch.setattr(policy_loader, "FRESHNESS_POLICY_PATH", policy_path)
    policy_loader._load_freshness_policy_document.cache_clear()
    try:
        policy = policy_loader.load_freshness_policy(override_path=override_path)
    finally:
        policy_loader._load_freshness_policy_document.cache_clear()

    assert policy.people_registry_stale_after_days == 45


def test_real_freshness_policy_yaml_has_people_registry_section() -> None:
    """Regression: BL-E3 formalized this section for real; guard against a
    future edit silently dropping it back to the pre-BL-E3 placeholder state."""
    policy_loader._load_freshness_policy_document.cache_clear()
    try:
        policy = policy_loader.load_freshness_policy()
    finally:
        policy_loader._load_freshness_policy_document.cache_clear()

    assert policy.people_registry_stale_after_days == 90


def test_load_freshness_policy_reads_enrichment_trigger_every(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BL-E4 activation: the operator-chosen event-driven cadence
    (nudge/report run counts), not an OS-level wall-clock schedule."""
    policy_path = tmp_path / "freshness_policy.yaml"
    policy_path.write_text(
        'policy_schema_version: "1"\nfact_type_ttl_days: {}\ngather_cadence_hours: {}\n'
        "people_registry:\n  stale_after_days: 90\n  enrichment_trigger:\n    nudge_run_every: 7\n    report_run_every: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(policy_loader, "FRESHNESS_POLICY_PATH", policy_path)
    policy_loader._load_freshness_policy_document.cache_clear()
    try:
        policy = policy_loader.load_freshness_policy()
    finally:
        policy_loader._load_freshness_policy_document.cache_clear()

    assert policy.people_registry_enrichment_nudge_every == 7
    assert policy.people_registry_enrichment_report_every == 2


def test_load_freshness_policy_defaults_enrichment_trigger_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    policy_path = tmp_path / "freshness_policy.yaml"
    policy_path.write_text(
        'policy_schema_version: "1"\nfact_type_ttl_days: {}\ngather_cadence_hours: {}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(policy_loader, "FRESHNESS_POLICY_PATH", policy_path)
    policy_loader._load_freshness_policy_document.cache_clear()
    try:
        policy = policy_loader.load_freshness_policy()
    finally:
        policy_loader._load_freshness_policy_document.cache_clear()

    assert policy.people_registry_enrichment_nudge_every == 5
    assert policy.people_registry_enrichment_report_every == 3


def test_load_freshness_policy_null_disables_an_enrichment_trigger_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    policy_path = tmp_path / "freshness_policy.yaml"
    policy_path.write_text(
        'policy_schema_version: "1"\nfact_type_ttl_days: {}\ngather_cadence_hours: {}\n'
        "people_registry:\n  enrichment_trigger:\n    nudge_run_every: null\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(policy_loader, "FRESHNESS_POLICY_PATH", policy_path)
    policy_loader._load_freshness_policy_document.cache_clear()
    try:
        policy = policy_loader.load_freshness_policy()
    finally:
        policy_loader._load_freshness_policy_document.cache_clear()

    assert policy.people_registry_enrichment_nudge_every is None
    assert policy.people_registry_enrichment_report_every == 3


def test_load_freshness_policy_override_path_can_override_enrichment_trigger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    policy_path = tmp_path / "freshness_policy.yaml"
    policy_path.write_text(
        'policy_schema_version: "1"\nfact_type_ttl_days: {}\ngather_cadence_hours: {}\n',
        encoding="utf-8",
    )
    override_path = tmp_path / "program_freshness_policy.yaml"
    override_path.write_text("people_registry:\n  enrichment_trigger:\n    report_run_every: 10\n", encoding="utf-8")
    monkeypatch.setattr(policy_loader, "FRESHNESS_POLICY_PATH", policy_path)
    policy_loader._load_freshness_policy_document.cache_clear()
    try:
        policy = policy_loader.load_freshness_policy(override_path=override_path)
    finally:
        policy_loader._load_freshness_policy_document.cache_clear()

    assert policy.people_registry_enrichment_report_every == 10
    assert policy.people_registry_enrichment_nudge_every == 5


def test_real_freshness_policy_yaml_has_enrichment_trigger_section() -> None:
    """Regression: BL-E4 activation shipped this section with 5/3 defaults;
    guard against a future edit silently dropping it."""
    policy_loader._load_freshness_policy_document.cache_clear()
    try:
        policy = policy_loader.load_freshness_policy()
    finally:
        policy_loader._load_freshness_policy_document.cache_clear()

    assert policy.people_registry_enrichment_nudge_every == 5
    assert policy.people_registry_enrichment_report_every == 3
