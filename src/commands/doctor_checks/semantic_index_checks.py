from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.commands.doctor_checks.models import format_bytes
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.edition_resolver import resolve_edition
from src.core.semantic_index import get_semantic_index_path, load_semantic_index_state


def run_semantic_index_doctor(
    *,
    edition_name: str,
    editions_root: Path,
    programs_root: Path,
    archive_root: Path,
    semantic_index_enabled_fn: Callable[[dict[str, Any] | None], bool],
    build_semantic_index_checks_fn: Callable[..., tuple[DoctorCheck, ...]],
) -> DoctorReport:
    resolved = resolve_edition(
        edition_name,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    if resolved is None:
        return DoctorReport(edition=edition_name, checks=(DoctorCheck("Semantic Index", "fail", f"Edition '{edition_name}' could not be resolved."),))
    if not semantic_index_enabled_fn(resolved.raw_program):
        return DoctorReport(
            edition=edition_name,
            checks=(DoctorCheck("Semantic Index", "ok", "Program AI semantic index is disabled."),),
        )
    return DoctorReport(
        edition=edition_name,
        checks=build_semantic_index_checks_fn(edition_name=edition_name, archive_root=archive_root),
    )


def build_semantic_index_checks(*, edition_name: str, archive_root: Path) -> tuple[DoctorCheck, ...]:
    archive_index = read_archive_index(edition_name, archive_root=archive_root)
    latest_confirmed = find_latest_confirmed_entry(archive_index)
    state = load_semantic_index_state(edition_name, archive_root=archive_root)
    index_path = get_semantic_index_path(edition_name, archive_root=archive_root)

    if latest_confirmed is None:
        return (
            DoctorCheck("Semantic Freshness", "ok", "No confirmed issues yet; semantic index not required."),
            DoctorCheck("Semantic Dirty", "ok", "No semantic index parity drift recorded."),
            DoctorCheck("Semantic Optimize", "ok", "Semantic index has not been built yet."),
        )

    freshness_status = "ok"
    freshness_detail = f"Latest confirmed issue {latest_confirmed.issue_number:03d} is within semantic index freshness bounds."
    if state is None or not index_path.exists() or state.last_built_at is None:
        freshness_status = "warn"
        freshness_detail = f"Semantic index missing for latest confirmed issue {latest_confirmed.issue_number:03d}; run `vertex index rebuild --edition {edition_name}`."
    elif latest_confirmed.generated_at - state.last_built_at > timedelta(days=7):
        freshness_status = "warn"
        freshness_detail = (
            f"Semantic index last built {state.last_built_at.date().isoformat()} but latest confirmed issue {latest_confirmed.issue_number:03d} "
            f"was generated on {latest_confirmed.generated_at.date().isoformat()}."
        )

    dirty_status = "ok"
    dirty_detail = "No semantic index parity drift recorded."
    if state is None and not index_path.exists():
        dirty_status = "warn"
        dirty_detail = "Semantic index state is missing; run `vertex index rebuild` after archive changes."
    elif state is not None and state.semantic_index_dirty:
        dirty_status = "warn"
        dirty_detail = f"semantic_index_dirty=true ({state.dirty_reason or 'no reason recorded'})."

    optimize_status = "ok"
    if not index_path.exists():
        optimize_detail = "Semantic index has not been built yet."
    else:
        size_bytes = index_path.stat().st_size
        optimize_detail = f"Semantic index size: {format_bytes(size_bytes)}."
        if size_bytes > 50 * 1024 * 1024 and (state is None or state.last_optimized_document_count < state.indexed_document_count):
            optimize_status = "warn"
            optimize_detail = f"Semantic index size {format_bytes(size_bytes)} exceeds 50 MB without a recent optimize."

    return (
        DoctorCheck("Semantic Freshness", freshness_status, freshness_detail),
        DoctorCheck("Semantic Dirty", dirty_status, dirty_detail),
        DoctorCheck("Semantic Optimize", optimize_status, optimize_detail),
    )


def semantic_index_enabled(raw_program: dict[str, Any] | None) -> bool:
    if not isinstance(raw_program, dict):
        return False
    ai_config = raw_program.get("ai")
    if not isinstance(ai_config, dict):
        return False
    return bool(ai_config.get("semantic_index"))
