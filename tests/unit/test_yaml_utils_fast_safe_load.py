"""specs/people.md Phase 3, PPL-W3.5: §8.5's "loader uses LibYAML
CSafeLoader when available with behavior-parity coverage for the
pure-Python fallback" -- found unimplemented while diagnosing a real
100x cold-compile budget miss during the 10,000-person scale benchmark.
"""

from __future__ import annotations

import yaml

from src.core.yaml_utils import fast_safe_load


def test_fast_safe_load_parses_a_mapping() -> None:
    result = fast_safe_load("a: 1\nb: two\n")

    assert result == {"a": 1, "b": "two"}


def test_fast_safe_load_parses_a_list() -> None:
    result = fast_safe_load("- 1\n- 2\n- 3\n")

    assert result == [1, 2, 3]


def test_fast_safe_load_matches_yaml_safe_load_output() -> None:
    text = (
        "schema_version: \"2.0\"\n"
        "entities:\n"
        "  - entity_id: \"person:alice\"\n"
        "    aliases: [alice, aadams]\n"
        "    nested:\n"
        "      key: value\n"
        "      count: 3\n"
        "      flag: true\n"
    )

    assert fast_safe_load(text) == yaml.safe_load(text)


def test_fast_safe_load_handles_empty_document() -> None:
    assert fast_safe_load("") is None


def test_fast_safe_load_uses_csafeloader_when_available() -> None:
    # This environment has LibYAML installed (confirmed via yaml.__with_libyaml__
    # during PPL-W3.5's investigation); assert the fast path is genuinely wired,
    # not silently falling back, so a future environment regression is caught.
    if getattr(yaml, "__with_libyaml__", False):
        from src.core.yaml_utils import _FAST_YAML_LOADER

        assert _FAST_YAML_LOADER is yaml.CSafeLoader
