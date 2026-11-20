from __future__ import annotations

from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core import policy_loader


def _write_ai_policy(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def _set_ai_policy_path(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(policy_loader, "AI_POLICY_PATH", path)
    policy_loader._load_ai_policy_document.cache_clear()


def _full_policy_body() -> str:
    return (
        'policy_schema_version: "1"\n'
        "ai_features:\n"
        "  action_extractor:\n"
        "    max_tokens: 500\n"
        "    temperature: 0.0\n"
        "    model_tier: standard\n"
        "    frontier_eligible: true\n"
        "  onboard_assistant:\n"
        "    structure_max_tokens: 600\n"
        "    style_max_tokens: 500\n"
        "    temperature: 0.2\n"
        "    model_tier: standard\n"
        "    frontier_eligible: true\n"
        "  default:\n"
        "    max_tokens: 500\n"
        "    temperature: 0.2\n"
        "    model_tier: standard\n"
        "    frontier_eligible: true\n"
    )


def test_load_ai_feature_policy_returns_policy_for_known_feature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_path = tmp_path / "ai_policy.yaml"
    _write_ai_policy(policy_path, _full_policy_body())
    _set_ai_policy_path(monkeypatch, policy_path)

    try:
        policy = policy_loader.load_ai_feature_policy("action_extractor")
    finally:
        policy_loader._load_ai_policy_document.cache_clear()

    assert policy.max_tokens == 500
    assert policy.temperature == 0.0
    assert policy.model_tier == "standard"
    assert policy.frontier_eligible is True
    assert policy.structure_max_tokens is None
    assert policy.style_max_tokens is None


def test_exact_model_price_requires_deployment_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        policy_loader,
        "_load_policy_defaults",
        lambda: {
            "policy_schema_version": "1",
            "ai": {
                "models": {
                    "default": {
                        "input_cost_per_1k_tokens": 0.1,
                        "output_cost_per_1k_tokens": 0.2,
                    },
                    "priced-model": {
                        "input_cost_per_1k_tokens": 0.3,
                        "output_cost_per_1k_tokens": 0.4,
                    },
                },
            },
        },
    )

    exact = policy_loader.load_ai_model_cost_policy_exact("priced-model")
    assert exact.input_cost_per_1k_tokens == 0.3
    assert exact.output_cost_per_1k_tokens == 0.4
    assert policy_loader.load_ai_model_cost_policy("unlisted").input_cost_per_1k_tokens == 0.1
    with pytest.raises(ConfigError, match="explicit price"):
        policy_loader.load_ai_model_cost_policy_exact("unlisted")


def test_load_ai_feature_policy_falls_back_to_default_for_unknown_feature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_path = tmp_path / "ai_policy.yaml"
    _write_ai_policy(policy_path, _full_policy_body())
    _set_ai_policy_path(monkeypatch, policy_path)

    try:
        policy = policy_loader.load_ai_feature_policy("nonexistent_feature")
    finally:
        policy_loader._load_ai_policy_document.cache_clear()

    assert policy.max_tokens == 500
    assert policy.temperature == 0.2
    assert policy.model_tier == "standard"
    assert policy.frontier_eligible is True


def test_load_ai_feature_policy_rejects_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_path = tmp_path / "missing_ai_policy.yaml"
    _set_ai_policy_path(monkeypatch, policy_path)
    try:
        with pytest.raises(ConfigError, match="Missing required file"):
            policy_loader.load_ai_feature_policy("action_extractor")
    finally:
        policy_loader._load_ai_policy_document.cache_clear()


def test_load_ai_feature_policy_rejects_invalid_max_tokens_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_path = tmp_path / "ai_policy.yaml"
    _write_ai_policy(
        policy_path,
        'policy_schema_version: "1"\n'
        "ai_features:\n"
        "  action_extractor:\n"
        "    max_tokens: \"not-an-int\"\n"
        "    temperature: 0.0\n"
        "    model_tier: standard\n"
        "    frontier_eligible: true\n"
        "  default:\n"
        "    max_tokens: 500\n"
        "    temperature: 0.2\n"
        "    model_tier: standard\n"
        "    frontier_eligible: true\n",
    )
    _set_ai_policy_path(monkeypatch, policy_path)
    try:
        with pytest.raises(ConfigError, match="max_tokens must be an int"):
            policy_loader.load_ai_feature_policy("action_extractor")
    finally:
        policy_loader._load_ai_policy_document.cache_clear()


def test_load_ai_feature_policy_supports_onboard_assistant_dual_token_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_path = tmp_path / "ai_policy.yaml"
    _write_ai_policy(policy_path, _full_policy_body())
    _set_ai_policy_path(monkeypatch, policy_path)
    try:
        policy = policy_loader.load_ai_feature_policy("onboard_assistant")
    finally:
        policy_loader._load_ai_policy_document.cache_clear()

    assert policy.structure_max_tokens == 600
    assert policy.style_max_tokens == 500
    assert policy.temperature == 0.2
    assert policy.model_tier == "standard"
    assert policy.frontier_eligible is True


def test_load_ai_feature_policy_rejects_invalid_model_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_path = tmp_path / "ai_policy.yaml"
    _write_ai_policy(
        policy_path,
        'policy_schema_version: "1"\n'
        "ai_features:\n"
        "  action_extractor:\n"
        "    max_tokens: 500\n"
        "    temperature: 0.0\n"
        "    model_tier: mega\n"
        "    frontier_eligible: true\n"
        "  default:\n"
        "    max_tokens: 500\n"
        "    temperature: 0.2\n"
        "    model_tier: standard\n"
        "    frontier_eligible: true\n",
    )
    _set_ai_policy_path(monkeypatch, policy_path)
    try:
        with pytest.raises(ConfigError, match="model_tier must be one of"):
            policy_loader.load_ai_feature_policy("action_extractor")
    finally:
        policy_loader._load_ai_policy_document.cache_clear()


@pytest.mark.parametrize(
    ("model_entries", "deployment", "reason"),
    [
        (
            "    default:\n"
            "      input_cost_per_1k_tokens: 0.1\n"
            "      output_cost_per_1k_tokens: 0.2\n",
            "resolved-deployment",
            "missing",
        ),
        (
            "    resolved-deployment:\n"
            "      input_cost_per_1k_tokens: 0.1\n"
            "      output_cost_per_1k_tokens: 0.2\n"
            "    ' resolved-deployment ':\n"
            "      input_cost_per_1k_tokens: 0.3\n"
            "      output_cost_per_1k_tokens: 0.4\n",
            "resolved-deployment",
            "ambiguous",
        ),
    ],
)
def test_load_ai_model_cost_policy_requires_unique_exact_deployment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model_entries: str,
    deployment: str,
    reason: str,
) -> None:
    policy_path = tmp_path / "defaults.yaml"
    policy_path.write_text(
        'policy_schema_version: "1"\n'
        "ai:\n"
        "  request_router:\n"
        "    observe_only: false\n"
        "  models:\n"
        f"{model_entries}",
        encoding="utf-8",
    )
    monkeypatch.setattr(policy_loader, "DEFAULT_POLICY_PATH", policy_path)
    policy_loader._load_policy_defaults.cache_clear()

    try:
        with pytest.raises(ConfigError, match=reason):
            policy_loader.load_ai_model_cost_policy_exact(deployment)
    finally:
        policy_loader._load_policy_defaults.cache_clear()
