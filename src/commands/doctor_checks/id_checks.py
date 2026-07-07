from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable

import yaml

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.core.chapter_contract_loader import canonical_dimension_binding_id
from src.core.config_loader import load_bundle
from src.core.edition_resolver import resolve_edition
from src.core.exceptions import ConfigError
from src.core.program_fact_store import load_current_workstreams
from src.core.workstream_registry import load_workstream_registry


def load_dependency_workstream_ids(
    program_id: str,
    *,
    programs_root: Path,
    load_current_workstreams_fn: Callable[[str], tuple[Any, ...]] | None = None,
) -> tuple[str, ...]:
    workstreams_path = programs_root / program_id / "workstreams.yaml"
    if not workstreams_path.exists():
        raise ConfigError(f"Missing workstreams.yaml for program '{program_id}'.")
    current_workstreams = load_current_workstreams_fn or (
        lambda current_program_id: load_current_workstreams(current_program_id, programs_root=programs_root)
    )
    return tuple(sorted({workstream.id for workstream in current_workstreams(program_id)}))


def load_scorecard_dimension_bindings(program_id: str, *, programs_root: Path) -> tuple[tuple[str, str, str], ...]:
    scorecards_path = programs_root / program_id / "scorecards.yaml"
    if not scorecards_path.exists():
        raise ConfigError(f"Missing scorecards.yaml for program '{program_id}'.")
    try:
        document = yaml.safe_load(scorecards_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {scorecards_path}: {error}") from error
    raw_scorecards = document.get("scorecards") or []
    if not isinstance(raw_scorecards, list):
        raise ConfigError(f"Expected 'scorecards' list in {scorecards_path}.")

    bindings: list[tuple[str, str, str]] = []
    for scorecard_index, raw_scorecard in enumerate(raw_scorecards, start=1):
        if not isinstance(raw_scorecard, dict):
            raise ConfigError(f"Scorecard entry #{scorecard_index} in {scorecards_path} must be a mapping.")
        scorecard_name = str(raw_scorecard.get("name") or "").strip()
        if not scorecard_name:
            raise ConfigError(f"Scorecard entry #{scorecard_index} in {scorecards_path} is missing name.")
        raw_dimensions = raw_scorecard.get("dimensions") or []
        if not isinstance(raw_dimensions, list):
            raise ConfigError(f"scorecard '{scorecard_name}' dimensions must be a list in {scorecards_path}.")

        for dimension_index, raw_dimension in enumerate(raw_dimensions, start=1):
            if not isinstance(raw_dimension, dict):
                raise ConfigError(
                    f"Dimension entry #{dimension_index} for scorecard '{scorecard_name}' in {scorecards_path} must be a mapping."
                )
            dimension_name = str(raw_dimension.get("name") or "").strip()
            if not dimension_name:
                raise ConfigError(
                    f"Dimension entry #{dimension_index} for scorecard '{scorecard_name}' in {scorecards_path} is missing name."
                )
            workstream_id = str(raw_dimension.get("workstream_id") or "").strip()
            if not workstream_id:
                raise ConfigError(
                    f"Dimension '{dimension_name}' for scorecard '{scorecard_name}' in {scorecards_path} is missing workstream_id."
                )
            bindings.append((scorecard_name, dimension_name, workstream_id))
    return tuple(bindings)


def run_id_doctor(
    *,
    edition_name: str,
    reports_root: Path,
    editions_root: Path,
    programs_root: Path,
    load_dependency_workstream_ids_fn: Callable[..., tuple[str, ...]],
    load_scorecard_dimension_bindings_fn: Callable[..., tuple[tuple[str, str, str], ...]],
) -> DoctorReport:
    resolved = resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)
    if resolved is None:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("IDs", "fail", f"Edition '{edition_name}' could not be resolved."),),
        )

    program_id = resolved.paths.program_id
    try:
        bundle = load_bundle(
            edition_name,
            reports_root=reports_root,
            editions_root=editions_root,
            programs_root=programs_root,
        )
        if not bundle.slice_contracts:
            return DoctorReport(
                edition=edition_name,
                checks=(DoctorCheck("IDs", "fail", f"programs/{program_id}/slice_contracts.yaml is missing or empty."),),
            )
        workstream_ids = set(load_dependency_workstream_ids_fn(program_id, programs_root=programs_root))
        scorecard_dimensions = load_scorecard_dimension_bindings_fn(program_id, programs_root=programs_root)
        registry_entries = load_workstream_registry(
            program_id=program_id,
            slice_contracts=bundle.slice_contracts,
            programs_root=programs_root,
            program_context=bundle.program_context,
        )
    except ConfigError as error:
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("IDs", "fail", str(error)),),
        )

    problems: list[str] = []
    canonical_scorecard_ids: list[str] = []
    canonical_sources: dict[str, str] = {}
    for scorecard_name, dimension_name, workstream_id in scorecard_dimensions:
        if workstream_id not in workstream_ids:
            problems.append(
                f"Scorecard dimension '{scorecard_name} / {dimension_name}' references unknown workstream_id '{workstream_id}'."
            )
        canonical_id = canonical_dimension_binding_id(
            scorecard_name,
            dimension_name,
            chapter_namespace=bundle.chapter_namespace,
        )
        previous_source = canonical_sources.get(canonical_id)
        current_source = f"{scorecard_name} / {dimension_name}"
        if previous_source is not None and previous_source != current_source:
            problems.append(
                f"Scorecard dimensions '{previous_source}' and '{current_source}' collide on canonical id '{canonical_id}'."
            )
        else:
            canonical_sources[canonical_id] = current_source
        canonical_scorecard_ids.append(canonical_id)

    slice_ids = {contract.id for contract in bundle.slice_contracts}
    missing_slice_ids = sorted(set(canonical_scorecard_ids) - slice_ids)
    extra_slice_ids = sorted(slice_ids - set(canonical_scorecard_ids))
    if missing_slice_ids:
        problems.append(
            f"Canonical scorecard ids missing from slice_contracts.yaml: {', '.join(missing_slice_ids[:3])}"
            + (f", +{len(missing_slice_ids) - 3} more" if len(missing_slice_ids) > 3 else "")
            + "."
        )
    if extra_slice_ids:
        problems.append(
            f"slice_contracts.yaml defines ids with no scorecard dimension source: {', '.join(extra_slice_ids[:3])}"
            + (f", +{len(extra_slice_ids) - 3} more" if len(extra_slice_ids) > 3 else "")
            + "."
        )

    requires_chapter_contract = bundle.config.edition.type != "narrative"
    if bundle.chapter_contract is None:
        if requires_chapter_contract:
            problems.append(f"programs/{program_id}/chapter_contract.yaml is missing.")
    else:
        chapter_dimension_ids = {
            dimension_id
            for chapter in bundle.chapter_contract.chapters
            for dimension_id in chapter.dimensions
        }
        unknown_chapter_dimension_ids = sorted(chapter_dimension_ids - slice_ids)
        if unknown_chapter_dimension_ids:
            problems.append(
                f"chapter_contract.yaml references unknown canonical dimension ids: {', '.join(unknown_chapter_dimension_ids[:3])}"
                + (f", +{len(unknown_chapter_dimension_ids) - 3} more" if len(unknown_chapter_dimension_ids) > 3 else "")
                + "."
            )

    unknown_registry_source_ids = sorted(
        {
            source_slice_id
            for entry in registry_entries
            for source_slice_id in entry.source_slice_ids
            if source_slice_id not in slice_ids
        }
    )
    if unknown_registry_source_ids:
        problems.append(
            f"workstream_registry.yaml references unknown source_slice_ids: {', '.join(unknown_registry_source_ids[:3])}"
            + (f", +{len(unknown_registry_source_ids) - 3} more" if len(unknown_registry_source_ids) > 3 else "")
            + "."
        )

    if problems:
        detail = "; ".join(problems[:2])
        if len(problems) > 2:
            detail = f"{detail}; +{len(problems) - 2} more"
        return DoctorReport(edition=edition_name, checks=(DoctorCheck("IDs", "fail", detail),))

    composition_check = build_cross_edition_composition_check(
        program_id=program_id,
        edition_name=edition_name,
        reports_root=reports_root,
        editions_root=editions_root,
        programs_root=programs_root,
        fallback_dimension_ids=tuple(sorted(set(canonical_scorecard_ids))),
    )
    anchor_checks = build_slice_anchor_checks(bundle.slice_contracts, as_of=date.today())
    return DoctorReport(
        edition=edition_name,
        checks=(
            DoctorCheck(
                "IDs",
                "ok",
                f"programs/{program_id} scorecards, chapter contract, slice contracts, registry, and workstreams align ({len(slice_ids)} canonical dimensions, {len(registry_entries)} registry lanes, {len(workstream_ids)} workstreams).",
            ),
            *anchor_checks,
            composition_check,
        ),
    )


