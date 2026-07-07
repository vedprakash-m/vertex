from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src.core.edition_resolver import EDITIONS_ROOT
from src.core.config_loader import PROGRAMS_ROOT
from src.core.exceptions import ConfigError
from src.core.program_paths import get_platform_proof_log_path
from src.core.edition_resolver import resolve_edition_paths
from src.core.platform_proof_catalog import validate_platform_proof_identity


@dataclass(frozen=True, slots=True)
class PlatformProofRecord:
    proof_id: str
    status: str
    recorded_at: datetime
    program_id: str
    recorded_by: str | None = None
    edition: str | None = None
    notes: str | None = None
    elapsed_minutes: float | None = None
    no_code_changes: bool | None = None
    confirm_exit_code: int | None = None
    archetype: str | None = None


def load_platform_proof_records(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[PlatformProofRecord, ...]:
    path = get_platform_proof_log_path(program_id, programs_root=programs_root)
    if not path.exists():
        return ()

    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {path}.") from error

    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping in {path}.")

    schema_version = _required_string(document.get("schema_version"), field_name="schema_version").strip()
    if schema_version.split(".", 1)[0] != "1":
        raise ConfigError(f"Unsupported platform proof log schema_version {schema_version!r} in {path}.")

    raw_proofs = document.get("proofs")
    if raw_proofs is None:
        return ()
    if not isinstance(raw_proofs, list):
        raise ConfigError(f"Expected 'proofs' list in {path}.")

    proofs: list[PlatformProofRecord] = []
    for index, entry in enumerate(raw_proofs, start=1):
        if not isinstance(entry, dict):
            raise ConfigError(f"Proof entry #{index} in {path} must be a mapping.")
        proof_id = _required_string(entry.get("proof_id"), field_name="proof_id").strip()
        status = _required_string(entry.get("status"), field_name="status").strip().lower()
        if not proof_id:
            raise ConfigError(f"Proof entry #{index} in {path} is missing proof_id.")
        if status not in {"passed", "failed"}:
            raise ConfigError(f"Proof entry '{proof_id}' in {path} must use status 'passed' or 'failed'.")
        raw_archetype = _optional_string(entry.get("archetype"), field_name="archetype")
        try:
            _, normalized_archetype = validate_platform_proof_identity(proof_id=proof_id, archetype=raw_archetype)
        except ValueError as error:
            raise ConfigError(f"Invalid proof entry '{proof_id}' in {path}: {error}") from error
        proofs.append(
            PlatformProofRecord(
                proof_id=proof_id,
                status=status,
                recorded_at=_parse_datetime(entry.get("recorded_at")),
                program_id=_optional_string(entry.get("program_id"), field_name="program_id") or program_id,
                recorded_by=_optional_string(entry.get("recorded_by"), field_name="recorded_by"),
                edition=_optional_string(entry.get("edition"), field_name="edition"),
                notes=_optional_string(entry.get("notes"), field_name="notes"),
                elapsed_minutes=_optional_float(entry.get("elapsed_minutes"), field_name="elapsed_minutes"),
                no_code_changes=_optional_bool(entry.get("no_code_changes"), field_name="no_code_changes"),
                confirm_exit_code=_optional_int(entry.get("confirm_exit_code"), field_name="confirm_exit_code"),
                archetype=normalized_archetype,
            )
        )
    return tuple(proofs)


def record_platform_proof(
    *,
    program_id: str,
    proof_id: str,
    status: str,
    recorded_at: datetime,
    recorded_by: str | None,
    edition: str | None = None,
    notes: str | None = None,
    elapsed_minutes: float | None = None,
    no_code_changes: bool | None = None,
    confirm_exit_code: int | None = None,
    archetype: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> PlatformProofRecord:
    normalized_proof_id = proof_id.strip()
    normalized_status = status.strip().lower()
    if not normalized_proof_id:
        raise ValueError("proof_id must be non-empty.")
    if normalized_status not in {"passed", "failed"}:
        raise ValueError("status must be 'passed' or 'failed'.")
    if elapsed_minutes is not None and elapsed_minutes < 0:
        raise ValueError("elapsed_minutes must be >= 0.")
    if confirm_exit_code is not None and confirm_exit_code < 0:
        raise ValueError("confirm_exit_code must be >= 0.")
    _, normalized_archetype = validate_platform_proof_identity(
        proof_id=normalized_proof_id,
        archetype=_optional_string(archetype, field_name="archetype"),
    )

    new_record = PlatformProofRecord(
        proof_id=normalized_proof_id,
        status=normalized_status,
        recorded_at=_require_aware_datetime(recorded_at),
        program_id=program_id,
        recorded_by=_optional_string(recorded_by, field_name="recorded_by"),
        edition=_optional_string(edition, field_name="edition"),
        notes=_optional_string(notes, field_name="notes"),
        elapsed_minutes=elapsed_minutes,
        no_code_changes=no_code_changes,
        confirm_exit_code=confirm_exit_code,
        archetype=normalized_archetype,
    )
    existing_records = list(load_platform_proof_records(program_id, programs_root=programs_root))
    if existing_records and existing_records[-1] == new_record:
        return existing_records[-1]
    existing_records.append(new_record)
    _write_platform_proof_records(program_id, tuple(existing_records), programs_root=programs_root)
    return new_record


def resolve_platform_proof_program(
    *,
    edition: str | None,
    program: str | None,
    editions_root: Path = EDITIONS_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[str, str | None]:
    normalized_program = _optional_string(program, field_name="program")
    normalized_edition = _optional_string(edition, field_name="edition")
    if normalized_program is None and normalized_edition is None:
        raise ValueError("Provide --program or --edition.")
    if normalized_edition is None:
        return normalized_program or "", None
    resolved = resolve_edition_paths(
        normalized_edition,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if resolved is None:
        raise ValueError(f"Edition '{normalized_edition}' could not be resolved.")
    if normalized_program is not None and normalized_program != resolved.program_id:
        raise ValueError(
            f"--program {normalized_program!r} does not match edition '{normalized_edition}' (program {resolved.program_id!r})."
        )
    return resolved.program_id, normalized_edition


def _write_platform_proof_records(
    program_id: str,
    records: tuple[PlatformProofRecord, ...],
    *,
    programs_root: Path,
) -> Path:
    path = get_platform_proof_log_path(program_id, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "proofs": [
            {
                "proof_id": record.proof_id,
                "status": record.status,
                "recorded_at": record.recorded_at.isoformat(),
                "program_id": record.program_id,
                "recorded_by": record.recorded_by,
                "edition": record.edition,
                "notes": record.notes,
                "elapsed_minutes": record.elapsed_minutes,
                "no_code_changes": record.no_code_changes,
                "confirm_exit_code": record.confirm_exit_code,
                "archetype": record.archetype,
            }
            for record in records
        ],
    }
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    return path


def _parse_datetime(value: object) -> datetime:
    if value in (None, ""):
        raise ConfigError("platform proof log entries require recorded_at.")
    if isinstance(value, datetime):
        return _require_aware_datetime(value, error_type=ConfigError)
    if not isinstance(value, str):
        raise ConfigError("platform proof log recorded_at must be a string.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ConfigError(f"Invalid datetime value {value!r} in platform proof log.") from error
    return _require_aware_datetime(parsed, error_type=ConfigError)


def _require_aware_datetime(value: datetime, *, error_type: type[Exception] = ValueError) -> datetime:
    if value.tzinfo is None:
        raise error_type("platform proof log recorded_at must include timezone information.")
    return value.astimezone(timezone.utc)


def _required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"platform proof log {field_name} must be a string.")
    return value


def _optional_string(value: object, *, field_name: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ConfigError(f"platform proof log {field_name} must be a string.")
    return value.strip() or None


def _optional_float(value: object, *, field_name: str) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"platform proof log {field_name} must be numeric.")
    return float(value)


def _optional_int(value: object, *, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"platform proof log {field_name} must be an integer.")
    return value


def _optional_bool(value: object, *, field_name: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise ConfigError(f"platform proof log {field_name} must be a boolean.")
