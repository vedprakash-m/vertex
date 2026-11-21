from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Iterable
import xml.etree.ElementTree as ET


@dataclass(frozen=True, slots=True)
class TestCase:
    classname: str
    name: str
    outcome: str
    message: str = ""

    @property
    def base_name(self) -> str:
        return self.name.split("[", 1)[0]


@dataclass(slots=True)
class JunitReport:
    cases: list[TestCase] = field(default_factory=list)


@dataclass(slots=True)
class FileCoverage:
    statements: set[int]
    covered_statements: set[int]
    branches: set[tuple[int, int]]
    covered_branches: set[tuple[int, int]]


@dataclass(slots=True)
class CoverageResult:
    files: dict[str, FileCoverage]

    @property
    def statement_count(self) -> int:
        return sum(len(detail.statements) for detail in self.files.values())

    @property
    def covered_statement_count(self) -> int:
        return sum(len(detail.covered_statements) for detail in self.files.values())

    @property
    def branch_count(self) -> int:
        return sum(len(detail.branches) for detail in self.files.values())

    @property
    def covered_branch_count(self) -> int:
        return sum(len(detail.covered_branches) for detail in self.files.values())

    @property
    def statement_percent(self) -> float:
        if not self.statement_count:
            return 0.0
        return self.covered_statement_count * 100.0 / self.statement_count

    @property
    def branch_percent(self) -> float:
        if not self.branch_count:
            return 0.0
        return self.covered_branch_count * 100.0 / self.branch_count


def changed_executable_lines(base: str, paths: Iterable[str], coverage: CoverageResult) -> tuple[int, int]:
    result = subprocess.run(
        ["git", "diff", "--unified=0", f"{base}...HEAD", "--", *paths],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr or f"Unable to diff against {base}")
    current_file: str | None = None
    changed_lines: dict[str, set[int]] = {}
    for line in result.stdout.splitlines():
        if line.startswith("+++ b/"):
            current_file = line.removeprefix("+++ b/").replace("\\", "/")
            continue
        if current_file is None or not line.startswith("@@"):
            continue
        match = re.search(r"\+(\d+)(?:,(\d+))?", line)
        if match is None:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        changed_lines.setdefault(current_file, set()).update(range(start, start + count))
    executable = 0
    covered = 0
    for path, lines in changed_lines.items():
        detail = coverage.files.get(path)
        if detail is None:
            continue
        measured = lines & detail.statements
        executable += len(measured)
        covered += len(measured & detail.covered_statements)
    return covered, executable


def load_junit_reports(paths: Iterable[Path]) -> list[JunitReport]:
    reports: list[JunitReport] = []
    for path in paths:
        root = ET.parse(path).getroot()
        report = JunitReport()
        for case in root.iter("testcase"):
            outcome = "passed"
            message = ""
            for tag in ("failure", "error", "skipped"):
                detail = case.find(tag)
                if detail is not None:
                    outcome = tag
                    message = str(detail.attrib.get("message") or detail.text or "").strip()
                    break
            report.cases.append(
                TestCase(
                    classname=str(case.attrib.get("classname") or ""),
                    name=str(case.attrib.get("name") or ""),
                    outcome=outcome,
                    message=message,
                )
            )
        reports.append(report)
    return reports


def _node_matches(case: TestCase, node_id: str) -> bool:
    path, *parts = node_id.split("::")
    if not parts:
        return False
    module = ".".join(Path(path).with_suffix("").parts)
    if not (case.classname == module or case.classname.startswith(f"{module}.")):
        return False
    return case.base_name == parts[-1]


def missing_required_nodes(reports: Iterable[JunitReport], node_ids: Iterable[str]) -> list[str]:
    cases = [case for report in reports for case in report.cases if case.outcome == "passed"]
    return [node_id for node_id in node_ids if not any(_node_matches(case, node_id) for case in cases)]


