from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
import re
from typing import Any

import yaml

from src.core.edition_resolver import PROGRAMS_ROOT, REPO_ROOT
from src.core.exceptions import ConfigError
from src.core.source_health import SourceWaiver


SCHEMA_VERSION = "1.0"
DEFAULT_ALLOWED_ROLES: tuple[str, ...] = ("telemetry", "advisory", "unbacked")
_OWNER_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True, slots=True)
class SourceWaiverFieldSpec:
    """Specification for a single field on a SourceWaiver."""

    field_name: str
    type_name: str
    required: bool
    allowed_values: tuple[str, ...] = ()
    pattern: str | None = None
    min_length: int | None = None
    default: Any = None
    invariants: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceWaiverSchema:
    """Structured view of `vertex/policies/source_waivers.schema.yaml`.

    Carries the schema metadata + a tuple of waiver field specs so
    doctor and runtime can validate program-level waiver files against
    a single source of truth.
    """

    schema_id: str
    schema_version: str
    description: str
    location_program: str
    waiver_fields: tuple[SourceWaiverFieldSpec, ...]
    allowed_roles: tuple[str, ...]
    owner_email_pattern: str
    raw: Mapping[str, Any] = field(default_factory=dict)


def load_source_waivers(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[SourceWaiver, ...]:
    path = programs_root / program_id / "source_waivers.yaml"
    if not path.exists():
        return ()
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, ValueError) as error:
        raise ConfigError(f"Invalid source waiver document in {path}") from error
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    if document.get("schema_version") != "1.0":
        raise ConfigError(f"Unsupported source waiver schema version in {path}")
    raw_waivers = document.get("waivers", [])
    if not isinstance(raw_waivers, list):
        raise ConfigError(f"waivers must be a list in {path}")
    return tuple(_parse_source_waiver(raw_waiver, path=path, program_id=program_id) for raw_waiver in raw_waivers)


def load_source_waivers_schema(
    *,
    policies_root: Path | None = None,
) -> SourceWaiverSchema:
    """Load the canonical source-waiver schema and return a structured view.

    The schema file lives at ``vertex/policies/source_waivers.schema.yaml``
    (relative to the repo root) and is the governance artifact that
    materializes D-32. Validators MUST call this loader rather than
    hard-coding the contract.
    """

    resolved_policies_root = policies_root if policies_root is not None else REPO_ROOT / "vertex" / "policies"
    schema_path = resolved_policies_root / "source_waivers.schema.yaml"
    if not schema_path.exists():
        raise ConfigError(
            f"Source waiver schema is missing at {schema_path}. "
            "D-32 requires vertex/policies/source_waivers.schema.yaml to exist."
        )
    try:
        document = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, ValueError) as error:
        raise ConfigError(f"Invalid source waiver schema at {schema_path}") from error
    if not isinstance(document, Mapping):
        raise ConfigError(f"Expected mapping at top-level in {schema_path}")
    version = document.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ConfigError(
            f"Unsupported source waiver schema version {version!r} in {schema_path}; expected {SCHEMA_VERSION!r}."
        )
    schema_id = str(document.get("schema_id") or "").strip()
    if not schema_id:
        raise ConfigError(f"source_waivers.schema.yaml is missing schema_id in {schema_path}")
    description = str(document.get("description") or "").strip()
    location = document.get("location") or {}
    location_program = ""
    if isinstance(location, Mapping):
        location_program = str(location.get("program") or "").strip()
    raw_waiver_fields = document.get("waiver_fields")
    if not isinstance(raw_waiver_fields, list) or not raw_waiver_fields:
        raise ConfigError(f"waiver_fields must be a non-empty list in {schema_path}")
    field_specs: list[SourceWaiverFieldSpec] = []
    for raw_field in raw_waiver_fields:
        if not isinstance(raw_field, Mapping):
            raise ConfigError(f"Each waiver_fields entry must be a mapping in {schema_path}")
        spec = _parse_field_spec(raw_field, schema_path=schema_path)
        field_specs.append(spec)
    role_spec = next((spec for spec in field_specs if spec.field_name == "role"), None)
    allowed_roles = role_spec.allowed_values if role_spec is not None else DEFAULT_ALLOWED_ROLES
    if not allowed_roles:
        allowed_roles = DEFAULT_ALLOWED_ROLES
    owner_spec = next((spec for spec in field_specs if spec.field_name == "owner"), None)
    owner_pattern = owner_spec.pattern if owner_spec is not None and owner_spec.pattern else _OWNER_EMAIL_RE.pattern
    return SourceWaiverSchema(
        schema_id=schema_id,
        schema_version=version,
        description=description,
        location_program=location_program,
        waiver_fields=tuple(field_specs),
        allowed_roles=allowed_roles,
        owner_email_pattern=owner_pattern,
        raw=dict(document),
    )


