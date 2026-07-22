#!/usr/bin/env python3
"""specs/backlog.md BL-L1 action (2): the explicit, separately-named,
attested evidence-regeneration operation for activation verification.

`scripts/verify_activation.py --write-corpus-freeze` / `--write-counterfactual-pair`
/ `--write-counterfactual-freeze` still exist and still perform the actual
file writes (this script calls the same functions, not a fork of them).
What was missing -- and what this script adds -- is the other half of
BL-L1's action (2): a durable, separately-named operation that *attests*
who regenerated evidence, when, against which commit, and why, recorded
as a permanent, append-only audit trail
(``programs/<id>/_quality/evidence_regeneration_log.jsonl``). Before this,
"someone regenerated the corpus freeze" was an unlogged side effect of a
verifier invocation someone happened to add a flag to -- exactly the
pattern BL-L1's problem statement warned could mask a real regression.

Usage:
    python scripts/regenerate_activation_evidence.py --program xpf --corpus-freeze \\
        --reason "quarterly corpus refresh after new EML batch"

    python scripts/regenerate_activation_evidence.py --program xpf --counterfactual-freeze \\
        --with-fact-render <path> --without-fact-render <path> \\
        --source-document-key <key> --reason "..."

    python scripts/regenerate_activation_evidence.py --program xpf --counterfactual-pair \\
        --fact-id <milestone.entry fact_id> --reason "..."

Exactly one of --corpus-freeze / --counterfactual-freeze / --counterfactual-pair
is required per invocation, plus --reason (always required -- this is what
makes the attestation meaningful rather than boilerplate).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import json
from pathlib import Path
import sys

# Allow running as a standalone script (scripts/ is outside the package root).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.core.config_loader import PROGRAMS_ROOT  # noqa: E402
from src.core.jsonl_utils import append_jsonl_line  # noqa: E402
from scripts.verify_activation import (  # noqa: E402
    _file_sha256,
    _git_metadata,
    _write_temp_render,
    write_corpus_freeze_manifest,
    write_counterfactual_freeze_manifest,
)

_LOG_SCHEMA_VERSION = "activation_evidence_regeneration_log.v1"
#: Matches proposal_audit.jsonl's rotation cap (jsonl_utils.py convention).
_MAX_LOG_BYTES = 10 * 1024 * 1024


def _attest(
    *,
    program: str,
    programs_root: Path,
    operation: str,
    reason: str,
    manifest_path: Path,
    detail: dict[str, object],
) -> dict[str, object]:
    git_sha, dirty = _git_metadata(_REPO_ROOT)
    record: dict[str, object] = {
        "schema_version": _LOG_SCHEMA_VERSION,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "program": program,
        "operation": operation,
        "reason": reason,
        "operator": getpass.getuser(),
        "git_sha": git_sha,
        "git_dirty": dirty,
        "manifest_path": str(manifest_path),
        "manifest_sha256": _file_sha256(manifest_path),
        "detail": detail,
    }
    log_path = programs_root / program / "_quality" / "evidence_regeneration_log.jsonl"
    append_jsonl_line(log_path, json.dumps(record, sort_keys=True) + "\n", max_bytes=_MAX_LOG_BYTES)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--program", required=True)
    parser.add_argument("--programs-root", type=Path, default=PROGRAMS_ROOT)
    parser.add_argument("--reason", required=True, help="Why evidence is being regenerated now (recorded in the attestation log).")
    parser.add_argument("--corpus-freeze", action="store_true", help="Regenerate programs/<id>/_quality/corpus_freeze.json.")
    parser.add_argument(
        "--counterfactual-freeze",
        action="store_true",
        help="Regenerate the counterfactual freeze manifest for an explicit --with-fact-render/--without-fact-render/--source-document-key triple.",
    )
    parser.add_argument(
        "--counterfactual-pair",
        action="store_true",
        help="Auto-generate a with/without-fact render pair for --fact-id from ProgramReality, then freeze it.",
    )
    parser.add_argument("--with-fact-render", type=Path)
    parser.add_argument("--without-fact-render", type=Path)
    parser.add_argument("--source-document-key")
    parser.add_argument("--approval-event-id", help="Optional approval event id for AG-6 reverse lookup.")
    parser.add_argument("--fact-id", help="The milestone.entry fact_id to suppress for --counterfactual-pair.")
    args = parser.parse_args(argv)

    selected = (args.corpus_freeze, args.counterfactual_freeze, args.counterfactual_pair)
    if sum(bool(flag) for flag in selected) != 1:
        print(
            "Exactly one of --corpus-freeze / --counterfactual-freeze / --counterfactual-pair is required.",
            file=sys.stderr,
        )
        return 2

    if args.corpus_freeze:
        path = write_corpus_freeze_manifest(program=args.program, programs_root=args.programs_root, repo_root=_REPO_ROOT)
        record = _attest(
            program=args.program,
            programs_root=args.programs_root,
            operation="corpus_freeze",
            reason=args.reason,
            manifest_path=path,
            detail={},
        )
        _print_confirmation(path, record)
        return 0

    if args.counterfactual_freeze:
        if not (args.with_fact_render and args.without_fact_render and args.source_document_key):
            print(
                "--counterfactual-freeze requires --with-fact-render, --without-fact-render, and --source-document-key.",
                file=sys.stderr,
            )
            return 2
        path = args.programs_root / args.program / "_quality" / "counterfactual_freeze" / f"{args.source_document_key}.json"
        write_counterfactual_freeze_manifest(
            output_path=path,
            with_fact_path=args.with_fact_render,
            without_fact_path=args.without_fact_render,
            source_document_key=args.source_document_key,
            approval_event_id=args.approval_event_id,
            repo_root=_REPO_ROOT,
        )
        record = _attest(
            program=args.program,
            programs_root=args.programs_root,
            operation="counterfactual_freeze",
            reason=args.reason,
            manifest_path=path,
            detail={"source_document_key": args.source_document_key, "approval_event_id": args.approval_event_id},
        )
        _print_confirmation(path, record)
        return 0

    # --counterfactual-pair
    from src.commands.counterfactual_render import build_counterfactual_pair

    pair = build_counterfactual_pair(program_id=args.program, fact_id=args.fact_id or "", programs_root=args.programs_root)
    if pair is None or not pair.differs or not pair.source_document_key:
        print(
            f"Could not generate an attributable diff for fact_id={args.fact_id!r}. The fact must be an "
            "approved REV milestone carrying source_document_key lineage.",
            file=sys.stderr,
        )
        return 2

    with_path = _write_temp_render(pair.with_fact_text, "with")
    without_path = _write_temp_render(pair.without_fact_text, "without")
    source_document_key = args.source_document_key or pair.source_document_key
    approval_event_id = args.approval_event_id if args.approval_event_id is not None else pair.approval_event_id
    freeze_path = args.programs_root / args.program / "_quality" / "counterfactual_freeze" / f"{source_document_key}.json"
    write_counterfactual_freeze_manifest(
        output_path=freeze_path,
        with_fact_path=with_path,
        without_fact_path=without_path,
        source_document_key=source_document_key,
        approval_event_id=approval_event_id,
        repo_root=_REPO_ROOT,
    )
    record = _attest(
        program=args.program,
        programs_root=args.programs_root,
        operation="counterfactual_pair",
        reason=args.reason,
        manifest_path=freeze_path,
        detail={"fact_id": args.fact_id, "source_document_key": source_document_key, "approval_event_id": approval_event_id},
    )
    _print_confirmation(freeze_path, record)
    return 0


def _print_confirmation(path: Path, record: dict[str, object]) -> None:
    print(
        f"Regenerated {path} "
        f"(attested {record['recorded_at']} by {record['operator']!r} at git_sha={record['git_sha']}"
        f"{' [dirty tree]' if record['git_dirty'] else ''})."
    )


if __name__ == "__main__":
    raise SystemExit(main())
