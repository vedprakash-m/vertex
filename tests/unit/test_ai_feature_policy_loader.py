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
