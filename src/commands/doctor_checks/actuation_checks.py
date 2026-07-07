"""WI-7.1: Doctor checks for the actuation engine configuration."""
from __future__ import annotations

from pathlib import Path

from src.commands.doctor_checks.models import DoctorCheck


def run_actuation_policy_check(
    program_id: str,
    *,
    programs_root: Path,
) -> DoctorCheck:
    """Check actuation policy configuration for a program.

    Reports:
    - ok:   policy loaded, enabled=false (safe default)
    - warn: any rule has an unknown predicate (schema drift) or
            policy enabled=true without CP-7 dry-run review being recorded
    - info: policy loads cleanly and is disabled
    """
    try:
        from src.core.actuation_engine import load_actuation_policy
        policy = load_actuation_policy(program_id, programs_root=programs_root)
    except ValueError as exc:
        return DoctorCheck(
            "Actuation Policy",
            "fail",
            f"Actuation policy schema validation failed for {program_id!r}: {exc}",
        )
    except Exception as exc:
        return DoctorCheck(
            "Actuation Policy",
            "warn",
            f"Could not load actuation policy for {program_id!r}: {exc}",
        )

    if policy.enabled:
        enabled_rule_ids = [r.id for r in policy.rules if r.enabled]
        if enabled_rule_ids:
            return DoctorCheck(
                "Actuation Policy",
                "warn",
                f"Actuation is ENABLED for {program_id!r} with {len(enabled_rule_ids)} active rule(s): "
                f"{enabled_rule_ids}. Ensure CP-7 dry-run review was completed before any live execution.",
                metadata={"program_id": program_id, "enabled_rules": enabled_rule_ids},
            )
        return DoctorCheck(
            "Actuation Policy",
            "ok",
            f"Actuation enabled for {program_id!r} but no rules individually enabled (safe).",
        )

    return DoctorCheck(
        "Actuation Policy",
        "ok",
        f"Actuation disabled for {program_id!r} ({len(policy.rules)} rule(s) defined, all off).",
    )