def build_cross_edition_composition_check(
    *,
    program_id: str,
    edition_name: str,
    reports_root: Path,
    editions_root: Path,
    programs_root: Path,
    fallback_dimension_ids: tuple[str, ...],
) -> DoctorCheck:
    edition_ids = load_program_edition_ids(program_id, editions_root=editions_root)
    if len(edition_ids) <= 1:
        return DoctorCheck(
            "Composition",
            "ok",
            f"Only edition '{edition_name}' is staged for program '{program_id}'; no cross-edition scorecard comparison available.",
        )

    comparisons: dict[str, tuple[str, tuple[str, ...]]] = {}
    load_failures: list[str] = []
    for candidate_edition_name in edition_ids:
        try:
            candidate_bundle = load_bundle(
                candidate_edition_name,
                reports_root=reports_root,
                editions_root=editions_root,
                programs_root=programs_root,
            )
        except ConfigError as error:
            load_failures.append(f"{candidate_edition_name}: {error}")
            continue
        comparisons[candidate_edition_name] = (
            candidate_bundle.config.edition.type,
            effective_edition_dimension_ids(candidate_bundle, fallback_dimension_ids=fallback_dimension_ids),
        )

    if edition_name not in comparisons:
        detail = "; ".join(load_failures[:2]) if load_failures else f"Could not load edition '{edition_name}' for composition comparison."
        if len(load_failures) > 2:
            detail = f"{detail}; +{len(load_failures) - 2} more"
        return DoctorCheck("Composition", "warn", detail)

    if load_failures:
        detail = "; ".join(load_failures[:2])
        if len(load_failures) > 2:
            detail = f"{detail}; +{len(load_failures) - 2} more"
        return DoctorCheck("Composition", "warn", f"Skipped some same-program editions during comparison: {detail}")

    reference_type, reference_ids = comparisons[edition_name]
    reference_set = set(reference_ids)
    mismatches: list[str] = []
    for candidate_edition_name, (candidate_type, candidate_ids) in comparisons.items():
        if candidate_edition_name == edition_name:
            continue
        candidate_set = set(candidate_ids)
        missing = sorted(reference_set - candidate_set)
        extra = sorted(candidate_set - reference_set)
        if not missing and not extra:
            continue
        fragments: list[str] = []
        if missing:
            fragments.append(
                f"missing {len(missing)} canonical dimension{'s' if len(missing) != 1 else ''}: {', '.join(missing[:3])}"
                + (f", +{len(missing) - 3} more" if len(missing) > 3 else "")
            )
        if extra:
            fragments.append(
                f"adds {len(extra)} canonical dimension{'s' if len(extra) != 1 else ''}: {', '.join(extra[:3])}"
                + (f", +{len(extra) - 3} more" if len(extra) > 3 else "")
            )
        mismatches.append(
            f"{candidate_edition_name} ({candidate_type}) vs {edition_name} ({reference_type}): {'; '.join(fragments)}"
        )

    if mismatches:
        detail = "; ".join(mismatches[:2])
        if len(mismatches) > 2:
            detail = f"{detail}; +{len(mismatches) - 2} more"
        return DoctorCheck("Composition", "warn", detail)

    ordered_editions = ", ".join(
        f"{candidate_edition_name} ({candidate_type})"
        for candidate_edition_name, (candidate_type, _) in comparisons.items()
    )
    return DoctorCheck(
        "Composition",
        "ok",
        f"Cross-edition scorecard composition aligns across {ordered_editions} ({len(reference_ids)} canonical dimensions).",
    )


