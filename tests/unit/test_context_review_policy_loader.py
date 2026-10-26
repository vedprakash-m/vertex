from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.core import policy_loader
from src.core.exceptions import ConfigError


@pytest.fixture
def base_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "freshness_policy.yaml"
    path.write_text(
        "policy_schema_version: '1'\nfact_type_ttl_days: {}\ngather_cadence_hours: {}\n"
        "context_review:\n  files:\n    program.yaml:\n      warn_days: 12\n      block_days: 30\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(policy_loader, "FRESHNESS_POLICY_PATH", path)
    policy_loader._load_freshness_policy_document.cache_clear()
    yield path
    policy_loader._load_freshness_policy_document.cache_clear()


def test_actual_loader_exposes_base_and_partial_private_review_policy(base_policy: Path, tmp_path: Path) -> None:
    override = tmp_path / "private.yaml"
    override.write_text(
        "context_review:\n  files:\n    program.yaml:\n      warn_days: 5\n", encoding="utf-8",
    )
    policy = policy_loader.load_freshness_policy(override_path=override)
    rule = dict(policy.context_review.files)["program.yaml"]
    assert (rule.warn_days, rule.block_days) == (5, 30)


@pytest.mark.parametrize("content", ["null", "", "[]", "context_review: null", "secret: [PRIVATE-YAML", "date: 2026-13-01"])
def test_present_invalid_private_policy_is_not_silently_ignored(
    base_policy: Path, tmp_path: Path, content: str,
) -> None:
    override = tmp_path / "private.yaml"
    override.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        policy_loader.load_freshness_policy(override_path=override)
    assert "PRIVATE-YAML" not in str(caught.value)


def test_actual_missing_override_preserves_base_policy(base_policy: Path, tmp_path: Path) -> None:
    policy = policy_loader.load_freshness_policy(override_path=tmp_path / "absent.yaml")
    assert dict(policy.context_review.files)["program.yaml"].warn_days == 12


def test_policy_override_reads_instead_of_trusting_exists(
    base_policy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_loader.load_freshness_policy()
    override = tmp_path / "private.yaml"
    override.write_text("context_review:\n  files:\n    program.yaml:\n      warn_days: 5\n", encoding="utf-8")
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "exists", lambda self: False)
        policy = policy_loader.load_freshness_policy(override_path=override)
    assert dict(policy.context_review.files)["program.yaml"].warn_days == 5


def test_malformed_base_rule_cannot_be_hidden_by_override(base_policy: Path, tmp_path: Path) -> None:
    base_policy.write_text("policy_schema_version: '1'\ncontext_review: null\n", encoding="utf-8")
    override = tmp_path / "private.yaml"
    override.write_text("context_review: {}", encoding="utf-8")
    with pytest.raises(ConfigError):
        policy_loader.load_freshness_policy(override_path=override)


def test_private_policy_failed_read_is_not_an_optional_absence(
    base_policy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_loader.load_freshness_policy()
    override = tmp_path / "private.yaml"
    original = Path.read_text

    def read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if self == override:
            raise PermissionError("PRIVATE-ACCESS")
        return original(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(ConfigError) as caught:
        policy_loader.load_freshness_policy(override_path=override)
    assert "PRIVATE-ACCESS" not in str(caught.value)


def test_explicit_incompatible_override_schema_is_rejected(base_policy: Path, tmp_path: Path) -> None:
    override = tmp_path / "private.yaml"
    override.write_text("policy_schema_version: '2'\ncontext_review: {}", encoding="utf-8")
    with pytest.raises(ConfigError, match="Policy schema mismatch"):
        policy_loader.load_freshness_policy(override_path=override)


def test_compiled_context_uses_actual_program_policy(
    base_policy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core import program_context

    for name in ("workstreams", "milestones", "risk_entries", "decision_entries", "assumptions", "dependencies"):
        monkeypatch.setattr(program_context, f"load_current_{name}", lambda *args, **kwargs: ())

    root = tmp_path / "programs"
    program = root / "fixture"
    program.mkdir(parents=True)
    today = date(2026, 10, 24)
    reviewed = (today - timedelta(days=5)).isoformat()
    (program / "program.yaml").write_text(
        f"schema_version: '3.0'\nid: fixture\nname: Fixture\nlast_reviewed_date: {reviewed}\n", encoding="utf-8",
    )
    (program / "freshness_policy.yaml").write_text(
        "context_review:\n  files:\n    program.yaml:\n      warn_days: 1\n      block_days: 3\n", encoding="utf-8",
    )
    context = program_context.load_program_context("fixture", programs_root=root, today=today, raise_on_error=False)
    flag, = [flag for flag in context.staleness_flags if flag.file == "program.yaml"]
    assert flag.days_stale == 5
    assert flag.severity == "block"


def test_legacy_freshness_policy_constructor_gets_compatibility_review_defaults() -> None:
    policy = policy_loader.FreshnessPolicy(fact_type_ttl_days={}, gather_cadence_hours={})
    assert dict(policy.context_review.files)["program.yaml"].warn_days > 0


@pytest.mark.parametrize("mapping", [False, True])
def test_existing_ttl_and_cadence_override_rules_are_preserved(
    base_policy: Path, tmp_path: Path, mapping: bool,
) -> None:
    override = tmp_path / "private.yaml"
    override.write_text(
        "policy_schema_version: '1'\nfact_type_ttl_days: {signal: 11, ignored: text}\n"
        "gather_cadence_hours: {ado: 2.5, ignored: text}\n" if mapping else
        "fact_type_ttl_days: []\ngather_cadence_hours: []\n",
        encoding="utf-8",
    )
    policy = policy_loader.load_freshness_policy(override_path=override)
    assert policy.fact_type_ttl_days == ({"signal": 11} if mapping else {})
    assert policy.gather_cadence_hours == ({"ado": 2.5} if mapping else {})
    assert dict(policy.context_review.files)["program.yaml"].warn_days == 12


@pytest.mark.parametrize("condition", ["missing", "incompatible"])
def test_required_policy_is_not_replaced_by_defaults(
    base_policy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, condition: str,
) -> None:
    if condition == "missing":
        monkeypatch.setattr(policy_loader, "FRESHNESS_POLICY_PATH", tmp_path / "absent.yaml")
    else:
        base_policy.write_text("policy_schema_version: '2'", encoding="utf-8")
    with pytest.raises(ConfigError):
        policy_loader.load_freshness_policy()


@pytest.mark.parametrize("layer", ["base", "override"])
@pytest.mark.parametrize("content", [
    "context_review: null\ncontext_review: {}\n",
    "context_review:\n  files: null\n  files: {}\n",
    "context_review:\n  files:\n    program.yaml: null\n    program.yaml: {}\n",
    "context_review:\n  files:\n    program.yaml:\n      block_days: false\n      block_days: null\n",
    "unused:\n  - {PRIVATE-DUPLICATE: false, PRIVATE-DUPLICATE: null}\n",
    "unused: &rule {block_days: false, block_days: null}\n"
    "context_review:\n  files:\n    program.yaml: {<<: *rule}\n",
    "unused: &rule {warn_days: 1}\n"
    "context_review:\n  files:\n    program.yaml: {<<: *rule, <<: *rule}\n",
])
def test_duplicate_explicit_policy_keys_are_rejected(
    base_policy: Path, tmp_path: Path, layer: str, content: str,
) -> None:
    path = base_policy if layer == "base" else tmp_path / "override.yaml"
    path.write_text("policy_schema_version: '1'\n" + content, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        policy_loader.load_freshness_policy(override_path=path if layer == "override" else None)
    assert "PRIVATE-DUPLICATE" not in str(caught.value)
    assert "block_days" not in str(caught.value)


@pytest.mark.parametrize("layer", ["base", "override"])
def test_policy_aliases_and_merge_precedence_remain_valid(
    base_policy: Path, tmp_path: Path, layer: str,
) -> None:
    path = base_policy if layer == "base" else tmp_path / "override.yaml"
    path.write_text(
        "policy_schema_version: '1'\n"
        "first: &first {warn_days: 2, block_days: 20}\n"
        "second: &second {warn_days: 5, block_days: 30}\n"
        "nested: &nested {<<: *first, warn_days: 3}\n"
        "context_review:\n  files:\n"
        "    program.yaml: {<<: [*first, *second], warn_days: 1}\n"
        "    workstreams.yaml: *nested\n"
        "    milestones.yaml: {<<: *nested, block_days: 40}\n",
        encoding="utf-8",
    )
    policy = policy_loader.load_freshness_policy(override_path=path if layer == "override" else None)
    rules = dict(policy.context_review.files)
    assert (rules["program.yaml"].warn_days, rules["program.yaml"].block_days) == (1, 20)
    assert (rules["workstreams.yaml"].warn_days, rules["workstreams.yaml"].block_days) == (3, 20)
    assert (rules["milestones.yaml"].warn_days, rules["milestones.yaml"].block_days) == (3, 40)


def test_compiled_context_rejects_duplicate_policy_before_reading_context(
    base_policy: Path, tmp_path: Path,
) -> None:
    from src.core.program_context import load_program_context

    program = tmp_path / "synthetic_programs" / "fixture"
    program.mkdir(parents=True)
    (program / "freshness_policy.yaml").write_text(
        "context_review:\n  files:\n    program.yaml:\n"
        "      block_days: false\n      block_days: null\n", encoding="utf-8",
    )
    with pytest.raises(ConfigError) as caught:
        load_program_context("fixture", programs_root=program.parent, today=date(2026, 10, 24))
    assert "freshness_policy.yaml" in str(caught.value)
    assert "Missing required file" not in str(caught.value)


@pytest.mark.parametrize("content", [
    "unused: [PRIVATE-PARSE", "unused: 2026-13-01", "unused: {[a]: b}",
    "unused: {<<: 42}", "unused: {<<: [42]}",
])
def test_freshness_parser_failures_are_safely_diagnosed(
    base_policy: Path, tmp_path: Path, content: str,
) -> None:
    override = tmp_path / "override.yaml"
    override.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        policy_loader.load_freshness_policy(override_path=override)
    assert "PRIVATE-PARSE" not in str(caught.value)


def test_freshness_decode_failure_is_safely_diagnosed(base_policy: Path, tmp_path: Path) -> None:
    override = tmp_path / "override.yaml"
    override.write_bytes(b"\xffPRIVATE-DECODE")
    with pytest.raises(ConfigError) as caught:
        policy_loader.load_freshness_policy(override_path=override)
    assert "PRIVATE-DECODE" not in str(caught.value)


def test_freshness_unexpected_read_error_propagates(
    base_policy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_loader.load_freshness_policy()
    original = Path.read_text
    override = tmp_path / "override.yaml"

    def read_text(self: Path, **kwargs) -> str:
        if self == override:
            raise RuntimeError("synthetic reader bug")
        return original(self, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(RuntimeError, match="synthetic reader bug"):
        policy_loader.load_freshness_policy(override_path=override)


def test_unrelated_yaml_loading_keeps_existing_duplicate_behavior(tmp_path: Path) -> None:
    from src.core.yaml_utils import load_yaml_mapping_strict

    path = tmp_path / "unrelated.yaml"
    path.write_text("key: first\nkey: last\n", encoding="utf-8")
    assert load_yaml_mapping_strict(path) == {"key": "last"}
