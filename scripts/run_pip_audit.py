#!/usr/bin/env python3
"""Local parity for the CI pip-audit step (WS-7).

Wraps `pip-audit` with the same defaults the CI workflow uses so a developer
can run the audit locally and get the same exit code and output. This is a
thin shim: it does NOT swallow errors, does NOT rewrite findings, and does
NOT pretend a vulnerable tree is clean.

Exit codes mirror pip-audit's own:
- 0: no vulnerabilities found
- 1: vulnerabilities found
- 2: invalid arguments / setup error
- 3+: pip-audit internal error (raised)

BL-C5: requirements.txt is retired (pyproject.toml + its extras are now the
sole packaging authority). By default this audits the CURRENTLY INSTALLED
environment (pip-audit's own default with no `-r`/`--requirement` flag),
which is correct as long as the environment was set up via `pip install -e
".[dev,ai,ai-local,m365,kusto,render]"` (CI's install step). Pass
`--requirement <path>` explicitly if you want to audit a specific
requirements file instead (e.g. a generated lock file).

Usage:
    python scripts/run_pip_audit.py --strict
    python scripts/run_pip_audit.py --strict --requirement some-lock-file.txt
    python scripts/run_pip_audit.py --help
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `src/` importable so this script can be run from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


def _build_argparser() -> argparse.ArgumentParser:
    """Forward common pip-audit flags. Anything we don't know about we pass through."""
    parser = argparse.ArgumentParser(
        prog="run_pip_audit",
        description="Local-dev parity for the CI pip-audit step (WS-7).",
    )
    # We deliberately forward the most-common flags rather than re-implement
    # pip-audit's full CLI; the goal is parity, not a new interface.
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on any vulnerability (CI default).",
    )
    parser.add_argument(
        "--requirement",
        "-r",
        type=Path,
        default=None,
        help="Audit this requirements file instead of the current environment.",
    )
    parser.add_argument(
        "--vulnerability-service",
        choices=("osv", "pypi"),
        default="osv",
        help="Vulnerability service (default: osv, keyless).",
    )
    return parser


def main(argv: tuple[str, ...] | None = None) -> int:
    args = _build_argparser().parse_args(argv)

    if args.requirement is not None and not args.requirement.exists():
        print(
            f"ERROR: requirements file not found: {args.requirement}",
            file=sys.stderr,
        )
        return 2

    # We invoke pip-audit as a subprocess so the local run reports findings
    # the same way the CI step does. `python -m pip_audit` is the canonical
    # entry point; we pass our args through verbatim after the module name.
    # No `--requirement` means pip-audit falls back to its own default:
    # audit the currently installed environment.
    extra: list[str] = ["--requirement", str(args.requirement)] if args.requirement is not None else []
    if args.strict:
        extra.append("--strict")
    if args.vulnerability_service:
        extra.extend(["--vulnerability-service", args.vulnerability_service])

    import subprocess  # local import — only needed when the script is invoked.

    cmd = [sys.executable, "-m", "pip_audit", *extra]
    print(f"$ {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd)
    return result.returncode


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
