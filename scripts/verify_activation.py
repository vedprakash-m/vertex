#!/usr/bin/env python3
"""Verify the live activation evidence for ``specs/activation.md``.

The activation spec is intentionally falsifiable: a real, approved REV fact
must change a real newsletter, with lineage back to the source EML.  This
script keeps the on-disk evidence current and fail-closed while the external
operator/IT gates are still in progress.

Usage
-----
    python scripts/verify_activation.py --program nova
    python scripts/verify_activation.py --program nova --markdown output/activation-evidence.md
    python scripts/verify_activation.py --self-test
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from src.core.rev.authority_scope import (  # noqa: E402
    REV_AUTHORITY_EVENT_SPECS,
    assess_rev_authority_scope,
)
from src.core.activation_readiness import (  # noqa: E402
    BindingReleaseReadiness,
    CrossSourceReconciliationPlan,
    ExplainDrilldownPlan,
    evaluate_binding_release_readiness,
    evaluate_cross_source_reconciliation,
    evaluate_explain_drilldown,
    evaluate_base_schema_cross_program,
    load_program_schema_sample,
)
from src.core.rev.quality_metrics import compute_quality_report  # noqa: E402
from src.core.truth_model import load_source_authority_policy  # noqa: E402


_DEFAULT_PROGRAMS_ROOT = _REPO_ROOT / "programs"
_DEFAULT_KEYSTONE = "milestone.completed"
_DATA_SUFFICIENCY_FLOOR = 30
_DUAL_LABEL_FLOOR = 20
_KAPPA_FLOOR = 0.70
_XTRACT_PREC_CI_LOW_FLOOR = 0.80
_ACCEPT_PREC_CI_LOW_FLOOR = 0.85
_SUPPORTED_STATUS = "recommended_v1_authoritative"
_CORPUS_FREEZE_SCHEMA_VERSION = "activation_corpus_freeze.v1"
_CORPUS_FREEZE_FILES = ("rev_labeled_corpus.jsonl", "corpus_manifest.jsonl")
_STRATA_BY_CLAIM_TYPE = {
    "deployment.completed": ("milestone", "deployment"),
    "milestone.completed": ("milestone",),
    "commitment.date_set": ("commitment",),
    "ownership.changed": ("workstream", "ownership"),
}


@dataclass(frozen=True, slots=True)
class CheckResult:
    check_id: str
    status: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status == "fail"


@dataclass(frozen=True, slots=True)
class FamilyMatrixRow:
    claim_event_type: str
    ledger_event_type: str
    fact_type: str | None
    authority_family: str | None
    accessor: str | None
    status: str
    labeled_count: int
    dual_labeled_count: int
    reachable_document_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_event_type": self.claim_event_type,
            "ledger_event_type": self.ledger_event_type,
            "fact_type": self.fact_type,
            "authority_family": self.authority_family,
            "accessor": self.accessor,
            "status": self.status,
            "labeled_count": self.labeled_count,
            "dual_labeled_count": self.dual_labeled_count,
            "reachable_document_count": self.reachable_document_count,
        }


@dataclass(frozen=True, slots=True)
class CounterfactualDiffResult:
    passed: bool
    added_line_count: int
    removed_line_count: int
    source_document_key_present: bool
    reason: str
    approval_event_id_present: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "added_line_count": self.added_line_count,
            "removed_line_count": self.removed_line_count,
            "source_document_key_present": self.source_document_key_present,
            "approval_event_id_present": self.approval_event_id_present,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ActivationReport:
    generated_at: str
    git_sha: str
    dirty_worktree: bool
    program: str
    keystone_family: str
    family_matrix: tuple[FamilyMatrixRow, ...]
    checks: tuple[CheckResult, ...]

    @property
    def failed(self) -> bool:
        return any(check.failed for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "git_sha": self.git_sha,
            "dirty_worktree": self.dirty_worktree,
            "program": self.program,
            "keystone_family": self.keystone_family,
            "family_matrix": [row.to_dict() for row in self.family_matrix],
            "checks": [
                {
                    "check_id": check.check_id,
                    "status": check.status,
                    "summary": check.summary,
                    "details": check.details,
                }
                for check in self.checks
            ],
            "failed": self.failed,
        }


def is_clean_cycle(cycle: Mapping[str, Any], *, eml_present: bool) -> bool:
    """Return the AG-3/AG-4 clean-cycle verdict from activation.md §6.14.2.

    A *clean* cycle is one that completed successfully AND processed real EMLs
    through the LLM extractor without any shield/fallback/degradation. The
    pipeline's terminal-clean status is ``fully_verified`` (all assertions
    written); ``complete`` is accepted for back-compat with the spec's prose
    vocabulary. ``acquisition_complete`` is *not* clean — it means assertions
    were not written for every staged candidate.
    """
    status = str(cycle.get("cycle_status", ""))
    return (
        status in {"complete", "fully_verified"}
        and not _as_bool(cycle.get("shield_degrade"))
        and not _as_bool(cycle.get("extraction_degraded"))
        and int(cycle.get("terminal_failures") or 0) == 0
        and int(cycle.get("enumerated") or 0) >= 1
        and int(cycle.get("candidates_staged") or 0) >= 1
        and int(cycle.get("llm_fallback_count") or 0) == 0
        and eml_present
    )


def classify_cycle(cycle: Mapping[str, Any], *, eml_present: bool) -> str:
    """Classify a REV cycle using activation.md §6.14.3."""
    status = str(cycle.get("cycle_status", ""))
    if status not in {"complete", "fully_verified", "acquisition_complete"}:
        return "incomplete"
    if is_clean_cycle(cycle, eml_present=eml_present):
        return "authority_valid"
    if (
        int(cycle.get("llm_fallback_count") or 0) == 0
        and not _as_bool(cycle.get("shield_degrade"))
        and not _as_bool(cycle.get("extraction_degraded"))
    ):
        return "quality_valid_not_clean"
    return "publication_valid_degraded"


def clean_cycle_streak(cycles: Iterable[Mapping[str, Any]], *, eml_present: bool) -> int:
    """Return consecutive trailing cycles that satisfy ``is_clean_cycle``."""
    streak = 0
    for cycle in reversed(tuple(cycles)):
        if not is_clean_cycle(cycle, eml_present=eml_present):
            break
        streak += 1
    return streak


def counterfactual_render_diff(
    with_fact_text: str,
    without_fact_text: str,
    *,
    source_document_key: str,
    approval_event_id: str | None = None,
) -> CounterfactualDiffResult:
    """Check that withholding the fact changes render output and cites source."""
    with_lines = with_fact_text.splitlines()
    without_lines = without_fact_text.splitlines()
    diff = tuple(difflib.ndiff(without_lines, with_lines))
    added = tuple(line[2:] for line in diff if line.startswith("+ "))
    removed = tuple(line[2:] for line in diff if line.startswith("- "))
    if not added and not removed:
        return CounterfactualDiffResult(False, 0, 0, False, "render outputs are identical", False if approval_event_id else None)
    source_present = any(source_document_key in line for line in added)
    if not source_present:
        return CounterfactualDiffResult(
            False,
            len(added),
            len(removed),
            False,
            "render changed, but added content does not carry source_document_key",
            False if approval_event_id else None,
        )
    approval_present = None
    if approval_event_id:
        approval_present = any(approval_event_id in line for line in added)
        if not approval_present:
            return CounterfactualDiffResult(
                False,
                len(added),
                len(removed),
                True,
                "render changed, but added content does not carry approval_event_id",
                False,
            )
    return CounterfactualDiffResult(
        True,
        len(added),
        len(removed),
        True,
        "counterfactual delta is attributable",
        approval_present,
    )


def build_counterfactual_diff_artifact(
    with_fact_text: str,
    without_fact_text: str,
    *,
    source_document_key: str,
    approval_event_id: str | None = None,
    with_fact_label: str = "with-fact-render",
    without_fact_label: str = "without-fact-render",
    context_lines: int = 3,
) -> str:
    """Render the durable AG-1 proof artifact for a counterfactual render pair."""
    result = counterfactual_render_diff(
        with_fact_text,
        without_fact_text,
        source_document_key=source_document_key,
        approval_event_id=approval_event_id,
    )
    diff_lines = difflib.unified_diff(
        without_fact_text.splitlines(),
        with_fact_text.splitlines(),
        fromfile=without_fact_label,
        tofile=with_fact_label,
        lineterm="",
        n=max(0, context_lines),
    )
    header = [
        "# Activation Counterfactual Diff",
        "",
        f"- source_document_key: `{source_document_key}`",
        f"- passed: `{str(result.passed).lower()}`",
        f"- reason: {result.reason}",
        f"- added_line_count: {result.added_line_count}",
        f"- removed_line_count: {result.removed_line_count}",
        f"- source_document_key_present: `{str(result.source_document_key_present).lower()}`",
        f"- approval_event_id: `{approval_event_id}`" if approval_event_id else "- approval_event_id: `not supplied`",
        f"- approval_event_id_present: `{str(result.approval_event_id_present).lower()}`"
        if result.approval_event_id_present is not None
        else "- approval_event_id_present: `not checked`",
        "",
        "```diff",
    ]
    body = list(diff_lines)
    footer = ["```", ""]
    return "\n".join(header + body + footer)


def build_family_matrix(
    *,
    program: str,
    programs_root: Path,
    repo_root: Path = _REPO_ROOT,
) -> tuple[FamilyMatrixRow, ...]:
    policy = load_source_authority_policy(repo_root=repo_root)
    assessments = {row.claim_event_type: row for row in assess_rev_authority_scope(policy)}
    labels = _read_jsonl(programs_root / program / "_quality" / "rev_labeled_corpus.jsonl")
    manifest = _read_jsonl(programs_root / program / "_quality" / "corpus_manifest.jsonl")
    labeled_counts = Counter(str(row.get("expected_event_type", "")) for row in labels)
    dual_counts = Counter(
        str(row.get("expected_event_type", ""))
        for row in labels
        if _has_second_label(row)
    )
    rows: list[FamilyMatrixRow] = []
    for spec in REV_AUTHORITY_EVENT_SPECS:
        assessment = assessments.get(spec.claim_event_type)
        rows.append(
            FamilyMatrixRow(
                claim_event_type=spec.claim_event_type,
                ledger_event_type=spec.ledger_event_type,
                fact_type=spec.fact_type,
                authority_family=assessment.authority_family if assessment is not None else None,
                accessor=spec.accessor,
                status=assessment.status if assessment is not None else spec.status,
                labeled_count=labeled_counts[spec.ledger_event_type],
                dual_labeled_count=dual_counts[spec.ledger_event_type],
                reachable_document_count=_reachable_document_count(manifest, spec.claim_event_type),
            )
        )
    return tuple(rows)


def build_activation_report(
    *,
    program: str,
    programs_root: Path = _DEFAULT_PROGRAMS_ROOT,
    repo_root: Path = _REPO_ROOT,
    keystone_family: str = _DEFAULT_KEYSTONE,
    with_fact_path: Path | None = None,
    without_fact_path: Path | None = None,
    source_document_key: str | None = None,
    approval_event_id: str | None = None,
    data_floor: int = _DATA_SUFFICIENCY_FLOOR,
) -> ActivationReport:
    git_sha, dirty = _git_metadata(repo_root)
    matrix = build_family_matrix(program=program, programs_root=programs_root, repo_root=repo_root)
    matrix_by_family = {row.claim_event_type: row for row in matrix}
    checks: list[CheckResult] = []

    checks.append(_check_verifier_self_contract())
    checks.extend(_check_keystone_matrix(matrix_by_family, keystone_family, data_floor=data_floor))
    checks.append(_check_last_cycle(program=program, programs_root=programs_root))
    checks.append(_check_clean_cycle_streak(program=program, programs_root=programs_root))
    checks.append(_check_counterfactual_paths(with_fact_path, without_fact_path, source_document_key, approval_event_id))
    checks.extend(_build_evidence_appendix_checks(program=program, programs_root=programs_root, repo_root=repo_root))

    return ActivationReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        git_sha=git_sha,
        dirty_worktree=dirty,
        program=program,
        keystone_family=keystone_family,
        family_matrix=matrix,
        checks=tuple(checks),
    )


def render_markdown(report: ActivationReport) -> str:
    lines = [
        f"<!-- generated by scripts/verify_activation.py at {report.generated_at} -->",
        "",
        f"**Commit:** `{report.git_sha}`",
        f"**Dirty worktree:** `{str(report.dirty_worktree).lower()}`",
        f"**Program:** `{report.program}`",
        f"**Keystone family:** `{report.keystone_family}`",
        "",
        "| Check | Status | Evidence |",
        "|---|---|---|",
    ]
    for check in report.checks:
        details = _compact_details(check.details)
        lines.append(f"| `{check.check_id}` | {check.status.upper()} | {check.summary}{details} |")
    lines.extend([
        "",
        "### Family/accessor matrix",
        "",
        "| Claim event | Ledger event | Fact type | Family | Accessor | Status | Labeled | Dual-labeled | Reachable docs |",
        "|---|---|---|---|---|---|---:|---:|---:|",
    ])
    for row in report.family_matrix:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{row.claim_event_type}`",
                    f"`{row.ledger_event_type}`",
                    f"`{row.fact_type or '-'}`",
                    f"`{row.authority_family or '-'}`",
                    f"`{row.accessor or '-'}`",
                    row.status,
                    str(row.labeled_count),
                    str(row.dual_labeled_count),
                    str(row.reachable_document_count),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def run_self_test() -> tuple[CheckResult, ...]:
    good_cycle = {
        "cycle_status": "complete",
        "shield_degrade": False,
        "extraction_degraded": False,
        "terminal_failures": 0,
        "enumerated": 1,
        "candidates_staged": 1,
        "llm_fallback_count": 0,
    }
    empty_cycle = dict(good_cycle, enumerated=0, candidates_staged=0)
    degraded_cycle = dict(good_cycle, extraction_degraded=True)
    good_diff = counterfactual_render_diff(
        "Milestone GA completed [source: eml:abc]",
        "",
        source_document_key="eml:abc",
    )
    bad_diff = counterfactual_render_diff("same", "same", source_document_key="eml:abc")
    results = [
        CheckResult(
            "SELF-CLEAN-CYCLE-POSITIVE",
            "pass" if is_clean_cycle(good_cycle, eml_present=True) else "fail",
            "known-good clean cycle is accepted",
        ),
        CheckResult(
            "SELF-CLEAN-CYCLE-EMPTY",
            "pass" if not is_clean_cycle(empty_cycle, eml_present=True) else "fail",
            "empty complete cycle is rejected",
        ),
        CheckResult(
            "SELF-CLEAN-CYCLE-DEGRADED",
            "pass" if not is_clean_cycle(degraded_cycle, eml_present=True) else "fail",
            "extraction_degraded cycle is rejected",
        ),
        CheckResult(
            "SELF-DIFF-POSITIVE",
            "pass" if good_diff.passed else "fail",
            "attributable render delta is accepted",
            good_diff.to_dict(),
        ),
        CheckResult(
            "SELF-DIFF-NEGATIVE",
            "pass" if not bad_diff.passed else "fail",
            "byte-identical render is rejected",
            bad_diff.to_dict(),
        ),
    ]
    return tuple(results)


def _check_verifier_self_contract() -> CheckResult:
    self_results = run_self_test()
    failures = [result.check_id for result in self_results if result.failed]
    if failures:
        return CheckResult(
            "P0-SELF-TEST",
            "fail",
            "verifier negative self-test failed",
            {"failures": failures},
        )
    return CheckResult(
        "P0-SELF-TEST",
        "pass",
        "verifier self-test rejects empty cycles and non-attributable render diffs",
        {"checks": len(self_results)},
    )


def _check_keystone_matrix(
    matrix_by_family: Mapping[str, FamilyMatrixRow],
    keystone_family: str,
    *,
    data_floor: int,
) -> tuple[CheckResult, ...]:
    row = matrix_by_family.get(keystone_family)
    if row is None:
        return (
            CheckResult(
                "O-0-KEYSTONE",
                "fail",
                f"keystone family {keystone_family!r} is absent from authority_scope.py",
            ),
        )
    details = row.to_dict() | {"data_floor": data_floor}
    checks: list[CheckResult] = []
    if row.status != _SUPPORTED_STATUS:
        checks.append(CheckResult("O-0-KEYSTONE", "fail", "keystone family is not v1-authoritative", details))
    else:
        checks.append(CheckResult("O-0-KEYSTONE", "pass", "keystone family is v1-authoritative", details))
    if row.reachable_document_count < data_floor:
        checks.append(
            CheckResult(
                "P-1-RAW-DATA",
                "fail",
                "keystone raw-data feasibility is below floor",
                details,
            )
        )
    else:
        checks.append(
            CheckResult(
                "P-1-RAW-DATA",
                "pass",
                "keystone raw-data feasibility meets floor",
                details,
            )
        )
    if row.labeled_count < data_floor:
        checks.append(
            CheckResult(
                "O-0-DATA-SUFFICIENCY",
                "fail",
                "keystone labeled corpus is below floor",
                details,
            )
        )
    else:
        checks.append(
            CheckResult(
                "O-0-DATA-SUFFICIENCY",
                "pass",
                "keystone labeled corpus meets floor",
                details,
            )
        )
    return tuple(checks)


def _check_last_cycle(*, program: str, programs_root: Path) -> CheckResult:
    cycle_path = programs_root / program / "_rev" / "last_cycle.json"
    if not cycle_path.exists():
        return CheckResult("AG-3-CLEAN-CYCLE", "fail", "last REV cycle file is missing", {"path": str(cycle_path)})
    cycle = _read_json(cycle_path)
    eml_present = _program_has_eml(program=program, programs_root=programs_root)
    clean = is_clean_cycle(cycle, eml_present=eml_present)
    return CheckResult(
        "AG-3-CLEAN-CYCLE",
        "pass" if clean else "fail",
        "last REV cycle satisfies is_clean_cycle()" if clean else "last REV cycle is not clean",
        {
            "path": str(cycle_path.relative_to(_REPO_ROOT)) if cycle_path.is_relative_to(_REPO_ROOT) else str(cycle_path),
            "cycle_status": cycle.get("cycle_status"),
            "cycle_class": classify_cycle(cycle, eml_present=eml_present),
            "shield_degrade": cycle.get("shield_degrade"),
            "extraction_degraded": cycle.get("extraction_degraded", False),
            "terminal_failures": cycle.get("terminal_failures", 0),
            "enumerated": cycle.get("enumerated", 0),
            "candidates_staged": cycle.get("candidates_staged", 0),
            "llm_fallback_count": cycle.get("llm_fallback_count", 0),
            "eml_present": eml_present,
        },
    )


def _check_clean_cycle_streak(*, program: str, programs_root: Path, required: int = 5) -> CheckResult:
    history_path = programs_root / program / "_rev" / "cycle_history.jsonl"
    rows = _read_jsonl(history_path)
    eml_present = _program_has_eml(program=program, programs_root=programs_root)
    streak = clean_cycle_streak(rows, eml_present=eml_present)
    return CheckResult(
        "AG-3-CLEAN-CYCLE-STREAK",
        "pass" if streak >= required else "fail",
        f"{streak}/{required} trailing REV cycles satisfy is_clean_cycle()",
        {
            "path": str(history_path.relative_to(_REPO_ROOT)) if history_path.is_relative_to(_REPO_ROOT) else str(history_path),
            "required": required,
            "streak": streak,
            "history_rows": len(rows),
            "eml_present": eml_present,
        },
    )


def _check_counterfactual_paths(
    with_fact_path: Path | None,
    without_fact_path: Path | None,
    source_document_key: str | None,
    approval_event_id: str | None,
) -> CheckResult:
    if with_fact_path is None or without_fact_path is None or not source_document_key:
        return CheckResult(
            "AG-1-COUNTERFACTUAL-DIFF",
            "fail",
            "counterfactual render diff was not supplied",
            {
                "required": [
                    "--with-fact-render",
                    "--without-fact-render",
                    "--source-document-key",
                ]
            },
        )
    if not with_fact_path.exists() or not without_fact_path.exists():
        return CheckResult(
            "AG-1-COUNTERFACTUAL-DIFF",
            "fail",
            "counterfactual render artifact path is missing",
            {"with_fact": str(with_fact_path), "without_fact": str(without_fact_path)},
        )
    result = counterfactual_render_diff(
        with_fact_path.read_text(encoding="utf-8", errors="replace"),
        without_fact_path.read_text(encoding="utf-8", errors="replace"),
        source_document_key=source_document_key,
        approval_event_id=approval_event_id,
    )
    return CheckResult(
        "AG-1-COUNTERFACTUAL-DIFF",
        "pass" if result.passed else "fail",
        result.reason,
        result.to_dict()
        | {
            "with_fact": str(with_fact_path),
            "without_fact": str(without_fact_path),
            "source_document_key": source_document_key,
            "approval_event_id": approval_event_id,
        },
    )


def write_counterfactual_diff_artifact(
    *,
    output_path: Path,
    with_fact_path: Path | None,
    without_fact_path: Path | None,
    source_document_key: str | None,
    approval_event_id: str | None = None,
    context_lines: int = 3,
) -> None:
    """Write the AG-1 counterfactual diff artifact, fail-closed on missing inputs."""
    if with_fact_path is None or without_fact_path is None or not source_document_key:
        raise ValueError("--counterfactual-diff requires --with-fact-render, --without-fact-render, and --source-document-key")
    if not with_fact_path.exists():
        raise FileNotFoundError(f"with-fact render does not exist: {with_fact_path}")
    if not without_fact_path.exists():
        raise FileNotFoundError(f"without-fact render does not exist: {without_fact_path}")
    artifact = build_counterfactual_diff_artifact(
        with_fact_path.read_text(encoding="utf-8", errors="replace"),
        without_fact_path.read_text(encoding="utf-8", errors="replace"),
        source_document_key=source_document_key,
        approval_event_id=approval_event_id,
        with_fact_label=str(with_fact_path),
        without_fact_label=str(without_fact_path),
        context_lines=context_lines,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(artifact, encoding="utf-8")


def build_corpus_certification_check(
    *,
    labels: Iterable[Mapping[str, Any]],
    quality_metrics: Mapping[str, Any],
    keystone_ledger_event_type: str,
    data_floor: int = _DATA_SUFFICIENCY_FLOOR,
    dual_label_floor: int = _DUAL_LABEL_FLOOR,
) -> CheckResult:
    """Evaluate the strict AG-2 activation corpus-certification bar."""
    label_rows = tuple(labels)
    keystone_rows = tuple(
        row for row in label_rows
        if str(row.get("expected_event_type", "")) == keystone_ledger_event_type
    )
    keystone_dual_rows = tuple(row for row in keystone_rows if _has_second_label(row))
    all_dual_count = sum(1 for row in label_rows if _has_second_label(row))
    kappa = _optional_float(quality_metrics.get("kappa"))
    kappa_n = _optional_int(quality_metrics.get("kappa_n")) or 0
    xtract_ci_low = _ci_low(quality_metrics.get("g_xtract_prec_ci"))
    accept_ci_low = _ci_low(quality_metrics.get("g_accept_prec_ci"))
    failures: list[str] = []
    if len(keystone_rows) < data_floor:
        failures.append(f"keystone_labels {len(keystone_rows)} < {data_floor}")
    if len(keystone_dual_rows) < dual_label_floor:
        failures.append(f"keystone_dual_labels {len(keystone_dual_rows)} < {dual_label_floor}")
    if kappa_n < dual_label_floor:
        failures.append(f"kappa_n {kappa_n} < {dual_label_floor}")
    if kappa is None or kappa < _KAPPA_FLOOR:
        failures.append(f"kappa {kappa if kappa is not None else 'null'} < {_KAPPA_FLOOR}")
    if xtract_ci_low is None or xtract_ci_low < _XTRACT_PREC_CI_LOW_FLOOR:
        failures.append(
            f"g_xtract_prec_ci_low {xtract_ci_low if xtract_ci_low is not None else 'null'} < {_XTRACT_PREC_CI_LOW_FLOOR}"
        )
    if accept_ci_low is None or accept_ci_low < _ACCEPT_PREC_CI_LOW_FLOOR:
        failures.append(
            f"g_accept_prec_ci_low {accept_ci_low if accept_ci_low is not None else 'null'} < {_ACCEPT_PREC_CI_LOW_FLOOR}"
        )
    details = {
        "keystone_ledger_event_type": keystone_ledger_event_type,
        "keystone_label_count": len(keystone_rows),
        "keystone_dual_labeled_count": len(keystone_dual_rows),
        "all_dual_labeled_count": all_dual_count,
        "data_floor": data_floor,
        "dual_label_floor": dual_label_floor,
        "kappa": kappa,
        "kappa_n": kappa_n,
        "g_xtract_prec": quality_metrics.get("g_xtract_prec"),
        "g_xtract_prec_ci_low": xtract_ci_low,
        "g_accept_prec": quality_metrics.get("g_accept_prec"),
        "g_accept_prec_ci_low": accept_ci_low,
        "quality_failures": list(quality_metrics.get("failures") or ()),
    }
    if failures:
        return CheckResult(
            "AG-2-CORPUS-CERTIFICATION",
            "fail",
            "keystone corpus is not certified for activation",
            details | {"activation_failures": failures},
        )
    return CheckResult(
        "AG-2-CORPUS-CERTIFICATION",
        "pass",
        "keystone corpus clears activation certification bar",
        details,
    )


def build_corpus_freeze_manifest(
    *,
    program: str,
    programs_root: Path = _DEFAULT_PROGRAMS_ROOT,
    repo_root: Path = _REPO_ROOT,
) -> dict[str, Any]:
    """Build the deterministic Track-C corpus freeze manifest payload."""
    git_sha, _dirty = _git_metadata(repo_root)
    quality_root = programs_root / program / "_quality"
    files: dict[str, dict[str, Any]] = {}
    for filename in _CORPUS_FREEZE_FILES:
        path = quality_root / filename
        files[filename] = {
            "path": str(path.relative_to(repo_root)) if path.exists() and path.is_relative_to(repo_root) else str(path),
            "exists": path.exists(),
            "rows": _jsonl_row_count(path),
            "sha256": _file_sha256(path),
        }
    return {
        "schema_version": _CORPUS_FREEZE_SCHEMA_VERSION,
        "program": program,
        "git_sha": git_sha,
        "files": files,
    }


def write_corpus_freeze_manifest(
    *,
    program: str,
    programs_root: Path = _DEFAULT_PROGRAMS_ROOT,
    repo_root: Path = _REPO_ROOT,
) -> Path:
    """Write the current corpus freeze manifest for an explicit operator freeze."""
    output_path = programs_root / program / "_quality" / "corpus_freeze.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            build_corpus_freeze_manifest(
                program=program,
                programs_root=programs_root,
                repo_root=repo_root,
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return output_path


def build_corpus_freeze_check(
    *,
    program: str,
    programs_root: Path = _DEFAULT_PROGRAMS_ROOT,
    repo_root: Path = _REPO_ROOT,
) -> CheckResult:
    """Verify `_quality/corpus_freeze.json` pins corpus hashes, row counts, and SHA."""
    freeze_path = programs_root / program / "_quality" / "corpus_freeze.json"
    expected = build_corpus_freeze_manifest(
        program=program,
        programs_root=programs_root,
        repo_root=repo_root,
    )
    observed = _read_json(freeze_path)
    details = {
        "path": str(freeze_path),
        "expected": expected,
        "actual": observed or None,
    }
    if not observed:
        return CheckResult(
            "P2-CORPUS-FREEZE-MANIFEST",
            "fail",
            "corpus freeze manifest is missing",
            details,
        )
    failures: list[str] = []
    for key in ("schema_version", "program", "git_sha"):
        if observed.get(key) != expected.get(key):
            failures.append(f"{key} mismatch")
    observed_files = observed.get("files") if isinstance(observed.get("files"), dict) else {}
    for filename, expected_file in expected["files"].items():
        observed_file = observed_files.get(filename) if isinstance(observed_files, dict) else None
        if not isinstance(observed_file, dict):
            failures.append(f"{filename} missing")
            continue
        for key in ("exists", "rows", "sha256"):
            if observed_file.get(key) != expected_file.get(key):
                failures.append(f"{filename}.{key} mismatch")
    if failures:
        return CheckResult(
            "P2-CORPUS-FREEZE-MANIFEST",
            "fail",
            "corpus freeze manifest does not match current corpus inputs",
            details | {"failures": failures},
        )
    return CheckResult(
        "P2-CORPUS-FREEZE-MANIFEST",
        "pass",
        "corpus freeze manifest matches current corpus hashes, row counts, and commit SHA",
        details,
    )


def _build_evidence_appendix_checks(
    *,
    program: str,
    programs_root: Path,
    repo_root: Path,
) -> tuple[CheckResult, ...]:
    report_py = repo_root / "src" / "commands" / "report.py"
    verify_activation_py = repo_root / "scripts" / "verify_activation.py"
    ledger_py = repo_root / "src" / "commands" / "ledger.py"
    milestone_stage_py = repo_root / "src" / "core" / "stages" / "milestone_stage.py"
    privacy_py = repo_root / "src" / "core" / "rev" / "privacy.py"
    normalizer_py = repo_root / "src" / "core" / "rev" / "normalizer.py"
    entity_binding_py = repo_root / "src" / "core" / "rev" / "entity_binding_gate.py"
    rev_pipeline_py = repo_root / "src" / "core" / "rev" / "pipeline.py"
    fact_bridge_py = repo_root / "src" / "core" / "ledger" / "fact_bridge.py"
    truth_model_py = repo_root / "src" / "core" / "truth_model.py"
    program_reality_py = repo_root / "src" / "core" / "program_reality.py"
    view_models_py = repo_root / "src" / "core" / "view_models.py"
    report_deck_py = repo_root / "src" / "commands" / "report_deck.py"
    deck_renderer_py = repo_root / "src" / "core" / "deck_renderer.py"
    deck_template = repo_root / "templates" / "archetypes" / "deck.j2"
    milestone_template = repo_root / "templates" / "partials" / "milestone_rows.j2"
    vertex_prd_md = repo_root / "specs" / "vertex-prd.md"
    automation_honesty_adr = repo_root / "governance" / "decisions" / "0007-activation-automation-honesty.md"
    rev_contracts_py = repo_root / "tests" / "contracts" / "test_rev_contracts.py"
    conflict_contract_py = repo_root / "tests" / "contracts" / "test_conflict_engine.py"
    entity_binding_contract_py = repo_root / "tests" / "contracts" / "test_s6_entity_binding_gate.py"
    workflow_contract_py = repo_root / "tests" / "contracts" / "test_activation_operator_workflow_contract.py"
    rev_pipeline_tests_py = repo_root / "tests" / "unit" / "test_rev_pipeline_local_import.py"
    report_refs = _count_text(report_py, "ProgramReality")
    rollback_env_present = _count_text(milestone_stage_py, "VERTEX_REPORT_ALLOW_LEGACY_MILESTONE_ROLLBACK") > 0
    rollback_warning_present = _count_text(milestone_stage_py, "degraded to legacy milestone source via audited rollback flag") > 0
    row_source_field_present = _count_text(view_models_py, "source_document_key: str | None = None") > 0
    row_approval_field_present = _count_text(view_models_py, "approval_event_id: str | None = None") > 0
    template_source_present = _count_text(milestone_template, "row.source_document_key") > 0
    diff_artifact_present = (
        _count_text(verify_activation_py, "write_counterfactual_diff_artifact") > 0
        and _count_text(verify_activation_py, "--counterfactual-diff") > 0
        and _count_text(verify_activation_py, "--approval-event-id") > 0
        and _count_text(verify_activation_py, "difflib.unified_diff") > 0
    )
    last_cycle = _read_json(programs_root / program / "_rev" / "last_cycle.json")
    labels = _read_jsonl(programs_root / program / "_quality" / "rev_labeled_corpus.jsonl")
    label_counts = Counter(str(row.get("expected_event_type", "")) for row in labels)
    quality_metrics_path = programs_root / program / "_quality" / "rev_quality_metrics.json"
    quality_metrics = compute_quality_report(program_id=program, programs_root=programs_root).to_dict()
    bridge_default = _count_text(repo_root / "src" / "core" / "models_v2.py", "fact_bridge_enabled: bool = True") > 0
    bridge_env_set = os.environ.get("VERTEX_LEDGER_FACT_BRIDGE", "").strip().lower() in {"1", "true", "yes", "on"}
    triage_edit_present = _count_text(ledger_py, '@triage_app.command("edit")') > 0
    triage_revoke_present = _count_text(ledger_py, '@triage_app.command("revoke")') > 0
    correction_event_present = _count_text(ledger_py, "operator.correction.v1") > 0
    candidate_revoked_event_present = _count_text(ledger_py, "discovery.candidate_revoked.v1") > 0
    privacy_scaffold = {
        "pseudonym_table": _count_text(privacy_py, "class PseudonymTable") > 0,
        "pseudonymize_text": _count_text(privacy_py, "def pseudonymize_text") > 0,
        "local_checks": _count_text(privacy_py, "def run_local_checks") > 0,
        "credential_scan": _count_text(privacy_py, "def scan_credentials") > 0,
        "normalizer_mapping": _count_text(normalizer_py, "pseudonym_table") > 0,
        "contract_tests": _count_text(rev_contracts_py, "pseudonym_table") > 0
        and _count_text(rev_contracts_py, "run_local_checks") > 0,
        # v1.24 (AG-11 honesty): run_local_checks must be WIRED into the
        # candidate→fact projection chokepoint (the trust-root gate), not just
        # defined + used at ingest. This closes the over-claim where the scaffold
        # passed while the projection path was unchecked.
        "projection_gate_wired": _count_text(ledger_py, "_projection_privacy_gate") > 0
        and _count_text(ledger_py, "run_local_checks") > 0
        and _count_text(ledger_py, "OPERATOR_CONFIRMED") > 0,
    }
    conflict_scaffold = {
        "conflict_detector": _count_text(truth_model_py, "def detect_corroboration_and_conflicts") > 0,
        "fact_conflict": _count_text(truth_model_py, "fact.conflict") > 0,
        "disputed_projection": _count_text(program_reality_py, "disputed_natural_keys") > 0,
        "reality_conflicts": _count_text(program_reality_py, "def conflicts") > 0,
        "contract_tests": _count_text(conflict_contract_py, "fact.conflict") > 0,
    }
    # v1.25 (AG-9): the detector is now WIRED into the REV cycle finalize path —
    # ``_run_cross_source_conflict_check`` (in rev/pipeline.py) delegates to
    # ``fact_bridge.run_cross_source_conflict_detection`` (v1.29+: moved out of
    # the REV zone per W2-12 — REV modules must never import ProgramFactStore/
    # append_fact directly), which calls ``detect_corroboration_and_conflicts``
    # over the fact-store snapshot and writes ``fact.conflict``/``fact.corroboration``
    # (§6.14.5). The detector finds no conflict when entity keys don't yet align
    # (current pilot-program state) — that's the honest "no disagreement detected"
    # result, not a gap.
    conflict_wired = (
        _count_text(rev_pipeline_py, "_run_cross_source_conflict_check") > 0
        and _count_text(fact_bridge_py, "def run_cross_source_conflict_detection") > 0
        and _count_text(fact_bridge_py, "detect_corroboration_and_conflicts") > 0
    )
    entity_binding_scaffold = {
        "evaluate_binding": _count_text(entity_binding_py, "def evaluate_binding") > 0,
        "binding_record": _count_text(entity_binding_py, "def binding_record_from_entity_refs") > 0,
        "precision_floor": _count_text(entity_binding_py, "PRECISION_FLOOR") > 0,
        "coverage_floor": _count_text(entity_binding_py, "COVERAGE_FLOOR") > 0,
        "contract_tests": _count_text(entity_binding_contract_py, "evaluate_binding") > 0,
        # v1.24 (AG-15 honesty): the S-6 gate must have REAL input — candidates
        # must carry resolved entity_resolution tuples, not the empty () that
        # left the gate with no data. This closes the over-claim where the scaffold
        # passed while candidates were staged with entity_resolution=().
        "resolution_wired": _count_text(rev_pipeline_py, "_resolve_candidate_entities") > 0
        and _count_text(rev_pipeline_py, "EntityRegistry") > 0,
    }
    degradation_scaffold = {
        "cycle_status": _count_text(rev_pipeline_py, "cycle_status") > 0,
        "extraction_degraded": _count_text(rev_pipeline_py, "extraction_degraded") > 0,
        # §6.14.3 (v1.16): extraction_degraded must be a REAL persisted boolean
        # field on RevCycleReport + last_cycle.json, not just a cycle_status
        # string value (the v1.15 verifier only checked the string appeared).
        "extraction_degraded_field": _count_text(rev_pipeline_py, "extraction_degraded: bool = False") > 0,
        "extraction_degraded_persisted": _count_text(rev_pipeline_py, '"extraction_degraded": report.extraction_degraded') >= 2,
        "terminal_failures": _count_text(rev_pipeline_py, "terminal_failures") > 0,
        "degradation_tests": _count_text(rev_pipeline_tests_py, "extraction_degraded") > 0,
        "source_unreachable": _count_text(rev_pipeline_py, "source_unreachable") > 0,
    }
    consolidated_version = _read_consolidated_version(repo_root / ".archive" / "specs" / "consolidated.md")

    # ---- v1.16 hardening scaffolds (§6.12/§6.14.9/§6.14.13/§6.15.2/§6.10) ----
    operator_identity_py = repo_root / "src" / "core" / "operator_identity.py"
    provenance_gate_py = repo_root / "src" / "core" / "rev" / "provenance_gate.py"
    ado_schema_drift_py = repo_root / "src" / "core" / "ado_schema_drift.py"
    triage_telemetry_py = repo_root / "src" / "core" / "ledger" / "triage_telemetry.py"
    extractor_py = repo_root / "src" / "ai" / "rev" / "extractor.py"
    candidate_store_py = repo_root / "src" / "core" / "ledger" / "candidate_store.py"
    source_refs_py = repo_root / "src" / "core" / "ledger" / "source_refs.py"
    runbook_md = repo_root / "governance" / "runbook.md"
    activation_slo_py = repo_root / "src" / "core" / "activation_slo.py"
    activation_benefit_py = repo_root / "src" / "core" / "activation_benefit.py"
    activation_fleet_py = repo_root / "src" / "core" / "activation_fleet.py"
    fact_sor_state_py = repo_root / "src" / "core" / "fact_sor_state.py"
    ncfl_apply_py = repo_root / "src" / "core" / "ncfl_apply.py"
    ncfl_apply_policy_py = repo_root / "src" / "core" / "ncfl_apply_policy.py"
    ncfl_store_policy_py = repo_root / "src" / "core" / "ncfl_store_policy.py"
    program_fact_store_py = repo_root / "src" / "core" / "program_fact_store.py"
    source_authority_yaml = repo_root / "vertex" / "policies" / "source_authority.yaml"
    privacy_matrix_md = repo_root / "governance" / "privacy-matrix.md"
    data_classification_yaml = repo_root / "governance" / "data-classification.yaml"
    ux_spec_md = repo_root / "specs" / "vertex-ux-spec.md"
    operator_clarity_review_md = repo_root / "governance" / "activation-operator-clarity-review.md"
    raw_data_feasibility_md = repo_root / "governance" / "activation-raw-data-feasibility.md"
    annotation_plan_md = repo_root / "governance" / "activation-annotation-plan.md"
    pilot_degrade_adr = repo_root / "governance" / "decisions" / "0008-activation-pilot-degrade-exception.md"
    vision_reconciliation_md = repo_root / "governance" / "activation-vision-reconciliation-plan.md"
    release_checklist_md = repo_root / "governance" / "activation-release-checklist.md"
    flip_gate_contract_py = repo_root / "tests" / "contracts" / "test_s5c_flip_gate.py"
    fleet_isolation_contract_py = repo_root / "tests" / "contracts" / "test_fleet_isolation.py"
    reality_facade_tests_py = repo_root / "tests" / "unit" / "test_reality_facade_extensions.py"
    reverse_lookup_contract_py = repo_root / "tests" / "contracts" / "test_s3c_lineage_reverse_lookup.py"
    rev_bridge_contract_py = repo_root / "tests" / "contracts" / "test_rev_bridge_decoupling.py"
    ncfl_apply_policy_contract_py = repo_root / "tests" / "contracts" / "test_ncfl_apply_policy.py"
    ncfl_store_policy_contract_py = repo_root / "tests" / "contracts" / "test_ncfl_store_policy.py"
    ncfl_flow_tests_py = repo_root / "tests" / "unit" / "test_ncfl_flow.py"
    commands_ledger_tests_py = repo_root / "tests" / "unit" / "test_commands_ledger.py"
    platform_proof_log = programs_root / program / "platform_proof_log.yaml"
    trusted_baseline = programs_root / program / "trusted_baseline.yaml"
    operator_identity_scaffold = {
        "capture_helper": _count_text(operator_identity_py, "def capture_operator_identity") > 0,
        "principal_machine": _count_text(operator_identity_py, "principal") > 0
        and _count_text(operator_identity_py, "machine") > 0,
        "assertion_ref_fields": _count_text(source_refs_py, "principal: str | None = None") > 0
        and _count_text(source_refs_py, "machine: str | None = None") > 0,
        "wired_into_triage": _count_text(ledger_py, "capture_operator_identity") > 0,
    }
    provenance_scaffold = {
        "evaluate_sender": _count_text(provenance_gate_py, "def evaluate_sender") > 0,
        "allowlist_loader": _count_text(provenance_gate_py, "def load_allowlist") > 0,
        "forge_eml_reason": _count_text(provenance_gate_py, "forge-EML") > 0,
        "wired_into_pipeline": _count_text(rev_pipeline_py, "provenance_admit") > 0,
    }
    ado_drift_scaffold = {
        "required_fields": _count_text(ado_schema_drift_py, "ADO_REQUIRED_FIELDS") > 0,
        "fail_closed": _count_text(ado_schema_drift_py, "class SchemaDriftError") > 0,
        "contract_drift": _count_text(ado_schema_drift_py, "def inspect_contract_drift") > 0,
        "wired_into_hydration": _count_text(repo_root / "src" / "core" / "ado_hydration.py", "assert_row_shape") > 0,
    }
    telemetry_scaffold = {
        "record_helper": _count_text(triage_telemetry_py, "def record_triage_decision_telemetry") > 0,
        "time_to_triage": _count_text(triage_telemetry_py, "time_to_triage_seconds") > 0,
        "summarizer": _count_text(triage_telemetry_py, "def summarize_triage_telemetry") > 0,
        "wired_into_decisions": _count_text(candidate_store_py, "record_triage_decision_telemetry") > 0,
    }
    lineage_richness_scaffold = {
        "prompt_version_candidate": _count_text(candidate_store_py, "prompt_version: str | None = None") > 0,
        "extraction_rationale_candidate": _count_text(candidate_store_py, "extraction_rationale: str | None = None") > 0,
        "prompt_version_claim": _count_text(extractor_py, "prompt_version: str =") > 0,
        "extraction_rationale_claim": _count_text(extractor_py, "extraction_rationale: str | None = None") > 0,
        "thread_id_email": _count_text(source_refs_py, "thread_id: str | None = None") > 0,
    }
    injection_fence_scaffold = {
        "randomized_fence": _count_text(extractor_py, "_injection_fence") > 0,
        "untrusted_wrapping": _count_text(extractor_py, "untrusted-email-") > 0,
        "secrets_token": _count_text(extractor_py, "secrets.token_hex") > 0,
    }
    slo_scaffold = {
        "budget_dataclass": _count_text(activation_slo_py, "class ActivationSloBudget") > 0,
        "sample_dataclass": _count_text(activation_slo_py, "class ActivationSloSample") > 0,
        "evaluator": _count_text(activation_slo_py, "def evaluate_activation_slo") > 0,
        "wall_clock_budget": _count_text(activation_slo_py, "rev_wall_clock_seconds_per_100_eml") > 0,
        "cost_budget": _count_text(activation_slo_py, "cost_usd_per_100_eml") > 0,
    }
    roi_scaffold = {
        "time_motion_sample": _count_text(activation_slo_py, "class TimeMotionSample") > 0,
        "roi_evaluator": _count_text(activation_slo_py, "def evaluate_time_motion_roi") > 0,
        "manual_export": _count_text(activation_slo_py, "manual_export_seconds") > 0,
        "manual_typing": _count_text(activation_slo_py, "manual_typing_seconds") > 0,
    }
    automation_honesty = {
        "adr_present": automation_honesty_adr.exists(),
        "manual_export_named": _count_text(automation_honesty_adr, "manual") > 0
        and _count_text(automation_honesty_adr, "EML") > 0,
        "automatic_after_deposit": _count_text(automation_honesty_adr, "automatic_after_deposit") > 0
        and _count_text(vertex_prd_md, "automatic_after_deposit") > 0,
        "graph_roadmap": _count_text(automation_honesty_adr, "Graph") > 0,
        "roi_includes_export": _count_text(activation_slo_py, "manual_export_seconds") > 0,
    }
    explain_min_scaffold = {
        "candidate_rationale_field": _count_text(candidate_store_py, "extraction_rationale: str | None = None") > 0,
        "triage_json_rationale": _count_text(ledger_py, '"extraction_rationale": candidate.extraction_rationale') > 0,
        "triage_text_why": _count_text(ledger_py, "why:") > 0,
        "source_key_visible": _count_text(ledger_py, '"source_document_key": candidate.source_document_key') > 0,
    }
    multi_altitude_scaffold = {
        "deck_consumer_exists": report_deck_py.exists(),
        "deck_reads_program_reality": _count_text(report_deck_py, "ProgramReality") > 0,
        "deck_milestone_lineage_row": _count_text(deck_renderer_py, "class DeckMilestoneRow") > 0
        and _count_text(deck_renderer_py, "source_document_key: str | None = None") > 0
        and _count_text(deck_renderer_py, "approval_event_id: str | None = None") > 0,
        "deck_builder_accepts_lineage": _count_text(report_deck_py, "milestone_lineage") > 0
        and _count_text(report_deck_py, "source_document_key=lineage.get") > 0,
        "deck_template_renders_lineage": _count_text(deck_template, "row.source_document_key") > 0
        and _count_text(deck_template, "row.approval_event_id") > 0,
    }
    operator_workflow_contract = {
        "contract_present": workflow_contract_py.exists(),
        "export_step": _count_text(workflow_contract_py, "export EML") > 0
        or _count_text(workflow_contract_py, "manual export") > 0,
        "gather_rev_step": _count_text(workflow_contract_py, "gather") > 0
        and _count_text(workflow_contract_py, "REV") > 0,
        "triage_edit_approve": _count_text(workflow_contract_py, "edit") > 0
        and _count_text(workflow_contract_py, "approve") > 0,
        "report_revoke_rerender": _count_text(workflow_contract_py, "report") > 0
        and _count_text(workflow_contract_py, "revoke") > 0,
    }
    ag1b_robustness_scaffold = {
        "duplicate_eml_suppressed": _count_text(rev_pipeline_tests_py, "test_second_cycle_does_not_reprocess_processed_file") > 0
        and _count_text(rev_contracts_py, "test_only_one_record_written_on_duplicate") > 0,
        "malformed_fail_closed": _count_text(rev_pipeline_tests_py, "quarantine") > 0
        and _count_text(rev_contracts_py, "is rejected") > 0,
        "rejected_candidate_invisible": _count_text(commands_ledger_tests_py, "discovery.candidate_rejected.v1") > 0
        and _count_text(commands_ledger_tests_py, 'projection["proj_milestone"] == []') > 0,
        "crash_recovery": _count_text(rev_pipeline_tests_py, "claimed_at_startup_count") > 0
        and _count_text(rev_pipeline_tests_py, "crash_loop") > 0,
        "replay_deterministic": _count_text(commands_ledger_tests_py, "deep_projection_match") > 0
        and _count_text(rev_bridge_contract_py, "canonical_projection_dump") > 0,
    }
    ncfl_scaffold = {
        "apply_engine": _count_text(ncfl_apply_py, "def apply_proposal") > 0
        and _count_text(ncfl_apply_py, "def apply_proposals_batch") > 0,
        "recoverable_journal": _count_text(ncfl_apply_py, "_write_journal") > 0
        and _count_text(ncfl_apply_py, "needs_repair") > 0,
        "optimistic_concurrency": _count_text(ncfl_apply_py, "current_value_hash") > 0,
        "canonical_save_dispatch": _count_text(ncfl_apply_py, "canonical save_*") > 0
        and _count_text(ncfl_apply_py, "save_milestones") > 0
        and _count_text(ncfl_apply_py, "save_workstreams_document") > 0,
        "knowledge_doc_writable": _count_text(ncfl_store_policy_py, "knowledge_doc") > 0
        and _count_text(ncfl_store_policy_contract_py, "knowledge_doc") > 0,
        "policy_contract": _count_text(ncfl_apply_policy_py, "NCFL_APPLY_TRANSITIONS") > 0
        and _count_text(ncfl_apply_policy_contract_py, "test_apply_state_machine_matches_recoverable_spec") > 0,
        "flow_tests": _count_text(ncfl_flow_tests_py, "context apply") > 0
        or _count_text(ncfl_flow_tests_py, "extract_proposals") > 0,
    }
    reverse_lookup_scaffold = {
        "lineage_source_key": _count_text(program_fact_store_py, "source_document_key: str | None = None") > 0,
        "lineage_approval_id": _count_text(program_fact_store_py, "approval_event_id: str | None = None") > 0,
        "redaction_unavailable": _count_text(program_fact_store_py, "def as_redacted") > 0
        and _count_text(program_fact_store_py, "FactLineageUnavailable") > 0,
        "retention_unavailable": _count_text(program_fact_store_py, "def as_retention_expired") > 0,
        "contract_tests": _count_text(reverse_lookup_contract_py, "test_reverse_lookup_source_document_key_present") > 0
        and _count_text(reverse_lookup_contract_py, "test_access_denied_unavailability") > 0,
    }
    longitudinal_benefit_scaffold = {
        "sample_dataclass": _count_text(activation_benefit_py, "class BenefitTrendSample") > 0,
        "evaluator": _count_text(activation_benefit_py, "def evaluate_longitudinal_benefit") > 0,
        "auto_approved_signal_rate": _count_text(activation_benefit_py, "auto_approved_signal_rate") > 0,
        "operator_review_seconds": _count_text(activation_benefit_py, "operator_review_seconds") > 0,
        "tests": _count_text(repo_root / "tests" / "unit" / "test_activation_hardening_v1_16.py", "test_longitudinal_benefit") > 0,
    }
    corpus_rollback_scaffold = {
        "quality_sample": _count_text(activation_benefit_py, "class SustainingQualitySample") > 0,
        "rollback_evaluator": _count_text(activation_benefit_py, "def evaluate_corpus_rollback") > 0,
        "kappa_floor": _count_text(activation_benefit_py, "kappa_floor") > 0,
        "precision_recall_floors": _count_text(activation_benefit_py, "precision_ci_low_floor") > 0
        and _count_text(activation_benefit_py, "recall_ci_low_floor") > 0,
        "operator_runbook": _count_text(runbook_md, "corpus rollback") > 0
        or _count_text(runbook_md, "Auto-demote") > 0,
        "tests": _count_text(repo_root / "tests" / "unit" / "test_activation_hardening_v1_16.py", "test_corpus_rollback") > 0,
    }
    accessor_ladder_scaffold = {
        "rollout_plan_dataclass": _count_text(activation_fleet_py, "class AccessorRolloutPlan") > 0,
        "rollout_plan_evaluator": _count_text(activation_fleet_py, "def build_accessor_rollout_plan") > 0,
        "rollout_plan_tests": _count_text(
            repo_root / "tests" / "unit" / "test_activation_hardening_v1_16.py",
            "test_accessor_rollout_plan",
        )
        > 0,
        "shared_milestones_accessor": _count_text(repo_root / "tests" / "unit" / "test_verify_activation.py", "test_family_matrix_surfaces_shared_milestones_accessor") > 0,
        "family_flip_gate": _count_text(fact_sor_state_py, "def evaluate_family_flip_gate") > 0,
        "sor_policy": _count_text(source_authority_yaml, "sor_flip:") > 0,
    }
    fleet_soak_scaffold = {
        "fleet_sample_dataclass": _count_text(activation_fleet_py, "class FleetProgramSample") > 0,
        "fleet_soak_evaluator": _count_text(activation_fleet_py, "def evaluate_fleet_soak") > 0,
        "quiet_lane_required": _count_text(activation_fleet_py, "quiet_lane_count") > 0,
        "growth_cost_concurrency_budgets": _count_text(activation_fleet_py, "growth_mb_per_program") > 0
        and _count_text(activation_fleet_py, "cost_usd_per_program") > 0
        and _count_text(activation_fleet_py, "fleet_concurrency_cap") > 0,
        "cross_program_isolation_contract": _count_text(fleet_isolation_contract_py, "Fleet isolation") > 0,
        "fleet_reality_contract": _count_text(reality_facade_tests_py, "FleetReality") > 0,
    }
    data_residency_scaffold = {
        "residency_evaluator": _count_text(activation_fleet_py, "def evaluate_data_residency") > 0,
        "privacy_matrix": privacy_matrix_md.exists()
        and _count_text(privacy_matrix_md, "retention") > 0,
        "data_classification": data_classification_yaml.exists(),
        "m365_or_kusto_boundary": _count_text(privacy_matrix_md, "kusto") > 0
        or _count_text(privacy_matrix_md, "M365") > 0,
    }
    operator_clarity_scaffold = {
        "clarity_evaluator": _count_text(activation_fleet_py, "def evaluate_operator_clarity") > 0,
        "review_template": operator_clarity_review_md.exists()
        and _count_text(operator_clarity_review_md, "EXPLAIN-min") > 0
        and _count_text(operator_clarity_review_md, "disputed") > 0,
        "ux_accessibility_contract": _count_text(ux_spec_md, "Accessibility") > 0,
        "triage_explain": _count_text(ledger_py, "why:") > 0,
        "downgrade_banner_contract": _count_text(milestone_stage_py, "degraded to legacy milestone source via audited rollback flag") > 0,
    }
    raw_data_plan_scaffold = {
        "readiness_evaluator": _count_text(repo_root / "src" / "core" / "activation_readiness.py", "def evaluate_raw_data_feasibility") > 0,
        "feasibility_report": raw_data_feasibility_md.exists(),
        "current_counts": _count_text(raw_data_feasibility_md, "29") > 0
        and _count_text(raw_data_feasibility_md, "30") > 0,
        "owner_path": _count_text(raw_data_feasibility_md, "Acquisition owner") > 0
        and _count_text(raw_data_feasibility_md, "Acquisition path") > 0,
        "fail_closed_rule": _count_text(raw_data_feasibility_md, "do not mark `P-1-RAW-DATA` green") > 0,
    }
    annotation_staffing_scaffold = {
        "staffing_evaluator": _count_text(repo_root / "src" / "core" / "activation_readiness.py", "def evaluate_annotation_staffing") > 0,
        "plan_present": annotation_plan_md.exists(),
        "second_annotator": _count_text(annotation_plan_md, "Second annotator") > 0
        and _count_text(annotation_plan_md, "activation-secondary-labeler") > 0,
        "adjudicator": _count_text(annotation_plan_md, "Adjudicator") > 0
        and _count_text(annotation_plan_md, "activation-adjudicator") > 0,
        "dual_label_target": _count_text(annotation_plan_md, "20 dual-labeled") > 0,
        "quality_bar": _count_text(annotation_plan_md, "κ >= 0.70") > 0
        and _count_text(annotation_plan_md, "g_xtract_prec_ci_low") > 0,
    }
    pilot_degrade_exception_scaffold = {
        "exception_evaluator": _count_text(repo_root / "src" / "core" / "activation_readiness.py", "def evaluate_pilot_degrade_exception") > 0,
        "adr_present": pilot_degrade_adr.exists(),
        "proof_only": _count_text(pilot_degrade_adr, "proof_only: true") > 0,
        "blocks_authority": _count_text(pilot_degrade_adr, "blocks_authority_cycles: true") > 0,
        "expiry_owner": _count_text(pilot_degrade_adr, "Expires on") > 0
        and _count_text(pilot_degrade_adr, "Owner") > 0,
    }
    base_schema_samples = tuple(
        load_program_schema_sample(candidate, programs_root=programs_root)
        for candidate in (program, "armada")
        if (programs_root / candidate).exists()
    )
    base_schema_verdict = evaluate_base_schema_cross_program(base_schema_samples)
    base_schema_scaffold = {
        "schema_evaluator": _count_text(repo_root / "src" / "core" / "activation_readiness.py", "def evaluate_base_schema_cross_program") > 0,
        "usable_two_programs": base_schema_verdict.passed,
        "programs": [sample.program_id for sample in base_schema_samples],
        "reasons": list(base_schema_verdict.reasons),
    }
    vision_reconciliation_verdict = evaluate_cross_source_reconciliation(
        CrossSourceReconciliationPlan(
            source_nodes=tuple(
                source
                for source in ("ado", "eml", "kusto", "icm")
                if _count_text(vision_reconciliation_md, source) > 0
            ),
            has_materiality_policy=_count_text(vision_reconciliation_md, "Materiality") > 0
            or _count_text(vision_reconciliation_md, "materiality") > 0,
            carries_as_of=_count_text(vision_reconciliation_md, "as_of") > 0,
            has_disputed_output=_count_text(vision_reconciliation_md, "disputed") > 0,
            has_operator_adjudication_queue=_count_text(vision_reconciliation_md, "adjudication queue") > 0,
        )
    )
    explain_drilldown_verdict = evaluate_explain_drilldown(
        ExplainDrilldownPlan(
            has_source_excerpt=_count_text(vision_reconciliation_md, "source excerpt") > 0,
            has_counter_source_context=_count_text(vision_reconciliation_md, "counter-source context") > 0,
            has_lineage_keys=_count_text(vision_reconciliation_md, "source_document_key") > 0
            and _count_text(vision_reconciliation_md, "approval_event_id") > 0,
            has_operator_action=_count_text(vision_reconciliation_md, "accept, edit, reject, revoke, or defer") > 0,
            has_accessibility_review=_count_text(vision_reconciliation_md, "accessibility review") > 0,
        )
    )
    vision_reconciliation_scaffold = {
        "plan_present": vision_reconciliation_md.exists(),
        "reconciliation_evaluator": _count_text(repo_root / "src" / "core" / "activation_readiness.py", "def evaluate_cross_source_reconciliation") > 0,
        "four_source_nodes": vision_reconciliation_verdict.passed,
        "reasons": list(vision_reconciliation_verdict.reasons),
    }
    explain_drilldown_scaffold = {
        "plan_present": vision_reconciliation_md.exists(),
        "explain_evaluator": _count_text(repo_root / "src" / "core" / "activation_readiness.py", "def evaluate_explain_drilldown") > 0,
        "explain_contract": explain_drilldown_verdict.passed,
        "reasons": list(explain_drilldown_verdict.reasons),
    }
    binding_release_verdict = evaluate_binding_release_readiness(
        BindingReleaseReadiness(
            dirty_worktree=False,
            branch_local_evidence_count=0,
            committed_canonical_evidence=True,
            verifier_self_test_passed=True,
            generated_evidence_current=True,
            required_green_checks=("P0-VERIFIER-SELF-TEST", "P0-CONSOLIDATED-VERSION-PIN"),
            passing_check_ids=("P0-VERIFIER-SELF-TEST", "P0-CONSOLIDATED-VERSION-PIN"),
            failing_check_ids=("P-1-RAW-DATA",),
            allowed_red_blockers=("P-1-RAW-DATA",),
            release_owner="activation-release-owner",
        )
    )
    binding_release_scaffold = {
        "checklist_present": release_checklist_md.exists(),
        "readiness_evaluator": _count_text(repo_root / "src" / "core" / "activation_readiness.py", "def evaluate_binding_release_readiness") > 0,
        "branch_local_vs_canonical": _count_text(release_checklist_md, "branch-local") > 0
        and _count_text(release_checklist_md, "canonical") > 0,
        "dirty_worktree_rule": _count_text(release_checklist_md, "dirty_worktree") > 0
        and _count_text(release_checklist_md, "must be false") > 0,
        "self_test_required": _count_text(release_checklist_md, "--self-test") > 0,
        "real_blockers_named": _count_text(release_checklist_md, "P-1-RAW-DATA") > 0
        and _count_text(release_checklist_md, "AG-1-COUNTERFACTUAL-DIFF") > 0,
        "fail_closed_contract": binding_release_verdict.passed,
        "reasons": list(binding_release_verdict.reasons),
    }
    binding_release_scaffold_ready = all(
        value for key, value in binding_release_scaffold.items() if key != "reasons"
    )
    batch_reject_present = _count_text(ledger_py, '@triage_app.command("batch-reject")') > 0
    runbook_present = runbook_md.exists() and _count_text(runbook_md, "operator correction protocol") > 0
    authority_flip_scaffold = {
        "evaluate_family_flip_gate": _count_text(fact_sor_state_py, "def evaluate_family_flip_gate") > 0,
        "family_clean_cycle_counter": _count_text(fact_sor_state_py, "fact_store_family_cycles.yaml") > 0,
        "rollback_to_shadow": _count_text(fact_sor_state_py, "rolled_back_to_shadow") > 0,
        "sor_flip_policy": _count_text(source_authority_yaml, "sor_flip:") > 0
        and _count_text(source_authority_yaml, "clean_cycles_to_flip: 5") > 0,
        "contract_tests": _count_text(flip_gate_contract_py, "test_flip_occurs_after_threshold_clean_cycles") > 0
        and _count_text(flip_gate_contract_py, "test_primary_family_rolls_back_on_divergence") > 0,
    }
    rollback_proof = _latest_platform_proof(platform_proof_log, "s7a_rollback_drill")
    trusted_baseline_doc = _read_yaml_mapping(trusted_baseline)
    baseline_history = trusted_baseline_doc.get("history") if isinstance(trusted_baseline_doc.get("history"), list) else []
    trusted_baseline_drill = next(
        (
            row for row in reversed(baseline_history)
            if isinstance(row, Mapping) and row.get("action") == "rollback_drill_passed"
        ),
        None,
    )
    test_count = len(tuple((repo_root / "tests").rglob("test_*.py")))
    return (
        CheckResult(
            "PS-1-REPORT-REALITY-REFS",
            "pass" if report_refs > 0 else "fail",
            f"src/commands/report.py ProgramReality reference count is {report_refs}",
            {"count": report_refs},
        ),
        CheckResult(
            "P5-AUDITED-ROLLBACK-FLAG",
            "pass" if rollback_env_present and rollback_warning_present else "fail",
            "milestone read-path legacy rollback is explicit and audited"
            if rollback_env_present and rollback_warning_present
            else "milestone read-path legacy rollback flag/warning is missing",
            {"env_flag": rollback_env_present, "warning": rollback_warning_present},
        ),
        CheckResult(
            "P5-MILESTONE-LINEAGE-RENDER",
            "pass" if row_source_field_present and row_approval_field_present and template_source_present else "fail",
            "milestone render rows carry source_document_key and approval_event_id"
            if row_source_field_present and row_approval_field_present and template_source_present
            else "milestone render lineage fields/template are incomplete",
            {
                "row_source_document_key": row_source_field_present,
                "row_approval_event_id": row_approval_field_present,
                "template_source_document_key": template_source_present,
            },
        ),
        CheckResult(
            "P6-COUNTERFACTUAL-DIFF-ARTIFACT",
            "pass" if diff_artifact_present else "fail",
            "verifier can write a durable unified diff artifact with source and approval-id checks"
            if diff_artifact_present
            else "verifier does not expose durable counterfactual diff artifact output",
        ),
        CheckResult(
            "PS-2-BRIDGE-DEFAULT",
            "pass" if bridge_default else "fail",
            "fact bridge defaults on (fix-data-flow.md Track A step 2 / ADR-0011) — "
            "any REV-configured program that doesn't explicitly opt out now bridges by default"
            if bridge_default
            else "fact bridge default is not the expected default-on baseline (ADR-0011)",
            {"model_default_true": bridge_default, "env_enabled": bridge_env_set},
        ),
        CheckResult(
            "PS-3-LAST-CYCLE-SNAPSHOT",
            "info",
            "latest cycle snapshot captured",
            {
                "cycle_status": last_cycle.get("cycle_status"),
                "shield_degrade": last_cycle.get("shield_degrade"),
                "enumerated": last_cycle.get("enumerated"),
                "candidates_staged": last_cycle.get("candidates_staged"),
                "processed_successfully": last_cycle.get("processed_successfully"),
            },
        ),
        CheckResult(
            "PS-5-CORPUS-SNAPSHOT",
            "fail" if not _has_dual_labels(labels) else "pass",
            "labeled corpus has no dual labels" if not _has_dual_labels(labels) else "labeled corpus includes dual labels",
            {
                "rows": len(labels),
                "dual_labeled_rows": sum(1 for row in labels if _has_second_label(row)),
                "top_event_types": label_counts.most_common(10),
            },
        ),
        build_corpus_freeze_check(
            program=program,
            programs_root=programs_root,
            repo_root=repo_root,
        ),
        build_corpus_certification_check(
            labels=labels,
            quality_metrics=quality_metrics,
            keystone_ledger_event_type="milestone.completed.v1",
            data_floor=_DATA_SUFFICIENCY_FLOOR,
        ),
        CheckResult(
            "P2-QUALITY-METRICS-ARTIFACT",
            "pass" if quality_metrics_path.exists() else "fail",
            "rev_quality_metrics.json is published"
            if quality_metrics_path.exists()
            else "rev_quality_metrics.json has not been published",
            {
                "path": str(quality_metrics_path),
                "exists": quality_metrics_path.exists(),
                "g_xtract_prec": quality_metrics.get("g_xtract_prec"),
                "g_xtract_prec_ci_low": _ci_low(quality_metrics.get("g_xtract_prec_ci")),
                "g_accept_prec": quality_metrics.get("g_accept_prec"),
                "g_accept_prec_ci_low": _ci_low(quality_metrics.get("g_accept_prec_ci")),
                "kappa": quality_metrics.get("kappa"),
                "kappa_n": quality_metrics.get("kappa_n"),
            },
        ),
        CheckResult(
            "P2-DENOMINATOR-PLAN",
            "pass" if quality_metrics.get("activation_denominator_plan") else "fail",
            "Wilson 95% activation denominator plan is available"
            if quality_metrics.get("activation_denominator_plan")
            else "Wilson 95% activation denominator plan is missing",
            {"plan": quality_metrics.get("activation_denominator_plan")},
        ),
        CheckResult(
            "PS-22-TRIAGE-REVOKE",
            "pass" if triage_edit_present and triage_revoke_present and correction_event_present and candidate_revoked_event_present else "fail",
            "triage edit/revoke commands and correction/revocation events are present"
            if triage_edit_present and triage_revoke_present and correction_event_present and candidate_revoked_event_present
            else "triage edit/revoke or correction/revocation event wiring is incomplete",
            {
                "triage_edit": triage_edit_present,
                "triage_revoke": triage_revoke_present,
                "operator_correction": correction_event_present,
                "candidate_revoked": candidate_revoked_event_present,
            },
        ),
        CheckResult(
            "AG-9-CONFLICT-SCAFFOLD",
            "pass" if all(conflict_scaffold.values()) else "fail",
            "conflict detector, fact.conflict projection, and contract tests are available"
            if all(conflict_scaffold.values())
            else "conflict/disputed projection scaffold is incomplete",
            conflict_scaffold,
        ),
        CheckResult(
            # v1.24 added this row marking the detector AVAILABLE but NOT invoked
            # (awaiting wiring). v1.25 wired it: ``_run_cross_source_conflict_check``
            # in the REV pipeline calls ``detect_corroboration_and_conflicts`` over
            # the fact-store snapshot and writes the reshaped ``fact.conflict``/
            # ``fact.corroboration`` facts. The detector finds no conflict when
            # entity keys don't align (current state) — honest, not a gap.
            "AG-9-CONFLICT-WIRED",
            "pass" if conflict_wired else "fail",
            "conflict detector is invoked on the REV/candidate finalize path "
            "(_run_cross_source_conflict_check writes fact.conflict with as_of)"
            if conflict_wired
            else "conflict detector is available but not invoked on the REV path",
            {"conflict_wired": conflict_wired},
        ),
        CheckResult(
            "AG-11-PRIVACY-SCAFFOLD",
            "pass" if all(privacy_scaffold.values()) else "fail",
            "privacy scaffold includes pseudonymization, local fail-closed checks, "
            "contract coverage, and a wired projection gate on ACCEPTED facts"
            if all(privacy_scaffold.values())
            else "privacy scaffold is incomplete or not wired into projection",
            privacy_scaffold,
        ),
        CheckResult(
            "AG-12-DEGRADATION-SCAFFOLD",
            "pass" if all(degradation_scaffold.values()) else "fail",
            "REV pipeline records extraction_degraded cycles and source_unreachable provider-limited stops"
            if all(degradation_scaffold.values())
            else "REV degradation cycle scaffold is incomplete",
            degradation_scaffold,
        ),
        CheckResult(
            "AG-15-ENTITY-BINDING-SCAFFOLD",
            "pass" if all(entity_binding_scaffold.values()) else "fail",
            "entity binding precision/coverage gate, contract tests, and wired "
            "candidate entity_resolution are present"
            if all(entity_binding_scaffold.values())
            else "entity binding gate scaffold is incomplete or candidates carry empty entity_resolution",
            entity_binding_scaffold,
        ),
        CheckResult(
            "AG-17-OPERATOR-IDENTITY-SCAFFOLD",
            "pass" if all(operator_identity_scaffold.values()) else "fail",
            "operator identity attestation (principal+machine+session) is captured on triage writes"
            if all(operator_identity_scaffold.values())
            else "operator identity / forge-approval mitigation scaffold is incomplete",
            operator_identity_scaffold,
        ),
        CheckResult(
            "AG-17-PROVENANCE-SCAFFOLD",
            "pass" if all(provenance_scaffold.values()) else "fail",
            "sender-allowlist provenance gate mitigates forge-EML (content shields scan content, not provenance)"
            if all(provenance_scaffold.values())
            else "forge-EML provenance gate scaffold is incomplete",
            provenance_scaffold,
        ),
        CheckResult(
            "AG-17-INJECTION-FENCE-SCAFFOLD",
            "pass" if all(injection_fence_scaffold.values()) else "fail",
            "LLM extractor wraps untrusted EML in a per-call randomized delimiter fence"
            if all(injection_fence_scaffold.values())
            else "prompt-injection delimiter-fence scaffold is incomplete",
            injection_fence_scaffold,
        ),
        CheckResult(
            "AG-13-TRIAGE-TELEMETRY-SCAFFOLD",
            "pass" if all(telemetry_scaffold.values()) else "fail",
            "triage telemetry records time-to-triage + accept/reject/edit rates per decision"
            if all(telemetry_scaffold.values())
            else "triage telemetry / time-to-triage scaffold is incomplete",
            telemetry_scaffold,
        ),
        CheckResult(
            "AG-14-SLO-SCAFFOLD",
            "pass" if all(slo_scaffold.values()) else "fail",
            "activation SLO evaluator covers per-item, per-cycle, render, revoke, growth, TTL, and cost budgets"
            if all(slo_scaffold.values())
            else "activation SLO evaluator scaffold is incomplete",
            slo_scaffold,
        ),
        CheckResult(
            "AG-20-TIME-MOTION-ROI-SCAFFOLD",
            "pass" if all(roi_scaffold.values()) else "fail",
            "time-motion ROI evaluator compares manual export + triage against manual typing"
            if all(roi_scaffold.values())
            else "time-motion ROI evaluator scaffold is incomplete",
            roi_scaffold,
        ),
        CheckResult(
            "AG-7-AUTOMATION-HONESTY-ADR",
            "pass" if all(automation_honesty.values()) else "fail",
            "automation-honesty ADR documents manual EML export, automatic-after-deposit scope, Graph roadmap, and ROI inclusion"
            if all(automation_honesty.values())
            else "automation-honesty ADR / product wording is incomplete",
            automation_honesty,
        ),
        CheckResult(
            "O-21-EXPLAIN-MIN-SCAFFOLD",
            "pass" if all(explain_min_scaffold.values()) else "fail",
            "triage surfaces source excerpts/rationale plus source keys for EXPLAIN-min"
            if all(explain_min_scaffold.values())
            else "EXPLAIN-min triage scaffold is incomplete",
            explain_min_scaffold,
        ),
        CheckResult(
            "AG-19-MULTI-ALTITUDE-SCAFFOLD",
            "pass" if all(multi_altitude_scaffold.values()) else "fail",
            "deck altitude consumes ProgramReality milestone rows and renders source/approval lineage"
            if all(multi_altitude_scaffold.values())
            else "multi-altitude deck lineage scaffold is incomplete",
            multi_altitude_scaffold,
        ),
        CheckResult(
            "P7-OPERATOR-WORKFLOW-E2E-CONTRACT",
            "pass" if all(operator_workflow_contract.values()) else "fail",
            "operator workflow contract covers export -> REV -> triage edit/approve -> report -> revoke -> re-report"
            if all(operator_workflow_contract.values())
            else "operator workflow E2E contract is missing or incomplete",
            operator_workflow_contract,
        ),
        CheckResult(
            "AG-1B-ROBUSTNESS-SCAFFOLD",
            "pass" if all(ag1b_robustness_scaffold.values()) else "fail",
            "negative/recovery contracts cover duplicate EMLs, malformed fail-closed, rejected invisibility, crash recovery, and replay"
            if all(ag1b_robustness_scaffold.values())
            else "AG-1b robustness scaffold is incomplete",
            ag1b_robustness_scaffold,
        ),
        CheckResult(
            "AG-5-NCFL-APPLY-SCAFFOLD",
            "pass" if all(ncfl_scaffold.values()) else "fail",
            "NCFL apply engine has recoverable journal, optimistic concurrency, canonical save dispatch, and policy contracts"
            if all(ncfl_scaffold.values())
            else "NCFL apply scaffold is incomplete",
            ncfl_scaffold,
        ),
        CheckResult(
            "AG-6-REVERSE-LOOKUP-SCAFFOLD",
            "pass" if all(reverse_lookup_scaffold.values()) else "fail",
            "FactLineage preserves source/approval lookup and degrades explicitly for redaction, retention expiry, and access denial"
            if all(reverse_lookup_scaffold.values())
            else "AG-6 reverse-lookup scaffold is incomplete",
            reverse_lookup_scaffold,
        ),
        CheckResult(
            "AG-16-LONGITUDINAL-BENEFIT-SCAFFOLD",
            "pass" if all(longitudinal_benefit_scaffold.values()) else "fail",
            "longitudinal benefit evaluator measures auto-approved-signal rate and operator review-time trends"
            if all(longitudinal_benefit_scaffold.values())
            else "AG-16 longitudinal benefit scaffold is incomplete",
            longitudinal_benefit_scaffold,
        ),
        CheckResult(
            "P15-CORPUS-ROLLBACK-SCAFFOLD",
            "pass" if all(corpus_rollback_scaffold.values()) else "fail",
            "sustaining quality rollback evaluator triggers demotion on kappa/precision/recall floor breaches"
            if all(corpus_rollback_scaffold.values())
            else "corpus rollback scaffold is incomplete",
            corpus_rollback_scaffold,
        ),
        CheckResult(
            "AG-4-ACCESSOR-LADDER-SCAFFOLD",
            "pass" if all(accessor_ladder_scaffold.values()) else "fail",
            "accessor rollout plan counts shared accessors once and ties remaining-family flips to policy/gate scaffolds"
            if all(accessor_ladder_scaffold.values())
            else "accessor ladder scaffold is incomplete",
            accessor_ladder_scaffold,
        ),
        CheckResult(
            "AG-8-AG14-FLEET-SOAK-SCAFFOLD",
            "pass" if all(fleet_soak_scaffold.values()) else "fail",
            "fleet soak evaluator covers 3-program ProgramReality render, quiet lane, isolation, growth, cost, and concurrency"
            if all(fleet_soak_scaffold.values())
            else "fleet soak scaffold is incomplete",
            fleet_soak_scaffold,
        ),
        CheckResult(
            "P1-DATA-RESIDENCY-SCAFFOLD",
            "pass" if all(data_residency_scaffold.values()) else "fail",
            "data-residency evaluator is backed by governance privacy/data-classification artifacts"
            if all(data_residency_scaffold.values())
            else "data-residency scaffold is incomplete",
            data_residency_scaffold,
        ),
        CheckResult(
            "P13-OPERATOR-CLARITY-SCAFFOLD",
            "pass" if all(operator_clarity_scaffold.values()) else "fail",
            "operator-clarity evaluator and review template cover EXPLAIN-min, disputed badges, downgrade banners, and accessibility"
            if all(operator_clarity_scaffold.values())
            else "operator-clarity/accessibility scaffold is incomplete",
            operator_clarity_scaffold,
        ),
        CheckResult(
            "P-1-RAW-DATA-FEASIBILITY-PLAN",
            "pass" if all(raw_data_plan_scaffold.values()) else "fail",
            "raw-data feasibility report records current floor gap, owner/path, and fail-closed go/no-go rule"
            if all(raw_data_plan_scaffold.values())
            else "raw-data feasibility plan is incomplete",
            raw_data_plan_scaffold,
        ),
        CheckResult(
            "P2-ANNOTATION-STAFFING-PLAN",
            "pass" if all(annotation_staffing_scaffold.values()) else "fail",
            "annotation plan names second-label/adjudication roles and corpus quality targets"
            if all(annotation_staffing_scaffold.values())
            else "annotation staffing/adjudication plan is incomplete",
            annotation_staffing_scaffold,
        ),
        CheckResult(
            "RK-1-PILOT-DEGRADE-ADR",
            "pass" if all(pilot_degrade_exception_scaffold.values()) else "fail",
            "pilot degrade ADR is proof-only and explicitly blocks authority-cycle credit"
            if all(pilot_degrade_exception_scaffold.values())
            else "pilot degrade exception ADR is incomplete",
            pilot_degrade_exception_scaffold,
        ),
        CheckResult(
            "P11-BASE-SCHEMA-CROSS-PROGRAM",
            "pass" if base_schema_verdict.passed and base_schema_scaffold["schema_evaluator"] else "fail",
            "base schema dry-validates across nova and a second usable program without nova coupling"
            if base_schema_verdict.passed and base_schema_scaffold["schema_evaluator"]
            else "base-schema cross-program readiness check is incomplete or failing",
            base_schema_scaffold,
        ),
        CheckResult(
            "P15-KUSTO-ICM-RECONCILIATION-SCAFFOLD",
            "pass"
            if vision_reconciliation_verdict.passed and vision_reconciliation_scaffold["reconciliation_evaluator"]
            else "fail",
            "Bar-C reconciliation plan covers ADO, EML, Kusto, IcM, materiality, as_of, disputed output, and adjudication"
            if vision_reconciliation_verdict.passed and vision_reconciliation_scaffold["reconciliation_evaluator"]
            else "Kusto/IcM reconciliation scaffold is incomplete",
            vision_reconciliation_scaffold,
        ),
        CheckResult(
            "GAP-36-GAP-37-EXPLAIN-DRILLDOWN-SCAFFOLD",
            "pass"
            if explain_drilldown_verdict.passed and explain_drilldown_scaffold["explain_evaluator"]
            else "fail",
            "EXPLAIN drill-down plan covers source excerpt, counter-source context, lineage keys, operator action, and accessibility"
            if explain_drilldown_verdict.passed and explain_drilldown_scaffold["explain_evaluator"]
            else "EXPLAIN drill-down scaffold is incomplete",
            explain_drilldown_scaffold,
        ),
        CheckResult(
            "P0-BINDING-RELEASE-CHECKLIST-SCAFFOLD",
            "pass" if binding_release_scaffold_ready else "fail",
            "binding release checklist distinguishes branch-local evidence from committed canonical evidence and names hard blockers"
            if binding_release_scaffold_ready
            else "binding release checklist scaffold is incomplete",
            binding_release_scaffold,
        ),
        CheckResult(
            "O-21-LINEAGE-RICHNESS-SCAFFOLD",
            "pass" if all(lineage_richness_scaffold.values()) else "fail",
            "candidate/claim lineage carries prompt_version + extraction_rationale + thread_id"
            if all(lineage_richness_scaffold.values())
            else "extraction-provenance lineage richness scaffold is incomplete",
            lineage_richness_scaffold,
        ),
        CheckResult(
            "O-16-ADO-SCHEMA-DRIFT-SCAFFOLD",
            "pass" if all(ado_drift_scaffold.values()) else "fail",
            "ADO schema-drift guard fails closed on missing required fields + alerts on contract drift"
            if all(ado_drift_scaffold.values())
            else "ADO schema-drift guard scaffold is incomplete",
            ado_drift_scaffold,
        ),
        CheckResult(
            "AG-10-BATCH-REJECT-SCAFFOLD",
            "pass" if batch_reject_present else "fail",
            "triage batch-reject supports bulk judgment for backlog ROI"
            if batch_reject_present
            else "triage batch-reject command is missing",
            {"batch_reject_command": batch_reject_present},
        ),
        CheckResult(
            "P9-AUTHORITY-FLIP-SCAFFOLD",
            "pass" if all(authority_flip_scaffold.values()) else "fail",
            "authority flip gate reads sor_flip thresholds, counts clean cycles, and rolls back divergent primary families"
            if all(authority_flip_scaffold.values())
            else "authority flip gate / rollback scaffold is incomplete",
            authority_flip_scaffold,
        ),
        CheckResult(
            "AG-18-ROLLBACK-DRILL-EVIDENCE",
            "pass"
            if rollback_proof.get("status") == "passed" and trusted_baseline_drill is not None
            else "fail",
            "rollback drill proof is recorded in platform proof log and trusted baseline history"
            if rollback_proof.get("status") == "passed" and trusted_baseline_drill is not None
            else "rollback drill proof is missing or incomplete",
            {
                "platform_proof": rollback_proof or None,
                "trusted_baseline_action": trusted_baseline_drill or None,
            },
        ),
        CheckResult(
            "O-17-OPERATOR-RUNBOOK",
            "pass" if runbook_present else "fail",
            "operator/on-call runbook covers cycle-red scenarios + correction protocol"
            if runbook_present
            else "operator/on-call runbook governance artifact is missing",
            {"runbook_present": runbook_present},
        ),
        CheckResult(
            "P0-CONSOLIDATED-VERSION-PIN",
            "pass" if consolidated_version == "2.25" else "fail",
            f"archived consolidated.md version is {consolidated_version or 'missing'}",
            {"expected": "2.25", "actual": consolidated_version},
        ),
        CheckResult(
            "P0-TEST-SUITE-COUNT",
            "info",
            f"test suite contains {test_count} test files",
            {"test_file_count": test_count},
        ),
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def _read_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.exists():
        return ()
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return tuple(rows)


def _jsonl_row_count(path: Path) -> int:
    return len(_read_jsonl(path))


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_consolidated_version(path: Path) -> str | None:
    if not path.exists():
        return None
    match = re.search(r"^\*\*Version:\*\*\s*([0-9.]+)\s*$", path.read_text(encoding="utf-8", errors="replace"), re.MULTILINE)
    return match.group(1) if match else None


def _latest_platform_proof(path: Path, proof_id: str) -> dict[str, Any]:
    payload = _read_yaml_mapping(path)
    proofs = payload.get("proofs") if isinstance(payload.get("proofs"), list) else []
    for row in reversed(proofs):
        if isinstance(row, Mapping) and row.get("proof_id") == proof_id:
            return dict(row)
    return {}


def _count_text(path: Path, needle: str) -> int:
    if not path.exists():
        return 0
    return path.read_text(encoding="utf-8", errors="replace").count(needle)


def _has_second_label(row: Mapping[str, Any]) -> bool:
    return any(
        isinstance(row.get(key), str) and bool(str(row.get(key)).strip())
        for key in ("second_label", "second_annotator", "adjudicated_label")
    )


def _has_dual_labels(rows: Iterable[Mapping[str, Any]]) -> bool:
    return any(_has_second_label(row) for row in rows)


def _reachable_document_count(rows: Iterable[Mapping[str, Any]], claim_event_type: str) -> int:
    strata = _STRATA_BY_CLAIM_TYPE.get(claim_event_type, ())
    if not strata:
        return 0
    return sum(
        1
        for row in rows
        if any(stratum in {str(item) for item in row.get("dominant_strata", ())} for stratum in strata)
    )


def _program_has_eml(*, program: str, programs_root: Path) -> bool:
    inbox = programs_root / program / "rev_inbox" / "inbox"
    if inbox.exists() and any(inbox.glob("*.eml")):
        return True
    manifest = programs_root / program / "_quality" / "corpus_manifest.jsonl"
    return any(str(row.get("filename", "")).lower().endswith(".eml") for row in _read_jsonl(manifest))


def _write_temp_render(text: str, suffix: str) -> Path:
    """Write a render arm to a temp file for the counterfactual diff (--write-counterfactual-pair)."""
    import tempfile
    fd, path = tempfile.mkstemp(prefix=f"activation_{suffix}_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return Path(path)


def _git_metadata(repo_root: Path) -> tuple[str, bool]:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return sha or "unknown", bool(status)
    except OSError:
        return "unknown", True


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _ci_low(value: Any) -> float | None:
    if isinstance(value, (list, tuple)) and value:
        return _optional_float(value[0])
    return None


def _compact_details(details: Mapping[str, Any]) -> str:
    if not details:
        return ""
    encoded = json.dumps(details, sort_keys=True, default=str)
    if len(encoded) > 240:
        encoded = encoded[:237] + "..."
    return f"<br><sub>`{encoded}`</sub>"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program", default="nova")
    parser.add_argument("--programs-root", type=Path, default=_DEFAULT_PROGRAMS_ROOT)
    parser.add_argument("--keystone-family", default=_DEFAULT_KEYSTONE)
    parser.add_argument("--data-floor", type=int, default=_DATA_SUFFICIENCY_FLOOR)
    parser.add_argument("--with-fact-render", type=Path)
    parser.add_argument("--without-fact-render", type=Path)
    parser.add_argument("--source-document-key")
    parser.add_argument("--approval-event-id", help="Optional approval event id that must appear in the added render delta for AG-6 reverse lookup.")
    parser.add_argument("--counterfactual-diff", type=Path, help="Write a durable unified diff artifact for the supplied render pair.")
    parser.add_argument("--counterfactual-context-lines", type=int, default=3, help="Context lines to include in --counterfactual-diff output.")
    parser.add_argument("--write-corpus-freeze", action="store_true", help="Write programs/<id>/_quality/corpus_freeze.json for the current corpus inputs before verifying.")
    parser.add_argument("--write-counterfactual-pair", action="store_true",
                        help="Auto-generate the with/without-fact render pair from ProgramReality for --fact-id "
                             "and supply them to the counterfactual check (mirrors --write-corpus-freeze). "
                             "Requires the fact to carry source_document_key lineage (a real approved REV fact).")
    parser.add_argument("--fact-id",
                        help="The milestone.entry fact_id to suppress for --write-counterfactual-pair.")
    parser.add_argument("--json", type=Path, help="Write machine-readable report JSON.")
    parser.add_argument("--markdown", type=Path, help="Write generated Markdown evidence.")
    parser.add_argument("--self-test", action="store_true", help="Run only verifier self-tests.")
    args = parser.parse_args()

    if args.self_test:
        results = run_self_test()
        print(json.dumps([{"check_id": r.check_id, "status": r.status, "summary": r.summary, "details": r.details} for r in results], indent=2))
        return 1 if any(result.failed for result in results) else 0

    if args.write_corpus_freeze:
        write_corpus_freeze_manifest(
            program=args.program,
            programs_root=args.programs_root,
            repo_root=_REPO_ROOT,
        )

    if args.write_counterfactual_pair:
        # AG-1 (§6.13): auto-generate the with/without render pair by suppressing
        # one approved fact from the milestone section. Requires the fact to carry
        # source_document_key lineage (a real EML-derived approved fact).
        from src.commands.counterfactual_render import build_counterfactual_pair
        pair = build_counterfactual_pair(
            program_id=args.program,
            fact_id=args.fact_id or "",
            programs_root=args.programs_root,
        )
        if pair is not None and pair.differs and pair.source_document_key:
            # Supply the generated pair to the counterfactual check.
            args.with_fact_render = _write_temp_render(pair.with_fact_text, "with")
            args.without_fact_render = _write_temp_render(pair.without_fact_text, "without")
            args.source_document_key = args.source_document_key or pair.source_document_key
            if args.approval_event_id is None and pair.approval_event_id:
                args.approval_event_id = pair.approval_event_id
        else:
            print(
                f"WARNING: --write-counterfactual-pair could not generate an attributable "
                f"diff for fact_id={args.fact_id!r}. The fact must be an approved REV "
                f"milestone carrying source_document_key lineage. "
                f"(differs={pair.differs if pair else 'N/A'}, "
                f"source_key={pair.source_document_key if pair else 'N/A'})",
                file=sys.stderr,
            )

    report = build_activation_report(
        program=args.program,
        programs_root=args.programs_root,
        keystone_family=args.keystone_family,
        with_fact_path=args.with_fact_render,
        without_fact_path=args.without_fact_render,
        source_document_key=args.source_document_key,
        approval_event_id=args.approval_event_id,
        data_floor=args.data_floor,
    )
    payload = report.to_dict()
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    if args.markdown is not None:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(report), encoding="utf-8")
    if args.counterfactual_diff is not None:
        write_counterfactual_diff_artifact(
            output_path=args.counterfactual_diff,
            with_fact_path=args.with_fact_render,
            without_fact_path=args.without_fact_render,
            source_document_key=args.source_document_key,
            approval_event_id=args.approval_event_id,
            context_lines=args.counterfactual_context_lines,
        )
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
