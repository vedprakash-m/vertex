from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
import sys


ERROR_RE = re.compile(r"^(?P<file>.+?):(?P<line>\d+): error: (?P<msg>.+?)  \[(?P<code>[^\]]+)\]$")


@dataclass(frozen=True, slots=True)
class BucketRule:
    bucket: str
    reason: str


IGNORE_RULES: dict[tuple[str, int, str], BucketRule] = {
    (
        "src\\core\\models.py",
        19,
        "attr-defined",
    ): BucketRule(
        bucket="ignore_with_rationale",
        reason="EnumParserMixin iterates enum subclasses through the Enum metaclass; mypy does not model type[Self] iteration here.",
    ),
    (
        "src\\core\\models.py",
        28,
        "misc",
    ): BucketRule(
        bucket="ignore_with_rationale",
        reason="Enum member declarations make _default_member_name appear final to mypy even though this mixin pattern is intentional.",
    ),
    (
        "src\\core\\models.py",
        48,
        "misc",
    ): BucketRule(
        bucket="ignore_with_rationale",
        reason="Enum member declarations make _default_member_name appear final to mypy even though this mixin pattern is intentional.",
    ),
}


def classify_error(file_path: str, line_number: int, message: str, code: str) -> tuple[str, str]:
    rule = IGNORE_RULES.get((file_path, line_number, code))
    if rule is not None:
        return rule.bucket, rule.reason
    if code == "import-untyped" or "Library stubs not installed" in message:
        return "annotation_debt", "Third-party library is present without typed stubs."
    return "real_bug", "Type mismatch, unsafe narrowing, or incompatible API contract in repo code."


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: python scripts/triage_mypy_baseline.py <baseline.txt> <output.txt>")
        return 2

    baseline_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    if not baseline_path.exists():
        print(f"Baseline file not found: {baseline_path}")
        return 2

    bucket_counts: Counter[str] = Counter()
    code_counts: Counter[str] = Counter()
    samples: dict[str, list[str]] = {
        "real_bug": [],
        "annotation_debt": [],
        "ignore_with_rationale": [],
    }
    total_errors = 0

    with baseline_path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            match = ERROR_RE.match(raw_line.rstrip())
            if match is None:
                continue
            total_errors += 1
            file_path = match.group("file")
            line_number = int(match.group("line"))
            message = match.group("msg")
            code = match.group("code")
            bucket, rationale = classify_error(file_path, line_number, message, code)
            bucket_counts[bucket] += 1
            code_counts[code] += 1
            if len(samples[bucket]) < 5:
                samples[bucket].append(f"{file_path}:{line_number} [{code}] {message} -- {rationale}")

    lines = [
        f"real_bug: {bucket_counts['real_bug']}, annotation_debt: {bucket_counts['annotation_debt']}, ignore_with_rationale: {bucket_counts['ignore_with_rationale']}",
        f"total_errors: {total_errors}",
        "",
        "classification_policy:",
        "- real_bug = repo-local type mismatch, unsafe union/object handling, incompatible assignment/call, or invalid internal contract",
        "- annotation_debt = third-party import or library-stub gap",
        "- ignore_with_rationale = narrow, intentional patterns codified in mypy overrides with explicit rationale",
        "",
        "top_error_codes:",
    ]
    for code, count in code_counts.most_common(12):
        lines.append(f"- {code}: {count}")

    for bucket in ("real_bug", "annotation_debt", "ignore_with_rationale"):
        lines.extend(("", f"{bucket}_samples:"))
        if samples[bucket]:
            lines.extend(f"- {sample}" for sample in samples[bucket])
        else:
            lines.append("- none")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
