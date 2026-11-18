from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
SKIP_PATHS = {
    "scripts/check_publication_content.py",
    "scripts/scrub_internal_names.py",
    "tests/unit/test_publication_content_scan.py",
    "tests/unit/test_ai_trace_sanitizer.py",
}
ALLOWED_VALUES = (
    "synthetic@microsoft.com",
    "AccountName=x;AccountKey=abcdEFGH1234567890+/=",
    "123-45-6789",
    "555-555-5555",
    "(206) 555-0101",
    "998877665544332211",
)
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAccountKey=[A-Za-z0-9+/=]{24,}"),
    re.compile(r"\bDefaultEndpointsProtocol=https?;AccountName=[^;]+;AccountKey=[A-Za-z0-9+/=]{24,}"),
)
PII_PATTERNS = (
    re.compile(
        r"\b[A-Za-z0-9._%+-]+@"
        r"(?!(?:[A-Za-z0-9-]+\.)*example\.(?:com|net|org|test|invalid)\b|contoso\.com\b|"
        r"users\.noreply\.github\.com\b|pytest\.)"
        r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    ),
    re.compile(r"\b(?:\+1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"[A-Za-z]:\\\\Users\\\\[^\\\\\\s]+"),
    re.compile(r"(?:packagefeedproxy\.microsoft\.io|(?:ms|ms-feed)-feed-[^/]+\.pkgs\.visualstudio\.com)"),
)
PRIVATE_NAME_PATTERN = re.compile(
    r"\b(?:XPF|xpf|Armada|armada|DDPF|ddpf|KRDMA|krdma|Pilotfish|pilotfish|XStore|xstore)\b"
)
BINARY_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".pdf",
    ".docx",
    ".xlsx",
    ".zip",
    ".whl",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pyc",
}


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _changed_paths(revision_range: str | None, staged: bool) -> list[str]:
    if revision_range:
        result = _git("diff", "--name-only", "--diff-filter=ACMR", revision_range)
    elif staged:
        result = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    else:
        tracked = _git("ls-files")
        untracked = _git("ls-files", "--others", "--exclude-standard")
        return sorted(set(tracked.stdout.splitlines()) | set(untracked.stdout.splitlines()))
    if result.returncode != 0:
        raise SystemExit(result.stderr or "Unable to derive changed paths.")
    return result.stdout.splitlines()


def _added_lines(path: str, revision_range: str | None, staged: bool) -> list[str]:
    if revision_range:
        result = _git("diff", "--unified=0", revision_range, "--", path)
    elif staged:
        result = _git("diff", "--cached", "--unified=0", "--", path)
    else:
        target = REPO_ROOT / path
        if not target.is_file():
            return []
        try:
            return target.read_text(encoding="utf-8", errors="strict").splitlines()
        except UnicodeError:
            return []
    if result.returncode != 0:
        raise SystemExit(result.stderr or f"Unable to inspect {path}.")
    return [
        line[1:]
        for line in result.stdout.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def scan_lines(path: str, lines: Iterable[str]) -> list[str]:
    if path in SKIP_PATHS or Path(path).suffix.lower() in BINARY_SUFFIXES:
        return []
    findings: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        if any(value in line for value in ALLOWED_VALUES):
            continue
        if PRIVATE_NAME_PATTERN.search(line):
            findings.append(f"{path}:{line_number}: private program name")
        for pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(f"{path}:{line_number}: potential secret")
        for pattern in PII_PATTERNS:
            if pattern.search(line):
                findings.append(f"{path}:{line_number}: potential private/PII value")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan publication candidates for secrets and private data.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--range", dest="revision_range")
    source.add_argument("--staged", action="store_true")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()

    paths = args.paths or _changed_paths(args.revision_range, args.staged)
    findings: list[str] = []
    for path in paths:
        if path in SKIP_PATHS:
            continue
        findings.extend(scan_lines(path, _added_lines(path, args.revision_range, args.staged)))
    if findings:
        raise SystemExit("\n".join(sorted(set(findings))))
    print(f"Publication content scan OK: {len(paths)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
