from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
FRESHNESS_RULES = (
    "FR-20",
    "FR-21",
    "FR-22",
    "FR-23",
    "FR-24",
    "FR-25",
    "FR-26",
    "FR-26a",
    "FR-42",
    "FR-42a",
    "FR-43",
    "FR-44",
    "FR-45",
    "FR-46",
    "FR-47",
)


def test_all_freshness_rule_ids_exist_in_engine() -> None:
    engine_source = (REPO_ROOT / "src/core/freshness_engine.py").read_text(encoding="utf-8")

    missing = [rule_id for rule_id in FRESHNESS_RULES if f'"{rule_id}"' not in engine_source]

    assert missing == [], f"Freshness rules missing from freshness_engine.py: {missing}"


def test_freshness_action_labels_match_expected_rule_set() -> None:
    engine_source = (REPO_ROOT / "src/core/freshness_engine.py").read_text(encoding="utf-8")
    action_label_rules = set(re.findall(r'"(FR-[0-9]+a?)"\s*:', engine_source))

    assert action_label_rules >= set(FRESHNESS_RULES), (
        f"Freshness action labels do not cover expected rules: {sorted(set(FRESHNESS_RULES) - action_label_rules)}"
    )


def test_all_freshness_rules_have_unit_test_coverage() -> None:
    test_source = (REPO_ROOT / "tests/unit/test_freshness_engine.py").read_text(encoding="utf-8")
    untested = [rule_id for rule_id in FRESHNESS_RULES if rule_id not in test_source]

    assert untested == [], f"Freshness rules without unit-test coverage: {untested}"