"""Read-only reducer for RELO-W6.3 tier telemetry and shadow enrollment."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.measurement_store import MeasurementReadError, read_measurements  # noqa: E402
from src.core.relo_w6 import build_w6_routing_evidence  # noqa: E402


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--window-start", type=_utc, required=True)
    parser.add_argument("--window-end", type=_utc, required=True)
    parser.add_argument("--eligible-routine-tasks", type=int, required=True)
    parser.add_argument("--completed-routine-tasks", type=int, required=True)
    parser.add_argument("--expected-tier-decisions", type=int, required=True)
    parser.add_argument("--ratified-local-coverage-floor", type=float, required=True)
    parser.add_argument("--ratified-routine-completion-floor", type=float, required=True)
    args = parser.parse_args(argv)
    try:
        records = read_measurements(args.store)
        result = build_w6_routing_evidence(
            list(records),
            window_start=args.window_start,
            window_end=args.window_end,
            eligible_routine_tasks=args.eligible_routine_tasks,
            completed_routine_tasks=args.completed_routine_tasks,
            expected_tier_decisions=args.expected_tier_decisions,
            ratified_local_coverage_floor=args.ratified_local_coverage_floor,
            ratified_routine_completion_floor=args.ratified_routine_completion_floor,
        )
    except (MeasurementReadError, OSError, ValueError) as error:
        print(json.dumps({
            "classification": "not_qualified",
            "failed": type(error).__name__,
            "detail": "Routing telemetry could not be admitted; inspect the selected store and enrollment inputs.",
        }, indent=2))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
