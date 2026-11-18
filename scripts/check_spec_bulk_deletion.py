from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PROTECTED_PATHS = (
    "specs/vertex-prd.md",
    "specs/vertex-tech-spec.md",
    "specs/vertex-ux-spec.md",
)


def _git(*args: str, text: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=text,
        check=False,
    )


def _line_count(revision: str, path: str) -> int | None:
    result = _git("show", f"{revision}:{path}")
    if result.returncode != 0:
        return None
    return len(result.stdout.splitlines())


def main() -> int:
    parser = argparse.ArgumentParser(description="Reject destructive canonical-spec line-count changes.")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args()

    errors: list[str] = []
    base = _git("cat-file", "-e", args.base)
    head = _git("cat-file", "-e", args.head)
    if base.returncode != 0 or head.returncode != 0:
        raise SystemExit("The canonical-spec baseline or head revision is unavailable.")

    for path in PROTECTED_PATHS:
        before = _line_count(args.base, path)
        after = _line_count(args.head, path)
        if before is None:
            continue
        if after is None:
            errors.append(f"{path} existed at {args.base} but is absent at {args.head}.")
            continue
        minimum = (before + 1) // 2
        if after < minimum:
            errors.append(
                f"{path} dropped from {before} to {after} lines "
                f"(minimum {minimum}, >50% deletion)."
            )
    if errors:
        raise SystemExit("\n".join(errors))
    print("Canonical spec bulk-deletion guard OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
