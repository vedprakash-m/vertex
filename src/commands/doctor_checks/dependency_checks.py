from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
import yaml

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.dependency_graph import build_dependency_dag
from src.core.edition_resolver import resolve_edition
from src.core.exceptions import ConfigError
from src.core.milestone_engine import get_milestones_path
from src.core.program_fact_store import load_current_milestones, load_program_facts, project_dependencies
from src.core.signal_review import signal_is_approved_for_evidence
from src.core.store_factory import build_signal_store_for_program_id, read_signal_review_log_for_program_id


def load_recent_dependency_signal_findings(
    program_id: str,
    *,
    programs_root: Path,
) -> tuple[dict[str, str], ...]:
    store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    review_states = {
        decision.signal_id: decision
        for decision in read_signal_review_log_for_program_id(program_id, programs_root=programs_root)
    }
    threshold = datetime.now(timezone.utc) - timedelta(days=14)
    findings: list[dict[str, str]] = []
    for signal in store.read(program_id):
        if signal.source != "ado/dependency" or signal.timestamp < threshold:
            continue
        metadata = signal.metadata or {}
        if not signal_is_approved_for_evidence(signal, review_states):
            continue
        label = str(metadata.get("dependency_label") or signal.workstream_id or "dependency")
        resolution_path = str(metadata.get("resolution_path") or "").strip().lower()
        prefix = "Dependency stale"
        classification = "unclassified"
        if resolution_path == "intra_storage":
            prefix = "Internal dependency stale"
            classification = "internal"
        elif resolution_path.startswith("cross_org"):
            prefix = "⚠ Cross-org dependency stale — escalation may be needed"
            classification = "cross_org"
        elif resolution_path == "external":
            prefix = "⚠ External dependency stale — escalation may be needed"
            classification = "external"
        findings.append(
            {
                "classification": classification,
                "dependency_label": label,
                "display": f"{prefix}: {label}: {signal.text}",
                "resolution_path": resolution_path,
            }
        )
    unique_findings: list[dict[str, str]] = []
    seen_displays: set[str] = set()
    for finding in findings:
        display = finding["display"]
        if display in seen_displays:
            continue
        seen_displays.add(display)
        unique_findings.append(finding)
    return tuple(unique_findings)


