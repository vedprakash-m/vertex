from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PATH_PATTERN = re.compile(
    r"(?P<path>(?:\.archive|docs|specs|src|templates|programs|knowledge|editions|scripts|tests|archive|output|\.github)"
    r"(?:/[A-Za-z0-9._<>-]+)+(?:\.[A-Za-z0-9._-]+)?/?|README\.md|cli\.py|pyproject\.toml|vertex\.py)"
)
SKIP_MARKERS = ("<", "*", "{", "}", "[", "]")
VALIDATED_PREFIXES = (
    ".github/",
    ".archive/specs/",
    "docs/adrs/",
    "docs/contributing.md",
    "docs/runbook.md",
    "editions/",
    "knowledge/",
    "scripts/",
    "specs/backlog.md",
    "specs/vertex-",
    "src/",
    "templates/",
)
VALIDATED_TOP_LEVEL = {"README.md", "cli.py", "pyproject.toml", "vertex.py"}
VALIDATED_EXTENSIONS = {
    ".md",
    ".py",
    ".toml",
    ".yaml",
    ".yml",
    ".j2",
    ".json",
    ".jsonl",
    ".html",
    ".eml",
    ".sqlite3",
}


def _iter_candidates(text: str) -> set[str]:
    candidates: set[str] = set()
    for match in PATH_PATTERN.finditer(text):
        candidate = match.group("path").rstrip(").,;:")
        if any(marker in candidate for marker in SKIP_MARKERS):
            continue
        candidates.add(candidate)
    return candidates


def _validate_path(raw_path: str) -> str | None:
    normalized = raw_path[:-1] if raw_path.endswith("/") else raw_path
    if not normalized:
        return None
    if normalized not in VALIDATED_TOP_LEVEL and not normalized.startswith(VALIDATED_PREFIXES):
        return None
    if normalized not in VALIDATED_TOP_LEVEL and Path(normalized).suffix.lower() not in VALIDATED_EXTENSIONS:
        return None
    absolute = REPO_ROOT / Path(normalized)
    if absolute.exists():
        return None
    return normalized


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate that concrete file and directory citations resolve in repo docs.")
    parser.add_argument(
        "paths",
        nargs="+",
        help="Markdown or text files to scan relative to the repo root.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    missing: list[tuple[str, str]] = []
    for raw_file in args.paths:
        file_path = REPO_ROOT / raw_file
        text = file_path.read_text(encoding="utf-8")
        for candidate in sorted(_iter_candidates(text)):
            unresolved = _validate_path(candidate)
            if unresolved is not None:
                missing.append((raw_file, unresolved))

    if missing:
        for owner_file, unresolved in missing:
            print(f"Unresolved citation in {owner_file}: {unresolved}", file=sys.stderr)
        return 1

    for raw_file in args.paths:
        print(f"Citation check passed: {raw_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
