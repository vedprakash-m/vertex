#!/usr/bin/env python
"""WS-6: proof_run.py — record an exit-0 non-forced dry-run confirm.

Records evidence that ``vertex confirm --dry-run`` (no ``--force``) succeeds
for a given edition.  The result is written to ``proof_runs/<ts>.json`` under
the repo root so operators can point to it as a proof artifact.

Usage::

    python scripts/proof_run.py --edition <edition_name>
    python scripts/proof_run.py --edition acme_weekly --programs-root programs/
    python scripts/proof_run.py --edition acme_weekly --dry-run-only

Exit codes:
    0  confirm would succeed (all non-forceable gates passed)
    1  confirm would be blocked (gate failure recorded in proof artifact)
    2  usage error
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PROOF_RUNS_DIR = REPO_ROOT / "proof_runs"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record a non-forced dry-run confirm as a proof artifact.",
    )
    parser.add_argument("--edition", required=True, help="Edition name (e.g. acme_weekly).")
    parser.add_argument(
        "--programs-root",
        default=None,
        help="Override the programs root directory.",
    )
    parser.add_argument(
        "--editions-root",
        default=None,
        help="Override the editions root directory.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Override the output directory for proof_runs/ artifacts.",
    )
    return parser.parse_args(argv)


def run_proof(
    edition: str,
    *,
    programs_root: Path | None = None,
    editions_root: Path | None = None,
    output_dir: Path | None = None,
) -> dict[str, object]:
    """Run ``vertex confirm --dry-run`` and write the result as a proof artifact.

    Returns the proof record dict.  Raises ``SystemExit`` if the confirm
    command is not on PATH.
    """
    ts = datetime.now(timezone.utc)
    ts_str = ts.strftime("%Y%m%dT%H%M%SZ")

    cmd = ["python", "-m", "vertex", "confirm", "--edition", edition, "--dry-run"]
    if programs_root is not None:
        cmd += ["--programs-root", str(programs_root)]
    if editions_root is not None:
        cmd += ["--editions-root", str(editions_root)]

    result = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    proof = {
        "recorded_at": ts.isoformat(),
        "edition": edition,
        "command": " ".join(cmd),
        "exit_code": result.returncode,
        "force": False,
        "passed": result.returncode == 0,
        "stdout_tail": result.stdout[-2000:] if result.stdout else "",
        "stderr_tail": result.stderr[-2000:] if result.stderr else "",
    }

    out_dir = output_dir or PROOF_RUNS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{ts_str}_{edition}.json"
    out_path.write_text(json.dumps(proof, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Proof artifact written: {out_path.relative_to(REPO_ROOT)}")
    print(f"exit_code={result.returncode}  passed={proof['passed']}")
    if result.returncode != 0 and result.stdout:
        # Print last 10 lines of stdout to surface gate failures.
        lines = result.stdout.strip().splitlines()[-10:]
        for line in lines:
            print("  >", line)

    return proof


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    proof = run_proof(
        args.edition,
        programs_root=Path(args.programs_root) if args.programs_root else None,
        editions_root=Path(args.editions_root) if args.editions_root else None,
    )
    return 0 if proof["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