def _parse_field_spec(raw: Mapping[str, Any], *, schema_path: Path) -> SourceWaiverFieldSpec:
    field_name = str(raw.get("field") or "").strip()
    if not field_name:
        raise ConfigError(f"waiver_fields entry is missing 'field' in {schema_path}")
    type_name = str(raw.get("type") or "").strip()
    if not type_name:
        raise ConfigError(f"waiver_fields[{field_name}] is missing 'type' in {schema_path}")
    required = bool(raw.get("required", False))
    allowed_values_raw = raw.get("allowed_values")
    allowed_values: tuple[str, ...] = ()
    if isinstance(allowed_values_raw, list):
        allowed_values = tuple(str(value) for value in allowed_values_raw)
    pattern_value = raw.get("pattern")
    pattern = str(pattern_value).strip() if isinstance(pattern_value, str) and pattern_value.strip() else None
    min_length_raw = raw.get("min_length")
    min_length = int(min_length_raw) if isinstance(min_length_raw, int) and min_length_raw > 0 else None
    default_value: Any = raw.get("default")
    invariants_raw = raw.get("invariants")
    invariants: tuple[str, ...] = ()
    if isinstance(invariants_raw, list):
        invariants = tuple(str(item).strip() for item in invariants_raw if str(item).strip())
    return SourceWaiverFieldSpec(
        field_name=field_name,
        type_name=type_name,
        required=required,
        allowed_values=allowed_values,
        pattern=pattern,
        min_length=min_length,
        default=default_value,
        invariants=invariants,
    )


def validate_waiver_against_schema(
    waiver: SourceWaiver,
    *,
    schema: SourceWaiverSchema,
    today: date | None = None,
) -> tuple[list[str], list[str]]:
    """Return ``(errors, warnings)`` for a parsed waiver against a schema.

    This is the function doctor uses to verify each program waiver
    against the canonical contract. It is conservative: it does NOT
    reparse YAML, but it does enforce the invariants and structural
    rules described in the schema.
    """

    errors: list[str] = []
    warnings: list[str] = []
    if waiver.role not in schema.allowed_roles:
        errors.append(
            f"role={waiver.role!r} is not in the allowed set {list(schema.allowed_roles)}."
        )
    owner_re = re.compile(schema.owner_email_pattern)
    if not owner_re.match(waiver.owner):
        errors.append(f"owner={waiver.owner!r} is not email-shaped.")
    reason_spec = next((spec for spec in schema.waiver_fields if spec.field_name == "reason"), None)
    if reason_spec is not None and reason_spec.min_length is not None and len(waiver.reason.strip()) < reason_spec.min_length:
        errors.append(
            f"reason is shorter than the required minimum length ({reason_spec.min_length} chars)."
        )
    if waiver.expires < waiver.granted:
        errors.append("expires must be on or after granted.")
    if today is not None and waiver.expires < today:
        warnings.append(
            f"waiver expired on {waiver.expires.isoformat()}; doctor will keep surfacing it until removed."
        )
    return errors, warnings


