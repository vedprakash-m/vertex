# ADR-0016: Per-class quality floors, denominator plan, and corpus staffing plan (ADF-W0.4)

**Date:** 2026-07-13
**Status:** Accepted (2026-07-13)
**Workstream (from `specs/arch-data-fix.md`):** ADF-W0.4
**Author(s):** Vertex engineering (drafted per live decision-by-decision
session)
**Approver(s):** the Platform DRI (Evaluator role per
ADR-0013)

## Context

The spec requires a Wilson z=1.96 (95% CI) sample plan with ratified
recall/coverage/abstention floors, explicitly warning not to reuse "the
existing 90% helper" unchanged. Code discovery found:

- `src/core/rev/entity_binding_gate.py` — a 90%-CI (z=1.645) helper with
  `PRECISION_FLOOR=0.95`, `COVERAGE_FLOOR=0.80`. This is the helper the
  spec says not to reuse as-is (wrong confidence level for ADF
  certification).
- `src/core/rev/quality_metrics.py` — already has a correct 95%-CI
  (z=1.96) implementation with `minimum_total_for_perfect_wilson_floor`/
  `minimum_successes_for_wilson_floor`/`activation_denominator_plan`
  already built, and existing floors `G_XTRACT_PREC_FLOOR=0.80`,
  `G_ACCEPT_PREC_FLOOR=0.85`, `G_PER_TYPE_RECALL_FLOOR=0.50`,
  `G_CRITICAL_FAMILY_RECALL_FLOOR=0.60`, `G_KAPPA_FLOOR=0.70`.
- The spec's own fleet-certification bar (ADF-OM3B) states critical
  recall ≥0.90 and precision lower-bound ≥0.95 — the recall number is
  stricter than what's gated in code today (0.60); no ADF-specific
  coverage or abstention floor was ratified anywhere.

## Decision

1. **Reuse, don't reimplement.** `src/core/adf_quality_floors.py` (new)
   imports `quality_metrics.py`'s existing z=1.96 Wilson primitives
   directly rather than building a parallel implementation — this already
   satisfies the spec's requirement (the correct-confidence helper
   already existed; the spec's warning was about the *other*, 90%-CI
   helper, which this module does not touch).

2. **Ratified floors** (all decided live with the Platform DRI, 2026-07-13):

   | Floor | Value | Rationale |
   |---|---|---|
   | Precision | 0.95 | Matches ADF-OM3B's stated bound and the pre-existing `entity_binding_gate.py` precedent — no tier split needed, both agree already. |
   | Critical-family recall — **advisory tier** | 0.60 | Matches the existing operative `G_CRITICAL_FAMILY_RECALL_FLOOR` — achievable now, unblocks ADF-OM3A single-program advisory reporting without inventing a new, currently-unmeasured bar. |
   | Critical-family recall — **fleet tier** | 0.90 | Matches ADF-OM3B exactly. Only required for fleet certification (≥3 programs), never blocks single-program advisory operation. |
   | Coverage | 0.80 | Matches `entity_binding_gate.py`'s existing `COVERAGE_FLOOR` — one number across entity-binding, risk, and dependency classes rather than a bespoke per-domain figure. |
   | Abstention | 0.90 | Promotes `quality_metrics.py`'s previously reported-only "abstention coverage" metric to a gated floor: at least 90% of true candidates must be staged before any human reviews anything; the extractor may silently drop at most 10%. |

3. **Two-tier design is deliberate, not a compromise.** The spec itself
   frames ADF-OM3A (single-program advisory) and ADF-OM3B (fleet-
   certified) as different authority levels with different evidence
   requirements (§3.4.2: "Single-program operation may earn advisory
   authority from all available labeled evidence but may not claim fleet
   certification when the denominator is insufficient"). Only the
   critical-recall floor needed a tier split because it is the only floor
   where the spec's explicit target (0.90) diverges from the code's
   current operative value (0.60); every other floor already agreed at
   both tiers.

4. **Corpus staffing plan (the acceptance evidence's second half).**
   Per ADR-0013 (RACI), the current corpus has exactly one annotator
   (the single operator / Platform DRI). This ADR does **not** invent a second annotator —
   it states the resulting, honest denominator picture:

   - Every floor's `min_total_if_perfect` (computed by
     `adf_denominator_plan()`) is the *minimum sample count to trust a
     100%-observed rate* at 95% confidence — a **necessary**, not
     sufficient, condition. Cohen's κ (inter-annotator agreement) remains
     unmeasurable with one annotator regardless of sample count (ADR-0006
     Amendments A2-A4 already established this precedent for the REV
     corpus).
   - **Fleet certification (ADF-OM3B, ADF-W6.2) requires both**: (a) the
     per-class denominators this ADR computes, met across **≥3
     operational programs** (ADF-W0.12, still open), **and** (b) a second
     independent annotator for κ (ADR-0013, still UNFILLED). Neither
     condition is close to being met today (one program with confirmed
     issues, one annotator).
   - **Single-program advisory authority (ADF-OM3A) is achievable now**:
     it only requires reporting observed precision/recall/coverage/
     abstention by class against the ratified advisory-tier floors above,
     with honest denominator-insufficiency labeling where the sample is
     too small — no second annotator or second program required.

## BL-A2 reconciliation

BL-A2 ("corpus certification limitation": single-program advisory vs.
≥3-program fleet certification) is not a separate open question — it is
exactly what this ADR's two-tier design and staffing-plan section already
resolve. There is nothing further to reconcile beyond what's stated
above: BL-A2 stays open as a **tracked, honest limitation** (not a bug)
until ADF-W0.12 and ADR-0013's second-annotator gap both close.

## Alternatives considered

| Option | Why not chosen |
|---|---|
| Build a new parameterized Wilson function (z as an explicit argument) instead of reusing `quality_metrics.py`'s hardcoded `_WILSON_Z=1.96` | The spec's literal requirement (z=1.96) is already what that module hardcodes — building a more general parameterized version would be real but unnecessary engineering with no current second use case (no other z value is needed anywhere in ADF). Deferred until a genuine need for a different confidence level appears. |
| Single uniform floor set (no advisory/fleet split) | Rejected per Decision 1's reasoning above — would either strand ADF-OM3A behind an unmeasured 0.90 bar with no near-term path to any advisory authority, or silently under-shoot ADF-OM3B's explicit spec requirement. |

## Consequences

**Easier:** ADF-OM3A single-program advisory reporting has a concrete,
ratified floor set to report against immediately; the fleet-certification
gap (ADF-OM3B) is honestly staged behind its two real prerequisites
instead of being conflated with the advisory bar.

**Harder:** none new — the honest staffing/denominator picture was
already implied by ADR-0013; this ADR just makes it load-bearing for
ADF-W0.4's specific acceptance evidence instead of leaving it implicit.

## References

- `src/core/adf_quality_floors.py`, `tests/unit/test_adf_quality_floors.py`
- `src/core/rev/quality_metrics.py` (reused Wilson primitives)
- `src/core/rev/entity_binding_gate.py` (the 90%-CI helper explicitly NOT reused)
- `specs/arch-data-fix.md` §3.4.2, ADF-OM3A/ADF-OM3B, BL-A2
- Related: ADR-0013 (RACI — second-annotator gap), ADR-0006 Amendments
  A2-A4 (precedent: single-annotator results stay "preliminary")
