"""ADF-W1.6: sequential Kusto benchmark for the ADF-W0.6 ratification decision.

Runs every edition-required Kusto query for a program SEQUENTIALLY (matching
the pre-ADF-W1.6, still-default ``max_concurrency=1`` behavior in
``src/core/kusto_hydration.py``) and records per-query and total wall-time
to a versioned artifact, so ADF-W0.6 can compare against the Section 8.3.3
candidate budget (<=180s total for the edition-required set) before
ratifying bounded parallelism.

This script queries a live Kusto cluster (read-only) when run without
``--executor-module``; running it against a real program requires live
Kusto credentials and is an operator action, not something exercised by the
test suite (``tests/unit/test_adf_kusto_bench.py`` uses a fake executor).

Usage::

    python scripts/adf_kusto_bench.py --program xpf
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.kusto_query_loader import load_kpi_queries
from src.core.kusto_templates import KustoTemplateContext, render_kusto_query
from src.core.models_v2 import KustoQuery

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCH_OUTPUT_DIR = REPO_ROOT / "governance" / "baselines"

#: Section 8.3.3 Phase-0 candidate: "<=180 seconds total for the edition-
#: required set." A candidate, not yet ratified by ADF-W0.6.
SECTION_8_3_3_TOTAL_BUDGET_SECONDS = 180


def bench_sequential(
    program_id: str,
    *,
    executor: Callable[[KustoQuery], list[dict[str, Any]]],
    programs_root: Path = PROGRAMS_ROOT,
    include_unvalidated: bool = False,
) -> dict[str, Any]:
    """Run every applicable query for *program_id* one at a time, timed."""
    queries = [
        query
        for query in load_kpi_queries(program_id, programs_root=programs_root)
        if query.engine == "kusto" and (include_unvalidated or query.validated)
    ]

    per_query: list[dict[str, Any]] = []
    total_started = time.monotonic()
    for query in queries:
        rendered = render_kusto_query(
            query,
            context=KustoTemplateContext(program_id=program_id, area_paths=(), date_window_days=None),
        )
        started = time.monotonic()
        status = "ok"
        row_count = 0
        error: str | None = None
        try:
            rows = executor(rendered)
            row_count = len(rows)
        except Exception as exc:  # noqa: BLE001 - a query failure is a benchmark data point, not a crash
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        per_query.append(
            {
                "query_id": query.id,
                "elapsed_ms": elapsed_ms,
                "status": status,
                "row_count": row_count,
                "error": error,
            }
        )
    total_elapsed_seconds = round(time.monotonic() - total_started, 3)

    return {
        "schema_version": "1",
        "program_id": program_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "mode": "sequential",
        "query_count": len(queries),
        "total_elapsed_seconds": total_elapsed_seconds,
        "section_8_3_3_candidate_budget_seconds": SECTION_8_3_3_TOTAL_BUDGET_SECONDS,
        "within_candidate_budget": total_elapsed_seconds <= SECTION_8_3_3_TOTAL_BUDGET_SECONDS,
        "queries": per_query,
    }


def write_bench_artifact(result: dict[str, Any], *, output_dir: Path = BENCH_OUTPUT_DIR) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"adf_kusto_bench_{result['program_id']}_{timestamp}.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _print_summary(result: dict[str, Any]) -> None:
    print(
        f"Sequential total: {result['total_elapsed_seconds']}s across {result['query_count']} quer"
        f"{'y' if result['query_count'] == 1 else 'ies'} "
        f"(Section 8.3.3 candidate budget {result['section_8_3_3_candidate_budget_seconds']}s; "
        f"within budget: {result['within_candidate_budget']})."
    )
    for row in result["queries"]:
        marker = "OK" if row["status"] == "ok" else "ERROR"
        suffix = f" -- {row['error']}" if row["error"] else ""
        print(f"  [{marker}] {row['query_id']}: {row['elapsed_ms']}ms ({row['row_count']} rows){suffix}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--program", required=True, help="Program id, e.g. xpf.")
    parser.add_argument("--programs-root", type=Path, default=PROGRAMS_ROOT)
    parser.add_argument("--include-unvalidated", action="store_true", help="Also benchmark unvalidated queries.")
    parser.add_argument(
        "--output-dir", type=Path, default=BENCH_OUTPUT_DIR, help="Where to write the bench artifact."
    )
    args = parser.parse_args(argv)

    from src.core.kusto_client import build_live_kusto_query_executor

    executor = build_live_kusto_query_executor()
    result = bench_sequential(
        args.program,
        executor=executor,
        programs_root=args.programs_root,
        include_unvalidated=args.include_unvalidated,
    )
    path = write_bench_artifact(result, output_dir=args.output_dir)
    print(f"Wrote {path}")
    _print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