def _parse_source_waiver(
    raw_waiver: object,
    *,
    path: Path,
    program_id: str,
) -> SourceWaiver:
    if not isinstance(raw_waiver, Mapping):
        raise ConfigError(f"Each source waiver must be a mapping in {path}")
    contract_id = _require_non_empty_string(raw_waiver.get("contract_id"), path=path, field_name="contract_id", program_id=program_id)
    role = _require_non_empty_string(raw_waiver.get("role"), path=path, field_name="role", program_id=program_id)
    owner = _require_non_empty_string(raw_waiver.get("owner"), path=path, field_name="owner", program_id=program_id)
    reason = _require_non_empty_string(raw_waiver.get("reason"), path=path, field_name="reason", program_id=program_id)
    granted = _require_date(raw_waiver.get("granted"), path=path, field_name="granted", program_id=program_id)
    expires = _require_date(raw_waiver.get("expires"), path=path, field_name="expires", program_id=program_id)
    if expires < granted:
        raise ConfigError(f"Invalid source waiver for '{program_id}' in {path}: expires must be on or after granted.")
    return SourceWaiver(
        contract_id=contract_id,
        role=role,
        owner=owner,
        reason=reason,
        granted=granted,
        expires=expires,
    )


def _require_non_empty_string(
    value: object,
    *,
    path: Path,
    field_name: str,
    program_id: str,
) -> str:
    normalized = str(value or "").strip()
    if normalized:
        return normalized
    raise ConfigError(f"Invalid source waiver for '{program_id}' in {path}: missing {field_name}.")


def _require_date(
    value: object,
    *,
    path: Path,
    field_name: str,
    program_id: str,
) -> date:
    normalized = str(value or "").strip()
    if not normalized:
        raise ConfigError(f"Invalid source waiver for '{program_id}' in {path}: missing {field_name}.")
    try:
        return date.fromisoformat(normalized)
    except ValueError as error:
        raise ConfigError(f"Invalid source waiver for '{program_id}' in {path}: {field_name} must be YYYY-MM-DD.") from error


def is_waiver_active(waiver: SourceWaiver, *, today: date | None = None) -> bool:
    """Section 11.3 acceptance evidence: a waiver is active only within its
    [granted, expires] window (both inclusive). An expired waiver is NOT
    active -- confirm-time logic still considers it until re-anchored, but
    signal/cockpit surfaces must treat it as expired so the operator knows the
    gate is no longer formally covered."""
    resolved_today = today or date.today()
    return waiver.granted <= resolved_today <= waiver.expires


def find_waiver_for_query(
    query_id: str,
    waivers: tuple[SourceWaiver, ...],
    slice_contracts: tuple[Any, ...],
    *,
    today: date | None = None,
) -> SourceWaiver | None:
    """ADF-W2.3 (Section 8.5.3 / 11.3): the policy integration deferred in the
    prior pass. Bridges ``query_id -> contract_id -> waiver`` using slice
    contracts: a Kusto query's ``query_id`` is the telemetry source for a
    ``SliceContract`` whose ``source_contract.telemetry.query_id`` matches;
    that contract's ``id`` is the ``contract_id`` a ``SourceWaiver`` keys on.

    Returns the first active (non-expired) waiver for the query's telemetry
    role, or ``None`` when no waiver exists or the query has no bound slice
    contract. ``slice_contracts`` is typed ``Any`` to avoid importing
    ``SliceContract`` (which would create a Zone-A -> Zone-A coupling the
    import-boundary contract does not forbid, but the lazy-duck-type keeps this
    helper testable with simple fakes without pulling the full contract loader).
    """
    if not query_id or not waivers:
        return None
    # Find the slice contract whose telemetry query_id matches.
    contract_ids_for_query: list[str] = []
    for contract in slice_contracts:
        telemetry = getattr(getattr(contract, "source_contract", None), "telemetry", None)
        if telemetry is not None and getattr(telemetry, "query_id", None) == query_id:
            contract_id = getattr(contract, "id", None)
            if isinstance(contract_id, str) and contract_id:
                contract_ids_for_query.append(contract_id)

    if not contract_ids_for_query:
        return None

    for waiver in waivers:
        if waiver.contract_id in contract_ids_for_query and waiver.role == "telemetry" and is_waiver_active(waiver, today=today):
            return waiver
    return None
