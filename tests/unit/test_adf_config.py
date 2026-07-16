"""Unit tests for ``src/core/adf_config.py`` (ADF-W0.5 typed config loader)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.adf_config import (
    AUTONOMY_LEVELS,
    AlertDelivery,
    ArchDataFixConfig,
    ChannelBudget,
    CockpitConfig,
    ExecutionMode,
    ProgramGovernancePolicy,
    RETENTION_FLOOR_DAYS,
    SamplingRule,
    SolicitationMode,
    SolicitationPolicy,
    load_arch_data_fix,
    parse_arch_data_fix,
)
from src.core.exceptions import ConfigError


def test_absent_block_yields_off_defaults() -> None:
    cfg = parse_arch_data_fix("xpf", None)
    assert cfg.mode is ExecutionMode.OFF
    assert cfg.is_off is True
    assert cfg.is_enforce is False
    assert cfg.cockpit.enabled is True
    assert cfg.actuation.enabled is False
    assert cfg.solicitation is SolicitationMode.DISABLED
    assert cfg.program_id == "xpf"


def test_empty_mapping_yields_off_defaults() -> None:
    cfg = parse_arch_data_fix("xpf", {})
    assert cfg.mode is ExecutionMode.OFF


def test_invalid_mode_raises() -> None:
    with pytest.raises(ConfigError, match="mode"):
        parse_arch_data_fix("xpf", {"mode": "explode"})


def test_enforce_mode_round_trip() -> None:
    cfg = parse_arch_data_fix(
        "xpf",
        {
            "mode": "enforce",
            "cockpit": {"enabled": True, "history_keep_builds": 10, "fleet_enabled": True},
            "actuation": {"enabled": True, "outbox_worker_enabled": True},
            "solicitation": "approved_batch",
        },
    )
    assert cfg.mode is ExecutionMode.ENFORCE
    assert cfg.is_enforce is True
    assert cfg.cockpit.fleet_enabled is True
    assert cfg.cockpit.history_keep_builds == 10
    assert cfg.actuation.enabled is True
    assert cfg.actuation.outbox_worker_enabled is True
    assert cfg.solicitation is SolicitationMode.APPROVED_BATCH


def test_invalid_enum_alert_delivery() -> None:
    with pytest.raises(ConfigError, match="alerts.delivery"):
        parse_arch_data_fix("xpf", {"alerts": {"delivery": "sms"}})


def test_alert_delivery_all_values() -> None:
    for delivery in AlertDelivery:
        cfg = parse_arch_data_fix("xpf", {"alerts": {"delivery": delivery.value}})
        assert cfg.alerts.delivery is delivery


def test_negative_budget_rejected() -> None:
    with pytest.raises(ConfigError, match=">= 0"):
        parse_arch_data_fix(
            "xpf",
            {"channels": {"ado": {"per_attempt_timeout_seconds": -5}}},
        )


def test_total_budget_below_per_attempt_rejected() -> None:
    with pytest.raises(ConfigError, match="total_budget_seconds"):
        parse_arch_data_fix(
            "xpf",
            {
                "channels": {
                    "ado": {"per_attempt_timeout_seconds": 60, "total_budget_seconds": 10},
                }
            },
        )


def test_channel_budget_parsed() -> None:
    cfg = parse_arch_data_fix(
        "xpf",
        {
            "channels": {
                "workiq_nl": {
                    "required": False,
                    "inline_allowed": False,
                    "per_attempt_timeout_seconds": 90,
                    "total_budget_seconds": 90,
                    "prefetch_required": True,
                }
            }
        },
    )
    workiq = cfg.channels["workiq_nl"]
    assert isinstance(workiq, ChannelBudget)
    assert workiq.inline_allowed is False
    assert workiq.total_budget_seconds == 90
    assert workiq.prefetch_required is True


def test_retention_defaults_match_section_9_7_floors() -> None:
    cfg = parse_arch_data_fix("xpf", {})
    assert cfg.retention.tier_decision_days == RETENTION_FLOOR_DAYS["tier_decision_days"]
    assert cfg.retention.ai_telemetry_days == RETENTION_FLOOR_DAYS["ai_telemetry_days"]
    assert cfg.retention.run_telemetry_days == RETENTION_FLOOR_DAYS["run_telemetry_days"]


def test_retention_override_may_lengthen_only() -> None:
    cfg = parse_arch_data_fix(
        "xpf",
        {
            "governance": {"retention_overrides_days": {"tier_decision_days": 90}},
            "retention": {"tier_decision_days": 90},
        },
    )
    assert cfg.retention.tier_decision_days == 90


def test_retention_override_below_floor_rejected() -> None:
    with pytest.raises(ConfigError, match=">= 45"):
        parse_arch_data_fix(
            "xpf",
            {
                "governance": {"retention_overrides_days": {"tier_decision_days": 10}},
                "retention": {"tier_decision_days": 10},
            },
        )


def test_unknown_retention_override_key_rejected() -> None:
    with pytest.raises(ConfigError, match="not a known retention key"):
        parse_arch_data_fix(
            "xpf",
            {"governance": {"retention_overrides_days": {"unknown_key": 100}}},
        )


def test_governance_autonomy_ceiling_validates_levels() -> None:
    cfg = parse_arch_data_fix(
        "xpf",
        {"governance": {"autonomy_ceiling": {"ado_create_task": "l3", "solicitation": "l1"}}},
    )
    assert cfg.governance.autonomy_ceiling == {"ado_create_task": "l3", "solicitation": "l1"}


def test_governance_autonomy_ceiling_invalid_level() -> None:
    with pytest.raises(ConfigError, match="autonomy level"):
        parse_arch_data_fix(
            "xpf",
            {"governance": {"autonomy_ceiling": {"ado_create_task": "l9"}}},
        )


def test_governance_default_ceiling_present() -> None:
    cfg = parse_arch_data_fix("xpf", {})
    gov: ProgramGovernancePolicy = cfg.governance
    assert "solicitation" in gov.autonomy_ceiling
    assert "ado_create_task" in gov.autonomy_ceiling
    # default l2
    assert gov.autonomy_ceiling["solicitation"] == "l2"


def test_sampling_rule_parsed() -> None:
    cfg = parse_arch_data_fix(
        "xpf",
        {
            "governance": {
                "review_sampling": {
                    "risk_proposal": {
                        "sample_rate": 0.25,
                        "min_weekly_samples": 5,
                        "include_low_confidence": False,
                    }
                }
            }
        },
    )
    rule = cfg.governance.review_sampling["risk_proposal"]
    assert isinstance(rule, SamplingRule)
    assert rule.sample_rate == 0.25
    assert rule.min_weekly_samples == 5
    assert rule.include_low_confidence is False


def test_sampling_rate_out_of_range_rejected() -> None:
    with pytest.raises(ConfigError, match=r"\[0,1\]"):
        parse_arch_data_fix(
            "xpf",
            {"governance": {"review_sampling": {"risk_proposal": {"sample_rate": 1.5, "min_weekly_samples": 1}}}},
        )


def test_solicitation_policy_parsed() -> None:
    cfg = parse_arch_data_fix(
        "xpf",
        {
            "governance": {
                "solicitation": {
                    "mode": "standing_policy",
                    "audience_tiers": {"tpm": "l4", "em": "l3"},
                    "per_recipient_cooldown_days": 7,
                    "senior_requires_named_approval": False,
                }
            },
        },
    )
    sol: SolicitationPolicy = cfg.governance.solicitation
    assert sol.mode is SolicitationMode.STANDING_POLICY
    assert sol.audience_tiers == {"tpm": "l4", "em": "l3"}
    assert sol.per_recipient_cooldown_days == 7
    assert sol.senior_requires_named_approval is False


def test_solicitation_audience_tier_invalid_level() -> None:
    with pytest.raises(ConfigError, match="autonomy level"):
        parse_arch_data_fix(
            "xpf",
            {"governance": {"solicitation": {"audience_tiers": {"tpm": "l99"}}}},
        )


def test_top_three_proposals_parsed() -> None:
    cfg = parse_arch_data_fix("xpf", {"top_3_proposals": {"enabled": False, "require_human_acceptance": False}})
    assert cfg.top_3_proposals.enabled is False
    assert cfg.top_3_proposals.require_human_acceptance is False


def test_value_config_parsed() -> None:
    cfg = parse_arch_data_fix(
        "xpf",
        {"value": {"workflow": "risk_closure", "minimum_baseline_samples": 8, "historical_baseline_allowed": False}},
    )
    assert cfg.value.workflow == "risk_closure"
    assert cfg.value.minimum_baseline_samples == 8
    assert cfg.value.historical_baseline_allowed is False


def test_non_mapping_block_rejected() -> None:
    with pytest.raises(ConfigError, match="must be a mapping"):
        parse_arch_data_fix("xpf", ["not", "a", "mapping"])


def test_autonomy_levels_set_complete() -> None:
    assert AUTONOMY_LEVELS == frozenset({"l0", "l1", "l2", "l3", "l4"})


def test_load_arch_data_fix_from_missing_program(tmp_path: Path) -> None:
    cfg = load_arch_data_fix("ghost", programs_root=tmp_path)
    assert cfg.mode is ExecutionMode.OFF
    assert cfg.program_id == "ghost"


def test_load_arch_data_fix_from_program_yaml(tmp_path: Path) -> None:
    (tmp_path / "xpf").mkdir()
    (tmp_path / "xpf" / "program.yaml").write_text(
        "arch_data_fix:\n  mode: observe\n  cockpit:\n    fleet_enabled: true\n",
        encoding="utf-8",
    )
    cfg = load_arch_data_fix("xpf", programs_root=tmp_path)
    assert cfg.mode is ExecutionMode.OBSERVE
    assert cfg.cockpit.fleet_enabled is True


def test_load_arch_data_fix_invalid_yaml_raises(tmp_path: Path) -> None:
    (tmp_path / "xpf").mkdir()
    (tmp_path / "xpf" / "program.yaml").write_text("arch_data_fix:\n  mode: explode\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mode"):
        load_arch_data_fix("xpf", programs_root=tmp_path)


def test_schema_version_stable() -> None:
    cfg = parse_arch_data_fix("xpf", {})
    assert cfg.schema_version == "1"
    assert cfg.governance.schema_version == "1"
