# Outcome metrics (OM-1/2/4/5)

*(specs/backlog.md BL-C7 — lifted out of the archived, untracked
`.archive/specs/arch-fix.md` §11 "Definition of Done" and §0.2's risk table,
so the acceptance bar for Part A's canary window is durably available.)*

`arch-fix.md`'s Part A ships independently on the condition that "MVP
outcome metrics OM-1/2/4/5 [are] observed over the canary window" (§11).
BL-C7 found that nothing was instrumented to collect them — an 8-week
canary window could elapse with these still unmeasured. This document is
the durable acceptance bar; `src/core/outcome_metrics.py` is the live
measurement.

## Definitions (verbatim from the archived source, §0.2)

- **OM-1** — zero hallucinated/ungrounded facts reach a published
  newsletter, over an 8-week window.
- **OM-2** — zero duplicate ADO entities created, over 6 months.
- **OM-3** — measured frontier $/cycle reduced ≥ the Phase-0-ratified
  target with no output-quality regression (operator-blind A/B on the
  pilot). *(Not in BL-C7's scope — a cost metric, not an AI-safety/
  actuation outcome. Not measured here.)*
- **OM-4** — zero unaudited AI outputs consumed.
- **OM-5** — operator can complete a weekly cycle with zero forced
  overrides in the steady state.

## Live measurement status (2026-07-22)

| Metric | Confidence tier | Why |
|---|---|---|
| OM-1 | `unavailable` | Depends on BL-C2's AI safety boundary (semantic validation / hallucination detection) actually covering the REV extractor/judge path — the highest-consequence path, and the one BL-C2's own reopening found bypasses the gateway entirely. **OM-1 is unmeasurable on the path that matters until BL-C2 closes; that is itself the finding**, not a placeholder to paper over. |
| OM-2 | `unavailable` | `src/core/actuation_outbox.py`'s outbox is not yet wired to a live ADO mutation domain (per `cockpit_builder.py`'s own `_build_reliability_summary` docstring) — there is currently no real create-task traffic to compute a duplicate-detection ratio from. The **related but distinct** `duplicate_preventions` counter (real, counts `actuation.duplicate_prevented.v1` ledger events from the search-before-create safeguard) is already surfaced in the reliability cockpit summary — it measures prevention *attempts*, not the OM-2 outcome itself. |
| OM-4 | **`measured`** | `src/core/quality_gates/ai_release_audit.py`'s ledger events (`ai.application_receipt.v1`, `ai.release_decision.v1`) are real, `SOURCE_AUTHORITATIVE` records written on every AI consumption BL-C3 covers. `audit_coverage = (consumed AI runs with a durable 'released' terminal) / (all consumed AI runs)`. `1.0` = fully compliant. Wired into `ReliabilityCockpitSummary.audit_coverage` (previously a hardcoded `None` placeholder). |
| OM-5 | `unavailable` | No measurement protocol is defined yet — "operator friction" needs an explicit answer to *what is timed, for whom, against what baseline* before anything can be counted. This is a product/protocol design decision (mirroring BL-D3's own pre-registration requirement), not something inferable from existing telemetry. See "What OM-5 needs" below. |

**Never silently promote a tier.** `ValueConfidence.PROXY`/`UNAVAILABLE`
metrics must never be presented as `MEASURED` (INV-ADF-11, `cockpit_models.py`)
— an honest "we don't know yet" is the entire point of this document
existing before the canary window starts, not after.

## What OM-1 needs to become measurable

BL-C2's remaining scope (see `specs/backlog.md`): wire the REV extractor
and REV judge call sites through `AISchemaGateway`'s semantic validation
(not just schema validation), and land a durable per-fact grounding/
hallucination check whose negative result is queryable. Once that exists,
`compute_om1_hallucination_rate` (not yet written) can count published
facts lacking a grounding record over the trailing 8 weeks.

## What OM-2 needs to become measurable

An actual live mutation domain wired through `src/core/actuation_outbox.py`
(tracked separately, not a BL-C7 item) so `remote_response_hash` values
exist to correlate. Once real create-task traffic flows through the
outbox, `compute_om2_duplicate_entities` (not yet written) can group
succeeded outbox entries by `remote_response_hash` and flag any hash
shared by more than one distinct `outbox_id`.

## What OM-5 needs

A pre-registered protocol, mirroring BL-D3's own requirement for exactly
this class of ambiguous human-in-the-loop metric:
1. **What is timed** — e.g. wall-clock time from `vertex gather` start to
   the operator's last manual edit/override before publish.
2. **For whom** — this workspace's sole operator, or a defined cohort once
   BL-G1's fleet exists.
3. **Against what baseline** — the pre-registry manual process, or a fixed
   historical week.
4. **What counts as a "forced override"** — every `--force-*` flag usage?
   Every manual edit to an AI-proposed value? Needs an enumerated list.

This is a product/DRI decision, not something this document can resolve
unilaterally — recorded here so it isn't lost, matching BL-C7's own
instruction to "specify what is timed, for whom, against what baseline."

## Definition of done

This document (and `src/core/outcome_metrics.py`) close when: (1) all four
OM-1/2/4/5 metrics have a live, queryable value with an honest confidence
tier surfaced in `vertex cockpit`/`vertex doctor` — **done as of 2026-07-22**
(OM-4 measured; OM-1/2/5 honestly unavailable with a stated path to
measurability) — and (2) before BL-C6's canary window is declared started,
someone re-reads this table and confirms which metrics have flipped from
`unavailable`/`proxy` to `measured` in the interim.
