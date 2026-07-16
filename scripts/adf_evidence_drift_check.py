"""ADF-W0.13: automated evidence-drift check for frozen corpus artifacts.

Wraps the existing Track-C corpus-freeze check
(``scripts/verify_activation.py::build_corpus_freeze_check``) as a
CI-enforceable, read-only drift check. On drift it prints the exact
regeneration command instead of a generic failure message.

This script never mutates program state: freezing a corpus is an explicit
operator action (``verify_activation.py --write-corpus-freeze``) and is not
performed here. Three distinct outcomes are reported and exit differently:

- ``never_frozen`` -- no ``_quality/corpus_freeze.json`` exists yet for the
  program. This is not drift (nothing to compare against); exit 0.
- ``drift`` -- a freeze manifest exists but no longer matches the current
  corpus inputs/commit SHA; exit 1 with the regeneration command.
- ``clean`` -- the freeze manifest matches; exit 0.

Counterfactual-artifact freeze-checking (the other half of ADF-W0.13's
"corpus/counterfactual artifacts" scope) reuses the same three-outcome
convention via ``verify_activation.py::build_counterfactual_freeze_check``:
pins the SHA-256 of the AG-1 counterfactual diff artifact regenerated from
a frozen with-fact/without-fact render pair, so a code-behavior regression
in diff generation itself is detected (distinct from the inputs changing).
This half is opt-in per invocation (``--counterfactual-freeze-check`` with
the same ``--with-fact-render``/``--without-fact-render``/
``--source-document-key`` flags ``verify_activation.py --counterfactual-diff``
already takes) because, unlike the corpus, there is no single fixed
counterfactual artifact per program -- each is scoped to one specific
with/without-fact render pair an operator names explicitly.

Usage::

    python scripts/adf_evidence_drift_check.py --program nova
    python scripts/adf_evidence_drift_check.py --program nova \\
        --counterfactual-freeze-check --with-fact-render <path> \\
        --without-fact-render <path> --source-document-key <key>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# When this file runs as a direct script (``python scripts/adf_evidence_drift_check.py``),
# Python sets sys.path[0] to this file's own directory (scripts/), which breaks a
# package-qualified ``scripts.verify_activation`` import (it would look for a nested
# scripts/scripts/ package). Insert the repo root ahead of it so the import resolves
# the same way under both direct execution and pytest's package-style import.
_REPO_ROOT_FOR_IMPORT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT_FOR_IMPORT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT_FOR_IMPORT))

from scripts.verify_activation import (  # noqa: E402
    _DEFAULT_PROGRAMS_ROOT,
    _REPO_ROOT,
    build_corpus_freeze_check,
    build_counterfactual_freeze_check,
)

REGENERATE_COMMAND_TEMPLATE = "python scripts/verify_activation.py --write-corpus-freeze --program {program}"
COUNTERFACTUAL_FREEZE_PATH_TEMPLATE = "_quality/counterfactual_freeze/{source_document_key}.json"


def check_corpus_freeze_drift(
    *,
    program: str,
    programs_root: Path = _DEFAULT_PROGRAMS_ROOT,
    repo_root: Path = _REPO_ROOT,
) -> tuple[str, str]:
    """Returns ``(outcome, message)`` where outcome is one of never_frozen/drift/clean."""
    result = build_corpus_freeze_check(program=program, programs_root=programs_root, repo_root=repo_root)
    if result.status == "pass":
        return "clean", result.summary
    if result.summary == "corpus freeze manifest is missing":
        return "never_frozen", result.summary
    regenerate = REGENERATE_COMMAND_TEMPLATE.format(program=program)
    failures = result.details.get("failures") or []
    detail = f" ({'; '.join(failures)})" if failures else ""
    return "drift", f"{result.summary}{detail}. Regenerate with: {regenerate}"


def check_counterfactual_freeze_drift(
    *,
    program: str,
    with_fact_render: Path,
    without_fact_render: Path,
    source_document_key: str,
    approval_event_id: str | None = None,
    programs_root: Path = _DEFAULT_PROGRAMS_ROOT,
    repo_root: Path = _REPO_ROOT,
) -> tuple[str, str]:
    """Returns ``(outcome, message)``, same three-outcome convention as
    ``check_corpus_freeze_drift``."""
    freeze_path = programs_root / program / COUNTERFACTUAL_FREEZE_PATH_TEMPLATE.format(
        source_document_key=source_document_key
    )
    result = build_counterfactual_freeze_check(
        freeze_path=freeze_path,
        with_fact_path=with_fact_render,
        without_fact_path=without_fact_render,
        source_document_key=source_document_key,
        approval_event_id=approval_event_id,
        repo_root=repo_root,
    )
    if result.status == "pass":
        return "clean", result.summary
    if result.summary == "counterfactual freeze manifest is missing":
        return "never_frozen", result.summary
    regenerate = (
        f"python scripts/verify_activation.py --program {program} --write-counterfactual-freeze "
        f"--with-fact-render {with_fact_render} --without-fact-render {without_fact_render} "
        f"--source-document-key {source_document_key}"
    )
    return "drift", f"{result.summary}. Regenerate with: {regenerate}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--program", default="nova")
    parser.add_argument("--programs-root", type=Path, default=_DEFAULT_PROGRAMS_ROOT)
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    parser.add_argument("--counterfactual-freeze-check", action="store_true", help="Also run the counterfactual-artifact freeze check for one named render pair.")
    parser.add_argument("--with-fact-render", type=Path, help="Path to the with-fact render text (required with --counterfactual-freeze-check).")
    parser.add_argument("--without-fact-render", type=Path, help="Path to the without-fact render text (required with --counterfactual-freeze-check).")
    parser.add_argument("--source-document-key", help="The source document key this counterfactual pair proves (required with --counterfactual-freeze-check).")
    parser.add_argument("--approval-event-id", default=None)
    args = parser.parse_args(argv)

    outcome, message = check_corpus_freeze_drift(
        program=args.program, programs_root=args.programs_root, repo_root=args.repo_root
    )
    print(f"[{outcome}] {message}")
    exit_code = 1 if outcome == "drift" else 0

    if args.counterfactual_freeze_check:
        if not args.with_fact_render or not args.without_fact_render or not args.source_document_key:
            print("--counterfactual-freeze-check requires --with-fact-render, --without-fact-render, and --source-document-key")
            return 2
        cf_outcome, cf_message = check_counterfactual_freeze_drift(
            program=args.program,
            with_fact_render=args.with_fact_render,
            without_fact_render=args.without_fact_render,
            source_document_key=args.source_document_key,
            approval_event_id=args.approval_event_id,
            programs_root=args.programs_root,
            repo_root=args.repo_root,
        )
        print(f"[{cf_outcome}] {cf_message}")
        if cf_outcome == "drift":
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
