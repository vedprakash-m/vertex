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
