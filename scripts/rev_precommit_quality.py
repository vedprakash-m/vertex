#!/usr/bin/env python3
"""REV quality-floor pre-commit gate (P2-9).

Wires ``scripts/rev_quality_check.py`` as a pre-commit hook that fires **only
when ``src/ai/rev/extractor.py`` or its prompt assets change**, runs the
G-floor regression gate against a labeled corpus, and blocks the commit on any
gated-metric failure. Target latency ≤ 30s warm cache (the gate is
measurement-only — it reads the staged candidate store + labeled corpus from
disk, never re-runs extraction — so a warm extraction cache (P2-12) makes
re-staging cheap and the gate itself sub-second).

Enable in a local clone:

    git config core.hooksPath .githooks

Then every commit that touches the extractor (or its prompt) runs:

    python scripts/rev_precommit_quality.py --program {program_id} \
        [--programs-root <path>] [--changed-files <path>]

The ``--changed-files`` file holds one staged path per line (the
``.githooks/pre-commit`` script writes ``git diff --cached --name-only`` there).
When changed files are passed programmatically (tests), supply them via
``main(argv, changed_files=[...])``.

Behaviour:
* no relevant file changed → skip (exit 0);
* relevant file changed but no labeled corpus present → advisory notice,
  exit 0 (the gate cannot run without operator-authored labels — never blocks
  on a fresh clone / pre-OA-3);
* relevant file changed + corpus present → run the gate; exit 1 on failure;
* elapsed > 30s → warn (regression target breach) but still exit on gate result.

All logic is unit-testable; the git-facing shell wrapper is ``.githooks/pre-commit``.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Allow running as a standalone script (scripts/ is outside the package root).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.core.config_loader import PROGRAMS_ROOT  # noqa: E402
from src.core.rev.quality_metrics import _corpus_path  # noqa: E402

# Files whose staged change should trigger the quality gate. The prompt version
# lives in registry.yaml; the prompt body in rev_extractor.v1.txt.
RELEVANT_PATHS: tuple[str, ...] = (
    "src/ai/rev/extractor.py",
    "src/ai/prompts/rev_extractor.v1.txt",
    "src/ai/prompts/registry.yaml",
)

LATENCY_TARGET_SECONDS = 30.0


def is_relevant_change(changed_files: list[str]) -> bool:
    """True iff any staged file is an extractor-relevant path.

    A directory-level change under ``src/ai/rev/`` (e.g. a rename) also counts
    so a refactor that moves the extractor still trips the gate.
    """
    normalized = {p.replace("\\", "/").strip() for p in changed_files if p and p.strip()}
    for rel in RELEVANT_PATHS:
        if rel in normalized:
            return True
    # Broad guard: any change under src/ai/rev/ that touches the extractor module
    # family (covers renames / moves the explicit list would miss).
    for p in normalized:
        if p.startswith("src/ai/rev/") and p.endswith(".py"):
            # Only the extractor + its direct deps (judge / verification) are
            # quality-relevant; be conservative and include the whole rev/ pkg
            # so a regression in a sibling module is caught.
            return True
    return False


def _corpus_exists(program_id: str, programs_root: Path) -> bool:
    return _corpus_path(program_id, programs_root).exists()


def main(
    argv: list[str] | None = None,
    *,
    changed_files: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="REV quality-floor pre-commit gate (P2-9).")
    parser.add_argument("--program", default=os.environ.get("VERTEX_REV_QC_PROGRAM", "nova"),
                        help="Program id whose labeled corpus to gate against (env VERTEX_REV_QC_PROGRAM; default nova).")
    parser.add_argument("--programs-root", default=str(PROGRAMS_ROOT),
                        help="Programs root directory.")
    parser.add_argument("--changed-files", default=None,
                        help="Path to a file listing staged paths (one per line). "
                             "Omitted in tests — pass changed_files=[...] instead.")
    args = parser.parse_args(argv)

    # Resolve the staged-file list.
    if changed_files is None:
        cf_path = args.changed_files
        if cf_path:
            try:
                changed_files = Path(cf_path).read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                print(f"rev_precommit_quality: cannot read --changed-files {cf_path}: {exc}",
                      file=sys.stderr)
                return 0  # do not block on a hook wiring error
        else:
            changed_files = []

    if not is_relevant_change(changed_files):
        # No extractor-relevant change → skip silently.
        return 0

    programs_root = Path(args.programs_root)
    if not _corpus_exists(args.program, programs_root):
        print(
            f"rev_precommit_quality: extractor changed but no labeled corpus at "
            f"{_corpus_path(args.program, programs_root)} — gate is advisory until "
            "OA-3 labels exist. Skipping (exit 0).",
            file=sys.stderr,
        )
        return 0

    # Delegate to the quality-check runner (P2-3) for the actual measurement.
    from scripts.rev_quality_check import main as qc_main
    start = time.perf_counter()
    rc = qc_main(["--program", args.program, "--programs-root", str(programs_root)])
    elapsed = time.perf_counter() - start
    if elapsed > LATENCY_TARGET_SECONDS:
        print(
            f"rev_precommit_quality: WARNING gate took {elapsed:.1f}s > "
            f"{LATENCY_TARGET_SECONDS:.0f}s target — investigate corpus size / "
            "candidate-store growth.",
            file=sys.stderr,
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())