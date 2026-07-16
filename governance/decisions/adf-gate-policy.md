# ADF Quality-Gate Policy Matrix (ADF-W0.9)

**Status:** Ratified for Phase 0 registration. Enforcement activates per the
`Activates` column as the owning slice lands (`specs/arch-data-fix.md`
Section 12.1 is authoritative; this doc is a generated reference, not a
second source of truth — see
`src/core/quality_gates/gate_registry.py::QG_POLICY_MATRIX`).

| Gate | Name | Enforcement point | Enforce behavior | Forceable | Activates |
|---|---|---|---|---|---|
| QG-29 | AI Release Audit | Before AI result consumption | Discard AI result and use deterministic fallback; state mutation remains blocked | No | Slice 2 per migrated feature |
| QG-30 | Source Completeness | Gather and confirm | Required unbound/unknown source blocks; transient failures require explicit unexpired waiver through existing `source_waiver_store.py` | Only through waiver policy | Slice 2 |
| QG-31 | Channel Budget | Channel runtime and report | Cancel/degrade over-budget optional channel; any inline-prohibited invocation blocks that path | No | Slice 1 |
| QG-32 | Context Budget | Before provider invocation | Reject compile and use bounded fallback | No | Slice 2 |
| QG-33 | AI Economics | Before and after provider invocation | Spend ceiling blocks additional frontier call; avoidance miss is cockpit/advisory until certification | Spend: no; avoidance: n/a | Phase 0 telemetry, Slice 5 enforcement |
| QG-34 | Cross-Surface Consistency | Before artifact write and confirm | Material conflict blocks affected artifact; non-material conflict warns | Material: no | Slice 2 |
| QG-35 | Actuation Intent | Before outbox enqueue and dispatch | Missing/stale intent, approval, preflight, or receipt blocks mutation | No | Slice 1 |
| QG-36 | Value Evidence | Cockpit/value render | Unsupported metric is hidden and marked unavailable; never blocks program publication | n/a | Phase 0 |
| QG-37 | State Authority | Startup of mutating command and doctor | Ambiguous authoritative path blocks mutation | No | Slice 1 |
| QG-38 | Cockpit Freshness | Cockpit build/show | Stale snapshot displays age and warns; rebuild when safe | n/a | Phase 0 |
| QG-39 | Source Semantic Integrity | Gather/report/confirm | Missing required relation/metric semantics blocks affected material section; optional source warns | Through existing source-waiver policy | Slice 2 |
| QG-40 | Extraction Certification | Proposal authority promotion | Uncertified risk/inferred-dependency/entity-binding classes remain advisory and cannot earn automatic authority | No | Slice 4 pilot reporting; fleet certification in ADF-W6.2 |

## One economics enforcement path (QG-33)

QG-33 does not duplicate the pre-existing QG-WS5B AI-budget gate
(`src/core/quality_gates/ai_budget.py`). It is a relabeled delegate
(`src/core/quality_gates/adf_economics.py::evaluate_qg33_ai_economics_gate`)
that calls the same evaluator and republishes the result under the `QG-33`
ID. Any future QG-33 spend-ceiling or avoidance-rate logic must extend
`ai_budget.py`, not fork a parallel implementation.

## Phase-0-active gates

Per Section 12.1 and the Phase-0 operating-principle note in Section 12
("During observe mode, new QG-30-40 findings are recorded but do not add
hard confirmation blocks except QG-29, QG-35, and QG-37 safety/authority
failures"), only QG-33 (telemetry only), QG-36, and QG-38 are active in
Phase 0:

- **QG-33** — telemetry/advisory only until Slice 5 enforcement; see above.
- **QG-36 Value Evidence** and **QG-38 Cockpit Freshness** — enforced
  procedurally inside `src/core/cockpit_builder.py` (ADF-W0.8) rather than as
  standalone `evaluate_*` functions, because both gates only have one
  consumer (the cockpit render path) today. They gain a dedicated evaluator
  module if and when a second consumer needs the same check.

All other rows (QG-30, QG-31, QG-32, QG-34, QG-35, QG-37, QG-39, QG-40)
remain **reserved** (`gate_registry.RESERVED_GATE_IDS`): the ID is spoken
for, but no evaluator exists yet. They gain real enforcement when their
owning Slice-1/2/4 work item lands, per the Appendix C brief for that item.
