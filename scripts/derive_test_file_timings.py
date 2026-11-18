from __future__ import annotations

import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPO_ROOT / "tests"


def _file_for_classname(classname: str) -> str | None:
    parts = classname.split(".")
    for length in range(len(parts), 0, -1):
        candidate = REPO_ROOT.joinpath(*parts[:length]).with_suffix(".py")
        if candidate.is_file() and candidate.is_relative_to(TESTS_ROOT):
            return candidate.relative_to(TESTS_ROOT).as_posix()
    return None


def derive_file_timings(junit_paths: list[Path]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for junit_path in junit_paths:
        root = ET.parse(junit_path).getroot()
        for case in root.iter("testcase"):
            classname = str(case.attrib.get("classname") or "")
            relative = _file_for_classname(classname)
            if relative is None:
                continue
            totals[relative] = totals.get(relative, 0.0) + float(case.attrib.get("time") or 0.0)
    return dict(sorted(totals.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Derive per-file test durations from JUnit XML.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("junit", type=Path, nargs="+")
    args = parser.parse_args()
    timings = derive_file_timings(args.junit)
    payload = {
        "schema_version": 1,
        "source": "JUnit XML from the measured full functional selection",
        "files": {name: {"seconds": round(seconds, 3)} for name, seconds in timings.items()},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote timings for {len(timings)} test files to {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
