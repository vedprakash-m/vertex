"""GAP-35: derive snapshot-derived *operational-completion* signals per capability.

``derive_code_completion.py`` answers "is the code there?". This companion
answers the orthogonal question the ``Completion Snapshot`` table's
"Operational (est.)" column guesses at: "has the operator *exercised* it?".
Operational completion is largely **OPERATOR-gated** (live ADO/Kusto runs,
DPA sign-off, AI graduation sign-off) and cannot be faked from the code tree.
So this tool does **not** emit a misleading percentage. Instead it honestly
reports the on-disk operational artifacts that *are* mechanically checkable:

  - AI graduations recorded under ``governance/graduations/``
  - AI-safety-approver role files under ``governance/roles/``
  - DPA scope artifact ``governance/dpa-scope.md``
  - ``specs/test-evidence.md`` CI-evidence row count (GAP-20)
  - confirmed-issue manifest counts per program (acme / fabrikam) from
    ``programs/<id>/archive/*/manifests/issue_*.json``
  - multi-program operational proof (fabrikam confirmed-issues > 0)

Each metric is a concrete count or boolean — never a synthesized ratio — so
the spec author can replace the frozen editorial estimates with grounded
numbers without overstating OPERATOR-gated readiness.

Design constraints (matching ``derive_spec_counts.py``):
  - **Read-only**.
  - **No masking** — a missing artifact reports ``0`` / ``False``, not ``-1``
    (every probe is a concrete on-disk check). Exits ``1`` only if the repo
    root cannot be resolved (genuine probe failure).
  - **Honest** — operational readiness is reported as counts/presence, not a
    fake percentage, because the remaining gap is human/OPERATOR action that
    no tree scan can confirm.

Usage:
    python scripts/derive_operational_completion.py
    python scripts/derive_operational_completion.py --format json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _count_files(directory: Path, suffix: str = "") -> int:
    if not directory.exists():
        return 0
    if suffix:
        return sum(1 for _ in directory.glob(f"*{suffix}"))
    return sum(1 for p in directory.iterdir() if p.is_file())


def _exists(path: Path) -> bool:
    return path.exists()


def _count_confirmed_issues(program_id: str) -> int:
    """Count ``issue_*.json`` manifests across all editions of a program.

    Layout: ``programs/<id>/archive/<edition>/manifests/issue_NNN.json``.
    """
    archive_root = REPO_ROOT / "programs" / program_id / "archive"
    if not archive_root.exists():
        return 0
    total = 0
    for edition_dir in archive_root.iterdir():
        manifests = edition_dir / "manifests"
        if manifests.is_dir():
            total += sum(1 for _ in manifests.glob("issue_*.json"))
    return total


def _test_evidence_rows() -> int:
    """Count non-blank, non-header rows in ``specs/test-evidence.md``.

    The file is a markdown table; rows are lines starting with ``|`` excluding
    the separator (``|---``) and the header. Returns 0 if the file is absent
    (GAP-20 — not yet populated).
    """
    path = REPO_ROOT / "specs" / "test-evidence.md"
    if not path.exists():
        return 0
    rows = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if set(stripped.replace("|", "").replace("-", "").strip()) <= set():
            continue
        if "---" in stripped:
            continue
        rows += 1
    # Subtract the header row (first ``|``-line that is not a separator).
    return max(rows - 1, 0)


def derive() -> dict[str, object]:
    if not REPO_ROOT.exists():
        raise FileNotFoundError(f"REPO_ROOT not found: {REPO_ROOT}")

    graduations_dir = REPO_ROOT / "governance" / "graduations"
    roles_dir = REPO_ROOT / "governance" / "roles"
    ai_approver_role = roles_dir / "ai-safety-approver.md"
    ai_approver_yaml = roles_dir / "ai_safety_approver.yaml"
    dpa_scope = REPO_ROOT / "governance" / "dpa-scope.md"

    primary_confirmed = _count_confirmed_issues("acme")
    secondary_confirmed = _count_confirmed_issues("fabrikam")

    return {
        "ai_graduations": _count_files(graduations_dir, suffix=".md"),
        "ai_safety_approver_role_md": _exists(ai_approver_role),
        "ai_safety_approver_role_yaml": _exists(ai_approver_yaml),
        "dpa_scope_artifact": _exists(dpa_scope),
        "test_evidence_rows": _test_evidence_rows(),
        "primary_confirmed_issues": primary_confirmed,
        "secondary_confirmed_issues": secondary_confirmed,
        "multi_program_operational_proof": secondary_confirmed > 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive snapshot-derived operational-completion signals from on-disk artifacts."
    )
    parser.add_argument("--format", choices=("human", "json"), default="human")
    args = parser.parse_args(argv)

    try:
        derived = derive()
    except FileNotFoundError as error:
        print(f"ERROR: {error}  <-- PROBE FAILED")
        return 1

    if args.format == "json":
        print(json.dumps(derived, indent=2))
        return 0

    print(f"{'signal':>34}  {'value':>12}")
    print("-" * 50)
    for key, value in derived.items():
        if isinstance(value, bool):
            shown = "yes" if value else "no"
        else:
            shown = str(value)
        print(f"{key:>34}  {shown:>12}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())