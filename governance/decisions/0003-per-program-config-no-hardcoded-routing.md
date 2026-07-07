# ADR-0003: Per-Program Config for Decision Source Routing (No Hardcoded Defaults)

**Date:** 2026-06-16
**Status:** Accepted
**Workstream:** GAP-14 (Zone-A neutrality — decision source routing)
**Author(s):** Vertex engineering
**Approver(s):** n/a

## Context

`decision_source_defaults.py` contained two hardcoded if-blocks that returned Acme-specific and
Contoso-specific decision source defaults based on `program_id == "acme"` and
`source_id in {"lt_deck", "contoso_daily"}`. This made the function non-neutral and prevented Fabrikam
(and future programs) from using the same routing path without code changes. The per-program values
were already present in `programs/acme/slice_contracts.yaml` under `decision_sources`.

## Decision

`get_legacy_decision_source_default` now always returns `None`. Production callers pass
`allow_legacy_decision_source_fallback=False`, so the function is effectively dead code on the live
path. The per-program defaults are authoritative in `programs/<id>/slice_contracts.yaml`. The function
and the `LegacyDecisionSourceDefault` dataclass are preserved for API compatibility with the
legacy-compat test path. The neutrality ratchet in `tests/contracts/test_architecture_fitness.py`
bans the literal strings `"acme"` and `"contoso"` from Zone A/B/C (`_PHASE7_D24_PROGRAM_LITERALS`).

## Consequences

Easier: adding a new program requires only a `slice_contracts.yaml` entry, not a code change;
Zone A passes the neutrality contract. Harder: the legacy-compat test path (`build_slice_source_health_summary_for_legacy_compat_tests`) now always gets `None`, which is the correct sentinel for "use the YAML". Explicitly rejected: a central registry of program defaults in Zone A (still couples core to specific programs).

## References

- `src/core/decision_source_defaults.py` — implementation
- `programs/acme/slice_contracts.yaml §decision_sources` — per-program config
- `tests/contracts/test_architecture_fitness.py` — neutrality ratchet (`_PHASE7_D24_PROGRAM_LITERALS`)
- `specs/gaps.md GAP-14`
