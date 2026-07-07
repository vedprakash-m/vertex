#!/usr/bin/env python3
"""REV quality-floor regression gate (P2-3 / P2-9 pre-commit hook target).

    python scripts/rev_quality_check.py --program {program_id} [--programs-root <path>] [--check-judge-independence] [--json] [--output <path>]

Computes the G-floor metrics (G-xtract-prec, G-accept-prec, G-reject-rate,
per-event-type recall with N≥5 guard, Cohen's kappa) from the operator-annotated
labeled corpus joined to the staged candidate store, prints a human report (or
JSON with --json), and exits 1 on any gated metric failure.

Pass --check-judge-independence to also enforce P2-11 (judge and extractor
deployments differ via VERTEX_AI_DEPLOYMENT / VERTEX_AI_JUDGE_DEPLOYMENT env).

This script is the regression gate wired as a pre-commit hook on
``src/ai/rev/extractor.py`` (P2-9). It delegates all logic to the Zone-A pure
``src.core.rev.quality_metrics`` module so it is unit-testable.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sys
from pathlib import Path

# Allow running as a standalone script (scripts/ is outside the package root).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.core.config_loader import PROGRAMS_ROOT  # noqa: E402
from src.core.jsonl_utils import append_jsonl_line  # noqa: E402
from src.core.rev.quality_metrics import (  # noqa: E402
    compute_quality_report,
    render_report_human,
    verify_judge_independence,
)

#: specs/backlog.md BL-A3 action item 2: persist each published run's
#: headline precision figures so a trend exists, not just a point reading.
#: Matches proposal_audit.jsonl's rotation cap (jsonl_utils.py convention).
_TREND_MAX_BYTES = 10 * 1024 * 1024


def _append_trend_record(output_path: Path, payload: dict, *, recorded_at: datetime | None = None) -> None:
    trend_path = output_path.parent / "rev_quality_trend.jsonl"
    record = {
        "recorded_at": (recorded_at or datetime.now(timezone.utc)).isoformat(),
        "program_id": payload.get("program_id"),
        "n_total": payload.get("n_total"),
        "g_xtract_prec": payload.get("g_xtract_prec"),
        "g_accept_prec": payload.get("g_accept_prec"),
        "g_reject_rate": payload.get("g_reject_rate"),
    }
    append_jsonl_line(trend_path, json.dumps(record, sort_keys=True) + "\n", max_bytes=_TREND_MAX_BYTES)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="REV quality-floor regression gate (P2-3).")
    parser.add_argument("--program", required=True, help="Program id.")
    parser.add_argument("--programs-root", default=str(PROGRAMS_ROOT),
                        help="Programs root directory.")
    parser.add_argument("--check-judge-independence", action="store_true",
                        help="Also enforce P2-11 (judge != extractor deployment).")
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    parser.add_argument("--output", type=Path, help="Write the report JSON to a file, e.g. programs/<id>/_quality/rev_quality_metrics.json.")
    args = parser.parse_args(argv)

    report = compute_quality_report(
        program_id=args.program,
        programs_root=Path(args.programs_root),
    )

    if args.check_judge_independence:
        ok, msg = verify_judge_independence()
        if not ok:
            report.failures.append(f"judge-independence: {msg}")
        report.warnings.append(f"judge-independence: {msg}")

    payload = report.to_dict()
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _append_trend_record(args.output, payload)

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_report_human(report))

    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
