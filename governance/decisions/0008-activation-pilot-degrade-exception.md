# ADR-0008: Activation Pilot Degrade Exception

**Status:** Proposed

**Owner:** activation-tpm

**Expires on:** 2026-07-31

## Context

Azure Content Safety or LLM provisioning can block the first real activation proof even when the report read path, lineage, privacy, operator loop, and counterfactual verifier are ready. The activation spec allows a narrow pilot-local exception for the AG-1 proof only.

## Decision

The pilot degrade exception is proof-only. It may be used to demonstrate that a human-approved fact changes a real XPF newsletter with reverse-linkable lineage, but it never counts toward AG-3 or AG-4 authority promotion.

## Constraints

- `proof_only: true`
- `blocks_authority_cycles: true`
- Any render produced under the exception must carry explicit degraded provenance.
- Any authority flip still requires non-degraded `is_clean_cycle()` evidence and the normal rollback drill.
