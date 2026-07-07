"""WS-13 PB-34: bootstrap a test program on a fresh clone.

A fresh clone has no `programs/<program>/` data; the spec §0.3 recipe requires
that the test suite passes on a clean checkout. This script seeds a test
program under a caller-specified `programs_root` (default: the repo's
`programs/` dir) by copying the tracked template at
`programs/_templates/example_tpm/`. CI runs this before `pytest` so the test
suite has a known program to load.

The filename is intentionally `setup_test_program.py`, not
`bootstrap_test_program.py` — `.gitignore:99` ignores `scripts/bootstrap_*.py`
(the spec's §0.4 step 4 calls this out: new scripts in a globbed prefix must
be renamed or `git add -f`'d; renaming is preferred so the file is
auto-tracked on a normal `git add scripts/`).

Usage:
    python scripts/setup_test_program.py \\
        --template example_tpm \\
        --program acme \\
        --programs-root programs

It is idempotent: a pre-existing `programs/<program>/` is left alone unless
--force is passed. A marker file (`programs/<program>/.bootstrapped`) is
written to record the bootstrap, so subsequent runs short-circuit.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _copy_tree(src: Path, dst: Path) -> int:
    """Copy `src` (a directory) to `dst`. Returns the number of files copied.
    Refuses to overwrite existing files unless the caller has already
    removed `dst`."""
    if not src.exists() or not src.is_dir():
        raise FileNotFoundError(f"Template not found: {src}")
    if dst.exists():
        raise FileExistsError(
            f"Destination already exists: {dst}. Pass --force to overwrite."
        )
    shutil.copytree(src, dst)
    return sum(1 for _ in dst.rglob("*") if _.is_file())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "WS-13 bootstrap")
    parser.add_argument("--template", default="example_tpm", help="Template name under programs/_templates/")
    parser.add_argument("--program", default="acme", help="Program id to materialize")
    parser.add_argument(
        "--programs-root",
        default=str(REPO_ROOT / "programs"),
        help="Destination programs root",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing programs/<program>/",
    )
    parser.add_argument(
        "--bootstrap-marker",
        default=".bootstrapped",
        help="Marker filename written to the new program dir to record the bootstrap",
    )
    args = parser.parse_args(argv)

    template_dir = Path(args.programs_root) / "_templates" / args.template
    target_dir = Path(args.programs_root) / args.program
    marker = target_dir / args.bootstrap_marker

    if marker.exists() and not args.force:
        print(f"OK: {target_dir} already bootstrapped (marker: {args.bootstrap_marker}).")
        return 0

    if target_dir.exists() and not args.force:
        # Do NOT overwrite a real program without --force.
        if any(target_dir.iterdir()):
            print(
                f"REFUSE: {target_dir} is non-empty and --force not set. Use --force to overwrite.",
                flush=True,
            )
            return 2

    if target_dir.exists() and args.force:
        shutil.rmtree(target_dir)

    file_count = _copy_tree(template_dir, target_dir)
    marker.write_text(
        f"bootstrapped from _templates/{args.template} by scripts/setup_test_program.py\n",
        encoding="utf-8",
    )
    print(f"OK: bootstrapped {target_dir} from {template_dir} ({file_count} files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
