from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from src.core.vertical_artifact_evaluation import (
    evaluate_vertical_artifact_protocol,
    vertical_artifact_report_to_json,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate deterministic program vertical artifact comparisons. Missing D9, "
            "owner, blind, live, or real-program evidence fails qualification."
        )
    )
    parser.add_argument("--protocol", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = json.loads(args.protocol.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("protocol must be a JSON object")
    report = evaluate_vertical_artifact_protocol(payload)
    print(json.dumps(vertical_artifact_report_to_json(report), indent=2, sort_keys=True))
    return 0 if report.qualification_status.value == "qualified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
