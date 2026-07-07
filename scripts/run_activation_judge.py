#!/usr/bin/env python
"""Run the activation LLM-judge over the current evidence (activation.md §6.16).

Usage:
    python scripts/run_activation_judge.py --program xpf
    python scripts/run_activation_judge.py --program xpf --gates AG-1,AG-2
    python scripts/run_activation_judge.py --program xpf --flip milestone.completed

Fail-closed: if VERTEX_AI_JUDGE_DEPLOYMENT is unset, the extractor and judge
deployments are equal, the LLM is unreachable, or budget is exceeded, every gate
is reported JUDGE_UNAVAILABLE with the deterministic finding preserved. The
judge never silently passes.

Writes ``output/judge-verdicts.json`` (durable, commit+model stamped) and prints
a human-readable summary including the AMBIGUOUS human-decision packets.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.ai.prompt_registry import load_prompt, PromptRegistryError  # noqa: E402
from src.ai.activation_judge import (  # noqa: E402
    JUDGE_PROMPT_VERSION,
    STATUS_AMBIGUOUS,
    STATUS_FAIL,
    STATUS_JUDGE_UNAVAILABLE,
    STATUS_PASS,
    assess_activation,
    assess_activation_deterministic,
)
from src.core.rev.quality_metrics import verify_judge_independence  # noqa: E402

log = logging.getLogger("activation_judge")


def main() -> int:
    parser = argparse.ArgumentParser(description="Activation LLM-judge assessment")
    parser.add_argument("--program", default="xpf")
    parser.add_argument("--report", default="output/activation-report.json",
                        help="Input activation report (run verify_activation.py first)")
    parser.add_argument("--out", default="output/judge-verdicts.json",
                        help="Output durable verdict artifact")
    parser.add_argument("--gates", default="",
                        help="Comma-separated gate IDs to assess (default: all)")
    parser.add_argument("--flip", default="",
                        help="Assess authority-flip readiness for a family")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    report_path = REPO_ROOT / args.report
    if not report_path.exists():
        # Auto-generate the evidence so the judge always reads current data.
        log.info("activation report missing — generating via verify_activation.py")
        import subprocess
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "verify_activation.py"),
             "--program", args.program,
             "--json", str(report_path),
             "--markdown", str(REPO_ROOT / "output" / "activation-evidence.md")],
            check=True, cwd=str(REPO_ROOT),
        )
    activation_report = json.loads(report_path.read_text(encoding="utf-8"))

    target_gates = tuple(g.strip() for g in args.gates.split(",") if g.strip()) or None
    flip_family = args.flip.strip() or None

    # Resolve the judge LLM client (fail-closed if unavailable).
    client, judge_model, unavailable_reason = _resolve_judge_client()
    if client is None:
        log.warning("LLM judge unavailable (%s) — using deterministic expert fallback", unavailable_reason)

    # Gather program artifacts the judge reasons over (real evidence data).
    programs_root = REPO_ROOT / "programs"
    program_artifacts = _load_program_artifacts(args.program, programs_root)
    git_sha = activation_report.get("git_sha", "unknown")

    if client is not None:
        # Load the deep-expertise system prompt (registered, versioned).
        try:
            system_prompt = load_prompt(JUDGE_PROMPT_VERSION)
        except PromptRegistryError as exc:
            log.error("prompt load failed: %s", exc)
            return 2
        report = assess_activation(
            activation_report=activation_report,
            program_artifacts=program_artifacts,
            client=client,
            system_prompt=system_prompt,
            judge_model=judge_model,
            git_sha=git_sha,
            target_gates=target_gates,
            flip_family=flip_family,
        )
    else:
        # Deterministic deep-expertise fallback: same evidence, same falsifiability
        # rules, no LLM. Produces the optimal-sequence + human-decision packets.
        report = assess_activation_deterministic(
            activation_report=activation_report,
            program_artifacts=program_artifacts,
            git_sha=git_sha,
            flip_family=flip_family,
        )

    out_path = REPO_ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
    log.info("verdicts written to %s", out_path)

    _print_summary(report)
    return 0 if report.judge_available else 1


def _resolve_judge_client() -> tuple[object, str, str]:
    """Build the judge LLM client from VERTEX_AI_JUDGE_DEPLOYMENT.

    Returns (client, model_name, unavailable_reason). When the deployment is
    unset, judge-independence fails, or the client can't be built, returns
    (None, "unavailable", reason) — fail-closed.
    """
    judge_deployment = os.environ.get("VERTEX_AI_JUDGE_DEPLOYMENT", "").strip()
    if not judge_deployment:
        return None, "unavailable", "VERTEX_AI_JUDGE_DEPLOYMENT not set"

    ok, msg = verify_judge_independence()
    if not ok:
        return None, "unavailable", f"judge-independence check failed: {msg}"

    try:
        from src.ai.client import AIClient
        from src.ai.deployment_fallback import resolve_ai_deployments_for_feature
        deployments = resolve_ai_deployments_for_feature(
            feature_name="activation_judge",
            primary_candidates=(judge_deployment,),
            backup_candidates=(),
            primary_fallback_envs=("VERTEX_AI_JUDGE_DEPLOYMENT",),
            backup_fallback_envs=(),
        )
        if not deployments:
            return None, "unavailable", "no judge deployments resolved"
        client = AIClient(deployments=deployments, temperature=0.0, budget_usd=1.0)
        return client, judge_deployment, ""
    except Exception as exc:  # noqa: BLE001
        return None, "unavailable", f"client construction failed: {exc}"


def _load_program_artifacts(program_id: str, programs_root: Path) -> dict[str, object]:
    """Load the real evidence artifacts the judge reasons over."""
    import yaml
    program_dir = programs_root / program_id
    artifacts: dict[str, object] = {}

    def _read_json(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except Exception:
            return None

    def _read_yaml(path: Path) -> dict | None:
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
        except Exception:
            return None

    artifacts["last_cycle"] = _read_json(program_dir / "_rev" / "last_cycle.json")
    artifacts["quality_metrics"] = _read_json(program_dir / "_quality" / "rev_quality_metrics.json")
    artifacts["platform_proof_log"] = _read_yaml(program_dir / "platform_proof_log.yaml")
    artifacts["trusted_baseline"] = _read_yaml(program_dir / "trusted_baseline.yaml")
    artifacts["fact_sor_state"] = _read_yaml(program_dir / "fact_store_sor.yaml")
    # Corpus summary (counts, not full rows — the judge doesn't need every row).
    corpus_path = program_dir / "_quality" / "rev_labeled_corpus.jsonl"
    if corpus_path.exists():
        try:
            rows = [json.loads(line) for line in corpus_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            from collections import Counter
            event_types = Counter(str(r.get("expected_event_type", "")) for r in rows)
            dual = sum(1 for r in rows if r.get("second_label"))
            artifacts["corpus_summary"] = {
                "row_count": len(rows),
                "dual_labeled_count": dual,
                "event_type_counts": dict(event_types.most_common(10)),
            }
        except Exception:
            artifacts["corpus_summary"] = {"error": "unreadable"}
    return artifacts


def _print_summary(report) -> None:
    print("\n" + "=" * 72)
    print(f"ACTIVATION JUDGE — {report.judge_model}  (prompt {report.prompt_version})")
    print(f"commit {report.git_sha}  ·  generated {report.generated_at}")
    print("=" * 72)
    if not report.judge_available:
        print("⚠  JUDGE UNAVAILABLE — deterministic findings only (fail-closed)\n")
    counts = {STATUS_PASS: 0, STATUS_FAIL: 0, STATUS_AMBIGUOUS: 0, STATUS_JUDGE_UNAVAILABLE: 0}
    for v in report.verdicts:
        counts[v.status] = counts.get(v.status, 0) + 1
    print(f"Verdicts: {counts[STATUS_PASS]} PASS · {counts[STATUS_FAIL]} FAIL · "
          f"{counts[STATUS_AMBIGUOUS]} AMBIGUOUS · {counts[STATUS_JUDGE_UNAVAILABLE]} UNAVAILABLE\n")
    for v in report.verdicts:
        marker = {"PASS": "✓", "FAIL": "✗", "AMBIGUOUS": "?", "JUDGE_UNAVAILABLE": "—"}.get(v.status, " ")
        print(f"  {marker} [{v.bar}] {v.gate_id:<32} {v.status}")
        if v.reasoning:
            print(f"      {v.reasoning[:200]}")
    if report.sequence_recommendation:
        print("\nRecommended sequence:")
        for i, step in enumerate(report.sequence_recommendation, 1):
            print(f"  {i}. {step}")
    packets = report.human_decision_packets()
    if packets:
        print(f"\n{'!'*4}  {len(packets)} HUMAN DECISION(S) REQUIRED  {'!'*4}")
        for v in packets:
            print(f"\n  ◇ {v.gate_id} ({v.bar}) — {v.recommendation}")
            if v.alternatives:
                print("    alternatives:")
                for a in v.alternatives:
                    print(f"      - {a}")
            if v.decision_context:
                print(f"    context: {v.decision_context[:500]}")
    print(f"\n{report.summary}")
    print(f"\nFull verdicts: output/judge-verdicts.json")


if __name__ == "__main__":
    raise SystemExit(main())
