from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

import yaml

from src.core.exceptions import ConfigError


_CADENCE_WINDOWS = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "biweekly": timedelta(days=14),
    "monthly": timedelta(days=30),
}


ALLOWED_RULE_FIELDS = {
    "consecutive_high",
    "milestone_status",
    "milestone_days_to_target",
    "decision_ask_age_days",
    "decision_ask_status",
    "vitality_composite",
    "stale_days",
}
ALLOWED_OPERATORS = {">=", "<=", "==", "!=", ">", "<"}
ESCALATION_RULES_SCHEMA_VERSION = "1.0"
_SUPPORTED_ESCALATION_ACTION = "draft_escalation"


@dataclass(frozen=True, slots=True)
class PolicyCondition:
    field: str
    operator: str
    value: int | float | str | bool


@dataclass(frozen=True, slots=True)
class PolicyRule:
    name: str
    conditions: tuple[PolicyCondition, ...]
    cooldown_hours: int = 0


def get_escalation_rules_path(program_id: str, *, programs_root: Path) -> Path:
    return programs_root / program_id / "escalation_rules.yaml"


def build_default_escalation_rules_document() -> dict[str, Any]:
    return {
        "schema_version": ESCALATION_RULES_SCHEMA_VERSION,
        "rules": [
            {
                "name": "consecutive_high",
                "conditions": [
                    {"field": "consecutive_high", "op": ">=", "value": 3},
                ],
                "action": _SUPPORTED_ESCALATION_ACTION,
                "cooldown_hours": 168,
            },
            {
                "name": "milestone_at_risk",
                "conditions": [
                    {"field": "milestone_status", "op": "==", "value": "at_risk"},
                    {"field": "milestone_days_to_target", "op": "<=", "value": 14},
                ],
                "action": _SUPPORTED_ESCALATION_ACTION,
                "cooldown_hours": 168,
            },
            {
                "name": "unresolved_ask",
                "conditions": [
                    {"field": "decision_ask_age_days", "op": ">=", "value": 21},
                    {"field": "decision_ask_status", "op": "==", "value": "open"},
                ],
                "action": _SUPPORTED_ESCALATION_ACTION,
                "cooldown_hours": 336,
            },
        ],
    }


def load_escalation_rules(
    *,
    program_id: str,
    programs_root: Path,
    rules_path: Path | None = None,
) -> tuple[PolicyRule, ...]:
    path = rules_path or get_escalation_rules_path(program_id, programs_root=programs_root)
    if not path.exists():
        return ()
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")

    schema_version = payload.get("schema_version")
    if schema_version is not None and str(schema_version) != ESCALATION_RULES_SCHEMA_VERSION:
        raise ConfigError(
            f"Unsupported schema_version '{schema_version}' in {path}; expected {ESCALATION_RULES_SCHEMA_VERSION}."
        )

    rules_payload = payload.get("rules")
    if not isinstance(rules_payload, list):
        raise ConfigError(f"Expected 'rules' list in {path}")

    parsed_rules: list[PolicyRule] = []
    for entry in rules_payload:
        if not isinstance(entry, dict):
            raise ConfigError(f"Each escalation rule must be a mapping in {path}")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ConfigError(f"Escalation rules in {path} require a non-empty name")
        raw_conditions = entry.get("conditions", entry.get("when"))
        if not isinstance(raw_conditions, list) or not raw_conditions:
            raise ConfigError(f"Escalation rule '{name}' in {path} requires at least one condition")

        action = str(entry.get("action") or _SUPPORTED_ESCALATION_ACTION).strip()
        if action != _SUPPORTED_ESCALATION_ACTION:
            raise ConfigError(f"Rule '{name}' in {path} has unsupported action '{action}'")

        conditions: list[PolicyCondition] = []
        for raw_condition in raw_conditions:
            if not isinstance(raw_condition, dict):
                raise ConfigError(f"Rule '{name}' in {path} has a non-mapping condition")
            field = str(raw_condition.get("field") or "").strip()
            operator = str(raw_condition.get("op") or raw_condition.get("operator") or "").strip()
            if not field or not operator or "value" not in raw_condition:
                raise ConfigError(
                    f"Rule '{name}' in {path} must define condition field, op/operator, and value"
                )
            value = raw_condition["value"]
            if not isinstance(value, (str, int, float, bool)):
                raise ConfigError(f"Rule '{name}' in {path} has unsupported condition value type")
            conditions.append(PolicyCondition(field=field, operator=operator, value=value))

        raw_cooldown_hours = entry.get("cooldown_hours", 0)
        try:
            cooldown_hours = int(raw_cooldown_hours or 0)
        except (TypeError, ValueError) as error:
            raise ConfigError(f"Rule '{name}' in {path} has invalid cooldown_hours") from error
        if cooldown_hours < 0:
            raise ConfigError(f"Rule '{name}' in {path} has negative cooldown_hours")

        parsed_rules.append(PolicyRule(name=name, conditions=tuple(conditions), cooldown_hours=cooldown_hours))
    return tuple(parsed_rules)