def count_legacy_dependencies(program_id: str, *, programs_root: Path) -> int:
    program_path = programs_root / program_id / "program.yaml"
    if not program_path.exists():
        return 0
    try:
        document = yaml.safe_load(program_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {program_path}: {error}") from error
    raw_dependencies = document.get("key_dependencies") or ()
    if not isinstance(raw_dependencies, list):
        return 0
    return sum(1 for entry in raw_dependencies if isinstance(entry, dict))


def load_dependency_milestone_ids(program_id: str, *, programs_root: Path) -> tuple[str, ...] | None:
    milestone_path = get_milestones_path(program_id, programs_root=programs_root)
    if not milestone_path.exists():
        return None
    return tuple(milestone.id for milestone in load_current_milestones(program_id, programs_root=programs_root))


def validate_dependency_references(
    dependencies: Any,
    *,
    programs_root: Path,
    load_dependency_workstream_ids_fn: Callable[[str], tuple[str, ...]],
    load_dependency_milestone_ids_fn: Callable[[str], tuple[str, ...] | None],
) -> list[str]:
    workstream_cache: dict[str, tuple[str, ...]] = {}
    milestone_cache: dict[str, tuple[str, ...] | None] = {}
    problems: list[str] = []

    for dependency in dependencies:
        for side in ("from", "to"):
            side_program_id = getattr(dependency, f"{side}_program_id")
            program_dir = programs_root / side_program_id
            if not program_dir.exists():
                problems.append(
                    f"Unknown {side}_program_id '{side_program_id}' referenced by dependency '{dependency.id}'."
                )
                continue

            workstream_id = getattr(dependency, f"{side}_workstream_id")
            if workstream_id is not None:
                workstream_ids = workstream_cache.setdefault(
                    side_program_id,
                    load_dependency_workstream_ids_fn(side_program_id),
                )
                if workstream_id not in workstream_ids:
                    problems.append(
                        f"Unknown {side}_workstream_id '{workstream_id}' referenced by dependency '{dependency.id}'."
                    )

            milestone_id = getattr(dependency, f"{side}_milestone_id")
            if milestone_id is not None:
                milestone_ids = milestone_cache.setdefault(
                    side_program_id,
                    load_dependency_milestone_ids_fn(side_program_id),
                )
                if milestone_ids is None:
                    problems.append(
                        f"programs/{side_program_id}/milestones.yaml is missing but dependency '{dependency.id}' references milestone '{milestone_id}'."
                    )
                elif milestone_id not in milestone_ids:
                    problems.append(
                        f"Unknown {side}_milestone_id '{milestone_id}' referenced by dependency '{dependency.id}'."
                    )

    return problems


def run_dependency_doctor(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    count_legacy_dependencies_fn: Callable[[str], int],
    validate_dependency_references_fn: Callable[[Any], list[str]],
    get_dependencies_path_fn: Callable[[str], Path],
) -> DoctorReport:
    resolved = resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)
    if resolved is None:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Dependencies", "fail", f"Edition '{edition_name}' could not be resolved."),),
        )

    program_id = resolved.paths.program_id
    dependencies_path = get_dependencies_path_fn(program_id)
    legacy_count = count_legacy_dependencies_fn(program_id)

    if not dependencies_path.exists():
        detail = (
            f"programs/{program_id}/dependencies.yaml is absent; using {legacy_count} legacy key_dependenc{'y' if legacy_count == 1 else 'ies'} from program.yaml."
            if legacy_count
            else f"programs/{program_id}/dependencies.yaml is absent; enhanced dependency model is not configured."
        )
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Dependencies", "warn", detail),),
        )

    try:
        dependencies = project_dependencies(load_program_facts(program_id, programs_root=programs_root, fact_types=("dependency.link",)))
        build_dependency_dag(dependencies)
        problems = validate_dependency_references_fn(dependencies)
    except ConfigError as error:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Dependencies", "fail", str(error)),),
        )

    if problems:
        detail = "; ".join(problems[:2])
        if len(problems) > 2:
            detail = f"{detail}; +{len(problems) - 2} more"
        check = DoctorCheck("Dependencies", "fail", detail)
    else:
        recent_dependency_findings = load_recent_dependency_signal_findings(
            program_id,
            programs_root=programs_root,
        )
        resolution_path_count = sum(1 for dependency in dependencies if dependency.resolution_path)
        missing_resolution_path_count = len(dependencies) - resolution_path_count
        cross_org_count = sum(
            1
            for dependency in dependencies
            if dependency.from_program_id != dependency.to_program_id
            or (dependency.resolution_path or "").startswith("cross_org")
        )
        metadata = {
            "program_id": program_id,
            "dependency_count": len(dependencies),
            "resolution_path_count": resolution_path_count,
            "missing_resolution_path_count": missing_resolution_path_count,
            "cross_org_count": cross_org_count,
        }
        detail_suffix = (
            f" resolution_path {resolution_path_count}/{len(dependencies)}; cross-org classified={cross_org_count}."
            if dependencies
            else ""
        )
        if missing_resolution_path_count:
            check = DoctorCheck(
                "Dependencies",
                "warn",
                f"programs/{program_id}/dependencies.yaml loaded ({len(dependencies)} dependencies); schema, references, and DAG valid, but {missing_resolution_path_count} entr{'y is' if missing_resolution_path_count == 1 else 'ies are'} still missing resolution_path.{detail_suffix}",
                metadata=metadata,
            )
        elif recent_dependency_findings:
            preview = "; ".join(finding["display"] for finding in recent_dependency_findings[:2])
            if len(recent_dependency_findings) > 2:
                preview = f"{preview}; +{len(recent_dependency_findings) - 2} more"
            metadata["recent_dependency_findings"] = list(recent_dependency_findings)
            metadata["recent_internal_dependency_finding_count"] = sum(
                1 for finding in recent_dependency_findings if finding["classification"] == "internal"
            )
            metadata["recent_cross_org_dependency_finding_count"] = sum(
                1 for finding in recent_dependency_findings if finding["classification"] == "cross_org"
            )
            metadata["recent_external_dependency_finding_count"] = sum(
                1 for finding in recent_dependency_findings if finding["classification"] == "external"
            )
            check = DoctorCheck(
                "Dependencies",
                "warn",
                f"programs/{program_id}/dependencies.yaml loaded ({len(dependencies)} dependencies); schema, references, DAG, and resolution_path classification valid.{detail_suffix} Recent dependency findings: {preview}",
                metadata=metadata,
            )
        elif legacy_count:
            label = "entry" if legacy_count == 1 else "entries"
            metadata["legacy_dependency_count"] = legacy_count
            check = DoctorCheck(
                "Dependencies",
                "warn",
                f"programs/{program_id}/dependencies.yaml loaded ({len(dependencies)} dependencies); schema, references, DAG, and resolution_path classification valid. Legacy key_dependencies still contain {legacy_count} {label}.{detail_suffix}",
                metadata=metadata,
            )
        else:
            check = DoctorCheck(
                "Dependencies",
                "ok",
                f"programs/{program_id}/dependencies.yaml loaded ({len(dependencies)} dependencies); schema, references, DAG, and resolution_path classification valid.{detail_suffix}",
                metadata=metadata,
            )

    return DoctorReport(edition=edition_name, checks=(check,))
