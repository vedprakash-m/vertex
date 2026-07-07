from __future__ import annotations

from pathlib import Path
from typing import Any

from src.commands.doctor_checks.models import DoctorCheck, SchemaVersionAssessment
from src.commands.doctor_checks.yaml_support import _SCHEMA_VERSION_RE, load_yaml_document
from src.core.edition_resolver import find_edition_yaml
from src.core.exceptions import ConfigError


def run_config_governance_check(
    *,
    edition_name: str,
    resolved: Any,
    editions_root: Path,
    programs_root: Path,
) -> DoctorCheck:
    edition_path = editions_root / f"{edition_name}.yaml"
    # Fallback to the programs tree when the legacy flat editions_root is empty
    # (editions now live under programs/<id>/editions/).
    if not edition_path.exists():
        candidate = find_edition_yaml(edition_name, programs_root=programs_root)
        if candidate.exists():
            edition_path = candidate
    program_path = programs_root / resolved.paths.program_id / "program.yaml"
    readiness_path = programs_root / resolved.paths.program_id / "readiness.yaml"
    template_contract_path = programs_root / resolved.paths.program_id / "template_contract.yaml"
    slice_contracts_path = programs_root / resolved.paths.program_id / "slice_contracts.yaml"
    chapter_contract_path = programs_root / resolved.paths.program_id / "chapter_contract.yaml"

    assessments = {
        "edition": assess_schema_version(edition_path, expected_major=2, expected_minor=0, required=True),
        "program": assess_schema_version(program_path, expected_major=3, expected_minor=0, required=True),
        "readiness": assess_schema_version(
            readiness_path,
            expected_major=1,
            expected_minor=0,
            required=False,
            missing_detail="readiness.yaml absent (optional until readiness governance is enabled).",
        ),
        "template_contract": assess_schema_version(
            template_contract_path,
            expected_major=1,
            expected_minor=0,
            required=False,
            missing_detail="template_contract.yaml absent.",
        ),
        "slice_contracts": assess_schema_version(
            slice_contracts_path,
            expected_major=1,
            expected_minor=0,
            required=False,
            missing_detail="slice_contracts.yaml absent.",
        ),
        "chapter_contract": assess_schema_version(
            chapter_contract_path,
            expected_major=1,
            expected_minor=0,
            required=False,
            missing_detail="chapter_contract.yaml absent.",
        ),
    }

    if any(assessment.status == "fail" for assessment in assessments.values()):
        status = "fail"
    elif any(assessment.status == "warn" for assessment in assessments.values()):
        status = "warn"
    else:
        status = "ok"

    problems = [
        f"{name}: {assessment.detail}"
        for name, assessment in assessments.items()
        if assessment.status != "ok"
    ]
    if problems:
        detail = "; ".join(problems[:3])
        if len(problems) > 3:
            detail = f"{detail}; +{len(problems) - 3} more"
    else:
        detail = (
            "schema versions valid: "
            + ", ".join(
                f"{name}={assessment.version}"
                for name, assessment in assessments.items()
                if assessment.version is not None
            )
        )

    return DoctorCheck(
        "Config Governance",
        status,
        detail,
        metadata={
            "edition_name": edition_name,
            "program_id": resolved.paths.program_id,
            "assessments": {
                name: {
                    "status": assessment.status,
                    "detail": assessment.detail,
                    "version": assessment.version,
                }
                for name, assessment in assessments.items()
            },
        },
    )


def assess_schema_version(
    path: Path,
    *,
    expected_major: int,
    expected_minor: int,
    required: bool,
    missing_detail: str | None = None,
) -> SchemaVersionAssessment:
    if not path.exists():
        if required:
            return SchemaVersionAssessment("fail", f"{path} is missing.", None)
        return SchemaVersionAssessment("warn", missing_detail or f"{path} is missing.", None)

    try:
        document = load_yaml_document(path)
    except ConfigError as error:
        return SchemaVersionAssessment("fail", str(error), None)

    raw_version = document.get("schema_version")
    if not isinstance(raw_version, str) or not raw_version.strip():
        return SchemaVersionAssessment("fail", f"{path} is missing schema_version.", None)

    version = raw_version.strip()
    match = _SCHEMA_VERSION_RE.match(version)
    if match is None:
        return SchemaVersionAssessment("fail", f"{path} has invalid schema_version {version!r}.", version)

    major = int(match.group(1))
    minor = int(match.group(2))
    expected = f"{expected_major}.{expected_minor}"
    if major != expected_major:
        return SchemaVersionAssessment(
            "fail",
            f"{path} declares schema_version {version}; expected major version {expected_major}.x (baseline {expected}).",
            version,
        )
    if minor != expected_minor:
        return SchemaVersionAssessment(
            "warn",
            f"{path} declares schema_version {version}; expected baseline {expected}. Verify compatibility or add a migration note.",
            version,
        )
    return SchemaVersionAssessment("ok", f"{path} schema_version {version} matches expected baseline {expected}.", version)
