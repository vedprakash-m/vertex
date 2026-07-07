# ADR-0005: Tiered Router as Canonical Fact-Routing Entrypoint

**Date:** 2026-06-08
**Status:** Accepted
**Workstream:** D-06 (tiered router — rev. 352)
**Author(s):** Vertex engineering
**Approver(s):** n/a

## Context

Fact retrieval was fragmented across three separate code paths: the legacy `ProgramFactStore` direct
access pattern, the SoR-aware `build_slice_source_health_summary` path, and ad-hoc per-command
queries in the Orchestrator. Each path had different behavior for shadow-write vs. primary-mode
programs, creating D-06 ("no centralized Tier 0→1→2 decision recording"). The D-06 debt spec called
for a centralized `tiered_router.py` that records §10.6 decision provenance on every query.

## Decision

`src/core/tiered_router.py` is the canonical Tier 0→1→2 entrypoint for all fact-routing. It:
(1) checks Tier 0 (in-session memoized cache), (2) routes to Tier 1 (Program Fact Store primary or
shadow), (3) falls back to Tier 2 (legacy JSON/YAML). Every routing decision records a
`fact_source_decision` event with `tier`, `source_store`, `reason`, and `program_id`. The
`claim_extractor` reference implementation adopts the tiered router first; all other callers migrate
via the WI-1.2 ratchet gate (INV-SG-10 AST gate, per-module). D-05 (legacy-path retirement) still
defaults to the legacy path until operators flip per-family SoR mode to `primary`.

## Consequences

Easier: routing behavior is auditable and testable; the shadow-write → primary flip is a config
change, not a code change; D-05/D-06 retirement ratchet tracks adoption mechanically. Harder:
callers must import from `tiered_router` instead of calling store methods directly (1/15 adoption at
rev. 352; others migrate opportunistically). Explicitly rejected: a per-command routing switch
(duplicates the logic in 106 command files).

## References

- `src/core/tiered_router.py` — implementation
- `tests/contracts/test_architecture_fitness.py` — INV-SG-10 adoption ratchet
- `specs/vertex-tech-spec.md §9.16` — F1→F4 migration roadmap
- `specs/gaps.md D-06`
- ADR-0001 (zone boundary — tiered_router lives in Zone A)