def build_slice_anchor_checks(slice_contracts: Any, *, as_of: date) -> tuple[DoctorCheck, ...]:
    checks: list[DoctorCheck] = []
    for contract in slice_contracts:
        ado_contract = contract.source_contract.ado
        if ado_contract is None:
            checks.append(
                DoctorCheck(
                    f"Anchor {contract.id}",
                    "warn",
                    f"{contract.id}: raw anchor gap; source_contract.ado is missing, so add saved_queries or explicit_work_item_ids.",
                )
            )
            continue

        is_filter_only = (
            ado_contract.filters is not None
            and not ado_contract.filters.is_empty()
            and not ado_contract.saved_queries
            and not ado_contract.explicit_work_item_ids
        )
        if is_filter_only and ado_contract.intentional_filter_only:
            expires_on = ado_contract.intentional_filter_only_expires_on
            if expires_on is None:
                checks.append(
                    DoctorCheck(
                        f"Anchor {contract.id}",
                        "warn",
                        f"{contract.id}: filter-only waiver is missing intentional_filter_only_expires_on.",
                    )
                )
                continue
            if expires_on < as_of:
                checks.append(
                    DoctorCheck(
                        f"Anchor {contract.id}",
                        "warn",
                        f"{contract.id}: filter-only waiver expired on {expires_on.isoformat()}; add saved_queries or explicit_work_item_ids, or renew the waiver.",
                    )
                )
                continue
            continue

        if ado_contract.saved_queries or ado_contract.explicit_work_item_ids:
            continue

        if is_filter_only:
            checks.append(
                DoctorCheck(
                    f"Anchor {contract.id}",
                    "warn",
                    f"{contract.id}: raw anchor gap; contract is filter-only, so add saved_queries or explicit_work_item_ids, or mark it intentional with an expiry date.",
                )
            )
            continue

        checks.append(
            DoctorCheck(
                f"Anchor {contract.id}",
                "warn",
                f"{contract.id}: raw anchor gap; no saved_queries or explicit_work_item_ids are configured.",
            )
        )
    return tuple(checks)


