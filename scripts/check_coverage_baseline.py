from __future__ import annotations

import argparse
import json
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check src/core coverage against a committed baseline.")
    parser.add_argument("coverage_json", type=Path, help="Path to coverage.py JSON report.")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path("tests/coverage_baseline.txt"),
        help="Path to the committed baseline file.",
    )
    return parser.parse_args()


def _load_baseline(path: Path) -> float:
    payload: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        payload[key.strip()] = value.strip()
    try:
        return float(payload["src_core_percent"])
    except (KeyError, ValueError) as error:
        raise SystemExit(f"Invalid coverage baseline file: {path}") from error


def _load_current_core_percent(path: Path) -> float:
    payload = json.loads(path.read_text(encoding="utf-8"))
    files = payload.get("files")
    if not isinstance(files, dict):
        raise SystemExit(f"Invalid coverage JSON: {path}")

    total_statements = 0
    total_covered = 0
    for file_path, detail in files.items():
        if not isinstance(file_path, str) or not file_path.replace("\\", "/").startswith("src/core/"):
            continue
        if not isinstance(detail, dict):
            continue
        summary = detail.get("summary")
        if not isinstance(summary, dict):
            continue
        statements = summary.get("num_statements")
        missing = summary.get("missing_lines")
        if not isinstance(statements, int) or not isinstance(missing, int):
            continue
        total_statements += statements
        total_covered += statements - missing

    if total_statements == 0:
        raise SystemExit("Coverage JSON does not contain any src/core rows.")
    return round((total_covered * 100.0) / total_statements, 2)


def main() -> None:
    args = _parse_args()
    baseline = _load_baseline(args.baseline)
    current = _load_current_core_percent(args.coverage_json)
    if current < baseline:
        raise SystemExit(
            f"src/core coverage dropped below baseline: current={current:.2f}% baseline={baseline:.2f}%"
        )
    print(f"src/core coverage OK: current={current:.2f}% baseline={baseline:.2f}%")


if __name__ == "__main__":
    main()