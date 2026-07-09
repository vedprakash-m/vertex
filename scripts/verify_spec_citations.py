#!/usr/bin/env python3
"""Verify a sample of file:line citations in specs/ against the live source.

Fix-data-flow.md Track G / PR-2 (v1.3 addition): the meta-lesson from four
rounds of spec review is that reviewer "cannot reproduce" / "zero grep
results" claims have repeatedly been tooling false negatives, and that
file:line citations in the spec can drift silently (e.g. the
``_MAX_INCREMENTAL_DELTA`` citation that pointed to the wrong line, and the
PS-11 numbers dispute across v1.1-v1.2). This script converts "trust the
citation" into "the citation is mechanically re-resolved" for a curated set
of load-bearing citations.

It is a *sample*, not an exhaustive link-checker: it covers the citations
this spec's verification actually depended on (the ones whose correctness
determined a scope or effort estimate). Add entries to ``CITATIONS`` as new
load-bearing claims are introduced.

Usage:
    python scripts/verify_spec_citations.py [--format human|json] [--strict]

Exit code: 0 if all citations resolve, 1 otherwise (or if any citation's
claimed content is absent, under ``--strict``).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Citation:
    """A single load-bearing citation to re-verify.

    ``claim`` is the human description of what the citation supports.
    ``path`` is the repo-relative source file. ``line`` is the 1-indexed line
    number the citation points at. ``must_contain`` is a snippet that must
    appear on or near that line (within a small window) for the citation to
    count as resolved — guards against a line-number shift silently moving
    the citation to an unrelated statement.
    """

    claim: str
    path: str
    line: int
    must_contain: str
    # Window of lines either side of ``line`` to search for ``must_contain``.
    # Default 3 absorbs minor reformatting without making the check vacuous.
    window: int = 3


# Curated set of load-bearing citations from specs/fix-data-flow.md.
# Each entry's correctness was verified during spec review; this script keeps
# it correct as the codebase evolves. Add new entries as new load-bearing
# claims land in the spec.
CITATIONS: tuple[Citation, ...] = (
    Citation(
        claim="Track B.5 (closed 2026-07-08): risk_stage.py now routes through sor_gated_family_load(family=\"judgment\", ...) instead of a hand-written inline branch",
        path="src/core/stages/risk_stage.py",
        line=30,
        must_contain="judgment",
    ),
    Citation(
        claim="PS-1: ActionStage mirrors the risk pattern (direct fact-store read)",
        path="src/core/stages/action_stage.py",
        line=44,
        must_contain="load_program_facts",
    ),
    Citation(
        claim="PS-1: milestone_stage dependency load is the same isolated function Track B reuses",
        path="src/core/stages/milestone_stage.py",
        line=190,
        must_contain="_load_current_dependencies",
    ),
    Citation(
        claim="Track B (2026-07-08): milestone SoR-gate pattern (the proven template Track B will mirror)",
        path="src/core/stages/milestone_stage.py",
        line=34,
        must_contain="resolve_family_sor_mode",
    ),
    Citation(
        claim="Track E (closed 2026-07-08): report_health now accepts an optional pre-loaded `reality` and preserves FactAssessment metadata (fixed the PS-1 .record-stripping defect this citation originally pointed at)",
        path="src/commands/report_health.py",
        line=140,
        must_contain="reality: ProgramReality",
    ),
    Citation(
        claim="PS-1: report_deck reloads ProgramReality fresh per call site (3 sites)",
        path="src/commands/report_deck.py",
        line=241,
        must_contain="ProgramReality",
    ),
    Citation(
        claim="PS-1: report_scorecards calls ProgramReality (cross-program dependency data)",
        path="src/commands/report_scorecards.py",
        line=396,
        must_contain="ProgramReality",
    ),
    Citation(
        claim="Track C (closed 2026-07-08): report_lookback's assumption lifecycle now routes through sor_gated_family_load(family=\"judgment\", ...)",
        path="src/commands/report_lookback.py",
        line=799,
        must_contain="judgment",
    ),
    Citation(
        claim="Track A (closed 2026-07-07): bridge-disabled early return now gated by _ledger_fact_bridge_enabled, with a reactive warning added alongside it",
        path="src/commands/ledger.py",
        line=2228,
        must_contain="_ledger_fact_bridge_enabled",
    ),
    Citation(
        claim="Track A (closed 2026-07-07): PASSTHROUGH branch now logs at debug (previously a bare silent return, per PS-2's v1.1 addition)",
        path="src/commands/ledger.py",
        line=2242,
        must_contain="PASSTHROUGH",
    ),
    Citation(
        claim="Track A (closed 2026-07-07): bridge except-Exception handler (logs ERROR, now also records to bridge_failures.jsonl for the backlog doctor check)",
        path="src/commands/ledger.py",
        line=2297,
        must_contain="except Exception",
    ),
    Citation(
        claim="PS-3: incremental-projection function already exists (Track D is a wiring task)",
        path="src/core/ledger/program_views.py",
        line=174,
        must_contain="def project_events_incremental_to_sqlite",
    ),
    Citation(
        claim="PS-3: _MAX_INCREMENTAL_DELTA definition site (cited at both :26 and :213 across revisions)",
        path="src/core/ledger/program_views.py",
        line=26,
        must_contain="_MAX_INCREMENTAL_DELTA",
    ),
    Citation(
        claim="PS-1: all 8 ProgramReality family accessors exist (1089-1170)",
        path="src/core/program_reality.py",
        line=1092,
        must_contain="def risks",
    ),
    Citation(
        claim="PS-1: all 7 bridge appenders exist (836-925)",
        path="src/core/ledger/fact_bridge.py",
        line=836,
        must_contain="def append_bridged_risk_event",
    ),
    Citation(
        claim="PS-6: digest.j2 deprecation shim with 2026-12-31 sunset",
        path="src/core/html_renderer.py",
        line=227,
        must_contain="digest.j2",
    ),
    Citation(
        claim="PS-10: AI narrative bypass — assemble_stage passes bundle.program_context, not ProgramReality",
        path="src/commands/report_pipeline/assemble_stage.py",
        line=1052,
        must_contain="bundle.program_context",
    ),
    Citation(
        claim="Track B (closed 2026-07-08): render_stage.py's former 7th risk-read call site now reuses ctx.risks, falling back to _load_current_risks only for isolated-stage execution",
        path="src/core/stages/render_stage.py",
        line=570,
        must_contain="_load_current_risks",
    ),
    Citation(
        claim="PS-15: resolve_family_sor_mode fallback returns program-level mode for un-pinned families",
        path="src/core/fact_sor_state.py",
        line=62,
        must_contain="family_modes.get",
    ),
    Citation(
        claim="PS-16: AUTHORITY_FAMILIES 6-family allow-list excludes risk/action/decision/dependency/workstream",
        path="src/core/fact_sor_state.py",
        line=18,
        must_contain="AUTHORITY_FAMILIES",
    ),
    Citation(
        claim="PS-16: admin_fact_store_flip enforces AUTHORITY_FAMILIES via BadParameter",
        path="src/commands/admin_fact_store_flip.py",
        line=107,
        must_contain="AUTHORITY_FAMILIES",
    ),
    Citation(
        claim="PS-14: PROGRAMS_ROOT defaults to <repo>/programs (path-resolution root cause)",
        path="src/core/edition_resolver.py",
        line=41,
        must_contain="PROGRAMS_ROOT",
    ),
    Citation(
        claim="PS-14: db_root defaults to programs_root.parent (resolves to <repo>/<program_id>/)",
        path="src/core/program_fact_store.py",
        line=457,
        must_contain="resolved_db_root = programs_root.parent",
    ),
    Citation(
        claim="PS-1: ask_intents renders truth_level as plain bracketed text (no glyph badges anywhere)",
        path="src/commands/ask_intents.py",
        line=138,
        must_contain="truth",
    ),
)


@dataclass
class Result:
    citation: dict
    status: str  # pass | fail
    detail: str


def _verify_one(citation: Citation, repo_root: Path) -> Result:
    src_path = repo_root / citation.path
    if not src_path.exists():
        return Result(asdict(citation), "fail", f"file not found: {citation.path}")
    try:
        text = src_path.read_text(encoding="utf-8")
    except OSError as exc:
        return Result(asdict(citation), "fail", f"could not read {citation.path}: {exc}")
    lines = text.splitlines()
    if citation.line < 1 or citation.line > len(lines):
        return Result(
            asdict(citation), "fail",
            f"line {citation.line} out of range (file has {len(lines)} lines)",
        )
    lo = max(0, citation.line - 1 - citation.window)
    hi = min(len(lines), citation.line + citation.window)
    window_text = "\n".join(lines[lo:hi])
    if citation.must_contain not in window_text:
        return Result(
            asdict(citation), "fail",
            f"line {citation.line} ±{citation.window} does not contain {citation.must_contain!r}",
        )
    return Result(
        asdict(citation), "pass",
        f"line {citation.line} ({citation.path}) contains {citation.must_contain!r}",
    )


def run(*, repo_root: Path = REPO_ROOT) -> list[Result]:
    return [_verify_one(c, repo_root) for c in CITATIONS]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-verify a sample of load-bearing file:line citations in specs against live source.",
    )
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--format", choices=("human", "json"), default="human")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="(Reserved) promote advisory findings to failures. Currently all failures are real.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    results = run(repo_root=repo_root)
    failed = [r for r in results if r.status != "pass"]

    if args.format == "json":
        payload = {
            "overall": "pass" if not failed else "fail",
            "passed": len(results) - len(failed),
            "failed": len(failed),
            "results": [asdict(r) for r in results],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(f"Spec citation checks: {len(results) - len(failed)}/{len(results)} resolved")
        for r in results:
            prefix = "PASS" if r.status == "pass" else "FAIL"
            print(f"{prefix:4} {r.citation['path']}:{r.citation['line']}  {r.detail}")
            if r.status != "pass":
                print(f"        claim: {r.citation['claim']}")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
