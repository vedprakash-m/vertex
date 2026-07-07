"""REV quality-floor metrics + regression gate (P2-3 / G-floor).

``scripts/rev_quality_check.py --program {program_id}``` computes the G-floor
metrics from the operator-annotated labeled corpus
(``programs/<id>/_quality/rev_labeled_corpus.jsonl``) joined to the staged
candidate store (``proposed_event_type`` per ``candidate_id``) and exits 1 when
a gated metric fails.

**Labeled corpus record schema** (one JSONL line per labeled candidate):
```
{
  "candidate_id": "<staged candidate id>",          # required
  "expected_event_type": "<ground-truth event>",   # required
  "label": "accept" | "reject",                    # required
  "annotator": "<reviewer id>",                    # optional
  "second_label": "accept" | "reject",             # optional (2nd annotator → kappa)
  "notes": "..."                                    # optional
}
```

**Metrics (per specs/gaps.md G-floor):**
- **G-xtract-prec** (extraction precision) = correct_type / N_total, where
  ``correct_type`` counts candidates whose ``proposed_event_type`` equals the
  record's ``expected_event_type``. Gate: ≥ 0.80.
- **G-accept-prec** (acceptance precision) = accept_correct / accept_n, where
  ``accept_correct`` counts ``label == "accept"`` candidates that are
  correct-type **and** grounded (have ≥1 evidence_ref). Gate: ≥ 0.85.
- **G-reject-rate** = reject_n / N_total (reported; not gated).
- **Macro-F1** (average of per-type F1s, types with N≥5 only; reported only).
- **Wilson CI** (95 % confidence intervals) for G-xtract-prec and G-accept-prec
  so operators know uncertainty at small corpus sizes.
- **per-event-type recall**: for each ``expected_event_type`` with N ≥ 5 labeled
  examples, recall_t = correct_t / N_t. Gate: each ≥ 0.50. Types with N < 5 emit
  ``insufficient_sample_for_gate`` and are excluded from the pass/fail count.
- **per-critical-family recall**: deployment/milestone/commitment families must
  meet an elevated recall floor (≥ 0.60, gated; families with N < 5 skipped).
- **Abstention coverage** = fraction of corpus rows that have a matched staged
  candidate (reported; low = many events were never staged).
- **Acceptance coverage** = fraction of ``label=accept`` rows where the
  candidate also had ≥1 evidence ref (reported; low = grounding gap).
- **Cohen's kappa** (OA-3 inter-annotator agreement): computed across records
  that carry a ``second_label``, over the first 20 such records. Gate: ≥ 0.70
  (only enforced when dual annotations exist).

**Judge independence (P2-11):** ``verify_judge_independence()`` asserts the
extractor and judge Azure OpenAI deployment IDs differ (env: ``VERTEX_AI_DEPLOYMENT``
vs ``VERTEX_AI_JUDGE_DEPLOYMENT``) so the LLM-as-judge never scores its own
output. The script calls this when ``--check-judge-independence`` is set.

Zone A — no AI or M365 imports; reads via the sanctioned ledger loaders.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.jsonl_utils import read_jsonl_records
from src.core.ledger.candidate_store import load_pending_candidates

log = logging.getLogger(__name__)

# Gate thresholds (specs/gaps.md G-floor + OA-3).
G_XTRACT_PREC_FLOOR = 0.80
G_ACCEPT_PREC_FLOOR = 0.85
G_PER_TYPE_RECALL_FLOOR = 0.50
G_CRITICAL_FAMILY_RECALL_FLOOR = 0.60   # elevated floor for critical families (S-9d)
G_KAPPA_FLOOR = 0.70
PER_TYPE_MIN_SAMPLE = 5   # N≥5 before per-type recall is meaningful
KAPPA_MIN_RECORDS = 1     # need ≥1 dual-annotated record to compute kappa
SMALL_N_WARN = 10         # warn (not fail) below this total

# Critical event-type families (S-9d): deployment, milestone, commitment.
# Any type prefixed with these strings gets the elevated recall gate.
CRITICAL_FAMILY_PREFIXES = ("deployment.", "milestone.", "commitment.")

# Wilson CI z-score for 95 % confidence interval.
_WILSON_Z = 1.96


@dataclass(frozen=True, slots=True)
class WilsonDenominatorRequirement:
    metric: str
    floor: float
    min_total_if_perfect: int
    min_successes_at_data_floor: int | None
    data_floor: int
    ci_low_at_data_floor_if_perfect: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "floor": self.floor,
            "min_total_if_perfect": self.min_total_if_perfect,
            "min_successes_at_data_floor": self.min_successes_at_data_floor,
            "data_floor": self.data_floor,
            "ci_low_at_data_floor_if_perfect": round(self.ci_low_at_data_floor_if_perfect, 4),
        }


@dataclass
class PerTypeRecall:
    event_type: str
    n: int
    correct: int
    recall: float
    insufficient_sample: bool = False
    is_critical_family: bool = False
    # Per-type F1 (requires precision; computed by _compute_per_type_f1).
    precision: float = 0.0
    f1: float = 0.0


@dataclass
class QualityReport:
    program_id: str
    n_total: int = 0
    n_matched_candidate: int = 0       # corpus rows that joined to a staged candidate
    correct_type: int = 0
    g_xtract_prec: float = 0.0
    # Wilson 95 % CI for g_xtract_prec.
    g_xtract_prec_ci_low: float = 0.0
    g_xtract_prec_ci_high: float = 1.0
    accept_n: int = 0
    accept_correct: int = 0
    g_accept_prec: float = 0.0
    # Wilson 95 % CI for g_accept_prec.
    g_accept_prec_ci_low: float = 0.0
    g_accept_prec_ci_high: float = 1.0
    reject_n: int = 0
    g_reject_rate: float = 0.0
    # Macro-F1 across per-type F1s (types with N≥5; reported, not gated).
    macro_f1: float | None = None
    # Abstention coverage: fraction of corpus rows that matched a staged candidate.
    abstention_coverage: float = 0.0
    # Acceptance coverage: fraction of accept-labeled rows that are also grounded.
    acceptance_coverage: float = 0.0
    per_type: list[PerTypeRecall] = field(default_factory=list)
    kappa: float | None = None
    kappa_n: int = 0
    gates_passed: dict[str, bool] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "n_total": self.n_total,
            "n_matched_candidate": self.n_matched_candidate,
            "correct_type": self.correct_type,
            "g_xtract_prec": round(self.g_xtract_prec, 4),
            "g_xtract_prec_ci": (
                round(self.g_xtract_prec_ci_low, 4),
                round(self.g_xtract_prec_ci_high, 4),
            ),
            "accept_n": self.accept_n,
            "accept_correct": self.accept_correct,
            "g_accept_prec": round(self.g_accept_prec, 4),
            "g_accept_prec_ci": (
                round(self.g_accept_prec_ci_low, 4),
                round(self.g_accept_prec_ci_high, 4),
            ),
            "reject_n": self.reject_n,
            "g_reject_rate": round(self.g_reject_rate, 4),
            "macro_f1": round(self.macro_f1, 4) if self.macro_f1 is not None else None,
            "abstention_coverage": round(self.abstention_coverage, 4),
            "acceptance_coverage": round(self.acceptance_coverage, 4),
            "per_type_recall": [
                {
                    "event_type": p.event_type, "n": p.n, "correct": p.correct,
                    "recall": round(p.recall, 4),
                    "precision": round(p.precision, 4),
                    "f1": round(p.f1, 4),
                    "insufficient_sample_for_gate": p.insufficient_sample,
                    "is_critical_family": p.is_critical_family,
                }
                for p in self.per_type
            ],
            "kappa": round(self.kappa, 4) if self.kappa is not None else None,
            "kappa_n": self.kappa_n,
            "gates_passed": dict(self.gates_passed),
            "failures": list(self.failures),
            "warnings": list(self.warnings),
            "activation_denominator_plan": [
                requirement.to_dict()
                for requirement in activation_denominator_plan()
            ],
        }


def _corpus_path(program_id: str, programs_root: Path) -> Path:
    return programs_root / program_id / "_quality" / "rev_labeled_corpus.jsonl"


def compute_quality_report(
    *,
    program_id: str,
    programs_root: Path,
) -> QualityReport:
    """Compute the G-floor quality report from the labeled corpus + candidate store."""
    report = QualityReport(program_id=program_id)
    path = _corpus_path(program_id, programs_root)
    if not path.exists():
        report.failures.append(
            f"no labeled corpus at {path} — annotate via `vertex ledger triage list` (OA-3)"
        )
        return report

    rows = read_jsonl_records(path)
    report.n_total = len(rows)
    if report.n_total == 0:
        report.failures.append(f"labeled corpus {path} is empty")
        return report
    if report.n_total < SMALL_N_WARN:
        report.warnings.append(
            f"small sample (N={report.n_total} < {SMALL_N_WARN}) — uncertainty ±22pp; "
            "target ≥50 before non-pilot rollout"
        )

    # Index staged candidates by candidate_id → proposed_event_type + grounded flag.
    candidates = load_pending_candidates(program_id, programs_root=programs_root)
    by_id: dict[str, Any] = {c.candidate_id: c for c in candidates}

    # Per-type accumulators: (n_expected, n_proposed_correct, n_proposed_total_for_type).
    per_type_n: dict[str, int] = {}
    per_type_correct: dict[str, int] = {}
    per_type_predicted: dict[str, int] = {}   # times a type was predicted (for precision)

    # Kappa accumulators (label vs second_label).
    kappa_pairs: list[tuple[str, str]] = []
    accept_grounded = 0

    # Population split (§33.3.1 / corpus design, v2.22): the corpus carries an
    # optional ``population`` field — ``"extraction"`` (AI-extracted from source
    # documents; grounded) vs ``"import"`` (trusted YAML backfill; not grounded).
    # The headline gates (G-xtract-prec, G-accept-prec) measure the *extraction*
    # population only, because G-accept-prec rewards grounding and the import
    # population is structurally ungroundable. The import population is reported
    # separately as import-fidelity context (it should be ~100% type-correct).
    pop_correct = {"extraction": 0, "import": 0, "unknown": 0}
    pop_matched = {"extraction": 0, "import": 0, "unknown": 0}
    pop_accept_n = {"extraction": 0, "import": 0, "unknown": 0}
    pop_accept_correct = {"extraction": 0, "import": 0, "unknown": 0}

    for row in rows:
        cid = str(row.get("candidate_id", "")).strip()
        expected = str(row.get("expected_event_type", "")).strip()
        label = str(row.get("label", "")).strip().lower()
        if not cid or not expected or not label:
            report.warnings.append(f"skipping malformed corpus row (missing fields): {row!r}")
            continue
        if label not in ("accept", "reject"):
            report.warnings.append(f"skipping corpus row with unknown label {label!r}: {cid}")
            continue

        cand = by_id.get(cid)
        if cand is None:
            report.warnings.append(f"corpus row {cid} has no matching staged candidate (skipped)")
            continue
        report.n_matched_candidate += 1

        population = str(row.get("population", "")).strip().lower() or "unknown"
        if population not in pop_matched:
            population = "unknown"

        proposed = getattr(cand, "proposed_event_type", "")
        grounded = bool(getattr(cand, "evidence_refs", ()) or ())
        type_correct = (proposed == expected)

        pop_matched[population] += 1
        if type_correct:
            pop_correct[population] += 1

        per_type_n[expected] = per_type_n.get(expected, 0) + 1
        per_type_predicted[proposed] = per_type_predicted.get(proposed, 0) + 1
        if type_correct:
            per_type_correct[expected] = per_type_correct.get(expected, 0) + 1

        if label == "accept":
            pop_accept_n[population] += 1
            if grounded:
                accept_grounded += 1
            if type_correct and grounded:
                report.accept_correct += 1
                pop_accept_correct[population] += 1
        else:
            report.reject_n += 1

        second = row.get("second_label")
        if isinstance(second, str) and second.strip().lower() in ("accept", "reject"):
            kappa_pairs.append((label, second.strip().lower()))

    # ── Precision / recall / F1 metrics ─────────────────────────────────────
    # Population split (Option 1, v2.22): when the corpus carries a ``population``
    # field, headline gates measure the *extraction* population (AI-extracted,
    # grounded). The import population (trusted YAML backfill) is reported
    # separately as import-fidelity context. For legacy corpora without a
    # ``population`` field, behavior is unchanged (whole-corpus accounting).
    has_population_field = any(str(row.get("population", "")).strip() for row in rows)
    if has_population_field and pop_matched["extraction"] > 0:
        report.accept_n = pop_accept_n["extraction"]
        report.accept_correct = pop_accept_correct["extraction"]
        report.correct_type = pop_correct["extraction"]
        xtract_denom = pop_matched["extraction"]
    else:
        report.accept_n = sum(pop_accept_n.values())
        report.accept_correct = sum(pop_accept_correct.values())
        report.correct_type = sum(pop_correct.values())
        xtract_denom = report.n_matched_candidate
    if xtract_denom > 0:
        report.g_xtract_prec = report.correct_type / xtract_denom
        lo, hi = _wilson_ci(report.correct_type, xtract_denom)
        report.g_xtract_prec_ci_low, report.g_xtract_prec_ci_high = lo, hi
    if report.accept_n > 0:
        report.g_accept_prec = report.accept_correct / report.accept_n
        lo, hi = _wilson_ci(report.accept_correct, report.accept_n)
        report.g_accept_prec_ci_low, report.g_accept_prec_ci_high = lo, hi
    if report.n_matched_candidate > 0:
        report.g_reject_rate = report.reject_n / report.n_matched_candidate

    # Import-fidelity context (reported, not gated): did the YAML backfill
    # preserve event types? Should be ~100%; a drop signals an import bug.
    if has_population_field and pop_matched["import"] > 0:
        import_fidelity = pop_correct["import"] / pop_matched["import"]
        report.warnings.append(
            f"import-fidelity (YAML backfill, N={pop_matched['import']}): "
            f"{pop_correct['import']}/{pop_matched['import']} = {import_fidelity:.1%} type-correct "
            f"(excluded from G-xtract-prec/G-accept-prec; not grounded by construction)"
        )

    # ── Abstention / acceptance coverage ────────────────────────────────────
    report.abstention_coverage = (
        report.n_matched_candidate / report.n_total if report.n_total > 0 else 0.0
    )
    report.acceptance_coverage = (
        accept_grounded / report.accept_n if report.accept_n > 0 else 0.0
    )

    # ── Per-type recall (+ precision + F1) ──────────────────────────────────
    # ``no_event`` is the false-positive sentinel (a labeled candidate where no
    # real event exists), not an event type the extractor predicts. It is a
    # precision signal (it lowers G-xtract-prec via type-mismatch), never a
    # recall target — exclude it from the recall table so it isn't gated.
    for et in sorted(per_type_n):
        if et == "no_event":
            continue
        n = per_type_n[et]
        correct = per_type_correct.get(et, 0)
        predicted_total = per_type_predicted.get(et, 0)
        is_critical = any(et.startswith(p) for p in CRITICAL_FAMILY_PREFIXES)
        if n < PER_TYPE_MIN_SAMPLE:
            report.per_type.append(PerTypeRecall(
                event_type=et, n=n, correct=correct, recall=0.0,
                insufficient_sample=True,
                is_critical_family=is_critical,
            ))
        else:
            recall_val = correct / n
            prec_val = correct / predicted_total if predicted_total > 0 else 0.0
            denom = prec_val + recall_val
            f1_val = (2 * prec_val * recall_val / denom) if denom > 0 else 0.0
            report.per_type.append(PerTypeRecall(
                event_type=et, n=n, correct=correct, recall=recall_val,
                precision=prec_val, f1=f1_val,
                is_critical_family=is_critical,
            ))

    # Macro-F1 (average over types with N≥5).
    valid_f1s = [p.f1 for p in report.per_type if not p.insufficient_sample]
    if valid_f1s:
        report.macro_f1 = sum(valid_f1s) / len(valid_f1s)

    # ── Kappa ────────────────────────────────────────────────────────────────
    if len(kappa_pairs) >= KAPPA_MIN_RECORDS:
        report.kappa_n = len(kappa_pairs)
        report.kappa = _cohen_kappa(kappa_pairs[:20])

    # ── Gates ────────────────────────────────────────────────────────────────
    report.gates_passed["g_xtract_prec>=0.80"] = report.g_xtract_prec >= G_XTRACT_PREC_FLOOR
    if not report.gates_passed["g_xtract_prec>=0.80"]:
        report.failures.append(
            f"G-xtract-prec {report.g_xtract_prec:.1%} < {G_XTRACT_PREC_FLOOR:.0%} "
            f"(95% CI: {report.g_xtract_prec_ci_low:.1%}–{report.g_xtract_prec_ci_high:.1%})"
        )
    report.gates_passed["g_accept_prec>=0.85"] = report.g_accept_prec >= G_ACCEPT_PREC_FLOOR
    if not report.gates_passed["g_accept_prec>=0.85"]:
        report.failures.append(
            f"G-accept-prec {report.g_accept_prec:.1%} < {G_ACCEPT_PREC_FLOOR:.0%} "
            f"(95% CI: {report.g_accept_prec_ci_low:.1%}–{report.g_accept_prec_ci_high:.1%})"
        )
    for p in report.per_type:
        if p.insufficient_sample:
            continue
        floor = G_CRITICAL_FAMILY_RECALL_FLOOR if p.is_critical_family else G_PER_TYPE_RECALL_FLOOR
        key = f"recall[{p.event_type}]>={floor:.0%}"
        passed = p.recall >= floor
        report.gates_passed[key] = passed
        if not passed:
            family_note = " (critical family — elevated floor)" if p.is_critical_family else ""
            report.failures.append(
                f"per-type recall[{p.event_type}] {p.recall:.1%} < {floor:.0%} (N={p.n}){family_note}"
            )
    if report.kappa is not None:
        report.gates_passed["kappa>=0.70"] = report.kappa >= G_KAPPA_FLOOR
        if report.kappa < G_KAPPA_FLOOR:
            report.failures.append(
                f"inter-annotator kappa {report.kappa:.3f} < {G_KAPPA_FLOOR} (N={report.kappa_n}) — "
                "align guidelines and re-annotate"
            )

    return report


def _wilson_ci(successes: int, total: int) -> tuple[float, float]:
    """Wilson score 95 % confidence interval (lower, upper)."""
    if total == 0:
        return (0.0, 1.0)
    p_hat = successes / total
    z = _WILSON_Z
    denom = 1 + z ** 2 / total
    center = (p_hat + z ** 2 / (2 * total)) / denom
    margin = (
        z * math.sqrt(p_hat * (1 - p_hat) / total + z ** 2 / (4 * total ** 2))
        / denom
    )
    return (max(0.0, center - margin), min(1.0, center + margin))


def wilson_ci(successes: int, total: int) -> tuple[float, float]:
    """Public Wilson score 95% confidence interval helper."""
    return _wilson_ci(successes, total)


def minimum_successes_for_wilson_floor(*, total: int, floor: float) -> int | None:
    """Return the fewest successes whose Wilson 95% lower bound clears *floor*."""
    if total <= 0:
        return None
    for successes in range(total + 1):
        if _wilson_ci(successes, total)[0] >= floor:
            return successes
    return None


def minimum_total_for_perfect_wilson_floor(*, floor: float, max_total: int = 10_000) -> int:
    """Return minimum N where N/N has a Wilson 95% lower bound above *floor*."""
    for total in range(1, max_total + 1):
        if _wilson_ci(total, total)[0] >= floor:
            return total
    raise ValueError(f"floor {floor} cannot be reached by max_total={max_total}")


def activation_denominator_plan(*, data_floor: int = 30) -> tuple[WilsonDenominatorRequirement, ...]:
    """Minimum denominator guidance for the activation corpus annotation sprint."""
    specs = (
        ("g_xtract_prec_ci_low", G_XTRACT_PREC_FLOOR),
        ("g_accept_prec_ci_low", G_ACCEPT_PREC_FLOOR),
        ("critical_recall_ci_low", G_CRITICAL_FAMILY_RECALL_FLOOR),
    )
    return tuple(
        WilsonDenominatorRequirement(
            metric=metric,
            floor=floor,
            min_total_if_perfect=minimum_total_for_perfect_wilson_floor(floor=floor),
            min_successes_at_data_floor=minimum_successes_for_wilson_floor(total=data_floor, floor=floor),
            data_floor=data_floor,
            ci_low_at_data_floor_if_perfect=_wilson_ci(data_floor, data_floor)[0],
        )
        for metric, floor in specs
    )


def _cohen_kappa(pairs: list[tuple[str, str]]) -> float:
    """Cohen's kappa over (label, second_label) pairs (both ∈ {accept, reject})."""
    if not pairs:
        return 0.0
    n = len(pairs)
    labels = ("accept", "reject")
    observed = sum(1 for a, b in pairs if a == b) / n
    # Marginals.
    pa = sum(1 for a, _ in pairs if a == "accept") / n
    pb = sum(1 for _, b in pairs if b == "accept") / n
    expected = pa * pb + (1 - pa) * (1 - pb)
    if expected == 1.0:
        return 1.0  # perfect agreement, no variance
    return (observed - expected) / (1 - expected)


