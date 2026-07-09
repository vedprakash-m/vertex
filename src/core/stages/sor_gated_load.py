"""Shared SoR-gated fact-family load helper (fix-data-flow.md Track B.5 / §6.2b).

Extracted after Track B proved the pattern for a second family (risk,
mirroring milestone's original S-8a shape) — the "copy the SoR-gated
overlay pattern by hand for every family" approach that produced this
extraction's own motivating risk (the fact-join prefix bug from Track F was
exactly this class of copy-paste drift).

Every fact family this project migrates onto ``ProgramReality`` follows the
same shape:

1. Resolve the family's SoR mode (``resolve_family_sor_mode``).
2. When legacy: call the family's direct ``load_program_facts`` loader,
   gracefully degrading on ``ConfigError``.
3. When non-legacy: read via an already-loaded (or lazily-loaded)
   ``ProgramReality`` accessor, with:
   - an empty-set cross-check (warn, don't silently render "nothing to
     report", when reality returns empty but legacy would have been
     non-empty — this is a REAL divergent-behavior signal, not a
     stylistic nicety);
   - a render-safe graceful rollback to the legacy loader on any
     unexpected (non-``ConfigError``) exception, gated behind a
     per-family "audited legacy rollback" environment variable, so a
     program can be reverted to the legacy path without a code change if
     the new path misbehaves;
   - a lineage map (``source_document_key``/``approval_event_id`` per
     record id) built from each ``FactAssessment.lineage``.

See ``docs/contributing/migrate-fact-family.md`` for the full protocol this
helper is the mandatory implementation of.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Iterable

from src.core.exceptions import ConfigError
from src.core.fact_sor_state import resolve_family_sor_mode


def sor_gated_family_load(
    *,
    program_id: str,
    family: str,
    programs_root: Any,
    reality_accessor: Callable[[Any], Iterable[Any]],
    legacy_loader: Callable[[], tuple[Any, ...]],
    allow_legacy_rollback_env: str,
    cross_check_label: str,
    load_program_reality: Callable[..., Any] | None = None,
    as_of: Any = None,
    edition_name: str | None = None,
    archive_root: Any = None,
) -> tuple[tuple[Any, ...], tuple[Any, ...] | None, tuple[str, ...], dict[str, dict[str, str | None]] | None]:
    """Load one fact family through the SoR-gated overlay pattern.

    Parameters
    ----------
    family:
        The **resolved authority family** to check with
        ``resolve_family_sor_mode`` — e.g. ``"judgment"`` for risk/decision/
        assumption, ``"workitem.state"`` for milestone/dependency/action/
        workstream, ``"commitment"`` for commitment. **This must be the true
        authority family from `vertex/policies/source_authority.yaml`'s
        `family_map`, never an invented family name matching the
        human-readable fact type** (see fix-data-flow.md v1.5 — PS-15/PS-16
        were originally built on the wrong assumption that each fact type is
        its own independent family; several fact types actually share one).
    reality_accessor:
        Callable taking a loaded ``ProgramReality`` and returning its
        ``FactAssessment`` tuple for this family (e.g. ``lambda r: r.risks()``).
    legacy_loader:
        Zero-arg callable returning the family's current legacy-path records
        (e.g. ``lambda: _load_current_risks(program_id, programs_root=programs_root)``).
        Called both for the legacy-mode primary path and for the non-legacy
        empty-set cross-check / rollback fallback.
    allow_legacy_rollback_env:
        Name of the per-family env var that, when truthy, permits falling
        back to ``legacy_loader`` on an unexpected (non-``ConfigError``)
        reality-read exception (e.g. ``"VERTEX_REPORT_ALLOW_LEGACY_RISK_ROLLBACK"``).
    cross_check_label:
        Short, human-readable family label used in warning text (e.g. ``"risk"``).
    load_program_reality:
        Optional injected loader (``ctx.stage_support.load_program_reality``),
        matching the seam milestone_stage.py already uses so a pipeline-level
        already-loaded ``ProgramReality`` can be threaded down instead of this
        function performing its own ``ProgramReality.load()`` call. When
        ``None``, loads directly.

    Returns
    -------
    ``(records, assessments, warnings, lineage)`` — ``assessments``/``lineage``
    are ``None`` in legacy mode (no ``FactAssessment`` wrapper exists there).
    """
    sor_mode = resolve_family_sor_mode(program_id, family, programs_root=programs_root)
    if sor_mode == "legacy":
        try:
            records = legacy_loader()
        except ConfigError as exc:
            return (), None, (f"{cross_check_label} skipped: {exc}",), None
        return records, None, (), None

    try:
        if load_program_reality is None:
            from src.core.program_reality import ProgramReality  # noqa: PLC0415

            kwargs: dict[str, Any] = {
                "programs_root": programs_root,
                "as_of": as_of,
                "edition_name": edition_name,
            }
            if archive_root is not None:
                kwargs["archive_root"] = archive_root
            reality = ProgramReality.load(program_id, **kwargs)
        else:
            reality = load_program_reality(
                program_id,
                programs_root=programs_root,
                as_of=as_of,
                edition_name=edition_name,
                archive_root=archive_root,
            )
        assessments = tuple(reality_accessor(reality))
        records = tuple(assessment.record for assessment in assessments)
        warnings: tuple[str, ...] = ()
        if not records:
            legacy_records = legacy_loader()
            if legacy_records:
                warnings = (
                    f"[{cross_check_label} cross-check] ProgramReality returned 0 {cross_check_label}s "
                    f"but the legacy source has {len(legacy_records)} — check whether "
                    f"{cross_check_label} facts have actually been bridged for this program.",
                )
        return records, assessments, warnings, _lineage_map(assessments)
    except ConfigError as exc:
        return (), (), (f"{cross_check_label} skipped (reality): {exc}",), {}
    except Exception as exc:  # noqa: BLE001 — deliberately broad: any reality-read failure triggers rollback
        if not _rollback_enabled(allow_legacy_rollback_env):
            raise ConfigError(
                f"ProgramReality {cross_check_label} read failed while {family} SoR is non-legacy. "
                f"Set {allow_legacy_rollback_env}=1 to use the audited legacy rollback path."
            ) from exc
        fallback_records = legacy_loader()
        return (
            fallback_records,
            (),
            (
                f"[{cross_check_label} SoR] degraded to legacy {cross_check_label} source via audited "
                f"rollback flag; {allow_legacy_rollback_env}=1; ProgramReality error: {exc}",
            ),
            {},
        )


def _lineage_map(assessments: tuple[Any, ...]) -> dict[str, dict[str, str | None]]:
    lineage_by_id: dict[str, dict[str, str | None]] = {}
    for assessment in assessments:
        record = getattr(assessment, "record", None)
        record_id = getattr(record, "id", None)
        if not record_id:
            continue
        lineage = getattr(assessment, "lineage", None)
        if lineage is None:
            continue
        lineage_by_id[str(record_id)] = {
            "source_document_key": getattr(lineage, "source_document_key", None),
            "approval_event_id": getattr(lineage, "approval_event_id", None),
        }
    return lineage_by_id


def _rollback_enabled(env_var_name: str) -> bool:
    return os.environ.get(env_var_name, "").strip().lower() in {"1", "true", "yes", "on"}