def load_program_edition_ids(program_id: str, *, editions_root: Path) -> tuple[str, ...]:
    from src.core.edition_resolver import PROGRAMS_ROOT, _program_dir_for_reference

    edition_ids: list[str] = []
    edition_paths = sorted(editions_root.glob("*.yaml")) if editions_root.exists() else []
    # Fallback to the program's canonical editions directory under the programs tree
    # when the legacy flat editions_root is empty (editions now live under
    # programs/<id>/editions/).
    if not edition_paths:
        candidate_dir = _program_dir_for_reference(program_id, programs_root=PROGRAMS_ROOT) / "editions"
        edition_paths = sorted(candidate_dir.glob("*.yaml")) if candidate_dir.exists() else []
    for path in edition_paths:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(document, dict):
            continue
        candidate_program_id = document.get("program_id")
        candidate_edition_id = document.get("id")
        if candidate_program_id != program_id:
            continue
        if not isinstance(candidate_edition_id, str) or not candidate_edition_id.strip():
            continue
        edition_ids.append(candidate_edition_id.strip())
    return tuple(edition_ids)


def effective_edition_dimension_ids(bundle: Any, *, fallback_dimension_ids: tuple[str, ...]) -> tuple[str, ...]:
    chapter_contract = bundle.chapter_contract
    edition_type = bundle.config.edition.type
    if chapter_contract is None or edition_type not in {"detailed", "focused", "lookback"}:
        return fallback_dimension_ids

    chapter_dimension_ids = {
        dimension_id
        for chapter in chapter_contract.chapters_for(edition_type)
        for dimension_id in chapter.dimensions
    }
    if not chapter_dimension_ids:
        return fallback_dimension_ids

    return tuple(sorted(chapter_dimension_ids | set(chapter_contract.unmapped_dimensions)))
