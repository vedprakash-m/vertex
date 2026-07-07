# Graduation Record — `blurb_generator`

**Feature:** AI-generated workstream blurb / section narrative (Zone B `src/ai/blurb_generator.py`)
**Status:** Proposed (candidate for WS-5a sign-off)
**Date proposed:** 2026-06-16
**Proposer:** — (operator to assign)
**AI Safety Approver required:** per `governance/roles/ai-safety-approver.yaml`

## What the feature does

`blurb_generator` takes a set of facts (workstream signals, ADO work items, IcM incidents) and produces
a short narrative paragraph for inclusion in the weekly program status email. It is called from
`gather.py` → `report.py` → `blurb_generator.generate(...)`. All output passes through
`process_generated_text` (injection scanner + ban-list) before reaching the email renderer.

## Safety pipeline verification

| Check | Status |
|-------|--------|
| `process_generated_text` wrapper | ✅ enforced (contract test `test_ai_safety_pipeline_enforced`) |
| Ban-list filter (QG-4/QG-9) | ✅ applied at render time |
| `frontier_eligible` kill switch | ✅ in `ai_policy.yaml`; checked by `deployment_fallback.py` |
| AI telemetry row written | ✅ per-call row in `ai_telemetry.jsonl` |
| AI proposal TTL GC | ✅ `AI_PROPOSAL_TTL_DAYS = 14` in `ai_proposal_store.py` |

## Graduation checklist (for AI Safety Approver)

- [ ] Review ≥3 blurb outputs against source ADO evidence (semantic similarity ≥0.82, QG-23)
- [ ] Confirm ban-list covers all restricted terms for the target program(s)
- [ ] Confirm no personally identifying information surfaces in blurb text
- [ ] Sign below and commit

## Sign-off

```
Approved-by: [AI Safety Approver name]
Date: YYYY-MM-DD
Notes: [any caveats or follow-up items]
```

## References

- `specs/vertex-prd.md §14.8` — AI graduation gate (T0-2)
- `governance/roles/ai-safety-approver.md` — role charter
- `src/ai/blurb_generator.py` — implementation
- ADR-0003 — Per-program config (no hardcoded routing)
