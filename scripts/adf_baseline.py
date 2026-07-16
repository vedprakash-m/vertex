"""ADF-W0.3: Phase-0 baseline probe runner (`specs/arch-data-fix.md` Section 4.1).

Regenerates the Section 4.1 evidence baseline from executable, read-only
probes and writes a versioned, reproducible artifact:

    governance/baselines/adf_baseline_<utcdate>.json

Each row records ``{evidence, command, owner, status, value, detail}``.
``status`` is one of:

- ``ok`` -- the probe ran and returned a value;
- ``error`` -- the probe raised (recorded, never crashes the run);
- ``accepted_limitation`` -- the row is not yet automatable; ``detail``
  names the owning work item that will close the gap (Phase-0 exit
  requires every row to carry one or the other).

``--verify`` re-runs every probe read-only against the newest existing
artifact and reports per-row reproducibility (no probe raises) plus any
value drift since that artifact was captured. It never mutates program
state and never writes a new artifact.

Usage::

    python scripts/adf_baseline.py --capture --program xpf
    python scripts/adf_baseline.py --verify  [--program xpf]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_DIR = REPO_ROOT / "governance" / "baselines"
DEFAULT_PROGRAM_ID = "xpf"
DEFAULT_PROGRAMS_ROOT = REPO_ROOT / "programs"
ARTIFACT_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class ProbeRow:
    evidence: str
    command: str
    owner: str
    status: str
    value: Any = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _run_probe(*, evidence: str, command: str, owner: str, fn: Callable[[], Any]) -> ProbeRow:
    try:
        value = fn()
        return ProbeRow(evidence=evidence, command=command, owner=owner, status="ok", value=value)
    except Exception as exc:  # noqa: BLE001 - a probe failure is data, not a crash
        return ProbeRow(
            evidence=evidence,
            command=command,
            owner=owner,
            status="error",
            value=None,
            detail=f"{type(exc).__name__}: {exc}",
        )


def _accepted_limitation(*, evidence: str, owner: str, detail: str) -> ProbeRow:
    return ProbeRow(evidence=evidence, command="", owner=owner, status="accepted_limitation", value=None, detail=detail)


# --------------------------------------------------------------------------------------
# Individual probes (Section 4.1 rows). Each is read-only.
# --------------------------------------------------------------------------------------


def _probe_ai_telemetry_records(program_id: str, programs_root: Path) -> int:
    from src.core.ai_telemetry import read_ai_telemetry

    return len(read_ai_telemetry(program_id, programs_root=programs_root))


def _probe_ai_policy() -> dict[str, Any]:
    from src.core.yaml_utils import load_yaml_mapping

    path = REPO_ROOT / "vertex" / "policies" / "ai_policy.yaml"
    document = load_yaml_mapping(path, required=True)
    features = document.get("ai_features") or {}
    if not isinstance(features, dict):
        features = {}
    frontier_eligible = sum(
        1 for spec in features.values() if isinstance(spec, dict) and spec.get("frontier_eligible", True)
    )
    return {"named_features": len(features), "frontier_eligible": frontier_eligible}


def _probe_tier_decisions(program_id: str, programs_root: Path) -> dict[str, Any]:
    from src.core.measurement_store import read_measurements, tier_decision_store_path

    path = tier_decision_store_path(program_id, programs_root=programs_root)
    rows = read_measurements(path)
    by_tier: dict[str, int] = {}
    for row in rows:
        tier = str(row.get("chosen_tier") or "unknown")
        by_tier[tier] = by_tier.get(tier, 0) + 1
    return {"total": len(rows), "by_tier": by_tier}


def _probe_risk_register(program_id: str, programs_root: Path) -> dict[str, Any]:
    from src.core.risk_register_engine import load_risk_register

    entries = load_risk_register(program_id, programs_root=programs_root)
    return {"rows": len(entries)}


def _probe_fact_store_candidates(program_id: str, programs_root: Path) -> dict[str, Any]:
    """Count distinct ``*.sqlite3`` candidate paths under known program roots.

    This is a lightweight structural scan, not the full QG-37 authority
    check (ADF-W1.9 owns that). It exists so the baseline row is not silent.

    Reports repo-relative sample paths only (never a raw absolute
    ``~/.vertex`` path) so a captured artifact never embeds an operator's
    home-directory username (Appendix D data-hygiene rule).
    """
    program_dir = programs_root / program_id
    under_program_root = sorted(p.relative_to(programs_root) for p in program_dir.rglob("*.sqlite3")) if program_dir.exists() else []
    home_fallback = Path.home() / ".vertex"
    under_home_fallback = sorted(p.name for p in home_fallback.rglob("*.sqlite3")) if home_fallback.exists() else []
    return {
        "under_programs_root_count": len(under_program_root),
        "under_home_fallback_count": len(under_home_fallback),
        "sample_under_programs_root": [str(p) for p in under_program_root[:10]],
    }


def build_probes(*, program_id: str, programs_root: Path) -> tuple[ProbeRow, ...]:
    return (
        _run_probe(
            evidence="AI telemetry records",
            command=f"read_ai_telemetry({program_id!r})",
            owner="ADF-W0.7",
            fn=lambda: _probe_ai_telemetry_records(program_id, programs_root),
        ),
        _run_probe(
            evidence="AI policy",
            command="load vertex/policies/ai_policy.yaml",
            owner="ADF-W5.1-ADF-W5.3",
            fn=_probe_ai_policy,
        ),
        _run_probe(
            evidence="Tier decisions (all tiers)",
            command=f"read_measurements(tier_decision_store_path({program_id!r}))",
            owner="ADF-W0.7",
            fn=lambda: _probe_tier_decisions(program_id, programs_root),
        ),
        _run_probe(
            evidence="Risk register",
            command=f"load_risk_register({program_id!r})",
            owner="ADF-W4.2-ADF-W4.3",
            fn=lambda: _probe_risk_register(program_id, programs_root),
        ),
        _run_probe(
            evidence="Fact-store path candidates",
            command=f"rglob('*.sqlite3') under programs/{program_id} and ~/.vertex",
            owner="ADF-W1.9",
            fn=lambda: _probe_fact_store_candidates(program_id, programs_root),
        ),
        _accepted_limitation(
            evidence="Activation verifier",
            owner="ADF-W0.3, ADF-W0.4, ADF-W6.2",
            detail="scripts/verify_activation.py is a separate long-running probe; run it directly and "
            "attach its pass/fail counts to the tracked baseline record rather than duplicating it here.",
        ),
        _accepted_limitation(
            evidence="Accepted-fact lineage",
            owner="ADF-W2.4-ADF-W2.5",
            detail="Lineage accounting lands with ADF-W2.4; no lineaged/waived/defect split exists yet.",
        ),
        _accepted_limitation(
            evidence="REV hydration fallback / unverified / pending counters",
            owner="ADF-W3.1-ADF-W3.2, ADF-W2.8, ADF-W3.3-ADF-W3.4",
            detail="REV counters are produced by the rev status pipeline; wire a probe once ADF-W3.1 lands.",
        ),
        _accepted_limitation(
            evidence="XPF operator gates / WorkIQ / Kusto historical latency",
            owner="ADF-W1.4-ADF-W1.6, ADF-W2.3, ADF-W3.1-ADF-W3.2",
            detail="Requires live-source measurement; captured once ADF-W0.7 channel telemetry accumulates "
            "real runs rather than a synthetic probe.",
        ),
        _accepted_limitation(
            evidence="Issue 079 AI contribution",
            owner="ADF-W2.8-ADF-W2.10",
            detail="No AI release-audit records exist yet (QG-29 activates in Slice 2).",
        ),
        _accepted_limitation(
            evidence="Maturity check crash state",
            owner="ADF-W1.8",
            detail="Running `vertex maturity-check` here would require live program state, which this "
            "read-only baseline probe must not touch; track pass/fail via CI instead.",
        ),
        _accepted_limitation(
            evidence="ADO create-task path safety",
            owner="ADF-W1.1-ADF-W1.3",
            detail="Structural fact, not a numeric probe; verified by tests/contracts/test_actuation_transport.py "
            "and tests/contracts/test_create_intent_idempotency.py once those land.",
        ),
    )


def _artifact_path(*, when: datetime) -> Path:
    return BASELINE_DIR / f"adf_baseline_{when.strftime('%Y%m%d')}.json"


def _newest_artifact() -> Path | None:
    if not BASELINE_DIR.exists():
        return None
    artifacts = sorted(BASELINE_DIR.glob("adf_baseline_*.json"))
    return artifacts[-1] if artifacts else None


def capture(*, program_id: str, programs_root: Path, out_dir: Path = BASELINE_DIR) -> Path:
    now = datetime.now(timezone.utc)
    rows = build_probes(program_id=program_id, programs_root=programs_root)
    artifact = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "captured_at": now.isoformat(),
        "git_sha": _git_sha(),
        "program_id": program_id,
        "rows": [row.to_dict() for row in rows],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"adf_baseline_{now.strftime('%Y%m%d')}.json"
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def verify(*, program_id: str, programs_root: Path, baseline_dir: Path = BASELINE_DIR) -> bool:
    """Re-run every probe and print per-row status. Returns True if reproducible."""
    rows = build_probes(program_id=program_id, programs_root=programs_root)
    previous_path = None
    if baseline_dir.exists():
        artifacts = sorted(baseline_dir.glob("adf_baseline_*.json"))
        previous_path = artifacts[-1] if artifacts else None
    previous_by_evidence: dict[str, Any] = {}
    if previous_path is not None:
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            previous_by_evidence = {row["evidence"]: row.get("value") for row in previous.get("rows", [])}
        except Exception:
            previous_by_evidence = {}

    ok = True
    print(f"ADF baseline verify -- program={program_id} previous_artifact={previous_path or '(none)'}")
    for row in rows:
        if row.status == "error":
            ok = False
            marker = "ERROR"
        elif row.status == "accepted_limitation":
            marker = "LIMITATION"
        else:
            drift = ""
            if row.evidence in previous_by_evidence and previous_by_evidence[row.evidence] != row.value:
                drift = " (DRIFT vs previous artifact)"
            marker = f"OK{drift}"
        print(f"  [{marker}] {row.evidence}: {row.value if row.status == 'ok' else row.detail}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program", default=DEFAULT_PROGRAM_ID, help="Program id to probe (default: xpf).")
    parser.add_argument(
        "--programs-root", default=str(DEFAULT_PROGRAMS_ROOT), help="Root directory containing program folders."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--capture", action="store_true", help="Write a new baseline artifact.")
    mode.add_argument("--verify", action="store_true", help="Re-run probes read-only and report status (default).")
    args = parser.parse_args(argv)

    programs_root = Path(args.programs_root)
    if args.capture:
        path = capture(program_id=args.program, programs_root=programs_root, out_dir=BASELINE_DIR)
        print(f"Wrote {path}")
        return 0

    reproducible = verify(program_id=args.program, programs_root=programs_root, baseline_dir=BASELINE_DIR)
    return 0 if reproducible else 1


if __name__ == "__main__":
    sys.exit(main())