def forbidden_skip_reasons(
    reports: Iterable[JunitReport],
    patterns: Iterable[str] = (
        "requires local",
        "private data",
        "programs/acme",
        "programs/fabrikam",
        "operator-local",
        "operator data",
        "gold corpus",
        "fixture data",
    ),
) -> list[str]:
    lowered = [pattern.casefold() for pattern in patterns]
    findings: list[str] = []
    for report in reports:
        for case in report.cases:
            if case.outcome != "skipped":
                continue
            if any(pattern in case.message.casefold() for pattern in lowered):
                findings.append(f"{case.classname}::{case.name}: {case.message}")
    return findings


def _branch_pairs(value: object) -> set[tuple[int, int]]:
    pairs: set[tuple[int, int]] = set()
    if not isinstance(value, list):
        return pairs
    for item in value:
        if (
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], int)
            and isinstance(item[1], int)
        ):
            pairs.add((item[0], item[1]))
    return pairs


def merge_coverage_json(paths: Iterable[Path]) -> CoverageResult:
    files: dict[str, FileCoverage] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        source_files = payload.get("files")
        if not isinstance(source_files, dict):
            raise ValueError(f"coverage report missing files object: {path}")
        for name, detail in source_files.items():
            if not isinstance(detail, dict):
                continue
            normalized = str(name).replace("\\", "/")
            statements = set(detail.get("executed_lines") or ()) | set(detail.get("missing_lines") or ())
            covered_statements = set(detail.get("executed_lines") or ())
            branches = _branch_pairs(detail.get("executed_branches")) | _branch_pairs(
                detail.get("missing_branches")
            )
            covered_branches = _branch_pairs(detail.get("executed_branches"))
            aggregate = files.setdefault(normalized, FileCoverage(set(), set(), set(), set()))
            aggregate.statements.update(statements)
            aggregate.covered_statements.update(covered_statements)
            aggregate.branches.update(branches)
            aggregate.covered_branches.update(covered_branches)
    if not files:
        raise ValueError("coverage reports contained no source files")
    return CoverageResult(files)


def load_coverage_baseline(
    path: Path,
    root: Path,
    *,
    expected_files: set[str],
) -> CoverageResult:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("coverage baseline schema_version must be 1")
    source_files = payload.get("files")
    source_hashes = payload.get("sources")
    if not isinstance(source_files, dict):
        raise ValueError("coverage baseline missing files object")
    if not isinstance(source_hashes, dict):
        raise ValueError("coverage baseline missing sources object")
    observed_sources = set()
    for name, expected_hash in source_hashes.items():
        normalized = str(name).replace("\\", "/")
        observed_sources.add(normalized)
        source_path = root / normalized
        if not source_path.is_file():
            raise ValueError(f"coverage baseline source is missing: {normalized}")
        normalized_source = source_path.read_bytes().replace(b"\r\n", b"\n")
        actual_hash = hashlib.sha256(normalized_source).hexdigest()
        if expected_hash != actual_hash:
            raise ValueError(
                f"coverage baseline invalid: normalized source bytes changed for {normalized}"
            )
    if observed_sources != expected_files:
        missing = sorted(expected_files - observed_sources)
        unexpected = sorted(observed_sources - expected_files)
        raise ValueError(
            "coverage baseline source inventory mismatch: "
            f"missing={missing}, unexpected={unexpected}"
        )
    files: dict[str, FileCoverage] = {}
    for name, detail in source_files.items():
        normalized = str(name).replace("\\", "/")
        if not isinstance(detail, dict):
            raise ValueError(f"coverage baseline detail must be an object: {normalized}")
        if normalized not in observed_sources:
            raise ValueError(
                f"coverage baseline has coverage for an unknown source: {normalized}"
            )
        statements = set(detail.get("executed_lines") or ()) | set(
            detail.get("missing_lines") or ()
        )
        covered_statements = set(detail.get("executed_lines") or ())
        branches = _branch_pairs(detail.get("executed_branches")) | _branch_pairs(
            detail.get("missing_branches")
        )
        covered_branches = _branch_pairs(detail.get("executed_branches"))
        files[normalized] = FileCoverage(
            statements, covered_statements, branches, covered_branches
        )
    missing_measurements = sorted(observed_sources - set(files))
    if missing_measurements:
        raise ValueError(
            "coverage baseline is missing measurement entries: "
            f"{missing_measurements}"
        )
    return CoverageResult(files)


