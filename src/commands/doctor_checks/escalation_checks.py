from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.edition_resolver import resolve_edition
from src.core.exceptions import ConfigError
from src.core.policy_evaluator import get_escalation_rules_path, load_escalation_rules


def run_escalation_doctor(*, edition_name: str, editions_root: Path, programs_root: Path) -> DoctorReport:
    resolved = resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)
    if resolved is None:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Escalations", "fail", f"Edition '{edition_name}' could not be resolved."),),
        )

    program_id = resolved.paths.program_id
    rules_path = get_escalation_rules_path(program_id, programs_root=programs_root)
    if not rules_path.exists():
        return DoctorReport(
            edition=edition_name,
            checks=(
                DoctorCheck(
                    "Escalations",
                    "warn",
                    f"programs/{program_id}/escalation_rules.yaml is absent; escalation features are skipped.",
                ),
            ),
        )

    try:
        rules = load_escalation_rules(program_id=program_id, programs_root=programs_root)
        state_detail = validate_escalation_state(program_id, programs_root=programs_root)
    except ConfigError as error:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Escalations", "fail", str(error)),),
        )

    label = "rule" if len(rules) == 1 else "rules"
    check = DoctorCheck(
        "Escalations",
        "ok",
        f"programs/{program_id}/escalation_rules.yaml loaded ({len(rules)} {label}); schema and supported conditions valid. {state_detail}",
    )
    return DoctorReport(edition=edition_name, checks=(check,))


def validate_escalation_state(program_id: str, *, programs_root: Path) -> str:
    path = programs_root / program_id / "escalation_state.json"
    if not path.exists():
        return f"programs/{program_id}/escalation_state.json not created yet."

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigError(f"Invalid JSON in {path}: {error.msg}") from error

    if not isinstance(payload, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")

    for key, value in payload.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigError(f"Cooldown state keys in {path} must be non-empty strings")
        if not isinstance(value, str):
            raise ConfigError(f"Cooldown state '{key}' in {path} must be an ISO timestamp string")
        try:
            datetime.fromisoformat(value)
        except ValueError as error:
            raise ConfigError(f"Cooldown state '{key}' in {path} has invalid ISO timestamp '{value}'") from error

    label = "key" if len(payload) == 1 else "keys"
    return f"programs/{program_id}/escalation_state.json loaded ({len(payload)} tracked cooldown {label})."