def evaluate_rules(
    rules: tuple[PolicyRule, ...],
    context: dict[str, Any],
) -> tuple[str, ...]:
    matched_rules: list[str] = []
    for rule in rules:
        if all(_evaluate_condition(condition, context) for condition in rule.conditions):
            matched_rules.append(rule.name)
    return tuple(matched_rules)


def get_cadence_window(cadence: str) -> timedelta | None:
    return _CADENCE_WINDOWS.get(cadence)


def check_cadence(
    cadence: str,
    last_confirmed: datetime | None,
    *,
    as_of: datetime | None = None,
) -> bool:
    if last_confirmed is None:
        return False
    window = get_cadence_window(cadence)
    if window is None:
        return False
    current_time = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if last_confirmed.tzinfo is None:
        resolved_last_confirmed = last_confirmed.replace(tzinfo=timezone.utc)
    else:
        resolved_last_confirmed = last_confirmed.astimezone(timezone.utc)
    return current_time - resolved_last_confirmed <= window


def check_cooldown(
    state_file: Path,
    item_id: str,
    cooldown_hours: int,
    *,
    as_of: datetime | None = None,
) -> bool:
    if cooldown_hours <= 0:
        return True
    current_time = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload = _load_state_file(state_file)
    raw_timestamp = payload.get(item_id)
    if not isinstance(raw_timestamp, str) or not raw_timestamp.strip():
        return True
    try:
        last_triggered = datetime.fromisoformat(raw_timestamp)
    except ValueError:
        return True
    if last_triggered.tzinfo is None:
        last_triggered = last_triggered.replace(tzinfo=timezone.utc)
    else:
        last_triggered = last_triggered.astimezone(timezone.utc)
    return current_time - last_triggered >= timedelta(hours=cooldown_hours)


def record_cooldown(
    state_file: Path,
    item_id: str,
    *,
    triggered_at: datetime | None = None,
) -> Path:
    payload = _load_state_file(state_file)
    resolved_triggered_at = (triggered_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload[item_id] = resolved_triggered_at.isoformat()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_file.with_suffix(state_file.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(state_file)
    return state_file


def _evaluate_condition(condition: PolicyCondition, context: dict[str, Any]) -> bool:
    if condition.field not in ALLOWED_RULE_FIELDS:
        raise ConfigError(f"Unsupported escalation condition field '{condition.field}'.")
    if condition.operator not in ALLOWED_OPERATORS:
        raise ConfigError(f"Unsupported escalation operator '{condition.operator}'.")
    if condition.field not in context:
        return False

    actual = context[condition.field]
    expected = condition.value
    if condition.operator == "==":
        return actual == expected
    if condition.operator == "!=":
        return actual != expected

    if not isinstance(actual, (int, float)) or not isinstance(expected, (int, float)):
        raise ConfigError(
            f"Escalation condition '{condition.field} {condition.operator} {condition.value}' requires numeric values."
        )
    if condition.operator == ">=":
        return actual >= expected
    if condition.operator == "<=":
        return actual <= expected
    if condition.operator == ">":
        return actual > expected
    return actual < expected


def _load_state_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    return {
        str(key): str(value)
        for key, value in payload.items()
        if isinstance(key, str) and isinstance(value, str)
    }