def _expected_coverage_files(root: Path) -> set[str]:
    expected_files = {
        path.relative_to(root).as_posix()
        for path in (root / "src").rglob("*.py")
        if path.is_file()
    }
    cli_path = root / "cli.py"
    if cli_path.is_file():
        expected_files.add("cli.py")
    return expected_files


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate CI test and coverage evidence.")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--junit", type=Path, nargs="+", required=True)
    parser.add_argument("--coverage", type=Path, nargs="*")
    parser.add_argument("--coverage-baseline", type=Path)
    parser.add_argument("--base")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    reports = load_junit_reports(args.junit)
    expected_reports = (
        int(policy["functional_shards"])
        + int(policy["performance_reports"])
        + int(policy["windows_reports"])
    )
    if len(reports) != expected_reports:
        raise SystemExit(f"Expected {expected_reports} JUnit reports, observed {len(reports)}.")
    failures = [
        f"{case.classname}::{case.name}"
        for report in reports
        for case in report.cases
        if case.outcome in {"failure", "error"}
    ]
    if failures:
        raise SystemExit("JUnit evidence contains failures: " + ", ".join(failures))

    required = [entry["node"] for entry in policy["critical_obligations"]]
    required.extend(policy["performance_nodes"])
    required.extend(policy["windows_nodes"])
    required.extend(policy["control_nodes"])
    missing = missing_required_nodes(reports, required)
    if missing:
        raise SystemExit("Required tests did not pass: " + ", ".join(missing))

    forbidden_skips = forbidden_skip_reasons(reports, policy["forbidden_skip_patterns"])
    if forbidden_skips:
        raise SystemExit("Forbidden deterministic skips: " + "; ".join(forbidden_skips))

    expected_files = _expected_coverage_files(Path.cwd())
    if args.coverage_baseline:
        coverage = load_coverage_baseline(
            args.coverage_baseline,
            Path.cwd(),
            expected_files=expected_files,
        )
    elif args.coverage:
        coverage = merge_coverage_json(args.coverage)
        missing_measurements = sorted(expected_files - set(coverage.files))
        if missing_measurements:
            raise SystemExit(
                "Coverage evidence is missing measurement entries: "
                + ", ".join(missing_measurements)
            )
    else:
        raise SystemExit("Coverage evidence or a hash-bound coverage baseline is required.")
    statement_floor = float(policy["coverage"]["statement_floor"])
    branch_floor = float(policy["coverage"]["branch_floor"])
    if coverage.statement_percent + 1e-9 < statement_floor:
        raise SystemExit(
            f"statement coverage {coverage.statement_percent:.2f}% is below {statement_floor:.2f}%"
        )
    if coverage.branch_percent + 1e-9 < branch_floor:
        raise SystemExit(f"branch coverage {coverage.branch_percent:.2f}% is below {branch_floor:.2f}%")

    changed_message = "not measured"
    if args.base and not args.base.startswith("0" * 40):
        covered_changed, changed = changed_executable_lines(
            args.base, policy["coverage"]["sources"], coverage
        )
        if changed:
            changed_percent = covered_changed * 100.0 / changed
            changed_floor = float(policy["coverage"]["changed_code_statement_floor"])
            if changed_percent + 1e-9 < changed_floor:
                raise SystemExit(
                    f"changed-code statement coverage {changed_percent:.2f}% is below "
                    f"{changed_floor:.2f}%"
                )
            changed_message = f"{changed_percent:.2f}% of {changed} changed lines"
        else:
            changed_message = "no changed executable lines"

    print(
        "Test evidence OK: "
        f"{sum(len(report.cases) for report in reports)} cases, "
        f"{coverage.covered_statement_count}/{coverage.statement_count} statements "
        f"({coverage.statement_percent:.2f}%), "
        f"{coverage.covered_branch_count}/{coverage.branch_count} branches "
        f"({coverage.branch_percent:.2f}%), changed code: {changed_message}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