def verify_judge_independence(*, extractor_deployment: str | None = None,
                              judge_deployment: str | None = None) -> tuple[bool, str]:
    """P2-11 code enforcement: assert the judge and extractor deployments differ.

    Reads ``VERTEX_AI_DEPLOYMENT`` (extractor) and ``VERTEX_AI_JUDGE_DEPLOYMENT``
    (judge) from env when not passed explicitly. Returns (ok, message). The judge
    must never score its own output.
    """
    ext = extractor_deployment or os.environ.get("VERTEX_AI_DEPLOYMENT")
    jud = judge_deployment or os.environ.get("VERTEX_AI_JUDGE_DEPLOYMENT")
    if not ext:
        return False, "VERTEX_AI_DEPLOYMENT (extractor) not set — cannot verify judge independence"
    if not jud:
        return False, ("VERTEX_AI_JUDGE_DEPLOYMENT not set — the LLM-as-judge would reuse the "
                        "extractor deployment (judge scores its own output). Set a separate "
                        "judge deployment before confirming G-llm.")
    if ext.strip() == jud.strip():
        return False, (f"judge deployment {jud!r} == extractor deployment {ext!r} — "
                       "the judge must use a different deployment/model (P2-11)")
    return True, f"judge deployment {jud!r} differs from extractor {ext!r} ✓"


