from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Callable

import typer
import yaml

from src.commands.onboard import run_onboard_migrate_v3
from src.core.claim_extraction_calibration_store import summarize_claim_extraction_calibration
from src.core.exceptions import ConfigError
from src.core.edition_resolver import resolve_edition_paths
from src.core.journal import PROGRAMS_ROOT


@dataclass(frozen=True, slots=True)
class ConfigKeySpec:
    path: str
    default_value: Any
    parser: Callable[[str], Any]
    description: str
    governance_critical: bool = False


@dataclass(frozen=True, slots=True)
class ConfigSchemaTarget:
    name: str
    path: Path
    expected_major: int
    expected_minor: int
    required: bool


@dataclass(frozen=True, slots=True)
class ConfigSchemaAssessment:
    name: str
    path: Path
    status: str
    detail: str
    version: str | None
    expected_version: str


SCHEMA_VERSION = "3.0"
_SCHEMA_BASELINES = {
    "edition": (2, 0),
    "program": (3, 0),
    "readiness": (1, 0),
    "template_contract": (1, 0),
    "slice_contracts": (1, 0),
    "chapter_contract": (1, 0),
}


app = typer.Typer(help="Inspect and update governed program configuration.", invoke_without_command=True)


@app.callback(invoke_without_command=True)
def config_command(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    typer.echo(ctx.get_help())
    raise typer.Exit(code=0)


@app.command("get")
def get_command(
    key: str = typer.Argument(..., help="Allowlisted program.yaml key path, for example catchup.enabled."),
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
) -> None:
    spec = _get_spec(key)
    document, path = _load_program_document(program.strip(), programs_root=PROGRAMS_ROOT)
    exists, value = _get_nested_value(document, spec.path)
    resolved_value = value if exists else spec.default_value
    source = "explicit" if exists else "default"

    typer.echo(
        f"{program.strip()} {spec.path} = {_format_literal(resolved_value)} "
        f"({source}; {path.relative_to(PROGRAMS_ROOT.parent).as_posix()})"
    )
    raise typer.Exit(code=0)


@app.command("set")
def set_command(
    key: str = typer.Argument(..., help="Allowlisted program.yaml key path, for example catchup.enabled."),
    value: str = typer.Argument(..., help="Literal value to persist."),
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and preview without writing program.yaml."),
) -> None:
    program_id = program.strip()
    spec = _get_spec(key)
    document, path = _load_program_document(program_id, programs_root=PROGRAMS_ROOT)
    parsed_value = _parse_value(spec, value)
    exists, current_value = _get_nested_value(document, spec.path)
    effective_current = current_value if exists else spec.default_value

    _enforce_governance(
        spec,
        program_id=program_id,
        document=document,
        programs_root=PROGRAMS_ROOT,
        current_value=effective_current,
        next_value=parsed_value,
    )

    updated_document = deepcopy(document)
    _set_nested_value(updated_document, spec.path, parsed_value)
    updated_document["schema_version"] = SCHEMA_VERSION
    changed = updated_document != document

    if dry_run:
        typer.echo(
            f"Dry-run: would set {program_id} {spec.path} from {_format_literal(effective_current)} "
            f"to {_format_literal(parsed_value)} in {path.relative_to(PROGRAMS_ROOT.parent).as_posix()}."
        )
        raise typer.Exit(code=0)

    if not changed:
        typer.echo(
            f"No change: {program_id} {spec.path} is already {_format_literal(parsed_value)} in "
            f"{path.relative_to(PROGRAMS_ROOT.parent).as_posix()}."
        )
        raise typer.Exit(code=0)

    _write_program_document(path, updated_document)
    typer.echo(
        f"Updated {program_id} {spec.path} from {_format_literal(effective_current)} "
        f"to {_format_literal(parsed_value)} in {path.relative_to(PROGRAMS_ROOT.parent).as_posix()}."
    )
    raise typer.Exit(code=0)


@app.command("validate")
def validate_command(
    edition: str | None = typer.Option(None, "--edition", help="Edition id, e.g. myprogram_weekly."),
    program: str | None = typer.Option(None, "--program", help="Program id, e.g. myprogram."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    resolved_format = format.strip().lower()
    if resolved_format not in {"human", "json"}:
        raise typer.BadParameter("--format must be one of: human, json")

    resolved_targets = _resolve_schema_targets(edition=edition, program=program, programs_root=PROGRAMS_ROOT)
    assessments = tuple(_assess_config_schema(target) for target in resolved_targets)
    status = _summarize_schema_status(assessments)

    payload = {
        "status": status,
        "edition": edition.strip() if isinstance(edition, str) and edition.strip() else None,
        "program": next(
            (target.path.parent.name for target in resolved_targets if target.name == "program"),
            None,
        ),
        "assessments": [
            {
                "name": assessment.name,
                "path": str(assessment.path),
                "status": assessment.status,
                "detail": assessment.detail,
                "version": assessment.version,
                "expected_version": assessment.expected_version,
            }
            for assessment in assessments
        ],
    }
    if resolved_format == "json":
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(f"Config schema validation: {status.upper()}")
        for assessment in assessments:
            typer.echo(
                f"- {assessment.name}: {assessment.status.upper()} "
                f"({assessment.path.relative_to(PROGRAMS_ROOT.parent).as_posix()})"
            )
            typer.echo(f"  {assessment.detail}")

    if status == "fail":
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


@app.command("migrate")
def migrate_command(
    edition: str | None = typer.Option(None, "--edition", help="Edition id, e.g. myprogram_weekly."),
    program: str | None = typer.Option(None, "--program", help="Program id, e.g. myprogram."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview schema updates without writing files."),
) -> None:
    resolved_targets = _resolve_schema_targets(edition=edition, program=program, programs_root=PROGRAMS_ROOT)
    operations: list[str] = []

    program_migration_edition = _resolve_program_schema_migration_edition(
        edition=edition,
        program=program,
        programs_root=PROGRAMS_ROOT,
    )
    program_target = next((target for target in resolved_targets if target.name == "program"), None)
    if program_target is not None:
        program_assessment = _assess_config_schema(program_target)
        if program_assessment.status == "fail" and _schema_major(program_assessment.version) == 2:
            operations.append(
                f"program schema: run onboard V3 migration for {program_migration_edition} "
                f"({program_target.path.relative_to(PROGRAMS_ROOT.parent).as_posix()})"
            )
            if not dry_run:
                run_onboard_migrate_v3(
                    program_migration_edition,
                    reports_root=PROGRAMS_ROOT.parent / "reports",
                )

    for target in resolved_targets:
        if target.name == "program":
            continue
        document = _maybe_load_mapping(target.path)
        if document is None:
            continue
        expected_version = f"{target.expected_major}.{target.expected_minor}"
        current_version = str(document.get("schema_version") or "").strip()
        if current_version == expected_version:
            continue
        current_major = _schema_major(current_version)
        if current_version and current_major is not None and current_major != target.expected_major:
            raise typer.BadParameter(
                f"Cannot auto-migrate {target.path}: schema_version {current_version!r} does not match "
                f"expected major {target.expected_major}.x."
            )
        operations.append(
            f"normalize {target.name} schema_version to {expected_version} "
            f"({target.path.relative_to(PROGRAMS_ROOT.parent).as_posix()})"
        )
        if not dry_run:
            document["schema_version"] = expected_version
            _write_program_document(target.path, document)

    if not operations:
        typer.echo("No config schema migrations required.")
        raise typer.Exit(code=0)

    if dry_run:
        typer.echo("Dry-run: would perform the following config schema migrations:")
    else:
        typer.echo("Applied config schema migrations:")
    for operation in operations:
        typer.echo(f"- {operation}")
    raise typer.Exit(code=0)


def _get_spec(key: str) -> ConfigKeySpec:
    normalized_key = key.strip()
    spec = _CONFIG_KEY_SPECS.get(normalized_key)
    if spec is None:
        allowed = ", ".join(sorted(_CONFIG_KEY_SPECS))
        raise typer.BadParameter(f"Unsupported config key '{normalized_key}'. Allowed keys: {allowed}")
    return spec


def _parse_value(spec: ConfigKeySpec, raw_value: str) -> Any:
    try:
        return spec.parser(raw_value)
    except ValueError as error:
        raise typer.BadParameter(f"Invalid value for {spec.path}: {error}") from error


def _load_program_document(program_id: str, *, programs_root: Path) -> tuple[dict[str, Any], Path]:
    path = programs_root / program_id / "program.yaml"
    if not path.exists():
        raise typer.BadParameter(f"Program '{program_id}' is missing program.yaml.")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {path}: {error}") from error
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}.")
    return document, path


def _get_nested_value(document: dict[str, Any], dotted_path: str) -> tuple[bool, Any]:
    current: Any = document
    for token in dotted_path.split("."):
        if not isinstance(current, dict) or token not in current:
            return False, None
        current = current[token]
    return True, current


def _set_nested_value(document: dict[str, Any], dotted_path: str, value: Any) -> None:
    tokens = dotted_path.split(".")
    current: dict[str, Any] = document
    for token in tokens[:-1]:
        next_value = current.get(token)
        if next_value is None:
            next_value = {}
            current[token] = next_value
        if not isinstance(next_value, dict):
            raise ConfigError(f"Cannot set {dotted_path}: '{token}' is not a mapping in program.yaml.")
        current = next_value
    current[tokens[-1]] = value


def _write_program_document(path: Path, document: dict[str, Any]) -> None:
    body = yaml.safe_dump(document, sort_keys=False, allow_unicode=False)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(body, encoding="utf-8")
    os.replace(temp_path, path)


def _resolve_schema_targets(
    *,
    edition: str | None,
    program: str | None,
    programs_root: Path,
) -> tuple[ConfigSchemaTarget, ...]:
    normalized_edition = edition.strip() if isinstance(edition, str) and edition.strip() else None
    normalized_program = program.strip() if isinstance(program, str) and program.strip() else None
    if normalized_edition and normalized_program:
        raise typer.BadParameter("Specify either --edition or --program, not both.")
    if not normalized_edition and not normalized_program:
        raise typer.BadParameter("One of --edition or --program is required.")

    if normalized_edition:
        resolved = resolve_edition_paths(
            normalized_edition,
            programs_root=programs_root,
        )
        if resolved is None:
            raise typer.BadParameter(f"Unknown edition '{normalized_edition}'.")
        program_dir = resolved.program_dir
        return (
            _build_schema_target("edition", resolved.edition_path),
            _build_schema_target("program", program_dir / "program.yaml"),
            _build_schema_target("readiness", program_dir / "readiness.yaml", required=False),
            _build_schema_target("template_contract", program_dir / "template_contract.yaml", required=False),
            _build_schema_target("slice_contracts", program_dir / "slice_contracts.yaml", required=False),
            _build_schema_target("chapter_contract", program_dir / "chapter_contract.yaml", required=False),
        )

    assert normalized_program is not None
    program_dir = programs_root / normalized_program
    if not program_dir.exists():
        raise typer.BadParameter(f"Unknown program '{normalized_program}'.")
    return (
        _build_schema_target("program", program_dir / "program.yaml"),
        _build_schema_target("readiness", program_dir / "readiness.yaml", required=False),
        _build_schema_target("template_contract", program_dir / "template_contract.yaml", required=False),
        _build_schema_target("slice_contracts", program_dir / "slice_contracts.yaml", required=False),
        _build_schema_target("chapter_contract", program_dir / "chapter_contract.yaml", required=False),
    )


def _build_schema_target(name: str, path: Path, *, required: bool | None = None) -> ConfigSchemaTarget:
    expected_major, expected_minor = _SCHEMA_BASELINES[name]
    return ConfigSchemaTarget(
        name=name,
        path=path,
        expected_major=expected_major,
        expected_minor=expected_minor,
        required=(name in {"edition", "program"} if required is None else required),
    )


def _assess_config_schema(target: ConfigSchemaTarget) -> ConfigSchemaAssessment:
    expected_version = f"{target.expected_major}.{target.expected_minor}"
    if not target.path.exists():
        status = "fail" if target.required else "warn"
        detail = f"{target.path} is missing."
        return ConfigSchemaAssessment(target.name, target.path, status, detail, None, expected_version)

    try:
        document = _load_yaml_mapping(target.path)
    except ConfigError as error:
        return ConfigSchemaAssessment(target.name, target.path, "fail", str(error), None, expected_version)

    raw_version = document.get("schema_version")
    if not isinstance(raw_version, str) or not raw_version.strip():
        return ConfigSchemaAssessment(
            target.name,
            target.path,
            "fail",
            f"{target.path} is missing schema_version.",
            None,
            expected_version,
        )

    version = raw_version.strip()
    major_minor = _parse_schema_version(version)
    if major_minor is None:
        return ConfigSchemaAssessment(
            target.name,
            target.path,
            "fail",
            f"{target.path} has invalid schema_version {version!r}.",
            version,
            expected_version,
        )

    major, minor = major_minor
    if major != target.expected_major:
        return ConfigSchemaAssessment(
            target.name,
            target.path,
            "fail",
            f"{target.path} declares schema_version {version}; expected major version {target.expected_major}.x "
            f"(baseline {expected_version}).",
            version,
            expected_version,
        )
    if minor != target.expected_minor:
        return ConfigSchemaAssessment(
            target.name,
            target.path,
            "warn",
            f"{target.path} declares schema_version {version}; expected baseline {expected_version}.",
            version,
            expected_version,
        )
    return ConfigSchemaAssessment(
        target.name,
        target.path,
        "ok",
        f"{target.path} schema_version {version} matches expected baseline {expected_version}.",
        version,
        expected_version,
    )


def _summarize_schema_status(assessments: tuple[ConfigSchemaAssessment, ...]) -> str:
    if any(assessment.status == "fail" for assessment in assessments):
        return "fail"
    if any(assessment.status == "warn" for assessment in assessments):
        return "warn"
    return "ok"


def _resolve_program_schema_migration_edition(
    *,
    edition: str | None,
    program: str | None,
    programs_root: Path,
) -> str:
    normalized_edition = edition.strip() if isinstance(edition, str) and edition.strip() else None
    if normalized_edition is not None:
        return normalized_edition

    assert program is not None
    editions_root = programs_root / program.strip() / "editions"
    matching_editions = sorted(
        edition_path.stem
        for edition_path in editions_root.glob("*.yaml")
    )
    if not matching_editions:
        raise typer.BadParameter(
            f"Program '{program.strip()}' has no edition YAML, so legacy program schema migration cannot infer an edition."
        )
    if len(matching_editions) > 1:
        joined = ", ".join(matching_editions)
        raise typer.BadParameter(
            f"Program '{program.strip()}' maps to multiple editions ({joined}); rerun with --edition to choose one."
        )
    return matching_editions[0]


def _edition_program_id(path: Path) -> str | None:
    try:
        document = _load_yaml_mapping(path)
    except ConfigError:
        return None
    raw_program = document.get("program_id")
    if not isinstance(raw_program, str) or not raw_program.strip():
        return None
    return raw_program.strip()


def _maybe_load_mapping(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return _load_yaml_mapping(path)


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {path}: {error}") from error
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}.")
    return document


def _parse_schema_version(version: str) -> tuple[int, int] | None:
    parts = version.split(".", 1)
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return int(parts[0]), int(parts[1])


def _schema_major(version: str | None) -> int | None:
    if not version:
        return None
    parsed = _parse_schema_version(version)
    return parsed[0] if parsed is not None else None


def _enforce_governance(
    spec: ConfigKeySpec,
    *,
    program_id: str,
    document: dict[str, Any],
    programs_root: Path,
    current_value: Any,
    next_value: Any,
) -> None:
    if not spec.governance_critical:
        return
    if spec.path == "ai.claim_extractor.mode" and current_value != "production" and next_value == "production":
        calibration_min_confirms = _claim_extractor_calibration_min_confirms(document)
        summary = summarize_claim_extraction_calibration(
            program_id,
            recent_cycles=10,
            programs_root=programs_root,
        )
        if summary.calibration_sample_count < calibration_min_confirms:
            raise typer.BadParameter(
                "ai.claim_extractor.mode=production requires calibration evidence: "
                f"{summary.calibration_sample_count}/{calibration_min_confirms} calibration confirm(s) recorded."
            )
        if summary.recent_sample_count < 10:
            raise typer.BadParameter(
                "ai.claim_extractor.mode=production requires 10 recent calibration cycles in claim-extraction trust history."
            )
        if summary.recent_agreement_rate < 0.85:
            raise typer.BadParameter(
                "ai.claim_extractor.mode=production requires >=85% recent claim-extraction agreement; "
                f"current={summary.recent_agreement_rate:.0%} over the last {summary.recent_sample_count} cycle(s)."
            )
        if summary.recent_average_difference_count > 2.0:
            raise typer.BadParameter(
                "ai.claim_extractor.mode=production requires average claim-extraction disagreement <=2.0 claims per cycle; "
                f"current={summary.recent_average_difference_count:.2f} over the last {summary.recent_sample_count} cycle(s)."
            )
        typer.echo("Claim extraction production promotion check:")
        typer.echo(
            f"- Calibration confirms: {summary.calibration_sample_count}/{calibration_min_confirms}"
        )
        typer.echo(
            f"- Recent agreement (last {summary.recent_sample_count} cycles): {summary.recent_agreement_rate:.0%}"
        )
        typer.echo(
            f"- Average disagreement count (last {summary.recent_sample_count} cycles): {summary.recent_average_difference_count:.2f}"
        )
        if not typer.confirm("Promote ai.claim_extractor.mode to production?", default=False):
            raise typer.Abort()


def _claim_extractor_calibration_min_confirms(document: dict[str, Any]) -> int:
    exists, value = _get_nested_value(document, "ai.claim_extractor.calibration_min_confirms")
    if not exists or value is None:
        return int(_CONFIG_KEY_SPECS["ai.claim_extractor.calibration_min_confirms"].default_value)
    return int(value)


def _parse_bool(raw_value: str) -> bool:
    normalized = raw_value.strip().lower()
    if normalized in {"true", "1", "yes", "y", "on"}:
        return True
    if normalized in {"false", "0", "no", "n", "off"}:
        return False
    raise ValueError("expected true/false")


def _parse_int(*, minimum: int | None = None, maximum: int | None = None) -> Callable[[str], int]:
    def parse(raw_value: str) -> int:
        normalized = raw_value.strip()
        try:
            parsed = int(normalized)
        except ValueError as error:
            raise ValueError("expected integer") from error
        if minimum is not None and parsed < minimum:
            raise ValueError(f"expected integer >= {minimum}")
        if maximum is not None and parsed > maximum:
            raise ValueError(f"expected integer <= {maximum}")
        return parsed

    return parse


def _parse_float(*, minimum: float | None = None, maximum: float | None = None) -> Callable[[str], float]:
    def parse(raw_value: str) -> float:
        normalized = raw_value.strip()
        try:
            parsed = float(normalized)
        except ValueError as error:
            raise ValueError("expected number") from error
        if minimum is not None and parsed < minimum:
            raise ValueError(f"expected number >= {minimum}")
        if maximum is not None and parsed > maximum:
            raise ValueError(f"expected number <= {maximum}")
        return parsed

    return parse


def _parse_enum(*allowed: str) -> Callable[[str], str]:
    allowed_values = tuple(value.strip() for value in allowed)

    def parse(raw_value: str) -> str:
        normalized = raw_value.strip().lower()
        if normalized not in allowed_values:
            raise ValueError(f"expected one of: {', '.join(allowed_values)}")
        return normalized

    return parse


def _format_literal(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


_CONFIG_KEY_SPECS: dict[str, ConfigKeySpec] = {
    "catchup.enabled": ConfigKeySpec(
        path="catchup.enabled",
        default_value=True,
        parser=_parse_bool,
        description="Enable or disable the session-start catchup hook for the program.",
    ),
    "catchup.catchup_interval_minutes": ConfigKeySpec(
        path="catchup.catchup_interval_minutes",
        default_value=30,
        parser=_parse_int(minimum=1),
        description="Minimum minutes between automatic catchup sweeps.",
    ),
    "catchup.workiq_timeout_seconds": ConfigKeySpec(
        path="catchup.workiq_timeout_seconds",
        default_value=30,
        parser=_parse_int(minimum=1),
        description="Per-call WorkIQ timeout budget in seconds.",
    ),
    "catchup.workiq_total_budget_seconds": ConfigKeySpec(
        path="catchup.workiq_total_budget_seconds",
        default_value=90,
        parser=_parse_int(minimum=1),
        description="Total WorkIQ wall-clock budget per catchup session.",
    ),
    "salience.min_weight": ConfigKeySpec(
        path="salience.min_weight",
        default_value=0.2,
        parser=_parse_float(minimum=0.0, maximum=1.0),
        description="Hard floor for per-workstream author salience weights.",
    ),
    "salience.ema_alpha": ConfigKeySpec(
        path="salience.ema_alpha",
        default_value=0.1,
        parser=_parse_float(minimum=0.0, maximum=1.0),
        description="EMA decay factor for salience updates.",
    ),
    "salience.confirmation_weight": ConfigKeySpec(
        path="salience.confirmation_weight",
        default_value=2.0,
        parser=_parse_float(minimum=0.0),
        description="Multiplier applied when a previously dismissed anomaly is later confirmed.",
    ),
    "ai.semantic_index": ConfigKeySpec(
        path="ai.semantic_index",
        default_value=False,
        parser=_parse_bool,
        description="Enable the local semantic index surfaces for the program.",
    ),
    "ai.requests_per_minute": ConfigKeySpec(
        path="ai.requests_per_minute",
        default_value=10,
        parser=_parse_int(minimum=1),
        description="AOAI requests-per-minute cap for governed AI surfaces.",
    ),
    "ai.claim_extractor.mode": ConfigKeySpec(
        path="ai.claim_extractor.mode",
        default_value="calibration",
        parser=_parse_enum("calibration", "production"),
        description="Claim extractor operating mode.",
        governance_critical=True,
    ),
    "ai.claim_extractor.calibration_min_confirms": ConfigKeySpec(
        path="ai.claim_extractor.calibration_min_confirms",
        default_value=20,
        parser=_parse_int(minimum=1),
        description="Minimum confirm count before production-mode promotion can be considered.",
    ),
    "readiness.gate": ConfigKeySpec(
        path="readiness.gate",
        default_value=False,
        parser=_parse_bool,
        description="Enable readiness quality gates during confirm.",
    ),
    "readiness.snapshot_max_age_days": ConfigKeySpec(
        path="readiness.snapshot_max_age_days",
        default_value=7,
        parser=_parse_int(minimum=1),
        description="Maximum accepted age for readiness snapshots before warning.",
    ),
    "scorecard.include_dependency_risk": ConfigKeySpec(
        path="scorecard.include_dependency_risk",
        default_value=False,
        parser=_parse_bool,
        description="Include dependency-derived risk in scorecard computations when supported.",
    ),
    "audit.retention_days": ConfigKeySpec(
        path="audit.retention_days",
        default_value=365,
        parser=_parse_int(minimum=1),
        description="Retention window for active autonomy audit rows before archive.",
    ),
    "audit.archive_threshold_rows": ConfigKeySpec(
        path="audit.archive_threshold_rows",
        default_value=50000,
        parser=_parse_int(minimum=1),
        description="Advisory threshold for active audit row count.",
    ),
    "gather.backend": ConfigKeySpec(
        path="gather.backend",
        default_value="sync",
        parser=_parse_enum("sync", "async"),
        description="Gather execution backend.",
    ),
}
