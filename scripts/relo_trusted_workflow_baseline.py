"""Capture an operator-selected, benchmark-only baseline for trusted workflows.

This tool deliberately records only aggregate metadata from the existing
newsletter and nudge workflows.  It is a comparison point for improvement,
not a certification result, an approved budget, or a claim that a message was
received or acted upon.

``--verify`` is read-only.  ``--capture`` writes a timestamped artifact under
the explicit ``--baseline-dir`` and never changes program evidence. Both
modes require ``--program`` and ``--programs-root``. Review collected metadata
before sharing it; benchmark selection does not establish publication consent.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_SCHEMA_VERSION = "1"


def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _newsletter_baseline(program_id: str, programs_root: Path) -> dict[str, Any]:
    """Return non-identifying newsletter baseline fields, preserving gaps."""
    from src.core.trusted_baseline_store import load_trusted_baseline_for_program
    from src.core.yaml_utils import load_yaml_mapping

    manual_path = programs_root / program_id / "baseline.yaml"
    manual: dict[str, Any] = {}
    if manual_path.exists():
        raw = load_yaml_mapping(manual_path, required=True)
        summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
        validation = raw.get("baseline_validation") if isinstance(raw.get("baseline_validation"), dict) else {}
        manual = {
            "status": raw.get("status"),
            "recorded_on": raw.get("recorded_on"),
            "sample_size_completed": validation.get("sample_size_completed"),
            "sample_size_target": validation.get("sample_size_target"),
            "summary": {
                key: summary.get(key)
                for key in (
                    "mean_issue_cycle_time_minutes",
                    "median_issue_cycle_time_minutes",
                    "mean_author_active_minutes",
                    "mean_ado_data_staleness_minutes_at_publish",
                    "mean_dri_feedback_turnaround_minutes",
                    "ready_to_commit_l1_targets",
                )
            },
        }

    trusted = load_trusted_baseline_for_program(program_id, programs_root=programs_root)
    trusted_summary = None
    if trusted is not None:
        notes_conflict = _trusted_issue_conflicted_by_notes(trusted.notes, trusted.trusted_issue_number)
        trusted_state = (
            "trusted_issue_requires_reconciliation"
            if notes_conflict
            else ("trusted_issue_available" if trusted.trusted_issue_number is not None else "onboarding_no_trusted_issue")
        )
        trusted_summary = {
            "edition": trusted.edition,
            "trusted_issue_number": trusted.trusted_issue_number,
            "established_at": trusted.established_at.isoformat(),
            "bridge_graduated": trusted.bridge_graduated,
            "graduation_issue": trusted.graduation_issue,
            "locked_issue_count": len(trusted.locked_issues),
            "has_last_untrusted_issue": trusted.last_untrusted is not None,
            "state": trusted_state,
            "eligible_for_improvement_comparison": trusted.trusted_issue_number is not None and not notes_conflict,
        }
    return {"manual_baseline": manual or None, "trusted_baseline": trusted_summary}


def _trusted_issue_conflicted_by_notes(notes: str | None, trusted_issue_number: int | None) -> bool:
    """Detect a direct test-generated-range contradiction without copying notes.

    A trust pointer must not silently outrank a note saying that the pointed
    issue is test-generated.  This deliberately recognizes only an explicit
    ``test-generated ... N-M`` range, leaving ordinary editorial notes to
    the owner rather than guessing at their meaning.
    """
    if not notes or trusted_issue_number is None:
        return False
    normalized = notes.replace("–", "-").lower()
    for match in re.finditer(r"test-generated[^0-9]{0,40}(\d+)\s*-\s*(\d+)", normalized):
        lower, upper = int(match.group(1)), int(match.group(2))
        if lower <= trusted_issue_number <= upper:
            return True
    return False


def _nudge_baseline(program_id: str, programs_root: Path) -> dict[str, Any]:
    """Summarize nudge evidence without reading message bodies or recipients."""
    from src.core.edition_resolver import get_nudge_paths
    from src.core.nudge_state_store import load_nudge_state

    paths = get_nudge_paths(program_id, programs_root=programs_root)
    state = load_nudge_state(paths.state_path)
    origin_counts: dict[str, int] = {}
    for entry in state:
        origin = entry.origin or "unknown"
        origin_counts[origin] = origin_counts.get(origin, 0) + 1

    indexed_sent_count: int | None = None
    index_error: str | None = None
    if paths.published_eml_index_path.exists():
        try:
            index = json.loads(paths.published_eml_index_path.read_text(encoding="utf-8"))
            indexed_sent_count = len(index) if isinstance(index, list) else None
            if indexed_sent_count is None:
                index_error = "published EML index is not a list"
        except (OSError, json.JSONDecodeError) as exc:
            index_error = f"{type(exc).__name__}: {exc}"

    audit_record_count = 0
    invalid_audit_line_count = 0
    if paths.audit_path.exists():
        for line in paths.audit_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                json.loads(line)
                audit_record_count += 1
            except json.JSONDecodeError:
                invalid_audit_line_count += 1

    return {
        "cooldown_state_item_count": len(state),
        "cooldown_state_by_origin": dict(sorted(origin_counts.items())),
        "nudge_audit_record_count": audit_record_count,
        "invalid_nudge_audit_line_count": invalid_audit_line_count,
        "published_eml_artifact_count": len(list(paths.published_eml_dir.glob("*.eml"))) if paths.published_eml_dir.exists() else 0,
        "published_eml_indexed_count": indexed_sent_count,
        "published_eml_index_error": index_error,
        "interpretation": "Local published-EML and audit artifacts are workflow evidence, not proof of recipient delivery, reading, or action.",
    }


def _outcomes(program_id: str, programs_root: Path) -> list[dict[str, Any]]:
    from src.core.outcome_metrics import compute_all_outcome_metrics

    return [
        {
            "metric_id": metric.metric_id,
            "value": metric.value,
            "unit": metric.unit,
            "confidence": metric.confidence.value,
            "detail": metric.detail,
            "evidence_ref_count": len(metric.evidence_refs),
        }
        for metric in compute_all_outcome_metrics(program_id, programs_root=programs_root)
    ]


def collect_program_benchmark(program_id: str, *, programs_root: Path) -> dict[str, Any]:
    """Collect all safe workflow aggregates for one program without mutation."""
    from src.core.adf_workflow_metrics import compute_workflow_measurement_report

    workflow = compute_workflow_measurement_report(program_id, programs_root=programs_root).to_dict()
    return {
        "program_id": program_id,
        "newsletter": _newsletter_baseline(program_id, programs_root),
        "nudge": _nudge_baseline(program_id, programs_root),
        "proposal_review_workflow": workflow,
        "outcome_metrics": _outcomes(program_id, programs_root),
    }


def build_artifact(*, program_ids: tuple[str, ...], programs_root: Path, captured_at: datetime | None = None) -> dict[str, Any]:
    if not program_ids:
        raise ValueError("At least one explicit program is required.")
    when = captured_at or datetime.now(timezone.utc)
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "classification": "benchmark_only",
        "captured_at": when.isoformat(),
        "git_sha": _git_sha(),
        "authority": "none; operator-selected benchmark inputs only",
        "interpretation": (
            "Existing trusted newsletter and nudge workflows are a reproducible improvement benchmark only. "
            "This artifact neither ratifies a budget nor certifies an operational outcome."
        ),
        "programs": [collect_program_benchmark(program_id, programs_root=programs_root) for program_id in program_ids],
    }


def _artifact_path(out_dir: Path, when: datetime) -> Path:
    return out_dir / f"relo_trusted_workflow_baseline_{when.strftime('%Y%m%dT%H%M%SZ')}.json"


def _newest_artifact(baseline_dir: Path) -> Path | None:
    artifacts = sorted(baseline_dir.glob("relo_trusted_workflow_baseline_*.json")) if baseline_dir.exists() else []
    return artifacts[-1] if artifacts else None


def capture(*, program_ids: tuple[str, ...], programs_root: Path, out_dir: Path) -> Path:
    now = datetime.now(timezone.utc)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = _artifact_path(out_dir, now)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite an existing baseline artifact: {path}")
    path.write_text(json.dumps(build_artifact(program_ids=program_ids, programs_root=programs_root, captured_at=now), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def verify(*, program_ids: tuple[str, ...], programs_root: Path, baseline_dir: Path) -> bool:
    """Recollect evidence read-only; show drift from the latest captured artifact."""
    current = build_artifact(program_ids=program_ids, programs_root=programs_root)
    prior_path = _newest_artifact(baseline_dir)
    if prior_path is None:
        print("RELO trusted workflow benchmark verify: no captured artifact; comparison unavailable.")
        return False
    try:
        prior = json.loads(prior_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"RELO trusted workflow benchmark verify: invalid prior artifact {prior_path}: {exc}")
        return False

    prior_programs = {row["program_id"]: row for row in prior.get("programs", []) if isinstance(row, dict) and "program_id" in row}
    changed = [row["program_id"] for row in current["programs"] if prior_programs.get(row["program_id"]) != row]
    marker = "DRIFT" if changed else "MATCH"
    print(f"RELO trusted workflow benchmark verify: {marker}; artifact={prior_path.name}; changed_programs={changed}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program", action="append", dest="programs", required=True, help="Explicit program id to include; repeatable.")
    parser.add_argument("--programs-root", type=Path, required=True, help="Explicit root directory containing program folders.")
    parser.add_argument("--baseline-dir", type=Path, required=True, help="Explicit artifact directory for capture or comparison.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--capture", action="store_true", help="Write a timestamped benchmark artifact.")
    mode.add_argument("--verify", action="store_true", help="Recollect evidence read-only (default).")
    args = parser.parse_args(argv)
    program_ids = tuple(args.programs)
    programs_root = args.programs_root
    if args.capture:
        path = capture(program_ids=program_ids, programs_root=programs_root, out_dir=args.baseline_dir)
        print(f"Wrote benchmark-only artifact: {path}")
        return 0
    return 0 if verify(program_ids=program_ids, programs_root=programs_root, baseline_dir=args.baseline_dir) else 1


if __name__ == "__main__":
    sys.exit(main())