def render_report_human(report: QualityReport) -> str:
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append(f"REV Quality-Floor Report — {report.program_id}")
    lines.append("=" * 70)
    lines.append(f"  N (labeled):            {report.n_total}")
    lines.append(f"  N matched candidate:    {report.n_matched_candidate}")
    lines.append(f"  Abstention coverage:    {report.abstention_coverage:.1%}  (matched/total)")
    lines.append(f"  Acceptance coverage:    {report.acceptance_coverage:.1%}  (grounded accepts/accepts)")
    lines.append("")
    ci_prec = f"[{report.g_xtract_prec_ci_low:.1%}–{report.g_xtract_prec_ci_high:.1%}]"
    lines.append(f"  G-xtract-prec:          {report.g_xtract_prec:.1%}  95%CI={ci_prec}  (gate >=80%)")
    ci_acc = f"[{report.g_accept_prec_ci_low:.1%}–{report.g_accept_prec_ci_high:.1%}]"
    lines.append(f"  G-accept-prec:          {report.g_accept_prec:.1%}  95%CI={ci_acc}  (gate >=85%)")
    lines.append(f"  G-reject-rate:          {report.g_reject_rate:.1%}  (reported)")
    if report.macro_f1 is not None:
        lines.append(f"  Macro-F1:               {report.macro_f1:.1%}  (average over types N>=5; reported)")
    lines.append("")
    lines.append("Per-event-type recall:")
    if not report.per_type:
        lines.append("  (no types)")
    for p in report.per_type:
        if p.insufficient_sample:
            tag = "insufficient_sample_for_gate"
        else:
            crit = "*" if p.is_critical_family else " "
            floor = G_CRITICAL_FAMILY_RECALL_FLOOR if p.is_critical_family else G_PER_TYPE_RECALL_FLOOR
            passed = "PASS" if p.recall >= floor else "FAIL"
            tag = f"recall={p.recall:.1%} prec={p.precision:.1%} F1={p.f1:.1%} {passed}{crit}"
        lines.append(f"  {p.event_type:30} N={p.n:3}  {tag}")
    lines.append("  (* = critical family, elevated floor 60%)")
    lines.append("")
    if report.kappa is not None:
        lines.append(f"  Cohen's kappa:          {report.kappa:.3f}  (N={report.kappa_n}; gate >=0.70)")
    else:
        lines.append("  Cohen's kappa:          not_available (no second_label records)")
    lines.append("")
    if report.warnings:
        lines.append("WARNINGS:")
        for w in report.warnings:
            lines.append(f"  - {w}")
        lines.append("")
    if report.failures:
        lines.append("GATE FAILURES:")
        for f in report.failures:
            lines.append(f"  X {f}")
        lines.append("")
        lines.append("RESULT: FAIL (exits 1)")
    else:
        lines.append("RESULT: PASS")
    return "\n".join(lines)


__all__ = [
    "QualityReport",
    "PerTypeRecall",
    "WilsonDenominatorRequirement",
    "compute_quality_report",
    "activation_denominator_plan",
    "minimum_successes_for_wilson_floor",
    "minimum_total_for_perfect_wilson_floor",
    "render_report_human",
    "wilson_ci",
    "verify_judge_independence",
    "G_XTRACT_PREC_FLOOR",
    "G_ACCEPT_PREC_FLOOR",
    "G_PER_TYPE_RECALL_FLOOR",
    "G_CRITICAL_FAMILY_RECALL_FLOOR",
    "G_KAPPA_FLOOR",
    "PER_TYPE_MIN_SAMPLE",
    "CRITICAL_FAMILY_PREFIXES",
]
