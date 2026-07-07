from __future__ import annotations

from datetime import date, datetime, timezone
from importlib.util import find_spec
from pathlib import Path
import re
from typing import Any

import yaml

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.commands.doctor_checks.yaml_support import _SCHEMA_VERSION_RE, load_yaml_document
from src.core.config_loader import load_bundle
from src.core.context_gap_store import append_context_gap
from src.core.exceptions import ConfigError
from src.core.persona_checker import CHECK_TYPE_REGISTRY
from src.core.persona_models import PersonaCheck, PersonaDefinition
from src.core.edition_resolver import resolve_edition


_PERSONA_KNOWN_BAN_CATEGORIES = frozenset({"banned_openings", "banned_phrases", "banned_regex"})
_PERSONA_VALID_PHASES = frozenset({"ramp_active", "steady_state", "legacy", None})
_PERSONA_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_NESTED_QUANTIFIER_RE = re.compile(r"\(\([^)]*[+*][^)]*\)[+*]")


def run_persona_doctor(
    *,
    edition_name: str | None,
    programs_root: Path,
    editions_root: Path,
    reports_root: Path,
) -> DoctorReport:
    checks: list[DoctorCheck] = []

    if edition_name:
        try:
            resolved = resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)
            if resolved is None:
                return DoctorReport(
                    edition=edition_name,
                    checks=(DoctorCheck("Personas", "fail", f"Edition '{edition_name}' could not be resolved."),),
                )
            program_id = resolved.paths.program_id
        except (ConfigError, OSError, ValueError):
            return DoctorReport(
                edition=edition_name or "unknown",
                checks=(DoctorCheck("Personas", "fail", f"Could not resolve edition '{edition_name}'."),),
            )
    else:
        program_dirs = [path for path in programs_root.iterdir() if (path / "program.yaml").exists()]
        if len(program_dirs) == 1:
            program_id = program_dirs[0].name
        elif not program_dirs:
            return DoctorReport(
                edition="personas",
                checks=(DoctorCheck("Personas", "fail", "No program.yaml found in programs/."),),
            )
        else:
            return DoctorReport(
                edition="personas",
                checks=(DoctorCheck("Personas", "fail", "Specify --edition when multiple programs exist."),),
            )

    program_dir = programs_root / program_id
    personas_path = program_dir / "knowledge" / "personas.yaml"

    if not personas_path.exists():
        return DoctorReport(
            edition=program_id,
            checks=(DoctorCheck("Personas", "warn", "No personas.yaml found — persona enforcement inactive."),),
        )

    re2_available = find_spec("re2") is not None
    checks.append(
        DoctorCheck(
            "Personas",
            "warn" if not re2_available else "ok",
            f"google-re2 {'available' if re2_available else 'NOT available — install google-re2 for guaranteed linear-time regex matching'}",
        )
    )

    try:
        raw = load_yaml_document(personas_path)
    except (OSError, yaml.YAMLError) as error:
        return DoctorReport(
            edition=program_id,
            checks=(DoctorCheck("Personas", "fail", f"Failed to load personas.yaml: {error}"),),
        )

    if not isinstance(raw, dict):
        return DoctorReport(
            edition=program_id,
            checks=(DoctorCheck("Personas", "fail", "personas.yaml must be a mapping"),),
        )

    sv = raw.get("schema_version", "")
    if not sv:
        checks.append(DoctorCheck("Personas", "fail", "personas.yaml missing schema_version"))
    elif not _SCHEMA_VERSION_RE.match(str(sv)):
        checks.append(DoctorCheck("Personas", "fail", f"personas.yaml has unparseable schema_version '{sv}'"))
    else:
        checks.append(DoctorCheck("Personas", "ok", f"personas.yaml schema_version {sv}"))

    enforcement_raw = raw.get("enforcement", {})
    if not isinstance(enforcement_raw, dict):
        checks.append(DoctorCheck("Personas", "fail", "personas.yaml enforcement must be a mapping"))
    else:
        mode = enforcement_raw.get("mode", "enforce")
        staleness = enforcement_raw.get("staleness_threshold_days", 90)
        checks.append(DoctorCheck("Personas", "ok", f"enforcement: mode={mode}, staleness_threshold_days={staleness}"))

    personas_raw = raw.get("personas", [])
    if not isinstance(personas_raw, list):
        checks.append(DoctorCheck("Personas", "fail", "personas.yaml personas must be a list"))
        return DoctorReport(edition=program_id, checks=tuple(checks))

    personas_parsed: list[PersonaDefinition] = []
    all_check_ids: set[str] = set()
    duplicate_check_ids: list[str] = []
    duplicate_persona_ids: list[str] = []
    seen_persona_ids: set[str] = set()
    phase_warnings: list[str] = []
    staleness_warnings: list[str] = []
    density_failures: list[str] = []
    quarantine_warnings: list[str] = []
    regex_errors: list[str] = []
    rule_ref_errors: list[str] = []
    enforce_after_warnings: list[str] = []

    today = datetime.now(timezone.utc).date()
    staleness_threshold = int(enforcement_raw.get("staleness_threshold_days", 90)) if isinstance(enforcement_raw, dict) else 90

    for raw_persona in personas_raw:
        if not isinstance(raw_persona, dict):
            continue
        persona_id = str(raw_persona.get("id", "")).strip()
        if not _PERSONA_ID_RE.match(persona_id):
            checks.append(DoctorCheck("Personas", "fail", f"Invalid persona id '{persona_id}'"))
            continue
        if persona_id in seen_persona_ids:
            duplicate_persona_ids.append(persona_id)
        seen_persona_ids.add(persona_id)

        raw_checks = raw_persona.get("checks", []) or []
        parsed_checks: list[PersonaCheck] = []
        local_check_ids: set[str] = set()
        for raw_check in raw_checks:
            if not isinstance(raw_check, dict):
                continue
            check_id = str(raw_check.get("id", "")).strip()
            if not _PERSONA_ID_RE.match(check_id):
                checks.append(DoctorCheck("Personas", "fail", f"Invalid check id '{check_id}' in persona {persona_id}"))
                continue
            if check_id in local_check_ids:
                duplicate_check_ids.append(f"{persona_id}/{check_id}")
            local_check_ids.add(check_id)
            if check_id in all_check_ids:
                duplicate_check_ids.append(f"{persona_id}/{check_id}")
            all_check_ids.add(check_id)

            check_type = str(raw_check.get("type", ""))
            if check_type not in CHECK_TYPE_REGISTRY:
                quarantine_warnings.append(f"{persona_id}/{check_id}: unknown check type '{check_type}'")

            scope_val = raw_check.get("scope", "")
            if isinstance(scope_val, list):
                for scope_item in scope_val:
                    if not isinstance(scope_item, str) or not scope_item:
                        checks.append(DoctorCheck("Personas", "fail", f"{persona_id}/{check_id}: empty scope item"))
            elif not isinstance(scope_val, str) or not scope_val:
                checks.append(DoctorCheck("Personas", "fail", f"{persona_id}/{check_id}: invalid scope '{scope_val}'"))

            severity = str(raw_check.get("severity", "warn"))
            if severity not in ("warn", "block"):
                checks.append(DoctorCheck("PersonaSchema", "fail", f"{persona_id}/{check_id}: severity must be 'warn' or 'block', got '{severity}'"))

            check = PersonaCheck(
                id=check_id,
                type=check_type,
                scope=scope_val,
                message=str(raw_check.get("message", "")),
                severity=severity,
                keywords=tuple(raw_check.get("keywords", [])) or (),
                pattern=raw_check.get("pattern"),
                regex_flags=raw_check.get("regex_flags"),
                threshold=raw_check.get("threshold"),
                element=raw_check.get("element"),
                rule_ref=raw_check.get("rule_ref"),
                enforce_after=raw_check.get("enforce_after"),
                updated_at=raw_check.get("updated_at"),
                phase=raw_check.get("phase"),
                requires=tuple(raw_check.get("requires", [])) or (),
                strict_scope=bool(raw_check.get("strict_scope", False)),
            )
            parsed_checks.append(check)

            if check.pattern:
                if len(check.pattern) > 500:
                    regex_errors.append(f"{persona_id}/{check_id}: pattern exceeds 500 char limit ({len(check.pattern)})")
                try:
                    re.compile(check.pattern)
                except re.error as error:
                    regex_errors.append(f"{persona_id}/{check_id}: invalid regex: {error}")
                if _NESTED_QUANTIFIER_RE.search(check.pattern):
                    regex_errors.append(f"{persona_id}/{check_id}: nested quantifier detected in pattern")

            if check.enforce_after:
                try:
                    enforce_date = date.fromisoformat(check.enforce_after)
                    if enforce_date < today:
                        if check.updated_at is None:
                            enforce_after_warnings.append(f"{persona_id}/{check_id}: enforce_after {check.enforce_after} has passed and updated_at is not set")
                        else:
                            try:
                                updated_date = date.fromisoformat(check.updated_at)
                                if updated_date <= enforce_date:
                                    enforce_after_warnings.append(f"{persona_id}/{check_id}: enforce_after {check.enforce_after} has passed without updated_at update")
                            except ValueError:
                                enforce_after_warnings.append(f"{persona_id}/{check_id}: invalid updated_at date '{check.updated_at}'")
                except ValueError:
                    regex_errors.append(f"{persona_id}/{check_id}: invalid enforce_after date '{check.enforce_after}'")

        persona_def = PersonaDefinition(
            id=persona_id,
            priority=str(raw_persona.get("priority", "normal")),
            role=raw_persona.get("role"),
            owner=raw_persona.get("owner"),
            frame=raw_persona.get("frame"),
            always_active=bool(raw_persona.get("always_active", False)),
            checks=tuple(parsed_checks),
        )
        personas_parsed.append(persona_def)

        for check in parsed_checks:
            if check.phase is not None and check.phase not in _PERSONA_VALID_PHASES:
                phase_warnings.append(f"{persona_id}/{check.id}: unknown phase '{check.phase}'")

        if persona_def.priority == "critical":
            for check in parsed_checks:
                if check.updated_at:
                    try:
                        updated_date = date.fromisoformat(check.updated_at)
                        age_days = (today - updated_date).days
                        if age_days > staleness_threshold:
                            staleness_warnings.append(f"{persona_id}/{check.id}: check is {age_days} days stale (threshold={staleness_threshold})")
                    except ValueError:
                        pass

        priority = persona_def.priority
        check_count = len(parsed_checks)
        min_required = {"critical": 3, "high": 2, "normal": 1}.get(priority, 1)
        if check_count < min_required:
            density_failures.append(f"{persona_id} ({priority}): has {check_count} checks, minimum {min_required} required")

    if duplicate_persona_ids:
        checks.append(DoctorCheck("Personas", "fail", f"Duplicate persona ids: {', '.join(sorted(set(duplicate_persona_ids)))}"))
    if duplicate_check_ids:
        checks.append(DoctorCheck("Personas", "fail", f"Duplicate check ids: {', '.join(sorted(set(duplicate_check_ids)))}"))

    for persona in personas_parsed:
        by_id = {check.id: check for check in persona.checks}
        visited: set[str] = set()
        path: list[str] = []

        def visit(check_id: str) -> bool:
            if check_id in path:
                return True
            if check_id in visited:
                return False
            visited.add(check_id)
            path.append(check_id)
            if check_id in by_id:
                for dependency in by_id[check_id].requires:
                    if visit(dependency):
                        return True
            path.pop()
            return False

        for check in persona.checks:
            if visit(check.id):
                checks.append(DoctorCheck("Personas", "fail", f"{persona.id}: requires cycle detected"))
                break

    structural_rule_ids: set[str] = set()
    try:
        bundle = load_bundle(
            edition_name or program_id,
            reports_root=reports_root,
            editions_root=editions_root,
            programs_root=programs_root,
        )
        if bundle and bundle.editorial_rules:
            structural_rule_ids = {rule.id for rule in bundle.editorial_rules.structural_rules}
    except (OSError, KeyError, ValueError, yaml.YAMLError):
        pass

    for persona in personas_parsed:
        by_id = {check.id: check for check in persona.checks}
        for check in persona.checks:
            if check.rule_ref:
                ref = str(check.rule_ref)
                if ref not in _PERSONA_KNOWN_BAN_CATEGORIES and ref not in structural_rule_ids:
                    rule_ref_errors.append(f"{persona.id}/{check.id}: rule_ref '{ref}' not found (not a known ban-list category or structural rule id)")
            for dependency in check.requires:
                if dependency not in by_id:
                    checks.append(DoctorCheck("Personas", "fail", f"{persona.id}/{check.id}: requires '{dependency}' not found in same persona"))

    if not checks or all(check.status != "fail" for check in checks):
        checks.append(DoctorCheck("Personas", "ok", f"personas.yaml loaded: {len(personas_parsed)} personas, {len(all_check_ids)} checks"))

    for message in regex_errors:
        checks.append(DoctorCheck("PersonaSchema", "fail", message))
    for message in rule_ref_errors:
        checks.append(DoctorCheck("PersonaSchema", "fail", message))
    for message in enforce_after_warnings:
        checks.append(DoctorCheck("Personas", "warn", message))
    for message in phase_warnings:
        checks.append(DoctorCheck("Personas", "warn", message))
    for message in staleness_warnings:
        checks.append(DoctorCheck("Personas", "warn", message))
        try:
            append_context_gap(
                feature="doctor --context",
                program=program_id,
                field="personas.check.updated_at",
                severity="quality_degraded",
                message=message,
                impact_estimate="medium",
                programs_root=programs_root,
            )
        except OSError:
            pass
    for message in density_failures:
        checks.append(DoctorCheck("Personas", "warn", message))
    for message in quarantine_warnings:
        checks.append(DoctorCheck("Personas", "warn", message))

    return DoctorReport(edition=program_id, checks=tuple(checks))
